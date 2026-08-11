from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator
from support.workflow_compiler_g12 import (
    AGGREGATE_ID,
    BODY_ID,
    CONDITION_ID,
    END_ID,
    EXTRA_ID,
    LEFT_ID,
    LOOP_ID,
    RIGHT_ID,
    START_ID,
    condition_aggregate_spec_payload,
    literal,
    loop_condition_aggregate_spec_payload,
    loop_spec_payload,
    node,
    node_output,
    template,
    transform_config,
    transition,
    value_type,
    workflow_input,
)

import deerflow.workflows.compiler.core as compiler_core
import deerflow.workflows.compiler.topology as compiler_topology
from deerflow.workflows import RestrictedTemplate, WorkflowSpecV1, semantic_checksum
from deerflow.workflows.compiler import (
    COMPILER_CONTRACT_VERSION_V1,
    GRAPH_SCHEMA_VERSION_V1,
    CompilerCacheKey,
    WorkflowCompilerCache,
    WorkflowCompilerUnavailableError,
    WorkflowOutputSettlementError,
    compile_workflow,
    settle_workflow_outputs,
    workflow_ir_public_projection_v1,
)
from deerflow.workflows.compiler.ir import WORKFLOW_VALUE_MISSING, thaw_json
from deerflow.workflows.json_schema import INLINE_SCHEMA_REF_PREFIX
from deerflow.workflows.registry import FIRST_BATCH_RUNTIME_REGISTRY, WorkflowNodeRegistry
from deerflow.workflows.semantics import (
    LoopCommitDecision,
    LoopCommitProtocolError,
    commit_loop_iteration,
    evaluate_predicate,
    render_restricted_template,
    route_condition,
)
from deerflow.workflows.validation import WorkflowCompilationLimits, validate_workflow

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _edge(
    transition_id: str,
    source_node_id: str,
    source_port_id: str,
    target_node_id: str,
    target_port_id: str = "in",
) -> dict[str, object]:
    return {
        "transition_id": transition_id,
        "source": {"node_id": source_node_id, "port_id": source_port_id},
        "target": {"node_id": target_node_id, "port_id": target_port_id},
    }


def test_condition_transform_and_aggregate_lowering_matches_the_frozen_golden() -> None:
    spec = WorkflowSpecV1.model_validate(condition_aggregate_spec_payload())
    ir = compile_workflow(spec)
    projection = workflow_ir_public_projection_v1(ir)

    assert ir.graph_schema_version == GRAPH_SCHEMA_VERSION_V1 == 1
    assert ir.compiler_contract_version == COMPILER_CONTRACT_VERSION_V1 == 1
    assert ir.semantic_checksum == semantic_checksum(spec)
    assert ir.cache_key == CompilerCacheKey(1, 1, ir.semantic_checksum)
    assert projection["branches"] == [
        {
            "node_id": CONDITION_ID,
            "routes": [
                {
                    "output_port_id": "truthy",
                    "target_node_id": LEFT_ID,
                    "predicate": {
                        "op": "and",
                        "items": [
                            {
                                "left": {"kind": "workflow_input", "input_id": "flag"},
                                "operator": "eq",
                                "right": {"kind": "literal", "value": True},
                            }
                        ],
                    },
                }
            ],
            "else_output_port_id": "fallback",
            "else_target_node_id": RIGHT_ID,
            "error_route": None,
        }
    ]
    assert projection["transforms"] == [
        {
            "node_id": LEFT_ID,
            "mode": "text",
            "missing_variable": "error",
            "template": {
                "version": 1,
                "segments": [{"kind": "text", "value": "left"}],
            },
        },
        {
            "node_id": RIGHT_ID,
            "mode": "text",
            "missing_variable": "error",
            "template": {
                "version": 1,
                "segments": [{"kind": "text", "value": "right"}],
            },
        },
    ]
    assert projection["aggregates"] == [
        {
            "node_id": AGGREGATE_ID,
            "condition_node_id": CONDITION_ID,
            "groups": [
                {
                    "output_id": "result",
                    "candidate_input_ids": ["left", "right"],
                    "candidate_branch_port_ids": ["truthy", "fallback"],
                }
            ],
        }
    ]
    assert not any(edge["source"]["node_id"] == CONDITION_ID for edge in projection["static_edges"])


