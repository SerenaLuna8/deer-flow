"""Versioned, authority-free WorkflowSpec v1 to immutable IR compiler."""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Final

from deerflow.workflows.canonical import semantic_checksum
from deerflow.workflows.catalog_contracts import PortDefinition
from deerflow.workflows.compiler.cache import CompilerCacheKey, WorkflowCompilerCache
from deerflow.workflows.compiler.ir import (
    CompiledAggregate,
    CompiledAggregateGroup,
    CompiledBranch,
    CompiledBranchRoute,
    CompiledEdge,
    CompiledEndpoint,
    CompiledLoopRegion,
    CompiledLoopVariable,
    CompiledNode,
    CompiledOutcomeRoute,
    CompiledPort,
    CompiledTransform,
    CompiledValueType,
    CompiledWorkflowOutput,
    FrozenObject,
    WorkflowIR,
    freeze_json,
)
from deerflow.workflows.contracts import WorkflowNodeSpec, WorkflowSpecV1, WorkflowValueType
from deerflow.workflows.registry import FIRST_BATCH_RUNTIME_REGISTRY, WorkflowNodeRegistry, resolve_node_ports
from deerflow.workflows.validation import WorkflowCompilationLimits, WorkflowValidationResult, validate_workflow

GRAPH_SCHEMA_VERSION_V1: Final = 1
COMPILER_CONTRACT_VERSION_V1: Final = 1
CURRENT_COMPILER_CONTRACT_VERSION: Final = COMPILER_CONTRACT_VERSION_V1


class WorkflowCompilerUnavailableError(LookupError):
    """The exact compiler contract frozen by a Version/Run is unavailable."""


def _frozen_object(value: object) -> FrozenObject:
    frozen = freeze_json(value)
    if not isinstance(frozen, FrozenObject):
        raise TypeError("Workflow compiler expected a JSON object")
    return frozen


def _compiled_value_type(value: WorkflowValueType | None) -> CompiledValueType | None:
    if value is None:
        return None
    return CompiledValueType(
        kind=value.kind,
        collection=value.collection,
        nullable=value.nullable,
        schema_ref=value.schema_ref,
    )


def _compiled_port(port: PortDefinition, direction: str) -> CompiledPort:
    return CompiledPort(
        id=port.id,
        kind=port.kind,
        direction=direction,  # type: ignore[arg-type]
        cardinality=port.cardinality,
        required=port.required,
        value_type=_compiled_value_type(port.value_type),
    )


def _scope_path(node: WorkflowNodeSpec) -> tuple[str, ...]:
    if node.scope.kind == "root":
        return ("root",)
    return ("root", f"loop:{node.scope.loop_node_id}")


def _schema_for_value_type(value: WorkflowValueType) -> dict[str, object]:
    primitive: dict[str, object]
    if value.kind == "string":
        primitive = {"type": "string"}
    elif value.kind == "number":
        primitive = {"type": "number"}
    elif value.kind == "boolean":
        primitive = {"type": "boolean"}
    elif value.kind == "messages":
        primitive = {"type": "array", "items": {"type": "object"}}
    else:
        primitive = {"not": {"type": ["null", "array"]}}
    if value.collection and value.kind != "messages":
        primitive = {"type": "array", "items": {} if value.kind == "json" else primitive}
    if value.nullable:
        primitive = {"anyOf": [primitive, {"type": "null"}]}
    if value.schema_ref is not None:
        primitive["x-actweave-schema-ref"] = value.schema_ref
    return primitive


def _input_property_schema(declaration: object) -> dict[str, object]:
    schema = _schema_for_value_type(declaration.value_type)
    constraints = declaration.constraints
    target = schema
    if declaration.value_type.nullable:
        target = schema["anyOf"][0]
    if constraints.kind == "string":
        if constraints.min_length is not None:
            target["minLength"] = constraints.min_length
        if constraints.max_length is not None:
            target["maxLength"] = constraints.max_length
    elif constraints.kind == "number":
        if constraints.minimum is not None:
            target["minimum"] = constraints.minimum
        if constraints.maximum is not None:
            target["maximum"] = constraints.maximum
    elif constraints.kind == "enum":
        target["enum"] = list(constraints.options)
    if "default" in declaration.model_fields_set:
        schema["default"] = declaration.default
    return schema


