from __future__ import annotations

import asyncio
import copy
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph.message import add_messages
from pydantic import ValidationError

from app.gateway.routers.project_memory import (
    ProjectMemoryImportRequest,
    ProjectMemoryResponse,
    reload_project_memory,
)
from deerflow.agents.memory.queue import ProjectMemoryUpdateQueue
from deerflow.agents.memory.storage import (
    ProjectMemorySnapshot,
    ProjectMemoryStorage,
    create_empty_memory,
)
from deerflow.agents.memory.updater import MemoryUpdater
from deerflow.agents.middlewares.dynamic_context_middleware import (
    DynamicContextMiddleware,
)
from deerflow.config.app_config import AppConfig
from deerflow.config.memory_config import MemoryConfig
from deerflow.persistence.private_work.memory_repository import (
    PrivateMemoryRecord,
    PrivateMemoryVersionConflict,
)
from deerflow.private_scope import PrivateResourceScope


def _scope() -> PrivateResourceScope:
    return PrivateResourceScope(
        project_id=str(uuid.uuid4()),
        owner_user_id=str(uuid.uuid4()),
        membership_version=1,
    )


def _app_config() -> AppConfig:
    return AppConfig(
        sandbox={"use": "deerflow.sandbox.local:LocalSandboxProvider"},
        memory=MemoryConfig(token_counting="char", staleness_review_enabled=False),
    )


class _AlwaysActive:
    async def is_active(self, scope: PrivateResourceScope) -> bool:
        return True


class _BlockingUpdater:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.calls: list[str] = []
        self.cancelled: list[str] = []
        self.active = 0
        self.peak = 0

    async def aupdate_project_memory(self, **kwargs) -> bool:
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.calls.append(kwargs["thread_id"])
        self.started.set()
        try:
            await self.release.wait()
            return True
        except asyncio.CancelledError:
            self.cancelled.append(kwargs["thread_id"])
            raise
        finally:
            self.active -= 1


@pytest.mark.asyncio
async def test_queue_serializes_same_memory_across_threads_without_dropping_items() -> None:
    updater = _BlockingUpdater()
    queue = ProjectMemoryUpdateQueue(
        object(),
        updater=updater,
        revalidator=_AlwaysActive(),
        debounce_seconds=0,
    )
    scope = _scope()
    memory_config = MemoryConfig(token_counting="char")
    app_config = _app_config()
    try:
        for thread_id in ("thread-a", "thread-b"):
            queue.enqueue(
                scope=scope,
                thread_id=thread_id,
                run_id=f"run-{thread_id}",
                namespace="agent:lead",
                messages=[HumanMessage(content=thread_id)],
                memory_config=memory_config,
                app_config=app_config,
            )

        assert queue.pending_count == 2
        await asyncio.wait_for(updater.started.wait(), timeout=1)
        await asyncio.sleep(0.05)
        updater.release.set()
        await asyncio.wait_for(queue.flush_all(), timeout=1)

        assert sorted(updater.calls) == ["thread-a", "thread-b"]
        assert updater.peak == 1
    finally:
        updater.release.set()
        await queue.clear()


@pytest.mark.asyncio
async def test_cancelling_waiting_flush_does_not_cancel_active_memory_update() -> None:
    updater = _BlockingUpdater()
    queue = ProjectMemoryUpdateQueue(
        object(),
        updater=updater,
        revalidator=_AlwaysActive(),
        debounce_seconds=3600,
    )
    scope = _scope()
    common = {
        "scope": scope,
        "namespace": "agent:lead",
        "memory_config": MemoryConfig(token_counting="char"),
        "app_config": _app_config(),
    }
    first_flush = None
    waiting_flush = None
    try:
        first = queue.enqueue(
            **common,
            thread_id="thread-a",
            run_id="run-a",
            messages=[HumanMessage(content="a")],
        )
        first_flush = asyncio.create_task(queue.flush(first.key))
        await asyncio.wait_for(updater.started.wait(), timeout=1)

        second = queue.enqueue(
            **common,
            thread_id="thread-b",
            run_id="run-b",
            messages=[HumanMessage(content="b")],
        )
        waiting_flush = asyncio.create_task(queue.flush(second.key))
        await asyncio.sleep(0.05)
        waiting_flush.cancel()
        await asyncio.gather(waiting_flush, return_exceptions=True)

        assert updater.cancelled == []
        assert queue.pending_count == 1

        updater.release.set()
        assert await asyncio.wait_for(first_flush, timeout=1) is True
        assert await asyncio.wait_for(queue.flush(second.key), timeout=1) is True
        assert updater.calls == ["thread-a", "thread-b"]
        assert updater.peak == 1
    finally:
        updater.release.set()
        for task in (first_flush, waiting_flush):
            if task is not None and not task.done():
                task.cancel()
        await queue.clear()


