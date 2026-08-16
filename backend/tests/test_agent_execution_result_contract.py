from __future__ import annotations

import pytest

from app.reliability.run_execution.contracts import AgentExecutionResult


def test_success_may_carry_exact_suspended_approval_anchor() -> None:
    result = AgentExecutionResult.succeeded(
        suspended_approval_id="approval-current-run",
    )

    assert result.status == "succeeded"
    assert result.suspended_approval_id == "approval-current-run"


@pytest.mark.parametrize("status", ["cancelled", "failed"])
def test_non_success_cannot_carry_suspended_approval_anchor(
    status: str,
) -> None:
    with pytest.raises(
        ValueError,
        match="only successful approval suspension",
    ):
        AgentExecutionResult(  # type: ignore[arg-type]
            status=status,
            public_error_code=("FAILED" if status == "failed" else None),
            suspended_approval_id="approval-current-run",
        )


@pytest.mark.parametrize("approval_id", ["", 1])
def test_success_rejects_invalid_suspended_approval_anchor(
    approval_id: object,
) -> None:
    with pytest.raises(
        ValueError,
        match="suspended_approval_id must be a non-empty string",
    ):
        AgentExecutionResult.succeeded(  # type: ignore[arg-type]
            suspended_approval_id=approval_id,
        )