def _input_schema(spec: WorkflowSpecV1) -> FrozenObject:
    properties = {item.id: _input_property_schema(item) for item in spec.workflow_inputs}
    required = [item.id for item in spec.workflow_inputs if item.required and "default" not in item.model_fields_set]
    return _frozen_object(
        {
            "type": "object",
            "properties": properties,
            "required": sorted(required),
            "additionalProperties": False,
        }
    )


def _output_schema(spec: WorkflowSpecV1) -> FrozenObject:
    properties: dict[str, dict[str, object]] = {}
    for item in spec.workflow_outputs:
        schema = _schema_for_value_type(item.value_type)
        if "default" in item.model_fields_set:
            schema["default"] = item.default
        properties[item.id] = schema
    required = [item.id for item in spec.workflow_outputs if not item.value_type.nullable and "default" not in item.model_fields_set]
    return _frozen_object(
        {
            "type": "object",
            "properties": properties,
            "required": sorted(required),
            "additionalProperties": False,
        }
    )


def _compiled_workflow_outputs(spec: WorkflowSpecV1) -> tuple[CompiledWorkflowOutput, ...]:
    outputs: list[CompiledWorkflowOutput] = []
    for declaration in sorted(spec.workflow_outputs, key=lambda item: item.id):
        value_type = _compiled_value_type(declaration.value_type)
        assert value_type is not None
        has_default = "default" in declaration.model_fields_set
        outputs.append(
            CompiledWorkflowOutput(
                id=declaration.id,
                value_type=value_type,
                source=(
                    None
                    if declaration.source is None
                    else _frozen_object(
                        declaration.source.model_dump(
                            mode="json",
                            by_alias=True,
                            exclude_unset=True,
                        )
                    )
                ),
                has_default=has_default,
                default=freeze_json(declaration.default) if has_default else None,
            )
        )
    return tuple(outputs)


def _topological_rank(spec: WorkflowSpecV1) -> dict[str, int]:
    root_nodes = {node.id for node in spec.nodes if node.scope.kind == "root"}
    outgoing: dict[str, list[str]] = {node_id: [] for node_id in root_nodes}
    indegree = {node_id: 0 for node_id in root_nodes}
    for transition in spec.transitions:
        if transition.source.node_id in root_nodes and transition.target.node_id in root_nodes:
            outgoing[transition.source.node_id].append(transition.target.node_id)
            indegree[transition.target.node_id] += 1
    ready = [node_id for node_id, value in indegree.items() if value == 0]
    heapq.heapify(ready)
    ordered: list[str] = []
    while ready:
        current = heapq.heappop(ready)
        ordered.append(current)
        for target in sorted(outgoing[current]):
            indegree[target] -= 1
            if indegree[target] == 0:
                heapq.heappush(ready, target)
    return {node_id: index for index, node_id in enumerate(ordered)}


def _body_depth(node_ids: tuple[str, ...], edges: tuple[tuple[str, str], ...]) -> int:
    outgoing: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    indegree = {node_id: 0 for node_id in node_ids}
    for source, target in edges:
        outgoing[source].append(target)
        indegree[target] += 1
    ready = [node_id for node_id, value in indegree.items() if value == 0]
    heapq.heapify(ready)
    depth = {node_id: 1 for node_id in node_ids}
    while ready:
        current = heapq.heappop(ready)
        for target in sorted(outgoing[current]):
            depth[target] = max(depth[target], depth[current] + 1)
            indegree[target] -= 1
            if indegree[target] == 0:
                heapq.heappush(ready, target)
    return max(depth.values(), default=0)


