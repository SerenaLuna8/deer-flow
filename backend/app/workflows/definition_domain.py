"""Pure Definition/Draft/Publish derivation and immutable port values."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from app.system_runtime_settings.models import LockedWorkflowRuntimePolicy
from app.system_runtime_settings.workflow_runtime import WorkflowRuntimeFacetReadinessV1
from app.workflows.definition_contracts import (
    WorkflowDraftCanvasV1,
    WorkflowDraftSpecV1,
    WorkflowPublishedCodeRequirementV1,
    WorkflowPublishedCredentialSlotV1,
    WorkflowPublishedHttpRequirementV1,
    WorkflowPublishedModelRefV1,
    WorkflowPublishedRequirementsV1,
    _trusted_definition_contract_validate,
    workflow_draft_canvas_public_projection_v1,
    workflow_draft_spec_public_projection_v1,
)
from app.workflows.runtime_policy import WorkflowRuntimePolicyV1
from deerflow.workflows import CanvasDocumentV1, WorkflowSpecV1, canonical_json_value
from deerflow.workflows.catalog_contracts import FIRST_BATCH_NODE_REGISTRY_V1
from deerflow.workflows.compiler import WorkflowIR
from deerflow.workflows.json_schema import validate_strict_json_schema


def _json_object(value: object, *, label: str) -> dict[str, object]:
    if type(value) is WorkflowDraftSpecV1:
        return workflow_draft_spec_public_projection_v1(value)
    if type(value) is WorkflowDraftCanvasV1:
        return workflow_draft_canvas_public_projection_v1(value)
    if isinstance(value, Mapping):
        result = dict(value)
        canonical_json_value(result)  # portable JSON and canonical key checks
        return result
    raise TypeError(f"{label} must be a validated Draft document or JSON object")


def _sha256_json(value: dict[str, object]) -> str:
    return hashlib.sha256(canonical_json_value(value).encode("utf-8")).hexdigest()


def canonical_workflow_draft_checksum_v1(
    *,
    spec: WorkflowDraftSpecV1 | Mapping[str, object],
    canvas: WorkflowDraftCanvasV1 | Mapping[str, object],
) -> str:
    """Hash the exact persisted Draft material, including Canvas.

    This checksum is a CAS identity, not a semantic checksum.  The semantic
    checksum is derived only after a complete Spec passes the G12 compiler.
    """

    return _sha256_json(
        {
            "contract": "actweave.workflow.draft.v1",
            "spec": _json_object(spec, label="spec"),
            "canvas": _json_object(canvas, label="canvas"),
        }
    )


def canonical_workflow_slot_schema_checksum_v1(
    payload_schema: Mapping[str, object],
) -> str:
    schema = dict(payload_schema)
    validate_strict_json_schema(schema)  # type: ignore[arg-type]
    return hashlib.sha256(canonical_json_value(schema).encode("utf-8")).hexdigest()


def canonical_workflow_publish_request_digest_v1(
    *,
    expected_revision: int,
    expected_draft_checksum: str,
) -> str:
    if type(expected_revision) is not int or expected_revision < 1:
        raise ValueError("publish expected revision must be positive")
    if not isinstance(expected_draft_checksum, str) or len(expected_draft_checksum) != 64:
        raise ValueError("publish expected Draft checksum is invalid")
    return _sha256_json(
        {
            "contract": "actweave.workflow.publish-request.v1",
            "expected_revision": expected_revision,
            "expected_draft_checksum": expected_draft_checksum,
        }
    )


def canonical_workflow_control_request_digest_v1(
    *,
    operation: str,
    project_id: uuid.UUID,
    request: Mapping[str, object],
    workflow_id: uuid.UUID | None = None,
    version_id: uuid.UUID | None = None,
    slot_id: str | None = None,
) -> str:
    """Hash one strict public control request plus its exact route scope."""

    operations = {
        "create",
        "update",
        "save_draft",
        "archive",
        "publish",
        "draft_grant_put",
        "draft_grant_delete",
        "version_grant_put",
        "version_grant_delete",
    }
    if operation not in operations:
        raise ValueError("unsupported Workflow control operation")
    # asyncpg returns a UUID subclass for native PostgreSQL UUID columns. It is
    # still the trusted UUID value type; strings and other caller values remain
    # rejected before canonical serialization.
    if not isinstance(project_id, uuid.UUID):
        raise TypeError("project_id must be a UUID")
    if workflow_id is not None and not isinstance(workflow_id, uuid.UUID):
        raise TypeError("workflow_id must be a UUID")
    if version_id is not None and not isinstance(version_id, uuid.UUID):
        raise TypeError("version_id must be a UUID")
    if slot_id is not None and (not isinstance(slot_id, str) or not slot_id):
        raise ValueError("slot_id must be a non-empty string")
    body = dict(request)
    canonical_json_value(body)
    return _sha256_json(
        {
            "contract": "actweave.workflow.control-request.v1",
            "operation": operation,
            "project_id": str(project_id),
            "workflow_id": None if workflow_id is None else str(workflow_id),
            "version_id": None if version_id is None else str(version_id),
            "slot_id": slot_id,
            "request": body,
        }
    )


@dataclass(frozen=True, slots=True)
class WorkflowDefinitionAuthoritySnapshot:
    locked_policy: LockedWorkflowRuntimePolicy
    facets: WorkflowRuntimeFacetReadinessV1

    def __post_init__(self) -> None:
        if type(self.locked_policy) is not LockedWorkflowRuntimePolicy:
            raise TypeError("Definition authority requires an exact locked policy")
        if type(self.facets) is not WorkflowRuntimeFacetReadinessV1:
            raise TypeError("Definition authority requires exact runtime facets")


@dataclass(frozen=True, slots=True)
class WorkflowDefinitionValidationArtifact:
    spec: WorkflowSpecV1
    canvas: CanvasDocumentV1
    ir: WorkflowIR
    requirements: WorkflowPublishedRequirementsV1
    catalog_generation: str
    policy_revision: int

    def __post_init__(self) -> None:
        if type(self.spec) is not WorkflowSpecV1:
            raise TypeError("validation artifact requires an exact WorkflowSpecV1")
        if type(self.canvas) is not CanvasDocumentV1:
            raise TypeError("validation artifact requires an exact CanvasDocumentV1")
        if type(self.ir) is not WorkflowIR:
            raise TypeError("validation artifact requires an exact WorkflowIR")
        if type(self.requirements) is not WorkflowPublishedRequirementsV1:
            raise TypeError("validation artifact requires exact published requirements")
        if not isinstance(self.catalog_generation, str) or len(self.catalog_generation) != 64:
            raise ValueError("validation artifact catalog generation is invalid")
        if type(self.policy_revision) is not int or self.policy_revision < 1:
            raise ValueError("validation artifact policy revision is invalid")


class WorkflowDefinitionDependencyError(ValueError):
    """One safe, stable publish-time dependency failure."""

    def __init__(
        self,
        code: str,
        *,
        node_id: str | None = None,
        port_id: str | None = None,
    ) -> None:
        self.code = code
        self.node_id = node_id
        self.port_id = port_id
        super().__init__(code)


_CREDENTIAL_CONTRACT_SCHEMAS: dict[str, dict[str, object]] = {
    "bearer_token_v1": {
        "type": "object",
        "properties": {"token": {"type": "string"}},
        "required": ["token"],
        "additionalProperties": False,
    },
    "basic_auth_v1": {
        "type": "object",
        "properties": {
            "username": {"type": "string"},
            "password": {"type": "string"},
        },
        "required": ["username", "password"],
        "additionalProperties": False,
    },
    "api_key_v1": {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    },
}


def _http_requirement(
    *,
    node: object,
    policy: WorkflowRuntimePolicyV1,
) -> tuple[WorkflowPublishedHttpRequirementV1, str | None]:
    config = node.config
    matches = tuple(endpoint for endpoint in policy.http.endpoint_policies if endpoint.origin == config.base_origin and config.method in endpoint.allowed_methods)
    if len(matches) != 1:
        raise WorkflowDefinitionDependencyError(
            "WORKFLOW_HTTP_ENDPOINT_FORBIDDEN",
            node_id=node.id,
        )
    endpoint = matches[0]
    injection_profile_id: str | None = None
    credential_slot_id: str | None = None
    credential_contract: str | None = None
    if config.auth.mode == "endpoint_profile":
        injection_profile_id = config.auth.injection_profile_id
        credential_slot_id = config.auth.credential_slot_id
        if injection_profile_id not in endpoint.injection_profile_ids:
            raise WorkflowDefinitionDependencyError(
                "WORKFLOW_HTTP_ENDPOINT_FORBIDDEN",
                node_id=node.id,
            )
        profiles = tuple(profile for profile in policy.http.injection_profiles if profile.id == injection_profile_id)
        if len(profiles) != 1:
            raise WorkflowDefinitionDependencyError(
                "WORKFLOW_HTTP_ENDPOINT_FORBIDDEN",
                node_id=node.id,
            )
        credential_contract = profiles[0].credential_payload_contract
    return (
        WorkflowPublishedHttpRequirementV1(
            node_id=uuid.UUID(node.id),
            method=config.method,
            endpoint_policy_id=endpoint.id,
            injection_profile_id=injection_profile_id,
            credential_slot_id=credential_slot_id,
        ),
        credential_contract,
    )


def derive_workflow_published_requirements_v1(
    *,
    spec: WorkflowSpecV1,
    policy: WorkflowRuntimePolicyV1,
) -> WorkflowPublishedRequirementsV1:
    if type(spec) is not WorkflowSpecV1:
        raise TypeError("published requirements require an exact WorkflowSpecV1")
    if type(policy) is not WorkflowRuntimePolicyV1:
        raise TypeError("published requirements require an exact runtime policy")

    model_refs = tuple(
        WorkflowPublishedModelRefV1(
            node_id=uuid.UUID(node.id),
            purpose="primary",
            logical_model_name=node.config.model_ref,
        )
        for node in sorted(spec.nodes, key=lambda item: item.id)
        if node.type == "llm"
    )
    code = tuple(
        WorkflowPublishedCodeRequirementV1(
            node_id=uuid.UUID(node.id),
            runtime_contract=policy.code.runtime_contract,
        )
        for node in sorted(spec.nodes, key=lambda item: item.id)
        if node.type == "python_code"
    )
    http_requirements: list[WorkflowPublishedHttpRequirementV1] = []
    slot_contracts: dict[str, str] = {}
    for node in sorted(spec.nodes, key=lambda item: item.id):
        if node.type != "http_request":
            continue
        requirement, credential_contract = _http_requirement(
            node=node,
            policy=policy,
        )
        http_requirements.append(requirement)
        if requirement.credential_slot_id is not None and credential_contract is not None:
            previous = slot_contracts.setdefault(
                requirement.credential_slot_id,
                credential_contract,
            )
            if previous != credential_contract:
                raise WorkflowDefinitionDependencyError(
                    "WORKFLOW_CREDENTIAL_SLOT_SCHEMA_INVALID",
                    node_id=node.id,
                    port_id=requirement.credential_slot_id,
                )

    credential_slots: list[WorkflowPublishedCredentialSlotV1] = []
    for slot in sorted(spec.credential_slots, key=lambda item: item.id):
        try:
            checksum = canonical_workflow_slot_schema_checksum_v1(slot.payload_schema)
        except (TypeError, ValueError):
            raise WorkflowDefinitionDependencyError(
                "WORKFLOW_CREDENTIAL_SLOT_SCHEMA_INVALID",
                port_id=slot.id,
            ) from None
        contract = slot_contracts.get(slot.id)
        if contract is not None:
            expected = _CREDENTIAL_CONTRACT_SCHEMAS[contract]
            if canonical_json_value(slot.payload_schema) != canonical_json_value(expected):
                raise WorkflowDefinitionDependencyError(
                    "WORKFLOW_CREDENTIAL_SLOT_SCHEMA_INVALID",
                    port_id=slot.id,
                )
        credential_slots.append(
            WorkflowPublishedCredentialSlotV1(
                slot_id=slot.id,
                name=slot.name,
                purpose=slot.purpose,
                payload_schema=slot.payload_schema,
                payload_schema_checksum=checksum,
                required=True,
            )
        )

    present_types = {node.type for node in spec.nodes}
    node_types = tuple(definition.type for definition in FIRST_BATCH_NODE_REGISTRY_V1 if definition.type in present_types)
    http = tuple(http_requirements)
    write_methods = {"POST", "PUT", "PATCH", "DELETE"}
    result = _trusted_definition_contract_validate(
        WorkflowPublishedRequirementsV1,
        {
            "node_types": node_types,
            "model_refs": model_refs,
            "code": code,
            "http": http,
            "credential_slots": tuple(credential_slots),
            "requires_code": bool(code),
            "requires_http": bool(http),
            "requires_http_write": any(item.method in write_methods for item in http),
        },
    )
    if type(result) is not WorkflowPublishedRequirementsV1:  # pragma: no cover
        raise AssertionError("trusted requirements factory returned wrong type")
    return result


__all__ = [
    "WorkflowDefinitionAuthoritySnapshot",
    "WorkflowDefinitionDependencyError",
    "WorkflowDefinitionValidationArtifact",
    "canonical_workflow_draft_checksum_v1",
    "canonical_workflow_control_request_digest_v1",
    "canonical_workflow_publish_request_digest_v1",
    "canonical_workflow_slot_schema_checksum_v1",
    "derive_workflow_published_requirements_v1",
]
