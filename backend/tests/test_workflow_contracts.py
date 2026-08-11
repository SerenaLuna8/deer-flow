from __future__ import annotations

import hashlib
import json
import struct
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from app.workflows.catalog_contracts import resolve_workflow_instance_ports_v1
from deerflow.workflows import (
    WORKFLOW_NODE_KINDS,
    BoundedJsonPointer,
    CanvasDocumentV1,
    ConditionNodeConfigV1,
    EndNodeConfigV1,
    HttpRequestNodeConfigV1,
    LlmNodeConfigV1,
    LoopNodeConfigV1,
    PythonCodeNodeConfigV1,
    StartNodeConfigV1,
    TransformNodeConfigV1,
    VariableAggregateNodeConfigV1,
    WorkflowCredentialSlotId,
    WorkflowInputId,
    WorkflowPortId,
    WorkflowSpecV1,
    WorkflowValueType,
    canonical_json_value,
    canonical_json_value_with_utf8_budget,
    canvas_document_public_projection_v1,
    semantic_canonical_json,
    semantic_checksum,
    workflow_spec_public_projection_v1,
)
from deerflow.workflows.canonical import CANONICAL_BINARY64_ALGORITHM

WORKFLOW_FIXTURE_DIRECTORY = Path(__file__).resolve().parents[2] / "frontend/tests/fixtures/workflows"
START_NODE_ID = "00000000-0000-4000-8000-000000000001"
LLM_NODE_ID = "00000000-0000-4000-8000-000000000002"
CONDITION_NODE_ID = "00000000-0000-4000-8000-000000000003"
TRANSFORM_NODE_ID = "00000000-0000-4000-8000-000000000004"
AGGREGATE_NODE_ID = "00000000-0000-4000-8000-000000000005"
LOOP_NODE_ID = "00000000-0000-4000-8000-000000000006"
HTTP_NODE_ID = "00000000-0000-4000-8000-000000000007"
PYTHON_NODE_ID = "00000000-0000-4000-8000-000000000008"
END_NODE_ID = "00000000-0000-4000-8000-000000000009"
PYTHON_INPUT_ID = "10000000-0000-4000-8000-000000000001"
_SHARED_PUBLIC_PROJECTIONS = WORKFLOW_FIXTURE_DIRECTORY / "public-projections-v1.json"
_SHARED_UNICODE_BOUNDARIES = WORKFLOW_FIXTURE_DIRECTORY / "unicode-code-point-boundaries-v1.json"


def _value_type(kind: str = "string") -> dict[str, object]:
    return {
        "kind": kind,
        "collection": False,
        "nullable": False,
    }


def _literal(value: object) -> dict[str, object]:
    return {"kind": "literal", "value": value}


@pytest.mark.parametrize("future_kind", ["file", "image", "document"])
def test_first_batch_value_types_reject_future_file_kinds(future_kind: str) -> None:
    with pytest.raises(ValidationError):
        WorkflowValueType.model_validate(_value_type(future_kind))


