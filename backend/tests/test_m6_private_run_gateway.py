from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.private_work.context import PrivateWorkContext
from app.private_work.run_admission import AdmittedPrivateRun, PersistedRunSnapshot
from app.private_work.run_repository import PrivateRunRecord
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.reliability.jobs import AdmittedJobRecord, private_run_idempotency_key
from deerflow.runtime import RunStatus


def _context() -> PrivateWorkContext:
    return PrivateWorkContext.from_project(
        ProjectContext(
            user_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            membership_id=uuid.uuid4(),
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id="req-m6-gateway-admission",
        )
    )


@pytest.mark.anyio
async def test_gateway_private_run_is_admission_only_and_strips_client_authority(
    monkeypatch,
) -> None:
    from app.gateway import services

    context = _context()
    now = datetime.now(UTC)
    run_id = str(uuid.uuid4())
    job_id = uuid.uuid4()
    persisted = PrivateRunRecord(
        run_id=run_id,
        thread_id="private-thread",
        project_id=context.project_id,
        owner_user_id=str(context.user_id),
        assistant_id=str(uuid.uuid4()),
        status="pending",
        multitask_strategy="reject",
        metadata={"safe": "value"},
        kwargs={"input": {"messages": []}},
        error=None,
        model_name="exact-model",
        created_at=now,
        updated_at=now,
        job_id=job_id,
    )
    admitted = AdmittedPrivateRun(
        run=persisted,
        snapshot=PersistedRunSnapshot(assets=(), mcp_grants=(), catalog_generation=1),
        opaque_runtime_scope=context.resource_scope,
        job=AdmittedJobRecord(
            job_id=job_id,
            job_type="private_run",
            project_id=context.project_id,
            owner_user_id=str(context.user_id),
            run_id=run_id,
            idempotency_key=private_run_idempotency_key(run_id),
            status="queued",
        ),
    )
    captured = None
    captured_server_context = None

    class Admission:
        async def admit(self, passed_context, thread_id, request, *, server_context=None):
            nonlocal captured, captured_server_context
            assert passed_context is context
            assert thread_id == "private-thread"
            captured = request
            captured_server_context = server_context
            return admitted

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Gateway must not launch or register private execution")

    monkeypatch.setattr(services.asyncio, "create_task", forbidden)
    monkeypatch.setattr(services, "get_run_manager", forbidden)
    monkeypatch.setattr(services, "_launch_registered_run", forbidden)
    body = SimpleNamespace(
        assistant_id="forged-assistant",
        input={"messages": [{"role": "user", "content": "hello"}]},
        command={"resume": {"answer": "ok", "project_id": "forged-command-project"}},
        metadata={"safe": "value", "project_id": "forged-project"},
        config={"context": {"project_id": "forged-project", "non_interactive": True}},
        context={"non_interactive": True, "owner_user_id": "forged-owner"},
        checkpoint_id=None,
        checkpoint=None,
        on_disconnect="cancel",
        multitask_strategy="reject",
        stream_mode=["values"],
        stream_subgraphs=False,
        interrupt_before="*",
        interrupt_after=["after-agent"],
    )

    record = await services.start_private_run(
        body,
        "private-thread",
        SimpleNamespace(),
        context,
        run_id=run_id,
        admission_service=Admission(),
    )

    assert captured is not None
    assert captured.assistant_id is None
    assert "project_id" not in captured.metadata
    assert "project_id" not in captured.kwargs["config"]["context"]
    assert "owner_user_id" not in captured.kwargs["config"]["context"]
    assert "non_interactive" not in captured.kwargs["config"]["context"]
    assert captured.kwargs["command"] == {"resume": {"answer": "ok"}}
    assert captured.kwargs["interrupt_before"] == "*"
    assert captured.kwargs["interrupt_after"] == ["after-agent"]
    assert captured.kwargs["stream_mode"] == ["values"]
    assert captured.kwargs["stream_subgraphs"] is False
    assert captured_server_context is None
    assert record.run_id == run_id
    assert record.status is RunStatus.pending
    assert record.task is None
    assert record.store_only is True
    assert record.scope == context.resource_scope

    await services.start_private_run(
        body,
        "private-thread",
        SimpleNamespace(),
        context,
        run_id=run_id,
        server_context={"non_interactive": True, "project_id": "forged-server-project"},
        admission_service=Admission(),
    )
    assert "non_interactive" not in captured.kwargs["config"]["context"]
    assert "project_id" not in captured.kwargs["config"]["context"]
    assert isinstance(
        captured_server_context,
        services.PrivateRunAdmissionServerContext,
    )
    assert captured_server_context.non_interactive is True
