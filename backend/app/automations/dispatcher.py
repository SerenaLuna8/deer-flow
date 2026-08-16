from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.automations.errors import (
    AutomationActiveRun,
    AutomationConcurrencyLimit,
    AutomationConflict,
    AutomationError,
    AutomationForbidden,
    AutomationInvalid,
    AutomationNotFound,
    AutomationUnavailable,
    AutomationVersionConflict,
)
from app.automations.execution_authority import (
    automation_retry_denial,
    lock_automation_execution_authority,
)
from app.automations.occurrences import (
    _AUTOMATION_ADMISSION_LOCK,
    deterministic_run_id,
    deterministic_thread_id,
    hash_manual_idempotency,
    manual_occurrence_key,
    scheduled_occurrence_key,
)
from app.automations.settlement import (
    ACTIVE_PRIVATE_RUN_STATUSES,
    automation_outcome_for_private_run,
    settle_created_terminal_occurrence,
    settle_terminal_occurrence,
)
from app.automations.system_policy import (
    AutomationsPolicyPort,
    AutomationsPolicyUnavailable,
    current_automations_policy,
)
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import (
    PrivateWorkAssetStale,
    PrivateWorkConflict,
    PrivateWorkError,
    PrivateWorkForbidden,
    PrivateWorkNotFound,
    PrivateWorkRunQuotaExceeded,
    PrivateWorkUnavailable,
)
from app.private_work.revalidation import PrivateWorkRevalidator
from app.private_work.run_metadata import strip_server_run_metadata
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRecord, PrivateRunRepository
from app.private_work.runtime_context import prepare_private_run_config
from app.private_work.snapshot_repository import (
    RunModelSnapshotAdmissionPort,
    RunRuntimePolicyAdmissionPort,
    RunSnapshotAssetStale,
    RunSnapshotRepository,
)
from app.private_work.thread_repository import (
    PrivateThreadRecord,
    PrivateThreadRepository,
    ThreadAgentRef,
)
from app.private_work.thread_service import PrivateThreadService
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext, resolve_project_context_in_transaction
from app.projects.errors import (
    ProjectDatabaseUnavailable,
    ProjectForbidden,
    ProjectNotFound,
)
from app.reliability.jobs import (
    AdmittedJobRecord,
    AutomationRunJobRepository,
    JobScope,
)
from app.shared_assets.errors import (
    AssetForbidden,
    AssetResolutionUnavailable,
    AssetStorageUnavailable,
    AssetValidationFailed,
)
from app.shared_assets.model_refs import ModelRefResolver
from app.shared_assets.models import (
    AssetKind,
    AssetSelection,
    ResolvedRunAssetClosure,
)
from app.shared_assets.resolver import ProjectAssetResolver
from app.system_runtime_settings import AutomationsPolicyValue
from deerflow.mcp_definition_policy import McpEndpointPolicy
from deerflow.persistence.scheduled_task_runs import (
    ScheduledTaskRunCreate,
    ScheduledTaskRunRecord,
    ScheduledTaskRunRepository,
    ScheduledTaskRunRow,
)
from deerflow.persistence.scheduled_tasks import (
    ScheduledTaskRecord,
    ScheduledTaskRepository,
    ScheduledTaskRow,
)
from deerflow.runtime import RunRecord
from deerflow.runtime.private_scope import PrivateResourceScope
from deerflow.scheduler.schedules import next_scheduled_occurrence

_DISPATCH_REQUEST_ID = "automation-dispatch"
_OCCURRENCE_NAMESPACE = uuid.UUID("54f6732f-c6d5-5db6-8d4e-f166b4f3d014")


class AutomationQuotaPort(Protocol):
    async def reserve_concurrent_run(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        run: PrivateRunRecord,
    ) -> None: ...


class AutomationAuditPort(Protocol):
    async def automation_admitted(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        task_id: str,
        trigger: str,
        run: PrivateRunRecord,
        job: AdmittedJobRecord,
    ) -> None: ...


class _NoopAutomationQuota:
    async def reserve_concurrent_run(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        run: PrivateRunRecord,
    ) -> None:
        del session, context, run


class _NoopAutomationAudit:
    async def automation_admitted(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        task_id: str,
        trigger: str,
        run: PrivateRunRecord,
        job: AdmittedJobRecord,
    ) -> None:
        del session, context, task_id, trigger, run, job


@dataclass(frozen=True, slots=True)
class AutomationDefinitionRef:
    project_id: uuid.UUID
    owner_user_id: str
    task_id: str
    membership_version: int

    def __post_init__(self) -> None:
        try:
            project_id = uuid.UUID(str(self.project_id))
            owner_user_id = str(uuid.UUID(self.owner_user_id))
        except (TypeError, ValueError):
            raise ValueError("definition authority is invalid") from None
        if not self.task_id or len(self.task_id) > 64:
            raise ValueError("definition task_id is invalid")
        if type(self.membership_version) is not int or self.membership_version < 1:
            raise ValueError("definition membership version is invalid")
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "owner_user_id", owner_user_id)

    @property
    def scope(self) -> PrivateResourceScope:
        return PrivateResourceScope(
            project_id=str(self.project_id),
            owner_user_id=self.owner_user_id,
            membership_version=self.membership_version,
        )


