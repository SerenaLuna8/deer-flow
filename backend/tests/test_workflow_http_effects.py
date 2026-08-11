from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.workflows.contracts import WORKFLOW_HTTP_SETTLED_OUTCOME_V1_ADAPTER
from app.workflows.http_effects import (
    SIDE_EFFECT_UNKNOWN_PUBLIC_RETRY_ALLOWED,
    WorkflowHttpCredentialGrantLiveStateV1,
    WorkflowHttpCredentialSlotRequirementV1,
    WorkflowHttpDraftGrantIntentV1,
    WorkflowHttpEffectIdentityV1,
    WorkflowHttpEffectRecordV1,
    WorkflowHttpRunCredentialSnapshotV1,
    WorkflowHttpVersionGrantV1,
    derive_workflow_http_idempotency_key,
    derive_workflow_http_operation_key,
    derive_workflow_http_request_fingerprint,
    require_live_workflow_http_dispatch_grant,
    workflow_http_settled_outcome_digest,
)

RUN_ID = uuid.UUID("10000000-0000-4000-8000-000000000001")
WORKFLOW_ID = uuid.UUID("10000000-0000-4000-8000-000000000002")
VERSION_ID = uuid.UUID("10000000-0000-4000-8000-000000000003")
NODE_ID = uuid.UUID("10000000-0000-4000-8000-000000000004")
GRANT_ID = uuid.UUID("10000000-0000-4000-8000-000000000005")
CREDENTIAL_ID = uuid.UUID("10000000-0000-4000-8000-000000000006")
CREDENTIAL_VERSION_ID = uuid.UUID("10000000-0000-4000-8000-000000000007")
NOW = datetime(2026, 8, 10, tzinfo=UTC)
EFFECT_HMAC_KEY = b"workflow-http-effect-test-key!!" * 2


def _validate_json(model: type[object], payload: dict[str, object]):
    return model.model_validate_json(json.dumps(payload))


def _success_outcome() -> object:
    return WORKFLOW_HTTP_SETTLED_OUTCOME_V1_ADAPTER.validate_json(
        json.dumps(
            {
                "kind": "success",
                "response": {
                    "status_code": 200,
                    "headers": [],
                    "body": {"kind": "json", "value": {"ok": True}},
                    "duration_ms": 5,
                    "wire_byte_count": {"value": 11, "relation": "exact"},
                    "decoded_byte_count": {"value": 11, "relation": "exact"},
                    "retained_body_byte_count": 11,
                },
            }
        )
    )


def _snapshot_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": str(RUN_ID),
        "workflow_version_id": str(VERSION_ID),
        "slot_id": "partner-api",
        "injection_profile_id": "bearer-v1",
        "credential_payload_contract": "bearer_token_v1",
        "credential_grant_id": str(GRANT_ID),
        "credential_id": str(CREDENTIAL_ID),
        "credential_version_id": str(CREDENTIAL_VERSION_ID),
        "grant_version": 3,
    }


def test_http_credential_contract_freezes_intent_version_grant_and_run_snapshot_without_envelope() -> None:
    requirement = _validate_json(
        WorkflowHttpCredentialSlotRequirementV1,
        {
            "schema_version": 1,
            "slot_id": "partner-api",
            "injection_profile_id": "bearer-v1",
            "credential_payload_contract": "bearer_token_v1",
            "required": True,
        },
    )
    intent = _validate_json(
        WorkflowHttpDraftGrantIntentV1,
        {
            "schema_version": 1,
            "workflow_id": str(WORKFLOW_ID),
            "draft_revision": 7,
            "slot_id": requirement.slot_id,
            "credential_id": str(CREDENTIAL_ID),
        },
    )
    grant = _validate_json(
        WorkflowHttpVersionGrantV1,
        {
            "schema_version": 1,
            "workflow_version_id": str(VERSION_ID),
            "slot_id": requirement.slot_id,
            "injection_profile_id": requirement.injection_profile_id,
            "credential_payload_contract": requirement.credential_payload_contract,
            "credential_grant_id": str(GRANT_ID),
            "credential_id": str(intent.credential_id),
            "credential_version_id": str(CREDENTIAL_VERSION_ID),
            "grant_version": 3,
        },
    )
    snapshot = _validate_json(
        WorkflowHttpRunCredentialSnapshotV1,
        {
            **grant.model_dump(mode="json"),
            "run_id": str(RUN_ID),
        },
    )

    assert intent.slot_id == grant.slot_id == snapshot.slot_id
    serialized = snapshot.model_dump(mode="json")
    assert serialized["credential_version_id"] == str(CREDENTIAL_VERSION_ID)
    assert all("envelope" not in key for key in serialized)

    invalid_required = requirement.model_dump(mode="json")
    invalid_required["required"] = 1
    with pytest.raises(ValidationError, match="real boolean"):
        _validate_json(WorkflowHttpCredentialSlotRequirementV1, invalid_required)

    with pytest.raises(ValidationError):
        _validate_json(
            WorkflowHttpRunCredentialSnapshotV1,
            {**_snapshot_payload(), "credential_envelope_id": str(uuid.uuid4())},
        )