def _generated_loop_ids(loop_id: str) -> tuple[str, str, str, str, str]:
    prefix = f"@loop/{loop_id}"
    return (
        f"{prefix}/init",
        f"{prefix}/commit",
        f"{prefix}/route",
        f"{prefix}/done",
        f"{prefix}/limit",
    )


def _compiled_edge(
    transition_id: str,
    source_node_id: str,
    source_port_id: str,
    target_node_id: str,
    target_port_id: str,
) -> CompiledEdge:
    return CompiledEdge(
        transition_id=transition_id,
        source=CompiledEndpoint(source_node_id, source_port_id),
        target=CompiledEndpoint(target_node_id, target_port_id),
    )


def _lower_ir(
    spec: WorkflowSpecV1,
    *,
    registry: WorkflowNodeRegistry,
    validation: WorkflowValidationResult,
) -> WorkflowIR:
    checksum = semantic_checksum(spec)
    node_by_id = {node.id: node for node in spec.nodes}
    compiled_nodes: list[CompiledNode] = []
    for node in sorted(spec.nodes, key=lambda item: item.id):
        definition = registry.require(node.type, node.type_version)
        input_ports, output_ports = resolve_node_ports(spec, node)
        compiled_nodes.append(
            CompiledNode(
                id=node.id,
                type=node.type,
                type_version=node.type_version,
                scope_path=_scope_path(node),
                input_bindings=tuple(
                    (
                        input_id,
                        None if binding is None else freeze_json(binding.model_dump(mode="json", by_alias=True, exclude_unset=True)),
                    )
                    for input_id, binding in sorted(node.input_bindings.items())
                ),
                execution_policy=_frozen_object(node.execution_policy.model_dump(mode="json", by_alias=True, exclude_unset=True)),
                config=_frozen_object(node.config.model_dump(mode="json", by_alias=True, exclude_unset=True)),
                input_ports=tuple(_compiled_port(port, "input") for port in input_ports),
                output_ports=tuple(_compiled_port(port, "output") for port in output_ports),
                executor_port=definition.executor_port,
            )
        )

    loops = {node.id: node for node in spec.nodes if node.type == "loop" and node.scope.kind == "root"}
    body_nodes: dict[str, tuple[str, ...]] = {loop_id: tuple(sorted(node.id for node in spec.nodes if node.scope.kind == "loop_body" and node.scope.loop_node_id == loop_id)) for loop_id in loops}
    loop_id_by_body_node = {body_node_id: loop_id for loop_id, ids in body_nodes.items() for body_node_id in ids}
    body_edge_lists: dict[str, list[CompiledEdge]] = {loop_id: [] for loop_id in loops}
    for transition in spec.transitions:
        source_loop_id = loop_id_by_body_node.get(transition.source.node_id)
        if source_loop_id is not None and source_loop_id == loop_id_by_body_node.get(transition.target.node_id):
            body_edge_lists[source_loop_id].append(
                _compiled_edge(
                    transition.id,
                    transition.source.node_id,
                    transition.source.port_id,
                    transition.target.node_id,
                    transition.target.port_id,
                )
            )
    body_edges = {loop_id: tuple(sorted(edges, key=lambda edge: edge.transition_id)) for loop_id, edges in body_edge_lists.items()}
    loop_regions: list[CompiledLoopRegion] = []
    for loop_id, loop in sorted(loops.items()):
        init_id, commit_id, route_id, done_id, limit_id = _generated_loop_ids(loop_id)
        depth = _body_depth(
            body_nodes[loop_id],
            tuple((edge.source.node_id, edge.target.node_id) for edge in body_edges[loop_id]),
        )
        worst_supersteps = 2 + loop.config.max_iterations * (depth + 2)
        worst_activations = 2 + loop.config.max_iterations * (len(body_nodes[loop_id]) + 2)
        exit_node = node_by_id[loop.config.body_exit_node_id]
        if exit_node.type == "condition":
            exit_port_ids = tuple(branch.output_port_id for branch in exit_node.config.branches) + (exit_node.config.else_output_port_id,)
        elif exit_node.type == "http_request":
            exit_port_ids = ("success",)
        else:
            exit_port_ids = ("next",)
        generated_edges = (
            _compiled_edge(
                f"@loop/{loop_id}/init-entry",
                init_id,
                "next",
                loop.config.body_entry_node_id,
                "in",
            ),
            *(
                _compiled_edge(
                    f"@loop/{loop_id}/exit-commit/{port_id}",
                    loop.config.body_exit_node_id,
                    port_id,
                    commit_id,
                    "in",
                )
                for port_id in exit_port_ids
            ),
            _compiled_edge(
                f"@loop/{loop_id}/commit-route",
                commit_id,
                "next",
                route_id,
                "in",
            ),
        )
        loop_regions.append(
            CompiledLoopRegion(
                loop_node_id=loop_id,
                scope_path=("root", f"loop:{loop_id}"),
                body_entry_node_id=loop.config.body_entry_node_id,
                body_exit_node_id=loop.config.body_exit_node_id,
                body_node_ids=body_nodes[loop_id],
                body_edges=body_edges[loop_id],
                init_node_id=init_id,
                commit_node_id=commit_id,
                route_node_id=route_id,
                done_node_id=done_id,
                limit_error_node_id=limit_id,
                variables=tuple(
                    CompiledLoopVariable(
                        id=variable.id,
                        initial_input_id=variable.initial_input_id,
                        next_input_id=variable.next_input_id,
                        output_port_id=variable.output_port_id,
                    )
                    for variable in loop.config.variables
                ),
                termination_condition=_frozen_object(loop.config.termination_condition.model_dump(mode="json", by_alias=True, exclude_unset=True)),
                max_iterations=loop.config.max_iterations,
                generated_edges=generated_edges,
                generated_back_edge=_compiled_edge(
                    f"@loop/{loop_id}/continue",
                    route_id,
                    "continue",
                    loop.config.body_entry_node_id,
                    "in",
                ),
                condition_met_edge=_compiled_edge(
                    f"@loop/{loop_id}/done",
                    route_id,
                    "done",
                    done_id,
                    "in",
                ),
                limit_exceeded_edge=_compiled_edge(
                    f"@loop/{loop_id}/limit",
                    route_id,
                    "limit",
                    limit_id,
                    "in",
                ),
                limit_error_code="WORKFLOW_LOOP_LIMIT_EXCEEDED",
                worst_case_supersteps=worst_supersteps,
                worst_case_activations=worst_activations,
            )
        )

    rank = _topological_rank(spec)
    transition_source_by_id = {transition.id: transition.source.node_id for transition in spec.transitions}
    static_edges: list[CompiledEdge] = []
    outcome_routes: list[CompiledOutcomeRoute] = []
    for transition in spec.transitions:
        source_authored_node = node_by_id[transition.source.node_id]
        if source_authored_node.type == "condition":
            continue
        if transition.source.node_id in loop_id_by_body_node:
            continue
        source_node_id = transition.source.node_id
        target_node_id = transition.target.node_id
        if source_node_id in loops:
            _init_id, _commit_id, _route_id, done_id, limit_id = _generated_loop_ids(source_node_id)
            if transition.source.port_id == "next":
                source_node_id = done_id
            elif transition.source.port_id == "error":
                source_node_id = limit_id
        if target_node_id in loops:
            target_node_id = _generated_loop_ids(target_node_id)[0]
        compiled_edge = _compiled_edge(
            transition.id,
            source_node_id,
            transition.source.port_id,
            target_node_id,
            transition.target.port_id,
        )
        if source_authored_node.type == "start":
            static_edges.append(compiled_edge)
        else:
            outcome_routes.append(
                CompiledOutcomeRoute(
                    outcome="error" if transition.source.port_id == "error" else "success",
                    edge=compiled_edge,
                )
            )
    static_edges.sort(
        key=lambda edge: (
            rank.get(transition_source_by_id[edge.transition_id], 1_000_000),
            edge.transition_id,
        )
    )
    outcome_routes.sort(
        key=lambda route: (
            rank.get(transition_source_by_id[route.edge.transition_id], 1_000_000),
            0 if route.outcome == "success" else 1,
            route.edge.transition_id,
        )
    )

    branches: list[CompiledBranch] = []
    outgoing_by_node_port: dict[tuple[str, str], tuple[str, str, str]] = {}
    for transition in spec.transitions:
        target = transition.target.node_id
        if target in loops:
            target = _generated_loop_ids(target)[0]
        outgoing_by_node_port[(transition.source.node_id, transition.source.port_id)] = (
            transition.id,
            target,
            transition.target.port_id,
        )
    for node in sorted((item for item in spec.nodes if item.type == "condition"), key=lambda item: item.id):
        body_loop_id = loop_id_by_body_node.get(node.id)
        commit_target = None
        if body_loop_id is not None and loops[body_loop_id].config.body_exit_node_id == node.id:
            commit_target = _generated_loop_ids(body_loop_id)[1]

        def condition_target(port_id: str) -> str:
            authored = outgoing_by_node_port.get((node.id, port_id))
            if authored is not None:
                return authored[1]
            if commit_target is not None:
                return commit_target
            raise KeyError((node.id, port_id))

        error_authored = outgoing_by_node_port.get((node.id, "error"))
        branches.append(
            CompiledBranch(
                node_id=node.id,
                routes=tuple(
                    CompiledBranchRoute(
                        output_port_id=branch.output_port_id,
                        target_node_id=condition_target(branch.output_port_id),
                        predicate=_frozen_object(branch.predicate.model_dump(mode="json", by_alias=True, exclude_unset=True)),
                    )
                    for branch in node.config.branches
                ),
                else_output_port_id=node.config.else_output_port_id,
                else_target_node_id=condition_target(node.config.else_output_port_id),
                error_route=(
                    None
                    if error_authored is None
                    else _compiled_edge(
                        error_authored[0],
                        node.id,
                        "error",
                        error_authored[1],
                        error_authored[2],
                    )
                ),
            )
        )

    transforms = tuple(
        CompiledTransform(
            node_id=node.id,
            mode=node.config.mode,
            missing_variable=node.config.missing_variable,
            template=_frozen_object(node.config.template.model_dump(mode="json", by_alias=True, exclude_unset=True)),
        )
        for node in sorted((item for item in spec.nodes if item.type == "transform"), key=lambda item: item.id)
    )
    provenance_by_id = {item.node_id: item for item in validation.aggregate_provenance}
    aggregates = tuple(
        CompiledAggregate(
            node_id=node.id,
            condition_node_id=provenance_by_id[node.id].condition_node_id,
            groups=tuple(
                CompiledAggregateGroup(
                    output_id=group.id,
                    candidate_input_ids=tuple(group.candidate_input_ids),
                    candidate_branch_port_ids=dict(provenance_by_id[node.id].group_branch_ports)[group.id],
                )
                for group in node.config.groups
            ),
        )
        for node in sorted((item for item in spec.nodes if item.type == "variable_aggregate"), key=lambda item: item.id)
    )
    return WorkflowIR(
        graph_schema_version=GRAPH_SCHEMA_VERSION_V1,
        compiler_contract_version=COMPILER_CONTRACT_VERSION_V1,
        semantic_checksum=checksum,
        nodes=tuple(compiled_nodes),
        workflow_outputs=_compiled_workflow_outputs(spec),
        static_edges=tuple(static_edges),
        outcome_routes=tuple(outcome_routes),
        branches=tuple(branches),
        transforms=transforms,
        aggregates=aggregates,
        loop_regions=tuple(loop_regions),
        input_schema=_input_schema(spec),
        output_schema=_output_schema(spec),
        worst_case_depth=validation.metrics.depth,
        worst_case_recursion_depth=validation.metrics.recursion_depth,
        worst_case_parallelism=validation.metrics.max_parallelism,
        worst_case_fan_out=validation.metrics.max_fan_out,
        worst_case_steps=validation.metrics.total_steps,
        worst_case_activations=validation.metrics.total_activations,
        worst_case_iterations=validation.metrics.total_iterations,
    )


