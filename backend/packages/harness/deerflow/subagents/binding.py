"""Opaque parent-Run inputs bound to a Sub-Agent Task invocation.

The public Agent builders create one :class:`ParentExecutionBindingFactory`
per Agent Graph and close it over a graph-local ``task`` tool Adapter.  The
Adapter installs the factory into ``ToolRuntime.context`` immediately before
the canonical task tool is called, so caller supplied context/configurable
metadata cannot select or replace the parent execution profile.

This module deliberately does not build a subagent graph or admit work to the
isolated scheduler.  The lifecycle owner materializes a runner lazily, after
its scheduler gate, from the immutable inputs captured here.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, Any, Literal

from langchain.agents.middleware import AgentMiddleware
from langchain.tools import InjectedToolCallId
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.types import Command

from deerflow.runtime.context_keys import RuntimeContextKeys

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from deerflow.agents.middlewares.tool_call_control import (
        ResolvedGraphToolCallControlProfile,
        RunToolCallLimitAuthority,
        ToolCallControlObservation,
        ToolCallControlObserver,
        ToolCallControlScope,
    )
    from deerflow.subagents.lifecycle import (
        SubagentExecutionBinding,
        SubagentRunnerFactory,
        SubagentUsageSettlementHook,
    )
    from deerflow.tools.types import Runtime
else:
    # Importing ``deerflow.tools.types`` executes the tools package initializer,
    # which imports the canonical task tool and therefore this module. Runtime
    # is annotation-only here; the graph-local tool reuses the canonical
    # StructuredTool's already-built argument schema.
    Runtime = Any


def _validate_tool_call_control_profile(value: object | None) -> None:
    if value is None:
        return
    # Delayed to avoid the tools -> task_tool -> binding initialization cycle.
    from deerflow.agents.middlewares.tool_call_control import (
        ResolvedGraphToolCallControlProfile,
    )

    if type(value) is not ResolvedGraphToolCallControlProfile:
        raise TypeError(
            "tool_call_control_profile must be ResolvedGraphToolCallControlProfile or None",
        )


class _OpaqueExecutionObject:
    """Keep model, tool, authority, and snapshot objects out of diagnostics."""

    __slots__ = ()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<opaque>)"

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("parent execution binding is not serializable")


@dataclass(frozen=True, slots=True, repr=False)
class AgentGraphExecutionInputs(_OpaqueExecutionObject):
    """Exact effective inputs used to build one parent Agent Graph.

    ``tools`` contains the canonical pre-binding tool list.  The graph-local
    task Adapter is an authority Seam only; it does not change the effective
    tool capability set represented here.
    """

    model: object
    tools: tuple[BaseTool, ...]
    middleware: tuple[object, ...]
    system_prompt: object | None
    state_schema: object
    checkpointer: object | None = None
    name: str | None = None


@dataclass(frozen=True, slots=True, repr=False)
class SdkFeatureSnapshot(_OpaqueExecutionObject):
    """Immutable copy of SDK feature choices at Agent Graph construction."""

    sandbox: object
    memory: object
    summarization: object
    subagent: object
    vision: object
    auto_title: object
    guardrail: object
    loop_detection: object
    token_budget: object
    extra_middleware: tuple[AgentMiddleware, ...] = ()

    def __post_init__(self) -> None:
        if any(not isinstance(middleware, AgentMiddleware) for middleware in self.extra_middleware):
            raise TypeError(
                "SDK delegated extra middleware must be AgentMiddleware instances",
            )

    @classmethod
    def capture(
        cls,
        features: object,
        *,
        extra_middleware: Sequence[AgentMiddleware] = (),
    ) -> SdkFeatureSnapshot:
        return cls(
            sandbox=getattr(features, "sandbox"),
            memory=getattr(features, "memory"),
            summarization=getattr(features, "summarization"),
            subagent=getattr(features, "subagent"),
            vision=getattr(features, "vision"),
            auto_title=getattr(features, "auto_title"),
            guardrail=getattr(features, "guardrail"),
            loop_detection=getattr(features, "loop_detection"),
            token_budget=getattr(features, "token_budget"),
            extra_middleware=tuple(extra_middleware),
        )


@dataclass(frozen=True, slots=True, repr=False)
class SdkParentExecutionProfile(_OpaqueExecutionObject):
    """Exact caller-owned profile for ``create_deerflow_agent``."""

    graph: AgentGraphExecutionInputs
    features: SdkFeatureSnapshot | None
    full_middleware_takeover: bool
    plan_mode: bool
    checkpoint_channel_mode: object
    checkpoint_snapshot_frequency: int | None
    kind: Literal["sdk"] = "sdk"


@dataclass(frozen=True, slots=True, repr=False)
class EmbeddedParentExecutionProfile(_OpaqueExecutionObject):
    """Exact config and trusted asset context used by ``DeerFlowClient``."""

    graph: AgentGraphExecutionInputs
    app_config: object
    asset_context: object | None
    model_name: str | None
    thinking_enabled: bool
    subagent_enabled: bool
    plan_mode: bool
    agent_name: str | None
    available_skills: tuple[str, ...] | None
    kind: Literal["embedded"] = "embedded"


@dataclass(frozen=True, slots=True, repr=False)
class ConfiguredLeadParentExecutionProfile(_OpaqueExecutionObject):
    """Resolved non-private lead Agent profile."""

    graph: AgentGraphExecutionInputs
    app_config: object
    asset_context: object | None
    agent_config: object | None
    model_name: str
    thinking_enabled: bool
    reasoning_effort: object | None
    plan_mode: bool
    subagent_enabled: bool
    agent_name: str | None
    available_skills: tuple[str, ...] | None
    kind: Literal["configured_lead"] = "configured_lead"


@dataclass(frozen=True, slots=True, repr=False)
class PrivateRunParentExecutionProfile(_OpaqueExecutionObject):
    """Exact immutable private-Run profile; never inferred from private_scope."""

    graph: AgentGraphExecutionInputs
    app_config: object
    asset_context: object | None
    private_runtime: object
    model_name: str
    thinking_enabled: bool
    reasoning_effort: object | None
    runtime_skills: tuple[object, ...]
    runtime_agent_catalog: object | None
    tool_groups: tuple[str, ...]
    kind: Literal["private_run"] = "private_run"


ParentExecutionProfile = SdkParentExecutionProfile | EmbeddedParentExecutionProfile | ConfiguredLeadParentExecutionProfile | PrivateRunParentExecutionProfile


def _complete_waiter(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)


class ParentExecutionBarrier(_OpaqueExecutionObject):
    """Track real owner-loop operations until their target ``finally`` runs.

    A ``run_coroutine_threadsafe`` future can become cancelled/done before its
    target coroutine has unwound.  Lifecycle shutdown must therefore wait for
    receipts from this barrier instead of treating proxy Futures as evidence
    of quiescence.
    """

    __slots__ = ("_active", "_lock", "_next_token", "_sealed", "_waiters")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: set[int] = set()
        self._next_token = 0
        self._sealed = False
        self._waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = set()

    def open_operation(self) -> ParentExecutionReceipt:
        """Open one owner-target operation before scheduling it."""

        with self._lock:
            if self._sealed:
                raise RuntimeError("parent execution barrier is sealed")
            self._next_token += 1
            token = self._next_token
            self._active.add(token)
        return ParentExecutionReceipt(self, token)

    def seal(self) -> None:
        """Reject new owner operations after the graph has unwound."""

        with self._lock:
            self._sealed = True

    @property
    def active_operations(self) -> int:
        with self._lock:
            return len(self._active)

    def is_quiescent(self) -> bool:
        """Return a thread-safe retirement check for the parent owner loop."""

        with self._lock:
            return not self._active

    async def wait_quiescent(self, *, timeout: float | None = None) -> None:
        """Wait until every opened receipt has acknowledged its real target."""

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        waiter = (loop, future)
        with self._lock:
            if not self._active:
                return
            self._waiters.add(waiter)
        try:
            if timeout is None:
                await future
            else:
                await asyncio.wait_for(future, timeout=timeout)
        finally:
            with self._lock:
                self._waiters.discard(waiter)

    def _acknowledge(self, token: int) -> bool:
        waiters: tuple[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]], ...] = ()
        with self._lock:
            if token not in self._active:
                return False
            self._active.remove(token)
            if not self._active:
                waiters = tuple(self._waiters)
                self._waiters.clear()
        for loop, future in waiters:
            try:
                loop.call_soon_threadsafe(_complete_waiter, future)
            except RuntimeError:
                # The waiter loop is already closed; the operation receipt is
                # still correctly acknowledged for every remaining waiter.
                pass
        return True


class ParentExecutionReceipt(_OpaqueExecutionObject):
    """Exactly-once acknowledgement token for one owner-loop target."""

    __slots__ = ("_barrier", "_lock", "_token")

    def __init__(self, barrier: ParentExecutionBarrier, token: int) -> None:
        self._barrier = barrier
        self._token = token
        self._lock = threading.Lock()

    def acknowledge(self) -> bool:
        """Acknowledge from the target coroutine's real ``finally`` block."""

        with self._lock:
            if self._token is None:
                return False
            token = self._token
            self._token = None
        return self._barrier._acknowledge(token)


