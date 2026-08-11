"""Deterministic publish-grade validation for strict WorkflowSpec/Canvas v1."""

from __future__ import annotations

import heapq
import re
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, fields, replace
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel

from deerflow.workflows.canonical import canonical_json_value
from deerflow.workflows.catalog_contracts import PortDefinition
from deerflow.workflows.contracts import (
    CanvasDocumentV1,
    LoopVariableValueBinding,
    NodeOutputValueBinding,
    PredicateAst,
    RestrictedJsonTemplate,
    WorkflowInputValueBinding,
    WorkflowNodeSpec,
    WorkflowSpecV1,
    WorkflowValueType,
)
from deerflow.workflows.json_schema import (
    WorkflowJsonSchemaError,
    validate_strict_json_schema,
    value_type_at_json_pointer,
    value_type_from_json_schema,
)
from deerflow.workflows.registry import FIRST_BATCH_RUNTIME_REGISTRY, WorkflowNodeRegistry, WorkflowNodeRegistryError, resolve_node_ports

_PHASE_ORDER = {
    "structure": 0,
    "topology": 1,
    "port": 2,
    "dataflow": 3,
    "routing": 4,
    "security": 5,
    "runtime_policy": 6,
    "output": 7,
    "canvas": 8,
}
_JSON_TEMPLATE_TOKEN = re.compile(r"^\{\{([A-Za-z][A-Za-z0-9_.:-]{0,127})\}\}$")
_COMPILER_HARD_MAX_NODES = 10_000
_COMPILER_HARD_MAX_EDGES = 50_000
_DANGEROUS_HTTP_HEADERS = frozenset(
    {
        "authorization",
        "connection",
        "content-length",
        "cookie",
        "host",
        "idempotency-key",
        "proxy-authorization",
        "transfer-encoding",
    }
)


@dataclass(frozen=True, slots=True)
class WorkflowValidationIssue:
    phase: str
    code: str
    message: str
    node_id: str | None = None
    transition_id: str | None = None
    port_id: str | None = None


class WorkflowValidationError(ValueError):
    def __init__(self, issues: tuple[WorkflowValidationIssue, ...]) -> None:
        self.issues = issues
        codes = ", ".join(issue.code for issue in issues[:8])
        super().__init__(f"Workflow validation failed: {codes}")


@dataclass(frozen=True, slots=True)
class WorkflowCompilationLimits:
    max_nodes: int
    max_edges: int
    max_depth: int
    max_total_steps: int
    max_recursion_depth: int
    max_parallelism: int
    max_fan_out: int
    max_loops: int
    max_loop_body_nodes: int
    max_loop_body_edges: int
    max_loop_iterations: int
    max_total_iterations: int
    max_total_activations: int
    max_aggregate_groups: int
    max_aggregate_candidates: int

    @classmethod
    def permissive(cls) -> WorkflowCompilationLimits:
        return cls(
            max_nodes=10_000,
            max_edges=50_000,
            max_depth=1_000_000,
            max_total_steps=10_000_000,
            max_recursion_depth=10_000_000,
            max_parallelism=10_000,
            max_fan_out=50_000,
            max_loops=1_000,
            max_loop_body_nodes=10_000,
            max_loop_body_edges=50_000,
            max_loop_iterations=1_000_000,
            max_total_iterations=10_000_000,
            max_total_activations=10_000_000,
            max_aggregate_groups=254,
            max_aggregate_candidates=100_000,
        )

    def replace(self, **changes: int) -> WorkflowCompilationLimits:
        known = {item.name for item in fields(self)}
        if set(changes) - known:
            raise TypeError("unknown Workflow compilation limit")
        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class WorkflowMetrics:
    node_count: int
    edge_count: int
    depth: int
    recursion_depth: int
    max_parallelism: int
    max_fan_out: int
    loop_count: int
    total_iterations: int
    total_steps: int
    total_activations: int


@dataclass(frozen=True, slots=True)
class WorkflowAggregateProvenance:
    node_id: str
    condition_node_id: str
    group_branch_ports: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True, slots=True)
class WorkflowValidationResult:
    issues: tuple[WorkflowValidationIssue, ...]
    metrics: WorkflowMetrics
    aggregate_provenance: tuple[WorkflowAggregateProvenance, ...]

    @property
    def is_valid(self) -> bool:
        return not self.issues

    @property
    def issue_codes(self) -> tuple[str, ...]:
        return tuple(issue.code for issue in self.issues)

    def raise_for_errors(self) -> None:
        if self.issues:
            raise WorkflowValidationError(self.issues)


@dataclass(frozen=True, slots=True)
class _DominatorIndex:
    bit_by_node: dict[str, int]
    mask_by_node: dict[str, int]

    def dominates(self, dominator_id: str, node_id: str) -> bool:
        bit = self.bit_by_node.get(dominator_id)
        return bit is not None and bool(self.mask_by_node.get(node_id, 0) & bit)


@dataclass(frozen=True, slots=True)
class _ReachabilityIndex:
    bit_by_node: dict[str, int]
    mask_by_node: dict[str, int]

    def reaches(self, source_id: str, target_id: str) -> bool:
        bit = self.bit_by_node.get(target_id)
        return bit is not None and bool(self.mask_by_node.get(source_id, 0) & bit)


@dataclass(slots=True)
class _Graph:
    nodes: frozenset[str]
    edges: tuple[tuple[str, str], ...]

    def adjacency(self) -> dict[str, tuple[str, ...]]:
        values: dict[str, list[str]] = {node_id: [] for node_id in self.nodes}
        for source, target in self.edges:
            values[source].append(target)
        return {node_id: tuple(sorted(targets)) for node_id, targets in values.items()}

    def reverse_adjacency(self) -> dict[str, tuple[str, ...]]:
        values: dict[str, list[str]] = {node_id: [] for node_id in self.nodes}
        for source, target in self.edges:
            values[target].append(source)
        return {node_id: tuple(sorted(sources)) for node_id, sources in values.items()}

    def topological(self) -> tuple[str, ...] | None:
        adjacency = self.adjacency()
        indegree = {node_id: 0 for node_id in self.nodes}
        for _source, target in self.edges:
            indegree[target] += 1
        ready = [node_id for node_id, degree in indegree.items() if degree == 0]
        heapq.heapify(ready)
        ordered: list[str] = []
        while ready:
            node_id = heapq.heappop(ready)
            ordered.append(node_id)
            for target in adjacency[node_id]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    heapq.heappush(ready, target)
        return tuple(ordered) if len(ordered) == len(self.nodes) else None

    def reachable(self, starts: tuple[str, ...], *, reverse: bool = False, stop: str | None = None) -> frozenset[str]:
        adjacency = self.reverse_adjacency() if reverse else self.adjacency()
        seen: set[str] = set()
        pending = deque(node_id for node_id in starts if node_id in self.nodes)
        while pending:
            node_id = pending.popleft()
            if node_id in seen:
                continue
            seen.add(node_id)
            if node_id == stop:
                continue
            pending.extend(adjacency[node_id])
        return frozenset(seen)

    def dominators(self, entry: str) -> _DominatorIndex:
        ordered = self.topological()
        if entry not in self.nodes or ordered is None:
            return _DominatorIndex({}, {})
        reverse = self.reverse_adjacency()
        reachable = self.reachable((entry,))
        bit_by_node = {node_id: 1 << index for index, node_id in enumerate(ordered)}
        masks: dict[str, int] = {}
        for node_id in ordered:
            if node_id not in reachable:
                continue
            if node_id == entry:
                masks[node_id] = bit_by_node[entry]
                continue
            predecessors = [source for source in reverse[node_id] if source in masks]
            if predecessors:
                predecessor_mask = masks[predecessors[0]]
                for source in predecessors[1:]:
                    predecessor_mask &= masks[source]
            else:
                predecessor_mask = 0
            masks[node_id] = predecessor_mask | bit_by_node[node_id]
        return _DominatorIndex(bit_by_node, masks)

    def reachability(self) -> _ReachabilityIndex:
        ordered = self.topological()
        if ordered is None:
            return _ReachabilityIndex({}, {})
        adjacency = self.adjacency()
        bit_by_node = {node_id: 1 << index for index, node_id in enumerate(ordered)}
        masks: dict[str, int] = {}
        for node_id in reversed(ordered):
            mask = bit_by_node[node_id]
            for target in adjacency[node_id]:
                mask |= masks[target]
            masks[node_id] = mask
        return _ReachabilityIndex(bit_by_node, masks)

    def longest_weighted_path(self, weights: dict[str, int]) -> int:
        ordered = self.topological()
        if ordered is None:
            return 0
        reverse = self.reverse_adjacency()
        distances: dict[str, int] = {}
        for node_id in ordered:
            predecessors = reverse[node_id]
            distances[node_id] = weights.get(node_id, 1) + max((distances[source] for source in predecessors), default=0)
        return max(distances.values(), default=0)

    def max_layer_width(self) -> int:
        """Return a deterministic conservative parallel-width upper bound."""

        ordered = self.topological()
        if ordered is None:
            return 0
        reverse = self.reverse_adjacency()
        layers: dict[str, int] = {}
        for node_id in ordered:
            layers[node_id] = max(
                (layers[source] + 1 for source in reverse[node_id]),
                default=0,
            )
        return max(Counter(layers.values()).values(), default=0)


def _issue(
    issues: list[WorkflowValidationIssue],
    phase: str,
    code: str,
    message: str,
    *,
    node_id: str | None = None,
    transition_id: str | None = None,
    port_id: str | None = None,
) -> None:
    issues.append(
        WorkflowValidationIssue(
            phase=phase,
            code=code,
            message=message,
            node_id=node_id,
            transition_id=transition_id,
            port_id=port_id,
        )
    )


def _sorted_issues(issues: list[WorkflowValidationIssue]) -> tuple[WorkflowValidationIssue, ...]:
    unique = set(issues)
    return tuple(
        sorted(
            unique,
            key=lambda item: (
                _PHASE_ORDER.get(item.phase, 999),
                item.code,
                item.node_id or "",
                item.transition_id or "",
                item.port_id or "",
                item.message,
            ),
        )
    )


def _duplicates(values: list[str]) -> tuple[str, ...]:
    return tuple(sorted(value for value, count in Counter(values).items() if count > 1))


