"""Shared, app-independent Workflow Node Registry contracts.

This module is the single code authority for the first-batch type/version,
config schemas, bilingual titles, stable and derived instance ports, and
manifest checksum. Application Catalog availability is layered above it.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Final, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_serializer,
    field_validator,
    model_validator,
)

from deerflow.workflows import WORKFLOW_NODE_KINDS, JsonSchema, StrictLiteralOne, WorkflowNodeKind, WorkflowValueType
from deerflow.workflows.contracts import (
    ConditionNodeConfigV1,
    EndNodeConfigV1,
    HttpRequestNodeConfigV1,
    LlmNodeConfigV1,
    LoopNodeConfigV1,
    PythonCodeNodeConfigV1,
    StartNodeConfigV1,
    TransformNodeConfigV1,
    VariableAggregateNodeConfigV1,
    WorkflowNodeId,
    WorkflowNodeSpec,
    WorkflowSpecV1,
)
from deerflow.workflows.json_schema import value_type_from_json_schema

WORKFLOW_NODE_REGISTRY_CONTRACT_VERSION: Final = 1


class _ImmutableWorkflowValueType(WorkflowValueType):
    """Catalog-local immutable form of a port value-type contract."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
        serialize_by_alias=True,
    )


def _freeze_json_authority(value: object) -> object:
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise TypeError("Workflow Catalog JSON authority keys must be strings")
        return MappingProxyType({key: _freeze_json_authority(item) for key, item in value.items()})
    if type(value) in {list, tuple}:
        return tuple(_freeze_json_authority(item) for item in value)
    return value


def _thaw_json_authority(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json_authority(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw_json_authority(item) for item in value]
    return value


FIRST_BATCH_NODE_TITLES: Final[Mapping[str, Mapping[str, str]]] = MappingProxyType(
    {
        node_type: MappingProxyType(titles)
        for node_type, titles in {
            "start": {"zh-CN": "开始", "en-US": "Start"},
            "llm": {"zh-CN": "大模型", "en-US": "LLM"},
            "condition": {"zh-CN": "条件分支", "en-US": "Condition"},
            "transform": {"zh-CN": "模板转换", "en-US": "Template Transform"},
            "variable_aggregate": {"zh-CN": "变量聚合", "en-US": "Variable Aggregate"},
            "loop": {"zh-CN": "循环", "en-US": "Loop"},
            "http_request": {"zh-CN": "HTTP 请求", "en-US": "HTTP Request"},
            "python_code": {"zh-CN": "代码执行", "en-US": "Code Execution"},
            "end": {"zh-CN": "结束", "en-US": "End"},
        }.items()
    }
)

_EXPECTED_RETRY_SEMANTICS: Final = {
    "start": "pure",
    "llm": "read",
    "condition": "pure",
    "transform": "pure",
    "variable_aggregate": "pure",
    "loop": "loop_body_v1",
    "http_request": "http_method_v1",
    "python_code": "isolated_compute",
    "end": "pure",
}
_EXPECTED_CAPABILITIES: Final = {
    "http_request": ["workflow.http.use"],
    "python_code": ["workflow.code.use"],
}

_SafeIdentifier = Annotated[
    StrictStr,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"),
]
_RendererKey = Annotated[
    StrictStr,
    Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$"),
]
_Capability = Annotated[
    StrictStr,
    Field(min_length=1, max_length=128, pattern=r"^workflow\.[a-z][a-z0-9_.]*$"),
]
_SafeReasonCode = Annotated[
    StrictStr,
    Field(min_length=1, max_length=128, pattern=r"^[A-Z][A-Z0-9_]*$"),
]
_Sha256Hex = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
_PublicBytes = Annotated[StrictInt, Field(ge=1, le=2_147_483_648)]
_PublicHttpResponseBytes = Annotated[StrictInt, Field(ge=1, le=2_097_152)]
_PublicMilliseconds = Annotated[StrictInt, Field(ge=1, le=31_536_000_000)]
_PublicLoopIterations = Annotated[StrictInt, Field(ge=1, le=1_000_000)]
_PublicAggregateGroups = Annotated[StrictInt, Field(ge=1, le=254)]
_PublicAggregateCandidates = Annotated[StrictInt, Field(ge=1, le=100_000)]
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class _StrictCatalogModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
        serialize_by_alias=True,
    )


