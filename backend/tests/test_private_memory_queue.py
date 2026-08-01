from __future__ import annotations

import asyncio
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
from deerflow.config.app_config import AppConfig
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
        self.called = asyncio.Event()

    async def aupdate_project_memory(self, **kwargs: object) -> bool:
        self.calls.append(kwargs)
        self.called.set()
        return True


class _BlockingUpdater:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.active = 0
        self.peak = 0

    async def aupdate_project_memory(self, **kwargs: object) -> bool:
        self.calls.append(kwargs)
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.started.set()
        try:
            await self.release.wait()
        finally:
            self.active -= 1
        return True


class _Storage:
    pass


def _scope(project: str, owner: str, *, version: int = 1) -> PrivateResourceScope:
    return PrivateResourceScope(project_id=project, owner_user_id=owner, membership_version=version)


def _app_config(*, memory: MemoryConfig | None = None) -> AppConfig:
    return AppConfig.model_validate(
        {
            "sandbox": {
                "use": "deerflow.sandbox.local:LocalSandboxProvider",
            },
            "memory": (memory or MemoryConfig()).model_dump(mode="python"),
        }
    )


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
    admitted_memory_config = MemoryConfig(
        debounce_seconds=17,
        fact_confidence_threshold=0.91,
    )
    admitted_app_config = _app_config(memory=admitted_memory_config)

    item = queue.enqueue(
        scope=original_scope,
        thread_id="thread-a",
        run_id="run-a",
        namespace="default",
        messages=["visible conversation"],
        memory_config=admitted_memory_config,
        app_config=admitted_app_config,
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
    assert updater.calls[0]["memory_config"] == admitted_memory_config
    assert updater.calls[0]["memory_config"] is not admitted_memory_config
    assert item.memory_config is updater.calls[0]["memory_config"]
    assert updater.calls[0]["app_config"] == admitted_app_config
    assert updater.calls[0]["app_config"] is not admitted_app_config
    assert item.app_config is updater.calls[0]["app_config"]
    assert "app_config" not in repr(item)
    assert updater.calls[0]["langfuse_trace_correlation_enabled"] is False


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
            memory_config=MemoryConfig(),
            app_config=_app_config(),
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
        memory_config=MemoryConfig(),
        app_config=_app_config(),
    )

    assert await queue.flush(item.key) is False
    assert updater.calls == []


@pytest.mark.asyncio
async def test_immediate_enqueue_flushes_existing_normal_pending_once_without_overwriting() -> None:
    updater = _Updater()
    queue = ProjectMemoryUpdateQueue(
        _Storage(),
        updater=updater,
        revalidator=_Revalidator(),
        debounce_seconds=3600,
    )
    scope = _scope("project-a", "owner-a")
    normal = queue.enqueue(
        scope=scope,
        thread_id="thread-a",
        run_id="run-normal",
        namespace="default",
        messages=["complete normal snapshot"],
        memory_config=MemoryConfig(),
        app_config=_app_config(),
        deerflow_trace_id="trace-normal",
    )

    selected = queue.enqueue_immediate(
        scope=scope,
        thread_id="thread-a",
        run_id="run-normal",
        namespace="default",
        messages=["older messages being compressed"],
        memory_config=MemoryConfig(),
        app_config=_app_config(),
        correction_detected=True,
        deerflow_trace_id="trace-compression",
    )

    await asyncio.wait_for(updater.called.wait(), timeout=1)
    assert selected is normal
    assert len(updater.calls) == 1
    assert updater.calls[0]["messages"] == ("complete normal snapshot",)
    assert updater.calls[0]["run_id"] == "run-normal"
    assert updater.calls[0]["correction_detected"] is False
    assert updater.calls[0]["deerflow_trace_id"] == "trace-normal"
    assert queue.pending_count == 0
    await queue.clear()


@pytest.mark.asyncio
async def test_immediate_enqueue_without_normal_pending_persists_once() -> None:
    updater = _Updater()
    queue = ProjectMemoryUpdateQueue(
        _Storage(),
        updater=updater,
        revalidator=_Revalidator(),
        debounce_seconds=3600,
    )
    scope = _scope("project-a", "owner-a")

    selected = queue.enqueue_immediate(
        scope=scope,
        thread_id="thread-a",
        run_id="run-compression",
        namespace="default",
        messages=["messages being compressed"],
        memory_config=MemoryConfig(),
        app_config=_app_config(),
        correction_detected=True,
        deerflow_trace_id="trace-compression",
    )

    await asyncio.wait_for(updater.called.wait(), timeout=1)
    assert selected.messages == ("messages being compressed",)
    assert len(updater.calls) == 1
    assert updater.calls[0]["messages"] == ("messages being compressed",)
    assert updater.calls[0]["run_id"] == "run-compression"
    assert updater.calls[0]["correction_detected"] is True
    assert updater.calls[0]["deerflow_trace_id"] == "trace-compression"
    assert queue.pending_count == 0
    await queue.clear()


