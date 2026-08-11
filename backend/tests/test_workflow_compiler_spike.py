from __future__ import annotations

import importlib.metadata
import uuid
from copy import deepcopy

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError

from deerflow.workflows import WorkflowSpecV1
from deerflow.workflows.compiler import (
    CompilerCacheKey,
    StructuredLoopPlan,
    StructuredLoopTemplate,
    WorkflowCompilerCache,
    WorkflowTopologyError,
    analyze_workflow_topology,
)
from deerflow.workflows.runtime import (
    MISSING,
    AmbiguousAggregateValueError,
    InjectedWorkflowFault,
    MissingAggregateValueError,
    OneShotWorkflowFault,
    StaleWorkflowAttemptError,
    WorkflowActivationIdentity,
    WorkflowLoopIterationLimitExceeded,
    WorkflowLoopRunner,
    WorkflowLoopRuntimeContext,
    resolve_exclusive_branch_value,
)

START_ID = "00000000-0000-4000-8000-000000000001"
LOOP_ID = "00000000-0000-4000-8000-000000000002"
BODY_ID = "00000000-0000-4000-8000-000000000003"
END_ID = "00000000-0000-4000-8000-000000000004"


def _execution_policy() -> dict[str, object]:
    return {
        "retry": {"mode": "none"},
        "on_error": {"mode": "fail_workflow"},
    }


def _predicate() -> dict[str, object]:
    return {
        "op": "and",
        "items": [
            {
                "left": {"kind": "literal", "value": True},
                "operator": "eq",
                "right": {"kind": "literal", "value": True},
            }
        ],
    }


def _node(
    node_id: str,
    node_type: str,
    config: dict[str, object],
    *,
    scope: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "id": node_id,
        "type": node_type,
        "type_version": 1,
        "scope": scope or {"kind": "root"},
        "custom_label": None,
        "description": None,
        "input_bindings": {},
        "execution_policy": _execution_policy(),
        "config": config,
    }


def _spec_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "entry_node_id": START_ID,
        "nodes": [
            _node(START_ID, "start", {}),
            _node(
                LOOP_ID,
                "loop",
                {
                    "mode": "do_until",
                    "body_entry_node_id": BODY_ID,
                    "body_exit_node_id": BODY_ID,
                    "max_iterations": 3,
                    "termination_condition": _predicate(),
                    "variables": [
                        {
                            "id": "value",
                            "name": "value",
                            "value_type": {
                                "kind": "string",
                                "collection": False,
                                "nullable": False,
                            },
                            "initial_input_id": "value-initial",
                            "next_input_id": "value-next",
                            "output_port_id": "value",
                        }
                    ],
                },
            ),
            _node(
                BODY_ID,
                "transform",
                {
                    "input_variables": [],
                    "missing_variable": "error",
                    "mode": "text",
                    "template": {"version": 1, "segments": []},
                    "output_schema": None,
                },
                scope={"kind": "loop_body", "loop_node_id": LOOP_ID},
            ),
            _node(END_ID, "end", {}),
        ],
        "transitions": [
            {
                "id": "start-loop",
                "source": {"node_id": START_ID, "port_id": "next"},
                "target": {"node_id": LOOP_ID, "port_id": "in"},
            },
            {
                "id": "loop-end",
                "source": {"node_id": LOOP_ID, "port_id": "next"},
                "target": {"node_id": END_ID, "port_id": "in"},
            },
        ],
        "workflow_inputs": [],
        "workflow_outputs": [],
        "credential_slots": [],
    }


def test_topology_keeps_authored_root_and_body_graphs_acyclic_and_marks_only_generated_back_edge() -> None:
    topology = analyze_workflow_topology(WorkflowSpecV1.model_validate(_spec_payload()))

    assert topology.root.nodes == frozenset({START_ID, LOOP_ID, END_ID})
    assert topology.root.edges == ((START_ID, LOOP_ID), (LOOP_ID, END_ID))
    assert topology.loop_bodies[LOOP_ID].nodes == frozenset({BODY_ID})
    assert topology.loop_bodies[LOOP_ID].edges == ()
    assert topology.generated_back_edges == ((LOOP_ID, BODY_ID, BODY_ID),)
    with pytest.raises(TypeError):
        topology.loop_bodies[LOOP_ID] = topology.root  # type: ignore[index]


def test_spike_is_pinned_to_the_reviewed_langgraph_stategraph_version() -> None:
    assert importlib.metadata.version("langgraph") == "1.2.9"


