from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.workflows.compatibility import (
    WORKFLOW_COMPILER_SNAPSHOT_CONTRACT_V1_ADAPTER,
    WORKFLOW_SCHEMA_COMPATIBILITY_CASE_V1_ADAPTER,
    assess_workflow_schema_compatibility,
)
from app.workflows.run_contracts import (
    WORKFLOW_EXECUTION_REFERENCE_V1_ADAPTER,
    WORKFLOW_RUN_CONTRACT_FIXTURE_V1_ADAPTER,
    WORKFLOW_RUN_INPUT_MAX_CANONICAL_BYTES,
    WORKFLOW_RUN_INPUT_MAX_DEPTH,
    WORKFLOW_RUN_INPUT_MAX_NODES,
    WorkflowOwnerPrivateRunV1,
    WorkflowPrivateJobV1,
    WorkflowPrivateRunAuthorityV1,
    WorkflowRunAdmissionRequestV1,
    WorkflowRunAdmissionResponseV1,
    WorkflowRunExecutionReferenceV1,
    WorkflowRunJobAuthorityV1,
    WorkflowRunJobEpochMappingV1,
)
from deerflow.workflows import canonical_json_value

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "frontend/tests/fixtures/workflows/workflow-run-contracts-v1.json"
INVALID_FIXTURE_PATH = Path(__file__).resolve().parents[2] / "frontend/tests/fixtures/workflows/workflow-run-invalid-v1.json"


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _invalid_fixture() -> dict[str, object]:
    return json.loads(INVALID_FIXTURE_PATH.read_text(encoding="utf-8"))


def _validate_json(model: type, payload: object):
    return model.model_validate_json(json.dumps(payload))


def _safe_run_error() -> dict[str, object]:
    return {
        "code": "WORKFLOW_INPUT_INVALID",
        "safe_message": "工作流执行失败",
        "line": None,
        "column": None,
    }


def _owner_run_for_status(status: str) -> dict[str, object]:
    payload = deepcopy(_fixture()["owner_private_run"])
    payload.update(
        {
            "status": status,
            "started_at": None if status == "queued" else "2026-08-10T01:00:01Z",
            "completed_at": "2026-08-10T01:00:02Z" if status in {"succeeded", "failed", "cancelled", "side_effect_unknown"} else None,
            "error": _safe_run_error() if status in {"failed", "side_effect_unknown"} else None,
        }
    )
    return payload


def _private_job_for_status(status: str) -> dict[str, object]:
    payload = deepcopy(_fixture()["authority_bundles"][0]["job"])
    terminal = status in {"succeeded", "failed", "cancelled", "dead"}
    payload.update(
        {
            "status": status,
            "attempt_count": 0 if status == "queued" else 1,
            "started_at": None if status == "queued" else "2026-08-10T01:00:01Z",
            "completed_at": "2026-08-10T01:00:02Z" if terminal else None,
            "public_error_code": "WORKFLOW_TEMPORARY" if status in {"retry_wait", "failed", "dead"} else None,
        }
    )
    return payload


def _invalid_input_payload(case: dict[str, object]) -> dict[str, object]:
    kind = case["kind"]
    if kind == "input_id":
        return {str(case["value"]): 1}
    if kind == "unpaired_surrogate_value":
        return {"value": "\ud800"}
    if kind == "unpaired_surrogate_key":
        return {"value": {"bad\ud800": 1}}
    if kind == "depth":
        nested: object = "leaf"
        for _ in range(int(case["array_levels"])):
            nested = [nested]
        return {"value": nested}
    if kind == "node_count":
        return {"value": [None] * int(case["array_length"])}
    if kind == "canonical_bytes":
        return {"value": "x" * int(case["string_length"])}
    raise AssertionError(f"unknown invalid input fixture kind: {kind}")


