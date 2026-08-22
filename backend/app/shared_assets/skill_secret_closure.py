from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.shared_assets.errors import SharedAssetError
from app.shared_assets.skill_secret_policy import parse_skill_secret_requirements
from app.shared_assets.skill_secret_store import SkillSecretStore
from deerflow.persistence.shared_assets import SkillVersionRow
from deerflow.persistence.shared_assets.skill_secret_model import (
    ProjectSkillSecretGenerationRow,
    ProjectSkillSecretStateRow,
)
from deerflow.secrets import SecretEnvelope


class SkillSecretClosureInvalid(Exception):
    pass


@dataclass(frozen=True, slots=True)
class SkillSecretClosureTarget:
    skill_id: uuid.UUID
    skill_version_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class AdmittedSkillSecretReference:
    skill_id: uuid.UUID
    skill_version_id: uuid.UUID
    secret_name: str
    secret_revision: int
    secret_generation_id: uuid.UUID
    secret_generation_digest: str


@dataclass(frozen=True, slots=True)
class LockedSkillSecretMaterial:
    skill_id: uuid.UUID
    skill_version_id: uuid.UUID
    secret_name: str
    secret_revision: int
    secret_generation_id: uuid.UUID
    secret_generation_digest: str
    envelope: SecretEnvelope | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class LockedSkillSecretClosure:
    skill_id: uuid.UUID
    skill_version_id: uuid.UUID
    materials: tuple[LockedSkillSecretMaterial, ...] = field(repr=False)


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


async def lock_skill_secret_closures(
    session: AsyncSession,
    project_id: uuid.UUID,
    targets: Sequence[SkillSecretClosureTarget],
    *,
    load_envelopes: bool = False,
    require_required: bool = True,
) -> dict[uuid.UUID, LockedSkillSecretClosure]:
    if not isinstance(project_id, uuid.UUID) or not isinstance(load_envelopes, bool) or not isinstance(require_required, bool):
        raise SkillSecretClosureInvalid
    normalized: dict[uuid.UUID, SkillSecretClosureTarget] = {}
    for target in targets:
        if not isinstance(target, SkillSecretClosureTarget) or not isinstance(target.skill_id, uuid.UUID) or not isinstance(target.skill_version_id, uuid.UUID):
            raise SkillSecretClosureInvalid
        prior = normalized.get(target.skill_version_id)
        if prior is not None and prior != target:
            raise SkillSecretClosureInvalid
        normalized[target.skill_version_id] = target

    result: dict[uuid.UUID, LockedSkillSecretClosure] = {}
    store = SkillSecretStore(session)
    for version_id in sorted(normalized, key=lambda value: value.int):
        target = normalized[version_id]
        version = (
            await session.execute(
                select(SkillVersionRow)
                .where(
                    SkillVersionRow.id == version_id,
                    SkillVersionRow.skill_id == target.skill_id,
                    SkillVersionRow.revoked_at.is_(None),
                )
                .with_for_update(read=True, of=SkillVersionRow)
            )
        ).scalar_one_or_none()
        if version is None:
            raise SkillSecretClosureInvalid
        try:
            requirements = parse_skill_secret_requirements(
                version.secret_requirements,
                request_id="unknown",
            )
            source = await store.load_materials(
                project_id=project_id,
                skill_id=target.skill_id,
                skill_version_id=version_id,
                requirements=requirements,
                require_required=require_required,
                for_update=True,
                request_id="unknown",
            )
        except SharedAssetError:
            raise SkillSecretClosureInvalid from None
        materials = tuple(
            LockedSkillSecretMaterial(
                skill_id=item.skill_id,
                skill_version_id=item.skill_version_id,
                secret_name=item.secret_name,
                secret_revision=item.revision,
                secret_generation_id=item.generation_id,
                secret_generation_digest=item.generation_digest,
                envelope=item.envelope if load_envelopes else None,
            )
            for item in source
        )
        result[version_id] = LockedSkillSecretClosure(
            skill_id=target.skill_id,
            skill_version_id=version_id,
            materials=materials,
        )
    return result


async def lock_skill_secret_closure(
    session: AsyncSession,
    project_id: uuid.UUID,
    skill_id: uuid.UUID,
    skill_version_id: uuid.UUID,
    *,
    load_envelopes: bool = False,
    require_required: bool = True,
) -> LockedSkillSecretClosure:
    closures = await lock_skill_secret_closures(
        session,
        project_id,
        (SkillSecretClosureTarget(skill_id, skill_version_id),),
        load_envelopes=load_envelopes,
        require_required=require_required,
    )
    return closures[skill_version_id]


