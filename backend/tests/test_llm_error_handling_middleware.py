"""Structured classification tests for LLM request failures."""

import logging
import time
from types import SimpleNamespace

import httpx
import pytest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime
from openai import APIStatusError, AuthenticationError

from deerflow.agents.middlewares.llm_error_handling_middleware import (
    LLMErrorHandlingMiddleware,
)
from deerflow.agents.middlewares.provider_request_usage import (
    ProviderRequestProfileDrift,
)
from deerflow.error_codes import CURRENT_UPLOAD_FAILURE_DETAIL
from deerflow.runtime.context_keys import RuntimeContextKeys
from deerflow.runtime.recovered_llm_failures import (
    RECOVERED_LLM_FAILURES_KEY,
    RunRecoveredLLMFailureRecorder,
    read_recovered_llm_failures,
)


def _middleware() -> LLMErrorHandlingMiddleware:
    return LLMErrorHandlingMiddleware(
        app_config=SimpleNamespace(
            circuit_breaker=SimpleNamespace(
                failure_threshold=5,
                recovery_timeout_sec=60,
            )
        )
    )


@pytest.mark.parametrize(
    ("jitter_sample", "expected_waits"),
    [
        (0.0, [0.5, 1.0]),
        (1.0, [1.0, 2.0]),
    ],
)
def test_retry_backoff_uses_injected_bounded_jitter_without_changing_attempts(
    monkeypatch,
    jitter_sample: float,
    expected_waits: list[float],
) -> None:
    middleware = LLMErrorHandlingMiddleware(
        app_config=SimpleNamespace(
            circuit_breaker=SimpleNamespace(
                failure_threshold=5,
                recovery_timeout_sec=60,
            )
        ),
        retry_jitter_source=lambda: jitter_sample,
    )
    waits: list[float] = []
    monkeypatch.setattr(time, "sleep", waits.append)
    attempts = 0
    provider_request = httpx.Request(
        "POST",
        "https://provider.invalid/v1/responses",
    )

    def handler(_request) -> ModelResponse:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ConnectError(
                "secret transport detail",
                request=provider_request,
            )
        return ModelResponse(result=[AIMessage(content="completed")])

    response = middleware.wrap_model_call(object(), handler)  # type: ignore[arg-type]

    assert isinstance(response, ModelResponse)
    assert attempts == 3
    assert waits == expected_waits


def test_structured_retry_after_bypasses_jitter_and_is_recorded_as_http_status(
    monkeypatch,
) -> None:
    def unexpected_jitter() -> float:
        pytest.fail("Retry-After must bypass local jitter")

    middleware = LLMErrorHandlingMiddleware(
        app_config=SimpleNamespace(
            circuit_breaker=SimpleNamespace(
                failure_threshold=5,
                recovery_timeout_sec=60,
            )
        ),
        retry_jitter_source=unexpected_jitter,
    )
    recorder = RunRecoveredLLMFailureRecorder()
    model_request = _run_request(recorder)
    waits: list[float] = []
    monkeypatch.setattr(time, "sleep", waits.append)
    provider_request = httpx.Request(
        "POST",
        "https://provider.invalid/v1/responses",
    )
    response = httpx.Response(
        503,
        request=provider_request,
        headers={"retry-after-ms": "1750"},
    )
    with pytest.raises(httpx.HTTPStatusError) as captured:
        response.raise_for_status()
    attempts = 0

    def handler(_request) -> ModelResponse:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise captured.value
        return ModelResponse(result=[AIMessage(content="completed")])

    middleware.wrap_model_call(model_request, handler)  # type: ignore[arg-type]

    assert attempts == 2
    assert waits == [1.75]
    failure = recorder.snapshot()[0]
    assert failure["failure_subtype"] == "http_status"
    assert failure["status_code"] == 503


