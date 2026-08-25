from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, text
from support.private_thread_seed import seed_private_thread_database

from app.private_work.account_private_lifecycle import (
    AccountPrivateLifecycle,
    AccountPrivateLifecycleClosed,
)
from app.private_work.errors import PrivateWorkForbidden
from app.private_work.run_admission import PrivateRunAdmissionService
from app.private_work.run_repository import PrivateRunCreate
from app.private_work.thread_repository import (
    PrivateThreadRepository,
    ThreadAgentRef,
)
from deerflow.persistence.jobs.model import JobRow
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.user.model import UserRow


async def _set_transaction_application_name(session, name: str) -> None:
    await session.execute(
        text(
            "SELECT set_config('application_name', :application_name, true)",
        ),
        {"application_name": name},
    )


async def _wait_for_postgres_lock(factory, application_name: str) -> str:
    async def observe() -> str:
        while True:
            async with factory() as session:
                row = (
                    await session.execute(
                        text(
                            """SELECT wait_event
                                 FROM pg_stat_activity
                                WHERE datname = current_database()
                                  AND application_name = :application_name
                                  AND wait_event_type = 'Lock'
                                ORDER BY pid
                                LIMIT 1"""
                        ),
                        {"application_name": application_name},
                    )
                ).scalar_one_or_none()
            if isinstance(row, str) and row:
                return row
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(observe(), timeout=5)


