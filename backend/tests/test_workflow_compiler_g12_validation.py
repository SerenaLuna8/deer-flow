from __future__ import annotations

import inspect
from copy import deepcopy

import pytest
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
    loop_variable,
    node,
    node_output,
    predicate,
    template,
    transform_config,
    transition,
    value_type,
)

import deerflow.workflows.validation as workflow_validation
from deerflow.workflows import CanvasDocumentV1, WorkflowSpecV1
from deerflow.workflows.json_schema import INLINE_SCHEMA_REF_PREFIX, inline_json_schema_ref
from deerflow.workflows.registry import resolve_node_ports
from deerflow.workflows.validation import (
    WorkflowCompilationLimits,
    WorkflowValidationError,
    validate_canvas_document,
    validate_workflow,
)


def _validate(payload: dict[str, object], *, limits: WorkflowCompilationLimits | None = None):
    return validate_workflow(
        WorkflowSpecV1.model_validate(payload),
        limits=limits or WorkflowCompilationLimits.permissive(),
    )


def _codes(payload: dict[str, object], *, limits: WorkflowCompilationLimits | None = None) -> tuple[str, ...]:
    return _validate(payload, limits=limits).issue_codes


def _llm_config(*, context_input_ids: list[str] | None = None) -> dict[str, object]:
    return {
        "model_ref": "model/default",
        "mode": "chat",
        "context_input_ids": context_input_ids or [],
        "messages": [
            {
                "id": "message-1",
                "role": "user",
                "content": template("hello"),
            }
        ],
        "model_parameters": {},
        "stream": False,
        "reasoning_output": "omit",
        "structured_output": {"enabled": False, "schema": None},
    }


def _http_config(*, method: str = "GET") -> dict[str, object]:
    return {
        "method": method,
        "base_origin": "https://example.com",
        "path_template": template("/resource"),
        "query": [],
        "headers": [],
        "auth": {"mode": "none"},
        "body": {"kind": "none"},
        "timeout": {"connect_ms": None, "read_ms": None, "write_ms": None},
        "response": {"mode": "text", "accepted_statuses": [{"from": 200, "to": 299}], "schema": None},
    }


def _python_config() -> dict[str, object]:
    return {
        "source": "def main(value):\n    return {'result': value}\n",
        "input_variables": [],
        "output_schema": {"type": "object"},
        "timeout_ms": None,
    }


def test_complete_condition_aggregate_and_loop_specs_pass_every_pure_compiler_phase() -> None:
    for payload in (condition_aggregate_spec_payload(), loop_spec_payload()):
        result = _validate(payload)
        assert result.is_valid, result.issues
        assert result.issue_codes == ()
        result.raise_for_errors()


def test_validation_issues_are_deterministic_stable_and_raise_as_one_fail_closed_error() -> None:
    payload = condition_aggregate_spec_payload()
    payload["transitions"].append(deepcopy(payload["transitions"][0]))  # type: ignore[index,union-attr]

    first = _validate(payload)
    second = _validate(payload)

    assert first == second
    assert "WORKFLOW_TRANSITION_ID_DUPLICATE" in first.issue_codes
    with pytest.raises(WorkflowValidationError) as raised:
        first.raise_for_errors()
    assert raised.value.issues == first.issues
    assert "WORKFLOW_TRANSITION_ID_DUPLICATE" in str(raised.value)


def test_unique_start_entry_end_reachability_and_termination_are_required() -> None:
    duplicate_start = condition_aggregate_spec_payload()
    duplicate_start["nodes"].append(node(EXTRA_ID, "start", {}))  # type: ignore[union-attr]
    assert "WORKFLOW_START_COUNT_INVALID" in _codes(duplicate_start)

    entry_not_start = condition_aggregate_spec_payload()
    entry_not_start["entry_node_id"] = CONDITION_ID
    assert "WORKFLOW_ENTRY_NOT_START" in _codes(entry_not_start)

    no_end = condition_aggregate_spec_payload()
    no_end["nodes"] = [item for item in no_end["nodes"] if item["id"] != END_ID]  # type: ignore[index]
    no_end["transitions"] = [item for item in no_end["transitions"] if item["target"]["node_id"] != END_ID]  # type: ignore[index]
    assert "WORKFLOW_END_REQUIRED" in _codes(no_end)

    unreachable = condition_aggregate_spec_payload()
    unreachable["nodes"].append(node(EXTRA_ID, "transform", transform_config("orphan")))  # type: ignore[union-attr]
    codes = _codes(unreachable)
    assert "WORKFLOW_NODE_UNREACHABLE" in codes
    assert "WORKFLOW_NODE_CANNOT_REACH_END" in codes


@pytest.mark.parametrize("body_type", ["start", "end"])
def test_start_and_end_are_root_only_and_body_terminals_cannot_pollute_root_completion(
    body_type: str,
) -> None:
    payload = loop_spec_payload()
    body = next(item for item in payload["nodes"] if item["id"] == BODY_ID)
    body["type"] = body_type
    body["config"] = {}
    body["input_bindings"] = {}

    codes = _codes(payload)

    assert "WORKFLOW_LOOP_BODY_TERMINAL_FORBIDDEN" in codes
    assert "WORKFLOW_START_COUNT_INVALID" not in codes
    assert "WORKFLOW_END_REQUIRED" not in codes


def test_control_ports_direction_existence_required_cardinality_and_duplicate_edges_are_validated() -> None:
    unknown_source = condition_aggregate_spec_payload()
    unknown_source["transitions"][0]["source"]["port_id"] = "does-not-exist"  # type: ignore[index]
    assert "WORKFLOW_SOURCE_PORT_UNKNOWN" in _codes(unknown_source)

    unknown_target = condition_aggregate_spec_payload()
    unknown_target["transitions"][0]["target"]["port_id"] = "result"  # type: ignore[index]
    assert "WORKFLOW_TARGET_PORT_UNKNOWN" in _codes(unknown_target)

    duplicate_edge = condition_aggregate_spec_payload()
    duplicate_edge["transitions"].append(  # type: ignore[union-attr]
        transition("duplicate-semantic-edge", START_ID, "next", CONDITION_ID)
    )
    assert "WORKFLOW_CONTROL_EDGE_DUPLICATE" in _codes(duplicate_edge)

    branch_fanout = condition_aggregate_spec_payload()
    branch_fanout["transitions"].append(  # type: ignore[union-attr]
        transition("second-branch-target", CONDITION_ID, "truthy", RIGHT_ID)
    )
    assert "WORKFLOW_SOURCE_PORT_CARDINALITY" in _codes(branch_fanout)

    missing_fallback = condition_aggregate_spec_payload()
    missing_fallback["transitions"] = [item for item in missing_fallback["transitions"] if not (item["source"]["node_id"] == CONDITION_ID and item["source"]["port_id"] == "fallback")]
    assert "WORKFLOW_REQUIRED_ROUTE_MISSING" in _codes(missing_fallback)


