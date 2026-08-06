from __future__ import annotations

from enum import StrEnum


class PublicProjectRole(StrEnum):
    """Human project roles that may cross the public Gateway boundary."""

    ADMIN = "admin"
    EDITOR = "editor"
    RUNNER = "runner"
    VIEWER = "viewer"


class PublicInvitationRole(StrEnum):
    """Human non-admin roles accepted by project invitations."""

    EDITOR = "editor"
    RUNNER = "runner"
    VIEWER = "viewer"


__all__ = ["PublicInvitationRole", "PublicProjectRole"]
