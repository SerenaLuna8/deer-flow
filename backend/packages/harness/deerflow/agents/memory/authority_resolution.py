"""Shared resolution of the Worker-issued opaque Memory authority.

The authority travels only through the Worker-owned runtime context. JSON can
only produce plain dicts, so a dict value is always a forgery and is rejected;
a real authority is a Worker-constructed object exposing the requested method.
"""

from __future__ import annotations

MEMORY_AUTHORITY_CONTEXT_KEY = "__memory_authority"


def resolve_memory_authority(
    context: object,
    *,
    method: str = "load_snapshot",
) -> object | None:
    """Return the trusted Memory authority from a runtime context, or None."""

    mapping = context if isinstance(context, dict) else {}
    authority = mapping.get(MEMORY_AUTHORITY_CONTEXT_KEY)
    if authority is None or isinstance(authority, dict):
        return None
    if not callable(getattr(authority, method, None)):
        return None
    return authority


def memory_recall_available(context: object) -> bool:
    """True when the runtime context carries a search-capable Memory authority."""

    return resolve_memory_authority(context, method="search_episodes") is not None


__all__ = [
    "MEMORY_AUTHORITY_CONTEXT_KEY",
    "memory_recall_available",
    "resolve_memory_authority",
]
