from __future__ import annotations

import json
import uuid
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.workflows.run_contracts import WORKFLOW_RUN_INPUT_MAX_CANONICAL_BYTES
from app.workflows.runtime_policy import (
    FIRST_BATCH_WORKFLOW_NODE_KINDS,
    WORKFLOW_RUNTIME_MAX_AGGREGATE_GROUPS,
    WORKFLOW_RUNTIME_MAX_EVENT_PREVIEW_BYTES,
    WORKFLOW_RUNTIME_MAX_INPUT_BYTES,
    WORKFLOW_RUNTIME_POLICY_EFFECT_SCOPE,
    WORKFLOW_RUNTIME_READINESS_V1_ADAPTER,
    WorkflowRuntimeAdminPolicyV1,
    WorkflowRuntimeEffectivePolicyV1,
    WorkflowRuntimePolicyUpdateRequestV1,
    WorkflowRuntimePolicyUpdateResponseV1,
    WorkflowRuntimePolicyV1,
    WorkflowRuntimeStoredPolicyV1,
    create_workflow_runtime_admin_policy,
    create_workflow_runtime_stored_policy,
    create_workflow_runtime_update_response,
    revalidate_trusted_workflow_runtime_policy,
    workflow_runtime_policy_checksum,
)

_SHARED_RUNTIME_POLICY_FIXTURE = Path(__file__).resolve().parents[2] / "frontend/tests/fixtures/workflows/workflow-runtime-policy-v1.json"
_SHARED_RUN_INVALID_FIXTURE = Path(__file__).resolve().parents[2] / "frontend/tests/fixtures/workflows/workflow-run-invalid-v1.json"


def _shared_runtime_policy_fixture() -> dict[str, object]:
    return json.loads(_SHARED_RUNTIME_POLICY_FIXTURE.read_text(encoding="utf-8"))


def _runtime_policy_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "enabled": False,
        "admission_enabled": False,
        "catalog": {
            "allowed_type_versions": [{"type": node_type, "versions": [1]} for node_type in FIRST_BATCH_WORKFLOW_NODE_KINDS],
        },
        "graph_limits": {
            "max_nodes": 100,
            "max_edges": 200,
            "max_depth": 20,
            "max_total_steps": 1_000,
            "max_recursion_depth": 2_000,
            "max_parallelism": 8,
            "max_fan_out": 16,
            "max_loops": 8,
            "max_loop_body_nodes": 32,
            "max_loop_body_edges": 64,
            "max_loop_iterations": 100,
            "max_total_iterations": 500,
            "max_total_activations": 2_000,
            "max_aggregate_groups": 32,
            "max_aggregate_candidates": 64,
        },
        "execution_limits": {
            "max_node_timeout_ms": 30_000,
            "max_run_timeout_ms": 300_000,
            "max_human_wait_timeout_ms": 86_400_000,
            "max_input_bytes": 1_048_576,
            "max_state_bytes": 4_194_304,
            "max_output_bytes": 524_288,
            "max_event_preview_bytes": 65_536,
            "max_retry_attempts": 3,
            "retry_backoff_initial_ms": 100,
            "retry_backoff_max_ms": 5_000,
            "max_llm_tokens_per_call": 32_768,
            "max_llm_calls": 100,
            "max_code_activations": 0,
            "max_code_duration_ms": 300_000,
            "max_http_calls": 0,
            "max_http_request_bytes": 1_048_576,
            "max_http_response_bytes": 1_048_576,
            "max_http_total_bytes": 8_388_608,
            "max_mcp_calls": 0,
            "max_files": 0,
            "max_file_bytes": 0,
        },
        "code": {
            "enabled": False,
            "provider_adapter_key": None,
            "execution_profile_id": None,
            "runtime_contract": "python3.12-v1",
            "image_digest": None,
            "isolation_profile": None,
            "network_policy": "deny_all",
            "dns_policy": "deny_all",
            "hard_limits": {
                "cpu_millicores": 1_000,
                "memory_bytes": 268_435_456,
                "max_pids": 32,
                "tmpfs_bytes": 67_108_864,
                "wall_timeout_ms": 30_000,
                "max_source_bytes": 65_536,
                "max_stdout_bytes": 65_536,
                "max_stderr_bytes": 65_536,
                "max_result_bytes": 524_288,
                "max_total_log_bytes": 262_144,
                "read_only_root_filesystem": True,
                "allow_mounts": False,
                "allow_host_environment": False,
                "allow_credentials": False,
                "allow_runtime_sockets": False,
            },
        },
        "http": {
            "enabled": False,
            "write_enabled": False,
            "egress_profile_id": None,
            "egress_profile_digest": None,
            "endpoint_policies": [],
            "injection_profiles": [],
            "transport": {
                "connect_timeout_ms": 5_000,
                "read_timeout_ms": 30_000,
                "write_timeout_ms": 30_000,
                "total_timeout_ms": 60_000,
                "max_headers": 64,
                "max_header_name_bytes": 128,
                "max_header_value_bytes": 4_096,
                "max_request_bytes": 1_048_576,
                "max_wire_response_bytes": 1_048_576,
                "max_decompressed_response_bytes": 2_097_152,
                "max_json_depth": 32,
                "max_retries": 3,
                "retry_backoff_initial_ms": 100,
                "retry_backoff_max_ms": 5_000,
                "max_retry_after_ms": 30_000,
                "tls_verify": True,
                "follow_redirects": False,
                "cookie_jar": False,
                "trust_env": False,
            },
        },
        "retention": {
            "terminal_run_days": 30,
            "event_days": 30,
            "http_effect_days": 30,
            "destroyed_code_lease_days": 7,
        },
        "future": {
            "human_input_enabled": False,
            "agent_enabled": False,
            "tool_enabled": False,
            "mcp_enabled": False,
            "iteration_enabled": False,
            "subworkflow_enabled": False,
            "automation_enabled": False,
            "chatflow_enabled": False,
        },
    }


