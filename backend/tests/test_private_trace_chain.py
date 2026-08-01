from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.private_work.context import strip_private_client_fields
from app.private_work.http_runtime import start_private_run
from app.private_work.run_admission import PrivateRunAdmissionServerContext
from app.private_work.runtime_context import prepare_private_run_config
from app.reliability.execution import (
    AgentExecutionResult,
    PermanentExecutionError,
    RunAgentPrivateExecutor,
)
from deerflow.runtime.private_scope import PrivateResourceScope
from deerflow.trace_context import (
    get_current_trace_id,
    request_trace_context,
)


def test_private_client_trace_fields_are_stripped_from_metadata_config_and_context() -> None:
    assert strip_private_client_fields(
        {
            "trace_id": "forged-generic",
            "deerflow_trace_id": "forged-top",
            "origin_trace_id": "forged-origin",
            "nested": {
                "trace_id": "forged-generic-nested",
                "deerflow_trace_id": "forged-nested",
                "safe": "kept",
            },
        }
    ) == {"nested": {"safe": "kept"}}

    config = prepare_private_run_config(
        thread_id="thread-1",
        opaque_scope=object(),
        request_config={
            "metadata": {
                "trace_id": "forged-config-generic",
                "deerflow_trace_id": "forged-config-metadata",
            },
            "context": {
                "trace_id": "forged-config-context-generic",
                "deerflow_trace_id": "forged-config-context",
                "origin_trace_id": "forged-config-origin",
                "safe": "kept",
            },
        },
        metadata={
            "trace_id": "forged-body-metadata-generic",
            "deerflow_trace_id": "forged-body-metadata",
            "origin_trace_id": "forged-body-origin",
            "safe": "kept",
        },
        body_context={
            "trace_id": "forged-body-context-generic",
            "deerflow_trace_id": "forged-body-context",
            "origin_trace_id": "forged-body-context-origin",
        },
    )

    assert config["metadata"] == {"safe": "kept"}
    assert config["context"]["safe"] == "kept"
    assert "trace_id" not in repr(config)
    assert "deerflow_trace_id" not in repr(config)
    assert "origin_trace_id" not in repr(config)


@pytest.mark.anyio
async def test_http_runtime_uses_ambient_server_trace_and_never_persists_it_in_public_metadata() -> None:
    captured: dict[str, object] = {}
    scope = PrivateResourceScope(
        project_id="00000000-0000-0000-0000-000000000001",
        owner_user_id="00000000-0000-0000-0000-000000000002",
        membership_version=1,
    )
    run = SimpleNamespace(
        run_id="00000000-0000-0000-0000-000000000003",
        thread_id="00000000-0000-0000-0000-000000000004",
        assistant_id=None,
        status="pending",
        multitask_strategy="reject",
        metadata={"safe": "kept"},
        kwargs={},
        owner_user_id=scope.owner_user_id,
        created_at=SimpleNamespace(isoformat=lambda: "2026-07-30T00:00:00+00:00"),
        updated_at=SimpleNamespace(isoformat=lambda: "2026-07-30T00:00:00+00:00"),
        model_name=None,
        origin_trace_id="trace-from-header",
    )

    class Admission:
        async def admit(self, context, thread_id, create_request, *, server_context):
            del context, thread_id
            captured["request"] = create_request
            captured["server_context"] = server_context
            run.metadata = create_request.metadata
            run.kwargs = create_request.kwargs
            return SimpleNamespace(
                run=run,
                thread_id=run.thread_id,
                opaque_runtime_scope=scope,
                inbound_delivery_replay=False,
            )

    body = SimpleNamespace(
        config={
            "context": {"deerflow_trace_id": "forged-config"},
        },
        metadata={
            "deerflow_trace_id": "forged-metadata",
            "origin_trace_id": "forged-origin",
            "safe": "kept",
        },
        context={"deerflow_trace_id": "forged-context"},
        checkpoint=None,
        command=None,
        input={"messages": []},
        stream_mode=["values"],
        stream_subgraphs=False,
        multitask_strategy="reject",
        on_disconnect="cancel",
    )
    context = SimpleNamespace(
        request_id="issued-context-trace",
        resource_scope=scope,
    )

    with request_trace_context("trace-from-header"):
        await start_private_run(
            body,
            run.thread_id,
            SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
            context,
            run_id=run.run_id,
            server_context=PrivateRunAdmissionServerContext(
                non_interactive=True,
                origin_trace_id="stale-server-trace",
            ),
            admission_service=Admission(),
        )

    server_context = captured["server_context"]
    assert isinstance(server_context, PrivateRunAdmissionServerContext)
    assert server_context.origin_trace_id == "trace-from-header"
    request = captured["request"]
    assert request.metadata == {"safe": "kept"}
    assert request.origin_trace_id == "trace-from-header"
    assert "deerflow_trace_id" not in repr(request.kwargs)
    assert "origin_trace_id" not in repr(request.kwargs)


@pytest.mark.anyio
async def test_private_executor_binds_persisted_trace_for_runner_and_restores_context() -> None:
    observed: list[str | None] = []
    executor = object.__new__(RunAgentPrivateExecutor)

    async def execute_with_trace(execution, authority):
        del execution, authority
        observed.append(get_current_trace_id())
        return AgentExecutionResult.succeeded()

    executor._execute_with_trace = execute_with_trace
    execution = SimpleNamespace(
        run=SimpleNamespace(origin_trace_id="persisted-worker-trace"),
    )
    authority = SimpleNamespace(
        claim=SimpleNamespace(origin_trace_id="persisted-worker-trace"),
    )

    assert get_current_trace_id() is None
    result = await executor.execute(execution, authority)
    assert result.status == "succeeded"
    assert observed == ["persisted-worker-trace"]
    assert get_current_trace_id() is None


@pytest.mark.anyio
async def test_private_executor_fails_closed_before_runner_on_trace_mismatch() -> None:
    called = False
    executor = object.__new__(RunAgentPrivateExecutor)

    async def execute_with_trace(execution, authority):
        del execution, authority
        nonlocal called
        called = True
        return AgentExecutionResult.succeeded()

    executor._execute_with_trace = execute_with_trace
    execution = SimpleNamespace(
        run=SimpleNamespace(origin_trace_id="run-trace"),
    )
    authority = SimpleNamespace(
        claim=SimpleNamespace(origin_trace_id="job-trace"),
    )

    with pytest.raises(PermanentExecutionError) as raised:
        await executor.execute(execution, authority)

    assert raised.value.public_error_code == "RUN_TRACE_MISMATCH"
    assert called is False
