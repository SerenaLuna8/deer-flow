"""Project-scoped Thread metadata ORM and repository."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from deerflow.persistence.thread_meta.base import InvalidMetadataFilterError
    from deerflow.persistence.thread_meta.model import ThreadMetaRow
    from deerflow.persistence.thread_meta.sql import ThreadMetaRepository

__all__ = [
    "InvalidMetadataFilterError",
    "ThreadMetaRepository",
    "ThreadMetaRow",
]


def __getattr__(name: str) -> Any:
    """Keep public imports compatible without loading the SQL/runtime stack.

    Persistence model consumers frequently import the concrete ``model``
    submodule. Eagerly importing ``sql`` here made that otherwise-light path
    initialize the complete LangGraph runtime.
    """

    if name == "InvalidMetadataFilterError":
        from deerflow.persistence.thread_meta.base import (
            InvalidMetadataFilterError,
        )

        return InvalidMetadataFilterError
    if name == "ThreadMetaRow":
        from deerflow.persistence.thread_meta.model import ThreadMetaRow

        return ThreadMetaRow
    if name == "ThreadMetaRepository":
        from deerflow.persistence.thread_meta.sql import ThreadMetaRepository

        return ThreadMetaRepository
    raise AttributeError(name)
