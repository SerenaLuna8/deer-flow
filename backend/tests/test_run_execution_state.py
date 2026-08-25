from __future__ import annotations

from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.private_work.run_execution_state import (
    ActiveAttemptExecutionFacts,
    RunExecutionState,
    RunExecutionStateFacts,
    RunExecutionStatePolicy,
    RunExecutionStateUnavailable,
    SettledAttemptExecutionFacts,
    project_run_execution_state,
)

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
CREATED = NOW - timedelta(minutes=10)
UPDATED = NOW - timedelta(minutes=2)
STARTED = NOW - timedelta(minutes=8)
COMPLETED = NOW - timedelta(minutes=1)
POLICY = RunExecutionStatePolicy(worker_fresh_for_seconds=60)


def _facts(**changes: object) -> RunExecutionStateFacts:
    base = RunExecutionStateFacts(
        observed_at=NOW,
        run_status="pending",
        run_execution_started_at=None,
        run_job_identity_exact=True,
        job_status="queued",
        job_created_at=CREATED,
        job_updated_at=UPDATED,
        job_available_at=CREATED,
        job_completed_at=None,
        attempt_count=0,
        max_attempts=3,
        retry_safety="safe",
        cancel_requested_at=None,
        lease_expires_at=None,
        active_lease_state="absent",
        active_attempt_lease_state="absent",
        run_lease_state="absent",
        lease_worker_identity_state="absent",
        active_attempt=None,
        latest_attempt=None,
        lease_worker_heartbeat_at=None,
        eligible_worker_exists=True,
    )
    return replace(base, **changes)


def test_terminal_projection_requires_legal_pair_and_hides_authority_facts() -> None:
    facts = _facts(
        run_status="success",
        run_execution_started_at=STARTED,
        job_status="succeeded",
        job_completed_at=COMPLETED,
        attempt_count=1,
        latest_attempt=SettledAttemptExecutionFacts(
            attempt_number=1,
            outcome="succeeded",
            finished_at=COMPLETED,
        ),
    )

    state = project_run_execution_state(facts, POLICY)

    assert type(state) is RunExecutionState
    assert asdict(state) == {
        "phase": "terminal",
        "observed_at": NOW,
        "phase_started_at": COMPLETED,
        "execution_started_at": STARTED,
        "retry_at": None,
        "run_status": "success",
    }

    assert (
        type(
            project_run_execution_state(
                replace(facts, job_status="failed"),
                POLICY,
            )
        )
        is RunExecutionStateUnavailable
    )


