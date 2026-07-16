from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.automations.execution_authority import (
    AUTOMATION_AUTHORIZATION_REVOKED_ERROR_CODE,
    AutomationExecutionAuthority,
    automation_retry_denial,
    lock_automation_execution_authority,
)
from deerflow.runtime.private_scope import PrivateResourceScope


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


@pytest.mark.asyncio
async def test_execution_authority_locks_project_then_membership() -> None:
    project_id = uuid.uuid4()
    owner_user_id = uuid.uuid4()
    session = AsyncMock()
    session.execute.side_effect = [
        _ScalarResult(
            SimpleNamespace(
                status="active",
                is_suspended=False,
            )
        ),
        _ScalarResult(
            SimpleNamespace(
                status="active",
                role="runner",
            )
        ),
    ]

    authority = await lock_automation_execution_authority(
        session,
        PrivateResourceScope(
            project_id=str(project_id),
            owner_user_id=str(owner_user_id),
            membership_version=1,
        ),
    )

    assert authority is not None and authority.can_execute
    statements = [str(call.args[0]) for call in session.execute.await_args_list]
    assert "FROM projects" in statements[0]
    assert "project_memberships" not in statements[0]
    assert "FOR UPDATE" in statements[0]
    assert "FROM project_memberships" in statements[1]
    assert "FOR UPDATE" in statements[1]


@pytest.mark.parametrize(
    ("authority", "task", "expected_status", "expected_code"),
    [
        (
            AutomationExecutionAuthority("active", False, "removed", "runner"),
            SimpleNamespace(
                status="enabled",
                version=1,
                frozen_at=None,
                deleted_at=None,
            ),
            "cancelled",
            AUTOMATION_AUTHORIZATION_REVOKED_ERROR_CODE,
        ),
        (
            AutomationExecutionAuthority("active", False, "active", "runner"),
            SimpleNamespace(
                status="paused",
                version=2,
                frozen_at=object(),
                deleted_at=None,
            ),
            "cancelled",
            AUTOMATION_AUTHORIZATION_REVOKED_ERROR_CODE,
        ),
        (
            AutomationExecutionAuthority("active", False, "active", "runner"),
            SimpleNamespace(
                status="enabled",
                version=2,
                frozen_at=None,
                deleted_at=None,
            ),
            "rejected",
            "AUTOMATION_VERSION_CONFLICT",
        ),
    ],
)
def test_retry_denial_is_stable_for_authority_freeze_and_version_drift(
    authority,
    task,
    expected_status: str,
    expected_code: str,
) -> None:
    denial = automation_retry_denial(
        authority,
        task,
        SimpleNamespace(
            task_version=1,
            trigger="scheduled",
        ),
    )

    assert denial is not None
    assert denial.occurrence_status == expected_status
    assert denial.error_code == expected_code


@pytest.mark.parametrize(
    ("task_status", "trigger"),
    [("enabled", "scheduled"), ("paused", "manual")],
)
def test_retry_remains_allowed_for_current_executable_definition(
    task_status: str,
    trigger: str,
) -> None:
    denial = automation_retry_denial(
        AutomationExecutionAuthority("active", False, "active", "runner"),
        SimpleNamespace(
            status=task_status,
            version=1,
            frozen_at=None,
            deleted_at=None,
        ),
        SimpleNamespace(
            task_version=1,
            trigger=trigger,
        ),
    )

    assert denial is None
