"""Tests for FeedbackRepository and follow-up association.

Uses isolated PostgreSQL databases for ORM tests.
"""

import pytest
import pytest_asyncio
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from deerflow.persistence.feedback import FeedbackRepository
from deerflow.persistence.run import RunRepository
from deerflow.persistence.thread_meta import ThreadMetaRepository

_SEED: M4ThreadSeed | None = None


@pytest_asyncio.fixture()
async def _postgres_database(migrated_postgres_database_url):
    global _SEED
    _SEED = await seed_m4_thread_database(migrated_postgres_database_url)
    scope = _SEED.owner_a_scope
    thread_repo = ThreadMetaRepository(_SEED.factory)
    run_repo = RunRepository(_SEED.factory)
    for thread_id in ("t1", "t2"):
        await thread_repo.create(
            thread_id,
            scope=scope,
            agent_asset_id=_SEED.project_agent_id,
            agent_scope="project",
        )
    for run_id, thread_id in (("r1", "t1"), ("r2", "t1"), ("r3", "t2")):
        await run_repo.put(run_id, thread_id=thread_id, scope=scope)
    try:
        yield
    finally:
        await _SEED.engine.dispose()
        _SEED = None


async def _make_feedback_repo(_tmp_path):
    assert _SEED is not None
    return FeedbackRepository(_SEED.factory)


async def _cleanup():
    return None


def _scope():
    assert _SEED is not None
    return _SEED.owner_a_scope


# -- FeedbackRepository --


