from __future__ import annotations

import uuid
from functools import wraps
from typing import Annotated, Literal, Protocol

from fastapi import APIRouter, Depends, Header, Path, Query, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictStr,
    field_validator,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.deps import (
    get_workflow_definition_service,
    get_workflow_project_control_service,
    project_session,
    workflow_project_context,
)
from app.private_work.context import PrivateWorkContext
from app.projects.context import ProjectContext
from app.workflows.authorization import ProjectWorkflowCapabilityPolicy, WorkflowAction
from app.workflows.catalog_contracts import (
    NodeCatalogResponseV1,
    WorkflowCatalogCapabilityProjectionV1,
    node_catalog_response_public_projection_v1,
)
from app.workflows.contracts import WorkflowProjectReadinessV1
from app.workflows.definition_contracts import (
    WorkflowCredentialGrantMutationRequestV1,
    WorkflowCredentialGrantResponseV1,
    WorkflowDefinitionArchiveRequestV1,
    WorkflowDefinitionCreateRequestV1,
    WorkflowDefinitionListQueryV1,
    WorkflowDefinitionPageV1,
    WorkflowDefinitionResponseV1,
    WorkflowDefinitionUpdateRequestV1,
    WorkflowDraftGrantIntentDeleteResponseV1,
    WorkflowDraftGrantIntentResponseV1,
    WorkflowDraftResponseV1,
    WorkflowDraftSaveRequestV1,
    WorkflowDraftValidateRequestV1,
    WorkflowDraftValidationResponseV1,
    WorkflowPublishRequestV1,
    WorkflowPublishResponseV1,
    WorkflowVersionListQueryV1,
    WorkflowVersionPageV1,
    WorkflowVersionResponseV1,
    workflow_definition_response_public_projection_v1,
)
from app.workflows.error_mapping import WorkflowRoute, workflow_http_exception
from app.workflows.errors import WorkflowError, WorkflowForbidden, WorkflowUnavailable

_CAPABILITY_POLICY = ProjectWorkflowCapabilityPolicy()
_SLOT_ID_PATTERN = r"^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$"
_IDEMPOTENCY_KEY_PATTERN = r"^[!-~]+$"


def _canonical_uuid(value: object) -> uuid.UUID:
    if type(value) is uuid.UUID:
        return value
    if type(value) is str and len(value) == 36 and value == value.lower():
        try:
            parsed = uuid.UUID(value)
        except ValueError:
            pass
        else:
            if str(parsed) == value:
                return parsed
    raise ValueError("UUID must use canonical lowercase hyphenated text")


_CanonicalUuid = Annotated[uuid.UUID, BeforeValidator(_canonical_uuid)]


class _StrictWorkflowQuery(BaseModel):
    # Query parameters arrive as strings and are decoded by FastAPI/Pydantic;
    # the closed enum/range and extra-field boundary remain strict.
    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkflowDefinitionListQuery(_StrictWorkflowQuery):
    query: str | None = Field(default=None, max_length=255)
    lifecycle: Literal["active", "archived"] = "active"
    publication: Literal["all", "draft_only", "published"] = "all"
    sort: Literal["updated_desc", "name_asc", "name_desc"] = "updated_desc"
    cursor: str | None = Field(default=None, min_length=1, max_length=1024, pattern=_IDEMPOTENCY_KEY_PATTERN)
    limit: int = Field(default=50, ge=1, le=100)

    @field_validator("query")
    @classmethod
    def require_trimmed_query(cls, value: str | None) -> str | None:
        if value == "":
            return None
        if value is not None and value != value.strip():
            raise ValueError("Workflow Definition query must be trimmed")
        return value

    def to_contract(self) -> WorkflowDefinitionListQueryV1:
        return WorkflowDefinitionListQueryV1(
            query=self.query,
            lifecycle=self.lifecycle,
            publication=self.publication,
            sort=self.sort,
            cursor=self.cursor,
            limit=self.limit,
        )


class WorkflowVersionListQuery(_StrictWorkflowQuery):
    cursor: str | None = Field(default=None, min_length=1, max_length=1024, pattern=_IDEMPOTENCY_KEY_PATTERN)
    limit: int = Field(default=50, ge=1, le=100)

    def to_contract(self) -> WorkflowVersionListQueryV1:
        return WorkflowVersionListQueryV1(cursor=self.cursor, limit=self.limit)


class WorkflowCredentialGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    credential_id: _CanonicalUuid
    expected_credential_version_id: _CanonicalUuid
    expected_slot_schema_checksum: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")

    def to_contract(self) -> WorkflowCredentialGrantMutationRequestV1:
        return WorkflowCredentialGrantMutationRequestV1(
            credential_id=self.credential_id,
            expected_credential_version_id=self.expected_credential_version_id,
            expected_slot_schema_checksum=self.expected_slot_schema_checksum,
        )


class WorkflowDefinitionGatewayService(Protocol):
    """Gateway-facing G15 service port; implementations own DB/domain work."""

    async def list_definitions(self, session: AsyncSession, **kwargs) -> object: ...

    async def create_definition(self, session: AsyncSession, **kwargs) -> object: ...

    async def get_definition(self, session: AsyncSession, **kwargs) -> object: ...

    async def update_definition(self, session: AsyncSession, **kwargs) -> object: ...

    async def get_draft(self, session: AsyncSession, **kwargs) -> object: ...

    async def save_draft(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        workflow_id: uuid.UUID,
        request: WorkflowDraftSaveRequestV1,
        *,
        idempotency_key: str,
    ) -> object: ...

    async def validate_draft(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        workflow_id: uuid.UUID,
        request: WorkflowDraftValidateRequestV1,
    ) -> object: ...

    async def publish(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        workflow_id: uuid.UUID,
        request: WorkflowPublishRequestV1,
        *,
        idempotency_key: str,
    ) -> object: ...

    async def list_versions(self, session: AsyncSession, **kwargs) -> object: ...

    async def get_version(self, session: AsyncSession, **kwargs) -> object: ...

    async def put_draft_grant_intent(self, session: AsyncSession, **kwargs) -> object: ...

    async def delete_draft_grant_intent(self, session: AsyncSession, **kwargs) -> object: ...

    async def put_version_grant(self, session: AsyncSession, **kwargs) -> object: ...

    async def revoke_version_grant(self, session: AsyncSession, **kwargs) -> object: ...

    async def archive_definition(self, session: AsyncSession, **kwargs) -> object: ...


