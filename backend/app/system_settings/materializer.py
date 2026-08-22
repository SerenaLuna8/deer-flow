"""Worker and auxiliary-call materializers for admitted System Models."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.system_settings.execution_adapter import (
    SystemModelExecutionAdapter,
    SystemModelMaterializationUnavailable,
)
from app.system_settings.models import (
    ConnectionTestSystemModelMaterial,
    FrozenSystemModelExecution,
)
from app.system_settings.repository import (
    SystemModelRepository,
    SystemModelRepositoryInvariant,
)
from app.system_settings.validation import (
    is_provider_adapter_eligible_for_new_binding,
)
from deerflow.config.model_config import ModelConfig


class SystemModelMaterializer:
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        execution_adapter: SystemModelExecutionAdapter | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._execution_adapter = execution_adapter or SystemModelExecutionAdapter()

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
                    load_secret=True,
                )
                if material is None or not (
                    is_provider_adapter_eligible_for_new_binding(
                        material.model.provider_adapter,
                    )
                ):
                    raise SystemModelMaterializationUnavailable
                return await asyncio.to_thread(
                    self._execution_adapter.materialize,
                    material,
                )
        except SystemModelMaterializationUnavailable:
            raise
        except (DBAPIError, RuntimeError, SystemModelRepositoryInvariant):
            raise SystemModelMaterializationUnavailable() from None

    async def materialize_connection_test(
        self,
        material: ConnectionTestSystemModelMaterial,
    ) -> ModelConfig:
        """Materialize one non-persistent, admin-authorized probe."""

        try:
            return await asyncio.to_thread(
                self._execution_adapter.materialize_connection_test,
                material,
            )
        except SystemModelMaterializationUnavailable:
            raise
        except RuntimeError:
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
                    self._execution_adapter.materialize,
                    material,
                )
        except SystemModelMaterializationUnavailable:
            raise
        except (DBAPIError, RuntimeError, SystemModelRepositoryInvariant):
            raise SystemModelMaterializationUnavailable() from None

    async def materialize_frozen(
        self,
        execution: FrozenSystemModelExecution,
    ) -> ModelConfig:
        try:
            async with self._session_factory() as session, session.begin():
                material = await SystemModelRepository(
                    session,
                ).lock_frozen_material(execution)
                if material is None:
                    raise SystemModelMaterializationUnavailable
                return await asyncio.to_thread(
                    self._execution_adapter.materialize,
                    material,
                )
        except SystemModelMaterializationUnavailable:
            raise
        except (DBAPIError, RuntimeError, SystemModelRepositoryInvariant):
            raise SystemModelMaterializationUnavailable() from None


__all__ = ["SystemModelMaterializer"]
