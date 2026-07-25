"""Lease-authorized private Run execution for the independent Worker."""

from __future__ import annotations

import asyncio
import copy
import logging
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Literal, Protocol

import sqlalchemy as sa
from langchain_core.messages import BaseMessage
from langchain_core.messages.utils import convert_to_messages
from langgraph.types import Command
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.private_work.asset_runtime import PrivateAssetRuntime
from app.private_work.authorization import PrivateRunAuthorizationBoundary
from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkMcpQuotaExceeded
from app.private_work.file_finalizer import PrivateFileFinalizer
from app.private_work.run_admission import (
    AdmittedPrivateRun,
    PersistedRunSnapshot,
)
from app.private_work.run_repository import (
    PrivateRunExecutionLeaseLost,
    PrivateRunRecord,
    PrivateRunRepository,
)
from app.private_work.sandbox_files import (
    PrivateFileRunScope,
    PrivateRunFileAuthority,
    PrivateSandboxFileProjection,
)
from app.private_work.snapshot_repository import RunSnapshotRepository
from app.private_work.thread_repository import PrivateThreadRepository
from app.projects.capabilities import Capability
from app.projects.context import resolve_project_context_in_transaction
from app.projects.errors import ProjectForbidden, ProjectNotFound
from app.reliability.jobs import (
    AdmittedJobRecord,
    automation_run_idempotency_key,
    private_run_idempotency_key,
)
from app.shared_assets.model_refs import resolve_model_ref
from app.worker.service import (
    JobLeaseAuthority,
    JobOutcome,
    JobSettlement,
    LeaseLost,
)
from deerflow.persistence.jobs.sql import (
    JobClaim,
    JobRepository,
    JobTerminalEvent,
    JobTerminalResult,
)
from deerflow.persistence.private_work.model import PrivateFileRow
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.scheduled_task_runs.model import ScheduledTaskRunRow
from deerflow.persistence.scheduled_tasks.model import ScheduledTaskRow
from deerflow.runtime import (
    DisconnectMode,
    RunContext,
    RunManager,
    RunStatus,
    run_agent,
)
from deerflow.runtime.events.models import (
    StoredStreamFrame,
    StreamFrame,
    StreamLeaseProof,
    StreamWriteAuthorizationRevoked,
    StreamWriteCancelled,
    StreamWriteLeaseLost,
)
from deerflow.runtime.events.store.db import DbRunEventStore
from deerflow.runtime.private_scope import PrivateResourceScope
from deerflow.runtime.user_context import (
    reset_current_user,
    reset_runtime_storage_user_id,
    set_current_user,
    set_runtime_storage_user_id,
)
from deerflow.sandbox.sandbox import AuthorizationRevoked
from deerflow.sandbox.sandbox_provider import RunScopedReadOnlyMount

_PUBLIC_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
logger = logging.getLogger(__name__)


class TransientExecutionError(RuntimeError):
    """A public-safe failure before an ambiguous external side effect."""

    def __init__(self, public_error_code: str) -> None:
        if _PUBLIC_ERROR_CODE.fullmatch(public_error_code) is None:
            raise ValueError("transient execution error requires a public code")
        self.public_error_code = public_error_code
        super().__init__(public_error_code)


class PermanentExecutionError(RuntimeError):
    """A deterministic public-safe failure that must not be retried."""

    def __init__(self, public_error_code: str) -> None:
        if _PUBLIC_ERROR_CODE.fullmatch(public_error_code) is None:
            raise ValueError("permanent execution error requires a public code")
        self.public_error_code = public_error_code
        super().__init__(public_error_code)


class AmbiguousExternalSideEffect(RuntimeError):
    """Execution may have crossed an external side-effect boundary."""


@dataclass(frozen=True, slots=True)
class AgentExecutionResult:
    status: Literal["succeeded", "cancelled", "failed"]
    public_error_code: str | None = None
    retryable: bool = False

    def __post_init__(self) -> None:
        JobOutcome(self.status, self.public_error_code)
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if self.status != "failed" and self.retryable:
            raise ValueError("terminal success/cancel outcomes cannot be retryable")

    @classmethod
    def succeeded(cls) -> AgentExecutionResult:
        return cls("succeeded")

    @classmethod
    def cancelled(cls) -> AgentExecutionResult:
        return cls("cancelled")

    @classmethod
    def failed(
        cls,
        public_error_code: str,
        *,
        retryable: bool = True,
    ) -> AgentExecutionResult:
        return cls("failed", public_error_code, retryable)


@dataclass(frozen=True, slots=True)
class PrivateRunExecution:
    context: PrivateWorkContext
    run: PrivateRunRecord
    snapshot: PersistedRunSnapshot
    checkpoint_namespace: str
    graph_input: object
    command: object | None
    config: dict[str, Any]
    interrupt_before: list[str] | Literal["*"] | None
    interrupt_after: list[str] | Literal["*"] | None
    stream_mode: list[str]
    stream_subgraphs: bool
    resume_from_checkpoint: bool = False


class PrivateRunExecutor(Protocol):
    async def execute(
        self,
        execution: PrivateRunExecution,
        authority: JobLeaseAuthority,
    ) -> AgentExecutionResult: ...


class PrivateRunExecutionQuotaPort(Protocol):
    async def release_concurrent_run(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        run_id: str,
        request_id: str,
    ) -> None: ...


class PrivateRunExecutionAuditPort(Protocol):
    async def run_terminal(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        run_id: str,
        job_id: uuid.UUID,
        job_type: str,
        status: str,
        public_error_code: str | None,
        request_id: str,
    ) -> None: ...

    async def job_terminalized(
        self,
        session: AsyncSession,
        event: JobTerminalEvent,
    ) -> None: ...


