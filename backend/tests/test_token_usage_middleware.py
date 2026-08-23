"""Durable Sub-Agent Task usage attribution at the Agent Graph message seam."""

from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END
from langgraph.graph.message import add_messages
from langgraph.types import Command, Overwrite

from deerflow.agents.middlewares.host_execution_batch_barrier_middleware import (
    HostExecutionApprovalPauseMiddleware,
)
from deerflow.agents.middlewares.token_budget_middleware import (
    TOKEN_BUDGET_USAGE_STATE_KEY,
    TokenBudgetMiddleware,
)
from deerflow.agents.middlewares.token_usage_middleware import TokenUsageMiddleware
from deerflow.agents.thread_state import (
    get_thread_state_schema,
    normalize_middleware_state_schemas,
)
from deerflow.config.token_budget_config import TokenBudgetConfig
from deerflow.runtime.checkpoint_mode import inject_checkpoint_mode
from deerflow.runtime.checkpoint_state import (
    CheckpointStateAccessor,
    build_state_mutation_graph,
)
from deerflow.runtime.serialization import (
    serialize,
    serialize_channel_values_for_api,
)


def _task_call(tool_call_id: str) -> dict[str, object]:
    return {
        "name": "task",
        "args": {
            "description": "inspect the report",
            "prompt": "inspect the report",
            "subagent_type": "general-purpose",
        },
        "id": tool_call_id,
        "type": "tool_call",
    }


def _budget(*, max_tokens: int) -> TokenBudgetMiddleware:
    return TokenBudgetMiddleware.from_config(
        TokenBudgetConfig(
            enabled=True,
            max_tokens=max_tokens,
            warn_threshold=0.8,
            hard_stop_threshold=1.0,
        )
    )


def _runtime(run_id: str = "run-receipts") -> SimpleNamespace:
    return SimpleNamespace(context={"run_id": run_id})


def _assert_budget_checkpoint_only(update: dict | None) -> None:
    assert update is not None
    assert set(update) == {TOKEN_BUDGET_USAGE_STATE_KEY}


def test_token_budget_private_checkpoint_is_excluded_from_public_serialization() -> None:
    private_usage = {
        "version": 1,
        "run_id": "private-budget-run",
        "input_tokens": 87651,
        "output_tokens": 87652,
        "total_tokens": 175303,
    }
    payload = {
        TOKEN_BUDGET_USAGE_STATE_KEY: private_usage,
        "messages": [HumanMessage(content="visible")],
        "business_state": {"usage": "business instructions"},
    }

    values = serialize(payload, mode="values")
    updates = serialize({"checkpoint": payload}, mode="updates")
    rest = serialize_channel_values_for_api(payload)

    assert TOKEN_BUDGET_USAGE_STATE_KEY not in values
    assert TOKEN_BUDGET_USAGE_STATE_KEY not in updates["checkpoint"]
    assert TOKEN_BUDGET_USAGE_STATE_KEY not in rest
    assert values["business_state"] == {"usage": "business instructions"}
    assert payload[TOKEN_BUDGET_USAGE_STATE_KEY] is private_usage


def test_receipt_wire_adds_usage_and_persists_its_contribution() -> None:
    dispatch = AIMessage(
        id="dispatch-1",
        content="",
        tool_calls=[_task_call("shared-call")],
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": 4,
            "total_tokens": 14,
        },
    )
    result = ToolMessage(
        id="result-1",
        content="Task Succeeded. Result: inspected",
        tool_call_id="shared-call",
        additional_kwargs={
            "subagent_token_usage": {
                "input_tokens": 3,
                "output_tokens": 5,
                "total_tokens": 8,
            },
            "subagent_usage_receipt_id": "receipt-1",
        },
    )
    next_response = AIMessage(
        id="response-1",
        content="Finished.",
        usage_metadata={
            "input_tokens": 7,
            "output_tokens": 2,
            "total_tokens": 9,
        },
    )

    update = TokenUsageMiddleware().after_model(
        {"messages": [dispatch, result, next_response]},
        SimpleNamespace(),
    )

    assert update is not None
    updated_dispatch = next(message for message in update["messages"] if isinstance(message, AIMessage) and message.id == "dispatch-1")
    assert updated_dispatch.usage_metadata == {
        "input_tokens": 13,
        "output_tokens": 9,
        "total_tokens": 22,
    }
    assert updated_dispatch.additional_kwargs["subagent_usage_receipt_state"] == {
        "version": 1,
        "baseline": {
            "input_tokens": 10,
            "output_tokens": 4,
            "total_tokens": 14,
        },
        "contributions": [
            {
                "receipt_id": "receipt-1",
                "usage": {
                    "input_tokens": 3,
                    "output_tokens": 5,
                    "total_tokens": 8,
                },
            }
        ],
    }


def test_receipt_reducer_preserves_provider_usage_detail_fields() -> None:
    dispatch = AIMessage(
        id="dispatch-details",
        content="",
        tool_calls=[_task_call("details-call")],
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": 4,
            "total_tokens": 14,
            "input_token_details": {"cache_read": 6},
            "output_token_details": {"reasoning": 2},
        },
    )
    result = ToolMessage(
        id="details-result",
        content="done",
        tool_call_id="details-call",
        additional_kwargs={
            "subagent_usage_receipt_id": "details-receipt",
            "subagent_token_usage": {
                "input_tokens": 3,
                "output_tokens": 5,
                "total_tokens": 8,
            },
        },
    )

    update = TokenUsageMiddleware().after_model(
        {
            "messages": [
                dispatch,
                result,
                AIMessage(id="details-final", content="finished"),
            ]
        },
        SimpleNamespace(),
    )

    assert update is not None
    updated_dispatch = next(message for message in update["messages"] if isinstance(message, AIMessage) and message.id == "dispatch-details")
    assert updated_dispatch.usage_metadata["input_token_details"] == {
        "cache_read": 6,
    }
    assert updated_dispatch.usage_metadata["output_token_details"] == {
        "reasoning": 2,
    }


def test_token_budget_counts_subagent_receipt_on_the_same_model_tick() -> None:
    dispatch = AIMessage(
        id="budget-dispatch",
        content="",
        tool_calls=[_task_call("budget-call")],
        usage_metadata={
            "input_tokens": 300,
            "output_tokens": 200,
            "total_tokens": 500,
        },
    )
    result = ToolMessage(
        id="budget-result",
        content="done",
        tool_call_id="budget-call",
        additional_kwargs={
            "subagent_usage_receipt_id": "budget-receipt",
            "subagent_token_usage": {
                "input_tokens": 300,
                "output_tokens": 300,
                "total_tokens": 600,
            },
        },
    )
    next_response = AIMessage(
        id="budget-next",
        content="",
        tool_calls=[
            {
                "name": "lookup",
                "args": {},
                "id": "next-call",
                "type": "tool_call",
            }
        ],
        usage_metadata={
            "input_tokens": 5,
            "output_tokens": 5,
            "total_tokens": 10,
        },
    )

    update = _budget(max_tokens=1_000).after_model(
        {"messages": [dispatch, result, next_response]},
        _runtime(),
    )

    assert update is not None
    stopped = update["messages"][0]
    assert isinstance(stopped, AIMessage)
    assert stopped.tool_calls == []