class _ParentOwnerLoopToolCallControlObserver(_OpaqueExecutionObject):
    """Deliver one graph observation on its parent execution owner loop."""

    __slots__ = ("_barrier", "_owner_loop", "_target")

    def __init__(
        self,
        *,
        target: ToolCallControlObserver,
        owner_loop: asyncio.AbstractEventLoop,
        barrier: ParentExecutionBarrier,
    ) -> None:
        self._target = target
        self._owner_loop = owner_loop
        self._barrier = barrier

    def observe(self, observation: ToolCallControlObservation) -> None:
        """Schedule one receipt; delivery failure never changes enforcement."""

        receipt = self._barrier.open_operation()

        def deliver() -> None:
            try:
                self._target.observe(observation)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Tool-call control observation delivery failed: reason_code=%s role=%s exception_type=%s",
                    observation.reason_code,
                    observation.role,
                    type(exc).__name__,
                )
            finally:
                receipt.acknowledge()

        if self._owner_loop.is_closed() or not self._owner_loop.is_running():
            receipt.acknowledge()
            logger.error(
                "Tool-call control observation owner loop is unavailable: reason_code=%s role=%s",
                observation.reason_code,
                observation.role,
            )
            return
        try:
            self._owner_loop.call_soon_threadsafe(deliver)
        except RuntimeError:
            receipt.acknowledge()
            logger.error(
                "Tool-call control observation owner loop rejected delivery: reason_code=%s role=%s",
                observation.reason_code,
                observation.role,
            )