def test_binding_references_types_and_ordinary_dominance_are_fail_closed() -> None:
    wrong_type = condition_aggregate_spec_payload()
    left = next(item for item in wrong_type["nodes"] if item["id"] == LEFT_ID)
    left["config"]["input_variables"] = [{"id": "name", "name": "name", "value_type": value_type("string")}]
    left["input_bindings"] = {"name": literal(7)}
    assert "WORKFLOW_BINDING_TYPE_MISMATCH" in _codes(wrong_type)

    unknown_output = condition_aggregate_spec_payload()
    unknown_output["workflow_outputs"][0]["source"]["output_id"] = "missing"  # type: ignore[index]
    assert "WORKFLOW_NODE_OUTPUT_UNKNOWN" in _codes(unknown_output)

    cross_branch = condition_aggregate_spec_payload()
    right = next(item for item in cross_branch["nodes"] if item["id"] == RIGHT_ID)
    right["config"]["input_variables"] = [{"id": "left", "name": "left", "value_type": value_type("string")}]
    right["input_bindings"] = {"left": node_output(LEFT_ID, "result")}
    assert "WORKFLOW_BINDING_NOT_DOMINATED" in _codes(cross_branch)

    future = loop_spec_payload()
    body = next(item for item in future["nodes"] if item["id"] == BODY_ID)
    body["input_bindings"]["value"] = node_output(END_ID, "result")
    codes = _codes(future)
    assert "WORKFLOW_NODE_OUTPUT_UNKNOWN" in codes or "WORKFLOW_BINDING_SCOPE_INVALID" in codes

    scalar_to_collection = condition_aggregate_spec_payload()
    left = next(item for item in scalar_to_collection["nodes"] if item["id"] == LEFT_ID)
    left["config"]["input_variables"] = [
        {
            "id": "items",
            "name": "items",
            "value_type": value_type("json", collection=True),
        }
    ]
    left["input_bindings"] = {"items": literal({"not": "an array"})}
    assert "WORKFLOW_BINDING_TYPE_MISMATCH" in _codes(scalar_to_collection)

    untyped_to_schema_ref = condition_aggregate_spec_payload()
    left = next(item for item in untyped_to_schema_ref["nodes"] if item["id"] == LEFT_ID)
    typed_json = value_type("json")
    typed_json["schema_ref"] = "schema/customer-v1"
    left["config"]["input_variables"] = [{"id": "customer", "name": "customer", "value_type": typed_json}]
    left["input_bindings"] = {"customer": literal({"name": "Ada"})}
    assert "WORKFLOW_BINDING_TYPE_MISMATCH" in _codes(untyped_to_schema_ref)

    nullable_literal = condition_aggregate_spec_payload()
    left = next(item for item in nullable_literal["nodes"] if item["id"] == LEFT_ID)
    left["config"]["input_variables"] = [
        {
            "id": "optional",
            "name": "optional",
            "value_type": value_type("string", nullable=True),
        }
    ]
    left["input_bindings"] = {"optional": literal(None)}
    assert "WORKFLOW_BINDING_TYPE_MISMATCH" not in _codes(nullable_literal)


def test_value_type_cross_field_invariants_are_publish_validated() -> None:
    invalid_messages = condition_aggregate_spec_payload()
    invalid_messages["workflow_outputs"][0]["value_type"] = value_type("messages", collection=False)
    assert "WORKFLOW_VALUE_TYPE_INVALID" in _codes(invalid_messages)

    invalid_schema_ref = condition_aggregate_spec_payload()
    invalid_schema_ref["workflow_inputs"][0]["value_type"]["schema_ref"] = "schema/boolean-v1"
    assert "WORKFLOW_VALUE_TYPE_INVALID" not in _codes(invalid_schema_ref)


def test_transform_result_binding_type_is_derived_from_text_or_json_mode() -> None:
    text_payload = condition_aggregate_spec_payload()
    assert "WORKFLOW_BINDING_TYPE_MISMATCH" not in _codes(text_payload)

    json_to_string = condition_aggregate_spec_payload()
    left = next(item for item in json_to_string["nodes"] if item["id"] == LEFT_ID)
    left["config"] = {
        "input_variables": [],
        "missing_variable": "error",
        "mode": "json",
        "template": {"version": 1, "template": {"value": True}, "bindings": {}},
        "output_schema": {"type": "object"},
    }
    assert "WORKFLOW_BINDING_TYPE_MISMATCH" in _codes(json_to_string)

    all_json = condition_aggregate_spec_payload()
    for node_id in (LEFT_ID, RIGHT_ID):
        transform = next(item for item in all_json["nodes"] if item["id"] == node_id)
        transform["config"] = {
            "input_variables": [],
            "missing_variable": "error",
            "mode": "json",
            "template": {"version": 1, "template": {"value": node_id}, "bindings": {}},
            "output_schema": {"type": "object"},
        }
    aggregate = next(item for item in all_json["nodes"] if item["id"] == AGGREGATE_ID)
    aggregate["config"]["groups"][0]["value_type"] = value_type("json")
    all_json["workflow_outputs"][0]["value_type"] = value_type("json")
    assert "WORKFLOW_BINDING_TYPE_MISMATCH" not in _codes(all_json)


def test_variable_aggregate_requires_exact_same_condition_distinct_mutually_exclusive_branches() -> None:
    literal_candidate = condition_aggregate_spec_payload()
    aggregate = next(item for item in literal_candidate["nodes"] if item["id"] == AGGREGATE_ID)
    aggregate["input_bindings"]["left"] = literal("always present")
    assert "WORKFLOW_AGGREGATE_CANDIDATE_NOT_EXCLUSIVE" in _codes(literal_candidate)

    same_branch = condition_aggregate_spec_payload()
    aggregate = next(item for item in same_branch["nodes"] if item["id"] == AGGREGATE_ID)
    aggregate["input_bindings"]["right"] = node_output(LEFT_ID, "result")
    assert "WORKFLOW_AGGREGATE_BRANCH_AMBIGUOUS" in _codes(same_branch)

    missing_candidate = condition_aggregate_spec_payload()
    aggregate = next(item for item in missing_candidate["nodes"] if item["id"] == AGGREGATE_ID)
    aggregate["input_bindings"].pop("right")
    assert "WORKFLOW_INPUT_BINDING_REQUIRED" in _codes(missing_candidate)


