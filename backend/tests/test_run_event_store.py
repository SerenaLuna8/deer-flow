"""Tests for RunEventStore contract across all backends.

Uses a helper to create the store for each backend type.
Memory tests run directly; DB and JSONL tests create stores inside each test.
"""

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from support.m4_private_threads import seed_m4_thread_database
from support.memory_event_store import MemoryRunEventStore

from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from deerflow.runtime.events.store.db import DbRunEventStore
from deerflow.runtime.private_scope import PrivateResourceScope

_DATABASE_URL: str | None = None
_TEST_SCOPE: PrivateResourceScope | None = None


class _ScopedEventStore:
    """Test-only adapter binding DB event calls to seeded private authority."""

    _SCOPED_METHODS = frozenset(
        {
            "count_messages",
            "delete_by_run",
            "delete_by_thread",
            "list_events",
            "list_messages",
            "list_messages_by_run",
            "put",
            "put_batch",
        }
    )

    def __init__(self, store: DbRunEventStore, scope: PrivateResourceScope) -> None:
        self._store = store
        self._scope = scope

    def __getattr__(self, name):
        target = getattr(self._store, name)
        if name not in self._SCOPED_METHODS:
            return target

        async def scoped(*args, **kwargs):
            kwargs["scope"] = self._scope
            return await target(*args, **kwargs)

        return scoped


async def _seed_parent_runs(database_url: str, thread_id: str, run_ids: tuple[str, ...]):
    seed = await seed_m4_thread_database(database_url)
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
        repository = PrivateRunRepository(session)
        for run_id in run_ids:
            await repository.create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                request=PrivateRunCreate(run_id=run_id),
            )
    return seed


@pytest_asyncio.fixture
async def _postgres_database(migrated_postgres_database_url):
    global _DATABASE_URL, _TEST_SCOPE
    _DATABASE_URL = migrated_postgres_database_url
    seed = await _seed_parent_runs(migrated_postgres_database_url, "t1", ("r1", "r2"))
    _TEST_SCOPE = seed.owner_a_scope
    try:
        yield
    finally:
        from deerflow.persistence.engine import close_engine

        await close_engine()
        await seed.engine.dispose()
        _DATABASE_URL = None
        _TEST_SCOPE = None


async def _init_db() -> None:
    from deerflow.config.database_config import DatabaseConfig
    from deerflow.persistence.engine import init_engine

    assert _DATABASE_URL is not None
    await init_engine(DatabaseConfig(url=_DATABASE_URL))


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_concurrent_db_writes_assign_unique_contiguous_sequence(
    migrated_postgres_database_url: str,
) -> None:
    seed = await _seed_parent_runs(
        migrated_postgres_database_url,
        "concurrent-thread",
        ("run-1",),
    )
    first_engine = create_async_engine(migrated_postgres_database_url, poolclass=NullPool)
    second_engine = create_async_engine(migrated_postgres_database_url, poolclass=NullPool)
    first = _ScopedEventStore(
        DbRunEventStore(async_sessionmaker(first_engine, expire_on_commit=False)),
        seed.owner_a_scope,
    )
    second = _ScopedEventStore(
        DbRunEventStore(async_sessionmaker(second_engine, expire_on_commit=False)),
        seed.owner_a_scope,
    )
    try:
        writes = [
            (first if index % 2 == 0 else second).put(
                thread_id="concurrent-thread",
                run_id="run-1",
                event_type="human_message",
                category="message",
                content=str(index),
            )
            for index in range(20)
        ]
        records = await asyncio.gather(*writes)
        assert sorted(record["seq"] for record in records) == list(range(1, 21))

        persisted = await first.list_events("concurrent-thread", "run-1", limit=50, user_id=None)
        assert [record["seq"] for record in persisted] == list(range(1, 21))
    finally:
        await first_engine.dispose()
        await second_engine.dispose()
        await seed.engine.dispose()


# -- Test-double contract smoke tests --


