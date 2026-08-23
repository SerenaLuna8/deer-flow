"""Private absolute usage contract for one logical Run token budget."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Final

TOKEN_BUDGET_USAGE_SCHEMA_VERSION: Final = "token.budget.usage.v1"
TOKEN_BUDGET_USAGE_RECORDER_CONTEXT_KEY: Final = "__token_budget_usage_recorder"


class TokenBudgetUsageInvalid(ValueError):
    """One usage snapshot does not satisfy the exact wire contract."""


class TokenBudgetUsageConflict(RuntimeError):
    """Two snapshots cannot represent one monotonic logical Run history."""


@dataclass(frozen=True, slots=True)
class TokenBudgetUsageSnapshot:
    """Absolute input/output counters settled for one logical Run."""

    run_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id:
            raise TokenBudgetUsageInvalid("run_id must be a non-empty string")
        for field_name in ("input_tokens", "output_tokens", "total_tokens"):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise TokenBudgetUsageInvalid(
                    f"{field_name} must be a non-negative integer",
                )
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise TokenBudgetUsageInvalid(
                "total_tokens must equal input_tokens plus output_tokens",
            )

    @classmethod
    def zero(cls, run_id: str) -> TokenBudgetUsageSnapshot:
        return cls(run_id=run_id)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": TOKEN_BUDGET_USAGE_SCHEMA_VERSION,
            "run_id": self.run_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


def dominant_token_budget_usage(
    left: TokenBudgetUsageSnapshot,
    right: TokenBudgetUsageSnapshot,
) -> TokenBudgetUsageSnapshot:
    """Return the dimensionally dominant same-Run snapshot or fail closed."""

    if type(left) is not TokenBudgetUsageSnapshot or type(right) is not TokenBudgetUsageSnapshot:
        raise TokenBudgetUsageConflict("token budget usage type is invalid")
    if left.run_id != right.run_id:
        raise TokenBudgetUsageConflict(
            "token budget usage belongs to another Run",
        )
    left_dominates = left.input_tokens >= right.input_tokens and left.output_tokens >= right.output_tokens
    right_dominates = right.input_tokens >= left.input_tokens and right.output_tokens >= left.output_tokens
    if left_dominates:
        return left
    if right_dominates:
        return right
    raise TokenBudgetUsageConflict(
        "same-Run token budget usage is dimensionally inconsistent",
    )


class TokenBudgetUsageRecorder:
    """Opaque attempt-local recorder seeded from the durable Run aggregate."""

    __slots__ = ("_lock", "_snapshot")

    def __init__(self, baseline: TokenBudgetUsageSnapshot) -> None:
        if type(baseline) is not TokenBudgetUsageSnapshot:
            raise TypeError("baseline must be a TokenBudgetUsageSnapshot")
        self._lock = threading.Lock()
        self._snapshot = baseline

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<opaque>)"

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("token budget usage recorder is not serializable")

    def snapshot(self) -> TokenBudgetUsageSnapshot:
        with self._lock:
            return self._snapshot

    def merge(
        self,
        candidate: TokenBudgetUsageSnapshot,
    ) -> TokenBudgetUsageSnapshot:
        with self._lock:
            self._snapshot = dominant_token_budget_usage(
                self._snapshot,
                candidate,
            )
            return self._snapshot


__all__ = [
    "TOKEN_BUDGET_USAGE_RECORDER_CONTEXT_KEY",
    "TOKEN_BUDGET_USAGE_SCHEMA_VERSION",
    "TokenBudgetUsageConflict",
    "TokenBudgetUsageInvalid",
    "TokenBudgetUsageRecorder",
    "TokenBudgetUsageSnapshot",
    "dominant_token_budget_usage",
]
