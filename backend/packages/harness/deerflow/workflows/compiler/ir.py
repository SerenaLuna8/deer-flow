"""Deeply immutable Workflow compiler IR.

The ``StructuredLoop*`` objects at the bottom remain the G02 executable Spike
contract.  The general ``WorkflowIR`` objects are the G12 publication compiler
contract and deliberately contain no application authority or runtime handles.
"""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from deerflow.workflows.compiler.cache import CompilerCacheKey
from deerflow.workflows.contracts import MAX_SAFE_JSON_INTEGER


@dataclass(frozen=True, slots=True)
class FrozenObject:
    """Canonical-key-ordered immutable JSON object."""

    items: tuple[tuple[str, FrozenJson], ...]

    def __post_init__(self) -> None:
        keys = tuple(key for key, _value in self.items)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("FrozenObject keys must be unique and canonically ordered")


type FrozenJson = None | bool | int | float | str | tuple[FrozenJson, ...] | FrozenObject


WORKFLOW_VALUE_MISSING = object()


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    if any(0xD800 <= ord(character) <= 0xDFFF for character in normalized):
        raise ValueError("Workflow IR supports only Unicode scalar values")
    return normalized


def freeze_json(value: Any) -> FrozenJson:
    """Deep-freeze a validated portable JSON value."""

    if value is None or type(value) is bool:
        return value
    if type(value) is str:
        return _normalize_text(value)
    if type(value) is int:
        if abs(value) > MAX_SAFE_JSON_INTEGER:
            raise ValueError("Workflow IR integer exceeds the cross-runtime safe range")
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Workflow IR does not support non-finite numbers")
        if value.is_integer() and abs(value) > MAX_SAFE_JSON_INTEGER:
            raise ValueError("Workflow IR number exceeds the cross-runtime safe integer range")
        if value.is_integer():
            return int(value)
        return value
    if isinstance(value, list | tuple):
        return tuple(freeze_json(item) for item in value)
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("Workflow IR JSON object keys must be strings")
        normalized_items: list[tuple[str, FrozenJson]] = []
        normalized_keys: set[str] = set()
        for key, item in value.items():
            normalized_key = _normalize_text(key)
            if normalized_key in normalized_keys:
                raise ValueError("Unicode normalization produced duplicate Workflow IR keys")
            normalized_keys.add(normalized_key)
            normalized_items.append((normalized_key, freeze_json(item)))
        return FrozenObject(tuple(sorted(normalized_items, key=lambda item: item[0])))
    raise TypeError(f"unsupported Workflow IR value: {type(value).__name__}")


def thaw_json(value: FrozenJson) -> Any:
    """Return a detached JSON projection of immutable IR data."""

    if isinstance(value, FrozenObject):
        return {key: thaw_json(item) for key, item in value.items}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class CompiledValueType:
    kind: str
    collection: bool
    nullable: bool
    schema_ref: str | None = None


@dataclass(frozen=True, slots=True)
class CompiledPort:
    id: str
    kind: Literal["control", "data"]
    direction: Literal["input", "output"]
    cardinality: Literal["one", "many"]
    required: bool
    value_type: CompiledValueType | None = None


@dataclass(frozen=True, slots=True)
class CompiledNode:
    id: str
    type: str
    type_version: int
    scope_path: tuple[str, ...]
    input_bindings: tuple[tuple[str, FrozenJson | None], ...]
    execution_policy: FrozenObject
    config: FrozenObject
    input_ports: tuple[CompiledPort, ...]
    output_ports: tuple[CompiledPort, ...]
    executor_port: Literal["llm", "code", "http"] | None


@dataclass(frozen=True, slots=True)
class CompiledWorkflowOutput:
    id: str
    value_type: CompiledValueType
    source: FrozenObject | None
    has_default: bool
    default: FrozenJson = None

    def __post_init__(self) -> None:
        if not self.has_default and self.default is not None:
            raise ValueError("an omitted Workflow output default must not carry a value")


class WorkflowOutputSettlementError(ValueError):
    """A validated output produced no value and has no frozen fallback."""


def _settled_value_matches_type(value: Any, value_type: CompiledValueType) -> bool:
    if value is None:
        return value_type.nullable
    if value_type.kind == "messages":
        return isinstance(value, list) and all(isinstance(item, dict) for item in value)
    if value_type.collection:
        if not isinstance(value, list):
            return False
        if value_type.kind == "json":
            return True
        item_type = CompiledValueType(
            kind=value_type.kind,
            collection=False,
            nullable=False,
        )
        return all(_settled_value_matches_type(item, item_type) for item in value)
    if value_type.kind == "string":
        return isinstance(value, str)
    if value_type.kind == "number":
        return type(value) in {int, float}
    if value_type.kind == "boolean":
        return type(value) is bool
    return type(value) in {str, int, float, bool} or isinstance(value, dict)