def test_condition_error_policy_lowers_error_as_an_exclusive_typed_branch_outcome() -> None:
    payload = condition_aggregate_spec_payload()
    condition = next(item for item in payload["nodes"] if item["id"] == CONDITION_ID)
    condition["execution_policy"]["on_error"] = {
        "mode": "route_error",
        "output_port_id": "error",
    }
    payload["transitions"].append(transition("t-condition-error", CONDITION_ID, "error", END_ID))
    payload["workflow_outputs"][0]["default"] = "fallback"

    projection = workflow_ir_public_projection_v1(compile_workflow(WorkflowSpecV1.model_validate(payload), cache=None))

    error_edge = projection["branches"][0]["error_route"]
    assert error_edge == _edge("t-condition-error", CONDITION_ID, "error", END_ID)
    assert not any(edge["transition_id"] == "t-condition-error" for edge in projection["static_edges"])
    assert not any(edge["transition_id"] == "t-condition-error" for edge in projection["outcome_routes"])
    assert [route["output_port_id"] for route in projection["branches"][0]["routes"]] == ["truthy"]


def test_loop_lowering_has_stable_internal_ids_and_the_only_generated_back_edge() -> None:
    spec = WorkflowSpecV1.model_validate(loop_spec_payload(max_iterations=3))
    ir = compile_workflow(spec)
    projection = workflow_ir_public_projection_v1(ir)
    [region] = projection["loop_regions"]

    prefix = f"@loop/{LOOP_ID}"
    assert region["body_edges"] == []
    assert region["generated_edges"] == [
        _edge(f"{prefix}/init-entry", f"{prefix}/init", "next", BODY_ID),
        _edge(f"{prefix}/exit-commit/next", BODY_ID, "next", f"{prefix}/commit"),
        _edge(f"{prefix}/commit-route", f"{prefix}/commit", "next", f"{prefix}/route"),
    ]
    assert region["generated_back_edge"] == _edge(f"{prefix}/continue", f"{prefix}/route", "continue", BODY_ID)
    assert region["condition_met_edge"] == _edge(f"{prefix}/done", f"{prefix}/route", "done", f"{prefix}/done")
    assert region["limit_exceeded_edge"] == _edge(f"{prefix}/limit", f"{prefix}/route", "limit", f"{prefix}/limit")
    assert region["limit_error_code"] == "WORKFLOW_LOOP_LIMIT_EXCEEDED"
    assert region["worst_case_supersteps"] == 11
    assert region["worst_case_activations"] == 11
    assert projection["static_edges"] == [_edge("t-start-loop", START_ID, "next", f"{prefix}/init")]
    assert projection["outcome_routes"] == [{"outcome": "success", **_edge("t-loop-end", f"{prefix}/done", "next", END_ID)}]
    assert ir.worst_case_iterations == 3
    assert ir.worst_case_activations == 13
    assert ir.worst_case_steps == 13
    assert ir.worst_case_recursion_depth > ir.worst_case_depth
    assert ir.worst_case_parallelism == 1
    assert ir.worst_case_fan_out == 1


