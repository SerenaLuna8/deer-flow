"""Dream admission and Scheduler coordination for document Memory."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.personalization.repository import (
    AccountPersonalizationNotFound,
    AccountPersonalizationRepository,
)
from app.private_work.account_private_lifecycle import (
    AccountPrivateLifecycle,
    AccountPrivateLifecyclePort,
)
from app.projects.capabilities import Capability, capabilities_for
from app.projects.context import resolve_project_context_in_transaction
from app.projects.errors import ProjectForbidden, ProjectNotFound
from app.projects.models import ProjectRole
from app.system_runtime_settings.materializer import SystemRuntimePolicyMaterializer
from app.system_runtime_settings.models import (
    AgentRuntimePolicyValue,
    LockedMemoryDocumentPolicy,
    RuntimePolicySection,
)
from app.system_runtime_settings.service import SystemRuntimePolicyService
from app.system_settings.execution_payload import freeze_system_model_material
from app.system_settings.repository import SystemModelRepository
from deerflow.memory_contract import (
    DREAM_PROMPT_VERSION,
    estimate_memory_tokens,
    render_empty_memory_document,
)
from deerflow.persistence.jobs.sql import JobRepository
from deerflow.persistence.private_work.memory_document_repository import (
    MemoryBudgetRewriteScanCursor,
    MemoryBudgetRewriteScopePage,
    MemoryDocumentRepository,
    MemoryDocumentScope,
    MemoryDreamAdmissionRecord,
    MemoryDreamFrozenRuntime,
    MemoryDreamTrigger,
)
from deerflow.persistence.user.private_lifecycle import AccountPrivateGeneration

_SCHEDULER_REQUEST_ID = "memory-dream-scheduler"
_BUDGET_REWRITE_DISCOVERY_PAGE_SIZE = 100
_PRIVATE_WORK_CREATE_ROLES = tuple(role.value for role in ProjectRole if Capability.PRIVATE_WORK_CREATE in capabilities_for(role))
logger = logging.getLogger(__name__)


class MemoryDreamModelUnavailable(RuntimeError):
    """The enabled Memory policy points to no resolvable Dream model."""

    def __init__(self) -> None:
        super().__init__("Memory Dream model is unavailable")


class MemoryDreamAdmissionService:
    """Freeze exact policy, model, preference and oldest history in one transaction."""

    def __init__(
        self,
        *,
        repository_builder=MemoryDocumentRepository,
        personalization_repository_builder=AccountPersonalizationRepository,
        job_repository_builder=JobRepository,
        account_private_lifecycle: AccountPrivateLifecyclePort | None = None,
    ) -> None:
        if not all(
            callable(value)
            for value in (
                repository_builder,
                personalization_repository_builder,
                job_repository_builder,
            )
        ):
            raise ValueError("Dream admission configuration is invalid")
        self._repository_builder = repository_builder
        self._personalization_repository_builder = personalization_repository_builder
        self._job_repository_builder = job_repository_builder
        self._account_private_lifecycle = account_private_lifecycle or AccountPrivateLifecycle()

    def _repository(self, session: AsyncSession) -> MemoryDocumentRepository:
        return self._repository_builder(
            session,
            jobs=self._job_repository_builder(session),
        )

    @staticmethod
    def _nothing_pending() -> MemoryDreamAdmissionRecord:
        return MemoryDreamAdmissionRecord(
            disposition="nothing_pending",
            job_id=None,
            history_count=0,
        )

    @staticmethod
    def _requires_budget_rewrite(state, policy: AgentRuntimePolicyValue) -> bool:
        document = state.document
        return state.pending_count == 0 and document is not None and document.version >= 1 and estimate_memory_tokens(document.content) > policy.memory.max_injection_tokens

    @staticmethod
    async def _platform_policy(
        session: AsyncSession,
        *,
        for_update: bool,
    ) -> tuple[AgentRuntimePolicyValue, int] | None:
        policy, policy_revision = await SystemRuntimePolicyMaterializer.materialize_current_with_revision_in_session(
            session,
            RuntimePolicySection.AGENT_RUNTIME,
            for_update=for_update,
        )
        if not isinstance(policy, AgentRuntimePolicyValue) or not policy.memory.enabled:
            return None
        return policy, policy_revision

    @staticmethod
    async def _platform_runtime(
        session: AsyncSession,
        *,
        create_document: bool,
        policy_state: tuple[AgentRuntimePolicyValue, int] | None = None,
    ) -> (
        tuple[
            AgentRuntimePolicyValue,
            int,
            object,
            LockedMemoryDocumentPolicy | None,
        ]
        | None
    ):
        if policy_state is None:
            policy_state = await MemoryDreamAdmissionService._platform_policy(
                session,
                for_update=True,
            )
        if policy_state is None:
            return None
        policy, policy_revision = policy_state
        creation_policy = (
            await SystemRuntimePolicyService.lock_memory_document_for_creation(
                session,
            )
            if create_document
            else None
        )
        model = await SystemModelRepository(session).resolve_active_model(
            policy.memory.model_name,
            load_secret=True,
        )
        if model is None:
            raise MemoryDreamModelUnavailable
        return policy, policy_revision, model, creation_policy

    async def admit(
        self,
        session: AsyncSession,
        scope: MemoryDocumentScope,
        *,
        trigger: MemoryDreamTrigger,
        now: datetime,
        account_private_generation: AccountPrivateGeneration | None = None,
    ) -> MemoryDreamAdmissionRecord:
        if account_private_generation is None:
            account_private_generation = await self.require_account_private_generation_after_membership(
                session,
                scope,
            )
        elif type(account_private_generation) is not AccountPrivateGeneration or account_private_generation.owner_user_id != scope.owner_user_id:
            raise ValueError("Dream account-private generation mismatch")
        repository = self._repository(session)
        state = await repository.read_state(scope)
        policy_state = None
        if state.pending_count == 0:
            policy_state = await self._platform_policy(
                session,
                for_update=True,
            )
            if policy_state is None or not self._requires_budget_rewrite(
                state,
                policy_state[0],
            ):
                return self._nothing_pending()
        if policy_state is None:
            runtime = await self._platform_runtime(
                session,
                create_document=state.document.sections_policy_version_id is None,
            )
        else:
            runtime = await self._platform_runtime(
                session,
                create_document=state.document.sections_policy_version_id is None,
                policy_state=policy_state,
            )
        if runtime is None:
            return self._nothing_pending()
        return await self._admit_with_runtime(
            session,
            scope,
            trigger=trigger,
            now=now,
            runtime=runtime,
            account_private_generation=account_private_generation,
        )

    async def require_account_private_generation_after_membership(
        self,
        session: AsyncSession,
        scope: MemoryDocumentScope,
    ) -> AccountPrivateGeneration:
        """Acquire the L-05 User guard before any Dream domain lock."""

        if type(scope) is not MemoryDocumentScope:
            raise TypeError("MemoryDocumentScope is required")
        return await self._account_private_lifecycle.require_active_after_membership(
            session,
            scope.owner_user_id,
        )

    async def _admit_with_runtime(
        self,
        session: AsyncSession,
        scope: MemoryDocumentScope,
        *,
        trigger: MemoryDreamTrigger,
        now: datetime,
        runtime: tuple[
            AgentRuntimePolicyValue,
            int,
            object,
            LockedMemoryDocumentPolicy | None,
        ],
        account_private_generation: AccountPrivateGeneration,
        budget_only: bool = False,
    ) -> MemoryDreamAdmissionRecord:
        _policy, policy_revision, model, creation_policy = runtime
        try:
            preference = await self._personalization_repository_builder(session).read_memory(
                scope.owner_user_id,
                for_update=True,
            )
        except AccountPersonalizationNotFound:
            return self._nothing_pending()
        if not preference.memory_enabled:
            return self._nothing_pending()
        frozen = MemoryDreamFrozenRuntime(
            preference_version=preference.version,
            policy_revision=policy_revision,
            model_execution=freeze_system_model_material(model),
            prompt_version=DREAM_PROMPT_VERSION,
        )
        repository = self._repository(session)
        effective_trigger: MemoryDreamTrigger = trigger
        if trigger in {"auto_dream", "manual_dream"}:
            # The empty-batch budget rescue is a server-side decision: it is
            # legal only when the current document exceeds the current budget
            # and there is no pending history to consume. Requests carry no
            # trigger input.
            state = await repository.read_state(scope)
            if self._requires_budget_rewrite(state, _policy):
                effective_trigger = "budget_rewrite"
        if budget_only and effective_trigger != "budget_rewrite":
            # Budget discovery over-approximates; a scope that turns out to be
            # inside budget (or gained pending work) must wait for the normal
            # due rules instead of dreaming early.
            return self._nothing_pending()
        return await repository.admit_dream(
            scope,
            account_private_generation=account_private_generation,
            trigger=effective_trigger,
            frozen=frozen,
            initial_content=(render_empty_memory_document(creation_policy.value.sections) if creation_policy is not None else None),
            initial_sections=(tuple(creation_policy.value.sections) if creation_policy is not None else None),
            sections_policy_version_id=(creation_policy.policy_version_id if creation_policy is not None else None),
            now=now,
        )

    async def list_due_scopes(
        self,
        session: AsyncSession,
        *,
        now: datetime,
        max_jobs: int = 100,
    ) -> tuple[MemoryDocumentScope, ...]:
        if type(max_jobs) is not int or not 1 <= max_jobs <= 100:
            raise ValueError("Dream Scheduler batch is invalid")
        policy_state = await self._platform_policy(
            session,
            for_update=False,
        )
        if policy_state is None:
            return ()
        policy, _policy_revision = policy_state
        return await self._repository(session).list_due_scopes(
            now=now,
            interval_minutes=policy.memory.dream_interval_minutes,
            limit=max_jobs,
        )

    async def list_budget_rewrite_scope_page(
        self,
        session: AsyncSession,
        *,
        cursor: MemoryBudgetRewriteScanCursor | None = None,
        page_size: int = _BUDGET_REWRITE_DISCOVERY_PAGE_SIZE,
    ) -> MemoryBudgetRewriteScopePage:
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise ValueError("Dream Scheduler batch is invalid")
        policy_state = await self._platform_policy(
            session,
            for_update=False,
        )
        if policy_state is None:
            return MemoryBudgetRewriteScopePage(scopes=(), next_cursor=None)
        policy, _policy_revision = policy_state
        return await self._repository(session).list_budget_rewrite_scope_page(
            budget_tokens=policy.memory.max_injection_tokens,
            admissible_roles=_PRIVATE_WORK_CREATE_ROLES,
            cursor=cursor,
            limit=page_size,
        )

    async def admit_scheduled_scope(
        self,
        session: AsyncSession,
        scope: MemoryDocumentScope,
        *,
        now: datetime,
        require_due: bool = True,
    ) -> MemoryDreamAdmissionRecord:
        context = await resolve_project_context_in_transaction(
            session,
            uuid.UUID(scope.owner_user_id),
            scope.project_id,
            _SCHEDULER_REQUEST_ID,
            lock=True,
        )
        context.require(Capability.PRIVATE_WORK_CREATE)
        account_private_generation = await self._account_private_lifecycle.require_active_after_membership(
            session,
            scope.owner_user_id,
        )
        repository = self._repository(session)
        state = await repository.read_state(scope)
        policy_state = await self._platform_policy(
            session,
            for_update=True,
        )
        if policy_state is None:
            return self._nothing_pending()
        policy, _policy_revision = policy_state
        if require_due:
            if not await repository.is_scope_due(
                scope,
                now=now,
                interval_minutes=policy.memory.dream_interval_minutes,
            ):
                return self._nothing_pending()
            if state.pending_count == 0 and not self._requires_budget_rewrite(
                state,
                policy,
            ):
                return self._nothing_pending()
        elif not self._requires_budget_rewrite(state, policy):
            return self._nothing_pending()
        runtime = await self._platform_runtime(
            session,
            create_document=state.document.sections_policy_version_id is None,
            policy_state=policy_state,
        )
        if runtime is None:
            return self._nothing_pending()
        return await self._admit_with_runtime(
            session,
            scope,
            trigger="auto_dream",
            now=now,
            runtime=runtime,
            account_private_generation=account_private_generation,
            budget_only=not require_due,
        )


class MemoryDreamSchedulerService:
    """Bound one Scheduler poll to a bounded set of due Dream admissions."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        admission: MemoryDreamAdmissionService | None = None,
        max_jobs_per_poll: int = 100,
        audit=None,
    ) -> None:
        if not callable(session_factory) or type(max_jobs_per_poll) is not int or not 1 <= max_jobs_per_poll <= 100:
            raise ValueError("Dream Scheduler configuration is invalid")
        if audit is not None and not callable(getattr(audit, "memory_dream_admitted", None)):
            raise ValueError("Dream Scheduler audit port is invalid")
        self._sessions = session_factory
        self._admission = admission or MemoryDreamAdmissionService()
        self._max_jobs_per_poll = max_jobs_per_poll
        self._audit = audit

    async def admit_due(
        self,
        *,
        now: datetime,
    ) -> int:
        async with self._sessions() as session, session.begin():
            scopes = await self._admission.list_due_scopes(
                session,
                now=now,
                max_jobs=self._max_jobs_per_poll,
            )
        seen = set(scopes)
        admitted = 0
        for scope in scopes:
            if await self._admit_scope(scope, now=now, require_due=True):
                admitted += 1
                if admitted == self._max_jobs_per_poll:
                    return admitted

        cursor: MemoryBudgetRewriteScanCursor | None = None
        while admitted < self._max_jobs_per_poll:
            async with self._sessions() as session, session.begin():
                # Discovery width is independent of the remaining success
                # budget: rejected candidates must not shrink scan progress.
                page = await self._admission.list_budget_rewrite_scope_page(
                    session,
                    cursor=cursor,
                    page_size=_BUDGET_REWRITE_DISCOVERY_PAGE_SIZE,
                )
            for scope in page.scopes:
                if scope in seen:
                    continue
                seen.add(scope)
                if await self._admit_scope(
                    scope,
                    now=now,
                    require_due=False,
                ):
                    admitted += 1
                    if admitted == self._max_jobs_per_poll:
                        return admitted
            if page.next_cursor is None:
                break
            if page.next_cursor == cursor:
                raise RuntimeError("Budget rewrite discovery cursor did not advance")
            cursor = page.next_cursor
        return admitted

    async def _admit_scope(
        self,
        scope: MemoryDocumentScope,
        *,
        now: datetime,
        require_due: bool,
    ) -> bool:
        try:
            async with self._sessions() as session, session.begin():
                result = await self._admission.admit_scheduled_scope(
                    session,
                    scope,
                    now=now,
                    require_due=require_due,
                )
                if result.disposition == "queued" and self._audit is not None:
                    if result.job_id is None:
                        raise RuntimeError("Dream admission returned no Job")
                    await self._audit.memory_dream_admitted(
                        session,
                        project_id=scope.project_id,
                        job_id=result.job_id,
                        request_id=_SCHEDULER_REQUEST_ID,
                        origin="scheduled",
                        trigger=("budget_rewrite" if result.admission_kind == "budget_rewrite" else "auto_dream"),
                        history_count=result.history_count,
                    )
        except MemoryDreamModelUnavailable:
            raise
        except (
            AccountPersonalizationNotFound,
            ProjectForbidden,
            ProjectNotFound,
            ValueError,
        ):
            return False
        except Exception as error:  # noqa: BLE001 - isolate owner scopes
            logger.error(
                "Memory Dream admission failed: error_type=%s",
                type(error).__name__,
            )
            return False
        return result.disposition == "queued"


__all__ = [
    "MemoryDreamAdmissionService",
    "MemoryDreamModelUnavailable",
    "MemoryDreamSchedulerService",
]
