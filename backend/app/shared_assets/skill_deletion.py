"""Irreversible Project Skill archival."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.context import ProjectContext
from app.shared_assets.errors import AssetConflict, AssetStorageUnavailable
from app.shared_assets.skill_repository import SkillRepository


@dataclass(frozen=True, slots=True)
class SkillDeleteResult:
    affected_agent_count: int

    def __post_init__(self) -> None:
        if type(self.affected_agent_count) is not int or self.affected_agent_count < 0:
            raise ValueError("affected Agent count must be non-negative")


class AgentSkillDefinitionRemovalPort(Protocol):
    async def remove_project_skill_from_definitions_in_session(
        self,
        session: AsyncSession,
        actor: ProjectContext,
        skill_id: uuid.UUID,
    ) -> tuple[object, ...]: ...


class SkillDeletionCoordinator:
    """One atomic Project-governed Skill archival decision."""

    def __init__(self, agents: AgentSkillDefinitionRemovalPort) -> None:
        self._agents = agents

    async def delete_in_session(
        self,
        session: AsyncSession,
        context: ProjectContext,
        skill_id: uuid.UUID,
        expected_revision: int,
    ) -> SkillDeleteResult:
        if not isinstance(session, AsyncSession) or not session.in_transaction() or not isinstance(context, ProjectContext) or not isinstance(skill_id, uuid.UUID) or type(expected_revision) is not int or expected_revision < 1:
            raise AssetConflict(getattr(context, "request_id", "unknown"))

        repository = SkillRepository(session)
        await repository.lock_project_delete_scope(context)
        asset = await repository.get_project_asset(
            context,
            skill_id,
            for_update=True,
        )
        if asset.revision != expected_revision:
            raise AssetConflict(context.request_id)

        affected_agents = await self._agents.remove_project_skill_from_definitions_in_session(
            session,
            context,
            skill_id,
        )
        if type(affected_agents) is not tuple:
            raise AssetStorageUnavailable(context.request_id)

        # Archival retains immutable Version files, quota reservations, and
        # every encrypted Secret Generation for the lifetime of the Project.
        await repository.archive_project_asset(context, asset)
        return SkillDeleteResult(affected_agent_count=len(affected_agents))


__all__ = [
    "AgentSkillDefinitionRemovalPort",
    "SkillDeleteResult",
    "SkillDeletionCoordinator",
]