def test_loop_success_and_limit_error_routes_lower_from_distinct_internal_sources() -> None:
    payload = loop_spec_payload(max_iterations=3)
    loop = next(item for item in payload["nodes"] if item["id"] == LOOP_ID)
    loop["execution_policy"]["on_error"] = {"mode": "route_error", "output_port_id": "error"}
    payload["transitions"].append(transition("t-loop-error", LOOP_ID, "error", END_ID))
    payload["workflow_outputs"][0]["default"] = "fallback"

    projection = workflow_ir_public_projection_v1(compile_workflow(WorkflowSpecV1.model_validate(payload), cache=None))
    [region] = projection["loop_regions"]
    edges = {edge["transition_id"]: edge for edge in projection["outcome_routes"]}

    assert region["condition_met_edge"]["source"]["port_id"] == "done"
    assert region["limit_exceeded_edge"]["source"]["port_id"] == "limit"
    assert region["limit_error_code"] == "WORKFLOW_LOOP_LIMIT_EXCEEDED"
    assert edges["t-loop-end"]["source"] == {
        "node_id": f"@loop/{LOOP_ID}/done",
        "port_id": "next",
    }
    assert edges["t-loop-error"]["source"] == {
        "node_id": f"@loop/{LOOP_ID}/limit",
        "port_id": "error",
    }


def test_condition_and_aggregate_lowering_is_scope_complete_inside_a_loop_body() -> None:
    projection = workflow_ir_public_projection_v1(
        compile_workflow(
            WorkflowSpecV1.model_validate(loop_condition_aggregate_spec_payload()),
            cache=None,
        )
    )

    assert projection["aggregates"] == [
        {
            "node_id": AGGREGATE_ID,
            "condition_node_id": CONDITION_ID,
            "groups": [
                {
                    "output_id": "result",
                    "candidate_input_ids": ["done", "pending"],
                    "candidate_branch_port_ids": ["done", "pending"],
                }
            ],
        }
    ]
    [region] = projection["loop_regions"]
    assert region["body_entry_node_id"] == CONDITION_ID
    assert region["body_exit_node_id"] == AGGREGATE_ID


def test_single_condition_loop_body_exit_synthesizes_every_normal_branch_to_commit() -> None:
    payload = loop_spec_payload()
    loop_node = next(item for item in payload["nodes"] if item["id"] == LOOP_ID)
    body = next(item for item in payload["nodes"] if item["id"] == BODY_ID)
    body["type"] = "condition"
    body["config"] = {
        "branches": [
            {
                "id": "finish",
                "output_port_id": "finish",
                "label": "Finish",
                "predicate": {
                    "op": "and",
                    "items": [
                        {
                            "left": {"kind": "literal", "value": True},
                            "operator": "eq",
                            "right": {"kind": "literal", "value": True},
                        }
                    ],
                },
            }
        ],
        "else_output_port_id": "continue_once",
    }
    body["input_bindings"] = {}
    loop_node["input_bindings"]["next"] = literal("next")
    spec = WorkflowSpecV1.model_validate(payload)

    validation = validate_workflow(spec, limits=WorkflowCompilationLimits.permissive())
    assert validation.is_valid, validation.issues
    projection = workflow_ir_public_projection_v1(compile_workflow(spec, cache=None))
    [branch] = projection["branches"]
    [region] = projection["loop_regions"]
    commit_id = f"@loop/{LOOP_ID}/commit"

    assert branch["routes"][0]["target_node_id"] == commit_id
    assert branch["else_target_node_id"] == commit_id
    exit_edges = [edge for edge in region["generated_edges"] if edge["target"]["node_id"] == commit_id]
    assert [edge["source"]["port_id"] for edge in exit_edges] == [
        "finish",
        "continue_once",
    ]


