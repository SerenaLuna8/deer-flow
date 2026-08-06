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


class ProjectValidationFailed(Exception):
    code = "project_validation_failed"

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("Project validation failed")


class ProjectSlugConflict(Exception):
    code = "project_slug_conflict"

    def __init__(self) -> None:
        super().__init__("Project slug already exists")


class ProjectDatabaseUnavailable(Exception):
    code = "project_database_unavailable"

    def __init__(self) -> None:
        super().__init__("Project storage unavailable")


class ProjectLastAdmin(Exception):
    code = "project_last_admin"

    def __init__(self) -> None:
        super().__init__("Project must keep an active admin")


class ProjectMembershipVersionConflict(Exception):
    code = "project_membership_version_conflict"

    def __init__(self) -> None:
        super().__init__("Project membership changed")


class ProjectMemberQuotaExceeded(Exception):
    code = "project_member_quota_exceeded"

    def __init__(self) -> None:
        super().__init__("Project member quota was exceeded")


class ProjectQuotaStateConflict(Exception):
    code = "project_quota_state_conflict"

    def __init__(self) -> None:
        super().__init__("Project quota state conflict")


class ProjectDeletionStateConflict(Exception):
    code = "project_deletion_state_conflict"

    def __init__(self) -> None:
        super().__init__("Project deletion state does not allow this operation")


class ProjectBootstrapFailed(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("Project bootstrap failed")
