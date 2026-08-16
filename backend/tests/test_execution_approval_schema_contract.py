"""Offline persistence contract for one-shot Local host execution approvals."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import ForeignKeyConstraint, Index, PrimaryKeyConstraint, UniqueConstraint
from sqlalchemy.exc import IntegrityError
from support.private_thread_seed import seed_private_thread_database

import deerflow.persistence.models  # noqa: F401 -- populate metadata
from deerflow.persistence.bootstrap import CURRENT_SCHEMA_REVISION
from deerflow.persistence.execution_approvals import (
    EXECUTION_APPROVAL_ACTIVE_STATUSES,
    EXECUTION_APPROVAL_OUTPUT_DELIVERY_MODES,
    EXECUTION_APPROVAL_OUTPUT_DELIVERY_STATUSES,
    EXECUTION_APPROVAL_STATUSES,
    ExecutionApprovalOutputDeliveryCandidateRow,
    ExecutionApprovalOutputDeliveryObligationRow,
    ExecutionApprovalRequestRow,
    ExecutionApprovalResultReceiptRow,
)
from deerflow.persistence.jobs import JobAttemptRow, JobRow, WorkerNodeRow
from deerflow.persistence.private_work import PrivateFileRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow

BACKEND_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = BACKEND_ROOT / "packages" / "harness" / "deerflow" / "persistence" / "full_schema.sql"


def _constraint(table, name: str):
    return next(constraint for constraint in table.constraints if constraint.name == name)


def _index(table, name: str) -> Index:
    return next(index for index in table.indexes if index.name == name)


def _normalized(value: object) -> str:
    return " ".join(str(value).split())


def test_request_model_freezes_the_private_source_command_and_continuation() -> None:
    table = ExecutionApprovalRequestRow.__table__
    assert table.name == "execution_approval_requests"
    assert tuple(table.columns) == tuple(table.c)
    assert tuple(table.c.keys()) == (
        "id",
        "project_id",
        "owner_user_id",
        "thread_id",
        "source_run_id",
        "source_job_id",
        "source_job_attempt_id",
        "source_agent_path",
        "tool_call_id",
        "kind",
        "command_digest",
        "execution_domain_affinity",
        "command_private_json",
        "status",
        "version",
        "decision",
        "decision_idempotency_key",
        "decision_request_digest",
        "decided_by_user_id",
        "decided_at",
        "continuation_run_id",
        "continuation_job_id",
        "execution_job_attempt_id",
        "claimed_at",
        "expires_at",
        "terminal_at",
        "created_at",
        "updated_at",
        "spawn_authorized_at",
    )
    assert not table.c.command_private_json.nullable
    assert not table.c.command_digest.nullable
    assert not table.c.execution_domain_affinity.nullable
    assert not table.c.source_agent_path.nullable
    assert table.c.spawn_authorized_at.nullable

    statuses = str(_constraint(table, "ck_execution_approval_requests_status").sqltext)
    assert EXECUTION_APPROVAL_STATUSES == frozenset(
        {
            "staged",
            "pending",
            "approved",
            "claimed",
            "finished",
            "launch_failed",
            "unknown",
            "denied",
            "expired",
            "cancelled",
        }
    )
    assert all(f"'{status}'" in statuses for status in EXECUTION_APPROVAL_STATUSES)
    assert "local_bash" in str(_constraint(table, "ck_execution_approval_requests_kind").sqltext)
    assert "json_typeof(command_private_json) = 'object'" in str(_constraint(table, "ck_execution_approval_requests_command_json").sqltext)
    assert "json_typeof(source_agent_path) = 'array'" in str(_constraint(table, "ck_execution_approval_requests_agent_path_json").sqltext)
    assert "^[0-9a-f]{64}$" in str(_constraint(table, "ck_execution_approval_requests_digest").sqltext)
    assert "^[0-9a-f]{64}$" in str(
        _constraint(
            table,
            "ck_execution_approval_requests_execution_domain_affinity",
        ).sqltext,
    )

    source_intent = _constraint(table, "uq_execution_approval_requests_source_tool")
    assert isinstance(source_intent, UniqueConstraint)
    assert tuple(column.name for column in source_intent.columns) == (
        "project_id",
        "owner_user_id",
        "source_run_id",
        "tool_call_id",
    )

    receipt_scope = _constraint(
        table,
        "uq_execution_approval_requests_receipt_scope",
    )
    assert isinstance(receipt_scope, UniqueConstraint)
    assert tuple(column.name for column in receipt_scope.columns) == (
        "id",
        "project_id",
        "owner_user_id",
        "thread_id",
        "continuation_job_id",
        "execution_job_attempt_id",
    )

    execution_shape = _normalized(
        _constraint(
            table,
            "ck_execution_approval_requests_execution_shape",
        ).sqltext
    )
    assert "status = 'approved' AND execution_job_attempt_id IS NULL" in execution_shape
    assert "status = 'approved' AND continuation_job_id IS NOT NULL" not in execution_shape
    for status in ("claimed", "finished", "launch_failed", "unknown"):
        assert f"'{status}'" in execution_shape
    assert "continuation_job_id IS NOT NULL" in execution_shape
    assert "execution_job_attempt_id IS NOT NULL" in execution_shape

    spawn_authorization = _normalized(
        _constraint(
            table,
            "ck_execution_approval_requests_spawn_authorization",
        ).sqltext,
    )
    assert "status != 'finished' OR spawn_authorized_at IS NOT NULL" in spawn_authorization
    assert "spawn_authorized_at IS NULL" in spawn_authorization
    assert "spawn_authorized_at >= claimed_at" in spawn_authorization

    decision_idempotency = _index(
        table,
        "uq_execution_approval_requests_decision_idempotency",
    )
    assert decision_idempotency.unique
    assert tuple(column.name for column in decision_idempotency.columns) == (
        "project_id",
        "owner_user_id",
        "decision_idempotency_key",
    )
    assert "decision_idempotency_key IS NOT NULL" in str(decision_idempotency.dialect_options["postgresql"]["where"])
    decision_shape = str(_constraint(table, "ck_execution_approval_requests_decision_shape").sqltext)
    assert "decision_idempotency_key" in decision_shape
    assert "decision_request_digest" in decision_shape

    active = _index(table, "uq_execution_approval_requests_active_thread")
    assert active.unique
    assert EXECUTION_APPROVAL_ACTIVE_STATUSES == frozenset({"staged", "pending", "approved", "claimed"})
    predicate = str(active.dialect_options["postgresql"]["where"])
    assert all(f"'{status}'" in predicate for status in EXECUTION_APPROVAL_ACTIVE_STATUSES)

    foreign_keys = {constraint.name: constraint for constraint in table.constraints if isinstance(constraint, ForeignKeyConstraint)}
    assert {
        "fk_execution_approval_requests_private_thread",
        "fk_execution_approval_requests_source_run",
        "fk_execution_approval_requests_source_job",
        "fk_execution_approval_requests_source_attempt",
        "fk_execution_approval_requests_continuation_run",
        "fk_execution_approval_requests_continuation_job",
        "fk_execution_approval_requests_execution_attempt",
    } <= set(foreign_keys)
    continuation_job = foreign_keys["fk_execution_approval_requests_continuation_job"]
    assert tuple(element.parent.name for element in continuation_job.elements) == (
        "continuation_job_id",
        "project_id",
        "owner_user_id",
        "continuation_run_id",
        "execution_domain_affinity",
    )
    assert tuple(element.target_fullname for element in continuation_job.elements) == (
        "jobs.id",
        "jobs.project_id",
        "jobs.owner_user_id",
        "jobs.run_id",
        "jobs.execution_domain_affinity",
    )


def test_result_receipt_is_one_per_approval_and_bound_to_the_exact_attempt() -> None:
    table = ExecutionApprovalResultReceiptRow.__table__
    assert table.name == "execution_approval_result_receipts"
    assert tuple(table.c.keys()) == (
        "id",
        "approval_id",
        "project_id",
        "owner_user_id",
        "thread_id",
        "execution_job_id",
        "execution_job_attempt_id",
        "outcome",
        "exit_code",
        "result_digest",
        "result_private_json",
        "public_error_code",
        "created_at",
    )

    approval_unique = _constraint(table, "uq_execution_approval_result_receipts_approval")
    assert isinstance(approval_unique, UniqueConstraint)
    assert tuple(column.name for column in approval_unique.columns) == ("approval_id",)
    assert "json_typeof(result_private_json) = 'object'" in str(_constraint(table, "ck_execution_approval_result_receipts_result_json").sqltext)
    assert "2097152" in str(
        _constraint(
            table,
            "ck_execution_approval_result_receipts_result_json",
        ).sqltext
    )
    assert "^[0-9a-f]{64}$" in str(
        _constraint(
            table,
            "ck_execution_approval_result_receipts_digest",
        ).sqltext
    )
    assert "finished" in str(_constraint(table, "ck_execution_approval_result_receipts_outcome").sqltext)
    assert "launch_failed" in str(_constraint(table, "ck_execution_approval_result_receipts_outcome").sqltext)

    foreign_keys = {constraint.name: constraint for constraint in table.constraints if isinstance(constraint, ForeignKeyConstraint)}
    assert {
        "fk_execution_approval_result_receipts_approval_execution",
        "fk_execution_approval_result_receipts_execution_job",
        "fk_execution_approval_result_receipts_execution_attempt",
    } <= set(foreign_keys)
    approval_execution = foreign_keys["fk_execution_approval_result_receipts_approval_execution"]
    assert tuple(element.parent.name for element in approval_execution.elements) == (
        "approval_id",
        "project_id",
        "owner_user_id",
        "thread_id",
        "execution_job_id",
        "execution_job_attempt_id",
    )
    assert tuple(element.target_fullname for element in approval_execution.elements) == (
        "execution_approval_requests.id",
        "execution_approval_requests.project_id",
        "execution_approval_requests.owner_user_id",
        "execution_approval_requests.thread_id",
        "execution_approval_requests.continuation_job_id",
        "execution_approval_requests.execution_job_attempt_id",
    )


def test_output_delivery_obligation_is_one_per_private_approval() -> None:
    table = ExecutionApprovalOutputDeliveryObligationRow.__table__
    assert table.name == "execution_approval_output_delivery_obligations"
    assert tuple(table.c.keys()) == (
        "approval_id",
        "project_id",
        "owner_user_id",
        "thread_id",
        "mode",
        "status",
        "continuation_run_id",
        "continuation_job_id",
        "intent_tool_call_id",
        "intent_digest",
        "intent_private_json",
        "satisfied_artifact_id",
        "version",
        "assigned_at",
        "intent_recorded_at",
        "terminal_at",
        "created_at",
        "updated_at",
    )
    assert tuple(column.name for column in table.primary_key.columns) == ("approval_id",)
    assert EXECUTION_APPROVAL_OUTPUT_DELIVERY_MODES == frozenset({"any_one"})
    assert EXECUTION_APPROVAL_OUTPUT_DELIVERY_STATUSES == frozenset(
        {
            "deferred",
            "assigned",
            "intent_recorded",
            "delivered",
            "cancelled",
            "blocked_unknown",
            "failed",
        }
    )
    mode = str(_constraint(table, "ck_ea_output_delivery_obligations_mode").sqltext)
    statuses = str(_constraint(table, "ck_ea_output_delivery_obligations_status").sqltext)
    assert "'any_one'" in mode
    assert all(f"'{status}'" in statuses for status in EXECUTION_APPROVAL_OUTPUT_DELIVERY_STATUSES)

    intent_shape = _normalized(
        _constraint(
            table,
            "ck_ea_output_delivery_obligations_intent_shape",
        ).sqltext
    )
    assert "intent_digest ~ '^[0-9a-f]{64}$'" in intent_shape
    assert "json_typeof(intent_private_json) = 'object'" in intent_shape
    assert "octet_length(intent_private_json::text) <= 1048576" in intent_shape
    lifecycle = _normalized(
        _constraint(
            table,
            "ck_ea_output_delivery_obligations_lifecycle_shape",
        ).sqltext
    )
    assert "status = 'delivered'" in lifecycle
    assert "satisfied_artifact_id IS NOT NULL" in lifecycle

    foreign_keys = {constraint.name: constraint for constraint in table.constraints if isinstance(constraint, ForeignKeyConstraint)}
    approval = foreign_keys["fk_ea_output_delivery_obligations_approval"]
    assert approval.ondelete == "CASCADE"
    assert tuple(element.target_fullname for element in approval.elements) == (
        "execution_approval_requests.id",
        "execution_approval_requests.project_id",
        "execution_approval_requests.owner_user_id",
        "execution_approval_requests.thread_id",
    )
    artifact = foreign_keys["fk_ea_output_delivery_obligations_satisfied_artifact"]
    assert artifact.ondelete == "RESTRICT"
    assert tuple(element.target_fullname for element in artifact.elements) == (
        "artifacts.project_id",
        "artifacts.owner_user_id",
        "artifacts.thread_id",
        "artifacts.run_id",
        "artifacts.id",
    )


def test_output_delivery_candidates_freeze_private_file_identity() -> None:
    table = ExecutionApprovalOutputDeliveryCandidateRow.__table__
    assert table.name == "execution_approval_output_delivery_candidates"
    assert tuple(table.c.keys()) == (
        "approval_id",
        "file_id",
        "project_id",
        "owner_user_id",
        "thread_id",
        "logical_path",
        "file_version",
        "sha256",
        "created_at",
    )
    primary_key = table.primary_key
    assert isinstance(primary_key, PrimaryKeyConstraint)
    assert tuple(column.name for column in primary_key.columns) == (
        "approval_id",
        "file_id",
    )
    path_unique = _constraint(table, "uq_ea_output_delivery_candidates_path")
    assert isinstance(path_unique, UniqueConstraint)
    assert tuple(column.name for column in path_unique.columns) == (
        "approval_id",
        "logical_path",
    )
    path_check = str(_constraint(table, "ck_ea_output_delivery_candidates_path").sqltext)
    assert "outputs/%" in path_check
    assert "(^|/)\\.\\.(/|$)" in path_check
    assert "^[A-Za-z]:" in path_check

    foreign_keys = {constraint.name: constraint for constraint in table.constraints if isinstance(constraint, ForeignKeyConstraint)}
    obligation = foreign_keys["fk_ea_output_delivery_candidates_obligation"]
    assert obligation.ondelete == "CASCADE"
    assert tuple(element.target_fullname for element in obligation.elements) == (
        "execution_approval_output_delivery_obligations.approval_id",
        "execution_approval_output_delivery_obligations.project_id",
        "execution_approval_output_delivery_obligations.owner_user_id",
        "execution_approval_output_delivery_obligations.thread_id",
    )
    private_file = foreign_keys["fk_ea_output_delivery_candidates_private_file"]
    assert private_file.ondelete == "RESTRICT"
    assert tuple(element.target_fullname for element in private_file.elements) == (
        "files.project_id",
        "files.owner_user_id",
        "files.thread_id",
        "files.id",
    )


def test_fresh_schema_contains_the_closed_execution_approval_contract() -> None:
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    assert schema.count("CREATE TABLE execution_approval_requests (") == 1
    assert schema.count("CREATE TABLE execution_approval_result_receipts (") == 1
    assert schema.count("CREATE TABLE execution_approval_output_delivery_obligations (") == 1
    assert schema.count("CREATE TABLE execution_approval_output_delivery_candidates (") == 1
    assert "CREATE UNIQUE INDEX uq_execution_approval_requests_active_thread" in schema
    assert "CONSTRAINT uq_execution_approval_requests_receipt_scope UNIQUE" in schema
    assert "CONSTRAINT fk_execution_approval_result_receipts_approval_execution FOREIGN KEY(approval_id, project_id, owner_user_id, thread_id, execution_job_id, execution_job_attempt_id)" in schema
    assert "status = 'approved' AND execution_job_attempt_id IS NULL" in schema
    assert f"INSERT INTO alembic_version (version_num) VALUES ('{CURRENT_SCHEMA_REVISION}');" in schema


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_output_delivery_retention_restricts_files_and_cascades_with_approval(
    migrated_postgres_database_url: str,
) -> None:
    """Candidate identity survives file cleanup but follows approval retention."""

    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = f"thread-{uuid.uuid4()}"
    run_id = f"run-{uuid.uuid4()}"
    job_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    approval_id = uuid.uuid4()
    file_id = uuid.uuid4()
    owner_user_id = str(seed.owner_a.user_id)
    project_id = seed.owner_a.project_id
    now = datetime.now(UTC)
    affinity = "a" * 64

    try:
        async with seed.factory() as session, session.begin():
            session.add(
                ThreadMetaRow(
                    thread_id=thread_id,
                    owner_user_id=owner_user_id,
                    status="idle",
                    metadata_json={},
                    created_at=now,
                    updated_at=now,
                    project_id=project_id,
                    agent_asset_id=seed.project_agent_id,
                    agent_scope="project",
                )
            )
            await session.flush()
            run = RunRow(
                run_id=run_id,
                thread_id=thread_id,
                owner_user_id=owner_user_id,
                status="running",
                origin_trace_id=f"trace-{uuid.uuid4()}",
                project_id=project_id,
            )
            session.add(run)
            await session.flush()
            session.add_all(
                [
                    JobRow(
                        id=job_id,
                        job_type="private_run",
                        project_id=project_id,
                        owner_user_id=owner_user_id,
                        run_id=run_id,
                        origin_trace_id=run.origin_trace_id,
                        idempotency_key="b" * 64,
                        status="running",
                        max_attempts=1,
                        execution_domain_affinity=affinity,
                    ),
                    WorkerNodeRow(
                        id=worker_id,
                        version="output-delivery-retention-test",
                        capabilities_json=["private_run"],
                        max_concurrent_jobs=1,
                    ),
                ]
            )
            await session.flush()
            run.job_id = job_id
            session.add(
                JobAttemptRow(
                    id=attempt_id,
                    job_id=job_id,
                    attempt_number=1,
                    worker_id=worker_id,
                    lease_token_hash="c" * 64,
                )
            )
            await session.flush()
            session.add(
                ExecutionApprovalRequestRow(
                    id=approval_id,
                    project_id=project_id,
                    owner_user_id=owner_user_id,
                    thread_id=thread_id,
                    source_run_id=run_id,
                    source_job_id=job_id,
                    source_job_attempt_id=attempt_id,
                    source_agent_path=["lead"],
                    tool_call_id="retention-tool-call",
                    kind="local_bash",
                    command_digest="d" * 64,
                    execution_domain_affinity=affinity,
                    command_private_json={"schema_version": 1},
                    status="staged",
                    expires_at=now + timedelta(minutes=5),
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                PrivateFileRow(
                    id=file_id,
                    project_id=project_id,
                    owner_user_id=owner_user_id,
                    thread_id=thread_id,
                    kind="output",
                    logical_path="outputs/retained.txt",
                    media_type="text/plain",
                    size=1,
                    sha256="e" * 64,
                    status="ready",
                    version=1,
                    created_by_run_id=run_id,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()
            session.add(
                ExecutionApprovalOutputDeliveryObligationRow(
                    approval_id=approval_id,
                    project_id=project_id,
                    owner_user_id=owner_user_id,
                    thread_id=thread_id,
                    mode="any_one",
                    status="deferred",
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()
            session.add(
                ExecutionApprovalOutputDeliveryCandidateRow(
                    approval_id=approval_id,
                    file_id=file_id,
                    project_id=project_id,
                    owner_user_id=owner_user_id,
                    thread_id=thread_id,
                    logical_path="outputs/retained.txt",
                    file_version=1,
                    sha256="e" * 64,
                    created_at=now,
                )
            )

        with pytest.raises(IntegrityError):
            async with seed.factory() as session, session.begin():
                await session.execute(sa.delete(PrivateFileRow).where(PrivateFileRow.id == file_id))

        async with seed.factory() as session, session.begin():
            await session.execute(sa.delete(ExecutionApprovalRequestRow).where(ExecutionApprovalRequestRow.id == approval_id))

        async with seed.factory() as session:
            assert (
                await session.get(
                    ExecutionApprovalOutputDeliveryObligationRow,
                    approval_id,
                )
                is None
            )
            assert await session.scalar(sa.select(sa.func.count()).select_from(ExecutionApprovalOutputDeliveryCandidateRow).where(ExecutionApprovalOutputDeliveryCandidateRow.approval_id == approval_id)) == 0
            assert await session.get(PrivateFileRow, file_id) is not None
    finally:
        await seed.engine.dispose()