@pytest.mark.parametrize(
    ("entry_type", "normal_port"),
    [("transform", "next"), ("http_request", "success")],
)
def test_loop_body_edges_preserve_transition_identity_and_distinct_outcome_ports(
    entry_type: str,
    normal_port: str,
) -> None:
    payload = loop_spec_payload()
    loop_node = next(item for item in payload["nodes"] if item["id"] == LOOP_ID)
    entry = next(item for item in payload["nodes"] if item["id"] == BODY_ID)
    body_scope = {"kind": "loop_body", "loop_node_id": LOOP_ID}
    if entry_type == "http_request":
        entry["type"] = "http_request"
        entry["config"] = {
            "method": "GET",
            "base_origin": "https://example.com",
            "path_template": template("/resource"),
            "query": [],
            "headers": [],
            "auth": {"mode": "none"},
            "body": {"kind": "none"},
            "timeout": {"connect_ms": None, "read_ms": None, "write_ms": None},
            "response": {
                "mode": "text",
                "accepted_statuses": [{"from": 200, "to": 299}],
                "schema": None,
            },
        }
        entry["input_bindings"] = {}
    entry["execution_policy"]["on_error"] = {
        "mode": "route_error",
        "output_port_id": "error",
    }
    payload["nodes"].append(
        node(
            EXTRA_ID,
            "transform",
            transform_config("settled"),
            scope=body_scope,
        )
    )
    loop_node["config"]["body_exit_node_id"] = EXTRA_ID
    loop_node["input_bindings"]["next"] = node_output(EXTRA_ID, "result")
    payload["transitions"].extend(
        [
            transition("t-body-normal", BODY_ID, normal_port, EXTRA_ID),
            transition("t-body-error", BODY_ID, "error", EXTRA_ID),
        ]
    )
    spec = WorkflowSpecV1.model_validate(payload)

    validation = validate_workflow(spec, limits=WorkflowCompilationLimits.permissive())
    assert validation.is_valid, validation.issues
    [region] = workflow_ir_public_projection_v1(compile_workflow(spec, cache=None))["loop_regions"]

    assert region["body_edges"] == [
        _edge("t-body-error", BODY_ID, "error", EXTRA_ID),
        _edge("t-body-normal", BODY_ID, normal_port, EXTRA_ID),
    ]


def test_ir_and_every_nested_semantic_value_are_deeply_immutable() -> None:
    ir = compile_workflow(WorkflowSpecV1.model_validate(condition_aggregate_spec_payload()))

    with pytest.raises((AttributeError, TypeError)):
        ir.nodes = ()  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        ir.nodes[0].config.items = ()  # type: ignore[misc]
    assert isinstance(ir.nodes, tuple)
    assert isinstance(ir.static_edges, tuple)
    assert isinstance(ir.branches[0].routes, tuple)


def test_versioned_compiler_cache_contains_no_runtime_authority_and_is_keyed_only_by_the_triple() -> None:
    spec = WorkflowSpecV1.model_validate(loop_spec_payload())
    cache: WorkflowCompilerCache[object] = WorkflowCompilerCache()

    first = compile_workflow(spec, cache=cache)
    second = compile_workflow(spec, cache=cache)

    assert first is second
    assert cache.hits == 1
    assert not hasattr(first, "owner_id")
    assert not hasattr(first, "credential")
    assert not hasattr(first, "database")
    assert not hasattr(first, "checkpointer")


