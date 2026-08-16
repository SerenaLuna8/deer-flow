"""Platform-admin model catalog settings backed only by PostgreSQL."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, BeforeValidator, ConfigDict
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import AuditAuthorityRejected, SystemAuditContext
from app.final_schema import (
    FinalSchemaProbe,
    FinalSchemaRequired,
    FinalSchemaUnavailable,
)
from app.gateway.deps import (
    get_config,
    get_system_model_catalog,
    get_system_model_materializer,
    project_session,
)
from app.gateway.routers.admin_operations import (
    AdminOperationsRoute,
    authenticated_system_identity,
)
from app.gateway.system_model_callers import ModelConnectionTester
from app.reliability.error_mapping import reliability_http_exception
from app.reliability.errors import (
    ReliabilityDatabaseUnavailable,
    ReliabilityNotFound,
)
from app.reliability.operations import resolve_current_system_audit_context
from app.system_settings import (
    CreateSystemModel,
    SystemModelCatalogService,
    SystemModelCatalogView,
    SystemModelConnectionCheck,
    SystemModelMaterializationUnavailable,
    SystemModelMaterializer,
    SystemModelView,
    UpdateSystemModel,
)
from app.system_settings.errors import SystemModelError
from app.system_settings.validation import BUILTIN_PROVIDER_ADAPTERS
from deerflow.config.app_config import AppConfig

router = APIRouter(
    prefix="/api/admin/settings/models",
    tags=["admin-model-settings"],
    route_class=AdminOperationsRoute,
)


def _parse_json_uuid(value: object) -> object:
    if isinstance(value, uuid.UUID):
        return value
    if type(value) is str:
        try:
            return uuid.UUID(value)
        except ValueError:
            return value
    return value


_JsonUuid = Annotated[uuid.UUID, BeforeValidator(_parse_json_uuid)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AdminModelCreateRequest(_StrictModel):
    display_name: str
    status: Literal["active", "suspended"] = "suspended"
    provider_adapter: str
    provider_model: str
    settings: dict[str, object]
    supports_thinking: bool = False
    supports_reasoning_effort: bool = False
    supports_vision: bool = False
    credential_id: _JsonUuid | None = None
    credential_version_id: _JsonUuid | None = None
    credential_env_key: str | None = None


class AdminModelUpdateRequest(_StrictModel):
    display_name: str
    provider_adapter: str
    provider_model: str
    settings: dict[str, object]
    supports_thinking: bool = False
    supports_reasoning_effort: bool = False
    supports_vision: bool = False
    credential_id: _JsonUuid | None = None
    credential_version_id: _JsonUuid | None = None
    credential_env_key: str | None = None
    expected_revision: int


class AdminModelStatusRequest(_StrictModel):
    status: Literal["active", "suspended"]
    expected_revision: int


class AdminModelDefaultRequest(_StrictModel):
    expected_catalog_revision: int


class AdminModelConnectionTestRequest(_StrictModel):
    provider_adapter: str
    provider_model: str
    settings: dict[str, object]
    supports_vision: bool
    credential_id: _JsonUuid | None = None
    credential_version_id: _JsonUuid | None = None
    credential_env_key: str | None = None


class AdminModelItemResponse(_StrictModel):
    id: uuid.UUID
    display_name: str
    provider_adapter: str
    provider_model: str
    settings: dict[str, object]
    supports_thinking: bool
    supports_reasoning_effort: bool
    supports_vision: bool
    status: Literal["active", "suspended"]
    is_default: bool
    revision: int
    version_number: int
    credential_id: uuid.UUID | None
    credential_version_id: uuid.UUID | None
    credential_env_key: str | None
    updated_at: datetime


class AdminModelProviderSettingFieldResponse(_StrictModel):
    name: str
    label: str
    input_type: Literal[
        "boolean",
        "enum",
        "integer",
        "json",
        "number",
        "string",
        "url",
    ]
    advanced: bool
    minimum: int | float | None
    maximum: int | float | None
    step: int | float | None
    options: list[str]


class AdminModelProviderAdapterResponse(_StrictModel):
    id: str
    credential_required: bool
    setting_fields: list[AdminModelProviderSettingFieldResponse]


class AdminModelCatalogResponse(_StrictModel):
    items: list[AdminModelItemResponse]
    provider_adapters: list[AdminModelProviderAdapterResponse]
    catalog_revision: int
    request_id: str


class AdminModelMutationResponse(_StrictModel):
    item: AdminModelItemResponse
    catalog_revision: int
    request_id: str


class AdminModelConnectionTestResponse(_StrictModel):
    status: Literal["succeeded", "failed"]
    request_id: str


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _item_response(
    item: SystemModelView,
    *,
    default_model_config_id: uuid.UUID | None,
) -> AdminModelItemResponse:
    settings = _thaw_json(item.current_version.settings)
    if not isinstance(settings, dict):
        raise TypeError("model settings must be an object")
    return AdminModelItemResponse(
        id=item.id,
        display_name=item.display_name,
        provider_adapter=item.current_version.provider_adapter,
        provider_model=item.current_version.provider_model,
        settings=settings,
        supports_thinking=item.current_version.supports_thinking,
        supports_reasoning_effort=(item.current_version.supports_reasoning_effort),
        supports_vision=item.current_version.supports_vision,
        status=item.status,
        is_default=item.id == default_model_config_id,
        revision=item.revision,
        version_number=item.current_version.version_number,
        credential_id=item.current_version.credential_id,
        credential_version_id=item.current_version.credential_version_id,
        credential_env_key=item.current_version.credential_env_key,
        updated_at=item.updated_at,
    )


def _catalog_response(
    catalog: SystemModelCatalogView,
    request_id: str,
) -> AdminModelCatalogResponse:
    return AdminModelCatalogResponse(
        items=[
            _item_response(
                item,
                default_model_config_id=catalog.default_model_config_id,
            )
            for item in catalog.items
        ],
        provider_adapters=[
            AdminModelProviderAdapterResponse(
                id=adapter_id,
                credential_required=descriptor.credential_required,
                setting_fields=[
                    AdminModelProviderSettingFieldResponse(
                        name=field.name,
                        label=field.label,
                        input_type=field.input_type,
                        advanced=field.advanced,
                        minimum=field.minimum,
                        maximum=field.maximum,
                        step=field.step,
                        options=list(field.options),
                    )
                    for field in sorted(
                        descriptor.fields,
                        key=lambda item: item.name,
                    )
                ],
            )
            for adapter_id, descriptor in BUILTIN_PROVIDER_ADAPTERS.items()
        ],
        catalog_revision=catalog.catalog_revision,
        request_id=request_id,
    )


async def current_model_admin_context(
    identity: Annotated[
        tuple[uuid.UUID, str],
        Depends(authenticated_system_identity),
    ],
    session: Annotated[AsyncSession, Depends(project_session)],
) -> SystemAuditContext:
    try:
        async with session.begin():
            context = await resolve_current_system_audit_context(
                session,
                identity[0],
                identity[1],
            )
            await FinalSchemaProbe().require_ready(session)
            return context
    except AuditAuthorityRejected:
        raise reliability_http_exception(
            ReliabilityNotFound(identity[1]),
        ) from None
    except (
        DBAPIError,
        FinalSchemaRequired,
        FinalSchemaUnavailable,
        RuntimeError,
    ):
        raise reliability_http_exception(
            ReliabilityDatabaseUnavailable(identity[1]),
        ) from None


def _system_model_http_exception(error: SystemModelError) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail={
            "code": error.code,
            "message": error.public_message,
            "request_id": error.request_id,
        },
    )


async def _catalog_after_mutation(
    service: SystemModelCatalogService,
    context: SystemAuditContext,
) -> SystemModelCatalogView:
    return await service.list_models(context)


@router.get("", response_model=AdminModelCatalogResponse)
async def list_admin_models(
    context: Annotated[
        SystemAuditContext,
        Depends(current_model_admin_context),
    ],
    service: Annotated[
        SystemModelCatalogService,
        Depends(get_system_model_catalog),
    ],
) -> AdminModelCatalogResponse:
    try:
        return _catalog_response(
            await service.list_models(context),
            context.request_id,
        )
    except SystemModelError as error:
        raise _system_model_http_exception(error) from None


@router.post("", response_model=AdminModelMutationResponse)
async def create_admin_model(
    body: AdminModelCreateRequest,
    context: Annotated[
        SystemAuditContext,
        Depends(current_model_admin_context),
    ],
    service: Annotated[
        SystemModelCatalogService,
        Depends(get_system_model_catalog),
    ],
) -> AdminModelMutationResponse:
    try:
        created = await service.create_model(
            context,
            CreateSystemModel(
                display_name=body.display_name,
                status=body.status,
                provider_adapter=body.provider_adapter,
                provider_model=body.provider_model,
                settings=body.settings,
                supports_thinking=body.supports_thinking,
                supports_reasoning_effort=(body.supports_reasoning_effort),
                supports_vision=body.supports_vision,
                credential_id=body.credential_id,
                credential_version_id=body.credential_version_id,
                credential_env_key=body.credential_env_key,
            ),
        )
        catalog = await _catalog_after_mutation(service, context)
        return AdminModelMutationResponse(
            item=_item_response(
                created,
                default_model_config_id=catalog.default_model_config_id,
            ),
            catalog_revision=catalog.catalog_revision,
            request_id=context.request_id,
        )
    except SystemModelError as error:
        raise _system_model_http_exception(error) from None


@router.post(
    "/test-connection",
    response_model=AdminModelConnectionTestResponse,
)
async def test_admin_model_connection(
    body: AdminModelConnectionTestRequest,
    context: Annotated[
        SystemAuditContext,
        Depends(current_model_admin_context),
    ],
    service: Annotated[
        SystemModelCatalogService,
        Depends(get_system_model_catalog),
    ],
    materializer: Annotated[
        SystemModelMaterializer,
        Depends(get_system_model_materializer),
    ],
    config: Annotated[AppConfig, Depends(get_config)],
) -> AdminModelConnectionTestResponse:
    try:
        material = await service.prepare_connection_test(
            context,
            SystemModelConnectionCheck(
                provider_adapter=body.provider_adapter,
                provider_model=body.provider_model,
                settings=body.settings,
                supports_vision=body.supports_vision,
                credential_id=body.credential_id,
                credential_version_id=body.credential_version_id,
                credential_env_key=body.credential_env_key,
            ),
        )
        model = await materializer.materialize_connection_test(material)
    except SystemModelError as error:
        raise _system_model_http_exception(error) from None
    except SystemModelMaterializationUnavailable:
        return AdminModelConnectionTestResponse(
            status="failed",
            request_id=context.request_id,
        )
    return AdminModelConnectionTestResponse(
        status=("succeeded" if await ModelConnectionTester(config).test(model) else "failed"),
        request_id=context.request_id,
    )


@router.put("/{model_config_id}", response_model=AdminModelMutationResponse)
async def update_admin_model(
    model_config_id: uuid.UUID,
    body: AdminModelUpdateRequest,
    context: Annotated[
        SystemAuditContext,
        Depends(current_model_admin_context),
    ],
    service: Annotated[
        SystemModelCatalogService,
        Depends(get_system_model_catalog),
    ],
) -> AdminModelMutationResponse:
    try:
        updated = await service.update_model(
            context,
            model_config_id,
            UpdateSystemModel(
                display_name=body.display_name,
                provider_adapter=body.provider_adapter,
                provider_model=body.provider_model,
                settings=body.settings,
                supports_thinking=body.supports_thinking,
                supports_reasoning_effort=(body.supports_reasoning_effort),
                supports_vision=body.supports_vision,
                credential_id=body.credential_id,
                credential_version_id=body.credential_version_id,
                credential_env_key=body.credential_env_key,
            ),
            expected_revision=body.expected_revision,
        )
        catalog = await _catalog_after_mutation(service, context)
        return AdminModelMutationResponse(
            item=_item_response(
                updated,
                default_model_config_id=catalog.default_model_config_id,
            ),
            catalog_revision=catalog.catalog_revision,
            request_id=context.request_id,
        )
    except SystemModelError as error:
        raise _system_model_http_exception(error) from None


@router.post(
    "/{model_config_id}/status",
    response_model=AdminModelMutationResponse,
)
async def set_admin_model_status(
    model_config_id: uuid.UUID,
    body: AdminModelStatusRequest,
    context: Annotated[
        SystemAuditContext,
        Depends(current_model_admin_context),
    ],
    service: Annotated[
        SystemModelCatalogService,
        Depends(get_system_model_catalog),
    ],
) -> AdminModelMutationResponse:
    try:
        updated = await service.set_status(
            context,
            model_config_id,
            body.status,
            expected_revision=body.expected_revision,
        )
        catalog = await _catalog_after_mutation(service, context)
        return AdminModelMutationResponse(
            item=_item_response(
                updated,
                default_model_config_id=catalog.default_model_config_id,
            ),
            catalog_revision=catalog.catalog_revision,
            request_id=context.request_id,
        )
    except SystemModelError as error:
        raise _system_model_http_exception(error) from None


@router.post(
    "/{model_config_id}/default",
    response_model=AdminModelMutationResponse,
)
async def set_admin_model_default(
    model_config_id: uuid.UUID,
    body: AdminModelDefaultRequest,
    context: Annotated[
        SystemAuditContext,
        Depends(current_model_admin_context),
    ],
    service: Annotated[
        SystemModelCatalogService,
        Depends(get_system_model_catalog),
    ],
) -> AdminModelMutationResponse:
    try:
        await service.set_default(
            context,
            model_config_id,
            expected_catalog_revision=body.expected_catalog_revision,
        )
        catalog = await _catalog_after_mutation(service, context)
        selected = next(
            (item for item in catalog.items if item.id == model_config_id),
            None,
        )
        if selected is None:
            raise TypeError("default model disappeared")
        return AdminModelMutationResponse(
            item=_item_response(
                selected,
                default_model_config_id=catalog.default_model_config_id,
            ),
            catalog_revision=catalog.catalog_revision,
            request_id=context.request_id,
        )
    except SystemModelError as error:
        raise _system_model_http_exception(error) from None


__all__ = [
    "AdminModelCatalogResponse",
    "AdminModelConnectionTestRequest",
    "AdminModelConnectionTestResponse",
    "AdminModelCreateRequest",
    "AdminModelItemResponse",
    "AdminModelProviderAdapterResponse",
    "AdminModelProviderSettingFieldResponse",
    "AdminModelMutationResponse",
    "AdminModelUpdateRequest",
    "current_model_admin_context",
    "router",
]
