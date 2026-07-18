"""Resolve provider identities into authoritative project-private conversations."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.channels.message_bus import InboundMessage
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import (
    PrivateWorkInvalid,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from app.private_work.http_runtime import start_private_run
from app.private_work.run_service import (
    TERMINAL_PRIVATE_RUN_STATUSES,
    PrivateRunService,
)
from app.private_work.thread_repository import ThreadAgentRef
from app.projects.context import resolve_project_context
from app.projects.errors import ProjectDatabaseUnavailable, ProjectNotFound
from deerflow.runtime.private_scope import PrivateResourceScope


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    """Provider-owned coordinates; deliberately contains no project authority."""

    provider: str
    external_account_id: str
    workspace_id: str | None
    external_conversation_id: str
    external_topic_id: str | None


@dataclass(frozen=True, slots=True)
class ResolvedInboundPrivateWork:
    """Authoritative private-work destination for one inbound conversation."""

    account_id: uuid.UUID
    context: PrivateWorkContext
    connection_id: str
    thread_id: str
    created: bool


ProjectInboundState = dict[str, Any] | list[Any]
ProjectInboundRunLauncher = Callable[
    [PrivateWorkContext, str, InboundMessage],
    Awaitable[ProjectInboundState],
]


@dataclass(frozen=True, slots=True)
class ProjectInboundDispatchResult:
    resolved: ResolvedInboundPrivateWork
    state: ProjectInboundState


class ConnectionInboundRepository(Protocol):
    async def find_connection_by_external_identity(
        self,
        *,
        provider: str,
        external_account_id: str,
        workspace_id: str | None,
    ) -> Mapping[str, Any] | None: ...

    async def get_thread_id(
        self,
        *,
        scope: PrivateResourceScope,
        connection_id: str,
        external_conversation_id: str,
        external_topic_id: str | None,
    ) -> str | None: ...

    async def set_thread_id(
        self,
        *,
        scope: PrivateResourceScope,
        connection_id: str,
        provider: str,
        external_conversation_id: str,
        external_topic_id: str | None,
        thread_id: str,
    ) -> None: ...


class PrivateThreadCreator(Protocol):
    async def create(
        self,
        context: PrivateWorkContext,
        *,
        thread_id: str,
        agent: ThreadAgentRef,
        display_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> object: ...


class PrivateWorkInboundResolver(Protocol):
    async def resolve(
        self,
        provider_identity: ProviderIdentity,
    ) -> ResolvedInboundPrivateWork: ...


class ProjectInboundDispatcher:
    """Resolve an IM message, then launch it with the resolved private scope."""

    def __init__(
        self,
        resolver: PrivateWorkInboundResolver,
        run_launcher: ProjectInboundRunLauncher,
    ) -> None:
        self._resolver = resolver
        self._run_launcher = run_launcher

    async def dispatch(self, message: InboundMessage) -> ProjectInboundDispatchResult:
        resolved = await self._resolver.resolve(
            ProviderIdentity(
                provider=message.channel_name,
                external_account_id=message.user_id,
                workspace_id=message.workspace_id,
                external_conversation_id=message.chat_id,
                external_topic_id=message.topic_id,
            )
        )
        state = await self._run_launcher(
            resolved.context,
            resolved.thread_id,
            message,
        )
        return ProjectInboundDispatchResult(resolved=resolved, state=state)


def build_gateway_project_run_launcher(
    *,
    app: Any,
    start_private_run_fn: Callable[..., Awaitable[Any]] | None = None,
) -> ProjectInboundRunLauncher:
    """Build the in-process Gateway launcher used by project-bound IM text."""

    from app.gateway.auth_disabled import AUTH_SOURCE_INTERNAL
    from app.gateway.deps import get_project_checkpointer
    from app.gateway.internal_auth import get_internal_user
    from deerflow.runtime import serialize_channel_values_for_api

    private_start = start_private_run_fn or start_private_run

    async def launch(
        context: PrivateWorkContext,
        thread_id: str,
        message: InboundMessage,
    ) -> ProjectInboundState:
        async def is_disconnected() -> bool:
            return False

        request = SimpleNamespace(
            app=app,
            headers={},
            state=SimpleNamespace(
                user=get_internal_user(owner_user_id=str(context.user_id)),
                auth_source=AUTH_SOURCE_INTERNAL,
            ),
            cookies={},
            is_disconnected=is_disconnected,
        )
        body = SimpleNamespace(
            assistant_id=None,
            input={"messages": [{"role": "user", "content": message.text}]},
            command=None,
            metadata={
                "channel_source": {
                    "type": "im_channel",
                    "provider": message.channel_name,
                    "chat_id": message.chat_id,
                    "topic_id": message.topic_id,
                }
            },
            config=None,
            context={
                "channel_name": message.channel_name,
                "channel_user_id": message.user_id,
            },
            multitask_strategy="reject",
        )
        record = await private_start(body, thread_id, request, context)
        service = getattr(app.state, "private_run_service", None)
        if not isinstance(service, PrivateRunService):
            raise PrivateWorkUnavailable(context.request_id)
        while True:
            durable = await service.get(
                context,
                thread_id,
                record.run_id,
            )
            if durable.status in TERMINAL_PRIVATE_RUN_STATUSES:
                break
            await asyncio.sleep(0.1)

        checkpointer = get_project_checkpointer(request, context)
        checkpoint_tuple = await checkpointer.aget_tuple(
            {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": "",
                }
            }
        )
        if checkpoint_tuple is None:
            return {}
        checkpoint = getattr(checkpoint_tuple, "checkpoint", {}) or {}
        if not isinstance(checkpoint, Mapping):
            return {}
        channel_values = checkpoint.get("channel_values", {}) or {}
        if not isinstance(channel_values, Mapping):
            return {}
        return serialize_channel_values_for_api(channel_values)

    return launch


class ConnectionInboundResolver:
    """Make the persisted connection the sole project/owner source for IM work."""

    def __init__(
        self,
        *,
        repository: ConnectionInboundRepository,
        session_factory: async_sessionmaker[AsyncSession],
        thread_service: PrivateThreadCreator,
        request_id_factory: Callable[[], str] | None = None,
        thread_id_factory: Callable[[], uuid.UUID] | None = None,
    ) -> None:
        self._repository = repository
        self._session_factory = session_factory
        self._thread_service = thread_service
        self._request_id_factory = request_id_factory or (lambda: uuid.uuid4().hex)
        self._thread_id_factory = thread_id_factory or uuid.uuid4

    async def resolve(
        self,
        provider_identity: ProviderIdentity,
    ) -> ResolvedInboundPrivateWork:
        request_id = self._request_id_factory()
        self._require_valid_identity(provider_identity, request_id)
        connection = await self._repository.find_connection_by_external_identity(
            provider=provider_identity.provider,
            external_account_id=provider_identity.external_account_id,
            workspace_id=provider_identity.workspace_id,
        )
        account_id, project_id, owner_user_id, connection_id = self._connection_coordinates(
            connection,
            request_id,
        )
        context = await self._resolve_context(
            project_id=project_id,
            owner_user_id=owner_user_id,
            request_id=request_id,
        )
        scope = context.resource_scope
        thread_id = await self._repository.get_thread_id(
            scope=scope,
            connection_id=connection_id,
            external_conversation_id=provider_identity.external_conversation_id,
            external_topic_id=provider_identity.external_topic_id,
        )
        if thread_id is not None:
            if not isinstance(thread_id, str) or not thread_id:
                raise PrivateWorkInvalid(request_id)
            return ResolvedInboundPrivateWork(
                account_id=account_id,
                context=context,
                connection_id=connection_id,
                thread_id=thread_id,
                created=False,
            )

        agent = self._connection_agent(connection, request_id)
        thread_id = self._new_thread_id(request_id)
        await self._thread_service.create(
            context,
            thread_id=thread_id,
            agent=agent,
            metadata={
                "source": "channel",
                "channel_name": provider_identity.provider,
                "external_conversation_id": provider_identity.external_conversation_id,
                "external_topic_id": provider_identity.external_topic_id,
            },
        )
        await self._repository.set_thread_id(
            scope=scope,
            connection_id=connection_id,
            provider=provider_identity.provider,
            external_conversation_id=provider_identity.external_conversation_id,
            external_topic_id=provider_identity.external_topic_id,
            thread_id=thread_id,
        )
        return ResolvedInboundPrivateWork(
            account_id=account_id,
            context=context,
            connection_id=connection_id,
            thread_id=thread_id,
            created=True,
        )

    @staticmethod
    def _require_valid_identity(
        provider_identity: ProviderIdentity,
        request_id: str,
    ) -> None:
        if type(provider_identity) is not ProviderIdentity:
            raise PrivateWorkInvalid(request_id)
        if not provider_identity.provider or not provider_identity.external_account_id or not provider_identity.external_conversation_id:
            raise PrivateWorkInvalid(request_id)

    @staticmethod
    def _connection_coordinates(
        connection: Mapping[str, Any] | None,
        request_id: str,
    ) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, str]:
        if connection is None or connection.get("status") != "connected":
            raise PrivateWorkNotFound(request_id)
        connection_id = connection.get("id")
        if not isinstance(connection_id, str) or not connection_id:
            raise PrivateWorkNotFound(request_id)
        try:
            account_id = uuid.UUID(str(connection["account_id"]))
            project_id = uuid.UUID(str(connection["project_id"]))
            owner_user_id = uuid.UUID(str(connection["owner_user_id"]))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise PrivateWorkNotFound(request_id) from None
        if account_id != owner_user_id:
            raise PrivateWorkNotFound(request_id)
        return account_id, project_id, owner_user_id, connection_id

    async def _resolve_context(
        self,
        *,
        project_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        request_id: str,
    ) -> PrivateWorkContext:
        try:
            async with self._session_factory() as session:
                project_context = await resolve_project_context(
                    session,
                    owner_user_id,
                    project_id,
                    request_id,
                )
        except ProjectNotFound:
            raise PrivateWorkNotFound(request_id) from None
        except ProjectDatabaseUnavailable:
            raise PrivateWorkUnavailable(request_id) from None
        return PrivateWorkContext.from_project(project_context)

    @staticmethod
    def _connection_agent(
        connection: Mapping[str, Any],
        request_id: str,
    ) -> ThreadAgentRef:
        metadata = connection.get("metadata")
        if not isinstance(metadata, Mapping):
            raise PrivateWorkInvalid(request_id)
        agent_scope = metadata.get("agent_scope")
        if agent_scope not in {"project", "system"}:
            raise PrivateWorkInvalid(request_id)
        try:
            agent_asset_id = uuid.UUID(str(metadata["agent_asset_id"]))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise PrivateWorkInvalid(request_id) from None
        return ThreadAgentRef(asset_id=agent_asset_id, scope=agent_scope)

    def _new_thread_id(self, request_id: str) -> str:
        try:
            return str(uuid.UUID(str(self._thread_id_factory())))
        except (TypeError, ValueError, AttributeError):
            raise PrivateWorkInvalid(request_id) from None