def test_token_budget_does_not_count_persisted_receipt_state_twice() -> None:
    dispatch = AIMessage(
        id="budget-dispatch",
        content="",
        tool_calls=[_task_call("budget-call")],
        usage_metadata={
            "input_tokens": 300,
            "output_tokens": 200,
            "total_tokens": 500,
        },
    )
    result = ToolMessage(
        id="budget-result",
        content="done",
        tool_call_id="budget-call",
        additional_kwargs={
            "subagent_usage_receipt_id": "budget-receipt",
            "subagent_token_usage": {
                "input_tokens": 300,
                "output_tokens": 300,
                "total_tokens": 600,
            },
        },
    )
    first_response = AIMessage(
        id="budget-first-response",
        content="continue",
        usage_metadata={
            "input_tokens": 5,
            "output_tokens": 5,
            "total_tokens": 10,
        },
    )
    runtime = _runtime("run-no-double-count")
    budget = _budget(max_tokens=1_500)
    messages = [dispatch, result, first_response]

    _assert_budget_checkpoint_only(
        budget.after_model({"messages": messages}, runtime),
    )
    usage_update = TokenUsageMiddleware().after_model(
        {"messages": messages},
        runtime,
    )
    assert usage_update is not None
    resumed = add_messages(messages, usage_update["messages"])
    resumed.append(
        AIMessage(
            id="budget-second-response",
            content="finished",
            usage_metadata={
                "input_tokens": 5,
                "output_tokens": 5,
                "total_tokens": 10,
            },
        )
    )

    _assert_budget_checkpoint_only(
        budget.after_model({"messages": resumed}, runtime),
    )
    cumulative = budget._cumulative_usage["run-no-double-count"]
    assert cumulative.total == 1_120


def test_token_budget_seeds_checkpoint_history_without_recounting_receipts() -> None:
    dispatch = AIMessage(
        id="historical-dispatch",
        content="",
        tool_calls=[_task_call("historical-call")],
        usage_metadata={
            "input_tokens": 600,
            "output_tokens": 500,
            "total_tokens": 1_100,
        },
        additional_kwargs={
            "subagent_usage_receipt_state": {
                "version": 1,
                "baseline": {
                    "input_tokens": 300,
                    "output_tokens": 200,
                    "total_tokens": 500,
                },
                "contributions": [
                    {
                        "receipt_id": "historical-receipt",
                        "usage": {
                            "input_tokens": 300,
                            "output_tokens": 300,
                            "total_tokens": 600,
                        },
                    }
                ],
            }
        },
    )
    result = ToolMessage(
        id="historical-result",
        content="done",
        tool_call_id="historical-call",
        additional_kwargs={
            "subagent_usage_receipt_id": "historical-receipt",
            "subagent_token_usage": {
                "input_tokens": 300,
                "output_tokens": 300,
                "total_tokens": 600,
            },
        },
    )
    runtime = _runtime("run-history")
    budget = _budget(max_tokens=1_000)
    budget.before_agent({"messages": [dispatch, result]}, runtime)
    next_response = AIMessage(
        id="new-response",
        content="finished",
        usage_metadata={
            "input_tokens": 5,
            "output_tokens": 5,
            "total_tokens": 10,
        },
    )

    _assert_budget_checkpoint_only(
        budget.after_model(
            {"messages": [dispatch, result, next_response]},
            runtime,
        ),
    )
    assert budget._cumulative_usage["run-history"].total == 10


def test_token_budget_restores_same_run_checkpoint_usage_after_attempt_recreation() -> None:
    runtime = _runtime("run-retried")
    prior_response = AIMessage(
        id="prior-attempt-response",
        content="partial",
        usage_metadata={
            "input_tokens": 450,
            "output_tokens": 450,
            "total_tokens": 900,
        },
    )
    prior_update = _budget(max_tokens=1_000).after_model(
        {"messages": [prior_response]},
        runtime,
    )

    assert prior_update is not None
    durable_usage = prior_update["token_budget_usage"]

    recreated = _budget(max_tokens=1_000)
    recreated.before_agent(
        {
            "messages": [prior_response],
            "token_budget_usage": durable_usage,
        },
        runtime,
    )
    current_response = AIMessage(
        id="retried-attempt-response",
        content="",
        tool_calls=[
            {
                "name": "lookup",
                "args": {},
                "id": "retried-attempt-call",
                "type": "tool_call",
            }
        ],
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 100,
            "total_tokens": 200,
        },
    )

    stopped = recreated.after_model(
        {"messages": [prior_response, current_response]},
        runtime,
    )

    assert stopped is not None
    assert stopped[TOKEN_BUDGET_USAGE_STATE_KEY]["total_tokens"] == 1_100
    assert stopped["messages"][0].tool_calls == []


def test_token_budget_does_not_inherit_checkpoint_usage_from_an_older_run() -> None:
    old_run_usage = {
        "version": 1,
        "run_id": "run-old",
        "input_tokens": 450,
        "output_tokens": 450,
        "total_tokens": 900,
    }
    current = _budget(max_tokens=1_000)
    runtime = _runtime("run-new")
    reset_update = current.before_agent(
        {"messages": [], "token_budget_usage": old_run_usage},
        runtime,
    )
    assert reset_update == {
        "token_budget_usage": {
            "version": 1,
            "run_id": "run-new",
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }
    }
    response = AIMessage(
        id="new-run-response",
        content="",
        tool_calls=[
            {
                "name": "lookup",
                "args": {},
                "id": "new-run-call",
                "type": "tool_call",
            }
        ],
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 100,
            "total_tokens": 200,
        },
    )

    update = current.after_model(
        {"messages": [response]},
        runtime,
    )

    assert update == {
        "token_budget_usage": {
            "version": 1,
            "run_id": "run-new",
            "input_tokens": 100,
            "output_tokens": 100,
            "total_tokens": 200,
        }
    }


def test_token_budget_checkpoints_terminal_subagent_usage_before_clearing_attempt_state() -> None:
    dispatch = AIMessage(
        id="terminal-budget-dispatch",
        content="",
        tool_calls=[_task_call("terminal-budget-call")],
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": 4,
            "total_tokens": 14,
        },
    )
    result = ToolMessage(
        id="terminal-budget-result",
        content="approval required",
        tool_call_id="terminal-budget-call",
        additional_kwargs={
            "subagent_usage_receipt_id": "terminal-budget-receipt",
            "subagent_token_usage": {
                "input_tokens": 3,
                "output_tokens": 5,
                "total_tokens": 8,
            },
        },
    )
    runtime = _runtime("run-terminal-budget")
    budget = _budget(max_tokens=1_000)
    model_update = budget.after_model(
        {"messages": [dispatch]},
        runtime,
    )
    assert model_update is not None

    terminal_update = budget.after_agent(
        {
            "messages": [dispatch, result],
            "token_budget_usage": model_update["token_budget_usage"],
        },
        runtime,
    )

    assert terminal_update == {
        "token_budget_usage": {
            "version": 1,
            "run_id": "run-terminal-budget",
            "input_tokens": 13,
            "output_tokens": 9,
            "total_tokens": 22,
        }
    }


