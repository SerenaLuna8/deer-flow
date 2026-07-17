from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession


class _StructuredLogger(Protocol):
    def info(self, message: str, *args: object, **kwargs: object) -> None: ...


class SharedAssetGovernanceEventSink:
    def __init__(self, logger: _StructuredLogger | None = None) -> None:
        self._logger = logger or logging.getLogger(__name__)

    def write_override(
        self,
        *,
        actor: uuid.UUID,
        project_id: uuid.UUID | None,
        asset_id: uuid.UUID,
        version_id: uuid.UUID | None,
        action: str,
        request_id: str,
    ) -> None:
        event: Mapping[str, object] = {
            "actor_user_id": str(actor),
            "project_id": str(project_id) if project_id is not None else None,
            "asset_id": str(asset_id),
            "version_id": str(version_id) if version_id is not None else None,
            "action": action,
            "request_id": request_id,
        }
        self._logger.info("shared_asset_governance_override", extra={"governance_event": event})

    async def append_override(
        self,
        session: AsyncSession,
        *,
        actor: uuid.UUID,
        project_id: uuid.UUID | None,
        asset_id: uuid.UUID,
        version_id: uuid.UUID | None,
        action: str,
        request_id: str,
        asset_kind: str | None = None,
    ) -> None:
        del session, asset_kind
        self.write_override(
            actor=actor,
            project_id=project_id,
            asset_id=asset_id,
            version_id=version_id,
            action=action,
            request_id=request_id,
        )

    async def append_project(
        self,
        session: AsyncSession,
        *,
        actor: uuid.UUID,
        project_id: uuid.UUID,
        asset_id: uuid.UUID,
        version_id: uuid.UUID | None,
        action: str,
        request_id: str,
        asset_kind: str | None = None,
    ) -> None:
        """Compatibility hook; the formal M6 adapter persists project events."""

        del session, actor, project_id, asset_id, version_id, action, request_id, asset_kind