class _NoopPrivateRunExecutionQuota:
    async def release_concurrent_run(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        run_id: str,
        request_id: str,
    ) -> None:
        del session, scope, run_id, request_id


class _NoopPrivateRunExecutionAudit:
    async def run_terminal(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        run_id: str,
        job_id: uuid.UUID,
        job_type: str,
        status: str,
        public_error_code: str | None,
        request_id: str,
    ) -> None:
        del session, scope, run_id, job_id, job_type, status, public_error_code, request_id

    async def job_terminalized(
        self,
        session: AsyncSession,
        event: JobTerminalEvent,
    ) -> None:
        del session, event


class PrivateRunAgentQuotaPort(Protocol):
    async def consume_mcp_dispatch(
        self,
        context: PrivateWorkContext,
        *,
        dispatch_id: uuid.UUID,
    ) -> None: ...

    async def reserve_file(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        file_id: uuid.UUID,
        size: int,
    ) -> None: ...

    async def release_file(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        file_id: uuid.UUID,
        size: int,
        request_id: str,
    ) -> None: ...


class _NoopPrivateRunAgentQuota:
    async def consume_mcp_dispatch(
        self,
        context: PrivateWorkContext,
        *,
        dispatch_id: uuid.UUID,
    ) -> None:
        del context, dispatch_id

    async def reserve_file(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        file_id: uuid.UUID,
        size: int,
    ) -> None:
        del session, context, file_id, size

    async def release_file(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        file_id: uuid.UUID,
        size: int,
        request_id: str,
    ) -> None:
        del session, scope, file_id, size, request_id


class PrivateRunJobTerminalPort:
    """Atomically converge an unowned private job, Run, and staging files."""

    def __init__(
        self,
        *,
        quota: PrivateRunExecutionQuotaPort | None = None,
        audit: PrivateRunExecutionAuditPort | None = None,
    ) -> None:
        self._automation_reconciliation_pending = asyncio.Event()
        self._quota = quota or _NoopPrivateRunExecutionQuota()
        self._audit = audit or _NoopPrivateRunExecutionAudit()

    def take_automation_reconciliation_pending(self) -> bool:
        if not self._automation_reconciliation_pending.is_set():
            return False
        self._automation_reconciliation_pending.clear()
        return True

    def restore_automation_reconciliation_pending(self) -> None:
        self._automation_reconciliation_pending.set()

    async def job_terminalized(
        self,
        session: AsyncSession,
        event: JobTerminalEvent,
    ) -> JobTerminalResult:
        if event.job_type not in {"private_run", "automation_run"}:
            await self._audit.job_terminalized(session, event)
            return JobTerminalResult(run_terminal_published=False)
        if event.owner_user_id is None or event.run_id is None:
            raise RuntimeError("private job terminal authority is incomplete")
        project_exists = await session.scalar(sa.select(ProjectRow.id).where(ProjectRow.id == event.project_id).with_for_update(of=ProjectRow))
        membership_version = await session.scalar(
            sa.select(ProjectMembershipRow.version)
            .where(
                ProjectMembershipRow.project_id == event.project_id,
                ProjectMembershipRow.user_id == event.owner_user_id,
            )
            .with_for_update(of=ProjectMembershipRow)
        )
        if project_exists is None or membership_version is None:
            raise RuntimeError("private job terminal membership is missing")
        run_status = "interrupted" if event.status == "cancelled" else "error"
        run_error = event.cancel_reason if event.status == "cancelled" else event.public_error_code
        finalization_status = sa.case(
            (RunRow.finalization_status == "finalizing", "failed"),
            else_=RunRow.finalization_status,
        )
        terminalized = await session.execute(
            sa.update(RunRow)
            .where(
                RunRow.project_id == event.project_id,
                RunRow.owner_user_id == event.owner_user_id,
                RunRow.run_id == event.run_id,
                RunRow.job_id == event.job_id,
                RunRow.status.in_(("pending", "running")),
            )
            .values(
                status=run_status,
                error=run_error,
                execution_lease_token_hash=None,
                execution_lease_expires_at=None,
                execution_heartbeat_at=None,
                finalization_status=finalization_status,
                updated_at=event.occurred_at,
            )
        )
        if terminalized.rowcount == 1:
            scope = PrivateResourceScope(
                project_id=str(event.project_id),
                owner_user_id=event.owner_user_id,
                membership_version=membership_version,
            )
            await self._quota.release_concurrent_run(
                session,
                scope,
                run_id=event.run_id,
                request_id="worker-job-terminal",
            )
            await self._audit.run_terminal(
                session,
                scope,
                run_id=event.run_id,
                job_id=event.job_id,
                job_type=event.job_type,
                status=run_status,
                public_error_code=event.public_error_code,
                request_id="worker-job-terminal",
            )
        run_terminal_published = terminalized.rowcount == 1
        if event.job_type == "automation_run":
            if event.occurrence_id is None:
                raise RuntimeError("automation job terminal authority is incomplete")
            occurrence_status = "interrupted" if event.status == "cancelled" else "failed"
            error_code = event.cancel_reason if event.status == "cancelled" else event.public_error_code
            task_id = await session.scalar(
                sa.select(ScheduledTaskRunRow.task_id).where(
                    ScheduledTaskRunRow.id == event.occurrence_id,
                    ScheduledTaskRunRow.project_id == event.project_id,
                    ScheduledTaskRunRow.owner_user_id == event.owner_user_id,
                    ScheduledTaskRunRow.run_id == event.run_id,
                    ScheduledTaskRunRow.job_id == event.job_id,
                )
            )
            task = None
            if task_id is not None:
                task = (
                    await session.execute(
                        sa.select(ScheduledTaskRow)
                        .where(
                            ScheduledTaskRow.id == task_id,
                            ScheduledTaskRow.project_id == event.project_id,
                            ScheduledTaskRow.owner_user_id == event.owner_user_id,
                        )
                        .with_for_update(
                            of=ScheduledTaskRow,
                            skip_locked=True,
                        )
                    )
                ).scalar_one_or_none()
            occurrence = None
            if task is not None:
                occurrence = (
                    await session.execute(
                        sa.select(ScheduledTaskRunRow)
                        .where(
                            ScheduledTaskRunRow.id == event.occurrence_id,
                            ScheduledTaskRunRow.project_id == event.project_id,
                            ScheduledTaskRunRow.owner_user_id == event.owner_user_id,
                            ScheduledTaskRunRow.task_id == task.id,
                            ScheduledTaskRunRow.run_id == event.run_id,
                            ScheduledTaskRunRow.job_id == event.job_id,
                            ScheduledTaskRunRow.status.in_(
                                ("queued", "launching", "running"),
                            ),
                        )
                        .with_for_update(
                            of=ScheduledTaskRunRow,
                            skip_locked=True,
                        )
                    )
                ).scalar_one_or_none()
            if task is not None and occurrence is not None:
                occurrence.status = occurrence_status
                occurrence.error_code = error_code
                occurrence.error_message = None
                occurrence.finished_at = event.occurred_at
                occurrence.updated_at = event.occurred_at
                if task.schedule_type == "once":
                    task.status = "cancelled" if occurrence_status == "interrupted" else "failed"
                    task.next_run_at = None
                task.last_run_at = event.occurred_at
                task.last_outcome = occurrence_status
                task.last_error_code = error_code
                task.run_count += 1
                task.updated_at = event.occurred_at
            else:
                self._automation_reconciliation_pending.set()
        await session.execute(
            sa.delete(PrivateFileRow).where(
                PrivateFileRow.project_id == event.project_id,
                PrivateFileRow.owner_user_id == event.owner_user_id,
                PrivateFileRow.created_by_run_id == event.run_id,
                PrivateFileRow.status == "staging",
            )
        )
        await self._audit.job_terminalized(session, event)
        return JobTerminalResult(
            run_terminal_published=run_terminal_published,
        )


