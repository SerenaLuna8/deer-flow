from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from app.gateway.auth.invitation_claim import (
    INVITATION_CLAIM_COOKIE_NAME,
    INVITATION_CLAIM_COOKIE_PATH,
    INVITATION_CLAIM_MAX_AGE,
    InvitationClaimSigner,
)
from app.gateway.auth.invitation_rate_limit import (
    InvitationRateLimitRepository,
    hash_rate_limit_key,
)
from app.gateway.csrf_middleware import is_allowed_auth_origin, is_secure_request
from app.gateway.deps import (
    get_current_user_from_request,
    get_operational_audit_sink,
    get_project_quota_enforcer,
    project_session,
)
from app.gateway.routers.auth import _get_client_ip
from app.gateway.routers.project_governance import (
    GOVERNANCE_DOMAIN_ERRORS,
    GovernanceRoute,
    governance_error,
    raise_governance_error,
)
from app.gateway.routers.projects import authenticated_project_identity
from app.projects.context import resolve_project_context
from app.projects.errors import ProjectDatabaseUnavailable, ProjectValidationFailed
from app.projects.invitation_models import (
    CreatedInvitation,
    InvitationClaim,
    InvitationView,
    ProjectInvitationConflict,
    ProjectInvitationInvalid,
    RedeemedInvitation,
)
from app.projects.invitation_repository import InvitationRepository
from app.projects.invitation_service import (
    InvitationService,
    hash_invitation_token,
)
from app.projects.models import ProjectRole
from deerflow.trace_context import generate_trace_id, get_current_trace_id

router = APIRouter(tags=["project-invitations"], route_class=GovernanceRoute)


class InvitationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str
    role: ProjectRole


class InvitationVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)


class InvitationClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str


class InvitationResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    invited_email: str
    role: ProjectRole
    status: str
    expires_at: datetime
    version: int
    created_at: datetime


class CreatedInvitationResponse(InvitationResponse):
    invite_url_fragment: str


class RedeemedInvitationResponse(BaseModel):
    invitation_id: uuid.UUID
    project_id: uuid.UUID
    project_slug: str
    membership_id: uuid.UUID
    role: ProjectRole


class InvitationClaimResponse(BaseModel):
    message: str = "Invitation claim processed"


def _response(view: InvitationView) -> InvitationResponse:
    return InvitationResponse(**vars(view))


def _created_response(created: CreatedInvitation) -> CreatedInvitationResponse:
    return CreatedInvitationResponse(
        **vars(created.invitation),
        invite_url_fragment=f"/invite#token={created.token}",
    )


def claim_signer() -> InvitationClaimSigner:
    return InvitationClaimSigner()


async def authenticated_invitation_identity(
    user=Depends(get_current_user_from_request),
) -> tuple[uuid.UUID, str, str]:
    return (
        uuid.UUID(str(user.id)),
        str(user.email),
        get_current_trace_id() or generate_trace_id(),
    )


def _claim_key(request: Request) -> str:
    return hash_rate_limit_key(f"claim\x00{_get_client_ip(request)}")


def _redeem_key(request: Request, user_email: str) -> str:
    normalized_email = user_email.strip().lower()
    return hash_rate_limit_key(f"redeem\x00{_get_client_ip(request)}\x00{normalized_email}")


def _set_claim_cookie(
    response: Response,
    request: Request,
    signed_claim: str,
) -> None:
    response.set_cookie(
        key=INVITATION_CLAIM_COOKIE_NAME,
        value=signed_claim,
        max_age=INVITATION_CLAIM_MAX_AGE,
        httponly=True,
        secure=is_secure_request(request),
        samesite="lax",
        path=INVITATION_CLAIM_COOKIE_PATH,
    )


def _clear_claim_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        key=INVITATION_CLAIM_COOKIE_NAME,
        secure=is_secure_request(request),
        samesite="lax",
        path=INVITATION_CLAIM_COOKIE_PATH,
    )


def _redeem_error_response(
    exc: Exception,
    request_id: str,
    request: Request,
) -> JSONResponse:
    status_code, detail = governance_error(exc, request_id)
    response = JSONResponse(
        status_code=status_code,
        content={"detail": detail},
        headers={"Retry-After": "1"} if status_code == 429 else None,
    )
    _clear_claim_cookie(response, request)
    return response


@router.get(
    "/api/project-invitations/mine",
    response_model=list[InvitationResponse],
)
async def list_my_invitations(
    identity: tuple[uuid.UUID, str, str] = Depends(authenticated_invitation_identity),
    session: AsyncSession = Depends(project_session),
):
    _, user_email, request_id = identity
    try:
        views = await InvitationService(InvitationRepository(session)).list_mine(
            user_email,
            datetime.now(UTC),
        )
        return [_response(view) for view in views]
    except GOVERNANCE_DOMAIN_ERRORS as exc:
        raise_governance_error(exc, request_id)


@router.get(
    "/api/projects/{project_id}/invitations",
    response_model=list[InvitationResponse],
)
async def list_project_invitations(
    project_id: uuid.UUID,
    identity: tuple[uuid.UUID, str] = Depends(authenticated_project_identity),
    session: AsyncSession = Depends(project_session),
):
    try:
        context = await resolve_project_context(
            session,
            identity[0],
            project_id,
            identity[1],
        )
        views = await InvitationService(InvitationRepository(session)).list_for_project(context)
        return [_response(view) for view in views]
    except GOVERNANCE_DOMAIN_ERRORS as exc:
        raise_governance_error(exc, identity[1])