@pytest.mark.asyncio
async def test_immediate_enqueue_does_not_duplicate_normal_item_already_processing() -> None:
    updater = _BlockingUpdater()
    queue = ProjectMemoryUpdateQueue(
        _Storage(),
        updater=updater,
        revalidator=_Revalidator(),
        debounce_seconds=0,
    )
    scope = _scope("project-a", "owner-a")
    normal = queue.enqueue(
        scope=scope,
        thread_id="thread-a",
        run_id="run-normal",
        namespace="default",
        messages=["complete normal snapshot"],
        memory_config=MemoryConfig(),
        app_config=_app_config(),
    )
    await asyncio.wait_for(updater.started.wait(), timeout=1)

    try:
        selected = queue.enqueue_immediate(
            scope=scope,
            thread_id="thread-a",
            run_id="run-normal",
            namespace="default",
            messages=["overlapping compressed messages"],
            memory_config=MemoryConfig(),
            app_config=_app_config(),
        )
        await asyncio.sleep(0)

        assert selected is normal
        assert len(updater.calls) == 1
        assert updater.peak == 1
        assert queue.pending_count == 0
    finally:
        updater.release.set()
        await queue.flush_all()
        await asyncio.sleep(0)
        await queue.clear()


@pytest.mark.asyncio
async def test_immediate_enqueue_serializes_newer_normal_pending_behind_active_write() -> None:
    updater = _BlockingUpdater()
    queue = ProjectMemoryUpdateQueue(
        _Storage(),
        updater=updater,
        revalidator=_Revalidator(),
        debounce_seconds=0,
    )
    scope = _scope("project-a", "owner-a")
    first_app_config = _app_config(
        memory=MemoryConfig(model_name="first-run-model"),
    )
    newer_app_config = _app_config(
        memory=MemoryConfig(model_name="newer-run-model"),
    )
    queue.enqueue(
        scope=scope,
        thread_id="thread-a",
        run_id="run-first",
        namespace="default",
        messages=["first complete snapshot"],
        memory_config=MemoryConfig(),
        app_config=first_app_config,
    )
    await asyncio.wait_for(updater.started.wait(), timeout=1)
    newer = queue.enqueue(
        scope=scope,
        thread_id="thread-a",
        run_id="run-newer",
        namespace="default",
        messages=["newer complete snapshot"],
        memory_config=MemoryConfig(),
        app_config=newer_app_config,
    )
    await asyncio.sleep(0)

    try:
        selected = queue.enqueue_immediate(
            scope=scope,
            thread_id="thread-a",
            run_id="run-newer",
            namespace="default",
            messages=["overlapping compressed messages"],
            memory_config=MemoryConfig(),
            app_config=newer_app_config,
        )
        await asyncio.sleep(0)

        assert selected is newer
        assert len(updater.calls) == 1
        assert updater.peak == 1
    finally:
        updater.release.set()
        await queue.flush_all()
        await asyncio.sleep(0)
        await queue.clear()

    assert [call["messages"] for call in updater.calls] == [
        ("first complete snapshot",),
        ("newer complete snapshot",),
    ]
    assert [call["app_config"].memory.model_name for call in updater.calls] == ["first-run-model", "newer-run-model"]
    assert updater.peak == 1


def test_memory_model_none_uses_first_model_from_exact_run_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deerflow.agents.memory.updater as updater_module

    exact = AppConfig.model_validate(
        {
            "sandbox": {"use": "test"},
            "models": [
                {
                    "name": "lead-snapshot",
                    "use": "tests.fake:Model",
                    "model": "provider/lead-snapshot",
                }
            ],
        }
    )
    captured: dict[str, object] = {}
    marker = object()

    def _create_chat_model(*, name, thinking_enabled, app_config):
        captured.update(
            name=name,
            thinking_enabled=thinking_enabled,
            app_config=app_config,
        )
        return marker

    monkeypatch.setattr(
        updater_module,
        "create_chat_model",
        _create_chat_model,
    )

    assert MemoryUpdater()._get_model(MemoryConfig(model_name=None), exact) is marker
    assert captured == {
        "name": None,
        "thinking_enabled": False,
        "app_config": exact,
    }


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
    admitted_memory_config = MemoryConfig(
        enabled=True,
        model_name="admitted-memory-model",
        fact_confidence_threshold=0.7,
    )
    admitted_app_config = _app_config(memory=admitted_memory_config)
    monkeypatch.setattr(
        updater,
        "_get_model",
        lambda config, app_config: model if config is admitted_memory_config and app_config is admitted_app_config else (_ for _ in ()).throw(AssertionError("wrong memory config")),
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
        memory_config=admitted_memory_config,
        app_config=admitted_app_config,
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
        app_config=_app_config(memory=MemoryConfig(enabled=True)),
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
                "app_config": middleware._app_config,
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
    assert call["memory_config"] == middleware._memory_config
    assert call["app_config"] is middleware._app_config
    assert tuple(message.type for message in call["messages"]) == ("human", "ai")
    assert call["langfuse_trace_correlation_enabled"] is False


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
                    "app_config": _app_config(),
                }
            ),
        )
    )

    private_queue.enqueue.assert_not_called()
    private_queue.enqueue_immediate.assert_called_once()
    call = private_queue.enqueue_immediate.call_args.kwargs
    assert call["scope"] == scope
    assert call["thread_id"] == "thread-a"
    assert call["run_id"] == "run-a"
    assert call["namespace"] == "agent:researcher"
    assert type(call["app_config"]) is AppConfig
    assert call["langfuse_trace_correlation_enabled"] is False


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
