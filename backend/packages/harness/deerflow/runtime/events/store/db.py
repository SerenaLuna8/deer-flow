"""SQLAlchemy-backed RunEventStore implementation.

Persists events to the ``run_events`` table. Trace content is truncated
at ``max_trace_content`` bytes to avoid bloating the database.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.jobs.model import JobAttemptRow, JobRow
from deerflow.persistence.models.run_event import RunEventRow, ThreadEventSequenceRow
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.runtime.events.models import (
    STREAM_TERMINAL_ERROR_CODES,
    StoredStreamFrame,
    StreamClosed,
    StreamCursorOutOfRange,
    StreamFrame,
    StreamLeaseProof,
    StreamScopeNotFound,
    StreamTerminalCandidate,
    StreamWriteAuthorityRequired,
    StreamWriteAuthorizationRevoked,
    StreamWriteCancelled,
    StreamWriteLeaseLost,
    canonical_stream_terminal_status,
    stream_terminal_status_for_run_settlement,
)
from deerflow.runtime.events.store.base import RunEventStore
from deerflow.runtime.private_scope import PrivateResourceScope
from deerflow.runtime.runs.schemas import RunStatus
from deerflow.runtime.user_context import AUTO, _AutoSentinel
from deerflow.utils.time import coerce_iso

logger = logging.getLogger(__name__)
_EXECUTABLE_ROLES = frozenset({"admin", "editor", "runner", "channel_guest"})
_STREAM_EVENT_NAME_METADATA_KEY = "stream_event_name"
_STREAM_TERMINAL_AUTHORITY_METADATA_KEY = "stream_terminal_authority"
_STREAM_TERMINAL_CANDIDATE_EVENT_TYPE = "run.terminal_candidate"
_INTERNAL_EVENT_CATEGORY = "internal"
_StreamCancellationAuthority = Literal["none", "ordinary", "authorization_revoked"]
_SETTLED_STREAM_TERMINAL_AUTHORITY_ISSUER = object()

RUN_EVENTS_NOTIFY_CHANNEL = "run_events"
"""LISTEN/NOTIFY channel that wakes durable SSE consumers after a stream commit.

