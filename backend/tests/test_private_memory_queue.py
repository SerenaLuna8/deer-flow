from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.runtime import Runtime
from sqlalchemy import text
from support.m4_private_threads import seed_m4_thread_database

from deerflow.agents.memory.queue import (
    ProjectMemoryMembershipRevalidator,
    ProjectMemoryUpdateQueue,
)
from deerflow.agents.memory.storage import create_empty_memory
from deerflow.agents.memory.summarization_hook import memory_flush_hook
from deerflow.agents.memory.updater import MemoryUpdater
from deerflow.agents.middlewares.memory_middleware import MemoryMiddleware
from deerflow.agents.middlewares.summarization_middleware import SummarizationEvent
from deerflow.config.memory_config import MemoryConfig
from deerflow.runtime.private_scope import PrivateResourceScope
from deerflow.runtime.user_context import reset_current_user, set_current_user


@dataclass(frozen=True)
class _User:
    id: str


class _Revalidator:
    def __init__(self, active: bool = True) -> None:
        self.active = active
        self.seen: list[PrivateResourceScope] = []

    async def is_active(self, scope: PrivateResourceScope) -> bool:
        self.seen.append(scope)
        return self.active


class _Updater:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def aupdate_project_memory(self, **kwargs: object) -> bool:
        self.calls.append(kwargs)
        return True


class _Storage:
    pass


def _scope(project: str, owner: str, *, version: int = 1) -> PrivateResourceScope:
    return PrivateResourceScope(project_id=project, owner_user_id=owner, membership_version=version)


@pytest.mark.asyncio
async def test_project_queue_uses_enqueued_scope_after_current_user_changes() -> None:
    updater = _Updater()
    revalidator = _Revalidator()
    queue = ProjectMemoryUpdateQueue(
        _Storage(),
        updater=updater,
        revalidator=revalidator,
        debounce_seconds=3600,
    )
    original_scope = _scope("project-a", "owner-a", version=7)

    item = queue.enqueue(
        scope=original_scope,
        thread_id="thread-a",
        run_id="run-a",
        namespace="default",
        messages=["visible conversation"],
    )
    token = set_current_user(_User("other-owner"))
    try:
        assert await queue.flush(item.key) is True
    finally:
        reset_current_user(token)

    assert revalidator.seen == [original_scope]
    assert len(updater.calls) == 1
    assert updater.calls[0]["scope"] == original_scope
    assert updater.calls[0]["thread_id"] == "thread-a"
    assert updater.calls[0]["run_id"] == "run-a"
    assert updater.calls[0]["namespace"] == "default"
    assert updater.calls[0]["messages"] == ("visible conversation",)


@pytest.mark.asyncio
async def test_project_queue_keeps_project_and_owner_targets_separate() -> None:
    updater = _Updater()
    queue = ProjectMemoryUpdateQueue(
        _Storage(),
        updater=updater,
        revalidator=_Revalidator(),
        debounce_seconds=3600,
    )
    items = [
        queue.enqueue(
            scope=scope,
            thread_id="thread",
            run_id=f"run-{index}",
            namespace="default",
            messages=[label],
        )
        for index, (scope, label) in enumerate(
            (
                (_scope("project-a", "owner-a"), "a/a"),
                (_scope("project-a", "owner-b"), "a/b"),
                (_scope("project-b", "owner-a"), "b/a"),
            )
        )
    ]

    assert queue.pending_count == 3
    for item in items:
        assert await queue.flush(item.key) is True

    assert [(call["scope"], call["messages"]) for call in updater.calls] == [
        (_scope("project-a", "owner-a"), ("a/a",)),
        (_scope("project-a", "owner-b"), ("a/b",)),
        (_scope("project-b", "owner-a"), ("b/a",)),
    ]


@pytest.mark.asyncio
async def test_project_queue_drops_item_when_membership_is_no_longer_active() -> None:
    updater = _Updater()
    queue = ProjectMemoryUpdateQueue(
        _Storage(),
        updater=updater,
        revalidator=_Revalidator(active=False),
        debounce_seconds=3600,
    )
    item = queue.enqueue(
        scope=_scope("project-a", "owner-a"),
        thread_id="thread-a",
        run_id="run-a",
        namespace="default",
        messages=["must not persist"],
    )

    assert await queue.flush(item.key) is False
    assert updater.calls == []


@pytest.mark.asyncio
async def test_project_updater_loads_and_saves_the_same_scope_and_version(monkeypatch: pytest.MonkeyPatch) -> None:
    memory = create_empty_memory()
    snapshot = SimpleNamespace(memory=memory, version=4)

    class Storage:
        def __init__(self) -> None:
            self.loaded: list[tuple[PrivateResourceScope, str]] = []
            self.saved: list[dict[str, object]] = []

        async def load(self, *, scope: PrivateResourceScope, namespace: str):
            self.loaded.append((scope, namespace))
            return snapshot

        async def save(self, memory_data, *, scope, namespace, expected_version):
            self.saved.append(
                {
                    "memory": memory_data,
                    "scope": scope,
                    "namespace": namespace,
                    "expected_version": expected_version,
                }
            )
            return SimpleNamespace(memory=memory_data, version=expected_version + 1)

    response = {
        "user": {},
        "history": {},
        "newFacts": [
            {
                "content": "Prefer runnable milestones.",
                "category": "preference",
                "confidence": 0.95,
            }
        ],
        "factsToRemove": [],
    }
    model = MagicMock()
    model.invoke.return_value = SimpleNamespace(content=__import__("json").dumps(response))
    updater = MemoryUpdater()
    monkeypatch.setattr(updater, "_get_model", lambda: model)
    monkeypatch.setattr(
        "deerflow.agents.memory.updater.get_memory_config",
        lambda: MemoryConfig(enabled=True, fact_confidence_threshold=0.7),
    )
    storage = Storage()
    scope = _scope("project-a", "owner-a")

    assert await updater.aupdate_project_memory(
        storage=storage,
        scope=scope,
        namespace="default",
        messages=(HumanMessage(content="Ship it."), AIMessage(content="Done.")),
        thread_id="thread-a",
        run_id="run-a",
    )

    assert storage.loaded == [(scope, "default")]
    assert len(storage.saved) == 1
    saved = storage.saved[0]
    assert saved["scope"] == scope
    assert saved["namespace"] == "default"
    assert saved["expected_version"] == 4
    assert saved["memory"]["facts"][0]["sourceThreadId"] == "thread-a"
    assert saved["memory"]["facts"][0]["sourceRunId"] == "run-a"


