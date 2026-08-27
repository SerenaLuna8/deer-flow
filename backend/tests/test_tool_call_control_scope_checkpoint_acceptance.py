"""Focused acceptance for ToolCallControl scope and checkpoint ownership."""

from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.runtime import Runtime

import deerflow.runtime.runs.worker as run_worker
from deerflow.agents.middlewares.tool_call_control import (
    TOOL_CALL_CONTROL_RECEIPT_KEY,
    TOOL_CALL_CONTROL_STATE_KEY,
    FixedToolCallControlScope,
    RepeatedCallPolicy,
    ResolvedToolCallControlPolicy,
    ToolCallBudgetObservation,
    ToolCallControlBinding,
    build_tool_call_control,
)
from deerflow.agents.thread_state import (
    get_thread_state_schema,
    normalize_middleware_state_schemas,
)
from deerflow.config.database_config import CheckpointChannelMode
from deerflow.runtime.checkpoint_mode import (
    CHECKPOINT_MODE_METADATA_KEY,
    inject_checkpoint_mode,
)
from deerflow.runtime.runs.manager import RunManager
from deerflow.runtime.runs.worker import RunContext, run_agent


class _ToolBindingFakeModel(GenericFakeChatModel):
    def bind_tools(
        self,
        tools: Sequence[Any],
        **kwargs: Any,
    ) -> GenericFakeChatModel:
        del tools, kwargs
        return self


def _control_policy(*, hard_limit: int = 2) -> ResolvedToolCallControlPolicy:
    return ResolvedToolCallControlPolicy(
        repeated_calls=RepeatedCallPolicy(
            enabled=False,
            warn_threshold=1,
            hard_limit=2,
            window_size=2,
        ),
        internal_tool_call_limit=hard_limit,
    )


def test_legacy_shared_run_checkpoint_fingerprint_remains_replayable() -> None:
    control = build_tool_call_control(
        _control_policy(),
        ToolCallControlBinding(
            role="lead",
            scope=FixedToolCallControlScope("legacy-run"),
        ),
    )
    initialized = control.before_agent({}, Runtime(context={}))

    assert initialized is not None
    facts = initialized[TOOL_CALL_CONTROL_STATE_KEY]
    assert facts["contract_fingerprint"] == ("669f7e7d029689d80ea9ce9dcd6818c70de72706dda0a1ba750c5e658dd97931")

    replay = control.after_model(
        {
            TOOL_CALL_CONTROL_STATE_KEY: facts,
            "messages": [
                AIMessage(
                    id="legacy-checkpoint-proposal",
                    content="",
                    tool_calls=[
                        {
                            "name": "web_search",
                            "args": {"query": "legacy"},
                            "id": "legacy-call",
                        }
                    ],
                )
            ],
        },
        Runtime(context={}),
    )

    assert replay is not None
    assert replay[TOOL_CALL_CONTROL_STATE_KEY]["admitted_count"] == 1
    assert replay["messages"][0].tool_calls[0]["id"] == "legacy-call"