NOTIFY is only an alarm clock: the payload is the ``run_id`` and delivery rides
the writing transaction's commit. Readers never trust it for data — a lost
notification merely degrades a consumer to its poll-timeout fallback.
"""


@dataclass(frozen=True, slots=True, init=False)
class _SettledStreamTerminalAuthority:
    """Transaction-local proof of one exact revoked Run settlement."""

    _session: AsyncSession = field(repr=False, compare=False)
    _transaction: object = field(repr=False, compare=False)
    project_id: uuid.UUID
    owner_user_id: str
    membership_version: int
    thread_id: str
    run_id: str
    job_id: uuid.UUID
    attempt_id: uuid.UUID
    attempt_number: int
    origin_trace_id: str
    job_type: str
    automation_occurrence_id: str | None
    predecessor_dead_job_id: uuid.UUID | None
    _lease_token_hash: str = field(repr=False)

    def __init__(
        self,
        *,
        issuer: object,
        session: AsyncSession,
        project_id: uuid.UUID,
        owner_user_id: str,
        membership_version: int,
        thread_id: str,
        run_id: str,
        job_id: uuid.UUID,
        attempt_id: uuid.UUID,
        attempt_number: int,
        origin_trace_id: str,
        job_type: str,
        automation_occurrence_id: str | None,
        predecessor_dead_job_id: uuid.UUID | None,
        lease_token_hash: str,
    ) -> None:
        if issuer is not _SETTLED_STREAM_TERMINAL_AUTHORITY_ISSUER:
            raise TypeError("settled stream terminal authority is private")
        transaction = session.sync_session.get_transaction()
        if transaction is None or not transaction.is_active:
            raise RuntimeError(
                "settled stream terminal authority requires an active transaction",
            )
        for name, value in (
            ("_session", session),
            ("_transaction", transaction),
            ("project_id", project_id),
            ("owner_user_id", owner_user_id),
            ("membership_version", membership_version),
            ("thread_id", thread_id),
            ("run_id", run_id),
            ("job_id", job_id),
            ("attempt_id", attempt_id),
            ("attempt_number", attempt_number),
            ("origin_trace_id", origin_trace_id),
            ("job_type", job_type),
            ("automation_occurrence_id", automation_occurrence_id),
            ("predecessor_dead_job_id", predecessor_dead_job_id),
            ("_lease_token_hash", lease_token_hash),
        ):
            object.__setattr__(self, name, value)


def _issue_settled_stream_terminal_authority(
    session: AsyncSession,
    *,
    scope: PrivateResourceScope,
    run: RunRow,
    job: JobRow,
    attempt: JobAttemptRow,
    lease_token: str,
) -> _SettledStreamTerminalAuthority:
    """Issue only from the exact locked authorization-revoked settlement."""

    if type(scope) is not PrivateResourceScope or type(run) is not RunRow or type(job) is not JobRow or type(attempt) is not JobAttemptRow or not isinstance(lease_token, str) or not lease_token:
        raise TypeError("locked stream terminal settlement is invalid")
    try:
        project_id = uuid.UUID(scope.project_id)
        owner_user_id = str(uuid.UUID(scope.owner_user_id))
    except (TypeError, ValueError):
        raise ValueError("locked stream terminal scope is invalid") from None
    lease_token_hash = hashlib.sha256(lease_token.encode("utf-8")).hexdigest()
    valid_job_lineage = (job.job_type == "private_run" and job.automation_occurrence_id is None) or (job.job_type == "automation_run" and job.automation_occurrence_id is not None)
    if (
        (run.status, job.status, attempt.outcome) != ("interrupted", "cancelled", "cancelled")
        or run.authorization_cancel_requested_at is None
        or not run.authorization_cancel_reason
        or run.execution_lease_token_hash is not None
        or run.execution_lease_expires_at is not None
        or run.execution_heartbeat_at is not None
        or job.lease_owner_id is not None
        or job.lease_token_hash is not None
        or job.lease_expires_at is not None
        or job.heartbeat_at is not None
        or job.completed_at is None
        or attempt.finished_at is None
        or run.project_id != project_id
        or run.owner_user_id != owner_user_id
        or job.project_id != project_id
        or job.owner_user_id != owner_user_id
        or run.job_id != job.id
        or job.run_id != run.run_id
        or attempt.job_id != job.id
        or attempt.attempt_number != job.attempt_count
        or attempt.lease_token_hash != lease_token_hash
        or job.origin_trace_id != run.origin_trace_id
        or not isinstance(run.origin_trace_id, str)
        or not run.origin_trace_id
        or not valid_job_lineage
    ):
        raise RuntimeError(
            "settled stream terminal authority requires exact locked lineage",
        )
    return _SettledStreamTerminalAuthority(
        issuer=_SETTLED_STREAM_TERMINAL_AUTHORITY_ISSUER,
        session=session,
        project_id=project_id,
        owner_user_id=owner_user_id,
        membership_version=scope.membership_version,
        thread_id=run.thread_id,
        run_id=run.run_id,
        job_id=job.id,
        attempt_id=attempt.id,
        attempt_number=attempt.attempt_number,
        origin_trace_id=run.origin_trace_id,
        job_type=job.job_type,
        automation_occurrence_id=job.automation_occurrence_id,
        predecessor_dead_job_id=job.predecessor_dead_job_id,
        lease_token_hash=lease_token_hash,
    )


class DbRunEventStore(RunEventStore):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        max_trace_content: int = 10240,
        run_event_notify_enabled: bool = True,
    ):
        self._sf = session_factory
        self._max_trace_content = max_trace_content
        self._run_event_notify_enabled = run_event_notify_enabled

    @staticmethod
    def _coordinates(scope: PrivateResourceScope) -> tuple[uuid.UUID, str]:
        if type(scope) is not PrivateResourceScope:
            raise ValueError("private event scope is required")
        try:
            return uuid.UUID(scope.project_id), str(uuid.UUID(scope.owner_user_id))
        except (TypeError, ValueError):
            raise ValueError("private event scope is invalid") from None

    @classmethod
    def _scope_predicates(cls, scope: PrivateResourceScope):
        project_id, owner_user_id = cls._coordinates(scope)
        return (
            RunEventRow.project_id == project_id,
            RunEventRow.owner_user_id == owner_user_id,
        )

    @classmethod
    async def _lock_event_sequence(
        cls,
        session: AsyncSession,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
    ) -> ThreadEventSequenceRow:
        """Lock a deletion-stable Thread high-watermark before event writes."""

        project_id, owner_user_id = cls._coordinates(scope)
        bind = session.get_bind()
        if bind is not None and bind.dialect.name == "postgresql":
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(CAST(:thread_id AS text))::bigint)"),
                {"thread_id": thread_id},
            )
        sequence = (
            await session.execute(
                select(ThreadEventSequenceRow)
                .where(
                    ThreadEventSequenceRow.project_id == project_id,
                    ThreadEventSequenceRow.owner_user_id == owner_user_id,
                    ThreadEventSequenceRow.thread_id == thread_id,
                )
                .with_for_update(of=ThreadEventSequenceRow)
            )
        ).scalar_one_or_none()
        if sequence is None:
            thread_exists = await session.scalar(
                select(ThreadMetaRow.thread_id).where(
                    ThreadMetaRow.project_id == project_id,
                    ThreadMetaRow.owner_user_id == owner_user_id,
                    ThreadMetaRow.thread_id == thread_id,
                )
            )
            if thread_exists is None:
                raise StreamScopeNotFound("scoped stream thread was not found")
            current = await session.scalar(
                select(func.max(RunEventRow.seq)).where(
                    RunEventRow.project_id == project_id,
                    RunEventRow.owner_user_id == owner_user_id,
                    RunEventRow.thread_id == thread_id,
                )
            )
            sequence = ThreadEventSequenceRow(
                project_id=project_id,
                owner_user_id=owner_user_id,
                thread_id=thread_id,
                high_watermark=int(current or 0),
            )
            session.add(sequence)
            await session.flush()
        return sequence

    @classmethod
    async def _event_high_watermark(
        cls,
        session: AsyncSession,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
    ) -> int:
        project_id, owner_user_id = cls._coordinates(scope)
        value = await session.scalar(
            select(ThreadEventSequenceRow.high_watermark).where(
                ThreadEventSequenceRow.project_id == project_id,
                ThreadEventSequenceRow.owner_user_id == owner_user_id,
                ThreadEventSequenceRow.thread_id == thread_id,
            )
        )
        if value is not None:
            return int(value)
        legacy_max = await session.scalar(
            select(func.max(RunEventRow.seq)).where(
                RunEventRow.project_id == project_id,
                RunEventRow.owner_user_id == owner_user_id,
                RunEventRow.thread_id == thread_id,
            )
        )
        return int(legacy_max or 0)

    @staticmethod
    def _advance_event_sequence(
        sequence: ThreadEventSequenceRow,
        *,
        count: int = 1,
    ) -> int:
        if type(count) is not int or count < 1:
            raise ValueError("event sequence reservation must be positive")
        first = sequence.high_watermark + 1
        sequence.high_watermark += count
        return first

    @staticmethod
    def _reject_reserved_stream_write(*, event_type: str, category: str) -> None:
        if category == "stream" or event_type == "stream.end":
            raise ValueError(
                "durable stream events are reserved for append_stream_frame",
            )

    @staticmethod
    async def _lock_stream_governance(
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        membership_version: int,
    ) -> None:
        """Lock and revalidate current private stream mutation authority."""

        project = (await session.execute(select(ProjectRow).where(ProjectRow.id == project_id).with_for_update(read=True, of=ProjectRow))).scalar_one_or_none()
        membership = (
            await session.execute(
                select(ProjectMembershipRow)
                .where(
                    ProjectMembershipRow.project_id == project_id,
                    ProjectMembershipRow.user_id == owner_user_id,
                )
                .with_for_update(read=True, of=ProjectMembershipRow)
            )
        ).scalar_one_or_none()
        if project is None or project.status != "active" or project.is_suspended or membership is None or membership.status != "active" or membership.role not in _EXECUTABLE_ROLES or membership.version != membership_version:
            raise StreamWriteAuthorizationRevoked(
                "stream execution governance is no longer active",
            )

    @staticmethod
    async def _authorize_stream_lease(
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        membership_version: int,
        thread_id: str,
        run_id: str,
        lease: StreamLeaseProof,
    ) -> _StreamCancellationAuthority:
        if type(lease) is not StreamLeaseProof:
            raise StreamWriteLeaseLost(
                "stream lease capability is invalid",
            )
        await DbRunEventStore._lock_stream_governance(
            session,
            project_id=project_id,
            owner_user_id=owner_user_id,
            membership_version=membership_version,
        )

        job = (
            await session.execute(
                select(JobRow)
                .where(
                    JobRow.id == lease.job_id,
                    JobRow.job_type.in_(("private_run", "automation_run")),
                    JobRow.project_id == project_id,
                    JobRow.owner_user_id == owner_user_id,
                    JobRow.run_id == run_id,
                )
                .with_for_update(of=JobRow)
            )
        ).scalar_one_or_none()
        run = (
            await session.execute(
                select(RunRow)
                .where(
                    RunRow.project_id == project_id,
                    RunRow.owner_user_id == owner_user_id,
                    RunRow.thread_id == thread_id,
                    RunRow.run_id == run_id,
                    RunRow.job_id == lease.job_id,
                )
                .with_for_update(of=RunRow)
            )
        ).scalar_one_or_none()
        checked_at = datetime.now(UTC)
        token_hash = hashlib.sha256(
            lease.lease_token.encode("utf-8"),
        ).hexdigest()
        if (
            job is None
            or run is None
            or job.status != "running"
            or job.lease_token_hash != token_hash
            or job.lease_expires_at is None
            or job.lease_expires_at <= checked_at
            or run.status != "running"
            or run.execution_lease_token_hash != token_hash
            or run.execution_lease_expires_at is None
            or run.execution_lease_expires_at <= checked_at
        ):
            raise StreamWriteLeaseLost(
                "stream execution lease is no longer active",
            )
        if run.authorization_cancel_requested_at is not None:
            return "authorization_revoked"
        if job.cancel_requested_at is not None or run.cancel_requested_at is not None:
            return "ordinary"
        return "none"

    @classmethod
    async def _require_authorized_event_parent(
        cls,
        session: AsyncSession,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        run_id: str,
        lease: StreamLeaseProof | None,
    ) -> tuple[uuid.UUID, str]:
        """Resolve the parent and, when supplied, atomically enforce Job authority."""

        project_id, owner_user_id = cls._coordinates(scope)
        if lease is None:
            await cls._lock_stream_governance(
                session,
                project_id=project_id,
                owner_user_id=owner_user_id,
                membership_version=scope.membership_version,
            )
            parent = (
                await session.execute(
                    select(RunRow)
                    .where(
                        RunRow.project_id == project_id,
                        RunRow.owner_user_id == owner_user_id,
                        RunRow.thread_id == thread_id,
                        RunRow.run_id == run_id,
                    )
                    .with_for_update(of=RunRow)
                )
            ).scalar_one_or_none()
            if parent is None:
                raise ValueError("scoped parent run not found")
            if parent.job_id is not None:
                raise StreamWriteAuthorityRequired(
                    "job-owned event write requires live execution authority",
                )
            return parent.project_id, parent.owner_user_id
        cancellation_authority = await cls._authorize_stream_lease(
            session,
            project_id=project_id,
            owner_user_id=owner_user_id,
            membership_version=scope.membership_version,
            thread_id=thread_id,
            run_id=run_id,
            lease=lease,
        )
        if cancellation_authority == "authorization_revoked":
            raise StreamWriteAuthorizationRevoked(
                "authorization-revoked execution cannot append an internal event",
            )
        if cancellation_authority == "ordinary":
            raise StreamWriteCancelled(
                "cancelled execution cannot append an internal event",
            )
        return project_id, owner_user_id

    @staticmethod
    def _row_to_dict(row: RunEventRow) -> dict:
        d = row.to_dict()
        d["metadata"] = d.pop("event_metadata", {})
        val = d.get("created_at")
        if isinstance(val, datetime):
            # ``coerce_iso`` normalizes legacy naive datetimes as UTC.
            d["created_at"] = coerce_iso(val)
        d.pop("id", None)
        if isinstance(d.get("project_id"), uuid.UUID):
            d["project_id"] = str(d["project_id"])
        # Restore structured content that was JSON-serialized on write.
        raw = d.get("content", "")
        metadata = d.get("metadata", {})
        if isinstance(raw, str) and (metadata.get("content_is_json") or metadata.get("content_is_dict")):
            try:
                d["content"] = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                # Content looked like JSON but failed to parse;
                # keep the raw string as-is.
                logger.debug("Failed to deserialize content as JSON for event seq=%s", d.get("seq"))
        return d

    def _truncate_trace(self, category: str, content: Any, metadata: dict | None) -> tuple[Any, dict]:
        if category == "trace":
            text = content if isinstance(content, str) else json.dumps(content, default=str, ensure_ascii=False)
            encoded = text.encode("utf-8")
            if len(encoded) > self._max_trace_content:
                # Truncate by bytes, then decode back (may cut a multi-byte char, so use errors="ignore")
                content = encoded[: self._max_trace_content].decode("utf-8", errors="ignore")
                metadata = {**(metadata or {}), "content_truncated": True, "original_byte_length": len(encoded)}
        return content, metadata or {}

    @staticmethod
    def _content_to_db(content: Any, metadata: dict | None) -> tuple[str, dict]:
        metadata = metadata or {}
        if isinstance(content, str):
            return content, metadata

        db_content = json.dumps(content, default=str, ensure_ascii=False)
        metadata = {**metadata, "content_is_json": True}
        if isinstance(content, dict):
            metadata["content_is_dict"] = True
        return db_content, metadata

    @classmethod
    def _stream_row(
        cls,
        row: RunEventRow,
        *,
        created: bool,
    ) -> StoredStreamFrame:
        record = cls._row_to_dict(row)
        terminal = record["event_type"] == "stream.end"
        event = "end" if terminal else record["event_type"]
        metadata = record.get("metadata")
        namespaced_event = metadata.get(_STREAM_EVENT_NAME_METADATA_KEY) if isinstance(metadata, dict) else None
        if not terminal and isinstance(namespaced_event, str) and namespaced_event.partition("|")[0] == record["event_type"]:
            try:
                StreamFrame(event=namespaced_event, data=record["content"])
            except (TypeError, ValueError):
                pass
            else:
                event = namespaced_event
        return StoredStreamFrame(
            id=str(record["seq"]),
            thread_id=record["thread_id"],
            run_id=record["run_id"],
            event=event,
            data=record["content"],
            terminal=terminal,
            created=created,
            terminal_authority=(metadata.get(_STREAM_TERMINAL_AUTHORITY_METADATA_KEY, "ordinary") if terminal and isinstance(metadata, dict) else "ordinary"),
        )

    async def _notify_stream_append(self, session: AsyncSession, run_id: str) -> None:
        """Queue a consumer wakeup that is delivered with the caller's commit."""
        if not self._run_event_notify_enabled:
            return
        bind = session.get_bind()
        if bind is None or bind.dialect.name != "postgresql":
            return
        await session.execute(
            text("SELECT pg_notify(:channel, :payload)"),
            {"channel": RUN_EVENTS_NOTIFY_CHANNEL, "payload": run_id},
        )

    @staticmethod
    def _stream_event_storage(frame: StreamFrame) -> tuple[str, dict[str, object]]:
        """Return a bounded database event type plus lossless protocol metadata."""
        if frame.terminal:
            return "stream.end", {
                "stream_terminal": True,
                _STREAM_TERMINAL_AUTHORITY_METADATA_KEY: frame.terminal_authority,
            }
        event_type, separator, _namespace = frame.event.partition("|")
        metadata: dict[str, object] = {"stream_terminal": False}
        if separator:
            metadata[_STREAM_EVENT_NAME_METADATA_KEY] = frame.event
        return event_type, metadata

    async def append_stream_frame(
        self,
        session: AsyncSession,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        run_id: str,
        frame: StreamFrame,
        lease: StreamLeaseProof | None = None,
    ) -> StoredStreamFrame:
        """Append under the caller's transaction and thread advisory lock."""

        if type(frame) is not StreamFrame:
            raise TypeError("StreamFrame is required")
        project_id, owner_user_id = self._coordinates(scope)
        if lease is not None:
            cancellation_authority = await self._authorize_stream_lease(
                session,
                project_id=project_id,
                owner_user_id=owner_user_id,
                membership_version=scope.membership_version,
                thread_id=thread_id,
                run_id=run_id,
                lease=lease,
            )
        else:
            try:
                await self._require_authorized_event_parent(
                    session,
                    scope=scope,
                    thread_id=thread_id,
                    run_id=run_id,
                    lease=None,
                )
            except ValueError:
                raise StreamScopeNotFound(
                    "scoped stream Run was not found",
                ) from None
            cancellation_authority = "none"

        # All governance and execution rows are locked before the thread
        # advisory/sequence lock.  Keep the terminal lookup after the sequence
        # lock so a concurrent terminal publisher cannot pass this recheck.
        sequence = await self._lock_event_sequence(
            session,
            scope=scope,
            thread_id=thread_id,
        )
        terminal = (
            await session.execute(
                select(RunEventRow).where(
                    RunEventRow.project_id == project_id,
                    RunEventRow.owner_user_id == owner_user_id,
                    RunEventRow.thread_id == thread_id,
                    RunEventRow.run_id == run_id,
                    RunEventRow.category == "stream",
                    RunEventRow.event_type == "stream.end",
                )
            )
        ).scalar_one_or_none()
        if terminal is not None:
            if frame.terminal:
                return self._stream_row(terminal, created=False)
            raise StreamClosed("run stream is already terminal")

        if cancellation_authority == "authorization_revoked":
            if not frame.terminal:
                raise StreamWriteAuthorizationRevoked(
                    "authorization-revoked execution cannot append a data frame",
                )
            frame = StreamFrame.end(status="interrupted")
        elif cancellation_authority == "ordinary":
            if not frame.terminal:
                raise StreamWriteCancelled(
                    "cancelled execution cannot append a data frame",
                )
            if frame.terminal_authority != "durable_response":
                frame = StreamFrame.end(status="interrupted")

        event_type, stream_metadata = self._stream_event_storage(frame)
        db_content, metadata = self._content_to_db(frame.data, stream_metadata)
        seq = self._advance_event_sequence(sequence)
        row = RunEventRow(
            thread_id=thread_id,
            run_id=run_id,
            project_id=project_id,
            owner_user_id=owner_user_id,
            event_type=event_type,
            category="stream",
            content=db_content,
            event_metadata=metadata,
            seq=seq,
            created_at=datetime.now(UTC),
        )
        session.add(row)
        await session.flush()
        await self._notify_stream_append(session, run_id)
        return self._stream_row(row, created=True)

    @staticmethod
    def _terminal_candidate_from_row(
        row: RunEventRow,
    ) -> StreamTerminalCandidate:
        record = DbRunEventStore._row_to_dict(row)
        try:
            return StreamTerminalCandidate.from_payload(record["content"])
        except (TypeError, ValueError):
            raise StreamWriteAuthorityRequired(
                "stored stream terminal candidate is invalid",
            ) from None

    async def get_stream_terminal_candidate(
        self,
        session: AsyncSession,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        run_id: str,
    ) -> StreamTerminalCandidate | None:
        """Read one internal candidate without exposing it to replay APIs."""

        project_id, owner_user_id = self._coordinates(scope)
        rows = (
            (
                await session.execute(
                    select(RunEventRow)
                    .where(
                        RunEventRow.project_id == project_id,
                        RunEventRow.owner_user_id == owner_user_id,
                        RunEventRow.thread_id == thread_id,
                        RunEventRow.run_id == run_id,
                        RunEventRow.category == _INTERNAL_EVENT_CATEGORY,
                        RunEventRow.event_type == _STREAM_TERMINAL_CANDIDATE_EVENT_TYPE,
                    )
                    .order_by(RunEventRow.seq.asc())
                    .limit(2)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise StreamWriteAuthorityRequired(
                "multiple stream terminal candidates exist",
            )
        return self._terminal_candidate_from_row(rows[0])

    async def append_stream_terminal_candidate(
        self,
        session: AsyncSession,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        run_id: str,
        candidate: StreamTerminalCandidate,
        lease: StreamLeaseProof,
    ) -> StreamTerminalCandidate:
        """Persist one lease-bound internal candidate, idempotently."""

        if type(candidate) is not StreamTerminalCandidate:
            raise TypeError("StreamTerminalCandidate is required")
        project_id, owner_user_id = self._coordinates(scope)
        # Candidate data is internal settlement evidence, not a public stream
        # mutation. Ordinary Stop and authorization revocation are therefore
        # observed by settlement rather than rewriting or suppressing it here.
        await self._authorize_stream_lease(
            session,
            project_id=project_id,
            owner_user_id=owner_user_id,
            membership_version=scope.membership_version,
            thread_id=thread_id,
            run_id=run_id,
            lease=lease,
        )
        sequence = await self._lock_event_sequence(
            session,
            scope=scope,
            thread_id=thread_id,
        )
        terminal_exists = await session.scalar(
            select(RunEventRow.id).where(
                RunEventRow.project_id == project_id,
                RunEventRow.owner_user_id == owner_user_id,
                RunEventRow.thread_id == thread_id,
                RunEventRow.run_id == run_id,
                RunEventRow.category == "stream",
                RunEventRow.event_type == "stream.end",
            )
        )
        if terminal_exists is not None:
            raise StreamClosed("run stream is already terminal")

        existing = await self.get_stream_terminal_candidate(
            session,
            scope=scope,
            thread_id=thread_id,
            run_id=run_id,
        )
        if existing is not None:
            if existing != candidate:
                raise StreamWriteAuthorityRequired(
                    "stream terminal candidate conflicts with existing proof",
                )
            return existing

        db_content, metadata = self._content_to_db(
            candidate.to_payload(),
            {"internal_event": True},
        )
        row = RunEventRow(
            thread_id=thread_id,
            run_id=run_id,
            project_id=project_id,
            owner_user_id=owner_user_id,
            event_type=_STREAM_TERMINAL_CANDIDATE_EVENT_TYPE,
            category=_INTERNAL_EVENT_CATEGORY,
            content=db_content,
            event_metadata=metadata,
            seq=self._advance_event_sequence(sequence),
            created_at=datetime.now(UTC),
        )
        session.add(row)
        await session.flush()
        return candidate

    @staticmethod
    def _root_values_frame_condition():
        """Match root ``values`` rows; namespaced subgraph frames keep the full
        event name in metadata and are never treated as the root state."""
        return (RunEventRow.event_type == "values") & (RunEventRow.event_metadata[_STREAM_EVENT_NAME_METADATA_KEY].as_string().is_(None))

    async def list_stream_frames(
        self,
        session: AsyncSession,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        cursor: int,
        limit: int,
        run_id: str | None = None,
        full_state_horizon: int | None = None,
    ) -> tuple[StoredStreamFrame, ...]:
        if type(cursor) is not int or cursor < 0:
            raise ValueError("stream cursor must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("stream limit must be between 1 and 1000")
        if full_state_horizon is not None and (type(full_state_horizon) is not int or full_state_horizon < 0):
            raise ValueError("stream full-state horizon must be a non-negative integer")
        project_id, owner_user_id = self._coordinates(scope)
        thread_exists = await session.scalar(
            select(ThreadMetaRow.thread_id).where(
                ThreadMetaRow.project_id == project_id,
                ThreadMetaRow.owner_user_id == owner_user_id,
                ThreadMetaRow.thread_id == thread_id,
            )
        )
        if thread_exists is None:
            raise StreamScopeNotFound("scoped stream thread was not found")

        high_watermark = await self._event_high_watermark(
            session,
            scope=scope,
            thread_id=thread_id,
        )
        if cursor > high_watermark:
            raise StreamCursorOutOfRange("stream cursor is ahead of the event log")
        statement = select(RunEventRow).where(
            RunEventRow.project_id == project_id,
            RunEventRow.owner_user_id == owner_user_id,
            RunEventRow.thread_id == thread_id,
            RunEventRow.category == "stream",
            RunEventRow.seq > cursor,
        )
        if run_id is not None:
            statement = statement.where(RunEventRow.run_id == run_id)
        if full_state_horizon is not None:
            # A root ``values`` frame is the complete Run state, so the newest
            # one below the horizon supersedes every earlier root ``values``
            # frame. Dropping the superseded rows keeps ids monotonic with
            # gaps, which durable cursor consumers already accept.
            statement = statement.where(
                or_(
                    RunEventRow.seq >= full_state_horizon,
                    ~self._root_values_frame_condition(),
                )
            )
        rows = (await session.execute(statement.order_by(RunEventRow.seq.asc()).limit(limit))).scalars()
        return tuple(self._stream_row(row, created=False) for row in rows)

    async def latest_full_state_stream_seq(
        self,
        session: AsyncSession,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        run_id: str,
    ) -> int:
        """Return the newest root ``values`` sequence for one exact Run.

        Used as the replay compaction horizon: zero means the Run has not
        published a root complete-state frame yet and nothing may be dropped.
        """
        project_id, owner_user_id = self._coordinates(scope)
        value = await session.scalar(
            select(func.max(RunEventRow.seq)).where(
                RunEventRow.project_id == project_id,
                RunEventRow.owner_user_id == owner_user_id,
                RunEventRow.thread_id == thread_id,
                RunEventRow.run_id == run_id,
                RunEventRow.category == "stream",
                self._root_values_frame_condition(),
            )
        )
        return int(value or 0)

    async def get_stream_terminal(
        self,
        session: AsyncSession,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        run_id: str,
    ) -> StoredStreamFrame | None:
        project_id, owner_user_id = self._coordinates(scope)
        row = (
            await session.execute(
                select(RunEventRow).where(
                    RunEventRow.project_id == project_id,
                    RunEventRow.owner_user_id == owner_user_id,
                    RunEventRow.thread_id == thread_id,
                    RunEventRow.run_id == run_id,
                    RunEventRow.category == "stream",
                    RunEventRow.event_type == "stream.end",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return self._stream_row(row, created=False)

    @staticmethod
    def _settled_terminal_matches(
        terminal: RunEventRow,
        *,
        expected_content: str,
        expected_metadata: dict,
    ) -> bool:
        """Compare a retained terminal row under status-spelling equivalence.

        Stored rows are immutable, and rows written before the canonical
        spelling cutover carry ``{"status": "success"}`` where the settled
        repair expects ``{"status": "completed"}``. Both describe the same
        business outcome, so idempotent settlement accepts them; every other
        divergence (status class or error code) still fails closed.
        """

        stored_metadata = dict(terminal.event_metadata or {})
        stored_metadata.setdefault(_STREAM_TERMINAL_AUTHORITY_METADATA_KEY, "ordinary")
        normalized_expected_metadata = dict(expected_metadata)
        normalized_expected_metadata.setdefault(
            _STREAM_TERMINAL_AUTHORITY_METADATA_KEY,
            "ordinary",
        )
        if terminal.content == expected_content and stored_metadata == normalized_expected_metadata:
            return True
        if stored_metadata != normalized_expected_metadata:
            return False
        try:
            stored = json.loads(terminal.content)
            expected = json.loads(expected_content)
        except (TypeError, ValueError):
            return False
        if not isinstance(stored, dict) or not isinstance(expected, dict):
            return False
        stored_status = stored.get("status")
        expected_status = expected.get("status")
        if not isinstance(stored_status, str) or not isinstance(expected_status, str):
            return False
        return {
            **stored,
            "status": canonical_stream_terminal_status(stored_status),
        } == {
            **expected,
            "status": canonical_stream_terminal_status(expected_status),
        }

    @classmethod
    async def _require_settled_stream_terminal_authority(
        cls,
        session: AsyncSession,
        *,
        authority: _SettledStreamTerminalAuthority,
        scope: PrivateResourceScope,
        thread_id: str,
        run_id: str,
        job: JobRow | None,
        run: RunRow | None,
    ) -> None:
        """Revalidate an exact transaction-local revoked-settlement proof."""

        project_id, owner_user_id = cls._coordinates(scope)
        transaction = session.sync_session.get_transaction()
        if (
            type(authority) is not _SettledStreamTerminalAuthority
            or authority._session is not session
            or transaction is None
            or transaction is not authority._transaction
            or not transaction.is_active
            or authority.project_id != project_id
            or authority.owner_user_id != owner_user_id
            or authority.membership_version != scope.membership_version
            or authority.thread_id != thread_id
            or authority.run_id != run_id
            or job is None
            or run is None
            or authority.job_id != job.id
            or run.job_id != job.id
            or job.project_id != project_id
            or job.owner_user_id != owner_user_id
            or job.run_id != run_id
            or run.project_id != project_id
            or run.owner_user_id != owner_user_id
            or run.thread_id != thread_id
            or run.run_id != run_id
            or (run.status, job.status) != ("interrupted", "cancelled")
            or run.authorization_cancel_requested_at is None
            or not run.authorization_cancel_reason
            or run.execution_lease_token_hash is not None
            or run.execution_lease_expires_at is not None
            or run.execution_heartbeat_at is not None
            or job.lease_owner_id is not None
            or job.lease_token_hash is not None
            or job.lease_expires_at is not None
            or job.heartbeat_at is not None
            or job.completed_at is None
            or authority.origin_trace_id != job.origin_trace_id
            or authority.origin_trace_id != run.origin_trace_id
            or authority.job_type != job.job_type
            or authority.automation_occurrence_id != job.automation_occurrence_id
            or authority.predecessor_dead_job_id != job.predecessor_dead_job_id
            or authority.attempt_number != job.attempt_count
        ):
            raise StreamWriteAuthorityRequired(
                "stream terminal settlement authority is invalid",
            )
        attempt = (
            await session.execute(
                select(JobAttemptRow)
                .where(
                    JobAttemptRow.id == authority.attempt_id,
                    JobAttemptRow.job_id == authority.job_id,
                    JobAttemptRow.attempt_number == authority.attempt_number,
                    JobAttemptRow.lease_token_hash == authority._lease_token_hash,
                    JobAttemptRow.outcome == "cancelled",
                    JobAttemptRow.finished_at.is_not(None),
                )
                .with_for_update(of=JobAttemptRow)
            )
        ).scalar_one_or_none()
        if attempt is None:
            raise StreamWriteAuthorityRequired(
                "stream terminal settlement lease lineage is invalid",
            )

    async def ensure_settled_stream_terminal(
        self,
        session: AsyncSession,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        run_id: str,
        status: str,
        error_code: str | None = None,
        settlement_authority: _SettledStreamTerminalAuthority | None = None,
    ) -> StoredStreamFrame:
        """Persist the missing terminal fact for an already-settled Run.

        This path deliberately cannot close an executing job. It is only the
        durable repair used by reconnect readers after the authoritative Run
        and, when present, Job rows already reached a consistent terminal pair.
        """

        frame = StreamFrame.end(status=status, error_code=error_code)
        project_id, owner_user_id = self._coordinates(scope)
        if settlement_authority is None:
            await self._lock_stream_governance(
                session,
                project_id=project_id,
                owner_user_id=owner_user_id,
                membership_version=scope.membership_version,
            )
        elif type(settlement_authority) is not _SettledStreamTerminalAuthority:
            raise StreamWriteAuthorityRequired(
                "stream terminal settlement authority is invalid",
            )
        projected_job_id = await session.scalar(
            select(RunRow.job_id).where(
                RunRow.project_id == project_id,
                RunRow.owner_user_id == owner_user_id,
                RunRow.thread_id == thread_id,
                RunRow.run_id == run_id,
            )
        )
        job = None
        if projected_job_id is not None:
            job = (
                await session.execute(
                    select(JobRow)
                    .where(
                        JobRow.id == projected_job_id,
                        JobRow.job_type.in_(("private_run", "automation_run")),
                        JobRow.project_id == project_id,
                        JobRow.owner_user_id == owner_user_id,
                        JobRow.run_id == run_id,
                    )
                    .with_for_update(of=JobRow)
                )
            ).scalar_one_or_none()
        run = (
            await session.execute(
                select(RunRow)
                .where(
                    RunRow.project_id == project_id,
                    RunRow.owner_user_id == owner_user_id,
                    RunRow.thread_id == thread_id,
                    RunRow.run_id == run_id,
                )
                .with_for_update(of=RunRow)
            )
        ).scalar_one_or_none()
        if run is None or run.job_id != projected_job_id:
            raise StreamScopeNotFound("scoped stream Run was not found")
        if projected_job_id is not None and job is None:
            raise StreamWriteAuthorityRequired(
                "stream terminal repair requires the exact settled Job",
            )
        if settlement_authority is not None:
            await self._require_settled_stream_terminal_authority(
                session,
                authority=settlement_authority,
                scope=scope,
                thread_id=thread_id,
                run_id=run_id,
                job=job,
                run=run,
            )

        try:
            expected_status = stream_terminal_status_for_run_settlement(
                RunStatus(run.status),
            )
        except ValueError:
            expected_status = None
        expected_data: dict[str, str] = {"status": expected_status or ""}
        if run.error in STREAM_TERMINAL_ERROR_CODES:
            expected_data["error_code"] = run.error
        if expected_status is None or frame.data != expected_data:
            raise StreamWriteAuthorityRequired(
                "stream terminal repair requires the exact settled Run state",
            )
        if job is not None:
            allowed_job_statuses = {
                "success": frozenset({"succeeded"}),
                "error": frozenset({"failed", "dead"}),
                "timeout": frozenset({"failed", "dead"}),
                "interrupted": frozenset({"cancelled"}),
            }[run.status]
            if job.status not in allowed_job_statuses:
                raise StreamWriteAuthorityRequired(
                    "stream terminal repair requires the exact settled Job state",
                )

        # Match every other event writer's global lock order.  The terminal
        # lookup must remain after the sequence lock to make repair idempotence
        # atomic with concurrent publishers.
        sequence = await self._lock_event_sequence(
            session,
            scope=scope,
            thread_id=thread_id,
        )
        db_content, metadata = self._content_to_db(
            frame.data,
            self._stream_event_storage(frame)[1],
        )
        terminal = (
            await session.execute(
                select(RunEventRow).where(
                    RunEventRow.project_id == project_id,
                    RunEventRow.owner_user_id == owner_user_id,
                    RunEventRow.thread_id == thread_id,
                    RunEventRow.run_id == run_id,
                    RunEventRow.category == "stream",
                    RunEventRow.event_type == "stream.end",
                )
            )
        ).scalar_one_or_none()
        if terminal is not None:
            if not self._settled_terminal_matches(
                terminal,
                expected_content=db_content,
                expected_metadata=metadata,
            ):
                raise StreamWriteAuthorityRequired(
                    "existing stream terminal disagrees with settled Run state",
                )
            return self._stream_row(terminal, created=False)

        row = RunEventRow(
            thread_id=thread_id,
            run_id=run_id,
            project_id=project_id,
            owner_user_id=owner_user_id,
            event_type="stream.end",
            category="stream",
            content=db_content,
            event_metadata=metadata,
            seq=self._advance_event_sequence(sequence),
            created_at=datetime.now(UTC),
        )
        session.add(row)
        await session.flush()
        await self._notify_stream_append(session, run_id)
        return self._stream_row(row, created=True)

    async def put(
        self,
        *,
        thread_id,
        run_id,
        event_type,
        category,
        content="",
        metadata=None,
        created_at=None,
        scope=None,
        lease: StreamLeaseProof | None = None,
    ):  # noqa: D401
        """Write a single event — low-frequency path only.

        This opens a dedicated transaction with a FOR UPDATE lock to
        assign a monotonic *seq*.  For high-throughput writes use
        :meth:`put_batch`, which acquires the lock once for the whole
        batch.  Currently the only caller is ``worker.run_agent`` for
        the initial ``human_message`` event (once per run).
        """
        self._reject_reserved_stream_write(
            event_type=event_type,
            category=category,
        )
        content, metadata = self._truncate_trace(category, content, metadata)
        db_content, metadata = self._content_to_db(content, metadata)
        if scope is None:
            raise ValueError("private event scope is required")
        async with self._sf() as session:
            async with session.begin():
                project_id, owner_user_id = await self._require_authorized_event_parent(
                    session,
                    scope=scope,
                    thread_id=thread_id,
                    run_id=run_id,
                    lease=lease,
                )
                sequence = await self._lock_event_sequence(
                    session,
                    scope=scope,
                    thread_id=thread_id,
                )
                seq = self._advance_event_sequence(sequence)
                row = RunEventRow(
                    thread_id=thread_id,
                    run_id=run_id,
                    project_id=project_id,
                    owner_user_id=owner_user_id,
                    event_type=event_type,
                    category=category,
                    content=db_content,
                    event_metadata=metadata,
                    seq=seq,
                    created_at=datetime.fromisoformat(created_at) if created_at else datetime.now(UTC),
                )
                session.add(row)
            return self._row_to_dict(row)

    async def put_batch(
        self,
        events,
        *,
        scope=None,
        lease: StreamLeaseProof | None = None,
    ):
        if not events:
            return []
        for event in events:
            self._reject_reserved_stream_write(
                event_type=event["event_type"],
                category=event.get("category", "trace"),
            )
        thread_ids = {e["thread_id"] for e in events}
        if len(thread_ids) > 1:
            raise ValueError(f"put_batch requires all events to belong to the same thread; got {thread_ids!r}")
        if scope is None:
            raise ValueError("private event scope is required")
        async with self._sf() as session:
            async with session.begin():
                # All events belong to the same thread (validated above).
                thread_id = events[0]["thread_id"]
                parent_by_run: dict[str, tuple[uuid.UUID, str]] = {}
                for run_id in sorted({e["run_id"] for e in events}):
                    parent_by_run[run_id] = await self._require_authorized_event_parent(
                        session,
                        scope=scope,
                        thread_id=thread_id,
                        run_id=run_id,
                        lease=lease,
                    )
                sequence = await self._lock_event_sequence(
                    session,
                    scope=scope,
                    thread_id=thread_id,
                )
                seq = (
                    self._advance_event_sequence(
                        sequence,
                        count=len(events),
                    )
                    - 1
                )
                rows = []
                for e in events:
                    seq += 1
                    content = e.get("content", "")
                    category = e.get("category", "trace")
                    metadata = e.get("metadata")
                    content, metadata = self._truncate_trace(category, content, metadata)
                    db_content, metadata = self._content_to_db(content, metadata)
                    project_id, owner_user_id = parent_by_run[e["run_id"]]
                    row = RunEventRow(
                        thread_id=e["thread_id"],
                        run_id=e["run_id"],
                        project_id=project_id,
                        owner_user_id=owner_user_id,
                        event_type=e["event_type"],
                        category=category,
                        content=db_content,
                        event_metadata=metadata,
                        seq=seq,
                        created_at=datetime.fromisoformat(e["created_at"]) if e.get("created_at") else datetime.now(UTC),
                    )
                    session.add(row)
                    rows.append(row)
            return [self._row_to_dict(r) for r in rows]

    async def list_messages(
        self,
        thread_id,
        *,
        limit=50,
        before_seq=None,
        after_seq=None,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ):
        if scope is None:
            return []
        stmt = select(RunEventRow).where(
            RunEventRow.thread_id == thread_id,
            RunEventRow.category == "message",
            *self._scope_predicates(scope),
        )
        if before_seq is not None:
            stmt = stmt.where(RunEventRow.seq < before_seq)
        if after_seq is not None:
            stmt = stmt.where(RunEventRow.seq > after_seq)

        if after_seq is not None:
            # Forward pagination: first `limit` records after cursor
            stmt = stmt.order_by(RunEventRow.seq.asc()).limit(limit)
            async with self._sf() as session:
                result = await session.execute(stmt)
                return [self._row_to_dict(r) for r in result.scalars()]
        else:
            # before_seq or default (latest): take last `limit` records, return ascending
            stmt = stmt.order_by(RunEventRow.seq.desc()).limit(limit)
            async with self._sf() as session:
                result = await session.execute(stmt)
                rows = list(result.scalars())
                return [self._row_to_dict(r) for r in reversed(rows)]

    async def list_events(
        self,
        thread_id,
        run_id,
        *,
        event_types=None,
        task_id=None,
        limit=500,
        after_seq=None,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ):
        if scope is None:
            return []
        stmt = select(RunEventRow).where(
            RunEventRow.thread_id == thread_id,
            RunEventRow.run_id == run_id,
            RunEventRow.category != _INTERNAL_EVENT_CATEGORY,
            *self._scope_predicates(scope),
        )
        if event_types:
            stmt = stmt.where(RunEventRow.event_type.in_(event_types))
        if task_id is not None:
            # Filter on metadata["task_id"] in SQL (before LIMIT) so cursor
            # pagination over a single subagent task stays correct (#3779). The
            # query is already scoped to (thread_id, run_id), so the JSON probe
            # only runs over this run's small candidate set; ``.as_string()``
            # renders to PostgreSQL JSON text extraction.
            stmt = stmt.where(RunEventRow.event_metadata["task_id"].as_string() == task_id)
        if after_seq is not None:
            stmt = stmt.where(RunEventRow.seq > after_seq)
        stmt = stmt.order_by(RunEventRow.seq.asc()).limit(limit)
        async with self._sf() as session:
            result = await session.execute(stmt)
            return [self._row_to_dict(r) for r in result.scalars()]

    async def list_messages_by_run(
        self,
        thread_id,
        run_id,
        *,
        limit=50,
        before_seq=None,
        after_seq=None,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ):
        if scope is None:
            return []
        stmt = select(RunEventRow).where(
            RunEventRow.thread_id == thread_id,
            RunEventRow.run_id == run_id,
            RunEventRow.category == "message",
            *self._scope_predicates(scope),
        )
        if before_seq is not None:
            stmt = stmt.where(RunEventRow.seq < before_seq)
        if after_seq is not None:
            stmt = stmt.where(RunEventRow.seq > after_seq)

        if after_seq is not None:
            stmt = stmt.order_by(RunEventRow.seq.asc()).limit(limit)
            async with self._sf() as session:
                result = await session.execute(stmt)
                return [self._row_to_dict(r) for r in result.scalars()]
        else:
            stmt = stmt.order_by(RunEventRow.seq.desc()).limit(limit)
            async with self._sf() as session:
                result = await session.execute(stmt)
                rows = list(result.scalars())
                return [self._row_to_dict(r) for r in reversed(rows)]

    async def count_messages(
        self,
        thread_id,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ):
        if scope is None:
            return 0
        stmt = (
            select(func.count())
            .select_from(RunEventRow)
            .where(
                RunEventRow.thread_id == thread_id,
                RunEventRow.category == "message",
                *self._scope_predicates(scope),
            )
        )
        async with self._sf() as session:
            return await session.scalar(stmt) or 0

    async def get_last_visible_ai_seq_by_run(
        self,
        thread_id,
        run_ids,
        *,
        scope: PrivateResourceScope | None = None,
    ):
        # The maximum AI sequence is not necessarily visible: a hidden lead
        # message or nested-caller tail may follow the final user-facing answer.
        # Reuse the bounded scoped reader until a grouped SQL query can express
        # the exact same caller/content visibility contract.
        return await super().get_last_visible_ai_seq_by_run(
            thread_id,
            run_ids,
            scope=scope,
        )

    async def delete_by_thread(
        self,
        thread_id,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ):
        if scope is None:
            return 0
        async with self._sf() as session:
            count_conditions = [RunEventRow.thread_id == thread_id, *self._scope_predicates(scope)]
            count_stmt = select(func.count()).select_from(RunEventRow).where(*count_conditions)
            count = await session.scalar(count_stmt) or 0
            if count > 0:
                await session.execute(delete(RunEventRow).where(*count_conditions))
                await session.commit()
            return count

    async def delete_by_run(
        self,
        thread_id,
        run_id,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ):
        if scope is None:
            return 0
        async with self._sf() as session:
            count_conditions = [
                RunEventRow.thread_id == thread_id,
                RunEventRow.run_id == run_id,
                *self._scope_predicates(scope),
            ]
            count_stmt = select(func.count()).select_from(RunEventRow).where(*count_conditions)
            count = await session.scalar(count_stmt) or 0
            if count > 0:
                await session.execute(delete(RunEventRow).where(*count_conditions))
                await session.commit()
            return count
