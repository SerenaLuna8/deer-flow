"""Replay-only route that prepares the Agent used by the browser core test."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from app.gateway.deps import private_work_context
from app.private_work.context import (
    PrivateWorkContext,
    require_issued_private_work_context,
)
from app.projects.context import resolve_project_context_in_transaction
from app.shared_assets.agent_catalog import (
    AgentCatalogValidator,
    StaticToolGroupCatalog,
)
from app.shared_assets.agent_service import AgentService, CreateAgent
from app.shared_assets.models import AgentPayload
from deerflow.persistence.engine import get_session_factory

router = APIRouter(
    prefix="/api/projects/{project_id}/test-only",
    tags=["test-only"],
)


@router.post("/prepare-agent", status_code=status.HTTP_201_CREATED)
async def prepare_replay_agent(
    project_id: uuid.UUID,
    context: PrivateWorkContext = Depends(private_work_context),
) -> dict[str, object]:
    """Create one suspended project Agent through the production service seam."""

    context = require_issued_private_work_context(context)
    if context.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    session_factory = get_session_factory()
    async with session_factory() as session, session.begin():
        project_context = await resolve_project_context_in_transaction(
            session,
            context.user_id,
            context.project_id,
            context.request_id,
            lock=True,
        )
        created = await AgentService(
            session_factory,
            catalog_validator=AgentCatalogValidator(
                StaticToolGroupCatalog(("file:read", "file:write")),
            ),
        ).create_project_from_design_in_session(
            session,
            project_context,
            CreateAgent("replay-agent", "Replay Agent"),
            AgentPayload(
                description="Deterministic gateway replay",
                soul="Use the exact project tools to complete the request.",
                model_ref="scenario-model",
                tool_groups=("file:read", "file:write"),
                skill_version_ids=(),
                mcp_version_ids=(),
            ),
            publish=True,
        )

    return {
        "id": str(created.asset.id),
        "scope": "project",
        "version": created.asset.version,
    }
