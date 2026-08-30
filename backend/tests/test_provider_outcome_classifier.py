from __future__ import annotations

import httpx
import pytest
from openai import APIConnectionError, APITimeoutError

from deerflow.models.provider_outcome import (
    ProviderNoResponseProof,
    ProviderNoResponseProvenError,
    classify_provider_no_response,
)

_OPENAI_FAMILY_ADAPTERS = (
    "deepseek",
    "openai",
    "openai_responses",
)


def test_provider_no_response_marker_owns_one_closed_failure_code() -> None:
    error = ProviderNoResponseProvenError(
        failure_code="PROVIDER_CONNECT_FAILED",
    )

    assert error.failure_code == "PROVIDER_CONNECT_FAILED"
    with pytest.raises(ValueError, match="provider failure code is invalid"):
        ProviderNoResponseProvenError(failure_code="raw provider detail")


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://provider.invalid/v1/chat/completions")


def _openai_connection_error(cause: Exception) -> APIConnectionError:
    try:
        raise cause
    except Exception as error:
        try:
            raise APIConnectionError(request=_request()) from error
        except APIConnectionError as wrapped:
            return wrapped


def _openai_timeout_error(cause: httpx.TimeoutException) -> APITimeoutError:
    try:
        raise cause
    except httpx.TimeoutException as error:
        try:
            raise APITimeoutError(request=_request()) from error
        except APITimeoutError as wrapped:
            return wrapped


@pytest.mark.parametrize("provider_adapter", _OPENAI_FAMILY_ADAPTERS)
@pytest.mark.parametrize(
    ("cause_type", "failure_code"),
    [
        (httpx.ConnectError, "PROVIDER_CONNECT_FAILED"),
        (httpx.ConnectTimeout, "PROVIDER_CONNECT_TIMEOUT"),
        (httpx.PoolTimeout, "PROVIDER_POOL_TIMEOUT"),
    ],
)
@pytest.mark.parametrize("wrapped", [False, True])
def test_openai_family_connect_stage_proves_no_provider_response(
    provider_adapter: str,
    cause_type: type[httpx.RequestError],
    failure_code: str,
    wrapped: bool,
) -> None:
    cause = cause_type("connect stage failed", request=_request())
    error = _openai_connection_error(cause) if wrapped else cause

    assert classify_provider_no_response(
        provider_adapter=provider_adapter,
        error=error,
    ) == ProviderNoResponseProof(failure_code=failure_code)


@pytest.mark.parametrize(
    ("cause_type", "failure_code"),
    [
        (httpx.ConnectTimeout, "PROVIDER_CONNECT_TIMEOUT"),
        (httpx.PoolTimeout, "PROVIDER_POOL_TIMEOUT"),
    ],
)
def test_openai_timeout_wrapper_preserves_connect_stage_proof(
    cause_type: type[httpx.TimeoutException],
    failure_code: str,
) -> None:
    assert issubclass(APITimeoutError, APIConnectionError)
    cause = cause_type("connect stage timeout", request=_request())
    error = _openai_timeout_error(cause)

    assert error.__cause__ is cause
    assert classify_provider_no_response(
        provider_adapter="deepseek",
        error=error,
    ) == ProviderNoResponseProof(failure_code=failure_code)


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadError("read failed", request=_request()),
        httpx.WriteError("write failed", request=_request()),
        httpx.RemoteProtocolError("protocol failed", request=_request()),
        _openai_connection_error(
            httpx.ReadError("read failed", request=_request()),
        ),
        _openai_connection_error(
            httpx.WriteError("write failed", request=_request()),
        ),
        _openai_connection_error(
            httpx.RemoteProtocolError(
                "protocol failed",
                request=_request(),
            ),
        ),
        _openai_timeout_error(
            httpx.ReadTimeout("read timeout", request=_request()),
        ),
        _openai_timeout_error(
            httpx.WriteTimeout("write timeout", request=_request()),
        ),
        APIConnectionError(request=_request()),
    ],
)
def test_provider_failure_without_connect_stage_proof_remains_unknown(
    error: Exception,
) -> None:
    assert (
        classify_provider_no_response(
            provider_adapter="deepseek",
            error=error,
        )
        is None
    )


@pytest.mark.parametrize("provider_adapter", ["anthropic", "vllm", None])
def test_non_openai_family_adapter_cannot_reuse_openai_transport_proof(
    provider_adapter: str | None,
) -> None:
    assert (
        classify_provider_no_response(
            provider_adapter=provider_adapter,
            error=httpx.ConnectError(
                "connect failed",
                request=_request(),
            ),
        )
        is None
    )
