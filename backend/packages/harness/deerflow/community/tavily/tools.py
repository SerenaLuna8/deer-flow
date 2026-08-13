import json
import logging

from langchain.tools import tool
from langgraph.errors import GraphBubbleUp
from tavily import TavilyClient

from deerflow.community.errors import CommunityToolError, community_error_json, no_results_json
from deerflow.community.url_safety import sanitize_public_http_reference_url
from deerflow.config import get_app_config

logger = logging.getLogger(__name__)


def _tavily_error(context: str, error: Exception) -> str:
    if isinstance(error, CommunityToolError):
        return community_error_json(error, query=context)
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None) or getattr(error, "status_code", None)
    if error.__class__.__name__ == "MissingAPIKeyError" or isinstance(
        error,
        (FileNotFoundError, TypeError, ValueError),
    ):
        code, message, retryable = "configuration_error", "Tavily is not configured correctly", False
    elif status_code in {401, 403}:
        code, message, retryable = "provider_authentication_failed", "Tavily authentication failed", False
    elif status_code == 429:
        code, message, retryable = "provider_rate_limited", "Tavily rate limit exceeded", True
    elif isinstance(status_code, int) and status_code < 500:
        code, message, retryable = "provider_request_failed", "Tavily request failed", False
    else:
        code, message, retryable = "provider_unavailable", "Tavily is temporarily unavailable", True
    logger.error("Tavily request failed; provider_error_type=%s", type(error).__name__)
    return community_error_json(
        CommunityToolError(provider="tavily", code=code, message=message, retryable=retryable),
        query=context,
    )


def _tavily_url_error(url: str) -> str | None:
    # Tavily performs extraction in its remote SaaS. This output-boundary check
    # intentionally avoids local DNS resolution; a local downloader must use
    # validate_public_http_url and revalidate redirects.
    if sanitize_public_http_reference_url(url):
        return None
    return community_error_json(
        CommunityToolError(
            provider="tavily",
            code="url_not_public",
            message="Only public http(s) URLs may be fetched",
            retryable=False,
        ),
        query=url,
    )


def _get_tavily_client() -> TavilyClient:
    config = get_app_config().get_tool_config("web_search")
    api_key = None
    if config is not None and "api_key" in config.model_extra:
        api_key = config.model_extra.get("api_key")
    return TavilyClient(api_key=api_key)


@tool("web_search", parse_docstring=True)
def web_search_tool(query: str) -> str:
    """Search the web.

    Args:
        query: The query to search for.
    """
    try:
        config = get_app_config().get_tool_config("web_search")
        max_results = 5
        extra = (config.model_extra or {}) if config is not None else {}
        if "max_results" in extra:
            max_results = extra.get("max_results")

        client = _get_tavily_client()
        res = client.search(query, max_results=max_results)
        if not isinstance(res, dict) or not isinstance(res.get("results"), list):
            raise CommunityToolError(
                provider="tavily",
                code="provider_response_invalid",
                message="Tavily returned an unexpected response format",
                retryable=True,
            )
        raw_results = res["results"]
        normalized_results = []
        for result in raw_results:
            if not isinstance(result, dict):
                continue
            safe_url = sanitize_public_http_reference_url(result.get("url"))
            if not safe_url:
                continue
            normalized_results.append(
                {
                    "title": result.get("title", ""),
                    "url": safe_url,
                    "snippet": result.get("content", ""),
                }
            )
        if not normalized_results:
            return no_results_json(
                provider="tavily",
                query=query,
                message="No safe results found",
                code="no_safe_results" if raw_results else "no_results",
            )
        return json.dumps(normalized_results, indent=2, ensure_ascii=False)
    except GraphBubbleUp:
        raise
    except Exception as error:
        return _tavily_error(query, error)


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
    url_error = _tavily_url_error(url)
    if url_error is not None:
        return url_error

    try:
        client = _get_tavily_client()
        res = client.extract([url])
        if not isinstance(res, dict):
            raise CommunityToolError(
                provider="tavily",
                code="provider_response_invalid",
                message="Tavily returned an unexpected response format",
                retryable=True,
            )
        if res.get("failed_results"):
            raise CommunityToolError(
                provider="tavily",
                code="provider_request_failed",
                message="Tavily could not fetch the requested URL",
                retryable=False,
            )
        results = res.get("results")
        if not isinstance(results, list) or not results:
            return no_results_json(
                provider="tavily",
                query=url,
                message="No results found",
            )
        result = results[0]
        if not isinstance(result, dict):
            raise CommunityToolError(
                provider="tavily",
                code="provider_response_invalid",
                message="Tavily returned an unexpected response format",
                retryable=True,
            )
        content = result.get("raw_content") or ""
        if not content:
            return no_results_json(
                provider="tavily",
                query=url,
                message="No content found",
            )
        return f"# {result.get('title', 'Untitled')}\n\n{content[:4096]}"
    except GraphBubbleUp:
        raise
    except Exception as error:
        return _tavily_error(url, error)