@pytest.mark.parametrize("mode", ["full", "delta"])
def test_materialized_checkpoint_replay_preserves_controlled_batch_and_hard_limit(
    mode: CheckpointChannelMode,
) -> None:
    calls: list[str] = []

    @tool
    def web_search(query: str) -> str:
        """Search one query."""

        calls.append(query)
        return f"result:{query}"

    control = build_tool_call_control(
        _control_policy(),
        ToolCallControlBinding(
            role="lead",
            scope=FixedToolCallControlScope("run-checkpoint"),
        ),
    )
    boundary = AIMessage(
        id="boundary-proposal",
        content="",
        tool_calls=[
            {
                "name": "web_search",
                "args": {"query": f"query-{index}"},
                "id": f"call-{index}",
            }
            for index in range(3)
        ],
    )
    saver = InMemorySaver()
    graph = create_agent(
        model=_ToolBindingFakeModel(
            messages=iter(
                [
                    boundary,
                    AIMessage(id="final", content="done"),
                ]
            )
        ),
        tools=[web_search],
        middleware=normalize_middleware_state_schemas(
            [control],
            mode,
            2,
        ),
        state_schema=get_thread_state_schema(mode, 2),
        checkpointer=saver,
    )
    config: dict[str, Any] = {
        "configurable": {
            "thread_id": f"checkpoint-{mode}",
            "checkpoint_ns": "",
        }
    }
    inject_checkpoint_mode(config, mode)

    graph.invoke(
        {"messages": [HumanMessage(content="research")]},
        config,
    )

    controlled_snapshot = next(
        snapshot
        for snapshot in graph.get_state_history(config)
        if snapshot.values.get(TOOL_CALL_CONTROL_STATE_KEY, {}).get("admitted_count") == 2 and snapshot.values.get("messages") and isinstance(snapshot.values["messages"][-1], AIMessage) and snapshot.values["messages"][-1].id == boundary.id
    )
    materialized = graph.get_state(controlled_snapshot.config)
    facts = materialized.values[TOOL_CALL_CONTROL_STATE_KEY]
    controlled_message = materialized.values["messages"][-1]

    assert facts["admitted_count"] == 2
    assert facts["limit_exhausted"] is True
    assert [call["id"] for call in controlled_message.tool_calls] == [
        "call-0",
        "call-1",
    ]
    assert controlled_message.additional_kwargs[TOOL_CALL_CONTROL_RECEIPT_KEY]
    assert calls == ["query-0", "query-1"]
    assert (materialized.metadata.get(CHECKPOINT_MODE_METADATA_KEY) == "delta") is (mode == "delta")

    replay = control.after_model(
        dict(materialized.values),
        Runtime(context={}),
    )

    assert replay is not None
    assert replay[TOOL_CALL_CONTROL_STATE_KEY]["admitted_count"] == 2
    assert replay[TOOL_CALL_CONTROL_STATE_KEY]["limit_exhausted"] is True
    assert [call["id"] for call in replay["messages"][0].tool_calls] == [
        "call-0",
        "call-1",
    ]

    rejected = control.after_model(
        {
            **materialized.values,
            "messages": [
                *materialized.values["messages"],
                AIMessage(
                    id="after-hard-limit",
                    content="",
                    tool_calls=[
                        {
                            "name": "web_search",
                            "args": {"query": "must-not-run"},
                            "id": "call-0",
                        }
                    ],
                ),
            ],
        },
        Runtime(context={}),
    )

    assert rejected is not None
    assert rejected[TOOL_CALL_CONTROL_STATE_KEY]["admitted_count"] == 2
    assert rejected[TOOL_CALL_CONTROL_STATE_KEY]["limit_exhausted"] is True
    assert rejected["messages"][0].tool_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["full", "delta"])
