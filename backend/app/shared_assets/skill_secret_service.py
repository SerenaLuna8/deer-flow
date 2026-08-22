from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, TypeVar

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.errors import (
    AssetConflict,
    AssetForbidden,
    AssetNotFound,
    AssetStorageUnavailable,
    AssetValidationFailed,
    SharedAssetError,
    SkillSecretConfigurationInvalid,
    SkillSecretRevisionStale,
    SkillSecretsIncomplete,
)
from app.shared_assets.governance_events import SharedAssetGovernanceEventSink
from app.shared_assets.models import AssetScope, VersionRelation
from app.shared_assets.skill_secret_policy import (
    normalize_skill_secret_values,
    parse_skill_secret_requirements,
)
from app.shared_assets.skill_secret_store import SkillSecretStore
from app.shared_assets.version_relation import VersionLineageNode, classify_version_relations
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.shared_assets import (
    ProjectSystemSkillBindingRow,
    SkillRow,
    SkillVersionRow,
)
from deerflow.persistence.shared_assets.skill_secret_model import ProjectSkillSecretStateRow

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class SkillSecretTarget:
    asset: SkillRow
    version: SkillVersionRow


@dataclass(frozen=True, slots=True)
class SkillSecretRequirementStatus:
    name: str
    optional: bool
    configured: bool
    revision: int


@dataclass(frozen=True, slots=True)
class SkillSecretSetView:
    skill_id: uuid.UUID
    skill_version_id: uuid.UUID
    revision: int
    readiness: Literal["ready", "unready"]
    requirements: tuple[SkillSecretRequirementStatus, ...]


@dataclass(frozen=True, slots=True)
class SkillSecretReadinessRequirementView:
    name: str
    optional: bool
    configured: bool


@dataclass(frozen=True, slots=True)
class SkillActivationReadinessView:
    skill_id: uuid.UUID
    skill_version_id: uuid.UUID
    revision: int
    payload_checksum: str
    secret_revision: int
    secrets_autonomous: bool
    ready: bool
    required_count: int
    configured_required_count: int
    requirements: tuple[SkillSecretReadinessRequirementView, ...]


def aggregate_skill_secret_revision(
    states: Sequence[ProjectSkillSecretStateRow],
) -> int:
    return sum(int(row.revision) for row in states)


def _requirements(version: SkillVersionRow, request_id: str) -> tuple[tuple[str, bool], ...]:
    return parse_skill_secret_requirements(
        version.secret_requirements,
        request_id=request_id,
    )


async def validate_skill_secret_readiness_in_transaction(
    session: AsyncSession,
    actor: ProjectContext | SystemAssetGovernanceContext,
    asset: SkillRow,
    version: SkillVersionRow,
    *,
    expected_revision: int | None,
    require_complete: bool,
) -> int:
    project_id = getattr(actor, "project_id", None)
    if not isinstance(project_id, uuid.UUID):
        raise AssetForbidden(getattr(actor, "request_id", "unknown"))
    request_id = getattr(actor, "request_id", "unknown")
    requirements = _requirements(version, request_id)
    states = await SkillSecretStore(session).list_states(
        project_id=project_id,
        skill_id=asset.id,
        skill_version_id=version.id,
        for_update=True,
    )
    by_name = {row.secret_name: row for row in states}
    if set(by_name) - {name for name, _optional in requirements}:
        raise SkillSecretConfigurationInvalid(request_id)
    revision = aggregate_skill_secret_revision(states)
    if expected_revision is not None and revision != expected_revision:
        raise SkillSecretRevisionStale(request_id)
    if require_complete and any(not optional and (name not in by_name or by_name[name].current_generation_id is None) for name, optional in requirements):
        raise SkillSecretsIncomplete(request_id)
    return revision


