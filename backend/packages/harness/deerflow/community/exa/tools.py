import json
import logging

from exa_py import Exa
from langchain.tools import tool
from langgraph.errors import GraphBubbleUp

from deerflow.community.errors import CommunityToolError, community_error_json, no_results_json
from deerflow.community.url_safety import sanitize_public_http_reference_url
from deerflow.config import get_app_config

logger = logging.getLogger(__name__)


def _exa_error(context: str, error: Exception) -> str:
    if isinstance(error, CommunityToolError):
        return community_error_json(error, query=context)
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None) or getattr(error, "status_code", None)
    if isinstance(error, (FileNotFoundError, TypeError, ValueError)):
        code, message, retryable = (
            "configuration_error",
            "Exa is not configured correctly",
            False,
        )
    elif status_code in {401, 403}:
        code, message, retryable = (
            "provider_authentication_failed",
            "Exa authentication failed",
            False,
        )
    elif status_code == 429:
        code, message, retryable = (
            "provider_rate_limited",
            "Exa rate limit exceeded",
            True,
        )
    elif isinstance(status_code, int) and status_code < 500:
        code, message, retryable = (
            "provider_request_failed",
            "Exa request failed",
            False,
        )
    else:
        code, message, retryable = (
            "provider_unavailable",
            "Exa is temporarily unavailable",
            True,
        )
    logger.error("Exa request failed; provider_error_type=%s", type(error).__name__)
    return community_error_json(
        CommunityToolError(
            provider="exa",
            code=code,
            message=message,
            retryable=retryable,
        ),
        query=context,
    )


def _exa_url_error(url: str) -> str | None:
    # Exa performs extraction in its remote SaaS. This is therefore a
    # reference-boundary check without local DNS resolution; any future local
    # downloader must use validate_public_http_url instead.
    if sanitize_public_http_reference_url(url):
        return None
    return community_error_json(
        CommunityToolError(
            provider="exa",
            code="url_not_public",
            message="Only public http(s) URLs may be fetched",
            retryable=False,
        ),
        query=url,
    )


def _get_exa_client(tool_name: str = "web_search") -> Exa:
    config = get_app_config().get_tool_config(tool_name)
    api_key = None
    if config is not None and "api_key" in config.model_extra:
        api_key = config.model_extra.get("api_key")
    return Exa(api_key=api_key)


@tool("web_search", parse_docstring=True)
def web_search_tool(query: str) -> str:
    """Search the web.

    Args:
        query: The query to search for.
    """
    try:
        config = get_app_config().get_tool_config("web_search")
        max_results = 5
        search_type = "auto"
        contents_max_characters = 1000
        if config is not None:
            extra = config.model_extra or {}
            max_results = extra.get("max_results", max_results)
            search_type = extra.get("search_type", search_type)
            contents_max_characters = extra.get("contents_max_characters", contents_max_characters)

        client = _get_exa_client()
        res = client.search(
            query,
            type=search_type,
            num_results=max_results,
            contents={"highlights": {"max_characters": contents_max_characters}},
        )

        normalized_results = []
        for result in res.results:
            safe_url = sanitize_public_http_reference_url(result.url)
            if not safe_url:
                continue
            normalized_results.append(
                {
                    "title": result.title or "",
                    "url": safe_url,
                    "snippet": "\n".join(result.highlights) if result.highlights else "",
                }
            )
        if not normalized_results:
            return no_results_json(
                provider="exa",
                query=query,
                message="No safe results found",
                code="no_safe_results" if res.results else "no_results",
            )
        json_results = json.dumps(normalized_results, indent=2, ensure_ascii=False)
        return json_results
    except GraphBubbleUp:
        raise
    except Exception as error:
        return _exa_error(query, error)


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
    url_error = _exa_url_error(url)
    if url_error is not None:
        return url_error

    try:
        client = _get_exa_client("web_fetch")
        res = client.get_contents([url], text={"max_characters": 4096})

        if res.results:
            result = res.results[0]
            title = result.title or "Untitled"
            text = result.text or ""
            if not text.strip():
                return no_results_json(
                    provider="exa",
                    query=url,
                    message="No content found",
                )
            return f"# {title}\n\n{text[:4096]}"
        return no_results_json(
            provider="exa",
            query=url,
            message="No results found",
        )
    except GraphBubbleUp:
        raise
    except Exception as error:
        return _exa_error(url, error)
