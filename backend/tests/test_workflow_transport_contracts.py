from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.workflows.contracts import (
    WORKFLOW_EXECUTION_REFERENCE_V1_ADAPTER,
    WORKFLOW_HTTP_SETTLED_OUTCOME_V1_ADAPTER,
    WORKFLOW_PROJECT_READINESS_V1_ADAPTER,
    WORKFLOW_RUN_STATUS_V1_ADAPTER,
    SafePreviewV1,
    WorkflowErrorCode,
    WorkflowEventEnvelopeV1,
    WorkflowJobExecutionContextV1,
    WorkflowNodeLastRunV1,
    WorkflowValidationIssueV1,
)
from deerflow.workflows import canonical_json_value

_SHARED_HTTP_OUTCOME_FIXTURE = Path(__file__).resolve().parents[2] / "frontend/tests/fixtures/workflows/workflow-http-outcomes-v1.json"
_SHARED_RUN_INVALID_FIXTURE = Path(__file__).resolve().parents[2] / "frontend/tests/fixtures/workflows/workflow-run-invalid-v1.json"


@pytest.mark.parametrize(
    "payload",
    [
        {
            "status": "ready",
            "code": "WORKFLOW_CONTROL_PLANE_READY",
            "workflow_enabled": True,
            "schema_ready": True,
            "admission_ready": True,
            "request_id": "req-ready",
        },
        {
            "status": "ready",
            "code": "WORKFLOW_CONTROL_PLANE_READY",
            "workflow_enabled": True,
            "schema_ready": True,
            "admission_ready": False,
            "request_id": "req-worker-offline",
        },
        {
            "status": "ready",
            "code": "WORKFLOW_DISABLED",
            "workflow_enabled": False,
            "schema_ready": True,
            "admission_ready": False,
            "request_id": "req-disabled",
        },
        {
            "status": "unavailable",
            "code": "WORKFLOW_SCHEMA_UNAVAILABLE",
            "workflow_enabled": False,
            "schema_ready": False,
            "admission_ready": False,
            "request_id": "req-schema",
        },
        {
            "status": "unavailable",
            "code": "WORKFLOW_POLICY_UNAVAILABLE",
            "workflow_enabled": False,
            "schema_ready": True,
            "admission_ready": False,
            "request_id": "req-policy",
        },
    ],
)
def test_readiness_accepts_only_the_frozen_combinations(payload: dict[str, object]) -> None:
    parsed = WORKFLOW_PROJECT_READINESS_V1_ADAPTER.validate_python(payload)

    assert parsed.code == payload["code"]


@pytest.mark.parametrize(
    "change",
    [
        {"status": "ready", "code": "WORKFLOW_SCHEMA_UNAVAILABLE"},
        {"workflow_enabled": True, "code": "WORKFLOW_DISABLED"},
        {"schema_ready": False, "code": "WORKFLOW_POLICY_UNAVAILABLE"},
        {"admission_ready": True, "code": "WORKFLOW_DISABLED"},
        {"workflow_enabled": 1},
        {"provider_id": "must-not-leak"},
    ],
)
def test_readiness_rejects_contradictions_coercion_and_private_fields(change: dict[str, object]) -> None:
    payload: dict[str, object] = {
        "status": "ready",
        "code": "WORKFLOW_CONTROL_PLANE_READY",
        "workflow_enabled": True,
        "schema_ready": True,
        "admission_ready": False,
        "request_id": "req",
    }
    payload.update(change)

    with pytest.raises(ValidationError):
        WORKFLOW_PROJECT_READINESS_V1_ADAPTER.validate_python(payload)


def _node_event_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": uuid.uuid4(),
        "workflow_version_id": uuid.uuid4(),
        "seq": "42",
        "type": "workflow.node.started",
        "node_id": uuid.uuid4(),
        "activation_id": "activation-01",
        "scope_path_hash": "a" * 64,
        "iteration_path": (3,),
        "attempt": 1,
        "occurred_at": datetime.now(UTC),
        "payload": {"node_type": "llm"},
    }


def test_event_envelope_preserves_canonical_cursor_and_activation_identity() -> None:
    event = WorkflowEventEnvelopeV1.model_validate(_node_event_payload())

    assert event.seq == "42"
    assert event.iteration_path == (3,)
    assert event.attempt == 1

    maximum = _node_event_payload()
    maximum["seq"] = "9223372036854775807"
    assert WorkflowEventEnvelopeV1.model_validate(maximum).seq == "9223372036854775807"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("seq", 42),
        ("seq", "042"),
        ("seq", "-1"),
        ("seq", "9223372036854775808"),
        ("seq", "9" * 256),
        ("attempt", 0),
        ("attempt", 2_147_483_648),
        ("iteration_path", (0,)),
        ("iteration_path", (2_147_483_648,)),
        ("activation_id", ""),
        ("activation_id", "invalid activation"),
        ("origin_trace_id", "server-private"),
    ],
)
def test_event_envelope_rejects_noncanonical_or_private_fields(field: str, value: object) -> None:
    payload = _node_event_payload()
    payload[field] = value

    with pytest.raises(ValidationError):
        WorkflowEventEnvelopeV1.model_validate(payload)