class LocalizedNodeTitle(_StrictCatalogModel):
    zh_cn: Annotated[StrictStr, Field(alias="zh-CN", min_length=1, max_length=128)]
    en_us: Annotated[StrictStr, Field(alias="en-US", min_length=1, max_length=128)]


class PortDefinition(_StrictCatalogModel):
    id: _SafeIdentifier
    title_i18n: LocalizedNodeTitle
    kind: Literal["control", "data"]
    value_type: WorkflowValueType | None
    cardinality: Literal["one", "many"]
    required: StrictBool

    @model_validator(mode="after")
    def validate_port_value_type(self) -> Self:
        if self.kind == "control" and self.value_type is not None:
            raise ValueError("control ports cannot declare a data value type")
        if self.kind == "data" and self.value_type is None:
            raise ValueError("data ports require a Workflow value type")
        if self.value_type is not None:
            object.__setattr__(
                self,
                "value_type",
                _ImmutableWorkflowValueType.model_validate(
                    self.value_type.model_dump(
                        mode="python",
                        by_alias=True,
                        exclude_unset=True,
                    )
                ),
            )
        return self


class PortDerivationV1(_StrictCatalogModel):
    """Closed instructions for deriving instance-only handles.

    These enum values are executable registry semantics, not configurable
    object paths.  A resolver may read only the already validated Workflow v1
    document and the selected node's discriminated config.
    """

    version: StrictLiteralOne
    input_source: Literal["none"]
    output_source: Literal[
        "none",
        "workflow_inputs",
        "condition_branches",
        "llm_result_v1",
        "transform_result_v1",
        "aggregate_groups",
        "loop_variables",
        "http_response_body",
        "python_result_v1",
    ]


_EXPECTED_PORT_DERIVATION: Final[dict[WorkflowNodeKind, str]] = {
    "start": "workflow_inputs",
    "llm": "llm_result_v1",
    "condition": "condition_branches",
    "transform": "transform_result_v1",
    "variable_aggregate": "aggregate_groups",
    "loop": "loop_variables",
    "http_request": "http_response_body",
    "python_code": "python_result_v1",
    "end": "none",
}


class NodeTypeDefinition(_StrictCatalogModel):
    type: WorkflowNodeKind
    version: Annotated[StrictInt, Field(ge=1, le=1)]
    renderer_key: _RendererKey
    title_i18n: LocalizedNodeTitle
    config_schema: JsonSchema
    input_ports: Annotated[tuple[PortDefinition, ...], Field(max_length=256)]
    output_ports: Annotated[tuple[PortDefinition, ...], Field(max_length=256)]
    port_derivation: PortDerivationV1
    required_capabilities: Annotated[tuple[_Capability, ...], Field(max_length=32)]
    retry_semantics: Literal[
        "pure",
        "isolated_compute",
        "read",
        "idempotent_write",
        "unsafe_write",
        "http_method_v1",
        "loop_body_v1",
    ]
    supports_streaming: StrictBool

    @field_validator(
        "input_ports",
        "output_ports",
        "required_capabilities",
        mode="before",
    )
    @classmethod
    def freeze_array_fields(cls, value: object) -> object:
        if type(value) is list:
            return tuple(value)
        if type(value) is tuple:
            return value
        raise ValueError("Node Registry arrays must be JSON arrays or tuples")

    @field_validator("config_schema", mode="before")
    @classmethod
    def thaw_config_schema_for_validation(cls, value: object) -> object:
        return _thaw_json_authority(value)

    @field_serializer("config_schema")
    def serialize_config_schema(self, value: object) -> object:
        return _thaw_json_authority(value)

    @model_validator(mode="after")
    def validate_first_batch_definition(self) -> Self:
        if self.renderer_key != self.type:
            raise ValueError("first-batch renderer_key must equal the stable node type")
        expected_title = FIRST_BATCH_NODE_TITLES[self.type]
        if self.title_i18n.model_dump(by_alias=True) != expected_title:
            raise ValueError("first-batch node titles must match the frozen bilingual catalog")
        if self.retry_semantics != _EXPECTED_RETRY_SEMANTICS[self.type]:
            raise ValueError("node retry semantics do not match the first-batch registry contract")
        expected_capabilities = tuple(_EXPECTED_CAPABILITIES.get(self.type, []))
        if self.required_capabilities != expected_capabilities:
            raise ValueError("node required capabilities do not match the first-batch registry contract")
        if self.supports_streaming is not (self.type == "llm"):
            raise ValueError("only the first-batch LLM node supports streaming")
        if self.port_derivation != PortDerivationV1(
            version=1,
            input_source="none",
            output_source=_EXPECTED_PORT_DERIVATION[self.type],
        ):
            raise ValueError("node port derivation does not match the first-batch registry contract")
        if self.config_schema.get("type") != "object" or self.config_schema.get("additionalProperties") is not False:
            raise ValueError("node config_schema must be a closed object schema")
        for ports in (self.input_ports, self.output_ports):
            port_ids = [port.id for port in ports]
            if len(port_ids) != len(set(port_ids)):
                raise ValueError("node port ids must be unique within one direction")
        object.__setattr__(
            self,
            "config_schema",
            _freeze_json_authority(self.config_schema),
        )
        return self