@pytest.mark.anyio
async def test_memory_event_store_contract_smoke() -> None:
    store = MemoryRunEventStore()
    records = await store.put_batch(
        [
            {"thread_id": "t1", "run_id": "r1", "event_type": "human_message", "category": "message", "content": "first"},
            {"thread_id": "t1", "run_id": "r1", "event_type": "trace", "category": "trace", "metadata": {"task_id": "task-a"}},
            {"thread_id": "t1", "run_id": "r2", "event_type": "ai_message", "category": "message", "content": "second"},
        ]
    )

    assert [record["seq"] for record in records] == [1, 2, 3]
    assert [record["seq"] for record in await store.list_messages("t1")] == [1, 3]
    assert [record["seq"] for record in await store.list_messages_by_run("t1", "r2")] == [3]
    assert [record["seq"] for record in await store.list_events("t1", "r1", task_id="task-a")] == [2]
    assert await store.count_messages("t1") == 2


@pytest.mark.anyio
async def test_memory_event_store_delete_contract_smoke() -> None:
    store = MemoryRunEventStore()
    await store.put(thread_id="t1", run_id="r1", event_type="human_message", category="message")
    await store.put(thread_id="t1", run_id="r2", event_type="ai_message", category="message")

    assert await store.delete_by_run("t1", "r2") == 1
    assert [record["run_id"] for record in await store.list_messages("t1")] == ["r1"]
    assert await store.delete_by_thread("t1") == 1
    assert await store.list_messages("t1") == []


# -- DB-specific tests --


