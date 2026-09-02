from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile, status

from app.gateway.routers.project_asset_routes.common import (
    ASSET_ERRORS,
    AssetRoute,
    _current_version_asset_item,
    _list_assets,
    _read_skill_archive_upload,
    _response_data,
    _version_call,
    get_binding_service,
    get_skill_secret_service,
    get_skill_service,
    project_asset_context,
    raise_asset_domain,
)
from app.gateway.routers.project_asset_routes.contracts import (
    ScopedCurrentVersionSkillAssetListResponse,
    SkillActivationReadinessResponse,
    SkillArchiveImportResponse,
    SkillFileContentItemResponse,
    SkillFileContentResponse,
    SkillForkRequest,
    SkillSecretClearRequest,
    SkillSecretExactReplaceRequest,
    SkillSecretReplaceRequest,
    SkillSecretSetResponse,
    SkillVersionItemResponse,
    SkillVersionResponse,
)
from app.projects.context import ProjectContext
from app.shared_assets import AssetKind, BindingService, SkillFileChange, SkillService
from app.shared_assets.skill_secret_service import SkillSecretService

primary_router = APIRouter(route_class=AssetRoute)
listing_router = APIRouter(route_class=AssetRoute)


@primary_router.get(
    "/skills/{asset_id}/versions/{version_id}/files/content",
    response_model=SkillFileContentResponse,
)
async def preview_project_skill_file(
    asset_id: uuid.UUID,
    version_id: uuid.UUID,
    response: Response,
    path: Annotated[str, Query(min_length=1, max_length=1024)],
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[SkillService, Depends(get_skill_service)],
):
    try:
        view = await service.preview_version_file(
            context,
            asset_id,
            version_id,
            path,
        )
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return SkillFileContentResponse(
            data=SkillFileContentItemResponse(**_response_data(view)),
            request_id=context.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@primary_router.post(
    "/skills/{asset_id}/versions/{source_version_id}/fork",
    response_model=SkillVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def fork_project_skill_version(
    asset_id: uuid.UUID,
    source_version_id: uuid.UUID,
    body: SkillForkRequest,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[SkillService, Depends(get_skill_service)],
):
    changes = tuple(
        SkillFileChange(
            op=item.op,
            path=item.path,
            content=getattr(item, "content", None),
            media_type=getattr(item, "media_type", None),
        )
        for item in body.changes
    )
    return await _version_call(
        context,
        lambda: service.fork_version(
            context,
            asset_id,
            source_version_id,
            changes,
            expected_asset_version=body.expected_revision,
            expected_source_payload_checksum=body.expected_source_payload_checksum,
        ),
        SkillVersionResponse,
    )


@primary_router.post(
    "/skills/import",
    response_model=SkillArchiveImportResponse,
    status_code=status.HTTP_201_CREATED,
)
async def import_project_skill_archive(
    archive: Annotated[UploadFile, File(description="Skill package archive")],
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[SkillService, Depends(get_skill_service)],
):
    try:
        payload, filename = await _read_skill_archive_upload(
            archive,
            context.request_id,
        )
        result = await service.create_project_from_archive_upload(
            context,
            payload,
            filename=filename,
        )
        return SkillArchiveImportResponse(
            item=_current_version_asset_item(result.asset),
            version=SkillVersionItemResponse.model_validate(
                _response_data(result.version),
            ),
            request_id=context.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


def _skill_secret_response(
    value,
    request_id: str,
) -> SkillSecretSetResponse:
    return SkillSecretSetResponse(
        **_response_data(value),
        request_id=request_id,
    )


@primary_router.get(
    "/skills/{skill_id}/versions/{version_id}/activation-readiness",
    response_model=SkillActivationReadinessResponse,
)
async def get_project_skill_activation_readiness(
    skill_id: uuid.UUID,
    version_id: uuid.UUID,
    response: Response,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[
        SkillSecretService,
        Depends(get_skill_secret_service),
    ],
):
    try:
        value = await service.get_for_version(context, skill_id, version_id)
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return SkillActivationReadinessResponse(
            **_response_data(value),
            request_id=context.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@primary_router.get(
    "/skills/{skill_id}/versions/{version_id}/secrets",
    response_model=SkillSecretSetResponse,
)
async def get_project_skill_version_secrets(
    skill_id: uuid.UUID,
    version_id: uuid.UUID,
    response: Response,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[
        SkillSecretService,
        Depends(get_skill_secret_service),
    ],
):
    try:
        value = await service.get_exact(context, skill_id, version_id)
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return _skill_secret_response(
            value,
            context.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@primary_router.put(
    "/skills/{skill_id}/versions/{version_id}/secrets",
    response_model=SkillSecretSetResponse,
)
async def replace_project_skill_version_secrets(
    skill_id: uuid.UUID,
    version_id: uuid.UUID,
    body: SkillSecretExactReplaceRequest,
    response: Response,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[
        SkillSecretService,
        Depends(get_skill_secret_service),
    ],
):
    try:
        value = await service.replace_for_version(
            context,
            skill_id,
            version_id,
            body.secrets,
        )
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return _skill_secret_response(
            value,
            context.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@primary_router.get(
    "/skills/{skill_id}/secrets",
    response_model=SkillSecretSetResponse,
)
async def get_project_skill_secrets(
    skill_id: uuid.UUID,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[
        SkillSecretService,
        Depends(get_skill_secret_service),
    ],
):
    try:
        return _skill_secret_response(
            await service.get(context, skill_id),
            context.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@primary_router.put(
    "/skills/{skill_id}/secrets",
    response_model=SkillSecretSetResponse,
)
async def replace_project_skill_secrets(
    skill_id: uuid.UUID,
    body: SkillSecretReplaceRequest,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[
        SkillSecretService,
        Depends(get_skill_secret_service),
    ],
):
    try:
        return _skill_secret_response(
            await service.replace(
                context,
                skill_id,
                body.secrets,
                expected_skill_version_id=body.expected_skill_version_id,
            ),
            context.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@primary_router.post(
    "/skills/{skill_id}/versions/{version_id}/secrets/{secret_name}/clear",
    response_model=SkillSecretSetResponse,
)
async def clear_project_skill_version_secret(
    skill_id: uuid.UUID,
    version_id: uuid.UUID,
    secret_name: str,
    body: SkillSecretClearRequest,
    response: Response,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[SkillSecretService, Depends(get_skill_secret_service)],
):
    try:
        value = await service.clear(
            context,
            skill_id,
            version_id,
            secret_name,
            confirmed=body.confirmed,
        )
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return _skill_secret_response(value, context.request_id)
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


@listing_router.get(
    "/skills",
    response_model=ScopedCurrentVersionSkillAssetListResponse,
)
async def list_project_skills(
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[SkillService, Depends(get_skill_service)],
    binding_service: Annotated[BindingService, Depends(get_binding_service)],
):
    return await _list_assets(context, AssetKind.SKILL, service, binding_service)
