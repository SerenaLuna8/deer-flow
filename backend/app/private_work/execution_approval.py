"""Durable one-shot authority for Local Provider host process execution.

The model may describe a process launch and the browser may decide whether to
allow it, but neither is an authority source.  This module owns the private
frozen plan, provider-policy snapshot, one-shot claim, and durable receipt.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.private_work.context import PrivateWorkContext
from app.private_work.errors import (
    ExecutionApprovalConflict,
    ExecutionApprovalNotFound,
    PrivateWorkError,
    PrivateWorkUnavailable,
)
from app.private_work.execution_approval_audit import (
    HostExecutionApprovalAuditPort,
    NoopHostExecutionApprovalAudit,
)
from app.private_work.execution_approval_lifecycle import (
    CLAIMED_EXECUTION_SETTLEMENT_GRACE_SECONDS,
    ExecutionApprovalContinuationQuotaPort,
    ExecutionApprovalContinuationRunAuditPort,
    ExecutionApprovalPrivateLifecycleConflict,
    LockedExecutionApprovalRows,
    claimed_execution_absolute_deadline,
    lock_execution_approval_private_rows,
    reconcile_locked_execution_approval,
    reconcile_locked_execution_approval_and_continuation,
)
from app.private_work.execution_profile import RequestedRunExecutionProfile
from app.private_work.revalidation import PrivateWorkRevalidator
from app.private_work.run_admission import (
    PrivateRunAdmissionServerContext,
    PrivateRunAdmissionService,
)
from app.private_work.run_repository import (
    PrivateRunCreate,
    PrivateRunExecutionLeaseLost,
    PrivateRunRepository,
)
from app.projects.capabilities import Capability
from deerflow.persistence.execution_approvals import (
    ExecutionApprovalRequestRow,
    ExecutionApprovalResultReceiptRow,
)
from deerflow.persistence.jobs.model import JobAttemptRow, JobRow
from deerflow.persistence.jobs.sql import JobClaim
from deerflow.persistence.private_work import (
    RunAssetVersionRow,
    RunMcpGrantSnapshotRow,
    RunSkillCredentialSnapshotRow,
)
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.system_runtime_settings import RunRuntimePolicySnapshotRow
from deerflow.persistence.system_settings import RunModelConfigSnapshotRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.runtime.host_execution_approval import (
    HostExecutionApprovalArtifact,
    HostExecutionApprovalPort,
    HostExecutionApprovalResult,
    HostExecutionContinuationPort,
    HostExecutionFrozenClaim,
    HostExecutionOutcome,
    HostExecutionPlan,
)
from deerflow.runtime.host_execution_domain import (
    HostExecutionDomainSnapshot,
    host_execution_environment_fingerprint,
)
from deerflow.sandbox.env_policy import build_sandbox_env

if TYPE_CHECKING:
    from deerflow.config.app_config import AppConfig

_RESULT_TEXT_LIMIT = 20_000
_CLAIM_TTL_SECONDS = 60
_PRIVATE_ENVELOPE_SCHEMA_VERSION = 3
_PROVIDER_POLICY_SCHEMA_VERSION = 2
_RESULT_SCHEMA_VERSION = 1
_CONTINUATION_NAME = "host-execution-continuation:v1"
_HOST_EXECUTION_MODES = frozenset(
    {
        "isolated_direct",
        "local_disabled",
        "local_approval_required",
        "local_legacy_allow",
    },
)


def _now() -> datetime:
    return datetime.now(UTC)


async def _database_now(session: AsyncSession) -> datetime:
    value = await session.scalar(sa.select(sa.func.clock_timestamp()))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise PrivateRunExecutionLeaseLost
    return value.astimezone(UTC)


def _canonical_digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="surrogatepass"),
    ).hexdigest()


def _bounded_text(value: str | None) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    if not isinstance(value, str):
        raise TypeError("host execution result fields must be strings or None")
    return value[:_RESULT_TEXT_LIMIT], len(value) > _RESULT_TEXT_LIMIT


def _decision_digest(
    *,
    approval_id: uuid.UUID,
    source_run_id: str,
    decision: str,
    expected_version: int,
) -> str:
    return _canonical_digest(
        {
            "schema_version": 1,
            "approval_id": str(approval_id),
            "source_run_id": source_run_id,
            "decision": decision,
            "expected_version": expected_version,
        },
    )


def _idempotency_digest(value: uuid.UUID) -> str:
    return hashlib.sha256(
        f"execution-approval-decision:{value}".encode(),
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class HostExecutionProviderPolicySnapshot:
    """Strict, app-owned Local host-execution policy snapshot.

    The snapshot deliberately contains only non-secret scalar configuration.
    A disabled or isolated snapshot is representable so drift can be compared
    and rejected instead of failing while constructing the trusted adapter.
    """

    provider_use: str
    host_execution_mode: str
    allow_host_bash: bool
    bash_command_timeout: int
    approval_max_timeout_seconds: int
    request_ttl_seconds: int
    execution_domain_id: str | None
    local_mounts: tuple[tuple[str, str, bool], ...] = ()
    skills_container_path: str = "/mnt/skills"
    schema_version: int = field(
        default=_PROVIDER_POLICY_SCHEMA_VERSION,
        init=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.provider_use, str) or not self.provider_use or self.provider_use.strip() != self.provider_use:
            raise ValueError("provider_use must be a non-empty exact string")
        if self.host_execution_mode not in _HOST_EXECUTION_MODES:
            raise ValueError("host_execution_mode is invalid")
        if type(self.allow_host_bash) is not bool:
            raise TypeError("allow_host_bash must be a boolean")
        for name in (
            "bash_command_timeout",
            "approval_max_timeout_seconds",
            "request_ttl_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.skills_container_path, str) or not self.skills_container_path.startswith("/") or (self.skills_container_path != "/" and self.skills_container_path.endswith("/")):
            raise ValueError("skills_container_path must be a normalized absolute path")
        if self.execution_domain_id is not None and (not isinstance(self.execution_domain_id, str) or not self.execution_domain_id or self.execution_domain_id.strip() != self.execution_domain_id):
            raise ValueError(
                "execution_domain_id must be an exact non-empty string",
            )
        if self.approval_enabled and self.execution_domain_id is None:
            raise ValueError(
                "Local approval policy requires an execution_domain_id",
            )
        if not isinstance(self.local_mounts, tuple):
            raise TypeError("local_mounts must be a tuple")
        for mount in self.local_mounts:
            if (
                not isinstance(mount, tuple)
                or len(mount) != 3
                or not isinstance(mount[0], str)
                or not mount[0]
                or not isinstance(mount[1], str)
                or not mount[1].startswith("/")
                or (mount[1] != "/" and mount[1].endswith("/"))
                or type(mount[2]) is not bool
            ):
                raise ValueError("local_mounts contains an invalid mount")

    @property
    def approval_enabled(self) -> bool:
        return self.host_execution_mode == "local_approval_required" and self.allow_host_bash is False

    @property
    def execution_timeout_seconds(self) -> int:
        return min(
            self.bash_command_timeout,
            self.approval_max_timeout_seconds,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "provider_use": self.provider_use,
            "host_execution_mode": self.host_execution_mode,
            "allow_host_bash": self.allow_host_bash,
            "bash_command_timeout": self.bash_command_timeout,
            "approval_max_timeout_seconds": (self.approval_max_timeout_seconds),
            "request_ttl_seconds": self.request_ttl_seconds,
            "execution_domain_id": self.execution_domain_id,
            "local_mounts": [
                {
                    "host_path": host_path,
                    "container_path": container_path,
                    "read_only": read_only,
                }
                for host_path, container_path, read_only in self.local_mounts
            ],
            "skills_container_path": self.skills_container_path,
        }

    @property
    def digest(self) -> str:
        return _canonical_digest(self.to_payload())

    @classmethod
    def from_payload(cls, payload: object) -> HostExecutionProviderPolicySnapshot:
        expected = {
            "schema_version",
            "provider_use",
            "host_execution_mode",
            "allow_host_bash",
            "bash_command_timeout",
            "approval_max_timeout_seconds",
            "request_ttl_seconds",
            "execution_domain_id",
            "local_mounts",
            "skills_container_path",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ValueError("invalid provider policy snapshot")
        if payload.get("schema_version") != _PROVIDER_POLICY_SCHEMA_VERSION:
            raise ValueError("unsupported provider policy snapshot")
        mounts_payload = payload.get("local_mounts")
        if not isinstance(mounts_payload, list):
            raise ValueError("invalid provider policy mounts")
        local_mounts: list[tuple[str, str, bool]] = []
        for mount in mounts_payload:
            if not isinstance(mount, dict) or set(mount) != {
                "host_path",
                "container_path",
                "read_only",
            }:
                raise ValueError("invalid provider policy mount")
            local_mounts.append(
                (
                    mount.get("host_path"),
                    mount.get("container_path"),
                    mount.get("read_only"),
                ),
            )
        return cls(
            provider_use=payload.get("provider_use"),
            host_execution_mode=payload.get("host_execution_mode"),
            allow_host_bash=payload.get("allow_host_bash"),
            bash_command_timeout=payload.get("bash_command_timeout"),
            approval_max_timeout_seconds=payload.get(
                "approval_max_timeout_seconds",
            ),
            request_ttl_seconds=payload.get("request_ttl_seconds"),
            execution_domain_id=payload.get("execution_domain_id"),
            local_mounts=tuple(local_mounts),
            skills_container_path=payload.get("skills_container_path"),
        )

    @classmethod
    def from_app_config(
        cls,
        app_config: AppConfig,
    ) -> HostExecutionProviderPolicySnapshot:
        """Build the app-owned snapshot from typed runtime configuration."""

        from deerflow.sandbox.security import resolve_host_bash_execution_mode

        sandbox = getattr(app_config, "sandbox", None)
        approval = getattr(sandbox, "host_execution_approval", None)
        if sandbox is None or approval is None:
            raise ValueError("sandbox host execution policy is unavailable")
        mode = resolve_host_bash_execution_mode(app_config)
        local_mounts = tuple(
            (
                (str(Path(mount.host_path).expanduser().resolve(strict=False)) if Path(mount.host_path).is_absolute() else mount.host_path),
                mount.container_path.rstrip("/") or "/",
                mount.read_only,
            )
            for mount in sandbox.mounts
        )
        skills_container_path = app_config.skills.container_path.rstrip("/") or "/"
        return cls(
            provider_use=getattr(sandbox, "use", None),
            host_execution_mode=mode.value,
            allow_host_bash=getattr(sandbox, "allow_host_bash", None),
            bash_command_timeout=getattr(sandbox, "bash_command_timeout", None),
            approval_max_timeout_seconds=getattr(
                approval,
                "max_timeout_seconds",
                None,
            ),
            request_ttl_seconds=getattr(
                approval,
                "request_ttl_seconds",
                None,
            ),
            execution_domain_id=getattr(
                approval,
                "execution_domain_id",
                None,
            ),
            local_mounts=local_mounts,
            skills_container_path=skills_container_path,
        )


def _private_envelope(
    plan: HostExecutionPlan,
    policy: HostExecutionProviderPolicySnapshot,
    execution_domain: HostExecutionDomainSnapshot,
) -> dict[str, object]:
    return {
        "schema_version": _PRIVATE_ENVELOPE_SCHEMA_VERSION,
        "plan": plan.to_private_payload(),
        "provider_policy": policy.to_payload(),
        "provider_policy_digest": policy.digest,
        "execution_domain": execution_domain.to_private_payload(),
    }


def _frozen_plan_from_row(
    row: ExecutionApprovalRequestRow,
) -> tuple[
    HostExecutionPlan,
    HostExecutionProviderPolicySnapshot,
    HostExecutionDomainSnapshot,
]:
    envelope = row.command_private_json
    expected = {
        "schema_version",
        "plan",
        "provider_policy",
        "provider_policy_digest",
        "execution_domain",
    }
    if not isinstance(envelope, dict) or set(envelope) != expected:
        raise ValueError("invalid host execution envelope")
    if envelope.get("schema_version") != _PRIVATE_ENVELOPE_SCHEMA_VERSION:
        raise ValueError("unsupported host execution envelope")
    policy = HostExecutionProviderPolicySnapshot.from_payload(
        envelope.get("provider_policy"),
    )
    if envelope.get("provider_policy_digest") != policy.digest:
        raise ValueError("provider policy snapshot digest mismatch")
    plan = HostExecutionPlan.from_private_payload(
        envelope.get("plan"),
        source_tool_call_id=row.tool_call_id,
        source_run_id=row.source_run_id,
        source_thread_id=row.thread_id,
    )
    if plan.execution_digest != row.command_digest:
        raise ValueError("frozen execution digest mismatch")
    if list(plan.agent_path) != row.source_agent_path:
        raise ValueError("frozen agent path mismatch")
    if plan.timeout_seconds != policy.execution_timeout_seconds:
        raise ValueError("frozen execution timeout does not match policy")
    execution_domain = HostExecutionDomainSnapshot.from_private_payload(
        envelope.get("execution_domain"),
    )
    if execution_domain.configured_id != policy.execution_domain_id:
        raise ValueError("execution domain does not match provider policy")
    if execution_domain.affinity != row.execution_domain_affinity:
        raise ValueError("execution domain does not match approval affinity")
    return plan, policy, execution_domain


def _result_payload(outcome: HostExecutionOutcome) -> dict[str, object]:
    stdout, stdout_truncated = _bounded_text(outcome.stdout)
    stderr, stderr_truncated = _bounded_text(outcome.stderr)
    result_text, result_text_truncated = _bounded_text(outcome.result_text)
    return {
        "schema_version": _RESULT_SCHEMA_VERSION,
        "status": outcome.status,
        "exit_code": outcome.exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "result_text": result_text,
        "reason_code": outcome.reason_code,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "result_text_truncated": result_text_truncated,
    }


def _outcome_from_receipt(
    row: ExecutionApprovalRequestRow,
    receipt: ExecutionApprovalResultReceiptRow | None,
) -> HostExecutionOutcome:
    if receipt is None:
        raise ValueError("terminal host execution receipt is missing")
    payload = receipt.result_private_json
    expected = {
        "schema_version",
        "status",
        "exit_code",
        "stdout",
        "stderr",
        "result_text",
        "reason_code",
        "stdout_truncated",
        "stderr_truncated",
        "result_text_truncated",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValueError("invalid host execution receipt")
    if payload.get("schema_version") != _RESULT_SCHEMA_VERSION:
        raise ValueError("unsupported host execution receipt")
    if _canonical_digest(payload) != receipt.result_digest:
        raise ValueError("host execution receipt digest mismatch")
    if row.status not in {"finished", "launch_failed"} or receipt.outcome != row.status or payload.get("status") != row.status or payload.get("exit_code") != receipt.exit_code or payload.get("reason_code") != receipt.public_error_code:
        raise ValueError("host execution receipt scope mismatch")
    for key in (
        "stdout_truncated",
        "stderr_truncated",
        "result_text_truncated",
    ):
        if type(payload.get(key)) is not bool:
            raise ValueError("invalid host execution receipt truncation flag")
    return HostExecutionOutcome(
        status=row.status,
        exit_code=payload.get("exit_code"),
        stdout=payload.get("stdout"),
        stderr=payload.get("stderr"),
        result_text=payload.get("result_text"),
        reason_code=payload.get("reason_code"),
    )


@dataclass(frozen=True, slots=True)
class ExecutionApprovalProjection:
    schema_version: int
    server_time: datetime
    approval: dict[str, object] | None


async def _asset_closure(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    owner_user_id: str,
    run_id: str,
) -> tuple[
    tuple[object, ...],
    tuple[object, ...],
    tuple[object, ...],
    tuple[object, ...],
    tuple[object, ...],
]:
    assets = (
        await session.execute(
            sa.select(RunAssetVersionRow)
            .where(
                RunAssetVersionRow.project_id == project_id,
                RunAssetVersionRow.owner_user_id == owner_user_id,
                RunAssetVersionRow.run_id == run_id,
            )
            .order_by(
                RunAssetVersionRow.asset_kind,
                RunAssetVersionRow.dependency_order,
            ),
        )
    ).scalars()
    asset_values = tuple(
        (
            row.asset_kind,
            row.dependency_order,
            row.asset_scope,
            str(row.asset_id),
            str(row.version_id),
            row.payload_checksum,
            row.catalog_generation,
        )
        for row in assets
    )
    grants = (
        await session.execute(
            sa.select(RunMcpGrantSnapshotRow)
            .where(
                RunMcpGrantSnapshotRow.project_id == project_id,
                RunMcpGrantSnapshotRow.owner_user_id == owner_user_id,
                RunMcpGrantSnapshotRow.run_id == run_id,
            )
            .order_by(
                RunMcpGrantSnapshotRow.mcp_version_id,
                RunMcpGrantSnapshotRow.credential_slot_id,
            ),
        )
    ).scalars()
    grant_values = tuple(
        (
            str(row.mcp_version_id),
            str(row.credential_slot_id),
            str(row.credential_grant_id),
            str(row.credential_version_id),
        )
        for row in grants
    )
    skill_credentials = (
        await session.execute(
            sa.select(RunSkillCredentialSnapshotRow)
            .where(
                RunSkillCredentialSnapshotRow.project_id == project_id,
                RunSkillCredentialSnapshotRow.owner_user_id == owner_user_id,
                RunSkillCredentialSnapshotRow.run_id == run_id,
            )
            .order_by(
                RunSkillCredentialSnapshotRow.skill_version_id,
                RunSkillCredentialSnapshotRow.secret_name,
            ),
        )
    ).scalars()
    skill_credential_values = tuple(
        (
            str(row.skill_id),
            str(row.skill_version_id),
            row.secret_name,
            str(row.skill_credential_binding_id),
            row.binding_revision,
            str(row.credential_id),
            str(row.credential_version_id),
        )
        for row in skill_credentials
    )
    model_snapshots = (
        await session.execute(
            sa.select(RunModelConfigSnapshotRow)
            .where(
                RunModelConfigSnapshotRow.project_id == project_id,
                RunModelConfigSnapshotRow.owner_user_id == owner_user_id,
                RunModelConfigSnapshotRow.run_id == run_id,
            )
            .order_by(RunModelConfigSnapshotRow.purpose),
        )
    ).scalars()
    model_snapshot_values = tuple(
        (
            row.purpose,
            row.logical_name,
            str(row.model_config_id),
            str(row.model_config_version_id),
            row.payload_checksum,
            str(row.credential_id) if row.credential_id is not None else None,
            (str(row.credential_version_id) if row.credential_version_id is not None else None),
            row.credential_env_key,
        )
        for row in model_snapshots
    )
    runtime_snapshots = (
        await session.execute(
            sa.select(RunRuntimePolicySnapshotRow)
            .where(
                RunRuntimePolicySnapshotRow.project_id == project_id,
                RunRuntimePolicySnapshotRow.owner_user_id == owner_user_id,
                RunRuntimePolicySnapshotRow.run_id == run_id,
            )
            .order_by(RunRuntimePolicySnapshotRow.section),
        )
    ).scalars()
    runtime_snapshot_values = tuple(
        (
            row.section,
            str(row.policy_version_id),
            row.schema_version,
            row.payload_checksum,
        )
        for row in runtime_snapshots
    )
    return (
        asset_values,
        grant_values,
        skill_credential_values,
        model_snapshot_values,
        runtime_snapshot_values,
    )


class WorkerHostExecutionApprovalPort(
    HostExecutionApprovalPort,
    HostExecutionContinuationPort,
):
    """Worker-owned adapter bound to one exact private Run Job lease."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        context: PrivateWorkContext,
        claim: JobClaim,
        thread_id: str,
        request_ttl_seconds: int,
        provider_policy: HostExecutionProviderPolicySnapshot,
        execution_domain: HostExecutionDomainSnapshot | None = None,
        continuation_approval_id: str | None = None,
        audit: HostExecutionApprovalAuditPort | None = None,
    ) -> None:
        if claim.run_id is None or claim.scope.owner_user_id is None:
            raise ValueError("private Run claim is required")
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("thread_id is required")
        if isinstance(request_ttl_seconds, bool) or not isinstance(request_ttl_seconds, int) or request_ttl_seconds <= 0:
            raise ValueError("request_ttl_seconds must be positive")
        if type(provider_policy) is not HostExecutionProviderPolicySnapshot:
            raise TypeError("provider_policy snapshot is required")
        if provider_policy.request_ttl_seconds != request_ttl_seconds:
            raise ValueError("request TTL does not match provider policy")
        if provider_policy.approval_enabled:
            if type(execution_domain) is not HostExecutionDomainSnapshot:
                raise TypeError(
                    "Local approval requires a Worker execution domain snapshot",
                )
            if execution_domain.configured_id != provider_policy.execution_domain_id:
                raise ValueError(
                    "Worker execution domain does not match provider policy",
                )
        elif execution_domain is not None:
            raise ValueError(
                "Worker execution domain is unavailable outside Local approval mode",
            )
        self._factory = session_factory
        self._context = context
        self._claim = claim
        self._thread_id = thread_id
        self._request_ttl_seconds = request_ttl_seconds
        self._provider_policy = provider_policy
        self._execution_domain = execution_domain
        self._continuation_approval_id = uuid.UUID(continuation_approval_id) if continuation_approval_id is not None else None
        self._continuation_consumed = False
        self._audit = audit or NoopHostExecutionApprovalAudit()

    def prepare_host_execution_environment(self) -> dict[str, str] | None:
        """Freeze and verify the sanitized Local environment for one spawn."""

        execution_domain = self._execution_domain
        if execution_domain is None:
            return None
        prepared = build_sandbox_env(None)
        if host_execution_environment_fingerprint(prepared) != execution_domain.environment_fingerprint:
            return None
        return prepared

    async def _lock_thread_scope_shell(
        self,
        session: AsyncSession,
        *,
        allow_deleted: bool = False,
    ) -> None:
        """Lock the exact Thread shell before any Job/Run/approval mutation."""

        predicates = [
            ThreadMetaRow.project_id == self._context.project_id,
            ThreadMetaRow.owner_user_id == str(self._context.user_id),
            ThreadMetaRow.thread_id == self._thread_id,
        ]
        if not allow_deleted:
            predicates.extend(
                (
                    ThreadMetaRow.deleted_at.is_(None),
                    ThreadMetaRow.frozen_at.is_(None),
                )
            )
        thread = await session.scalar(sa.select(ThreadMetaRow.thread_id).where(*predicates).with_for_update(of=ThreadMetaRow))
        if thread is None:
            if allow_deleted:
                raise RuntimeError(
                    "host execution Thread authority is unavailable",
                )
            raise PrivateRunExecutionLeaseLost

    async def _lock_completion_scope_shells(
        self,
        session: AsyncSession,
    ) -> None:
        """Preserve executed-outcome authority under the FK lock prefix.

        Completion is post-side-effect closure, so this deliberately accepts a
        deleting Project and a left/removed membership. Re-authorizing here
        could discard the only durable receipt after the host process ran.
        """

        project = await session.scalar(sa.select(ProjectRow.id).where(ProjectRow.id == self._context.project_id).with_for_update(of=ProjectRow))
        membership = await session.scalar(
            sa.select(ProjectMembershipRow.id)
            .where(
                ProjectMembershipRow.project_id == self._context.project_id,
                ProjectMembershipRow.user_id == str(self._context.user_id),
            )
            .with_for_update(of=ProjectMembershipRow)
        )
        if project is None or membership is None:
            raise RuntimeError("host execution private scope is unavailable")
        await self._lock_thread_scope_shell(session, allow_deleted=True)

    def _source_matches(self, plan: HostExecutionPlan) -> bool:
        return plan.source_run_id == self._claim.run_id and plan.source_thread_id == self._thread_id

    async def request_host_execution(
        self,
        plan: HostExecutionPlan,
    ) -> HostExecutionApprovalResult:
        if not self._source_matches(plan):
            return HostExecutionApprovalResult.denied("source_scope_mismatch")
        if self._continuation_approval_id is not None and not self._continuation_consumed:
            return HostExecutionApprovalResult.denied(
                "frozen_continuation_not_consumed",
            )
        return await self._stage(plan)

    async def _stage(
        self,
        plan: HostExecutionPlan,
    ) -> HostExecutionApprovalResult:
        if not self._provider_policy.approval_enabled or self._execution_domain is None or plan.timeout_seconds != self._provider_policy.execution_timeout_seconds:
            return HostExecutionApprovalResult.denied(
                "host_execution_policy_unavailable",
            )
        created_id = uuid.uuid4()
        try:
            async with self._factory() as session, session.begin():
                await PrivateWorkRevalidator().require(
                    session,
                    self._context,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.PRIVATE_WORK_APPROVE_HOST_EXECUTION,
                    lock=True,
                )
                await self._lock_thread_scope_shell(session)
                cancelled = await PrivateRunRepository(
                    session,
                ).assert_execution_active(
                    scope=self._context.resource_scope,
                    run_id=self._claim.run_id or "",
                    job_id=self._claim.job_id,
                    lease_token=self._claim.lease_token,
                )
                if cancelled:
                    return HostExecutionApprovalResult.denied(
                        "source_run_cancelled",
                    )
                existing = (
                    await session.execute(
                        sa.select(ExecutionApprovalRequestRow)
                        .where(
                            ExecutionApprovalRequestRow.project_id == self._context.project_id,
                            ExecutionApprovalRequestRow.owner_user_id == str(self._context.user_id),
                            ExecutionApprovalRequestRow.source_run_id == self._claim.run_id,
                            ExecutionApprovalRequestRow.tool_call_id == plan.source_tool_call_id,
                        )
                        .with_for_update(),
                        execution_options={"populate_existing": True},
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    created_at = await _database_now(session)
                    cancelled = await PrivateRunRepository(
                        session,
                    ).assert_execution_active(
                        scope=self._context.resource_scope,
                        run_id=self._claim.run_id or "",
                        job_id=self._claim.job_id,
                        lease_token=self._claim.lease_token,
                        now=created_at,
                    )
                    if cancelled:
                        return HostExecutionApprovalResult.denied(
                            "source_run_cancelled",
                        )
                    try:
                        (
                            existing_plan,
                            existing_policy,
                            existing_domain,
                        ) = _frozen_plan_from_row(
                            existing,
                        )
                    except (TypeError, ValueError):
                        if existing.status == "staged":
                            existing.status = "cancelled"
                            existing.version += 1
                            existing.terminal_at = created_at
                            existing.updated_at = created_at
                            await self._audit.host_execution_approval_terminal(
                                session,
                                project_id=existing.project_id,
                                source_run_id=existing.source_run_id,
                                status="cancelled",
                                request_id=self._claim.origin_trace_id,
                                occurred_at=created_at,
                            )
                        return HostExecutionApprovalResult.denied(
                            "approval_request_conflict",
                        )
                    exact_replay = (
                        existing.status in {"staged", "pending"}
                        and existing_plan == plan
                        and existing_policy == self._provider_policy
                        and existing_domain.affinity == self._execution_domain.affinity
                        and existing.source_job_id == self._claim.job_id
                    )
                    if not exact_replay:
                        if existing.status == "staged":
                            existing.status = "cancelled"
                            existing.version += 1
                            existing.terminal_at = created_at
                            existing.updated_at = created_at
                            await self._audit.host_execution_approval_terminal(
                                session,
                                project_id=existing.project_id,
                                source_run_id=existing.source_run_id,
                                status="cancelled",
                                request_id=self._claim.origin_trace_id,
                                occurred_at=created_at,
                            )
                        return HostExecutionApprovalResult.denied(
                            "approval_request_conflict",
                        )
                    if existing.status == "staged" and existing.source_job_attempt_id != self._claim.attempt_id:
                        existing.source_job_attempt_id = self._claim.attempt_id
                        existing.version += 1
                        existing.updated_at = created_at
                        await session.flush()
                    return HostExecutionApprovalResult.pending(
                        HostExecutionApprovalArtifact(
                            approval_id=str(existing.id),
                            source_run_id=existing.source_run_id,
                            source_tool_call_id=existing.tool_call_id,
                        ),
                    )
                active = (
                    await session.execute(
                        sa.select(ExecutionApprovalRequestRow.id)
                        .where(
                            ExecutionApprovalRequestRow.project_id == self._context.project_id,
                            ExecutionApprovalRequestRow.owner_user_id == str(self._context.user_id),
                            ExecutionApprovalRequestRow.thread_id == self._thread_id,
                            ExecutionApprovalRequestRow.status.in_(
                                ("staged", "pending", "approved", "claimed"),
                            ),
                        )
                        .with_for_update(),
                    )
                ).scalar_one_or_none()
                if active is not None:
                    return HostExecutionApprovalResult.denied(
                        "another_host_execution_is_active",
                    )
                created_at = await _database_now(session)
                cancelled = await PrivateRunRepository(
                    session,
                ).assert_execution_active(
                    scope=self._context.resource_scope,
                    run_id=self._claim.run_id or "",
                    job_id=self._claim.job_id,
                    lease_token=self._claim.lease_token,
                    now=created_at,
                )
                if cancelled:
                    return HostExecutionApprovalResult.denied(
                        "source_run_cancelled",
                    )
                session.add(
                    ExecutionApprovalRequestRow(
                        id=created_id,
                        project_id=self._context.project_id,
                        owner_user_id=str(self._context.user_id),
                        thread_id=self._thread_id,
                        source_run_id=self._claim.run_id,
                        source_job_id=self._claim.job_id,
                        source_job_attempt_id=self._claim.attempt_id,
                        source_agent_path=list(plan.agent_path),
                        tool_call_id=plan.source_tool_call_id,
                        kind="local_bash",
                        command_digest=plan.execution_digest,
                        execution_domain_affinity=(self._execution_domain.affinity),
                        command_private_json=_private_envelope(
                            plan,
                            self._provider_policy,
                            self._execution_domain,
                        ),
                        status="staged",
                        version=1,
                        expires_at=created_at + timedelta(seconds=self._request_ttl_seconds),
                        created_at=created_at,
                        updated_at=created_at,
                    ),
                )
                await session.flush()
                await self._audit.host_execution_approval_requested(
                    session,
                    project_id=self._context.project_id,
                    source_run_id=self._claim.run_id or "",
                    request_id=self._claim.origin_trace_id,
                    occurred_at=created_at,
                )
        except (IntegrityError, DBAPIError, PrivateRunExecutionLeaseLost):
            return HostExecutionApprovalResult.denied(
                "approval_storage_unavailable",
            )
        return HostExecutionApprovalResult.pending(
            HostExecutionApprovalArtifact(
                approval_id=str(created_id),
                source_run_id=self._claim.run_id or "",
                source_tool_call_id=plan.source_tool_call_id,
            ),
        )

    async def claim_frozen_host_execution(self) -> HostExecutionFrozenClaim:
        approval_id = self._continuation_approval_id
        if approval_id is None:
            return HostExecutionFrozenClaim.not_applicable()
        if self._execution_domain is None or self._claim.execution_domain_affinity != self._execution_domain.affinity:
            return HostExecutionFrozenClaim.denied(
                "host_execution_domain_mismatch",
            )
        try:
            async with self._factory() as session, session.begin():
                await PrivateWorkRevalidator().require(
                    session,
                    self._context,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.PRIVATE_WORK_APPROVE_HOST_EXECUTION,
                    lock=True,
                )
                await self._lock_thread_scope_shell(session)
                run_repository = PrivateRunRepository(session)
                cancelled = await run_repository.assert_execution_active(
                    scope=self._context.resource_scope,
                    run_id=self._claim.run_id or "",
                    job_id=self._claim.job_id,
                    lease_token=self._claim.lease_token,
                )
                if cancelled:
                    return HostExecutionFrozenClaim.denied(
                        "continuation_run_cancelled",
                    )
                row = (
                    await session.execute(
                        sa.select(ExecutionApprovalRequestRow)
                        .where(
                            ExecutionApprovalRequestRow.id == approval_id,
                            ExecutionApprovalRequestRow.project_id == self._context.project_id,
                            ExecutionApprovalRequestRow.owner_user_id == str(self._context.user_id),
                            ExecutionApprovalRequestRow.thread_id == self._thread_id,
                        )
                        .with_for_update(),
                    )
                ).scalar_one_or_none()
                if row is None:
                    return HostExecutionFrozenClaim.denied(
                        "approval_not_found",
                    )
                claimed_at = await _database_now(session)
                cancelled = await run_repository.assert_execution_active(
                    scope=self._context.resource_scope,
                    run_id=self._claim.run_id or "",
                    job_id=self._claim.job_id,
                    lease_token=self._claim.lease_token,
                    now=claimed_at,
                )
                if cancelled:
                    return HostExecutionFrozenClaim.denied(
                        "continuation_run_cancelled",
                    )
                try:
                    (
                        plan,
                        persisted_policy,
                        persisted_domain,
                    ) = _frozen_plan_from_row(row)
                except (TypeError, ValueError):
                    if row.status == "approved":
                        row.status = "cancelled"
                        row.version += 1
                        row.terminal_at = claimed_at
                        row.updated_at = claimed_at
                        await self._audit.host_execution_approval_terminal(
                            session,
                            project_id=row.project_id,
                            source_run_id=row.source_run_id,
                            status="cancelled",
                            request_id=self._claim.origin_trace_id,
                            occurred_at=claimed_at,
                        )
                    return HostExecutionFrozenClaim.denied(
                        "approval_payload_invalid",
                    )

                if row.status in {"finished", "launch_failed"}:
                    receipt = await session.scalar(
                        sa.select(ExecutionApprovalResultReceiptRow).where(
                            ExecutionApprovalResultReceiptRow.approval_id == row.id,
                        ),
                    )
                    try:
                        outcome = _outcome_from_receipt(row, receipt)
                    except (TypeError, ValueError):
                        return HostExecutionFrozenClaim.denied(
                            "approval_receipt_invalid",
                        )
                    self._continuation_consumed = True
                    return HostExecutionFrozenClaim.replay(
                        str(row.id),
                        plan,
                        outcome,
                    )
                if row.status == "claimed":
                    # A prior claim may have launched a Local process before
                    # its Worker disappeared. A renewable DB lease cannot
                    # prove that process has stopped; only the frozen command
                    # timeout plus settlement grace closes that uncertainty.
                    if claimed_at < claimed_execution_absolute_deadline(row):
                        return HostExecutionFrozenClaim.denied(
                            "host_execution_in_progress",
                        )
                    await reconcile_locked_execution_approval(
                        session,
                        row,
                        now=claimed_at,
                        audit=self._audit,
                    )
                    if row.status == "claimed":
                        return HostExecutionFrozenClaim.denied(
                            "host_execution_in_progress",
                        )
                    return HostExecutionFrozenClaim.denied(
                        "host_execution_state_unknown",
                    )
                if row.status != "approved" or row.decision != "allow_once":
                    return HostExecutionFrozenClaim.denied(
                        f"approval_{row.status}",
                    )
                if row.expires_at <= claimed_at:
                    row.status = "expired"
                    row.version += 1
                    row.terminal_at = claimed_at
                    row.updated_at = claimed_at
                    await self._audit.host_execution_approval_terminal(
                        session,
                        project_id=row.project_id,
                        source_run_id=row.source_run_id,
                        status="expired",
                        request_id=self._claim.origin_trace_id,
                        occurred_at=claimed_at,
                    )
                    return HostExecutionFrozenClaim.denied(
                        "approval_expired",
                    )
                if not self._provider_policy.approval_enabled or persisted_policy != self._provider_policy:
                    row.status = "cancelled"
                    row.version += 1
                    row.terminal_at = claimed_at
                    row.updated_at = claimed_at
                    await self._audit.host_execution_approval_terminal(
                        session,
                        project_id=row.project_id,
                        source_run_id=row.source_run_id,
                        status="cancelled",
                        request_id=self._claim.origin_trace_id,
                        occurred_at=claimed_at,
                    )
                    return HostExecutionFrozenClaim.denied(
                        "host_execution_policy_drift",
                    )
                if row.execution_domain_affinity != self._claim.execution_domain_affinity or persisted_domain.affinity != self._execution_domain.affinity:
                    row.status = "cancelled"
                    row.version += 1
                    row.terminal_at = claimed_at
                    row.updated_at = claimed_at
                    await self._audit.host_execution_approval_terminal(
                        session,
                        project_id=row.project_id,
                        source_run_id=row.source_run_id,
                        status="cancelled",
                        request_id=self._claim.origin_trace_id,
                        occurred_at=claimed_at,
                    )
                    return HostExecutionFrozenClaim.denied(
                        "host_execution_domain_mismatch",
                    )
                if row.continuation_run_id is None:
                    if row.continuation_job_id is not None:
                        return HostExecutionFrozenClaim.denied(
                            "approval_continuation_invalid",
                        )
                    row.continuation_run_id = self._claim.run_id
                    row.continuation_job_id = self._claim.job_id
                elif row.continuation_run_id != self._claim.run_id or row.continuation_job_id != self._claim.job_id:
                    return HostExecutionFrozenClaim.denied(
                        "approval_continuation_mismatch",
                    )
                source_closure = await _asset_closure(
                    session,
                    project_id=self._context.project_id,
                    owner_user_id=str(self._context.user_id),
                    run_id=row.source_run_id,
                )
                continuation_closure = await _asset_closure(
                    session,
                    project_id=self._context.project_id,
                    owner_user_id=str(self._context.user_id),
                    run_id=self._claim.run_id or "",
                )
                if not source_closure[0] or not source_closure[3] or not source_closure[4] or source_closure != continuation_closure:
                    row.status = "cancelled"
                    row.version += 1
                    row.terminal_at = claimed_at
                    row.updated_at = claimed_at
                    await self._audit.host_execution_approval_terminal(
                        session,
                        project_id=row.project_id,
                        source_run_id=row.source_run_id,
                        status="cancelled",
                        request_id=self._claim.origin_trace_id,
                        occurred_at=claimed_at,
                    )
                    return HostExecutionFrozenClaim.denied(
                        "host_execution_asset_closure_drift",
                    )
                claim_authorized_at = await _database_now(session)
                cancelled = await run_repository.assert_execution_active(
                    scope=self._context.resource_scope,
                    run_id=self._claim.run_id or "",
                    job_id=self._claim.job_id,
                    lease_token=self._claim.lease_token,
                    now=claim_authorized_at,
                )
                if cancelled:
                    return HostExecutionFrozenClaim.denied(
                        "continuation_run_cancelled",
                    )
                if row.expires_at <= claim_authorized_at:
                    row.status = "expired"
                    row.version += 1
                    row.terminal_at = claim_authorized_at
                    row.updated_at = claim_authorized_at
                    await self._audit.host_execution_approval_terminal(
                        session,
                        project_id=row.project_id,
                        source_run_id=row.source_run_id,
                        status="expired",
                        request_id=self._claim.origin_trace_id,
                        occurred_at=claim_authorized_at,
                    )
                    return HostExecutionFrozenClaim.denied(
                        "approval_expired",
                    )
                cancelled = await run_repository.mark_execution_side_effect_unknown(
                    scope=self._context.resource_scope,
                    run_id=self._claim.run_id or "",
                    job_id=self._claim.job_id,
                    lease_token=self._claim.lease_token,
                    now=claim_authorized_at,
                )
                if cancelled:
                    return HostExecutionFrozenClaim.denied(
                        "continuation_run_cancelled",
                    )
                # The pre-spawn lease check above samples time after all
                # authority locks. Freeze the command deadline at that final
                # boundary, never at transaction entry before lock waits.
                claimed_at = claim_authorized_at
                row.status = "claimed"
                row.version += 1
                row.execution_job_attempt_id = self._claim.attempt_id
                row.claimed_at = claimed_at
                row.updated_at = claimed_at
                await self._audit.host_execution_approval_claimed(
                    session,
                    project_id=row.project_id,
                    source_run_id=row.source_run_id,
                    request_id=self._claim.origin_trace_id,
                    occurred_at=claimed_at,
                )
                await session.flush()
        except (DBAPIError, PrivateRunExecutionLeaseLost):
            return HostExecutionFrozenClaim.denied(
                "approval_claim_unavailable",
            )
        self._continuation_consumed = True
        return HostExecutionFrozenClaim.claimed(str(approval_id), plan)

    async def authorize_claimed_host_execution_spawn(
        self,
        approval_id: str,
    ) -> float | None:
        """Revalidate exact durable authority at the process-create boundary.

        Claim time starts the frozen command's absolute lifecycle deadline.
        Final authorization must therefore complete within the existing
        settlement-grace preparation window. A process starting before that
        boundary and consuming its full timeout still ends no later than the
        canonical claimed execution deadline used by reconciliation.
        """

        try:
            parsed_id = uuid.UUID(approval_id)
        except (TypeError, ValueError):
            return None
        execution_domain = self._execution_domain
        if parsed_id != self._continuation_approval_id or execution_domain is None or self._claim.execution_domain_affinity != execution_domain.affinity:
            return None
        token_hash = hashlib.sha256(
            self._claim.lease_token.encode(),
        ).hexdigest()
        try:
            async with self._factory() as session, session.begin():
                await PrivateWorkRevalidator().require(
                    session,
                    self._context,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.PRIVATE_WORK_APPROVE_HOST_EXECUTION,
                    lock=True,
                )
                await self._lock_thread_scope_shell(session)
                run_repository = PrivateRunRepository(session)
                cancelled = await run_repository.assert_execution_active(
                    scope=self._context.resource_scope,
                    run_id=self._claim.run_id or "",
                    job_id=self._claim.job_id,
                    lease_token=self._claim.lease_token,
                )
                if cancelled:
                    return None
                job = (
                    await session.execute(
                        sa.select(JobRow)
                        .where(
                            JobRow.id == self._claim.job_id,
                            JobRow.project_id == self._context.project_id,
                            JobRow.owner_user_id == str(self._context.user_id),
                            JobRow.run_id == self._claim.run_id,
                            JobRow.execution_domain_affinity == execution_domain.affinity,
                        )
                        .with_for_update(of=JobRow),
                        execution_options={"populate_existing": True},
                    )
                ).scalar_one_or_none()
                run = (
                    await session.execute(
                        sa.select(RunRow)
                        .where(
                            RunRow.project_id == self._context.project_id,
                            RunRow.owner_user_id == str(self._context.user_id),
                            RunRow.thread_id == self._thread_id,
                            RunRow.run_id == self._claim.run_id,
                            RunRow.job_id == self._claim.job_id,
                        )
                        .with_for_update(of=RunRow),
                        execution_options={"populate_existing": True},
                    )
                ).scalar_one_or_none()
                if job is None or run is None:
                    return None
                attempt = (
                    await session.execute(
                        sa.select(JobAttemptRow)
                        .where(
                            JobAttemptRow.id == self._claim.attempt_id,
                            JobAttemptRow.job_id == self._claim.job_id,
                        )
                        .with_for_update(of=JobAttemptRow),
                        execution_options={"populate_existing": True},
                    )
                ).scalar_one_or_none()
                if attempt is None or attempt.attempt_number != job.attempt_count or attempt.lease_token_hash != token_hash or attempt.finished_at is not None or attempt.outcome is not None:
                    return None
                row = (
                    await session.execute(
                        sa.select(ExecutionApprovalRequestRow)
                        .where(
                            ExecutionApprovalRequestRow.id == parsed_id,
                            ExecutionApprovalRequestRow.project_id == self._context.project_id,
                            ExecutionApprovalRequestRow.owner_user_id == str(self._context.user_id),
                            ExecutionApprovalRequestRow.thread_id == self._thread_id,
                            ExecutionApprovalRequestRow.continuation_run_id == self._claim.run_id,
                            ExecutionApprovalRequestRow.continuation_job_id == self._claim.job_id,
                            ExecutionApprovalRequestRow.execution_job_attempt_id == self._claim.attempt_id,
                            ExecutionApprovalRequestRow.execution_domain_affinity == execution_domain.affinity,
                            ExecutionApprovalRequestRow.status == "claimed",
                            ExecutionApprovalRequestRow.decision == "allow_once",
                        )
                        .with_for_update(of=ExecutionApprovalRequestRow),
                        execution_options={"populate_existing": True},
                    )
                ).scalar_one_or_none()
                if row is None or row.claimed_at is None:
                    return None
                try:
                    _plan, persisted_policy, persisted_domain = _frozen_plan_from_row(row)
                except (TypeError, ValueError):
                    return None
                if persisted_policy != self._provider_policy or persisted_domain.affinity != execution_domain.affinity:
                    return None

                # The approval lock can wait behind a concurrent lifecycle
                # transaction. Sample a fresh clock only after every authority
                # row is locked, then re-check the already locked Job/Run lease.
                authorized_at = await _database_now(session)
                cancelled = await run_repository.assert_execution_active(
                    scope=self._context.resource_scope,
                    run_id=self._claim.run_id or "",
                    job_id=self._claim.job_id,
                    lease_token=self._claim.lease_token,
                    now=authorized_at,
                )
                if cancelled:
                    return None
                preparation_deadline = row.claimed_at.astimezone(
                    UTC,
                ) + timedelta(
                    seconds=(CLAIMED_EXECUTION_SETTLEMENT_GRACE_SECONDS),
                )
                if job.lease_expires_at is None or run.execution_lease_expires_at is None:
                    return None
                launch_deadline = min(
                    preparation_deadline,
                    job.lease_expires_at.astimezone(UTC),
                    run.execution_lease_expires_at.astimezone(UTC),
                )
                remaining_seconds = (launch_deadline - authorized_at).total_seconds()
                return remaining_seconds if remaining_seconds > 0 else None
        except (
            DBAPIError,
            PrivateRunExecutionLeaseLost,
            PrivateWorkError,
        ):
            return None

    async def _lock_completion_lease(
        self,
        session: AsyncSession,
    ) -> tuple[JobRow, RunRow, JobAttemptRow]:
        token_hash = hashlib.sha256(self._claim.lease_token.encode()).hexdigest()
        job = await session.scalar(
            sa.select(JobRow)
            .where(
                JobRow.id == self._claim.job_id,
                JobRow.project_id == self._context.project_id,
                JobRow.owner_user_id == str(self._context.user_id),
                JobRow.run_id == self._claim.run_id,
            )
            .with_for_update(),
        )
        if job is None:
            raise RuntimeError("host execution Job is unavailable")
        run = await session.scalar(
            sa.select(RunRow)
            .where(
                RunRow.project_id == self._context.project_id,
                RunRow.owner_user_id == str(self._context.user_id),
                RunRow.thread_id == self._thread_id,
                RunRow.run_id == self._claim.run_id,
                RunRow.job_id == self._claim.job_id,
            )
            .with_for_update(),
        )
        attempt = await session.scalar(
            sa.select(JobAttemptRow)
            .where(
                JobAttemptRow.id == self._claim.attempt_id,
                JobAttemptRow.job_id == self._claim.job_id,
            )
            .with_for_update(),
        )
        if (
            run is None
            or attempt is None
            or job.status not in {"leased", "running"}
            or run.status != "running"
            or job.lease_token_hash != token_hash
            or run.execution_lease_token_hash != token_hash
            or attempt.lease_token_hash != token_hash
            or attempt.finished_at is not None
        ):
            raise RuntimeError("host execution completion lease was lost")
        return job, run, attempt

    async def complete_host_execution(
        self,
        approval_id: str,
        outcome: HostExecutionOutcome,
    ) -> None:
        try:
            parsed_id = uuid.UUID(approval_id)
        except (TypeError, ValueError) as error:
            raise ValueError("approval_id must be a UUID") from error
        if type(outcome) is not HostExecutionOutcome:
            raise TypeError("HostExecutionOutcome is required")
        result_payload = _result_payload(outcome)
        try:
            async with self._factory() as session, session.begin():
                await self._lock_completion_scope_shells(session)
                job, _run, _attempt = await self._lock_completion_lease(session)
                row = (
                    await session.execute(
                        sa.select(ExecutionApprovalRequestRow)
                        .where(
                            ExecutionApprovalRequestRow.id == parsed_id,
                            ExecutionApprovalRequestRow.project_id == self._context.project_id,
                            ExecutionApprovalRequestRow.owner_user_id == str(self._context.user_id),
                            ExecutionApprovalRequestRow.thread_id == self._thread_id,
                            ExecutionApprovalRequestRow.continuation_run_id == self._claim.run_id,
                            ExecutionApprovalRequestRow.continuation_job_id == self._claim.job_id,
                            ExecutionApprovalRequestRow.execution_job_attempt_id == self._claim.attempt_id,
                        )
                        .with_for_update(),
                    )
                ).scalar_one_or_none()
                if row is None:
                    raise RuntimeError("claimed host execution is unavailable")
                existing_receipt = await session.scalar(
                    sa.select(ExecutionApprovalResultReceiptRow)
                    .where(
                        ExecutionApprovalResultReceiptRow.approval_id == row.id,
                    )
                    .with_for_update(),
                )
                completed_at = await _database_now(session)
                if row.status in {"finished", "launch_failed"}:
                    existing = _outcome_from_receipt(row, existing_receipt)
                    if _result_payload(existing) != result_payload:
                        raise RuntimeError(
                            "host execution completion conflicts with receipt",
                        )
                    if job.retry_safety != "safe":
                        job.retry_safety = "safe"
                        job.updated_at = completed_at
                    return
                if row.status != "claimed" or existing_receipt is not None:
                    raise RuntimeError("host execution is not claim-completable")
                if outcome.status == "unknown":
                    # The runner can report unknown after process creation if
                    # it cannot prove termination. Keep the claimed gate until
                    # the same frozen, non-renewable deadline used by lazy
                    # reconciliation; Run failure must not authorize replay.
                    if completed_at < claimed_execution_absolute_deadline(row):
                        return
                    row.status = "unknown"
                    row.version += 1
                    row.terminal_at = completed_at
                    row.updated_at = completed_at
                    await self._audit.host_execution_approval_terminal(
                        session,
                        project_id=row.project_id,
                        source_run_id=row.source_run_id,
                        status="unknown",
                        request_id=self._claim.origin_trace_id,
                        occurred_at=completed_at,
                    )
                    await session.flush()
                    return
                session.add(
                    ExecutionApprovalResultReceiptRow(
                        approval_id=row.id,
                        project_id=row.project_id,
                        owner_user_id=row.owner_user_id,
                        thread_id=row.thread_id,
                        execution_job_id=self._claim.job_id,
                        execution_job_attempt_id=self._claim.attempt_id,
                        outcome=outcome.status,
                        exit_code=outcome.exit_code,
                        result_digest=_canonical_digest(result_payload),
                        result_private_json=result_payload,
                        public_error_code=outcome.reason_code,
                        created_at=completed_at,
                    ),
                )
                row.status = outcome.status
                row.version += 1
                row.terminal_at = completed_at
                row.updated_at = completed_at
                job.retry_safety = "safe"
                job.updated_at = completed_at
                await self._audit.host_execution_approval_terminal(
                    session,
                    project_id=row.project_id,
                    source_run_id=row.source_run_id,
                    status=outcome.status,
                    request_id=self._claim.origin_trace_id,
                    occurred_at=completed_at,
                )
                await session.flush()
        except (IntegrityError, DBAPIError) as error:
            raise RuntimeError(
                "host execution completion was not persisted",
            ) from error


async def settle_staged_execution_approvals(
    session: AsyncSession,
    *,
    claim: JobClaim,
    succeeded: bool,
    request_ttl_seconds: int,
    audit: HostExecutionApprovalAuditPort | None = None,
) -> None:
    """Activate staged requests only after their source Run succeeds."""

    if claim.run_id is None or claim.scope.owner_user_id is None:
        return
    rows = tuple(
        (
            await session.execute(
                sa.select(ExecutionApprovalRequestRow)
                .where(
                    ExecutionApprovalRequestRow.project_id == claim.scope.project_id,
                    ExecutionApprovalRequestRow.owner_user_id == claim.scope.owner_user_id,
                    ExecutionApprovalRequestRow.source_run_id == claim.run_id,
                    ExecutionApprovalRequestRow.source_job_id == claim.job_id,
                    ExecutionApprovalRequestRow.status == "staged",
                )
                .with_for_update(),
                execution_options={"populate_existing": True},
            )
        ).scalars()
    )
    settled_at = await _database_now(session)
    approval_audit = audit or NoopHostExecutionApprovalAudit()
    for row in rows:
        available = succeeded and row.source_job_attempt_id == claim.attempt_id
        row.status = "pending" if available else "cancelled"
        row.version += 1
        row.expires_at = settled_at + timedelta(seconds=request_ttl_seconds)
        row.terminal_at = None if available else settled_at
        row.updated_at = settled_at
        if available:
            await approval_audit.host_execution_approval_available(
                session,
                project_id=row.project_id,
                source_run_id=row.source_run_id,
                request_id=claim.origin_trace_id,
                occurred_at=settled_at,
            )
        else:
            await approval_audit.host_execution_approval_terminal(
                session,
                project_id=row.project_id,
                source_run_id=row.source_run_id,
                status="cancelled",
                request_id=claim.origin_trace_id,
                occurred_at=settled_at,
            )


class ExecutionApprovalService:
    """Owner-scoped Gateway reads and approval-first decisions."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        admission: PrivateRunAdmissionService,
        provider_policy: HostExecutionProviderPolicySnapshot,
        quota: ExecutionApprovalContinuationQuotaPort,
        run_audit: ExecutionApprovalContinuationRunAuditPort,
        audit: HostExecutionApprovalAuditPort | None = None,
    ) -> None:
        if type(provider_policy) is not HostExecutionProviderPolicySnapshot:
            raise TypeError("provider_policy snapshot is required")
        self._factory = session_factory
        self._admission = admission
        self._provider_policy = provider_policy
        self._quota = quota
        self._run_audit = run_audit
        self._audit = audit or NoopHostExecutionApprovalAudit()

    @staticmethod
    async def _lock_thread(
        session: AsyncSession,
        context: PrivateWorkContext,
        thread_id: str,
    ) -> None:
        from app.private_work.thread_repository import PrivateThreadRepository

        thread = await PrivateThreadRepository(session).get(
            scope=context.resource_scope,
            thread_id=thread_id,
            lock=True,
        )
        if thread is None:
            raise ExecutionApprovalNotFound(context.request_id)

    @staticmethod
    async def _locked_row(
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        thread_id: str,
        approval_id: uuid.UUID,
    ) -> tuple[ExecutionApprovalRequestRow | None, LockedExecutionApprovalRows]:
        try:
            locked = await lock_execution_approval_private_rows(
                session,
                project_id=context.project_id,
                owner_user_id=str(context.user_id),
                thread_id=thread_id,
                approval_id=approval_id,
            )
        except ExecutionApprovalPrivateLifecycleConflict:
            raise PrivateWorkUnavailable(context.request_id) from None
        return (locked.rows[0] if locked.rows else None), locked

    async def _reconcile_if_needed(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        row: ExecutionApprovalRequestRow,
        *,
        locked: LockedExecutionApprovalRows,
        now: datetime,
    ) -> None:
        try:
            await reconcile_locked_execution_approval_and_continuation(
                session,
                row,
                locked=locked,
                context=context,
                now=now,
                quota=self._quota,
                run_audit=self._run_audit,
                approval_audit=self._audit,
            )
        except ExecutionApprovalPrivateLifecycleConflict:
            raise PrivateWorkUnavailable(context.request_id) from None

    @staticmethod
    async def _project(
        session: AsyncSession,
        context: PrivateWorkContext,
        row: ExecutionApprovalRequestRow | None,
        *,
        now: datetime,
    ) -> ExecutionApprovalProjection:
        if row is None:
            return ExecutionApprovalProjection(1, now, None)
        try:
            plan, _policy, execution_domain = _frozen_plan_from_row(row)
        except (TypeError, ValueError):
            raise PrivateWorkUnavailable(context.request_id) from None
        agent_path = list(plan.agent_path)
        is_subagent = len(agent_path) > 1 or agent_path[0] != "lead"
        continuation: dict[str, object] | None = None
        if row.continuation_run_id is not None:
            run = await PrivateRunRepository(session).get(
                scope=context.resource_scope,
                run_id=row.continuation_run_id,
            )
            if run is not None:
                continuation = {"run_id": run.run_id, "status": run.status}
        public_status = "pending" if row.status == "staged" else row.status
        approval: dict[str, object] = {
            "approval_id": str(row.id),
            "source_run_id": row.source_run_id,
            "source_tool_call_id": row.tool_call_id,
            "status": public_status,
            "version": str(row.version),
            "execution_domain": {
                "label": execution_domain.public_label,
                "effective_user_label": "Worker process identity",
            },
            "command_preview": plan.requested_command,
            "cwd_preview": "/mnt/user-data/workspace",
            "timeout_seconds": plan.timeout_seconds,
            "source_agent": {
                "kind": "subagent" if is_subagent else "lead",
                "label": (agent_path[-1].removeprefix("subagent:") if is_subagent else "Project Assistant"),
                "path": agent_path,
            },
            "risk_level": "host_execution",
            "warning_code": "LOCAL_PROCESS_RUNS_ON_HOST",
            "can_decide": (row.status == "pending" and Capability.PRIVATE_WORK_APPROVE_HOST_EXECUTION in context.capabilities),
            "continuation_run": continuation,
        }
        if row.status in {"staged", "pending"}:
            approval.update(
                {
                    "decision_expires_at": row.expires_at.isoformat(),
                    "remaining_ttl_seconds": max(
                        0,
                        int((row.expires_at - now).total_seconds()),
                    ),
                },
            )
        elif row.status == "approved":
            approval.update(
                {
                    "decision_at": (row.decided_at or row.updated_at).isoformat(),
                    "claim_expires_at": row.expires_at.isoformat(),
                },
            )
        elif row.status == "claimed":
            approval["claimed_at"] = (row.claimed_at or row.updated_at).isoformat()
        elif row.status in {"finished", "launch_failed"}:
            receipt = await session.scalar(
                sa.select(ExecutionApprovalResultReceiptRow).where(
                    ExecutionApprovalResultReceiptRow.approval_id == row.id,
                ),
            )
            try:
                outcome = _outcome_from_receipt(row, receipt)
            except (TypeError, ValueError):
                raise PrivateWorkUnavailable(context.request_id) from None
            approval["finished_at"] = (row.terminal_at or row.updated_at).isoformat()
            if outcome.status == "finished":
                approval["exit_code"] = outcome.exit_code
                approval["result_summary_code"] = "PROCESS_EXITED"
            else:
                approval["reason_code"] = outcome.reason_code
        elif row.status == "unknown":
            approval.update(
                {
                    "finished_at": (row.terminal_at or row.updated_at).isoformat(),
                    "warning_code": "HOST_EXECUTION_STATE_UNKNOWN",
                },
            )
        elif row.status == "denied":
            approval.update(
                {
                    "decision_at": (row.decided_at or row.updated_at).isoformat(),
                    "denial_delivery_status": "not_required",
                },
            )
        elif row.status in {"expired", "cancelled"}:
            approval.update(
                {
                    "finished_at": (row.terminal_at or row.updated_at).isoformat(),
                    "reason_code": (("DECISION_TTL_EXPIRED" if row.decision is None else "CLAIM_TTL_EXPIRED") if row.status == "expired" else ("SOURCE_RUN_CANCELLED" if row.decision is None else "CLAIM_OR_POLICY_INVALIDATED")),
                },
            )
        return ExecutionApprovalProjection(1, now, approval)

    async def active(
        self,
        context: PrivateWorkContext,
        thread_id: str,
    ) -> ExecutionApprovalProjection:
        try:
            async with self._factory() as session, session.begin():
                await PrivateWorkRevalidator().require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_READ_OWN,
                    lock=True,
                )
                await self._lock_thread(session, context, thread_id)
                try:
                    locked = await lock_execution_approval_private_rows(
                        session,
                        project_id=context.project_id,
                        owner_user_id=str(context.user_id),
                        thread_id=thread_id,
                        active_only=True,
                    )
                except ExecutionApprovalPrivateLifecycleConflict:
                    raise PrivateWorkUnavailable(context.request_id) from None
                now = await _database_now(session)
                row = locked.rows[0] if locked.rows else None
                if row is not None:
                    await self._reconcile_if_needed(
                        session,
                        context,
                        row,
                        locked=locked,
                        now=now,
                    )
                    if row.status not in {"staged", "pending", "approved", "claimed"}:
                        row = None
                return await self._project(session, context, row, now=now)
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def get(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        approval_id: uuid.UUID,
    ) -> ExecutionApprovalProjection:
        try:
            async with self._factory() as session, session.begin():
                await PrivateWorkRevalidator().require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_READ_OWN,
                    lock=True,
                )
                await self._lock_thread(session, context, thread_id)
                row, locked = await self._locked_row(
                    session,
                    context,
                    thread_id=thread_id,
                    approval_id=approval_id,
                )
                if row is None:
                    raise ExecutionApprovalNotFound(context.request_id)
                now = await _database_now(session)
                await self._reconcile_if_needed(
                    session,
                    context,
                    row,
                    locked=locked,
                    now=now,
                )
                return await self._project(session, context, row, now=now)
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def decide(
        self,
        context: PrivateWorkContext,
        *,
        thread_id: str,
        source_run_id: str,
        approval_id: uuid.UUID,
        decision: Literal["allow_once", "deny"],
        expected_version: int,
        idempotency_key: uuid.UUID,
    ) -> ExecutionApprovalProjection:
        if expected_version < 1:
            raise ExecutionApprovalConflict(context.request_id)
        idempotency_digest = _idempotency_digest(idempotency_key)
        request_digest = _decision_digest(
            approval_id=approval_id,
            source_run_id=source_run_id,
            decision=decision,
            expected_version=expected_version,
        )
        source_model_name: str | None = None
        should_admit = False
        admission_digest: str | None = None
        execution_domain_affinity: str | None = None
        channel_user_id: str | None = None
        try:
            async with self._factory() as session, session.begin():
                await PrivateWorkRevalidator().require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.PRIVATE_WORK_APPROVE_HOST_EXECUTION,
                    lock=True,
                )
                await self._lock_thread(session, context, thread_id)
                row, locked = await self._locked_row(
                    session,
                    context,
                    thread_id=thread_id,
                    approval_id=approval_id,
                )
                if row is None or row.source_run_id != source_run_id:
                    raise ExecutionApprovalNotFound(context.request_id)
                decided_at = await _database_now(session)
                status_before_reconciliation = row.status
                await self._reconcile_if_needed(
                    session,
                    context,
                    row,
                    locked=locked,
                    now=decided_at,
                )
                if row.status != status_before_reconciliation and row.status in {"expired", "cancelled", "unknown"}:
                    # Commit lifecycle convergence and its audit append before
                    # returning the now-terminal server projection.
                    return await self._project(
                        session,
                        context,
                        row,
                        now=decided_at,
                    )
                if row.decision_idempotency_key == idempotency_digest:
                    if row.decision_request_digest != request_digest:
                        raise ExecutionApprovalConflict(context.request_id)
                    if row.decision != decision:
                        raise ExecutionApprovalConflict(context.request_id)
                    if decision == "allow_once" and row.status == "approved":
                        try:
                            (
                                _plan,
                                persisted_policy,
                                persisted_domain,
                            ) = _frozen_plan_from_row(row)
                        except (TypeError, ValueError):
                            raise ExecutionApprovalConflict(
                                context.request_id,
                            ) from None
                        if not self._provider_policy.approval_enabled or persisted_policy != self._provider_policy:
                            raise ExecutionApprovalConflict(context.request_id)
                        if persisted_domain.affinity != row.execution_domain_affinity:
                            raise ExecutionApprovalConflict(context.request_id)
                    if decision != "allow_once" or row.status != "approved" or row.continuation_run_id is not None:
                        return await self._project(
                            session,
                            context,
                            row,
                            now=decided_at,
                        )
                    should_admit = True
                    admission_digest = row.decision_request_digest
                elif decision == "allow_once" and row.status == "approved" and row.decision == "allow_once" and row.continuation_run_id is None and row.version == expected_version:
                    # The decision is already durable, but the first Gateway
                    # may have failed before deterministic continuation
                    # admission. A refreshed owner may resume that admission
                    # with a new browser idempotency key without replacing the
                    # original decision evidence.
                    try:
                        (
                            _plan,
                            persisted_policy,
                            persisted_domain,
                        ) = _frozen_plan_from_row(row)
                    except (TypeError, ValueError):
                        raise ExecutionApprovalConflict(
                            context.request_id,
                        ) from None
                    if row.expires_at <= decided_at or not self._provider_policy.approval_enabled or persisted_policy != self._provider_policy or not row.decision_request_digest:
                        raise ExecutionApprovalConflict(context.request_id)
                    if persisted_domain.affinity != row.execution_domain_affinity:
                        raise ExecutionApprovalConflict(context.request_id)
                    should_admit = True
                    admission_digest = row.decision_request_digest
                else:
                    if row.status == "expired":
                        # Reconciliation and its audit append share this
                        # transaction. Returning the terminal projection lets
                        # the context manager commit both before the caller
                        # observes expiry; raising here would roll them back.
                        return await self._project(
                            session,
                            context,
                            row,
                            now=decided_at,
                        )
                    if row.status != "pending" or row.version != expected_version:
                        raise ExecutionApprovalConflict(context.request_id)
                    try:
                        (
                            _plan,
                            persisted_policy,
                            persisted_domain,
                        ) = _frozen_plan_from_row(row)
                    except (TypeError, ValueError):
                        raise ExecutionApprovalConflict(context.request_id) from None
                    if not self._provider_policy.approval_enabled or persisted_policy != self._provider_policy:
                        raise ExecutionApprovalConflict(context.request_id)
                    if persisted_domain.affinity != row.execution_domain_affinity:
                        raise ExecutionApprovalConflict(context.request_id)
                    if decision == "deny":
                        row.status = "denied"
                        row.decision = "deny"
                        row.decision_idempotency_key = idempotency_digest
                        row.decision_request_digest = request_digest
                        row.decided_by_user_id = str(context.user_id)
                        row.decided_at = decided_at
                        row.terminal_at = decided_at
                        row.version += 1
                        row.updated_at = decided_at
                        await self._audit.host_execution_approval_decided(
                            session,
                            context,
                            source_run_id=row.source_run_id,
                            decision="deny",
                            occurred_at=decided_at,
                        )
                        await session.flush()
                        return await self._project(
                            session,
                            context,
                            row,
                            now=decided_at,
                        )
                    row.status = "approved"
                    row.decision = "allow_once"
                    row.decision_idempotency_key = idempotency_digest
                    row.decision_request_digest = request_digest
                    row.decided_by_user_id = str(context.user_id)
                    row.decided_at = decided_at
                    row.expires_at = decided_at + timedelta(
                        seconds=_CLAIM_TTL_SECONDS,
                    )
                    row.version += 1
                    row.updated_at = decided_at
                    await self._audit.host_execution_approval_decided(
                        session,
                        context,
                        source_run_id=row.source_run_id,
                        decision="allow_once",
                        occurred_at=decided_at,
                    )
                    await session.flush()
                    should_admit = True
                    admission_digest = request_digest
                if should_admit:
                    execution_domain_affinity = row.execution_domain_affinity
                    try:
                        continuation_plan, _policy, _domain = _frozen_plan_from_row(row)
                    except (TypeError, ValueError):
                        raise ExecutionApprovalConflict(
                            context.request_id,
                        ) from None
                    if continuation_plan.channel_identity_mode == "set":
                        channel_user_id = continuation_plan.channel_user_id
                source_run = await PrivateRunRepository(session).get(
                    scope=context.resource_scope,
                    run_id=source_run_id,
                    lock=True,
                )
                if source_run is None or source_run.thread_id != thread_id or source_run.status != "success":
                    raise ExecutionApprovalConflict(context.request_id)
                source_model_name = source_run.model_name
        except PrivateWorkError:
            raise
        except IntegrityError:
            raise ExecutionApprovalConflict(context.request_id) from None
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

        if not should_admit:  # pragma: no cover - all branches return or admit
            raise ExecutionApprovalConflict(context.request_id)
        if admission_digest is None:  # pragma: no cover - guarded above
            raise ExecutionApprovalConflict(context.request_id)
        if execution_domain_affinity is None:  # pragma: no cover - guarded above
            raise ExecutionApprovalConflict(context.request_id)
        continuation_run_id = str(
            uuid.uuid5(approval_id, _CONTINUATION_NAME),
        )
        continuation = await self._admission.admit(
            context,
            thread_id,
            PrivateRunCreate(
                run_id=continuation_run_id,
                metadata={"execution_approval_continuation": True},
                kwargs={
                    "input": {"messages": []},
                    "config": {},
                    "command": None,
                    "stream_mode": ["values", "messages-tuple", "custom"],
                    "stream_subgraphs": True,
                },
                execution_profile=RequestedRunExecutionProfile(
                    model_name=source_model_name,
                ),
            ),
            server_context=PrivateRunAdmissionServerContext(
                host_execution_approval_id=approval_id,
                host_execution_decision_digest=admission_digest,
                host_execution_domain_affinity=execution_domain_affinity,
                channel_user_id=channel_user_id,
            ),
        )
        try:
            async with self._factory() as session, session.begin():
                await PrivateWorkRevalidator().require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.PRIVATE_WORK_APPROVE_HOST_EXECUTION,
                    lock=True,
                )
                await self._lock_thread(session, context, thread_id)
                row, locked = await self._locked_row(
                    session,
                    context,
                    thread_id=thread_id,
                    approval_id=approval_id,
                )
                linked_at = await _database_now(session)
                if row is not None:
                    status_before_reconciliation = row.status
                    await self._reconcile_if_needed(
                        session,
                        context,
                        row,
                        locked=locked,
                        now=linked_at,
                    )
                    if row.status != status_before_reconciliation and row.status in {"expired", "cancelled", "unknown"}:
                        return await self._project(
                            session,
                            context,
                            row,
                            now=linked_at,
                        )
                if (
                    row is None
                    or row.status != "approved"
                    or row.source_run_id != source_run_id
                    or row.decision != "allow_once"
                    or row.decision_request_digest != admission_digest
                    or row.continuation_run_id != continuation.run.run_id
                    or row.continuation_job_id != continuation.job.job_id
                ):
                    raise ExecutionApprovalConflict(context.request_id)
                return await self._project(
                    session,
                    context,
                    row,
                    now=linked_at,
                )
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None


__all__ = [
    "ExecutionApprovalProjection",
    "ExecutionApprovalService",
    "HostExecutionProviderPolicySnapshot",
    "WorkerHostExecutionApprovalPort",
    "settle_staged_execution_approvals",
]
