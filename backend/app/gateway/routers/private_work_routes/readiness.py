from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.deps import private_work_context, project_session
from app.gateway.private_work_schemas import PrivateWorkRoute
from app.gateway.routers.private_work_routes.contracts import PrivateWorkReadinessResponse
from app.private_work.context import PrivateWorkContext
from app.private_work.readiness_service import PrivateWorkReadinessService

router = APIRouter(route_class=PrivateWorkRoute)


@router.get("/readiness", response_model=PrivateWorkReadinessResponse)
async def get_private_work_readiness(
    context: PrivateWorkContext = Depends(private_work_context),
    session: AsyncSession = Depends(project_session),
) -> PrivateWorkReadinessResponse:
    result = await PrivateWorkReadinessService().read(session, context)
    return PrivateWorkReadinessResponse(
        status=result.status,
        code=result.code,
        request_id=result.request_id,
    )
