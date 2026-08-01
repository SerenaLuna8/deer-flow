"""Test-only run/message seeder for the multi-run render-order e2e (issue #3352).

Mounted **only** by ``scripts/run_replay_gateway.py`` (the replay e2e gateway)
and never by the production app, so it cannot ship. It lets a Playwright spec
stand up a thread with >=2 runs whose per-run messages exercise the frontend's
reload / history-rebuild ordering path — with no real model, no recording, and
no API key.

Why a seeder instead of recording a conversation: issue #3352 only reproduces
when the checkpoint no longer holds the older messages (post-compression), so
the frontend rebuilds them from the per-run history endpoints. A seeder lets us
create exactly that precondition deterministically — runs in the run store +
per-run ``category="message"`` events, and **no checkpoint** — so on reload the
buggy ``findLatestUnloadedRunIndex`` + prepend in ``core/threads/hooks.ts`` is
the sole source of truth and its reversed order becomes observable.

It derives one server-issued ``PrivateWorkContext`` from the project path, creates
the durable Thread parent without a checkpoint, and writes through the gateway's
own scoped Run and ``private_run_event_store`` ports. The event shape mirrors
exactly what ``runtime/journal.py`` writes for real runs
(``event_type`` ``llm.human.input`` / ``llm.ai.response``, ``category``
``"message"``, ``content`` = ``message.model_dump()``, ``metadata.caller`` =
``"lead_agent"``).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.gateway.deps import private_work_context
from app.private_work.context import (
    PrivateWorkContext,
    require_issued_private_work_context,
)
from app.private_work.executable_agent import require_executable_agent
from app.private_work.revalidation import PrivateWorkRevalidator
from app.private_work.thread_repository import (
    PrivateThreadRepository,
    ThreadAgentRef,
)
from app.projects.capabilities import Capability
from app.shared_assets.models import AgentPayload
from deerflow.persistence.engine import get_session_factory
from deerflow.runtime.private_scope import PrivateResourceScope

router = APIRouter(
    prefix="/api/projects/{project_id}/test-only",
    tags=["test-only"],
)

# Mirror runtime/journal.py: human prompts are recorded as ``llm.human.input``
# and assistant turns as ``llm.ai.response``; both land in ``category="message"``.
_EVENT_TYPE = {"human": "llm.human.input", "ai": "llm.ai.response"}


class SeedMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["human", "ai"]
    content: str = Field(max_length=65_536)
    id: str = Field(min_length=1, max_length=128)


class SeedRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID
    # ISO timestamp; RunManager.list_by_thread sorts newest-first by created_at,
    # so a later created_at must mean a later run for the ordering to be faithful.
    created_at: AwareDatetime
    messages: list[SeedMessage] = Field(min_length=1, max_length=100)


class SeedRunsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: uuid.UUID
    agent_asset_id: uuid.UUID
    agent_scope: Literal["system", "project"]
    runs: list[SeedRun] = Field(min_length=1, max_length=10)


class RunSeedStore(Protocol):
    async def put(self, run_id: str, **kwargs: Any) -> None: ...


class EventSeedStore(Protocol):
    async def put_batch(
        self,
        events: list[dict[str, Any]],
        *,
        scope: PrivateResourceScope,
    ) -> list[dict[str, Any]]: ...


CreateSeedThread = Callable[
    [PrivateWorkContext, SeedRunsBody],
    Awaitable[None],
]


async def prepare_replay_agent(
    context: PrivateWorkContext,
    *,
    session_factory: object | None = None,
    create_agent: Callable[..., Awaitable[Any]] | None = None,
) -> dict[str, object]:
    """Create the exact project Agent profile used by the replay golden."""

    context = require_issued_private_work_context(context)
    if create_agent is None:
        from support.project_agent_factory import create_project_agent_from_design

        create_agent = create_project_agent_from_design
    created = await create_agent(
        session_factory or get_session_factory(),
        user_id=context.user_id,
        project_id=context.project_id,
        slug="replay-agent",
        display_name="Replay Agent",
        payload=AgentPayload(
            description="Deterministic gateway replay",
            soul="Use the exact project tools to complete the request.",
            model_ref="scenario-model",
            tool_groups=("file:read", "file:write"),
            skill_version_ids=(),
            mcp_version_ids=(),
        ),
        request_id="replay-agent-setup",
    )
    return {
        "id": str(created.asset.id),
        "scope": "project",
        "version": created.asset.version,
    }


async def _create_seed_thread(
    context: PrivateWorkContext,
    body: SeedRunsBody,
) -> None:
    """Create only the durable Thread parent, intentionally without a checkpoint."""

    context = require_issued_private_work_context(context)
    session_factory = get_session_factory()
    async with session_factory() as session, session.begin():
        await PrivateWorkRevalidator().require(
            session,
            context,
            Capability.PRIVATE_WORK_CREATE,
            lock=True,
        )
        agent = ThreadAgentRef(
            asset_id=body.agent_asset_id,
            scope=body.agent_scope,
        )
        await require_executable_agent(session, context, agent)
        await PrivateThreadRepository(session).create(
            scope=context.resource_scope,
            thread_id=str(body.thread_id),
            agent=agent,
            display_name="Replay history",
            metadata={},
        )


async def seed_project_runs(
    body: SeedRunsBody,
    *,
    context: PrivateWorkContext,
    create_thread: CreateSeedThread,
    run_store: RunSeedStore,
    event_store: EventSeedStore,
) -> dict[str, object]:
    """Seed one exact project-owner Thread and its chronological Run history."""

    from langchain_core.messages import AIMessage, HumanMessage

    context = require_issued_private_work_context(context)
    scope = context.resource_scope
    thread_id = str(body.thread_id)
    await create_thread(context, body)

    for run in body.runs:
        run_id = str(run.run_id)
        created_at = run.created_at.isoformat()
        await run_store.put(
            run_id,
            thread_id=thread_id,
            assistant_id="lead_agent",
            status="success",
            created_at=created_at,
            scope=scope,
        )
        events = []
        for message in run.messages:
            persisted = (HumanMessage if message.role == "human" else AIMessage)(
                content=message.content,
                id=message.id,
            )
            events.append(
                {
                    "thread_id": thread_id,
                    "run_id": run_id,
                    "event_type": _EVENT_TYPE[message.role],
                    "category": "message",
                    "content": persisted.model_dump(),
                    "metadata": {"caller": "lead_agent"},
                    "created_at": created_at,
                }
            )
        await event_store.put_batch(events, scope=scope)

    return {
        "ok": True,
        "thread_id": thread_id,
        "runs": len(body.runs),
    }


@router.post("/prepare-agent", status_code=status.HTTP_201_CREATED)
async def prepare_replay_agent_route(
    project_id: uuid.UUID,
    context: PrivateWorkContext = Depends(private_work_context),
) -> dict[str, object]:
    if context.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return await prepare_replay_agent(context)


@router.post("/seed-runs")
async def seed_runs(
    body: SeedRunsBody,
    request: Request,
    project_id: uuid.UUID,
    context: PrivateWorkContext = Depends(private_work_context),
) -> dict[str, object]:
    """Seed runs + per-run message events for the authenticated project owner.

    No checkpoint is written: that is the whole point — it forces the frontend's
    reload path to rebuild history from the per-run endpoints (the #3352 bug
    site) instead of the (correctly ordered) checkpoint snapshot.
    """
    if context.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    run_store = getattr(request.app.state, "run_store", None)
    event_store = getattr(request.app.state, "private_run_event_store", None)
    if not callable(getattr(run_store, "put", None)) or not callable(getattr(event_store, "put_batch", None)):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Replay persistence is unavailable",
        )
    return await seed_project_runs(
        body,
        context=context,
        create_thread=_create_seed_thread,
        run_store=run_store,
        event_store=event_store,
    )
