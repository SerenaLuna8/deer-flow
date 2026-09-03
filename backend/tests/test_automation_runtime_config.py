from __future__ import annotations

import uuid

from app.automations.dispatcher import AutomationDispatcher
from app.private_work.context import PrivateWorkContext
from app.private_work.run_repository import PrivateRunCreate
from app.private_work.snapshot_admission_rules import _apply_runtime_recursion_limit
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.system_runtime_settings.models import (
    AgentRuntimePolicyValue,
    LockedAgentRuntimePolicy,
)


def _private_context() -> PrivateWorkContext:
    role = ProjectRole.ADMIN
    return PrivateWorkContext.from_project(
        ProjectContext(
            user_id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
            project_id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
            membership_id=uuid.UUID("33333333-3333-4333-8333-333333333333"),
            role=role,
            capabilities=capabilities_for(role),
            membership_version=7,
            request_id="automation-runtime-config",
        )
    )


def test_automation_runtime_config_requests_long_run_recursion_limit() -> None:
    """A missing Automation override must not fall back to the generic limit."""

    thread_id = "44444444-4444-4444-8444-444444444444"
    config = AutomationDispatcher._private_runtime_config(
        _private_context(),
        thread_id=thread_id,
        metadata={"scheduled_task_id": "daily-research"},
    )

    assert config["recursion_limit"] == 1_000
    assert config["context"]["non_interactive"] is True
    assert config["context"]["thread_id"] == thread_id
    assert config["configurable"]["thread_id"] == thread_id
    assert config["configurable"]["checkpoint_ns"] == ""


def test_automation_runtime_config_is_clamped_by_the_locked_runtime_policy() -> None:
    config = AutomationDispatcher._private_runtime_config(
        _private_context(),
        thread_id="44444444-4444-4444-8444-444444444444",
        metadata={"scheduled_task_id": "daily-research"},
    )
    request = PrivateRunCreate(kwargs={"config": config})
    policy = LockedAgentRuntimePolicy(
        policy_version_id=uuid.UUID("55555555-5555-4555-8555-555555555555"),
        revision=1,
        schema_version=1,
        payload_checksum="a" * 64,
        value=AgentRuntimePolicyValue(max_recursion_limit=77),
    )

    clamped = _apply_runtime_recursion_limit(request, policy)

    assert clamped.kwargs["config"]["recursion_limit"] == 77
    assert request.kwargs["config"]["recursion_limit"] == 1_000
