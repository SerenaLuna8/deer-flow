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

from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from deerflow.runtime.events.store.db import DbRunEventStore
from deerflow.runtime.events.store.memory import MemoryRunEventStore
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


@pytest.fixture
def store():
    return MemoryRunEventStore()


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


# -- Basic write and query --


class TestPutAndSeq:
    @pytest.mark.anyio
    async def test_put_returns_dict_with_seq(self, store):
        record = await store.put(thread_id="t1", run_id="r1", event_type="human_message", category="message", content="hello")
        assert "seq" in record
        assert record["seq"] == 1
        assert record["thread_id"] == "t1"
        assert record["run_id"] == "r1"
        assert record["event_type"] == "human_message"
        assert record["category"] == "message"
        assert record["content"] == "hello"
        assert "created_at" in record

    @pytest.mark.anyio
    async def test_seq_strictly_increasing_same_thread(self, store):
        r1 = await store.put(thread_id="t1", run_id="r1", event_type="human_message", category="message")
        r2 = await store.put(thread_id="t1", run_id="r1", event_type="ai_message", category="message")
        r3 = await store.put(thread_id="t1", run_id="r1", event_type="llm_end", category="trace")
        assert r1["seq"] == 1
        assert r2["seq"] == 2
        assert r3["seq"] == 3

    @pytest.mark.anyio
    async def test_seq_independent_across_threads(self, store):
        r1 = await store.put(thread_id="t1", run_id="r1", event_type="human_message", category="message")
        r2 = await store.put(thread_id="t2", run_id="r2", event_type="human_message", category="message")
        assert r1["seq"] == 1
        assert r2["seq"] == 1

    @pytest.mark.anyio
    async def test_put_respects_provided_created_at(self, store):
        ts = "2024-06-01T12:00:00+00:00"
        record = await store.put(thread_id="t1", run_id="r1", event_type="human_message", category="message", created_at=ts)
        assert record["created_at"] == ts

    @pytest.mark.anyio
    async def test_put_metadata_preserved(self, store):
        meta = {"model": "gpt-4", "tokens": 100}
        record = await store.put(thread_id="t1", run_id="r1", event_type="llm_end", category="trace", metadata=meta)
        assert record["metadata"] == meta


# -- list_messages --


class TestListMessages:
    @pytest.mark.anyio
    async def test_only_returns_message_category(self, store):
        await store.put(thread_id="t1", run_id="r1", event_type="human_message", category="message")
        await store.put(thread_id="t1", run_id="r1", event_type="llm_end", category="trace")
        await store.put(thread_id="t1", run_id="r1", event_type="run_start", category="lifecycle")
        messages = await store.list_messages("t1")
        assert len(messages) == 1
        assert messages[0]["category"] == "message"

    @pytest.mark.anyio
    async def test_ascending_seq_order(self, store):
        await store.put(thread_id="t1", run_id="r1", event_type="human_message", category="message", content="first")
        await store.put(thread_id="t1", run_id="r1", event_type="ai_message", category="message", content="second")
        await store.put(thread_id="t1", run_id="r1", event_type="human_message", category="message", content="third")
        messages = await store.list_messages("t1")
        seqs = [m["seq"] for m in messages]
        assert seqs == sorted(seqs)

    @pytest.mark.anyio
    async def test_before_seq_pagination(self, store):
        for i in range(10):
            await store.put(thread_id="t1", run_id="r1", event_type="human_message", category="message", content=str(i))
        messages = await store.list_messages("t1", before_seq=6, limit=3)
        assert len(messages) == 3
        assert [m["seq"] for m in messages] == [3, 4, 5]

    @pytest.mark.anyio
    async def test_after_seq_pagination(self, store):
        for i in range(10):
            await store.put(thread_id="t1", run_id="r1", event_type="human_message", category="message", content=str(i))
        messages = await store.list_messages("t1", after_seq=7, limit=3)
        assert len(messages) == 3
        assert [m["seq"] for m in messages] == [8, 9, 10]

    @pytest.mark.anyio
    async def test_limit_restricts_count(self, store):
        for _ in range(20):
            await store.put(thread_id="t1", run_id="r1", event_type="human_message", category="message")
        messages = await store.list_messages("t1", limit=5)
        assert len(messages) == 5

    @pytest.mark.anyio
    async def test_cross_run_unified_ordering(self, store):
        await store.put(thread_id="t1", run_id="r1", event_type="human_message", category="message")
        await store.put(thread_id="t1", run_id="r1", event_type="ai_message", category="message")
        await store.put(thread_id="t1", run_id="r2", event_type="human_message", category="message")
        await store.put(thread_id="t1", run_id="r2", event_type="ai_message", category="message")
        messages = await store.list_messages("t1")
        assert [m["seq"] for m in messages] == [1, 2, 3, 4]
        assert messages[0]["run_id"] == "r1"
        assert messages[2]["run_id"] == "r2"

    @pytest.mark.anyio
    async def test_default_returns_latest(self, store):
        for _ in range(10):
            await store.put(thread_id="t1", run_id="r1", event_type="human_message", category="message")
        messages = await store.list_messages("t1", limit=3)
        assert [m["seq"] for m in messages] == [8, 9, 10]

    @pytest.mark.anyio
    async def test_pagination_with_interleaved_trace_events(self, store):
        # Messages and non-message events interleave, so message seqs are
        # non-contiguous (1, 3, 5, 7, 9). Seq-window pagination must still be
        # correct over the messages-only projection, including when the cursor
        # lands in a gap or exactly on a message seq (exclusive bound).
        for i in range(10):
            category = "message" if i % 2 == 0 else "trace"
            await store.put(thread_id="t1", run_id="r1", event_type="e", category=category, content=str(i))

        assert [m["seq"] for m in await store.list_messages("t1")] == [1, 3, 5, 7, 9]
        # before_seq in a gap: seq < 6 -> [1, 3, 5], last 2
        assert [m["seq"] for m in await store.list_messages("t1", before_seq=6, limit=2)] == [3, 5]
        # before_seq on a message seq is exclusive: seq < 5 -> [1, 3]
        assert [m["seq"] for m in await store.list_messages("t1", before_seq=5, limit=5)] == [1, 3]
        # after_seq in a gap: seq > 4 -> [5, 7, 9], first 2
        assert [m["seq"] for m in await store.list_messages("t1", after_seq=4, limit=2)] == [5, 7]
        # after_seq on a message seq is exclusive: seq > 5 -> [7, 9]
        assert [m["seq"] for m in await store.list_messages("t1", after_seq=5, limit=5)] == [7, 9]


