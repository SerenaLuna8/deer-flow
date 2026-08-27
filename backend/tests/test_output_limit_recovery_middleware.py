from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware.types import (
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.runtime import Runtime

from deerflow.agents.middlewares.output_limit_recovery_middleware import (
    OUTPUT_LIMIT_RECOVERY_STATE_KEY,
    OutputLimitRecoveryMiddleware,
)
from deerflow.agents.middlewares.token_budget_middleware import (
    OUTPUT_LIMIT_BUDGET_HARD_STOP_STATE_KEY,
    TOKEN_BUDGET_STATUS_KEY,
    TokenBudgetMiddleware,
)
from deerflow.agents.thread_state import (
    get_thread_state_schema,
    normalize_middleware_state_schemas,
)
from deerflow.config.token_budget_config import TokenBudgetConfig
from deerflow.error_codes import PublicRunError, PublicRunErrorCode
from deerflow.runtime.context_keys import RuntimeContextKeys
from deerflow.runtime.runs.execution_contracts import RunSemanticStopRecorder
from deerflow.runtime.serialization import serialize_channel_values


class _ToolBindingFakeModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def _model(responses: Iterable[AIMessage]) -> _ToolBindingFakeModel:
    return _ToolBindingFakeModel(messages=iter(responses))


def _request(
    *,
    model=None,
    messages: list[Any] | None = None,
    state: dict[str, Any] | None = None,
    response_format=None,
    context: dict[str, Any] | None = None,
) -> ModelRequest:
    return ModelRequest(
        model=model or _model([AIMessage(content="unused")]),
        messages=messages or [HumanMessage(content="question")],
        tools=[{"type": "function", "function": {"name": "probe"}}],
        tool_choice="auto",
        response_format=response_format,
        state=state or {},
        runtime=Runtime(context=context or {"run_id": "run-1"}),
        model_settings={"temperature": 0.7},
    )


def _facts(result: ExtendedModelResponse) -> dict[str, Any]:
    assert result.command is not None
    assert isinstance(result.command.update, dict)
    return result.command.update[OUTPUT_LIMIT_RECOVERY_STATE_KEY]


def test_length_response_checkpoints_private_pending_facts() -> None:
    middleware = OutputLimitRecoveryMiddleware(recovery_model=_model([AIMessage(content="recovered")]))
    result = middleware.wrap_model_call(
        _request(),
        lambda _request: ModelResponse(
            result=[
                AIMessage(
                    content="partial",
                    response_metadata={"finish_reason": "max_tokens"},
                )
            ]
        ),
    )

    assert isinstance(result, ExtendedModelResponse)
    observed = _facts(result)
    assert observed == {
        "version": 1,
        "run_id": "run-1",
        "phase": "initial_observed",
        "limit_hit": True,
        "safe": True,
        "visible": True,
    }
    decision = middleware.after_model(
        {"messages": [], OUTPUT_LIMIT_RECOVERY_STATE_KEY: observed},
        Runtime(context={"run_id": "run-1"}),
    )
    assert decision is not None
    assert decision["jump_to"] == "model"
    assert decision[OUTPUT_LIMIT_RECOVERY_STATE_KEY]["phase"] == "pending"


def test_new_middleware_instance_resumes_pending_without_persisting_prompt() -> None:
    recovery_model = _model([AIMessage(content="unused")])
    middleware = OutputLimitRecoveryMiddleware(recovery_model=recovery_model)
    pending = {
        "version": 1,
        "run_id": "run-1",
        "phase": "pending",
        "limit_hit": True,
        "safe": True,
        "visible": False,
    }
    truncated = AIMessage(
        content=[
            {"type": "reasoning", "reasoning": "private chain"},
            {"type": "text", "text": "partial"},
        ],
        additional_kwargs={"reasoning_content": "private chain"},
    )
    captured: list[ModelRequest] = []

    result = middleware.wrap_model_call(
        _request(
            messages=[HumanMessage(content="question"), truncated],
            state={OUTPUT_LIMIT_RECOVERY_STATE_KEY: pending},
        ),
        lambda request: captured.append(request) or ModelResponse(result=[AIMessage(content="complete answer")]),
    )

    assert isinstance(result, ExtendedModelResponse)
    assert captured[0].model is recovery_model
    assert captured[0].tools == []
    assert captured[0].tool_choice is None
    assert captured[0].response_format is None
    assert captured[0].model_settings == {}
    sent_truncated = captured[0].messages[-2]
    assert isinstance(sent_truncated, AIMessage)
    assert "reasoning_content" not in sent_truncated.additional_kwargs
    assert sent_truncated.content == [{"type": "text", "text": "partial"}]
    hidden = captured[0].messages[-1]
    assert isinstance(hidden, HumanMessage)
    assert hidden.additional_kwargs["hide_from_ui"] is True
    assert hidden not in _request().messages

    observed = _facts(result)
    decision = middleware.after_model(
        {"messages": [], OUTPUT_LIMIT_RECOVERY_STATE_KEY: observed},
        Runtime(context={"run_id": "run-1"}),
    )
    assert decision == {
        OUTPUT_LIMIT_RECOVERY_STATE_KEY: None,
        "jump_to": "end",
    }


@pytest.mark.parametrize(
    "second",
    [
        AIMessage(content="", response_metadata={"finish_reason": "length"}),
        AIMessage(content=""),
        AIMessage(content="", additional_kwargs={"reasoning_content": "thought"}),
        AIMessage(
            content="",
            tool_calls=[{"name": "probe", "args": {}, "id": "new-call"}],
        ),
    ],
)
def test_second_incomplete_or_nonvisible_response_is_typed_failure(
    second: AIMessage,
) -> None:
    middleware = OutputLimitRecoveryMiddleware(recovery_model=_model([AIMessage(content="unused")]))
    pending = {
        "version": 1,
        "run_id": "run-1",
        "phase": "pending",
        "limit_hit": True,
        "safe": True,
        "visible": False,
    }
    result = middleware.wrap_model_call(
        _request(state={OUTPUT_LIMIT_RECOVERY_STATE_KEY: pending}),
        lambda _request: ModelResponse(result=[second]),
    )
    assert isinstance(result, ExtendedModelResponse)

    with pytest.raises(PublicRunError) as raised:
        middleware.after_model(
            {
                "messages": [],
                OUTPUT_LIMIT_RECOVERY_STATE_KEY: _facts(result),
            },
            Runtime(context={"run_id": "run-1"}),
        )
    assert raised.value.code is PublicRunErrorCode.MODEL_OUTPUT_LIMIT


def test_current_truncated_tool_intent_fails_without_recovery() -> None:
    middleware = OutputLimitRecoveryMiddleware(recovery_model=_model([AIMessage(content="unused")]))
    result = middleware.wrap_model_call(
        _request(),
        lambda _request: ModelResponse(
            result=[
                AIMessage(
                    content="partial",
                    response_metadata={"finish_reason": "length"},
                    additional_kwargs={"function_call": {"name": "probe", "arguments": "{}"}},
                )
            ]
        ),
    )
    assert isinstance(result, ExtendedModelResponse)
    with pytest.raises(PublicRunError):
        middleware.after_model(
            {
                "messages": [],
                OUTPUT_LIMIT_RECOVERY_STATE_KEY: _facts(result),
            },
            Runtime(context={"run_id": "run-1"}),
        )


def test_unsafe_output_limit_records_terminal_receipt_before_after_model() -> None:
    recorder = RunSemanticStopRecorder()
    middleware = OutputLimitRecoveryMiddleware(
        recovery_model=_model([AIMessage(content="unused")]),
    )
    result = middleware.wrap_model_call(
        _request(
            context={
                "run_id": "run-1",
                RuntimeContextKeys.RUN_SEMANTIC_STOP_RECORDER: recorder,
            }
        ),
        lambda _request: ModelResponse(
            result=[
                AIMessage(
                    content="",
                    invalid_tool_calls=[
                        {
                            "name": "task",
                            "args": '{"description":"partial',
                            "id": "truncated-task",
                            "error": "invalid json",
                            "type": "invalid_tool_call",
                        }
                    ],
                    response_metadata={"finish_reason": "max_tokens"},
                )
            ]
        ),
    )

    assert isinstance(result, ExtendedModelResponse)
    assert recorder.reason == "model_output_limit"


def test_safe_recoverable_output_limit_does_not_record_terminal_receipt() -> None:
    recorder = RunSemanticStopRecorder()
    middleware = OutputLimitRecoveryMiddleware(
        recovery_model=_model([AIMessage(content="unused")]),
    )
    result = middleware.wrap_model_call(
        _request(
            context={
                "run_id": "run-1",
                RuntimeContextKeys.RUN_SEMANTIC_STOP_RECORDER: recorder,
            }
        ),
        lambda _request: ModelResponse(
            result=[
                AIMessage(
                    content="partial",
                    response_metadata={"finish_reason": "max_tokens"},
                )
            ]
        ),
    )

    assert isinstance(result, ExtendedModelResponse)
    assert recorder.reason is None


def test_required_tool_choice_length_fails_without_recovery() -> None:
    middleware = OutputLimitRecoveryMiddleware(recovery_model=_model([AIMessage(content="unused")]))
    request = _request()
    request = request.override(tool_choice="required")
    result = middleware.wrap_model_call(
        request,
        lambda _request: ModelResponse(
            result=[
                AIMessage(
                    content="partial",
                    response_metadata={"finish_reason": "length"},
                )
            ]
        ),
    )
    assert isinstance(result, ExtendedModelResponse)
    with pytest.raises(PublicRunError):
        middleware.after_model(
            {
                "messages": [],
                OUTPUT_LIMIT_RECOVERY_STATE_KEY: _facts(result),
            },
            Runtime(context={"run_id": "run-1"}),
        )


def test_length_response_with_extra_tool_message_fails_closed() -> None:
    middleware = OutputLimitRecoveryMiddleware(recovery_model=_model([AIMessage(content="unused")]))
    result = middleware.wrap_model_call(
        _request(),
        lambda _request: ModelResponse(
            result=[
                AIMessage(
                    content="partial",
                    response_metadata={"finish_reason": "length"},
                ),
                ToolMessage(content="raw result", tool_call_id="call-1"),
            ]
        ),
    )
    assert isinstance(result, ExtendedModelResponse)
    with pytest.raises(PublicRunError):
        middleware.after_model(
            {
                "messages": [],
                OUTPUT_LIMIT_RECOVERY_STATE_KEY: _facts(result),
            },
            Runtime(context={"run_id": "run-1"}),
        )


def test_token_budget_hard_stop_wins_and_does_not_schedule_recovery() -> None:
    middleware = OutputLimitRecoveryMiddleware(
        recovery_model=_model([AIMessage(content="must not run")]),
        budget_hard_stopped=lambda run_id: run_id == "run-1",
    )
    result = middleware.wrap_model_call(
        _request(),
        lambda _request: ModelResponse(
            result=[
                AIMessage(
                    content="partial",
                    response_metadata={"finish_reason": "length"},
                )
            ]
        ),
    )
    assert isinstance(result, ExtendedModelResponse)
    assert middleware.after_model(
        {"messages": [], OUTPUT_LIMIT_RECOVERY_STATE_KEY: _facts(result)},
        Runtime(context={"run_id": "run-1"}),
    ) == {
        OUTPUT_LIMIT_RECOVERY_STATE_KEY: None,
        OUTPUT_LIMIT_BUDGET_HARD_STOP_STATE_KEY: None,
        "jump_to": "end",
    }


def test_new_middleware_instance_honors_checkpointed_budget_hard_stop() -> None:
    middleware = OutputLimitRecoveryMiddleware(recovery_model=_model([AIMessage(content="must not run")]))
    observed = {
        "version": 1,
        "run_id": "run-1",
        "phase": "initial_observed",
        "limit_hit": True,
        "safe": True,
        "visible": True,
    }

    assert middleware.after_model(
        {
            "messages": [],
            OUTPUT_LIMIT_RECOVERY_STATE_KEY: observed,
            OUTPUT_LIMIT_BUDGET_HARD_STOP_STATE_KEY: {"run_id": "run-1"},
        },
        Runtime(context={"run_id": "run-1"}),
    ) == {
        OUTPUT_LIMIT_RECOVERY_STATE_KEY: None,
        OUTPUT_LIMIT_BUDGET_HARD_STOP_STATE_KEY: None,
        "jump_to": "end",
    }


def test_unobserved_pending_provider_fallback_ends_before_todo_can_retry() -> None:
    middleware = OutputLimitRecoveryMiddleware(recovery_model=_model([AIMessage(content="unused")]))
    pending = {
        "version": 1,
        "run_id": "run-1",
        "phase": "pending",
        "limit_hit": True,
        "safe": True,
        "visible": True,
    }
    fallback = AIMessage(
        content="provider unavailable",
        additional_kwargs={"deerflow_error_fallback": True},
    )

    assert middleware.after_model(
        {
            "messages": [fallback],
            "todos": [{"content": "unfinished", "status": "pending"}],
            OUTPUT_LIMIT_RECOVERY_STATE_KEY: pending,
        },
        Runtime(context={"run_id": "run-1"}),
    ) == {OUTPUT_LIMIT_RECOVERY_STATE_KEY: None, "jump_to": "end"}


def test_private_recovery_state_is_removed_from_public_values() -> None:
    assert serialize_channel_values(
        {
            "messages": [],
            OUTPUT_LIMIT_RECOVERY_STATE_KEY: {"phase": "pending"},
            OUTPUT_LIMIT_BUDGET_HARD_STOP_STATE_KEY: {"run_id": "run-1"},
        }
    ) == {"messages": []}


def test_historical_paired_tool_is_not_reexecuted_during_recovery() -> None:
    calls: list[str] = []

    @tool
    def lookup(value: str) -> str:
        """Return a deterministic lookup value."""

        calls.append(value)
        return f"result:{value}"

    main_model = _model(
        [
            AIMessage(
                content="",
                tool_calls=[{"name": "lookup", "args": {"value": "once"}, "id": "call-1"}],
            ),
            AIMessage(
                content="partial summary",
                response_metadata={"finish_reason": "length"},
            ),
        ]
    )
    recovery_model = _model([AIMessage(content="complete summary")])
    agent = create_agent(
        model=main_model,
        tools=[lookup],
        middleware=[OutputLimitRecoveryMiddleware(recovery_model=recovery_model)],
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="look it up")]},
        context={"run_id": "run-1"},
    )

    assert calls == ["once"]
    assert result["messages"][-1].content == "complete summary"
    assert not any(isinstance(message, HumanMessage) and message.name == "output_limit_recovery" for message in result["messages"])
    assert sum(isinstance(message, ToolMessage) for message in result["messages"]) == 1


