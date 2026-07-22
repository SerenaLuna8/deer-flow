from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.gateway.auth_middleware import _is_public
from app.gateway.deps import project_session
from app.gateway.routers import project_invitations
from app.projects.capabilities import Capability
from app.projects.errors import (
    ProjectForbidden,
    ProjectMemberQuotaExceeded,
    ProjectNotFound,
)
from app.projects.invitation_models import (
    CreatedInvitation,
    InvitationClaim,
    InvitationView,
    ProjectInvitationInvalid,
    RedeemedInvitation,
)
from app.projects.models import ProjectRole

USER_ID = uuid.uuid4()
PROJECT_ID = uuid.uuid4()
INVITATION_ID = uuid.uuid4()
NOW = datetime(2026, 7, 12, 9, 0, tzinfo=UTC)


def _invitation() -> InvitationView:
    return InvitationView(
        id=INVITATION_ID,
        project_id=PROJECT_ID,
        invited_email="member@example.com",
        role=ProjectRole.VIEWER,
        status="pending",
        expires_at=NOW + timedelta(days=7),
        version=1,
        created_at=NOW,
    )


def _app() -> FastAPI:
    app = FastAPI()
    app.state.project_quota_enforcer = object()
    app.state.operational_audit_sink = AsyncMock()
    app.include_router(project_invitations.router)

    async def fake_session():
        yield object()

    app.dependency_overrides[project_session] = fake_session
    app.dependency_overrides[project_invitations.authenticated_invitation_identity] = lambda: (
        USER_ID,
        "member@example.com",
        "req-invitations",
    )
    app.dependency_overrides[project_invitations.authenticated_project_identity] = lambda: (
        USER_ID,
        "req-invitations",
    )
    return app


