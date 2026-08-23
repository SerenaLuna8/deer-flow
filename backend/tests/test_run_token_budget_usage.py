from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from app.gateway.private_work_schemas import PrivateRunCreateRequest
from app.gateway.routers.private_work import _run_response
from app.private_work.run_admission import PrivateRunAdmissionService
from app.private_work.run_metadata import (
    RUN_TOKEN_BUDGET_USAGE_KEY,
    RunTokenBudgetUsageInvalid,
    run_token_budget_usage,
    with_run_token_budget_usage,
)
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRecord
from deerflow.agents.middlewares.token_budget_middleware import (
    TOKEN_BUDGET_USAGE_STATE_KEY,
    TokenBudgetMiddleware,
)
from deerflow.config.token_budget_config import TokenBudgetConfig
from deerflow.runtime.context_carrier import RuntimeContextCarrier
from deerflow.runtime.context_keys import RuntimeContextKeys
from deerflow.token_budget_usage import (
    TokenBudgetUsageConflict,
    TokenBudgetUsageRecorder,
    TokenBudgetUsageSnapshot,
    dominant_token_budget_usage,
)


def _usage(
    run_id: str,
    input_tokens: int,
    output_tokens: int,
) -> TokenBudgetUsageSnapshot:
    return TokenBudgetUsageSnapshot(
        run_id=run_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )


def _budget() -> TokenBudgetMiddleware:
    return TokenBudgetMiddleware(
        TokenBudgetConfig(
            enabled=True,
            max_tokens=1_000,
            warn_threshold=0.8,
            hard_stop_threshold=1.0,
        )
    )


def _runtime(
    run_id: str,
    recorder: TokenBudgetUsageRecorder,
) -> SimpleNamespace:
    return SimpleNamespace(
        context={
            RuntimeContextKeys.RUN_ID: run_id,
            RuntimeContextKeys.TOKEN_BUDGET_USAGE_RECORDER: recorder,
            RuntimeContextKeys.TOKEN_USAGE_TRACKING_ENABLED: False,
        }
    )


def test_run_token_budget_metadata_is_strict_monotonic_and_server_only() -> None:
    run_id = "run-budget-metadata"
    prior = _usage(run_id, 450, 450)
    current = _usage(run_id, 500, 600)

    metadata = with_run_token_budget_usage({"visible": True}, usage=prior)
    assert run_token_budget_usage(metadata, run_id=run_id) == prior
    metadata = with_run_token_budget_usage(metadata, usage=current)
    assert run_token_budget_usage(metadata, run_id=run_id) == current

    for malformed in (
        None,
        {},
        {
            "schema_version": "token.budget.usage.v1",
            "run_id": run_id,
            "input_tokens": True,
            "output_tokens": 0,
            "total_tokens": 1,
        },
        {
            "schema_version": "token.budget.usage.v1",
            "run_id": run_id,
            "input_tokens": 1,
            "output_tokens": 1,
            "total_tokens": 3,
        },
        {
            "schema_version": "token.budget.usage.v1",
            "run_id": run_id,
            "input_tokens": 1,
            "output_tokens": 1,
            "total_tokens": 2,
            "extra": 1,
        },
    ):
        with pytest.raises(RunTokenBudgetUsageInvalid):
            run_token_budget_usage(
                {RUN_TOKEN_BUDGET_USAGE_KEY: malformed},
                run_id=run_id,
            )

    with pytest.raises(RunTokenBudgetUsageInvalid):
        with_run_token_budget_usage(metadata, usage=_usage(run_id, 400, 700))
    with pytest.raises(RunTokenBudgetUsageInvalid):
        with_run_token_budget_usage(metadata, usage=_usage("other-run", 600, 600))

    body = PrivateRunCreateRequest.model_validate(
        {
            "metadata": {
                RUN_TOKEN_BUDGET_USAGE_KEY: current.as_dict(),
                "visible": True,
            }
        }
    )
    assert body.metadata == {"visible": True}

    trusted_recorder = TokenBudgetUsageRecorder(current)
    runtime_context = RuntimeContextCarrier(
        token_budget_usage_recorder=trusted_recorder,
    ).build(
        {
            RuntimeContextKeys.TOKEN_BUDGET_USAGE_RECORDER: object(),
        }
    )
    assert runtime_context[RuntimeContextKeys.TOKEN_BUDGET_USAGE_RECORDER] is trusted_recorder


