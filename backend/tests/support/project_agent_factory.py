from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.projects.context import resolve_project_context_in_transaction
from app.shared_assets.agent_service import (
    AgentService,
    CreateAgent,
    ProjectAgentCreateResult,
)
from app.shared_assets.models import AgentPayload


async def create_project_agent_from_design(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
    slug: str,
    display_name: str,
    payload: AgentPayload,
    request_id: str,
) -> ProjectAgentCreateResult:
    """Create a complete test Agent through the Builder's atomic commit seam."""

    async with session_factory() as session, session.begin():
        context = await resolve_project_context_in_transaction(
            session,
            user_id,
            project_id,
            request_id,
            lock=True,
        )
        return await AgentService(
            session_factory,
        ).create_project_from_design_in_session(
            session,
            context,
            CreateAgent(slug, display_name),
            payload,
        )
