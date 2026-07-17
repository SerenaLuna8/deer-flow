"""Finalize project reliability after explicit migration probes.

Revision ID: 0015_project_reliability_finalize
Revises: 0014_project_reliability_expand
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision: str = "0015_project_reliability_finalize"
down_revision: str | Sequence[str] | None = "0014_project_reliability_expand"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REQUIRED_LEDGER_DOMAINS = frozenset({"jobs", "quotas", "audit", "stream", "recovery"})


def _assert_project_cutovers_complete(connection: Connection) -> None:
    private_marker = connection.execute(sa.text("SELECT stage,cutover_at FROM private_work_cutover_state WHERE id=1")).one_or_none()
    automation_marker = connection.execute(sa.text("SELECT stage,final_schema_probe_complete,cutover_at FROM automation_cutover_state WHERE id=1")).one_or_none()
    if private_marker is None or private_marker.stage != "cutover_complete" or private_marker.cutover_at is None:
        raise RuntimeError("reliability finalize prerequisites are incomplete: M4 cutover is not complete")
    if automation_marker is None or automation_marker.stage != "cutover_complete" or automation_marker.final_schema_probe_complete is not True or automation_marker.cutover_at is None:
        raise RuntimeError("reliability finalize prerequisites are incomplete: M5 cutover is not complete")


def _assert_no_active_legacy_execution(connection: Connection) -> None:
    active_runs = connection.execute(
        sa.text(
            """SELECT EXISTS (
                SELECT 1 FROM runs
                WHERE status IN ('pending', 'running') AND job_id IS NULL
            )"""
        )
    ).scalar_one()
    active_occurrences = connection.execute(
        sa.text(
            """SELECT EXISTS (
                SELECT 1 FROM scheduled_task_runs
                WHERE status IN ('queued', 'launching', 'running') AND job_id IS NULL
            )"""
        )
    ).scalar_one()
    if active_runs or active_occurrences:
        raise RuntimeError("reliability migration required: active legacy execution remains")


def _assert_migration_evidence(connection: Connection) -> None:
    marker = connection.execute(
        sa.text(
            """SELECT migration_run_id,empty_domain_probe_complete,source_probe_complete,
                      active_run_probe_complete,quota_backfill_probe_complete,
                      job_relation_probe_complete,audit_trigger_probe_complete,
                      stream_probe_complete,recovery_probe_complete
               FROM reliability_cutover_state
               WHERE id=1 AND stage IN ('empty_install','migration_ready')"""
        )
    ).one_or_none()
    if marker is None or not all(marker[2:]):
        raise RuntimeError("reliability migration required: migration_ready probes are incomplete")
    if marker.empty_domain_probe_complete is True and marker.migration_run_id is None:
        return
    if marker.empty_domain_probe_complete is True or marker.migration_run_id is None:
        raise RuntimeError("reliability migration required: migration evidence authority is invalid")
    migration_run = connection.execute(
        sa.text(
            """SELECT status,completed_at,source_probe_complete,
                      active_run_probe_complete,backup_proof_digest
               FROM reliability_migration_runs
               WHERE id=:migration_run_id AND mode='execute'"""
        ),
        {"migration_run_id": marker.migration_run_id},
    ).one_or_none()
    if (
        migration_run is None
        or migration_run.status != "completed"
        or migration_run.completed_at is None
        or migration_run.source_probe_complete is not True
        or migration_run.active_run_probe_complete is not True
        or migration_run.backup_proof_digest is None
    ):
        raise RuntimeError("reliability migration required: completed migration evidence is missing")
    domains = set(
        connection.execute(
            sa.text(
                """SELECT domain FROM reliability_migration_ledger
                   WHERE migration_run_id=:migration_run_id AND status='complete'
                     AND source_row_count=target_row_count"""
            ),
            {"migration_run_id": marker.migration_run_id},
        ).scalars()
    )
    if domains != _REQUIRED_LEDGER_DOMAINS:
        raise RuntimeError("reliability migration required: migration ledgers are incomplete")


def _assert_relation_and_quota_shape(connection: Connection) -> None:
    invalid_job = connection.execute(
        sa.text(
            """SELECT EXISTS (
                SELECT 1 FROM jobs job
                LEFT JOIN projects project ON project.id=job.project_id
                LEFT JOIN runs run
                  ON run.project_id=job.project_id
                 AND run.owner_user_id=job.owner_user_id
                 AND run.run_id=job.run_id
                LEFT JOIN scheduled_task_runs occurrence
                  ON occurrence.project_id=job.project_id
                 AND occurrence.owner_user_id=job.owner_user_id
                 AND occurrence.id=job.automation_occurrence_id
                WHERE project.id IS NULL
                   OR (job.run_id IS NOT NULL AND run.run_id IS NULL)
                   OR (job.automation_occurrence_id IS NOT NULL AND occurrence.id IS NULL)
            )"""
        )
    ).scalar_one()
    missing_quota = connection.execute(
        sa.text(
            """SELECT EXISTS (
                SELECT 1 FROM projects project
                LEFT JOIN project_quotas quota ON quota.project_id=project.id
                WHERE quota.project_id IS NULL
            )"""
        )
    ).scalar_one()
    if invalid_job or missing_quota:
        raise RuntimeError("reliability migration required: relation or quota probes failed")


def _assert_finalize_ready(connection: Connection) -> None:
    _assert_project_cutovers_complete(connection)
    _assert_no_active_legacy_execution(connection)
    _assert_migration_evidence(connection)
    _assert_relation_and_quota_shape(connection)


def _install_append_only_triggers() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_m6_append_only_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'M6 append-only rows cannot be updated or deleted'
                USING ERRCODE = 'integrity_constraint_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in ("project_usage_ledger", "audit_logs", "dead_jobs"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
        op.execute(
            f"""CREATE TRIGGER trg_{table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_m6_append_only_mutation()"""
        )


def _record_cutover_complete(connection: Connection) -> None:
    result = connection.execute(
        sa.text(
            """UPDATE reliability_cutover_state
               SET stage='cutover_complete',final_schema_probe_complete=true,
                   schema_revision='0015_project_reliability_finalize',
                   cutover_at=now(),updated_at=now()
               WHERE id=1 AND stage IN ('empty_install','migration_ready')"""
        )
    )
    if result.rowcount != 1:
        raise RuntimeError("reliability final schema marker update failed")


def upgrade() -> None:
    connection = op.get_bind()
    _assert_finalize_ready(connection)

    op.create_unique_constraint("uq_runs_job_scope", "runs", ["project_id", "owner_user_id", "run_id"])
    op.create_unique_constraint(
        "uq_scheduled_task_runs_job_scope",
        "scheduled_task_runs",
        ["project_id", "owner_user_id", "id"],
    )
    op.create_unique_constraint(
        "uq_jobs_predecessor_dead_job",
        "jobs",
        ["predecessor_dead_job_id"],
    )
    op.create_foreign_key(
        "fk_jobs_private_run",
        "jobs",
        "runs",
        ["project_id", "owner_user_id", "run_id"],
        ["project_id", "owner_user_id", "run_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_jobs_automation_occurrence",
        "jobs",
        "scheduled_task_runs",
        ["project_id", "owner_user_id", "automation_occurrence_id"],
        ["project_id", "owner_user_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_jobs_predecessor_dead_job",
        "jobs",
        "dead_jobs",
        ["predecessor_dead_job_id"],
        ["job_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key("fk_runs_job", "runs", "jobs", ["job_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key(
        "fk_scheduled_task_runs_job",
        "scheduled_task_runs",
        "jobs",
        ["job_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    _install_append_only_triggers()
    _record_cutover_complete(connection)


def downgrade() -> None:
    raise RuntimeError("M6 reliability migration is forward-only; restore a verified pre-M6 database")
