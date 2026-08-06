"""Authenticated account personalization API."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.gateway.deps import get_current_user_from_request
from app.personalization.service import (
    AccountMemoryResetResult,
    AccountPersonalizationConflict,
    AccountPersonalizationNotFound,
    AccountPersonalizationService,
    AccountPersonalizationUnavailable,
    AccountPersonalizationView,
)
from deerflow.persistence.engine import get_session_factory

router = APIRouter(
    prefix="/api/v1/account/personalization",
    tags=["account-personalization"],
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class AccountPersonalizationResponse(_StrictModel):
    memory_enabled: bool = Field(alias="memoryEnabled")
    effective_memory_enabled: bool = Field(alias="effectiveMemoryEnabled")
    platform_memory_available: bool = Field(alias="platformMemoryAvailable")
    version: int = Field(ge=1)


class UpdateAccountPersonalizationRequest(_StrictModel):
    memory_enabled: bool = Field(alias="memoryEnabled", strict=True)
    expected_version: int = Field(alias="expectedVersion", ge=1, strict=True)


class ResetAccountMemoryRequest(_StrictModel):
    confirm: Literal[True]
    expected_version: int = Field(alias="expectedVersion", ge=1, strict=True)


class ResetAccountMemoryResponse(_StrictModel):
    version: int = Field(ge=1)
    scopes_reset: int = Field(alias="scopesReset", ge=0)
    history_entries: int = Field(alias="historyEntries", ge=0)
    documents: int = Field(ge=0)
    versions: int = Field(ge=0)
    dream_runs: int = Field(alias="dreamRuns", ge=0)
    snapshots: int = Field(ge=0)
    jobs_cancelled: int = Field(alias="jobsCancelled", ge=0)


def _service(request: Request):
    service = getattr(request.app.state, "account_personalization_service", None)
    if service is None:
        service = AccountPersonalizationService(get_session_factory())
        request.app.state.account_personalization_service = service
    return service


def _user_id(user) -> uuid.UUID:
    try:
        return uuid.UUID(str(user.id))
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(
            status_code=401,
            detail={"code": "NOT_AUTHENTICATED", "message": "Not authenticated"},
        ) from None


def _raise_error(error: Exception) -> None:
    if isinstance(error, AccountPersonalizationConflict):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PERSONALIZATION_CONFLICT",
                "message": "Personalization changed; reload and try again",
            },
        ) from None
    if isinstance(error, AccountPersonalizationNotFound):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "ACCOUNT_NOT_FOUND",
                "message": "Account not found",
            },
        ) from None
    if isinstance(error, AccountPersonalizationUnavailable):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "PERSONALIZATION_UNAVAILABLE",
                "message": "Personalization is temporarily unavailable",
            },
            headers={"Retry-After": "1"},
        ) from None
    raise error


def _response(view: AccountPersonalizationView) -> AccountPersonalizationResponse:
    return AccountPersonalizationResponse.model_validate(view, from_attributes=True)


@router.get("", response_model=AccountPersonalizationResponse)
async def get_account_personalization(
    request: Request,
    response: Response,
    user=Depends(get_current_user_from_request),
) -> AccountPersonalizationResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        return _response(await _service(request).get(_user_id(user)))
    except Exception as error:
        _raise_error(error)


@router.patch("", response_model=AccountPersonalizationResponse)
async def update_account_personalization(
    request: Request,
    body: UpdateAccountPersonalizationRequest,
    response: Response,
    user=Depends(get_current_user_from_request),
) -> AccountPersonalizationResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        return _response(
            await _service(request).update_memory(
                _user_id(user),
                memory_enabled=body.memory_enabled,
                expected_version=body.expected_version,
            )
        )
    except Exception as error:
        _raise_error(error)


@router.post(
    "/memory/reset",
    response_model=ResetAccountMemoryResponse,
)
async def reset_account_memory(
    request: Request,
    body: ResetAccountMemoryRequest,
    response: Response,
    user=Depends(get_current_user_from_request),
) -> ResetAccountMemoryResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        result: AccountMemoryResetResult = await _service(request).reset_memory(
            _user_id(user),
            expected_version=body.expected_version,
        )
        return ResetAccountMemoryResponse.model_validate(result, from_attributes=True)
    except Exception as error:
        _raise_error(error)


__all__ = ["router"]
