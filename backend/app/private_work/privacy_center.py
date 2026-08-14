"""Account-scoped former-member privacy center.

Only the authenticated account's ended memberships are visible. Exports use a
PostgreSQL repeatable-read snapshot and stream NDJSON records. File chunks are
encoded one at a time, so a valid 100 MiB file never requires a 100 MiB Python
buffer. Credential envelopes, OAuth tokens, and secret-bearing tables are never
queried. Host-execution JSON is projected through an explicit scalar allowlist;
provider policy, host paths, digests, and environment-key names stay private.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import case, func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    AsyncSessionTransaction,
)

from app.private_work.retention_jobs import (
    RetentionJobAdmission,
    former_owner_retention_key,
)
from deerflow.persistence.channel_connections.model import (
    ChannelConnectionRow,
    ChannelConversationRow,
)
from deerflow.persistence.execution_approvals import (
    ExecutionApprovalRequestRow,
    ExecutionApprovalResultReceiptRow,
)
from deerflow.persistence.jobs.model import JobRow
from deerflow.persistence.models.run_event import RunEventRow
from deerflow.persistence.private_work.memory_document_model import (
    MemoryDocumentRow,
    MemoryDocumentVersionRow,
    MemoryDreamRunRow,
    MemoryEpisodeRow,
    MemoryHistoryEntryRow,
    RunMemoryContextSnapshotRow,
)
from deerflow.persistence.private_work.model import (
    PrivateArtifactRow,
    PrivateFileChunkRow,
    PrivateFileRow,
)
from deerflow.persistence.projects.model import (
    ProjectMembershipRow,
    ProjectRow,
)
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.scheduled_tasks.model import ScheduledTaskRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow

_EARLY_ACTIVE_STATUSES = ("queued", "leased", "running", "retry_wait")
_STREAM_BATCH_SIZE = 100
_SENSITIVE_METADATA_TERMS = (
    "authorization",
    "ciphertext",
    "cookie",
    "credential",
    "envelope",
    "key_id",
    "nonce",
    "password",
    "refresh_token",
    "secret",
    "storage_locator",
    "token",
)


class PrivacyCaseNotFound(RuntimeError):
    def __init__(self) -> None:
        super().__init__("PRIVACY_CASE_NOT_FOUND")


@dataclass(frozen=True, slots=True)
class PrivacyCaseView:
    project_id: uuid.UUID
    project_slug: str
    project_display_name: str
    project_icon: str
    membership_status: str
    retention_kind: str
    deletion_deadline: datetime
    early_delete_requested: bool


@dataclass(frozen=True, slots=True)
class PrivacyEarlyDeleteView:
    project_id: uuid.UUID
    job_id: uuid.UUID
    status: str


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("privacy timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else _aware(value).isoformat()


def _safe_export_metadata(value):
    """Remove system secret-bearing fields from otherwise private metadata."""

    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if any(term in normalized for term in _SENSITIVE_METADATA_TERMS):
                continue
            result[str(key)] = _safe_export_metadata(item)
        return result
    if isinstance(value, list):
        return [_safe_export_metadata(item) for item in value]
    if isinstance(value, tuple):
        return [_safe_export_metadata(item) for item in value]
    return value


def _export_line(record_type: str, **payload) -> bytes:
    return (
        json.dumps(
            {"record_type": record_type, **payload},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


class PrivacyCenterService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        retention_jobs: object = RetentionJobAdmission,
    ) -> None:
        self._session = session
        self._retention_jobs = retention_jobs

    @staticmethod
    def _deadline(
        project: ProjectRow,
        membership: ProjectMembershipRow,
    ) -> tuple[str, datetime] | None:
        if membership.retention_until is None:
            return None
        member_deadline = _aware(membership.retention_until)
        if project.status == "pending_deletion" and project.deletion_effective_at is not None:
            project_deadline = _aware(project.deletion_effective_at)
            if project_deadline <= member_deadline:
                return "project", project_deadline
        return "former_owner", member_deadline

    @staticmethod
    def _early_key(
        project: ProjectRow,
        membership: ProjectMembershipRow,
    ) -> str:
        if membership.retention_until is None:
            raise PrivacyCaseNotFound
        return former_owner_retention_key(
            project_id=project.id,
            owner_user_id=membership.user_id,
            membership_id=membership.id,
            activation_generation=membership.activation_generation,
            retention_until=membership.retention_until,
            early_delete=True,
        )

    async def list_cases(
        self,
        user_id: uuid.UUID,
        *,
        now: datetime,
    ) -> tuple[PrivacyCaseView, ...]:
        current = _aware(now)
        owner_user_id = str(uuid.UUID(str(user_id)))
        async with self._session.begin():
            rows = (
                await self._session.execute(
                    select(ProjectRow, ProjectMembershipRow)
                    .join(
                        ProjectMembershipRow,
                        ProjectMembershipRow.project_id == ProjectRow.id,
                    )
                    .where(
                        ProjectMembershipRow.user_id == owner_user_id,
                        ProjectMembershipRow.status.in_(("left", "removed")),
                        ProjectMembershipRow.retention_until.is_not(None),
                    )
                    .order_by(ProjectRow.display_name, ProjectRow.id)
                )
            ).all()
            keyed: list[
                tuple[
                    ProjectRow,
                    ProjectMembershipRow,
                    str,
                    datetime,
                    str,
                ]
            ] = []
            for row in rows:
                deadline = self._deadline(
                    row.ProjectRow,
                    row.ProjectMembershipRow,
                )
                if deadline is None or deadline[1] <= current:
                    continue
                keyed.append(
                    (
                        row.ProjectRow,
                        row.ProjectMembershipRow,
                        deadline[0],
                        deadline[1],
                        self._early_key(
                            row.ProjectRow,
                            row.ProjectMembershipRow,
                        ),
                    )
                )
            if not keyed:
                return ()
            job_rows = (
                await self._session.execute(
                    select(JobRow.idempotency_key, JobRow.status).where(
                        JobRow.job_type == "retention_purge",
                        JobRow.idempotency_key.in_(item[4] for item in keyed),
                    )
                )
            ).all()
            statuses: dict[str, set[str]] = {}
            for job in job_rows:
                statuses.setdefault(job.idempotency_key, set()).add(job.status)
            result: list[PrivacyCaseView] = []
            for project, membership, kind, deadline, early_key in keyed:
                case_statuses = statuses.get(early_key, set())
                if "succeeded" in case_statuses:
                    continue
                result.append(
                    PrivacyCaseView(
                        project_id=project.id,
                        project_slug=project.slug,
                        project_display_name=project.display_name,
                        project_icon=project.icon,
                        membership_status=membership.status,
                        retention_kind=kind,
                        deletion_deadline=deadline,
                        early_delete_requested=bool(
                            case_statuses.intersection(
                                _EARLY_ACTIVE_STATUSES,
                            )
                        ),
                    )
                )
            return tuple(result)

    async def _find_case(
        self,
        *,
        user_id: uuid.UUID,
        project_id: uuid.UUID,
        now: datetime,
        lock: bool,
    ) -> tuple[ProjectRow, ProjectMembershipRow]:
        project_uuid = uuid.UUID(str(project_id))
        owner_user_id = str(uuid.UUID(str(user_id)))
        project_statement = select(ProjectRow).where(
            ProjectRow.id == project_uuid,
        )
        membership_statement = select(ProjectMembershipRow).where(
            ProjectMembershipRow.project_id == project_uuid,
            ProjectMembershipRow.user_id == owner_user_id,
        )
        if lock:
            project_statement = project_statement.with_for_update(
                of=ProjectRow,
            )
            membership_statement = membership_statement.with_for_update(
                of=ProjectMembershipRow,
            )
        project = await self._session.scalar(project_statement)
        if project is None:
            raise PrivacyCaseNotFound
        membership = await self._session.scalar(membership_statement)
        deadline = None if membership is None else self._deadline(project, membership)
        if membership is None or membership.status not in {"left", "removed"} or membership.retention_until is None or deadline is None or deadline[1] <= _aware(now):
            raise PrivacyCaseNotFound
        early_succeeded = await self._session.scalar(
            select(JobRow.id).where(
                JobRow.job_type == "retention_purge",
                JobRow.idempotency_key == self._early_key(project, membership),
                JobRow.status == "succeeded",
            )
        )
        if early_succeeded is not None:
            raise PrivacyCaseNotFound
        return project, membership

    async def request_early_delete(
        self,
        user_id: uuid.UUID,
        project_id: uuid.UUID,
        *,
        now: datetime,
    ) -> PrivacyEarlyDeleteView:
        requested_at = _aware(now)
        async with self._session.begin():
            project, membership = await self._find_case(
                user_id=user_id,
                project_id=project_id,
                now=requested_at,
                lock=True,
            )
            job_id = await self._retention_jobs.admit_early_delete(
                self._session,
                project_id=project.id,
                owner_user_id=membership.user_id,
                membership_id=membership.id,
                activation_generation=membership.activation_generation,
                retention_until=membership.retention_until,
                now=requested_at,
            )
            status = await self._session.scalar(select(JobRow.status).where(JobRow.id == job_id))
            if status is None:
                raise RuntimeError(
                    "retention early-delete admission is missing",
                )
            return PrivacyEarlyDeleteView(
                project_id=project.id,
                job_id=job_id,
                status=status,
            )

    async def open_case_export(
        self,
        user_id: uuid.UUID,
        project_id: uuid.UUID,
        *,
        now: datetime,
    ) -> AsyncIterator[bytes]:
        """Authorize before headers and hold one read-only export snapshot."""

        generated_at = _aware(now)
        transaction = await self._session.begin()
        try:
            # PostgreSQL-only product boundary. No row locks are retained for
            # the download, while MVCC keeps all records snapshot-consistent
            # if the Worker purges immediately after authorization.
            await self._session.execute(
                text(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",
                )
            )
            project, membership = await self._find_case(
                user_id=user_id,
                project_id=project_id,
                now=generated_at,
                lock=False,
            )
        except BaseException:
            await transaction.rollback()
            raise
        return self._stream_export(
            transaction,
            project=project,
            membership=membership,
            generated_at=generated_at,
        )

    async def _scalar_rows(self, statement):
        stream = await self._session.stream_scalars(
            statement.execution_options(yield_per=_STREAM_BATCH_SIZE),
        )
        try:
            async for row in stream:
                yield row
        finally:
            await stream.close()

    async def _stream_export(
        self,
        transaction: AsyncSessionTransaction,
        *,
        project: ProjectRow,
        membership: ProjectMembershipRow,
        generated_at: datetime,
    ) -> AsyncIterator[bytes]:
        scope = (project.id, membership.user_id)
        try:
            yield _export_line(
                "manifest",
                schema_version=3,
                format="deer-flow-privacy-ndjson",
                generated_at=generated_at.isoformat(),
                project={
                    "id": str(project.id),
                    "slug": project.slug,
                    "display_name": project.display_name,
                    "icon": project.icon,
                },
                membership={
                    "status": membership.status,
                    "ended_at": _iso(membership.ended_at),
                    "retention_until": _iso(membership.retention_until),
                },
            )

            threads = (
                select(ThreadMetaRow)
                .where(
                    ThreadMetaRow.project_id == scope[0],
                    ThreadMetaRow.owner_user_id == scope[1],
                )
                .order_by(ThreadMetaRow.created_at, ThreadMetaRow.thread_id)
            )
            async for row in self._scalar_rows(threads):
                yield _export_line(
                    "thread",
                    data={
                        "thread_id": row.thread_id,
                        "display_name": row.display_name,
                        "status": row.status,
                        "created_at": _iso(row.created_at),
                        "updated_at": _iso(row.updated_at),
                    },
                )

            runs = (
                select(RunRow)
                .where(
                    RunRow.project_id == scope[0],
                    RunRow.owner_user_id == scope[1],
                )
                .order_by(RunRow.created_at, RunRow.run_id)
            )
            async for row in self._scalar_rows(runs):
                yield _export_line(
                    "run",
                    data={
                        "run_id": row.run_id,
                        "thread_id": row.thread_id,
                        "status": row.status,
                        "first_human_message": row.first_human_message,
                        "last_ai_message": row.last_ai_message,
                        "created_at": _iso(row.created_at),
                        "updated_at": _iso(row.updated_at),
                    },
                )

            # Never load the complete frozen command envelope. PostgreSQL
            # projects only the user-facing logical fields approved for this
            # owner export; effective host commands, cwd/shell, provider
            # policy, digests, and environment-key names remain inaccessible.
            command_plan = ExecutionApprovalRequestRow.command_private_json["plan"]
            plan_description = command_plan["description"]
            plan_requested_command = command_plan["requested_command"]
            plan_timeout_seconds = command_plan["timeout_seconds"]
            approval_plans = (
                select(
                    ExecutionApprovalRequestRow.id,
                    ExecutionApprovalRequestRow.thread_id,
                    ExecutionApprovalRequestRow.source_run_id,
                    ExecutionApprovalRequestRow.tool_call_id,
                    ExecutionApprovalRequestRow.kind,
                    ExecutionApprovalRequestRow.status,
                    ExecutionApprovalRequestRow.decision,
                    ExecutionApprovalRequestRow.expires_at,
                    ExecutionApprovalRequestRow.decided_at,
                    ExecutionApprovalRequestRow.continuation_run_id,
                    ExecutionApprovalRequestRow.terminal_at,
                    ExecutionApprovalRequestRow.created_at,
                    case(
                        (
                            func.json_typeof(plan_description) == "string",
                            plan_description.as_string(),
                        ),
                    ).label("description"),
                    case(
                        (
                            func.json_typeof(plan_requested_command) == "string",
                            plan_requested_command.as_string(),
                        ),
                    ).label("requested_command"),
                    case(
                        (
                            func.json_typeof(plan_timeout_seconds) == "number",
                            plan_timeout_seconds.as_integer(),
                        ),
                    ).label("timeout_seconds"),
                    ExecutionApprovalRequestRow.source_agent_path,
                )
                .where(
                    ExecutionApprovalRequestRow.project_id == scope[0],
                    ExecutionApprovalRequestRow.owner_user_id == scope[1],
                )
                .order_by(
                    ExecutionApprovalRequestRow.created_at,
                    ExecutionApprovalRequestRow.id,
                )
            )
            approval_plan_rows = await self._session.stream(
                approval_plans.execution_options(
                    yield_per=_STREAM_BATCH_SIZE,
                )
            )
            try:
                async for row in approval_plan_rows:
                    yield _export_line(
                        "execution_approval_plan",
                        data={
                            "approval_id": str(row.id),
                            "thread_id": row.thread_id,
                            "source_run_id": row.source_run_id,
                            "source_tool_call_id": row.tool_call_id,
                            "kind": row.kind,
                            "status": row.status,
                            "decision": row.decision,
                            "description": row.description,
                            "requested_command": row.requested_command,
                            "timeout_seconds": row.timeout_seconds,
                            "source_agent_path": list(
                                row.source_agent_path,
                            ),
                            "continuation_run_id": row.continuation_run_id,
                            "expires_at": _iso(row.expires_at),
                            "decided_at": _iso(row.decided_at),
                            "terminal_at": _iso(row.terminal_at),
                            "created_at": _iso(row.created_at),
                        },
                    )
            finally:
                await approval_plan_rows.close()

            # Result output is owner-private user data, like Run events and
            # files. Select only the frozen result fields; execution Job IDs,
            # result digests, and any future envelope additions are excluded.
            result_json = ExecutionApprovalResultReceiptRow.result_private_json
            result_stdout = result_json["stdout"]
            result_stderr = result_json["stderr"]
            result_text = result_json["result_text"]
            result_reason_code = result_json["reason_code"]
            stdout_truncated = result_json["stdout_truncated"]
            stderr_truncated = result_json["stderr_truncated"]
            result_text_truncated = result_json["result_text_truncated"]
            approval_results = (
                select(
                    ExecutionApprovalResultReceiptRow.id,
                    ExecutionApprovalResultReceiptRow.approval_id,
                    ExecutionApprovalResultReceiptRow.thread_id,
                    ExecutionApprovalResultReceiptRow.outcome,
                    ExecutionApprovalResultReceiptRow.exit_code,
                    ExecutionApprovalResultReceiptRow.created_at,
                    case(
                        (
                            func.json_typeof(result_stdout) == "string",
                            result_stdout.as_string(),
                        ),
                    ).label("stdout"),
                    case(
                        (
                            func.json_typeof(result_stderr) == "string",
                            result_stderr.as_string(),
                        ),
                    ).label("stderr"),
                    case(
                        (
                            func.json_typeof(result_text) == "string",
                            result_text.as_string(),
                        ),
                    ).label("result_text"),
                    case(
                        (
                            func.json_typeof(result_reason_code) == "string",
                            result_reason_code.as_string(),
                        ),
                    ).label("reason_code"),
                    case(
                        (
                            func.json_typeof(stdout_truncated) == "boolean",
                            stdout_truncated.as_boolean(),
                        ),
                    ).label("stdout_truncated"),
                    case(
                        (
                            func.json_typeof(stderr_truncated) == "boolean",
                            stderr_truncated.as_boolean(),
                        ),
                    ).label("stderr_truncated"),
                    case(
                        (
                            func.json_typeof(result_text_truncated) == "boolean",
                            result_text_truncated.as_boolean(),
                        ),
                    ).label("result_text_truncated"),
                )
                .where(
                    ExecutionApprovalResultReceiptRow.project_id == scope[0],
                    ExecutionApprovalResultReceiptRow.owner_user_id == scope[1],
                )
                .order_by(
                    ExecutionApprovalResultReceiptRow.created_at,
                    ExecutionApprovalResultReceiptRow.id,
                )
            )
            approval_result_rows = await self._session.stream(
                approval_results.execution_options(
                    yield_per=_STREAM_BATCH_SIZE,
                )
            )
            try:
                async for row in approval_result_rows:
                    yield _export_line(
                        "execution_approval_result",
                        data={
                            "receipt_id": str(row.id),
                            "approval_id": str(row.approval_id),
                            "thread_id": row.thread_id,
                            "outcome": row.outcome,
                            "exit_code": row.exit_code,
                            "stdout": row.stdout,
                            "stderr": row.stderr,
                            "result_text": row.result_text,
                            "reason_code": row.reason_code,
                            "stdout_truncated": row.stdout_truncated,
                            "stderr_truncated": row.stderr_truncated,
                            "result_text_truncated": (row.result_text_truncated),
                            "created_at": _iso(row.created_at),
                        },
                    )
            finally:
                await approval_result_rows.close()

            events = (
                select(RunEventRow)
                .where(
                    RunEventRow.project_id == scope[0],
                    RunEventRow.owner_user_id == scope[1],
                )
                .order_by(RunEventRow.thread_id, RunEventRow.seq)
            )
            async for row in self._scalar_rows(events):
                yield _export_line(
                    "event",
                    data={
                        "thread_id": row.thread_id,
                        "run_id": row.run_id,
                        "seq": row.seq,
                        "event_type": row.event_type,
                        "category": row.category,
                        # Private user content is intentionally exportable.
                        "content": row.content,
                        "metadata": _safe_export_metadata(
                            row.event_metadata,
                        ),
                        "created_at": _iso(row.created_at),
                    },
                )

            memory_documents = (
                select(MemoryDocumentRow)
                .where(
                    MemoryDocumentRow.project_id == scope[0],
                    MemoryDocumentRow.owner_user_id == scope[1],
                )
                .order_by(
                    MemoryDocumentRow.namespace,
                )
            )
            async for row in self._scalar_rows(memory_documents):
                yield _export_line(
                    "memory_document",
                    data={
                        "namespace": row.namespace,
                        "content": row.content,
                        "version": row.version,
                        "dream_cursor": row.dream_cursor,
                        "updated_at": _iso(row.updated_at),
                    },
                )

            memory_versions = (
                select(MemoryDocumentVersionRow)
                .where(
                    MemoryDocumentVersionRow.project_id == scope[0],
                    MemoryDocumentVersionRow.owner_user_id == scope[1],
                )
                .order_by(
                    MemoryDocumentVersionRow.namespace,
                    MemoryDocumentVersionRow.version,
                )
            )
            async for row in self._scalar_rows(memory_versions):
                yield _export_line(
                    "memory_document_version",
                    data={
                        "namespace": row.namespace,
                        "version": row.version,
                        "content": row.content,
                        "unified_diff": row.unified_diff,
                        "trigger": row.trigger,
                        "history_from": row.history_from,
                        "history_to": row.history_to,
                        "history_count": row.history_count,
                        "created_at": _iso(row.created_at),
                    },
                )

            memory_history = (
                select(MemoryHistoryEntryRow)
                .where(
                    MemoryHistoryEntryRow.project_id == scope[0],
                    MemoryHistoryEntryRow.owner_user_id == scope[1],
                )
                .order_by(MemoryHistoryEntryRow.sequence)
            )
            async for row in self._scalar_rows(memory_history):
                yield _export_line(
                    "memory_history_entry",
                    data={
                        "id": str(row.id),
                        "sequence": row.sequence,
                        "namespace": row.namespace,
                        "thread_id": row.thread_id,
                        "status": row.status,
                        "tagged_text": row.tagged_text,
                        "created_at": _iso(row.created_at),
                        "consumed_at": _iso(row.consumed_at),
                    },
                )

            memory_episodes = (
                select(MemoryEpisodeRow)
                .where(
                    MemoryEpisodeRow.project_id == scope[0],
                    MemoryEpisodeRow.owner_user_id == scope[1],
                )
                .order_by(
                    MemoryEpisodeRow.namespace,
                    MemoryEpisodeRow.occurred_at,
                    MemoryEpisodeRow.id,
                )
            )
            async for row in self._scalar_rows(memory_episodes):
                yield _export_line(
                    "memory_episode",
                    data={
                        "id": str(row.id),
                        "namespace": row.namespace,
                        "thread_id": row.thread_id,
                        "origin": row.origin,
                        "tagged_text": row.tagged_text,
                        "occurred_at": _iso(row.occurred_at),
                        "created_at": _iso(row.created_at),
                    },
                )

            dream_runs = (
                select(MemoryDreamRunRow)
                .where(
                    MemoryDreamRunRow.project_id == scope[0],
                    MemoryDreamRunRow.owner_user_id == scope[1],
                )
                .order_by(MemoryDreamRunRow.created_at, MemoryDreamRunRow.job_id)
            )
            async for row in self._scalar_rows(dream_runs):
                yield _export_line(
                    "memory_dream_run",
                    data={
                        "job_id": str(row.job_id),
                        "namespace": row.namespace,
                        "trigger": row.trigger,
                        "history_from": row.history_from,
                        "history_to": row.history_to,
                        "history_count": row.history_count,
                        "base_document_version": row.base_document_version,
                        "result_version": row.result_version,
                        "created_at": _iso(row.created_at),
                        "completed_at": _iso(row.completed_at),
                    },
                )

            memory_snapshots = (
                select(RunMemoryContextSnapshotRow)
                .where(
                    RunMemoryContextSnapshotRow.project_id == scope[0],
                    RunMemoryContextSnapshotRow.owner_user_id == scope[1],
                )
                .order_by(RunMemoryContextSnapshotRow.created_at, RunMemoryContextSnapshotRow.run_id)
            )
            async for row in self._scalar_rows(memory_snapshots):
                yield _export_line(
                    "run_memory_context_snapshot",
                    data={
                        "run_id": row.run_id,
                        "namespace": row.namespace,
                        "document_version": row.document_version,
                        "content": row.content,
                        "created_at": _iso(row.created_at),
                    },
                )

            files = (
                select(PrivateFileRow)
                .where(
                    PrivateFileRow.project_id == scope[0],
                    PrivateFileRow.owner_user_id == scope[1],
                )
                .order_by(PrivateFileRow.created_at, PrivateFileRow.id)
            )
            async for row in self._scalar_rows(files):
                yield _export_line(
                    "file",
                    data={
                        "id": str(row.id),
                        "thread_id": row.thread_id,
                        "kind": row.kind,
                        "logical_path": row.logical_path,
                        "media_type": row.media_type,
                        "size": row.size,
                        "sha256": row.sha256,
                        "status": row.status,
                        "created_at": _iso(row.created_at),
                    },
                )

            chunks = (
                select(PrivateFileChunkRow)
                .join(
                    PrivateFileRow,
                    PrivateFileRow.id == PrivateFileChunkRow.file_id,
                )
                .where(
                    PrivateFileRow.project_id == scope[0],
                    PrivateFileRow.owner_user_id == scope[1],
                )
                .order_by(
                    PrivateFileChunkRow.file_id,
                    PrivateFileChunkRow.chunk_index,
                )
            )
            async for row in self._scalar_rows(chunks):
                yield _export_line(
                    "file_chunk",
                    data={
                        "file_id": str(row.file_id),
                        "chunk_index": row.chunk_index,
                        "size": row.size,
                        "sha256": row.sha256,
                        "content_base64": base64.b64encode(
                            row.content,
                        ).decode("ascii"),
                    },
                )

            artifacts = (
                select(PrivateArtifactRow)
                .where(
                    PrivateArtifactRow.project_id == scope[0],
                    PrivateArtifactRow.owner_user_id == scope[1],
                )
                .order_by(
                    PrivateArtifactRow.created_at,
                    PrivateArtifactRow.id,
                )
            )
            async for row in self._scalar_rows(artifacts):
                yield _export_line(
                    "artifact",
                    data={
                        "id": str(row.id),
                        "thread_id": row.thread_id,
                        "run_id": row.run_id,
                        "file_id": str(row.file_id),
                        "display_name": row.display_name,
                        "media_type": row.media_type,
                        "metadata": _safe_export_metadata(
                            row.artifact_metadata,
                        ),
                        "created_at": _iso(row.created_at),
                    },
                )

            automations = (
                select(ScheduledTaskRow)
                .where(
                    ScheduledTaskRow.project_id == scope[0],
                    ScheduledTaskRow.owner_user_id == scope[1],
                )
                .order_by(ScheduledTaskRow.created_at, ScheduledTaskRow.id)
            )
            async for row in self._scalar_rows(automations):
                yield _export_line(
                    "automation",
                    data={
                        "id": str(row.id),
                        "title": row.title,
                        "prompt": row.prompt,
                        "schedule_type": row.schedule_type,
                        "schedule_spec": _safe_export_metadata(
                            row.schedule_spec,
                        ),
                        "timezone": row.timezone,
                        "status": row.status,
                        "created_at": _iso(row.created_at),
                    },
                )

            # Select only allowlisted connection columns. OAuth tokens,
            # encrypted token material, provider payloads, and credential
            # references never enter the ORM identity map or export stream.
            connection_statement = (
                select(
                    ChannelConnectionRow.id,
                    ChannelConnectionRow.provider,
                    ChannelConnectionRow.status,
                    ChannelConnectionRow.external_account_name,
                    ChannelConnectionRow.workspace_name,
                    ChannelConnectionRow.created_at,
                )
                .where(
                    ChannelConnectionRow.project_id == scope[0],
                    ChannelConnectionRow.owner_user_id == scope[1],
                )
                .order_by(
                    ChannelConnectionRow.created_at,
                    ChannelConnectionRow.id,
                )
            )
            connection_rows = await self._session.stream(
                connection_statement.execution_options(
                    yield_per=_STREAM_BATCH_SIZE,
                )
            )
            try:
                async for row in connection_rows:
                    yield _export_line(
                        "connection",
                        data={
                            "id": str(row.id),
                            "provider": row.provider,
                            "status": row.status,
                            "external_account_name": (row.external_account_name),
                            "workspace_name": row.workspace_name,
                            "created_at": _iso(row.created_at),
                        },
                    )
            finally:
                await connection_rows.close()

            conversations = (
                select(ChannelConversationRow)
                .where(
                    ChannelConversationRow.project_id == scope[0],
                    ChannelConversationRow.owner_user_id == scope[1],
                )
                .order_by(
                    ChannelConversationRow.created_at,
                    ChannelConversationRow.id,
                )
            )
            async for row in self._scalar_rows(conversations):
                yield _export_line(
                    "channel_conversation",
                    data={
                        "id": str(row.id),
                        "connection_id": str(row.connection_id),
                        "provider": row.provider,
                        "thread_id": row.thread_id,
                        "created_at": _iso(row.created_at),
                    },
                )
        finally:
            if transaction.is_active:
                await transaction.rollback()


__all__ = [
    "PrivacyCaseNotFound",
    "PrivacyCaseView",
    "PrivacyCenterService",
    "PrivacyEarlyDeleteView",
]
