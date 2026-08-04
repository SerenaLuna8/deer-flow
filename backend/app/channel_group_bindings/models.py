from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

GroupBindingStatus = Literal["active", "disabled"]
GroupBindingAgentScope = Literal["project", "system"]


@dataclass(frozen=True, slots=True)
class CreateGroupBindingChallenge:
    provider: str
    agent_asset_id: uuid.UUID
    agent_scope: GroupBindingAgentScope


@dataclass(frozen=True, slots=True)
class UpdateGroupBinding:
    expected_revision: int
    enabled: bool | None = None
    agent_asset_id: uuid.UUID | None = None
    agent_scope: GroupBindingAgentScope | None = None


@dataclass(frozen=True, slots=True)
class GroupBindingChallengeView:
    provider: str
    code: str = field(repr=False)
    command: str = field(repr=False)
    expires_at: datetime
    expires_in: int


@dataclass(frozen=True, slots=True)
class ProjectChannelGroupBindingView:
    id: uuid.UUID
    provider: str
    display_name: str
    status: GroupBindingStatus
    agent_asset_id: uuid.UUID
    agent_scope: GroupBindingAgentScope
    last_activity_at: datetime | None
    revision: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class GroupBindingGuestAuthority:
    project_id: uuid.UUID
    group_binding_id: uuid.UUID
    principal_user_id: uuid.UUID
    membership_id: uuid.UUID
    membership_version: int
    agent_asset_id: uuid.UUID
    agent_scope: GroupBindingAgentScope


__all__ = [
    "CreateGroupBindingChallenge",
    "GroupBindingAgentScope",
    "GroupBindingChallengeView",
    "GroupBindingGuestAuthority",
    "GroupBindingStatus",
    "ProjectChannelGroupBindingView",
    "UpdateGroupBinding",
]