def test_recovery_and_token_budget_state_schemas_compile_together() -> None:
    create_agent(
        model=_model([AIMessage(content="done")]),
        middleware=[
            OutputLimitRecoveryMiddleware(recovery_model=_model([AIMessage(content="recovered")])),
            TokenBudgetMiddleware.from_config(TokenBudgetConfig(enabled=True, max_tokens=1_000)),
        ],
    )


def test_token_budget_without_recovery_accepts_its_private_marker_channel() -> None:
    middleware = TokenBudgetMiddleware.from_config(
        TokenBudgetConfig(
            enabled=True,
            max_tokens=1_000,
            warn_threshold=0,
            hard_stop_threshold=0,
        )
    )
    model = _model(
        [
            AIMessage(
                content="budget answer",
                usage_metadata={
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "total_tokens": 2,
                },
            )
        ]
    )
    agent = create_agent(model=model, middleware=[middleware])

    result = agent.invoke(
        {"messages": [HumanMessage(content="question")]},
        context={"run_id": "budget-run"},
    )

    assert OUTPUT_LIMIT_BUDGET_HARD_STOP_STATE_KEY not in result
    final_message = result["messages"][-1]
    assert final_message.content == "budget answer"
    assert "TOKEN BUDGET EXCEEDED" not in str(final_message.content)
    assert final_message.response_metadata[TOKEN_BUDGET_STATUS_KEY] == {
        "version": 1,
        "status": "exceeded",
        "reason": "total",
    }


