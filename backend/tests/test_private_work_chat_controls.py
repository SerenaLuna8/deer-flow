from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import empty_checkpoint, uuid6
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.gateway.routers.private_work import (
    PrivateSuggestionsRequest,
    PrivateThreadCompactRequest,
)
from app.private_work.errors import (
    PrivateWorkConflict,
    PrivateWorkForbidden,
    PrivateWorkNotFound,
)
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.thread_repository import ThreadAgentRef
from deerflow.config.app_config import AppConfig


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


def _message_checkpoint(messages: list[object]) -> dict[str, object]:
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"messages": messages}
    return checkpoint


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
    prepared_checkpoint_id: str | None = None
    initial_items = [item async for item in raw.alist(_config(thread_id))]

    async def prepare_with_concurrent_head_change(reader, selected_thread_id, **_kwargs):
        nonlocal mutated_checkpoint_id, prepared_checkpoint_id
        captured = await reader.aget_tuple(_config(selected_thread_id))
        prepared_checkpoint = copy.deepcopy(captured.checkpoint)
        prepared_checkpoint_id = str(uuid6())
        prepared_checkpoint["id"] = prepared_checkpoint_id

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
            write_config=_config(selected_thread_id),
            checkpoint=prepared_checkpoint,
            metadata={"source": "update", "step": 2, "parents": {}},
            new_versions={},
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
            app_config=AppConfig(),
        )

    current = await saver.aget_tuple(_config(thread_id))
    assert controls.ProjectChatControlService._checkpoint_id(current) == mutated_checkpoint_id
    final_items = [item async for item in raw.alist(_config(thread_id))]
    final_ids = {controls.ProjectChatControlService._checkpoint_id(item) for item in final_items}
    assert prepared_checkpoint_id not in final_ids
    assert len(final_items) == len(initial_items) + 1


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_branch_copies_only_the_selected_scoped_turn(
    seed: M4ThreadSeed,
) -> None:
    service, threads, scoped, _, thread_id = await _create_service(seed)
    saver = scoped.for_context(seed.owner_a)
    await saver.aput(
        _config(thread_id),
        _message_checkpoint(
            [
                HumanMessage(content="branch request", id="human-branch"),
                AIMessage(content="branch response", id="ai-branch"),
            ]
        ),
        {"source": "loop", "step": 1, "parents": {}},
        {},
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
    target_item = await saver.aget_tuple(_config(record.thread_id))
    assert target_item is not None
    assert [message.id for message in service._messages(target_item)] == [
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
                    "run_id": "run-authoritative",
                }
            ]
        )
    )
    service, _, scoped, _, thread_id = await _create_service(
        seed,
        run_event_store=events,
    )
    saver = scoped.for_context(seed.owner_a)
    root = await saver.aget_tuple(_config(thread_id))
    assert root is not None
    root_id = service._checkpoint_id(root)
    await saver.aput(
        _config(thread_id),
        _message_checkpoint([HumanMessage(content="first question", id="human-first")]),
        {"source": "loop", "step": 1, "parents": {}},
        {},
    )
    await saver.aput(
        _config(thread_id),
        _message_checkpoint(
            [
                HumanMessage(content="first question", id="human-first"),
                AIMessage(content="older answer", id="ai-older"),
                HumanMessage(content="latest question", id="human-latest"),
                AIMessage(content="latest answer", id="ai-latest"),
            ]
        ),
        {"source": "loop", "step": 2, "parents": {}},
        {},
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

    assert payload["target_run_id"] == "run-authoritative"
    assert payload["metadata"] == {
        "regenerate_from_message_id": "ai-latest",
        "regenerate_from_run_id": "run-authoritative",
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
async def test_suggestions_use_authoritative_checkpoint_and_thread_agent_model(
    seed: M4ThreadSeed,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.private_work.chat_controls as controls

    service, _, scoped, _, thread_id = await _create_service(seed)
    saver = scoped.for_context(seed.owner_a)
    await saver.aput(
        _config(thread_id),
        _message_checkpoint(
            [
                HumanMessage(content="authoritative question", id="human-suggest"),
                AIMessage(content="authoritative answer", id="ai-suggest"),
            ]
        ),
        {"source": "loop", "step": 1, "parents": {}},
        {},
    )
    model_call = AsyncMock(return_value='["Continue from authoritative state?", "Verify scope?"]')
    monkeypatch.setattr(controls, "run_oneshot_llm", model_call)

    suggestions = await service.suggest(
        seed.owner_a,
        thread_id,
        n=2,
        app_config=AppConfig(),
    )

    assert suggestions == [
        "Continue from authoritative state?",
        "Verify scope?",
    ]
    model_call.assert_awaited_once()
    assert model_call.await_args.kwargs["model_name"] == "test-model"
    assert "authoritative question" in model_call.await_args.kwargs["user_content"]
    assert "authoritative answer" in model_call.await_args.kwargs["user_content"]
