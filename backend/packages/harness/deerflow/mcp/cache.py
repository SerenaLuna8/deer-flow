"""Fail-closed tombstone for the removed global MCP tool cache."""

from __future__ import annotations

from langchain_core.tools import BaseTool

_mcp_tools_cache: list[BaseTool] | None = None
_cache_initialized = False
_config_mtime: float | None = None


async def initialize_mcp_tools(*, asset_context: object | None = None) -> list[BaseTool]:
    """Global MCP discovery is disabled; exact run tools are materialized by app."""
    return []


def get_cached_mcp_tools(*, asset_context: object | None = None) -> list[BaseTool]:
    """Return no global MCP tools."""
    return []


def reset_mcp_tools_cache() -> None:
    """Clear compatibility state and close any old persistent sessions."""
    global _mcp_tools_cache, _cache_initialized, _config_mtime
    _mcp_tools_cache = None
    _cache_initialized = False
    _config_mtime = None
    try:
        from deerflow.mcp.session_pool import get_session_pool, reset_session_pool

        get_session_pool().close_all_sync()
        reset_session_pool()
    except Exception:
        pass
