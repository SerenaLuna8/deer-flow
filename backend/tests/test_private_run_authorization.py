from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import text
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.private_work.authorization import (
    AUTHORIZATION_REVOKED_REASON,
    PrivateRunAuthorizationBoundary,
    PrivateRunAuthorizationService,
)
from app.private_work.retention import PrivateWorkRetentionService
from deerflow.agents.middlewares.summarization_middleware import (
    DeerFlowSummarizationMiddleware,
)
from deerflow.agents.middlewares.title_middleware import TitleMiddleware
from deerflow.agents.middlewares.tool_error_handling_middleware import ToolErrorHandlingMiddleware
from deerflow.config.title_config import TitleConfig
from deerflow.runtime.goal import evaluate_goal_completion
from deerflow.runtime.private_scope import PrivateResourceScope
from deerflow.runtime.runs.manager import RunManager
from deerflow.runtime.runs.schemas import RunStatus
from deerflow.runtime.runs.worker import RunContext, run_agent
from deerflow.sandbox.sandbox import AuthorizationRevoked

NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def private_seed(
    migrated_postgres_database_url: str,
) -> M4ThreadSeed:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        yield seed
    finally:
        await seed.engine.dispose()


class _ScalarResult:
    def __init__(self, values=(), scalar=None):
        self._values = values
        self._scalar = scalar

    def scalars(self):
        return self

    def all(self):
        return list(self._values)

    def scalar_one_or_none(self):
        return self._scalar


@pytest.mark.asyncio
async def test_mark_revoked_locks_only_live_unmarked_runs_and_returns_ids() -> None:
    session = AsyncMock()
    session.execute.side_effect = [
        _ScalarResult(("run-a", "run-b")),
        _ScalarResult(),
    ]
    project_id = uuid.uuid4()
    owner_user_id = str(uuid.uuid4())

    run_ids = await PrivateRunAuthorizationService.mark_revoked(
        session,
        project_id=project_id,
        owner_user_id=owner_user_id,
        reason=AUTHORIZATION_REVOKED_REASON,
        now=NOW,
    )

    assert run_ids == ("run-a", "run-b")
    statements = [str(call.args[0]) for call in session.execute.await_args_list]
    assert all("runs.project_id" in statement for statement in statements)
    assert all("runs.owner_user_id" in statement for statement in statements)
    assert all("runs.status IN" in statement for statement in statements)
    assert all("runs.authorization_cancel_requested_at IS NULL" in statement for statement in statements)
    assert "FOR UPDATE" in statements[0]


@pytest.mark.asyncio
async def test_remote_boundary_fails_closed_and_sets_abort_before_side_effect() -> None:
    session = AsyncMock()
    session.execute.return_value = _ScalarResult(scalar=None)

    @asynccontextmanager
    async def factory():
        yield session

    abort_event = asyncio.Event()
    boundary = PrivateRunAuthorizationBoundary(
        factory,
        project_id=uuid.uuid4(),
        owner_user_id=str(uuid.uuid4()),
        run_id="run-1",
        abort_event=abort_event,
    )

    with pytest.raises(AuthorizationRevoked) as exc_info:
        await boundary.before_tool_call()

    assert str(exc_info.value) == AUTHORIZATION_REVOKED_REASON
    assert abort_event.is_set()


@pytest.mark.asyncio
async def test_database_failure_at_boundary_is_fail_closed_without_detail() -> None:
    @asynccontextmanager
    async def factory():
        raise RuntimeError("postgres password=secret")
        yield

    boundary = PrivateRunAuthorizationBoundary(
        factory,
        project_id=uuid.uuid4(),
        owner_user_id=str(uuid.uuid4()),
        run_id="run-1",
    )

    with pytest.raises(AuthorizationRevoked) as exc_info:
        await boundary.before_model_call()

    assert str(exc_info.value) == AUTHORIZATION_REVOKED_REASON
    assert "secret" not in str(exc_info.value)


class _RecordingBoundary:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def before_model_call(self) -> None:
        self.calls.append("model")

    async def before_tool_call(self) -> None:
        self.calls.append("tool")

    async def before_mcp_call(self) -> None:
        self.calls.append("mcp")


