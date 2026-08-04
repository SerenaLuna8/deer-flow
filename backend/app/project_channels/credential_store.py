from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from app.project_channels.errors import (
    ChannelInstanceConflict,
    ChannelInstanceStorageUnavailable,
    ChannelInstanceValidationFailed,
)
from app.projects.context import ProjectContext
from app.shared_assets.credential_repository import CredentialRepository
from app.shared_assets.crypto import (
    CredentialEncryptFailed,
    CredentialPayloadInvalid,
    encrypt_credential_payload,
)
from app.shared_assets.keyring import CredentialKeyring, CredentialKeyringInvalid
from app.shared_assets.models import AssetScope
from deerflow.persistence.shared_assets import (
    CredentialEnvelopeRow,
    CredentialRow,
    CredentialVersionRow,
)


@dataclass(frozen=True)
class ProjectChannelCredentialRef:
    credential_id: uuid.UUID
    credential_version_id: uuid.UUID


class ProjectChannelCredentialStore:
    """Write channel credentials inside the caller-owned database transaction."""

    def __init__(
        self,
        repository: CredentialRepository,
        *,
        keyring: CredentialKeyring | None = None,
    ) -> None:
        self._repository = repository
        self._keyring = keyring

    @staticmethod
    def _payload_schema(
        context: ProjectContext,
        payload: object,
    ) -> dict[str, list[str]]:
        try:
            if not isinstance(payload, Mapping) or not payload:
                raise ValueError
            schema: dict[str, list[str]] = {}
            for section, values in payload.items():
                if section not in {"env", "headers", "oauth", "query"}:
                    raise ValueError
                if not isinstance(values, Mapping) or not values:
                    raise ValueError
                names = sorted(values)
                if any(not isinstance(name, str) or not name or not isinstance(values[name], str) or not values[name] for name in names):
                    raise ValueError
                schema[section] = names
            return schema
        except (RecursionError, TypeError, ValueError):
            raise ChannelInstanceValidationFailed(
                context.request_id,
                "Channel credentials are invalid.",
                fields=("credentials",),
            ) from None

    def _envelope(
        self,
        context: ProjectContext,
        payload: object,
        version_id: uuid.UUID,
    ):
        try:
            keyring = self._keyring or CredentialKeyring.from_environment()
            return encrypt_credential_payload(
                payload,
                AssetScope.PROJECT,
                context.project_id,
                version_id,
                keyring,
            )
        except CredentialPayloadInvalid:
            raise ChannelInstanceValidationFailed(
                context.request_id,
                "Channel credentials are invalid.",
                fields=("credentials",),
            ) from None
        except (CredentialEncryptFailed, CredentialKeyringInvalid):
            raise ChannelInstanceStorageUnavailable(context.request_id) from None

    async def create(
        self,
        context: ProjectContext,
        *,
        instance_id: uuid.UUID,
        provider: str,
        display_name: str,
        payload: object,
    ) -> ProjectChannelCredentialRef:
        payload_schema = self._payload_schema(context, payload)
        version_id = uuid.uuid4()
        encrypted = self._envelope(context, payload, version_id)
        credential = CredentialRow(
            scope="project",
            project_id=context.project_id,
            name=f"channel-{provider}-{instance_id.hex[:16]}",
            display_name=f"{display_name} channel credential",
            credential_type=f"channel.{provider}",
            created_by_user_id=str(context.user_id),
        )
        await self._repository.create_project_credential(context, credential)
        version = CredentialVersionRow(
            id=version_id,
            credential_id=credential.id,
            version_number=1,
            status="active",
            payload_schema_version=1,
            payload_schema=payload_schema,
            created_by_user_id=str(context.user_id),
        )
        envelope = CredentialEnvelopeRow(
            credential_version_id=version_id,
            envelope_generation=1,
            key_id=encrypted.key_id,
            nonce=encrypted.nonce,
            ciphertext=encrypted.ciphertext,
            is_active=True,
            created_by_user_id=str(context.user_id),
            activated_at=datetime.now(UTC),
        )
        await self._repository.add_version(
            credential,
            version,
            envelope,
            request_id=context.request_id,
        )
        credential.current_version_id = version_id
        await self._repository.session.flush()
        return ProjectChannelCredentialRef(credential.id, version_id)

    async def rotate(
        self,
        context: ProjectContext,
        *,
        credential_id: uuid.UUID,
        provider: str,
        payload: object,
    ) -> ProjectChannelCredentialRef:
        payload_schema = self._payload_schema(context, payload)
        version_id = uuid.uuid4()
        encrypted = self._envelope(context, payload, version_id)
        credential = await self._repository.get_project_credential(
            context,
            credential_id,
            for_update=True,
        )
        if credential.status != "active" or credential.credential_type != f"channel.{provider}":
            raise ChannelInstanceConflict(context.request_id)
        previous = await self._repository.lock_current_version(
            credential,
            request_id=context.request_id,
        )
        if previous.status != "active":
            raise ChannelInstanceConflict(context.request_id)
        number = await self._repository.next_version_number(credential)
        previous.status = "retired"
        previous.retired_at = datetime.now(UTC)
        version = CredentialVersionRow(
            id=version_id,
            credential_id=credential.id,
            version_number=number,
            status="active",
            payload_schema_version=1,
            payload_schema=payload_schema,
            supersedes_version_id=previous.id,
            created_by_user_id=str(context.user_id),
        )
        envelope = CredentialEnvelopeRow(
            credential_version_id=version_id,
            envelope_generation=1,
            key_id=encrypted.key_id,
            nonce=encrypted.nonce,
            ciphertext=encrypted.ciphertext,
            is_active=True,
            created_by_user_id=str(context.user_id),
            activated_at=datetime.now(UTC),
        )
        await self._repository.add_version(
            credential,
            version,
            envelope,
            request_id=context.request_id,
        )
        credential.current_version_id = version_id
        credential.version += 1
        await self._repository.session.flush()
        return ProjectChannelCredentialRef(credential.id, version_id)

    async def revoke(
        self,
        context: ProjectContext,
        *,
        credential_id: uuid.UUID,
        provider: str,
    ) -> None:
        credential = await self._repository.get_project_credential(
            context,
            credential_id,
            for_update=True,
        )
        if credential.credential_type != f"channel.{provider}":
            raise ChannelInstanceConflict(context.request_id)
        timestamp = datetime.now(UTC)
        for version in await self._repository.lock_all_versions(credential):
            if version.status == "revoked":
                continue
            version.status = "revoked"
            version.revoked_at = timestamp
            version.revoked_by_user_id = str(context.user_id)
        credential.status = "revoked"
        credential.revoked_at = timestamp
        credential.revoked_by_user_id = str(context.user_id)
        await self._repository.mark_deleted(
            credential,
            request_id=context.request_id,
        )
        await self._repository.session.flush()


__all__ = [
    "ProjectChannelCredentialRef",
    "ProjectChannelCredentialStore",
]
