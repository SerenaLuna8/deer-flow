"""Read-only project Memory tools.

The model controls only the query. Project, owner, namespace, thread, Run, and
lease coordinates are closed over by the Worker-issued ``__memory_authority``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Annotated

from langchain.tools import tool
from pydantic import Field

from deerflow.agents.memory.manager import get_project_memory_manager
from deerflow.agents.middlewares.input_sanitization_middleware import (
    neutralize_untrusted_tags,
)
from deerflow.sandbox.sandbox import AuthorizationRevoked
from deerflow.tools.types import Runtime

logger = logging.getLogger(__name__)

_MEMORY_AUTHORITY_CONTEXT_KEY = "__memory_authority"
_MAX_RESULT_CONTENT_CHARS = 768
_MAX_RESULT_JSON_CHARS = 20_000
_UNAVAILABLE = {
    "error": {
        "code": "MEMORY_SEARCH_UNAVAILABLE",
        "message": "Project Memory search is unavailable.",
    }
}


def _memory_authority(runtime: Runtime) -> object:
    context = getattr(runtime, "context", None)
    if not isinstance(context, Mapping) or "private_scope" not in context:
        raise RuntimeError("project memory authority is unavailable")
    authority = context.get(_MEMORY_AUTHORITY_CONTEXT_KEY)
    if authority is None or isinstance(authority, Mapping):
        raise RuntimeError("project memory authority is unavailable")
    if not callable(getattr(authority, "load_snapshot", None)):
        raise RuntimeError("project memory authority is unavailable")
    return authority


def _bounded_untrusted_text(value: object, *, max_chars: int) -> str:
    content = neutralize_untrusted_tags(value if isinstance(value, str) else str(value))
    if len(content) <= max_chars:
        return content
    return content[: max_chars - 3].rstrip() + "..."


def _render_response(snapshot_version: int | None, results: tuple[dict, ...]) -> str:
    projected = [
        {
            "id": result["id"],
            "content": _bounded_untrusted_text(
                result["content"],
                max_chars=_MAX_RESULT_CONTENT_CHARS,
            ),
            "category": _bounded_untrusted_text(
                result["category"],
                max_chars=128,
            ),
            "confidence": result["confidence"],
            "createdAt": result["createdAt"],
            "score": result["score"],
            "matchType": result["matchType"],
        }
        for result in results
    ]
    payload = {
        "snapshotVersion": snapshot_version,
        "count": len(projected),
        "results": projected,
    }
    rendered = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    while len(rendered) > _MAX_RESULT_JSON_CHARS and projected:
        projected.pop()
        payload["count"] = len(projected)
        rendered = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return rendered


@tool("memory_search")
async def memory_search_tool(
    runtime: Runtime,
    query: Annotated[
        str,
        Field(
            min_length=1,
            max_length=1_000,
            description="Keywords or a short phrase to recall from this project's Memory.",
        ),
    ],
    category: Annotated[
        str | None,
        Field(
            max_length=32,
            description="Optional exact Memory category, such as preference, context, or correction.",
        ),
    ] = None,
    top_k: Annotated[
        int,
        Field(
            ge=1,
            le=20,
            description="Maximum number of matching facts to return.",
        ),
    ] = 5,
) -> str:
    """Search facts in the current private Run's authorized project Memory."""

    try:
        response = await get_project_memory_manager().asearch(
            authority=_memory_authority(runtime),
            query=query,
            category=category,
            top_k=top_k,
        )
    except AuthorizationRevoked:
        raise
    except Exception as exc:
        logger.warning(
            "Project Memory search failed with %s",
            type(exc).__name__,
        )
        return json.dumps(_UNAVAILABLE, ensure_ascii=False, separators=(",", ":"))
    return _render_response(response.snapshot_version, response.results)


def get_project_memory_tools() -> list:
    """Return the complete model-visible project Memory tool registry."""

    return [memory_search_tool]


__all__ = ["get_project_memory_tools", "memory_search_tool"]
