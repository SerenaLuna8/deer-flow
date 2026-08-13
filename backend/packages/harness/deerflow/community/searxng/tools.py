import json
import logging

from langchain.tools import tool
from langgraph.errors import GraphBubbleUp

from deerflow.community.errors import CommunityToolError, community_error_json, no_results_json
from deerflow.community.url_safety import sanitize_public_http_reference_url
from deerflow.config import get_app_config

from .searxng_client import SearxngClient

logger = logging.getLogger(__name__)


def _get_tool_config(tool_name: str) -> dict | None:
    """Get tool config extras safely, returning None if not configured."""
    config = get_app_config().get_tool_config(tool_name)
    if config is None:
        return None
    extras = config.model_extra
    return extras if extras is not None else {}


def _get_searxng_client() -> SearxngClient:
    cfg = _get_tool_config("web_search")
    base_url = "http://localhost:8088"
    if cfg is not None:
        base_url = cfg.get("base_url", base_url)
    return SearxngClient(base_url=base_url)


@tool("web_search", parse_docstring=True)
async def web_search_tool(query: str) -> str:
    """Search the web using SearXNG.

    Args:
        query: The query to search for.
    """
    try:
        cfg = _get_tool_config("web_search")
        max_results = 5
        if cfg is not None:
            raw = cfg.get("max_results", max_results)
            max_results = int(raw) if not isinstance(raw, int) else raw

        client = _get_searxng_client()
        results = await client.search(query, max_results=max_results)

        normalized = []
        for result in results:
            if not isinstance(result, dict):
                continue
            # SearXNG returns citations; ActWeave does not fetch these URLs in
            # this tool. Sanitize the reference without local DNS resolution.
            safe_url = sanitize_public_http_reference_url(result.get("url"))
            if not safe_url:
                continue
            normalized.append(
                {
                    "title": result.get("title", ""),
                    "url": safe_url,
                    "snippet": result.get("content", ""),
                }
            )
        if not normalized:
            return no_results_json(
                provider="searxng",
                query=query,
                message="No safe results found",
                code="no_safe_results" if results else "no_results",
            )
        return json.dumps(normalized, indent=2, ensure_ascii=False)
    except GraphBubbleUp:
        raise
    except CommunityToolError as error:
        return community_error_json(error, query=query)
    except (FileNotFoundError, TypeError, ValueError) as error:
        logger.error("SearXNG configuration failed; provider_error_type=%s", type(error).__name__)
        return community_error_json(
            CommunityToolError(
                provider="searxng",
                code="configuration_error",
                message="SearXNG is not configured correctly",
                retryable=False,
            ),
            query=query,
        )
    except Exception as error:
        logger.error("SearXNG tool failed; provider_error_type=%s", type(error).__name__)
        return community_error_json(
            CommunityToolError(
                provider="searxng",
                code="provider_unavailable",
                message="SearXNG is temporarily unavailable",
                retryable=True,
            ),
            query=query,
        )
