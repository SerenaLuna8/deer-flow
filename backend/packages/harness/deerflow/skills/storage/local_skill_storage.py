"""Compatibility tombstone for the removed local Skill storage backend."""

from __future__ import annotations

from deerflow.skills.storage.skill_storage import SkillStorage


class LocalSkillStorage(SkillStorage):
    """No filesystem scanning or mutation; retained only for config parsing."""

    def __init__(
        self,
        host_path: str | None = None,
        container_path: str = "/mnt/skills",
        **_kwargs: object,
    ) -> None:
        super().__init__(container_path=container_path)
