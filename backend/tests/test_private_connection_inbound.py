from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import text
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.channels.connection_identity import attach_connection_identity
from app.channels.message_bus import InboundMessage
from app.private_work.errors import (
    PrivateWorkConflict,
    PrivateWorkInvalid,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from deerflow.runtime.private_scope import PrivateResourceScope

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
        self.set_result = True
        self.creation_locks: dict[tuple[object, ...], asyncio.Lock] = {}

    async def find_connection_by_external_identity(
        self,
        *,
        provider: str,
        channel_instance_id: str | None = None,
        external_account_id: str,
        workspace_id: str | None,
        expected_connection_id: str | None = None,
        expected_scope: object | None = None,
    ) -> dict[str, Any] | None:
        del channel_instance_id
        connection = self.connections.get(
            (provider, external_account_id, workspace_id),
        )
        if connection is None:
            return None
        if expected_connection_id is not None and connection.get("id") != expected_connection_id:
            return None
        if expected_scope is not None and (connection.get("project_id") != expected_scope.project_id or connection.get("owner_user_id") != expected_scope.owner_user_id):
            return None
        return connection

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

    async def set_thread_id(self, **kwargs: Any) -> bool:
        self.set_calls.append(kwargs)
        if not self.set_result:
            return False
        self.conversations[
            (
                kwargs["scope"],
                kwargs["connection_id"],
                kwargs["external_conversation_id"],
                kwargs["external_topic_id"],
            )
        ] = kwargs["thread_id"]
        return True

    @asynccontextmanager
    async def serialize_conversation_creation(self, **kwargs: Any):
        key = (
            kwargs["scope"],
            kwargs["connection_id"],
            kwargs["provider"],
            kwargs["external_conversation_id"],
            kwargs["external_topic_id"],
        )
        lock = self.creation_locks.setdefault(key, asyncio.Lock())
        async with lock:
            yield


class FakePrivateThreadService:
    def __init__(self) -> None:
        self.create_calls: list[dict[str, Any]] = []
        self.records: dict[str, object] = {}
        self.conflict_on_create = False
        self.initialized = True

    async def create(self, context, **kwargs: Any):
        self.create_calls.append({"context": context, **kwargs})
        if self.conflict_on_create or kwargs["thread_id"] in self.records:
            raise PrivateWorkConflict(context.request_id)
        record = SimpleNamespace(
            thread_id=kwargs["thread_id"],
            project_id=context.project_id,
            owner_user_id=str(context.user_id),
            agent_asset_id=kwargs["agent"].asset_id,
            agent_scope=kwargs["agent"].scope,
            metadata=dict(kwargs["metadata"]),
        )
        self.records[kwargs["thread_id"]] = record
        return record

    async def get(self, context, thread_id: str):
        del context
        return self.records.get(thread_id)

    async def is_initialized(self, context, thread_id: str) -> bool:
        del context
        return self.initialized and thread_id in self.records


def _connection(context, *, connection_id: str, agent_id: uuid.UUID | None) -> dict[str, Any]:
    metadata = {}
    if agent_id is not None:
        metadata = {
            "agent_asset_id": str(agent_id),
            "agent_scope": "project",
        }
    return {
        "id": connection_id,
        "account_id": str(context.user_id),
        "project_id": str(context.project_id),
        "owner_user_id": str(context.user_id),
        "external_account_id": "external-a",
        "workspace_id": "workspace-a",
        "status": "connected",
        "metadata": metadata,
    }


def _identity(
    *,
    account: str = "external-a",
    conversation: str = "chat-a",
):
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
async def test_resolver_fails_closed_when_new_conversation_binding_loses_authority() -> None:
    from app.private_work.connection_inbound import ConnectionInboundResolver

    owner_id = uuid.uuid4()
    context = SimpleNamespace(
        user_id=owner_id,
        project_id=uuid.uuid4(),
        resource_scope=object(),
    )
    repository = FakeConnectionRepository()
    repository.connections[("slack", "external-a", "workspace-a")] = _connection(
        context,
        connection_id="connection-a",
        agent_id=uuid.uuid4(),
    )
    repository.set_result = False
    resolver = ConnectionInboundResolver(
        repository=repository,
        session_factory=object(),
        thread_service=FakePrivateThreadService(),
        request_id_factory=lambda: "req-inbound",
        thread_id_factory=lambda: THREAD_ID,
    )
    resolver._resolve_context = AsyncMock(return_value=context)

    with pytest.raises(PrivateWorkNotFound):
        await resolver.resolve(_identity())


@pytest.mark.asyncio
async def test_resolver_recovers_exact_deterministic_thread_left_before_mapping_commit() -> None:
    from app.private_work.connection_inbound import ConnectionInboundResolver

    owner_id = uuid.uuid4()
    project_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    scope = PrivateResourceScope(
        project_id=str(project_id),
        owner_user_id=str(owner_id),
        membership_version=1,
    )
    context = SimpleNamespace(
        user_id=owner_id,
        project_id=project_id,
        resource_scope=scope,
        request_id="req-inbound",
    )
    repository = FakeConnectionRepository()
    repository.connections[("slack", "external-a", "workspace-a")] = _connection(
        context,
        connection_id="connection-a",
        agent_id=agent_id,
    )
    threads = FakePrivateThreadService()
    threads.conflict_on_create = True
    threads.records[str(THREAD_ID)] = SimpleNamespace(
        thread_id=str(THREAD_ID),
        project_id=project_id,
        owner_user_id=str(owner_id),
        agent_asset_id=agent_id,
        agent_scope="project",
        metadata={
            "source": "channel",
            "channel_name": "slack",
            "channel_instance_id": "slack",
            "external_conversation_id": "chat-a",
            "external_topic_id": "topic-a",
        },
    )
    resolver = ConnectionInboundResolver(
        repository=repository,
        session_factory=object(),
        thread_service=threads,
        request_id_factory=lambda: "req-inbound",
        thread_id_factory=lambda: THREAD_ID,
    )
    resolver._resolve_context = AsyncMock(return_value=context)

    resolved = await resolver.resolve(_identity())

    assert resolved.thread_id == str(THREAD_ID)
    assert resolved.created is False
    assert repository.conversations[(scope, "connection-a", "chat-a", "topic-a")] == str(THREAD_ID)


@pytest.mark.asyncio
async def test_resolver_never_maps_thread_without_initialized_checkpoint() -> None:
    from app.private_work.connection_inbound import ConnectionInboundResolver

    owner_id = uuid.uuid4()
    project_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    scope = PrivateResourceScope(
        project_id=str(project_id),
        owner_user_id=str(owner_id),
        membership_version=1,
    )
    context = SimpleNamespace(
        user_id=owner_id,
        project_id=project_id,
        resource_scope=scope,
        request_id="req-inbound",
    )
    repository = FakeConnectionRepository()
    repository.connections[("slack", "external-a", "workspace-a")] = _connection(
        context,
        connection_id="connection-a",
        agent_id=agent_id,
    )
    threads = FakePrivateThreadService()
    threads.conflict_on_create = True
    threads.initialized = False
    threads.records[str(THREAD_ID)] = SimpleNamespace(
        thread_id=str(THREAD_ID),
        project_id=project_id,
        owner_user_id=str(owner_id),
        agent_asset_id=agent_id,
        agent_scope="project",
        metadata={
            "source": "channel",
            "channel_name": "slack",
            "channel_instance_id": "slack",
            "external_conversation_id": "chat-a",
            "external_topic_id": "topic-a",
        },
    )
    resolver = ConnectionInboundResolver(
        repository=repository,
        session_factory=object(),
        thread_service=threads,
        request_id_factory=lambda: "req-inbound",
        thread_id_factory=lambda: THREAD_ID,
    )
    resolver._resolve_context = AsyncMock(return_value=context)

    with pytest.raises(PrivateWorkUnavailable):
        await resolver.resolve(_identity())

    assert repository.set_calls == []


@pytest.mark.asyncio
async def test_resolver_rejects_a_row_other_than_the_attached_exact_connection() -> None:
    from app.private_work.connection_inbound import ConnectionInboundResolver

    owner_id = uuid.uuid4()
    project_id = uuid.uuid4()
    scope = PrivateResourceScope(
        project_id=str(project_id),
        owner_user_id=str(owner_id),
        membership_version=1,
    )
    context = SimpleNamespace(
        user_id=owner_id,
        project_id=project_id,
        resource_scope=scope,
    )
    repository = FakeConnectionRepository()
    repository.connections[("slack", "external-a", "workspace-a")] = _connection(
        context,
        connection_id="actual-connection",
        agent_id=uuid.uuid4(),
    )
    threads = FakePrivateThreadService()
    resolver = ConnectionInboundResolver(
        repository=repository,
        session_factory=object(),
        thread_service=threads,
        request_id_factory=lambda: "req-inbound",
    )

    with pytest.raises(PrivateWorkNotFound):
        await resolver.resolve(
            _identity(),
            expected_connection_id="attached-connection",
            expected_scope=scope,
        )

    assert threads.create_calls == []


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_concurrent_first_messages_do_not_deadlock_with_single_connection_pool(
    seed: M4ThreadSeed,
    migrated_postgres_database_url: str,
) -> None:
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.private_work.connection_inbound import ConnectionInboundResolver
    from app.private_work.thread_repository import PrivateThreadRepository
    from deerflow.persistence.channel_connections.model import (
        ChannelConversationRow,
    )
    from deerflow.persistence.channel_connections.sql import (
        ChannelConnectionRepository,
    )
    from deerflow.persistence.thread_meta.model import ThreadMetaRow

    engine = create_async_engine(
        migrated_postgres_database_url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=1,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = ChannelConnectionRepository(factory)
    connection = await repository.upsert_connection(
        scope=seed.owner_a.resource_scope,
        provider="slack",
        external_account_id="external-a",
        workspace_id="workspace-a",
        metadata={
            "agent_asset_id": str(seed.project_agent_id),
            "agent_scope": "project",
        },
    )

    class _DatabaseThreadCreator:
        async def create(self, context, **kwargs: Any):
            async with factory() as session, session.begin():
                return await PrivateThreadRepository(session).create(
                    scope=context.resource_scope,
                    thread_id=kwargs["thread_id"],
                    agent=kwargs["agent"],
                    metadata=kwargs["metadata"],
                )

        async def get(self, context, thread_id: str):
            async with factory() as session, session.begin():
                return await PrivateThreadRepository(session).get(
                    scope=context.resource_scope,
                    thread_id=thread_id,
                )

        async def is_initialized(self, context, thread_id: str) -> bool:
            del context, thread_id
            return True

    resolver = ConnectionInboundResolver(
        repository=repository,
        session_factory=factory,
        thread_service=_DatabaseThreadCreator(),
        request_id_factory=lambda: "single-pool-first-message",
    )
    identity = _identity()
    try:
        first, second = await asyncio.wait_for(
            asyncio.gather(
                resolver.resolve(
                    identity,
                    expected_connection_id=connection["id"],
                    expected_scope=seed.owner_a.resource_scope,
                ),
                resolver.resolve(
                    identity,
                    expected_connection_id=connection["id"],
                    expected_scope=seed.owner_a.resource_scope,
                ),
            ),
            timeout=3,
        )
        assert first.thread_id == second.thread_id
        assert {first.created, second.created} == {True, False}
        async with factory() as session:
            thread_count = await session.scalar(
                select(func.count())
                .select_from(ThreadMetaRow)
                .where(
                    ThreadMetaRow.project_id == seed.owner_a.project_id,
                    ThreadMetaRow.owner_user_id == str(seed.owner_a.user_id),
                    ThreadMetaRow.thread_id == first.thread_id,
                )
            )
            conversation_count = await session.scalar(
                select(func.count())
                .select_from(ChannelConversationRow)
                .where(
                    ChannelConversationRow.project_id == seed.owner_a.project_id,
                    ChannelConversationRow.owner_user_id == str(seed.owner_a.user_id),
                    ChannelConversationRow.connection_id == connection["id"],
                    ChannelConversationRow.external_conversation_id == "chat-a",
                    ChannelConversationRow.external_topic_id == "topic-a",
                )
            )
        assert thread_count == 1
        assert conversation_count == 1

        removed = await repository.remove_thread_ids(
            scope=seed.owner_a.resource_scope,
            connection_id=connection["id"],
            provider="slack",
            external_conversation_id="chat-a",
            external_topic_id="topic-a",
        )
        assert removed is True
        recovered = await resolver.resolve(
            identity,
            expected_connection_id=connection["id"],
            expected_scope=seed.owner_a.resource_scope,
        )
        assert recovered.thread_id == first.thread_id
        assert recovered.created is False
        assert (
            await repository.get_thread_id(
                scope=seed.owner_a.resource_scope,
                connection_id=connection["id"],
                external_conversation_id="chat-a",
                external_topic_id="topic-a",
            )
            == first.thread_id
        )
    finally:
        await engine.dispose()


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
async def test_resolver_rejects_connection_account_owner_mismatch(
    seed: M4ThreadSeed,
) -> None:
    repository = FakeConnectionRepository()
    connection = _connection(
        seed.owner_a,
        connection_id="connection-a",
        agent_id=seed.project_agent_id,
    )
    connection["account_id"] = str(seed.owner_b.user_id)
    repository.connections[("slack", "external-a", "workspace-a")] = connection

    with pytest.raises(PrivateWorkNotFound):
        await _resolver(seed, repository, FakePrivateThreadService()).resolve(_identity())


@pytest.mark.asyncio
@pytest.mark.postgres
@pytest.mark.parametrize("invalid_scope", ["removed-member", "deleted-project"])
async def test_resolver_rejects_inactive_project_authority(
    seed: M4ThreadSeed,
    invalid_scope: str,
) -> None:
    repository = FakeConnectionRepository()
    repository.connections[("slack", "external-a", "workspace-a")] = _connection(
        seed.owner_a,
        connection_id="connection-a",
        agent_id=seed.project_agent_id,
    )
    async with seed.engine.begin() as connection:
        if invalid_scope == "removed-member":
            await connection.execute(
                text(
                    """UPDATE project_memberships
                    SET status='removed', ended_at=now(), end_reason='removed'
                    WHERE project_id=:project_id AND user_id=:user_id"""
                ),
                {
                    "project_id": seed.owner_a.project_id,
                    "user_id": str(seed.owner_a.user_id),
                },
            )
        else:
            await connection.execute(
                text(
                    """UPDATE projects
                    SET status='pending_deletion', deletion_requested_at=now(),
                        deletion_effective_at=now()
                    WHERE id=:project_id"""
                ),
                {"project_id": seed.owner_a.project_id},
            )

    with pytest.raises(PrivateWorkNotFound):
        await _resolver(seed, repository, FakePrivateThreadService()).resolve(_identity())


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
    assert attached.workspace_id == "workspace-a"
    assert attached.owner_user_id != str(owner_user_id)
    assert attached.project_id != str(project_id)
    assert attached.private_scope is not None
    assert attached.private_scope.project_id == str(project_id)
    assert attached.private_scope.owner_user_id == str(owner_user_id)


@pytest.mark.asyncio
async def test_attach_connection_identity_clears_forged_mapping_scope_when_unbound() -> None:
    repository = FakeConnectionRepository()
    inbound = InboundMessage(
        channel_name="feishu",
        chat_id="chat-a",
        user_id="external-a",
        text="hello",
        connection_id="forged-connection",
        private_scope=SimpleNamespace(),  # type: ignore[arg-type]
    )

    attached = await attach_connection_identity(
        inbound,
        repo=repository,
        provider="feishu",
        workspace_id="chat-a",
    )

    assert attached.connection_id is None
    assert attached.private_scope is None


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_project_inbound_dispatcher_uses_only_resolved_context_and_thread(
    seed: M4ThreadSeed,
) -> None:
    from app.private_work.connection_inbound import (
        ProjectInboundDispatcher,
        ResolvedInboundPrivateWork,
    )
    from app.private_work.run_admission import PrivateRunInboundAuthority

    authority = PrivateRunInboundAuthority(
        connection_id="connection-a",
        provider="slack",
        external_account_id="external-a",
        workspace_id="workspace-a",
        external_conversation_id="chat-a",
        external_topic_id="topic-a",
    )
    resolved = ResolvedInboundPrivateWork(
        account_id=seed.owner_a.user_id,
        context=seed.owner_a,
        connection_id="connection-a",
        thread_id="private-thread",
        created=False,
        authority=authority,
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
        connection_id="connection-a",
        private_scope=seed.owner_a.resource_scope,
        metadata={"message_id": "delivery-a"},
    )

    result = await ProjectInboundDispatcher(resolver, launcher).dispatch(message)

    identity = resolver.resolve.await_args.args[0]
    assert identity.provider == "slack"
    assert identity.external_account_id == "external-a"
    assert identity.workspace_id == "workspace-a"
    assert identity.external_conversation_id == "chat-a"
    assert identity.external_topic_id == "topic-a"
    assert resolver.resolve.await_args.kwargs == {
        "expected_connection_id": "connection-a",
        "expected_scope": seed.owner_a.resource_scope,
    }
    launcher.assert_awaited_once_with(
        seed.owner_a,
        "private-thread",
        message,
        authority,
    )
    assert result.resolved is resolved
    assert result.state["messages"][-1]["content"] == "project response"


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_gateway_project_run_launcher_calls_private_start_wait_and_scoped_state(
    seed: M4ThreadSeed,
) -> None:
    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.checkpoint.memory import InMemorySaver

    from app.private_work.checkpointer import ProjectScopedCheckpointer
    from app.private_work.connection_inbound import build_gateway_project_run_launcher
    from app.private_work.run_admission import PrivateRunInboundAuthority
    from app.private_work.run_service import PrivateRunService
    from app.private_work.thread_repository import (
        PrivateThreadRepository,
        ThreadAgentRef,
    )

    channel_values = {
        "messages": [
            {"type": "human", "content": "hello"},
            {"type": "ai", "content": "finished"},
        ]
    }

    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=seed.owner_a.resource_scope,
            thread_id="private-thread",
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
    raw = InMemorySaver()
    project_checkpointer = ProjectScopedCheckpointer(raw, seed.factory)
    checkpoint = empty_checkpoint()
    messages_version = checkpoint["id"]
    checkpoint["channel_versions"] = {"messages": messages_version}
    checkpoint["channel_values"] = channel_values
    await project_checkpointer.for_context(seed.owner_a).aput(
        {
            "configurable": {
                "thread_id": "private-thread",
                "checkpoint_ns": "",
            }
        },
        checkpoint,
        {"source": "loop", "step": 0, "parents": {}},
        {"messages": messages_version},
    )

    durable_reads: list[tuple[object, str, str]] = []

    class FakePrivateRunService(PrivateRunService):
        def __init__(self) -> None:
            self.statuses = iter(("pending", "success"))

        async def get(self, context, thread_id, run_id):
            durable_reads.append((context, thread_id, run_id))
            return SimpleNamespace(status=next(self.statuses))

    app = SimpleNamespace(
        state=SimpleNamespace(
            private_run_service=FakePrivateRunService(),
            project_scoped_checkpointer=project_checkpointer,
        )
    )
    record = SimpleNamespace(run_id="run-a", thread_id="private-thread")
    private_start = AsyncMock(return_value=record)
    launcher = build_gateway_project_run_launcher(
        app=app,
        start_private_run_fn=private_start,
    )
    message = InboundMessage(
        channel_name="slack",
        chat_id="chat-a",
        user_id="external-a",
        text="hello",
        topic_id="topic-a",
        provider_delivery_id="delivery-a",
    )
    authority = PrivateRunInboundAuthority(
        connection_id="connection-a",
        provider="slack",
        external_account_id="external-a",
        workspace_id="workspace-a",
        external_conversation_id="chat-a",
        external_topic_id="topic-a",
    )

    result = await launcher(seed.owner_a, "private-thread", message, authority)

    private_start.assert_awaited_once()
    body, thread_id, request, context = private_start.await_args.args
    assert thread_id == "private-thread"
    assert context is seed.owner_a
    assert body.input == {"messages": [{"role": "user", "content": "hello"}]}
    server_context = private_start.await_args.kwargs["server_context"]
    assert server_context.inbound_authority == authority
    assert server_context.inbound_delivery.provider_delivery_id == "delivery-a"
    assert durable_reads == [
        (seed.owner_a, "private-thread", "run-a"),
        (seed.owner_a, "private-thread", "run-a"),
    ]
    assert result.state == {
        **channel_values,
        "artifacts": [],
        "delegations": [],
        "skill_context": [],
        "viewed_images": {},
    }
    assert result.disposition == "admitted"


def test_channel_service_builds_project_dispatcher_from_gateway_runtime() -> None:
    from app.channels.service import ChannelService
    from app.private_work.connection_inbound import ProjectInboundDispatcher

    session_factory = object()
    scoped_checkpointer = object()
    repository = SimpleNamespace(session_factory=session_factory)
    gateway_app = SimpleNamespace(state=SimpleNamespace(project_scoped_checkpointer=scoped_checkpointer))

    service = ChannelService(
        connection_repo=repository,
        gateway_app=gateway_app,
    )

    dispatcher = service.manager._private_inbound_dispatcher
    assert isinstance(dispatcher, ProjectInboundDispatcher)
    assert dispatcher._resolver._repository is repository
    assert dispatcher._resolver._session_factory is session_factory
    assert dispatcher._resolver._thread_service._project_scoped_checkpointer is scoped_checkpointer
