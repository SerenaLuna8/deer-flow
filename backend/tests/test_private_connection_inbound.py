from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import text
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.channels.connection_identity import attach_connection_identity
from app.channels.message_bus import InboundMessage
from app.private_work.errors import PrivateWorkInvalid, PrivateWorkNotFound

THREAD_ID = uuid.UUID("00000000-0000-4000-8000-000000000010")


@pytest_asyncio.fixture
async def seed(migrated_postgres_database_url: str) -> M4ThreadSeed:
    value = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        yield value
    finally:
        await value.engine.dispose()


class FakeConnectionRepository:
    def __init__(self) -> None:
        self.connections: dict[tuple[str, str, str | None], dict[str, Any]] = {}
        self.conversations: dict[tuple[object, str, str, str | None], str] = {}
        self.get_calls: list[dict[str, Any]] = []
        self.set_calls: list[dict[str, Any]] = []

    async def find_connection_by_external_identity(
        self,
        *,
        provider: str,
        external_account_id: str,
        workspace_id: str | None,
    ) -> dict[str, Any] | None:
        return self.connections.get((provider, external_account_id, workspace_id))

    async def get_thread_id(
        self,
        *,
        scope: object,
        connection_id: str,
        external_conversation_id: str,
        external_topic_id: str | None,
    ) -> str | None:
        call = {
            "scope": scope,
            "connection_id": connection_id,
            "external_conversation_id": external_conversation_id,
            "external_topic_id": external_topic_id,
        }
        self.get_calls.append(call)
        return self.conversations.get((scope, connection_id, external_conversation_id, external_topic_id))

    async def set_thread_id(self, **kwargs: Any) -> None:
        self.set_calls.append(kwargs)
        self.conversations[
            (
                kwargs["scope"],
                kwargs["connection_id"],
                kwargs["external_conversation_id"],
                kwargs["external_topic_id"],
            )
        ] = kwargs["thread_id"]


class FakePrivateThreadService:
    def __init__(self) -> None:
        self.create_calls: list[dict[str, Any]] = []

    async def create(self, context, **kwargs: Any):
        self.create_calls.append({"context": context, **kwargs})
        return SimpleNamespace(thread_id=kwargs["thread_id"])


def _connection(context, *, connection_id: str, agent_id: uuid.UUID | None) -> dict[str, Any]:
    metadata = {}
    if agent_id is not None:
        metadata = {
            "agent_asset_id": str(agent_id),
            "agent_scope": "project",
        }
    return {
        "id": connection_id,
        "project_id": str(context.project_id),
        "owner_user_id": str(context.user_id),
        "status": "connected",
        "metadata": metadata,
    }


def _identity(*, account: str = "external-a", conversation: str = "chat-a"):
    from app.private_work.connection_inbound import ProviderIdentity

    return ProviderIdentity(
        provider="slack",
        external_account_id=account,
        workspace_id="workspace-a",
        external_conversation_id=conversation,
        external_topic_id="topic-a",
    )


