"""Cross-owner isolation tests for final-schema private work.

Every product-path call carries an issued ``project_id + owner_user_id`` scope.
No unscoped read or mutation path exists.
"""

from __future__ import annotations

import pytest
from support.private_thread_seed import seed_private_thread_database

from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from deerflow.persistence.feedback import FeedbackRepository
from deerflow.persistence.run import RunRepository
from deerflow.persistence.thread_meta import ThreadMetaRepository
from deerflow.runtime.events.store.db import DbRunEventStore


async def _seed_private_data(database_url):
    seed = await seed_private_thread_database(database_url)
    thread_repo = ThreadMetaRepository(seed.factory)
    for thread_id, scope in (
        ("t-alpha", seed.owner_a_scope),
        ("t-beta", seed.owner_b_scope),
    ):
        await thread_repo.create(
            thread_id,
            display_name=f"{thread_id} private thread",
            scope=scope,
            agent_asset_id=seed.project_agent_id,
            agent_scope="project",
        )
    async with seed.factory() as session, session.begin():
        for run_id, thread_id, scope in (
            ("run-a1", "t-alpha", seed.owner_a_scope),
            ("run-a2", "t-alpha", seed.owner_a_scope),
            ("run-b1", "t-beta", seed.owner_b_scope),
        ):
            await PrivateRunRepository(session).create_terminal_empty_shell(
                scope=scope,
                thread_id=thread_id,
                request=PrivateRunCreate(run_id=run_id, status="success"),
            )
    return seed


@pytest.mark.anyio
async def test_thread_meta_cross_user_isolation(migrated_postgres_database_url):
    seed = await _seed_private_data(migrated_postgres_database_url)
    try:
        repo = ThreadMetaRepository(seed.factory)
        a_view = await repo.get("t-alpha", scope=seed.owner_a_scope)
        assert a_view is not None
        assert a_view["display_name"] == "t-alpha private thread"
        assert await repo.get("t-beta", scope=seed.owner_a_scope) is None
        assert [row["thread_id"] for row in await repo.search(scope=seed.owner_a_scope)] == ["t-alpha"]

        b_view = await repo.get("t-beta", scope=seed.owner_b_scope)
        assert b_view is not None
        assert b_view["display_name"] == "t-beta private thread"
        assert await repo.get("t-alpha", scope=seed.owner_b_scope) is None
        assert [row["thread_id"] for row in await repo.search(scope=seed.owner_b_scope)] == ["t-beta"]
    finally:
        await seed.engine.dispose()


@pytest.mark.anyio
async def test_thread_meta_cross_user_mutation_denied(migrated_postgres_database_url):
    seed = await _seed_private_data(migrated_postgres_database_url)
    try:
        repo = ThreadMetaRepository(seed.factory)
        await repo.update_display_name("t-alpha", "hacked", scope=seed.owner_b_scope)
        row = await repo.get("t-alpha", scope=seed.owner_a_scope)
        assert row is not None
        assert row["display_name"] == "t-alpha private thread"

        await repo.delete("t-alpha", scope=seed.owner_b_scope)
        assert await repo.get("t-alpha", scope=seed.owner_a_scope) is not None
    finally:
        await seed.engine.dispose()


@pytest.mark.anyio
async def test_runs_cross_user_isolation(migrated_postgres_database_url):
    seed = await _seed_private_data(migrated_postgres_database_url)
    try:
        repo = RunRepository(seed.factory)
        assert await repo.get("run-a1", scope=seed.owner_a_scope) is not None
        assert await repo.get("run-b1", scope=seed.owner_a_scope) is None
        a_runs = await repo.list_by_thread("t-alpha", scope=seed.owner_a_scope)
        assert {row["run_id"] for row in a_runs} == {"run-a1", "run-a2"}
        assert await repo.list_by_thread("t-beta", scope=seed.owner_a_scope) == []

        assert await repo.get("run-a1", scope=seed.owner_b_scope) is None
        b_runs = await repo.list_by_thread("t-beta", scope=seed.owner_b_scope)
        assert [row["run_id"] for row in b_runs] == ["run-b1"]
    finally:
        await seed.engine.dispose()


@pytest.mark.anyio
async def test_runs_cross_user_delete_denied(migrated_postgres_database_url):
    seed = await _seed_private_data(migrated_postgres_database_url)
    try:
        repo = RunRepository(seed.factory)
        await repo.delete("run-a1", scope=seed.owner_b_scope)
        assert await repo.get("run-a1", scope=seed.owner_a_scope) is not None
    finally:
        await seed.engine.dispose()


