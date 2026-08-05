from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph.message import add_messages

import deerflow.agents.middlewares.dynamic_context_middleware as dynamic_context_module
from deerflow.agents.memory.manager import ProjectMemoryManager
from deerflow.agents.memory.storage import ProjectMemorySnapshot, create_empty_memory
from deerflow.agents.middlewares.dynamic_context_middleware import DynamicContextMiddleware
from deerflow.config.app_config import AppConfig
from deerflow.config.memory_config import MemoryConfig
from deerflow.private_scope import PrivateResourceScope
from deerflow.sandbox.sandbox import AuthorizationRevoked


@dataclass(frozen=True, slots=True)
class _V2Snapshot:
    id: str
    version: int
    facts: tuple[dict[str, Any], ...]
    rendered_content: str | None
    rendered_content_digest: str
    created_at: datetime


class _V2Authority:
    pipeline_mode = "v2"

    def __init__(self, *snapshots: _V2Snapshot | None) -> None:
        self._snapshots = snapshots
        self.calls = 0

    async def load_snapshot(self) -> _V2Snapshot | None:
        snapshot = self._snapshots[min(self.calls, len(self._snapshots) - 1)]
        self.calls += 1
        return snapshot


class _RevokedV2Authority:
    pipeline_mode = "v2"

    async def load_snapshot(self) -> None:
        raise AuthorizationRevoked


class _SlowV2Authority:
    pipeline_mode = "v2"

    def __init__(self) -> None:
        self.calls = 0

    async def load_snapshot(self) -> None:
        self.calls += 1
        await asyncio.sleep(60)


class _CancelledV2Authority:
    pipeline_mode = "v2"

    async def load_snapshot(self) -> None:
        raise asyncio.CancelledError


class _ConsolidateAuthority:
    pipeline_mode = "consolidate"

    async def load_snapshot(self) -> None:
        raise AssertionError("consolidate mode must not use the v2 recall authority")


class _AlwaysActive:
    async def is_active(self, scope: PrivateResourceScope) -> bool:
        return True


class _V1Storage:
    def __init__(self, snapshot: ProjectMemorySnapshot) -> None:
        self._snapshot = snapshot
        self.calls = 0

    async def load(self, **kwargs: Any) -> ProjectMemorySnapshot:
        self.calls += 1
        return self._snapshot


def _app_config(
    *,
    pipeline_mode: str,
    enabled: bool = True,
    injection_enabled: bool = True,
) -> AppConfig:
    return AppConfig(
        sandbox={"use": "deerflow.sandbox.local:LocalSandboxProvider"},
        memory=MemoryConfig(
            enabled=enabled,
            pipeline_mode=pipeline_mode,
            injection_enabled=injection_enabled,
            token_counting="char",
            staleness_review_enabled=False,
        ),
    )


def _scope() -> PrivateResourceScope:
    return PrivateResourceScope(
        project_id=str(uuid.uuid4()),
        owner_user_id=str(uuid.uuid4()),
        membership_version=1,
    )


def _snapshot(
    content: str | None,
    *,
    snapshot_id: str = "snapshot-1",
    version: int = 7,
    facts: tuple[dict[str, Any], ...] | None = None,
) -> _V2Snapshot:
    digest_source = content if content is not None else ""
    return _V2Snapshot(
        id=snapshot_id,
        version=version,
        facts=facts
        if facts is not None
        else (
            {
                "id": "fact-1",
                "content": "Use concise Chinese answers",
                "category": "preference",
                "confidence": 0.95,
                "createdAt": "2026-08-01T00:00:00Z",
            },
        ),
        rendered_content=content,
        rendered_content_digest=hashlib.sha256(digest_source.encode()).hexdigest(),
        created_at=datetime(2026, 8, 5, 8, 0, tzinfo=UTC),
    )


def _runtime(authority: object, *, scope: PrivateResourceScope | None = None) -> SimpleNamespace:
    context: dict[str, object] = {"__memory_authority": authority}
    if scope is not None:
        context["private_scope"] = scope
    return SimpleNamespace(context=context)


