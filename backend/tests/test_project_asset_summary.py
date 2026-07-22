from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.projects.repository import ProjectRepository


class _Result:
    def __init__(self, row: object):
        self._row = row

    def all(self) -> list[object]:
        return [self._row]


class _Session:
    def __init__(self, row: object):
        self._row = row

    @asynccontextmanager
    async def begin(self):
        yield

    async def execute(self, _statement: object) -> _Result:
        return _Result(self._row)


@pytest.mark.asyncio
async def test_project_view_uses_the_persisted_shared_asset_summary() -> None:
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    context = ProjectContext(
        user_id,
        project_id,
        membership_id,
        ProjectRole.ADMIN,
        capabilities_for(ProjectRole.ADMIN),
        1,
        "req-project-summary",
    )
    now = datetime.now(UTC)
    row = SimpleNamespace(
        ProjectRow=SimpleNamespace(
            id=project_id,
            slug="alpha",
            display_name="Alpha",
            description="",
            icon="folder",
            status="active",
            is_suspended=False,
            deletion_effective_at=None,
            created_at=now,
        ),
        ProjectMembershipRow=SimpleNamespace(
            id=membership_id,
            role="admin",
            is_pinned=False,
            last_entered_at=None,
            version=1,
        ),
        member_count=3,
        agent_count=4,
        skill_count=5,
        mcp_count=6,
        quota_members_used=3,
        quota_members_reserved=0,
        quota_members_limit=20,
        quota_storage_bytes_used=1_024,
        quota_storage_bytes_reserved=512,
        quota_storage_bytes_limit=5_368_709_120,
        quota_concurrent_runs_used=1,
        quota_concurrent_runs_reserved=1,
        quota_concurrent_runs_limit=3,
        quota_mcp_calls_daily_used=25,
        quota_mcp_calls_daily_reserved=5,
        quota_mcp_calls_daily_limit=10_000,
    )

    view = await ProjectRepository(_Session(row)).get(context)  # type: ignore[arg-type]

    assert view.member_count == 3
    assert view.agent_count == 4
    assert view.skill_count == 5
    assert view.mcp_count == 6
    assert view.quota_summary.members.used == 3
    assert view.quota_summary.storage_bytes.reserved == 512
    assert view.quota_summary.concurrent_runs.limit == 3
    assert view.quota_summary.mcp_calls_daily.used == 25
