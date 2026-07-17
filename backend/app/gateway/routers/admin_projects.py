from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.deps import project_session
from app.gateway.routers.admin_operations import (
    AdminOperationsRoute,
    authenticated_system_identity,
    current_system_context,
    map_admin_operations_errors,
)
from app.reliability.operations import SystemOperationsRepository

router = APIRouter(
    prefix="/api/admin/projects",
    tags=["admin-projects"],
    route_class=AdminOperationsRoute,
)


class AdminProjectResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    project_id: uuid.UUID
    status: Literal["active", "pending_deletion"]
    is_suspended: bool


class AdminProjectPageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    items: list[AdminProjectResponse]
    next_cursor: str | None


@router.get("", response_model=AdminProjectPageResponse)
@map_admin_operations_errors
async def list_admin_projects(
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
    status: Literal["active", "pending_deletion"] | None = None,
    suspended: bool | None = None,
    identity: tuple[uuid.UUID, str] = Depends(authenticated_system_identity),
    session: AsyncSession = Depends(project_session),
) -> AdminProjectPageResponse:
    async with session.begin():
        await current_system_context(session, identity)
        page = await SystemOperationsRepository(session).list_projects(
            limit=limit,
            cursor=cursor,
            status=status,
            suspended=suspended,
        )
        return AdminProjectPageResponse(
            items=[
                AdminProjectResponse(
                    project_id=item.project_id,
                    status=item.status,
                    is_suspended=item.is_suspended,
                )
                for item in page.items
            ],
            next_cursor=page.next_cursor,
        )


__all__ = ["router"]