@pytest.mark.parametrize(
    (
        "expected_phase",
        "facts",
        "expected_phase_started_at",
        "expected_retry_at",
    ),
    [
        ("queued", _facts(), CREATED, None),
        (
            "waiting_for_worker",
            _facts(eligible_worker_exists=False),
            CREATED,
            None,
        ),
        (
            "starting",
            _facts(
                job_status="running",
                attempt_count=1,
                active_lease_state="exact",
                active_attempt_lease_state="exact",
                lease_expires_at=NOW + timedelta(minutes=2),
                active_attempt=ActiveAttemptExecutionFacts(
                    attempt_number=1,
                    started_at=STARTED,
                    execution_started_at=None,
                ),
                lease_worker_heartbeat_at=NOW - timedelta(seconds=30),
                lease_worker_identity_state="exact",
            ),
            STARTED,
            None,
        ),
        (
            "executing",
            _facts(
                run_status="running",
                run_execution_started_at=STARTED,
                job_status="running",
                attempt_count=1,
                active_lease_state="exact",
                active_attempt_lease_state="exact",
                run_lease_state="exact",
                lease_expires_at=NOW + timedelta(minutes=2),
                active_attempt=ActiveAttemptExecutionFacts(
                    attempt_number=1,
                    started_at=STARTED,
                    execution_started_at=STARTED + timedelta(seconds=5),
                ),
                lease_worker_heartbeat_at=NOW - timedelta(seconds=30),
                lease_worker_identity_state="exact",
            ),
            STARTED + timedelta(seconds=5),
            None,
        ),
        (
            "retry_wait",
            _facts(
                job_status="retry_wait",
                job_available_at=NOW + timedelta(minutes=3),
                attempt_count=1,
                latest_attempt=SettledAttemptExecutionFacts(
                    attempt_number=1,
                    outcome="retry",
                    finished_at=UPDATED,
                ),
            ),
            UPDATED,
            NOW + timedelta(minutes=3),
        ),
        (
            "waiting_for_lease_expiry",
            _facts(
                run_status="running",
                run_execution_started_at=STARTED,
                job_status="running",
                attempt_count=1,
                retry_safety="unsafe",
                active_lease_state="exact",
                active_attempt_lease_state="exact",
                run_lease_state="exact",
                lease_expires_at=NOW + timedelta(minutes=2),
                active_attempt=ActiveAttemptExecutionFacts(
                    attempt_number=1,
                    started_at=STARTED,
                    execution_started_at=STARTED + timedelta(seconds=5),
                ),
                lease_worker_heartbeat_at=NOW - timedelta(seconds=90),
                lease_worker_identity_state="exact",
            ),
            NOW - timedelta(seconds=30),
            NOW + timedelta(minutes=2),
        ),
        (
            "waiting_for_terminalization",
            _facts(
                run_status="running",
                run_execution_started_at=STARTED,
                job_status="running",
                attempt_count=1,
                retry_safety="unsafe",
                active_lease_state="exact",
                active_attempt_lease_state="exact",
                run_lease_state="exact",
                lease_expires_at=NOW - timedelta(seconds=10),
                active_attempt=ActiveAttemptExecutionFacts(
                    attempt_number=1,
                    started_at=STARTED,
                    execution_started_at=STARTED + timedelta(seconds=5),
                ),
                lease_worker_heartbeat_at=NOW - timedelta(minutes=2),
                lease_worker_identity_state="exact",
                eligible_worker_exists=True,
            ),
            NOW - timedelta(seconds=10),
            None,
        ),
        (
            "waiting_for_recovery",
            _facts(
                run_status="running",
                run_execution_started_at=STARTED,
                job_status="running",
                attempt_count=1,
                active_lease_state="exact",
                active_attempt_lease_state="exact",
                run_lease_state="exact",
                lease_expires_at=NOW - timedelta(seconds=10),
                active_attempt=ActiveAttemptExecutionFacts(
                    attempt_number=1,
                    started_at=STARTED,
                    execution_started_at=STARTED + timedelta(seconds=5),
                ),
                lease_worker_heartbeat_at=NOW - timedelta(minutes=2),
                lease_worker_identity_state="exact",
                eligible_worker_exists=True,
            ),
            NOW - timedelta(seconds=10),
            None,
        ),
        (
            "recovering",
            _facts(
                run_execution_started_at=STARTED,
                job_status="leased",
                attempt_count=2,
                active_lease_state="exact",
                active_attempt_lease_state="exact",
                lease_expires_at=NOW + timedelta(minutes=2),
                active_attempt=ActiveAttemptExecutionFacts(
                    attempt_number=2,
                    started_at=NOW - timedelta(seconds=20),
                    execution_started_at=None,
                ),
                latest_attempt=SettledAttemptExecutionFacts(
                    attempt_number=1,
                    outcome="lease_lost",
                    finished_at=NOW - timedelta(seconds=30),
                ),
                lease_worker_heartbeat_at=NOW - timedelta(seconds=5),
                lease_worker_identity_state="exact",
            ),
            NOW - timedelta(seconds=20),
            None,
        ),
        (
            "cancelling",
            _facts(
                job_status="retry_wait",
                job_available_at=NOW + timedelta(minutes=3),
                attempt_count=1,
                cancel_requested_at=NOW - timedelta(seconds=15),
                latest_attempt=SettledAttemptExecutionFacts(
                    attempt_number=1,
                    outcome="retry",
                    finished_at=UPDATED,
                ),
            ),
            NOW - timedelta(seconds=15),
            None,
        ),
    ],
)
def test_nonterminal_phase_matrix_is_mutually_exclusive(
    expected_phase: str,
    facts: RunExecutionStateFacts,
    expected_phase_started_at: datetime | None,
    expected_retry_at: datetime | None,
) -> None:
    state = project_run_execution_state(facts, POLICY)

    assert type(state) is RunExecutionState
    assert state.phase == expected_phase
    assert state.phase_started_at == expected_phase_started_at
    assert state.retry_at == expected_retry_at
    assert state.execution_started_at == facts.run_execution_started_at
    assert (
        type(
            project_run_execution_state(
                replace(facts, run_job_identity_exact=False),
                POLICY,
            )
        )
        is RunExecutionStateUnavailable
    )


