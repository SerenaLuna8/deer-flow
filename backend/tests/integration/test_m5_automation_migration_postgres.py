from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from postgres_utils import temporary_postgres_database
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from support.m5_automation import (
    M5LegacyMigrationDatabase,
    isolated_m5_legacy_migration_database,
)

from deerflow.persistence.bootstrap import bootstrap_schema
from scripts import migrate_automations
from scripts.migrate_automations import (
    AutomationMigrationError,
    normalize_owner_map,
    run_automation_migration,
)

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


@pytest_asyncio.fixture()
async def m5_legacy_database(
    postgres_admin_url: str,
    tmp_path: Path,
):
    async with isolated_m5_legacy_migration_database(
        postgres_admin_url,
        tmp_path / "m5-backup-proof",
    ) as database:
        yield database


@pytest_asyncio.fixture()
async def m5_fresh_database_url(postgres_admin_url: str):
    async with temporary_postgres_database(postgres_admin_url) as url:
        database = make_url(url).database or ""
        assert database.startswith("deerflow_test_")
        yield url


async def _automation_control_state(
    database: M5LegacyMigrationDatabase,
) -> tuple[int, int, tuple[object, ...] | None]:
    async with database.engine.connect() as connection:
        migration_runs_table = await connection.scalar(text("SELECT to_regclass('automation_migration_runs')"))
        migration_runs = await connection.scalar(text("SELECT count(*) FROM automation_migration_runs")) if migration_runs_table is not None else 0
        ledger_table = await connection.scalar(text("SELECT to_regclass('automation_migration_ledger')"))
        ledgers = await connection.scalar(text("SELECT count(*) FROM automation_migration_ledger")) if ledger_table is not None else 0
        marker_table = await connection.scalar(text("SELECT to_regclass('automation_cutover_state')"))
        marker = None
        if marker_table is not None:
            marker_row = (
                await connection.execute(
                    text(
                        """SELECT stage,migration_run_id,
                                  empty_domain_probe_complete,
                                  final_schema_probe_complete,cutover_at
                        FROM automation_cutover_state WHERE id=1"""
                    )
                )
            ).one_or_none()
            marker = tuple(marker_row) if marker_row is not None else None
    return int(migration_runs or 0), int(ledgers or 0), marker


async def _stage_migration(database: M5LegacyMigrationDatabase) -> None:
    await database.upgrade("0012_project_automation_expand")
    targets = normalize_owner_map(database.owner_map)
    inventory = await migrate_automations._collect_inventory(database.engine)
    async with database.engine.connect() as connection:
        plan = await migrate_automations._preflight(
            connection,
            inventory,
            targets,
        )
    await migrate_automations._execute_staging(database.engine, plan=plan)


async def _wait_for_pending_source_lock(
    database_url: str,
    *,
    timeout: float = 10,
) -> None:
    engine = create_async_engine(database_url)
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        while asyncio.get_running_loop().time() < deadline:
            async with engine.connect() as connection:
                waiting = await connection.scalar(
                    text(
                        """SELECT EXISTS (
                            SELECT 1 FROM pg_locks
                            WHERE relation='scheduled_tasks'::regclass
                              AND mode='ShareRowExclusiveLock'
                              AND granted=false
                              AND pid <> pg_backend_pid()
                        )"""
                    )
                )
            if waiting is True:
                return
            await asyncio.sleep(0.02)
    finally:
        await engine.dispose()
    raise AssertionError("M5 execute did not reach the protected source lock")


