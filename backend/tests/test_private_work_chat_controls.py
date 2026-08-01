from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Overwrite
from pydantic import ValidationError
from sqlalchemy import update
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.gateway.deps import get_current_agent_runtime_config
from app.gateway.routers.private_work import (
    PrivateSuggestionsRequest,
    PrivateThreadCompactRequest,
    compact_private_thread,
)
from app.private_work.errors import (
    PrivateWorkConflict,
    PrivateWorkForbidden,
    PrivateWorkNotFound,
)
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.thread_repository import ThreadAgentRef
from deerflow.config.app_config import AppConfig, get_app_config
from deerflow.persistence.projects.model import ProjectMembershipRow
from deerflow.sandbox.sandbox import AuthorizationRevoked, check_authorization_boundary


def _app_config_with_models(*model_names: str) -> AppConfig:
    return AppConfig.model_validate(
        {
            "sandbox": {
                "use": "deerflow.sandbox.local:LocalSandboxProvider",
            },
            "models": [
                {
                    "name": name,
                    "use": "tests.fake:Model",
                    "model": f"provider/{name}",
                }
                for name in model_names
            ],
        }
    )


@pytest_asyncio.fixture
async def seed(migrated_postgres_database_url: str) -> M4ThreadSeed:
    value = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        yield value
    finally:
        await value.engine.dispose()


def _config(thread_id: str) -> dict[str, object]:
    return {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "",
        }
    }


def _message_checkpoint(
    messages: list[object],
) -> tuple[dict[str, object], dict[str, object]]:
    checkpoint = empty_checkpoint()
    message_version = checkpoint["id"]
    checkpoint["channel_versions"] = {"messages": message_version}
    checkpoint["channel_values"] = {"messages": messages}
    return checkpoint, {"messages": message_version}


async def _create_service(
    seed: M4ThreadSeed,
    *,
    run_event_store: object | None = None,
):
    from app.private_work.chat_controls import ProjectChatControlService
    from app.private_work.checkpointer import ProjectScopedCheckpointer
    from app.private_work.thread_service import PrivateThreadService

    raw = InMemorySaver()
    scoped = ProjectScopedCheckpointer(raw, seed.factory)
    threads = PrivateThreadService(seed.factory, scoped)
    service = ProjectChatControlService(
        seed.factory,
        scoped,
        threads,
        run_event_store or MagicMock(),
    )
    thread_id = str(uuid.uuid4())
    await threads.create(
        seed.owner_a,
        thread_id=thread_id,
        agent=ThreadAgentRef(seed.project_agent_id, "project"),
    )
    return service, threads, scoped, raw, thread_id


def test_chat_control_requests_reject_client_agent_and_conversation_authority() -> None:
    with pytest.raises(ValidationError):
        PrivateThreadCompactRequest.model_validate({"force": True, "agent_name": "forged-agent"})
    with pytest.raises(ValidationError):
        PrivateSuggestionsRequest.model_validate(
            {
                "n": 3,
                "messages": [{"role": "user", "content": "forged context"}],
                "model_name": "forged-model",
            }
        )


def test_manual_compact_depends_on_current_database_agent_policy() -> None:
    dependency = inspect.signature(compact_private_thread).parameters["config"].default
    assert dependency.dependency is get_current_agent_runtime_config