@pytest.mark.parametrize("value", ["输入", "😀", "1starts-with-digit", "_leading-underscore", "a" * 129])
def test_workflow_input_ids_are_ascii_and_cross_runtime_bounded(value: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(WorkflowInputId).validate_python(value)


@pytest.mark.parametrize(
    ("contract", "value"),
    [
        (WorkflowPortId, "_leading-underscore"),
        (WorkflowPortId, "分支"),
        (WorkflowCredentialSlotId, "1starts-with-digit"),
        (WorkflowCredentialSlotId, "😀"),
    ],
)
def test_port_and_credential_slot_ids_use_the_frozen_ascii_contract(contract: object, value: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(contract).validate_python(value)


def test_credential_slot_required_literal_rejects_integer_coercion() -> None:
    payload = _spec_payload()
    payload["credential_slots"] = [
        {
            "id": "http_auth",
            "name": "HTTP auth",
            "purpose": "http_auth",
            "payload_schema": {},
            "required": 1,
        }
    ]

    with pytest.raises(ValidationError):
        WorkflowSpecV1.model_validate(payload)


def test_json_pointer_uses_the_shared_unicode_code_point_boundary() -> None:
    shared = json.loads(_SHARED_UNICODE_BOUNDARIES.read_text(encoding="utf-8"))
    character = shared["astral_character"]
    minimum = shared["json_pointer"]["minimum"]
    maximum = shared["json_pointer"]["maximum"]

    assert TypeAdapter(BoundedJsonPointer).validate_python(character * minimum) == character * minimum
    assert TypeAdapter(BoundedJsonPointer).validate_python(character * maximum) == character * maximum
    with pytest.raises(ValidationError):
        TypeAdapter(BoundedJsonPointer).validate_python(character * (maximum + 1))


def _template(value: str = "hello") -> dict[str, object]:
    return {
        "version": 1,
        "segments": [{"kind": "text", "value": value}],
    }


def _predicate() -> dict[str, object]:
    return {
        "op": "and",
        "items": [
            {
                "left": _literal(1),
                "operator": "eq",
                "right": _literal(1),
            }
        ],
    }


def _execution_policy() -> dict[str, object]:
    return {
        "retry": {"mode": "none"},
        "on_error": {"mode": "fail_workflow"},
    }


def _node(node_id: str, node_type: str, config: dict[str, object]) -> dict[str, object]:
    return {
        "id": node_id,
        "type": node_type,
        "type_version": 1,
        "scope": {"kind": "root"},
        "custom_label": None,
        "description": None,
        "input_bindings": {},
        "execution_policy": _execution_policy(),
        "config": config,
    }


def _all_node_payloads() -> list[dict[str, object]]:
    return [
        _node(START_NODE_ID, "start", {}),
        _node(
            LLM_NODE_ID,
            "llm",
            {
                "model_ref": "default",
                "mode": "chat",
                "context_input_ids": ["context"],
                "messages": [
                    {
                        "id": "message-1",
                        "role": "system",
                        "content": _template("请回答"),
                    }
                ],
                "model_parameters": {"temperature": 0.0},
                "stream": True,
                "reasoning_output": "omit",
                "structured_output": {"enabled": False, "schema": None},
            },
        ),
        _node(
            CONDITION_NODE_ID,
            "condition",
            {
                "branches": [
                    {
                        "id": "branch-1",
                        "output_port_id": "true",
                        "label": "命中",
                        "predicate": _predicate(),
                    }
                ],
                "else_output_port_id": "else",
            },
        ),
        _node(
            TRANSFORM_NODE_ID,
            "transform",
            {
                "mode": "text",
                "input_variables": [
                    {
                        "id": "input-1",
                        "name": "name",
                        "value_type": _value_type(),
                    }
                ],
                "missing_variable": "error",
                "template": _template(),
                "output_schema": None,
            },
        ),
        _node(
            AGGREGATE_NODE_ID,
            "variable_aggregate",
            {
                "strategy": "exclusive_branch",
                "groups": [
                    {
                        "id": "result",
                        "name": "result",
                        "value_type": _value_type(),
                        "candidate_input_ids": ["left", "right"],
                    }
                ],
            },
        ),
        _node(
            LOOP_NODE_ID,
            "loop",
            {
                "mode": "do_until",
                "body_entry_node_id": PYTHON_NODE_ID,
                "body_exit_node_id": PYTHON_NODE_ID,
                "max_iterations": 10,
                "termination_condition": _predicate(),
                "variables": [
                    {
                        "id": "loop-value",
                        "name": "value",
                        "value_type": _value_type("number"),
                        "initial_input_id": "initial",
                        "next_input_id": "next",
                        "output_port_id": "value",
                    }
                ],
            },
        ),
        _node(
            HTTP_NODE_ID,
            "http_request",
            {
                "method": "POST",
                "base_origin": "https://example.com",
                "path_template": _template("/v1/items"),
                "query": [],
                "headers": [],
                "auth": {"mode": "none"},
                "body": {
                    "kind": "json",
                    "template": {
                        "version": 1,
                        "template": {"name": {"$binding": "name"}},
                        "bindings": {"name": _literal("Ada")},
                    },
                },
                "timeout": {
                    "connect_ms": None,
                    "read_ms": 1000,
                    "write_ms": None,
                },
                "response": {
                    "mode": "json",
                    "accepted_statuses": [{"from": 200, "to": 299}],
                    "schema": {"type": "object"},
                },
            },
        ),
        _node(
            PYTHON_NODE_ID,
            "python_code",
            {
                "source": "def main(inputs):\n    return inputs\n",
                "input_variables": [
                    {
                        "id": PYTHON_INPUT_ID,
                        "name": "value",
                        "value_type": _value_type(),
                    }
                ],
                "output_schema": {"type": "object"},
                "timeout_ms": None,
            },
        ),
        _node(END_NODE_ID, "end", {}),
    ]


def _spec_payload(nodes: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "schema_version": 1,
        "entry_node_id": START_NODE_ID,
        "nodes": nodes if nodes is not None else [_node(START_NODE_ID, "start", {})],
        "transitions": [],
        "workflow_inputs": [],
        "workflow_outputs": [],
        "credential_slots": [],
    }


def test_public_projection_preserves_nullable_fields_and_omits_unset_fields() -> None:
    shared = json.loads(_SHARED_PUBLIC_PROJECTIONS.read_text(encoding="utf-8"))

    spec_projection = workflow_spec_public_projection_v1(WorkflowSpecV1.model_validate(shared["workflow_spec"]))
    canvas_projection = canvas_document_public_projection_v1(CanvasDocumentV1.model_validate(shared["canvas_document"]))

    assert spec_projection == shared["workflow_spec"]
    assert canvas_projection == shared["canvas_document"]
    assert spec_projection["workflow_inputs"][0]["label"] is None
    assert "default" not in spec_projection["workflow_inputs"][0]
    assert "schema_ref" not in spec_projection["workflow_inputs"][0]["value_type"]
    assert "parent_node_id" not in canvas_projection["node_layouts"][0]


def test_workflow_input_ids_are_valid_start_ports_at_the_strict_spec_boundary() -> None:
    valid_input_id = "a" + "_" * 127
    payload = _spec_payload()
    payload["workflow_inputs"] = [
        {
            "id": valid_input_id,
            "name": "input",
            "label": None,
            "description": None,
            "value_type": _value_type(),
            "required": True,
            "constraints": {"kind": "none"},
        }
    ]

    strict_spec = WorkflowSpecV1.model_validate(payload)
    resolved = resolve_workflow_instance_ports_v1(payload)

    assert strict_spec.workflow_inputs[0].id == valid_input_id
    assert any(port.id == valid_input_id for port in resolved.nodes[0].output_ports)

    invalid = deepcopy(payload)
    invalid["workflow_inputs"][0]["id"] = "_leading-underscore"
    with pytest.raises(ValidationError):
        WorkflowSpecV1.model_validate(invalid)
    with pytest.raises(ValidationError):
        resolve_workflow_instance_ports_v1(invalid)


def test_workflow_spec_parses_all_nine_discriminated_node_configs() -> None:
    spec = WorkflowSpecV1.model_validate(_spec_payload(_all_node_payloads()))

    assert [type(node.config) for node in spec.nodes] == [
        StartNodeConfigV1,
        LlmNodeConfigV1,
        ConditionNodeConfigV1,
        TransformNodeConfigV1,
        VariableAggregateNodeConfigV1,
        LoopNodeConfigV1,
        HttpRequestNodeConfigV1,
        PythonCodeNodeConfigV1,
        EndNodeConfigV1,
    ]
    assert spec.nodes[1].config.structured_output.model_dump() == {"enabled": False, "schema": None}
    assert spec.nodes[6].config.response.model_dump()["accepted_statuses"] == [{"from": 200, "to": 299}]


def test_frontend_fixture_has_the_same_strict_contract_and_semantic_checksum() -> None:
    payload = json.loads((WORKFLOW_FIXTURE_DIRECTORY / "workflow-spec-v1.json").read_text(encoding="utf-8"))
    expected_checksum = (WORKFLOW_FIXTURE_DIRECTORY / "workflow-spec-v1.semantic.sha256").read_text(encoding="ascii").strip()

    spec = WorkflowSpecV1.model_validate(payload)
    resolved = resolve_workflow_instance_ports_v1(payload)

    assert len(spec.nodes) == len(WORKFLOW_NODE_KINDS)
    assert len(resolved.nodes) == len(WORKFLOW_NODE_KINDS)
    assert semantic_checksum(spec) == expected_checksum


def test_canonical_json_utf8_budget_is_exact_and_stops_amplified_number_output() -> None:
    canonical, byte_count = canonical_json_value_with_utf8_budget(["测"], max_utf8_bytes=7)
    assert canonical == '["测"]'
    assert byte_count == 7

    with pytest.raises(ValueError, match="UTF-8 byte budget"):
        canonical_json_value_with_utf8_budget(["测"], max_utf8_bytes=6)

    escaped_value = {"accent": "e\u0301", "control": "\x00\b\f\n\r\t", "quoted": '"\\', "separator": "\u2028"}
    escaped_canonical = canonical_json_value(escaped_value)
    escaped_bytes = len(escaped_canonical.encode("utf-8"))
    assert canonical_json_value_with_utf8_budget(escaped_value, max_utf8_bytes=escaped_bytes) == (escaped_canonical, escaped_bytes)
    with pytest.raises(ValueError, match="UTF-8 byte budget"):
        canonical_json_value_with_utf8_budget(escaped_value, max_utf8_bytes=escaped_bytes - 1)

    visited = 0

    class CountingSubnormalList(list[float]):
        def __iter__(self):
            nonlocal visited
            for item in super().__iter__():
                visited += 1
                yield item

    amplified = CountingSubnormalList([5e-324] * 65_535)
    with pytest.raises(ValueError, match="UTF-8 byte budget"):
        canonical_json_value_with_utf8_budget(amplified, max_utf8_bytes=4_096)
    assert visited < 10


def test_shared_canonical_value_corpus_matches_python_and_typescript_contract() -> None:
    corpus = json.loads((WORKFLOW_FIXTURE_DIRECTORY / "canonical-values-v1.json").read_text(encoding="utf-8"))

    for case in corpus["accepted"]:
        assert canonical_json_value(case["value"]) == case["canonical"]
        expected_bytes = len(case["canonical"].encode("utf-8"))
        assert canonical_json_value_with_utf8_budget(case["value"], max_utf8_bytes=expected_bytes) == (case["canonical"], expected_bytes)
    for case in corpus["rejected"]:
        with pytest.raises(ValueError):
            canonical_json_value(case["value"])


def _float_from_binary64_bits(bits: str) -> float:
    return struct.unpack(">d", bytes.fromhex(bits))[0]


def _canonical_or_rejection(value: float) -> str:
    try:
        return canonical_json_value(value)
    except ValueError as error:
        message = str(error)
        if "non-finite" in message:
            return "reject:non-finite"
        if "safe range" in message:
            return "reject:unsafe-integer"
        raise


def test_binary64_canonical_algorithm_matches_raw_bit_pattern_corpus() -> None:
    corpus = json.loads((WORKFLOW_FIXTURE_DIRECTORY / "canonical-binary64-v1.json").read_text(encoding="utf-8"))

    assert corpus["algorithm"] == CANONICAL_BINARY64_ALGORITHM
    legacy_mismatches = [case for case in corpus["cases"] if "legacy_python" in case]
    assert len(legacy_mismatches) == corpus["legacy_cross_runtime_mismatch_count"] == 4
    assert all(case["legacy_python"] != case["legacy_typescript"] for case in legacy_mismatches)
    for case in corpus["cases"]:
        assert _canonical_or_rejection(_float_from_binary64_bits(case["bits"])) == case["canonical"]


def test_binary64_canonical_algorithm_matches_deterministic_random_property_digest() -> None:
    corpus = json.loads((WORKFLOW_FIXTURE_DIRECTORY / "canonical-binary64-v1.json").read_text(encoding="utf-8"))
    property_contract = corpus["random_property"]
    assert property_contract["generator"] == "splitmix64-v1"

    mask = (1 << 64) - 1
    state = int(property_contract["seed"], 16)
    digest = hashlib.sha256()
    for _ in range(property_contract["count"]):
        state = (state + 0x9E3779B97F4A7C15) & mask
        generated = state
        generated = ((generated ^ (generated >> 30)) * 0xBF58476D1CE4E5B9) & mask
        generated = ((generated ^ (generated >> 27)) * 0x94D049BB133111EB) & mask
        bits = (generated ^ (generated >> 31)) & mask
        encoded = _canonical_or_rejection(_float_from_binary64_bits(f"{bits:016x}"))
        digest.update(f"{bits:016x}:{encoded}\n".encode())

    assert digest.hexdigest() == property_contract["sha256"]


@pytest.mark.parametrize(
    "invalid_node_id",
    [
        "not-a-uuid",
        "00000000-0000-4000-8000-00000000000A",
        "{00000000-0000-4000-8000-00000000000a}",
    ],
)
def test_node_identity_fields_reject_noncanonical_uuid_strings(invalid_node_id: str) -> None:
    payload = _spec_payload()
    payload["entry_node_id"] = invalid_node_id

    with pytest.raises(ValidationError, match="canonical lowercase UUID"):
        WorkflowSpecV1.model_validate(payload)


def test_workflow_node_kind_contract_has_one_stable_nine_kind_order() -> None:
    assert WORKFLOW_NODE_KINDS == (
        "start",
        "llm",
        "condition",
        "transform",
        "variable_aggregate",
        "loop",
        "http_request",
        "python_code",
        "end",
    )


def test_scope_value_bindings_and_execution_policy_are_discriminated() -> None:
    node = _node(PYTHON_NODE_ID, "python_code", _all_node_payloads()[7]["config"])
    node["scope"] = {"kind": "loop_body", "loop_node_id": LOOP_NODE_ID}
    node["input_bindings"] = {
        "literal": _literal({"value": None}),
        "workflow": {"kind": "workflow_input", "input_id": "workflow-input"},
        "loop": {"kind": "loop_variable", "loop_node_id": LOOP_NODE_ID, "variable_id": "value"},
        "node": {"kind": "node_output", "node_id": LLM_NODE_ID, "output_id": "result", "path": "/name"},
        "draft-unbound": None,
    }
    node["execution_policy"] = {
        "retry": {"mode": "bounded", "max_attempts": 3, "backoff_ms": 100},
        "on_error": {"mode": "route_error", "output_port_id": "error"},
    }

    spec = WorkflowSpecV1.model_validate(_spec_payload([node]))

    assert spec.nodes[0].scope.kind == "loop_body"
    assert [binding.kind if binding is not None else None for binding in spec.nodes[0].input_bindings.values()] == [
        "literal",
        "workflow_input",
        "loop_variable",
        "node_output",
        None,
    ]
    assert spec.nodes[0].execution_policy.retry.mode == "bounded"
    assert spec.nodes[0].execution_policy.on_error.mode == "route_error"

    node["input_bindings"]["credential"] = {"kind": "credential", "credential_id": "secret"}
    with pytest.raises(ValidationError):
        WorkflowSpecV1.model_validate(_spec_payload([node]))


def test_workflow_spec_rejects_node_type_config_mismatch() -> None:
    llm_config = _all_node_payloads()[1]["config"]

    with pytest.raises(ValidationError):
        WorkflowSpecV1.model_validate(_spec_payload([_node(START_NODE_ID, "start", llm_config)]))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("schema_version",), "1"),
        (("schema_version",), 2),
        (("nodes", 0, "type_version"), 1.5),
        (("nodes", 0, "type_version"), 2),
        (("nodes", 0, "unexpected"), True),
        (("nodes", 0, "scope", "unexpected"), True),
        (("nodes", 0, "execution_policy", "retry", "unexpected"), True),
    ],
)
def test_workflow_spec_is_strict_and_forbids_unknown_fields(path: tuple[object, ...], value: object) -> None:
    payload = _spec_payload()
    target: object = payload
    for part in path[:-1]:
        target = target[part]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]

    with pytest.raises(ValidationError):
        WorkflowSpecV1.model_validate(payload)


