from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from deerflow.agents.memory.dream import DREAM_PROMPT_VERSION, EMPTY_MEMORY_DOCUMENT
from deerflow.persistence.private_work.memory_document_model import (
    MemoryDocumentVersionRow,
    MemoryDreamRunRow,
)
from deerflow.persistence.private_work.memory_document_repository import (
    MemoryDocumentConflict,
    MemoryDocumentRepository,
    MemoryDocumentScope,
    MemoryDreamFrozenRuntime,
    compute_dream_history_digest,
    memory_document_digest,
)

NOW = datetime(2026, 8, 5, 1, 2, 3, tzinfo=UTC)


class _Result:
    def __init__(self, value=None, *, rows=()) -> None:
        self.value = value
        self.rows = tuple(rows)

    def scalar_one(self):
        assert self.value is not None
        return self.value

    def scalar_one_or_none(self):
        return self.value

    def one_or_none(self):
        return self.value

    def scalars(self):
        return iter(self.rows)

    def __iter__(self):
        return iter(self.rows)


class _Session:
    def __init__(self, *results: _Result, scalars: list[int] | None = None) -> None:
        self.results = list(results)
        self.scalars = list(scalars or [])
        self.added: list[object] = []
        self.executed: list[object] = []
        self.flushes = 0

    async def execute(self, statement):
        self.executed.append(statement)
        return self.results.pop(0)

    async def scalar(self, statement):
        self.executed.append(statement)
        return self.scalars.pop(0)

    def add(self, value) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flushes += 1


class _Jobs:
    def __init__(self) -> None:
        self.job_id = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        self.enqueued = []
        self.succeeded = []
        self.failed = []
        self.cancelled = []

    async def enqueue(self, request):
        self.enqueued.append(request)
        return self.job_id

    async def settle_success(self, job_id, **kwargs):
        self.succeeded.append((job_id, kwargs))
        return True

    async def settle_cancelled(self, job_id, **kwargs):
        self.cancelled.append((job_id, kwargs))
        return True

    async def retry_or_dead(self, job_id, **kwargs):
        self.failed.append((job_id, kwargs))
        return True


def _scope() -> MemoryDocumentScope:
    return MemoryDocumentScope(
        project_id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
        owner_user_id="22222222-2222-4222-8222-222222222222",
        namespace="default",
    )


def _document(*, active_job_id=None, version: int = 4, cursor: int = 8):
    return SimpleNamespace(
        project_id=_scope().project_id,
        owner_user_id=_scope().owner_user_id,
        namespace="default",
        content=EMPTY_MEMORY_DOCUMENT,
        content_digest=memory_document_digest(EMPTY_MEMORY_DOCUMENT),
        version=version,
        dream_cursor=cursor,
        active_dream_job_id=active_job_id,
        updated_at=NOW,
    )


def _history(sequence: int):
    text = f"- [durable] history-{sequence}"
    return SimpleNamespace(
        id=uuid.uuid5(uuid.NAMESPACE_URL, f"history-{sequence}"),
        sequence=sequence,
        project_id=_scope().project_id,
        owner_user_id=_scope().owner_user_id,
        namespace="default",
        status="pending",
        tagged_text=text,
        content_digest=hashlib.sha256(text.encode()).hexdigest(),
        dream_job_id=None,
        consumed_at=None,
    )


def _frozen() -> MemoryDreamFrozenRuntime:
    return MemoryDreamFrozenRuntime(
        preference_version=6,
        policy_revision=17,
        model_config_id=uuid.UUID("33333333-3333-4333-8333-333333333333"),
        model_version_id=uuid.UUID("44444444-4444-4444-8444-444444444444"),
        model_payload_checksum="a" * 64,
        prompt_version=DREAM_PROMPT_VERSION,
    )


