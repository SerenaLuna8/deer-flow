"""Transactional System Model configuration and API Key ownership."""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.audit.models import (
    AuditAction,
    AuditActor,
    AuditOutcome,
    AuditTarget,
    AuditTargetKind,
    SystemAuditContext,
    is_issued_system_audit_context,
)
from app.audit.service import AuditService
from app.system_settings.errors import (
    SystemModelAdministrationRequired,
    SystemModelConflict,
    SystemModelError,
    SystemModelInvalid,
    SystemModelNotFound,
    SystemModelStorageUnavailable,
)
from app.system_settings.execution_payload import system_model_provider_payload
from app.system_settings.model_refs import DEFAULT_MODEL_REF, exact_model_ref
from app.system_settings.models import (
    ConnectionTestSystemModelMaterial,
    CreateSystemModel,
    PublicSystemModelView,
    RunModelConfigSnapshotView,
    SystemModelCatalogStateView,
    SystemModelCatalogView,
    SystemModelConnectionCheck,
    SystemModelView,
    UpdateSystemModel,
)
from app.system_settings.repository import (
    SystemModelRepository,
    SystemModelRepositoryInvariant,
)
from app.system_settings.secrets import (
    model_secret_envelope_digest,
    model_secret_recipient,
)
from app.system_settings.validation import (
    ModelSettingsInvalid,
    canonical_model_payload_checksum,
    is_provider_adapter_eligible_for_new_binding,
    provider_api_key_required,
    validate_create_system_model,
    validate_system_model_connection_test,
    validate_update_system_model,
)
from deerflow.persistence.system_settings import (
    RunModelConfigSnapshotRow,
    SystemModelConfigRow,
    SystemModelSecretGenerationRow,
    SystemModelSecretTombstoneRow,
)
from deerflow.persistence.user.model import UserRow
from deerflow.secrets import (
    SecretEnvelope,
    SecretKey,
    SecretKeyInvalid,
    SecretProtectionFailed,
)

