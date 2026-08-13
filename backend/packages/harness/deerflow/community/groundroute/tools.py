"""GroundRoute community web search + fetch tools.

GroundRoute (https://groundroute.ai) is a meta search layer: one API in front of
six search engines (Serper, Brave, Exa, Tavily, Firecrawl, Perplexity). It routes
each query to the cheapest engine that clears a quality bar and caches repeats, so
high-volume research runs keep working when one engine is down and pay no more than
going to a single engine direct. Pricing is gain-share: the caller keeps about half
of any cache savings.

This module is self-contained (httpx only, no GroundRoute SDK). The /v1/search
request and response mapping mirrors the GroundRoute MCP server and the verified
Langflow component:
  results[] = {url, title, snippet, content, source_engine, published_at}

`web_search` returns a normalized JSON list of {title, url, snippet, source_engine}.
`web_fetch` reads one URL via GroundRoute mode=page and returns its extracted text.
"""

import json
import logging
import os

import httpx
from langchain.tools import tool

from deerflow.community.errors import (
    CommunityToolError,
    community_error_json,
    no_results_json,
)
from deerflow.community.url_safety import sanitize_public_http_reference_url
from deerflow.config import get_app_config

logger = logging.getLogger(__name__)

_GROUNDROUTE_ENDPOINT = "https://api.groundroute.ai/v1/search"
_DEFAULT_MAX_RESULTS = 5
# GroundRoute clamps max_results to 1-50 server-side; clamp here too to mirror it.
_MAX_RESULTS_CAP = 50
_TIMEOUT_S = 30.0
_FETCH_SNIPPET_LIMIT = 4096
# Warn at most once per tool ("web_search" / "web_fetch") about a missing key.
_api_key_warned: set[str] = set()


def _get_api_key(tool_name: str) -> str | None:
    """Resolve the GroundRoute key from a given tool's config block, then the env var.

    `tool_name` is the config section to read (web_search vs web_fetch) so a flow that
    runs GroundRoute for fetch but a different engine for search still reads the right
    key. Mirrors serper/exa/firecrawl, which all take the tool name.
    """
    config = get_app_config().get_tool_config(tool_name)
    if config is not None:
        api_key = (config.model_extra or {}).get("api_key")
        if isinstance(api_key, str) and api_key.strip():
            return api_key.strip()
    return os.getenv("GROUNDROUTE_API_KEY")


def _coerce_max_results(value: object, *, default: int = _DEFAULT_MAX_RESULTS) -> int:
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        logger.warning("Invalid GroundRoute max_results=%r; using default %s", value, default)
        coerced = default
    return max(1, min(coerced, _MAX_RESULTS_CAP))


def _missing_key_error(tool_name: str, **context: str) -> str:
    if tool_name not in _api_key_warned:
        _api_key_warned.add(tool_name)
        logger.warning(
            "GroundRoute API key is not set for '%s'. Set GROUNDROUTE_API_KEY in your environment or provide api_key in config.yaml. Get a free key at https://groundroute.ai/keys",
            tool_name,
        )
    return community_error_json(
        CommunityToolError(
            provider="groundroute",
            code="configuration_error",
            message="GROUNDROUTE_API_KEY is not configured",
            retryable=False,
        ),
        query=context.get("query") or context.get("url") or "",
    )


def _groundroute_http_error(query: str, status_code: int) -> str:
    if status_code in {401, 403}:
        code, retryable = "provider_authentication_failed", False
    elif status_code == 429:
        code, retryable = "provider_rate_limited", True
    elif status_code >= 500:
        code, retryable = "provider_unavailable", True
    else:
        code, retryable = "provider_request_failed", False
    return community_error_json(
        CommunityToolError(
            provider="groundroute",
            code=code,
            message=f"GroundRoute API request failed with HTTP {status_code}",
            retryable=retryable,
        ),
        query=query,
    )


def _groundroute_unavailable(query: str) -> str:
    return community_error_json(
        CommunityToolError(
            provider="groundroute",
            code="provider_unavailable",
            message="GroundRoute is temporarily unavailable",
            retryable=True,
        ),
        query=query,
    )