def test_python_and_typescript_share_one_runtime_policy_golden_fixture() -> None:
    fixture = _shared_runtime_policy_fixture()
    policy = WorkflowRuntimePolicyV1.model_validate(fixture["policy"])

    assert workflow_runtime_policy_checksum(policy) == fixture["payload_checksum"]

    stored_payload = {
        **fixture["stored_identity"],
        "payload_checksum": fixture["payload_checksum"],
        "value": fixture["policy"],
    }
    stored = WorkflowRuntimeStoredPolicyV1.model_validate_json(json.dumps(stored_payload))
    effective_payload = {
        **fixture["effective_identity"],
        "payload_checksum": fixture["payload_checksum"],
    }
    projection_payload = {
        **fixture["admin_projection"],
        "stored": stored.model_dump(mode="json"),
        "effective": effective_payload,
    }
    projection = WorkflowRuntimeAdminPolicyV1.model_validate_json(json.dumps(projection_payload))

    assert projection.effect_scope == WORKFLOW_RUNTIME_POLICY_EFFECT_SCOPE
    assert projection.stored.policy_version_id == projection.effective.policy_version_id

    update_request = WorkflowRuntimePolicyUpdateRequestV1.model_validate(
        {
            **fixture["update_request"],
            "value": fixture["policy"],
        }
    )
    update_response = WorkflowRuntimePolicyUpdateResponseV1.model_validate_json(
        json.dumps(
            {
                **projection_payload,
                "catalog_revision": fixture["catalog_revision"],
            }
        )
    )

    assert update_request.expected_revision == 6
    assert update_response.catalog_revision == 12


def test_workflow_runtime_policy_accepts_the_complete_secret_free_disabled_contract() -> None:
    policy = WorkflowRuntimePolicyV1.model_validate(_runtime_policy_payload())

    assert policy.schema_version == 1
    assert tuple(item.type for item in policy.catalog.allowed_type_versions) == FIRST_BATCH_WORKFLOW_NODE_KINDS
    assert policy.code.network_policy == "deny_all"
    assert policy.code.dns_policy == "deny_all"
    assert WORKFLOW_RUNTIME_POLICY_EFFECT_SCOPE == "new_workflow_runs"


def test_runtime_policy_hard_caps_match_run_and_port_contracts() -> None:
    fixture_cases = _shared_runtime_policy_fixture()["audit_negative_cases"]
    assert WORKFLOW_RUNTIME_MAX_INPUT_BYTES == WORKFLOW_RUN_INPUT_MAX_CANONICAL_BYTES
    assert WORKFLOW_RUNTIME_MAX_EVENT_PREVIEW_BYTES == 65_536
    assert WORKFLOW_RUNTIME_MAX_AGGREGATE_GROUPS == 254

    for section, hard_maxima in (
        (
            "graph_limits",
            {
                "max_aggregate_groups": WORKFLOW_RUNTIME_MAX_AGGREGATE_GROUPS,
            },
        ),
        (
            "execution_limits",
            {
                "max_input_bytes": WORKFLOW_RUNTIME_MAX_INPUT_BYTES,
                "max_event_preview_bytes": WORKFLOW_RUNTIME_MAX_EVENT_PREVIEW_BYTES,
            },
        ),
    ):
        at_hard_cap = _runtime_policy_payload()
        at_hard_cap[section].update(hard_maxima)  # type: ignore[union-attr]
        WorkflowRuntimePolicyV1.model_validate(at_hard_cap)

        for field, value in fixture_cases[f"{section.removesuffix('_limits')}_limit_overrides"].items():  # type: ignore[union-attr]
            above_hard_cap = _runtime_policy_payload()
            above_hard_cap[section][field] = value  # type: ignore[index]
            with pytest.raises(ValidationError):
                WorkflowRuntimePolicyV1.model_validate(above_hard_cap)

    # The System Admin may narrow each limit below the immutable contract cap.
    WorkflowRuntimePolicyV1.model_validate(_runtime_policy_payload())


@pytest.mark.parametrize(
    ("path", "field", "value"),
    [
        ((), "config_file", "config.yaml"),
        ((), "environment", {"WORKFLOW_ENABLED": "true"}),
        (("code",), "import_path", "package.module:Executor"),
        (("code",), "provider_locator", "https://provisioner.internal"),
        (("code",), "secret", "plaintext"),
        (("http",), "proxy_url", "https://proxy.internal"),
        (("http",), "credential_id", "system-credential"),
        (("http",), "api_token", "plaintext"),
    ],
)
def test_runtime_policy_rejects_config_env_locator_and_secret_fields(path: tuple[str, ...], field: str, value: object) -> None:
    payload = _runtime_policy_payload()
    target = payload
    for item in path:
        target = target[item]  # type: ignore[assignment,index]
    target[field] = value

    with pytest.raises(ValidationError):
        WorkflowRuntimePolicyV1.model_validate(payload)


@pytest.mark.parametrize("future_field", ["human_input_enabled", "agent_enabled", "tool_enabled", "mcp_enabled"])
def test_future_and_second_batch_capabilities_are_explicitly_false(future_field: str) -> None:
    payload = _runtime_policy_payload()
    payload["future"][future_field] = True  # type: ignore[index]

    with pytest.raises(ValidationError):
        WorkflowRuntimePolicyV1.model_validate(payload)


def test_admission_and_http_write_cannot_widen_disabled_parent_authority() -> None:
    payload = _runtime_policy_payload()
    payload["admission_enabled"] = True

    with pytest.raises(ValidationError):
        WorkflowRuntimePolicyV1.model_validate(payload)

    payload = _runtime_policy_payload()
    payload["http"]["write_enabled"] = True  # type: ignore[index]

    with pytest.raises(ValidationError):
        WorkflowRuntimePolicyV1.model_validate(payload)