def _map_workflow_errors(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except WorkflowError as error:
            raise workflow_http_exception(error) from None

    return wrapped


def _workflow_definition_transaction(
    response_type: type[BaseModel],
    *,
    response_status: int = status.HTTP_200_OK,
):
    """Own one transaction and exact public projection for one G15 route."""

    def decorate(function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            session = kwargs["session"]
            context = kwargs["context"]
            async with session.begin():
                result = await function(*args, **kwargs)
                if type(result) is not response_type:
                    raise WorkflowUnavailable(context.request_id)
                try:
                    projection = workflow_definition_response_public_projection_v1(
                        result  # type: ignore[arg-type]
                    )
                    return JSONResponse(
                        content=jsonable_encoder(projection),
                        status_code=response_status,
                    )
                except (TypeError, ValueError):
                    raise WorkflowUnavailable(context.request_id) from None

        return wrapped

    return decorate


router = APIRouter(
    prefix="/api/projects/{project_id}/workflows",
    tags=["project-workflows"],
    route_class=WorkflowRoute,
)


def _require_workflow_read(context: ProjectContext) -> None:
    if not _CAPABILITY_POLICY.allows(context, WorkflowAction.READ):
        raise WorkflowForbidden(context.request_id)


def _require_workflow_actions(
    context: ProjectContext,
    *actions: WorkflowAction,
) -> None:
    if not actions or any(not _CAPABILITY_POLICY.allows(context, action) for action in actions):
        raise WorkflowForbidden(context.request_id)


# Static control-plane routes intentionally precede every future
# ``/{workflow_id}`` route in this module.
@router.get("/readiness", response_model=WorkflowProjectReadinessV1)
@_map_workflow_errors
async def get_workflow_project_readiness(
    context: ProjectContext = Depends(workflow_project_context),
    session: AsyncSession = Depends(project_session),
    service=Depends(get_workflow_project_control_service),
) -> WorkflowProjectReadinessV1:
    _require_workflow_read(context)
    return await service.read_readiness(
        session,
        request_id=context.request_id,
    )


@router.get(
    "/node-catalog",
    response_model=NodeCatalogResponseV1,
    response_model_exclude_defaults=True,
)
@_map_workflow_errors
async def get_workflow_node_catalog(
    context: ProjectContext = Depends(workflow_project_context),
    session: AsyncSession = Depends(project_session),
    service=Depends(get_workflow_project_control_service),
) -> dict[str, object]:
    _require_workflow_read(context)
    capabilities = WorkflowCatalogCapabilityProjectionV1(
        code_use=_CAPABILITY_POLICY.allows(context, WorkflowAction.CODE_USE),
        http_use=_CAPABILITY_POLICY.allows(context, WorkflowAction.HTTP_USE),
    )
    result: NodeCatalogResponseV1 = await service.read_node_catalog(
        session,
        request_id=context.request_id,
        capabilities=capabilities,
    )
    return node_catalog_response_public_projection_v1(result)


@router.get(
    "",
    response_model=WorkflowDefinitionPageV1,
    response_model_exclude_unset=False,
    response_model_exclude_defaults=False,
)
@_map_workflow_errors
@_workflow_definition_transaction(WorkflowDefinitionPageV1)
async def list_workflow_definitions(
    query: Annotated[WorkflowDefinitionListQuery, Query()],
    context: ProjectContext = Depends(workflow_project_context),
    session: AsyncSession = Depends(project_session),
    service: WorkflowDefinitionGatewayService = Depends(get_workflow_definition_service),
) -> object:
    _require_workflow_actions(context, WorkflowAction.READ)
    return await service.list_definitions(
        session,
        context=PrivateWorkContext.from_project(context),
        query=query.query,
        lifecycle=query.lifecycle,
        publication=query.publication,
        sort=query.sort,
        cursor=query.cursor,
        limit=query.limit,
    )


@router.post(
    "",
    response_model=WorkflowDefinitionResponseV1,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_unset=False,
    response_model_exclude_defaults=False,
)
@_map_workflow_errors
@_workflow_definition_transaction(
    WorkflowDefinitionResponseV1,
    response_status=status.HTTP_201_CREATED,
)
async def create_workflow_definition(
    body: WorkflowDefinitionCreateRequestV1,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=255, pattern=_IDEMPOTENCY_KEY_PATTERN),
    ],
    context: ProjectContext = Depends(workflow_project_context),
    session: AsyncSession = Depends(project_session),
    service: WorkflowDefinitionGatewayService = Depends(get_workflow_definition_service),
) -> object:
    _require_workflow_actions(context, WorkflowAction.EDIT)
    return await service.create_definition(
        session,
        context=PrivateWorkContext.from_project(context),
        name=body.name,
        description=body.description,
        idempotency_key=idempotency_key,
    )


@router.get(
    "/{workflow_id}/draft",
    response_model=WorkflowDraftResponseV1,
    response_model_exclude_unset=False,
    response_model_exclude_defaults=False,
)
@_map_workflow_errors
@_workflow_definition_transaction(WorkflowDraftResponseV1)
async def get_workflow_draft(
    workflow_id: uuid.UUID,
    context: ProjectContext = Depends(workflow_project_context),
    session: AsyncSession = Depends(project_session),
    service: WorkflowDefinitionGatewayService = Depends(get_workflow_definition_service),
) -> object:
    _require_workflow_actions(context, WorkflowAction.READ)
    return await service.get_draft(
        session,
        context=PrivateWorkContext.from_project(context),
        workflow_id=workflow_id,
    )


@router.put(
    "/{workflow_id}/draft",
    response_model=WorkflowDraftResponseV1,
    response_model_exclude_unset=False,
    response_model_exclude_defaults=False,
)
@_map_workflow_errors
@_workflow_definition_transaction(WorkflowDraftResponseV1)
async def save_workflow_draft(
    workflow_id: uuid.UUID,
    body: WorkflowDraftSaveRequestV1,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=255, pattern=_IDEMPOTENCY_KEY_PATTERN),
    ],
    context: ProjectContext = Depends(workflow_project_context),
    session: AsyncSession = Depends(project_session),
    service: WorkflowDefinitionGatewayService = Depends(get_workflow_definition_service),
) -> object:
    _require_workflow_actions(context, WorkflowAction.EDIT)
    return await service.save_draft(
        session,
        context=PrivateWorkContext.from_project(context),
        workflow_id=workflow_id,
        request=body,
        idempotency_key=idempotency_key,
    )


