from __future__ import annotations

import ast
import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.private_work.account_private_lifecycle import AccountPrivateGeneration
from app.private_work.retention_purge import RetentionCandidate
from app.reliability.jobs import (
    AutomationRunJobRepository,
    PrivateRunJobRepository,
)
from deerflow.persistence.jobs.model import JobAttemptRow, JobRow, WorkerNodeRow
from deerflow.persistence.jobs.sql import (
    EnqueueJob,
    JobScope,
    RetentionPurgeJobAuthority,
)

_FULL_SCHEMA = (Path(__file__).resolve().parents[1] / "packages" / "harness" / "deerflow" / "persistence" / "full_schema.sql").read_text()


def _checks(table) -> dict[str, str]:
    return {constraint.name: str(constraint.sqltext) for constraint in table.constraints if constraint.name is not None and hasattr(constraint, "sqltext")}


def _enqueue_job_calls() -> dict[str, list[ast.Call]]:
    backend = Path(__file__).resolve().parents[1]
    calls: dict[str, list[ast.Call]] = {}
    for source_root in (backend / "app", backend / "packages" / "harness"):
        for path in source_root.rglob("*.py"):
            relative = str(path.relative_to(backend))
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "EnqueueJob":
                    calls.setdefault(relative, []).append(node)
    return calls


def test_owner_private_job_generation_is_a_required_typed_fact() -> None:
    owner_user_id = str(uuid.uuid4())
    generation = AccountPrivateGeneration(
        owner_user_id=owner_user_id,
        generation=7,
    )
    request = EnqueueJob(
        job_type="memory_seal",
        scope=JobScope(uuid.uuid4(), owner_user_id),
        owner_private_generation=generation,
        namespace="thread-1",
        idempotency_key=hashlib.sha256(b"memory-seal").hexdigest(),
        run_id=None,
        occurrence_id=None,
        max_attempts=3,
    )

    assert request.owner_private_generation is generation
    with pytest.raises(TypeError, match="AccountPrivateGeneration"):
        EnqueueJob(
            job_type="memory_seal",
            scope=JobScope(uuid.uuid4(), owner_user_id),
            owner_private_generation=7,  # type: ignore[arg-type]
            namespace="thread-2",
            idempotency_key=hashlib.sha256(b"raw-generation").hexdigest(),
            run_id=None,
            occurrence_id=None,
            max_attempts=3,
        )
    with pytest.raises(ValueError, match="owner"):
        EnqueueJob(
            job_type="memory_seal",
            scope=JobScope(uuid.uuid4(), owner_user_id),
            owner_private_generation=AccountPrivateGeneration(
                owner_user_id=str(uuid.uuid4()),
                generation=7,
            ),
            namespace="thread-3",
            idempotency_key=hashlib.sha256(b"wrong-owner").hexdigest(),
            run_id=None,
            occurrence_id=None,
            max_attempts=3,
        )


def test_job_worker_and_attempt_facts_match_the_schema_v1_snapshot() -> None:
    jobs = JobRow.__table__
    generation = jobs.c.owner_private_generation
    assert generation.nullable is True
    assert generation.type.python_type is int
    assert _checks(jobs)["ck_jobs_owner_private_generation"] == ("owner_private_generation IS NOT NULL AND owner_private_generation >= 1 AND (job_type = 'retention_purge' OR owner_user_id IS NOT NULL)")
    assert jobs.c.retention_resource_kind.nullable is True
    assert jobs.c.retention_effective_at.nullable is True
    assert jobs.c.retention_membership_id.nullable is True
    assert "retention_resource_kind IS NOT NULL" in _checks(jobs)["ck_jobs_retention_authority"]

    workers = WorkerNodeRow.__table__
    assert workers.c.execution_domain_affinity.nullable is True
    assert _checks(workers)["ck_worker_nodes_execution_domain_affinity"] == ("execution_domain_affinity IS NULL OR execution_domain_affinity ~ '^[0-9a-f]{64}$'")
    worker_indexes = {index.name: index for index in workers.indexes}
    assert tuple(expression.name for expression in worker_indexes["ix_worker_nodes_fresh_affinity"].expressions) == ("execution_domain_affinity", "heartbeat_at")
    assert str(worker_indexes["ix_worker_nodes_fresh_affinity"].dialect_options["postgresql"]["where"]) == "draining = false"

    attempts = JobAttemptRow.__table__
    assert attempts.c.execution_started_at.nullable is True
    assert "owner_private_generation BIGINT" in _FULL_SCHEMA
    assert "CONSTRAINT ck_jobs_owner_private_generation CHECK" in _FULL_SCHEMA
    assert "retention_resource_kind VARCHAR(16)" in _FULL_SCHEMA
    assert "retention_effective_at TIMESTAMP WITH TIME ZONE" in _FULL_SCHEMA
    assert "retention_membership_id UUID" in _FULL_SCHEMA
    assert "CONSTRAINT ck_jobs_retention_authority CHECK" in _FULL_SCHEMA
    assert "execution_domain_affinity CHAR(64)" in _FULL_SCHEMA
    assert "CONSTRAINT ck_worker_nodes_execution_domain_affinity CHECK" in _FULL_SCHEMA
    assert "CREATE INDEX ix_worker_nodes_fresh_affinity" in _FULL_SCHEMA
    assert "execution_started_at TIMESTAMP WITH TIME ZONE" in _FULL_SCHEMA


