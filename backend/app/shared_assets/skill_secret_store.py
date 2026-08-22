from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.shared_assets.errors import AssetStorageUnavailable, SkillSecretConfigurationInvalid
from deerflow.persistence.shared_assets.skill_secret_model import (
    ProjectSkillSecretGenerationRow,
    ProjectSkillSecretStateRow,
    ProjectSkillSecretTombstoneRow,
)
from deerflow.secrets import (
    SecretEnvelope,
    SecretKey,
    SecretKeyInvalid,
    SecretMaterializationFailed,
    SecretProtectionFailed,
)


def skill_secret_recipient(
    project_id: uuid.UUID,
    skill_id: uuid.UUID,
    skill_version_id: uuid.UUID,
    secret_name: str,
) -> str:
    return ":".join(
        (
            "skill",
            str(project_id),
            str(skill_id),
            str(skill_version_id),
            secret_name,
        )
    )


def _digest(recipient: str, envelope: SecretEnvelope) -> str:
    return hashlib.sha256(recipient.encode("utf-8") + envelope.nonce + envelope.ciphertext).hexdigest()


@dataclass(frozen=True, slots=True)
class SkillSecretMaterial:
    project_id: uuid.UUID
    skill_id: uuid.UUID
    skill_version_id: uuid.UUID
    secret_name: str
    optional: bool
    revision: int
    generation_id: uuid.UUID
    generation_digest: str
    envelope: SecretEnvelope = field(repr=False)


