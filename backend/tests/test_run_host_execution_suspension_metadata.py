from __future__ import annotations

import uuid

import pytest

from app.private_work.run_metadata import (
    HOST_EXECUTION_SUSPENSION_SCHEMA_VERSION,
    RUN_HOST_EXECUTION_SUSPENSION_KEY,
    RunHostExecutionSuspension,
    RunHostExecutionSuspensionInvalid,
    run_host_execution_suspension,
    with_run_host_execution_suspension,
)


def _marker() -> RunHostExecutionSuspension:
    return RunHostExecutionSuspension(
        approval_id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
        source_job_id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
        producing_attempt_id=uuid.UUID(
            "33333333-3333-4333-8333-333333333333",
        ),
    )


def test_host_execution_suspension_marker_round_trips() -> None:
    marker = _marker()
    metadata = with_run_host_execution_suspension(
        {"public": "preserved"},
        suspension=marker,
    )

    assert metadata["public"] == "preserved"
    assert run_host_execution_suspension(metadata) == marker


def test_host_execution_suspension_marker_cannot_change_producing_attempt() -> None:
    marker = _marker()
    metadata = with_run_host_execution_suspension({}, suspension=marker)

    with pytest.raises(RunHostExecutionSuspensionInvalid):
        with_run_host_execution_suspension(
            metadata,
            suspension=RunHostExecutionSuspension(
                approval_id=marker.approval_id,
                source_job_id=marker.source_job_id,
                producing_attempt_id=uuid.uuid4(),
            ),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("approval_id", 1),
        ("source_job_id", uuid.UUID("22222222-2222-4222-8222-222222222222")),
        ("producing_attempt_id", "33333333-3333-4333-8333-33333333333A"),
    ],
)
def test_host_execution_suspension_marker_rejects_noncanonical_coordinates(
    field: str,
    value: object,
) -> None:
    payload = {
        "schema_version": HOST_EXECUTION_SUSPENSION_SCHEMA_VERSION,
        "approval_id": "11111111-1111-4111-8111-111111111111",
        "source_job_id": "22222222-2222-4222-8222-222222222222",
        "producing_attempt_id": "33333333-3333-4333-8333-333333333333",
    }
    payload[field] = value

    with pytest.raises(RunHostExecutionSuspensionInvalid):
        run_host_execution_suspension(
            {RUN_HOST_EXECUTION_SUSPENSION_KEY: payload},
        )