def _control_port(
    port_id: str,
    zh_cn: str,
    en_us: str,
    *,
    cardinality: Literal["one", "many"],
    required: bool,
) -> dict[str, object]:
    return {
        "id": port_id,
        "title_i18n": {"zh-CN": zh_cn, "en-US": en_us},
        "kind": "control",
        "value_type": None,
        "cardinality": cardinality,
        "required": required,
    }


def _data_port(
    port_id: str,
    zh_cn: str,
    en_us: str,
    *,
    kind: Literal["string", "number", "boolean", "json", "messages"],
    nullable: bool = False,
    cardinality: Literal["one", "many"] = "many",
) -> dict[str, object]:
    return {
        "id": port_id,
        "title_i18n": {"zh-CN": zh_cn, "en-US": en_us},
        "kind": "data",
        "value_type": {
            "kind": kind,
            "collection": False,
            "nullable": nullable,
        },
        "cardinality": cardinality,
        "required": True,
    }


_CONTROL_IN = _control_port("in", "输入", "Input", cardinality="many", required=True)
_CONTROL_NEXT = _control_port("next", "下一步", "Next", cardinality="many", required=True)
_CONTROL_ERROR = _control_port("error", "异常", "Error", cardinality="one", required=False)

_FIRST_BATCH_PORTS: Final[dict[WorkflowNodeKind, tuple[list[dict[str, object]], list[dict[str, object]]]]] = {
    "start": (
        [],
        [_CONTROL_NEXT],
    ),
    "llm": (
        [_CONTROL_IN],
        [
            _CONTROL_NEXT,
            _CONTROL_ERROR,
            _data_port("text", "文本", "Text", kind="string"),
            _data_port("usage", "用量", "Usage", kind="json"),
        ],
    ),
    "condition": (
        [_CONTROL_IN],
        [
            _CONTROL_ERROR,
        ],
    ),
    "transform": (
        [_CONTROL_IN],
        [
            _CONTROL_NEXT,
            _CONTROL_ERROR,
        ],
    ),
    "variable_aggregate": (
        [_CONTROL_IN],
        [
            _CONTROL_NEXT,
            _CONTROL_ERROR,
        ],
    ),
    "loop": (
        [_CONTROL_IN],
        [
            _control_port("body", "循环体", "Loop Body", cardinality="one", required=True),
            _CONTROL_NEXT,
            _CONTROL_ERROR,
            _data_port("iteration_count", "循环次数", "Iteration Count", kind="number"),
        ],
    ),
    "http_request": (
        [_CONTROL_IN],
        [
            _control_port("success", "成功", "Success", cardinality="one", required=True),
            _CONTROL_ERROR,
            _data_port("status_code", "状态码", "Status Code", kind="number"),
            _data_port("headers", "响应头", "Response Headers", kind="json"),
            _data_port("duration_ms", "耗时（毫秒）", "Duration (ms)", kind="number"),
        ],
    ),
    "python_code": (
        [_CONTROL_IN],
        [
            _CONTROL_NEXT,
            _CONTROL_ERROR,
        ],
    ),
    "end": (
        [_CONTROL_IN],
        [],
    ),
}

