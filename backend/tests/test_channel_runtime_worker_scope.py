"""Project-channel admission and independent Worker scope contracts."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.gateway.private_work_schemas import PrivateRunCreateRequest
from app.private_work.context import PrivateWorkContext
from app.private_work.http_runtime import start_private_run
from app.private_work.run_admission import (
    AdmittedPrivateRun,
    PersistedRunSnapshot,
)
from app.private_work.run_repository import PrivateRunRecord
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.reliability.execution import PrivateRunExecution, RunAgentPrivateExecutor
from app.reliability.jobs import AdmittedJobRecord, JobClaim, JobScope


def _context() -> PrivateWorkContext:
    return PrivateWorkContext.from_project(
        ProjectContext(
            user_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            membership_id=uuid.uuid4(),
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=3,
            request_id="channel-worker-scope",
        )
    )


def _run(context: PrivateWorkContext, *, run_id: str, thread_id: str) -> PrivateRunRecord:
    now = datetime.now(UTC)
    return PrivateRunRecord(
        run_id=run_id,
        thread_id=thread_id,
        project_id=context.project_id,
        owner_user_id=str(context.user_id),
        assistant_id=None,
        status="pending",
        multitask_strategy="reject",
        metadata={},
        kwargs={},
        error=None,
        model_name=None,
        created_at=now,
        updated_at=now,
        job_id=uuid.uuid4(),
    )


@pytest.mark.anyio
async def test_channel_admission_uses_resolved_project_scope_not_message_authority() -> None:
    context = _context()
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    persisted = _run(context, run_id=run_id, thread_id=thread_id)
    admitted = AdmittedPrivateRun(
        run=persisted,
        snapshot=PersistedRunSnapshot(assets=(), mcp_grants=(), catalog_generation=1),
        opaque_runtime_scope=context.resource_scope,
        job=AdmittedJobRecord(
            job_id=persisted.job_id,
            job_type="private_run",
            project_id=context.project_id,
            owner_user_id=str(context.user_id),
            run_id=run_id,
            idempotency_key="0" * 64,
            status="queued",
        ),
    )
    captured = None

    class Admission:
        async def admit(self, passed_context, passed_thread_id, request, *, server_context=None):
            nonlocal captured
            assert passed_context is context
            assert passed_thread_id == thread_id
            assert server_context is None
            captured = request
            return admitted

    body = PrivateRunCreateRequest(
        input={"messages": [{"role": "user", "content": "hello"}]},
        context={
            "channel_name": "slack",
            "channel_user_id": "external-user",
            "owner_user_id": "forged-owner",
            "project_id": "forged-project",
            "account_id": "forged-account",
            "connection_id": "forged-connection",
        },
    )
    record = await start_private_run(
        body,
        thread_id,
        SimpleNamespace(),
        context,
        run_id=run_id,
        admission_service=Admission(),
    )

    assert captured is not None
    assert captured.kwargs["config"]["context"] == {"thread_id": thread_id}
    assert record.scope == context.resource_scope
    assert record.user_id == str(context.user_id)
    assert record.store_only is True


def test_independent_worker_rebuilds_admission_from_issued_project_context() -> None:
    context = _context()
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    run = _run(context, run_id=run_id, thread_id=thread_id)
    snapshot = PersistedRunSnapshot(assets=(), mcp_grants=(), catalog_generation=1)
    execution = PrivateRunExecution(
        context=context,
        run=run,
        snapshot=snapshot,
        checkpoint_namespace=run_id,
        graph_input={},
        command=None,
        config={},
        interrupt_before=None,
        interrupt_after=None,
        stream_mode=["values"],
        stream_subgraphs=False,
    )
    claim = JobClaim(
        job_id=run.job_id,
        attempt_id=uuid.uuid4(),
        lease_token="worker-lease",
        job_type="private_run",
        scope=JobScope(
            project_id=context.project_id,
            owner_user_id=str(context.user_id),
        ),
        run_id=run_id,
        occurrence_id=None,
        retry_safety="safe",
        cancel_requested=False,
    )

    admitted = RunAgentPrivateExecutor._admitted(execution, claim)

    assert admitted.opaque_runtime_scope == context.resource_scope
    assert admitted.job.project_id == context.project_id
    assert admitted.job.owner_user_id == str(context.user_id)
    assert admitted.run is run