def test_canvas_document_is_strict_and_forbids_runtime_fields() -> None:
    canvas = CanvasDocumentV1.model_validate(
        {
            "schema_version": 1,
            "node_layouts": [
                {
                    "node_id": PYTHON_NODE_ID,
                    "position": {"x": 12.5, "y": 20.0},
                    "parent_node_id": LOOP_NODE_ID,
                    "collapsed": False,
                }
            ],
            "edge_layouts": [{"edge_id": "edge-1", "routing": "smoothstep"}],
        }
    )

    assert canvas.node_layouts[0].parent_node_id == LOOP_NODE_ID

    with pytest.raises(ValidationError):
        CanvasDocumentV1.model_validate(
            {
                "schema_version": 1,
                "node_layouts": [],
                "edge_layouts": [],
                "viewport": {"x": 0, "y": 0, "zoom": 1},
            }
        )

    for invalid_edge_id in ("", "edge with spaces", "e" * 129):
        with pytest.raises(ValidationError):
            CanvasDocumentV1.model_validate(
                {
                    "schema_version": 1,
                    "node_layouts": [],
                    "edge_layouts": [{"edge_id": invalid_edge_id, "routing": "bezier"}],
                }
            )

    with pytest.raises(ValidationError):
        CanvasDocumentV1.model_validate(
            {
                "schema_version": 2,
                "node_layouts": [],
                "edge_layouts": [],
            }
        )