def _hidden_memory_messages(messages: list[object]) -> list[HumanMessage]:
    return [message for message in messages if isinstance(message, HumanMessage) and message.additional_kwargs.get("hide_from_ui") is True and message.additional_kwargs.get("project_memory_loaded") is True]


@pytest.mark.asyncio
async def test_project_memory_manager_accepts_virtual_version_zero() -> None:
    authority = _V2Authority(_snapshot(None, version=0, facts=()))

    response = await ProjectMemoryManager().asearch(
        authority=authority,
        query="anything",
        category=None,
        top_k=5,
    )

    assert response.snapshot_version == 0
    assert response.results == ()


@pytest.mark.asyncio
async def test_v2_before_model_loads_runtime_authority_and_injects_hidden_human_memory() -> None:
    memory_content = "<memory>\nUse concise Chinese answers\n</memory>"
    authority = _V2Authority(_snapshot(memory_content))
    middleware = DynamicContextMiddleware(agent_name="lead", app_config=_app_config(pipeline_mode="v2"))
    initial = [HumanMessage(content="Help me plan this task", id="turn-1")]

    update = await middleware.abefore_model(
        {"messages": initial},
        _runtime(authority),
    )

    assert update is not None
    merged = add_messages(initial, update["messages"])
    injected = _hidden_memory_messages(merged)
    assert len(injected) == 1
    assert injected[0].content == memory_content
    assert authority.calls == 1


@pytest.mark.asyncio
async def test_v2_same_run_reuses_one_memory_message() -> None:
    memory_content = "<memory>\nSame frozen Run snapshot\n</memory>"
    authority = _V2Authority(_snapshot(memory_content))
    middleware = DynamicContextMiddleware(agent_name="lead", app_config=_app_config(pipeline_mode="v2"))
    messages = [HumanMessage(content="first model boundary", id="turn-1")]

    first = await middleware.abefore_model({"messages": messages}, _runtime(authority))
    assert first is not None
    messages = add_messages(messages, first["messages"])
    second = await middleware.abefore_model({"messages": messages}, _runtime(authority))
    if second is not None:
        messages = add_messages(messages, second["messages"])

    injected = _hidden_memory_messages(messages)
    assert len(injected) == 1
    assert injected[0].content == memory_content


@pytest.mark.asyncio
async def test_v2_new_run_replaces_prior_memory_message_without_duplication() -> None:
    old_content = "<memory>\nOld Run context\n</memory>"
    new_content = "<memory>\nNew Run context\n</memory>"
    old_authority = _V2Authority(_snapshot(old_content, snapshot_id="snapshot-old", version=4))
    new_authority = _V2Authority(_snapshot(new_content, snapshot_id="snapshot-new", version=8))
    middleware = DynamicContextMiddleware(agent_name="lead", app_config=_app_config(pipeline_mode="v2"))
    messages = [HumanMessage(content="first Run", id="turn-1")]

    first = await middleware.abefore_model({"messages": messages}, _runtime(old_authority))
    assert first is not None
    messages = add_messages(messages, first["messages"])
    messages = add_messages(messages, [HumanMessage(content="second Run", id="turn-2")])
    second = await middleware.abefore_model({"messages": messages}, _runtime(new_authority))
    assert second is not None
    messages = add_messages(messages, second["messages"])

    injected = _hidden_memory_messages(messages)
    assert len(injected) == 1
    assert injected[0].content == new_content
    assert all(old_content not in str(message.content) for message in messages)


@pytest.mark.asyncio
async def test_v2_hard_forget_overlay_removes_stale_body_at_next_model_boundary() -> None:
    forgotten_content = "<memory>\nThe body that must be forgotten\n</memory>"
    authority = _V2Authority(
        _snapshot(forgotten_content),
        _snapshot(None),
    )
    middleware = DynamicContextMiddleware(agent_name="lead", app_config=_app_config(pipeline_mode="v2"))
    messages = [HumanMessage(content="first boundary", id="turn-1")]

    first = await middleware.abefore_model({"messages": messages}, _runtime(authority))
    assert first is not None
    messages = add_messages(messages, first["messages"])
    assert any(forgotten_content in str(message.content) for message in messages)

    overlay = await middleware.abefore_model({"messages": messages}, _runtime(authority))
    assert overlay is not None
    messages = add_messages(messages, overlay["messages"])

    assert all(forgotten_content not in str(message.content) for message in messages)


