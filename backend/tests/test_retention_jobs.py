from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.private_work.retention_jobs import (
    former_owner_retention_key,
    project_retention_key,
)
from deerflow.persistence.jobs.sql import EnqueueJob, JobScope

NOW = datetime(2026, 7, 22, 8, 0, tzinfo=UTC)


def test_former_owner_retention_authority_is_exact_and_generation_bound() -> None:
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    owner_user_id = str(uuid.uuid4())
    deadline = NOW + timedelta(days=30)

    regular = former_owner_retention_key(
        project_id=project_id,
        owner_user_id=owner_user_id,
        membership_id=membership_id,
        activation_generation=3,
        retention_until=deadline,
        early_delete=False,
    )
    early = former_owner_retention_key(
        project_id=project_id,
        owner_user_id=owner_user_id,
        membership_id=membership_id,
        activation_generation=3,
        retention_until=deadline,
        early_delete=True,
    )
    next_generation = former_owner_retention_key(
        project_id=project_id,
        owner_user_id=owner_user_id,
        membership_id=membership_id,
        activation_generation=4,
        retention_until=deadline,
        early_delete=False,
    )

    assert len(regular) == 64
    assert regular != early
    assert regular != next_generation
    assert regular == former_owner_retention_key(
        project_id=project_id,
        owner_user_id=owner_user_id,
        membership_id=membership_id,
        activation_generation=3,
        retention_until=deadline,
        early_delete=False,
    )


def test_project_retention_authority_is_exact_deadline_bound() -> None:
    project_id = uuid.uuid4()
    assert project_retention_key(project_id, NOW) != project_retention_key(
        project_id,
        NOW + timedelta(seconds=1),
    )


def test_retention_job_accepts_exact_former_owner_scope() -> None:
    request = EnqueueJob(
        job_type="retention_purge",
        scope=JobScope(uuid.uuid4(), str(uuid.uuid4())),
        idempotency_key="a" * 64,
        run_id=None,
        occurrence_id=None,
        max_attempts=5,
        available_at=NOW,
    )

    assert request.scope.owner_user_id is not None
