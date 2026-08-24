"""Project Skill creation is limited to archive import and AI Builder."""

from __future__ import annotations

import io
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

import app.shared_assets as shared_assets
from app.gateway.routers import admin_assets, project_assets, project_skill_builder
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.shared_assets.models import AssetScope, VersionRelation
from app.shared_assets.skill_archive import load_skill_archive_package
from app.shared_assets.skill_repository import SkillRepository
from app.shared_assets.skill_service import (
    ProjectSkillArchiveCreateResult,
    SkillAssetView,
    SkillService,
    SkillVersionView,
)

_PROJECT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
_USER_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
_MEMBERSHIP_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
_SKILL_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
_VERSION_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")
_REQUEST_ID = "a" * 32


def _project_context() -> ProjectContext:
    return ProjectContext(
        user_id=_USER_ID,
        project_id=_PROJECT_ID,
        membership_id=_MEMBERSHIP_ID,
        role="admin",
        capabilities=frozenset(
            {
                Capability.SHARED_ASSETS_READ,
                Capability.SHARED_ASSETS_EDIT,
            }
        ),
        membership_version=1,
        request_id=_REQUEST_ID,
    )


def _archive_create_result() -> ProjectSkillArchiveCreateResult:
    now = datetime(2026, 8, 18, tzinfo=UTC)
    return ProjectSkillArchiveCreateResult(
        asset=SkillAssetView(
            id=_SKILL_ID,
            scope=AssetScope.PROJECT,
            project_id=_PROJECT_ID,
            slug="route-import-skill",
            display_name="route-import-skill",
            status="suspended",
            current_version_id=None,
            revision=2,
            created_by_user_id=str(_USER_ID),
            created_at=now,
            updated_at=now,
            description="Route import test",
        ),
        version=SkillVersionView(
            id=_VERSION_ID,
            skill_id=_SKILL_ID,
            version_number=1,
            relation=VersionRelation.CANDIDATE,
            description="Route import test",
            frontmatter={
                "name": "route-import-skill",
                "description": "Route import test",
            },
            compatibility=None,
            secret_requirements=(),
            file_views=(),
            supersedes_version_id=None,
            payload_checksum="b" * 64,
            revoked_at=None,
            revoked_by_user_id=None,
            revocation_reason_code=None,
            governance_status="active",
            binding_eligible=True,
            created_by_user_id=str(_USER_ID),
            created_at=now,
        ),
    )


def _security_blocked_archive() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "SKILL.md",
            "---\nname: blocked-upload\ndescription: Blocked upload.\n---\n\n# Blocked upload\n",
        )
        archive.writestr(
            "scripts/run.py",
            'import subprocess\nsubprocess.Popen(["echo", "unsafe"], shell=True)\n',
        )
    return buffer.getvalue()


@dataclass
class _RecordingSkillService:
    calls: list[tuple[ProjectContext, bytes, str]]

    async def create_project_from_archive_upload(
        self,
        actor: ProjectContext,
        payload: bytes,
        *,
        filename: str,
    ) -> ProjectSkillArchiveCreateResult:
        self.calls.append((actor, payload, filename))
        return _archive_create_result()


def _has_post_route(path: str) -> bool:
    routers = (
        project_assets.project_router,
        admin_assets.admin_project_router,
        project_skill_builder.router,
    )
    return any(isinstance(route, APIRoute) and route.path == path and "POST" in route.methods for router in routers for route in router.routes)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        f"/api/projects/{_PROJECT_ID}/skills",
        f"/api/admin/projects/{_PROJECT_ID}/assets/skills",
    ],
)
async def test_manual_project_skill_create_route_is_not_available(
    path: str,
) -> None:
    application = FastAPI()
    application.include_router(project_assets.project_router)
    application.include_router(admin_assets.admin_project_router)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.post(
            path,
            json={"slug": "manual-skill", "display_name": "Manual Skill"},
        )

    assert response.status_code == 405


@pytest.mark.asyncio
async def test_project_skill_archive_import_forwards_multipart_upload_and_returns_created_resource() -> None:
    service = _RecordingSkillService(calls=[])
    context = _project_context()
    application = FastAPI()
    application.include_router(project_assets.project_router)
    application.dependency_overrides[project_assets.project_asset_context] = lambda: context
    application.dependency_overrides[project_assets.get_skill_service] = lambda: service

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/projects/{_PROJECT_ID}/skills/import",
            files={
                "archive": (
                    "route-import.skill",
                    b"archive-route-payload",
                    "application/octet-stream",
                )
            },
        )

    assert response.status_code == 201
    assert service.calls == [(context, b"archive-route-payload", "route-import.skill")]
    assert response.json() == {
        "item": {
            "id": str(_SKILL_ID),
            "scope": "project",
            "project_id": str(_PROJECT_ID),
            "slug": "route-import-skill",
            "display_name": "route-import-skill",
            "status": "suspended",
            "current_version_id": None,
            "revision": 2,
            "created_by_user_id": str(_USER_ID),
            "created_at": "2026-08-18T00:00:00Z",
            "updated_at": "2026-08-18T00:00:00Z",
        },
        "version": {
            "id": str(_VERSION_ID),
            "skill_id": str(_SKILL_ID),
            "version_number": 1,
            "relation": "candidate",
            "description": "Route import test",
            "frontmatter": {
                "name": "route-import-skill",
                "description": "Route import test",
            },
            "compatibility": None,
            "secret_requirements": [],
            "file_views": [],
            "supersedes_version_id": None,
            "payload_checksum": "b" * 64,
            "revoked_at": None,
            "revoked_by_user_id": None,
            "revocation_reason_code": None,
            "governance_status": "active",
            "binding_eligible": True,
            "created_by_user_id": str(_USER_ID),
            "created_at": "2026-08-18T00:00:00Z",
        },
        "request_id": _REQUEST_ID,
    }


@pytest.mark.asyncio
async def test_skill_preview_applies_structural_validation_without_security_scan() -> None:
    files = load_skill_archive_package(
        _security_blocked_archive(),
        filename="blocked-preview.zip",
        request_id=_REQUEST_ID,
    )

    preview = await SkillService(lambda: None).preview_archive(
        _project_context(),
        files,
    )

    assert preview.frontmatter["name"] == "blocked-upload"


def test_archive_import_and_ai_builder_create_routes_remain_available() -> None:
    assert _has_post_route(
        "/api/projects/{project_id}/skills/import",
    )
    assert _has_post_route(
        "/api/projects/{project_id}/skill-builder/sessions",
    )


def test_skill_service_exposes_only_supported_project_create_entrypoints() -> None:
    assert not hasattr(SkillService, "create_asset")
    assert not hasattr(SkillService, "create_project_with_template")
    assert hasattr(SkillService, "create_project_from_archive_upload")
    assert hasattr(SkillService, "create_project_from_preview_in_session")


def test_skill_create_identity_command_and_dead_repository_paths_are_not_public() -> None:
    assert not hasattr(shared_assets, "CreateSkill")
    assert hasattr(SkillRepository, "create_project_asset")
    assert not hasattr(SkillRepository, "create_system_asset")
    assert not hasattr(SkillRepository, "create_override_asset")
