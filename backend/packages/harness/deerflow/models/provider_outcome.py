"""Provider-adapter proof for known failures and safe retry admission."""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx
from anthropic import APIConnectionError as AnthropicAPIConnectionError
from anthropic import APIStatusError as AnthropicAPIStatusError
from openai import APIConnectionError
from openai import APIStatusError as OpenAIAPIStatusError

_OPENAI_FAMILY_ADAPTERS = frozenset(
    {
        "deepseek",
        "openai",
        "openai_responses",
    }
)
# vLLM is an OpenAI-compatible adapter built on the OpenAI SDK, so its status
# answers carry the same provenance as the OpenAI family.
_OPENAI_SDK_STATUS_ADAPTERS = _OPENAI_FAMILY_ADAPTERS | {"vllm"}
_ANTHROPIC_SDK_STATUS_ADAPTERS = frozenset({"anthropic"})

# Statuses that are a definite Provider failure answer produced without a
# completion. Gateway answers are deliberately absent: an intermediary 502 or
# 504 cannot prove the upstream did not execute or bill the request, so those
# outcomes remain ambiguous.
_DEFINITE_FAILURE_STATUS_CODES = frozenset({400, 401, 403, 404, 409, 413, 422, 429, 500})
# The closed adapter truth table treats these statuses as admission rejection,
# so an identical retry cannot double-execute or double-bill.
_RETRY_SAFE_STATUS_CODES = frozenset({429})

_NO_RESPONSE_FAILURE_CODES: tuple[
    tuple[type[httpx.RequestError], str],
    ...,
] = (
    (httpx.ConnectError, "PROVIDER_CONNECT_FAILED"),
    (httpx.ConnectTimeout, "PROVIDER_CONNECT_TIMEOUT"),
    (httpx.PoolTimeout, "PROVIDER_POOL_TIMEOUT"),
)


@dataclass(frozen=True, slots=True)
class ProviderNoResponseProof:
    """One closed failure code backed by a Provider transport-stage proof."""

    failure_code: str


class ProviderNoResponseProvenError(RuntimeError):
    """Provider-adapter proof that one request produced no response."""

    def __init__(self, *, failure_code: str) -> None:
        if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", failure_code) is None:
            raise ValueError("provider failure code is invalid")
        self.failure_code = failure_code
        super().__init__(failure_code)


@dataclass(frozen=True, slots=True)
class ProviderFailedResponseProof:
    """A definite Provider failure answer plus its adapter-proven retry safety."""

    failure_code: str
    retry_safe: bool


class ProviderFailedResponseError(RuntimeError):
    """A definite failure answer whose retry policy is adapter-owned."""

    def __init__(
        self,
        *,
        proof: ProviderFailedResponseProof,
        provider_error: BaseException,
    ) -> None:
        self.failure_code = proof.failure_code
        self.retry_safe = proof.retry_safe
        self.provider_error = provider_error
        super().__init__(proof.failure_code)


def _failed_response_status(
    provider_adapter: str | None,
    error: BaseException,
) -> int | None:
    if provider_adapter in _OPENAI_SDK_STATUS_ADAPTERS and isinstance(error, OpenAIAPIStatusError):
        status = getattr(error, "status_code", None)
    elif provider_adapter in _ANTHROPIC_SDK_STATUS_ADAPTERS and isinstance(error, AnthropicAPIStatusError):
        status = getattr(error, "status_code", None)
    else:
        return None
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def classify_provider_failed_response(
    *,
    provider_adapter: str | None,
    error: BaseException,
) -> ProviderFailedResponseProof | None:
    """Return proof for a definite Provider failure answer without a completion.

    An HTTP status response means the request was delivered and the Provider
    (or an intermediary) answered. Only the closed status set is treated
    as a definite "no completion" outcome, and only statuses the exact SDK
    family documents as rejected-before-inference are marked retry-safe.
    Everything else, including gateway 502/504 answers, proves nothing and
    stays with the ambiguous-dispatch handling.
    """

    status = _failed_response_status(provider_adapter, error)
    if status is None or status not in _DEFINITE_FAILURE_STATUS_CODES:
        return None
    return ProviderFailedResponseProof(
        failure_code=f"PROVIDER_HTTP_{status}",
        retry_safe=status in _RETRY_SAFE_STATUS_CODES,
    )


def classify_provider_no_response(
    *,
    provider_adapter: str | None,
    error: BaseException,
) -> ProviderNoResponseProof | None:
    """Return proof only for adapter-owned failures before request dispatch.

    The OpenAI and Anthropic SDKs wrap httpx transport failures in their own
    ``APIConnectionError`` and retain the exact transport exception as
    ``__cause__``. A connect or pool-acquisition failure cannot have delivered
    request bytes to the Provider. Read, write, protocol, and unclassified
    connection failures remain unknown.
    """

    if provider_adapter in _OPENAI_FAMILY_ADAPTERS:
        if isinstance(error, APIConnectionError):
            candidate: BaseException | None = error.__cause__
        elif isinstance(error, httpx.RequestError):
            candidate = error
        else:
            return None
    elif provider_adapter == "vllm":
        if not isinstance(error, APIConnectionError):
            return None
        candidate = error.__cause__
    elif provider_adapter in _ANTHROPIC_SDK_STATUS_ADAPTERS:
        if not isinstance(error, AnthropicAPIConnectionError):
            return None
        candidate = error.__cause__
    else:
        return None
    for exception_type, failure_code in _NO_RESPONSE_FAILURE_CODES:
        if isinstance(candidate, exception_type):
            return ProviderNoResponseProof(failure_code=failure_code)
    return None


__all__ = [
    "ProviderFailedResponseError",
    "ProviderFailedResponseProof",
    "ProviderNoResponseProof",
    "ProviderNoResponseProvenError",
    "classify_provider_failed_response",
    "classify_provider_no_response",
]
