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
    RECOVERED_LLM_FAILURES_KEY,
    LLMErrorHandlingMiddleware,
    read_recovered_llm_failures,
)
from deerflow.error_codes import CURRENT_UPLOAD_FAILURE_DETAIL
from deerflow.runtime.context_keys import RuntimeContextKeys
from deerflow.runtime.recovered_llm_failures import (
    RunRecoveredLLMFailureRecorder,
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


def _provider_response(status_code: int) -> httpx.Response:
    request = httpx.Request("POST", "https://provider.invalid/v1/responses")
    return httpx.Response(status_code, request=request)


def _run_request(
    recorder: RunRecoveredLLMFailureRecorder,
) -> SimpleNamespace:
    return SimpleNamespace(
        runtime=Runtime(
            context={
                RuntimeContextKeys.RECOVERED_LLM_FAILURE_RECORDER: recorder,
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
                "schema_version": 1,
                "failures": [
                    {
                        "attempt": 1,
                        "max_attempts": 3,
                        "error_code": malformed_error_code,
                        "reason": "transient",
                        "disposition": "recovered",
                    }
                ],
            }
        )
        == ()
    )


def test_recovered_failure_reason_and_error_code_must_match() -> None:
    assert (
        read_recovered_llm_failures(
            {
                "schema_version": 1,
                "failures": [
                    {
                        "attempt": 1,
                        "max_attempts": 3,
                        "error_code": "LLM_PROVIDER_BUSY",
                        "reason": "transient",
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


def test_success_after_retry_carries_safe_recovered_failure_receipt() -> None:
    middleware = _middleware()
    middleware.retry_base_delay_ms = 0
    attempts = 0
    request = httpx.Request("POST", "https://provider.invalid/v1/responses")

    def handler(_request) -> ModelResponse:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("connection secret detail", request=request)
        return ModelResponse(result=[AIMessage(content="completed answer")])

    response = middleware.wrap_model_call(object(), handler)  # type: ignore[arg-type]

    assert isinstance(response, ModelResponse)
    message = response.result[0]
    assert isinstance(message, AIMessage)
    assert message.additional_kwargs[RECOVERED_LLM_FAILURES_KEY] == {
        "schema_version": 1,
        "failures": [
            {
                "attempt": 1,
                "max_attempts": 3,
                "error_code": "LLM_PROVIDER_UNAVAILABLE",
                "reason": "transient",
                "disposition": "recovered",
            }
        ],
    }
    assert "secret" not in str(message.additional_kwargs)


@pytest.mark.anyio
async def test_async_success_after_retry_carries_safe_recovered_failure_receipt() -> None:
    middleware = _middleware()
    middleware.retry_base_delay_ms = 0
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

    response = await middleware.awrap_model_call(object(), handler)  # type: ignore[arg-type]

    assert isinstance(response, ModelResponse)
    message = response.result[0]
    assert isinstance(message, AIMessage)
    assert message.additional_kwargs[RECOVERED_LLM_FAILURES_KEY] == {
        "schema_version": 1,
        "failures": [
            {
                "attempt": 1,
                "max_attempts": 3,
                "error_code": "LLM_PROVIDER_UNAVAILABLE",
                "reason": "transient",
                "disposition": "recovered",
            }
        ],
    }
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


def test_terminal_fallback_keeps_only_prior_run_recovered_failures() -> None:
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
    failures = terminal.additional_kwargs[RECOVERED_LLM_FAILURES_KEY]["failures"]
    assert len(failures) == 1
    assert failures[0]["disposition"] == "recovered"


@pytest.mark.anyio
async def test_async_terminal_fallback_keeps_prior_run_recovered_failures() -> None:
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
    assert len(terminal.additional_kwargs[RECOVERED_LLM_FAILURES_KEY]["failures"]) == 1


def test_circuit_open_fallback_keeps_prior_run_recovered_failures() -> None:
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
    assert len(terminal.additional_kwargs[RECOVERED_LLM_FAILURES_KEY]["failures"]) == 1
