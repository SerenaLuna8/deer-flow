"""Episodic archive contract: settlement transfer, retention, and reset scope."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

import deerflow.persistence.models  # noqa: F401 -- populate final metadata
from app.private_work.privacy_center import PrivacyCenterService
from app.private_work.retention_purge import purge_private_scope
from deerflow.agents.memory.dream import (
    DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
    DREAM_PROMPT_VERSION,
    EMPTY_MEMORY_DOCUMENT,
)
from deerflow.persistence.base import Base
from deerflow.persistence.private_work.memory_document_model import MemoryEpisodeRow
from deerflow.persistence.private_work.memory_document_repository import (
    DEFAULT_EPISODE_RETENTION_DAYS,
    MemoryDocumentRepository,
    MemoryDocumentScope,
    MemoryDreamHistoryRecord,
    compute_dream_history_digest,
    memory_document_digest,
)

NOW = datetime(2026, 8, 6, 10, 20, 30, tzinfo=UTC)
JOB_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def _scope() -> MemoryDocumentScope:
    return MemoryDocumentScope(
        project_id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
        owner_user_id="22222222-2222-4222-8222-222222222222",
        namespace="default",
    )


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

    def all(self):
        return list(self.rows)

    def __iter__(self):
        return iter(self.rows)


class _Session:
    def __init__(self, *results: _Result, scalars: list | None = None) -> None:
        self.results = list(results)
        self.scalar_values = list(scalars or [])
        self.added: list[object] = []
        self.executed: list[object] = []
        self.flushes = 0

    async def execute(self, statement):
        self.executed.append(statement)
        return self.results.pop(0)

    async def scalar(self, statement):
        self.executed.append(statement)
        return self.scalar_values.pop(0)

    def add(self, value) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flushes += 1


class _Jobs:
    def __init__(self) -> None:
        self.succeeded = []

    async def settle_success(self, job_id, **kwargs):
        self.succeeded.append((job_id, kwargs))
        return True


def _compiled(statement, *, literal: bool = False) -> str:
    kwargs = {"literal_binds": True} if literal else {}
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs=kwargs))


def _history_row(index: int, *, origin: str = "snip") -> SimpleNamespace:
    text = f"- [durable] episode-fact-{index:02d}"
    return SimpleNamespace(
        id=uuid.uuid4(),
        sequence=index,
        project_id=_scope().project_id,
        owner_user_id=_scope().owner_user_id,
        namespace=_scope().namespace,
        thread_id="episode-thread",
        origin=origin,
        status="processing",
        dream_job_id=JOB_ID,
        tagged_text=text,
        content_digest=memory_document_digest(text),
        created_at=NOW - timedelta(hours=index),
        consumed_at=None,
    )


def _finalize_fixture(history_rows):
    history = tuple(
        MemoryDreamHistoryRecord(
            id=row.id,
            sequence=int(row.sequence),
            tagged_text=row.tagged_text,
            content_digest=row.content_digest,
        )
        for row in history_rows
    )
    history_digest = compute_dream_history_digest(history)
    document = SimpleNamespace(
        project_id=_scope().project_id,
        owner_user_id=_scope().owner_user_id,
        namespace=_scope().namespace,
        content=EMPTY_MEMORY_DOCUMENT,
        content_digest=memory_document_digest(EMPTY_MEMORY_DOCUMENT),
        sections=list(DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES),
        sections_policy_section="memory_document",
        sections_policy_version_id=uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        version=4,
        dream_cursor=0,
        active_dream_job_id=JOB_ID,
        updated_at=NOW,
    )
    run = SimpleNamespace(
        job_id=JOB_ID,
        trigger="auto_dream",
        history_from=history_rows[0].sequence,
        history_to=history_rows[-1].sequence,
        history_count=len(history_rows),
        history_digest=history_digest,
        base_document_version=4,
        base_content_digest=document.content_digest,
        prompt_version=DREAM_PROMPT_VERSION,
        model_ref=uuid.uuid4(),
        result_version=None,
        completed_at=None,
    )
    return document, run, history_digest


def test_memory_episode_row_is_scope_bound_and_lifecycle_independent() -> None:
    table = Base.metadata.tables["memory_episodes"]

    assert [column.name for column in table.primary_key.columns] == ["id"]
    assert {column.name for column in table.columns} == {
        "id",
        "project_id",
        "owner_user_id",
        "namespace",
        "thread_id",
        "origin",
        "tagged_text",
        "content_digest",
        "occurred_at",
        "consumed_dream_job_id",
        "created_at",
    }

    foreign_keys = {constraint.name for constraint in table.foreign_key_constraints}
    assert foreign_keys == {
        "fk_memory_episodes_project",
        "fk_memory_episodes_owner",
        "fk_memory_episodes_membership",
    }
    referred = {constraint.referred_table.name for constraint in table.foreign_key_constraints}
    # Episodes must survive Job, Dream-run, document, and Thread deletion.
    assert referred == {"projects", "users", "project_memberships"}

    constraint_names = {constraint.name for constraint in table.constraints}
    assert {
        "ck_memory_episodes_namespace",
        "ck_memory_episodes_origin",
        "ck_memory_episodes_text",
        "ck_memory_episodes_digest",
    } <= constraint_names

    indexes = {index.name: index for index in table.indexes}
    assert set(indexes) == {
        "ix_memory_episodes_scope_time",
        "ix_memory_episodes_trgm",
    }
    trgm = indexes["ix_memory_episodes_trgm"]
    assert trgm.dialect_options["postgresql"]["using"] == "gin"
    assert trgm.dialect_options["postgresql"]["ops"] == {"tagged_text": "gin_trgm_ops"}


@pytest.mark.asyncio
async def test_finalize_dream_copies_history_into_episodes_before_erasing_text() -> None:
    history_rows = (_history_row(9), _history_row(10, origin="tool"))
    document, run, history_digest = _finalize_fixture(history_rows)
    session = _Session(
        _Result(None),
        _Result(document),
        _Result(run),
        _Result(rows=history_rows),
        _Result(),
    )
    jobs = _Jobs()

    version = await MemoryDocumentRepository(session, jobs=jobs).finalize_dream(
        _scope(),
        job_id=JOB_ID,
        lease_token="lease",
        expected_history_digest=history_digest,
        expected_base_version=4,
        expected_base_digest=document.content_digest,
        expected_sections=DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
        content=EMPTY_MEMORY_DOCUMENT + "\n\n- 更新",
        now=NOW,
    )

    assert version.version == 5
    episodes = [value for value in session.added if isinstance(value, MemoryEpisodeRow)]
    assert [episode.id for episode in episodes] == [row.id for row in history_rows]
    for episode, row in zip(episodes, history_rows, strict=True):
        assert episode.project_id == row.project_id
        assert episode.owner_user_id == row.owner_user_id
        assert episode.namespace == row.namespace
        assert episode.thread_id == row.thread_id
        assert episode.origin == row.origin
        assert episode.tagged_text.startswith("- [durable] episode-fact-")
        assert episode.content_digest == memory_document_digest(episode.tagged_text)
        assert episode.occurred_at == row.created_at
        assert episode.consumed_dream_job_id == JOB_ID

    # The tombstone contract is unchanged: text erased in the same settlement.
    assert all(row.status == "consumed" and row.tagged_text is None and row.consumed_at == NOW for row in history_rows)
    assert jobs.succeeded and jobs.succeeded[0][0] == JOB_ID

    prune = _compiled(session.executed[-1])
    assert "DELETE FROM memory_episodes" in prune
    assert "occurred_at" in prune
    assert "LIMIT" in prune


@pytest.mark.asyncio
async def test_finalize_dream_with_zero_retention_keeps_episodes_forever() -> None:
    history_rows = (_history_row(3),)
    document, run, history_digest = _finalize_fixture(history_rows)
    session = _Session(
        _Result(None),
        _Result(document),
        _Result(run),
        _Result(rows=history_rows),
    )

    await MemoryDocumentRepository(session, jobs=_Jobs()).finalize_dream(
        _scope(),
        job_id=JOB_ID,
        lease_token="lease",
        expected_history_digest=history_digest,
        expected_base_version=4,
        expected_base_digest=document.content_digest,
        expected_sections=DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
        content=EMPTY_MEMORY_DOCUMENT + "\n\n- 更新",
        now=NOW,
        episode_retention_days=0,
    )

    assert all("DELETE FROM memory_episodes" not in _compiled(statement) for statement in session.executed)


@pytest.mark.asyncio
@pytest.mark.parametrize("retention", [-1, 1, 29, 3651, True])
async def test_finalize_dream_rejects_out_of_contract_retention(retention) -> None:
    history_rows = (_history_row(3),)
    document, run, history_digest = _finalize_fixture(history_rows)
    session = _Session(
        _Result(None),
        _Result(document),
        _Result(run),
        _Result(rows=history_rows),
    )

    with pytest.raises(ValueError):
        await MemoryDocumentRepository(session, jobs=_Jobs()).finalize_dream(
            _scope(),
            job_id=JOB_ID,
            lease_token="lease",
            expected_history_digest=history_digest,
            expected_base_version=4,
            expected_base_digest=document.content_digest,
            expected_sections=DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
            content=EMPTY_MEMORY_DOCUMENT,
            now=NOW,
            episode_retention_days=retention,
        )


def test_default_episode_retention_matches_the_platform_contract() -> None:
    assert DEFAULT_EPISODE_RETENTION_DAYS == 365


class _PurgeSession:
    """Recording session for the many-statement retention purge path."""

    def __init__(self) -> None:
        self.executed: list[object] = []

    async def execute(self, statement, parameters=None):
        self.executed.append(statement)
        return _Result(rows=())

    async def scalar(self, statement, parameters=None):
        # ``to_regclass`` probes report the checkpoint tables as absent.
        return None


class _ExportStream:
    def __init__(self, rows) -> None:
        self._rows = list(rows)

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for row in self._rows:
            yield row

    async def close(self) -> None:
        return None


class _ExportSession:
    """Yields episode rows for their table and empty streams for the rest."""

    def __init__(self, episodes) -> None:
        self._episodes = list(episodes)

    async def stream_scalars(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        return _ExportStream(self._episodes if entity is MemoryEpisodeRow else ())

    async def stream(self, statement):
        return _ExportStream(())


class _ExportTransaction:
    def __init__(self) -> None:
        self.rolled_back = False

    @property
    def is_active(self) -> bool:
        return not self.rolled_back

    async def rollback(self) -> None:
        self.rolled_back = True


@pytest.mark.asyncio
async def test_retention_purge_deletes_episodes_for_project_and_owner_scopes() -> None:
    project_session = _PurgeSession()
    await purge_private_scope(
        project_session,
        project_id=_scope().project_id,
        owner_user_id=None,
    )
    project_wide = next(sql for sql in (_compiled(statement) for statement in project_session.executed) if "DELETE FROM memory_episodes" in sql)
    assert "project_id" in project_wide and "owner_user_id" not in project_wide

    owner_session = _PurgeSession()
    await purge_private_scope(
        owner_session,
        project_id=_scope().project_id,
        owner_user_id=_scope().owner_user_id,
    )
    owner_scoped = next(sql for sql in (_compiled(statement) for statement in owner_session.executed) if "DELETE FROM memory_episodes" in sql)
    assert "project_id" in owner_scoped and "owner_user_id" in owner_scoped


@pytest.mark.asyncio
async def test_retention_purge_requests_cancellation_for_active_memory_jobs() -> None:
    project_session = _PurgeSession()
    await purge_private_scope(
        project_session,
        project_id=_scope().project_id,
        owner_user_id=None,
    )
    project_job_update = next(_compiled(statement, literal=True) for statement in project_session.executed if "UPDATE jobs" in _compiled(statement))
    assert "memory_dream" in project_job_update
    assert "memory_seal" in project_job_update
    assert "owner_user_id" not in project_job_update
    assert "cancel_requested_at" in project_job_update
    assert "retention_scope_purged" in project_job_update

    owner_session = _PurgeSession()
    await purge_private_scope(
        owner_session,
        project_id=_scope().project_id,
        owner_user_id=_scope().owner_user_id,
    )
    owner_job_update = next(_compiled(statement, literal=True) for statement in owner_session.executed if "UPDATE jobs" in _compiled(statement))
    assert "memory_dream" in owner_job_update
    assert "memory_seal" in owner_job_update
    assert "owner_user_id" in owner_job_update


@pytest.mark.asyncio
async def test_privacy_export_streams_episode_rows_for_the_case_scope() -> None:
    occurred_at = NOW - timedelta(days=3)
    episode = MemoryEpisodeRow(
        id=uuid.UUID("33333333-3333-4333-8333-333333333333"),
        project_id=_scope().project_id,
        owner_user_id=_scope().owner_user_id,
        namespace="default",
        thread_id="episode-thread",
        origin="snip",
        tagged_text="- [durable] exported-fact",
        content_digest=memory_document_digest("- [durable] exported-fact"),
        occurred_at=occurred_at,
        consumed_dream_job_id=JOB_ID,
        created_at=NOW,
    )
    service = PrivacyCenterService(_ExportSession([episode]))
    project = SimpleNamespace(
        id=_scope().project_id,
        slug="former-project",
        display_name="Former project",
        icon="folder",
    )
    membership = SimpleNamespace(
        user_id=_scope().owner_user_id,
        status="left",
        ended_at=None,
        retention_until=None,
    )
    transaction = _ExportTransaction()

    lines = [
        json.loads(line)
        async for line in service._stream_export(
            transaction,
            project=project,
            membership=membership,
            generated_at=NOW,
        )
    ]

    assert transaction.rolled_back
    assert [line["record_type"] for line in lines] == ["manifest", "memory_episode"]
    assert lines[0]["format"] == "deer-flow-privacy-ndjson"
    assert lines[1]["data"] == {
        "id": "33333333-3333-4333-8333-333333333333",
        "namespace": "default",
        "thread_id": "episode-thread",
        "origin": "snip",
        "tagged_text": "- [durable] exported-fact",
        "occurred_at": occurred_at.isoformat(),
        "created_at": NOW.isoformat(),
    }


@pytest.mark.asyncio
async def test_reset_owner_deletes_and_counts_episodes_and_cancels_seal_jobs() -> None:
    owner = "22222222-2222-4222-8222-222222222222"
    session = _Session(
        _Result(rows=()),
        _Result(rows=()),
        _Result(rows=()),
        _Result(),
        _Result(),
        _Result(),
        _Result(),
        scalars=[7, 1, 2, 3, 4, 5],
    )

    counts = await MemoryDocumentRepository(session, jobs=_Jobs()).reset_owner(
        owner,
        now=NOW,
    )

    assert counts.history_entries == 7
    assert counts.documents == 1
    assert counts.versions == 2
    assert counts.dream_runs == 3
    assert counts.snapshots == 4
    assert counts.episodes == 5

    compiled = [_compiled(statement) for statement in session.executed]
    assert any("DELETE FROM memory_episodes" in sql for sql in compiled)
    active_jobs = next(statement for statement, sql in zip(session.executed, compiled, strict=True) if "jobs.job_type" in sql)
    active_jobs_sql = _compiled(active_jobs, literal=True)
    assert "memory_dream" in active_jobs_sql and "memory_seal" in active_jobs_sql
