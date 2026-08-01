from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from support.m4_private_threads import seed_m4_thread_database

from app.private_work.context import PrivateWorkContext
from app.private_work.inbound_dedupe import DuplicateInboundDelivery
from app.private_work.run_admission import (
    AdmittedPrivateRun,
    PersistedRunSnapshot,
    PrivateRunAdmissionServerContext,
    PrivateRunAdmissionService,
)
from app.private_work.run_repository import PrivateRunRecord
from app.private_work.thread_repository import (
    PrivateThreadRepository,
    ThreadAgentRef,
)
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.reliability.execution import (
    AgentExecutionResult,
    PrivateRunJobHandler,
)
from app.reliability.jobs import AdmittedJobRecord, private_run_idempotency_key
from app.reliability.workers import WorkerRegistry
from app.worker.service import JobLeaseAuthority
from deerflow.persistence.jobs.sql import JobRepository
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
async def test_gateway_private_run_is_admission_only_and_strips_client_authority() -> None:
    from app.private_work import http_runtime

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
        origin_trace_id=context.request_id,
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
            origin_trace_id=context.request_id,
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

    record = await http_runtime.start_private_run(
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
    assert captured.kwargs["command"] == {
        "resume": {
            "answer": "ok",
            "project_id": "forged-command-project",
        }
    }
    assert set(captured.kwargs) == {
        "input",
        "config",
        "command",
        "stream_mode",
        "stream_subgraphs",
    }
    assert captured.kwargs["stream_mode"] == ["values"]
    assert captured.kwargs["stream_subgraphs"] is False
    assert isinstance(
        captured_server_context,
        PrivateRunAdmissionServerContext,
    )
    assert captured_server_context.origin_trace_id == context.request_id
    assert record.run_id == run_id
    assert record.status is RunStatus.pending
    assert record.task is None
    assert record.store_only is True
    assert record.scope == context.resource_scope

    await http_runtime.start_private_run(
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
        PrivateRunAdmissionServerContext,
    )
    assert captured_server_context.non_interactive is True
    assert captured_server_context.origin_trace_id == context.request_id

    class ReplayAdmission:
        async def admit(
            self,
            passed_context,
            thread_id,
            request,
            *,
            server_context=None,
        ):
            del passed_context, thread_id, request, server_context
            return replace(admitted, inbound_delivery_replay=True)

    with pytest.raises(DuplicateInboundDelivery):
        await http_runtime.start_private_run(
            body,
            "private-thread",
            SimpleNamespace(),
            context,
            run_id=run_id,
            admission_service=ReplayAdmission(),
        )


@pytest.mark.postgres
@pytest.mark.anyio
async def test_gateway_db_worker_preserves_command_state_payload(
    migrated_postgres_database_url: str,
) -> None:
    from app.private_work.http_runtime import start_private_run

    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"m6-command-fidelity-{uuid.uuid4()}"
    command = {
        "resume": {
            "role": "tool",
            "project_id": "state-project-value",
            "user_id": "state-user-value",
            "answer": "approved",
        }
    }
    captured = []

    class Executor:
        async def execute(self, execution, _authority):
            captured.append(execution)
            return AgentExecutionResult.succeeded()

    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
        body = SimpleNamespace(
            input={
                "messages": [
                    {
                        "role": "user",
                        "content": "continue",
                        "user_id": "message-state-user",
                    }
                ]
            },
            command=command,
            metadata={},
            config={"configurable": {"thread_id": thread_id}},
            context={},
            checkpoint_id=None,
            checkpoint=None,
            on_disconnect="cancel",
            multitask_strategy="reject",
            stream_mode=["values"],
            stream_subgraphs=False,
            interrupt_before=[],
            interrupt_after=[],
        )
        record = await start_private_run(
            body,
            thread_id,
            SimpleNamespace(),
            seed.owner_a,
            admission_service=PrivateRunAdmissionService(seed.factory),
        )

        worker_id = uuid.uuid4()
        await WorkerRegistry(
            seed.factory,
            version="test-m6-command-fidelity",
        ).register(worker_id, frozenset({"private_run"}), 1)
        async with seed.factory() as session, session.begin():
            jobs = JobRepository(session)
            claim = await jobs.claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=90,
            )
            assert claim is not None
            assert claim.run_id == record.run_id
            assert await jobs.mark_running(
                claim.job_id,
                lease_token=claim.lease_token,
            )
        settlement = await PrivateRunJobHandler(
            seed.factory,
            executor=Executor(),
        )(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
        )
        await settlement.commit()

        assert len(captured) == 1
        assert captured[0].command == command
        assert captured[0].graph_input["messages"][0]["role"] == "user"
        assert captured[0].graph_input["messages"][0]["user_id"] == ("message-state-user")
        async with seed.factory() as session:
            persisted_command = await session.scalar(
                text("SELECT kwargs_json->'command' FROM runs WHERE run_id=:run_id"),
                {"run_id": record.run_id},
            )
        assert persisted_command == command
    finally:
        await seed.engine.dispose()
