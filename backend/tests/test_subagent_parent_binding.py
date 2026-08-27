"""Parent execution binding and graph-local task Adapter contracts."""

from __future__ import annotations

import asyncio
import importlib
import pickle
import threading
import uuid
from contextlib import suppress
from types import MappingProxyType
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool
from langgraph.runtime import Runtime

from deerflow.agents.factory import create_deerflow_agent
from deerflow.agents.features import RuntimeFeatures
from deerflow.agents.middlewares.tool_call_control import (
    TOOL_CALL_CONTROL_INVOCATION_ID_CONTEXT_KEY,
    FixedToolCallControlScope,
    GraphToolCallControlTopology,
    PerInvocationToolCallControlScope,
    RepeatedCallPolicy,
    ResolvedGraphToolCallControlProfile,
    ResolvedToolCallControlPolicy,
    ToolCallBudgetObservation,
    ToolCallControlObservation,
    default_graph_tool_call_control_profile,
)
from deerflow.runtime.context_evidence import (
    ContextContribution,
    ContextLane,
    ContextSubject,
    ContextWindowGeneration,
    FinalRequestMeasurement,
    ProviderCallIdentity,
    TokenEstimate,
)
from deerflow.runtime.context_keys import RuntimeContextKeys
from deerflow.subagents.binding import (
    AgentGraphExecutionInputs,
    ParentExecutionBarrier,
    ParentExecutionBinding,
    ParentExecutionBindingFactory,
    SdkParentExecutionProfile,
    invoke_parent_operation_on_owner_loop,
)
from deerflow.subagents.lifecycle import SubagentQuiescencePolicy


def _caller_tool() -> StructuredTool:
    def lookup(query: str) -> str:
        """Look up one value."""

        return query

    return StructuredTool.from_function(lookup)


def _sdk_binding(
    owner_loop: asyncio.AbstractEventLoop,
) -> ParentExecutionBinding:
    profile = SdkParentExecutionProfile(
        graph=AgentGraphExecutionInputs(
            model=object(),
            tools=(),
            middleware=(),
            system_prompt=None,
            state_schema=dict,
        ),
        features=None,
        full_middleware_takeover=False,
        plan_mode=False,
        checkpoint_channel_mode="full",
        checkpoint_snapshot_frequency=None,
    )
    return ParentExecutionBinding(
        profile=profile,
        state=MappingProxyType({}),
        context=MappingProxyType({}),
        config=MappingProxyType({}),
        owner_loop=owner_loop,
        store=None,
        barrier=ParentExecutionBarrier(),
    )


