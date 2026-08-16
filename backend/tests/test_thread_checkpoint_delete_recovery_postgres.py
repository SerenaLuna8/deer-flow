from __future__ import annotations

import asyncio
import uuid

import pytest
from langgraph.checkpoint.base import BaseCheckpointSaver
from support.private_thread_seed import seed_private_thread_database

from app.private_work.checkpoint_delete_recovery import (
    CheckpointDeleteReconciler,
)
from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.errors import PrivateWorkConflict, PrivateWorkNotFound
from app.private_work.thread_repository import (
    PrivateThreadRepository,
    ThreadAgentRef,
)
from deerflow.persistence.thread_meta.model import ThreadMetaRow


class _RawCheckpointSaver(BaseCheckpointSaver):
    def __init__(
        self,
        *,
        failures: set[str] | None = None,
        entered: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        super().__init__()
        self.calls: list[str] = []
        self.failures = failures or set()
        self.entered = entered
        self.release = release

    async def adelete_thread(self, thread_id: str) -> None:
        self.calls.append(thread_id)
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if thread_id in self.failures:
            raise RuntimeError("raw checkpoint cleanup failed")


async def _add_thread(seed, thread_id: str) -> None:
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=seed.owner_a.resource_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )


async def _delete_status(seed, thread_id: str) -> str:
    async with seed.factory() as session:
        row = await session.get(ThreadMetaRow, thread_id)
        assert row is not None
        return row.checkpoint_delete_status


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_delete_commits_tombstone_when_raw_cleanup_fails_and_repeat_recovers(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = str(uuid.uuid4())
    raw = _RawCheckpointSaver(failures={thread_id})
    try:
        await _add_thread(seed, thread_id)
        saver = ProjectScopedCheckpointer(raw, seed.factory).for_context(seed.owner_a)

        # The tombstone transaction is the public success boundary. A failed
        # physical checkpoint cleanup is durable retry work, not a false 503.
        await saver.adelete_thread(thread_id, expected_version=1)
        assert await _delete_status(seed, thread_id) == "retry_required"

        raw.failures.clear()
        # The browser retries the version it confirmed before the first call;
        # the scoped tombstone is idempotent even though its version advanced.
        await saver.adelete_thread(thread_id, expected_version=1)
        assert raw.calls == [thread_id, thread_id]
        assert await _delete_status(seed, thread_id) == "complete"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_repeat_delete_is_scoped_but_active_stale_version_still_conflicts(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    deleted_thread_id = str(uuid.uuid4())
    active_thread_id = str(uuid.uuid4())
    raw = _RawCheckpointSaver()
    try:
        await _add_thread(seed, deleted_thread_id)
        await _add_thread(seed, active_thread_id)
        owner_saver = ProjectScopedCheckpointer(raw, seed.factory).for_context(seed.owner_a)
        await owner_saver.adelete_thread(deleted_thread_id, expected_version=1)

        wrong_owner_saver = ProjectScopedCheckpointer(raw, seed.factory).for_context(seed.owner_b)
        with pytest.raises(PrivateWorkNotFound):
            await wrong_owner_saver.adelete_thread(
                deleted_thread_id,
                expected_version=1,
            )
        with pytest.raises(PrivateWorkConflict):
            await owner_saver.adelete_thread(active_thread_id, expected_version=99)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_delete_status_write_failure_does_not_change_public_success(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = str(uuid.uuid4())
    raw = _RawCheckpointSaver()
    try:
        await _add_thread(seed, thread_id)

        async def fail_status_write(*args, **kwargs):
            del args, kwargs
            raise RuntimeError("status write failed")

        monkeypatch.setattr(
            PrivateThreadRepository,
            "set_checkpoint_delete_status",
            fail_status_write,
        )
        saver = ProjectScopedCheckpointer(raw, seed.factory).for_context(seed.owner_a)
        await saver.adelete_thread(thread_id, expected_version=1)

        assert raw.calls == [thread_id]
        assert await _delete_status(seed, thread_id) == "pending"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_checkpoint_delete_status_transition_is_monotonic(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = str(uuid.uuid4())
    try:
        await _add_thread(seed, thread_id)
        async with seed.factory() as session, session.begin():
            repository = PrivateThreadRepository(session)
            await repository.mark_deleted(
                scope=seed.owner_a.resource_scope,
                thread_id=thread_id,
                expected_version=1,
            )
            assert await repository.set_checkpoint_delete_status(
                scope=seed.owner_a.resource_scope,
                thread_id=thread_id,
                status="complete",
            )

        async with seed.factory() as session, session.begin():
            changed = await PrivateThreadRepository(
                session,
            ).set_checkpoint_delete_status(
                scope=seed.owner_a.resource_scope,
                thread_id=thread_id,
                status="retry_required",
            )
            assert not changed

        assert await _delete_status(seed, thread_id) == "complete"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_reconciler_continues_after_one_bad_item(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    bad_thread_id = str(uuid.uuid4())
    good_thread_id = str(uuid.uuid4())
    raw = _RawCheckpointSaver(failures={bad_thread_id})
    try:
        for thread_id in (bad_thread_id, good_thread_id):
            await _add_thread(seed, thread_id)
            async with seed.factory() as session, session.begin():
                await PrivateThreadRepository(session).mark_deleted(
                    scope=seed.owner_a.resource_scope,
                    thread_id=thread_id,
                    expected_version=1,
                )

        reconciler = CheckpointDeleteReconciler(
            raw,
            seed.factory,
            batch_size=10,
            interval_seconds=60,
        )
        report = await reconciler.run_once()

        assert report.selected == 2
        assert report.completed == 1
        assert report.retry_required == 1
        assert set(raw.calls) == {bad_thread_id, good_thread_id}
        assert await _delete_status(seed, bad_thread_id) == "retry_required"
        assert await _delete_status(seed, good_thread_id) == "complete"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_concurrent_reconcilers_serialize_under_exact_tombstone_fence(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = str(uuid.uuid4())
    entered = asyncio.Event()
    release = asyncio.Event()
    raw = _RawCheckpointSaver(entered=entered, release=release)
    try:
        await _add_thread(seed, thread_id)
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).mark_deleted(
                scope=seed.owner_a.resource_scope,
                thread_id=thread_id,
                expected_version=1,
            )

        first = CheckpointDeleteReconciler(raw, seed.factory)
        second = CheckpointDeleteReconciler(raw, seed.factory)
        first_task = asyncio.create_task(first.run_once())
        await asyncio.wait_for(entered.wait(), timeout=2)
        second_task = asyncio.create_task(second.run_once())
        await asyncio.sleep(0.05)
        assert raw.calls == [thread_id]
        release.set()
        await asyncio.gather(first_task, second_task)

        assert raw.calls == [thread_id]
        assert await _delete_status(seed, thread_id) == "complete"
    finally:
        release.set()
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_stale_candidate_does_not_delete_recreated_active_thread_checkpoint(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = str(uuid.uuid4())
    raw = _RawCheckpointSaver()
    try:
        await _add_thread(seed, thread_id)
        async with seed.factory() as session, session.begin():
            repository = PrivateThreadRepository(session)
            await repository.mark_deleted(
                scope=seed.owner_a.resource_scope,
                thread_id=thread_id,
                expected_version=1,
            )
            stale_candidate = (await repository.list_checkpoint_delete_candidates(limit=10))[0]
            await repository.set_checkpoint_delete_status(
                scope=seed.owner_a.resource_scope,
                thread_id=thread_id,
                status="complete",
            )
            await repository.purge_compensated_create(
                scope=seed.owner_a.resource_scope,
                thread_id=thread_id,
            )

        await _add_thread(seed, thread_id)
        reconciler = CheckpointDeleteReconciler(raw, seed.factory)
        recovered = await reconciler.recover_candidate(stale_candidate)

        assert recovered
        assert raw.calls == []
        async with seed.factory() as session:
            recreated = await session.get(ThreadMetaRow, thread_id)
            assert recreated is not None
            assert recreated.deleted_at is None
            assert recreated.checkpoint_delete_status == "not_requested"
    finally:
        await seed.engine.dispose()
