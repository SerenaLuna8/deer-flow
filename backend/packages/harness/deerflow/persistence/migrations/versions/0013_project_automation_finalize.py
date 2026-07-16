"""Finalize project automation scope after explicit migration probes.

Revision ID: 0013_project_automation_finalize
Revises: 0012_project_automation_expand
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

from deerflow.persistence.automations.migration_digest import (
    canonical_digest,
    expanded_select_sql,
    target_select_sql,
)

revision: str = "0013_project_automation_finalize"
down_revision: str | Sequence[str] | None = "0012_project_automation_expand"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


AUTOMATION_LEDGER_DOMAINS = frozenset({"scheduled_tasks", "scheduled_task_runs"})

_CREATE_AGENT_PROJECT_INTEGRITY_FUNCTION = """
CREATE OR REPLACE FUNCTION enforce_scheduled_task_agent_project()
RETURNS trigger AS $$
BEGIN
    IF TG_TABLE_NAME = 'scheduled_tasks' THEN
        IF NEW.agent_scope = 'project' THEN
            PERFORM 1
            FROM agents
            WHERE id = NEW.agent_asset_id
              AND scope = 'project'
              AND project_id = NEW.project_id
            FOR SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'project Agent must belong to the scheduled task project'
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
        END IF;
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'agents'
       AND NEW.project_id IS DISTINCT FROM OLD.project_id
       AND EXISTS (
           SELECT 1
           FROM scheduled_tasks task
           WHERE task.agent_asset_id = OLD.id
             AND task.agent_scope = 'project'
             AND task.project_id IS DISTINCT FROM NEW.project_id
       ) THEN
        RAISE EXCEPTION 'cannot move a project Agent referenced by scheduled tasks'
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


def _lock_automation_sources(connection: Connection) -> None:
    connection.execute(sa.text("LOCK TABLE scheduled_tasks, scheduled_task_runs IN SHARE ROW EXCLUSIVE MODE"))


def _is_empty_automation_domain(connection: Connection) -> bool:
    return all(
        connection.execute(sa.text(f'SELECT NOT EXISTS (SELECT 1 FROM "{table}" LIMIT 1)')).scalar_one()  # noqa: S608 - fixed internal table allowlist
        for table in ("scheduled_tasks", "scheduled_task_runs")
    )


def _assert_m4_cutover_complete(connection: Connection) -> None:
    marker = connection.execute(
        sa.text(
            """SELECT stage,cutover_at
            FROM private_work_cutover_state
            WHERE id=1"""
        )
    ).one_or_none()
    if marker is None or marker.stage != "cutover_complete" or marker.cutover_at is None:
        raise RuntimeError("automation finalize prerequisites are incomplete: M4 cutover is not complete")


def _record_empty_domain_probe(connection: Connection) -> None:
    connection.execute(
        sa.text(
            """INSERT INTO automation_cutover_state
            (id,stage,migration_run_id,empty_domain_probe_complete,
             final_schema_probe_complete,cutover_at,updated_at)
            VALUES (1,'empty_install',NULL,true,false,NULL,now())
            ON CONFLICT (id) DO NOTHING"""
        )
    )
    marker = connection.execute(
        sa.text(
            """SELECT stage,migration_run_id,empty_domain_probe_complete
            FROM automation_cutover_state WHERE id=1"""
        )
    ).one_or_none()
    if marker is None or marker.stage != "empty_install" or marker.migration_run_id is not None or marker.empty_domain_probe_complete is not True:
        raise RuntimeError("automation finalize prerequisites are incomplete: empty-domain marker conflict")


def _migration_ready_run_id(connection: Connection):
    marker = connection.execute(
        sa.text(
            """SELECT migration_run_id
            FROM automation_cutover_state
            WHERE id=1 AND stage='migration_ready'"""
        )
    ).scalar_one_or_none()
    if marker is None:
        raise RuntimeError("automation migration required: migration_ready marker missing")
    return marker


