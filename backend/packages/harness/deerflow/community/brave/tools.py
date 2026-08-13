"""
Web and image search tools powered by the Brave Search API.

Brave Search provides web and image results from an independent search index
via a REST API. An API key is required. Sign up at
https://brave.com/search/api/ to get one.

Unlike the DuckDuckGo ``backend: brave`` option (which scrapes results via the
DDGS aggregator), this provider calls the official Brave Search API directly,
giving structured results, authenticated quota, and a documented SLA.
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
from deerflow.community.url_safety import (
    is_url_value_present as _is_url_present,
)
from deerflow.community.url_safety import (
    sanitize_public_http_reference_url as _safe_public_url,
)
from deerflow.config import get_app_config

logger = logging.getLogger(__name__)

_BRAVE_WEB_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_BRAVE_IMAGES_ENDPOINT = "https://api.search.brave.com/res/v1/images/search"
_DEFAULT_MAX_RESULTS = 5
# Brave Search API caps the `count` parameter at 20 results per request.
_BRAVE_WEB_MAX_COUNT = 20
# Brave Image Search supports larger batches than web search.
_BRAVE_IMAGE_MAX_COUNT = 200
_api_key_warned: set[str] = set()


def _get_api_key(tool_name: str = "web_search") -> str | None:
    config = get_app_config().get_tool_config(tool_name)
    if config is not None:
        api_key = (config.model_extra or {}).get("api_key")
        if isinstance(api_key, str) and api_key.strip():
            return api_key.strip()
    env_key = os.getenv("BRAVE_SEARCH_API_KEY")
    if isinstance(env_key, str) and env_key.strip():
        return env_key.strip()
    return None


def _coerce_max_results(
    value: object,
    *,
    default: int = _DEFAULT_MAX_RESULTS,
    max_allowed: int = _BRAVE_WEB_MAX_COUNT,
) -> int:
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid Brave Search max_results=%r; using default %s",
            value,
            default,
        )
        coerced = default

    return max(1, min(coerced, max_allowed))


def _clean_query(query: str, *, max_length: int = 400) -> str:
    query = query.strip()
    if len(query) > max_length:
        query = query[:max_length]
    return query


def _missing_key_error(query: str, tool_name: str) -> str:
    if tool_name not in _api_key_warned:
        _api_key_warned.add(tool_name)
        logger.warning(
            "Brave Search API key is not set for '%s'. Set BRAVE_SEARCH_API_KEY in your environment or provide api_key in config.yaml. Sign up at https://brave.com/search/api/",
            tool_name,
        )
    return community_error_json(
        CommunityToolError(
            provider="brave",
            code="configuration_error",
            message="BRAVE_SEARCH_API_KEY is not configured",
            retryable=False,
        ),
        query=query,
    )


def _unexpected_format_error(query: str, *, service_name: str = "Brave Search") -> str:
    return community_error_json(
        CommunityToolError(
            provider="brave",
            code="provider_response_invalid",
            message=f"{service_name} returned an unexpected response format",
            retryable=True,
        ),
        query=query,
    )


def _brave_http_error(query: str, *, service_name: str, status_code: int) -> str:
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
            provider="brave",
            code=code,
            message=f"{service_name} API request failed with HTTP {status_code}",
            retryable=retryable,
        ),
        query=query,
    )


def _brave_get(
    endpoint: str,
    api_key: str,
    query: str,
    params: dict[str, object],
    *,
    service_name: str,
) -> tuple[dict | None, str | None]:
    headers = {
        "X-Subscription-Token": api_key,
        "Accept": "application/json",
    }
    try:
        with httpx.Client(timeout=30) as client:
            response = client.get(endpoint, headers=headers, params=params)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            logger.error("%s returned an unexpected payload type: %s", service_name, type(data).__name__)
            return None, _unexpected_format_error(query, service_name=service_name)
        return data, None
    except httpx.HTTPStatusError as e:
        logger.error(
            "%s API returned HTTP %s",
            service_name,
            e.response.status_code,
        )
        return None, _brave_http_error(
            query,
            service_name=service_name,
            status_code=e.response.status_code,
        )
    except Exception as exc:
        logger.error(
            "%s request failed; provider_error_type=%s",
            service_name,
            type(exc).__name__,
        )
        return None, community_error_json(
            CommunityToolError(
                provider="brave",
                code="provider_unavailable",
                message=f"{service_name} is temporarily unavailable",
                retryable=True,
            ),
            query=query,
        )


@tool("web_search", parse_docstring=True)
def web_search_tool(query: str, max_results: int = 5) -> str:
    """Search the web for information using Brave Search.

    Args:
        query: Search keywords describing what you want to find. Be specific for better results.
        max_results: Maximum number of search results to return. Default is 5.
    """
    config = get_app_config().get_tool_config("web_search")
    if config is not None and "max_results" in (config.model_extra or {}):
        max_results = config.model_extra["max_results"]

    count = _coerce_max_results(max_results, max_allowed=_BRAVE_WEB_MAX_COUNT)
    query = _clean_query(query)

    api_key = _get_api_key("web_search")
    if not api_key:
        return _missing_key_error(query, "web_search")

    params = {"q": query, "count": count, "text_decorations": False}

    data, error_json = _brave_get(_BRAVE_WEB_ENDPOINT, api_key, query, params, service_name="Brave Search")
    if error_json is not None:
        return error_json

    web_results = (data.get("web") or {}).get("results", [])
    if not web_results:
        return no_results_json(
            provider="brave",
            query=query,
            message="No results found",
        )

    normalized_results = [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": r.get("description", ""),
        }
        for r in web_results
    ]

    output = {
        "query": query,
        "total_results": len(normalized_results),
        "results": normalized_results,
    }
    return json.dumps(output, indent=2, ensure_ascii=False)


@tool("image_search", parse_docstring=True)
def image_search_tool(query: str, max_results: int = 5) -> str:
    """Search for images online using Brave Image Search. Use this tool BEFORE image generation to find reference images for characters, portraits, objects, scenes, or any content requiring visual accuracy.

    The returned image URLs can be used as reference images in image generation to significantly improve quality.

    Args:
        query: Search keywords describing the images you want to find. Be specific for better results.
        max_results: Maximum number of images to return. Default is 5, capped at 200.
    """
    config = get_app_config().get_tool_config("image_search")
    extra = (config.model_extra or {}) if config is not None else {}
    if "max_results" in extra:
        max_results = extra["max_results"]
    count = _coerce_max_results(max_results, max_allowed=_BRAVE_IMAGE_MAX_COUNT)
    query = _clean_query(query)

    api_key = _get_api_key("image_search")
    if not api_key:
        return _missing_key_error(query, "image_search")

    params: dict[str, object] = {"q": query, "count": count}
    for key in ("country", "search_lang", "safesearch", "spellcheck"):
        if key in extra:
            params[key] = extra[key]

    data, error_json = _brave_get(
        _BRAVE_IMAGES_ENDPOINT,
        api_key,
        query,
        params,
        service_name="Brave Image Search",
    )
    if error_json is not None:
        return error_json

    images = data.get("results")
    if images is None:
        images = []
    if not isinstance(images, list):
        logger.error("Brave Image Search returned unexpected 'results' payload type: %s", type(images).__name__)
        return _unexpected_format_error(query, service_name="Brave Image Search")
    if not images:
        return no_results_json(
            provider="brave",
            query=query,
            message="No images found",
        )

    normalized_results = []
    for item in images:
        if not isinstance(item, dict):
            continue
        thumbnail = item.get("thumbnail") if isinstance(item.get("thumbnail"), dict) else {}
        properties = item.get("properties") if isinstance(item.get("properties"), dict) else {}
        raw_image = properties.get("url")
        raw_thumb = thumbnail.get("src")
        raw_source = item.get("url")

        safe_image = _safe_public_url(raw_image)
        safe_thumb = _safe_public_url(raw_thumb)
        safe_source = _safe_public_url(raw_source)

        # Surface a URL and remember which dict it came from, so the reported
        # width/height describe the URL we actually return rather than a
        # dropped one.
        if safe_image:
            image_url, image_dims = safe_image, properties
        elif not _is_url_present(raw_image):
            image_url, image_dims = safe_thumb, thumbnail
        else:
            image_url, image_dims = "", {}

        if safe_thumb:
            thumbnail_url, thumb_dims = safe_thumb, thumbnail
        elif not _is_url_present(raw_thumb):
            thumbnail_url, thumb_dims = safe_image, properties
        else:
            thumbnail_url, thumb_dims = "", {}

        if not image_url and not thumbnail_url:
            continue

        dims = image_dims if image_url else thumb_dims

        normalized_results.append(
            {
                "title": item.get("title", ""),
                "image_url": image_url,
                "thumbnail_url": thumbnail_url,
                "source_url": safe_source,
                "source": item.get("source", ""),
                "width": dims.get("width"),
                "height": dims.get("height"),
            }
        )
        if len(normalized_results) >= count:
            break

    if not normalized_results:
        return no_results_json(
            provider="brave",
            query=query,
            message="No safe image URLs found",
            code="no_safe_results",
        )

    output = {
        "query": query,
        "total_results": len(normalized_results),
        "results": normalized_results,
        "usage_hint": "Use the 'image_url' values as reference images in image generation. Download them first if needed.",
    }
    return json.dumps(output, indent=2, ensure_ascii=False)
