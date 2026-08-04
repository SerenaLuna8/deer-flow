from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from app.channel_group_bindings.agent_validation import (
    GroupBindingAgentValidator,
    ProjectGroupBindingAgentValidator,
)
from app.channel_group_bindings.errors import (
    GroupBindingAgentUnavailable,
    GroupBindingConflict,
    GroupBindingForbidden,
    GroupBindingInvalid,
    GroupBindingNotFound,
    GroupBindingUnavailable,
    ProjectChannelGroupBindingError,
)
from app.channel_group_bindings.identity import (
    AuditChannelGroupIdentityHasher,
    ChannelGroupIdentityHasher,
)
from app.channel_group_bindings.models import (
    CreateGroupBindingChallenge,
    GroupBindingChallengeView,
    ProjectChannelGroupBindingView,
    UpdateGroupBinding,
)
from app.channel_group_bindings.repository import (
    GroupBindingRepositoryAgentUnavailable,
    GroupBindingRepositoryConflict,
    GroupBindingRepositoryNotFound,
    PostgresProjectChannelGroupBindingRepository,
)
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.reliability.owner_refs import AuditHmacKeyringInvalid

_PROVIDER = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_CODE = re.compile(r"[A-Za-z0-9_-]{16,128}")
_DEFAULT_CHALLENGE_TTL_SECONDS = 10 * 60
_MAX_BIGINT = 9_223_372_036_854_775_807


