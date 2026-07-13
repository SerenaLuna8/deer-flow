from __future__ import annotations

import dataclasses
import uuid

import pytest

from app.projects.capabilities import Capability, capabilities_for
from app.projects.context import ProjectContext
from app.projects.errors import ProjectForbidden
from app.projects.models import ProjectRole

BASE = {
    Capability.PROJECT_READ,
    Capability.PROJECT_ENTER,
    Capability.PROJECT_PIN,
}
PRIVATE = {
    Capability.PRIVATE_WORK_CREATE,
    Capability.PRIVATE_WORK_READ_OWN,
    Capability.AUTOMATION_MANAGE_OWN,
}


def test_capability_enum_and_role_matrix_are_exact_and_complete() -> None:
    assert {role.value for role in ProjectRole} == {"admin", "editor", "runner", "viewer"}
    assert {item.value for item in Capability} == {
        "project.read",
        "project.update",
        "project.enter",
        "project.pin",
        "project.members.manage",
        "shared_assets.read",
        "shared_assets.execute",
        "shared_assets.edit",
        "shared_assets.manage_bindings",
        "mcp.credentials.approve",
        "private_work.create",
        "private_work.read_own",
        "automation.manage_own",
        "project.audit.read",
        "project.usage.read",
        "project.lifecycle.manage",
    }
    assert capabilities_for(ProjectRole.ADMIN) == frozenset(Capability)
    assert capabilities_for(ProjectRole.EDITOR) == frozenset(
        BASE
        | PRIVATE
        | {
            Capability.SHARED_ASSETS_READ,
            Capability.SHARED_ASSETS_EXECUTE,
            Capability.SHARED_ASSETS_EDIT,
        }
    )
    assert capabilities_for(ProjectRole.RUNNER) == frozenset(
        BASE
        | PRIVATE
        | {
            Capability.SHARED_ASSETS_READ,
            Capability.SHARED_ASSETS_EXECUTE,
        }
    )
    assert capabilities_for(ProjectRole.VIEWER) == frozenset(BASE | {Capability.PRIVATE_WORK_READ_OWN, Capability.SHARED_ASSETS_READ})
    assert set().union(*(capabilities_for(role) for role in ProjectRole)) == set(Capability)
    assert all(isinstance(capabilities_for(role), frozenset) for role in ProjectRole)
    admin_only = {
        Capability.PROJECT_UPDATE,
        Capability.PROJECT_MEMBERS_MANAGE,
        Capability.SHARED_ASSETS_MANAGE_BINDINGS,
        Capability.MCP_CREDENTIALS_APPROVE,
        Capability.PROJECT_AUDIT_READ,
        Capability.PROJECT_USAGE_READ,
        Capability.PROJECT_LIFECYCLE_MANAGE,
    }
    for capability in admin_only:
        assert {role for role in ProjectRole if capability in capabilities_for(role)} == {ProjectRole.ADMIN}


def test_only_admin_manages_system_bindings_and_credentials() -> None:
    assert Capability.SHARED_ASSETS_MANAGE_BINDINGS in capabilities_for(ProjectRole.ADMIN)
    assert Capability.MCP_CREDENTIALS_APPROVE in capabilities_for(ProjectRole.ADMIN)
    for role in (ProjectRole.EDITOR, ProjectRole.RUNNER, ProjectRole.VIEWER):
        assert Capability.SHARED_ASSETS_MANAGE_BINDINGS not in capabilities_for(role)
        assert Capability.MCP_CREDENTIALS_APPROVE not in capabilities_for(role)


def test_project_context_is_frozen_and_require_raises_stable_safe_error() -> None:
    context = ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.VIEWER,
        capabilities=capabilities_for(ProjectRole.VIEWER),
        membership_version=3,
        request_id="req-1",
    )
    context.require(Capability.PROJECT_READ)
    with pytest.raises(ProjectForbidden) as exc_info:
        context.require(Capability.PROJECT_UPDATE)
    assert exc_info.value.code == "project_forbidden"
    assert exc_info.value.capability == Capability.PROJECT_UPDATE
    assert str(exc_info.value) == "Project capability required"
    assert str(context.project_id) not in str(exc_info.value)
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.request_id = "changed"  # type: ignore[misc]