class LeaseAuthorizedStreamBridge:
    """Guard every Worker-side stream mutation with current Run authority."""

    def __init__(
        self,
        bridge: Any,
        boundary: PrivateRunExecutionBoundary,
        *,
        scope: PrivateResourceScope | None = None,
        thread_id: str | None = None,
        terminal_status: Callable[[], str] | None = None,
    ) -> None:
        self._bridge = bridge
        self._boundary = boundary
        self._scope = scope
        self._thread_id = thread_id
        self._terminal_status = terminal_status

    @property
    def supports_cross_process(self) -> bool:
        return bool(getattr(self._bridge, "supports_cross_process", False))

    async def publish(self, run_id: str, event: str, data: Any) -> None:
        publish_frame = getattr(self._bridge, "publish_frame", None)
        if callable(publish_frame) and self._scope is not None and self._thread_id is not None:
            try:
                await publish_frame(
                    self._scope,
                    self._thread_id,
                    run_id,
                    StreamFrame(event=event, data=data),
                    lease=self._boundary.stream_lease_proof(),
                )
            except StreamWriteAuthorizationRevoked:
                self._boundary.record_stream_authorization_revoked()
                raise AuthorizationRevoked from None
            except StreamWriteLeaseLost:
                self._boundary.record_stream_lease_lost()
                raise AuthorizationRevoked from None
            except StreamWriteCancelled:
                self._boundary.request_local_cancel()
                raise AuthorizationRevoked from None
            return
        await self._boundary.before_stream_publish()
        await self._bridge.publish(run_id, event, data)

    async def publish_end(self, run_id: str) -> None:
        publish_terminal = getattr(self._bridge, "publish_terminal", None)
        if callable(publish_terminal) and self._scope is not None and self._thread_id is not None:
            try:
                stored = await publish_terminal(
                    self._scope,
                    self._thread_id,
                    run_id,
                    status=(self._terminal_status() if self._terminal_status is not None else "completed"),
                    lease=self._boundary.stream_lease_proof(),
                )
            except StreamWriteAuthorizationRevoked:
                self._boundary.record_stream_authorization_revoked()
                raise AuthorizationRevoked from None
            except StreamWriteLeaseLost:
                self._boundary.record_stream_lease_lost()
                raise AuthorizationRevoked from None
            if isinstance(stored, StoredStreamFrame) and isinstance(stored.data, Mapping) and stored.data.get("status") in {"cancelled", "interrupted"}:
                self._boundary.request_local_cancel()
            return
        await self._boundary.before_stream_terminal()
        await self._bridge.publish_end(run_id)

    def subscribe(self, *args, **kwargs):
        return self._bridge.subscribe(*args, **kwargs)

    async def cleanup(self, run_id: str, *, delay: float = 0) -> None:
        if delay > 0:
            await asyncio.sleep(delay)
        if await self._boundary.stream_cleanup_allowed():
            await self._bridge.cleanup(run_id, delay=0)


