"""Durable project-scoped inbound delivery dedupe."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.channel_connections import ChannelInboundDeliveryRow
from deerflow.runtime.private_scope import PrivateResourceScope


@dataclass(frozen=True, slots=True)
class PrivateRunInboundDelivery:
    """Opaque provider delivery identity used only at server admission."""

    provider_delivery_id: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.provider_delivery_id, str) or not self.provider_delivery_id:
            raise TypeError("provider_delivery_id must be a non-empty string")

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            self.provider_delivery_id.encode("utf-8"),
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class PrivateRunInboundDeliveryRecord:
    run_id: str


class DuplicateInboundDelivery(Exception):
    """Internal control flow for an already-admitted provider delivery."""

    def __init__(self, run_id: str) -> None:
        if not isinstance(run_id, str) or not run_id:
            raise TypeError("run_id must be a non-empty string")
        self.run_id = run_id
        super().__init__("provider delivery was already admitted")


class ProjectInboundDeliveryRepository:
    """Session-bound repository; callers own lock order and transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _coordinates(
        scope: PrivateResourceScope,
    ) -> tuple[uuid.UUID, str]:
        if type(scope) is not PrivateResourceScope:
            raise TypeError("scope must be PrivateResourceScope")
        try:
            return uuid.UUID(scope.project_id), str(
                uuid.UUID(scope.owner_user_id),
            )
        except (TypeError, ValueError):
            raise TypeError("scope contains invalid private coordinates") from None

    async def get(
        self,
        *,
        scope: PrivateResourceScope,
        connection_id: str,
        provider: str,
        external_conversation_id: str,
        external_topic_id: str | None,
        delivery: PrivateRunInboundDelivery,
        lock: bool = False,
    ) -> PrivateRunInboundDeliveryRecord | None:
        project_id, owner_user_id = self._coordinates(scope)
        statement = select(ChannelInboundDeliveryRow.run_id).where(
            ChannelInboundDeliveryRow.project_id == project_id,
            ChannelInboundDeliveryRow.owner_user_id == owner_user_id,
            ChannelInboundDeliveryRow.connection_id == connection_id,
            ChannelInboundDeliveryRow.provider == provider,
            ChannelInboundDeliveryRow.external_conversation_id == external_conversation_id,
            ChannelInboundDeliveryRow.external_topic_id == (external_topic_id or ""),
            ChannelInboundDeliveryRow.provider_delivery_digest == delivery.digest,
        )
        if lock:
            statement = statement.with_for_update(
                of=ChannelInboundDeliveryRow,
            )
        run_id = (await self._session.execute(statement)).scalar_one_or_none()
        if run_id is None:
            return None
        return PrivateRunInboundDeliveryRecord(run_id=run_id)

    async def bind(
        self,
        *,
        scope: PrivateResourceScope,
        connection_id: str,
        provider: str,
        external_conversation_id: str,
        external_topic_id: str | None,
        thread_id: str,
        delivery: PrivateRunInboundDelivery,
        run_id: str,
    ) -> PrivateRunInboundDeliveryRecord:
        project_id, owner_user_id = self._coordinates(scope)
        row = ChannelInboundDeliveryRow(
            id=str(uuid.uuid4()),
            project_id=project_id,
            owner_user_id=owner_user_id,
            connection_id=connection_id,
            provider=provider,
            external_conversation_id=external_conversation_id,
            external_topic_id=external_topic_id or "",
            thread_id=thread_id,
            provider_delivery_digest=delivery.digest,
            run_id=run_id,
        )
        self._session.add(row)
        await self._session.flush()
        return PrivateRunInboundDeliveryRecord(run_id=run_id)


__all__ = [
    "DuplicateInboundDelivery",
    "PrivateRunInboundDelivery",
    "PrivateRunInboundDeliveryRecord",
    "ProjectInboundDeliveryRepository",
]
