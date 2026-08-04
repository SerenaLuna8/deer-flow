"""Account-scoped former-member privacy center.

Only the authenticated account's ended memberships are visible. Exports use a
PostgreSQL repeatable-read snapshot and stream NDJSON records. File chunks are
encoded one at a time, so a valid 100 MiB file never requires a 100 MiB Python
buffer. Credential, envelope, OAuth-token, and secret-bearing tables are never
queried.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    AsyncSessionTransaction,
)

from app.private_work.memory_v2_export import iter_memory_v2_export_records
from app.private_work.retention_jobs import (
    RetentionJobAdmission,
    former_owner_retention_key,
)
from deerflow.persistence.channel_connections.model import (
    ChannelConnectionRow,
    ChannelConversationRow,
)
from deerflow.persistence.jobs.model import JobRow
from deerflow.persistence.models.run_event import RunEventRow
from deerflow.persistence.private_work.model import (
    PrivateArtifactRow,
    PrivateFileChunkRow,
    PrivateFileRow,
    UserProjectMemoryFactRow,
    UserProjectMemoryRow,
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
                schema_version=2,
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

            memories = (
                select(UserProjectMemoryRow)
                .where(
                    UserProjectMemoryRow.project_id == scope[0],
                    UserProjectMemoryRow.owner_user_id == scope[1],
                )
                .order_by(
                    UserProjectMemoryRow.created_at,
                    UserProjectMemoryRow.id,
                )
            )
            async for row in self._scalar_rows(memories):
                yield _export_line(
                    "memory",
                    data={
                        "id": str(row.id),
                        "namespace": row.namespace,
                        "context_summary": row.context_summary,
                        "created_at": _iso(row.created_at),
                        "updated_at": _iso(row.updated_at),
                    },
                )

            facts = (
                select(UserProjectMemoryFactRow)
                .where(
                    UserProjectMemoryFactRow.project_id == scope[0],
                    UserProjectMemoryFactRow.owner_user_id == scope[1],
                )
                .order_by(
                    UserProjectMemoryFactRow.created_at,
                    UserProjectMemoryFactRow.id,
                )
            )
            async for row in self._scalar_rows(facts):
                yield _export_line(
                    "memory_fact",
                    data={
                        "id": str(row.id),
                        "memory_id": str(row.memory_id),
                        "content": row.content,
                        "category": row.category,
                        "confidence": row.confidence,
                        "source_thread_id": row.source_thread_id,
                        "source_run_id": row.source_run_id,
                        "created_at": _iso(row.created_at),
                    },
                )

            async for record_type, data in iter_memory_v2_export_records(
                self._session,
                project_id=scope[0],
                owner_user_id=scope[1],
                namespace=None,
            ):
                yield _export_line(record_type, data=data)

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
