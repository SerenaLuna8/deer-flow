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
    FixedToolCallControlScope,
    GraphToolCallControlTopology,
    RepeatedCallPolicy,
    ResolvedGraphToolCallControlProfile,
    ResolvedToolCallControlPolicy,
    ToolCallBudgetObservation,
    ToolCallControlObservation,
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


def _profile(
    *,
    accounting_mode: str,
    lead_limit: int = 2,
    subagent_limit: int = 2,
) -> ResolvedGraphToolCallControlProfile:
    repeated_calls = RepeatedCallPolicy(
        enabled=False,
        warn_threshold=1,
        hard_limit=2,
        window_size=2,
    )
    lead = ResolvedToolCallControlPolicy(
        repeated_calls=repeated_calls,
        internal_tool_call_limit=lead_limit,
    )
    subagent = ResolvedToolCallControlPolicy(
        repeated_calls=repeated_calls,
        internal_tool_call_limit=subagent_limit,
    )
    return ResolvedGraphToolCallControlProfile(
        workload_profile="interactive",
        accounting_mode=accounting_mode,  # type: ignore[arg-type]
        lead=lead,
        subagent=subagent,
    )


def _feature_snapshot() -> SdkFeatureSnapshot:
    return SdkFeatureSnapshot(
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


def _delegated_context() -> DelegatedRuntimeContextProjection:
    return DelegatedRuntimeContextProjection(
        _carrier=RuntimeContextCarrier(
            is_subagent=True,
            run_id="parent-run-id",
        ),
        channel_identity_mode="absent",
        agent_prompt_bundle=None,
        runtime_skills=(),
    )


@pytest.mark.asyncio
async def test_parallel_tasks_each_receive_their_own_subagent_task_limit() -> None:
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

    topology = GraphToolCallControlTopology(
        profile=_profile(
            accounting_mode="lead_run_subagent_task",
            lead_limit=3,
            subagent_limit=2,
        ),
        lead_scope=FixedToolCallControlScope("parent-run-id"),
    )

    def runner_factory() -> _SubagentGraphRunner:
        return _SubagentGraphRunner(
            config=SubagentConfig(
                name="general-purpose",
                description="delegated scope acceptance",
                model="inherit",
            ),
            tools=[lookup],
            delegated_context=_delegated_context(),
            model_override=_ToolBindingFakeModel(
                messages=iter(
                    [
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "lookup",
                                    "args": {"value": f"delegated-{index}"},
                                    "id": f"lookup-{index}",
                                }
                                for index in range(2)
                            ],
                        ),
                        AIMessage(content="delegated task complete"),
                    ]
                )
            ),
            sdk_feature_snapshot=_feature_snapshot(),
            tool_search_enabled=False,
            tool_call_control_topology=topology,
            tool_call_control_observer=Observer(),
        )

    binding = SubagentExecutionBinding(
        runner_factory=runner_factory,
        quiescence_policy=(SubagentQuiescencePolicy.REQUIRED_BEFORE_RETURN),
        inherited_operations_barrier=NO_INHERITED_OPERATIONS,
    )
    lifecycle = SubagentTaskLifecycle(
        _scheduler=_ProcessSubagentScheduler(max_concurrency=2),
    )
    try:
        outcomes = await asyncio.gather(
            *(
                lifecycle.run(
                    SubagentTaskCall(
                        task_id=_PUBLIC_TASK_TOOL_CALL_ID,
                        prompt="Do two delegated lookups",
                        queue_timeout_seconds=5,
                        execution_timeout_seconds=5,
                        quiescence_timeout_seconds=1,
                    ),
                    binding,
                )
                for _ in range(2)
            )
        )
    finally:
        await lifecycle.aclose()

    assert all(isinstance(outcome, SubagentCompleted) for outcome in outcomes)
    execution_scopes = {str(outcome.execution_id) for outcome in outcomes}
    assert len(execution_scopes) == 2
    assert sorted(tool_calls) == [
        "delegated-0",
        "delegated-0",
        "delegated-1",
        "delegated-1",
    ]
    budget_observations = [observation for observation in observations if isinstance(observation, ToolCallBudgetObservation)]
    assert {observation.scope_id for observation in budget_observations} == execution_scopes
    assert all(observation.budget_scope == "subagent_task" for observation in budget_observations)
    assert all(observation.count_after == 2 for observation in budget_observations)
    assert all(observation.disposition == "exhaust_subject" for observation in budget_observations)


@pytest.mark.asyncio
async def test_legacy_shared_run_mode_keeps_one_parent_limit_across_parallel_tasks() -> None:
    policy = ResolvedToolCallControlPolicy(
        repeated_calls=RepeatedCallPolicy(
            enabled=False,
            warn_threshold=1,
            hard_limit=2,
            window_size=2,
        ),
        internal_tool_call_limit=2,
    )
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

    topology = GraphToolCallControlTopology(
        profile=ResolvedGraphToolCallControlProfile(
            workload_profile="interactive",
            accounting_mode="shared_run",
            lead=policy,
            subagent=policy,
        ),
        lead_scope=FixedToolCallControlScope("parent-run-id"),
    )

    def runner_factory() -> _SubagentGraphRunner:
        return _SubagentGraphRunner(
            config=SubagentConfig(
                name="general-purpose",
                description="delegated scope acceptance",
                model="inherit",
            ),
            tools=[lookup],
            delegated_context=_delegated_context(),
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
            sdk_feature_snapshot=_feature_snapshot(),
            tool_search_enabled=False,
            tool_call_control_topology=topology,
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
    budget_observations = [observation for observation in observations if isinstance(observation, ToolCallBudgetObservation)]
    assert {observation.scope_id for observation in budget_observations} <= execution_scopes
    assert len(budget_observations) == 2
    assert all(observation.count_after == 2 for observation in budget_observations)
    assert all(observation.budget_scope == "run" for observation in budget_observations)
    assert all(observation.disposition in {"exhaust_run", "truncate_tool_calls"} for observation in budget_observations)
    assert tool_calls == ["delegated"] * 2