@pytest.mark.asyncio
async def test_automatic_due_queries_include_latest_dream_job_activity_cooldown() -> None:
    list_session = _Session(_Result(rows=()))
    await MemoryDocumentRepository(list_session, jobs=_Jobs()).list_due_scopes(
        now=NOW,
        interval_minutes=15,
    )

    recheck_session = _Session(scalars=[None])
    assert not await MemoryDocumentRepository(
        recheck_session,
        jobs=_Jobs(),
    ).is_scope_due(
        _scope(),
        now=NOW,
        interval_minutes=15,
    )

    for statement in (
        list_session.executed[-1],
        recheck_session.executed[-1],
    ):
        sql = str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        assert "memory_dream_runs" in sql
        assert "jobs.updated_at" in sql
        assert "memory_dream_runs.created_at" in sql


@pytest.mark.asyncio
async def test_admission_freezes_exact_oldest_twenty_and_serializes_scope() -> None:
    document = _document()
    history = [_history(index) for index in range(1, 26)]
    jobs = _Jobs()
    session = _Session(
        _Result(),
        _Result(document),
        _Result(rows=history),
        scalars=[0],
    )
    repository = MemoryDocumentRepository(session, jobs=jobs)

    result = await repository.admit_dream(
        _scope(),
        trigger="manual_dream",
        frozen=_frozen(),
        initial_content=EMPTY_MEMORY_DOCUMENT,
        now=NOW,
    )

    assert result.disposition == "queued"
    assert result.history_count == 20
    assert [row.status for row in history[:20]] == ["processing"] * 20
    assert [row.status for row in history[20:]] == ["pending"] * 5
    assert document.active_dream_job_id == jobs.job_id
    run = next(value for value in session.added if isinstance(value, MemoryDreamRunRow))
    assert (run.history_from, run.history_to, run.history_count) == (1, 20, 20)
    assert run.preference_version == 6
    assert run.policy_revision == 17
    assert run.model_ref == _frozen().model_version_id
    assert run.prompt_version == DREAM_PROMPT_VERSION


@pytest.mark.asyncio
async def test_competing_admission_returns_the_same_active_job() -> None:
    jobs = _Jobs()
    document = _document(active_job_id=jobs.job_id)
    run = SimpleNamespace(history_count=12)
    active_job = SimpleNamespace(
        id=jobs.job_id,
        status="running",
    )
    session = _Session(
        _Result(),
        _Result(document),
        _Result((active_job, run)),
    )

    result = await MemoryDocumentRepository(session, jobs=jobs).admit_dream(
        _scope(),
        trigger="auto_dream",
        frozen=_frozen(),
        initial_content=EMPTY_MEMORY_DOCUMENT,
        now=NOW,
    )

    assert result.disposition == "already_running"
    assert result.job_id == jobs.job_id
    assert result.history_count == 12
    assert not jobs.enqueued


@pytest.mark.asyncio
async def test_finalize_consumes_tombstones_advances_cursor_and_settles_job() -> None:
    jobs = _Jobs()
    document = _document(active_job_id=jobs.job_id)
    rows = [_history(9), _history(10)]
    for row in rows:
        row.status = "processing"
        row.dream_job_id = jobs.job_id
    records = tuple(
        SimpleNamespace(
            id=row.id,
            sequence=row.sequence,
            tagged_text=row.tagged_text,
            content_digest=row.content_digest,
        )
        for row in rows
    )
    digest = compute_dream_history_digest(records)
    run = SimpleNamespace(
        job_id=jobs.job_id,
        trigger="manual_dream",
        history_from=9,
        history_to=10,
        history_count=2,
        history_digest=digest,
        base_document_version=4,
        base_content_digest=document.content_digest,
        prompt_version=DREAM_PROMPT_VERSION,
        model_ref=_frozen().model_version_id,
        result_version=None,
        completed_at=None,
    )
    session = _Session(
        _Result(None),
        _Result(document),
        _Result(run),
        _Result(rows=rows),
    )
    repository = MemoryDocumentRepository(session, jobs=jobs)

    version = await repository.finalize_dream(
        _scope(),
        job_id=jobs.job_id,
        lease_token="lease",
        expected_history_digest=digest,
        expected_base_version=4,
        expected_base_digest=document.content_digest,
        content=EMPTY_MEMORY_DOCUMENT,
        now=NOW,
    )

    assert version.version == 5
    assert version.unified_diff == ""
    assert document.version == 5
    assert document.dream_cursor == 10
    assert document.active_dream_job_id is None
    assert [(row.status, row.tagged_text) for row in rows] == [
        ("consumed", None),
        ("consumed", None),
    ]
    assert run.result_version == 5
    assert jobs.succeeded[0][0] == jobs.job_id


