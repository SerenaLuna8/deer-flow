"""Knowledge configuration stays administrable even while its feature is off."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import DBAPIError

from app.audit.models import SystemAuditContext
from app.audit.service import AuditService
from app.gateway.deps import get_project_audit_service
from app.gateway.routers.admin_model_settings import current_model_admin_context
from app.gateway.routers.admin_operations import AdminOperationsRoute
from app.knowledge_settings.models import AdminKnowledgeSettingsResponse, AdminKnowledgeSettingsUpdateRequest
from app.knowledge_settings.service import (
    KnowledgeSettingsError,
    knowledge_settings_response,
    read_knowledge_system_settings,
    require_settings_admin,
    update_knowledge_system_settings,
)
from deerflow.persistence.engine import get_session_factory
from deerflow.secrets import SecretKey, SecretKeyInvalid

router = APIRouter(prefix="/api/admin/settings/knowledge", tags=["admin-knowledge-settings"], route_class=AdminOperationsRoute)


def _http_error(error: KnowledgeSettingsError, context: SystemAuditContext) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail={"code": error.code, "message": error.public_message, "request_id": context.request_id})


def _factory(request: Request):
    return getattr(request.app.state, "admin_operations_session_factory", None) or get_session_factory()


@router.get("", response_model=AdminKnowledgeSettingsResponse)
async def get_settings(
    request: Request,
    context: Annotated[SystemAuditContext, Depends(current_model_admin_context)],
) -> AdminKnowledgeSettingsResponse:
    try:
        async with _factory(request)() as session, session.begin():
            await require_settings_admin(session, context)
            return await knowledge_settings_response(session, await read_knowledge_system_settings(session), request_id=context.request_id)
    except KnowledgeSettingsError as error:
        raise _http_error(error, context) from None
    except DBAPIError:
        raise _http_error(KnowledgeSettingsError("KNOWLEDGE_SETTINGS_UNAVAILABLE", 503), context) from None


@router.put("", response_model=AdminKnowledgeSettingsResponse)
async def put_settings(
    request: Request,
    body: AdminKnowledgeSettingsUpdateRequest,
    context: Annotated[SystemAuditContext, Depends(current_model_admin_context)],
    audit_service: Annotated[AuditService, Depends(get_project_audit_service)],
) -> AdminKnowledgeSettingsResponse:
    try:
        factory = _factory(request)
        row = await update_knowledge_system_settings(factory, actor=context, request=body, secret_key=SecretKey.from_environment(), audit_service=audit_service)
        async with factory() as session, session.begin():
            await require_settings_admin(session, context)
            return await knowledge_settings_response(session, row, request_id=context.request_id)
    except KnowledgeSettingsError as error:
        raise _http_error(error, context) from None
    except (DBAPIError, SecretKeyInvalid):
        raise _http_error(KnowledgeSettingsError("KNOWLEDGE_SETTINGS_UNAVAILABLE", 503), context) from None