@pytest.mark.asyncio
async def test_middleware_checks_every_model_tool_and_private_mcp_dispatch() -> None:
    boundary = _RecordingBoundary()
    runtime = SimpleNamespace(context={"__authorization_boundary": boundary})
    model_request = SimpleNamespace(runtime=runtime)
    tool_request = SimpleNamespace(
        runtime=runtime,
        tool=SimpleNamespace(metadata={"deerflow_private_mcp": True}),
        tool_call={"name": "private_mcp", "id": "call-1"},
    )
    middleware = ToolErrorHandlingMiddleware()

    await middleware.awrap_model_call(model_request, AsyncMock(return_value="model-result"))
    await middleware.awrap_tool_call(
        tool_request,
        AsyncMock(
            return_value=ToolMessage(
                content="tool-result",
                tool_call_id="call-1",
                name="private_mcp",
            )
        ),
    )

    assert boundary.calls == ["model", "tool", "mcp"]


@pytest.mark.asyncio
async def test_authorization_revoked_bubbles_past_tool_error_wrapper() -> None:
    boundary = _RecordingBoundary()

    async def revoke() -> None:
        raise AuthorizationRevoked

    boundary.before_tool_call = revoke
    request = SimpleNamespace(
        runtime=SimpleNamespace(context={"__authorization_boundary": boundary}),
        tool=SimpleNamespace(metadata={}),
        tool_call={"name": "bash", "id": "call-1"},
    )
    handler = AsyncMock()

    with pytest.raises(AuthorizationRevoked):
        await ToolErrorHandlingMiddleware().awrap_tool_call(request, handler)
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_retention_freeze_and_restore_are_scope_bound_and_non_destructive() -> None:
    session = AsyncMock()
    session.execute.side_effect = [
        _ScalarResult(("thread-1",)),
        _ScalarResult(("connection-1",)),
        _ScalarResult(("thread-1",)),
        _ScalarResult(("connection-1",)),
    ]
    project_id = uuid.uuid4()
    owner_user_id = str(uuid.uuid4())

    frozen = await PrivateWorkRetentionService.freeze_owner(
        session,
        project_id=project_id,
        owner_user_id=owner_user_id,
        now=NOW,
    )
    restored = await PrivateWorkRetentionService.restore_owner(
        session,
        project_id=project_id,
        owner_user_id=owner_user_id,
        now=NOW,
    )

    assert frozen.thread_ids == ("thread-1",)
    assert frozen.connection_ids == ("connection-1",)
    assert restored.thread_ids == ("thread-1",)
    assert restored.connection_ids == ("connection-1",)
    statements = [str(call.args[0]) for call in session.execute.await_args_list]
    assert all("project_id" in statement and "owner_user_id" in statement for statement in statements)
    assert all(not statement.lstrip().upper().startswith("DELETE ") for statement in statements)
    assert "status = 'connected'" in statements[0] or "status" in statements[1]


@pytest.mark.asyncio
async def test_trusted_local_cancel_is_scope_independent_and_unknown_is_harmless() -> None:
    manager = RunManager()
    scope = PrivateResourceScope(
        project_id=str(uuid.uuid4()),
        owner_user_id=str(uuid.uuid4()),
        membership_version=1,
    )
    record = await manager.register_persisted(
        run_id="run-private",
        thread_id="thread-private",
        assistant_id=None,
        scope=scope,
    )

    assert await manager.cancel_authorization_revoked("unknown") is False
    assert await manager.cancel_authorization_revoked(record.run_id) is True
    current = await manager.get(record.run_id, scope=scope)
    assert current is not None
    assert current.status is RunStatus.interrupted
    assert current.error == AUTHORIZATION_REVOKED_REASON


