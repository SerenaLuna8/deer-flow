from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.context import ProjectContext
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.errors import AssetForbidden, AssetNotFound
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.shared_assets import (
    CredentialEnvelopeRow,
    CredentialRow,
    CredentialVersionRow,
    ProjectSkillCredentialBindingRow,
    ProjectSkillCredentialConfigRow,
    SkillRow,
    SkillVersionRow,
)


@dataclass(frozen=True)
class SkillCredentialTarget:
    asset: SkillRow
    version: SkillVersionRow


@dataclass(frozen=True)
class EligibleSkillCredentialRecord:
    credential: CredentialRow
    version: CredentialVersionRow


class SkillCredentialRepository:
    """Project-scoped Skill secret bindings with immutable binding history."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _require_actor(context: ProjectContext) -> None:
        if not isinstance(context, ProjectContext):
            raise AssetForbidden(getattr(context, "request_id", "unknown"))

    @staticmethod
    def _require_binding_actor(
        context: ProjectContext | SystemAssetGovernanceContext,
    ) -> None:
        if isinstance(context, ProjectContext):
            return
        if isinstance(context, SystemAssetGovernanceContext) and context.project_id is not None:
            return
        raise AssetForbidden(getattr(context, "request_id", "unknown"))

    @staticmethod
    def _project_context_exists(context: ProjectContext):
        return exists(
            select(1)
            .select_from(ProjectMembershipRow)
            .join(ProjectRow, ProjectRow.id == ProjectMembershipRow.project_id)
            .where(
                ProjectMembershipRow.id == context.membership_id,
                ProjectMembershipRow.project_id == context.project_id,
                ProjectMembershipRow.user_id == str(context.user_id),
                ProjectMembershipRow.status == "active",
                ProjectMembershipRow.version == context.membership_version,
                ProjectRow.id == context.project_id,
                ProjectRow.status == "active",
                ProjectRow.is_suspended.is_(False),
            )
        )

    @classmethod
    def _binding_project_exists(
        cls,
        context: ProjectContext | SystemAssetGovernanceContext,
    ):
        cls._require_binding_actor(context)
        if isinstance(context, ProjectContext):
            return cls._project_context_exists(context)
        return exists(
            select(1)
            .select_from(ProjectRow)
            .where(
                ProjectRow.id == context.project_id,
                ProjectRow.status == "active",
                ProjectRow.is_suspended.is_(False),
            )
        )

    async def lock_project(
        self,
        context: ProjectContext,
        *,
        read: bool = False,
    ) -> None:
        self._require_actor(context)
        statement = (
            select(ProjectRow.id)
            .join(
                ProjectMembershipRow,
                ProjectMembershipRow.project_id == ProjectRow.id,
            )
            .where(
                ProjectRow.id == context.project_id,
                ProjectRow.status == "active",
                ProjectRow.is_suspended.is_(False),
                ProjectMembershipRow.id == context.membership_id,
                ProjectMembershipRow.user_id == str(context.user_id),
                ProjectMembershipRow.status == "active",
                ProjectMembershipRow.version == context.membership_version,
            )
            .with_for_update(
                read=read,
                of=[ProjectRow, ProjectMembershipRow],
            )
        )
        if (await self.session.execute(statement)).scalar_one_or_none() is None:
            raise AssetNotFound(context.request_id)

    async def lock_configurable_current_skill(
        self,
        context: ProjectContext,
        skill_id: uuid.UUID,
        *,
        read: bool = False,
    ) -> SkillCredentialTarget:
        self._require_actor(context)
        asset_statement = (
            select(SkillRow)
            .where(
                SkillRow.id == skill_id,
                or_(
                    and_(
                        SkillRow.scope == "project",
                        SkillRow.project_id == context.project_id,
                        SkillRow.status.in_(("active", "suspended")),
                    ),
                    and_(
                        SkillRow.scope == "system",
                        SkillRow.project_id.is_(None),
                        SkillRow.status == "active",
                    ),
                ),
                self._project_context_exists(context),
            )
            .with_for_update(read=read, of=SkillRow)
        )
        asset = (await self.session.execute(asset_statement)).scalar_one_or_none()
        if asset is None or asset.current_version_id is None:
            raise AssetNotFound(context.request_id)
        version_statement = (
            select(SkillVersionRow)
            .where(
                SkillVersionRow.id == asset.current_version_id,
                SkillVersionRow.skill_id == asset.id,
                SkillVersionRow.revoked_at.is_(None),
            )
            .with_for_update(read=read, of=SkillVersionRow)
        )
        version = (await self.session.execute(version_statement)).scalar_one_or_none()
        if version is None:
            raise AssetNotFound(context.request_id)
        return SkillCredentialTarget(asset, version)

    async def lock_configurable_project_skill_version(
        self,
        context: ProjectContext,
        skill_id: uuid.UUID,
        skill_version_id: uuid.UUID,
        *,
        read: bool = False,
    ) -> SkillCredentialTarget:
        """Lock one exact project Skill version after the project gate.

        Callers acquire ``lock_project`` first. Keeping that gate separate lets
        a composite activation transaction reuse the Project lock already held by
        ``SkillRepository`` instead of attempting an unsafe lock upgrade.
        """

        self._require_actor(context)
        asset_statement = (
            select(SkillRow)
            .where(
                SkillRow.id == skill_id,
                SkillRow.scope == "project",
                SkillRow.project_id == context.project_id,
                SkillRow.status.in_(("active", "suspended")),
                self._project_context_exists(context),
            )
            .with_for_update(read=read, of=SkillRow)
        )
        asset = (await self.session.execute(asset_statement)).scalar_one_or_none()
        if asset is None:
            raise AssetNotFound(context.request_id)
        version_statement = (
            select(SkillVersionRow)
            .where(
                SkillVersionRow.id == skill_version_id,
                SkillVersionRow.skill_id == asset.id,
                SkillVersionRow.revoked_at.is_(None),
            )
            .with_for_update(read=read, of=SkillVersionRow)
        )
        version = (await self.session.execute(version_statement)).scalar_one_or_none()
        if version is None:
            raise AssetNotFound(context.request_id)
        return SkillCredentialTarget(asset, version)

    async def lock_configurable_exact_skill_version(
        self,
        context: ProjectContext,
        skill_id: uuid.UUID,
        skill_version_id: uuid.UUID,
        *,
        read: bool = False,
    ) -> SkillCredentialTarget:
        """Lock an exact Project Skill or a System Skill version.

        Services keep Project Candidate/Historical and System Current policy
        explicit after this scoped lookup. The project membership predicate is
        retained even for a packaged System Skill because mappings are owned by
        the requesting project.
        """

        self._require_actor(context)
        asset_statement = (
            select(SkillRow)
            .where(
                SkillRow.id == skill_id,
                or_(
                    and_(
                        SkillRow.scope == "project",
                        SkillRow.project_id == context.project_id,
                        SkillRow.status.in_(("active", "suspended")),
                    ),
                    and_(
                        SkillRow.scope == "system",
                        SkillRow.project_id.is_(None),
                        SkillRow.status == "active",
                    ),
                ),
                self._project_context_exists(context),
            )
            .with_for_update(read=read, of=SkillRow)
        )
        asset = (await self.session.execute(asset_statement)).scalar_one_or_none()
        if asset is None:
            raise AssetNotFound(context.request_id)
        version_statement = (
            select(SkillVersionRow)
            .where(
                SkillVersionRow.id == skill_version_id,
                SkillVersionRow.skill_id == asset.id,
                SkillVersionRow.revoked_at.is_(None),
            )
            .with_for_update(read=read, of=SkillVersionRow)
        )
        version = (await self.session.execute(version_statement)).scalar_one_or_none()
        if version is None:
            raise AssetNotFound(context.request_id)
        return SkillCredentialTarget(asset, version)

    async def get_config(
        self,
        context: ProjectContext | SystemAssetGovernanceContext,
        skill_id: uuid.UUID,
        skill_version_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> ProjectSkillCredentialConfigRow | None:
        self._require_binding_actor(context)
        statement = select(ProjectSkillCredentialConfigRow).where(
            ProjectSkillCredentialConfigRow.project_id == context.project_id,
            ProjectSkillCredentialConfigRow.skill_id == skill_id,
            ProjectSkillCredentialConfigRow.skill_version_id == skill_version_id,
            self._binding_project_exists(context),
        )
        if for_update:
            statement = statement.with_for_update(
                of=ProjectSkillCredentialConfigRow,
            )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def active_bindings(
        self,
        context: ProjectContext | SystemAssetGovernanceContext,
        skill_id: uuid.UUID,
        skill_version_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> tuple[ProjectSkillCredentialBindingRow, ...]:
        self._require_binding_actor(context)
        statement = (
            select(ProjectSkillCredentialBindingRow)
            .where(
                ProjectSkillCredentialBindingRow.project_id == context.project_id,
                ProjectSkillCredentialBindingRow.skill_id == skill_id,
                ProjectSkillCredentialBindingRow.skill_version_id == skill_version_id,
                ProjectSkillCredentialBindingRow.status == "active",
                ProjectSkillCredentialBindingRow.admission_only.is_(False),
                self._binding_project_exists(context),
            )
            .order_by(
                ProjectSkillCredentialBindingRow.secret_name,
                ProjectSkillCredentialBindingRow.id,
            )
        )
        if for_update:
            statement = statement.with_for_update(
                of=ProjectSkillCredentialBindingRow,
            )
        return tuple((await self.session.execute(statement)).scalars().all())

    async def eligible_credentials(
        self,
        context: ProjectContext,
    ) -> tuple[EligibleSkillCredentialRecord, ...]:
        self._require_actor(context)
        statement = (
            select(CredentialRow, CredentialVersionRow)
            .join(
                CredentialVersionRow,
                (CredentialVersionRow.id == CredentialRow.current_version_id) & (CredentialVersionRow.credential_id == CredentialRow.id),
            )
            .where(
                CredentialRow.scope == "project",
                CredentialRow.project_id == context.project_id,
                CredentialRow.status == "active",
                CredentialRow.is_delete.is_(False),
                CredentialVersionRow.status == "active",
                self._project_context_exists(context),
                exists(
                    select(1).where(
                        CredentialEnvelopeRow.credential_version_id == CredentialVersionRow.id,
                        CredentialEnvelopeRow.is_active.is_(True),
                    )
                ),
            )
            .order_by(
                CredentialRow.display_name,
                CredentialRow.id,
                CredentialVersionRow.version_number,
            )
        )
        return tuple(EligibleSkillCredentialRecord(credential, version) for credential, version in (await self.session.execute(statement)).all())

    async def lock_selected_credentials(
        self,
        context: ProjectContext | SystemAssetGovernanceContext,
        version_ids: Sequence[uuid.UUID],
    ) -> Mapping[uuid.UUID, EligibleSkillCredentialRecord]:
        self._require_binding_actor(context)
        ordered_ids = tuple(
            sorted(
                {uuid.UUID(str(version_id)) for version_id in version_ids},
                key=lambda value: value.int,
            )
        )
        if not ordered_ids:
            return {}
        references = {
            uuid.UUID(str(version_id)): uuid.UUID(str(credential_id))
            for version_id, credential_id in (
                await self.session.execute(
                    select(
                        CredentialVersionRow.id,
                        CredentialVersionRow.credential_id,
                    )
                    .join(
                        CredentialRow,
                        CredentialRow.id == CredentialVersionRow.credential_id,
                    )
                    .where(
                        CredentialVersionRow.id.in_(ordered_ids),
                        CredentialRow.scope == "project",
                        CredentialRow.project_id == context.project_id,
                        CredentialRow.is_delete.is_(False),
                        self._binding_project_exists(context),
                    )
                )
            ).all()
        }
        if set(references) != set(ordered_ids):
            raise AssetNotFound(context.request_id)

        credentials: dict[uuid.UUID, CredentialRow] = {}
        for credential_id in sorted(
            set(references.values()),
            key=lambda value: value.int,
        ):
            credential = (
                await self.session.execute(
                    select(CredentialRow)
                    .where(
                        CredentialRow.id == credential_id,
                        CredentialRow.scope == "project",
                        CredentialRow.project_id == context.project_id,
                        CredentialRow.is_delete.is_(False),
                        self._binding_project_exists(context),
                    )
                    .with_for_update(of=CredentialRow)
                )
            ).scalar_one_or_none()
            if credential is None:
                raise AssetNotFound(context.request_id)
            credentials[credential_id] = credential

        result: dict[uuid.UUID, EligibleSkillCredentialRecord] = {}
        for version_id, credential_id in sorted(
            references.items(),
            key=lambda item: (item[1].int, item[0].int),
        ):
            version = (
                await self.session.execute(
                    select(CredentialVersionRow)
                    .where(
                        CredentialVersionRow.id == version_id,
                        CredentialVersionRow.credential_id == credential_id,
                    )
                    .with_for_update(of=CredentialVersionRow)
                )
            ).scalar_one_or_none()
            if version is None:
                raise AssetNotFound(context.request_id)
            result[version_id] = EligibleSkillCredentialRecord(
                credentials[credential_id],
                version,
            )
        return result

    async def active_envelope_exists(
        self,
        credential_version_id: uuid.UUID,
    ) -> bool:
        return bool(
            await self.session.scalar(
                select(
                    exists(
                        select(1)
                        .select_from(CredentialEnvelopeRow)
                        .join(
                            CredentialVersionRow,
                            CredentialVersionRow.id == CredentialEnvelopeRow.credential_version_id,
                        )
                        .join(
                            CredentialRow,
                            CredentialRow.id == CredentialVersionRow.credential_id,
                        )
                        .where(
                            CredentialEnvelopeRow.credential_version_id == credential_version_id,
                            CredentialEnvelopeRow.is_active.is_(True),
                            CredentialRow.is_delete.is_(False),
                        )
                    )
                )
            )
        )

    async def lock_active_envelopes(
        self,
        credential_version_ids: Sequence[uuid.UUID],
    ) -> frozenset[uuid.UUID]:
        ordered_ids = tuple(
            sorted(
                {uuid.UUID(str(version_id)) for version_id in credential_version_ids},
                key=lambda value: value.int,
            )
        )
        if not ordered_ids:
            return frozenset()
        rows = tuple(
            (
                await self.session.execute(
                    select(CredentialEnvelopeRow)
                    .where(
                        CredentialEnvelopeRow.credential_version_id.in_(
                            ordered_ids,
                        ),
                        CredentialEnvelopeRow.is_active.is_(True),
                    )
                    .order_by(
                        CredentialEnvelopeRow.credential_version_id,
                        CredentialEnvelopeRow.envelope_generation,
                        CredentialEnvelopeRow.id,
                    )
                    .with_for_update(of=CredentialEnvelopeRow)
                )
            )
            .scalars()
            .all()
        )
        return frozenset(uuid.UUID(str(row.credential_version_id)) for row in rows)

    async def create_config(
        self,
        context: ProjectContext | SystemAssetGovernanceContext,
        target: SkillCredentialTarget,
    ) -> ProjectSkillCredentialConfigRow:
        self._require_binding_actor(context)
        row = ProjectSkillCredentialConfigRow(
            project_id=context.project_id,
            skill_id=target.asset.id,
            skill_version_id=target.version.id,
            revision=1,
            created_by_user_id=str(context.user_id),
            updated_by_user_id=str(context.user_id),
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def replace_bindings(
        self,
        context: ProjectContext | SystemAssetGovernanceContext,
        config: ProjectSkillCredentialConfigRow,
        target: SkillCredentialTarget,
        bindings: Sequence[tuple[str, str, EligibleSkillCredentialRecord]],
        *,
        now: datetime,
        existing: Sequence[ProjectSkillCredentialBindingRow],
        new_revision: int,
    ) -> tuple[ProjectSkillCredentialBindingRow, ...]:
        self._require_binding_actor(context)
        live_binding_ids = [row.id for row in existing]
        retired = (
            tuple(
                (
                    await self.session.execute(
                        select(ProjectSkillCredentialBindingRow)
                        .where(
                            ProjectSkillCredentialBindingRow.project_id == context.project_id,
                            ProjectSkillCredentialBindingRow.skill_id == target.asset.id,
                            ProjectSkillCredentialBindingRow.runtime_authority_binding_id.in_(
                                live_binding_ids,
                            ),
                            ProjectSkillCredentialBindingRow.admission_only.is_(
                                True,
                            ),
                            ProjectSkillCredentialBindingRow.status == "active",
                        )
                        .order_by(ProjectSkillCredentialBindingRow.id)
                        .with_for_update(of=ProjectSkillCredentialBindingRow)
                    )
                )
                .scalars()
                .all()
            )
            if live_binding_ids
            else ()
        )
        for row in (*existing, *retired):
            row.status = "revoked"
            row.revoked_at = now
            row.revoked_by_user_id = str(context.user_id)
        config.skill_version_id = target.version.id
        config.revision = new_revision
        config.updated_by_user_id = str(context.user_id)
        await self.session.flush()
        created = tuple(
            ProjectSkillCredentialBindingRow(
                project_id=context.project_id,
                skill_id=target.asset.id,
                skill_version_id=target.version.id,
                secret_name=name,
                source_env_field_name=source_env_field_name,
                credential_id=record.credential.id,
                credential_version_id=record.version.id,
                config_revision=new_revision,
                created_by_user_id=str(context.user_id),
            )
            for name, source_env_field_name, record in bindings
        )
        self.session.add_all(created)
        await self.session.flush()
        return created
