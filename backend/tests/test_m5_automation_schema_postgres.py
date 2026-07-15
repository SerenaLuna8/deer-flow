from __future__ import annotations

import asyncio
import importlib
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from postgres_utils import temporary_postgres_database
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from support.m4_private_threads import seed_m4_thread_database

from deerflow.persistence.base import Base
from deerflow.persistence.bootstrap import _get_alembic_config, bootstrap_schema

M5_TABLES = {
    "scheduled_tasks",
    "scheduled_task_runs",
    "automation_migration_runs",
    "automation_migration_ledger",
    "automation_cutover_state",
}

EXPECTED_TASK_CHECKS = {
    "ck_scheduled_tasks_context_mode",
    "ck_scheduled_tasks_schedule_type",
    "ck_scheduled_tasks_status",
    "ck_scheduled_tasks_overlap_policy",
    "ck_scheduled_tasks_thread_mode",
    "ck_scheduled_tasks_agent_scope",
    "ck_scheduled_tasks_version",
}

EXPECTED_RUN_CHECKS = {
    "ck_scheduled_task_runs_trigger",
    "ck_scheduled_task_runs_status",
    "ck_scheduled_task_runs_run_requires_thread",
    "ck_scheduled_task_runs_attempt_count",
}


def _normalize(value: object) -> str | None:
    if value is None:
        return None
    return " ".join(str(value).split())


def _m5_catalog(sync_connection) -> dict[str, dict]:
    inspector = inspect(sync_connection)
    catalog: dict[str, dict] = {}
    for table in sorted(M5_TABLES):
        catalog[table] = {
            "columns": tuple(
                sorted(
                    (
                        column["name"],
                        str(column["type"]),
                        column["nullable"],
                        _normalize(column.get("default")),
                    )
                    for column in inspector.get_columns(table)
                )
            ),
            "primary_key": inspector.get_pk_constraint(table),
            "unique": tuple(sorted((constraint["name"], tuple(constraint["column_names"])) for constraint in inspector.get_unique_constraints(table))),
            "checks": tuple(sorted((constraint["name"], _normalize(constraint["sqltext"])) for constraint in inspector.get_check_constraints(table))),
            "foreign_keys": tuple(
                sorted(
                    (
                        constraint["name"],
                        tuple(constraint["constrained_columns"]),
                        constraint["referred_table"],
                        tuple(constraint["referred_columns"]),
                        constraint.get("options", {}).get("ondelete"),
                    )
                    for constraint in inspector.get_foreign_keys(table)
                )
            ),
            "indexes": tuple(
                sorted(
                    (
                        index["name"],
                        tuple(index["column_names"]),
                        index["unique"],
                        _normalize(index.get("dialect_options", {}).get("postgresql_where")),
                    )
                    for index in inspector.get_indexes(table)
                )
            ),
            "triggers": tuple(
                sync_connection.execute(
                    text(
                        """SELECT trigger_name FROM information_schema.triggers
                        WHERE event_object_schema=current_schema()
                          AND event_object_table=:table
                        ORDER BY trigger_name"""
                    ),
                    {"table": table},
                ).scalars()
            ),
        }
    return catalog


def _constraint_names(table_name: str) -> set[str | None]:
    return {constraint.name for constraint in Base.metadata.tables[table_name].constraints}


def test_m5_models_register_only_final_automation_shape() -> None:
    importlib.import_module("deerflow.persistence.models")

    assert M5_TABLES <= set(Base.metadata.tables)
    task_columns = set(Base.metadata.tables["scheduled_tasks"].c.keys())
    run_columns = set(Base.metadata.tables["scheduled_task_runs"].c.keys())
    assert {
        "project_id",
        "owner_user_id",
        "agent_asset_id",
        "agent_scope",
        "version",
        "frozen_at",
        "deleted_at",
    } <= task_columns
    assert {
        "project_id",
        "owner_user_id",
        "task_version",
        "occurrence_key",
        "manual_idempotency_hash",
        "launch_attempt_count",
    } <= run_columns
    assert {
        "user_id",
        "assistant_id",
        "last_run_id",
        "last_thread_id",
        "last_error",
        "lease_owner",
        "lease_expires_at",
    }.isdisjoint(task_columns)
    assert "error" not in run_columns
    assert EXPECTED_TASK_CHECKS <= _constraint_names("scheduled_tasks")
    assert EXPECTED_RUN_CHECKS <= _constraint_names("scheduled_task_runs")


