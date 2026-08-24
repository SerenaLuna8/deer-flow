from __future__ import annotations

from types import SimpleNamespace

from app.reliability.execution import (
    PrivateRunJobHandler,
    PrivateRunUsageSnapshot,
    RunAgentPrivateExecutor,
)
from deerflow.persistence.jobs.sql import _dead_error_code_for_failure
from deerflow.runtime.events.models import (
    STREAM_TERMINAL_ERROR_CODES,
    StoredStreamFrame,
    StreamFrame,
)


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


def test_every_durable_error_terminal_is_nonretryable() -> None:
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
    current_upload = StoredStreamFrame(
        id="3",
        thread_id="thread-1",
        run_id="run-3",
        event="end",
        data={
            "status": "error",
            "error_code": "CURRENT_UPLOAD_UNAVAILABLE",
        },
        terminal=True,
    )
    provider_unavailable = StoredStreamFrame(
        id="4",
        thread_id="thread-1",
        run_id="run-4",
        event="end",
        data={
            "status": "error",
            "error_code": "LLM_PROVIDER_UNAVAILABLE",
        },
        terminal=True,
    )
    loop_safety = StoredStreamFrame(
        id="5",
        thread_id="thread-1",
        run_id="run-5",
        event="end",
        data={
            "status": "error",
            "error_code": "LOOP_SAFETY_LIMIT",
        },
        terminal=True,
    )

    typed_result = PrivateRunJobHandler._terminal_result(typed)
    legacy_result = PrivateRunJobHandler._terminal_result(legacy)
    current_upload_result = PrivateRunJobHandler._terminal_result(current_upload)
    provider_unavailable_result = PrivateRunJobHandler._terminal_result(provider_unavailable)
    loop_safety_result = PrivateRunJobHandler._terminal_result(loop_safety)

    assert typed_result.public_error_code == "MODEL_OUTPUT_LIMIT"
    assert typed_result.retryable is False
    assert legacy_result.public_error_code == "AGENT_EXECUTION_FAILED"
    assert legacy_result.retryable is False
    assert current_upload_result.public_error_code == "CURRENT_UPLOAD_UNAVAILABLE"
    assert current_upload_result.retryable is False
    assert provider_unavailable_result.public_error_code == "LLM_PROVIDER_UNAVAILABLE"
    assert provider_unavailable_result.retryable is False
    assert loop_safety_result.public_error_code == "LOOP_SAFETY_LIMIT"
    assert loop_safety_result.retryable is False


def test_stream_terminal_error_code_is_a_closed_contract() -> None:
    assert STREAM_TERMINAL_ERROR_CODES == {
        "MODEL_OUTPUT_LIMIT",
        "LOOP_SAFETY_LIMIT",
        "OUTPUT_DELIVERY_INCOMPLETE",
        "CURRENT_UPLOAD_UNAVAILABLE",
        "LLM_QUOTA_EXCEEDED",
        "LLM_AUTHENTICATION_FAILED",
        "LLM_PROVIDER_BUSY",
        "LLM_PROVIDER_UNAVAILABLE",
        "LLM_REQUEST_FAILED",
        "LLM_CIRCUIT_OPEN",
        "RUN_POLICY_STALE",
        "TOOL_CALL_CONTROL_STATE_INVALID",
        "LOOP_FINALIZATION_FAILED",
        "PROVIDER_REQUEST_USAGE_UNSUPPORTED",
        "PROVIDER_REQUEST_PROFILE_DRIFT",
        "PROVIDER_REQUEST_CAPACITY_EXCEEDED",
        "SIDE_EFFECT_STATE_UNKNOWN",
    }
    assert StreamFrame.end(
        status="error",
        error_code="MODEL_OUTPUT_LIMIT",
    ).data == {
        "status": "error",
        "error_code": "MODEL_OUTPUT_LIMIT",
    }
    assert StreamFrame.end(
        status="error",
        error_code="LOOP_SAFETY_LIMIT",
    ).data == {
        "status": "error",
        "error_code": "LOOP_SAFETY_LIMIT",
    }
    assert StreamFrame.end(
        status="error",
        error_code="SIDE_EFFECT_STATE_UNKNOWN",
    ).data == {
        "status": "error",
        "error_code": "SIDE_EFFECT_STATE_UNKNOWN",
    }
    assert StreamFrame.end(
        status="error",
        error_code="OUTPUT_DELIVERY_INCOMPLETE",
    ).data == {
        "status": "error",
        "error_code": "OUTPUT_DELIVERY_INCOMPLETE",
    }
    assert StreamFrame.end(
        status="error",
        error_code="CURRENT_UPLOAD_UNAVAILABLE",
    ).data == {
        "status": "error",
        "error_code": "CURRENT_UPLOAD_UNAVAILABLE",
    }
    assert StreamFrame.end(
        status="error",
        error_code="LLM_PROVIDER_UNAVAILABLE",
    ).data == {
        "status": "error",
        "error_code": "LLM_PROVIDER_UNAVAILABLE",
    }

    import pytest

    with pytest.raises(ValueError, match="error code"):
        StreamFrame.end(status="error", error_code="RAW_PROVIDER_ERROR")