def test_admission_inputs_accept_exact_portable_boundaries() -> None:
    exact_depth: object = "leaf"
    for _ in range(WORKFLOW_RUN_INPUT_MAX_DEPTH - 1):
        exact_depth = [exact_depth]

    exact_nodes = [None] * (WORKFLOW_RUN_INPUT_MAX_NODES - 2)
    empty_payload_bytes = len(canonical_json_value({"payload": ""}).encode("utf-8"))
    exact_bytes = "x" * (WORKFLOW_RUN_INPUT_MAX_CANONICAL_BYTES - empty_payload_bytes)

    payload = {
        "workflow_version_id": None,
        "inputs": {
            "a" + "_" * 127: "汉字😀",
            "allowed.path:-_": True,
        },
    }
    assert _validate_json(WorkflowRunAdmissionRequestV1, payload).inputs == payload["inputs"]
    assert (
        _validate_json(
            WorkflowRunAdmissionRequestV1,
            {"workflow_version_id": None, "inputs": {"value": exact_depth}},
        ).inputs["value"]
        == exact_depth
    )
    assert (
        _validate_json(
            WorkflowRunAdmissionRequestV1,
            {"workflow_version_id": None, "inputs": {"value": exact_nodes}},
        ).inputs["value"]
        == exact_nodes
    )

    exact_byte_inputs = {"payload": exact_bytes}
    assert len(canonical_json_value(exact_byte_inputs).encode("utf-8")) == WORKFLOW_RUN_INPUT_MAX_CANONICAL_BYTES
    assert (
        _validate_json(
            WorkflowRunAdmissionRequestV1,
            {"workflow_version_id": None, "inputs": exact_byte_inputs},
        ).inputs
        == exact_byte_inputs
    )


def test_shared_invalid_admission_input_corpus_is_rejected() -> None:
    for case in _invalid_fixture()["input_cases"]:
        with pytest.raises(ValidationError, match="Workflow|workflow|Input|input|JSON|string"):
            _validate_json(
                WorkflowRunAdmissionRequestV1,
                {"workflow_version_id": None, "inputs": _invalid_input_payload(case)},
            )


def test_admission_rejects_65k_subnormal_values_without_materializing_amplified_json() -> None:
    amplified = [5e-324] * (WORKFLOW_RUN_INPUT_MAX_NODES - 2)

    with pytest.raises(ValidationError, match="canonical UTF-8 byte count"):
        WorkflowRunAdmissionRequestV1.model_validate(
            {
                "workflow_version_id": None,
                "inputs": {"value": amplified},
            }
        )


def test_canonical_rfc3339_utc_timestamps_round_trip_with_z() -> None:
    run = _owner_run_for_status("running")
    run.update(
        {
            "created_at": "2026-08-10T01:00:00.1Z",
            "started_at": "2026-08-10T01:00:01.123456Z",
        }
    )
    parsed = _validate_json(WorkflowOwnerPrivateRunV1, run)
    dumped = parsed.model_dump_json()

    assert '"created_at":"2026-08-10T01:00:00.1Z"' in dumped
    assert '"started_at":"2026-08-10T01:00:01.123456Z"' in dumped
    assert "+00:00" not in dumped


def test_shared_invalid_timestamp_corpus_is_rejected_by_run_job_and_mapping() -> None:
    for timestamp in _invalid_fixture()["time_values"]:
        run = _owner_run_for_status("running")
        run["created_at"] = timestamp
        with pytest.raises(ValidationError):
            _validate_json(WorkflowOwnerPrivateRunV1, run)

        job = _private_job_for_status("running")
        job["created_at"] = timestamp
        with pytest.raises(ValidationError):
            _validate_json(WorkflowPrivateJobV1, job)

        authority = deepcopy(_fixture()["authority_bundles"][0])
        authority["job"]["created_at"] = timestamp
        authority["mapping"]["created_at"] = timestamp
        with pytest.raises(ValidationError):
            _validate_json(WorkflowRunJobAuthorityV1, authority)


@pytest.mark.parametrize("status", ["queued", "running", "succeeded", "failed", "cancelled", "side_effect_unknown"])
def test_owner_run_accepts_each_exact_status_shape(status: str) -> None:
    assert _validate_json(WorkflowOwnerPrivateRunV1, _owner_run_for_status(status)).status == status