def _validate_unique(
    values: list[str],
    issues: list[WorkflowValidationIssue],
    *,
    phase: str,
    code: str,
    message: str,
    node_id: str | None = None,
) -> None:
    for duplicate in _duplicates(values):
        _issue(issues, phase, code, message, node_id=node_id, port_id=duplicate)


def _scope_key(node: WorkflowNodeSpec) -> tuple[str, str | None]:
    if node.scope.kind == "root":
        return ("root", None)
    return ("loop_body", node.scope.loop_node_id)


def _graphs(
    spec: WorkflowSpecV1,
) -> tuple[_Graph, dict[str, _Graph], dict[str, tuple[str, str | None]]]:
    scope_by_id = {node.id: _scope_key(node) for node in spec.nodes}
    root_nodes = frozenset(node_id for node_id, scope in scope_by_id.items() if scope[0] == "root")
    body_nodes: dict[str, set[str]] = defaultdict(set)
    for node_id, scope in scope_by_id.items():
        if scope[0] == "loop_body" and scope[1] is not None:
            body_nodes[scope[1]].add(node_id)
    root_edges: list[tuple[str, str]] = []
    body_edges: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for transition in spec.transitions:
        source_scope = scope_by_id.get(transition.source.node_id)
        target_scope = scope_by_id.get(transition.target.node_id)
        if source_scope is None or target_scope is None or source_scope != target_scope:
            continue
        if source_scope[0] == "root":
            root_edges.append((transition.source.node_id, transition.target.node_id))
        elif source_scope[1] is not None:
            body_edges[source_scope[1]].append((transition.source.node_id, transition.target.node_id))
    return (
        _Graph(root_nodes, tuple(root_edges)),
        {loop_id: _Graph(frozenset(nodes), tuple(body_edges.get(loop_id, []))) for loop_id, nodes in body_nodes.items()},
        scope_by_id,
    )


def _outcome_route_node_id(node_id: str, port_id: str) -> str:
    return f"@outcome/{node_id}/{port_id}"


def _outcome_graphs(
    spec: WorkflowSpecV1,
    ports: dict[str, tuple[dict[str, PortDefinition], dict[str, PortDefinition]]],
    scope_by_id: dict[str, tuple[str, str | None]],
) -> tuple[_Graph, dict[str, _Graph]]:
    """Expand each control output into an exclusive outcome point.

    A node data output becomes available only at its normal-success outcome;
    the error outcome is a distinct path even when both routes later rejoin.
    """

    nodes_by_scope: dict[tuple[str, str | None], set[str]] = defaultdict(set)
    edges_by_scope: dict[tuple[str, str | None], list[tuple[str, str]]] = defaultdict(list)
    for node in spec.nodes:
        scope = scope_by_id[node.id]
        nodes_by_scope[scope].add(node.id)
        for port in ports.get(node.id, ({}, {}))[1].values():
            if port.kind != "control" or (node.type == "loop" and port.id == "body"):
                continue
            route_id = _outcome_route_node_id(node.id, port.id)
            nodes_by_scope[scope].add(route_id)
            edges_by_scope[scope].append((node.id, route_id))
    for transition in spec.transitions:
        source_scope = scope_by_id.get(transition.source.node_id)
        if source_scope is None or source_scope != scope_by_id.get(transition.target.node_id):
            continue
        source_port = ports.get(transition.source.node_id, ({}, {}))[1].get(transition.source.port_id)
        if source_port is None or source_port.kind != "control":
            continue
        route_id = _outcome_route_node_id(transition.source.node_id, transition.source.port_id)
        if route_id in nodes_by_scope[source_scope]:
            edges_by_scope[source_scope].append((route_id, transition.target.node_id))
    root_scope = ("root", None)
    root = _Graph(
        frozenset(nodes_by_scope.get(root_scope, set())),
        tuple(edges_by_scope.get(root_scope, [])),
    )
    bodies = {
        loop_id: _Graph(
            frozenset(nodes),
            tuple(edges_by_scope.get(("loop_body", loop_id), [])),
        )
        for (kind, loop_id), nodes in nodes_by_scope.items()
        if kind == "loop_body" and loop_id is not None
    }
    return root, bodies


def _success_outcome_point(node: WorkflowNodeSpec) -> str | None:
    if node.type == "start":
        return node.id
    if node.type == "http_request":
        return _outcome_route_node_id(node.id, "success")
    if node.type in {"llm", "transform", "variable_aggregate", "loop", "python_code"}:
        return _outcome_route_node_id(node.id, "next")
    return None


def _body_exit_completion_points(
    loop: WorkflowNodeSpec,
    node_by_id: dict[str, WorkflowNodeSpec],
) -> tuple[str, ...]:
    exit_node = node_by_id.get(loop.config.body_exit_node_id)  # type: ignore[union-attr]
    if exit_node is None:
        return ()
    if exit_node.type == "condition":
        return tuple(
            _outcome_route_node_id(exit_node.id, port_id)
            for port_id in (
                *(branch.output_port_id for branch in exit_node.config.branches),
                exit_node.config.else_output_port_id,
            )
        )
    point = _success_outcome_point(exit_node)
    return () if point is None else (point,)


def _port_maps(
    spec: WorkflowSpecV1,
    registry: WorkflowNodeRegistry,
    issues: list[WorkflowValidationIssue],
) -> dict[str, tuple[dict[str, PortDefinition], dict[str, PortDefinition]]]:
    result: dict[str, tuple[dict[str, PortDefinition], dict[str, PortDefinition]]] = {}
    for node in spec.nodes:
        try:
            registry.require(node.type, node.type_version)
            inputs, outputs = resolve_node_ports(spec, node)
        except (ValueError, WorkflowNodeRegistryError) as error:
            _issue(issues, "structure", "WORKFLOW_NODE_TYPE_UNAVAILABLE", str(error), node_id=node.id)
            continue
        result[node.id] = (
            {port.id: port for port in inputs},
            {port.id: port for port in outputs},
        )
    return result


def _validate_predicate(
    predicate: PredicateAst,
    issues: list[WorkflowValidationIssue],
    *,
    node_id: str,
) -> None:
    if not predicate.items:
        _issue(issues, "security", "WORKFLOW_PREDICATE_EMPTY", "Predicate groups cannot be empty", node_id=node_id)
    for item in predicate.items:
        if isinstance(item, PredicateAst):
            _validate_predicate(item, issues, node_id=node_id)
            continue
        if item.operator in {"is_null", "is_not_null"}:
            if item.right is not None:
                _issue(
                    issues,
                    "security",
                    "WORKFLOW_PREDICATE_RIGHT_FORBIDDEN",
                    "Null predicates cannot carry a right binding",
                    node_id=node_id,
                )
        elif item.right is None:
            _issue(
                issues,
                "security",
                "WORKFLOW_PREDICATE_RIGHT_REQUIRED",
                "Comparison predicates require a right binding",
                node_id=node_id,
            )


def _json_template_tokens(value: Any, issues: list[WorkflowValidationIssue], *, node_id: str) -> set[str]:
    tokens: set[str] = set()
    if isinstance(value, str):
        matched = _JSON_TEMPLATE_TOKEN.fullmatch(value)
        if matched:
            tokens.add(matched.group(1))
        elif "{{" in value or "}}" in value:
            _issue(
                issues,
                "security",
                "WORKFLOW_JSON_TEMPLATE_TOKEN_INVALID",
                "JSON binding tokens must occupy the complete string value",
                node_id=node_id,
            )
        return tokens
    if isinstance(value, list):
        for item in value:
            tokens.update(_json_template_tokens(item, issues, node_id=node_id))
        return tokens
    if isinstance(value, dict):
        if "$binding" in value:
            if set(value) != {"$binding"} or not isinstance(value["$binding"], str):
                _issue(
                    issues,
                    "security",
                    "WORKFLOW_JSON_TEMPLATE_TOKEN_INVALID",
                    "Object binding tokens must contain only one string $binding field",
                    node_id=node_id,
                )
            else:
                tokens.add(value["$binding"])
            return tokens
        for item in value.values():
            tokens.update(_json_template_tokens(item, issues, node_id=node_id))
    return tokens


def _validate_json_template(template: RestrictedJsonTemplate, issues: list[WorkflowValidationIssue], *, node_id: str) -> None:
    tokens = _json_template_tokens(template.template, issues, node_id=node_id)
    binding_ids = set(template.bindings)
    for missing in sorted(tokens - binding_ids):
        _issue(
            issues,
            "security",
            "WORKFLOW_JSON_TEMPLATE_BINDING_MISSING",
            f"JSON template binding is missing: {missing}",
            node_id=node_id,
        )
    for unused in sorted(binding_ids - tokens):
        _issue(
            issues,
            "security",
            "WORKFLOW_JSON_TEMPLATE_BINDING_UNUSED",
            f"JSON template binding is unused: {unused}",
            node_id=node_id,
        )


def _iter_nested_values(value: Any):
    if isinstance(value, (WorkflowInputValueBinding, LoopVariableValueBinding, NodeOutputValueBinding)) or getattr(value, "kind", None) == "literal":
        yield value
        return
    if isinstance(value, BaseModel):
        for field_name in type(value).model_fields:
            yield from _iter_nested_values(getattr(value, field_name))
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _iter_nested_values(item)
        return
    if isinstance(value, list | tuple):
        for item in value:
            yield from _iter_nested_values(item)


def _iter_value_types(value: Any):
    if isinstance(value, WorkflowValueType):
        yield value
        return
    if isinstance(value, BaseModel):
        for field_name in type(value).model_fields:
            yield from _iter_value_types(getattr(value, field_name))
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _iter_value_types(item)
        return
    if isinstance(value, list | tuple):
        for item in value:
            yield from _iter_value_types(item)


def _validate_value_types(spec: WorkflowSpecV1, issues: list[WorkflowValidationIssue]) -> None:
    def validate(value_type: WorkflowValueType, *, node_id: str | None, port_id: str | None) -> None:
        if value_type.kind == "messages" and not value_type.collection:
            _issue(
                issues,
                "dataflow",
                "WORKFLOW_VALUE_TYPE_INVALID",
                "messages values must use collection=true",
                node_id=node_id,
                port_id=port_id,
            )

    for declaration in spec.workflow_inputs:
        validate(declaration.value_type, node_id=None, port_id=declaration.id)
    for declaration in spec.workflow_outputs:
        validate(declaration.value_type, node_id=None, port_id=declaration.id)
    for node in spec.nodes:
        for value_type in _iter_value_types(node.config):
            validate(value_type, node_id=node.id, port_id=None)