def test_activation_key_excludes_attempt_but_changes_for_the_next_iteration() -> None:
    run_id = str(uuid.uuid4())
    first = WorkflowActivationIdentity(
        run_id=run_id,
        node_id=BODY_ID,
        iteration_path=(1,),
        attempt=1,
    )
    retry = WorkflowActivationIdentity(
        run_id=run_id,
        node_id=BODY_ID,
        iteration_path=(1,),
        attempt=2,
    )
    next_iteration = WorkflowActivationIdentity(
        run_id=run_id,
        node_id=BODY_ID,
        iteration_path=(2,),
        attempt=1,
    )

    assert first.key == retry.key
    assert first.attempt_bucket_key != retry.attempt_bucket_key
    assert next_iteration.key != first.key
    with pytest.raises(ValueError, match="positive integers"):
        WorkflowActivationIdentity(
            run_id=run_id,
            node_id=BODY_ID,
            iteration_path=(0,),
        )


@pytest.mark.parametrize("scope", ["root", "body"])
def test_authored_back_edge_is_rejected_in_every_scope(scope: str) -> None:
    payload = _spec_payload()
    if scope == "root":
        payload["transitions"].append(  # type: ignore[union-attr]
            {
                "id": "root-cycle",
                "source": {"node_id": END_ID, "port_id": "next"},
                "target": {"node_id": START_ID, "port_id": "in"},
            }
        )
    else:
        payload["transitions"].append(  # type: ignore[union-attr]
            {
                "id": "body-cycle",
                "source": {"node_id": BODY_ID, "port_id": "next"},
                "target": {"node_id": BODY_ID, "port_id": "in"},
            }
        )

    with pytest.raises(WorkflowTopologyError, match="authored DAG contains a cycle"):
        analyze_workflow_topology(WorkflowSpecV1.model_validate(payload))


def test_cross_scope_transition_and_nested_loop_are_rejected() -> None:
    cross_scope = _spec_payload()
    cross_scope["transitions"].append(  # type: ignore[union-attr]
        {
            "id": "cross-scope",
            "source": {"node_id": LOOP_ID, "port_id": "body"},
            "target": {"node_id": BODY_ID, "port_id": "in"},
        }
    )
    with pytest.raises(WorkflowTopologyError, match="cross-scope"):
        analyze_workflow_topology(WorkflowSpecV1.model_validate(cross_scope))

    nested = _spec_payload()
    nested_loop = deepcopy(nested["nodes"][1])  # type: ignore[index]
    nested_loop["id"] = "00000000-0000-4000-8000-000000000005"
    nested_loop["scope"] = {"kind": "loop_body", "loop_node_id": LOOP_ID}
    nested_loop["config"]["body_entry_node_id"] = BODY_ID
    nested_loop["config"]["body_exit_node_id"] = BODY_ID
    nested["nodes"].append(nested_loop)  # type: ignore[union-attr]
    with pytest.raises(WorkflowTopologyError, match="nested Loop"):
        analyze_workflow_topology(WorkflowSpecV1.model_validate(nested))


def test_compiler_cache_uses_only_the_frozen_compatibility_triple() -> None:
    cache: WorkflowCompilerCache[object] = WorkflowCompilerCache(max_entries=2)
    calls = 0

    def factory() -> object:
        nonlocal calls
        calls += 1
        return object()

    key = CompilerCacheKey(
        graph_schema_version=1,
        compiler_contract_version=1,
        semantic_checksum="a" * 64,
    )
    first = cache.get_or_compile(key, factory)
    second = cache.get_or_compile(key, factory)
    graph_v2 = cache.get_or_compile(
        CompilerCacheKey(
            graph_schema_version=2,
            compiler_contract_version=key.compiler_contract_version,
            semantic_checksum=key.semantic_checksum,
        ),
        factory,
    )
    compiler_v2 = cache.get_or_compile(
        CompilerCacheKey(
            graph_schema_version=key.graph_schema_version,
            compiler_contract_version=2,
            semantic_checksum=key.semantic_checksum,
        ),
        factory,
    )

    assert first is second
    assert graph_v2 is not first
    assert compiler_v2 is not first
    assert calls == 3
    assert cache.hits == 1
    assert cache.misses == 3


def test_cached_lowering_is_authority_free_and_each_runner_binds_its_own_mode_and_saver() -> None:
    plan = StructuredLoopPlan(
        graph_schema_version=1,
        compiler_contract_version=1,
        semantic_checksum="b" * 64,
        loop_node_id=LOOP_ID,
        body_node_id=BODY_ID,
        max_iterations=2,
    )
    cache = WorkflowCompilerCache()
    full_saver = InMemorySaver()
    delta_saver = InMemorySaver()
    full = WorkflowLoopRunner.compile(
        plan,
        checkpointer=full_saver,
        checkpoint_mode="full",
        cache=cache,
    )
    delta = WorkflowLoopRunner.compile(
        plan,
        checkpointer=delta_saver,
        checkpoint_mode="delta",
        cache=cache,
    )

    assert full.template is delta.template
    assert not hasattr(full.template, "checkpointer")
    assert not hasattr(full.template, "checkpoint_mode")
    assert full.graph is not delta.graph
    assert full.graph.checkpointer is full_saver
    assert delta.graph.checkpointer is delta_saver
    assert full.checkpoint_mode == "full"
    assert delta.checkpoint_mode == "delta"
    assert cache.hits == 1
    with pytest.raises(TypeError):
        StructuredLoopTemplate(
            plan=plan,
            generated_back_edge=("authored", "cycle"),  # type: ignore[call-arg]
        )


