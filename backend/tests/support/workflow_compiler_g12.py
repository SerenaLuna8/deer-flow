"""Deterministic authored Workflow fixtures for the G12 compiler tests."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

START_ID = "00000000-0000-4000-8000-000000000001"
CONDITION_ID = "00000000-0000-4000-8000-000000000002"
LEFT_ID = "00000000-0000-4000-8000-000000000003"
RIGHT_ID = "00000000-0000-4000-8000-000000000004"
AGGREGATE_ID = "00000000-0000-4000-8000-000000000005"
LOOP_ID = "00000000-0000-4000-8000-000000000006"
BODY_ID = "00000000-0000-4000-8000-000000000007"
END_ID = "00000000-0000-4000-8000-000000000008"
EXTRA_ID = "00000000-0000-4000-8000-000000000009"


def value_type(
    kind: str = "string",
    *,
    collection: bool = False,
    nullable: bool = False,
) -> dict[str, object]:
    return {
        "kind": kind,
        "collection": collection,
        "nullable": nullable,
    }


def literal(value: object) -> dict[str, object]:
    return {"kind": "literal", "value": value}


def workflow_input(input_id: str) -> dict[str, object]:
    return {"kind": "workflow_input", "input_id": input_id}


def node_output(
    node_id: str,
    output_id: str,
    *,
    path: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "kind": "node_output",
        "node_id": node_id,
        "output_id": output_id,
    }
    if path is not None:
        result["path"] = path
    return result


def loop_variable(loop_node_id: str, variable_id: str) -> dict[str, object]:
    return {
        "kind": "loop_variable",
        "loop_node_id": loop_node_id,
        "variable_id": variable_id,
    }


def template(*segments: str | dict[str, object]) -> dict[str, object]:
    return {
        "version": 1,
        "segments": [{"kind": "text", "value": segment} if isinstance(segment, str) else {"kind": "binding", "value": segment} for segment in segments],
    }


def predicate(
    left: dict[str, object] | None = None,
    *,
    operator: str = "eq",
    right: dict[str, object] | None = None,
) -> dict[str, object]:
    clause: dict[str, object] = {
        "left": left or literal(True),
        "operator": operator,
    }
    if operator not in {"is_null", "is_not_null"}:
        clause["right"] = right or literal(True)
    return {"op": "and", "items": [clause]}


def execution_policy() -> dict[str, object]:
    return {
        "retry": {"mode": "none"},
        "on_error": {"mode": "fail_workflow"},
    }


def node(
    node_id: str,
    node_type: str,
    config: dict[str, object],
    *,
    bindings: dict[str, dict[str, object] | None] | None = None,
    scope: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "id": node_id,
        "type": node_type,
        "type_version": 1,
        "scope": scope or {"kind": "root"},
        "custom_label": None,
        "description": None,
        "input_bindings": bindings or {},
        "execution_policy": execution_policy(),
        "config": config,
    }


def transition(
    transition_id: str,
    source_node_id: str,
    source_port_id: str,
    target_node_id: str,
    target_port_id: str = "in",
) -> dict[str, object]:
    return {
        "id": transition_id,
        "source": {"node_id": source_node_id, "port_id": source_port_id},
        "target": {"node_id": target_node_id, "port_id": target_port_id},
    }


def transform_config(
    *segments: str | dict[str, object],
    input_variables: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "input_variables": input_variables or [],
        "missing_variable": "error",
        "mode": "text",
        "template": template(*segments),
        "output_schema": None,
    }


def condition_aggregate_spec_payload() -> dict[str, Any]:
    """A complete mutually-exclusive branch graph with a legal aggregate join."""

    return {
        "schema_version": 1,
        "entry_node_id": START_ID,
        "nodes": [
            node(START_ID, "start", {}),
            node(
                CONDITION_ID,
                "condition",
                {
                    "branches": [
                        {
                            "id": "branch-true",
                            "output_port_id": "truthy",
                            "label": "Truthy",
                            "predicate": predicate(workflow_input("flag")),
                        }
                    ],
                    "else_output_port_id": "fallback",
                },
            ),
            node(LEFT_ID, "transform", transform_config("left")),
            node(RIGHT_ID, "transform", transform_config("right")),
            node(
                AGGREGATE_ID,
                "variable_aggregate",
                {
                    "strategy": "exclusive_branch",
                    "groups": [
                        {
                            "id": "result",
                            "name": "result",
                            "value_type": value_type(),
                            "candidate_input_ids": ["left", "right"],
                        }
                    ],
                },
                bindings={
                    "left": node_output(LEFT_ID, "result"),
                    "right": node_output(RIGHT_ID, "result"),
                },
            ),
            node(END_ID, "end", {}),
        ],
        "transitions": [
            transition("t-start-condition", START_ID, "next", CONDITION_ID),
            transition("t-condition-left", CONDITION_ID, "truthy", LEFT_ID),
            transition("t-condition-right", CONDITION_ID, "fallback", RIGHT_ID),
            transition("t-left-aggregate", LEFT_ID, "next", AGGREGATE_ID),
            transition("t-right-aggregate", RIGHT_ID, "next", AGGREGATE_ID),
            transition("t-aggregate-end", AGGREGATE_ID, "next", END_ID),
        ],
        "workflow_inputs": [
            {
                "id": "flag",
                "name": "flag",
                "label": None,
                "description": None,
                "value_type": value_type("boolean"),
                "required": True,
                "constraints": {"kind": "none"},
            }
        ],
        "workflow_outputs": [
            {
                "id": "result",
                "name": "result",
                "description": None,
                "value_type": value_type(),
                "source": node_output(AGGREGATE_ID, "result"),
            }
        ],
        "credential_slots": [],
    }


def loop_spec_payload(*, max_iterations: int = 3) -> dict[str, Any]:
    """A complete one-node bounded do-until body."""

    current = loop_variable(LOOP_ID, "current")
    return {
        "schema_version": 1,
        "entry_node_id": START_ID,
        "nodes": [
            node(START_ID, "start", {}),
            node(
                LOOP_ID,
                "loop",
                {
                    "mode": "do_until",
                    "body_entry_node_id": BODY_ID,
                    "body_exit_node_id": BODY_ID,
                    "max_iterations": max_iterations,
                    "termination_condition": predicate(
                        current,
                        right=literal("done"),
                    ),
                    "variables": [
                        {
                            "id": "current",
                            "name": "current",
                            "value_type": value_type(),
                            "initial_input_id": "initial",
                            "next_input_id": "next",
                            "output_port_id": "current",
                        }
                    ],
                },
                bindings={
                    "initial": literal("start"),
                    "next": node_output(BODY_ID, "result"),
                },
            ),
            node(
                BODY_ID,
                "transform",
                transform_config(
                    current,
                    input_variables=[
                        {
                            "id": "value",
                            "name": "value",
                            "value_type": value_type(),
                        }
                    ],
                ),
                bindings={"value": current},
                scope={"kind": "loop_body", "loop_node_id": LOOP_ID},
            ),
            node(END_ID, "end", {}),
        ],
        "transitions": [
            transition("t-start-loop", START_ID, "next", LOOP_ID),
            transition("t-loop-end", LOOP_ID, "next", END_ID),
        ],
        "workflow_inputs": [],
        "workflow_outputs": [
            {
                "id": "current",
                "name": "current",
                "description": None,
                "value_type": value_type(),
                "source": node_output(LOOP_ID, "current"),
            }
        ],
        "credential_slots": [],
    }


def loop_condition_aggregate_spec_payload(*, max_iterations: int = 3) -> dict[str, Any]:
    """A Loop body whose next value joins one body-local Condition."""

    current = loop_variable(LOOP_ID, "current")
    body_scope = {"kind": "loop_body", "loop_node_id": LOOP_ID}
    return {
        "schema_version": 1,
        "entry_node_id": START_ID,
        "nodes": [
            node(START_ID, "start", {}),
            node(
                LOOP_ID,
                "loop",
                {
                    "mode": "do_until",
                    "body_entry_node_id": CONDITION_ID,
                    "body_exit_node_id": AGGREGATE_ID,
                    "max_iterations": max_iterations,
                    "termination_condition": predicate(current, right=literal("done")),
                    "variables": [
                        {
                            "id": "current",
                            "name": "current",
                            "value_type": value_type(),
                            "initial_input_id": "initial",
                            "next_input_id": "next",
                            "output_port_id": "current",
                        }
                    ],
                },
                bindings={
                    "initial": literal("start"),
                    "next": node_output(AGGREGATE_ID, "result"),
                },
            ),
            node(
                CONDITION_ID,
                "condition",
                {
                    "branches": [
                        {
                            "id": "branch-done",
                            "output_port_id": "done",
                            "label": "Done",
                            "predicate": predicate(current, right=literal("start")),
                        }
                    ],
                    "else_output_port_id": "pending",
                },
                scope=body_scope,
            ),
            node(LEFT_ID, "transform", transform_config("done"), scope=body_scope),
            node(RIGHT_ID, "transform", transform_config("pending"), scope=body_scope),
            node(
                AGGREGATE_ID,
                "variable_aggregate",
                {
                    "strategy": "exclusive_branch",
                    "groups": [
                        {
                            "id": "result",
                            "name": "result",
                            "value_type": value_type(),
                            "candidate_input_ids": ["done", "pending"],
                        }
                    ],
                },
                bindings={
                    "done": node_output(LEFT_ID, "result"),
                    "pending": node_output(RIGHT_ID, "result"),
                },
                scope=body_scope,
            ),
            node(END_ID, "end", {}),
        ],
        "transitions": [
            transition("t-start-loop", START_ID, "next", LOOP_ID),
            transition("t-loop-end", LOOP_ID, "next", END_ID),
            transition("t-condition-left", CONDITION_ID, "done", LEFT_ID),
            transition("t-condition-right", CONDITION_ID, "pending", RIGHT_ID),
            transition("t-left-aggregate", LEFT_ID, "next", AGGREGATE_ID),
            transition("t-right-aggregate", RIGHT_ID, "next", AGGREGATE_ID),
        ],
        "workflow_inputs": [],
        "workflow_outputs": [
            {
                "id": "current",
                "name": "current",
                "description": None,
                "value_type": value_type(),
                "source": node_output(LOOP_ID, "current"),
            }
        ],
        "credential_slots": [],
    }


def clone(payload: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(payload)