class TestDbRunEventStore:
    """Tests for DbRunEventStore with temp SQLite."""

    @pytest.mark.anyio
    async def test_postgres_sequence_uses_advisory_lock_and_row_lock(self):
        import uuid

        from sqlalchemy.dialects import postgresql

        from deerflow.persistence.models.run_event import ThreadEventSequenceRow
        from deerflow.runtime.events.store.db import DbRunEventStore

        sequence = ThreadEventSequenceRow(
            project_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            owner_user_id="00000000-0000-0000-0000-000000000002",
            thread_id="thread-1",
            high_watermark=41,
        )

        class FakeResult:
            def scalar_one_or_none(self):
                return sequence

        class FakeSession:
            def __init__(self):
                self.dialect = postgresql.dialect()
                self.execute_calls = []

            def get_bind(self):
                return self

            async def execute(self, stmt, params=None):
                self.execute_calls.append((stmt, params))
                return FakeResult()

        session = FakeSession()
        scope = PrivateResourceScope(
            project_id=str(sequence.project_id),
            owner_user_id=sequence.owner_user_id,
            membership_version=1,
        )

        locked = await DbRunEventStore._lock_event_sequence(
            session,  # type: ignore[arg-type]
            scope=scope,
            thread_id="thread-1",
        )

        assert locked is sequence
        assert len(session.execute_calls) == 2
        assert session.execute_calls[0][1] == {"thread_id": "thread-1"}
        assert "pg_advisory_xact_lock" in str(session.execute_calls[0][0])
        compiled = str(
            session.execute_calls[1][0].compile(dialect=postgresql.dialect()),
        )
        assert "FOR UPDATE" in compiled

    @pytest.mark.anyio
    async def test_basic_crud(self, _postgres_database):
        from deerflow.persistence.engine import close_engine, get_session_factory
        from deerflow.runtime.events.store.db import DbRunEventStore

        await _init_db()
        assert _TEST_SCOPE is not None
        s = _ScopedEventStore(DbRunEventStore(get_session_factory()), _TEST_SCOPE)

        r = await s.put(thread_id="t1", run_id="r1", event_type="human_message", category="message", content="hi")
        assert r["seq"] == 1
        r2 = await s.put(thread_id="t1", run_id="r1", event_type="ai_message", category="message", content="hello")
        assert r2["seq"] == 2

        messages = await s.list_messages("t1")
        assert len(messages) == 2

        count = await s.count_messages("t1")
        assert count == 2

        await close_engine()

    @pytest.mark.anyio
    async def test_trace_content_truncation(self, _postgres_database):
        from deerflow.persistence.engine import close_engine, get_session_factory
        from deerflow.runtime.events.store.db import DbRunEventStore

        await _init_db()
        assert _TEST_SCOPE is not None
        s = _ScopedEventStore(
            DbRunEventStore(get_session_factory(), max_trace_content=100),
            _TEST_SCOPE,
        )

        long = "x" * 200
        r = await s.put(thread_id="t1", run_id="r1", event_type="llm_end", category="trace", content=long)
        assert len(r["content"]) == 100
        assert r["metadata"].get("content_truncated") is True

        # message content NOT truncated
        m = await s.put(thread_id="t1", run_id="r1", event_type="ai_message", category="message", content=long)
        assert len(m["content"]) == 200

        await close_engine()

    @pytest.mark.anyio
    async def test_structured_content_round_trips(self, _postgres_database):
        from deerflow.persistence.engine import close_engine, get_session_factory
        from deerflow.runtime.events.store.db import DbRunEventStore

        await _init_db()
        assert _TEST_SCOPE is not None
        s = _ScopedEventStore(DbRunEventStore(get_session_factory()), _TEST_SCOPE)

        content = [{"type": "text", "text": "hello"}, {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}}]
        record = await s.put(thread_id="t1", run_id="r1", event_type="ai_message", category="message", content=content)

        assert record["content"] == content
        assert record["metadata"]["content_is_json"] is True
        assert "content_is_dict" not in record["metadata"]

        messages = await s.list_messages("t1")
        assert messages[0]["content"] == content
        assert messages[0]["metadata"]["content_is_json"] is True

        await close_engine()

    @pytest.mark.anyio
    async def test_pagination(self, _postgres_database):
        from deerflow.persistence.engine import close_engine, get_session_factory
        from deerflow.runtime.events.store.db import DbRunEventStore

        await _init_db()
        assert _TEST_SCOPE is not None
        s = _ScopedEventStore(DbRunEventStore(get_session_factory()), _TEST_SCOPE)

        for i in range(10):
            await s.put(thread_id="t1", run_id="r1", event_type="human_message", category="message", content=str(i))

        # before_seq
        msgs = await s.list_messages("t1", before_seq=6, limit=3)
        assert [m["seq"] for m in msgs] == [3, 4, 5]

        # after_seq
        msgs = await s.list_messages("t1", after_seq=7, limit=3)
        assert [m["seq"] for m in msgs] == [8, 9, 10]

        # default (latest)
        msgs = await s.list_messages("t1", limit=3)
        assert [m["seq"] for m in msgs] == [8, 9, 10]

        await close_engine()

    @pytest.mark.anyio
    async def test_delete(self, _postgres_database):
        from deerflow.persistence.engine import close_engine, get_session_factory
        from deerflow.runtime.events.store.db import DbRunEventStore

        await _init_db()
        assert _TEST_SCOPE is not None
        s = _ScopedEventStore(DbRunEventStore(get_session_factory()), _TEST_SCOPE)

        await s.put(thread_id="t1", run_id="r1", event_type="human_message", category="message")
        await s.put(thread_id="t1", run_id="r2", event_type="ai_message", category="message")
        c = await s.delete_by_run("t1", "r2")
        assert c == 1
        assert await s.count_messages("t1") == 1

        c = await s.delete_by_thread("t1")
        assert c == 1
        assert await s.count_messages("t1") == 0

        await close_engine()

    @pytest.mark.anyio
    async def test_put_batch_seq_continuity(self, _postgres_database):
        """Batch write produces continuous seq values with no gaps."""
        from deerflow.persistence.engine import close_engine, get_session_factory
        from deerflow.runtime.events.store.db import DbRunEventStore

        await _init_db()
        assert _TEST_SCOPE is not None
        s = _ScopedEventStore(DbRunEventStore(get_session_factory()), _TEST_SCOPE)

        events = [{"thread_id": "t1", "run_id": "r1", "event_type": "trace", "category": "trace"} for _ in range(50)]
        results = await s.put_batch(events)
        seqs = [r["seq"] for r in results]
        assert seqs == list(range(1, 51))
        await close_engine()

    @pytest.mark.anyio
    async def test_put_batch_accepts_structured_content(self, _postgres_database):
        from deerflow.persistence.engine import close_engine, get_session_factory
        from deerflow.runtime.events.store.db import DbRunEventStore

        await _init_db()
        assert _TEST_SCOPE is not None
        s = _ScopedEventStore(DbRunEventStore(get_session_factory()), _TEST_SCOPE)

        content = [{"messages": [{"type": "ai", "content": ""}]}]
        results = await s.put_batch(
            [
                {
                    "thread_id": "t1",
                    "run_id": "r1",
                    "event_type": "run.end",
                    "category": "outputs",
                    "content": content,
                }
            ]
        )

        assert results[0]["content"] == content
        assert results[0]["metadata"]["content_is_json"] is True

        events = await s.list_events("t1", "r1")
        assert events[0]["content"] == content
        assert events[0]["metadata"]["content_is_json"] is True

        await close_engine()

    @pytest.mark.anyio
    async def test_dict_content_keeps_legacy_metadata_flag(self, _postgres_database):
        from deerflow.persistence.engine import close_engine, get_session_factory
        from deerflow.runtime.events.store.db import DbRunEventStore

        await _init_db()
        assert _TEST_SCOPE is not None
        s = _ScopedEventStore(DbRunEventStore(get_session_factory()), _TEST_SCOPE)

        content = {"status": "success"}
        record = await s.put(thread_id="t1", run_id="r1", event_type="run.end", category="outputs", content=content)

        assert record["content"] == content
        assert record["metadata"]["content_is_json"] is True
        assert record["metadata"]["content_is_dict"] is True

        await close_engine()


