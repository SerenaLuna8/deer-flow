"""Project authorization domain."""

from app.projects.capabilities import Capability, capabilities_for
from app.projects.context import ProjectContext, resolve_project_context
from app.projects.errors import ProjectDatabaseUnavailable, ProjectForbidden, ProjectNotFound, ProjectSlugConflict, ProjectValidationFailed
from app.projects.models import CreateProject, ProjectChanges, ProjectPage, ProjectRole, ProjectView
from app.projects.repository import ProjectRepository
from app.projects.service import ProjectService, normalize_slug

__all__ = [
    "Capability",
    "ProjectContext",
    "ProjectDatabaseUnavailable",
    "ProjectForbidden",
    "ProjectNotFound",
    "ProjectRole",
    "CreateProject",
    "ProjectChanges",
    "ProjectPage",
    "ProjectRepository",
    "ProjectService",
    "ProjectSlugConflict",
    "ProjectValidationFailed",
    "ProjectView",
    "capabilities_for",
    "resolve_project_context",
    "normalize_slug",
]