@pytest.mark.anyio
async def test_run_events_cross_user_isolation(migrated_postgres_database_url):
    """Conversation content never crosses the explicit owner boundary."""
    seed = await _seed_private_data(migrated_postgres_database_url)
    try:
        store = DbRunEventStore(seed.factory)
        for event_type, content in (
            ("human_message", "User A private question"),
            ("ai_message", "User A private answer"),
        ):
            await store.put(
                thread_id="t-alpha",
                run_id="run-a1",
                event_type=event_type,
                category="message",
                content=content,
                scope=seed.owner_a_scope,
            )
        await store.put(
            thread_id="t-beta",
            run_id="run-b1",
            event_type="human_message",
            category="message",
            content="User B private question",
            scope=seed.owner_b_scope,
        )

        a_contents = [row["content"] for row in await store.list_messages("t-alpha", scope=seed.owner_a_scope)]
        assert a_contents == ["User A private question", "User A private answer"]
        assert await store.list_messages("t-beta", scope=seed.owner_a_scope) == []
        assert await store.list_events("t-beta", "run-b1", scope=seed.owner_a_scope) == []
        assert await store.count_messages("t-beta", scope=seed.owner_a_scope) == 0

        b_contents = [row["content"] for row in await store.list_messages("t-beta", scope=seed.owner_b_scope)]
        assert b_contents == ["User B private question"]
        assert await store.count_messages("t-alpha", scope=seed.owner_b_scope) == 0
    finally:
        await seed.engine.dispose()


@pytest.mark.anyio
async def test_run_events_cross_user_delete_denied(migrated_postgres_database_url):
    seed = await _seed_private_data(migrated_postgres_database_url)
    try:
        store = DbRunEventStore(seed.factory)
        await store.put(
            thread_id="t-alpha",
            run_id="run-a1",
            event_type="human_message",
            category="message",
            content="hello",
            scope=seed.owner_a_scope,
        )
        assert await store.delete_by_thread("t-alpha", scope=seed.owner_b_scope) == 0
        assert await store.count_messages("t-alpha", scope=seed.owner_a_scope) == 1
    finally:
        await seed.engine.dispose()


@pytest.mark.anyio
async def test_feedback_cross_user_isolation(migrated_postgres_database_url):
    seed = await _seed_private_data(migrated_postgres_database_url)
    try:
        repo = FeedbackRepository(seed.factory)
        a_feedback = await repo.create(
            run_id="run-a1",
            thread_id="t-alpha",
            rating=1,
            comment="A liked this",
            scope=seed.owner_a_scope,
        )
        b_feedback = await repo.create(
            run_id="run-b1",
            thread_id="t-beta",
            rating=-1,
            comment="B disliked this",
            scope=seed.owner_b_scope,
        )

        assert (await repo.get(a_feedback["feedback_id"], scope=seed.owner_a_scope))["comment"] == "A liked this"
        assert await repo.get(b_feedback["feedback_id"], scope=seed.owner_a_scope) is None
        assert await repo.list_by_run("t-beta", "run-b1", scope=seed.owner_a_scope) == []
        assert await repo.get(a_feedback["feedback_id"], scope=seed.owner_b_scope) is None
        b_list = await repo.list_by_run("t-beta", "run-b1", scope=seed.owner_b_scope)
        assert [row["comment"] for row in b_list] == ["B disliked this"]
    finally:
        await seed.engine.dispose()


@pytest.mark.anyio
async def test_feedback_cross_user_delete_denied(migrated_postgres_database_url):
    seed = await _seed_private_data(migrated_postgres_database_url)
    try:
        repo = FeedbackRepository(seed.factory)
        feedback = await repo.create(
            run_id="run-a1",
            thread_id="t-alpha",
            rating=1,
            scope=seed.owner_a_scope,
        )
        assert await repo.delete(feedback["feedback_id"], scope=seed.owner_b_scope) is False
        assert await repo.get(feedback["feedback_id"], scope=seed.owner_a_scope) is not None
    finally:
        await seed.engine.dispose()


@pytest.mark.anyio
async def test_product_repositories_without_scope_fail_closed(migrated_postgres_database_url):
    seed = await _seed_private_data(migrated_postgres_database_url)
    try:
        run_repo = RunRepository(seed.factory)
        event_store = DbRunEventStore(seed.factory)
        feedback_repo = FeedbackRepository(seed.factory)
        assert await run_repo.get("run-a1") is None
        assert await run_repo.list_by_thread("t-alpha") == []
        assert await event_store.list_messages("t-alpha") == []
        assert await feedback_repo.get("anything") is None
    finally:
        await seed.engine.dispose()
