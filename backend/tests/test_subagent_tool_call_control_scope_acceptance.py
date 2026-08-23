"""Acceptance for lifecycle-owned Sub-Agent ToolCallControl scopes."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from deerflow.agents.middlewares.tool_call_control import (
    RepeatedCallPolicy,
    ResolvedGraphToolCallControlProfile,
    ResolvedToolCallBudgetPolicy,
    ResolvedToolCallControlPolicy,
    ToolCallBudgetObservation,
    ToolCallControlObservation,
    ToolCallLimit,
)
from deerflow.runtime.context_carrier import RuntimeContextCarrier
from deerflow.subagents.binding import SdkFeatureSnapshot
from deerflow.subagents.config import SubagentConfig
from deerflow.subagents.delegated_context import DelegatedRuntimeContextProjection
from deerflow.subagents.executor import _SubagentGraphRunner
from deerflow.subagents.lifecycle import (
    NO_INHERITED_OPERATIONS,
    SubagentCompleted,
    SubagentExecutionBinding,
    SubagentQuiescencePolicy,
    SubagentTaskCall,
    SubagentTaskLifecycle,
    _ProcessSubagentScheduler,
)

_PUBLIC_TASK_TOOL_CALL_ID = "reused-public-task-tool-call-id"


class _ToolBindingFakeModel(GenericFakeChatModel):
    def bind_tools(
        self,
        tools: Sequence[Any],
        **kwargs: Any,
    ) -> GenericFakeChatModel:
        del tools, kwargs
        return self


def _profile() -> ResolvedGraphToolCallControlProfile:
    policy = ResolvedToolCallControlPolicy(
        repeated_calls=RepeatedCallPolicy(
            enabled=False,
            warn_threshold=1,
            hard_limit=2,
            window_size=2,
        ),
        tool_budget=ResolvedToolCallBudgetPolicy(
            default=ToolCallLimit(
                warn_threshold=1,
                hard_limit=2,
            ),
            tools={},
        ),
    )
    return ResolvedGraphToolCallControlProfile(
        workload_profile="interactive",
        lead=policy,
        subagent=policy,
    )


@pytest.mark.asyncio
async def test_three_tasks_use_distinct_internal_scopes_not_the_public_tool_call_id() -> None:
    tool_calls: list[str] = []
    observations: list[ToolCallControlObservation] = []

    @tool
    def lookup(value: str) -> str:
        """Look up one value."""

        tool_calls.append(value)
        return value

    class Observer:
        def observe(self, observation: ToolCallControlObservation) -> None:
            observations.append(observation)

    delegated_context = DelegatedRuntimeContextProjection(
        _carrier=RuntimeContextCarrier(
            is_subagent=True,
            run_id="parent-run-id",
        ),
        channel_identity_mode="absent",
        agent_prompt_bundle=None,
        runtime_skills=(),
    )
    feature_snapshot = SdkFeatureSnapshot(
        sandbox=False,
        memory=False,
        summarization=False,
        subagent=False,
        vision=False,
        auto_title=False,
        guardrail=False,
        loop_detection=True,
        token_budget=False,
    )

    def runner_factory() -> _SubagentGraphRunner:
        return _SubagentGraphRunner(
            config=SubagentConfig(
                name="general-purpose",
                description="delegated scope acceptance",
                model="inherit",
            ),
            tools=[lookup],
            delegated_context=delegated_context,
            model_override=_ToolBindingFakeModel(
                messages=iter(
                    [
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "lookup",
                                    "args": {"value": "delegated"},
                                    "id": _PUBLIC_TASK_TOOL_CALL_ID,
                                }
                            ],
                        ),
                        AIMessage(content="delegated task complete"),
                    ]
                )
            ),
            sdk_feature_snapshot=feature_snapshot,
            tool_search_enabled=False,
            tool_call_control_profile=_profile(),
            tool_call_control_observer=Observer(),
        )

    binding = SubagentExecutionBinding(
        runner_factory=runner_factory,
        quiescence_policy=(SubagentQuiescencePolicy.REQUIRED_BEFORE_RETURN),
        inherited_operations_barrier=NO_INHERITED_OPERATIONS,
    )
    lifecycle = SubagentTaskLifecycle(
        _scheduler=_ProcessSubagentScheduler(max_concurrency=3),
    )
    try:
        outcomes = await asyncio.gather(
            *(
                lifecycle.run(
                    SubagentTaskCall(
                        task_id=_PUBLIC_TASK_TOOL_CALL_ID,
                        prompt="Do one delegated lookup",
                        queue_timeout_seconds=5,
                        execution_timeout_seconds=5,
                        quiescence_timeout_seconds=1,
                    ),
                    binding,
                )
                for _ in range(3)
            )
        )
    finally:
        await lifecycle.aclose()

    assert all(isinstance(outcome, SubagentCompleted) for outcome in outcomes)
    assert {outcome.task_id for outcome in outcomes} == {_PUBLIC_TASK_TOOL_CALL_ID}
    execution_scopes = {str(outcome.execution_id) for outcome in outcomes}
    assert len(execution_scopes) == 3
    assert _PUBLIC_TASK_TOOL_CALL_ID not in execution_scopes
    assert {observation.scope_id for observation in observations if isinstance(observation, ToolCallBudgetObservation) and observation.reason_code == "tool_budget_warning"} == execution_scopes
    assert tool_calls == ["delegated"] * 3