@pytest.mark.parametrize("mode", ["full", "delta"])
def test_token_budget_private_checkpoint_survives_message_compaction_in_each_mode(
    mode: str,
) -> None:
    run_id = f"run-budget-{mode}"
    budget = _budget(max_tokens=1_000)
    checkpointer = InMemorySaver()
    graph = create_agent(
        model=GenericFakeChatModel(
            messages=iter(
                [
                    AIMessage(
                        id=f"prior-{mode}",
                        content="partial",
                        usage_metadata={
                            "input_tokens": 450,
                            "output_tokens": 450,
                            "total_tokens": 900,
                        },
                    )
                ]
            )
        ),
        tools=None,
        middleware=normalize_middleware_state_schemas(
            [budget],
            mode,  # type: ignore[arg-type]
            2,
        ),
        state_schema=get_thread_state_schema(
            mode,  # type: ignore[arg-type]
            2,
        ),
        context_schema=dict,
        checkpointer=checkpointer,
    )
    config: dict[str, Any] = {
        "configurable": {
            "thread_id": f"thread-budget-{mode}",
            "checkpoint_ns": "",
        }
    }
    inject_checkpoint_mode(config, mode)  # type: ignore[arg-type]

    public_result = graph.invoke(
        {"messages": [HumanMessage(content="start")]},
        config,
        context={"run_id": run_id},
    )
    assert TOKEN_BUDGET_USAGE_STATE_KEY not in public_result
    accessor = CheckpointStateAccessor.bind(
        graph,
        checkpointer,
        mode=mode,  # type: ignore[arg-type]
    )
    before_compaction = accessor.get(config)
    durable_usage = before_compaction.values[TOKEN_BUDGET_USAGE_STATE_KEY]
    assert durable_usage["total_tokens"] == 900

    compaction_accessor = CheckpointStateAccessor.bind(
        build_state_mutation_graph(
            "manual_compaction",
            mode,  # type: ignore[arg-type]
            snapshot_frequency=2,
        ),
        checkpointer,
        mode=mode,  # type: ignore[arg-type]
    )
    compaction_accessor.update(
        config,
        {"messages": Overwrite([])},
        as_node="manual_compaction",
    )
    after_compaction = accessor.get(config)

    assert after_compaction.values[TOKEN_BUDGET_USAGE_STATE_KEY] == durable_usage

    recreated = _budget(max_tokens=1_000)
    recreated.before_agent(dict(after_compaction.values), _runtime(run_id))
    current_response = AIMessage(
        id=f"current-{mode}",
        content="",
        tool_calls=[
            {
                "name": "lookup",
                "args": {},
                "id": f"current-call-{mode}",
                "type": "tool_call",
            }
        ],
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 100,
            "total_tokens": 200,
        },
    )
    stopped = recreated.after_model(
        {
            **dict(after_compaction.values),
            "messages": [current_response],
        },
        _runtime(run_id),
    )

    assert stopped is not None
    assert stopped[TOKEN_BUDGET_USAGE_STATE_KEY]["total_tokens"] == 1_100
    assert stopped["messages"][0].tool_calls == []


def test_token_budget_counts_distinct_receipts_even_when_tool_call_id_is_reused() -> None:
    dispatch = AIMessage(
        id="budget-distinct-dispatch",
        content="",
        tool_calls=[_task_call("reused-budget-call")],
        usage_metadata={
            "input_tokens": 50,
            "output_tokens": 50,
            "total_tokens": 100,
        },
    )
    results = [
        ToolMessage(
            id=f"budget-distinct-{index}",
            content="done",
            tool_call_id="reused-budget-call",
            additional_kwargs={
                "subagent_usage_receipt_id": f"budget-receipt-{index}",
                "subagent_token_usage": {
                    "input_tokens": 250,
                    "output_tokens": 250,
                    "total_tokens": 500,
                },
            },
        )
        for index in (1, 2)
    ]
    response = AIMessage(
        id="budget-distinct-response",
        content="",
        tool_calls=[{"name": "lookup", "args": {}, "id": "next", "type": "tool_call"}],
        usage_metadata={
            "input_tokens": 5,
            "output_tokens": 5,
            "total_tokens": 10,
        },
    )

    update = _budget(max_tokens=1_000).after_model(
        {"messages": [dispatch, *results, response]},
        _runtime("run-distinct-receipts"),
    )

    assert update is not None
    assert update["messages"][0].tool_calls == []


def test_token_budget_hard_stops_on_a_conflicting_receipt_batch() -> None:
    dispatch = AIMessage(
        id="budget-conflict-dispatch",
        content="",
        tool_calls=[_task_call("budget-conflict-call")],
        usage_metadata={
            "input_tokens": 50,
            "output_tokens": 50,
            "total_tokens": 100,
        },
    )

    def result(message_id: str, input_tokens: int, output_tokens: int) -> ToolMessage:
        return ToolMessage(
            id=message_id,
            content="done",
            tool_call_id="budget-conflict-call",
            additional_kwargs={
                "subagent_usage_receipt_id": "conflicting-budget-receipt",
                "subagent_token_usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                },
            },
        )

    response = AIMessage(
        id="budget-conflict-response",
        content="finished",
        usage_metadata={
            "input_tokens": 5,
            "output_tokens": 5,
            "total_tokens": 10,
        },
    )
    runtime = _runtime("run-conflicting-receipt")
    budget = _budget(max_tokens=1_000)

    update = budget.after_model(
        {
            "messages": [
                dispatch,
                result("conflict-one", 450, 450),
                result("conflict-two", 550, 550),
                response,
            ]
        },
        runtime,
    )

    assert update is not None
    assert update["messages"][0].tool_calls == []