def _literal_type(value: Any) -> tuple[str, bool, bool]:
    if value is None:
        return ("null", False, True)
    if type(value) is bool:
        return ("boolean", False, False)
    if type(value) in {int, float}:
        return ("number", False, False)
    if isinstance(value, str):
        return ("string", False, False)
    return ("json", isinstance(value, list), False)


def _type_compatible(
    source: tuple[str, bool, bool, str | None],
    target: WorkflowValueType,
) -> bool:
    source_kind, source_collection, source_nullable, source_schema = source
    if source_kind == "null":
        return target.nullable
    if source_nullable and not target.nullable:
        return False
    if source_kind != target.kind:
        return False
    if source_collection != target.collection:
        return False
    if target.schema_ref is not None and target.schema_ref != source_schema:
        return False
    return True


def _binding_source_type(
    binding: Any,
    *,
    input_types: dict[str, WorkflowValueType],
    output_ports: dict[str, dict[str, PortDefinition]],
    output_schemas: dict[tuple[str, str], dict[str, Any]],
    loop_variable_types: dict[tuple[str, str], WorkflowValueType],
    issues: list[WorkflowValidationIssue],
    node_id: str | None,
) -> tuple[str, bool, bool, str | None] | None:
    if binding.kind == "literal":
        kind, collection, nullable = _literal_type(binding.value)
        return (kind, collection, nullable, None)
    if isinstance(binding, WorkflowInputValueBinding):
        value_type = input_types.get(binding.input_id)
        if value_type is None:
            _issue(issues, "dataflow", "WORKFLOW_INPUT_UNKNOWN", "Binding references an unknown Workflow input", node_id=node_id)
            return None
        return (value_type.kind, value_type.collection, value_type.nullable, value_type.schema_ref)
    if isinstance(binding, LoopVariableValueBinding):
        value_type = loop_variable_types.get((binding.loop_node_id, binding.variable_id))
        if value_type is None:
            _issue(
                issues,
                "dataflow",
                "WORKFLOW_LOOP_VARIABLE_UNKNOWN",
                "Binding references an unknown Loop variable",
                node_id=node_id,
            )
            return None
        return (value_type.kind, value_type.collection, value_type.nullable, value_type.schema_ref)
    if isinstance(binding, NodeOutputValueBinding):
        port = output_ports.get(binding.node_id, {}).get(binding.output_id)
        if port is None or port.kind != "data" or port.value_type is None:
            _issue(
                issues,
                "dataflow",
                "WORKFLOW_NODE_OUTPUT_UNKNOWN",
                "Binding references an unknown node data output",
                node_id=node_id,
                port_id=binding.output_id,
            )
            return None
        value_type = port.value_type
        if binding.path is not None:
            if value_type.kind != "json":
                _issue(
                    issues,
                    "dataflow",
                    "WORKFLOW_NODE_OUTPUT_PATH_INVALID",
                    "A JSON Pointer can be applied only to a JSON output",
                    node_id=node_id,
                    port_id=binding.output_id,
                )
                return None
            schema = output_schemas.get((binding.node_id, binding.output_id))
            if schema is None:
                _issue(
                    issues,
                    "dataflow",
                    "WORKFLOW_JSON_POINTER_SCHEMA_UNAVAILABLE",
                    "JSON Pointer typing requires a frozen inline node output schema",
                    node_id=node_id,
                    port_id=binding.output_id,
                )
                return None
            try:
                pointed = value_type_at_json_pointer(schema, binding.path)
            except WorkflowJsonSchemaError as error:
                _issue(
                    issues,
                    "dataflow",
                    error.code,
                    str(error),
                    node_id=node_id,
                    port_id=binding.output_id,
                )
                return None
            return (
                pointed.kind,
                pointed.collection,
                pointed.nullable,
                pointed.schema_ref,
            )
        return (value_type.kind, value_type.collection, value_type.nullable, value_type.schema_ref)
    raise AssertionError("unknown strict ValueBinding variant")


def _predicate_types_compatible(
    operator: str,
    left: tuple[str, bool, bool, str | None],
    right: tuple[str, bool, bool, str | None],
) -> bool:
    left_kind, left_collection, left_nullable, left_schema = left
    right_kind, right_collection, right_nullable, right_schema = right
    if operator in {"contains", "starts_with", "ends_with"}:
        return left_kind == right_kind == "string" and not left_collection and not right_collection and not left_nullable and not right_nullable
    if operator in {"gt", "gte", "lt", "lte"}:
        return not left_collection and not right_collection and not left_nullable and not right_nullable and (left_kind == right_kind == "number" or left_kind == right_kind == "string")
    if operator not in {"eq", "ne"}:
        return False
    if left_kind == "null":
        return right_nullable
    if right_kind == "null":
        return left_nullable
    if left_kind != right_kind or left_collection != right_collection:
        return False
    return left_schema is None or right_schema is None or left_schema == right_schema


def _validate_predicate_types(
    predicate: PredicateAst,
    issues: list[WorkflowValidationIssue],
    *,
    node_id: str,
    input_types: dict[str, WorkflowValueType],
    output_ports: dict[str, dict[str, PortDefinition]],
    output_schemas: dict[tuple[str, str], dict[str, Any]],
    loop_variable_types: dict[tuple[str, str], WorkflowValueType],
) -> None:
    for item in predicate.items:
        if isinstance(item, PredicateAst):
            _validate_predicate_types(
                item,
                issues,
                node_id=node_id,
                input_types=input_types,
                output_ports=output_ports,
                output_schemas=output_schemas,
                loop_variable_types=loop_variable_types,
            )
            continue
        left_type = _binding_source_type(
            item.left,
            input_types=input_types,
            output_ports=output_ports,
            output_schemas=output_schemas,
            loop_variable_types=loop_variable_types,
            issues=issues,
            node_id=node_id,
        )
        if left_type is None:
            continue
        if item.operator in {"is_null", "is_not_null"}:
            if not left_type[2]:
                _issue(
                    issues,
                    "dataflow",
                    "WORKFLOW_PREDICATE_NULLABILITY_INVALID",
                    "Null predicates require a nullable left operand",
                    node_id=node_id,
                )
            continue
        if item.right is None:
            continue
        right_type = _binding_source_type(
            item.right,
            input_types=input_types,
            output_ports=output_ports,
            output_schemas=output_schemas,
            loop_variable_types=loop_variable_types,
            issues=issues,
            node_id=node_id,
        )
        if right_type is not None and not _predicate_types_compatible(item.operator, left_type, right_type):
            _issue(
                issues,
                "dataflow",
                "WORKFLOW_PREDICATE_TYPE_MISMATCH",
                "Predicate operator is incompatible with its operand types",
                node_id=node_id,
            )


def _literal_matches_value_type(value: Any, value_type: WorkflowValueType) -> bool:
    if value is None:
        return value_type.nullable
    if value_type.kind == "messages":
        return isinstance(value, list) and all(isinstance(item, dict) for item in value)
    if value_type.collection and value_type.kind != "messages":
        if not isinstance(value, list):
            return False
        if value_type.kind == "json":
            return True
        scalar_type = value_type.model_copy(update={"collection": False, "nullable": False})
        return all(_literal_matches_value_type(item, scalar_type) for item in value)
    if value_type.kind == "string":
        return isinstance(value, str)
    if value_type.kind == "number":
        return type(value) in {int, float}
    if value_type.kind == "boolean":
        return type(value) is bool
    return not isinstance(value, list)


def _input_constraints_match(value: Any, constraints: Any) -> bool:
    if constraints.kind == "none":
        return True
    if value is None:
        return True
    if constraints.kind == "enum":
        encoded = canonical_json_value(value)
        return any(encoded == canonical_json_value(option) for option in constraints.options)
    if constraints.kind == "string":
        if not isinstance(value, str):
            return False
        if constraints.min_length is not None and len(value) < constraints.min_length:
            return False
        if constraints.max_length is not None and len(value) > constraints.max_length:
            return False
        return True
    if type(value) not in {int, float}:
        return False
    if constraints.minimum is not None and value < constraints.minimum:
        return False
    return constraints.maximum is None or value <= constraints.maximum


