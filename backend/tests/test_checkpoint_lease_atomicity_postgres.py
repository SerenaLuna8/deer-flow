from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from support.private_thread_seed import seed_private_thread_database

from app.private_work.checkpoint_state import checkpoint_config
from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.run_admission import PrivateRunAdmissionService
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.thread_repository import (
    PrivateThreadRepository,
    ThreadAgentRef,
)
from app.reliability.execution import PrivateRunExecutionBoundary
from app.reliability.run_execution.delegation_ledger_settlement import (
    settle_run_delegation_ledger_cancelled,
)
from deerflow.persistence.jobs import sql as job_sql
from deerflow.persistence.jobs.model import WorkerNodeRow
from deerflow.persistence.jobs.sql import JobClaim, JobRepository
from deerflow.sandbox.sandbox import AuthorizationRevoked


def _checkpointer_url(database_url: str) -> str:
    return (
        sa.engine.make_url(database_url)
        .set(
            drivername="postgresql",
        )
        .render_as_string(hide_password=False)
    )


async def _running_claim(seed, *, thread_id: str, run_id: str) -> tuple[JobClaim, uuid.UUID]:
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
    admitted = await PrivateRunAdmissionService(seed.factory).admit(
        seed.owner_a,
        thread_id,
        PrivateRunCreate(
            run_id=run_id,
            kwargs={"input": {"messages": []}},
        ),
    )
    worker_id = uuid.uuid4()
    async with seed.factory() as session, session.begin():
        session.add(
            WorkerNodeRow(
                id=worker_id,
                version="checkpoint-lease-test",
                capabilities_json=["private_run"],
                max_concurrent_jobs=1,
            )
        )
    async with seed.factory() as session, session.begin():
        jobs = JobRepository(session)
        claim = await jobs.claim_next(
            worker_id=worker_id,
            capabilities=frozenset({"private_run"}),
            lease_seconds=300,
        )
        assert claim is not None
        assert claim.job_id == admitted.job.job_id
        assert await jobs.mark_running(
            claim.job_id,
            lease_token=claim.lease_token,
        )
        await PrivateRunRepository(session).begin_execution(
            scope=seed.owner_a_scope,
            run_id=run_id,
            job_id=claim.job_id,
            lease_token=claim.lease_token,
            origin_trace_id=claim.origin_trace_id,
        )
    return claim, worker_id


@pytest.mark.postgres
@pytest.mark.anyio
async def test_immediate_run_claim_uses_database_clock_when_application_clock_is_ahead(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = f"checkpoint-clock-{uuid.uuid4().hex}"
    run_id = f"checkpoint-clock-run-{uuid.uuid4().hex}"

    class AheadDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz or UTC) + timedelta(minutes=5)

    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
        with monkeypatch.context() as clock_patch:
            clock_patch.setattr(job_sql, "datetime", AheadDateTime)
            admitted = await PrivateRunAdmissionService(seed.factory).admit(
                seed.owner_a,
                thread_id,
                PrivateRunCreate(
                    run_id=run_id,
                    kwargs={"input": {"messages": []}},
                ),
            )

        worker_id = uuid.uuid4()
        async with seed.factory() as session, session.begin():
            session.add(
                WorkerNodeRow(
                    id=worker_id,
                    version="checkpoint-clock-test",
                    capabilities_json=["private_run"],
                    max_concurrent_jobs=1,
                )
            )
        async with seed.factory() as session, session.begin():
            claim = await JobRepository(session).claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=300,
            )
        assert claim is not None
        assert claim.job_id == admitted.job.job_id
    finally:
        await seed.engine.dispose()


async def _expire_lease_on_raw_connection(
    connection: AsyncConnection,
    *,
    claim: JobClaim,
) -> None:
    await connection.execute(
        """UPDATE jobs
           SET lease_expires_at = clock_timestamp() - interval '1 second'
           WHERE id = %s""",
        (claim.job_id,),
    )
    await connection.execute(
        """UPDATE runs
           SET execution_lease_expires_at =
                   clock_timestamp() - interval '1 second'
           WHERE run_id = %s AND job_id = %s""",
        (claim.run_id, claim.job_id),
    )


