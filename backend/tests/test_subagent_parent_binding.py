"""Parent execution binding and graph-local task Adapter contracts."""

from __future__ import annotations

import asyncio
import importlib
import pickle
import threading
from contextlib import suppress
from types import MappingProxyType
from unittest.mock import MagicMock, patch

import pytest
from langchain.tools import ToolRuntime
from langchain_core.tools import StructuredTool

from deerflow.agents.factory import create_deerflow_agent
from deerflow.agents.features import RuntimeFeatures
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

    assert result is compiled_graph
    bound_task = next(tool for tool in create_agent.call_args.kwargs["tools"] if tool.name == "task")
    forged_factory = object()
    runtime = ToolRuntime(
        state={"messages": []},
        context={
            RuntimeContextKeys.PARENT_EXECUTION_BINDING_FACTORY: forged_factory,
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
