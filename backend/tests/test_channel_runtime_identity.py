"""Cross-layer contracts for trusted IM-channel runtime identity."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from app.channels.manager import ChannelManager, _channel_storage_user_id
from app.channels.message_bus import InboundMessage, MessageBus
from app.channels.run_policy import CHANNEL_RUN_POLICY, ChannelRunPolicy
from app.channels.store import ChannelStore
from app.gateway.auth_disabled import AUTH_SOURCE_INTERNAL, AUTH_SOURCE_SESSION
from app.gateway.internal_auth import (
    INTERNAL_AUTH_HEADER_NAME,
    INTERNAL_OWNER_USER_ID_HEADER_NAME,
    create_internal_auth_headers,
    get_internal_user,
)
from app.gateway.routers.thread_runs import RunCreateRequest
from app.gateway.services import start_run
from deerflow.config.app_config import AppConfig, reset_app_config, set_app_config
from deerflow.persistence.thread_meta.memory import MemoryThreadMetaStore
from deerflow.runtime import RunManager, RunStatus
from deerflow.runtime.runs.store import MemoryRunStore
from deerflow.runtime.user_context import DEFAULT_USER_ID, get_effective_user_id, reset_current_user, set_current_user

RUNTIME_USER_ID_HEADER_NAME = "X-DeerFlow-Runtime-User-Id"


@pytest.fixture(autouse=True)
def _stub_app_config(monkeypatch):
    from support.m4_private_threads import OpenProjectCutoverGuard

    set_app_config(AppConfig.model_validate({"sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"}}))
    monkeypatch.setattr(
        "app.gateway.deps.get_private_work_cutover_guard",
        lambda _request: OpenProjectCutoverGuard(),
    )
    yield
    reset_app_config()


class _ForwardingThreads:
    def __init__(self, client: _ForwardingGatewayClient) -> None:
        self._client = client

    async def create(self, **kwargs: Any) -> dict[str, str]:
        headers = dict(kwargs.get("headers") or {})
        self._client.thread_calls.append({"operation": "create", "headers": headers})
        thread_id = kwargs.get("thread_id") or f"thread-{len(self._client.thread_calls)}"
        owner_user_id = headers.get(INTERNAL_OWNER_USER_ID_HEADER_NAME) or DEFAULT_USER_ID
        await self._client.thread_store.create(
            thread_id,
            user_id=owner_user_id,
            assistant_id=kwargs.get("assistant_id"),
            metadata=kwargs.get("metadata") or {},
        )
        return {"thread_id": thread_id}

    async def update(self, thread_id: str, **kwargs: Any) -> dict[str, str]:
        self._client.thread_calls.append(
            {
                "operation": "update",
                "thread_id": thread_id,
                "headers": dict(kwargs.get("headers") or {}),
            }
        )
        return {"thread_id": thread_id}


class _ForwardingRuns:
    def __init__(self, client: _ForwardingGatewayClient) -> None:
        self._client = client

    async def _invoke(self, operation: str, thread_id: str, assistant_id: str | None, kwargs: dict[str, Any]) -> dict[str, Any]:
        per_call_headers = dict(kwargs.pop("headers", {}) or {})
        self._client.run_calls.append(
            {
                "operation": operation,
                "headers": per_call_headers,
                "thread_id": thread_id,
            }
        )
        # ChannelManager's real SDK client supplies the internal token as a
        # client-level default. Per-call run headers are layered on top here.
        headers = create_internal_auth_headers()
        headers.update(per_call_headers)
        owner_user_id = headers.get(INTERNAL_OWNER_USER_ID_HEADER_NAME)
        internal_user = get_internal_user(owner_user_id=owner_user_id)
        request = SimpleNamespace(
            headers=headers,
            state=SimpleNamespace(
                user=internal_user,
                auth_source=AUTH_SOURCE_INTERNAL,
            ),
            app=self._client.app,
        )
        body = RunCreateRequest(assistant_id=assistant_id, **kwargs)
        token = set_current_user(internal_user)
        try:
            record = await start_run(body, thread_id, request)
            self._client.records.append(record)
            assert record.task is not None
            await record.task
        finally:
            reset_current_user(token)
        return {
            "messages": [
                {"type": "human", "content": "hi"},
                {"type": "ai", "content": "ok"},
            ],
            "artifacts": [],
        }

    async def wait(self, thread_id: str, assistant_id: str | None, **kwargs: Any) -> dict[str, Any]:
        return await self._invoke("wait", thread_id, assistant_id, dict(kwargs))

    async def create(self, thread_id: str, assistant_id: str | None, **kwargs: Any) -> dict[str, Any]:
        return await self._invoke("create", thread_id, assistant_id, dict(kwargs))

    def stream(self, thread_id: str, assistant_id: str | None, **kwargs: Any):
        async def _events():
            result = await self._invoke("stream", thread_id, assistant_id, dict(kwargs))
            yield SimpleNamespace(event="values", data=result)

        return _events()


class _ForwardingGatewayClient:
    def __init__(self) -> None:
        self.thread_store = MemoryThreadMetaStore(InMemoryStore())
        self.run_manager = RunManager(store=MemoryRunStore())
        state = SimpleNamespace(
            stream_bridge=SimpleNamespace(),
            run_manager=self.run_manager,
            checkpointer=InMemorySaver(),
            store=InMemoryStore(),
            run_event_store=SimpleNamespace(),
            run_events_config=None,
            thread_store=self.thread_store,
        )
        self.app = SimpleNamespace(state=state)
        self.run_calls: list[dict[str, Any]] = []
        self.thread_calls: list[dict[str, Any]] = []
        self.records: list[Any] = []
        self.runtime_captures: list[dict[str, Any]] = []
        self.threads = _ForwardingThreads(self)
        self.runs = _ForwardingRuns(self)


@pytest.fixture
def forwarding_gateway(monkeypatch) -> _ForwardingGatewayClient:
    client = _ForwardingGatewayClient()

    async def fake_run_agent(_bridge, run_manager, record, **kwargs):
        client.runtime_captures.append(
            {
                "thread_id": record.thread_id,
                "config": kwargs["config"],
                "effective_user_id": get_effective_user_id(),
            }
        )
        await run_manager.set_status(record.run_id, RunStatus.success)

    class _Provider:
        async def get_user(self, user_id: str):
            return SimpleNamespace(
                id=user_id,
                system_role="user",
                oauth_provider=None,
                oauth_id=None,
            )

    monkeypatch.setattr("app.gateway.services.resolve_agent_factory", lambda _assistant_id: object())
    monkeypatch.setattr("app.gateway.services.run_agent", fake_run_agent)
    monkeypatch.setattr("app.gateway.services.get_local_provider", lambda: _Provider())
    monkeypatch.setattr("app.channels.manager._auth_disabled_owner_user_id", lambda: None)
    return client


def _manager(tmp_path, client: _ForwardingGatewayClient) -> ChannelManager:
    manager = ChannelManager(
        bus=MessageBus(),
        store=ChannelStore(path=tmp_path / "channels.json"),
    )
    manager._client = client
    return manager


def test_unbound_channel_runtime_identity_crosses_gateway_and_background_task(tmp_path, forwarding_gateway):
    async def scenario():
        manager = _manager(tmp_path, forwarding_gateway)
        msg = InboundMessage(
            channel_name="slack",
            chat_id="chat-a",
            user_id="platform-user-a",
            text="hi",
        )

        await manager._handle_chat(msg)

        expected_user_id = _channel_storage_user_id(msg)
        assert expected_user_id is not None
        assert forwarding_gateway.run_calls[0]["headers"][RUNTIME_USER_ID_HEADER_NAME] == expected_user_id
        capture = forwarding_gateway.runtime_captures[0]
        assert capture["config"]["context"]["user_id"] == expected_user_id
        assert capture["effective_user_id"] == expected_user_id

    asyncio.run(scenario())


def test_unbound_platform_users_never_converge_on_default(tmp_path, forwarding_gateway):
    async def scenario():
        manager = _manager(tmp_path, forwarding_gateway)
        first = InboundMessage(channel_name="slack", chat_id="chat-a", user_id="platform-a", text="hi")
        second = InboundMessage(channel_name="slack", chat_id="chat-b", user_id="platform-b", text="hi")

        await manager._handle_chat(first)
        await manager._handle_chat(second)

        effective_ids = [capture["effective_user_id"] for capture in forwarding_gateway.runtime_captures]
        assert effective_ids == [_channel_storage_user_id(first), _channel_storage_user_id(second)]
        assert len(set(effective_ids)) == 2
        assert DEFAULT_USER_ID not in effective_ids

    asyncio.run(scenario())


def test_runtime_identity_never_becomes_run_owner_thread_owner_or_private_authority(tmp_path, forwarding_gateway):
    async def scenario():
        manager = _manager(tmp_path, forwarding_gateway)
        msg = InboundMessage(channel_name="slack", chat_id="chat-a", user_id="platform-a", text="hi")

        await manager._handle_chat(
            msg,
            extra_context={
                "project_id": "attacker-project",
                "owner_user_id": "attacker-owner",
                "project_context": {"role": "admin"},
                "__private_scope": {"capabilities": ["shared_assets.execute"]},
            },
        )

        record = forwarding_gateway.records[0]
        assert record.user_id is None
        assert "user_id" not in record.kwargs["config"].get("context", {})
        assert RUNTIME_USER_ID_HEADER_NAME not in str(record.kwargs)
        assert RUNTIME_USER_ID_HEADER_NAME not in str(record.metadata)

        thread = await forwarding_gateway.thread_store.get(record.thread_id, user_id=None)
        assert thread is not None
        assert thread["user_id"] == DEFAULT_USER_ID

        runtime_context = forwarding_gateway.runtime_captures[0]["config"]["context"]
        assert runtime_context["user_id"] == _channel_storage_user_id(msg)
        checkpoint_config = forwarding_gateway.runtime_captures[0]["config"]["configurable"]
        assert "user_id" not in checkpoint_config
        assert RUNTIME_USER_ID_HEADER_NAME not in str(checkpoint_config)
        for authority_key in ("project_id", "owner_user_id", "project_context", "__private_scope", "role", "capabilities"):
            assert authority_key not in runtime_context

    asyncio.run(scenario())


def test_non_internal_runtime_header_is_ignored_and_body_user_id_is_stripped(forwarding_gateway):
    async def scenario():
        await forwarding_gateway.thread_store.create("browser-thread", user_id="browser-user", metadata={})
        request = SimpleNamespace(
            headers={RUNTIME_USER_ID_HEADER_NAME: "header-attacker"},
            state=SimpleNamespace(
                user=SimpleNamespace(id="browser-user", system_role="user"),
                auth_source=AUTH_SOURCE_SESSION,
            ),
            app=forwarding_gateway.app,
        )
        body = RunCreateRequest(
            assistant_id="lead_agent",
            input={"messages": [{"role": "human", "content": "hi"}]},
            context={
                "user_id": "body-attacker",
                "project_id": "attacker-project",
                "project_context": {"role": "admin"},
            },
        )
        token = set_current_user(request.state.user)
        try:
            record = await start_run(body, "browser-thread", request)
            forwarding_gateway.records.append(record)
            assert record.task is not None
            await record.task
        finally:
            reset_current_user(token)

        capture = forwarding_gateway.runtime_captures[0]
        assert capture["config"]["context"]["user_id"] == "browser-user"
        assert capture["effective_user_id"] == "browser-user"
        assert "project_id" not in capture["config"]["context"]
        assert "project_context" not in capture["config"]["context"]

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["wait", "create", "stream"])
def test_bound_owner_header_contract_is_preserved_for_all_channel_run_calls(
    operation,
    tmp_path,
    forwarding_gateway,
    monkeypatch,
):
    async def scenario():
        manager = _manager(tmp_path, forwarding_gateway)
        channel_name = f"runtime-{operation}"
        if operation == "create":
            CHANNEL_RUN_POLICY[channel_name] = ChannelRunPolicy(fire_and_forget=True)
        if operation == "stream":
            monkeypatch.setattr(manager, "_channel_supports_streaming", lambda _channel_name: True)
        msg = InboundMessage(
            channel_name=channel_name,
            chat_id="bound-chat",
            user_id="platform-user",
            owner_user_id="owner-user",
            text="hi",
        )
        try:
            await manager._handle_chat(msg)
        finally:
            CHANNEL_RUN_POLICY.pop(channel_name, None)

        call = forwarding_gateway.run_calls[0]
        assert call["operation"] == operation
        assert call["headers"][INTERNAL_AUTH_HEADER_NAME]
        assert call["headers"][INTERNAL_OWNER_USER_ID_HEADER_NAME] == "owner-user"
        assert call["headers"][RUNTIME_USER_ID_HEADER_NAME] == _channel_storage_user_id(msg)

        record = forwarding_gateway.records[0]
        assert record.user_id == "owner-user"
        thread = await forwarding_gateway.thread_store.get(record.thread_id, user_id=None)
        assert thread is not None
        assert thread["user_id"] == "owner-user"
        assert forwarding_gateway.runtime_captures[0]["config"]["context"]["user_id"] == "owner-user"
        assert forwarding_gateway.runtime_captures[0]["effective_user_id"] == "owner-user"

        thread_create_headers = forwarding_gateway.thread_calls[0]["headers"]
        assert thread_create_headers[INTERNAL_OWNER_USER_ID_HEADER_NAME] == "owner-user"
        assert RUNTIME_USER_ID_HEADER_NAME not in thread_create_headers

    asyncio.run(scenario())
