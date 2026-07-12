from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from app.projects.models import ProjectRole


@dataclass(frozen=True)
class InvitationView:
    id: uuid.UUID
    project_id: uuid.UUID
    invited_email: str
    role: ProjectRole
    status: str
    expires_at: datetime
    version: int
    created_at: datetime


@dataclass(frozen=True)
class CreatedInvitation:
    invitation: InvitationView
    token: str = field(repr=False)


@dataclass(frozen=True)
class InvitationClaim:
    invitation_id: uuid.UUID
    token_hash: str = field(repr=False)


@dataclass(frozen=True)
class RedeemedInvitation:
    invitation_id: uuid.UUID
    project_id: uuid.UUID
    project_slug: str
    membership_id: uuid.UUID
    role: ProjectRole


class ProjectInvitationConflict(Exception):
    code = "project_invitation_conflict"

    def __init__(self) -> None:
        super().__init__("Project invitation changed")


class ProjectInvitationInvalid(Exception):
    code = "project_invitation_invalid"

    def __init__(self) -> None:
        super().__init__("Project invitation is invalid")