def test_subagent_timeout_receipt_uses_closed_safe_attribution() -> None:
    middleware = _middleware()
    middleware.retry_base_delay_ms = 0
    recorder = RunRecoveredLLMFailureRecorder()
    model_request = _run_request(recorder, is_subagent=True)
    provider_request = httpx.Request(
        "POST",
        "https://provider.invalid/v1/responses",
    )
    attempts = 0

    def handler(_request) -> ModelResponse:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout(
                "secret timeout at https://provider.invalid/private",
                request=provider_request,
            )
        return ModelResponse(result=[AIMessage(content="completed")])

    middleware.wrap_model_call(model_request, handler)  # type: ignore[arg-type]

    failure = recorder.snapshot()[0]
    assert failure == {
        "attempt": 1,
        "max_attempts": 3,
        "error_code": "LLM_PROVIDER_UNAVAILABLE",
        "reason": "transient",
        "caller": "subagent",
        "failure_subtype": "timeout",
        "status_code": None,
        "disposition": "recovered",
    }
    assert "secret" not in repr(failure)
    assert "provider.invalid" not in repr(failure)


def _provider_response(status_code: int) -> httpx.Response:
    request = httpx.Request("POST", "https://provider.invalid/v1/responses")
    return httpx.Response(status_code, request=request)


def test_public_provider_request_failure_is_not_converted_to_llm_fallback() -> None:
    error = ProviderRequestProfileDrift("test drift")

    with pytest.raises(ProviderRequestProfileDrift) as caught:
        _middleware().wrap_model_call(
            SimpleNamespace(),
            lambda _request: (_ for _ in ()).throw(error),
        )

    assert caught.value is error


@pytest.mark.asyncio
async def test_async_public_provider_request_failure_is_not_converted_to_llm_fallback() -> None:
    error = ProviderRequestProfileDrift("test drift")

    async def handler(_request):
        raise error

    with pytest.raises(ProviderRequestProfileDrift) as caught:
        await _middleware().awrap_model_call(SimpleNamespace(), handler)

    assert caught.value is error


def _run_request(
    recorder: RunRecoveredLLMFailureRecorder,
    *,
    is_subagent: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        runtime=Runtime(
            context={
                RuntimeContextKeys.RECOVERED_LLM_FAILURE_RECORDER: recorder,
                RuntimeContextKeys.IS_SUBAGENT: is_subagent,
            },
        )
    )


@pytest.mark.parametrize("malformed_error_code", [[], {}])
def test_malformed_recovered_failure_error_code_fails_closed(
    malformed_error_code: object,
) -> None:
    assert (
        read_recovered_llm_failures(
            {
                "schema_version": 2,
                "failures": [
                    {
                        "attempt": 1,
                        "max_attempts": 3,
                        "error_code": malformed_error_code,
                        "reason": "transient",
                        "caller": "lead_agent",
                        "failure_subtype": "connection",
                        "status_code": None,
                        "disposition": "recovered",
                    }
                ],
            }
        )
        == ()
    )


@pytest.mark.parametrize("field", ["caller", "failure_subtype"])
@pytest.mark.parametrize("malformed_value", [[], {}])
def test_malformed_recovered_failure_attribution_fails_closed_without_raising(
    field: str,
    malformed_value: object,
) -> None:
    failure = {
        "attempt": 1,
        "max_attempts": 3,
        "error_code": "LLM_PROVIDER_UNAVAILABLE",
        "reason": "transient",
        "caller": "lead_agent",
        "failure_subtype": "connection",
        "status_code": None,
        "disposition": "recovered",
    }
    failure[field] = malformed_value

    assert (
        read_recovered_llm_failures(
            {
                "schema_version": 2,
                "failures": [failure],
            }
        )
        == ()
    )


def test_recovered_failure_reason_and_error_code_must_match() -> None:
    assert (
        read_recovered_llm_failures(
            {
                "schema_version": 2,
                "failures": [
                    {
                        "attempt": 1,
                        "max_attempts": 3,
                        "error_code": "LLM_PROVIDER_BUSY",
                        "reason": "transient",
                        "caller": "lead_agent",
                        "failure_subtype": "connection",
                        "status_code": None,
                        "disposition": "recovered",
                    }
                ],
            }
        )
        == ()
    )