@pytest.mark.parametrize(
    "invalid_value",
    [
        float("nan"),
        float("inf"),
        {1: "non-string-key"},
        ("tuple-is-not-json",),
    ],
)
def test_json_values_are_strict_finite_json(invalid_value: object) -> None:
    payload = _spec_payload()
    payload["workflow_inputs"] = [
        {
            "id": "input",
            "name": "input",
            "label": None,
            "description": None,
            "value_type": _value_type("json"),
            "required": False,
            "default": invalid_value,
            "constraints": {"kind": "none"},
        }
    ]

    with pytest.raises(ValidationError):
        WorkflowSpecV1.model_validate(payload)


def test_semantic_canonical_json_has_a_frozen_minimal_golden() -> None:
    spec = WorkflowSpecV1.model_validate(_spec_payload())

    canonical = semantic_canonical_json(spec)

    assert canonical == (
        f'{{"credential_slots":[],"entry_node_id":"{START_NODE_ID}","nodes":['
        '{"config":{},"execution_policy":{"on_error":{"mode":"fail_workflow"},'
        f'"retry":{{"mode":"none"}}}},"id":"{START_NODE_ID}","input_bindings":{{}},'
        '"scope":{"kind":"root"},"type":"start","type_version":1}],'
        '"schema_version":1,"transitions":[],"workflow_inputs":[],"workflow_outputs":[]}'
    )
    assert semantic_checksum(spec) == "32e43addc4f47abbd66b4de0c701636673c09abd02b6362c1b0efc69e4e44b32"