class _CasStorage:
    def __init__(self, snapshots: list[ProjectMemorySnapshot], *, always_conflict: bool = False) -> None:
        self._snapshots = snapshots
        self.always_conflict = always_conflict
        self.load_count = 0
        self.expected_versions: list[int] = []

    async def load(self, **kwargs) -> ProjectMemorySnapshot:
        snapshot = self._snapshots[min(self.load_count, len(self._snapshots) - 1)]
        self.load_count += 1
        return snapshot

    async def save(self, memory_data, **kwargs) -> ProjectMemorySnapshot:
        self.expected_versions.append(kwargs["expected_version"])
        if self.always_conflict or len(self.expected_versions) == 1:
            raise PrivateMemoryVersionConflict("stale")
        return ProjectMemorySnapshot(memory=copy.deepcopy(memory_data), version=3)


class _RecordingModel:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def invoke(self, prompt: str, *, config) -> SimpleNamespace:
        self.prompts.append(prompt)
        return SimpleNamespace(
            content=json.dumps(
                {
                    "user": {
                        "workContext": {
                            "shouldUpdate": True,
                            "summary": "merged result",
                        }
                    },
                    "history": {},
                    "newFacts": [],
                    "factsToRemove": [],
                }
            )
        )


def _memory_with_summary(summary: str) -> dict:
    memory = create_empty_memory()
    memory["user"]["workContext"] = {
        "summary": summary,
        "updatedAt": "2026-01-01T00:00:00Z",
    }
    return memory


@pytest.mark.asyncio
async def test_updater_reloads_and_recomputes_after_version_conflict(monkeypatch) -> None:
    storage = _CasStorage(
        [
            ProjectMemorySnapshot(_memory_with_summary("initial-memory"), 1),
            ProjectMemorySnapshot(_memory_with_summary("winner-memory"), 2),
        ]
    )
    model = _RecordingModel()
    updater = MemoryUpdater()
    monkeypatch.setattr(updater, "_get_model", lambda memory_config, app_config: model)

    updated = await updater.aupdate_project_memory(
        storage=storage,
        scope=_scope(),
        namespace="agent:lead",
        messages=[HumanMessage(content="Remember the merged result")],
        thread_id="thread-a",
        run_id="run-a",
        memory_config=MemoryConfig(token_counting="char", staleness_review_enabled=False),
        app_config=_app_config(),
    )

    assert updated is True
    assert storage.expected_versions == [1, 2]
    assert len(model.prompts) == 2
    assert "winner-memory" in model.prompts[1]


@pytest.mark.asyncio
async def test_updater_stops_after_three_version_conflict_retries(monkeypatch) -> None:
    storage = _CasStorage(
        [ProjectMemorySnapshot(_memory_with_summary("current"), 1)],
        always_conflict=True,
    )
    model = _RecordingModel()
    updater = MemoryUpdater()
    monkeypatch.setattr(updater, "_get_model", lambda memory_config, app_config: model)

    updated = await updater.aupdate_project_memory(
        storage=storage,
        scope=_scope(),
        namespace="agent:lead",
        messages=[HumanMessage(content="Remember this")],
        thread_id="thread-a",
        run_id="run-a",
        memory_config=MemoryConfig(token_counting="char", staleness_review_enabled=False),
        app_config=_app_config(),
    )

    assert updated is False
    assert storage.load_count == 4
    assert len(model.prompts) == 4
    assert storage.expected_versions == [1, 1, 1, 1]


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Session(_Transaction):
    def begin(self) -> _Transaction:
        return _Transaction()


