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
        asset = (
            await session.execute(
                select(AgentRow)
                .where(
                    AgentRow.id == agent.asset_id,
                    AgentRow.scope == "project",
                    AgentRow.project_id == context.project_id,
                    AgentRow.status == "active",
                )
                .with_for_update(read=True, of=AgentRow)
            )
        ).scalar_one_or_none()
        if asset is None or asset.current_published_version_id is None:
            raise PrivateWorkNotFound(context.request_id)
        version_id = asset.current_published_version_id
    elif agent.scope == "system":
        binding = (
            await session.execute(
                select(ProjectSystemAgentBindingRow)
                .where(
                    ProjectSystemAgentBindingRow.system_agent_id == agent.asset_id,
                    ProjectSystemAgentBindingRow.project_id == context.project_id,
                    ProjectSystemAgentBindingRow.enabled.is_(True),
                )
                .with_for_update(read=True, of=ProjectSystemAgentBindingRow)
            )
        ).scalar_one_or_none()
        if binding is None:
            raise PrivateWorkNotFound(context.request_id)
        asset = (
            await session.execute(
                select(AgentRow)
                .where(
                    AgentRow.id == agent.asset_id,
                    AgentRow.scope == "system",
                    AgentRow.project_id.is_(None),
                    AgentRow.status == "active",
                )
                .with_for_update(read=True, of=AgentRow)
            )
        ).scalar_one_or_none()
        if asset is None:
            raise PrivateWorkNotFound(context.request_id)
        version_id = binding.agent_version_id
    else:
        raise PrivateWorkNotFound(context.request_id)

    version = (
        await session.execute(
            select(AgentVersionRow.id)
            .where(
                AgentVersionRow.id == version_id,
                AgentVersionRow.agent_id == agent.asset_id,
                AgentVersionRow.workflow_status == "published",
            )
            .with_for_update(read=True, of=AgentVersionRow)
        )
    ).scalar_one_or_none()
    if version is None:
        raise PrivateWorkNotFound(context.request_id)


__all__ = ["require_executable_agent"]