_PURPOSE = re.compile(r"[a-z][a-z0-9._-]{0,63}\Z")
_CONFLICT_CONSTRAINTS = frozenset(
    {
        "pk_run_model_config_snapshots",
        "uq_system_model_secret_generations_revision",
        "uq_system_model_secret_tombstones_revision",
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
        current = getattr(current, "orig", None) or getattr(
            current,
            "__cause__",
            None,
        )
    return None


def _freeze_json(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()},
        )
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _model_view(row: SystemModelConfigRow) -> SystemModelView:
    frozen_settings = _freeze_json(dict(row.settings))
    if not isinstance(frozen_settings, Mapping):
        raise SystemModelRepositoryInvariant
    configured = row.current_secret_generation_id is not None
    eligible = is_provider_adapter_eligible_for_new_binding(row.provider_adapter)
    ready = eligible and (not provider_api_key_required(row.provider_adapter) or configured)
    return SystemModelView(
        id=uuid.UUID(str(row.id)),
        display_name=row.display_name,
        status=row.status,
        provider_adapter=row.provider_adapter,
        provider_model=row.provider_model,
        settings=frozen_settings,
        supports_thinking=row.supports_thinking,
        supports_reasoning_effort=row.supports_reasoning_effort,
        supports_vision=row.supports_vision,
        payload_checksum=row.payload_checksum,
        api_key_configured=configured,
        secret_readiness="ready" if ready else "unready",
        secret_revision=int(row.secret_revision),
        revision=int(row.revision),
        created_by_user_id=row.created_by_user_id,
        updated_by_user_id=row.updated_by_user_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _snapshot_payload(
    row: RunModelConfigSnapshotRow,
) -> tuple[str, str, dict[str, object], bool, bool, bool]:
    payload = row.provider_payload
    try:
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError
        adapter = payload["provider_adapter"]
        provider_model = payload["provider_model"]
        settings = payload["settings"]
        thinking = payload["supports_thinking"]
        reasoning_effort = payload["supports_reasoning_effort"]
        vision = payload["supports_vision"]
        if type(adapter) is not str or type(provider_model) is not str or not isinstance(settings, dict) or type(thinking) is not bool or type(reasoning_effort) is not bool or type(vision) is not bool:
            raise ValueError
        return (
            adapter,
            provider_model,
            dict(settings),
            thinking,
            reasoning_effort,
            vision,
        )
    except (KeyError, TypeError, ValueError):
        raise SystemModelRepositoryInvariant from None


def _snapshot_view(row: RunModelConfigSnapshotRow) -> RunModelConfigSnapshotView:
    adapter, provider_model, settings, thinking, reasoning_effort, vision = _snapshot_payload(row)
    return RunModelConfigSnapshotView(
        project_id=uuid.UUID(str(row.project_id)),
        owner_user_id=row.owner_user_id,
        thread_id=row.thread_id,
        run_id=row.run_id,
        purpose=row.purpose,
        model_ref=str(uuid.UUID(str(row.model_config_id))),
        provider_adapter=adapter,
        provider_model=provider_model,
        provider_settings=settings,
        model_config_id=uuid.UUID(str(row.model_config_id)),
        payload_checksum=row.payload_checksum,
        secret_generation_id=(uuid.UUID(str(row.secret_generation_id)) if row.secret_generation_id is not None else None),
        secret_envelope_digest=row.secret_envelope_digest,
        supports_thinking=thinking,
        supports_reasoning_effort=reasoning_effort,
        supports_vision=vision,
        created_at=row.created_at,
    )


class SystemModelCatalogService:
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        secret_key: SecretKey | None = None,
        audit_service: AuditService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._secret_key = secret_key
        self._audit_service = audit_service

    async def _append_secret_event(
        self,
        session: AsyncSession,
        actor: SystemAuditContext,
        model: SystemModelConfigRow,
        *,
        action: str,
        generation_id: uuid.UUID | None,
        reason: str,
    ) -> None:
        if self._audit_service is None:
            return
        view = _model_view(model)
        await self._audit_service.append(
            session,
            AuditActor.system_admin(actor),
            AuditAction.ASSET_UPDATED,
            AuditTarget(
                AuditTargetKind.ASSET,
                uuid.UUID(str(model.id)),
                None,
            ),
            AuditOutcome.SUCCESS,
            {
                "asset_kind": "model",
                "operation": action,
                "generation_id": generation_id,
                "revision": int(model.secret_revision),
                "result": "cleared" if action.endswith(".clear") else "configured",
                "reason": reason,
                "readiness": view.secret_readiness,
            },
            request_id=actor.request_id,
        )

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
                    raise SystemModelNotFound(issued.request_id)
                return await operation(SystemModelRepository(session), issued)
        except SystemModelError:
            raise
        except IntegrityError as exc:
            if _constraint_name(exc) in _CONFLICT_CONSTRAINTS:
                raise SystemModelConflict(issued.request_id) from None
            raise SystemModelInvalid(issued.request_id) from None
        except (ModelSettingsInvalid, SystemModelRepositoryInvariant):
            raise SystemModelInvalid(issued.request_id) from None
        except (
            DBAPIError,
            RuntimeError,
            SecretKeyInvalid,
            SecretProtectionFailed,
        ):
            raise SystemModelStorageUnavailable(issued.request_id) from None

    def _protect_api_key(
        self,
        *,
        model_config_id: uuid.UUID,
        provider_adapter: str,
        settings: Mapping[str, object],
        api_key: str,
        revision: int,
        actor_user_id: str,
    ) -> SystemModelSecretGenerationRow:
        recipient = model_secret_recipient(
            model_config_id,
            provider_adapter,
            settings,
        )
        envelope = SecretEnvelope.protect(
            api_key.encode("utf-8"),
            recipient=recipient,
            key=self._secret_key or SecretKey.from_environment(),
        )
        return SystemModelSecretGenerationRow(
            id=uuid.uuid4(),
            model_config_id=model_config_id,
            revision=revision,
            nonce=envelope.nonce,
            ciphertext=envelope.ciphertext,
            envelope_digest=model_secret_envelope_digest(recipient, envelope),
            created_by_user_id=actor_user_id,
        )

    async def _replace_secret(
        self,
        repository: SystemModelRepository,
        model: SystemModelConfigRow,
        *,
        api_key: str,
        actor_user_id: str,
        reason: str,
    ) -> None:
        previous = await repository.current_secret(model, for_update=True)
        next_revision = int(model.secret_revision) + 1
        generation = self._protect_api_key(
            model_config_id=uuid.UUID(str(model.id)),
            provider_adapter=model.provider_adapter,
            settings=model.settings,
            api_key=api_key,
            revision=next_revision,
            actor_user_id=actor_user_id,
        )
        await repository.add_secret_generation(generation)
        model.current_secret_generation_id = generation.id
        model.secret_revision = next_revision
        await repository.session.flush()
        if previous is not None:
            await repository.add_secret_tombstone(
                SystemModelSecretTombstoneRow(
                    generation_id=previous.id,
                    model_config_id=previous.model_config_id,
                    revision=previous.revision,
                    envelope_digest=previous.envelope_digest,
                    reason=reason,
                    destroyed_by_user_id=actor_user_id,
                    created_at=previous.created_at,
                )
            )
            await repository.delete_secret_generation(previous)

    async def _clear_secret(
        self,
        repository: SystemModelRepository,
        model: SystemModelConfigRow,
        *,
        actor_user_id: str,
    ) -> bool:
        previous = await repository.current_secret(model, for_update=True)
        if previous is None:
            return False
        model.current_secret_generation_id = None
        model.secret_revision = int(model.secret_revision) + 1
        await repository.session.flush()
        await repository.add_secret_tombstone(
            SystemModelSecretTombstoneRow(
                generation_id=previous.id,
                model_config_id=previous.model_config_id,
                revision=previous.revision,
                envelope_digest=previous.envelope_digest,
                reason="cleared",
                destroyed_by_user_id=actor_user_id,
                created_at=previous.created_at,
            )
        )
        await repository.delete_secret_generation(previous)
        return True

    async def list_models(
        self,
        context: SystemAuditContext,
    ) -> SystemModelCatalogView:
        async def operation(
            repository: SystemModelRepository,
            _issued: SystemAuditContext,
        ) -> SystemModelCatalogView:
            state = await repository.catalog_state()
            return SystemModelCatalogView(
                catalog_revision=int(state.revision),
                default_model_config_id=(uuid.UUID(str(state.default_model_config_id)) if state.default_model_config_id is not None else None),
                items=tuple(_model_view(row) for row in await repository.list_models()),
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
            return _model_view(model)

        return await self._admin_operation(context, operation)

    async def list_available_models(self) -> tuple[PublicSystemModelView, ...]:
        try:
            async with self._session_factory() as session, session.begin():
                repository = SystemModelRepository(session)
                state = await repository.catalog_state()
                rows = tuple(
                    row
                    for row in await repository.list_models(active_only=True)
                    if is_provider_adapter_eligible_for_new_binding(
                        row.provider_adapter,
                    )
                    and (not provider_api_key_required(row.provider_adapter) or row.current_secret_generation_id is not None)
                )
                rows = tuple(
                    sorted(
                        rows,
                        key=lambda row: row.id != state.default_model_config_id,
                    )
                )
                return tuple(
                    PublicSystemModelView(
                        model_ref=str(uuid.UUID(str(row.id))),
                        display_name=row.display_name,
                        supports_thinking=row.supports_thinking,
                        supports_reasoning_effort=row.supports_reasoning_effort,
                        supports_vision=row.supports_vision,
                        supports_vision_bridge=row.supports_vision,
                        is_default=row.id == state.default_model_config_id,
                    )
                    for row in rows
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

        async def operation(
            repository: SystemModelRepository,
            actor: SystemAuditContext,
        ) -> SystemModelView:
            state = await repository.catalog_state(for_update=True)
            model = SystemModelConfigRow(
                id=model_id,
                display_name=command.display_name,
                status=command.status,
                provider_adapter=command.provider_adapter,
                provider_model=command.provider_model,
                settings=dict(command.settings),
                supports_thinking=command.supports_thinking,
                supports_reasoning_effort=command.supports_reasoning_effort,
                supports_vision=command.supports_vision,
                payload_checksum=canonical_model_payload_checksum(
                    model_id,
                    command,
                ),
                revision=1,
                secret_revision=0,
                created_by_user_id=str(actor.user_id),
                updated_by_user_id=str(actor.user_id),
            )
            await repository.add_model(model)
            if command.api_key is not None:
                await self._replace_secret(
                    repository,
                    model,
                    api_key=command.api_key,
                    actor_user_id=str(actor.user_id),
                    reason="replaced",
                )
                await self._append_secret_event(
                    repository.session,
                    actor,
                    model,
                    action="model.secret.configure",
                    generation_id=model.current_secret_generation_id,
                    reason="created",
                )
            if state.default_model_config_id is None and model.status == "active" and _model_view(model).secret_readiness == "ready":
                state.default_model_config_id = model.id
            state.revision += 1
            state.updated_by_user_id = str(actor.user_id)
            await repository.session.flush()
            return _model_view(model)

        return await self._admin_operation(issued, operation)

    async def prepare_connection_test(
        self,
        context: SystemAuditContext,
        command: SystemModelConnectionCheck,
    ) -> ConnectionTestSystemModelMaterial:
        issued = self._require_admin(context)
        try:
            command = validate_system_model_connection_test(command)
        except ModelSettingsInvalid:
            raise SystemModelInvalid(issued.request_id) from None

        async def operation(
            _repository: SystemModelRepository,
            _actor: SystemAuditContext,
        ) -> ConnectionTestSystemModelMaterial:
            return ConnectionTestSystemModelMaterial(command=command)

        return await self._admin_operation(issued, operation)

    async def update_model(
        self,
        context: SystemAuditContext,
        model_config_id: uuid.UUID,
        command: UpdateSystemModel,
    ) -> SystemModelView:
        issued = self._require_admin(context)
        try:
            command = validate_update_system_model(command)
            if type(model_config_id) is not uuid.UUID:
                raise ModelSettingsInvalid
        except ModelSettingsInvalid:
            raise SystemModelInvalid(issued.request_id) from None

        async def operation(
            repository: SystemModelRepository,
            actor: SystemAuditContext,
        ) -> SystemModelView:
            state = await repository.catalog_state(for_update=True)
            model = await repository.lock_model(model_config_id)
            if model is None:
                raise SystemModelNotFound(actor.request_id)
            had_secret = model.current_secret_generation_id is not None
            old_recipient = model_secret_recipient(
                uuid.UUID(str(model.id)),
                model.provider_adapter,
                model.settings,
            )
            new_recipient = model_secret_recipient(
                uuid.UUID(str(model.id)),
                command.provider_adapter,
                command.settings,
            )
            if model.current_secret_generation_id is not None and old_recipient != new_recipient and command.api_key is None:
                raise SystemModelInvalid(actor.request_id)
            model.display_name = command.display_name
            model.provider_adapter = command.provider_adapter
            model.provider_model = command.provider_model
            model.settings = dict(command.settings)
            # JSON numbers such as 600 and 600.0 compare equal in Python even
            # though their canonical JSON bytes (and therefore checksum) differ.
            # Force the exact validated representation to be persisted whenever
            # the checksum is replaced in this transaction.
            flag_modified(model, "settings")
            model.supports_thinking = command.supports_thinking
            model.supports_reasoning_effort = command.supports_reasoning_effort
            model.supports_vision = command.supports_vision
            model.payload_checksum = canonical_model_payload_checksum(
                uuid.UUID(str(model.id)),
                command,
            )
            if command.api_key is not None:
                await self._replace_secret(
                    repository,
                    model,
                    api_key=command.api_key,
                    actor_user_id=str(actor.user_id),
                    reason=("recipient_changed" if old_recipient != new_recipient else "replaced"),
                )
                await self._append_secret_event(
                    repository.session,
                    actor,
                    model,
                    action=("model.secret.replace" if had_secret else "model.secret.configure"),
                    generation_id=model.current_secret_generation_id,
                    reason=("recipient_changed" if had_secret and old_recipient != new_recipient else "replaced" if had_secret else "created"),
                )
            model.revision += 1
            model.updated_by_user_id = str(actor.user_id)
            state.revision += 1
            state.updated_by_user_id = str(actor.user_id)
            await repository.session.flush()
            return _model_view(model)

        return await self._admin_operation(issued, operation)

    async def clear_api_key(
        self,
        context: SystemAuditContext,
        model_config_id: uuid.UUID,
        *,
        confirmed: bool,
    ) -> SystemModelView:
        issued = self._require_admin(context)
        if type(model_config_id) is not uuid.UUID or confirmed is not True:
            raise SystemModelInvalid(issued.request_id)

        async def operation(
            repository: SystemModelRepository,
            actor: SystemAuditContext,
        ) -> SystemModelView:
            state = await repository.catalog_state(for_update=True)
            model = await repository.lock_model(model_config_id)
            if model is None:
                raise SystemModelNotFound(actor.request_id)
            previous_generation_id = model.current_secret_generation_id
            changed = await self._clear_secret(
                repository,
                model,
                actor_user_id=str(actor.user_id),
            )
            if changed:
                model.revision += 1
                model.updated_by_user_id = str(actor.user_id)
                state.revision += 1
                state.updated_by_user_id = str(actor.user_id)
                await self._append_secret_event(
                    repository.session,
                    actor,
                    model,
                    action="model.secret.clear",
                    generation_id=previous_generation_id,
                    reason="cleared",
                )
                await repository.session.flush()
            return _model_view(model)

        return await self._admin_operation(issued, operation)

    async def set_status(
        self,
        context: SystemAuditContext,
        model_config_id: uuid.UUID,
        status: str,
    ) -> SystemModelView:
        issued = self._require_admin(context)
        if type(model_config_id) is not uuid.UUID or status not in {"active", "suspended"}:
            raise SystemModelInvalid(issued.request_id)

        async def operation(
            repository: SystemModelRepository,
            actor: SystemAuditContext,
        ) -> SystemModelView:
            state = await repository.catalog_state(for_update=True)
            model = await repository.lock_model(model_config_id)
            if model is None:
                raise SystemModelNotFound(actor.request_id)
            if model.status == status:
                return _model_view(model)
            if status == "active" and not is_provider_adapter_eligible_for_new_binding(
                model.provider_adapter,
            ):
                raise SystemModelInvalid(actor.request_id)
            model.status = status
            model.revision += 1
            model.updated_by_user_id = str(actor.user_id)
            if status == "suspended" and state.default_model_config_id == model.id:
                state.default_model_config_id = None
            elif status == "active" and state.default_model_config_id is None and _model_view(model).secret_readiness == "ready":
                state.default_model_config_id = model.id
            state.revision += 1
            state.updated_by_user_id = str(actor.user_id)
            await repository.session.flush()
            return _model_view(model)

        return await self._admin_operation(issued, operation)

    async def set_default(
        self,
        context: SystemAuditContext,
        model_config_id: uuid.UUID,
    ) -> SystemModelCatalogStateView:
        issued = self._require_admin(context)
        if type(model_config_id) is not uuid.UUID:
            raise SystemModelInvalid(issued.request_id)

        async def operation(
            repository: SystemModelRepository,
            actor: SystemAuditContext,
        ) -> SystemModelCatalogStateView:
            state = await repository.catalog_state(for_update=True)
            model = await repository.lock_model(model_config_id)
            if model is None or model.status != "active":
                raise SystemModelNotFound(actor.request_id)
            if not is_provider_adapter_eligible_for_new_binding(
                model.provider_adapter,
            ):
                raise SystemModelInvalid(actor.request_id)
            if _model_view(model).secret_readiness != "ready":
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
        repository = SystemModelRepository(session)
        try:
            material = await repository.resolve_active_model(
                model_ref,
                load_secret=True,
            )
            if material is None or not is_provider_adapter_eligible_for_new_binding(
                material.model.provider_adapter,
            ):
                raise SystemModelNotFound(request_id)
            model = material.model
            generation = material.secret_generation
            if provider_api_key_required(model.provider_adapter) and generation is None:
                raise SystemModelInvalid(request_id)
            existing = await repository.existing_snapshot(
                project_id=project_id,
                owner_user_id=owner_user_id,
                run_id=run_id,
                purpose=purpose,
            )
            provider_payload = system_model_provider_payload(model)
            generation_id = generation.id if generation is not None else None
            envelope_digest = generation.envelope_digest if generation is not None else None
            if existing is not None:
                if (
                    existing.thread_id != thread_id
                    or existing.model_config_id != model.id
                    or existing.provider_payload != provider_payload
                    or existing.payload_checksum != model.payload_checksum
                    or existing.secret_generation_id != generation_id
                    or existing.secret_envelope_digest != envelope_digest
                ):
                    raise SystemModelConflict(request_id)
                return _snapshot_view(existing)
            snapshot = RunModelConfigSnapshotRow(
                project_id=uuid.UUID(str(project_id)),
                owner_user_id=owner_user_id,
                thread_id=thread_id,
                run_id=run_id,
                purpose=purpose,
                model_config_id=model.id,
                provider_payload=provider_payload,
                payload_checksum=model.payload_checksum,
                secret_generation_id=generation_id,
                secret_envelope_digest=envelope_digest,
            )
            await repository.add_snapshot(snapshot)
            return _snapshot_view(snapshot)
        except SystemModelError:
            raise
        except IntegrityError:
            raise SystemModelConflict(request_id) from None
        except SystemModelRepositoryInvariant:
            raise SystemModelInvalid(request_id) from None
        except (DBAPIError, RuntimeError):
            raise SystemModelStorageUnavailable(request_id) from None


__all__ = ["SystemModelCatalogService"]