def test_token_budget_conflict_decision_is_independent_of_arrival_timing() -> None:
    dispatch = AIMessage(
        id="timing-dispatch",
        content="",
        tool_calls=[_task_call("timing-call")],
        usage_metadata={
            "input_tokens": 50,
            "output_tokens": 50,
            "total_tokens": 100,
        },
    )

    def result(message_id: str, amount: int) -> ToolMessage:
        return ToolMessage(
            id=message_id,
            content="done",
            tool_call_id="timing-call",
            additional_kwargs={
                "subagent_usage_receipt_id": "timing-conflict",
                "subagent_token_usage": {
                    "input_tokens": amount,
                    "output_tokens": amount,
                    "total_tokens": amount * 2,
                },
            },
        )

    first = result("timing-first", 450)
    first_response = AIMessage(
        id="timing-response-1",
        content="continue",
        usage_metadata={
            "input_tokens": 5,
            "output_tokens": 5,
            "total_tokens": 10,
        },
    )
    second = result("timing-second", 550)
    final_response = AIMessage(
        id="timing-response-2",
        content="",
        tool_calls=[
            {
                "name": "lookup",
                "args": {},
                "id": "next",
                "type": "tool_call",
            }
        ],
        usage_metadata={
            "input_tokens": 5,
            "output_tokens": 5,
            "total_tokens": 10,
        },
    )
    messages = [dispatch, first, first_response, second, final_response]

    incremental = _budget(max_tokens=5_000)
    incremental_runtime = _runtime("run-conflict-incremental")
    _assert_budget_checkpoint_only(
        incremental.after_model(
            {"messages": messages[:3]},
            incremental_runtime,
        ),
    )
    incremental_stop = incremental.after_model(
        {"messages": messages},
        incremental_runtime,
    )
    one_shot_stop = _budget(max_tokens=5_000).after_model(
        {"messages": messages},
        _runtime("run-conflict-one-shot"),
    )

    assert incremental_stop is not None
    assert one_shot_stop is not None
    assert incremental_stop["messages"][0].tool_calls == []
    assert one_shot_stop["messages"][0].tool_calls == []


def test_token_budget_hard_stops_when_a_seen_receipt_is_replaced() -> None:
    dispatch = AIMessage(
        id="replacement-dispatch",
        content="",
        tool_calls=[_task_call("replacement-call")],
        usage_metadata={
            "input_tokens": 50,
            "output_tokens": 50,
            "total_tokens": 100,
        },
    )

    def result(amount: int) -> ToolMessage:
        return ToolMessage(
            id="replacement-result",
            content="done",
            tool_call_id="replacement-call",
            additional_kwargs={
                "subagent_usage_receipt_id": "replacement-receipt",
                "subagent_token_usage": {
                    "input_tokens": amount,
                    "output_tokens": amount,
                    "total_tokens": amount * 2,
                },
            },
        )

    first_response = AIMessage(
        id="replacement-response-1",
        content="continue",
        usage_metadata={
            "input_tokens": 5,
            "output_tokens": 5,
            "total_tokens": 10,
        },
    )
    second_response = AIMessage(
        id="replacement-response-2",
        content="",
        tool_calls=[
            {
                "name": "lookup",
                "args": {},
                "id": "next",
                "type": "tool_call",
            }
        ],
        usage_metadata={
            "input_tokens": 5,
            "output_tokens": 5,
            "total_tokens": 10,
        },
    )
    budget = _budget(max_tokens=5_000)
    runtime = _runtime("run-replaced-conflict")

    _assert_budget_checkpoint_only(
        budget.after_model(
            {"messages": [dispatch, result(10), first_response]},
            runtime,
        ),
    )

    stopped = budget.after_model(
        {"messages": [dispatch, result(20), first_response, second_response]},
        runtime,
    )

    assert stopped is not None
    assert stopped["messages"][0].tool_calls == []


def test_historical_receipt_conflict_does_not_poison_a_new_run() -> None:
    historical_dispatch = AIMessage(
        id="historical-conflict-dispatch",
        content="",
        tool_calls=[_task_call("historical-conflict-call")],
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": 10,
            "total_tokens": 20,
        },
    )

    def historical_result(message_id: str, amount: int) -> ToolMessage:
        return ToolMessage(
            id=message_id,
            content="old",
            tool_call_id="historical-conflict-call",
            additional_kwargs={
                "subagent_usage_receipt_id": "historical-conflict-receipt",
                "subagent_token_usage": {
                    "input_tokens": amount,
                    "output_tokens": amount,
                    "total_tokens": amount * 2,
                },
            },
        )

    history = [
        historical_dispatch,
        historical_result("historical-conflict-one", 10),
        historical_result("historical-conflict-two", 20),
        AIMessage(id="historical-conflict-final", content="old answer"),
    ]
    current = AIMessage(
        id="current-response",
        content="",
        tool_calls=[
            {
                "name": "lookup",
                "args": {},
                "id": "current-next",
                "type": "tool_call",
            }
        ],
        usage_metadata={
            "input_tokens": 5,
            "output_tokens": 5,
            "total_tokens": 10,
        },
    )
    budget = _budget(max_tokens=999_999)
    runtime = _runtime("run-after-historical-conflict")
    budget.before_agent({"messages": history}, runtime)

    _assert_budget_checkpoint_only(
        budget.after_model(
            {"messages": [*history, current]},
            runtime,
        ),
    )


def test_terminal_after_agent_records_a_new_conflict_only_for_the_current_run() -> None:
    dispatch = AIMessage(
        id="terminal-conflict-dispatch",
        content="",
        tool_calls=[_task_call("terminal-conflict-call")],
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": 10,
            "total_tokens": 20,
        },
    )

    def result(message_id: str, amount: int) -> ToolMessage:
        return ToolMessage(
            id=message_id,
            content="approval terminal",
            tool_call_id="terminal-conflict-call",
            additional_kwargs={
                "subagent_usage_receipt_id": "terminal-conflict-receipt",
                "subagent_token_usage": {
                    "input_tokens": amount,
                    "output_tokens": amount,
                    "total_tokens": amount * 2,
                },
            },
        )

    messages = [
        dispatch,
        result("terminal-conflict-one", 10),
        result("terminal-conflict-two", 20),
    ]
    budget = _budget(max_tokens=999_999)
    current_runtime = _runtime("run-terminal-conflict")

    budget.after_agent({"messages": messages}, current_runtime)

    assert budget.consume_stop_reason("run-terminal-conflict") == "token_capped"

    replay_runtime = _runtime("run-after-terminal-conflict")
    budget.before_agent({"messages": messages}, replay_runtime)
    budget.after_agent({"messages": messages}, replay_runtime)
    assert budget.consume_stop_reason("run-after-terminal-conflict") is None


def test_malformed_historical_state_does_not_hide_a_new_valid_receipt() -> None:
    historical = AIMessage(
        id="malformed-history",
        content="old",
        usage_metadata={
            "input_tokens": 20,
            "output_tokens": 10,
            "total_tokens": 30,
        },
        additional_kwargs={
            "subagent_usage_receipt_state": {
                "version": 1,
                "baseline": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 15,
                },
                "contributions": "corrupted",
            }
        },
    )
    dispatch = AIMessage(
        id="new-valid-dispatch",
        content="",
        tool_calls=[_task_call("new-valid-call")],
        usage_metadata={
            "input_tokens": 50,
            "output_tokens": 50,
            "total_tokens": 100,
        },
    )
    result = ToolMessage(
        id="new-valid-result",
        content="done",
        tool_call_id="new-valid-call",
        additional_kwargs={
            "subagent_usage_receipt_id": "new-valid-receipt",
            "subagent_token_usage": {
                "input_tokens": 600,
                "output_tokens": 600,
                "total_tokens": 1_200,
            },
        },
    )
    response = AIMessage(
        id="new-valid-response",
        content="",
        tool_calls=[
            {
                "name": "lookup",
                "args": {},
                "id": "next",
                "type": "tool_call",
            }
        ],
        usage_metadata={
            "input_tokens": 5,
            "output_tokens": 5,
            "total_tokens": 10,
        },
    )
    runtime = _runtime("run-after-malformed-history")
    budget = _budget(max_tokens=1_000)
    budget.before_agent({"messages": [historical]}, runtime)

    update = budget.after_model(
        {"messages": [historical, dispatch, result, response]},
        runtime,
    )

    assert update is not None
    assert update["messages"][0].tool_calls == []


