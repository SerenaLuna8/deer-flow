"""Channel-owned write-only secret bundle lifecycle."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.project_channels.errors import (
    ChannelInstanceStorageUnavailable,
    ChannelInstanceValidationFailed,
)
from app.projects.context import ProjectContext
from deerflow.persistence.channel_connections.model import (
    ProjectChannelInstanceRow,
    ProjectChannelSecretGenerationRow,
    ProjectChannelSecretStateRow,
    ProjectChannelSecretTombstoneRow,
)
from deerflow.secrets import (
    SecretEnvelope,
    SecretKey,
    SecretKeyInvalid,
    SecretProtectionFailed,
)


def channel_secret_recipient(instance: ProjectChannelInstanceRow) -> str:
    return ":".join(
        (
            "channel",
            str(uuid.UUID(str(instance.project_id))),
            str(uuid.UUID(str(instance.id))),
            instance.provider,
            instance.provider_identity_digest,
        )
    )


def _envelope_digest(
    recipient: str,
    envelope: SecretEnvelope,
) -> str:
    return hashlib.sha256(recipient.encode("utf-8") + envelope.nonce + envelope.ciphertext).hexdigest()


@dataclass(frozen=True, slots=True)
class ProjectChannelSecretStatus:
    configured: bool
    revision: int
    generation_id: uuid.UUID | None


class ProjectChannelSecretStore:
    """Mutate exactly one Channel Instance bundle in a caller transaction."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        secret_key: SecretKey | None = None,
    ) -> None:
        self._session = session
        self._secret_key = secret_key

    @staticmethod
    def _plaintext(
        context: ProjectContext,
        payload: object,
    ) -> bytes:
        try:
            if not isinstance(payload, Mapping) or not payload:
                raise ValueError
            normalized: dict[str, str] = {}
            for key, value in payload.items():
                if not isinstance(key, str) or not key or not isinstance(value, str) or not value or "\x00" in value:
                    raise ValueError
                normalized[key] = value
            return json.dumps(
                normalized,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (RecursionError, TypeError, ValueError):
            raise ChannelInstanceValidationFailed(
                context.request_id,
                "Channel secrets are invalid.",
                fields=("secrets",),
            ) from None

    async def status(
        self,
        *,
        project_id: uuid.UUID,
        instance_id: uuid.UUID,
        for_update: bool = False,
    ) -> ProjectChannelSecretStatus:
        statement = select(ProjectChannelSecretStateRow).where(
            ProjectChannelSecretStateRow.project_id == project_id,
            ProjectChannelSecretStateRow.channel_instance_id == instance_id,
        )
        if for_update:
            statement = statement.with_for_update()
        state = (await self._session.execute(statement)).scalar_one_or_none()
        return ProjectChannelSecretStatus(
            configured=(state is not None and state.current_generation_id is not None),
            revision=(0 if state is None else int(state.revision)),
            generation_id=(None if state is None else state.current_generation_id),
        )

    async def replace(
        self,
        context: ProjectContext,
        *,
        instance: ProjectChannelInstanceRow,
        payload: object,
        reason: str = "replace",
    ) -> ProjectChannelSecretStatus:
        if reason not in {"replace", "recipient_change"}:
            raise ChannelInstanceValidationFailed(
                context.request_id,
                "Channel secret replacement reason is invalid.",
            )
        plaintext = self._plaintext(context, payload)
        try:
            recipient = channel_secret_recipient(instance)
            envelope = SecretEnvelope.protect(
                plaintext,
                recipient=recipient,
                key=self._secret_key or SecretKey.from_environment(),
            )
        except (SecretKeyInvalid, SecretProtectionFailed, ValueError):
            raise ChannelInstanceStorageUnavailable(context.request_id) from None
        state = (
            await self._session.execute(
                select(ProjectChannelSecretStateRow)
                .where(
                    ProjectChannelSecretStateRow.project_id == context.project_id,
                    ProjectChannelSecretStateRow.channel_instance_id == instance.id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if state is None:
            state = ProjectChannelSecretStateRow(
                project_id=context.project_id,
                channel_instance_id=instance.id,
                revision=0,
                current_generation_id=None,
                updated_by_user_id=str(context.user_id),
            )
            self._session.add(state)
            await self._session.flush()
        revision = int(state.revision) + 1
        await self._destroy_current(
            context,
            state=state,
            reason=reason,
            revision=revision,
        )
        generation = ProjectChannelSecretGenerationRow(
            project_id=context.project_id,
            channel_instance_id=instance.id,
            revision=revision,
            nonce=envelope.nonce,
            ciphertext=envelope.ciphertext,
            envelope_digest=_envelope_digest(recipient, envelope),
            created_by_user_id=str(context.user_id),
        )
        self._session.add(generation)
        await self._session.flush()
        state.current_generation_id = generation.id
        state.revision = revision
        state.updated_by_user_id = str(context.user_id)
        state.updated_at = datetime.now(UTC)
        await self._session.flush()
        return ProjectChannelSecretStatus(True, revision, generation.id)

    async def clear(
        self,
        context: ProjectContext,
        *,
        instance: ProjectChannelInstanceRow,
        reason: str = "clear",
    ) -> ProjectChannelSecretStatus:
        state = (
            await self._session.execute(
                select(ProjectChannelSecretStateRow)
                .where(
                    ProjectChannelSecretStateRow.project_id == context.project_id,
                    ProjectChannelSecretStateRow.channel_instance_id == instance.id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if state is None:
            state = ProjectChannelSecretStateRow(
                project_id=context.project_id,
                channel_instance_id=instance.id,
                revision=0,
                current_generation_id=None,
                updated_by_user_id=str(context.user_id),
            )
            self._session.add(state)
            await self._session.flush()
        revision = int(state.revision) + 1
        await self._destroy_current(
            context,
            state=state,
            reason=reason,
            revision=revision,
        )
        state.revision = revision
        state.updated_by_user_id = str(context.user_id)
        state.updated_at = datetime.now(UTC)
        await self._session.flush()
        return ProjectChannelSecretStatus(False, revision, None)

    async def _destroy_current(
        self,
        context: ProjectContext,
        *,
        state: ProjectChannelSecretStateRow,
        reason: str,
        revision: int,
    ) -> None:
        generation_id = state.current_generation_id
        if generation_id is None:
            return
        generation = (
            await self._session.execute(
                select(ProjectChannelSecretGenerationRow)
                .where(
                    ProjectChannelSecretGenerationRow.id == generation_id,
                    ProjectChannelSecretGenerationRow.project_id == state.project_id,
                    ProjectChannelSecretGenerationRow.channel_instance_id == state.channel_instance_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if generation is None:
            raise ChannelInstanceStorageUnavailable(context.request_id)
        state.current_generation_id = None
        await self._session.flush()
        self._session.add(
            ProjectChannelSecretTombstoneRow(
                project_id=state.project_id,
                channel_instance_id=state.channel_instance_id,
                destroyed_generation_id=generation.id,
                revision=revision,
                envelope_digest=generation.envelope_digest,
                reason=reason,
                destroyed_by_user_id=str(context.user_id),
            )
        )
        await self._session.execute(delete(ProjectChannelSecretGenerationRow).where(ProjectChannelSecretGenerationRow.id == generation.id))


__all__ = [
    "ProjectChannelSecretStatus",
    "ProjectChannelSecretStore",
    "channel_secret_recipient",
]