def settle_workflow_outputs(
    outputs: Sequence[CompiledWorkflowOutput],
    *,
    resolve: Callable[[FrozenObject], Any],
) -> FrozenObject:
    """Settle typed portable outputs without confusing MISSING with JSON null.

    This is the final primitive/collection/nullability envelope.  A source
    carrying ``schema_ref`` must already have passed that exact schema in its
    node executor's typed-output boundary before settlement.
    """

    settled: list[tuple[str, FrozenJson]] = []
    for output in sorted(outputs, key=lambda item: item.id):
        resolved = WORKFLOW_VALUE_MISSING if output.source is None else resolve(output.source)
        if resolved is WORKFLOW_VALUE_MISSING:
            if output.has_default:
                candidate = thaw_json(output.default)
            elif output.value_type.nullable:
                candidate = None
            else:
                raise WorkflowOutputSettlementError(f"WORKFLOW_OUTPUT_MISSING: output {output.id} produced no value")
        else:
            candidate = resolved
        try:
            value = freeze_json(candidate)
        except (TypeError, ValueError) as error:
            raise WorkflowOutputSettlementError(f"WORKFLOW_OUTPUT_INVALID: output {output.id} is not portable JSON") from error
        if not _settled_value_matches_type(candidate, output.value_type):
            raise WorkflowOutputSettlementError(f"WORKFLOW_OUTPUT_INVALID: output {output.id} does not match its frozen value type")
        settled.append((output.id, value))
    return FrozenObject(tuple(settled))


@dataclass(frozen=True, slots=True)
class CompiledEndpoint:
    node_id: str
    port_id: str


@dataclass(frozen=True, slots=True)
class CompiledEdge:
    transition_id: str
    source: CompiledEndpoint
    target: CompiledEndpoint


@dataclass(frozen=True, slots=True)
class CompiledOutcomeRoute:
    outcome: Literal["success", "error"]
    edge: CompiledEdge


@dataclass(frozen=True, slots=True)
class CompiledBranchRoute:
    output_port_id: str
    target_node_id: str
    predicate: FrozenObject


@dataclass(frozen=True, slots=True)
class CompiledBranch:
    node_id: str
    routes: tuple[CompiledBranchRoute, ...]
    else_output_port_id: str
    else_target_node_id: str
    error_route: CompiledEdge | None


@dataclass(frozen=True, slots=True)
class CompiledTransform:
    node_id: str
    mode: Literal["text", "json"]
    missing_variable: Literal["error", "null", "empty"]
    template: FrozenObject


@dataclass(frozen=True, slots=True)
class CompiledAggregateGroup:
    output_id: str
    candidate_input_ids: tuple[str, ...]
    candidate_branch_port_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CompiledAggregate:
    node_id: str
    condition_node_id: str
    groups: tuple[CompiledAggregateGroup, ...]


@dataclass(frozen=True, slots=True)
class CompiledLoopVariable:
    id: str
    initial_input_id: str
    next_input_id: str
    output_port_id: str


@dataclass(frozen=True, slots=True)
class CompiledLoopRegion:
    loop_node_id: str
    scope_path: tuple[str, ...]
    body_entry_node_id: str
    body_exit_node_id: str
    body_node_ids: tuple[str, ...]
    body_edges: tuple[CompiledEdge, ...]
    init_node_id: str
    commit_node_id: str
    route_node_id: str
    done_node_id: str
    limit_error_node_id: str
    variables: tuple[CompiledLoopVariable, ...]
    termination_condition: FrozenObject
    max_iterations: int
    generated_edges: tuple[CompiledEdge, ...]
    generated_back_edge: CompiledEdge
    condition_met_edge: CompiledEdge
    limit_exceeded_edge: CompiledEdge
    limit_error_code: Literal["WORKFLOW_LOOP_LIMIT_EXCEEDED"]
    worst_case_supersteps: int
    worst_case_activations: int


