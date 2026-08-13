import json
import logging
import os

from firecrawl import FirecrawlApp
from langchain.tools import tool
from langgraph.errors import GraphBubbleUp

from deerflow.community.errors import CommunityToolError, community_error_json, no_results_json
from deerflow.community.url_safety import sanitize_public_http_reference_url, validate_public_http_url
from deerflow.config import get_app_config

# fastCRW is a Firecrawl-compatible web data engine (single Rust binary; self-host
# or cloud). Because the REST API is Firecrawl-compatible, this provider reuses the
# Firecrawl client and only swaps the base URL. Cloud default points at the managed
# service; override `base_url` in the tool config (or set CRW_API_URL) for self-host.
DEFAULT_BASE_URL = "https://fastcrw.com/api"
logger = logging.getLogger(__name__)


def _fastcrw_error(context: str, error: Exception) -> str:
    if isinstance(error, CommunityToolError):
        return community_error_json(error, query=context)
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None) or getattr(error, "status_code", None)
    if isinstance(error, (FileNotFoundError, TypeError, ValueError)):
        code, message, retryable = "configuration_error", "fastCRW is not configured correctly", False
    elif status_code in {401, 403}:
        code, message, retryable = "provider_authentication_failed", "fastCRW authentication failed", False
    elif status_code == 429:
        code, message, retryable = "provider_rate_limited", "fastCRW rate limit exceeded", True
    elif isinstance(status_code, int) and status_code < 500:
        code, message, retryable = "provider_request_failed", "fastCRW request failed", False
    else:
        code, message, retryable = "provider_unavailable", "fastCRW is temporarily unavailable", True
    logger.error("fastCRW request failed; provider_error_type=%s", type(error).__name__)
    return community_error_json(
        CommunityToolError(provider="fastcrw", code=code, message=message, retryable=retryable),
        query=context,
    )


def _fastcrw_url_error(url: str, validation_error: str | None) -> str | None:
    if validation_error is None:
        return None
    return community_error_json(
        CommunityToolError(
            provider="fastcrw",
            code="url_not_public",
            message="Only public http(s) URLs may be fetched",
            retryable=False,
        ),
        query=url,
    )


def _get_fastcrw_client(tool_name: str = "web_search") -> FirecrawlApp:
    config = get_app_config().get_tool_config(tool_name)
    api_key = None
    base_url = None
    if config is not None:
        if "api_key" in config.model_extra:
            api_key = config.model_extra.get("api_key")
        if "base_url" in config.model_extra:
            base_url = config.model_extra.get("base_url")
    if api_key is None:
        api_key = os.getenv("CRW_API_KEY")
    if base_url is None:
        base_url = os.getenv("CRW_API_URL", DEFAULT_BASE_URL)
    return FirecrawlApp(api_key=api_key, api_url=base_url)  # type: ignore[arg-type]


def _get_tool_config_extra(tool_name: str) -> dict:
    config = get_app_config().get_tool_config(tool_name)
    return dict(config.model_extra or {}) if config is not None else {}


def _coerce_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


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

        client = _get_fastcrw_client("web_search")
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
                provider="fastcrw",
                query=query,
                message="No safe results found",
                code="no_safe_results" if web_results else "no_results",
            )
        json_results = json.dumps(normalized_results, indent=2, ensure_ascii=False)
        return json_results
    except GraphBubbleUp:
        raise
    except Exception as error:
        return _fastcrw_error(query, error)


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
    try:
        cfg = _get_tool_config_extra("web_fetch")
        allow_private_addresses = _coerce_bool(cfg.get("allow_private_addresses"), False)
        # fastCRW can be self-hosted inside the deployment, so this delegated
        # fetch uses DNS-aware validation. The remote-only providers use the
        # weaker reference sanitizer because they never fetch from our network.
        url_error = _fastcrw_url_error(
            url,
            validate_public_http_url(url, allow_private_addresses=allow_private_addresses),
        )
        if url_error is not None:
            return url_error
        client = _get_fastcrw_client("web_fetch")
        result = client.scrape(url, formats=["markdown"])

        markdown_content = result.markdown or ""
        metadata = result.metadata
        title = metadata.title if metadata and metadata.title else "Untitled"

        if not markdown_content:
            return no_results_json(
                provider="fastcrw",
                query=url,
                message="No content found",
            )
    except GraphBubbleUp:
        raise
    except Exception as error:
        return _fastcrw_error(url, error)

    return f"# {title}\n\n{markdown_content[:4096]}"
