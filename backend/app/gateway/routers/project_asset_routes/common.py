from __future__ import annotations

import base64
import binascii
import uuid
from collections.abc import Mapping
from dataclasses import fields as dataclass_fields
from dataclasses import is_dataclass
from ipaddress import ip_address
from typing import Annotated, NoReturn
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.deps import get_config, get_current_user_from_request
from app.private_work.agent_runtime_assessment import AgentRuntimeAssessmentService
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext, resolve_project_context
from app.projects.errors import (
    ProjectDatabaseUnavailable,
    ProjectForbidden,
    ProjectNotFound,
)
from app.shared_assets import (
    AgentDesignConflictUnresolved,
    AgentDesignGenerationProfileStale,
    AgentDesignSessionLimitExceeded,
    AgentDesignSlugConflict,
    AgentService,
    AssetConflict,
    AssetForbidden,
    AssetInUse,
    AssetKind,
    AssetNotFound,
    AssetRunAdmissionBusy,
    AssetRunPayloadTooLarge,
    AssetRunQuotaExceeded,
    AssetScope,
    AssetStorageQuotaExceeded,
    AssetStorageUnavailable,
    AssetValidationFailed,
    BindingService,
    McpDefinition,
    McpService,
    ProjectDefaultAgentService,
    SharedAssetError,
    SkillArchiveFile,
    SkillArchiveLimitExceeded,
    SkillDesignNoChanges,
    SkillDesignTargetDeleted,
    SkillDesignTargetSessionExists,
    SkillDesignTargetUnsupported,
    SkillRuntimeNameConflict,
    SkillSecretConfigurationInvalid,
    SkillSecretRevisionStale,
    SkillSecretsIncomplete,
    SkillService,
)
from app.shared_assets.agent_catalog import AgentCatalogValidator, StaticToolGroupCatalog
from app.shared_assets.contexts import SystemAssetReadContext, resolve_asset_reader
from app.shared_assets.mcp_secret_service import McpSecretService
from app.shared_assets.skill_archive import MAX_SKILL_ARCHIVE_UPLOAD_BYTES
from app.shared_assets.skill_secret_service import SkillSecretService
from app.shared_assets.skill_service import MAX_SKILL_ARCHIVE_BYTES
from deerflow.config.app_config import AppConfig
from deerflow.mcp_definition_policy import NetworkMcpEndpointPolicy
from deerflow.mcp_endpoint_policy import validate_remote_mcp_endpoint_syntax
from deerflow.persistence.engine import get_session_factory
from deerflow.trace_context import generate_trace_id, get_current_trace_id

from .contracts import (
    MAX_SKILL_ARCHIVE_BASE64_CHARS,
    AgentAssetItemResponse,
    AgentBindingItemResponse,
    AgentDefinitionItemResponse,
    AgentDefinitionResponse,
    AssetItemResponse,
    AssetMutationResponse,
    BindingItemResponse,
    CurrentBindingItemResponse,
    CurrentVersionAssetItemResponse,
    CurrentVersionAssetMutationResponse,
    ProjectAgentItemResponse,
    ProjectAssetItemResponse,
    ProjectCurrentVersionSkillItemResponse,
    ScopedAgentAssetListResponse,
    ScopedAssetListResponse,
    ScopedCurrentVersionSkillAssetListResponse,
    SkillVersionRequest,
    _StrictModel,
)


class AssetRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError:
                request_id = get_current_trace_id() or generate_trace_id()
                raise_asset_domain(AssetValidationFailed(request_id), request_id)

        return handler


def _binding_item_response(
    view,
) -> BindingItemResponse | CurrentBindingItemResponse | AgentBindingItemResponse:
    values = vars(view)
    if view.kind is AssetKind.MCP:
        return BindingItemResponse(**values)
    if view.kind is AssetKind.AGENT:
        return AgentBindingItemResponse(
            **{key: value for key, value in values.items() if key != "version_id"},
            definition_id=view.version_id,
        )
    return CurrentBindingItemResponse(
        **{key: value for key, value in values.items() if key != "version_id"},
        current_version_id=view.version_id,
    )


