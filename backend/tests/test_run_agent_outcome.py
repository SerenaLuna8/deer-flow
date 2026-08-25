"""Contracts for the internal Harness Execution semantic outcome."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

import deerflow.runtime.runs.worker as run_worker
from deerflow.agents.middlewares.token_budget_middleware import (
    TOKEN_BUDGET_USAGE_STATE_KEY,
)
from deerflow.agents.middlewares.tool_call_control import (
    ToolCallControlLoopFinalizationFailed,
    ToolCallControlStateInvalid,
)
from deerflow.config.app_config import AppConfig
from deerflow.runtime.context_keys import RuntimeContextKeys
from deerflow.runtime.journal import RunJournal
from deerflow.runtime.runs.execution_contracts import (
    RunAgentOutcome,
    RunAgentResourceOwnership,
    RunAgentUsageSnapshot,
    RunSemanticStopRecorder,
)
from deerflow.runtime.runs.manager import RunManager
from deerflow.runtime.runs.worker import RunContext, run_agent
from deerflow.sandbox.sandbox_provider import NotAcquired
from deerflow.token_budget_usage import (
    TokenBudgetUsageRecorder,
    TokenBudgetUsageSnapshot,
)


def _usage() -> RunAgentUsageSnapshot:
    return RunAgentUsageSnapshot(
        total_input_tokens=3,
        total_output_tokens=2,
        total_tokens=5,
        llm_call_count=1,
        lead_agent_tokens=4,
        subagent_tokens=1,
        middleware_tokens=0,
        token_usage_by_model={
            "model-a": {
                "input_tokens": 3,
                "output_tokens": 2,
            },
        },
    )


_PUBLIC_USAGE_KEYS = (
    "usage",
    "usage_completeness",
    "usage_metadata",
    "token_usage",
    "token_usage_attribution",
    "subagent_token_usage",
    "subagent_usage_completeness",
    "subagent_usage_receipt_id",
    "subagent_usage_receipt_state",
)


def _assert_public_payload_has_no_token_tracking(
    payload: object,
    *,
    sentinel_values: tuple[int, ...],
) -> None:
    encoded = json.dumps(payload, sort_keys=True, default=str)
    for key in _PUBLIC_USAGE_KEYS:
        assert f'"{key}"' not in encoded
    for value in sentinel_values:
        assert str(value) not in encoded


def test_run_agent_outcome_is_immutable_and_has_closed_combinations() -> None:
    source = {
        "model-a": {
            "input_tokens": 3,
            "output_tokens": 2,
        },
    }
    usage = RunAgentUsageSnapshot(
        total_input_tokens=3,
        total_output_tokens=2,
        total_tokens=5,
        llm_call_count=1,
        lead_agent_tokens=4,
        subagent_tokens=1,
        middleware_tokens=0,
        token_usage_by_model=source,
    )
    source["model-a"]["input_tokens"] = 99

    assert usage.token_usage_by_model["model-a"]["input_tokens"] == 3
    with pytest.raises(TypeError):
        usage.token_usage_by_model["model-a"]["input_tokens"] = 7  # type: ignore[index]
    assert (
        RunAgentOutcome.succeeded(
            usage,
            suspended_approval_id="approval-1",
        ).status
        == "succeeded"
    )
    assert RunAgentOutcome.cancelled(usage).status == "cancelled"
    assert (
        RunAgentOutcome.failed(
            usage,
            public_error_code="MODEL_OUTPUT_LIMIT",
        ).status
        == "failed"
    )

    with pytest.raises(ValueError, match="failed outcome requires"):
        RunAgentOutcome("failed", usage)
    with pytest.raises(ValueError, match="only successful"):
        RunAgentOutcome(
            "cancelled",
            usage,
            suspended_approval_id="approval-1",
        )


def test_run_agent_resource_ownership_transfers_once() -> None:
    ownership = RunAgentResourceOwnership()

    assert ownership.transferred is False
    ownership.transfer_to_runner()
    assert ownership.transferred is True
    with pytest.raises(RuntimeError, match="already transferred"):
        ownership.transfer_to_runner()


@pytest.mark.asyncio
async def test_run_agent_disabled_token_tracking_excludes_provider_usage_from_settlement() -> None:
    persisted_events: list[dict] = []
    original_messages: list[AIMessage] = []
    run_manager = RunManager()
    record = await run_manager.create("disabled-token-tracking-thread")
    private_budget_baseline = TokenBudgetUsageSnapshot(
        run_id=record.run_id,
        input_tokens=450,
        output_tokens=450,
        total_tokens=900,
    )
    private_budget_recorder = TokenBudgetUsageRecorder(
        private_budget_baseline,
    )

    class EventStore:
        async def put_batch(self, events, **_kwargs) -> None:
            persisted_events.extend(events)

    class Agent:
        async def astream(self, *_args, **kwargs):
            assert kwargs["config"]["context"][RuntimeContextKeys.TOKEN_BUDGET_USAGE_RECORDER] is private_budget_recorder
            journal = next(callback for callback in kwargs["config"]["callbacks"] if isinstance(callback, RunJournal))
            message = AIMessage(
                id="disabled-token-tracking-answer",
                content="done",
                response_metadata={
                    "model_name": "provider-model",
                    "usage": {"input_tokens": 11},
                    "token_usage": {"output_tokens": 7},
                    "usage_metadata": {"total_tokens": 18},
                },
                usage_metadata={
                    "input_tokens": 11,
                    "output_tokens": 7,
                    "total_tokens": 18,
                },
            )
            original_messages.append(message)
            journal.on_llm_end(
                SimpleNamespace(
                    generations=[[SimpleNamespace(message=message)]],
                ),
                run_id=uuid.uuid4(),
                tags=["lead_agent"],
            )
            yield {"messages": [message]}

    class Bridge:
        async def publish(self, *_args, **_kwargs) -> None:
            return None

        async def publish_end(self, _run_id: str) -> None:
            return None

    outcome = await run_agent(
        Bridge(),
        run_manager,
        record,
        ctx=RunContext(
            checkpointer=None,
            event_store=EventStore(),
            app_config=AppConfig(
                token_usage={"enabled": False},
                sandbox={
                    "use": "deerflow.sandbox.local:LocalSandboxProvider",
                },
            ),
            token_budget_usage_recorder=private_budget_recorder,
        ),
        agent_factory=lambda *, config: Agent(),
        graph_input={},
        config={},
    )

    assert outcome.usage.total_input_tokens == 0
    assert outcome.usage.total_output_tokens == 0
    assert outcome.usage.total_tokens == 0
    assert outcome.usage.llm_call_count == 0
    assert outcome.usage.token_budget_usage == private_budget_baseline
    ai_event = next(event for event in persisted_events if event["event_type"] == "llm.ai.response")
    assert "usage_metadata" not in ai_event["content"]
    assert "usage" not in ai_event["metadata"]
    assert ai_event["content"]["response_metadata"] == {"model_name": "provider-model"}
    assert original_messages[0].usage_metadata == {
        "input_tokens": 11,
        "output_tokens": 7,
        "total_tokens": 18,
    }
    assert original_messages[0].response_metadata == {
        "model_name": "provider-model",
        "usage": {"input_tokens": 11},
        "token_usage": {"output_tokens": 7},
        "usage_metadata": {"total_tokens": 18},
    }


@pytest.mark.asyncio
async def test_run_agent_disabled_token_tracking_sanitizes_all_provider_sse_without_mutating_graph_values() -> None:
    published: list[tuple[str, object]] = []
    message_chunk = AIMessageChunk(
        id="provider-delta",
        content="partial",
        usage_metadata={
            "input_tokens": 9101,
            "output_tokens": 9102,
            "total_tokens": 18203,
        },
        response_metadata={
            "model_name": "provider-model",
            "token_usage": {"output_tokens": 9103},
            "nested": {"usage_metadata": {"total_tokens": 9104}},
        },
    )
    final_message = AIMessage(
        id="provider-final",
        content="done",
        tool_calls=[
            {
                "name": "business_tool",
                "args": {"usage": "business tool instructions"},
                "id": "business-tool-call",
            }
        ],
        usage_metadata={
            "input_tokens": 9201,
            "output_tokens": 9202,
            "total_tokens": 18403,
        },
        response_metadata={
            "model_name": "provider-model",
            "usage": {"input_tokens": 9203},
            "nested": {"token_usage": {"output_tokens": 9204}},
        },
    )
    tool_message = ToolMessage(
        id="business-tool-result",
        content=[
            {
                "type": "text",
                "text": "tool result",
                "usage": "business content instructions",
            }
        ],
        tool_call_id="business-tool-call",
        additional_kwargs={"usage": "business envelope instructions"},
    )
    message_metadata = {
        "langgraph_node": "agent",
        "usage": {"input_tokens": 9301},
        "nested": {
            "response_metadata": {
                "usage_metadata": {"total_tokens": 9302},
            }
        },
    }
    values = {
        "messages": [final_message, tool_message],
        "business_state": {
            "status": "complete",
            "usage": "business state instructions",
        },
        TOKEN_BUDGET_USAGE_STATE_KEY: {
            "run_id": "private-budget-authority",
            "input_tokens": 86751,
            "output_tokens": 86752,
            "total_tokens": 173503,
        },
    }

    class Agent:
        async def astream(self, *_args, **kwargs):
            assert kwargs["config"]["context"]["__token_usage_tracking_enabled"] is False
            yield "messages", (message_chunk, message_metadata)
            yield "values", values

    class Bridge:
        async def publish(self, _run_id: str, event: str, payload: object) -> None:
            published.append((event, payload))

        async def publish_end(self, _run_id: str) -> None:
            return None

    run_manager = RunManager()
    record = await run_manager.create("disabled-token-tracking-sse-thread")

    await run_agent(
        Bridge(),
        run_manager,
        record,
        ctx=RunContext(
            checkpointer=None,
            run_events_config=SimpleNamespace(
                track_token_usage=False,
            ),
            app_config=AppConfig(
                token_usage={"enabled": True},
                sandbox={
                    "use": "deerflow.sandbox.local:LocalSandboxProvider",
                },
            ),
        ),
        agent_factory=lambda *, config: Agent(),
        graph_input={},
        config={},
        stream_modes=["messages", "values"],
    )

    messages_payload = next(payload for event, payload in published if event == "messages")
    values_payload = next(payload for event, payload in published if event == "values")
    encoded = json.dumps(
        [messages_payload, values_payload],
        sort_keys=True,
        default=str,
    )
    for sentinel in (
        9101,
        9102,
        18203,
        9103,
        9104,
        9201,
        9202,
        18403,
        9203,
        9204,
        9301,
        9302,
        86751,
        86752,
        173503,
    ):
        assert str(sentinel) not in encoded
    assert messages_payload[0]["response_metadata"] == {  # type: ignore[index]
        "model_name": "provider-model",
        "nested": {},
    }
    assert "usage_metadata" not in messages_payload[0]  # type: ignore[operator]
    assert messages_payload[1] == {  # type: ignore[index]
        "langgraph_node": "agent",
        "nested": {"response_metadata": {}},
    }
    assert values_payload["business_state"] == {  # type: ignore[index]
        "status": "complete",
        "usage": "business state instructions",
    }
    assert TOKEN_BUDGET_USAGE_STATE_KEY not in values_payload  # type: ignore[operator]
    projected_final = values_payload["messages"][0]  # type: ignore[index]
    assert projected_final["tool_calls"][0]["args"] == {
        "usage": "business tool instructions",
    }
    projected_tool = values_payload["messages"][1]  # type: ignore[index]
    assert projected_tool["content"][0]["usage"] == ("business content instructions")
    assert projected_tool["additional_kwargs"]["usage"] == ("business envelope instructions")
    assert message_chunk.usage_metadata == {
        "input_tokens": 9101,
        "output_tokens": 9102,
        "total_tokens": 18203,
    }
    assert message_chunk.response_metadata["token_usage"] == {
        "output_tokens": 9103,
    }
    assert message_metadata["usage"] == {"input_tokens": 9301}
    assert final_message.usage_metadata == {
        "input_tokens": 9201,
        "output_tokens": 9202,
        "total_tokens": 18403,
    }
    assert final_message.tool_calls[0]["args"] == {"usage": "business tool instructions"}
    assert tool_message.additional_kwargs["usage"] == ("business envelope instructions")
    assert values[TOKEN_BUDGET_USAGE_STATE_KEY]["total_tokens"] == 173503


@pytest.mark.asyncio
async def test_run_agent_disabled_token_tracking_sanitizes_subagent_sse_and_durable_event_without_mutating_chunk() -> None:
    published: list[tuple[str, object]] = []
    persisted_events: list[dict] = []
    terminal_chunk = {
        "type": "task_completed",
        "task_id": "delegated-task-1",
        "model_name": "subagent-model",
        "usage": {
            "input_tokens": 9501,
            "output_tokens": 9502,
            "total_tokens": 19003,
        },
        "usage_completeness": "final_observed",
        "business_payload": {
            "usage": "business custom-event instructions",
        },
        "result": "delegated result",
    }

    class EventStore:
        async def put_batch(self, events, **_kwargs) -> None:
            persisted_events.extend(events)

    class Agent:
        async def astream(self, *_args, **_kwargs):
            yield "custom", terminal_chunk
            yield "values", {"messages": []}

    class Bridge:
        async def publish(self, _run_id: str, event: str, payload: object) -> None:
            published.append((event, payload))

        async def publish_end(self, _run_id: str) -> None:
            return None

    run_manager = RunManager()
    record = await run_manager.create("disabled-subagent-token-tracking-thread")

    await run_agent(
        Bridge(),
        run_manager,
        record,
        ctx=RunContext(
            checkpointer=None,
            event_store=EventStore(),
            app_config=AppConfig(
                token_usage={"enabled": False},
                sandbox={
                    "use": "deerflow.sandbox.local:LocalSandboxProvider",
                },
            ),
        ),
        agent_factory=lambda *, config: Agent(),
        graph_input={},
        config={},
        stream_modes=["custom"],
    )

    custom_payload = next(payload for event, payload in published if event == "custom")
    assert "usage" not in custom_payload
    assert "usage_completeness" not in custom_payload
    assert custom_payload["business_payload"] == {
        "usage": "business custom-event instructions",
    }
    custom_encoded = json.dumps(custom_payload, sort_keys=True, default=str)
    for sentinel in (9501, 9502, 19003):
        assert str(sentinel) not in custom_encoded
    durable_terminal = next(event for event in persisted_events if event["event_type"] == "subagent.end")
    _assert_public_payload_has_no_token_tracking(
        durable_terminal,
        sentinel_values=(9501, 9502, 19003),
    )
    assert durable_terminal["content"] == {
        "task_id": "delegated-task-1",
        "status": "completed",
        "model_name": "subagent-model",
        "result": "delegated result",
    }
    assert terminal_chunk["usage"] == {
        "input_tokens": 9501,
        "output_tokens": 9502,
        "total_tokens": 19003,
    }
    assert terminal_chunk["usage_completeness"] == "final_observed"


@pytest.mark.asyncio
async def test_run_agent_disabled_token_tracking_excludes_subagent_receipt_from_journal_and_settlement() -> None:
    persisted_events: list[dict] = []
    original_messages: list[ToolMessage] = []

    class EventStore:
        async def put_batch(self, events, **_kwargs) -> None:
            persisted_events.extend(events)

    journals: list[RunJournal] = []

    class Agent:
        async def astream(self, *_args, **kwargs):
            journal = kwargs["config"]["context"][RuntimeContextKeys.RUN_JOURNAL]
            assert isinstance(journal, RunJournal)
            journals.append(journal)
            journal.record_external_llm_usage_records(
                [
                    {
                        "source_run_id": "subagent-receipt-1",
                        "caller": "subagent:general-purpose",
                        "model_name": "subagent-model",
                        "input_tokens": 13,
                        "output_tokens": 5,
                        "total_tokens": 18,
                    }
                ]
            )
            message = ToolMessage(
                id="subagent-result-message",
                content="Task Succeeded. Result: done",
                tool_call_id="subagent-call",
                additional_kwargs={
                    "subagent_status": "completed",
                    "subagent_token_usage": {
                        "input_tokens": 13,
                        "output_tokens": 5,
                        "total_tokens": 18,
                    },
                    "subagent_usage_receipt_id": "subagent-receipt-1",
                    "subagent_usage_completeness": "final_observed",
                    "subagent_usage_receipt_state": {
                        "version": 1,
                        "baseline": {
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "total_tokens": 0,
                        },
                        "contributions": [],
                        "conflicts": [],
                    },
                },
            )
            original_messages.append(message)
            journal.on_tool_end(
                message,
                run_id=uuid.uuid4(),
                tags=["lead_agent"],
            )
            yield {"messages": []}

    class Bridge:
        async def publish(self, *_args, **_kwargs) -> None:
            return None

        async def publish_end(self, _run_id: str) -> None:
            return None

    run_manager = RunManager()
    record = await run_manager.create("disabled-subagent-token-tracking-thread")

    outcome = await run_agent(
        Bridge(),
        run_manager,
        record,
        ctx=RunContext(
            checkpointer=None,
            event_store=EventStore(),
            app_config=AppConfig(
                token_usage={"enabled": False},
                sandbox={
                    "use": "deerflow.sandbox.local:LocalSandboxProvider",
                },
            ),
        ),
        agent_factory=lambda *, config: Agent(),
        graph_input={},
        config={},
    )

    assert len(journals) == 1
    completion = journals[0].get_completion_data()
    assert completion["total_input_tokens"] == 0
    assert completion["total_output_tokens"] == 0
    assert completion["total_tokens"] == 0
    assert completion["subagent_tokens"] == 0
    assert completion["token_usage_by_model"] == {}
    assert outcome.usage.total_input_tokens == 0
    assert outcome.usage.total_output_tokens == 0
    assert outcome.usage.total_tokens == 0
    assert outcome.usage.subagent_tokens == 0
    assert outcome.usage.token_usage_by_model == {}
    tool_event = next(event for event in persisted_events if event["event_type"] == "llm.tool.result")
    assert tool_event["content"]["additional_kwargs"] == {"subagent_status": "completed"}
    assert original_messages[0].additional_kwargs["subagent_usage_receipt_id"] == "subagent-receipt-1"
    assert original_messages[0].additional_kwargs["subagent_usage_completeness"] == "final_observed"


@pytest.mark.asyncio
async def test_run_agent_returns_outcome_after_owned_resources_and_terminal_close() -> None:
    events: list[str] = []
    release_outcome = NotAcquired(owner_id=uuid.uuid4())

    class Authority:
        async def restore(self) -> object:
            events.append("authority:restore")
            return object()

        async def finalize(self) -> object:
            events.append("authority:finalize")
            return SimpleNamespace(workspace_changes=None, artifacts=())

        async def output_delivery_status(self) -> str:
            return "not_required"

        async def mark_failed(self) -> None:
            events.append("authority:failed")

        async def release(self) -> NotAcquired:
            events.append("authority:release")
            return release_outcome

    class Runtime:
        async def aclose(self, observed_outcome) -> None:
            assert observed_outcome is release_outcome
            events.append("runtime:close")

    class Agent:
        async def astream(self, *_args, **_kwargs):
            yield {"messages": []}

    class Bridge:
        async def publish(self, *_args, **_kwargs) -> None:
            return None

        async def publish_end(self, _run_id: str) -> None:
            events.append("stream:end")

    def agent_factory(*, config, private_runtime):
        del config
        assert isinstance(private_runtime, Runtime)
        return Agent()

    run_manager = RunManager()
    record = await run_manager.create("outcome-thread")
    ownership = RunAgentResourceOwnership()

    outcome = await run_agent(
        Bridge(),
        run_manager,
        record,
        ctx=RunContext(
            checkpointer=None,
            file_authority=Authority(),
            private_agent_runtime=Runtime(),  # type: ignore[arg-type]
            resource_ownership=ownership,
        ),
        agent_factory=agent_factory,
        graph_input={},
        config={},
    )

    assert outcome.status == "succeeded"
    assert ownership.transferred is True
    assert events.index("authority:finalize") < events.index("authority:release")
    assert events.index("authority:release") < events.index("runtime:close")
    assert events.index("runtime:close") < events.index("stream:end")


@pytest.mark.asyncio
async def test_loop_capped_lead_run_finalizes_and_skips_hidden_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    published: list[tuple[str, object]] = []
    evaluator = AsyncMock(
        side_effect=AssertionError("goal continuation must not start after loop cap"),
    )
    monkeypatch.setattr(run_worker, "_prepare_goal_continuation_input", evaluator)

    class Authority:
        async def restore(self) -> object:
            return object()

        async def finalize(self) -> object:
            events.append("authority:finalize")
            return SimpleNamespace(workspace_changes=None, artifacts=())

        async def output_delivery_status(self) -> str:
            raise AssertionError("loop-capped Run does not settle as successful output")

        async def mark_failed(self) -> None:
            events.append("authority:failed")

        async def release(self) -> None:
            events.append("authority:release")

    class Agent:
        async def astream(self, *_args, **kwargs):
            config = kwargs["config"]
            recorder = config["context"][RuntimeContextKeys.RUN_SEMANTIC_STOP_RECORDER]
            assert isinstance(recorder, RunSemanticStopRecorder)
            recorder.record("loop_capped")
            yield {
                "messages": [
                    AIMessage(
                        content=("Stopped at the web-search safety limit; collected results are incomplete."),
                    ),
                ],
            }

    class Bridge:
        async def publish(self, _run_id: str, event: str, payload: object) -> None:
            published.append((event, payload))

        async def publish_end(self, _run_id: str) -> None:
            events.append("stream:end")

    run_manager = RunManager()
    record = await run_manager.create("loop-capped-thread")

    outcome = await run_agent(
        Bridge(),
        run_manager,
        record,
        ctx=RunContext(checkpointer=None, file_authority=Authority()),
        agent_factory=lambda *, config: Agent(),
        graph_input={},
        config={},
    )

    assert record.status.value == "error"
    assert record.error == "LOOP_SAFETY_LIMIT"
    assert outcome.status == "failed"
    assert outcome.public_error_code == "LOOP_SAFETY_LIMIT"
    assert "authority:failed" not in events
    assert events.index("authority:finalize") < events.index("authority:release")
    assert events.index("authority:release") < events.index("stream:end")
    assert any(event == "values" and "collected results are incomplete" in str(payload) for event, payload in published)
    evaluator.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_failure_precedes_loop_capped_semantic_outcome() -> None:
    class Agent:
        async def astream(self, *_args, **kwargs):
            recorder = kwargs["config"]["context"][RuntimeContextKeys.RUN_SEMANTIC_STOP_RECORDER]
            assert isinstance(recorder, RunSemanticStopRecorder)
            recorder.record("loop_capped")
            yield {
                "messages": [
                    AIMessage(
                        content="The model provider is unavailable.",
                        additional_kwargs={
                            "deerflow_error_fallback": True,
                            "error_reason": "transient",
                            "error_detail": "Connection error.",
                        },
                    ),
                ],
            }

    class Bridge:
        async def publish(self, *_args, **_kwargs) -> None:
            return None

        async def publish_end(self, _run_id: str) -> None:
            return None

    run_manager = RunManager()
    record = await run_manager.create("provider-failed-and-loop-capped-thread")

    outcome = await run_agent(
        Bridge(),
        run_manager,
        record,
        ctx=RunContext(checkpointer=None),
        agent_factory=lambda *, config: Agent(),
        graph_input={},
        config={},
    )

    assert record.status.value == "error"
    assert record.error == "LLM_PROVIDER_UNAVAILABLE"
    assert outcome.status == "failed"
    assert outcome.public_error_code == "LLM_PROVIDER_UNAVAILABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (
            ToolCallControlStateInvalid("checkpoint policy mismatch"),
            "TOOL_CALL_CONTROL_STATE_INVALID",
        ),
        (
            ToolCallControlLoopFinalizationFailed(
                "model attempted another tool call",
            ),
            "LOOP_FINALIZATION_FAILED",
        ),
    ],
)
async def test_tool_call_control_contract_failures_keep_stable_direct_cause(
    failure: RuntimeError,
    expected_code: str,
) -> None:
    published: list[tuple[str, object]] = []

    class Agent:
        async def astream(self, *_args, **_kwargs):
            if False:
                yield None
            raise failure

    class Bridge:
        async def publish(
            self,
            _run_id: str,
            event: str,
            payload: object,
        ) -> None:
            published.append((event, payload))

        async def publish_end(self, _run_id: str) -> None:
            published.append(("end", None))

    run_manager = RunManager()
    record = await run_manager.create(f"{expected_code.lower()}-thread")

    outcome = await run_agent(
        Bridge(),
        run_manager,
        record,
        ctx=RunContext(checkpointer=None),
        agent_factory=lambda *, config: Agent(),
        graph_input={},
        config={},
    )

    assert record.status.value == "error"
    assert record.error == expected_code
    assert outcome.status == "failed"
    assert outcome.public_error_code == expected_code
    assert any(event == "error" and isinstance(payload, dict) and payload.get("name") == expected_code for event, payload in published)
    assert published[-1] == ("end", None)
