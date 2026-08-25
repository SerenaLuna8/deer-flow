"""Lock-order contracts for every PostgreSQL Run-event writer."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import text
from support.run_closure import add_sealed_test_run

from deerflow.persistence.models.run_event import ThreadEventSequenceRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.thread_meta import ThreadMetaRepository
from deerflow.runtime.events.models import (
    StreamFrame,
    StreamLeaseProof,
    StreamWriteAuthorizationRevoked,
)
from deerflow.runtime.events.store.db import DbRunEventStore
from deerflow.runtime.private_scope import PrivateResourceScope


class _StatementResult:
    def __init__(self, value=None) -> None:
        self._value = value

    def one_or_none(self):
        return self._value

    def scalar_one_or_none(self):
        return self._value


class _Bind:
    dialect = SimpleNamespace(name="sqlite")


class _Session:
    def __init__(self, terminal) -> None:
        self._terminal = terminal

    def get_bind(self):
        return _Bind()

    async def execute(self, _statement, _params=None):
        return _StatementResult(self._terminal)


class _AsyncContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False


class _WritingSession(_Session):
    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    def begin(self):
        return _AsyncContext()

    def add(self, _row) -> None:
        return None


def _scope() -> PrivateResourceScope:
    return PrivateResourceScope(
        project_id=str(uuid.uuid4()),
        owner_user_id=str(uuid.uuid4()),
        membership_version=1,
    )


@pytest.mark.asyncio
async def test_append_stream_frame_locks_governance_before_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream writer must never own the sequence while waiting on authority."""

    calls: list[str] = []
    store = DbRunEventStore(AsyncMock(), run_event_notify_enabled=False)

    async def governance(*_args, **_kwargs):
        calls.append("project-membership-run")

    async def sequence(*_args, **_kwargs):
        calls.append("sequence")
        return SimpleNamespace(high_watermark=0)

    monkeypatch.setattr(store, "_require_authorized_event_parent", governance)
    monkeypatch.setattr(store, "_lock_event_sequence", sequence)
    monkeypatch.setattr(store, "_advance_event_sequence", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(store, "_notify_stream_append", AsyncMock())

    class _TerminalSession(_Session):
        async def execute(self, _statement, _params=None):
            calls.append("terminal-recheck")
            return _StatementResult(None)

    session = _TerminalSession(None)
    session.add = lambda _row: None  # type: ignore[attr-defined]
    session.flush = AsyncMock()  # type: ignore[attr-defined]

    await store.append_stream_frame(
        session,  # type: ignore[arg-type]
        scope=_scope(),
        thread_id="thread-1",
        run_id="run-1",
        frame=StreamFrame(event="values", data={"step": 1}),
    )

    assert calls == [
        "project-membership-run",
        "sequence",
        "terminal-recheck",
    ]


@pytest.mark.asyncio
async def test_job_stream_validates_exact_lease_before_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    store = DbRunEventStore(AsyncMock(), run_event_notify_enabled=False)
    job_id = uuid.uuid4()
    lease = StreamLeaseProof(job_id=job_id, lease_token="exact-current-token")

    async def authorize(*_args, **kwargs):
        calls.append("project-membership-job-run")
        assert kwargs["thread_id"] == "thread-1"
        assert kwargs["run_id"] == "run-1"
        assert kwargs["lease"] is lease
        return False

    async def sequence(*_args, **_kwargs):
        calls.append("sequence")
        return SimpleNamespace(high_watermark=0)

    monkeypatch.setattr(store, "_authorize_stream_lease", authorize)
    monkeypatch.setattr(store, "_lock_event_sequence", sequence)
    monkeypatch.setattr(store, "_advance_event_sequence", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(store, "_notify_stream_append", AsyncMock())
    session = _Session(None)
    session.add = lambda _row: None  # type: ignore[attr-defined]
    session.flush = AsyncMock()  # type: ignore[attr-defined]

    await store.append_stream_frame(
        session,  # type: ignore[arg-type]
        scope=_scope(),
        thread_id="thread-1",
        run_id="run-1",
        frame=StreamFrame(event="values", data={"step": 1}),
        lease=lease,
    )

    assert calls == ["project-membership-job-run", "sequence"]


@pytest.mark.asyncio
async def test_unleased_jobless_event_writer_locks_governance_and_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    scope = _scope()
    project_id = uuid.UUID(scope.project_id)
    owner_user_id = scope.owner_user_id
    store = DbRunEventStore(AsyncMock(), run_event_notify_enabled=False)
    parent = SimpleNamespace(
        project_id=project_id,
        owner_user_id=owner_user_id,
        job_id=None,
    )

    async def governance(*_args, **_kwargs):
        calls.append("project-membership")

    class _ParentSession(_Session):
        async def execute(self, _statement, _params=None):
            calls.append("run")
            return _StatementResult(parent)

    monkeypatch.setattr(
        DbRunEventStore,
        "_lock_stream_governance",
        staticmethod(governance),
    )

    coordinates = await store._require_authorized_event_parent(
        _ParentSession(None),  # type: ignore[arg-type]
        scope=scope,
        thread_id="thread-1",
        run_id="run-1",
        lease=None,
    )

    assert calls == ["project-membership", "run"]
    assert coordinates == (project_id, owner_user_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ("put", "put_batch"))
async def test_ordinary_event_writers_lock_parent_before_sequence(
    monkeypatch: pytest.MonkeyPatch,
    writer: str,
) -> None:
    calls: list[str] = []
    project_id = uuid.uuid4()
    owner_user_id = str(uuid.uuid4())
    session = _WritingSession(None)
    session.flush = AsyncMock()  # type: ignore[attr-defined]
    store = DbRunEventStore(lambda: session, run_event_notify_enabled=False)

    async def parent(*_args, **_kwargs):
        calls.append("project-membership-job-run")
        return project_id, owner_user_id

    async def sequence(*_args, **_kwargs):
        calls.append("sequence")
        return SimpleNamespace(high_watermark=0)

    monkeypatch.setattr(store, "_require_authorized_event_parent", parent)
    monkeypatch.setattr(store, "_lock_event_sequence", sequence)
    monkeypatch.setattr(store, "_row_to_dict", lambda row: {"seq": row.seq})

    event = {
        "thread_id": "thread-1",
        "run_id": "run-1",
        "event_type": "on_chain_start",
        "category": "trace",
        "content": {"step": 1},
    }
    if writer == "put":
        await store.put(**event, scope=_scope())
    else:
        await store.put_batch([event], scope=_scope())

    assert calls == ["project-membership-job-run", "sequence"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("job_status", "run_status", "run_error", "terminal_status", "error_code"),
    [
        ("succeeded", "success", None, "completed", None),
        (
            "dead",
            "error",
            "OUTPUT_DELIVERY_INCOMPLETE",
            "error",
            "OUTPUT_DELIVERY_INCOMPLETE",
        ),
        (
            "dead",
            "error",
            "CURRENT_UPLOAD_UNAVAILABLE",
            "error",
            "CURRENT_UPLOAD_UNAVAILABLE",
        ),
    ],
)
async def test_terminal_repair_locks_governance_job_run_before_sequence(
    monkeypatch: pytest.MonkeyPatch,
    job_status: str,
    run_status: str,
    run_error: str | None,
    terminal_status: str,
    error_code: str | None,
) -> None:
    calls: list[str] = []
    store = DbRunEventStore(AsyncMock(), run_event_notify_enabled=False)
    job_id = uuid.uuid4()
    job = SimpleNamespace(status=job_status)
    run = SimpleNamespace(job_id=job_id, status=run_status, error=run_error)

    async def governance(*_args, **_kwargs):
        calls.append("project-membership")

    async def sequence(*_args, **_kwargs):
        calls.append("sequence")
        return SimpleNamespace(high_watermark=0)

    class _RepairSession(_Session):
        def __init__(self) -> None:
            super().__init__(None)
            self._execute_values = iter((job, run, None))

        async def scalar(self, _statement):
            calls.append("run-coordinate")
            return job_id

        async def execute(self, _statement, _params=None):
            value = next(self._execute_values)
            if value is job:
                calls.append("job")
            elif value is run:
                calls.append("run")
            else:
                calls.append("terminal-recheck")
            return _StatementResult(value)

    monkeypatch.setattr(store, "_lock_stream_governance", governance)
    monkeypatch.setattr(store, "_lock_event_sequence", sequence)
    monkeypatch.setattr(store, "_advance_event_sequence", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(store, "_notify_stream_append", AsyncMock())
    session = _RepairSession()
    session.add = lambda _row: None  # type: ignore[attr-defined]
    session.flush = AsyncMock()  # type: ignore[attr-defined]

    await store.ensure_settled_stream_terminal(
        session,  # type: ignore[arg-type]
        scope=_scope(),
        thread_id="thread-1",
        run_id="run-1",
        status=terminal_status,
        error_code=error_code,
    )

    assert calls == [
        "project-membership",
        "run-coordinate",
        "job",
        "run",
        "sequence",
        "terminal-recheck",
    ]


async def _seed_active_job_run(seed, *, label: str):
    scope = seed.owner_a_scope
    thread_id = f"elo-{label[:12]}-{uuid.uuid4().hex[:16]}"
    run_id = str(uuid.uuid4())
    job_id = uuid.uuid4()
    lease_token = f"event-lock-order-{uuid.uuid4()}"
    token_hash = hashlib.sha256(lease_token.encode()).hexdigest()
    await ThreadMetaRepository(seed.factory).create(
        thread_id,
        display_name=f"Event lock order {label}",
        scope=scope,
        agent_asset_id=seed.project_agent_id,
        agent_scope="project",
    )
    async with seed.factory() as session, session.begin():
        trace_id = uuid.uuid4().hex
        await add_sealed_test_run(
            session,
            RunRow(
                run_id=run_id,
                thread_id=thread_id,
                assistant_id=str(seed.project_agent_id),
                owner_user_id=scope.owner_user_id,
                status="pending",
                multitask_strategy="reject",
                metadata_json={},
                kwargs_json={},
                origin_trace_id=trace_id,
                project_id=uuid.UUID(scope.project_id),
            ),
        )
        await session.execute(
            text(
                """INSERT INTO jobs
                (id,job_type,project_id,owner_user_id,owner_private_generation,
                 run_id,origin_trace_id,
                 idempotency_key,status,max_attempts,lease_token_hash,
                 lease_expires_at,retry_safety)
                VALUES
                (:job_id,'private_run',:project_id,:owner_user_id,1,:run_id,
                 :trace_id,:idempotency_key,'running',1,:token_hash,
                 now() + interval '60 seconds','safe')"""
            ),
            {
                "job_id": job_id,
                "project_id": uuid.UUID(scope.project_id),
                "owner_user_id": scope.owner_user_id,
                "run_id": run_id,
                "trace_id": trace_id,
                "idempotency_key": hashlib.sha256(f"event-lock-order:{run_id}".encode()).hexdigest(),
                "token_hash": token_hash,
            },
        )
        await session.execute(
            text(
                """UPDATE runs
                SET job_id=:job_id,status='running',
                    execution_lease_token_hash=:token_hash,
                    execution_lease_expires_at=now() + interval '60 seconds'
                WHERE run_id=:run_id"""
            ),
            {
                "job_id": job_id,
                "token_hash": token_hash,
                "run_id": run_id,
            },
        )
        session.add(
            ThreadEventSequenceRow(
                project_id=uuid.UUID(scope.project_id),
                owner_user_id=scope.owner_user_id,
                thread_id=thread_id,
                high_watermark=0,
            )
        )
    return SimpleNamespace(
        thread_id=thread_id,
        run_id=run_id,
        lease=StreamLeaseProof(
            job_id=job_id,
            lease_token=lease_token,
        ),
    )


async def _append_job_stream(store, seed, active, *, step: int = 1) -> None:
    async with seed.factory() as session, session.begin():
        await store.append_stream_frame(
            session,
            scope=seed.owner_a_scope,
            thread_id=active.thread_id,
            run_id=active.run_id,
            frame=StreamFrame(event="values", data={"step": step}),
            lease=active.lease,
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_stream_and_journal_writers_complete_concurrently_without_deadlock(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the production lease path against independent PG sessions."""

    from support.private_thread_seed import seed_private_thread_database

    seed = await seed_private_thread_database(migrated_postgres_database_url)
    scope = seed.owner_a_scope
    try:
        active = await _seed_active_job_run(seed, label="journal-deadlock")
        store = DbRunEventStore(seed.factory, run_event_notify_enabled=False)
        stream_at_sequence = asyncio.Event()
        release_stream_sequence = asyncio.Event()
        journal_authorization_started = asyncio.Event()
        journal_authorized = asyncio.Event()
        journal_backend_pid: int | None = None
        original_lock_sequence = store._lock_event_sequence
        original_authorize_lease = store._authorize_stream_lease

        async def gated_lock_sequence(session, **kwargs):
            sequence = await original_lock_sequence(session, **kwargs)
            current = asyncio.current_task()
            if current is not None and current.get_name() == "stream-writer":
                stream_at_sequence.set()
                await release_stream_sequence.wait()
            return sequence

        async def record_authorized_lease(session, **kwargs):
            nonlocal journal_backend_pid
            current = asyncio.current_task()
            if current is not None and current.get_name() == "journal-writer":
                journal_backend_pid = await session.scalar(
                    text("SELECT pg_backend_pid()"),
                )
                journal_authorization_started.set()
            cancelled = await original_authorize_lease(session, **kwargs)
            if current is not None and current.get_name() == "journal-writer":
                journal_authorized.set()
            return cancelled

        async def journal_acquired_authority_before_sequence() -> bool:
            await journal_authorization_started.wait()
            assert journal_backend_pid is not None
            while True:
                if journal_authorized.is_set():
                    return True
                async with seed.engine.connect() as connection:
                    wait_event_type = await connection.scalar(
                        text(
                            """SELECT wait_event_type FROM pg_stat_activity
                            WHERE pid=:pid"""
                        ),
                        {"pid": journal_backend_pid},
                    )
                if wait_event_type == "Lock":
                    return False
                await asyncio.sleep(0.01)

        monkeypatch.setattr(store, "_lock_event_sequence", gated_lock_sequence)
        monkeypatch.setattr(
            DbRunEventStore,
            "_authorize_stream_lease",
            staticmethod(record_authorized_lease),
        )

        async def append_stream() -> None:
            async with seed.factory() as session, session.begin():
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                await store.append_stream_frame(
                    session,
                    scope=scope,
                    thread_id=active.thread_id,
                    run_id=active.run_id,
                    frame=StreamFrame(event="values", data={"step": 1}),
                    lease=active.lease,
                )

        async def append_journal() -> None:
            await store.put_batch(
                [
                    {
                        "thread_id": active.thread_id,
                        "run_id": active.run_id,
                        "event_type": "on_chain_start",
                        "category": "trace",
                        "content": {"step": 1},
                    }
                ],
                scope=scope,
                lease=active.lease,
            )

        stream_task = asyncio.create_task(append_stream(), name="stream-writer")
        journal_task: asyncio.Task | None = None
        try:
            await asyncio.wait_for(stream_at_sequence.wait(), timeout=5)
            journal_task = asyncio.create_task(append_journal(), name="journal-writer")
            journal_authorized_before_release = await asyncio.wait_for(
                journal_acquired_authority_before_sequence(),
                timeout=5,
            )
            release_stream_sequence.set()
            await asyncio.wait_for(
                asyncio.gather(stream_task, journal_task),
                timeout=10,
            )
        finally:
            release_stream_sequence.set()
            pending = tuple(task for task in (stream_task, journal_task) if task is not None and not task.done())
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        assert journal_authorized_before_release is False

        events = await store.list_events(
            active.thread_id,
            active.run_id,
            scope=scope,
        )
        assert len(events) == 2
        assert [event["seq"] for event in events] == [1, 2]
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run_event_notify_enabled", "expected_statement_count"),
    (
        pytest.param(False, 10, id="core-write"),
        pytest.param(True, 11, id="default-with-notify"),
    ),
)
async def test_job_owned_append_has_bounded_client_statement_count(
    migrated_postgres_database_url: str,
    run_event_notify_enabled: bool,
    expected_statement_count: int,
) -> None:
    from support.private_thread_seed import seed_private_thread_database

    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        active = await _seed_active_job_run(seed, label="statement-budget")
        store = DbRunEventStore(
            seed.factory,
            run_event_notify_enabled=run_event_notify_enabled,
        )
        statements: list[str] = []

        def record_statement(
            _connection,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            statements.append(" ".join(statement.split()))

        sqlalchemy_event.listen(
            seed.engine.sync_engine,
            "before_cursor_execute",
            record_statement,
        )
        try:
            await _append_job_stream(store, seed, active)
        finally:
            sqlalchemy_event.remove(
                seed.engine.sync_engine,
                "before_cursor_execute",
                record_statement,
            )

        assert len(statements) == expected_statement_count, statements
        notify_statements = [statement for statement in statements if "pg_notify" in statement]
        assert len(notify_statements) == int(run_event_notify_enabled)
        governance_locks = [statement for statement in statements if "FROM projects" in statement or "FROM project_memberships" in statement]
        assert len(governance_locks) == 2
        assert all(" FOR SHARE" in statement for statement in governance_locks)
        assert all("FOR KEY SHARE" not in statement for statement in governance_locks)
        job_locks = [statement for statement in statements if "FROM jobs" in statement]
        run_locks = [statement for statement in statements if "FROM runs" in statement]
        assert len(job_locks) == len(run_locks) == 1
        assert " FOR UPDATE" in job_locks[0]
        assert " FOR UPDATE" in run_locks[0]
        assert all(
            column in job_locks[0]
            for column in (
                "jobs.id",
                "jobs.project_id",
                "jobs.owner_user_id",
                "jobs.run_id",
            )
        )
        assert all(
            column in run_locks[0]
            for column in (
                "runs.project_id",
                "runs.owner_user_id",
                "runs.thread_id",
                "runs.run_id",
                "runs.job_id",
            )
        )
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_same_project_different_run_governance_reads_do_not_block(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from support.private_thread_seed import seed_private_thread_database

    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        first = await _seed_active_job_run(seed, label="parallel-first")
        second = await _seed_active_job_run(seed, label="parallel-second")
        store = DbRunEventStore(seed.factory, run_event_notify_enabled=False)
        first_authorized = asyncio.Event()
        release_first = asyncio.Event()
        second_authorized = asyncio.Event()
        original_authorize = DbRunEventStore._authorize_stream_lease

        async def gated_authorize(session, **kwargs):
            cancelled = await original_authorize(session, **kwargs)
            current = asyncio.current_task()
            if current is not None and current.get_name() == "first-run-writer":
                first_authorized.set()
                await release_first.wait()
            elif current is not None and current.get_name() == "second-run-writer":
                second_authorized.set()
            return cancelled

        monkeypatch.setattr(
            DbRunEventStore,
            "_authorize_stream_lease",
            staticmethod(gated_authorize),
        )
        first_task = asyncio.create_task(
            _append_job_stream(store, seed, first),
            name="first-run-writer",
        )
        second_task: asyncio.Task | None = None
        try:
            await asyncio.wait_for(first_authorized.wait(), timeout=5)
            second_task = asyncio.create_task(
                _append_job_stream(store, seed, second),
                name="second-run-writer",
            )
            await asyncio.wait_for(second_authorized.wait(), timeout=5)
            release_first.set()
            await asyncio.wait_for(
                asyncio.gather(first_task, second_task),
                timeout=10,
            )
        finally:
            release_first.set()
            pending = tuple(task for task in (first_task, second_task) if task is not None and not task.done())
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_governance_revocation_update_waits_for_authorized_append(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from support.private_thread_seed import seed_private_thread_database

    seed = await seed_private_thread_database(migrated_postgres_database_url)
    scope = seed.owner_a_scope
    try:
        active = await _seed_active_job_run(seed, label="revocation")
        store = DbRunEventStore(seed.factory, run_event_notify_enabled=False)
        writer_authorized = asyncio.Event()
        release_writer = asyncio.Event()
        revocation_started = asyncio.Event()
        revocation_committed = asyncio.Event()
        revocation_backend_pid: int | None = None
        original_authorize = DbRunEventStore._authorize_stream_lease

        async def gated_authorize(session, **kwargs):
            cancelled = await original_authorize(session, **kwargs)
            writer_authorized.set()
            await release_writer.wait()
            return cancelled

        monkeypatch.setattr(
            DbRunEventStore,
            "_authorize_stream_lease",
            staticmethod(gated_authorize),
        )

        async def revoke_membership() -> None:
            nonlocal revocation_backend_pid
            async with seed.factory() as session, session.begin():
                revocation_backend_pid = await session.scalar(
                    text("SELECT pg_backend_pid()"),
                )
                revocation_started.set()
                await session.execute(
                    text(
                        """UPDATE project_memberships
                        SET status='removed',end_reason='removed',ended_at=now(),
                            version=version + 1
                        WHERE project_id=:project_id AND user_id=:owner_user_id"""
                    ),
                    {
                        "project_id": uuid.UUID(scope.project_id),
                        "owner_user_id": scope.owner_user_id,
                    },
                )
            revocation_committed.set()

        async def revocation_is_waiting_on_writer() -> bool:
            await revocation_started.wait()
            assert revocation_backend_pid is not None
            while True:
                if revocation_committed.is_set():
                    return False
                async with seed.engine.connect() as connection:
                    wait_event_type = await connection.scalar(
                        text(
                            """SELECT wait_event_type FROM pg_stat_activity
                            WHERE pid=:pid"""
                        ),
                        {"pid": revocation_backend_pid},
                    )
                if wait_event_type == "Lock":
                    return True
                await asyncio.sleep(0.01)

        writer_task = asyncio.create_task(
            _append_job_stream(store, seed, active),
            name="authorized-writer",
        )
        revocation_task: asyncio.Task | None = None
        try:
            await asyncio.wait_for(writer_authorized.wait(), timeout=5)
            revocation_task = asyncio.create_task(revoke_membership())
            assert await asyncio.wait_for(
                revocation_is_waiting_on_writer(),
                timeout=5,
            )
            release_writer.set()
            await asyncio.wait_for(
                asyncio.gather(writer_task, revocation_task),
                timeout=10,
            )
        finally:
            release_writer.set()
            pending = tuple(task for task in (writer_task, revocation_task) if task is not None and not task.done())
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        assert revocation_committed.is_set()
        with pytest.raises(StreamWriteAuthorizationRevoked):
            await _append_job_stream(store, seed, active, step=2)
    finally:
        await seed.engine.dispose()
