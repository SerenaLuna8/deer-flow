"""PostgreSQL-backed IM conversation-to-thread mappings."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from deerflow.runtime.private_scope import PrivateResourceScope


class ChannelConversationRepository(Protocol):
    """Persistence contract implemented by ``ChannelConnectionRepository``."""

    async def get_thread_id(
        self,
        *,
        scope: PrivateResourceScope,
        connection_id: str,
        provider: str | None,
        external_conversation_id: str,
        external_topic_id: str | None,
    ) -> str | None: ...

    async def set_thread_id(
        self,
        *,
        scope: PrivateResourceScope,
        connection_id: str,
        provider: str,
        external_conversation_id: str,
        external_topic_id: str | None,
        thread_id: str,
    ) -> bool: ...

    async def remove_thread_ids(
        self,
        *,
        scope: PrivateResourceScope,
        connection_id: str,
        provider: str,
        external_conversation_id: str,
        external_topic_id: str | None,
    ) -> bool: ...

    async def list_thread_ids(
        self,
        *,
        scope: PrivateResourceScope,
        connection_id: str,
        provider: str | None,
    ) -> list[Mapping[str, Any]]: ...


class ChannelStore:
    """Scoped facade over the authoritative PostgreSQL channel conversations.

    Reads used while preparing an inbound provider event require the exact
    server-resolved ``connection_id`` plus the immutable Project/Owner scope.
    Execution still re-resolves current connection and membership authority.
    """

    def __init__(self, repository: ChannelConversationRepository) -> None:
        if repository is None:
            raise RuntimeError("channel conversation persistence is unavailable")
        self._repository = repository

    @property
    def repository(self) -> ChannelConversationRepository:
        return self._repository

    async def get_thread_id(
        self,
        channel_name: str,
        chat_id: str,
        topic_id: str | None = None,
        *,
        connection_id: str,
        scope: PrivateResourceScope,
    ) -> str | None:
        """Return the private Thread mapped to one connected IM conversation."""
        self._require_coordinate(channel_name, "channel_name")
        self._require_coordinate(chat_id, "chat_id")
        self._require_coordinate(connection_id, "connection_id")
        self._require_scope(scope)
        return await self._repository.get_thread_id(
            scope=scope,
            connection_id=connection_id,
            provider=channel_name,
            external_conversation_id=chat_id,
            external_topic_id=topic_id,
        )

    async def set_thread_id(
        self,
        channel_name: str,
        chat_id: str,
        thread_id: str,
        *,
        topic_id: str | None = None,
        connection_id: str,
        scope: PrivateResourceScope,
    ) -> bool:
        """Create or update a mapping inside an exact Project/Owner scope."""
        self._require_coordinate(channel_name, "channel_name")
        self._require_coordinate(chat_id, "chat_id")
        self._require_coordinate(thread_id, "thread_id")
        self._require_coordinate(connection_id, "connection_id")
        self._require_scope(scope)
        return await self._repository.set_thread_id(
            scope=scope,
            connection_id=connection_id,
            provider=channel_name,
            external_conversation_id=chat_id,
            external_topic_id=topic_id,
            thread_id=thread_id,
        )

    async def remove(
        self,
        channel_name: str,
        chat_id: str,
        topic_id: str | None = None,
        *,
        connection_id: str,
        scope: PrivateResourceScope,
    ) -> bool:
        """Remove an exact topic, or all topics for one scoped conversation."""
        self._require_coordinate(channel_name, "channel_name")
        self._require_coordinate(chat_id, "chat_id")
        self._require_coordinate(connection_id, "connection_id")
        self._require_scope(scope)
        return await self._repository.remove_thread_ids(
            scope=scope,
            connection_id=connection_id,
            provider=channel_name,
            external_conversation_id=chat_id,
            external_topic_id=topic_id,
        )

    async def list_entries(
        self,
        channel_name: str | None = None,
        *,
        connection_id: str,
        scope: PrivateResourceScope,
    ) -> list[dict[str, Any]]:
        """List mappings for one private connection, optionally by provider."""
        if channel_name is not None:
            self._require_coordinate(channel_name, "channel_name")
        self._require_coordinate(connection_id, "connection_id")
        self._require_scope(scope)
        rows = await self._repository.list_thread_ids(
            scope=scope,
            connection_id=connection_id,
            provider=channel_name,
        )
        return [dict(row) for row in rows]

    @staticmethod
    def _require_coordinate(value: str, name: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} is required")

    @staticmethod
    def _require_scope(scope: PrivateResourceScope) -> None:
        if type(scope) is not PrivateResourceScope:
            raise RuntimeError("channel mapping operation requires private scope")