def test_nfc_equivalent_specs_compile_to_identical_ir_independent_of_cache_order() -> None:
    def build(schema_ref: str) -> WorkflowSpecV1:
        payload = condition_aggregate_spec_payload()
        payload["workflow_inputs"].append(
            {
                "id": "document",
                "name": "Document",
                "label": None,
                "description": schema_ref,
                "value_type": {
                    "kind": "json",
                    "collection": False,
                    "nullable": False,
                    "schema_ref": schema_ref,
                },
                "required": False,
                "constraints": {"kind": "none"},
            }
        )
        return WorkflowSpecV1.model_validate(payload)

    decomposed = build("sche\u0301ma")
    composed = build("schéma")

    assert semantic_checksum(decomposed) == semantic_checksum(composed)
    direct = [workflow_ir_public_projection_v1(compile_workflow(spec, cache=None)) for spec in (decomposed, composed)]
    assert direct[0] == direct[1]
    assert json.dumps(direct[0], ensure_ascii=False, sort_keys=True, separators=(",", ":")) == json.dumps(direct[1], ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    for first_spec, second_spec in ((decomposed, composed), (composed, decomposed)):
        cache: WorkflowCompilerCache[object] = WorkflowCompilerCache()
        first = workflow_ir_public_projection_v1(compile_workflow(first_spec, cache=cache))
        second = workflow_ir_public_projection_v1(compile_workflow(second_spec, cache=cache))
        assert first == second == direct[0]
        assert cache.hits == 1


def test_large_output_schema_is_retained_once_in_config_but_only_compact_identity_reaches_ir_ports() -> None:
    schema = {
        "type": "object",
        "properties": {f"field_{index}": {"type": "string"} for index in range(256)},
        "additionalProperties": False,
    }
    payload = {
        "schema_version": 1,
        "entry_node_id": START_ID,
        "nodes": [
            node(START_ID, "start", {}),
            node(
                LEFT_ID,
                "transform",
                {
                    "input_variables": [],
                    "missing_variable": "error",
                    "mode": "json",
                    "template": {"version": 1, "template": {}, "bindings": {}},
                    "output_schema": schema,
                },
            ),
            node(END_ID, "end", {}),
        ],
        "transitions": [
            transition("t-start-transform", START_ID, "next", LEFT_ID),
            transition("t-transform-end", LEFT_ID, "next", END_ID),
        ],
        "workflow_inputs": [],
        "workflow_outputs": [
            {
                "id": "result",
                "name": "result",
                "description": None,
                "value_type": value_type("json"),
                "source": node_output(LEFT_ID, "result"),
            }
        ],
        "credential_slots": [],
    }

    projection = workflow_ir_public_projection_v1(compile_workflow(WorkflowSpecV1.model_validate(payload), cache=None))
    transform_node = next(item for item in projection["nodes"] if item["id"] == LEFT_ID)
    result_port = next(port for port in transform_node["output_ports"] if port["id"] == "result")
    schema_ref = result_port["value_type"]["schema_ref"]
    encoded = json.dumps(projection, ensure_ascii=False, sort_keys=True)

    assert schema_ref.startswith(INLINE_SCHEMA_REF_PREFIX)
    assert len(schema_ref) == len(INLINE_SCHEMA_REF_PREFIX) + 64
    assert encoded.count("field_255") == 1


def test_compiler_rejects_a_different_registry_identity_before_touching_the_triple_cache() -> None:
    spec = WorkflowSpecV1.model_validate(loop_spec_payload())
    clone_registry = WorkflowNodeRegistry(tuple(FIRST_BATCH_RUNTIME_REGISTRY.require(kind, 1) for kind, _version in FIRST_BATCH_RUNTIME_REGISTRY.keys()))
    cache: WorkflowCompilerCache[object] = WorkflowCompilerCache()

    with pytest.raises(WorkflowCompilerUnavailableError, match="WORKFLOW_COMPILER_REGISTRY_UNAVAILABLE"):
        compile_workflow(spec, registry=clone_registry, cache=cache)

    assert cache.hits == cache.misses == 0


def test_ir_input_and_output_schemas_freeze_defaults_nullability_and_all_input_constraints() -> None:
    payload = condition_aggregate_spec_payload()
    flag = payload["workflow_inputs"][0]
    flag["value_type"] = value_type("string", nullable=True)
    flag["default"] = "abc"
    flag["constraints"] = {
        "kind": "string",
        "min_length": 2,
        "max_length": 8,
    }
    condition = next(item for item in payload["nodes"] if item["id"] == CONDITION_ID)
    condition["config"]["branches"][0]["predicate"]["items"][0]["right"] = literal("abc")
    payload["workflow_inputs"].extend(
        [
            {
                "id": "count",
                "name": "count",
                "label": None,
                "description": None,
                "value_type": value_type("number"),
                "required": True,
                "constraints": {"kind": "number", "minimum": 1, "maximum": 10},
            },
            {
                "id": "mode",
                "name": "mode",
                "label": None,
                "description": None,
                "value_type": value_type("string"),
                "required": False,
                "constraints": {"kind": "enum", "options": ["fast", "safe"]},
            },
        ]
    )
    payload["workflow_outputs"][0]["default"] = "fallback"

    projection = workflow_ir_public_projection_v1(compile_workflow(WorkflowSpecV1.model_validate(payload), cache=None))

    assert projection["input_schema"] == {
        "type": "object",
        "properties": {
            "count": {"type": "number", "minimum": 1, "maximum": 10},
            "flag": {
                "anyOf": [
                    {"type": "string", "minLength": 2, "maxLength": 8},
                    {"type": "null"},
                ],
                "default": "abc",
            },
            "mode": {"type": "string", "enum": ["fast", "safe"]},
        },
        "required": ["count"],
        "additionalProperties": False,
    }
    assert projection["output_schema"]["properties"]["result"]["default"] == "fallback"
    assert projection["output_schema"]["required"] == []


def test_ir_freezes_output_sources_and_settles_missing_separately_from_json_null() -> None:
    defaulted = condition_aggregate_spec_payload()
    defaulted["workflow_outputs"][0]["source"] = node_output(LEFT_ID, "result")  # type: ignore[index]
    defaulted["workflow_outputs"][0]["default"] = "fallback"  # type: ignore[index]
    defaulted_ir = compile_workflow(WorkflowSpecV1.model_validate(defaulted), cache=None)
    defaulted_projection = workflow_ir_public_projection_v1(defaulted_ir)

    assert defaulted_projection["workflow_outputs"] == [
        {
            "id": "result",
            "value_type": value_type(),
            "source": node_output(LEFT_ID, "result"),
            "has_default": True,
            "default": "fallback",
        }
    ]
    assert thaw_json(
        settle_workflow_outputs(
            defaulted_ir.workflow_outputs,
            resolve=lambda _source: WORKFLOW_VALUE_MISSING,
        )
    ) == {"result": "fallback"}

    nullable = condition_aggregate_spec_payload()
    nullable["workflow_outputs"][0]["source"] = None  # type: ignore[index]
    nullable["workflow_outputs"][0]["value_type"]["nullable"] = True  # type: ignore[index]
    nullable_ir = compile_workflow(WorkflowSpecV1.model_validate(nullable), cache=None)
    assert thaw_json(
        settle_workflow_outputs(
            nullable_ir.workflow_outputs,
            resolve=lambda _source: WORKFLOW_VALUE_MISSING,
        )
    ) == {"result": None}

    explicit_null = condition_aggregate_spec_payload()
    explicit_null["workflow_outputs"][0]["source"] = literal(None)  # type: ignore[index]
    explicit_null["workflow_outputs"][0]["value_type"]["nullable"] = True  # type: ignore[index]
    explicit_null["workflow_outputs"][0]["default"] = "fallback"  # type: ignore[index]
    explicit_null_ir = compile_workflow(WorkflowSpecV1.model_validate(explicit_null), cache=None)
    assert thaw_json(
        settle_workflow_outputs(
            explicit_null_ir.workflow_outputs,
            resolve=lambda _source: None,
        )
    ) == {"result": None}

    for invalid_value in (float("nan"), float("inf"), object(), 1):
        with pytest.raises(WorkflowOutputSettlementError, match="WORKFLOW_OUTPUT_INVALID"):
            settle_workflow_outputs(
                defaulted_ir.workflow_outputs,
                resolve=lambda _source, value=invalid_value: value,
            )


def test_ir_json_schema_enforces_json_null_collection_and_nullable_enum_semantics() -> None:
    payload = condition_aggregate_spec_payload()
    flag = payload["workflow_inputs"][0]
    flag["value_type"] = value_type("string", nullable=True)
    flag["default"] = None
    flag["constraints"] = {"kind": "enum", "options": ["enabled"]}
    condition = next(item for item in payload["nodes"] if item["id"] == CONDITION_ID)
    clause = condition["config"]["branches"][0]["predicate"]["items"][0]
    clause["operator"] = "is_null"
    clause.pop("right")
    payload["workflow_inputs"].extend(
        [
            {
                "id": "document",
                "name": "document",
                "label": None,
                "description": None,
                "value_type": value_type("json"),
                "required": True,
                "constraints": {"kind": "none"},
            },
            {
                "id": "items",
                "name": "items",
                "label": None,
                "description": None,
                "value_type": value_type("json", collection=True),
                "required": True,
                "constraints": {"kind": "none"},
            },
        ]
    )
    projection = workflow_ir_public_projection_v1(compile_workflow(WorkflowSpecV1.model_validate(payload), cache=None))
    validator = Draft202012Validator(projection["input_schema"])

    assert validator.is_valid({"flag": None, "document": {"id": 1}, "items": [None, {"id": 1}]})
    assert validator.is_valid({"flag": "enabled", "document": "scalar-json", "items": []})
    assert not validator.is_valid({"flag": "disabled", "document": {}, "items": []})
    assert not validator.is_valid({"flag": None, "document": None, "items": []})
    assert not validator.is_valid({"flag": None, "document": [], "items": []})
    assert not validator.is_valid({"flag": None, "document": {}, "items": {}})


def test_compiler_topology_and_static_edge_ordering_have_no_quadratic_list_scans() -> None:
    source = inspect.getsource(compiler_core)
    assert "pop(0)" not in source
    assert "transition_source_original" not in source
    assert "pop(0)" not in inspect.getsource(compiler_topology)

    transform_count = 750
    transform_ids = [str(UUID(int=index + 100)) for index in range(transform_count)]
    nodes = [node(START_ID, "start", {})]
    nodes.extend(node(node_id, "transform", transform_config(str(index))) for index, node_id in enumerate(transform_ids))
    nodes.append(node(END_ID, "end", {}))
    transitions = [transition("edge-start", START_ID, "next", transform_ids[0])]
    transitions.extend(transition(f"edge-{index}", source, "next", target) for index, (source, target) in enumerate(zip(transform_ids, transform_ids[1:])))
    transitions.append(transition("edge-end", transform_ids[-1], "next", END_ID))
    payload = {
        "schema_version": 1,
        "entry_node_id": START_ID,
        "nodes": nodes,
        "transitions": transitions,
        "workflow_inputs": [],
        "workflow_outputs": [
            {
                "id": "result",
                "name": "result",
                "description": None,
                "value_type": value_type(),
                "source": node_output(transform_ids[-1], "result"),
            }
        ],
        "credential_slots": [],
    }
    spec = WorkflowSpecV1.model_validate(payload)

    first = workflow_ir_public_projection_v1(compile_workflow(spec, cache=None))
    second = workflow_ir_public_projection_v1(compile_workflow(spec, cache=None))
    assert first == second
    assert len(first["static_edges"]) == 1
    assert len(first["outcome_routes"]) == transform_count


def test_semantically_equivalent_authored_order_compiles_identically_across_processes() -> None:
    payload = condition_aggregate_spec_payload()
    payload["nodes"] = list(reversed(payload["nodes"]))
    payload["transitions"] = list(reversed(payload["transitions"]))
    spec = WorkflowSpecV1.model_validate(payload)
    expected = json.dumps(workflow_ir_public_projection_v1(compile_workflow(spec)), sort_keys=True, separators=(",", ":"))

    script = """
import json, sys
from deerflow.workflows import WorkflowSpecV1
from deerflow.workflows.compiler import compile_workflow, workflow_ir_public_projection_v1
spec = WorkflowSpecV1.model_validate(json.load(sys.stdin))
print(json.dumps(workflow_ir_public_projection_v1(compile_workflow(spec)), sort_keys=True, separators=(',', ':')))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=BACKEND_ROOT,
        input=json.dumps(payload),
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == expected


def test_checksum_equivalent_unicode_and_integral_numbers_lower_to_identical_ir() -> None:
    composed = condition_aggregate_spec_payload()
    decomposed = condition_aggregate_spec_payload()
    for payload, text_value, number_default in (
        (composed, "é", 1),
        (decomposed, "e\u0301", 1.0),
    ):
        transform = next(item for item in payload["nodes"] if item["id"] == LEFT_ID)
        transform["config"]["template"]["segments"][0]["value"] = text_value
        payload["workflow_inputs"].append(
            {
                "id": "count",
                "name": "count",
                "label": None,
                "description": None,
                "value_type": value_type("number"),
                "required": False,
                "default": number_default,
                "constraints": {"kind": "none"},
            }
        )

    composed_spec = WorkflowSpecV1.model_validate(composed)
    decomposed_spec = WorkflowSpecV1.model_validate(decomposed)
    assert semantic_checksum(composed_spec) == semantic_checksum(decomposed_spec)

    composed_json = json.dumps(
        workflow_ir_public_projection_v1(compile_workflow(composed_spec, cache=None)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    decomposed_json = json.dumps(
        workflow_ir_public_projection_v1(compile_workflow(decomposed_spec, cache=None)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert composed_json == decomposed_json


def test_predicate_template_and_condition_route_semantics_are_bounded_and_ordered() -> None:
    values = {
        ("workflow_input", "flag"): True,
        ("workflow_input", "name"): "Ada",
    }

    def resolve(binding: object) -> object:
        kind = binding.kind
        if kind == "literal":
            return binding.value
        if kind == "workflow_input":
            return values[(kind, binding.input_id)]
        raise AssertionError(kind)

    ast = WorkflowSpecV1.model_validate(condition_aggregate_spec_payload()).nodes[1].config.branches[0].predicate  # type: ignore[union-attr]
    assert evaluate_predicate(ast, resolve=resolve) is True
    assert route_condition((("truthy", ast),), else_output_port_id="fallback", resolve=resolve) == "truthy"

    rendered = render_restricted_template(
        RestrictedTemplate.model_validate(template("Hello ", workflow_input("name"), literal(None))),
        resolve=resolve,
    )
    assert rendered == "Hello Adanull"


def test_loop_commit_protocol_updates_every_variable_atomically_then_routes_or_fails_at_limit() -> None:
    observed: list[dict[str, object]] = []

    first = commit_loop_iteration(
        current_iteration=0,
        max_iterations=2,
        variable_ids=("left", "right"),
        next_values={"left": 1, "right": None},
        termination=lambda values, iteration: observed.append(dict(values)) or (values["left"] >= 2 and iteration >= 1),
    )
    assert first.iteration == 1
    assert dict(first.variables) == {"left": 1, "right": None}
    assert observed == [{"left": 1, "right": None}]
    assert first.decision is LoopCommitDecision.CONTINUE

    done = commit_loop_iteration(
        current_iteration=1,
        max_iterations=2,
        variable_ids=("left", "right"),
        next_values={"left": 2, "right": "committed"},
        termination=lambda values, iteration: values["right"] == "committed" and iteration == 2,
    )
    assert done.decision is LoopCommitDecision.DONE

    limited = commit_loop_iteration(
        current_iteration=1,
        max_iterations=2,
        variable_ids=("left",),
        next_values={"left": 2},
        termination=lambda _values, _iteration: False,
    )
    assert limited.decision is LoopCommitDecision.LIMIT_EXCEEDED
    assert limited.error_code == "WORKFLOW_LOOP_LIMIT_EXCEEDED"

    with pytest.raises(LoopCommitProtocolError, match="complete exact variable set"):
        commit_loop_iteration(
            current_iteration=0,
            max_iterations=2,
            variable_ids=("left", "right"),
            next_values={"left": 1},
            termination=lambda _values, _iteration: False,
        )
