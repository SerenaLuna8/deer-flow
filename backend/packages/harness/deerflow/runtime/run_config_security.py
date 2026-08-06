"""Shared, authority-free bounds for LangGraph run configuration."""

from __future__ import annotations

from typing import Any

DEFAULT_RECURSION_LIMIT = 100
# This is an authority-free parser bound, not the operator policy. Private Run
# admission applies the current database-backed agent_runtime limit in the same
# transaction that persists the exact policy snapshot.
ABSOLUTE_MAX_RECURSION_LIMIT = 100_000


def clamp_recursion_limit(value: Any, max_limit: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return DEFAULT_RECURSION_LIMIT
    return min(value, max_limit)