async def _write_checkpoint(
    saver,
    *,
    operation: str,
    thread_id: str,
    cancel_settlement: bool = False,
) -> None:
    config = checkpoint_config(thread_id)
    if operation == "aput":
        method = saver.aput_cancel_settlement if cancel_settlement else saver.aput
        await method(
            config,
            empty_checkpoint(),
            {"source": "input", "step": -1, "parents": {}},
            {},
        )
        return
    method = saver.aput_writes_cancel_settlement if cancel_settlement else saver.aput_writes
    await method(
        {
            "configurable": {
                **config["configurable"],
                "checkpoint_id": "pending-checkpoint",
            }
        },
        (("messages", "checkpoint write"),),
        "task-1",
    )


async def _checkpoint_row_count(seed, *, operation: str, thread_id: str) -> int:
    statement = "SELECT count(*) FROM checkpoints WHERE thread_id = :thread_id" if operation == "aput" else "SELECT count(*) FROM checkpoint_writes WHERE thread_id = :thread_id"
    async with seed.factory() as session:
        count = await session.scalar(
            sa.text(statement),
            {"thread_id": thread_id},
        )
    assert isinstance(count, int)
    return count


@pytest.mark.postgres
@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["aput", "aput_writes"])
async def test_checkpoint_write_rolls_back_when_lease_expires_after_precheck(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = f"checkpoint-lease-{operation}-{uuid.uuid4().hex}"
    run_id = f"checkpoint-lease-run-{uuid.uuid4().hex}"
    claim, worker_id = await _running_claim(
        seed,
        thread_id=thread_id,
        run_id=run_id,
    )
    original = getattr(AsyncPostgresSaver, operation)
    injected = False

    async def expire_after_precheck(self, *args, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            assert isinstance(self.conn, AsyncConnection)
            await _expire_lease_on_raw_connection(
                self.conn,
                claim=claim,
            )
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(
        AsyncPostgresSaver,
        operation,
        expire_after_precheck,
    )
    try:
        async with AsyncPostgresSaver.from_conn_string(_checkpointer_url(migrated_postgres_database_url)) as raw:
            await raw.setup()
            saver = ProjectScopedCheckpointer(raw, seed.factory).for_context(seed.owner_a)
            saver.set_authorization_boundary(
                PrivateRunExecutionBoundary(
                    seed.factory,
                    context=seed.owner_a,
                    claim=claim,
                    expected_worker_id=worker_id,
                )
            )

            with pytest.raises(AuthorizationRevoked):
                await _write_checkpoint(
                    saver,
                    operation=operation,
                    thread_id=thread_id,
                )

        assert (
            await _checkpoint_row_count(
                seed,
                operation=operation,
                thread_id=thread_id,
            )
            == 0
        )
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["aput", "aput_writes"])
async def test_only_explicit_cancel_settlement_mode_can_write_after_ordinary_cancel(
    migrated_postgres_database_url: str,
    operation: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = f"checkpoint-cancel-{operation}-{uuid.uuid4().hex}"
    run_id = f"checkpoint-cancel-run-{uuid.uuid4().hex}"
    claim, worker_id = await _running_claim(
        seed,
        thread_id=thread_id,
        run_id=run_id,
    )
    try:
        async with seed.factory() as session, session.begin():
            await session.execute(
                sa.text(
                    """UPDATE jobs
                       SET cancel_requested_at = clock_timestamp(),
                           cancel_reason = 'user_requested'
                       WHERE id = :job_id"""
                ),
                {"job_id": claim.job_id},
            )

        async with AsyncPostgresSaver.from_conn_string(_checkpointer_url(migrated_postgres_database_url)) as raw:
            await raw.setup()
            saver = ProjectScopedCheckpointer(raw, seed.factory).for_context(seed.owner_a)
            saver.set_authorization_boundary(
                PrivateRunExecutionBoundary(
                    seed.factory,
                    context=seed.owner_a,
                    claim=claim,
                    expected_worker_id=worker_id,
                )
            )
            with pytest.raises(AuthorizationRevoked):
                await _write_checkpoint(
                    saver,
                    operation=operation,
                    thread_id=thread_id,
                )
            await _write_checkpoint(
                saver,
                operation=operation,
                thread_id=thread_id,
                cancel_settlement=True,
            )

        assert (
            await _checkpoint_row_count(
                seed,
                operation=operation,
                thread_id=thread_id,
            )
            == 1
        )
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_ordinary_cancel_terminalizes_exact_run_delegation_ledger(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = f"checkpoint-cancel-ledger-{uuid.uuid4().hex}"
    run_id = f"checkpoint-cancel-ledger-run-{uuid.uuid4().hex}"
    claim, worker_id = await _running_claim(
        seed,
        thread_id=thread_id,
        run_id=run_id,
    )
    active_entry = {
        "id": "task-call",
        "occurrence": 2,
        "project_id": str(seed.owner_a.project_id),
        "owner_user_id": str(seed.owner_a.user_id),
        "run_id": run_id,
        "description": "research",
        "subagent_type": "researcher",
        "status": "in_progress",
        "created_at": "2026-08-28T00:00:00Z",
    }
    terminal_entry = {
        **active_entry,
        "id": "task-terminal",
        "occurrence": 1,
        "status": "completed",
    }
    try:
        async with AsyncPostgresSaver.from_conn_string(_checkpointer_url(migrated_postgres_database_url)) as raw:
            await raw.setup()
            saver = ProjectScopedCheckpointer(raw, seed.factory).for_context(seed.owner_a)
            saver.set_authorization_boundary(
                PrivateRunExecutionBoundary(
                    seed.factory,
                    context=seed.owner_a,
                    claim=claim,
                    expected_worker_id=worker_id,
                )
            )
            version = saver.get_next_version(None, "delegations")
            checkpoint = empty_checkpoint()
            checkpoint["channel_values"] = {"delegations": [active_entry, terminal_entry]}
            checkpoint["channel_versions"] = {"delegations": version}
            await saver.aput(
                checkpoint_config(thread_id),
                checkpoint,
                {"source": "input", "step": -1, "parents": {}},
                {"delegations": version},
            )

            async with seed.factory() as session, session.begin():
                await session.execute(
                    sa.text(
                        """UPDATE jobs
                           SET cancel_requested_at = clock_timestamp(),
                               cancel_reason = 'user_requested'
                           WHERE id = :job_id"""
                    ),
                    {"job_id": claim.job_id},
                )

            assert await settle_run_delegation_ledger_cancelled(
                saver,
                thread_id=thread_id,
                project_id=str(seed.owner_a.project_id),
                owner_user_id=str(seed.owner_a.user_id),
                run_id=run_id,
            )
            latest = await raw.aget_tuple(checkpoint_config(thread_id))
            assert latest is not None
            assert latest.checkpoint["channel_values"]["delegations"] == [
                {**active_entry, "status": "cancelled"},
                terminal_entry,
            ]
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["aput", "aput_writes"])
async def test_cancel_settlement_mode_still_rejects_authorization_revocation(
    migrated_postgres_database_url: str,
    operation: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = f"checkpoint-revoked-{operation}-{uuid.uuid4().hex}"
    run_id = f"checkpoint-revoked-run-{uuid.uuid4().hex}"
    claim, worker_id = await _running_claim(
        seed,
        thread_id=thread_id,
        run_id=run_id,
    )
    try:
        async with seed.factory() as session, session.begin():
            await session.execute(
                sa.text(
                    """UPDATE runs
                       SET authorization_cancel_requested_at = clock_timestamp(),
                           authorization_cancel_reason = 'authorization_revoked'
                       WHERE run_id = :run_id AND job_id = :job_id"""
                ),
                {"run_id": run_id, "job_id": claim.job_id},
            )

        async with AsyncPostgresSaver.from_conn_string(_checkpointer_url(migrated_postgres_database_url)) as raw:
            await raw.setup()
            saver = ProjectScopedCheckpointer(raw, seed.factory).for_context(seed.owner_a)
            saver.set_authorization_boundary(
                PrivateRunExecutionBoundary(
                    seed.factory,
                    context=seed.owner_a,
                    claim=claim,
                    expected_worker_id=worker_id,
                )
            )
            with pytest.raises(AuthorizationRevoked):
                await _write_checkpoint(
                    saver,
                    operation=operation,
                    thread_id=thread_id,
                    cancel_settlement=True,
                )

        assert (
            await _checkpoint_row_count(
                seed,
                operation=operation,
                thread_id=thread_id,
            )
            == 0
        )
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["aput", "aput_writes"])
async def test_checkpoint_write_rejects_an_already_expired_exact_lease(
    migrated_postgres_database_url: str,
    operation: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = f"checkpoint-expired-{operation}-{uuid.uuid4().hex}"
    run_id = f"checkpoint-expired-run-{uuid.uuid4().hex}"
    claim, worker_id = await _running_claim(
        seed,
        thread_id=thread_id,
        run_id=run_id,
    )
    try:
        async with seed.factory() as session, session.begin():
            await session.execute(
                sa.text(
                    """UPDATE jobs
                       SET lease_expires_at = clock_timestamp() - interval '1 second'
                       WHERE id = :job_id"""
                ),
                {"job_id": claim.job_id},
            )
            await session.execute(
                sa.text(
                    """UPDATE runs
                       SET execution_lease_expires_at =
                               clock_timestamp() - interval '1 second'
                       WHERE run_id = :run_id AND job_id = :job_id"""
                ),
                {"run_id": run_id, "job_id": claim.job_id},
            )

        async with AsyncPostgresSaver.from_conn_string(_checkpointer_url(migrated_postgres_database_url)) as raw:
            await raw.setup()
            saver = ProjectScopedCheckpointer(raw, seed.factory).for_context(seed.owner_a)
            saver.set_authorization_boundary(
                PrivateRunExecutionBoundary(
                    seed.factory,
                    context=seed.owner_a,
                    claim=claim,
                    expected_worker_id=worker_id,
                )
            )
            with pytest.raises(AuthorizationRevoked):
                await _write_checkpoint(
                    saver,
                    operation=operation,
                    thread_id=thread_id,
                )

        assert (
            await _checkpoint_row_count(
                seed,
                operation=operation,
                thread_id=thread_id,
            )
            == 0
        )
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["aput", "aput_writes"])
async def test_checkpoint_write_commits_under_the_current_exact_lease(
    migrated_postgres_database_url: str,
    operation: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = f"checkpoint-active-{operation}-{uuid.uuid4().hex}"
    run_id = f"checkpoint-active-run-{uuid.uuid4().hex}"
    claim, worker_id = await _running_claim(
        seed,
        thread_id=thread_id,
        run_id=run_id,
    )
    try:
        async with AsyncPostgresSaver.from_conn_string(_checkpointer_url(migrated_postgres_database_url)) as raw:
            await raw.setup()
            saver = ProjectScopedCheckpointer(raw, seed.factory).for_context(seed.owner_a)
            saver.set_authorization_boundary(
                PrivateRunExecutionBoundary(
                    seed.factory,
                    context=seed.owner_a,
                    claim=claim,
                    expected_worker_id=worker_id,
                )
            )
            await _write_checkpoint(
                saver,
                operation=operation,
                thread_id=thread_id,
            )

        assert (
            await _checkpoint_row_count(
                seed,
                operation=operation,
                thread_id=thread_id,
            )
            == 1
        )
    finally:
        await seed.engine.dispose()