@dataclass(frozen=True, slots=True)
class WorkflowIR:
    graph_schema_version: int
    compiler_contract_version: int
    semantic_checksum: str
    nodes: tuple[CompiledNode, ...]
    workflow_outputs: tuple[CompiledWorkflowOutput, ...]
    static_edges: tuple[CompiledEdge, ...]
    outcome_routes: tuple[CompiledOutcomeRoute, ...]
    branches: tuple[CompiledBranch, ...]
    transforms: tuple[CompiledTransform, ...]
    aggregates: tuple[CompiledAggregate, ...]
    loop_regions: tuple[CompiledLoopRegion, ...]
    input_schema: FrozenObject
    output_schema: FrozenObject
    worst_case_depth: int
    worst_case_recursion_depth: int
    worst_case_parallelism: int
    worst_case_fan_out: int
    worst_case_steps: int
    worst_case_activations: int
    worst_case_iterations: int

    @property
    def cache_key(self) -> CompilerCacheKey:
        return CompilerCacheKey(
            graph_schema_version=self.graph_schema_version,
            compiler_contract_version=self.compiler_contract_version,
            semantic_checksum=self.semantic_checksum,
        )


def _port_projection(port: CompiledPort) -> dict[str, Any]:
    value_type: dict[str, Any] | None = None
    if port.value_type is not None:
        value_type = {
            "kind": port.value_type.kind,
            "collection": port.value_type.collection,
            "nullable": port.value_type.nullable,
        }
        if port.value_type.schema_ref is not None:
            value_type["schema_ref"] = port.value_type.schema_ref
    return {
        "id": port.id,
        "kind": port.kind,
        "direction": port.direction,
        "cardinality": port.cardinality,
        "required": port.required,
        "value_type": value_type,
    }


def _edge_projection(edge: CompiledEdge) -> dict[str, Any]:
    return {
        "transition_id": edge.transition_id,
        "source": {"node_id": edge.source.node_id, "port_id": edge.source.port_id},
        "target": {"node_id": edge.target.node_id, "port_id": edge.target.port_id},
    }


def workflow_ir_public_projection_v1(ir: WorkflowIR) -> dict[str, Any]:
    """Stable secret-free JSON projection used by compiler golden tests/cache diagnostics."""

    return {
        "graph_schema_version": ir.graph_schema_version,
        "compiler_contract_version": ir.compiler_contract_version,
        "semantic_checksum": ir.semantic_checksum,
        "nodes": [
            {
                "id": node.id,
                "type": node.type,
                "type_version": node.type_version,
                "scope_path": list(node.scope_path),
                "input_bindings": [[key, None if value is None else thaw_json(value)] for key, value in node.input_bindings],
                "execution_policy": thaw_json(node.execution_policy),
                "config": thaw_json(node.config),
                "input_ports": [_port_projection(port) for port in node.input_ports],
                "output_ports": [_port_projection(port) for port in node.output_ports],
                "executor_port": node.executor_port,
            }
            for node in ir.nodes
        ],
        "workflow_outputs": [
            {
                "id": output.id,
                "value_type": {
                    "kind": output.value_type.kind,
                    "collection": output.value_type.collection,
                    "nullable": output.value_type.nullable,
                    **({"schema_ref": output.value_type.schema_ref} if output.value_type.schema_ref is not None else {}),
                },
                "source": None if output.source is None else thaw_json(output.source),
                "has_default": output.has_default,
                "default": thaw_json(output.default) if output.has_default else None,
            }
            for output in ir.workflow_outputs
        ],
        "static_edges": [_edge_projection(edge) for edge in ir.static_edges],
        "outcome_routes": [
            {
                "outcome": route.outcome,
                **_edge_projection(route.edge),
            }
            for route in ir.outcome_routes
        ],
        "branches": [
            {
                "node_id": branch.node_id,
                "routes": [
                    {
                        "output_port_id": route.output_port_id,
                        "target_node_id": route.target_node_id,
                        "predicate": thaw_json(route.predicate),
                    }
                    for route in branch.routes
                ],
                "else_output_port_id": branch.else_output_port_id,
                "else_target_node_id": branch.else_target_node_id,
                "error_route": None if branch.error_route is None else _edge_projection(branch.error_route),
            }
            for branch in ir.branches
        ],
        "transforms": [
            {
                "node_id": transform.node_id,
                "mode": transform.mode,
                "missing_variable": transform.missing_variable,
                "template": thaw_json(transform.template),
            }
            for transform in ir.transforms
        ],
        "aggregates": [
            {
                "node_id": aggregate.node_id,
                "condition_node_id": aggregate.condition_node_id,
                "groups": [
                    {
                        "output_id": group.output_id,
                        "candidate_input_ids": list(group.candidate_input_ids),
                        "candidate_branch_port_ids": list(group.candidate_branch_port_ids),
                    }
                    for group in aggregate.groups
                ],
            }
            for aggregate in ir.aggregates
        ],
        "loop_regions": [
            {
                "loop_node_id": region.loop_node_id,
                "scope_path": list(region.scope_path),
                "body_entry_node_id": region.body_entry_node_id,
                "body_exit_node_id": region.body_exit_node_id,
                "body_node_ids": list(region.body_node_ids),
                "body_edges": [_edge_projection(edge) for edge in region.body_edges],
                "init_node_id": region.init_node_id,
                "commit_node_id": region.commit_node_id,
                "route_node_id": region.route_node_id,
                "done_node_id": region.done_node_id,
                "limit_error_node_id": region.limit_error_node_id,
                "variables": [
                    {
                        "id": variable.id,
                        "initial_input_id": variable.initial_input_id,
                        "next_input_id": variable.next_input_id,
                        "output_port_id": variable.output_port_id,
                    }
                    for variable in region.variables
                ],
                "termination_condition": thaw_json(region.termination_condition),
                "max_iterations": region.max_iterations,
                "generated_edges": [_edge_projection(edge) for edge in region.generated_edges],
                "generated_back_edge": _edge_projection(region.generated_back_edge),
                "condition_met_edge": _edge_projection(region.condition_met_edge),
                "limit_exceeded_edge": _edge_projection(region.limit_exceeded_edge),
                "limit_error_code": region.limit_error_code,
                "worst_case_supersteps": region.worst_case_supersteps,
                "worst_case_activations": region.worst_case_activations,
            }
            for region in ir.loop_regions
        ],
        "input_schema": thaw_json(ir.input_schema),
        "output_schema": thaw_json(ir.output_schema),
        "worst_case_depth": ir.worst_case_depth,
        "worst_case_recursion_depth": ir.worst_case_recursion_depth,
        "worst_case_parallelism": ir.worst_case_parallelism,
        "worst_case_fan_out": ir.worst_case_fan_out,
        "worst_case_steps": ir.worst_case_steps,
        "worst_case_activations": ir.worst_case_activations,
        "worst_case_iterations": ir.worst_case_iterations,
    }


