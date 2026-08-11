from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from pydantic import ValidationError

from app.audit.models import resolve_system_audit_context
from app.gateway.routers.admin_system_settings import (
    AdminSystemUpdateRequest,
    router,
    update_admin_system_setting,
)
from app.system_runtime_settings.errors import SystemRuntimePolicyInvalid
from app.system_runtime_settings.models import (
    LockedWorkflowRuntimePolicy,
    RuntimePolicySection,
    default_policy_value,
)
from app.system_runtime_settings.service import SystemRuntimePolicyService
from app.system_runtime_settings.workflow_runtime import (
    WORKFLOW_RUN_HANDLER_INSTALLED,
    WorkflowRuntimeConvergence,
    WorkflowRuntimeMaterializedIdentity,
)
from app.workflows.runtime_policy import (
    WorkflowRuntimePolicyUpdateRequestV1,
    WorkflowRuntimePolicyUpdateResponseV1,
    WorkflowRuntimePolicyV1,
    workflow_runtime_policy_checksum,
)


def _identity(*, revision: int = 7, checksum: str = "a" * 64) -> WorkflowRuntimeMaterializedIdentity:
    return WorkflowRuntimeMaterializedIdentity(
        policy_version_id=uuid.UUID("53f5a2b9-1c63-43ec-92d4-2aa799f18857"),
        revision=revision,
        schema_version=1,
        payload_checksum=checksum,
    )


def test_workflow_handler_capability_is_code_closed_until_g32() -> None:
    assert WORKFLOW_RUN_HANDLER_INSTALLED is False


def test_materialized_identity_is_strict_safe_and_changes_with_exact_policy() -> None:
    identity = _identity()

    assert identity.revision == 7
    assert identity.payload_checksum == "a" * 64
    assert identity != _identity(revision=8)
    assert identity != _identity(checksum="b" * 64)

    with pytest.raises((TypeError, ValueError, ValidationError)):
        WorkflowRuntimeMaterializedIdentity(  # type: ignore[arg-type]
            policy_version_id=str(identity.policy_version_id),
            revision=7,
            schema_version=1,
            payload_checksum="a" * 64,
        )
    with pytest.raises((TypeError, ValueError, ValidationError)):
        WorkflowRuntimeMaterializedIdentity(
            policy_version_id=identity.policy_version_id,
            revision=True,  # type: ignore[arg-type]
            schema_version=1,
            payload_checksum="a" * 64,
        )


def test_convergence_has_no_process_local_role_materialization_fallback() -> None:
    with pytest.raises(TypeError):
        WorkflowRuntimeConvergence(role_materializations=object())  # type: ignore[call-arg]


def test_locked_workflow_policy_identity_is_not_derived_from_ambient_config() -> None:
    value = default_policy_value(RuntimePolicySection.WORKFLOW_RUNTIME)
    assert isinstance(value, WorkflowRuntimePolicyV1)
    locked = LockedWorkflowRuntimePolicy.create(
        policy_version_id=uuid.UUID("53f5a2b9-1c63-43ec-92d4-2aa799f18857"),
        revision=7,
        schema_version=1,
        payload_checksum=workflow_runtime_policy_checksum(value),
        value=value,
    )

    assert WorkflowRuntimeMaterializedIdentity.from_locked(locked) == WorkflowRuntimeMaterializedIdentity(
        policy_version_id=locked.policy_version_id,
        revision=7,
        schema_version=1,
        payload_checksum=locked.payload_checksum,
    )
    assert locked.value == value
    assert locked.value is not value

    with pytest.raises(TypeError):
        LockedWorkflowRuntimePolicy(  # type: ignore[call-arg]
            policy_version_id=locked.policy_version_id,
            revision=locked.revision,
            schema_version=locked.schema_version,
            payload_checksum=locked.payload_checksum,
            value=value,
        )
    with pytest.raises(ValueError, match="identity does not match"):
        LockedWorkflowRuntimePolicy.create(
            policy_version_id=locked.policy_version_id,
            revision=locked.revision,
            schema_version=locked.schema_version,
            payload_checksum="f" * 64,
            value=value,
        )


def test_admin_put_workflow_runtime_uses_the_closed_typed_contract_before_generic_route() -> None:
    routes = [route for route in router.routes if isinstance(route, APIRoute)]
    exact = next(route for route in routes if route.path.endswith("/workflow_runtime"))
    generic = next(route for route in routes if route.path.endswith("/{section}"))

    assert routes.index(exact) < routes.index(generic)
    assert exact.body_field is not None
    assert exact.body_field.field_info.annotation is WorkflowRuntimePolicyUpdateRequestV1
    assert exact.response_model is WorkflowRuntimePolicyUpdateResponseV1
    assert generic.dependant.path_params[0].field_info.annotation.__args__ == (
        "agent_runtime",
        "auth",
        "memory_document",
        "quotas",
    )


@pytest.mark.anyio
async def test_generic_admin_handler_explicitly_rejects_workflow_runtime() -> None:
    with pytest.raises(HTTPException) as error:
        await update_admin_system_setting(
            section="workflow_runtime",  # type: ignore[arg-type]
            body=AdminSystemUpdateRequest(expected_revision=1, value={}),
            context=SimpleNamespace(request_id="workflow-g11-route-negative"),  # type: ignore[arg-type]
            service=None,  # type: ignore[arg-type]
        )

    assert error.value.status_code == 422
    assert error.value.detail == {
        "code": "system_runtime_policy_invalid",
        "message": "System runtime policy invalid",
        "request_id": "workflow-g11-route-negative",
    }


@pytest.mark.anyio
async def test_generic_service_update_cannot_bypass_the_closed_workflow_contract() -> None:
    context = resolve_system_audit_context(
        SimpleNamespace(
            id=uuid.UUID("08b4fbf2-6f37-41bf-baa6-01493872ac03"),
            system_role="system_admin",
        ),
        request_id="workflow-g11-service-negative",
    )
    service = SystemRuntimePolicyService(  # type: ignore[arg-type]
        lambda: None,
        object(),
    )

    with pytest.raises(SystemRuntimePolicyInvalid):
        await service.update_policy(
            context,
            RuntimePolicySection.WORKFLOW_RUNTIME,
            expected_revision=1,
            value=default_policy_value(RuntimePolicySection.WORKFLOW_RUNTIME),
        )