@dataclass(frozen=True, slots=True, repr=False)
class ParentExecutionBinding(_OpaqueExecutionObject):
    """One Sub-Agent Task's immutable parent Run snapshot and authority."""

    profile: ParentExecutionProfile
    state: Mapping[str, Any]
    context: Mapping[str, Any]
    config: Mapping[str, Any]
    owner_loop: asyncio.AbstractEventLoop
    store: object | None
    barrier: ParentExecutionBarrier
    tool_call_control_profile: ResolvedGraphToolCallControlProfile | None = None
    tool_call_control_observer: ToolCallControlObserver | None = None
    tool_call_limit_authority: RunToolCallLimitAuthority | None = None
    tool_call_limit_scope_id: str | None = None

    def __post_init__(self) -> None:
        _validate_tool_call_control_profile(self.tool_call_control_profile)
        if self.tool_call_control_observer is not None and not callable(
            getattr(self.tool_call_control_observer, "observe", None),
        ):
            raise TypeError(
                "tool_call_control_observer must implement observe()",
            )
        if self.tool_call_control_observer is not None and self.tool_call_control_profile is None:
            raise ValueError(
                "tool_call_control_observer requires a resolved graph profile",
            )
        if self.tool_call_control_profile is not None:
            from deerflow.agents.middlewares.tool_call_control import (
                RunToolCallLimitAuthority,
            )

            if not isinstance(
                self.tool_call_limit_authority,
                RunToolCallLimitAuthority,
            ):
                raise TypeError(
                    "tool_call_limit_authority is required with a resolved graph profile",
                )
            if not isinstance(self.tool_call_limit_scope_id, str) or not self.tool_call_limit_scope_id.strip():
                raise ValueError(
                    "tool_call_limit_scope_id is required with a resolved graph profile",
                )

    def to_lifecycle_binding(
        self,
        runner_factory: SubagentRunnerFactory,
        *,
        settle_usage: SubagentUsageSettlementHook | None = None,
    ) -> SubagentExecutionBinding:
        """Adapt exact parent inputs to the lifecycle's lazy runner Seam.

        ``runner_factory`` must only close over resolved construction inputs;
        the lifecycle invokes it after scheduler admission.  In particular,
        callers must not pass an already-created graph runner.
        """

        from deerflow.subagents.lifecycle import (
            SubagentExecutionBinding,
            SubagentQuiescencePolicy,
        )

        if not callable(runner_factory):
            raise TypeError("runner_factory must be callable")
        policy = SubagentQuiescencePolicy.REQUIRED_BEFORE_RETURN if type(self.profile) is PrivateRunParentExecutionProfile else SubagentQuiescencePolicy.BOUNDED_WITH_REAPER
        return SubagentExecutionBinding(
            runner_factory=runner_factory,
            quiescence_policy=policy,
            inherited_operations_barrier=self.barrier,
            owner_loop_quiescent=self.barrier.is_quiescent,
            settle_usage=settle_usage,
        )


