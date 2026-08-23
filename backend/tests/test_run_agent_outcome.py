"""Contracts for the internal Harness Execution semantic outcome."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage

import deerflow.runtime.runs.worker as run_worker
from deerflow.agents.middlewares.tool_call_control import (
    ToolCallControlLoopFinalizationFailed,
    ToolCallControlStateInvalid,
)
from deerflow.runtime.context_keys import RuntimeContextKeys
from deerflow.runtime.runs.execution_contracts import (
    RunAgentOutcome,
    RunAgentResourceOwnership,
    RunAgentUsageSnapshot,
    RunSemanticStopRecorder,
)
from deerflow.runtime.runs.manager import RunManager
from deerflow.runtime.runs.worker import RunContext, run_agent


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
async def test_run_agent_returns_outcome_after_owned_resources_and_terminal_close() -> None:
    events: list[str] = []

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

        async def release(self) -> None:
            events.append("authority:release")

    class Runtime:
        async def aclose(self) -> None:
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
