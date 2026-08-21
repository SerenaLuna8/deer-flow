from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

from app.gateway.routers.project_assets import project_asset_context
from app.gateway.routers.project_skill_builder import (
    get_skill_design_service,
    router,
)
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.errors import AssetNotFound
from app.shared_assets.skill_design_repository import SkillDesignRepository
from app.shared_assets.skill_design_service import (
    SkillDesignService,
    SkillDesignSessionView,
    SkillDesignStatus,
)
from deerflow.persistence.shared_assets import SkillDesignSessionRow

_PROJECT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
_USER_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
_MEMBERSHIP_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
_SESSION_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
_THREAD_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")
_SKILL_ID = uuid.UUID("66666666-6666-4666-8666-666666666666")
_VERSION_ID = uuid.UUID("77777777-7777-4777-8777-777777777777")
_NOW = datetime(2026, 8, 22, tzinfo=UTC)


def _context() -> ProjectContext:
    role = ProjectRole.VIEWER
    return ProjectContext(
        user_id=_USER_ID,
        project_id=_PROJECT_ID,
        membership_id=_MEMBERSHIP_ID,
        role=role,
        capabilities=capabilities_for(role),
        membership_version=1,
        request_id="skill-builder-design-record",
    )


def _completed_session() -> SkillDesignSessionView:
    return SkillDesignSessionView(
        id=_SESSION_ID,
        project_id=_PROJECT_ID,
        owner_user_id=str(_USER_ID),
        thread_id=_THREAD_ID,
        slug="catalog-auditor",
        display_name="Catalog auditor",
        status=SkillDesignStatus.COMPLETED,
        revision=8,
        messages=(),
        active_clarification=None,
        progress=(),
        files=(),
        draft_checksum="a" * 64,
        validation=None,
        error_code=None,
        error_message=None,
        created_skill_id=_SKILL_ID,
        created_skill_version_id=_VERSION_ID,
        created_at=_NOW,
        updated_at=_NOW,
        session_kind="revise",
        target_skill_id=_SKILL_ID,
        base_version_id=uuid.UUID("88888888-8888-4888-8888-888888888888"),
        base_version_number=3,
        base_payload_checksum="b" * 64,
    )


class _DesignRecordService:
    def __init__(self) -> None:
        self.calls: list[tuple[ProjectContext, uuid.UUID]] = []

    async def get_by_created_version(
        self,
        context: ProjectContext,
        version_id: uuid.UUID,
    ) -> SkillDesignSessionView:
        self.calls.append((context, version_id))
        return _completed_session()


@pytest.mark.asyncio
async def test_design_record_route_reads_the_exact_created_version_for_owner() -> None:
    app = FastAPI()
    service = _DesignRecordService()
    app.dependency_overrides[project_asset_context] = _context
    app.dependency_overrides[get_skill_design_service] = lambda: service
    app.include_router(router)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(f"/api/projects/{_PROJECT_ID}/skill-builder/sessions/by-version/{_VERSION_ID}")

    assert response.status_code == 200
    assert response.json()["data"]["id"] == str(_SESSION_ID)
    assert service.calls == [(_context(), _VERSION_ID)]


class _EmptyResult:
    @staticmethod
    def scalar_one_or_none() -> None:
        return None


class _RecordingSession:
    def __init__(self) -> None:
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _EmptyResult()


@pytest.mark.asyncio
async def test_design_record_repository_collapses_non_owner_or_missing_to_not_found() -> None:
    session = _RecordingSession()
    repository = SkillDesignRepository(session)  # type: ignore[arg-type]

    with pytest.raises(AssetNotFound):
        await repository.get_by_created_version(_context(), _VERSION_ID)

    assert session.statement is not None
    statement = str(session.statement)
    assert "skill_design_sessions.project_id" in statement
    assert "skill_design_sessions.owner_user_id" in statement
    assert "skill_design_sessions.created_skill_version_id" in statement
    assert "skill_design_sessions.status" in statement
    assert "skill_design_sessions.created_skill_deleted IS false" in statement


class _TransactionSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self):
        return self


class _DesignRecordRepository:
    def __init__(self, row: SkillDesignSessionRow) -> None:
        self.row = row
        self.calls: list[tuple[ProjectContext, uuid.UUID]] = []

    async def get_by_created_version(
        self,
        context: ProjectContext,
        version_id: uuid.UUID,
    ) -> SkillDesignSessionRow:
        self.calls.append((context, version_id))
        return self.row

    async def load_draft_files(self, *_args: object):
        return ()

    async def load_base_file_metadata(self, *_args: object):
        return ()


@pytest.mark.asyncio
async def test_design_record_service_allows_owner_with_read_without_edit() -> None:
    row = SkillDesignSessionRow(
        id=_SESSION_ID,
        project_id=_PROJECT_ID,
        owner_user_id=str(_USER_ID),
        thread_id=_THREAD_ID,
        slug="catalog-auditor",
        display_name="Catalog auditor",
        status="completed",
        revision=8,
        messages_json=[],
        progress_json=[],
        active_clarification_json=None,
        draft_checksum="a" * 64,
        validation_json=None,
        error_code=None,
        error_message=None,
        created_skill_id=_SKILL_ID,
        created_skill_version_id=_VERSION_ID,
        created_at=_NOW,
        updated_at=_NOW,
        authoring_dependencies_json=None,
        session_kind="revise",
        target_skill_id=_SKILL_ID,
        base_version_id=uuid.UUID("88888888-8888-4888-8888-888888888888"),
        base_version_number=3,
        base_payload_checksum="b" * 64,
        target_skill_deleted=False,
        execution_model_ref=None,
        execution_mode=None,
        execution_thinking_enabled=None,
        execution_reasoning_effort=None,
    )
    repository = _DesignRecordRepository(row)
    service = SkillDesignService(
        lambda: _TransactionSession(),  # type: ignore[arg-type]
        repository_factory=lambda _session: repository,  # type: ignore[arg-type]
    )

    result = await service.get_by_created_version(_context(), _VERSION_ID)

    assert result.id == _SESSION_ID
    assert result.created_skill_version_id == _VERSION_ID
    assert repository.calls == [(_context(), _VERSION_ID)]
