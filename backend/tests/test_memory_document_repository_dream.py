from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from deerflow.agents.memory.dream import (
    DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
    DREAM_PROMPT_VERSION,
    EMPTY_MEMORY_DOCUMENT,
)
from deerflow.persistence.private_work.memory_document_model import (
    MemoryDocumentVersionRow,
)
from deerflow.persistence.private_work.memory_document_repository import (
    MemoryDocumentConflict,
    MemoryDocumentRepository,
    MemoryDocumentScope,
    MemoryDreamFrozenRuntime,
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
        sections=list(DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES),
        sections_policy_section="memory_document",
        sections_policy_version_id=uuid.UUID("55555555-5555-4555-8555-555555555555"),
        version=version,
        dream_cursor=cursor,
        active_dream_job_id=active_job_id,
        updated_at=NOW,
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
        needs_review=False,
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
        expected_sections=DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
        content=EMPTY_MEMORY_DOCUMENT,
        now=NOW,
    )

    assert version.version == 5
    assert version.dream_job_id == jobs.job_id
    assert jobs.succeeded == []


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
        expected_sections=DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
        max_tokens=8_000,
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
                expected_sections=DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
                max_tokens=8_000,
                now=NOW,
            )
