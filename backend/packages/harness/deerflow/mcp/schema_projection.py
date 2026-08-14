from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, create_model

__all__ = ["safe_mcp_args_model"]

_MAX_MCP_SCHEMA_DEPTH = 12
_MAX_MCP_SCHEMA_NODES = 2_048
_MAX_MCP_SCHEMA_MAPPING_ENTRIES = 256
_MAX_MCP_SCHEMA_SEQUENCE_ITEMS = 256
_MAX_MCP_SCHEMA_STRING_LENGTH = 16_384
_MAX_MCP_SCHEMA_TOTAL_STRING_LENGTH = 262_144
_MCP_OPTIONAL_FIELD_MISSING = object()
_MCP_SCHEMA_RESERVED_FIELDS = frozenset(
    {
        "model_computed_fields",
        "model_config",
        "model_extra",
        "model_fields",
        "model_fields_set",
    }
)
_MCP_SCHEMA_FORBIDDEN_KEYS = frozenset(
    {
        "$dynamicRef",
        "$recursiveRef",
        "allOf",
        "definitions",
        "dependentSchemas",
        "else",
        "if",
        "not",
        "pattern",
        "patternProperties",
        "propertyNames",
        "then",
        "unevaluatedProperties",
    }
)


@dataclass(slots=True)
class _McpSchemaBudget:
    nodes: int = 0
    string_characters: int = 0


def _bounded_mcp_schema_copy(
    value: object,
    *,
    budget: _McpSchemaBudget,
    active: set[int],
    depth: int = 0,
) -> object:
    """Copy one untrusted JSON schema under strict structural limits."""

    if depth > _MAX_MCP_SCHEMA_DEPTH:
        raise ValueError("MCP tool schema is too deep")
    budget.nodes += 1
    if budget.nodes > _MAX_MCP_SCHEMA_NODES:
        raise ValueError("MCP tool schema is too large")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("MCP tool schema contains a non-finite number")
        return value
    if isinstance(value, str):
        if len(value) > _MAX_MCP_SCHEMA_STRING_LENGTH:
            raise ValueError("MCP tool schema string is too long")
        budget.string_characters += len(value)
        if budget.string_characters > _MAX_MCP_SCHEMA_TOTAL_STRING_LENGTH:
            raise ValueError("MCP tool schema strings are too large")
        return value
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active or len(value) > _MAX_MCP_SCHEMA_MAPPING_ENTRIES:
            raise ValueError("MCP tool schema mapping is invalid")
        active.add(identity)
        try:
            copied: dict[str, object] = {}
            for key, item in value.items():
                if not isinstance(key, str) or len(key) > _MAX_MCP_SCHEMA_STRING_LENGTH or key in _MCP_SCHEMA_FORBIDDEN_KEYS:
                    raise ValueError("MCP tool schema key is invalid")
                copied[key] = _bounded_mcp_schema_copy(
                    item,
                    budget=budget,
                    active=active,
                    depth=depth + 1,
                )
            return copied
        finally:
            active.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active or len(value) > _MAX_MCP_SCHEMA_SEQUENCE_ITEMS:
            raise ValueError("MCP tool schema sequence is invalid")
        active.add(identity)
        try:
            return [
                _bounded_mcp_schema_copy(
                    item,
                    budget=budget,
                    active=active,
                    depth=depth + 1,
                )
                for item in value
            ]
        finally:
            active.remove(identity)
    raise ValueError("MCP tool schema is not JSON")


def _decode_mcp_schema_pointer_token(token: str) -> str:
    decoded: list[str] = []
    index = 0
    while index < len(token):
        character = token[index]
        if character != "~":
            decoded.append(character)
            index += 1
            continue
        if index + 1 >= len(token) or token[index + 1] not in {"0", "1"}:
            raise ValueError("MCP tool schema reference is invalid")
        decoded.append("~" if token[index + 1] == "0" else "/")
        index += 2
    return "".join(decoded)