def _assert_marker_stage(connection: Connection, required: str) -> None:
    marker = connection.execute(
        sa.text(
            """SELECT stage,migration_run_id
            FROM automation_cutover_state WHERE id=1"""
        )
    ).one_or_none()
    if marker is None or marker.stage != required or marker.migration_run_id is None:
        raise RuntimeError(f"automation migration required: {required} marker missing")


def _assert_domain_ledgers_complete(connection: Connection) -> None:
    migration_run_id = _migration_ready_run_id(connection)
    migration_run = connection.execute(
        sa.text(
            """SELECT status,completed_at,source_probe_complete,
                      scope_relation_probe_complete
            FROM automation_migration_runs
            WHERE id=:migration_run_id AND mode='execute'"""
        ),
        {"migration_run_id": migration_run_id},
    ).one_or_none()
    if migration_run is None or migration_run.status != "completed" or migration_run.completed_at is None or migration_run.source_probe_complete is not True or migration_run.scope_relation_probe_complete is not True:
        raise RuntimeError("automation migration required: completed migration run missing")
    domains = set(
        connection.execute(
            sa.text(
                """SELECT domain FROM automation_migration_ledger
                WHERE migration_run_id=:migration_run_id AND status='complete'"""
            ),
            {"migration_run_id": migration_run_id},
        ).scalars()
    )
    if domains != AUTOMATION_LEDGER_DOMAINS:
        raise RuntimeError("automation migration required: domain ledgers incomplete")


def _assert_source_target_counts(connection: Connection) -> None:
    migration_run_id = _migration_ready_run_id(connection)
    ledgers = connection.execute(
        sa.text(
            """SELECT domain,source_row_count,target_row_count
            FROM automation_migration_ledger
            WHERE migration_run_id=:migration_run_id"""
        ),
        {"migration_run_id": migration_run_id},
    ).all()
    for ledger in ledgers:
        if ledger.source_row_count != ledger.target_row_count:
            raise RuntimeError("automation migration required: source/target row counts differ")
        actual = connection.execute(sa.text(f'SELECT count(*) FROM "{ledger.domain}"')).scalar_one()  # noqa: S608 - domain is constrained by the fixed ledger allowlist
        if actual != ledger.target_row_count:
            raise RuntimeError("automation migration required: target row count probe failed")


def _assert_target_digests(connection: Connection) -> None:
    migration_run_id = _migration_ready_run_id(connection)
    ledgers = {
        row.domain: row
        for row in connection.execute(
            sa.text(
                """SELECT domain,target_digest,target_row_count
                FROM automation_migration_ledger
                WHERE migration_run_id=:migration_run_id AND status='complete'"""
            ),
            {"migration_run_id": migration_run_id},
        )
    }
    if set(ledgers) != AUTOMATION_LEDGER_DOMAINS:
        raise RuntimeError("automation migration required: domain ledgers incomplete")
    for domain in sorted(AUTOMATION_LEDGER_DOMAINS):
        rows = [dict(row) for row in connection.execute(sa.text(target_select_sql(domain))).mappings()]
        ledger = ledgers[domain]
        if len(rows) != int(ledger.target_row_count) or canonical_digest(rows) != ledger.target_digest:
            raise RuntimeError("automation migration required: target digest probe failed")


def _assert_source_fingerprint(connection: Connection) -> None:
    migration_run_id = _migration_ready_run_id(connection)
    source_fingerprint = connection.execute(
        sa.text(
            """SELECT source_fingerprint FROM automation_migration_runs
            WHERE id=:migration_run_id"""
        ),
        {"migration_run_id": migration_run_id},
    ).scalar_one_or_none()
    ledger_sources = set(
        connection.execute(
            sa.text(
                """SELECT source_fingerprint FROM automation_migration_ledger
                WHERE migration_run_id=:migration_run_id"""
            ),
            {"migration_run_id": migration_run_id},
        ).scalars()
    )
    rows = {domain: [dict(row) for row in connection.execute(sa.text(expanded_select_sql(domain))).mappings()] for domain in ("scheduled_tasks", "scheduled_task_runs")}
    if source_fingerprint is None or ledger_sources != {source_fingerprint} or canonical_digest(rows) != source_fingerprint:
        raise RuntimeError("automation migration required: source fingerprint probe failed")