def test_aggregate_candidate_success_must_dominate_its_complete_conditioned_branch() -> None:
    nested_bypass = condition_aggregate_spec_payload()
    inner_condition = node(
        EXTRA_ID,
        "condition",
        {
            "branches": [
                {
                    "id": "inner-hit",
                    "output_port_id": "hit",
                    "label": "Hit",
                    "predicate": predicate(literal(True)),
                }
            ],
            "else_output_port_id": "skip",
        },
    )
    nested_bypass["nodes"].append(inner_condition)
    nested_bypass["transitions"] = [item for item in nested_bypass["transitions"] if item["id"] not in {"t-condition-left"}]
    nested_bypass["transitions"].extend(
        [
            transition("t-outer-inner", CONDITION_ID, "truthy", EXTRA_ID),
            transition("t-inner-left", EXTRA_ID, "hit", LEFT_ID),
            transition("t-inner-skip-aggregate", EXTRA_ID, "skip", AGGREGATE_ID),
        ]
    )

    assert "WORKFLOW_AGGREGATE_BRANCH_AMBIGUOUS" in _codes(nested_bypass)


@pytest.mark.parametrize("inside_loop", [False, True])
def test_aggregate_rejects_candidate_error_outcome_rejoining_the_merge(
    inside_loop: bool,
) -> None:
    payload = loop_condition_aggregate_spec_payload() if inside_loop else condition_aggregate_spec_payload()
    left = next(item for item in payload["nodes"] if item["id"] == LEFT_ID)
    left["execution_policy"]["on_error"] = {
        "mode": "route_error",
        "output_port_id": "error",
    }
    payload["transitions"].append(transition("t-left-error-aggregate", LEFT_ID, "error", AGGREGATE_ID))

    assert "WORKFLOW_AGGREGATE_BRANCH_AMBIGUOUS" in _codes(payload)


@pytest.mark.parametrize("inside_loop", [False, True])
def test_aggregate_rejects_condition_error_outcome_rejoining_the_merge(
    inside_loop: bool,
) -> None:
    payload = loop_condition_aggregate_spec_payload() if inside_loop else condition_aggregate_spec_payload()
    condition = next(item for item in payload["nodes"] if item["id"] == CONDITION_ID)
    condition["execution_policy"]["on_error"] = {
        "mode": "route_error",
        "output_port_id": "error",
    }
    error_target_id = AGGREGATE_ID if inside_loop else LEFT_ID
    payload["transitions"].append(transition("t-condition-error-aggregate", CONDITION_ID, "error", error_target_id))

    assert _codes(payload) == ("WORKFLOW_AGGREGATE_BRANCH_AMBIGUOUS",)


def test_every_workflow_output_must_be_bound_typed_and_available_on_every_end_path() -> None:
    unbound = condition_aggregate_spec_payload()
    unbound["workflow_outputs"][0]["source"] = None  # type: ignore[index]
    assert "WORKFLOW_OUTPUT_UNBOUND" in _codes(unbound)

    nullable_unbound = condition_aggregate_spec_payload()
    nullable_unbound["workflow_outputs"][0]["source"] = None  # type: ignore[index]
    nullable_unbound["workflow_outputs"][0]["value_type"]["nullable"] = True  # type: ignore[index]
    assert "WORKFLOW_OUTPUT_UNBOUND" not in _codes(nullable_unbound)

    defaulted_unbound = condition_aggregate_spec_payload()
    defaulted_unbound["workflow_outputs"][0]["source"] = None  # type: ignore[index]
    defaulted_unbound["workflow_outputs"][0]["default"] = "fallback"  # type: ignore[index]
    assert "WORKFLOW_OUTPUT_UNBOUND" not in _codes(defaulted_unbound)

    one_branch = condition_aggregate_spec_payload()
    one_branch["workflow_outputs"][0]["source"] = node_output(LEFT_ID, "result")  # type: ignore[index]
    assert "WORKFLOW_OUTPUT_NOT_AVAILABLE_ON_ALL_PATHS" in _codes(one_branch)

    nullable_branch = deepcopy(one_branch)
    nullable_branch["workflow_outputs"][0]["value_type"]["nullable"] = True  # type: ignore[index]
    assert "WORKFLOW_OUTPUT_NOT_AVAILABLE_ON_ALL_PATHS" not in _codes(nullable_branch)

    defaulted_branch = deepcopy(one_branch)
    defaulted_branch["workflow_outputs"][0]["default"] = "fallback"  # type: ignore[index]
    assert "WORKFLOW_OUTPUT_NOT_AVAILABLE_ON_ALL_PATHS" not in _codes(defaulted_branch)

    type_mismatch = condition_aggregate_spec_payload()
    type_mismatch["workflow_outputs"][0]["value_type"] = value_type("number")  # type: ignore[index]
    assert "WORKFLOW_OUTPUT_TYPE_MISMATCH" in _codes(type_mismatch)


def test_authored_root_and_body_cycles_cross_scope_and_nested_loops_are_rejected() -> None:
    root_cycle = condition_aggregate_spec_payload()
    root_cycle["transitions"] = [item for item in root_cycle["transitions"] if item["id"] != "t-left-aggregate"]
    root_cycle["transitions"].append(  # type: ignore[union-attr]
        transition("t-left-condition", LEFT_ID, "next", CONDITION_ID)
    )
    assert "WORKFLOW_AUTHORED_CYCLE" in _codes(root_cycle)

    cross_scope = loop_spec_payload()
    cross_scope["transitions"].append(  # type: ignore[union-attr]
        transition("cross-scope", START_ID, "next", BODY_ID)
    )
    assert "WORKFLOW_CROSS_SCOPE_TRANSITION" in _codes(cross_scope)

    nested = loop_spec_payload()
    nested_loop = deepcopy(next(item for item in nested["nodes"] if item["id"] == LOOP_ID))
    nested_loop["id"] = EXTRA_ID
    nested_loop["scope"] = {"kind": "loop_body", "loop_node_id": LOOP_ID}
    nested["nodes"].append(nested_loop)  # type: ignore[union-attr]
    assert "WORKFLOW_NESTED_LOOP_FORBIDDEN" in _codes(nested)

    authored_body_entry = loop_spec_payload()
    authored_body_entry["transitions"].append(transition("authored-body", LOOP_ID, "body", END_ID))
    assert "WORKFLOW_LOOP_BODY_ROUTE_AUTHORED" in _codes(authored_body_entry)


