"""Closed JSON Schema subset used by first-batch typed node outputs.

The subset is deliberately smaller than Draft 2020-12.  It has no references,
regular expressions, conditionals, or open-ended applicators, so Registry port
derivation and JSON Pointer typing stay deterministic across Python/TypeScript.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final, Literal

from jsonschema import Draft202012Validator, SchemaError

from deerflow.workflows.canonical import canonical_json_value
from deerflow.workflows.contracts import JsonSchema, WorkflowValueType

INLINE_SCHEMA_REF_PREFIX: Final = "inline-json-schema-v1:sha256:"
MAX_JSON_SCHEMA_DEPTH: Final = 16
MAX_JSON_SCHEMA_PROPERTIES: Final = 256
MAX_JSON_SCHEMA_ITEMS: Final = 10_000
MAX_JSON_SCHEMA_ENUM_VALUES: Final = 256

_PRIMITIVE_TYPES: Final = frozenset({"null", "boolean", "object", "array", "number", "integer", "string"})
_ALLOWED_KEYWORDS: Final = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "enum",
        "const",
        "default",
        "title",
        "description",
        "anyOf",
    }
)


class WorkflowJsonSchemaError(ValueError):
    """An authored schema is outside the closed compiler-v1 subset."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class JsonSchemaTypeShape:
    non_null_type: Literal["boolean", "object", "array", "number", "string"]
    nullable: bool


def _invalid(message: str) -> WorkflowJsonSchemaError:
    return WorkflowJsonSchemaError("WORKFLOW_JSON_SCHEMA_INVALID", message)


def _unsupported(message: str) -> WorkflowJsonSchemaError:
    return WorkflowJsonSchemaError("WORKFLOW_JSON_SCHEMA_UNSUPPORTED_KEYWORD", message)


def _limited(message: str) -> WorkflowJsonSchemaError:
    return WorkflowJsonSchemaError("WORKFLOW_JSON_SCHEMA_LIMIT_EXCEEDED", message)


def _type_names(schema: JsonSchema) -> tuple[str, ...]:
    declared = schema.get("type")
    if isinstance(declared, str):
        names = (declared,)
    elif isinstance(declared, list) and declared and all(isinstance(item, str) for item in declared):
        names = tuple(declared)
    elif "anyOf" in schema:
        alternatives = schema["anyOf"]
        if not isinstance(alternatives, list) or not alternatives:
            raise _invalid("anyOf must contain at least one schema")
        names_list: list[str] = []
        for alternative in alternatives:
            if not isinstance(alternative, dict):
                raise _invalid("anyOf alternatives must be object schemas")
            names_list.extend(_type_names(alternative))
        names = tuple(names_list)
    else:
        raise _invalid("a closed output schema must declare one explicit type")
    if any(name not in _PRIMITIVE_TYPES for name in names):
        raise _invalid("schema type is outside the frozen primitive set")
    if len(names) != len(set(names)):
        raise _invalid("schema type alternatives must be unique")
    return names


def json_schema_type_shape(schema: JsonSchema) -> JsonSchemaTypeShape:
    names = _type_names(schema)
    nullable = "null" in names
    non_null = [name for name in names if name != "null"]
    if len(non_null) != 1:
        raise WorkflowJsonSchemaError(
            "WORKFLOW_JSON_SCHEMA_TOP_LEVEL_TYPE_INVALID",
            "schema must contain exactly one non-null top-level type",
        )
    normalized = "number" if non_null[0] == "integer" else non_null[0]
    return JsonSchemaTypeShape(non_null_type=normalized, nullable=nullable)  # type: ignore[arg-type]