def _validate_declaration_defaults_and_constraints(
    spec: WorkflowSpecV1,
    issues: list[WorkflowValidationIssue],
) -> None:
    for declaration in spec.workflow_inputs:
        constraints = declaration.constraints
        if constraints.kind == "string" and (declaration.value_type.kind != "string" or declaration.value_type.collection):
            _issue(
                issues,
                "dataflow",
                "WORKFLOW_INPUT_CONSTRAINT_TYPE_MISMATCH",
                "String constraints require a string Workflow input",
                port_id=declaration.id,
            )
        elif constraints.kind == "number" and (declaration.value_type.kind != "number" or declaration.value_type.collection):
            _issue(
                issues,
                "dataflow",
                "WORKFLOW_INPUT_CONSTRAINT_TYPE_MISMATCH",
                "Number constraints require a number Workflow input",
                port_id=declaration.id,
            )
        elif constraints.kind == "enum" and any(not _literal_matches_value_type(option, declaration.value_type) for option in constraints.options):
            _issue(
                issues,
                "dataflow",
                "WORKFLOW_INPUT_CONSTRAINT_TYPE_MISMATCH",
                "Enum options must match the Workflow input value type",
                port_id=declaration.id,
            )
        if constraints.kind == "enum":
            options = [canonical_json_value(option) for option in constraints.options]
            if not options or len(options) != len(set(options)):
                _issue(
                    issues,
                    "dataflow",
                    "WORKFLOW_INPUT_CONSTRAINT_INVALID",
                    "Enum constraints require a non-empty set of unique JSON values",
                    port_id=declaration.id,
                )
        if constraints.kind == "string":
            if constraints.min_length is not None and constraints.max_length is not None and constraints.min_length > constraints.max_length:
                _issue(
                    issues,
                    "dataflow",
                    "WORKFLOW_INPUT_CONSTRAINT_INVALID",
                    "String constraint bounds must be ordered",
                    port_id=declaration.id,
                )
            if constraints.pattern is not None:
                _issue(
                    issues,
                    "security",
                    "WORKFLOW_INPUT_PATTERN_UNSUPPORTED",
                    "Regex input patterns are not supported by the first compiler contract",
                    port_id=declaration.id,
                )
        if constraints.kind == "number" and (constraints.minimum is not None and constraints.maximum is not None and constraints.minimum > constraints.maximum):
            _issue(
                issues,
                "dataflow",
                "WORKFLOW_INPUT_CONSTRAINT_INVALID",
                "Number constraint bounds must be ordered",
                port_id=declaration.id,
            )
        if "default" in declaration.model_fields_set:
            if declaration.value_type.schema_ref is not None:
                _issue(
                    issues,
                    "dataflow",
                    "WORKFLOW_DEFAULT_SCHEMA_UNVERIFIABLE",
                    "A schema-referenced Workflow input cannot declare an inline default in compiler v1",
                    port_id=declaration.id,
                )
            elif not _literal_matches_value_type(declaration.default, declaration.value_type):
                _issue(
                    issues,
                    "dataflow",
                    "WORKFLOW_INPUT_DEFAULT_TYPE_MISMATCH",
                    "Workflow input default does not match its value type",
                    port_id=declaration.id,
                )
            elif not _input_constraints_match(declaration.default, constraints):
                _issue(
                    issues,
                    "dataflow",
                    "WORKFLOW_INPUT_DEFAULT_CONSTRAINT_MISMATCH",
                    "Workflow input default does not satisfy its constraints",
                    port_id=declaration.id,
                )
    for declaration in spec.workflow_outputs:
        if "default" not in declaration.model_fields_set:
            continue
        if declaration.value_type.schema_ref is not None:
            _issue(
                issues,
                "output",
                "WORKFLOW_DEFAULT_SCHEMA_UNVERIFIABLE",
                "A schema-referenced Workflow output cannot declare an inline default in compiler v1",
                port_id=declaration.id,
            )
        elif not _literal_matches_value_type(declaration.default, declaration.value_type):
            _issue(
                issues,
                "output",
                "WORKFLOW_OUTPUT_DEFAULT_TYPE_MISMATCH",
                "Workflow output default does not match its value type",
                port_id=declaration.id,
            )


def _validate_binding_scope_and_dominance(
    binding: Any,
    *,
    consumer_node_id: str | None,
    usage: str,
    scope_by_id: dict[str, tuple[str, str | None]],
    root_dominators: _DominatorIndex,
    body_dominators: dict[str, _DominatorIndex],
    success_points: dict[str, str],
    body_exit_points: dict[str, tuple[str, ...]],
    loop_by_id: dict[str, WorkflowNodeSpec],
    issues: list[WorkflowValidationIssue],
) -> None:
    if isinstance(binding, LoopVariableValueBinding):
        if usage in {"loop_next", "loop_termination"} and consumer_node_id == binding.loop_node_id:
            return
        consumer_scope = scope_by_id.get(consumer_node_id or "")
        if consumer_scope != ("loop_body", binding.loop_node_id):
            _issue(
                issues,
                "dataflow",
                "WORKFLOW_LOOP_VARIABLE_SCOPE_INVALID",
                "Loop variables are visible only in their owning body and commit contract",
                node_id=consumer_node_id,
            )
        return
    if not isinstance(binding, NodeOutputValueBinding):
        return
    source_point = success_points.get(binding.node_id)
    if source_point is None:
        return
    source_scope = scope_by_id.get(binding.node_id)
    if consumer_node_id is None:
        if source_scope is not None and source_scope[0] != "root":
            _issue(
                issues,
                "dataflow",
                "WORKFLOW_BINDING_SCOPE_INVALID",
                "Loop body output cannot escape directly to Workflow output",
                port_id=binding.output_id,
            )
        return
    consumer_scope = scope_by_id.get(consumer_node_id)
    if source_scope is None or consumer_scope is None:
        return
    if usage == "loop_next":
        loop_node = loop_by_id.get(consumer_node_id)
        if loop_node is None or source_scope != ("loop_body", consumer_node_id):
            _issue(
                issues,
                "dataflow",
                "WORKFLOW_BINDING_SCOPE_INVALID",
                "Loop next binding must come from its own body",
                node_id=consumer_node_id,
            )
            return
        body_dominator = body_dominators.get(consumer_node_id, _DominatorIndex({}, {}))
        exit_points = body_exit_points.get(consumer_node_id, ())
        if not exit_points or any(not body_dominator.dominates(source_point, point) for point in exit_points):
            _issue(
                issues,
                "dataflow",
                "WORKFLOW_BINDING_NOT_DOMINATED",
                "Loop next binding must be available on every body exit path",
                node_id=consumer_node_id,
            )
        return
    if consumer_scope[0] == "root":
        if source_scope[0] != "root":
            _issue(
                issues,
                "dataflow",
                "WORKFLOW_BINDING_SCOPE_INVALID",
                "Loop body output cannot escape its scope",
                node_id=consumer_node_id,
            )
        elif not root_dominators.dominates(source_point, consumer_node_id):
            _issue(
                issues,
                "dataflow",
                "WORKFLOW_BINDING_NOT_DOMINATED",
                "Node output is not available on every path to the consumer",
                node_id=consumer_node_id,
            )
        return
    loop_id = consumer_scope[1]
    if source_scope == consumer_scope:
        if not body_dominators.get(loop_id or "", _DominatorIndex({}, {})).dominates(
            source_point,
            consumer_node_id,
        ):
            _issue(
                issues,
                "dataflow",
                "WORKFLOW_BINDING_NOT_DOMINATED",
                "Body node output is not available on every path to the consumer",
                node_id=consumer_node_id,
            )
    elif source_scope[0] == "root" and loop_id is not None:
        if not root_dominators.dominates(source_point, loop_id):
            _issue(
                issues,
                "dataflow",
                "WORKFLOW_BINDING_NOT_DOMINATED",
                "Root output is not available before entering this Loop",
                node_id=consumer_node_id,
            )
    else:
        _issue(
            issues,
            "dataflow",
            "WORKFLOW_BINDING_SCOPE_INVALID",
            "Binding crosses unrelated Loop scopes",
            node_id=consumer_node_id,
        )


def _aggregate_provenance(
    spec: WorkflowSpecV1,
    root_graph: _Graph,
    body_graphs: dict[str, _Graph],
    root_dominators: _DominatorIndex,
    body_dominators: dict[str, _DominatorIndex],
    scope_by_id: dict[str, tuple[str, str | None]],
    issues: list[WorkflowValidationIssue],
) -> tuple[WorkflowAggregateProvenance, ...]:
    graphs_by_scope: dict[tuple[str, str | None], _Graph] = {
        ("root", None): root_graph,
        **{("loop_body", loop_id): graph for loop_id, graph in body_graphs.items()},
    }
    dominators_by_scope: dict[tuple[str, str | None], _DominatorIndex] = {
        ("root", None): root_dominators,
        **{("loop_body", loop_id): dominators for loop_id, dominators in body_dominators.items()},
    }
    reachability_by_scope = {scope: graph.reachability() for scope, graph in graphs_by_scope.items()}
    conditioned_dominators: dict[tuple[tuple[str, str | None], str], _DominatorIndex] = {}
    success_points = {node.id: point for node in spec.nodes if (point := _success_outcome_point(node)) is not None}
    conditions_by_scope: dict[tuple[str, str | None], tuple[WorkflowNodeSpec, ...]] = {}
    for scope in graphs_by_scope:
        conditions_by_scope[scope] = tuple(
            sorted(
                (node for node in spec.nodes if node.type == "condition" and scope_by_id.get(node.id) == scope),
                key=lambda node: node.id,
            )
        )
    results: list[WorkflowAggregateProvenance] = []
    for aggregate in sorted((node for node in spec.nodes if node.type == "variable_aggregate"), key=lambda item: item.id):
        aggregate_scope = scope_by_id.get(aggregate.id)
        dominators = dominators_by_scope.get(aggregate_scope, _DominatorIndex({}, {}))
        reachability = reachability_by_scope.get(aggregate_scope, _ReachabilityIndex({}, {}))
        selected_condition: WorkflowNodeSpec | None = None
        group_ports: list[tuple[str, tuple[str, ...]]] = []
        for condition in conditions_by_scope.get(aggregate_scope, ()):
            if not dominators.dominates(condition.id, aggregate.id):
                continue
            expected_ports = tuple(branch.output_port_id for branch in condition.config.branches) + (condition.config.else_output_port_id,)  # type: ignore[union-attr]
            candidate_groups: list[tuple[str, tuple[str, ...]]] = []
            condition_valid = not reachability.reaches(
                _outcome_route_node_id(condition.id, "error"),
                aggregate.id,
            )
            if not condition_valid:
                continue
            for group in aggregate.config.groups:  # type: ignore[union-attr]
                ports: list[str] = []
                for input_id in group.candidate_input_ids:
                    binding = aggregate.input_bindings.get(input_id)
                    if not isinstance(binding, NodeOutputValueBinding):
                        condition_valid = False
                        continue
                    candidate_point = success_points.get(binding.node_id)
                    if candidate_point is None or binding.node_id == aggregate.id or not reachability.reaches(candidate_point, aggregate.id):
                        condition_valid = False
                        continue
                    matching = [
                        port
                        for port in expected_ports
                        if reachability.reaches(
                            _outcome_route_node_id(condition.id, port),
                            candidate_point,
                        )
                    ]
                    if len(matching) != 1:
                        condition_valid = False
                        continue
                    branch_port = matching[0]
                    branch_point = _outcome_route_node_id(condition.id, branch_port)
                    conditioned_key = (aggregate_scope, branch_point)
                    branch_dominators = conditioned_dominators.get(conditioned_key)
                    if branch_dominators is None:
                        branch_dominators = graphs_by_scope[aggregate_scope].dominators(branch_point)
                        conditioned_dominators[conditioned_key] = branch_dominators
                    if not branch_dominators.dominates(candidate_point, aggregate.id):
                        condition_valid = False
                        continue
                    ports.append(branch_port)
                if len(ports) != len(set(ports)) or set(ports) != set(expected_ports):
                    condition_valid = False
                candidate_groups.append((group.id, tuple(ports)))
            if condition_valid:
                selected_condition = condition
                group_ports = candidate_groups
                break
        if selected_condition is None:
            for group in aggregate.config.groups:  # type: ignore[union-attr]
                bindings = [aggregate.input_bindings.get(input_id) for input_id in group.candidate_input_ids]
                if any(not isinstance(binding, NodeOutputValueBinding) for binding in bindings):
                    code = "WORKFLOW_AGGREGATE_CANDIDATE_NOT_EXCLUSIVE"
                    message = "Aggregate candidates must be node outputs from mutually exclusive branches"
                else:
                    code = "WORKFLOW_AGGREGATE_BRANCH_AMBIGUOUS"
                    message = "Aggregate candidates do not cover distinct branches of one dominating Condition"
                _issue(issues, "dataflow", code, message, node_id=aggregate.id)
            continue
        results.append(
            WorkflowAggregateProvenance(
                node_id=aggregate.id,
                condition_node_id=selected_condition.id,
                group_branch_ports=tuple(group_ports),
            )
        )
    return tuple(results)


