from __future__ import annotations

from types import SimpleNamespace

from app.reliability.execution import (
    PrivateRunJobHandler,
    PrivateRunUsageSnapshot,
    RunAgentPrivateExecutor,
)
from deerflow.persistence.jobs.sql import _dead_error_code_for_failure
from deerflow.runtime.events.models import StoredStreamFrame, StreamFrame


def _usage() -> PrivateRunUsageSnapshot:
    return PrivateRunUsageSnapshot(
        total_input_tokens=11,
        total_output_tokens=7,
        total_tokens=18,
        llm_call_count=2,
        lead_agent_tokens=18,
        subagent_tokens=0,
        middleware_tokens=0,
        token_usage_by_model={},
    )


def test_permanent_output_limit_preserves_attempt_usage_for_job_settlement() -> None:
    record = SimpleNamespace(
        total_input_tokens=11,
        total_output_tokens=7,
        total_tokens=18,
        llm_call_count=2,
        lead_agent_tokens=18,
        subagent_tokens=0,
        middleware_tokens=0,
        token_usage_by_model={},
    )
    error = RunAgentPrivateExecutor._output_limit_error(
        record,
        lease_lost=False,
    )
    result = PrivateRunJobHandler._permanent_failure(error)

    assert error.public_error_code == "MODEL_OUTPUT_LIMIT"
    assert error.attempt_usage == _usage()
    assert result.public_error_code == "MODEL_OUTPUT_LIMIT"
    assert result.retryable is False
    assert result.attempt_usage == _usage()


def test_typed_terminal_is_nonretryable_and_legacy_error_remains_generic() -> None:
    typed = StoredStreamFrame(
        id="1",
        thread_id="thread-1",
        run_id="run-1",
        event="end",
        data={"status": "error", "error_code": "MODEL_OUTPUT_LIMIT"},
        terminal=True,
    )
    legacy = StoredStreamFrame(
        id="2",
        thread_id="thread-1",
        run_id="run-2",
        event="end",
        data={"status": "error"},
        terminal=True,
    )

    typed_result = PrivateRunJobHandler._terminal_result(typed)
    legacy_result = PrivateRunJobHandler._terminal_result(legacy)

    assert typed_result.public_error_code == "MODEL_OUTPUT_LIMIT"
    assert typed_result.retryable is False
    assert legacy_result.public_error_code == "AGENT_EXECUTION_FAILED"
    assert legacy_result.retryable is True


def test_stream_terminal_error_code_is_a_closed_contract() -> None:
    assert StreamFrame.end(
        status="error",
        error_code="MODEL_OUTPUT_LIMIT",
    ).data == {
        "status": "error",
        "error_code": "MODEL_OUTPUT_LIMIT",
    }

    import pytest

    with pytest.raises(ValueError, match="error code"):
        StreamFrame.end(status="error", error_code="RAW_PROVIDER_ERROR")


def test_executor_record_mapping_is_nonretryable_for_output_limit() -> None:
    # The executor's post-run branch uses this exact closed mapping; retain an
    # explicit contract here so a generic AGENT_EXECUTION_FAILED fallback does
    # not silently re-enable whole-Run retries.
    import inspect

    source = inspect.getsource(RunAgentPrivateExecutor._execute_with_trace)
    assert "PublicRunErrorCode.MODEL_OUTPUT_LIMIT.value" in source
    assert "retryable=False" in source


def test_unknown_retry_safety_preserves_only_nonretryable_output_limit() -> None:
    assert (
        _dead_error_code_for_failure(
            retry_safety="unknown",
            public_error_code="MODEL_OUTPUT_LIMIT",
            retryable=False,
        )
        == "MODEL_OUTPUT_LIMIT"
    )
    assert (
        _dead_error_code_for_failure(
            retry_safety="unknown",
            public_error_code="MODEL_OUTPUT_LIMIT",
            retryable=True,
        )
        == "SIDE_EFFECT_STATE_UNKNOWN"
    )
    assert (
        _dead_error_code_for_failure(
            retry_safety="unknown",
            public_error_code="ANOTHER_FAILURE",
            retryable=False,
        )
        == "SIDE_EFFECT_STATE_UNKNOWN"
    )
