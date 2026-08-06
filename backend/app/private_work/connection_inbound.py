"""Resolve provider identities into authoritative project-private conversations."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.channels.instance_authority import ChannelInstanceAuthorityGuard
from app.channels.instance_identity import (
    normalize_channel_instance_id,
    persisted_channel_instance_id,
)
from app.channels.message_bus import InboundMessage
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import (
    PrivateWorkConflict,
    PrivateWorkInvalid,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from app.private_work.http_runtime import start_private_run
from app.private_work.inbound_dedupe import (
    DuplicateInboundDelivery,
    PrivateRunInboundDelivery,
)
from app.private_work.run_admission import (
    PrivateRunAdmissionServerContext,
    PrivateRunInboundAuthority,
)
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
    channel_instance_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "channel_instance_id",
            normalize_channel_instance_id(
                self.provider,
                self.channel_instance_id,
            ),
        )


@dataclass(frozen=True, slots=True)
class ResolvedInboundPrivateWork:
    """Authoritative private-work destination for one inbound conversation."""

    account_id: uuid.UUID
    context: PrivateWorkContext
    connection_id: str
    thread_id: str
    created: bool
    authority: PrivateRunInboundAuthority


ProjectInboundState = dict[str, Any] | list[Any]


@dataclass(frozen=True, slots=True)
class ProjectInboundLaunchResult:
    state: ProjectInboundState
    disposition: Literal["admitted", "duplicate_delivery"] = "admitted"


ProjectInboundRunLauncher = Callable[
    [PrivateWorkContext, str, InboundMessage, PrivateRunInboundAuthority],
    Awaitable[ProjectInboundState | ProjectInboundLaunchResult],
]


@dataclass(frozen=True, slots=True)
class ProjectInboundDispatchResult:
    resolved: ResolvedInboundPrivateWork
    state: ProjectInboundState
    disposition: Literal["admitted", "duplicate_delivery"] = "admitted"


class ConnectionInboundRepository(Protocol):
    async def find_connection_by_external_identity(
        self,
        *,
        provider: str,
        channel_instance_id: str | None,
        external_account_id: str,
        workspace_id: str | None,
        expected_connection_id: str | None = None,
        expected_scope: PrivateResourceScope | None = None,
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
    ) -> bool: ...


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

    async def get(
        self,
        context: PrivateWorkContext,
        thread_id: str,
    ) -> object | None: ...

    async def is_initialized(
        self,
        context: PrivateWorkContext,
        thread_id: str,
    ) -> bool: ...


class PrivateWorkInboundResolver(Protocol):
    async def resolve(
        self,
        provider_identity: ProviderIdentity,
        *,
        expected_connection_id: str | None = None,
        expected_scope: PrivateResourceScope | None = None,
    ) -> ResolvedInboundPrivateWork: ...


class ProjectInboundDispatcher:
    """Resolve an IM message, then launch it with the resolved private scope."""

    def __init__(
        self,
        resolver: PrivateWorkInboundResolver,
        run_launcher: ProjectInboundRunLauncher,
        *,
        instance_authority_guard: ChannelInstanceAuthorityGuard | None = None,
    ) -> None:
        self._resolver = resolver
        self._run_launcher = run_launcher
        self._instance_authority_guard = instance_authority_guard

    async def dispatch(self, message: InboundMessage) -> ProjectInboundDispatchResult:
        guard = self._instance_authority_guard
        if guard is not None:
            await guard.require(
                message.channel_name,
                message.channel_instance_id,
            )
        provider_identity = ProviderIdentity(
            provider=message.channel_name,
            channel_instance_id=message.channel_instance_id,
            external_account_id=message.user_id,
            workspace_id=message.workspace_id,
            external_conversation_id=(message.resolved_conversation_id or message.chat_id),
            external_topic_id=(message.resolved_topic_id if message.resolved_conversation_id is not None else message.topic_id),
        )
        if message.connection_id is None:
            resolved = await self._resolver.resolve(provider_identity)
        else:
            resolved = await self._resolver.resolve(
                provider_identity,
                expected_connection_id=message.connection_id,
                expected_scope=message.private_scope,
            )
        if guard is not None:
            await guard.require(
                message.channel_name,
                message.channel_instance_id,
            )
        launched = await self._run_launcher(
            resolved.context,
            resolved.thread_id,
            message,
            resolved.authority,
        )
        if type(launched) is ProjectInboundLaunchResult:
            return ProjectInboundDispatchResult(
                resolved=resolved,
                state=launched.state,
                disposition=launched.disposition,
            )
        return ProjectInboundDispatchResult(
            resolved=resolved,
            state=launched,
        )


def build_gateway_project_run_launcher(
    *,
    app: Any,
    start_private_run_fn: Callable[..., Awaitable[Any]] | None = None,
) -> ProjectInboundRunLauncher:
    """Build the in-process Gateway launcher used by project-bound IM text."""

    from app.gateway.auth_disabled import AUTH_SOURCE_INTERNAL
    from app.gateway.deps import get_config
    from app.gateway.internal_auth import get_internal_user
    from app.private_work.checkpoint_state import (
        bind_scoped_checkpoint_state,
        checkpoint_config,
        snapshot_checkpoint_id,
    )
    from app.private_work.checkpointer import ProjectScopedCheckpointer
    from deerflow.runtime import serialize_channel_values_for_api

    private_start = start_private_run_fn or start_private_run

    async def launch(
        context: PrivateWorkContext,
        thread_id: str,
        message: InboundMessage,
        authority: PrivateRunInboundAuthority,
    ) -> ProjectInboundState | ProjectInboundLaunchResult:
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
                    "channel_instance_id": message.channel_instance_id,
                    "chat_id": authority.external_conversation_id,
                    "topic_id": authority.external_topic_id,
                }
            },
            config=None,
            context={
                "channel_name": message.channel_name,
                "channel_instance_id": message.channel_instance_id,
                "channel_user_id": authority.external_account_id,
            },
            multitask_strategy="reject",
        )
        provider_delivery_id = message.provider_delivery_id
        if not isinstance(provider_delivery_id, str) or not provider_delivery_id:
            raise PrivateWorkInvalid(context.request_id)
        try:
            inbound_delivery = PrivateRunInboundDelivery(
                provider_delivery_id,
            )
        except TypeError:
            raise PrivateWorkInvalid(context.request_id) from None
        try:
            record = await private_start(
                body,
                thread_id,
                request,
                context,
                server_context=PrivateRunAdmissionServerContext(
                    inbound_authority=authority,
                    inbound_delivery=inbound_delivery,
                ),
            )
        except DuplicateInboundDelivery:
            return ProjectInboundLaunchResult(
                state={},
                disposition="duplicate_delivery",
            )
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

        project_checkpointer = getattr(
            app.state,
            "project_scoped_checkpointer",
            None,
        )
        if not isinstance(project_checkpointer, ProjectScopedCheckpointer):
            raise PrivateWorkUnavailable(context.request_id)
        snapshot = await bind_scoped_checkpoint_state(
            project_checkpointer,
            context,
            get_config(),
            as_node="inbound_response",
        ).aget(checkpoint_config(thread_id))
        if snapshot_checkpoint_id(snapshot) is None:
            return ProjectInboundLaunchResult(state={})
        return ProjectInboundLaunchResult(
            state=serialize_channel_values_for_api(
                dict(snapshot.values or {}),
            ),
        )

    return launch


class ConnectionInboundResolver:
    """Make the persisted connection the sole project/owner source for IM work."""

    _THREAD_NAMESPACE = uuid.UUID("e9fd4d38-24b0-4e70-bec8-2abf563cb48d")
    _BINDING_WAIT_ATTEMPTS = 50
    _BINDING_WAIT_SECONDS = 0.02

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
        self._thread_id_factory = thread_id_factory

    async def resolve(
        self,
        provider_identity: ProviderIdentity,
        *,
        expected_connection_id: str | None = None,
        expected_scope: PrivateResourceScope | None = None,
    ) -> ResolvedInboundPrivateWork:
        request_id = self._request_id_factory()
        self._require_valid_identity(provider_identity, request_id)
        account_id, context, connection_id, connection, authority = await self._load_connection_authority(
            provider_identity,
            request_id=request_id,
            expected_connection_id=expected_connection_id,
            expected_scope=expected_scope,
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
                authority=authority,
            )

        # Re-read every mutable authority field immediately before Thread
        # creation. A concurrent disable, membership change, or exact-
        # connection replacement must fail before the side effect.
        account_id, context, connection_id, connection, authority = await self._load_connection_authority(
            provider_identity,
            request_id=request_id,
            expected_connection_id=(expected_connection_id or connection_id),
            expected_scope=expected_scope,
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
                authority=authority,
            )

        agent = self._connection_agent(connection, request_id)
        thread_metadata = self._thread_metadata(provider_identity)
        thread_id = self._new_thread_id(
            request_id,
            scope=scope,
            connection_id=connection_id,
            provider_identity=provider_identity,
            agent=agent,
        )
        try:
            await self._thread_service.create(
                context,
                thread_id=thread_id,
                agent=agent,
                metadata=thread_metadata,
            )
        except PrivateWorkConflict:
            for _ in range(self._BINDING_WAIT_ATTEMPTS):
                existing_thread_id = await self._repository.get_thread_id(
                    scope=scope,
                    connection_id=connection_id,
                    external_conversation_id=provider_identity.external_conversation_id,
                    external_topic_id=provider_identity.external_topic_id,
                )
                if isinstance(existing_thread_id, str) and existing_thread_id:
                    return ResolvedInboundPrivateWork(
                        account_id=account_id,
                        context=context,
                        connection_id=connection_id,
                        thread_id=existing_thread_id,
                        created=False,
                        authority=authority,
                    )
                await asyncio.sleep(self._BINDING_WAIT_SECONDS)
            return await self._recover_deterministic_thread(
                provider_identity,
                request_id=request_id,
                expected_connection_id=connection_id,
                expected_scope=scope,
                expected_thread_id=thread_id,
            )

        bound = await self._repository.set_thread_id(
            scope=scope,
            connection_id=connection_id,
            provider=provider_identity.provider,
            external_conversation_id=provider_identity.external_conversation_id,
            external_topic_id=provider_identity.external_topic_id,
            thread_id=thread_id,
        )
        if bound is not True:
            raise PrivateWorkNotFound(request_id)
        return ResolvedInboundPrivateWork(
            account_id=account_id,
            context=context,
            connection_id=connection_id,
            thread_id=thread_id,
            created=True,
            authority=authority,
        )

    async def _recover_deterministic_thread(
        self,
        provider_identity: ProviderIdentity,
        *,
        request_id: str,
        expected_connection_id: str,
        expected_scope: PrivateResourceScope,
        expected_thread_id: str,
    ) -> ResolvedInboundPrivateWork:
        """Attach an exact orphan Thread left by a crash before mapping commit."""

        account_id, context, connection_id, connection, authority = await self._load_connection_authority(
            provider_identity,
            request_id=request_id,
            expected_connection_id=expected_connection_id,
            expected_scope=expected_scope,
        )
        scope = context.resource_scope
        agent = self._connection_agent(connection, request_id)
        deterministic_thread_id = self._new_thread_id(
            request_id,
            scope=scope,
            connection_id=connection_id,
            provider_identity=provider_identity,
            agent=agent,
        )
        if deterministic_thread_id != expected_thread_id:
            raise PrivateWorkUnavailable(request_id)
        record = await self._thread_service.get(context, expected_thread_id)
        if not self._thread_matches(
            record,
            context=context,
            thread_id=expected_thread_id,
            agent=agent,
            metadata=self._thread_metadata(provider_identity),
        ):
            raise PrivateWorkUnavailable(request_id)
        if (
            await self._thread_service.is_initialized(
                context,
                expected_thread_id,
            )
            is not True
        ):
            raise PrivateWorkUnavailable(request_id)
        bound = await self._repository.set_thread_id(
            scope=scope,
            connection_id=connection_id,
            provider=provider_identity.provider,
            external_conversation_id=provider_identity.external_conversation_id,
            external_topic_id=provider_identity.external_topic_id,
            thread_id=expected_thread_id,
        )
        if bound is not True:
            existing_thread_id = await self._repository.get_thread_id(
                scope=scope,
                connection_id=connection_id,
                external_conversation_id=provider_identity.external_conversation_id,
                external_topic_id=provider_identity.external_topic_id,
            )
            if existing_thread_id != expected_thread_id:
                # Distinguish revoked authority from transient storage conflict
                # without ever adopting a row from another connection/scope.
                await self._load_connection_authority(
                    provider_identity,
                    request_id=request_id,
                    expected_connection_id=connection_id,
                    expected_scope=scope,
                )
                raise PrivateWorkUnavailable(request_id)
        return ResolvedInboundPrivateWork(
            account_id=account_id,
            context=context,
            connection_id=connection_id,
            thread_id=expected_thread_id,
            created=False,
            authority=authority,
        )

    async def _load_connection_authority(
        self,
        provider_identity: ProviderIdentity,
        *,
        request_id: str,
        expected_connection_id: str | None,
        expected_scope: PrivateResourceScope | None,
    ) -> tuple[
        uuid.UUID,
        PrivateWorkContext,
        str,
        Mapping[str, Any],
        PrivateRunInboundAuthority,
    ]:
        lookup_kwargs: dict[str, Any] = {
            "provider": provider_identity.provider,
            "channel_instance_id": persisted_channel_instance_id(
                provider_identity.provider,
                provider_identity.channel_instance_id,
            ),
            "external_account_id": provider_identity.external_account_id,
            "workspace_id": provider_identity.workspace_id,
        }
        if expected_connection_id is not None:
            lookup_kwargs["expected_connection_id"] = expected_connection_id
        if expected_scope is not None:
            lookup_kwargs["expected_scope"] = expected_scope
        connection = await self._repository.find_connection_by_external_identity(
            **lookup_kwargs,
        )
        self._require_connection_instance(
            connection,
            provider_identity,
            request_id,
        )
        account_id, project_id, owner_user_id, connection_id = self._connection_coordinates(connection, request_id)
        if expected_connection_id is not None and connection_id != expected_connection_id:
            raise PrivateWorkNotFound(request_id)
        if expected_scope is not None and (str(project_id) != expected_scope.project_id or str(owner_user_id) != expected_scope.owner_user_id):
            raise PrivateWorkNotFound(request_id)
        external_account_id, workspace_id = self._connection_external_coordinates(connection, request_id)
        context = await self._resolve_context(
            project_id=project_id,
            owner_user_id=owner_user_id,
            request_id=request_id,
        )
        return (
            account_id,
            context,
            connection_id,
            connection,
            PrivateRunInboundAuthority(
                connection_id=connection_id,
                channel_instance_id=provider_identity.channel_instance_id,
                provider=provider_identity.provider,
                external_account_id=external_account_id,
                workspace_id=workspace_id,
                external_conversation_id=(provider_identity.external_conversation_id),
                external_topic_id=provider_identity.external_topic_id,
            ),
        )

    @staticmethod
    def _require_valid_identity(
        provider_identity: ProviderIdentity,
        request_id: str,
    ) -> None:
        if type(provider_identity) is not ProviderIdentity:
            raise PrivateWorkInvalid(request_id)
        if not provider_identity.provider or not provider_identity.channel_instance_id or not provider_identity.external_account_id or not provider_identity.external_conversation_id:
            raise PrivateWorkInvalid(request_id)

    @staticmethod
    def _require_connection_instance(
        connection: Mapping[str, Any] | None,
        provider_identity: ProviderIdentity,
        request_id: str,
    ) -> None:
        if connection is None:
            raise PrivateWorkNotFound(request_id)
        expected = persisted_channel_instance_id(
            provider_identity.provider,
            provider_identity.channel_instance_id,
        )
        actual = connection.get("channel_instance_id")
        if expected is None:
            if actual is not None:
                raise PrivateWorkNotFound(request_id)
            return
        try:
            if uuid.UUID(str(actual)) != uuid.UUID(expected):
                raise PrivateWorkNotFound(request_id)
        except (TypeError, ValueError, AttributeError):
            raise PrivateWorkNotFound(request_id) from None

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

    @staticmethod
    def _connection_external_coordinates(
        connection: Mapping[str, Any],
        request_id: str,
    ) -> tuple[str, str | None]:
        external_account_id = connection.get("external_account_id")
        workspace_id = connection.get("workspace_id")
        if not isinstance(external_account_id, str) or not external_account_id:
            raise PrivateWorkNotFound(request_id)
        if workspace_id is not None and not isinstance(workspace_id, str):
            raise PrivateWorkNotFound(request_id)
        return external_account_id, workspace_id

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

    @staticmethod
    def _thread_metadata(
        provider_identity: ProviderIdentity,
    ) -> dict[str, object]:
        return {
            "source": "channel",
            "channel_name": provider_identity.provider,
            "channel_instance_id": provider_identity.channel_instance_id,
            "external_conversation_id": provider_identity.external_conversation_id,
            "external_topic_id": provider_identity.external_topic_id,
        }

    @staticmethod
    def _thread_matches(
        record: object | None,
        *,
        context: PrivateWorkContext,
        thread_id: str,
        agent: ThreadAgentRef,
        metadata: Mapping[str, object],
    ) -> bool:
        if record is None:
            return False
        try:
            exact_scope = uuid.UUID(str(getattr(record, "project_id"))) == context.project_id and uuid.UUID(str(getattr(record, "owner_user_id"))) == context.user_id
        except (TypeError, ValueError, AttributeError):
            return False
        record_metadata = getattr(record, "metadata", None)
        return (
            exact_scope
            and getattr(record, "thread_id", None) == thread_id
            and getattr(record, "agent_asset_id", None) == agent.asset_id
            and getattr(record, "agent_scope", None) == agent.scope
            and isinstance(record_metadata, Mapping)
            and all(record_metadata.get(key) == value for key, value in metadata.items())
        )

    def _new_thread_id(
        self,
        request_id: str,
        *,
        scope: PrivateResourceScope,
        connection_id: str,
        provider_identity: ProviderIdentity,
        agent: ThreadAgentRef,
    ) -> str:
        try:
            if self._thread_id_factory is not None:
                return str(uuid.UUID(str(self._thread_id_factory())))
            coordinate = "\x00".join(
                (
                    scope.project_id,
                    scope.owner_user_id,
                    connection_id,
                    provider_identity.provider,
                    provider_identity.external_conversation_id,
                    provider_identity.external_topic_id or "",
                    agent.scope,
                    str(agent.asset_id),
                )
            )
            return str(uuid.uuid5(self._THREAD_NAMESPACE, coordinate))
        except (TypeError, ValueError, AttributeError):
            raise PrivateWorkInvalid(request_id) from None
