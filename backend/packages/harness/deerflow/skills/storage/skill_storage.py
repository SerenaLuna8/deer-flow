"""Compatibility type for the removed file-backed Skill storage boundary."""

from __future__ import annotations

from deerflow.assets.catalog import AssetCatalogUnavailable
from deerflow.constants import DEFAULT_SKILLS_CONTAINER_PATH


class SkillStorage:
    """Fail-closed tombstone; runtime Skills must come from run admission."""

    def __init__(self, container_path: str = DEFAULT_SKILLS_CONTAINER_PATH) -> None:
        self._container_root = container_path

    def get_container_root(self) -> str:
        return self._container_root

    def load_skills(self, *, enabled_only: bool = False):
        raise AssetCatalogUnavailable("file-backed Skill discovery was removed; use the admitted run snapshot")