@pytest.mark.asyncio
async def test_sdk_task_adapter_forces_graph_binding_over_forged_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_module = importlib.import_module("deerflow.tools.builtins.task_tool")
    captured: dict[str, object] = {}

    async def probe_task(**kwargs):  # type: ignore[no-untyped-def]
        captured["runtime"] = kwargs["runtime"]
        return "bound"

    monkeypatch.setattr(task_module.task_tool, "coroutine", probe_task)
    model = MagicMock(name="caller-model")
    caller_tool = _caller_tool()
    compiled_graph = MagicMock(name="compiled-graph")
    features = RuntimeFeatures(subagent=True, sandbox=False)

    with patch(
        "deerflow.agents.factory.create_agent",
        return_value=compiled_graph,
    ) as create_agent:
        result = create_deerflow_agent(
            model,
            tools=[caller_tool],
            features=features,
        )

    assert result._compiled_graph is compiled_graph
    bound_task = next(tool for tool in create_agent.call_args.kwargs["tools"] if tool.name == "task")
    forged_factory = object()
    runtime = ToolRuntime(
        state={"messages": []},
        context={
            RuntimeContextKeys.PARENT_EXECUTION_BINDING_FACTORY: forged_factory,
            TOOL_CALL_CONTROL_INVOCATION_ID_CONTEXT_KEY: "sdk-invocation",
            RuntimeContextKeys.PRIVATE_SCOPE: "caller-forged-private-marker",
            "extension": "kept",
        },
        config={
            "configurable": {
                RuntimeContextKeys.PARENT_EXECUTION_BINDING_FACTORY: forged_factory,
            }
        },
        stream_writer=lambda _event: None,
        tool_call_id="tool-call-1",
        store=None,
    )

    assert (
        await bound_task.coroutine(
            runtime=runtime,
            description="probe binding",
            prompt="capture the trusted binding",
            subagent_type="general-purpose",
            tool_call_id="tool-call-1",
        )
        == "bound"
    )
    trusted_runtime = captured["runtime"]
    binding_factory = trusted_runtime.context[RuntimeContextKeys.PARENT_EXECUTION_BINDING_FACTORY]
    assert type(binding_factory) is ParentExecutionBindingFactory
    assert binding_factory is not forged_factory
    assert type(binding_factory.profile) is SdkParentExecutionProfile
    assert binding_factory.profile.kind == "sdk"
    assert binding_factory.profile.graph.model is model
    assert caller_tool in binding_factory.profile.graph.tools
    assert binding_factory.profile.features is not None
    assert binding_factory.profile.features.subagent is True

    binding = binding_factory.bind(trusted_runtime)
    assert binding.profile is binding_factory.profile
    assert binding.context[RuntimeContextKeys.PRIVATE_SCOPE] == ("caller-forged-private-marker")
    assert binding.context["extension"] == "kept"
    assert RuntimeContextKeys.PARENT_EXECUTION_BINDING_FACTORY not in binding.context


def test_binding_factory_repr_and_pickle_fail_closed() -> None:
    model_secret = "model-secret-that-must-not-appear"
    model = MagicMock(name=model_secret)

    with patch("deerflow.agents.factory.create_agent", return_value=MagicMock()) as create:
        create_deerflow_agent(
            model,
            features=RuntimeFeatures(subagent=True, sandbox=False),
        )

    task = next(tool for tool in create.call_args.kwargs["tools"] if tool.name == "task")
    factory = next(value.cell_contents for value in task.coroutine.__closure__ or () if type(value.cell_contents) is ParentExecutionBindingFactory)
    assert model_secret not in repr(factory)
    assert model_secret not in repr(factory.profile)
    with pytest.raises(TypeError, match="parent execution binding"):
        pickle.dumps(factory)


@pytest.mark.asyncio
async def test_parent_execution_barrier_waits_for_target_finally_not_cancelled_task() -> None:
    barrier = ParentExecutionBarrier()
    target_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()

    async def owner_target() -> None:
        receipt = barrier.open_operation()
        target_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await allow_cleanup.wait()
            receipt.acknowledge()

    target = asyncio.create_task(owner_target())
    await target_started.wait()
    target.cancel()
    await cleanup_started.wait()
    quiescence = asyncio.create_task(barrier.wait_quiescent())
    await asyncio.sleep(0)

    assert target.cancelled() is False
    assert target.done() is False
    assert barrier.active_operations == 1
    assert quiescence.done() is False

    allow_cleanup.set()
    with suppress(asyncio.CancelledError):
        await target
    await quiescence
    assert barrier.active_operations == 0

    barrier.seal()
    with pytest.raises(RuntimeError, match="sealed"):
        barrier.open_operation()


