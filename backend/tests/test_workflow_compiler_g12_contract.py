from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError
from support.workflow_compiler_g12 import (
    AGGREGATE_ID,
    CONDITION_ID,
    LOOP_ID,
    START_ID,
    condition_aggregate_spec_payload,
    loop_spec_payload,
)

from deerflow.workflows import WORKFLOW_NODE_KINDS, CanvasDocumentV1, WorkflowSpecV1, semantic_checksum
from deerflow.workflows.catalog_contracts import FIRST_BATCH_NODE_REGISTRY_V1
from deerflow.workflows.compiler import (
    CURRENT_COMPILER_CONTRACT_VERSION,
    WorkflowCompilerUnavailableError,
    require_compiler_contract,
)
from deerflow.workflows.migration import (
    CURRENT_WORKFLOW_SCHEMA_VERSION,
    WorkflowSchemaMigrationError,
    migrate_workflow_spec,
    parse_canvas_document,
    parse_workflow_spec,
    round_trip_workflow_spec,
)
from deerflow.workflows.registry import (
    FIRST_BATCH_RUNTIME_REGISTRY,
    WorkflowCodeExecutionPort,
    WorkflowHttpExecutionPort,
    WorkflowLlmExecutionPort,
    WorkflowNodeExecutor,
    WorkflowNodeRegistryError,
)


def test_schema_v1_parse_migration_and_round_trip_are_strict_and_checksum_stable() -> None:
    payload = condition_aggregate_spec_payload()

    parsed = parse_workflow_spec(payload)
    migrated = migrate_workflow_spec(payload, target_schema_version=1)
    round_tripped = round_trip_workflow_spec(parsed)

    assert CURRENT_WORKFLOW_SCHEMA_VERSION == 1
    assert parsed == migrated == round_tripped
    assert semantic_checksum(parsed) == semantic_checksum(round_tripped)
    assert json.loads(parsed.model_dump_json(by_alias=True, exclude_unset=True)) == parsed.model_dump(
        mode="json",
        by_alias=True,
        exclude_unset=True,
    )


def test_workflow_contract_normalizes_every_string_to_nfc_and_rejects_key_collisions() -> None:
    payload = loop_spec_payload()
    decomposed = "sche\u0301ma"
    composed = "schéma"
    payload["workflow_inputs"] = [
        {
            "id": "document",
            "name": "Document",
            "label": None,
            "description": decomposed,
            "value_type": {
                "kind": "json",
                "collection": False,
                "nullable": False,
                "schema_ref": decomposed,
            },
            "required": False,
            "constraints": {"kind": "none"},
        }
    ]
    loop = next(item for item in payload["nodes"] if item["id"] == LOOP_ID)
    loop["config"]["variables"][0]["id"] = decomposed
    loop["config"]["termination_condition"]["items"][0]["left"]["variable_id"] = decomposed

    parsed = WorkflowSpecV1.model_validate(payload)
    parsed_loop = next(item for item in parsed.nodes if item.id == LOOP_ID)

    assert parsed.workflow_inputs[0].description == composed
    assert parsed.workflow_inputs[0].value_type.schema_ref == composed
    assert parsed_loop.config.variables[0].id == composed
    assert parsed_loop.config.termination_condition.items[0].left.variable_id == composed

    payload = condition_aggregate_spec_payload()
    aggregate = next(item for item in payload["nodes"] if item["id"] == AGGREGATE_ID)
    aggregate["input_bindings"] = {
        composed: aggregate["input_bindings"]["left"],
        decomposed: aggregate["input_bindings"]["right"],
    }
    with pytest.raises(ValidationError, match="normalization.*duplicate"):
        WorkflowSpecV1.model_validate(payload)


@pytest.mark.parametrize(
    ("node_id", "field"),
    [
        (CONDITION_ID, "branches"),
        (AGGREGATE_ID, "groups"),
        (LOOP_ID, "variables"),
    ],
)
def test_first_batch_routing_and_stateful_config_collections_require_one_item(
    node_id: str,
    field: str,
) -> None:
    payload = loop_spec_payload() if node_id == LOOP_ID else condition_aggregate_spec_payload()
    target = next(item for item in payload["nodes"] if item["id"] == node_id)
    target["config"][field] = []

    with pytest.raises(ValidationError):
        WorkflowSpecV1.model_validate(payload)


