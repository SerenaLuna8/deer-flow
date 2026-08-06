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
from app.projects.capabilities import Capability
from app.projects.context import resolve_project_context_in_transaction
from app.projects.errors import ProjectForbidden, ProjectNotFound
from app.system_runtime_settings.materializer import SystemRuntimePolicyMaterializer
from app.system_runtime_settings.models import (
    AgentRuntimePolicyValue,
    RuntimePolicySection,
)
from app.system_settings.repository import SystemModelRepository
from deerflow.agents.memory.dream import (
    DREAM_PROMPT_VERSION,
    EMPTY_MEMORY_DOCUMENT,
)
from deerflow.persistence.jobs.sql import JobRepository
from deerflow.persistence.private_work.memory_document_repository import (
    MemoryDocumentRepository,
    MemoryDocumentScope,
    MemoryDreamAdmissionRecord,
    MemoryDreamFrozenRuntime,
    MemoryDreamTrigger,
)

_SCHEDULER_REQUEST_ID = "memory-dream-scheduler"
logger = logging.getLogger(__name__)


class MemoryDreamAdmissionService:
    """Freeze exact policy, model, preference and oldest history in one transaction."""

    def __init__(
        self,
        *,
        repository_builder=MemoryDocumentRepository,
        personalization_repository_builder=AccountPersonalizationRepository,
        job_repository_builder=JobRepository,
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
    ) -> tuple[AgentRuntimePolicyValue, int, object] | None:
        policy_state = await MemoryDreamAdmissionService._platform_policy(
            session,
            for_update=True,
        )
        if policy_state is None:
            return None
        policy, policy_revision = policy_state
        model = await SystemModelRepository(session).resolve_active_model(
            policy.memory.model_name,
            load_envelope=False,
        )
        if model is None:
            return None
        return policy, policy_revision, model

    async def admit(
        self,
        session: AsyncSession,
        scope: MemoryDocumentScope,
        *,
        trigger: MemoryDreamTrigger,
        now: datetime,
    ) -> MemoryDreamAdmissionRecord:
        runtime = await self._platform_runtime(session)
        if runtime is None:
            return self._nothing_pending()
        return await self._admit_with_runtime(
            session,
            scope,
            trigger=trigger,
            now=now,
            runtime=runtime,
        )

    async def _admit_with_runtime(
        self,
        session: AsyncSession,
        scope: MemoryDocumentScope,
        *,
        trigger: MemoryDreamTrigger,
        now: datetime,
        runtime: tuple[AgentRuntimePolicyValue, int, object],
    ) -> MemoryDreamAdmissionRecord:
        _policy, policy_revision, model = runtime
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
            model_config_id=model.model.id,
            model_version_id=model.version.id,
            model_payload_checksum=model.version.payload_checksum,
            prompt_version=DREAM_PROMPT_VERSION,
        )
        return await self._repository(session).admit_dream(
            scope,
            trigger=trigger,
            frozen=frozen,
            initial_content=EMPTY_MEMORY_DOCUMENT,
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

    async def admit_scheduled_scope(
        self,
        session: AsyncSession,
        scope: MemoryDocumentScope,
        *,
        now: datetime,
    ) -> MemoryDreamAdmissionRecord:
        context = await resolve_project_context_in_transaction(
            session,
            uuid.UUID(scope.owner_user_id),
            scope.project_id,
            _SCHEDULER_REQUEST_ID,
            lock=True,
        )
        context.require(Capability.PRIVATE_WORK_CREATE)
        runtime = await self._platform_runtime(session)
        if runtime is None:
            return self._nothing_pending()
        policy, _policy_revision, _model = runtime
        if not await self._repository(session).is_scope_due(
            scope,
            now=now,
            interval_minutes=policy.memory.dream_interval_minutes,
        ):
            return self._nothing_pending()
        return await self._admit_with_runtime(
            session,
            scope,
            trigger="auto_dream",
            now=now,
            runtime=runtime,
        )


class MemoryDreamSchedulerService:
    """Bound one Scheduler poll to a bounded set of due Dream admissions."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        admission: MemoryDreamAdmissionService | None = None,
        max_jobs_per_poll: int = 100,
    ) -> None:
        if not callable(session_factory) or type(max_jobs_per_poll) is not int or not 1 <= max_jobs_per_poll <= 100:
            raise ValueError("Dream Scheduler configuration is invalid")
        self._sessions = session_factory
        self._admission = admission or MemoryDreamAdmissionService()
        self._max_jobs_per_poll = max_jobs_per_poll

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

        admitted = 0
        for scope in scopes:
            try:
                async with self._sessions() as session, session.begin():
                    result = await self._admission.admit_scheduled_scope(
                        session,
                        scope,
                        now=now,
                    )
            except (
                AccountPersonalizationNotFound,
                ProjectForbidden,
                ProjectNotFound,
                ValueError,
            ):
                continue
            except Exception as error:  # noqa: BLE001 - isolate owner scopes
                logger.error(
                    "Memory Dream admission failed: error_type=%s",
                    type(error).__name__,
                )
                continue
            if result.disposition == "queued":
                admitted += 1
        return admitted


__all__ = [
    "MemoryDreamAdmissionService",
    "MemoryDreamSchedulerService",
]