def test_semantic_canonical_json_sorts_object_keys_by_unicode_scalar_value() -> None:
    payload = _spec_payload()
    payload["workflow_inputs"] = [
        {
            "id": "input",
            "name": "input",
            "label": None,
            "description": None,
            "value_type": _value_type("json"),
            "required": False,
            "default": {
                "2": "two",
                "10": "ten",
                "\U00010000": "astral",
                "\ue000": "bmp-private-use",
            },
            "constraints": {"kind": "none"},
        }
    ]

    canonical = semantic_canonical_json(WorkflowSpecV1.model_validate(payload))

    assert '"default":{"10":"ten","2":"two","\ue000":"bmp-private-use","\U00010000":"astral"}' in canonical


def test_workflow_contract_rejects_unpaired_unicode_surrogates() -> None:
    payload = _spec_payload()
    payload["workflow_inputs"] = [
        {
            "id": "input",
            "name": "input",
            "label": None,
            "description": None,
            "value_type": _value_type("string"),
            "required": False,
            "default": "\ud800",
            "constraints": {"kind": "none"},
        }
    ]

    with pytest.raises(ValidationError, match="Unicode scalar"):
        WorkflowSpecV1.model_validate(payload)


def test_semantic_checksum_ignores_top_level_order_and_display_metadata() -> None:
    payload = _spec_payload([_node(START_NODE_ID, "start", {}), _node(END_NODE_ID, "end", {})])
    payload["transitions"] = [
        {
            "id": "transition-b",
            "source": {"node_id": START_NODE_ID, "port_id": "out"},
            "target": {"node_id": END_NODE_ID, "port_id": "in"},
        },
        {
            "id": "transition-a",
            "source": {"node_id": START_NODE_ID, "port_id": "out-2"},
            "target": {"node_id": END_NODE_ID, "port_id": "in-2"},
        },
    ]
    payload["workflow_inputs"] = [
        {
            "id": "input-b",
            "name": "b",
            "label": "B",
            "description": "input description B",
            "value_type": _value_type(),
            "required": False,
            "default": None,
            "constraints": {"kind": "none"},
        },
        {
            "id": "input-a",
            "name": "a",
            "label": "A",
            "description": "input description A",
            "value_type": _value_type(),
            "required": True,
            "constraints": {"kind": "none"},
        },
    ]
    payload["workflow_outputs"] = [
        {
            "id": "output-b",
            "name": "b",
            "description": "output description B",
            "value_type": _value_type(),
            "source": None,
        },
        {
            "id": "output-a",
            "name": "a",
            "description": "output description A",
            "value_type": _value_type(),
            "source": _literal("value"),
        },
    ]
    payload["credential_slots"] = [
        {
            "id": "slot-b",
            "name": "b",
            "purpose": "http_auth",
            "payload_schema": {"type": "object", "required": ["token"]},
            "required": True,
        },
        {
            "id": "slot-a",
            "name": "a",
            "purpose": "http_auth",
            "payload_schema": {"type": "object"},
            "required": True,
        },
    ]
    reordered = deepcopy(payload)
    for key in ("nodes", "transitions", "workflow_inputs", "workflow_outputs", "credential_slots"):
        reordered[key] = list(reversed(reordered[key]))
    reordered["nodes"][0]["custom_label"] = "结束（展示名）"
    reordered["nodes"][0]["description"] = "new node description"
    reordered["workflow_inputs"][0]["description"] = "new input description"
    reordered["workflow_outputs"][0]["description"] = "new output description"

    original_spec = WorkflowSpecV1.model_validate(payload)
    reordered_spec = WorkflowSpecV1.model_validate(reordered)

    assert semantic_canonical_json(original_spec) == semantic_canonical_json(reordered_spec)
    assert semantic_checksum(original_spec) == semantic_checksum(reordered_spec)