def test_exclusive_aggregate_distinguishes_missing_from_null_and_rejects_zero_or_many() -> None:
    assert (
        resolve_exclusive_branch_value(
            {"left": None, "right": MISSING},
            ("left", "right"),
        ).value
        is None
    )

    with pytest.raises(MissingAggregateValueError, match="no branch value"):
        resolve_exclusive_branch_value(
            {"left": MISSING},
            ("left", "right"),
        )
    with pytest.raises(AmbiguousAggregateValueError, match="left, right"):
        resolve_exclusive_branch_value(
            {"left": 1, "right": 2},
            ("left", "right"),
        )


@pytest.mark.parametrize("checkpoint_mode", ["full", "delta"])
@pytest.mark.anyio
async def test_bounded_do_until_runs_once_checks_after_commit_and_has_stable_limit_failure(
    checkpoint_mode: str,
) -> None:
    plan = StructuredLoopPlan(
        graph_schema_version=1,
        compiler_contract_version=1,
        semantic_checksum="c" * 64,
        loop_node_id=LOOP_ID,
        body_node_id=BODY_ID,
        max_iterations=2,
    )
    runner = WorkflowLoopRunner.compile(
        plan,
        checkpointer=InMemorySaver(),
        checkpoint_mode=checkpoint_mode,
    )
    context = WorkflowLoopRuntimeContext(
        body_step=lambda value, _identity: value + 1,
        until=lambda value, _iteration: value >= 1,
    )
    completed = await runner.run(
        run_id=str(uuid.uuid4()),
        initial_value=5,
        context=context,
    )
    assert completed.value == 6
    assert completed.iterations == 1
    assert len(completed.activation_ids) == 1

    never = WorkflowLoopRuntimeContext(
        body_step=lambda value, _identity: value + 1,
        until=lambda _value, _iteration: False,
    )
    run_id = str(uuid.uuid4())
    with pytest.raises(WorkflowLoopIterationLimitExceeded) as first:
        await runner.run(run_id=run_id, initial_value=0, context=never)
    with pytest.raises(WorkflowLoopIterationLimitExceeded) as resumed:
        await runner.resume(run_id=run_id, context=never)

    assert first.value.iterations == resumed.value.iterations == 2
    assert first.value.activation_ids == resumed.value.activation_ids


@pytest.mark.anyio
async def test_new_attempt_fences_old_attempt_without_changing_activation_key() -> None:
    runner = WorkflowLoopRunner.compile(
        StructuredLoopPlan(
            graph_schema_version=1,
            compiler_contract_version=1,
            semantic_checksum="7" * 64,
            loop_node_id=LOOP_ID,
            body_node_id=BODY_ID,
            max_iterations=4,
        ),
        checkpointer=InMemorySaver(),
        checkpoint_mode="full",
        cache=WorkflowCompilerCache(),
    )
    run_id = str(uuid.uuid4())
    first_context = WorkflowLoopRuntimeContext(
        body_step=lambda value, _identity: value + 1,
        until=lambda value, _iteration: value >= 2,
        fault=OneShotWorkflowFault(stage="body_after_checkpoint"),
    )
    with pytest.raises(InjectedWorkflowFault):
        await runner.run(
            run_id=run_id,
            initial_value=0,
            context=first_context,
            attempt=1,
        )

    retry_context = WorkflowLoopRuntimeContext(
        body_step=lambda value, _identity: value + 1,
        until=lambda value, _iteration: value >= 2,
    )
    result = await runner.resume(
        run_id=run_id,
        context=retry_context,
        attempt=2,
    )
    assert result.current_attempt == 2
    assert len(result.activation_ids) == 2

    before = await runner.state(run_id=run_id)
    with pytest.raises(StaleWorkflowAttemptError):
        await runner.resume(
            run_id=run_id,
            context=first_context,
            attempt=1,
        )
    assert await runner.state(run_id=run_id) == before


@pytest.mark.anyio
async def test_recursion_limit_is_only_a_depth_guard_not_the_loop_limit() -> None:
    runner = WorkflowLoopRunner.compile(
        StructuredLoopPlan(
            graph_schema_version=1,
            compiler_contract_version=1,
            semantic_checksum="e" * 64,
            loop_node_id=LOOP_ID,
            body_node_id=BODY_ID,
            max_iterations=100,
        ),
        checkpointer=InMemorySaver(),
        checkpoint_mode="full",
    )
    context = WorkflowLoopRuntimeContext(
        body_step=lambda value, _identity: value + 1,
        until=lambda value, _iteration: value >= 20,
    )

    with pytest.raises(GraphRecursionError):
        await runner.run(
            run_id=str(uuid.uuid4()),
            initial_value=0,
            context=context,
            recursion_limit=4,
        )
