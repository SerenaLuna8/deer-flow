from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.deps import get_current_user_from_request, project_session
from app.private_work.privacy_center import (
    PrivacyCaseNotFound,
    PrivacyCenterService,
)

router = APIRouter(prefix="/api/privacy", tags=["privacy"])


class PrivacyCaseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)
    project_id: uuid.UUID
    project_slug: str
    project_display_name: str
    project_icon: str
    membership_status: str
    retention_kind: str
    deletion_deadline: datetime
    early_delete_requested: bool


class PrivacyEarlyDeleteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)
    project_id: uuid.UUID
    job_id: uuid.UUID
    status: str


def _user_id(user) -> uuid.UUID:
    try:
        return uuid.UUID(str(user.id))
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(
            status_code=401,
            detail={"code": "NOT_AUTHENTICATED", "message": "Not authenticated"},
        ) from None


def _raise_error(error: Exception) -> None:
    if isinstance(error, PrivacyCaseNotFound):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "PRIVACY_CASE_NOT_FOUND",
                "message": "Privacy retention case not found",
            },
        ) from None
    if isinstance(error, SQLAlchemyError):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "DATABASE_UNAVAILABLE",
                "message": "Privacy storage unavailable",
            },
            headers={"Retry-After": "1"},
        ) from None
    raise error


@router.get("/cases", response_model=list[PrivacyCaseResponse])
async def list_privacy_cases(
    response: Response,
    user=Depends(get_current_user_from_request),
    session: AsyncSession = Depends(project_session),
):
    try:
        response.headers["Cache-Control"] = "no-store"
        return await PrivacyCenterService(session).list_cases(
            _user_id(user),
            now=datetime.now(UTC),
        )
    except (PrivacyCaseNotFound, SQLAlchemyError) as error:
        _raise_error(error)


@router.get("/cases/{project_id}/export")
async def export_privacy_case(
    project_id: uuid.UUID,
    user=Depends(get_current_user_from_request),
    session: AsyncSession = Depends(project_session),
):
    try:
        stream = await PrivacyCenterService(session).open_case_export(
            _user_id(user),
            project_id,
            now=datetime.now(UTC),
        )
        return StreamingResponse(
            stream,
            media_type="application/x-ndjson",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": (f'attachment; filename="act-weave-privacy-{project_id}.ndjson"'),
                "X-Content-Type-Options": "nosniff",
            },
        )
    except (PrivacyCaseNotFound, SQLAlchemyError) as error:
        _raise_error(error)


@router.post(
    "/cases/{project_id}/early-delete",
    response_model=PrivacyEarlyDeleteResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_privacy_early_delete(
    project_id: uuid.UUID,
    response: Response,
    user=Depends(get_current_user_from_request),
    session: AsyncSession = Depends(project_session),
):
    try:
        response.headers["Cache-Control"] = "no-store"
        return await PrivacyCenterService(session).request_early_delete(
            _user_id(user),
            project_id,
            now=datetime.now(UTC),
        )
    except (PrivacyCaseNotFound, SQLAlchemyError) as error:
        _raise_error(error)


__all__ = ["router"]