class PrivateRunExecutionBoundary:
    """Combine member authorization with the current job/run lease proof."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        context: PrivateWorkContext,
        claim: JobClaim,
        quota: PrivateRunAgentQuotaPort | None = None,
    ) -> None:
        if claim.run_id is None:
            raise ValueError("private execution claim requires a Run")
        self._factory = session_factory
        self._context = context
        self._claim = claim
        self._quota = quota or _NoopPrivateRunAgentQuota()
        self._authorization = PrivateRunAuthorizationBoundary(
            session_factory,
            project_id=context.project_id,
            owner_user_id=str(context.user_id),
            run_id=claim.run_id,
        )
        self._abort_event: asyncio.Event | None = None
        self._lease_lost = False
        self._authorization_revoked = False
        self._cancel_requested = False
        self._ambiguous_side_effect = False

    @property
    def execution_job_id(self) -> uuid.UUID:
        return self._claim.job_id

    @property
    def lease_lost(self) -> bool:
        return self._lease_lost

    @property
    def authorization_revoked(self) -> bool:
        return self._authorization_revoked

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_requested

    @property
    def ambiguous_side_effect(self) -> bool:
        return self._ambiguous_side_effect

    def bind_abort_event(self, abort_event: asyncio.Event) -> None:
        if self._abort_event is not None and self._abort_event is not abort_event:
            raise RuntimeError("execution boundary abort event is already bound")
        self._abort_event = abort_event
        self._authorization.bind_abort_event(abort_event)

    def request_local_cancel(self) -> None:
        self._cancel_requested = True
        if self._abort_event is not None:
            self._abort_event.set()

    def stream_lease_proof(self) -> StreamLeaseProof:
        return StreamLeaseProof(
            job_id=self._claim.job_id,
            lease_token=self._claim.lease_token,
        )

    def record_stream_lease_lost(self) -> None:
        self._lease_lost = True
        if self._abort_event is not None:
            self._abort_event.set()

    def record_stream_authorization_revoked(self) -> None:
        self._authorization_revoked = True
        if self._abort_event is not None:
            self._abort_event.set()

    async def _check(
        self,
        authorization_method: str,
        *,
        ambiguous_side_effect: bool = False,
        allow_cancel: bool = False,
    ) -> None:
        try:
            await getattr(self._authorization, authorization_method)()
        except AuthorizationRevoked:
            self._authorization_revoked = True
            if self._abort_event is not None:
                self._abort_event.set()
            raise
        try:
            async with self._factory() as session, session.begin():
                repository = PrivateRunRepository(session)
                if ambiguous_side_effect:
                    cancel_requested = await repository.mark_execution_side_effect_unknown(
                        scope=self._context.resource_scope,
                        run_id=self._claim.run_id or "",
                        job_id=self._claim.job_id,
                        lease_token=self._claim.lease_token,
                    )
                else:
                    cancel_requested = await repository.assert_execution_active(
                        scope=self._context.resource_scope,
                        run_id=self._claim.run_id or "",
                        job_id=self._claim.job_id,
                        lease_token=self._claim.lease_token,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._lease_lost = True
            if self._abort_event is not None:
                self._abort_event.set()
            raise AuthorizationRevoked from None
        if cancel_requested:
            self.request_local_cancel()
            if not allow_cancel:
                raise AuthorizationRevoked
        if ambiguous_side_effect:
            self._ambiguous_side_effect = True

    async def before_model_call(self) -> None:
        await self._check("before_model_call")

    async def before_tool_call(self) -> None:
        await self._check(
            "before_tool_call",
            ambiguous_side_effect=True,
        )

    async def before_mcp_call(self) -> None:
        # Discovery/materialization is read-only.  The exact remote dispatch
        # hook below owns both quota consumption and the retry-safety fence.
        await self._check("before_mcp_call")

    async def before_mcp_tool_dispatch(self) -> None:
        await self._check("before_mcp_call")
        await self._quota.consume_mcp_dispatch(
            self._context,
            dispatch_id=uuid.uuid4(),
        )
        # Quota rejection happens before this durable unknown-side-effect
        # marker, so it remains a stable, retry-safe public failure.  Once the
        # marker commits the caller immediately invokes the remote MCP tool.
        await self._check(
            "before_mcp_call",
            ambiguous_side_effect=True,
        )

    async def before_sandbox_write(self) -> None:
        await self._check(
            "before_sandbox_write",
            ambiguous_side_effect=True,
        )

    async def before_sandbox_exec(self) -> None:
        await self._check(
            "before_sandbox_exec",
            ambiguous_side_effect=True,
        )

    async def before_sandbox_restore(self) -> None:
        # Restoring a deterministic private snapshot into an ephemeral
        # sandbox is retry-safe, but sandbox acquisition still requires the
        # current durable execution token.
        await self._check("before_sandbox_write")

    async def before_checkpoint_read(self) -> None:
        await self._check("before_checkpoint_read")

    async def before_checkpoint_write(self) -> None:
        await self._check("before_checkpoint_write")

    async def before_stream_publish(self) -> None:
        await self._check("before_checkpoint_write")

    async def before_stream_terminal(self) -> None:
        await self._check(
            "before_checkpoint_write",
            allow_cancel=True,
        )

    async def stream_cleanup_allowed(self) -> bool:
        try:
            async with self._factory() as session, session.begin():
                return await PrivateRunRepository(session).stream_cleanup_allowed(
                    scope=self._context.resource_scope,
                    run_id=self._claim.run_id or "",
                    job_id=self._claim.job_id,
                )
        except Exception:
            return False

    async def before_file_finalization(self) -> None:
        await self._check(
            "before_file_finalization",
            ambiguous_side_effect=True,
        )

    async def before_file_finalization_in_session(
        self,
        session: AsyncSession,
    ) -> None:
        """Validate file/Run mutation authority in its owning transaction."""

        try:
            cancel_requested = await PrivateRunRepository(
                session,
            ).mark_execution_side_effect_unknown(
                scope=self._context.resource_scope,
                run_id=self._claim.run_id or "",
                job_id=self._claim.job_id,
                lease_token=self._claim.lease_token,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._lease_lost = True
            if self._abort_event is not None:
                self._abort_event.set()
            raise AuthorizationRevoked from None
        if cancel_requested:
            self.request_local_cancel()
            raise AuthorizationRevoked
        self._ambiguous_side_effect = True


class _PrivateRunThreadMetadataStore:
    """Persist the first completed title inside the exact private scope."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        scope: PrivateResourceScope,
        boundary: PrivateRunExecutionBoundary,
    ) -> None:
        self._factory = session_factory
        self._scope = scope
        self._boundary = boundary

    async def update_display_name(
        self,
        thread_id: str,
        display_name: str,
    ) -> None:
        await self._boundary.before_checkpoint_write()
        async with self._factory() as session, session.begin():
            await PrivateThreadRepository(
                session,
            ).set_automatic_display_name(
                scope=self._scope,
                thread_id=thread_id,
                display_name=display_name,
            )

    async def update_status(self, thread_id: str, status: str) -> None:
        # Private Run admission/settlement owns status. The harness invokes this
        # compatibility hook after title sync, so it intentionally stays a no-op.
        del thread_id, status


