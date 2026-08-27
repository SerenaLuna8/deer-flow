from __future__ import annotations

import asyncio
import uuid

import pytest
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from support.private_thread_seed import seed_private_thread_database
from support.run_closure import add_sealed_test_run

from app.private_work.checkpoint_delete_recovery import (
    CheckpointDeleteReconciler,
)
from app.private_work.checkpoint_state import checkpoint_config
from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.errors import (
    PrivateWorkConflict,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from app.private_work.thread_repository import (
    PrivateThreadRepository,
    ThreadAgentRef,
)
from app.private_work.thread_service import PrivateThreadService
from deerflow.persistence.run.model import RunRow
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


class _FailAfterPersistSaver(InMemorySaver):
    async def aput(self, config, checkpoint, metadata, new_versions):
        await super().aput(
            config,
            checkpoint,
            metadata,
            new_versions,
        )
        raise RuntimeError("checkpoint write failed after persistence")


class _FailTargetUpdateSaver(InMemorySaver):
    def __init__(self) -> None:
        super().__init__()
        self.fail_thread_id: str | None = None

    async def aput(self, config, checkpoint, metadata, new_versions):
        persisted = await super().aput(
            config,
            checkpoint,
            metadata,
            new_versions,
        )
        configurable = config.get("configurable", {})
        if configurable.get("thread_id") == self.fail_thread_id:
            self.fail_thread_id = None
            raise RuntimeError("target checkpoint failed after persistence")
        return persisted


class _FailingBranchRollbackHook:
    async def copy_branch_authority(self, *args, **kwargs) -> None:
        del args, kwargs

    async def rollback_branch_authority(self, *args, **kwargs) -> None:
        del args, kwargs
        raise RuntimeError("branch authority rollback failed")


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
async def test_user_delete_retains_checkpoint_and_never_enqueues_cleanup(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = str(uuid.uuid4())
    raw = InMemorySaver()
    try:
        scoped = ProjectScopedCheckpointer(raw, seed.factory)
        service = PrivateThreadService(
            seed.factory,
            scoped,
        )
        created = await service.create(
            seed.owner_a,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
        assert await raw.aget_tuple(checkpoint_config(thread_id)) is not None

        await service.delete(
            seed.owner_a,
            thread_id,
            expected_version=created.version,
        )
        await service.delete(
            seed.owner_a,
            thread_id,
            expected_version=created.version,
        )

        assert await raw.aget_tuple(checkpoint_config(thread_id)) is not None
        assert await service.get(seed.owner_a, thread_id) is None
        assert all(item.thread_id != thread_id for item in await service.search(seed.owner_a))
        with pytest.raises(PrivateWorkNotFound):
            await scoped.for_context(seed.owner_a).aget_tuple(
                checkpoint_config(thread_id),
            )
        assert await _delete_status(seed, thread_id) == "not_requested"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_failed_thread_create_physically_compensates_persisted_checkpoint(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = str(uuid.uuid4())
    raw = _FailAfterPersistSaver()
    try:
        service = PrivateThreadService(
            seed.factory,
            ProjectScopedCheckpointer(raw, seed.factory),
        )
        with pytest.raises(PrivateWorkUnavailable):
            await service.create(
                seed.owner_a,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )

        assert await raw.aget_tuple(checkpoint_config(thread_id)) is None
        async with seed.factory() as session:
            assert await session.get(ThreadMetaRow, thread_id) is None
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_failed_create_compensation_revokes_run_admitted_before_its_lock(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    raw = InMemorySaver()
    try:
        scoped = ProjectScopedCheckpointer(raw, seed.factory)
        created = await PrivateThreadService(seed.factory, scoped).create(
            seed.owner_a,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
        async with seed.factory() as session, session.begin():
            await add_sealed_test_run(
                session,
                RunRow(
                    run_id=run_id,
                    thread_id=thread_id,
                    assistant_id=str(seed.project_agent_id),
                    owner_user_id=str(seed.owner_a.user_id),
                    status="running",
                    metadata_json={},
                    kwargs_json={},
                    project_id=seed.owner_a.project_id,
                ),
            )

        compensator = scoped.for_context(seed.owner_a)
        tombstone = await compensator.atombstone_compensated_create(
            thread_id,
            expected_version=created.version,
            expected_created_at=created.created_at,
        )
        assert tombstone.deleted_at is not None
        cleaned = await compensator.acleanup_compensated_create(
            thread_id,
            expected_created_at=created.created_at,
            expected_deleted_at=tombstone.deleted_at,
        )

        assert cleaned
        assert await raw.aget_tuple(checkpoint_config(thread_id)) is None
        async with seed.factory() as session:
            run = await session.get(RunRow, run_id)
            tombstone = await session.get(ThreadMetaRow, thread_id)
            assert run is not None
            assert run.status == "interrupted"
            assert tombstone is not None
            assert tombstone.checkpoint_delete_status == "complete"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_branch_rollback_failure_retains_hidden_target_checkpoint(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    source_thread_id = str(uuid.uuid4())
    target_thread_id = str(uuid.uuid4())
    raw = _FailTargetUpdateSaver()
    try:
        scoped = ProjectScopedCheckpointer(raw, seed.factory)
        service = PrivateThreadService(
            seed.factory,
            scoped,
            branch_copy_hook=_FailingBranchRollbackHook(),
        )
        source = await service.create(
            seed.owner_a,
            thread_id=source_thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
        source_checkpoint = await raw.aget_tuple(
            checkpoint_config(source_thread_id),
        )
        assert source_checkpoint is not None
        checkpoint_id = source_checkpoint.config["configurable"]["checkpoint_id"]
        raw.fail_thread_id = target_thread_id

        with pytest.raises(PrivateWorkUnavailable):
            await service.branch(
                seed.owner_a,
                source_thread_id=source_thread_id,
                target_thread_id=target_thread_id,
                checkpoint_id=checkpoint_id,
                replay_base_checkpoint_id=checkpoint_id,
                expected_source_version=source.version,
            )

        assert await raw.aget_tuple(checkpoint_config(target_thread_id)) is not None
        async with seed.factory() as session:
            target = await session.get(ThreadMetaRow, target_thread_id)
            assert target is not None
            assert target.deleted_at is not None
            assert target.checkpoint_delete_status == "not_requested"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_stale_failed_create_compensation_cannot_tombstone_recreated_thread(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = str(uuid.uuid4())
    raw = InMemorySaver()
    try:
        scoped = ProjectScopedCheckpointer(raw, seed.factory)
        service = PrivateThreadService(seed.factory, scoped)
        original = await service.create(
            seed.owner_a,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
        compensator = scoped.for_context(seed.owner_a)
        tombstone = await compensator.atombstone_compensated_create(
            thread_id,
            expected_version=original.version,
            expected_created_at=original.created_at,
        )
        assert tombstone.deleted_at is not None
        assert await compensator.acleanup_compensated_create(
            thread_id,
            expected_created_at=original.created_at,
            expected_deleted_at=tombstone.deleted_at,
        )
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).purge_compensated_create(
                scope=seed.owner_a.resource_scope,
                thread_id=thread_id,
                expected_created_at=original.created_at,
                expected_deleted_at=tombstone.deleted_at,
            )

        replacement = await service.create(
            seed.owner_a,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
        with pytest.raises(PrivateWorkConflict):
            await compensator.atombstone_compensated_create(
                thread_id,
                expected_version=replacement.version,
                expected_created_at=original.created_at,
            )

        assert await raw.aget_tuple(checkpoint_config(thread_id)) is not None
        current = await service.get(seed.owner_a, thread_id)
        assert current is not None
        assert current.created_at == replacement.created_at
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
async def test_user_delete_does_not_depend_on_checkpoint_cleanup_status_writer(
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

        assert raw.calls == []
        assert await _delete_status(seed, thread_id) == "not_requested"
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
            tombstone = await repository.mark_deleted(
                scope=seed.owner_a.resource_scope,
                thread_id=thread_id,
                expected_version=1,
            )
            assert tombstone.deleted_at is not None
            await repository.request_checkpoint_delete_for_compensation(
                scope=seed.owner_a.resource_scope,
                thread_id=thread_id,
                expected_created_at=tombstone.created_at,
                expected_deleted_at=tombstone.deleted_at,
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
                tombstone = await PrivateThreadRepository(session).mark_deleted(
                    scope=seed.owner_a.resource_scope,
                    thread_id=thread_id,
                    expected_version=1,
                )
                assert tombstone.deleted_at is not None
                await PrivateThreadRepository(
                    session,
                ).request_checkpoint_delete_for_compensation(
                    scope=seed.owner_a.resource_scope,
                    thread_id=thread_id,
                    expected_created_at=tombstone.created_at,
                    expected_deleted_at=tombstone.deleted_at,
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
            tombstone = await PrivateThreadRepository(session).mark_deleted(
                scope=seed.owner_a.resource_scope,
                thread_id=thread_id,
                expected_version=1,
            )
            assert tombstone.deleted_at is not None
            await PrivateThreadRepository(
                session,
            ).request_checkpoint_delete_for_compensation(
                scope=seed.owner_a.resource_scope,
                thread_id=thread_id,
                expected_created_at=tombstone.created_at,
                expected_deleted_at=tombstone.deleted_at,
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
            tombstone = await repository.mark_deleted(
                scope=seed.owner_a.resource_scope,
                thread_id=thread_id,
                expected_version=1,
            )
            assert tombstone.deleted_at is not None
            await repository.request_checkpoint_delete_for_compensation(
                scope=seed.owner_a.resource_scope,
                thread_id=thread_id,
                expected_created_at=tombstone.created_at,
                expected_deleted_at=tombstone.deleted_at,
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
                expected_created_at=stale_candidate.created_at,
                expected_deleted_at=stale_candidate.deleted_at,
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
