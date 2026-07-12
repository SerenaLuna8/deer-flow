from __future__ import annotations

from enum import StrEnum


class ProjectRole(StrEnum):
    ADMIN = "admin"
    EDITOR = "editor"
    RUNNER = "runner"
    VIEWER = "viewer"
