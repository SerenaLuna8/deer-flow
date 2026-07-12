from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.errors import ProjectValidationFailed
from app.projects.models import CreateProject, ProjectChanges, ProjectPage, ProjectView
from app.projects.repository import ProjectRepository

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def normalize_slug(value: str) -> str:
    normalized = value.strip().lower()
    if not 3 <= len(normalized) <= 63 or SLUG_PATTERN.fullmatch(normalized) is None:
        raise ProjectValidationFailed("invalid_slug")
    return normalized


def _text(value: str, *, field: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str):
        raise ProjectValidationFailed(f"invalid_{field}")
    normalized = value.strip() if minimum else value
    if not minimum <= len(normalized) <= maximum:
        raise ProjectValidationFailed(f"invalid_{field}")
    return normalized


class ProjectService:
    def __init__(self, repository: ProjectRepository):
        self.repository = repository

    async def create(self, user_id: uuid.UUID, command: CreateProject, request_id: str) -> ProjectContext:
        validated = CreateProject(
            slug=normalize_slug(command.slug),
            display_name=_text(command.display_name, field="display_name", minimum=1, maximum=120),
            description=_text(command.description, field="description", minimum=0, maximum=500),
            icon=_text(command.icon, field="icon", minimum=1, maximum=32),
        )
        return await self.repository.create_with_admin(user_id, validated, request_id)

    async def get(self, context: ProjectContext) -> ProjectView:
        context.require(Capability.PROJECT_READ)
        return await self.repository.get(context)

    async def update(self, context: ProjectContext, changes: ProjectChanges) -> ProjectView:
        context.require(Capability.PROJECT_UPDATE)
        values = vars(changes)
        if not any(value is not None for value in values.values()):
            raise ProjectValidationFailed("empty_changes")
        validated = ProjectChanges(
            display_name=None if changes.display_name is None else _text(changes.display_name, field="display_name", minimum=1, maximum=120),
            description=None if changes.description is None else _text(changes.description, field="description", minimum=0, maximum=500),
            icon=None if changes.icon is None else _text(changes.icon, field="icon", minimum=1, maximum=32),
        )
        return await self.repository.update(context, validated)

    async def enter(self, context: ProjectContext) -> ProjectView:
        context.require(Capability.PROJECT_ENTER)
        return await self.repository.enter(context, datetime.now(UTC))

    async def pin(self, context: ProjectContext, pinned: bool) -> ProjectView:
        context.require(Capability.PROJECT_PIN)
        if type(pinned) is not bool:
            raise ProjectValidationFailed("invalid_pinned")
        return await self.repository.pin(context, pinned)

    async def list(self, user_id: uuid.UUID, *, query: str | None = None, pinned: bool | None = None, cursor: str | None = None, limit: int = 20, request_id: str) -> ProjectPage:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ProjectValidationFailed("invalid_limit")
        if pinned is not None and type(pinned) is not bool:
            raise ProjectValidationFailed("invalid_pinned")
        normalized_query = None
        if query is not None:
            normalized_query = _text(query, field="query", minimum=0, maximum=120).strip() or None
        return await self.repository.list_for_user(user_id, normalized_query, pinned, cursor, limit, request_id)