def test_loop_scope_entry_exit_next_bindings_and_loop_variable_scope_are_validated() -> None:
    missing_next = loop_spec_payload()
    loop = next(item for item in missing_next["nodes"] if item["id"] == LOOP_ID)
    loop["input_bindings"].pop("next")
    assert "WORKFLOW_INPUT_BINDING_REQUIRED" in _codes(missing_next)

    wrong_body_owner = loop_spec_payload()
    body = next(item for item in wrong_body_owner["nodes"] if item["id"] == BODY_ID)
    body["scope"]["loop_node_id"] = EXTRA_ID
    codes = _codes(wrong_body_owner)
    assert "WORKFLOW_LOOP_BODY_OWNER_UNKNOWN" in codes

    escaping_body_output = loop_spec_payload()
    escaping_body_output["workflow_outputs"][0]["source"] = node_output(BODY_ID, "result")  # type: ignore[index]
    assert "WORKFLOW_BINDING_SCOPE_INVALID" in _codes(escaping_body_output)

    wrong_loop_variable = loop_spec_payload()
    body = next(item for item in wrong_loop_variable["nodes"] if item["id"] == BODY_ID)
    body["input_bindings"]["value"] = loop_variable(EXTRA_ID, "current")
    assert "WORKFLOW_LOOP_VARIABLE_SCOPE_INVALID" in _codes(wrong_loop_variable)


def test_predicate_and_restricted_template_shapes_are_semantically_strict() -> None:
    empty_predicate = condition_aggregate_spec_payload()
    condition = next(item for item in empty_predicate["nodes"] if item["id"] == CONDITION_ID)
    condition["config"]["branches"][0]["predicate"]["items"] = []
    assert "WORKFLOW_PREDICATE_EMPTY" in _codes(empty_predicate)

    missing_right = condition_aggregate_spec_payload()
    condition = next(item for item in missing_right["nodes"] if item["id"] == CONDITION_ID)
    condition["config"]["branches"][0]["predicate"]["items"][0].pop("right")
    assert "WORKFLOW_PREDICATE_RIGHT_REQUIRED" in _codes(missing_right)

    unexpected_right = condition_aggregate_spec_payload()
    condition = next(item for item in unexpected_right["nodes"] if item["id"] == CONDITION_ID)
    clause = condition["config"]["branches"][0]["predicate"]["items"][0]
    clause["operator"] = "is_null"
    assert "WORKFLOW_PREDICATE_RIGHT_FORBIDDEN" in _codes(unexpected_right)

    unused_json_binding = condition_aggregate_spec_payload()
    left = next(item for item in unused_json_binding["nodes"] if item["id"] == LEFT_ID)
    left["config"] = {
        "input_variables": [],
        "missing_variable": "error",
        "mode": "json",
        "template": {
            "version": 1,
            "template": {"value": {"$binding": "used"}},
            "bindings": {"used": literal(1), "unused": literal(2)},
        },
        "output_schema": {"type": "object"},
    }
    assert "WORKFLOW_JSON_TEMPLATE_BINDING_UNUSED" in _codes(unused_json_binding)


def test_general_input_bindings_are_an_exact_closed_contract_for_every_node_kind() -> None:
    extra = condition_aggregate_spec_payload()
    left = next(item for item in extra["nodes"] if item["id"] == LEFT_ID)
    left["input_bindings"]["undeclared"] = literal("surprise")
    assert "WORKFLOW_INPUT_BINDING_UNKNOWN" in _codes(extra)

    forbidden = condition_aggregate_spec_payload()
    condition = next(item for item in forbidden["nodes"] if item["id"] == CONDITION_ID)
    condition["input_bindings"]["anything"] = literal(True)
    assert "WORKFLOW_INPUT_BINDING_UNKNOWN" in _codes(forbidden)

    missing_context = condition_aggregate_spec_payload()
    left = next(item for item in missing_context["nodes"] if item["id"] == LEFT_ID)
    left["type"] = "llm"
    left["config"] = _llm_config(context_input_ids=["context"])
    left["input_bindings"] = {}
    assert "WORKFLOW_INPUT_BINDING_REQUIRED" in _codes(missing_context)


def test_predicate_operators_are_checked_against_binding_types_and_nullability() -> None:
    ordered_boolean = condition_aggregate_spec_payload()
    condition = next(item for item in ordered_boolean["nodes"] if item["id"] == CONDITION_ID)
    clause = condition["config"]["branches"][0]["predicate"]["items"][0]
    clause["operator"] = "gt"
    clause["right"] = literal(True)
    assert "WORKFLOW_PREDICATE_TYPE_MISMATCH" in _codes(ordered_boolean)

    contains_number = condition_aggregate_spec_payload()
    condition = next(item for item in contains_number["nodes"] if item["id"] == CONDITION_ID)
    clause = condition["config"]["branches"][0]["predicate"]["items"][0]
    clause["left"] = literal("abc")
    clause["operator"] = "contains"
    clause["right"] = literal(1)
    assert "WORKFLOW_PREDICATE_TYPE_MISMATCH" in _codes(contains_number)

    incompatible_equality = condition_aggregate_spec_payload()
    condition = next(item for item in incompatible_equality["nodes"] if item["id"] == CONDITION_ID)
    clause = condition["config"]["branches"][0]["predicate"]["items"][0]
    clause["right"] = literal("true")
    assert "WORKFLOW_PREDICATE_TYPE_MISMATCH" in _codes(incompatible_equality)

    non_nullable_null_test = condition_aggregate_spec_payload()
    condition = next(item for item in non_nullable_null_test["nodes"] if item["id"] == CONDITION_ID)
    clause = condition["config"]["branches"][0]["predicate"]["items"][0]
    clause["operator"] = "is_null"
    clause.pop("right")
    assert "WORKFLOW_PREDICATE_NULLABILITY_INVALID" in _codes(non_nullable_null_test)

    nullable_null_test = condition_aggregate_spec_payload()
    nullable_null_test["workflow_inputs"][0]["value_type"]["nullable"] = True
    condition = next(item for item in nullable_null_test["nodes"] if item["id"] == CONDITION_ID)
    clause = condition["config"]["branches"][0]["predicate"]["items"][0]
    clause["operator"] = "is_null"
    clause.pop("right")
    assert "WORKFLOW_PREDICATE_NULLABILITY_INVALID" not in _codes(nullable_null_test)


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (
            lambda payload: payload["workflow_outputs"].append(deepcopy(payload["workflow_outputs"][0])),
            "WORKFLOW_OUTPUT_ID_DUPLICATE",
        ),
        (
            lambda payload: next(item for item in payload["nodes"] if item["id"] == CONDITION_ID)["config"]["branches"].append(
                {
                    "id": "branch-true",
                    "output_port_id": "another",
                    "label": "Another",
                    "predicate": predicate(),
                }
            ),
            "WORKFLOW_CONDITION_BRANCH_ID_DUPLICATE",
        ),
    ],
)
def test_publish_identity_lists_cannot_silently_overwrite(
    mutate,
    expected_code: str,
) -> None:
    payload = condition_aggregate_spec_payload()
    mutate(payload)
    assert expected_code in _codes(payload)