@pytest.mark.postgres
@pytest.mark.usefixtures("_postgres_database")
class TestFeedbackRepository:
    @pytest.mark.anyio
    async def test_create_positive(self, tmp_path):
        repo = await _make_feedback_repo(tmp_path)
        record = await repo.create(run_id="r1", thread_id="t1", rating=1, scope=_scope())
        assert record["feedback_id"]
        assert record["rating"] == 1
        assert record["run_id"] == "r1"
        assert record["thread_id"] == "t1"
        assert "created_at" in record
        await _cleanup()

    @pytest.mark.anyio
    async def test_create_negative_with_comment(self, tmp_path):
        repo = await _make_feedback_repo(tmp_path)
        record = await repo.create(
            run_id="r1",
            thread_id="t1",
            rating=-1,
            scope=_scope(),
            comment="Response was inaccurate",
        )
        assert record["rating"] == -1
        assert record["comment"] == "Response was inaccurate"
        await _cleanup()

    @pytest.mark.anyio
    async def test_create_with_message_id(self, tmp_path):
        repo = await _make_feedback_repo(tmp_path)
        record = await repo.create(run_id="r1", thread_id="t1", rating=1, message_id="msg-42", scope=_scope())
        assert record["message_id"] == "msg-42"
        await _cleanup()

    @pytest.mark.anyio
    async def test_create_with_owner(self, tmp_path):
        repo = await _make_feedback_repo(tmp_path)
        record = await repo.create(run_id="r1", thread_id="t1", rating=1, user_id="ignored", scope=_scope())
        assert record["owner_user_id"] == _scope().owner_user_id
        await _cleanup()

    @pytest.mark.anyio
    async def test_create_invalid_rating_zero(self, tmp_path):
        repo = await _make_feedback_repo(tmp_path)
        with pytest.raises(ValueError):
            await repo.create(run_id="r1", thread_id="t1", rating=0, scope=_scope())
        await _cleanup()

    @pytest.mark.anyio
    async def test_create_invalid_rating_five(self, tmp_path):
        repo = await _make_feedback_repo(tmp_path)
        with pytest.raises(ValueError):
            await repo.create(run_id="r1", thread_id="t1", rating=5, scope=_scope())
        await _cleanup()

    @pytest.mark.anyio
    async def test_get(self, tmp_path):
        repo = await _make_feedback_repo(tmp_path)
        created = await repo.create(run_id="r1", thread_id="t1", rating=1, scope=_scope())
        fetched = await repo.get(created["feedback_id"], scope=_scope())
        assert fetched is not None
        assert fetched["feedback_id"] == created["feedback_id"]
        assert fetched["rating"] == 1
        await _cleanup()

    @pytest.mark.anyio
    async def test_get_nonexistent(self, tmp_path):
        repo = await _make_feedback_repo(tmp_path)
        assert await repo.get("nonexistent", scope=_scope()) is None
        await _cleanup()

    @pytest.mark.anyio
    async def test_list_by_run(self, tmp_path):
        repo = await _make_feedback_repo(tmp_path)
        await repo.create(run_id="r1", thread_id="t1", rating=1, scope=_scope())
        await repo.create(run_id="r2", thread_id="t1", rating=-1, scope=_scope())
        results = await repo.list_by_run("t1", "r1", scope=_scope())
        assert len(results) == 1
        assert all(r["run_id"] == "r1" for r in results)
        await _cleanup()

    @pytest.mark.anyio
    async def test_list_by_thread(self, tmp_path):
        repo = await _make_feedback_repo(tmp_path)
        await repo.create(run_id="r1", thread_id="t1", rating=1, scope=_scope())
        await repo.create(run_id="r2", thread_id="t1", rating=-1, scope=_scope())
        await repo.create(run_id="r3", thread_id="t2", rating=1, scope=_scope())
        results = await repo.list_by_thread("t1", scope=_scope())
        assert len(results) == 2
        assert all(r["thread_id"] == "t1" for r in results)
        await _cleanup()

    @pytest.mark.anyio
    async def test_delete(self, tmp_path):
        repo = await _make_feedback_repo(tmp_path)
        created = await repo.create(run_id="r1", thread_id="t1", rating=1, scope=_scope())
        deleted = await repo.delete(created["feedback_id"], scope=_scope())
        assert deleted is True
        assert await repo.get(created["feedback_id"], scope=_scope()) is None
        await _cleanup()

    @pytest.mark.anyio
    async def test_delete_nonexistent(self, tmp_path):
        repo = await _make_feedback_repo(tmp_path)
        deleted = await repo.delete("nonexistent", scope=_scope())
        assert deleted is False
        await _cleanup()

    @pytest.mark.anyio
    async def test_aggregate_by_run(self, tmp_path):
        repo = await _make_feedback_repo(tmp_path)
        await repo.create(run_id="r1", thread_id="t1", rating=-1, scope=_scope())
        stats = await repo.aggregate_by_run("t1", "r1", scope=_scope())
        assert stats["total"] == 1
        assert stats["positive"] == 0
        assert stats["negative"] == 1
        assert stats["run_id"] == "r1"
        await _cleanup()

    @pytest.mark.anyio
    async def test_aggregate_empty(self, tmp_path):
        repo = await _make_feedback_repo(tmp_path)
        stats = await repo.aggregate_by_run("t1", "r1", scope=_scope())
        assert stats["total"] == 0
        assert stats["positive"] == 0
        assert stats["negative"] == 0
        await _cleanup()

    @pytest.mark.anyio
    async def test_upsert_creates_new(self, tmp_path):
        repo = await _make_feedback_repo(tmp_path)
        record = await repo.upsert(run_id="r1", thread_id="t1", rating=1, user_id="ignored", scope=_scope())
        assert record["rating"] == 1
        assert record["feedback_id"]
        assert record["owner_user_id"] == _scope().owner_user_id
        await _cleanup()

    @pytest.mark.anyio
    async def test_upsert_updates_existing(self, tmp_path):
        repo = await _make_feedback_repo(tmp_path)
        first = await repo.upsert(run_id="r1", thread_id="t1", rating=1, scope=_scope())
        second = await repo.upsert(run_id="r1", thread_id="t1", rating=-1, comment="changed my mind", scope=_scope())
        assert second["feedback_id"] == first["feedback_id"]
        assert second["rating"] == -1
        assert second["comment"] == "changed my mind"
        await _cleanup()

    @pytest.mark.anyio
    async def test_upsert_ignores_legacy_user_id_and_updates_owner_feedback(self, tmp_path):
        repo = await _make_feedback_repo(tmp_path)
        r1 = await repo.upsert(run_id="r1", thread_id="t1", rating=1, user_id="ignored-a", scope=_scope())
        r2 = await repo.upsert(run_id="r1", thread_id="t1", rating=-1, user_id="ignored-b", scope=_scope())
        assert r1["feedback_id"] == r2["feedback_id"]
        assert r1["rating"] == 1
        assert r2["rating"] == -1
        await _cleanup()

    @pytest.mark.anyio
    async def test_upsert_invalid_rating(self, tmp_path):
        repo = await _make_feedback_repo(tmp_path)
        with pytest.raises(ValueError):
            await repo.upsert(run_id="r1", thread_id="t1", rating=0, scope=_scope())
        await _cleanup()

    @pytest.mark.anyio
    async def test_delete_by_run(self, tmp_path):
        repo = await _make_feedback_repo(tmp_path)
        await repo.upsert(run_id="r1", thread_id="t1", rating=1, scope=_scope())
        deleted = await repo.delete_by_run(thread_id="t1", run_id="r1", scope=_scope())
        assert deleted is True
        results = await repo.list_by_run("t1", "r1", scope=_scope())
        assert len(results) == 0
        await _cleanup()

    @pytest.mark.anyio
    async def test_delete_by_run_nonexistent(self, tmp_path):
        repo = await _make_feedback_repo(tmp_path)
        deleted = await repo.delete_by_run(thread_id="t1", run_id="r1", scope=_scope())
        assert deleted is False
        await _cleanup()

    @pytest.mark.anyio
    async def test_list_by_thread_grouped(self, tmp_path):
        repo = await _make_feedback_repo(tmp_path)
        await repo.upsert(run_id="r1", thread_id="t1", rating=1, scope=_scope())
        await repo.upsert(run_id="r2", thread_id="t1", rating=-1, scope=_scope())
        await repo.upsert(run_id="r3", thread_id="t2", rating=1, scope=_scope())
        grouped = await repo.list_by_thread_grouped("t1", scope=_scope())
        assert "r1" in grouped
        assert "r2" in grouped
        assert "r3" not in grouped
        assert grouped["r1"]["rating"] == 1
        assert grouped["r2"]["rating"] == -1
        await _cleanup()

    @pytest.mark.anyio
    async def test_list_by_thread_grouped_empty(self, tmp_path):
        repo = await _make_feedback_repo(tmp_path)
        grouped = await repo.list_by_thread_grouped("t1", scope=_scope())
        assert grouped == {}
        await _cleanup()


