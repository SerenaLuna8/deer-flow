from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.final_schema import FinalSchemaProbe, FinalSchemaRequired, FinalSchemaUnavailable
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkUnavailable

PRIVATE_WORK_READY = "PRIVATE_WORK_READY"

ReadinessStatus = Literal["ready", "unavailable"]


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
            await FinalSchemaProbe().require_ready(session)
        except (FinalSchemaRequired, FinalSchemaUnavailable):
            return PrivateWorkReadiness(
                status="unavailable",
                code=PrivateWorkUnavailable.code,
                request_id=context.request_id,
            )

        return PrivateWorkReadiness(
            status="ready",
            code=PRIVATE_WORK_READY,
            request_id=context.request_id,
        )


__all__ = [
    "PRIVATE_WORK_READY",
    "PrivateWorkReadiness",
    "PrivateWorkReadinessService",
    "ReadinessStatus",
]
