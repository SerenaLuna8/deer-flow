from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import and_, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from deerflow.persistence.projects.model import ProjectRow
from deerflow.persistence.shared_assets.agent_model import AgentRow, AgentVersionRow
from deerflow.persistence.shared_assets.binding_model import (
    ProjectSystemAgentBindingRow,
    ProjectSystemMcpBindingRow,
    ProjectSystemSkillBindingRow,
)
from deerflow.persistence.shared_assets.mcp_model import McpServerRow, McpServerVersionRow
from deerflow.persistence.shared_assets.skill_model import SkillRow, SkillVersionRow


@dataclass(frozen=True)
class ProjectAssetSummary:
    agent_count: int
    skill_count: int
    mcp_count: int


def project_asset_summary_columns(
    project_id: ColumnElement[uuid.UUID],
) -> tuple[ColumnElement[int], ColumnElement[int], ColumnElement[int]]:
    project_agents = (
        select(func.count())
        .select_from(AgentRow)
        .join(
            AgentVersionRow,
            and_(
                AgentVersionRow.agent_id == AgentRow.id,
                AgentVersionRow.id == AgentRow.current_published_version_id,
                AgentVersionRow.workflow_status == "published",
            ),
        )
        .where(
            AgentRow.scope == "project",
            AgentRow.project_id == project_id,
            AgentRow.status == "active",
        )
        .correlate(ProjectRow)
        .scalar_subquery()
    )
    bound_system_agents = (
        select(func.count())
        .select_from(ProjectSystemAgentBindingRow)
        .join(
            AgentRow,
            and_(
                AgentRow.id == ProjectSystemAgentBindingRow.system_agent_id,
                AgentRow.scope == "system",
                AgentRow.project_id.is_(None),
            ),
        )
        .join(
            AgentVersionRow,
            and_(
                AgentVersionRow.agent_id == AgentRow.id,
                AgentVersionRow.id == ProjectSystemAgentBindingRow.agent_version_id,
                AgentVersionRow.workflow_status == "published",
            ),
        )
        .where(
            ProjectSystemAgentBindingRow.project_id == project_id,
            ProjectSystemAgentBindingRow.enabled.is_(True),
            AgentRow.status == "active",
        )
        .correlate(ProjectRow)
        .scalar_subquery()
    )
    project_skills = (
        select(func.count())
        .select_from(SkillRow)
        .join(
            SkillVersionRow,
            and_(
                SkillVersionRow.skill_id == SkillRow.id,
                SkillVersionRow.id == SkillRow.current_published_version_id,
                SkillVersionRow.workflow_status == "published",
            ),
        )
        .where(
            SkillRow.scope == "project",
            SkillRow.project_id == project_id,
            SkillRow.status == "active",
        )
        .correlate(ProjectRow)
        .scalar_subquery()
    )
    bound_system_skills = (
        select(func.count())
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
                SkillVersionRow.workflow_status == "published",
            ),
        )
        .where(
            ProjectSystemSkillBindingRow.project_id == project_id,
            ProjectSystemSkillBindingRow.enabled.is_(True),
            SkillRow.status == "active",
        )
        .correlate(ProjectRow)
        .scalar_subquery()
    )
    project_mcp_servers = (
        select(func.count())
        .select_from(McpServerRow)
        .join(
            McpServerVersionRow,
            and_(
                McpServerVersionRow.mcp_server_id == McpServerRow.id,
                McpServerVersionRow.id == McpServerRow.current_published_version_id,
                McpServerVersionRow.workflow_status == "published",
            ),
        )
        .where(
            McpServerRow.scope == "project",
            McpServerRow.project_id == project_id,
            McpServerRow.status == "active",
        )
        .correlate(ProjectRow)
        .scalar_subquery()
    )
    bound_system_mcp_servers = (
        select(func.count())
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
                McpServerVersionRow.workflow_status == "published",
            ),
        )
        .where(
            ProjectSystemMcpBindingRow.project_id == project_id,
            ProjectSystemMcpBindingRow.enabled.is_(True),
            McpServerRow.status == "active",
        )
        .correlate(ProjectRow)
        .scalar_subquery()
    )
    return (
        (project_agents + bound_system_agents).label("agent_count"),
        (project_skills + bound_system_skills).label("skill_count"),
        (project_mcp_servers + bound_system_mcp_servers).label("mcp_count"),
    )


async def load_project_asset_summary(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> ProjectAssetSummary:
    columns = project_asset_summary_columns(literal(project_id))
    row = (await session.execute(select(*columns))).one()
    return ProjectAssetSummary(
        agent_count=int(row.agent_count),
        skill_count=int(row.skill_count),
        mcp_count=int(row.mcp_count),
    )
