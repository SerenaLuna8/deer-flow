"""Strict, secret-free Workflow runtime policy value contracts.

The models in this module describe the single atomic ``workflow_runtime``
value that will later be stored in PostgreSQL System Settings.  They do not
read configuration files, process environment, deployment locators, or the
existing Agent runtime-policy catalog.  Every configurable value is required
so a missing field cannot silently acquire a process-local fallback.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import uuid
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationInfo,
    field_validator,
    model_validator,
)

from deerflow.workflows import WORKFLOW_NODE_KINDS, WorkflowNodeKind

FIRST_BATCH_WORKFLOW_NODE_KINDS = WORKFLOW_NODE_KINDS
WORKFLOW_RUNTIME_POLICY_EFFECT_SCOPE: Literal["new_workflow_runs"] = "new_workflow_runs"
WORKFLOW_CODE_PROVIDER_ADAPTER_KEYS = (
    "aio_isolated_code_v1",
    "provisioner_isolated_code_v1",
)
WORKFLOW_RUNTIME_MAX_INPUT_BYTES = 2_097_152
WORKFLOW_RUNTIME_MAX_EVENT_PREVIEW_BYTES = 65_536
WORKFLOW_RUNTIME_MAX_AGGREGATE_GROUPS = 254

_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_PositiveInt = Annotated[StrictInt, Field(ge=1, le=_MAX_SAFE_INTEGER)]
_NonNegativeInt = Annotated[StrictInt, Field(ge=0, le=_MAX_SAFE_INTEGER)]
_BoundedBytes = Annotated[StrictInt, Field(ge=1, le=2_147_483_648)]
_BoundedMilliseconds = Annotated[StrictInt, Field(ge=1, le=31_536_000_000)]
_StableKey = Annotated[
    StrictStr,
    Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9._-]*$"),
]
_Sha256Hex = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
_ImageDigest = Annotated[StrictStr, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
_HeaderName = Annotated[
    StrictStr,
    Field(min_length=1, max_length=128, pattern=r"^[a-z0-9!#$%&'*+.^_`|~-]+$"),
]
_CANONICAL_UUID_TEXT = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _validate_canonical_uuid_input(value: object) -> object:
    if isinstance(value, str) and _CANONICAL_UUID_TEXT.fullmatch(value) is None:
        raise ValueError("UUID input must use canonical lowercase hyphenated text")
    return value


_CanonicalUuid = Annotated[uuid.UUID, BeforeValidator(_validate_canonical_uuid_input)]


_TRUSTED_IMMUTABLE_JSON_ARRAY_CONTEXT_KEY = "workflow_runtime_trusted_immutable_json_arrays"
_TRUSTED_IMMUTABLE_JSON_ARRAY_CONTEXT_VALUE = object()


def _trusted_immutable_json_array_context() -> dict[str, object]:
    return {
        _TRUSTED_IMMUTABLE_JSON_ARRAY_CONTEXT_KEY: _TRUSTED_IMMUTABLE_JSON_ARRAY_CONTEXT_VALUE,
    }


def _materialize_immutable_json_array(value: object, info: ValidationInfo) -> object:
    """Freeze an external JSON array or an explicitly trusted frozen tuple."""

    if type(value) is list:
        return tuple(value)
    if type(value) is tuple and type(info.context) is dict and info.context.get(_TRUSTED_IMMUTABLE_JSON_ARRAY_CONTEXT_KEY) is _TRUSTED_IMMUTABLE_JSON_ARRAY_CONTEXT_VALUE:
        return value
    raise ValueError("Workflow runtime collection input must be a JSON array")


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
_NON_CANONICAL_NUMERIC_HOST = re.compile(
    r"^(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*$",
    re.IGNORECASE,
)
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _is_transport_controlled_header(value: str) -> bool:
    return value in _TRANSPORT_CONTROLLED_HEADER_NAMES or value.startswith("proxy-") or value.startswith("x-forwarded-")


class _StrictPolicyModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
    )


class WorkflowAllowedNodeTypeVersionsV1(_StrictPolicyModel):
    type: WorkflowNodeKind
    versions: Annotated[
        tuple[Annotated[StrictInt, Field(ge=1, le=1)], ...],
        BeforeValidator(_materialize_immutable_json_array),
        Field(min_length=1, max_length=1),
    ]

    @model_validator(mode="after")
    def validate_versions(self) -> Self:
        if self.versions != (1,):
            raise ValueError("first-batch Workflow node types support only type version 1")
        return self


class WorkflowCatalogPolicyV1(_StrictPolicyModel):
    allowed_type_versions: Annotated[
        tuple[WorkflowAllowedNodeTypeVersionsV1, ...],
        BeforeValidator(_materialize_immutable_json_array),
        Field(max_length=9),
    ]

    @model_validator(mode="after")
    def validate_unique_canonical_types(self) -> Self:
        node_types = [item.type for item in self.allowed_type_versions]
        if len(node_types) != len(set(node_types)):
            raise ValueError("Workflow catalog type/version entries must be unique")
        expected_order = {node_type: index for index, node_type in enumerate(FIRST_BATCH_WORKFLOW_NODE_KINDS)}
        if node_types != sorted(node_types, key=expected_order.__getitem__):
            raise ValueError("Workflow catalog type/version entries must use canonical registry order")
        return self


class WorkflowGraphLimitsV1(_StrictPolicyModel):
    max_nodes: Annotated[StrictInt, Field(ge=2, le=10_000)]
    max_edges: Annotated[StrictInt, Field(ge=1, le=50_000)]
    max_depth: Annotated[StrictInt, Field(ge=1, le=1_000)]
    max_total_steps: Annotated[StrictInt, Field(ge=2, le=10_000_000)]
    max_recursion_depth: Annotated[StrictInt, Field(ge=1, le=10_000_000)]
    max_parallelism: Annotated[StrictInt, Field(ge=1, le=1_024)]
    max_fan_out: Annotated[StrictInt, Field(ge=1, le=10_000)]
    max_loops: Annotated[StrictInt, Field(ge=0, le=1_000)]
    max_loop_body_nodes: Annotated[StrictInt, Field(ge=1, le=10_000)]
    max_loop_body_edges: Annotated[StrictInt, Field(ge=1, le=50_000)]
    max_loop_iterations: Annotated[StrictInt, Field(ge=1, le=1_000_000)]
    max_total_iterations: Annotated[StrictInt, Field(ge=1, le=10_000_000)]
    max_total_activations: Annotated[StrictInt, Field(ge=2, le=10_000_000)]
    max_aggregate_groups: Annotated[StrictInt, Field(ge=1, le=WORKFLOW_RUNTIME_MAX_AGGREGATE_GROUPS)]
    max_aggregate_candidates: Annotated[StrictInt, Field(ge=1, le=100_000)]

    @model_validator(mode="after")
    def validate_nested_limits(self) -> Self:
        if self.max_loop_body_nodes > self.max_nodes:
            raise ValueError("Loop body node limit cannot exceed the Workflow node limit")
        if self.max_loop_body_edges > self.max_edges:
            raise ValueError("Loop body edge limit cannot exceed the Workflow edge limit")
        if self.max_loop_iterations > self.max_total_iterations:
            raise ValueError("per-Loop iteration limit cannot exceed the Run iteration limit")
        if self.max_total_steps > self.max_total_activations:
            raise ValueError("activation limit cannot be lower than the total step limit")
        return self


class WorkflowExecutionLimitsV1(_StrictPolicyModel):
    max_node_timeout_ms: _BoundedMilliseconds
    max_run_timeout_ms: _BoundedMilliseconds
    max_human_wait_timeout_ms: _BoundedMilliseconds
    max_input_bytes: Annotated[StrictInt, Field(ge=1, le=WORKFLOW_RUNTIME_MAX_INPUT_BYTES)]
    max_state_bytes: _BoundedBytes
    max_output_bytes: _BoundedBytes
    max_event_preview_bytes: Annotated[StrictInt, Field(ge=1, le=WORKFLOW_RUNTIME_MAX_EVENT_PREVIEW_BYTES)]
    max_retry_attempts: Annotated[StrictInt, Field(ge=0, le=100)]
    retry_backoff_initial_ms: _BoundedMilliseconds
    retry_backoff_max_ms: _BoundedMilliseconds
    max_llm_tokens_per_call: Annotated[StrictInt, Field(ge=1, le=10_000_000)]
    max_llm_calls: Annotated[StrictInt, Field(ge=0, le=1_000_000)]
    max_code_activations: Annotated[StrictInt, Field(ge=0, le=1_000_000)]
    max_code_duration_ms: _BoundedMilliseconds
    max_http_calls: Annotated[StrictInt, Field(ge=0, le=1_000_000)]
    max_http_request_bytes: _BoundedBytes
    max_http_response_bytes: _BoundedBytes
    max_http_total_bytes: _BoundedBytes
    max_mcp_calls: Annotated[StrictInt, Field(ge=0, le=1_000_000)]
    max_files: Annotated[StrictInt, Field(ge=0, le=1_000_000)]
    max_file_bytes: Annotated[StrictInt, Field(ge=0, le=2_147_483_648)]

    @model_validator(mode="after")
    def validate_execution_limits(self) -> Self:
        if self.max_node_timeout_ms > self.max_run_timeout_ms:
            raise ValueError("node timeout cannot exceed the Workflow Run timeout")
        if self.retry_backoff_initial_ms > self.retry_backoff_max_ms:
            raise ValueError("initial retry backoff cannot exceed maximum retry backoff")
        if self.max_http_total_bytes < max(self.max_http_request_bytes, self.max_http_response_bytes):
            raise ValueError("HTTP cumulative byte limit cannot be below a single request or response limit")
        return self


class WorkflowCodeHardLimitsV1(_StrictPolicyModel):
    cpu_millicores: Annotated[StrictInt, Field(ge=1, le=64_000)]
    memory_bytes: _BoundedBytes
    max_pids: Annotated[StrictInt, Field(ge=1, le=4_096)]
    tmpfs_bytes: _BoundedBytes
    wall_timeout_ms: _BoundedMilliseconds
    max_source_bytes: _BoundedBytes
    max_stdout_bytes: _BoundedBytes
    max_stderr_bytes: _BoundedBytes
    max_result_bytes: _BoundedBytes
    max_total_log_bytes: _BoundedBytes
    read_only_root_filesystem: Literal[True]
    allow_mounts: Literal[False]
    allow_host_environment: Literal[False]
    allow_credentials: Literal[False]
    allow_runtime_sockets: Literal[False]

    @field_validator(
        "read_only_root_filesystem",
        "allow_mounts",
        "allow_host_environment",
        "allow_credentials",
        "allow_runtime_sockets",
        mode="before",
    )
    @classmethod
    def require_real_boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("Code isolation flags must be real booleans")
        return value

    @model_validator(mode="after")
    def validate_log_budget(self) -> Self:
        if self.max_total_log_bytes < max(self.max_stdout_bytes, self.max_stderr_bytes):
            raise ValueError("Code total log limit cannot be below either retained stream limit")
        return self


class WorkflowCodePolicyV1(_StrictPolicyModel):
    enabled: StrictBool
    provider_adapter_key: _StableKey | None
    execution_profile_id: _StableKey | None
    runtime_contract: Literal["python3.12-v1"]
    image_digest: _ImageDigest | None
    isolation_profile: _StableKey | None
    network_policy: Literal["deny_all"]
    dns_policy: Literal["deny_all"]
    hard_limits: WorkflowCodeHardLimitsV1

    @model_validator(mode="after")
    def validate_enabled_profile(self) -> Self:
        if self.provider_adapter_key is not None and self.provider_adapter_key not in WORKFLOW_CODE_PROVIDER_ADAPTER_KEYS:
            raise ValueError("Workflow Code provider adapter key is not in the static allowlist")
        profile_values = (
            self.provider_adapter_key,
            self.execution_profile_id,
            self.image_digest,
            self.isolation_profile,
        )
        populated = sum(value is not None for value in profile_values)
        if populated not in {0, len(profile_values)}:
            raise ValueError("Workflow Code execution profile fields must be selected atomically")
        if self.enabled and any(value is None for value in profile_values):
            raise ValueError("enabled Workflow Code requires one exact static execution profile")
        return self


class WorkflowHttpInjectionProfileV1(_StrictPolicyModel):
    id: _StableKey
    location: Literal["header"]
    scheme: Literal["bearer", "basic", "api_key"]
    target_header: _HeaderName
    credential_payload_contract: Literal["bearer_token_v1", "basic_auth_v1", "api_key_v1"]

    @model_validator(mode="after")
    def validate_header_injection(self) -> Self:
        expected_contract = {
            "bearer": "bearer_token_v1",
            "basic": "basic_auth_v1",
            "api_key": "api_key_v1",
        }[self.scheme]
        if self.credential_payload_contract != expected_contract:
            raise ValueError("HTTP injection scheme and Credential payload contract do not match")
        if self.scheme in {"bearer", "basic"} and self.target_header != "authorization":
            raise ValueError("Bearer and Basic profiles must target the Authorization header")
        if self.scheme == "api_key" and self.target_header == "authorization":
            raise ValueError("API key profiles must target a custom safe header")
        if _is_transport_controlled_header(self.target_header):
            raise ValueError("HTTP injection profile targets a transport-controlled header")
        return self


class WorkflowHttpEndpointPolicyV1(_StrictPolicyModel):
    id: _StableKey
    origin: Annotated[StrictStr, Field(min_length=1, max_length=2_048)]
    allowed_methods: Annotated[
        tuple[Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"], ...],
        BeforeValidator(_materialize_immutable_json_array),
        Field(min_length=1, max_length=6),
    ]
    injection_profile_ids: Annotated[
        tuple[_StableKey, ...],
        BeforeValidator(_materialize_immutable_json_array),
        Field(max_length=32),
    ]
    write_idempotency: Literal["none", "server_derived_key"]
    idempotency_header: _HeaderName | None

    @field_validator("origin")
    @classmethod
    def validate_fixed_https_origin(cls, value: str) -> str:
        if not value.isascii() or "\\" in value or "%" in value:
            raise ValueError("HTTP endpoint origin must use canonical ASCII host syntax")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("HTTP endpoint origin is invalid") from exc
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("HTTP endpoint origin must use HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("HTTP endpoint origin cannot contain user information")
        if parsed.netloc != parsed.netloc.lower():
            raise ValueError("HTTP endpoint host syntax must be lowercase")
        if parsed.path or parsed.query or parsed.fragment:
            raise ValueError("HTTP endpoint policy accepts an origin, not a URL path or query")
        authority = parsed.netloc
        if authority.startswith("["):
            raise ValueError("HTTP endpoint origin cannot use an IP literal")
        raw_hostname = authority
        if ":" in authority:
            raw_hostname, _, raw_port = authority.rpartition(":")
            if not raw_hostname or ":" in raw_hostname or not raw_port.isdigit() or str(port) != raw_port:
                raise ValueError("HTTP endpoint origin must use a canonical port")
        hostname = parsed.hostname.rstrip(".").lower()
        if raw_hostname != hostname:
            raise ValueError("HTTP endpoint origin must use canonical host syntax")
        if parsed.hostname != hostname:
            raise ValueError("HTTP endpoint hostname must be lowercase without a trailing dot")
        if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith((".localhost", ".local", ".internal")):
            raise ValueError("HTTP endpoint origin is not public")
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            raise ValueError("HTTP endpoint origin cannot use an IP literal")
        if _NON_CANONICAL_NUMERIC_HOST.fullmatch(hostname):
            raise ValueError("HTTP endpoint origin cannot use a numeric host")
        labels = hostname.split(".")
        if len(hostname) > 253 or len(labels) < 2 or any(_DNS_LABEL.fullmatch(label) is None for label in labels):
            raise ValueError("HTTP endpoint origin must use a canonical DNS hostname")
        if port is not None and not 1 <= port <= 65_535:
            raise ValueError("HTTP endpoint origin port is invalid")
        return value

    @model_validator(mode="after")
    def validate_unique_endpoint_values(self) -> Self:
        if len(self.allowed_methods) != len(set(self.allowed_methods)):
            raise ValueError("HTTP endpoint methods must be unique")
        method_order = {method: index for index, method in enumerate(("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"))}
        if self.allowed_methods != tuple(sorted(self.allowed_methods, key=method_order.__getitem__)):
            raise ValueError("HTTP endpoint methods must use canonical order")
        if len(self.injection_profile_ids) != len(set(self.injection_profile_ids)):
            raise ValueError("HTTP endpoint injection profile references must be unique")
        if self.injection_profile_ids != tuple(sorted(self.injection_profile_ids)):
            raise ValueError("HTTP endpoint injection profile references must use canonical order")
        write_methods = {"POST", "PUT", "PATCH", "DELETE"}.intersection(self.allowed_methods)
        if self.write_idempotency == "server_derived_key":
            if not write_methods or self.idempotency_header is None:
                raise ValueError("server-derived idempotency requires a write method and reserved header")
        elif self.idempotency_header is not None:
            raise ValueError("idempotency header is only valid for server-derived write idempotency")
        if self.idempotency_header is not None and (self.idempotency_header == "authorization" or _is_transport_controlled_header(self.idempotency_header)):
            raise ValueError("idempotency header cannot target a transport- or Credential-controlled header")
        return self


class WorkflowHttpTransportPolicyV1(_StrictPolicyModel):
    connect_timeout_ms: _BoundedMilliseconds
    read_timeout_ms: _BoundedMilliseconds
    write_timeout_ms: _BoundedMilliseconds
    total_timeout_ms: _BoundedMilliseconds
    max_headers: Annotated[StrictInt, Field(ge=1, le=64)]
    max_header_name_bytes: Annotated[StrictInt, Field(ge=1, le=128)]
    max_header_value_bytes: Annotated[StrictInt, Field(ge=1, le=4_096)]
    max_request_bytes: _BoundedBytes
    max_wire_response_bytes: Annotated[StrictInt, Field(ge=1, le=2_097_152)]
    max_decompressed_response_bytes: Annotated[StrictInt, Field(ge=1, le=2_097_152)]
    max_json_depth: Annotated[StrictInt, Field(ge=1, le=64)]
    max_retries: Annotated[StrictInt, Field(ge=0, le=100)]
    retry_backoff_initial_ms: _BoundedMilliseconds
    retry_backoff_max_ms: _BoundedMilliseconds
    max_retry_after_ms: _BoundedMilliseconds
    tls_verify: Literal[True]
    follow_redirects: Literal[False]
    cookie_jar: Literal[False]
    trust_env: Literal[False]

    @field_validator("tls_verify", "follow_redirects", "cookie_jar", "trust_env", mode="before")
    @classmethod
    def require_real_boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("HTTP transport flags must be real booleans")
        return value

    @model_validator(mode="after")
    def validate_transport_limits(self) -> Self:
        if self.total_timeout_ms < max(self.connect_timeout_ms, self.read_timeout_ms, self.write_timeout_ms):
            raise ValueError("HTTP total timeout cannot be below a phase timeout")
        if self.retry_backoff_initial_ms > self.retry_backoff_max_ms:
            raise ValueError("initial HTTP retry backoff cannot exceed maximum retry backoff")
        if self.max_wire_response_bytes > self.max_decompressed_response_bytes:
            raise ValueError("decompressed HTTP response limit cannot be below the wire response limit")
        return self


class WorkflowHttpPolicyV1(_StrictPolicyModel):
    enabled: StrictBool
    write_enabled: StrictBool
    egress_profile_id: _StableKey | None
    egress_profile_digest: _Sha256Hex | None
    endpoint_policies: Annotated[
        tuple[WorkflowHttpEndpointPolicyV1, ...],
        BeforeValidator(_materialize_immutable_json_array),
        Field(max_length=64),
    ]
    injection_profiles: Annotated[
        tuple[WorkflowHttpInjectionProfileV1, ...],
        BeforeValidator(_materialize_immutable_json_array),
        Field(max_length=64),
    ]
    transport: WorkflowHttpTransportPolicyV1

    @model_validator(mode="after")
    def validate_http_profiles(self) -> Self:
        if self.write_enabled and not self.enabled:
            raise ValueError("HTTP write support cannot be enabled while HTTP is disabled")
        if (self.egress_profile_id is None) != (self.egress_profile_digest is None):
            raise ValueError("HTTP egress profile id and digest must be selected together")
        if self.enabled and (self.egress_profile_id is None or not self.endpoint_policies):
            raise ValueError("enabled Workflow HTTP requires an exact egress profile and endpoint policy")
        injection_ids = [profile.id for profile in self.injection_profiles]
        if len(injection_ids) != len(set(injection_ids)):
            raise ValueError("HTTP injection profile ids must be unique")
        if injection_ids != sorted(injection_ids):
            raise ValueError("HTTP injection profiles must use canonical id order")
        endpoint_ids = [endpoint.id for endpoint in self.endpoint_policies]
        if len(endpoint_ids) != len(set(endpoint_ids)):
            raise ValueError("HTTP endpoint policy ids must be unique")
        if endpoint_ids != sorted(endpoint_ids):
            raise ValueError("HTTP endpoint policies must use canonical id order")
        known_injection_ids = set(injection_ids)
        injection_by_id = {profile.id: profile for profile in self.injection_profiles}
        for endpoint in self.endpoint_policies:
            if not set(endpoint.injection_profile_ids) <= known_injection_ids:
                raise ValueError("HTTP endpoint references an unknown injection profile")
            if endpoint.idempotency_header is not None and any(injection_by_id[profile_id].target_header == endpoint.idempotency_header for profile_id in endpoint.injection_profile_ids):
                raise ValueError("HTTP Credential injection cannot target the server-owned idempotency header")
        return self


class WorkflowRetentionPolicyV1(_StrictPolicyModel):
    terminal_run_days: Annotated[StrictInt, Field(ge=1, le=3_650)]
    event_days: Annotated[StrictInt, Field(ge=1, le=3_650)]
    http_effect_days: Annotated[StrictInt, Field(ge=1, le=3_650)]
    destroyed_code_lease_days: Annotated[StrictInt, Field(ge=1, le=3_650)]


class WorkflowFutureCapabilitiesV1(_StrictPolicyModel):
    human_input_enabled: StrictBool
    agent_enabled: StrictBool
    tool_enabled: StrictBool
    mcp_enabled: StrictBool
    iteration_enabled: StrictBool
    subworkflow_enabled: StrictBool
    automation_enabled: StrictBool
    chatflow_enabled: StrictBool

    @model_validator(mode="after")
    def reject_unimplemented_capabilities(self) -> Self:
        if any(
            (
                self.human_input_enabled,
                self.agent_enabled,
                self.tool_enabled,
                self.mcp_enabled,
                self.iteration_enabled,
                self.subworkflow_enabled,
                self.automation_enabled,
                self.chatflow_enabled,
            )
        ):
            raise ValueError("future Workflow capabilities are not implemented in policy schema v1")
        return self


class WorkflowRuntimePolicyV1(_StrictPolicyModel):
    schema_version: Literal[1]
    enabled: StrictBool
    admission_enabled: StrictBool
    catalog: WorkflowCatalogPolicyV1
    graph_limits: WorkflowGraphLimitsV1
    execution_limits: WorkflowExecutionLimitsV1
    code: WorkflowCodePolicyV1
    http: WorkflowHttpPolicyV1
    retention: WorkflowRetentionPolicyV1
    future: WorkflowFutureCapabilitiesV1

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_integer_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Workflow runtime policy schema_version must be an integer")
        return value

    @model_validator(mode="after")
    def validate_atomic_policy(self) -> Self:
        if self.admission_enabled and not self.enabled:
            raise ValueError("Workflow admission cannot be enabled while Workflow is disabled")
        if self.code.enabled and self.execution_limits.max_code_activations == 0:
            raise ValueError("enabled Workflow Code requires a positive activation budget")
        if self.http.enabled and self.execution_limits.max_http_calls == 0:
            raise ValueError("enabled Workflow HTTP requires a positive call budget")
        if self.code.hard_limits.wall_timeout_ms > self.execution_limits.max_node_timeout_ms:
            raise ValueError("Code hard wall timeout cannot exceed the global node timeout")
        if self.code.enabled and self.code.hard_limits.wall_timeout_ms > self.execution_limits.max_code_duration_ms:
            raise ValueError("Code hard wall timeout cannot exceed the Run Code duration budget")
        if self.code.hard_limits.max_result_bytes > self.execution_limits.max_output_bytes:
            raise ValueError("Code result hard limit cannot exceed the global Workflow output limit")
        if self.execution_limits.max_http_request_bytes > self.http.transport.max_request_bytes:
            raise ValueError("Workflow HTTP request budget cannot exceed the transport hard limit")
        if self.execution_limits.max_http_response_bytes > self.http.transport.max_decompressed_response_bytes:
            raise ValueError("Workflow HTTP response budget cannot exceed the transport hard limit")
        return self


def workflow_runtime_policy_checksum(value: WorkflowRuntimePolicyV1) -> str:
    """Return the canonical secret-free checksum persisted with a policy version."""

    if type(value) is not WorkflowRuntimePolicyV1:
        raise TypeError("value must be a validated WorkflowRuntimePolicyV1")
    payload = json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class WorkflowRuntimePolicyUpdateRequestV1(_StrictPolicyModel):
    expected_revision: _NonNegativeInt
    value: WorkflowRuntimePolicyV1


class WorkflowRuntimeStoredPolicyV1(_StrictPolicyModel):
    policy_version_id: _CanonicalUuid
    revision: _PositiveInt
    schema_version: Literal[1]
    payload_checksum: _Sha256Hex
    value: WorkflowRuntimePolicyV1

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_integer_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("stored Workflow policy schema_version must be an integer")
        return value

    @model_validator(mode="after")
    def validate_payload_identity(self) -> Self:
        if self.schema_version != self.value.schema_version:
            raise ValueError("stored Workflow policy schema version does not match its value")
        if self.payload_checksum != workflow_runtime_policy_checksum(self.value):
            raise ValueError("stored Workflow policy checksum does not match its value")
        return self


class WorkflowRuntimeEffectivePolicyV1(_StrictPolicyModel):
    policy_version_id: _CanonicalUuid
    revision: _PositiveInt
    schema_version: Literal[1]
    payload_checksum: _Sha256Hex

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_integer_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("effective Workflow policy schema_version must be an integer")
        return value


class _WorkflowRuntimeReadinessBaseV1(_StrictPolicyModel):
    @field_validator("admission_ready", mode="before", check_fields=False)
    @classmethod
    def require_real_boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("Workflow runtime readiness flags must be real booleans")
        return value


class WorkflowRuntimeReadyV1(_WorkflowRuntimeReadinessBaseV1):
    status: Literal["ready"]
    code: Literal["WORKFLOW_RUNTIME_READY"]
    admission_ready: StrictBool


class WorkflowRuntimeDisabledV1(_WorkflowRuntimeReadinessBaseV1):
    status: Literal["ready"]
    code: Literal["WORKFLOW_RUNTIME_DISABLED"]
    admission_ready: Literal[False]


class WorkflowRuntimePendingV1(_WorkflowRuntimeReadinessBaseV1):
    status: Literal["pending"]
    code: Literal["WORKFLOW_RUNTIME_PENDING"]
    admission_ready: Literal[False]


class WorkflowRuntimeUnavailableV1(_WorkflowRuntimeReadinessBaseV1):
    status: Literal["unavailable"]
    code: Literal["WORKFLOW_RUNTIME_UNAVAILABLE"]
    admission_ready: Literal[False]


type WorkflowRuntimeReadinessV1 = Annotated[
    WorkflowRuntimeReadyV1 | WorkflowRuntimeDisabledV1 | WorkflowRuntimePendingV1 | WorkflowRuntimeUnavailableV1,
    Field(discriminator="code"),
]
WORKFLOW_RUNTIME_READINESS_V1_ADAPTER = TypeAdapter(WorkflowRuntimeReadinessV1)


class WorkflowRuntimeAdminPolicyV1(_StrictPolicyModel):
    section: Literal["workflow_runtime"]
    stored: WorkflowRuntimeStoredPolicyV1
    effective: WorkflowRuntimeEffectivePolicyV1 | None
    effect_scope: Literal["new_workflow_runs"]
    pending_roles: Annotated[
        tuple[Literal["gateway", "worker", "scheduler"], ...],
        BeforeValidator(_materialize_immutable_json_array),
        Field(max_length=3),
    ]
    readiness: WorkflowRuntimeReadinessV1

    @model_validator(mode="after")
    def validate_materialization_projection(self) -> Self:
        role_order = {role: index for index, role in enumerate(("gateway", "worker", "scheduler"))}
        if len(self.pending_roles) != len(set(self.pending_roles)):
            raise ValueError("Workflow runtime pending roles must be unique")
        if self.pending_roles != tuple(sorted(self.pending_roles, key=role_order.__getitem__)):
            raise ValueError("Workflow runtime pending roles must use canonical order")

        stored_identity = (
            self.stored.policy_version_id,
            self.stored.revision,
            self.stored.schema_version,
            self.stored.payload_checksum,
        )
        effective_identity = None
        if self.effective is not None:
            effective_identity = (
                self.effective.policy_version_id,
                self.effective.revision,
                self.effective.schema_version,
                self.effective.payload_checksum,
            )

        if self.readiness.status == "ready":
            if self.pending_roles:
                raise ValueError("ready Workflow runtime projection cannot have pending roles")
            if effective_identity != stored_identity:
                raise ValueError("ready Workflow runtime projection requires the exact stored policy to be effective")
            if self.readiness.code == "WORKFLOW_RUNTIME_DISABLED":
                if self.stored.value.enabled:
                    raise ValueError("disabled Workflow runtime readiness requires the effective policy to be disabled")
            else:
                if not self.stored.value.enabled:
                    raise ValueError("ready Workflow runtime readiness requires the effective policy to be enabled")
                if self.readiness.admission_ready is not self.stored.value.admission_enabled:
                    raise ValueError("Workflow runtime admission readiness must match the effective policy gate")
        elif self.readiness.status == "pending":
            if not (self.stored.value.enabled and self.stored.value.admission_enabled):
                raise ValueError("pending Workflow runtime projection requires admission-enabled policy")
            if self.pending_roles != ("worker",):
                raise ValueError("pending Workflow runtime projection requires only the Worker pending role")
            if effective_identity != stored_identity:
                raise ValueError("pending Workflow runtime projection requires the Gateway-effective exact stored policy")
        else:
            if self.effective is not None or self.pending_roles != ("gateway",):
                raise ValueError("unavailable Workflow runtime projection requires null effective policy and only the Gateway pending role")
        return self


class WorkflowRuntimePolicyUpdateResponseV1(WorkflowRuntimeAdminPolicyV1):
    catalog_revision: _PositiveInt


def revalidate_trusted_workflow_runtime_policy(
    value: WorkflowRuntimePolicyV1,
) -> WorkflowRuntimePolicyV1:
    """Revalidate one already-validated frozen policy through the trusted seam.

    Raw request/database mappings must call ``model_validate`` without this
    context and therefore accept only real JSON-array ``list`` values.  This
    adapter is intentionally exact-type-only so arbitrary mappings cannot use
    the tuple revalidation path.
    """

    if type(value) is not WorkflowRuntimePolicyV1:
        raise TypeError("trusted Workflow runtime policy must be an exact validated v1 model")
    return WorkflowRuntimePolicyV1.model_validate(
        value,
        context=_trusted_immutable_json_array_context(),
    )


def create_workflow_runtime_stored_policy(
    *,
    policy_version_id: uuid.UUID,
    revision: int,
    schema_version: int,
    payload_checksum: str,
    value: WorkflowRuntimePolicyV1,
) -> WorkflowRuntimeStoredPolicyV1:
    """Build a Stored DTO from one exact, already-validated frozen policy."""

    if type(value) is not WorkflowRuntimePolicyV1:
        raise TypeError("stored Workflow runtime policy requires an exact validated v1 value")
    return WorkflowRuntimeStoredPolicyV1.model_validate(
        {
            "policy_version_id": policy_version_id,
            "revision": revision,
            "schema_version": schema_version,
            "payload_checksum": payload_checksum,
            "value": value,
        },
        context=_trusted_immutable_json_array_context(),
    )


def create_workflow_runtime_admin_policy(
    *,
    stored: WorkflowRuntimeStoredPolicyV1,
    effective: WorkflowRuntimeEffectivePolicyV1 | None,
    pending_roles: tuple[Literal["gateway", "worker", "scheduler"], ...],
    readiness: WorkflowRuntimeReadyV1 | WorkflowRuntimeDisabledV1 | WorkflowRuntimePendingV1 | WorkflowRuntimeUnavailableV1,
) -> WorkflowRuntimeAdminPolicyV1:
    """Build the closed Admin projection from validated internal values."""

    if type(stored) is not WorkflowRuntimeStoredPolicyV1:
        raise TypeError("Workflow runtime Admin projection requires an exact Stored policy")
    if effective is not None and type(effective) is not WorkflowRuntimeEffectivePolicyV1:
        raise TypeError("Workflow runtime Admin projection requires an exact Effective policy")
    if type(pending_roles) is not tuple:
        raise TypeError("trusted Workflow runtime pending roles must be an immutable tuple")
    if type(readiness) not in {
        WorkflowRuntimeReadyV1,
        WorkflowRuntimeDisabledV1,
        WorkflowRuntimePendingV1,
        WorkflowRuntimeUnavailableV1,
    }:
        raise TypeError("Workflow runtime Admin projection requires an exact readiness model")
    return WorkflowRuntimeAdminPolicyV1.model_validate(
        {
            "section": "workflow_runtime",
            "stored": stored,
            "effective": effective,
            "effect_scope": WORKFLOW_RUNTIME_POLICY_EFFECT_SCOPE,
            "pending_roles": pending_roles,
            "readiness": readiness,
        },
        context=_trusted_immutable_json_array_context(),
    )


def create_workflow_runtime_update_response(
    *,
    catalog_revision: int,
    projection: WorkflowRuntimeAdminPolicyV1,
) -> WorkflowRuntimePolicyUpdateResponseV1:
    """Add catalog CAS identity to one validated internal Admin projection."""

    if type(projection) is not WorkflowRuntimeAdminPolicyV1:
        raise TypeError("Workflow runtime update response requires an exact Admin projection")
    return WorkflowRuntimePolicyUpdateResponseV1.model_validate(
        {
            "catalog_revision": catalog_revision,
            "section": projection.section,
            "stored": projection.stored,
            "effective": projection.effective,
            "effect_scope": projection.effect_scope,
            "pending_roles": projection.pending_roles,
            "readiness": projection.readiness,
        },
        context=_trusted_immutable_json_array_context(),
    )


__all__ = [
    "FIRST_BATCH_WORKFLOW_NODE_KINDS",
    "WORKFLOW_CODE_PROVIDER_ADAPTER_KEYS",
    "WORKFLOW_RUNTIME_READINESS_V1_ADAPTER",
    "WORKFLOW_RUNTIME_POLICY_EFFECT_SCOPE",
    "WORKFLOW_RUNTIME_MAX_AGGREGATE_GROUPS",
    "WORKFLOW_RUNTIME_MAX_EVENT_PREVIEW_BYTES",
    "WORKFLOW_RUNTIME_MAX_INPUT_BYTES",
    "WorkflowAllowedNodeTypeVersionsV1",
    "WorkflowCatalogPolicyV1",
    "WorkflowCodeHardLimitsV1",
    "WorkflowCodePolicyV1",
    "WorkflowExecutionLimitsV1",
    "WorkflowFutureCapabilitiesV1",
    "WorkflowGraphLimitsV1",
    "WorkflowHttpEndpointPolicyV1",
    "WorkflowHttpInjectionProfileV1",
    "WorkflowHttpPolicyV1",
    "WorkflowHttpTransportPolicyV1",
    "WorkflowRetentionPolicyV1",
    "WorkflowRuntimeAdminPolicyV1",
    "WorkflowRuntimeDisabledV1",
    "WorkflowRuntimeEffectivePolicyV1",
    "WorkflowRuntimePendingV1",
    "WorkflowRuntimePolicyV1",
    "WorkflowRuntimePolicyUpdateRequestV1",
    "WorkflowRuntimePolicyUpdateResponseV1",
    "WorkflowRuntimeReadinessV1",
    "WorkflowRuntimeReadyV1",
    "WorkflowRuntimeStoredPolicyV1",
    "WorkflowRuntimeUnavailableV1",
    "create_workflow_runtime_admin_policy",
    "create_workflow_runtime_stored_policy",
    "create_workflow_runtime_update_response",
    "revalidate_trusted_workflow_runtime_policy",
    "workflow_runtime_policy_checksum",
]
