"""Provider-specific proof for failures that cannot have produced a response."""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx
from openai import APIConnectionError

_OPENAI_FAMILY_ADAPTERS = frozenset(
    {
        "deepseek",
        "openai",
        "openai_responses",
        "patched_deepseek",
        "patched_openai",
        "patched_openai_responses",
    }
)

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


def classify_provider_no_response(
    *,
    provider_adapter: str | None,
    error: BaseException,
) -> ProviderNoResponseProof | None:
    """Return proof only for OpenAI-family failures before request dispatch.

    OpenAI's SDK wraps httpx transport failures in ``APIConnectionError`` and
    retains the exact transport exception as ``__cause__``.  A connect or pool
    acquisition failure cannot have delivered request bytes to the Provider.
    Read, write, protocol, and unclassified connection failures remain unknown.
    """

    if provider_adapter not in _OPENAI_FAMILY_ADAPTERS:
        return None
    candidate: BaseException | None = error
    if isinstance(error, APIConnectionError):
        candidate = error.__cause__
    for exception_type, failure_code in _NO_RESPONSE_FAILURE_CODES:
        if isinstance(candidate, exception_type):
            return ProviderNoResponseProof(failure_code=failure_code)
    return None


__all__ = [
    "ProviderNoResponseProof",
    "ProviderNoResponseProvenError",
    "classify_provider_no_response",
]
