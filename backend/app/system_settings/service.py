"""Transactional System Model configuration and API Key ownership."""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
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
from app.model_registry.secrets import (
    ModelProviderSecretInvalid,
    materialize_provider_api_key,
)
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
    TestProviderCandidateConnection,
    TestSystemModelConnection,
    UpdateSystemModel,
)
from app.system_settings.provider_key_fanout import regenerate_model_secret
from app.system_settings.repository import (
    SystemModelRepository,
    SystemModelRepositoryInvariant,
)
from app.system_settings.secrets import model_secret_recipient
from app.system_settings.validation import (
    ModelSettingsInvalid,
    canonical_model_payload_checksum,
    is_provider_adapter_eligible_for_new_binding,
    provider_api_key_required,
    validate_create_system_model,
    validate_system_model_connection_test,
    validate_update_system_model,
)
from deerflow.persistence.model_registry import ModelProviderRow
from deerflow.persistence.system_settings import (
    RunModelConfigSnapshotRow,
    SystemModelConfigRow,
)
from deerflow.persistence.user.model import UserRow
from deerflow.secrets import (
    SecretKey,
    SecretKeyInvalid,
    SecretMaterializationFailed,
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


def _secret_readiness(row: SystemModelConfigRow) -> str:
    configured = row.current_secret_generation_id is not None
    eligible = is_provider_adapter_eligible_for_new_binding(row.provider_adapter)
    ready = eligible and (not provider_api_key_required(row.provider_adapter) or configured)
    return "ready" if ready else "unready"


def _model_view(row: SystemModelConfigRow, provider_name: str) -> SystemModelView:
    frozen_settings = _freeze_json(dict(row.settings))
    if not isinstance(frozen_settings, Mapping) or type(row.max_input_tokens) is not int or not 1 <= row.max_input_tokens <= 2_000_000:
        raise SystemModelRepositoryInvariant
    configured = row.current_secret_generation_id is not None
    return SystemModelView(
        id=uuid.UUID(str(row.id)),
        display_name=row.display_name,
        status=row.status,
        provider_id=uuid.UUID(str(row.provider_id)),
        provider_name=provider_name,
        provider_adapter=row.provider_adapter,
        provider_model=row.provider_model,
        max_input_tokens=row.max_input_tokens,
        settings=frozen_settings,
        supports_thinking=row.supports_thinking,
        supports_reasoning_effort=row.supports_reasoning_effort,
        supports_vision=row.supports_vision,
        payload_checksum=row.payload_checksum,
        api_key_configured=configured,
        secret_readiness=_secret_readiness(row),
        secret_revision=int(row.secret_revision),
        revision=int(row.revision),
        created_by_user_id=row.created_by_user_id,
        updated_by_user_id=row.updated_by_user_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _snapshot_payload(
    row: RunModelConfigSnapshotRow,
) -> tuple[str, str, int, dict[str, object], bool, bool, bool]:
    payload = row.provider_payload
    try:
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError
        adapter = payload["provider_adapter"]
        provider_model = payload["provider_model"]
        max_input_tokens = payload["max_input_tokens"]
        settings = payload["settings"]
        thinking = payload["supports_thinking"]
        reasoning_effort = payload["supports_reasoning_effort"]
        vision = payload["supports_vision"]
        if (
            type(adapter) is not str
            or type(provider_model) is not str
            or type(max_input_tokens) is not int
            or not 1 <= max_input_tokens <= 2_000_000
            or not isinstance(settings, dict)
            or type(thinking) is not bool
            or type(reasoning_effort) is not bool
            or type(vision) is not bool
        ):
            raise ValueError
        return (
            adapter,
            provider_model,
            max_input_tokens,
            dict(settings),
            thinking,
            reasoning_effort,
            vision,
        )
    except (KeyError, TypeError, ValueError):
        raise SystemModelRepositoryInvariant from None


async def _named_model_view(
    repository: SystemModelRepository,
    row: SystemModelConfigRow,
) -> SystemModelView:
    provider_name = (await repository.provider_names()).get(
        uuid.UUID(str(row.provider_id)),
    )
    if provider_name is None:
        raise SystemModelRepositoryInvariant
    return _model_view(row, provider_name)


def _snapshot_view(row: RunModelConfigSnapshotRow) -> RunModelConfigSnapshotView:
    adapter, provider_model, max_input_tokens, settings, thinking, reasoning_effort, vision = _snapshot_payload(row)
    return RunModelConfigSnapshotView(
        project_id=uuid.UUID(str(row.project_id)),
        owner_user_id=row.owner_user_id,
        thread_id=row.thread_id,
        run_id=row.run_id,
        purpose=row.purpose,
        model_ref=str(uuid.UUID(str(row.model_config_id))),
        provider_adapter=adapter,
        provider_model=provider_model,
        max_input_tokens=max_input_tokens,
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
                "result": "configured",
                "reason": reason,
                "readiness": _secret_readiness(model),
            },
            request_id=actor.request_id,
        )

    async def _append_delete_event(
        self,
        session: AsyncSession,
        actor: SystemAuditContext,
        model: SystemModelConfigRow,
    ) -> None:
        if self._audit_service is None:
            return
        await self._audit_service.append(
            session,
            AuditActor.system_admin(actor),
            AuditAction.ASSET_DELETED,
            AuditTarget(
                AuditTargetKind.ASSET,
                uuid.UUID(str(model.id)),
                None,
            ),
            AuditOutcome.SUCCESS,
            {
                "asset_kind": "model",
                "operation": "model.delete",
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

    def _resolved_secret_key(self) -> SecretKey:
        return self._secret_key or SecretKey.from_environment()

    def _materialize_provider_key(
        self,
        provider: ModelProviderRow,
        request_id: str,
    ) -> str:
        """Decrypt the bound Provider's Key once, inside the caller's locks."""

        try:
            return materialize_provider_api_key(
                provider_id=uuid.UUID(str(provider.id)),
                base_url=provider.base_url,
                nonce=bytes(provider.api_key_nonce),
                ciphertext=bytes(provider.api_key_ciphertext),
                key=self._resolved_secret_key(),
            )
        except (
            ModelProviderSecretInvalid,
            SecretKeyInvalid,
            SecretMaterializationFailed,
            UnicodeError,
            ValueError,
        ):
            raise SystemModelStorageUnavailable(request_id) from None

    async def list_models(
        self,
        context: SystemAuditContext,
    ) -> SystemModelCatalogView:
        async def operation(
            repository: SystemModelRepository,
            _issued: SystemAuditContext,
        ) -> SystemModelCatalogView:
            state = await repository.catalog_state()
            provider_names = await repository.provider_names()
            items: list[SystemModelView] = []
            for row in await repository.list_models():
                provider_name = provider_names.get(uuid.UUID(str(row.provider_id)))
                if provider_name is None:
                    raise SystemModelRepositoryInvariant
                items.append(_model_view(row, provider_name))
            return SystemModelCatalogView(
                catalog_revision=int(state.revision),
                default_model_config_id=(uuid.UUID(str(state.default_model_config_id)) if state.default_model_config_id is not None else None),
                items=tuple(items),
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
            return await _named_model_view(repository, model)

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
            provider = await repository.lock_provider_for_share(
                command.provider_id,
            )
            if provider is None:
                raise SystemModelInvalid(actor.request_id)
            settings = dict(command.settings)
            settings["base_url"] = provider.base_url
            derived = validate_create_system_model(
                replace(command, settings=settings),
                allow_derived_base_url=True,
            )
            model = SystemModelConfigRow(
                id=model_id,
                display_name=derived.display_name,
                status=derived.status,
                provider_id=provider.id,
                provider_adapter=derived.provider_adapter,
                provider_model=derived.provider_model,
                max_input_tokens=derived.max_input_tokens,
                settings=dict(derived.settings),
                supports_thinking=derived.supports_thinking,
                supports_reasoning_effort=derived.supports_reasoning_effort,
                supports_vision=derived.supports_vision,
                payload_checksum=canonical_model_payload_checksum(
                    model_id,
                    derived,
                ),
                revision=1,
                secret_revision=0,
                created_by_user_id=str(actor.user_id),
                updated_by_user_id=str(actor.user_id),
            )
            await repository.add_model(model)
            api_key = self._materialize_provider_key(provider, actor.request_id)
            generation = await regenerate_model_secret(
                repository.session,
                model,
                None,
                api_key=api_key,
                actor_user_id=str(actor.user_id),
                secret_key=self._resolved_secret_key(),
                reason="replaced",
            )
            await self._append_secret_event(
                repository.session,
                actor,
                model,
                action="model.secret.configure",
                generation_id=generation.id,
                reason="created",
            )
            if state.default_model_config_id is None and model.status == "active" and _secret_readiness(model) == "ready":
                state.default_model_config_id = model.id
            state.revision += 1
            state.updated_by_user_id = str(actor.user_id)
            await repository.session.flush()
            return _model_view(model, provider.name)

        return await self._admin_operation(issued, operation)

    async def prepare_connection_test(
        self,
        context: SystemAuditContext,
        command: TestSystemModelConnection,
    ) -> ConnectionTestSystemModelMaterial:
        """Stored-Key test: derive URL and Key from the selected Provider."""

        issued = self._require_admin(context)
        if not isinstance(command, TestSystemModelConnection) or type(command.provider_id) is not uuid.UUID or (isinstance(command.settings, Mapping) and "base_url" in command.settings):
            raise SystemModelInvalid(issued.request_id)

        async def operation(
            repository: SystemModelRepository,
            actor: SystemAuditContext,
        ) -> ConnectionTestSystemModelMaterial:
            provider = await repository.lock_provider_for_share(
                command.provider_id,
            )
            if provider is None:
                raise SystemModelInvalid(actor.request_id)
            settings: object = command.settings
            if isinstance(settings, Mapping):
                settings = dict(settings)
                settings["base_url"] = provider.base_url
            checked = validate_system_model_connection_test(
                SystemModelConnectionCheck(
                    provider_adapter=command.provider_adapter,
                    provider_model=command.provider_model,
                    max_input_tokens=command.max_input_tokens,
                    settings=settings,  # type: ignore[arg-type]  # validated above
                    supports_vision=command.supports_vision,
                    api_key=self._materialize_provider_key(
                        provider,
                        actor.request_id,
                    ),
                )
            )
            return ConnectionTestSystemModelMaterial(command=checked)

        return await self._admin_operation(issued, operation)

    async def prepare_candidate_connection_test(
        self,
        context: SystemAuditContext,
        command: TestProviderCandidateConnection,
    ) -> ConnectionTestSystemModelMaterial:
        """Candidate test: explicit URL and transient Key, no rows touched.

        Never falls back to a stored Provider Key and never persists anything;
        the material only lives for this authorized request.
        """

        issued = self._require_admin(context)
        try:
            if not isinstance(command, TestProviderCandidateConnection):
                raise ModelSettingsInvalid
            if isinstance(command.settings, Mapping) and "base_url" in command.settings:
                raise ModelSettingsInvalid
            settings: object = command.settings
            if isinstance(settings, Mapping):
                settings = dict(settings)
                settings["base_url"] = command.base_url
            checked = validate_system_model_connection_test(
                SystemModelConnectionCheck(
                    provider_adapter=command.provider_adapter,
                    provider_model=command.provider_model,
                    max_input_tokens=command.max_input_tokens,
                    settings=settings,  # type: ignore[arg-type]  # validated above
                    supports_vision=command.supports_vision,
                    api_key=command.api_key,
                )
            )
        except ModelSettingsInvalid:
            raise SystemModelInvalid(issued.request_id) from None

        async def operation(
            _repository: SystemModelRepository,
            _actor: SystemAuditContext,
        ) -> ConnectionTestSystemModelMaterial:
            return ConnectionTestSystemModelMaterial(command=checked)

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
            # Rebinding protocol: read the current binding under the catalog
            # lock, lock old and new Providers in UUID order (FOR SHARE), then
            # lock the model and re-verify the binding did not move.
            current_provider_id = await repository.current_model_provider_id(
                model_config_id,
            )
            if current_provider_id is None:
                raise SystemModelNotFound(actor.request_id)
            providers = await repository.lock_providers_for_share(
                (current_provider_id, command.provider_id),
            )
            target_provider = providers.get(command.provider_id)
            if target_provider is None:
                raise SystemModelInvalid(actor.request_id)
            model = await repository.lock_model(model_config_id)
            if model is None:
                raise SystemModelNotFound(actor.request_id)
            if uuid.UUID(str(model.provider_id)) != current_provider_id:
                raise SystemModelConflict(actor.request_id)
            rebind = command.provider_id != current_provider_id
            adapter_changed = command.provider_adapter != model.provider_adapter
            had_secret = model.current_secret_generation_id is not None
            old_recipient = model_secret_recipient(
                uuid.UUID(str(model.id)),
                model.provider_adapter,
                model.settings,
            )
            settings = dict(command.settings)
            settings["base_url"] = target_provider.base_url
            derived = validate_update_system_model(
                replace(command, settings=settings),
                allow_derived_base_url=True,
            )
            model.display_name = derived.display_name
            model.provider_id = target_provider.id
            model.provider_adapter = derived.provider_adapter
            model.provider_model = derived.provider_model
            model.max_input_tokens = derived.max_input_tokens
            model.settings = dict(derived.settings)
            # JSON numbers such as 600 and 600.0 compare equal in Python even
            # though their canonical JSON bytes (and therefore checksum) differ.
            # Force the exact validated representation to be persisted whenever
            # the checksum is replaced in this transaction.
            flag_modified(model, "settings")
            model.supports_thinking = derived.supports_thinking
            model.supports_reasoning_effort = derived.supports_reasoning_effort
            model.supports_vision = derived.supports_vision
            model.payload_checksum = canonical_model_payload_checksum(
                uuid.UUID(str(model.id)),
                derived,
            )
            # A rebind always regenerates — the Provider identity selects the
            # Key even when URL and recipient stay identical. Display or
            # timeout edits never read or re-encrypt the Provider Key.
            if rebind or adapter_changed:
                previous = await repository.current_secret(model, for_update=True)
                new_recipient = model_secret_recipient(
                    uuid.UUID(str(model.id)),
                    model.provider_adapter,
                    model.settings,
                )
                reason = "recipient_changed" if new_recipient != old_recipient else "replaced"
                api_key = self._materialize_provider_key(
                    target_provider,
                    actor.request_id,
                )
                generation = await regenerate_model_secret(
                    repository.session,
                    model,
                    previous,
                    api_key=api_key,
                    actor_user_id=str(actor.user_id),
                    secret_key=self._resolved_secret_key(),
                    reason=reason,
                )
                await self._append_secret_event(
                    repository.session,
                    actor,
                    model,
                    action=("model.secret.replace" if had_secret else "model.secret.configure"),
                    generation_id=generation.id,
                    reason=(reason if had_secret else "created"),
                )
            model.revision += 1
            model.updated_by_user_id = str(actor.user_id)
            state.revision += 1
            state.updated_by_user_id = str(actor.user_id)
            await repository.session.flush()
            return _model_view(model, target_provider.name)

        return await self._admin_operation(issued, operation)

    async def delete_model(
        self,
        context: SystemAuditContext,
        model_config_id: uuid.UUID,
    ) -> None:
        issued = self._require_admin(context)
        if type(model_config_id) is not uuid.UUID:
            raise SystemModelNotFound(issued.request_id)

        async def operation(
            repository: SystemModelRepository,
            actor: SystemAuditContext,
        ) -> None:
            state = await repository.catalog_state(for_update=True)
            model = await repository.lock_model(model_config_id)
            if model is None:
                raise SystemModelNotFound(actor.request_id)
            model.status = "suspended"
            model.deleted_at = datetime.now(UTC)
            model.revision += 1
            model.updated_by_user_id = str(actor.user_id)
            if state.default_model_config_id == model.id:
                state.default_model_config_id = None
            state.revision += 1
            state.updated_by_user_id = str(actor.user_id)
            await repository.session.flush()
            await self._append_delete_event(
                repository.session,
                actor,
                model,
            )

        await self._admin_operation(issued, operation)

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
                return await _named_model_view(repository, model)
            if status == "active" and not is_provider_adapter_eligible_for_new_binding(
                model.provider_adapter,
            ):
                raise SystemModelInvalid(actor.request_id)
            model.status = status
            model.revision += 1
            model.updated_by_user_id = str(actor.user_id)
            if status == "suspended" and state.default_model_config_id == model.id:
                state.default_model_config_id = None
            elif status == "active" and state.default_model_config_id is None and _secret_readiness(model) == "ready":
                state.default_model_config_id = model.id
            state.revision += 1
            state.updated_by_user_id = str(actor.user_id)
            await repository.session.flush()
            return await _named_model_view(repository, model)

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
            if _secret_readiness(model) != "ready":
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
