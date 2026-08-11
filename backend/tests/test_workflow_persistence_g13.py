from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import fields, replace

import pytest

from app.workflows.authorization import WorkflowAction
from app.workflows.domain import (
    CodeLeaseState,
    EffectStatus,
    RunStatus,
    WorkflowStateConflict,
    ensure_code_lease_transition,
    ensure_effect_transition,
    ensure_manual_retry_allowed,
    ensure_run_transition,
)
from app.workflows.errors import (
    WORKFLOW_ERROR_STATUS,
    WorkflowDraftConflict,
    WorkflowForbidden,
    WorkflowNotFound,
    WorkflowRunConflict,
    WorkflowRunRetryForbidden,
)
from app.workflows.repository import (
    WorkflowCredentialSlotCreate,
    WorkflowDefinitionCreate,
    WorkflowDraftUpdate,
    WorkflowRepository,
    WorkflowRunAdmissionRequest,
    WorkflowRunCreate,
    WorkflowRunEventAppend,
    WorkflowRunEventRecord,
)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (RunStatus.QUEUED, RunStatus.QUEUED),
        (RunStatus.QUEUED, RunStatus.RUNNING),
        (RunStatus.QUEUED, RunStatus.CANCELLED),
        (RunStatus.RUNNING, RunStatus.RUNNING),
        (RunStatus.RUNNING, RunStatus.SUCCEEDED),
        (RunStatus.RUNNING, RunStatus.FAILED),
        (RunStatus.RUNNING, RunStatus.CANCELLED),
        (RunStatus.RUNNING, RunStatus.SIDE_EFFECT_UNKNOWN),
    ],
)
def test_workflow_run_transition_matrix_accepts_only_forward_edges(
    source: RunStatus,
    target: RunStatus,
) -> None:
    assert ensure_run_transition(source, target) is target


@pytest.mark.parametrize("terminal", list(RunStatus.terminal()))
def test_workflow_run_terminal_and_unknown_are_irreversible(
    terminal: RunStatus,
) -> None:
    with pytest.raises(WorkflowStateConflict):
        ensure_run_transition(terminal, terminal)
    with pytest.raises(WorkflowStateConflict):
        ensure_run_transition(terminal, RunStatus.RUNNING)

    if terminal is RunStatus.SIDE_EFFECT_UNKNOWN:
        with pytest.raises(WorkflowStateConflict):
            ensure_manual_retry_allowed(terminal)
    else:
        assert ensure_manual_retry_allowed(terminal) is terminal


def test_effect_and_code_lease_state_machines_are_closed() -> None:
    assert ensure_effect_transition(EffectStatus.PREPARED, EffectStatus.DISPATCHING) is EffectStatus.DISPATCHING
    assert ensure_effect_transition(EffectStatus.DISPATCHING, EffectStatus.UNKNOWN) is EffectStatus.UNKNOWN
    with pytest.raises(WorkflowStateConflict):
        ensure_effect_transition(EffectStatus.UNKNOWN, EffectStatus.SETTLED)

    assert ensure_code_lease_transition(CodeLeaseState.PROVISIONING, CodeLeaseState.RUNNING) is CodeLeaseState.RUNNING
    assert ensure_code_lease_transition(CodeLeaseState.RUNNING, CodeLeaseState.CLEANUP_PENDING) is CodeLeaseState.CLEANUP_PENDING
    assert ensure_code_lease_transition(CodeLeaseState.CLEANUP_PENDING, CodeLeaseState.DESTROYED) is CodeLeaseState.DESTROYED
    with pytest.raises(WorkflowStateConflict):
        ensure_code_lease_transition(CodeLeaseState.DESTROYED, CodeLeaseState.RUNNING)


def test_workflow_errors_have_stable_public_and_job_terminal_mappings() -> None:
    expected = {
        WorkflowNotFound: ("WORKFLOW_NOT_FOUND", 404),
        WorkflowForbidden: ("WORKFLOW_FORBIDDEN", 403),
        WorkflowDraftConflict: ("WORKFLOW_DRAFT_CONFLICT", 409),
        WorkflowRunConflict: ("WORKFLOW_RUN_CONFLICT", 409),
        WorkflowRunRetryForbidden: ("WORKFLOW_RUN_RETRY_FORBIDDEN", 409),
    }
    for error_type, (code, status) in expected.items():
        error = error_type("req-g13")
        assert error.code == code
        assert error.http_status == status
        assert WORKFLOW_ERROR_STATUS[code] == status
        assert error.job_terminal_code == code
        assert "req-g13" not in str(error)