def test_local_runtime_error_text_does_not_impersonate_provider_authentication() -> None:
    middleware = _middleware()
    exc = RuntimeError("local file is unauthorized because workspace permission is unavailable")

    assert middleware._classify_error(exc) == (False, "generic")
    fallback = middleware._build_user_fallback_message(exc, "generic")
    assert fallback.additional_kwargs["error_code"] == "LLM_REQUEST_FAILED"
    assert "authentication or access is invalid" not in str(fallback.content)


def test_current_upload_error_has_safe_attachment_message_and_typed_contract() -> None:
    middleware = _middleware()
    exc = RuntimeError(CURRENT_UPLOAD_FAILURE_DETAIL)

    assert middleware._classify_error(exc) == (False, "current_upload")
    fallback = middleware._build_user_fallback_message(exc, "current_upload")
    assert fallback.additional_kwargs == {
        "deerflow_error_fallback": True,
        "error_code": "CURRENT_UPLOAD_UNAVAILABLE",
        "error_type": "CURRENT_UPLOAD_UNAVAILABLE",
        "error_reason": "current_upload",
        "error_detail": "CURRENT_UPLOAD_UNAVAILABLE",
    }
    assert fallback.content == ("The current image attachment could not be securely read or validated. Please retry this Run; if the problem continues, remove and attach the image again.")


def test_openai_authentication_error_is_classified_from_provider_structure() -> None:
    middleware = _middleware()
    exc = AuthenticationError(
        "provider rejected credentials",
        response=_provider_response(401),
        body={
            "error": {
                "type": "authentication_error",
                "code": "invalid_api_key",
                "message": "credential rejected",
            }
        },
    )

    assert middleware._classify_error(exc) == (False, "auth")
    fallback = middleware._build_user_fallback_message(exc, "auth")
    assert fallback.additional_kwargs["error_code"] == "LLM_AUTHENTICATION_FAILED"


def test_structured_credits_error_wins_over_openai_401_wrapper() -> None:
    middleware = _middleware()
    exc = AuthenticationError(
        "upstream returned 401",
        response=_provider_response(401),
        body={
            "error": {
                "type": "CreditsError",
                "message": "account credits unavailable",
            }
        },
    )

    assert middleware._classify_error(exc) == (False, "quota")


def test_proxy_transport_error_is_retriable_and_has_safe_network_message() -> None:
    middleware = _middleware()
    request = httpx.Request("POST", "https://provider.invalid/v1/responses")
    exc = httpx.ProxyError("proxy authentication required", request=request)

    assert middleware._classify_error(exc) == (True, "transient")
    fallback = middleware._build_user_fallback_message(exc, "transient")
    assert fallback.additional_kwargs["error_code"] == "LLM_PROVIDER_UNAVAILABLE"
    assert fallback.content == ("The configured LLM provider could not be reached because the model transport or proxy connection failed. Please check the Worker network configuration and try again.")


def test_proxy_407_is_not_misclassified_as_provider_authentication() -> None:
    middleware = _middleware()
    exc = APIStatusError(
        "Proxy Authentication Required",
        response=_provider_response(407),
        body={"error": {"type": "proxy_error", "message": "Proxy Authentication Required"}},
    )

    assert middleware._classify_error(exc) == (True, "transient")


@pytest.mark.parametrize("status_code", [401, 403])
def test_httpx_status_auth_is_classified_structurally(status_code: int) -> None:
    middleware = _middleware()
    response = _provider_response(status_code)

    with pytest.raises(httpx.HTTPStatusError) as captured:
        response.raise_for_status()

    assert middleware._classify_error(captured.value) == (False, "auth")


def test_httpx_proxy_407_remains_transient_before_auth_classification() -> None:
    middleware = _middleware()
    response = _provider_response(407)

    with pytest.raises(httpx.HTTPStatusError) as captured:
        response.raise_for_status()

    assert middleware._classify_error(captured.value) == (True, "transient")


