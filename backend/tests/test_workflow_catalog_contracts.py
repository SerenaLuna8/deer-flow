from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import uuid
from collections.abc import Mapping
from copy import deepcopy
from json import loads
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.workflows import catalog_contracts as app_catalog_contracts
from app.workflows.catalog_contracts import (
    FIRST_BATCH_NODE_REGISTRY_V1,
    FIRST_BATCH_NODE_TITLES,
    NodeCatalogEntry,
    NodeCatalogResponseV1,
    NodeTypeDefinition,
    PortDefinition,
    ResolvedNodeInstancePortsV1,
    derive_availability_generation,
    derive_catalog_generation,
    first_batch_node_registry_manifest_checksum_v1,
    first_batch_node_registry_manifest_v1,
    resolve_workflow_instance_ports_v1,
    resolved_workflow_instance_ports_public_projection_v1,
    validate_node_config_v1,
)
from deerflow.workflows import WORKFLOW_NODE_KINDS, WorkflowSpecV1
from deerflow.workflows import catalog_contracts as shared_catalog_contracts

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_SHARED_REGISTRY_GOLDEN = _REPOSITORY_ROOT / "frontend/src/core/project-workflows/node-registry-v1.json"
_SHARED_INSTANCE_PORTS_GOLDEN = _REPOSITORY_ROOT / "frontend/tests/fixtures/workflows/instance-ports-v1.json"
_SHARED_WORKFLOW_SPEC = _REPOSITORY_ROOT / "frontend/tests/fixtures/workflows/workflow-spec-v1.json"
_SHARED_NODE_CONFIG_CORPUS = _REPOSITORY_ROOT / "frontend/tests/fixtures/workflows/node-config-corpus-v1.json"
_SHARED_RUN_INVALID_FIXTURE = _REPOSITORY_ROOT / "frontend/tests/fixtures/workflows/workflow-run-invalid-v1.json"
_REGISTRY_GENERATOR = _REPOSITORY_ROOT / "backend/scripts/generate_workflow_node_registry.py"
_FRONTEND_PRETTIER = _REPOSITORY_ROOT / "frontend/node_modules/.bin/prettier"
_SHARED_PUBLIC_PROJECTIONS = _REPOSITORY_ROOT / "frontend/tests/fixtures/workflows/public-projections-v1.json"
_SHARED_UNICODE_BOUNDARIES = _REPOSITORY_ROOT / "frontend/tests/fixtures/workflows/unicode-code-point-boundaries-v1.json"


