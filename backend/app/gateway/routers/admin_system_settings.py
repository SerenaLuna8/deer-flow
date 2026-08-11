"""System-admin runtime-policy settings backed only by PostgreSQL."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, model_validator

from app.audit.models import SystemAuditContext
from app.gateway.deps import get_system_runtime_policy_service
from app.gateway.routers.admin_model_settings import current_model_admin_context
from app.gateway.routers.admin_operations import AdminOperationsRoute
from app.system_runtime_settings.errors import (
    SystemRuntimePolicyError,
    SystemRuntimePolicyInvalid,
)
from app.system_runtime_settings.models import (
    RuntimePolicyCatalogView,
    RuntimePolicySection,
    RuntimePolicyUpdateResult,
    RuntimePolicyView,
)
from app.system_runtime_settings.service import SystemRuntimePolicyService
from app.workflows.runtime_policy import (
    WorkflowRuntimeAdminPolicyV1,
    WorkflowRuntimePolicyUpdateRequestV1,
    WorkflowRuntimePolicyUpdateResponseV1,
)

router = APIRouter(
    prefix="/api/admin/settings/system",
    tags=["admin-system-settings"],
    route_class=AdminOperationsRoute,
)

_CatalogSectionName = Literal[
    "agent_runtime",
    "auth",
    "memory_document",
    "quotas",
    "workflow_runtime",
]
_GenericMutableSectionName = Literal[
    "agent_runtime",
    "auth",
    "memory_document",
    "quotas",
]
_GENERIC_MUTABLE_SECTIONS = frozenset(
    {
        RuntimePolicySection.AGENT_RUNTIME.value,
        RuntimePolicySection.AUTH.value,
        RuntimePolicySection.MEMORY_DOCUMENT.value,
        RuntimePolicySection.QUOTAS.value,
    }
)
_EffectScope = Literal[
    "new_requests_and_runs",
    "new_requests",
    "new_memory_documents",
    "next_authoritative_check",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AdminSystemPolicyResponse(_StrictModel):
    revision: int
    schema_version: int
    value: dict[str, object]


class AdminSystemSectionResponse(AdminSystemPolicyResponse):
    section: _GenericMutableSectionName
    effect_scope: _EffectScope
    effective_revision: int
    updated_at: datetime


class AdminSystemCatalogResponse(_StrictModel):
    catalog_revision: int
    sections: dict[
        _CatalogSectionName,
        AdminSystemSectionResponse | WorkflowRuntimeAdminPolicyV1,
    ]

    @model_validator(mode="after")
    def validate_section_shapes(self) -> AdminSystemCatalogResponse:
        expected = {section.value for section in RuntimePolicySection}
        if set(self.sections) != expected:
            raise ValueError("system settings response requires the complete section catalog")
        for key, value in self.sections.items():
            if key == RuntimePolicySection.WORKFLOW_RUNTIME.value:
                if not isinstance(value, WorkflowRuntimeAdminPolicyV1):
                    raise ValueError("workflow_runtime requires its closed admin projection")
            elif not isinstance(value, AdminSystemSectionResponse) or value.section != key:
                raise ValueError("system settings section key and payload do not match")
        return self


class AdminSystemUpdateRequest(_StrictModel):
    expected_revision: int
    value: dict[str, object]


class AdminSystemUpdateResponse(_StrictModel):
    catalog_revision: int
    section: _GenericMutableSectionName
    stored_revision: int
    effective_revision: int
    effect_scope: _EffectScope
    effective_at: datetime
    pending_roles: list[Literal["gateway", "worker", "scheduler"]]
    policy: AdminSystemPolicyResponse


def _section_response(view: RuntimePolicyView) -> AdminSystemSectionResponse:
    return AdminSystemSectionResponse(
        section=view.section.value,
        revision=view.revision,
        schema_version=view.schema_version,
        value=view.value.model_dump(mode="json"),
        effect_scope=view.effect_scope,
        effective_revision=view.effective_revision,
        updated_at=view.updated_at,
    )


def _catalog_response(
    catalog: RuntimePolicyCatalogView,
) -> AdminSystemCatalogResponse:
    if catalog.workflow_runtime is None:
        raise ValueError("workflow_runtime admin projection is unavailable")
    sections: dict[
        _CatalogSectionName,
        AdminSystemSectionResponse | WorkflowRuntimeAdminPolicyV1,
    ] = {section.value: _section_response(catalog.sections[section]) for section in RuntimePolicySection if section is not RuntimePolicySection.WORKFLOW_RUNTIME}
    sections[RuntimePolicySection.WORKFLOW_RUNTIME.value] = catalog.workflow_runtime
    return AdminSystemCatalogResponse(
        catalog_revision=catalog.catalog_revision,
        sections=sections,
    )


def _update_response(
    result: RuntimePolicyUpdateResult,
) -> AdminSystemUpdateResponse:
    view = result.policy
    return AdminSystemUpdateResponse(
        catalog_revision=result.catalog_revision,
        section=view.section.value,
        stored_revision=view.revision,
        effective_revision=view.effective_revision,
        effect_scope=view.effect_scope,
        effective_at=result.effective_at,
        pending_roles=list(result.pending_roles),
        policy=AdminSystemPolicyResponse(
            revision=view.revision,
            schema_version=view.schema_version,
            value=view.value.model_dump(mode="json"),
        ),
    )


def _http_exception(error: SystemRuntimePolicyError) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail={
            "code": error.code,
            "message": error.public_message,
            "request_id": error.request_id,
        },
    )


@router.get("", response_model=AdminSystemCatalogResponse)
async def get_admin_system_settings(
    context: Annotated[
        SystemAuditContext,
        Depends(current_model_admin_context),
    ],
    service: Annotated[
        SystemRuntimePolicyService,
        Depends(get_system_runtime_policy_service),
    ],
) -> AdminSystemCatalogResponse:
    try:
        return _catalog_response(await service.list_policies(context))
    except SystemRuntimePolicyError as error:
        raise _http_exception(error) from None


@router.put(
    "/workflow_runtime",
    response_model=WorkflowRuntimePolicyUpdateResponseV1,
)
async def update_admin_workflow_runtime_setting(
    body: WorkflowRuntimePolicyUpdateRequestV1,
    context: Annotated[
        SystemAuditContext,
        Depends(current_model_admin_context),
    ],
    service: Annotated[
        SystemRuntimePolicyService,
        Depends(get_system_runtime_policy_service),
    ],
) -> WorkflowRuntimePolicyUpdateResponseV1:
    try:
        return await service.update_workflow_runtime_policy(context, body)
    except SystemRuntimePolicyError as error:
        raise _http_exception(error) from None


@router.put(
    "/{section}",
    response_model=AdminSystemUpdateResponse,
)
async def update_admin_system_setting(
    section: _GenericMutableSectionName,
    body: AdminSystemUpdateRequest,
    context: Annotated[
        SystemAuditContext,
        Depends(current_model_admin_context),
    ],
    service: Annotated[
        SystemRuntimePolicyService,
        Depends(get_system_runtime_policy_service),
    ],
) -> AdminSystemUpdateResponse:
    if section not in _GENERIC_MUTABLE_SECTIONS:
        raise _http_exception(SystemRuntimePolicyInvalid(context.request_id))
    try:
        return _update_response(
            await service.update_policy(
                context,
                RuntimePolicySection(section),
                expected_revision=body.expected_revision,
                value=body.value,
            )
        )
    except SystemRuntimePolicyError as error:
        raise _http_exception(error) from None


__all__ = ["router"]
