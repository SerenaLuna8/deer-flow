"""Transaction-bound persistence for stable System Model configurations."""

from __future__ import annotations

import uuid

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.system_settings.model_refs import DEFAULT_MODEL_REF, exact_model_ref
from app.system_settings.models import (
    FrozenSystemModelExecution,
    LockedSystemModelMaterial,
)
from app.system_settings.validation import (
    is_provider_adapter_eligible_for_new_binding,
    provider_api_key_required,
)
from deerflow.persistence.system_settings import (
    RunModelConfigSnapshotRow,
    SystemModelCatalogStateRow,
    SystemModelConfigRow,
    SystemModelSecretGenerationRow,
    SystemModelSecretTombstoneRow,
)


class SystemModelRepositoryInvariant(Exception):
    pass


class SystemModelRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def catalog_state(
        self,
        *,
        for_update: bool = False,
    ) -> SystemModelCatalogStateRow:
        statement: Select[tuple[SystemModelCatalogStateRow]] = select(
            SystemModelCatalogStateRow,
        ).where(SystemModelCatalogStateRow.id == 1)
        if for_update:
            statement = statement.with_for_update(of=SystemModelCatalogStateRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise SystemModelRepositoryInvariant
        return row

    async def list_models(
        self,
        *,
        active_only: bool = False,
    ) -> tuple[SystemModelConfigRow, ...]:
        statement: Select[tuple[SystemModelConfigRow]] = select(
            SystemModelConfigRow,
        )
        if active_only:
            statement = statement.where(SystemModelConfigRow.status == "active")
        statement = statement.order_by(
            SystemModelConfigRow.created_at.desc(),
            SystemModelConfigRow.id.desc(),
        )
        return tuple((await self.session.execute(statement)).scalars().all())

    async def lock_model(
        self,
        model_config_id: uuid.UUID,
    ) -> SystemModelConfigRow | None:
        if not isinstance(model_config_id, uuid.UUID):
            return None
        return (await self.session.execute(select(SystemModelConfigRow).where(SystemModelConfigRow.id == model_config_id).with_for_update(of=SystemModelConfigRow))).scalar_one_or_none()

    async def current_secret(
        self,
        model: SystemModelConfigRow,
        *,
        for_update: bool = False,
    ) -> SystemModelSecretGenerationRow | None:
        generation_id = model.current_secret_generation_id
        if generation_id is None:
            return None
        statement: Select[tuple[SystemModelSecretGenerationRow]] = select(
            SystemModelSecretGenerationRow,
        ).where(
            SystemModelSecretGenerationRow.id == generation_id,
            SystemModelSecretGenerationRow.model_config_id == model.id,
        )
        if for_update:
            statement = statement.with_for_update(
                of=SystemModelSecretGenerationRow,
            )
        generation = (await self.session.execute(statement)).scalar_one_or_none()
        if generation is None:
            raise SystemModelRepositoryInvariant
        return generation

    async def resolve_active_model(
        self,
        model_ref: str | None,
        *,
        load_secret: bool,
    ) -> LockedSystemModelMaterial | None:
        if model_ref in {None, DEFAULT_MODEL_REF}:
            state = await self.catalog_state()
            model_id = state.default_model_config_id
        else:
            exact = exact_model_ref(model_ref)
            if exact is None:
                return None
            model_id = uuid.UUID(exact)
        if model_id is None:
            return None
        model = (
            await self.session.execute(
                select(SystemModelConfigRow)
                .where(
                    SystemModelConfigRow.id == model_id,
                    SystemModelConfigRow.status == "active",
                )
                .with_for_update(of=SystemModelConfigRow)
            )
        ).scalar_one_or_none()
        if model is None:
            return None
        generation = await self.current_secret(model, for_update=True) if load_secret else None
        return LockedSystemModelMaterial(
            model=model,
            secret_generation=generation,
        )

    async def resolve_admissible_active_model(
        self,
        model_ref: str | None,
    ) -> LockedSystemModelMaterial | None:
        """Resolve current-work eligibility without returning secret material."""

        material = await self.resolve_active_model(
            model_ref,
            load_secret=False,
        )
        if material is None or not is_provider_adapter_eligible_for_new_binding(
            material.model.provider_adapter,
        ):
            return None
        if provider_api_key_required(material.model.provider_adapter) and (await self.current_secret(material.model, for_update=True)) is None:
            return None
        return material

    async def add_model(self, model: SystemModelConfigRow) -> None:
        self.session.add(model)
        await self.session.flush()

    async def add_secret_generation(
        self,
        generation: SystemModelSecretGenerationRow,
    ) -> None:
        self.session.add(generation)
        await self.session.flush()

    async def add_secret_tombstone(
        self,
        tombstone: SystemModelSecretTombstoneRow,
    ) -> None:
        self.session.add(tombstone)
        await self.session.flush()

    async def delete_secret_generation(
        self,
        generation: SystemModelSecretGenerationRow,
    ) -> None:
        await self.session.delete(generation)
        await self.session.flush()

    async def existing_snapshot(
        self,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        run_id: str,
        purpose: str,
    ) -> RunModelConfigSnapshotRow | None:
        return (
            await self.session.execute(
                select(RunModelConfigSnapshotRow).where(
                    RunModelConfigSnapshotRow.project_id == project_id,
                    RunModelConfigSnapshotRow.owner_user_id == owner_user_id,
                    RunModelConfigSnapshotRow.run_id == run_id,
                    RunModelConfigSnapshotRow.purpose == purpose,
                )
            )
        ).scalar_one_or_none()

    async def add_snapshot(self, snapshot: RunModelConfigSnapshotRow) -> None:
        self.session.add(snapshot)
        await self.session.flush()

    async def lock_snapshot_material(
        self,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        run_id: str,
        purpose: str,
    ) -> LockedSystemModelMaterial | None:
        snapshot = (
            await self.session.execute(
                select(RunModelConfigSnapshotRow)
                .where(
                    RunModelConfigSnapshotRow.project_id == project_id,
                    RunModelConfigSnapshotRow.owner_user_id == owner_user_id,
                    RunModelConfigSnapshotRow.run_id == run_id,
                    RunModelConfigSnapshotRow.purpose == purpose,
                )
                .with_for_update(of=RunModelConfigSnapshotRow)
            )
        ).scalar_one_or_none()
        if snapshot is None:
            return None
        model = (
            await self.session.execute(
                select(SystemModelConfigRow).where(
                    SystemModelConfigRow.id == snapshot.model_config_id,
                )
            )
        ).scalar_one_or_none()
        if model is None:
            raise SystemModelRepositoryInvariant
        execution = FrozenSystemModelExecution(
            model_config_id=uuid.UUID(str(snapshot.model_config_id)),
            provider_payload=dict(snapshot.provider_payload),
            payload_checksum=snapshot.payload_checksum,
            secret_generation_id=(uuid.UUID(str(snapshot.secret_generation_id)) if snapshot.secret_generation_id is not None else None),
            secret_envelope_digest=snapshot.secret_envelope_digest,
        )
        generation = await self._lock_execution_secret(execution)
        return LockedSystemModelMaterial(
            model=model,
            secret_generation=generation,
            execution=execution,
        )

    async def _lock_execution_secret(
        self,
        execution: FrozenSystemModelExecution,
    ) -> SystemModelSecretGenerationRow | None:
        if execution.secret_generation_id is None:
            return None
        return (
            await self.session.execute(
                select(SystemModelSecretGenerationRow)
                .where(
                    SystemModelSecretGenerationRow.id == execution.secret_generation_id,
                    SystemModelSecretGenerationRow.model_config_id == execution.model_config_id,
                    SystemModelSecretGenerationRow.envelope_digest == execution.secret_envelope_digest,
                )
                .with_for_update(of=SystemModelSecretGenerationRow)
            )
        ).scalar_one_or_none()

    async def lock_frozen_material(
        self,
        execution: FrozenSystemModelExecution,
    ) -> LockedSystemModelMaterial | None:
        if not isinstance(execution, FrozenSystemModelExecution):
            return None
        model = (
            await self.session.execute(
                select(SystemModelConfigRow).where(
                    SystemModelConfigRow.id == execution.model_config_id,
                )
            )
        ).scalar_one_or_none()
        if model is None:
            return None
        return LockedSystemModelMaterial(
            model=model,
            secret_generation=await self._lock_execution_secret(execution),
            execution=execution,
        )


__all__ = ["SystemModelRepository", "SystemModelRepositoryInvariant"]