def _validate_schema_node(schema: JsonSchema, *, depth: int) -> None:
    if depth > MAX_JSON_SCHEMA_DEPTH:
        raise _limited("JSON Schema nesting exceeds the compiler limit")
    unknown = set(schema) - _ALLOWED_KEYWORDS
    if unknown:
        raise _unsupported(f"unsupported JSON Schema keyword: {min(unknown)}")
    if "anyOf" in schema and any(
        key in schema
        for key in (
            "type",
            "properties",
            "required",
            "additionalProperties",
            "items",
            "minItems",
            "maxItems",
            "minLength",
            "maxLength",
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
            "enum",
            "const",
        )
    ):
        raise _invalid("anyOf cannot be combined with sibling validation keywords")

    names = _type_names(schema)
    non_null_names = {"number" if name == "integer" else name for name in names if name != "null"}
    if len(non_null_names) > 1:
        raise WorkflowJsonSchemaError(
            "WORKFLOW_JSON_SCHEMA_TOP_LEVEL_TYPE_INVALID",
            "multiple non-null schema alternatives are unsupported",
        )

    alternatives = schema.get("anyOf")
    if isinstance(alternatives, list):
        for alternative in alternatives:
            assert isinstance(alternative, dict)
            _validate_schema_node(alternative, depth=depth + 1)
        return

    properties = schema.get("properties")
    if properties is not None:
        if non_null_names != {"object"} or not isinstance(properties, dict):
            raise _invalid("properties is allowed only on object schemas")
        if len(properties) > MAX_JSON_SCHEMA_PROPERTIES:
            raise _limited("JSON Schema properties exceed the compiler limit")
        for child in properties.values():
            if not isinstance(child, dict):
                raise _invalid("property schemas must be objects")
            _validate_schema_node(child, depth=depth + 1)
    required = schema.get("required")
    if required is not None:
        if non_null_names != {"object"} or not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            raise _invalid("required must be a string array on an object schema")
        if len(required) != len(set(required)) or any(item not in (properties or {}) for item in required):
            raise _invalid("required entries must be unique declared properties")
    additional = schema.get("additionalProperties")
    if additional is not None and (non_null_names != {"object"} or type(additional) is not bool):
        raise _invalid("additionalProperties must be boolean on an object schema")

    items = schema.get("items")
    if items is not None:
        if non_null_names != {"array"} or not isinstance(items, dict):
            raise _invalid("items must be one object schema on an array")
        _validate_schema_node(items, depth=depth + 1)
    for minimum_key, maximum_key, schema_type, upper_bound in (
        ("minItems", "maxItems", "array", MAX_JSON_SCHEMA_ITEMS),
        ("minLength", "maxLength", "string", MAX_JSON_SCHEMA_ITEMS),
    ):
        minimum = schema.get(minimum_key)
        maximum = schema.get(maximum_key)
        for value in (minimum, maximum):
            if value is not None and (type(value) is not int or value < 0 or value > upper_bound):
                raise _limited(f"{minimum_key}/{maximum_key} exceeds the compiler limit")
        if (minimum is not None or maximum is not None) and non_null_names != {schema_type}:
            raise _invalid(f"{minimum_key}/{maximum_key} is invalid for this schema type")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise _invalid(f"{minimum_key}/{maximum_key} bounds must be ordered")

    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        if key in schema and non_null_names != {"number"}:
            raise _invalid(f"{key} is allowed only on number schemas")
    if "minimum" in schema and "maximum" in schema and schema["minimum"] > schema["maximum"]:  # type: ignore[operator]
        raise _invalid("numeric bounds must be ordered")

    enum = schema.get("enum")
    if enum is not None:
        if not isinstance(enum, list) or not enum or len(enum) > MAX_JSON_SCHEMA_ENUM_VALUES:
            raise _limited("enum must contain a bounded non-empty value set")
        canonical = [canonical_json_value(item) for item in enum]
        if len(canonical) != len(set(canonical)):
            raise _invalid("enum values must be unique")


def validate_strict_json_schema(schema: JsonSchema) -> None:
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise _invalid("JSON Schema is not valid Draft 2020-12") from error
    _validate_schema_node(schema, depth=1)
    shape = json_schema_type_shape(schema)
    if "default" in schema and not Draft202012Validator(schema).is_valid(schema["default"]):
        raise _invalid("schema default does not satisfy the schema")
    if "const" in schema and not Draft202012Validator(schema).is_valid(schema["const"]):
        raise _invalid("schema const does not satisfy the schema")
    if shape.non_null_type == "array" and "items" not in schema:
        # Generic arrays are allowed as outputs, but pointer access remains fail-closed.
        return


def inline_json_schema_ref(schema: JsonSchema) -> str:
    validate_strict_json_schema(schema)
    canonical = canonical_json_value(schema).encode("utf-8")
    return INLINE_SCHEMA_REF_PREFIX + hashlib.sha256(canonical).hexdigest()