@dataclass(frozen=True, slots=True)
class WorkflowCompilerContract:
    contract_version: int

    def compile(
        self,
        spec: WorkflowSpecV1,
        *,
        limits: WorkflowCompilationLimits,
        cache: WorkflowCompilerCache[WorkflowIR] | None,
    ) -> WorkflowIR:
        validation = validate_workflow(spec, limits=limits, registry=FIRST_BATCH_RUNTIME_REGISTRY)
        validation.raise_for_errors()
        key = CompilerCacheKey(
            graph_schema_version=GRAPH_SCHEMA_VERSION_V1,
            compiler_contract_version=self.contract_version,
            semantic_checksum=semantic_checksum(spec),
        )
        if cache is None:
            return _lower_ir(spec, registry=FIRST_BATCH_RUNTIME_REGISTRY, validation=validation)
        return cache.get_or_compile(
            key,
            lambda: _lower_ir(spec, registry=FIRST_BATCH_RUNTIME_REGISTRY, validation=validation),
        )


_COMPILERS: Final = {
    COMPILER_CONTRACT_VERSION_V1: WorkflowCompilerContract(
        contract_version=COMPILER_CONTRACT_VERSION_V1,
    )
}
_DEFAULT_CACHE: WorkflowCompilerCache[WorkflowIR] = WorkflowCompilerCache(max_entries=128)


