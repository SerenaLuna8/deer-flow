"""Compatibility facade for Skill Builder Run admission."""

from app.private_work.skill_builder_run_admission import (
    SkillBuilderRunAdmissionService,
)
from app.shared_assets.skill_builder_admission_contract import (
    SkillBuilderRunAdmission,
    SkillBuilderRunAdmissionPort,
)

__all__ = [
    "SkillBuilderRunAdmission",
    "SkillBuilderRunAdmissionPort",
    "SkillBuilderRunAdmissionService",
]
