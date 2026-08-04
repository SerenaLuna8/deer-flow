from __future__ import annotations

import asyncio
import hashlib
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql

from app.channel_group_bindings.service import ProjectChannelGroupBindingService
from app.private_work.connection_inbound import ConnectionInboundResolver, ProviderIdentity
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkConflict
from app.private_work.thread_repository import PrivateThreadRepository
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from deerflow.runtime.private_scope import PrivateResourceScope

PROJECT_ID = uuid.UUID("10000000-0000-4000-8000-000000000010")
INSTANCE_ID = uuid.UUID("20000000-0000-4000-8000-000000000010")
AGENT_ID = uuid.UUID("30000000-0000-4000-8000-000000000010")
GROUP_ID = "oc_bound_group"
TOPIC_ID = "om_same_topic"


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def begin(self):
        return self


class _IdentityHasher:
    @staticmethod
    def group_refs(
        provider: str,
        instance_id: uuid.UUID,
        external_chat_id: str,
    ) -> tuple[str, ...]:
        assert (provider, instance_id, external_chat_id) == (
            "feishu",
            INSTANCE_ID,
            GROUP_ID,
        )
        return (hashlib.sha256(b"bound-feishu-group").hexdigest(),)

    @staticmethod
    def account_refs(
        provider: str,
        instance_id: uuid.UUID,
        external_account_id: str,
    ) -> tuple[str, ...]:
        assert provider == "feishu"
        assert instance_id == INSTANCE_ID
        return (
            hashlib.sha256(
                f"guest:{external_account_id}".encode(),
            ).hexdigest(),
        )


class _GuestAuthorityRepository:
    def __init__(self) -> None:
        self._authorities: dict[str, dict[str, object]] = {}

    async def resolve_or_create_guest(
        self,
        _session: object,
        *,
        provider: str,
        channel_instance_id: uuid.UUID,
        external_group_refs: tuple[str, ...],
        external_account_refs: tuple[str, ...],
        now: object,
    ) -> dict[str, object]:
        del now
        assert provider == "feishu"
        assert channel_instance_id == INSTANCE_ID
        assert external_group_refs == (hashlib.sha256(b"bound-feishu-group").hexdigest(),)
        account_ref = external_account_refs[0]
        authority = self._authorities.get(account_ref)
        if authority is not None:
            return authority
        owner_id = uuid.uuid5(uuid.NAMESPACE_URL, account_ref)
        authority = {
            "id": f"guest-{account_ref[:24]}",
            "account_id": str(owner_id),
            "project_id": str(PROJECT_ID),
            "owner_user_id": str(owner_id),
            "membership_version": 1,
            "provider": "feishu",
            "status": "connected",
            "channel_instance_id": str(INSTANCE_ID),
            "external_account_id": account_ref,
            "workspace_id": external_group_refs[0],
            "metadata": {
                "agent_asset_id": str(AGENT_ID),
                "agent_scope": "system",
            },
        }
        self._authorities[account_ref] = authority
        return authority


class _InboundRepository:
    def __init__(self) -> None:
        self.connections: dict[str, dict[str, object]] = {}
        self.conversations: dict[
            tuple[PrivateResourceScope, str, str, str | None],
            str,
        ] = {}
        self.creation_locks: dict[tuple[object, ...], asyncio.Lock] = {}

    async def find_connection_by_external_identity(
        self,
        *,
        provider: str,
        channel_instance_id: str | None,
        external_account_id: str,
        workspace_id: str | None,
        expected_connection_id: str | None = None,
        expected_scope: PrivateResourceScope | None = None,
    ) -> dict[str, object] | None:
        assert provider == "feishu"
        assert channel_instance_id == str(INSTANCE_ID)
        assert workspace_id == GROUP_ID
        connection = self.connections.get(external_account_id)
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
        scope: PrivateResourceScope,
        connection_id: str,
        external_conversation_id: str,
        external_topic_id: str | None,
    ) -> str | None:
        return self.conversations.get(
            (
                scope,
                connection_id,
                external_conversation_id,
                external_topic_id,
            )
        )

    async def set_thread_id(self, **kwargs: Any) -> bool:
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
        del kwargs
        raise AssertionError("conversation creation must not hold a pool connection")
        yield


class _ThreadService:
    def __init__(self) -> None:
        self.create_calls: list[dict[str, Any]] = []
        self.thread_ids: set[str] = set()
        self.records: dict[str, object] = {}

    async def create(
        self,
        context: PrivateWorkContext,
        **kwargs: Any,
    ) -> object:
        await asyncio.sleep(0)
        if kwargs["thread_id"] in self.thread_ids:
            raise PrivateWorkConflict(context.request_id)
        self.thread_ids.add(kwargs["thread_id"])
        self.create_calls.append({"context": context, **kwargs})
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

    async def get(
        self,
        context: PrivateWorkContext,
        thread_id: str,
    ) -> object | None:
        del context
        return self.records.get(thread_id)

    async def is_initialized(
        self,
        context: PrivateWorkContext,
        thread_id: str,
    ) -> bool:
        del context
        return thread_id in self.records


