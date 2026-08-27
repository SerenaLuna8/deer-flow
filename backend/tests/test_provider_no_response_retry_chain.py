"""Offline contract for Provider retry-safety across the real middleware order."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any, NotRequired
from uuid import UUID

import httpx
import pytest
from langchain.agents import AgentState, create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from openai import APIConnectionError
from pydantic import PrivateAttr

from deerflow.agents.middlewares.llm_error_handling_middleware import (
    LLMErrorHandlingMiddleware,
)
from deerflow.agents.middlewares.provider_request_usage import (
    PROVIDER_REQUEST_MEASUREMENT_STATE_KEY,
    PROVIDER_REQUEST_PROFILE_STATE_KEY,
    FinalProviderRequestGuard,
    ProviderDispatchOutcomeAmbiguous,
    ProviderRequestEvidenceObserver,
    build_provider_request_profile,
)
from deerflow.runtime.context_evidence import (
    ContextSubject,
    ContextWindowGeneration,
    FinalRequestMeasurement,
    ProviderAmbiguityReason,
    ProviderCallIdentity,
    ProviderRetrySafety,
)


class _RetryState(AgentState):
    provider_request_profile: NotRequired[dict[str, object]]
    provider_request_measurement: NotRequired[dict[str, object]]


class _SequencedProviderModel(BaseChatModel):
    """Provider-free model whose outcomes are consumed one invocation at a time."""

    _outcomes: list[BaseException | AIMessage] = PrivateAttr()
    _attempts: int = PrivateAttr(default=0)

    def __init__(self, outcomes: Sequence[BaseException | AIMessage]) -> None:
        super().__init__(profile={"max_input_tokens": 100_000})
        self._outcomes = list(outcomes)

    @property
    def _llm_type(self) -> str:
        return "sequenced-provider-free-model"

    @property
    def attempts(self) -> int:
        return self._attempts

    def bind_tools(self, tools: Sequence[object], **kwargs: Any) -> _SequencedProviderModel:
        del tools, kwargs
        return self

    def _next(self) -> ChatResult:
        self._attempts += 1
        if not self._outcomes:
            raise AssertionError("Provider received an unexpected extra invocation")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return ChatResult(generations=[ChatGeneration(message=outcome)])

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: object | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del messages, stop, run_manager, kwargs
        return self._next()

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: object | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del messages, stop, run_manager, kwargs
        return self._next()


class _RetryAwareObserver(ProviderRequestEvidenceObserver):
    """Small in-memory lifecycle that permits retry only after proven failure."""

    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []
        self.prepared_calls: list[ProviderCallIdentity] = []
        self._last_terminal: str | None = None

    async def record_request_prepared(
        self,
        measurement: FinalRequestMeasurement,
        /,
    ) -> ProviderCallIdentity:
        if self.prepared_calls and self._last_terminal != "failed_no_response":
            raise AssertionError("An unresolved Provider dispatch must not be retried")
        ordinal = len(self.prepared_calls)
        provider_call = ProviderCallIdentity.derive(
            subject=ContextSubject.lead_thread(thread_id="thread-1"),
            generation=ContextWindowGeneration(
                generation_id=UUID("44444444-4444-4444-8444-444444444444"),
            ),
            source_checkpoint_id="checkpoint-1",
            graph_step="model",
            model_call_ordinal=ordinal,
            request_fingerprint=measurement.request_fingerprint,
        )
        self.prepared_calls.append(provider_call)
        self._last_terminal = None
        self.events.append(("prepared", provider_call.provider_call_id))
        return provider_call

    async def record_request_dispatched(
        self,
        provider_call: ProviderCallIdentity,
        /,
    ) -> None:
        self.events.append(("dispatched", provider_call.provider_call_id))

    async def record_provider_observed(
        self,
        provider_call: ProviderCallIdentity,
        /,
        *,
        input_tokens: int,
    ) -> None:
        self._last_terminal = "observed"
        self.events.append(("observed", (provider_call.provider_call_id, input_tokens)))

    async def record_provider_usage_unreported(
        self,
        provider_call: ProviderCallIdentity,
        /,
    ) -> None:
        self._last_terminal = "observed"
        self.events.append(("usage_unreported", provider_call.provider_call_id))

    async def record_provider_failed(
        self,
        provider_call: ProviderCallIdentity,
        /,
        *,
        failure_code: str,
        retry_safety: ProviderRetrySafety,
    ) -> None:
        assert retry_safety is ProviderRetrySafety.NO_RESPONSE_PROVEN
        self._last_terminal = "failed_no_response"
        self.events.append(
            (
                "failed",
                (provider_call.provider_call_id, failure_code, retry_safety),
            )
        )

    async def record_provider_ambiguous(
        self,
        provider_call: ProviderCallIdentity,
        /,
        *,
        reason: ProviderAmbiguityReason,
    ) -> None:
        self._last_terminal = "ambiguous"
        self.events.append(("ambiguous", (provider_call.provider_call_id, reason)))


def _retry_middleware() -> LLMErrorHandlingMiddleware:
    middleware = LLMErrorHandlingMiddleware(
        app_config=SimpleNamespace(
            circuit_breaker=SimpleNamespace(
                failure_threshold=5,
                recovery_timeout_sec=60,
            ),
        ),
        retry_jitter_source=lambda: 0.0,
    )
    middleware.retry_base_delay_ms = 0
    return middleware


def _agent(
    model: _SequencedProviderModel,
    observer: _RetryAwareObserver,
):
    profile = build_provider_request_profile(
        model=model,
        model_name="offline-model",
        provider_adapter="openai",
        system_prompt="system",
        tools=(),
        supports_vision=False,
    )
    # This is the production registration direction: LLM retry is outer and
    # the final request guard is the innermost Provider boundary.
    return create_agent(
        model=model,
        tools=[],
        system_prompt="system",
        middleware=[
            _retry_middleware(),
            FinalProviderRequestGuard(
                profile,
                evidence_observer=observer,
            ),
        ],
        state_schema=_RetryState,
    )


def _api_connection_error_with_connect_proof() -> APIConnectionError:
    request = httpx.Request(
        "POST",
        "https://provider.invalid/v1/chat/completions",
    )
    error = APIConnectionError(request=request)
    error.__cause__ = httpx.ConnectError(
        "offline connection establishment failed",
        request=request,
    )
    return error


@pytest.mark.asyncio
async def test_proven_no_response_retries_with_a_new_provider_call_identity() -> None:
    observer = _RetryAwareObserver()
    model = _SequencedProviderModel(
        (
            _api_connection_error_with_connect_proof(),
            AIMessage(
                content="completed after retry",
                usage_metadata={
                    "input_tokens": 9,
                    "output_tokens": 3,
                    "total_tokens": 12,
                },
            ),
        )
    )

    result = await _agent(model, observer).ainvoke(
        {"messages": [HumanMessage(content="research this")]},
        context={"run_id": "run-1"},
    )

    assert result["messages"][-1].content == "completed after retry"
    assert model.attempts == 2
    assert len(observer.prepared_calls) == 2
    first, second = observer.prepared_calls
    assert first.model_call_ordinal == 0
    assert second.model_call_ordinal == 1
    assert first.provider_call_id != second.provider_call_id
    assert [name for name, _value in observer.events] == [
        "prepared",
        "dispatched",
        "failed",
        "prepared",
        "dispatched",
        "observed",
    ]
    assert observer.events[2][1] == (
        first.provider_call_id,
        "PROVIDER_CONNECT_FAILED",
        ProviderRetrySafety.NO_RESPONSE_PROVEN,
    )
    assert PROVIDER_REQUEST_PROFILE_STATE_KEY in result
    assert PROVIDER_REQUEST_MEASUREMENT_STATE_KEY in result


@pytest.mark.asyncio
async def test_unproven_api_connection_error_remains_single_ambiguous_dispatch() -> None:
    observer = _RetryAwareObserver()
    request = httpx.Request(
        "POST",
        "https://provider.invalid/v1/chat/completions",
    )
    model = _SequencedProviderModel(
        (
            APIConnectionError(request=request),
            AIMessage(content="must not retry"),
        )
    )

    with pytest.raises(ProviderDispatchOutcomeAmbiguous):
        await _agent(model, observer).ainvoke(
            {"messages": [HumanMessage(content="research this")]},
            context={"run_id": "run-1"},
        )

    assert model.attempts == 1
    assert len(observer.prepared_calls) == 1
    assert [name for name, _value in observer.events] == [
        "prepared",
        "dispatched",
        "ambiguous",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "persistence_error",
    [
        RuntimeError("Context Evidence persistence failed"),
        asyncio.CancelledError(),
    ],
)
async def test_proven_no_response_terminal_uncertainty_is_not_retried_or_swallowed(
    persistence_error: BaseException,
) -> None:
    class _TerminalFailureObserver(_RetryAwareObserver):
        async def record_provider_failed(
            self,
            provider_call: ProviderCallIdentity,
            /,
            *,
            failure_code: str,
            retry_safety: ProviderRetrySafety,
        ) -> None:
            del provider_call, failure_code, retry_safety
            raise persistence_error

    observer = _TerminalFailureObserver()
    model = _SequencedProviderModel(
        (
            _api_connection_error_with_connect_proof(),
            AIMessage(content="must not retry"),
        )
    )

    with pytest.raises(ProviderDispatchOutcomeAmbiguous) as caught:
        await _agent(model, observer).ainvoke(
            {"messages": [HumanMessage(content="research this")]},
            context={"run_id": "run-1"},
        )

    assert caught.value.__cause__ is persistence_error
    assert model.attempts == 1
    assert len(observer.prepared_calls) == 1
    assert [name for name, _value in observer.events] == [
        "prepared",
        "dispatched",
    ]
