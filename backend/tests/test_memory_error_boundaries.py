from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langgraph.errors import GraphBubbleUp
from sqlalchemy.exc import DBAPIError

import app.private_work.memory_service as memory_service_module
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkConflict, PrivateWorkUnavailable
from app.private_work.memory_service import PrivateMemoryDocumentService
from app.private_work.run_admission import PersistedRunSnapshot
from app.private_work.run_repository import PrivateRunRecord
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.reliability.execution import (
    PrivateRunExecution,
    RunAgentPrivateExecutor,
    TransientExecutionError,
)
from app.system_runtime_settings.models import AgentRuntimePolicyValue
from app.worker.service import JobLeaseAuthority
from deerflow.agents.memory.snip import SnipArchiveContext
from deerflow.agents.middlewares.tool_error_handling_middleware import (
    ToolErrorHandlingMiddleware,
)
from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig
from deerflow.error_codes import (
    PRIVATE_RUN_EXECUTION_FAILED_ERROR_CODE,
    MemoryAuthorityUnavailable,
)
from deerflow.memory_contract import MemoryDocumentInvalid
from deerflow.persistence.jobs.sql import JobClaim, JobScope
from deerflow.runtime.runs.manager import RunManager
from deerflow.runtime.runs.schemas import RunStatus
from deerflow.runtime.runs.worker import RunContext, run_agent


class _SensitiveFailure(RuntimeError):
    pass


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def begin(self):
        return _Transaction()


class _ExplodingRevalidator:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def require(self, *_args, **_kwargs):
        raise self._error


def _context() -> PrivateWorkContext:
    return PrivateWorkContext.from_project(
        ProjectContext(
            user_id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
            project_id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
            membership_id=uuid.UUID("33333333-3333-4333-8333-333333333333"),
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id="memory-error-boundary-test",
        )
    )


def test_memory_authority_unavailable_is_a_fatal_internal_signal_with_existing_public_code() -> None:
    error = MemoryAuthorityUnavailable()

    assert isinstance(error, GraphBubbleUp)
    assert error.public_error_code == PRIVATE_RUN_EXECUTION_FAILED_ERROR_CODE
    assert str(error) == "Memory authority unavailable"


@pytest.mark.asyncio
async def test_tool_error_middleware_does_not_soften_memory_authority_unavailable() -> None:
    middleware = ToolErrorHandlingMiddleware()
    request = SimpleNamespace(
        runtime=SimpleNamespace(context={}),
        tool=None,
        tool_call={"name": "recall_memory", "id": "tool-call"},
    )
    error = MemoryAuthorityUnavailable()

    async def handler(_request):
        raise error

    with pytest.raises(MemoryAuthorityUnavailable) as raised:
        await middleware.awrap_tool_call(request, handler)

    assert raised.value is error


@pytest.mark.asyncio
async def test_runtime_worker_preserves_memory_authority_unavailable_for_durable_executor() -> None:
    run_manager = RunManager()
    record = await run_manager.create("thread-memory-authority")
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )

    class Agent:
        async def astream(self, *_args, **_kwargs):
            raise MemoryAuthorityUnavailable()
            yield  # pragma: no cover

    with pytest.raises(MemoryAuthorityUnavailable):
        await run_agent(
            bridge,
            run_manager,
            record,
            ctx=RunContext(checkpointer=None),
            agent_factory=lambda **_kwargs: Agent(),
            graph_input={},
            config={},
        )

    assert record.status == RunStatus.running
    assert record.error is None
    assert not [call for call in bridge.publish.await_args_list if call.args[1] == "error"]
    bridge.publish_end.assert_not_awaited()


