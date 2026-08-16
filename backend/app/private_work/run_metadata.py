"""Strict server-owned metadata carried by one logical private Run."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

RUN_VISION_DISPATCH_BUDGET_KEY: Final = "__vision_dispatch_budget_v1"
VISION_DISPATCH_BUDGET_SCHEMA_VERSION: Final = "vision.dispatch.budget.v1"
RUN_HOST_EXECUTION_SUSPENSION_KEY: Final = "__host_execution_suspension_v1"
HOST_EXECUTION_SUSPENSION_SCHEMA_VERSION: Final = "host_execution.suspension.v1"
_VISION_DISPATCH_BUDGET_KEYS: Final = frozenset(
    {
        "schema_version",
        "call_count",
        "normalized_bytes",
        "normalized_pixels",
    }
)


class RunVisionDispatchBudgetInvalid(ValueError):
    """Persisted Vision dispatch counters do not match the exact contract."""


class RunVisionDispatchBudgetExceeded(RuntimeError):
    """One more governed HTTP attempt would cross a per-Run hard limit."""


class RunHostExecutionSuspensionInvalid(ValueError):
    """A persisted host-execution suspension marker is malformed."""


@dataclass(frozen=True, slots=True)
class RunHostExecutionSuspension:
    approval_id: uuid.UUID
    source_job_id: uuid.UUID
    producing_attempt_id: uuid.UUID

    def __post_init__(self) -> None:
        if any(
            type(value) is not uuid.UUID
            for value in (
                self.approval_id,
                self.source_job_id,
                self.producing_attempt_id,
            )
        ):
            raise RunHostExecutionSuspensionInvalid

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": HOST_EXECUTION_SUSPENSION_SCHEMA_VERSION,
            "approval_id": str(self.approval_id),
            "source_job_id": str(self.source_job_id),
            "producing_attempt_id": str(self.producing_attempt_id),
        }


def run_host_execution_suspension(
    metadata: Mapping[str, object],
) -> RunHostExecutionSuspension | None:
    """Parse the exact server-only suspension proof, if present."""

    if not isinstance(metadata, Mapping):
        raise RunHostExecutionSuspensionInvalid
    if RUN_HOST_EXECUTION_SUSPENSION_KEY not in metadata:
        return None
    raw = metadata[RUN_HOST_EXECUTION_SUSPENSION_KEY]
    if (
        not isinstance(raw, Mapping)
        or set(raw)
        != {
            "schema_version",
            "approval_id",
            "source_job_id",
            "producing_attempt_id",
        }
        or raw.get("schema_version") != HOST_EXECUTION_SUSPENSION_SCHEMA_VERSION
    ):
        raise RunHostExecutionSuspensionInvalid
    values: dict[str, uuid.UUID] = {}
    try:
        for key in (
            "approval_id",
            "source_job_id",
            "producing_attempt_id",
        ):
            value = raw.get(key)
            if type(value) is not str:
                raise RunHostExecutionSuspensionInvalid
            parsed = uuid.UUID(value)
            if str(parsed) != value:
                raise RunHostExecutionSuspensionInvalid
            values[key] = parsed
        return RunHostExecutionSuspension(
            approval_id=values["approval_id"],
            source_job_id=values["source_job_id"],
            producing_attempt_id=values["producing_attempt_id"],
        )
    except (AttributeError, TypeError, ValueError):
        raise RunHostExecutionSuspensionInvalid from None


def with_run_host_execution_suspension(
    metadata: Mapping[str, object],
    *,
    suspension: RunHostExecutionSuspension,
) -> dict[str, object]:
    """Return metadata containing one current server-issued marker."""

    if not isinstance(metadata, Mapping):
        raise RunHostExecutionSuspensionInvalid
    existing = run_host_execution_suspension(metadata)
    if existing is not None and existing != suspension:
        raise RunHostExecutionSuspensionInvalid
    result = dict(metadata)
    result[RUN_HOST_EXECUTION_SUSPENSION_KEY] = suspension.as_dict()
    return result


@dataclass(frozen=True, slots=True)
class RunVisionDispatchBudget:
    call_count: int = 0
    normalized_bytes: int = 0
    normalized_pixels: int = 0

    def __post_init__(self) -> None:
        for field_name in (
            "call_count",
            "normalized_bytes",
            "normalized_pixels",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise RunVisionDispatchBudgetInvalid

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": VISION_DISPATCH_BUDGET_SCHEMA_VERSION,
            "call_count": self.call_count,
            "normalized_bytes": self.normalized_bytes,
            "normalized_pixels": self.normalized_pixels,
        }


def run_vision_dispatch_budget(
    metadata: Mapping[str, object],
) -> RunVisionDispatchBudget:
    """Parse the exact durable aggregate; absence is the legacy zero state."""

    if not isinstance(metadata, Mapping):
        raise RunVisionDispatchBudgetInvalid
    if RUN_VISION_DISPATCH_BUDGET_KEY not in metadata:
        return RunVisionDispatchBudget()
    raw = metadata[RUN_VISION_DISPATCH_BUDGET_KEY]
    if not isinstance(raw, Mapping) or set(raw) != _VISION_DISPATCH_BUDGET_KEYS or raw.get("schema_version") != VISION_DISPATCH_BUDGET_SCHEMA_VERSION:
        raise RunVisionDispatchBudgetInvalid
    return RunVisionDispatchBudget(
        call_count=raw.get("call_count"),  # type: ignore[arg-type]
        normalized_bytes=raw.get("normalized_bytes"),  # type: ignore[arg-type]
        normalized_pixels=raw.get("normalized_pixels"),  # type: ignore[arg-type]
    )


def reserve_run_vision_dispatch_budget(
    metadata: Mapping[str, object],
    *,
    normalized_bytes: int,
    normalized_pixels: int,
    max_calls: int,
    max_normalized_bytes: int,
    max_normalized_pixels: int,
) -> dict[str, object]:
    """Return metadata with one cumulative attempt reserved or fail closed."""

    if (
        type(normalized_bytes) is not int
        or normalized_bytes < 1
        or type(normalized_pixels) is not int
        or normalized_pixels < 1
        or type(max_calls) is not int
        or max_calls < 1
        or type(max_normalized_bytes) is not int
        or max_normalized_bytes < 1
        or type(max_normalized_pixels) is not int
        or max_normalized_pixels < 1
    ):
        raise RunVisionDispatchBudgetInvalid
    current = run_vision_dispatch_budget(metadata)
    reserved = RunVisionDispatchBudget(
        call_count=current.call_count + 1,
        normalized_bytes=current.normalized_bytes + normalized_bytes,
        normalized_pixels=current.normalized_pixels + normalized_pixels,
    )
    if reserved.call_count > max_calls or reserved.normalized_bytes > max_normalized_bytes or reserved.normalized_pixels > max_normalized_pixels:
        raise RunVisionDispatchBudgetExceeded
    result = dict(metadata)
    result[RUN_VISION_DISPATCH_BUDGET_KEY] = reserved.as_dict()
    return result


def strip_server_run_metadata(value: Mapping[str, object]) -> dict[str, object]:
    """Remove recursively reserved metadata before compare or projection."""

    return {key: _strip_server_run_metadata_value(item) for key, item in value.items() if isinstance(key, str) and not key.startswith("__")}


def _strip_server_run_metadata_value(value: object) -> object:
    if isinstance(value, Mapping):
        return strip_server_run_metadata(value)
    if isinstance(value, list):
        return [_strip_server_run_metadata_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_strip_server_run_metadata_value(item) for item in value)
    return value


__all__ = [
    "HOST_EXECUTION_SUSPENSION_SCHEMA_VERSION",
    "RUN_HOST_EXECUTION_SUSPENSION_KEY",
    "RUN_VISION_DISPATCH_BUDGET_KEY",
    "RunHostExecutionSuspension",
    "RunHostExecutionSuspensionInvalid",
    "RunVisionDispatchBudget",
    "RunVisionDispatchBudgetExceeded",
    "RunVisionDispatchBudgetInvalid",
    "reserve_run_vision_dispatch_budget",
    "run_host_execution_suspension",
    "run_vision_dispatch_budget",
    "strip_server_run_metadata",
    "with_run_host_execution_suspension",
]
