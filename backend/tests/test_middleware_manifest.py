"""Behavioral projections for assembled middleware chains."""

from __future__ import annotations

import pytest
from langchain.agents.middleware import AgentMiddleware

from deerflow.agents.middlewares.manifest import (
    MiddlewareDispatchConstraint,
    MiddlewareDispatchDirection,
    MiddlewareHook,
    MiddlewarePhase,
    assign_middleware_layer,
    describe_middleware_chain,
    middleware_dispatch_direction,
    middleware_dispatch_order,
    validate_middleware_dispatch_constraints,
    validate_middleware_phase_ladder,
)


class _AllHooks(AgentMiddleware):
    def before_agent(self, state, runtime):
        return None

    def before_model(self, state, runtime):
        return None

    def wrap_model_call(self, request, handler):
        return handler(request)

    def wrap_tool_call(self, request, handler):
        return handler(request)

    def after_model(self, state, runtime):
        return None

    def after_agent(self, state, runtime):
        return None


class _AsyncAfterModel(AgentMiddleware):
    async def aafter_model(self, state, runtime):
        return None


def test_manifest_projects_runtime_dispatch_semantics() -> None:
    chain = [_AllHooks(), _AsyncAfterModel()]

    assert middleware_dispatch_order(
        chain,
        MiddlewareHook.BEFORE_MODEL,
    ) == ("_AllHooks",)
    assert middleware_dispatch_order(
        chain,
        MiddlewareHook.WRAP_MODEL_CALL,
    ) == ("_AllHooks",)
    assert middleware_dispatch_order(
        chain,
        MiddlewareHook.AFTER_MODEL,
    ) == ("_AsyncAfterModel", "_AllHooks")


def test_manifest_declares_each_hook_direction_once() -> None:
    assert (
        middleware_dispatch_direction(
            MiddlewareHook.BEFORE_AGENT,
        )
        is MiddlewareDispatchDirection.FORWARD
    )
    assert (
        middleware_dispatch_direction(
            MiddlewareHook.WRAP_TOOL_CALL,
        )
        is MiddlewareDispatchDirection.OUTER_FIRST
    )
    assert (
        middleware_dispatch_direction(
            MiddlewareHook.AFTER_AGENT,
        )
        is MiddlewareDispatchDirection.REVERSE
    )


def test_manifest_render_is_a_reviewable_literal_ladder() -> None:
    description = describe_middleware_chain(
        [_AllHooks(), _AsyncAfterModel()],
    )

    assert description.render().splitlines() == [
        "00 _AllHooks [before_agent, before_model, wrap_model_call, wrap_tool_call, after_model, after_agent]",
        "01 _AsyncAfterModel [after_model]",
    ]


def test_phase_ladder_rejects_reordered_registration() -> None:
    later = assign_middleware_layer(
        _AllHooks(),
        layer_id="later",
        phase=MiddlewarePhase.THREAD_INFRA,
        slot=10,
        why="test",
    )
    earlier = assign_middleware_layer(
        _AllHooks(),
        layer_id="earlier",
        phase=MiddlewarePhase.UNTRUSTED_CONTENT,
        slot=10,
        why="test",
    )

    with pytest.raises(RuntimeError, match="not monotonic"):
        validate_middleware_phase_ladder([later, earlier])


def test_dispatch_constraint_understands_reverse_hooks() -> None:
    loop = assign_middleware_layer(
        _AllHooks(),
        layer_id="loop",
        phase=MiddlewarePhase.TOOL_CALL_ARBITRATION,
        slot=20,
        why="test",
    )
    safety = assign_middleware_layer(
        _AllHooks(),
        layer_id="safety",
        phase=MiddlewarePhase.TOOL_CALL_ARBITRATION,
        slot=50,
        why="test",
    )
    constraint = MiddlewareDispatchConstraint(
        name="safety before loop",
        hook=MiddlewareHook.AFTER_MODEL,
        first="safety",
        then="loop",
        why="test",
    )

    validate_middleware_dispatch_constraints([loop, safety], [constraint])
    with pytest.raises(RuntimeError, match="safety before loop"):
        validate_middleware_dispatch_constraints([safety, loop], [constraint])