def _node_output_schemas(spec: WorkflowSpecV1) -> dict[tuple[str, str], dict[str, Any]]:
    schemas: dict[tuple[str, str], dict[str, Any]] = {}
    for node in spec.nodes:
        if node.type == "llm" and node.config.structured_output.enabled and node.config.structured_output.schema_ is not None:
            schemas[(node.id, "result")] = node.config.structured_output.schema_
        elif node.type == "transform" and node.config.mode == "json" and node.config.output_schema is not None:
            schemas[(node.id, "result")] = node.config.output_schema
        elif node.type == "http_request" and node.config.response.mode == "json" and node.config.response.schema_ is not None:
            schemas[(node.id, "body")] = node.config.response.schema_
        elif node.type == "python_code":
            schemas[(node.id, "result")] = node.config.output_schema
    return schemas


def _schema_values(spec: WorkflowSpecV1):
    for slot in spec.credential_slots:
        yield (None, slot.payload_schema, "object")
    for node in spec.nodes:
        if node.type == "llm" and node.config.structured_output.schema_ is not None:
            yield (node.id, node.config.structured_output.schema_, "object")
        elif node.type == "transform" and node.config.output_schema is not None:
            yield (node.id, node.config.output_schema, "any")
        elif node.type == "http_request" and node.config.response.schema_ is not None:
            yield (node.id, node.config.response.schema_, "any")
        elif node.type == "python_code":
            yield (node.id, node.config.output_schema, "object")


def _validate_schemas(spec: WorkflowSpecV1, issues: list[WorkflowValidationIssue]) -> None:
    for node_id, schema, requirement in _schema_values(spec):
        try:
            validate_strict_json_schema(schema)
            value_type_from_json_schema(
                schema,
                require_top_level=requirement,
            )
        except WorkflowJsonSchemaError as error:
            _issue(issues, "security", error.code, str(error), node_id=node_id)


def _compute_metrics(
    spec: WorkflowSpecV1,
    root_graph: _Graph,
    body_graphs: dict[str, _Graph],
) -> WorkflowMetrics:
    loop_nodes = [node for node in spec.nodes if node.type == "loop" and node.scope.kind == "root"]
    loop_weights: dict[str, int] = {}
    total_iterations = 0
    loop_activations = 0
    for loop in loop_nodes:
        body = body_graphs.get(loop.id, _Graph(frozenset(), ()))
        body_depth = body.longest_weighted_path({node_id: 1 for node_id in body.nodes})
        iterations = loop.config.max_iterations
        total_iterations += iterations
        supersteps = 2 + iterations * (body_depth + 2)
        activations = 2 + iterations * (len(body.nodes) + 2)
        loop_weights[loop.id] = supersteps
        loop_activations += activations
    weights = {node_id: loop_weights.get(node_id, 1) for node_id in root_graph.nodes}
    root_non_loop_non_body = sum(1 for node in spec.nodes if node.scope.kind == "root" and node.type != "loop")
    total_activations = root_non_loop_non_body + loop_activations
    fan_out_by_port = Counter((transition.source.node_id, transition.source.port_id) for transition in spec.transitions)
    return WorkflowMetrics(
        node_count=len(spec.nodes),
        edge_count=len(spec.transitions),
        depth=root_graph.longest_weighted_path({node_id: 1 for node_id in root_graph.nodes}),
        recursion_depth=root_graph.longest_weighted_path(weights),
        max_parallelism=max(
            (root_graph.max_layer_width(), *(body.max_layer_width() for body in body_graphs.values())),
            default=0,
        ),
        max_fan_out=max(fan_out_by_port.values(), default=0),
        loop_count=len(loop_nodes),
        total_iterations=total_iterations,
        total_steps=total_activations,
        total_activations=total_activations,
    )


def _validate_node_internal_identities(
    spec: WorkflowSpecV1,
    issues: list[WorkflowValidationIssue],
) -> None:
    for node in spec.nodes:
        if node.type == "llm":
            _validate_unique(
                list(node.config.context_input_ids),
                issues,
                phase="structure",
                code="WORKFLOW_LLM_CONTEXT_INPUT_ID_DUPLICATE",
                message="LLM context input ids must be unique",
                node_id=node.id,
            )
            _validate_unique(
                [message.id for message in node.config.messages],
                issues,
                phase="structure",
                code="WORKFLOW_LLM_MESSAGE_ID_DUPLICATE",
                message="LLM message ids must be unique",
                node_id=node.id,
            )
            if not node.config.messages:
                _issue(
                    issues,
                    "structure",
                    "WORKFLOW_LLM_MESSAGE_REQUIRED",
                    "LLM config requires at least one message",
                    node_id=node.id,
                )
            if node.config.mode == "completion" and (len(node.config.messages) != 1 or node.config.messages[0].role != "user"):
                _issue(
                    issues,
                    "structure",
                    "WORKFLOW_LLM_COMPLETION_SHAPE_INVALID",
                    "Completion mode requires exactly one user template",
                    node_id=node.id,
                )
            if node.config.mode == "chat":
                seen_non_system = False
                for message in node.config.messages:
                    if message.role != "system":
                        seen_non_system = True
                    elif seen_non_system:
                        _issue(
                            issues,
                            "structure",
                            "WORKFLOW_LLM_CHAT_ROLE_ORDER_INVALID",
                            "Chat system messages must precede user or assistant messages",
                            node_id=node.id,
                        )
                        break
            structured = node.config.structured_output
            if structured.enabled is (structured.schema_ is None):
                _issue(
                    issues,
                    "structure",
                    "WORKFLOW_LLM_STRUCTURED_OUTPUT_INVALID",
                    "Structured output schema is required exactly when structured output is enabled",
                    node_id=node.id,
                )
        elif node.type == "condition":
            _validate_unique(
                [branch.id for branch in node.config.branches],
                issues,
                phase="structure",
                code="WORKFLOW_CONDITION_BRANCH_ID_DUPLICATE",
                message="Condition branch ids must be unique",
                node_id=node.id,
            )
        elif node.type in {"transform", "python_code"}:
            variables = node.config.input_variables
            prefix = "TRANSFORM" if node.type == "transform" else "PYTHON"
            _validate_unique(
                [variable.id for variable in variables],
                issues,
                phase="structure",
                code=f"WORKFLOW_{prefix}_INPUT_ID_DUPLICATE",
                message=f"{prefix.title()} input variable ids must be unique",
                node_id=node.id,
            )
            _validate_unique(
                [variable.name for variable in variables],
                issues,
                phase="structure",
                code=f"WORKFLOW_{prefix}_INPUT_NAME_DUPLICATE",
                message=f"{prefix.title()} input variable names must be unique",
                node_id=node.id,
            )
        elif node.type == "variable_aggregate":
            for group in node.config.groups:
                _validate_unique(
                    list(group.candidate_input_ids),
                    issues,
                    phase="structure",
                    code="WORKFLOW_AGGREGATE_CANDIDATE_ID_DUPLICATE",
                    message="Aggregate candidate input ids must be unique within a group",
                    node_id=node.id,
                )
        elif node.type == "loop":
            variables = node.config.variables
            for attribute, code in (
                ("id", "WORKFLOW_LOOP_VARIABLE_ID_DUPLICATE"),
                ("name", "WORKFLOW_LOOP_VARIABLE_NAME_DUPLICATE"),
                ("initial_input_id", "WORKFLOW_LOOP_INITIAL_INPUT_ID_DUPLICATE"),
                ("next_input_id", "WORKFLOW_LOOP_NEXT_INPUT_ID_DUPLICATE"),
            ):
                _validate_unique(
                    [getattr(variable, attribute) for variable in variables],
                    issues,
                    phase="structure",
                    code=code,
                    message=f"Loop {attribute} values must be unique",
                    node_id=node.id,
                )
        elif node.type == "http_request":
            response = node.config.response
            if (response.mode == "json") is (response.schema_ is None):
                _issue(
                    issues,
                    "structure",
                    "WORKFLOW_HTTP_RESPONSE_SCHEMA_INVALID",
                    "HTTP JSON mode requires a schema and text mode forbids one",
                    node_id=node.id,
                )
            collections = [
                ("QUERY", list(node.config.query), False),
                ("HEADER", list(node.config.headers), True),
            ]
            if node.config.body.kind in {"form_urlencoded", "multipart_text"}:
                collections.append(("FORM_FIELD", list(node.config.body.fields), False))
            for label, entries, case_insensitive_name in collections:
                _validate_unique(
                    [entry.id for entry in entries],
                    issues,
                    phase="structure",
                    code=f"WORKFLOW_HTTP_{label}_ID_DUPLICATE",
                    message=f"HTTP {label.lower()} ids must be unique",
                    node_id=node.id,
                )
                names = [entry.name.lower() if case_insensitive_name else entry.name for entry in entries]
                _validate_unique(
                    names,
                    issues,
                    phase="structure",
                    code=f"WORKFLOW_HTTP_{label}_NAME_DUPLICATE",
                    message=f"HTTP {label.lower()} names must be unique",
                    node_id=node.id,
                )