ASSET_ERRORS = (
    AssetNotFound,
    AssetForbidden,
    AssetInUse,
    AssetConflict,
    AssetValidationFailed,
    AssetStorageUnavailable,
    AssetStorageQuotaExceeded,
    AssetRunQuotaExceeded,
    AssetRunAdmissionBusy,
    AssetRunPayloadTooLarge,
    SkillDesignTargetUnsupported,
    SkillDesignTargetSessionExists,
    SkillDesignTargetDeleted,
    SkillDesignNoChanges,
    SkillSecretConfigurationInvalid,
    SkillSecretRevisionStale,
    SkillSecretsIncomplete,
    SkillRuntimeNameConflict,
    AgentDesignSessionLimitExceeded,
    AgentDesignSlugConflict,
    AgentDesignConflictUnresolved,
    AgentDesignGenerationProfileStale,
    SkillArchiveLimitExceeded,
)


def raise_asset_domain(exc: SharedAssetError, request_id: str | None = None) -> NoReturn:
    known = {
        AssetNotFound: 404,
        AssetForbidden: 403,
        AssetInUse: 409,
        AssetConflict: 409,
        AssetValidationFailed: 422,
        AssetStorageQuotaExceeded: 429,
        AssetRunQuotaExceeded: 429,
        AssetRunAdmissionBusy: 503,
        AssetRunPayloadTooLarge: 413,
        AssetStorageUnavailable: 503,
        SkillDesignTargetUnsupported: 422,
        SkillDesignTargetSessionExists: 409,
        SkillDesignTargetDeleted: 409,
        SkillDesignNoChanges: 409,
        SkillSecretConfigurationInvalid: 422,
        SkillSecretsIncomplete: 422,
        SkillSecretRevisionStale: 409,
        SkillRuntimeNameConflict: 409,
        AgentDesignSessionLimitExceeded: 429,
        AgentDesignSlugConflict: 409,
        AgentDesignConflictUnresolved: 409,
        AgentDesignGenerationProfileStale: 409,
        SkillArchiveLimitExceeded: 413,
    }
    status_code = known.get(type(exc))
    if status_code is None:
        raise exc
    raise HTTPException(
        status_code,
        detail={
            "code": exc.code,
            "message": exc.public_message,
            "request_id": request_id or exc.request_id,
        },
        headers={"Retry-After": "1"} if type(exc) in {AssetStorageQuotaExceeded, AssetRunQuotaExceeded, AssetRunAdmissionBusy} else None,
    ) from None


async def authenticated_asset_identity(
    user=Depends(get_current_user_from_request),
) -> tuple[uuid.UUID, str]:
    return uuid.UUID(str(user.id)), get_current_trace_id() or generate_trace_id()


async def system_asset_catalog_actor(
    user=Depends(get_current_user_from_request),
) -> SystemAssetReadContext:
    request_id = get_current_trace_id() or generate_trace_id()
    try:
        return resolve_asset_reader(user, request_id=request_id)
    except AssetForbidden as exc:
        raise_asset_domain(exc)


async def asset_session():
    from deerflow.persistence.engine import get_session_factory as resolve_session_factory

    request_id = get_current_trace_id() or generate_trace_id()
    try:
        factory = resolve_session_factory()
    except RuntimeError:
        raise_asset_domain(AssetStorageUnavailable(request_id))
    async with factory() as session:
        yield session


async def project_asset_context(
    project_id: uuid.UUID,
    identity: Annotated[tuple[uuid.UUID, str], Depends(authenticated_asset_identity)],
    session: Annotated[AsyncSession, Depends(asset_session)],
) -> ProjectContext:
    user_id, request_id = identity
    try:
        return await resolve_project_context(session, user_id, project_id, request_id)
    except ProjectNotFound:
        raise_asset_domain(AssetNotFound(request_id))
    except ProjectForbidden:
        raise_asset_domain(AssetForbidden(request_id))
    except ProjectDatabaseUnavailable:
        raise_asset_domain(AssetStorageUnavailable(request_id))