async def lock_admitted_skill_secret_materials(
    session: AsyncSession,
    project_id: uuid.UUID,
    references: Sequence[AdmittedSkillSecretReference],
    *,
    declared_targets: frozenset[tuple[uuid.UUID, str]],
    required_targets: frozenset[tuple[uuid.UUID, str]],
    load_envelopes: bool = False,
) -> tuple[LockedSkillSecretMaterial, ...]:
    if not isinstance(project_id, uuid.UUID) or not isinstance(declared_targets, frozenset) or not isinstance(required_targets, frozenset) or not required_targets <= declared_targets:
        raise SkillSecretClosureInvalid
    normalized: list[AdmittedSkillSecretReference] = []
    seen: set[tuple[uuid.UUID, str]] = set()
    for reference in references:
        target = (reference.skill_version_id, reference.secret_name)
        if (
            not isinstance(reference, AdmittedSkillSecretReference)
            or not isinstance(reference.skill_id, uuid.UUID)
            or not isinstance(reference.skill_version_id, uuid.UUID)
            or not isinstance(reference.secret_name, str)
            or not reference.secret_name
            or isinstance(reference.secret_revision, bool)
            or not isinstance(reference.secret_revision, int)
            or reference.secret_revision < 1
            or not isinstance(reference.secret_generation_id, uuid.UUID)
            or not _valid_digest(reference.secret_generation_digest)
            or target in seen
            or target not in declared_targets
        ):
            raise SkillSecretClosureInvalid
        seen.add(target)
        normalized.append(reference)
    if not required_targets <= seen:
        raise SkillSecretClosureInvalid
    normalized.sort(key=lambda item: (item.skill_version_id.int, item.secret_name))

    materials: list[LockedSkillSecretMaterial] = []
    for reference in normalized:
        state = (
            await session.execute(
                select(ProjectSkillSecretStateRow)
                .where(
                    ProjectSkillSecretStateRow.project_id == project_id,
                    ProjectSkillSecretStateRow.skill_id == reference.skill_id,
                    ProjectSkillSecretStateRow.skill_version_id == reference.skill_version_id,
                    ProjectSkillSecretStateRow.secret_name == reference.secret_name,
                    ProjectSkillSecretStateRow.revision == reference.secret_revision,
                    ProjectSkillSecretStateRow.current_generation_id == reference.secret_generation_id,
                )
                .with_for_update(read=True, of=ProjectSkillSecretStateRow)
            )
        ).scalar_one_or_none()
        generation = (
            await session.execute(
                select(ProjectSkillSecretGenerationRow)
                .where(
                    ProjectSkillSecretGenerationRow.project_id == project_id,
                    ProjectSkillSecretGenerationRow.skill_id == reference.skill_id,
                    ProjectSkillSecretGenerationRow.skill_version_id == reference.skill_version_id,
                    ProjectSkillSecretGenerationRow.secret_name == reference.secret_name,
                    ProjectSkillSecretGenerationRow.id == reference.secret_generation_id,
                    ProjectSkillSecretGenerationRow.revision == reference.secret_revision,
                    ProjectSkillSecretGenerationRow.envelope_digest == reference.secret_generation_digest,
                )
                .with_for_update(read=True, of=ProjectSkillSecretGenerationRow)
            )
        ).scalar_one_or_none()
        if state is None or generation is None:
            raise SkillSecretClosureInvalid
        materials.append(
            LockedSkillSecretMaterial(
                skill_id=reference.skill_id,
                skill_version_id=reference.skill_version_id,
                secret_name=reference.secret_name,
                secret_revision=reference.secret_revision,
                secret_generation_id=reference.secret_generation_id,
                secret_generation_digest=reference.secret_generation_digest,
                envelope=(
                    SecretEnvelope(
                        nonce=bytes(generation.nonce),
                        ciphertext=bytes(generation.ciphertext),
                    )
                    if load_envelopes
                    else None
                ),
            )
        )
    return tuple(materials)


__all__ = [
    "AdmittedSkillSecretReference",
    "LockedSkillSecretClosure",
    "LockedSkillSecretMaterial",
    "SkillSecretClosureInvalid",
    "SkillSecretClosureTarget",
    "lock_admitted_skill_secret_materials",
    "lock_skill_secret_closure",
    "lock_skill_secret_closures",
]
