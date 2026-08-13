"""
Web and image search tools powered by Serper (Google Search API).

Serper provides real-time Google Search and Google Images results via a JSON
API. An API key is required. Sign up at https://serper.dev to get one.
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

_SERPER_SEARCH_ENDPOINT = "https://google.serper.dev/search"
_SERPER_IMAGES_ENDPOINT = "https://google.serper.dev/images"
_SERPER_MAX_RESULTS = 10
_api_key_warned: set[str] = set()


def _get_api_key(tool_name: str) -> str | None:
    config = get_app_config().get_tool_config(tool_name)
    if config is not None:
        api_key = config.model_extra.get("api_key")
        if isinstance(api_key, str) and api_key.strip():
            return api_key.strip()
    env_key = os.getenv("SERPER_API_KEY")
    if isinstance(env_key, str) and env_key.strip():
        return env_key.strip()
    return None


def _coerce_max_results(value: object, default: int = 5, max_allowed: int = _SERPER_MAX_RESULTS) -> int:
    """Coerce config/parameter input into a bounded positive result count."""
    try:
        count = int(value)
    except (TypeError, ValueError):
        return default
    if count <= 0:
        return default
    return min(count, max_allowed)


def _missing_key_error(query: str, tool_name: str) -> str:
    if tool_name not in _api_key_warned:
        _api_key_warned.add(tool_name)
        logger.warning("Serper API key is not set for '%s'. Set SERPER_API_KEY in your environment or provide api_key in config.yaml. Sign up at https://serper.dev", tool_name)
    return community_error_json(
        CommunityToolError(
            provider="serper",
            code="configuration_error",
            message="SERPER_API_KEY is not configured",
            retryable=False,
        ),
        query=query,
    )


def _unexpected_format_error(query: str) -> str:
    return community_error_json(
        CommunityToolError(
            provider="serper",
            code="provider_response_invalid",
            message="Serper returned an unexpected response format",
            retryable=True,
        ),
        query=query,
    )


def _serper_http_error(query: str, status_code: int) -> str:
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
            provider="serper",
            code=code,
            message=f"Serper API request failed with HTTP {status_code}",
            retryable=retryable,
        ),
        query=query,
    )


def _response_items(data: dict, field: str, query: str) -> tuple[list[dict] | None, str | None]:
    items = data.get(field)
    # Treat a missing or null field as "no results" (some APIs return
    # ``{"organic": null}`` to signal that) rather than a malformed payload.
    if items is None:
        return [], None
    if not isinstance(items, list):
        logger.error("Serper returned unexpected '%s' payload type: %s", field, type(items).__name__)
        return None, _unexpected_format_error(query)
    return [item for item in items if isinstance(item, dict)], None


def _clean_query(query: str) -> str:
    """Normalize a raw query into the value actually sent to Serper."""
    query = query.strip()
    if len(query) > 500:
        query = query[:500]
    return query


def _serper_post(endpoint: str, api_key: str, query: str, max_results: int) -> tuple[dict | None, str | None]:
    """Send a POST request to a Serper endpoint.

    ``query`` is expected to already be normalized via :func:`_clean_query`.

    Returns a ``(data, error_json)`` tuple: on success ``data`` is the parsed
    JSON response and ``error_json`` is ``None``; on failure ``data`` is ``None``
    and ``error_json`` is a serialized structured error ready to return.
    """
    headers = {
        "X-API-KEY": api_key,
        "Content-Type": "application/json",
    }
    payload = {"q": query, "num": max_results}

    try:
        with httpx.Client(timeout=30) as client:
            response = client.post(endpoint, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            logger.error("Serper returned an unexpected payload type: %s", type(data).__name__)
            return None, _unexpected_format_error(query)
        return data, None
    except httpx.HTTPStatusError as e:
        logger.error("Serper API returned HTTP %s", e.response.status_code)
        return None, _serper_http_error(query, e.response.status_code)
    except Exception as exc:
        logger.error(
            "Serper request failed; provider_error_type=%s",
            type(exc).__name__,
        )
        return None, community_error_json(
            CommunityToolError(
                provider="serper",
                code="provider_unavailable",
                message="Serper is temporarily unavailable",
                retryable=True,
            ),
            query=query,
        )


@tool("web_search", parse_docstring=True)
def web_search_tool(query: str, max_results: int = 5) -> str:
    """Search the web for information using Google Search via Serper.

    Args:
        query: Search keywords describing what you want to find. Be specific for better results.
        max_results: Maximum number of search results to return. Default is 5, capped at 10.
    """
    config = get_app_config().get_tool_config("web_search")
    if config is not None and "max_results" in config.model_extra:
        max_results = config.model_extra.get("max_results", max_results)
    max_results = _coerce_max_results(max_results)
    query = _clean_query(query)

    api_key = _get_api_key("web_search")
    if not api_key:
        return _missing_key_error(query, "web_search")

    data, error_json = _serper_post(_SERPER_SEARCH_ENDPOINT, api_key, query, max_results)
    if error_json is not None:
        return error_json

    organic, error_json = _response_items(data, "organic", query)
    if error_json is not None:
        return error_json
    if not organic:
        return no_results_json(
            provider="serper",
            query=query,
            message="No results found",
        )

    # Search result links are returned verbatim (not passed through
    # _safe_public_url): they are surfaced as citations for the model to read,
    # not fetched/downloaded by this tool, unlike image_search image URLs.
    normalized_results = [
        {
            "title": r.get("title", ""),
            "url": r.get("link", ""),
            "content": r.get("snippet", ""),
        }
        for r in organic[:max_results]
    ]

    output = {
        "query": query,
        "total_results": len(normalized_results),
        "results": normalized_results,
    }
    return json.dumps(output, indent=2, ensure_ascii=False)


@tool("image_search", parse_docstring=True)
def image_search_tool(query: str, max_results: int = 5) -> str:
    """Search for images online using Google Images via Serper. Use this tool BEFORE image generation to find reference images for characters, portraits, objects, scenes, or any content requiring visual accuracy.

    The returned image URLs can be used as reference images in image generation to significantly improve quality.

    Args:
        query: Search keywords describing the images you want to find. Be specific for better results (e.g., "Japanese woman street photography 1990s" instead of just "woman").
        max_results: Maximum number of images to return. Default is 5, capped at 10.
    """
    config = get_app_config().get_tool_config("image_search")
    if config is not None and "max_results" in config.model_extra:
        max_results = config.model_extra.get("max_results", max_results)
    max_results = _coerce_max_results(max_results)
    query = _clean_query(query)

    api_key = _get_api_key("image_search")
    if not api_key:
        return _missing_key_error(query, "image_search")

    data, error_json = _serper_post(_SERPER_IMAGES_ENDPOINT, api_key, query, max_results)
    if error_json is not None:
        return error_json

    images, error_json = _response_items(data, "images", query)
    if error_json is not None:
        return error_json
    if not images:
        return no_results_json(
            provider="serper",
            query=query,
            message="No images found",
        )

    normalized_results = []
    for r in images:
        raw_image = r.get("imageUrl")
        raw_thumb = r.get("thumbnailUrl")
        # Evaluate the (non-trivial) SSRF guard once per field instead of twice.
        safe_image = _safe_public_url(raw_image)
        safe_thumb = _safe_public_url(raw_thumb)
        # Cross-fall back only when the other field was *absent*. A field that
        # was present but failed the SSRF filter is left empty rather than
        # collapsed onto its counterpart, so a dropped high-res URL never
        # silently masquerades as the preview (and vice versa), preserving the
        # high-res/preview contract callers rely on.
        image_url = safe_image or (safe_thumb if not _is_url_present(raw_image) else "")
        thumbnail_url = safe_thumb or (safe_image if not _is_url_present(raw_thumb) else "")
        if not image_url and not thumbnail_url:
            continue
        normalized_results.append(
            {
                "title": r.get("title", ""),
                "image_url": image_url,
                "thumbnail_url": thumbnail_url,
            }
        )
        if len(normalized_results) >= max_results:
            break

    if not normalized_results:
        return no_results_json(
            provider="serper",
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