def _factory():
    try:
        return get_session_factory()
    except RuntimeError:
        raise HTTPException(
            503,
            detail={
                "code": AssetStorageUnavailable.code,
                "message": AssetStorageUnavailable.public_message,
                "request_id": get_current_trace_id() or generate_trace_id(),
            },
        ) from None


def _governance_sink(request: Request):
    value = getattr(request.app.state, "shared_asset_audit_sink", None)
    if value is None:
        request_id = get_current_trace_id() or generate_trace_id()
        raise_asset_domain(AssetStorageUnavailable(request_id))
    return value


def _agent_tool_group_catalog(config: AppConfig) -> StaticToolGroupCatalog:
    return StaticToolGroupCatalog(
        (
            *(group.name for group in config.tool_groups),
            *(tool.group for tool in config.tools),
            "task",
        )
    )


def get_agent_service(
    request: Request,
    config: AppConfig = Depends(get_config),
) -> AgentService:
    return AgentService(
        _factory(),
        governance_sink=_governance_sink(request),
        catalog_validator=AgentCatalogValidator(
            _agent_tool_group_catalog(config),
        ),
    )


def get_agent_runtime_assessment_service(
    request: Request,
) -> AgentRuntimeAssessmentService:
    endpoint_policy = getattr(request.app.state, "mcp_endpoint_policy", None)
    if not isinstance(endpoint_policy, NetworkMcpEndpointPolicy):
        request_id = get_current_trace_id() or generate_trace_id()
        raise_asset_domain(AssetStorageUnavailable(request_id))
    return AgentRuntimeAssessmentService(
        _factory(),
        endpoint_policy=endpoint_policy,
    )


def get_skill_service(request: Request) -> SkillService:
    quota = getattr(request.app.state, "project_quota_enforcer", None)
    return SkillService(
        _factory(),
        governance_sink=_governance_sink(request),
        quota=quota,
    )


def get_mcp_service(request: Request) -> McpService:
    endpoint_policy = getattr(request.app.state, "mcp_endpoint_policy", None)
    if not isinstance(endpoint_policy, NetworkMcpEndpointPolicy):
        request_id = get_current_trace_id() or generate_trace_id()
        raise_asset_domain(AssetStorageUnavailable(request_id))
    return McpService(
        _factory(),
        governance_sink=_governance_sink(request),
        endpoint_policy=endpoint_policy,
    )


def get_mcp_secret_service(request: Request) -> McpSecretService:
    return McpSecretService(
        _factory(),
        governance_sink=_governance_sink(request),
    )


def get_binding_service(request: Request) -> BindingService:
    return BindingService(_factory(), governance_sink=_governance_sink(request))


def get_project_default_agent_service(
    request: Request,
) -> ProjectDefaultAgentService:
    return ProjectDefaultAgentService(
        _factory(),
        governance_sink=_governance_sink(request),
    )


def get_skill_secret_service(
    request: Request,
) -> SkillSecretService:
    return SkillSecretService(
        _factory(),
        governance_sink=_governance_sink(request),
    )


def _asset_item(view) -> AssetItemResponse:
    return AssetItemResponse.model_validate(view, from_attributes=True)


def _current_version_asset_item(view) -> CurrentVersionAssetItemResponse:
    return CurrentVersionAssetItemResponse.model_validate(
        view,
        from_attributes=True,
    )


def _agent_asset_item(view) -> AgentAssetItemResponse:
    return AgentAssetItemResponse.model_validate(view, from_attributes=True)


def _agent_definition_response(result, request_id: str) -> AgentDefinitionResponse:
    return AgentDefinitionResponse(
        item=_agent_asset_item(result.asset),
        definition=AgentDefinitionItemResponse.model_validate(
            _response_data(result.definition),
        ),
        request_id=request_id,
    )


