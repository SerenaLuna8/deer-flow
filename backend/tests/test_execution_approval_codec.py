"""Pure codec contracts for Execution Approval private envelopes and receipts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.private_work import execution_approval as legacy
from app.private_work import execution_approval_codec as codec
from app.private_work.execution_approval_codec import (
    _RESULT_TEXT_LIMIT,
    _bounded_text,
    _frozen_plan_from_row,
    _outcome_from_receipt,
    _private_envelope,
    _result_payload,
)
from app.private_work.execution_approval_policy import (
    HostExecutionProviderPolicySnapshot,
    _canonical_digest,
)
from deerflow.runtime.host_execution_approval import (
    HostExecutionOutcome,
    HostExecutionPlan,
)
from deerflow.runtime.host_execution_domain import HostExecutionDomainSnapshot

_CODEC_NAMES = (
    "_RESULT_TEXT_LIMIT",
    "_PRIVATE_ENVELOPE_SCHEMA_VERSION",
    "_RESULT_SCHEMA_VERSION",
    "_bounded_text",
    "_private_envelope",
    "_frozen_plan_from_row",
    "_result_payload",
    "_outcome_from_receipt",
)


def _plan(**overrides: object) -> HostExecutionPlan:
    values: dict[str, object] = {
        "source_tool_call_id": "call-1",
        "source_run_id": "run-1",
        "source_thread_id": "thread-1",
        "description": "list the workspace",
        "requested_command": "ls -la",
        "effective_command": "ls -la",
        "shell": "/bin/zsh",
        "cwd": "/mnt/user-data/workspace",
        "timeout_seconds": 60,
        "agent_path": ("lead",),
    }
    values.update(overrides)
    return HostExecutionPlan(**values)  # type: ignore[arg-type]


def _policy() -> HostExecutionProviderPolicySnapshot:
    return HostExecutionProviderPolicySnapshot(
        provider_use="deerflow.sandbox.local:LocalSandboxProvider",
        host_execution_mode="local_approval_required",
        allow_host_bash=False,
        bash_command_timeout=60,
        approval_max_timeout_seconds=60,
        request_ttl_seconds=300,
        execution_domain_id="mac-primary",
    )


def _domain() -> HostExecutionDomainSnapshot:
    return HostExecutionDomainSnapshot(
        configured_id="mac-primary",
        public_label="Worker host environment",
        os_name="posix",
        sys_platform="darwin",
        machine="arm64",
        device_fingerprint="d" * 64,
        environment_fingerprint="f" * 64,
        euid=501,
        egid=20,
        runtime_base_dir="/srv/actweave-runtime-a",
    )


def _row(
    plan: HostExecutionPlan,
    policy: HostExecutionProviderPolicySnapshot,
    domain: HostExecutionDomainSnapshot,
) -> SimpleNamespace:
    # The smallest database-free stand-in: exactly the row attributes the codec reads.
    return SimpleNamespace(
        command_private_json=_private_envelope(plan, policy, domain),
        tool_call_id=plan.source_tool_call_id,
        source_run_id=plan.source_run_id,
        thread_id=plan.source_thread_id,
        command_digest=plan.execution_digest,
        source_agent_path=list(plan.agent_path),
        execution_domain_affinity=domain.affinity,
    )


def test_codec_objects_are_exact_legacy_reexports() -> None:
    for name in _CODEC_NAMES:
        assert getattr(legacy, name) is getattr(codec, name), name
    assert _RESULT_TEXT_LIMIT == 20_000
    assert codec._PRIVATE_ENVELOPE_SCHEMA_VERSION == 3
    assert codec._RESULT_SCHEMA_VERSION == 1


def test_frozen_plan_round_trips_through_the_private_envelope() -> None:
    plan, policy, domain = _plan(), _policy(), _domain()
    row = _row(plan, policy, domain)
    assert set(vars(row)) == {
        "command_private_json",
        "tool_call_id",
        "source_run_id",
        "thread_id",
        "command_digest",
        "source_agent_path",
        "execution_domain_affinity",
    }

    decoded_plan, decoded_policy, decoded_domain = _frozen_plan_from_row(row)

    assert decoded_plan == plan
    assert decoded_policy == policy
    assert decoded_domain == domain
    assert row.command_private_json["provider_policy_digest"] == policy.digest
    assert row.command_private_json["schema_version"] == codec._PRIVATE_ENVELOPE_SCHEMA_VERSION


def _drift_provider_policy_digest(row: SimpleNamespace) -> None:
    row.command_private_json["provider_policy_digest"] = "0" * 64


def _drift_plan_digest(row: SimpleNamespace) -> None:
    row.command_digest = "0" * 64


def _drift_agent_path(row: SimpleNamespace) -> None:
    row.source_agent_path = ["lead", "researcher"]


def _drift_execution_domain_affinity(row: SimpleNamespace) -> None:
    row.execution_domain_affinity = "a" * 64


def _drift_timeout(row: SimpleNamespace) -> None:
    shorter = _plan(timeout_seconds=30)
    row.command_private_json["plan"] = shorter.to_private_payload()
    row.command_digest = shorter.execution_digest


@pytest.mark.parametrize(
    ("drift", "message"),
    [
        (_drift_provider_policy_digest, "provider policy snapshot digest mismatch"),
        (_drift_plan_digest, "frozen execution digest mismatch"),
        (_drift_agent_path, "frozen agent path mismatch"),
        (_drift_execution_domain_affinity, "execution domain does not match approval affinity"),
        (_drift_timeout, "frozen execution timeout does not match policy"),
    ],
    ids=["provider-policy-digest", "plan-digest", "agent-path", "execution-domain-affinity", "timeout"],
)
def test_frozen_plan_rejects_one_drifted_field_at_a_time(
    drift: Callable[[SimpleNamespace], None],
    message: str,
) -> None:
    row = _row(_plan(), _policy(), _domain())
    drift(row)

    with pytest.raises(ValueError, match=message):
        _frozen_plan_from_row(row)


def test_result_payload_truncates_each_text_field_at_the_limit() -> None:
    long_text = "x" * (_RESULT_TEXT_LIMIT + 1)
    outcome = HostExecutionOutcome(
        status="finished",
        exit_code=0,
        stdout=long_text,
        stderr=long_text,
        result_text=long_text,
    )

    payload = _result_payload(outcome)

    for key in ("stdout", "stderr", "result_text"):
        assert len(payload[key]) == 20_000
        assert payload[key] == "x" * 20_000
        assert payload[f"{key}_truncated"] is True
    assert payload["schema_version"] == codec._RESULT_SCHEMA_VERSION
    assert payload["status"] == "finished"
    assert payload["exit_code"] == 0
    assert payload["reason_code"] is None

    exact = "y" * _RESULT_TEXT_LIMIT
    assert _bounded_text(exact) == (exact, False)
    assert _bounded_text(None) == (None, False)
    with pytest.raises(TypeError):
        _bounded_text(123)  # type: ignore[arg-type]


def _receipt(payload: dict[str, object], **overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "result_private_json": payload,
        "result_digest": _canonical_digest(payload),
        "outcome": payload["status"],
        "exit_code": payload["exit_code"],
        "public_error_code": payload["reason_code"],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_outcome_from_receipt_round_trips_and_fails_closed() -> None:
    outcome = HostExecutionOutcome(status="finished", exit_code=0, stdout="ok", result_text="done")
    payload = _result_payload(outcome)
    row = SimpleNamespace(status="finished")

    assert _outcome_from_receipt(row, _receipt(payload)) == outcome

    with pytest.raises(ValueError, match="terminal host execution receipt is missing"):
        _outcome_from_receipt(row, None)
    with pytest.raises(ValueError, match="host execution receipt digest mismatch"):
        _outcome_from_receipt(row, _receipt(payload, result_digest="0" * 64))
    with pytest.raises(ValueError, match="host execution receipt scope mismatch"):
        _outcome_from_receipt(row, _receipt(payload, outcome="launch_failed"))
    with pytest.raises(ValueError, match="host execution receipt scope mismatch"):
        _outcome_from_receipt(row, _receipt(payload, exit_code=1))
    with pytest.raises(ValueError, match="host execution receipt scope mismatch"):
        _outcome_from_receipt(SimpleNamespace(status="pending"), _receipt(payload))

    launch_failed = replace(outcome, status="launch_failed", exit_code=None, reason_code="SPAWN_DENIED")
    failed_payload = _result_payload(launch_failed)
    assert _outcome_from_receipt(SimpleNamespace(status="launch_failed"), _receipt(failed_payload)) == launch_failed

    tampered = dict(payload)
    tampered["stdout_truncated"] = "false"
    with pytest.raises(ValueError, match="invalid host execution receipt truncation flag"):
        _outcome_from_receipt(row, _receipt(tampered))
    unsupported = dict(payload)
    unsupported["schema_version"] = 99
    with pytest.raises(ValueError, match="unsupported host execution receipt"):
        _outcome_from_receipt(row, _receipt(unsupported))
