from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.gateway.routers import project_skill_builder
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.errors import AssetConflict
from app.shared_assets.models import AssetScope
from app.shared_assets.skill_design_service import (
    SkillDesignCommitResult,
    SkillDesignFileView,
    SkillDesignMessage,
    SkillDesignProgressItem,
    SkillDesignProgressStatus,
    SkillDesignSessionSummary,
    SkillDesignSessionView,
    SkillDesignStatus,
)
from app.shared_assets.skill_service import SkillAssetView

PROJECT_ID = uuid.uuid4()
SESSION_ID = uuid.uuid4()
NOW = datetime.now(UTC)


def _context() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=PROJECT_ID,
        membership_id=uuid.uuid4(),
        role=ProjectRole.EDITOR,
        capabilities=capabilities_for(ProjectRole.EDITOR),
        membership_version=1,
        request_id="req-skill-builder",
    )


def _session(
    *,
    status: SkillDesignStatus = SkillDesignStatus.DRAFT_READY,
) -> SkillDesignSessionView:
    return SkillDesignSessionView(
        id=SESSION_ID,
        project_id=PROJECT_ID,
        owner_user_id=str(uuid.uuid4()),
        thread_id=uuid.uuid4(),
        slug="release-notes",
        display_name="Release Notes",
        status=status,
        revision=3,
        messages=(
            SkillDesignMessage(
                id="message-1",
                role="assistant",
                content="请描述这个 Skill。",
                created_at=NOW,
            ),
        ),
        active_clarification=None,
        progress=(
            SkillDesignProgressItem(
                id="draft",
                label="Skill 草稿",
                status=SkillDesignProgressStatus.COMPLETED,
            ),
        ),
        files=(
            SkillDesignFileView(
                path="SKILL.md",
                media_type="text/markdown",
                size_bytes=5,
                sha256="a" * 64,
                encoding="utf-8",
                content="hello",
            ),
        ),
        draft_checksum="b" * 64,
        validation=None,
        error_code=None,
        error_message=None,
        created_skill_id=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _client(service: AsyncMock) -> TestClient:
    app = FastAPI()
    app.include_router(project_skill_builder.router)
    app.dependency_overrides[project_skill_builder.project_asset_context] = _context
    app.dependency_overrides[project_skill_builder.get_skill_design_service] = lambda: service
    return TestClient(app)


def test_skill_builder_create_list_and_get_use_frozen_envelopes() -> None:
    service = AsyncMock()
    service.create.return_value = _session(status=SkillDesignStatus.INTERVIEWING)
    service.list_incomplete.return_value = (
        SkillDesignSessionSummary(
            id=SESSION_ID,
            slug="release-notes",
            display_name="Release Notes",
            status=SkillDesignStatus.INTERVIEWING,
            updated_at=NOW,
        ),
    )
    service.get.return_value = _session()
    client = _client(service)

    created = client.post(
        f"/api/projects/{PROJECT_ID}/skill-builder/sessions",
        json={
            "slug": "release-notes",
            "display_name": "Release Notes",
            "idempotency_key": "create-1",
        },
    )
    listed = client.get(f"/api/projects/{PROJECT_ID}/skill-builder/sessions")
    fetched = client.get(f"/api/projects/{PROJECT_ID}/skill-builder/sessions/{SESSION_ID}")

    assert created.status_code == 201
    assert set(created.json()) == {"data", "request_id"}
    assert created.json()["data"]["files"][0] == {
        "path": "SKILL.md",
        "media_type": "text/markdown",
        "size_bytes": 5,
        "sha256": "a" * 64,
        "encoding": "utf-8",
        "content": "hello",
    }
    assert listed.status_code == 200
    assert set(listed.json()["data"][0]) == {
        "id",
        "slug",
        "display_name",
        "status",
        "updated_at",
    }
    assert fetched.status_code == 200


def test_skill_builder_turn_union_maps_message_clarification_and_draft() -> None:
    service = AsyncMock()
    service.submit_turn.return_value = _session()
    client = _client(service)
    base = f"/api/projects/{PROJECT_ID}/skill-builder/sessions/{SESSION_ID}/turns"

    message = client.post(
        base,
        json={
            "input": {"kind": "message", "message": "读取合并的 PR。"},
            "expected_revision": 2,
            "idempotency_key": "message-1",
        },
    )
    clarification = client.post(
        base,
        json={
            "input": {
                "kind": "clarification",
                "response": {
                    "version": 1,
                    "kind": "human_input_response",
                    "source": "skill_builder",
                    "request_id": "question-1",
                    "response_kind": "option",
                    "option_id": "github",
                    "value": "GitHub",
                },
            },
            "expected_revision": 3,
            "idempotency_key": "clarification-1",
        },
    )
    draft = client.post(
        base,
        json={
            "input": {
                "kind": "draft_update",
                "expected_draft_checksum": "b" * 64,
                "changes": [
                    {
                        "op": "replace",
                        "path": "SKILL.md",
                        "media_type": "text/markdown",
                        "content": "# Updated",
                    }
                ],
            },
            "expected_revision": 4,
            "idempotency_key": "draft-1",
        },
    )

    assert message.status_code == clarification.status_code == draft.status_code == 200
    commands = [call.args[2] for call in service.submit_turn.await_args_list]
    assert commands[0].input.message == "读取合并的 PR。"
    assert commands[1].input.response.option_id == "github"
    assert commands[2].input.changes[0].path == "SKILL.md"
    assert commands[2].input.changes[0].content == "# Updated"


def test_skill_builder_validate_commit_and_cancel_match_frontend_contract() -> None:
    service = AsyncMock()
    skill_id = uuid.uuid4()
    version_id = uuid.uuid4()
    completed = dataclasses.replace(
        _session(status=SkillDesignStatus.COMPLETED),
        files=(),
        created_skill_id=skill_id,
    )
    skill = SkillAssetView(
        id=skill_id,
        scope=AssetScope.PROJECT,
        project_id=PROJECT_ID,
        slug="release-notes",
        display_name="Release Notes",
        status="suspended",
        current_published_version_id=version_id,
        version=2,
        created_by_user_id=str(uuid.uuid4()),
        created_at=NOW,
        updated_at=NOW,
    )
    service.validate.return_value = _session(status=SkillDesignStatus.VALIDATED)
    service.commit.return_value = SkillDesignCommitResult(
        session=completed,
        skill=skill,
    )
    service.cancel.return_value = _session(status=SkillDesignStatus.CANCELLED)
    client = _client(service)
    base = f"/api/projects/{PROJECT_ID}/skill-builder/sessions/{SESSION_ID}"

    validated = client.post(
        f"{base}/validate",
        json={
            "expected_revision": 3,
            "expected_draft_checksum": "b" * 64,
            "idempotency_key": "validate-1",
        },
    )
    committed = client.post(
        f"{base}/commit",
        json={
            "expected_revision": 4,
            "expected_draft_checksum": "b" * 64,
            "acknowledge_warnings": True,
            "idempotency_key": "commit-1",
        },
    )
    cancelled = client.post(
        f"{base}/cancel",
        json={"expected_revision": 3, "idempotency_key": "cancel-1"},
    )

    assert validated.status_code == 200
    assert committed.status_code == 200
    assert set(committed.json()["data"]) == {"session", "skill"}
    assert committed.json()["data"]["skill"]["status"] == "suspended"
    assert cancelled.status_code == 200


def test_skill_builder_rejects_invalid_union_and_maps_conflict() -> None:
    service = AsyncMock()
    service.get.side_effect = AssetConflict("req-skill-builder")
    client = _client(service)

    conflict = client.get(f"/api/projects/{PROJECT_ID}/skill-builder/sessions/{SESSION_ID}")
    invalid = client.post(
        f"/api/projects/{PROJECT_ID}/skill-builder/sessions/{SESSION_ID}/turns",
        json={
            "input": {
                "kind": "draft_update",
                "expected_draft_checksum": "b" * 64,
                "changes": [
                    {
                        "op": "delete",
                        "path": "SKILL.md",
                        "content": "must not be accepted",
                    }
                ],
            },
            "expected_revision": 3,
            "idempotency_key": "draft-invalid",
        },
    )

    assert conflict.status_code == 409
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "asset_validation_failed"


def test_skill_builder_dependency_uses_generation_audit_and_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = object()
    generation = object()
    audit = object()
    quota = object()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                skill_design_generation_service=generation,
                shared_asset_audit_sink=audit,
                project_quota_enforcer=quota,
            )
        )
    )
    monkeypatch.setattr(
        project_skill_builder,
        "get_session_factory",
        lambda: session_factory,
    )

    service = project_skill_builder.get_skill_design_service(request)

    assert service._session_factory is session_factory
    assert service._generator is generation
    assert service._skill_service._governance_sink is audit
    assert service._skill_service._quota is quota
    assert request.app.state.skill_design_service is service


def test_skill_builder_dependency_fails_closed_without_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    monkeypatch.setattr(
        project_skill_builder,
        "get_session_factory",
        lambda: object(),
    )

    with pytest.raises(HTTPException) as captured:
        project_skill_builder.get_skill_design_service(request)

    assert captured.value.status_code == 503
    assert captured.value.detail["code"] == "asset_storage_unavailable"