def _asset_item_capabilities(
    context: ProjectContext,
    scope: AssetScope,
    kind: AssetKind,
) -> list[Capability]:
    allowed = {
        Capability.SHARED_ASSETS_READ,
        Capability.SHARED_ASSETS_EXECUTE,
        Capability.SHARED_ASSETS_MANAGE_BINDINGS,
    }
    if scope is AssetScope.PROJECT:
        allowed.add(Capability.SHARED_ASSETS_EDIT)
    return sorted(context.capabilities & allowed, key=str)


def _scoped_assets(
    views,
    bindings,
    context: ProjectContext,
    kind: AssetKind,
) -> ScopedAssetListResponse | ScopedAgentAssetListResponse | ScopedCurrentVersionSkillAssetListResponse:
    by_asset_id = {binding.asset_id: binding for binding in bindings}
    if kind is AssetKind.SKILL:
        item_model = ProjectCurrentVersionSkillItemResponse
        response_model = ScopedCurrentVersionSkillAssetListResponse
    elif kind is AssetKind.AGENT:
        item_model = ProjectAgentItemResponse
        response_model = ScopedAgentAssetListResponse
    else:
        item_model = ProjectAssetItemResponse
        response_model = ScopedAssetListResponse
    items = [
        item_model(
            **vars(view),
            capabilities=_asset_item_capabilities(context, view.scope, kind),
            binding=(_binding_item_response(by_asset_id[view.id]) if view.scope is AssetScope.SYSTEM and view.id in by_asset_id else None),
        )
        for view in views
    ]
    return response_model(
        system_items=[item for item in items if item.scope is AssetScope.SYSTEM],
        project_items=[item for item in items if item.scope is AssetScope.PROJECT],
        request_id=context.request_id,
    )


async def _list_assets(
    context: ProjectContext,
    kind: AssetKind,
    service,
    binding_service: BindingService,
) -> ScopedAssetListResponse | ScopedAgentAssetListResponse | ScopedCurrentVersionSkillAssetListResponse:
    try:
        views = await service.list_visible(context)
        bindings = await binding_service.list_visible(context, kind)
        return _scoped_assets(views, bindings, context, kind)
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


async def _asset_call(actor, operation):
    try:
        result = await operation()
        return AssetMutationResponse(item=_asset_item(result), request_id=actor.request_id)
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