@pytest.mark.asyncio
async def test_v2_authorization_revoked_propagates_from_model_boundary() -> None:
    middleware = DynamicContextMiddleware(agent_name="lead", app_config=_app_config(pipeline_mode="v2"))

    with pytest.raises(AuthorizationRevoked):
        await middleware.abefore_model(
            {"messages": [HumanMessage(content="private", id="turn-1")]},
            _runtime(_RevokedV2Authority()),
        )


@pytest.mark.asyncio
async def test_v2_snapshot_timeout_degrades_to_empty_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dynamic_context_module, "_INJECT_TIMEOUT_SECONDS", 0.01)
    authority = _SlowV2Authority()
    middleware = DynamicContextMiddleware(
        agent_name="lead",
        app_config=_app_config(pipeline_mode="v2"),
    )

    update = await middleware.abefore_model(
        {"messages": [HumanMessage(content="continue", id="turn-1")]},
        _runtime(authority),
    )

    assert update is not None
    assert authority.calls == 1
    assert _hidden_memory_messages(update["messages"]) == []


@pytest.mark.asyncio
async def test_v2_snapshot_cancellation_propagates() -> None:
    middleware = DynamicContextMiddleware(
        agent_name="lead",
        app_config=_app_config(pipeline_mode="v2"),
    )

    with pytest.raises(asyncio.CancelledError):
        await middleware.abefore_model(
            {"messages": [HumanMessage(content="continue", id="turn-1")]},
            _runtime(_CancelledV2Authority()),
        )


@pytest.mark.asyncio
async def test_v2_injection_and_search_share_snapshot_version_and_cjk_lexical_facts() -> None:
    facts = (
        {
            "id": "fact-zh",
            "content": "用户使用中文交流",
            "category": "preference",
            "confidence": 0.95,
            "createdAt": "2026-08-01T00:00:00Z",
        },
        {
            "id": "fact-en",
            "content": "The user prefers concise conclusions",
            "category": "preference",
            "confidence": 0.9,
            "createdAt": "2026-08-02T00:00:00Z",
        },
    )
    snapshot = _snapshot(
        "<memory>\n用户使用中文交流\nThe user prefers concise conclusions\n</memory>",
        version=11,
        facts=facts,
    )
    authority = _V2Authority(snapshot)
    middleware = DynamicContextMiddleware(
        agent_name="lead",
        app_config=_app_config(pipeline_mode="v2"),
    )

    injected = await middleware.abefore_model(
        {"messages": [HumanMessage(content="继续", id="turn-1")]},
        _runtime(authority),
    )
    chinese = await ProjectMemoryManager().asearch(
        authority=authority,
        query="中文交流",
        category=None,
        top_k=5,
    )
    english = await ProjectMemoryManager().asearch(
        authority=authority,
        query="concise conclusions",
        category=None,
        top_k=5,
    )

    assert injected is not None
    assert chinese.snapshot_version == english.snapshot_version == 11
    assert [result["id"] for result in chinese.results] == ["fact-zh"]
    assert [result["id"] for result in english.results] == ["fact-en"]


@pytest.mark.asyncio
async def test_v2_injection_disabled_removes_prior_checkpoint_memory() -> None:
    content = "<memory>\nThis must not survive an injection-disabled Run\n</memory>"
    authority = _V2Authority(_snapshot(content))
    enabled = DynamicContextMiddleware(
        agent_name="lead",
        app_config=_app_config(pipeline_mode="v2"),
    )
    messages = [HumanMessage(content="first Run", id="turn-1")]
    first = await enabled.abefore_model({"messages": messages}, _runtime(authority))
    assert first is not None
    messages = add_messages(messages, first["messages"])

    disabled = DynamicContextMiddleware(
        agent_name="lead",
        app_config=_app_config(
            pipeline_mode="v2",
            injection_enabled=False,
        ),
    )
    update = await disabled.abefore_model({"messages": messages}, _runtime(authority))
    assert update is not None
    messages = add_messages(messages, update["messages"])

    assert all(content not in str(message.content) for message in messages)
    assert authority.calls == 1


