"""SQLAlchemy-backed RunEventStore implementation.

Persists events to the ``run_events`` table. Trace content is truncated
at ``max_trace_content`` bytes to avoid bloating the database.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.jobs.model import JobRow
from deerflow.persistence.models.run_event import RunEventRow
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.runtime.events.models import (
    StoredStreamFrame,
    StreamClosed,
    StreamCursorOutOfRange,
    StreamFrame,
    StreamLeaseProof,
    StreamScopeNotFound,
    StreamWriteAuthorityRequired,
    StreamWriteAuthorizationRevoked,
    StreamWriteCancelled,
    StreamWriteLeaseLost,
)
from deerflow.runtime.events.store.base import RunEventStore
from deerflow.runtime.private_scope import PrivateResourceScope
from deerflow.runtime.user_context import AUTO, _AutoSentinel
from deerflow.utils.time import coerce_iso

logger = logging.getLogger(__name__)
_EXECUTABLE_ROLES = frozenset({"admin", "editor", "runner"})


class DbRunEventStore(RunEventStore):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, max_trace_content: int = 10240):
        self._sf = session_factory
        self._max_trace_content = max_trace_content

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
    async def _require_parent_run(
        cls,
        session: AsyncSession,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        run_id: str,
    ) -> tuple[uuid.UUID, str]:
        project_id, owner_user_id = cls._coordinates(scope)
        parent = (
            await session.execute(
                select(RunRow.project_id, RunRow.owner_user_id).where(
                    RunRow.project_id == project_id,
                    RunRow.owner_user_id == owner_user_id,
                    RunRow.thread_id == thread_id,
                    RunRow.run_id == run_id,
                )
            )
        ).one_or_none()
        if parent is None:
            raise ValueError("scoped parent run not found")
        return parent.project_id, parent.owner_user_id

    @classmethod
    async def _require_stream_parent_run(
        cls,
        session: AsyncSession,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        run_id: str,
    ) -> tuple[uuid.UUID, str, uuid.UUID | None]:
        project_id, owner_user_id = cls._coordinates(scope)
        parent = (
            await session.execute(
                select(
                    RunRow.project_id,
                    RunRow.owner_user_id,
                    RunRow.job_id,
                ).where(
                    RunRow.project_id == project_id,
                    RunRow.owner_user_id == owner_user_id,
                    RunRow.thread_id == thread_id,
                    RunRow.run_id == run_id,
                )
            )
        ).one_or_none()
        if parent is None:
            raise ValueError("scoped parent run not found")
        return parent.project_id, parent.owner_user_id, parent.job_id

    @staticmethod
    def _reject_reserved_stream_write(*, event_type: str, category: str) -> None:
        if category == "stream" or event_type == "stream.end":
            raise ValueError(
                "durable stream events are reserved for append_stream_frame",
            )

    @staticmethod
    async def _authorize_stream_lease(
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        run_id: str,
        job_id: uuid.UUID,
        lease: StreamLeaseProof,
    ) -> bool:
        if type(lease) is not StreamLeaseProof or lease.job_id != job_id:
            raise StreamWriteLeaseLost(
                "stream lease capability does not match the Run job",
            )
        project = (await session.execute(select(ProjectRow).where(ProjectRow.id == project_id).with_for_update(of=ProjectRow))).scalar_one_or_none()
        membership = (
            await session.execute(
                select(ProjectMembershipRow)
                .where(
                    ProjectMembershipRow.project_id == project_id,
                    ProjectMembershipRow.user_id == owner_user_id,
                )
                .with_for_update(of=ProjectMembershipRow)
            )
        ).scalar_one_or_none()
        if project is None or project.status != "active" or project.is_suspended or membership is None or membership.status != "active" or membership.role not in _EXECUTABLE_ROLES:
            raise StreamWriteAuthorizationRevoked(
                "stream execution governance is no longer active",
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
        return any(
            value is not None
            for value in (
                job.cancel_requested_at,
                run.cancel_requested_at,
                run.authorization_cancel_requested_at,
            )
        )

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
        return StoredStreamFrame(
            id=str(record["seq"]),
            thread_id=record["thread_id"],
            run_id=record["run_id"],
            event="end" if terminal else record["event_type"],
            data=record["content"],
            terminal=terminal,
            created=created,
        )

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
        max_seq = await self._max_seq_for_thread(session, thread_id, scope)
        try:
            project_id, owner_user_id, job_id = await self._require_stream_parent_run(
                session,
                scope=scope,
                thread_id=thread_id,
                run_id=run_id,
            )
        except ValueError:
            raise StreamScopeNotFound("scoped stream Run was not found") from None
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

        if job_id is not None and lease is None:
            raise StreamWriteAuthorityRequired(
                "job-owned stream append requires live execution authority",
            )
        if job_id is None and lease is not None:
            raise StreamWriteAuthorityRequired(
                "jobless stream append cannot accept a job lease",
            )
        cancel_requested = (
            await self._authorize_stream_lease(
                session,
                project_id=project_id,
                owner_user_id=owner_user_id,
                run_id=run_id,
                job_id=job_id,
                lease=lease,
            )
            if job_id is not None and lease is not None
            else False
        )
        if cancel_requested:
            if not frame.terminal:
                raise StreamWriteCancelled(
                    "cancelled execution cannot append a data frame",
                )
            frame = StreamFrame.end(status="interrupted")

        db_content, metadata = self._content_to_db(
            frame.data,
            {"stream_terminal": frame.terminal},
        )
        row = RunEventRow(
            thread_id=thread_id,
            run_id=run_id,
            project_id=project_id,
            owner_user_id=owner_user_id,
            event_type="stream.end" if frame.terminal else frame.event,
            category="stream",
            content=db_content,
            event_metadata=metadata,
            seq=(max_seq or 0) + 1,
            created_at=datetime.now(UTC),
        )
        session.add(row)
        await session.flush()
        return self._stream_row(row, created=True)

    async def list_stream_frames(
        self,
        session: AsyncSession,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        cursor: int,
        limit: int,
        run_id: str | None = None,
    ) -> tuple[StoredStreamFrame, ...]:
        if type(cursor) is not int or cursor < 0:
            raise ValueError("stream cursor must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("stream limit must be between 1 and 1000")
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

        max_seq = await session.scalar(
            select(func.max(RunEventRow.seq)).where(
                RunEventRow.project_id == project_id,
                RunEventRow.owner_user_id == owner_user_id,
                RunEventRow.thread_id == thread_id,
            )
        )
        if cursor > (max_seq or 0):
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
        rows = (await session.execute(statement.order_by(RunEventRow.seq.asc()).limit(limit))).scalars()
        return tuple(self._stream_row(row, created=False) for row in rows)

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
    async def _max_seq_for_thread(
        session: AsyncSession,
        thread_id: str,
        scope: PrivateResourceScope | None = None,
    ) -> int | None:
        """Return the current max seq while serializing writers per thread.

        PostgreSQL rejects ``SELECT max(...) FOR UPDATE`` because aggregate
        results are not lockable rows. As a release-safe workaround, take a
        transaction-level advisory lock keyed by thread_id before reading the
        aggregate. Other dialects keep the existing row-locking statement.
        """
        stmt = select(func.max(RunEventRow.seq)).where(RunEventRow.thread_id == thread_id)
        if scope is not None:
            stmt = stmt.where(*DbRunEventStore._scope_predicates(scope))
        bind = session.get_bind()
        dialect_name = bind.dialect.name if bind is not None else ""

        if dialect_name == "postgresql":
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(CAST(:thread_id AS text))::bigint)"),
                {"thread_id": thread_id},
            )
            return await session.scalar(stmt)

        return await session.scalar(stmt.with_for_update())

    async def put(self, *, thread_id, run_id, event_type, category, content="", metadata=None, created_at=None, scope=None):  # noqa: D401
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
                project_id, owner_user_id = await self._require_parent_run(
                    session,
                    scope=scope,
                    thread_id=thread_id,
                    run_id=run_id,
                )
                max_seq = await self._max_seq_for_thread(session, thread_id, scope)
                seq = (max_seq or 0) + 1
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

    async def put_batch(self, events, *, scope=None):
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
                for run_id in {e["run_id"] for e in events}:
                    parent_by_run[run_id] = await self._require_parent_run(
                        session,
                        scope=scope,
                        thread_id=thread_id,
                        run_id=run_id,
                    )
                max_seq = await self._max_seq_for_thread(session, thread_id, scope)
                seq = max_seq or 0
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