def test_terminal_tool_receipt_is_attributed_when_agent_exits_before_another_model() -> None:
    dispatch = AIMessage(
        id="dispatch-approval",
        content="",
        tool_calls=[_task_call("approval-call")],
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": 4,
            "total_tokens": 14,
        },
    )
    approval = ToolMessage(
        id="approval-result",
        content="Delegated host command execution requires approval.",
        tool_call_id="approval-call",
        additional_kwargs={
            "subagent_token_usage": {
                "input_tokens": 3,
                "output_tokens": 5,
                "total_tokens": 8,
            },
            "subagent_usage_receipt_id": "approval-receipt",
        },
    )
    middleware = TokenUsageMiddleware()

    update = middleware.after_agent(
        {"messages": [dispatch, approval]},
        SimpleNamespace(),
    )

    assert update is not None
    assert len(update["messages"]) == 1
    updated_dispatch = update["messages"][0]
    assert isinstance(updated_dispatch, AIMessage)
    assert updated_dispatch.id == "dispatch-approval"
    assert updated_dispatch.usage_metadata == {
        "input_tokens": 13,
        "output_tokens": 9,
        "total_tokens": 22,
    }
    assert updated_dispatch.additional_kwargs["subagent_usage_receipt_state"]["contributions"] == [
        {
            "receipt_id": "approval-receipt",
            "usage": {
                "input_tokens": 3,
                "output_tokens": 5,
                "total_tokens": 8,
            },
        }
    ]


def test_checkpoint_reentry_does_not_apply_the_same_receipt_twice() -> None:
    dispatch = AIMessage(
        id="dispatch-1",
        content="",
        tool_calls=[_task_call("shared-call")],
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": 4,
            "total_tokens": 14,
        },
    )
    result = ToolMessage(
        id="result-1",
        content="Task Succeeded. Result: inspected",
        tool_call_id="shared-call",
        additional_kwargs={
            "subagent_token_usage": {
                "input_tokens": 3,
                "output_tokens": 5,
                "total_tokens": 8,
            },
            "subagent_usage_receipt_id": "receipt-1",
        },
    )
    next_response = AIMessage(id="response-1", content="Finished.")
    messages = [dispatch, result, next_response]
    middleware = TokenUsageMiddleware()

    first_update = middleware.after_model(
        {"messages": messages},
        SimpleNamespace(),
    )
    assert first_update is not None
    resumed_messages = add_messages(messages, first_update["messages"])

    replay_update = middleware.after_model(
        {"messages": resumed_messages},
        SimpleNamespace(),
    )

    assert replay_update is None
    replayed_dispatch = next(message for message in resumed_messages if isinstance(message, AIMessage) and message.id == "dispatch-1")
    receipt_state = replayed_dispatch.additional_kwargs["subagent_usage_receipt_state"]
    assert [contribution["receipt_id"] for contribution in receipt_state["contributions"]] == ["receipt-1"]


def test_distinct_receipts_with_the_same_tool_call_id_are_both_attributed() -> None:
    dispatch = AIMessage(
        id="dispatch-1",
        content="",
        tool_calls=[_task_call("reused-call"), _task_call("reused-call")],
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": 4,
            "total_tokens": 14,
        },
    )
    first_result = ToolMessage(
        id="result-1",
        content="Task Succeeded. Result: first",
        tool_call_id="reused-call",
        additional_kwargs={
            "subagent_token_usage": {
                "input_tokens": 3,
                "output_tokens": 5,
                "total_tokens": 8,
            },
            "subagent_usage_receipt_id": "receipt-1",
        },
    )
    second_result = ToolMessage(
        id="result-2",
        content="Task Succeeded. Result: second",
        tool_call_id="reused-call",
        additional_kwargs={
            "subagent_token_usage": {
                "input_tokens": 7,
                "output_tokens": 11,
                "total_tokens": 18,
            },
            "subagent_usage_receipt_id": "receipt-2",
        },
    )
    next_response = AIMessage(id="response-1", content="Finished.")

    update = TokenUsageMiddleware().after_model(
        {
            "messages": [
                dispatch,
                first_result,
                second_result,
                next_response,
            ]
        },
        SimpleNamespace(),
    )

    assert update is not None
    updated_dispatch = next(message for message in update["messages"] if isinstance(message, AIMessage) and message.id == "dispatch-1")
    assert updated_dispatch.usage_metadata == {
        "input_tokens": 20,
        "output_tokens": 20,
        "total_tokens": 40,
    }
    receipt_state = updated_dispatch.additional_kwargs["subagent_usage_receipt_state"]
    assert {contribution["receipt_id"] for contribution in receipt_state["contributions"]} == {"receipt-1", "receipt-2"}


def test_stored_baseline_recomputes_usage_instead_of_adding_to_current_totals() -> None:
    dispatch = AIMessage(
        id="dispatch-1",
        content="",
        tool_calls=[_task_call("shared-call")],
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": 4,
            "total_tokens": 14,
        },
    )
    result = ToolMessage(
        id="result-1",
        content="Task Succeeded. Result: inspected",
        tool_call_id="shared-call",
        additional_kwargs={
            "subagent_token_usage": {
                "input_tokens": 3,
                "output_tokens": 5,
                "total_tokens": 8,
            },
            "subagent_usage_receipt_id": "receipt-1",
        },
    )
    next_response = AIMessage(id="response-1", content="Finished.")
    middleware = TokenUsageMiddleware()
    first_update = middleware.after_model(
        {"messages": [dispatch, result, next_response]},
        SimpleNamespace(),
    )
    assert first_update is not None
    resumed_messages = add_messages(
        [dispatch, result, next_response],
        first_update["messages"],
    )
    stored_dispatch = next(message for message in resumed_messages if isinstance(message, AIMessage) and message.id == "dispatch-1")
    inflated_dispatch = stored_dispatch.model_copy(
        update={
            "usage_metadata": {
                "input_tokens": 16,
                "output_tokens": 14,
                "total_tokens": 30,
            }
        }
    )
    resumed_messages = add_messages(resumed_messages, [inflated_dispatch])

    replay_update = middleware.after_model(
        {"messages": resumed_messages},
        SimpleNamespace(),
    )

    assert replay_update is not None
    recomputed_dispatch = next(message for message in replay_update["messages"] if isinstance(message, AIMessage) and message.id == "dispatch-1")
    assert recomputed_dispatch.usage_metadata == {
        "input_tokens": 13,
        "output_tokens": 9,
        "total_tokens": 22,
    }