def _resolver(seed: M4ThreadSeed, repository: FakeConnectionRepository, threads: FakePrivateThreadService):
    from app.private_work.connection_inbound import ConnectionInboundResolver

    return ConnectionInboundResolver(
        repository=repository,
        session_factory=seed.factory,
        thread_service=threads,
        request_id_factory=lambda: "req-inbound",
        thread_id_factory=lambda: THREAD_ID,
    )


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_resolver_ignores_forged_message_authority_and_reuses_existing_conversation(
    seed: M4ThreadSeed,
) -> None:
    message = InboundMessage(
        channel_name="slack",
        chat_id="chat-a",
        user_id="external-a",
        text="hello",
        workspace_id="workspace-a",
        owner_user_id=str(seed.owner_b.user_id),
        project_id=str(seed.project_b_owner_a.project_id),
    )
    identity = _identity(account=message.user_id, conversation=message.chat_id)
    repository = FakeConnectionRepository()
    repository.connections[("slack", "external-a", "workspace-a")] = _connection(
        seed.owner_a,
        connection_id="connection-a",
        agent_id=seed.project_agent_id,
    )
    repository.conversations[(seed.owner_a.resource_scope, "connection-a", "chat-a", "topic-a")] = "existing-thread"
    threads = FakePrivateThreadService()

    resolved = await _resolver(seed, repository, threads).resolve(identity)

    assert resolved.context.project_id == seed.owner_a.project_id
    assert resolved.context.user_id == seed.owner_a.user_id
    assert resolved.thread_id == "existing-thread"
    assert resolved.created is False
    assert threads.create_calls == []
    assert repository.set_calls == []


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_resolver_keeps_conversation_lookup_inside_exact_project_owner_scope(
    seed: M4ThreadSeed,
) -> None:
    repository = FakeConnectionRepository()
    repository.connections[("slack", "external-a", "workspace-a")] = _connection(
        seed.owner_a,
        connection_id="connection-a",
        agent_id=seed.project_agent_id,
    )
    repository.conversations[(seed.project_b_owner_a.resource_scope, "connection-a", "chat-a", "topic-a")] = "wrong-project-thread"
    repository.conversations[(seed.owner_a.resource_scope, "connection-a", "chat-a", "topic-a")] = "project-a-thread"

    resolved = await _resolver(seed, repository, FakePrivateThreadService()).resolve(_identity())

    assert resolved.thread_id == "project-a-thread"
    assert repository.get_calls == [
        {
            "scope": seed.owner_a.resource_scope,
            "connection_id": "connection-a",
            "external_conversation_id": "chat-a",
            "external_topic_id": "topic-a",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_resolver_rejects_inactive_membership_before_thread_lookup_or_create(
    seed: M4ThreadSeed,
) -> None:
    repository = FakeConnectionRepository()
    repository.connections[("slack", "external-a", "workspace-a")] = _connection(
        seed.owner_a,
        connection_id="connection-a",
        agent_id=seed.project_agent_id,
    )
    threads = FakePrivateThreadService()
    async with seed.engine.begin() as connection:
        await connection.execute(
            text(
                """UPDATE project_memberships SET status='left'
                WHERE project_id=:project_id AND user_id=:owner_user_id"""
            ),
            {
                "project_id": seed.owner_a.project_id,
                "owner_user_id": str(seed.owner_a.user_id),
            },
        )

    with pytest.raises(PrivateWorkNotFound):
        await _resolver(seed, repository, threads).resolve(_identity())

    assert repository.get_calls == []
    assert repository.set_calls == []
    assert threads.create_calls == []


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_resolver_creates_missing_conversation_once_and_writes_exact_scope(
    seed: M4ThreadSeed,
) -> None:
    from app.private_work.thread_repository import ThreadAgentRef

    repository = FakeConnectionRepository()
    repository.connections[("slack", "external-a", "workspace-a")] = _connection(
        seed.owner_a,
        connection_id="connection-a",
        agent_id=seed.project_agent_id,
    )
    threads = FakePrivateThreadService()

    resolved = await _resolver(seed, repository, threads).resolve(_identity())

    assert resolved.thread_id == str(THREAD_ID)
    assert resolved.created is True
    assert len(threads.create_calls) == 1
    assert threads.create_calls[0]["context"].project_id == seed.owner_a.project_id
    assert threads.create_calls[0]["agent"] == ThreadAgentRef(
        seed.project_agent_id,
        "project",
    )
    assert repository.set_calls == [
        {
            "scope": seed.owner_a.resource_scope,
            "connection_id": "connection-a",
            "provider": "slack",
            "external_conversation_id": "chat-a",
            "external_topic_id": "topic-a",
            "thread_id": str(THREAD_ID),
        }
    ]


@pytest.mark.asyncio
@pytest.mark.postgres
@pytest.mark.parametrize("status", ["frozen", "revoked"])
async def test_resolver_rejects_non_connected_connection_before_private_work(
    seed: M4ThreadSeed,
    status: str,
) -> None:
    repository = FakeConnectionRepository()
    connection = _connection(
        seed.owner_a,
        connection_id="connection-a",
        agent_id=seed.project_agent_id,
    )
    connection["status"] = status
    repository.connections[("slack", "external-a", "workspace-a")] = connection
    threads = FakePrivateThreadService()

    with pytest.raises(PrivateWorkNotFound):
        await _resolver(seed, repository, threads).resolve(_identity())

    assert repository.get_calls == []
    assert threads.create_calls == []


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_resolver_rejects_missing_server_agent_before_thread_creation(
    seed: M4ThreadSeed,
) -> None:
    repository = FakeConnectionRepository()
    repository.connections[("slack", "external-a", "workspace-a")] = _connection(
        seed.owner_a,
        connection_id="connection-a",
        agent_id=None,
    )
    threads = FakePrivateThreadService()

    with pytest.raises(PrivateWorkInvalid):
        await _resolver(seed, repository, threads).resolve(_identity())

    assert threads.create_calls == []
    assert repository.set_calls == []


@pytest.mark.asyncio
async def test_attach_connection_identity_only_copies_server_lookup_fields() -> None:
    repository = FakeConnectionRepository()
    project_id = uuid.uuid4()
    owner_user_id = uuid.uuid4()
    repository.connections[("slack", "external-a", "workspace-a")] = {
        "id": "connection-a",
        "project_id": str(project_id),
        "owner_user_id": str(owner_user_id),
        "status": "connected",
        "metadata": {},
    }
    inbound = InboundMessage(
        channel_name="slack",
        chat_id="chat-a",
        user_id="external-a",
        text="hello",
        workspace_id="workspace-a",
        connection_id="forged-connection",
        owner_user_id=str(uuid.uuid4()),
        project_id=str(uuid.uuid4()),
    )

    attached = await attach_connection_identity(
        inbound,
        repo=repository,
        provider="slack",
        workspace_id="workspace-a",
    )

    assert attached.connection_id == "connection-a"
    assert attached.owner_user_id == str(owner_user_id)
    assert attached.project_id == str(project_id)


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_project_inbound_dispatcher_uses_only_resolved_context_and_thread(
    seed: M4ThreadSeed,
) -> None:
    from app.private_work.connection_inbound import (
        ProjectInboundDispatcher,
        ResolvedInboundPrivateWork,
    )

    resolved = ResolvedInboundPrivateWork(
        context=seed.owner_a,
        connection_id="connection-a",
        thread_id="private-thread",
        created=False,
    )
    resolver = SimpleNamespace(resolve=AsyncMock(return_value=resolved))
    launcher = AsyncMock(return_value={"messages": [{"type": "ai", "content": "project response"}]})
    message = InboundMessage(
        channel_name="slack",
        chat_id="chat-a",
        user_id="external-a",
        text="hello",
        workspace_id="workspace-a",
        topic_id="topic-a",
        owner_user_id=str(seed.owner_b.user_id),
        project_id=str(seed.project_b_owner_a.project_id),
    )

    result = await ProjectInboundDispatcher(resolver, launcher).dispatch(message)

    identity = resolver.resolve.await_args.args[0]
    assert identity.provider == "slack"
    assert identity.external_account_id == "external-a"
    assert identity.workspace_id == "workspace-a"
    assert identity.external_conversation_id == "chat-a"
    assert identity.external_topic_id == "topic-a"
    launcher.assert_awaited_once_with(seed.owner_a, "private-thread", message)
    assert result.resolved is resolved
    assert result.state["messages"][-1]["content"] == "project response"


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_gateway_project_run_launcher_calls_private_start_wait_and_scoped_state(
    seed: M4ThreadSeed,
    monkeypatch,
) -> None:
    from app.gateway import services
    from app.private_work.connection_inbound import build_gateway_project_run_launcher

    channel_values = {
        "messages": [
            {"type": "human", "content": "hello"},
            {"type": "ai", "content": "finished"},
        ]
    }

    class FakeCheckpointer:
        async def aget_tuple(self, config):
            assert config == {
                "configurable": {
                    "thread_id": "private-thread",
                    "checkpoint_ns": "",
                }
            }
            return SimpleNamespace(checkpoint={"channel_values": channel_values})

    class FakeProjectScopedCheckpointer:
        def for_context(self, context):
            assert context is seed.owner_a
            return FakeCheckpointer()

    bridge = object()
    run_manager = object()
    app = SimpleNamespace(
        state=SimpleNamespace(
            stream_bridge=bridge,
            run_manager=run_manager,
            project_scoped_checkpointer=FakeProjectScopedCheckpointer(),
        )
    )
    record = SimpleNamespace(run_id="run-a", thread_id="private-thread")
    private_start = AsyncMock(return_value=record)
    legacy_start = AsyncMock(side_effect=AssertionError("legacy start_run used"))
    wait = AsyncMock(return_value=True)
    monkeypatch.setattr(services, "start_private_run", private_start)
    monkeypatch.setattr(services, "start_run", legacy_start)
    monkeypatch.setattr(services, "wait_for_run_completion", wait)
    launcher = build_gateway_project_run_launcher(app=app)
    message = InboundMessage(
        channel_name="slack",
        chat_id="chat-a",
        user_id="external-a",
        text="hello",
        topic_id="topic-a",
    )

    result = await launcher(seed.owner_a, "private-thread", message)

    private_start.assert_awaited_once()
    body, thread_id, request, context = private_start.await_args.args
    assert thread_id == "private-thread"
    assert context is seed.owner_a
    assert body.input == {"messages": [{"role": "user", "content": "hello"}]}
    wait.assert_awaited_once_with(bridge, record, request, run_manager)
    legacy_start.assert_not_awaited()
    assert result == channel_values


def test_channel_service_builds_project_dispatcher_from_gateway_runtime() -> None:
    from app.channels.service import ChannelService
    from app.private_work.connection_inbound import ProjectInboundDispatcher

    session_factory = object()
    scoped_checkpointer = object()
    repository = SimpleNamespace(session_factory=session_factory)
    gateway_app = SimpleNamespace(state=SimpleNamespace(project_scoped_checkpointer=scoped_checkpointer))

    service = ChannelService(
        connection_repo=repository,
        require_bound_identity=True,
        gateway_app=gateway_app,
    )

    dispatcher = service.manager._private_inbound_dispatcher
    assert isinstance(dispatcher, ProjectInboundDispatcher)
    assert dispatcher._resolver._repository is repository
    assert dispatcher._resolver._session_factory is session_factory
    assert dispatcher._resolver._thread_service._project_scoped_checkpointer is scoped_checkpointer