@pytest.mark.asyncio
async def test_consolidate_mode_keeps_existing_v1_storage_injection() -> None:
    memory = create_empty_memory()
    memory["user"]["workContext"] = {
        "summary": "Continue using the v1 storage path",
        "updatedAt": "2026-08-05T00:00:00Z",
    }
    storage = _V1Storage(ProjectMemorySnapshot(memory=memory, version=3))
    middleware = DynamicContextMiddleware(
        agent_name="lead",
        app_config=_app_config(pipeline_mode="consolidate"),
        project_memory_storage=storage,
        project_memory_revalidator=_AlwaysActive(),
    )
    messages = [HumanMessage(content="keep the rollback path", id="turn-1")]
    runtime = _runtime(_ConsolidateAuthority(), scope=_scope())

    update = await middleware.abefore_agent({"messages": messages}, runtime)

    assert update is not None
    merged = add_messages(messages, update["messages"])
    injected = _hidden_memory_messages(merged)
    assert len(injected) == 1
    assert "Continue using the v1 storage path" in str(injected[0].content)
    assert storage.calls == 1
    assert await middleware.abefore_model({"messages": merged}, runtime) is None


@pytest.mark.asyncio
async def test_consolidate_rollback_replaces_prior_v2_checkpoint_memory_with_v1() -> None:
    v2_content = "<memory>\nOld v2 Run context\n</memory>"
    v2_authority = _V2Authority(_snapshot(v2_content))
    v2 = DynamicContextMiddleware(
        agent_name="lead",
        app_config=_app_config(pipeline_mode="v2"),
    )
    messages = [HumanMessage(content="v2 Run", id="turn-1")]
    first = await v2.abefore_model({"messages": messages}, _runtime(v2_authority))
    assert first is not None
    messages = add_messages(messages, first["messages"])
    messages = add_messages(messages, [HumanMessage(content="rollback Run", id="turn-2")])

    memory = create_empty_memory()
    memory["user"]["workContext"] = {
        "summary": "Rollback now reads the v1 aggregate",
        "updatedAt": "2026-08-05T00:00:00Z",
    }
    storage = _V1Storage(ProjectMemorySnapshot(memory=memory, version=4))
    v1 = DynamicContextMiddleware(
        agent_name="lead",
        app_config=_app_config(pipeline_mode="consolidate"),
        project_memory_storage=storage,
        project_memory_revalidator=_AlwaysActive(),
    )
    update = await v1.abefore_agent(
        {"messages": messages},
        _runtime(_ConsolidateAuthority(), scope=_scope()),
    )
    assert update is not None
    messages = add_messages(messages, update["messages"])

    injected = _hidden_memory_messages(messages)
    assert len(injected) == 1
    assert "Rollback now reads the v1 aggregate" in str(injected[0].content)
    assert all(v2_content not in str(message.content) for message in messages)
    assert storage.calls == 1

    historical_v2_date = SystemMessage(
        content="<system-reminder>\n<current_date>2026-08-04, Tuesday</current_date>\n</system-reminder>",
        id="historical-v2-date",
        additional_kwargs={
            "hide_from_ui": True,
            "dynamic_context_reminder": True,
            "reminder_date": "2026-08-04, Tuesday",
            "project_memory_loaded": True,
            "project_memory_mode": "v2",
        },
    )
    messages = add_messages(
        [historical_v2_date, *messages],
        [HumanMessage(content="another consolidate Run", id="turn-3")],
    )
    repeated = await v1.abefore_agent(
        {"messages": messages},
        _runtime(_ConsolidateAuthority(), scope=_scope()),
    )
    if repeated is not None:
        messages = add_messages(messages, repeated["messages"])

    assert len(_hidden_memory_messages(messages)) == 1
    assert storage.calls == 1
