"""Private Run Job claim handling and terminal settlement."""

from __future__ import annotations

import asyncio
import copy
import logging
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal

import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkMcpQuotaExceeded
from app.private_work.execution_approval import (
    recover_staged_execution_approval_id,
    settle_staged_execution_approvals,
)
from app.private_work.execution_approval_audit import (
    NoopHostExecutionApprovalAudit,
)
from app.private_work.execution_approval_lifecycle import (
    ExecutionApprovalPrivateLifecycleConflict,
)
from app.private_work.output_delivery_obligation import (
    OutputDeliveryObligationConflict,
    settle_continuation_output_delivery,
)
from app.private_work.run_admission import PersistedRunSnapshot
from app.private_work.run_metadata import run_token_budget_usage
from app.private_work.run_repository import (
    PrivateRunExecutionLeaseLost,
    PrivateRunRepository,
)
from app.private_work.snapshot_repository import RunSnapshotRepository
from app.projects.capabilities import Capability
from app.projects.context import resolve_project_context_in_transaction
from app.projects.errors import ProjectForbidden, ProjectNotFound
from app.reliability.run_execution.contracts import (
    AgentExecutionResult,
    PrivateRunExecution,
)
from app.reliability.run_execution.contracts import (
    RecoveredPrivateRunTerminal as _RecoveredPrivateRunTerminal,
)
from app.reliability.run_execution.errors import (
    AmbiguousExternalSideEffect,
    PermanentExecutionError,
    TransientExecutionError,
    is_public_error_code,
)
from app.reliability.run_execution.ports import (
    NoopPrivateRunExecutionAudit as _NoopPrivateRunExecutionAudit,
)
from app.reliability.run_execution.ports import (
    NoopPrivateRunExecutionQuota as _NoopPrivateRunExecutionQuota,
)
from app.reliability.run_execution.ports import (
    PrivateRunExecutionAuditPort,
    PrivateRunExecutionQuotaPort,
    PrivateRunExecutor,
)
from app.reliability.run_execution.projections import (
    checkpoint_progress_cursor as _checkpoint_progress_cursor,
)
from app.shared_assets.skill_design_activity import SkillDesignActivityRepository
from app.shared_assets.skill_design_generation import (
    SkillBuilderDependencySnapshot,
)
from app.shared_assets.skill_design_repository import SkillDesignRepository
from app.worker.service import (
    JobLeaseAuthority,
    JobOutcome,
    JobSettlement,
    LeaseLost,
)
from deerflow.mcp_definition_policy import McpEndpointPolicy
from deerflow.persistence.jobs.sql import JobClaim, JobRepository
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.shared_assets import (
    SkillDesignOperationRow,
    SkillDesignSessionRow,
)
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.runtime.events.models import (
    STREAM_TERMINAL_ERROR_CODES,
    StoredStreamFrame,
)
from deerflow.runtime.events.store.db import DbRunEventStore
from deerflow.runtime.private_scope import PrivateResourceScope
from deerflow.trace_context import normalize_trace_id, request_trace_context