@pytest.mark.asyncio
async def test_legacy_migration_reaches_head_with_receipts_and_is_idempotent(
    m5_legacy_database: M5LegacyMigrationDatabase,
) -> None:
    before_dry_run = await m5_legacy_database.snapshot()
    assert before_dry_run.control_relations == {
        "automation_migration_runs": False,
        "automation_migration_ledger": False,
        "automation_cutover_state": False,
    }
    dry_run = await run_automation_migration(
        m5_legacy_database.url,
        owner_map=m5_legacy_database.owner_map,
        backup_dir=m5_legacy_database.backup_dir,
        execute=False,
    )
    assert dry_run.counts == m5_legacy_database.expected_counts
    assert dry_run.cutover_complete is False
    assert dry_run.noop is False
    assert await m5_legacy_database.snapshot() == before_dry_run

    executed = await run_automation_migration(
        m5_legacy_database.url,
        owner_map=m5_legacy_database.owner_map,
        backup_dir=m5_legacy_database.backup_dir,
        execute=True,
    )
    assert executed.counts == m5_legacy_database.expected_counts
    assert executed.cutover_complete is True
    assert executed.empty_install is False
    assert executed.noop is False
    assert await m5_legacy_database.current_revision() == ("0013_project_automation_finalize")

    async with m5_legacy_database.engine.connect() as connection:
        marker = (
            await connection.execute(
                text(
                    """SELECT stage,migration_run_id,
                              empty_domain_probe_complete,
                              final_schema_probe_complete,cutover_at
                    FROM automation_cutover_state WHERE id=1"""
                )
            )
        ).one()
        ledgers = (
            await connection.execute(
                text(
                    """SELECT domain,status,source_row_count,target_row_count,
                              length(source_fingerprint),length(target_digest)
                    FROM automation_migration_ledger ORDER BY domain"""
                )
            )
        ).all()
        final_columns = set(
            (
                await connection.execute(
                    text(
                        """SELECT column_name FROM information_schema.columns
                        WHERE table_schema=current_schema()
                          AND table_name='scheduled_tasks'"""
                    )
                )
            ).scalars()
        )
        constraints = set(
            (
                await connection.execute(
                    text(
                        """SELECT conname FROM pg_constraint
                        WHERE conrelid IN
                          ('scheduled_tasks'::regclass,
                           'scheduled_task_runs'::regclass)"""
                    )
                )
            ).scalars()
        )
    assert marker.stage == "cutover_complete"
    assert marker.migration_run_id is not None
    assert marker.empty_domain_probe_complete is False
    assert marker.final_schema_probe_complete is True
    assert marker.cutover_at is not None
    assert [tuple(row) for row in ledgers] == [
        ("scheduled_task_runs", "complete", 2, 2, 64, 64),
        ("scheduled_tasks", "complete", 2, 2, 64, 64),
    ]
    assert "user_id" not in final_columns
    assert {
        "uq_scheduled_tasks_private_scope",
        "fk_scheduled_tasks_project_membership",
        "uq_scheduled_task_runs_occurrence",
        "fk_scheduled_task_runs_task",
    } <= constraints

    repeated = await run_automation_migration(
        m5_legacy_database.url,
        owner_map=m5_legacy_database.owner_map,
        backup_dir=m5_legacy_database.backup_dir,
        execute=True,
    )
    assert repeated.counts == m5_legacy_database.expected_counts
    assert repeated.cutover_complete is True
    assert repeated.noop is True