def test_node_event_requires_node_activation_attempt_fields() -> None:
    payload = _node_event_payload()
    payload.pop("activation_id")

    with pytest.raises(ValidationError):
        WorkflowEventEnvelopeV1.model_validate(payload)


def test_event_identity_requires_an_explicit_iteration_path() -> None:
    payload = _node_event_payload()
    payload.pop("iteration_path")

    with pytest.raises(ValidationError):
        WorkflowEventEnvelopeV1.model_validate(payload)

    run_payload = _node_event_payload()
    run_payload.update(
        {
            "type": "workflow.run.started",
            "node_id": None,
            "activation_id": None,
            "scope_path_hash": None,
            "attempt": None,
            "payload": {},
        }
    )
    run_payload.pop("iteration_path")

    with pytest.raises(ValidationError):
        WorkflowEventEnvelopeV1.model_validate(run_payload)


def test_event_payload_is_type_specific_and_rejects_private_or_raw_fields() -> None:
    payload = _node_event_payload()
    payload["payload"] = {
        "node_type": "llm",
        "credential_id": str(uuid.uuid4()),
    }

    with pytest.raises(ValidationError):
        WorkflowEventEnvelopeV1.model_validate(payload)

    log_payload = _node_event_payload()
    log_payload["type"] = "workflow.node.log"
    log_payload["payload"] = {
        "node_type": "python_code",
        "stream": "stdout",
        "text": "safe log tail",
        "truncated": False,
    }
    event = WorkflowEventEnvelopeV1.model_validate(log_payload)
    assert event.payload["stream"] == "stdout"

    log_payload["payload"]["source"] = "def main(inputs): ..."
    with pytest.raises(ValidationError):
        WorkflowEventEnvelopeV1.model_validate(log_payload)


def test_run_event_rejects_node_activation_fields() -> None:
    payload = _node_event_payload()
    payload["type"] = "workflow.run.completed"

    with pytest.raises(ValidationError):
        WorkflowEventEnvelopeV1.model_validate(payload)


def test_run_event_accepts_only_its_safe_payload_shape() -> None:
    payload = _node_event_payload()
    payload.update(
        {
            "type": "workflow.run.completed",
            "node_id": None,
            "activation_id": None,
            "scope_path_hash": None,
            "iteration_path": (),
            "attempt": None,
            "payload": {
                "duration_ms": 42,
                "output_preview": {
                    "format": "summary",
                    "text": "完成",
                    "truncated": False,
                    "redacted": False,
                },
            },
        }
    )

    assert WorkflowEventEnvelopeV1.model_validate(payload).type == "workflow.run.completed"

    payload["payload"]["output_json"] = {"secret": "full output"}
    with pytest.raises(ValidationError):
        WorkflowEventEnvelopeV1.model_validate(payload)


def test_numeric_event_metadata_must_fit_the_cross_runtime_safe_integer_range() -> None:
    payload = _node_event_payload()
    payload["attempt"] = 9_007_199_254_740_992

    with pytest.raises(ValidationError):
        WorkflowEventEnvelopeV1.model_validate(payload)


def test_safe_preview_and_last_run_are_strict_bounded_runtime_projections() -> None:
    preview = SafePreviewV1(
        format="json",
        text='{"ok":true}',
        truncated=False,
        redacted=True,
        original_byte_count=11,
    )
    last_run = WorkflowNodeLastRunV1(
        run_id=uuid.uuid4(),
        node_id=uuid.uuid4(),
        activation_id="activation-01",
        iteration_path=(1,),
        attempt=2,
        status="succeeded",
        duration_ms=12,
        output_preview=preview,
        retry_count=1,
    )

    assert last_run.output_preview == preview

    with pytest.raises(ValidationError):
        SafePreviewV1.model_validate(
            {
                "format": "text",
                "text": "safe",
                "truncated": False,
                "redacted": False,
                "credential_id": str(uuid.uuid4()),
            }
        )


