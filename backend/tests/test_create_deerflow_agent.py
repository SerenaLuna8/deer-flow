"""Tests for create_deerflow_agent SDK entry point."""

from types import SimpleNamespace
from typing import ClassVar, get_type_hints
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from deerflow.agents.factory import create_deerflow_agent
from deerflow.agents.features import Next, Prev, RuntimeFeatures
from deerflow.agents.middlewares.host_execution_batch_barrier_middleware import (
    HostExecutionBatchBarrierMiddleware,
)
from deerflow.agents.middlewares.subagent_limit_middleware import (
    SubagentLimitMiddleware,
)
from deerflow.agents.middlewares.tool_call_control import (
    TOOL_CALL_CONTROL_INVOCATION_ID_CONTEXT_KEY,
    TOOL_CALL_CONTROL_STATE_KEY,
    ToolCallControl,
)
from deerflow.agents.middlewares.tool_error_handling_middleware import (
    ToolErrorHandlingMiddleware,
)
from deerflow.agents.middlewares.view_image_middleware import ViewImageMiddleware
from deerflow.agents.thread_state import ThreadState
from deerflow.sandbox.sandbox import AuthorizationRevoked
from deerflow.subagents.binding import ParentExecutionBindingFactory


def _make_mock_model():
    return MagicMock(name="mock_model")


def _make_mock_tool(name: str = "my_tool"):
    tool = MagicMock(name=name)
    tool.name = name
    return tool


# ---------------------------------------------------------------------------
# 1. Minimal creation — only model
# ---------------------------------------------------------------------------
@patch("deerflow.agents.factory.create_agent")
def test_minimal_creation(mock_create_agent):
    mock_create_agent.return_value = MagicMock(name="compiled_graph")
    model = _make_mock_model()

    result = create_deerflow_agent(model)

    mock_create_agent.assert_called_once()
    assert result._compiled_graph is mock_create_agent.return_value
    call_kwargs = mock_create_agent.call_args[1]
    assert call_kwargs["model"] is model
    assert call_kwargs["system_prompt"] is None


# ---------------------------------------------------------------------------
# 2. With tools
# ---------------------------------------------------------------------------
@patch("deerflow.agents.factory.create_agent")
def test_with_tools(mock_create_agent):
    mock_create_agent.return_value = MagicMock()
    model = _make_mock_model()
    tool = _make_mock_tool("search")

    create_deerflow_agent(model, tools=[tool])

    call_kwargs = mock_create_agent.call_args[1]
    tool_names = [t.name for t in call_kwargs["tools"]]
    assert "search" in tool_names


# ---------------------------------------------------------------------------
# 3. With system_prompt
# ---------------------------------------------------------------------------
@patch("deerflow.agents.factory.create_agent")
def test_with_system_prompt(mock_create_agent):
    mock_create_agent.return_value = MagicMock()
    prompt = "You are a helpful assistant."

    create_deerflow_agent(_make_mock_model(), system_prompt=prompt)

    call_kwargs = mock_create_agent.call_args[1]
    assert call_kwargs["system_prompt"] == prompt


# ---------------------------------------------------------------------------
# 4. Features mode — auto-assemble middleware chain
# ---------------------------------------------------------------------------
@patch("deerflow.agents.factory.create_agent")
def test_features_mode(mock_create_agent):
    mock_create_agent.return_value = MagicMock()
    feat = RuntimeFeatures(sandbox=True, auto_title=True)

    create_deerflow_agent(_make_mock_model(), features=feat)

    call_kwargs = mock_create_agent.call_args[1]
    middleware = call_kwargs["middleware"]
    assert len(middleware) > 0
    mw_types = [type(m).__name__ for m in middleware]
    assert "ThreadDataMiddleware" in mw_types
    assert "SandboxMiddleware" in mw_types
    assert "TitleMiddleware" in mw_types
    assert "ClarificationMiddleware" in mw_types


# ---------------------------------------------------------------------------
# 5. Middleware full takeover
# ---------------------------------------------------------------------------
@patch("deerflow.agents.factory.create_agent")
def test_middleware_takeover(mock_create_agent):
    mock_create_agent.return_value = MagicMock()
    custom_mw = MagicMock(name="custom_middleware")
    custom_mw.name = "custom"

    create_deerflow_agent(
        _make_mock_model(),
        middleware=[custom_mw],
        workload_profile="research",
    )

    call_kwargs = mock_create_agent.call_args[1]
    assert call_kwargs["middleware"] == [custom_mw]


# ---------------------------------------------------------------------------
# 6. Conflict — middleware + features raises ValueError
# ---------------------------------------------------------------------------
def test_middleware_and_features_conflict():
    with pytest.raises(ValueError, match="Cannot specify both"):
        create_deerflow_agent(
            _make_mock_model(),
            middleware=[MagicMock()],
            features=RuntimeFeatures(),
        )


# ---------------------------------------------------------------------------
# 7. Vision feature auto-injects view_image_tool when thread data is available
# ---------------------------------------------------------------------------
@patch("deerflow.agents.factory.create_agent")
def test_vision_injects_view_image_tool(mock_create_agent):
    mock_create_agent.return_value = MagicMock()
    feat = RuntimeFeatures(vision=True, sandbox=True)

    create_deerflow_agent(_make_mock_model(), features=feat)

    call_kwargs = mock_create_agent.call_args[1]
    tool_names = [t.name for t in call_kwargs["tools"]]
    assert "view_image" in tool_names


@patch("deerflow.agents.factory.create_agent")
def test_vision_without_sandbox_does_not_inject_view_image_tool(mock_create_agent):
    mock_create_agent.return_value = MagicMock()
    feat = RuntimeFeatures(vision=True, sandbox=False)

    create_deerflow_agent(_make_mock_model(), features=feat)

    call_kwargs = mock_create_agent.call_args[1]
    tool_names = [t.name for t in call_kwargs["tools"]]
    assert "view_image" not in tool_names