def test_semantic_checksum_preserves_nested_ordered_arrays() -> None:
    aggregate = _all_node_payloads()[4]
    reversed_candidates = deepcopy(aggregate)
    reversed_candidates["config"]["groups"][0]["candidate_input_ids"].reverse()

    left = WorkflowSpecV1.model_validate(_spec_payload([aggregate]))
    right = WorkflowSpecV1.model_validate(_spec_payload([reversed_candidates]))

    assert semantic_checksum(left) != semantic_checksum(right)


def test_semantic_projection_does_not_strip_description_keys_inside_user_json() -> None:
    payload = _spec_payload()
    payload["workflow_inputs"] = [
        {
            "id": "input",
            "name": "input",
            "label": None,
            "description": "display-only declaration description",
            "value_type": _value_type("json"),
            "required": False,
            "default": {"description": "semantic user value"},
            "constraints": {"kind": "none"},
        }
    ]
    payload["credential_slots"] = [
        {
            "id": "slot",
            "name": "slot",
            "purpose": "http_auth",
            "payload_schema": {"type": "object", "description": "semantic schema annotation"},
            "required": True,
        }
    ]

    canonical = semantic_canonical_json(WorkflowSpecV1.model_validate(payload))

    assert "display-only declaration description" not in canonical
    assert "semantic user value" in canonical
    assert "semantic schema annotation" in canonical