def test_run_token_budget_metadata_is_not_projected_publicly() -> None:
    import uuid
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    run_id = "run-budget-response"
    record = PrivateRunRecord(
        run_id=run_id,
        thread_id="thread-budget-response",
        project_id=uuid.uuid4(),
        owner_user_id=str(uuid.uuid4()),
        assistant_id=None,
        status="pending",
        multitask_strategy="reject",
        metadata=with_run_token_budget_usage(
            {"visible": "kept"},
            usage=_usage(run_id, 500, 600),
        ),
        kwargs={"input": {"messages": []}},
        origin_trace_id=uuid.uuid4().hex,
        error=None,
        model_name="test-model",
        created_at=now,
        updated_at=now,
    )

    assert _run_response(record).metadata == {"visible": "kept"}
    assert PrivateRunAdmissionService._is_same_request(
        record,
        thread_id=record.thread_id,
        request=PrivateRunCreate(
            run_id=record.run_id,
            metadata={"visible": "kept"},
            kwargs={"input": {"messages": []}},
        ),
    )


def test_same_run_recovery_requires_two_dimensional_dominance() -> None:
    run_id = "run-budget-dominance"
    baseline = _usage(run_id, 450, 450)
    checkpoint_ahead = _usage(run_id, 500, 600)

    assert dominant_token_budget_usage(baseline, checkpoint_ahead) == checkpoint_ahead
    assert dominant_token_budget_usage(checkpoint_ahead, baseline) == checkpoint_ahead
    with pytest.raises(TokenBudgetUsageConflict):
        dominant_token_budget_usage(
            _usage(run_id, 900, 100),
            _usage(run_id, 100, 900),
        )
    with pytest.raises(TokenBudgetUsageConflict):
        dominant_token_budget_usage(
            baseline,
            _usage("foreign-run", 500, 500),
        )


def test_tracking_disabled_missing_checkpoint_inherits_private_run_baseline() -> None:
    run_id = "run-budget-missing-checkpoint"
    recorder = TokenBudgetUsageRecorder(_usage(run_id, 450, 450))
    runtime = _runtime(run_id, recorder)
    budget = _budget()

    before = budget.before_agent({"messages": []}, runtime)
    assert before == {
        TOKEN_BUDGET_USAGE_STATE_KEY: {
            "version": 1,
            "run_id": run_id,
            "input_tokens": 450,
            "output_tokens": 450,
            "total_tokens": 900,
        }
    }
    current = AIMessage(
        id="current-response",
        content="",
        tool_calls=[
            {
                "name": "lookup",
                "args": {},
                "id": "current-call",
                "type": "tool_call",
            }
        ],
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 100,
            "total_tokens": 200,
        },
    )
    stopped = budget.after_model({"messages": [current]}, runtime)

    assert stopped is not None
    assert stopped[TOKEN_BUDGET_USAGE_STATE_KEY]["total_tokens"] == 1_100
    assert stopped["messages"][0].tool_calls == []
    assert recorder.snapshot() == _usage(run_id, 550, 550)


def test_checkpoint_ahead_of_run_baseline_wins_after_reclaim() -> None:
    run_id = "run-budget-checkpoint-ahead"
    recorder = TokenBudgetUsageRecorder(_usage(run_id, 300, 300))
    runtime = _runtime(run_id, recorder)
    budget = _budget()
    checkpoint = {
        "version": 1,
        "run_id": run_id,
        "input_tokens": 450,
        "output_tokens": 450,
        "total_tokens": 900,
    }

    assert (
        budget.before_agent(
            {
                "messages": [],
                TOKEN_BUDGET_USAGE_STATE_KEY: checkpoint,
            },
            runtime,
        )
        is None
    )
    assert recorder.snapshot() == _usage(run_id, 450, 450)
    current = AIMessage(
        id="reclaimed-current-response",
        content="",
        tool_calls=[
            {
                "name": "lookup",
                "args": {},
                "id": "reclaimed-current-call",
                "type": "tool_call",
            }
        ],
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 100,
            "total_tokens": 200,
        },
    )
    stopped = budget.after_model({"messages": [current]}, runtime)
    assert stopped is not None
    assert stopped[TOKEN_BUDGET_USAGE_STATE_KEY]["total_tokens"] == 1_100
    assert stopped["messages"][0].tool_calls == []
    assert recorder.snapshot() == _usage(run_id, 550, 550)


def test_crossed_checkpoint_and_run_baseline_fail_closed() -> None:
    run_id = "run-budget-crossed"
    recorder = TokenBudgetUsageRecorder(_usage(run_id, 900, 100))
    runtime = _runtime(run_id, recorder)

    with pytest.raises(RuntimeError, match="inconsistent"):
        _budget().before_agent(
            {
                "messages": [],
                TOKEN_BUDGET_USAGE_STATE_KEY: {
                    "version": 1,
                    "run_id": run_id,
                    "input_tokens": 100,
                    "output_tokens": 900,
                    "total_tokens": 1_000,
                },
            },
            runtime,
        )