def validate_workflow(
    spec: WorkflowSpecV1,
    *,
    limits: WorkflowCompilationLimits,
    registry: WorkflowNodeRegistry = FIRST_BATCH_RUNTIME_REGISTRY,
) -> WorkflowValidationResult:
    """Validate one strict Spec in a fixed deterministic phase order."""

    if not isinstance(spec, WorkflowSpecV1):
        raise TypeError("spec must be a strict WorkflowSpecV1")
    if not isinstance(limits, WorkflowCompilationLimits):
        raise TypeError("limits must be WorkflowCompilationLimits")
    issues: list[WorkflowValidationIssue] = []
    if len(spec.nodes) > _COMPILER_HARD_MAX_NODES:
        _issue(issues, "runtime_policy", "WORKFLOW_NODE_LIMIT_EXCEEDED", "Workflow node count exceeds the compiler hard limit")
    if len(spec.transitions) > _COMPILER_HARD_MAX_EDGES:
        _issue(issues, "runtime_policy", "WORKFLOW_EDGE_LIMIT_EXCEEDED", "Workflow edge count exceeds the compiler hard limit")
    if issues:
        return WorkflowValidationResult(
            issues=_sorted_issues(issues),
            metrics=WorkflowMetrics(
                node_count=len(spec.nodes),
                edge_count=len(spec.transitions),
                depth=0,
                recursion_depth=0,
                max_parallelism=0,
                max_fan_out=0,
                loop_count=sum(1 for node in spec.nodes if node.type == "loop"),
                total_iterations=0,
                total_steps=0,
                total_activations=0,
            ),
            aggregate_provenance=(),
        )
    node_by_id = {node.id: node for node in spec.nodes}
    root_graph, body_graphs, scope_by_id = _graphs(spec)
    ports = _port_maps(spec, registry, issues)
    output_ports = {node_id: values[1] for node_id, values in ports.items()}
    output_schemas = _node_output_schemas(spec)

    transition_ids = [transition.id for transition in spec.transitions]
    for transition_id in _duplicates(transition_ids):
        _issue(issues, "structure", "WORKFLOW_TRANSITION_ID_DUPLICATE", "Transition ids must be unique", transition_id=transition_id)
    _validate_unique(
        [declaration.id for declaration in spec.workflow_inputs],
        issues,
        phase="structure",
        code="WORKFLOW_INPUT_ID_DUPLICATE",
        message="Workflow input ids must be unique",
    )
    _validate_unique(
        [declaration.id for declaration in spec.workflow_outputs],
        issues,
        phase="structure",
        code="WORKFLOW_OUTPUT_ID_DUPLICATE",
        message="Workflow output ids must be unique",
    )
    _validate_node_internal_identities(spec, issues)
    _validate_value_types(spec, issues)
    _validate_declaration_defaults_and_constraints(spec, issues)
    starts = [node for node in spec.nodes if node.type == "start" and node.scope.kind == "root"]
    ends = [node for node in spec.nodes if node.type == "end" and node.scope.kind == "root"]
    if len(starts) != 1:
        _issue(issues, "structure", "WORKFLOW_START_COUNT_INVALID", "Published Workflow must contain exactly one Start")
    if not ends:
        _issue(issues, "structure", "WORKFLOW_END_REQUIRED", "Published Workflow must contain at least one End")
    if len(starts) == 1 and spec.entry_node_id != starts[0].id:
        _issue(issues, "structure", "WORKFLOW_ENTRY_NOT_START", "entry_node_id must identify the unique Start")

    loop_by_id = {node.id: node for node in spec.nodes if node.type == "loop"}
    for node in spec.nodes:
        if node.scope.kind == "loop_body" and node.type in {"start", "end"}:
            _issue(
                issues,
                "topology",
                "WORKFLOW_LOOP_BODY_TERMINAL_FORBIDDEN",
                "Start and End nodes are root-only and cannot appear in a Loop body",
                node_id=node.id,
            )
        if node.type == "loop" and node.scope.kind != "root":
            _issue(issues, "topology", "WORKFLOW_NESTED_LOOP_FORBIDDEN", "Nested Loop nodes are not supported", node_id=node.id)
        if node.scope.kind == "loop_body" and node.scope.loop_node_id not in loop_by_id:
            _issue(
                issues,
                "topology",
                "WORKFLOW_LOOP_BODY_OWNER_UNKNOWN",
                "Loop body scope references a missing Loop",
                node_id=node.id,
            )

    semantic_edges: set[tuple[str, str, str, str]] = set()
    source_counts: dict[tuple[str, str], int] = defaultdict(int)
    incoming_counts: dict[str, int] = defaultdict(int)
    outgoing_counts: dict[str, int] = defaultdict(int)
    for transition in spec.transitions:
        source_node = node_by_id.get(transition.source.node_id)
        target_node = node_by_id.get(transition.target.node_id)
        if source_node is None:
            _issue(issues, "structure", "WORKFLOW_TRANSITION_SOURCE_UNKNOWN", "Transition source node is unknown", transition_id=transition.id)
            continue
        if target_node is None:
            _issue(issues, "structure", "WORKFLOW_TRANSITION_TARGET_UNKNOWN", "Transition target node is unknown", transition_id=transition.id)
            continue
        source_scope = scope_by_id[source_node.id]
        target_scope = scope_by_id[target_node.id]
        if source_node.type == "loop" and transition.source.port_id == "body":
            _issue(
                issues,
                "topology",
                "WORKFLOW_LOOP_BODY_ROUTE_AUTHORED",
                "Loop body entry is Compiler-managed and cannot be authored as a transition",
                transition_id=transition.id,
                node_id=source_node.id,
                port_id="body",
            )
        if source_scope != target_scope:
            _issue(
                issues,
                "topology",
                "WORKFLOW_CROSS_SCOPE_TRANSITION",
                "Authored transitions cannot cross root/Loop body scopes",
                transition_id=transition.id,
            )
        source_port = ports.get(source_node.id, ({}, {}))[1].get(transition.source.port_id)
        if source_port is None or source_port.kind != "control":
            _issue(
                issues,
                "port",
                "WORKFLOW_SOURCE_PORT_UNKNOWN",
                "Transition source must reference a resolved control output",
                transition_id=transition.id,
                node_id=source_node.id,
                port_id=transition.source.port_id,
            )
        target_port = ports.get(target_node.id, ({}, {}))[0].get(transition.target.port_id)
        if target_port is None or target_port.kind != "control":
            _issue(
                issues,
                "port",
                "WORKFLOW_TARGET_PORT_UNKNOWN",
                "Transition target must reference a resolved control input",
                transition_id=transition.id,
                node_id=target_node.id,
                port_id=transition.target.port_id,
            )
        identity = (source_node.id, transition.source.port_id, target_node.id, transition.target.port_id)
        if identity in semantic_edges:
            _issue(
                issues,
                "port",
                "WORKFLOW_CONTROL_EDGE_DUPLICATE",
                "Duplicate semantic control edges are forbidden",
                transition_id=transition.id,
            )
        semantic_edges.add(identity)
        source_counts[(source_node.id, transition.source.port_id)] += 1
        incoming_counts[target_node.id] += 1
        outgoing_counts[source_node.id] += 1

    for node_id, (_inputs, node_outputs) in ports.items():
        node = node_by_id[node_id]
        for port in node_outputs.values():
            count = source_counts[(node_id, port.id)]
            if port.kind == "control" and port.cardinality == "one" and count > 1:
                _issue(
                    issues,
                    "port",
                    "WORKFLOW_SOURCE_PORT_CARDINALITY",
                    "Control output exceeds its one-edge cardinality",
                    node_id=node_id,
                    port_id=port.id,
                )
            compiler_managed = node.type == "loop" and port.id == "body"
            body_exit = node.scope.kind == "loop_body" and any(loop.config.body_exit_node_id == node.id for loop in loop_by_id.values() if loop.scope.kind == "root")
            if port.kind == "control" and port.required and count == 0 and not compiler_managed and not body_exit:
                _issue(
                    issues,
                    "routing",
                    "WORKFLOW_REQUIRED_ROUTE_MISSING",
                    "Required control route has no target",
                    node_id=node_id,
                    port_id=port.id,
                )
        body_entry = node.scope.kind == "loop_body" and any(loop.config.body_entry_node_id == node.id for loop in loop_by_id.values() if loop.scope.kind == "root")
        if node.type != "start" and incoming_counts[node_id] == 0 and not body_entry:
            _issue(issues, "topology", "WORKFLOW_NODE_NO_INCOMING", "Node has no control predecessor", node_id=node_id)
        if node.type == "start" and incoming_counts[node_id] != 0:
            _issue(issues, "topology", "WORKFLOW_START_HAS_INCOMING", "Start cannot have an incoming transition", node_id=node_id)
        if node.type == "end" and outgoing_counts[node_id] != 0:
            _issue(issues, "topology", "WORKFLOW_END_HAS_OUTGOING", "End cannot have an outgoing transition", node_id=node_id)

    for node in spec.nodes:
        node_outputs = ports.get(node.id, ({}, {}))[1]
        on_error = node.execution_policy.on_error
        if on_error.mode == "route_error":
            error_port = node_outputs.get(on_error.output_port_id)
            if error_port is None or error_port.kind != "control":
                _issue(
                    issues,
                    "runtime_policy",
                    "WORKFLOW_ERROR_ROUTE_UNSUPPORTED",
                    "Node execution policy cannot route errors without an error control port",
                    node_id=node.id,
                    port_id=on_error.output_port_id,
                )
            elif source_counts[(node.id, on_error.output_port_id)] == 0:
                _issue(
                    issues,
                    "runtime_policy",
                    "WORKFLOW_ERROR_ROUTE_MISSING",
                    "route_error requires the declared error control port to be connected",
                    node_id=node.id,
                    port_id=on_error.output_port_id,
                )
        elif source_counts[(node.id, "error")] > 0:
            _issue(
                issues,
                "runtime_policy",
                "WORKFLOW_ERROR_ROUTE_UNEXPECTED",
                "An authored error route requires on_error=route_error",
                node_id=node.id,
                port_id="error",
            )
        elif on_error.mode == "continue_with_typed_default":
            _issue(
                issues,
                "runtime_policy",
                "WORKFLOW_TYPED_DEFAULT_UNSUPPORTED",
                "First-batch nodes do not support continue_with_typed_default",
                node_id=node.id,
            )
        retry = node.execution_policy.retry
        if retry.mode == "bounded":
            try:
                semantics = registry.require(node.type, node.type_version).shared.retry_semantics
            except WorkflowNodeRegistryError:
                continue
            if semantics == "http_method_v1" and node.type == "http_request":
                if node.config.method not in {"GET", "HEAD"}:
                    _issue(
                        issues,
                        "runtime_policy",
                        "WORKFLOW_HTTP_WRITE_RETRY_UNSUPPORTED",
                        "Authored retry cannot make an HTTP write method retryable",
                        node_id=node.id,
                    )
            elif semantics not in {"read", "isolated_compute", "idempotent_write"}:
                _issue(
                    issues,
                    "runtime_policy",
                    "WORKFLOW_RETRY_UNSUPPORTED",
                    "Bounded retry is incompatible with the node Registry retry semantics",
                    node_id=node.id,
                )

    if root_graph.topological() is None:
        _issue(issues, "topology", "WORKFLOW_AUTHORED_CYCLE", "Root authored transitions must form a DAG")
    for loop_id, graph in sorted(body_graphs.items()):
        if graph.topological() is None:
            _issue(
                issues,
                "topology",
                "WORKFLOW_AUTHORED_CYCLE",
                "Loop body authored transitions must form a DAG",
                node_id=loop_id,
            )

    root_reachable = root_graph.reachable((spec.entry_node_id,)) if spec.entry_node_id in root_graph.nodes else frozenset()
    can_reach_end = root_graph.reachable(tuple(node.id for node in ends), reverse=True)
    for node_id in sorted(root_graph.nodes - root_reachable):
        _issue(issues, "topology", "WORKFLOW_NODE_UNREACHABLE", "Root node is unreachable from Start", node_id=node_id)
    for node_id in sorted(root_graph.nodes - can_reach_end):
        _issue(issues, "topology", "WORKFLOW_NODE_CANNOT_REACH_END", "Root node cannot reach an End", node_id=node_id)

    for loop_id, loop in sorted(loop_by_id.items()):
        if loop.scope.kind != "root":
            continue
        graph = body_graphs.get(loop_id)
        if graph is None or not graph.nodes:
            _issue(issues, "topology", "WORKFLOW_LOOP_BODY_REQUIRED", "Loop must own a non-empty body", node_id=loop_id)
            continue
        entry = loop.config.body_entry_node_id
        exit_id = loop.config.body_exit_node_id
        if entry not in graph.nodes or exit_id not in graph.nodes:
            _issue(
                issues,
                "topology",
                "WORKFLOW_LOOP_ENTRY_EXIT_INVALID",
                "Loop entry and exit must belong to its body",
                node_id=loop_id,
            )
            continue
        reachable = graph.reachable((entry,))
        reaches_exit = graph.reachable((exit_id,), reverse=True)
        if reachable != graph.nodes or reaches_exit != graph.nodes:
            _issue(
                issues,
                "topology",
                "WORKFLOW_LOOP_BODY_NOT_SINGLE_ENTRY_EXIT",
                "Every Loop body node must be between the declared entry and exit",
                node_id=loop_id,
            )

    outcome_root_graph, outcome_body_graphs = _outcome_graphs(spec, ports, scope_by_id)
    root_dominators = outcome_root_graph.dominators(spec.entry_node_id)
    body_dominators = {
        loop_id: graph.dominators(loop_by_id[loop_id].config.body_entry_node_id)  # type: ignore[union-attr]
        for loop_id, graph in outcome_body_graphs.items()
        if loop_id in loop_by_id and loop_by_id[loop_id].type == "loop"
    }
    success_points = {node.id: point for node in spec.nodes if (point := _success_outcome_point(node)) is not None}
    body_exit_points = {loop_id: _body_exit_completion_points(loop, node_by_id) for loop_id, loop in loop_by_id.items() if loop.scope.kind == "root"}
    input_types = {item.id: item.value_type for item in spec.workflow_inputs}
    loop_variable_types = {(node.id, variable.id): variable.value_type for node in spec.nodes if node.type == "loop" for variable in node.config.variables}

    expected_bindings: dict[str, dict[str, tuple[WorkflowValueType | None, str]]] = defaultdict(dict)
    for node in spec.nodes:
        if node.type == "llm":
            expected_bindings[node.id].update((input_id, (None, "normal")) for input_id in node.config.context_input_ids)
        elif node.type == "transform":
            expected_bindings[node.id].update((item.id, (item.value_type, "normal")) for item in node.config.input_variables)
        elif node.type == "variable_aggregate":
            for group in node.config.groups:
                for input_id in group.candidate_input_ids:
                    expected_bindings[node.id][input_id] = (group.value_type, "aggregate")
        elif node.type == "loop":
            for variable in node.config.variables:
                expected_bindings[node.id][variable.initial_input_id] = (variable.value_type, "loop_initial")
                expected_bindings[node.id][variable.next_input_id] = (variable.value_type, "loop_next")
        elif node.type == "python_code":
            expected_bindings[node.id].update((item.id, (item.value_type, "normal")) for item in node.config.input_variables)

    for node in spec.nodes:
        expected = expected_bindings[node.id]
        for input_id in sorted(set(node.input_bindings) - set(expected)):
            _issue(
                issues,
                "dataflow",
                "WORKFLOW_INPUT_BINDING_UNKNOWN",
                "Node input binding key is not declared by the node config",
                node_id=node.id,
                port_id=input_id,
            )
        for input_id, (target_type, usage) in expected.items():
            binding = node.input_bindings.get(input_id)
            if binding is None:
                _issue(
                    issues,
                    "dataflow",
                    "WORKFLOW_INPUT_BINDING_REQUIRED",
                    "Declared node input requires a binding",
                    node_id=node.id,
                    port_id=input_id,
                )
                continue
            source_type = _binding_source_type(
                binding,
                input_types=input_types,
                output_ports=output_ports,
                output_schemas=output_schemas,
                loop_variable_types=loop_variable_types,
                issues=issues,
                node_id=node.id,
            )
            if target_type is not None and source_type is not None and not _type_compatible(source_type, target_type):
                _issue(
                    issues,
                    "dataflow",
                    "WORKFLOW_BINDING_TYPE_MISMATCH",
                    "Binding value type is incompatible with the declared input",
                    node_id=node.id,
                    port_id=input_id,
                )
            if usage != "aggregate":
                _validate_binding_scope_and_dominance(
                    binding,
                    consumer_node_id=node.id,
                    usage=usage,
                    scope_by_id=scope_by_id,
                    root_dominators=root_dominators,
                    body_dominators=body_dominators,
                    success_points=success_points,
                    body_exit_points=body_exit_points,
                    loop_by_id=loop_by_id,
                    issues=issues,
                )

        for binding in _iter_nested_values(node.config):
            _binding_source_type(
                binding,
                input_types=input_types,
                output_ports=output_ports,
                output_schemas=output_schemas,
                loop_variable_types=loop_variable_types,
                issues=issues,
                node_id=node.id,
            )
            usage = "loop_termination" if node.type == "loop" and binding in tuple(_iter_nested_values(node.config.termination_condition)) else "normal"
            _validate_binding_scope_and_dominance(
                binding,
                consumer_node_id=node.id,
                usage=usage,
                scope_by_id=scope_by_id,
                root_dominators=root_dominators,
                body_dominators=body_dominators,
                success_points=success_points,
                body_exit_points=body_exit_points,
                loop_by_id=loop_by_id,
                issues=issues,
            )
        if node.type == "condition":
            for branch in node.config.branches:
                _validate_predicate(branch.predicate, issues, node_id=node.id)
                _validate_predicate_types(
                    branch.predicate,
                    issues,
                    node_id=node.id,
                    input_types=input_types,
                    output_ports=output_ports,
                    output_schemas=output_schemas,
                    loop_variable_types=loop_variable_types,
                )
        elif node.type == "loop":
            _validate_predicate(node.config.termination_condition, issues, node_id=node.id)
            _validate_predicate_types(
                node.config.termination_condition,
                issues,
                node_id=node.id,
                input_types=input_types,
                output_ports=output_ports,
                output_schemas=output_schemas,
                loop_variable_types=loop_variable_types,
            )
        elif node.type == "transform" and isinstance(node.config.template, RestrictedJsonTemplate):
            _validate_json_template(node.config.template, issues, node_id=node.id)
        elif node.type == "http_request":
            if isinstance(node.config.body, object) and hasattr(node.config.body, "template") and isinstance(node.config.body.template, RestrictedJsonTemplate):
                _validate_json_template(node.config.body.template, issues, node_id=node.id)

    aggregate_provenance = _aggregate_provenance(
        spec,
        outcome_root_graph,
        outcome_body_graphs,
        root_dominators,
        body_dominators,
        scope_by_id,
        issues,
    )

    for output in spec.workflow_outputs:
        if output.source is None:
            if not output.value_type.nullable and "default" not in output.model_fields_set:
                _issue(
                    issues,
                    "output",
                    "WORKFLOW_OUTPUT_UNBOUND",
                    "Published Workflow output must be bound or declare nullable/default fallback semantics",
                    port_id=output.id,
                )
            continue
        source_type = _binding_source_type(
            output.source,
            input_types=input_types,
            output_ports=output_ports,
            output_schemas=output_schemas,
            loop_variable_types=loop_variable_types,
            issues=issues,
            node_id=None,
        )
        if source_type is not None and not _type_compatible(source_type, output.value_type):
            _issue(issues, "output", "WORKFLOW_OUTPUT_TYPE_MISMATCH", "Workflow output binding type is incompatible", port_id=output.id)
        _validate_binding_scope_and_dominance(
            output.source,
            consumer_node_id=None,
            usage="workflow_output",
            scope_by_id=scope_by_id,
            root_dominators=root_dominators,
            body_dominators=body_dominators,
            success_points=success_points,
            body_exit_points=body_exit_points,
            loop_by_id=loop_by_id,
            issues=issues,
        )
        needs_value_on_every_path = not output.value_type.nullable and "default" not in output.model_fields_set
        if needs_value_on_every_path and isinstance(output.source, NodeOutputValueBinding) and scope_by_id.get(output.source.node_id, (None, None))[0] == "root":
            source_point = success_points.get(output.source.node_id)
            if source_point is None or any(not root_dominators.dominates(source_point, end.id) for end in ends):
                _issue(
                    issues,
                    "output",
                    "WORKFLOW_OUTPUT_NOT_AVAILABLE_ON_ALL_PATHS",
                    "Workflow output source is not available on every ending path",
                    port_id=output.id,
                )

    slot_ids = [slot.id for slot in spec.credential_slots]
    for duplicate in _duplicates(slot_ids):
        _issue(issues, "structure", "WORKFLOW_CREDENTIAL_SLOT_DUPLICATE", "Credential slot ids must be unique", port_id=duplicate)
    referenced_slots: set[str] = set()
    for node in spec.nodes:
        if node.type != "http_request":
            continue
        try:
            parsed = urlsplit(node.config.base_origin)
            parsed_port = parsed.port
            origin_invalid = (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.path not in {"", "/"}
                or bool(parsed.query)
                or bool(parsed.fragment)
                or parsed.username is not None
                or parsed.password is not None
                or (parsed_port is not None and not 1 <= parsed_port <= 65_535)
            )
        except ValueError:
            origin_invalid = True
        if origin_invalid:
            _issue(issues, "security", "WORKFLOW_HTTP_ORIGIN_INVALID", "HTTP base_origin must be a literal HTTPS origin", node_id=node.id)
        text_segments = node.config.path_template.segments
        if not text_segments or text_segments[0].kind != "text" or not text_segments[0].value.startswith("/"):
            _issue(issues, "security", "WORKFLOW_HTTP_PATH_INVALID", "HTTP path template must begin with a literal slash", node_id=node.id)
        if node.config.method in {"GET", "HEAD"} and node.config.body.kind != "none":
            _issue(issues, "security", "WORKFLOW_HTTP_BODY_FORBIDDEN", "GET and HEAD requests cannot carry a body", node_id=node.id)
        for header in node.config.headers:
            if header.name.lower() in _DANGEROUS_HTTP_HEADERS or header.name.lower().startswith("proxy-"):
                _issue(issues, "security", "WORKFLOW_HTTP_HEADER_FORBIDDEN", "HTTP header is transport-controlled", node_id=node.id)
        if node.config.auth.mode == "endpoint_profile":
            referenced_slots.add(node.config.auth.credential_slot_id)
            if node.config.auth.credential_slot_id not in slot_ids:
                _issue(
                    issues,
                    "dataflow",
                    "WORKFLOW_CREDENTIAL_SLOT_UNKNOWN",
                    "HTTP auth references an unknown Credential slot declaration",
                    node_id=node.id,
                )
    for slot_id in sorted(set(slot_ids) - referenced_slots):
        _issue(
            issues,
            "dataflow",
            "WORKFLOW_CREDENTIAL_SLOT_UNUSED",
            "Credential slots may be declared only for HTTP auth references",
            port_id=slot_id,
        )
    _validate_schemas(spec, issues)

    metrics = _compute_metrics(spec, root_graph, body_graphs)
    if metrics.node_count > limits.max_nodes:
        _issue(issues, "runtime_policy", "WORKFLOW_NODE_LIMIT_EXCEEDED", "Workflow node count exceeds policy")
    if metrics.edge_count > limits.max_edges:
        _issue(issues, "runtime_policy", "WORKFLOW_EDGE_LIMIT_EXCEEDED", "Workflow edge count exceeds policy")
    if metrics.depth > limits.max_depth:
        _issue(issues, "runtime_policy", "WORKFLOW_DEPTH_LIMIT_EXCEEDED", "Workflow depth exceeds policy")
    if metrics.recursion_depth > limits.max_recursion_depth:
        _issue(
            issues,
            "runtime_policy",
            "WORKFLOW_RECURSION_DEPTH_LIMIT_EXCEEDED",
            "Worst-case compiled Workflow recursion depth exceeds policy",
        )
    if metrics.max_parallelism > limits.max_parallelism:
        _issue(
            issues,
            "runtime_policy",
            "WORKFLOW_PARALLELISM_LIMIT_EXCEEDED",
            "Static Workflow parallelism upper bound exceeds policy",
        )
    if metrics.max_fan_out > limits.max_fan_out:
        _issue(
            issues,
            "runtime_policy",
            "WORKFLOW_FAN_OUT_LIMIT_EXCEEDED",
            "Workflow control-port fan-out exceeds policy",
        )
    if metrics.loop_count > limits.max_loops:
        _issue(issues, "runtime_policy", "WORKFLOW_LOOP_COUNT_LIMIT_EXCEEDED", "Workflow Loop count exceeds policy")
    if metrics.total_iterations > limits.max_total_iterations:
        _issue(issues, "runtime_policy", "WORKFLOW_TOTAL_ITERATION_LIMIT_EXCEEDED", "Worst-case total Loop iterations exceed policy")
    if metrics.total_steps > limits.max_total_steps:
        _issue(issues, "runtime_policy", "WORKFLOW_TOTAL_STEP_LIMIT_EXCEEDED", "Worst-case Workflow steps exceed policy")
    if metrics.total_activations > limits.max_total_activations:
        _issue(issues, "runtime_policy", "WORKFLOW_TOTAL_ACTIVATION_LIMIT_EXCEEDED", "Worst-case Workflow activations exceed policy")
    for loop in loop_by_id.values():
        if loop.scope.kind != "root":
            continue
        body = body_graphs.get(loop.id, _Graph(frozenset(), ()))
        if len(body.nodes) > limits.max_loop_body_nodes:
            _issue(issues, "runtime_policy", "WORKFLOW_LOOP_BODY_NODE_LIMIT_EXCEEDED", "Loop body node count exceeds policy", node_id=loop.id)
        if len(body.edges) > limits.max_loop_body_edges:
            _issue(issues, "runtime_policy", "WORKFLOW_LOOP_BODY_EDGE_LIMIT_EXCEEDED", "Loop body edge count exceeds policy", node_id=loop.id)
        if loop.config.max_iterations > limits.max_loop_iterations:
            _issue(
                issues,
                "runtime_policy",
                "WORKFLOW_LOOP_ITERATION_LIMIT_EXCEEDED",
                "Loop max_iterations exceeds policy",
                node_id=loop.id,
            )
    for node in spec.nodes:
        if node.type != "variable_aggregate":
            continue
        if len(node.config.groups) > limits.max_aggregate_groups:
            _issue(issues, "runtime_policy", "WORKFLOW_AGGREGATE_GROUP_LIMIT_EXCEEDED", "Aggregate group count exceeds policy", node_id=node.id)
        if any(len(group.candidate_input_ids) > limits.max_aggregate_candidates for group in node.config.groups):
            _issue(
                issues,
                "runtime_policy",
                "WORKFLOW_AGGREGATE_CANDIDATE_LIMIT_EXCEEDED",
                "Aggregate candidate count exceeds policy",
                node_id=node.id,
            )

    return WorkflowValidationResult(
        issues=_sorted_issues(issues),
        metrics=metrics,
        aggregate_provenance=aggregate_provenance,
    )


