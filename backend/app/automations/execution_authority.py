from __future__ import annotations

import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.scheduled_task_runs import ScheduledTaskRunRecord
from deerflow.persistence.scheduled_tasks import ScheduledTaskRecord
from deerflow.runtime.private_scope import PrivateResourceScope

AUTOMATION_AUTHORIZATION_REVOKED_ERROR_CODE = "AUTOMATION_AUTHORIZATION_REVOKED"
_EXECUTABLE_ROLES = frozenset({"admin", "editor", "runner"})


@dataclass(frozen=True, slots=True)
class AutomationExecutionAuthority:
    project_status: str
    project_is_suspended: bool
    membership_status: str
    membership_role: str

    @property
    def can_execute(self) -> bool:
        return self.project_status == "active" and not self.project_is_suspended and self.membership_status == "active" and self.membership_role in _EXECUTABLE_ROLES


@dataclass(frozen=True, slots=True)
class AutomationRetryDenial:
    occurrence_status: str
    error_code: str


async def lock_automation_execution_authority(
    session: AsyncSession,
    scope: PrivateResourceScope,
) -> AutomationExecutionAuthority | None:
    """Lock project then membership without treating inactive state as absent."""

    if type(scope) is not PrivateResourceScope:
        raise TypeError("PrivateResourceScope is required")
    try:
        project_id = uuid.UUID(scope.project_id)
        owner_user_id = str(uuid.UUID(scope.owner_user_id))
    except (TypeError, ValueError):
        raise TypeError("PrivateResourceScope is invalid") from None

    project = (await session.execute(sa.select(ProjectRow).where(ProjectRow.id == project_id).with_for_update(of=ProjectRow))).scalar_one_or_none()
    if project is None:
        return None
    membership = (
        await session.execute(
            sa.select(ProjectMembershipRow)
            .where(
                ProjectMembershipRow.project_id == project_id,
                ProjectMembershipRow.user_id == owner_user_id,
            )
            .with_for_update(of=ProjectMembershipRow)
        )
    ).scalar_one_or_none()
    if membership is None:
        return None
    return AutomationExecutionAuthority(
        project_status=project.status,
        project_is_suspended=project.is_suspended,
        membership_status=membership.status,
        membership_role=membership.role,
    )


def automation_retry_denial(
    authority: AutomationExecutionAuthority | None,
    task: ScheduledTaskRecord | None,
    occurrence: ScheduledTaskRunRecord,
) -> AutomationRetryDenial | None:
    if authority is None or not authority.can_execute or task is None or task.frozen_at is not None or task.deleted_at is not None:
        return AutomationRetryDenial(
            occurrence_status="cancelled",
            error_code=AUTOMATION_AUTHORIZATION_REVOKED_ERROR_CODE,
        )
    if task.version != occurrence.task_version:
        return AutomationRetryDenial(
            occurrence_status="rejected",
            error_code="AUTOMATION_VERSION_CONFLICT",
        )
    allowed_status = task.status == "enabled" or (occurrence.trigger == "manual" and task.status == "paused")
    if not allowed_status:
        return AutomationRetryDenial(
            occurrence_status="rejected",
            error_code="AUTOMATION_CONFLICT",
        )
    return None


__all__ = [
    "AUTOMATION_AUTHORIZATION_REVOKED_ERROR_CODE",
    "AutomationExecutionAuthority",
    "AutomationRetryDenial",
    "automation_retry_denial",
    "lock_automation_execution_authority",
]
