from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from deerflow.constants import DEFAULT_SKILLS_CONTAINER_PATH

SKILL_MD_FILE = "SKILL.md"


class SkillCategory(StrEnum):
    """Source category for a skill.

    - ``PUBLIC``: built-in skill bundled with the platform, read-only.
    - ``CUSTOM``: user-authored skill that can be edited or deleted.
    Runtime Skill objects are materialized only from an immutable run-admission
    snapshot; these values describe their run-local mount layout.
    """

    PUBLIC = "public"
    CUSTOM = "custom"


@dataclass(frozen=True)
class SecretRequirement:
    """One logical Skill secret and its exact Sandbox environment target.

    ``name`` selects the Project-owned value. ``target_env`` is the only child
    process environment variable that may receive it. Historical shorthand
    declarations default the target to the logical name.
    """

    name: str
    optional: bool = False
    target_env: str | None = None

    def __post_init__(self) -> None:
        if self.target_env is None:
            object.__setattr__(self, "target_env", self.name)


@dataclass(frozen=True)
class Skill:
    """Represents a skill with its metadata and file path"""

    name: str
    description: str
    license: str | None
    skill_dir: Path
    skill_file: Path
    relative_path: Path  # Relative path from category root to skill directory
    category: SkillCategory  # 'public' or 'custom'
    allowed_tools: tuple[str, ...] | None = None
    enabled: bool = False  # Whether this skill is enabled
    required_secrets: tuple[SecretRequirement, ...] = field(default_factory=tuple)
    # Whether declared secrets may bind when the skill is in-context via an
    # autonomous model load (skill_context), or only on explicit /slash
    # activation. Frontmatter: ``secrets-autonomous`` (default true).
    secrets_autonomous: bool = True
    runtime_read_only: bool = False

    @property
    def skill_path(self) -> str:
        """Returns the relative path from the category root (skills/{category}) to this skill's directory"""
        path = self.relative_path.as_posix()
        return "" if path == "." else path

    def get_container_path(self, container_base_path: str = DEFAULT_SKILLS_CONTAINER_PATH) -> str:
        """
        Get the full path to this skill in the container.

        Args:
            container_base_path: Base path where skills are mounted in the container

        Returns:
            Full container path to the skill directory
        """
        category_base = f"{container_base_path}/{self.category}"
        skill_path = self.skill_path
        if skill_path:
            return f"{category_base}/{skill_path}"
        return category_base

    def get_container_file_path(self, container_base_path: str = DEFAULT_SKILLS_CONTAINER_PATH) -> str:
        """
        Get the full path to this skill's main file (SKILL.md) in the container.

        Args:
            container_base_path: Base path where skills are mounted in the container

        Returns:
            Full container path to the skill's SKILL.md file
        """
        return f"{self.get_container_path(container_base_path)}/SKILL.md"

    def __repr__(self) -> str:
        return f"Skill(name={self.name!r}, description={self.description!r}, category={self.category!r})"
