"""Immutable contracts for one in-process Harness Execution."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from deerflow.token_budget_usage import TokenBudgetUsageSnapshot

RunSemanticStopReason = Literal["loop_capped"]


class RunSemanticStopRecorder:
    """Run-scoped owner channel for a Harness-enforced semantic stop."""

    __slots__ = ("_lock", "_reason", "_suppressed_ai_message_ids")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reason: RunSemanticStopReason | None = None
        self._suppressed_ai_message_ids: dict[str, None] = {}

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<opaque>)"

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("Run semantic stop recorder is not serializable")

    @property
    def reason(self) -> RunSemanticStopReason | None:
        with self._lock:
            return self._reason

    @property
    def suppressed_ai_message_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._suppressed_ai_message_ids)

    def record(
        self,
        reason: RunSemanticStopReason,
        *,
        suppressed_ai_message_id: str | None = None,
    ) -> None:
        if reason != "loop_capped":
            raise ValueError("unsupported Run semantic stop reason")
        if suppressed_ai_message_id is not None and (not isinstance(suppressed_ai_message_id, str) or not suppressed_ai_message_id.strip()):
            raise ValueError(
                "suppressed_ai_message_id must be a non-empty string",
            )
        with self._lock:
            if self._reason is None:
                self._reason = reason
            if suppressed_ai_message_id is not None:
                self._suppressed_ai_message_ids.setdefault(
                    suppressed_ai_message_id,
                    None,
                )


@dataclass(frozen=True, slots=True)
class RunAgentUsageSnapshot:
    """Final attempt-local usage observed by the Harness Execution runner."""

    total_input_tokens: int
    total_output_tokens: int
    total_tokens: int
    llm_call_count: int
    lead_agent_tokens: int
    subagent_tokens: int
    middleware_tokens: int
    token_usage_by_model: Mapping[str, Mapping[str, int]]
    token_budget_usage: TokenBudgetUsageSnapshot | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "total_input_tokens",
            "total_output_tokens",
            "total_tokens",
            "llm_call_count",
            "lead_agent_tokens",
            "subagent_tokens",
            "middleware_tokens",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise TypeError(f"{field_name} must be a non-negative integer")
        if not isinstance(self.token_usage_by_model, Mapping):
            raise TypeError("token_usage_by_model must be a mapping")
        frozen_models: dict[str, Mapping[str, int]] = {}
        for model_name, raw_usage in self.token_usage_by_model.items():
            if type(model_name) is not str or not model_name:
                raise TypeError("token usage model names must be non-empty strings")
            if not isinstance(raw_usage, Mapping):
                raise TypeError("per-model token usage must be a mapping")
            usage: dict[str, int] = {}
            for counter_name, counter_value in raw_usage.items():
                if type(counter_name) is not str or not counter_name:
                    raise TypeError(
                        "token usage counter names must be non-empty strings",
                    )
                if type(counter_value) is not int or counter_value < 0:
                    raise TypeError(
                        "per-model token usage counters must be non-negative integers",
                    )
                usage[counter_name] = counter_value
            frozen_models[model_name] = MappingProxyType(usage)
        object.__setattr__(
            self,
            "token_usage_by_model",
            MappingProxyType(frozen_models),
        )
        if self.token_budget_usage is not None and type(self.token_budget_usage) is not TokenBudgetUsageSnapshot:
            raise TypeError(
                "token_budget_usage must be a TokenBudgetUsageSnapshot or None",
            )


@dataclass(frozen=True, slots=True)
class RunAgentOutcome:
    """One semantic outcome after terminal publication and resource cleanup."""

    status: Literal["succeeded", "cancelled", "failed"]
    usage: RunAgentUsageSnapshot
    public_error_code: str | None = None
    suspended_approval_id: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"succeeded", "cancelled", "failed"}:
            raise ValueError("unsupported Run Agent outcome status")
        if type(self.usage) is not RunAgentUsageSnapshot:
            raise TypeError("usage must be a RunAgentUsageSnapshot")
        if self.status == "failed":
            if not isinstance(self.public_error_code, str) or not self.public_error_code:
                raise ValueError("failed outcome requires a public error code")
        elif self.public_error_code is not None:
            raise ValueError("only failed outcome may carry a public error code")
        if self.suspended_approval_id is not None:
            if self.status != "succeeded":
                raise ValueError(
                    "only successful outcome may carry a suspended approval id",
                )
            if not isinstance(self.suspended_approval_id, str) or not self.suspended_approval_id:
                raise ValueError(
                    "suspended_approval_id must be a non-empty string",
                )

    @classmethod
    def succeeded(
        cls,
        usage: RunAgentUsageSnapshot,
        *,
        suspended_approval_id: str | None = None,
    ) -> RunAgentOutcome:
        return cls(
            "succeeded",
            usage,
            suspended_approval_id=suspended_approval_id,
        )

    @classmethod
    def cancelled(
        cls,
        usage: RunAgentUsageSnapshot,
    ) -> RunAgentOutcome:
        return cls("cancelled", usage)

    @classmethod
    def failed(
        cls,
        usage: RunAgentUsageSnapshot,
        *,
        public_error_code: str,
    ) -> RunAgentOutcome:
        return cls(
            "failed",
            usage,
            public_error_code=public_error_code,
        )


class RunAgentResourceOwnership:
    """Single-transfer marker for executor-owned private Run resources."""

    __slots__ = ("_transferred",)

    def __init__(self) -> None:
        self._transferred = False

    @property
    def transferred(self) -> bool:
        return self._transferred

    def transfer_to_runner(self) -> None:
        if self._transferred:
            raise RuntimeError("Run Agent resource ownership already transferred")
        self._transferred = True


__all__ = [
    "RunAgentOutcome",
    "RunAgentResourceOwnership",
    "RunAgentUsageSnapshot",
    "RunSemanticStopReason",
    "RunSemanticStopRecorder",
]
