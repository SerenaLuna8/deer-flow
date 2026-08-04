from __future__ import annotations

from app.gateway.public_project_roles import (
    PublicInvitationRole,
    PublicProjectRole,
)
from app.gateway.routers.notifications import NotificationResponse
from app.gateway.routers.project_invitations import (
    InvitationCreateRequest,
    InvitationResponse,
)
from app.gateway.routers.project_members import (
    MembershipMutationRequest,
    MembershipResponse,
)
from app.gateway.routers.projects import ProjectResponse


def test_channel_guest_role_is_absent_from_every_public_project_contract() -> None:
    assert [role.value for role in PublicProjectRole] == [
        "admin",
        "editor",
        "runner",
        "viewer",
    ]
    assert [role.value for role in PublicInvitationRole] == [
        "editor",
        "runner",
        "viewer",
    ]

    models = (
        ProjectResponse,
        MembershipMutationRequest,
        MembershipResponse,
        InvitationCreateRequest,
        InvitationResponse,
        NotificationResponse,
    )
    for model in models:
        assert "channel_guest" not in str(model.model_json_schema())
