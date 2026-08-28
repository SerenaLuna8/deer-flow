"""Definite Provider failure responses must not be recorded as ambiguous.

An HTTP status answer from the Provider is a deterministic outcome: the
request was delivered and no completion was returned. The final request guard
records adapter-proven failure responses as ``ProviderFailedV1`` (not
``ProviderAmbiguousV1``) and propagates the original SDK error together with
adapter-owned retry safety. Gateway-style 502/504 answers stay ambiguous
because an intermediary response cannot prove the upstream did not execute or
bill the request.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, NotRequired
from uuid import UUID

import anthropic
import httpx
import openai
import pytest
from langchain.agents import AgentState, create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

from deerflow.agents.middlewares import (
    llm_error_handling_middleware as llm_error_module,
)
from deerflow.agents.middlewares.llm_error_handling_middleware import (
    LLMErrorHandlingMiddleware,
)
from deerflow.agents.middlewares.provider_request_usage import (
    FinalProviderRequestGuard,
    ProviderDispatchOutcomeAmbiguous,
    ProviderRequestEvidenceObserver,
    build_provider_request_profile,
)
from deerflow.models.provider_outcome import (
    ProviderNoResponseProof,
    classify_provider_failed_response,
    classify_provider_no_response,
)
from deerflow.runtime.context_evidence import (
    ContextSubject,
    ContextWindowGeneration,
    FinalRequestMeasurement,
    ProviderAmbiguityReason,
    ProviderCallIdentity,
    ProviderRetrySafety,
)
from deerflow.runtime.context_keys import RuntimeContextKeys
from deerflow.runtime.recovered_llm_failures import (
    RunRecoveredLLMFailureRecorder,
)


class _RetryState(AgentState):
    provider_request_profile: NotRequired[dict[str, object]]
    provider_request_measurement: NotRequired[dict[str, object]]


class _SequencedProviderModel(BaseChatModel):
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


class _Observer(ProviderRequestEvidenceObserver):
    """Permits a new dispatch only after the previous one reached a terminal."""

    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []
        self.prepared_calls: list[ProviderCallIdentity] = []
        self._open = False

    async def record_request_prepared(
        self,
        measurement: FinalRequestMeasurement,
        /,
    ) -> ProviderCallIdentity:
        if self._open:
            raise AssertionError("An unresolved Provider dispatch must not be retried")
        provider_call = ProviderCallIdentity.derive(
            subject=ContextSubject.lead_thread(thread_id="thread-1"),
            generation=ContextWindowGeneration(
                generation_id=UUID("44444444-4444-4444-8444-444444444444"),
            ),
            source_checkpoint_id="checkpoint-1",
            graph_step="model",
            model_call_ordinal=len(self.prepared_calls),
            request_fingerprint=measurement.request_fingerprint,
        )
        self.prepared_calls.append(provider_call)
        self._open = True
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
        del provider_call
        self._open = False
        self.events.append(("observed", input_tokens))

    async def record_provider_usage_unreported(
        self,
        provider_call: ProviderCallIdentity,
        /,
    ) -> None:
        del provider_call
        self._open = False
        self.events.append(("usage_unreported", None))

    async def record_provider_failed(
        self,
        provider_call: ProviderCallIdentity,
        /,
        *,
        failure_code: str,
        retry_safety: ProviderRetrySafety,
    ) -> None:
        del provider_call
        self._open = False
        self.events.append(("failed", (failure_code, retry_safety)))

    async def record_provider_ambiguous(
        self,
        provider_call: ProviderCallIdentity,
        /,
        *,
        reason: ProviderAmbiguityReason,
    ) -> None:
        del provider_call
        self._open = False
        self.events.append(("ambiguous", reason))


def _retry_middleware() -> LLMErrorHandlingMiddleware:
    from types import SimpleNamespace

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
    observer: _Observer,
    *,
    provider_adapter: str = "openai",
):
    profile = build_provider_request_profile(
        model=model,
        model_name="offline-model",
        provider_adapter=provider_adapter,
        system_prompt="system",
        tools=(),
        supports_vision=False,
    )
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


def _openai_status_error(
    status_code: int,
    *,
    headers: dict[str, str] | None = None,
) -> openai.APIStatusError:
    request = httpx.Request(
        "POST",
        "https://provider.invalid/v1/chat/completions",
    )
    response = httpx.Response(
        status_code,
        request=request,
        headers=headers,
    )
    if status_code == 429:
        return openai.RateLimitError(
            "rate limited",
            response=response,
            body=None,
        )
    if status_code == 401:
        return openai.AuthenticationError(
            "invalid key",
            response=response,
            body=None,
        )
    return openai.APIStatusError(
        f"status {status_code}",
        response=response,
        body=None,
    )


def _anthropic_status_error(
    status_code: int,
    *,
    headers: dict[str, str] | None = None,
) -> anthropic.APIStatusError:
    request = httpx.Request(
        "POST",
        "https://provider.invalid/v1/messages",
    )
    response = httpx.Response(
        status_code,
        request=request,
        headers=headers,
    )
    if status_code == 429:
        return anthropic.RateLimitError(
            "rate limited",
            response=response,
            body=None,
        )
    if status_code == 401:
        return anthropic.AuthenticationError(
            "invalid key",
            response=response,
            body=None,
        )
    if status_code == 500:
        return anthropic.InternalServerError(
            "internal error",
            response=response,
            body=None,
        )
    return anthropic.APIStatusError(
        f"status {status_code}",
        response=response,
        body=None,
    )


def _status_error(
    provider_adapter: str,
    status_code: int,
    *,
    headers: dict[str, str] | None = None,
) -> openai.APIStatusError | anthropic.APIStatusError:
    if provider_adapter == "anthropic":
        return _anthropic_status_error(status_code, headers=headers)
    return _openai_status_error(status_code, headers=headers)


def _connection_error(
    provider_adapter: str,
    cause_type: type[httpx.RequestError] = httpx.ConnectError,
) -> BaseException:
    request = httpx.Request(
        "POST",
        "https://provider.invalid/v1/messages",
    )
    try:
        raise cause_type("connect failed", request=request)
    except httpx.RequestError as cause:
        try:
            error_type = anthropic.APIConnectionError if provider_adapter == "anthropic" else openai.APIConnectionError
            raise error_type(request=request) from cause
        except (anthropic.APIConnectionError, openai.APIConnectionError) as error:
            return error


@pytest.mark.parametrize("provider_adapter", ["openai", "anthropic", "vllm"])
@pytest.mark.parametrize(
    ("status_code", "retry_safe"),
    [
        (429, True),
        (401, False),
        (500, False),
        (502, None),
        (504, None),
    ],
)
def test_classifier_truth_table(
    provider_adapter: str,
    status_code: int,
    retry_safe: bool | None,
) -> None:
    """Result classification and retry safety remain independent dimensions."""

    proof = classify_provider_failed_response(
        provider_adapter=provider_adapter,
        error=_status_error(provider_adapter, status_code),
    )
    if retry_safe is None:
        assert proof is None
        return
    assert proof is not None
    assert proof.failure_code == f"PROVIDER_HTTP_{status_code}"
    assert proof.retry_safe is retry_safe


@pytest.mark.parametrize("provider_adapter", ["openai", "anthropic", "vllm"])
@pytest.mark.parametrize(
    ("cause_type", "failure_code"),
    [
        (httpx.ConnectError, "PROVIDER_CONNECT_FAILED"),
        (httpx.ConnectTimeout, "PROVIDER_CONNECT_TIMEOUT"),
        (httpx.PoolTimeout, "PROVIDER_POOL_TIMEOUT"),
    ],
)
def test_connect_stage_truth_table(
    provider_adapter: str,
    cause_type: type[httpx.RequestError],
    failure_code: str,
) -> None:
    assert classify_provider_no_response(
        provider_adapter=provider_adapter,
        error=_connection_error(provider_adapter, cause_type),
    ) == ProviderNoResponseProof(failure_code=failure_code)


def test_classifier_rejects_sdk_adapter_mismatch_and_non_status_errors() -> None:
    """An SDK object only proves facts for the adapter that owns it."""

    # SDK/adapter mismatches and non-status errors prove nothing.
    assert (
        classify_provider_failed_response(
            provider_adapter="anthropic",
            error=_openai_status_error(429),
        )
        is None
    )
    assert (
        classify_provider_failed_response(
            provider_adapter="openai",
            error=RuntimeError("not a status answer"),
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_adapter", ["openai", "anthropic", "vllm"])
async def test_rate_limit_answer_is_recorded_failed_and_recovered_by_outer_retry(
    monkeypatch: pytest.MonkeyPatch,
    provider_adapter: str,
) -> None:
    waits: list[float] = []

    async def record_sleep(delay: float) -> None:
        waits.append(delay)

    monkeypatch.setattr(llm_error_module.asyncio, "sleep", record_sleep)
    observer = _Observer()
    recorder = RunRecoveredLLMFailureRecorder()
    model = _SequencedProviderModel(
        (
            _status_error(
                provider_adapter,
                429,
                headers={"retry-after-ms": "1750"},
            ),
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

    result = await _agent(
        model,
        observer,
        provider_adapter=provider_adapter,
    ).ainvoke(
        {"messages": [HumanMessage(content="research this")]},
        context={
            "run_id": "run-1",
            RuntimeContextKeys.RECOVERED_LLM_FAILURE_RECORDER: recorder,
        },
    )

    assert result["messages"][-1].content == "completed after retry"
    assert model.attempts == 2
    assert [name for name, _value in observer.events] == [
        "prepared",
        "dispatched",
        "failed",
        "prepared",
        "dispatched",
        "observed",
    ]
    assert observer.events[2][1] == (
        "PROVIDER_HTTP_429",
        ProviderRetrySafety.FAILED_RESPONSE_RETRY_SAFE,
    )
    assert waits == [1.75]
    assert recorder.snapshot()[0] == {
        "attempt": 1,
        "max_attempts": 3,
        "error_code": "LLM_PROVIDER_UNAVAILABLE",
        "reason": "transient",
        "caller": "lead_agent",
        "failure_subtype": "http_status",
        "status_code": 429,
        "disposition": "recovered",
    }


@pytest.mark.asyncio
async def test_retry_safe_rate_limit_does_not_override_nonretryable_quota_policy() -> None:
    request = httpx.Request(
        "POST",
        "https://provider.invalid/v1/chat/completions",
    )
    quota_error = openai.RateLimitError(
        "quota exhausted",
        response=httpx.Response(429, request=request),
        body={"error": {"code": "insufficient_quota"}},
    )
    observer = _Observer()
    model = _SequencedProviderModel(
        (
            quota_error,
            AIMessage(content="must not retry"),
        )
    )

    result = await _agent(model, observer).ainvoke(
        {"messages": [HumanMessage(content="research this")]},
        context={"run_id": "run-1"},
    )

    assert model.attempts == 1
    assert result["messages"][-1].additional_kwargs["error_reason"] == "quota"
    assert observer.events[-1] == (
        "failed",
        (
            "PROVIDER_HTTP_429",
            ProviderRetrySafety.FAILED_RESPONSE_RETRY_SAFE,
        ),
    )


@pytest.mark.asyncio
async def test_unsafe_conflict_answer_does_not_retry_provider() -> None:
    observer = _Observer()
    model = _SequencedProviderModel(
        (
            _openai_status_error(409),
            AIMessage(content="must not retry"),
        )
    )

    result = await _agent(model, observer).ainvoke(
        {"messages": [HumanMessage(content="research this")]},
        context={"run_id": "run-1"},
    )

    assert model.attempts == 1
    assert result["messages"][-1].additional_kwargs["deerflow_error_fallback"] is True
    assert [name for name, _value in observer.events] == [
        "prepared",
        "dispatched",
        "failed",
    ]
    assert observer.events[-1][1] == (
        "PROVIDER_HTTP_409",
        ProviderRetrySafety.UNSAFE,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_adapter", ["openai", "anthropic", "vllm"])
async def test_unsafe_auth_answer_is_known_failed_without_retry(
    provider_adapter: str,
) -> None:
    observer = _Observer()
    model = _SequencedProviderModel(
        (
            _status_error(provider_adapter, 401),
            AIMessage(content="must not retry"),
        )
    )

    result = await _agent(
        model,
        observer,
        provider_adapter=provider_adapter,
    ).ainvoke(
        {"messages": [HumanMessage(content="research this")]},
        context={"run_id": "run-1"},
    )

    terminal = result["messages"][-1]
    assert model.attempts == 1
    assert terminal.additional_kwargs["error_reason"] == "auth"
    assert observer.events[-1] == (
        "failed",
        ("PROVIDER_HTTP_401", ProviderRetrySafety.UNSAFE),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_adapter", ["openai", "anthropic", "vllm"])
async def test_unsafe_500_answer_is_known_failed_without_retry(
    provider_adapter: str,
) -> None:
    observer = _Observer()
    model = _SequencedProviderModel(
        (
            _status_error(provider_adapter, 500),
            AIMessage(content="must not retry"),
        )
    )

    result = await _agent(
        model,
        observer,
        provider_adapter=provider_adapter,
    ).ainvoke(
        {"messages": [HumanMessage(content="research this")]},
        context={"run_id": "run-1"},
    )

    assert model.attempts == 1
    assert result["messages"][-1].additional_kwargs["deerflow_error_fallback"] is True
    assert [name for name, _value in observer.events] == [
        "prepared",
        "dispatched",
        "failed",
    ]
    assert observer.events[-1][1] == (
        "PROVIDER_HTTP_500",
        ProviderRetrySafety.UNSAFE,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_adapter", ["openai", "anthropic", "vllm"])
async def test_connect_stage_failure_is_recorded_and_recovered_by_outer_retry(
    provider_adapter: str,
) -> None:
    observer = _Observer()
    recorder = RunRecoveredLLMFailureRecorder()
    model = _SequencedProviderModel(
        (
            _connection_error(provider_adapter),
            AIMessage(
                content="completed after reconnect",
                usage_metadata={
                    "input_tokens": 9,
                    "output_tokens": 3,
                    "total_tokens": 12,
                },
            ),
        )
    )

    result = await _agent(
        model,
        observer,
        provider_adapter=provider_adapter,
    ).ainvoke(
        {"messages": [HumanMessage(content="research this")]},
        context={
            "run_id": "run-1",
            RuntimeContextKeys.RECOVERED_LLM_FAILURE_RECORDER: recorder,
        },
    )

    assert result["messages"][-1].content == "completed after reconnect"
    assert model.attempts == 2
    assert observer.events[2] == (
        "failed",
        (
            "PROVIDER_CONNECT_FAILED",
            ProviderRetrySafety.NO_RESPONSE_PROVEN,
        ),
    )
    assert recorder.snapshot()[0]["failure_subtype"] == "connection"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_adapter", ["openai", "anthropic", "vllm"])
@pytest.mark.parametrize("status_code", [502, 504])
async def test_gateway_answer_stays_a_single_ambiguous_dispatch(
    provider_adapter: str,
    status_code: int,
) -> None:
    observer = _Observer()
    model = _SequencedProviderModel(
        (
            _status_error(provider_adapter, status_code),
            AIMessage(content="must not retry"),
        )
    )

    with pytest.raises(ProviderDispatchOutcomeAmbiguous):
        await _agent(
            model,
            observer,
            provider_adapter=provider_adapter,
        ).ainvoke(
            {"messages": [HumanMessage(content="research this")]},
            context={"run_id": "run-1"},
        )

    assert model.attempts == 1
    assert [name for name, _value in observer.events] == [
        "prepared",
        "dispatched",
        "ambiguous",
    ]