@pytest.mark.asyncio
async def test_goal_direct_model_call_checks_boundary_first() -> None:
    boundary = AsyncMock()
    boundary.before_model_call.side_effect = AuthorizationRevoked()
    model = AsyncMock()

    with pytest.raises(AuthorizationRevoked):
        await evaluate_goal_completion(
            {
                "objective": "finish",
                "status": "active",
                "continuation_count": 0,
                "max_continuations": 1,
            },
            [HumanMessage(content="do it"), AIMessage(content="done")],
            model=model,
            authorization_boundary=boundary,
        )
    model.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_title_direct_model_call_does_not_swallow_revocation(monkeypatch) -> None:
    boundary = AsyncMock()
    boundary.before_model_call.side_effect = AuthorizationRevoked()
    model = AsyncMock()
    monkeypatch.setattr(
        "deerflow.agents.middlewares.title_middleware.create_chat_model",
        lambda **_kwargs: model,
    )
    middleware = TitleMiddleware(
        title_config=TitleConfig(model_name="test-model"),
    )
    state = {
        "messages": [
            HumanMessage(content="Please inspect the project"),
            AIMessage(content="I inspected it"),
        ]
    }

    with pytest.raises(AuthorizationRevoked):
        await middleware.aafter_model(
            state,
            SimpleNamespace(
                context={"__authorization_boundary": boundary},
            ),
        )
    model.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_summarization_direct_model_call_does_not_swallow_revocation() -> None:
    boundary = AsyncMock()
    boundary.before_model_call.side_effect = AuthorizationRevoked()
    model = MagicMock()
    model.with_config.return_value = model
    model.ainvoke = AsyncMock()
    middleware = DeerFlowSummarizationMiddleware(
        model=model,
        trigger=("messages", 2),
        keep=("messages", 1),
        token_counter=len,
    )

    with pytest.raises(AuthorizationRevoked):
        await middleware._asummarize_with(
            [HumanMessage(content="old context")],
            authorization_context={"__authorization_boundary": boundary},
        )
    model.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_maps_revocation_to_interrupted_and_skips_title(
    monkeypatch,
) -> None:
    from deerflow.runtime.runs import worker

    manager = RunManager()
    record = await manager.create("thread-auth-revoked")
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )
    checkpointer = AsyncMock()
    checkpointer.aget_tuple.return_value = None
    ensure_title = AsyncMock()
    monkeypatch.setattr(worker, "_ensure_interrupted_title", ensure_title)

    class RevokedAgent:
        async def astream(self, *_args, **_kwargs):
            raise AuthorizationRevoked
            yield

    await run_agent(
        bridge,
        manager,
        record,
        ctx=RunContext(checkpointer=checkpointer),
        agent_factory=lambda **_kwargs: RevokedAgent(),
        graph_input={},
        config={},
    )

    current = await manager.get(record.run_id)
    assert current is not None
    assert current.status is RunStatus.interrupted
    assert current.error == AUTHORIZATION_REVOKED_REASON
    ensure_title.assert_not_awaited()
    error_events = [call.args for call in bridge.publish.await_args_list if len(call.args) >= 2 and call.args[1] == "error"]
    assert error_events[-1][2] == {
        "message": AUTHORIZATION_REVOKED_REASON,
        "name": AUTHORIZATION_REVOKED_REASON,
    }


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_postgres_marker_boundary_and_retention_are_scope_bound(
    private_seed: M4ThreadSeed,
) -> None:
    from app.private_work.thread_repository import (
        PrivateThreadRepository,
        ThreadAgentRef,
    )

    seed = private_seed
    owner = str(seed.owner_a.user_id)
    project_id = seed.owner_a.project_id
    other_project_id = seed.project_b_owner_a.project_id
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=seed.owner_a_scope,
            thread_id="task6-thread-a",
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
        await PrivateThreadRepository(session).create(
            scope=seed.project_b_owner_a_scope,
            thread_id="task6-thread-b",
            agent=ThreadAgentRef(seed.project_b_agent_id, "project"),
        )
        await session.execute(
            text(
                """INSERT INTO runs
                (run_id,thread_id,project_id,owner_user_id,status,
                 multitask_strategy,metadata_json,kwargs_json,finalization_status,
                 message_count,total_input_tokens,total_output_tokens,total_tokens,
                 llm_call_count,lead_agent_tokens,subagent_tokens,middleware_tokens,
                 token_usage_by_model,created_at,updated_at)
                VALUES
                ('task6-live','task6-thread-a',:project_id,:owner,'running',
                 'reject','{}'::json,'{}'::json,'pending',0,0,0,0,0,0,0,0,
                 '{}'::json,now(),now()),
                ('task6-success','task6-thread-a',:project_id,:owner,'success',
                 'reject','{}'::json,'{}'::json,'complete',0,0,0,0,0,0,0,0,
                 '{}'::json,now(),now()),
                ('task6-other','task6-thread-b',:other_project_id,:owner,'running',
                 'reject','{}'::json,'{}'::json,'pending',0,0,0,0,0,0,0,0,
                 '{}'::json,now(),now())"""
            ),
            {
                "project_id": project_id,
                "other_project_id": other_project_id,
                "owner": owner,
            },
        )
        await session.execute(
            text(
                """INSERT INTO channel_connections
                (id,owner_user_id,provider,status,external_account_id,workspace_id,
                 scopes_json,capabilities_json,metadata_json,project_id,
                 created_at,updated_at)
                VALUES ('task6-connection-a',:owner,'slack','connected',
                        'external-task6','workspace-task6','[]'::json,'{}'::json,
                        '{}'::json,:project_id,now(),now())"""
            ),
            {"project_id": project_id, "owner": owner},
        )

    abort_event = asyncio.Event()
    boundary = PrivateRunAuthorizationBoundary(
        seed.factory,
        project_id=project_id,
        owner_user_id=owner,
        run_id="task6-live",
        abort_event=abort_event,
    )
    await boundary.before_model_call()

    async with seed.factory() as session, session.begin():
        await session.execute(
            text(
                """UPDATE project_memberships SET role='editor',version=version+1
                WHERE project_id=:project_id AND user_id=:owner"""
            ),
            {"project_id": project_id, "owner": owner},
        )
    await boundary.before_checkpoint_write()
    await boundary.before_model_call()
    await boundary.before_tool_call()
    await boundary.before_mcp_call()
    await boundary.before_sandbox_exec()
    await boundary.before_sandbox_write()

    from app.private_work.checkpointer import ProjectScopedCheckpointer

    raw = InMemorySaver()
    checkpointer = ProjectScopedCheckpointer(raw, seed.factory).for_context(seed.owner_a)
    checkpointer.set_authorization_boundary(boundary)
    await checkpointer.aput(
        {
            "configurable": {
                "thread_id": "task6-thread-a",
                "checkpoint_ns": "",
            }
        },
        empty_checkpoint(),
        {"source": "input", "step": -1, "parents": {}},
        {},
    )

    async with seed.factory() as session, session.begin():
        run_ids = await PrivateRunAuthorizationService.mark_revoked(
            session,
            project_id=project_id,
            owner_user_id=owner,
            now=NOW,
        )
        await PrivateWorkRetentionService.freeze_owner(
            session,
            project_id=project_id,
            owner_user_id=owner,
            now=NOW,
        )
    assert run_ids == ("task6-live",)

    async with seed.engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    """SELECT run_id,status,authorization_cancel_reason
                    FROM runs ORDER BY run_id"""
                )
            )
        ).all()
        assert [(row.run_id, row.status, row.authorization_cancel_reason) for row in rows] == [
            ("task6-live", "running", AUTHORIZATION_REVOKED_REASON),
            ("task6-other", "running", None),
            ("task6-success", "success", None),
        ]
        thread_rows = (
            await connection.execute(
                text(
                    """SELECT thread_id,frozen_at IS NOT NULL AS frozen
                    FROM threads_meta ORDER BY thread_id"""
                )
            )
        ).all()
        assert [(row.thread_id, row.frozen) for row in thread_rows] == [
            ("task6-thread-a", True),
            ("task6-thread-b", False),
        ]

    with pytest.raises(AuthorizationRevoked):
        await boundary.before_tool_call()
    assert abort_event.is_set()


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_postgres_restore_keeps_colliding_connection_frozen(
    private_seed: M4ThreadSeed,
) -> None:
    seed = private_seed
    owner = str(seed.owner_a.user_id)
    other_owner = str(seed.owner_b.user_id)
    project_id = seed.owner_a.project_id
    async with seed.factory() as session, session.begin():
        await session.execute(
            text(
                """INSERT INTO channel_connections
                (id,owner_user_id,provider,status,external_account_id,workspace_id,
                 scopes_json,capabilities_json,metadata_json,project_id,frozen_at,
                 created_at,updated_at)
                VALUES
                ('task6-frozen',:owner,'slack','frozen','external-collision',
                 'workspace-collision','[]'::json,'{}'::json,'{}'::json,
                 :project_id,now(),now(),now()),
                ('task6-connected',:other_owner,'slack','connected',
                 'external-collision','workspace-collision','[]'::json,'{}'::json,
                 '{}'::json,:project_id,NULL,now(),now())"""
            ),
            {
                "project_id": project_id,
                "owner": owner,
                "other_owner": other_owner,
            },
        )
        restored = await PrivateWorkRetentionService.restore_owner(
            session,
            project_id=project_id,
            owner_user_id=owner,
            now=NOW,
        )
    assert restored.connection_ids == ()
    async with seed.engine.connect() as connection:
        status = (
            await connection.execute(
                text(
                    """SELECT status FROM channel_connections
                    WHERE id='task6-frozen'"""
                )
            )
        ).scalar_one()
    assert status == "frozen"