def test_dispatch_revalidates_exact_grant_and_active_envelope_generation() -> None:
    snapshot = _validate_json(WorkflowHttpRunCredentialSnapshotV1, _snapshot_payload())
    live = _validate_json(
        WorkflowHttpCredentialGrantLiveStateV1,
        {
            "schema_version": 1,
            "credential_grant_id": str(GRANT_ID),
            "credential_version_id": str(CREDENTIAL_VERSION_ID),
            "grant_version": 3,
            "status": "active",
            "active_envelope_generation": 9,
        },
    )

    assert require_live_workflow_http_dispatch_grant(snapshot, live) == 9
    rotated = live.model_copy(update={"active_envelope_generation": 10})
    assert require_live_workflow_http_dispatch_grant(snapshot, rotated) == 10

    for change in (
        {"status": "revoked"},
        {"grant_version": 4},
        {"credential_version_id": uuid.uuid4()},
    ):
        with pytest.raises(ValueError, match="dispatch grant"):
            require_live_workflow_http_dispatch_grant(snapshot, live.model_copy(update=change))


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_write_effect_identity_requires_stable_server_derived_idempotency(method: str) -> None:
    request_fingerprint = derive_workflow_http_request_fingerprint(
        hmac_key=EFFECT_HMAC_KEY,
        canonical_request_material=f"{method}:request".encode(),
    )
    operation_key = derive_workflow_http_operation_key(
        hmac_key=EFFECT_HMAC_KEY,
        run_id=RUN_ID,
        node_id=NODE_ID,
        activation_key="http-node-1",
        request_fingerprint=request_fingerprint,
    )
    identity = _validate_json(
        WorkflowHttpEffectIdentityV1,
        {
            "schema_version": 1,
            "effect_id": "10000000-0000-4000-8000-000000000008",
            "run_id": str(RUN_ID),
            "workflow_version_id": str(VERSION_ID),
            "node_id": str(NODE_ID),
            "activation_key": "http-node-1",
            "operation_key": operation_key,
            "method": method,
            "request_fingerprint": request_fingerprint,
            "idempotency_key": derive_workflow_http_idempotency_key(
                hmac_key=EFFECT_HMAC_KEY,
                operation_key=operation_key,
            ),
        },
    )
    assert identity.idempotency_key == derive_workflow_http_idempotency_key(
        hmac_key=EFFECT_HMAC_KEY,
        operation_key=operation_key,
    )

    invalid = identity.model_dump(mode="json")
    invalid["idempotency_key"] = None
    with pytest.raises(ValidationError, match="idempotency"):
        _validate_json(WorkflowHttpEffectIdentityV1, invalid)

    assert request_fingerprint != derive_workflow_http_request_fingerprint(
        hmac_key=b"different-workflow-http-key!!" * 2,
        canonical_request_material=f"{method}:request".encode(),
    )
    assert operation_key != derive_workflow_http_operation_key(
        hmac_key=EFFECT_HMAC_KEY,
        run_id=RUN_ID,
        node_id=NODE_ID,
        activation_key="http-node-new-iteration",
        request_fingerprint=request_fingerprint,
    )


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_read_effect_identity_rejects_write_idempotency_key(method: str) -> None:
    request_fingerprint = derive_workflow_http_request_fingerprint(
        hmac_key=EFFECT_HMAC_KEY,
        canonical_request_material=f"{method}:request".encode(),
    )
    operation_key = derive_workflow_http_operation_key(
        hmac_key=EFFECT_HMAC_KEY,
        run_id=RUN_ID,
        node_id=NODE_ID,
        activation_key="http-node-1",
        request_fingerprint=request_fingerprint,
    )
    payload = {
        "schema_version": 1,
        "effect_id": "10000000-0000-4000-8000-000000000008",
        "run_id": str(RUN_ID),
        "workflow_version_id": str(VERSION_ID),
        "node_id": str(NODE_ID),
        "activation_key": "http-node-1",
        "operation_key": operation_key,
        "method": method,
        "request_fingerprint": request_fingerprint,
        "idempotency_key": None,
    }
    assert _validate_json(WorkflowHttpEffectIdentityV1, payload).method == method
    payload["idempotency_key"] = "b" * 64
    with pytest.raises(ValidationError, match="idempotency"):
        _validate_json(WorkflowHttpEffectIdentityV1, payload)


