"""Final-cutover contract for the removed owner-only channel HTTP surface.

Project connections are covered by ``test_project_connections_router.py``.
After the M4 marker completes, every route in the legacy ``/api/channels``
router must stop before reading configuration or calling the now project-
scoped repository.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from _router_auth_helpers import make_authed_test_app
from fastapi.testclient import TestClient

from app.gateway.auth.models import User
from app.gateway.routers import channel_connections
from app.private_work.errors import PrivateWorkCutover


class _CompletedCutoverGuard:
    async def require_legacy_open(self) -> None:
        raise PrivateWorkCutover("req-channel-legacy-cutover")


def _user() -> User:
    return User(
        id=UUID("11111111-2222-3333-4444-555555555555"),
        email="alice@example.com",
        password_hash="x",
        system_role="system_admin",
    )


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("GET", "/api/channels/providers", None),
        ("GET", "/api/channels/connections", None),
        ("POST", "/api/channels/slack/connect", None),
        ("POST", "/api/channels/slack/runtime-config", {"bot_token": "never-read"}),
        ("DELETE", "/api/channels/slack/runtime-config", None),
        ("DELETE", "/api/channels/connections/legacy-connection", None),
    ],
)
def test_legacy_channel_routes_fail_at_completed_cutover(
    method: str,
    path: str,
    json_body: dict | None,
) -> None:
    app = make_authed_test_app(user_factory=_user)
    app.state.private_work_cutover_guard = _CompletedCutoverGuard()
    app.include_router(channel_connections.router)

    with TestClient(app) as client:
        response = client.request(method, path, json=json_body)

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "PRIVATE_WORK_CUTOVER",
        "message": "Private work cutover is not complete.",
        "request_id": "req-channel-legacy-cutover",
    }
