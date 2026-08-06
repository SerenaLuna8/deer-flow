from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace
from typing import get_args

import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import ValidationError

import app.reliability.execution as execution_module
import app.reliability.jobs as jobs_module
import app.worker.app as worker_app_module
from app.audit.models import AuditAction, AuditTargetKind, JobAuditMetadata
from app.audit.sinks import OperationalAuditSink
from app.gateway.routers.admin_jobs import AdminJobResponse
from app.reliability.execution import PrivateRunJobHandler
from app.reliability.operations import JobType as OperationsJobType
from app.reliability.owner_refs import AuditHmacKeyring
from app.reliability.workers import WorkerRegistry
from app.scheduler.app import SchedulerApp
from app.worker.service import WorkerService
from deerflow.agents.lead_agent import agent as lead_agent_module
from deerflow.agents.middlewares.dynamic_context_middleware import (
    DynamicContextMiddleware,
)
from deerflow.agents.middlewares.tool_error_handling_middleware import (
    _is_trusted_read_only_tool,
)
from deerflow.config.app_config import AppConfig
from deerflow.config.worker_config import WorkerConfig
from deerflow.tools.builtins.list_uploaded_files_tool import list_uploaded_files_tool

REMOVED_MEMORY_JOB_TYPES = frozenset(
    {
        "memory_extract",
        "memory_consolidate",
        "memory_retention_purge",
    }
)


def test_private_run_settlement_has_no_legacy_memory_source_admission() -> None:
    assert (
        "memory_source_admission"
        not in inspect.signature(
            PrivateRunJobHandler,
        ).parameters
    )
    assert not hasattr(execution_module, "MemorySourceAdmissionPort")


def test_worker_composes_the_opaque_frozen_memory_authority() -> None:
    source = inspect.getsource(execution_module.RunAgentPrivateExecutor)

    assert "PrivateRunMemoryAuthority(" in source
    assert "memory_authority=memory_authority" in source


@pytest.mark.parametrize(
    "job_type",
    sorted(REMOVED_MEMORY_JOB_TYPES),
)
def test_pr3_worker_rejects_only_removed_memory_handlers(job_type: str) -> None:
    with pytest.raises(ValueError, match="unsupported job type"):
        WorkerService(
            None,
            None,
            {job_type: object()},
            WorkerConfig(),
        )


def test_pr3_worker_accepts_only_the_dream_memory_handler() -> None:
    WorkerService(
        None,
        None,
        {"memory_dream": object()},
        WorkerConfig(),
    )
    assert WorkerRegistry._capabilities(frozenset({"memory_dream"})) == ["memory_dream"]
    for job_type in sorted(REMOVED_MEMORY_JOB_TYPES):
        with pytest.raises(ValueError, match="unsupported job type"):
            WorkerRegistry._capabilities(frozenset({job_type}))


def test_pr1_worker_bootstrap_imports_no_legacy_memory_handler() -> None:
    assert hasattr(worker_app_module, "MemoryDreamJobHandler")
    for symbol in (
        "MemorySourceAdmissionService",
        "MemoryExtractJobHandler",
        "MemoryConsolidateJobHandler",
        "MemoryRetentionPurgeJobHandler",
    ):
        assert not hasattr(worker_app_module, symbol)


def test_pr3_scheduler_has_only_the_dream_memory_lane() -> None:
    assert "memory_service" not in SchedulerApp.__dataclass_fields__
    assert "dream_service" in SchedulerApp.__dataclass_fields__


def test_persisted_contract_exposes_only_future_memory_dream_job() -> None:
    operation_types = set(get_args(OperationsJobType))
    assert "memory_dream" in operation_types
    assert REMOVED_MEMORY_JOB_TYPES.isdisjoint(operation_types)

    payload = {
        "job_id": uuid.UUID("11111111-1111-4111-8111-111111111111"),
        "dead_job_id": None,
        "project_id": uuid.UUID("22222222-2222-4222-8222-222222222222"),
        "project_slug": "memory-contract",
        "project_display_name": "Memory Contract",
        "job_type": "memory_dream",
        "status": "queued",
        "retry_safety": "safe",
        "safe_to_requeue": False,
        "public_error_code": None,
        "predecessor_dead_job_id": None,
    }
    assert AdminJobResponse.model_validate(payload).job_type == "memory_dream"
    assert (
        JobAuditMetadata.model_validate(
            {
                "job_type": "memory_dream",
                "public_error_code": None,
                "attempt_count": 0,
                "retry_safety": "safe",
            }
        ).job_type
        == "memory_dream"
    )
    for removed in REMOVED_MEMORY_JOB_TYPES:
        with pytest.raises(ValidationError):
            AdminJobResponse.model_validate({**payload, "job_type": removed})


def test_legacy_extract_job_identity_helper_is_removed() -> None:
    assert not hasattr(jobs_module, "memory_extract_idempotency_key")


def test_legacy_memory_source_and_candidate_fact_audit_contracts_are_removed() -> None:
    assert not hasattr(AuditHmacKeyring, "memory_source_ref")
    assert not hasattr(AuditHmacKeyring, "memory_source_refs")
    assert not hasattr(AuditAction, "MEMORY_CHANGED")
    assert not hasattr(AuditTargetKind, "MEMORY")
    assert not hasattr(OperationalAuditSink, "memory_changed")


def test_lead_agent_no_longer_adds_project_memory_search() -> None:
    assert not hasattr(lead_agent_module, "_project_memory_tools")


def test_private_dynamic_context_keeps_the_frozen_runtime_config() -> None:
    base = AppConfig(
        sandbox={"use": "deerflow.sandbox.local:LocalSandboxProvider"},
    )

    private = lead_agent_module._dynamic_context_config(
        base,
        private_runtime=True,
    )

    assert private is base
    assert (
        lead_agent_module._dynamic_context_config(
            base,
            private_runtime=False,
        )
        is base
    )


@pytest.mark.asyncio
async def test_dynamic_context_keeps_date_without_an_issued_snapshot_authority() -> None:
    middleware = DynamicContextMiddleware(
        agent_name="lead",
        app_config=lead_agent_module._dynamic_context_config(
            AppConfig(
                sandbox={"use": "deerflow.sandbox.local:LocalSandboxProvider"},
            ),
            private_runtime=True,
        ),
    )

    update = await middleware.abefore_agent(
        {"messages": [HumanMessage(content="continue", id="turn-1")]},
        SimpleNamespace(context={}),
    )

    assert update is not None
    assert any(isinstance(message, SystemMessage) and "<current_date>" in str(message.content) for message in update["messages"])
    assert all("<memory>" not in str(message.content) for message in update["messages"])


def test_memory_search_is_not_a_privileged_read_only_tool() -> None:
    assert _is_trusted_read_only_tool(
        SimpleNamespace(tool=list_uploaded_files_tool),
    )
    assert not _is_trusted_read_only_tool(SimpleNamespace(tool=object()))
    assert "memory" not in inspect.getsource(_is_trusted_read_only_tool)
