"""Thread metadata persistence — ORM, abstract store, and concrete implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from deerflow.persistence.thread_meta.base import (
    InvalidMetadataFilterError,
    ThreadMetaStore,
    TrustedUnscopedThreadMetaStore,
)
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
    "TrustedUnscopedThreadMetaStore",
    "make_thread_store",
]


def make_thread_store(
    session_factory: async_sessionmaker[AsyncSession],
) -> TrustedUnscopedThreadMetaStore:
    """Create the explicit trusted legacy adapter over PostgreSQL storage.

    Tests that need an in-memory double construct ``MemoryThreadMetaStore``
    directly instead of routing it through the production factory. Project
    business paths use ``PrivateThreadRepository`` and never this adapter.
    """
    if session_factory is None:
        raise TypeError("make_thread_store requires a PostgreSQL session factory")
    return TrustedUnscopedThreadMetaStore(ThreadMetaRepository(session_factory))