def test_enabled_code_requires_only_static_profile_identifiers_and_digest() -> None:
    payload = _runtime_policy_payload()
    payload["code"].update(  # type: ignore[union-attr]
        {
            "enabled": True,
            "provider_adapter_key": "aio_isolated_code_v1",
            "execution_profile_id": "python312-isolated-v1",
            "image_digest": "sha256:" + "a" * 64,
            "isolation_profile": "workflow-python-code-v1",
        }
    )
    payload["execution_limits"]["max_code_activations"] = 10  # type: ignore[index]

    policy = WorkflowRuntimePolicyV1.model_validate(payload)

    assert policy.code.provider_adapter_key == "aio_isolated_code_v1"

    for invalid_adapter in (
        "deerflow.community.aio_sandbox:AioSandboxProvider",
        "https://provisioner.internal",
        "local",
        "custom_adapter",
    ):
        invalid = deepcopy(payload)
        invalid["code"]["provider_adapter_key"] = invalid_adapter  # type: ignore[index]
        with pytest.raises(ValidationError):
            WorkflowRuntimePolicyV1.model_validate(invalid)

    partial = _runtime_policy_payload()
    partial["code"]["provider_adapter_key"] = "aio_isolated_code_v1"  # type: ignore[index]
    with pytest.raises(ValidationError):
        WorkflowRuntimePolicyV1.model_validate(partial)


def test_code_hardening_constants_cannot_be_relaxed_or_coerced() -> None:
    for field, value in (
        ("network_policy", "controlled_egress"),
        ("dns_policy", "allow"),
    ):
        payload = _runtime_policy_payload()
        payload["code"][field] = value  # type: ignore[index]
        with pytest.raises(ValidationError):
            WorkflowRuntimePolicyV1.model_validate(payload)

    for field, value in (
        ("read_only_root_filesystem", False),
        ("allow_mounts", True),
        ("allow_host_environment", True),
        ("allow_credentials", True),
        ("allow_runtime_sockets", True),
        ("allow_mounts", 0),
    ):
        payload = _runtime_policy_payload()
        payload["code"]["hard_limits"][field] = value  # type: ignore[index]
        with pytest.raises(ValidationError):
            WorkflowRuntimePolicyV1.model_validate(payload)


def test_enabled_http_requires_fixed_safe_profiles_and_https_origin() -> None:
    payload = _runtime_policy_payload()
    payload["http"].update(  # type: ignore[union-attr]
        {
            "enabled": True,
            "egress_profile_id": "controlled-egress-v1",
            "egress_profile_digest": "b" * 64,
            "injection_profiles": [
                {
                    "id": "api-key-v1",
                    "location": "header",
                    "scheme": "api_key",
                    "target_header": "x-api-key",
                    "credential_payload_contract": "api_key_v1",
                }
            ],
            "endpoint_policies": [
                {
                    "id": "example-api",
                    "origin": "https://api.example.com:443",
                    "allowed_methods": ["GET", "HEAD"],
                    "injection_profile_ids": ["api-key-v1"],
                    "write_idempotency": "none",
                    "idempotency_header": None,
                }
            ],
        }
    )
    payload["execution_limits"]["max_http_calls"] = 10  # type: ignore[index]

    policy = WorkflowRuntimePolicyV1.model_validate(payload)

    assert policy.http.endpoint_policies[0].origin == "https://api.example.com:443"

    fixture_cases = _shared_runtime_policy_fixture()["audit_negative_cases"]
    for origin in (
        "http://api.example.com",
        "https://api.example.com/v1",
        "https://user:password@api.example.com",
        *fixture_cases["rejected_origins"],  # type: ignore[index]
    ):
        invalid = deepcopy(payload)
        invalid["http"]["endpoint_policies"][0]["origin"] = origin  # type: ignore[index]
        with pytest.raises(ValidationError):
            WorkflowRuntimePolicyV1.model_validate(invalid)

    for origin in fixture_cases["allowed_origins"]:  # type: ignore[union-attr]
        valid = deepcopy(payload)
        valid["http"]["endpoint_policies"][0]["origin"] = origin  # type: ignore[index]
        WorkflowRuntimePolicyV1.model_validate(valid)


def test_http_transport_security_constants_and_references_fail_closed() -> None:
    payload = _runtime_policy_payload()
    for field, value in (
        ("tls_verify", False),
        ("follow_redirects", True),
        ("cookie_jar", True),
        ("trust_env", True),
        ("trust_env", 0),
    ):
        invalid = deepcopy(payload)
        invalid["http"]["transport"][field] = value  # type: ignore[index]
        with pytest.raises(ValidationError):
            WorkflowRuntimePolicyV1.model_validate(invalid)

    enabled = _runtime_policy_payload()
    enabled["http"]["enabled"] = True  # type: ignore[index]
    enabled["execution_limits"]["max_http_calls"] = 1  # type: ignore[index]
    with pytest.raises(ValidationError):
        WorkflowRuntimePolicyV1.model_validate(enabled)

    fixture_cases = _shared_runtime_policy_fixture()["audit_negative_cases"]
    for field, value in fixture_cases["transport_limit_overrides"].items():  # type: ignore[union-attr]
        invalid = _runtime_policy_payload()
        invalid["http"]["transport"][field] = value  # type: ignore[index]
        with pytest.raises(ValidationError):
            WorkflowRuntimePolicyV1.model_validate(invalid)