async def test_private_run_scope_spans_hidden_goal_turns_but_resets_for_a_new_run(
    mode: CheckpointChannelMode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread_id = f"goal-scope-{mode}"
    saver = InMemorySaver()
    manager = RunManager()
    first_record = await manager.create(thread_id)
    first_calls: list[str] = []
    first_observations: list[object] = []

    @tool
    def first_search(query: str) -> str:
        """Search during the first Run."""

        first_calls.append(query)
        return f"result:{query}"

    class Observer:
        def __init__(self, target: list[object]) -> None:
            self._target = target

        def observe(self, observation: object) -> None:
            self._target.append(observation)

    def build_graph(
        *,
        run_id: str,
        responses: list[AIMessage],
        search_tool: Any,
        observations: list[object],
    ) -> Any:
        control = build_tool_call_control(
            _control_policy(),
            ToolCallControlBinding(
                role="lead",
                scope=FixedToolCallControlScope(run_id),
                observer=Observer(observations),
            ),
        )
        return create_agent(
            model=_ToolBindingFakeModel(messages=iter(responses)),
            tools=[search_tool],
            middleware=normalize_middleware_state_schemas(
                [control],
                mode,
                2,
            ),
            state_schema=get_thread_state_schema(mode, 2),
            checkpointer=saver,
        )

    first_graph = build_graph(
        run_id=first_record.run_id,
        search_tool=first_search,
        observations=first_observations,
        responses=[
            AIMessage(
                id="initial-proposal",
                content="",
                tool_calls=[
                    {
                        "name": "first_search",
                        "args": {"query": "initial"},
                        "id": "initial-call",
                    }
                ],
            ),
            AIMessage(id="initial-final", content="initial turn complete"),
            AIMessage(
                id="goal-continuation-proposal",
                content="",
                tool_calls=[
                    {
                        "name": "first_search",
                        "args": {"query": "continuation-admitted"},
                        "id": "continuation-call-1",
                    },
                    {
                        "name": "first_search",
                        "args": {"query": "continuation-rejected"},
                        "id": "continuation-call-2",
                    },
                ],
            ),
            AIMessage(
                id="goal-continuation-final",
                content="hidden continuation complete",
            ),
        ],
    )
    continuation_calls = 0

    async def continue_once(**_kwargs: object) -> dict[str, object] | None:
        nonlocal continuation_calls
        continuation_calls += 1
        if continuation_calls == 1:
            return {"messages": [HumanMessage(content="hidden Goal Continuation Graph Turn")]}
        return None

    monkeypatch.setattr(
        run_worker,
        "_prepare_goal_continuation_input",
        continue_once,
    )
    app_config = SimpleNamespace(
        database=SimpleNamespace(
            checkpoint_channel_mode=mode,
            checkpoint_delta=SimpleNamespace(snapshot_frequency=2),
        )
    )
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )

    first_outcome = await run_agent(
        bridge,
        manager,
        first_record,
        ctx=RunContext(
            app_config=app_config,
            checkpointer=saver,
        ),
        agent_factory=lambda **_kwargs: first_graph,
        graph_input={"messages": [HumanMessage(content="initial Run turn")]},
        config={},
    )

    first_config: dict[str, Any] = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "",
        }
    }
    inject_checkpoint_mode(first_config, mode)
    first_facts = first_graph.get_state(first_config).values[TOOL_CALL_CONTROL_STATE_KEY]
    first_exhaustion = next(observation for observation in first_observations if isinstance(observation, ToolCallBudgetObservation) and observation.reason_code == "tool_budget_exhausted")

    assert first_outcome.status == "succeeded"
    assert continuation_calls == 2
    assert first_calls == ["initial", "continuation-admitted"]
    assert first_facts["scope_id"] == first_record.run_id
    assert first_facts["admitted_count"] == 2
    assert first_facts["limit_exhausted"] is True
    assert (
        first_exhaustion.count_before,
        first_exhaustion.proposed,
        first_exhaustion.admitted,
        first_exhaustion.rejected,
        first_exhaustion.count_after,
    ) == (1, 2, 1, 1, 2)

    second_record = await manager.create(thread_id)
    second_calls: list[str] = []
    second_observations: list[object] = []

    @tool
    def second_search(query: str) -> str:
        """Search during the second Run."""

        second_calls.append(query)
        return f"result:{query}"

    second_graph = build_graph(
        run_id=second_record.run_id,
        search_tool=second_search,
        observations=second_observations,
        responses=[
            AIMessage(
                id="new-run-proposal",
                content="",
                tool_calls=[
                    {
                        "name": "second_search",
                        "args": {"query": "new-run-1"},
                        "id": "new-run-call-1",
                    },
                    {
                        "name": "second_search",
                        "args": {"query": "new-run-2"},
                        "id": "new-run-call-2",
                    },
                ],
            ),
            AIMessage(id="new-run-final", content="new Run complete"),
        ],
    )

    async def no_continuation(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        run_worker,
        "_prepare_goal_continuation_input",
        no_continuation,
    )
    second_outcome = await run_agent(
        bridge,
        manager,
        second_record,
        ctx=RunContext(
            app_config=app_config,
            checkpointer=saver,
        ),
        agent_factory=lambda **_kwargs: second_graph,
        graph_input={"messages": [HumanMessage(content="new Run, same Thread")]},
        config={},
    )
    second_facts = second_graph.get_state(first_config).values[TOOL_CALL_CONTROL_STATE_KEY]
    second_exhaustion = next(observation for observation in second_observations if isinstance(observation, ToolCallBudgetObservation) and observation.reason_code == "tool_budget_exhausted")

    assert second_outcome.status == "succeeded"
    assert second_calls == ["new-run-1", "new-run-2"]
    assert second_facts["scope_id"] == second_record.run_id
    assert second_facts["admitted_count"] == 2
    assert second_facts["limit_exhausted"] is True
    assert (
        second_exhaustion.count_before,
        second_exhaustion.proposed,
        second_exhaustion.admitted,
        second_exhaustion.rejected,
        second_exhaustion.count_after,
    ) == (0, 2, 2, 0, 2)
