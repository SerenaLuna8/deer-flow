"""Checkpoint-separated structured bounded do-until execution."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, TypedDict

from langgraph.channels import DeltaChannel
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from deerflow.config.database_config import CheckpointChannelMode
from deerflow.runtime.checkpoint_mode import inject_checkpoint_mode
from deerflow.workflows.compiler.cache import WorkflowCompilerCache
from deerflow.workflows.compiler.ir import (
    StructuredLoopPlan,
    StructuredLoopTemplate,
    lower_structured_loop,
)
from deerflow.workflows.runtime.activation import WorkflowActivationIdentity

DEFAULT_WORKFLOW_RECURSION_LIMIT = 10_000


def _append_activation_write(
    current: list[str],
    write: Any,
) -> list[str]:
    if not isinstance(write, list) or any(not isinstance(item, str) for item in write):
        raise TypeError("activation journal writes must be lists of strings")
    result = list(current)
    result.extend(write)
    return result


def _append_activation_writes(
    current: list[str],
    writes: Sequence[Any],
) -> list[str]:
    result = list(current)
    for write in writes:
        result = _append_activation_write(result, write)
    return result


ActivationJournalFull = Annotated[list[str], _append_activation_write]
ActivationJournalDelta = Annotated[
    list[str],
    DeltaChannel(
        _append_activation_writes,
        snapshot_frequency=2,
    ),
]


class _LoopStateBase(TypedDict, total=False):
    run_id: str
    loop_node_id: str
    body_node_id: str
    current_value: Any
    iteration: int
    pending_value: Any
    pending_activation_id: str | None
    pending_ready: bool
    status: Literal["running", "completed", "iteration_limit_exceeded"]
    route_decision: Literal["continue", "finish"]
    current_attempt: int


class _FullLoopState(_LoopStateBase, total=False):
    activation_ids: ActivationJournalFull


class _DeltaLoopState(_LoopStateBase, total=False):
    activation_ids: ActivationJournalDelta


BodyStep = Callable[
    [Any, WorkflowActivationIdentity],
    Any | Awaitable[Any],
]
UntilPredicate = Callable[[Any, int], bool | Awaitable[bool]]
FaultCallback = Callable[
    [str, WorkflowActivationIdentity],
    None | Awaitable[None],
]


@dataclass(slots=True)
class WorkflowLoopTrace:
    """Non-durable test/telemetry observation; never used for correctness."""

    attempts: dict[str, int] = field(default_factory=dict)
    activation_attempts: dict[str, int] = field(default_factory=dict)

    def record(self, stage: str, identity: WorkflowActivationIdentity) -> None:
        self.attempts[stage] = self.attempts.get(stage, 0) + 1
        key = f"{stage}:{identity.attempt_bucket_key}"
        self.activation_attempts[key] = self.activation_attempts.get(key, 0) + 1


class InjectedWorkflowFault(RuntimeError):
    """Deterministic crash boundary used by the takeover conformance suite."""


class StaleWorkflowAttemptError(RuntimeError):
    """An older attempt tried to write state after a newer attempt took over."""


@dataclass(slots=True)
class OneShotWorkflowFault:
    stage: str
    iteration_path: tuple[int, ...] = (1,)
    fired: bool = False

    def __call__(
        self,
        stage: str,
        identity: WorkflowActivationIdentity,
    ) -> None:
        if not self.fired and stage == self.stage and identity.iteration_path == self.iteration_path:
            self.fired = True
            raise InjectedWorkflowFault(f"injected Workflow fault at {stage} for {identity.key}")


@dataclass(slots=True)
class WorkflowLoopRuntimeContext:
    body_step: BodyStep
    until: UntilPredicate
    fault: FaultCallback | None = None
    trace: WorkflowLoopTrace = field(default_factory=WorkflowLoopTrace)
    attempt: int = 1


@dataclass(frozen=True, slots=True)
class WorkflowLoopExecutionResult:
    run_id: str
    value: Any
    iterations: int
    activation_ids: tuple[str, ...]
    status: Literal["completed", "iteration_limit_exceeded"]
    current_attempt: int


class WorkflowLoopIterationLimitExceeded(RuntimeError):
    def __init__(self, result: WorkflowLoopExecutionResult) -> None:
        self.iterations = result.iterations
        self.activation_ids = result.activation_ids
        super().__init__(f"Workflow Loop reached its stable max_iterations failure after {result.iterations} committed iterations")


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _identity(
    state: _LoopStateBase,
    context: WorkflowLoopRuntimeContext,
) -> WorkflowActivationIdentity:
    current_attempt = state.get("current_attempt", 1)
    if context.attempt != current_attempt:
        raise StaleWorkflowAttemptError(f"Workflow attempt {context.attempt} cannot write current attempt {current_attempt}")
    return WorkflowActivationIdentity(
        run_id=state["run_id"],
        node_id=state["body_node_id"],
        # ``iteration`` is the number of committed rounds.  Public runtime
        # contracts use one-based positive ordinals for the round currently
        # being activated.
        iteration_path=(state.get("iteration", 0) + 1,),
        attempt=context.attempt,
    )


async def _hit(
    context: WorkflowLoopRuntimeContext,
    stage: str,
    identity: WorkflowActivationIdentity,
) -> None:
    context.trace.record(stage, identity)
    if context.fault is not None:
        await _maybe_await(context.fault(stage, identity))


def _build_graph(
    template: StructuredLoopTemplate,
    *,
    checkpointer: Any,
    checkpoint_mode: CheckpointChannelMode,
) -> Any:
    plan = template.plan
    schema: type[_FullLoopState] | type[_DeltaLoopState]
    schema = _FullLoopState if checkpoint_mode == "full" else _DeltaLoopState
    builder = StateGraph(schema, context_schema=WorkflowLoopRuntimeContext)

    async def body(
        state: _LoopStateBase,
        runtime: Runtime[WorkflowLoopRuntimeContext],
    ) -> dict[str, Any]:
        identity = _identity(state, runtime.context)
        await _hit(runtime.context, "body_before_output", identity)
        candidate = await _maybe_await(runtime.context.body_step(state.get("current_value"), identity))
        return {
            "pending_value": candidate,
            "pending_activation_id": identity.key,
            "pending_ready": True,
        }

    async def after_body(
        state: _LoopStateBase,
        runtime: Runtime[WorkflowLoopRuntimeContext],
    ) -> dict[str, Any]:
        await _hit(
            runtime.context,
            "body_after_checkpoint",
            _identity(state, runtime.context),
        )
        return {}

    async def commit(
        state: _LoopStateBase,
        runtime: Runtime[WorkflowLoopRuntimeContext],
    ) -> dict[str, Any]:
        identity = _identity(state, runtime.context)
        await _hit(runtime.context, "commit_before_output", identity)
        if not state.get("pending_ready"):
            raise RuntimeError("Loop commit has no checkpointed body candidate")
        if state.get("pending_activation_id") != identity.key:
            raise RuntimeError("Loop pending activation does not match iteration identity")
        if identity.key in state.get("activation_ids", []):
            raise RuntimeError("Loop activation was already committed")
        iteration = state.get("iteration", 0) + 1
        value = state.get("pending_value")
        terminated = bool(await _maybe_await(runtime.context.until(value, iteration)))
        if terminated:
            status: Literal["running", "completed", "iteration_limit_exceeded"] = "completed"
        elif iteration >= plan.max_iterations:
            status = "iteration_limit_exceeded"
        else:
            status = "running"
        return {
            "current_value": value,
            "iteration": iteration,
            "activation_ids": [identity.key],
            "pending_value": None,
            "pending_activation_id": None,
            "pending_ready": False,
            "status": status,
        }

    async def after_commit(
        state: _LoopStateBase,
        runtime: Runtime[WorkflowLoopRuntimeContext],
    ) -> dict[str, Any]:
        committed_iteration = state.get("iteration", 0)
        identity = WorkflowActivationIdentity(
            run_id=state["run_id"],
            node_id=state["body_node_id"],
            iteration_path=(committed_iteration,),
            attempt=runtime.context.attempt,
        )
        await _hit(runtime.context, "commit_after_checkpoint", identity)
        return {}

    async def route(
        state: _LoopStateBase,
        runtime: Runtime[WorkflowLoopRuntimeContext],
    ) -> dict[str, Any]:
        committed_iteration = state.get("iteration", 0)
        identity = WorkflowActivationIdentity(
            run_id=state["run_id"],
            node_id=state["body_node_id"],
            iteration_path=(committed_iteration,),
            attempt=runtime.context.attempt,
        )
        await _hit(runtime.context, "route_before_output", identity)
        return {"route_decision": ("continue" if state.get("status") == "running" else "finish")}

    async def after_route(
        state: _LoopStateBase,
        runtime: Runtime[WorkflowLoopRuntimeContext],
    ) -> dict[str, Any]:
        committed_iteration = state.get("iteration", 0)
        identity = WorkflowActivationIdentity(
            run_id=state["run_id"],
            node_id=state["body_node_id"],
            iteration_path=(committed_iteration,),
            attempt=runtime.context.attempt,
        )
        await _hit(runtime.context, "route_after_checkpoint", identity)
        return {}

    def choose_next(state: _LoopStateBase) -> Literal["body", "finish"]:
        return "body" if state.get("route_decision") == "continue" else "finish"

    builder.add_node("loop_body", body)
    builder.add_node("loop_body_checkpoint", after_body)
    builder.add_node("loop_commit", commit)
    builder.add_node("loop_commit_checkpoint", after_commit)
    builder.add_node("loop_route", route)
    builder.add_node("loop_route_checkpoint", after_route)
    builder.add_edge(START, template.nodes[0])
    for source, target in template.static_edges:
        builder.add_edge(source, target)
    generated_source, generated_target = template.generated_back_edge
    builder.add_conditional_edges(
        generated_source,
        choose_next,
        {"body": generated_target, "finish": END},
    )
    return builder.compile(
        checkpointer=checkpointer,
        name=f"workflow-loop-{plan.loop_node_id}",
    )


_DEFAULT_CACHE: WorkflowCompilerCache[StructuredLoopTemplate] = WorkflowCompilerCache(max_entries=128)


@dataclass(slots=True)
class WorkflowLoopRunner:
    plan: StructuredLoopPlan
    template: StructuredLoopTemplate
    graph: Any
    checkpoint_mode: CheckpointChannelMode

    @classmethod
    def compile(
        cls,
        plan: StructuredLoopPlan,
        *,
        checkpointer: Any,
        checkpoint_mode: CheckpointChannelMode,
        cache: WorkflowCompilerCache[StructuredLoopTemplate] | None = None,
    ) -> WorkflowLoopRunner:
        selected_cache = cache or _DEFAULT_CACHE
        fresh_template = lower_structured_loop(plan)
        template = selected_cache.get_or_compile(
            fresh_template.cache_key,
            lambda: fresh_template,
        )
        graph = _build_graph(
            template,
            checkpointer=checkpointer,
            checkpoint_mode=checkpoint_mode,
        )
        return cls(
            plan=template.plan,
            template=template,
            graph=graph,
            checkpoint_mode=checkpoint_mode,
        )

    def config(
        self,
        run_id: str,
        *,
        recursion_limit: int = DEFAULT_WORKFLOW_RECURSION_LIMIT,
    ) -> dict[str, Any]:
        # Validate run_id through the exact activation identity contract.
        WorkflowActivationIdentity(
            run_id=run_id,
            node_id=self.plan.body_node_id,
            iteration_path=(1,),
        )
        if recursion_limit <= 0:
            raise ValueError("recursion_limit must be positive")
        config: dict[str, Any] = {
            "configurable": {
                "thread_id": run_id,
                # A Workflow Run id is globally unique and is the checkpoint
                # ownership key. Non-empty checkpoint_ns values are reserved
                # by LangGraph for compiled subgraphs and make root graph
                # state reads resolve a non-existent subgraph.
                "checkpoint_ns": "",
            },
            "metadata": {
                "workflow_checksum": self.plan.semantic_checksum,
                "workflow_graph_schema_version": self.plan.graph_schema_version,
                "workflow_compiler_contract_version": (self.plan.compiler_contract_version),
                "workflow_loop_node_id": self.plan.loop_node_id,
            },
            "recursion_limit": recursion_limit,
        }
        inject_checkpoint_mode(config, self.checkpoint_mode)
        return config

    async def run(
        self,
        *,
        run_id: str,
        initial_value: Any,
        context: WorkflowLoopRuntimeContext,
        attempt: int = 1,
        recursion_limit: int = DEFAULT_WORKFLOW_RECURSION_LIMIT,
    ) -> WorkflowLoopExecutionResult:
        if type(attempt) is not int or attempt <= 0:
            raise ValueError("attempt must be positive")
        context.attempt = attempt
        config = self.config(run_id, recursion_limit=recursion_limit)
        values = await self.graph.ainvoke(
            {
                "run_id": run_id,
                "loop_node_id": self.plan.loop_node_id,
                "body_node_id": self.plan.body_node_id,
                "current_value": initial_value,
                "iteration": 0,
                "status": "running",
                "pending_ready": False,
                "current_attempt": attempt,
            },
            config=config,
            context=context,
            durability="sync",
        )
        return self._result_or_raise(run_id, values)

    async def resume(
        self,
        *,
        run_id: str,
        context: WorkflowLoopRuntimeContext,
        attempt: int | None = None,
        recursion_limit: int = DEFAULT_WORKFLOW_RECURSION_LIMIT,
    ) -> WorkflowLoopExecutionResult:
        config = self.config(run_id, recursion_limit=recursion_limit)
        snapshot = await self.graph.aget_state(config)
        current_attempt = snapshot.values.get("current_attempt", 1)
        requested_attempt = current_attempt if attempt is None else attempt
        if type(requested_attempt) is not int or requested_attempt <= 0:
            raise ValueError("attempt must be positive")
        if requested_attempt < current_attempt:
            raise StaleWorkflowAttemptError(f"Workflow attempt {requested_attempt} is stale; current attempt is {current_attempt}")
        if requested_attempt > current_attempt:
            # Persist the checkpoint-level fence before this attempt executes
            # a node. The final Worker path must additionally enforce its Job
            # lease/CAS fence against truly concurrent stale processes.
            await self.graph.aupdate_state(
                config,
                {"current_attempt": requested_attempt},
            )
        context.attempt = requested_attempt
        values = await self.graph.ainvoke(
            None,
            config=config,
            context=context,
            durability="sync",
        )
        if values is None:
            values = (await self.graph.aget_state(config)).values
        return self._result_or_raise(run_id, values)

    async def state(self, *, run_id: str) -> dict[str, Any]:
        return dict((await self.graph.aget_state(self.config(run_id))).values)

    async def history(self, *, run_id: str) -> list[Any]:
        return [snapshot async for snapshot in self.graph.aget_state_history(self.config(run_id))]

    def _result_or_raise(
        self,
        run_id: str,
        values: dict[str, Any],
    ) -> WorkflowLoopExecutionResult:
        raw_status = values.get("status")
        if raw_status not in {"completed", "iteration_limit_exceeded"}:
            raise RuntimeError("Workflow Loop stopped without a terminal status")
        result = WorkflowLoopExecutionResult(
            run_id=run_id,
            value=values.get("current_value"),
            iterations=values.get("iteration", 0),
            activation_ids=tuple(values.get("activation_ids", [])),
            status=raw_status,
            current_attempt=values.get("current_attempt", 1),
        )
        if result.status == "iteration_limit_exceeded":
            raise WorkflowLoopIterationLimitExceeded(result)
        return result