def test_executor_outcome_mapping_is_nonretryable_for_output_limit() -> None:
    result = RunAgentPrivateExecutor._terminal_failure_result(
        "MODEL_OUTPUT_LIMIT",
        attempt_usage=_usage(),
    )

    assert result.public_error_code == "MODEL_OUTPUT_LIMIT"
    assert result.retryable is False
    assert result.attempt_usage == _usage()


def test_executor_generic_durable_terminal_is_nonretryable() -> None:
    result = RunAgentPrivateExecutor._terminal_failure_result(
        "AGENT_EXECUTION_FAILED",
        attempt_usage=_usage(),
    )

    assert result.public_error_code == "AGENT_EXECUTION_FAILED"
    assert result.retryable is False
    assert result.attempt_usage == _usage()


def test_executor_loop_safety_terminal_is_nonretryable() -> None:
    result = RunAgentPrivateExecutor._terminal_failure_result(
        "LOOP_SAFETY_LIMIT",
        attempt_usage=_usage(),
    )

    assert result.public_error_code == "LOOP_SAFETY_LIMIT"
    assert result.retryable is False
    assert result.attempt_usage == _usage()


def test_executor_current_upload_terminal_preserves_public_error_code() -> None:
    result = RunAgentPrivateExecutor._terminal_failure_result(
        "CURRENT_UPLOAD_UNAVAILABLE",
        attempt_usage=_usage(),
    )

    assert result.public_error_code == "CURRENT_UPLOAD_UNAVAILABLE"
    assert result.retryable is False
    assert result.attempt_usage == _usage()


def test_executor_provider_terminal_is_typed_and_nonretryable() -> None:
    result = RunAgentPrivateExecutor._terminal_failure_result(
        "LLM_PROVIDER_UNAVAILABLE",
        attempt_usage=_usage(),
    )

    assert result.public_error_code == "LLM_PROVIDER_UNAVAILABLE"
    assert result.retryable is False
    assert result.attempt_usage == _usage()


def test_unknown_retry_safety_preserves_reviewed_nonretryable_terminal_codes() -> None:
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
            public_error_code="LOOP_SAFETY_LIMIT",
            retryable=False,
        )
        == "LOOP_SAFETY_LIMIT"
    )
    assert (
        _dead_error_code_for_failure(
            retry_safety="unknown",
            public_error_code="OUTPUT_DELIVERY_INCOMPLETE",
            retryable=False,
        )
        == "OUTPUT_DELIVERY_INCOMPLETE"
    )
    assert (
        _dead_error_code_for_failure(
            retry_safety="unknown",
            public_error_code="CURRENT_UPLOAD_UNAVAILABLE",
            retryable=False,
        )
        == "CURRENT_UPLOAD_UNAVAILABLE"
    )
    assert (
        _dead_error_code_for_failure(
            retry_safety="unknown",
            public_error_code="LLM_PROVIDER_UNAVAILABLE",
            retryable=False,
        )
        == "LLM_PROVIDER_UNAVAILABLE"
    )
    assert (
        _dead_error_code_for_failure(
            retry_safety="unknown",
            public_error_code="ANOTHER_FAILURE",
            retryable=False,
        )
        == "SIDE_EFFECT_STATE_UNKNOWN"
    )
