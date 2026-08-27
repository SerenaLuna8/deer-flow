"""Side-effect-free value contracts for private Run execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from app.private_work.context import PrivateWorkContext
from app.private_work.run_admission import PersistedRunSnapshot
from app.private_work.run_repository import (
    PrivateRunRecord,
    PrivateRunUsageSnapshot,
)
from app.worker.service import JobOutcome
from deerflow.token_budget_usage import TokenBudgetUsageSnapshot


@dataclass(frozen=True, slots=True)
class AgentExecutionResult:
    status: Literal["succeeded", "cancelled", "failed"]
    public_error_code: str | None = None
    retryable: bool = False
    attempt_usage: PrivateRunUsageSnapshot | None = None
    suspended_approval_id: str | None = None
    durable_terminal: bool = False

    def __post_init__(self) -> None:
        JobOutcome(self.status, self.public_error_code)
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if type(self.durable_terminal) is not bool:
            raise TypeError("durable_terminal must be a boolean")
        if self.status != "failed" and self.durable_terminal:
            raise ValueError(
                "only failed execution may carry a durable terminal",
            )
        if self.status != "failed" and self.retryable:
            raise ValueError("terminal success/cancel outcomes cannot be retryable")
        if self.attempt_usage is not None and type(self.attempt_usage) is not PrivateRunUsageSnapshot:
            raise TypeError("attempt_usage must be a PrivateRunUsageSnapshot or None")
        if self.suspended_approval_id is not None:
            if self.status != "succeeded":
                raise ValueError(
                    "only successful approval suspension may carry an approval id",
                )
            if not isinstance(self.suspended_approval_id, str) or not self.suspended_approval_id:
                raise ValueError(
                    "suspended_approval_id must be a non-empty string",
                )

    @classmethod
    def succeeded(
        cls,
        *,
        attempt_usage: PrivateRunUsageSnapshot | None = None,
        suspended_approval_id: str | None = None,
    ) -> AgentExecutionResult:
        return cls(
            "succeeded",
            attempt_usage=attempt_usage,
            suspended_approval_id=suspended_approval_id,
        )

    @classmethod
    def cancelled(
        cls,
        *,
        attempt_usage: PrivateRunUsageSnapshot | None = None,
    ) -> AgentExecutionResult:
        return cls("cancelled", attempt_usage=attempt_usage)

    @classmethod
    def failed(
        cls,
        public_error_code: str,
        *,
        retryable: bool = True,
        attempt_usage: PrivateRunUsageSnapshot | None = None,
        durable_terminal: bool = False,
    ) -> AgentExecutionResult:
        return cls(
            status="failed",
            public_error_code=public_error_code,
            retryable=retryable,
            attempt_usage=attempt_usage,
            durable_terminal=durable_terminal,
        )


@dataclass(frozen=True, slots=True)
class RecoveredPrivateRunTerminal:
    result: AgentExecutionResult
    ensure_stream_terminal: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.result, AgentExecutionResult):
            raise TypeError("recovered terminal requires an AgentExecutionResult")
        if type(self.ensure_stream_terminal) is not bool:
            raise TypeError("ensure_stream_terminal must be a boolean")
        if self.ensure_stream_terminal and self.result.status != "succeeded":
            raise ValueError("only recovered success can repair a stream terminal")


@dataclass(frozen=True, slots=True)
class PrivateRunExecution:
    context: PrivateWorkContext
    run: PrivateRunRecord
    snapshot: PersistedRunSnapshot
    checkpoint_namespace: str
    graph_input: object
    command: object | None
    config: dict[str, Any]
    interrupt_before: list[str] | Literal["*"] | None
    interrupt_after: list[str] | Literal["*"] | None
    stream_mode: list[str]
    stream_subgraphs: bool
    resume_from_checkpoint: bool = False
    runtime_kind: Literal["chat", "skill_builder"] = "chat"
    token_budget_usage: TokenBudgetUsageSnapshot | None = None

    def __post_init__(self) -> None:
        if self.token_budget_usage is not None and type(self.token_budget_usage) is not TokenBudgetUsageSnapshot:
            raise TypeError(
                "token_budget_usage must be a TokenBudgetUsageSnapshot or None",
            )


__all__ = [
    "AgentExecutionResult",
    "PrivateRunExecution",
    "RecoveredPrivateRunTerminal",
]
