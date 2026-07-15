from __future__ import annotations

import uuid
from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime

import pytest

from app.automations.error_mapping import automation_http_exception
from app.automations.errors import (
    AutomationActiveRun,
    AutomationConcurrencyLimit,
    AutomationCutover,
    AutomationError,
    AutomationForbidden,
    AutomationNotFound,
    AutomationOnceExpired,
    AutomationUnavailable,
    AutomationVersionConflict,
)
from app.automations.models import (
    AutomationChanges,
    AutomationCreate,
    AutomationRunView,
    AutomationView,
)


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (AutomationNotFound("req"), 404, "AUTOMATION_NOT_FOUND"),
        (AutomationForbidden("req"), 403, "AUTOMATION_FORBIDDEN"),
        (AutomationVersionConflict("req"), 409, "AUTOMATION_VERSION_CONFLICT"),
        (AutomationActiveRun("req"), 409, "AUTOMATION_ACTIVE_RUN"),
        (AutomationOnceExpired("req"), 409, "AUTOMATION_ONCE_EXPIRED"),
        (AutomationCutover("req"), 409, "AUTOMATION_CUTOVER"),
        (AutomationConcurrencyLimit("req"), 429, "AUTOMATION_CONCURRENCY_LIMIT"),
        (AutomationUnavailable("req"), 503, "AUTOMATION_UNAVAILABLE"),
    ],
)
def test_automation_error_mapping(
    error: AutomationError,
    status_code: int,
    code: str,
) -> None:
    response = automation_http_exception(error)
    assert response.status_code == status_code
    assert response.detail == {
        "code": code,
        "message": error.public_message,
        "request_id": "req",
    }


def test_automation_error_mapping_rejects_unknown_subclasses() -> None:
    class InternalAutomationError(AutomationError):
        code = "INTERNAL_DETAIL"
        public_message = "internal detail"

    with pytest.raises(TypeError, match="unsupported automation error"):
        automation_http_exception(InternalAutomationError("req"))


def test_automation_invalid_is_public_stable_422_without_internal_details() -> None:
    import app.automations as automations

    error = automations.AutomationInvalid("req")
    error.__cause__ = ValueError("invalid cron provider details")

    response = automation_http_exception(error)

    assert response.status_code == 422
    assert response.detail == {
        "code": "AUTOMATION_INVALID",
        "message": "Automation request is invalid.",
        "request_id": "req",
    }
    assert "provider details" not in repr(response.detail)


def test_automation_commands_are_frozen_and_slotted() -> None:
    create = AutomationCreate(
        title="Daily summary",
        prompt="Summarize project activity.",
        context_mode="fresh_thread_per_run",
        thread_id=None,
        agent_asset_id=uuid.uuid4(),
        agent_scope="system",
        schedule_type="cron",
        schedule_spec={"cron": "0 9 * * *"},
        timezone="Asia/Shanghai",
    )
    changes = AutomationChanges(expected_version=1, title="Updated summary")

    assert create.__dataclass_params__.frozen is True
    assert changes.__dataclass_params__.frozen is True
    assert hasattr(create, "__slots__")
    assert hasattr(changes, "__slots__")
    with pytest.raises(FrozenInstanceError):
        create.title = "forged"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        changes.expected_version = 9  # type: ignore[misc]


def test_automation_views_are_frozen_and_expose_only_public_fields() -> None:
    now = datetime.now(UTC)
    automation = AutomationView(
        id="automation-1",
        thread_id=None,
        context_mode="fresh_thread_per_run",
        agent_asset_id=uuid.uuid4(),
        agent_scope="system",
        title="Daily summary",
        prompt="Summarize project activity.",
        schedule_type="cron",
        schedule_spec={"cron": "0 9 * * *"},
        timezone="Asia/Shanghai",
        status="enabled",
        next_run_at=now,
        last_run_at=None,
        last_outcome=None,
        last_error_code=None,
        run_count=0,
        version=1,
        created_at=now,
        updated_at=now,
    )
    occurrence = AutomationRunView(
        id="occurrence-1",
        automation_id=automation.id,
        automation_version=automation.version,
        scheduled_for=now,
        trigger="scheduled",
        status="queued",
        thread_id=None,
        run_id=None,
        error_code=None,
        started_at=None,
        finished_at=None,
        created_at=now,
        updated_at=now,
    )

    forbidden = {
        "project_id",
        "owner_user_id",
        "user_id",
        "lease_owner",
        "lease_expires_at",
        "occurrence_key",
        "manual_idempotency_hash",
        "resolved_membership_id",
        "resolved_membership_version",
        "runtime_kwargs",
        "credential_id",
        "credential_version_id",
        "error_message",
    }
    assert automation.__dataclass_params__.frozen is True
    assert occurrence.__dataclass_params__.frozen is True
    assert forbidden.isdisjoint(field.name for field in fields(AutomationView))
    assert forbidden.isdisjoint(field.name for field in fields(AutomationRunView))