def _resolve_mcp_schema_refs(
    schema: Mapping[str, object],
) -> Mapping[str, object]:
    """Expand bounded local ``$defs`` references and reject recursive refs."""

    budget = _McpSchemaBudget()

    def resolve_pointer(reference: object) -> Mapping[str, object]:
        if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
            raise ValueError("MCP tool schema reference is invalid")
        current: object = schema
        for raw_token in reference[2:].split("/"):
            token = _decode_mcp_schema_pointer_token(raw_token)
            if not isinstance(current, Mapping) or token not in current:
                raise ValueError("MCP tool schema reference is invalid")
            current = current[token]
        if not isinstance(current, Mapping):
            raise ValueError("MCP tool schema reference is invalid")
        return current

    def expand(
        value: object,
        *,
        active_references: set[str],
        depth: int,
    ) -> object:
        if depth > _MAX_MCP_SCHEMA_DEPTH:
            raise ValueError("MCP tool schema reference is too deep")
        budget.nodes += 1
        if budget.nodes > _MAX_MCP_SCHEMA_NODES:
            raise ValueError("MCP tool schema reference expansion is too large")
        if isinstance(value, Mapping):
            if "$ref" in value:
                if set(value) != {"$ref"}:
                    raise ValueError("MCP tool schema reference is invalid")
                reference = value["$ref"]
                if not isinstance(reference, str) or reference in active_references:
                    raise ValueError("MCP tool schema reference is recursive")
                return expand(
                    resolve_pointer(reference),
                    active_references={*active_references, reference},
                    depth=depth + 1,
                )
            return {
                key: expand(
                    item,
                    active_references=active_references,
                    depth=depth + 1,
                )
                for key, item in value.items()
                if key != "$defs"
            }
        if isinstance(value, list):
            return [
                expand(
                    item,
                    active_references=active_references,
                    depth=depth + 1,
                )
                for item in value
            ]
        return value

    resolved = expand(schema, active_references=set(), depth=0)
    if not isinstance(resolved, Mapping):
        raise ValueError("MCP tool schema is invalid")
    return resolved


def _valid_mcp_schema_field_name(name: object) -> bool:
    return isinstance(name, str) and 0 < len(name) <= 128 and not name.startswith("_") and name not in _MCP_SCHEMA_RESERVED_FIELDS and all(ord(character) >= 0x20 and ord(character) != 0x7F for character in name)


def _mcp_schema_literal(values: object) -> object:
    if not isinstance(values, list) or not values or len(values) > _MAX_MCP_SCHEMA_SEQUENCE_ITEMS or any(value is not None and not isinstance(value, (str, bool, int, float)) for value in values):
        raise ValueError("MCP tool enum is invalid")
    return Literal.__getitem__(tuple(values))


def _mcp_schema_union(annotations: list[object]) -> object:
    if not annotations or len(annotations) > 16:
        raise ValueError("MCP tool union is invalid")
    unique: list[object] = []
    for annotation in annotations:
        if annotation not in unique:
            unique.append(annotation)
    if len(unique) == 1:
        return unique[0]
    return Union.__getitem__(tuple(unique))


def _mcp_schema_field(
    schema: Mapping[str, object],
    *,
    required: bool,
) -> object:
    field_kwargs: dict[str, object] = {}
    description = schema.get("description")
    title = schema.get("title")
    if description is not None:
        if not isinstance(description, str):
            raise ValueError("MCP tool field description is invalid")
        field_kwargs["description"] = description
    if title is not None:
        if not isinstance(title, str):
            raise ValueError("MCP tool field title is invalid")
        field_kwargs["title"] = title

    schema_type = schema.get("type")
    if schema_type == "string":
        for source, target in (
            ("minLength", "min_length"),
            ("maxLength", "max_length"),
        ):
            value = schema.get(source)
            if value is not None:
                if type(value) is not int or value < 0:
                    raise ValueError("MCP tool string constraint is invalid")
                field_kwargs[target] = value
    elif schema_type in {"integer", "number"}:
        for source, target in (
            ("minimum", "ge"),
            ("maximum", "le"),
            ("exclusiveMinimum", "gt"),
            ("exclusiveMaximum", "lt"),
            ("multipleOf", "multiple_of"),
        ):
            value = schema.get(source)
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                    raise ValueError("MCP tool number constraint is invalid")
                field_kwargs[target] = value
    elif schema_type == "array":
        for source, target in (
            ("minItems", "min_length"),
            ("maxItems", "max_length"),
        ):
            value = schema.get(source)
            if value is not None:
                if type(value) is not int or value < 0:
                    raise ValueError("MCP tool array constraint is invalid")
                field_kwargs[target] = value

    if required:
        return Field(..., **field_kwargs)
    return Field(
        default_factory=lambda: _MCP_OPTIONAL_FIELD_MISSING,
        exclude_if=lambda value: value is _MCP_OPTIONAL_FIELD_MISSING,
        **field_kwargs,
    )


