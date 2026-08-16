"""Transactional system model catalog application service."""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import (
    SystemAuditContext,
    is_issued_system_audit_context,
)
from app.shared_assets.model_refs import DEFAULT_MODEL_REF, exact_model_ref
from app.system_settings.errors import (
    SystemModelAdministrationRequired,
    SystemModelConflict,
    SystemModelError,
    SystemModelInvalid,
    SystemModelNotFound,
    SystemModelStorageUnavailable,
)
from app.system_settings.models import (
    ConnectionTestSystemModelMaterial,
    CreateSystemModel,
    PublicSystemModelView,
    RunModelConfigSnapshotView,
    SystemModelCatalogStateView,
    SystemModelCatalogView,
    SystemModelConnectionCheck,
    SystemModelVersionView,
    SystemModelView,
    UpdateSystemModel,
)
from app.system_settings.repository import (
    SystemModelRepository,
    SystemModelRepositoryInvariant,
)
from app.system_settings.validation import (
    ModelSettingsInvalid,
    canonical_model_payload_checksum,
    is_provider_adapter_supported,
    validate_create_system_model,
    validate_system_model_connection_test,
    validate_update_system_model,
)
from deerflow.persistence.system_settings import (
    RunModelConfigSnapshotRow,
    SystemModelConfigRow,
    SystemModelConfigVersionRow,
)
from deerflow.persistence.user.model import UserRow
from deerflow.vision.compatibility import (
    VISION_BRIDGE_CONTRACT_V1,
    resolve_vision_bridge_protocol,
)

_PURPOSE = re.compile(r"[a-z][a-z0-9._-]{0,63}\Z")
_CONFLICT_CONSTRAINTS = frozenset(
    {
        "pk_run_model_config_snapshots",
        "uq_system_model_config_versions_number",
    }
)
_T = TypeVar("_T")


def _is_admissible_model_ref(value: object) -> bool:
    return type(value) is str and (value == DEFAULT_MODEL_REF or exact_model_ref(value) is not None)


def _constraint_name(exc: BaseException) -> str | None:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        value = getattr(current, "constraint_name", None)
        if isinstance(value, str):
            return value
        current = getattr(current, "orig", None) or getattr(current, "__cause__", None)
    return None


def _freeze_json(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()},
        )
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _version_view(
    row: SystemModelConfigVersionRow,
) -> SystemModelVersionView:
    frozen_settings = _freeze_json(dict(row.settings))
    if not isinstance(frozen_settings, Mapping):
        raise SystemModelRepositoryInvariant
    return SystemModelVersionView(
        id=uuid.UUID(str(row.id)),
        model_config_id=uuid.UUID(str(row.model_config_id)),
        version_number=int(row.version_number),
        provider_adapter=row.provider_adapter,
        provider_model=row.provider_model,
        settings=frozen_settings,
        supports_thinking=row.supports_thinking,
        supports_reasoning_effort=row.supports_reasoning_effort,
        supports_vision=row.supports_vision,
        credential_id=(uuid.UUID(str(row.credential_id)) if row.credential_id is not None else None),
        credential_version_id=(uuid.UUID(str(row.credential_version_id)) if row.credential_version_id is not None else None),
        credential_env_key=row.credential_env_key,
        payload_checksum=row.payload_checksum,
        supersedes_version_id=(uuid.UUID(str(row.supersedes_version_id)) if row.supersedes_version_id is not None else None),
        created_by_user_id=row.created_by_user_id,
        created_at=row.created_at,
    )