@pytest.mark.asyncio
async def test_fresh_install_bootstraps_final_empty_domain_without_owner_map(
    m5_fresh_database_url: str,
    tmp_path: Path,
) -> None:
    engine = create_async_engine(m5_fresh_database_url)
    try:
        await bootstrap_schema(engine)
        async with engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            marker = (
                await connection.execute(
                    text(
                        """SELECT stage,migration_run_id,
                                  empty_domain_probe_complete,
                                  final_schema_probe_complete,cutover_at
                        FROM automation_cutover_state WHERE id=1"""
                    )
                )
            ).one()
            counts = (
                await connection.execute(
                    text(
                        """SELECT
                          (SELECT count(*) FROM scheduled_tasks),
                          (SELECT count(*) FROM scheduled_task_runs),
                          (SELECT count(*) FROM automation_migration_runs),
                          (SELECT count(*) FROM automation_migration_ledger)"""
                    )
                )
            ).one()
        assert revision == "0013_project_automation_finalize"
        assert tuple(marker)[:4] == ("cutover_complete", None, True, True)
        assert marker.cutover_at is not None
        assert tuple(counts) == (0, 0, 0, 0)

        report = await run_automation_migration(
            m5_fresh_database_url,
            owner_map={},
            backup_dir=tmp_path / "no-owner-map-needed",
            execute=True,
        )
        assert report.cutover_complete is True
        assert report.empty_install is True
        assert report.noop is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_invalid_missing_conflicting_maps_and_relations_fail_before_expand(
    m5_legacy_database: M5LegacyMigrationDatabase,
) -> None:
    seed = m5_legacy_database.seed
    canonical_item = next(iter(m5_legacy_database.owner_map.values()))
    initial = await m5_legacy_database.snapshot()
    assert initial.control_relations == {
        "automation_migration_runs": False,
        "automation_migration_ledger": False,
        "automation_cutover_state": False,
    }
    invalid_maps = (
        ("missing", {}),
        (
            "extra_conflicting_owner",
            {
                **m5_legacy_database.owner_map,
                str(uuid.uuid4()): canonical_item,
            },
        ),
        (
            "reuse_thread_cross_scope",
            {
                str(seed.owner_a.user_id): {
                    "project_id": str(seed.project_b_owner_a.project_id),
                    "fresh_thread_agent": {
                        "asset_id": str(seed.project_b_agent_id),
                        "scope": "project",
                    },
                },
            },
        ),
    )
    for case, owner_map in invalid_maps:
        before_failure = await m5_legacy_database.snapshot()
        with pytest.raises(AutomationMigrationError):
            await run_automation_migration(
                m5_legacy_database.url,
                owner_map=owner_map,
                backup_dir=m5_legacy_database.backup_dir,
                execute=True,
            )
        assert await m5_legacy_database.snapshot() == before_failure, case

    before_parse_failure = await m5_legacy_database.snapshot()
    with pytest.raises(AutomationMigrationError, match="owner map is invalid"):
        await run_automation_migration(
            m5_legacy_database.url,
            owner_map={"not-a-uuid": canonical_item},
            backup_dir=m5_legacy_database.backup_dir,
            execute=True,
        )
    assert await m5_legacy_database.snapshot() == before_parse_failure

    async with m5_legacy_database.engine.begin() as connection:
        await connection.execute(
            text(
                """UPDATE scheduled_task_runs SET run_id='unmapped-run',
                                               status='running'
                WHERE id='m5-legacy-success'"""
            )
        )
    before_relation_failure = await m5_legacy_database.snapshot()
    with pytest.raises(AutomationMigrationError, match="orphan automation run"):
        await run_automation_migration(
            m5_legacy_database.url,
            owner_map=m5_legacy_database.owner_map,
            backup_dir=m5_legacy_database.backup_dir,
            execute=True,
        )
    assert await m5_legacy_database.snapshot() == before_relation_failure


@pytest.mark.asyncio
async def test_legacy_snapshot_detects_source_mutation_and_restoration(
    m5_legacy_database: M5LegacyMigrationDatabase,
) -> None:
    before = await m5_legacy_database.snapshot()
    async with m5_legacy_database.engine.begin() as connection:
        await connection.execute(
            text(
                """UPDATE scheduled_tasks SET title='snapshot mutation probe'
                WHERE id='m5-legacy-fresh'"""
            )
        )
    assert await m5_legacy_database.snapshot() != before

    async with m5_legacy_database.engine.begin() as connection:
        await connection.execute(
            text(
                """UPDATE scheduled_tasks SET title='Legacy fresh'
                WHERE id='m5-legacy-fresh'"""
            )
        )
    assert await m5_legacy_database.snapshot() == before


@pytest.mark.asyncio
async def test_execute_rejects_source_drift_seen_after_real_lock_wait(
    m5_legacy_database: M5LegacyMigrationDatabase,
) -> None:
    await m5_legacy_database.upgrade("0012_project_automation_expand")
    reviewed = await run_automation_migration(
        m5_legacy_database.url,
        owner_map=m5_legacy_database.owner_map,
        backup_dir=m5_legacy_database.backup_dir,
        execute=False,
    )
    assert reviewed.counts == m5_legacy_database.expected_counts
    assert await _automation_control_state(m5_legacy_database) == (0, 0, None)

    blocker_engine = create_async_engine(m5_legacy_database.url)
    blocker = await blocker_engine.connect()
    transaction = await blocker.begin()
    execute_task: asyncio.Task | None = None
    try:
        await blocker.execute(text("LOCK TABLE scheduled_tasks IN ROW EXCLUSIVE MODE"))
        execute_task = asyncio.create_task(
            run_automation_migration(
                m5_legacy_database.url,
                owner_map=m5_legacy_database.owner_map,
                backup_dir=m5_legacy_database.backup_dir,
                execute=True,
            )
        )
        await _wait_for_pending_source_lock(m5_legacy_database.url)
        await blocker.execute(
            text(
                """UPDATE scheduled_tasks SET title='drift after reviewed preflight'
                WHERE id='m5-legacy-fresh'"""
            )
        )
        await transaction.commit()
        with pytest.raises(
            AutomationMigrationError,
            match="legacy source fingerprint changed",
        ):
            await asyncio.wait_for(execute_task, timeout=10)
    finally:
        if transaction.is_active:
            await transaction.rollback()
        if execute_task is not None and not execute_task.done():
            execute_task.cancel()
            await asyncio.gather(execute_task, return_exceptions=True)
        await blocker.close()
        await blocker_engine.dispose()

    assert await m5_legacy_database.current_revision() == ("0012_project_automation_expand")
    assert await _automation_control_state(m5_legacy_database) == (0, 0, None)