def test_terminal_warning_logs_only_safe_provider_classifiers(caplog) -> None:
    middleware = _middleware()
    middleware.retry_max_attempts = 1
    exc = AuthenticationError(
        "credential secret-value rejected at https://private.provider.invalid/account",
        response=_provider_response(401),
        body={
            "error": {
                "type": "authentication_error",
                "code": "invalid_api_key",
                "message": "raw-body-secret-value",
            }
        },
    )

    with caplog.at_level(
        logging.WARNING,
        logger="deerflow.agents.middlewares.llm_error_handling_middleware",
    ):
        middleware.wrap_model_call(object(), lambda _request: (_ for _ in ()).throw(exc))  # type: ignore[arg-type]

    text = caplog.text
    assert "exception_class=AuthenticationError" in text
    assert "status_code=401" in text
    assert "provider_error_code=invalid_api_key" in text
    assert "provider_error_type=authentication_error" in text
    assert "secret-value" not in text
    assert "private.provider.invalid" not in text
    assert "raw-body" not in text


def test_success_after_retry_records_diagnostic_without_decorating_ai_message() -> None:
    middleware = _middleware()
    middleware.retry_base_delay_ms = 0
    recorder = RunRecoveredLLMFailureRecorder()
    model_request = _run_request(recorder)
    attempts = 0
    request = httpx.Request("POST", "https://provider.invalid/v1/responses")

    def handler(_request) -> ModelResponse:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("connection secret detail", request=request)
        return ModelResponse(result=[AIMessage(content="completed answer")])

    response = middleware.wrap_model_call(model_request, handler)  # type: ignore[arg-type]

    assert isinstance(response, ModelResponse)
    message = response.result[0]
    assert isinstance(message, AIMessage)
    assert RECOVERED_LLM_FAILURES_KEY not in message.additional_kwargs
    assert recorder.snapshot() == (
        {
            "attempt": 1,
            "max_attempts": 3,
            "error_code": "LLM_PROVIDER_UNAVAILABLE",
            "reason": "transient",
            "caller": "lead_agent",
            "failure_subtype": "connection",
            "status_code": None,
            "disposition": "recovered",
        },
    )
    assert "secret" not in str(message.additional_kwargs)


@pytest.mark.anyio
async def test_async_success_after_retry_records_diagnostic_without_decorating_ai_message() -> None:
    middleware = _middleware()
    middleware.retry_base_delay_ms = 0
    recorder = RunRecoveredLLMFailureRecorder()
    model_request = _run_request(recorder)
    attempts = 0
    request = httpx.Request("POST", "https://provider.invalid/v1/responses")

    async def handler(_request) -> ModelResponse:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError(
                "async connection secret detail",
                request=request,
            )
        return ModelResponse(result=[AIMessage(content="completed async answer")])

    response = await middleware.awrap_model_call(model_request, handler)  # type: ignore[arg-type]

    assert isinstance(response, ModelResponse)
    message = response.result[0]
    assert isinstance(message, AIMessage)
    assert RECOVERED_LLM_FAILURES_KEY not in message.additional_kwargs
    assert len(recorder.snapshot()) == 1
    assert "secret" not in str(message.additional_kwargs)


def test_exhausted_retry_does_not_carry_recovered_failure_receipt() -> None:
    middleware = _middleware()
    middleware.retry_base_delay_ms = 0
    middleware.retry_max_attempts = 2
    attempts = 0
    request = httpx.Request("POST", "https://provider.invalid/v1/responses")

    def handler(_request) -> ModelResponse:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError(
            "terminal connection secret detail",
            request=request,
        )

    response = middleware.wrap_model_call(object(), handler)  # type: ignore[arg-type]

    assert attempts == 2
    assert isinstance(response, AIMessage)
    assert response.additional_kwargs["deerflow_error_fallback"] is True
    assert RECOVERED_LLM_FAILURES_KEY not in response.additional_kwargs