@dataclass(frozen=True, slots=True)
class AdmittedAutomationOccurrence:
    occurrence: ScheduledTaskRunRecord
    run: PrivateRunRecord
    job: AdmittedJobRecord
    created: bool


@dataclass(frozen=True, slots=True)
class SkippedAutomationOccurrence:
    occurrence: ScheduledTaskRunRecord
    created: bool


@dataclass(frozen=True, slots=True)
class AutomationDispatchResult:
    occurrence_id: str
    thread_id: str
    run_id: str


@dataclass(frozen=True, slots=True)
class _DispatchCoordinates:
    occurrence_id: str
    project_id: uuid.UUID
    owner_user_id: str
    task_id: str
    trigger: str
    context_mode: str
    reuse_thread_id: str | None

    @property
    def scope(self) -> PrivateResourceScope:
        # Repository coordinates are database-derived here; the scheduler has
        # no client-issued scope. The current membership version is resolved
        # before any runtime side effect.
        return PrivateResourceScope(
            project_id=str(self.project_id),
            owner_user_id=self.owner_user_id,
            membership_version=1,
        )

    @property
    def expected_thread_id(self) -> str:
        if self.context_mode == "reuse_thread" and self.reuse_thread_id:
            return self.reuse_thread_id
        return deterministic_thread_id(self.occurrence_id)

    @property
    def expected_run_id(self) -> str:
        return deterministic_run_id(self.occurrence_id)

    @property
    def run_metadata(self) -> dict[str, object]:
        return {
            "scheduled_task_id": self.task_id,
            "scheduled_task_run_id": self.occurrence_id,
            "scheduled_trigger": self.trigger,
        }


@dataclass(frozen=True, slots=True)
class _PreparedDispatch:
    context: PrivateWorkContext
    task: ScheduledTaskRecord
    occurrence: ScheduledTaskRunRecord


async def ensure_automation_thread(
    context: PrivateWorkContext,
    task: ScheduledTaskRecord,
    occurrence: ScheduledTaskRunRecord,
    *,
    thread_service: PrivateThreadService,
) -> str:
    """Create/adopt a deterministic fresh Thread or revalidate a reuse Thread."""

    expected_agent = ThreadAgentRef(task.agent_asset_id, task.agent_scope)
    if task.context_mode == "reuse_thread":
        if not task.thread_id:
            raise AutomationConflict(context.request_id)
        existing = await thread_service.get(context, task.thread_id)
        if existing is None or not _thread_agent_matches(existing, expected_agent):
            raise AutomationConflict(context.request_id)
        return existing.thread_id

    if task.context_mode != "fresh_thread_per_run" or task.thread_id is not None:
        raise AutomationConflict(context.request_id)
    thread_id = deterministic_thread_id(occurrence.id)
    expected_metadata = {
        "scheduled_task_id": task.id,
        "scheduled_task_run_id": occurrence.id,
        "scheduled_trigger": occurrence.trigger,
    }
    existing = await thread_service.get(context, thread_id)
    if existing is None:
        try:
            existing = await thread_service.create(
                context,
                thread_id=thread_id,
                agent=expected_agent,
                display_name=task.title,
                metadata=expected_metadata,
            )
        except PrivateWorkConflict:
            # A retry or concurrent dispatcher may have committed the exact
            # deterministic Thread. Adopt only after a fresh scoped read.
            existing = await thread_service.get(context, thread_id)
    if existing is None or not _thread_agent_matches(existing, expected_agent) or existing.metadata != expected_metadata:
        raise AutomationConflict(context.request_id)
    return existing.thread_id


def _thread_agent_matches(
    thread: PrivateThreadRecord,
    expected: ThreadAgentRef,
) -> bool:
    return thread.agent_asset_id == expected.asset_id and thread.agent_scope == expected.scope