@pytest.mark.asyncio
async def test_memory_middleware_enqueues_private_runtime_on_project_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    private_queue = MagicMock()
    monkeypatch.setattr(
        "deerflow.agents.middlewares.memory_middleware.get_project_memory_queue",
        lambda: private_queue,
    )
    scope = _scope("project-a", "owner-a", version=3)
    middleware = MemoryMiddleware(
        agent_name="researcher",
        memory_config=MemoryConfig(enabled=True),
    )

    result = await middleware.aafter_agent(
        {
            "messages": [
                HumanMessage(content="Remember the runnable-first preference."),
                AIMessage(content="I will."),
            ]
        },
        Runtime(
            context={
                "thread_id": "thread-a",
                "run_id": "run-a",
                "private_scope": scope,
            }
        ),
    )

    assert result is None
    private_queue.enqueue.assert_called_once()
    call = private_queue.enqueue.call_args.kwargs
    assert call["scope"] == scope
    assert call["thread_id"] == "thread-a"
    assert call["run_id"] == "run-a"
    assert call["namespace"] == "agent:researcher"
    assert tuple(message.type for message in call["messages"]) == ("human", "ai")


def test_memory_config_rejects_removed_filesystem_storage_options() -> None:
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        MemoryConfig(storage_path="memory.json")
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        MemoryConfig(storage_class="example.LegacyStorage")


def test_sync_memory_middleware_fails_closed_without_project_authority() -> None:
    middleware = MemoryMiddleware(memory_config=MemoryConfig(enabled=True))

    assert (
        middleware.after_agent(
            {
                "messages": [
                    HumanMessage(content="Remember this."),
                    AIMessage(content="Okay."),
                ]
            },
            Runtime(
                context={
                    "thread_id": "thread-a",
                    "run_id": "run-a",
                }
            ),
        )
        is None
    )


@pytest.mark.asyncio
async def test_async_memory_middleware_fails_closed_without_project_authority() -> None:
    middleware = MemoryMiddleware(memory_config=MemoryConfig(enabled=True))

    assert (
        await middleware.aafter_agent(
            {
                "messages": [
                    HumanMessage(content="Remember this."),
                    AIMessage(content="Okay."),
                ]
            },
            Runtime(context={"thread_id": "thread-a", "run_id": "run-a"}),
        )
        is None
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_database_membership_revalidator_drops_a_left_member(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        revalidator = ProjectMemoryMembershipRevalidator(seed.factory)
        assert await revalidator.is_active(seed.owner_a_scope) is True

        async with seed.factory() as session, session.begin():
            await session.execute(
                text(
                    """UPDATE project_memberships
                    SET status='left', ended_at=now(), end_reason='left', version=version+1
                    WHERE project_id=:project_id AND user_id=:owner_user_id"""
                ),
                {
                    "project_id": seed.owner_a_scope.project_id,
                    "owner_user_id": seed.owner_a_scope.owner_user_id,
                },
            )

        assert await revalidator.is_active(seed.owner_a_scope) is False
    finally:
        await seed.engine.dispose()


def test_private_summarization_hook_uses_project_queue_not_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_queue = MagicMock()
    monkeypatch.setattr(
        "deerflow.agents.memory.summarization_hook.get_project_memory_queue",
        lambda: private_queue,
    )
    scope = _scope("project-a", "owner-a")

    memory_flush_hook(
        SummarizationEvent(
            messages_to_summarize=(
                HumanMessage(content="Remember project context."),
                AIMessage(content="Okay."),
            ),
            preserved_messages=(),
            thread_id="thread-a",
            agent_name="researcher",
            runtime=SimpleNamespace(
                context={
                    "thread_id": "thread-a",
                    "run_id": "run-a",
                    "private_scope": scope,
                }
            ),
        )
    )

    private_queue.enqueue.assert_called_once()
    call = private_queue.enqueue.call_args.kwargs
    assert call["scope"] == scope
    assert call["thread_id"] == "thread-a"
    assert call["run_id"] == "run-a"
    assert call["namespace"] == "agent:researcher"


def test_summarization_hook_fails_closed_without_project_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_queue = MagicMock()
    monkeypatch.setattr(
        "deerflow.agents.memory.summarization_hook.get_project_memory_queue",
        lambda: private_queue,
    )

    memory_flush_hook(
        SummarizationEvent(
            messages_to_summarize=(
                HumanMessage(content="Remember global context."),
                AIMessage(content="Okay."),
            ),
            preserved_messages=(),
            thread_id="thread-a",
            agent_name=None,
            runtime=SimpleNamespace(context={"thread_id": "thread-a", "run_id": "run-a"}),
        )
    )

    private_queue.enqueue.assert_not_called()