# -- list_events --


class TestListEvents:
    @pytest.mark.anyio
    async def test_returns_all_categories_for_run(self, store):
        await store.put(thread_id="t1", run_id="r1", event_type="human_message", category="message")
        await store.put(thread_id="t1", run_id="r1", event_type="llm_end", category="trace")
        await store.put(thread_id="t1", run_id="r1", event_type="run_start", category="lifecycle")
        events = await store.list_events("t1", "r1")
        assert len(events) == 3

    @pytest.mark.anyio
    async def test_event_types_filter(self, store):
        await store.put(thread_id="t1", run_id="r1", event_type="llm_start", category="trace")
        await store.put(thread_id="t1", run_id="r1", event_type="llm_end", category="trace")
        await store.put(thread_id="t1", run_id="r1", event_type="tool_start", category="trace")
        events = await store.list_events("t1", "r1", event_types=["llm_end"])
        assert len(events) == 1
        assert events[0]["event_type"] == "llm_end"

    @pytest.mark.anyio
    async def test_only_returns_specified_run(self, store):
        await store.put(thread_id="t1", run_id="r1", event_type="llm_end", category="trace")
        await store.put(thread_id="t1", run_id="r2", event_type="llm_end", category="trace")
        events = await store.list_events("t1", "r1")
        assert len(events) == 1
        assert events[0]["run_id"] == "r1"


# -- list_messages_by_run --


class TestListMessagesByRun:
    @pytest.mark.anyio
    async def test_only_messages_for_specified_run(self, store):
        await store.put(thread_id="t1", run_id="r1", event_type="human_message", category="message")
        await store.put(thread_id="t1", run_id="r1", event_type="llm_end", category="trace")
        await store.put(thread_id="t1", run_id="r2", event_type="human_message", category="message")
        messages = await store.list_messages_by_run("t1", "r1")
        assert len(messages) == 1
        assert messages[0]["run_id"] == "r1"
        assert messages[0]["category"] == "message"


# -- count_messages --


class TestCountMessages:
    @pytest.mark.anyio
    async def test_counts_only_message_category(self, store):
        await store.put(thread_id="t1", run_id="r1", event_type="human_message", category="message")
        await store.put(thread_id="t1", run_id="r1", event_type="ai_message", category="message")
        await store.put(thread_id="t1", run_id="r1", event_type="llm_end", category="trace")
        assert await store.count_messages("t1") == 2


# -- put_batch --


