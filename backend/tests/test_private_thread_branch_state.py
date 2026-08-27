"""Private Thread branch state-authority boundaries."""

from app.private_work.thread_service import _copyable_branch_state_values
from deerflow.agents.middlewares.token_budget_middleware import (
    TOKEN_BUDGET_USAGE_STATE_KEY,
)
from deerflow.agents.provider_request_contract import (
    CONTEXT_COMPACTION_RECEIPT_STATE_KEY,
    CONTEXT_PROJECTION_SNAPSHOT_STATE_KEY,
)


def test_branch_does_not_copy_run_bound_token_budget_authority() -> None:
    values = {
        "messages": ["visible history"],
        "business_state": {"usage": "business instructions"},
        TOKEN_BUDGET_USAGE_STATE_KEY: {
            "version": 1,
            "run_id": "source-logical-run",
            "input_tokens": 400,
            "output_tokens": 500,
            "total_tokens": 900,
        },
        CONTEXT_PROJECTION_SNAPSHOT_STATE_KEY: {"contract_version": 1},
        CONTEXT_COMPACTION_RECEIPT_STATE_KEY: {"contract_version": 1},
    }

    projected = _copyable_branch_state_values(values)

    assert projected == {
        "messages": ["visible history"],
        "business_state": {"usage": "business instructions"},
    }
    assert values[TOKEN_BUDGET_USAGE_STATE_KEY]["total_tokens"] == 900