def value_type_from_json_schema(
    schema: JsonSchema,
    *,
    require_top_level: Literal["any", "object"] = "any",
) -> WorkflowValueType:
    validate_strict_json_schema(schema)
    shape = json_schema_type_shape(schema)
    if require_top_level == "object" and (shape.non_null_type != "object" or shape.nullable):
        raise WorkflowJsonSchemaError(
            "WORKFLOW_JSON_SCHEMA_TOP_LEVEL_TYPE_INVALID",
            "this node output requires a non-null top-level object schema",
        )
    kind = {
        "string": "string",
        "number": "number",
        "boolean": "boolean",
        "object": "json",
        "array": "json",
    }[shape.non_null_type]
    return WorkflowValueType(
        kind=kind,
        collection=shape.non_null_type == "array",
        nullable=shape.nullable,
        schema_ref=inline_json_schema_ref(schema),
    )


def _single_schema(schema: JsonSchema) -> tuple[JsonSchema, bool]:
    shape = json_schema_type_shape(schema)
    alternatives = schema.get("anyOf")
    if not isinstance(alternatives, list):
        return schema, shape.nullable
    for alternative in alternatives:
        assert isinstance(alternative, dict)
        names = _type_names(alternative)
        if any(name != "null" for name in names):
            return alternative, shape.nullable
    raise _invalid("nullable schema is missing its non-null alternative")


def _decode_pointer_token(token: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(token):
        if token[index] != "~":
            result.append(token[index])
            index += 1
            continue
        if index + 1 >= len(token) or token[index + 1] not in {"0", "1"}:
            raise WorkflowJsonSchemaError(
                "WORKFLOW_NODE_OUTPUT_PATH_INVALID",
                "JSON Pointer contains an invalid escape",
            )
        result.append("~" if token[index + 1] == "0" else "/")
        index += 2
    return "".join(result)


def value_type_at_json_pointer(schema: JsonSchema, pointer: str) -> WorkflowValueType:
    validate_strict_json_schema(schema)
    if pointer == "":
        return value_type_from_json_schema(schema)
    if not pointer.startswith("/"):
        raise WorkflowJsonSchemaError(
            "WORKFLOW_NODE_OUTPUT_PATH_INVALID",
            "JSON Pointer must be empty or start with a slash",
        )
    nullable_from_path = False
    current = schema
    for raw_token in pointer[1:].split("/"):
        token = _decode_pointer_token(raw_token)
        current, parent_nullable = _single_schema(current)
        nullable_from_path = nullable_from_path or parent_nullable
        shape = json_schema_type_shape(current)
        if shape.non_null_type == "object":
            properties = current.get("properties")
            if not isinstance(properties, dict) or token not in properties:
                raise WorkflowJsonSchemaError(
                    "WORKFLOW_NODE_OUTPUT_PATH_INVALID",
                    "JSON Pointer does not resolve to a declared property",
                )
            required = current.get("required", [])
            nullable_from_path = nullable_from_path or token not in required
            child = properties[token]
            assert isinstance(child, dict)
            current = child
        elif shape.non_null_type == "array":
            if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
                raise WorkflowJsonSchemaError(
                    "WORKFLOW_NODE_OUTPUT_PATH_INVALID",
                    "array JSON Pointer tokens must be canonical non-negative indexes",
                )
            items = current.get("items")
            if not isinstance(items, dict):
                raise WorkflowJsonSchemaError(
                    "WORKFLOW_NODE_OUTPUT_PATH_INVALID",
                    "array JSON Pointer typing requires one items schema",
                )
            nullable_from_path = True
            current = items
        else:
            raise WorkflowJsonSchemaError(
                "WORKFLOW_NODE_OUTPUT_PATH_INVALID",
                "JSON Pointer cannot traverse a scalar schema",
            )
    result = value_type_from_json_schema(current)
    if nullable_from_path and not result.nullable:
        result = result.model_copy(update={"nullable": True})
    return result


__all__ = [
    "INLINE_SCHEMA_REF_PREFIX",
    "MAX_JSON_SCHEMA_DEPTH",
    "MAX_JSON_SCHEMA_ENUM_VALUES",
    "MAX_JSON_SCHEMA_ITEMS",
    "MAX_JSON_SCHEMA_PROPERTIES",
    "WorkflowJsonSchemaError",
    "inline_json_schema_ref",
    "json_schema_type_shape",
    "validate_strict_json_schema",
    "value_type_at_json_pointer",
    "value_type_from_json_schema",
]
