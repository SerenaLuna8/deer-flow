import uuid
from importlib.util import find_spec

import pytest

from app.reliability.jobs import EnqueueJob, JobClaim, JobHeartbeat, JobScope
from deerflow.persistence.jobs.sql import (
    EnqueueJob as PersistenceEnqueueJob,
)
from deerflow.persistence.jobs.sql import (
    JobClaim as PersistenceJobClaim,
)
from deerflow.persistence.jobs.sql import (
    JobScope as PersistenceJobScope,
)
from deerflow.persistence.jobs.sql import retry_backoff_seconds


def test_m6_job_repository_and_contract_modules_exist() -> None:
    assert find_spec("deerflow.persistence.jobs.sql") is not None
    assert find_spec("app.reliability.jobs") is not None


def test_app_contracts_reexport_the_repository_authority_types() -> None:
    assert JobScope is PersistenceJobScope
    assert EnqueueJob is PersistenceEnqueueJob
    assert JobClaim is PersistenceJobClaim


def test_job_scope_is_frozen_and_normalizes_owner_uuid() -> None:
    project_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    scope = JobScope(project_id=project_id, owner_user_id=str(owner_id).upper())
    assert scope.project_id == project_id
    assert scope.owner_user_id == str(owner_id)
    with pytest.raises(AttributeError):
        scope.owner_user_id = None  # type: ignore[misc]


def test_enqueue_job_rejects_invalid_authority_shapes() -> None:
    private_scope = JobScope(uuid.uuid4(), str(uuid.uuid4()))
    project_scope = JobScope(uuid.uuid4(), None)
    with pytest.raises(ValueError, match="private_run"):
        EnqueueJob(
            job_type="private_run",
            scope=private_scope,
            idempotency_key="a" * 64,
            run_id=None,
            occurrence_id=None,
            max_attempts=3,
        )
    with pytest.raises(ValueError, match="automation_run"):
        EnqueueJob(
            job_type="automation_run",
            scope=private_scope,
            idempotency_key="b" * 64,
            run_id="run",
            occurrence_id=None,
            max_attempts=3,
        )
    with pytest.raises(ValueError, match="retention_purge"):
        EnqueueJob(
            job_type="retention_purge",
            scope=project_scope,
            idempotency_key="c" * 64,
            run_id="run",
            occurrence_id=None,
            max_attempts=3,
        )
    with pytest.raises(ValueError, match="SHA-256"):
        EnqueueJob(
            job_type="retention_purge",
            scope=project_scope,
            idempotency_key="not-a-digest",
            run_id=None,
            occurrence_id=None,
            max_attempts=3,
        )


def test_retry_backoff_is_bounded_exponential() -> None:
    assert [
        retry_backoff_seconds(
            attempt_count=attempt,
            initial_seconds=2,
            max_seconds=10,
        )
        for attempt in range(1, 6)
    ] == [2, 4, 8, 10, 10]
    with pytest.raises(ValueError):
        retry_backoff_seconds(attempt_count=0, initial_seconds=2, max_seconds=10)


def test_heartbeat_result_is_frozen_and_reports_late_cancel() -> None:
    result = JobHeartbeat(cancel_requested=True)
    assert result.cancel_requested is True
    with pytest.raises(AttributeError):
        result.cancel_requested = False  # type: ignore[misc]


def test_job_claim_repr_redacts_raw_lease_token() -> None:
    secret = "raw-lease-token-must-not-be-logged"
    claim = JobClaim(
        job_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        lease_token=secret,
        job_type="retention_purge",
        scope=JobScope(uuid.uuid4(), None),
        run_id=None,
        occurrence_id=None,
        retry_safety="safe",
        cancel_requested=False,
    )
    assert secret not in repr(claim)