def validate_canvas_document(
    spec: WorkflowSpecV1,
    canvas: CanvasDocumentV1,
) -> WorkflowValidationResult:
    """Validate Canvas identity and Loop parent projection without geometry inference."""

    if not isinstance(spec, WorkflowSpecV1) or not isinstance(canvas, CanvasDocumentV1):
        raise TypeError("strict WorkflowSpecV1 and CanvasDocumentV1 values are required")
    issues: list[WorkflowValidationIssue] = []
    node_ids = [layout.node_id for layout in canvas.node_layouts]
    edge_ids = [layout.edge_id for layout in canvas.edge_layouts]
    spec_node_ids = {node.id for node in spec.nodes}
    spec_edge_ids = {edge.id for edge in spec.transitions}
    for duplicate in _duplicates(node_ids):
        _issue(issues, "canvas", "WORKFLOW_CANVAS_NODE_LAYOUT_DUPLICATE", "Canvas node layouts must be unique", node_id=duplicate)
    for duplicate in _duplicates(edge_ids):
        _issue(issues, "canvas", "WORKFLOW_CANVAS_EDGE_LAYOUT_DUPLICATE", "Canvas edge layouts must be unique", transition_id=duplicate)
    if set(node_ids) != spec_node_ids:
        _issue(issues, "canvas", "WORKFLOW_CANVAS_NODE_IDENTITY_MISMATCH", "Canvas must project exactly the Spec node identities")
    if set(edge_ids) != spec_edge_ids:
        _issue(issues, "canvas", "WORKFLOW_CANVAS_EDGE_IDENTITY_MISMATCH", "Canvas must project exactly the Spec edge identities")
    node_by_id = {node.id: node for node in spec.nodes}
    seen: set[str] = set()
    for layout in canvas.node_layouts:
        node = node_by_id.get(layout.node_id)
        if node is None:
            continue
        expected_parent = node.scope.loop_node_id if node.scope.kind == "loop_body" else None
        if layout.parent_node_id != expected_parent:
            _issue(
                issues,
                "canvas",
                "WORKFLOW_CANVAS_LOOP_PARENT_MISMATCH",
                "Canvas parent_node_id must be the exact projection of Spec Loop scope",
                node_id=layout.node_id,
            )
        if expected_parent is not None and expected_parent not in seen:
            _issue(
                issues,
                "canvas",
                "WORKFLOW_CANVAS_PARENT_ORDER_INVALID",
                "Loop parent layout must precede its body children",
                node_id=layout.node_id,
            )
        seen.add(layout.node_id)
    empty_metrics = WorkflowMetrics(
        node_count=0,
        edge_count=0,
        depth=0,
        recursion_depth=0,
        max_parallelism=0,
        max_fan_out=0,
        loop_count=0,
        total_iterations=0,
        total_steps=0,
        total_activations=0,
    )
    return WorkflowValidationResult(
        issues=_sorted_issues(issues),
        metrics=empty_metrics,
        aggregate_provenance=(),
    )


__all__ = [
    "WorkflowAggregateProvenance",
    "WorkflowCompilationLimits",
    "WorkflowMetrics",
    "WorkflowValidationError",
    "WorkflowValidationIssue",
    "WorkflowValidationResult",
    "validate_canvas_document",
    "validate_workflow",
]