def _checkpoint_agent(
    *,
    saver: InMemorySaver,
    mode: str,
    main: AIMessage,
    recovery: AIMessage,
    interrupt_before: list[str] | None = None,
    interrupt_after: list[str] | None = None,
):
    middleware = OutputLimitRecoveryMiddleware(recovery_model=_model([recovery]))
    return create_agent(
        model=_model([main]),
        middleware=normalize_middleware_state_schemas([middleware], mode),
        state_schema=get_thread_state_schema(mode),
        checkpointer=saver,
        interrupt_before=interrupt_before,
        interrupt_after=interrupt_after,
    )


@pytest.mark.parametrize("mode", ["full", "delta"])
def test_checkpoint_takeover_runs_exactly_one_recovery_and_no_third_call(
    mode: str,
) -> None:
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": f"checkpoint-{mode}"}}
    context = {"run_id": "run-1"}
    first = _checkpoint_agent(
        saver=saver,
        mode=mode,
        main=AIMessage(
            content="partial",
            response_metadata={"finish_reason": "length"},
        ),
        recovery=AIMessage(content="must not run"),
        interrupt_after=["model"],
    )
    first.invoke(
        {"messages": [HumanMessage(content="question")]},
        config,
        context=context,
    )
    assert first.get_state(config).values[OUTPUT_LIMIT_RECOVERY_STATE_KEY]["phase"] == "initial_observed"

    pending = _checkpoint_agent(
        saver=saver,
        mode=mode,
        main=AIMessage(content="must not run"),
        recovery=AIMessage(content="must not run"),
        interrupt_before=["model"],
    )
    pending.invoke(None, config, context=context)
    assert pending.get_state(config).values[OUTPUT_LIMIT_RECOVERY_STATE_KEY]["phase"] == "pending"

    recovered = _checkpoint_agent(
        saver=saver,
        mode=mode,
        main=AIMessage(content="must not run"),
        recovery=AIMessage(content="complete answer"),
        interrupt_after=["model"],
    )
    recovered.invoke(None, config, context=context)
    assert recovered.get_state(config).values[OUTPUT_LIMIT_RECOVERY_STATE_KEY]["phase"] == "recovery_observed"

    completed = _checkpoint_agent(
        saver=saver,
        mode=mode,
        main=AIMessage(content="must not run"),
        recovery=AIMessage(content="must not run"),
    )
    result = completed.invoke(None, config, context=context)
    assert [message.content for message in result["messages"]] == [
        "question",
        "partial",
        "complete answer",
    ]
    assert completed.get_state(config).next == ()