async def copy_compatible_skill_secrets_in_transaction(
    session: AsyncSession,
    actor: ProjectContext | SystemAssetGovernanceContext,
    asset: SkillRow,
    source: SkillVersionRow | None,
    target: SkillVersionRow,
) -> tuple[ProjectSkillSecretStateRow, ...]:
    if source is None or not isinstance(actor, ProjectContext):
        return ()
    if (
        not {
            Capability.SHARED_ASSETS_EDIT,
            Capability.SHARED_ASSETS_MANAGE_BINDINGS,
        }
        <= actor.capabilities
    ):
        return ()
    return await SkillSecretStore(session).copy_compatible(
        project_id=actor.project_id,
        skill_id=asset.id,
        source_version_id=source.id,
        source_requirements=_requirements(source, actor.request_id),
        target_version_id=target.id,
        target_requirements=_requirements(target, actor.request_id),
        actor_user_id=str(actor.user_id),
        request_id=actor.request_id,
    )


class SkillSecretService:
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        governance_sink: SharedAssetGovernanceEventSink | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._governance_sink = governance_sink or SharedAssetGovernanceEventSink()

    async def get(
        self,
        actor: ProjectContext,
        skill_id: uuid.UUID,
    ) -> SkillSecretSetView:
        self._require(actor, Capability.SHARED_ASSETS_READ)

        async def operation(session: AsyncSession) -> SkillSecretSetView:
            await self._lock_project(session, actor, read=True)
            target = await self._target(session, actor, skill_id, None, read=True)
            return await self._view(session, actor, target)

        return await self._execute(actor, operation)

    async def get_exact(
        self,
        actor: ProjectContext,
        skill_id: uuid.UUID,
        version_id: uuid.UUID,
    ) -> SkillSecretSetView:
        self._require(actor, Capability.SHARED_ASSETS_READ)

        async def operation(session: AsyncSession) -> SkillSecretSetView:
            await self._lock_project(session, actor, read=True)
            target = await self._target(
                session,
                actor,
                skill_id,
                version_id,
                read=True,
            )
            return await self._view(session, actor, target)

        return await self._execute(actor, operation)

    async def get_for_version(
        self,
        actor: ProjectContext,
        skill_id: uuid.UUID,
        version_id: uuid.UUID,
    ) -> SkillActivationReadinessView:
        self._require(actor, Capability.SHARED_ASSETS_READ)

        async def operation(session: AsyncSession) -> SkillActivationReadinessView:
            await self._lock_project(session, actor, read=True)
            target = await self._target(
                session,
                actor,
                skill_id,
                version_id,
                read=True,
            )
            if target.asset.scope != AssetScope.PROJECT.value:
                raise AssetConflict(actor.request_id)
            relations = await self._relations(session, target.asset)
            if relations.get(target.version.id) is not VersionRelation.CANDIDATE:
                raise AssetConflict(actor.request_id)
            view = await self._view(session, actor, target)
            requirements = _requirements(target.version, actor.request_id)
            frontmatter = target.version.frontmatter
            if not isinstance(frontmatter, Mapping):
                raise SkillSecretConfigurationInvalid(actor.request_id)
            autonomous = frontmatter.get("secrets-autonomous", True)
            if not isinstance(autonomous, bool):
                raise SkillSecretConfigurationInvalid(actor.request_id)
            required_count = sum(not optional for _name, optional in requirements)
            configured_required_count = sum(not item.optional and item.configured for item in view.requirements)
            return SkillActivationReadinessView(
                skill_id=target.asset.id,
                skill_version_id=target.version.id,
                revision=int(target.asset.revision),
                payload_checksum=target.version.payload_checksum,
                secret_revision=view.revision,
                secrets_autonomous=autonomous,
                ready=configured_required_count == required_count,
                required_count=required_count,
                configured_required_count=configured_required_count,
                requirements=tuple(
                    SkillSecretReadinessRequirementView(
                        name=item.name,
                        optional=item.optional,
                        configured=item.configured,
                    )
                    for item in view.requirements
                ),
            )

        return await self._execute(actor, operation)

    async def replace(
        self,
        actor: ProjectContext,
        skill_id: uuid.UUID,
        values: Mapping[str, str],
        *,
        expected_skill_version_id: uuid.UUID,
    ) -> SkillSecretSetView:
        return await self.replace_for_version(
            actor,
            skill_id,
            expected_skill_version_id,
            values,
        )

    async def replace_for_version(
        self,
        actor: ProjectContext,
        skill_id: uuid.UUID,
        version_id: uuid.UUID,
        values: Mapping[str, str],
    ) -> SkillSecretSetView:
        self._require(actor, Capability.SHARED_ASSETS_MANAGE_BINDINGS)

        async def operation(session: AsyncSession) -> SkillSecretSetView:
            await self._lock_project(session, actor)
            target = await self._target(session, actor, skill_id, version_id)
            await self._require_replaceable(session, actor, target)
            requirements = _requirements(target.version, actor.request_id)
            normalized = normalize_skill_secret_values(
                values,
                declared_names=frozenset(name for name, _optional in requirements),
                request_id=actor.request_id,
            )
            existing = {
                row.secret_name: row.current_generation_id is not None
                for row in await SkillSecretStore(session).list_states(
                    project_id=actor.project_id,
                    skill_id=target.asset.id,
                    skill_version_id=target.version.id,
                    for_update=True,
                )
            }
            if normalized:
                await SkillSecretStore(session).replace_values(
                    project_id=actor.project_id,
                    skill_id=target.asset.id,
                    skill_version_id=target.version.id,
                    requirements=requirements,
                    values=normalized,
                    actor_user_id=str(actor.user_id),
                    request_id=actor.request_id,
                )
            result = await self._view(session, actor, target, for_update=True)
            current = {
                row.secret_name: row
                for row in await SkillSecretStore(session).list_states(
                    project_id=actor.project_id,
                    skill_id=target.asset.id,
                    skill_version_id=target.version.id,
                    for_update=True,
                )
            }
            for name in normalized:
                state = current[name]
                await self._append_event(
                    session,
                    actor,
                    result,
                    name,
                    state.current_generation_id,
                    int(state.revision),
                    "skill.secret.replace" if existing.get(name) else "skill.secret.configure",
                    "replaced" if existing.get(name) else "created",
                )
            return result

        return await self._execute(actor, operation)

    async def clear(
        self,
        actor: ProjectContext,
        skill_id: uuid.UUID,
        version_id: uuid.UUID,
        secret_name: str,
        *,
        confirmed: bool,
    ) -> SkillSecretSetView:
        self._require(actor, Capability.SHARED_ASSETS_MANAGE_BINDINGS)
        if confirmed is not True:
            raise SkillSecretConfigurationInvalid(actor.request_id)

        async def operation(session: AsyncSession) -> SkillSecretSetView:
            await self._lock_project(session, actor)
            target = await self._target(session, actor, skill_id, version_id)
            await self._require_replaceable(session, actor, target)
            requirements = _requirements(target.version, actor.request_id)
            if secret_name not in {name for name, _optional in requirements}:
                raise SkillSecretConfigurationInvalid(actor.request_id)
            states = await SkillSecretStore(session).list_states(
                project_id=actor.project_id,
                skill_id=skill_id,
                skill_version_id=version_id,
                for_update=True,
            )
            previous_generation_id = next(
                (row.current_generation_id for row in states if row.secret_name == secret_name),
                None,
            )
            if not any(row.secret_name == secret_name for row in states):
                await SkillSecretStore(session).ensure_states(
                    project_id=actor.project_id,
                    skill_id=skill_id,
                    skill_version_id=version_id,
                    requirements=requirements,
                    actor_user_id=str(actor.user_id),
                )
            await SkillSecretStore(session).clear(
                project_id=actor.project_id,
                skill_id=skill_id,
                skill_version_id=version_id,
                secret_name=secret_name,
                actor_user_id=str(actor.user_id),
                request_id=actor.request_id,
            )
            result = await self._view(session, actor, target, for_update=True)
            requirement = next(item for item in result.requirements if item.name == secret_name)
            await self._append_event(
                session,
                actor,
                result,
                secret_name,
                previous_generation_id,
                requirement.revision,
                "skill.secret.clear",
                "cleared",
            )
            return result

        return await self._execute(actor, operation)

    async def _view(
        self,
        session: AsyncSession,
        actor: ProjectContext,
        target: SkillSecretTarget,
        *,
        for_update: bool = False,
    ) -> SkillSecretSetView:
        requirements = _requirements(target.version, actor.request_id)
        states = await SkillSecretStore(session).list_states(
            project_id=actor.project_id,
            skill_id=target.asset.id,
            skill_version_id=target.version.id,
            for_update=for_update,
        )
        by_name = {row.secret_name: row for row in states}
        if set(by_name) - {name for name, _optional in requirements}:
            raise SkillSecretConfigurationInvalid(actor.request_id)
        views = tuple(
            SkillSecretRequirementStatus(
                name=name,
                optional=optional,
                configured=(name in by_name and by_name[name].current_generation_id is not None),
                revision=0 if name not in by_name else int(by_name[name].revision),
            )
            for name, optional in requirements
        )
        ready = all(item.optional or item.configured for item in views)
        return SkillSecretSetView(
            skill_id=target.asset.id,
            skill_version_id=target.version.id,
            revision=aggregate_skill_secret_revision(states),
            readiness="ready" if ready else "unready",
            requirements=views,
        )

    async def _target(
        self,
        session: AsyncSession,
        actor: ProjectContext,
        skill_id: uuid.UUID,
        version_id: uuid.UUID | None,
        *,
        read: bool = False,
    ) -> SkillSecretTarget:
        if not isinstance(skill_id, uuid.UUID) or (version_id is not None and not isinstance(version_id, uuid.UUID)):
            raise AssetValidationFailed(actor.request_id)
        statement = (
            select(SkillRow)
            .where(
                SkillRow.id == skill_id,
                or_(
                    and_(
                        SkillRow.scope == "project",
                        SkillRow.project_id == actor.project_id,
                        SkillRow.status.in_(("active", "suspended")),
                    ),
                    and_(
                        SkillRow.scope == "system",
                        SkillRow.project_id.is_(None),
                        SkillRow.status == "active",
                    ),
                ),
            )
            .with_for_update(read=read, of=SkillRow)
        )
        asset = (await session.execute(statement)).scalar_one_or_none()
        if asset is None:
            raise AssetNotFound(actor.request_id)
        selected = asset.current_version_id if version_id is None else version_id
        if selected is None:
            raise AssetNotFound(actor.request_id)
        version = (
            await session.execute(
                select(SkillVersionRow)
                .where(
                    SkillVersionRow.id == selected,
                    SkillVersionRow.skill_id == asset.id,
                    SkillVersionRow.revoked_at.is_(None),
                )
                .with_for_update(read=read, of=SkillVersionRow)
            )
        ).scalar_one_or_none()
        if version is None or (asset.scope == AssetScope.SYSTEM.value and version.id != asset.current_version_id):
            raise AssetConflict(actor.request_id)
        if asset.scope == AssetScope.SYSTEM.value:
            await self._require_system_binding(
                session,
                actor,
                asset,
                read=read,
            )
        return SkillSecretTarget(asset, version)

    @staticmethod
    async def _require_system_binding(
        session: AsyncSession,
        actor: ProjectContext,
        asset: SkillRow,
        *,
        read: bool,
    ) -> None:
        statement = (
            select(ProjectSystemSkillBindingRow.project_id)
            .where(
                ProjectSystemSkillBindingRow.project_id == actor.project_id,
                ProjectSystemSkillBindingRow.system_skill_id == asset.id,
                ProjectSystemSkillBindingRow.enabled.is_(True),
            )
            .with_for_update(read=read, of=ProjectSystemSkillBindingRow)
        )
        if await session.scalar(statement) is None:
            raise AssetNotFound(actor.request_id)

    @staticmethod
    async def _relations(
        session: AsyncSession,
        asset: SkillRow,
    ) -> Mapping[uuid.UUID, VersionRelation]:
        rows = tuple((await session.execute(select(SkillVersionRow).where(SkillVersionRow.skill_id == asset.id).order_by(SkillVersionRow.version_number))).scalars().all())
        try:
            return classify_version_relations(
                scope=AssetScope(asset.scope),
                current_version_id=asset.current_version_id,
                nodes=tuple(
                    VersionLineageNode(
                        row.id,
                        row.version_number,
                        row.supersedes_version_id,
                    )
                    for row in rows
                ),
            )
        except (TypeError, ValueError):
            raise SkillSecretConfigurationInvalid("unknown") from None

    async def _require_replaceable(
        self,
        session: AsyncSession,
        actor: ProjectContext,
        target: SkillSecretTarget,
    ) -> None:
        if target.asset.scope == AssetScope.SYSTEM.value:
            if target.version.id != target.asset.current_version_id:
                raise AssetConflict(actor.request_id)
            return
        relation = (await self._relations(session, target.asset)).get(target.version.id)
        if relation not in {VersionRelation.CURRENT, VersionRelation.CANDIDATE}:
            raise AssetConflict(actor.request_id)

    @staticmethod
    async def _lock_project(
        session: AsyncSession,
        actor: ProjectContext,
        *,
        read: bool = False,
    ) -> None:
        value = await session.scalar(
            select(ProjectRow.id)
            .join(ProjectMembershipRow, ProjectMembershipRow.project_id == ProjectRow.id)
            .where(
                ProjectRow.id == actor.project_id,
                ProjectRow.status == "active",
                ProjectRow.is_suspended.is_(False),
                ProjectMembershipRow.id == actor.membership_id,
                ProjectMembershipRow.user_id == str(actor.user_id),
                ProjectMembershipRow.status == "active",
                ProjectMembershipRow.version == actor.membership_version,
            )
            .with_for_update(read=read, of=[ProjectRow, ProjectMembershipRow])
        )
        if value is None:
            raise AssetForbidden(actor.request_id)

    async def _execute(
        self,
        actor: ProjectContext,
        operation: Callable[[AsyncSession], Awaitable[_T]],
    ) -> _T:
        try:
            async with self._session_factory() as session, session.begin():
                return await operation(session)
        except SharedAssetError:
            raise
        except (DBAPIError, IntegrityError, SATimeoutError):
            raise AssetStorageUnavailable(actor.request_id) from None

    async def _append_event(
        self,
        session: AsyncSession,
        actor: ProjectContext,
        result: SkillSecretSetView,
        secret_name: str,
        generation_id: uuid.UUID | None,
        revision: int,
        action: str,
        reason: str,
    ) -> None:
        await self._governance_sink.append_project(
            session,
            actor=actor.user_id,
            project_id=actor.project_id,
            asset_id=result.skill_id,
            version_id=result.skill_version_id,
            action=action,
            request_id=actor.request_id,
            asset_kind="skill",
            secret_metadata={
                "version_id": result.skill_version_id,
                "secret_name": secret_name,
                "generation_id": generation_id,
                "revision": revision,
                "result": "cleared" if action.endswith(".clear") else "configured",
                "reason": reason,
                "readiness": result.readiness,
            },
        )

    @staticmethod
    def _require(actor: ProjectContext, capability: Capability) -> None:
        if not isinstance(actor, ProjectContext) or capability not in actor.capabilities:
            raise AssetForbidden(getattr(actor, "request_id", "unknown"))


__all__ = [
    "SkillActivationReadinessView",
    "SkillSecretService",
    "SkillSecretSetView",
    "aggregate_skill_secret_revision",
    "copy_compatible_skill_secrets_in_transaction",
    "validate_skill_secret_readiness_in_transaction",
]
