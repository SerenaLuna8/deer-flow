from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.automations.errors import AutomationUnavailable
from app.final_schema import FinalSchemaProbe, FinalSchemaRequired, FinalSchemaUnavailable
from app.private_work.context import PrivateWorkContext

AUTOMATION_READY = "AUTOMATION_READY"

AutomationReadinessStatus = Literal["ready", "unavailable"]
SchedulerReadinessStatus = Literal[
    "disabled",
    "stopped",
    "running",
    "ownership_lost",
]


@dataclass(frozen=True, slots=True)
class AutomationReadiness:
    status: AutomationReadinessStatus
    code: str
    scheduler_enabled: bool
    scheduler_status: SchedulerReadinessStatus
    project_private_work_ready: bool
    schema_ready: bool
    request_id: str


class AutomationReadinessService:
    def __init__(
        self,
        scheduler_status_provider: Callable[[], SchedulerReadinessStatus] | None = None,
    ) -> None:
        self._scheduler_status_provider = scheduler_status_provider

    def _scheduler_status(self, scheduler_enabled: bool) -> SchedulerReadinessStatus:
        if not scheduler_enabled:
            return "disabled"
        if self._scheduler_status_provider is None:
            return "stopped"
        return self._scheduler_status_provider()

    async def read(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        scheduler_enabled: bool,
    ) -> AutomationReadiness:
        scheduler_status = self._scheduler_status(scheduler_enabled)
        try:
            await FinalSchemaProbe().require_ready(session)
        except (FinalSchemaRequired, FinalSchemaUnavailable):
            return AutomationReadiness(
                status="unavailable",
                code=AutomationUnavailable.code,
                scheduler_enabled=scheduler_enabled,
                scheduler_status=scheduler_status,
                project_private_work_ready=False,
                schema_ready=False,
                request_id=context.request_id,
            )

        return AutomationReadiness(
            status="ready",
            code=AUTOMATION_READY,
            scheduler_enabled=scheduler_enabled,
            scheduler_status=scheduler_status,
            project_private_work_ready=True,
            schema_ready=True,
            request_id=context.request_id,
        )


__all__ = [
    "AUTOMATION_READY",
    "AutomationReadiness",
    "AutomationReadinessService",
    "AutomationReadinessStatus",
    "SchedulerReadinessStatus",
]