def _post_search(api_key: str, body: dict) -> dict:
    with httpx.Client(timeout=_TIMEOUT_S) as client:
        response = client.post(
            _GROUNDROUTE_ENDPOINT,
            json=body,
            headers={"Authorization": f"Bearer {api_key}"},
        )
    response.raise_for_status()
    return response.json()


@tool("web_search", parse_docstring=True)
def web_search_tool(query: str, max_results: int | None = None) -> str:
    """Search the web for information using GroundRoute.

    GroundRoute routes the query across six search engines and returns the result
    set from the engine it selected, with failover if one engine is unavailable.

    Args:
        query: Search keywords describing what you want to find. Be specific for better results.
        max_results: Maximum number of search results to return. If omitted, uses the configured value (default 5). Clamped to 1-50.
    """
    # Honor the caller-supplied max_results; fall back to config only when omitted.
    if max_results is None:
        config = get_app_config().get_tool_config("web_search")
        if config is not None:
            max_results = (config.model_extra or {}).get("max_results")
    count = _DEFAULT_MAX_RESULTS if max_results is None else _coerce_max_results(max_results)

    api_key = _get_api_key("web_search")
    if not api_key:
        return _missing_key_error("web_search", query=query)

    try:
        data = _post_search(api_key, {"query": query, "max_results": count})
    except httpx.HTTPStatusError as e:
        logger.error("GroundRoute API returned HTTP %s", e.response.status_code)
        return _groundroute_http_error(query, e.response.status_code)
    except Exception as exc:
        logger.error(
            "GroundRoute search failed; provider_error_type=%s",
            type(exc).__name__,
        )
        return _groundroute_unavailable(query)

    results = data.get("results") or []
    if not results:
        return no_results_json(
            provider="groundroute",
            query=query,
            message="No results found",
        )

    normalized_results = [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("snippet", ""),
            "source_engine": r.get("source_engine", ""),
        }
        for r in results
    ]
    return json.dumps(normalized_results, indent=2, ensure_ascii=False)


@tool("web_fetch", parse_docstring=True)
def web_fetch_tool(url: str) -> str:
    """Fetch the contents of a web page at a given URL via GroundRoute.
    Only fetch EXACT URLs that have been provided directly by the user or have been returned in results from the web_search and web_fetch tools.
    This tool can NOT access content that requires authentication, such as private Google Docs or pages behind login walls.
    Do NOT add www. to URLs that do NOT have them.
    URLs must include the schema: https://example.com is a valid URL while example.com is an invalid URL.

    Args:
        url: The URL to fetch the contents of.
    """
    # GroundRoute performs the network fetch in its remote SaaS, not inside the
    # ActWeave deployment. Reject unsafe literal references here without local
    # DNS resolution; a local downloader would require validate_public_http_url.
    if not sanitize_public_http_reference_url(url):
        return community_error_json(
            CommunityToolError(
                provider="groundroute",
                code="url_not_public",
                message="Only public http(s) URLs may be fetched",
                retryable=False,
            ),
            query=url,
        )

    api_key = _get_api_key("web_fetch")
    if not api_key:
        return _missing_key_error("web_fetch", url=url)

    try:
        data = _post_search(api_key, {"query": url, "mode": "page", "max_results": 1})
    except httpx.HTTPStatusError as e:
        logger.error("GroundRoute fetch returned HTTP %s", e.response.status_code)
        return _groundroute_http_error(url, e.response.status_code)
    except Exception as exc:
        logger.error(
            "GroundRoute fetch failed; provider_error_type=%s",
            type(exc).__name__,
        )
        return _groundroute_unavailable(url)

    results = data.get("results") or []
    if not results:
        return no_results_json(
            provider="groundroute",
            query=url,
            message="No results found",
        )

    result = results[0]
    content = result.get("content") or result.get("snippet") or ""
    title = result.get("title", "")
    return f"# {title}\n\n{content[:_FETCH_SNIPPET_LIMIT]}"