def test_revision_ancestry_accepts_m5_as_m4_descendant() -> None:
    revisions = importlib.import_module("deerflow.persistence.revisions")
    ancestry = revisions.RevisionAncestry.from_script_directory()

    assert ancestry.contains(
        "0013_project_automation_finalize",
        "0011_private_artifact_tombstone",
    )
    assert ancestry.contains(
        "0013_project_automation_finalize",
        "0013_project_automation_finalize",
    )
    assert not ancestry.contains(
        "0011_private_artifact_tombstone",
        "0013_project_automation_finalize",
    )
    assert not ancestry.contains("unknown_branch", "0011_private_artifact_tombstone")


def test_m5_revisions_form_the_linear_alembic_head() -> None:
    expand = importlib.import_module("deerflow.persistence.migrations.versions.0012_project_automation_expand")
    finalize = importlib.import_module("deerflow.persistence.migrations.versions.0013_project_automation_finalize")
    config = AlembicConfig()
    config.set_main_option(
        "script_location",
        str(Path(finalize.__file__).resolve().parents[1]),
    )

    assert expand.revision == "0012_project_automation_expand"
    assert expand.down_revision == "0011_private_artifact_tombstone"
    assert finalize.revision == "0013_project_automation_finalize"
    assert finalize.down_revision == "0012_project_automation_expand"
    assert ScriptDirectory.from_config(config).get_current_head() == finalize.revision


