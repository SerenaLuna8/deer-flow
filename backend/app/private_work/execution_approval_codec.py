"""Pure serialization for Execution Approval private envelopes and result receipts.

Every function here is a deterministic transformation between typed Harness
objects and the private JSON stored on approval and receipt rows.  Nothing in
this module opens a session, holds a lease, or talks to the Gateway.
"""

from __future__ import annotations

from app.private_work.execution_approval_policy import (
    HostExecutionProviderPolicySnapshot,
    _canonical_digest,
)
from deerflow.persistence.execution_approvals import (
    ExecutionApprovalRequestRow,
    ExecutionApprovalResultReceiptRow,
)
from deerflow.runtime.host_execution_approval import (
    HostExecutionOutcome,
    HostExecutionPlan,
)
from deerflow.runtime.host_execution_domain import HostExecutionDomainSnapshot

_RESULT_TEXT_LIMIT = 20_000
_PRIVATE_ENVELOPE_SCHEMA_VERSION = 3
_RESULT_SCHEMA_VERSION = 1


def _bounded_text(value: str | None) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    if not isinstance(value, str):
        raise TypeError("host execution result fields must be strings or None")
    return value[:_RESULT_TEXT_LIMIT], len(value) > _RESULT_TEXT_LIMIT


def _private_envelope(
    plan: HostExecutionPlan,
    policy: HostExecutionProviderPolicySnapshot,
    execution_domain: HostExecutionDomainSnapshot,
) -> dict[str, object]:
    return {
        "schema_version": _PRIVATE_ENVELOPE_SCHEMA_VERSION,
        "plan": plan.to_private_payload(),
        "provider_policy": policy.to_payload(),
        "provider_policy_digest": policy.digest,
        "execution_domain": execution_domain.to_private_payload(),
    }


def _frozen_plan_from_row(
    row: ExecutionApprovalRequestRow,
) -> tuple[
    HostExecutionPlan,
    HostExecutionProviderPolicySnapshot,
    HostExecutionDomainSnapshot,
]:
    envelope = row.command_private_json
    expected = {
        "schema_version",
        "plan",
        "provider_policy",
        "provider_policy_digest",
        "execution_domain",
    }
    if not isinstance(envelope, dict) or set(envelope) != expected:
        raise ValueError("invalid host execution envelope")
    if envelope.get("schema_version") != _PRIVATE_ENVELOPE_SCHEMA_VERSION:
        raise ValueError("unsupported host execution envelope")
    policy = HostExecutionProviderPolicySnapshot.from_payload(
        envelope.get("provider_policy"),
    )
    if envelope.get("provider_policy_digest") != policy.digest:
        raise ValueError("provider policy snapshot digest mismatch")
    plan = HostExecutionPlan.from_private_payload(
        envelope.get("plan"),
        source_tool_call_id=row.tool_call_id,
        source_run_id=row.source_run_id,
        source_thread_id=row.thread_id,
    )
    if plan.execution_digest != row.command_digest:
        raise ValueError("frozen execution digest mismatch")
    if list(plan.agent_path) != row.source_agent_path:
        raise ValueError("frozen agent path mismatch")
    if plan.timeout_seconds != policy.execution_timeout_seconds:
        raise ValueError("frozen execution timeout does not match policy")
    execution_domain = HostExecutionDomainSnapshot.from_private_payload(
        envelope.get("execution_domain"),
    )
    if execution_domain.configured_id != policy.execution_domain_id:
        raise ValueError("execution domain does not match provider policy")
    if execution_domain.affinity != row.execution_domain_affinity:
        raise ValueError("execution domain does not match approval affinity")
    return plan, policy, execution_domain


def _result_payload(outcome: HostExecutionOutcome) -> dict[str, object]:
    stdout, stdout_truncated = _bounded_text(outcome.stdout)
    stderr, stderr_truncated = _bounded_text(outcome.stderr)
    result_text, result_text_truncated = _bounded_text(outcome.result_text)
    return {
        "schema_version": _RESULT_SCHEMA_VERSION,
        "status": outcome.status,
        "exit_code": outcome.exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "result_text": result_text,
        "reason_code": outcome.reason_code,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "result_text_truncated": result_text_truncated,
    }


def _outcome_from_receipt(
    row: ExecutionApprovalRequestRow,
    receipt: ExecutionApprovalResultReceiptRow | None,
) -> HostExecutionOutcome:
    if receipt is None:
        raise ValueError("terminal host execution receipt is missing")
    payload = receipt.result_private_json
    expected = {
        "schema_version",
        "status",
        "exit_code",
        "stdout",
        "stderr",
        "result_text",
        "reason_code",
        "stdout_truncated",
        "stderr_truncated",
        "result_text_truncated",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValueError("invalid host execution receipt")
    if payload.get("schema_version") != _RESULT_SCHEMA_VERSION:
        raise ValueError("unsupported host execution receipt")
    if _canonical_digest(payload) != receipt.result_digest:
        raise ValueError("host execution receipt digest mismatch")
    if row.status not in {"finished", "launch_failed"} or receipt.outcome != row.status or payload.get("status") != row.status or payload.get("exit_code") != receipt.exit_code or payload.get("reason_code") != receipt.public_error_code:
        raise ValueError("host execution receipt scope mismatch")
    for key in (
        "stdout_truncated",
        "stderr_truncated",
        "result_text_truncated",
    ):
        if type(payload.get(key)) is not bool:
            raise ValueError("invalid host execution receipt truncation flag")
    return HostExecutionOutcome(
        status=row.status,
        exit_code=payload.get("exit_code"),
        stdout=payload.get("stdout"),
        stderr=payload.get("stderr"),
        result_text=payload.get("result_text"),
        reason_code=payload.get("reason_code"),
    )
