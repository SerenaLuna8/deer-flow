"""Transactional system runtime-policy administration and Run admission."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import (
    AuditAction,
    AuditActor,
    AuditError,
    AuditOutcome,
    AuditTarget,
    AuditTargetKind,
    SystemAuditContext,
    is_issued_system_audit_context,
)
from app.audit.service import AuditService
from app.system_runtime_settings.errors import (
    SystemRuntimePolicyAdministrationRequired,
    SystemRuntimePolicyConflict,
    SystemRuntimePolicyError,
    SystemRuntimePolicyInvalid,
    SystemRuntimePolicyNotFound,
    SystemRuntimePolicyStorageUnavailable,
)
from app.system_runtime_settings.models import (
    AgentRuntimePolicyValue,
    LockedAgentRuntimePolicy,
    LockedMemoryDocumentPolicy,
    MemoryDocumentPolicy,
    RuntimePolicyCatalogView,
    RuntimePolicyEffectScope,
    RuntimePolicySection,
    RuntimePolicyUpdateResult,
    RuntimePolicyValue,
    RuntimePolicyView,
)
from app.system_runtime_settings.repository import (
    SystemRuntimePolicyRepository,
    SystemRuntimePolicyRepositoryInvariant,
)
from app.system_runtime_settings.validation import (
    RuntimePolicyInvalid,
    canonical_policy_payload,
    canonical_policy_payload_for_schema,
    decode_policy_value_for_schema,
    parse_policy_value,
)
from app.system_settings.validation import (
    is_provider_adapter_eligible_for_new_binding,
    provider_api_key_required,
)
from deerflow.persistence.system_runtime_settings import (
    RunRuntimePolicySnapshotRow,
    SystemRuntimePolicyVersionRow,
)
from deerflow.persistence.system_settings import (
    SystemModelConfigRow,
)
from deerflow.persistence.user.model import UserRow

_TARGET_NAMESPACE = uuid.UUID("4475fe37-f970-5820-9dcb-7db6c9585200")
_CONFLICT_CONSTRAINTS = frozenset(
    {
        "pk_run_runtime_policy_snapshots",
        "uq_system_runtime_policy_versions_number",
    }
)
_EFFECT_SCOPE: Mapping[RuntimePolicySection, RuntimePolicyEffectScope] = {
    RuntimePolicySection.AGENT_RUNTIME: "new_requests_and_runs",
    RuntimePolicySection.AUTH: "new_requests",
    RuntimePolicySection.AUTOMATIONS: "new_requests",
    RuntimePolicySection.MEMORY_DOCUMENT: "new_memory_documents",
    RuntimePolicySection.QUOTAS: "next_authoritative_check",
}
_T = TypeVar("_T")


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


def _view(
    section: RuntimePolicySection,
    policy_revision: int,
    version: SystemRuntimePolicyVersionRow,
    updated_at: datetime,
) -> RuntimePolicyView:
    canonical = canonical_policy_payload_for_schema(
        section,
        dict(version.value),
        schema_version=int(version.schema_version),
    )
    if int(version.schema_version) != canonical.schema_version or version.payload_checksum != canonical.checksum or int(version.version_number) != policy_revision:
        raise SystemRuntimePolicyRepositoryInvariant
    return RuntimePolicyView(
        section=section,
        revision=policy_revision,
        schema_version=canonical.schema_version,
        value=decode_policy_value_for_schema(
            section,
            canonical.value,
            schema_version=canonical.schema_version,
        ),
        effect_scope=_EFFECT_SCOPE[section],
        effective_revision=policy_revision,
        updated_at=updated_at,
    )


def _model_refs(value: RuntimePolicyValue) -> frozenset[str]:
    if not isinstance(value, AgentRuntimePolicyValue):
        return frozenset()
    return frozenset(
        ref
        for ref in (
            value.title.model_name,
            value.input_polish.model_name,
            value.summarization.model_name,
            value.memory.model_name,
            value.vision_bridge.model_name,
        )
        if ref is not None
    )


class SystemRuntimePolicyService:
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        audit_service: AuditService,
    ) -> None:
        self._session_factory = session_factory
        self._audit = audit_service

    @staticmethod
    def _require_admin(context: object) -> SystemAuditContext:
        if not is_issued_system_audit_context(context):
            raise SystemRuntimePolicyAdministrationRequired
        return context

    async def _admin_operation(
        self,
        context: object,
        operation: Callable[
            [SystemRuntimePolicyRepository, SystemAuditContext],
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
                    raise SystemRuntimePolicyNotFound(issued.request_id)
                return await operation(
                    SystemRuntimePolicyRepository(session),
                    issued,
                )
        except SystemRuntimePolicyError:
            raise
        except IntegrityError as exc:
            if _constraint_name(exc) in _CONFLICT_CONSTRAINTS:
                raise SystemRuntimePolicyConflict(issued.request_id) from None
            raise SystemRuntimePolicyInvalid(issued.request_id) from None
        except (RuntimePolicyInvalid, SystemRuntimePolicyRepositoryInvariant):
            raise SystemRuntimePolicyInvalid(issued.request_id) from None
        except (AuditError, DBAPIError, RuntimeError):
            raise SystemRuntimePolicyStorageUnavailable(issued.request_id) from None

    async def list_policies(
        self,
        context: SystemAuditContext,
    ) -> RuntimePolicyCatalogView:
        async def operation(
            repository: SystemRuntimePolicyRepository,
            _issued: SystemAuditContext,
        ) -> RuntimePolicyCatalogView:
            # The singleton lock makes catalog_revision and all section
            # pointers one coherent committed view under READ COMMITTED.
            state = await repository.catalog_state(for_update=True)
            sections: dict[RuntimePolicySection, RuntimePolicyView] = {}
            for policy, version in await repository.list_current():
                section = RuntimePolicySection(policy.section)
                sections[section] = _view(
                    section,
                    int(policy.revision),
                    version,
                    policy.updated_at,
                )
            return RuntimePolicyCatalogView.create(
                int(state.revision),
                sections,
            )

        return await self._admin_operation(context, operation)

    async def update_policy(
        self,
        context: SystemAuditContext,
        section: RuntimePolicySection | str,
        *,
        expected_revision: int,
        value: RuntimePolicyValue | Mapping[str, object],
    ) -> RuntimePolicyUpdateResult:
        issued = self._require_admin(context)
        try:
            parsed_section = RuntimePolicySection(section)
            if type(expected_revision) is not int or expected_revision < 1:
                raise RuntimePolicyInvalid
            canonical = canonical_policy_payload(parsed_section, value)
            parsed_value = parse_policy_value(parsed_section, canonical.value)
        except (RuntimePolicyInvalid, TypeError, ValueError):
            raise SystemRuntimePolicyInvalid(issued.request_id) from None

        async def operation(
            repository: SystemRuntimePolicyRepository,
            actor: SystemAuditContext,
        ) -> RuntimePolicyUpdateResult:
            state = await repository.catalog_state(for_update=True)
            policy, previous = await repository.current(
                parsed_section,
                for_update=True,
            )
            if int(policy.revision) != expected_revision:
                raise SystemRuntimePolicyConflict(actor.request_id)

            refs = _model_refs(parsed_value)
            if refs:
                model_ids = tuple(uuid.UUID(ref) for ref in refs)
                active_models = tuple(
                    (
                        await repository.session.execute(
                            select(
                                SystemModelConfigRow.id,
                                SystemModelConfigRow.provider_adapter,
                                SystemModelConfigRow.supports_vision,
                                SystemModelConfigRow.current_secret_generation_id,
                            )
                            .where(
                                SystemModelConfigRow.status == "active",
                                SystemModelConfigRow.id.in_(model_ids),
                            )
                            .with_for_update(
                                read=True,
                                of=SystemModelConfigRow,
                            )
                        )
                    ).all()
                )
                active_by_ref = {str(row.id): row for row in active_models}
                if frozenset(active_by_ref) != refs or any(
                    not is_provider_adapter_eligible_for_new_binding(
                        row.provider_adapter,
                    )
                    or (provider_api_key_required(row.provider_adapter) and row.current_secret_generation_id is None)
                    for row in active_models
                ):
                    raise SystemRuntimePolicyInvalid(actor.request_id)
                if isinstance(parsed_value, AgentRuntimePolicyValue):
                    vision_ref = parsed_value.vision_bridge.model_name
                    if vision_ref is not None:
                        vision_model = active_by_ref[vision_ref]
                        if not vision_model.supports_vision:
                            raise SystemRuntimePolicyInvalid(actor.request_id)

            now = datetime.now(UTC)
            next_revision = int(policy.revision) + 1
            version = SystemRuntimePolicyVersionRow(
                id=uuid.uuid4(),
                section=parsed_section.value,
                version_number=next_revision,
                schema_version=canonical.schema_version,
                value=canonical.value,
                payload_checksum=canonical.checksum,
                supersedes_version_id=previous.id,
                created_by_user_id=str(actor.user_id),
                created_at=now,
            )
            await repository.add_version(policy, version)
            policy.revision = next_revision
            policy.updated_by_user_id = str(actor.user_id)
            policy.updated_at = now
            state.revision = int(state.revision) + 1
            state.updated_by_user_id = str(actor.user_id)
            state.updated_at = now
            await repository.session.flush()

            await self._audit.append(
                repository.session,
                AuditActor.system_admin(actor),
                AuditAction.SYSTEM_SETTING_UPDATED,
                AuditTarget(
                    kind=AuditTargetKind.SYSTEM_SETTING,
                    authority_id=uuid.uuid5(
                        _TARGET_NAMESPACE,
                        parsed_section.value,
                    ),
                    project_id=None,
                ),
                AuditOutcome.SUCCESS,
                {
                    "section": parsed_section.value,
                    "revision": next_revision,
                    "schema_version": canonical.schema_version,
                    "payload_checksum": canonical.checksum,
                    "effect_scope": _EFFECT_SCOPE[parsed_section],
                },
                request_id=actor.request_id,
                occurred_at=now,
            )
            await repository.session.flush()
            view = _view(parsed_section, next_revision, version, now)
            return RuntimePolicyUpdateResult(
                catalog_revision=int(state.revision),
                policy=view,
                effective_at=now,
            )

        return await self._admin_operation(issued, operation)

    @staticmethod
    async def _agent_runtime_for_admission(
        session: AsyncSession,
        *,
        for_update: bool,
    ) -> LockedAgentRuntimePolicy:
        if not isinstance(session, AsyncSession) or not session.in_transaction():
            raise SystemRuntimePolicyRepositoryInvariant
        try:
            policy, version = await SystemRuntimePolicyRepository(session).current(
                RuntimePolicySection.AGENT_RUNTIME,
                for_update=for_update,
            )
            canonical = canonical_policy_payload_for_schema(
                RuntimePolicySection.AGENT_RUNTIME,
                dict(version.value),
                schema_version=int(version.schema_version),
            )
            value = decode_policy_value_for_schema(
                RuntimePolicySection.AGENT_RUNTIME,
                canonical.value,
                schema_version=canonical.schema_version,
            )
            if canonical.schema_version != int(version.schema_version) or canonical.checksum != version.payload_checksum or int(policy.revision) != int(version.version_number) or not isinstance(value, AgentRuntimePolicyValue):
                raise SystemRuntimePolicyRepositoryInvariant
            return LockedAgentRuntimePolicy(
                policy_version_id=uuid.UUID(str(version.id)),
                revision=int(version.version_number),
                schema_version=canonical.schema_version,
                payload_checksum=canonical.checksum,
                value=value,
            )
        except SystemRuntimePolicyRepositoryInvariant:
            raise
        except (RuntimePolicyInvalid, TypeError, ValueError):
            raise SystemRuntimePolicyRepositoryInvariant from None

    @staticmethod
    async def read_agent_runtime_for_admission(
        session: AsyncSession,
    ) -> LockedAgentRuntimePolicy:
        """Read one immutable policy revision without retaining a row lock."""

        return await SystemRuntimePolicyService._agent_runtime_for_admission(
            session,
            for_update=False,
        )

    @staticmethod
    async def lock_agent_runtime_for_admission(
        session: AsyncSession,
    ) -> LockedAgentRuntimePolicy:
        return await SystemRuntimePolicyService._agent_runtime_for_admission(
            session,
            for_update=True,
        )

    @staticmethod
    async def lock_memory_document_for_creation(
        session: AsyncSession,
    ) -> LockedMemoryDocumentPolicy:
        """Lock the exact policy used to create one new Memory document."""

        if not isinstance(session, AsyncSession) or not session.in_transaction():
            raise SystemRuntimePolicyRepositoryInvariant
        try:
            policy, version = await SystemRuntimePolicyRepository(session).current(
                RuntimePolicySection.MEMORY_DOCUMENT,
                for_update=True,
            )
            canonical = canonical_policy_payload_for_schema(
                RuntimePolicySection.MEMORY_DOCUMENT,
                dict(version.value),
                schema_version=int(version.schema_version),
            )
            value = decode_policy_value_for_schema(
                RuntimePolicySection.MEMORY_DOCUMENT,
                canonical.value,
                schema_version=canonical.schema_version,
            )
            if canonical.schema_version != int(version.schema_version) or canonical.checksum != version.payload_checksum or int(policy.revision) != int(version.version_number) or not isinstance(value, MemoryDocumentPolicy):
                raise SystemRuntimePolicyRepositoryInvariant
            return LockedMemoryDocumentPolicy(
                policy_version_id=uuid.UUID(str(version.id)),
                revision=int(version.version_number),
                schema_version=canonical.schema_version,
                payload_checksum=canonical.checksum,
                value=value,
            )
        except SystemRuntimePolicyRepositoryInvariant:
            raise
        except (RuntimePolicyInvalid, TypeError, ValueError):
            raise SystemRuntimePolicyRepositoryInvariant from None

    @staticmethod
    async def admit_run_snapshot(
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        thread_id: str,
        run_id: str,
        locked_policy: LockedAgentRuntimePolicy | None = None,
    ) -> RunRuntimePolicySnapshotRow:
        repository = SystemRuntimePolicyRepository(session)
        existing = await repository.existing_snapshot(
            project_id=project_id,
            owner_user_id=owner_user_id,
            run_id=run_id,
            section=RuntimePolicySection.AGENT_RUNTIME,
        )
        if existing is not None:
            if existing.thread_id != thread_id or (
                locked_policy is not None and (existing.policy_version_id != locked_policy.policy_version_id or existing.schema_version != locked_policy.schema_version or existing.payload_checksum != locked_policy.payload_checksum)
            ):
                raise SystemRuntimePolicyRepositoryInvariant
            return existing
        admitted = locked_policy or await SystemRuntimePolicyService.lock_agent_runtime_for_admission(session)
        if not isinstance(admitted, LockedAgentRuntimePolicy):
            raise SystemRuntimePolicyRepositoryInvariant
        snapshot = RunRuntimePolicySnapshotRow(
            project_id=project_id,
            owner_user_id=owner_user_id,
            thread_id=thread_id,
            run_id=run_id,
            section=RuntimePolicySection.AGENT_RUNTIME.value,
            policy_version_id=admitted.policy_version_id,
            schema_version=admitted.schema_version,
            payload_checksum=admitted.payload_checksum,
        )
        await repository.add_snapshot(snapshot)
        return snapshot


__all__ = ["SystemRuntimePolicyService"]
