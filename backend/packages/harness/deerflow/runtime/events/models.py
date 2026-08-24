"""Typed durable stream frames stored in the unified Run event log."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from deerflow.public_error_codes import LLM_PUBLIC_ERROR_CODES

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
            "PROVIDER_REQUEST_CAPACITY_EXCEEDED",
            "SIDE_EFFECT_STATE_UNKNOWN",
        }
    )
    | LLM_PUBLIC_ERROR_CODES
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
    ) -> StreamFrame:
        if status not in _TERMINAL_STATUSES:
            raise ValueError("terminal stream status is invalid")
        data = {"status": status}
        if error_code is not None:
            data["error_code"] = error_code
        return cls(event="end", data=data, terminal=True)


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

    def __post_init__(self) -> None:
        if _POSITIVE_ASCII_DECIMAL.fullmatch(self.id) is None:
            raise ValueError("stored stream frame id must be a positive decimal")
        StreamFrame(
            event=self.event,
            data=self.data,
            category=self.category,
            terminal=self.terminal,
        )


__all__ = [
    "STREAM_TERMINAL_ERROR_CODES",
    "StoredStreamFrame",
    "StreamClosed",
    "StreamCursorOutOfRange",
    "StreamFrame",
    "StreamLeaseProof",
    "StreamScopeRequired",
    "StreamScopeNotFound",
    "StreamWriteAuthorizationRevoked",
    "StreamWriteAuthorityRequired",
    "StreamWriteCancelled",
    "StreamWriteLeaseLost",
]
