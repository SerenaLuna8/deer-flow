"""Application service for owner-private Memory documents."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.personalization.repository import (
    AccountPersonalizationNotFound,
    AccountPersonalizationRepository,
)
from app.private_work.context import (
    PrivateWorkContext,
    require_issued_private_work_context,
)
from app.private_work.errors import (
    PrivateWorkConflict,
    PrivateWorkDreamModelUnavailable,
    PrivateWorkError,
    PrivateWorkInvalid,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from app.private_work.memory_dream_service import (
    MemoryDreamAdmissionService,
    MemoryDreamModelUnavailable,
)
from app.private_work.memory_injection import (
    MemoryInjectionAssessment,
    MemoryInjectionCandidate,
    assess_memory_injection,
)
from app.private_work.memory_observability import record_memory_failure
from app.private_work.revalidation import PrivateWorkRevalidator
from app.projects.capabilities import Capability
from app.system_runtime_settings.materializer import SystemRuntimePolicyMaterializer
from app.system_runtime_settings.models import (
    AgentRuntimePolicyValue,
    RuntimePolicySection,
)
from deerflow.memory_contract import (
    MemoryDocumentInvalid,
    estimate_memory_tokens,
    validate_memory_document,
)
from deerflow.persistence.private_work.memory_document_repository import (
    DEFAULT_EPISODE_RETENTION_DAYS,
    DEFAULT_MEMORY_NAMESPACE,
    MemoryDocumentConflict,
    MemoryDocumentNotFound,
    MemoryDocumentRepository,
    MemoryDocumentScope,
    MemoryDocumentState,
    MemoryDocumentVersionRecord,
    MemoryDreamAdmissionRecord,
    MemoryEpisodeCursorInvalid,
    MemoryEpisodePage,
    MemoryPendingEntryRecord,
)


class PrivateMemoryDocumentService:
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        repository_builder=MemoryDocumentRepository,
        revalidator: PrivateWorkRevalidator | None = None,
        dream_admission: MemoryDreamAdmissionService | None = None,
        personalization_repository_builder=AccountPersonalizationRepository,
        audit=None,
    ) -> None:
        if not callable(session_factory) or not callable(repository_builder) or not callable(personalization_repository_builder):
            raise ValueError("Memory document service configuration is invalid")
        if audit is not None and not all(
            callable(getattr(audit, method, None))
            for method in (
                "memory_dream_admitted",
                "memory_restore_executed",
            )
        ):
            raise ValueError("Memory document audit port is invalid")
        self._sessions = session_factory
        self._repository_builder = repository_builder
        self._revalidator = revalidator or PrivateWorkRevalidator()
        self._dream_admission = dream_admission or MemoryDreamAdmissionService(
            repository_builder=repository_builder,
            personalization_repository_builder=(personalization_repository_builder),
        )
        self._personalization_repository_builder = personalization_repository_builder
        self._audit = audit

    @staticmethod
    def _scope(context: PrivateWorkContext) -> MemoryDocumentScope:
        context = require_issued_private_work_context(context)
        return MemoryDocumentScope(
            project_id=context.project_id,
            owner_user_id=str(context.user_id),
            namespace=DEFAULT_MEMORY_NAMESPACE,
        )

    async def get(
        self,
        context: PrivateWorkContext,
    ) -> tuple[MemoryDocumentState, str]:
        """Return the rolling legacy two-state read without new failure modes."""

        scope = self._scope(context)
        try:
            async with self._sessions() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_READ_OWN,
                )
                state = await self._repository_builder(session).read_state(scope)
                policy = await SystemRuntimePolicyMaterializer.materialize_current_in_session(
                    session,
                    RuntimePolicySection.AGENT_RUNTIME,
                )
                injection_status = "ok"
                if state.document.version >= 1 and isinstance(policy, AgentRuntimePolicyValue) and estimate_memory_tokens(state.document.content) > policy.memory.max_injection_tokens:
                    injection_status = "skipped_over_budget"
                return state, injection_status
        except PrivateWorkError:
            raise
        except DBAPIError as error:
            record_memory_failure("get", error, failure_category="database")
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception as error:
            record_memory_failure("get", error, failure_category="internal")
            raise PrivateWorkUnavailable(context.request_id) from None

    async def get_with_injection_advisory(
        self,
        context: PrivateWorkContext,
    ) -> tuple[MemoryDocumentState, MemoryInjectionAssessment]:
        """Return current non-continuation evidence for opt-in clients."""

        scope = self._scope(context)
        try:
            async with self._sessions() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_READ_OWN,
                )
                policy = await SystemRuntimePolicyMaterializer.materialize_current_in_session(
                    session,
                    RuntimePolicySection.AGENT_RUNTIME,
                )
                if not isinstance(policy, AgentRuntimePolicyValue):
                    raise RuntimeError("Agent runtime policy is invalid")
                account_enabled = True
                if policy.memory.enabled:
                    preference = await self._personalization_repository_builder(session).read_memory(
                        str(context.user_id),
                        for_update=False,
                    )
                    account_enabled = preference.memory_enabled
                state = await self._repository_builder(session).read_state(scope)
                candidate = None
                if state.document.version >= 1:
                    candidate = MemoryInjectionCandidate(
                        content=state.document.content,
                        content_digest=state.document.content_digest,
                        sections=state.document.sections,
                    )
                advisory = assess_memory_injection(
                    platform_enabled=policy.memory.enabled,
                    account_enabled=account_enabled,
                    max_injection_tokens=policy.memory.max_injection_tokens,
                    candidate=candidate,
                )
                return state, advisory
        except PrivateWorkError:
            raise
        except (MemoryDocumentConflict, MemoryDocumentInvalid) as error:
            record_memory_failure(
                "get_with_injection_advisory",
                error,
                failure_category="data_integrity",
            )
            raise PrivateWorkConflict(context.request_id) from None
        except DBAPIError as error:
            record_memory_failure(
                "get_with_injection_advisory",
                error,
                failure_category="database",
            )
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception as error:
            record_memory_failure(
                "get_with_injection_advisory",
                error,
                failure_category="internal",
            )
            raise PrivateWorkUnavailable(context.request_id) from None

    async def list_versions(
        self,
        context: PrivateWorkContext,
        *,
        limit: int,
        offset: int,
    ) -> tuple[MemoryDocumentVersionRecord, ...]:
        scope = self._scope(context)
        try:
            async with self._sessions() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_READ_OWN,
                )
                return await self._repository_builder(session).list_versions(
                    scope,
                    limit=limit,
                    offset=offset,
                )
        except PrivateWorkError:
            raise
        except DBAPIError as error:
            record_memory_failure(
                "list_versions",
                error,
                failure_category="database",
            )
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception as error:
            record_memory_failure(
                "list_versions",
                error,
                failure_category="internal",
            )
            raise PrivateWorkUnavailable(context.request_id) from None

    async def list_pending(
        self,
        context: PrivateWorkContext,
        *,
        limit: int,
        offset: int,
    ) -> tuple[MemoryPendingEntryRecord, ...]:
        """Backlog read model: what the next Dream will organize, oldest first."""

        scope = self._scope(context)
        try:
            async with self._sessions() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_READ_OWN,
                )
                return await self._repository_builder(session).list_pending_entries(
                    scope,
                    limit=limit,
                    offset=offset,
                )
        except PrivateWorkError:
            raise
        except DBAPIError as error:
            record_memory_failure(
                "list_pending",
                error,
                failure_category="database",
            )
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception as error:
            record_memory_failure(
                "list_pending",
                error,
                failure_category="internal",
            )
            raise PrivateWorkUnavailable(context.request_id) from None

    async def list_episodes(
        self,
        context: PrivateWorkContext,
        *,
        q: str | None,
        tags: tuple[str, ...],
        cursor: str | None,
        limit: int,
        before: datetime | None = None,
    ) -> MemoryEpisodePage:
        """Archive read model: ranked search with ``q``, time browse without."""

        if q is not None and cursor is not None:
            raise PrivateWorkInvalid(context.request_id)
        scope = self._scope(context)
        try:
            async with self._sessions() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_READ_OWN,
                )
                repository = self._repository_builder(session)
                now = datetime.now(UTC)
                policy = await SystemRuntimePolicyMaterializer.materialize_current_in_session(
                    session,
                    RuntimePolicySection.AGENT_RUNTIME,
                )
                retention_days = policy.memory.episode_retention_days if isinstance(policy, AgentRuntimePolicyValue) else DEFAULT_EPISODE_RETENTION_DAYS
                if q is not None:
                    return MemoryEpisodePage(
                        items=await repository.search_episodes(
                            scope,
                            query=q,
                            tags=tags,
                            limit=limit,
                            retention_days=retention_days,
                            now=now,
                        ),
                        next_cursor=None,
                    )
                return await repository.list_episodes(
                    scope,
                    tags=tags,
                    cursor=cursor,
                    before=before,
                    limit=limit,
                    retention_days=retention_days,
                    now=now,
                )
        except PrivateWorkError:
            raise
        except MemoryEpisodeCursorInvalid:
            raise PrivateWorkInvalid(context.request_id) from None
        except DBAPIError as error:
            record_memory_failure(
                "list_episodes",
                error,
                failure_category="database",
            )
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception as error:
            record_memory_failure(
                "list_episodes",
                error,
                failure_category="internal",
            )
            raise PrivateWorkUnavailable(context.request_id) from None

    async def get_version(
        self,
        context: PrivateWorkContext,
        version: int,
    ) -> MemoryDocumentVersionRecord:
        scope = self._scope(context)
        try:
            async with self._sessions() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_READ_OWN,
                )
                return await self._repository_builder(session).read_version(
                    scope,
                    version,
                )
        except MemoryDocumentNotFound:
            raise PrivateWorkNotFound(context.request_id) from None
        except PrivateWorkError:
            raise
        except DBAPIError as error:
            record_memory_failure(
                "get_version",
                error,
                failure_category="database",
            )
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception as error:
            record_memory_failure(
                "get_version",
                error,
                failure_category="internal",
            )
            raise PrivateWorkUnavailable(context.request_id) from None

    async def dream(
        self,
        context: PrivateWorkContext,
    ) -> MemoryDreamAdmissionRecord:
        scope = self._scope(context)
        try:
            async with self._sessions() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_CREATE,
                    lock=True,
                )
                admitted = await self._dream_admission.admit(
                    session,
                    scope,
                    trigger="manual_dream",
                    now=datetime.now(UTC),
                )
                await self._audit_manual_dream_admission(
                    session,
                    context,
                    admitted,
                )
                return admitted
        except MemoryDreamModelUnavailable:
            raise PrivateWorkDreamModelUnavailable(context.request_id) from None
        except PrivateWorkError:
            raise
        except MemoryDocumentConflict:
            raise PrivateWorkConflict(context.request_id) from None
        except MemoryDocumentInvalid:
            raise PrivateWorkConflict(context.request_id) from None
        except DBAPIError as error:
            record_memory_failure("dream", error, failure_category="database")
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception as error:
            record_memory_failure("dream", error, failure_category="internal")
            raise PrivateWorkUnavailable(context.request_id) from None

    async def _audit_manual_dream_admission(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        admitted: MemoryDreamAdmissionRecord,
    ) -> None:
        """Append only a newly queued manual Dream in its admission transaction."""

        if admitted.disposition != "queued" or self._audit is None:
            return
        if admitted.job_id is None:
            raise RuntimeError("Dream admission returned no Job")
        await self._audit.memory_dream_admitted(
            session,
            project_id=context.project_id,
            job_id=admitted.job_id,
            request_id=context.request_id,
            origin="manual",
            trigger=("budget_rewrite" if admitted.admission_kind == "budget_rewrite" else "manual_dream"),
            history_count=admitted.history_count,
            context=context,
        )

    async def restore(
        self,
        context: PrivateWorkContext,
        *,
        target_version: int,
        expected_current_version: int,
    ) -> MemoryDocumentVersionRecord:
        scope = self._scope(context)
        try:
            async with self._sessions() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_CREATE,
                    lock=True,
                )
                policy = await SystemRuntimePolicyMaterializer.materialize_current_in_session(
                    session,
                    RuntimePolicySection.AGENT_RUNTIME,
                    for_update=True,
                )
                preference = await self._personalization_repository_builder(session).read_memory(
                    scope.owner_user_id,
                    for_update=True,
                )
                if not preference.memory_enabled or not isinstance(policy, AgentRuntimePolicyValue) or not policy.memory.enabled:
                    raise MemoryDocumentConflict("Memory is disabled")
                repository = self._repository_builder(session)
                state = await repository.read_state(scope, for_update=True)
                if state.document.sections_policy_version_id is None:
                    raise MemoryDocumentNotFound
                target = await repository.read_version(scope, target_version)
                validate_memory_document(
                    target.content,
                    policy.memory.max_injection_tokens,
                    sections=state.document.sections,
                )
                previous_version = state.document.version
                changed = state.document.content_digest != target.content_digest
                restored = await repository.restore_version(
                    scope,
                    target_version=target_version,
                    expected_current_version=expected_current_version,
                    expected_sections=state.document.sections,
                    max_tokens=policy.memory.max_injection_tokens,
                    now=datetime.now(UTC),
                )
                if self._audit is not None:
                    await self._audit.memory_restore_executed(
                        session,
                        context,
                        source_version=target_version,
                        previous_version=previous_version,
                        published_version=restored.version,
                        changed=changed,
                    )
                return restored
        except MemoryDocumentNotFound:
            raise PrivateWorkNotFound(context.request_id) from None
        except MemoryDocumentConflict:
            raise PrivateWorkConflict(context.request_id) from None
        except AccountPersonalizationNotFound:
            raise PrivateWorkNotFound(context.request_id) from None
        except MemoryDocumentInvalid:
            raise PrivateWorkConflict(context.request_id) from None
        except PrivateWorkError:
            raise
        except DBAPIError as error:
            record_memory_failure("restore", error, failure_category="database")
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception as error:
            record_memory_failure("restore", error, failure_category="internal")
            raise PrivateWorkUnavailable(context.request_id) from None


__all__ = ["PrivateMemoryDocumentService"]
