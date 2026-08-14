"""Neutral contract for admitting a durable Skill Builder Run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.context import ProjectContext
from app.shared_assets.skill_design_generation import SkillDesignGenerationRequest
from deerflow.persistence.shared_assets import (
    SkillDesignOperationRow,
    SkillDesignSessionRow,
)


@dataclass(frozen=True, slots=True)
class SkillBuilderRunAdmission:
    run_id: str
    status: str
    thread_id: str


class SkillBuilderRunAdmissionPort(Protocol):
    async def admit_in_session(
        self,
        session: AsyncSession,
        context: ProjectContext,
        design: SkillDesignSessionRow,
        operation: SkillDesignOperationRow,
        request: SkillDesignGenerationRequest,
        *,
        turn_message: str,
        model_name: str | None,
        reasoning_effort: str | None,
    ) -> SkillBuilderRunAdmission: ...


__all__ = ["SkillBuilderRunAdmission", "SkillBuilderRunAdmissionPort"]