@router.post(
    "/{workflow_id}/validate",
    response_model=WorkflowDraftValidationResponseV1,
    response_model_exclude_unset=False,
    response_model_exclude_defaults=False,
)
@_map_workflow_errors
@_workflow_definition_transaction(WorkflowDraftValidationResponseV1)
async def validate_workflow_draft(
    workflow_id: uuid.UUID,
    body: WorkflowDraftValidateRequestV1,
    context: ProjectContext = Depends(workflow_project_context),
    session: AsyncSession = Depends(project_session),
    service: WorkflowDefinitionGatewayService = Depends(get_workflow_definition_service),
) -> object:
    _require_workflow_actions(context, WorkflowAction.EDIT)
    return await service.validate_draft(
        session,
        context=PrivateWorkContext.from_project(context),
        workflow_id=workflow_id,
        request=body,
    )


@router.post(
    "/{workflow_id}/publish",
    response_model=WorkflowPublishResponseV1,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_unset=False,
    response_model_exclude_defaults=False,
)
@_map_workflow_errors
@_workflow_definition_transaction(
    WorkflowPublishResponseV1,
    response_status=status.HTTP_201_CREATED,
)
async def publish_workflow_draft(
    workflow_id: uuid.UUID,
    body: WorkflowPublishRequestV1,
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=255,
            pattern=_IDEMPOTENCY_KEY_PATTERN,
        ),
    ],
    context: ProjectContext = Depends(workflow_project_context),
    session: AsyncSession = Depends(project_session),
    service: WorkflowDefinitionGatewayService = Depends(get_workflow_definition_service),
) -> object:
    _require_workflow_actions(context, WorkflowAction.PUBLISH)
    return await service.publish(
        session,
        context=PrivateWorkContext.from_project(context),
        workflow_id=workflow_id,
        request=body,
        idempotency_key=idempotency_key,
    )


@router.get(
    "/{workflow_id}/versions",
    response_model=WorkflowVersionPageV1,
    response_model_exclude_unset=False,
    response_model_exclude_defaults=False,
)
@_map_workflow_errors
@_workflow_definition_transaction(WorkflowVersionPageV1)
async def list_workflow_versions(
    workflow_id: uuid.UUID,
    query: Annotated[WorkflowVersionListQuery, Query()],
    context: ProjectContext = Depends(workflow_project_context),
    session: AsyncSession = Depends(project_session),
    service: WorkflowDefinitionGatewayService = Depends(get_workflow_definition_service),
) -> object:
    _require_workflow_actions(context, WorkflowAction.READ)
    return await service.list_versions(
        session,
        context=PrivateWorkContext.from_project(context),
        workflow_id=workflow_id,
        cursor=query.cursor,
        limit=query.limit,
    )


@router.get(
    "/{workflow_id}/versions/{version_id}",
    response_model=WorkflowVersionResponseV1,
    response_model_exclude_unset=False,
    response_model_exclude_defaults=False,
)
@_map_workflow_errors
@_workflow_definition_transaction(WorkflowVersionResponseV1)
async def get_workflow_version(
    workflow_id: uuid.UUID,
    version_id: uuid.UUID,
    context: ProjectContext = Depends(workflow_project_context),
    session: AsyncSession = Depends(project_session),
    service: WorkflowDefinitionGatewayService = Depends(get_workflow_definition_service),
) -> object:
    _require_workflow_actions(context, WorkflowAction.READ)
    return await service.get_version(
        session,
        context=PrivateWorkContext.from_project(context),
        workflow_id=workflow_id,
        version_id=version_id,
    )