def test_view_image_middleware_preserves_viewed_images_reducer():
    middleware_hints = get_type_hints(ViewImageMiddleware.state_schema, include_extras=True)
    thread_hints = get_type_hints(ThreadState, include_extras=True)

    assert middleware_hints["viewed_images"] == thread_hints["viewed_images"]


# ---------------------------------------------------------------------------
# 8. Subagent feature auto-injects task_tool
# ---------------------------------------------------------------------------
@patch("deerflow.agents.factory.create_agent")
def test_subagent_injects_task_tool(mock_create_agent):
    mock_create_agent.return_value = MagicMock()
    feat = RuntimeFeatures(subagent=True, sandbox=False)

    create_deerflow_agent(_make_mock_model(), features=feat)

    call_kwargs = mock_create_agent.call_args[1]
    tool_names = [t.name for t in call_kwargs["tools"]]
    assert "task" in tool_names


# ---------------------------------------------------------------------------
# 9. Middleware ordering — ClarificationMiddleware always last
# ---------------------------------------------------------------------------
@patch("deerflow.agents.factory.create_agent")
def test_clarification_always_last(mock_create_agent):
    mock_create_agent.return_value = MagicMock()
    feat = RuntimeFeatures(sandbox=True, vision=True)

    create_deerflow_agent(_make_mock_model(), features=feat)

    call_kwargs = mock_create_agent.call_args[1]
    middleware = call_kwargs["middleware"]
    last_mw = middleware[-1]
    assert type(last_mw).__name__ == "ClarificationMiddleware"


# ---------------------------------------------------------------------------
# 10. RuntimeFeatures default values
# ---------------------------------------------------------------------------
def test_agent_features_defaults():
    f = RuntimeFeatures()
    assert f.sandbox is True
    assert f.memory is False
    assert f.summarization is False
    assert f.subagent is False
    assert f.vision is False
    assert f.auto_title is False
    assert f.guardrail is False
    assert f.loop_detection is True


# ---------------------------------------------------------------------------
# 11. Tool deduplication — user-provided tools take priority
# ---------------------------------------------------------------------------
@patch("deerflow.agents.factory.create_agent")
def test_tool_deduplication(mock_create_agent):
    """If user provides a tool with the same name as an auto-injected one, no duplicate."""
    mock_create_agent.return_value = MagicMock()
    user_clarification = _make_mock_tool("ask_clarification")

    create_deerflow_agent(_make_mock_model(), tools=[user_clarification], features=RuntimeFeatures(sandbox=False))

    call_kwargs = mock_create_agent.call_args[1]
    names = [t.name for t in call_kwargs["tools"]]
    assert names.count("ask_clarification") == 1
    # The first one should be the user-provided tool
    assert call_kwargs["tools"][0] is user_clarification


# ---------------------------------------------------------------------------
# 12. Sandbox disabled — no ThreadData/Uploads/Sandbox middleware
# ---------------------------------------------------------------------------
@patch("deerflow.agents.factory.create_agent")
def test_sandbox_disabled(mock_create_agent):
    mock_create_agent.return_value = MagicMock()
    feat = RuntimeFeatures(sandbox=False)

    create_deerflow_agent(_make_mock_model(), features=feat)

    call_kwargs = mock_create_agent.call_args[1]
    mw_types = [type(m).__name__ for m in call_kwargs["middleware"]]
    assert "ThreadDataMiddleware" not in mw_types
    assert "UploadsMiddleware" not in mw_types
    assert "SandboxMiddleware" not in mw_types


# ---------------------------------------------------------------------------
# 13. Checkpointer passed through
# ---------------------------------------------------------------------------
@patch("deerflow.agents.factory.create_agent")
def test_checkpointer_passthrough(mock_create_agent):
    mock_create_agent.return_value = MagicMock()
    cp = MagicMock(name="checkpointer")

    create_deerflow_agent(_make_mock_model(), checkpointer=cp)

    call_kwargs = mock_create_agent.call_args[1]
    assert call_kwargs["checkpointer"] is cp


# ---------------------------------------------------------------------------
# 14. Custom AgentMiddleware instance replaces default
# ---------------------------------------------------------------------------
@patch("deerflow.agents.factory.create_agent")
def test_custom_middleware_replaces_default(mock_create_agent):
    """Passing an AgentMiddleware instance uses it directly instead of the built-in default."""
    from langchain.agents.middleware import AgentMiddleware

    mock_create_agent.return_value = MagicMock()

    class MyMemoryMiddleware(AgentMiddleware):
        pass

    custom_memory = MyMemoryMiddleware()
    feat = RuntimeFeatures(sandbox=False, memory=custom_memory)

    create_deerflow_agent(_make_mock_model(), features=feat)

    call_kwargs = mock_create_agent.call_args[1]
    middleware = call_kwargs["middleware"]
    assert custom_memory in middleware
    assert middleware.count(custom_memory) == 1