def test_transport_controlled_headers_are_rejected_for_injection_and_idempotency() -> None:
    fixture_cases = _shared_runtime_policy_fixture()["audit_negative_cases"]
    headers = fixture_cases["transport_controlled_headers"]  # type: ignore[index]

    for header in headers:
        injection = _runtime_policy_payload()
        injection["http"]["injection_profiles"] = [  # type: ignore[index]
            {
                "id": "injection-v1",
                "location": "header",
                "scheme": "api_key",
                "target_header": header,
                "credential_payload_contract": "api_key_v1",
            }
        ]
        with pytest.raises(ValidationError):
            WorkflowRuntimePolicyV1.model_validate(injection)

    for scheme, contract in (("bearer", "bearer_token_v1"), ("basic", "basic_auth_v1")):
        credential_injection = _runtime_policy_payload()
        credential_injection["http"]["injection_profiles"] = [  # type: ignore[index]
            {
                "id": f"{scheme}-v1",
                "location": "header",
                "scheme": scheme,
                "target_header": "authorization",
                "credential_payload_contract": contract,
            }
        ]
        WorkflowRuntimePolicyV1.model_validate(credential_injection)

    api_key_authorization = _runtime_policy_payload()
    api_key_authorization["http"]["injection_profiles"] = [  # type: ignore[index]
        {
            "id": "api-key-v1",
            "location": "header",
            "scheme": "api_key",
            "target_header": "authorization",
            "credential_payload_contract": "api_key_v1",
        }
    ]
    with pytest.raises(ValidationError):
        WorkflowRuntimePolicyV1.model_validate(api_key_authorization)

    idempotency_headers = [
        *headers,
        *fixture_cases["credential_controlled_headers"],  # type: ignore[index]
    ]
    for header in idempotency_headers:
        idempotency = _runtime_policy_payload()
        idempotency["http"]["endpoint_policies"] = [  # type: ignore[index]
            {
                "id": "example-write-api",
                "origin": "https://api.example.com:443",
                "allowed_methods": ["POST"],
                "injection_profile_ids": [],
                "write_idempotency": "server_derived_key",
                "idempotency_header": header,
            }
        ]
        with pytest.raises(ValidationError):
            WorkflowRuntimePolicyV1.model_validate(idempotency)


def test_http_write_idempotency_header_is_explicit_and_cannot_collide_with_credentials() -> None:
    payload = _runtime_policy_payload()
    payload["http"].update(  # type: ignore[union-attr]
        {
            "enabled": True,
            "write_enabled": True,
            "egress_profile_id": "controlled-egress-v1",
            "egress_profile_digest": "b" * 64,
            "injection_profiles": [
                {
                    "id": "api-key-v1",
                    "location": "header",
                    "scheme": "api_key",
                    "target_header": "x-api-key",
                    "credential_payload_contract": "api_key_v1",
                }
            ],
            "endpoint_policies": [
                {
                    "id": "example-write-api",
                    "origin": "https://api.example.com:443",
                    "allowed_methods": ["POST"],
                    "injection_profile_ids": ["api-key-v1"],
                    "write_idempotency": "server_derived_key",
                    "idempotency_header": "idempotency-key",
                }
            ],
        }
    )
    payload["execution_limits"]["max_http_calls"] = 10  # type: ignore[index]

    policy = WorkflowRuntimePolicyV1.model_validate(payload)
    assert policy.http.endpoint_policies[0].idempotency_header == "idempotency-key"

    for invalid_header in (None, "authorization", "x-api-key"):
        invalid = deepcopy(payload)
        invalid["http"]["endpoint_policies"][0]["idempotency_header"] = invalid_header  # type: ignore[index]
        with pytest.raises(ValidationError):
            WorkflowRuntimePolicyV1.model_validate(invalid)

    invalid = deepcopy(payload)
    invalid["http"]["endpoint_policies"][0].update(  # type: ignore[index,union-attr]
        {
            "write_idempotency": "none",
            "idempotency_header": "idempotency-key",
        }
    )
    with pytest.raises(ValidationError):
        WorkflowRuntimePolicyV1.model_validate(invalid)


def test_runtime_policy_rejects_duplicate_or_future_node_type_versions() -> None:
    payload = _runtime_policy_payload()
    payload["catalog"]["allowed_type_versions"].append(  # type: ignore[index,union-attr]
        {"type": "start", "versions": [1]}
    )
    with pytest.raises(ValidationError):
        WorkflowRuntimePolicyV1.model_validate(payload)


def _runtime_policy_with_every_collection() -> WorkflowRuntimePolicyV1:
    payload = _runtime_policy_payload()
    payload["http"].update(  # type: ignore[union-attr]
        {
            "enabled": True,
            "egress_profile_id": "controlled-egress-v1",
            "egress_profile_digest": "b" * 64,
            "injection_profiles": [
                {
                    "id": "api-key-v1",
                    "location": "header",
                    "scheme": "api_key",
                    "target_header": "x-api-key",
                    "credential_payload_contract": "api_key_v1",
                }
            ],
            "endpoint_policies": [
                {
                    "id": "example-api",
                    "origin": "https://api.example.com",
                    "allowed_methods": ["GET"],
                    "injection_profile_ids": ["api-key-v1"],
                    "write_idempotency": "none",
                    "idempotency_header": None,
                }
            ],
        }
    )
    payload["execution_limits"]["max_http_calls"] = 1  # type: ignore[index]
    return WorkflowRuntimePolicyV1.model_validate_json(json.dumps(payload))


def test_runtime_policy_json_arrays_materialize_as_immutable_tuples_and_serialize_as_arrays() -> None:
    policy = _runtime_policy_with_every_collection()
    stored = create_workflow_runtime_stored_policy(
        policy_version_id=uuid.UUID("53f5a2b9-1c63-43ec-92d4-2aa799f18857"),
        revision=7,
        schema_version=1,
        payload_checksum=workflow_runtime_policy_checksum(policy),
        value=policy,
    )
    before_checksum = workflow_runtime_policy_checksum(policy)
    before_snapshot = stored.model_dump_json()
    collections = {
        "versions": policy.catalog.allowed_type_versions[0].versions,
        "allowed_type_versions": policy.catalog.allowed_type_versions,
        "allowed_methods": policy.http.endpoint_policies[0].allowed_methods,
        "injection_profile_ids": policy.http.endpoint_policies[0].injection_profile_ids,
        "endpoint_policies": policy.http.endpoint_policies,
        "injection_profiles": policy.http.injection_profiles,
    }

    for name, collection in collections.items():
        assert isinstance(collection, tuple), name
        with pytest.raises(AttributeError):
            collection.append(object())  # type: ignore[attr-defined]

    serialized = policy.model_dump(mode="json")
    assert isinstance(serialized["catalog"]["allowed_type_versions"], list)
    assert isinstance(serialized["catalog"]["allowed_type_versions"][0]["versions"], list)
    assert isinstance(serialized["http"]["endpoint_policies"], list)
    assert isinstance(serialized["http"]["endpoint_policies"][0]["allowed_methods"], list)
    assert isinstance(serialized["http"]["endpoint_policies"][0]["injection_profile_ids"], list)
    assert isinstance(serialized["http"]["injection_profiles"], list)
    assert workflow_runtime_policy_checksum(policy) == before_checksum
    assert stored.model_dump_json() == before_snapshot

    invalid = _runtime_policy_payload()
    invalid["catalog"]["allowed_type_versions"][0]["versions"] = {1}  # type: ignore[index]
    with pytest.raises(ValidationError):
        WorkflowRuntimePolicyV1.model_validate(invalid)


