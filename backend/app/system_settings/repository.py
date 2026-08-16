"""Caller-transaction PostgreSQL repository for system model settings."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.shared_assets.model_refs import DEFAULT_MODEL_REF, exact_model_ref
from app.system_settings.models import LockedSystemModelMaterial
from app.system_settings.validation import (
    is_provider_adapter_eligible_for_new_binding,
)
from deerflow.persistence.shared_assets import (
    CredentialEnvelopeRow,
    CredentialRow,
    CredentialVersionRow,
)
from deerflow.persistence.system_settings import (
    RunModelConfigSnapshotRow,
    SystemModelCatalogStateRow,
    SystemModelConfigRow,
    SystemModelConfigVersionRow,
)


class SystemModelRepositoryInvariant(Exception):
    pass


@dataclass(frozen=True, slots=True)
class SystemCredentialReference:
    credential: CredentialRow = field(repr=False)
    version: CredentialVersionRow = field(repr=False)
    envelope: CredentialEnvelopeRow | None = field(default=None, repr=False)


class SystemModelRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def catalog_state(
        self,
        *,
        for_update: bool = False,
    ) -> SystemModelCatalogStateRow:
        statement = select(SystemModelCatalogStateRow).where(
            SystemModelCatalogStateRow.id == 1,
        )
        if for_update:
            statement = statement.with_for_update(
                of=SystemModelCatalogStateRow,
            )
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise SystemModelRepositoryInvariant
        return row

    async def list_models(
        self,
        *,
        active_only: bool = False,
    ) -> tuple[
        tuple[SystemModelConfigRow, SystemModelConfigVersionRow],
        ...,
    ]:
        statement = (
            select(SystemModelConfigRow, SystemModelConfigVersionRow)
            .join(
                SystemModelConfigVersionRow,
                (SystemModelConfigVersionRow.id == SystemModelConfigRow.current_version_id) & (SystemModelConfigVersionRow.model_config_id == SystemModelConfigRow.id),
            )
            .order_by(
                SystemModelConfigRow.created_at.desc(),
                SystemModelConfigRow.id.desc(),
            )
        )
        if active_only:
            statement = statement.where(
                SystemModelConfigRow.status == "active",
            )
        return tuple((model, version) for model, version in (await self.session.execute(statement)).all())

    async def lock_model(
        self,
        model_config_id: uuid.UUID,
    ) -> SystemModelConfigRow | None:
        return (await self.session.execute(select(SystemModelConfigRow).where(SystemModelConfigRow.id == model_config_id).with_for_update(of=SystemModelConfigRow))).scalar_one_or_none()

    async def current_version(
        self,
        model: SystemModelConfigRow,
        *,
        for_update: bool = False,
    ) -> SystemModelConfigVersionRow:
        if model.current_version_id is None:
            raise SystemModelRepositoryInvariant
        statement = select(SystemModelConfigVersionRow).where(
            SystemModelConfigVersionRow.id == model.current_version_id,
            SystemModelConfigVersionRow.model_config_id == model.id,
        )
        if for_update:
            statement = statement.with_for_update(
                read=True,
                of=SystemModelConfigVersionRow,
            )
        version = (await self.session.execute(statement)).scalar_one_or_none()
        if version is None:
            raise SystemModelRepositoryInvariant
        return version

    async def resolve_active_model(
        self,
        model_ref: str | None,
        *,
        load_envelope: bool,
    ) -> LockedSystemModelMaterial | None:
        state = await self.catalog_state()
        if model_ref is None or model_ref == DEFAULT_MODEL_REF:
            model_config_id = state.default_model_config_id
            if model_config_id is None:
                return None
            predicate = SystemModelConfigRow.id == model_config_id
        else:
            exact_ref = exact_model_ref(model_ref)
            if exact_ref is None:
                return None
            model_config_id = uuid.UUID(exact_ref)
            predicate = SystemModelConfigRow.id == model_config_id
        model = (
            await self.session.execute(
                select(SystemModelConfigRow)
                .where(
                    predicate,
                    SystemModelConfigRow.status == "active",
                )
                .with_for_update(read=True, of=SystemModelConfigRow)
            )
        ).scalar_one_or_none()
        if model is None:
            return None
        version = await self.current_version(model, for_update=True)
        if not is_provider_adapter_eligible_for_new_binding(
            version.provider_adapter,
        ):
            return None
        credential = await self.lock_system_credential_reference(
            version.credential_id,
            version.credential_version_id,
            version.credential_env_key,
            require_current=False,
            load_envelope=load_envelope,
        )
        return LockedSystemModelMaterial(
            model=model,
            version=version,
            credential=(credential.credential if credential is not None else None),
            credential_version=(credential.version if credential is not None else None),
            envelope=(credential.envelope if credential is not None else None),
        )

    async def lock_exact_material(
        self,
        *,
        model_config_id: uuid.UUID,
        model_config_version_id: uuid.UUID,
        payload_checksum: str,
        load_envelope: bool,
    ) -> LockedSystemModelMaterial | None:
        if not isinstance(model_config_id, uuid.UUID) or not isinstance(model_config_version_id, uuid.UUID) or not isinstance(payload_checksum, str) or len(payload_checksum) != 64 or type(load_envelope) is not bool:
            raise SystemModelRepositoryInvariant
        model = (
            await self.session.execute(
                select(SystemModelConfigRow)
                .where(
                    SystemModelConfigRow.id == model_config_id,
                    SystemModelConfigRow.status == "active",
                )
                .with_for_update(read=True, of=SystemModelConfigRow)
            )
        ).scalar_one_or_none()
        version = (
            await self.session.execute(
                select(SystemModelConfigVersionRow)
                .where(
                    SystemModelConfigVersionRow.id == model_config_version_id,
                    SystemModelConfigVersionRow.model_config_id == model_config_id,
                    SystemModelConfigVersionRow.payload_checksum == payload_checksum,
                )
                .with_for_update(
                    read=True,
                    of=SystemModelConfigVersionRow,
                )
            )
        ).scalar_one_or_none()
        if model is None or version is None:
            return None
        credential = await self.lock_system_credential_reference(
            version.credential_id,
            version.credential_version_id,
            version.credential_env_key,
            require_current=False,
            load_envelope=load_envelope,
        )
        return LockedSystemModelMaterial(
            model=model,
            version=version,
            credential=(credential.credential if credential is not None else None),
            credential_version=(credential.version if credential is not None else None),
            envelope=(credential.envelope if credential is not None else None),
        )

    async def lock_system_credential_reference(
        self,
        credential_id: uuid.UUID | None,
        credential_version_id: uuid.UUID | None,
        credential_env_key: str | None,
        *,
        require_current: bool,
        load_envelope: bool,
    ) -> SystemCredentialReference | None:
        if credential_id is None and credential_version_id is None and credential_env_key is None:
            return None
        # asyncpg exposes UUID values through a uuid.UUID subclass. Commands
        # are validated strictly before persistence, while values reloaded
        # through SQLAlchemy must retain their accepted UUID semantics.
        if not isinstance(credential_id, uuid.UUID) or not isinstance(credential_version_id, uuid.UUID) or not isinstance(credential_env_key, str):
            raise SystemModelRepositoryInvariant
        credential = (
            await self.session.execute(
                select(CredentialRow)
                .where(
                    CredentialRow.id == credential_id,
                    CredentialRow.scope == "system",
                    CredentialRow.project_id.is_(None),
                    CredentialRow.credential_type == "model_api_key",
                    CredentialRow.status == "active",
                    CredentialRow.is_delete.is_(False),
                )
                .with_for_update(read=True, of=CredentialRow)
            )
        ).scalar_one_or_none()
        if (
            credential is None
            or credential.id != credential_id
            or credential.scope != "system"
            or credential.project_id is not None
            or credential.credential_type != "model_api_key"
            or credential.status != "active"
            or credential.is_delete
            or (require_current and credential.current_version_id != credential_version_id)
        ):
            raise SystemModelRepositoryInvariant
        allowed_statuses = ("active",) if require_current else ("active", "retired")
        version = (
            await self.session.execute(
                select(CredentialVersionRow)
                .where(
                    CredentialVersionRow.id == credential_version_id,
                    CredentialVersionRow.credential_id == credential_id,
                    CredentialVersionRow.status.in_(allowed_statuses),
                )
                .with_for_update(read=True, of=CredentialVersionRow)
            )
        ).scalar_one_or_none()
        env_schema = version.payload_schema.get("env") if version is not None and isinstance(version.payload_schema, dict) else None
        if (
            version is None
            or version.id != credential_version_id
            or version.credential_id != credential_id
            or version.status not in allowed_statuses
            or not isinstance(env_schema, list)
            or not all(isinstance(value, str) for value in env_schema)
            or credential_env_key not in env_schema
        ):
            raise SystemModelRepositoryInvariant
        envelope_statement = select(CredentialEnvelopeRow if load_envelope else CredentialEnvelopeRow.id).where(
            CredentialEnvelopeRow.credential_version_id == credential_version_id,
            CredentialEnvelopeRow.is_active.is_(True),
        )
        envelope_statement = envelope_statement.with_for_update(
            read=True,
            of=CredentialEnvelopeRow,
        )
        envelope_value = (await self.session.execute(envelope_statement)).scalar_one_or_none()
        if envelope_value is None or (load_envelope and (envelope_value.credential_version_id != credential_version_id or not envelope_value.is_active)):
            raise SystemModelRepositoryInvariant
        return SystemCredentialReference(
            credential=credential,
            version=version,
            envelope=envelope_value if load_envelope else None,
        )

    async def add_model(
        self,
        model: SystemModelConfigRow,
        version: SystemModelConfigVersionRow,
    ) -> None:
        self.session.add(model)
        await self.session.flush()
        self.session.add(version)
        await self.session.flush()
        model.current_version_id = version.id
        await self.session.flush()

    async def add_version(
        self,
        model: SystemModelConfigRow,
        version: SystemModelConfigVersionRow,
    ) -> None:
        self.session.add(version)
        await self.session.flush()
        model.current_version_id = version.id
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
                select(RunModelConfigSnapshotRow)
                .where(
                    RunModelConfigSnapshotRow.project_id == project_id,
                    RunModelConfigSnapshotRow.owner_user_id == owner_user_id,
                    RunModelConfigSnapshotRow.run_id == run_id,
                    RunModelConfigSnapshotRow.purpose == purpose,
                )
                .with_for_update(
                    read=True,
                    of=RunModelConfigSnapshotRow,
                )
            )
        ).scalar_one_or_none()

    async def add_snapshot(
        self,
        snapshot: RunModelConfigSnapshotRow,
    ) -> None:
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
        snapshot = await self.existing_snapshot(
            project_id=project_id,
            owner_user_id=owner_user_id,
            run_id=run_id,
            purpose=purpose,
        )
        if snapshot is None:
            return None
        model = (
            await self.session.execute(
                select(SystemModelConfigRow)
                .where(
                    SystemModelConfigRow.id == snapshot.model_config_id,
                    SystemModelConfigRow.status == "active",
                )
                .with_for_update(read=True, of=SystemModelConfigRow)
            )
        ).scalar_one_or_none()
        version = (
            await self.session.execute(
                select(SystemModelConfigVersionRow)
                .where(
                    SystemModelConfigVersionRow.id == snapshot.model_config_version_id,
                    SystemModelConfigVersionRow.model_config_id == snapshot.model_config_id,
                    SystemModelConfigVersionRow.payload_checksum == snapshot.payload_checksum,
                )
                .with_for_update(
                    read=True,
                    of=SystemModelConfigVersionRow,
                )
            )
        ).scalar_one_or_none()
        if model is None or version is None or version.credential_id != snapshot.credential_id or version.credential_version_id != snapshot.credential_version_id or version.credential_env_key != snapshot.credential_env_key:
            raise SystemModelRepositoryInvariant
        credential = await self.lock_system_credential_reference(
            snapshot.credential_id,
            snapshot.credential_version_id,
            snapshot.credential_env_key,
            require_current=False,
            load_envelope=True,
        )
        return LockedSystemModelMaterial(
            model=model,
            version=version,
            credential=(credential.credential if credential is not None else None),
            credential_version=(credential.version if credential is not None else None),
            envelope=(credential.envelope if credential is not None else None),
            snapshot=snapshot,
        )


__all__ = [
    "SystemCredentialReference",
    "SystemModelRepository",
    "SystemModelRepositoryInvariant",
]