@pytest.mark.parametrize("schema_version", [0, 2, "1", None, True])
def test_unknown_or_non_strict_workflow_schema_versions_fail_closed(schema_version: object) -> None:
    payload = condition_aggregate_spec_payload()
    payload["schema_version"] = schema_version

    with pytest.raises(WorkflowSchemaMigrationError, match="WORKFLOW_SCHEMA_UNSUPPORTED"):
        parse_workflow_spec(payload)
    with pytest.raises(WorkflowSchemaMigrationError, match="WORKFLOW_SCHEMA_UNSUPPORTED"):
        migrate_workflow_spec(payload, target_schema_version=1)


def test_migration_does_not_mutate_caller_payload_and_rejects_unknown_fields() -> None:
    payload = condition_aggregate_spec_payload()
    original = deepcopy(payload)
    assert migrate_workflow_spec(payload) == WorkflowSpecV1.model_validate(payload)
    assert payload == original

    payload["runtime"] = {"owner_id": "forged"}
    with pytest.raises(ValidationError):
        migrate_workflow_spec(payload)


def test_canvas_parser_is_strict_and_does_not_accept_react_flow_session_state() -> None:
    payload = {
        "schema_version": 1,
        "node_layouts": [
            {
                "node_id": START_ID,
                "position": {"x": 1, "y": 2},
            }
        ],
        "edge_layouts": [],
    }
    assert parse_canvas_document(payload) == CanvasDocumentV1.model_validate(payload)

    payload["viewport"] = {"x": 0, "y": 0, "zoom": 1}
    with pytest.raises(ValidationError):
        parse_canvas_document(payload)


def test_first_batch_runtime_registry_is_exact_immutable_and_has_only_injected_external_ports() -> None:
    registry = FIRST_BATCH_RUNTIME_REGISTRY

    assert registry.keys() == tuple((kind, 1) for kind in WORKFLOW_NODE_KINDS)
    assert registry.require("start", 1).executor_port is None
    assert registry.require("condition", 1).executor_port is None
    assert registry.require("transform", 1).executor_port is None
    assert registry.require("variable_aggregate", 1).executor_port is None
    assert registry.require("loop", 1).executor_port is None
    assert registry.require("end", 1).executor_port is None
    assert registry.require("llm", 1).executor_port == "llm"
    assert registry.require("python_code", 1).executor_port == "code"
    assert registry.require("http_request", 1).executor_port == "http"
    assert not hasattr(registry.require("llm", 1), "executor")

    with pytest.raises((AttributeError, TypeError)):
        registry.require("start", 1).title_zh_cn = "changed"  # type: ignore[misc]
    with pytest.raises(WorkflowNodeRegistryError, match="WORKFLOW_NODE_TYPE_UNAVAILABLE"):
        registry.require("agent", 1)
    with pytest.raises(WorkflowNodeRegistryError, match="WORKFLOW_NODE_VERSION_UNAVAILABLE"):
        registry.require("start", 2)


def test_runtime_registry_wraps_every_shared_manifest_definition_by_identity_without_copying_authority() -> None:
    shared_by_key = {(definition.type, definition.version): definition for definition in FIRST_BATCH_NODE_REGISTRY_V1}

    assert FIRST_BATCH_RUNTIME_REGISTRY.keys() == tuple(shared_by_key)
    for key in FIRST_BATCH_RUNTIME_REGISTRY.keys():
        assert FIRST_BATCH_RUNTIME_REGISTRY.require(*key).shared is shared_by_key[key]


def test_executor_and_external_side_effect_boundaries_are_protocols_not_implementations() -> None:
    assert getattr(WorkflowNodeExecutor, "_is_protocol", False)
    assert getattr(WorkflowLlmExecutionPort, "_is_protocol", False)
    assert getattr(WorkflowCodeExecutionPort, "_is_protocol", False)
    assert getattr(WorkflowHttpExecutionPort, "_is_protocol", False)


def test_compiler_contract_dispatch_is_exact_and_old_or_future_contracts_fail_closed() -> None:
    assert CURRENT_COMPILER_CONTRACT_VERSION == 1
    assert require_compiler_contract(1).contract_version == 1
    for value in (0, 2, "1", True):
        with pytest.raises(WorkflowCompilerUnavailableError, match="WORKFLOW_COMPILER_UNAVAILABLE"):
            require_compiler_contract(value)  # type: ignore[arg-type]


def test_harness_workflow_package_never_imports_app_across_a_fresh_process() -> None:
    script = """
import sys
import deerflow.workflows.compiler
import deerflow.workflows.migration
import deerflow.workflows.registry
assert not any(name == 'app' or name.startswith('app.') for name in sys.modules)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