def test_malformed_persisted_receipt_state_fails_closed_without_reapplying() -> None:
    dispatch = AIMessage(
        id="dispatch-1",
        content="",
        tool_calls=[_task_call("shared-call")],
        usage_metadata={
            "input_tokens": 13,
            "output_tokens": 9,
            "total_tokens": 22,
        },
        additional_kwargs={
            "subagent_usage_receipt_state": {
                "version": 1,
                "baseline": {
                    "input_tokens": 10,
                    "output_tokens": 4,
                    "total_tokens": 14,
                },
                "contributions": "corrupted",
            }
        },
    )
    result = ToolMessage(
        id="result-1",
        content="Task Succeeded. Result: inspected",
        tool_call_id="shared-call",
        additional_kwargs={
            "subagent_token_usage": {
                "input_tokens": 3,
                "output_tokens": 5,
                "total_tokens": 8,
            },
            "subagent_usage_receipt_id": "receipt-1",
        },
    )

    update = TokenUsageMiddleware().after_model(
        {
            "messages": [
                dispatch,
                result,
                AIMessage(id="response-1", content="Finished."),
            ]
        },
        SimpleNamespace(),
    )

    assert update is not None
    assert all(not isinstance(message, AIMessage) or message.id != "dispatch-1" for message in update["messages"])


def test_conflicting_duplicate_receipt_fails_closed_independent_of_message_order() -> None:
    def apply(results: list[ToolMessage]) -> AIMessage:
        dispatch = AIMessage(
            id="dispatch-1",
            content="",
            tool_calls=[_task_call("shared-call")],
            usage_metadata={
                "input_tokens": 10,
                "output_tokens": 4,
                "total_tokens": 14,
            },
        )
        update = TokenUsageMiddleware().after_model(
            {
                "messages": [
                    dispatch,
                    *results,
                    AIMessage(id="response-1", content="Finished."),
                ]
            },
            SimpleNamespace(),
        )
        assert update is not None
        return next(
            (message for message in update["messages"] if isinstance(message, AIMessage) and message.id == "dispatch-1"),
            dispatch,
        )

    first = ToolMessage(
        id="result-1",
        content="first",
        tool_call_id="shared-call",
        additional_kwargs={
            "subagent_usage_receipt_id": "receipt-conflict",
            "subagent_token_usage": {
                "input_tokens": 3,
                "output_tokens": 5,
                "total_tokens": 8,
            },
        },
    )
    second = ToolMessage(
        id="result-2",
        content="second",
        tool_call_id="shared-call",
        additional_kwargs={
            "subagent_usage_receipt_id": "receipt-conflict",
            "subagent_token_usage": {
                "input_tokens": 7,
                "output_tokens": 11,
                "total_tokens": 18,
            },
        },
    )

    forward = apply([first, second])
    reverse = apply([second, first])

    assert (
        forward.usage_metadata
        == reverse.usage_metadata
        == {
            "input_tokens": 10,
            "output_tokens": 4,
            "total_tokens": 14,
        }
    )
    assert (
        forward.additional_kwargs["subagent_usage_receipt_state"]
        == reverse.additional_kwargs["subagent_usage_receipt_state"]
        == {
            "version": 1,
            "baseline": {
                "input_tokens": 10,
                "output_tokens": 4,
                "total_tokens": 14,
            },
            "contributions": [],
            "conflicts": ["receipt-conflict"],
        }
    )


def test_conflict_tombstone_survives_tool_message_replacement_and_replay() -> None:
    dispatch = AIMessage(
        id="tombstone-dispatch",
        content="",
        tool_calls=[_task_call("tombstone-call")],
        usage_metadata={
            "input_tokens": 1,
            "output_tokens": 1,
            "total_tokens": 2,
        },
    )

    def result(amount: int) -> ToolMessage:
        return ToolMessage(
            id="tombstone-result",
            content="done",
            tool_call_id="tombstone-call",
            additional_kwargs={
                "subagent_usage_receipt_id": "tombstone-receipt",
                "subagent_token_usage": {
                    "input_tokens": amount,
                    "output_tokens": amount,
                    "total_tokens": amount * 2,
                },
            },
        )

    final = AIMessage(id="tombstone-final", content="finished")
    middleware = TokenUsageMiddleware()
    original = [dispatch, result(20), final]
    first_update = middleware.after_model({"messages": original}, SimpleNamespace())
    assert first_update is not None
    stored = add_messages(original, first_update["messages"])

    replaced = add_messages(stored, [result(30)])
    conflict_update = middleware.after_model({"messages": replaced}, SimpleNamespace())
    assert conflict_update is not None
    tombstoned = add_messages(replaced, conflict_update["messages"])
    tombstoned_dispatch = next(message for message in tombstoned if isinstance(message, AIMessage) and message.id == "tombstone-dispatch")
    assert tombstoned_dispatch.usage_metadata == {
        "input_tokens": 1,
        "output_tokens": 1,
        "total_tokens": 2,
    }
    assert tombstoned_dispatch.additional_kwargs["subagent_usage_receipt_state"]["conflicts"] == ["tombstone-receipt"]

    assert middleware.after_model({"messages": tombstoned}, SimpleNamespace()) is None
    replayed_dispatch = next(message for message in tombstoned if isinstance(message, AIMessage) and message.id == "tombstone-dispatch")
    assert replayed_dispatch.usage_metadata == {
        "input_tokens": 1,
        "output_tokens": 1,
        "total_tokens": 2,
    }


def test_conflict_tombstone_survives_compression_of_the_earliest_turn() -> None:
    middleware = TokenUsageMiddleware()
    first_dispatch = AIMessage(
        id="compressed-conflict-dispatch-1",
        content="",
        tool_calls=[_task_call("compressed-conflict-call-1")],
        usage_metadata={
            "input_tokens": 2,
            "output_tokens": 2,
            "total_tokens": 4,
        },
    )
    first_result = ToolMessage(
        id="compressed-conflict-result-1",
        content="first",
        tool_call_id="compressed-conflict-call-1",
        additional_kwargs={
            "subagent_usage_receipt_id": "compressed-conflict-receipt",
            "subagent_token_usage": {
                "input_tokens": 10,
                "output_tokens": 10,
                "total_tokens": 20,
            },
        },
    )
    first_final = AIMessage(id="compressed-conflict-final-1", content="continue")
    first_turn = [first_dispatch, first_result, first_final]
    first_update = middleware.after_model(
        {"messages": first_turn},
        SimpleNamespace(),
    )
    assert first_update is not None
    stored = add_messages(first_turn, first_update["messages"])

    second_dispatch = AIMessage(
        id="compressed-conflict-dispatch-2",
        content="",
        tool_calls=[_task_call("compressed-conflict-call-2")],
        usage_metadata={
            "input_tokens": 2,
            "output_tokens": 2,
            "total_tokens": 4,
        },
    )
    second_result = ToolMessage(
        id="compressed-conflict-result-2",
        content="second",
        tool_call_id="compressed-conflict-call-2",
        additional_kwargs={
            "subagent_usage_receipt_id": "compressed-conflict-receipt",
            "subagent_token_usage": {
                "input_tokens": 20,
                "output_tokens": 20,
                "total_tokens": 40,
            },
        },
    )
    second_final = AIMessage(id="compressed-conflict-final-2", content="finished")
    full_transcript = [*stored, second_dispatch, second_result, second_final]
    conflict_update = middleware.after_model(
        {"messages": full_transcript},
        SimpleNamespace(),
    )
    assert conflict_update is not None
    conflicted = add_messages(full_transcript, conflict_update["messages"])

    surviving_turn = [
        message
        for message in conflicted
        if message.id
        in {
            "compressed-conflict-dispatch-2",
            "compressed-conflict-result-2",
            "compressed-conflict-final-2",
        }
    ]
    assert (
        middleware.after_model(
            {"messages": surviving_turn},
            SimpleNamespace(),
        )
        is None
    )
    surviving_dispatch = next(message for message in surviving_turn if isinstance(message, AIMessage) and message.id == "compressed-conflict-dispatch-2")
    assert surviving_dispatch.usage_metadata == {
        "input_tokens": 2,
        "output_tokens": 2,
        "total_tokens": 4,
    }
    assert surviving_dispatch.additional_kwargs["subagent_usage_receipt_state"]["conflicts"] == ["compressed-conflict-receipt"]


