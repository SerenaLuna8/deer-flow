from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import AuditPage, AuditRecord
from app.gateway.deps import get_project_audit_service, project_session
from app.gateway.routers.admin_operations import (
    AdminOperationsRoute,
    authenticated_system_identity,
    current_system_context,
    map_admin_operations_errors,
)

router = APIRouter(
    prefix="/api/admin/audit",
    tags=["admin-audit"],
    route_class=AdminOperationsRoute,
)


class AdminAuditItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: uuid.UUID
    occurred_at: str
    actor: str
    action: str
    target_kind: str
    outcome: str
    public_error_code: str | None
    metadata: dict[str, object]


class AdminAuditPageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    items: list[AdminAuditItemResponse]
    next_cursor: str | None


def _actor(record: AuditRecord) -> str:
    if record.actor_process is not None:
        return record.actor_process.value
    if record.actor_platform_role is not None:
        return record.actor_platform_role.value
    return "user"


def _response(page: AuditPage) -> AdminAuditPageResponse:
    return AdminAuditPageResponse(
        items=[
            AdminAuditItemResponse(
                id=item.id,
                occurred_at=item.occurred_at.isoformat(),
                actor=_actor(item),
                action=item.action.value,
                target_kind=item.target_kind.value,
                outcome=item.outcome.value,
                public_error_code=item.public_error_code,
                metadata=item.metadata,
            )
            for item in page.items
        ],
        next_cursor=page.next_cursor,
    )


@router.get("", response_model=AdminAuditPageResponse)
@map_admin_operations_errors
async def list_admin_audit(
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
    identity: tuple[uuid.UUID, str] = Depends(authenticated_system_identity),
    session: AsyncSession = Depends(project_session),
    audit=Depends(get_project_audit_service),
) -> AdminAuditPageResponse:
    async with session.begin():
        context = await current_system_context(session, identity)
        return _response(
            await audit.list_platform(
                session,
                context,
                limit=limit,
                cursor=cursor,
            )
        )


__all__ = ["router"]
