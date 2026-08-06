"""Authenticated, secret-free public projection of active system models."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.gateway.deps import (
    get_current_agent_runtime_config,
    get_current_user_from_request,
    get_system_model_catalog,
)
from app.system_settings import (
    PublicSystemModelView,
    SystemModelCatalogService,
)
from app.system_settings.errors import SystemModelStorageUnavailable
from deerflow.config.app_config import AppConfig

router = APIRouter(prefix="/api", tags=["models"])


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ModelResponse(_StrictModel):
    """Safe selector metadata; provider configuration is admin-only."""

    name: str = Field(..., description="Stable logical model name")
    model: str = Field(
        ...,
        description="Compatibility alias equal to the logical model name",
    )
    display_name: str
    description: str
    supports_thinking: bool = False
    supports_reasoning_effort: bool = False
    supports_vision: bool = False
    is_default: bool = False


class TokenUsageResponse(_StrictModel):
    enabled: bool = False


class ModelsListResponse(_StrictModel):
    models: list[ModelResponse]
    token_usage: TokenUsageResponse


def _public_response(model: PublicSystemModelView) -> ModelResponse:
    return ModelResponse(
        name=model.logical_name,
        # Never expose the provider model identifier through this endpoint.
        model=model.logical_name,
        display_name=model.display_name,
        description=model.description,
        supports_thinking=model.supports_thinking,
        supports_reasoning_effort=model.supports_reasoning_effort,
        supports_vision=model.supports_vision,
        is_default=model.is_default,
    )


def _storage_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "code": "system_model_storage_unavailable",
            "message": "System model storage unavailable",
        },
    )


@router.get(
    "/models",
    response_model=ModelsListResponse,
    summary="List available models",
)
async def list_models(
    service: Annotated[
        SystemModelCatalogService,
        Depends(get_system_model_catalog),
    ],
    config: Annotated[AppConfig, Depends(get_current_agent_runtime_config)],
    _user=Depends(get_current_user_from_request),
) -> ModelsListResponse:
    del _user
    try:
        models = await service.list_available_models()
    except SystemModelStorageUnavailable:
        raise _storage_unavailable() from None
    return ModelsListResponse(
        models=[_public_response(model) for model in models],
        token_usage=TokenUsageResponse(enabled=config.token_usage.enabled),
    )


@router.get(
    "/models/{model_name}",
    response_model=ModelResponse,
    summary="Get available model",
)
async def get_model(
    model_name: str,
    service: Annotated[
        SystemModelCatalogService,
        Depends(get_system_model_catalog),
    ],
    _user=Depends(get_current_user_from_request),
) -> ModelResponse:
    del _user
    try:
        selected = next(
            (model for model in await service.list_available_models() if model.logical_name == model_name),
            None,
        )
    except SystemModelStorageUnavailable:
        raise _storage_unavailable() from None
    if selected is None:
        raise HTTPException(status_code=404, detail="Model not found")
    return _public_response(selected)


__all__ = [
    "ModelResponse",
    "ModelsListResponse",
    "TokenUsageResponse",
    "router",
]