@pytest.mark.asyncio
async def test_staged_map_and_ledger_digest_conflicts_never_complete_cutover(
    m5_legacy_database: M5LegacyMigrationDatabase,
) -> None:
    await _stage_migration(m5_legacy_database)
    seed = m5_legacy_database.seed
    changed_map = {
        str(seed.owner_a.user_id): {
            "project_id": str(seed.owner_a.project_id),
            "fresh_thread_agent": {
                "asset_id": str(seed.system_agent_id),
                "scope": "system",
            },
        }
    }
    with pytest.raises(AutomationMigrationError):
        await run_automation_migration(
            m5_legacy_database.url,
            owner_map=changed_map,
            backup_dir=m5_legacy_database.backup_dir,
            execute=True,
        )
    runs, ledgers, marker = await _automation_control_state(m5_legacy_database)
    assert (runs, ledgers) == (1, 2)
    assert marker is not None
    assert marker[0] == "migration_ready"
    assert marker[3:] == (False, None)

    async with m5_legacy_database.engine.begin() as connection:
        await connection.execute(
            text(
                """UPDATE automation_migration_ledger
                SET target_digest=:digest
                WHERE domain='scheduled_tasks'"""
            ),
            {"digest": "f" * 64},
        )
    with pytest.raises(AutomationMigrationError, match="ledger conflicts"):
        await run_automation_migration(
            m5_legacy_database.url,
            owner_map=m5_legacy_database.owner_map,
            backup_dir=m5_legacy_database.backup_dir,
            execute=True,
        )
    _runs, _ledgers, marker = await _automation_control_state(m5_legacy_database)
    assert marker is not None
    assert marker[0] == "migration_ready"
    assert marker[3:] == (False, None)
    assert await m5_legacy_database.current_revision() == ("0012_project_automation_expand")


@pytest.mark.asyncio
async def test_finalize_relation_probe_fails_before_ddl_and_can_retry(
    m5_legacy_database: M5LegacyMigrationDatabase,
) -> None:
    await _stage_migration(m5_legacy_database)
    async with m5_legacy_database.engine.begin() as connection:
        await connection.execute(
            text(
                """UPDATE threads_meta SET owner_user_id=:other_owner
                WHERE thread_id=:thread"""
            ),
            {
                "other_owner": str(m5_legacy_database.seed.owner_b.user_id),
                "thread": m5_legacy_database.reuse_thread_id,
            },
        )
    before = await m5_legacy_database.schema_fingerprint()

    with pytest.raises(RuntimeError, match="relation probe failed"):
        await m5_legacy_database.upgrade("head")

    assert await m5_legacy_database.current_revision() == ("0012_project_automation_expand")
    assert await m5_legacy_database.schema_fingerprint() == before
    async with m5_legacy_database.engine.connect() as connection:
        columns = set(
            (
                await connection.execute(
                    text(
                        """SELECT column_name FROM information_schema.columns
                        WHERE table_schema=current_schema()
                          AND table_name='scheduled_tasks'"""
                    )
                )
            ).scalars()
        )
    assert "user_id" in columns
    _runs, _ledgers, marker = await _automation_control_state(m5_legacy_database)
    assert marker is not None
    assert marker[0] == "migration_ready"
    assert marker[3:] == (False, None)

    async with m5_legacy_database.engine.begin() as connection:
        await connection.execute(
            text(
                """UPDATE threads_meta SET owner_user_id=:owner
                WHERE thread_id=:thread"""
            ),
            {
                "owner": str(m5_legacy_database.seed.owner_a.user_id),
                "thread": m5_legacy_database.reuse_thread_id,
            },
        )
    await m5_legacy_database.upgrade("head")
    resumed = await run_automation_migration(
        m5_legacy_database.url,
        owner_map=m5_legacy_database.owner_map,
        backup_dir=m5_legacy_database.backup_dir,
        execute=True,
    )
    assert resumed.cutover_complete is True
    assert await m5_legacy_database.current_revision() == ("0013_project_automation_finalize")