class _SessionFactory:
    def __call__(self) -> _Session:
        return _Session()


@pytest.mark.asyncio
async def test_storage_missing_load_is_virtual_and_does_not_create(monkeypatch) -> None:
    class FakeRepository:
        def __init__(self, session) -> None:
            pass

        async def load(self, **kwargs):
            return None

        async def create_if_needed(self, **kwargs):
            raise AssertionError("read path must not create memory")

    monkeypatch.setattr(
        "deerflow.agents.memory.storage.PrivateMemoryRepository",
        FakeRepository,
    )
    snapshot = await ProjectMemoryStorage(_SessionFactory()).load(
        scope=_scope(),
        namespace="agent:lead",
    )

    assert snapshot.version == 0
    assert snapshot.memory["lastUpdated"] == ""
    assert snapshot.memory["facts"] == []


def test_storage_snapshot_uses_database_updated_time() -> None:
    database_time = datetime(2026, 7, 8, 9, 10, 11, tzinfo=UTC)
    record = PrivateMemoryRecord(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        owner_user_id=str(uuid.uuid4()),
        namespace="agent:lead",
        context_summary={
            **create_empty_memory(),
            "lastUpdated": "1999-01-01T00:00:00Z",
        },
        version=7,
        created_at=database_time,
        updated_at=database_time,
        facts=(),
    )

    snapshot = ProjectMemoryStorage._snapshot(record)

    assert snapshot.memory["lastUpdated"] == "2026-07-08T09:10:11Z"


class _SequenceStorage:
    def __init__(self, results: list[object]) -> None:
        self.results = results
        self.calls = 0

    async def load(self, **kwargs) -> ProjectMemorySnapshot:
        result = self.results[self.calls]
        self.calls += 1
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, ProjectMemorySnapshot)
        return result


@pytest.mark.asyncio
async def test_private_memory_injection_retries_after_date_only_failure() -> None:
    memory = _memory_with_summary("Use concise Chinese answers")
    storage = _SequenceStorage([RuntimeError("temporary failure"), ProjectMemorySnapshot(memory, 1)])
    middleware = DynamicContextMiddleware(
        agent_name="lead",
        app_config=_app_config(),
        project_memory_storage=storage,
        project_memory_revalidator=_AlwaysActive(),
    )

    first = await middleware._inject_private(
        {"messages": [HumanMessage(content="first", id="m1")]},
        _scope(),
    )
    assert first is not None
    assert any(isinstance(message, SystemMessage) for message in first["messages"])
    assert not any(message.additional_kwargs.get("project_memory_loaded") for message in first["messages"])

    second = await middleware._inject_private(
        {
            "messages": [
                *first["messages"],
                HumanMessage(content="second", id="m2"),
            ]
        },
        _scope(),
    )
    assert second is not None
    assert not any(isinstance(message, SystemMessage) for message in second["messages"])
    assert any(isinstance(message, HumanMessage) and message.additional_kwargs.get("project_memory_loaded") is True and "<memory>" in str(message.content) for message in second["messages"])

    third = await middleware._inject_private(
        {
            "messages": [
                *first["messages"],
                *second["messages"],
                HumanMessage(content="third", id="m3"),
            ]
        },
        _scope(),
    )
    assert third is None
    assert storage.calls == 2


@pytest.mark.asyncio
async def test_successful_empty_memory_load_marks_injection_complete() -> None:
    storage = _SequenceStorage([ProjectMemorySnapshot(create_empty_memory(), 0)])
    middleware = DynamicContextMiddleware(
        agent_name="lead",
        app_config=_app_config(),
        project_memory_storage=storage,
        project_memory_revalidator=_AlwaysActive(),
    )

    first = await middleware._inject_private(
        {"messages": [HumanMessage(content="first", id="m1")]},
        _scope(),
    )
    assert first is not None
    assert any(message.additional_kwargs.get("project_memory_loaded") is True for message in first["messages"])

    second = await middleware._inject_private(
        {
            "messages": [
                *first["messages"],
                HumanMessage(content="second", id="m2"),
            ]
        },
        _scope(),
    )
    assert second is None
    assert storage.calls == 1


