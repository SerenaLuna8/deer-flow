"""Strict, secret-free transport contracts for Workflow Definition control.

Drafts intentionally use a partial form of the authored Spec and Canvas.  An
omitted semantic field is legal here, while every field that is present still
has to be portable JSON and every structural object remains closed.  Publish
never accepts a Spec or Canvas; it can only identify the stored Draft revision.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from app.workflows.contracts import WorkflowValidationIssueV1
from deerflow.workflows import (
    ConditionNodeConfigV1,
    EndNodeConfigV1,
    HttpRequestNodeConfigV1,
    LlmNodeConfigV1,
    LoopNodeConfigV1,
    PythonCodeNodeConfigV1,
    StartNodeConfigV1,
    StrictLiteralOne,
    TransformNodeConfigV1,
    VariableAggregateNodeConfigV1,
)
from deerflow.workflows.compiler import FrozenObject, freeze_json, thaw_json

_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CANONICAL_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_NODE_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SAFE_SLOT_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$")
_SAFE_REQUEST_ID = re.compile(r"^[\x20-\x7e]{1,512}$")

# These are authority/execution-boundary fields, not authored Workflow data.
# The comparison is exact so a JSON Schema property named e.g. ``project`` is
# not confused with the server-owned ``project_id`` coordinate.
_FORBIDDEN_DRAFT_FIELDS = frozenset(
    {
        "authority",
        "authority_id",
        "capability",
        "capabilities",
        "credential_grant_id",
        "credential_id",
        "credential_version_id",
        "envelope_id",
        "execution_profile",
        "executor",
        "executor_id",
        "membership_id",
        "membership_version",
        "owner_id",
        "owner_user_id",
        "private_scope",
        "project_context",
        "project_id",
        "project_role",
        "project_slug",
        "runtime",
        "runtime_id",
        "runtime_profile",
        "secret",
        "secret_value",
        "secrets",
        "user_id",
    }
)


def _freeze_object(value: object) -> FrozenObject:
    if type(value) is FrozenObject:
        return value
    frozen = freeze_json(value)
    if not isinstance(frozen, FrozenObject):
        raise ValueError("Workflow Draft field must be a JSON object")
    return frozen


def _thaw_object(value: FrozenObject) -> dict[str, object]:
    materialized = thaw_json(value)
    if not isinstance(materialized, dict):
        raise TypeError("frozen Workflow Draft object did not materialize as an object")
    return materialized


type _FrozenJsonObject = Annotated[
    FrozenObject,
    BeforeValidator(_freeze_object),
    PlainSerializer(_thaw_object, return_type=dict[str, object]),
]


def _freeze_json_value(value: object) -> object:
    return freeze_json(value)


def _thaw_json_value(value: object) -> object:
    return thaw_json(value)


type _FrozenJsonValue = Annotated[
    object,
    BeforeValidator(_freeze_json_value),
    PlainSerializer(_thaw_json_value),
]


_TRUSTED_ARRAY_CONTEXT_KEY = "workflow_definition_trusted_json_arrays"
_TRUSTED_ARRAY_CONTEXT_VALUE = object()


def _trusted_array_context() -> dict[str, object]:
    return {_TRUSTED_ARRAY_CONTEXT_KEY: _TRUSTED_ARRAY_CONTEXT_VALUE}


def _freeze_json_array(value: object, info: ValidationInfo) -> object:
    if type(value) is list:
        return tuple(value)
    if type(value) is tuple and type(info.context) is dict and info.context.get(_TRUSTED_ARRAY_CONTEXT_KEY) is _TRUSTED_ARRAY_CONTEXT_VALUE:
        return value
    raise ValueError("Workflow Draft collection must be a JSON array")


def _reject_forbidden_fields(value: object) -> None:
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, FrozenObject):
            current = _thaw_object(current)
        if isinstance(current, dict):
            forbidden = sorted(key for key in current if not isinstance(key, str) or key in _FORBIDDEN_DRAFT_FIELDS or key.startswith("__"))
            if forbidden:
                raise ValueError("Workflow Draft contains a server-owned field")
            stack.extend(current.values())
        elif isinstance(current, list | tuple):
            stack.extend(current)


_NODE_CONFIG_MODELS: dict[str, type[BaseModel]] = {
    "start": StartNodeConfigV1,
    "llm": LlmNodeConfigV1,
    "condition": ConditionNodeConfigV1,
    "transform": TransformNodeConfigV1,
    "variable_aggregate": VariableAggregateNodeConfigV1,
    "loop": LoopNodeConfigV1,
    "http_request": HttpRequestNodeConfigV1,
    "python_code": PythonCodeNodeConfigV1,
    "end": EndNodeConfigV1,
}


def _validate_partial_known_config(node_type: object, config: object) -> None:
    _reject_forbidden_fields(config)
    model = _NODE_CONFIG_MODELS.get(node_type) if isinstance(node_type, str) else None
    if model is None:
        # Unknown/historical types may be preserved in a Draft.  Validate and
        # Publish will reject them against the exact installed Registry.
        return
    try:
        model.model_validate(config)
    except ValidationError as error:
        # Missing semantic fields are the defining property of a Draft.  Any
        # present field with a wrong type/value or any unknown field remains a
        # transport error.
        incomplete_only = {"missing", "union_tag_not_found"}
        if any(item["type"] not in incomplete_only for item in error.errors()):
            raise ValueError("Workflow Draft node config contains an invalid present field") from error


def _safe_string(value: object, *, pattern: re.Pattern[str], label: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


class _StrictDefinitionContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
        serialize_by_alias=True,
    )


class WorkflowDraftEndpointV1(_StrictDefinitionContract):
    node_id: StrictStr | None = None
    port_id: StrictStr | None = None

    @field_validator("node_id")
    @classmethod
    def validate_node_id(cls, value: str | None) -> str | None:
        if value is not None:
            _safe_string(value, pattern=_NODE_ID, label="Draft node ID")
        return value

    @field_validator("port_id")
    @classmethod
    def validate_port_id(cls, value: str | None) -> str | None:
        if value is not None:
            _safe_string(value, pattern=_SAFE_IDENTIFIER, label="Draft port ID")
        return value


class WorkflowDraftTransitionV1(_StrictDefinitionContract):
    id: StrictStr | None = None
    source: WorkflowDraftEndpointV1 | None = None
    target: WorkflowDraftEndpointV1 | None = None

    @field_validator("id")
    @classmethod
    def validate_transition_id(cls, value: str | None) -> str | None:
        if value is not None:
            _safe_string(value, pattern=_SAFE_IDENTIFIER, label="Draft transition ID")
        return value


class WorkflowDraftNodeV1(_StrictDefinitionContract):
    id: StrictStr | None = None
    type: Annotated[StrictStr, Field(min_length=1, max_length=128)] | None = None
    type_version: Annotated[StrictInt, Field(ge=1, le=_MAX_SAFE_INTEGER)] | None = None
    scope: _FrozenJsonObject | None = None
    custom_label: StrictStr | None = None
    description: StrictStr | None = None
    input_bindings: _FrozenJsonObject | None = None
    execution_policy: _FrozenJsonObject | None = None
    config: _FrozenJsonObject | None = None

    @model_validator(mode="before")
    @classmethod
    def validate_present_structure(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        _reject_forbidden_fields(value)
        config = value.get("config")
        if config is not None:
            _validate_partial_known_config(value.get("type"), config)
        return value

    @field_validator("id")
    @classmethod
    def validate_node_id(cls, value: str | None) -> str | None:
        if value is not None:
            _safe_string(value, pattern=_NODE_ID, label="Draft node ID")
        return value


class WorkflowDraftInputV1(_StrictDefinitionContract):
    id: StrictStr | None = None
    name: StrictStr | None = None
    label: StrictStr | None = None
    description: StrictStr | None = None
    value_type: _FrozenJsonObject | None = None
    required: StrictBool | None = None
    default: _FrozenJsonValue | None = None
    constraints: _FrozenJsonObject | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_authority(cls, value: object) -> object:
        _reject_forbidden_fields(value)
        return value


class WorkflowDraftOutputV1(_StrictDefinitionContract):
    id: StrictStr | None = None
    name: StrictStr | None = None
    description: StrictStr | None = None
    value_type: _FrozenJsonObject | None = None
    source: _FrozenJsonObject | None = None
    default: _FrozenJsonValue | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_authority(cls, value: object) -> object:
        _reject_forbidden_fields(value)
        return value


class WorkflowDraftCredentialSlotV1(_StrictDefinitionContract):
    id: StrictStr | None = None
    name: StrictStr | None = None
    purpose: Literal["http_auth"] | None = None
    payload_schema: _FrozenJsonObject | None = None
    required: Literal[True] | None = None

    @field_validator("id")
    @classmethod
    def validate_slot_id(cls, value: str | None) -> str | None:
        if value is not None:
            _safe_string(value, pattern=_SAFE_SLOT_ID, label="Draft Credential slot ID")
        return value

    @field_validator("required", mode="before")
    @classmethod
    def require_real_true(cls, value: object) -> object:
        if value is not None and (type(value) is not bool or value is not True):
            raise ValueError("Draft Credential slot required must be true")
        return value


class WorkflowDraftSpecV1(_StrictDefinitionContract):
    schema_version: StrictLiteralOne
    entry_node_id: StrictStr | None = None
    nodes: (
        Annotated[
            tuple[WorkflowDraftNodeV1, ...],
            BeforeValidator(_freeze_json_array),
            Field(max_length=10_000),
        ]
        | None
    ) = None
    transitions: (
        Annotated[
            tuple[WorkflowDraftTransitionV1, ...],
            BeforeValidator(_freeze_json_array),
            Field(max_length=50_000),
        ]
        | None
    ) = None
    workflow_inputs: (
        Annotated[
            tuple[WorkflowDraftInputV1, ...],
            BeforeValidator(_freeze_json_array),
            Field(max_length=255),
        ]
        | None
    ) = None
    workflow_outputs: (
        Annotated[
            tuple[WorkflowDraftOutputV1, ...],
            BeforeValidator(_freeze_json_array),
            Field(max_length=10_000),
        ]
        | None
    ) = None
    credential_slots: (
        Annotated[
            tuple[WorkflowDraftCredentialSlotV1, ...],
            BeforeValidator(_freeze_json_array),
            Field(max_length=255),
        ]
        | None
    ) = None

    @field_validator("entry_node_id")
    @classmethod
    def validate_entry_node_id(cls, value: str | None) -> str | None:
        if value is not None:
            _safe_string(value, pattern=_NODE_ID, label="Draft entry node ID")
        return value


class WorkflowDraftPositionV1(_StrictDefinitionContract):
    x: StrictInt | float | None = None
    y: StrictInt | float | None = None

    @field_validator("x", "y")
    @classmethod
    def validate_number(cls, value: int | float | None) -> int | float | None:
        if value is not None:
            freeze_json(value)
        return value


class WorkflowDraftNodeLayoutV1(_StrictDefinitionContract):
    node_id: StrictStr | None = None
    position: WorkflowDraftPositionV1 | None = None
    parent_node_id: StrictStr | None = None
    collapsed: StrictBool | None = None

    @field_validator("node_id", "parent_node_id")
    @classmethod
    def validate_node_id(cls, value: str | None) -> str | None:
        if value is not None:
            _safe_string(value, pattern=_NODE_ID, label="Draft Canvas node ID")
        return value


class WorkflowDraftEdgeLayoutV1(_StrictDefinitionContract):
    edge_id: StrictStr | None = None
    routing: Literal["bezier", "smoothstep"] | None = None

    @field_validator("edge_id")
    @classmethod
    def validate_edge_id(cls, value: str | None) -> str | None:
        if value is not None:
            _safe_string(value, pattern=_SAFE_IDENTIFIER, label="Draft Canvas edge ID")
        return value


class WorkflowDraftCanvasV1(_StrictDefinitionContract):
    schema_version: StrictLiteralOne
    node_layouts: (
        Annotated[
            tuple[WorkflowDraftNodeLayoutV1, ...],
            BeforeValidator(_freeze_json_array),
            Field(max_length=10_000),
        ]
        | None
    ) = None
    edge_layouts: (
        Annotated[
            tuple[WorkflowDraftEdgeLayoutV1, ...],
            BeforeValidator(_freeze_json_array),
            Field(max_length=50_000),
        ]
        | None
    ) = None


class WorkflowDraftSaveRequestV1(_StrictDefinitionContract):
    expected_revision: Annotated[StrictInt, Field(ge=1, le=_MAX_SAFE_INTEGER)]
    spec: WorkflowDraftSpecV1
    canvas: WorkflowDraftCanvasV1


class WorkflowDraftValidateRequestV1(_StrictDefinitionContract):
    expected_revision: Annotated[StrictInt, Field(ge=1, le=_MAX_SAFE_INTEGER)]
    expected_draft_checksum: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]


class WorkflowPublishRequestV1(_StrictDefinitionContract):
    expected_revision: Annotated[StrictInt, Field(ge=1, le=_MAX_SAFE_INTEGER)]
    expected_draft_checksum: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]


class WorkflowDefinitionCreateRequestV1(_StrictDefinitionContract):
    name: Annotated[StrictStr, Field(min_length=1, max_length=255)]
    description: Annotated[StrictStr, Field(max_length=4_096)]

    @field_validator("name")
    @classmethod
    def validate_trimmed_name(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("Workflow Definition name must be trimmed")
        return value


class WorkflowDefinitionUpdateRequestV1(_StrictDefinitionContract):
    expected_revision: Annotated[StrictInt, Field(ge=1, le=_MAX_SAFE_INTEGER)]
    name: Annotated[StrictStr, Field(min_length=1, max_length=255)] | None = None
    description: Annotated[StrictStr, Field(max_length=4_096)] | None = None

    @field_validator("name")
    @classmethod
    def validate_trimmed_name(cls, value: str | None) -> str | None:
        if value is not None and value != value.strip():
            raise ValueError("Workflow Definition name must be trimmed")
        return value

    @model_validator(mode="after")
    def require_change(self) -> Self:
        if self.name is None and self.description is None:
            raise ValueError("Workflow Definition update requires one field")
        return self


class WorkflowDefinitionArchiveRequestV1(_StrictDefinitionContract):
    expected_revision: Annotated[StrictInt, Field(ge=1, le=_MAX_SAFE_INTEGER)]


class WorkflowDefinitionListQueryV1(_StrictDefinitionContract):
    query: Annotated[StrictStr, Field(min_length=1, max_length=255)] | None = None
    lifecycle: Literal["active", "archived"] = "active"
    publication: Literal["all", "draft_only", "published"] = "all"
    sort: Literal["updated_desc", "name_asc", "name_desc"] = "updated_desc"
    cursor: Annotated[StrictStr, Field(min_length=1, max_length=1_024)] | None = None
    limit: Annotated[StrictInt, Field(ge=1, le=100)] = 50

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str | None) -> str | None:
        if value is not None and value != value.strip():
            raise ValueError("Workflow Definition query must be trimmed")
        return value


class WorkflowVersionListQueryV1(_StrictDefinitionContract):
    cursor: Annotated[StrictStr, Field(min_length=1, max_length=1_024)] | None = None
    limit: Annotated[StrictInt, Field(ge=1, le=100)] = 50


class WorkflowCredentialGrantMutationRequestV1(_StrictDefinitionContract):
    credential_id: uuid.UUID
    expected_credential_version_id: uuid.UUID
    expected_slot_schema_checksum: Annotated[
        StrictStr,
        Field(pattern=r"^[0-9a-f]{64}$"),
    ]


class WorkflowDraftResponseV1(_StrictDefinitionContract):
    workflow_id: uuid.UUID
    revision: Annotated[StrictInt, Field(ge=1, le=_MAX_SAFE_INTEGER)]
    spec: _FrozenJsonObject
    canvas: _FrozenJsonObject
    draft_checksum: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    updated_at: datetime


class WorkflowDefinitionResponseV1(_StrictDefinitionContract):
    id: uuid.UUID
    name: Annotated[StrictStr, Field(min_length=1, max_length=255)]
    description: Annotated[StrictStr, Field(max_length=4_096)]
    lifecycle: Literal["active", "archived"]
    publication: Literal["draft_only", "published"]
    revision: Annotated[StrictInt, Field(ge=1, le=_MAX_SAFE_INTEGER)]
    current_published_version_id: uuid.UUID | None
    current_published_version_number: (
        Annotated[
            StrictInt,
            Field(ge=1, le=_MAX_SAFE_INTEGER),
        ]
        | None
    )
    draft_revision: Annotated[StrictInt, Field(ge=1, le=_MAX_SAFE_INTEGER)]
    draft_checksum: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_publication(self) -> Self:
        published = self.current_published_version_id is not None
        if published is not (self.current_published_version_number is not None):
            raise ValueError("Workflow Definition published coordinates disagree")
        if self.publication != ("published" if published else "draft_only"):
            raise ValueError("Workflow Definition publication is not derived")
        return self


class WorkflowDefinitionPageV1(_StrictDefinitionContract):
    items: Annotated[
        tuple[WorkflowDefinitionResponseV1, ...],
        BeforeValidator(_freeze_json_array),
        Field(max_length=100),
    ]
    next_cursor: Annotated[StrictStr, Field(min_length=1, max_length=1_024)] | None


class WorkflowVersionResponseV1(_StrictDefinitionContract):
    id: uuid.UUID
    workflow_id: uuid.UUID
    version_number: Annotated[StrictInt, Field(ge=1, le=_MAX_SAFE_INTEGER)]
    graph_schema_version: Literal[1]
    canvas_schema_version: Literal[1]
    compiler_contract_version: Literal[1]
    semantic_checksum: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    spec: _FrozenJsonObject
    canvas: _FrozenJsonObject
    credential_slots: Annotated[
        tuple[WorkflowPublishedCredentialSlotV1, ...],
        BeforeValidator(_freeze_json_array),
        Field(max_length=255),
    ]
    missing_required_credential_slot_ids: Annotated[
        tuple[Annotated[StrictStr, Field(min_length=1, max_length=128)], ...],
        BeforeValidator(_freeze_json_array),
        Field(max_length=255),
    ]
    executable: StrictBool
    published_at: datetime

    @model_validator(mode="after")
    def validate_grant_coverage(self) -> Self:
        required = {slot.slot_id for slot in self.credential_slots if slot.required}
        missing = set(self.missing_required_credential_slot_ids)
        if len(missing) != len(self.missing_required_credential_slot_ids):
            raise ValueError("missing Credential slot IDs must be unique")
        if not missing <= required:
            raise ValueError("missing Credential slots must be declared by the Version")
        if self.executable is not (not missing):
            raise ValueError("Workflow Version executable state contradicts grant coverage")
        return self


class WorkflowVersionPageV1(_StrictDefinitionContract):
    items: Annotated[
        tuple[WorkflowVersionResponseV1, ...],
        BeforeValidator(_freeze_json_array),
        Field(max_length=100),
    ]
    next_cursor: Annotated[StrictStr, Field(min_length=1, max_length=1_024)] | None


class WorkflowDraftGrantIntentResponseV1(_StrictDefinitionContract):
    workflow_id: uuid.UUID
    slot_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    slot_schema_checksum: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    credential_id: uuid.UUID
    expected_credential_version_id: uuid.UUID
    updated_at: datetime


class WorkflowDraftGrantIntentDeleteResponseV1(_StrictDefinitionContract):
    workflow_id: uuid.UUID
    slot_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    deleted: Literal[True]


class WorkflowCredentialGrantResponseV1(_StrictDefinitionContract):
    workflow_id: uuid.UUID
    workflow_version_id: uuid.UUID
    slot_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    payload_schema_checksum: Annotated[
        StrictStr,
        Field(pattern=r"^[0-9a-f]{64}$"),
    ]
    credential_id: uuid.UUID
    credential_version_id: uuid.UUID
    status: Literal["active", "revoked"]
    revision: Annotated[StrictInt, Field(ge=1, le=_MAX_SAFE_INTEGER)]
    created_at: datetime
    revoked_at: datetime | None


class WorkflowPublishedModelRefV1(_StrictDefinitionContract):
    node_id: uuid.UUID
    purpose: Literal["primary"]
    logical_model_name: Annotated[StrictStr, Field(min_length=1, max_length=128)]


class WorkflowPublishedCredentialSlotV1(_StrictDefinitionContract):
    slot_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    name: Annotated[StrictStr, Field(min_length=1, max_length=255)]
    purpose: Literal["http_auth"]
    payload_schema: _FrozenJsonObject
    payload_schema_checksum: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    required: Literal[True]


class WorkflowPublishedCodeRequirementV1(_StrictDefinitionContract):
    node_id: uuid.UUID
    runtime_contract: Literal["python3.12-v1"]


class WorkflowPublishedHttpRequirementV1(_StrictDefinitionContract):
    node_id: uuid.UUID
    method: Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]
    endpoint_policy_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    injection_profile_id: Annotated[StrictStr, Field(min_length=1, max_length=128)] | None
    credential_slot_id: Annotated[StrictStr, Field(min_length=1, max_length=128)] | None


class WorkflowPublishedRequirementsV1(_StrictDefinitionContract):
    node_types: Annotated[
        tuple[
            Literal[
                "start",
                "llm",
                "condition",
                "transform",
                "variable_aggregate",
                "loop",
                "http_request",
                "python_code",
                "end",
            ],
            ...,
        ],
        BeforeValidator(_freeze_json_array),
        Field(min_length=2, max_length=9),
    ]
    model_refs: Annotated[
        tuple[WorkflowPublishedModelRefV1, ...],
        BeforeValidator(_freeze_json_array),
        Field(max_length=10_000),
    ]
    code: Annotated[
        tuple[WorkflowPublishedCodeRequirementV1, ...],
        BeforeValidator(_freeze_json_array),
        Field(max_length=10_000),
    ]
    http: Annotated[
        tuple[WorkflowPublishedHttpRequirementV1, ...],
        BeforeValidator(_freeze_json_array),
        Field(max_length=10_000),
    ]
    credential_slots: Annotated[
        tuple[WorkflowPublishedCredentialSlotV1, ...],
        BeforeValidator(_freeze_json_array),
        Field(max_length=255),
    ]
    requires_code: StrictBool
    requires_http: StrictBool
    requires_http_write: StrictBool

    @model_validator(mode="after")
    def validate_derived_flags(self) -> Self:
        if self.requires_code is not bool(self.code):
            raise ValueError("requires_code must be derived from Code requirements")
        if self.requires_http is not bool(self.http):
            raise ValueError("requires_http must be derived from HTTP requirements")
        write_methods = {"POST", "PUT", "PATCH", "DELETE"}
        if self.requires_http_write is not any(item.method in write_methods for item in self.http):
            raise ValueError("requires_http_write must be derived from HTTP methods")
        identities = [item.node_id for item in self.model_refs]
        if len(identities) != len(set(identities)):
            raise ValueError("published Model refs must be unique per node")
        slot_ids = [item.slot_id for item in self.credential_slots]
        if len(slot_ids) != len(set(slot_ids)):
            raise ValueError("published Credential slots must be unique")
        return self


class WorkflowDraftValidationResponseV1(_StrictDefinitionContract):
    request_id: Annotated[StrictStr, Field(min_length=1, max_length=512)]
    workflow_id: uuid.UUID
    draft_revision: Annotated[StrictInt, Field(ge=1, le=_MAX_SAFE_INTEGER)]
    draft_checksum: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    valid: StrictBool
    issues: Annotated[
        tuple[WorkflowValidationIssueV1, ...],
        BeforeValidator(_freeze_json_array),
        Field(max_length=1_024),
    ]
    semantic_checksum: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")] | None
    requirements: WorkflowPublishedRequirementsV1 | None
    catalog_generation: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")] | None
    policy_revision: Annotated[StrictInt, Field(ge=1, le=_MAX_SAFE_INTEGER)] | None

    @field_validator("request_id")
    @classmethod
    def validate_request_id(cls, value: str) -> str:
        return _safe_string(value, pattern=_SAFE_REQUEST_ID, label="request ID")

    @model_validator(mode="after")
    def validate_result_shape(self) -> Self:
        successful = not self.issues and self.semantic_checksum is not None and self.requirements is not None and self.catalog_generation is not None and self.policy_revision is not None
        if self.valid is not successful:
            raise ValueError("Workflow validation result fields contradict valid")
        if not self.valid and (not self.issues or self.semantic_checksum is not None or self.requirements is not None):
            raise ValueError("invalid Workflow validation must carry only safe issues")
        return self


class WorkflowPublishResponseV1(_StrictDefinitionContract):
    request_id: Annotated[StrictStr, Field(min_length=1, max_length=512)]
    workflow_id: uuid.UUID
    version_id: uuid.UUID
    version_number: Annotated[StrictInt, Field(ge=1, le=_MAX_SAFE_INTEGER)]
    graph_schema_version: Literal[1]
    canvas_schema_version: Literal[1]
    compiler_contract_version: Literal[1]
    semantic_checksum: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    spec: _FrozenJsonObject
    canvas: _FrozenJsonObject
    credential_slots: Annotated[
        tuple[WorkflowPublishedCredentialSlotV1, ...],
        BeforeValidator(_freeze_json_array),
        Field(max_length=255),
    ]
    missing_required_credential_slot_ids: Annotated[
        tuple[Annotated[StrictStr, Field(min_length=1, max_length=128)], ...],
        BeforeValidator(_freeze_json_array),
        Field(max_length=255),
    ]
    executable: StrictBool
    published_at: datetime

    @field_validator("request_id")
    @classmethod
    def validate_request_id(cls, value: str) -> str:
        return _safe_string(value, pattern=_SAFE_REQUEST_ID, label="request ID")

    @model_validator(mode="after")
    def validate_grant_coverage(self) -> Self:
        required = {slot.slot_id for slot in self.credential_slots if slot.required}
        missing = set(self.missing_required_credential_slot_ids)
        if len(missing) != len(self.missing_required_credential_slot_ids):
            raise ValueError("missing Credential slot IDs must be unique")
        if not missing <= required:
            raise ValueError("missing Credential slots must be declared by the Version")
        if self.executable is not (not missing):
            raise ValueError("Workflow Version executable state contradicts grant coverage")
        return self


WorkflowVersionResponseV1.model_rebuild()

type WorkflowDefinitionPublicResponseV1 = (
    WorkflowCredentialGrantResponseV1
    | WorkflowDefinitionPageV1
    | WorkflowDefinitionResponseV1
    | WorkflowDraftGrantIntentDeleteResponseV1
    | WorkflowDraftGrantIntentResponseV1
    | WorkflowDraftResponseV1
    | WorkflowDraftValidationResponseV1
    | WorkflowPublishResponseV1
    | WorkflowVersionPageV1
    | WorkflowVersionResponseV1
)


def workflow_definition_response_public_projection_v1(
    value: WorkflowDefinitionPublicResponseV1,
) -> dict[str, object]:
    """Project one exact server DTO back to ordinary JSON containers."""

    if type(value) not in {
        WorkflowCredentialGrantResponseV1,
        WorkflowDefinitionPageV1,
        WorkflowDefinitionResponseV1,
        WorkflowDraftGrantIntentDeleteResponseV1,
        WorkflowDraftGrantIntentResponseV1,
        WorkflowDraftResponseV1,
        WorkflowDraftValidationResponseV1,
        WorkflowPublishResponseV1,
        WorkflowVersionPageV1,
        WorkflowVersionResponseV1,
    }:
        raise TypeError("exact Workflow Definition response DTO is required")
    projection = value.model_dump(
        mode="python",
        by_alias=True,
        exclude_unset=False,
        exclude_defaults=False,
    )
    materialized = _materialize_public_arrays(projection)
    if type(materialized) is not dict:  # pragma: no cover - model invariant
        raise AssertionError("Workflow Definition projection must be an object")
    return materialized


def _materialize_public_arrays(value: object) -> object:
    """Thaw tuple authority without stringifying UUID/timestamp response types."""

    if type(value) is tuple:
        return [_materialize_public_arrays(item) for item in value]
    if type(value) is list:
        return [_materialize_public_arrays(item) for item in value]
    if type(value) is dict:
        return {key: _materialize_public_arrays(item) for key, item in value.items()}
    return value


def _trusted_definition_contract_validate(
    model_type: type[_StrictDefinitionContract],
    payload: dict[str, object],
) -> _StrictDefinitionContract:
    """Validate one server-derived projection through the tuple-only seam.

    Public request/response parsing never receives the unexported context
    sentinel and therefore accepts only actual JSON-array ``list`` values.
    The closed model allowlist prevents this helper from becoming a generic
    bypass for transport contracts.
    """

    if model_type not in {
        WorkflowDefinitionPageV1,
        WorkflowDraftValidationResponseV1,
        WorkflowPublishResponseV1,
        WorkflowPublishedRequirementsV1,
        WorkflowVersionPageV1,
        WorkflowVersionResponseV1,
    }:
        raise TypeError("unsupported trusted Workflow Definition projection")
    if type(payload) is not dict:
        raise TypeError("trusted Workflow Definition projection requires a dict")
    return model_type.model_validate(
        payload,
        context=_trusted_array_context(),
    )


def workflow_draft_spec_public_projection_v1(
    value: WorkflowDraftSpecV1,
) -> dict[str, object]:
    if type(value) is not WorkflowDraftSpecV1:
        raise TypeError("exact WorkflowDraftSpecV1 is required")
    return value.model_dump(mode="json", by_alias=True, exclude_unset=True)


def workflow_draft_canvas_public_projection_v1(
    value: WorkflowDraftCanvasV1,
) -> dict[str, object]:
    if type(value) is not WorkflowDraftCanvasV1:
        raise TypeError("exact WorkflowDraftCanvasV1 is required")
    return value.model_dump(mode="json", by_alias=True, exclude_unset=True)


__all__ = [
    "WorkflowCredentialGrantMutationRequestV1",
    "WorkflowCredentialGrantResponseV1",
    "WorkflowDefinitionArchiveRequestV1",
    "WorkflowDefinitionCreateRequestV1",
    "WorkflowDefinitionListQueryV1",
    "WorkflowDefinitionPageV1",
    "WorkflowDefinitionResponseV1",
    "WorkflowDefinitionUpdateRequestV1",
    "WorkflowDraftCanvasV1",
    "WorkflowDraftGrantIntentDeleteResponseV1",
    "WorkflowDraftGrantIntentResponseV1",
    "WorkflowDraftResponseV1",
    "WorkflowDraftSaveRequestV1",
    "WorkflowDraftSpecV1",
    "WorkflowDraftValidateRequestV1",
    "WorkflowDraftValidationResponseV1",
    "WorkflowPublishRequestV1",
    "WorkflowPublishResponseV1",
    "WorkflowPublishedCodeRequirementV1",
    "WorkflowPublishedCredentialSlotV1",
    "WorkflowPublishedHttpRequirementV1",
    "WorkflowPublishedModelRefV1",
    "WorkflowPublishedRequirementsV1",
    "WorkflowVersionListQueryV1",
    "WorkflowVersionPageV1",
    "WorkflowVersionResponseV1",
    "workflow_definition_response_public_projection_v1",
    "workflow_draft_canvas_public_projection_v1",
    "workflow_draft_spec_public_projection_v1",
]
