"""Admin HTTP surface for the retrieval model registry.

Routes live under ``/api/admin/settings`` beside the system model settings and
reuse the same platform system-admin gate (non-admins receive 404). The
registry stays administrable while the Knowledge feature is disabled: the
service borrows the Gateway-owned lifespan probe client and the package-level
``retrieval_model_in_use`` reference query, so no ``KnowledgeModule`` is
required. Errors keep the ``KNOWLEDGE_*`` body shape.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from actweave_knowledge import (
    KNOWLEDGE_STORAGE_UNAVAILABLE,
    KnowledgeError,
    retrieval_model_in_use,
)
from actweave_knowledge.models import KnowledgeModelClient
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import AuditAuthorityRejected, SystemAuditContext
from app.audit.service import AuditService
from app.final_schema import (
    FinalSchemaProbe,
    FinalSchemaRequired,
    FinalSchemaUnavailable,
)
from app.gateway.deps import (
    get_config,
    get_model_registry_probe_client,
    get_project_audit_service,
    get_system_model_catalog,
    get_system_model_materializer,
    project_session,
)
from app.gateway.routers.admin_model_settings import system_model_http_exception
from app.gateway.routers.admin_operations import (
    AdminOperationsRoute,
    authenticated_system_identity,
)
from app.gateway.system_model_callers import ModelConnectionTester
from app.knowledge.gateway import knowledge_http_exception
from app.model_registry.service import (
    ModelProviderView,
    ModelRegistryService,
    ProviderModelView,
)
from app.reliability.error_mapping import reliability_http_exception
from app.reliability.errors import (
    ReliabilityDatabaseUnavailable,
    ReliabilityNotFound,
)
from app.reliability.operations import resolve_current_system_audit_context
from app.system_settings import (
    SystemModelCatalogService,
    SystemModelMaterializationUnavailable,
    SystemModelMaterializer,
    TestProviderCandidateConnection,
)
from app.system_settings.errors import SystemModelError
from deerflow.config.app_config import AppConfig
from deerflow.secrets import SecretKey, SecretKeyInvalid
from deerflow.trace_context import generate_trace_id, get_current_trace_id, normalize_trace_id

router = APIRouter(
    prefix="/api/admin/settings",
    tags=["admin-model-registry"],
    route_class=AdminOperationsRoute,
)

_DEFAULT_EMBEDDING_MAX_BATCH = 64
_DEFAULT_RERANK_MAX_BATCH = 32


def _request_id(request: Request) -> str:
    return get_current_trace_id() or normalize_trace_id(request.headers.get("x-trace-id")) or generate_trace_id()


async def require_model_registry_admin_context(
    identity: Annotated[
        tuple[uuid.UUID, str],
        Depends(authenticated_system_identity),
    ],
    session: Annotated[AsyncSession, Depends(project_session)],
) -> SystemAuditContext:
    """Authorize a platform system admin exactly like other admin settings."""

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
        raise reliability_http_exception(ReliabilityNotFound(identity[1])) from None
    except (DBAPIError, FinalSchemaRequired, FinalSchemaUnavailable, RuntimeError):
        raise reliability_http_exception(ReliabilityDatabaseUnavailable(identity[1])) from None


def get_model_registry_service(
    request: Request,
    # The Gateway lifespan owns this probe client; requests only borrow it.
    # No KnowledgeModule is involved, so the admin surface stays available
    # while the Knowledge feature is disabled.
    probe_client: Annotated[
        KnowledgeModelClient,
        Depends(get_model_registry_probe_client),
    ],
    # Registry writes are governed asset changes: resolve the audit service
    # through the shared fail-closed dependency (503 when unset) instead of
    # assembling a service that would silently drop the trail.
    audit_service: Annotated[AuditService, Depends(get_project_audit_service)],
) -> ModelRegistryService:
    """Assemble the registry service on Gateway-owned host resources."""

    from deerflow.persistence.engine import get_session_factory

    try:
        session_factory = get_session_factory()
        secret_key = SecretKey.from_environment()
    except (RuntimeError, SecretKeyInvalid):
        raise knowledge_http_exception(
            KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "模型注册表存储暂时不可用"),
            _request_id(request),
        ) from None
    return ModelRegistryService(
        session_factory,
        secret_key=secret_key,
        client=probe_client,
        model_in_use=retrieval_model_in_use,
        audit_service=audit_service,
    )


class _StrictModel(BaseModel):
    # ``model_type``/``model_name`` are registry field names, not pydantic's.
    model_config = ConfigDict(extra="forbid", strict=True, protected_namespaces=())


class ModelProviderCreateRequest(_StrictModel):
    name: str
    base_url: str
    request_timeout_seconds: int = 30
    api_key: SecretStr

    @field_validator("api_key")
    @classmethod
    def require_non_empty_api_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value():
            raise ValueError("api_key must not be empty")
        return value


class ModelProviderUpdateRequest(_StrictModel):
    name: str | None = None
    base_url: str | None = None
    request_timeout_seconds: int | None = None
    api_key: SecretStr | None = None

    @field_validator("api_key")
    @classmethod
    def require_non_empty_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value():
            raise ValueError("api_key must not be empty")
        return value


class ModelProviderConnectionTestRequest(_StrictModel):
    """Candidate URL/Key test against one explicit text-model target.

    Requires no existing Provider row and creates or modifies nothing; the
    Key is transient request material and never enters stored configuration.
    """

    base_url: str
    api_key: SecretStr
    provider_adapter: str
    provider_model: str
    max_input_tokens: int = Field(ge=1, le=2_000_000)
    settings: dict[str, object]
    supports_vision: bool = False

    @field_validator("api_key")
    @classmethod
    def require_non_empty_api_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value():
            raise ValueError("api_key must not be empty")
        return value


class ModelProviderConnectionTestResponse(_StrictModel):
    status: Literal["succeeded", "failed"]
    request_id: str


class ProviderModelCreateRequest(_StrictModel):
    model_type: Literal["embedding", "rerank"]
    model_name: str
    embedding_dimension: int | None = None
    max_batch: int | None = None


class ProviderModelStatusRequest(_StrictModel):
    status: Literal["active", "disabled"]


class ModelProviderItemResponse(_StrictModel):
    id: uuid.UUID
    name: str
    base_url: str
    request_timeout_seconds: int
    api_key_configured: bool
    model_count: int
    active_model_count: int
    endpoint_frozen: bool
    created_at: datetime
    updated_at: datetime


class ModelProviderListResponse(_StrictModel):
    items: list[ModelProviderItemResponse]
    request_id: str


class ModelProviderMutationResponse(_StrictModel):
    item: ModelProviderItemResponse
    request_id: str


class ModelProviderDeleteResponse(_StrictModel):
    request_id: str


class ProviderModelItemResponse(_StrictModel):
    id: uuid.UUID
    provider_id: uuid.UUID
    model_type: Literal["embedding", "rerank"]
    model_name: str
    embedding_dimension: int | None
    max_batch: int
    status: Literal["active", "disabled"]
    in_use: bool
    created_at: datetime
    updated_at: datetime


class ProviderModelListResponse(_StrictModel):
    items: list[ProviderModelItemResponse]
    request_id: str


class ProviderModelMutationResponse(_StrictModel):
    item: ProviderModelItemResponse
    request_id: str


class ProviderModelDeleteResponse(_StrictModel):
    request_id: str


class ProviderModelTestResponse(_StrictModel):
    ok: bool
    message: str
    request_id: str


def _provider_response(view: ModelProviderView) -> ModelProviderItemResponse:
    return ModelProviderItemResponse(
        id=view.id,
        name=view.name,
        base_url=view.base_url,
        request_timeout_seconds=view.request_timeout_seconds,
        api_key_configured=view.api_key_configured,
        model_count=view.model_count,
        active_model_count=view.active_model_count,
        endpoint_frozen=view.endpoint_frozen,
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


def _model_response(view: ProviderModelView) -> ProviderModelItemResponse:
    return ProviderModelItemResponse(
        id=view.id,
        provider_id=view.provider_id,
        model_type=view.model_type,  # type: ignore[arg-type]  # CHECK-constrained values
        model_name=view.model_name,
        embedding_dimension=view.embedding_dimension,
        max_batch=view.max_batch,
        status=view.status,  # type: ignore[arg-type]  # CHECK-constrained values
        in_use=view.in_use,
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


@router.get("/model-providers", response_model=ModelProviderListResponse)
async def list_model_providers(
    context: Annotated[SystemAuditContext, Depends(require_model_registry_admin_context)],
    service: Annotated[ModelRegistryService, Depends(get_model_registry_service)],
) -> ModelProviderListResponse:
    try:
        views = await service.list_providers(context)
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return ModelProviderListResponse(
        items=[_provider_response(view) for view in views],
        request_id=context.request_id,
    )


@router.post("/model-providers", response_model=ModelProviderMutationResponse)
async def create_model_provider(
    body: ModelProviderCreateRequest,
    context: Annotated[SystemAuditContext, Depends(require_model_registry_admin_context)],
    service: Annotated[ModelRegistryService, Depends(get_model_registry_service)],
) -> ModelProviderMutationResponse:
    try:
        view = await service.create_provider(
            context,
            name=body.name,
            base_url=body.base_url,
            request_timeout_seconds=body.request_timeout_seconds,
            api_key=body.api_key.get_secret_value(),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return ModelProviderMutationResponse(item=_provider_response(view), request_id=context.request_id)


@router.patch("/model-providers/{provider_id}", response_model=ModelProviderMutationResponse)
async def update_model_provider(
    provider_id: uuid.UUID,
    body: ModelProviderUpdateRequest,
    context: Annotated[SystemAuditContext, Depends(require_model_registry_admin_context)],
    service: Annotated[ModelRegistryService, Depends(get_model_registry_service)],
) -> ModelProviderMutationResponse:
    try:
        view = await service.update_provider(
            context,
            provider_id,
            name=body.name,
            base_url=body.base_url,
            request_timeout_seconds=body.request_timeout_seconds,
            api_key=(body.api_key.get_secret_value() if body.api_key is not None else None),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return ModelProviderMutationResponse(item=_provider_response(view), request_id=context.request_id)


@router.post(
    "/model-providers/test-connection",
    response_model=ModelProviderConnectionTestResponse,
)
async def test_model_provider_candidate(
    body: ModelProviderConnectionTestRequest,
    context: Annotated[SystemAuditContext, Depends(require_model_registry_admin_context)],
    catalog: Annotated[SystemModelCatalogService, Depends(get_system_model_catalog)],
    materializer: Annotated[
        SystemModelMaterializer,
        Depends(get_system_model_materializer),
    ],
    config: Annotated[AppConfig, Depends(get_config)],
) -> ModelProviderConnectionTestResponse:
    """Probe candidate Provider material without saving or replacing anything."""

    try:
        material = await catalog.prepare_candidate_connection_test(
            context,
            TestProviderCandidateConnection(
                base_url=body.base_url,
                provider_adapter=body.provider_adapter,
                provider_model=body.provider_model,
                max_input_tokens=body.max_input_tokens,
                settings=body.settings,
                supports_vision=body.supports_vision,
                api_key=body.api_key.get_secret_value(),
            ),
        )
        model = await materializer.materialize_connection_test(material)
    except SystemModelError as error:
        raise system_model_http_exception(error) from None
    except SystemModelMaterializationUnavailable:
        return ModelProviderConnectionTestResponse(
            status="failed",
            request_id=context.request_id,
        )
    return ModelProviderConnectionTestResponse(
        status=("succeeded" if await ModelConnectionTester(config).test(model) else "failed"),
        request_id=context.request_id,
    )


@router.delete("/model-providers/{provider_id}", response_model=ModelProviderDeleteResponse)
async def delete_model_provider(
    provider_id: uuid.UUID,
    context: Annotated[SystemAuditContext, Depends(require_model_registry_admin_context)],
    service: Annotated[ModelRegistryService, Depends(get_model_registry_service)],
) -> ModelProviderDeleteResponse:
    try:
        await service.delete_provider(context, provider_id)
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return ModelProviderDeleteResponse(request_id=context.request_id)


@router.get("/model-providers/{provider_id}/models", response_model=ProviderModelListResponse)
async def list_provider_models(
    provider_id: uuid.UUID,
    context: Annotated[SystemAuditContext, Depends(require_model_registry_admin_context)],
    service: Annotated[ModelRegistryService, Depends(get_model_registry_service)],
) -> ProviderModelListResponse:
    try:
        views = await service.list_models(context, provider_id)
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return ProviderModelListResponse(
        items=[_model_response(view) for view in views],
        request_id=context.request_id,
    )


@router.post("/model-providers/{provider_id}/models", response_model=ProviderModelMutationResponse)
async def create_provider_model(
    provider_id: uuid.UUID,
    body: ProviderModelCreateRequest,
    context: Annotated[SystemAuditContext, Depends(require_model_registry_admin_context)],
    service: Annotated[ModelRegistryService, Depends(get_model_registry_service)],
) -> ProviderModelMutationResponse:
    default_batch = _DEFAULT_EMBEDDING_MAX_BATCH if body.model_type == "embedding" else _DEFAULT_RERANK_MAX_BATCH
    try:
        view = await service.create_model(
            context,
            provider_id,
            model_type=body.model_type,
            model_name=body.model_name,
            embedding_dimension=body.embedding_dimension,
            max_batch=(body.max_batch if body.max_batch is not None else default_batch),
        )
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return ProviderModelMutationResponse(item=_model_response(view), request_id=context.request_id)


@router.patch("/provider-models/{model_id}", response_model=ProviderModelMutationResponse)
async def set_provider_model_status(
    model_id: uuid.UUID,
    body: ProviderModelStatusRequest,
    context: Annotated[SystemAuditContext, Depends(require_model_registry_admin_context)],
    service: Annotated[ModelRegistryService, Depends(get_model_registry_service)],
) -> ProviderModelMutationResponse:
    """Toggle status; identity fields are immutable — create a new model instead."""

    try:
        view = await service.set_model_status(context, model_id, body.status)
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return ProviderModelMutationResponse(item=_model_response(view), request_id=context.request_id)


@router.delete("/provider-models/{model_id}", response_model=ProviderModelDeleteResponse)
async def delete_provider_model(
    model_id: uuid.UUID,
    context: Annotated[SystemAuditContext, Depends(require_model_registry_admin_context)],
    service: Annotated[ModelRegistryService, Depends(get_model_registry_service)],
) -> ProviderModelDeleteResponse:
    try:
        await service.delete_model(context, model_id)
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return ProviderModelDeleteResponse(request_id=context.request_id)


@router.post("/provider-models/{model_id}/test", response_model=ProviderModelTestResponse)
async def test_provider_model(
    model_id: uuid.UUID,
    context: Annotated[SystemAuditContext, Depends(require_model_registry_admin_context)],
    service: Annotated[ModelRegistryService, Depends(get_model_registry_service)],
) -> ProviderModelTestResponse:
    try:
        result = await service.test_model(context, model_id)
    except KnowledgeError as error:
        raise knowledge_http_exception(error, context.request_id) from None
    return ProviderModelTestResponse(ok=result.ok, message=result.message, request_id=context.request_id)


__all__ = [
    "ModelProviderConnectionTestRequest",
    "ModelProviderConnectionTestResponse",
    "ModelProviderCreateRequest",
    "ModelProviderDeleteResponse",
    "ModelProviderItemResponse",
    "ModelProviderListResponse",
    "ModelProviderMutationResponse",
    "ModelProviderUpdateRequest",
    "ProviderModelCreateRequest",
    "ProviderModelDeleteResponse",
    "ProviderModelItemResponse",
    "ProviderModelListResponse",
    "ProviderModelMutationResponse",
    "ProviderModelStatusRequest",
    "ProviderModelTestResponse",
    "get_model_registry_service",
    "require_model_registry_admin_context",
    "router",
]
