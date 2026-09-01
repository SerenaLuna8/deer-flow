"""Pure authoritative projection of private Run execution state."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.private_work.context import (
    PrivateWorkContext,
    require_issued_private_work_context,
)
from app.private_work.errors import PrivateWorkNotFound, PrivateWorkUnavailable
from deerflow.persistence.jobs.model import JobAttemptRow, JobRow, WorkerNodeRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow

RunStatus = Literal[
    "pending",
    "running",
    "success",
    "error",
    "timeout",
    "interrupted",
]
JobStatus = Literal[
    "queued",
    "leased",
    "running",
    "retry_wait",
    "succeeded",
    "failed",
    "cancelled",
    "dead",
]
RunExecutionPhase = Literal[
    "queued",
    "waiting_for_worker",
    "starting",
    "executing",
    "retry_wait",
    "waiting_for_lease_expiry",
    "waiting_for_terminalization",
    "waiting_for_recovery",
    "recovering",
    "cancelling",
    "terminal",
]

_TERMINAL_RUN_STATUSES = frozenset({"success", "error", "timeout", "interrupted"})
_TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "cancelled", "dead"})
_RUN_STATUSES = frozenset({"pending", "running", "success", "error", "timeout", "interrupted"})
_JOB_STATUSES = frozenset(
    {
        "queued",
        "leased",
        "running",
        "retry_wait",
        "succeeded",
        "failed",
        "cancelled",
        "dead",
    }
)
_NONTERMINAL_RUN_STATUSES = frozenset({"pending", "running"})
_NONTERMINAL_JOB_STATUSES = frozenset({"queued", "leased", "running", "retry_wait"})
_RETRY_SAFETY_VALUES = frozenset({"safe", "unknown", "unsafe"})
_IDENTITY_STATES = frozenset({"absent", "exact", "mismatch"})
_RUN_LEASE_STATES = frozenset({"absent", "exact", "expired_previous", "mismatch"})
_SETTLED_ATTEMPT_OUTCOMES = frozenset({"succeeded", "retry", "cancelled", "failed", "lease_lost", "dead"})
_RECOVERY_OUTCOMES = frozenset({"retry", "lease_lost"})
_TERMINAL_PAIRS = frozenset(
    {
        ("success", "succeeded"),
        ("interrupted", "cancelled"),
        ("error", "failed"),
        ("error", "dead"),
        ("timeout", "failed"),
        ("timeout", "dead"),
    }
)


@dataclass(frozen=True, slots=True)
class RunExecutionStatePolicy:
    worker_fresh_for_seconds: int

    def __post_init__(self) -> None:
        if type(self.worker_fresh_for_seconds) is not int or self.worker_fresh_for_seconds <= 0:
            raise ValueError("Worker freshness policy must be positive")


@dataclass(frozen=True, slots=True, repr=False)
class ActiveAttemptExecutionFacts:
    attempt_number: int
    started_at: datetime
    execution_started_at: datetime | None


@dataclass(frozen=True, slots=True, repr=False)
class SettledAttemptExecutionFacts:
    attempt_number: int
    outcome: Literal[
        "succeeded",
        "retry",
        "cancelled",
        "failed",
        "lease_lost",
        "dead",
    ]
    finished_at: datetime


@dataclass(frozen=True, slots=True, repr=False)
class RunExecutionStateFacts:
    observed_at: datetime
    run_status: RunStatus
    run_execution_started_at: datetime | None
    run_job_identity_exact: bool
    job_status: JobStatus
    job_created_at: datetime
    job_updated_at: datetime
    job_available_at: datetime
    job_completed_at: datetime | None
    attempt_count: int
    max_attempts: int
    retry_safety: Literal["safe", "unknown", "unsafe"]
    cancel_requested_at: datetime | None
    lease_expires_at: datetime | None
    active_lease_state: Literal["absent", "exact", "mismatch"]
    active_attempt_lease_state: Literal["absent", "exact", "mismatch"]
    run_lease_state: Literal["absent", "exact", "expired_previous", "mismatch"]
    lease_worker_identity_state: Literal["absent", "exact", "mismatch"]
    active_attempt: ActiveAttemptExecutionFacts | None
    latest_attempt: SettledAttemptExecutionFacts | None
    lease_worker_heartbeat_at: datetime | None
    eligible_worker_exists: bool


@dataclass(frozen=True, slots=True)
class RunExecutionState:
    phase: RunExecutionPhase
    observed_at: datetime
    phase_started_at: datetime | None
    execution_started_at: datetime | None
    retry_at: datetime | None
    run_status: RunStatus


@dataclass(frozen=True, slots=True)
class RunExecutionStateUnavailable:
    observed_at: datetime
    kind: Literal["unavailable"] = "unavailable"


type RunExecutionStateProjection = RunExecutionState | RunExecutionStateUnavailable


def _aware(value: datetime | None) -> bool:
    return value is None or (value.tzinfo is not None and value.utcoffset() is not None)


def _unavailable(facts: RunExecutionStateFacts) -> RunExecutionStateUnavailable:
    return RunExecutionStateUnavailable(observed_at=facts.observed_at)


def _state(
    facts: RunExecutionStateFacts,
    *,
    phase: RunExecutionPhase,
    phase_started_at: datetime | None,
    retry_at: datetime | None = None,
) -> RunExecutionState:
    return RunExecutionState(
        phase=phase,
        observed_at=facts.observed_at,
        phase_started_at=phase_started_at,
        execution_started_at=facts.run_execution_started_at,
        retry_at=retry_at,
        run_status=facts.run_status,
    )


def _common_facts_are_valid(facts: RunExecutionStateFacts) -> bool:
    active_attempt = facts.active_attempt
    latest_attempt = facts.latest_attempt
    timestamps = (
        facts.observed_at,
        facts.run_execution_started_at,
        facts.job_created_at,
        facts.job_updated_at,
        facts.job_available_at,
        facts.job_completed_at,
        facts.cancel_requested_at,
        facts.lease_expires_at,
        facts.lease_worker_heartbeat_at,
        active_attempt.started_at if active_attempt else None,
        active_attempt.execution_started_at if active_attempt else None,
        latest_attempt.finished_at if latest_attempt else None,
    )
    if (
        not all(_aware(value) for value in timestamps)
        or facts.run_status not in _RUN_STATUSES
        or facts.job_status not in _JOB_STATUSES
        or facts.retry_safety not in _RETRY_SAFETY_VALUES
        or facts.active_lease_state not in _IDENTITY_STATES
        or facts.active_attempt_lease_state not in _IDENTITY_STATES
        or facts.run_lease_state not in _RUN_LEASE_STATES
        or facts.lease_worker_identity_state not in _IDENTITY_STATES
        or type(facts.run_job_identity_exact) is not bool
        or not facts.run_job_identity_exact
        or type(facts.eligible_worker_exists) is not bool
        or type(facts.attempt_count) is not int
        or type(facts.max_attempts) is not int
        or facts.attempt_count < 0
        or facts.max_attempts < 1
        or facts.attempt_count > facts.max_attempts
        or facts.job_created_at > facts.observed_at
        or facts.job_updated_at > facts.observed_at
    ):
        return False
    bounded_past_timestamps = (
        facts.run_execution_started_at,
        facts.job_completed_at,
        facts.cancel_requested_at,
        facts.lease_worker_heartbeat_at,
        active_attempt.started_at if active_attempt else None,
        active_attempt.execution_started_at if active_attempt else None,
        latest_attempt.finished_at if latest_attempt else None,
    )
    if any(value is not None and value > facts.observed_at for value in bounded_past_timestamps):
        return False
    if active_attempt is not None:
        if (
            type(active_attempt) is not ActiveAttemptExecutionFacts
            or type(active_attempt.attempt_number) is not int
            or active_attempt.attempt_number < 1
            or (active_attempt.execution_started_at is not None and active_attempt.execution_started_at < active_attempt.started_at)
        ):
            return False
    if latest_attempt is not None and (
        type(latest_attempt) is not SettledAttemptExecutionFacts or type(latest_attempt.attempt_number) is not int or latest_attempt.attempt_number < 1 or latest_attempt.outcome not in _SETTLED_ATTEMPT_OUTCOMES
    ):
        return False
    return True


def _terminal_facts_are_valid(facts: RunExecutionStateFacts) -> bool:
    if (
        (facts.run_status, facts.job_status) not in _TERMINAL_PAIRS
        or facts.active_lease_state != "absent"
        or facts.active_attempt_lease_state != "absent"
        or facts.run_lease_state != "absent"
        or facts.lease_worker_identity_state != "absent"
        or facts.lease_expires_at is not None
        or facts.active_attempt is not None
        or facts.lease_worker_heartbeat_at is not None
        or facts.job_completed_at is None
    ):
        return False
    if facts.attempt_count == 0:
        return facts.latest_attempt is None
    return facts.latest_attempt is not None and facts.latest_attempt.attempt_number == facts.attempt_count


def _active_authority_is_valid(facts: RunExecutionStateFacts) -> bool:
    attempt = facts.active_attempt
    previous = facts.latest_attempt
    if (
        facts.active_lease_state != "exact"
        or facts.active_attempt_lease_state != "exact"
        or facts.job_status not in {"leased", "running"}
        or facts.lease_expires_at is None
        or attempt is None
        or facts.attempt_count < 1
        or attempt.attempt_number != facts.attempt_count
    ):
        return False
    if facts.lease_worker_identity_state == "exact":
        if facts.lease_worker_heartbeat_at is None:
            return False
    elif facts.lease_worker_identity_state == "absent":
        if facts.lease_worker_heartbeat_at is not None:
            return False
    else:
        return False
    if facts.attempt_count == 1:
        if previous is not None:
            return False
    elif previous is None or previous.attempt_number != facts.attempt_count - 1 or previous.outcome not in _RECOVERY_OUTCOMES:
        return False
    if facts.run_status == "pending":
        return facts.run_lease_state == "absent" and attempt.execution_started_at is None
    if facts.run_status != "running" or facts.run_execution_started_at is None:
        return False
    if facts.run_lease_state == "exact":
        return facts.job_status == "running" and attempt.execution_started_at is not None
    return facts.run_lease_state == "expired_previous" and facts.attempt_count > 1 and previous is not None and previous.outcome == "lease_lost" and attempt.execution_started_at is None


def _unowned_authority_is_valid(facts: RunExecutionStateFacts) -> bool:
    if (
        facts.active_lease_state != "absent"
        or facts.active_attempt_lease_state != "absent"
        or facts.run_lease_state != "absent"
        or facts.lease_worker_identity_state != "absent"
        or facts.lease_expires_at is not None
        or facts.active_attempt is not None
        or facts.lease_worker_heartbeat_at is not None
        or facts.run_status != "pending"
        or facts.job_status not in {"queued", "retry_wait"}
    ):
        return False
    if facts.attempt_count == 0:
        return facts.job_status == "queued" and facts.latest_attempt is None
    return facts.job_status == "retry_wait" and facts.latest_attempt is not None and facts.latest_attempt.attempt_number == facts.attempt_count and facts.latest_attempt.outcome in _RECOVERY_OUTCOMES


def project_run_execution_state(
    facts: RunExecutionStateFacts,
    policy: RunExecutionStatePolicy,
) -> RunExecutionStateProjection:
    """Project one closed public state or fail closed as typed unavailable."""

    if type(facts) is not RunExecutionStateFacts or type(policy) is not RunExecutionStatePolicy:
        raise TypeError("Invalid Run execution state projection input")
    if not _common_facts_are_valid(facts):
        return _unavailable(facts)

    run_terminal = facts.run_status in _TERMINAL_RUN_STATUSES
    job_terminal = facts.job_status in _TERMINAL_JOB_STATUSES
    if run_terminal or job_terminal:
        if not _terminal_facts_are_valid(facts):
            return _unavailable(facts)
        return _state(
            facts,
            phase="terminal",
            phase_started_at=facts.job_completed_at,
        )

    if (
        facts.run_status not in _NONTERMINAL_RUN_STATUSES
        or facts.job_status not in _NONTERMINAL_JOB_STATUSES
        or facts.job_completed_at is not None
        or facts.active_lease_state == "mismatch"
        or facts.active_attempt_lease_state == "mismatch"
        or facts.run_lease_state == "mismatch"
        or facts.lease_worker_identity_state == "mismatch"
    ):
        return _unavailable(facts)

    has_active_lease = facts.active_lease_state == "exact"
    if has_active_lease:
        if not _active_authority_is_valid(facts):
            return _unavailable(facts)
    elif not _unowned_authority_is_valid(facts):
        return _unavailable(facts)

    # Terminal projection wins above. Cancellation is otherwise the highest
    # priority phase, but never conceals malformed execution authority.
    if facts.cancel_requested_at is not None:
        return _state(
            facts,
            phase="cancelling",
            phase_started_at=facts.cancel_requested_at,
        )

    if not has_active_lease:
        if facts.job_available_at > facts.observed_at:
            if facts.job_status != "retry_wait" or facts.attempt_count == 0:
                return _unavailable(facts)
            return _state(
                facts,
                phase="retry_wait",
                phase_started_at=facts.job_updated_at,
                retry_at=facts.job_available_at,
            )
        if facts.attempt_count == 0:
            if facts.retry_safety != "safe":
                return _unavailable(facts)
            return _state(
                facts,
                phase="queued" if facts.eligible_worker_exists else "waiting_for_worker",
                phase_started_at=facts.job_created_at,
            )
        if facts.retry_safety != "safe" or facts.attempt_count >= facts.max_attempts:
            return _state(
                facts,
                phase="waiting_for_terminalization",
                phase_started_at=facts.latest_attempt.finished_at,
            )
        return _state(
            facts,
            phase=("waiting_for_recovery" if facts.eligible_worker_exists else "waiting_for_worker"),
            phase_started_at=facts.job_available_at,
        )

    lease_expires_at = facts.lease_expires_at
    active_attempt = facts.active_attempt
    if lease_expires_at is None or active_attempt is None:
        return _unavailable(facts)
    if lease_expires_at <= facts.observed_at:
        if facts.retry_safety != "safe" or facts.attempt_count >= facts.max_attempts:
            return _state(
                facts,
                phase="waiting_for_terminalization",
                phase_started_at=lease_expires_at,
            )
        return _state(
            facts,
            phase=("waiting_for_recovery" if facts.eligible_worker_exists else "waiting_for_worker"),
            phase_started_at=lease_expires_at,
        )

    worker_heartbeat = facts.lease_worker_heartbeat_at
    worker_stale_at = None if worker_heartbeat is None else worker_heartbeat + timedelta(seconds=policy.worker_fresh_for_seconds)
    if worker_stale_at is None or worker_stale_at < facts.observed_at:
        return _state(
            facts,
            phase="waiting_for_lease_expiry",
            phase_started_at=worker_stale_at,
            retry_at=lease_expires_at,
        )
    if facts.run_lease_state == "exact":
        return _state(
            facts,
            phase="executing",
            phase_started_at=active_attempt.execution_started_at,
        )
    return _state(
        facts,
        phase="starting" if facts.attempt_count == 1 else "recovering",
        phase_started_at=active_attempt.started_at,
    )


def _row_value(row: object, name: str) -> object:
    if isinstance(row, Mapping):
        return row.get(name)
    return getattr(row, name, None)


def _attempt_facts(
    row: object,
) -> tuple[
    ActiveAttemptExecutionFacts | None,
    SettledAttemptExecutionFacts | None,
    bool,
]:
    active_values = {
        name: _row_value(row, f"active_attempt_{name}")
        for name in (
            "id",
            "number",
            "worker_id",
            "lease_token_hash",
            "started_at",
            "execution_started_at",
            "finished_at",
            "outcome",
        )
    }
    if all(value is None for value in active_values.values()):
        active = None
    elif (
        isinstance(active_values["id"], uuid.UUID)
        and type(active_values["number"]) is int
        and active_values["number"] >= 1
        and isinstance(active_values["worker_id"], uuid.UUID)
        and type(active_values["lease_token_hash"]) is str
        and isinstance(active_values["started_at"], datetime)
        and (active_values["execution_started_at"] is None or isinstance(active_values["execution_started_at"], datetime))
        and active_values["finished_at"] is None
        and active_values["outcome"] is None
    ):
        active = ActiveAttemptExecutionFacts(
            attempt_number=active_values["number"],
            started_at=active_values["started_at"],
            execution_started_at=active_values["execution_started_at"],
        )
    else:
        return None, None, False

    latest_values = {name: _row_value(row, f"latest_attempt_{name}") for name in ("id", "number", "outcome", "finished_at")}
    if all(value is None for value in latest_values.values()):
        latest = None
    elif isinstance(latest_values["id"], uuid.UUID) and type(latest_values["number"]) is int and latest_values["number"] >= 1 and latest_values["outcome"] in _SETTLED_ATTEMPT_OUTCOMES and isinstance(latest_values["finished_at"], datetime):
        latest = SettledAttemptExecutionFacts(
            attempt_number=latest_values["number"],
            outcome=latest_values["outcome"],
            finished_at=latest_values["finished_at"],
        )
    else:
        return None, None, False
    return active, latest, True


def _facts_from_row(
    row: object,
) -> RunExecutionStateFacts | RunExecutionStateUnavailable:
    observed_at = _row_value(row, "observed_at")
    if not isinstance(observed_at, datetime) or not _aware(observed_at):
        raise PrivateWorkUnavailable("unknown")

    required_datetimes = {
        name: _row_value(row, name)
        for name in (
            "job_created_at",
            "job_updated_at",
            "job_available_at",
        )
    }
    job_id = _row_value(row, "job_id")
    run_job_id = _row_value(row, "run_job_id")
    if not isinstance(job_id, uuid.UUID) or run_job_id != job_id or not all(isinstance(value, datetime) for value in required_datetimes.values()):
        return RunExecutionStateUnavailable(observed_at=observed_at)

    active_attempt, latest_attempt, attempts_valid = _attempt_facts(row)
    if not attempts_valid:
        return RunExecutionStateUnavailable(observed_at=observed_at)

    job_lease_owner_id = _row_value(row, "job_lease_owner_id")
    job_lease_token_hash = _row_value(row, "job_lease_token_hash")
    job_lease_expires_at = _row_value(row, "job_lease_expires_at")
    job_lease_values = (
        job_lease_owner_id,
        job_lease_token_hash,
        job_lease_expires_at,
    )
    if all(value is None for value in job_lease_values):
        active_lease_state: Literal["absent", "exact", "mismatch"] = "absent"
    elif isinstance(job_lease_owner_id, uuid.UUID) and type(job_lease_token_hash) is str and isinstance(job_lease_expires_at, datetime):
        active_lease_state = "exact"
    else:
        active_lease_state = "mismatch"

    active_attempt_worker_id = _row_value(row, "active_attempt_worker_id")
    active_attempt_token_hash = _row_value(
        row,
        "active_attempt_lease_token_hash",
    )
    if active_attempt is None:
        active_attempt_lease_state: Literal[
            "absent",
            "exact",
            "mismatch",
        ] = "absent"
    elif active_lease_state == "exact" and active_attempt_worker_id == job_lease_owner_id and active_attempt_token_hash == job_lease_token_hash:
        active_attempt_lease_state = "exact"
    else:
        active_attempt_lease_state = "mismatch"

    run_lease_token_hash = _row_value(
        row,
        "run_execution_lease_token_hash",
    )
    run_lease_expires_at = _row_value(
        row,
        "run_execution_lease_expires_at",
    )
    if run_lease_token_hash is None and run_lease_expires_at is None:
        run_lease_state: Literal[
            "absent",
            "exact",
            "expired_previous",
            "mismatch",
        ] = "absent"
    elif not (type(run_lease_token_hash) is str and isinstance(run_lease_expires_at, datetime)):
        run_lease_state = "mismatch"
    elif active_lease_state == "exact" and run_lease_token_hash == job_lease_token_hash and run_lease_expires_at == job_lease_expires_at:
        run_lease_state = "exact"
    elif active_lease_state == "exact" and run_lease_expires_at <= observed_at:
        run_lease_state = "expired_previous"
    else:
        run_lease_state = "mismatch"

    lease_worker_id = _row_value(row, "lease_worker_id")
    lease_worker_heartbeat_at = _row_value(
        row,
        "lease_worker_heartbeat_at",
    )
    if job_lease_owner_id is None:
        lease_worker_identity_state: Literal[
            "absent",
            "exact",
            "mismatch",
        ] = "absent" if lease_worker_id is None and lease_worker_heartbeat_at is None else "mismatch"
    elif lease_worker_id is None and lease_worker_heartbeat_at is None:
        lease_worker_identity_state = "absent"
    elif lease_worker_id == job_lease_owner_id and isinstance(lease_worker_heartbeat_at, datetime):
        lease_worker_identity_state = "exact"
    else:
        lease_worker_identity_state = "mismatch"

    optional_datetimes = {
        name: _row_value(row, name)
        for name in (
            "run_execution_started_at",
            "job_completed_at",
            "cancel_requested_at",
        )
    }
    if any(value is not None and not isinstance(value, datetime) for value in optional_datetimes.values()):
        return RunExecutionStateUnavailable(observed_at=observed_at)
    eligible_worker_exists = _row_value(row, "eligible_worker_exists")
    if type(eligible_worker_exists) is not bool:
        return RunExecutionStateUnavailable(observed_at=observed_at)

    return RunExecutionStateFacts(
        observed_at=observed_at,
        run_status=_row_value(row, "run_status"),
        run_execution_started_at=optional_datetimes["run_execution_started_at"],
        run_job_identity_exact=True,
        job_status=_row_value(row, "job_status"),
        job_created_at=required_datetimes["job_created_at"],
        job_updated_at=required_datetimes["job_updated_at"],
        job_available_at=required_datetimes["job_available_at"],
        job_completed_at=optional_datetimes["job_completed_at"],
        attempt_count=_row_value(row, "job_attempt_count"),
        max_attempts=_row_value(row, "job_max_attempts"),
        retry_safety=_row_value(row, "job_retry_safety"),
        cancel_requested_at=optional_datetimes["cancel_requested_at"],
        lease_expires_at=job_lease_expires_at,
        active_lease_state=active_lease_state,
        active_attempt_lease_state=active_attempt_lease_state,
        run_lease_state=run_lease_state,
        lease_worker_identity_state=lease_worker_identity_state,
        active_attempt=active_attempt,
        latest_attempt=latest_attempt,
        lease_worker_heartbeat_at=(lease_worker_heartbeat_at if lease_worker_identity_state == "exact" else None),
        eligible_worker_exists=eligible_worker_exists,
    )


async def read_run_execution_state(
    session: AsyncSession,
    context: PrivateWorkContext,
    thread_id: str,
    run_id: str,
    policy: RunExecutionStatePolicy,
) -> RunExecutionStateProjection:
    """Read one scoped Run graph with the PostgreSQL clock and project it."""

    context = require_issued_private_work_context(context)
    if type(policy) is not RunExecutionStatePolicy:
        raise TypeError("Invalid Run execution state policy")
    try:
        selected_thread_id = str(uuid.UUID(thread_id))
        selected_run_id = str(uuid.UUID(run_id))
    except (AttributeError, TypeError, ValueError):
        raise PrivateWorkNotFound(context.request_id) from None

    clock = sa.select(
        sa.func.clock_timestamp().label("observed_at"),
    ).cte("execution_clock")
    active_attempt = (
        sa.select(
            JobAttemptRow.id.label("id"),
            JobAttemptRow.attempt_number.label("number"),
            JobAttemptRow.worker_id.label("worker_id"),
            JobAttemptRow.lease_token_hash.label("lease_token_hash"),
            JobAttemptRow.started_at.label("started_at"),
            JobAttemptRow.execution_started_at.label("execution_started_at"),
            JobAttemptRow.finished_at.label("finished_at"),
            JobAttemptRow.outcome.label("outcome"),
        )
        .where(
            JobAttemptRow.job_id == JobRow.id,
            JobAttemptRow.attempt_number == JobRow.attempt_count,
            JobAttemptRow.outcome.is_(None),
        )
        .order_by(JobAttemptRow.id)
        .limit(1)
        .correlate(JobRow)
        .lateral("active_attempt")
    )
    latest_attempt = (
        sa.select(
            JobAttemptRow.id.label("id"),
            JobAttemptRow.attempt_number.label("number"),
            JobAttemptRow.outcome.label("outcome"),
            JobAttemptRow.finished_at.label("finished_at"),
        )
        .where(
            JobAttemptRow.job_id == JobRow.id,
            JobAttemptRow.outcome.is_not(None),
        )
        .order_by(
            JobAttemptRow.attempt_number.desc(),
            JobAttemptRow.id.desc(),
        )
        .limit(1)
        .correlate(JobRow)
        .lateral("latest_attempt")
    )
    lease_worker = aliased(WorkerNodeRow, name="lease_worker")
    eligible_worker = aliased(WorkerNodeRow, name="eligible_worker")
    fresh_after = clock.c.observed_at - timedelta(
        seconds=policy.worker_fresh_for_seconds,
    )
    eligible_worker_exists = sa.exists(
        sa.select(sa.literal(1))
        .select_from(eligible_worker)
        .where(
            sa.cast(
                eligible_worker.capabilities_json,
                JSONB,
            ).op("@>")(
                sa.func.jsonb_build_array(JobRow.job_type),
            ),
            eligible_worker.draining.is_(False),
            eligible_worker.heartbeat_at >= fresh_after,
            sa.or_(
                JobRow.execution_domain_affinity.is_(None),
                eligible_worker.execution_domain_affinity == JobRow.execution_domain_affinity,
            ),
        )
        .correlate(JobRow, clock)
    )
    statement = (
        sa.select(
            clock.c.observed_at,
            RunRow.job_id.label("run_job_id"),
            RunRow.status.label("run_status"),
            RunRow.execution_started_at.label("run_execution_started_at"),
            RunRow.execution_lease_token_hash.label(
                "run_execution_lease_token_hash",
            ),
            RunRow.execution_lease_expires_at.label(
                "run_execution_lease_expires_at",
            ),
            JobRow.id.label("job_id"),
            JobRow.status.label("job_status"),
            JobRow.created_at.label("job_created_at"),
            JobRow.updated_at.label("job_updated_at"),
            JobRow.available_at.label("job_available_at"),
            JobRow.completed_at.label("job_completed_at"),
            JobRow.attempt_count.label("job_attempt_count"),
            JobRow.max_attempts.label("job_max_attempts"),
            JobRow.retry_safety.label("job_retry_safety"),
            sa.func.least(
                JobRow.cancel_requested_at,
                RunRow.cancel_requested_at,
                RunRow.authorization_cancel_requested_at,
            ).label("cancel_requested_at"),
            JobRow.lease_owner_id.label("job_lease_owner_id"),
            JobRow.lease_token_hash.label("job_lease_token_hash"),
            JobRow.lease_expires_at.label("job_lease_expires_at"),
            active_attempt.c.id.label("active_attempt_id"),
            active_attempt.c.number.label("active_attempt_number"),
            active_attempt.c.worker_id.label("active_attempt_worker_id"),
            active_attempt.c.lease_token_hash.label(
                "active_attempt_lease_token_hash",
            ),
            active_attempt.c.started_at.label("active_attempt_started_at"),
            active_attempt.c.execution_started_at.label(
                "active_attempt_execution_started_at",
            ),
            active_attempt.c.finished_at.label("active_attempt_finished_at"),
            active_attempt.c.outcome.label("active_attempt_outcome"),
            latest_attempt.c.id.label("latest_attempt_id"),
            latest_attempt.c.number.label("latest_attempt_number"),
            latest_attempt.c.outcome.label("latest_attempt_outcome"),
            latest_attempt.c.finished_at.label("latest_attempt_finished_at"),
            lease_worker.id.label("lease_worker_id"),
            lease_worker.heartbeat_at.label("lease_worker_heartbeat_at"),
            eligible_worker_exists.label("eligible_worker_exists"),
        )
        .select_from(clock)
        .join(
            ThreadMetaRow,
            sa.and_(
                ThreadMetaRow.thread_id == selected_thread_id,
                ThreadMetaRow.project_id == context.project_id,
                ThreadMetaRow.owner_user_id == str(context.user_id),
                ThreadMetaRow.thread_kind == "chat",
                ThreadMetaRow.deleted_at.is_(None),
                ThreadMetaRow.frozen_at.is_(None),
            ),
        )
        .join(
            RunRow,
            sa.and_(
                RunRow.run_id == selected_run_id,
                RunRow.thread_id == ThreadMetaRow.thread_id,
                RunRow.project_id == ThreadMetaRow.project_id,
                RunRow.owner_user_id == ThreadMetaRow.owner_user_id,
            ),
        )
        .outerjoin(
            JobRow,
            sa.and_(
                JobRow.id == RunRow.job_id,
                JobRow.job_type.in_(("private_run", "automation_run")),
                JobRow.project_id == RunRow.project_id,
                JobRow.owner_user_id == RunRow.owner_user_id,
                JobRow.run_id == RunRow.run_id,
                JobRow.origin_trace_id == RunRow.origin_trace_id,
            ),
        )
        .outerjoin(active_attempt, sa.true())
        .outerjoin(latest_attempt, sa.true())
        .outerjoin(
            lease_worker,
            lease_worker.id == JobRow.lease_owner_id,
        )
    )
    try:
        row = (await session.execute(statement)).mappings().one_or_none()
    except (DBAPIError, SATimeoutError):
        raise PrivateWorkUnavailable(context.request_id) from None
    if row is None:
        raise PrivateWorkNotFound(context.request_id)
    try:
        facts = _facts_from_row(row)
    except PrivateWorkUnavailable:
        raise PrivateWorkUnavailable(context.request_id) from None
    if type(facts) is RunExecutionStateUnavailable:
        return facts
    return project_run_execution_state(facts, policy)


__all__ = [
    "ActiveAttemptExecutionFacts",
    "RunExecutionState",
    "RunExecutionStateFacts",
    "RunExecutionStatePolicy",
    "RunExecutionStateProjection",
    "RunExecutionStateUnavailable",
    "SettledAttemptExecutionFacts",
    "project_run_execution_state",
    "read_run_execution_state",
]