def _guest_context(owner_user_id: uuid.UUID) -> PrivateWorkContext:
    return PrivateWorkContext.from_project(
        ProjectContext(
            user_id=owner_user_id,
            project_id=PROJECT_ID,
            membership_id=uuid.uuid5(uuid.NAMESPACE_OID, str(owner_user_id)),
            role=ProjectRole.CHANNEL_GUEST,
            capabilities=capabilities_for(ProjectRole.CHANNEL_GUEST),
            membership_version=1,
            request_id="channel-group-guest",
        )
    )


def _identity(sender_id: str) -> ProviderIdentity:
    return ProviderIdentity(
        provider="feishu",
        channel_instance_id=str(INSTANCE_ID),
        external_account_id=sender_id,
        workspace_id=GROUP_ID,
        external_conversation_id=GROUP_ID,
        external_topic_id=TOPIC_ID,
    )


@pytest.mark.asyncio
async def test_bound_group_same_topic_reuses_per_sender_thread_without_sharing_owner() -> None:
    guest_repository = _GuestAuthorityRepository()
    binding_service = ProjectChannelGroupBindingService(
        _Session,
        repository=guest_repository,
        identity_hasher=_IdentityHasher(),
    )
    inbound_repository = _InboundRepository()
    thread_service = _ThreadService()
    resolver = ConnectionInboundResolver(
        repository=inbound_repository,
        session_factory=object(),
        thread_service=thread_service,
        request_id_factory=lambda: "channel-group-guest",
    )

    contexts: dict[uuid.UUID, PrivateWorkContext] = {}

    async def resolve_context(
        *,
        project_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        request_id: str,
    ) -> PrivateWorkContext:
        assert project_id == PROJECT_ID
        assert request_id == "channel-group-guest"
        return contexts.setdefault(owner_user_id, _guest_context(owner_user_id))

    resolver._resolve_context = AsyncMock(side_effect=resolve_context)

    async def register(sender_id: str) -> None:
        authority = await binding_service.resolve_or_create_guest(
            provider="feishu",
            channel_instance_id=INSTANCE_ID,
            chat_id=GROUP_ID,
            sender_id=sender_id,
        )
        inbound_repository.connections[sender_id] = authority

    await register("ou_sender_a")
    sender_a_first = await resolver.resolve(_identity("ou_sender_a"))
    await register("ou_sender_a")
    sender_a_second = await resolver.resolve(_identity("ou_sender_a"))
    await register("ou_sender_b")
    sender_b = await resolver.resolve(_identity("ou_sender_b"))
    await register("ou_sender_c")
    sender_c_first, sender_c_second = await asyncio.gather(
        resolver.resolve(_identity("ou_sender_c")),
        resolver.resolve(_identity("ou_sender_c")),
    )

    assert sender_a_first.created is True
    assert sender_a_second.created is False
    assert sender_a_first.thread_id == sender_a_second.thread_id
    assert sender_a_first.context.user_id == sender_a_second.context.user_id

    assert sender_b.created is True
    assert sender_b.thread_id != sender_a_first.thread_id
    assert sender_b.connection_id != sender_a_first.connection_id
    assert sender_b.context.user_id != sender_a_first.context.user_id
    assert sender_b.context.resource_scope != sender_a_first.context.resource_scope
    assert sender_c_first.thread_id == sender_c_second.thread_id
    assert {sender_c_first.created, sender_c_second.created} == {True, False}

    assert len(thread_service.create_calls) == 3
    assert {call["context"].role for call in thread_service.create_calls} == {ProjectRole.CHANNEL_GUEST}
    assert {call["metadata"]["external_conversation_id"] for call in thread_service.create_calls} == {GROUP_ID}
    assert {call["metadata"]["external_topic_id"] for call in thread_service.create_calls} == {TOPIC_ID}


class _EmptyRows:
    def scalars(self):
        return self

    def __iter__(self):
        return iter(())


class _CaptureSession:
    def __init__(self) -> None:
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _EmptyRows()


@pytest.mark.asyncio
async def test_normal_web_thread_catalog_is_exact_owner_scoped_and_excludes_channel_guest() -> None:
    project_id = uuid.UUID("50000000-0000-4000-8000-000000000010")
    signed_in_user_id = uuid.UUID("60000000-0000-4000-8000-000000000010")
    guest_user_id = uuid.UUID("70000000-0000-4000-8000-000000000010")
    session = _CaptureSession()

    assert (
        await PrivateThreadRepository(session).search(  # type: ignore[arg-type]
            scope=PrivateResourceScope(
                project_id=str(project_id),
                owner_user_id=str(signed_in_user_id),
                membership_version=3,
            )
        )
        == ()
    )

    compiled = str(
        session.statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "threads_meta.project_id =" in compiled
    assert f"threads_meta.owner_user_id = '{signed_in_user_id}'" in compiled
    assert str(guest_user_id) not in compiled
