from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from app.project_channels.credential_store import ProjectChannelCredentialStore
from app.project_channels.errors import (
    ChannelInstanceConflict,
    ChannelInstanceForbidden,
    ChannelInstanceIdentityConflict,
    ChannelInstanceNotFound,
    ChannelInstanceStorageUnavailable,
    ChannelInstanceValidationFailed,
    ProjectChannelError,
)
from app.project_channels.models import (
    ConfigureProjectChannelInstance,
    ProjectChannelInstanceView,
)
from app.project_channels.providers import (
    CHANNEL_PROVIDER_SPECS,
    ChannelProviderSpec,
    validate_channel_configuration,
)
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.shared_assets.credential_repository import CredentialRepository
from app.shared_assets.errors import AssetForbidden, AssetNotFound, AssetStorageUnavailable
from deerflow.persistence.channel_connections.model import ChannelConnectionRow
from deerflow.persistence.channel_connections.project_instance_repository import (
    ProjectChannelInstanceConflict,
    ProjectChannelInstanceNotFound,
    ProjectChannelInstanceRepository,
)

_IDENTITY_CONSTRAINTS = {
    "uq_project_channel_instances_live_identity",
}
_CONFLICT_CONSTRAINTS = {
    "uq_project_channel_instances_live_provider",
    "uq_project_channel_credential_bindings_active_instance",
    "uq_credentials_project_name",
    "uq_credential_versions_asset_number",
}
_LAST_ERROR_MESSAGES = {
    "channel_credentials_unavailable": "Channel credentials could not be loaded.",
    "channel_provider_start_failed": "The channel could not connect. Check the application credentials and permissions.",
    "channel_lease_lost": "The channel is reconnecting on another runtime.",
}


def _constraint_name(exc: BaseException) -> str | None:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        value = getattr(current, "constraint_name", None)
        if isinstance(value, str):
            return value
        current = getattr(current, "orig", None) or getattr(
            current,
            "__cause__",
            None,
        )
    return None