def test_nonempty_invalid_parent_usage_is_preserved_without_creating_a_baseline() -> None:
    dispatch = AIMessage(
        id="dispatch-1",
        content="",
        tool_calls=[_task_call("shared-call")],
    )
    object.__setattr__(
        dispatch,
        "usage_metadata",
        {
            "input_tokens": 10,
            "output_tokens": 4,
            # Deliberately lacks total_tokens.
        },
    )
    result = ToolMessage(
        id="result-1",
        content="done",
        tool_call_id="shared-call",
        additional_kwargs={
            "subagent_usage_receipt_id": "receipt-1",
            "subagent_token_usage": {
                "input_tokens": 3,
                "output_tokens": 5,
                "total_tokens": 8,
            },
        },
    )

    update = TokenUsageMiddleware().after_model(
        {
            "messages": [
                dispatch,
                result,
                AIMessage(id="response-1", content="Finished."),
            ]
        },
        SimpleNamespace(),
    )

    assert update is not None
    assert all(not isinstance(message, AIMessage) or message.id != "dispatch-1" for message in update["messages"])
    assert dispatch.usage_metadata == {
        "input_tokens": 10,
        "output_tokens": 4,
    }


def test_boolean_receipt_state_version_is_rejected_without_rewriting_dispatch() -> None:
    dispatch = AIMessage(
        id="dispatch-1",
        content="",
        tool_calls=[_task_call("shared-call")],
        usage_metadata={
            "input_tokens": 13,
            "output_tokens": 9,
            "total_tokens": 22,
        },
        additional_kwargs={
            "subagent_usage_receipt_state": {
                "version": True,
                "baseline": {
                    "input_tokens": 10,
                    "output_tokens": 4,
                    "total_tokens": 14,
                },
                "contributions": [],
            }
        },
    )
    result = ToolMessage(
        id="result-1",
        content="done",
        tool_call_id="shared-call",
        additional_kwargs={
            "subagent_usage_receipt_id": "receipt-1",
            "subagent_token_usage": {
                "input_tokens": 3,
                "output_tokens": 5,
                "total_tokens": 8,
            },
        },
    )

    update = TokenUsageMiddleware().after_model(
        {
            "messages": [
                dispatch,
                result,
                AIMessage(id="response-1", content="Finished."),
            ]
        },
        SimpleNamespace(),
    )

    assert update is not None
    assert all(not isinstance(message, AIMessage) or message.id != "dispatch-1" for message in update["messages"])


def test_unmarked_historical_tool_usage_is_not_guessed_or_double_counted() -> None:
    dispatch = AIMessage(
        id="dispatch-1",
        content="",
        tool_calls=[_task_call("shared-call")],
        usage_metadata={
            # This historical dispatch may already contain usage folded by the
            # pre-receipt process cache. There is no durable marker proving
            # whether the ToolMessage below was applied.
            "input_tokens": 13,
            "output_tokens": 9,
            "total_tokens": 22,
        },
    )
    historical_result = ToolMessage(
        id="legacy-result",
        content="legacy checkpoint",
        tool_call_id="shared-call",
        additional_kwargs={
            "subagent_token_usage": {
                "input_tokens": 3,
                "output_tokens": 5,
                "total_tokens": 8,
            }
        },
    )

    middleware = TokenUsageMiddleware()
    messages = [
        dispatch,
        historical_result,
        AIMessage(
            id="response-1",
            content="Finished.",
            usage_metadata={
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 2,
            },
        ),
    ]
    budget = _budget(max_tokens=1_000)
    runtime = _runtime("run-legacy-receipt")
    _assert_budget_checkpoint_only(
        budget.after_model({"messages": messages}, runtime),
    )
    assert budget._cumulative_usage["run-legacy-receipt"].total == 24
    update = middleware.after_model(
        {
            "messages": messages,
        },
        SimpleNamespace(),
    )

    assert update is not None
    assert all(not isinstance(message, AIMessage) or message.id != "dispatch-1" for message in update["messages"])
    _assert_budget_checkpoint_only(
        budget.after_model({"messages": messages}, runtime),
    )
    assert budget._cumulative_usage["run-legacy-receipt"].total == 24


def test_unmarked_history_does_not_hide_a_new_explicit_receipt() -> None:
    historical_dispatch = AIMessage(
        id="mixed-historical-dispatch",
        content="",
        tool_calls=[_task_call("mixed-historical-call")],
        usage_metadata={
            "input_tokens": 13,
            "output_tokens": 9,
            "total_tokens": 22,
        },
    )
    historical_result = ToolMessage(
        id="mixed-historical-result",
        content="old",
        tool_call_id="mixed-historical-call",
        additional_kwargs={
            "subagent_token_usage": {
                "input_tokens": 3,
                "output_tokens": 5,
                "total_tokens": 8,
            }
        },
    )
    historical_final = AIMessage(id="mixed-historical-final", content="old answer")
    history = [historical_dispatch, historical_result, historical_final]

    current_dispatch = AIMessage(
        id="mixed-current-dispatch",
        content="",
        tool_calls=[_task_call("mixed-current-call")],
        usage_metadata={
            "input_tokens": 5,
            "output_tokens": 5,
            "total_tokens": 10,
        },
    )
    current_result = ToolMessage(
        id="mixed-current-result",
        content="new",
        tool_call_id="mixed-current-call",
        additional_kwargs={
            "subagent_usage_receipt_id": "mixed-current-receipt",
            "subagent_token_usage": {
                "input_tokens": 4,
                "output_tokens": 6,
                "total_tokens": 10,
            },
        },
    )
    current_final = AIMessage(
        id="mixed-current-final",
        content="new answer",
        usage_metadata={
            "input_tokens": 1,
            "output_tokens": 1,
            "total_tokens": 2,
        },
    )
    messages = [*history, current_dispatch, current_result, current_final]

    update = TokenUsageMiddleware().after_model(
        {"messages": messages},
        SimpleNamespace(),
    )
    assert update is not None
    updated_dispatches = {message.id: message for message in update["messages"] if isinstance(message, AIMessage)}
    assert "mixed-historical-dispatch" not in updated_dispatches
    assert updated_dispatches["mixed-current-dispatch"].usage_metadata == {
        "input_tokens": 9,
        "output_tokens": 11,
        "total_tokens": 20,
    }

    budget = _budget(max_tokens=1_000)
    runtime = _runtime("run-mixed-history")
    budget.before_agent({"messages": history}, runtime)
    _assert_budget_checkpoint_only(
        budget.after_model({"messages": messages}, runtime),
    )
    assert budget._cumulative_usage["run-mixed-history"].total == 22