@pytest.mark.parametrize(
    ("run_status", "job_status", "attempt_outcome"),
    [
        ("success", "succeeded", "succeeded"),
        ("interrupted", "cancelled", "cancelled"),
        ("error", "failed", "failed"),
        ("error", "dead", "dead"),
        ("timeout", "failed", "failed"),
        ("timeout", "dead", "dead"),
    ],
)
def test_every_legal_terminal_pair_wins_over_cancel(
    run_status: str,
    job_status: str,
    attempt_outcome: str,
) -> None:
    state = project_run_execution_state(
        _facts(
            run_status=run_status,
            run_execution_started_at=STARTED,
            job_status=job_status,
            job_completed_at=COMPLETED,
            attempt_count=1,
            cancel_requested_at=UPDATED,
            latest_attempt=SettledAttemptExecutionFacts(
                attempt_number=1,
                outcome=attempt_outcome,
                finished_at=COMPLETED,
            ),
        ),
        POLICY,
    )

    assert type(state) is RunExecutionState
    assert state.phase == "terminal"
    assert state.phase_started_at == COMPLETED


def _starting_facts(**changes: Any) -> RunExecutionStateFacts:
    return replace(
        _facts(
            job_status="running",
            attempt_count=1,
            active_lease_state="exact",
            active_attempt_lease_state="exact",
            lease_expires_at=NOW + timedelta(minutes=2),
            active_attempt=ActiveAttemptExecutionFacts(
                attempt_number=1,
                started_at=STARTED,
                execution_started_at=None,
            ),
            lease_worker_identity_state="exact",
            lease_worker_heartbeat_at=NOW - timedelta(seconds=30),
        ),
        **changes,
    )


def _executing_facts(**changes: Any) -> RunExecutionStateFacts:
    return replace(
        _facts(
            run_status="running",
            run_execution_started_at=STARTED,
            job_status="running",
            attempt_count=1,
            active_lease_state="exact",
            active_attempt_lease_state="exact",
            run_lease_state="exact",
            lease_expires_at=NOW + timedelta(minutes=2),
            active_attempt=ActiveAttemptExecutionFacts(
                attempt_number=1,
                started_at=STARTED,
                execution_started_at=STARTED + timedelta(seconds=5),
            ),
            lease_worker_identity_state="exact",
            lease_worker_heartbeat_at=NOW - timedelta(seconds=30),
        ),
        **changes,
    )


@pytest.mark.parametrize(
    "facts",
    [
        _facts(run_job_identity_exact=False),
        _facts(active_lease_state="mismatch"),
        _starting_facts(active_attempt_lease_state="mismatch"),
        _starting_facts(lease_worker_identity_state="mismatch"),
        _executing_facts(run_lease_state="mismatch"),
        _starting_facts(
            active_attempt=ActiveAttemptExecutionFacts(
                attempt_number=2,
                started_at=STARTED,
                execution_started_at=None,
            )
        ),
        _facts(job_status="running"),
        _executing_facts(
            active_attempt=ActiveAttemptExecutionFacts(
                attempt_number=1,
                started_at=STARTED,
                execution_started_at=None,
            )
        ),
        _facts(observed_at=NOW.replace(tzinfo=None)),
    ],
)
def test_authority_or_clock_mismatch_is_typed_unavailable(
    facts: RunExecutionStateFacts,
) -> None:
    state = project_run_execution_state(facts, POLICY)

    assert state == RunExecutionStateUnavailable(observed_at=facts.observed_at)


@pytest.mark.parametrize(
    ("heartbeat_at", "worker_identity", "expected_phase", "expected_started_at"),
    [
        (
            NOW - timedelta(seconds=60),
            "exact",
            "starting",
            STARTED,
        ),
        (
            NOW - timedelta(seconds=60, microseconds=1),
            "exact",
            "waiting_for_lease_expiry",
            NOW - timedelta(microseconds=1),
        ),
        (None, "absent", "waiting_for_lease_expiry", None),
    ],
)
def test_worker_freshness_uses_database_clock_and_stable_boundary(
    heartbeat_at: datetime | None,
    worker_identity: str,
    expected_phase: str,
    expected_started_at: datetime | None,
) -> None:
    facts = _starting_facts(
        lease_worker_heartbeat_at=heartbeat_at,
        lease_worker_identity_state=worker_identity,
    )

    state = project_run_execution_state(facts, POLICY)

    assert type(state) is RunExecutionState
    assert state.phase == expected_phase
    assert state.phase_started_at == expected_started_at
    assert state.retry_at == (facts.lease_expires_at if expected_phase == "waiting_for_lease_expiry" else None)