@dataclass(frozen=True, slots=True, repr=False)
class ParentExecutionBindingFactory(_OpaqueExecutionObject):
    """Graph-owned factory for per-Sub-Agent Task parent bindings."""

    profile: ParentExecutionProfile
    tool_call_control_profile: ResolvedGraphToolCallControlProfile | None = None
    tool_call_control_observer: ToolCallControlObserver | None = None
    tool_call_limit_authority: RunToolCallLimitAuthority | None = None
    tool_call_limit_scope: ToolCallControlScope | None = None

    def __post_init__(self) -> None:
        if type(self.profile) not in {
            SdkParentExecutionProfile,
            EmbeddedParentExecutionProfile,
            ConfiguredLeadParentExecutionProfile,
            PrivateRunParentExecutionProfile,
        }:
            raise TypeError("unsupported parent execution profile")
        _validate_tool_call_control_profile(self.tool_call_control_profile)
        if self.tool_call_control_observer is not None and not callable(
            getattr(self.tool_call_control_observer, "observe", None),
        ):
            raise TypeError(
                "tool_call_control_observer must implement observe()",
            )
        if self.tool_call_control_observer is not None and self.tool_call_control_profile is None:
            raise ValueError(
                "tool_call_control_observer requires a resolved graph profile",
            )
        if self.tool_call_control_profile is not None:
            from deerflow.agents.middlewares.tool_call_control import (
                FixedToolCallControlScope,
                PerInvocationToolCallControlScope,
                RunToolCallLimitAuthority,
            )

            if not isinstance(
                self.tool_call_limit_authority,
                RunToolCallLimitAuthority,
            ):
                raise TypeError(
                    "tool_call_limit_authority is required with a resolved graph profile",
                )
            if not isinstance(
                self.tool_call_limit_scope,
                (FixedToolCallControlScope, PerInvocationToolCallControlScope),
            ):
                raise TypeError(
                    "tool_call_limit_scope is required with a resolved graph profile",
                )

    def bind(self, runtime: Runtime) -> ParentExecutionBinding:
        """Capture one invocation without materializing a subagent runner."""

        if runtime is None:
            raise TypeError("ToolRuntime is required for parent execution binding")
        raw_context = runtime.context if isinstance(runtime.context, Mapping) else {}
        if raw_context.get(RuntimeContextKeys.PARENT_EXECUTION_BINDING_FACTORY) is not self:
            raise PermissionError("parent execution binding factory is not graph-authoritative")
        state = runtime.state if isinstance(runtime.state, Mapping) else {}
        config = runtime.config if isinstance(runtime.config, Mapping) else {}
        # The factory key is an internal construction capability, not parent
        # Run authority inherited by the child graph itself.
        context = {key: value for key, value in raw_context.items() if key != RuntimeContextKeys.PARENT_EXECUTION_BINDING_FACTORY}
        owner_loop = asyncio.get_running_loop()
        barrier = ParentExecutionBarrier()
        observer = (
            None
            if self.tool_call_control_observer is None
            else _ParentOwnerLoopToolCallControlObserver(
                target=self.tool_call_control_observer,
                owner_loop=owner_loop,
                barrier=barrier,
            )
        )
        limit_scope_id = None if self.tool_call_limit_scope is None else self.tool_call_limit_scope.resolve(runtime)
        return ParentExecutionBinding(
            profile=self.profile,
            state=MappingProxyType(dict(state)),
            context=MappingProxyType(context),
            config=MappingProxyType(dict(config)),
            owner_loop=owner_loop,
            store=runtime.store,
            barrier=barrier,
            tool_call_control_profile=self.tool_call_control_profile,
            tool_call_control_observer=observer,
            tool_call_limit_authority=self.tool_call_limit_authority,
            tool_call_limit_scope_id=limit_scope_id,
        )


