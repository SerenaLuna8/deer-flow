"""Static release contracts for the first production Workflow schema revision.

G10 is intentionally wider than a table smoke test: fresh SQL, ORM metadata,
the migration chain, readiness inventory, Job DTOs, and the four public Job
type literals are one atomic contract.  Worker execution is deliberately not
part of this revision; ``workflow_run`` remains unadvertised until G32 ships
the real handler and execution boundary.
"""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path
from typing import get_args

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

import deerflow.persistence.models  # noqa: F401 -- register every ORM table
from app.audit.models import JobAuditMetadata
from app.final_schema import FINAL_REQUIRED_RELATIONS
from app.gateway.routers.admin_jobs import AdminJobResponse
from app.reliability.operations import JobType as OperationsJobType
from app.reliability.workers import WorkerRegistry
from app.worker.service import WorkerService
from deerflow.config.worker_config import WorkerConfig
from deerflow.persistence.base import Base
from deerflow.persistence.bootstrap import CURRENT_SCHEMA_REVISION
from deerflow.persistence.final_schema_contract import FINAL_APP_SEQUENCES
from deerflow.persistence.jobs.sql import (
    EnqueueJob,
    JobClaim,
    JobScope,
    JobType,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FULL_SCHEMA_PATH = BACKEND_ROOT / "packages/harness/deerflow/persistence/full_schema.sql"

G10_WORKFLOW_TABLES = {
    "workflow_definitions",
    "workflow_drafts",
    "workflow_versions",
    "workflow_version_model_refs",
    "workflow_draft_credential_grant_intents",
    "workflow_version_credential_slots",
    "workflow_credential_grants",
    "workflow_runs",
    "workflow_run_jobs",
    "workflow_run_snapshots",
    "workflow_run_runtime_policy_snapshots",
    "workflow_run_model_snapshots",
    "workflow_run_code_snapshots",
    "workflow_run_http_snapshots",
    "workflow_code_sandbox_leases",
    "workflow_node_effects",
    "workflow_run_event_partition_state",
    "workflow_run_event_invariants",
    "workflow_run_events",
}

LATER_PHASE_TABLES = {
    "workflow_waits",
    "workflow_version_agent_refs",
    "workflow_version_skill_refs",
    "workflow_version_mcp_refs",
    "workflow_run_agent_snapshots",
    "workflow_run_skill_snapshots",
    "workflow_run_mcp_snapshots",
    "workflow_run_files",
    "workflow_run_artifacts",
}

EXPECTED_JOB_TYPES = {
    "private_run",
    "automation_run",
    "workflow_run",
    "retention_purge",
    "mcp_discovery",
    "memory_dream",
    "memory_seal",
}


def _workflow_enqueue_kwargs() -> dict[str, object]:
    return {
        "job_type": "workflow_run",
        "scope": JobScope(uuid.uuid4(), str(uuid.uuid4())),
        "idempotency_key": "a" * 64,
        "run_id": None,
        "occurrence_id": None,
        "max_attempts": 3,
        "namespace": None,
        "origin_trace_id": "workflow-g10-origin-trace",
        "workflow_run_id": uuid.uuid4(),
        "workflow_epoch": 1,
        "required_worker_profile_digest": "b" * 64,
    }


def _workflow_claim_kwargs(
    enqueue: dict[str, object] | None = None,
) -> dict[str, object]:
    enqueue = enqueue or _workflow_enqueue_kwargs()
    return {
        "job_id": uuid.uuid4(),
        "attempt_id": uuid.uuid4(),
        "lease_token": "lease-token",
        "job_type": "workflow_run",
        "scope": enqueue["scope"],
        "run_id": None,
        "occurrence_id": None,
        "retry_safety": "safe",
        "cancel_requested": False,
        "namespace": None,
        "origin_trace_id": enqueue["origin_trace_id"],
        "workflow_run_id": enqueue["workflow_run_id"],
        "workflow_epoch": 1,
        "required_worker_profile_digest": enqueue["required_worker_profile_digest"],
    }


def test_g10_schema_is_registered_in_every_catalog_surface() -> None:
    assert CURRENT_SCHEMA_REVISION == "full_schema_v12"
    assert G10_WORKFLOW_TABLES <= set(Base.metadata.tables)
    assert G10_WORKFLOW_TABLES <= set(FINAL_REQUIRED_RELATIONS)
    assert LATER_PHASE_TABLES.isdisjoint(Base.metadata.tables)
    assert LATER_PHASE_TABLES.isdisjoint(FINAL_REQUIRED_RELATIONS)
    assert (
        "workflow_run_events_id_seq",
        "workflow_run_events",
    ) in FINAL_APP_SEQUENCES

    schema_sql = FULL_SCHEMA_PATH.read_text(encoding="utf-8")
    migration_source = (BACKEND_ROOT / "migrations/versions/full_schema_v10_workflows.py").read_text(encoding="utf-8")
    for table in G10_WORKFLOW_TABLES:
        assert f"CREATE TABLE {table} (" in schema_sql
    for table in LATER_PHASE_TABLES:
        assert f"CREATE TABLE {table} (" not in schema_sql
    assert "workflow_automation_run" not in schema_sql
    assert schema_sql.count("INSERT INTO alembic_version (version_num) VALUES ('full_schema_v12');") == 1

    workflow_run = Base.metadata.tables["workflow_runs"]
    assert "thread_id" not in workflow_run.c
    assert "agent_id" not in workflow_run.c
    assert "agent_version_id" not in workflow_run.c
    jobs = Base.metadata.tables["jobs"]
    assert {
        "workflow_run_id",
        "workflow_epoch",
        "required_worker_profile_digest",
    } <= set(jobs.c.keys())
    worker_nodes = Base.metadata.tables["worker_nodes"]
    assert {
        "runtime_profile_digests_json",
        "workflow_runtime_policy_section",
        "workflow_runtime_policy_version_id",
        "workflow_runtime_policy_revision",
        "workflow_runtime_policy_schema_version",
        "workflow_runtime_policy_checksum",
    } <= set(worker_nodes.c.keys())
    assert {
        "ck_worker_nodes_workflow_runtime_identity",
        "fk_worker_nodes_workflow_runtime_identity",
    } <= {constraint.name for constraint in worker_nodes.constraints}
    identity_check = next(constraint for constraint in worker_nodes.constraints if constraint.name == "ck_worker_nodes_workflow_runtime_identity")
    identity_fk = next(constraint for constraint in worker_nodes.constraints if constraint.name == "fk_worker_nodes_workflow_runtime_identity")
    assert "workflow_runtime_policy_section IS NOT NULL" in str(identity_check.sqltext)
    assert identity_fk.match == "FULL"
    assert "MATCH FULL" in schema_sql
    assert "MATCH FULL" in migration_source
    assert "ix_worker_nodes_workflow_runtime_identity_fresh" in {index.name for index in worker_nodes.indexes}
    event = Base.metadata.tables["workflow_run_events"]
    assert {
        "node_id",
        "activation_id",
        "iteration_path",
        "attempt",
    } <= set(event.c.keys())


def test_g10_workflow_migration_remains_explicit_and_does_not_import_live_orm() -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    revision = ScriptDirectory.from_config(config).get_revision("full_schema_v10")
    assert revision is not None
    assert revision.down_revision == "full_schema_v9"
    payload = Path(revision.path).read_text(encoding="utf-8")
    assert "deerflow.persistence" not in payload
    assert "from app." not in payload
    assert "workflow_definitions" in payload
    assert "workflow_run_events" in payload
    assert "workflow_runtime" in payload


def test_workflow_job_type_is_closed_and_consistent_across_four_contracts() -> None:
    contracts = (
        JobType,
        OperationsJobType,
        AdminJobResponse.model_fields["job_type"].annotation,
        JobAuditMetadata.model_fields["job_type"].annotation,
    )
    for contract in contracts:
        assert set(get_args(contract)) == EXPECTED_JOB_TYPES
    assert "workflow_automation_run" not in EXPECTED_JOB_TYPES


def test_workflow_job_dtos_carry_the_independent_execution_reference() -> None:
    expected_fields = {
        "workflow_run_id",
        "workflow_epoch",
        "required_worker_profile_digest",
    }
    assert expected_fields <= {field.name for field in dataclasses.fields(EnqueueJob)}
    assert expected_fields <= {field.name for field in dataclasses.fields(JobClaim)}

    enqueue_kwargs = _workflow_enqueue_kwargs()
    request = EnqueueJob(**enqueue_kwargs)  # type: ignore[arg-type]
    claim = JobClaim(**_workflow_claim_kwargs(enqueue_kwargs))  # type: ignore[arg-type]
    assert request.run_id is None
    assert request.workflow_run_id == claim.workflow_run_id
    assert request.workflow_epoch == claim.workflow_epoch == 1
    assert request.required_worker_profile_digest == claim.required_worker_profile_digest


@pytest.mark.parametrize(
    "updates",
    [
        {"scope": JobScope(uuid.uuid4(), None)},
        {"run_id": "hidden-chat-run"},
        {"occurrence_id": "future-workflow-automation"},
        {"workflow_run_id": None},
        {"workflow_epoch": None},
        {"workflow_epoch": 0},
        {"required_worker_profile_digest": "not-a-digest"},
        {"origin_trace_id": None},
        {"namespace": "chat-thread-or-memory-namespace"},
    ],
)
def test_workflow_enqueue_rejects_mixed_or_incomplete_authority(
    updates: dict[str, object],
) -> None:
    kwargs = {**_workflow_enqueue_kwargs(), **updates}
    with pytest.raises(ValueError):
        EnqueueJob(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "updates",
    [
        {"scope": JobScope(uuid.uuid4(), None)},
        {"run_id": "hidden-chat-run"},
        {"occurrence_id": "future-workflow-automation"},
        {"workflow_run_id": None},
        {"workflow_epoch": None},
        {"workflow_epoch": 0},
        {"required_worker_profile_digest": "not-a-digest"},
        {"origin_trace_id": None},
        {"namespace": "chat-thread-or-memory-namespace"},
    ],
)
def test_workflow_claim_rejects_mixed_or_incomplete_authority(
    updates: dict[str, object],
) -> None:
    kwargs = {**_workflow_claim_kwargs(), **updates}
    with pytest.raises(ValueError):
        JobClaim(**kwargs)  # type: ignore[arg-type]


def test_g10_does_not_advertise_or_register_a_workflow_worker() -> None:
    with pytest.raises(ValueError, match="unsupported job type"):
        WorkerRegistry._capabilities(frozenset({"workflow_run"}))
    with pytest.raises(ValueError, match="unsupported job type"):
        WorkerService(
            None,
            None,
            {"workflow_run": object()},
            WorkerConfig(),
        )

    app_source = (BACKEND_ROOT / "app/worker/app.py").read_text(encoding="utf-8")
    assert '"workflow_run":' not in app_source
