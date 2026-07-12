from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from app.projects.models import ProjectRole


@dataclass(frozen=True)
class MembershipView:
    membership_id: uuid.UUID
    user_id: uuid.UUID
    account_email: str
    role: ProjectRole
    status: str
    version: int
    joined_at: datetime