def _mcp_schema_annotation(
    schema: Mapping[str, object],
    *,
    model_name: str,
    depth: int = 0,
) -> object:
    if depth > _MAX_MCP_SCHEMA_DEPTH:
        raise ValueError("MCP tool schema is too deep")
    if "const" in schema:
        return _mcp_schema_literal([schema["const"]])
    if "enum" in schema:
        return _mcp_schema_literal(schema["enum"])

    unions = [key for key in ("anyOf", "oneOf") if key in schema]
    if len(unions) > 1:
        raise ValueError("MCP tool schema union is ambiguous")
    if unions:
        choices = schema[unions[0]]
        if not isinstance(choices, list):
            raise ValueError("MCP tool schema union is invalid")
        annotation = _mcp_schema_union(
            [
                _mcp_schema_annotation(
                    choice,
                    model_name=f"{model_name}Choice{index}",
                    depth=depth + 1,
                )
                for index, choice in enumerate(choices)
                if isinstance(choice, Mapping)
            ]
        )
        if len(choices) == 0 or any(not isinstance(choice, Mapping) for choice in choices):
            raise ValueError("MCP tool schema union is invalid")
        return annotation

    raw_type = schema.get("type")
    if isinstance(raw_type, list):
        return _mcp_schema_union(
            [
                _mcp_schema_annotation(
                    {**schema, "type": candidate},
                    model_name=f"{model_name}Type{index}",
                    depth=depth + 1,
                )
                for index, candidate in enumerate(raw_type)
            ]
        )
    if raw_type is None and ("properties" in schema or "additionalProperties" in schema):
        raw_type = "object"
    if raw_type is None:
        return Any
    if raw_type == "string":
        return str
    if raw_type == "integer":
        return int
    if raw_type == "number":
        return float
    if raw_type == "boolean":
        return bool
    if raw_type == "null":
        return type(None)
    if raw_type == "array":
        items = schema.get("items", {})
        if not isinstance(items, Mapping):
            raise ValueError("MCP tool array items are invalid")
        return list[
            _mcp_schema_annotation(
                items,
                model_name=f"{model_name}Item",
                depth=depth + 1,
            )
        ]
    if raw_type != "object":
        raise ValueError("MCP tool schema type is invalid")

    properties = schema.get("properties", {})
    required = schema.get("required", [])
    additional_properties = schema.get("additionalProperties", True)
    if (
        not isinstance(properties, Mapping)
        or len(properties) > _MAX_MCP_SCHEMA_MAPPING_ENTRIES
        or not isinstance(required, list)
        or len(required) != len(set(required))
        or any(not isinstance(name, str) for name in required)
        or not (isinstance(additional_properties, bool) or isinstance(additional_properties, Mapping))
    ):
        raise ValueError("MCP tool object schema is invalid")
    additional_annotation: object | None = None
    if isinstance(additional_properties, Mapping):
        additional_annotation = _mcp_schema_annotation(
            additional_properties,
            model_name=f"{model_name}AdditionalValue",
            depth=depth + 1,
        )
    property_names = set(properties)
    if not set(required).issubset(property_names):
        raise ValueError("MCP tool required fields are invalid")

    fields_by_name: dict[str, tuple[object, object]] = {}
    for index, (field_name, field_schema) in enumerate(properties.items()):
        if not _valid_mcp_schema_field_name(field_name) or not isinstance(
            field_schema,
            Mapping,
        ):
            raise ValueError("MCP tool field is invalid")
        assert isinstance(field_name, str)
        annotation = _mcp_schema_annotation(
            field_schema,
            model_name=f"{model_name}Field{index}",
            depth=depth + 1,
        )
        fields_by_name[field_name] = (
            annotation,
            _mcp_schema_field(
                field_schema,
                required=field_name in required,
            ),
        )

    if additional_annotation is not None:
        fields_by_name["__pydantic_extra__"] = (
            dict[str, additional_annotation],
            Field(init=False),
        )

    return create_model(
        model_name,
        __config__=ConfigDict(
            arbitrary_types_allowed=False,
            extra="forbid" if additional_properties is False else "allow",
        ),
        **fields_by_name,
    )


def safe_mcp_args_model(
    schema: Mapping[object, object],
    *,
    model_name: str,
) -> type[BaseModel]:
    copied = _bounded_mcp_schema_copy(
        schema,
        budget=_McpSchemaBudget(),
        active=set(),
    )
    if not isinstance(copied, Mapping):
        raise ValueError("MCP tool schema is invalid")
    resolved = _resolve_mcp_schema_refs(copied)
    model = _mcp_schema_annotation(resolved, model_name=model_name)
    if not isinstance(model, type) or not issubclass(model, BaseModel):
        raise ValueError("MCP tool root schema must be an object")
    return model