def _assert_scope_agent_thread_run_relations(connection: Connection) -> None:
    invalid_task = connection.execute(
        sa.text(
            """SELECT EXISTS (
                SELECT 1
                FROM scheduled_tasks task
                LEFT JOIN projects project ON project.id=task.project_id
                LEFT JOIN users owner ON owner.id=task.owner_user_id
                LEFT JOIN project_memberships membership
                  ON membership.project_id=task.project_id
                 AND membership.user_id=task.owner_user_id
                LEFT JOIN agents agent
                  ON agent.id=task.agent_asset_id AND agent.scope=task.agent_scope
                LEFT JOIN threads_meta thread
                  ON thread.project_id=task.project_id
                 AND thread.owner_user_id=task.owner_user_id
                 AND thread.thread_id=task.thread_id
                WHERE task.project_id IS NULL
                   OR task.owner_user_id IS NULL
                   OR task.agent_asset_id IS NULL
                   OR task.agent_scope IS NULL
                   OR task.version IS NULL
                   OR project.id IS NULL
                   OR owner.id IS NULL
                   OR membership.id IS NULL
                   OR agent.id IS NULL
                   OR (agent.scope='project' AND agent.project_id IS DISTINCT FROM task.project_id)
                   OR (task.thread_id IS NOT NULL AND thread.thread_id IS NULL)
                   OR (task.context_mode='reuse_thread' AND task.thread_id IS NULL)
                   OR (task.context_mode='fresh_thread_per_run' AND task.thread_id IS NOT NULL)
                LIMIT 1
            )"""
        )
    ).scalar_one()
    if invalid_task:
        raise RuntimeError("automation migration required: task scope/agent/thread relation probe failed")

    invalid_occurrence = connection.execute(
        sa.text(
            """SELECT EXISTS (
                SELECT 1
                FROM scheduled_task_runs occurrence
                LEFT JOIN scheduled_tasks task
                  ON task.project_id=occurrence.project_id
                 AND task.owner_user_id=occurrence.owner_user_id
                 AND task.id=occurrence.task_id
                LEFT JOIN threads_meta thread
                  ON thread.project_id=occurrence.project_id
                 AND thread.owner_user_id=occurrence.owner_user_id
                 AND thread.thread_id=occurrence.thread_id
                LEFT JOIN runs run
                  ON run.project_id=occurrence.project_id
                 AND run.owner_user_id=occurrence.owner_user_id
                 AND run.thread_id=occurrence.thread_id
                 AND run.run_id=occurrence.run_id
                WHERE occurrence.project_id IS NULL
                   OR occurrence.owner_user_id IS NULL
                   OR occurrence.task_version IS NULL
                   OR occurrence.occurrence_key IS NULL
                   OR occurrence.launch_attempt_count IS NULL
                   OR occurrence.updated_at IS NULL
                   OR task.id IS NULL
                   OR (occurrence.thread_id IS NOT NULL AND thread.thread_id IS NULL)
                   OR (occurrence.run_id IS NOT NULL AND run.run_id IS NULL)
                LIMIT 1
            )"""
        )
    ).scalar_one()
    if invalid_occurrence:
        raise RuntimeError("automation migration required: occurrence task/thread/run relation probe failed")


def _assert_finalize_ready(connection: Connection) -> None:
    if _is_empty_automation_domain(connection):
        _assert_m4_cutover_complete(connection)
        _record_empty_domain_probe(connection)
        return
    _assert_marker_stage(connection, "migration_ready")
    _assert_domain_ledgers_complete(connection)
    _assert_source_target_counts(connection)
    _assert_target_digests(connection)
    _assert_source_fingerprint(connection)
    _assert_scope_agent_thread_run_relations(connection)