class ProjectChannelGroupBindingService:
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        repository: Any | None = None,
        agent_validator: GroupBindingAgentValidator | None = None,
        identity_hasher: ChannelGroupIdentityHasher | None = None,
        clock: Callable[[], datetime] | None = None,
        code_factory: Callable[[], str] | None = None,
        challenge_ttl_seconds: int = _DEFAULT_CHALLENGE_TTL_SECONDS,
    ) -> None:
        if not 60 <= challenge_ttl_seconds <= 3600:
            raise ValueError("group binding challenge TTL must be 60..3600 seconds")
        self._session_factory = session_factory
        self._repository = repository or PostgresProjectChannelGroupBindingRepository()
        self._agent_validator = agent_validator or ProjectGroupBindingAgentValidator(session_factory)
        self._identity_hasher = identity_hasher or AuditChannelGroupIdentityHasher()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._code_factory = code_factory or (lambda: secrets.token_urlsafe(32))
        self._challenge_ttl_seconds = challenge_ttl_seconds

    @staticmethod
    def _require_manage(context: ProjectContext) -> None:
        if not isinstance(context, ProjectContext) or Capability.PROJECT_CHANNELS_MANAGE not in context.capabilities:
            raise GroupBindingForbidden(getattr(context, "request_id", "unknown"))

    @staticmethod
    def _provider(value: object, request_id: str) -> str:
        if type(value) is not str or _PROVIDER.fullmatch(value) is None:
            raise GroupBindingInvalid(
                request_id,
                "Channel provider is invalid.",
                fields=("provider",),
            )
        return value

    @staticmethod
    def _agent(
        asset_id: object,
        scope: object,
        request_id: str,
    ) -> tuple[uuid.UUID, str]:
        if type(asset_id) is not uuid.UUID or scope not in {"project", "system"}:
            raise GroupBindingInvalid(
                request_id,
                "Agent selection is invalid.",
                fields=("agent_asset_id", "agent_scope"),
            )
        return asset_id, str(scope)

    @staticmethod
    def _binding_id(value: object, request_id: str) -> uuid.UUID:
        if type(value) is not uuid.UUID:
            raise GroupBindingInvalid(request_id)
        return value

    @staticmethod
    def _instance_id(value: object, request_id: str) -> uuid.UUID:
        try:
            return value if type(value) is uuid.UUID else uuid.UUID(str(value))
        except (AttributeError, TypeError, ValueError):
            raise GroupBindingNotFound(request_id) from None

    @staticmethod
    def _revision(value: object, request_id: str) -> int:
        if type(value) is not int or not 1 <= value <= _MAX_BIGINT:
            raise GroupBindingInvalid(
                request_id,
                "Expected revision is invalid.",
                fields=("expected_revision",),
            )
        return value

    @staticmethod
    def _refs(
        hasher: ChannelGroupIdentityHasher,
        method_name: str,
        fallback_name: str,
        provider: str,
        channel_instance_id: uuid.UUID | str,
        external_id: str,
    ) -> tuple[str, ...]:
        method = getattr(hasher, method_name, None)
        if method is not None:
            values = method(provider, channel_instance_id, external_id)
        else:
            values = (
                getattr(hasher, fallback_name)(
                    provider,
                    channel_instance_id,
                    external_id,
                ),
            )
        if not isinstance(values, tuple) or not values or any(type(value) is not str or len(value) != 64 or re.fullmatch(r"[0-9a-f]{64}", value) is None for value in values):
            raise ValueError("channel identity references are invalid")
        return tuple(dict.fromkeys(values))

    @staticmethod
    def _view(row: object) -> ProjectChannelGroupBindingView:
        display_name = getattr(row, "external_group_name", None)
        if not isinstance(display_name, str) or not display_name:
            display_name = getattr(row, "display_name", None)
        if not isinstance(display_name, str) or not display_name:
            display_name = "Connected group"
        return ProjectChannelGroupBindingView(
            id=row.id,
            provider=row.provider,
            display_name=display_name,
            status=row.status,
            agent_asset_id=row.agent_asset_id,
            agent_scope=row.agent_scope,
            last_activity_at=row.last_activity_at,
            revision=row.revision,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def list(
        self,
        context: ProjectContext,
    ) -> tuple[ProjectChannelGroupBindingView, ...]:
        self._require_manage(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._repository.lock_project_context(session, context, read=True)
                rows = await self._repository.list_bindings(
                    session,
                    project_id=context.project_id,
                )
                return tuple(self._view(row) for row in rows)
        except ProjectChannelGroupBindingError:
            raise
        except GroupBindingRepositoryNotFound:
            raise GroupBindingNotFound(context.request_id) from None
        except (DBAPIError, SATimeoutError):
            raise GroupBindingUnavailable(context.request_id) from None

    async def create_challenge(
        self,
        context: ProjectContext,
        command: CreateGroupBindingChallenge,
    ) -> GroupBindingChallengeView:
        self._require_manage(context)
        if not isinstance(command, CreateGroupBindingChallenge):
            raise GroupBindingInvalid(context.request_id)
        provider = self._provider(command.provider, context.request_id)
        agent_asset_id, agent_scope = self._agent(
            command.agent_asset_id,
            command.agent_scope,
            context.request_id,
        )
        now = self._clock()
        expires_at = now + timedelta(seconds=self._challenge_ttl_seconds)
        code = self._code_factory()
        if type(code) is not str or _CODE.fullmatch(code) is None:
            raise GroupBindingUnavailable(context.request_id)
        code_digest = hashlib.sha256(code.encode("ascii")).hexdigest()
        try:
            async with self._session_factory() as session, session.begin():
                await self._repository.lock_project_context(session, context, read=False)
                instance = await self._repository.get_runtime_instance(
                    session,
                    project_id=context.project_id,
                    provider=provider,
                    for_update=True,
                )
                if instance is None:
                    raise GroupBindingNotFound(context.request_id)
                if instance.desired_status != "enabled" or instance.observed_status != "running":
                    raise GroupBindingConflict(context.request_id)
                await self._agent_validator.validate(
                    session,
                    context,
                    agent_asset_id,
                    agent_scope,
                )
                await self._repository.create_challenge(
                    session,
                    project_id=context.project_id,
                    channel_instance_id=instance.id,
                    provider=provider,
                    code_digest=code_digest,
                    agent_asset_id=agent_asset_id,
                    agent_scope=agent_scope,
                    membership_id=context.membership_id,
                    membership_version=context.membership_version,
                    created_by_user_id=str(context.user_id),
                    expires_at=expires_at,
                    now=now,
                )
        except ProjectChannelGroupBindingError:
            raise
        except GroupBindingRepositoryNotFound:
            raise GroupBindingNotFound(context.request_id) from None
        except GroupBindingRepositoryConflict:
            raise GroupBindingConflict(context.request_id) from None
        except IntegrityError:
            raise GroupBindingConflict(context.request_id) from None
        except (DBAPIError, SATimeoutError):
            raise GroupBindingUnavailable(context.request_id) from None
        return GroupBindingChallengeView(
            provider=provider,
            code=code,
            command=f"/bind-project {code}",
            expires_at=expires_at,
            expires_in=self._challenge_ttl_seconds,
        )

    async def update(
        self,
        context: ProjectContext,
        binding_id: uuid.UUID,
        command: UpdateGroupBinding,
    ) -> ProjectChannelGroupBindingView:
        self._require_manage(context)
        binding_id = self._binding_id(binding_id, context.request_id)
        if not isinstance(command, UpdateGroupBinding):
            raise GroupBindingInvalid(context.request_id)
        expected_revision = self._revision(command.expected_revision, context.request_id)
        if command.enabled is None and command.agent_asset_id is None and command.agent_scope is None:
            raise GroupBindingInvalid(context.request_id)
        if (command.agent_asset_id is None) != (command.agent_scope is None):
            raise GroupBindingInvalid(
                context.request_id,
                "Agent ID and scope must be updated together.",
                fields=("agent_asset_id", "agent_scope"),
            )
        agent_asset_id: uuid.UUID | None = None
        agent_scope: str | None = None
        if command.agent_asset_id is not None:
            agent_asset_id, agent_scope = self._agent(
                command.agent_asset_id,
                command.agent_scope,
                context.request_id,
            )
        now = self._clock()
        try:
            async with self._session_factory() as session, session.begin():
                await self._repository.lock_project_context(session, context, read=False)
                if agent_asset_id is not None and agent_scope is not None:
                    await self._agent_validator.validate(
                        session,
                        context,
                        agent_asset_id,
                        agent_scope,
                    )
                row = await self._repository.update_binding(
                    session,
                    context,
                    binding_id=binding_id,
                    expected_revision=expected_revision,
                    enabled=command.enabled,
                    agent_asset_id=agent_asset_id,
                    agent_scope=agent_scope,
                    now=now,
                )
                return self._view(row)
        except ProjectChannelGroupBindingError:
            raise
        except GroupBindingRepositoryNotFound:
            raise GroupBindingNotFound(context.request_id) from None
        except GroupBindingRepositoryConflict:
            raise GroupBindingConflict(context.request_id) from None
        except IntegrityError:
            raise GroupBindingConflict(context.request_id) from None
        except (DBAPIError, SATimeoutError):
            raise GroupBindingUnavailable(context.request_id) from None

    async def delete(
        self,
        context: ProjectContext,
        binding_id: uuid.UUID,
        *,
        expected_revision: int,
    ) -> None:
        self._require_manage(context)
        binding_id = self._binding_id(binding_id, context.request_id)
        expected_revision = self._revision(expected_revision, context.request_id)
        try:
            async with self._session_factory() as session, session.begin():
                await self._repository.lock_project_context(session, context, read=False)
                await self._repository.delete_binding(
                    session,
                    context,
                    binding_id=binding_id,
                    expected_revision=expected_revision,
                    now=self._clock(),
                )
        except GroupBindingRepositoryNotFound:
            raise GroupBindingNotFound(context.request_id) from None
        except GroupBindingRepositoryConflict:
            raise GroupBindingConflict(context.request_id) from None
        except IntegrityError:
            raise GroupBindingConflict(context.request_id) from None
        except (DBAPIError, SATimeoutError):
            raise GroupBindingUnavailable(context.request_id) from None

    async def complete_challenge(
        self,
        provider: str,
        channel_instance_id: uuid.UUID,
        code: str,
        chat_id: str,
        sender_id: str,
        display_name: str | None = None,
    ) -> ProjectChannelGroupBindingView:
        request_id = "channel-group-bind"
        provider = self._provider(provider, request_id)
        channel_instance_id = self._instance_id(channel_instance_id, request_id)
        if type(code) is not str or _CODE.fullmatch(code) is None:
            raise GroupBindingNotFound(request_id)
        try:
            external_group_refs = self._refs(
                self._identity_hasher,
                "group_refs",
                "group_ref",
                provider,
                channel_instance_id,
                chat_id,
            )
            # Validate the sender using the same strict transient boundary even
            # though the first release deliberately does not associate it with
            # an ActWeave account or persist its identifier.
            self._refs(
                self._identity_hasher,
                "account_refs",
                "account_ref",
                provider,
                channel_instance_id,
                sender_id,
            )
        except AuditHmacKeyringInvalid:
            raise GroupBindingUnavailable(request_id) from None
        except ValueError:
            raise GroupBindingNotFound(request_id) from None
        normalized_name = (display_name or "Connected group").strip()
        if not normalized_name or len(normalized_name) > 120:
            normalized_name = "Connected group"
        try:
            async with self._session_factory() as session, session.begin():
                row = await self._repository.complete_challenge(
                    session,
                    provider=provider,
                    channel_instance_id=channel_instance_id,
                    code_digest=hashlib.sha256(code.encode("ascii")).hexdigest(),
                    external_group_ref=external_group_refs[0],
                    external_group_refs=external_group_refs,
                    display_name=normalized_name,
                    now=self._clock(),
                )
                if row is None:
                    raise GroupBindingNotFound(request_id)
                return self._view(row)
        except ProjectChannelGroupBindingError:
            raise
        except GroupBindingRepositoryConflict:
            raise GroupBindingConflict(request_id) from None
        except IntegrityError:
            raise GroupBindingConflict(request_id) from None
        except (DBAPIError, SATimeoutError):
            raise GroupBindingUnavailable(request_id) from None

    async def resolve_or_create_guest(
        self,
        provider: str,
        channel_instance_id: uuid.UUID | str,
        chat_id: str,
        sender_id: str,
        topic_id: str | None = None,
    ) -> dict[str, object]:
        request_id = "channel-group-inbound"
        provider = self._provider(provider, request_id)
        channel_instance_id = self._instance_id(channel_instance_id, request_id)
        try:
            external_group_refs = self._refs(
                self._identity_hasher,
                "group_refs",
                "group_ref",
                provider,
                channel_instance_id,
                chat_id,
            )
            external_account_refs = self._refs(
                self._identity_hasher,
                "account_refs",
                "account_ref",
                provider,
                channel_instance_id,
                sender_id,
            )
            external_topic_refs = (
                ()
                if topic_id is None
                else self._refs(
                    self._identity_hasher,
                    "topic_refs",
                    "topic_ref",
                    provider,
                    channel_instance_id,
                    topic_id,
                )
            )
        except AuditHmacKeyringInvalid:
            raise GroupBindingUnavailable(request_id) from None
        except ValueError:
            raise GroupBindingNotFound(request_id) from None
        try:
            async with self._session_factory() as session, session.begin():
                authority = await self._repository.resolve_or_create_guest(
                    session,
                    provider=provider,
                    channel_instance_id=channel_instance_id,
                    external_group_refs=external_group_refs,
                    external_account_refs=external_account_refs,
                    now=self._clock(),
                )
                if authority is None:
                    raise GroupBindingNotFound(request_id)
                resolved = dict(authority)
                conversation_ref = resolved.get("workspace_id")
                if type(conversation_ref) is not str or conversation_ref not in external_group_refs:
                    raise GroupBindingNotFound(request_id)
                resolved["resolved_conversation_id"] = conversation_ref
                if external_topic_refs:
                    generation = external_group_refs.index(conversation_ref)
                    if generation >= len(external_topic_refs):
                        raise GroupBindingUnavailable(request_id)
                    resolved["resolved_topic_id"] = external_topic_refs[generation]
                else:
                    resolved["resolved_topic_id"] = None
                return resolved
        except ProjectChannelGroupBindingError:
            raise
        except GroupBindingRepositoryAgentUnavailable:
            raise GroupBindingAgentUnavailable(request_id) from None
        except IntegrityError:
            raise GroupBindingConflict(request_id) from None
        except (DBAPIError, SATimeoutError):
            raise GroupBindingUnavailable(request_id) from None

    def pseudonymize_topic_aliases(
        self,
        *,
        provider: str,
        channel_instance_id: uuid.UUID | str,
        chat_id: str,
        resolved_conversation_id: str,
        topic_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Hash provider topic aliases with the bound group's retained key generation."""

        request_id = "channel-group-topic-alias"
        provider = self._provider(provider, request_id)
        channel_instance_id = self._instance_id(channel_instance_id, request_id)
        if not isinstance(topic_ids, tuple) or len(topic_ids) > 32:
            raise GroupBindingNotFound(request_id)
        try:
            group_refs = self._refs(
                self._identity_hasher,
                "group_refs",
                "group_ref",
                provider,
                channel_instance_id,
                chat_id,
            )
            if resolved_conversation_id not in group_refs:
                raise GroupBindingNotFound(request_id)
            generation = group_refs.index(resolved_conversation_id)
            aliases: list[str] = []
            for topic_id in topic_ids:
                topic_refs = self._refs(
                    self._identity_hasher,
                    "topic_refs",
                    "topic_ref",
                    provider,
                    channel_instance_id,
                    topic_id,
                )
                if generation >= len(topic_refs):
                    raise GroupBindingUnavailable(request_id)
                aliases.append(topic_refs[generation])
            return tuple(aliases)
        except ProjectChannelGroupBindingError:
            raise
        except AuditHmacKeyringInvalid:
            raise GroupBindingUnavailable(request_id) from None
        except ValueError:
            raise GroupBindingNotFound(request_id) from None


__all__ = ["ProjectChannelGroupBindingService"]