@pytest.mark.parametrize(
    "uuid_case",
    json.loads(_SHARED_RUN_INVALID_FIXTURE.read_text(encoding="utf-8"))["uuid_values"],
    ids=lambda case: case["id"],
)
def test_resolved_node_ports_reject_noncanonical_uuid_text(uuid_case: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        ResolvedNodeInstancePortsV1.model_validate_json(
            json.dumps(
                {
                    "node_id": uuid_case["value"],
                    "input_ports": [],
                    "output_ports": [],
                }
            )
        )


def _value_type() -> dict[str, object]:
    return {
        "kind": "string",
        "collection": False,
        "nullable": False,
    }


def _definition(node_type: str = "start") -> dict[str, object]:
    retry_semantics = {
        "start": "pure",
        "llm": "read",
        "condition": "pure",
        "transform": "pure",
        "variable_aggregate": "pure",
        "loop": "loop_body_v1",
        "http_request": "http_method_v1",
        "python_code": "isolated_compute",
        "end": "pure",
    }
    capabilities = {
        "http_request": ["workflow.http.use"],
        "python_code": ["workflow.code.use"],
    }
    return {
        "type": node_type,
        "version": 1,
        "renderer_key": node_type,
        "title_i18n": dict(FIRST_BATCH_NODE_TITLES[node_type]),
        "config_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "input_ports": [],
        "output_ports": [],
        "port_derivation": {
            "version": 1,
            "input_source": "none",
            "output_source": {
                "start": "workflow_inputs",
                "llm": "llm_result_v1",
                "condition": "condition_branches",
                "transform": "transform_result_v1",
                "variable_aggregate": "aggregate_groups",
                "loop": "loop_variables",
                "http_request": "http_response_body",
                "python_code": "python_result_v1",
                "end": "none",
            }[node_type],
        },
        "required_capabilities": capabilities.get(node_type, []),
        "retry_semantics": retry_semantics[node_type],
        "supports_streaming": node_type == "llm",
    }


def _catalog_entry(node_type: str = "start") -> dict[str, object]:
    definition = next(definition for definition in first_batch_node_registry_manifest_v1() if definition["type"] == node_type)
    return {
        "definition": definition,
        "availability": {"state": "enabled", "reason_code": None},
        "public_limits": None,
    }


def _port_projection(port: PortDefinition) -> str:
    if port.kind == "control":
        return f"control:{port.id}"
    assert port.value_type is not None
    return f"data:{port.id}:{port.value_type.kind}:{str(port.value_type.collection).lower()}:{str(port.value_type.nullable).lower()}"


def _mutate_node_config(config: dict[str, object], mutation: str) -> None:
    if mutation == "none":
        return
    if mutation == "add_unknown_field":
        config["unexpected"] = True
        return
    if mutation == "transform_text_with_json_shape":
        config["template"] = {"version": 1, "template": {}, "bindings": {}}
        config["output_schema"] = {"type": "object"}
        return
    if mutation == "http_reversed_status_range":
        config["response"]["accepted_statuses"][0] = {"from": 300, "to": 200}  # type: ignore[index]
        return
    if mutation == "llm_null_node_output_path":
        config["messages"][0]["content"]["segments"] = [  # type: ignore[index]
            {
                "kind": "binding",
                "value": {
                    "kind": "node_output",
                    "node_id": "00000000-0000-4000-8000-000000000002",
                    "output_id": "text",
                    "path": None,
                },
            }
        ]
        return
    if mutation == "loop_null_schema_ref":
        config["variables"][0]["value_type"]["schema_ref"] = None  # type: ignore[index]
        return
    if mutation == "condition_fixed_output_collision":
        config["branches"][0]["output_port_id"] = "error"  # type: ignore[index]
        return
    if mutation == "aggregate_fixed_output_collision":
        config["groups"][0]["id"] = "next"  # type: ignore[index]
        return
    if mutation == "loop_fixed_output_collision":
        config["variables"][0]["output_port_id"] = "body"  # type: ignore[index]
        return
    raise AssertionError(f"unknown shared node-config mutation: {mutation}")


def _manifest_schema_accepts(
    *,
    node_type: str,
    schema: dict[str, object],
    config: dict[str, object],
) -> bool:
    Draft202012Validator.check_schema(schema)
    if list(Draft202012Validator(schema).iter_errors(config)):
        return False
    extension = schema.get("x-actweave-validation")
    if extension is None:
        return True
    expected_rules = {
        "condition": ["condition_output_ports_resolvable"],
        "variable_aggregate": ["aggregate_output_ports_resolvable"],
        "loop": ["loop_output_ports_resolvable"],
        "http_request": ["http_accepted_status_ranges_ordered"],
    }
    if extension != {"version": 1, "rules": expected_rules.get(node_type)}:
        raise AssertionError("unknown manifest config validation extension")
    if node_type == "condition":
        branches = config["branches"]  # type: ignore[assignment]
        port_ids = [branch["output_port_id"] for branch in branches]
        else_port_id = config["else_output_port_id"]
        return len(port_ids) == len(set(port_ids)) and else_port_id not in port_ids and "error" not in {*port_ids, else_port_id}
    if node_type == "variable_aggregate":
        port_ids = [group["id"] for group in config["groups"]]  # type: ignore[index]
        return len(port_ids) == len(set(port_ids)) and not ({"next", "error"} & set(port_ids))
    if node_type == "loop":
        port_ids = [variable["output_port_id"] for variable in config["variables"]]  # type: ignore[index]
        return len(port_ids) == len(set(port_ids)) and not ({"body", "next", "error", "iteration_count"} & set(port_ids))
    ranges = config["response"]["accepted_statuses"]  # type: ignore[index]
    return all(item["from"] <= item["to"] for item in ranges)  # type: ignore[index]


def test_first_batch_registry_matches_the_complete_shared_golden_manifest() -> None:
    golden = loads(_SHARED_REGISTRY_GOLDEN.read_text(encoding="utf-8"))
    actual = first_batch_node_registry_manifest_v1()

    assert actual == golden
    assert [(definition.type, definition.version) for definition in FIRST_BATCH_NODE_REGISTRY_V1] == [(node_type, 1) for node_type in WORKFLOW_NODE_KINDS]


def test_shared_registry_authority_is_deeply_immutable_and_checksum_stable() -> None:
    manifest_before = first_batch_node_registry_manifest_v1()
    start = FIRST_BATCH_NODE_REGISTRY_V1[0]
    data_port = next(port for definition in FIRST_BATCH_NODE_REGISTRY_V1 for port in (*definition.input_ports, *definition.output_ports) if port.value_type is not None)
    python_code = next(definition for definition in FIRST_BATCH_NODE_REGISTRY_V1 if definition.type == "python_code")
    original_title = FIRST_BATCH_NODE_TITLES["start"]["zh-CN"]
    original_value_kind = data_port.value_type.kind
    original_output_ports = list(start.output_ports)
    original_capabilities = list(python_code.required_capabilities)
    assert type(start.output_ports) is tuple
    assert type(python_code.required_capabilities) is tuple
    assert isinstance(start.config_schema, Mapping)
    assert not isinstance(start.config_schema, dict)
    schema_properties = start.config_schema["properties"]
    schema_array = next(item for definition in FIRST_BATCH_NODE_REGISTRY_V1 for item in definition.config_schema.values() if type(item) is tuple)
    try:
        with pytest.raises(TypeError):
            dict.__setitem__(
                FIRST_BATCH_NODE_TITLES,
                "start",
                {"zh-CN": "漂移", "en-US": "Drift"},
            )
        with pytest.raises(TypeError):
            dict.__setitem__(FIRST_BATCH_NODE_TITLES["start"], "zh-CN", "漂移")
        with pytest.raises(TypeError):
            dict.__setitem__(start.config_schema, "x-mutated", True)
        with pytest.raises(TypeError):
            dict.__setitem__(schema_properties, "x-mutated", True)
        with pytest.raises(TypeError):
            list.append(schema_array, "x-mutated")
        with pytest.raises(TypeError):
            list.clear(start.output_ports)
        with pytest.raises(TypeError):
            list.append(python_code.required_capabilities, "workflow.http.use")
        with pytest.raises(ValidationError):
            data_port.value_type.kind = "json"  # type: ignore[misc]
    finally:
        # This cleanup is reached only by the pre-fix red test; immutable
        # authority never enters any of these branches.
        if FIRST_BATCH_NODE_TITLES["start"]["zh-CN"] != original_title:
            dict.__setitem__(FIRST_BATCH_NODE_TITLES["start"], "zh-CN", original_title)
        if "x-mutated" in start.config_schema:
            dict.__delitem__(start.config_schema, "x-mutated")
        if list(start.output_ports) != original_output_ports:
            list.clear(start.output_ports)
            list.extend(start.output_ports, original_output_ports)
        if list(python_code.required_capabilities) != original_capabilities:
            list.clear(python_code.required_capabilities)
            list.extend(python_code.required_capabilities, original_capabilities)
        if data_port.value_type.kind != original_value_kind:
            object.__setattr__(data_port.value_type, "kind", original_value_kind)

    assert first_batch_node_registry_manifest_v1() == manifest_before
    assert first_batch_node_registry_manifest_checksum_v1() == "a667832d211e96a953c02a662f832184941441145e3bc768d73e28f298055c24"


def test_app_catalog_compatibility_surface_reexports_the_neutral_harness_authority() -> None:
    assert app_catalog_contracts.FIRST_BATCH_NODE_REGISTRY_V1 is shared_catalog_contracts.FIRST_BATCH_NODE_REGISTRY_V1
    assert app_catalog_contracts.NodeTypeDefinition is shared_catalog_contracts.NodeTypeDefinition
    assert app_catalog_contracts.resolve_workflow_instance_ports_v1 is shared_catalog_contracts.resolve_workflow_instance_ports_v1


def test_registry_declares_only_real_fixed_handles_and_closed_port_derivation() -> None:
    definitions = {definition.type: definition for definition in FIRST_BATCH_NODE_REGISTRY_V1}

    assert definitions["start"].port_derivation.model_dump() == {
        "version": 1,
        "input_source": "none",
        "output_source": "workflow_inputs",
    }
    assert definitions["condition"].port_derivation.output_source == "condition_branches"
    assert definitions["llm"].port_derivation.output_source == "llm_result_v1"
    assert definitions["transform"].port_derivation.output_source == "transform_result_v1"
    assert definitions["variable_aggregate"].port_derivation.output_source == "aggregate_groups"
    assert definitions["loop"].port_derivation.output_source == "loop_variables"
    assert definitions["http_request"].port_derivation.output_source == "http_response_body"
    assert definitions["python_code"].port_derivation.output_source == "python_result_v1"

    fixed_ids = {node_type: {port.id for port in definition.output_ports} for node_type, definition in definitions.items()}
    assert not ({"branch", "else"} & fixed_ids["condition"])
    assert "group" not in fixed_ids["variable_aggregate"]
    assert "variable" not in fixed_ids["loop"]
    assert "body" not in fixed_ids["http_request"]

    invalid = _definition("start")
    invalid["port_derivation"] = {
        "version": 1,
        "input_source": "none",
        "output_source": "$.workflow_inputs[*]",
    }
    with pytest.raises(ValidationError):
        NodeTypeDefinition.model_validate(invalid)


def test_instance_port_resolver_matches_the_shared_golden() -> None:
    golden = loads(_SHARED_INSTANCE_PORTS_GOLDEN.read_text(encoding="utf-8"))
    resolved = resolve_workflow_instance_ports_v1(golden["workflow_spec"])

    actual = [
        {
            "node_id": node.node_id,
            "input_ports": [_port_projection(port) for port in node.input_ports],
            "output_ports": [_port_projection(port) for port in node.output_ports],
        }
        for node in resolved.nodes
    ]
    assert actual == golden["expected"]


def test_resolved_ports_public_projection_round_trips_through_the_shared_fixture() -> None:
    shared = loads(_SHARED_PUBLIC_PROJECTIONS.read_text(encoding="utf-8"))
    projection = resolved_workflow_instance_ports_public_projection_v1(resolve_workflow_instance_ports_v1(shared["workflow_spec"]))

    assert projection == shared["resolved_ports"]
    assert projection["nodes"][0]["output_ports"][0]["value_type"] is None
    assert "schema_ref" not in projection["nodes"][0]["output_ports"][1]["value_type"]


def test_resolved_port_authority_is_deeply_immutable_but_projects_json_arrays() -> None:
    shared = loads(_SHARED_PUBLIC_PROJECTIONS.read_text(encoding="utf-8"))
    resolved = resolve_workflow_instance_ports_v1(shared["workflow_spec"])
    projection = resolved_workflow_instance_ports_public_projection_v1(resolved)

    assert type(resolved.nodes) is tuple
    assert type(resolved.nodes[0].output_ports) is tuple
    with pytest.raises(TypeError):
        list.clear(resolved.nodes)
    with pytest.raises(TypeError):
        list.clear(resolved.nodes[0].output_ports)
    data_port = next(port for node in resolved.nodes for port in (*node.input_ports, *node.output_ports) if port.value_type is not None)
    with pytest.raises(ValidationError):
        data_port.value_type.kind = "json"  # type: ignore[misc]

    assert type(projection["nodes"]) is list
    assert type(projection["nodes"][0]["output_ports"]) is list


def test_checked_in_registry_passes_the_reproducible_generator_check() -> None:
    completed = subprocess.run(
        [sys.executable, str(_REGISTRY_GENERATOR), "--check"],
        cwd=_REPOSITORY_ROOT / "backend",
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_checked_in_registry_is_repository_prettier_stable() -> None:
    completed = subprocess.run(
        [str(_FRONTEND_PRETTIER), "--check", str(_SHARED_REGISTRY_GOLDEN)],
        cwd=_REPOSITORY_ROOT / "frontend",
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_instance_port_resolver_rejects_collisions_duplicates_and_pseudo_transitions() -> None:
    golden = loads(_SHARED_INSTANCE_PORTS_GOLDEN.read_text(encoding="utf-8"))

    for transition in golden["invalid_transitions"]:
        invalid = deepcopy(golden["workflow_spec"])
        invalid["transitions"] = [transition]
        with pytest.raises(ValueError, match="resolved control port"):
            resolve_workflow_instance_ports_v1(invalid)

    condition_collision = deepcopy(golden["workflow_spec"])
    condition = next(node for node in condition_collision["nodes"] if node["type"] == "condition")
    condition["config"]["branches"][0]["output_port_id"] = "error"
    with pytest.raises(ValidationError):
        resolve_workflow_instance_ports_v1(condition_collision)

    duplicate_aggregate = deepcopy(golden["workflow_spec"])
    aggregate = next(node for node in duplicate_aggregate["nodes"] if node["type"] == "variable_aggregate")
    aggregate["config"]["groups"][1]["id"] = aggregate["config"]["groups"][0]["id"]
    with pytest.raises(ValidationError):
        resolve_workflow_instance_ports_v1(duplicate_aggregate)


def _replace_dynamic_ports(payload: dict[str, object], node_type: str, count: int) -> str:
    if node_type == "start":
        source = deepcopy(payload["workflow_inputs"][0])  # type: ignore[index]
        payload["workflow_inputs"] = [{**source, "id": f"input_{index}", "name": f"Input {index}"} for index in range(count)]
        return str(payload["entry_node_id"])

    node = next(candidate for candidate in payload["nodes"] if candidate["type"] == node_type)  # type: ignore[union-attr]
    if node_type == "condition":
        source = deepcopy(node["config"]["branches"][0])
        node["config"]["branches"] = [{**source, "id": f"branch-{index}", "output_port_id": f"branch_{index}"} for index in range(count)]
    elif node_type == "variable_aggregate":
        source = deepcopy(node["config"]["groups"][0])
        node["config"]["groups"] = [{**source, "id": f"group_{index}", "name": f"Group {index}"} for index in range(count)]
    elif node_type == "loop":
        source = deepcopy(node["config"]["variables"][0])
        node["config"]["variables"] = [
            {
                **source,
                "id": f"variable-{index}",
                "name": f"Variable {index}",
                "initial_input_id": f"initial-{index}",
                "next_input_id": f"next-{index}",
                "output_port_id": f"variable_{index}",
            }
            for index in range(count)
        ]
    else:
        raise AssertionError(f"unsupported dynamic-port node: {node_type}")
    return str(node["id"])


@pytest.mark.parametrize(
    ("node_type", "maximum"),
    [
        ("start", 255),
        ("condition", 254),
        ("variable_aggregate", 254),
        ("loop", 252),
    ],
)
def test_strict_spec_caps_dynamic_ports_before_the_resolver(node_type: str, maximum: int) -> None:
    golden = loads(_SHARED_INSTANCE_PORTS_GOLDEN.read_text(encoding="utf-8"))
    at_limit = deepcopy(golden["workflow_spec"])
    node_id = _replace_dynamic_ports(at_limit, node_type, maximum)

    WorkflowSpecV1.model_validate(at_limit)
    resolved = resolve_workflow_instance_ports_v1(at_limit)
    resolved_node = next(node for node in resolved.nodes if node.node_id == node_id)
    assert len(resolved_node.output_ports) == 256

    over_limit = deepcopy(golden["workflow_spec"])
    _replace_dynamic_ports(over_limit, node_type, maximum + 1)
    with pytest.raises(ValidationError):
        WorkflowSpecV1.model_validate(over_limit)
    with pytest.raises(ValidationError):
        resolve_workflow_instance_ports_v1(over_limit)


def _set_dynamic_title(payload: dict[str, object], case: str, value: object) -> None:
    if case.startswith("workflow_input_"):
        payload["workflow_inputs"][0][case.removeprefix("workflow_input_")] = value  # type: ignore[index]
        return
    node_type, field = case.split(":", maxsplit=1)
    node = next(candidate for candidate in payload["nodes"] if candidate["type"] == node_type)  # type: ignore[union-attr]
    collection = {
        "condition": "branches",
        "variable_aggregate": "groups",
        "loop": "variables",
    }[node_type]
    node["config"][collection][0][field] = value


@pytest.mark.parametrize(
    "case",
    [
        "workflow_input_name",
        "workflow_input_label",
        "condition:id",
        "condition:label",
        "variable_aggregate:name",
        "loop:name",
    ],
)
def test_dynamic_port_titles_use_shared_unicode_code_point_bounds(case: str) -> None:
    golden = loads(_SHARED_INSTANCE_PORTS_GOLDEN.read_text(encoding="utf-8"))
    boundary = loads(_SHARED_UNICODE_BOUNDARIES.read_text(encoding="utf-8"))
    character = boundary["astral_character"]
    maximum = boundary["port_title"]["maximum"]

    valid = deepcopy(golden["workflow_spec"])
    _set_dynamic_title(valid, case, character * maximum)
    WorkflowSpecV1.model_validate(valid)
    resolve_workflow_instance_ports_v1(valid)

    invalid = deepcopy(golden["workflow_spec"])
    _set_dynamic_title(invalid, case, character * (maximum + 1))
    with pytest.raises(ValidationError):
        WorkflowSpecV1.model_validate(invalid)
    with pytest.raises(ValidationError):
        resolve_workflow_instance_ports_v1(invalid)


@pytest.mark.parametrize("case", ["workflow_input_label", "condition:label"])
def test_dynamic_port_labels_reject_empty_strings_before_resolution(case: str) -> None:
    golden = loads(_SHARED_INSTANCE_PORTS_GOLDEN.read_text(encoding="utf-8"))
    invalid = deepcopy(golden["workflow_spec"])
    _set_dynamic_title(invalid, case, "")

    with pytest.raises(ValidationError):
        WorkflowSpecV1.model_validate(invalid)
    with pytest.raises(ValidationError):
        resolve_workflow_instance_ports_v1(invalid)


def _apply_structural_dynamic_port_mutation(payload: dict[str, object], case: str) -> None:
    if case == "start_fixed":
        payload["workflow_inputs"][0]["id"] = "next"  # type: ignore[index]
        return
    if case == "start_duplicate":
        payload["workflow_inputs"][1]["id"] = payload["workflow_inputs"][0]["id"]  # type: ignore[index]
        return

    node_type, mutation = case.split(":", maxsplit=1)
    node = next(candidate for candidate in payload["nodes"] if candidate["type"] == node_type)  # type: ignore[union-attr]
    config = node["config"]
    if node_type == "condition":
        if mutation == "branch_fixed":
            config["branches"][0]["output_port_id"] = "error"
        elif mutation == "else_fixed":
            config["else_output_port_id"] = "error"
        elif mutation == "duplicate":
            config["branches"][1]["output_port_id"] = config["branches"][0]["output_port_id"]
        elif mutation == "else_duplicate":
            config["else_output_port_id"] = config["branches"][0]["output_port_id"]
        return
    if node_type == "variable_aggregate":
        if mutation == "fixed":
            config["groups"][0]["id"] = "next"
        else:
            config["groups"][1]["id"] = config["groups"][0]["id"]
        return
    if node_type == "loop":
        if mutation == "fixed":
            config["variables"][0]["output_port_id"] = "body"
        else:
            duplicate = deepcopy(config["variables"][0])
            duplicate["id"] = "duplicate-variable"
            config["variables"].append(duplicate)
        return
    raise AssertionError(f"unsupported structural mutation: {case}")


@pytest.mark.parametrize(
    "case",
    [
        "start_fixed",
        "start_duplicate",
        "condition:branch_fixed",
        "condition:else_fixed",
        "condition:duplicate",
        "condition:else_duplicate",
        "variable_aggregate:fixed",
        "variable_aggregate:duplicate",
        "loop:fixed",
        "loop:duplicate",
    ],
)
def test_strict_spec_rejects_structural_dynamic_port_failures(case: str) -> None:
    golden = loads(_SHARED_INSTANCE_PORTS_GOLDEN.read_text(encoding="utf-8"))
    invalid = deepcopy(golden["workflow_spec"])
    _apply_structural_dynamic_port_mutation(invalid, case)

    with pytest.raises(ValidationError):
        WorkflowSpecV1.model_validate(invalid)
    with pytest.raises(ValidationError):
        resolve_workflow_instance_ports_v1(invalid)


def test_shared_per_node_config_corpus_matches_pydantic_and_manifest_schema() -> None:
    corpus = loads(_SHARED_NODE_CONFIG_CORPUS.read_text(encoding="utf-8"))
    source = loads(_SHARED_WORKFLOW_SPEC.read_text(encoding="utf-8"))
    source_configs = {node["type"]: node["config"] for node in source["nodes"]}
    definitions = {definition.type: definition for definition in FIRST_BATCH_NODE_REGISTRY_V1}

    for case in corpus["cases"]:
        node_type = case["node_type"]
        config = deepcopy(source_configs[node_type])
        _mutate_node_config(config, case["mutation"])

        try:
            validate_node_config_v1(node_type, config)
        except ValidationError:
            pydantic_valid = False
        else:
            pydantic_valid = True
        manifest_valid = _manifest_schema_accepts(
            node_type=node_type,
            schema=definitions[node_type].model_dump(
                mode="json",
                by_alias=True,
                exclude_unset=True,
            )["config_schema"],
            config=config,
        )
        assert pydantic_valid is case["valid"], case["name"]
        assert manifest_valid is case["valid"], case["name"]


def test_catalog_entry_rejects_any_config_schema_or_port_drift_from_the_registry() -> None:
    config_drift = _catalog_entry("python_code")
    config_drift["definition"]["config_schema"]["properties"]["source"]["description"] = "drift"  # type: ignore[index]
    with pytest.raises(ValidationError):
        NodeCatalogEntry.model_validate(config_drift)

    port_drift = _catalog_entry("http_request")
    port_drift["definition"]["output_ports"][0]["cardinality"] = "many"  # type: ignore[index]
    with pytest.raises(ValidationError):
        NodeCatalogEntry.model_validate(port_drift)


def test_port_definition_uses_the_shared_workflow_value_type_contract() -> None:
    port = PortDefinition.model_validate(
        {
            "id": "prompt",
            "title_i18n": {"zh-CN": "提示词", "en-US": "Prompt"},
            "kind": "data",
            "value_type": _value_type(),
            "cardinality": "one",
            "required": True,
        }
    )

    assert port.value_type is not None
    assert port.value_type.kind == "string"

    with pytest.raises(ValidationError):
        PortDefinition.model_validate(
            {
                "id": "next",
                "title_i18n": {"zh-CN": "下一步", "en-US": "Next"},
                "kind": "control",
                "value_type": _value_type(),
                "cardinality": "many",
                "required": False,
            }
        )


def test_node_definition_freezes_first_batch_identity_titles_renderer_and_semantics() -> None:
    for node_type in WORKFLOW_NODE_KINDS:
        definition = NodeTypeDefinition.model_validate(_definition(node_type))
        assert definition.type == node_type
        assert definition.version == 1
        assert definition.renderer_key == node_type

    for change in (
        {"type": "agent"},
        {"version": 2},
        {"renderer_key": "package.module:Renderer"},
        {"title_i18n": {"zh-CN": "开始", "en-US": "Renamed"}},
        {"retry_semantics": "unsafe_write"},
        {"supports_streaming": True},
    ):
        payload = _definition("start")
        payload.update(change)
        with pytest.raises(ValidationError):
            NodeTypeDefinition.model_validate(payload)


def test_node_definition_rejects_open_config_schema_duplicate_ports_and_private_fields() -> None:
    payload = _definition("transform")
    payload["config_schema"]["additionalProperties"] = True  # type: ignore[index]
    with pytest.raises(ValidationError):
        NodeTypeDefinition.model_validate(payload)

    payload = _definition("start")
    payload["config_schema"][1] = {"type": "string"}  # type: ignore[index]
    with pytest.raises((TypeError, ValidationError)):
        NodeTypeDefinition.model_validate(payload)

    payload = _definition("transform")
    port = {
        "id": "input",
        "title_i18n": {"zh-CN": "输入", "en-US": "Input"},
        "kind": "data",
        "value_type": _value_type(),
        "cardinality": "one",
        "required": True,
    }
    payload["input_ports"] = [port, deepcopy(port)]
    with pytest.raises(ValidationError):
        NodeTypeDefinition.model_validate(payload)

    payload = _definition("start")
    payload["dynamic_import"] = "package.module:Renderer"
    with pytest.raises(ValidationError):
        NodeTypeDefinition.model_validate(payload)


def test_catalog_entry_exposes_only_safe_availability_and_applicable_public_limits() -> None:
    payload = _catalog_entry("python_code")
    payload["availability"] = {
        "state": "disabled",
        "reason_code": "WORKFLOW_CODE_PROFILE_UNAVAILABLE",
    }
    payload["public_limits"] = {
        "max_source_bytes": 65_536,
        "max_timeout_ms": 30_000,
        "max_iterations": None,
        "max_aggregate_groups": None,
        "max_aggregate_candidates": None,
        "max_http_request_bytes": None,
        "max_http_response_bytes": None,
    }

    entry = NodeCatalogEntry.model_validate(payload)

    assert entry.availability.reason_code == "WORKFLOW_CODE_PROFILE_UNAVAILABLE"

    for reason in ("aio-worker-7", "https://sandbox.internal", "provider:secret"):
        invalid = deepcopy(payload)
        invalid["availability"]["reason_code"] = reason  # type: ignore[index]
        with pytest.raises(ValidationError):
            NodeCatalogEntry.model_validate(invalid)

    missing_reason = deepcopy(payload)
    missing_reason["availability"] = {"state": "disabled"}
    with pytest.raises(ValidationError):
        NodeCatalogEntry.model_validate(missing_reason)

    invalid = deepcopy(payload)
    invalid["public_limits"]["max_http_response_bytes"] = 1_024  # type: ignore[index]
    with pytest.raises(ValidationError):
        NodeCatalogEntry.model_validate(invalid)

    minimal = _catalog_entry("start")
    minimal.pop("public_limits")
    minimal["availability"] = {"state": "enabled"}
    parsed_minimal = NodeCatalogEntry.model_validate(minimal)
    assert parsed_minimal.public_limits is None
    assert parsed_minimal.availability.reason_code is None


def test_http_catalog_entry_exposes_only_closed_safe_authoring_options() -> None:
    payload = _catalog_entry("http_request")
    payload["http_authoring"] = {
        "endpoints": [
            {
                "id": "public-api",
                "origin": "https://api.example.com",
                "allowed_methods": ["GET", "POST"],
                "write_idempotency": "server_derived_key",
                "injection_profiles": [
                    {
                        "id": "api-key-v1",
                        "scheme": "api_key",
                        "target_header": "x-api-key",
                        "credential_payload_contract": "api_key_v1",
                    }
                ],
            }
        ]
    }

    parsed = NodeCatalogEntry.model_validate(payload)

    assert parsed.http_authoring is not None
    assert parsed.http_authoring.endpoints[0].origin == "https://api.example.com"
    assert parsed.http_authoring.endpoints[0].allowed_methods == ("GET", "POST")
    assert parsed.http_authoring.endpoints[0].injection_profiles[0].target_header == "x-api-key"
    projection = parsed.model_dump(mode="json", by_alias=True, exclude_unset=True)
    assert type(projection["http_authoring"]["endpoints"]) is list
    assert type(projection["http_authoring"]["endpoints"][0]["allowed_methods"]) is list
    assert type(projection["http_authoring"]["endpoints"][0]["injection_profiles"]) is list

    for forbidden_field in (
        "policy_version_id",
        "policy_checksum",
        "egress_profile_id",
        "egress_profile_digest",
        "provider_id",
        "worker_id",
        "profile_digest",
        "credential_id",
        "credential_version_id",
        "grant_id",
        "secret",
    ):
        for level in ("authoring", "endpoint", "profile"):
            invalid = deepcopy(payload)
            if level == "authoring":
                invalid["http_authoring"][forbidden_field] = "must-not-leak"  # type: ignore[index]
            elif level == "endpoint":
                invalid["http_authoring"]["endpoints"][0][forbidden_field] = "must-not-leak"  # type: ignore[index]
            else:
                invalid["http_authoring"]["endpoints"][0]["injection_profiles"][0][forbidden_field] = "must-not-leak"  # type: ignore[index]
            with pytest.raises(ValidationError):
                NodeCatalogEntry.model_validate(invalid)

    non_http = _catalog_entry("start")
    non_http["http_authoring"] = deepcopy(payload["http_authoring"])
    with pytest.raises(ValidationError):
        NodeCatalogEntry.model_validate(non_http)

    default_https_port = deepcopy(payload)
    default_https_port["http_authoring"]["endpoints"][0]["origin"] = "https://api.example.com:443"  # type: ignore[index]
    assert NodeCatalogEntry.model_validate(default_https_port).http_authoring is not None

    duplicate_coordinate = deepcopy(payload)
    duplicate_coordinate["http_authoring"]["endpoints"].append(  # type: ignore[index]
        {
            "id": "public-api-shadow",
            "origin": "https://api.example.com:443",
            "allowed_methods": ["GET"],
            "write_idempotency": "none",
            "injection_profiles": [],
        }
    )
    with pytest.raises(ValidationError):
        NodeCatalogEntry.model_validate(duplicate_coordinate)


def test_public_limit_fields_match_runtime_policy_and_transport_hard_bounds() -> None:
    loop = _catalog_entry("loop")
    loop["public_limits"] = {
        "max_timeout_ms": 31_536_000_000,
        "max_iterations": 1_000_000,
    }
    assert NodeCatalogEntry.model_validate(loop).public_limits is not None

    aggregate = _catalog_entry("variable_aggregate")
    aggregate["public_limits"] = {
        "max_aggregate_groups": 254,
        "max_aggregate_candidates": 100_000,
    }
    assert NodeCatalogEntry.model_validate(aggregate).public_limits is not None

    http = _catalog_entry("http_request")
    http["public_limits"] = {
        "max_http_request_bytes": 2_147_483_648,
        "max_http_response_bytes": 2_097_152,
    }
    assert NodeCatalogEntry.model_validate(http).public_limits is not None

    for node_type, field, value in (
        ("loop", "max_timeout_ms", 31_536_000_001),
        ("loop", "max_iterations", 1_000_001),
        ("variable_aggregate", "max_aggregate_groups", 255),
        ("variable_aggregate", "max_aggregate_candidates", 100_001),
        ("python_code", "max_source_bytes", 2_147_483_649),
        ("http_request", "max_http_request_bytes", 2_147_483_649),
        ("http_request", "max_http_response_bytes", 2_097_153),
    ):
        payload = _catalog_entry(node_type)
        payload["public_limits"] = {field: value}
        with pytest.raises(ValidationError):
            NodeCatalogEntry.model_validate(payload)


def test_catalog_generations_are_server_derived_opaque_digests() -> None:
    policy_version_id = uuid.UUID("53f5a2b9-1c63-43ec-92d4-2aa799f18857")
    policy_checksum = "a" * 64
    catalog_generation = derive_catalog_generation(
        registry_contract_version=1,
        policy_version_id=policy_version_id,
        policy_revision=7,
        policy_checksum=policy_checksum,
    )
    same = derive_catalog_generation(
        registry_contract_version=1,
        policy_version_id=policy_version_id,
        policy_revision=7,
        policy_checksum=policy_checksum,
    )
    changed = derive_catalog_generation(
        registry_contract_version=1,
        policy_version_id=policy_version_id,
        policy_revision=8,
        policy_checksum=policy_checksum,
    )
    availability_generation = derive_availability_generation(
        catalog_generation=catalog_generation,
        readiness_generation="b" * 64,
    )

    assert catalog_generation == same
    assert catalog_generation != changed
    assert len(catalog_generation) == 64
    assert len(availability_generation) == 64
    assert str(policy_version_id) not in catalog_generation

    manifest_checksum = first_batch_node_registry_manifest_checksum_v1()
    expected_payload = json.dumps(
        [
            "actweave.workflow.node-catalog.v1",
            1,
            manifest_checksum,
            str(policy_version_id),
            7,
            policy_checksum,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    assert catalog_generation == hashlib.sha256(expected_payload).hexdigest()


def test_catalog_response_is_canonical_strict_and_contains_no_private_generation_inputs() -> None:
    entries = [_catalog_entry(node_type) for node_type in WORKFLOW_NODE_KINDS]
    response = NodeCatalogResponseV1.model_validate(
        {
            "schema_version": 1,
            "catalog_generation": "a" * 64,
            "availability_generation": "b" * 64,
            "entries": entries,
        }
    )

    assert len(response.entries) == 9

    for field in ("policy_version_id", "worker_id", "provider_id", "sandbox_id", "profile_locator"):
        payload = response.model_dump(mode="json", by_alias=True)
        payload[field] = "must-not-leak"
        with pytest.raises(ValidationError):
            NodeCatalogResponseV1.model_validate(payload)

    payload = response.model_dump(mode="json", by_alias=True)
    payload["entries"] = list(reversed(payload["entries"]))
    with pytest.raises(ValidationError):
        NodeCatalogResponseV1.model_validate(payload)