async def invoke_parent_operation_on_owner_loop(
    binding: ParentExecutionBinding,
    target: object,
    *args: object,
    **kwargs: object,
) -> object:
    """Invoke an inherited target and receipt its *actual* async unwind.

    Owner-loop proxy Adapters should delegate through this helper.  Cancelling
    the awaiting wrapper also requests cancellation of the owner task, but the
    receipt remains active until the target coroutine's real ``finally`` runs.
    """

    if type(binding) is not ParentExecutionBinding:
        raise TypeError("binding must be ParentExecutionBinding")
    if not callable(target):
        raise TypeError("target must be callable")

    receipt = binding.barrier.open_operation()

    async def invoke() -> object:
        try:
            result = target(*args, **kwargs)
            return await result if inspect.isawaitable(result) else result
        finally:
            receipt.acknowledge()

    if asyncio.get_running_loop() is binding.owner_loop:
        return await invoke()

    coroutine = invoke()
    try:
        future = asyncio.run_coroutine_threadsafe(coroutine, binding.owner_loop)
    except BaseException:
        coroutine.close()
        receipt.acknowledge()
        raise
    try:
        return await asyncio.wrap_future(future)
    except asyncio.CancelledError:
        future.cancel()
        raise


def build_task_tool(
    binding_factory: ParentExecutionBindingFactory,
) -> StructuredTool:
    """Return a graph-local task tool that installs *binding_factory*.

    The factory lives only in this Python closure.  It is never copied into
    Runnable configurable metadata, checkpoints, or the tool's serializable
    metadata fields.
    """

    if type(binding_factory) is not ParentExecutionBindingFactory:
        raise TypeError("binding_factory must be ParentExecutionBindingFactory")

    from deerflow.tools.builtins import task_tool

    original_coroutine = task_tool.coroutine
    if original_coroutine is None:
        raise RuntimeError("canonical task tool has no async implementation")

    async def bound_task_tool(
        runtime: Runtime,
        description: str,
        prompt: str,
        subagent_type: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> str | Command:
        parent_context = runtime.context if isinstance(runtime.context, Mapping) else {}
        trusted_context = dict(parent_context)
        trusted_context[RuntimeContextKeys.PARENT_EXECUTION_BINDING_FACTORY] = binding_factory
        trusted_runtime = replace(runtime, context=trusted_context)
        return await original_coroutine(
            runtime=trusted_runtime,
            description=description,
            prompt=prompt,
            subagent_type=subagent_type,
            tool_call_id=tool_call_id,
        )

    bound_sync = None
    if task_tool.func is not None:
        from deerflow.tools.sync import make_sync_tool_wrapper

        bound_sync = make_sync_tool_wrapper(bound_task_tool, task_tool.name)
    return task_tool.model_copy(
        update={
            "coroutine": bound_task_tool,
            "func": bound_sync,
        },
    )


def bind_task_tool_in_tools(
    tools: Sequence[BaseTool],
    binding_factory: ParentExecutionBindingFactory,
) -> list[BaseTool]:
    """Replace only the canonical task tool; preserve caller name overrides."""

    from deerflow.tools.builtins import task_tool

    bound_task: StructuredTool | None = None
    result: list[BaseTool] = []
    for candidate in tools:
        if candidate is task_tool:
            if bound_task is None:
                bound_task = build_task_tool(binding_factory)
            result.append(bound_task)
        else:
            result.append(candidate)
    return result


__all__ = [
    "AgentGraphExecutionInputs",
    "ConfiguredLeadParentExecutionProfile",
    "EmbeddedParentExecutionProfile",
    "ParentExecutionBarrier",
    "ParentExecutionBinding",
    "ParentExecutionBindingFactory",
    "ParentExecutionProfile",
    "ParentExecutionReceipt",
    "PrivateRunParentExecutionProfile",
    "SdkFeatureSnapshot",
    "SdkParentExecutionProfile",
    "bind_task_tool_in_tools",
    "build_task_tool",
    "invoke_parent_operation_on_owner_loop",
]
