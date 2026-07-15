"""Regression tests for #3120: SQL stores must emit tz-aware ISO timestamps.

SQLAlchemy's ``DateTime(timezone=True)`` is a no-op on SQLite because the
backend has no native timezone type, so values read back are naive
``datetime`` instances. The four SQL ``_row_to_dict`` helpers therefore
have to normalize through :func:`deerflow.utils.time.coerce_iso` instead
of calling ``.isoformat()`` directly; otherwise the API ships
timezone-less strings (e.g. ``"2026-05-20T06:10:22.970977"``) and the
frontend's ``new Date(...)`` parses them as local time, shifting recent
threads by the local UTC offset.
"""

import re

import pytest
from support.m4_private_threads import seed_m4_thread_database

_TZ_SUFFIX_RE = re.compile(r"(?:\+\d{2}:\d{2}|Z)$")


def _assert_tz_aware(value: str | None, *, context: str) -> None:
    assert value, f"{context}: expected ISO string, got {value!r}"
    assert _TZ_SUFFIX_RE.search(value), f"{context}: timestamp lacks tz suffix: {value!r}"


@pytest.mark.anyio
async def test_thread_meta_emits_tz_aware_timestamps(migrated_postgres_database_url):
    from deerflow.persistence.thread_meta import ThreadMetaRepository

    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    repo = ThreadMetaRepository(seed.factory)
    try:
        created = await repo.create(
            "t-tz",
            scope=seed.owner_a_scope,
            agent_asset_id=seed.project_agent_id,
            agent_scope="project",
            display_name="tz",
        )
        _assert_tz_aware(created["created_at"], context="thread_meta.create.created_at")
        _assert_tz_aware(created["updated_at"], context="thread_meta.create.updated_at")

        # Second read from DB exercises the same _row_to_dict path on a
        # value that PostgreSQL has round-tripped.
        fetched = await repo.get("t-tz", scope=seed.owner_a_scope)
        _assert_tz_aware(fetched["created_at"], context="thread_meta.get.created_at")
        _assert_tz_aware(fetched["updated_at"], context="thread_meta.get.updated_at")

        listed = await repo.search(scope=seed.owner_a_scope)
        assert listed, "search must return the created row"
        _assert_tz_aware(listed[0]["created_at"], context="thread_meta.search.created_at")
        _assert_tz_aware(listed[0]["updated_at"], context="thread_meta.search.updated_at")
    finally:
        await seed.engine.dispose()


@pytest.mark.anyio
async def test_run_repository_emits_tz_aware_timestamps(migrated_postgres_database_url):
    from deerflow.persistence.run import RunRepository
    from deerflow.persistence.thread_meta import ThreadMetaRepository

    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    repo = RunRepository(seed.factory)
    try:
        await ThreadMetaRepository(seed.factory).create(
            "t-tz",
            scope=seed.owner_a_scope,
            agent_asset_id=seed.project_agent_id,
            agent_scope="project",
        )
        await repo.put("r-tz", thread_id="t-tz", scope=seed.owner_a_scope)
        row = await repo.get("r-tz", scope=seed.owner_a_scope)
        _assert_tz_aware(row["created_at"], context="run.get.created_at")
        _assert_tz_aware(row["updated_at"], context="run.get.updated_at")
    finally:
        await seed.engine.dispose()


@pytest.mark.anyio
async def test_feedback_repository_emits_tz_aware_timestamps(migrated_postgres_database_url):
    from deerflow.persistence.feedback import FeedbackRepository
    from deerflow.persistence.run import RunRepository
    from deerflow.persistence.thread_meta import ThreadMetaRepository

    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    repo = FeedbackRepository(seed.factory)
    try:
        await ThreadMetaRepository(seed.factory).create(
            "t-tz",
            scope=seed.owner_a_scope,
            agent_asset_id=seed.project_agent_id,
            agent_scope="project",
        )
        await RunRepository(seed.factory).put(
            "r-tz",
            thread_id="t-tz",
            scope=seed.owner_a_scope,
        )
        record = await repo.create(
            run_id="r-tz",
            thread_id="t-tz",
            rating=1,
            scope=seed.owner_a_scope,
        )
        _assert_tz_aware(record["created_at"], context="feedback.create.created_at")
    finally:
        await seed.engine.dispose()


@pytest.mark.anyio
async def test_run_event_store_emits_tz_aware_timestamps(migrated_postgres_database_url):
    from deerflow.persistence.run import RunRepository
    from deerflow.persistence.thread_meta import ThreadMetaRepository
    from deerflow.runtime.events.store.db import DbRunEventStore

    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    store = DbRunEventStore(seed.factory)
    try:
        await ThreadMetaRepository(seed.factory).create(
            "t-tz",
            scope=seed.owner_a_scope,
            agent_asset_id=seed.project_agent_id,
            agent_scope="project",
        )
        await RunRepository(seed.factory).put(
            "r-tz",
            thread_id="t-tz",
            scope=seed.owner_a_scope,
        )
        await store.put(
            thread_id="t-tz",
            run_id="r-tz",
            event_type="log",
            category="log",
            content="hello",
            scope=seed.owner_a_scope,
        )
        events = await store.list_events(
            "t-tz",
            "r-tz",
            scope=seed.owner_a_scope,
        )
        assert events, "expected at least one event"
        _assert_tz_aware(events[0]["created_at"], context="run_event.list.created_at")
    finally:
        await seed.engine.dispose()
