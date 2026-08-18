from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.agent_repository import AgentRepository
from app.shared_assets.errors import AssetConflict, AssetNotFound


class _Session:
    def __init__(self) -> None:
        self.flushed = 0

    async def flush(self) -> None:
        self.flushed += 1


def _context() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.EDITOR,
        capabilities=capabilities_for(ProjectRole.EDITOR),
        membership_version=1,
        request_id="agent-archive-contract",
    )


def _asset(context: ProjectContext, *, status: str = "active") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        scope="project",
        project_id=context.project_id,
        status=status,
        version=7,
        current_published_version_id=uuid.uuid4(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_status", ["active", "suspended"])
async def test_archive_changes_only_agent_lifecycle_state(
    initial_status: str,
) -> None:
    context = _context()
    asset = _asset(context, status=initial_status)
    published_version_id = asset.current_published_version_id
    session = _Session()

    await AgentRepository(session).archive_project_asset(  # type: ignore[arg-type]
        context,
        asset,
    )

    assert asset.status == "archived"
    assert asset.version == 8
    assert asset.current_published_version_id == published_version_id
    assert session.flushed == 1


@pytest.mark.asyncio
async def test_archive_rejects_an_already_archived_agent() -> None:
    context = _context()
    asset = _asset(context, status="archived")
    session = _Session()

    with pytest.raises(AssetConflict) as exc_info:
        await AgentRepository(session).archive_project_asset(  # type: ignore[arg-type]
            context,
            asset,
        )

    assert exc_info.value.request_id == context.request_id
    assert session.flushed == 0


@pytest.mark.asyncio
async def test_archive_rejects_a_cross_project_agent() -> None:
    context = _context()
    asset = _asset(context)
    asset.project_id = uuid.uuid4()
    session = _Session()

    with pytest.raises(AssetNotFound) as exc_info:
        await AgentRepository(session).archive_project_asset(  # type: ignore[arg-type]
            context,
            asset,
        )

    assert exc_info.value.request_id == context.request_id
    assert session.flushed == 0