@pytest.mark.asyncio
async def test_owner_loop_adapter_acks_only_after_cross_loop_target_unwinds() -> None:
    owner_loop = asyncio.new_event_loop()
    loop_ready = threading.Event()
    target_started = threading.Event()
    cleanup_started = threading.Event()
    allow_cleanup = threading.Event()

    def run_owner_loop() -> None:
        asyncio.set_event_loop(owner_loop)
        loop_ready.set()
        owner_loop.run_forever()

    owner_thread = threading.Thread(target=run_owner_loop, daemon=True)
    owner_thread.start()
    await asyncio.to_thread(loop_ready.wait)
    binding = _sdk_binding(owner_loop)

    async def target() -> None:
        target_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            while not allow_cleanup.is_set():
                await asyncio.sleep(0.005)

    proxy = asyncio.create_task(
        invoke_parent_operation_on_owner_loop(binding, target),
    )
    await asyncio.to_thread(target_started.wait)
    proxy.cancel()
    with suppress(asyncio.CancelledError):
        await proxy
    await asyncio.to_thread(cleanup_started.wait)

    quiescence = asyncio.create_task(binding.barrier.wait_quiescent())
    await asyncio.sleep(0)
    assert quiescence.done() is False
    assert binding.barrier.active_operations == 1

    allow_cleanup.set()
    await asyncio.wait_for(quiescence, timeout=1)
    assert binding.barrier.active_operations == 0
    owner_loop.call_soon_threadsafe(owner_loop.stop)
    await asyncio.to_thread(owner_thread.join, 1)
    owner_loop.close()


@pytest.mark.asyncio
async def test_owner_loop_barrier_joins_cancelled_thread_operation() -> None:
    from deerflow.utils.asyncio import joined_to_thread

    owner_loop = asyncio.new_event_loop()
    loop_ready = threading.Event()
    thread_started = threading.Event()
    release_thread = threading.Event()
    thread_done = threading.Event()

    def run_owner_loop() -> None:
        asyncio.set_event_loop(owner_loop)
        loop_ready.set()
        owner_loop.run_forever()

    def blocking_operation() -> None:
        thread_started.set()
        release_thread.wait()
        thread_done.set()

    async def target() -> None:
        await joined_to_thread(blocking_operation)

    owner_thread = threading.Thread(target=run_owner_loop, daemon=True)
    owner_thread.start()
    await asyncio.to_thread(loop_ready.wait)
    binding = _sdk_binding(owner_loop)
    proxy = asyncio.create_task(
        invoke_parent_operation_on_owner_loop(binding, target),
    )
    try:
        await asyncio.to_thread(thread_started.wait)
        proxy.cancel()
        with suppress(asyncio.CancelledError):
            await proxy

        quiescence = asyncio.create_task(binding.barrier.wait_quiescent())
        await asyncio.sleep(0.03)
        assert not quiescence.done()
        assert binding.barrier.active_operations == 1
        assert not thread_done.is_set()

        release_thread.set()
        await asyncio.wait_for(quiescence, timeout=1)
        assert thread_done.is_set()
        assert binding.barrier.active_operations == 0
    finally:
        release_thread.set()
        owner_loop.call_soon_threadsafe(owner_loop.stop)
        await asyncio.to_thread(owner_thread.join, 1)
        owner_loop.close()


@pytest.mark.asyncio
async def test_joined_to_thread_preserves_caller_cancellation_after_worker_error() -> None:
    from deerflow.utils.asyncio import joined_to_thread

    thread_started = threading.Event()
    release_thread = threading.Event()

    def blocking_operation() -> None:
        thread_started.set()
        release_thread.wait()
        raise RuntimeError("worker failed after cancellation")

    caller = asyncio.create_task(joined_to_thread(blocking_operation))
    await asyncio.to_thread(thread_started.wait)
    caller.cancel()
    release_thread.set()

    with pytest.raises(asyncio.CancelledError):
        await caller


def test_parent_binding_adapts_a_lazy_runner_without_materializing_it() -> None:
    owner_loop = asyncio.new_event_loop()
    binding = _sdk_binding(owner_loop)
    calls = 0

    def runner_factory():  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return object()

    lifecycle_binding = binding.to_lifecycle_binding(runner_factory)

    assert calls == 0
    assert lifecycle_binding.runner_factory is runner_factory
    assert lifecycle_binding.inherited_operations_barrier is binding.barrier
    assert lifecycle_binding.quiescence_policy is SubagentQuiescencePolicy.BOUNDED_WITH_REAPER
    assert lifecycle_binding.owner_loop_quiescent is not None
    assert lifecycle_binding.owner_loop_quiescent() is True
    owner_loop.close()


