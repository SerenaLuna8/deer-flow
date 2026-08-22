"""SDK-specific Sub-Agent graph runner inputs remain config-free."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from langchain.agents.middleware import AgentMiddleware

from deerflow.agents.factory import create_deerflow_agent
from deerflow.agents.features import RuntimeFeatures
from deerflow.runtime.context_carrier import RuntimeContextCarrier
from deerflow.subagents.binding import (
    ParentExecutionBindingFactory,
    SdkFeatureSnapshot,
    SdkParentExecutionProfile,
)
from deerflow.subagents.config import SubagentConfig
from deerflow.subagents.delegated_context import DelegatedRuntimeContextProjection
from deerflow.subagents.executor import _SubagentGraphRunner


def _config() -> SubagentConfig:
    return SubagentConfig(
        name="general-purpose",
        description="delegated work",
        model="inherit",
    )


def _delegated_context() -> DelegatedRuntimeContextProjection:
    return DelegatedRuntimeContextProjection(
        _carrier=RuntimeContextCarrier(is_subagent=True),
        channel_identity_mode="absent",
        agent_prompt_bundle=None,
        runtime_skills=(),
    )


def test_sdk_full_takeover_uses_exact_model_and_middleware_without_global_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.subagents import executor as executor_module

    model = object()
    middleware = object()
    captured: dict[str, object] = {}

    def fake_create_agent(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(executor_module, "create_agent", fake_create_agent)
    monkeypatch.setattr(
        executor_module,
        "get_app_config",
        lambda: (_ for _ in ()).throw(AssertionError("global config was read")),
    )
    runner = _SubagentGraphRunner(
        config=_config(),
        tools=[],
        delegated_context=_delegated_context(),
        model_override=model,
        middleware_override=(middleware,),
        tool_search_enabled=False,
    )

    runner._create_agent([])

    assert captured["model"] is model
    assert captured["middleware"] == [middleware]
    assert runner.app_config is None


def test_sdk_feature_profile_preserves_explicit_extra_middleware_once_for_delegation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.agents import factory as factory_module
    from deerflow.subagents import executor as executor_module

    class DelegatedMarkerMiddleware(AgentMiddleware):
        pass

    model = MagicMock(name="sdk-model")
    marker = DelegatedMarkerMiddleware()
    parent_graph_inputs: dict[str, object] = {}

    def capture_parent_graph(**kwargs):
        parent_graph_inputs.update(kwargs)
        return object()

    monkeypatch.setattr(factory_module, "create_agent", capture_parent_graph)
    create_deerflow_agent(
        model,
        features=RuntimeFeatures(subagent=True, sandbox=False),
        extra_middleware=[marker],
    )

    bound_task = next(
        tool
        for tool in parent_graph_inputs["tools"]  # type: ignore[union-attr]
        if tool.name == "task"
    )
    binding_factory = next(cell.cell_contents for cell in bound_task.coroutine.__closure__ or () if type(cell.cell_contents) is ParentExecutionBindingFactory)
    profile = binding_factory.profile
    assert type(profile) is SdkParentExecutionProfile
    assert profile.features is not None

    delegated_graph_inputs: dict[str, object] = {}

    def capture_delegated_graph(**kwargs):
        delegated_graph_inputs.update(kwargs)
        return object()

    monkeypatch.setattr(executor_module, "create_agent", capture_delegated_graph)
    runner = _SubagentGraphRunner(
        config=_config(),
        tools=[],
        delegated_context=_delegated_context(),
        model_override=profile.graph.model,
        sdk_feature_snapshot=profile.features,
        tool_search_enabled=False,
    )

    runner._create_agent([])

    assert (
        sum(
            middleware is marker
            for middleware in delegated_graph_inputs["middleware"]  # type: ignore[union-attr]
        )
        == 1
    )


@pytest.mark.asyncio
async def test_sdk_feature_profile_builds_state_without_global_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.subagents import executor as executor_module

    monkeypatch.setattr(
        executor_module,
        "get_app_config",
        lambda: (_ for _ in ()).throw(AssertionError("global config was read")),
    )
    runner = _SubagentGraphRunner(
        config=_config(),
        tools=[],
        delegated_context=_delegated_context(),
        model_override=MagicMock(name="sdk-model"),
        sdk_feature_snapshot=SdkFeatureSnapshot(
            sandbox=False,
            memory=False,
            summarization=False,
            subagent=True,
            vision=False,
            auto_title=True,
            guardrail=False,
            loop_detection=False,
            token_budget=False,
        ),
        tool_search_enabled=False,
    )

    state, tools, deferred = await runner._build_initial_state("inspect")

    assert state["messages"][-1].content == "inspect"
    assert tools == []
    assert deferred.deferred_names == frozenset()