def test_semantic_canonical_json_normalizes_json_numbers_and_unicode() -> None:
    left_payload = _spec_payload()
    right_payload = _spec_payload()
    for payload, default, label in ((left_payload, -0.0, "e\u0301"), (right_payload, 0, "é")):
        payload["workflow_inputs"] = [
            {
                "id": "input",
                "name": "输入",
                "label": label,
                "description": None,
                "value_type": _value_type("number"),
                "required": False,
                "default": default,
                "constraints": {"kind": "none"},
            }
        ]

    left = WorkflowSpecV1.model_validate(left_payload)
    right = WorkflowSpecV1.model_validate(right_payload)

    assert semantic_checksum(left) == semantic_checksum(right)
    assert "é" in semantic_canonical_json(left)
    assert "e\u0301" not in semantic_canonical_json(left)


def test_semantic_canonical_json_rejects_duplicate_nfc_object_keys() -> None:
    payload = _spec_payload()
    payload["workflow_inputs"] = [
        {
            "id": "input",
            "name": "input",
            "label": None,
            "description": None,
            "value_type": _value_type("json"),
            "required": False,
            "default": {"e\u0301": 1, "é": 2},
            "constraints": {"kind": "none"},
        }
    ]

    with pytest.raises(ValueError, match="Unicode normalization produced duplicate Workflow object keys"):
        WorkflowSpecV1.model_validate(payload)