@pytest.mark.asyncio
async def test_legacy_date_reminder_can_record_successful_empty_memory_load() -> None:
    storage = _SequenceStorage([ProjectMemorySnapshot(create_empty_memory(), 0)])
    middleware = DynamicContextMiddleware(
        agent_name="lead",
        app_config=_app_config(),
        project_memory_storage=storage,
        project_memory_revalidator=_AlwaysActive(),
    )
    legacy_date = SystemMessage(
        content=middleware._build_date_update_reminder(),
        id="legacy-date",
        additional_kwargs={
            "hide_from_ui": True,
            "dynamic_context_reminder": True,
        },
    )
    initial_messages = [
        legacy_date,
        HumanMessage(content="current turn", id="current-turn"),
    ]

    first = await middleware._inject_private(
        {"messages": initial_messages},
        _scope(),
    )
    assert first is not None
    merged = add_messages(initial_messages, first["messages"])
    assert [message.id for message in merged].count("legacy-date") == 1
    assert next(message for message in merged if message.id == "legacy-date").additional_kwargs["project_memory_loaded"] is True

    second = await middleware._inject_private(
        {
            "messages": add_messages(
                merged,
                [HumanMessage(content="next turn", id="next-turn")],
            )
        },
        _scope(),
    )
    assert second is None
    assert storage.calls == 1


def _valid_memory_document() -> dict:
    fact_id = str(uuid.uuid4())
    summary = {"summary": "", "updatedAt": ""}
    return {
        "version": "1.0",
        "lastUpdated": "",
        "user": {
            "workContext": dict(summary),
            "personalContext": dict(summary),
            "topOfMind": dict(summary),
        },
        "history": {
            "recentMonths": dict(summary),
            "earlierContext": dict(summary),
            "longTermBackground": dict(summary),
        },
        "facts": [
            {
                "id": fact_id,
                "content": "PostgreSQL is preferred",
                "category": "preference",
                "confidence": 0.9,
                "createdAt": "2026-01-01T00:00:00Z",
                "source": "manual",
            }
        ],
    }


def test_memory_import_and_response_use_strict_nested_schema_and_limits() -> None:
    valid = _valid_memory_document()
    request = ProjectMemoryImportRequest.model_validate({"expected_version": 0, "memory": valid})
    ProjectMemoryResponse.model_validate({"namespace": "default", "version": 0, "memory": valid})
    assert request.expected_version == 0

    nested_extra = copy.deepcopy(valid)
    nested_extra["user"]["workContext"]["unexpected"] = True
    with pytest.raises(ValidationError):
        ProjectMemoryImportRequest.model_validate({"expected_version": 0, "memory": nested_extra})

    too_many = copy.deepcopy(valid)
    too_many["facts"] = [
        {
            **copy.deepcopy(valid["facts"][0]),
            "id": str(uuid.uuid4()),
        }
        for _ in range(501)
    ]
    with pytest.raises(ValidationError):
        ProjectMemoryImportRequest.model_validate({"expected_version": 0, "memory": too_many})

    long_source = copy.deepcopy(valid)
    long_source["facts"][0]["sourceThreadId"] = "t" * 65
    with pytest.raises(ValidationError):
        ProjectMemoryImportRequest.model_validate({"expected_version": 0, "memory": long_source})

    duplicate_ids = copy.deepcopy(valid)
    duplicate_ids["facts"].append(copy.deepcopy(duplicate_ids["facts"][0]))
    with pytest.raises(ValidationError):
        ProjectMemoryImportRequest.model_validate({"expected_version": 0, "memory": duplicate_ids})

    snake_case = copy.deepcopy(valid)
    snake_case["last_updated"] = snake_case.pop("lastUpdated")
    with pytest.raises(ValidationError):
        ProjectMemoryImportRequest.model_validate({"expected_version": 0, "memory": snake_case})

    for invalid_version in ("0", True):
        with pytest.raises(ValidationError):
            ProjectMemoryImportRequest.model_validate({"expected_version": invalid_version, "memory": valid})


@pytest.mark.asyncio
async def test_reload_endpoint_explicitly_reports_unsupported() -> None:
    with pytest.raises(HTTPException) as raised:
        await reload_project_memory(
            request=object(),
            namespace="default",
            context=object(),
        )
    assert raised.value.status_code == 501
