"""Fail-closed authored-DAG analysis before any LangGraph lowering."""

from __future__ import annotations

import heapq
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from deerflow.workflows.contracts import WorkflowSpecV1


class WorkflowTopologyError(ValueError):
    """An authored graph cannot be lowered without changing its meaning."""


@dataclass(frozen=True, slots=True)
class AuthoredDag:
    nodes: frozenset[str]
    edges: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class WorkflowTopology:
    root: AuthoredDag
    loop_bodies: Mapping[str, AuthoredDag]
    # loop node, body exit, body entry. These edges never come from authored
    # transitions; the compiler owns their provenance.
    generated_back_edges: tuple[tuple[str, str, str], ...]


def _assert_acyclic(dag: AuthoredDag, *, scope: str) -> None:
    outgoing: dict[str, list[str]] = {node_id: [] for node_id in dag.nodes}
    indegree = {node_id: 0 for node_id in dag.nodes}
    for source, target in dag.edges:
        outgoing[source].append(target)
        indegree[target] += 1
    ready = [node_id for node_id, count in indegree.items() if count == 0]
    heapq.heapify(ready)
    visited = 0
    while ready:
        node_id = heapq.heappop(ready)
        visited += 1
        for target in sorted(outgoing[node_id]):
            indegree[target] -= 1
            if indegree[target] == 0:
                heapq.heappush(ready, target)
    if visited != len(dag.nodes):
        raise WorkflowTopologyError(f"{scope} authored DAG contains a cycle")


def analyze_workflow_topology(spec: WorkflowSpecV1) -> WorkflowTopology:
    """Partition strict Spec transitions without inventing authored edges."""

    node_by_id = {node.id: node for node in spec.nodes}
    if spec.entry_node_id not in node_by_id:
        raise WorkflowTopologyError("entry node does not exist")

    root_nodes: set[str] = set()
    body_nodes: dict[str, set[str]] = {}
    scope_by_node: dict[str, tuple[str, str | None]] = {}
    for node in spec.nodes:
        if node.scope.kind == "root":
            root_nodes.add(node.id)
            scope_by_node[node.id] = ("root", None)
            continue
        loop_node_id = node.scope.loop_node_id
        if node.type == "loop":
            raise WorkflowTopologyError("nested Loop nodes are not supported")
        body_nodes.setdefault(loop_node_id, set()).add(node.id)
        scope_by_node[node.id] = ("loop_body", loop_node_id)

    if scope_by_node[spec.entry_node_id][0] != "root":
        raise WorkflowTopologyError("entry node must be in the root DAG")

    root_edges: list[tuple[str, str]] = []
    body_edges: dict[str, list[tuple[str, str]]] = {loop_node_id: [] for loop_node_id in body_nodes}
    for transition in spec.transitions:
        source_id = transition.source.node_id
        target_id = transition.target.node_id
        if source_id not in node_by_id or target_id not in node_by_id:
            raise WorkflowTopologyError("transition references an unknown node")
        source_scope = scope_by_node[source_id]
        target_scope = scope_by_node[target_id]
        if source_scope != target_scope:
            raise WorkflowTopologyError("cross-scope authored transitions are forbidden")
        if source_scope[0] == "root":
            root_edges.append((source_id, target_id))
        else:
            body_edges[source_scope[1]].append((source_id, target_id))  # type: ignore[index]

    root = AuthoredDag(frozenset(root_nodes), tuple(root_edges))
    _assert_acyclic(root, scope="root")

    loop_bodies: dict[str, AuthoredDag] = {}
    generated_back_edges: list[tuple[str, str, str]] = []
    loop_nodes = {node.id: node for node in spec.nodes if node.type == "loop"}
    for loop_node_id, nodes in sorted(body_nodes.items()):
        loop_node = loop_nodes.get(loop_node_id)
        if loop_node is None or loop_node.scope.kind != "root":
            raise WorkflowTopologyError("loop body references a missing root Loop node")
        entry = loop_node.config.body_entry_node_id
        exit_ = loop_node.config.body_exit_node_id
        if entry not in nodes or exit_ not in nodes:
            raise WorkflowTopologyError("Loop entry and exit must belong to its body DAG")
        dag = AuthoredDag(frozenset(nodes), tuple(body_edges[loop_node_id]))
        _assert_acyclic(dag, scope=f"loop body {loop_node_id}")
        loop_bodies[loop_node_id] = dag
        generated_back_edges.append((loop_node_id, exit_, entry))

    for loop_node_id in sorted(loop_nodes):
        if loop_node_id not in loop_bodies:
            raise WorkflowTopologyError("root Loop node must own a non-empty body DAG")

    return WorkflowTopology(
        root=root,
        loop_bodies=MappingProxyType(loop_bodies),
        generated_back_edges=tuple(generated_back_edges),
    )