def test_terminal_fallback_does_not_project_prior_run_recovered_failures() -> None:
    middleware = _middleware()
    middleware.retry_base_delay_ms = 0
    middleware.retry_max_attempts = 2
    recorder = RunRecoveredLLMFailureRecorder()
    model_request = _run_request(recorder)
    provider_request = httpx.Request(
        "POST",
        "https://provider.invalid/v1/responses",
    )
    recovered_attempts = 0

    def recovered_handler(_request) -> ModelResponse:
        nonlocal recovered_attempts
        recovered_attempts += 1
        if recovered_attempts == 1:
            raise httpx.ConnectError(
                "prior recovered secret",
                request=provider_request,
            )
        return ModelResponse(result=[AIMessage(content="intermediate")])

    middleware.wrap_model_call(model_request, recovered_handler)  # type: ignore[arg-type]

    terminal = middleware.wrap_model_call(
        model_request,  # type: ignore[arg-type]
        lambda _request: (_ for _ in ()).throw(
            httpx.ConnectError(
                "terminal secret",
                request=provider_request,
            )
        ),
    )

    assert isinstance(terminal, AIMessage)
    assert RECOVERED_LLM_FAILURES_KEY not in terminal.additional_kwargs
    assert len(recorder.snapshot()) == 1


@pytest.mark.anyio
async def test_async_terminal_fallback_does_not_project_prior_run_recovered_failures() -> None:
    middleware = _middleware()
    middleware.retry_base_delay_ms = 0
    middleware.retry_max_attempts = 2
    recorder = RunRecoveredLLMFailureRecorder()
    model_request = _run_request(recorder)
    provider_request = httpx.Request(
        "POST",
        "https://provider.invalid/v1/responses",
    )
    recovered_attempts = 0

    async def recovered_handler(_request) -> ModelResponse:
        nonlocal recovered_attempts
        recovered_attempts += 1
        if recovered_attempts == 1:
            raise httpx.ConnectError(
                "prior async recovered secret",
                request=provider_request,
            )
        return ModelResponse(result=[AIMessage(content="intermediate")])

    await middleware.awrap_model_call(  # type: ignore[arg-type]
        model_request,
        recovered_handler,
    )

    async def terminal_handler(_request) -> ModelResponse:
        raise httpx.ConnectError(
            "terminal async secret",
            request=provider_request,
        )

    terminal = await middleware.awrap_model_call(  # type: ignore[arg-type]
        model_request,
        terminal_handler,
    )

    assert isinstance(terminal, AIMessage)
    assert RECOVERED_LLM_FAILURES_KEY not in terminal.additional_kwargs
    assert len(recorder.snapshot()) == 1


def test_circuit_open_fallback_does_not_project_prior_run_recovered_failures() -> None:
    middleware = _middleware()
    middleware.retry_base_delay_ms = 0
    recorder = RunRecoveredLLMFailureRecorder()
    model_request = _run_request(recorder)
    provider_request = httpx.Request(
        "POST",
        "https://provider.invalid/v1/responses",
    )
    attempts = 0

    def recovered_handler(_request) -> ModelResponse:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError(
                "prior circuit secret",
                request=provider_request,
            )
        return ModelResponse(result=[AIMessage(content="intermediate")])

    middleware.wrap_model_call(model_request, recovered_handler)  # type: ignore[arg-type]
    with middleware._circuit_lock:
        middleware._circuit_state = "open"
        middleware._circuit_open_until = time.time() + 60

    terminal = middleware.wrap_model_call(  # type: ignore[arg-type]
        model_request,
        lambda _request: pytest.fail("open circuit must not call handler"),
    )

    assert isinstance(terminal, AIMessage)
    assert terminal.additional_kwargs["error_reason"] == "circuit_open"
    assert RECOVERED_LLM_FAILURES_KEY not in terminal.additional_kwargs
    assert len(recorder.snapshot()) == 1