def test_llm_transform_python_and_http_internal_ids_and_names_are_unique() -> None:
    duplicate_context = condition_aggregate_spec_payload()
    left = next(item for item in duplicate_context["nodes"] if item["id"] == LEFT_ID)
    left["type"] = "llm"
    left["config"] = _llm_config(context_input_ids=["context", "context"])
    left["input_bindings"] = {"context": literal("value")}
    assert "WORKFLOW_LLM_CONTEXT_INPUT_ID_DUPLICATE" in _codes(duplicate_context)

    duplicate_transform = condition_aggregate_spec_payload()
    left = next(item for item in duplicate_transform["nodes"] if item["id"] == LEFT_ID)
    left["config"]["input_variables"] = [
        {"id": "one", "name": "same", "value_type": value_type()},
        {"id": "two", "name": "same", "value_type": value_type()},
    ]
    left["input_bindings"] = {"one": literal("1"), "two": literal("2")}
    assert "WORKFLOW_TRANSFORM_INPUT_NAME_DUPLICATE" in _codes(duplicate_transform)

    duplicate_python = condition_aggregate_spec_payload()
    left = next(item for item in duplicate_python["nodes"] if item["id"] == LEFT_ID)
    left["type"] = "python_code"
    left["config"] = _python_config()
    left["config"]["input_variables"] = [
        {"id": EXTRA_ID, "name": "one", "value_type": value_type()},
        {"id": EXTRA_ID, "name": "two", "value_type": value_type()},
    ]
    left["input_bindings"] = {EXTRA_ID: literal("1")}
    assert "WORKFLOW_PYTHON_INPUT_ID_DUPLICATE" in _codes(duplicate_python)

    duplicate_http = condition_aggregate_spec_payload()
    left = next(item for item in duplicate_http["nodes"] if item["id"] == LEFT_ID)
    left["type"] = "http_request"
    left["config"] = _http_config()
    left["config"]["headers"] = [
        {"id": "header-1", "name": "X-Trace", "value": literal("one")},
        {"id": "header-2", "name": "x-trace", "value": literal("two")},
    ]
    left["input_bindings"] = {}
    assert "WORKFLOW_HTTP_HEADER_NAME_DUPLICATE" in _codes(duplicate_http)


def test_llm_mode_and_structured_output_are_internally_consistent() -> None:
    empty_chat = condition_aggregate_spec_payload()
    left = next(item for item in empty_chat["nodes"] if item["id"] == LEFT_ID)
    left["type"] = "llm"
    left["config"] = _llm_config()
    left["config"]["messages"] = []
    assert "WORKFLOW_LLM_MESSAGE_REQUIRED" in _codes(empty_chat)

    duplicate_messages = condition_aggregate_spec_payload()
    left = next(item for item in duplicate_messages["nodes"] if item["id"] == LEFT_ID)
    left["type"] = "llm"
    left["config"] = _llm_config()
    left["config"]["messages"].append(deepcopy(left["config"]["messages"][0]))
    assert "WORKFLOW_LLM_MESSAGE_ID_DUPLICATE" in _codes(duplicate_messages)

    bad_completion = condition_aggregate_spec_payload()
    left = next(item for item in bad_completion["nodes"] if item["id"] == LEFT_ID)
    left["type"] = "llm"
    left["config"] = _llm_config()
    left["config"]["mode"] = "completion"
    left["config"]["messages"][0]["role"] = "system"
    assert "WORKFLOW_LLM_COMPLETION_SHAPE_INVALID" in _codes(bad_completion)

    bad_structured = condition_aggregate_spec_payload()
    left = next(item for item in bad_structured["nodes"] if item["id"] == LEFT_ID)
    left["type"] = "llm"
    left["config"] = _llm_config()
    left["config"]["structured_output"] = {"enabled": True, "schema": None}
    assert "WORKFLOW_LLM_STRUCTURED_OUTPUT_INVALID" in _codes(bad_structured)


def test_execution_policy_is_compatible_with_registry_ports_and_retry_semantics() -> None:
    missing_error_route = condition_aggregate_spec_payload()
    left = next(item for item in missing_error_route["nodes"] if item["id"] == LEFT_ID)
    left["execution_policy"]["on_error"] = {"mode": "route_error", "output_port_id": "error"}
    assert "WORKFLOW_ERROR_ROUTE_MISSING" in _codes(missing_error_route)

    unsupported_error_route = condition_aggregate_spec_payload()
    start = next(item for item in unsupported_error_route["nodes"] if item["id"] == START_ID)
    start["execution_policy"]["on_error"] = {"mode": "route_error", "output_port_id": "error"}
    assert "WORKFLOW_ERROR_ROUTE_UNSUPPORTED" in _codes(unsupported_error_route)

    typed_default = condition_aggregate_spec_payload()
    left = next(item for item in typed_default["nodes"] if item["id"] == LEFT_ID)
    left["execution_policy"]["on_error"] = {"mode": "continue_with_typed_default", "value": "fallback"}
    assert "WORKFLOW_TYPED_DEFAULT_UNSUPPORTED" in _codes(typed_default)

    pure_retry = condition_aggregate_spec_payload()
    left = next(item for item in pure_retry["nodes"] if item["id"] == LEFT_ID)
    left["execution_policy"]["retry"] = {"mode": "bounded", "max_attempts": 2, "backoff_ms": 10}
    assert "WORKFLOW_RETRY_UNSUPPORTED" in _codes(pure_retry)

    write_retry = condition_aggregate_spec_payload()
    left = next(item for item in write_retry["nodes"] if item["id"] == LEFT_ID)
    left["type"] = "http_request"
    left["config"] = _http_config(method="POST")
    left["execution_policy"]["retry"] = {"mode": "bounded", "max_attempts": 2, "backoff_ms": 10}
    assert "WORKFLOW_HTTP_WRITE_RETRY_UNSUPPORTED" in _codes(write_retry)