async def _current_version_asset_call(actor, operation):
    try:
        result = await operation()
        return CurrentVersionAssetMutationResponse(
            item=_current_version_asset_item(result),
            request_id=actor.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


async def _agent_definition_call(actor, operation):
    try:
        return _agent_definition_response(await operation(), actor.request_id)
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


async def _version_call(actor, operation, response_model: type[_StrictModel]):
    try:
        result = await operation()
        return response_model(
            data=_response_data(
                result,
                redact_project_mcp=_is_project_asset_actor(actor),
            ),
            request_id=actor.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


async def _version_history(actor, operation, response_model: type[_StrictModel]):
    try:
        versions = await operation()
        return response_model(
            data=[
                _response_data(
                    version,
                    redact_project_mcp=_is_project_asset_actor(actor),
                )
                for version in versions
            ],
            request_id=actor.request_id,
        )
    except ASSET_ERRORS as exc:
        raise_asset_domain(exc)


def _is_project_asset_actor(actor: object) -> bool:
    return (
        isinstance(actor, ProjectContext)
        or getattr(
            actor,
            "project_id",
            None,
        )
        is not None
    )


def _redacted_project_mcp_url(value: object) -> str | None:
    """Expose only a non-secret HTTP(S) origin from historical Project rows."""

    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"} or not hostname or "*" in hostname or parsed.username is not None or parsed.password is not None or "#" in value or parsed.netloc.endswith(":") or (port is not None and not 1 <= port <= 65535):
        return None
    authority = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        authority = f"{authority}:{port}"
    return f"{scheme}://{authority}"


def _editable_project_mcp_url(value: object) -> str | None:
    """Expose a path only for a structurally safe IP-literal endpoint.

    The service revalidates the selected current definition against the
    process-frozen CIDR policy before this response projection is reached.
    Unsafe or non-current-compatible values fall back to the historical
    origin-only representation.
    """

    origin = _redacted_project_mcp_url(value)
    if origin is None or not isinstance(value, str):
        return origin
    try:
        endpoint = validate_remote_mcp_endpoint_syntax(value)
        hostname = urlsplit(endpoint).hostname
        if hostname is None:
            return origin
        ip_address(hostname)
    except ValueError:
        return origin
    return endpoint


def _response_data(
    value: object,
    *,
    redact_project_mcp: bool = False,
    editable_project_mcp: bool = False,
) -> object:
    """Copy immutable domain views into ordinary response-safe containers."""
    if is_dataclass(value) and not isinstance(value, type):
        response = {
            field.name: _response_data(
                getattr(value, field.name),
                redact_project_mcp=redact_project_mcp,
                editable_project_mcp=editable_project_mcp,
            )
            for field in dataclass_fields(value)
        }
        if isinstance(value, McpDefinition):
            # Historical versions may contain values that were labelled
            # "non-secret" at authoring time. Arbitrary values cannot be
            # classified reliably, so public API responses expose only the
            # Secret-slot schema and never replay persisted env/header values.
            response["env"] = {}
            response["headers"] = {}
            if redact_project_mcp:
                response["command"] = None
                response["args"] = []
                project_url = getattr(value, "url", None)
                response["url"] = _editable_project_mcp_url(project_url) if editable_project_mcp else _redacted_project_mcp_url(project_url)
                response["oauth"] = {}
                response["routing"] = {}
                response["tool_overrides"] = {}
        return response
    if isinstance(value, Mapping):
        return {
            str(key): _response_data(
                item,
                redact_project_mcp=redact_project_mcp,
                editable_project_mcp=editable_project_mcp,
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _response_data(
                item,
                redact_project_mcp=redact_project_mcp,
                editable_project_mcp=editable_project_mcp,
            )
            for item in value
        ]
    return value


def _decode_skill_files(body: SkillVersionRequest, request_id: str) -> tuple[SkillArchiveFile, ...]:
    try:
        if sum(len(item.content_base64) for item in body.files) > MAX_SKILL_ARCHIVE_BASE64_CHARS:
            raise AssetValidationFailed(request_id)
        files: list[SkillArchiveFile] = []
        total_decoded_bytes = 0
        for item in body.files:
            content = base64.b64decode(item.content_base64, validate=True)
            total_decoded_bytes += len(content)
            if total_decoded_bytes > MAX_SKILL_ARCHIVE_BYTES:
                raise AssetValidationFailed(request_id)
            files.append(
                SkillArchiveFile(
                    path=item.path,
                    content=content,
                    media_type=item.media_type,
                )
            )
        return tuple(files)
    except AssetValidationFailed as exc:
        raise_asset_domain(exc)
    except (binascii.Error, ValueError):
        raise_asset_domain(AssetValidationFailed(request_id))


async def _read_skill_archive_upload(
    archive: UploadFile,
    request_id: str,
) -> tuple[bytes, str]:
    filename = archive.filename
    if not isinstance(filename, str) or not filename.strip():
        raise AssetValidationFailed(request_id)
    payload = bytearray()
    try:
        while True:
            remaining = MAX_SKILL_ARCHIVE_UPLOAD_BYTES - len(payload)
            chunk = await archive.read(min(1024 * 1024, remaining + 1))
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > MAX_SKILL_ARCHIVE_UPLOAD_BYTES:
                raise SkillArchiveLimitExceeded(request_id)
    finally:
        await archive.close()
    if not payload:
        raise AssetValidationFailed(request_id)
    return bytes(payload), filename