def test_memory_true_requires_custom_middleware() -> None:
    with pytest.raises(ValueError, match="memory=True requires a custom AgentMiddleware"):
        create_deerflow_agent(
            _make_mock_model(),
            features=RuntimeFeatures(sandbox=False, memory=True),  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# 15. Custom sandbox middleware replaces the 3-middleware group
# ---------------------------------------------------------------------------
@patch("deerflow.agents.factory.create_agent")
def test_custom_sandbox_replaces_group(mock_create_agent):
    """Passing an AgentMiddleware for sandbox replaces ThreadData+Uploads+Sandbox with one."""
    from langchain.agents.middleware import AgentMiddleware

    mock_create_agent.return_value = MagicMock()

    class MySandbox(AgentMiddleware):
        pass

    custom_sb = MySandbox()
    feat = RuntimeFeatures(sandbox=custom_sb)

    create_deerflow_agent(_make_mock_model(), features=feat)

    call_kwargs = mock_create_agent.call_args[1]
    middleware = call_kwargs["middleware"]
    assert custom_sb in middleware
    mw_types = [type(m).__name__ for m in middleware]
    assert "ThreadDataMiddleware" not in mw_types
    assert "UploadsMiddleware" not in mw_types
    assert "SandboxMiddleware" not in mw_types


# ---------------------------------------------------------------------------
# 16. Always-on error handling middlewares are present
# ---------------------------------------------------------------------------
@patch("deerflow.agents.factory.create_agent")
def test_always_on_error_handling(mock_create_agent):
    mock_create_agent.return_value = MagicMock()
    feat = RuntimeFeatures(sandbox=False)

    create_deerflow_agent(_make_mock_model(), features=feat)

    call_kwargs = mock_create_agent.call_args[1]
    middleware = call_kwargs["middleware"]
    mw_types = [type(m).__name__ for m in middleware]
    assert "DanglingToolCallMiddleware" in mw_types
    assert "ToolErrorHandlingMiddleware" in mw_types
    tool_error_middleware = next(m for m in middleware if type(m).__name__ == "ToolErrorHandlingMiddleware")
    assert tool_error_middleware._app_config is None


# ---------------------------------------------------------------------------
# 17. Vision with custom middleware follows thread-data availability
# ---------------------------------------------------------------------------
@patch("deerflow.agents.factory.create_agent")
def test_vision_custom_middleware_without_sandbox_does_not_inject_tool(mock_create_agent):
    """Custom vision middleware without thread data does not get view_image_tool auto-injected."""
    from langchain.agents.middleware import AgentMiddleware

    mock_create_agent.return_value = MagicMock()

    class MyVision(AgentMiddleware):
        pass

    feat = RuntimeFeatures(sandbox=False, vision=MyVision())

    create_deerflow_agent(_make_mock_model(), features=feat)

    call_kwargs = mock_create_agent.call_args[1]
    tool_names = [t.name for t in call_kwargs["tools"]]
    assert "view_image" not in tool_names


# ===========================================================================
# @Next / @Prev decorators and extra_middleware insertion
# ===========================================================================


# ---------------------------------------------------------------------------
# 18. @Next decorator sets _next_anchor
# ---------------------------------------------------------------------------
def test_next_decorator():
    from langchain.agents.middleware import AgentMiddleware

    class Anchor(AgentMiddleware):
        pass

    @Next(Anchor)
    class MyMW(AgentMiddleware):
        pass

    assert MyMW._next_anchor is Anchor


# ---------------------------------------------------------------------------
# 19. @Prev decorator sets _prev_anchor
# ---------------------------------------------------------------------------
def test_prev_decorator():
    from langchain.agents.middleware import AgentMiddleware

    class Anchor(AgentMiddleware):
        pass

    @Prev(Anchor)
    class MyMW(AgentMiddleware):
        pass

    assert MyMW._prev_anchor is Anchor


# ---------------------------------------------------------------------------
# 20. extra_middleware with @Next inserts after anchor
# ---------------------------------------------------------------------------
@patch("deerflow.agents.factory.create_agent")
def test_extra_next_inserts_after_anchor(mock_create_agent):
    from langchain.agents.middleware import AgentMiddleware

    from deerflow.agents.middlewares.dangling_tool_call_middleware import DanglingToolCallMiddleware

    mock_create_agent.return_value = MagicMock()

    @Next(DanglingToolCallMiddleware)
    class MyAudit(AgentMiddleware):
        pass

    audit = MyAudit()
    create_deerflow_agent(
        _make_mock_model(),
        features=RuntimeFeatures(sandbox=False),
        extra_middleware=[audit],
    )

    call_kwargs = mock_create_agent.call_args[1]
    middleware = call_kwargs["middleware"]
    mw_types = [type(m).__name__ for m in middleware]
    dangling_idx = mw_types.index("DanglingToolCallMiddleware")
    audit_idx = mw_types.index("MyAudit")
    assert audit_idx == dangling_idx + 1


# ---------------------------------------------------------------------------
# 21. extra_middleware with @Prev inserts before anchor
# ---------------------------------------------------------------------------
@patch("deerflow.agents.factory.create_agent")
def test_extra_prev_inserts_before_anchor(mock_create_agent):
    from langchain.agents.middleware import AgentMiddleware

    from deerflow.agents.middlewares.clarification_middleware import ClarificationMiddleware

    mock_create_agent.return_value = MagicMock()

    @Prev(ClarificationMiddleware)
    class MyFilter(AgentMiddleware):
        pass

    filt = MyFilter()
    create_deerflow_agent(
        _make_mock_model(),
        features=RuntimeFeatures(sandbox=False),
        extra_middleware=[filt],
    )

    call_kwargs = mock_create_agent.call_args[1]
    middleware = call_kwargs["middleware"]
    mw_types = [type(m).__name__ for m in middleware]
    clar_idx = mw_types.index("ClarificationMiddleware")
    filt_idx = mw_types.index("MyFilter")
    assert filt_idx == clar_idx - 1


# ---------------------------------------------------------------------------
# 22. Unanchored extra_middleware goes before ClarificationMiddleware
# ---------------------------------------------------------------------------
@patch("deerflow.agents.factory.create_agent")
def test_extra_unanchored_before_clarification(mock_create_agent):
    from langchain.agents.middleware import AgentMiddleware

    mock_create_agent.return_value = MagicMock()

    class MyPlain(AgentMiddleware):
        pass

    plain = MyPlain()
    create_deerflow_agent(
        _make_mock_model(),
        features=RuntimeFeatures(sandbox=False),
        extra_middleware=[plain],
    )

    call_kwargs = mock_create_agent.call_args[1]
    middleware = call_kwargs["middleware"]
    mw_types = [type(m).__name__ for m in middleware]
    assert mw_types[-1] == "ClarificationMiddleware"
    assert mw_types[-2] == "MyPlain"
    assert middleware.count(plain) == 1


@pytest.mark.parametrize("position", [Next, Prev], ids=["next", "prev"])
@pytest.mark.parametrize(
    "anchor",
    [
        ToolCallControl,
        HostExecutionBatchBarrierMiddleware,
        SubagentLimitMiddleware,
    ],
    ids=["tool-call-control", "host-execution-batch-barrier", "subagent-limit"],
)
def test_after_model_extra_cannot_anchor_around_protected_arbitration(
    position,
    anchor,
):
    from langchain.agents.middleware import AgentMiddleware

    @position(anchor)
    class AnchoredAfterModel(AgentMiddleware):
        def after_model(self, state, runtime):
            return None

    with (
        patch(
            "deerflow.agents.factory.create_agent",
            return_value=MagicMock(),
        ),
        pytest.raises(
            ValueError,
            match=("after_model hooks cannot use @Next/@Prev.*protected custom band.*full takeover"),
        ),
    ):
        create_deerflow_agent(
            _make_mock_model(),
            features=RuntimeFeatures(sandbox=False, subagent=True),
            extra_middleware=[AnchoredAfterModel()],
        )


@patch("deerflow.agents.factory.create_agent")
def test_unanchored_after_model_extra_uses_protected_custom_band(
    mock_create_agent,
):
    from langchain.agents.middleware import AgentMiddleware

    from deerflow.agents.middlewares.manifest import (
        MiddlewareHook,
        MiddlewarePhase,
        middleware_dispatch_order,
        middleware_layer_metadata,
    )

    class UnanchoredAfterModel(AgentMiddleware):
        def after_model(self, state, runtime):
            return None

    custom = UnanchoredAfterModel()
    mock_create_agent.return_value = MagicMock()

    create_deerflow_agent(
        _make_mock_model(),
        features=RuntimeFeatures(
            sandbox=False,
            subagent=True,
            token_budget=True,
        ),
        extra_middleware=[custom],
    )

    chain = mock_create_agent.call_args.kwargs["middleware"]
    metadata = middleware_layer_metadata(custom)
    assert metadata is not None
    assert metadata.phase is MiddlewarePhase.CUSTOM
    relevant = {
        "UnanchoredAfterModel",
        "TokenBudgetMiddleware",
        "SubagentLimitMiddleware",
        "ToolCallControl",
    }
    assert tuple(name for name in middleware_dispatch_order(chain, MiddlewareHook.AFTER_MODEL) if name in relevant) == (
        "UnanchoredAfterModel",
        "TokenBudgetMiddleware",
        "SubagentLimitMiddleware",
        "ToolCallControl",
    )


@patch("deerflow.agents.factory.create_agent")
def test_anchored_after_model_extra_keeps_position_without_tool_call_control(
    mock_create_agent,
):
    from langchain.agents.middleware import AgentMiddleware

    from deerflow.agents.middlewares.clarification_middleware import (
        ClarificationMiddleware,
    )

    @Prev(ClarificationMiddleware)
    class PositionedAfterModel(AgentMiddleware):
        def after_model(self, state, runtime):
            return None

    custom = PositionedAfterModel()
    mock_create_agent.return_value = MagicMock()

    create_deerflow_agent(
        _make_mock_model(),
        features=RuntimeFeatures(sandbox=False, loop_detection=False),
        extra_middleware=[custom],
    )

    chain = mock_create_agent.call_args.kwargs["middleware"]
    assert chain.index(custom) == next(index for index, middleware in enumerate(chain) if isinstance(middleware, ClarificationMiddleware)) - 1


# ---------------------------------------------------------------------------
# 23. Conflict: two extras @Next same anchor → ValueError
# ---------------------------------------------------------------------------
def test_extra_conflict_same_next_target():
    from langchain.agents.middleware import AgentMiddleware

    from deerflow.agents.middlewares.dangling_tool_call_middleware import DanglingToolCallMiddleware

    @Next(DanglingToolCallMiddleware)
    class MW1(AgentMiddleware):
        pass

    @Next(DanglingToolCallMiddleware)
    class MW2(AgentMiddleware):
        pass

    with pytest.raises(ValueError, match="Conflict"):
        create_deerflow_agent(
            _make_mock_model(),
            features=RuntimeFeatures(sandbox=False),
            extra_middleware=[MW1(), MW2()],
        )


# ---------------------------------------------------------------------------
# 24. Conflict: two extras @Prev same anchor → ValueError
# ---------------------------------------------------------------------------
def test_extra_conflict_same_prev_target():
    from langchain.agents.middleware import AgentMiddleware

    from deerflow.agents.middlewares.clarification_middleware import ClarificationMiddleware

    @Prev(ClarificationMiddleware)
    class MW1(AgentMiddleware):
        pass

    @Prev(ClarificationMiddleware)
    class MW2(AgentMiddleware):
        pass

    with pytest.raises(ValueError, match="Conflict"):
        create_deerflow_agent(
            _make_mock_model(),
            features=RuntimeFeatures(sandbox=False),
            extra_middleware=[MW1(), MW2()],
        )


# ---------------------------------------------------------------------------
# 25. Both @Next and @Prev on same class → ValueError
# ---------------------------------------------------------------------------
def test_extra_both_next_and_prev_error():
    from langchain.agents.middleware import AgentMiddleware

    from deerflow.agents.middlewares.clarification_middleware import ClarificationMiddleware
    from deerflow.agents.middlewares.dangling_tool_call_middleware import DanglingToolCallMiddleware

    class MW(AgentMiddleware):
        pass

    MW._next_anchor = DanglingToolCallMiddleware
    MW._prev_anchor = ClarificationMiddleware

    with pytest.raises(ValueError, match="both @Next and @Prev"):
        create_deerflow_agent(
            _make_mock_model(),
            features=RuntimeFeatures(sandbox=False),
            extra_middleware=[MW()],
        )


# ---------------------------------------------------------------------------
# 26. Cross-external anchoring: extra anchors to another extra
# ---------------------------------------------------------------------------
@patch("deerflow.agents.factory.create_agent")
def test_extra_cross_external_anchoring(mock_create_agent):
    from langchain.agents.middleware import AgentMiddleware

    from deerflow.agents.middlewares.dangling_tool_call_middleware import DanglingToolCallMiddleware

    mock_create_agent.return_value = MagicMock()

    @Next(DanglingToolCallMiddleware)
    class First(AgentMiddleware):
        pass

    @Next(First)
    class Second(AgentMiddleware):
        pass

    create_deerflow_agent(
        _make_mock_model(),
        features=RuntimeFeatures(sandbox=False),
        extra_middleware=[Second(), First()],  # intentionally reversed
    )

    call_kwargs = mock_create_agent.call_args[1]
    middleware = call_kwargs["middleware"]
    mw_types = [type(m).__name__ for m in middleware]
    dangling_idx = mw_types.index("DanglingToolCallMiddleware")
    first_idx = mw_types.index("First")
    second_idx = mw_types.index("Second")
    assert first_idx == dangling_idx + 1
    assert second_idx == first_idx + 1


# ---------------------------------------------------------------------------
# 27. Unresolvable anchor → ValueError
# ---------------------------------------------------------------------------
def test_extra_unresolvable_anchor():
    from langchain.agents.middleware import AgentMiddleware

    class Ghost(AgentMiddleware):
        pass

    @Next(Ghost)
    class MW(AgentMiddleware):
        pass

    with pytest.raises(ValueError, match="Cannot resolve"):
        create_deerflow_agent(
            _make_mock_model(),
            features=RuntimeFeatures(sandbox=False),
            extra_middleware=[MW()],
        )


# ---------------------------------------------------------------------------
# 28. extra_middleware + middleware (full takeover) → ValueError
# ---------------------------------------------------------------------------
def test_extra_with_middleware_takeover_conflict():
    with pytest.raises(ValueError, match="full takeover"):
        create_deerflow_agent(
            _make_mock_model(),
            middleware=[MagicMock()],
            extra_middleware=[MagicMock()],
        )


# ===========================================================================
# LoopDetection, TodoMiddleware, GuardrailMiddleware
# ===========================================================================


# ---------------------------------------------------------------------------
# 29. ToolCallControl is the sole SDK auto-path enforcement adapter
# ---------------------------------------------------------------------------
@patch("deerflow.agents.factory.create_agent")
def test_tool_call_control_is_present_without_legacy_loop_detection(
    mock_create_agent,
):
    mock_create_agent.return_value = MagicMock()
    create_deerflow_agent(_make_mock_model(), features=RuntimeFeatures(sandbox=False))

    call_kwargs = mock_create_agent.call_args[1]
    mw_types = [type(m).__name__ for m in call_kwargs["middleware"]]
    assert "ToolCallControl" in mw_types
    assert "LoopDetectionMiddleware" not in mw_types


# ---------------------------------------------------------------------------
# 30. ToolCallControl before Clarification
# ---------------------------------------------------------------------------
@patch("deerflow.agents.factory.create_agent")
def test_tool_call_control_before_clarification(mock_create_agent):
    mock_create_agent.return_value = MagicMock()
    create_deerflow_agent(_make_mock_model(), features=RuntimeFeatures(sandbox=False))

    call_kwargs = mock_create_agent.call_args[1]
    mw_types = [type(m).__name__ for m in call_kwargs["middleware"]]
    control_idx = mw_types.index("ToolCallControl")
    clar_idx = mw_types.index("ClarificationMiddleware")
    assert control_idx < clar_idx
    assert control_idx == clar_idx - 1


# ---------------------------------------------------------------------------
# 30b. loop_detection=False skips ToolCallControl
# ---------------------------------------------------------------------------
@patch("deerflow.agents.factory.create_agent")
def test_loop_detection_disabled(mock_create_agent):
    mock_create_agent.return_value = MagicMock()
    create_deerflow_agent(
        _make_mock_model(),
        features=RuntimeFeatures(sandbox=False, loop_detection=False),
    )

    call_kwargs = mock_create_agent.call_args[1]
    mw_types = [type(m).__name__ for m in call_kwargs["middleware"]]
    assert "ToolCallControl" not in mw_types
    assert "LoopDetectionMiddleware" not in mw_types


# ---------------------------------------------------------------------------
# 30c. loop_detection=<custom AgentMiddleware> has an explicit migration error
# ---------------------------------------------------------------------------
def test_loop_detection_custom_middleware_requires_explicit_extension_seam():
    from langchain.agents.middleware import AgentMiddleware as AM

    class MyLoopDetection(AM):
        pass

    custom = MyLoopDetection()
    with pytest.raises(
        ValueError,
        match="extra_middleware.*full takeover",
    ):
        create_deerflow_agent(
            _make_mock_model(),
            features=RuntimeFeatures(sandbox=False, loop_detection=custom),
        )


@pytest.mark.parametrize("loop_detection", [None, "enabled", 1])
def test_loop_detection_compatibility_switch_is_strict(loop_detection):
    with pytest.raises(TypeError, match="loop_detection"):
        create_deerflow_agent(
            _make_mock_model(),
            features=RuntimeFeatures(
                sandbox=False,
                loop_detection=loop_detection,  # type: ignore[arg-type]
            ),
        )


@pytest.mark.parametrize("workload_profile", ["", "batch", None, [], {}])
def test_workload_profile_is_strict(workload_profile):
    with pytest.raises(ValueError, match="workload_profile"):
        create_deerflow_agent(
            _make_mock_model(),
            workload_profile=workload_profile,  # type: ignore[arg-type]
        )


@patch("deerflow.agents.factory.create_agent")
def test_cached_sdk_control_requires_and_isolates_invocation_scope(
    mock_create_agent,
):
    mock_create_agent.return_value = MagicMock()
    create_deerflow_agent(
        _make_mock_model(),
        features=RuntimeFeatures(sandbox=False),
        workload_profile="research",
    )

    control = next(middleware for middleware in mock_create_agent.call_args.kwargs["middleware"] if isinstance(middleware, ToolCallControl))
    runtime = MagicMock()
    runtime.context = {}
    with pytest.raises(RuntimeError, match="explicit invocation scope missing"):
        control.before_agent({"messages": []}, runtime)

    runtime.context = {
        TOOL_CALL_CONTROL_INVOCATION_ID_CONTEXT_KEY: "sdk-invocation-a",
    }
    first = control.before_agent({"messages": []}, runtime)
    admitted = control.after_model(
        {
            "messages": [
                HumanMessage(content="research Agent history"),
                AIMessage(
                    id="sdk-proposal-a",
                    content="",
                    tool_calls=[
                        {
                            "name": "web_search",
                            "args": {"query": "Agent history"},
                            "id": "sdk-call-a",
                        }
                    ],
                ),
            ],
            TOOL_CALL_CONTROL_STATE_KEY: first[TOOL_CALL_CONTROL_STATE_KEY],
        },
        runtime,
    )
    assert admitted is not None
    assert admitted[TOOL_CALL_CONTROL_STATE_KEY]["admitted_counts"] == {
        "web_search": 1,
    }

    runtime.context = {
        TOOL_CALL_CONTROL_INVOCATION_ID_CONTEXT_KEY: "sdk-invocation-b",
    }
    second = control.before_agent(
        {
            "messages": [],
            TOOL_CALL_CONTROL_STATE_KEY: admitted[TOOL_CALL_CONTROL_STATE_KEY],
        },
        runtime,
    )

    assert first[TOOL_CALL_CONTROL_STATE_KEY]["scope_id"] == "sdk-invocation-a"
    assert second[TOOL_CALL_CONTROL_STATE_KEY]["scope_id"] == "sdk-invocation-b"
    assert second[TOOL_CALL_CONTROL_STATE_KEY]["admitted_counts"] == {}


def test_sdk_graph_invoke_supplies_invocation_scope_and_calls_the_model():
    class CountingModel(GenericFakeChatModel):
        calls: ClassVar[int] = 0

        def bind_tools(self, tools, **kwargs):
            del tools, kwargs
            return self

        def _generate(self, *args, **kwargs):
            type(self).calls += 1
            return super()._generate(*args, **kwargs)

    model = CountingModel(messages=iter([AIMessage(content="complete")]))
    graph = create_deerflow_agent(
        model,
        features=RuntimeFeatures(sandbox=False),
    )

    result = graph.invoke({"messages": [HumanMessage(content="research")]})

    assert result["messages"][-1].content == "complete"
    assert CountingModel.calls == 1


def test_sdk_graph_stream_supplies_invocation_scope() -> None:
    class StreamModel(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            del tools, kwargs
            return self

    graph = create_deerflow_agent(
        StreamModel(messages=iter([AIMessage(content="stream complete")])),
        features=RuntimeFeatures(sandbox=False),
    )

    chunks = list(graph.stream({"messages": [HumanMessage(content="research")]}))

    assert any(message.content == "stream complete" for chunk in chunks for update in chunk.values() if isinstance(update, dict) for message in update.get("messages", []))


@pytest.mark.asyncio
async def test_sdk_graph_async_entrypoints_supply_invocation_scope() -> None:
    class AsyncModel(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            del tools, kwargs
            return self

    graph = create_deerflow_agent(
        AsyncModel(
            messages=iter(
                [
                    AIMessage(content="ainvoke complete"),
                    AIMessage(content="astream complete"),
                ]
            )
        ),
        features=RuntimeFeatures(sandbox=False),
    )

    invoked = await graph.ainvoke(
        {"messages": [HumanMessage(content="research")]},
    )
    streamed = [
        chunk
        async for chunk in graph.astream(
            {"messages": [HumanMessage(content="research again")]},
        )
    ]

    assert invoked["messages"][-1].content == "ainvoke complete"
    assert any(message.content == "astream complete" for chunk in streamed for update in chunk.values() if isinstance(update, dict) for message in update.get("messages", []))


@patch("deerflow.agents.factory.create_agent")
@pytest.mark.asyncio
async def test_sdk_public_entrypoints_generate_distinct_invocation_scopes(
    mock_create_agent,
):
    compiled_graph = MagicMock()
    compiled_graph.invoke.return_value = {"mode": "invoke"}
    compiled_graph.stream.return_value = iter([{"mode": "stream"}])
    compiled_graph.ainvoke = AsyncMock(return_value={"mode": "ainvoke"})

    async def async_chunks():
        yield {"mode": "astream"}

    compiled_graph.astream.side_effect = lambda *args, **kwargs: async_chunks()
    mock_create_agent.return_value = compiled_graph
    graph = create_deerflow_agent(
        _make_mock_model(),
        features=RuntimeFeatures(sandbox=False),
    )
    caller_context = {
        "caller": "sdk-test",
        TOOL_CALL_CONTROL_INVOCATION_ID_CONTEXT_KEY: "caller-supplied-value",
    }

    assert graph.invoke({"messages": []}, context=caller_context) == {
        "mode": "invoke",
    }
    assert list(graph.stream({"messages": []}, context=caller_context)) == [
        {"mode": "stream"},
    ]
    assert await graph.ainvoke({"messages": []}, context=caller_context) == {
        "mode": "ainvoke",
    }
    assert [chunk async for chunk in graph.astream({"messages": []}, context=caller_context)] == [{"mode": "astream"}]

    contexts = [
        compiled_graph.invoke.call_args.kwargs["context"],
        compiled_graph.stream.call_args.kwargs["context"],
        compiled_graph.ainvoke.call_args.kwargs["context"],
        compiled_graph.astream.call_args.kwargs["context"],
    ]
    invocation_ids = {context[TOOL_CALL_CONTROL_INVOCATION_ID_CONTEXT_KEY] for context in contexts}
    assert len(invocation_ids) == 4
    assert "caller-supplied-value" not in invocation_ids
    assert all(context["caller"] == "sdk-test" for context in contexts)
    assert caller_context[TOOL_CALL_CONTROL_INVOCATION_ID_CONTEXT_KEY] == ("caller-supplied-value")


def test_sync_model_call_without_private_authority_delegates() -> None:
    middleware = ToolErrorHandlingMiddleware()
    request = SimpleNamespace(runtime=SimpleNamespace(context={}))
    response = object()

    assert middleware.wrap_model_call(request, lambda _request: response) is response


@pytest.mark.parametrize(
    "authority_context",
    [
        {"private_scope": object()},
        {"__authorization_boundary": object()},
        {"__authorization_checker": lambda: None},
    ],
)
def test_sync_model_call_with_private_authority_fails_closed(
    authority_context,
) -> None:
    middleware = ToolErrorHandlingMiddleware()
    request = SimpleNamespace(
        runtime=SimpleNamespace(context=authority_context),
    )
    handler_called = False

    def handler(_request):
        nonlocal handler_called
        handler_called = True
        return object()

    with pytest.raises(AuthorizationRevoked):
        middleware.wrap_model_call(request, handler)

    assert not handler_called


@patch("deerflow.agents.factory.create_agent")
def test_research_profile_is_bound_for_delegated_sdk_execution(
    mock_create_agent,
):
    mock_create_agent.return_value = MagicMock()
    create_deerflow_agent(
        _make_mock_model(),
        features=RuntimeFeatures(sandbox=False, subagent=True),
        workload_profile="research",
    )

    bound_task = next(tool for tool in mock_create_agent.call_args.kwargs["tools"] if tool.name == "task")
    binding_factory = next(cell.cell_contents for cell in bound_task.coroutine.__closure__ or () if type(cell.cell_contents) is ParentExecutionBindingFactory)
    profile = binding_factory.tool_call_control_profile

    assert profile.workload_profile == "research"
    assert profile.lead.tool_budget.limit_for("web_search").hard_limit == 30
    assert profile.subagent.tool_budget.limit_for("web_search").hard_limit == 20


# ---------------------------------------------------------------------------
# 31. plan_mode=True adds TodoMiddleware
# ---------------------------------------------------------------------------
@patch("deerflow.agents.factory.create_agent")
def test_plan_mode_adds_todo_middleware(mock_create_agent):
    mock_create_agent.return_value = MagicMock()
    create_deerflow_agent(_make_mock_model(), features=RuntimeFeatures(sandbox=False), plan_mode=True)

    call_kwargs = mock_create_agent.call_args[1]
    mw_types = [type(m).__name__ for m in call_kwargs["middleware"]]
    assert "TodoMiddleware" in mw_types


# ---------------------------------------------------------------------------
# 32. plan_mode=False (default) — no TodoMiddleware
# ---------------------------------------------------------------------------
@patch("deerflow.agents.factory.create_agent")
def test_plan_mode_default_no_todo(mock_create_agent):
    mock_create_agent.return_value = MagicMock()
    create_deerflow_agent(_make_mock_model(), features=RuntimeFeatures(sandbox=False))

    call_kwargs = mock_create_agent.call_args[1]
    mw_types = [type(m).__name__ for m in call_kwargs["middleware"]]
    assert "TodoMiddleware" not in mw_types


# ---------------------------------------------------------------------------
# 33. summarization=True without model → ValueError
# ---------------------------------------------------------------------------
def test_summarization_true_raises():
    with pytest.raises(ValueError, match="requires a custom AgentMiddleware"):
        create_deerflow_agent(
            _make_mock_model(),
            features=RuntimeFeatures(sandbox=False, summarization=True),
        )


# ---------------------------------------------------------------------------
# 34. guardrail=True without built-in → ValueError
# ---------------------------------------------------------------------------
def test_guardrail_true_raises():
    with pytest.raises(ValueError, match="requires a custom AgentMiddleware"):
        create_deerflow_agent(
            _make_mock_model(),
            features=RuntimeFeatures(sandbox=False, guardrail=True),
        )


# ---------------------------------------------------------------------------
# 34. guardrail with custom AgentMiddleware replaces default
# ---------------------------------------------------------------------------
@patch("deerflow.agents.factory.create_agent")
def test_guardrail_custom_middleware(mock_create_agent):
    from langchain.agents.middleware import AgentMiddleware as AM

    mock_create_agent.return_value = MagicMock()

    class MyGuardrail(AM):
        pass

    custom = MyGuardrail()
    create_deerflow_agent(
        _make_mock_model(),
        features=RuntimeFeatures(sandbox=False, guardrail=custom),
    )

    call_kwargs = mock_create_agent.call_args[1]
    middleware = call_kwargs["middleware"]
    assert custom in middleware
    mw_types = [type(m).__name__ for m in middleware]
    assert "GuardrailMiddleware" not in mw_types


# ---------------------------------------------------------------------------
# 35. guardrail=False (default) — no GuardrailMiddleware
# ---------------------------------------------------------------------------
@patch("deerflow.agents.factory.create_agent")
def test_guardrail_default_off(mock_create_agent):
    mock_create_agent.return_value = MagicMock()
    create_deerflow_agent(_make_mock_model(), features=RuntimeFeatures(sandbox=False))

    call_kwargs = mock_create_agent.call_args[1]
    mw_types = [type(m).__name__ for m in call_kwargs["middleware"]]
    assert "GuardrailMiddleware" not in mw_types


# ---------------------------------------------------------------------------
# 36. Full chain order matches make_lead_agent (all features on)
# ---------------------------------------------------------------------------
@patch("deerflow.agents.factory.create_agent")
def test_full_chain_order(mock_create_agent):
    from langchain.agents.middleware import AgentMiddleware as AM

    mock_create_agent.return_value = MagicMock()

    class MyGuardrail(AM):
        pass

    class MySummarization(AM):
        pass

    class MyMemory(AM):
        pass

    feat = RuntimeFeatures(
        sandbox=True,
        memory=MyMemory(),
        summarization=MySummarization(),
        subagent=True,
        vision=True,
        auto_title=True,
        guardrail=MyGuardrail(),
    )
    create_deerflow_agent(_make_mock_model(), features=feat, plan_mode=True)

    call_kwargs = mock_create_agent.call_args[1]
    mw_types = [type(m).__name__ for m in call_kwargs["middleware"]]

    expected_order = [
        "ThreadDataMiddleware",
        "UploadsMiddleware",
        "SandboxMiddleware",
        "DanglingToolCallMiddleware",
        "MyGuardrail",
        "ToolErrorHandlingMiddleware",
        "MySummarization",
        "TodoMiddleware",
        "TitleMiddleware",
        "MyMemory",
        "ViewImageMiddleware",
        "ToolCallControl",
        "SubagentLimitMiddleware",
        "ClarificationMiddleware",
    ]
    assert mw_types == expected_order


# ---------------------------------------------------------------------------
# 37. @Next(ClarificationMiddleware) does not break tail invariant
# ---------------------------------------------------------------------------
@patch("deerflow.agents.factory.create_agent")
def test_next_clarification_preserves_tail_invariant(mock_create_agent):
    """Even with @Next(ClarificationMiddleware), Clarification stays last."""
    from langchain.agents.middleware import AgentMiddleware

    from deerflow.agents.middlewares.clarification_middleware import ClarificationMiddleware

    mock_create_agent.return_value = MagicMock()

    @Next(ClarificationMiddleware)
    class AfterClar(AgentMiddleware):
        pass

    create_deerflow_agent(
        _make_mock_model(),
        features=RuntimeFeatures(sandbox=False),
        extra_middleware=[AfterClar()],
    )

    call_kwargs = mock_create_agent.call_args[1]
    middleware = call_kwargs["middleware"]
    mw_types = [type(m).__name__ for m in middleware]
    assert mw_types[-1] == "ClarificationMiddleware"
    assert "AfterClar" in mw_types


# ---------------------------------------------------------------------------
# 38. @Next(X) + @Prev(X) on same anchor from different extras → ValueError
# ---------------------------------------------------------------------------
def test_extra_opposite_direction_same_anchor_conflict():
    from langchain.agents.middleware import AgentMiddleware

    from deerflow.agents.middlewares.dangling_tool_call_middleware import DanglingToolCallMiddleware

    @Next(DanglingToolCallMiddleware)
    class AfterDangling(AgentMiddleware):
        pass

    @Prev(DanglingToolCallMiddleware)
    class BeforeDangling(AgentMiddleware):
        pass

    with pytest.raises(ValueError, match="cross-anchoring"):
        create_deerflow_agent(
            _make_mock_model(),
            features=RuntimeFeatures(sandbox=False),
            extra_middleware=[AfterDangling(), BeforeDangling()],
        )


# ===========================================================================
# Input validation and error message hardening
# ===========================================================================


# ---------------------------------------------------------------------------
# 39. @Next with non-AgentMiddleware anchor → TypeError
# ---------------------------------------------------------------------------
def test_next_bad_anchor_type():
    with pytest.raises(TypeError, match="AgentMiddleware subclass"):

        @Next(str)  # type: ignore[arg-type]
        class MW:
            pass


# ---------------------------------------------------------------------------
# 40. @Prev with non-AgentMiddleware anchor → TypeError
# ---------------------------------------------------------------------------
def test_prev_bad_anchor_type():
    with pytest.raises(TypeError, match="AgentMiddleware subclass"):

        @Prev(42)  # type: ignore[arg-type]
        class MW:
            pass


# ---------------------------------------------------------------------------
# 41. extra_middleware with non-AgentMiddleware item → TypeError
# ---------------------------------------------------------------------------
def test_extra_middleware_bad_type():
    with pytest.raises(TypeError, match="AgentMiddleware instances"):
        create_deerflow_agent(
            _make_mock_model(),
            features=RuntimeFeatures(sandbox=False),
            extra_middleware=[object()],  # type: ignore[list-item]
        )


# ---------------------------------------------------------------------------
# 42. Circular dependency among extras → clear error message
# ---------------------------------------------------------------------------
def test_extra_circular_dependency():
    from langchain.agents.middleware import AgentMiddleware

    class MW_A(AgentMiddleware):
        pass

    class MW_B(AgentMiddleware):
        pass

    MW_A._next_anchor = MW_B  # type: ignore[attr-defined]
    MW_B._next_anchor = MW_A  # type: ignore[attr-defined]

    with pytest.raises(ValueError, match="Circular dependency"):
        create_deerflow_agent(
            _make_mock_model(),
            features=RuntimeFeatures(sandbox=False),
            extra_middleware=[MW_A(), MW_B()],
        )