@pytest.mark.parametrize(
    "node_type",
    ["transform", "llm", "python_code", "http_request", "loop", "condition"],
)
def test_authored_error_edges_require_route_error_for_every_fallible_node_kind(
    node_type: str,
) -> None:
    if node_type == "loop":
        payload = loop_spec_payload()
        source_id = LOOP_ID
    else:
        payload = condition_aggregate_spec_payload()
        source_id = CONDITION_ID if node_type == "condition" else LEFT_ID
        source = next(item for item in payload["nodes"] if item["id"] == source_id)
        if node_type == "llm":
            source["type"] = "llm"
            source["config"] = _llm_config()
            source["input_bindings"] = {}
        elif node_type == "python_code":
            source["type"] = "python_code"
            source["config"] = _python_config()
            source["input_bindings"] = {}
        elif node_type == "http_request":
            source["type"] = "http_request"
            source["config"] = _http_config()
            source["input_bindings"] = {}
            for transition_value in payload["transitions"]:
                if transition_value["source"]["node_id"] == source_id and transition_value["source"]["port_id"] == "next":
                    transition_value["source"]["port_id"] = "success"
    payload["transitions"].append(transition(f"t-{node_type}-unexpected-error", source_id, "error", END_ID))

    assert "WORKFLOW_ERROR_ROUTE_UNEXPECTED" in _codes(payload)


def test_error_outcome_does_not_make_success_data_available_at_a_rejoined_end() -> None:
    payload = {
        "schema_version": 1,
        "entry_node_id": START_ID,
        "nodes": [
            node(START_ID, "start", {}),
            node(LEFT_ID, "transform", transform_config("value")),
            node(END_ID, "end", {}),
        ],
        "transitions": [
            transition("t-start-transform", START_ID, "next", LEFT_ID),
            transition("t-transform-next", LEFT_ID, "next", END_ID),
            transition("t-transform-error", LEFT_ID, "error", END_ID),
        ],
        "workflow_inputs": [],
        "workflow_outputs": [
            {
                "id": "result",
                "name": "result",
                "description": None,
                "value_type": value_type(),
                "source": node_output(LEFT_ID, "result"),
            }
        ],
        "credential_slots": [],
    }
    transform_node = next(item for item in payload["nodes"] if item["id"] == LEFT_ID)
    transform_node["execution_policy"]["on_error"] = {
        "mode": "route_error",
        "output_port_id": "error",
    }

    assert "WORKFLOW_OUTPUT_NOT_AVAILABLE_ON_ALL_PATHS" in _codes(payload)


def test_input_and_output_defaults_and_input_constraints_are_type_checked() -> None:
    wrong_input_default = condition_aggregate_spec_payload()
    wrong_input_default["workflow_inputs"][0]["default"] = "yes"
    assert "WORKFLOW_INPUT_DEFAULT_TYPE_MISMATCH" in _codes(wrong_input_default)

    null_input_default = condition_aggregate_spec_payload()
    null_input_default["workflow_inputs"][0]["default"] = None
    assert "WORKFLOW_INPUT_DEFAULT_TYPE_MISMATCH" in _codes(null_input_default)

    nullable_default = condition_aggregate_spec_payload()
    nullable_default["workflow_inputs"][0]["value_type"]["nullable"] = True
    nullable_default["workflow_inputs"][0]["default"] = None
    assert "WORKFLOW_INPUT_DEFAULT_TYPE_MISMATCH" not in _codes(nullable_default)

    json_collection_default = condition_aggregate_spec_payload()
    json_collection_default["workflow_inputs"][0]["value_type"] = value_type(
        "json",
        collection=True,
    )
    json_collection_default["workflow_inputs"][0]["default"] = [None, [1], {"ok": True}]
    assert "WORKFLOW_INPUT_DEFAULT_TYPE_MISMATCH" not in _codes(json_collection_default)

    wrong_constraint_kind = condition_aggregate_spec_payload()
    wrong_constraint_kind["workflow_inputs"][0]["constraints"] = {"kind": "string", "min_length": 1}
    assert "WORKFLOW_INPUT_CONSTRAINT_TYPE_MISMATCH" in _codes(wrong_constraint_kind)

    enum_default = condition_aggregate_spec_payload()
    enum_default["workflow_inputs"][0]["constraints"] = {"kind": "enum", "options": [False]}
    enum_default["workflow_inputs"][0]["default"] = True
    assert "WORKFLOW_INPUT_DEFAULT_CONSTRAINT_MISMATCH" in _codes(enum_default)

    wrong_output_default = condition_aggregate_spec_payload()
    wrong_output_default["workflow_outputs"][0]["default"] = 1
    assert "WORKFLOW_OUTPUT_DEFAULT_TYPE_MISMATCH" in _codes(wrong_output_default)

    schema_ref_default = condition_aggregate_spec_payload()
    schema_ref_default["workflow_inputs"][0]["value_type"]["schema_ref"] = "schema/flag-v1"
    schema_ref_default["workflow_inputs"][0]["default"] = True
    assert "WORKFLOW_DEFAULT_SCHEMA_UNVERIFIABLE" in _codes(schema_ref_default)


@pytest.mark.parametrize(
    "pattern",
    [
        "a" * 1_024,
        "(?=a)a",
        r"(a)\1",
        "(a+)+$",
    ],
)
def test_input_regex_patterns_fail_closed_before_any_python_regex_execution(pattern: str) -> None:
    payload = condition_aggregate_spec_payload()
    declaration = payload["workflow_inputs"][0]
    declaration["value_type"] = value_type("string")
    declaration["default"] = "a" * 100_000 + "!"
    declaration["constraints"] = {"kind": "string", "pattern": pattern}
    condition = next(item for item in payload["nodes"] if item["id"] == CONDITION_ID)
    condition["config"]["branches"][0]["predicate"]["items"][0]["right"] = literal("a")

    assert "WORKFLOW_INPUT_PATTERN_UNSUPPORTED" in _codes(payload)
    assert "re.search(" not in inspect.getsource(workflow_validation)


