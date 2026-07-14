"""Shared, authority-free bounds for LangGraph run configuration."""

from __future__ import annotations

from typing import Any

DEFAULT_RECURSION_LIMIT = 100
DEFAULT_MAX_RECURSION_LIMIT = 1000


def resolve_max_recursion_limit() -> int:
    try:
        from deerflow.config.app_config import get_app_config

        return get_app_config().max_recursion_limit
    except Exception:
        return DEFAULT_MAX_RECURSION_LIMIT


def clamp_recursion_limit(value: Any, max_limit: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return DEFAULT_RECURSION_LIMIT
    return min(value, max_limit)