def test_safe_preview_and_event_text_limits_are_utf8_byte_limits() -> None:
    assert SafePreviewV1(
        format="text",
        text="a" * 65_536,
        truncated=False,
        redacted=False,
        original_byte_count=65_536,
    ).text.endswith("a")

    with pytest.raises(ValidationError):
        SafePreviewV1(
            format="text",
            text="😀" * 16_385,
            truncated=True,
            redacted=False,
            original_byte_count=65_540,
        )

    with pytest.raises(ValidationError):
        SafePreviewV1(
            format="text",
            text="完成",
            truncated=False,
            redacted=False,
            original_byte_count=1,
        )

    log_payload = _node_event_payload()
    log_payload["type"] = "workflow.node.log"
    log_payload["payload"] = {
        "node_type": "python_code",
        "stream": "stdout",
        "text": "😀" * 16_385,
        "truncated": True,
    }
    with pytest.raises(ValidationError):
        WorkflowEventEnvelopeV1.model_validate(log_payload)


def test_validation_issue_has_stable_location_without_private_authority() -> None:
    issue = WorkflowValidationIssueV1(
        severity="error",
        code="WORKFLOW_PORT_NOT_FOUND",
        message="端口不存在",
        path=("nodes", "0", "input_bindings", "prompt"),
        node_id=uuid.uuid4(),
        edge_id="edge-1",
        port_id="prompt",
    )

    assert issue.severity == "error"
    assert issue.edge_id == "edge-1"

    with pytest.raises(ValidationError):
        WorkflowValidationIssueV1.model_validate(
            {
                **issue.model_dump(),
                "project_id": uuid.uuid4(),
            }
        )


@pytest.mark.parametrize(
    "uuid_case",
    json.loads(_SHARED_RUN_INVALID_FIXTURE.read_text(encoding="utf-8"))["uuid_values"],
    ids=lambda case: case["id"],
)
def test_every_transport_dto_rejects_noncanonical_uuid_text(uuid_case: dict[str, str]) -> None:
    invalid_uuid = uuid_case["value"]
    issue = WorkflowValidationIssueV1(
        severity="error",
        code="WORKFLOW_PORT_NOT_FOUND",
        message="端口不存在",
        path=("nodes", "0"),
        node_id=uuid.uuid4(),
    ).model_dump(mode="json")
    event = WorkflowEventEnvelopeV1.model_validate(_node_event_payload()).model_dump(mode="json")
    last_run = WorkflowNodeLastRunV1(
        run_id=uuid.uuid4(),
        node_id=uuid.uuid4(),
        activation_id="activation-01",
        iteration_path=(1,),
        attempt=1,
        status="running",
    ).model_dump(mode="json")
    context = WorkflowJobExecutionContextV1(
        job_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        origin_trace_id="trace-uuid-contract",
        execution_reference={
            "kind": "workflow_run",
            "workflow_run_id": uuid.uuid4(),
            "workflow_epoch": 1,
            "required_worker_profile_digest": None,
        },
    ).model_dump(mode="json")

    for model, payload, fields in (
        (WorkflowValidationIssueV1, issue, ("node_id",)),
        (WorkflowEventEnvelopeV1, event, ("run_id", "workflow_version_id", "node_id")),
        (WorkflowNodeLastRunV1, last_run, ("run_id", "node_id")),
        (WorkflowJobExecutionContextV1, context, ("job_id", "project_id", "owner_user_id")),
    ):
        for field in fields:
            invalid = {**payload, field: invalid_uuid}
            with pytest.raises(ValidationError):
                model.model_validate_json(json.dumps(invalid))


def test_execution_reference_is_a_strict_agent_or_workflow_union() -> None:
    agent_run_id = uuid.uuid4()
    workflow_run_id = uuid.uuid4()

    agent = WORKFLOW_EXECUTION_REFERENCE_V1_ADAPTER.validate_python({"kind": "agent_run", "run_id": agent_run_id})
    workflow = WORKFLOW_EXECUTION_REFERENCE_V1_ADAPTER.validate_python(
        {
            "kind": "workflow_run",
            "workflow_run_id": workflow_run_id,
            "workflow_epoch": 2,
            "required_worker_profile_digest": "b" * 64,
        }
    )

    assert agent.run_id == agent_run_id
    assert workflow.workflow_run_id == workflow_run_id

    with pytest.raises(ValidationError):
        WORKFLOW_EXECUTION_REFERENCE_V1_ADAPTER.validate_python(
            {
                "kind": "workflow_run",
                "run_id": agent_run_id,
                "workflow_run_id": workflow_run_id,
                "workflow_epoch": 1,
            }
        )


