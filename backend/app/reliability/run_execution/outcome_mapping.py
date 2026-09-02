"""Pure private Run outcome mapping without database or authority side effects."""

from __future__ import annotations

from app.private_work.run_repository import PrivateRunUsageSnapshot
from app.reliability.run_execution.contracts import AgentExecutionResult
from app.reliability.run_execution.errors import (
    AmbiguousExternalSideEffect,
    PermanentExecutionError,
)
from deerflow.error_codes import PublicRunErrorCode
from deerflow.runtime import RunRecord
from deerflow.runtime.events.models import STREAM_TERMINAL_ERROR_CODES
from deerflow.runtime.runs.execution_contracts import (
    RunAgentOutcome,
    RunAgentUsageSnapshot,
)
from deerflow.token_budget_usage import TokenBudgetUsageRecorder


def usage_snapshot(
    record: RunRecord,
    recorder: TokenBudgetUsageRecorder | None = None,
) -> PrivateRunUsageSnapshot:
    return PrivateRunUsageSnapshot(
        total_input_tokens=record.total_input_tokens,
        total_output_tokens=record.total_output_tokens,
        total_tokens=record.total_tokens,
        llm_call_count=record.llm_call_count,
        lead_agent_tokens=record.lead_agent_tokens,
        subagent_tokens=record.subagent_tokens,
        middleware_tokens=record.middleware_tokens,
        token_usage_by_model=record.token_usage_by_model,
        token_budget_usage=(recorder.snapshot() if recorder is not None else None),
    )


def outcome_usage_snapshot(
    usage: RunAgentUsageSnapshot,
    recorder: TokenBudgetUsageRecorder | None = None,
) -> PrivateRunUsageSnapshot:
    return PrivateRunUsageSnapshot(
        total_input_tokens=usage.total_input_tokens,
        total_output_tokens=usage.total_output_tokens,
        total_tokens=usage.total_tokens,
        llm_call_count=usage.llm_call_count,
        lead_agent_tokens=usage.lead_agent_tokens,
        subagent_tokens=usage.subagent_tokens,
        middleware_tokens=usage.middleware_tokens,
        token_usage_by_model={model_name: dict(counters) for model_name, counters in usage.token_usage_by_model.items()},
        token_budget_usage=(usage.token_budget_usage if usage.token_budget_usage is not None else (recorder.snapshot() if recorder is not None else None)),
    )


def terminal_failure_result(
    public_error_code: str,
    *,
    attempt_usage: PrivateRunUsageSnapshot,
) -> AgentExecutionResult:
    return AgentExecutionResult.failed(
        public_error_code,
        retryable=False,
        attempt_usage=attempt_usage,
        durable_terminal=True,
    )


def output_limit_error(
    record: RunRecord | None,
    *,
    lease_lost: bool,
    recorder: TokenBudgetUsageRecorder | None = None,
) -> PermanentExecutionError:
    return PermanentExecutionError(
        PublicRunErrorCode.MODEL_OUTPUT_LIMIT.value,
        attempt_usage=(usage_snapshot(record, recorder) if record is not None and not lease_lost else None),
    )


def map_run_agent_outcome(
    outcome: RunAgentOutcome,
    *,
    attempt_usage: PrivateRunUsageSnapshot,
    authorization_revoked: bool,
    cancel_requested: bool,
    ambiguous_side_effect: bool,
) -> AgentExecutionResult:
    """Map an immutable Worker outcome plus boundary facts onto the Job result.

    Priority is unchanged from the inline executor: revoked authority wins,
    then a failed outcome (durable stream terminal codes are non-retryable and
    an ambiguous external side effect raises), then an ordinary cancel
    request, then the Worker's own success or cancellation.
    """
    if authorization_revoked:
        return AgentExecutionResult.cancelled(attempt_usage=attempt_usage)
    if outcome.status == "failed":
        error_code = outcome.public_error_code
        if error_code is None:
            raise RuntimeError("failed Run Agent outcome has no error code")
        if error_code in STREAM_TERMINAL_ERROR_CODES:
            return terminal_failure_result(error_code, attempt_usage=attempt_usage)
        if ambiguous_side_effect:
            raise AmbiguousExternalSideEffect(attempt_usage=attempt_usage)
        return terminal_failure_result(error_code, attempt_usage=attempt_usage)
    if cancel_requested:
        return AgentExecutionResult.cancelled(attempt_usage=attempt_usage)
    if outcome.status == "succeeded":
        return AgentExecutionResult.succeeded(
            attempt_usage=attempt_usage,
            suspended_approval_id=outcome.suspended_approval_id,
        )
    if outcome.status == "cancelled":
        return AgentExecutionResult.cancelled(attempt_usage=attempt_usage)
    raise RuntimeError("Run Agent returned an unsupported outcome")


__all__ = [
    "map_run_agent_outcome",
    "outcome_usage_snapshot",
    "output_limit_error",
    "terminal_failure_result",
    "usage_snapshot",
]