def test_http_origin_rejects_all_userinfo_and_invalid_ports() -> None:
    for origin in ("https://:password@example.com", "https://example.com:99999"):
        payload = condition_aggregate_spec_payload()
        left = next(item for item in payload["nodes"] if item["id"] == LEFT_ID)
        left["type"] = "http_request"
        left["config"] = _http_config()
        left["config"]["base_origin"] = origin
        assert "WORKFLOW_HTTP_ORIGIN_INVALID" in _codes(payload)


@pytest.mark.parametrize(
    ("schema", "expected_code"),
    [
        ({"type": "object", "$ref": "#/$defs/Anything"}, "WORKFLOW_JSON_SCHEMA_UNSUPPORTED_KEYWORD"),
        ({"type": "string", "pattern": "(a+)+$"}, "WORKFLOW_JSON_SCHEMA_UNSUPPORTED_KEYWORD"),
        ({"type": ["object", "array"]}, "WORKFLOW_JSON_SCHEMA_TOP_LEVEL_TYPE_INVALID"),
        ({"type": "null"}, "WORKFLOW_JSON_SCHEMA_TOP_LEVEL_TYPE_INVALID"),
        (
            {
                "type": "object",
                "properties": {f"field_{index}": {"type": "string"} for index in range(257)},
            },
            "WORKFLOW_JSON_SCHEMA_LIMIT_EXCEEDED",
        ),
    ],
)
def test_output_json_schemas_use_one_strict_bounded_keyword_subset(
    schema: dict[str, object],
    expected_code: str,
) -> None:
    payload = condition_aggregate_spec_payload()
    left = next(item for item in payload["nodes"] if item["id"] == LEFT_ID)
    left["config"] = {
        "input_variables": [],
        "missing_variable": "error",
        "mode": "json",
        "template": {"version": 1, "template": {}, "bindings": {}},
        "output_schema": schema,
    }

    assert expected_code in _codes(payload)


def test_four_schema_output_nodes_share_precise_value_type_and_compact_schema_identity_derivation() -> None:
    cases: list[tuple[str, dict[str, object], str, bool, bool]] = []

    transform_payload = condition_aggregate_spec_payload()
    transform = next(item for item in transform_payload["nodes"] if item["id"] == LEFT_ID)
    transform["config"] = {
        "input_variables": [],
        "missing_variable": "error",
        "mode": "json",
        "template": {"version": 1, "template": [], "bindings": {}},
        "output_schema": {"type": "array", "items": {"type": "string"}},
    }
    cases.append(("transform", transform_payload, "json", True, False))

    http_payload = condition_aggregate_spec_payload()
    http = next(item for item in http_payload["nodes"] if item["id"] == LEFT_ID)
    http["type"] = "http_request"
    http["config"] = _http_config()
    http["config"]["response"] = {
        "mode": "json",
        "accepted_statuses": [{"from": 200, "to": 299}],
        "schema": {"type": ["string", "null"]},
    }
    http["input_bindings"] = {}
    for edge in http_payload["transitions"]:
        if edge["source"]["node_id"] == LEFT_ID and edge["source"]["port_id"] == "next":
            edge["source"]["port_id"] = "success"
    cases.append(("http_request", http_payload, "string", False, True))

    python_payload = condition_aggregate_spec_payload()
    python_node = next(item for item in python_payload["nodes"] if item["id"] == LEFT_ID)
    python_node["type"] = "python_code"
    python_node["config"] = _python_config()
    python_node["input_bindings"] = {}
    cases.append(("python_code", python_payload, "json", False, False))

    llm_payload = condition_aggregate_spec_payload()
    llm = next(item for item in llm_payload["nodes"] if item["id"] == LEFT_ID)
    llm["type"] = "llm"
    llm["config"] = _llm_config()
    llm["config"]["structured_output"] = {
        "enabled": True,
        "schema": {"type": "object", "properties": {"answer": {"type": "string"}}},
    }
    llm["input_bindings"] = {}
    cases.append(("llm", llm_payload, "json", False, False))

    for node_type, payload, kind, collection, nullable in cases:
        spec = WorkflowSpecV1.model_validate(payload)
        target = next(item for item in spec.nodes if item.type == node_type)
        _inputs, outputs = resolve_node_ports(spec, target)
        port_id = "body" if node_type == "http_request" else "result"
        value_type_value = next(port for port in outputs if port.id == port_id).value_type
        assert value_type_value is not None
        assert (
            value_type_value.kind,
            value_type_value.collection,
            value_type_value.nullable,
        ) == (kind, collection, nullable)
        assert value_type_value.schema_ref is not None
        assert value_type_value.schema_ref.startswith(INLINE_SCHEMA_REF_PREFIX)
        assert len(value_type_value.schema_ref) == len(INLINE_SCHEMA_REF_PREFIX) + 64


def test_python_and_llm_require_non_null_object_output_schemas() -> None:
    python_payload = condition_aggregate_spec_payload()
    python_node = next(item for item in python_payload["nodes"] if item["id"] == LEFT_ID)
    python_node["type"] = "python_code"
    python_node["config"] = _python_config()
    python_node["config"]["output_schema"] = {"type": "array", "items": {"type": "string"}}
    python_node["input_bindings"] = {}
    assert "WORKFLOW_JSON_SCHEMA_TOP_LEVEL_TYPE_INVALID" in _codes(python_payload)

    llm_payload = condition_aggregate_spec_payload()
    llm = next(item for item in llm_payload["nodes"] if item["id"] == LEFT_ID)
    llm["type"] = "llm"
    llm["config"] = _llm_config()
    llm["config"]["structured_output"] = {
        "enabled": True,
        "schema": {"type": ["object", "null"]},
    }
    llm["input_bindings"] = {}
    assert "WORKFLOW_JSON_SCHEMA_TOP_LEVEL_TYPE_INVALID" in _codes(llm_payload)