_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
logger = logging.getLogger("app.reliability.execution")


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
        execution_approval_ttl_seconds: int = 300,
    ) -> None:
        if retry_initial_seconds < 1 or retry_max_seconds < retry_initial_seconds:
            raise ValueError("invalid private Run retry policy")
        if execution_approval_ttl_seconds < 1:
            raise ValueError("invalid execution approval TTL")
        self._factory = session_factory
        self._executor = executor
        self._retry_initial_seconds = retry_initial_seconds
        self._retry_max_seconds = retry_max_seconds
        self._execution_approval_ttl_seconds = execution_approval_ttl_seconds
        self._job_repository_builder = job_repository_builder
        self._project_checkpointer = project_checkpointer
        self._quota = quota or _NoopPrivateRunExecutionQuota()
        self._audit = audit or _NoopPrivateRunExecutionAudit()
        self._execution_approval_audit = (
            self._audit
            if callable(
                getattr(
                    self._audit,
                    "host_execution_approval_available",
                    None,
                ),
            )
            else NoopHostExecutionApprovalAudit()
        )
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
        repository = SkillDesignRepository(session)
        baseline = await repository.restore_locked_operation_baseline(
            operation,
            design,
            request_id=normalize_trace_id(claim.origin_trace_id) or "unknown",
        )
        baseline_checksum = baseline.draft_checksum
        design.draft_checksum = baseline_checksum
        design.authoring_dependencies_json = None
        stopped = settled_status == "interrupted" and operation.stop_requested_at is not None
        if stopped:
            design.status = "draft_ready" if baseline_checksum is not None else "interviewing"
            design.active_clarification_json = None
            design.validation_json = None
            design.validated_draft_checksum = None
            design.error_code = None
            design.error_message = None
            design.progress_json = [
                {"id": "interview", "label": "确认需求", "status": "completed"},
                {
                    "id": "package",
                    "label": "生成候选文件",
                    "status": "completed" if baseline_checksum else "pending",
                },
                {"id": "validate", "label": "检查 Skill", "status": "pending"},
            ]
            design.revision += 1
            operation.status = "stopped"
            operation.result_revision = design.revision
            operation.public_error_code = None
            terminal_status = "stopped"
            terminal_code = None
        else:
            code = "SKILL_DESIGN_INVALID_MODEL_OUTPUT" if settled_status == "success" else public_error_code or "SKILL_DESIGN_GENERATION_UNAVAILABLE"
            if not is_public_error_code(code):
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
                    "operation_id": str(operation.id),
                },
            ]
            design.revision += 1
            operation.status = "failed"
            operation.result_revision = design.revision
            operation.public_error_code = code
            terminal_status = "failed"
            terminal_code = code
        await SkillDesignActivityRepository(
            session,
        ).append_locked_settlement_terminal(
            operation,
            status=terminal_status,
            code=terminal_code,
        )
        await repository.clear_locked_operation_baseline(operation)
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
                terminal_result = self._terminal_result(terminal)
                if terminal_result.status == "succeeded":
                    try:
                        suspended_approval_id = await recover_staged_execution_approval_id(
                            session,
                            claim=claim,
                        )
                    except ExecutionApprovalPrivateLifecycleConflict:
                        raise LeaseLost(claim.job_id) from None
                    terminal_result = AgentExecutionResult.succeeded(
                        suspended_approval_id=suspended_approval_id,
                    )
                return (
                    None,
                    state.cancel_requested,
                    _RecoveredPrivateRunTerminal(
                        terminal_result,
                    ),
                    context.resource_scope,
                )
            # The suspension marker is committed after checkpoint drain and
            # before the public success terminal.  If that later stream write
            # lost its Worker, the marker itself is the server-owned recovery
            # proof: revalidate its exact staged approval and settle without
            # invoking the graph again, while repairing the missing terminal
            # in the same authoritative settlement transaction.
            try:
                suspended_approval_id = await recover_staged_execution_approval_id(
                    session,
                    claim=claim,
                )
            except ExecutionApprovalPrivateLifecycleConflict:
                raise LeaseLost(claim.job_id) from None
            if suspended_approval_id is not None:
                return (
                    None,
                    state.cancel_requested,
                    _RecoveredPrivateRunTerminal(
                        AgentExecutionResult.succeeded(
                            suspended_approval_id=suspended_approval_id,
                        ),
                        ensure_stream_terminal=True,
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
            assets = await self._snapshots.list_asset_facts_in_session(
                session,
                context,
                state.run.thread_id,
                state.run.run_id,
                lock=True,
            )
            secrets = await self._snapshots.list_mcp_secrets_in_session(
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
                mcp_secrets=secrets,
                catalog_generation=generations.pop(),
            )
            resume_from_checkpoint = False
            if self._project_checkpointer is not None:
                saver = self._project_checkpointer.for_context(
                    context,
                    thread_kind=runtime_kind,
                )
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
                token_budget_usage=run_token_budget_usage(
                    state.run.metadata,
                    run_id=state.run.run_id,
                ),
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
            if isinstance(terminal.data, Mapping) and terminal.data.get("error_code") in STREAM_TERMINAL_ERROR_CODES:
                return AgentExecutionResult.failed(
                    str(terminal.data["error_code"]),
                    retryable=False,
                )
            return AgentExecutionResult.failed(
                "AGENT_EXECUTION_FAILED",
                retryable=False,
            )
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
                        try:
                            await settle_continuation_output_delivery(
                                session,
                                approval_id_value=settled_run.kwargs.get(
                                    "host_execution_approval_id",
                                ),
                                project_id=locked_scope.project_id,
                                owner_user_id=locked_scope.owner_user_id,
                                thread_id=settled_run.thread_id,
                                continuation_run_id=settled_run.run_id,
                                continuation_job_id=claim.job_id,
                                settled_status=settled_run.status,
                                now=settled_run.updated_at,
                                ambiguous_side_effect=ambiguous_side_effect,
                            )
                        except OutputDeliveryObligationConflict:
                            raise PrivateRunExecutionLeaseLost from None
                        await settle_staged_execution_approvals(
                            session,
                            claim=claim,
                            succeeded=settled_run.status == "success",
                            suspended_approval_id=(result.suspended_approval_id if settled_run.status == "success" else None),
                            request_ttl_seconds=(self._execution_approval_ttl_seconds),
                            durable_terminal_replay=durable_terminal,
                            audit=self._execution_approval_audit,
                        )
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

    async def _recover_sealed_suspension_after_execution(
        self,
        claim: JobClaim,
    ) -> str | None:
        """Re-read durable proof after a post-marker executor failure."""

        try:
            async with self._factory() as session, session.begin():
                return await recover_staged_execution_approval_id(
                    session,
                    claim=claim,
                )
        except Exception:
            # A malformed/mismatched marker or unavailable authority must not
            # fall through to failure settlement, which would erase the only
            # checkpoint-safe success proof.
            raise LeaseLost(claim.job_id) from None

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
        ambiguous_side_effect = False
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
            result = AgentExecutionResult.failed(
                "SIDE_EFFECT_STATE_UNKNOWN",
                attempt_usage=error.attempt_usage,
            )
            ambiguous_side_effect = True
        except Exception:
            result = AgentExecutionResult.failed(
                "SIDE_EFFECT_STATE_UNKNOWN",
            )
            ambiguous_side_effect = True
        if not isinstance(result, AgentExecutionResult):
            result = AgentExecutionResult.failed("INVALID_AGENT_RESULT")
        if result.status == "succeeded" and result.suspended_approval_id is not None:
            # A successful suspension can only return after the Worker sealed
            # its exact marker.  Give that durable proof precedence over a
            # cancellation arriving after the marker, and idempotently repair
            # a terminal whose ACK was lost.
            return self._settlement(
                claim,
                result,
                scope=execution.context.resource_scope,
                durable_terminal=True,
                ensure_stream_terminal=True,
            )
        if result.status != "succeeded" or authority.cancel_requested:
            suspended_approval_id = await self._recover_sealed_suspension_after_execution(
                claim,
            )
            if suspended_approval_id is not None:
                return self._settlement(
                    claim,
                    AgentExecutionResult.succeeded(
                        attempt_usage=result.attempt_usage,
                        suspended_approval_id=suspended_approval_id,
                    ),
                    scope=execution.context.resource_scope,
                    durable_terminal=True,
                    ensure_stream_terminal=True,
                )
        if authority.cancel_requested:
            result = AgentExecutionResult.cancelled(
                attempt_usage=result.attempt_usage,
            )
        return self._settlement(
            claim,
            result,
            scope=execution.context.resource_scope,
            ambiguous_side_effect=ambiguous_side_effect,
        )


__all__ = ["PrivateRunJobHandler"]