def _model_view(
    row: SystemModelConfigRow,
    version: SystemModelConfigVersionRow,
) -> SystemModelView:
    if row.current_version_id is None or row.current_version_id != version.id:
        raise SystemModelRepositoryInvariant
    return SystemModelView(
        id=uuid.UUID(str(row.id)),
        display_name=row.display_name,
        status=row.status,
        current_version_id=uuid.UUID(str(row.current_version_id)),
        revision=int(row.revision),
        current_version=_version_view(version),
        created_by_user_id=row.created_by_user_id,
        updated_by_user_id=row.updated_by_user_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _snapshot_view(
    row: RunModelConfigSnapshotRow,
    version: SystemModelConfigVersionRow,
) -> RunModelConfigSnapshotView:
    return RunModelConfigSnapshotView(
        project_id=uuid.UUID(str(row.project_id)),
        owner_user_id=row.owner_user_id,
        thread_id=row.thread_id,
        run_id=row.run_id,
        purpose=row.purpose,
        model_ref=str(uuid.UUID(str(row.model_config_id))),
        provider_adapter=version.provider_adapter,
        provider_settings=dict(version.settings),
        model_config_id=uuid.UUID(str(row.model_config_id)),
        model_config_version_id=uuid.UUID(
            str(row.model_config_version_id),
        ),
        payload_checksum=row.payload_checksum,
        credential_id=(uuid.UUID(str(row.credential_id)) if row.credential_id is not None else None),
        credential_version_id=(uuid.UUID(str(row.credential_version_id)) if row.credential_version_id is not None else None),
        credential_env_key=row.credential_env_key,
        supports_thinking=version.supports_thinking,
        supports_reasoning_effort=version.supports_reasoning_effort,
        supports_vision=version.supports_vision,
        created_at=row.created_at,
    )


class SystemModelCatalogService:
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _require_admin(context: object) -> SystemAuditContext:
        if not is_issued_system_audit_context(context):
            raise SystemModelAdministrationRequired
        return context

    async def _admin_operation(
        self,
        context: object,
        operation: Callable[
            [SystemModelRepository, SystemAuditContext],
            Awaitable[_T],
        ],
    ) -> _T:
        issued = self._require_admin(context)
        try:
            async with self._session_factory() as session, session.begin():
                current_role = (
                    await session.execute(
                        select(UserRow.system_role)
                        .where(
                            UserRow.id == str(issued.user_id),
                            UserRow.system_role == "system_admin",
                        )
                        .with_for_update(of=UserRow)
                    )
                ).scalar_one_or_none()
                if current_role != "system_admin":
                    # Hide the administrative surface if the role was revoked
                    # after request authentication but before this write
                    # transaction acquired authority.
                    raise SystemModelNotFound(issued.request_id)
                return await operation(
                    SystemModelRepository(session),
                    issued,
                )
        except SystemModelError:
            raise
        except IntegrityError as exc:
            if _constraint_name(exc) in _CONFLICT_CONSTRAINTS:
                raise SystemModelConflict(issued.request_id) from None
            raise SystemModelInvalid(issued.request_id) from None
        except SystemModelRepositoryInvariant:
            raise SystemModelInvalid(issued.request_id) from None
        except (DBAPIError, RuntimeError):
            raise SystemModelStorageUnavailable(issued.request_id) from None

    async def list_models(
        self,
        context: SystemAuditContext,
    ) -> SystemModelCatalogView:
        async def operation(
            repository: SystemModelRepository,
            _issued: SystemAuditContext,
        ) -> SystemModelCatalogView:
            state = await repository.catalog_state()
            items = tuple(_model_view(model, version) for model, version in await repository.list_models())
            return SystemModelCatalogView(
                catalog_revision=int(state.revision),
                default_model_config_id=(uuid.UUID(str(state.default_model_config_id)) if state.default_model_config_id is not None else None),
                items=items,
            )

        return await self._admin_operation(context, operation)

    async def get_model(
        self,
        context: SystemAuditContext,
        model_config_id: uuid.UUID,
    ) -> SystemModelView:
        async def operation(
            repository: SystemModelRepository,
            issued: SystemAuditContext,
        ) -> SystemModelView:
            if type(model_config_id) is not uuid.UUID:
                raise SystemModelNotFound(issued.request_id)
            model = await repository.lock_model(model_config_id)
            if model is None:
                raise SystemModelNotFound(issued.request_id)
            return _model_view(
                model,
                await repository.current_version(model),
            )

        return await self._admin_operation(context, operation)

    async def list_available_models(
        self,
    ) -> tuple[PublicSystemModelView, ...]:
        try:
            async with self._session_factory() as session, session.begin():
                repository = SystemModelRepository(session)
                state = await repository.catalog_state()
                default_id = state.default_model_config_id
                rows = tuple(
                    (model, version)
                    for model, version in await repository.list_models(
                        active_only=True,
                    )
                    if is_provider_adapter_supported(version.provider_adapter)
                )
                rows = tuple(
                    sorted(
                        rows,
                        key=lambda item: item[0].id != default_id,
                    ),
                )
                return tuple(
                    PublicSystemModelView(
                        model_ref=str(uuid.UUID(str(model.id))),
                        display_name=model.display_name,
                        supports_thinking=version.supports_thinking,
                        supports_reasoning_effort=(version.supports_reasoning_effort),
                        supports_vision=version.supports_vision,
                        supports_vision_bridge=(
                            version.supports_vision
                            and resolve_vision_bridge_protocol(
                                version.provider_adapter,
                                version.settings,
                                VISION_BRIDGE_CONTRACT_V1,
                            )
                            is not None
                        ),
                        is_default=model.id == default_id,
                    )
                    for model, version in rows
                )
        except (DBAPIError, RuntimeError, SystemModelRepositoryInvariant):
            raise SystemModelStorageUnavailable("public-model-catalog") from None

    async def create_model(
        self,
        context: SystemAuditContext,
        command: CreateSystemModel,
    ) -> SystemModelView:
        issued = self._require_admin(context)
        try:
            command = validate_create_system_model(command)
        except ModelSettingsInvalid:
            raise SystemModelInvalid(issued.request_id) from None
        model_id = uuid.uuid4()
        version_id = uuid.uuid4()

        async def operation(
            repository: SystemModelRepository,
            actor: SystemAuditContext,
        ) -> SystemModelView:
            state = await repository.catalog_state(for_update=True)
            await repository.lock_system_credential_reference(
                command.credential_id,
                command.credential_version_id,
                command.credential_env_key,
                require_current=True,
                load_envelope=False,
            )
            model = SystemModelConfigRow(
                id=model_id,
                display_name=command.display_name,
                status=command.status,
                revision=1,
                created_by_user_id=str(actor.user_id),
                updated_by_user_id=str(actor.user_id),
            )
            version = SystemModelConfigVersionRow(
                id=version_id,
                model_config_id=model_id,
                version_number=1,
                provider_adapter=command.provider_adapter,
                provider_model=command.provider_model,
                settings=dict(command.settings),
                supports_thinking=command.supports_thinking,
                supports_reasoning_effort=(command.supports_reasoning_effort),
                supports_vision=command.supports_vision,
                credential_id=command.credential_id,
                credential_version_id=command.credential_version_id,
                credential_env_key=command.credential_env_key,
                payload_checksum=canonical_model_payload_checksum(
                    model_id,
                    command,
                ),
                created_by_user_id=str(actor.user_id),
            )
            await repository.add_model(model, version)
            if state.default_model_config_id is None and model.status == "active":
                state.default_model_config_id = model.id
            state.revision += 1
            state.updated_by_user_id = str(actor.user_id)
            await repository.session.flush()
            return _model_view(model, version)

        return await self._admin_operation(issued, operation)

    async def prepare_connection_test(
        self,
        context: SystemAuditContext,
        command: SystemModelConnectionCheck,
    ) -> ConnectionTestSystemModelMaterial:
        """Re-authorize and decryptably lock a Credential without persisting a model."""

        issued = self._require_admin(context)
        try:
            command = validate_system_model_connection_test(command)
        except ModelSettingsInvalid:
            raise SystemModelInvalid(issued.request_id) from None

        async def operation(
            repository: SystemModelRepository,
            _actor: SystemAuditContext,
        ) -> ConnectionTestSystemModelMaterial:
            credential = await repository.lock_system_credential_reference(
                command.credential_id,
                command.credential_version_id,
                command.credential_env_key,
                require_current=True,
                load_envelope=True,
            )
            return ConnectionTestSystemModelMaterial(
                command=command,
                credential=(credential.credential if credential is not None else None),
                credential_version=(credential.version if credential is not None else None),
                envelope=(credential.envelope if credential is not None else None),
            )

        return await self._admin_operation(issued, operation)

    async def update_model(
        self,
        context: SystemAuditContext,
        model_config_id: uuid.UUID,
        command: UpdateSystemModel,
        *,
        expected_revision: int,
    ) -> SystemModelView:
        issued = self._require_admin(context)
        try:
            command = validate_update_system_model(command)
            if type(model_config_id) is not uuid.UUID or type(expected_revision) is not int or expected_revision < 1:
                raise ModelSettingsInvalid
        except ModelSettingsInvalid:
            raise SystemModelInvalid(issued.request_id) from None
        version_id = uuid.uuid4()

        async def operation(
            repository: SystemModelRepository,
            actor: SystemAuditContext,
        ) -> SystemModelView:
            state = await repository.catalog_state(for_update=True)
            model = await repository.lock_model(model_config_id)
            if model is None:
                raise SystemModelNotFound(actor.request_id)
            if model.revision != expected_revision:
                raise SystemModelConflict(actor.request_id)
            previous = await repository.current_version(
                model,
                for_update=True,
            )
            await repository.lock_system_credential_reference(
                command.credential_id,
                command.credential_version_id,
                command.credential_env_key,
                require_current=True,
                load_envelope=False,
            )
            version = SystemModelConfigVersionRow(
                id=version_id,
                model_config_id=model.id,
                version_number=previous.version_number + 1,
                provider_adapter=command.provider_adapter,
                provider_model=command.provider_model,
                settings=dict(command.settings),
                supports_thinking=command.supports_thinking,
                supports_reasoning_effort=(command.supports_reasoning_effort),
                supports_vision=command.supports_vision,
                credential_id=command.credential_id,
                credential_version_id=command.credential_version_id,
                credential_env_key=command.credential_env_key,
                payload_checksum=canonical_model_payload_checksum(
                    uuid.UUID(str(model.id)),
                    command,
                ),
                supersedes_version_id=previous.id,
                created_by_user_id=str(actor.user_id),
            )
            await repository.add_version(model, version)
            model.display_name = command.display_name
            model.revision += 1
            model.updated_by_user_id = str(actor.user_id)
            state.revision += 1
            state.updated_by_user_id = str(actor.user_id)
            await repository.session.flush()
            return _model_view(model, version)

        return await self._admin_operation(issued, operation)

    async def set_status(
        self,
        context: SystemAuditContext,
        model_config_id: uuid.UUID,
        status: str,
        *,
        expected_revision: int,
    ) -> SystemModelView:
        issued = self._require_admin(context)
        if type(model_config_id) is not uuid.UUID or status not in {"active", "suspended"} or type(expected_revision) is not int or expected_revision < 1:
            raise SystemModelInvalid(issued.request_id)

        async def operation(
            repository: SystemModelRepository,
            actor: SystemAuditContext,
        ) -> SystemModelView:
            state = await repository.catalog_state(for_update=True)
            model = await repository.lock_model(model_config_id)
            if model is None:
                raise SystemModelNotFound(actor.request_id)
            if model.revision != expected_revision or model.status == status:
                raise SystemModelConflict(actor.request_id)
            version = await repository.current_version(model)
            if status == "active" and not is_provider_adapter_supported(
                version.provider_adapter,
            ):
                raise SystemModelInvalid(actor.request_id)
            model.status = status
            model.revision += 1
            model.updated_by_user_id = str(actor.user_id)
            if status == "suspended" and state.default_model_config_id == model.id:
                state.default_model_config_id = None
            elif status == "active" and state.default_model_config_id is None:
                state.default_model_config_id = model.id
            state.revision += 1
            state.updated_by_user_id = str(actor.user_id)
            await repository.session.flush()
            return _model_view(model, version)

        return await self._admin_operation(issued, operation)

    async def set_default(
        self,
        context: SystemAuditContext,
        model_config_id: uuid.UUID,
        *,
        expected_catalog_revision: int,
    ) -> SystemModelCatalogStateView:
        issued = self._require_admin(context)
        if type(model_config_id) is not uuid.UUID or type(expected_catalog_revision) is not int or expected_catalog_revision < 1:
            raise SystemModelInvalid(issued.request_id)

        async def operation(
            repository: SystemModelRepository,
            actor: SystemAuditContext,
        ) -> SystemModelCatalogStateView:
            state = await repository.catalog_state(for_update=True)
            if state.revision != expected_catalog_revision:
                raise SystemModelConflict(actor.request_id)
            model = await repository.lock_model(model_config_id)
            if model is None or model.status != "active":
                raise SystemModelNotFound(actor.request_id)
            version = await repository.current_version(model)
            if not is_provider_adapter_supported(version.provider_adapter):
                raise SystemModelInvalid(actor.request_id)
            if state.default_model_config_id != model.id:
                state.default_model_config_id = model.id
                state.revision += 1
                state.updated_by_user_id = str(actor.user_id)
                await repository.session.flush()
            return SystemModelCatalogStateView(
                revision=int(state.revision),
                default_model_config_id=uuid.UUID(
                    str(state.default_model_config_id),
                ),
                updated_at=state.updated_at,
            )

        return await self._admin_operation(issued, operation)

    async def admit_model_snapshot(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        thread_id: str,
        run_id: str,
        purpose: str,
        model_ref: str,
        request_id: str = "unknown",
    ) -> RunModelConfigSnapshotView:
        if (
            not isinstance(session, AsyncSession)
            or not session.in_transaction()
            or not isinstance(project_id, uuid.UUID)
            or type(owner_user_id) is not str
            or not owner_user_id
            or type(thread_id) is not str
            or not thread_id
            or type(run_id) is not str
            or not run_id
            or type(purpose) is not str
            or _PURPOSE.fullmatch(purpose) is None
            or not _is_admissible_model_ref(model_ref)
        ):
            raise SystemModelInvalid(request_id)
        canonical_project_id = uuid.UUID(str(project_id))
        repository = SystemModelRepository(session)
        try:
            material = await repository.resolve_active_model(
                model_ref,
                load_envelope=False,
            )
            if material is None or not is_provider_adapter_supported(
                material.version.provider_adapter,
            ):
                raise SystemModelNotFound(request_id)
            existing = await repository.existing_snapshot(
                project_id=project_id,
                owner_user_id=owner_user_id,
                run_id=run_id,
                purpose=purpose,
            )
            version = material.version
            if existing is not None:
                if (
                    existing.thread_id != thread_id
                    or existing.model_config_id != material.model.id
                    or existing.model_config_version_id != version.id
                    or existing.payload_checksum != version.payload_checksum
                    or existing.credential_id != version.credential_id
                    or existing.credential_version_id != version.credential_version_id
                    or existing.credential_env_key != version.credential_env_key
                ):
                    raise SystemModelConflict(request_id)
                return _snapshot_view(existing, version)
            snapshot = RunModelConfigSnapshotRow(
                project_id=canonical_project_id,
                owner_user_id=owner_user_id,
                thread_id=thread_id,
                run_id=run_id,
                purpose=purpose,
                model_config_id=material.model.id,
                model_config_version_id=version.id,
                payload_checksum=version.payload_checksum,
                credential_id=version.credential_id,
                credential_version_id=version.credential_version_id,
                credential_env_key=version.credential_env_key,
            )
            await repository.add_snapshot(snapshot)
            return _snapshot_view(snapshot, version)
        except SystemModelError:
            raise
        except IntegrityError:
            raise SystemModelConflict(request_id) from None
        except SystemModelRepositoryInvariant:
            raise SystemModelInvalid(request_id) from None
        except (DBAPIError, RuntimeError):
            raise SystemModelStorageUnavailable(request_id) from None


__all__ = ["SystemModelCatalogService"]