def test_shared_invalid_owner_run_status_corpus_is_rejected() -> None:
    for case in _invalid_fixture()["run_cases"]:
        run = _owner_run_for_status(str(case["status"]))
        if case.get("error_mode") == "safe":
            run["error"] = _safe_run_error()
        elif case.get("error_mode") == "none":
            run["error"] = None
        run.update({key: value for key, value in case.items() if key not in {"id", "status", "error_mode"}})
        with pytest.raises(ValidationError):
            _validate_json(WorkflowOwnerPrivateRunV1, run)


@pytest.mark.parametrize(
    "status",
    ["queued", "leased", "running", "retry_wait", "succeeded", "failed", "cancelled", "dead"],
)
def test_private_job_accepts_each_exact_status_shape(status: str) -> None:
    assert _validate_json(WorkflowPrivateJobV1, _private_job_for_status(status)).status == status


def test_shared_invalid_private_job_status_corpus_is_rejected() -> None:
    for case in _invalid_fixture()["job_cases"]:
        job = _private_job_for_status(str(case["status"]))
        job.update({key: value for key, value in case.items() if key not in {"id", "status"}})
        with pytest.raises(ValidationError):
            _validate_json(WorkflowPrivateJobV1, job)


def test_shared_fixture_round_trips_all_public_and_private_contracts() -> None:
    fixture = WORKFLOW_RUN_CONTRACT_FIXTURE_V1_ADAPTER.validate_json(FIXTURE_PATH.read_text(encoding="utf-8"))

    assert fixture.schema_version == 1
    assert fixture.admission_response.status == "queued"
    assert fixture.owner_private_run.execution_epoch == 1
    assert [bundle.mapping.cause for bundle in fixture.authority_bundles] == ["initial", "resume"]
    assert [bundle.mapping.execution_epoch for bundle in fixture.authority_bundles] == [1, 2]


def test_cancel_before_start_is_a_valid_terminal_run_and_zero_attempt_job() -> None:
    fixture = _fixture()

    run = _validate_json(WorkflowOwnerPrivateRunV1, fixture["cancelled_before_start_run"])
    job = _validate_json(WorkflowPrivateJobV1, fixture["cancelled_before_start_job"])

    assert run.status == "cancelled"
    assert run.started_at is None
    assert run.completed_at is not None
    assert job.status == "cancelled"
    assert job.attempt_count == 0
    assert job.started_at is None
    assert job.completed_at is not None


@pytest.mark.parametrize(
    "server_owned_field",
    [
        "project_id",
        "owner_user_id",
        "origin_trace_id",
        "execution_epoch",
        "current_job_id",
        "checkpoint_id",
        "credential_version_id",
        "required_worker_profile_digest",
        "idempotency_key",
    ],
)
def test_run_admission_request_rejects_every_server_owned_field(server_owned_field: str) -> None:
    payload = deepcopy(_fixture()["admission_request"])
    assert isinstance(payload, dict)
    payload[server_owned_field] = "forbidden"

    with pytest.raises(ValidationError):
        _validate_json(WorkflowRunAdmissionRequestV1, payload)


@pytest.mark.parametrize(
    "private_field",
    ["project_id", "owner_user_id", "origin_trace_id", "current_job_id", "job_id", "attempt_count", "lease_token"],
)
def test_public_run_dtos_reject_server_private_authority(private_field: str) -> None:
    fixture = _fixture()
    for model, key in (
        (WorkflowRunAdmissionResponseV1, "admission_response"),
        (WorkflowOwnerPrivateRunV1, "owner_private_run"),
    ):
        payload = deepcopy(fixture[key])
        assert isinstance(payload, dict)
        payload[private_field] = "forbidden"
        with pytest.raises(ValidationError):
            _validate_json(model, payload)


def test_owner_private_run_status_timestamps_and_retry_identity_are_closed() -> None:
    payload = deepcopy(_fixture()["owner_private_run"])
    assert isinstance(payload, dict)

    payload["status"] = "succeeded"
    payload["completed_at"] = None
    with pytest.raises(ValidationError):
        _validate_json(WorkflowOwnerPrivateRunV1, payload)

    payload = deepcopy(_fixture()["owner_private_run"])
    assert isinstance(payload, dict)
    payload["retry_of_run_id"] = payload["run_id"]
    with pytest.raises(ValidationError):
        _validate_json(WorkflowOwnerPrivateRunV1, payload)

    payload = deepcopy(_fixture()["owner_private_run"])
    assert isinstance(payload, dict)
    payload.update(
        {
            "status": "side_effect_unknown",
            "completed_at": "2026-08-10T01:00:02Z",
            "error": {
                "code": "SIDE_EFFECT_STATE_UNKNOWN",
                "safe_message": "无法确认远端写请求是否已生效",
                "line": None,
                "column": None,
            },
        }
    )
    assert _validate_json(WorkflowOwnerPrivateRunV1, payload).status == "side_effect_unknown"