_IMMUTABLE_COLLECTION_PATHS = (
    ("catalog", "allowed_type_versions"),
    ("catalog", "allowed_type_versions", 0, "versions"),
    ("http", "endpoint_policies"),
    ("http", "endpoint_policies", 0, "allowed_methods"),
    ("http", "endpoint_policies", 0, "injection_profile_ids"),
    ("http", "injection_profiles"),
)


def _set_nested_value(payload: object, path: tuple[str | int, ...], value: object) -> None:
    cursor = payload
    for part in path[:-1]:
        cursor = cursor[part]  # type: ignore[index]
    cursor[path[-1]] = value  # type: ignore[index]


def _get_nested_value(payload: object, path: tuple[str | int, ...]) -> object:
    cursor = payload
    for part in path:
        cursor = cursor[part]  # type: ignore[index]
    return cursor


def _collection_authority_payload(model: type[object]) -> tuple[dict[str, object], tuple[str, ...]]:
    policy = _runtime_policy_with_every_collection()
    policy_payload = policy.model_dump(mode="json")
    stored = {
        "policy_version_id": "53f5a2b9-1c63-43ec-92d4-2aa799f18857",
        "revision": 7,
        "schema_version": 1,
        "payload_checksum": workflow_runtime_policy_checksum(policy),
        "value": policy_payload,
    }
    projection = {
        "section": "workflow_runtime",
        "stored": stored,
        "effective": {
            "policy_version_id": stored["policy_version_id"],
            "revision": stored["revision"],
            "schema_version": stored["schema_version"],
            "payload_checksum": stored["payload_checksum"],
        },
        "effect_scope": "new_workflow_runs",
        "pending_roles": [],
        "readiness": {
            "status": "ready",
            "code": "WORKFLOW_RUNTIME_DISABLED",
            "admission_ready": False,
        },
    }
    if model is WorkflowRuntimePolicyV1:
        return policy_payload, ()
    if model is WorkflowRuntimePolicyUpdateRequestV1:
        return {"expected_revision": 7, "value": policy_payload}, ("value",)
    if model is WorkflowRuntimeStoredPolicyV1:
        return stored, ("value",)
    if model is WorkflowRuntimeAdminPolicyV1:
        return projection, ("stored", "value")
    if model is WorkflowRuntimePolicyUpdateResponseV1:
        return {"catalog_revision": 12, **projection}, ("stored", "value")
    raise AssertionError("unknown Workflow runtime authority model")


def test_every_external_workflow_runtime_authority_rejects_non_json_array_collection_shapes() -> None:
    authority_models = (
        WorkflowRuntimePolicyV1,
        WorkflowRuntimePolicyUpdateRequestV1,
        WorkflowRuntimeStoredPolicyV1,
        WorkflowRuntimeAdminPolicyV1,
        WorkflowRuntimePolicyUpdateResponseV1,
    )
    bad_inputs = (
        ("tuple", lambda current: tuple(current)),  # type: ignore[arg-type]
        ("set", lambda _current: {"not-a-json-array"}),
        ("generator", lambda current: (item for item in current)),  # type: ignore[union-attr]
        ("string", lambda _current: "not-a-json-array"),
    )

    for model in authority_models:
        for collection_path in _IMMUTABLE_COLLECTION_PATHS:
            for _bad_name, bad_factory in bad_inputs:
                payload, prefix = _collection_authority_payload(model)
                path = (*prefix, *collection_path)
                current = _get_nested_value(payload, path)
                _set_nested_value(payload, path, bad_factory(current))
                with pytest.raises(ValidationError):
                    model.model_validate(payload)


def test_external_admin_projection_and_response_pending_roles_accept_only_json_arrays() -> None:
    for model in (WorkflowRuntimeAdminPolicyV1, WorkflowRuntimePolicyUpdateResponseV1):
        for bad_pending_roles in (
            (),
            {"worker"},
            (role for role in ()),
            "worker",
        ):
            payload, _prefix = _collection_authority_payload(model)
            payload["pending_roles"] = bad_pending_roles
            with pytest.raises(ValidationError):
                model.model_validate(payload)


def test_every_external_workflow_runtime_authority_accepts_json_arrays_and_freezes_them() -> None:
    for model in (
        WorkflowRuntimePolicyV1,
        WorkflowRuntimePolicyUpdateRequestV1,
        WorkflowRuntimeStoredPolicyV1,
        WorkflowRuntimeAdminPolicyV1,
        WorkflowRuntimePolicyUpdateResponseV1,
    ):
        payload, _prefix = _collection_authority_payload(model)
        parsed = model.model_validate_json(json.dumps(payload))
        if isinstance(parsed, WorkflowRuntimePolicyV1):
            policy = parsed
        elif isinstance(parsed, WorkflowRuntimePolicyUpdateRequestV1):
            policy = parsed.value
        elif isinstance(parsed, WorkflowRuntimeStoredPolicyV1):
            policy = parsed.value
        else:
            policy = parsed.stored.value
            assert isinstance(parsed.pending_roles, tuple)

        for path in _IMMUTABLE_COLLECTION_PATHS:
            assert isinstance(_get_nested_value(policy.model_dump(mode="python"), path), tuple)


