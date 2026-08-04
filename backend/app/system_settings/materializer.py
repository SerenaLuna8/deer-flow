"""Worker and auxiliary-call materializers for exact system model versions."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.system_settings.credential_adapter import (
    SystemModelCredentialAdapter,
    SystemModelMaterializationUnavailable,
)
from app.system_settings.repository import (
    SystemModelRepository,
    SystemModelRepositoryInvariant,
)
from deerflow.config.model_config import ModelConfig


class SystemModelMaterializer:
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        credential_adapter: SystemModelCredentialAdapter | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._credential_adapter = credential_adapter or SystemModelCredentialAdapter()

    async def materialize_active(
        self,
        model_ref: str | None = None,
    ) -> ModelConfig:
        try:
            async with self._session_factory() as session, session.begin():
                material = await SystemModelRepository(
                    session,
                ).resolve_active_model(
                    model_ref,
                    load_envelope=True,
                )
                if material is None:
                    raise SystemModelMaterializationUnavailable
                return await asyncio.to_thread(
                    self._credential_adapter.materialize,
                    material,
                )
        except SystemModelMaterializationUnavailable:
            raise
        except (DBAPIError, RuntimeError, SystemModelRepositoryInvariant):
            raise SystemModelMaterializationUnavailable() from None

    async def materialize_exact(
        self,
        *,
        model_config_id: uuid.UUID,
        model_config_version_id: uuid.UUID,
        payload_checksum: str,
    ) -> ModelConfig:
        try:
            async with self._session_factory() as session, session.begin():
                material = await SystemModelRepository(
                    session,
                ).lock_exact_material(
                    model_config_id=model_config_id,
                    model_config_version_id=model_config_version_id,
                    payload_checksum=payload_checksum,
                    load_envelope=True,
                )
                if material is None:
                    raise SystemModelMaterializationUnavailable
                return await asyncio.to_thread(
                    self._credential_adapter.materialize,
                    material,
                )
        except SystemModelMaterializationUnavailable:
            raise
        except (DBAPIError, RuntimeError, SystemModelRepositoryInvariant):
            raise SystemModelMaterializationUnavailable() from None

    async def materialize_snapshot(
        self,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        run_id: str,
        purpose: str,
    ) -> ModelConfig:
        try:
            async with self._session_factory() as session, session.begin():
                material = await SystemModelRepository(
                    session,
                ).lock_snapshot_material(
                    project_id=project_id,
                    owner_user_id=owner_user_id,
                    run_id=run_id,
                    purpose=purpose,
                )
                if material is None:
                    raise SystemModelMaterializationUnavailable
                return await asyncio.to_thread(
                    self._credential_adapter.materialize,
                    material,
                )
        except SystemModelMaterializationUnavailable:
            raise
        except (DBAPIError, RuntimeError, SystemModelRepositoryInvariant):
            raise SystemModelMaterializationUnavailable() from None


__all__ = ["SystemModelMaterializer"]