def test_agent_and_workflow_execution_references_are_mutually_exclusive() -> None:
    references = _fixture()["execution_references"]
    assert isinstance(references, list)
    assert [WORKFLOW_EXECUTION_REFERENCE_V1_ADAPTER.validate_json(json.dumps(value)).kind for value in references] == ["agent_run", "workflow_run"]

    mixed = {**references[1], "run_id": references[0]["run_id"]}
    with pytest.raises(ValidationError):
        WORKFLOW_EXECUTION_REFERENCE_V1_ADAPTER.validate_json(json.dumps(mixed))


@pytest.mark.parametrize("uuid_case", _invalid_fixture()["uuid_values"], ids=lambda case: case["id"])
def test_every_run_dto_rejects_noncanonical_uuid_text(uuid_case: dict[str, str]) -> None:
    invalid_uuid = uuid_case["value"]
    fixture = _fixture()

    request = deepcopy(fixture["admission_request"])
    request["workflow_version_id"] = invalid_uuid
    response = deepcopy(fixture["admission_response"])
    response["run_id"] = invalid_uuid
    owner_run = deepcopy(fixture["owner_private_run"])
    owner_run["workflow_id"] = invalid_uuid
    private_run = deepcopy(fixture["authority_bundles"][0]["run"])
    private_run["owner_user_id"] = invalid_uuid
    private_job = deepcopy(fixture["authority_bundles"][0]["job"])
    private_job["project_id"] = invalid_uuid
    mapping = deepcopy(fixture["authority_bundles"][0]["mapping"])
    mapping["job_id"] = invalid_uuid
    workflow_reference = deepcopy(fixture["execution_references"][1])
    workflow_reference["workflow_run_id"] = invalid_uuid

    for model, payload in (
        (WorkflowRunAdmissionRequestV1, request),
        (WorkflowRunAdmissionResponseV1, response),
        (WorkflowOwnerPrivateRunV1, owner_run),
        (WorkflowPrivateRunAuthorityV1, private_run),
        (WorkflowPrivateJobV1, private_job),
        (WorkflowRunJobEpochMappingV1, mapping),
        (WorkflowRunExecutionReferenceV1, workflow_reference),
    ):
        with pytest.raises(ValidationError):
            _validate_json(model, payload)

    agent_reference = deepcopy(fixture["execution_references"][0])
    agent_reference["run_id"] = invalid_uuid
    with pytest.raises(ValidationError):
        WORKFLOW_EXECUTION_REFERENCE_V1_ADAPTER.validate_json(json.dumps(agent_reference))

    for snapshot_field in ("workflow_run_id", "workflow_version_id"):
        compiler = deepcopy(fixture["compiler_snapshot_contract"])
        compiler["snapshot_identity"][snapshot_field] = invalid_uuid
        with pytest.raises(ValidationError):
            WORKFLOW_COMPILER_SNAPSHOT_CONTRACT_V1_ADAPTER.validate_json(json.dumps(compiler))


@pytest.mark.parametrize("invalid_code", _invalid_fixture()["public_error_codes"])
def test_private_job_public_error_code_matches_the_cross_runtime_contract(invalid_code: str) -> None:
    job = deepcopy(_fixture()["authority_bundles"][0]["job"])
    job["public_error_code"] = invalid_code

    with pytest.raises(ValidationError):
        _validate_json(WorkflowPrivateJobV1, job)