def _install_final_task_constraints() -> None:
    op.create_unique_constraint(
        "uq_scheduled_tasks_private_scope",
        "scheduled_tasks",
        ["project_id", "owner_user_id", "id"],
    )
    op.create_foreign_key(
        "fk_scheduled_tasks_project",
        "scheduled_tasks",
        "projects",
        ["project_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_scheduled_tasks_owner",
        "scheduled_tasks",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_scheduled_tasks_project_membership",
        "scheduled_tasks",
        "project_memberships",
        ["project_id", "owner_user_id"],
        ["project_id", "user_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_scheduled_tasks_private_thread",
        "scheduled_tasks",
        "threads_meta",
        ["project_id", "owner_user_id", "thread_id"],
        ["project_id", "owner_user_id", "thread_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_scheduled_tasks_agent_asset",
        "scheduled_tasks",
        "agents",
        ["agent_asset_id", "agent_scope"],
        ["id", "scope"],
        ondelete="RESTRICT",
    )
    for name, condition in (
        (
            "ck_scheduled_tasks_context_mode",
            "context_mode IN ('fresh_thread_per_run', 'reuse_thread')",
        ),
        ("ck_scheduled_tasks_schedule_type", "schedule_type IN ('once', 'cron')"),
        (
            "ck_scheduled_tasks_status",
            "status IN ('enabled', 'paused', 'completed', 'failed', 'cancelled')",
        ),
        ("ck_scheduled_tasks_overlap_policy", "overlap_policy = 'skip'"),
        (
            "ck_scheduled_tasks_thread_mode",
            "(context_mode = 'reuse_thread' AND thread_id IS NOT NULL) OR (context_mode = 'fresh_thread_per_run' AND thread_id IS NULL)",
        ),
        (
            "ck_scheduled_tasks_agent_scope",
            "agent_scope IN ('system', 'project')",
        ),
        ("ck_scheduled_tasks_version", "version >= 1"),
        ("ck_scheduled_tasks_run_count", "run_count >= 0"),
        (
            "ck_scheduled_tasks_last_outcome",
            "last_outcome IS NULL OR last_outcome IN ('success', 'failed', 'skipped', 'interrupted', 'cancelled', 'rejected')",
        ),
    ):
        op.create_check_constraint(name, "scheduled_tasks", condition)


def _install_agent_project_integrity() -> None:
    op.execute(_CREATE_AGENT_PROJECT_INTEGRITY_FUNCTION)
    op.execute("CREATE TRIGGER trg_scheduled_tasks_agent_project BEFORE INSERT OR UPDATE OF project_id, agent_asset_id, agent_scope ON scheduled_tasks FOR EACH ROW EXECUTE FUNCTION enforce_scheduled_task_agent_project()")
    op.execute("CREATE TRIGGER trg_agents_scheduled_task_project BEFORE UPDATE OF project_id ON agents FOR EACH ROW EXECUTE FUNCTION enforce_scheduled_task_agent_project()")


def _install_final_occurrence_constraints() -> None:
    op.create_unique_constraint(
        "uq_scheduled_task_runs_occurrence",
        "scheduled_task_runs",
        ["project_id", "owner_user_id", "task_id", "occurrence_key"],
    )
    for name, referred, local, remote, ondelete in (
        (
            "fk_scheduled_task_runs_project",
            "projects",
            ["project_id"],
            ["id"],
            "RESTRICT",
        ),
        (
            "fk_scheduled_task_runs_owner",
            "users",
            ["owner_user_id"],
            ["id"],
            "RESTRICT",
        ),
        (
            "fk_scheduled_task_runs_task",
            "scheduled_tasks",
            ["project_id", "owner_user_id", "task_id"],
            ["project_id", "owner_user_id", "id"],
            "CASCADE",
        ),
        (
            "fk_scheduled_task_runs_private_thread",
            "threads_meta",
            ["project_id", "owner_user_id", "thread_id"],
            ["project_id", "owner_user_id", "thread_id"],
            "RESTRICT",
        ),
        (
            "fk_scheduled_task_runs_private_run",
            "runs",
            ["project_id", "owner_user_id", "thread_id", "run_id"],
            ["project_id", "owner_user_id", "thread_id", "run_id"],
            "RESTRICT",
        ),
    ):
        op.create_foreign_key(
            name,
            "scheduled_task_runs",
            referred,
            local,
            remote,
            ondelete=ondelete,
        )
    for name, condition in (
        (
            "ck_scheduled_task_runs_trigger",
            "trigger IN ('scheduled', 'manual')",
        ),
        (
            "ck_scheduled_task_runs_status",
            "status IN ('queued', 'launching', 'running', 'success', 'failed', 'skipped', 'interrupted', 'cancelled', 'rejected')",
        ),
        (
            "ck_scheduled_task_runs_run_requires_thread",
            "run_id IS NULL OR thread_id IS NOT NULL",
        ),
        (
            "ck_scheduled_task_runs_attempt_count",
            "launch_attempt_count >= 0 AND (resolved_membership_version IS NULL OR resolved_membership_version >= 1)",
        ),
        ("ck_scheduled_task_runs_task_version", "task_version >= 1"),
    ):
        op.create_check_constraint(name, "scheduled_task_runs", condition)
    op.create_index(
        "uq_scheduled_task_runs_manual_idempotency",
        "scheduled_task_runs",
        ["project_id", "owner_user_id", "task_id", "manual_idempotency_hash"],
        unique=True,
        postgresql_where=sa.text("manual_idempotency_hash IS NOT NULL"),
    )
    op.create_index(
        "ix_scheduled_task_runs_active_occurrence",
        "scheduled_task_runs",
        ["project_id", "owner_user_id", "status", "scheduled_for", "id"],
        postgresql_where=sa.text("status IN ('queued', 'launching', 'running')"),
    )
    op.create_index(
        "ix_scheduled_task_runs_history",
        "scheduled_task_runs",
        [
            "project_id",
            "owner_user_id",
            "task_id",
            sa.text("created_at DESC"),
            sa.text("id DESC"),
        ],
    )


def _record_final_schema_probe(connection: Connection) -> None:
    inspector = sa.inspect(connection)
    task_columns = {column["name"]: column for column in inspector.get_columns("scheduled_tasks")}
    run_columns = {column["name"]: column for column in inspector.get_columns("scheduled_task_runs")}
    required_task = {
        "project_id",
        "owner_user_id",
        "agent_asset_id",
        "agent_scope",
        "version",
    }
    required_run = {
        "project_id",
        "owner_user_id",
        "task_version",
        "occurrence_key",
        "launch_attempt_count",
        "updated_at",
    }
    if (
        not required_task <= task_columns.keys()
        or not required_run <= run_columns.keys()
        or any(task_columns[column]["nullable"] for column in required_task)
        or any(run_columns[column]["nullable"] for column in required_run)
        or "user_id" in task_columns
    ):
        raise RuntimeError("automation final schema probe failed")
    triggers = set(
        connection.execute(
            sa.text(
                """SELECT trigger_name FROM information_schema.triggers
                WHERE event_object_schema=current_schema()
                  AND trigger_name IN
                      ('trg_scheduled_tasks_agent_project',
                       'trg_agents_scheduled_task_project')"""
            )
        ).scalars()
    )
    if triggers != {
        "trg_scheduled_tasks_agent_project",
        "trg_agents_scheduled_task_project",
    }:
        raise RuntimeError("automation final Agent scope probe failed")

    stage = connection.execute(sa.text("SELECT stage FROM automation_cutover_state WHERE id=1")).scalar_one()
    if stage == "empty_install":
        result = connection.execute(
            sa.text(
                """UPDATE automation_cutover_state
                SET stage='cutover_complete',final_schema_probe_complete=true,
                    cutover_at=now(),updated_at=now()
                WHERE id=1 AND migration_run_id IS NULL
                  AND empty_domain_probe_complete"""
            )
        )
    else:
        result = connection.execute(
            sa.text(
                """UPDATE automation_cutover_state
                SET final_schema_probe_complete=true,updated_at=now()
                WHERE id=1 AND stage='migration_ready'
                  AND migration_run_id IS NOT NULL"""
            )
        )
    if result.rowcount != 1:
        raise RuntimeError("automation final schema marker update failed")


def upgrade() -> None:
    connection = op.get_bind()
    _lock_automation_sources(connection)
    _assert_finalize_ready(connection)

    for index in (
        "ix_scheduled_tasks_user_id",
        "ix_scheduled_tasks_thread_id",
        "ix_scheduled_tasks_status",
        "ix_scheduled_tasks_next_run_at",
    ):
        op.drop_index(index, table_name="scheduled_tasks")
    with op.batch_alter_table("scheduled_tasks") as batch:
        for column in (
            "last_run_id",
            "last_thread_id",
            "last_error",
            "lease_owner",
            "lease_expires_at",
            "assistant_id",
            "user_id",
        ):
            batch.drop_column(column)
        for column, type_ in (
            ("project_id", sa.Uuid()),
            ("owner_user_id", sa.String(36)),
            ("agent_asset_id", sa.Uuid()),
            ("agent_scope", sa.String(16)),
            ("version", sa.BigInteger()),
        ):
            batch.alter_column(column, existing_type=type_, nullable=False)
        batch.alter_column(
            "run_count",
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=False,
        )

    for index in (
        "ix_scheduled_task_runs_task_id",
        "ix_scheduled_task_runs_thread_id",
        "ix_scheduled_task_runs_status",
    ):
        op.drop_index(index, table_name="scheduled_task_runs")
    with op.batch_alter_table("scheduled_task_runs") as batch:
        batch.drop_column("error")
        for column, type_ in (
            ("project_id", sa.Uuid()),
            ("owner_user_id", sa.String(36)),
            ("task_version", sa.BigInteger()),
            ("occurrence_key", sa.CHAR(64)),
            ("launch_attempt_count", sa.Integer()),
            ("updated_at", sa.DateTime(timezone=True)),
        ):
            batch.alter_column(column, existing_type=type_, nullable=False)
        batch.alter_column(
            "thread_id",
            existing_type=sa.String(64),
            nullable=True,
        )

    _install_final_task_constraints()
    _install_agent_project_integrity()
    _install_final_occurrence_constraints()
    _record_final_schema_probe(connection)


def _assert_downgrade_safe() -> None:
    connection = op.get_bind()
    for table in ("scheduled_tasks", "scheduled_task_runs"):
        if connection.execute(sa.text(f'SELECT EXISTS (SELECT 1 FROM "{table}" LIMIT 1)')).scalar_one():  # noqa: S608 - fixed internal table allowlist
            raise RuntimeError("cannot downgrade M5 finalize while automation data exists")


def downgrade() -> None:
    _assert_downgrade_safe()
    connection = op.get_bind()
    op.execute("DROP TRIGGER IF EXISTS trg_agents_scheduled_task_project ON agents")
    op.execute("DROP TRIGGER IF EXISTS trg_scheduled_tasks_agent_project ON scheduled_tasks")
    op.execute("DROP FUNCTION IF EXISTS enforce_scheduled_task_agent_project()")
    connection.execute(
        sa.text(
            """UPDATE automation_cutover_state
            SET stage=CASE WHEN migration_run_id IS NULL
                           THEN 'empty_install' ELSE 'migration_ready' END,
                final_schema_probe_complete=false,cutover_at=NULL,updated_at=now()
            WHERE id=1"""
        )
    )

    for index in (
        "ix_scheduled_task_runs_history",
        "ix_scheduled_task_runs_active_occurrence",
        "uq_scheduled_task_runs_manual_idempotency",
    ):
        op.drop_index(index, table_name="scheduled_task_runs")
    for constraint, type_ in (
        ("ck_scheduled_task_runs_task_version", "check"),
        ("ck_scheduled_task_runs_attempt_count", "check"),
        ("ck_scheduled_task_runs_run_requires_thread", "check"),
        ("ck_scheduled_task_runs_status", "check"),
        ("ck_scheduled_task_runs_trigger", "check"),
        ("fk_scheduled_task_runs_private_run", "foreignkey"),
        ("fk_scheduled_task_runs_private_thread", "foreignkey"),
        ("fk_scheduled_task_runs_task", "foreignkey"),
        ("fk_scheduled_task_runs_owner", "foreignkey"),
        ("fk_scheduled_task_runs_project", "foreignkey"),
        ("uq_scheduled_task_runs_occurrence", "unique"),
    ):
        op.drop_constraint(constraint, "scheduled_task_runs", type_=type_)
    with op.batch_alter_table("scheduled_task_runs") as batch:
        batch.add_column(sa.Column("error", sa.Text(), nullable=True))
        for column, type_ in (
            ("project_id", sa.Uuid()),
            ("owner_user_id", sa.String(36)),
            ("task_version", sa.BigInteger()),
            ("occurrence_key", sa.CHAR(64)),
            ("launch_attempt_count", sa.Integer()),
            ("updated_at", sa.DateTime(timezone=True)),
        ):
            batch.alter_column(column, existing_type=type_, nullable=True)
        batch.alter_column(
            "thread_id",
            existing_type=sa.String(64),
            nullable=False,
        )
    op.create_index("ix_scheduled_task_runs_task_id", "scheduled_task_runs", ["task_id"])
    op.create_index("ix_scheduled_task_runs_thread_id", "scheduled_task_runs", ["thread_id"])
    op.create_index("ix_scheduled_task_runs_status", "scheduled_task_runs", ["status"])

    for constraint, type_ in (
        ("ck_scheduled_tasks_last_outcome", "check"),
        ("ck_scheduled_tasks_run_count", "check"),
        ("ck_scheduled_tasks_version", "check"),
        ("ck_scheduled_tasks_agent_scope", "check"),
        ("ck_scheduled_tasks_thread_mode", "check"),
        ("ck_scheduled_tasks_overlap_policy", "check"),
        ("ck_scheduled_tasks_status", "check"),
        ("ck_scheduled_tasks_schedule_type", "check"),
        ("ck_scheduled_tasks_context_mode", "check"),
        ("fk_scheduled_tasks_agent_asset", "foreignkey"),
        ("fk_scheduled_tasks_private_thread", "foreignkey"),
        ("fk_scheduled_tasks_project_membership", "foreignkey"),
        ("fk_scheduled_tasks_owner", "foreignkey"),
        ("fk_scheduled_tasks_project", "foreignkey"),
        ("uq_scheduled_tasks_private_scope", "unique"),
    ):
        op.drop_constraint(constraint, "scheduled_tasks", type_=type_)
    with op.batch_alter_table("scheduled_tasks") as batch:
        batch.add_column(sa.Column("user_id", sa.String(64), nullable=False))
        batch.add_column(sa.Column("assistant_id", sa.String(128), nullable=True))
        batch.add_column(sa.Column("last_run_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("last_thread_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("last_error", sa.Text(), nullable=True))
        batch.add_column(sa.Column("lease_owner", sa.String(128), nullable=True))
        batch.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
        for column, type_ in (
            ("project_id", sa.Uuid()),
            ("owner_user_id", sa.String(36)),
            ("agent_asset_id", sa.Uuid()),
            ("agent_scope", sa.String(16)),
            ("version", sa.BigInteger()),
        ):
            batch.alter_column(column, existing_type=type_, nullable=True)
        batch.alter_column(
            "run_count",
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=False,
        )
    op.create_index("ix_scheduled_tasks_user_id", "scheduled_tasks", ["user_id"])
    op.create_index("ix_scheduled_tasks_thread_id", "scheduled_tasks", ["thread_id"])
    op.create_index("ix_scheduled_tasks_status", "scheduled_tasks", ["status"])
    op.create_index("ix_scheduled_tasks_next_run_at", "scheduled_tasks", ["next_run_at"])