def test_m5_finalize_validates_readiness_before_any_ddl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = importlib.import_module("deerflow.persistence.migrations.versions.0013_project_automation_finalize")
    mutations: list[str] = []

    class _Batch:
        def __enter__(self):
            mutations.append("batch_enter")
            return self

        def __exit__(self, *_args):
            return False

    fake_op = SimpleNamespace(
        get_bind=lambda: object(),
        batch_alter_table=lambda *_args, **_kwargs: _Batch(),
        create_foreign_key=lambda *_args, **_kwargs: mutations.append("create_foreign_key"),
        create_unique_constraint=lambda *_args, **_kwargs: mutations.append("create_unique_constraint"),
        create_check_constraint=lambda *_args, **_kwargs: mutations.append("create_check_constraint"),
        create_index=lambda *_args, **_kwargs: mutations.append("create_index"),
        drop_index=lambda *_args, **_kwargs: mutations.append("drop_index"),
    )
    monkeypatch.setattr(migration, "op", fake_op)

    def fail_before_ddl(_connection) -> None:
        raise RuntimeError("automation finalize prerequisites are incomplete")

    monkeypatch.setattr(migration, "_assert_finalize_ready", fail_before_ddl)
    with pytest.raises(RuntimeError, match="prerequisites are incomplete"):
        migration.upgrade()
    assert mutations == []


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_m5_final_schema_has_private_scope_and_occurrence_constraints(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    try:
        async with engine.connect() as connection:
            tables = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
            task_columns = await connection.run_sync(lambda sync: {column["name"]: column for column in inspect(sync).get_columns("scheduled_tasks")})
            run_columns = await connection.run_sync(lambda sync: {column["name"]: column for column in inspect(sync).get_columns("scheduled_task_runs")})
            task_checks = await connection.run_sync(lambda sync: {item["name"] for item in inspect(sync).get_check_constraints("scheduled_tasks")})
            run_checks = await connection.run_sync(lambda sync: {item["name"] for item in inspect(sync).get_check_constraints("scheduled_task_runs")})
            task_fks = await connection.run_sync(lambda sync: {item["name"]: item for item in inspect(sync).get_foreign_keys("scheduled_tasks")})
            run_fks = await connection.run_sync(lambda sync: {item["name"]: item for item in inspect(sync).get_foreign_keys("scheduled_task_runs")})
            run_indexes = await connection.run_sync(lambda sync: {item["name"]: item for item in inspect(sync).get_indexes("scheduled_task_runs")})
            task_agent_triggers = set(
                (
                    await connection.execute(
                        text(
                            """SELECT trigger_name FROM information_schema.triggers
                            WHERE event_object_schema=current_schema()
                              AND event_object_table IN ('scheduled_tasks','agents')"""
                        )
                    )
                ).scalars()
            )
            cutover_checks = await connection.run_sync(lambda sync: {item["name"] for item in inspect(sync).get_check_constraints("automation_cutover_state")})
            marker = (
                await connection.execute(
                    text(
                        """SELECT stage,migration_run_id,empty_domain_probe_complete,
                        final_schema_probe_complete,cutover_at
                        FROM automation_cutover_state WHERE id=1"""
                    )
                )
            ).one()
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))

        assert M5_TABLES <= tables
        assert task_columns["project_id"]["nullable"] is False
        assert task_columns["owner_user_id"]["nullable"] is False
        assert task_columns["agent_asset_id"]["nullable"] is False
        assert "user_id" not in task_columns
        assert run_columns["occurrence_key"]["nullable"] is False
        assert run_columns["project_id"]["nullable"] is False
        assert run_columns["owner_user_id"]["nullable"] is False
        assert EXPECTED_TASK_CHECKS <= task_checks
        assert EXPECTED_RUN_CHECKS <= run_checks

        assert task_fks["fk_scheduled_tasks_project_membership"]["constrained_columns"] == ["project_id", "owner_user_id"]
        assert task_fks["fk_scheduled_tasks_private_thread"]["constrained_columns"] == ["project_id", "owner_user_id", "thread_id"]
        assert task_fks["fk_scheduled_tasks_agent_asset"]["constrained_columns"] == ["agent_asset_id", "agent_scope"]
        assert run_fks["fk_scheduled_task_runs_task"]["constrained_columns"] == [
            "project_id",
            "owner_user_id",
            "task_id",
        ]
        assert run_fks["fk_scheduled_task_runs_private_thread"]["constrained_columns"] == ["project_id", "owner_user_id", "thread_id"]
        assert run_fks["fk_scheduled_task_runs_private_run"]["constrained_columns"] == ["project_id", "owner_user_id", "thread_id", "run_id"]

        manual = run_indexes["uq_scheduled_task_runs_manual_idempotency"]
        assert manual["unique"] is True
        assert "manual_idempotency_hash" in _normalize(manual["dialect_options"]["postgresql_where"])
        active = run_indexes["ix_scheduled_task_runs_active_occurrence"]
        assert active["unique"] is False
        active_predicate = _normalize(active["dialect_options"]["postgresql_where"])
        assert all(status in active_predicate for status in ("queued", "launching", "running"))
        assert {
            "trg_scheduled_tasks_agent_project",
            "trg_agents_scheduled_task_project",
        } <= task_agent_triggers

        assert {
            "ck_automation_cutover_state_singleton",
            "ck_automation_cutover_state_stage",
            "ck_automation_cutover_state_complete",
        } <= cutover_checks
        assert marker.stage == "cutover_complete"
        assert marker.migration_run_id is None
        assert marker.empty_domain_probe_complete is True
        assert marker.final_schema_probe_complete is True
        assert marker.cutover_at is not None
        assert revision == "0013_project_automation_finalize"
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_scheduled_task_agent_project_integrity_is_bidirectional(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    task_sql = text(
        """INSERT INTO scheduled_tasks
        (id,project_id,owner_user_id,thread_id,context_mode,agent_asset_id,
         agent_scope,title,prompt,schedule_type,schedule_spec,timezone,status,
         overlap_policy,next_run_at,run_count,version,created_at,updated_at)
        VALUES
        (:id,:project_id,:owner_user_id,NULL,'fresh_thread_per_run',:agent_id,
         :agent_scope,:id,'private','cron','{"cron":"0 9 * * *"}'::json,
         'UTC','enabled','skip',now(),0,1,now(),now())"""
    )
    try:
        valid_rows = (
            {
                "id": "task-valid-system-agent",
                "project_id": seed.owner_a.project_id,
                "owner_user_id": str(seed.owner_a.user_id),
                "agent_id": seed.system_agent_id,
                "agent_scope": "system",
            },
            {
                "id": "task-valid-project-agent",
                "project_id": seed.owner_a.project_id,
                "owner_user_id": str(seed.owner_a.user_id),
                "agent_id": seed.project_agent_id,
                "agent_scope": "project",
            },
        )
        async with seed.engine.begin() as connection:
            await connection.execute(task_sql, valid_rows)

        with pytest.raises(IntegrityError):
            async with seed.engine.begin() as connection:
                await connection.execute(
                    task_sql,
                    {
                        "id": "task-cross-project-agent",
                        "project_id": seed.owner_a.project_id,
                        "owner_user_id": str(seed.owner_a.user_id),
                        "agent_id": seed.project_b_agent_id,
                        "agent_scope": "project",
                    },
                )

        with pytest.raises(IntegrityError):
            async with seed.engine.begin() as connection:
                await connection.execute(
                    text(
                        """UPDATE agents SET project_id=:other_project_id
                        WHERE id=:agent_id"""
                    ),
                    {
                        "other_project_id": seed.project_b_owner_a.project_id,
                        "agent_id": seed.project_agent_id,
                    },
                )
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_fresh_and_staged_m5_catalogs_are_identical(
    postgres_admin_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
    async with temporary_postgres_database(postgres_admin_url) as fresh_url:
        async with temporary_postgres_database(postgres_admin_url) as staged_url:
            fresh_engine = create_async_engine(fresh_url)
            staged_engine = create_async_engine(staged_url)
            try:
                await bootstrap_schema(fresh_engine)
                await bootstrap_schema(staged_engine)
                staged_config = _get_alembic_config(staged_engine)
                await asyncio.to_thread(
                    command.downgrade,
                    staged_config,
                    "0011_private_artifact_tombstone",
                )
                await staged_engine.dispose()
                staged_engine = create_async_engine(staged_url)
                staged_config = _get_alembic_config(staged_engine)
                await asyncio.to_thread(command.upgrade, staged_config, "head")

                async with fresh_engine.connect() as connection:
                    fresh_catalog = await connection.run_sync(_m5_catalog)
                async with staged_engine.connect() as connection:
                    staged_catalog = await connection.run_sync(_m5_catalog)
                assert fresh_catalog == staged_catalog
            finally:
                await fresh_engine.dispose()
                await staged_engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_nonempty_automation_domain_fails_before_finalize_ddl(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_schema(engine)
        config = _get_alembic_config(engine)
        await asyncio.to_thread(
            command.downgrade,
            config,
            "0011_private_artifact_tombstone",
        )
        await engine.dispose()
        engine = create_async_engine(postgres_database_url)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO scheduled_tasks
                    (id,user_id,thread_id,context_mode,assistant_id,title,prompt,
                     schedule_type,schedule_spec,timezone,status,overlap_policy,
                     next_run_at,last_run_at,last_run_id,last_thread_id,last_error,
                     lease_owner,lease_expires_at,run_count,created_at,updated_at)
                    VALUES
                    ('legacy-task','legacy-owner',NULL,'fresh_thread_per_run',NULL,
                     'Legacy','private','once','{}'::json,'UTC','enabled','skip',
                     NULL,NULL,NULL,NULL,NULL,NULL,NULL,0,now(),now())"""
                )
            )
        config = _get_alembic_config(engine)
        await asyncio.to_thread(
            command.upgrade,
            config,
            "0012_project_automation_expand",
        )

        with pytest.raises(RuntimeError, match="automation migration required"):
            await asyncio.to_thread(command.upgrade, config, "head")
        await engine.dispose()
        engine = create_async_engine(postgres_database_url)
        async with engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            columns = await connection.run_sync(lambda sync: {item["name"]: item for item in inspect(sync).get_columns("scheduled_tasks")})
        assert revision == "0012_project_automation_expand"
        assert "user_id" in columns
        assert columns["project_id"]["nullable"] is True
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_cross_project_agent_fails_finalize_before_destructive_ddl(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
    engine = create_async_engine(postgres_database_url)
    seed = None
    try:
        await bootstrap_schema(engine)
        seed = await seed_m4_thread_database(postgres_database_url)
        config = _get_alembic_config(engine)
        await seed.engine.dispose()
        await engine.dispose()
        await asyncio.to_thread(
            command.downgrade,
            config,
            "0011_private_artifact_tombstone",
        )
        engine = create_async_engine(postgres_database_url)
        config = _get_alembic_config(engine)
        await asyncio.to_thread(
            command.upgrade,
            config,
            "0012_project_automation_expand",
        )

        migration_run_id = uuid.uuid4()
        digest = "d" * 64
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO scheduled_tasks
                    (id,user_id,thread_id,context_mode,assistant_id,title,prompt,
                     schedule_type,schedule_spec,timezone,status,overlap_policy,
                     next_run_at,last_run_at,last_run_id,last_thread_id,last_error,
                     lease_owner,lease_expires_at,run_count,created_at,updated_at,
                     project_id,owner_user_id,agent_asset_id,agent_scope,version)
                    VALUES
                    ('cross-project-agent',:owner,NULL,'fresh_thread_per_run',NULL,
                     'Cross project','private','cron','{"cron":"0 9 * * *"}'::json,
                     'UTC','enabled','skip',now(),NULL,NULL,NULL,NULL,NULL,NULL,0,
                     now(),now(),:project_id,:owner,:agent_id,'project',1)"""
                ),
                {
                    "owner": str(seed.owner_a.user_id),
                    "project_id": seed.owner_a.project_id,
                    "agent_id": seed.project_b_agent_id,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO automation_migration_runs
                    (id,mode,status,source_fingerprint,owner_map_digest,
                     source_task_count,source_run_count,source_probe_complete,
                     scope_relation_probe_complete,completed_at)
                    VALUES
                    (:id,'execute','completed',:digest,:digest,1,0,true,true,now())"""
                ),
                {"id": migration_run_id, "digest": digest},
            )
            await connection.execute(
                text(
                    """INSERT INTO automation_migration_ledger
                    (migration_run_id,domain,source_fingerprint,target_digest,
                     status,source_row_count,target_row_count)
                    VALUES
                    (:id,'scheduled_tasks',:digest,:digest,'complete',1,1),
                    (:id,'scheduled_task_runs',:digest,:digest,'complete',0,0)"""
                ),
                {"id": migration_run_id, "digest": digest},
            )
            await connection.execute(
                text(
                    """INSERT INTO automation_cutover_state
                    (id,stage,migration_run_id,updated_at)
                    VALUES (1,'migration_ready',:id,now())"""
                ),
                {"id": migration_run_id},
            )

        with pytest.raises(
            RuntimeError,
            match="task scope/agent/thread relation probe failed",
        ):
            await asyncio.to_thread(command.upgrade, config, "head")

        async with engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            columns = await connection.run_sync(lambda sync: {item["name"] for item in inspect(sync).get_columns("scheduled_tasks")})
        assert revision == "0012_project_automation_expand"
        assert "user_id" in columns
    finally:
        if seed is not None:
            await seed.engine.dispose()
        await engine.dispose()
