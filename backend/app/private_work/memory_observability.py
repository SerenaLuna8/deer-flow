"""Content-free operational observations shared by Memory services."""

from __future__ import annotations

import logging

logger = logging.getLogger("app.private_work.memory")

_MEMORY_OPERATIONS = frozenset(
    {
        "dream",
        "get",
        "get_version",
        "get_with_injection_advisory",
        "list_episodes",
        "list_pending",
        "list_versions",
        "prepare_admit",
        "prepare_cancel",
        "prepare_read",
        "prepare_read_latest",
        "restore",
    }
)
_FAILURE_CATEGORIES = frozenset({"data_integrity", "database", "internal"})


def record_memory_failure(
    operation: str,
    error: Exception,
    *,
    failure_category: str,
) -> None:
    """Emit only closed routing data and the exception's class name."""

    safe_operation = operation if operation in _MEMORY_OPERATIONS else "unknown"
    safe_category = failure_category if failure_category in _FAILURE_CATEGORIES else "internal"
    logger.error(
        "Memory operation failed: operation=%s failure_category=%s failure_type=%s",
        safe_operation,
        safe_category,
        type(error).__name__,
    )


__all__ = ["record_memory_failure"]
