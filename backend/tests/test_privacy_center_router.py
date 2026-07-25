from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.gateway.deps import get_current_user_from_request, project_session
from app.gateway.routers import privacy_center
from app.private_work.privacy_center import (
    PrivacyCaseNotFound,
    PrivacyCaseView,
    PrivacyEarlyDeleteView,
)

USER_ID = uuid.uuid4()
PROJECT_ID = uuid.uuid4()
NOW = datetime(2026, 7, 22, 8, 0, tzinfo=UTC)


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(privacy_center.router)

    async def fake_session():
        yield object()

    app.dependency_overrides[project_session] = fake_session
    app.dependency_overrides[get_current_user_from_request] = lambda: SimpleNamespace(
        id=USER_ID,
    )
    return TestClient(app)


def test_privacy_list_returns_only_safe_project_metadata(monkeypatch) -> None:
    list_cases = AsyncMock(
        return_value=(
            PrivacyCaseView(
                project_id=PROJECT_ID,
                project_slug="former-project",
                project_display_name="Former project",
                project_icon="folder",
                membership_status="left",
                retention_kind="former_owner",
                deletion_deadline=NOW + timedelta(days=30),
                early_delete_requested=False,
            ),
        ),
    )
    monkeypatch.setattr(
        privacy_center.PrivacyCenterService,
        "list_cases",
        list_cases,
    )

    response = _client().get("/api/privacy/cases")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == [
        {
            "project_id": str(PROJECT_ID),
            "project_slug": "former-project",
            "project_display_name": "Former project",
            "project_icon": "folder",
            "membership_status": "left",
            "retention_kind": "former_owner",
            "deletion_deadline": "2026-08-21T08:00:00Z",
            "early_delete_requested": False,
        },
    ]
    assert set(response.json()[0]) == {
        "project_id",
        "project_slug",
        "project_display_name",
        "project_icon",
        "membership_status",
        "retention_kind",
        "deletion_deadline",
        "early_delete_requested",
    }
    assert list_cases.await_args.args[0] == USER_ID


def test_privacy_export_streams_attachment_and_never_adds_cacheability(monkeypatch) -> None:
    async def body():
        yield b'{"record_type":"manifest","schema_version":2}\n'

    export_case = AsyncMock(return_value=body())
    monkeypatch.setattr(
        privacy_center.PrivacyCenterService,
        "open_case_export",
        export_case,
    )

    response = _client().get(f"/api/privacy/cases/{PROJECT_ID}/export")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["content-type"].startswith(
        "application/x-ndjson",
    )
    assert response.content == (b'{"record_type":"manifest","schema_version":2}\n')
    assert export_case.await_args.args[:2] == (USER_ID, PROJECT_ID)


def test_early_delete_returns_durable_job_admission(monkeypatch) -> None:
    job_id = uuid.uuid4()
    request = AsyncMock(
        return_value=PrivacyEarlyDeleteView(
            project_id=PROJECT_ID,
            job_id=job_id,
            status="queued",
        ),
    )
    monkeypatch.setattr(
        privacy_center.PrivacyCenterService,
        "request_early_delete",
        request,
    )

    response = _client().post(
        f"/api/privacy/cases/{PROJECT_ID}/early-delete",
    )

    assert response.status_code == 202
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "project_id": str(PROJECT_ID),
        "job_id": str(job_id),
        "status": "queued",
    }
    assert request.await_args.args[:2] == (USER_ID, PROJECT_ID)


def test_cross_account_privacy_scope_fails_as_public_not_found(monkeypatch) -> None:
    monkeypatch.setattr(
        privacy_center.PrivacyCenterService,
        "open_case_export",
        AsyncMock(side_effect=PrivacyCaseNotFound()),
    )

    response = _client().get(f"/api/privacy/cases/{PROJECT_ID}/export")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "PRIVACY_CASE_NOT_FOUND"