def test_editor_cannot_create_invitation_with_stable_error(monkeypatch) -> None:
    monkeypatch.setattr(project_invitations, "resolve_project_context", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        project_invitations.InvitationService,
        "create",
        AsyncMock(side_effect=ProjectForbidden(Capability.PROJECT_MEMBERS_MANAGE)),
    )

    response = TestClient(_app()).post(
        f"/api/projects/{PROJECT_ID}/invitations",
        json={"email": "new@example.com", "role": "viewer"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == {
        "code": "PROJECT_MEMBERSHIP_FORBIDDEN",
        "message": "Project membership does not allow this operation",
        "request_id": "req-invitations",
    }


def test_create_returns_fragment_once_while_ordinary_responses_hide_token(monkeypatch) -> None:
    monkeypatch.setattr(project_invitations, "resolve_project_context", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        project_invitations.InvitationService,
        "create",
        AsyncMock(return_value=CreatedInvitation(_invitation(), "plain-token")),
    )
    monkeypatch.setattr(
        project_invitations.InvitationService,
        "list_for_project",
        AsyncMock(return_value=(_invitation(),)),
    )
    client = TestClient(_app())

    created = client.post(
        f"/api/projects/{PROJECT_ID}/invitations",
        json={"email": "member@example.com", "role": "viewer"},
    )
    listed = client.get(f"/api/projects/{PROJECT_ID}/invitations")

    assert created.status_code == 201
    assert created.json()["invite_url_fragment"] == "/invite#token=plain-token"
    assert "token" not in created.json()
    assert listed.status_code == 200
    assert "token" not in listed.text
    assert "hash" not in listed.text


def test_cross_project_invitation_revoke_is_404(monkeypatch) -> None:
    monkeypatch.setattr(project_invitations, "resolve_project_context", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        project_invitations.InvitationService,
        "revoke",
        AsyncMock(side_effect=ProjectNotFound()),
    )

    response = TestClient(_app()).request(
        "DELETE",
        f"/api/projects/{PROJECT_ID}/invitations/{INVITATION_ID}",
        json={"version": 1},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == {
        "code": "PROJECT_OR_MEMBER_NOT_FOUND",
        "message": "Project or member not found",
        "request_id": "req-invitations",
    }


def test_same_project_non_pending_invitation_revoke_is_409(monkeypatch) -> None:
    monkeypatch.setattr(project_invitations, "resolve_project_context", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        project_invitations.InvitationService,
        "revoke",
        AsyncMock(side_effect=ProjectInvitationInvalid()),
    )

    response = TestClient(_app()).request(
        "DELETE",
        f"/api/projects/{PROJECT_ID}/invitations/{INVITATION_ID}",
        json={"version": 2},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PROJECT_INVITATION_INVALID"


def test_mine_is_scoped_to_authenticated_email_and_hides_secrets(monkeypatch) -> None:
    list_mine = AsyncMock(return_value=(_invitation(),))
    monkeypatch.setattr(project_invitations.InvitationService, "list_mine", list_mine)

    response = TestClient(_app()).get("/api/project-invitations/mine")

    assert response.status_code == 200
    assert response.json()[0]["invited_email"] == "member@example.com"
    assert "token" not in response.text
    assert "hash" not in response.text
    assert list_mine.await_args.args[0] == "member@example.com"


def test_claim_valid_and_invalid_tokens_are_indistinguishable(monkeypatch) -> None:
    valid_claim = InvitationClaim(INVITATION_ID, "a" * 64)
    claim = AsyncMock(side_effect=[valid_claim, ProjectInvitationInvalid()])
    monkeypatch.setattr(project_invitations.InvitationService, "claim", claim)
    admit_attempt = AsyncMock(return_value=True)
    monkeypatch.setattr(
        project_invitations.InvitationRateLimitRepository,
        "admit_attempt",
        admit_attempt,
    )
    monkeypatch.setattr(
        project_invitations.InvitationRateLimitRepository,
        "clear",
        AsyncMock(),
    )
    client = TestClient(_app())

    valid = client.post(
        "/api/project-invitations/claim",
        json={"token": "valid-token"},
    )
    invalid = client.post(
        "/api/project-invitations/claim",
        json={"token": "invalid-token"},
    )

    assert valid.status_code == invalid.status_code == 200
    assert valid.json() == invalid.json() == {"message": "Invitation claim processed"}
    for response in (valid, invalid):
        cookie = response.headers["set-cookie"]
        assert "project_invitation_claim=" in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie
        assert "Path=/api/project-invitations" in cookie
        assert "Max-Age=600" in cookie
        assert "valid-token" not in cookie
        assert "invalid-token" not in cookie
        parsed = SimpleCookie()
        parsed.load(cookie)
        opaque = parsed["project_invitation_claim"].value
        raw = base64.urlsafe_b64decode(opaque + "=" * (-len(opaque) % 4))
        assert b"invitation_id" not in raw
        assert b"token_hash" not in raw
        with pytest.raises((UnicodeDecodeError, json.JSONDecodeError)):
            json.loads(raw.decode("utf-8"))
    assert len(admit_attempt.await_args_list) == 2
    assert all(len(call.args) == 1 for call in admit_attempt.await_args_list)


def test_claim_rate_limit_is_not_observable_from_status_body_or_cookie_shape(monkeypatch) -> None:
    monkeypatch.setattr(
        project_invitations.InvitationRateLimitRepository,
        "admit_attempt",
        AsyncMock(return_value=False),
    )
    claim = AsyncMock()
    monkeypatch.setattr(project_invitations.InvitationService, "claim", claim)

    response = TestClient(_app()).post(
        "/api/project-invitations/claim",
        json={"token": "candidate-token"},
    )

    assert response.status_code == 200
    assert response.json() == {"message": "Invitation claim processed"}
    assert "project_invitation_claim=" in response.headers["set-cookie"]
    claim.assert_not_awaited()


def test_claim_is_public_and_secure_cookie_follows_request_scheme(monkeypatch) -> None:
    monkeypatch.setattr(
        project_invitations.InvitationRateLimitRepository,
        "admit_attempt",
        AsyncMock(return_value=False),
    )
    response = TestClient(_app(), base_url="https://testserver").post(
        "/api/project-invitations/claim",
        json={"token": "candidate-token"},
    )

    assert _is_public("/api/project-invitations/claim")
    assert response.status_code == 200
    assert "Secure" in response.headers["set-cookie"]


def test_cross_site_claim_rejection_uses_stable_error_shape() -> None:
    response = TestClient(_app()).post(
        "/api/project-invitations/claim",
        headers={"origin": "https://attacker.example"},
        json={"token": "candidate-token"},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "PROJECT_VALIDATION_FAILED"
    assert detail["message"] == "Project validation failed"
    assert isinstance(detail["request_id"], str) and detail["request_id"]


def test_uninitialized_project_session_503_has_request_id(monkeypatch) -> None:
    from deerflow.persistence import engine as persistence_engine

    def unavailable_factory():
        raise RuntimeError("Persistence engine is not initialized")

    monkeypatch.setattr(persistence_engine, "get_session_factory", unavailable_factory)
    app = FastAPI()
    app.include_router(project_invitations.router)
    app.dependency_overrides[project_invitations.authenticated_invitation_identity] = lambda: (
        USER_ID,
        "member@example.com",
        "req-invitations",
    )

    response = TestClient(app).get("/api/project-invitations/mine")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "DATABASE_UNAVAILABLE"
    assert response.json()["detail"]["message"] == "Project storage unavailable"
    assert isinstance(response.json()["detail"]["request_id"], str)
    assert response.json()["detail"]["request_id"]


def test_redeem_validates_authenticated_email_and_always_clears_cookie(monkeypatch) -> None:
    signer = Mock()
    claim = InvitationClaim(INVITATION_ID, "c" * 64)
    signer.verify.return_value = claim
    monkeypatch.setattr(project_invitations, "claim_signer", lambda: signer)
    monkeypatch.setattr(
        project_invitations.InvitationRateLimitRepository,
        "admit_attempt",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        project_invitations.InvitationRateLimitRepository,
        "clear",
        AsyncMock(),
    )
    redeem = AsyncMock(
        side_effect=[
            RedeemedInvitation(
                invitation_id=INVITATION_ID,
                project_id=PROJECT_ID,
                project_slug="alpha",
                membership_id=uuid.uuid4(),
                role=ProjectRole.VIEWER,
            ),
            ProjectInvitationInvalid(),
        ]
    )
    monkeypatch.setattr(project_invitations.InvitationService, "redeem", redeem)
    client = TestClient(_app())
    client.cookies.set("project_invitation_claim", "signed-cookie", path="/api/project-invitations")

    success = client.post("/api/project-invitations/redeem")
    client.cookies.set("project_invitation_claim", "signed-cookie", path="/api/project-invitations")
    failure = client.post("/api/project-invitations/redeem")

    assert success.status_code == 200
    assert success.json()["project_slug"] == "alpha"
    assert failure.status_code == 409
    assert failure.json()["detail"]["code"] == "PROJECT_INVITATION_INVALID"
    for response in (success, failure):
        cookie = response.headers["set-cookie"]
        assert "project_invitation_claim=" in cookie
        assert "Max-Age=0" in cookie
        assert "Path=/api/project-invitations" in cookie
    assert [call.args[1] for call in redeem.await_args_list] == ["member@example.com"] * 2


def test_redeem_member_quota_returns_stable_429_and_retry_after(monkeypatch) -> None:
    signer = Mock()
    signer.verify.return_value = InvitationClaim(INVITATION_ID, "c" * 64)
    monkeypatch.setattr(project_invitations, "claim_signer", lambda: signer)
    monkeypatch.setattr(
        project_invitations.InvitationRateLimitRepository,
        "admit_attempt",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        project_invitations.InvitationService,
        "redeem",
        AsyncMock(side_effect=ProjectMemberQuotaExceeded()),
    )
    client = TestClient(_app())
    client.cookies.set(
        "project_invitation_claim",
        "signed-cookie",
        path="/api/project-invitations",
    )

    response = client.post("/api/project-invitations/redeem")

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "1"
    assert response.json()["detail"]["code"] == "PROJECT_MEMBER_QUOTA_EXCEEDED"
