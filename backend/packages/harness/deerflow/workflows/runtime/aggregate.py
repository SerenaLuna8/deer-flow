"""Exclusive-branch Variable Aggregate semantics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any


class _Missing(Enum):
    TOKEN = "workflow-missing"


MISSING = _Missing.TOKEN


class MissingAggregateValueError(ValueError):
    """No mutually exclusive branch produced this aggregate group."""


class AmbiguousAggregateValueError(ValueError):
    """More than one supposedly exclusive branch produced a value."""


@dataclass(frozen=True, slots=True)
class AggregateResolution:
    input_id: str
    value: Any


def resolve_exclusive_branch_value(
    candidates: Mapping[str, Any],
    candidate_input_ids: Sequence[str],
) -> AggregateResolution:
    """Resolve exactly one present value; JSON null is present, MISSING is not."""

    if len(candidate_input_ids) != len(set(candidate_input_ids)):
        raise ValueError("candidate input ids must be unique")
    present = [(input_id, candidates[input_id]) for input_id in candidate_input_ids if input_id in candidates and candidates[input_id] is not MISSING]
    if not present:
        raise MissingAggregateValueError("no branch value is present")
    if len(present) > 1:
        ids = ", ".join(input_id for input_id, _value in present)
        raise AmbiguousAggregateValueError(f"multiple branch values are present: {ids}")
    input_id, value = present[0]
    return AggregateResolution(input_id=input_id, value=value)