@pytest.mark.asyncio
async def test_finalize_retry_returns_the_existing_version_without_resettling() -> None:
    jobs = _Jobs()
    existing = SimpleNamespace(
        version=5,
        content=EMPTY_MEMORY_DOCUMENT,
        content_digest=memory_document_digest(EMPTY_MEMORY_DOCUMENT),
        unified_diff="",
        trigger="manual_dream",
        dream_job_id=jobs.job_id,
        history_from=9,
        history_to=10,
        history_count=2,
        prompt_version=DREAM_PROMPT_VERSION,
        model_ref=_frozen().model_version_id,
        created_at=NOW,
    )
    repository = MemoryDocumentRepository(
        _Session(_Result(existing)),
        jobs=jobs,
    )

    version = await repository.finalize_dream(
        _scope(),
        job_id=jobs.job_id,
        lease_token="already-settled",
        expected_history_digest="b" * 64,
        expected_base_version=4,
        expected_base_digest="c" * 64,
        content=EMPTY_MEMORY_DOCUMENT,
        now=NOW,
    )

    assert version.version == 5
    assert version.dream_job_id == jobs.job_id
    assert jobs.succeeded == []


@pytest.mark.asyncio
async def test_transient_failure_keeps_processing_batch_reserved_for_retry() -> None:
    jobs = _Jobs()
    session = _Session(scalars=[None, "retry_wait"])
    repository = MemoryDocumentRepository(session, jobs=jobs)

    assert await repository.release_dream(
        _scope(),
        job_id=jobs.job_id,
        lease_token="lease",
        now=NOW,
        cancelled=False,
        public_error_code="MEMORY_DREAM_TIMEOUT",
    )

    assert jobs.failed[0][1]["retryable"] is True
    assert session.executed
    assert len(session.executed) == 2


@pytest.mark.asyncio
async def test_exhausted_failure_releases_active_batch_back_to_pending() -> None:
    jobs = _Jobs()
    document = _document(active_job_id=jobs.job_id)
    run = SimpleNamespace(result_version=None)
    session = _Session(
        _Result(document),
        _Result(run),
        _Result(),
        scalars=[None, "dead"],
    )
    repository = MemoryDocumentRepository(session, jobs=jobs)

    assert await repository.release_dream(
        _scope(),
        job_id=jobs.job_id,
        lease_token="lease",
        now=NOW,
        cancelled=False,
        public_error_code="MEMORY_DREAM_TIMEOUT",
    )

    assert document.active_dream_job_id is None
    assert len(session.executed) == 5


@pytest.mark.asyncio
async def test_restore_is_new_version_and_never_rolls_back_dream_cursor() -> None:
    document = _document(version=12, cursor=99)
    target = SimpleNamespace(
        content=EMPTY_MEMORY_DOCUMENT,
        content_digest=memory_document_digest(EMPTY_MEMORY_DOCUMENT),
    )
    session = _Session(_Result(document), _Result(target))
    repository = MemoryDocumentRepository(session, jobs=_Jobs())

    restored = await repository.restore_version(
        _scope(),
        target_version=4,
        expected_current_version=12,
        now=NOW,
    )

    assert restored.version == 13
    assert restored.trigger == "restore"
    assert document.version == 13
    assert document.dream_cursor == 99
    assert isinstance(session.added[0], MemoryDocumentVersionRow)


@pytest.mark.asyncio
async def test_restore_rejects_active_dream_or_stale_cas() -> None:
    jobs = _Jobs()
    for document in (
        _document(active_job_id=jobs.job_id, version=12),
        _document(version=13),
    ):
        repository = MemoryDocumentRepository(
            _Session(_Result(document)),
            jobs=jobs,
        )
        with pytest.raises(MemoryDocumentConflict):
            await repository.restore_version(
                _scope(),
                target_version=4,
                expected_current_version=12,
                now=NOW,
            )
