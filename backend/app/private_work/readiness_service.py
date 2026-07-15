from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkCutover, PrivateWorkUnavailable
from deerflow.persistence.private_work.model import PrivateWorkCutoverStateRow

PRIVATE_WORK_READY = "PRIVATE_WORK_READY"

ReadinessStatus = Literal["ready", "migration_required", "unavailable"]


@dataclass(frozen=True)
class PrivateWorkReadiness:
    status: ReadinessStatus
    code: str
    request_id: str


class PrivateWorkReadinessService:
    async def read(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
    ) -> PrivateWorkReadiness:
        try:
            stage = await session.scalar(select(PrivateWorkCutoverStateRow.stage).where(PrivateWorkCutoverStateRow.id == 1))
        except SQLAlchemyError:
            return PrivateWorkReadiness(
                status="unavailable",
                code=PrivateWorkUnavailable.code,
                request_id=context.request_id,
            )

        if stage == "cutover_complete":
            return PrivateWorkReadiness(
                status="ready",
                code=PRIVATE_WORK_READY,
                request_id=context.request_id,
            )
        return PrivateWorkReadiness(
            status="migration_required",
            code=PrivateWorkCutover.code,
            request_id=context.request_id,
        )


__all__ = [
    "PRIVATE_WORK_READY",
    "PrivateWorkReadiness",
    "PrivateWorkReadinessService",
    "ReadinessStatus",
]
