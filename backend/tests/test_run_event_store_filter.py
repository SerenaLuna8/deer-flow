"""Tests for list_events task_id filtering + after_seq cursor pagination (#3779).

These power the subtask card's fetch-on-expand backfill, which must page through
ONE subagent task's persisted steps without the run-wide 500-event cap dropping
the tail (or an entire later subtask). The filter has to run in the store (before
the limit) so pagination stays correct.
"""

import pytest

from deerflow.runtime.events.store.memory import MemoryRunEventStore


async def _seed_two_tasks(store, *, scope=None):
    """Seed run r1 with task A (start + 3 steps) and task B (start + 2 steps)."""
    scope_kwargs = {} if scope is None else {"scope": scope}
    await store.put(thread_id="t1", run_id="r1", event_type="subagent.start", category="subagent", content={"task_id": "A"}, metadata={"task_id": "A"}, **scope_kwargs)
    await store.put(thread_id="t1", run_id="r1", event_type="subagent.start", category="subagent", content={"task_id": "B"}, metadata={"task_id": "B"}, **scope_kwargs)
    for i in range(3):
        await store.put(thread_id="t1", run_id="r1", event_type="subagent.step", category="subagent", content={"task_id": "A", "message_index": i}, metadata={"task_id": "A", "message_index": i}, **scope_kwargs)
    for i in range(2):
        await store.put(thread_id="t1", run_id="r1", event_type="subagent.step", category="subagent", content={"task_id": "B", "message_index": i}, metadata={"task_id": "B", "message_index": i}, **scope_kwargs)


def _task_ids(events):
    return [e["metadata"].get("task_id") for e in events]


async def _check_task_id_filter(store, *, scope=None):
    scope_kwargs = {} if scope is None else {"scope": scope}
    await _seed_two_tasks(store, scope=scope)
    a_events = await store.list_events("t1", "r1", task_id="A", **scope_kwargs)
    assert _task_ids(a_events) == ["A", "A", "A", "A"]  # start + 3 steps
    b_events = await store.list_events("t1", "r1", task_id="B", **scope_kwargs)
    assert _task_ids(b_events) == ["B", "B", "B"]  # start + 2 steps


async def _check_task_id_with_event_types(store, *, scope=None):
    scope_kwargs = {} if scope is None else {"scope": scope}
    await _seed_two_tasks(store, scope=scope)
    a_steps = await store.list_events("t1", "r1", task_id="A", event_types=["subagent.step"], **scope_kwargs)
    assert [e["event_type"] for e in a_steps] == ["subagent.step"] * 3
    assert _task_ids(a_steps) == ["A", "A", "A"]


async def _check_after_seq_cursor(store, *, scope=None):
    scope_kwargs = {} if scope is None else {"scope": scope}
    await _seed_two_tasks(store, scope=scope)
    everything = await store.list_events("t1", "r1", **scope_kwargs)
    cursor = everything[2]["seq"]
    after = await store.list_events("t1", "r1", after_seq=cursor, **scope_kwargs)
    assert all(e["seq"] > cursor for e in after)
    assert len(after) == len(everything) - 3


async def _check_task_id_after_seq_paginate(store, *, scope=None):
    """task_id + after_seq + small limit pages through ONE task with no gaps/dupes."""
    scope_kwargs = {} if scope is None else {"scope": scope}
    await _seed_two_tasks(store, scope=scope)
    collected = []
    after_seq = None
    for _ in range(10):  # safety bound
        page = await store.list_events("t1", "r1", task_id="A", event_types=["subagent.step"], limit=2, after_seq=after_seq, **scope_kwargs)
        collected.extend(page)
        if len(page) < 2:
            break
        after_seq = page[-1]["seq"]
    assert [e["content"]["message_index"] for e in collected] == [0, 1, 2]


async def _check_no_task_id_returns_all(store, *, scope=None):
    scope_kwargs = {} if scope is None else {"scope": scope}
    await _seed_two_tasks(store, scope=scope)
    everything = await store.list_events("t1", "r1", **scope_kwargs)
    assert len(everything) == 7  # 2 starts + 5 steps


# -- Memory backend --


@pytest.mark.anyio
async def test_memory_task_id_filter():
    await _check_task_id_filter(MemoryRunEventStore())


@pytest.mark.anyio
async def test_memory_task_id_with_event_types():
    await _check_task_id_with_event_types(MemoryRunEventStore())


@pytest.mark.anyio
async def test_memory_after_seq_cursor():
    await _check_after_seq_cursor(MemoryRunEventStore())


@pytest.mark.anyio
async def test_memory_task_id_after_seq_paginate():
    await _check_task_id_after_seq_paginate(MemoryRunEventStore())


@pytest.mark.anyio
async def test_memory_no_task_id_returns_all():
    await _check_no_task_id_returns_all(MemoryRunEventStore())


# -- PostgreSQL DB backend: exercises the JSON-field filter on the runtime dialect --


async def _make_db_store(database_url):
    from support.m4_private_threads import seed_m4_thread_database

    from deerflow.persistence.run import RunRepository
    from deerflow.persistence.thread_meta import ThreadMetaRepository
    from deerflow.runtime.events.store.db import DbRunEventStore

    seed = await seed_m4_thread_database(database_url)
    scope = seed.owner_a_scope
    await ThreadMetaRepository(seed.factory).create(
        "t1",
        scope=scope,
        agent_asset_id=seed.project_agent_id,
        agent_scope="project",
    )
    await RunRepository(seed.factory).put("r1", thread_id="t1", scope=scope)
    return seed, DbRunEventStore(seed.factory)


@pytest.mark.anyio
async def test_db_task_id_filter(migrated_postgres_database_url):
    seed, store = await _make_db_store(migrated_postgres_database_url)
    try:
        await _check_task_id_filter(store, scope=seed.owner_a_scope)
    finally:
        await seed.engine.dispose()


@pytest.mark.anyio
async def test_db_task_id_after_seq_paginate(migrated_postgres_database_url):
    seed, store = await _make_db_store(migrated_postgres_database_url)
    try:
        await _check_task_id_after_seq_paginate(store, scope=seed.owner_a_scope)
    finally:
        await seed.engine.dispose()


@pytest.mark.anyio
async def test_db_no_task_id_returns_all(migrated_postgres_database_url):
    seed, store = await _make_db_store(migrated_postgres_database_url)
    try:
        await _check_no_task_id_returns_all(store, scope=seed.owner_a_scope)
    finally:
        await seed.engine.dispose()


# -- JSONL backend --


@pytest.mark.anyio
async def test_jsonl_task_id_filter(tmp_path):
    from deerflow.runtime.events.store.jsonl import JsonlRunEventStore

    await _check_task_id_filter(JsonlRunEventStore(base_dir=str(tmp_path)))


@pytest.mark.anyio
async def test_jsonl_task_id_after_seq_paginate(tmp_path):
    from deerflow.runtime.events.store.jsonl import JsonlRunEventStore

    await _check_task_id_after_seq_paginate(JsonlRunEventStore(base_dir=str(tmp_path)))