_NODE_CONFIG_MODELS: Final[Mapping[WorkflowNodeKind, type[BaseModel]]] = {
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


def _tighten_generated_config_schema(value: object) -> None:
    """Align Pydantic's descriptive schema with strict runtime validation."""

    if isinstance(value, list):
        for item in value:
            _tighten_generated_config_schema(item)
        return
    if not isinstance(value, dict):
        return
    for item in value.values():
        _tighten_generated_config_schema(item)

    alternatives = value.get("anyOf")
    if value.get("default") is None and "default" in value and isinstance(alternatives, list):
        non_null = [item for item in alternatives if item != {"type": "null"}]
        if len(non_null) != len(alternatives):
            value["anyOf"] = non_null
            value.pop("default", None)

    definitions = value.get("$defs")
    if not isinstance(definitions, dict):
        return
    json_number = definitions.get("JsonNumber")
    if isinstance(json_number, dict):
        json_number.clear()
        json_number.update(
            {
                "minimum": -9_007_199_254_740_991,
                "maximum": 9_007_199_254_740_991,
                "type": "number",
            }
        )
    workflow_node_id = definitions.get("WorkflowNodeId")
    if isinstance(workflow_node_id, dict):
        workflow_node_id["pattern"] = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


def _node_config_schema_v1(node_type: WorkflowNodeKind) -> JsonSchema:
    schema = _NODE_CONFIG_MODELS[node_type].model_json_schema(
        by_alias=True,
        mode="validation",
    )
    _tighten_generated_config_schema(schema)
    if node_type == "transform":
        schema["allOf"] = [
            {
                "if": {
                    "properties": {"mode": {"const": "text"}},
                    "required": ["mode"],
                },
                "then": {
                    "properties": {
                        "template": {"$ref": "#/$defs/RestrictedTemplate"},
                        "output_schema": {"type": "null"},
                    }
                },
            },
            {
                "if": {
                    "properties": {"mode": {"const": "json"}},
                    "required": ["mode"],
                },
                "then": {
                    "properties": {
                        "template": {"$ref": "#/$defs/RestrictedJsonTemplate"},
                        "output_schema": {"$ref": "#/$defs/JsonSchema"},
                    }
                },
            },
        ]
    custom_validation_rules: dict[WorkflowNodeKind, list[str]] = {
        "condition": ["condition_output_ports_resolvable"],
        "variable_aggregate": ["aggregate_output_ports_resolvable"],
        "loop": ["loop_output_ports_resolvable"],
        "http_request": ["http_accepted_status_ranges_ordered"],
    }
    if node_type in custom_validation_rules:
        schema["x-actweave-validation"] = {
            "version": 1,
            "rules": custom_validation_rules[node_type],
        }
    return schema


def validate_node_config_v1(
    node_type: WorkflowNodeKind,
    config: object,
) -> BaseModel:
    """Strictly validate a single authored config against registry authority."""

    return _NODE_CONFIG_MODELS[node_type].model_validate(config)


def _build_first_batch_node_registry() -> tuple[NodeTypeDefinition, ...]:
    definitions: list[NodeTypeDefinition] = []
    for node_type in WORKFLOW_NODE_KINDS:
        input_ports, output_ports = _FIRST_BATCH_PORTS[node_type]
        definitions.append(
            NodeTypeDefinition.model_validate(
                {
                    "type": node_type,
                    "version": 1,
                    "renderer_key": node_type,
                    "title_i18n": dict(FIRST_BATCH_NODE_TITLES[node_type]),
                    "config_schema": _node_config_schema_v1(node_type),
                    "input_ports": input_ports,
                    "output_ports": output_ports,
                    "port_derivation": {
                        "version": 1,
                        "input_source": "none",
                        "output_source": _EXPECTED_PORT_DERIVATION[node_type],
                    },
                    "required_capabilities": _EXPECTED_CAPABILITIES.get(node_type, []),
                    "retry_semantics": _EXPECTED_RETRY_SEMANTICS[node_type],
                    "supports_streaming": node_type == "llm",
                }
            )
        )
    return tuple(definitions)


FIRST_BATCH_NODE_REGISTRY_V1: Final = _build_first_batch_node_registry()
_FIRST_BATCH_NODE_REGISTRY_BY_ID: Final = {(definition.type, definition.version): definition for definition in FIRST_BATCH_NODE_REGISTRY_V1}


class ResolvedNodeInstancePortsV1(_StrictCatalogModel):
    node_id: WorkflowNodeId
    input_ports: Annotated[tuple[PortDefinition, ...], Field(max_length=256)]
    output_ports: Annotated[tuple[PortDefinition, ...], Field(max_length=256)]

    @field_validator("input_ports", "output_ports", mode="before")
    @classmethod
    def freeze_port_collections(cls, value: object) -> object:
        if type(value) is list:
            return tuple(value)
        if type(value) is tuple:
            return value
        raise ValueError("resolved Workflow ports must be JSON arrays or tuples")


class ResolvedWorkflowInstancePortsV1(_StrictCatalogModel):
    schema_version: StrictLiteralOne
    nodes: tuple[ResolvedNodeInstancePortsV1, ...]

    @field_validator("nodes", mode="before")
    @classmethod
    def freeze_nodes(cls, value: object) -> object:
        if type(value) is list:
            return tuple(value)
        if type(value) is tuple:
            return value
        raise ValueError("resolved Workflow nodes must be a JSON array or tuple")


def _dynamic_title(value: str) -> dict[str, str]:
    return {"zh-CN": value, "en-US": value}


def _derived_control_port(port_id: str, title: str) -> PortDefinition:
    return PortDefinition.model_validate(_control_port(port_id, title, title, cardinality="one", required=True))


def _derived_data_port(port_id: str, title: str, value_type: object) -> PortDefinition:
    if isinstance(value_type, BaseModel):
        value_type = _public_catalog_projection_v1(value_type)
    return PortDefinition.model_validate(
        {
            "id": port_id,
            "title_i18n": _dynamic_title(title),
            "kind": "data",
            "value_type": value_type,
            "cardinality": "many",
            "required": True,
        }
    )


def _derive_instance_output_ports(
    workflow: WorkflowSpecV1,
    node: object,
    definition: NodeTypeDefinition,
) -> list[PortDefinition]:
    source = definition.port_derivation.output_source
    if source == "none":
        return []
    if source == "workflow_inputs":
        return [
            _derived_data_port(
                declaration.id,
                declaration.label if declaration.label is not None else declaration.name,
                declaration.value_type,
            )
            for declaration in workflow.workflow_inputs
        ]
    if source == "condition_branches":
        if not hasattr(node, "config") or not isinstance(node.config, ConditionNodeConfigV1):
            raise ValueError("condition port derivation requires a validated Condition config")
        ports = [
            _derived_control_port(
                branch.output_port_id,
                branch.label if branch.label is not None else branch.id,
            )
            for branch in node.config.branches
        ]
        ports.append(_derived_control_port(node.config.else_output_port_id, "ELSE"))
        return ports
    if source == "llm_result_v1":
        if not hasattr(node, "config") or not isinstance(node.config, LlmNodeConfigV1):
            raise ValueError("LLM port derivation requires a validated LLM config")
        structured = node.config.structured_output
        value_type = (
            {
                "kind": "json",
                "collection": False,
                "nullable": True,
            }
            if not structured.enabled
            else value_type_from_json_schema(
                structured.schema_,  # type: ignore[arg-type]
                require_top_level="object",
            )
        )
        return [
            _derived_data_port(
                "result",
                "结构化结果 / Structured Result",
                value_type,
            )
        ]
    if source == "transform_result_v1":
        if not hasattr(node, "config") or not isinstance(node.config, TransformNodeConfigV1):
            raise ValueError("Transform port derivation requires a validated Transform config")
        value_type = (
            {
                "kind": "string",
                "collection": False,
                "nullable": False,
            }
            if node.config.mode == "text"
            else value_type_from_json_schema(node.config.output_schema)  # type: ignore[arg-type]
        )
        return [
            _derived_data_port(
                "result",
                "转换结果 / Transform Result",
                value_type,
            )
        ]
    if source == "aggregate_groups":
        if not hasattr(node, "config") or not isinstance(node.config, VariableAggregateNodeConfigV1):
            raise ValueError("aggregate port derivation requires a validated Variable Aggregate config")
        return [_derived_data_port(group.id, group.name, group.value_type) for group in node.config.groups]
    if source == "loop_variables":
        if not hasattr(node, "config") or not isinstance(node.config, LoopNodeConfigV1):
            raise ValueError("loop port derivation requires a validated Loop config")
        return [_derived_data_port(variable.output_port_id, variable.name, variable.value_type) for variable in node.config.variables]
    if source == "http_response_body":
        if not hasattr(node, "config") or not isinstance(node.config, HttpRequestNodeConfigV1):
            raise ValueError("HTTP port derivation requires a validated HTTP Request config")
        value_type = (
            {
                "kind": "string",
                "collection": False,
                "nullable": False,
            }
            if node.config.response.mode == "text"
            else value_type_from_json_schema(node.config.response.schema_)  # type: ignore[arg-type]
        )
        return [
            _derived_data_port(
                "body",
                "响应体 / Response Body",
                value_type,
            )
        ]
    if source == "python_result_v1":
        if not hasattr(node, "config") or not isinstance(node.config, PythonCodeNodeConfigV1):
            raise ValueError("Python port derivation requires a validated Python Code config")
        return [
            _derived_data_port(
                "result",
                "执行结果 / Execution Result",
                value_type_from_json_schema(
                    node.config.output_schema,
                    require_top_level="object",
                ),
            )
        ]
    raise AssertionError(f"unhandled closed port derivation source: {source}")


def _combine_fixed_and_derived_ports(
    *,
    fixed: list[PortDefinition],
    derived: list[PortDefinition],
) -> list[PortDefinition]:
    fixed_ids = {port.id for port in fixed}
    derived_ids: set[str] = set()
    for port in derived:
        if port.id in derived_ids:
            raise ValueError(f"duplicate derived port id: {port.id}")
        if port.id in fixed_ids:
            raise ValueError(f"derived port id conflicts with a fixed port: {port.id}")
        derived_ids.add(port.id)
    return [*fixed, *derived]


def _public_catalog_projection_v1(value: BaseModel) -> dict[str, object]:
    """Freeze omission/null semantics for public catalog DTO projections."""

    return value.model_dump(
        mode="json",
        by_alias=True,
        exclude_unset=True,
    )


def _port_validation_payload(port: PortDefinition) -> dict[str, object]:
    return _public_catalog_projection_v1(port)


def _resolve_workflow_node_instance_ports_for_node_v1(
    workflow: WorkflowSpecV1,
    node: WorkflowNodeSpec,
) -> ResolvedNodeInstancePortsV1:
    """Resolve a validated member node without another full node-list scan."""

    definition = _FIRST_BATCH_NODE_REGISTRY_BY_ID.get((node.type, node.type_version))
    if definition is None:
        raise ValueError("Workflow node type/version is not installed")
    output_ports = _combine_fixed_and_derived_ports(
        fixed=list(definition.output_ports),
        derived=_derive_instance_output_ports(workflow, node, definition),
    )
    return ResolvedNodeInstancePortsV1.model_validate(
        {
            "node_id": node.id,
            "input_ports": [_port_validation_payload(port) for port in definition.input_ports],
            "output_ports": [_port_validation_payload(port) for port in output_ports],
        }
    )


def resolve_workflow_node_instance_ports_v1(
    workflow_spec: WorkflowSpecV1,
    node_id: str,
) -> ResolvedNodeInstancePortsV1:
    """Resolve one node without requiring the rest of the authored edges to be valid."""

    workflow = workflow_spec if isinstance(workflow_spec, WorkflowSpecV1) else WorkflowSpecV1.model_validate(workflow_spec)
    node = next((candidate for candidate in workflow.nodes if candidate.id == node_id), None)
    if node is None:
        raise ValueError("Workflow node does not exist")
    return _resolve_workflow_node_instance_ports_for_node_v1(workflow, node)


def resolve_workflow_instance_ports_v1(workflow_spec: object) -> ResolvedWorkflowInstancePortsV1:
    """Resolve every fixed and instance-derived handle from a strict Spec v1.

    Control transitions are checked against the resolved handle set so legacy
    placeholder ids such as ``branch``, ``group``, and ``variable`` cannot be
    compiled accidentally.
    """

    workflow = workflow_spec if isinstance(workflow_spec, WorkflowSpecV1) else WorkflowSpecV1.model_validate(workflow_spec)
    resolved_nodes: list[ResolvedNodeInstancePortsV1] = []
    resolved_by_node_id: dict[str, ResolvedNodeInstancePortsV1] = {}
    for node in workflow.nodes:
        if node.id in resolved_by_node_id:
            raise ValueError(f"duplicate Workflow node id: {node.id}")
        resolved = _resolve_workflow_node_instance_ports_for_node_v1(workflow, node)
        resolved_nodes.append(resolved)
        resolved_by_node_id[node.id] = resolved

    for transition in workflow.transitions:
        source = resolved_by_node_id.get(transition.source.node_id)
        if source is None or not any(port.id == transition.source.port_id and port.kind == "control" for port in source.output_ports):
            raise ValueError(f"transition {transition.id} source does not reference a resolved control port")
        target = resolved_by_node_id.get(transition.target.node_id)
        if target is None or not any(port.id == transition.target.port_id and port.kind == "control" for port in target.input_ports):
            raise ValueError(f"transition {transition.id} target does not reference a resolved control port")

    return ResolvedWorkflowInstancePortsV1.model_validate(
        {
            "schema_version": 1,
            "nodes": [_public_catalog_projection_v1(node) for node in resolved_nodes],
        }
    )


def resolved_workflow_instance_ports_public_projection_v1(
    value: ResolvedWorkflowInstancePortsV1,
) -> dict[str, object]:
    """Project resolved instance ports without turning omissions into nulls."""

    return _public_catalog_projection_v1(value)


def first_batch_node_registry_manifest_v1() -> list[dict[str, object]]:
    """Return the canonical public JSON projection used by every runtime."""

    manifest: list[dict[str, object]] = []
    for definition in FIRST_BATCH_NODE_REGISTRY_V1:
        manifest.append(_public_catalog_projection_v1(definition))
    return manifest


def first_batch_node_registry_manifest_checksum_v1() -> str:
    """Return a deterministic digest of every registry-authority field."""

    payload = json.dumps(
        first_batch_node_registry_manifest_v1(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "FIRST_BATCH_NODE_REGISTRY_V1",
    "FIRST_BATCH_NODE_TITLES",
    "LocalizedNodeTitle",
    "NodeTypeDefinition",
    "PortDefinition",
    "PortDerivationV1",
    "ResolvedNodeInstancePortsV1",
    "ResolvedWorkflowInstancePortsV1",
    "WORKFLOW_NODE_REGISTRY_CONTRACT_VERSION",
    "first_batch_node_registry_manifest_checksum_v1",
    "first_batch_node_registry_manifest_v1",
    "resolve_workflow_instance_ports_v1",
    "resolve_workflow_node_instance_ports_v1",
    "resolved_workflow_instance_ports_public_projection_v1",
    "validate_node_config_v1",
]
