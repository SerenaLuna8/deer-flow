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
from app.gateway.routers import notifications, project_invitations
from app.notifications.models import InvitationNotificationView, NotificationPage
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
    app.include_router(notifications.router)

    async def fake_session():
        yield object()

    app.dependency_overrides[project_session] = fake_session
    app.dependency_overrides[project_invitations.authenticated_invitation_identity] = lambda: (
        USER_ID,
        "member@example.com",
        "req-invitations",
    )
    app.dependency_overrides[notifications.authenticated_invitation_identity] = lambda: (
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


def test_notification_list_is_account_scoped_and_enriched_without_secrets(
    monkeypatch,
) -> None:
    notification = InvitationNotificationView(
        id=INVITATION_ID,
        project_id=PROJECT_ID,
        project_slug="research-lab",
        project_display_name="Research Lab",
        inviter_email="owner@example.com",
        role=ProjectRole.VIEWER,
        status="pending",
        is_read=False,
        created_at=NOW,
        expires_at=NOW + timedelta(days=7),
        version=1,
    )
    list_notifications = AsyncMock(
        return_value=NotificationPage(
            items=(notification,),
            unread_count=1,
        )
    )
    monkeypatch.setattr(
        notifications.InvitationService,
        "list_notifications",
        list_notifications,
    )

    response = TestClient(_app()).get("/api/notifications")

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "id": str(INVITATION_ID),
                "kind": "project_invitation",
                "project": {
                    "id": str(PROJECT_ID),
                    "slug": "research-lab",
                    "display_name": "Research Lab",
                },
                "actor": {"email": "owner@example.com"},
                "role": "viewer",
                "status": "pending",
                "is_read": False,
                "expires_at": (NOW + timedelta(days=7)).isoformat().replace("+00:00", "Z"),
                "version": 1,
                "created_at": NOW.isoformat().replace("+00:00", "Z"),
            }
        ],
        "next_cursor": None,
        "unread_count": 1,
    }
    assert "token" not in response.text
    assert "hash" not in response.text
    list_notifications.assert_awaited_once()
    assert list_notifications.await_args.args[0] == USER_ID
    assert list_notifications.await_args.kwargs == {
        "cursor": None,
        "limit": 50,
    }


def test_notification_list_forwards_cursor_and_bounded_limit(monkeypatch) -> None:
    list_notifications = AsyncMock(return_value=NotificationPage(items=(), unread_count=0))
    monkeypatch.setattr(
        notifications.InvitationService,
        "list_notifications",
        list_notifications,
    )

    response = TestClient(_app()).get("/api/notifications?cursor=opaque-cursor&limit=75")

    assert response.status_code == 200
    assert list_notifications.await_args.kwargs == {
        "cursor": "opaque-cursor",
        "limit": 75,
    }


def test_notification_list_rejects_invalid_cursor_with_stable_validation_error() -> None:
    response = TestClient(_app()).get("/api/notifications?cursor=not-a-valid-cursor")

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "PROJECT_VALIDATION_FAILED",
        "message": "Project validation failed",
        "request_id": "req-invitations",
    }


def test_notification_read_all_returns_single_account_update_count(
    monkeypatch,
) -> None:
    mark_all = AsyncMock(return_value=3)
    monkeypatch.setattr(
        notifications.InvitationService,
        "mark_all_notifications_read",
        mark_all,
    )

    response = TestClient(_app()).post("/api/notifications/read-all")

    assert response.status_code == 200
    assert response.json() == {"marked_count": 3}
    assert mark_all.await_args.args[0] == USER_ID


def test_notification_read_is_recipient_scoped_and_returns_updated_item(
    monkeypatch,
) -> None:
    notification = InvitationNotificationView(
        id=INVITATION_ID,
        project_id=PROJECT_ID,
        project_slug="research-lab",
        project_display_name="Research Lab",
        inviter_email="owner@example.com",
        role=ProjectRole.VIEWER,
        status="pending",
        is_read=True,
        created_at=NOW,
        expires_at=NOW + timedelta(days=7),
        version=1,
    )
    mark_read = AsyncMock(return_value=notification)
    monkeypatch.setattr(
        notifications.InvitationService,
        "mark_notification_read",
        mark_read,
    )

    response = TestClient(_app()).post(f"/api/notifications/{INVITATION_ID}/read")

    assert response.status_code == 200
    assert response.json()["is_read"] is True
    assert mark_read.await_args.args[:2] == (USER_ID, INVITATION_ID)


def test_notification_accept_uses_authenticated_account_and_returns_membership(
    monkeypatch,
) -> None:
    redeemed = RedeemedInvitation(
        invitation_id=INVITATION_ID,
        project_id=PROJECT_ID,
        project_slug="research-lab",
        membership_id=uuid.uuid4(),
        role=ProjectRole.VIEWER,
    )
    accept = AsyncMock(return_value=redeemed)
    monkeypatch.setattr(
        notifications.InvitationService,
        "accept_notification",
        accept,
    )

    response = TestClient(_app()).post(
        f"/api/notifications/{INVITATION_ID}/accept",
        json={"version": 1},
    )

    assert response.status_code == 200
    assert response.json()["project_slug"] == "research-lab"
    assert "token" not in response.text
    assert accept.await_args.args[:2] == (
        USER_ID,
        INVITATION_ID,
    )
    assert accept.await_args.kwargs["expected_version"] == 1
    assert accept.await_args.kwargs["request_id"] == "req-invitations"


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
