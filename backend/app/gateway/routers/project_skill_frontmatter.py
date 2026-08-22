from __future__ import annotations

from typing import Annotated, Literal, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from app.gateway.routers.project_assets import (
    ASSET_ERRORS,
    AssetRoute,
    project_asset_context,
    raise_asset_domain,
)
from app.projects.context import ProjectContext
from app.shared_assets.errors import (
    SkillFrontmatterSourceStale,
    SkillSecretDeclarationInvalid,
)
from app.shared_assets.skill_frontmatter_service import (
    MAX_SKILL_FRONTMATTER_DOCUMENT_BYTES,
    SkillFrontmatterService,
)
from deerflow.skills.types import SecretRequirement

_SAFE_RESPONSE_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
}


class SkillFrontmatterRoute(AssetRoute):
    """Apply the secret-safe cache policy to success and error responses."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            try:
                response = await original(request)
            except HTTPException as exc:
                exc.headers = {
                    **(exc.headers or {}),
                    **_SAFE_RESPONSE_HEADERS,
                }
                raise
            response.headers.update(_SAFE_RESPONSE_HEADERS)
            return response

        return handler


router = APIRouter(
    prefix="/api/projects/{project_id}/skills/frontmatter",
    tags=["project-skill-frontmatter"],
    route_class=SkillFrontmatterRoute,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SkillFrontmatterSourceRequest(_StrictModel):
    content: str = Field(max_length=MAX_SKILL_FRONTMATTER_DOCUMENT_BYTES)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SkillSecretDeclarationRequest(_StrictModel):
    name: str = Field(
        min_length=1,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
    )
    target_env: str = Field(
        min_length=1,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
    )
    optional: StrictBool = False


class SkillFrontmatterPatchRequest(SkillFrontmatterSourceRequest):
    required_secrets: list[SkillSecretDeclarationRequest] = Field(
        max_length=256,
    )
    secrets_autonomous: StrictBool


class SkillFrontmatterDiagnosticResponse(_StrictModel):
    code: str
    severity: Literal["error", "warning"]
    field_path: list[str | int]
    line: int | None
    column: int | None
    public_message: str


class SkillSecretProjectionResponse(_StrictModel):
    required_secrets: list[SkillSecretDeclarationRequest]
    secrets_autonomous: bool
    secrets_autonomous_explicit: bool
    shorthand_count: int = Field(ge=0)


class SkillFrontmatterParseResponse(_StrictModel):
    source_sha256: str
    valid: bool
    patchable: bool
    projection: SkillSecretProjectionResponse | None
    diagnostics: list[SkillFrontmatterDiagnosticResponse]
    request_id: str


class SkillFrontmatterPatchResponse(_StrictModel):
    source_sha256: str
    result_sha256: str
    content: str
    changed: bool
    changed_fields: list[Literal["required-secrets", "secrets-autonomous"]]
    projection: SkillSecretProjectionResponse
    diagnostics: list[SkillFrontmatterDiagnosticResponse]
    request_id: str


def get_skill_frontmatter_service() -> SkillFrontmatterService:
    return SkillFrontmatterService()


def _diagnostics(values) -> list[SkillFrontmatterDiagnosticResponse]:
    return [
        SkillFrontmatterDiagnosticResponse(
            code=value.code,
            severity=value.severity,
            field_path=list(value.field_path),
            line=value.line,
            column=value.column,
            public_message=value.public_message,
        )
        for value in values
    ]


def _projection(value) -> SkillSecretProjectionResponse:
    return SkillSecretProjectionResponse(
        required_secrets=[
            SkillSecretDeclarationRequest(
                name=item.name,
                target_env=item.target_env,
                optional=item.optional,
            )
            for item in value.required_secrets
        ],
        secrets_autonomous=value.secrets_autonomous,
        secrets_autonomous_explicit=value.secrets_autonomous_explicit,
        shorthand_count=value.shorthand_count,
    )


def _safe_headers(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"


def _raise_frontmatter_error(exc: Exception, request_id: str) -> NoReturn:
    if isinstance(exc, SkillSecretDeclarationInvalid):
        raise HTTPException(
            status_code=422,
            detail={
                "code": exc.code,
                "message": exc.public_message,
                "request_id": request_id,
                "diagnostics": [item.model_dump() for item in _diagnostics(exc.diagnostics)],
            },
            headers=_SAFE_RESPONSE_HEADERS,
        ) from None
    if isinstance(exc, SkillFrontmatterSourceStale):
        raise HTTPException(
            status_code=409,
            detail={
                "code": exc.code,
                "message": exc.public_message,
                "request_id": request_id,
            },
            headers=_SAFE_RESPONSE_HEADERS,
        ) from None
    if isinstance(exc, ASSET_ERRORS):
        raise_asset_domain(exc)
    raise exc


@router.post("/parse", response_model=SkillFrontmatterParseResponse)
async def parse_project_skill_frontmatter(
    body: SkillFrontmatterSourceRequest,
    response: Response,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[
        SkillFrontmatterService,
        Depends(get_skill_frontmatter_service),
    ],
) -> SkillFrontmatterParseResponse:
    try:
        result = await service.parse(
            context,
            body.content,
            expected_source_sha256=body.source_sha256,
        )
    except Exception as exc:  # noqa: BLE001 - typed public boundary below
        _raise_frontmatter_error(exc, context.request_id)
    _safe_headers(response)
    return SkillFrontmatterParseResponse(
        source_sha256=result.source_sha256,
        valid=result.valid,
        patchable=result.patchable,
        projection=(_projection(result.projection) if result.projection is not None else None),
        diagnostics=_diagnostics(result.diagnostics),
        request_id=context.request_id,
    )


@router.post("/patch", response_model=SkillFrontmatterPatchResponse)
async def patch_project_skill_frontmatter(
    body: SkillFrontmatterPatchRequest,
    response: Response,
    context: Annotated[ProjectContext, Depends(project_asset_context)],
    service: Annotated[
        SkillFrontmatterService,
        Depends(get_skill_frontmatter_service),
    ],
) -> SkillFrontmatterPatchResponse:
    try:
        result = await service.patch(
            context,
            body.content,
            expected_source_sha256=body.source_sha256,
            required_secrets=tuple(
                SecretRequirement(
                    name=item.name,
                    target_env=item.target_env,
                    optional=item.optional,
                )
                for item in body.required_secrets
            ),
            secrets_autonomous=body.secrets_autonomous,
        )
    except Exception as exc:  # noqa: BLE001 - typed public boundary below
        _raise_frontmatter_error(exc, context.request_id)
    _safe_headers(response)
    return SkillFrontmatterPatchResponse(
        source_sha256=result.source_sha256,
        result_sha256=result.result_sha256,
        content=result.content,
        changed=result.changed,
        changed_fields=list(result.changed_fields),
        projection=_projection(result.projection),
        diagnostics=_diagnostics(result.diagnostics),
        request_id=context.request_id,
    )