class ProjectChannelInstanceService:
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        repository: ProjectChannelInstanceRepository | Any | None = None,
        credential_repository_factory: Callable[[AsyncSession], CredentialRepository] | None = None,
        credential_store_factory: Callable[[CredentialRepository], ProjectChannelCredentialStore] | None = None,
        runtime_coordinator: Any | None = None,
        audit: Any | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._repository = repository or ProjectChannelInstanceRepository()
        self._credential_repository_factory = credential_repository_factory or CredentialRepository
        self._credential_store_factory = credential_store_factory or ProjectChannelCredentialStore
        self._runtime = runtime_coordinator
        self._audit = audit

    async def _record_update(
        self,
        session: AsyncSession,
        context: ProjectContext,
    ) -> None:
        if self._audit is not None:
            await self._audit.project_updated(session, context)

    @staticmethod
    def _require_manage(context: ProjectContext) -> None:
        if Capability.PROJECT_CHANNELS_MANAGE not in context.capabilities:
            raise ChannelInstanceForbidden(context.request_id)

    @staticmethod
    def _provider(
        context: ProjectContext,
        provider: str,
    ) -> ChannelProviderSpec:
        spec = CHANNEL_PROVIDER_SPECS.get(provider)
        if spec is None:
            raise ChannelInstanceValidationFailed(
                context.request_id,
                "This channel provider is not supported.",
                fields=("provider",),
            )
        return spec

    @staticmethod
    def _identity_digest(
        context: ProjectContext,
        spec: ChannelProviderSpec,
        public_config: dict[str, str],
    ) -> str:
        identity = {field: public_config[field] for field in spec.required_public_fields if field in public_config}
        if not identity:
            identity = {"project_id": str(context.project_id)}
        canonical = json.dumps(
            {"provider": spec.provider, "identity": identity},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    async def _freeze_connections(
        session: AsyncSession,
        instance_id: uuid.UUID,
    ) -> None:
        await session.execute(
            update(ChannelConnectionRow)
            .where(
                ChannelConnectionRow.channel_instance_id == instance_id,
                ChannelConnectionRow.status == "connected",
            )
            .values(status="frozen", frozen_at=datetime.now(UTC))
        )

    async def list(
        self,
        context: ProjectContext,
    ) -> tuple[ProjectChannelInstanceView, ...]:
        async def operation(session: AsyncSession):
            credentials = self._credential_repository_factory(session)
            await credentials.lock_project(context)
            rows = await self._repository.list_project_instances(
                session,
                context.project_id,
            )
            result: dict[str, ProjectChannelInstanceView] = {}
            for row in rows:
                binding = await self._repository.get_credential_binding(
                    session,
                    row.id,
                    project_id=context.project_id,
                )
                result[row.provider] = self._view(
                    row,
                    credential_configured=binding is not None,
                )
            return tuple(result.get(provider) or self._unconfigured(spec) for provider, spec in CHANNEL_PROVIDER_SPECS.items())

        return await self._execute(context, operation)

    async def configure(
        self,
        context: ProjectContext,
        provider: str,
        command: ConfigureProjectChannelInstance,
    ) -> ProjectChannelInstanceView:
        self._require_manage(context)
        spec = self._provider(context, provider)
        if not isinstance(command, ConfigureProjectChannelInstance):
            raise ChannelInstanceValidationFailed(
                context.request_id,
                "Channel configuration is invalid.",
            )
        display_name = (command.display_name or spec.display_name).strip()
        if not display_name or len(display_name) > 120:
            raise ChannelInstanceValidationFailed(
                context.request_id,
                "Channel display name is invalid.",
                fields=("display_name",),
            )

        async def operation(session: AsyncSession):
            credential_repository = self._credential_repository_factory(session)
            await credential_repository.lock_project(context)
            instance = await self._repository.get_project_provider_instance(
                session,
                project_id=context.project_id,
                provider=provider,
                for_update=True,
            )
            binding = None
            if instance is not None:
                binding = await self._repository.get_credential_binding(
                    session,
                    instance.id,
                    project_id=context.project_id,
                    for_update=True,
                )
            normalized = validate_channel_configuration(
                provider,
                public_config=command.public_config,
                credentials=command.credentials,
                has_existing_credential=binding is not None,
                request_id=context.request_id,
            )
            digest = self._identity_digest(
                context,
                spec,
                normalized.public_config,
            )
            identity_changed = instance is not None and instance.provider_identity_digest != digest
            if instance is None:
                instance = await self._repository.create_instance(
                    session,
                    project_id=context.project_id,
                    provider=provider,
                    display_name=display_name,
                    public_config=normalized.public_config,
                    provider_identity_digest=digest,
                    actor_user_id=str(context.user_id),
                    desired_status=("enabled" if command.enabled else "disabled"),
                )
            else:
                instance = await self._repository.update_instance(
                    session,
                    project_id=context.project_id,
                    channel_instance_id=instance.id,
                    expected_revision=instance.revision,
                    actor_user_id=str(context.user_id),
                    display_name=display_name,
                    public_config=normalized.public_config,
                    provider_identity_digest=digest,
                    desired_status=("enabled" if command.enabled else "disabled"),
                )
            if identity_changed:
                await self._freeze_connections(session, instance.id)

            if normalized.credential_payload is not None:
                store = self._credential_store_factory(credential_repository)
                if binding is None:
                    credential_ref = await store.create(
                        context,
                        instance_id=instance.id,
                        provider=provider,
                        display_name=display_name,
                        payload=normalized.credential_payload,
                    )
                else:
                    credential_ref = await store.rotate(
                        context,
                        credential_id=binding.credential_id,
                        provider=provider,
                        payload=normalized.credential_payload,
                    )
                binding = await self._repository.replace_credential_binding(
                    session,
                    project_id=context.project_id,
                    channel_instance_id=instance.id,
                    credential_id=credential_ref.credential_id,
                    credential_version_id=credential_ref.credential_version_id,
                    actor_user_id=str(context.user_id),
                )
            await self._repository.set_observed_status(
                session,
                channel_instance_id=instance.id,
                observed_status=("starting" if command.enabled else "stopped"),
                last_error_code=None,
                expected_revision=instance.revision,
            )
            await self._record_update(session, context)
            return instance, binding is not None

        instance, has_binding = await self._execute(context, operation)
        await self._reconcile(instance.id)
        return self._view(instance, credential_configured=has_binding)

    async def set_enabled(
        self,
        context: ProjectContext,
        provider: str,
        enabled: bool,
    ) -> ProjectChannelInstanceView:
        self._require_manage(context)
        self._provider(context, provider)

        async def operation(session: AsyncSession):
            credential_repository = self._credential_repository_factory(session)
            await credential_repository.lock_project(context)
            instance = await self._repository.get_project_provider_instance(
                session,
                project_id=context.project_id,
                provider=provider,
                for_update=True,
            )
            if instance is None:
                raise ChannelInstanceNotFound(context.request_id)
            binding = await self._repository.get_credential_binding(
                session,
                instance.id,
                project_id=context.project_id,
                for_update=True,
            )
            if enabled and binding is None:
                raise ChannelInstanceValidationFailed(
                    context.request_id,
                    f"{CHANNEL_PROVIDER_SPECS[provider].display_name} credentials are required before enabling.",
                    fields=("credentials",),
                )
            instance = await self._repository.update_instance(
                session,
                project_id=context.project_id,
                channel_instance_id=instance.id,
                expected_revision=instance.revision,
                actor_user_id=str(context.user_id),
                desired_status=("enabled" if enabled else "disabled"),
            )
            await self._repository.set_observed_status(
                session,
                channel_instance_id=instance.id,
                observed_status=("starting" if enabled else "stopping"),
                last_error_code=None,
                expected_revision=instance.revision,
            )
            await self._record_update(session, context)
            return instance, binding is not None

        instance, has_binding = await self._execute(context, operation)
        await self._reconcile(instance.id)
        return self._view(instance, credential_configured=has_binding)

    async def delete(
        self,
        context: ProjectContext,
        provider: str,
    ) -> None:
        self._require_manage(context)
        self._provider(context, provider)

        async def operation(session: AsyncSession):
            credential_repository = self._credential_repository_factory(session)
            await credential_repository.lock_project(context)
            instance = await self._repository.get_project_provider_instance(
                session,
                project_id=context.project_id,
                provider=provider,
                for_update=True,
            )
            if instance is None:
                raise ChannelInstanceNotFound(context.request_id)
            binding = await self._repository.get_credential_binding(
                session,
                instance.id,
                project_id=context.project_id,
                for_update=True,
            )
            await self._repository.revoke_credential_binding(
                session,
                project_id=context.project_id,
                channel_instance_id=instance.id,
                actor_user_id=str(context.user_id),
            )
            if binding is not None:
                await self._credential_store_factory(credential_repository).revoke(
                    context,
                    credential_id=binding.credential_id,
                    provider=provider,
                )
            await self._repository.soft_delete_instance(
                session,
                project_id=context.project_id,
                channel_instance_id=instance.id,
                expected_revision=instance.revision,
                actor_user_id=str(context.user_id),
            )
            await self._freeze_connections(session, instance.id)
            await self._record_update(session, context)
            return instance.id

        instance_id = await self._execute(context, operation)
        if self._runtime is not None:
            remove = getattr(self._runtime, "remove", None)
            if remove is not None:
                await remove(instance_id)

    async def _reconcile(self, instance_id: uuid.UUID) -> None:
        if self._runtime is None:
            return
        reconcile = getattr(self._runtime, "reconcile", None)
        if reconcile is not None:
            await reconcile(instance_id)

    async def _execute(self, context: ProjectContext, operation):
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    return await operation(session)
        except ProjectChannelError:
            raise
        except ProjectChannelInstanceNotFound:
            raise ChannelInstanceNotFound(context.request_id) from None
        except ProjectChannelInstanceConflict:
            raise ChannelInstanceConflict(context.request_id) from None
        except (AssetForbidden,):
            raise ChannelInstanceForbidden(context.request_id) from None
        except AssetNotFound:
            raise ChannelInstanceNotFound(context.request_id) from None
        except AssetStorageUnavailable:
            raise ChannelInstanceStorageUnavailable(context.request_id) from None
        except IntegrityError as exc:
            constraint = _constraint_name(exc)
            if constraint in _IDENTITY_CONSTRAINTS:
                raise ChannelInstanceIdentityConflict(context.request_id) from None
            if constraint in _CONFLICT_CONSTRAINTS:
                raise ChannelInstanceConflict(context.request_id) from None
            raise ChannelInstanceStorageUnavailable(context.request_id) from None
        except (DBAPIError, SATimeoutError):
            raise ChannelInstanceStorageUnavailable(context.request_id) from None

    @staticmethod
    def _unconfigured(spec: ChannelProviderSpec) -> ProjectChannelInstanceView:
        return ProjectChannelInstanceView(
            id=None,
            provider=spec.provider,
            display_name=spec.display_name,
            status="unconfigured",
            enabled=False,
            configured=False,
            credential_configured=False,
            public_config={},
            updated_at=None,
            last_error=None,
        )

    @staticmethod
    def _view(
        row: Any,
        *,
        credential_configured: bool,
    ) -> ProjectChannelInstanceView:
        enabled = row.desired_status == "enabled"
        if not enabled:
            status_value = "disabled"
        elif row.observed_status in {"stopped", "starting", "running", "error"}:
            status_value = row.observed_status
        else:
            status_value = "starting"
        last_error = None
        if row.last_error_code:
            last_error = _LAST_ERROR_MESSAGES.get(
                row.last_error_code,
                "The channel is unavailable. Check the configuration and try again.",
            )
        return ProjectChannelInstanceView(
            id=row.id,
            provider=row.provider,
            display_name=row.display_name,
            status=status_value,
            enabled=enabled,
            configured=credential_configured,
            credential_configured=credential_configured,
            public_config=dict(row.public_config),
            updated_at=row.updated_at,
            last_error=last_error,
        )


__all__ = ["ProjectChannelInstanceService"]