@router.put(
    "/{workflow_id}/draft/credential-grant-intents/{slot_id}",
    response_model=WorkflowDraftGrantIntentResponseV1,
    response_model_exclude_unset=False,
    response_model_exclude_defaults=False,
)
@_map_workflow_errors
@_workflow_definition_transaction(WorkflowDraftGrantIntentResponseV1)
async def put_workflow_draft_grant_intent(
    workflow_id: uuid.UUID,
    slot_id: Annotated[str, Path(pattern=_SLOT_ID_PATTERN)],
    body: WorkflowCredentialGrantRequest,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=255, pattern=_IDEMPOTENCY_KEY_PATTERN),
    ],
    context: ProjectContext = Depends(workflow_project_context),
    session: AsyncSession = Depends(project_session),
    service: WorkflowDefinitionGatewayService = Depends(get_workflow_definition_service),
) -> object:
    _require_workflow_actions(
        context,
        WorkflowAction.EDIT,
        WorkflowAction.CREDENTIAL_GRANT,
    )
    return await service.put_draft_grant_intent(
        session,
        context=PrivateWorkContext.from_project(context),
        workflow_id=workflow_id,
        slot_id=slot_id,
        credential_id=body.credential_id,
        expected_credential_version_id=body.expected_credential_version_id,
        expected_slot_schema_checksum=body.expected_slot_schema_checksum,
        idempotency_key=idempotency_key,
    )


@router.delete(
    "/{workflow_id}/draft/credential-grant-intents/{slot_id}",
    response_model=WorkflowDraftGrantIntentDeleteResponseV1,
    response_model_exclude_unset=False,
    response_model_exclude_defaults=False,
)
@_map_workflow_errors
@_workflow_definition_transaction(WorkflowDraftGrantIntentDeleteResponseV1)
async def delete_workflow_draft_grant_intent(
    workflow_id: uuid.UUID,
    slot_id: Annotated[str, Path(pattern=_SLOT_ID_PATTERN)],
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=255, pattern=_IDEMPOTENCY_KEY_PATTERN),
    ],
    context: ProjectContext = Depends(workflow_project_context),
    session: AsyncSession = Depends(project_session),
    service: WorkflowDefinitionGatewayService = Depends(get_workflow_definition_service),
) -> object:
    _require_workflow_actions(
        context,
        WorkflowAction.EDIT,
        WorkflowAction.CREDENTIAL_GRANT,
    )
    return await service.delete_draft_grant_intent(
        session,
        context=PrivateWorkContext.from_project(context),
        workflow_id=workflow_id,
        slot_id=slot_id,
        idempotency_key=idempotency_key,
    )


@router.put(
    "/{workflow_id}/versions/{version_id}/credential-grants/{slot_id}",
    response_model=WorkflowCredentialGrantResponseV1,
    response_model_exclude_unset=False,
    response_model_exclude_defaults=False,
)
@_map_workflow_errors
@_workflow_definition_transaction(WorkflowCredentialGrantResponseV1)
async def put_workflow_version_grant(
    workflow_id: uuid.UUID,
    version_id: uuid.UUID,
    slot_id: Annotated[str, Path(pattern=_SLOT_ID_PATTERN)],
    body: WorkflowCredentialGrantRequest,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=255, pattern=_IDEMPOTENCY_KEY_PATTERN),
    ],
    context: ProjectContext = Depends(workflow_project_context),
    session: AsyncSession = Depends(project_session),
    service: WorkflowDefinitionGatewayService = Depends(get_workflow_definition_service),
) -> object:
    _require_workflow_actions(
        context,
        WorkflowAction.PUBLISH,
        WorkflowAction.CREDENTIAL_GRANT,
    )
    return await service.put_version_grant(
        session,
        context=PrivateWorkContext.from_project(context),
        workflow_id=workflow_id,
        version_id=version_id,
        slot_id=slot_id,
        credential_id=body.credential_id,
        expected_credential_version_id=body.expected_credential_version_id,
        expected_slot_schema_checksum=body.expected_slot_schema_checksum,
        idempotency_key=idempotency_key,
    )


@router.delete(
    "/{workflow_id}/versions/{version_id}/credential-grants/{slot_id}",
    response_model=WorkflowCredentialGrantResponseV1,
    response_model_exclude_unset=False,
    response_model_exclude_defaults=False,
)
@_map_workflow_errors
@_workflow_definition_transaction(WorkflowCredentialGrantResponseV1)
async def revoke_workflow_version_grant(
    workflow_id: uuid.UUID,
    version_id: uuid.UUID,
    slot_id: Annotated[str, Path(pattern=_SLOT_ID_PATTERN)],
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=255, pattern=_IDEMPOTENCY_KEY_PATTERN),
    ],
    context: ProjectContext = Depends(workflow_project_context),
    session: AsyncSession = Depends(project_session),
    service: WorkflowDefinitionGatewayService = Depends(get_workflow_definition_service),
) -> object:
    _require_workflow_actions(
        context,
        WorkflowAction.PUBLISH,
        WorkflowAction.CREDENTIAL_GRANT,
    )
    return await service.revoke_version_grant(
        session,
        context=PrivateWorkContext.from_project(context),
        workflow_id=workflow_id,
        version_id=version_id,
        slot_id=slot_id,
        idempotency_key=idempotency_key,
    )