@pytest.mark.postgres
@pytest.mark.anyio
async def test_account_purge_waits_for_writer_then_observes_the_stable_scope(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    lifecycle = AccountPrivateLifecycle()
    writer_locked = asyncio.Event()
    release_writer = asyncio.Event()
    purge_attempting_lock = asyncio.Event()
    purge_application_name = f"account-purge-{uuid.uuid4().hex[:20]}"
    effective_at = datetime.now(UTC)

    async def writer():
        async with seed.factory() as session, session.begin():
            await session.scalar(select(ProjectRow.id).where(ProjectRow.id == seed.owner_a.project_id).with_for_update(of=ProjectRow))
            await session.scalar(select(ProjectMembershipRow.id).where(ProjectMembershipRow.id == seed.owner_a.membership_id).with_for_update(of=ProjectMembershipRow))
            generation = await lifecycle.require_active_after_membership(
                session,
                seed.owner_a.user_id,
            )
            writer_locked.set()
            await release_writer.wait()
            return generation

    async def purge():
        await writer_locked.wait()
        async with seed.factory() as session, session.begin():
            await _set_transaction_application_name(
                session,
                purge_application_name,
            )
            purge_attempting_lock.set()
            locked = await lifecycle.lock_stable_scope_for_purge(
                session,
                seed.owner_a.user_id,
            )
            return await lifecycle.begin_purge_after_memberships(
                session,
                locked,
                effective_at=effective_at,
            )

    writer_task = asyncio.create_task(writer())
    purge_task = None
    try:
        await asyncio.wait_for(writer_locked.wait(), timeout=2)
        purge_task = asyncio.create_task(purge())
        await asyncio.wait_for(purge_attempting_lock.wait(), timeout=2)
        wait_event = await _wait_for_postgres_lock(
            seed.factory,
            purge_application_name,
        )
        assert purge_task.done() is False
        assert wait_event in {"transactionid", "tuple"}

        release_writer.set()
        generation, fence = await asyncio.wait_for(
            asyncio.gather(writer_task, purge_task),
            timeout=5,
        )

        assert generation.generation == 1
        assert fence.generation == 2
        assert fence.project_ids == (seed.owner_a.project_id,)
        assert fence.membership_ids == (seed.owner_a.membership_id,)
    finally:
        release_writer.set()
        for task in (writer_task, purge_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (writer_task, purge_task) if task is not None),
            return_exceptions=True,
        )
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_account_purge_first_rolls_back_an_uncommitted_new_project_writer(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    lifecycle = AccountPrivateLifecycle()
    purge_locked = asyncio.Event()
    release_purge = asyncio.Event()
    writer_attempting_lock = asyncio.Event()
    writer_application_name = f"account-writer-{uuid.uuid4().hex[:20]}"
    effective_at = datetime.now(UTC)
    new_project_id = uuid.uuid4()
    new_membership_id = uuid.uuid4()

    async def purge():
        async with seed.factory() as session, session.begin():
            locked = await lifecycle.lock_stable_scope_for_purge(
                session,
                seed.owner_a.user_id,
            )
            fence = await lifecycle.begin_purge_after_memberships(
                session,
                locked,
                effective_at=effective_at,
            )
            purge_locked.set()
            await release_purge.wait()
            return fence

    async def writer() -> None:
        await purge_locked.wait()
        async with seed.factory() as session, session.begin():
            await _set_transaction_application_name(
                session,
                writer_application_name,
            )
            await session.execute(
                text(
                    """INSERT INTO projects (
                           id, slug, display_name, created_by_user_id
                       ) VALUES (
                           :project_id, :slug, 'Concurrent project', :owner_id
                       )"""
                ),
                {
                    "project_id": new_project_id,
                    "slug": f"concurrent-{new_project_id.hex[:12]}",
                    "owner_id": str(seed.owner_a.user_id),
                },
            )
            await session.execute(
                text(
                    """INSERT INTO project_memberships (
                           id, project_id, user_id, role, status, version
                       ) VALUES (
                           :membership_id, :project_id, :owner_id,
                           'admin', 'active', 1
                       )"""
                ),
                {
                    "membership_id": new_membership_id,
                    "project_id": new_project_id,
                    "owner_id": str(seed.owner_a.user_id),
                },
            )
            writer_attempting_lock.set()
            await lifecycle.require_active_after_membership(
                session,
                seed.owner_a.user_id,
            )

    purge_task = asyncio.create_task(purge())
    writer_task = None
    try:
        await asyncio.wait_for(purge_locked.wait(), timeout=2)
        writer_task = asyncio.create_task(writer())
        await asyncio.wait_for(writer_attempting_lock.wait(), timeout=2)
        wait_event = await _wait_for_postgres_lock(
            seed.factory,
            writer_application_name,
        )
        assert writer_task.done() is False
        assert wait_event in {"transactionid", "tuple"}

        release_purge.set()
        fence = await asyncio.wait_for(purge_task, timeout=5)
        with pytest.raises(AccountPrivateLifecycleClosed):
            await asyncio.wait_for(writer_task, timeout=5)

        assert fence.generation == 2
        async with seed.factory() as session:
            assert await session.get(ProjectRow, new_project_id) is None
            assert await session.get(ProjectMembershipRow, new_membership_id) is None
    finally:
        release_purge.set()
        for task in (purge_task, writer_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (purge_task, writer_task) if task is not None),
            return_exceptions=True,
        )
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_membership_reactivation_advances_generation_and_stales_purge_fence(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    lifecycle = AccountPrivateLifecycle()
    try:
        async with seed.factory() as session, session.begin():
            locked = await lifecycle.lock_stable_scope_for_purge(
                session,
                seed.owner_a.user_id,
            )
            purge_fence = await lifecycle.begin_purge_after_memberships(
                session,
                locked,
                effective_at=datetime.now(UTC),
            )

        async with seed.factory() as session, session.begin():
            await session.scalar(select(ProjectRow.id).where(ProjectRow.id == seed.owner_a.project_id).with_for_update(of=ProjectRow))
            await session.scalar(select(ProjectMembershipRow.id).where(ProjectMembershipRow.id == seed.owner_a.membership_id).with_for_update(of=ProjectMembershipRow))
            reactivated = await lifecycle.reactivate_after_membership(
                session,
                seed.owner_a.user_id,
            )

        assert purge_fence.generation == 2
        assert reactivated.generation == 3
        assert reactivated.generation != purge_fence.generation
        async with seed.factory() as session:
            owner = await session.get(UserRow, str(seed.owner_a.user_id))
            assert owner is not None
            assert owner.private_retention_state == "active"
            assert owner.private_retention_generation == 3
            assert owner.private_retention_effective_at is None
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_pending_account_lifecycle_blocks_run_admission_before_run_or_job(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
            owner = await session.scalar(select(UserRow).where(UserRow.id == str(seed.owner_a.user_id)).with_for_update(of=UserRow))
            assert owner is not None
            owner.private_retention_state = "pending_deletion"
            owner.private_retention_generation += 1
            owner.private_retention_effective_at = datetime.now(UTC)

        with pytest.raises(PrivateWorkForbidden):
            await PrivateRunAdmissionService(seed.factory).admit(
                seed.owner_a,
                thread_id,
                PrivateRunCreate(run_id=run_id),
            )

        async with seed.factory() as session:
            run_count = await session.scalar(select(func.count()).select_from(RunRow).where(RunRow.run_id == run_id))
            job_count = await session.scalar(select(func.count()).select_from(JobRow).where(JobRow.run_id == run_id))
        assert run_count == 0
        assert job_count == 0
    finally:
        await seed.engine.dispose()