def test_server_private_job_context_binds_trace_scope_and_execution_reference() -> None:
    context = WorkflowJobExecutionContextV1(
        job_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        origin_trace_id=uuid.uuid4().hex,
        execution_reference={
            "kind": "workflow_run",
            "workflow_run_id": uuid.uuid4(),
            "workflow_epoch": 1,
            "required_worker_profile_digest": None,
        },
    )

    assert context.execution_reference.kind == "workflow_run"

    with pytest.raises(ValidationError):
        WorkflowJobExecutionContextV1.model_validate(
            {
                **context.model_dump(),
                "origin_trace_id": "unsafe\ntrace",
            }
        )


def test_first_wave_run_statuses_exclude_future_waiting_state() -> None:
    for status in ("queued", "running", "succeeded", "failed", "cancelled", "side_effect_unknown"):
        assert WORKFLOW_RUN_STATUS_V1_ADAPTER.validate_python(status) == status

    with pytest.raises(ValidationError):
        WORKFLOW_RUN_STATUS_V1_ADAPTER.validate_python("waiting_input")


def test_public_error_codes_are_frozen_values() -> None:
    assert WorkflowErrorCode.SIDE_EFFECT_STATE_UNKNOWN == "SIDE_EFFECT_STATE_UNKNOWN"
    assert WorkflowErrorCode.WORKFLOW_LOOP_LIMIT_EXCEEDED == "WORKFLOW_LOOP_LIMIT_EXCEEDED"

    with pytest.raises(ValueError):
        WorkflowErrorCode("WORKFLOW_PROVIDER_INTERNAL_PATH")


def _settled_http_response() -> dict[str, object]:
    return {
        "status_code": 200,
        "headers": (
            {"name": "content-type", "value": "application/json"},
            {"name": "x-request-label", "value": "safe-label"},
        ),
        "body": {"kind": "json", "value": {"ok": True}},
        "duration_ms": 42,
        "wire_byte_count": {"value": 17, "relation": "exact"},
        "decoded_byte_count": {"value": 11, "relation": "exact"},
        "retained_body_byte_count": 11,
    }


def test_http_settled_outcome_matches_the_shared_python_typescript_corpus() -> None:
    values = json.loads(_SHARED_HTTP_OUTCOME_FIXTURE.read_text(encoding="utf-8"))
    assert [WORKFLOW_HTTP_SETTLED_OUTCOME_V1_ADAPTER.validate_json(json.dumps(value)).kind for value in values] == [
        "success",
        "http_error",
        "response_invalid",
    ]


@pytest.mark.parametrize("kind", ["success", "http_error"])
def test_http_settled_outcome_persists_a_replayable_typed_response(kind: str) -> None:
    outcome = WORKFLOW_HTTP_SETTLED_OUTCOME_V1_ADAPTER.validate_python({"kind": kind, "response": _settled_http_response()})

    assert outcome.kind == kind
    assert outcome.response.status_code == 200
    assert outcome.response.body.kind == "json"

    # JSONB is revalidated through the JSON contract rather than trusted as an
    # already-typed Python object during crash recovery.
    replayed = WORKFLOW_HTTP_SETTLED_OUTCOME_V1_ADAPTER.validate_json(json.dumps({"kind": kind, "response": _settled_http_response()}))
    assert replayed == outcome


def test_http_response_invalid_outcome_keeps_only_stable_safe_metadata() -> None:
    outcome = WORKFLOW_HTTP_SETTLED_OUTCOME_V1_ADAPTER.validate_python(
        {
            "kind": "response_invalid",
            "status_code": 200,
            "duration_ms": 42,
            "wire_byte_count": {"value": 17, "relation": "exact"},
            "decoded_byte_count": {"value": 11, "relation": "exact"},
            "error": {
                "code": WorkflowErrorCode.WORKFLOW_HTTP_RESPONSE_INVALID,
                "safe_message": "响应不符合已发布的输出结构",
                "line": None,
                "column": None,
            },
        }
    )

    assert outcome.kind == "response_invalid"


