"""Typed durable stream frames stored in the unified Run event log."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from deerflow.public_error_codes import LLM_PUBLIC_ERROR_CODES
from deerflow.runtime.runs.schemas import RunStatus

_STREAM_EVENT = re.compile(r"[a-z][a-z0-9_.-]{0,31}")
_STREAM_NAMESPACE_MAX_DEPTH = 32
_STREAM_NAMESPACE_SEGMENT_MAX_LENGTH = 256
_STREAM_NAMESPACED_EVENT_MAX_LENGTH = 4096
_POSITIVE_ASCII_DECIMAL = re.compile(r"[1-9][0-9]*")
_TERMINAL_STATUSES = frozenset(
    {
        "cancelled",
        "completed",
        "error",
        "failed",
        "interrupted",
        "success",
        "timeout",
    }
)
# Retained stream rows may use the legacy ``success`` spelling. Stream frames
# canonicalize that spelling independently from the typed Run-settlement
# projection below; ``success`` remains schema-legal only for immutable rows
# written before the cutover.
_STREAM_TERMINAL_STATUS_EQUIVALENCE = {"success": "completed"}
StreamTerminalAuthority = Literal["ordinary", "durable_response"]
_STREAM_TERMINAL_AUTHORITIES = frozenset({"ordinary", "durable_response"})
StreamTerminalCandidatePrecedence = Literal["preempts_ordinary_stop"]
RunSettlementStreamTerminalStatus = Literal[
    "completed",
    "error",
    "timeout",
    "interrupted",
]
_RUN_SETTLEMENT_STREAM_TERMINAL_STATUSES: dict[
    RunStatus,
    RunSettlementStreamTerminalStatus,
] = {
    RunStatus.success: "completed",
    RunStatus.error: "error",
    RunStatus.timeout: "timeout",
    RunStatus.interrupted: "interrupted",
}


def stream_terminal_status_for_run_settlement(
    status: RunStatus,
) -> RunSettlementStreamTerminalStatus:
    """Project one terminal Run settlement onto its stream spelling."""

    if type(status) is not RunStatus:
        raise TypeError("RunStatus is required")
    try:
        return _RUN_SETTLEMENT_STREAM_TERMINAL_STATUSES[status]
    except KeyError:
        raise ValueError("Run settlement status is not terminal") from None


def canonical_stream_terminal_status(status: str) -> str:
    """Canonicalize a stream-terminal spelling without interpreting a Run."""

    return _STREAM_TERMINAL_STATUS_EQUIVALENCE.get(status, status)


STREAM_TERMINAL_ERROR_CODES = (
    frozenset(
        {
            "MODEL_OUTPUT_LIMIT",
            "LOOP_SAFETY_LIMIT",
            "LOOP_FINALIZATION_FAILED",
            "TOOL_CALL_CONTROL_STATE_INVALID",
            "RUN_POLICY_STALE",
            "OUTPUT_DELIVERY_INCOMPLETE",
            "CURRENT_UPLOAD_UNAVAILABLE",
            "PROVIDER_REQUEST_USAGE_UNSUPPORTED",
            "PROVIDER_REQUEST_PROFILE_DRIFT",
            "CONTEXT_CAPACITY_EXCEEDED",
            "CONTEXT_PROVIDER_CALL_AMBIGUOUS",
            "SIDE_EFFECT_STATE_UNKNOWN",
        }
    )
    | LLM_PUBLIC_ERROR_CODES
)


@dataclass(frozen=True, slots=True)
class StreamTerminalCandidate:
    """Internal durable proposal awaiting Job/Run terminal arbitration."""

    status: Literal["error"]
    error_code: str
    authority: Literal["durable_response"] = "durable_response"
    precedence: StreamTerminalCandidatePrecedence = "preempts_ordinary_stop"
    version: Literal[1] = 1

    def __post_init__(self) -> None:
        if self.status != "error":
            raise ValueError("stream terminal candidate status is invalid")
        if self.error_code not in STREAM_TERMINAL_ERROR_CODES:
            raise ValueError("stream terminal candidate error code is invalid")
        if self.authority != "durable_response":
            raise ValueError("stream terminal candidate authority is invalid")
        if self.precedence != "preempts_ordinary_stop":
            raise ValueError("stream terminal candidate precedence is invalid")
        if self.version != 1:
            raise ValueError("stream terminal candidate version is invalid")

    def to_payload(self) -> dict[str, object]:
        return {
            "version": self.version,
            "status": self.status,
            "error_code": self.error_code,
            "authority": self.authority,
            "precedence": self.precedence,
        }

    @classmethod
    def from_payload(cls, payload: object) -> StreamTerminalCandidate:
        if not isinstance(payload, Mapping):
            raise ValueError("stream terminal candidate payload is invalid")
        return cls(
            status=payload.get("status"),  # type: ignore[arg-type]
            error_code=payload.get("error_code"),  # type: ignore[arg-type]
            authority=payload.get("authority"),  # type: ignore[arg-type]
            precedence=payload.get("precedence"),  # type: ignore[arg-type]
            version=payload.get("version"),  # type: ignore[arg-type]
        )


def _is_valid_stream_event(event: object) -> bool:
    if not isinstance(event, str) or len(event) > _STREAM_NAMESPACED_EVENT_MAX_LENGTH:
        return False
    base, separator, _namespace = event.partition("|")
    if _STREAM_EVENT.fullmatch(base) is None:
        return False
    if not separator:
        return True
    segments = event.split("|")[1:]
    if not 1 <= len(segments) <= _STREAM_NAMESPACE_MAX_DEPTH:
        return False
    return all(segment and len(segment) <= _STREAM_NAMESPACE_SEGMENT_MAX_LENGTH and "\x00" not in segment and "\r" not in segment and "\n" not in segment for segment in segments)


class StreamScopeNotFound(LookupError):
    """The requested thread is absent from the supplied private scope."""


class StreamCursorOutOfRange(ValueError):
    """The supplied cursor is ahead of the scoped thread event log."""


class StreamClosed(RuntimeError):
    """A terminal frame already closes this Run stream."""


class StreamScopeRequired(PermissionError):
    """A private durable stream operation omitted its explicit scope."""


class StreamWriteAuthorityRequired(PermissionError):
    """A job-owned Run stream append omitted its live lease authority."""


class StreamWriteAuthorizationRevoked(StreamWriteAuthorityRequired):
    """Current project governance no longer permits stream mutation."""


class StreamWriteLeaseLost(StreamWriteAuthorityRequired):
    """The supplied stream lease capability is not current."""


class StreamWriteCancelled(StreamWriteAuthorityRequired):
    """A cancellation marker forbids another non-terminal frame."""


@dataclass(frozen=True, slots=True)
class StreamLeaseProof:
    """Ephemeral raw-token capability for a job-owned stream append."""

    job_id: uuid.UUID
    lease_token: str = field(repr=False)

    def __post_init__(self) -> None:
        try:
            normalized_job_id = uuid.UUID(str(self.job_id))
        except (AttributeError, TypeError, ValueError):
            raise TypeError("stream lease proof requires a UUID job ID") from None
        if not isinstance(self.lease_token, str) or not 1 <= len(self.lease_token) <= 512:
            raise ValueError("stream lease proof token is invalid")
        object.__setattr__(self, "job_id", normalized_job_id)


@dataclass(frozen=True, slots=True)
class StreamFrame:
    """One validated SSE frame before durable sequence assignment."""

    event: str
    data: Any
    category: str = "stream"
    terminal: bool = False
    terminal_authority: StreamTerminalAuthority = "ordinary"

    def __post_init__(self) -> None:
        if self.category != "stream":
            raise ValueError("stream frame category must be 'stream'")
        if not _is_valid_stream_event(self.event):
            raise ValueError("stream frame event is invalid")
        base_event = self.event.partition("|")[0]
        if base_event in {"end", "stream.end"} and not self.terminal:
            raise ValueError("stream end events are reserved for terminal frames")
        if self.terminal and self.event != "end":
            raise ValueError("terminal stream frame event must be 'end'")
        if self.terminal_authority not in _STREAM_TERMINAL_AUTHORITIES:
            raise ValueError("stream terminal authority is invalid")
        if not self.terminal and self.terminal_authority != "ordinary":
            raise ValueError("non-terminal stream frame cannot carry terminal authority")
        if self.terminal:
            if not isinstance(self.data, Mapping):
                raise ValueError("terminal stream frame data is invalid")
            if set(self.data) - {"status", "error_code"}:
                raise ValueError("terminal stream frame data is invalid")
            if self.data.get("status") not in _TERMINAL_STATUSES:
                raise ValueError("terminal stream status is invalid")
            error_code = self.data.get("error_code")
            if error_code is not None and error_code not in STREAM_TERMINAL_ERROR_CODES:
                raise ValueError("terminal stream error code is invalid")

    @classmethod
    def end(
        cls,
        *,
        status: str,
        error_code: str | None = None,
        terminal_authority: StreamTerminalAuthority = "ordinary",
    ) -> StreamFrame:
        status = canonical_stream_terminal_status(status)
        if status not in _TERMINAL_STATUSES:
            raise ValueError("terminal stream status is invalid")
        data = {"status": status}
        if error_code is not None:
            data["error_code"] = error_code
        return cls(
            event="end",
            data=data,
            terminal=True,
            terminal_authority=terminal_authority,
        )


@dataclass(frozen=True, slots=True)
class StoredStreamFrame:
    """A PostgreSQL-backed frame with a thread-monotonic decimal ID."""

    id: str
    thread_id: str
    run_id: str
    event: str
    data: Any
    category: str = "stream"
    terminal: bool = False
    created: bool = True
    terminal_authority: StreamTerminalAuthority = "ordinary"

    def __post_init__(self) -> None:
        if _POSITIVE_ASCII_DECIMAL.fullmatch(self.id) is None:
            raise ValueError("stored stream frame id must be a positive decimal")
        StreamFrame(
            event=self.event,
            data=self.data,
            category=self.category,
            terminal=self.terminal,
            terminal_authority=self.terminal_authority,
        )


__all__ = [
    "RunSettlementStreamTerminalStatus",
    "STREAM_TERMINAL_ERROR_CODES",
    "StoredStreamFrame",
    "StreamClosed",
    "StreamCursorOutOfRange",
    "StreamFrame",
    "StreamLeaseProof",
    "StreamTerminalCandidate",
    "StreamTerminalCandidatePrecedence",
    "StreamTerminalAuthority",
    "StreamScopeRequired",
    "StreamScopeNotFound",
    "StreamWriteAuthorizationRevoked",
    "StreamWriteAuthorityRequired",
    "StreamWriteCancelled",
    "StreamWriteLeaseLost",
    "stream_terminal_status_for_run_settlement",
]
