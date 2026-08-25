"""Replay-only route that prepares the Agent used by the browser core test."""

from __future__ import annotations

import asyncio
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
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
from deerflow.persistence.jobs.model import JobRow
from deerflow.persistence.private_work import RunAssetVersionRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow

router = APIRouter(
    prefix="/api/projects/{project_id}/test-only",
    tags=["test-only"],
)


class ReplayWorkerControllerState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["immediate", "delayed"]
    running: bool
    fresh: bool
    held_model: bool
    held_claim: bool
    held_begin_execution: bool


class ReplayRetrySafetyState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    retry_safety: Literal["unknown"]


def build_replay_worker_router(controller: object) -> APIRouter:
    """Build the controller API mounted only by the replay Gateway script."""

    for method in ("status", "start", "stop", "crash", "hold", "release"):
        if not callable(getattr(controller, method, None)):
            raise TypeError("replay Worker controller is unavailable")

    worker_router = APIRouter(
        prefix="/api/test-only/replay-worker",
        tags=["test-only"],
    )

    async def invoke(
        method: Literal["status", "start", "stop", "crash", "hold", "release"],
        *args: object,
    ) -> ReplayWorkerControllerState:
        try:
            payload = await asyncio.to_thread(
                getattr(controller, method),
                *args,
            )
            return ReplayWorkerControllerState.model_validate(payload)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Replay Worker controller unavailable",
            ) from None

    @worker_router.get("", response_model=ReplayWorkerControllerState)
    async def replay_worker_status() -> ReplayWorkerControllerState:
        return await invoke("status")

    @worker_router.post("/start", response_model=ReplayWorkerControllerState)
    async def start_replay_worker() -> ReplayWorkerControllerState:
        return await invoke("start")

    @worker_router.post("/stop", response_model=ReplayWorkerControllerState)
    async def stop_replay_worker() -> ReplayWorkerControllerState:
        return await invoke("stop")

    @worker_router.post("/crash", response_model=ReplayWorkerControllerState)
    async def crash_replay_worker() -> ReplayWorkerControllerState:
        return await invoke("crash")

    @worker_router.post(
        "/faults/{fault}/{action}",
        response_model=ReplayWorkerControllerState,
    )
    async def control_replay_worker_fault(
        fault: Literal["model", "claim", "begin_execution"],
        action: Literal["hold", "release"],
    ) -> ReplayWorkerControllerState:
        return await invoke(action, fault)

    return worker_router


@router.post(
    "/threads/{thread_id}/runs/{run_id}/retry-safety/unknown",
    response_model=ReplayRetrySafetyState,
)
async def mark_replay_run_retry_safety_unknown(
    project_id: uuid.UUID,
    thread_id: str,
    run_id: str,
    context: PrivateWorkContext = Depends(private_work_context),
) -> ReplayRetrySafetyState:
    """Move one exact current private Run Job from safe to unknown."""

    context = require_issued_private_work_context(context)
    if context.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    session_factory = get_session_factory()
    owner_user_id = str(context.user_id)
    async with session_factory() as session, session.begin():
        await resolve_project_context_in_transaction(
            session,
            context.user_id,
            context.project_id,
            context.request_id,
            lock=True,
        )
        thread = await session.scalar(
            select(ThreadMetaRow.thread_id)
            .where(
                ThreadMetaRow.project_id == context.project_id,
                ThreadMetaRow.owner_user_id == owner_user_id,
                ThreadMetaRow.thread_id == thread_id,
                ThreadMetaRow.thread_kind == "chat",
                ThreadMetaRow.deleted_at.is_(None),
            )
            .with_for_update(of=ThreadMetaRow)
        )
        if thread is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

        run = await session.scalar(
            select(RunRow)
            .where(
                RunRow.project_id == context.project_id,
                RunRow.owner_user_id == owner_user_id,
                RunRow.thread_id == thread_id,
                RunRow.run_id == run_id,
            )
            .with_for_update(of=RunRow)
        )
        if run is None or run.job_id is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

        job = await session.scalar(
            select(JobRow)
            .where(
                JobRow.id == run.job_id,
                JobRow.project_id == context.project_id,
                JobRow.owner_user_id == owner_user_id,
                JobRow.run_id == run_id,
                JobRow.job_type == "private_run",
            )
            .with_for_update(of=JobRow)
        )
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        if job.retry_safety != "safe":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT)
        job.retry_safety = "unknown"

    return ReplayRetrySafetyState(
        run_id=run_id,
        retry_safety="unknown",
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
                payload_schema_version=4,
            ),
        )
    await service.enable(
        project_context,
        created.asset.id,
        expected_asset_version=created.asset.revision,
    )

    return {
        "id": str(created.asset.id),
        "scope": "project",
    }


@router.post("/agents/{agent_id}/save-next-definition")
async def save_next_replay_agent_definition(
    project_id: uuid.UUID,
    agent_id: uuid.UUID,
    context: PrivateWorkContext = Depends(private_work_context),
) -> dict[str, object]:
    """Save the next mutable Agent Definition for browser tests."""

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
    current = await service.get(project_context, agent_id)
    saved = await service.update_instructions(
        project_context,
        agent_id,
        AgentInstructions(
            agents_instructions=current.definition.agents_instructions,
            soul=current.definition.soul,
            identity=current.definition.identity,
            user_context=current.definition.user_context,
        ),
        expected_asset_version=current.asset.revision,
    )
    return {
        "definition_id": str(saved.definition.definition_id),
        "revision": saved.asset.revision,
    }


@router.get("/runs/{run_id}/lead-agent-definition")
async def get_replay_run_lead_agent_definition(
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
        definition_id = (
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
    if definition_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return {"definition_id": str(definition_id)}