def test_http_settled_outcome_enforces_utf8_shape_and_material_byte_bounds() -> None:
    multibyte_header = {"kind": "success", "response": _settled_http_response()}
    multibyte_header["response"]["headers"] = ({"name": "x-label", "value": "😀" * 1_025},)  # type: ignore[index]
    with pytest.raises(ValidationError):
        WORKFLOW_HTTP_SETTLED_OUTCOME_V1_ADAPTER.validate_python(multibyte_header)

    undersized_count = {"kind": "success", "response": _settled_http_response()}
    undersized_count["response"]["body"] = {"kind": "text", "text": "完成"}  # type: ignore[index]
    undersized_count["response"]["retained_body_byte_count"] = 1  # type: ignore[index]
    with pytest.raises(ValidationError):
        WORKFLOW_HTTP_SETTLED_OUTCOME_V1_ADAPTER.validate_python(undersized_count)

    oversized_json = {"kind": "success", "response": _settled_http_response()}
    oversized_json["response"]["body"] = {"kind": "json", "value": {"payload": "x" * 2_097_152}}  # type: ignore[index]
    with pytest.raises(ValidationError):
        WORKFLOW_HTTP_SETTLED_OUTCOME_V1_ADAPTER.validate_python(oversized_json)

    deeply_nested: object = None
    for _ in range(65):
        deeply_nested = [deeply_nested]
    deep_json = {"kind": "success", "response": _settled_http_response()}
    deep_json["response"]["body"] = {"kind": "json", "value": deeply_nested}  # type: ignore[index]
    with pytest.raises(ValidationError):
        WORKFLOW_HTTP_SETTLED_OUTCOME_V1_ADAPTER.validate_python(deep_json)

    amplified_json = {"kind": "success", "response": _settled_http_response()}
    amplified_json["response"]["body"] = {"kind": "json", "value": [5e-324] * 65_535}  # type: ignore[index]
    with pytest.raises(ValidationError, match="persisted byte limit"):
        WORKFLOW_HTTP_SETTLED_OUTCOME_V1_ADAPTER.validate_python(amplified_json)


@pytest.mark.parametrize(
    ("path", "field", "value"),
    [
        (("response",), "status_code", 99),
        (("response", "headers", 0), "name", "authorization"),
        (("response", "headers", 0), "name", "Set-Cookie"),
        (("response",), "request_url", "https://secret.example/path"),
        ((), "credential_id", "must-not-persist"),
    ],
)
def test_http_settled_outcome_rejects_unbounded_or_secret_transport_fields(path: tuple[str | int, ...], field: str, value: object) -> None:
    payload: dict[str, object] = {"kind": "success", "response": _settled_http_response()}
    target: object = payload
    for segment in path:
        target = target[segment]  # type: ignore[index]
    target[field] = value  # type: ignore[index]

    with pytest.raises(ValidationError):
        WORKFLOW_HTTP_SETTLED_OUTCOME_V1_ADAPTER.validate_python(payload)


def test_response_invalid_rejects_non_http_validation_errors() -> None:
    with pytest.raises(ValidationError):
        WORKFLOW_HTTP_SETTLED_OUTCOME_V1_ADAPTER.validate_python(
            {
                "kind": "response_invalid",
                "status_code": 200,
                "duration_ms": 1,
                "wire_byte_count": {"value": 1, "relation": "exact"},
                "decoded_byte_count": {"value": 1, "relation": "exact"},
                "error": {
                    "code": WorkflowErrorCode.WORKFLOW_HTTP_TIMEOUT,
                    "safe_message": "not a settled validation outcome",
                },
            }
        )


def test_http_settled_outcome_separates_raw_counts_from_canonical_json_material() -> None:
    payload = {"kind": "success", "response": _settled_http_response()}
    payload["response"]["body"] = {"kind": "json", "value": 1e-7}  # type: ignore[index]
    payload["response"]["wire_byte_count"] = {"value": 4, "relation": "exact"}  # type: ignore[index]
    payload["response"]["decoded_byte_count"] = {"value": 4, "relation": "exact"}  # type: ignore[index]
    payload["response"]["retained_body_byte_count"] = len(canonical_json_value(1e-7).encode("utf-8"))  # type: ignore[index]

    assert WORKFLOW_HTTP_SETTLED_OUTCOME_V1_ADAPTER.validate_python(payload).kind == "success"


def test_http_response_limit_uses_a_capped_at_least_observation() -> None:
    payload = {
        "kind": "response_invalid",
        "status_code": 200,
        "duration_ms": 1,
        "wire_byte_count": {"value": 2_097_152, "relation": "at_least"},
        "decoded_byte_count": {"value": 2_097_152, "relation": "at_least"},
        "error": {
            "code": WorkflowErrorCode.WORKFLOW_HTTP_RESPONSE_LIMIT,
            "safe_message": "response exceeded the frozen limit",
        },
    }
    assert WORKFLOW_HTTP_SETTLED_OUTCOME_V1_ADAPTER.validate_python(payload).kind == "response_invalid"

    payload["wire_byte_count"] = {"value": 2_097_152, "relation": "exact"}
    payload["decoded_byte_count"] = {"value": 2_097_152, "relation": "exact"}
    with pytest.raises(ValidationError):
        WORKFLOW_HTTP_SETTLED_OUTCOME_V1_ADAPTER.validate_python(payload)
