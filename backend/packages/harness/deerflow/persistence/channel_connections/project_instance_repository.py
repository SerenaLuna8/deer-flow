"""Transaction-scoped persistence for dynamically managed channel instances."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.channel_connections.model import (
    ProjectChannelCredentialBindingRow,
    ProjectChannelInstanceLeaseRow,
    ProjectChannelInstanceRow,
)
from deerflow.persistence.shared_assets import (
    CredentialEnvelopeRow,
    CredentialRow,
    CredentialVersionRow,
)

_PROVIDER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_DIGEST_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(^|_)(secret|token|password|api_key|private_key)($|_)",
    re.IGNORECASE,
)
_UNSET: Final[object] = object()


class ProjectChannelInstanceError(RuntimeError):
    """Stable base error without database or secret detail."""


class ProjectChannelInstanceNotFound(ProjectChannelInstanceError):
    pass


class ProjectChannelInstanceConflict(ProjectChannelInstanceError):
    pass


@dataclass(frozen=True, slots=True)
class ChannelInstanceLeaseClaim:
    project_id: uuid.UUID
    channel_instance_id: uuid.UUID
    holder_id: uuid.UUID
    lease_token: str
    fencing_generation: int
    lease_expires_at: datetime


class ProjectChannelInstanceRepository:
    """Repository whose caller owns the surrounding database transaction."""

    @staticmethod
    def _uuid(value: uuid.UUID | str, *, field: str) -> uuid.UUID:
        try:
            return value if isinstance(value, uuid.UUID) else uuid.UUID(value)
        except (TypeError, ValueError):
            raise ValueError(f"invalid {field}") from None

    @staticmethod
    def _now(value: datetime | None = None) -> datetime:
        result = value or datetime.now(UTC)
        if result.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return result

    @staticmethod
    def _ttl(ttl_seconds: int) -> timedelta:
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 300:
            raise ValueError("ttl_seconds must be between 1 and 300")
        return timedelta(seconds=ttl_seconds)

    @classmethod
    def validate_public_config(
        cls,
        public_config: dict[str, object],
    ) -> dict[str, object]:
        if type(public_config) is not dict:
            raise ValueError("public_config must be an object")

        def walk(value: object) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if type(key) is not str or _SENSITIVE_KEY_PATTERN.search(key):
                        raise ValueError("public_config contains a secret field")
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
            elif value is not None and type(value) not in {
                str,
                int,
                float,
                bool,
            }:
                raise ValueError("public_config contains a non-JSON value")

        walk(public_config)
        return public_config

    @staticmethod
    def validate_provider(provider: str) -> str:
        if _PROVIDER_PATTERN.fullmatch(provider) is None:
            raise ValueError("invalid channel provider")
        return provider

    @staticmethod
    def validate_identity_digest(value: str) -> str:
        if _DIGEST_PATTERN.fullmatch(value) is None:
            raise ValueError("invalid provider identity digest")
        return value

    @staticmethod
    def _validate_observation(
        observed_status: str,
        last_error_code: str | None,
    ) -> None:
        if observed_status not in {
            "stopped",
            "starting",
            "running",
            "stopping",
            "error",
        }:
            raise ValueError("invalid observed channel status")
        if observed_status == "error":
            if not last_error_code or len(last_error_code) > 64 or re.fullmatch(r"[a-z0-9_]+", last_error_code) is None:
                raise ValueError("invalid channel error code")
        elif last_error_code is not None:
            raise ValueError("last_error_code is only valid for error status")

    async def create_instance(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID | str,
        provider: str,
        display_name: str,
        public_config: dict[str, object],
        provider_identity_digest: str,
        actor_user_id: str,
        desired_status: str = "disabled",
        channel_instance_id: uuid.UUID | str | None = None,
    ) -> ProjectChannelInstanceRow:
        if not display_name.strip() or len(display_name) > 120:
            raise ValueError("invalid channel display name")
        if desired_status not in {"enabled", "disabled"}:
            raise ValueError("invalid desired channel status")
        instance = ProjectChannelInstanceRow(
            id=(uuid.uuid4() if channel_instance_id is None else self._uuid(channel_instance_id, field="channel_instance_id")),
            project_id=self._uuid(project_id, field="project_id"),
            provider=self.validate_provider(provider),
            display_name=display_name.strip(),
            desired_status=desired_status,
            observed_status="stopped",
            public_config=dict(self.validate_public_config(public_config)),
            provider_identity_digest=self.validate_identity_digest(provider_identity_digest),
            revision=1,
            created_by_user_id=actor_user_id,
            updated_by_user_id=actor_user_id,
        )
        session.add(instance)
        await session.flush()
        return instance

    async def get_instance(
        self,
        session: AsyncSession,
        channel_instance_id: uuid.UUID | str,
        *,
        project_id: uuid.UUID | str | None = None,
        for_update: bool = False,
        include_deleted: bool = False,
    ) -> ProjectChannelInstanceRow | None:
        statement = select(ProjectChannelInstanceRow).where(ProjectChannelInstanceRow.id == self._uuid(channel_instance_id, field="channel_instance_id"))
        if project_id is not None:
            statement = statement.where(ProjectChannelInstanceRow.project_id == self._uuid(project_id, field="project_id"))
        if not include_deleted:
            statement = statement.where(ProjectChannelInstanceRow.deleted_at.is_(None))
        if for_update:
            statement = statement.with_for_update()
        return (await session.execute(statement)).scalar_one_or_none()

    async def get_project_provider_instance(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID | str,
        provider: str,
        for_update: bool = False,
    ) -> ProjectChannelInstanceRow | None:
        statement = select(ProjectChannelInstanceRow).where(
            ProjectChannelInstanceRow.project_id == self._uuid(project_id, field="project_id"),
            ProjectChannelInstanceRow.provider == self.validate_provider(provider),
            ProjectChannelInstanceRow.deleted_at.is_(None),
        )
        if for_update:
            statement = statement.with_for_update()
        return (await session.execute(statement)).scalar_one_or_none()

    async def list_enabled_instances(
        self,
        session: AsyncSession,
    ) -> list[ProjectChannelInstanceRow]:
        result = await session.execute(
            select(ProjectChannelInstanceRow)
            .where(
                ProjectChannelInstanceRow.desired_status == "enabled",
                ProjectChannelInstanceRow.deleted_at.is_(None),
            )
            .order_by(
                ProjectChannelInstanceRow.project_id,
                ProjectChannelInstanceRow.provider,
                ProjectChannelInstanceRow.id,
            )
        )
        return list(result.scalars())

    async def list_project_instances(
        self,
        session: AsyncSession,
        project_id: uuid.UUID | str,
    ) -> list[ProjectChannelInstanceRow]:
        result = await session.execute(
            select(ProjectChannelInstanceRow)
            .where(
                ProjectChannelInstanceRow.project_id == self._uuid(project_id, field="project_id"),
                ProjectChannelInstanceRow.deleted_at.is_(None),
            )
            .order_by(
                ProjectChannelInstanceRow.provider,
                ProjectChannelInstanceRow.id,
            )
        )
        return list(result.scalars())

    async def update_instance(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID | str,
        channel_instance_id: uuid.UUID | str,
        expected_revision: int,
        actor_user_id: str,
        display_name: str | object = _UNSET,
        public_config: dict[str, object] | object = _UNSET,
        provider_identity_digest: str | object = _UNSET,
        desired_status: str | object = _UNSET,
    ) -> ProjectChannelInstanceRow:
        instance = await self.get_instance(
            session,
            channel_instance_id,
            project_id=project_id,
            for_update=True,
        )
        if instance is None:
            raise ProjectChannelInstanceNotFound()
        if instance.revision != expected_revision:
            raise ProjectChannelInstanceConflict()
        if display_name is not _UNSET:
            if type(display_name) is not str or not display_name.strip() or len(display_name) > 120:
                raise ValueError("invalid channel display name")
            instance.display_name = display_name.strip()
        if public_config is not _UNSET:
            if type(public_config) is not dict:
                raise ValueError("public_config must be an object")
            instance.public_config = dict(self.validate_public_config(public_config))
        if provider_identity_digest is not _UNSET:
            if type(provider_identity_digest) is not str:
                raise ValueError("invalid provider identity digest")
            instance.provider_identity_digest = self.validate_identity_digest(provider_identity_digest)
        if desired_status is not _UNSET:
            if desired_status not in {"enabled", "disabled"}:
                raise ValueError("invalid desired channel status")
            instance.desired_status = desired_status
        instance.updated_by_user_id = actor_user_id
        instance.revision += 1
        await session.flush()
        return instance

    async def soft_delete_instance(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID | str,
        channel_instance_id: uuid.UUID | str,
        expected_revision: int,
        actor_user_id: str,
        now: datetime | None = None,
    ) -> ProjectChannelInstanceRow:
        instance = await self.update_instance(
            session,
            project_id=project_id,
            channel_instance_id=channel_instance_id,
            expected_revision=expected_revision,
            actor_user_id=actor_user_id,
            desired_status="disabled",
        )
        instance.deleted_at = self._now(now)
        await session.flush()
        return instance

    async def set_observed_status(
        self,
        session: AsyncSession,
        *,
        channel_instance_id: uuid.UUID | str,
        observed_status: str,
        last_error_code: str | None,
        expected_revision: int | None = None,
    ) -> ProjectChannelInstanceRow:
        """Persist runtime observation without changing management revision.

        Management revision protects user-authored desired configuration. A
        Supervisor observation is orthogonal and therefore must not make an
        otherwise-current admin edit stale.
        """

        self._validate_observation(observed_status, last_error_code)
        instance = await self.get_instance(
            session,
            channel_instance_id,
            for_update=True,
            include_deleted=True,
        )
        if instance is None:
            raise ProjectChannelInstanceNotFound()
        if expected_revision is not None and instance.revision != expected_revision:
            raise ProjectChannelInstanceConflict()
        instance.observed_status = observed_status
        instance.last_error_code = last_error_code
        await session.flush()
        return instance

    async def set_observed_status_with_lease(
        self,
        session: AsyncSession,
        *,
        channel_instance_id: uuid.UUID | str,
        observed_status: str,
        last_error_code: str | None,
        holder_id: uuid.UUID | str,
        lease_token: str,
        fencing_generation: int,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> ProjectChannelInstanceRow | None:
        """Write one runtime observation only while the exact lease is live.

        ``stopped`` is allowed after an administrator disables or deletes an
        instance so the current owner can report completion. Every state that
        can imply a live adapter additionally requires the instance to remain
        enabled and non-deleted.
        """

        self._validate_observation(observed_status, last_error_code)
        timestamp = self._now(now)
        instance = await self.get_instance(
            session,
            channel_instance_id,
            for_update=True,
            include_deleted=True,
        )
        if instance is None:
            return None
        lease = (await session.execute(select(ProjectChannelInstanceLeaseRow).where(ProjectChannelInstanceLeaseRow.channel_instance_id == instance.id).with_for_update())).scalar_one_or_none()
        if not self._lease_matches(
            lease,
            holder_id=self._uuid(holder_id, field="holder_id"),
            lease_token=lease_token,
            fencing_generation=fencing_generation,
            now=timestamp,
        ):
            return None
        if expected_revision is not None and instance.revision != expected_revision:
            return None
        if observed_status != "stopped" and (instance.deleted_at is not None or instance.desired_status != "enabled"):
            return None
        instance.observed_status = observed_status
        instance.last_error_code = last_error_code
        await session.flush()
        return instance

    async def get_credential_binding(
        self,
        session: AsyncSession,
        channel_instance_id: uuid.UUID | str,
        *,
        project_id: uuid.UUID | str | None = None,
        for_update: bool = False,
    ) -> ProjectChannelCredentialBindingRow | None:
        statement = select(ProjectChannelCredentialBindingRow).where(
            ProjectChannelCredentialBindingRow.channel_instance_id == self._uuid(channel_instance_id, field="channel_instance_id"),
            ProjectChannelCredentialBindingRow.status == "active",
        )
        if project_id is not None:
            statement = statement.where(ProjectChannelCredentialBindingRow.project_id == self._uuid(project_id, field="project_id"))
        if for_update:
            statement = statement.with_for_update()
        return (await session.execute(statement)).scalar_one_or_none()

    async def replace_credential_binding(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID | str,
        channel_instance_id: uuid.UUID | str,
        credential_id: uuid.UUID | str,
        credential_version_id: uuid.UUID | str,
        actor_user_id: str,
        now: datetime | None = None,
    ) -> ProjectChannelCredentialBindingRow:
        project_uuid = self._uuid(project_id, field="project_id")
        instance = await self.get_instance(
            session,
            channel_instance_id,
            project_id=project_uuid,
            for_update=True,
        )
        if instance is None:
            raise ProjectChannelInstanceNotFound()
        current = await self.get_credential_binding(
            session,
            instance.id,
            project_id=project_uuid,
            for_update=True,
        )
        timestamp = self._now(now)
        next_revision = 1
        if current is not None:
            current.status = "revoked"
            current.revoked_at = timestamp
            current.revoked_by_user_id = actor_user_id
            next_revision = current.binding_revision + 1
            await session.flush()
        binding = ProjectChannelCredentialBindingRow(
            project_id=project_uuid,
            channel_instance_id=instance.id,
            credential_id=self._uuid(credential_id, field="credential_id"),
            credential_version_id=self._uuid(
                credential_version_id,
                field="credential_version_id",
            ),
            binding_revision=next_revision,
            status="active",
            created_by_user_id=actor_user_id,
        )
        session.add(binding)
        await session.flush()
        return binding

    async def revoke_credential_binding(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID | str,
        channel_instance_id: uuid.UUID | str,
        actor_user_id: str,
        now: datetime | None = None,
    ) -> bool:
        project_uuid = self._uuid(project_id, field="project_id")
        instance = await self.get_instance(
            session,
            channel_instance_id,
            project_id=project_uuid,
            for_update=True,
            include_deleted=True,
        )
        if instance is None:
            raise ProjectChannelInstanceNotFound()
        current = await self.get_credential_binding(
            session,
            instance.id,
            project_id=project_uuid,
            for_update=True,
        )
        if current is None:
            return False
        current.status = "revoked"
        current.revoked_at = self._now(now)
        current.revoked_by_user_id = actor_user_id
        await session.flush()
        return True

    async def claim_instance_lease(
        self,
        session: AsyncSession,
        channel_instance_id: uuid.UUID | str,
        holder_id: uuid.UUID | str,
        ttl_seconds: int,
        *,
        now: datetime | None = None,
    ) -> ChannelInstanceLeaseClaim | None:
        timestamp = self._now(now)
        expires_at = timestamp + self._ttl(ttl_seconds)
        holder_uuid = self._uuid(holder_id, field="holder_id")
        instance = await self.get_instance(
            session,
            channel_instance_id,
            for_update=True,
        )
        if instance is None or instance.desired_status != "enabled":
            return None
        lease = (await session.execute(select(ProjectChannelInstanceLeaseRow).where(ProjectChannelInstanceLeaseRow.channel_instance_id == instance.id).with_for_update())).scalar_one_or_none()
        if lease is not None and lease.lease_expires_at > timestamp:
            return None

        lease_token = secrets.token_urlsafe(32)
        lease_hash = self._token_hash(lease_token)
        if lease is None:
            lease = ProjectChannelInstanceLeaseRow(
                channel_instance_id=instance.id,
                project_id=instance.project_id,
                holder_id=holder_uuid,
                lease_token_hash=lease_hash,
                fencing_generation=1,
                lease_expires_at=expires_at,
                last_heartbeat_at=timestamp,
            )
            session.add(lease)
        else:
            lease.holder_id = holder_uuid
            lease.lease_token_hash = lease_hash
            lease.fencing_generation += 1
            lease.lease_expires_at = expires_at
            lease.last_heartbeat_at = timestamp
        await session.flush()
        return ChannelInstanceLeaseClaim(
            project_id=instance.project_id,
            channel_instance_id=instance.id,
            holder_id=holder_uuid,
            lease_token=lease_token,
            fencing_generation=lease.fencing_generation,
            lease_expires_at=expires_at,
        )

    async def is_instance_lease_authorized(
        self,
        session: AsyncSession,
        *,
        channel_instance_id: uuid.UUID | str,
        provider: str | None = None,
        holder_id: uuid.UUID | str,
        lease_token: str,
        fencing_generation: int,
        expected_revision: int | None = None,
        binding_revision: int | None = None,
        credential_version_id: uuid.UUID | str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Revalidate the exact live owner before an external side effect."""

        timestamp = self._now(now)
        instance = await self.get_instance(
            session,
            channel_instance_id,
            include_deleted=True,
        )
        if instance is None or instance.deleted_at is not None or instance.desired_status != "enabled" or (provider is not None and instance.provider != self.validate_provider(provider)):
            return False
        if expected_revision is not None and instance.revision != expected_revision:
            return False
        lease = (await session.execute(select(ProjectChannelInstanceLeaseRow).where(ProjectChannelInstanceLeaseRow.channel_instance_id == instance.id))).scalar_one_or_none()
        if not self._lease_matches(
            lease,
            holder_id=self._uuid(holder_id, field="holder_id"),
            lease_token=lease_token,
            fencing_generation=fencing_generation,
            now=timestamp,
        ):
            return False
        closure_values = (
            binding_revision,
            credential_version_id,
        )
        if any(value is not None for value in closure_values):
            if any(value is None for value in closure_values):
                raise ValueError("runtime closure fields must be provided together")
            assert binding_revision is not None
            assert credential_version_id is not None
            exact_version_id = self._uuid(
                credential_version_id,
                field="credential_version_id",
            )
            exact_binding = (
                await session.execute(
                    select(ProjectChannelCredentialBindingRow.id)
                    .join(
                        CredentialRow,
                        (CredentialRow.id == ProjectChannelCredentialBindingRow.credential_id)
                        & (CredentialRow.project_id == ProjectChannelCredentialBindingRow.project_id)
                        & (CredentialRow.scope == "project")
                        & (CredentialRow.status == "active")
                        & (CredentialRow.is_delete.is_(False)),
                    )
                    .join(
                        CredentialVersionRow,
                        (CredentialVersionRow.id == ProjectChannelCredentialBindingRow.credential_version_id) & (CredentialVersionRow.credential_id == CredentialRow.id) & (CredentialVersionRow.status == "active"),
                    )
                    .join(
                        CredentialEnvelopeRow,
                        (CredentialEnvelopeRow.credential_version_id == CredentialVersionRow.id) & (CredentialEnvelopeRow.is_active.is_(True)),
                    )
                    .where(
                        ProjectChannelCredentialBindingRow.project_id == instance.project_id,
                        ProjectChannelCredentialBindingRow.channel_instance_id == instance.id,
                        ProjectChannelCredentialBindingRow.status == "active",
                        ProjectChannelCredentialBindingRow.binding_revision == binding_revision,
                        ProjectChannelCredentialBindingRow.credential_version_id == exact_version_id,
                        CredentialRow.credential_type == f"channel.{instance.provider}",
                    )
                )
            ).scalar_one_or_none()
            if exact_binding is None:
                return False
        return True

    async def renew_instance_lease(
        self,
        session: AsyncSession,
        channel_instance_id: uuid.UUID | str,
        holder_id: uuid.UUID | str,
        lease_token: str,
        fencing_generation: int,
        ttl_seconds: int,
        *,
        now: datetime | None = None,
    ) -> ChannelInstanceLeaseClaim | None:
        timestamp = self._now(now)
        expires_at = timestamp + self._ttl(ttl_seconds)
        instance_uuid = self._uuid(
            channel_instance_id,
            field="channel_instance_id",
        )
        holder_uuid = self._uuid(holder_id, field="holder_id")
        instance = await self.get_instance(
            session,
            instance_uuid,
            for_update=True,
        )
        if instance is None or instance.desired_status != "enabled":
            return None
        lease = (await session.execute(select(ProjectChannelInstanceLeaseRow).where(ProjectChannelInstanceLeaseRow.channel_instance_id == instance_uuid).with_for_update())).scalar_one_or_none()
        if not self._lease_matches(
            lease,
            holder_id=holder_uuid,
            lease_token=lease_token,
            fencing_generation=fencing_generation,
            now=timestamp,
        ):
            return None
        assert lease is not None
        lease.lease_expires_at = expires_at
        lease.last_heartbeat_at = timestamp
        await session.flush()
        return ChannelInstanceLeaseClaim(
            project_id=lease.project_id,
            channel_instance_id=lease.channel_instance_id,
            holder_id=lease.holder_id,
            lease_token=lease_token,
            fencing_generation=lease.fencing_generation,
            lease_expires_at=expires_at,
        )

    async def release_instance_lease(
        self,
        session: AsyncSession,
        channel_instance_id: uuid.UUID | str,
        holder_id: uuid.UUID | str,
        lease_token: str,
        fencing_generation: int,
        *,
        now: datetime | None = None,
    ) -> bool:
        timestamp = self._now(now)
        lease = (
            await session.execute(
                select(ProjectChannelInstanceLeaseRow)
                .where(
                    ProjectChannelInstanceLeaseRow.channel_instance_id
                    == self._uuid(
                        channel_instance_id,
                        field="channel_instance_id",
                    )
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if not self._lease_matches(
            lease,
            holder_id=self._uuid(holder_id, field="holder_id"),
            lease_token=lease_token,
            fencing_generation=fencing_generation,
            now=timestamp,
        ):
            return False
        assert lease is not None
        # Keep the row so the next claimant must advance the fencing
        # generation; deleting it would incorrectly reset generation to one.
        lease.lease_expires_at = timestamp
        lease.last_heartbeat_at = timestamp
        await session.flush()
        return True

    @staticmethod
    def _token_hash(lease_token: str) -> str:
        if not lease_token:
            raise ValueError("lease_token is required")
        return hashlib.sha256(lease_token.encode("utf-8")).hexdigest()

    @classmethod
    def _lease_matches(
        cls,
        lease: ProjectChannelInstanceLeaseRow | None,
        *,
        holder_id: uuid.UUID,
        lease_token: str,
        fencing_generation: int,
        now: datetime,
    ) -> bool:
        return bool(
            lease is not None
            and lease.holder_id == holder_id
            and lease.fencing_generation == fencing_generation
            and lease.lease_expires_at > now
            and hmac.compare_digest(
                lease.lease_token_hash,
                cls._token_hash(lease_token),
            )
        )


__all__ = [
    "ChannelInstanceLeaseClaim",
    "ProjectChannelInstanceConflict",
    "ProjectChannelInstanceError",
    "ProjectChannelInstanceNotFound",
    "ProjectChannelInstanceRepository",
]
