"""Lease-authorized private Run execution for the independent Worker."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Literal, Protocol

import sqlalchemy as sa
from langchain_core.messages import BaseMessage
from langchain_core.messages.utils import convert_to_messages
from langgraph.types import Command
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.personalization.repository import AccountPersonalizationRepository
from app.private_work.asset_runtime import PrivateAssetRuntime
from app.private_work.authorization import PrivateRunAuthorizationBoundary
from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkAssetStale, PrivateWorkMcpQuotaExceeded
from app.private_work.execution_profile import (
    RUN_EXECUTION_PROFILE_KWARG,
    RunExecutionProfileUnsupported,
    effective_run_execution_profile_from_kwargs,
)
from app.private_work.file_finalizer import (
    PrivateFileFinalizationAuditPort,
    PrivateFileFinalizer,
)
from app.private_work.memory_authority import (
    DEFAULT_PRIVATE_MEMORY_NAMESPACE,
    PrivateRunMemoryAuthority,
)
from app.private_work.run_admission import (
    AdmittedPrivateRun,
    PersistedRunSnapshot,
    _strip_client_memory_archive_receipt,
)
from app.private_work.run_repository import (
    PrivateRunExecutionLeaseLost,
    PrivateRunRecord,
    PrivateRunRepository,
    PrivateRunUsageSnapshot,
)
from app.private_work.sandbox_files import (
    CurrentUploadSnapshotEntry,
    CurrentUploadSnapshotInvalid,
    CurrentUploadSnapshotStale,
    PrivateFileRunScope,
    PrivateRunFileAuthority,
    PrivateSandboxFileProjection,
    required_current_upload_snapshot_from_run_kwargs,
)
from app.private_work.snapshot_repository import (
    RunSnapshotRepository,
    agent_model_snapshot_purpose,
)
from app.private_work.thread_repository import PrivateThreadRepository
from app.projects.capabilities import Capability
from app.projects.context import resolve_project_context_in_transaction
from app.projects.errors import ProjectForbidden, ProjectNotFound
from app.projects.models import ProjectRole
from app.reliability.jobs import (
    AdmittedJobRecord,
    automation_run_idempotency_key,
    private_run_idempotency_key,
)
from app.shared_assets.model_refs import resolve_model_ref
from app.shared_assets.skill_builder_agent_runtime import (
    SkillBuilderAgentFactory,
    WorkerSkillBuilderAuthoringCatalog,
)
from app.shared_assets.skill_design_generation import (
    SkillBuilderDependencySnapshot,
)
from app.shared_assets.skill_design_service import SkillDesignService
from app.system_runtime_settings.models import auxiliary_model_snapshot_ref
from app.worker.service import (
    JobLeaseAuthority,
    JobOutcome,
    JobSettlement,
    LeaseLost,
)
from deerflow.agents.memory.snip import (
    MEMORY_ARCHIVE_CONTEXT_KEY,
    SnipArchiveContext,
)
from deerflow.config.app_config import (
    pop_current_app_config,
    push_current_app_config,
)
from deerflow.config.mcp_security_config import McpSecurityConfig
from deerflow.config.model_config import ModelConfig
from deerflow.error_codes import (
    PRIVATE_RUN_EXECUTION_FAILED_ERROR_CODE,
    MemoryAuthorityUnavailable,
    PublicRunError,
    PublicRunErrorCode,
)
from deerflow.mcp.http_security import make_secure_mcp_http_client_factory
from deerflow.mcp_definition_policy import (
    McpEndpointPolicy,
    NetworkMcpEndpointPolicy,
)
from deerflow.models.factory import AgentModelSettingsUnsupported
from deerflow.persistence.jobs.sql import (
    JobClaim,
    JobRepository,
    JobTerminalEvent,
    JobTerminalResult,
)
from deerflow.persistence.private_work.memory_document_model import (
    MemoryDreamPrepareRunRow,
)
from deerflow.persistence.private_work.memory_document_repository import (
    DEFAULT_MEMORY_NAMESPACE,
)
from deerflow.persistence.private_work.model import PrivateFileRow
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.scheduled_task_runs.model import ScheduledTaskRunRow
from deerflow.persistence.scheduled_tasks.model import ScheduledTaskRow
from deerflow.persistence.shared_assets import (
    SkillDesignOperationRow,
    SkillDesignSessionRow,
)
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.runtime import (
    DisconnectMode,
    RunContext,
    RunManager,
    RunRecord,
    RunStatus,
    run_agent,
)
from deerflow.runtime.checkpoint_mode import CheckpointModeMismatchError
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
from deerflow.trace_context import normalize_trace_id, request_trace_context

_PUBLIC_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
logger = logging.getLogger(__name__)


class SystemModelMaterializationPort(Protocol):
    """Materialize the exact secret-bearing model frozen for one Run."""

    async def materialize_snapshot(
        self,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        run_id: str,
        purpose: str,
    ) -> ModelConfig: ...


class SystemRuntimePolicyMaterializationPort(Protocol):
    """Materialize the exact global runtime policy frozen for one Run."""

    async def materialize_run_snapshot(
        self,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        run_id: str,
    ) -> object: ...


def _private_guardrail_attribution(
    context: PrivateWorkContext,
    run: PrivateRunRecord,
) -> dict[str, object]:
    """Build the closed Guardrail identity from the locked private Run."""

    if run.project_id != context.project_id or run.owner_user_id != str(context.user_id):
        raise RuntimeError("Private Run attribution scope mismatch")
    return {
        "user_id": str(context.user_id),
        "user_role": context.role.value,
        "thread_id": run.thread_id,
        "run_id": run.run_id,
        "is_subagent": False,
        "authz_attributes": {
            "project_id": str(context.project_id),
            "project_role": context.role.value,
            "capabilities": tuple(sorted(capability.value for capability in context.capabilities)),
        },
    }


def _checkpoint_progress_cursor(saver: Any, item: Any | None) -> str | None:
    """Fingerprint the latest durable checkpoint plus its pending writes."""

    if item is None:
        return None
    raw_configurable = item.config.get("configurable")
    checkpoint_id = None
    if isinstance(raw_configurable, Mapping):
        raw_checkpoint_id = raw_configurable.get("checkpoint_id")
        if isinstance(raw_checkpoint_id, str):
            checkpoint_id = raw_checkpoint_id
    pending_writes = getattr(item, "pending_writes", None)
    if not pending_writes:
        return checkpoint_id
    if checkpoint_id is None:
        raise RuntimeError("checkpoint pending writes require a checkpoint id")

    digest = hashlib.sha256()

    def update(part: bytes) -> None:
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)

    update(b"deerflow:checkpoint-progress:v1")
    update(checkpoint_id.encode())
    for pending_write in pending_writes:
        if not isinstance(pending_write, (list, tuple)) or len(pending_write) != 3:
            raise RuntimeError("checkpoint pending write is invalid")
        task_id, channel, value = pending_write
        if not isinstance(task_id, str) or not isinstance(channel, str):
            raise RuntimeError("checkpoint pending write identity is invalid")
        try:
            value_type, value_bytes = saver.serde.dumps_typed(value)
        except Exception:
            raise RuntimeError("checkpoint pending write serialization failed") from None
        if not isinstance(value_type, str) or not isinstance(value_bytes, bytes):
            raise RuntimeError("checkpoint pending write serialization is invalid")
        update(task_id.encode())
        update(channel.encode())
        update(value_type.encode())
        update(value_bytes)
    return f"pw:{digest.hexdigest()}"


class TransientExecutionError(RuntimeError):
    """A public-safe failure before an ambiguous external side effect."""

    def __init__(
        self,
        public_error_code: str,
        *,
        attempt_usage: PrivateRunUsageSnapshot | None = None,
    ) -> None:
        if _PUBLIC_ERROR_CODE.fullmatch(public_error_code) is None:
            raise ValueError("transient execution error requires a public code")
        if attempt_usage is not None and type(attempt_usage) is not PrivateRunUsageSnapshot:
            raise TypeError("attempt_usage must be a PrivateRunUsageSnapshot or None")
        self.public_error_code = public_error_code
        self.attempt_usage = attempt_usage
        super().__init__(public_error_code)


class PermanentExecutionError(RuntimeError):
    """A deterministic public-safe failure that must not be retried."""

    def __init__(
        self,
        public_error_code: str,
        *,
        attempt_usage: PrivateRunUsageSnapshot | None = None,
    ) -> None:
        if _PUBLIC_ERROR_CODE.fullmatch(public_error_code) is None:
            raise ValueError("permanent execution error requires a public code")
        if attempt_usage is not None and type(attempt_usage) is not PrivateRunUsageSnapshot:
            raise TypeError("attempt_usage must be a PrivateRunUsageSnapshot or None")
        self.public_error_code = public_error_code
        self.attempt_usage = attempt_usage
        super().__init__(public_error_code)


class AmbiguousExternalSideEffect(RuntimeError):
    """Execution may have crossed an external side-effect boundary."""

    def __init__(
        self,
        *,
        attempt_usage: PrivateRunUsageSnapshot | None = None,
    ) -> None:
        if attempt_usage is not None and type(attempt_usage) is not PrivateRunUsageSnapshot:
            raise TypeError("attempt_usage must be a PrivateRunUsageSnapshot or None")
        self.attempt_usage = attempt_usage
        super().__init__("external side-effect state is unknown")


@dataclass(frozen=True, slots=True)
class AgentExecutionResult:
    status: Literal["succeeded", "cancelled", "failed"]
    public_error_code: str | None = None
    retryable: bool = False
    attempt_usage: PrivateRunUsageSnapshot | None = None

    def __post_init__(self) -> None:
        JobOutcome(self.status, self.public_error_code)
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if self.status != "failed" and self.retryable:
            raise ValueError("terminal success/cancel outcomes cannot be retryable")
        if self.attempt_usage is not None and type(self.attempt_usage) is not PrivateRunUsageSnapshot:
            raise TypeError("attempt_usage must be a PrivateRunUsageSnapshot or None")

    @classmethod
    def succeeded(
        cls,
        *,
        attempt_usage: PrivateRunUsageSnapshot | None = None,
    ) -> AgentExecutionResult:
        return cls("succeeded", attempt_usage=attempt_usage)

    @classmethod
    def cancelled(
        cls,
        *,
        attempt_usage: PrivateRunUsageSnapshot | None = None,
    ) -> AgentExecutionResult:
        return cls("cancelled", attempt_usage=attempt_usage)

    @classmethod
    def failed(
        cls,
        public_error_code: str,
        *,
        retryable: bool = True,
        attempt_usage: PrivateRunUsageSnapshot | None = None,
    ) -> AgentExecutionResult:
        return cls(
            "failed",
            public_error_code,
            retryable,
            attempt_usage,
        )


@dataclass(frozen=True, slots=True)
class _RecoveredPrivateRunTerminal:
    result: AgentExecutionResult
    ensure_stream_terminal: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.result, AgentExecutionResult):
            raise TypeError("recovered terminal requires an AgentExecutionResult")
        if type(self.ensure_stream_terminal) is not bool:
            raise TypeError("ensure_stream_terminal must be a boolean")
        if self.ensure_stream_terminal and self.result.status != "succeeded":
            raise ValueError("only recovered success can repair a stream terminal")


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
    runtime_kind: Literal["chat", "skill_builder"] = "chat"


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
        if event.job_type == "memory_dream_prepare":
            # ``JobRepository.claim_next`` owns Project -> Membership -> Thread
            # -> preparation -> Job before generic reclaim terminalization.
            # Custom handler settlements also own that prefix and update the row
            # before this idempotent convergence step.
            phase = "cancelled" if event.status == "cancelled" else "failed"
            disposition = "cancelled" if event.status == "cancelled" else "failed"
            await session.execute(
                sa.update(MemoryDreamPrepareRunRow)
                .where(
                    MemoryDreamPrepareRunRow.job_id == event.job_id,
                    MemoryDreamPrepareRunRow.project_id == event.project_id,
                    MemoryDreamPrepareRunRow.owner_user_id == event.owner_user_id,
                    MemoryDreamPrepareRunRow.completed_at.is_(None),
                )
                .values(
                    phase=phase,
                    result_disposition=disposition,
                    completed_at=event.occurred_at,
                    updated_at=event.occurred_at,
                )
            )
        if event.job_type not in {"private_run", "automation_run"}:
            await self._audit.job_terminalized(session, event)
            return JobTerminalResult(run_terminal_published=False)
        if event.owner_user_id is None or event.run_id is None:
            raise RuntimeError("private job terminal authority is incomplete")
        origin_trace_id = normalize_trace_id(event.origin_trace_id)
        if origin_trace_id is None:
            raise RuntimeError("private job terminal trace authority is incomplete")
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
                RunRow.origin_trace_id == origin_trace_id,
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
                request_id=origin_trace_id,
            )
            await self._audit.run_terminal(
                session,
                scope,
                run_id=event.run_id,
                job_id=event.job_id,
                job_type=event.job_type,
                status=run_status,
                public_error_code=event.public_error_code,
                request_id=origin_trace_id,
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
        terminal_error_code: Callable[[], str | None] | None = None,
    ) -> None:
        self._bridge = bridge
        self._boundary = boundary
        self._scope = scope
        self._thread_id = thread_id
        self._terminal_status = terminal_status
        self._terminal_error_code = terminal_error_code

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
                    error_code=(self._terminal_error_code() if self._terminal_error_code is not None else None),
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


class LeaseAuthorizedRunEventStore:
    """Bind Worker journal events to the same atomic Job lease as SSE writes."""

    def __init__(
        self,
        store: Any,
        boundary: PrivateRunExecutionBoundary,
        *,
        scope: PrivateResourceScope,
    ) -> None:
        self._store = store
        self._boundary = boundary
        self._scope = scope

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    async def put(self, **event: Any) -> dict:
        event.pop("scope", None)
        try:
            return await self._store.put(
                **event,
                scope=self._scope,
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

    async def put_batch(
        self,
        events: list[dict[str, Any]],
        *,
        scope: PrivateResourceScope | None = None,
    ) -> list[dict]:
        del scope
        try:
            return await self._store.put_batch(
                events,
                scope=self._scope,
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


class PrivateRunExecutionBoundary:
    """Combine member authorization with the current job/run lease proof."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        context: PrivateWorkContext,
        claim: JobClaim,
        quota: PrivateRunAgentQuotaPort | None = None,
        runtime_kind: Literal["chat", "skill_builder"] = "chat",
    ) -> None:
        if claim.run_id is None:
            raise ValueError("private execution claim requires a Run")
        self._factory = session_factory
        self._context = context
        self._claim = claim
        self._quota = quota or _NoopPrivateRunAgentQuota()
        self._runtime_kind = runtime_kind
        executable_roles = (
            (ProjectRole.ADMIN.value, ProjectRole.EDITOR.value)
            if runtime_kind == "skill_builder"
            else (
                ProjectRole.ADMIN.value,
                ProjectRole.EDITOR.value,
                ProjectRole.RUNNER.value,
                ProjectRole.CHANNEL_GUEST.value,
            )
        )
        self._authorization = PrivateRunAuthorizationBoundary(
            session_factory,
            project_id=context.project_id,
            owner_user_id=str(context.user_id),
            run_id=claim.run_id,
            executable_roles=executable_roles,
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

    async def before_read_only_tool_call(self) -> None:
        await self._check("before_tool_call")

    async def before_idempotent_tool_call(self) -> None:
        await self._check("before_tool_call")

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
        model_materializer: SystemModelMaterializationPort | None = None,
        runtime_policy_materializer: SystemRuntimePolicyMaterializationPort | None = None,
        agent_factory: Any | None = None,
        runner=run_agent,
        quota: PrivateRunAgentQuotaPort | None = None,
        audit: PrivateFileFinalizationAuditPort | None = None,
    ) -> None:
        self._factory = session_factory
        self._app_config = app_config
        self._bridge = bridge
        self._project_checkpointer = project_checkpointer
        self._store = store
        self._event_store = event_store
        self._model_materializer = model_materializer
        self._runtime_policy_materializer = runtime_policy_materializer
        if asset_runtime is None:
            raw_mcp_security = getattr(app_config, "mcp_security", None)
            if isinstance(raw_mcp_security, McpSecurityConfig):
                mcp_security = raw_mcp_security
            elif isinstance(raw_mcp_security, Mapping):
                mcp_security = McpSecurityConfig.model_validate(raw_mcp_security)
            else:
                mcp_security = McpSecurityConfig()
            self._asset_runtime = PrivateAssetRuntime(
                session_factory,
                endpoint_policy=NetworkMcpEndpointPolicy(
                    mcp_security.project_remote_allowed_networks,
                ),
                http_client_factory=make_secure_mcp_http_client_factory(
                    proxy_url=mcp_security.egress_proxy_url,
                    timeout_seconds=max(
                        mcp_security.discovery_timeout_seconds,
                        mcp_security.tool_call_timeout_seconds,
                    ),
                ),
                discovery_timeout_seconds=mcp_security.discovery_timeout_seconds,
                tool_call_timeout_seconds=mcp_security.tool_call_timeout_seconds,
                run_session_reuse=mcp_security.run_session_reuse,
            )
        else:
            self._asset_runtime = asset_runtime
        self._agent_factory = agent_factory or self._default_agent_factory()
        self._runner = runner
        self._quota = quota or _NoopPrivateRunAgentQuota()
        self._file_finalization_audit = audit

    @staticmethod
    def _default_agent_factory():
        from deerflow.agents.lead_agent.agent import make_lead_agent

        return make_lead_agent

    @staticmethod
    def _usage_snapshot(record: RunRecord) -> PrivateRunUsageSnapshot:
        return PrivateRunUsageSnapshot(
            total_input_tokens=record.total_input_tokens,
            total_output_tokens=record.total_output_tokens,
            total_tokens=record.total_tokens,
            llm_call_count=record.llm_call_count,
            lead_agent_tokens=record.lead_agent_tokens,
            subagent_tokens=record.subagent_tokens,
            middleware_tokens=record.middleware_tokens,
            token_usage_by_model=record.token_usage_by_model,
        )

    @classmethod
    def _output_limit_error(
        cls,
        record: RunRecord | None,
        *,
        lease_lost: bool,
    ) -> PermanentExecutionError:
        return PermanentExecutionError(
            PublicRunErrorCode.MODEL_OUTPUT_LIMIT.value,
            attempt_usage=(cls._usage_snapshot(record) if record is not None and not lease_lost else None),
        )

    async def _memory_archive_context(
        self,
        execution: PrivateRunExecution,
        app_config: Any,
    ) -> SnipArchiveContext:
        if execution.runtime_kind == "skill_builder":
            return SnipArchiveContext(
                enabled=False,
                project_id=execution.context.project_id,
                owner_user_id=str(execution.context.user_id),
                namespace=DEFAULT_MEMORY_NAMESPACE,
                preference_version=1,
                summary_model_ref=None,
            )
        try:
            async with self._factory() as session, session.begin():
                preference = await AccountPersonalizationRepository(
                    session,
                ).read_memory(execution.context.user_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise TransientExecutionError(
                "EXECUTION_AUTHORITY_UNAVAILABLE",
            ) from None

        enabled = bool(app_config.memory.enabled and preference.memory_enabled)
        summary_model_ref: uuid.UUID | None = None
        if enabled:
            model_name = app_config.summarization.model_name
            if model_name is None:
                models = getattr(app_config, "models", None)
                if not isinstance(models, list) or not models:
                    raise PermanentExecutionError("RUN_ASSET_STALE")
                model_name = models[0].name
            model = app_config.get_model_config(model_name)
            summary_model_ref = getattr(
                model,
                "_system_model_config_version_id",
                None,
            )
            if not isinstance(summary_model_ref, uuid.UUID):
                raise PermanentExecutionError("RUN_ASSET_STALE")
        return SnipArchiveContext(
            enabled=enabled,
            project_id=execution.context.project_id,
            owner_user_id=str(execution.context.user_id),
            namespace=DEFAULT_MEMORY_NAMESPACE,
            preference_version=preference.version,
            summary_model_ref=summary_model_ref,
        )

    @staticmethod
    def _required_current_upload_snapshot(
        run_kwargs: object,
    ) -> tuple[CurrentUploadSnapshotEntry, ...]:
        try:
            return required_current_upload_snapshot_from_run_kwargs(run_kwargs)
        except CurrentUploadSnapshotInvalid:
            raise PermanentExecutionError(
                "RUN_CURRENT_UPLOAD_STALE",
            ) from None

    @staticmethod
    def _runner_config(
        execution: PrivateRunExecution,
        archive_context: SnipArchiveContext,
    ) -> dict[str, Any]:
        config = copy.deepcopy(execution.config)
        raw_profile_present = RUN_EXECUTION_PROFILE_KWARG in execution.run.kwargs
        try:
            effective_profile = effective_run_execution_profile_from_kwargs(
                execution.run.kwargs,
            )
        except RunExecutionProfileUnsupported:
            raise PermanentExecutionError(
                "RUN_EXECUTION_PROFILE_STALE",
            ) from None
        if raw_profile_present and effective_profile is None:
            raise PermanentExecutionError("RUN_EXECUTION_PROFILE_STALE")
        raw_context = config.get("context")
        runtime_context = dict(raw_context) if isinstance(raw_context, Mapping) else {}
        runtime_context[MEMORY_ARCHIVE_CONTEXT_KEY] = archive_context
        raw_configurable = config.get("configurable")
        configurable = dict(raw_configurable) if isinstance(raw_configurable, Mapping) else {}
        if effective_profile is not None:
            persisted_model_name = getattr(execution.run, "model_name", None)
            if persisted_model_name is not None and persisted_model_name != effective_profile.model_name:
                raise PermanentExecutionError(
                    "RUN_EXECUTION_PROFILE_STALE",
                )
            for key, value in (
                ("thinking_enabled", effective_profile.thinking_enabled),
                ("reasoning_effort", effective_profile.reasoning_effort),
            ):
                runtime_context[key] = value
                configurable[key] = value
        config["context"] = runtime_context
        config["configurable"] = configurable
        return config

    @staticmethod
    def _graph_input(execution: PrivateRunExecution) -> object:
        if execution.resume_from_checkpoint:
            return None
        if execution.command is not None:
            if not isinstance(execution.command, Mapping):
                raise TransientExecutionError("INVALID_RUN_PAYLOAD")
            try:
                clean_command = _strip_client_memory_archive_receipt(
                    execution.command,
                    command=True,
                )
                if not isinstance(clean_command, Mapping):
                    raise TypeError
                return Command(**dict(clean_command))
            except (TypeError, ValueError):
                raise TransientExecutionError("INVALID_RUN_PAYLOAD") from None
        if not isinstance(execution.graph_input, Mapping):
            if execution.graph_input is None:
                return {}
            raise TransientExecutionError("INVALID_RUN_PAYLOAD")
        graph_input = _strip_client_memory_archive_receipt(
            execution.graph_input,
            command=False,
        )
        if not isinstance(graph_input, dict):
            raise TransientExecutionError("INVALID_RUN_PAYLOAD")
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
                origin_trace_id=execution.run.origin_trace_id,
            ),
            snapshot=execution.snapshot,
            opaque_runtime_scope=execution.context.resource_scope,
        )

    async def execute(
        self,
        execution: PrivateRunExecution,
        authority: JobLeaseAuthority,
    ) -> AgentExecutionResult:
        run_trace_id = normalize_trace_id(execution.run.origin_trace_id)
        claim_trace_id = normalize_trace_id(authority.claim.origin_trace_id)
        if run_trace_id is None or claim_trace_id is None or run_trace_id != claim_trace_id:
            raise PermanentExecutionError("RUN_TRACE_MISMATCH")
        with request_trace_context(run_trace_id):
            return await self._execute_with_trace(execution, authority)

    async def _execute_with_trace(
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
            runtime_kind=execution.runtime_kind,
        )
        admitted = self._admitted(execution, claim)
        private_runtime = None
        file_authority = None
        record: RunRecord | None = None
        runtime_config_pushed = False
        try:
            current_upload_snapshot = self._required_current_upload_snapshot(
                execution.run.kwargs,
            )
            exact_model_name = execution.run.model_name
            if exact_model_name is None:
                raise PermanentExecutionError("RUN_ASSET_STALE")
            runtime_app_config = self._app_config
            runtime_policy = None
            delegate_model_names: dict[uuid.UUID, str] = {}
            if self._runtime_policy_materializer is not None:
                try:
                    runtime_policy = await self._runtime_policy_materializer.materialize_run_snapshot(
                        project_id=execution.context.project_id,
                        owner_user_id=str(execution.context.user_id),
                        run_id=execution.run.run_id,
                    )
                    runtime_app_config = self._app_config.with_runtime_policy(
                        runtime_policy,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise PermanentExecutionError(
                        "RUN_POLICY_STALE",
                    ) from None
            if self._model_materializer is not None:
                title_bound_name: str | None = None
                try:
                    lead_model = await self._model_materializer.materialize_snapshot(
                        project_id=execution.context.project_id,
                        owner_user_id=str(execution.context.user_id),
                        run_id=execution.run.run_id,
                        purpose="lead",
                    )
                    runtime_models: dict[str, ModelConfig] = {
                        lead_model.name: lead_model,
                    }
                    delegated_agent_versions: set[uuid.UUID] = set()
                    for asset in execution.snapshot.assets:
                        if asset.asset_kind != "agent" or asset.dependency_order == 0:
                            continue
                        if asset.version_id in delegated_agent_versions:
                            raise PermanentExecutionError("RUN_ASSET_STALE")
                        delegated_agent_versions.add(asset.version_id)
                        delegated_model = await self._model_materializer.materialize_snapshot(
                            project_id=execution.context.project_id,
                            owner_user_id=str(execution.context.user_id),
                            run_id=execution.run.run_id,
                            purpose=agent_model_snapshot_purpose(
                                asset.version_id,
                            ),
                        )
                        existing = runtime_models.get(delegated_model.name)
                        if existing is not None and existing != delegated_model:
                            raise PermanentExecutionError("RUN_ASSET_STALE")
                        runtime_models[delegated_model.name] = delegated_model
                        delegate_model_names[asset.version_id] = delegated_model.name
                    if runtime_policy is not None:
                        auxiliary_model_refs = (
                            ("title", runtime_app_config.title.model_name),
                            (
                                "summarization",
                                runtime_app_config.summarization.model_name,
                            ),
                            ("memory", runtime_app_config.memory.model_name),
                        )
                        for purpose, model_ref in auxiliary_model_refs:
                            snapshot_ref = auxiliary_model_snapshot_ref(
                                purpose,
                                model_ref,
                                title_enabled=runtime_app_config.title.enabled,
                            )
                            if snapshot_ref is None:
                                continue
                            try:
                                auxiliary_model = await self._model_materializer.materialize_snapshot(
                                    project_id=execution.context.project_id,
                                    owner_user_id=str(execution.context.user_id),
                                    run_id=execution.run.run_id,
                                    purpose=purpose,
                                )
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                if purpose == "title" and model_ref is None:
                                    continue
                                raise PermanentExecutionError(
                                    "RUN_ASSET_STALE",
                                ) from None
                            if model_ref is not None and auxiliary_model.name != model_ref:
                                raise PermanentExecutionError(
                                    "RUN_ASSET_STALE",
                                )
                            existing = runtime_models.get(auxiliary_model.name)
                            if existing is not None and existing != auxiliary_model:
                                raise PermanentExecutionError(
                                    "RUN_ASSET_STALE",
                                )
                            runtime_models[auxiliary_model.name] = auxiliary_model
                            if purpose == "title" and model_ref is None:
                                title_bound_name = auxiliary_model.name
                except asyncio.CancelledError:
                    raise
                except PermanentExecutionError:
                    raise
                except Exception:
                    raise PermanentExecutionError(
                        "RUN_ASSET_STALE",
                    ) from None
                if lead_model.name != exact_model_name:
                    raise PermanentExecutionError("RUN_ASSET_STALE")
                runtime_app_config = runtime_app_config.with_runtime_models(
                    tuple(runtime_models.values()),
                )
                if title_bound_name is not None:
                    runtime_app_config = runtime_app_config.model_copy(
                        update={
                            "title": runtime_app_config.title.model_copy(
                                update={"model_name": title_bound_name},
                            ),
                        },
                    )
            elif runtime_app_config.get_model_config(exact_model_name) is None:
                # Compatibility path for isolated unit tests. Production
                # composition always injects the PostgreSQL materializer.
                raise PermanentExecutionError("RUN_ASSET_STALE")

            archive_context = await self._memory_archive_context(
                execution,
                runtime_app_config,
            )
            push_current_app_config(runtime_app_config)
            runtime_config_pushed = True
            materialize_kwargs: dict[str, object] = {
                "authorization_boundary": boundary,
                "runtime_kind": execution.runtime_kind,
            }
            if delegate_model_names:
                materialize_kwargs["delegate_model_names"] = delegate_model_names
            private_runtime = await self._asset_runtime.materialize(
                execution.context,
                admitted,
                **materialize_kwargs,
            )
            resolved_runtime_model = resolve_model_ref(
                runtime_app_config,
                private_runtime.model_ref,
            )
            if getattr(resolved_runtime_model, "name", None) != exact_model_name:
                raise PermanentExecutionError("RUN_ASSET_STALE")

            skills_config = getattr(runtime_app_config, "skills", None)
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
            if execution.runtime_kind == "chat":
                file_authority = PrivateRunFileAuthority(
                    PrivateFileRunScope(
                        execution.context,
                        thread_id=execution.run.thread_id,
                        run_id=execution.run.run_id,
                        authorization_boundary=boundary,
                    ),
                    PrivateSandboxFileProjection(self._factory),
                    PrivateFileFinalizer(
                        self._factory,
                        quota=self._quota,
                        audit=self._file_finalization_audit,
                    ),
                    mounts=mounts,
                    current_upload_snapshot=current_upload_snapshot,
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
            memory_authority = (
                PrivateRunMemoryAuthority(
                    self._factory,
                    context=execution.context,
                    claim=claim,
                    thread_id=execution.run.thread_id,
                    namespace=DEFAULT_PRIVATE_MEMORY_NAMESPACE,
                    memory_config=runtime_app_config.memory,
                    audit=self._file_finalization_audit,
                )
                if execution.runtime_kind == "chat"
                else None
            )
            run_context = RunContext(
                checkpointer=checkpointer,
                store=self._store,
                event_store=LeaseAuthorizedRunEventStore(
                    self._event_store,
                    boundary,
                    scope=execution.context.resource_scope,
                ),
                run_events_config=None,
                thread_store=(
                    _PrivateRunThreadMetadataStore(
                        self._factory,
                        scope=execution.context.resource_scope,
                        boundary=boundary,
                    )
                    if execution.runtime_kind == "chat"
                    else None
                ),
                app_config=runtime_app_config,
                private_scope=execution.context.resource_scope,
                authorization_boundary=boundary,
                file_authority=file_authority,
                memory_authority=memory_authority,
                memory_archive_context=archive_context,
                guardrail_attribution=_private_guardrail_attribution(
                    execution.context,
                    execution.run,
                ),
                private_agent_runtime=private_runtime,
            )
            owner_token = set_current_user(
                SimpleNamespace(id=execution.run.owner_user_id),
            )
            storage_token = set_runtime_storage_user_id(
                execution.run.owner_user_id,
            )
            try:
                agent_factory = self._agent_factory
                if execution.runtime_kind == "skill_builder":
                    agent_factory = SkillBuilderAgentFactory(
                        catalog=WorkerSkillBuilderAuthoringCatalog(
                            self._factory,
                            execution.context,
                        ),
                        draft_sink=SkillDesignService(
                            self._factory,
                        ).terminal_sink(
                            execution.context,
                            claim,
                        ),
                    )
                await self._runner(
                    LeaseAuthorizedStreamBridge(
                        self._bridge,
                        boundary,
                        scope=execution.context.resource_scope,
                        thread_id=execution.run.thread_id,
                        terminal_status=lambda: str(record.status),
                        terminal_error_code=lambda: PublicRunErrorCode.MODEL_OUTPUT_LIMIT.value if record.error == PublicRunErrorCode.MODEL_OUTPUT_LIMIT.value else None,
                    ),
                    run_manager,
                    record,
                    ctx=run_context,
                    agent_factory=agent_factory,
                    graph_input=self._graph_input(execution),
                    config=self._runner_config(
                        execution,
                        archive_context,
                    ),
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
            attempt_usage = self._usage_snapshot(record)
            if boundary.cancel_requested or boundary.authorization_revoked:
                return AgentExecutionResult.cancelled(
                    attempt_usage=attempt_usage,
                )
            if record.status is RunStatus.success:
                return AgentExecutionResult.succeeded(
                    attempt_usage=attempt_usage,
                )
            if record.status is RunStatus.interrupted:
                return AgentExecutionResult.cancelled(
                    attempt_usage=attempt_usage,
                )
            if record.status is RunStatus.error and record.error == PublicRunErrorCode.MODEL_OUTPUT_LIMIT.value:
                return AgentExecutionResult.failed(
                    PublicRunErrorCode.MODEL_OUTPUT_LIMIT.value,
                    retryable=False,
                    attempt_usage=attempt_usage,
                )
            if boundary.ambiguous_side_effect:
                raise AmbiguousExternalSideEffect(
                    attempt_usage=attempt_usage,
                )
            return AgentExecutionResult.failed(
                "AGENT_EXECUTION_FAILED",
                attempt_usage=attempt_usage,
            )
        except asyncio.CancelledError:
            raise
        except CheckpointModeMismatchError as error:
            raise PermanentExecutionError(
                "CHECKPOINT_MODE_MISMATCH",
            ) from error
        except PrivateWorkAssetStale:
            raise PermanentExecutionError("RUN_ASSET_STALE") from None
        except CurrentUploadSnapshotStale:
            raise PermanentExecutionError(
                "RUN_CURRENT_UPLOAD_STALE",
            ) from None
        except AgentModelSettingsUnsupported:
            raise PermanentExecutionError(
                "RUN_EXECUTION_PROFILE_UNSUPPORTED",
            ) from None
        except TransientExecutionError as error:
            if error.attempt_usage is None and record is not None and not boundary.lease_lost:
                raise TransientExecutionError(
                    error.public_error_code,
                    attempt_usage=self._usage_snapshot(record),
                ) from error
            raise
        except PermanentExecutionError:
            # Deterministic admitted-snapshot drift must reach the Job handler
            # unchanged so it settles the Run dead instead of scheduling retry.
            raise
        except AmbiguousExternalSideEffect:
            raise
        except PrivateWorkMcpQuotaExceeded as error:
            raise TransientExecutionError(
                error.code,
                attempt_usage=(self._usage_snapshot(record) if record is not None and not boundary.lease_lost else None),
            ) from None
        except MemoryAuthorityUnavailable:
            raise TransientExecutionError(
                PRIVATE_RUN_EXECUTION_FAILED_ERROR_CODE,
                attempt_usage=(self._usage_snapshot(record) if record is not None and not boundary.lease_lost else None),
            ) from None
        except PublicRunError as error:
            if error.code is PublicRunErrorCode.MODEL_OUTPUT_LIMIT:
                raise self._output_limit_error(
                    record,
                    lease_lost=boundary.lease_lost,
                ) from error
            raise TransientExecutionError(
                PRIVATE_RUN_EXECUTION_FAILED_ERROR_CODE,
                attempt_usage=(self._usage_snapshot(record) if record is not None and not boundary.lease_lost else None),
            ) from None
        except AuthorizationRevoked:
            if boundary.lease_lost:
                raise TransientExecutionError(
                    "EXECUTION_AUTHORITY_UNAVAILABLE",
                ) from None
            return AgentExecutionResult.cancelled(
                attempt_usage=(self._usage_snapshot(record) if record is not None else None),
            )
        except Exception:
            if boundary.ambiguous_side_effect:
                raise AmbiguousExternalSideEffect(
                    attempt_usage=(self._usage_snapshot(record) if record is not None and not boundary.lease_lost else None),
                ) from None
            raise TransientExecutionError(
                PRIVATE_RUN_EXECUTION_FAILED_ERROR_CODE,
                attempt_usage=(self._usage_snapshot(record) if record is not None and not boundary.lease_lost else None),
            ) from None
        finally:
            if runtime_config_pushed:
                pop_current_app_config()
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

    @staticmethod
    def _permanent_failure(
        error: PermanentExecutionError,
    ) -> AgentExecutionResult:
        return AgentExecutionResult.failed(
            error.public_error_code,
            retryable=False,
            attempt_usage=error.attempt_usage,
        )

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        executor: PrivateRunExecutor,
        retry_initial_seconds: int = 2,
        retry_max_seconds: int = 300,
        job_repository_builder=JobRepository,
        project_checkpointer: ProjectScopedCheckpointer | None = None,
        endpoint_policy: McpEndpointPolicy | None = None,
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
        self._snapshots = RunSnapshotRepository(
            session_factory,
            endpoint_policy=endpoint_policy,
        )
        self._events = DbRunEventStore(session_factory)

    def _runs(self, session: AsyncSession) -> PrivateRunRepository:
        return PrivateRunRepository(
            session,
            jobs=self._job_repository_builder(session),
        )

    @staticmethod
    async def _project_skill_builder_terminal(
        session: AsyncSession,
        claim: JobClaim,
        *,
        settled_status: str,
        public_error_code: str | None,
    ) -> None:
        """Close an unfinished Builder operation in the Run settlement tx."""

        if claim.run_id is None or claim.scope.owner_user_id is None:
            return
        pair = (
            await session.execute(
                sa.select(SkillDesignOperationRow, SkillDesignSessionRow)
                .join(
                    SkillDesignSessionRow,
                    sa.and_(
                        SkillDesignSessionRow.project_id == SkillDesignOperationRow.project_id,
                        SkillDesignSessionRow.owner_user_id == SkillDesignOperationRow.owner_user_id,
                        SkillDesignSessionRow.id == SkillDesignOperationRow.session_id,
                    ),
                )
                .where(
                    SkillDesignOperationRow.project_id == claim.scope.project_id,
                    SkillDesignOperationRow.owner_user_id == claim.scope.owner_user_id,
                    SkillDesignOperationRow.run_id == claim.run_id,
                    SkillDesignOperationRow.operation_kind == "turn",
                )
                .with_for_update(
                    of=[SkillDesignOperationRow, SkillDesignSessionRow],
                )
            )
        ).one_or_none()
        if pair is None:
            return
        operation, design = pair
        if operation.status != "in_progress":
            return
        code = "SKILL_DESIGN_INVALID_MODEL_OUTPUT" if settled_status == "success" else public_error_code or "SKILL_DESIGN_GENERATION_UNAVAILABLE"
        if not _PUBLIC_ERROR_CODE.fullmatch(code):
            code = "SKILL_DESIGN_GENERATION_UNAVAILABLE"
        message = "生成结果不是有效的 Skill 文件包，请调整描述后重试。" if code == "SKILL_DESIGN_INVALID_MODEL_OUTPUT" else "Skill 生成暂时不可用，请稍后重试。"
        design.status = "failed"
        design.active_clarification_json = None
        design.validation_json = None
        design.validated_draft_checksum = None
        design.error_code = code
        design.error_message = message
        design.progress_json = [
            {"id": "interview", "label": "确认需求", "status": "completed"},
            {"id": "package", "label": "生成候选文件", "status": "failed"},
            {"id": "validate", "label": "检查 Skill", "status": "pending"},
        ]
        design.messages_json = [
            *design.messages_json,
            {
                "id": uuid.uuid4().hex,
                "role": "assistant",
                "content": message,
                "created_at": datetime.now(UTC).isoformat(),
            },
        ]
        design.revision += 1
        operation.status = "failed"
        operation.result_revision = design.revision
        operation.public_error_code = code
        await session.flush()

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

    @staticmethod
    async def _runtime_kind_in_session(
        session: AsyncSession,
        claim: JobClaim,
        *,
        lock_builder: bool = False,
    ) -> tuple[Literal["chat", "skill_builder"], str | None]:
        """Classify the trusted Run purpose from its durable owning relation."""

        if claim.run_id is None or claim.scope.owner_user_id is None:
            raise LeaseLost(claim.job_id)
        builder_statement = (
            sa.select(SkillDesignSessionRow.thread_id)
            .join(
                SkillDesignOperationRow,
                sa.and_(
                    SkillDesignOperationRow.project_id == SkillDesignSessionRow.project_id,
                    SkillDesignOperationRow.owner_user_id == SkillDesignSessionRow.owner_user_id,
                    SkillDesignOperationRow.session_id == SkillDesignSessionRow.id,
                ),
            )
            .where(
                SkillDesignOperationRow.project_id == claim.scope.project_id,
                SkillDesignOperationRow.owner_user_id == claim.scope.owner_user_id,
                SkillDesignOperationRow.run_id == claim.run_id,
                SkillDesignOperationRow.operation_kind == "turn",
            )
            .limit(1)
        )
        if lock_builder:
            builder_statement = builder_statement.with_for_update(
                of=[SkillDesignSessionRow, SkillDesignOperationRow],
            )
        builder_link = (await session.execute(builder_statement)).scalar_one_or_none()
        if builder_link is None:
            return "chat", None
        return "skill_builder", str(builder_link)

    @staticmethod
    def _completed_skill_builder_terminal(
        operation: SkillDesignOperationRow,
        design: SkillDesignSessionRow,
        *,
        thread_id: str,
    ) -> AgentExecutionResult | None:
        """Recognize only an exact, already-committed Builder terminal fact.

        This is the crash window after a terminal tool transaction committed
        but before the Worker settled its Run. No model arguments participate:
        the operation/session rows were selected through the trusted
        project+owner+Run link and are locked by ``_runtime_kind_in_session``.
        """

        if (
            operation.status != "completed"
            or operation.public_error_code is not None
            or operation.result_revision is None
            or operation.result_revision != design.revision
            or str(design.thread_id) != thread_id
            or operation.terminal_kind not in {"clarification", "candidate"}
            or not isinstance(operation.terminal_request_checksum, str)
            or _SHA256_HEX.fullmatch(operation.terminal_request_checksum) is None
        ):
            return None
        if operation.terminal_kind == "clarification":
            payload = design.active_clarification_json
            if (
                design.status != "awaiting_clarification"
                or not isinstance(payload, dict)
                or payload.get("version") != 1
                or payload.get("kind") != "human_input_request"
                or payload.get("source") != "skill-builder"
                or payload.get("clarification_type") != "skill_design"
                or payload.get("input_mode") not in {"free_text", "single_choice"}
                or not isinstance(payload.get("request_id"), str)
                or not payload["request_id"]
                or not isinstance(payload.get("question"), str)
                or not payload["question"]
                or not isinstance(payload.get("options"), list)
            ):
                return None
            return AgentExecutionResult.succeeded()
        if (
            design.status != "draft_ready"
            or not isinstance(design.draft_checksum, str)
            or _SHA256_HEX.fullmatch(design.draft_checksum) is None
            or design.active_clarification_json is not None
            or design.validation_json is not None
            or design.validated_draft_checksum is not None
        ):
            return None
        try:
            dependencies = SkillBuilderDependencySnapshot.model_validate(
                design.authoring_dependencies_json,
            )
        except ValidationError:
            return None
        if dependencies.draft_checksum != design.draft_checksum:
            return None
        return AgentExecutionResult.succeeded()

    @classmethod
    async def _recovered_skill_builder_terminal_in_session(
        cls,
        session: AsyncSession,
        claim: JobClaim,
        *,
        thread_id: str,
    ) -> AgentExecutionResult | None:
        if claim.run_id is None or claim.scope.owner_user_id is None:
            raise LeaseLost(claim.job_id)
        pair = (
            await session.execute(
                sa.select(SkillDesignOperationRow, SkillDesignSessionRow)
                .join(
                    SkillDesignSessionRow,
                    sa.and_(
                        SkillDesignSessionRow.project_id == SkillDesignOperationRow.project_id,
                        SkillDesignSessionRow.owner_user_id == SkillDesignOperationRow.owner_user_id,
                        SkillDesignSessionRow.id == SkillDesignOperationRow.session_id,
                    ),
                )
                .where(
                    SkillDesignOperationRow.project_id == claim.scope.project_id,
                    SkillDesignOperationRow.owner_user_id == claim.scope.owner_user_id,
                    SkillDesignOperationRow.run_id == claim.run_id,
                    SkillDesignOperationRow.operation_kind == "turn",
                    SkillDesignSessionRow.thread_id == uuid.UUID(thread_id),
                )
                .with_for_update(
                    of=[SkillDesignOperationRow, SkillDesignSessionRow],
                )
            )
        ).one_or_none()
        if pair is None:
            return None
        return cls._completed_skill_builder_terminal(
            pair[0],
            pair[1],
            thread_id=thread_id,
        )

    async def _begin(
        self,
        claim: JobClaim,
    ) -> tuple[
        PrivateRunExecution | None,
        bool,
        _RecoveredPrivateRunTerminal | None,
        PrivateResourceScope,
    ]:
        origin_trace_id = normalize_trace_id(claim.origin_trace_id)
        if claim.job_type not in {"private_run", "automation_run"} or claim.run_id is None or claim.scope.owner_user_id is None or (claim.job_type == "automation_run" and claim.occurrence_id is None) or origin_trace_id is None:
            raise LeaseLost(claim.job_id)
        try:
            owner_user_id = uuid.UUID(claim.scope.owner_user_id)
        except ValueError:
            raise LeaseLost(claim.job_id) from None
        async with self._factory() as session, session.begin():
            claim_scope = await self._claim_scope(session, claim)
            runtime_kind, builder_thread_id = await self._runtime_kind_in_session(
                session,
                claim,
                lock_builder=True,
            )
            try:
                project = await resolve_project_context_in_transaction(
                    session,
                    owner_user_id,
                    claim.scope.project_id,
                    origin_trace_id,
                    lock=True,
                )
                if runtime_kind == "skill_builder":
                    project.require(Capability.SHARED_ASSETS_READ)
                    project.require(Capability.SHARED_ASSETS_EDIT)
                else:
                    project.require(Capability.PRIVATE_WORK_CREATE)
                    project.require(Capability.SHARED_ASSETS_EXECUTE)
            except (ProjectNotFound, ProjectForbidden):
                state = await self._runs(session).begin_execution(
                    scope=claim_scope,
                    run_id=claim.run_id,
                    job_id=claim.job_id,
                    lease_token=claim.lease_token,
                    origin_trace_id=origin_trace_id,
                )
                return None, True, None, claim_scope
            context = PrivateWorkContext.from_project(project)
            runs = self._runs(session)
            state = await runs.begin_execution(
                scope=context.resource_scope,
                run_id=claim.run_id,
                job_id=claim.job_id,
                lease_token=claim.lease_token,
                origin_trace_id=origin_trace_id,
            )
            thread_kind = await session.scalar(
                sa.select(ThreadMetaRow.thread_kind).where(
                    ThreadMetaRow.project_id == context.project_id,
                    ThreadMetaRow.owner_user_id == str(context.user_id),
                    ThreadMetaRow.thread_id == state.run.thread_id,
                    ThreadMetaRow.deleted_at.is_(None),
                )
            )
            if thread_kind != runtime_kind or (runtime_kind == "skill_builder" and state.run.thread_id != builder_thread_id):
                raise TransientExecutionError("RUN_PURPOSE_INVALID")
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
                    _RecoveredPrivateRunTerminal(
                        self._terminal_result(terminal),
                    ),
                    context.resource_scope,
                )
            if runtime_kind == "skill_builder":
                recovered_builder_terminal = await self._recovered_skill_builder_terminal_in_session(
                    session,
                    claim,
                    thread_id=state.run.thread_id,
                )
                if recovered_builder_terminal is not None:
                    return (
                        None,
                        state.cancel_requested,
                        _RecoveredPrivateRunTerminal(
                            recovered_builder_terminal,
                            ensure_stream_terminal=True,
                        ),
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
                checkpoint_cursor = _checkpoint_progress_cursor(saver, item)
                resume_from_checkpoint = await runs.prepare_checkpoint_takeover(
                    scope=context.resource_scope,
                    run_id=claim.run_id,
                    job_id=claim.job_id,
                    attempt_id=claim.attempt_id,
                    lease_token=claim.lease_token,
                    latest_checkpoint_id=checkpoint_cursor,
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
                runtime_kind=runtime_kind,
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
            if isinstance(terminal.data, Mapping) and terminal.data.get("error_code") == PublicRunErrorCode.MODEL_OUTPUT_LIMIT.value:
                return AgentExecutionResult.failed(
                    PublicRunErrorCode.MODEL_OUTPUT_LIMIT.value,
                    retryable=False,
                )
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
        ensure_stream_terminal: bool = False,
    ) -> JobSettlement:
        if ensure_stream_terminal and (not durable_terminal or result.status != "succeeded"):
            raise ValueError(
                "stream terminal repair requires a durable successful result",
            )
        outcome = JobOutcome(result.status, result.public_error_code)
        origin_trace_id = normalize_trace_id(claim.origin_trace_id)
        if origin_trace_id is None:
            raise LeaseLost(claim.job_id)

        async def commit_with_trace() -> None:
            try:
                settled_run = None
                async with self._factory() as session, session.begin():
                    locked_scope = await self._claim_scope(session, claim)
                    if locked_scope.project_id != scope.project_id or locked_scope.owner_user_id != scope.owner_user_id:
                        raise LeaseLost(claim.job_id)
                    await self._runtime_kind_in_session(
                        session,
                        claim,
                        lock_builder=True,
                    )
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
                        attempt_usage=result.attempt_usage,
                    )
                    settled_run = settlement.run
                    if settled_run.status in {
                        "success",
                        "error",
                        "timeout",
                        "interrupted",
                    }:
                        await self._project_skill_builder_terminal(
                            session,
                            claim,
                            settled_status=settled_run.status,
                            public_error_code=result.public_error_code,
                        )
                    if ensure_stream_terminal:
                        if settled_run.status != "success":
                            raise PrivateRunExecutionLeaseLost
                        await self._events.ensure_settled_stream_terminal(
                            session,
                            scope=locked_scope,
                            thread_id=settled_run.thread_id,
                            run_id=settled_run.run_id,
                            status="completed",
                        )
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
                            request_id=claim.origin_trace_id,
                        )
                        await self._audit.run_terminal(
                            session,
                            locked_scope,
                            run_id=settled_run.run_id,
                            job_id=claim.job_id,
                            job_type=claim.job_type,
                            status=settled_run.status,
                            public_error_code=result.public_error_code,
                            request_id=claim.origin_trace_id,
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

        async def commit() -> None:
            with request_trace_context(origin_trace_id):
                await commit_with_trace()

        return JobSettlement(outcome, commit)

    async def __call__(
        self,
        claim: JobClaim,
        authority: JobLeaseAuthority,
    ) -> JobSettlement:
        origin_trace_id = normalize_trace_id(claim.origin_trace_id)
        if origin_trace_id is None:
            raise LeaseLost(claim.job_id)
        with request_trace_context(origin_trace_id):
            return await self._handle_with_trace(claim, authority)

    async def _handle_with_trace(
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
                recovered_terminal.result,
                scope=settlement_scope,
                durable_terminal=True,
                ensure_stream_terminal=(recovered_terminal.ensure_stream_terminal),
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
            result = AgentExecutionResult.failed(
                error.public_error_code,
                attempt_usage=error.attempt_usage,
            )
        except PermanentExecutionError as error:
            result = self._permanent_failure(error)
        except PrivateWorkMcpQuotaExceeded as error:
            result = AgentExecutionResult.failed(error.code)
        except AmbiguousExternalSideEffect as error:
            return self._settlement(
                claim,
                AgentExecutionResult.failed(
                    "SIDE_EFFECT_STATE_UNKNOWN",
                    attempt_usage=error.attempt_usage,
                ),
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
            result = AgentExecutionResult.cancelled(
                attempt_usage=result.attempt_usage,
            )
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
