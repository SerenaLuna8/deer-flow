"""Safe receipt contract for LLM attempts that recovered after retry."""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from typing import Final, Literal, TypedDict

from deerflow.public_error_codes import (
    LLM_PUBLIC_ERROR_CODES,
    llm_error_code_for_reason,
    normalize_llm_error_reason,
)

RECOVERED_LLM_FAILURES_KEY: Final[str] = "deerflow_recovered_llm_failures"
RECOVERED_LLM_FAILURES_VERSION: Final[int] = 2
MAX_RECOVERED_LLM_FAILURES: Final[int] = 512

RecoveredLLMCaller = Literal["lead_agent", "subagent"]
RecoveredLLMFailureSubtype = Literal[
    "connection",
    "timeout",
    "http_status",
    "provider_busy",
    "empty_response",
    "unknown",
]


class RecoveredLLMFailure(TypedDict):
    attempt: int
    max_attempts: int
    error_code: str
    reason: str
    caller: RecoveredLLMCaller
    failure_subtype: RecoveredLLMFailureSubtype
    status_code: int | None
    disposition: Literal["recovered"]


def build_recovered_llm_failures_receipt(
    failures: Sequence[RecoveredLLMFailure],
) -> dict[str, object]:
    """Build one versioned receipt from already-classified safe fields."""

    return {
        "schema_version": RECOVERED_LLM_FAILURES_VERSION,
        "failures": [dict(failure) for failure in failures],
    }


def read_recovered_llm_failures(
    value: object,
) -> tuple[RecoveredLLMFailure, ...]:
    """Validate one untrusted receipt without raising or coercing values."""

    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "failures",
    }:
        return ()
    if type(value.get("schema_version")) is not int or value.get("schema_version") != RECOVERED_LLM_FAILURES_VERSION:
        return ()
    raw_failures = value.get("failures")
    if not isinstance(raw_failures, list) or not raw_failures or len(raw_failures) > MAX_RECOVERED_LLM_FAILURES:
        return ()
    failures: list[RecoveredLLMFailure] = []
    for raw in raw_failures:
        if not isinstance(raw, Mapping) or set(raw) != {
            "attempt",
            "max_attempts",
            "error_code",
            "reason",
            "caller",
            "failure_subtype",
            "status_code",
            "disposition",
        }:
            return ()
        attempt = raw.get("attempt")
        max_attempts = raw.get("max_attempts")
        error_code = raw.get("error_code")
        reason = raw.get("reason")
        caller = raw.get("caller")
        failure_subtype = raw.get("failure_subtype")
        status_code = raw.get("status_code")
        if (
            type(attempt) is not int
            or type(max_attempts) is not int
            or not 1 <= attempt < max_attempts
            or type(error_code) is not str
            or error_code not in LLM_PUBLIC_ERROR_CODES
            or type(reason) is not str
            or normalize_llm_error_reason(reason) != reason
            or llm_error_code_for_reason(reason) != error_code
            or type(caller) is not str
            or caller not in {"lead_agent", "subagent"}
            or type(failure_subtype) is not str
            or failure_subtype
            not in {
                "connection",
                "timeout",
                "http_status",
                "provider_busy",
                "empty_response",
                "unknown",
            }
            or (status_code is not None and (type(status_code) is not int or not 100 <= status_code <= 599))
            or (failure_subtype == "http_status") != (status_code is not None)
            or raw.get("disposition") != "recovered"
        ):
            return ()
        failures.append(
            {
                "attempt": attempt,
                "max_attempts": max_attempts,
                "error_code": error_code,
                "reason": reason,
                "caller": caller,
                "failure_subtype": failure_subtype,
                "status_code": status_code,
                "disposition": "recovered",
            }
        )
    return tuple(failures)


def _copy_failure(failure: RecoveredLLMFailure) -> RecoveredLLMFailure:
    return {
        "attempt": failure["attempt"],
        "max_attempts": failure["max_attempts"],
        "error_code": failure["error_code"],
        "reason": failure["reason"],
        "caller": failure["caller"],
        "failure_subtype": failure["failure_subtype"],
        "status_code": failure["status_code"],
        "disposition": "recovered",
    }


class RunRecoveredLLMFailureRecorder:
    """Thread-safe, run-scoped owner of validated recovered-attempt facts."""

    __slots__ = ("_failures", "_lock")

    def __init__(self) -> None:
        self._failures: list[RecoveredLLMFailure] = []
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<opaque>)"

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("Run recovered LLM failure recorder is not serializable")

    def record(self, receipt: object) -> tuple[RecoveredLLMFailure, ...]:
        """Validate and append one receipt, returning the bounded aggregate."""

        parsed = read_recovered_llm_failures(receipt)
        with self._lock:
            remaining = MAX_RECOVERED_LLM_FAILURES - len(self._failures)
            if parsed and remaining > 0:
                self._failures.extend(parsed[:remaining])
            return tuple(_copy_failure(failure) for failure in self._failures)

    def snapshot(self) -> tuple[RecoveredLLMFailure, ...]:
        """Return an isolated immutable-container snapshot."""

        with self._lock:
            return tuple(_copy_failure(failure) for failure in self._failures)


__all__ = [
    "MAX_RECOVERED_LLM_FAILURES",
    "RECOVERED_LLM_FAILURES_KEY",
    "RECOVERED_LLM_FAILURES_VERSION",
    "RecoveredLLMCaller",
    "RecoveredLLMFailure",
    "RecoveredLLMFailureSubtype",
    "RunRecoveredLLMFailureRecorder",
    "build_recovered_llm_failures_receipt",
    "read_recovered_llm_failures",
]
