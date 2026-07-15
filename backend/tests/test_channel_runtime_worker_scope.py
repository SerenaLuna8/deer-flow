"""Real-worker regression for channel runtime/storage identity isolation."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, TypedDict

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.store.memory import InMemoryStore

from app.gateway.auth_disabled import AUTH_SOURCE_INTERNAL
from app.gateway.internal_auth import create_internal_auth_headers, get_internal_user
from app.gateway.routers.thread_runs import RunCreateRequest
from app.gateway.services import start_run
from deerflow.config.app_config import AppConfig, reset_app_config, set_app_config
from deerflow.persistence.thread_meta.memory import MemoryThreadMetaStore
from deerflow.runtime import RunManager, RunStatus
from deerflow.runtime.events.store.memory import MemoryRunEventStore
from deerflow.runtime.runs.store.memory import MemoryRunStore
from deerflow.runtime.user_context import AUTO, DEFAULT_USER_ID, get_current_user, get_effective_user_id, reset_current_user, resolve_user_id, set_current_user

pytestmark = pytest.mark.no_auto_user


class _WorkerState(TypedDict, total=False):
    messages: list[Any]
    title: str


class _CapturingBridge:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    async def publish(self, _run_id: str, event: str, payload: Any) -> None:
        self.events.append((event, payload))

    async def publish_end(self, _run_id: str) -> None:
        self.events.append(("end", None))

    async def cleanup(self, _run_id: str, *, delay: int = 0) -> None:
        del delay


class _RepositoryCapturingRunStore(MemoryRunStore):
    def __init__(self) -> None:
        super().__init__()
        self.created_owners: list[str | None] = []

    async def put(self, run_id: str, **kwargs: Any) -> None:
        if "user_id" not in kwargs:
            kwargs["user_id"] = resolve_user_id(AUTO, method_name="capturing run store")
        self.created_owners.append(kwargs["user_id"])
        await super().put(run_id, **kwargs)


class _RepositoryCapturingEventStore(MemoryRunEventStore):
    def __init__(self) -> None:
        super().__init__()
        self.write_owners: list[str | None] = []

    async def put(self, **kwargs: Any) -> dict:
        current = get_current_user()
        self.write_owners.append(str(current.id) if current is not None else None)
        return await super().put(**kwargs)

    async def put_batch(self, events: list[dict]) -> list[dict]:
        current = get_current_user()
        self.write_owners.append(str(current.id) if current is not None else None)
        return await super().put_batch(events)


class _RealWorkerHarness:
    def __init__(self) -> None:
        from support.m4_private_threads import OpenProjectCutoverGuard

        self.bridge = _CapturingBridge()
        self.run_store = _RepositoryCapturingRunStore()
        self.run_manager = RunManager(store=self.run_store)
        self.checkpointer = InMemorySaver()
        self.store = InMemoryStore()
        self.event_store = _RepositoryCapturingEventStore()
        self.thread_store = MemoryThreadMetaStore(self.store)
        self.runtime_observations: dict[str, dict[str, str | None]] = {}
        self.app = SimpleNamespace(
            state=SimpleNamespace(
                stream_bridge=self.bridge,
                run_manager=self.run_manager,
                checkpointer=self.checkpointer,
                store=self.store,
                run_event_store=self.event_store,
                run_events_config=None,
                thread_store=self.thread_store,
                private_work_cutover_guard=OpenProjectCutoverGuard(),
            )
        )

    def agent_factory(self, *, config: dict[str, Any]):
        thread_id = str(config["configurable"]["thread_id"])

        async def observe_runtime(_state: _WorkerState) -> _WorkerState:
            repository_user = get_current_user()
            self.runtime_observations[thread_id] = {
                "effective_user_id": get_effective_user_id(),
                "repository_user_id": str(repository_user.id) if repository_user is not None else None,
            }
            return {
                "messages": [AIMessage(content=f"completed {thread_id}")],
                "title": f"title {thread_id}",
            }

        builder = StateGraph(_WorkerState)
        builder.add_node("observe_runtime", observe_runtime)
        builder.add_edge(START, "observe_runtime")
        builder.add_edge("observe_runtime", END)
        return builder.compile()


@pytest.fixture
def real_worker_harness(monkeypatch: pytest.MonkeyPatch):
    from deerflow.config.tracing_config import reset_tracing_config

    set_app_config(AppConfig.model_validate({"sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"}}))
    monkeypatch.setenv("LANGFUSE_TRACING", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-runtime-scope-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-runtime-scope-test")
    reset_tracing_config()

    harness = _RealWorkerHarness()

    class _Provider:
        async def get_user(self, user_id: str):
            return SimpleNamespace(
                id=user_id,
                system_role="user",
                oauth_provider=None,
                oauth_id=None,
            )

    monkeypatch.setattr("app.gateway.services.resolve_agent_factory", lambda _assistant_id: harness.agent_factory)
    monkeypatch.setattr("app.gateway.services.get_local_provider", lambda: _Provider())
    yield harness

    reset_tracing_config()
    reset_app_config()


async def _run_internal_channel_worker(
    harness: _RealWorkerHarness,
    *,
    thread_id: str,
    runtime_user_id: str,
    owner_user_id: str | None = None,
) -> Any:
    internal_user = get_internal_user(owner_user_id=owner_user_id)
    token = set_current_user(internal_user)
    try:
        await harness.thread_store.create(
            thread_id,
            assistant_id="lead_agent",
            metadata={},
            user_id=owner_user_id or DEFAULT_USER_ID,
        )
        request = SimpleNamespace(
            headers=create_internal_auth_headers(
                owner_user_id=owner_user_id,
                runtime_user_id=runtime_user_id,
            ),
            state=SimpleNamespace(user=internal_user, auth_source=AUTH_SOURCE_INTERNAL),
            app=harness.app,
        )
        body = RunCreateRequest(
            assistant_id="lead_agent",
            input={"messages": [{"role": "human", "content": "hi"}]},
            stream_mode=["values"],
        )
        record = await start_run(body, thread_id, request)
    finally:
        reset_current_user(token)

    assert record.task is not None
    await record.task
    return record


@pytest.mark.asyncio
async def test_real_worker_keeps_runtime_storage_separate_from_repository_and_checkpoint_scope(
    real_worker_harness: _RealWorkerHarness,
) -> None:
    harness = real_worker_harness
    runtime_by_thread = {
        "thread-runtime-a": "platform-runtime-a",
        "thread-runtime-b": "platform-runtime-b",
    }

    records = []
    for thread_id, runtime_user_id in runtime_by_thread.items():
        records.append(
            await _run_internal_channel_worker(
                harness,
                thread_id=thread_id,
                runtime_user_id=runtime_user_id,
            )
        )

    assert {thread_id: observation["effective_user_id"] for thread_id, observation in harness.runtime_observations.items()} == runtime_by_thread

    persisted_scope: dict[str, dict[str, Any]] = {}
    for record in records:
        runtime_user_id = runtime_by_thread[record.thread_id]
        persisted_run = await harness.run_store.get(record.run_id)
        assert persisted_run is not None
        assert runtime_user_id not in str(persisted_run["kwargs"])
        assert runtime_user_id not in str(persisted_run["metadata"])

        thread = await harness.thread_store.get(record.thread_id, user_id=None)
        assert thread is not None

        checkpoint = await harness.checkpointer.aget_tuple({"configurable": {"thread_id": record.thread_id, "checkpoint_ns": ""}})
        assert checkpoint is not None
        assert runtime_user_id not in str(checkpoint.config.get("configurable", {}))
        persisted_scope[record.thread_id] = {
            "run_owner": persisted_run["user_id"],
            "thread_owner": thread["user_id"],
            "thread_title": thread["display_name"],
            "thread_status": thread["status"],
            "run_status": record.status,
            "checkpoint_langfuse_user_id": checkpoint.metadata.get("langfuse_user_id"),
            "runtime_in_checkpoint_metadata": runtime_user_id in str(checkpoint.metadata),
        }

    assert {
        "repository_users": {thread_id: observation["repository_user_id"] for thread_id, observation in harness.runtime_observations.items()},
        "run_create_owners": harness.run_store.created_owners,
        "event_write_owners": set(harness.event_store.write_owners),
        "persisted_scope": persisted_scope,
    } == {
        "repository_users": {thread_id: DEFAULT_USER_ID for thread_id in runtime_by_thread},
        "run_create_owners": [DEFAULT_USER_ID, DEFAULT_USER_ID],
        "event_write_owners": {DEFAULT_USER_ID},
        "persisted_scope": {
            thread_id: {
                "run_owner": DEFAULT_USER_ID,
                "thread_owner": DEFAULT_USER_ID,
                "thread_title": f"title {thread_id}",
                "thread_status": "idle",
                "run_status": RunStatus.success,
                "checkpoint_langfuse_user_id": DEFAULT_USER_ID,
                "runtime_in_checkpoint_metadata": False,
            }
            for thread_id in runtime_by_thread
        },
    }

    assert get_current_user() is None
    assert get_effective_user_id() == DEFAULT_USER_ID


@pytest.mark.asyncio
async def test_real_worker_preserves_bound_owner_for_repository_and_runtime_storage(
    real_worker_harness: _RealWorkerHarness,
) -> None:
    harness = real_worker_harness
    owner_user_id = "bound-owner"
    record = await _run_internal_channel_worker(
        harness,
        thread_id="thread-bound-owner",
        runtime_user_id=owner_user_id,
        owner_user_id=owner_user_id,
    )

    assert harness.runtime_observations[record.thread_id] == {
        "effective_user_id": owner_user_id,
        "repository_user_id": owner_user_id,
    }
    assert harness.run_store.created_owners == [owner_user_id]
    assert set(harness.event_store.write_owners) == {owner_user_id}

    persisted_run = await harness.run_store.get(record.run_id)
    assert persisted_run is not None
    assert persisted_run["user_id"] == owner_user_id
    thread = await harness.thread_store.get(record.thread_id, user_id=None)
    assert thread is not None
    assert thread["user_id"] == owner_user_id
    assert thread["display_name"] == f"title {record.thread_id}"
    assert thread["status"] == "idle"

    checkpoint = await harness.checkpointer.aget_tuple({"configurable": {"thread_id": record.thread_id, "checkpoint_ns": ""}})
    assert checkpoint is not None
    assert checkpoint.metadata.get("langfuse_user_id") == owner_user_id
