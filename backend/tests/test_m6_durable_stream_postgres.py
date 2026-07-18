from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from support.m4_private_threads import seed_m4_thread_database

from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.reliability.execution import LeaseAuthorizedStreamBridge
from deerflow.runtime.events.models import (
    StreamFrame,
    StreamLeaseProof,
    StreamScopeRequired,
    StreamWriteAuthorizationRevoked,
)
from deerflow.runtime.events.store.db import DbRunEventStore
from deerflow.runtime.stream_bridge.postgres import (
    PostgresStreamBridge,
    StreamClosed,
    StreamCursorOutOfRange,
    StreamScopeNotFound,
)


class RecordingNotifier:
    def __init__(self, factory=None, *, fail: bool = False) -> None:
        self.factory = factory
        self.fail = fail
        self.calls: list[tuple[str, str]] = []
        self.visible_at_notify: list[int] = []

    async def best_effort_notify(self, thread_id: str, event_id: str) -> None:
        self.calls.append((thread_id, event_id))
        if self.factory is not None:
            async with self.factory() as session:
                count = await session.scalar(
                    text(
                        """SELECT count(*) FROM run_events
                           WHERE thread_id=:thread_id AND seq=:seq"""
                    ),
                    {"thread_id": thread_id, "seq": int(event_id)},
                )
            self.visible_at_notify.append(int(count or 0))
        if self.fail:
            raise RuntimeError("notify unavailable")


async def _seed_run(seed, *, thread_id: str, run_id: str) -> None:
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
        await PrivateRunRepository(session).create(
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            request=PrivateRunCreate(run_id=run_id),
        )


async def _wait_for_advisory_wait(factory) -> None:
    for _ in range(200):
        async with factory() as session:
            waiting = await session.scalar(
                text(
                    """SELECT EXISTS (
                        SELECT 1 FROM pg_stat_activity
                        WHERE datname=current_database()
                          AND pid<>pg_backend_pid()
                          AND wait_event_type='Lock'
                          AND wait_event='advisory'
                    )"""
                )
            )
        if waiting:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("stream repair did not wait on the thread advisory lock")


