from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkNotFound
from app.private_work.thread_repository import ThreadAgentRef
from deerflow.persistence.shared_assets import (
    AgentRow,
    AgentVersionRow,
    ProjectSystemAgentBindingRow,
)


async def require_executable_agent(
    session: AsyncSession,
    context: PrivateWorkContext,
    agent: ThreadAgentRef,
) -> None:
    """Require the active published Agent target used by private execution."""

    if agent.scope == "project":
        statement = (
            select(AgentRow.id)
            .join(
                AgentVersionRow,
                AgentVersionRow.id == AgentRow.current_published_version_id,
            )
            .where(
                AgentRow.id == agent.asset_id,
                AgentRow.scope == "project",
                AgentRow.project_id == context.project_id,
                AgentRow.status == "active",
                AgentVersionRow.agent_id == AgentRow.id,
                AgentVersionRow.workflow_status == "published",
            )
        )
    elif agent.scope == "system":
        statement = (
            select(AgentRow.id)
            .join(
                ProjectSystemAgentBindingRow,
                ProjectSystemAgentBindingRow.system_agent_id == AgentRow.id,
            )
            .join(
                AgentVersionRow,
                AgentVersionRow.id == ProjectSystemAgentBindingRow.agent_version_id,
            )
            .where(
                AgentRow.id == agent.asset_id,
                AgentRow.scope == "system",
                AgentRow.status == "active",
                ProjectSystemAgentBindingRow.project_id == context.project_id,
                ProjectSystemAgentBindingRow.enabled.is_(True),
                AgentVersionRow.agent_id == AgentRow.id,
                AgentVersionRow.workflow_status == "published",
            )
        )
    else:
        raise PrivateWorkNotFound(context.request_id)
    if (await session.execute(statement)).scalar_one_or_none() is None:
        raise PrivateWorkNotFound(context.request_id)


__all__ = ["require_executable_agent"]
