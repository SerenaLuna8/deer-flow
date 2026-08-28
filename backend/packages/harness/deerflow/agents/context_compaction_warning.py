"""Private checkpoint contract for automatic Context compaction skips."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Literal, NotRequired, TypedDict

from langchain.agents import AgentState
from langchain.agents.middleware.types import PrivateStateAttr

CONTEXT_COMPACTION_WARNING_STATE_KEY = "context_compaction_warning"


class ContextCompactionFailureReason(StrEnum):
    """Closed reason vocabulary shared by automatic skips and force failures."""

    CHECKPOINT_UNMEASURABLE = "checkpoint_unmeasurable"
    COMPACTION_FAILED = "compaction_failed"
    OBSERVER_UNSUPPORTED = "observer_unsupported"
    PROMPT_BUDGET_TOO_SMALL = "prompt_budget_too_small"
    RECEIPT_INVALID = "receipt_invalid"
    SOURCE_TOO_LARGE = "source_too_large"


class ContextCompactionWarning(TypedDict):
    """Checkpoint-safe record that one automatic compaction was skipped."""

    version: Literal[1]
    disposition: Literal["skip_this_turn"]
    reason: ContextCompactionFailureReason


class ContextCompactionMiddlewareState(AgentState):
    context_compaction_warning: NotRequired[Annotated[ContextCompactionWarning | None, PrivateStateAttr]]


def context_compaction_warning_update(
    reason: ContextCompactionFailureReason,
) -> dict[str, ContextCompactionWarning]:
    """Build the sole state update for an automatic skip."""

    return {
        CONTEXT_COMPACTION_WARNING_STATE_KEY: {
            "version": 1,
            "disposition": "skip_this_turn",
            "reason": reason,
        }
    }


def clear_context_compaction_warning(
    state: Mapping[str, object],
) -> dict[str, None] | None:
    """Clear a prior one-turn warning without changing a fresh no-op result."""

    if state.get(CONTEXT_COMPACTION_WARNING_STATE_KEY) is None:
        return None
    return {CONTEXT_COMPACTION_WARNING_STATE_KEY: None}


def read_context_compaction_warning(
    value: object,
) -> ContextCompactionWarning | None:
    """Validate one checkpointed automatic-compaction warning."""

    if not isinstance(value, Mapping) or set(value) != {
        "version",
        "disposition",
        "reason",
    }:
        return None
    if type(value.get("version")) is not int or value.get("version") != 1:
        return None
    if value.get("disposition") != "skip_this_turn":
        return None
    try:
        reason = ContextCompactionFailureReason(value.get("reason"))
    except (TypeError, ValueError):
        return None
    return {
        "version": 1,
        "disposition": "skip_this_turn",
        "reason": reason,
    }


__all__ = [
    "CONTEXT_COMPACTION_WARNING_STATE_KEY",
    "ContextCompactionFailureReason",
    "ContextCompactionMiddlewareState",
    "ContextCompactionWarning",
    "clear_context_compaction_warning",
    "context_compaction_warning_update",
    "read_context_compaction_warning",
]
