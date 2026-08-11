"""Strict, app-independent contracts for authored Workflow documents.

This module intentionally freezes only the serializable v1 shapes.  Graph
topology, registry compatibility, publish-time binding validation, and runtime
policy admission belong to later compiler/application layers.
"""

from __future__ import annotations

import math
import unicodedata
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, Strict, field_validator, model_validator

MAX_SAFE_JSON_INTEGER = 9_007_199_254_740_991


def _validate_canonical_uuid_string(value: str) -> str:
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("node identity must be a canonical lowercase UUID string") from error
    if str(parsed) != value:
        raise ValueError("node identity must be a canonical lowercase UUID string")
    return value


def _require_portable_numbers(value: object) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_JSON_INTEGER:
            raise ValueError("JSON integer exceeds the cross-runtime safe range")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON number must be finite")
        if value.is_integer() and abs(value) > MAX_SAFE_JSON_INTEGER:
            raise ValueError("JSON integer exceeds the cross-runtime safe range")
        return
    if isinstance(value, list | tuple):
        for item in value:
            _require_portable_numbers(item)
        return
    if isinstance(value, dict):
        for item in value.values():
            _require_portable_numbers(item)


def _require_unicode_scalars(value: object) -> None:
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ValueError("Workflow strings must contain only Unicode scalar values")
        return
    if isinstance(value, list | tuple):
        for item in value:
            _require_unicode_scalars(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _require_unicode_scalars(key)
            _require_unicode_scalars(item)


def _normalize_unicode_json(value: object) -> object:
    """Normalize authored contract text before identity or checksum use.

    Object-key collisions are rejected at the transport boundary instead of
    being left for a later IR projection to overwrite silently.
    """

    if isinstance(value, str):
        normalized = unicodedata.normalize("NFC", value)
        _require_unicode_scalars(normalized)
        return normalized
    if isinstance(value, list):
        return [_normalize_unicode_json(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_normalize_unicode_json(item) for item in value)
    if isinstance(value, dict):
        normalized: dict[object, object] = {}
        for key, item in value.items():
            normalized_key = _normalize_unicode_json(key)
            if normalized_key in normalized:
                raise ValueError("Unicode normalization produced duplicate Workflow object keys")
            normalized[normalized_key] = _normalize_unicode_json(item)
        return normalized
    return value


def _validate_json_number(value: int | float) -> int | float:
    _require_portable_numbers(value)
    return value


def _validate_strict_literal_one(value: object) -> object:
    if type(value) is not int:
        raise ValueError("contract version must be the JSON integer 1")
    return value


class WorkflowContractModel(BaseModel):
    """Common fail-closed configuration for every nested Workflow DTO."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        revalidate_instances="always",
        serialize_by_alias=True,
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_cross_runtime_text(cls, value: object) -> object:
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="python", by_alias=True, exclude_unset=True)
        return _normalize_unicode_json(value)

    @model_validator(mode="after")
    def _validate_cross_runtime_numbers(self) -> WorkflowContractModel:
        for field_name in type(self).model_fields:
            value = getattr(self, field_name)
            _require_portable_numbers(value)
            _require_unicode_scalars(value)
        return self


type JsonNumber = Annotated[
    Annotated[int, Strict()] | Annotated[float, Strict(), Field(allow_inf_nan=False)],
    AfterValidator(_validate_json_number),
]
type StrictLiteralOne = Annotated[Literal[1], BeforeValidator(_validate_strict_literal_one)]
type JsonValue = None | bool | JsonNumber | str | list[JsonValue] | dict[str, JsonValue]
type JsonSchema = dict[str, JsonValue]
type NonEmptyString = Annotated[str, Strict(), Field(min_length=1)]
type NonNegativeInteger = Annotated[int, Strict(), Field(ge=0)]
type PositiveInteger = Annotated[int, Strict(), Field(gt=0)]
type BoundedJsonPointer = Annotated[str, Strict(), Field(max_length=2048)]
type PortTitle = Annotated[str, Strict(), Field(min_length=1, max_length=128)]
type PythonIdentifier = Annotated[str, Strict(), Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
type WorkflowNodeId = Annotated[str, Strict(), AfterValidator(_validate_canonical_uuid_string)]
type WorkflowEdgeId = Annotated[
    str,
    Strict(),
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"),
]
type WorkflowInputId = Annotated[
    str,
    Strict(),
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9_.:-]*$"),
]
type WorkflowOutputId = Annotated[
    str,
    Strict(),
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z_][A-Za-z0-9_.:-]*$"),
]
type WorkflowPortId = Annotated[
    str,
    Strict(),
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"),
]
type WorkflowCredentialSlotId = Annotated[
    str,
    Strict(),
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z_][A-Za-z0-9_.:-]*$"),
]
type WorkflowNodeKind = Literal[
    "start",
    "llm",
    "condition",
    "transform",
    "variable_aggregate",
    "loop",
    "http_request",
    "python_code",
    "end",
]
WORKFLOW_NODE_KINDS: tuple[WorkflowNodeKind, ...] = (
    "start",
    "llm",
    "condition",
    "transform",
    "variable_aggregate",
    "loop",
    "http_request",
    "python_code",
    "end",
)


class WorkflowValueType(WorkflowContractModel):
    kind: Literal[
        "string",
        "number",
        "boolean",
        "json",
        "messages",
    ]
    collection: bool
    nullable: bool
    schema_ref: NonEmptyString | None = None

    @field_validator("schema_ref", mode="before")
    @classmethod
    def _reject_explicit_null_schema_ref(cls, value: object) -> object:
        if value is None:
            raise ValueError("schema_ref may be omitted but not null")
        return value


class LiteralValueBinding(WorkflowContractModel):
    kind: Literal["literal"]
    value: JsonValue


class WorkflowInputValueBinding(WorkflowContractModel):
    kind: Literal["workflow_input"]
    input_id: WorkflowInputId


class LoopVariableValueBinding(WorkflowContractModel):
    kind: Literal["loop_variable"]
    loop_node_id: WorkflowNodeId
    variable_id: NonEmptyString


class NodeOutputValueBinding(WorkflowContractModel):
    kind: Literal["node_output"]
    node_id: WorkflowNodeId
    output_id: WorkflowPortId
    path: BoundedJsonPointer | None = None

    @field_validator("path", mode="before")
    @classmethod
    def _reject_explicit_null_path(cls, value: object) -> object:
        if value is None:
            raise ValueError("path may be omitted but not null")
        return value


ValueBinding = Annotated[
    LiteralValueBinding | WorkflowInputValueBinding | LoopVariableValueBinding | NodeOutputValueBinding,
    Field(discriminator="kind"),
]


class NoWorkflowInputConstraintsV1(WorkflowContractModel):
    kind: Literal["none"]


class StringWorkflowInputConstraintsV1(WorkflowContractModel):
    kind: Literal["string"]
    min_length: NonNegativeInteger | None = None
    max_length: NonNegativeInteger | None = None
    pattern: str | None = None

    @field_validator("min_length", "max_length", "pattern", mode="before")
    @classmethod
    def _reject_explicit_null_options(cls, value: object) -> object:
        if value is None:
            raise ValueError("constraint option may be omitted but not null")
        return value


class NumberWorkflowInputConstraintsV1(WorkflowContractModel):
    kind: Literal["number"]
    minimum: JsonNumber | None = None
    maximum: JsonNumber | None = None

    @field_validator("minimum", "maximum", mode="before")
    @classmethod
    def _reject_explicit_null_options(cls, value: object) -> object:
        if value is None:
            raise ValueError("constraint option may be omitted but not null")
        return value


class EnumWorkflowInputConstraintsV1(WorkflowContractModel):
    kind: Literal["enum"]
    options: list[JsonValue]


WorkflowInputConstraintsV1 = Annotated[
    NoWorkflowInputConstraintsV1 | StringWorkflowInputConstraintsV1 | NumberWorkflowInputConstraintsV1 | EnumWorkflowInputConstraintsV1,
    Field(discriminator="kind"),
]


class WorkflowInputDecl(WorkflowContractModel):
    id: WorkflowInputId
    name: PortTitle
    label: PortTitle | None
    description: str | None
    value_type: WorkflowValueType
    required: bool
    default: JsonValue = None
    constraints: WorkflowInputConstraintsV1


class WorkflowOutputDecl(WorkflowContractModel):
    id: WorkflowOutputId
    name: NonEmptyString
    description: str | None
    value_type: WorkflowValueType
    source: ValueBinding | None
    default: JsonValue = None


class WorkflowCredentialSlotDecl(WorkflowContractModel):
    id: WorkflowCredentialSlotId
    name: NonEmptyString
    purpose: Literal["http_auth"]
    payload_schema: JsonSchema
    required: Literal[True]

    @field_validator("required", mode="before")
    @classmethod
    def _require_strict_true(cls, value: object) -> object:
        if type(value) is not bool or value is not True:
            raise ValueError("credential slot required must be the boolean true literal")
        return value


class RootWorkflowNodeScope(WorkflowContractModel):
    kind: Literal["root"]


class LoopBodyWorkflowNodeScope(WorkflowContractModel):
    kind: Literal["loop_body"]
    loop_node_id: WorkflowNodeId


WorkflowNodeScope = Annotated[
    RootWorkflowNodeScope | LoopBodyWorkflowNodeScope,
    Field(discriminator="kind"),
]


class ControlTransitionEndpoint(WorkflowContractModel):
    node_id: WorkflowNodeId
    port_id: WorkflowPortId


class ControlTransition(WorkflowContractModel):
    id: WorkflowEdgeId
    source: ControlTransitionEndpoint
    target: ControlTransitionEndpoint


class NoRetryPolicyV1(WorkflowContractModel):
    mode: Literal["none"]


class BoundedRetryPolicyV1(WorkflowContractModel):
    mode: Literal["bounded"]
    max_attempts: PositiveInteger
    backoff_ms: NonNegativeInteger


RetryPolicyV1 = Annotated[
    NoRetryPolicyV1 | BoundedRetryPolicyV1,
    Field(discriminator="mode"),
]


class FailWorkflowOnErrorV1(WorkflowContractModel):
    mode: Literal["fail_workflow"]


class RouteErrorOnErrorV1(WorkflowContractModel):
    mode: Literal["route_error"]
    output_port_id: Literal["error"]


class ContinueWithTypedDefaultOnErrorV1(WorkflowContractModel):
    mode: Literal["continue_with_typed_default"]
    value: JsonValue


OnErrorPolicyV1 = Annotated[
    FailWorkflowOnErrorV1 | RouteErrorOnErrorV1 | ContinueWithTypedDefaultOnErrorV1,
    Field(discriminator="mode"),
]


class NodeExecutionPolicyV1(WorkflowContractModel):
    retry: RetryPolicyV1
    on_error: OnErrorPolicyV1


class PredicateClause(WorkflowContractModel):
    left: ValueBinding
    operator: Literal[
        "eq",
        "ne",
        "gt",
        "gte",
        "lt",
        "lte",
        "contains",
        "starts_with",
        "ends_with",
        "is_null",
        "is_not_null",
    ]
    right: ValueBinding | None = None

    @field_validator("right", mode="before")
    @classmethod
    def _reject_explicit_null_right(cls, value: object) -> object:
        if value is None:
            raise ValueError("right may be omitted but not null")
        return value


class PredicateAst(WorkflowContractModel):
    op: Literal["and", "or"]
    items: list[PredicateAst | PredicateClause]


class TextTemplateSegment(WorkflowContractModel):
    kind: Literal["text"]
    value: str


class BindingTemplateSegment(WorkflowContractModel):
    kind: Literal["binding"]
    value: ValueBinding


RestrictedTemplateSegment = Annotated[
    TextTemplateSegment | BindingTemplateSegment,
    Field(discriminator="kind"),
]


class RestrictedTemplate(WorkflowContractModel):
    version: StrictLiteralOne
    segments: list[RestrictedTemplateSegment]


class RestrictedJsonTemplate(WorkflowContractModel):
    version: StrictLiteralOne
    template: JsonValue
    bindings: dict[str, ValueBinding]


class HttpKeyValueBinding(WorkflowContractModel):
    id: NonEmptyString
    name: NonEmptyString
    value: ValueBinding | RestrictedTemplate


class NoHttpRequestAuthV1(WorkflowContractModel):
    mode: Literal["none"]


class EndpointProfileHttpRequestAuthV1(WorkflowContractModel):
    mode: Literal["endpoint_profile"]
    injection_profile_id: NonEmptyString
    credential_slot_id: WorkflowCredentialSlotId


HttpRequestAuthV1 = Annotated[
    NoHttpRequestAuthV1 | EndpointProfileHttpRequestAuthV1,
    Field(discriminator="mode"),
]


class StartNodeConfigV1(WorkflowContractModel):
    pass


class LlmMessageV1(WorkflowContractModel):
    id: NonEmptyString
    role: Literal["system", "user", "assistant"]
    content: RestrictedTemplate


class LlmStructuredOutputV1(WorkflowContractModel):
    enabled: bool
    schema_: JsonSchema | None = Field(alias="schema")


class LlmNodeConfigV1(WorkflowContractModel):
    model_ref: NonEmptyString
    mode: Literal["chat", "completion"]
    context_input_ids: list[NonEmptyString]
    messages: list[LlmMessageV1]
    model_parameters: dict[str, JsonValue]
    stream: bool
    reasoning_output: Literal["omit", "provider_summary"]
    structured_output: LlmStructuredOutputV1


class ConditionBranchV1(WorkflowContractModel):
    id: PortTitle
    output_port_id: WorkflowPortId
    label: PortTitle | None
    predicate: PredicateAst


class ConditionNodeConfigV1(WorkflowContractModel):
    branches: Annotated[list[ConditionBranchV1], Field(min_length=1, max_length=254)]
    else_output_port_id: WorkflowPortId

    @model_validator(mode="after")
    def _require_resolvable_output_ports(self) -> ConditionNodeConfigV1:
        output_port_ids = [branch.output_port_id for branch in self.branches]
        if len(output_port_ids) != len(set(output_port_ids)):
            raise ValueError("Condition branch output port ids must be unique")
        if self.else_output_port_id in output_port_ids:
            raise ValueError("Condition ELSE output port id must be distinct from branch ports")
        if "error" in {*output_port_ids, self.else_output_port_id}:
            raise ValueError("Condition derived output port conflicts with the fixed error port")
        return self


class TransformInputVariableV1(WorkflowContractModel):
    id: NonEmptyString
    name: NonEmptyString
    value_type: WorkflowValueType


class TransformNodeConfigV1(WorkflowContractModel):
    input_variables: list[TransformInputVariableV1]
    missing_variable: Literal["error", "null", "empty"]
    mode: Literal["text", "json"]
    template: RestrictedTemplate | RestrictedJsonTemplate
    output_schema: JsonSchema | None

    @model_validator(mode="after")
    def _match_template_and_schema_to_mode(self) -> TransformNodeConfigV1:
        if self.mode == "text":
            if not isinstance(self.template, RestrictedTemplate) or self.output_schema is not None:
                raise ValueError("text transform requires a text template and null output_schema")
        elif not isinstance(self.template, RestrictedJsonTemplate) or self.output_schema is None:
            raise ValueError("json transform requires a JSON template and output_schema")
        return self


class EndNodeConfigV1(WorkflowContractModel):
    pass


class VariableAggregateGroupV1(WorkflowContractModel):
    id: WorkflowPortId
    name: PortTitle
    value_type: WorkflowValueType
    candidate_input_ids: list[NonEmptyString]


class VariableAggregateNodeConfigV1(WorkflowContractModel):
    strategy: Literal["exclusive_branch"]
    groups: Annotated[list[VariableAggregateGroupV1], Field(min_length=1, max_length=254)]

    @model_validator(mode="after")
    def _require_resolvable_output_ports(self) -> VariableAggregateNodeConfigV1:
        output_port_ids = [group.id for group in self.groups]
        if len(output_port_ids) != len(set(output_port_ids)):
            raise ValueError("Variable Aggregate output port ids must be unique")
        if {"next", "error"} & set(output_port_ids):
            raise ValueError("Variable Aggregate derived output port conflicts with a fixed port")
        return self


class LoopVariableV1(WorkflowContractModel):
    id: NonEmptyString
    name: PortTitle
    value_type: WorkflowValueType
    initial_input_id: NonEmptyString
    next_input_id: NonEmptyString
    output_port_id: WorkflowPortId


class LoopNodeConfigV1(WorkflowContractModel):
    mode: Literal["do_until"]
    body_entry_node_id: WorkflowNodeId
    body_exit_node_id: WorkflowNodeId
    max_iterations: PositiveInteger
    termination_condition: PredicateAst
    variables: Annotated[list[LoopVariableV1], Field(min_length=1, max_length=252)]

    @model_validator(mode="after")
    def _require_resolvable_output_ports(self) -> LoopNodeConfigV1:
        output_port_ids = [variable.output_port_id for variable in self.variables]
        if len(output_port_ids) != len(set(output_port_ids)):
            raise ValueError("Loop variable output port ids must be unique")
        if {"body", "next", "error", "iteration_count"} & set(output_port_ids):
            raise ValueError("Loop variable output port conflicts with a fixed port")
        return self


class NoHttpBodyV1(WorkflowContractModel):
    kind: Literal["none"]


class JsonHttpBodyV1(WorkflowContractModel):
    kind: Literal["json"]
    template: RestrictedJsonTemplate


class FormUrlencodedHttpBodyV1(WorkflowContractModel):
    kind: Literal["form_urlencoded"]
    fields: list[HttpKeyValueBinding]


class MultipartTextHttpBodyV1(WorkflowContractModel):
    kind: Literal["multipart_text"]
    fields: list[HttpKeyValueBinding]


class RawTextHttpBodyV1(WorkflowContractModel):
    kind: Literal["raw_text"]
    content_type: NonEmptyString
    template: RestrictedTemplate


HttpRequestBodyV1 = Annotated[
    NoHttpBodyV1 | JsonHttpBodyV1 | FormUrlencodedHttpBodyV1 | MultipartTextHttpBodyV1 | RawTextHttpBodyV1,
    Field(discriminator="kind"),
]


class HttpRequestTimeoutV1(WorkflowContractModel):
    connect_ms: NonNegativeInteger | None
    read_ms: NonNegativeInteger | None
    write_ms: NonNegativeInteger | None


class HttpAcceptedStatusRangeV1(WorkflowContractModel):
    from_: Annotated[int, Strict(), Field(ge=100, le=599)] = Field(alias="from")
    to: Annotated[int, Strict(), Field(ge=100, le=599)]

    @model_validator(mode="after")
    def _require_ordered_range(self) -> HttpAcceptedStatusRangeV1:
        if self.from_ > self.to:
            raise ValueError("accepted status range must be ordered")
        return self


class HttpRequestResponseV1(WorkflowContractModel):
    mode: Literal["json", "text"]
    accepted_statuses: list[HttpAcceptedStatusRangeV1]
    schema_: JsonSchema | None = Field(alias="schema")


class HttpRequestNodeConfigV1(WorkflowContractModel):
    method: Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]
    base_origin: NonEmptyString
    path_template: RestrictedTemplate
    query: list[HttpKeyValueBinding]
    headers: list[HttpKeyValueBinding]
    auth: HttpRequestAuthV1
    body: HttpRequestBodyV1
    timeout: HttpRequestTimeoutV1
    response: HttpRequestResponseV1


class PythonCodeInputVariableV1(WorkflowContractModel):
    id: WorkflowNodeId
    name: PythonIdentifier
    value_type: WorkflowValueType


class PythonCodeNodeConfigV1(WorkflowContractModel):
    source: str
    input_variables: list[PythonCodeInputVariableV1]
    output_schema: JsonSchema
    timeout_ms: PositiveInteger | None


class WorkflowNodeSpecBase(WorkflowContractModel):
    id: WorkflowNodeId
    type_version: StrictLiteralOne
    scope: WorkflowNodeScope
    custom_label: str | None
    description: str | None
    input_bindings: dict[str, ValueBinding | None]
    execution_policy: NodeExecutionPolicyV1


class StartWorkflowNodeSpec(WorkflowNodeSpecBase):
    type: Literal["start"]
    config: StartNodeConfigV1


class LlmWorkflowNodeSpec(WorkflowNodeSpecBase):
    type: Literal["llm"]
    config: LlmNodeConfigV1


class ConditionWorkflowNodeSpec(WorkflowNodeSpecBase):
    type: Literal["condition"]
    config: ConditionNodeConfigV1


class TransformWorkflowNodeSpec(WorkflowNodeSpecBase):
    type: Literal["transform"]
    config: TransformNodeConfigV1


class VariableAggregateWorkflowNodeSpec(WorkflowNodeSpecBase):
    type: Literal["variable_aggregate"]
    config: VariableAggregateNodeConfigV1


class LoopWorkflowNodeSpec(WorkflowNodeSpecBase):
    type: Literal["loop"]
    config: LoopNodeConfigV1


class HttpRequestWorkflowNodeSpec(WorkflowNodeSpecBase):
    type: Literal["http_request"]
    config: HttpRequestNodeConfigV1


class PythonCodeWorkflowNodeSpec(WorkflowNodeSpecBase):
    type: Literal["python_code"]
    config: PythonCodeNodeConfigV1


class EndWorkflowNodeSpec(WorkflowNodeSpecBase):
    type: Literal["end"]
    config: EndNodeConfigV1


WorkflowNodeSpec = Annotated[
    StartWorkflowNodeSpec
    | LlmWorkflowNodeSpec
    | ConditionWorkflowNodeSpec
    | TransformWorkflowNodeSpec
    | VariableAggregateWorkflowNodeSpec
    | LoopWorkflowNodeSpec
    | HttpRequestWorkflowNodeSpec
    | PythonCodeWorkflowNodeSpec
    | EndWorkflowNodeSpec,
    Field(discriminator="type"),
]


class WorkflowSpecV1(WorkflowContractModel):
    schema_version: StrictLiteralOne
    entry_node_id: WorkflowNodeId
    nodes: list[WorkflowNodeSpec]
    transitions: list[ControlTransition]
    workflow_inputs: Annotated[list[WorkflowInputDecl], Field(max_length=255)]
    workflow_outputs: list[WorkflowOutputDecl]
    credential_slots: list[WorkflowCredentialSlotDecl]

    @model_validator(mode="after")
    def _require_resolvable_start_ports_and_node_identities(self) -> WorkflowSpecV1:
        node_ids = [node.id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("Workflow node ids must be unique")
        workflow_input_ids = [declaration.id for declaration in self.workflow_inputs]
        if len(workflow_input_ids) != len(set(workflow_input_ids)):
            raise ValueError("Workflow input ids must be unique")
        if "next" in workflow_input_ids:
            raise ValueError("Workflow input port conflicts with the fixed Start next port")
        return self


class CanvasPositionV1(WorkflowContractModel):
    x: JsonNumber
    y: JsonNumber


class CanvasNodeLayoutV1(WorkflowContractModel):
    node_id: WorkflowNodeId
    position: CanvasPositionV1
    parent_node_id: WorkflowNodeId | None = None
    collapsed: bool | None = None

    @field_validator("parent_node_id", "collapsed", mode="before")
    @classmethod
    def _reject_explicit_null_options(cls, value: object) -> object:
        if value is None:
            raise ValueError("canvas layout option may be omitted but not null")
        return value


class CanvasEdgeLayoutV1(WorkflowContractModel):
    edge_id: WorkflowEdgeId
    routing: Literal["bezier", "smoothstep"]


class CanvasDocumentV1(WorkflowContractModel):
    schema_version: StrictLiteralOne
    node_layouts: list[CanvasNodeLayoutV1]
    edge_layouts: list[CanvasEdgeLayoutV1]


def _public_projection_v1(value: BaseModel) -> dict[str, object]:
    """Freeze the omission/null semantics for public Pydantic DTOs."""

    return value.model_dump(
        mode="json",
        by_alias=True,
        exclude_unset=True,
    )


def workflow_spec_public_projection_v1(value: WorkflowSpecV1) -> dict[str, object]:
    """Project a strict WorkflowSpec without inventing omitted optional fields."""

    return _public_projection_v1(value)


def canvas_document_public_projection_v1(value: CanvasDocumentV1) -> dict[str, object]:
    """Project a strict CanvasDocument with the same omission/null contract."""

    return _public_projection_v1(value)


PredicateAst.model_rebuild()