def test_repository_is_session_bound_and_never_commits_or_targets_agent_tables() -> None:
    source = inspect.getsource(WorkflowRepository)
    module_source = inspect.getsource(inspect.getmodule(WorkflowRepository))
    assert ".commit(" not in source
    assert "deerflow.persistence.run.model" not in module_source
    assert "deerflow.persistence.thread_meta.model" not in module_source
    assert "deerflow.persistence.models.run_event" not in module_source
    assert "WorkflowRunEventRow" in source
    assert ".with_for_update" in inspect.getsource(WorkflowRepository._matching_job)
    assert not hasattr(WorkflowRepository, "delete_run")


def test_retry_remains_an_internal_domain_action_after_capability_mapping() -> None:
    assert WorkflowAction.RETRY.value == "retry"
    assert "run.retry_own" not in {action.value for action in WorkflowAction}
    assert all(not action.value.startswith("workflow.") for action in WorkflowAction)


def test_event_append_contract_rejects_authority_shape_drift() -> None:
    with pytest.raises(ValueError):
        WorkflowRunEventAppend(event_type="workflow.node.log", payload={})
    with pytest.raises(ValueError):
        WorkflowRunEventAppend(
            event_type="workflow.run.started",
            payload={},
            iteration_path=(1,),
        )
    valid = WorkflowRunEventAppend(
        event_type="workflow.node.log",
        payload={
            "node_type": "python_code",
            "stream": "stdout",
            "text": "safe",
            "truncated": False,
        },
        node_id="11111111-1111-1111-1111-111111111111",
        activation_id="activation-1",
        scope_path_hash="a" * 64,
        iteration_path=(1, 2),
        attempt=1,
    )
    assert valid.iteration_path == (1, 2)
    assert valid.materialize_payload() == {
        "node_type": "python_code",
        "stream": "stdout",
        "text": "safe",
        "truncated": False,
    }
    for invalid_activation_id in ("contains space", "激活-1"):
        with pytest.raises(ValueError):
            replace(valid, activation_id=invalid_activation_id)
    with pytest.raises(ValueError, match="PostgreSQL INTEGER"):
        replace(valid, iteration_path=(2_147_483_648,))
    with pytest.raises(ValueError, match="PostgreSQL INTEGER"):
        replace(valid, attempt=2_147_483_648)