def _canonical_uuid(value: str, field: str) -> None:
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise ValueError(f"{field} must be a canonical lowercase UUID")


@dataclass(frozen=True, slots=True)
class StructuredLoopPlan:
    """The executable subset frozen by G02; callbacks are runtime context."""

    graph_schema_version: int
    compiler_contract_version: int
    semantic_checksum: str
    loop_node_id: str
    body_node_id: str
    max_iterations: int

    def __post_init__(self) -> None:
        if type(self.graph_schema_version) is not int or self.graph_schema_version <= 0:
            raise ValueError("graph_schema_version must be positive")
        if type(self.compiler_contract_version) is not int or self.compiler_contract_version <= 0:
            raise ValueError("compiler_contract_version must be positive")
        if len(self.semantic_checksum) != 64 or any(character not in "0123456789abcdef" for character in self.semantic_checksum):
            raise ValueError("semantic_checksum must be a lowercase SHA-256 digest")
        _canonical_uuid(self.loop_node_id, "loop_node_id")
        _canonical_uuid(self.body_node_id, "body_node_id")
        if self.max_iterations <= 0:
            raise ValueError("max_iterations must be positive")


@dataclass(frozen=True, slots=True)
class StructuredLoopTemplate:
    """Immutable lowering cached before any DB/runtime authority is bound."""

    plan: StructuredLoopPlan
    nodes: tuple[str, ...] = field(
        default=(
            "loop_body",
            "loop_body_checkpoint",
            "loop_commit",
            "loop_commit_checkpoint",
            "loop_route",
            "loop_route_checkpoint",
        ),
        init=False,
    )
    static_edges: tuple[tuple[str, str], ...] = field(
        default=(
            ("loop_body", "loop_body_checkpoint"),
            ("loop_body_checkpoint", "loop_commit"),
            ("loop_commit", "loop_commit_checkpoint"),
            ("loop_commit_checkpoint", "loop_route"),
            ("loop_route", "loop_route_checkpoint"),
        ),
        init=False,
    )
    generated_back_edge: tuple[str, str] = field(
        default=("loop_route_checkpoint", "loop_body"),
        init=False,
    )

    @property
    def cache_key(self) -> CompilerCacheKey:
        return CompilerCacheKey(
            graph_schema_version=self.plan.graph_schema_version,
            compiler_contract_version=self.plan.compiler_contract_version,
            semantic_checksum=self.plan.semantic_checksum,
        )


def lower_structured_loop(plan: StructuredLoopPlan) -> StructuredLoopTemplate:
    return StructuredLoopTemplate(plan=plan)