def test_trusted_factories_revalidate_frozen_policy_without_weakening_external_ingress() -> None:
    policy = _runtime_policy_with_every_collection()
    with pytest.raises(ValidationError):
        WorkflowRuntimePolicyV1.model_validate(policy)

    revalidated = revalidate_trusted_workflow_runtime_policy(policy)
    assert revalidated == policy
    assert revalidated is not policy

    stored = create_workflow_runtime_stored_policy(
        policy_version_id=uuid.UUID("53f5a2b9-1c63-43ec-92d4-2aa799f18857"),
        revision=7,
        schema_version=1,
        payload_checksum=workflow_runtime_policy_checksum(policy),
        value=policy,
    )
    effective = WorkflowRuntimeEffectivePolicyV1.model_validate(
        {
            "policy_version_id": stored.policy_version_id,
            "revision": stored.revision,
            "schema_version": stored.schema_version,
            "payload_checksum": stored.payload_checksum,
        }
    )
    readiness = WORKFLOW_RUNTIME_READINESS_V1_ADAPTER.validate_python(
        {
            "status": "ready",
            "code": "WORKFLOW_RUNTIME_DISABLED",
            "admission_ready": False,
        }
    )
    projection = create_workflow_runtime_admin_policy(
        stored=stored,
        effective=effective,
        pending_roles=(),
        readiness=readiness,
    )
    response = create_workflow_runtime_update_response(
        catalog_revision=12,
        projection=projection,
    )

    assert projection.stored.value == policy
    assert response.stored.value == policy
    assert response.model_dump(mode="json")["pending_roles"] == []


def _stored_policy(*, revision: int = 7, enabled: bool = False, admission_enabled: bool = False) -> dict[str, object]:
    value = _runtime_policy_payload()
    value["enabled"] = enabled
    value["admission_enabled"] = admission_enabled
    policy = WorkflowRuntimePolicyV1.model_validate(value)
    return {
        "policy_version_id": uuid.UUID("53f5a2b9-1c63-43ec-92d4-2aa799f18857"),
        "revision": revision,
        "schema_version": 1,
        "payload_checksum": workflow_runtime_policy_checksum(policy),
        "value": value,
    }


def test_admin_update_request_is_typed_cas_not_arbitrary_json() -> None:
    request = WorkflowRuntimePolicyUpdateRequestV1.model_validate(
        {
            "expected_revision": 6,
            "value": _runtime_policy_payload(),
        }
    )

    assert request.expected_revision == 6
    assert isinstance(request.value, WorkflowRuntimePolicyV1)

    for field, value in (
        ("section", "workflow_runtime"),
        ("provider_locator", "https://provisioner.internal"),
        ("credential", "plaintext"),
    ):
        payload = {
            "expected_revision": 6,
            "value": _runtime_policy_payload(),
            field: value,
        }
        with pytest.raises(ValidationError):
            WorkflowRuntimePolicyUpdateRequestV1.model_validate(payload)


def test_admin_revision_integers_match_the_javascript_safe_range() -> None:
    fixture_cases = _shared_runtime_policy_fixture()["audit_negative_cases"]
    maximum = fixture_cases["max_safe_integer"]  # type: ignore[index]
    unsafe = fixture_cases["unsafe_integer"]  # type: ignore[index]

    WorkflowRuntimePolicyUpdateRequestV1.model_validate({"expected_revision": maximum, "value": _runtime_policy_payload()})
    WorkflowRuntimeStoredPolicyV1.model_validate(_stored_policy(revision=maximum))

    with pytest.raises(ValidationError):
        WorkflowRuntimePolicyUpdateRequestV1.model_validate({"expected_revision": unsafe, "value": _runtime_policy_payload()})
    with pytest.raises(ValidationError):
        WorkflowRuntimeStoredPolicyV1.model_validate(_stored_policy(revision=unsafe))


def test_stored_policy_checksum_is_canonical_and_fail_closed() -> None:
    stored_payload = _stored_policy()
    stored = WorkflowRuntimeStoredPolicyV1.model_validate(stored_payload)

    assert stored.payload_checksum == workflow_runtime_policy_checksum(stored.value)

    stored_payload["payload_checksum"] = "f" * 64
    with pytest.raises(ValidationError):
        WorkflowRuntimeStoredPolicyV1.model_validate(stored_payload)


