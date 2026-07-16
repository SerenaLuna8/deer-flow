from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from deerflow.persistence.bootstrap import _get_alembic_config, bootstrap_schema
from scripts import migrate_automations
from scripts.migrate_automations import (
    AutomationInventory,
    AutomationMigrationError,
    load_owner_map,
    normalize_owner_map,
    render_inventory,
    render_report,
    run_automation_migration,
)


@dataclass(frozen=True)
class _LegacyScenario:
    seed: M4ThreadSeed
    owner_map: dict[str, object]
    backup_dir: Path
    private_title: str
    private_prompt: str
    reuse_thread_id: str
    history_thread_id: str
    history_run_id: str


def _owner_map_item(seed: M4ThreadSeed, *, project_id: uuid.UUID | None = None, agent_id: uuid.UUID | None = None) -> dict[str, object]:
    return {
        "project_id": str(project_id or seed.owner_a.project_id),
        "fresh_thread_agent": {
            "asset_id": str(agent_id or seed.project_agent_id),
            "scope": "project",
        },
    }


def test_owner_map_requires_canonical_uuid_and_fresh_agent_without_echoing_values(tmp_path: Path) -> None:
    path = tmp_path / "owners.json"
    path.write_text(
        json.dumps(
            {
                "private@example.invalid": {
                    "project_id": "not-a-project",
                    "fresh_thread_agent": {"asset_id": "not-an-agent", "scope": "project"},
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AutomationMigrationError, match="owner map is invalid") as caught:
        load_owner_map(path)

    rendered = str(caught.value)
    assert "private@example.invalid" not in rendered
    assert "not-a-project" not in rendered
    assert "not-an-agent" not in rendered


def test_fresh_task_requires_explicit_agent_map() -> None:
    owner = str(uuid.uuid4())
    project = str(uuid.uuid4())

    with pytest.raises(AutomationMigrationError, match="fresh thread agent mapping is required"):
        normalize_owner_map({owner: {"project_id": project}})


def test_inventory_and_report_do_not_include_private_values_or_ids() -> None:
    owner = str(uuid.uuid4())
    thread_id = f"private-thread-{uuid.uuid4()}"
    inventory = AutomationInventory(
        source_fingerprint="a" * 64,
        task_rows=(
            {
                "id": "private-task-id",
                "user_id": owner,
                "thread_id": thread_id,
                "title": "private title",
                "prompt": "private prompt",
                "status": "enabled",
            },
        ),
        run_rows=(),
    )

    rendered_inventory = render_inventory(inventory)
    report = migrate_automations.AutomationMigrationReport(
        mode="dry-run",
        counts={"scheduled_tasks": 1, "scheduled_task_runs": 0},
        status_counts={"tasks:enabled": 1},
        source_key_hash="a" * 12,
        empty_install=False,
    )
    rendered_report = render_report(report)
    combined = f"{rendered_inventory}\n{rendered_report}\n{inventory!r}\n{report!r}"

    for private_value in (owner, thread_id, "private-task-id", "private title", "private prompt"):
        assert private_value not in combined
    assert json.loads(rendered_inventory) == {
        "counts": {"scheduled_task_runs": 0, "scheduled_tasks": 1},
        "source_key_hash": "a" * 12,
        "status_counts": {"tasks:enabled": 1},
    }


async def _downgrade_to_0011(url: str) -> None:
    engine = create_async_engine(url)
    try:
        config = _get_alembic_config(engine)
        await asyncio.to_thread(command.downgrade, config, "0011_private_artifact_tombstone")
    finally:
        await engine.dispose()


async def _seed_thread_and_run(seed: M4ThreadSeed, *, thread_id: str, run_id: str | None = None) -> None:
    async with seed.engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO threads_meta
                (thread_id,assistant_id,owner_user_id,display_name,status,metadata_json,
                 project_id,agent_asset_id,agent_scope,checkpoint_delete_status,version,
                 created_at,updated_at)
                VALUES (:thread_id,NULL,:owner,'Private thread','idle','{}'::jsonb,
                        :project,:agent,'project','not_requested',1,now(),now())"""
            ),
            {
                "thread_id": thread_id,
                "owner": str(seed.owner_a.user_id),
                "project": seed.owner_a.project_id,
                "agent": seed.project_agent_id,
            },
        )
        if run_id is not None:
            await connection.execute(
                text(
                    """INSERT INTO runs
                    (run_id,thread_id,assistant_id,owner_user_id,status,model_name,
                     multitask_strategy,metadata_json,kwargs_json,message_count,
                     total_input_tokens,total_output_tokens,total_tokens,llm_call_count,
                     lead_agent_tokens,subagent_tokens,middleware_tokens,token_usage_by_model,
                     project_id,finalization_status,created_at,updated_at)
                    VALUES (:run_id,:thread_id,NULL,:owner,'success','test-model','reject',
                            '{}'::jsonb,'{}'::jsonb,0,0,0,0,0,0,0,0,'{}'::jsonb,
                            :project,'complete',now(),now())"""
                ),
                {
                    "run_id": run_id,
                    "thread_id": thread_id,
                    "owner": str(seed.owner_a.user_id),
                    "project": seed.owner_a.project_id,
                },
            )


async def _seed_legacy_scenario(database_url: str, tmp_path: Path) -> _LegacyScenario:
    engine = create_async_engine(database_url)
    try:
        await bootstrap_schema(engine)
    finally:
        await engine.dispose()
    seed = await seed_m4_thread_database(database_url)
    reuse_thread_id = f"reuse-{uuid.uuid4().hex}"
    history_thread_id = f"history-{uuid.uuid4().hex}"
    history_run_id = f"run-{uuid.uuid4().hex}"
    await _seed_thread_and_run(seed, thread_id=reuse_thread_id)
    await _seed_thread_and_run(seed, thread_id=history_thread_id, run_id=history_run_id)
    await seed.engine.dispose()
    await _downgrade_to_0011(database_url)

    private_title = "migration title must stay private"
    private_prompt = "migration prompt must stay private"
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO scheduled_tasks
                    (id,user_id,thread_id,context_mode,assistant_id,title,prompt,
                     schedule_type,schedule_spec,timezone,status,overlap_policy,
                     next_run_at,last_run_at,last_run_id,last_thread_id,last_error,
                     lease_owner,lease_expires_at,run_count,created_at,updated_at)
                    VALUES
                    ('legacy-fresh',:owner,NULL,'fresh_thread_per_run','legacy-agent',
                     :title,:prompt,'cron','{"cron":"0 9 * * *"}'::json,'UTC',
                     'enabled','skip',now(),now(),:run_id,:history_thread,NULL,NULL,NULL,2,now(),now()),
                    ('legacy-reuse',:owner,:reuse_thread,'reuse_thread','ignored-agent',
                     'Reuse title','Reuse prompt','once','{}'::json,'UTC',
                     'paused','skip',NULL,NULL,NULL,NULL,NULL,NULL,NULL,0,now(),now())"""
                ),
                {
                    "owner": str(seed.owner_a.user_id),
                    "title": private_title,
                    "prompt": private_prompt,
                    "run_id": history_run_id,
                    "history_thread": history_thread_id,
                    "reuse_thread": reuse_thread_id,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO scheduled_task_runs
                    (id,task_id,thread_id,run_id,scheduled_for,trigger,status,error,
                     started_at,finished_at,created_at)
                    VALUES
                    ('legacy-occurrence-success','legacy-fresh',:history_thread,:run_id,
                     now(),'scheduled','success',NULL,now(),now(),now()),
                    ('legacy-occurrence-skipped','legacy-fresh',:orphan_thread,NULL,
                     now(),'scheduled','skipped','private legacy overlap detail',now(),now(),now())"""
                ),
                {
                    "history_thread": history_thread_id,
                    "run_id": history_run_id,
                    "orphan_thread": f"missing-{uuid.uuid4().hex}",
                },
            )
    finally:
        await engine.dispose()

    backup_dir = tmp_path / "backup-proof"
    owner_map = {str(seed.owner_a.user_id): _owner_map_item(seed)}
    return _LegacyScenario(
        seed=seed,
        owner_map=owner_map,
        backup_dir=backup_dir,
        private_title=private_title,
        private_prompt=private_prompt,
        reuse_thread_id=reuse_thread_id,
        history_thread_id=history_thread_id,
        history_run_id=history_run_id,
    )


def _write_backup_proof(directory: Path) -> None:
    directory.mkdir(parents=True)
    (directory / "operator-restore-proof.txt").write_text(
        "verified external PostgreSQL backup and restore rehearsal",
        encoding="utf-8",
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_legacy_migration_reaches_head_is_redacted_and_idempotent(
    postgres_database_url: str,
    tmp_path: Path,
) -> None:
    scenario = await _seed_legacy_scenario(postgres_database_url, tmp_path)

    dry_run = await run_automation_migration(
        postgres_database_url,
        owner_map=scenario.owner_map,
        backup_dir=scenario.backup_dir,
        execute=False,
    )
    assert dry_run.mode == "dry-run"
    assert dry_run.counts == {"scheduled_tasks": 2, "scheduled_task_runs": 2}
    assert dry_run.cutover_complete is False
    assert dry_run.noop is False
    encoded = render_report(dry_run)
    for private_value in (
        scenario.private_title,
        scenario.private_prompt,
        str(scenario.seed.owner_a.user_id),
        scenario.reuse_thread_id,
        scenario.history_thread_id,
        scenario.history_run_id,
    ):
        assert private_value not in encoded

    engine = create_async_engine(postgres_database_url)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0011_private_artifact_tombstone"
            assert await connection.scalar(text("SELECT to_regclass('automation_migration_runs')")) is None
    finally:
        await engine.dispose()

    with pytest.raises(AutomationMigrationError, match="operator backup proof is required"):
        await run_automation_migration(
            postgres_database_url,
            owner_map=scenario.owner_map,
            backup_dir=scenario.backup_dir,
            execute=True,
        )

    _write_backup_proof(scenario.backup_dir)
    executed = await run_automation_migration(
        postgres_database_url,
        owner_map=scenario.owner_map,
        backup_dir=scenario.backup_dir,
        execute=True,
    )
    assert executed.cutover_complete is True
    assert executed.empty_install is False
    assert executed.noop is False

    engine = create_async_engine(postgres_database_url)
    try:
        async with engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            marker = (
                await connection.execute(
                    text(
                        """SELECT stage,migration_run_id,empty_domain_probe_complete,
                        final_schema_probe_complete,cutover_at
                        FROM automation_cutover_state WHERE id=1"""
                    )
                )
            ).one()
            tasks = (
                (
                    await connection.execute(
                        text(
                            """SELECT id,project_id,owner_user_id,thread_id,context_mode,
                        agent_asset_id,agent_scope,status,version
                        FROM scheduled_tasks ORDER BY id"""
                        )
                    )
                )
                .mappings()
                .all()
            )
            occurrences = (
                (
                    await connection.execute(
                        text(
                            """SELECT id,project_id,owner_user_id,task_id,task_version,
                        occurrence_key,thread_id,run_id,status,launch_attempt_count
                        FROM scheduled_task_runs ORDER BY id"""
                        )
                    )
                )
                .mappings()
                .all()
            )
            ledgers = (
                await connection.execute(
                    text(
                        """SELECT domain,status,source_row_count,target_row_count
                        FROM automation_migration_ledger ORDER BY domain"""
                    )
                )
            ).all()

        assert revision == "0013_project_automation_finalize"
        assert marker.stage == "cutover_complete"
        assert marker.migration_run_id is not None
        assert marker.empty_domain_probe_complete is False
        assert marker.final_schema_probe_complete is True
        assert marker.cutover_at is not None
        assert len(tasks) == 2
        fresh = next(row for row in tasks if row["id"] == "legacy-fresh")
        reuse = next(row for row in tasks if row["id"] == "legacy-reuse")
        assert fresh["project_id"] == scenario.seed.owner_a.project_id
        assert fresh["owner_user_id"] == str(scenario.seed.owner_a.user_id)
        assert fresh["thread_id"] is None
        assert fresh["agent_asset_id"] == scenario.seed.project_agent_id
        assert fresh["agent_scope"] == "project"
        assert fresh["version"] == 1
        assert reuse["thread_id"] == scenario.reuse_thread_id
        assert reuse["agent_asset_id"] == scenario.seed.project_agent_id
        assert reuse["agent_scope"] == "project"
        assert all(len(row["occurrence_key"]) == 64 for row in occurrences)
        successful = next(row for row in occurrences if row["id"] == "legacy-occurrence-success")
        skipped = next(row for row in occurrences if row["id"] == "legacy-occurrence-skipped")
        assert successful["thread_id"] == scenario.history_thread_id
        assert successful["run_id"] == scenario.history_run_id
        assert successful["launch_attempt_count"] == 1
        assert skipped["thread_id"] is None
        assert skipped["run_id"] is None
        assert skipped["launch_attempt_count"] == 0
        assert [(row.domain, row.status, row.source_row_count, row.target_row_count) for row in ledgers] == [
            ("scheduled_task_runs", "complete", 2, 2),
            ("scheduled_tasks", "complete", 2, 2),
        ]
    finally:
        await engine.dispose()

    rerun = await run_automation_migration(
        postgres_database_url,
        owner_map=scenario.owner_map,
        backup_dir=scenario.backup_dir,
        execute=True,
    )
    assert rerun.cutover_complete is True
    assert rerun.noop is True
    assert rerun.counts == {"scheduled_tasks": 2, "scheduled_task_runs": 2}


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected"),
    (
        ("viewer", "mapped project membership is unavailable"),
        ("agent", "fresh Agent is not executable"),
        ("reuse_scope", "reuse Thread scope does not match owner map"),
        ("orphan_run", "orphan automation run"),
        ("orphan_task_history", "orphan automation run"),
        ("status", "unsupported legacy Automation status"),
    ),
)
async def test_invalid_legacy_source_fails_before_expand_ddl(
    postgres_database_url: str,
    tmp_path: Path,
    failure: str,
    expected: str,
) -> None:
    scenario = await _seed_legacy_scenario(postgres_database_url, tmp_path)
    owner_map = dict(scenario.owner_map)
    engine = create_async_engine(postgres_database_url)
    try:
        async with engine.begin() as connection:
            if failure == "viewer":
                await connection.execute(text("DELETE FROM scheduled_task_runs"))
                await connection.execute(text("DELETE FROM scheduled_tasks WHERE id='legacy-reuse'"))
                await connection.execute(
                    text("UPDATE scheduled_tasks SET user_id=:viewer WHERE id='legacy-fresh'"),
                    {"viewer": str(scenario.seed.viewer.user_id)},
                )
                owner_map = {str(scenario.seed.viewer.user_id): _owner_map_item(scenario.seed)}
            elif failure == "agent":
                owner_map = {
                    str(scenario.seed.owner_a.user_id): _owner_map_item(
                        scenario.seed,
                        agent_id=scenario.seed.project_b_agent_id,
                    )
                }
            elif failure == "reuse_scope":
                await connection.execute(
                    text(
                        """UPDATE scheduled_tasks
                        SET last_run_id=NULL,last_thread_id=NULL
                        WHERE id='legacy-fresh'"""
                    )
                )
                owner_map = {
                    str(scenario.seed.owner_a.user_id): _owner_map_item(
                        scenario.seed,
                        project_id=scenario.seed.project_b_owner_a.project_id,
                        agent_id=scenario.seed.project_b_agent_id,
                    )
                }
            elif failure == "orphan_run":
                await connection.execute(
                    text(
                        """UPDATE scheduled_task_runs
                        SET run_id='missing-run',status='running'
                        WHERE id='legacy-occurrence-success'"""
                    )
                )
            elif failure == "orphan_task_history":
                await connection.execute(
                    text(
                        """UPDATE scheduled_tasks
                        SET last_run_id='missing-run'
                        WHERE id='legacy-fresh'"""
                    )
                )
            elif failure == "status":
                await connection.execute(text("UPDATE scheduled_tasks SET status='unknown' WHERE id='legacy-fresh'"))
    finally:
        await engine.dispose()

    _write_backup_proof(scenario.backup_dir)
    with pytest.raises(AutomationMigrationError, match=expected):
        await run_automation_migration(
            postgres_database_url,
            owner_map=owner_map,
            backup_dir=scenario.backup_dir,
            execute=True,
        )

    engine = create_async_engine(postgres_database_url)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0011_private_artifact_tombstone"
            assert await connection.scalar(text("SELECT to_regclass('automation_migration_runs')")) is None
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_staged_rerun_rejects_target_tamper_and_source_fingerprint_change(
    postgres_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = await _seed_legacy_scenario(postgres_database_url, tmp_path)
    _write_backup_proof(scenario.backup_dir)
    original_upgrade = migrate_automations._upgrade_database

    async def stop_before_finalize(engine, revision: str) -> None:
        if revision == "head":
            raise AutomationMigrationError("injected finalize stop")
        await original_upgrade(engine, revision)

    monkeypatch.setattr(migrate_automations, "_upgrade_database", stop_before_finalize)
    with pytest.raises(AutomationMigrationError, match="injected finalize stop"):
        await run_automation_migration(
            postgres_database_url,
            owner_map=scenario.owner_map,
            backup_dir=scenario.backup_dir,
            execute=True,
        )
    monkeypatch.setattr(migrate_automations, "_upgrade_database", original_upgrade)

    engine = create_async_engine(postgres_database_url)
    try:
        async with engine.begin() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0012_project_automation_expand"
            await connection.execute(text("UPDATE scheduled_tasks SET agent_scope='system' WHERE id='legacy-fresh'"))
    finally:
        await engine.dispose()

    with pytest.raises(AutomationMigrationError, match="migration ledger conflicts"):
        await run_automation_migration(
            postgres_database_url,
            owner_map=scenario.owner_map,
            backup_dir=scenario.backup_dir,
            execute=True,
        )

    engine = create_async_engine(postgres_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("UPDATE scheduled_tasks SET agent_scope='project' WHERE id='legacy-fresh'"))
            await connection.execute(text("UPDATE scheduled_tasks SET title='changed after dry-run' WHERE id='legacy-fresh'"))
    finally:
        await engine.dispose()

    with pytest.raises(AutomationMigrationError, match="legacy source fingerprint changed"):
        await run_automation_migration(
            postgres_database_url,
            owner_map=scenario.owner_map,
            backup_dir=scenario.backup_dir,
            execute=True,
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_partial_domain_ledger_resumes_without_treating_unwritten_domain_as_tamper(
    postgres_database_url: str,
    tmp_path: Path,
) -> None:
    scenario = await _seed_legacy_scenario(postgres_database_url, tmp_path)
    _write_backup_proof(scenario.backup_dir)
    engine = create_async_engine(postgres_database_url)
    try:
        await asyncio.to_thread(
            command.upgrade,
            _get_alembic_config(engine),
            "0012_project_automation_expand",
        )
        targets = normalize_owner_map(scenario.owner_map)
        async with engine.begin() as connection:
            inventory = await migrate_automations._collect_inventory_connection(connection)
            plan = await migrate_automations._preflight(connection, inventory, targets)
            run_id = await migrate_automations._migration_run_id(
                connection,
                plan=plan,
                owner_map_digest=migrate_automations._owner_map_digest(targets),
            )
            await migrate_automations._write_domain_ledger(
                connection,
                migration_run_id=run_id,
                plan=plan,
                domain="scheduled_tasks",
            )
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0012_project_automation_expand"
            assert set((await connection.execute(text("SELECT domain FROM automation_migration_ledger"))).scalars()) == {"scheduled_tasks"}
    finally:
        await engine.dispose()

    resumed = await run_automation_migration(
        postgres_database_url,
        owner_map=scenario.owner_map,
        backup_dir=scenario.backup_dir,
        execute=True,
    )

    assert resumed.cutover_complete is True
    assert resumed.noop is False


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_atomic_staging_rolls_back_before_first_ledger(
    postgres_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = await _seed_legacy_scenario(postgres_database_url, tmp_path)
    _write_backup_proof(scenario.backup_dir)
    original_write = migrate_automations._write_domain_ledger

    async def stop_before_first_domain(*_args, **_kwargs):
        raise AutomationMigrationError("injected pre-ledger stop")

    monkeypatch.setattr(migrate_automations, "_write_domain_ledger", stop_before_first_domain)
    with pytest.raises(AutomationMigrationError, match="injected pre-ledger stop"):
        await run_automation_migration(
            postgres_database_url,
            owner_map=scenario.owner_map,
            backup_dir=scenario.backup_dir,
            execute=True,
        )
    monkeypatch.setattr(migrate_automations, "_write_domain_ledger", original_write)

    engine = create_async_engine(postgres_database_url)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0012_project_automation_expand"
            assert await connection.scalar(text("SELECT count(*) FROM automation_migration_runs")) == 0
            assert await connection.scalar(text("SELECT count(*) FROM automation_migration_ledger")) == 0
    finally:
        await engine.dispose()

    changed_map = {
        str(scenario.seed.owner_a.user_id): {
            "project_id": str(scenario.seed.owner_a.project_id),
            "fresh_thread_agent": {
                "asset_id": str(scenario.seed.system_agent_id),
                "scope": "system",
            },
        }
    }
    completed = await run_automation_migration(
        postgres_database_url,
        owner_map=changed_map,
        backup_dir=scenario.backup_dir,
        execute=True,
    )
    assert completed.cutover_complete is True


async def _stage_until_before_finalize(
    database_url: str,
    scenario: _LegacyScenario,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_upgrade = migrate_automations._upgrade_database

    async def stop_before_finalize(engine, revision: str) -> None:
        if revision == "head":
            raise AutomationMigrationError("injected finalize stop")
        await original_upgrade(engine, revision)

    monkeypatch.setattr(migrate_automations, "_upgrade_database", stop_before_finalize)
    with pytest.raises(AutomationMigrationError, match="injected finalize stop"):
        await run_automation_migration(
            database_url,
            owner_map=scenario.owner_map,
            backup_dir=scenario.backup_dir,
            execute=True,
        )
    monkeypatch.setattr(migrate_automations, "_upgrade_database", original_upgrade)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_finalize_rechecks_actual_target_digest_before_destructive_ddl(
    postgres_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = await _seed_legacy_scenario(postgres_database_url, tmp_path)
    _write_backup_proof(scenario.backup_dir)
    await _stage_until_before_finalize(
        postgres_database_url,
        scenario,
        monkeypatch,
    )

    engine = create_async_engine(postgres_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """UPDATE scheduled_tasks SET title='legal relation, tampered value'
                    WHERE id='legacy-fresh'"""
                )
            )
        config = _get_alembic_config(engine)
        with pytest.raises(RuntimeError, match="target digest"):
            await asyncio.to_thread(command.upgrade, config, "head")

        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0012_project_automation_expand"
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
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_final_schema_pre_marker_crash_resumes_by_revalidating_receipts_only(
    postgres_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = await _seed_legacy_scenario(postgres_database_url, tmp_path)
    _write_backup_proof(scenario.backup_dir)
    original_mark = migrate_automations._mark_cutover_complete

    async def fail_marker_write(*_args, **_kwargs) -> None:
        raise AutomationMigrationError("injected marker write failure")

    monkeypatch.setattr(migrate_automations, "_mark_cutover_complete", fail_marker_write)
    with pytest.raises(AutomationMigrationError, match="injected marker write failure"):
        await run_automation_migration(
            postgres_database_url,
            owner_map=scenario.owner_map,
            backup_dir=scenario.backup_dir,
            execute=True,
        )
    monkeypatch.setattr(migrate_automations, "_mark_cutover_complete", original_mark)

    engine = create_async_engine(postgres_database_url)
    try:
        async with engine.connect() as connection:
            marker = (
                await connection.execute(
                    text(
                        """SELECT stage,final_schema_probe_complete,cutover_at
                        FROM automation_cutover_state WHERE id=1"""
                    )
                )
            ).one()
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0013_project_automation_finalize"
            assert marker.stage == "migration_ready"
            assert marker.final_schema_probe_complete is True
            assert marker.cutover_at is None
    finally:
        await engine.dispose()

    resumed = await run_automation_migration(
        postgres_database_url,
        owner_map=scenario.owner_map,
        backup_dir=scenario.backup_dir,
        execute=True,
    )
    assert resumed.cutover_complete is True
    assert resumed.noop is False

    noop = await run_automation_migration(
        postgres_database_url,
        owner_map=scenario.owner_map,
        backup_dir=scenario.backup_dir,
        execute=True,
    )
    assert noop.cutover_complete is True
    assert noop.noop is True


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_final_schema_resume_rejects_target_tamper_without_rebuilding_lossy_source(
    postgres_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = await _seed_legacy_scenario(postgres_database_url, tmp_path)
    _write_backup_proof(scenario.backup_dir)
    original_mark = migrate_automations._mark_cutover_complete

    async def fail_marker_write(*_args, **_kwargs) -> None:
        raise AutomationMigrationError("injected marker write failure")

    monkeypatch.setattr(migrate_automations, "_mark_cutover_complete", fail_marker_write)
    with pytest.raises(AutomationMigrationError, match="injected marker write failure"):
        await run_automation_migration(
            postgres_database_url,
            owner_map=scenario.owner_map,
            backup_dir=scenario.backup_dir,
            execute=True,
        )
    monkeypatch.setattr(migrate_automations, "_mark_cutover_complete", original_mark)

    engine = create_async_engine(postgres_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """UPDATE scheduled_tasks SET title='final target tamper'
                    WHERE id='legacy-fresh'"""
                )
            )
    finally:
        await engine.dispose()

    with pytest.raises(AutomationMigrationError, match="target digest"):
        await run_automation_migration(
            postgres_database_url,
            owner_map=scenario.owner_map,
            backup_dir=scenario.backup_dir,
            execute=True,
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_execute_locks_legacy_writers_and_finalize_rejects_post_stage_drift(
    postgres_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = await _seed_legacy_scenario(postgres_database_url, tmp_path)
    _write_backup_proof(scenario.backup_dir)
    engine = create_async_engine(postgres_database_url)
    try:
        config = _get_alembic_config(engine)
        await asyncio.to_thread(
            command.upgrade,
            config,
            "0012_project_automation_expand",
        )
    finally:
        await engine.dispose()

    domain_entered = asyncio.Event()
    release_domain = asyncio.Event()
    staging_committed = asyncio.Event()
    allow_finalize = asyncio.Event()
    original_write = migrate_automations._write_domain_ledger
    original_execute = migrate_automations._execute_staging

    async def pause_first_domain(connection, *, migration_run_id, plan, domain):
        if domain == "scheduled_tasks":
            domain_entered.set()
            await release_domain.wait()
        await original_write(
            connection,
            migration_run_id=migration_run_id,
            plan=plan,
            domain=domain,
        )

    async def pause_after_staging(*args, **kwargs):
        result = await original_execute(*args, **kwargs)
        staging_committed.set()
        await allow_finalize.wait()
        return result

    monkeypatch.setattr(migrate_automations, "_write_domain_ledger", pause_first_domain)
    monkeypatch.setattr(migrate_automations, "_execute_staging", pause_after_staging)
    migration_task = asyncio.create_task(
        run_automation_migration(
            postgres_database_url,
            owner_map=scenario.owner_map,
            backup_dir=scenario.backup_dir,
            execute=True,
        )
    )
    update_engine = create_async_engine(postgres_database_url)

    async def concurrent_legacy_update() -> None:
        async with update_engine.begin() as connection:
            await connection.execute(
                text(
                    """UPDATE scheduled_tasks
                    SET assistant_id='concurrent legacy writer'
                    WHERE id='legacy-fresh' AND user_id=:owner"""
                ),
                {"owner": str(scenario.seed.owner_a.user_id)},
            )

    update_task = None
    try:
        await asyncio.wait_for(domain_entered.wait(), timeout=5)
        update_task = asyncio.create_task(concurrent_legacy_update())
        await asyncio.sleep(0.1)
        assert update_task.done() is False

        release_domain.set()
        await asyncio.wait_for(staging_committed.wait(), timeout=5)
        await asyncio.wait_for(update_task, timeout=5)
        allow_finalize.set()
        with pytest.raises(AutomationMigrationError):
            await asyncio.wait_for(migration_task, timeout=10)
    finally:
        release_domain.set()
        allow_finalize.set()
        for task in (update_task, migration_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (update_task, migration_task) if task is not None),
            return_exceptions=True,
        )
        await update_engine.dispose()

    engine = create_async_engine(postgres_database_url)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0012_project_automation_expand"
            assert await connection.scalar(text("SELECT assistant_id FROM scheduled_tasks WHERE id='legacy-fresh'")) == "concurrent legacy writer"
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("revision", "table"),
    (
        ("0011", "scheduled_tasks"),
        ("0011", "scheduled_task_runs"),
        ("0012", "scheduled_tasks"),
        ("0012", "scheduled_task_runs"),
    ),
)
async def test_unknown_legacy_source_column_fails_dry_run_and_execute_without_writes(
    postgres_database_url: str,
    tmp_path: Path,
    revision: str,
    table: str,
) -> None:
    scenario = await _seed_legacy_scenario(postgres_database_url, tmp_path)
    _write_backup_proof(scenario.backup_dir)
    engine = create_async_engine(postgres_database_url)
    try:
        if revision == "0012":
            config = _get_alembic_config(engine)
            await asyncio.to_thread(
                command.upgrade,
                config,
                "0012_project_automation_expand",
            )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f'ALTER TABLE "{table}" ADD COLUMN private_shadow TEXT'  # noqa: S608 - fixed parametrized allowlist
                )
            )
    finally:
        await engine.dispose()

    for execute in (False, True):
        with pytest.raises(AutomationMigrationError, match="schema is unsupported"):
            await run_automation_migration(
                postgres_database_url,
                owner_map=scenario.owner_map,
                backup_dir=scenario.backup_dir,
                execute=execute,
            )

    engine = create_async_engine(postgres_database_url)
    try:
        async with engine.connect() as connection:
            expected_revision = "0012_project_automation_expand" if revision == "0012" else "0011_private_artifact_tombstone"
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == expected_revision
            if revision == "0012":
                assert await connection.scalar(text("SELECT count(*) FROM automation_migration_runs")) == 0
                assert await connection.scalar(text("SELECT count(*) FROM automation_migration_ledger")) == 0
                assert await connection.scalar(text("SELECT count(*) FROM automation_cutover_state")) == 0
            else:
                assert await connection.scalar(text("SELECT to_regclass('automation_migration_runs')")) is None
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_empty_install_is_safe_noop(
    postgres_database_url: str,
    tmp_path: Path,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_schema(engine)
    finally:
        await engine.dispose()
    _write_backup_proof(tmp_path / "backup-proof")

    report = await run_automation_migration(
        postgres_database_url,
        owner_map={},
        backup_dir=tmp_path / "backup-proof",
        execute=True,
    )

    assert report.cutover_complete is True
    assert report.empty_install is True
    assert report.noop is True