@pytest.mark.postgres
@pytest.mark.anyio
async def test_frames_survive_bridge_restart_with_gap_free_decimal_cursor(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"m6-stream-{uuid.uuid4()}"
    run_id = str(uuid.uuid4())
    try:
        await _seed_run(seed, thread_id=thread_id, run_id=run_id)
        first = PostgresStreamBridge(seed.factory)
        stored = [
            await first.publish_frame(
                seed.owner_a_scope,
                thread_id,
                run_id,
                StreamFrame(event="updates", data={"delta": str(index)}),
            )
            for index in range(5)
        ]
        assert [frame.id for frame in stored] == ["1", "2", "3", "4", "5"]

        reopened = PostgresStreamBridge(seed.factory)
        first_page = await reopened.read_after(
            seed.owner_a_scope,
            thread_id,
            cursor=0,
            limit=2,
        )
        second_page = await reopened.read_after(
            seed.owner_a_scope,
            thread_id,
            cursor=int(first_page[-1].id),
            limit=2,
        )
        final_page = await reopened.read_after(
            seed.owner_a_scope,
            thread_id,
            cursor=int(second_page[-1].id),
            limit=2,
        )
        assert [frame.id for frame in (*first_page, *second_page, *final_page)] == [
            "1",
            "2",
            "3",
            "4",
            "5",
        ]
        assert [frame.data for frame in final_page] == [{"delta": "4"}]
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_thread_cursor_high_watermark_survives_run_event_retention(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"m6-stream-retention-{uuid.uuid4()}"
    first_run_id = str(uuid.uuid4())
    second_run_id = str(uuid.uuid4())
    try:
        await _seed_run(seed, thread_id=thread_id, run_id=first_run_id)
        bridge = PostgresStreamBridge(seed.factory)
        await bridge.publish_frame(
            seed.owner_a_scope,
            thread_id,
            first_run_id,
            StreamFrame(event="updates", data={"delta": "old"}),
        )
        old_terminal = await bridge.publish_terminal(
            seed.owner_a_scope,
            thread_id,
            first_run_id,
            status="completed",
        )

        async with seed.factory() as session, session.begin():
            repository = PrivateRunRepository(session)
            assert await repository.delete(
                scope=seed.owner_a_scope,
                run_id=first_run_id,
            )
            await repository.create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                request=PrivateRunCreate(run_id=second_run_id),
            )

        new_frame = await bridge.publish_frame(
            seed.owner_a_scope,
            thread_id,
            second_run_id,
            StreamFrame(event="updates", data={"delta": "new"}),
        )
        replay = await bridge.read_after(
            seed.owner_a_scope,
            thread_id,
            cursor=int(old_terminal.id),
            limit=10,
            run_id=second_run_id,
        )

        assert int(new_frame.id) > int(old_terminal.id)
        assert len(replay) == 1
        assert replay[0].id == new_frame.id
        assert replay[0].run_id == second_run_id
        assert replay[0].data == {"delta": "new"}
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_terminal_repair_revalidates_governance_after_thread_lock_wait(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"m6-stream-repair-revoke-{uuid.uuid4()}"
    run_id = str(uuid.uuid4())
    repair_task = None
    try:
        await _seed_run(seed, thread_id=thread_id, run_id=run_id)
        async with seed.factory() as session, session.begin():
            assert await PrivateRunRepository(session).update_status(
                scope=seed.owner_a_scope,
                run_id=run_id,
                status="interrupted",
            )

        bridge = PostgresStreamBridge(seed.factory)
        async with seed.factory() as blocker, blocker.begin():
            await blocker.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(CAST(:thread_id AS text))::bigint)"),
                {"thread_id": thread_id},
            )
            repair_task = asyncio.create_task(
                bridge.ensure_settled_terminal(
                    seed.owner_a_scope,
                    thread_id,
                    run_id,
                    status="interrupted",
                )
            )
            await _wait_for_advisory_wait(seed.factory)
            async with seed.factory() as session, session.begin():
                await session.execute(
                    text(
                        """UPDATE project_memberships
                           SET role='viewer',version=version+1,updated_at=now()
                           WHERE project_id=:project_id AND user_id=:user_id"""
                    ),
                    {
                        "project_id": seed.owner_a.project_id,
                        "user_id": str(seed.owner_a.user_id),
                    },
                )

        with pytest.raises(StreamWriteAuthorizationRevoked):
            await repair_task
        frames = await bridge.read_after(
            seed.owner_a_scope,
            thread_id,
            cursor=0,
            limit=10,
            run_id=run_id,
        )
        assert frames == ()
    finally:
        if repair_task is not None and not repair_task.done():
            repair_task.cancel()
            await asyncio.gather(repair_task, return_exceptions=True)
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_stream_read_is_strictly_scoped_and_rejects_invalid_contract(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"m6-stream-scope-{uuid.uuid4()}"
    run_id = str(uuid.uuid4())
    try:
        await _seed_run(seed, thread_id=thread_id, run_id=run_id)
        bridge = PostgresStreamBridge(seed.factory)
        await bridge.publish_frame(
            seed.owner_a_scope,
            thread_id,
            run_id,
            StreamFrame(event="metadata", data={"run_id": run_id}),
        )

        with pytest.raises(StreamScopeNotFound):
            await bridge.read_after(
                seed.owner_b_scope,
                thread_id,
                cursor=0,
                limit=10,
            )
        with pytest.raises(StreamScopeNotFound):
            await bridge.publish_frame(
                seed.owner_b_scope,
                thread_id,
                run_id,
                StreamFrame(event="updates", data={"forged": True}),
            )
        with pytest.raises(StreamScopeNotFound):
            await bridge.read_after(
                seed.project_b_owner_a_scope,
                thread_id,
                cursor=0,
                limit=10,
            )
        with pytest.raises(StreamCursorOutOfRange):
            await bridge.read_after(
                seed.owner_a_scope,
                thread_id,
                cursor=999,
                limit=10,
            )
        with pytest.raises(ValueError, match="cursor"):
            await bridge.read_after(
                seed.owner_a_scope,
                thread_id,
                cursor=-1,
                limit=10,
            )
        with pytest.raises(ValueError, match="limit"):
            await bridge.read_after(
                seed.owner_a_scope,
                thread_id,
                cursor=0,
                limit=0,
            )
        with pytest.raises(ValueError, match="category"):
            StreamFrame(event="updates", data={}, category="trace")
        with pytest.raises(ValueError, match="reserved"):
            StreamFrame(event="end", data={})
        with pytest.raises(StreamScopeRequired):
            await bridge.publish(run_id, "updates", {"forged": True})
        with pytest.raises(StreamScopeRequired):
            await bridge.publish_end(run_id)
        with pytest.raises(StreamScopeRequired):
            await bridge.stream_exists(run_id)
        with pytest.raises(StreamScopeRequired):
            bridge.subscribe(run_id)

        for invalid_cursor in (
            "01",
            "١",
            "+1",
            "-1",
            "9223372036854775808",
            "9" * 5_000,
        ):
            subscription = bridge.subscribe_scoped(
                seed.owner_a_scope,
                thread_id,
                run_id,
                last_event_id=invalid_cursor,
                heartbeat_interval=0.001,
            )
            with pytest.raises(ValueError, match="canonical ASCII"):
                await anext(subscription)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_generic_event_store_rejects_reserved_stream_writes(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"m6-stream-reserved-{uuid.uuid4()}"
    run_id = str(uuid.uuid4())
    try:
        await _seed_run(seed, thread_id=thread_id, run_id=run_id)
        store = DbRunEventStore(seed.factory)
        with pytest.raises(ValueError, match="reserved"):
            await store.put(
                thread_id=thread_id,
                run_id=run_id,
                event_type="stream.end",
                category="stream",
                content={"status": "success"},
                scope=seed.owner_a_scope,
            )
        with pytest.raises(ValueError, match="reserved"):
            await store.put_batch(
                [
                    {
                        "thread_id": thread_id,
                        "run_id": run_id,
                        "event_type": "stream.end",
                        "category": "message",
                        "content": {"status": "success"},
                    }
                ],
                scope=seed.owner_a_scope,
            )
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_publish_commits_before_best_effort_notify_and_rollback_never_notifies(
    migrated_postgres_database_url: str,
    monkeypatch,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"m6-stream-notify-{uuid.uuid4()}"
    run_id = str(uuid.uuid4())
    notifier = RecordingNotifier(seed.factory)
    try:
        await _seed_run(seed, thread_id=thread_id, run_id=run_id)
        bridge = PostgresStreamBridge(seed.factory, notifier=notifier)
        stored = await bridge.publish_frame(
            seed.owner_a_scope,
            thread_id,
            run_id,
            StreamFrame(event="updates", data={"delta": "committed"}),
        )
        assert notifier.calls == [(thread_id, stored.id)]
        assert notifier.visible_at_notify == [1]

        original_append = bridge._events.append_stream_frame

        async def append_then_fail(*args, **kwargs):
            await original_append(*args, **kwargs)
            raise RuntimeError("force rollback")

        monkeypatch.setattr(
            bridge._events,
            "append_stream_frame",
            append_then_fail,
        )
        with pytest.raises(RuntimeError, match="force rollback"):
            await bridge.publish_frame(
                seed.owner_a_scope,
                thread_id,
                run_id,
                StreamFrame(event="updates", data={"delta": "rolled back"}),
            )
        assert notifier.calls == [(thread_id, stored.id)]
        frames = await PostgresStreamBridge(seed.factory).read_after(
            seed.owner_a_scope,
            thread_id,
            cursor=0,
            limit=10,
        )
        assert [frame.data for frame in frames] == [{"delta": "committed"}]

        failing_notifier = RecordingNotifier(fail=True)
        durable = PostgresStreamBridge(seed.factory, notifier=failing_notifier)
        persisted = await durable.publish_frame(
            seed.owner_a_scope,
            thread_id,
            run_id,
            StreamFrame(event="updates", data={"delta": "notify failed"}),
        )
        assert persisted.id == "2"
        frames = await PostgresStreamBridge(seed.factory).read_after(
            seed.owner_a_scope,
            thread_id,
            cursor=1,
            limit=10,
        )
        assert [frame.data for frame in frames] == [{"delta": "notify failed"}]
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_terminal_is_unique_under_race_and_closes_stream(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"m6-stream-terminal-{uuid.uuid4()}"
    run_id = str(uuid.uuid4())
    try:
        await _seed_run(seed, thread_id=thread_id, run_id=run_id)
        first = PostgresStreamBridge(seed.factory)
        second = PostgresStreamBridge(seed.factory)
        terminals = await asyncio.gather(
            first.publish_terminal(
                seed.owner_a_scope,
                thread_id,
                run_id,
                status="completed",
            ),
            second.publish_terminal(
                seed.owner_a_scope,
                thread_id,
                run_id,
                status="completed",
            ),
        )
        retry = await first.publish_terminal(
            seed.owner_a_scope,
            thread_id,
            run_id,
            status="completed",
        )
        assert {frame.id for frame in (*terminals, retry)} == {"1"}
        assert sum(frame.created for frame in (*terminals, retry)) == 1

        with pytest.raises(StreamClosed):
            await first.publish_frame(
                seed.owner_a_scope,
                thread_id,
                run_id,
                StreamFrame(event="updates", data={"late": True}),
            )
        with pytest.raises(IntegrityError):
            async with seed.factory() as session, session.begin():
                await session.execute(
                    text(
                        """INSERT INTO run_events
                           (project_id,owner_user_id,thread_id,run_id,event_type,
                            category,content,event_metadata,seq,created_at)
                           VALUES
                           (:project_id,:owner_user_id,:thread_id,:run_id,
                            'stream.end','stream','{}','{}',2,now())"""
                    ),
                    {
                        "project_id": seed.owner_a.project_id,
                        "owner_user_id": str(seed.owner_a.user_id),
                        "thread_id": thread_id,
                        "run_id": run_id,
                    },
                )
        async with seed.factory() as session:
            terminal_count = await session.scalar(
                text(
                    """SELECT count(*) FROM run_events
                       WHERE project_id=:project_id
                         AND owner_user_id=:owner_user_id
                         AND thread_id=:thread_id
                         AND run_id=:run_id
                         AND category='stream'
                         AND event_type='stream.end'"""
                ),
                {
                    "project_id": seed.owner_a.project_id,
                    "owner_user_id": str(seed.owner_a.user_id),
                    "thread_id": thread_id,
                    "run_id": run_id,
                },
            )
        assert terminal_count == 1
    finally:
        await seed.engine.dispose()


@pytest.mark.asyncio
async def test_lease_authorized_bridge_supplies_explicit_private_scope() -> None:
    scope = object()
    lease = StreamLeaseProof(
        job_id=uuid.uuid4(),
        lease_token="test-lease-token",
    )
    raw_bridge = type(
        "DurableBridge",
        (),
        {
            "publish_frame": AsyncMock(),
            "publish_terminal": AsyncMock(),
        },
    )()
    boundary = type(
        "Boundary",
        (),
        {
            "before_stream_publish": AsyncMock(),
            "before_stream_terminal": AsyncMock(),
            "stream_lease_proof": Mock(return_value=lease),
            "record_stream_authorization_revoked": Mock(),
            "record_stream_lease_lost": Mock(),
            "request_local_cancel": Mock(),
        },
    )()
    bridge = LeaseAuthorizedStreamBridge(
        raw_bridge,
        boundary,
        scope=scope,
        thread_id="thread-1",
        terminal_status=lambda: "interrupted",
    )

    await bridge.publish("run-1", "updates", {"delta": "A"})
    await bridge.publish_end("run-1")

    raw_bridge.publish_frame.assert_awaited_once_with(
        scope,
        "thread-1",
        "run-1",
        StreamFrame(event="updates", data={"delta": "A"}),
        lease=lease,
    )
    raw_bridge.publish_terminal.assert_awaited_once_with(
        scope,
        "thread-1",
        "run-1",
        status="interrupted",
        lease=lease,
    )
    boundary.before_stream_publish.assert_not_awaited()
    boundary.before_stream_terminal.assert_not_awaited()