class RunAgentPrivateExecutor:
    """Production adapter that invokes ``run_agent`` only inside the Worker."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        app_config: Any,
        bridge: Any,
        project_checkpointer: ProjectScopedCheckpointer,
        store: Any,
        event_store: Any,
        asset_runtime: PrivateAssetRuntime | None = None,
        agent_factory: Any | None = None,
        runner=run_agent,
        quota: PrivateRunAgentQuotaPort | None = None,
    ) -> None:
        self._factory = session_factory
        self._app_config = app_config
        self._bridge = bridge
        self._project_checkpointer = project_checkpointer
        self._store = store
        self._event_store = event_store
        self._asset_runtime = asset_runtime or PrivateAssetRuntime(session_factory)
        self._agent_factory = agent_factory or self._default_agent_factory()
        self._runner = runner
        self._quota = quota or _NoopPrivateRunAgentQuota()

    @staticmethod
    def _default_agent_factory():
        from deerflow.agents.lead_agent.agent import make_lead_agent

        return make_lead_agent

    @staticmethod
    def _graph_input(execution: PrivateRunExecution) -> object:
        if execution.resume_from_checkpoint:
            return None
        if execution.command is not None:
            if not isinstance(execution.command, Mapping):
                raise TransientExecutionError("INVALID_RUN_PAYLOAD")
            try:
                return Command(**dict(execution.command))
            except (TypeError, ValueError):
                raise TransientExecutionError("INVALID_RUN_PAYLOAD") from None
        if not isinstance(execution.graph_input, Mapping):
            if execution.graph_input is None:
                return {}
            raise TransientExecutionError("INVALID_RUN_PAYLOAD")
        graph_input = copy.deepcopy(dict(execution.graph_input))
        messages = graph_input.get("messages")
        if isinstance(messages, list):
            converted: list[object] = []
            for message in messages:
                if isinstance(message, BaseMessage):
                    converted.append(message)
                    continue
                if not isinstance(message, Mapping):
                    converted.append(message)
                    continue
                try:
                    converted.extend(convert_to_messages([dict(message)]))
                except (TypeError, ValueError, NotImplementedError):
                    raise TransientExecutionError("INVALID_RUN_PAYLOAD") from None
            graph_input["messages"] = converted
        return graph_input

    @staticmethod
    def _admitted(
        execution: PrivateRunExecution,
        claim: JobClaim,
    ) -> AdmittedPrivateRun:
        return AdmittedPrivateRun(
            run=execution.run,
            job=AdmittedJobRecord(
                job_id=claim.job_id,
                job_type=claim.job_type,
                project_id=execution.run.project_id,
                owner_user_id=execution.run.owner_user_id,
                run_id=execution.run.run_id,
                idempotency_key=(automation_run_idempotency_key(claim.occurrence_id) if claim.job_type == "automation_run" and claim.occurrence_id is not None else private_run_idempotency_key(execution.run.run_id)),
                status="running",
            ),
            snapshot=execution.snapshot,
            opaque_runtime_scope=execution.context.resource_scope,
        )

    async def execute(
        self,
        execution: PrivateRunExecution,
        authority: JobLeaseAuthority,
    ) -> AgentExecutionResult:
        claim = authority.claim
        boundary = PrivateRunExecutionBoundary(
            self._factory,
            context=execution.context,
            claim=claim,
            quota=self._quota,
        )
        admitted = self._admitted(execution, claim)
        private_runtime = None
        file_authority = None
        try:
            exact_model_name = execution.run.model_name
            if exact_model_name is None or self._app_config.get_model_config(exact_model_name) is None:
                raise PermanentExecutionError("RUN_ASSET_STALE")
            private_runtime = await self._asset_runtime.materialize(
                execution.context,
                admitted,
                authorization_boundary=boundary,
            )
            resolved_runtime_model = resolve_model_ref(
                self._app_config,
                private_runtime.model_ref,
            )
            if getattr(resolved_runtime_model, "name", None) != exact_model_name:
                raise PermanentExecutionError("RUN_ASSET_STALE")

            skills_config = getattr(self._app_config, "skills", None)
            skill_container_path = getattr(
                skills_config,
                "container_path",
                None,
            )
            skill_root = getattr(private_runtime, "skill_root", None)
            mounts = (
                (
                    RunScopedReadOnlyMount(
                        run_id=execution.run.run_id,
                        container_path=skill_container_path,
                        host_path=str(skill_root),
                    ),
                )
                if isinstance(skill_container_path, str) and skill_root is not None
                else ()
            )
            file_authority = PrivateRunFileAuthority(
                PrivateFileRunScope(
                    execution.context,
                    thread_id=execution.run.thread_id,
                    run_id=execution.run.run_id,
                    authorization_boundary=boundary,
                ),
                PrivateSandboxFileProjection(self._factory),
                PrivateFileFinalizer(self._factory, quota=self._quota),
                mounts=mounts,
            )
            run_manager = RunManager()
            record = await run_manager.register_persisted(
                run_id=execution.run.run_id,
                thread_id=execution.run.thread_id,
                assistant_id=execution.run.assistant_id,
                on_disconnect=DisconnectMode.continue_,
                metadata=execution.run.metadata,
                kwargs=execution.run.kwargs,
                multitask_strategy=execution.run.multitask_strategy,
                model_name=exact_model_name,
                scope=execution.context.resource_scope,
                created_at=execution.run.created_at.isoformat(),
            )
            boundary.bind_abort_event(record.abort_event)
            authority.bind_cancel_callback(boundary.request_local_cancel)
            if authority.cancel_requested:
                boundary.request_local_cancel()

            checkpointer = self._project_checkpointer.for_context(
                execution.context,
            )
            set_boundary = getattr(
                checkpointer,
                "set_authorization_boundary",
                None,
            )
            if callable(set_boundary):
                set_boundary(boundary)
            run_context = RunContext(
                checkpointer=checkpointer,
                store=self._store,
                event_store=self._event_store,
                run_events_config=None,
                thread_store=_PrivateRunThreadMetadataStore(
                    self._factory,
                    scope=execution.context.resource_scope,
                    boundary=boundary,
                ),
                app_config=self._app_config,
                private_scope=execution.context.resource_scope,
                authorization_boundary=boundary,
                file_authority=file_authority,
                private_agent_runtime=private_runtime,
            )
            owner_token = set_current_user(
                SimpleNamespace(id=execution.run.owner_user_id),
            )
            storage_token = set_runtime_storage_user_id(
                execution.run.owner_user_id,
            )
            try:
                await self._runner(
                    LeaseAuthorizedStreamBridge(
                        self._bridge,
                        boundary,
                        scope=execution.context.resource_scope,
                        thread_id=execution.run.thread_id,
                        terminal_status=lambda: str(record.status),
                    ),
                    run_manager,
                    record,
                    ctx=run_context,
                    agent_factory=self._agent_factory,
                    graph_input=self._graph_input(execution),
                    config=copy.deepcopy(execution.config),
                    stream_modes=list(execution.stream_mode),
                    stream_subgraphs=execution.stream_subgraphs,
                    interrupt_before=execution.interrupt_before,
                    interrupt_after=execution.interrupt_after,
                )
            finally:
                reset_runtime_storage_user_id(storage_token)
                reset_current_user(owner_token)

            if boundary.lease_lost:
                raise TransientExecutionError(
                    "EXECUTION_AUTHORITY_UNAVAILABLE",
                )
            if boundary.cancel_requested or boundary.authorization_revoked:
                return AgentExecutionResult.cancelled()
            if record.status is RunStatus.success:
                return AgentExecutionResult.succeeded()
            if record.status is RunStatus.interrupted:
                return AgentExecutionResult.cancelled()
            if boundary.ambiguous_side_effect:
                raise AmbiguousExternalSideEffect
            return AgentExecutionResult.failed("AGENT_EXECUTION_FAILED")
        except asyncio.CancelledError:
            raise
        except (TransientExecutionError, AmbiguousExternalSideEffect):
            raise
        except PrivateWorkMcpQuotaExceeded as error:
            raise TransientExecutionError(error.code) from None
        except AuthorizationRevoked:
            if boundary.lease_lost:
                raise TransientExecutionError(
                    "EXECUTION_AUTHORITY_UNAVAILABLE",
                ) from None
            return AgentExecutionResult.cancelled()
        except Exception:
            if boundary.ambiguous_side_effect:
                raise AmbiguousExternalSideEffect from None
            raise TransientExecutionError(
                "PRIVATE_RUN_EXECUTION_FAILED",
            ) from None
        finally:
            if private_runtime is not None:
                try:
                    await private_runtime.aclose()
                except Exception:
                    logger.warning(
                        "Failed to clean private runtime for Run %s",
                        execution.run.run_id,
                        exc_info=True,
                    )
            if file_authority is not None:
                try:
                    await file_authority.release()
                except Exception:
                    logger.warning(
                        "Failed to release private file authority for Run %s",
                        execution.run.run_id,
                        exc_info=True,
                    )


class PrivateRunJobHandler:
    """The sole M6 adapter from a private_run Job claim to Agent execution."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        executor: PrivateRunExecutor,
        retry_initial_seconds: int = 2,
        retry_max_seconds: int = 300,
        job_repository_builder=JobRepository,
        project_checkpointer: ProjectScopedCheckpointer | None = None,
        quota: PrivateRunExecutionQuotaPort | None = None,
        audit: PrivateRunExecutionAuditPort | None = None,
    ) -> None:
        if retry_initial_seconds < 1 or retry_max_seconds < retry_initial_seconds:
            raise ValueError("invalid private Run retry policy")
        self._factory = session_factory
        self._executor = executor
        self._retry_initial_seconds = retry_initial_seconds
        self._retry_max_seconds = retry_max_seconds
        self._job_repository_builder = job_repository_builder
        self._project_checkpointer = project_checkpointer
        self._quota = quota or _NoopPrivateRunExecutionQuota()
        self._audit = audit or _NoopPrivateRunExecutionAudit()
        self._snapshots = RunSnapshotRepository(session_factory)
        self._events = DbRunEventStore(session_factory)

    def _runs(self, session: AsyncSession) -> PrivateRunRepository:
        return PrivateRunRepository(
            session,
            jobs=self._job_repository_builder(session),
        )

    @staticmethod
    async def _claim_scope(
        session: AsyncSession,
        claim: JobClaim,
    ) -> PrivateResourceScope:
        if claim.scope.owner_user_id is None:
            raise LeaseLost(claim.job_id)
        project_id = claim.scope.project_id
        project_exists = await session.scalar(sa.select(ProjectRow.id).where(ProjectRow.id == project_id).with_for_update(of=ProjectRow))
        membership_version = await session.scalar(
            sa.select(ProjectMembershipRow.version)
            .where(
                ProjectMembershipRow.project_id == project_id,
                ProjectMembershipRow.user_id == claim.scope.owner_user_id,
            )
            .with_for_update(of=ProjectMembershipRow)
        )
        if project_exists is None or membership_version is None:
            raise LeaseLost(claim.job_id)
        return PrivateResourceScope(
            project_id=str(project_id),
            owner_user_id=claim.scope.owner_user_id,
            membership_version=membership_version,
        )

    async def _begin(
        self,
        claim: JobClaim,
    ) -> tuple[
        PrivateRunExecution | None,
        bool,
        AgentExecutionResult | None,
        PrivateResourceScope,
    ]:
        if claim.job_type not in {"private_run", "automation_run"} or claim.run_id is None or claim.scope.owner_user_id is None or (claim.job_type == "automation_run" and claim.occurrence_id is None):
            raise LeaseLost(claim.job_id)
        try:
            owner_user_id = uuid.UUID(claim.scope.owner_user_id)
        except ValueError:
            raise LeaseLost(claim.job_id) from None
        async with self._factory() as session, session.begin():
            claim_scope = await self._claim_scope(session, claim)
            try:
                project = await resolve_project_context_in_transaction(
                    session,
                    owner_user_id,
                    claim.scope.project_id,
                    "worker-private-run",
                    lock=True,
                )
                project.require(Capability.PRIVATE_WORK_CREATE)
                project.require(Capability.SHARED_ASSETS_EXECUTE)
            except (ProjectNotFound, ProjectForbidden):
                state = await self._runs(session).begin_execution(
                    scope=claim_scope,
                    run_id=claim.run_id,
                    job_id=claim.job_id,
                    lease_token=claim.lease_token,
                )
                return None, True, None, claim_scope
            context = PrivateWorkContext.from_project(project)
            runs = self._runs(session)
            state = await runs.begin_execution(
                scope=context.resource_scope,
                run_id=claim.run_id,
                job_id=claim.job_id,
                lease_token=claim.lease_token,
            )
            terminal = await self._events.get_stream_terminal(
                session,
                scope=context.resource_scope,
                thread_id=state.run.thread_id,
                run_id=state.run.run_id,
            )
            if terminal is not None:
                return (
                    None,
                    state.cancel_requested,
                    self._terminal_result(terminal),
                    context.resource_scope,
                )
            assets = await self._snapshots.list_assets_in_session(
                session,
                context,
                state.run.run_id,
                lock=True,
            )
            grants = await self._snapshots.list_mcp_grants_in_session(
                session,
                context,
                state.run.run_id,
                lock=True,
            )
            generations = {asset.catalog_generation for asset in assets}
            if not assets or len(generations) != 1:
                raise TransientExecutionError("RUN_SNAPSHOT_UNAVAILABLE")
            snapshot = PersistedRunSnapshot(
                assets=assets,
                mcp_grants=grants,
                catalog_generation=generations.pop(),
            )
            resume_from_checkpoint = False
            if self._project_checkpointer is not None:
                saver = self._project_checkpointer.for_context(context)
                item = await saver.aget_tuple_already_authorized(
                    {
                        "configurable": {
                            "thread_id": state.run.thread_id,
                            "checkpoint_ns": "",
                        }
                    },
                    session=session,
                )
                latest_checkpoint_id = None
                if item is not None:
                    raw_configurable = item.config.get("configurable")
                    if isinstance(raw_configurable, Mapping):
                        raw_checkpoint_id = raw_configurable.get("checkpoint_id")
                        if isinstance(raw_checkpoint_id, str):
                            latest_checkpoint_id = raw_checkpoint_id
                resume_from_checkpoint = await runs.prepare_checkpoint_takeover(
                    scope=context.resource_scope,
                    run_id=claim.run_id,
                    job_id=claim.job_id,
                    attempt_id=claim.attempt_id,
                    lease_token=claim.lease_token,
                    latest_checkpoint_id=latest_checkpoint_id,
                )

        kwargs = state.run.kwargs
        raw_config = kwargs.get("config")
        config = copy.deepcopy(raw_config) if isinstance(raw_config, dict) else {}
        if resume_from_checkpoint:
            raw_configurable = config.get("configurable")
            configurable = dict(raw_configurable) if isinstance(raw_configurable, Mapping) else {}
            configurable.pop("checkpoint_id", None)
            configurable.pop("checkpoint_map", None)
            configurable["checkpoint_ns"] = ""
            config["configurable"] = configurable
        raw_stream_mode = kwargs.get("stream_mode")
        stream_mode = [str(value) for value in raw_stream_mode] if isinstance(raw_stream_mode, list) else ["values"]
        return (
            PrivateRunExecution(
                context=context,
                run=state.run,
                snapshot=snapshot,
                checkpoint_namespace=state.run.run_id,
                graph_input=(None if resume_from_checkpoint else kwargs.get("input")),
                command=(None if resume_from_checkpoint else kwargs.get("command")),
                config=config,
                interrupt_before=kwargs.get("interrupt_before"),
                interrupt_after=kwargs.get("interrupt_after"),
                stream_mode=stream_mode,
                stream_subgraphs=bool(kwargs.get("stream_subgraphs", False)),
                resume_from_checkpoint=resume_from_checkpoint,
            ),
            state.cancel_requested,
            None,
            context.resource_scope,
        )

    @staticmethod
    def _terminal_result(
        terminal: StoredStreamFrame,
    ) -> AgentExecutionResult:
        status = terminal.data.get("status") if isinstance(terminal.data, Mapping) else None
        if status in {"completed", "success"}:
            return AgentExecutionResult.succeeded()
        if status in {"cancelled", "interrupted"}:
            return AgentExecutionResult.cancelled()
        if status in {"error", "failed", "timeout"}:
            return AgentExecutionResult.failed("AGENT_EXECUTION_FAILED")
        return AgentExecutionResult.failed("DURABLE_STREAM_TERMINAL_INVALID")

    async def _heartbeat(
        self,
        claim: JobClaim,
        context: PrivateWorkContext,
    ) -> None:
        try:
            async with self._factory() as session, session.begin():
                await self._runs(session).heartbeat_execution(
                    scope=context.resource_scope,
                    run_id=claim.run_id or "",
                    job_id=claim.job_id,
                    lease_token=claim.lease_token,
                )
        except PrivateRunExecutionLeaseLost:
            raise LeaseLost(claim.job_id) from None

    def _settlement(
        self,
        claim: JobClaim,
        result: AgentExecutionResult,
        *,
        scope: PrivateResourceScope,
        ambiguous_side_effect: bool = False,
        durable_terminal: bool = False,
    ) -> JobSettlement:
        outcome = JobOutcome(result.status, result.public_error_code)

        async def commit() -> None:
            try:
                settled_run = None
                async with self._factory() as session, session.begin():
                    locked_scope = await self._claim_scope(session, claim)
                    if locked_scope.project_id != scope.project_id or locked_scope.owner_user_id != scope.owner_user_id:
                        raise LeaseLost(claim.job_id)
                    settlement = await self._runs(session).settle_execution(
                        scope=locked_scope,
                        run_id=claim.run_id or "",
                        job_id=claim.job_id,
                        lease_token=claim.lease_token,
                        outcome=result.status,
                        public_error_code=result.public_error_code,
                        ambiguous_side_effect=ambiguous_side_effect,
                        retryable_failure=(result.retryable and not durable_terminal),
                        cancel_preempts_outcome=not durable_terminal,
                        retry_initial_seconds=self._retry_initial_seconds,
                        retry_max_seconds=self._retry_max_seconds,
                    )
                    settled_run = settlement.run
                    if not settlement.run_terminal_published and settled_run.status in {
                        "success",
                        "error",
                        "timeout",
                        "interrupted",
                    }:
                        await self._quota.release_concurrent_run(
                            session,
                            locked_scope,
                            run_id=settled_run.run_id,
                            request_id="worker-private-run",
                        )
                        await self._audit.run_terminal(
                            session,
                            locked_scope,
                            run_id=settled_run.run_id,
                            job_id=claim.job_id,
                            job_type=claim.job_type,
                            status=settled_run.status,
                            public_error_code=result.public_error_code,
                            request_id="worker-private-run",
                        )
                if claim.job_type == "automation_run" and settled_run is not None:
                    from types import SimpleNamespace

                    from app.automations.errors import AutomationError
                    from app.automations.reconciliation import (
                        AutomationReconciler,
                    )

                    try:
                        await AutomationReconciler(
                            self._factory,
                        ).handle_run_completion(SimpleNamespace(run_id=settled_run.run_id))
                    except AutomationError as error:
                        logger.warning(
                            "Automation terminal reconciliation deferred: code=%s",
                            error.code,
                        )
            except PrivateRunExecutionLeaseLost:
                raise LeaseLost(claim.job_id) from None

        return JobSettlement(outcome, commit)

    async def __call__(
        self,
        claim: JobClaim,
        authority: JobLeaseAuthority,
    ) -> JobSettlement:
        try:
            execution, cancel_requested, recovered_terminal, settlement_scope = await self._begin(
                claim,
            )
        except LeaseLost:
            raise
        except Exception:
            # A database/authorization resolution failure before the Run lease
            # is attached must not fall back to WorkerService's job-only
            # settlement.  Let the durable lease expire for exact-scope retry.
            raise LeaseLost(claim.job_id) from None
        if recovered_terminal is not None:
            return self._settlement(
                claim,
                recovered_terminal,
                scope=settlement_scope,
                durable_terminal=True,
            )
        if execution is None:
            return self._settlement(
                claim,
                AgentExecutionResult.cancelled(),
                scope=settlement_scope,
            )
        authority.bind_heartbeat_callback(lambda: self._heartbeat(claim, execution.context))
        if cancel_requested or authority.cancel_requested:
            return self._settlement(
                claim,
                AgentExecutionResult.cancelled(),
                scope=execution.context.resource_scope,
            )
        try:
            result = await self._executor.execute(execution, authority)
        except asyncio.CancelledError:
            raise
        except TransientExecutionError as error:
            result = AgentExecutionResult.failed(error.public_error_code)
        except PermanentExecutionError as error:
            result = AgentExecutionResult.failed(
                error.public_error_code,
                retryable=False,
            )
        except PrivateWorkMcpQuotaExceeded as error:
            result = AgentExecutionResult.failed(error.code)
        except AmbiguousExternalSideEffect:
            return self._settlement(
                claim,
                AgentExecutionResult.failed("SIDE_EFFECT_STATE_UNKNOWN"),
                scope=execution.context.resource_scope,
                ambiguous_side_effect=True,
            )
        except Exception:
            return self._settlement(
                claim,
                AgentExecutionResult.failed("SIDE_EFFECT_STATE_UNKNOWN"),
                scope=execution.context.resource_scope,
                ambiguous_side_effect=True,
            )
        if not isinstance(result, AgentExecutionResult):
            result = AgentExecutionResult.failed("INVALID_AGENT_RESULT")
        if authority.cancel_requested:
            result = AgentExecutionResult.cancelled()
        return self._settlement(
            claim,
            result,
            scope=execution.context.resource_scope,
        )


__all__ = [
    "AgentExecutionResult",
    "AmbiguousExternalSideEffect",
    "PrivateRunExecution",
    "PrivateRunExecutionBoundary",
    "PrivateRunExecutor",
    "PrivateRunJobHandler",
    "PermanentExecutionError",
    "RunAgentPrivateExecutor",
    "TransientExecutionError",
]