def test_event_append_payload_is_deeply_frozen_and_materializes_detached_json() -> None:
    source = {
        "node_type": "llm",
        "duration_ms": 1,
        "output_preview": {
            "format": "json",
            "text": "{}",
            "truncated": False,
            "redacted": True,
        },
    }
    event = WorkflowRunEventAppend(
        event_type="workflow.node.completed",
        payload=source,
        node_id="11111111-1111-1111-1111-111111111111",
        activation_id="activation-frozen",
        scope_path_hash="a" * 64,
        attempt=1,
    )

    source["output_preview"]["text"] = "mutated source"  # type: ignore[index]
    with pytest.raises(TypeError):
        event.payload["duration_ms"] = 2  # type: ignore[index]
    frozen_preview = dict(event.payload.items)["output_preview"]  # type: ignore[union-attr]
    with pytest.raises(AttributeError):
        frozen_preview.items = ()  # type: ignore[union-attr]

    materialized = event.materialize_payload()
    materialized["output_preview"]["text"] = "mutated detached"  # type: ignore[index]
    assert event.materialize_payload()["output_preview"] == {
        "format": "json",
        "text": "{}",
        "truncated": False,
        "redacted": True,
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"raw_url": "https://example.invalid/?token=secret"},
        {"full_stdout": "secret"},
        {
            "node_type": "python_code",
            "stream": "stdout",
            "text": "safe",
            "truncated": False,
            "complete_output": "must-not-persist",
        },
    ],
)
def test_event_append_contract_rejects_unclosed_or_secret_shaped_payloads(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        WorkflowRunEventAppend(
            event_type="workflow.node.log",
            payload=payload,
            node_id="11111111-1111-1111-1111-111111111111",
            activation_id="activation-1",
            scope_path_hash="a" * 64,
            attempt=1,
        )

    with pytest.raises(ValueError):
        WorkflowRunEventAppend(
            event_type="workflow.run.completed",
            payload=payload,
        )


def test_event_append_contract_closes_all_eleven_public_event_shapes() -> None:
    run_events = {
        "workflow.run.started": {},
        "workflow.run.completed": {"duration_ms": 1},
        "workflow.run.failed": {
            "duration_ms": 1,
            "error": {
                "code": "WORKFLOW_INPUT_INVALID",
                "safe_message": "safe",
            },
        },
        "workflow.run.cancelled": {},
        "workflow.run.side_effect_unknown": {
            "code": "SIDE_EFFECT_STATE_UNKNOWN",
            "safe_message": "safe",
        },
    }
    node_events = {
        "workflow.node.queued": {"node_type": "llm"},
        "workflow.node.started": {"node_type": "llm"},
        "workflow.node.delta": {
            "node_type": "llm",
            "text": "chunk",
            "truncated": False,
        },
        "workflow.node.log": {
            "node_type": "python_code",
            "stream": "stdout",
            "text": "safe",
            "truncated": False,
        },
        "workflow.node.completed": {
            "node_type": "llm",
            "duration_ms": 1,
        },
        "workflow.node.failed": {
            "node_type": "llm",
            "duration_ms": 1,
            "error": {
                "code": "WORKFLOW_INPUT_INVALID",
                "safe_message": "safe",
            },
        },
    }
    for event_type, payload in run_events.items():
        event = WorkflowRunEventAppend(event_type=event_type, payload=payload)
        assert event.event_type == event_type
        assert event.materialize_payload() == payload
        with pytest.raises(TypeError):
            event.payload["unexpected"] = True  # type: ignore[index]
        with pytest.raises(ValueError):
            WorkflowRunEventAppend(
                event_type=event_type,
                payload=payload,
                node_id="11111111-1111-1111-1111-111111111111",
                activation_id="activation-1",
                scope_path_hash="a" * 64,
                attempt=1,
            )
    for event_type, payload in node_events.items():
        event = WorkflowRunEventAppend(
            event_type=event_type,
            payload=payload,
            node_id="11111111-1111-1111-1111-111111111111",
            activation_id="activation-1",
            scope_path_hash="a" * 64,
            iteration_path=(1,),
            attempt=1,
        )
        assert event.event_type == event_type
        assert event.materialize_payload() == payload
        with pytest.raises(TypeError):
            event.payload["unexpected"] = True  # type: ignore[index]
    assert len(run_events | node_events) == 11


def test_workflow_run_create_has_one_closed_authority_field_set() -> None:
    assert tuple(field.name for field in fields(WorkflowRunCreate)) == (
        "workflow_id",
        "workflow_version_id",
        "requested_workflow_version_id",
        "inputs",
        "input_digest",
        "idempotency_hash",
        "admission_request_digest",
        "trigger_kind",
        "trigger_ref",
        "origin_trace_id",
        "required_worker_profile_digest",
        "retry_of_run_id",
    )


def test_admission_request_digest_covers_only_canonical_client_coordinates() -> None:
    implicit = WorkflowRunAdmissionRequest(
        requested_workflow_version_id=None,
        inputs={"b": 2, "a": "e\u0301"},
        trigger_kind="manual",
        trigger_ref=None,
        retry_of_run_id=None,
    )
    reordered = WorkflowRunAdmissionRequest(
        requested_workflow_version_id=None,
        inputs={"a": "é", "b": 2},
        trigger_kind="manual",
        trigger_ref=None,
        retry_of_run_id=None,
    )
    explicit = WorkflowRunAdmissionRequest(
        requested_workflow_version_id="11111111-1111-1111-1111-111111111111",
        inputs={"a": "é", "b": 2},
        trigger_kind="manual",
        trigger_ref=None,
        retry_of_run_id=None,
    )
    retry = WorkflowRunAdmissionRequest(
        requested_workflow_version_id=None,
        inputs={"a": "é", "b": 2},
        trigger_kind="manual",
        trigger_ref=None,
        retry_of_run_id="22222222-2222-2222-2222-222222222222",
    )

    assert implicit.digest == reordered.digest
    assert implicit.digest != explicit.digest
    assert implicit.digest != retry.digest


def test_admission_request_digest_reuses_bounded_public_input_validation() -> None:
    with pytest.raises(ValueError, match="canonical UTF-8 byte count"):
        WorkflowRunAdmissionRequest(
            requested_workflow_version_id=None,
            inputs={"amplified": [5e-324] * 65_534},
            trigger_kind="manual",
            trigger_ref=None,
            retry_of_run_id=None,
        )


def test_admission_request_identity_is_stable_after_nested_input_mutation() -> None:
    source = {"nested": {"items": [1]}}
    request = WorkflowRunAdmissionRequest(
        requested_workflow_version_id=None,
        inputs=source,
        trigger_kind="manual",
        trigger_ref=None,
        retry_of_run_id=None,
    )
    digest = request.digest
    input_digest = request.input_digest

    source["nested"]["items"].append(2)
    assert request.materialize_inputs() == {"nested": {"items": [1]}}
    with pytest.raises(TypeError):
        request.inputs["other"] = "forbidden"  # type: ignore[index]
    nested = request.inputs["nested"]
    assert isinstance(nested, Mapping)
    items = nested["items"]
    assert isinstance(items, tuple)
    with pytest.raises(AttributeError):
        items.append(3)  # type: ignore[attr-defined]

    assert request.digest == digest
    assert request.input_digest == input_digest


def test_run_create_closes_inputs_and_both_persisted_request_digests() -> None:
    admission = WorkflowRunAdmissionRequest(
        requested_workflow_version_id=None,
        inputs={"question": "safe"},
        trigger_kind="manual",
        trigger_ref=None,
        retry_of_run_id=None,
    )
    command = WorkflowRunCreate(
        workflow_id="11111111-1111-1111-1111-111111111111",
        workflow_version_id="22222222-2222-2222-2222-222222222222",
        requested_workflow_version_id=None,
        inputs={"question": "safe"},
        input_digest=admission.input_digest,
        idempotency_hash="3" * 64,
        admission_request_digest=admission.digest,
        trigger_kind="manual",
        trigger_ref=None,
        origin_trace_id="workflow-g13-closed-material",
        required_worker_profile_digest=None,
        retry_of_run_id=None,
    )
    assert command.inputs == admission.inputs
    with pytest.raises(TypeError):
        command.inputs["question"] = "drift"  # type: ignore[index]
    assert command.materialize_inputs() == {"question": "safe"}

    with pytest.raises(ValueError, match="input_digest"):
        replace(command, input_digest="4" * 64)
    with pytest.raises(ValueError, match="admission_request_digest"):
        replace(command, admission_request_digest="5" * 64)


def test_explicit_run_version_selection_must_match_resolved_version() -> None:
    selected_version = "22222222-2222-2222-2222-222222222222"
    admission = WorkflowRunAdmissionRequest(
        requested_workflow_version_id=selected_version,
        inputs={"question": "safe"},
        trigger_kind="manual",
        trigger_ref=None,
        retry_of_run_id=None,
    )
    with pytest.raises(ValueError, match="requested_workflow_version_id"):
        WorkflowRunCreate(
            workflow_id="11111111-1111-1111-1111-111111111111",
            workflow_version_id="33333333-3333-3333-3333-333333333333",
            requested_workflow_version_id=selected_version,
            inputs={"question": "safe"},
            input_digest=admission.input_digest,
            idempotency_hash="3" * 64,
            admission_request_digest=admission.digest,
            trigger_kind="manual",
            trigger_ref=None,
            origin_trace_id="workflow-g13-explicit-version",
            required_worker_profile_digest=None,
            retry_of_run_id=None,
        )


def test_definition_draft_and_credential_json_are_strict_frozen_and_detached() -> None:
    spec_source = {"nodes": [{"id": "node-1", "config": {"value": 1}}]}
    canvas_source = {"viewport": {"x": 1, "selected": ["node-1"]}}
    schema_source = {
        "type": "object",
        "properties": {"token": {"type": "string"}},
        "required": ["token"],
    }
    definition = WorkflowDefinitionCreate(
        name="Frozen",
        description="",
        spec_schema_version=1,
        canvas_schema_version=1,
        spec=spec_source,
        canvas=canvas_source,
        draft_checksum="1" * 64,
    )
    update = WorkflowDraftUpdate(
        expected_revision=1,
        spec_schema_version=1,
        canvas_schema_version=1,
        spec=spec_source,
        canvas=canvas_source,
        draft_checksum="2" * 64,
    )
    slot = WorkflowCredentialSlotCreate(
        slot_id="credential",
        name="Credential",
        purpose="http",
        payload_schema=schema_source,
        payload_schema_checksum="3" * 64,
    )

    spec_source["nodes"][0]["config"]["value"] = 99  # type: ignore[index]
    canvas_source["viewport"]["selected"].append("node-2")  # type: ignore[index]
    schema_source["properties"]["token"]["type"] = "number"  # type: ignore[index]
    for frozen in (definition.spec, update.spec, definition.canvas, update.canvas, slot.payload_schema):
        with pytest.raises(TypeError):
            frozen["mutated"] = True  # type: ignore[index]
    frozen_nodes = dict(definition.spec.items)["nodes"]  # type: ignore[union-attr]
    with pytest.raises(TypeError):
        frozen_nodes[0] = "mutated"  # type: ignore[index]
    with pytest.raises(AttributeError):
        frozen_nodes[0].items = ()  # type: ignore[union-attr]

    assert definition.materialize_spec()["nodes"][0]["config"] == {"value": 1}  # type: ignore[index]
    assert update.materialize_canvas()["viewport"]["selected"] == ["node-1"]  # type: ignore[index]
    assert slot.materialize_payload_schema()["properties"]["token"] == {"type": "string"}  # type: ignore[index]

    detached = definition.materialize_spec()
    detached["nodes"][0]["config"]["value"] = 7  # type: ignore[index]
    assert definition.materialize_spec()["nodes"][0]["config"] == {"value": 1}  # type: ignore[index]

    invalid_json_values = (
        {"tuple": (1, 2)},
        {"non_string_key": {1: "value"}},
        {"number": float("nan")},
        {"object": object()},
    )
    for invalid in invalid_json_values:
        with pytest.raises((TypeError, ValueError)):
            WorkflowDefinitionCreate(
                name="Invalid JSON",
                description="",
                spec_schema_version=1,
                canvas_schema_version=1,
                spec=invalid,
                canvas={},
                draft_checksum="4" * 64,
            )
    with pytest.raises(TypeError):
        WorkflowDraftUpdate(
            expected_revision=1,
            spec_schema_version=1,
            canvas_schema_version=1,
            spec={},
            canvas={"python_tuple": (1,)},
            draft_checksum="5" * 64,
        )
    with pytest.raises(TypeError):
        WorkflowCredentialSlotCreate(
            slot_id="invalid",
            name="Invalid",
            purpose="http",
            payload_schema={"python_set": {1}},
            payload_schema_checksum="6" * 64,
        )


def test_publish_checks_locked_draft_schema_before_existing_version_lookup() -> None:
    source = inspect.getsource(WorkflowRepository.publish_version)
    schema_guard = source.index("draft.spec_schema_version")
    existing_lookup = source.index("existing =")
    assert schema_guard < existing_lookup


def test_event_record_retains_complete_envelope_identity() -> None:
    assert tuple(field.name for field in fields(WorkflowRunEventRecord)) == (
        "event_id",
        "run_id",
        "workflow_version_id",
        "seq",
        "event_type",
        "node_id",
        "activation_id",
        "scope_path_hash",
        "iteration_path",
        "attempt",
        "payload",
        "occurred_at",
    )