def test_semantic_canonical_json_preserves_omitted_vs_explicit_null_default() -> None:
    omitted_payload = _spec_payload()
    explicit_null_payload = _spec_payload()
    declaration = {
        "id": "input",
        "name": "input",
        "label": None,
        "description": None,
        "value_type": _value_type("json"),
        "required": False,
        "constraints": {"kind": "none"},
    }
    omitted_payload["workflow_inputs"] = [declaration]
    explicit_null_payload["workflow_inputs"] = [{**declaration, "default": None}]

    omitted = semantic_canonical_json(WorkflowSpecV1.model_validate(omitted_payload))
    explicit_null = semantic_canonical_json(WorkflowSpecV1.model_validate(explicit_null_payload))

    assert '"default"' not in omitted
    assert '"default":null' in explicit_null
    assert omitted != explicit_null


@pytest.mark.parametrize("value", [None])
def test_optional_nonnullable_fields_reject_explicit_null(value: object) -> None:
    payload = _spec_payload()
    payload["workflow_inputs"] = [
        {
            "id": "input",
            "name": "input",
            "label": None,
            "description": None,
            "value_type": {**_value_type(), "schema_ref": value},
            "required": False,
            "constraints": {"kind": "none"},
        }
    ]

    with pytest.raises(ValidationError):
        WorkflowSpecV1.model_validate(payload)

    with pytest.raises(ValidationError):
        CanvasDocumentV1.model_validate(
            {
                "schema_version": 1,
                "node_layouts": [
                    {
                        "node_id": START_NODE_ID,
                        "position": {"x": 0, "y": 0},
                        "parent_node_id": value,
                    }
                ],
                "edge_layouts": [],
            }
        )


@pytest.mark.parametrize(
    ("value", "encoded"),
    [
        (0.2, "2.00000000000000011102230246251565404236316680908203125e-1"),
        (1e-6, "9.99999999999999954748111825886258685613938723690807819366455078125e-7"),
        (1e-7, "9.99999999999999954748111825886258685613938723690807819366455078125e-8"),
        (1.5, "1.5e0"),
        (9_007_199_254_740_991, "9007199254740991"),
    ],
)
def test_semantic_canonical_json_has_cross_runtime_number_spellings(value: int | float, encoded: str) -> None:
    payload = _spec_payload()
    payload["workflow_inputs"] = [
        {
            "id": "input",
            "name": "input",
            "label": None,
            "description": None,
            "value_type": _value_type("number"),
            "required": False,
            "default": value,
            "constraints": {"kind": "none"},
        }
    ]

    canonical = semantic_canonical_json(WorkflowSpecV1.model_validate(payload))

    assert f'"default":{encoded}' in canonical


@pytest.mark.parametrize("unsafe_integer", [10**20, 1e20, 1e21, -(10**20)])
def test_workflow_contract_rejects_cross_runtime_unsafe_integers(unsafe_integer: int | float) -> None:
    payload = _spec_payload()
    payload["workflow_inputs"] = [
        {
            "id": "input",
            "name": "input",
            "label": None,
            "description": None,
            "value_type": _value_type("number"),
            "required": False,
            "default": unsafe_integer,
            "constraints": {"kind": "none"},
        }
    ]

    with pytest.raises(ValidationError, match="safe range"):
        WorkflowSpecV1.model_validate(payload)