class TestPutBatch:
    @pytest.mark.anyio
    async def test_batch_assigns_seq(self, store):
        events = [
            {"thread_id": "t1", "run_id": "r1", "event_type": "human_message", "category": "message", "content": "a"},
            {"thread_id": "t1", "run_id": "r1", "event_type": "ai_message", "category": "message", "content": "b"},
            {"thread_id": "t1", "run_id": "r1", "event_type": "llm_end", "category": "trace"},
        ]
        results = await store.put_batch(events)
        assert len(results) == 3
        assert all("seq" in r for r in results)

    @pytest.mark.anyio
    async def test_batch_seq_strictly_increasing(self, store):
        events = [
            {"thread_id": "t1", "run_id": "r1", "event_type": "human_message", "category": "message"},
            {"thread_id": "t1", "run_id": "r1", "event_type": "ai_message", "category": "message"},
        ]
        results = await store.put_batch(events)
        assert results[0]["seq"] == 1
        assert results[1]["seq"] == 2


# -- delete --


class TestDelete:
    @pytest.mark.anyio
    async def test_delete_by_thread(self, store):
        await store.put(thread_id="t1", run_id="r1", event_type="human_message", category="message")
        await store.put(thread_id="t1", run_id="r1", event_type="ai_message", category="message")
        await store.put(thread_id="t1", run_id="r2", event_type="llm_end", category="trace")
        count = await store.delete_by_thread("t1")
        assert count == 3
        assert await store.list_messages("t1") == []
        assert await store.count_messages("t1") == 0

    @pytest.mark.anyio
    async def test_delete_by_run(self, store):
        await store.put(thread_id="t1", run_id="r1", event_type="human_message", category="message")
        await store.put(thread_id="t1", run_id="r2", event_type="human_message", category="message")
        await store.put(thread_id="t1", run_id="r2", event_type="llm_end", category="trace")
        count = await store.delete_by_run("t1", "r2")
        assert count == 2
        messages = await store.list_messages("t1")
        assert len(messages) == 1
        assert messages[0]["run_id"] == "r1"

    @pytest.mark.anyio
    async def test_delete_nonexistent_thread_returns_zero(self, store):
        assert await store.delete_by_thread("nope") == 0

    @pytest.mark.anyio
    async def test_delete_nonexistent_run_returns_zero(self, store):
        await store.put(thread_id="t1", run_id="r1", event_type="human_message", category="message")
        assert await store.delete_by_run("t1", "nope") == 0

    @pytest.mark.anyio
    async def test_delete_nonexistent_thread_for_run_returns_zero(self, store):
        assert await store.delete_by_run("nope", "r1") == 0


# -- Edge cases --


class TestEdgeCases:
    @pytest.mark.anyio
    async def test_empty_thread_list_messages(self, store):
        assert await store.list_messages("empty") == []

    @pytest.mark.anyio
    async def test_empty_run_list_events(self, store):
        assert await store.list_events("empty", "r1") == []

    @pytest.mark.anyio
    async def test_empty_thread_count_messages(self, store):
        assert await store.count_messages("empty") == 0


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


# -- Factory tests --


class TestMakeRunEventStore:
    """Tests for the make_run_event_store factory function."""

    @pytest.mark.anyio
    async def test_memory_backend_default(self):
        from deerflow.runtime.events.store import make_run_event_store

        store = make_run_event_store(None)
        assert type(store).__name__ == "MemoryRunEventStore"

    @pytest.mark.anyio
    async def test_memory_backend_explicit(self):
        from unittest.mock import MagicMock

        from deerflow.runtime.events.store import make_run_event_store

        config = MagicMock()
        config.backend = "memory"
        store = make_run_event_store(config)
        assert type(store).__name__ == "MemoryRunEventStore"

    @pytest.mark.anyio
    async def test_db_backend_with_engine(self, _postgres_database):
        from unittest.mock import MagicMock

        from deerflow.persistence.engine import close_engine
        from deerflow.runtime.events.store import make_run_event_store

        await _init_db()

        config = MagicMock()
        config.backend = "db"
        config.max_trace_content = 10240
        store = make_run_event_store(config)
        assert type(store).__name__ == "DbRunEventStore"
        await close_engine()

    @pytest.mark.anyio
    async def test_jsonl_backend(self):
        from unittest.mock import MagicMock

        from deerflow.runtime.events.store import make_run_event_store

        config = MagicMock()
        config.backend = "jsonl"
        store = make_run_event_store(config)
        assert type(store).__name__ == "JsonlRunEventStore"

    @pytest.mark.anyio
    async def test_unknown_backend_raises(self):
        from unittest.mock import MagicMock

        from deerflow.runtime.events.store import make_run_event_store

        config = MagicMock()
        config.backend = "redis"
        with pytest.raises(ValueError, match="Unknown"):
            make_run_event_store(config)


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