@pytest.mark.parametrize(
    ("facts", "expected_phase", "expected_started_at"),
    [
        (
            _executing_facts(
                lease_expires_at=NOW - timedelta(seconds=10),
                lease_worker_heartbeat_at=NOW - timedelta(minutes=2),
                eligible_worker_exists=False,
            ),
            "waiting_for_worker",
            NOW - timedelta(seconds=10),
        ),
        (
            _facts(
                run_execution_started_at=STARTED,
                job_status="retry_wait",
                job_available_at=UPDATED,
                attempt_count=1,
                latest_attempt=SettledAttemptExecutionFacts(
                    attempt_number=1,
                    outcome="retry",
                    finished_at=UPDATED - timedelta(seconds=5),
                ),
            ),
            "waiting_for_recovery",
            UPDATED,
        ),
        (
            _facts(
                run_execution_started_at=STARTED,
                job_status="retry_wait",
                job_available_at=UPDATED,
                attempt_count=1,
                eligible_worker_exists=False,
                latest_attempt=SettledAttemptExecutionFacts(
                    attempt_number=1,
                    outcome="retry",
                    finished_at=UPDATED - timedelta(seconds=5),
                ),
            ),
            "waiting_for_worker",
            UPDATED,
        ),
        (
            _facts(
                run_execution_started_at=STARTED,
                job_status="retry_wait",
                job_available_at=UPDATED,
                attempt_count=1,
                retry_safety="unknown",
                latest_attempt=SettledAttemptExecutionFacts(
                    attempt_number=1,
                    outcome="retry",
                    finished_at=UPDATED - timedelta(seconds=5),
                ),
            ),
            "waiting_for_terminalization",
            UPDATED - timedelta(seconds=5),
        ),
        (
            _facts(
                run_execution_started_at=STARTED,
                job_status="retry_wait",
                job_available_at=UPDATED,
                attempt_count=1,
                max_attempts=1,
                latest_attempt=SettledAttemptExecutionFacts(
                    attempt_number=1,
                    outcome="retry",
                    finished_at=UPDATED - timedelta(seconds=5),
                ),
            ),
            "waiting_for_terminalization",
            UPDATED - timedelta(seconds=5),
        ),
    ],
)
def test_ready_recovery_branches_preserve_their_provenance(
    facts: RunExecutionStateFacts,
    expected_phase: str,
    expected_started_at: datetime,
) -> None:
    state = project_run_execution_state(facts, POLICY)

    assert type(state) is RunExecutionState
    assert state.phase == expected_phase
    assert state.phase_started_at == expected_started_at


def test_cancel_preempts_every_nonterminal_branch_without_resetting_run_time() -> None:
    facts = _executing_facts(
        cancel_requested_at=UPDATED,
        lease_expires_at=NOW - timedelta(seconds=10),
        retry_safety="unsafe",
        lease_worker_heartbeat_at=NOW - timedelta(minutes=2),
    )

    state = project_run_execution_state(facts, POLICY)

    assert type(state) is RunExecutionState
    assert state.phase == "cancelling"
    assert state.phase_started_at == UPDATED
    assert state.execution_started_at == STARTED
    assert state.retry_at is None


def test_future_retry_preempts_worker_and_recovery_classification() -> None:
    retry_at = NOW + timedelta(minutes=3)
    facts = _facts(
        run_execution_started_at=STARTED,
        job_status="retry_wait",
        job_available_at=retry_at,
        attempt_count=1,
        retry_safety="unknown",
        eligible_worker_exists=False,
        latest_attempt=SettledAttemptExecutionFacts(
            attempt_number=1,
            outcome="retry",
            finished_at=UPDATED,
        ),
    )

    state = project_run_execution_state(facts, POLICY)

    assert type(state) is RunExecutionState
    assert state.phase == "retry_wait"
    assert state.phase_started_at == UPDATED
    assert state.retry_at == retry_at


def test_recovering_accepts_only_an_exact_expired_previous_run_lease() -> None:
    attempt_started_at = NOW - timedelta(seconds=20)
    facts = _facts(
        run_status="running",
        run_execution_started_at=STARTED,
        job_status="leased",
        attempt_count=2,
        active_lease_state="exact",
        active_attempt_lease_state="exact",
        run_lease_state="expired_previous",
        lease_expires_at=NOW + timedelta(minutes=2),
        active_attempt=ActiveAttemptExecutionFacts(
            attempt_number=2,
            started_at=attempt_started_at,
            execution_started_at=None,
        ),
        latest_attempt=SettledAttemptExecutionFacts(
            attempt_number=1,
            outcome="lease_lost",
            finished_at=NOW - timedelta(seconds=30),
        ),
        lease_worker_identity_state="exact",
        lease_worker_heartbeat_at=NOW - timedelta(seconds=5),
    )

    state = project_run_execution_state(facts, POLICY)

    assert type(state) is RunExecutionState
    assert state.phase == "recovering"
    assert state.phase_started_at == attempt_started_at
    assert state.execution_started_at == STARTED
    assert (
        type(
            project_run_execution_state(
                replace(facts, run_lease_state="mismatch"),
                POLICY,
            )
        )
        is RunExecutionStateUnavailable
    )