@pytest.mark.asyncio
async def test_manual_compact_materializes_current_dedicated_model() -> None:
    from app.private_work.chat_controls import ProjectChatControlService

    service = object.__new__(ProjectChatControlService)
    exact_model = _app_config_with_models("summary-live").models[0]
    materialize = AsyncMock(return_value=exact_model)
    service._model_materializer = SimpleNamespace(
        materialize_active=materialize,
    )
    service._resolve_agent_authority = AsyncMock(
        side_effect=AssertionError("dedicated model must not read Agent authority"),
    )
    source = AppConfig.model_validate(
        {
            "sandbox": {"use": "test"},
            "models": [
                {
                    "name": "stale-yaml-model",
                    "use": "tests.fake:Model",
                    "model": "provider/stale",
                }
            ],
            "summarization": {"model_name": "summary-live"},
        }
    )

    runtime = await service._materialize_compaction_config(
        SimpleNamespace(request_id="request-compact"),
        "thread-a",
        source,
    )

    materialize.assert_awaited_once_with("summary-live")
    assert [model.name for model in runtime.models] == ["summary-live"]
    assert runtime.summarization.model_name == "summary-live"
    assert [model.name for model in source.models] == ["stale-yaml-model"]


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_compact_validates_private_scope_before_dedicated_model_materialization(
    seed: M4ThreadSeed,
) -> None:
    service, _, _, _, thread_id = await _create_service(seed)
    exact_model = _app_config_with_models("summary-live").models[0]
    materialize = AsyncMock(return_value=exact_model)
    service._model_materializer = SimpleNamespace(
        materialize_active=materialize,
    )
    source = AppConfig.model_validate(
        {
            "sandbox": {"use": "test"},
            "models": [
                {
                    "name": "stale-yaml-model",
                    "use": "tests.fake:Model",
                    "model": "provider/stale",
                }
            ],
            "summarization": {"model_name": "summary-live"},
        }
    )

    with pytest.raises(PrivateWorkForbidden):
        await service.compact(
            seed.viewer,
            thread_id,
            force=True,
            keep=None,
            app_config=source,
        )
    with pytest.raises(PrivateWorkNotFound):
        await service.compact(
            seed.owner_b,
            thread_id,
            force=True,
            keep=None,
            app_config=source,
        )

    async with seed.factory() as session, session.begin():
        await PrivateRunRepository(session).create(
            scope=seed.owner_a.resource_scope,
            thread_id=thread_id,
            request=PrivateRunCreate(
                run_id=str(uuid.uuid4()),
                status="running",
                metadata={},
                kwargs={},
                model_name="test-model",
            ),
        )
    with pytest.raises(PrivateWorkConflict):
        await service.compact(
            seed.owner_a,
            thread_id,
            force=True,
            keep=None,
            app_config=source,
        )

    materialize.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_compact_falls_back_to_current_thread_agent_model() -> None:
    from app.private_work.chat_controls import ProjectChatControlService

    service = object.__new__(ProjectChatControlService)
    exact_model = _app_config_with_models("agent-live").models[0]
    materialize = AsyncMock(return_value=exact_model)
    resolve_agent = AsyncMock(
        return_value=SimpleNamespace(
            payload=SimpleNamespace(model_ref="agent-live"),
        )
    )
    service._model_materializer = SimpleNamespace(
        materialize_active=materialize,
    )
    service._resolve_agent_authority = resolve_agent
    source = _app_config_with_models("stale-yaml-model")

    runtime = await service._materialize_compaction_config(
        SimpleNamespace(request_id="request-compact"),
        "thread-a",
        source,
    )

    resolve_agent.assert_awaited_once()
    materialize.assert_awaited_once_with("agent-live")
    assert [model.name for model in runtime.models] == ["agent-live"]
    assert runtime.summarization.model_name is None


@pytest.mark.asyncio
async def test_private_request_authorization_boundary_preserves_private_failure() -> None:
    from app.private_work.authorization import PrivateRequestAuthorizationBoundary

    checker = AsyncMock(side_effect=PrivateWorkForbidden("request-compact"))
    boundary = PrivateRequestAuthorizationBoundary(
        checker,
        request_id="request-compact",
    )

    with pytest.raises(AuthorizationRevoked):
        await boundary.before_model_call()

    checker.assert_awaited_once_with()
    error = boundary.private_error()
    assert type(error) is PrivateWorkForbidden
    assert error.request_id == "request-compact"


