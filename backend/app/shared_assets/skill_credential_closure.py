from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.shared_assets import (
    CredentialEnvelopeRow,
    CredentialRow,
    CredentialVersionRow,
    ProjectSkillCredentialBindingRow,
    ProjectSkillCredentialConfigRow,
    SkillVersionRow,
)


class SkillCredentialClosureInvalid(Exception):
    pass


@dataclass(frozen=True)
class SkillCredentialClosureTarget:
    skill_id: uuid.UUID
    skill_version_id: uuid.UUID


@dataclass(frozen=True)
class LockedSkillCredentialMaterial:
    binding_id: uuid.UUID
    binding_revision: int
    skill_id: uuid.UUID
    skill_version_id: uuid.UUID
    env_name: str
    credential_id: uuid.UUID
    credential_version_id: uuid.UUID
    credential_field_group: str
    credential_field_name: str
    envelope_id: uuid.UUID
    envelope: CredentialEnvelopeRow | None = field(default=None, repr=False)


@dataclass(frozen=True)
class LockedSkillCredentialClosure:
    skill_id: uuid.UUID
    skill_version_id: uuid.UUID
    config_revision: int
    materials: tuple[LockedSkillCredentialMaterial, ...] = field(repr=False)

    @property
    def binding_ids(self) -> tuple[uuid.UUID, ...]:
        return tuple(material.binding_id for material in self.materials)


def _normalize_targets(
    targets: Sequence[SkillCredentialClosureTarget],
) -> dict[uuid.UUID, SkillCredentialClosureTarget]:
    normalized: dict[uuid.UUID, SkillCredentialClosureTarget] = {}
    for target in targets:
        if not isinstance(target, SkillCredentialClosureTarget) or not isinstance(target.skill_id, uuid.UUID) or not isinstance(target.skill_version_id, uuid.UUID):
            raise SkillCredentialClosureInvalid
        canonical = SkillCredentialClosureTarget(
            uuid.UUID(str(target.skill_id)),
            uuid.UUID(str(target.skill_version_id)),
        )
        existing = normalized.get(canonical.skill_version_id)
        if existing is not None and existing != canonical:
            raise SkillCredentialClosureInvalid
        normalized[canonical.skill_version_id] = canonical
    return normalized


def _requirements(
    value: object,
) -> tuple[tuple[str, bool], ...]:
    if not isinstance(value, list):
        raise SkillCredentialClosureInvalid
    result: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) - {"name", "optional"} or not isinstance(item.get("name"), str) or not isinstance(item.get("optional", False), bool):
            raise SkillCredentialClosureInvalid
        name = item["name"]
        if not name or name in seen:
            raise SkillCredentialClosureInvalid
        seen.add(name)
        result.append((name, item.get("optional", False)))
    return tuple(result)


def _env_schema_contains(version: CredentialVersionRow, name: str) -> bool:
    env = version.payload_schema.get("env")
    return isinstance(env, list) and all(isinstance(item, str) for item in env) and name in env


