"""Offline persistence contract for one-shot Local host execution approvals."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import ForeignKeyConstraint, Index, UniqueConstraint

import deerflow.persistence.models  # noqa: F401 -- populate metadata
from deerflow.persistence.execution_approvals import (
    EXECUTION_APPROVAL_ACTIVE_STATUSES,
    EXECUTION_APPROVAL_STATUSES,
    ExecutionApprovalRequestRow,
    ExecutionApprovalResultReceiptRow,
)

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
    )
    assert not table.c.command_private_json.nullable
    assert not table.c.command_digest.nullable
    assert not table.c.execution_domain_affinity.nullable
    assert not table.c.source_agent_path.nullable

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


def test_fresh_schema_contains_the_closed_execution_approval_contract() -> None:
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    assert schema.count("CREATE TABLE execution_approval_requests (") == 1
    assert schema.count("CREATE TABLE execution_approval_result_receipts (") == 1
    assert "CREATE UNIQUE INDEX uq_execution_approval_requests_active_thread" in schema
    assert "CONSTRAINT uq_execution_approval_requests_receipt_scope UNIQUE" in schema
    assert "CONSTRAINT fk_execution_approval_result_receipts_approval_execution FOREIGN KEY(approval_id, project_id, owner_user_id, thread_id, execution_job_id, execution_job_attempt_id)" in schema
    assert "status = 'approved' AND execution_job_attempt_id IS NULL" in schema
    assert "INSERT INTO alembic_version (version_num) VALUES ('initial_schema');" in schema
