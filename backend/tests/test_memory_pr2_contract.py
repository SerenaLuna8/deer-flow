from __future__ import annotations

import hashlib
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError
from sqlalchemy import CheckConstraint

import deerflow.persistence.models  # noqa: F401
from app.audit.models import JobAuditMetadata
from app.gateway.routers.admin_jobs import AdminJobResponse
from app.reliability.operations import JobType as OperationsJobType
from app.reliability.workers import WorkerRegistry
from app.system_runtime_settings.models import MemoryPolicy
from app.system_runtime_settings.validation import RUNTIME_POLICY_SCHEMA_VERSION
from app.worker.service import WorkerService
from deerflow.config.app_config import AppConfig
from deerflow.config.memory_config import MemoryConfig
from deerflow.config.worker_config import WorkerConfig
from deerflow.persistence.base import Base
from deerflow.persistence.bootstrap import CURRENT_SCHEMA_REVISION
from deerflow.persistence.jobs.sql import EnqueueJob, JobScope, JobType

MEMORY_V2_TABLES = frozenset(
    {
        "memory_source_batches",
        "memory_source_items",
        "memory_extraction_generations",
        "memory_candidates",
        "memory_consolidation_generations",
        "memory_facts",
        "memory_fact_revisions",
        "memory_fact_evidence",
        "memory_context_summaries",
        "memory_suppressions",
        "run_memory_context_snapshots",
        "run_memory_context_items",
    }
)
MEMORY_JOB_TYPES = frozenset(
    {
        "memory_extract",
        "memory_consolidate",
        "memory_retention_purge",
    }
)
MEMORY_V2_FOREIGN_KEYS = frozenset(
    {
        "fk_memory_source_items_batch",
        "fk_memory_extraction_generations_batch",
        "fk_memory_candidates_extraction",
        "fk_memory_candidates_source_item",
        "fk_memory_candidates_consolidation",
        "fk_memory_fact_revisions_fact",
        "fk_memory_fact_revisions_candidate",
        "fk_memory_fact_revisions_supersedes",
        "fk_memory_facts_current_revision",
        "fk_memory_fact_evidence_revision",
        "fk_memory_fact_evidence_candidate",
        "fk_memory_fact_evidence_source_item",
        "fk_run_memory_context_snapshots_summary",
        "fk_run_memory_context_items_snapshot",
        "fk_run_memory_context_items_revision",
    }
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def test_memory_v2_schema_and_marker_are_registered() -> None:
    assert CURRENT_SCHEMA_REVISION == "full_schema_v2"
    assert MEMORY_V2_TABLES <= set(Base.metadata.tables)

    for table_name in MEMORY_V2_TABLES:
        table = Base.metadata.tables[table_name]
        assert {"project_id", "owner_user_id", "namespace"} <= set(table.columns.keys())
        assert {
            f"fk_{table_name}_project",
            f"fk_{table_name}_owner",
            f"fk_{table_name}_membership",
        } <= {constraint.name for constraint in table.foreign_key_constraints}
        for constraint in table.foreign_key_constraints:
            if constraint.referred_table.name not in MEMORY_V2_TABLES:
                continue
            assert tuple((element.parent.name, element.column.name) for element in constraint.elements[:3]) == (
                ("project_id", "project_id"),
                ("owner_user_id", "owner_user_id"),
                ("namespace", "namespace"),
            )

    memory_foreign_keys = {constraint.name for table_name in MEMORY_V2_TABLES for constraint in Base.metadata.tables[table_name].foreign_key_constraints if constraint.referred_table.name in MEMORY_V2_TABLES}
    assert memory_foreign_keys == MEMORY_V2_FOREIGN_KEYS

    candidate_status = next(constraint for constraint in Base.metadata.tables["memory_candidates"].constraints if isinstance(constraint, CheckConstraint) and constraint.name == "ck_memory_candidates_status")
    assert set(re.findall(r"'([^']+)'", str(candidate_status.sqltext))) == {
        "pending",
        "accepted",
        "rejected",
        "superseded",
    }

    fact_status = next(constraint for constraint in Base.metadata.tables["memory_facts"].constraints if isinstance(constraint, CheckConstraint) and constraint.name == "ck_memory_facts_status")
    assert set(re.findall(r"'([^']+)'", str(fact_status.sqltext))) == {
        "active",
        "disabled",
        "superseded",
        "deleted",
    }

    schema_sql = (Path(__file__).resolve().parents[1] / "packages/harness/deerflow/persistence/full_schema.sql").read_text(encoding="utf-8")
    assert "VALUES ('full_schema_v2')" in schema_sql
    assert "full_schema_v1" not in schema_sql
    for table_name in MEMORY_V2_TABLES:
        assert f"CREATE TABLE {table_name}" in schema_sql


def test_memory_pipeline_mode_defaults_off_and_runtime_overlay_is_bounded() -> None:
    assert MemoryConfig().pipeline_mode == "off"
    assert MemoryPolicy().pipeline_mode == "off"
    assert MemoryConfig().token_counting == "char"
    assert MemoryPolicy().token_counting == "char"
    assert MemoryConfig().consolidation_interval_minutes == 120
    assert MemoryPolicy().candidate_retention_days == 30
    assert RUNTIME_POLICY_SCHEMA_VERSION == 2

    app_config = AppConfig(
        sandbox={"use": "deerflow.sandbox.local:LocalSandboxProvider"},
    )
    shadow = app_config.with_runtime_policy({"memory": {"pipeline_mode": "shadow"}})
    assert shadow.memory.pipeline_mode == "shadow"
    assert app_config.memory.pipeline_mode == "off"

    for invalid in ("enabled", True, 1):
        with pytest.raises((ValidationError, ValueError)):
            MemoryConfig.model_validate({"pipeline_mode": invalid})


def test_memory_job_types_share_one_closed_runtime_contract() -> None:
    expected = {
        "private_run",
        "automation_run",
        "retention_purge",
        "mcp_discovery",
        *MEMORY_JOB_TYPES,
    }
    assert set(get_args(JobType)) == expected
    assert set(get_args(OperationsJobType)) == expected
    assert WorkerRegistry._capabilities(frozenset(MEMORY_JOB_TYPES)) == sorted(MEMORY_JOB_TYPES)

    project_id = uuid.uuid4()
    owner_id = str(uuid.uuid4())
    retention_cutoff = datetime.now(UTC) - timedelta(days=30)
    for job_type in MEMORY_JOB_TYPES:
        request = EnqueueJob(
            job_type=job_type,
            scope=JobScope(project_id, owner_id),
            idempotency_key=_digest(job_type),
            run_id=None,
            occurrence_id=None,
            max_attempts=3,
            namespace="default",
            memory_retention_cutoff_at=(retention_cutoff if job_type == "memory_retention_purge" else None),
        )
        assert request.job_type == job_type
        with pytest.raises(ValueError):
            EnqueueJob(
                job_type=job_type,
                scope=JobScope(project_id, None),
                idempotency_key=_digest(f"{job_type}:ownerless"),
                run_id=None,
                occurrence_id=None,
                max_attempts=3,
                namespace="default",
            )
        for invalid_namespace in (None, "", "x" * 256):
            with pytest.raises(ValueError):
                EnqueueJob(
                    job_type=job_type,
                    scope=JobScope(project_id, owner_id),
                    idempotency_key=_digest(f"{job_type}:{invalid_namespace}"),
                    run_id=None,
                    occurrence_id=None,
                    max_attempts=3,
                    namespace=invalid_namespace,
                )

        for run_id, occurrence_id, origin_trace_id in (
            ("run", None, None),
            (None, "occurrence", None),
            (None, None, "0" * 32),
        ):
            with pytest.raises(ValueError):
                EnqueueJob(
                    job_type=job_type,
                    scope=JobScope(project_id, owner_id),
                    idempotency_key=_digest(f"{job_type}:{run_id}:{occurrence_id}:{origin_trace_id}"),
                    run_id=run_id,
                    occurrence_id=occurrence_id,
                    max_attempts=3,
                    namespace="default",
                    origin_trace_id=origin_trace_id,
                )

        AdminJobResponse.model_validate(
            {
                "job_id": uuid.uuid4(),
                "dead_job_id": None,
                "project_id": project_id,
                "project_slug": "memory-contract",
                "project_display_name": "Memory Contract",
                "job_type": job_type,
                "status": "queued",
                "retry_safety": "safe",
                "safe_to_requeue": False,
                "public_error_code": None,
                "predecessor_dead_job_id": None,
            }
        )
        JobAuditMetadata.model_validate(
            {
                "job_type": job_type,
                "public_error_code": None,
                "attempt_count": 0,
                "retry_safety": "safe",
            }
        )

    with pytest.raises(ValueError):
        EnqueueJob(
            job_type="memory_retention_purge",
            scope=JobScope(project_id, owner_id),
            idempotency_key=_digest("retention:missing-cutoff"),
            run_id=None,
            occurrence_id=None,
            max_attempts=3,
            namespace="default",
        )
    with pytest.raises(ValueError):
        EnqueueJob(
            job_type="memory_consolidate",
            scope=JobScope(project_id, owner_id),
            idempotency_key=_digest("consolidate:unexpected-cutoff"),
            run_id=None,
            occurrence_id=None,
            max_attempts=3,
            namespace="default",
            memory_retention_cutoff_at=retention_cutoff,
        )

    with pytest.raises(ValueError):
        EnqueueJob(
            job_type="mcp_discovery",
            scope=JobScope(project_id, owner_id),
            idempotency_key=_digest("mcp-with-memory-namespace"),
            run_id=None,
            occurrence_id=None,
            max_attempts=3,
            namespace="default",
        )


@pytest.mark.parametrize("job_type", sorted(MEMORY_JOB_TYPES))
def test_memory_handlers_are_registered_in_pr5(job_type: str) -> None:
    WorkerService(
        None,
        None,
        {job_type: object()},
        WorkerConfig(),
    )