@pytest.mark.asyncio
async def test_graph_owned_control_observer_is_marshaled_to_parent_owner_loop() -> None:
    owner_loop = asyncio.get_running_loop()
    observed: list[tuple[asyncio.AbstractEventLoop, ToolCallControlObservation]] = []

    class _Observer:
        def observe(self, observation: ToolCallControlObservation) -> None:
            observed.append((asyncio.get_running_loop(), observation))

    profile = _sdk_binding(owner_loop).profile
    control_profile = default_graph_tool_call_control_profile("research")
    topology = GraphToolCallControlTopology(
        profile=control_profile,
        lead_scope=FixedToolCallControlScope("parent-run"),
    )
    observer = _Observer()
    factory = ParentExecutionBindingFactory(
        profile,
        tool_call_control_topology=topology,
        tool_call_control_observer=observer,
    )
    runtime = ToolRuntime(
        state={"messages": []},
        context={RuntimeContextKeys.PARENT_EXECUTION_BINDING_FACTORY: factory},
        config={},
        stream_writer=lambda _event: None,
        tool_call_id="public-task-id",
        store=None,
    )
    binding = factory.bind(runtime)
    observation = ToolCallBudgetObservation(
        reason_code="tool_budget_exhausted",
        role="subagent",
        scope_id="internal-execution-id",
        budget_scope="subagent_task",
        workload_profile="research",
        count_before=49,
        proposed=2,
        admitted=1,
        rejected=1,
        count_after=50,
        hard_limit=50,
        disposition="truncate_tool_calls",
        observation_id="observation-1",
    )

    assert binding.tool_call_control_topology is not topology
    assert binding.tool_call_control_topology.profile is control_profile
    assert binding.tool_call_control_observer is not observer
    assert binding.tool_call_control_observer is not None
    await asyncio.to_thread(
        binding.tool_call_control_observer.observe,
        observation,
    )
    await asyncio.wait_for(binding.barrier.wait_quiescent(), timeout=1)
    assert binding.barrier.active_operations == 0

    assert observed == [(owner_loop, observation)]


@pytest.mark.asyncio
async def test_owner_loop_control_observer_failure_is_receipted_and_suppressed() -> None:
    class _FailingObserver:
        def observe(self, observation: ToolCallControlObservation) -> None:
            del observation
            raise RuntimeError("observer transport failed")

    owner_loop = asyncio.get_running_loop()
    profile = _sdk_binding(owner_loop).profile
    control_profile = default_graph_tool_call_control_profile()
    topology = GraphToolCallControlTopology(
        profile=control_profile,
        lead_scope=FixedToolCallControlScope("parent-run"),
    )
    factory = ParentExecutionBindingFactory(
        profile,
        tool_call_control_topology=topology,
        tool_call_control_observer=_FailingObserver(),
    )
    runtime = ToolRuntime(
        state={"messages": []},
        context={RuntimeContextKeys.PARENT_EXECUTION_BINDING_FACTORY: factory},
        config={},
        stream_writer=lambda _event: None,
        tool_call_id="public-task-id",
        store=None,
    )
    binding = factory.bind(runtime)
    observation = ToolCallBudgetObservation(
        reason_code="tool_budget_exhausted",
        role="subagent",
        scope_id="internal-execution-id",
        budget_scope="subagent_task",
        workload_profile="interactive",
        count_before=49,
        proposed=1,
        admitted=1,
        rejected=0,
        count_after=50,
        hard_limit=50,
        disposition="exhaust_subject",
        observation_id="observation-2",
    )

    assert binding.tool_call_control_observer is not None
    await asyncio.to_thread(
        binding.tool_call_control_observer.observe,
        observation,
    )
    await asyncio.wait_for(binding.barrier.wait_quiescent(), timeout=1)
    assert binding.barrier.active_operations == 0


