import json
import logging

from firecrawl import FirecrawlApp
from langchain.tools import tool
from langgraph.errors import GraphBubbleUp

from deerflow.community.errors import CommunityToolError, community_error_json, no_results_json
from deerflow.community.url_safety import sanitize_public_http_reference_url
from deerflow.config import get_app_config

logger = logging.getLogger(__name__)


def _firecrawl_error(context: str, error: Exception) -> str:
    if isinstance(error, CommunityToolError):
        return community_error_json(error, query=context)
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None) or getattr(error, "status_code", None)
    if isinstance(error, (FileNotFoundError, TypeError, ValueError)):
        code, message, retryable = "configuration_error", "Firecrawl is not configured correctly", False
    elif status_code in {401, 403}:
        code, message, retryable = "provider_authentication_failed", "Firecrawl authentication failed", False
    elif status_code == 429:
        code, message, retryable = "provider_rate_limited", "Firecrawl rate limit exceeded", True
    elif isinstance(status_code, int) and status_code < 500:
        code, message, retryable = "provider_request_failed", "Firecrawl request failed", False
    else:
        code, message, retryable = "provider_unavailable", "Firecrawl is temporarily unavailable", True
    logger.error("Firecrawl request failed; provider_error_type=%s", type(error).__name__)
    return community_error_json(
        CommunityToolError(provider="firecrawl", code=code, message=message, retryable=retryable),
        query=context,
    )


def _firecrawl_url_error(url: str) -> str | None:
    # The managed Firecrawl service fetches the target remotely. Reject unsafe
    # literal references here without local DNS; local/self-hosted fetchers must
    # use validate_public_http_url at their delegated-fetch boundary.
    if sanitize_public_http_reference_url(url):
        return None
    return community_error_json(
        CommunityToolError(
            provider="firecrawl",
            code="url_not_public",
            message="Only public http(s) URLs may be fetched",
            retryable=False,
        ),
        query=url,
    )


def _get_firecrawl_client(tool_name: str = "web_search") -> FirecrawlApp:
    config = get_app_config().get_tool_config(tool_name)
    api_key = None
    if config is not None and "api_key" in config.model_extra:
        api_key = config.model_extra.get("api_key")
    return FirecrawlApp(api_key=api_key)  # type: ignore[arg-type]


@tool("web_search", parse_docstring=True)
def web_search_tool(query: str) -> str:
    """Search the web.

    Args:
        query: The query to search for.
    """
    try:
        config = get_app_config().get_tool_config("web_search")
        max_results = 5
        if config is not None:
            max_results = (config.model_extra or {}).get("max_results", max_results)

        client = _get_firecrawl_client("web_search")
        result = client.search(query, limit=max_results)

        # result.web contains list of SearchResultWeb objects
        web_results = result.web or []
        normalized_results = []
        for item in web_results:
            safe_url = sanitize_public_http_reference_url(getattr(item, "url", ""))
            if not safe_url:
                continue
            normalized_results.append(
                {
                    "title": getattr(item, "title", "") or "",
                    "url": safe_url,
                    "snippet": getattr(item, "description", "") or "",
                }
            )
        if not normalized_results:
            return no_results_json(
                provider="firecrawl",
                query=query,
                message="No safe results found",
                code="no_safe_results" if web_results else "no_results",
            )
        json_results = json.dumps(normalized_results, indent=2, ensure_ascii=False)
        return json_results
    except GraphBubbleUp:
        raise
    except Exception as error:
        return _firecrawl_error(query, error)


@tool("web_fetch", parse_docstring=True)
def web_fetch_tool(url: str) -> str:
    """Fetch the contents of a web page at a given URL.
    Only fetch EXACT URLs that have been provided directly by the user or have been returned in results from the web_search and web_fetch tools.
    This tool can NOT access content that requires authentication, such as private Google Docs or pages behind login walls.
    Do NOT add www. to URLs that do NOT have them.
    URLs must include the schema: https://example.com is a valid URL while example.com is an invalid URL.

    Args:
        url: The URL to fetch the contents of.
    """
    url_error = _firecrawl_url_error(url)
    if url_error is not None:
        return url_error

    try:
        client = _get_firecrawl_client("web_fetch")
        result = client.scrape(url, formats=["markdown"])

        markdown_content = result.markdown or ""
        metadata = result.metadata
        title = metadata.title if metadata and metadata.title else "Untitled"

        if not markdown_content:
            return no_results_json(
                provider="firecrawl",
                query=url,
                message="No content found",
            )
    except GraphBubbleUp:
        raise
    except Exception as error:
        return _firecrawl_error(url, error)

    return f"# {title}\n\n{markdown_content[:4096]}"