@pytest.mark.parametrize(
    ("second_usage", "expected_first_usage", "expected_contributions"),
    [
        (
            {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8},
            {"input_tokens": 13, "output_tokens": 9, "total_tokens": 22},
            1,
        ),
        (
            {"input_tokens": 7, "output_tokens": 11, "total_tokens": 18},
            {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
            0,
        ),
    ],
)
def test_receipt_identity_is_global_across_sequential_dispatches(
    second_usage: dict[str, int],
    expected_first_usage: dict[str, int],
    expected_contributions: int,
) -> None:
    middleware = TokenUsageMiddleware()
    first_dispatch = AIMessage(
        id="global-dispatch-1",
        content="",
        tool_calls=[_task_call("global-call-1")],
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": 4,
            "total_tokens": 14,
        },
    )
    first_result = ToolMessage(
        id="global-result-1",
        content="first",
        tool_call_id="global-call-1",
        additional_kwargs={
            "subagent_usage_receipt_id": "global-receipt",
            "subagent_token_usage": {
                "input_tokens": 3,
                "output_tokens": 5,
                "total_tokens": 8,
            },
        },
    )
    first_response = AIMessage(id="global-response-1", content="continue")
    first_messages = [first_dispatch, first_result, first_response]
    first_update = middleware.after_model(
        {"messages": first_messages},
        SimpleNamespace(),
    )
    assert first_update is not None
    resumed = add_messages(first_messages, first_update["messages"])

    second_dispatch = AIMessage(
        id="global-dispatch-2",
        content="",
        tool_calls=[_task_call("global-call-2")],
        usage_metadata={
            "input_tokens": 6,
            "output_tokens": 4,
            "total_tokens": 10,
        },
    )
    second_result = ToolMessage(
        id="global-result-2",
        content="second",
        tool_call_id="global-call-2",
        additional_kwargs={
            "subagent_usage_receipt_id": "global-receipt",
            "subagent_token_usage": second_usage,
        },
    )
    final_response = AIMessage(id="global-final", content="finished")
    resumed.extend([second_dispatch, second_result, final_response])

    second_update = middleware.after_model(
        {"messages": resumed},
        SimpleNamespace(),
    )
    assert second_update is not None
    reduced = add_messages(resumed, second_update["messages"])
    reduced_first = next(message for message in reduced if isinstance(message, AIMessage) and message.id == "global-dispatch-1")
    reduced_second = next(message for message in reduced if isinstance(message, AIMessage) and message.id == "global-dispatch-2")

    assert reduced_first.usage_metadata == expected_first_usage
    assert len(reduced_first.additional_kwargs["subagent_usage_receipt_state"]["contributions"]) == expected_contributions
    if expected_contributions:
        assert "subagent_usage_receipt_state" not in reduced_second.additional_kwargs
    else:
        assert reduced_second.additional_kwargs["subagent_usage_receipt_state"]["conflicts"] == ["global-receipt"]


@pytest.mark.asyncio
async def test_approval_pause_runs_after_agent_receipt_reducer_and_checkpoints_it() -> None:
    class Model(GenericFakeChatModel):
        calls: ClassVar[int] = 0

        def bind_tools(
            self,
            tools: Sequence[Any],
            **kwargs: Any,
        ) -> GenericFakeChatModel:
            del tools, kwargs
            return self

        def _generate(self, *args: Any, **kwargs: Any):
            type(self).calls += 1
            return super()._generate(*args, **kwargs)

    @tool("task")
    async def approval_task(value: str) -> Command:
        """Return one delegated host-execution approval anchor."""

        del value
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        id="approval-tool-result",
                        content="approval required",
                        tool_call_id="approval-tool-call",
                        artifact={
                            "host_execution_approval": {
                                "schema_version": 1,
                                "kind": "local_shell",
                                "approval_id": "approval-1",
                                "source_run_id": "approval-run",
                                "source_tool_call_id": "inner-call",
                            }
                        },
                        additional_kwargs={
                            "subagent_usage_receipt_id": "approval-graph-receipt",
                            "subagent_token_usage": {
                                "input_tokens": 3,
                                "output_tokens": 5,
                                "total_tokens": 8,
                            },
                        },
                    )
                ]
            },
            goto=END,
        )

    Model.calls = 0
    model = Model(
        messages=iter(
            [
                AIMessage(
                    id="approval-dispatch",
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {"value": "delegate"},
                            "id": "approval-tool-call",
                            "type": "tool_call",
                        }
                    ],
                    usage_metadata={
                        "input_tokens": 10,
                        "output_tokens": 4,
                        "total_tokens": 14,
                    },
                ),
                AIMessage(content="must not be generated"),
            ]
        )
    )
    checkpointer = InMemorySaver()
    agent = create_agent(
        model=model,
        tools=[approval_task],
        middleware=[
            TokenUsageMiddleware(),
            HostExecutionApprovalPauseMiddleware(),
        ],
        context_schema=dict,
        checkpointer=checkpointer,
    )
    config = {"configurable": {"thread_id": "approval-receipt-thread"}}

    result = await agent.ainvoke(
        {"messages": [HumanMessage(content="delegate")]},
        config,
        context={"run_id": "approval-run"},
    )

    assert Model.calls == 1
    dispatch = next(message for message in result["messages"] if isinstance(message, AIMessage) and message.id == "approval-dispatch")
    assert dispatch.usage_metadata == {
        "input_tokens": 13,
        "output_tokens": 9,
        "total_tokens": 22,
    }
    checkpoint = checkpointer.get_tuple(config)
    assert checkpoint is not None
    checkpoint_dispatch = next(message for message in checkpoint.checkpoint["channel_values"]["messages"] if isinstance(message, AIMessage) and message.id == "approval-dispatch")
    assert checkpoint_dispatch.additional_kwargs["subagent_usage_receipt_state"]["contributions"] == [
        {
            "receipt_id": "approval-graph-receipt",
            "usage": {
                "input_tokens": 3,
                "output_tokens": 5,
                "total_tokens": 8,
            },
        }
    ]