class SkillSecretStore:
    def __init__(
        self,
        session: AsyncSession,
        *,
        secret_key: SecretKey | None = None,
    ) -> None:
        self.session = session
        self._secret_key = secret_key

    def _key(self, request_id: str) -> SecretKey:
        try:
            return self._secret_key or SecretKey.from_environment()
        except SecretKeyInvalid:
            raise AssetStorageUnavailable(request_id) from None

    async def ensure_states(
        self,
        *,
        project_id: uuid.UUID,
        skill_id: uuid.UUID,
        skill_version_id: uuid.UUID,
        requirements: Sequence[tuple[str, bool]],
        actor_user_id: str,
    ) -> tuple[ProjectSkillSecretStateRow, ...]:
        rows = tuple(
            (
                await self.session.execute(
                    select(ProjectSkillSecretStateRow)
                    .where(
                        ProjectSkillSecretStateRow.project_id == project_id,
                        ProjectSkillSecretStateRow.skill_id == skill_id,
                        ProjectSkillSecretStateRow.skill_version_id == skill_version_id,
                    )
                    .order_by(ProjectSkillSecretStateRow.secret_name)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        by_name = {row.secret_name: row for row in rows}
        declared = dict(requirements)
        if set(by_name) - set(declared):
            raise SkillSecretConfigurationInvalid("unknown")
        for name, optional in requirements:
            row = by_name.get(name)
            if row is None:
                row = ProjectSkillSecretStateRow(
                    project_id=project_id,
                    skill_id=skill_id,
                    skill_version_id=skill_version_id,
                    secret_name=name,
                    optional=optional,
                    revision=0,
                    current_generation_id=None,
                    updated_by_user_id=actor_user_id,
                )
                self.session.add(row)
                by_name[name] = row
            elif row.optional != optional:
                raise SkillSecretConfigurationInvalid("unknown")
        await self.session.flush()
        return tuple(by_name[name] for name, _optional in requirements)

    async def list_states(
        self,
        *,
        project_id: uuid.UUID,
        skill_id: uuid.UUID,
        skill_version_id: uuid.UUID,
        for_update: bool = False,
    ) -> tuple[ProjectSkillSecretStateRow, ...]:
        statement = (
            select(ProjectSkillSecretStateRow)
            .where(
                ProjectSkillSecretStateRow.project_id == project_id,
                ProjectSkillSecretStateRow.skill_id == skill_id,
                ProjectSkillSecretStateRow.skill_version_id == skill_version_id,
            )
            .order_by(ProjectSkillSecretStateRow.secret_name)
        )
        if for_update:
            statement = statement.with_for_update()
        return tuple((await self.session.execute(statement)).scalars().all())

    async def replace_values(
        self,
        *,
        project_id: uuid.UUID,
        skill_id: uuid.UUID,
        skill_version_id: uuid.UUID,
        requirements: Sequence[tuple[str, bool]],
        values: Mapping[str, str],
        actor_user_id: str,
        request_id: str,
    ) -> tuple[ProjectSkillSecretStateRow, ...]:
        states = await self.ensure_states(
            project_id=project_id,
            skill_id=skill_id,
            skill_version_id=skill_version_id,
            requirements=requirements,
            actor_user_id=actor_user_id,
        )
        by_name = {row.secret_name: row for row in states}
        key = self._key(request_id)
        for name, value in values.items():
            state = by_name[name]
            recipient = skill_secret_recipient(
                project_id,
                skill_id,
                skill_version_id,
                name,
            )
            try:
                envelope = SecretEnvelope.protect(
                    value.encode("utf-8"),
                    recipient=recipient,
                    key=key,
                )
            except (SecretProtectionFailed, UnicodeError, ValueError):
                raise AssetStorageUnavailable(request_id) from None
            revision = int(state.revision) + 1
            await self._destroy_current(
                state,
                reason="replace",
                revision=revision,
                actor_user_id=actor_user_id,
                request_id=request_id,
            )
            generation = ProjectSkillSecretGenerationRow(
                project_id=project_id,
                skill_id=skill_id,
                skill_version_id=skill_version_id,
                secret_name=name,
                revision=revision,
                nonce=envelope.nonce,
                ciphertext=envelope.ciphertext,
                envelope_digest=_digest(recipient, envelope),
                created_by_user_id=actor_user_id,
            )
            self.session.add(generation)
            await self.session.flush()
            state.current_generation_id = generation.id
            state.revision = revision
            state.updated_by_user_id = actor_user_id
            state.updated_at = datetime.now(UTC)
        await self.session.flush()
        return states

    async def clear(
        self,
        *,
        project_id: uuid.UUID,
        skill_id: uuid.UUID,
        skill_version_id: uuid.UUID,
        secret_name: str,
        actor_user_id: str,
        request_id: str,
    ) -> ProjectSkillSecretStateRow:
        state = (
            await self.session.execute(
                select(ProjectSkillSecretStateRow)
                .where(
                    ProjectSkillSecretStateRow.project_id == project_id,
                    ProjectSkillSecretStateRow.skill_id == skill_id,
                    ProjectSkillSecretStateRow.skill_version_id == skill_version_id,
                    ProjectSkillSecretStateRow.secret_name == secret_name,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if state is None:
            raise SkillSecretConfigurationInvalid(request_id)
        revision = int(state.revision) + 1
        await self._destroy_current(
            state,
            reason="clear",
            revision=revision,
            actor_user_id=actor_user_id,
            request_id=request_id,
        )
        state.revision = revision
        state.updated_by_user_id = actor_user_id
        state.updated_at = datetime.now(UTC)
        await self.session.flush()
        return state

    async def copy_compatible(
        self,
        *,
        project_id: uuid.UUID,
        skill_id: uuid.UUID,
        source_version_id: uuid.UUID,
        source_requirements: Sequence[tuple[str, bool]],
        target_version_id: uuid.UUID,
        target_requirements: Sequence[tuple[str, bool]],
        compatible_names: frozenset[str],
        actor_user_id: str,
        request_id: str,
    ) -> tuple[ProjectSkillSecretStateRow, ...]:
        source_by_name = dict(source_requirements)
        compatible = tuple((name, optional) for name, optional in target_requirements if name in compatible_names and source_by_name.get(name) is optional)
        if not compatible:
            return ()
        source = await self.load_materials(
            project_id=project_id,
            skill_id=skill_id,
            skill_version_id=source_version_id,
            requirements=source_requirements,
            require_required=False,
            for_update=True,
            request_id=request_id,
        )
        compatible_names = {name for name, _optional in compatible}
        values: dict[str, str] = {}
        try:
            for material in source:
                if material.secret_name in compatible_names:
                    values[material.secret_name] = self.materialize(
                        material,
                        request_id=request_id,
                    )
            if not values:
                return ()
            return await self.replace_values(
                project_id=project_id,
                skill_id=skill_id,
                skill_version_id=target_version_id,
                requirements=target_requirements,
                values=values,
                actor_user_id=actor_user_id,
                request_id=request_id,
            )
        finally:
            values.clear()

    async def load_materials(
        self,
        *,
        project_id: uuid.UUID,
        skill_id: uuid.UUID,
        skill_version_id: uuid.UUID,
        requirements: Sequence[tuple[str, bool]],
        require_required: bool,
        for_update: bool,
        request_id: str,
    ) -> tuple[SkillSecretMaterial, ...]:
        states = await self.list_states(
            project_id=project_id,
            skill_id=skill_id,
            skill_version_id=skill_version_id,
            for_update=for_update,
        )
        by_name = {row.secret_name: row for row in states}
        if set(by_name) - {name for name, _optional in requirements}:
            raise SkillSecretConfigurationInvalid(request_id)
        if require_required and any(not optional and (name not in by_name or by_name[name].current_generation_id is None) for name, optional in requirements):
            raise SkillSecretConfigurationInvalid(request_id)
        materials: list[SkillSecretMaterial] = []
        for name, optional in requirements:
            state = by_name.get(name)
            if state is None or state.current_generation_id is None:
                continue
            statement = select(ProjectSkillSecretGenerationRow).where(
                ProjectSkillSecretGenerationRow.project_id == project_id,
                ProjectSkillSecretGenerationRow.skill_id == skill_id,
                ProjectSkillSecretGenerationRow.skill_version_id == skill_version_id,
                ProjectSkillSecretGenerationRow.secret_name == name,
                ProjectSkillSecretGenerationRow.id == state.current_generation_id,
                ProjectSkillSecretGenerationRow.revision == state.revision,
            )
            if for_update:
                statement = statement.with_for_update()
            generation = (await self.session.execute(statement)).scalar_one_or_none()
            if generation is None:
                raise SkillSecretConfigurationInvalid(request_id)
            materials.append(
                SkillSecretMaterial(
                    project_id=project_id,
                    skill_id=skill_id,
                    skill_version_id=skill_version_id,
                    secret_name=name,
                    optional=optional,
                    revision=int(state.revision),
                    generation_id=generation.id,
                    generation_digest=generation.envelope_digest,
                    envelope=SecretEnvelope(
                        nonce=bytes(generation.nonce),
                        ciphertext=bytes(generation.ciphertext),
                    ),
                )
            )
        return tuple(materials)

    def materialize(self, material: SkillSecretMaterial, *, request_id: str) -> str:
        recipient = skill_secret_recipient(
            material.project_id,
            material.skill_id,
            material.skill_version_id,
            material.secret_name,
        )
        try:
            value = material.envelope.materialize(
                recipient=recipient,
                key=self._key(request_id),
            ).decode("utf-8")
            if not value or "\x00" in value:
                raise ValueError
            return value
        except (SecretMaterializationFailed, UnicodeError, ValueError):
            raise AssetStorageUnavailable(request_id) from None

    async def _destroy_current(
        self,
        state: ProjectSkillSecretStateRow,
        *,
        reason: str,
        revision: int,
        actor_user_id: str,
        request_id: str,
    ) -> None:
        if state.current_generation_id is None:
            return
        generation = (
            await self.session.execute(
                select(ProjectSkillSecretGenerationRow)
                .where(
                    ProjectSkillSecretGenerationRow.project_id == state.project_id,
                    ProjectSkillSecretGenerationRow.skill_id == state.skill_id,
                    ProjectSkillSecretGenerationRow.skill_version_id == state.skill_version_id,
                    ProjectSkillSecretGenerationRow.secret_name == state.secret_name,
                    ProjectSkillSecretGenerationRow.id == state.current_generation_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if generation is None:
            raise AssetStorageUnavailable(request_id)
        state.current_generation_id = None
        await self.session.flush()
        self.session.add(
            ProjectSkillSecretTombstoneRow(
                project_id=state.project_id,
                skill_id=state.skill_id,
                skill_version_id=state.skill_version_id,
                secret_name=state.secret_name,
                destroyed_generation_id=generation.id,
                revision=revision,
                envelope_digest=generation.envelope_digest,
                reason=reason,
                destroyed_by_user_id=actor_user_id,
            )
        )
        await self.session.execute(delete(ProjectSkillSecretGenerationRow).where(ProjectSkillSecretGenerationRow.id == generation.id))


__all__ = [
    "SkillSecretMaterial",
    "SkillSecretStore",
    "skill_secret_recipient",
]
