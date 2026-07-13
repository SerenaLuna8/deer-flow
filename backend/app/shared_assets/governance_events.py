from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from typing import Protocol


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
