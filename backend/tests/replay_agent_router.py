"""Replay-only route that prepares the Agent used by the browser core test."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

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
from app.shared_assets.agent_service import (
    AgentInstructions,
    AgentService,
    CreateAgent,
)
from app.shared_assets.models import AgentPayload
from deerflow.persistence.engine import get_session_factory
from deerflow.persistence.private_work import RunAssetVersionRow

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
        service = AgentService(
            session_factory,
            catalog_validator=AgentCatalogValidator(
                StaticToolGroupCatalog(("file:read", "file:write")),
            ),
        )
        created = await service.create_project_from_design_in_session(
            session,
            project_context,
            CreateAgent("replay-agent", "Replay Agent"),
            AgentPayload(
                description="Deterministic gateway replay",
                soul="Use the exact project tools to complete the request.",
                model_ref="default",
                tool_groups=("file:read", "file:write"),
                skill_refs=(),
                mcp_version_ids=(),
            ),
        )
    await service.activate_version(
        project_context,
        created.asset.id,
        created.version.id,
        expected_asset_version=created.asset.revision,
    )

    return {
        "id": str(created.asset.id),
        "scope": "project",
    }


@router.post("/agents/{agent_id}/activate-next-version")
async def activate_next_replay_agent_version(
    project_id: uuid.UUID,
    agent_id: uuid.UUID,
    context: PrivateWorkContext = Depends(private_work_context),
) -> dict[str, object]:
    """Create and activate the next immutable Agent version for browser tests."""

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

    service = AgentService(
        session_factory,
        catalog_validator=AgentCatalogValidator(
            StaticToolGroupCatalog(("file:read", "file:write")),
        ),
    )
    asset = await service.get(project_context, agent_id)
    history = await service.get_version_history(project_context, agent_id)
    current = next(
        (version for version in history if version.id == asset.current_version_id),
        None,
    )
    if current is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT)

    candidate = await service.update_instructions(
        project_context,
        agent_id,
        AgentInstructions(
            agents_instructions=current.agents_instructions,
            soul=current.soul,
            identity=current.identity,
            user_context=current.user_context,
        ),
        expected_asset_version=asset.revision,
    )
    saved_asset = await service.get(project_context, agent_id)
    await service.activate_version(
        project_context,
        agent_id,
        candidate.id,
        expected_asset_version=saved_asset.revision,
    )
    return {
        "version_id": str(candidate.id),
        "version_number": candidate.version_number,
    }


@router.get("/runs/{run_id}/lead-agent-version")
async def get_replay_run_lead_agent_version(
    project_id: uuid.UUID,
    run_id: str,
    context: PrivateWorkContext = Depends(private_work_context),
) -> dict[str, str]:
    """Expose the admitted lead-Agent snapshot to browser acceptance tests."""

    context = require_issued_private_work_context(context)
    if context.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    session_factory = get_session_factory()
    async with session_factory() as session:
        version_id = (
            await session.execute(
                select(RunAssetVersionRow.version_id).where(
                    RunAssetVersionRow.project_id == context.project_id,
                    RunAssetVersionRow.owner_user_id == str(context.user_id),
                    RunAssetVersionRow.run_id == run_id,
                    RunAssetVersionRow.asset_kind == "agent",
                    RunAssetVersionRow.dependency_order == 0,
                )
            )
        ).scalar_one_or_none()
    if version_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return {"version_id": str(version_id)}