@pytest.mark.parametrize(
    "uuid_case",
    json.loads(_SHARED_RUN_INVALID_FIXTURE.read_text(encoding="utf-8"))["uuid_values"],
    ids=lambda case: case["id"],
)
def test_stored_and_effective_policy_dtos_reject_noncanonical_uuid_text(uuid_case: dict[str, str]) -> None:
    stored = WorkflowRuntimeStoredPolicyV1.model_validate(_stored_policy()).model_dump(mode="json")
    effective = {
        "policy_version_id": stored["policy_version_id"],
        "revision": stored["revision"],
        "schema_version": stored["schema_version"],
        "payload_checksum": stored["payload_checksum"],
    }

    for model, payload in (
        (WorkflowRuntimeStoredPolicyV1, stored),
        (WorkflowRuntimeEffectivePolicyV1, effective),
    ):
        invalid = {**payload, "policy_version_id": uuid_case["value"]}
        with pytest.raises(ValidationError):
            model.model_validate_json(json.dumps(invalid))


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "ready", "code": "WORKFLOW_RUNTIME_READY", "admission_ready": True},
        {"status": "ready", "code": "WORKFLOW_RUNTIME_READY", "admission_ready": False},
        {"status": "ready", "code": "WORKFLOW_RUNTIME_DISABLED", "admission_ready": False},
        {"status": "pending", "code": "WORKFLOW_RUNTIME_PENDING", "admission_ready": False},
        {"status": "unavailable", "code": "WORKFLOW_RUNTIME_UNAVAILABLE", "admission_ready": False},
    ],
)
def test_admin_runtime_readiness_accepts_only_frozen_status_code_combinations(payload: dict[str, object]) -> None:
    readiness = WORKFLOW_RUNTIME_READINESS_V1_ADAPTER.validate_python(payload)

    assert readiness.code == payload["code"]


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "ready", "code": "WORKFLOW_RUNTIME_PENDING", "admission_ready": False},
        {"status": "pending", "code": "WORKFLOW_RUNTIME_READY", "admission_ready": False},
        {"status": "unavailable", "code": "WORKFLOW_RUNTIME_DISABLED", "admission_ready": False},
        {"status": "ready", "code": "WORKFLOW_RUNTIME_DISABLED", "admission_ready": True},
        {"status": "pending", "code": "WORKFLOW_RUNTIME_PENDING", "admission_ready": True},
        {
            "status": "ready",
            "code": "WORKFLOW_RUNTIME_READY",
            "admission_ready": False,
            "worker_id": "private",
        },
    ],
)
def test_admin_runtime_readiness_rejects_contradictions_and_private_fields(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        WORKFLOW_RUNTIME_READINESS_V1_ADAPTER.validate_python(payload)


def test_admin_policy_projection_freezes_stored_effective_pending_and_effect_scope() -> None:
    stored = _stored_policy(revision=7, enabled=True, admission_enabled=True)
    effective = {
        "policy_version_id": stored["policy_version_id"],
        "revision": 7,
        "schema_version": 1,
        "payload_checksum": stored["payload_checksum"],
    }
    projection = WorkflowRuntimeAdminPolicyV1.model_validate(
        {
            "section": "workflow_runtime",
            "stored": stored,
            "effective": effective,
            "effect_scope": "new_workflow_runs",
            "pending_roles": [],
            "readiness": {
                "status": "ready",
                "code": "WORKFLOW_RUNTIME_READY",
                "admission_ready": True,
            },
        }
    )

    assert projection.stored.revision == projection.effective.revision
    assert projection.effect_scope == WORKFLOW_RUNTIME_POLICY_EFFECT_SCOPE
    assert WorkflowRuntimeAdminPolicyV1.model_validate_json(projection.model_dump_json()) == projection

    serialized = projection.model_dump(mode="json")
    serialized["effect_scope"] = "new_requests_and_runs"
    with pytest.raises(ValidationError):
        WorkflowRuntimeAdminPolicyV1.model_validate(serialized)


def test_pending_projection_keeps_exact_gateway_effective_and_closed_canonical_pending_roles() -> None:
    stored = _stored_policy(revision=8, enabled=True, admission_enabled=True)
    effective = {
        "policy_version_id": stored["policy_version_id"],
        "revision": stored["revision"],
        "schema_version": 1,
        "payload_checksum": stored["payload_checksum"],
    }
    payload = {
        "section": "workflow_runtime",
        "stored": stored,
        "effective": effective,
        "effect_scope": "new_workflow_runs",
        "pending_roles": ["worker"],
        "readiness": {
            "status": "pending",
            "code": "WORKFLOW_RUNTIME_PENDING",
            "admission_ready": False,
        },
    }

    projection = WorkflowRuntimeAdminPolicyV1.model_validate(payload)
    assert projection.pending_roles == ("worker",)

    for roles in (
        ["gateway"],
        ["worker", "gateway"],
        ["gateway", "gateway"],
        ["gateway", "provisioner"],
        [],
    ):
        invalid = deepcopy(payload)
        invalid["pending_roles"] = roles
        with pytest.raises(ValidationError):
            WorkflowRuntimeAdminPolicyV1.model_validate(invalid)

    for invalid_effective in (
        None,
        {
            "policy_version_id": uuid.UUID("08b4fbf2-6f37-41bf-baa6-01493872ac03"),
            "revision": 7,
            "schema_version": 1,
            "payload_checksum": "e" * 64,
        },
    ):
        invalid = deepcopy(payload)
        invalid["effective"] = invalid_effective
        with pytest.raises(ValidationError):
            WorkflowRuntimeAdminPolicyV1.model_validate(invalid)


def test_unavailable_projection_has_one_fail_closed_materialization_shape() -> None:
    stored = _stored_policy(revision=8, enabled=True, admission_enabled=True)
    payload = {
        "section": "workflow_runtime",
        "stored": stored,
        "effective": None,
        "effect_scope": "new_workflow_runs",
        "pending_roles": ["gateway"],
        "readiness": {
            "status": "unavailable",
            "code": "WORKFLOW_RUNTIME_UNAVAILABLE",
            "admission_ready": False,
        },
    }

    projection = WorkflowRuntimeAdminPolicyV1.model_validate(payload)
    assert projection.effective is None
    assert projection.pending_roles == ("gateway",)

    exact_effective = {
        "policy_version_id": stored["policy_version_id"],
        "revision": stored["revision"],
        "schema_version": stored["schema_version"],
        "payload_checksum": stored["payload_checksum"],
    }
    for effective, pending_roles in (
        (exact_effective, ["gateway"]),
        (None, []),
        (None, ["worker"]),
        (None, ["gateway", "worker"]),
    ):
        invalid = deepcopy(payload)
        invalid["effective"] = effective
        invalid["pending_roles"] = pending_roles
        with pytest.raises(ValidationError):
            WorkflowRuntimeAdminPolicyV1.model_validate(invalid)


def test_admin_projection_truth_table_is_closed_for_every_valid_policy_mode() -> None:
    cases = {
        "disabled_ready": {
            "pending_roles": [],
            "readiness": {
                "status": "ready",
                "code": "WORKFLOW_RUNTIME_DISABLED",
                "admission_ready": False,
            },
            "effective": "exact",
        },
        "builder_ready": {
            "pending_roles": [],
            "readiness": {
                "status": "ready",
                "code": "WORKFLOW_RUNTIME_READY",
                "admission_ready": False,
            },
            "effective": "exact",
        },
        "admission_ready": {
            "pending_roles": [],
            "readiness": {
                "status": "ready",
                "code": "WORKFLOW_RUNTIME_READY",
                "admission_ready": True,
            },
            "effective": "exact",
        },
        "pending_worker": {
            "pending_roles": ["worker"],
            "readiness": {
                "status": "pending",
                "code": "WORKFLOW_RUNTIME_PENDING",
                "admission_ready": False,
            },
            "effective": "exact",
        },
        "pending_gateway": {
            "pending_roles": ["gateway"],
            "readiness": {
                "status": "pending",
                "code": "WORKFLOW_RUNTIME_PENDING",
                "admission_ready": False,
            },
            "effective": "exact",
        },
        "pending_scheduler": {
            "pending_roles": ["scheduler"],
            "readiness": {
                "status": "pending",
                "code": "WORKFLOW_RUNTIME_PENDING",
                "admission_ready": False,
            },
            "effective": "exact",
        },
        "pending_multiple": {
            "pending_roles": ["gateway", "worker"],
            "readiness": {
                "status": "pending",
                "code": "WORKFLOW_RUNTIME_PENDING",
                "admission_ready": False,
            },
            "effective": "exact",
        },
        "unavailable": {
            "pending_roles": ["gateway"],
            "readiness": {
                "status": "unavailable",
                "code": "WORKFLOW_RUNTIME_UNAVAILABLE",
                "admission_ready": False,
            },
            "effective": None,
        },
    }
    valid_by_mode = {
        (False, False): {"disabled_ready", "unavailable"},
        (True, False): {"builder_ready", "unavailable"},
        (True, True): {"admission_ready", "pending_worker", "unavailable"},
    }

    for mode, valid_cases in valid_by_mode.items():
        stored = _stored_policy(enabled=mode[0], admission_enabled=mode[1])
        exact = {
            "policy_version_id": stored["policy_version_id"],
            "revision": stored["revision"],
            "schema_version": stored["schema_version"],
            "payload_checksum": stored["payload_checksum"],
        }
        for name, case in cases.items():
            payload = {
                "section": "workflow_runtime",
                "stored": stored,
                "effective": exact if case["effective"] == "exact" else None,
                "effect_scope": "new_workflow_runs",
                "pending_roles": case["pending_roles"],
                "readiness": case["readiness"],
            }
            if name in valid_cases:
                projection = WorkflowRuntimeAdminPolicyV1.model_validate(payload)
                assert isinstance(projection.pending_roles, tuple)
                assert isinstance(projection.model_dump(mode="json")["pending_roles"], list)
                with pytest.raises(AttributeError):
                    projection.pending_roles.append("worker")  # type: ignore[attr-defined]
            else:
                with pytest.raises(ValidationError):
                    WorkflowRuntimeAdminPolicyV1.model_validate(payload)


def test_ready_projection_requires_exact_effective_policy_and_consistent_disabled_state() -> None:
    stored = _stored_policy(revision=7, enabled=False, admission_enabled=False)
    effective = WorkflowRuntimeEffectivePolicyV1.model_validate(
        {
            "policy_version_id": stored["policy_version_id"],
            "revision": stored["revision"],
            "schema_version": stored["schema_version"],
            "payload_checksum": stored["payload_checksum"],
        }
    )
    payload = {
        "section": "workflow_runtime",
        "stored": stored,
        "effective": effective,
        "effect_scope": "new_workflow_runs",
        "pending_roles": [],
        "readiness": {
            "status": "ready",
            "code": "WORKFLOW_RUNTIME_DISABLED",
            "admission_ready": False,
        },
    }
    assert WorkflowRuntimeAdminPolicyV1.model_validate(payload).readiness.code == "WORKFLOW_RUNTIME_DISABLED"

    payload["readiness"] = {
        "status": "ready",
        "code": "WORKFLOW_RUNTIME_READY",
        "admission_ready": False,
    }
    with pytest.raises(ValidationError):
        WorkflowRuntimeAdminPolicyV1.model_validate(payload)


def test_builder_only_projection_is_effective_and_ready_without_admission() -> None:
    stored = _stored_policy(revision=7, enabled=True, admission_enabled=False)
    projection = WorkflowRuntimeAdminPolicyV1.model_validate(
        {
            "section": "workflow_runtime",
            "stored": stored,
            "effective": {
                "policy_version_id": stored["policy_version_id"],
                "revision": stored["revision"],
                "schema_version": stored["schema_version"],
                "payload_checksum": stored["payload_checksum"],
            },
            "effect_scope": "new_workflow_runs",
            "pending_roles": [],
            "readiness": {
                "status": "ready",
                "code": "WORKFLOW_RUNTIME_READY",
                "admission_ready": False,
            },
        }
    )

    assert projection.effective is not None
    assert projection.effective.revision == projection.stored.revision
    assert projection.pending_roles == ()
    assert projection.readiness.admission_ready is False


def test_update_response_adds_only_catalog_revision_to_safe_projection() -> None:
    stored = _stored_policy(revision=7)
    payload = {
        "catalog_revision": 12,
        "section": "workflow_runtime",
        "stored": stored,
        "effective": {
            "policy_version_id": stored["policy_version_id"],
            "revision": stored["revision"],
            "schema_version": stored["schema_version"],
            "payload_checksum": stored["payload_checksum"],
        },
        "effect_scope": "new_workflow_runs",
        "pending_roles": [],
        "readiness": {
            "status": "ready",
            "code": "WORKFLOW_RUNTIME_DISABLED",
            "admission_ready": False,
        },
    }

    response = WorkflowRuntimePolicyUpdateResponseV1.model_validate(payload)
    assert response.catalog_revision == 12

    payload["system_credential_id"] = "must-not-leak"
    with pytest.raises(ValidationError):
        WorkflowRuntimePolicyUpdateResponseV1.model_validate(payload)

    payload.pop("system_credential_id")
    payload["catalog_revision"] = _shared_runtime_policy_fixture()["audit_negative_cases"]["unsafe_integer"]  # type: ignore[index]
    with pytest.raises(ValidationError):
        WorkflowRuntimePolicyUpdateResponseV1.model_validate(payload)

    payload = _runtime_policy_payload()
    payload["catalog"]["allowed_type_versions"][0] = {"type": "agent", "versions": [1]}  # type: ignore[index]
    with pytest.raises(ValidationError):
        WorkflowRuntimePolicyV1.model_validate(payload)

    payload = _runtime_policy_payload()
    payload["catalog"]["allowed_type_versions"][0] = {"type": "start", "versions": [2]}  # type: ignore[index]
    with pytest.raises(ValidationError):
        WorkflowRuntimePolicyV1.model_validate(payload)