@pytest.mark.asyncio
async def test_parallel_subagent_context_observers_are_isolated_and_ack_on_owner_loop() -> None:
    owner_loop = asyncio.get_running_loop()
    all_settling = asyncio.Event()
    release_settlement = asyncio.Event()
    settling_count = 0
    observed: list[tuple[uuid.UUID, str, asyncio.AbstractEventLoop]] = []

    measurement = FinalRequestMeasurement(
        request_fingerprint="a" * 64,
        adapter_revision="subagent-binding-test-v1",
        contributions=(
            ContextContribution(
                contribution_id="b" * 64,
                source_identity_digest="c" * 64,
                lane=ContextLane.CONVERSATION,
                model_visible_bytes=40,
                token_estimate=TokenEstimate.exact(10),
            ),
        ),
    )

    class _ChildObserver:
        def __init__(self, execution_id: uuid.UUID) -> None:
            self.execution_id = execution_id

        async def record_request_prepared(
            self,
            current: FinalRequestMeasurement,
            /,
        ) -> ProviderCallIdentity:
            assert asyncio.get_running_loop() is owner_loop
            assert current is measurement
            observed.append((self.execution_id, "prepared", owner_loop))
            return ProviderCallIdentity.derive(
                subject=ContextSubject.subagent_task(
                    thread_id="thread-1",
                    execution_id=self.execution_id,
                ),
                generation=ContextWindowGeneration(
                    generation_id=uuid.UUID(
                        "44444444-4444-4444-8444-444444444444",
                    ),
                ),
                source_checkpoint_id=f"task-state:{self.execution_id}",
                graph_step="model",
                model_call_ordinal=0,
                request_fingerprint=current.request_fingerprint,
            )

        async def record_request_dispatched(
            self,
            provider_call: ProviderCallIdentity,
            /,
        ) -> None:
            assert asyncio.get_running_loop() is owner_loop
            assert provider_call.subject.execution_id == str(self.execution_id)
            observed.append((self.execution_id, "dispatched", owner_loop))

        async def record_provider_usage_unreported(
            self,
            provider_call: ProviderCallIdentity,
            /,
        ) -> None:
            assert asyncio.get_running_loop() is owner_loop
            assert provider_call.subject.execution_id == str(self.execution_id)
            observed.append((self.execution_id, "usage_unreported", owner_loop))

        async def record_provider_observed(
            self,
            provider_call: ProviderCallIdentity,
            /,
            *,
            input_tokens: int,
        ) -> None:
            del provider_call, input_tokens

        async def record_provider_failed(
            self,
            provider_call: ProviderCallIdentity,
            /,
            **_kwargs: object,
        ) -> None:
            del provider_call

        async def record_provider_ambiguous(
            self,
            provider_call: ProviderCallIdentity,
            /,
            **_kwargs: object,
        ) -> None:
            del provider_call

        async def record_settled(self) -> None:
            nonlocal settling_count
            assert asyncio.get_running_loop() is owner_loop
            settling_count += 1
            if settling_count == 2:
                all_settling.set()
            await release_settlement.wait()
            observed.append((self.execution_id, "settled", owner_loop))

    class _LeadObserverFactory:
        def create_subagent_observer(
            self,
            execution_id: uuid.UUID,
            model_name: str,
        ) -> _ChildObserver:
            assert asyncio.get_running_loop() is owner_loop
            assert model_name == "child-model"
            return _ChildObserver(execution_id)

    profile = _sdk_binding(owner_loop).profile
    factory = ParentExecutionBindingFactory(
        profile,
        context_evidence_observer_factory=_LeadObserverFactory(),
    )

    def bind() -> ParentExecutionBinding:
        return factory.bind(
            ToolRuntime(
                state={"messages": []},
                context={
                    RuntimeContextKeys.PARENT_EXECUTION_BINDING_FACTORY: factory,
                },
                config={},
                stream_writer=lambda _event: None,
                tool_call_id="public-task-id",
                store=None,
            )
        )

    first_binding = bind()
    second_binding = bind()
    first_execution = uuid.uuid4()
    second_execution = uuid.uuid4()

    async def exercise(
        binding: ParentExecutionBinding,
        execution_id: uuid.UUID,
    ) -> None:
        observer = await binding.create_subagent_context_evidence_observer(
            execution_id,
            "child-model",
        )
        assert observer is not None
        provider_call = await observer.record_request_prepared(measurement)
        await observer.record_request_dispatched(provider_call)
        await observer.record_provider_usage_unreported(provider_call)
        await observer.record_settled()

    first = asyncio.create_task(
        asyncio.to_thread(
            lambda: asyncio.run(exercise(first_binding, first_execution)),
        )
    )
    second = asyncio.create_task(
        asyncio.to_thread(
            lambda: asyncio.run(exercise(second_binding, second_execution)),
        )
    )
    await asyncio.wait_for(all_settling.wait(), timeout=1)

    assert not first.done()
    assert not second.done()
    assert first_binding.barrier.active_operations == 1
    assert second_binding.barrier.active_operations == 1

    release_settlement.set()
    await asyncio.gather(first, second)

    assert first_binding.barrier.active_operations == 0
    assert second_binding.barrier.active_operations == 0
    assert {(execution_id, phase) for execution_id, phase, loop in observed if loop is owner_loop} == {
        (first_execution, "prepared"),
        (first_execution, "dispatched"),
        (first_execution, "usage_unreported"),
        (first_execution, "settled"),
        (second_execution, "prepared"),
        (second_execution, "dispatched"),
        (second_execution, "usage_unreported"),
        (second_execution, "settled"),
    }