@pytest.mark.asyncio
async def test_durable_executor_maps_memory_authority_unavailable_to_existing_retry_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_ref = "00000000-0000-4000-8000-000000000308"
    model = ModelConfig(
        name=model_ref,
        display_name="Memory boundary model",
        description="",
        use="deerflow.vision.fake_chat_model:FakeVisionBridgeChatModel",
        model="memory-boundary-model",
    )
    app_config = AppConfig(
        models=[model],
        sandbox={"use": "deerflow.sandbox.local:LocalSandboxProvider"},
    )
    runtime = SimpleNamespace(
        model_ref=model.name,
        skill_root=None,
        aclose=AsyncMock(),
    )

    class Assets:
        async def materialize(self, *_args, **_kwargs):
            return runtime

    class Checkpointer:
        def for_context(self, _context):
            return SimpleNamespace(set_authorization_boundary=lambda _boundary: None)

    async def runner(*_args, **_kwargs):
        raise MemoryAuthorityUnavailable

    executor = RunAgentPrivateExecutor(
        lambda: None,
        app_config=app_config,
        bridge=SimpleNamespace(),
        project_checkpointer=Checkpointer(),
        store=SimpleNamespace(),
        event_store=SimpleNamespace(),
        asset_runtime=Assets(),
        agent_factory=object(),
        runner=runner,
    )
    context = _context()
    now = datetime.now(UTC)
    run = PrivateRunRecord(
        run_id=str(uuid.uuid4()),
        thread_id=str(uuid.uuid4()),
        project_id=context.project_id,
        owner_user_id=str(context.user_id),
        assistant_id=str(uuid.uuid4()),
        status="pending",
        multitask_strategy="reject",
        metadata={},
        kwargs={"input": {"messages": []}},
        origin_trace_id="a" * 32,
        error=None,
        model_name=model.name,
        created_at=now,
        updated_at=now,
    )
    claim = JobClaim(
        job_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        lease_token="lease",
        job_type="private_run",
        scope=JobScope(context.project_id, str(context.user_id)),
        run_id=run.run_id,
        occurrence_id=None,
        retry_safety="safe",
        cancel_requested=False,
        origin_trace_id=run.origin_trace_id,
    )
    authority = JobLeaseAuthority(lambda: None, claim, lease_seconds=30)
    archive_context = SnipArchiveContext(
        enabled=False,
        project_id=context.project_id,
        owner_user_id=str(context.user_id),
        namespace="default",
        preference_version=1,
        summary_model_ref=None,
    )

    async def memory_archive_context(*_args, **_kwargs):
        return archive_context

    monkeypatch.setattr(
        executor,
        "_memory_archive_context",
        memory_archive_context,
    )
    execution = PrivateRunExecution(
        context=context,
        run=run,
        snapshot=PersistedRunSnapshot(
            assets=(),
            mcp_grants=(),
            catalog_generation=1,
        ),
        checkpoint_namespace="",
        graph_input={"messages": []},
        command=None,
        config={},
        interrupt_before=None,
        interrupt_after=None,
        stream_mode=["values"],
        stream_subgraphs=False,
    )

    with pytest.raises(TransientExecutionError) as raised:
        await executor.execute(execution, authority)

    assert raised.value.public_error_code == PRIVATE_RUN_EXECUTION_FAILED_ERROR_CODE
    assert raised.value.attempt_usage is not None
    runtime.aclose.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "invoke"),
    (
        ("get", lambda service, context: service.get(context)),
        (
            "get_with_injection_advisory",
            lambda service, context: service.get_with_injection_advisory(context),
        ),
        (
            "list_versions",
            lambda service, context: service.list_versions(
                context,
                limit=20,
                offset=0,
            ),
        ),
        (
            "list_pending",
            lambda service, context: service.list_pending(
                context,
                limit=20,
                offset=0,
            ),
        ),
        (
            "list_episodes",
            lambda service, context: service.list_episodes(
                context,
                q=None,
                tags=(),
                cursor=None,
                limit=20,
            ),
        ),
        (
            "get_version",
            lambda service, context: service.get_version(context, 1),
        ),
        ("dream", lambda service, context: service.dream(context)),
        (
            "restore",
            lambda service, context: service.restore(
                context,
                target_version=1,
                expected_current_version=1,
            ),
        ),
    ),
)
async def test_memory_service_logs_content_free_failure_observation(
    operation,
    invoke,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "postgresql://private-user:secret@db/private?scope=private-id"
    error = _SensitiveFailure(f"sql=/private/path params={secret}")
    service = PrivateMemoryDocumentService(
        lambda: _Session(),
        revalidator=_ExplodingRevalidator(error),
    )

    with caplog.at_level(logging.ERROR, logger="app.private_work.memory"):
        with pytest.raises(PrivateWorkUnavailable):
            await invoke(service, _context())

    records = [record for record in caplog.records if record.name == "app.private_work.memory"]
    assert len(records) == 1
    assert records[0].exc_info is None
    assert f"operation={operation}" in records[0].getMessage()
    assert "failure_category=internal" in records[0].getMessage()
    assert "failure_type=_SensitiveFailure" in records[0].getMessage()
    assert secret not in caplog.text
    assert "/private/path" not in caplog.text
    assert "private-id" not in caplog.text


@pytest.mark.asyncio
async def test_memory_service_classifies_database_failure_without_logging_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "postgresql://private-user:secret@db/private?scope=private-id"
    error = DBAPIError(
        "SELECT * FROM private_memory WHERE scope=:scope",
        {"scope": secret},
        RuntimeError(f"socket=/private/path credential={secret}"),
        False,
    )
    service = PrivateMemoryDocumentService(
        lambda: _Session(),
        revalidator=_ExplodingRevalidator(error),
    )

    with caplog.at_level(logging.ERROR, logger="app.private_work.memory"):
        with pytest.raises(PrivateWorkUnavailable):
            await service.get(_context())

    assert len(caplog.records) == 1
    assert "operation=get" in caplog.text
    assert "failure_category=database" in caplog.text
    assert "failure_type=DBAPIError" in caplog.text
    assert caplog.records[0].exc_info is None
    assert secret not in caplog.text
    assert "/private/path" not in caplog.text
    assert "SELECT" not in caplog.text


@pytest.mark.asyncio
async def test_memory_injection_integrity_failure_is_observable_but_content_free(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "private-id /private/path credential=secret"

    class Revalidator:
        async def require(self, *_args, **_kwargs):
            return None

    class Repository:
        def __init__(self, _session) -> None:
            pass

        async def read_state(self, _scope):
            return SimpleNamespace(
                document=SimpleNamespace(
                    version=1,
                    content="private content",
                    content_digest="a" * 64,
                    sections=("section-a", "section-b"),
                )
            )

    class Personalization:
        def __init__(self, _session) -> None:
            pass

        async def read_memory(self, *_args, **_kwargs):
            return SimpleNamespace(memory_enabled=True)

    monkeypatch.setattr(
        memory_service_module.SystemRuntimePolicyMaterializer,
        "materialize_current_in_session",
        AsyncMock(return_value=AgentRuntimePolicyValue()),
    )

    def invalid_assessment(**_kwargs):
        raise MemoryDocumentInvalid(secret)

    monkeypatch.setattr(
        memory_service_module,
        "assess_memory_injection",
        invalid_assessment,
    )
    service = PrivateMemoryDocumentService(
        lambda: _Session(),
        repository_builder=Repository,
        revalidator=Revalidator(),
        personalization_repository_builder=Personalization,
    )

    with caplog.at_level(logging.ERROR, logger="app.private_work.memory"):
        with pytest.raises(PrivateWorkConflict):
            await service.get_with_injection_advisory(_context())

    assert len(caplog.records) == 1
    assert "operation=get_with_injection_advisory" in caplog.text
    assert "failure_category=data_integrity" in caplog.text
    assert "failure_type=MemoryDocumentInvalid" in caplog.text
    assert caplog.records[0].exc_info is None
    assert secret not in caplog.text


def test_memory_authority_unavailable_never_retains_or_logs_sensitive_cause(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.private_work.memory_authority import _raise_authority_unavailable

    raw = _SensitiveFailure("sql=/private/path params=secret")

    with caplog.at_level(
        logging.ERROR,
        logger="app.private_work.memory_authority",
    ):
        try:
            raise raw
        except _SensitiveFailure:
            with pytest.raises(MemoryAuthorityUnavailable) as raised:
                _raise_authority_unavailable("load_snapshot", raw)

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__ is True
    assert len(caplog.records) == 1
    assert "operation=load_snapshot" in caplog.text
    assert "disposition=fail_closed" in caplog.text
    assert "failure_type=_SensitiveFailure" in caplog.text
    assert caplog.records[0].exc_info is None
    assert "secret" not in caplog.text
    assert "/private/path" not in caplog.text