# -- Final export contract --


def test_run_event_store_factory_is_removed() -> None:
    import inspect

    import deerflow.runtime.events.store as store_module

    assert not hasattr(store_module, "make_run_event_store")
    parameter = inspect.signature(store_module.DbRunEventStore).parameters["session_factory"]
    assert parameter.default is inspect.Parameter.empty


# -- JSONL-specific tests --


class TestJsonlRunEventStore:
    @pytest.mark.anyio
    async def test_basic_crud(self, tmp_path):
        from deerflow.runtime.events.store.jsonl import JsonlRunEventStore

        s = JsonlRunEventStore(base_dir=tmp_path / "jsonl")
        r = await s.put(thread_id="t1", run_id="r1", event_type="human_message", category="message", content="hi")
        assert r["seq"] == 1
        messages = await s.list_messages("t1")
        assert len(messages) == 1

    @pytest.mark.anyio
    async def test_file_at_correct_path(self, tmp_path):
        from deerflow.runtime.events.store.jsonl import JsonlRunEventStore

        s = JsonlRunEventStore(base_dir=tmp_path / "jsonl")
        await s.put(thread_id="t1", run_id="r1", event_type="human_message", category="message")
        assert (tmp_path / "jsonl" / "threads" / "t1" / "runs" / "r1.jsonl").exists()

    @pytest.mark.anyio
    async def test_cross_run_messages(self, tmp_path):
        from deerflow.runtime.events.store.jsonl import JsonlRunEventStore

        s = JsonlRunEventStore(base_dir=tmp_path / "jsonl")
        await s.put(thread_id="t1", run_id="r1", event_type="human_message", category="message")
        await s.put(thread_id="t1", run_id="r2", event_type="human_message", category="message")
        messages = await s.list_messages("t1")
        assert len(messages) == 2
        assert [m["seq"] for m in messages] == [1, 2]

    @pytest.mark.anyio
    async def test_delete_by_run(self, tmp_path):
        from deerflow.runtime.events.store.jsonl import JsonlRunEventStore

        s = JsonlRunEventStore(base_dir=tmp_path / "jsonl")
        await s.put(thread_id="t1", run_id="r1", event_type="human_message", category="message")
        await s.put(thread_id="t1", run_id="r2", event_type="human_message", category="message")
        c = await s.delete_by_run("t1", "r2")
        assert c == 1
        assert not (tmp_path / "jsonl" / "threads" / "t1" / "runs" / "r2.jsonl").exists()
        assert await s.count_messages("t1") == 1