@router.post(
    "/api/projects/{project_id}/invitations",
    response_model=CreatedInvitationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_invitation(
    project_id: uuid.UUID,
    body: InvitationCreateRequest,
    identity: tuple[uuid.UUID, str] = Depends(authenticated_project_identity),
    session: AsyncSession = Depends(project_session),
    audit=Depends(get_operational_audit_sink),
):
    try:
        context = await resolve_project_context(
            session,
            identity[0],
            project_id,
            identity[1],
        )
        created = await InvitationService(
            InvitationRepository(session),
            audit=audit,
        ).create(
            context,
            body.email,
            body.role,
            datetime.now(UTC),
        )
        return _created_response(created)
    except GOVERNANCE_DOMAIN_ERRORS as exc:
        raise_governance_error(exc, identity[1])


@router.delete(
    "/api/projects/{project_id}/invitations/{invitation_id}",
    response_model=InvitationResponse,
)
async def revoke_invitation(
    project_id: uuid.UUID,
    invitation_id: uuid.UUID,
    body: InvitationVersionRequest,
    identity: tuple[uuid.UUID, str] = Depends(authenticated_project_identity),
    session: AsyncSession = Depends(project_session),
    audit=Depends(get_operational_audit_sink),
):
    try:
        context = await resolve_project_context(
            session,
            identity[0],
            project_id,
            identity[1],
        )
        view = await InvitationService(
            InvitationRepository(session),
            audit=audit,
        ).revoke(
            context,
            invitation_id,
            body.version,
            datetime.now(UTC),
        )
        return _response(view)
    except GOVERNANCE_DOMAIN_ERRORS as exc:
        raise_governance_error(exc, identity[1])


@router.post(
    "/api/project-invitations/claim",
    response_model=InvitationClaimResponse,
)
async def claim_invitation(
    request: Request,
    response: Response,
    body: InvitationClaimRequest,
    session: AsyncSession = Depends(project_session),
):
    request_id = get_current_trace_id() or generate_trace_id()
    if not is_allowed_auth_origin(request):
        status_code, detail = governance_error(
            ProjectValidationFailed("invalid_invitation_claim_origin"),
            request_id,
        )
        return JSONResponse(status_code=status_code, content={"detail": detail})
    rate_limit = InvitationRateLimitRepository(session)
    key_hash = _claim_key(request)
    now = datetime.now(UTC)
    try:
        admitted = await rate_limit.admit_attempt(key_hash)
        invitation_claim: InvitationClaim | None = None
        if admitted:
            try:
                invitation_claim = await InvitationService(InvitationRepository(session)).claim(body.token, now)
            except (ProjectInvitationInvalid, ProjectValidationFailed):
                pass
            else:
                await rate_limit.clear(key_hash)
        if invitation_claim is None:
            invitation_claim = InvitationClaim(
                invitation_id=uuid.uuid4(),
                token_hash=hash_invitation_token(body.token),
            )
        signed = claim_signer().issue(invitation_claim, datetime.now(UTC))
    except ProjectDatabaseUnavailable as exc:
        raise_governance_error(exc, request_id)
    _set_claim_cookie(response, request, signed)
    return InvitationClaimResponse()


@router.post(
    "/api/project-invitations/redeem",
    response_model=RedeemedInvitationResponse,
)
async def redeem_invitation(
    request: Request,
    identity: tuple[uuid.UUID, str, str] = Depends(authenticated_invitation_identity),
    session: AsyncSession = Depends(project_session),
    quota=Depends(get_project_quota_enforcer),
    audit=Depends(get_operational_audit_sink),
):
    user_id, user_email, request_id = identity
    rate_limit = InvitationRateLimitRepository(session)
    key_hash = _redeem_key(request, user_email)
    now = datetime.now(UTC)
    try:
        if not await rate_limit.admit_attempt(key_hash):
            return _redeem_error_response(
                ProjectInvitationInvalid(),
                request_id,
                request,
            )
        signed = request.cookies.get(INVITATION_CLAIM_COOKIE_NAME)
        if not signed:
            raise ProjectInvitationInvalid()
        invitation_claim = claim_signer().verify(signed, now)
        redeemed: RedeemedInvitation = await InvitationService(
            InvitationRepository(session),
            quota=quota,
            audit=audit,
        ).redeem(
            user_id,
            user_email,
            invitation_claim,
            now,
            request_id=request_id,
        )
    except (ProjectInvitationInvalid, ProjectInvitationConflict, ProjectValidationFailed) as exc:
        return _redeem_error_response(exc, request_id, request)
    except GOVERNANCE_DOMAIN_ERRORS as exc:
        return _redeem_error_response(exc, request_id, request)

    try:
        await rate_limit.clear(key_hash)
    except ProjectDatabaseUnavailable as exc:
        return _redeem_error_response(exc, request_id, request)
    payload = RedeemedInvitationResponse(**vars(redeemed)).model_dump(mode="json")
    response = JSONResponse(status_code=200, content=payload)
    _clear_claim_cookie(response, request)
    return response