async def lock_skill_credential_closures(
    session: AsyncSession,
    project_id: uuid.UUID,
    targets: Sequence[SkillCredentialClosureTarget],
    *,
    load_envelopes: bool = False,
    require_required: bool = True,
) -> dict[uuid.UUID, LockedSkillCredentialClosure]:
    """Lock and validate exact project-local Skill Credential closures.

    The caller must already hold and revalidate project authority. This helper
    returns IDs and field names only unless ``load_envelopes`` is explicitly
    requested by the Worker-side materializer.
    """

    if not isinstance(project_id, uuid.UUID) or not isinstance(load_envelopes, bool) or not isinstance(require_required, bool):
        raise SkillCredentialClosureInvalid
    normalized = _normalize_targets(targets)
    ordered_version_ids = sorted(normalized, key=lambda value: value.int)

    versions: dict[uuid.UUID, SkillVersionRow] = {}
    requirements_by_version: dict[
        uuid.UUID,
        tuple[tuple[str, bool], ...],
    ] = {}
    for version_id in ordered_version_ids:
        target = normalized[version_id]
        version = (
            await session.execute(
                select(SkillVersionRow)
                .where(
                    SkillVersionRow.id == version_id,
                    SkillVersionRow.skill_id == target.skill_id,
                    SkillVersionRow.workflow_status == "published",
                )
                .with_for_update(read=True, of=SkillVersionRow)
            )
        ).scalar_one_or_none()
        if version is None:
            raise SkillCredentialClosureInvalid
        versions[version_id] = version
        requirements_by_version[version_id] = _requirements(version.secret_requirements)

    configs: dict[uuid.UUID, ProjectSkillCredentialConfigRow | None] = {}
    bindings_by_version: dict[
        uuid.UUID,
        tuple[ProjectSkillCredentialBindingRow, ...],
    ] = {}
    for version_id in ordered_version_ids:
        target = normalized[version_id]
        config = (
            await session.execute(
                select(ProjectSkillCredentialConfigRow)
                .where(
                    ProjectSkillCredentialConfigRow.project_id == project_id,
                    ProjectSkillCredentialConfigRow.skill_id == target.skill_id,
                    ProjectSkillCredentialConfigRow.skill_version_id == version_id,
                )
                .with_for_update(read=True, of=ProjectSkillCredentialConfigRow)
            )
        ).scalar_one_or_none()
        configs[version_id] = config
        if config is None:
            bindings_by_version[version_id] = ()
            continue
        bindings_by_version[version_id] = tuple(
            (
                await session.execute(
                    select(ProjectSkillCredentialBindingRow)
                    .where(
                        ProjectSkillCredentialBindingRow.project_id == project_id,
                        ProjectSkillCredentialBindingRow.skill_id == target.skill_id,
                        ProjectSkillCredentialBindingRow.skill_version_id == version_id,
                        ProjectSkillCredentialBindingRow.status == "active",
                    )
                    .order_by(
                        ProjectSkillCredentialBindingRow.secret_name,
                        ProjectSkillCredentialBindingRow.id,
                    )
                    .with_for_update(
                        read=True,
                        of=ProjectSkillCredentialBindingRow,
                    )
                )
            )
            .scalars()
            .all()
        )

    all_bindings = tuple(binding for version_id in ordered_version_ids for binding in bindings_by_version[version_id])
    credential_ids = sorted(
        {uuid.UUID(str(binding.credential_id)) for binding in all_bindings},
        key=lambda value: value.int,
    )
    credentials: dict[uuid.UUID, CredentialRow] = {}
    for credential_id in credential_ids:
        credential = (
            await session.execute(
                select(CredentialRow)
                .where(
                    CredentialRow.id == credential_id,
                    CredentialRow.is_delete.is_(False),
                )
                .with_for_update(read=True, of=CredentialRow)
            )
        ).scalar_one_or_none()
        if credential is None:
            raise SkillCredentialClosureInvalid
        credentials[credential_id] = credential

    credential_version_ids = sorted(
        {uuid.UUID(str(binding.credential_version_id)) for binding in all_bindings},
        key=lambda value: value.int,
    )
    credential_versions: dict[uuid.UUID, CredentialVersionRow] = {}
    envelopes: dict[
        uuid.UUID,
        tuple[uuid.UUID, CredentialEnvelopeRow | None],
    ] = {}
    for credential_version_id in credential_version_ids:
        version = (await session.execute(select(CredentialVersionRow).where(CredentialVersionRow.id == credential_version_id).with_for_update(read=True, of=CredentialVersionRow))).scalar_one_or_none()
        if version is None:
            raise SkillCredentialClosureInvalid
        credential_versions[credential_version_id] = version
        envelope_statement = select(CredentialEnvelopeRow if load_envelopes else CredentialEnvelopeRow.id).where(
            CredentialEnvelopeRow.credential_version_id == credential_version_id,
            CredentialEnvelopeRow.is_active.is_(True),
        )
        envelope_statement = envelope_statement.with_for_update(
            read=True,
            of=CredentialEnvelopeRow,
        )
        envelope_value = (await session.execute(envelope_statement)).scalar_one_or_none()
        if envelope_value is None:
            raise SkillCredentialClosureInvalid
        if load_envelopes:
            envelopes[credential_version_id] = (
                uuid.UUID(str(envelope_value.id)),
                envelope_value,
            )
        else:
            envelopes[credential_version_id] = (
                uuid.UUID(str(envelope_value)),
                None,
            )

    closures: dict[uuid.UUID, LockedSkillCredentialClosure] = {}
    for version_id in ordered_version_ids:
        target = normalized[version_id]
        requirements = requirements_by_version[version_id]
        requirement_by_name = dict(requirements)
        config = configs[version_id]
        bindings = bindings_by_version[version_id]
        binding_by_name: dict[str, ProjectSkillCredentialBindingRow] = {}
        for binding in bindings:
            if binding.secret_name in binding_by_name:
                raise SkillCredentialClosureInvalid
            binding_by_name[binding.secret_name] = binding
        if set(binding_by_name) - set(requirement_by_name):
            raise SkillCredentialClosureInvalid
        if require_required and any(not optional and name not in binding_by_name for name, optional in requirements):
            raise SkillCredentialClosureInvalid

        materials: list[LockedSkillCredentialMaterial] = []
        for name, _optional in requirements:
            binding = binding_by_name.get(name)
            if binding is None:
                continue
            if config is None or binding.config_revision != config.revision or binding.skill_id != target.skill_id or binding.skill_version_id != version_id:
                raise SkillCredentialClosureInvalid
            credential_id = uuid.UUID(str(binding.credential_id))
            credential_version_id = uuid.UUID(str(binding.credential_version_id))
            credential = credentials.get(credential_id)
            credential_version = credential_versions.get(credential_version_id)
            envelope = envelopes.get(credential_version_id)
            if (
                credential is None
                or credential_version is None
                or envelope is None
                or credential.scope != "project"
                or credential.project_id != project_id
                or credential.status != "active"
                or credential.is_delete
                or credential.current_version_id != credential_version_id
                or credential_version.credential_id != credential_id
                or credential_version.status != "active"
                or not _env_schema_contains(credential_version, name)
            ):
                raise SkillCredentialClosureInvalid
            envelope_id, envelope_row = envelope
            materials.append(
                LockedSkillCredentialMaterial(
                    binding_id=uuid.UUID(str(binding.id)),
                    binding_revision=binding.config_revision,
                    skill_id=target.skill_id,
                    skill_version_id=version_id,
                    env_name=name,
                    credential_id=credential_id,
                    credential_version_id=credential_version_id,
                    credential_field_group="env",
                    credential_field_name=name,
                    envelope_id=envelope_id,
                    envelope=envelope_row,
                )
            )
        closures[version_id] = LockedSkillCredentialClosure(
            skill_id=target.skill_id,
            skill_version_id=version_id,
            config_revision=0 if config is None else config.revision,
            materials=tuple(materials),
        )
    return closures


async def lock_skill_credential_closure(
    session: AsyncSession,
    project_id: uuid.UUID,
    skill_id: uuid.UUID,
    skill_version_id: uuid.UUID,
    *,
    load_envelopes: bool = False,
    require_required: bool = True,
) -> LockedSkillCredentialClosure:
    version_id = uuid.UUID(str(skill_version_id))
    closures = await lock_skill_credential_closures(
        session,
        project_id,
        (
            SkillCredentialClosureTarget(
                uuid.UUID(str(skill_id)),
                version_id,
            ),
        ),
        load_envelopes=load_envelopes,
        require_required=require_required,
    )
    return closures[version_id]
