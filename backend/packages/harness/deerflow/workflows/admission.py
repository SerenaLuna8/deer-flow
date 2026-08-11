"""Canonical client request identity for Workflow Run admission."""

from __future__ import annotations

import hashlib
import math
import uuid
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, cast

from deerflow.workflows.canonical import (
    CanonicalJsonUtf8BudgetExceeded,
    canonical_json_value_with_utf8_budget,
)
from deerflow.workflows.contracts import MAX_SAFE_JSON_INTEGER, JsonValue

WORKFLOW_RUN_INPUT_MAX_DEPTH = 64
WORKFLOW_RUN_INPUT_MAX_NODES = 65_536
WORKFLOW_RUN_INPUT_MAX_CANONICAL_BYTES = 2_097_152
WORKFLOW_RUN_ADMISSION_REQUEST_MAX_CANONICAL_BYTES = WORKFLOW_RUN_INPUT_MAX_CANONICAL_BYTES + 1_024


def _optional_uuid(value: object, *, field_name: str) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a UUID or null") from None


def _require_unicode_scalars(value: str) -> None:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError("Workflow inputs must contain only Unicode scalar values")


def _validated_inputs_and_canonical(
    value: object,
) -> tuple[dict[str, object], str]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise ValueError("Workflow inputs must be an object with string keys")
    nodes = 0
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > WORKFLOW_RUN_INPUT_MAX_DEPTH:
            raise ValueError("Workflow inputs exceed the maximum JSON nesting depth")
        nodes += 1
        if nodes > WORKFLOW_RUN_INPUT_MAX_NODES:
            raise ValueError("Workflow inputs exceed the maximum JSON node count")
        if current is None or type(current) is bool:
            continue
        if type(current) is str:
            _require_unicode_scalars(current)
            continue
        if type(current) is int:
            if abs(current) > MAX_SAFE_JSON_INTEGER:
                raise ValueError("Workflow input integer exceeds the cross-runtime safe range")
            continue
        if type(current) is float:
            if not math.isfinite(current) or (current.is_integer() and abs(current) > MAX_SAFE_JSON_INTEGER):
                raise ValueError("Workflow input number is not canonical cross-runtime JSON")
            continue
        if type(current) is list:
            stack.extend((nested, depth + 1) for nested in current)
            continue
        if type(current) is dict:
            for key, nested in current.items():
                if type(key) is not str:
                    raise ValueError("Workflow input object keys must be strings")
                _require_unicode_scalars(key)
                stack.append((nested, depth + 1))
            continue
        raise ValueError("Workflow inputs must contain only JSON values")

    try:
        canonical, _ = canonical_json_value_with_utf8_budget(
            cast(JsonValue, value),
            max_utf8_bytes=WORKFLOW_RUN_INPUT_MAX_CANONICAL_BYTES,
        )
    except CanonicalJsonUtf8BudgetExceeded as error:
        raise ValueError("Workflow inputs exceed the maximum canonical UTF-8 byte count") from error
    except (TypeError, UnicodeEncodeError, ValueError) as error:
        raise ValueError("Workflow inputs must be portable canonical JSON") from error
    return deepcopy(value), canonical


def validate_workflow_run_inputs_v1(value: object) -> dict[str, object]:
    """Validate and deep-copy the one shared public/admission input boundary."""

    inputs, _ = _validated_inputs_and_canonical(value)
    return inputs


def _freeze_json(value: object) -> object:
    if type(value) is dict:
        return MappingProxyType({key: _freeze_json(nested) for key, nested in value.items()})
    if type(value) is list:
        return tuple(_freeze_json(nested) for nested in value)
    return value


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(nested) for key, nested in value.items()}
    if type(value) is tuple:
        return [_thaw_json(nested) for nested in value]
    return value


def materialize_workflow_run_inputs_v1(
    value: Mapping[str, object],
) -> dict[str, object]:
    inputs = _thaw_json(value)
    if type(inputs) is not dict:  # pragma: no cover - annotated invariant
        raise AssertionError("Workflow admission inputs must remain an object")
    return inputs


@dataclass(frozen=True, slots=True)
class WorkflowRunAdmissionRequest:
    """Only client-stable coordinates; resolved policy/runtime fields are absent."""

    requested_workflow_version_id: uuid.UUID | None
    inputs: Mapping[str, object]
    trigger_kind: Literal["manual", "api"]
    trigger_ref: str | None
    retry_of_run_id: uuid.UUID | None
    _input_digest: str = field(init=False, repr=False)
    _digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "requested_workflow_version_id",
            _optional_uuid(
                self.requested_workflow_version_id,
                field_name="requested_workflow_version_id",
            ),
        )
        if not isinstance(self.inputs, Mapping) or any(not isinstance(key, str) for key in self.inputs):
            raise TypeError("inputs must be an object mapping")
        inputs, canonical_inputs = _validated_inputs_and_canonical(dict(self.inputs))
        object.__setattr__(
            self,
            "_input_digest",
            hashlib.sha256(canonical_inputs.encode("utf-8")).hexdigest(),
        )
        if self.trigger_kind not in {"manual", "api"}:
            raise ValueError("unsupported Workflow trigger kind")
        if self.trigger_ref is not None and (not isinstance(self.trigger_ref, str) or not self.trigger_ref or len(self.trigger_ref) > 128):
            raise ValueError("trigger_ref must be a non-empty bounded string")
        object.__setattr__(
            self,
            "retry_of_run_id",
            _optional_uuid(self.retry_of_run_id, field_name="retry_of_run_id"),
        )
        canonical, _ = canonical_json_value_with_utf8_budget(
            cast(
                JsonValue,
                {
                    "contract": "workflow-run-admission-request-v1",
                    "workflow_version_id": (None if self.requested_workflow_version_id is None else str(self.requested_workflow_version_id)),
                    "inputs": inputs,
                    "trigger_kind": self.trigger_kind,
                    "trigger_ref": self.trigger_ref,
                    "retry_of_run_id": (None if self.retry_of_run_id is None else str(self.retry_of_run_id)),
                },
            ),
            max_utf8_bytes=WORKFLOW_RUN_ADMISSION_REQUEST_MAX_CANONICAL_BYTES,
        )
        object.__setattr__(
            self,
            "_digest",
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )
        object.__setattr__(self, "inputs", _freeze_json(inputs))

    @property
    def input_digest(self) -> str:
        return self._input_digest

    @property
    def digest(self) -> str:
        return self._digest

    def materialize_inputs(self) -> dict[str, object]:
        return materialize_workflow_run_inputs_v1(self.inputs)


__all__ = [
    "WORKFLOW_RUN_ADMISSION_REQUEST_MAX_CANONICAL_BYTES",
    "WORKFLOW_RUN_INPUT_MAX_CANONICAL_BYTES",
    "WORKFLOW_RUN_INPUT_MAX_DEPTH",
    "WORKFLOW_RUN_INPUT_MAX_NODES",
    "WorkflowRunAdmissionRequest",
    "materialize_workflow_run_inputs_v1",
    "validate_workflow_run_inputs_v1",
]