def test_retention_purge_requires_its_explicit_non_active_authority() -> None:
    project_id = uuid.uuid4()
    former_owner_user_id = str(uuid.uuid4())
    authority = RetentionPurgeJobAuthority(
        resource_kind="former_owner",
        project_id=project_id,
        owner_user_id=former_owner_user_id,
        generation=4,
        effective_at=datetime(2026, 8, 25, tzinfo=UTC),
        membership_id=uuid.uuid4(),
    )
    request = EnqueueJob(
        job_type="retention_purge",
        scope=JobScope(project_id, former_owner_user_id),
        owner_private_generation=authority,
        idempotency_key=hashlib.sha256(b"retention").hexdigest(),
        run_id=None,
        occurrence_id=None,
        max_attempts=5,
    )

    assert request.owner_private_generation is authority
    with pytest.raises(TypeError, match="RetentionPurgeJobAuthority"):
        EnqueueJob(
            job_type="retention_purge",
            scope=JobScope(project_id, former_owner_user_id),
            owner_private_generation=None,  # type: ignore[arg-type]
            idempotency_key=hashlib.sha256(b"retention-missing").hexdigest(),
            run_id=None,
            occurrence_id=None,
            max_attempts=5,
        )
    with pytest.raises(ValueError, match="scope"):
        EnqueueJob(
            job_type="retention_purge",
            scope=JobScope(project_id, former_owner_user_id),
            owner_private_generation=RetentionPurgeJobAuthority(
                resource_kind="former_owner",
                project_id=uuid.uuid4(),
                owner_user_id=former_owner_user_id,
                generation=4,
                effective_at=authority.effective_at,
                membership_id=authority.membership_id,
            ),
            idempotency_key=hashlib.sha256(b"retention-wrong-scope").hexdigest(),
            run_id=None,
            occurrence_id=None,
            max_attempts=5,
        )


def test_retention_job_authority_has_restart_safe_typed_coordinates() -> None:
    effective_at = datetime(2026, 8, 25, 8, 30, tzinfo=UTC)
    project = RetentionPurgeJobAuthority(
        resource_kind="project",
        project_id=uuid.uuid4(),
        owner_user_id=None,
        generation=3,
        effective_at=effective_at,
        membership_id=None,
    )
    account = RetentionPurgeJobAuthority(
        resource_kind="account",
        project_id=uuid.uuid4(),
        owner_user_id=str(uuid.uuid4()),
        generation=7,
        effective_at=effective_at,
        membership_id=None,
    )

    assert project.generation == 3
    assert project.effective_at == effective_at
    assert account.resource_kind == "account"
    with pytest.raises(ValueError, match="former_owner"):
        RetentionPurgeJobAuthority(
            resource_kind="former_owner",
            project_id=uuid.uuid4(),
            owner_user_id=str(uuid.uuid4()),
            generation=1,
            effective_at=effective_at,
            membership_id=None,
        )


def test_project_retention_candidate_keeps_generation_out_of_owner_coordinate() -> None:
    project_id = uuid.uuid4()
    effective_at = datetime(2026, 8, 25, 9, 15, tzinfo=UTC)

    candidate = RetentionCandidate.project(
        project_id=project_id,
        project_generation=9,
        deletion_effective_at=effective_at,
        idempotency_key=hashlib.sha256(b"project-retention").hexdigest(),
        request_id="project-retention-test",
    )

    assert candidate.project_id == project_id
    assert candidate.owner_user_id is None
    assert candidate.activation_generation == 9
    assert candidate.account_private_generation is None
    assert candidate.eligibility_at == effective_at


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "repository,extra",
    [
        (PrivateRunJobRepository, {}),
        (AutomationRunJobRepository, {"occurrence_id": "occurrence-1"}),
    ],
)
async def test_run_job_wrappers_reject_untyped_generation_before_storage(
    repository,
    extra,
) -> None:
    owner_user_id = str(uuid.uuid4())
    with pytest.raises(TypeError, match="AccountPrivateGeneration"):
        await repository(object()).enqueue(
            scope=JobScope(uuid.uuid4(), owner_user_id),
            run_id="run-1",
            origin_trace_id="a" * 32,
            account_private_generation=5,
            **extra,
        )


def test_owner_job_enqueue_source_inventory_has_no_silent_generation_default() -> None:
    calls = _enqueue_job_calls()
    assert {path: len(nodes) for path, nodes in calls.items()} == {
        "app/private_work/memory_seal_service.py": 1,
        "app/private_work/retention_jobs.py": 4,
        "app/reliability/jobs.py": 2,
        "app/shared_assets/mcp_discovery_repository.py": 1,
        "packages/harness/deerflow/persistence/jobs/sql.py": 1,
        "packages/harness/deerflow/persistence/private_work/memory_dream_prepare_repository.py": 1,
        "packages/harness/deerflow/persistence/private_work/memory_dream_store.py": 1,
    }
    missing = {
        path: len([node for node in nodes if "owner_private_generation" not in {keyword.arg for keyword in node.keywords}])
        for path, nodes in calls.items()
        if any("owner_private_generation" not in {keyword.arg for keyword in node.keywords} for node in nodes)
    }
    assert missing == {}


def test_run_job_admission_callers_forward_the_guard_token() -> None:
    backend = Path(__file__).resolve().parents[1]
    for relative in (
        "app/private_work/run_admission.py",
        "app/private_work/skill_builder_run_admission.py",
        "app/automations/dispatcher.py",
    ):
        tree = ast.parse((backend / relative).read_text())
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and any(keyword.arg == "origin_trace_id" for keyword in node.keywords) and isinstance(node.func, ast.Attribute) and node.func.attr == "enqueue"]
        assert calls, relative
        assert all("account_private_generation" in {keyword.arg for keyword in node.keywords} for node in calls), relative
