"""Application availability DTOs layered over the shared Workflow registry."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import uuid
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator

from app.system_runtime_settings.models import LockedWorkflowRuntimePolicy
from app.system_runtime_settings.workflow_runtime import WorkflowRuntimeFacetReadinessV1
from app.workflows.runtime_policy import WORKFLOW_RUNTIME_MAX_AGGREGATE_GROUPS
from deerflow.workflows import StrictLiteralOne
from deerflow.workflows.catalog_contracts import (
    FIRST_BATCH_NODE_REGISTRY_V1,
    FIRST_BATCH_NODE_TITLES,
    WORKFLOW_NODE_REGISTRY_CONTRACT_VERSION,
    LocalizedNodeTitle,
    NodeTypeDefinition,
    PortDefinition,
    PortDerivationV1,
    ResolvedNodeInstancePortsV1,
    ResolvedWorkflowInstancePortsV1,
    first_batch_node_registry_manifest_checksum_v1,
    first_batch_node_registry_manifest_v1,
    resolve_workflow_instance_ports_v1,
    resolved_workflow_instance_ports_public_projection_v1,
    validate_node_config_v1,
)

type WorkflowNodeDisabledReasonCode = Literal[
    "WORKFLOW_DISABLED",
    "WORKFLOW_NODE_CAPABILITY_REQUIRED",
    "WORKFLOW_NODE_NOT_ALLOWED",
    "WORKFLOW_CODE_DISABLED",
    "WORKFLOW_CODE_PROFILE_UNAVAILABLE",
    "WORKFLOW_HTTP_DISABLED",
    "WORKFLOW_HTTP_PROFILE_UNAVAILABLE",
]
_Sha256Hex = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
_PublicBytes = Annotated[StrictInt, Field(ge=1, le=2_147_483_648)]
_PublicHttpResponseBytes = Annotated[StrictInt, Field(ge=1, le=2_097_152)]
_PublicMilliseconds = Annotated[StrictInt, Field(ge=1, le=31_536_000_000)]
_PublicLoopIterations = Annotated[StrictInt, Field(ge=1, le=1_000_000)]
_PublicAggregateGroups = Annotated[StrictInt, Field(ge=1, le=WORKFLOW_RUNTIME_MAX_AGGREGATE_GROUPS)]
_PublicAggregateCandidates = Annotated[StrictInt, Field(ge=1, le=100_000)]
_HttpAuthoringId = Annotated[
    StrictStr,
    Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9._-]*$"),
]
_HttpHeaderName = Annotated[
    StrictStr,
    Field(min_length=1, max_length=128, pattern=r"^[a-z0-9!#$%&'*+.^_`|~-]+$"),
]
_HttpOrigin = Annotated[StrictStr, Field(min_length=1, max_length=2_048)]
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_NON_CANONICAL_NUMERIC_HOST = re.compile(
    r"^(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*$",
    re.IGNORECASE,
)
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_TRANSPORT_CONTROLLED_HEADER_NAMES = frozenset(
    {
        "authentication-info",
        "connection",
        "content-length",
        "cookie",
        "forwarded",
        "host",
        "keep-alive",
        "set-cookie",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "www-authenticate",
    }
)
_FIRST_BATCH_NODE_REGISTRY_BY_ID = {(definition.type, definition.version): definition for definition in FIRST_BATCH_NODE_REGISTRY_V1}


def _freeze_catalog_array(value: object) -> object:
    if type(value) in {list, tuple}:
        return tuple(value)
    raise ValueError("Catalog collections must be JSON arrays or frozen tuples")


def _is_transport_controlled_header(value: str) -> bool:
    return value in _TRANSPORT_CONTROLLED_HEADER_NAMES or value.startswith("proxy-") or value.startswith("x-forwarded-")


def _effective_http_origin(value: str) -> str:
    """Collapse the optional default HTTPS port for authority comparisons."""

    parsed = urlsplit(value)
    hostname = parsed.hostname
    if hostname is None:
        raise ValueError("HTTP authoring origin is invalid")
    return f"https://{hostname}" if parsed.port in {None, 443} else f"https://{hostname}:{parsed.port}"


class _StrictCatalogModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
        serialize_by_alias=True,
    )


class NodeAvailability(_StrictCatalogModel):
    state: Literal["enabled", "disabled"]
    reason_code: WorkflowNodeDisabledReasonCode | None = None

    @model_validator(mode="after")
    def validate_enabled_reason(self) -> Self:
        if self.state == "enabled" and self.reason_code is not None:
            raise ValueError("enabled catalog entries cannot carry a disabled reason")
        if self.state == "disabled" and self.reason_code is None:
            raise ValueError("disabled catalog entries require a safe reason code")
        return self


class NodePublicLimits(_StrictCatalogModel):
    max_source_bytes: _PublicBytes | None = None
    max_timeout_ms: _PublicMilliseconds | None = None
    max_iterations: _PublicLoopIterations | None = None
    max_aggregate_groups: _PublicAggregateGroups | None = None
    max_aggregate_candidates: _PublicAggregateCandidates | None = None
    max_http_request_bytes: _PublicBytes | None = None
    max_http_response_bytes: _PublicHttpResponseBytes | None = None


class WorkflowHttpInjectionProfileAuthoringV1(_StrictCatalogModel):
    """Member-safe semantics for one allowed Credential injection profile."""

    id: _HttpAuthoringId
    scheme: Literal["bearer", "basic", "api_key"]
    target_header: _HttpHeaderName
    credential_payload_contract: Literal["bearer_token_v1", "basic_auth_v1", "api_key_v1"]

    @model_validator(mode="after")
    def validate_injection_semantics(self) -> Self:
        expected_contract = {
            "bearer": "bearer_token_v1",
            "basic": "basic_auth_v1",
            "api_key": "api_key_v1",
        }[self.scheme]
        if self.credential_payload_contract != expected_contract:
            raise ValueError("HTTP authoring injection scheme and payload contract differ")
        if self.scheme in {"bearer", "basic"} and self.target_header != "authorization":
            raise ValueError("Bearer and Basic authoring profiles must target Authorization")
        if self.scheme == "api_key" and self.target_header == "authorization":
            raise ValueError("API key authoring profiles must target a custom safe header")
        if _is_transport_controlled_header(self.target_header):
            raise ValueError("HTTP authoring injection targets a transport-controlled header")
        return self


class WorkflowHttpEndpointAuthoringV1(_StrictCatalogModel):
    """Member-safe authoring coordinates for one approved HTTP endpoint."""

    id: _HttpAuthoringId
    origin: _HttpOrigin
    allowed_methods: Annotated[
        tuple[Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"], ...],
        BeforeValidator(_freeze_catalog_array),
        Field(min_length=1, max_length=6),
    ]
    write_idempotency: Literal["none", "server_derived_key"]
    injection_profiles: Annotated[
        tuple[WorkflowHttpInjectionProfileAuthoringV1, ...],
        BeforeValidator(_freeze_catalog_array),
        Field(max_length=32),
    ]

    @field_validator("origin")
    @classmethod
    def validate_fixed_https_origin(cls, value: str) -> str:
        if not value.isascii() or "\\" in value or "%" in value:
            raise ValueError("HTTP authoring origin must use canonical ASCII host syntax")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("HTTP authoring origin is invalid") from exc
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("HTTP authoring origin must use HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("HTTP authoring origin cannot contain user information")
        if parsed.netloc != parsed.netloc.lower():
            raise ValueError("HTTP authoring origin host syntax must be lowercase")
        if parsed.path or parsed.query or parsed.fragment:
            raise ValueError("HTTP authoring origin cannot contain a path, query or fragment")
        authority = parsed.netloc
        if authority.startswith("["):
            raise ValueError("HTTP authoring origin cannot use an IP literal")
        raw_hostname = authority
        if ":" in authority:
            raw_hostname, _, raw_port = authority.rpartition(":")
            if not raw_hostname or ":" in raw_hostname or not raw_port.isdigit() or str(port) != raw_port:
                raise ValueError("HTTP authoring origin must use a canonical port")
        hostname = parsed.hostname.rstrip(".").lower()
        if raw_hostname != hostname or parsed.hostname != hostname:
            raise ValueError("HTTP authoring origin must use canonical lowercase host syntax")
        if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith((".localhost", ".local", ".internal")):
            raise ValueError("HTTP authoring origin is not public")
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            raise ValueError("HTTP authoring origin cannot use an IP literal")
        if _NON_CANONICAL_NUMERIC_HOST.fullmatch(hostname):
            raise ValueError("HTTP authoring origin cannot use a numeric host")
        labels = hostname.split(".")
        if len(hostname) > 253 or len(labels) < 2 or any(_DNS_LABEL.fullmatch(label) is None for label in labels):
            raise ValueError("HTTP authoring origin must use a canonical DNS hostname")
        if port is not None and not 1 <= port <= 65_535:
            raise ValueError("HTTP authoring origin port is invalid")
        return value

    @model_validator(mode="after")
    def validate_canonical_collections(self) -> Self:
        method_order = {method: index for index, method in enumerate(("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"))}
        if len(self.allowed_methods) != len(set(self.allowed_methods)) or self.allowed_methods != tuple(sorted(self.allowed_methods, key=method_order.__getitem__)):
            raise ValueError("HTTP authoring methods must be unique and use canonical order")
        profile_ids = [profile.id for profile in self.injection_profiles]
        if len(profile_ids) != len(set(profile_ids)) or profile_ids != sorted(profile_ids):
            raise ValueError("HTTP authoring profiles must be unique and use canonical id order")
        return self


class WorkflowHttpAuthoringV1(_StrictCatalogModel):
    endpoints: Annotated[
        tuple[WorkflowHttpEndpointAuthoringV1, ...],
        BeforeValidator(_freeze_catalog_array),
        Field(max_length=64),
    ]

    @model_validator(mode="after")
    def validate_canonical_endpoints(self) -> Self:
        endpoint_ids = [endpoint.id for endpoint in self.endpoints]
        if len(endpoint_ids) != len(set(endpoint_ids)) or endpoint_ids != sorted(endpoint_ids):
            raise ValueError("HTTP authoring endpoints must be unique and use canonical id order")
        coordinates = [(_effective_http_origin(endpoint.origin), method) for endpoint in self.endpoints for method in endpoint.allowed_methods]
        if len(coordinates) != len(set(coordinates)):
            raise ValueError("HTTP authoring endpoint origin and method coordinates must be unique")
        return self


class NodeCatalogEntry(_StrictCatalogModel):
    definition: NodeTypeDefinition
    availability: NodeAvailability
    public_limits: NodePublicLimits | None = None
    http_authoring: WorkflowHttpAuthoringV1 | None = None

    @model_validator(mode="after")
    def validate_public_limit_scope(self) -> Self:
        expected_definition = _FIRST_BATCH_NODE_REGISTRY_BY_ID.get((self.definition.type, self.definition.version))
        if expected_definition is None or self.definition != expected_definition:
            raise ValueError("catalog entries must use the exact canonical node registry definition")
        if self.definition.type != "http_request" and self.http_authoring is not None:
            raise ValueError("only the HTTP Request catalog entry may expose HTTP authoring options")
        if self.public_limits is None:
            return self
        values = self.public_limits.model_dump()
        allowed_fields = {
            "python_code": {"max_source_bytes", "max_timeout_ms"},
            "loop": {"max_iterations", "max_timeout_ms"},
            "variable_aggregate": {"max_aggregate_groups", "max_aggregate_candidates", "max_timeout_ms"},
            "http_request": {
                "max_timeout_ms",
                "max_http_request_bytes",
                "max_http_response_bytes",
            },
        }.get(self.definition.type, {"max_timeout_ms"})
        populated = {field for field, value in values.items() if value is not None}
        if not populated <= allowed_fields:
            raise ValueError("catalog entry exposes a public limit that does not apply to this node type")
        return self


class NodeCatalogResponseV1(_StrictCatalogModel):
    schema_version: StrictLiteralOne
    catalog_generation: _Sha256Hex
    availability_generation: _Sha256Hex
    entries: Annotated[tuple[NodeCatalogEntry, ...], Field(min_length=9, max_length=9)]

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_integer_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Node Catalog schema_version must be an integer")
        return value

    @field_validator("entries", mode="before")
    @classmethod
    def freeze_entries(cls, value: object) -> object:
        if type(value) is list:
            return tuple(value)
        if type(value) is tuple:
            return value
        raise ValueError("Node Catalog entries must be a JSON array or frozen tuple")

    @model_validator(mode="after")
    def validate_canonical_entries(self) -> Self:
        identities = [(entry.definition.type, entry.definition.version) for entry in self.entries]
        expected = [(definition.type, definition.version) for definition in FIRST_BATCH_NODE_REGISTRY_V1]
        if identities != expected:
            raise ValueError("Node Catalog must contain the exact canonical first-batch registry")
        return self


class WorkflowCatalogCapabilityProjectionV1(_StrictCatalogModel):
    """Server-derived safe capability flags used only for Catalog projection."""

    code_use: StrictBool
    http_use: StrictBool


def derive_catalog_generation(
    *,
    registry_contract_version: int,
    policy_version_id: uuid.UUID,
    policy_revision: int,
    policy_checksum: str,
) -> str:
    """Derive a public opaque Catalog generation from exact server authority."""

    if type(registry_contract_version) is not int or registry_contract_version != WORKFLOW_NODE_REGISTRY_CONTRACT_VERSION:
        raise ValueError("unknown Workflow Node Registry contract version")
    if type(policy_version_id) is not uuid.UUID:
        raise TypeError("policy_version_id must be a UUID")
    if type(policy_revision) is not int or policy_revision < 1:
        raise ValueError("policy_revision must be a positive integer")
    if not isinstance(policy_checksum, str) or _SHA256_PATTERN.fullmatch(policy_checksum) is None:
        raise ValueError("policy_checksum must be a lowercase SHA-256 digest")
    canonical = json.dumps(
        [
            "actweave.workflow.node-catalog.v1",
            registry_contract_version,
            first_batch_node_registry_manifest_checksum_v1(),
            str(policy_version_id),
            policy_revision,
            policy_checksum,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def derive_availability_generation(*, catalog_generation: str, readiness_generation: str) -> str:
    """Derive dynamic availability from safe booleans/allowlists only.

    ``catalog_generation`` is validated as the paired response generation but
    is deliberately not hashed here: it contains the opaque exact policy
    identity.  A policy identity change is already represented by that sibling
    field and must not make Worker/policy IDs indirect inputs to availability.
    """

    for name, value in (
        ("catalog_generation", catalog_generation),
        ("readiness_generation", readiness_generation),
    ):
        if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    canonical = json.dumps(
        ["actweave.workflow.node-availability.v1", readiness_generation],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _derive_safe_readiness_generation(
    *,
    locked: LockedWorkflowRuntimePolicy,
    capabilities: WorkflowCatalogCapabilityProjectionV1,
    facets: WorkflowRuntimeFacetReadinessV1,
) -> str:
    """Hash only public booleans, allowlist and capability projection.

    Exact policy identity belongs to ``catalog_generation``.  Dynamic
    availability never hashes Worker IDs, heartbeats, profile/provider names,
    policy UUIDs, endpoints or infrastructure locators.
    """

    policy = locked.value
    canonical = json.dumps(
        [
            "actweave.workflow.node-availability-input.v1",
            policy.enabled,
            policy.admission_enabled,
            [[entry.type, list(entry.versions)] for entry in policy.catalog.allowed_type_versions],
            policy.code.enabled,
            policy.http.enabled,
            capabilities.code_use,
            capabilities.http_use,
            facets.generic_ready,
            facets.code_ready,
            facets.http_ready,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _public_limits_for_node(
    node_type: str,
    *,
    locked: LockedWorkflowRuntimePolicy,
) -> NodePublicLimits:
    policy = locked.value
    values: dict[str, int] = {
        "max_timeout_ms": policy.execution_limits.max_node_timeout_ms,
    }
    if node_type == "python_code":
        values.update(
            {
                "max_source_bytes": policy.code.hard_limits.max_source_bytes,
                "max_timeout_ms": min(
                    policy.execution_limits.max_node_timeout_ms,
                    policy.code.hard_limits.wall_timeout_ms,
                ),
            }
        )
    elif node_type == "http_request":
        values.update(
            {
                "max_timeout_ms": min(
                    policy.execution_limits.max_node_timeout_ms,
                    policy.http.transport.total_timeout_ms,
                ),
                "max_http_request_bytes": min(
                    policy.execution_limits.max_http_request_bytes,
                    policy.http.transport.max_request_bytes,
                ),
                "max_http_response_bytes": min(
                    policy.execution_limits.max_http_response_bytes,
                    policy.http.transport.max_decompressed_response_bytes,
                ),
            }
        )
    elif node_type == "loop":
        values["max_iterations"] = policy.graph_limits.max_loop_iterations
    elif node_type == "variable_aggregate":
        values.update(
            {
                "max_aggregate_groups": policy.graph_limits.max_aggregate_groups,
                "max_aggregate_candidates": policy.graph_limits.max_aggregate_candidates,
            }
        )
    return NodePublicLimits.model_validate(values)


def _http_authoring_for_policy(*, locked: LockedWorkflowRuntimePolicy) -> WorkflowHttpAuthoringV1:
    """Project only the closed member-safe subset of the exact locked policy."""

    policy = locked.value
    profiles_by_id = {profile.id: profile for profile in policy.http.injection_profiles}
    write_methods = frozenset({"POST", "PUT", "PATCH", "DELETE"})
    endpoint_methods = {endpoint.id: tuple(method for method in endpoint.allowed_methods if policy.http.write_enabled or method not in write_methods) for endpoint in policy.http.endpoint_policies}
    coordinate_counts: dict[tuple[str, str], int] = {}
    for endpoint in policy.http.endpoint_policies:
        effective_origin = _effective_http_origin(endpoint.origin)
        for method in endpoint_methods[endpoint.id]:
            coordinate = (effective_origin, method)
            coordinate_counts[coordinate] = coordinate_counts.get(coordinate, 0) + 1
    return WorkflowHttpAuthoringV1.model_validate(
        {
            "endpoints": [
                {
                    "id": endpoint.id,
                    "origin": endpoint.origin,
                    "allowed_methods": [method for method in endpoint_methods[endpoint.id] if coordinate_counts[(_effective_http_origin(endpoint.origin), method)] == 1],
                    "write_idempotency": (endpoint.write_idempotency if any(method in write_methods and coordinate_counts[(_effective_http_origin(endpoint.origin), method)] == 1 for method in endpoint_methods[endpoint.id]) else "none"),
                    "injection_profiles": [
                        {
                            "id": profiles_by_id[profile_id].id,
                            "scheme": profiles_by_id[profile_id].scheme,
                            "target_header": profiles_by_id[profile_id].target_header,
                            "credential_payload_contract": profiles_by_id[profile_id].credential_payload_contract,
                        }
                        for profile_id in endpoint.injection_profile_ids
                    ],
                }
                for endpoint in policy.http.endpoint_policies
                if any(coordinate_counts[(_effective_http_origin(endpoint.origin), method)] == 1 for method in endpoint_methods[endpoint.id])
            ]
        }
    )


def _availability_for_node(
    node_type: str,
    *,
    locked: LockedWorkflowRuntimePolicy,
    capabilities: WorkflowCatalogCapabilityProjectionV1,
    facets: WorkflowRuntimeFacetReadinessV1,
) -> NodeAvailability:
    policy = locked.value
    allowed = {(entry.type, version) for entry in policy.catalog.allowed_type_versions for version in entry.versions}
    reason: WorkflowNodeDisabledReasonCode | None = None
    if not policy.enabled:
        reason = "WORKFLOW_DISABLED"
    elif node_type == "python_code" and not capabilities.code_use:
        reason = "WORKFLOW_NODE_CAPABILITY_REQUIRED"
    elif node_type == "http_request" and not capabilities.http_use:
        reason = "WORKFLOW_NODE_CAPABILITY_REQUIRED"
    elif (node_type, 1) not in allowed:
        reason = "WORKFLOW_NODE_NOT_ALLOWED"
    elif node_type == "python_code" and not policy.code.enabled:
        reason = "WORKFLOW_CODE_DISABLED"
    elif node_type == "http_request" and not policy.http.enabled:
        reason = "WORKFLOW_HTTP_DISABLED"
    elif node_type == "python_code" and not facets.code_ready:
        reason = "WORKFLOW_CODE_PROFILE_UNAVAILABLE"
    elif node_type == "http_request" and not facets.http_ready:
        reason = "WORKFLOW_HTTP_PROFILE_UNAVAILABLE"
    if reason is None:
        return NodeAvailability(state="enabled")
    return NodeAvailability(state="disabled", reason_code=reason)


def build_project_node_catalog_v1(
    *,
    locked: LockedWorkflowRuntimePolicy,
    capabilities: WorkflowCatalogCapabilityProjectionV1,
    facets: WorkflowRuntimeFacetReadinessV1,
) -> NodeCatalogResponseV1:
    """Build the exact nine-entry, secret-free project Node Catalog."""

    if type(locked) is not LockedWorkflowRuntimePolicy:
        raise TypeError("Node Catalog requires an exact locked Workflow policy")
    if type(capabilities) is not WorkflowCatalogCapabilityProjectionV1:
        raise TypeError("Node Catalog requires a server capability projection")
    if type(facets) is not WorkflowRuntimeFacetReadinessV1:
        raise TypeError("Node Catalog requires exact runtime facets")
    catalog_generation = derive_catalog_generation(
        registry_contract_version=WORKFLOW_NODE_REGISTRY_CONTRACT_VERSION,
        policy_version_id=locked.policy_version_id,
        policy_revision=locked.revision,
        policy_checksum=locked.payload_checksum,
    )
    readiness_generation = _derive_safe_readiness_generation(
        locked=locked,
        capabilities=capabilities,
        facets=facets,
    )
    manifest = first_batch_node_registry_manifest_v1()
    entries = [
        {
            "definition": manifest[index],
            "availability": _availability_for_node(
                definition.type,
                locked=locked,
                capabilities=capabilities,
                facets=facets,
            ).model_dump(mode="json", exclude_unset=True),
            "public_limits": _public_limits_for_node(
                definition.type,
                locked=locked,
            ).model_dump(mode="json", exclude_unset=True),
            **(
                {
                    "http_authoring": _http_authoring_for_policy(
                        locked=locked,
                    ).model_dump(mode="json", exclude_unset=True)
                }
                if definition.type == "http_request"
                else {}
            ),
        }
        for index, definition in enumerate(FIRST_BATCH_NODE_REGISTRY_V1)
    ]
    return NodeCatalogResponseV1.model_validate(
        {
            "schema_version": 1,
            "catalog_generation": catalog_generation,
            "availability_generation": derive_availability_generation(
                catalog_generation=catalog_generation,
                readiness_generation=readiness_generation,
            ),
            "entries": entries,
        }
    )


def node_catalog_response_public_projection_v1(
    value: NodeCatalogResponseV1,
) -> dict[str, object]:
    """Return the canonical JSON transport shape without invented nulls.

    Catalog models deliberately revalidate instances to catch nested mutable
    drift.  FastAPI response-model validation must therefore consume this
    JSON projection instead of feeding an already-built model back through
    Pydantic's Python-mode field-name/default expansion.
    """

    if type(value) is not NodeCatalogResponseV1:
        raise TypeError("a validated Node Catalog response is required")
    return value.model_dump(
        mode="json",
        by_alias=True,
        exclude_unset=True,
    )


__all__ = [
    "FIRST_BATCH_NODE_REGISTRY_V1",
    "FIRST_BATCH_NODE_TITLES",
    "WORKFLOW_NODE_REGISTRY_CONTRACT_VERSION",
    "LocalizedNodeTitle",
    "NodeAvailability",
    "NodeCatalogEntry",
    "NodeCatalogResponseV1",
    "NodePublicLimits",
    "NodeTypeDefinition",
    "PortDerivationV1",
    "PortDefinition",
    "ResolvedNodeInstancePortsV1",
    "ResolvedWorkflowInstancePortsV1",
    "WorkflowCatalogCapabilityProjectionV1",
    "WorkflowHttpAuthoringV1",
    "WorkflowHttpEndpointAuthoringV1",
    "WorkflowHttpInjectionProfileAuthoringV1",
    "WorkflowNodeDisabledReasonCode",
    "build_project_node_catalog_v1",
    "derive_availability_generation",
    "derive_catalog_generation",
    "first_batch_node_registry_manifest_v1",
    "node_catalog_response_public_projection_v1",
    "first_batch_node_registry_manifest_checksum_v1",
    "resolve_workflow_instance_ports_v1",
    "resolved_workflow_instance_ports_public_projection_v1",
    "validate_node_config_v1",
]