@router.post(
    "/{workflow_id}/archive",
    response_model=WorkflowDefinitionResponseV1,
    response_model_exclude_unset=False,
    response_model_exclude_defaults=False,
)
@_map_workflow_errors
@_workflow_definition_transaction(WorkflowDefinitionResponseV1)
async def archive_workflow_definition(
    workflow_id: uuid.UUID,
    body: WorkflowDefinitionArchiveRequestV1,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=255, pattern=_IDEMPOTENCY_KEY_PATTERN),
    ],
    context: ProjectContext = Depends(workflow_project_context),
    session: AsyncSession = Depends(project_session),
    service: WorkflowDefinitionGatewayService = Depends(get_workflow_definition_service),
) -> object:
    _require_workflow_actions(context, WorkflowAction.EDIT)
    return await service.archive_definition(
        session,
        context=PrivateWorkContext.from_project(context),
        workflow_id=workflow_id,
        expected_revision=body.expected_revision,
        idempotency_key=idempotency_key,
    )


@router.get(
    "/{workflow_id}",
    response_model=WorkflowDefinitionResponseV1,
    response_model_exclude_unset=False,
    response_model_exclude_defaults=False,
)
@_map_workflow_errors
@_workflow_definition_transaction(WorkflowDefinitionResponseV1)
async def get_workflow_definition(
    workflow_id: uuid.UUID,
    context: ProjectContext = Depends(workflow_project_context),
    session: AsyncSession = Depends(project_session),
    service: WorkflowDefinitionGatewayService = Depends(get_workflow_definition_service),
) -> object:
    _require_workflow_actions(context, WorkflowAction.READ)
    return await service.get_definition(
        session,
        context=PrivateWorkContext.from_project(context),
        workflow_id=workflow_id,
    )


@router.patch(
    "/{workflow_id}",
    response_model=WorkflowDefinitionResponseV1,
    response_model_exclude_unset=False,
    response_model_exclude_defaults=False,
)
@_map_workflow_errors
@_workflow_definition_transaction(WorkflowDefinitionResponseV1)
async def update_workflow_definition(
    workflow_id: uuid.UUID,
    body: WorkflowDefinitionUpdateRequestV1,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=255, pattern=_IDEMPOTENCY_KEY_PATTERN),
    ],
    context: ProjectContext = Depends(workflow_project_context),
    session: AsyncSession = Depends(project_session),
    service: WorkflowDefinitionGatewayService = Depends(get_workflow_definition_service),
) -> object:
    _require_workflow_actions(context, WorkflowAction.EDIT)
    return await service.update_definition(
        session,
        context=PrivateWorkContext.from_project(context),
        workflow_id=workflow_id,
        expected_revision=body.expected_revision,
        name=body.name,
        description=body.description,
        idempotency_key=idempotency_key,
    )


__all__ = [
    "WorkflowCredentialGrantRequest",
    "WorkflowDefinitionArchiveRequestV1",
    "WorkflowDefinitionCreateRequestV1",
    "WorkflowDefinitionGatewayService",
    "WorkflowDefinitionListQuery",
    "WorkflowDefinitionPageV1",
    "WorkflowDefinitionResponseV1",
    "WorkflowDefinitionUpdateRequestV1",
    "WorkflowDraftResponseV1",
    "WorkflowDraftSaveRequestV1",
    "WorkflowDraftValidateRequestV1",
    "WorkflowDraftValidationResponseV1",
    "WorkflowCredentialGrantResponseV1",
    "WorkflowDraftGrantIntentDeleteResponseV1",
    "WorkflowDraftGrantIntentResponseV1",
    "WorkflowPublishRequestV1",
    "WorkflowPublishResponseV1",
    "WorkflowVersionPageV1",
    "WorkflowVersionResponseV1",
    "router",
]