# -- Follow-up association --


class TestFollowUpAssociation:
    @pytest.mark.anyio
    async def test_run_records_follow_up_via_memory_store(self):
        """MemoryRunStore stores follow_up_to_run_id in kwargs."""
        from support.memory_run_store import MemoryRunStore

        store = MemoryRunStore()
        await store.put("r1", thread_id="t1", status="success")
        # MemoryRunStore doesn't have follow_up_to_run_id as a top-level param,
        # but it can be passed via metadata
        await store.put("r2", thread_id="t1", metadata={"follow_up_to_run_id": "r1"})
        run = await store.get("r2")
        assert run["metadata"]["follow_up_to_run_id"] == "r1"

    @pytest.mark.anyio
    async def test_human_message_has_follow_up_metadata(self):
        """human_message event metadata includes follow_up_to_run_id."""
        from support.memory_event_store import MemoryRunEventStore

        event_store = MemoryRunEventStore()
        await event_store.put(
            thread_id="t1",
            run_id="r2",
            event_type="human_message",
            category="message",
            content="Tell me more about that",
            metadata={"follow_up_to_run_id": "r1"},
        )
        messages = await event_store.list_messages("t1")
        assert messages[0]["metadata"]["follow_up_to_run_id"] == "r1"

    @pytest.mark.anyio
    async def test_follow_up_auto_detection_logic(self):
        """Simulate the auto-detection: latest successful run becomes follow_up_to."""
        from support.memory_run_store import MemoryRunStore

        store = MemoryRunStore()
        await store.put("r1", thread_id="t1", status="success")
        await store.put("r2", thread_id="t1", status="error")

        # Auto-detect: list_by_thread returns newest first
        recent = await store.list_by_thread("t1", limit=1)
        follow_up = None
        if recent and recent[0].get("status") == "success":
            follow_up = recent[0]["run_id"]
        # r2 (error) is newest, so no follow_up detected
        assert follow_up is None

        # Now add a successful run
        await store.put("r3", thread_id="t1", status="success")
        recent = await store.list_by_thread("t1", limit=1)
        follow_up = None
        if recent and recent[0].get("status") == "success":
            follow_up = recent[0]["run_id"]
        assert follow_up == "r3"
