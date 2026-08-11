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
    MCP_CREDENTIALS_APPROVE = "mcp.credentials.approve"
    PRIVATE_WORK_CREATE = "private_work.create"
    PRIVATE_WORK_READ_OWN = "private_work.read_own"
    AUTOMATION_MANAGE_OWN = "automation.manage_own"
    PROJECT_AUDIT_READ = "project.audit.read"
    PROJECT_USAGE_READ = "project.usage.read"
    PROJECT_LIFECYCLE_MANAGE = "project.lifecycle.manage"
    WORKFLOW_READ = "workflow.read"
    WORKFLOW_EDIT = "workflow.edit"
    WORKFLOW_PUBLISH = "workflow.publish"
    WORKFLOW_EXECUTE = "workflow.execute"
    WORKFLOW_CODE_USE = "workflow.code.use"
    WORKFLOW_HTTP_USE = "workflow.http.use"
    WORKFLOW_HTTP_WRITE = "workflow.http.write"
    WORKFLOW_CREDENTIAL_GRANT = "workflow.credential.grant"
    WORKFLOW_RUN_READ_OWN = "workflow.run.read_own"
    WORKFLOW_RUN_CANCEL_OWN = "workflow.run.cancel_own"


WORKFLOW_CAPABILITIES = frozenset(
    {
        Capability.WORKFLOW_READ,
        Capability.WORKFLOW_EDIT,
        Capability.WORKFLOW_PUBLISH,
        Capability.WORKFLOW_EXECUTE,
        Capability.WORKFLOW_CODE_USE,
        Capability.WORKFLOW_HTTP_USE,
        Capability.WORKFLOW_HTTP_WRITE,
        Capability.WORKFLOW_CREDENTIAL_GRANT,
        Capability.WORKFLOW_RUN_READ_OWN,
        Capability.WORKFLOW_RUN_CANCEL_OWN,
    }
)


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
_WORKFLOW_EDITOR = WORKFLOW_CAPABILITIES - {
    Capability.WORKFLOW_CREDENTIAL_GRANT,
}
_WORKFLOW_RUNNER = frozenset(
    {
        Capability.WORKFLOW_READ,
        Capability.WORKFLOW_EXECUTE,
        Capability.WORKFLOW_CODE_USE,
        Capability.WORKFLOW_HTTP_USE,
        Capability.WORKFLOW_RUN_READ_OWN,
        Capability.WORKFLOW_RUN_CANCEL_OWN,
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
    )
    | _WORKFLOW_EDITOR,
    ProjectRole.RUNNER: _BASE
    | _PRIVATE_OWN
    | frozenset(
        {
            Capability.SHARED_ASSETS_READ,
            Capability.SHARED_ASSETS_EXECUTE,
        }
    )
    | _WORKFLOW_RUNNER,
    ProjectRole.VIEWER: _BASE
    | frozenset(
        {
            Capability.PRIVATE_WORK_READ_OWN,
            Capability.SHARED_ASSETS_READ,
        }
    )
    | frozenset({Capability.WORKFLOW_READ}),
    ProjectRole.CHANNEL_GUEST: frozenset(
        {
            Capability.PRIVATE_WORK_CREATE,
            Capability.PRIVATE_WORK_READ_OWN,
            Capability.SHARED_ASSETS_READ,
            Capability.SHARED_ASSETS_EXECUTE,
        }
    ),
}

assert all(not ((capabilities & WORKFLOW_CAPABILITIES) - {Capability.WORKFLOW_READ}) or Capability.WORKFLOW_READ in capabilities for capabilities in PROJECT_ROLE_CAPABILITIES.values())


def capabilities_for(role: ProjectRole) -> frozenset[Capability]:
    return PROJECT_ROLE_CAPABILITIES[role]