def test_epoch_mapping_is_initial_one_then_resume_and_job_attempt_does_not_change_epoch() -> None:
    bundles = _fixture()["authority_bundles"]
    assert isinstance(bundles, list)

    initial = deepcopy(bundles[0])
    initial["mapping"]["execution_epoch"] = 2
    with pytest.raises(ValidationError):
        _validate_json(WorkflowRunJobAuthorityV1, initial)

    resumed = deepcopy(bundles[1])
    resumed["mapping"]["execution_epoch"] = 1
    with pytest.raises(ValidationError):
        _validate_json(WorkflowRunJobAuthorityV1, resumed)

    retry_attempt = deepcopy(bundles[0])
    retry_attempt["job"]["attempt_count"] = 2
    assert _validate_json(WorkflowRunJobAuthorityV1, retry_attempt).mapping.execution_epoch == 1


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("run", "current_job_id"), "99999999-9999-4999-8999-999999999999"),
        (("job", "origin_trace_id"), "different-trace"),
        (("mapping", "job_id"), "99999999-9999-4999-8999-999999999999"),
        (("job", "execution_reference", "workflow_epoch"), 2),
        (("job", "execution_reference", "required_worker_profile_digest"), "d" * 64),
    ],
)
def test_private_run_job_authority_requires_one_exact_current_epoch_mapping(path: tuple[str, ...], replacement: object) -> None:
    payload = deepcopy(_fixture()["authority_bundles"][0])
    target = payload
    for segment in path[:-1]:
        target = target[segment]
    target[path[-1]] = replacement

    with pytest.raises(ValidationError):
        _validate_json(WorkflowRunJobAuthorityV1, payload)


def test_shared_compatibility_cases_forbid_silent_or_published_in_place_migration() -> None:
    for raw_case in _fixture()["compatibility_cases"]:
        case = WORKFLOW_SCHEMA_COMPATIBILITY_CASE_V1_ADAPTER.validate_json(json.dumps(raw_case))
        assessed = assess_workflow_schema_compatibility(
            artifact_kind=case.artifact_kind,
            source=case.source,
            supported=case.supported,
            migration_paths=case.migration_paths,
        )
        assert assessed == case.expected
        assert assessed.silent_upgrade_allowed is False

    published = WORKFLOW_SCHEMA_COMPATIBILITY_CASE_V1_ADAPTER.validate_json(json.dumps(_fixture()["compatibility_cases"][2]))
    assert published.expected.status == "read_only_unsupported"
    assert published.expected.reason == "PUBLISHED_VERSION_MIGRATION_FORBIDDEN"

    unknown = deepcopy(_fixture()["compatibility_cases"][0])
    unknown["expected"]["auto_upgrade"] = True
    with pytest.raises(ValidationError):
        WORKFLOW_SCHEMA_COMPATIBILITY_CASE_V1_ADAPTER.validate_json(json.dumps(unknown))


def test_unknown_future_run_snapshot_is_read_only_without_silent_upgrade() -> None:
    case = WORKFLOW_SCHEMA_COMPATIBILITY_CASE_V1_ADAPTER.validate_json(json.dumps(_fixture()["compatibility_cases"][0]))
    future = case.source.model_copy(update={"compiler_contract_version": 99})
    assessed = assess_workflow_schema_compatibility(
        artifact_kind="run_snapshot",
        source=future,
        supported=case.supported,
        migration_paths=(),
    )

    assert assessed.status == "read_only_unsupported"
    assert assessed.reason == "RUN_SNAPSHOT_MIGRATION_FORBIDDEN"
    assert assessed.read_only is True
    assert assessed.silent_upgrade_allowed is False


def test_compiler_and_snapshot_identity_must_match_exactly() -> None:
    payload = _fixture()["compiler_snapshot_contract"]
    contract = WORKFLOW_COMPILER_SNAPSHOT_CONTRACT_V1_ADAPTER.validate_json(json.dumps(payload))
    assert contract.snapshot_identity.compiler_contract_version == contract.compiler_identity.compiler_contract_version
    assert contract.compiler_identity.cache_key == (1, 1, "a" * 64)

    drifted = deepcopy(payload)
    drifted["snapshot_identity"]["compiler_contract_version"] = 2
    with pytest.raises(ValidationError):
        WORKFLOW_COMPILER_SNAPSHOT_CONTRACT_V1_ADAPTER.validate_json(json.dumps(drifted))
