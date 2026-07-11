"""Thread metadata persistence — ORM, abstract store, and concrete implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from deerflow.persistence.thread_meta.base import InvalidMetadataFilterError, ThreadMetaStore
from deerflow.persistence.thread_meta.memory import MemoryThreadMetaStore
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.persistence.thread_meta.sql import ThreadMetaRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "InvalidMetadataFilterError",
    "MemoryThreadMetaStore",
    "ThreadMetaRepository",
    "ThreadMetaRow",
    "ThreadMetaStore",
    "make_thread_store",
]


def make_thread_store(
    session_factory: async_sessionmaker[AsyncSession],
) -> ThreadMetaStore:
    """Create the PostgreSQL-backed ThreadMetaStore.

    Tests that need an in-memory double construct ``MemoryThreadMetaStore``
    directly instead of routing it through the production factory.
    """
    if session_factory is None:
        raise TypeError("make_thread_store requires a PostgreSQL session factory")
    return ThreadMetaRepository(session_factory)
