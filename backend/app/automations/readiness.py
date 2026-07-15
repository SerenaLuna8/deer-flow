from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.automations.cutover import AutomationCutoverGuard
from app.automations.errors import AutomationCutover, AutomationUnavailable
from app.private_work.context import PrivateWorkContext
from app.private_work.cutover import PrivateWorkCutoverGuard
from app.private_work.errors import PrivateWorkCutover, PrivateWorkUnavailable
from deerflow.persistence.revisions import REVISION_ANCESTRY, RevisionAncestry

AUTOMATION_READY = "AUTOMATION_READY"

AutomationReadinessStatus = Literal["ready", "migration_required", "unavailable"]


@dataclass(frozen=True, slots=True)
class AutomationReadiness:
    status: AutomationReadinessStatus
    code: str
    scheduler_enabled: bool
    project_private_work_ready: bool
    automation_cutover_ready: bool
    request_id: str


class AutomationReadinessService:
    def __init__(
        self,
        revisions: RevisionAncestry = REVISION_ANCESTRY,
    ) -> None:
        self._revisions = revisions

    async def read(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        scheduler_enabled: bool,
    ) -> AutomationReadiness:
        project_ready = False
        automation_ready = False
        try:
            await PrivateWorkCutoverGuard.for_session(
                session,
                request_id=context.request_id,
                revisions=self._revisions,
            ).require_project_open()
            project_ready = True
            await AutomationCutoverGuard.for_session(
                session,
                request_id=context.request_id,
                revisions=self._revisions,
            ).require_project_open()
            automation_ready = True
        except (PrivateWorkUnavailable, AutomationUnavailable):
            return AutomationReadiness(
                status="unavailable",
                code=AutomationUnavailable.code,
                scheduler_enabled=scheduler_enabled,
                project_private_work_ready=False,
                automation_cutover_ready=False,
                request_id=context.request_id,
            )
        except (PrivateWorkCutover, AutomationCutover):
            return AutomationReadiness(
                status="migration_required",
                code=AutomationCutover.code,
                scheduler_enabled=scheduler_enabled,
                project_private_work_ready=project_ready,
                automation_cutover_ready=automation_ready,
                request_id=context.request_id,
            )

        return AutomationReadiness(
            status="ready",
            code=AUTOMATION_READY,
            scheduler_enabled=scheduler_enabled,
            project_private_work_ready=True,
            automation_cutover_ready=True,
            request_id=context.request_id,
        )


__all__ = [
    "AUTOMATION_READY",
    "AutomationReadiness",
    "AutomationReadinessService",
    "AutomationReadinessStatus",
]