def test_json_pointer_type_is_derived_from_the_frozen_node_schema() -> None:
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
                    "template": {"version": 1, "template": {"name": "Ada"}, "bindings": {}},
                    "output_schema": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                },
            ),
            node(
                RIGHT_ID,
                "transform",
                transform_config(
                    "ok",
                    input_variables=[{"id": "name", "name": "name", "value_type": value_type("string")}],
                ),
                bindings={"name": node_output(LEFT_ID, "result", path="/name")},
            ),
            node(END_ID, "end", {}),
        ],
        "transitions": [
            transition("t-start-left", START_ID, "next", LEFT_ID),
            transition("t-left-right", LEFT_ID, "next", RIGHT_ID),
            transition("t-right-end", RIGHT_ID, "next", END_ID),
        ],
        "workflow_inputs": [],
        "workflow_outputs": [
            {
                "id": "result",
                "name": "result",
                "description": None,
                "value_type": value_type("string"),
                "source": node_output(RIGHT_ID, "result"),
            }
        ],
        "credential_slots": [],
    }
    assert _codes(payload) == ()

    optional = deepcopy(payload)
    optional_left = next(item for item in optional["nodes"] if item["id"] == LEFT_ID)
    optional_left["config"]["output_schema"].pop("required")
    assert "WORKFLOW_BINDING_TYPE_MISMATCH" in _codes(optional)

    unknown = deepcopy(payload)
    unknown_right = next(item for item in unknown["nodes"] if item["id"] == RIGHT_ID)
    unknown_right["input_bindings"]["name"]["path"] = "/missing"
    assert "WORKFLOW_NODE_OUTPUT_PATH_INVALID" in _codes(unknown)


def test_schema_identity_is_canonical_compact_and_changes_with_schema_semantics() -> None:
    left = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "number"}},
    }
    reordered = {
        "properties": {"b": {"type": "number"}, "a": {"type": "string"}},
        "type": "object",
    }
    different = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "boolean"}},
    }

    assert inline_json_schema_ref(left) == inline_json_schema_ref(reordered)
    assert inline_json_schema_ref(left) != inline_json_schema_ref(different)
    assert inline_json_schema_ref({"type": "array", "items": {"type": "string"}}) == ("inline-json-schema-v1:sha256:681004346c78d5f384b654c986b4eda8fea28010db3be7da70b19aa0080f3ff9")
    assert len(inline_json_schema_ref(left)) == len(INLINE_SCHEMA_REF_PREFIX) + 64


def test_duplicate_detection_has_a_linear_source_contract_and_deterministic_near_limit_result() -> None:
    source = inspect.getsource(workflow_validation)
    assert ".count(" not in source

    payload = condition_aggregate_spec_payload()
    original = deepcopy(payload["transitions"][0])
    payload["transitions"].extend(deepcopy(original) for _ in range(1_000))
    first = _validate(payload)
    second = _validate(payload)
    assert first == second
    assert "WORKFLOW_TRANSITION_ID_DUPLICATE" in first.issue_codes


def test_graph_loop_and_activation_budgets_are_calculated_before_runtime() -> None:
    payload = loop_spec_payload(max_iterations=3)
    limits = WorkflowCompilationLimits.permissive().replace(
        max_nodes=3,
        max_edges=1,
        max_depth=2,
        max_total_steps=5,
        max_loops=0,
        max_loop_body_nodes=0,
        max_loop_body_edges=0,
        max_loop_iterations=2,
        max_total_iterations=2,
        max_total_activations=5,
    )
    codes = _codes(payload, limits=limits)

    assert "WORKFLOW_NODE_LIMIT_EXCEEDED" in codes
    assert "WORKFLOW_EDGE_LIMIT_EXCEEDED" in codes
    assert "WORKFLOW_DEPTH_LIMIT_EXCEEDED" in codes
    assert "WORKFLOW_LOOP_COUNT_LIMIT_EXCEEDED" in codes
    assert "WORKFLOW_LOOP_ITERATION_LIMIT_EXCEEDED" in codes
    assert "WORKFLOW_TOTAL_ITERATION_LIMIT_EXCEEDED" in codes
    assert "WORKFLOW_TOTAL_STEP_LIMIT_EXCEEDED" in codes
    assert "WORKFLOW_TOTAL_ACTIVATION_LIMIT_EXCEEDED" in codes


def test_fan_out_parallelism_and_recursion_budgets_are_static_and_enforced() -> None:
    payload = loop_spec_payload(max_iterations=3)
    extra_id = "00000000-0000-4000-8000-000000000010"
    payload["nodes"].insert(1, node(extra_id, "transform", transform_config("parallel")))
    payload["transitions"].insert(
        1,
        transition("t-start-parallel", START_ID, "next", extra_id),
    )
    payload["transitions"].append(
        transition("t-parallel-end", extra_id, "next", END_ID),
    )
    limits = WorkflowCompilationLimits.permissive().replace(
        max_fan_out=1,
        max_parallelism=1,
        max_recursion_depth=2,
    )

    result = _validate(payload, limits=limits)

    assert result.metrics.max_fan_out == 2
    assert result.metrics.max_parallelism >= 2
    assert result.metrics.recursion_depth > result.metrics.depth
    assert "WORKFLOW_FAN_OUT_LIMIT_EXCEEDED" in result.issue_codes
    assert "WORKFLOW_PARALLELISM_LIMIT_EXCEEDED" in result.issue_codes
    assert "WORKFLOW_RECURSION_DEPTH_LIMIT_EXCEEDED" in result.issue_codes


def test_canvas_document_matches_spec_identity_edges_and_loop_parent_projection() -> None:
    spec = WorkflowSpecV1.model_validate(loop_spec_payload())
    canvas = CanvasDocumentV1.model_validate(
        {
            "schema_version": 1,
            "node_layouts": [
                {"node_id": START_ID, "position": {"x": 0, "y": 0}},
                {"node_id": LOOP_ID, "position": {"x": 100, "y": 0}},
                {
                    "node_id": BODY_ID,
                    "position": {"x": 10, "y": 10},
                    "parent_node_id": LOOP_ID,
                },
                {"node_id": END_ID, "position": {"x": 300, "y": 0}},
            ],
            "edge_layouts": [
                {"edge_id": "t-start-loop", "routing": "smoothstep"},
                {"edge_id": "t-loop-end", "routing": "bezier"},
            ],
        }
    )
    assert validate_canvas_document(spec, canvas).is_valid

    wrong = canvas.model_copy(deep=True)
    wrong.node_layouts[2].parent_node_id = START_ID
    wrong.node_layouts.append(wrong.node_layouts[0])
    issues = validate_canvas_document(spec, wrong)
    assert "WORKFLOW_CANVAS_NODE_LAYOUT_DUPLICATE" in issues.issue_codes
    assert "WORKFLOW_CANVAS_LOOP_PARENT_MISMATCH" in issues.issue_codes