def require_compiler_contract(contract_version: object) -> WorkflowCompilerContract:
    if type(contract_version) is not int or contract_version not in _COMPILERS:
        raise WorkflowCompilerUnavailableError("WORKFLOW_COMPILER_UNAVAILABLE: exact compiler contract is not installed")
    return _COMPILERS[contract_version]


def compile_workflow(
    spec: WorkflowSpecV1,
    *,
    graph_schema_version: int = GRAPH_SCHEMA_VERSION_V1,
    compiler_contract_version: int = CURRENT_COMPILER_CONTRACT_VERSION,
    limits: WorkflowCompilationLimits | None = None,
    registry: WorkflowNodeRegistry = FIRST_BATCH_RUNTIME_REGISTRY,
    cache: WorkflowCompilerCache[WorkflowIR] | None = _DEFAULT_CACHE,
) -> WorkflowIR:
    """Compile a strict Spec using one exact installed compatibility contract."""

    if type(graph_schema_version) is not int or graph_schema_version != GRAPH_SCHEMA_VERSION_V1:
        raise WorkflowCompilerUnavailableError("WORKFLOW_COMPILER_UNAVAILABLE: graph schema version is not installed")
    if registry is not FIRST_BATCH_RUNTIME_REGISTRY:
        raise WorkflowCompilerUnavailableError("WORKFLOW_COMPILER_REGISTRY_UNAVAILABLE: compiler contract is bound to the installed frozen Registry")
    compiler = require_compiler_contract(compiler_contract_version)
    return compiler.compile(
        spec,
        limits=limits or WorkflowCompilationLimits.permissive(),
        cache=cache,
    )


__all__ = [
    "COMPILER_CONTRACT_VERSION_V1",
    "CURRENT_COMPILER_CONTRACT_VERSION",
    "GRAPH_SCHEMA_VERSION_V1",
    "WorkflowCompilerContract",
    "WorkflowCompilerUnavailableError",
    "compile_workflow",
    "require_compiler_contract",
]
