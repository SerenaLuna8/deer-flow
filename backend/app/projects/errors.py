from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.projects.capabilities import Capability


class ProjectNotFound(Exception):
    code = "project_not_found"

    def __init__(self) -> None:
        super().__init__("Project not found")


class ProjectForbidden(Exception):
    code = "project_forbidden"

    def __init__(self, capability: Capability) -> None:
        self.capability = capability
        super().__init__("Project capability required")