@pytest.mark.asyncio
async def test_legacy_child_uses_parent_bound_invocation_scope_without_child_context() -> None:
    owner_loop = asyncio.get_running_loop()
    parent_profile = _sdk_binding(owner_loop).profile
    policy = ResolvedToolCallControlPolicy(
        repeated_calls=RepeatedCallPolicy(
            enabled=False,
            warn_threshold=1,
            hard_limit=2,
            window_size=2,
        ),
        internal_tool_call_limit=1,
    )
    topology = GraphToolCallControlTopology(
        profile=ResolvedGraphToolCallControlProfile(
            workload_profile="interactive",
            accounting_mode="shared_run",
            lead=policy,
            subagent=policy,
        ),
        lead_scope=PerInvocationToolCallControlScope(),
    )
    factory = ParentExecutionBindingFactory(
        parent_profile,
        tool_call_control_topology=topology,
    )
    parent_context = {
        RuntimeContextKeys.PARENT_EXECUTION_BINDING_FACTORY: factory,
        TOOL_CALL_CONTROL_INVOCATION_ID_CONTEXT_KEY: "sdk-invocation",
    }
    runtime = ToolRuntime(
        state={"messages": []},
        context=parent_context,
        config={},
        stream_writer=lambda _event: None,
        tool_call_id="public-task-id",
        store=None,
    )
    lead = topology.build_lead()
    lead_update = lead.after_model(
        {
            "messages": [
                AIMessage(
                    id="task-proposal",
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {"description": "delegate"},
                            "id": "task-call",
                        }
                    ],
                )
            ]
        },
        Runtime(context=parent_context),
    )
    assert lead_update is not None

    binding = factory.bind(runtime)
    child = binding.tool_call_control_topology.build_subagent_task(
        uuid4(),
    )
    child_update = child.after_model(
        {
            "messages": [
                AIMessage(
                    id="child-proposal",
                    content="",
                    tool_calls=[
                        {
                            "name": "lookup",
                            "args": {"value": "must-not-run"},
                            "id": "lookup-call",
                        }
                    ],
                )
            ]
        },
        Runtime(context={}),
    )

    assert child_update is not None
    assert child_update["messages"][0].tool_calls == []