class AutomationDispatcher:
    """Atomically admit Automation work; retain M5 dispatch only for migration tests."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        thread_service: PrivateThreadService | None = None,
        launch_private_run: Callable[..., Awaitable[RunRecord]] | None = None,
        clock: Callable[[], datetime] | None = None,
        retry_delay: timedelta = timedelta(seconds=30),
        max_concurrent_runs: int = 3,
        policy_reader: AutomationsPolicyPort | None = None,
        model_ref_resolver: ModelRefResolver | None = None,
        model_catalog: RunModelSnapshotAdmissionPort | None = None,
        runtime_policy: RunRuntimePolicyAdmissionPort | None = None,
        endpoint_policy: McpEndpointPolicy | None = None,
        quota: AutomationQuotaPort | None = None,
        audit: AutomationAuditPort | None = None,
    ) -> None:
        if retry_delay <= timedelta(0):
            raise ValueError("retry_delay must be positive")
        if type(max_concurrent_runs) is not int or max_concurrent_runs < 1:
            raise ValueError("max_concurrent_runs must be positive")
        self._session_factory = session_factory
        self._thread_service = thread_service
        self._launch_private_run = launch_private_run
        self._clock = clock or (lambda: datetime.now(UTC))
        self._retry_delay = retry_delay
        self._fallback_policy = AutomationsPolicyValue(
            max_concurrent_runs=max_concurrent_runs,
        )
        self._policy_reader = policy_reader
        self._quota = quota or _NoopAutomationQuota()
        self._audit = audit or _NoopAutomationAudit()
        self._revalidator = PrivateWorkRevalidator()
        self._resolver = ProjectAssetResolver(session_factory)
        self._snapshots = RunSnapshotRepository(
            session_factory,
            model_ref_resolver=model_ref_resolver,
            model_catalog=model_catalog,
            runtime_policy=runtime_policy,
            endpoint_policy=endpoint_policy,
            audit=(audit if callable(getattr(audit, "memory_injection_skipped", None)) else None),
        )

    async def _max_concurrent_runs(
        self,
        session: AsyncSession,
        request_id: str,
    ) -> int:
        try:
            policy = await current_automations_policy(
                session,
                self._policy_reader,
                fallback=self._fallback_policy,
            )
        except AutomationsPolicyUnavailable as error:
            raise AutomationUnavailable(request_id) from error
        return policy.max_concurrent_runs

    @staticmethod
    def _occurrence_id(
        definition: AutomationDefinitionRef,
        occurrence_key: str,
    ) -> str:
        coordinate = f"{definition.project_id}:{definition.owner_user_id}:{definition.task_id}:{occurrence_key}"
        return str(uuid.uuid5(_OCCURRENCE_NAMESPACE, coordinate))

    @staticmethod
    def _next_run_at(
        task: ScheduledTaskRecord,
        reference_time: datetime,
    ) -> datetime | None:
        if task.schedule_type == "once":
            return None
        return next_scheduled_occurrence(
            task.schedule_type,
            task.schedule_spec,
            task.timezone,
            now=reference_time,
            coalesce=True,
        )

    @staticmethod
    def _private_runtime_config(
        context: PrivateWorkContext,
        *,
        thread_id: str,
        metadata: dict[str, object],
    ) -> dict[str, Any]:
        config = prepare_private_run_config(
            thread_id=thread_id,
            opaque_scope=context.resource_scope,
            request_config=None,
            metadata=metadata,
            body_context=None,
        )
        persisted_context = dict(config.get("context", {}))
        persisted_context.pop("private_scope", None)
        persisted_context["non_interactive"] = True
        configurable = dict(config.get("configurable", {}))
        configurable["checkpoint_ns"] = ""
        return {
            **config,
            "configurable": configurable,
            "context": persisted_context,
        }

    async def _existing_admission(
        self,
        session: AsyncSession,
        definition: AutomationDefinitionRef,
        occurrence: ScheduledTaskRunRecord,
    ) -> AdmittedAutomationOccurrence | SkippedAutomationOccurrence:
        if occurrence.status == "skipped" and occurrence.run_id is None and occurrence.thread_id is None and occurrence.job_id is None:
            return SkippedAutomationOccurrence(
                occurrence=occurrence,
                created=False,
            )
        if occurrence.run_id is None or occurrence.thread_id is None or occurrence.job_id is None:
            raise AutomationConflict(_DISPATCH_REQUEST_ID)
        job = await AutomationRunJobRepository(session).get(
            scope=JobScope(definition.project_id, definition.owner_user_id),
            run_id=occurrence.run_id,
            occurrence_id=occurrence.id,
            job_id=occurrence.job_id,
            lock=True,
        )
        run = await PrivateRunRepository(session).get(
            scope=definition.scope,
            run_id=occurrence.run_id,
            lock=True,
        )
        if run is None or job is None or run.thread_id != occurrence.thread_id or run.job_id != job.job_id or run.origin_trace_id != job.origin_trace_id:
            raise AutomationConflict(_DISPATCH_REQUEST_ID)
        return AdmittedAutomationOccurrence(
            occurrence=occurrence,
            run=run,
            job=job,
            created=False,
        )

    async def _automation_thread_in_session(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        task: ScheduledTaskRecord,
        occurrence: ScheduledTaskRunRecord,
    ) -> PrivateThreadRecord:
        threads = PrivateThreadRepository(session)
        expected_agent = ThreadAgentRef(task.agent_asset_id, task.agent_scope)
        if task.context_mode == "reuse_thread":
            if task.thread_id is None:
                raise AutomationConflict(context.request_id)
            thread = await threads.get(
                scope=context.resource_scope,
                thread_id=task.thread_id,
                lock=True,
            )
            if thread is None or not _thread_agent_matches(thread, expected_agent):
                raise AutomationConflict(context.request_id)
            return thread
        if task.context_mode != "fresh_thread_per_run" or task.thread_id is not None:
            raise AutomationConflict(context.request_id)
        metadata = {
            "scheduled_task_id": task.id,
            "scheduled_task_run_id": occurrence.id,
            "scheduled_trigger": occurrence.trigger,
        }
        return await threads.create(
            scope=context.resource_scope,
            thread_id=deterministic_thread_id(occurrence.id),
            agent=expected_agent,
            display_name=task.title,
            metadata=metadata,
        )

    async def _after_job_attached(
        self,
        _session: AsyncSession,
        _admission: AdmittedAutomationOccurrence,
    ) -> None:
        """Transactional extension point for quota/audit hooks."""

    async def admit_occurrence(
        self,
        definition: AutomationDefinitionRef,
        *,
        scheduled_for: datetime,
        manual_idempotency_key: uuid.UUID | None = None,
        _session: AsyncSession | None = None,
    ) -> AdmittedAutomationOccurrence | SkippedAutomationOccurrence:
        if type(definition) is not AutomationDefinitionRef:
            raise AutomationInvalid(_DISPATCH_REQUEST_ID)
        scheduled_for = self._validated_time(scheduled_for)
        trigger = "manual" if manual_idempotency_key is not None else "scheduled"
        if manual_idempotency_key is not None and type(manual_idempotency_key) is not uuid.UUID:
            raise AutomationInvalid(_DISPATCH_REQUEST_ID)
        manual_hash = hash_manual_idempotency(manual_idempotency_key) if manual_idempotency_key is not None else None
        occurrence_key = manual_occurrence_key(definition.task_id, manual_hash) if manual_hash is not None else scheduled_occurrence_key(definition.task_id, scheduled_for)
        occurrence_id = self._occurrence_id(definition, occurrence_key)
        try:
            admitted_at = self._now()
            async with self._admission_session(_session) as session:
                await session.execute(
                    sa.text("SELECT pg_advisory_xact_lock(:lock_key)"),
                    {"lock_key": _AUTOMATION_ADMISSION_LOCK},
                )
                project = await resolve_project_context_in_transaction(
                    session,
                    uuid.UUID(definition.owner_user_id),
                    definition.project_id,
                    _DISPATCH_REQUEST_ID,
                    lock=True,
                )
                project.require(Capability.AUTOMATION_MANAGE_OWN)
                project.require(Capability.PRIVATE_WORK_CREATE)
                project.require(Capability.SHARED_ASSETS_EXECUTE)
                context = PrivateWorkContext.from_project(project)
                tasks = ScheduledTaskRepository(session)
                task = await tasks.lock_for_automation_outcome(
                    context.resource_scope,
                    definition.task_id,
                )
                if task is None:
                    raise AutomationNotFound(context.request_id)
                occurrences = ScheduledTaskRunRepository(session)
                existing = await occurrences.get_by_occurrence_key(
                    context.resource_scope,
                    task.id,
                    occurrence_key,
                    lock=True,
                )
                if existing is not None:
                    return await self._existing_admission(
                        session,
                        definition,
                        existing,
                    )
                if task.frozen_at is not None or task.deleted_at is not None:
                    raise AutomationNotFound(context.request_id)
                allowed = task.status == "enabled" or (trigger == "manual" and task.status == "paused")
                if not allowed:
                    raise AutomationConflict(context.request_id)
                if trigger == "scheduled" and task.next_run_at != scheduled_for:
                    raise AutomationConflict(context.request_id)
                overlapping = await occurrences.has_active(
                    context.resource_scope,
                    task.id,
                )
                if task.context_mode == "reuse_thread":
                    if task.thread_id is None:
                        raise AutomationConflict(context.request_id)
                    thread = await PrivateThreadRepository(session).get(
                        scope=context.resource_scope,
                        thread_id=task.thread_id,
                        lock=True,
                    )
                    expected_agent = ThreadAgentRef(
                        task.agent_asset_id,
                        task.agent_scope,
                    )
                    if thread is None or not _thread_agent_matches(
                        thread,
                        expected_agent,
                    ):
                        raise AutomationConflict(context.request_id)
                    overlapping = overlapping or await PrivateRunRepository(
                        session,
                    ).has_conflicting_active_run(
                        scope=context.resource_scope,
                        thread_id=thread.thread_id,
                    )
                if overlapping:
                    if trigger == "manual":
                        raise AutomationActiveRun(context.request_id)
                    skipped = await occurrences.create(
                        context.resource_scope,
                        ScheduledTaskRunCreate(
                            occurrence_id=occurrence_id,
                            task_id=task.id,
                            task_version=task.version,
                            occurrence_key=occurrence_key,
                            manual_idempotency_hash=None,
                            scheduled_for=scheduled_for,
                            trigger="scheduled",
                            status="skipped",
                            error_code="AUTOMATION_OVERLAP_SKIPPED",
                            finished_at=admitted_at,
                            created_at=admitted_at,
                        ),
                    )
                    advanced = await tasks.advance_after_reservation(
                        context.resource_scope,
                        task.id,
                        expected_next_run_at=scheduled_for,
                        next_run_at=self._next_run_at(task, admitted_at),
                        updated_at=admitted_at,
                    )
                    if advanced is None:
                        raise AutomationConflict(context.request_id)
                    await settle_created_terminal_occurrence(
                        tasks,
                        context.resource_scope,
                        task,
                        skipped,
                        occurred_at=admitted_at,
                        request_id=context.request_id,
                    )
                    return SkippedAutomationOccurrence(
                        occurrence=skipped,
                        created=True,
                    )
                active_count = int(
                    await session.scalar(
                        sa.select(sa.func.count())
                        .select_from(ScheduledTaskRunRow)
                        .where(
                            ScheduledTaskRunRow.status.in_(
                                ("queued", "launching", "running"),
                            )
                        )
                    )
                    or 0
                )
                if active_count >= await self._max_concurrent_runs(
                    session,
                    context.request_id,
                ):
                    raise AutomationConcurrencyLimit(context.request_id)

                occurrence = await occurrences.create(
                    context.resource_scope,
                    ScheduledTaskRunCreate(
                        occurrence_id=occurrence_id,
                        task_id=task.id,
                        task_version=task.version,
                        occurrence_key=occurrence_key,
                        manual_idempotency_hash=manual_hash,
                        scheduled_for=scheduled_for,
                        trigger=trigger,
                        status="launching",
                        created_at=admitted_at,
                    ),
                )
                thread = await self._automation_thread_in_session(
                    session,
                    context,
                    task,
                    occurrence,
                )
                resolved = await self._resolver.resolve_run_asset_closure_in_session(
                    session,
                    project,
                    AssetSelection(AssetKind.AGENT, task.agent_asset_id),
                )
                if type(resolved) is not ResolvedRunAssetClosure or resolved.lead_agent.scope.value != task.agent_scope:
                    raise AutomationConflict(context.request_id)
                run_metadata = {
                    "scheduled_task_id": task.id,
                    "scheduled_task_run_id": occurrence.id,
                    "scheduled_trigger": trigger,
                }
                runtime_config = self._private_runtime_config(
                    context,
                    thread_id=thread.thread_id,
                    metadata=run_metadata,
                )
                run = await self._snapshots.create_run_with_snapshot_in_session(
                    session,
                    context,
                    thread.thread_id,
                    PrivateRunCreate(
                        run_id=deterministic_run_id(occurrence.id),
                        metadata=run_metadata,
                        kwargs={
                            "input": {"messages": [{"role": "user", "content": task.prompt}]},
                            "config": runtime_config,
                        },
                    ),
                    resolved,
                )
                job = await AutomationRunJobRepository(session).enqueue(
                    scope=JobScope(project.project_id, str(project.user_id)),
                    run_id=run.run_id,
                    occurrence_id=occurrence.id,
                    origin_trace_id=run.origin_trace_id,
                )
                run = await PrivateRunRepository(session).attach_job(
                    scope=context.resource_scope,
                    run_id=run.run_id,
                    job_id=job.job_id,
                )
                admitted_occurrence = await occurrences.mark_admitted(
                    context.resource_scope,
                    occurrence.id,
                    thread_id=thread.thread_id,
                    run_id=run.run_id,
                    job_id=job.job_id,
                    membership_id=project.membership_id,
                    membership_version=project.membership_version,
                    admitted_at=admitted_at,
                )
                if admitted_occurrence is None:
                    raise AutomationConflict(context.request_id)
                if trigger == "scheduled":
                    advanced = await tasks.advance_after_reservation(
                        context.resource_scope,
                        task.id,
                        expected_next_run_at=scheduled_for,
                        next_run_at=self._next_run_at(task, admitted_at),
                        updated_at=admitted_at,
                    )
                    if advanced is None:
                        raise AutomationConflict(context.request_id)
                result = AdmittedAutomationOccurrence(
                    occurrence=admitted_occurrence,
                    run=run,
                    job=job,
                    created=True,
                )
                await self._quota.reserve_concurrent_run(
                    session,
                    context,
                    run,
                )
                await self._audit.automation_admitted(
                    session,
                    context,
                    task_id=task.id,
                    trigger=trigger,
                    run=run,
                    job=job,
                )
                await self._after_job_attached(session, result)
                return result
        except Exception as error:
            raise self._map_error(error) from None

    async def admit_occurrence_in_session(
        self,
        session: AsyncSession,
        definition: AutomationDefinitionRef,
        *,
        scheduled_for: datetime,
    ) -> AdmittedAutomationOccurrence | SkippedAutomationOccurrence:
        """Admit scheduled work inside the caller-owned transaction."""

        return await self.admit_occurrence(
            definition,
            scheduled_for=scheduled_for,
            _session=session,
        )

    @asynccontextmanager
    async def _admission_session(self, session: AsyncSession | None):
        if session is not None:
            yield session
            return
        async with self._session_factory() as owned_session, owned_session.begin():
            yield owned_session

    async def admit_manual(
        self,
        context: PrivateWorkContext,
        task_id: str,
        idempotency_key: uuid.UUID,
        *,
        scheduled_for: datetime,
    ) -> AdmittedAutomationOccurrence:
        if type(context) is not PrivateWorkContext:
            raise AutomationInvalid(_DISPATCH_REQUEST_ID)
        return await self.admit_occurrence(
            AutomationDefinitionRef(
                project_id=context.project_id,
                owner_user_id=str(context.user_id),
                task_id=task_id,
                membership_version=context.membership_version,
            ),
            scheduled_for=scheduled_for,
            manual_idempotency_key=idempotency_key,
        )

    @staticmethod
    def _validated_time(value: datetime) -> datetime:
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise AutomationInvalid(_DISPATCH_REQUEST_ID)
        return value.astimezone(UTC)

    async def dispatch(
        self,
        occurrence_id: str,
        *,
        app: Any,
    ) -> AutomationDispatchResult:
        if self._thread_service is None or self._launch_private_run is None:
            raise AutomationUnavailable(_DISPATCH_REQUEST_ID)
        if not isinstance(occurrence_id, str) or not occurrence_id or len(occurrence_id) > 64:
            raise AutomationInvalid(_DISPATCH_REQUEST_ID)
        coordinates = await self._load_coordinates(occurrence_id)
        try:
            prepared = await self._prepare(coordinates)
            if prepared.occurrence.status == "running":
                return self._result_from_running(coordinates, prepared.occurrence)
            thread_id = await ensure_automation_thread(
                prepared.context,
                prepared.task,
                prepared.occurrence,
                thread_service=self._thread_service,
            )
            if thread_id != coordinates.expected_thread_id:
                raise AutomationConflict(prepared.context.request_id)

            # Thread creation has its own committed transaction. Re-resolve
            # current membership and definition state once more before M4 run
            # admission so a concurrent freeze/delete/version change cannot
            # cross that boundary on the earlier snapshot.
            prepared = await self._prepare(coordinates)

            existing = await self._get_private_run(
                prepared.context.resource_scope,
                coordinates.expected_run_id,
            )
            if existing is not None:
                self._require_matching_run(coordinates, existing)
                if existing.status not in ACTIVE_PRIVATE_RUN_STATUSES:
                    raise AutomationConflict(prepared.context.request_id)
                return await self._mark_running(
                    coordinates,
                    prepared,
                    existing,
                )

            try:
                record = await self._launch_private_run(
                    app=app,
                    context=prepared.context,
                    thread_id=thread_id,
                    run_id=coordinates.expected_run_id,
                    prompt=prepared.task.prompt,
                    metadata=coordinates.run_metadata,
                )
            except PrivateWorkConflict:
                # Another dispatcher may have crossed the same deterministic
                # precheck and won M4 admission. Adopt only that exact scoped
                # Run; unrelated admission conflicts continue to fail closed.
                raced = await self._get_private_run(
                    prepared.context.resource_scope,
                    coordinates.expected_run_id,
                )
                if raced is None:
                    raise
                self._require_matching_run(coordinates, raced)
                if raced.status not in ACTIVE_PRIVATE_RUN_STATUSES:
                    raise AutomationConflict(prepared.context.request_id) from None
                return await self._mark_running(coordinates, prepared, raced)
            if record.thread_id != thread_id or record.run_id != coordinates.expected_run_id:
                raise AutomationConflict(prepared.context.request_id)
            persisted = await self._get_private_run(
                prepared.context.resource_scope,
                coordinates.expected_run_id,
            )
            if persisted is None:
                raise AutomationUnavailable(prepared.context.request_id)
            self._require_matching_run(coordinates, persisted)
            return await self._mark_running(coordinates, prepared, persisted)
        except Exception as error:
            mapped = self._map_error(error)
            await self._settle_failure(coordinates, mapped)
            raise mapped from None

    async def _load_coordinates(
        self,
        occurrence_id: str,
    ) -> _DispatchCoordinates:
        try:
            async with self._session_factory() as session, session.begin():
                pair = (
                    await session.execute(
                        sa.select(ScheduledTaskRunRow, ScheduledTaskRow)
                        .join(
                            ScheduledTaskRow,
                            sa.and_(
                                ScheduledTaskRow.project_id == ScheduledTaskRunRow.project_id,
                                ScheduledTaskRow.owner_user_id == ScheduledTaskRunRow.owner_user_id,
                                ScheduledTaskRow.id == ScheduledTaskRunRow.task_id,
                            ),
                        )
                        .where(ScheduledTaskRunRow.id == occurrence_id)
                    )
                ).one_or_none()
        except (DBAPIError, SATimeoutError):
            raise AutomationUnavailable(_DISPATCH_REQUEST_ID) from None
        if pair is None:
            raise AutomationNotFound(_DISPATCH_REQUEST_ID)
        occurrence, task = pair
        return _DispatchCoordinates(
            occurrence_id=occurrence.id,
            project_id=occurrence.project_id,
            owner_user_id=occurrence.owner_user_id,
            task_id=task.id,
            trigger=occurrence.trigger,
            context_mode=task.context_mode,
            reuse_thread_id=task.thread_id,
        )

    async def _prepare(
        self,
        coordinates: _DispatchCoordinates,
    ) -> _PreparedDispatch:
        try:
            async with self._session_factory() as session, session.begin():
                project = await resolve_project_context_in_transaction(
                    session,
                    uuid.UUID(coordinates.owner_user_id),
                    coordinates.project_id,
                    _DISPATCH_REQUEST_ID,
                    lock=True,
                )
                if type(project) is not ProjectContext:
                    raise ProjectNotFound
                context = PrivateWorkContext.from_project(project)
                project.require(Capability.AUTOMATION_MANAGE_OWN)
                project.require(Capability.PRIVATE_WORK_CREATE)
                project.require(Capability.SHARED_ASSETS_EXECUTE)

                task = await ScheduledTaskRepository(session).lock_active(
                    context.resource_scope,
                    coordinates.task_id,
                )
                if task is None:
                    raise AutomationNotFound(context.request_id)
                occurrence_repository = ScheduledTaskRunRepository(session)
                occurrence = await occurrence_repository.get(
                    context.resource_scope,
                    coordinates.occurrence_id,
                    lock=True,
                )
                if occurrence is None or occurrence.task_id != task.id:
                    raise AutomationNotFound(context.request_id)
                if task.version != occurrence.task_version:
                    raise AutomationVersionConflict(context.request_id)
                allowed_status = task.status == "enabled" or (occurrence.trigger == "manual" and task.status == "paused")
                if not allowed_status:
                    raise AutomationConflict(context.request_id)
                if occurrence.status == "running":
                    self._result_from_running(coordinates, occurrence)
                    return _PreparedDispatch(context, task, occurrence)
                if occurrence.status != "launching" or occurrence.thread_id is not None or occurrence.run_id is not None:
                    raise AutomationConflict(context.request_id)
                resolved = await occurrence_repository.record_resolution(
                    context.resource_scope,
                    occurrence.id,
                    membership_id=project.membership_id,
                    membership_version=project.membership_version,
                    updated_at=self._now(),
                )
                if resolved is None:
                    raise AutomationConflict(context.request_id)
                return _PreparedDispatch(context, task, resolved)
        except Exception as error:
            raise self._map_error(error) from None

    async def _mark_running(
        self,
        coordinates: _DispatchCoordinates,
        prepared: _PreparedDispatch,
        run: PrivateRunRecord,
    ) -> AutomationDispatchResult:
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(
                    session,
                    prepared.context,
                    Capability.AUTOMATION_MANAGE_OWN,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.SHARED_ASSETS_EXECUTE,
                    lock=True,
                )
                task = await ScheduledTaskRepository(session).lock_active(
                    prepared.context.resource_scope,
                    coordinates.task_id,
                )
                if task is None:
                    raise AutomationNotFound(prepared.context.request_id)
                occurrence_repository = ScheduledTaskRunRepository(session)
                occurrence = await occurrence_repository.get(
                    prepared.context.resource_scope,
                    coordinates.occurrence_id,
                    lock=True,
                )
                if occurrence is None:
                    raise AutomationNotFound(prepared.context.request_id)
                if task.version != occurrence.task_version:
                    raise AutomationVersionConflict(prepared.context.request_id)
                allowed_status = task.status == "enabled" or (occurrence.trigger == "manual" and task.status == "paused")
                if not allowed_status:
                    raise AutomationConflict(prepared.context.request_id)
                locked_run = await PrivateRunRepository(session).get(
                    scope=prepared.context.resource_scope,
                    run_id=run.run_id,
                    lock=True,
                )
                if locked_run is None:
                    raise AutomationUnavailable(prepared.context.request_id)
                self._require_matching_run(coordinates, locked_run)
                if locked_run.status not in ACTIVE_PRIVATE_RUN_STATUSES:
                    raise AutomationConflict(prepared.context.request_id)
                marked = await occurrence_repository.mark_running(
                    prepared.context.resource_scope,
                    occurrence.id,
                    thread_id=locked_run.thread_id,
                    run_id=locked_run.run_id,
                    started_at=locked_run.created_at,
                    updated_at=self._now(),
                )
                if marked is None:
                    if occurrence.status == "running":
                        return self._result_from_running(coordinates, occurrence)
                    raise AutomationConflict(prepared.context.request_id)
                return AutomationDispatchResult(
                    occurrence_id=marked.id,
                    thread_id=locked_run.thread_id,
                    run_id=locked_run.run_id,
                )
        except Exception as error:
            raise self._map_error(error) from None

    async def _get_private_run(
        self,
        scope: PrivateResourceScope,
        run_id: str,
    ) -> PrivateRunRecord | None:
        try:
            async with self._session_factory() as session, session.begin():
                return await PrivateRunRepository(session).get(
                    scope=scope,
                    run_id=run_id,
                )
        except (DBAPIError, SATimeoutError):
            raise AutomationUnavailable(_DISPATCH_REQUEST_ID) from None

    async def _settle_failure(
        self,
        coordinates: _DispatchCoordinates,
        error: AutomationError,
    ) -> None:
        now = self._now()
        try:
            async with self._session_factory() as session, session.begin():
                authority = await lock_automation_execution_authority(
                    session,
                    coordinates.scope,
                )
                tasks = ScheduledTaskRepository(session)
                task = await tasks.lock_for_automation_outcome(
                    coordinates.scope,
                    coordinates.task_id,
                )
                if task is None:
                    raise AutomationUnavailable(error.request_id)
                occurrences = ScheduledTaskRunRepository(session)
                occurrence = await occurrences.get(
                    coordinates.scope,
                    coordinates.occurrence_id,
                    lock=True,
                )
                if occurrence is None or occurrence.status != "launching":
                    return
                runs = PrivateRunRepository(session)
                run = await runs.get(
                    scope=coordinates.scope,
                    run_id=coordinates.expected_run_id,
                    lock=True,
                )
                attach_run = False
                if run is not None:
                    try:
                        self._require_matching_run(coordinates, run)
                    except AutomationConflict:
                        attach_run = False
                    else:
                        attach_run = True
                if attach_run and run is not None:
                    outcome = automation_outcome_for_private_run(run)
                    if outcome is not None:
                        await settle_terminal_occurrence(
                            tasks,
                            occurrences,
                            coordinates.scope,
                            task,
                            occurrence,
                            status=outcome.occurrence_status,
                            error_code=outcome.error_code,
                            error_message=outcome.error_message,
                            finished_at=now,
                            request_id=error.request_id,
                            thread_id=run.thread_id,
                            run_id=run.run_id,
                        )
                        return
                    if run.status in ACTIVE_PRIVATE_RUN_STATUSES:
                        await occurrences.mark_running(
                            coordinates.scope,
                            coordinates.occurrence_id,
                            thread_id=run.thread_id,
                            run_id=run.run_id,
                            started_at=run.created_at,
                            updated_at=now,
                        )
                        return
                denial = automation_retry_denial(authority, task, occurrence)
                if run is None and isinstance(error, AutomationUnavailable):
                    if denial is not None:
                        await settle_terminal_occurrence(
                            tasks,
                            occurrences,
                            coordinates.scope,
                            task,
                            occurrence,
                            status=denial.occurrence_status,
                            error_code=denial.error_code,
                            error_message=None,
                            finished_at=now,
                            request_id=error.request_id,
                        )
                        return
                    await occurrences.requeue_launch(
                        coordinates.scope,
                        coordinates.occurrence_id,
                        next_attempt_at=now + self._retry_delay,
                        error_code=error.code,
                        updated_at=now,
                    )
                    return
                scheduled_reuse_overlap = (
                    run is None
                    and coordinates.trigger == "scheduled"
                    and coordinates.context_mode == "reuse_thread"
                    and coordinates.reuse_thread_id is not None
                    and await runs.has_conflicting_active_run(
                        scope=coordinates.scope,
                        thread_id=coordinates.reuse_thread_id,
                    )
                )
                await settle_terminal_occurrence(
                    tasks,
                    occurrences,
                    coordinates.scope,
                    task,
                    occurrence,
                    status="skipped" if scheduled_reuse_overlap else "rejected",
                    error_code=("AUTOMATION_OVERLAP_SKIPPED" if scheduled_reuse_overlap else error.code),
                    error_message=None,
                    finished_at=now,
                    request_id=error.request_id,
                    thread_id=run.thread_id if attach_run and run is not None else None,
                    run_id=run.run_id if attach_run and run is not None else None,
                )
        except (DBAPIError, SATimeoutError):
            raise AutomationUnavailable(error.request_id) from None

    @staticmethod
    def _require_matching_run(
        coordinates: _DispatchCoordinates,
        run: PrivateRunRecord,
    ) -> None:
        if run.thread_id != coordinates.expected_thread_id or run.run_id != coordinates.expected_run_id or strip_server_run_metadata(run.metadata) != strip_server_run_metadata(coordinates.run_metadata):
            raise AutomationConflict(_DISPATCH_REQUEST_ID)

    @staticmethod
    def _result_from_running(
        coordinates: _DispatchCoordinates,
        occurrence: ScheduledTaskRunRecord,
    ) -> AutomationDispatchResult:
        if occurrence.thread_id != coordinates.expected_thread_id or occurrence.run_id != coordinates.expected_run_id:
            raise AutomationConflict(_DISPATCH_REQUEST_ID)
        return AutomationDispatchResult(
            occurrence_id=occurrence.id,
            thread_id=occurrence.thread_id,
            run_id=occurrence.run_id,
        )

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise AutomationUnavailable(_DISPATCH_REQUEST_ID)
        return value.astimezone(UTC)

    @staticmethod
    def _map_error(error: Exception) -> AutomationError:
        if isinstance(error, AutomationError):
            return error
        if isinstance(error, (ProjectNotFound, PrivateWorkNotFound)):
            return AutomationNotFound(_DISPATCH_REQUEST_ID)
        if isinstance(
            error,
            (ProjectForbidden, PrivateWorkForbidden, AssetForbidden),
        ):
            return AutomationForbidden(_DISPATCH_REQUEST_ID)
        if isinstance(
            error,
            (
                PrivateWorkConflict,
                PrivateWorkAssetStale,
                RunSnapshotAssetStale,
                AssetValidationFailed,
            ),
        ):
            return AutomationConflict(_DISPATCH_REQUEST_ID)
        if isinstance(error, PrivateWorkRunQuotaExceeded):
            return AutomationConcurrencyLimit(_DISPATCH_REQUEST_ID)
        if isinstance(
            error,
            (
                ProjectDatabaseUnavailable,
                PrivateWorkUnavailable,
                DBAPIError,
                SATimeoutError,
                AssetResolutionUnavailable,
                AssetStorageUnavailable,
            ),
        ):
            return AutomationUnavailable(_DISPATCH_REQUEST_ID)
        if isinstance(error, PrivateWorkError):
            return AutomationUnavailable(_DISPATCH_REQUEST_ID)
        raise error


__all__ = [
    "AdmittedAutomationOccurrence",
    "AutomationDefinitionRef",
    "AutomationDispatchResult",
    "AutomationDispatcher",
    "SkippedAutomationOccurrence",
    "ensure_automation_thread",
]