@pytest.mark.asyncio
async def test_prepare_compaction_forwards_request_authority_to_model_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.runtime import context_compaction

    model_calls: list[str] = []

    class _BoundaryAwareCompactionMiddleware:
        async def acompact_state(self, state, runtime, *, force=False):
            del state, force
            await check_authorization_boundary(
                runtime.context,
                "before_model_call",
            )
            model_calls.append("called")
            raise AssertionError("revoked authority must stop before the model")

    monkeypatch.setattr(
        context_compaction,
        "_create_compaction_middleware",
        lambda **_kwargs: _BoundaryAwareCompactionMiddleware(),
    )
    boundary = SimpleNamespace(
        before_model_call=AsyncMock(side_effect=AuthorizationRevoked()),
    )
    snapshot = SimpleNamespace(
        config={
            "configurable": {
                "thread_id": "thread-a",
                "checkpoint_ns": "",
                "checkpoint_id": "checkpoint-a",
            }
        },
        values={
            "messages": [
                HumanMessage(content="one", id="human-one"),
                AIMessage(content="two", id="ai-two"),
            ]
        },
    )

    with pytest.raises(AuthorizationRevoked):
        await context_compaction.prepare_thread_compaction(
            MagicMock(),
            "thread-a",
            app_config=_app_config_with_models("test-model"),
            snapshot=snapshot,
            authorization_boundary=boundary,
        )

    boundary.before_model_call.assert_awaited_once_with()
    assert model_calls == []


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_goal_is_project_owner_scoped_and_mutation_is_capability_gated(
    seed: M4ThreadSeed,
) -> None:
    service, _, _, _, thread_id = await _create_service(seed)

    assert await service.get_goal(seed.owner_a, thread_id) is None
    goal = await service.set_goal(
        seed.owner_a,
        thread_id,
        objective="Ship the scoped chat controls",
    )
    assert (await service.get_goal(seed.owner_a, thread_id)) == goal

    with pytest.raises(PrivateWorkForbidden):
        await service.set_goal(
            seed.viewer,
            thread_id,
            objective="must not mutate",
        )
    with pytest.raises(PrivateWorkNotFound):
        await service.get_goal(seed.owner_b, thread_id)
    with pytest.raises(PrivateWorkNotFound):
        await service.get_goal(seed.project_b_owner_a, thread_id)

    await service.clear_goal(seed.owner_a, thread_id)
    assert await service.get_goal(seed.owner_a, thread_id) is None

    async with seed.factory() as session, session.begin():
        await PrivateRunRepository(session).create(
            scope=seed.owner_a.resource_scope,
            thread_id=thread_id,
            request=PrivateRunCreate(
                run_id=str(uuid.uuid4()),
                status="running",
                metadata={},
                kwargs={},
                model_name="test-model",
            ),
        )
    with pytest.raises(PrivateWorkConflict):
        await service.set_goal(
            seed.owner_a,
            thread_id,
            objective="must wait for the active Run",
        )
    assert await service.get_goal(seed.owner_a, thread_id) is None


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_compact_rechecks_head_and_refuses_stale_prepared_write(
    seed: M4ThreadSeed,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.private_work.chat_controls as controls
    from deerflow.runtime.context_compaction import (
        PreparedThreadCompaction,
        ThreadCompactionResult,
    )

    service, _, scoped, raw, thread_id = await _create_service(seed)
    saver = scoped.for_context(seed.owner_a)
    source = await saver.aget_tuple(_config(thread_id))
    assert source is not None
    source_id = controls.ProjectChatControlService._checkpoint_id(source)
    assert source_id is not None
    mutated_checkpoint_id: str | None = None
    initial_items = [item async for item in raw.alist(_config(thread_id))]

    async def prepare_with_concurrent_head_change(reader, selected_thread_id, **_kwargs):
        nonlocal mutated_checkpoint_id
        captured = await reader.aget(_config(selected_thread_id))

        mutation = await saver.aput(
            _config(selected_thread_id),
            empty_checkpoint(),
            {"source": "loop", "step": 1, "parents": {}},
            {},
        )
        mutated_checkpoint_id = mutation["configurable"]["checkpoint_id"]
        return PreparedThreadCompaction(
            thread_id=selected_thread_id,
            source_checkpoint_id=source_id,
            result=ThreadCompactionResult(
                thread_id=selected_thread_id,
                compacted=True,
                removed_message_count=2,
                preserved_message_count=1,
                summary_updated=True,
            ),
            write_config=captured.config,
            update_values={
                "messages": Overwrite([]),
                "summary_text": "prepared",
            },
        )

    monkeypatch.setattr(
        controls,
        "prepare_thread_compaction",
        prepare_with_concurrent_head_change,
    )

    with pytest.raises(PrivateWorkConflict):
        await service.compact(
            seed.owner_a,
            thread_id,
            force=True,
            keep=None,
            app_config=_app_config_with_models("test-model"),
        )

    current = await saver.aget_tuple(_config(thread_id))
    assert controls.ProjectChatControlService._checkpoint_id(current) == mutated_checkpoint_id
    final_items = [item async for item in raw.alist(_config(thread_id))]
    assert len(final_items) == len(initial_items) + 1


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_compact_revalidates_authority_immediately_before_summary_model(
    seed: M4ThreadSeed,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.private_work.chat_controls as controls
    from deerflow.agents.middlewares.summarization_middleware import ContextCompactionResult
    from deerflow.runtime import context_compaction

    service, _, scoped, _, thread_id = await _create_service(seed)
    saver = scoped.for_context(seed.owner_a)
    root = await saver.aget_tuple(_config(thread_id))
    assert root is not None
    checkpoint, new_versions = _message_checkpoint(
        [
            HumanMessage(content="first", id="human-first"),
            AIMessage(content="first answer", id="ai-first"),
            HumanMessage(content="second", id="human-second"),
            AIMessage(content="second answer", id="ai-second"),
        ]
    )
    await saver.aput(
        root.config,
        checkpoint,
        {"source": "loop", "step": 1, "parents": {}},
        new_versions,
    )

    model_calls: list[str] = []

    class _BoundaryAwareCompactionMiddleware:
        async def acompact_state(self, state, runtime, *, force=False):
            del force
            await check_authorization_boundary(
                runtime.context,
                "before_model_call",
            )
            model_calls.append("called")
            messages = list(state["messages"])
            return ContextCompactionResult(
                summary_text="must not be generated",
                messages_to_summarize=tuple(messages[:-1]),
                preserved_messages=tuple(messages[-1:]),
                total_tokens=len(messages),
            )

    monkeypatch.setattr(
        context_compaction,
        "_create_compaction_middleware",
        lambda **_kwargs: _BoundaryAwareCompactionMiddleware(),
    )
    prepare = controls.prepare_thread_compaction

    async def revoke_then_prepare(*args, **kwargs):
        async with seed.factory() as session, session.begin():
            await session.execute(
                update(ProjectMembershipRow)
                .where(ProjectMembershipRow.id == seed.owner_a.membership_id)
                .values(
                    status="removed",
                    version=ProjectMembershipRow.version + 1,
                    ended_at=datetime.now(UTC),
                    ended_by_user_id=str(seed.owner_a.user_id),
                    end_reason="removed",
                )
            )
        return await prepare(*args, **kwargs)

    monkeypatch.setattr(
        controls,
        "prepare_thread_compaction",
        revoke_then_prepare,
    )

    with pytest.raises(PrivateWorkNotFound):
        await service.compact(
            seed.owner_a,
            thread_id,
            force=True,
            keep=None,
            app_config=_app_config_with_models("test-model"),
        )

    assert model_calls == []


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_branch_copies_only_the_selected_scoped_turn(
    seed: M4ThreadSeed,
) -> None:
    service, threads, scoped, _, thread_id = await _create_service(seed)
    saver = scoped.for_context(seed.owner_a)
    root = await saver.aget_tuple(_config(thread_id))
    assert root is not None
    checkpoint, new_versions = _message_checkpoint(
        [
            HumanMessage(content="branch request", id="human-branch"),
            AIMessage(content="branch response", id="ai-branch"),
        ]
    )
    await saver.aput(
        root.config,
        checkpoint,
        {"source": "loop", "step": 1, "parents": {}},
        new_versions,
    )

    record, checkpoint_id = await service.branch(
        seed.owner_a,
        thread_id,
        message_id="ai-branch",
        message_ids=["ai-branch"],
        title="Scoped branch",
    )

    assert record.metadata["branch_parent_thread_id"] == thread_id
    assert record.metadata["branch_parent_checkpoint_id"] == checkpoint_id
    assert record.metadata["workspace_clone_mode"] == "current_thread_authority_copy"
    target = await threads.get(seed.owner_a, record.thread_id)
    assert target is not None
    assert target.display_name == "Scoped branch"
    target_state = await service._state(
        seed.owner_a,
        get_app_config(),
        as_node="branch_test",
    ).aget(_config(record.thread_id))
    assert [message.id for message in target_state.values["messages"]] == [
        "human-branch",
        "ai-branch",
    ]
    assert await threads.get(seed.owner_b, record.thread_id) is None
    assert await threads.get(seed.project_b_owner_a, record.thread_id) is None


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_regenerate_uses_latest_ai_scoped_checkpoint_and_durable_run_event(
    seed: M4ThreadSeed,
) -> None:
    run_id = str(uuid.uuid4())
    events = SimpleNamespace(
        list_messages=AsyncMock(
            return_value=[
                {
                    "event_type": "llm.ai.response",
                    "content": {
                        "type": "ai",
                        "id": "ai-latest",
                        "content": "latest answer",
                    },
                    "run_id": run_id,
                }
            ]
        )
    )
    service, _, scoped, _, thread_id = await _create_service(
        seed,
        run_event_store=events,
    )
    async with seed.factory() as session, session.begin():
        await PrivateRunRepository(session).create(
            scope=seed.owner_a.resource_scope,
            thread_id=thread_id,
            request=PrivateRunCreate(run_id=run_id, status="success"),
        )
    saver = scoped.for_context(seed.owner_a)
    root = await saver.aget_tuple(_config(thread_id))
    assert root is not None
    root_id = service._checkpoint_id(root)
    checkpoint, new_versions = _message_checkpoint([HumanMessage(content="first question", id="human-first")])
    first_turn_config = await saver.aput(
        root.config,
        checkpoint,
        {"source": "loop", "step": 1, "parents": {}},
        new_versions,
    )
    checkpoint, new_versions = _message_checkpoint(
        [
            HumanMessage(content="first question", id="human-first"),
            AIMessage(content="older answer", id="ai-older"),
            HumanMessage(content="latest question", id="human-latest"),
            AIMessage(content="latest answer", id="ai-latest"),
        ]
    )
    await saver.aput(
        first_turn_config,
        checkpoint,
        {"source": "loop", "step": 2, "parents": {}},
        new_versions,
    )

    with pytest.raises(PrivateWorkConflict):
        await service.prepare_regenerate(
            seed.owner_a,
            thread_id,
            message_id="ai-older",
        )

    payload = await service.prepare_regenerate(
        seed.owner_a,
        thread_id,
        message_id="ai-latest",
    )

    assert payload["target_run_id"] == run_id
    assert payload["metadata"] == {
        "regenerate_from_message_id": "ai-latest",
        "regenerate_from_run_id": run_id,
        "regenerate_checkpoint_id": payload["checkpoint"]["checkpoint_id"],
    }
    assert payload["input"]["messages"][0]["id"] == "human-latest"
    assert payload["checkpoint"]["checkpoint_id"] != root_id
    events.list_messages.assert_awaited_once_with(
        thread_id,
        limit=200,
        scope=seed.owner_a.resource_scope,
    )


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_regenerate_follows_the_head_parent_lineage_not_a_newer_sibling(
    seed: M4ThreadSeed,
) -> None:
    run_id = str(uuid.uuid4())
    events = SimpleNamespace(
        list_messages=AsyncMock(
            return_value=[
                {
                    "event_type": "llm.ai.response",
                    "content": {"type": "ai", "id": "ai-target", "content": "answer"},
                    "run_id": run_id,
                }
            ]
        )
    )
    service, _, scoped, _, thread_id = await _create_service(seed, run_event_store=events)
    saver = scoped.for_context(seed.owner_a)
    root = await saver.aget_tuple(_config(thread_id))
    assert root is not None
    async with seed.factory() as session, session.begin():
        await PrivateRunRepository(session).create(
            scope=seed.owner_a.resource_scope,
            thread_id=thread_id,
            request=PrivateRunCreate(run_id=run_id, status="success"),
        )

    base_checkpoint, base_versions = _message_checkpoint(
        [
            HumanMessage(content="first", id="human-first"),
            AIMessage(content="first answer", id="ai-first"),
        ]
    )
    base_config = await saver.aput(
        _config(thread_id),
        base_checkpoint,
        {"source": "loop", "step": 1, "parents": {}},
        base_versions,
    )
    sibling_checkpoint, sibling_versions = _message_checkpoint(
        [
            HumanMessage(content="first", id="human-first"),
            AIMessage(content="first answer", id="ai-first"),
            HumanMessage(content="sibling", id="human-sibling"),
            AIMessage(content="sibling answer", id="ai-sibling"),
        ]
    )
    sibling_config = await saver.aput(
        base_config,
        sibling_checkpoint,
        {"source": "loop", "step": 2, "parents": {}},
        sibling_versions,
    )
    head_checkpoint, head_versions = _message_checkpoint(
        [
            HumanMessage(content="first", id="human-first"),
            AIMessage(content="first answer", id="ai-first"),
            HumanMessage(content="target", id="human-target"),
            AIMessage(content="answer", id="ai-target"),
        ]
    )
    await saver.aput(
        base_config,
        head_checkpoint,
        {"source": "loop", "step": 2, "parents": {}},
        head_versions,
    )

    payload = await service.prepare_regenerate(
        seed.owner_a,
        thread_id,
        message_id="ai-target",
    )

    assert payload["checkpoint"]["checkpoint_id"] == base_config["configurable"]["checkpoint_id"]
    assert payload["checkpoint"]["checkpoint_id"] != sibling_config["configurable"]["checkpoint_id"]


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_edit_regenerate_returns_a_new_human_message_and_strict_edit_metadata(
    seed: M4ThreadSeed,
) -> None:
    run_id = str(uuid.uuid4())
    events = SimpleNamespace(
        list_messages=AsyncMock(
            return_value=[
                {
                    "event_type": "llm.ai.response",
                    "content": {"type": "ai", "id": "ai-edit", "content": "answer"},
                    "run_id": run_id,
                }
            ]
        )
    )
    service, _, scoped, _, thread_id = await _create_service(seed, run_event_store=events)
    async with seed.factory() as session, session.begin():
        await PrivateRunRepository(session).create(
            scope=seed.owner_a.resource_scope,
            thread_id=thread_id,
            request=PrivateRunCreate(run_id=run_id, status="success"),
        )
    saver = scoped.for_context(seed.owner_a)
    root = await saver.aget_tuple(_config(thread_id))
    assert root is not None
    checkpoint, versions = _message_checkpoint(
        [
            HumanMessage(
                content="old question",
                id="human-edit",
                additional_kwargs={
                    "run_id": run_id,
                    "files": [{"path": "/uploads/source.txt"}],
                    "private": "drop",
                },
            ),
            AIMessage(content="answer", id="ai-edit"),
        ]
    )
    await saver.aput(
        root.config,
        checkpoint,
        {"source": "loop", "step": 1, "parents": {}},
        versions,
    )

    payload = await service.prepare_edit_regenerate(
        seed.owner_a,
        thread_id,
        human_message_id="human-edit",
        replacement_text="  revised question  ",
    )

    replacement = payload["input"]["messages"][0]
    assert replacement["id"] == payload["replacement_human_message_id"]
    assert replacement["content"] == [{"type": "text", "text": "revised question"}]
    assert replacement["additional_kwargs"] == {
        "files": [{"path": "/uploads/source.txt"}],
    }
    assert payload["source_message_ids"] == ["human-edit", "ai-edit"]
    assert payload["target_run_id"] == run_id
    assert payload["metadata"] == {
        "replay_kind": "edit",
        "regenerate_from_message_id": "ai-edit",
        "regenerate_from_run_id": run_id,
        "regenerate_checkpoint_id": payload["checkpoint"]["checkpoint_id"],
        "edit_from_message_id": "human-edit",
        "edit_message_id": payload["replacement_human_message_id"],
        "edit_version_group_id": "human-edit",
    }
    with pytest.raises(PrivateWorkNotFound):
        await service.prepare_edit_regenerate(
            seed.owner_b,
            thread_id,
            human_message_id="human-edit",
            replacement_text="other owner",
        )
    with pytest.raises(PrivateWorkNotFound):
        await service.prepare_edit_regenerate(
            seed.project_b_owner_a,
            thread_id,
            human_message_id="human-edit",
            replacement_text="other project",
        )


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_edit_regenerate_rejects_an_active_goal(
    seed: M4ThreadSeed,
) -> None:
    run_id = str(uuid.uuid4())
    events = SimpleNamespace(
        list_messages=AsyncMock(
            return_value=[
                {
                    "event_type": "llm.ai.response",
                    "content": {"type": "ai", "id": "ai-goal", "content": "answer"},
                    "run_id": run_id,
                }
            ]
        )
    )
    service, _, scoped, _, thread_id = await _create_service(seed, run_event_store=events)
    async with seed.factory() as session, session.begin():
        await PrivateRunRepository(session).create(
            scope=seed.owner_a.resource_scope,
            thread_id=thread_id,
            request=PrivateRunCreate(run_id=run_id, status="success"),
        )
    saver = scoped.for_context(seed.owner_a)
    root = await saver.aget_tuple(_config(thread_id))
    assert root is not None
    checkpoint, versions = _message_checkpoint(
        [
            HumanMessage(content="question", id="human-goal"),
            AIMessage(content="answer", id="ai-goal"),
        ]
    )
    checkpoint["channel_values"]["goal"] = {"status": "active"}
    checkpoint["channel_versions"]["goal"] = checkpoint["id"]
    versions["goal"] = checkpoint["id"]
    await saver.aput(
        root.config,
        checkpoint,
        {"source": "loop", "step": 1, "parents": {}},
        versions,
    )
    with pytest.raises(PrivateWorkConflict):
        await service.prepare_edit_regenerate(
            seed.owner_a,
            thread_id,
            human_message_id="human-goal",
            replacement_text="revised",
        )


@pytest.mark.asyncio
@pytest.mark.postgres
@pytest.mark.parametrize("blocking_status", ["running", "error"])
async def test_edit_regenerate_rejects_active_work_or_a_non_success_source(
    seed: M4ThreadSeed,
    blocking_status: str,
) -> None:
    source_run_id = str(uuid.uuid4())
    events = SimpleNamespace(
        list_messages=AsyncMock(
            return_value=[
                {
                    "event_type": "llm.ai.response",
                    "content": {
                        "type": "ai",
                        "id": "ai-blocked",
                        "content": "answer",
                    },
                    "run_id": source_run_id,
                }
            ]
        )
    )
    service, _, scoped, _, thread_id = await _create_service(
        seed,
        run_event_store=events,
    )
    async with seed.factory() as session, session.begin():
        await PrivateRunRepository(session).create(
            scope=seed.owner_a.resource_scope,
            thread_id=thread_id,
            request=PrivateRunCreate(
                run_id=source_run_id,
                status="error" if blocking_status == "error" else "success",
            ),
        )
        if blocking_status == "running":
            await PrivateRunRepository(session).create(
                scope=seed.owner_a.resource_scope,
                thread_id=thread_id,
                request=PrivateRunCreate(
                    run_id=str(uuid.uuid4()),
                    status="running",
                ),
            )
    saver = scoped.for_context(seed.owner_a)
    root = await saver.aget_tuple(_config(thread_id))
    assert root is not None
    checkpoint, versions = _message_checkpoint(
        [
            HumanMessage(content="question", id="human-blocked"),
            AIMessage(content="answer", id="ai-blocked"),
        ]
    )
    await saver.aput(
        root.config,
        checkpoint,
        {"source": "loop", "step": 1, "parents": {}},
        versions,
    )

    with pytest.raises(PrivateWorkConflict):
        await service.prepare_edit_regenerate(
            seed.owner_a,
            thread_id,
            human_message_id="human-blocked",
            replacement_text="revised",
        )


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_suggestions_use_authoritative_checkpoint_and_thread_agent_model(
    seed: M4ThreadSeed,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.private_work.chat_controls as controls

    service, _, scoped, _, thread_id = await _create_service(seed)
    saver = scoped.for_context(seed.owner_a)
    checkpoint, new_versions = _message_checkpoint(
        [
            HumanMessage(content="authoritative question", id="human-suggest"),
            AIMessage(content="authoritative answer", id="ai-suggest"),
        ]
    )
    await saver.aput(
        _config(thread_id),
        checkpoint,
        {"source": "loop", "step": 1, "parents": {}},
        new_versions,
    )
    model_call = AsyncMock(return_value='["Continue from authoritative state?", "Verify scope?"]')
    monkeypatch.setattr(controls, "run_oneshot_llm", model_call)

    suggestions = await service.suggest(
        seed.owner_a,
        thread_id,
        n=2,
        app_config=_app_config_with_models("test-model"),
    )

    assert suggestions == [
        "Continue from authoritative state?",
        "Verify scope?",
    ]
    model_call.assert_awaited_once()
    assert model_call.await_args.kwargs["model_name"] == "test-model"
    assert "authoritative question" in model_call.await_args.kwargs["user_content"]
    assert "authoritative answer" in model_call.await_args.kwargs["user_content"]


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_suggestions_resolve_default_agent_model_to_exact_configured_name(
    seed: M4ThreadSeed,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.private_work.chat_controls as controls

    service, _, scoped, _, thread_id = await _create_service(seed)
    saver = scoped.for_context(seed.owner_a)
    checkpoint, new_versions = _message_checkpoint(
        [
            HumanMessage(content="继续验证", id="human-default-suggest"),
            AIMessage(content="可以继续", id="ai-default-suggest"),
        ]
    )
    await saver.aput(
        _config(thread_id),
        checkpoint,
        {"source": "loop", "step": 1, "parents": {}},
        new_versions,
    )
    monkeypatch.setattr(
        service,
        "_resolve_agent_authority",
        AsyncMock(
            return_value=SimpleNamespace(
                payload=SimpleNamespace(model_ref="default"),
            )
        ),
    )
    model_call = AsyncMock(return_value='["继续下一轮？"]')
    monkeypatch.setattr(controls, "run_oneshot_llm", model_call)

    suggestions = await service.suggest(
        seed.owner_a,
        thread_id,
        n=1,
        app_config=_app_config_with_models(
            "primary-logical",
            "secondary-logical",
        ),
    )

    assert suggestions == ["继续下一轮？"]
    assert model_call.await_args.kwargs["model_name"] == "primary-logical"