def test_settled_effect_requires_typed_outcome_and_matching_digest() -> None:
    request_fingerprint = derive_workflow_http_request_fingerprint(
        hmac_key=EFFECT_HMAC_KEY,
        canonical_request_material=b"GET:request",
    )
    operation_key = derive_workflow_http_operation_key(
        hmac_key=EFFECT_HMAC_KEY,
        run_id=RUN_ID,
        node_id=NODE_ID,
        activation_key="http-node-1",
        request_fingerprint=request_fingerprint,
    )
    identity = _validate_json(
        WorkflowHttpEffectIdentityV1,
        {
            "schema_version": 1,
            "effect_id": "10000000-0000-4000-8000-000000000008",
            "run_id": str(RUN_ID),
            "workflow_version_id": str(VERSION_ID),
            "node_id": str(NODE_ID),
            "activation_key": "http-node-1",
            "operation_key": operation_key,
            "method": "GET",
            "request_fingerprint": request_fingerprint,
            "idempotency_key": None,
        },
    )
    outcome = _success_outcome()
    payload = {
        "schema_version": 1,
        "identity": identity.model_dump(mode="json"),
        "state": "settled",
        "revision": 3,
        "dispatch_job_id": "10000000-0000-4000-8000-000000000009",
        "dispatch_execution_epoch": 1,
        "dispatch_attempt": 1,
        "dispatch_owner": None,
        "dispatch_lease_token_hash": None,
        "dispatch_started_at": NOW.isoformat().replace("+00:00", "Z"),
        "outcome": WORKFLOW_HTTP_SETTLED_OUTCOME_V1_ADAPTER.dump_python(outcome, mode="json"),
        "outcome_digest": workflow_http_settled_outcome_digest(outcome),
        "safe_error_code": None,
        "updated_at": NOW.isoformat().replace("+00:00", "Z"),
    }
    assert _validate_json(WorkflowHttpEffectRecordV1, payload).state == "settled"

    for field, value in (
        ("outcome", None),
        ("outcome_digest", "b" * 64),
        ("dispatch_started_at", None),
    ):
        invalid = copy.deepcopy(payload)
        invalid[field] = value
        with pytest.raises(ValidationError):
            _validate_json(WorkflowHttpEffectRecordV1, invalid)

    failed_safe = copy.deepcopy(payload)
    failed_safe.update(
        {
            "state": "failed_safe",
            "outcome": None,
            "outcome_digest": None,
            "safe_error_code": "WORKFLOW_HTTP_TRANSPORT_ERROR",
        }
    )
    assert _validate_json(WorkflowHttpEffectRecordV1, failed_safe).state == "failed_safe"
    for field, value in (
        ("dispatch_started_at", None),
        ("safe_error_code", "SIDE_EFFECT_STATE_UNKNOWN"),
    ):
        invalid = copy.deepcopy(failed_safe)
        invalid[field] = value
        with pytest.raises(ValidationError):
            _validate_json(WorkflowHttpEffectRecordV1, invalid)


def test_unknown_effect_is_terminal_and_never_exposes_public_retry() -> None:
    assert SIDE_EFFECT_UNKNOWN_PUBLIC_RETRY_ALLOWED is False
