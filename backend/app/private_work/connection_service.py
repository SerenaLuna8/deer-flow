from __future__ import annotations

import secrets
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.private_work.context import (
    PrivateWorkContext,
    require_issued_private_work_context,
)
from app.private_work.errors import (
    PrivateWorkConflict,
    PrivateWorkError,
    PrivateWorkInvalid,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from app.private_work.revalidation import PrivateWorkRevalidator
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext, resolve_project_context_in_transaction
from app.projects.errors import ProjectDatabaseUnavailable, ProjectNotFound

_STATE_TTL_SECONDS = 10 * 60
_MAX_PENDING_STATES = 5


@dataclass(frozen=True, slots=True)
class ProjectConnectionChallenge:
    state: str
    code: str
    expires_at: datetime


ProjectContextResolver = Callable[..., Awaitable[ProjectContext]]


class ProjectConnectionService:
    """Project/owner-scoped application boundary for IM connections."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        repository: Any,
        revalidator: PrivateWorkRevalidator | None = None,
        context_resolver: ProjectContextResolver | None = None,
        clock: Callable[[], datetime] | None = None,
        state_factory: Callable[[], str] | None = None,
        state_ttl_seconds: int = _STATE_TTL_SECONDS,
        max_pending_states: int = _MAX_PENDING_STATES,
    ) -> None:
        self._session_factory = session_factory
        self._repository = repository
        self._revalidator = revalidator or PrivateWorkRevalidator()
        self._context_resolver = context_resolver or resolve_project_context_in_transaction
        self._clock = clock or (lambda: datetime.now(UTC))
        self._state_factory = state_factory or (lambda: secrets.token_urlsafe(32))
        self._state_ttl_seconds = state_ttl_seconds
        self._max_pending_states = max_pending_states

    async def _require(
        self,
        context: PrivateWorkContext,
        capability: Capability,
    ) -> PrivateWorkContext:
        context = require_issued_private_work_context(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(session, context, capability)
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
        return context

    @staticmethod
    def _required_text(value: object, request_id: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise PrivateWorkInvalid(request_id)
        return value.strip()

    @staticmethod
    def _canonical_uuid(value: object, request_id: str) -> uuid.UUID:
        try:
            return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        except (AttributeError, TypeError, ValueError):
            raise PrivateWorkInvalid(request_id) from None

    @staticmethod
    def _repository_error(request_id: str, exc: Exception) -> PrivateWorkError:
        if isinstance(exc, PrivateWorkError):
            return exc
        if isinstance(exc, (IntegrityError, TypeError, ValueError)):
            return PrivateWorkInvalid(request_id)
        return PrivateWorkUnavailable(request_id)

    async def begin_connect(
        self,
        context: PrivateWorkContext,
        provider: str,
        agent_asset_id: uuid.UUID | str,
        agent_scope: str = "project",
        redirect_after: str | None = None,
    ) -> ProjectConnectionChallenge:
        context = await self._require(context, Capability.PRIVATE_WORK_CREATE)
        provider = self._required_text(provider, context.request_id)
        agent_id = self._canonical_uuid(agent_asset_id, context.request_id)
        agent_scope = self._required_text(agent_scope, context.request_id)
        now = self._clock()
        expires_at = now + timedelta(seconds=self._state_ttl_seconds)
        state = self._state_factory()
        metadata = {
            "agent_asset_id": str(agent_id),
            "agent_scope": agent_scope,
            "membership_id": str(context.membership_id),
            "membership_version": context.membership_version,
            "request_id": context.request_id,
        }
        try:
            inserted = await self._repository.create_oauth_state_within_cap(
                scope=context.resource_scope,
                provider=provider,
                state=state,
                expires_at=expires_at,
                max_pending=self._max_pending_states,
                now=now,
                redirect_after=redirect_after,
                metadata=metadata,
            )
        except Exception as exc:
            raise self._repository_error(context.request_id, exc) from None
        if not inserted:
            raise PrivateWorkConflict(context.request_id)
        return ProjectConnectionChallenge(
            state=state,
            code=state,
            expires_at=expires_at,
        )

    async def _context_from_state(
        self,
        consumed: Mapping[str, object],
    ) -> tuple[PrivateWorkContext, Mapping[str, object]]:
        raw_metadata = consumed.get("metadata")
        metadata: Mapping[str, object] = raw_metadata if isinstance(raw_metadata, Mapping) else {}
        request_id_value = metadata.get("request_id")
        request_id = request_id_value if isinstance(request_id_value, str) and request_id_value else "connection-callback"
        project_id = self._canonical_uuid(consumed.get("project_id"), request_id)
        owner_user_id = self._canonical_uuid(consumed.get("owner_user_id"), request_id)
        expected_membership_id = self._canonical_uuid(metadata.get("membership_id"), request_id)
        expected_version = consumed.get("membership_version", metadata.get("membership_version"))
        if isinstance(expected_version, bool) or not isinstance(expected_version, int):
            raise PrivateWorkInvalid(request_id)

        try:
            async with self._session_factory() as session, session.begin():
                current = await self._context_resolver(
                    session,
                    owner_user_id,
                    project_id,
                    request_id,
                    lock=False,
                )
                if type(current) is not ProjectContext or current.user_id != owner_user_id or current.project_id != project_id or current.membership_id != expected_membership_id or current.membership_version != expected_version:
                    raise PrivateWorkNotFound(request_id)
                private_context = PrivateWorkContext.from_project(current)
                await self._revalidator.require(
                    session,
                    private_context,
                    Capability.PRIVATE_WORK_CREATE,
                )
        except PrivateWorkError:
            raise
        except ProjectNotFound:
            raise PrivateWorkNotFound(request_id) from None
        except ProjectDatabaseUnavailable:
            raise PrivateWorkUnavailable(request_id) from None
        except DBAPIError:
            raise PrivateWorkUnavailable(request_id) from None
        return private_context, metadata

    async def complete_callback(
        self,
        provider: str,
        state: str,
        external_account_id: str,
        workspace_id: str | None = None,
        **connection_fields: object,
    ) -> dict[str, Any]:
        request_id = "connection-callback"
        provider = self._required_text(provider, request_id)
        state = self._required_text(state, request_id)
        try:
            consumed = await self._repository.consume_oauth_state(
                provider=provider,
                state=state,
            )
        except Exception as exc:
            raise self._repository_error(request_id, exc) from None
        if not isinstance(consumed, Mapping):
            raise PrivateWorkNotFound(request_id)
        if consumed.get("provider") not in (None, provider):
            raise PrivateWorkNotFound(request_id)

        context, state_metadata = await self._context_from_state(consumed)
        raw_connection_metadata = connection_fields.pop("metadata", None)
        if raw_connection_metadata is None:
            connection_metadata: dict[str, object] = {}
        elif isinstance(raw_connection_metadata, Mapping):
            connection_metadata = dict(raw_connection_metadata)
        else:
            raise PrivateWorkInvalid(context.request_id)
        agent_asset_id = state_metadata.get("agent_asset_id")
        agent_scope = state_metadata.get("agent_scope")
        if not isinstance(agent_asset_id, str) or not isinstance(agent_scope, str):
            raise PrivateWorkInvalid(context.request_id)
        connection_metadata.update(
            agent_asset_id=agent_asset_id,
            agent_scope=agent_scope,
        )
        try:
            return await self._repository.upsert_connection(
                scope=context.resource_scope,
                provider=provider,
                external_account_id=external_account_id,
                workspace_id=workspace_id,
                metadata=connection_metadata,
                **connection_fields,
            )
        except Exception as exc:
            raise self._repository_error(context.request_id, exc) from None

    async def list(self, context: PrivateWorkContext) -> list[dict[str, Any]]:
        context = await self._require(context, Capability.PRIVATE_WORK_READ_OWN)
        try:
            return await self._repository.list_connections(context.resource_scope)
        except Exception as exc:
            raise self._repository_error(context.request_id, exc) from None

    async def disconnect(
        self,
        context: PrivateWorkContext,
        connection_id: str,
    ) -> None:
        context = await self._require(context, Capability.PRIVATE_WORK_CREATE)
        connection_id = self._required_text(connection_id, context.request_id)
        try:
            disconnected = await self._repository.disconnect_connection(
                scope=context.resource_scope,
                connection_id=connection_id,
            )
        except Exception as exc:
            raise self._repository_error(context.request_id, exc) from None
        if not disconnected:
            raise PrivateWorkNotFound(context.request_id)
