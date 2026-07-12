from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.projects.capabilities import Capability


class ProjectRole(StrEnum):
    ADMIN = "admin"
    EDITOR = "editor"
    RUNNER = "runner"
    VIEWER = "viewer"


@dataclass(frozen=True)
class CreateProject:
    slug: str
    display_name: str
    description: str = ""
    icon: str = "folder"


@dataclass(frozen=True)
class ProjectChanges:
    display_name: str | None = None
    description: str | None = None
    icon: str | None = None


@dataclass(frozen=True)
class ProjectView:
    id: uuid.UUID
    slug: str
    display_name: str
    description: str
    icon: str
    role: ProjectRole
    capabilities: frozenset[Capability]
    is_pinned: bool
    last_entered_at: datetime | None
    member_count: int
    agent_count: int
    skill_count: int
    mcp_count: int
    status: str
    is_suspended: bool
    membership_version: int
    request_id: str


@dataclass(frozen=True)
class ProjectPage:
    items: tuple[ProjectView, ...]
    next_cursor: str | None


class BootstrapStatus(StrEnum):
    NO_USERS = "no_users"
    WAITING_FOR_ADMIN = "waiting_for_admin"
    CREATED = "created"
    EXISTING = "existing"


@dataclass(frozen=True)
class BootstrapResult:
    status: BootstrapStatus
    project_id: uuid.UUID | None = None
