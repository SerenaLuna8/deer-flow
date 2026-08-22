from __future__ import annotations

from enum import StrEnum

from app.projects.models import ProjectRole


class Capability(StrEnum):
    PROJECT_READ = "project.read"
    PROJECT_UPDATE = "project.update"
    PROJECT_ENTER = "project.enter"
    PROJECT_PIN = "project.pin"
    PROJECT_MEMBERS_MANAGE = "project.members.manage"
    PROJECT_CHANNELS_MANAGE = "project.channels.manage"
    SHARED_ASSETS_READ = "shared_assets.read"
    SHARED_ASSETS_EXECUTE = "shared_assets.execute"
    SHARED_ASSETS_EDIT = "shared_assets.edit"
    SHARED_ASSETS_MANAGE_BINDINGS = "shared_assets.manage_bindings"
    PRIVATE_WORK_CREATE = "private_work.create"
    PRIVATE_WORK_READ_OWN = "private_work.read_own"
    PRIVATE_WORK_APPROVE_HOST_EXECUTION = "private_work.approve_host_execution"
    AUTOMATION_MANAGE_OWN = "automation.manage_own"
    PROJECT_AUDIT_READ = "project.audit.read"
    PROJECT_USAGE_READ = "project.usage.read"
    PROJECT_LIFECYCLE_MANAGE = "project.lifecycle.manage"


_BASE = frozenset(
    {
        Capability.PROJECT_READ,
        Capability.PROJECT_ENTER,
        Capability.PROJECT_PIN,
    }
)
_PRIVATE_OWN = frozenset(
    {
        Capability.PRIVATE_WORK_CREATE,
        Capability.PRIVATE_WORK_READ_OWN,
        Capability.AUTOMATION_MANAGE_OWN,
    }
)

PROJECT_ROLE_CAPABILITIES: dict[ProjectRole, frozenset[Capability]] = {
    ProjectRole.ADMIN: frozenset(Capability),
    ProjectRole.EDITOR: _BASE
    | _PRIVATE_OWN
    | frozenset(
        {
            Capability.SHARED_ASSETS_READ,
            Capability.SHARED_ASSETS_EXECUTE,
            Capability.SHARED_ASSETS_EDIT,
        }
    ),
    ProjectRole.RUNNER: _BASE
    | _PRIVATE_OWN
    | frozenset(
        {
            Capability.SHARED_ASSETS_READ,
            Capability.SHARED_ASSETS_EXECUTE,
        }
    ),
    ProjectRole.VIEWER: _BASE
    | frozenset(
        {
            Capability.PRIVATE_WORK_READ_OWN,
            Capability.SHARED_ASSETS_READ,
        }
    ),
    ProjectRole.CHANNEL_GUEST: frozenset(
        {
            Capability.PRIVATE_WORK_CREATE,
            Capability.PRIVATE_WORK_READ_OWN,
            Capability.SHARED_ASSETS_READ,
            Capability.SHARED_ASSETS_EXECUTE,
        }
    ),
}


def capabilities_for(role: ProjectRole) -> frozenset[Capability]:
    return PROJECT_ROLE_CAPABILITIES[role]
