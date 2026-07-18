"""Fail-closed compatibility types for removed file-backed Skill storage."""

from deerflow.skills.storage.local_skill_storage import LocalSkillStorage
from deerflow.skills.storage.skill_storage import SkillStorage

__all__ = ["LocalSkillStorage", "SkillStorage"]
