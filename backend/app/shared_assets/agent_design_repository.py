from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import and_, exists, func, literal, or_, select, text, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.context import ProjectContext
from app.shared_assets.errors import AssetForbidden, AssetNotFound
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.shared_assets import (
    AgentDesignOperationRow,
    AgentDesignSessionRow,
    AgentRow,
    McpServerRow,
    McpServerVersionRow,
    ProjectSystemMcpBindingRow,
    ProjectSystemSkillBindingRow,
    SkillRow,
    SkillVersionRow,
)


@dataclass(frozen=True, slots=True)
class AgentDesignAllowedAssetRecord:
    """One exact dependency version safe to disclose to Agent Builder."""

    kind: Literal["skill", "mcp"]
    scope: Literal["project", "system"]
    asset_id: uuid.UUID
    version_id: uuid.UUID
    name: str
    slug: str
    description: str


class AgentDesignRepository:
    """Owner-scoped persistence for conversational Agent design sessions."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _require_context(context: ProjectContext) -> None:
        if not isinstance(context, ProjectContext):
            raise AssetForbidden(getattr(context, "request_id", "unknown"))

    @staticmethod
    def _context_exists(context: ProjectContext):
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

    async def lock_context(self, context: ProjectContext) -> None:
        self._require_context(context)
        statement = (
            select(ProjectRow.id)
            .join(ProjectMembershipRow, ProjectMembershipRow.project_id == ProjectRow.id)
            .where(
                ProjectRow.id == context.project_id,
                ProjectRow.status == "active",
                ProjectRow.is_suspended.is_(False),
                ProjectMembershipRow.id == context.membership_id,
                ProjectMembershipRow.project_id == context.project_id,
                ProjectMembershipRow.user_id == str(context.user_id),
                ProjectMembershipRow.status == "active",
                ProjectMembershipRow.version == context.membership_version,
            )
            .with_for_update(read=True, of=[ProjectRow, ProjectMembershipRow])
        )
        if (await self.session.execute(statement)).scalar_one_or_none() is None:
            raise AssetNotFound(context.request_id)

    async def lock_session_create_scope(
        self,
        context: ProjectContext,
    ) -> None:
        """Serialize per-project Builder admission before counting sessions."""

        self._require_context(context)
        statement = (
            select(ProjectRow.id)
            .join(ProjectMembershipRow, ProjectMembershipRow.project_id == ProjectRow.id)
            .where(
                ProjectRow.id == context.project_id,
                ProjectRow.status == "active",
                ProjectRow.is_suspended.is_(False),
                ProjectMembershipRow.id == context.membership_id,
                ProjectMembershipRow.project_id == context.project_id,
                ProjectMembershipRow.user_id == str(context.user_id),
                ProjectMembershipRow.status == "active",
                ProjectMembershipRow.version == context.membership_version,
            )
            .with_for_update(of=ProjectRow)
        )
        if (await self.session.execute(statement)).scalar_one_or_none() is None:
            raise AssetNotFound(context.request_id)

    async def count_incomplete(
        self,
        context: ProjectContext,
    ) -> int:
        self._require_context(context)
        value = await self.session.scalar(
            select(func.count())
            .select_from(AgentDesignSessionRow)
            .where(
                AgentDesignSessionRow.project_id == context.project_id,
                AgentDesignSessionRow.owner_user_id == str(context.user_id),
                AgentDesignSessionRow.status.not_in(("completed", "cancelled")),
                self._context_exists(context),
            )
        )
        return int(value or 0)

    async def create(
        self,
        context: ProjectContext,
        row: AgentDesignSessionRow,
    ) -> AgentDesignSessionRow:
        await self.lock_context(context)
        if row.project_id != context.project_id or row.owner_user_id != str(context.user_id):
            raise AssetForbidden(context.request_id)
        self.session.add(row)
        await self.session.flush()
        return row

    async def get_by_create_idempotency(
        self,
        context: ProjectContext,
        idempotency_key_hash: str,
        *,
        for_update: bool = False,
    ) -> AgentDesignSessionRow | None:
        self._require_context(context)
        if for_update:
            await self.lock_context(context)
        statement = select(AgentDesignSessionRow).where(
            AgentDesignSessionRow.project_id == context.project_id,
            AgentDesignSessionRow.owner_user_id == str(context.user_id),
            AgentDesignSessionRow.create_idempotency_key_hash == idempotency_key_hash,
            self._context_exists(context),
        )
        if for_update:
            statement = statement.with_for_update(of=AgentDesignSessionRow).execution_options(populate_existing=True)
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def get(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> AgentDesignSessionRow:
        self._require_context(context)
        if for_update:
            await self.lock_context(context)
        statement = select(AgentDesignSessionRow).where(
            AgentDesignSessionRow.id == session_id,
            AgentDesignSessionRow.project_id == context.project_id,
            AgentDesignSessionRow.owner_user_id == str(context.user_id),
            self._context_exists(context),
        )
        if for_update:
            statement = statement.with_for_update(of=AgentDesignSessionRow).execution_options(populate_existing=True)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise AssetNotFound(context.request_id)
        return row

    async def list_incomplete(
        self,
        context: ProjectContext,
        *,
        limit: int = 20,
        before_created_at: datetime | None = None,
        before_id: uuid.UUID | None = None,
    ) -> tuple[AgentDesignSessionRow, ...]:
        self._require_context(context)
        filters = [
            AgentDesignSessionRow.project_id == context.project_id,
            AgentDesignSessionRow.owner_user_id == str(context.user_id),
            AgentDesignSessionRow.status.notin_(("completed", "cancelled")),
            self._context_exists(context),
        ]
        if before_created_at is not None and before_id is not None:
            filters.append(
                or_(
                    AgentDesignSessionRow.created_at < before_created_at,
                    and_(
                        AgentDesignSessionRow.created_at == before_created_at,
                        AgentDesignSessionRow.id < before_id,
                    ),
                )
            )
        statement = (
            select(AgentDesignSessionRow)
            .where(*filters)
            .order_by(
                AgentDesignSessionRow.created_at.desc(),
                AgentDesignSessionRow.id.desc(),
            )
            .limit(limit)
        )
        return tuple((await self.session.execute(statement)).scalars())

    async def project_agent_slug_exists(
        self,
        context: ProjectContext,
        slug: str,
        *,
        for_update: bool = False,
    ) -> bool:
        """Check the exact project Agent namespace under current authority."""

        self._require_context(context)
        if for_update:
            await self.lock_context(context)
        statement = select(AgentRow.id).where(
            AgentRow.scope == "project",
            AgentRow.project_id == context.project_id,
            AgentRow.status != "archived",
            func.lower(AgentRow.slug) == slug.casefold(),
            self._context_exists(context),
        )
        if for_update:
            statement = statement.with_for_update(of=AgentRow)
        return (await self.session.execute(statement)).scalar_one_or_none() is not None

    async def list_allowed_assets(
        self,
        context: ProjectContext,
        *,
        limit: int,
    ) -> tuple[AgentDesignAllowedAssetRecord, ...]:
        """List exact active/published Skill and MCP versions usable by a project.

        Project assets resolve through their current published pointer. System
        assets resolve through this project's enabled exact-version binding;
        the global catalog alone never grants Builder visibility or use.
        """

        self._require_context(context)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("limit must be a positive integer")

        project_skills = (
            select(
                literal(0).label("kind_rank"),
                literal(0).label("scope_rank"),
                literal("skill").label("kind"),
                literal("project").label("scope"),
                SkillRow.id.label("asset_id"),
                SkillVersionRow.id.label("version_id"),
                SkillRow.display_name.label("name"),
                SkillRow.slug,
                SkillVersionRow.description,
            )
            .select_from(SkillRow)
            .join(
                SkillVersionRow,
                and_(
                    SkillVersionRow.skill_id == SkillRow.id,
                    SkillRow.current_published_version_id == SkillVersionRow.id,
                ),
            )
            .where(
                SkillRow.scope == "project",
                SkillRow.project_id == context.project_id,
                SkillRow.status == "active",
                SkillVersionRow.workflow_status == "published",
                SkillVersionRow.revoked_at.is_(None),
            )
        )
        system_skills = (
            select(
                literal(0).label("kind_rank"),
                literal(1).label("scope_rank"),
                literal("skill").label("kind"),
                literal("system").label("scope"),
                SkillRow.id.label("asset_id"),
                SkillVersionRow.id.label("version_id"),
                SkillRow.display_name.label("name"),
                SkillRow.slug,
                SkillVersionRow.description,
            )
            .select_from(ProjectSystemSkillBindingRow)
            .join(
                SkillRow,
                and_(
                    SkillRow.id == ProjectSystemSkillBindingRow.system_skill_id,
                    SkillRow.scope == "system",
                    SkillRow.project_id.is_(None),
                ),
            )
            .join(
                SkillVersionRow,
                and_(
                    SkillVersionRow.skill_id == SkillRow.id,
                    SkillVersionRow.id == ProjectSystemSkillBindingRow.skill_version_id,
                ),
            )
            .where(
                ProjectSystemSkillBindingRow.project_id == context.project_id,
                ProjectSystemSkillBindingRow.enabled.is_(True),
                SkillRow.status == "active",
                SkillVersionRow.workflow_status == "published",
                SkillVersionRow.revoked_at.is_(None),
            )
        )
        project_mcps = (
            select(
                literal(1).label("kind_rank"),
                literal(0).label("scope_rank"),
                literal("mcp").label("kind"),
                literal("project").label("scope"),
                McpServerRow.id.label("asset_id"),
                McpServerVersionRow.id.label("version_id"),
                McpServerRow.display_name.label("name"),
                McpServerRow.slug,
                McpServerVersionRow.description,
            )
            .select_from(McpServerRow)
            .join(
                McpServerVersionRow,
                and_(
                    McpServerVersionRow.mcp_server_id == McpServerRow.id,
                    McpServerRow.current_published_version_id == McpServerVersionRow.id,
                ),
            )
            .where(
                McpServerRow.scope == "project",
                McpServerRow.project_id == context.project_id,
                McpServerRow.status == "active",
                McpServerVersionRow.workflow_status == "published",
            )
        )
        system_mcps = (
            select(
                literal(1).label("kind_rank"),
                literal(1).label("scope_rank"),
                literal("mcp").label("kind"),
                literal("system").label("scope"),
                McpServerRow.id.label("asset_id"),
                McpServerVersionRow.id.label("version_id"),
                McpServerRow.display_name.label("name"),
                McpServerRow.slug,
                McpServerVersionRow.description,
            )
            .select_from(ProjectSystemMcpBindingRow)
            .join(
                McpServerRow,
                and_(
                    McpServerRow.id == ProjectSystemMcpBindingRow.system_mcp_server_id,
                    McpServerRow.scope == "system",
                    McpServerRow.project_id.is_(None),
                ),
            )
            .join(
                McpServerVersionRow,
                and_(
                    McpServerVersionRow.mcp_server_id == McpServerRow.id,
                    McpServerVersionRow.id == ProjectSystemMcpBindingRow.mcp_server_version_id,
                ),
            )
            .where(
                ProjectSystemMcpBindingRow.project_id == context.project_id,
                ProjectSystemMcpBindingRow.enabled.is_(True),
                McpServerRow.status == "active",
                McpServerVersionRow.workflow_status == "published",
            )
        )
        catalog = union_all(
            project_skills,
            system_skills,
            project_mcps,
            system_mcps,
        ).subquery("agent_design_allowed_assets")
        statement = (
            select(
                catalog.c.kind,
                catalog.c.scope,
                catalog.c.asset_id,
                catalog.c.version_id,
                catalog.c.name,
                catalog.c.slug,
                catalog.c.description,
            )
            .where(self._context_exists(context))
            .order_by(
                catalog.c.kind_rank,
                catalog.c.scope_rank,
                func.lower(catalog.c.slug),
                catalog.c.asset_id,
                catalog.c.version_id,
            )
            .limit(limit)
        )
        return tuple(
            AgentDesignAllowedAssetRecord(
                kind=row.kind,
                scope=row.scope,
                asset_id=row.asset_id,
                version_id=row.version_id,
                name=row.name,
                slug=row.slug,
                description=row.description,
            )
            for row in (await self.session.execute(statement)).all()
        )

    async def get_operation(
        self,
        context: ProjectContext,
        *,
        operation_kind: str,
        idempotency_key_hash: str,
        for_update: bool = False,
    ) -> AgentDesignOperationRow | None:
        self._require_context(context)
        if for_update:
            await self.lock_context(context)
        statement = select(AgentDesignOperationRow).where(
            AgentDesignOperationRow.project_id == context.project_id,
            AgentDesignOperationRow.owner_user_id == str(context.user_id),
            AgentDesignOperationRow.operation_kind == operation_kind,
            AgentDesignOperationRow.idempotency_key_hash == idempotency_key_hash,
            self._context_exists(context),
        )
        if for_update:
            statement = statement.with_for_update(of=AgentDesignOperationRow)
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def lock_in_progress_turn_operations(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
    ) -> tuple[AgentDesignOperationRow, ...]:
        """Fence generation, then lock active operations before the session."""

        self._require_context(context)
        await self.lock_context(context)
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {
                "lock_key": self._session_fence_lock_key(
                    context.project_id,
                    session_id,
                )
            },
        )
        statement = (
            select(AgentDesignOperationRow)
            .where(
                AgentDesignOperationRow.project_id == context.project_id,
                AgentDesignOperationRow.owner_user_id == str(context.user_id),
                AgentDesignOperationRow.session_id == session_id,
                AgentDesignOperationRow.operation_kind == "turn",
                AgentDesignOperationRow.status == "in_progress",
                self._context_exists(context),
            )
            .order_by(
                AgentDesignOperationRow.created_at,
                AgentDesignOperationRow.id,
            )
            .with_for_update(of=AgentDesignOperationRow)
        )
        return tuple((await self.session.execute(statement)).scalars())

    @staticmethod
    def _session_fence_lock_key(
        project_id: uuid.UUID,
        session_id: uuid.UUID,
    ) -> int:
        digest = hashlib.sha256(f"agent-design-session\x00{project_id}\x00{session_id}".encode()).digest()
        # Match the repository-wide pg_advisory_xact_lock(bigint) convention.
        return int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF

    async def create_operation(
        self,
        context: ProjectContext,
        row: AgentDesignOperationRow,
    ) -> AgentDesignOperationRow:
        await self.lock_context(context)
        if row.project_id != context.project_id or row.owner_user_id != str(context.user_id):
            raise AssetForbidden(context.request_id)
        self.session.add(row)
        await self.session.flush()
        return row


__all__ = [
    "AgentDesignAllowedAssetRecord",
    "AgentDesignRepository",
]