@pytest.mark.parametrize("mode", ["full", "delta"])
def test_checkpoint_takeover_preserves_second_limit_typed_failure(mode: str) -> None:
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": f"checkpoint-fail-{mode}"}}
    context = {"run_id": "run-1"}
    first = _checkpoint_agent(
        saver=saver,
        mode=mode,
        main=AIMessage(
            content="partial",
            response_metadata={"finish_reason": "length"},
        ),
        recovery=AIMessage(content="unused"),
        interrupt_after=["model"],
    )
    first.invoke(
        {"messages": [HumanMessage(content="question")]},
        config,
        context=context,
    )
    pending = _checkpoint_agent(
        saver=saver,
        mode=mode,
        main=AIMessage(content="must not run"),
        recovery=AIMessage(
            content="still partial",
            response_metadata={"finish_reason": "max_output_tokens"},
        ),
        interrupt_after=["model"],
    )
    pending.invoke(None, config, context=context)
    resumed = _checkpoint_agent(
        saver=saver,
        mode=mode,
        main=AIMessage(content="must not run"),
        recovery=AIMessage(content="must not run"),
    )

    with pytest.raises(PublicRunError) as raised:
        resumed.invoke(None, config, context=context)
    assert raised.value.code is PublicRunErrorCode.MODEL_OUTPUT_LIMIT


def test_stale_recovery_state_from_another_run_is_ignored() -> None:
    middleware = OutputLimitRecoveryMiddleware(recovery_model=_model([AIMessage(content="must not run")]))
    stale = {
        "version": 1,
        "run_id": "old-run",
        "phase": "pending",
        "limit_hit": True,
        "safe": True,
        "visible": True,
    }
    request = _request(state={OUTPUT_LIMIT_RECOVERY_STATE_KEY: stale})
    result = middleware.wrap_model_call(
        request,
        lambda effective: ModelResponse(result=[AIMessage(content=("main model" if effective.model is request.model else "recovery model"))]),
    )
    assert isinstance(result, ModelResponse)
    assert result.result[0].content == "main model"
