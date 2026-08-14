from __future__ import annotations

import pytest
from pydantic import ValidationError

from deerflow.mcp.schema_projection import safe_mcp_args_model


def test_safe_mcp_args_model_projects_required_and_optional_fields() -> None:
    model = safe_mcp_args_model(
        {
            "type": "object",
            "properties": {
                "count": {"type": "integer"},
                "label": {"type": "string"},
            },
            "required": ["count"],
            "additionalProperties": False,
        },
        model_name="ProjectedArgs",
    )

    assert model.model_validate({"count": 3}).model_dump() == {"count": 3}
    assert model.model_validate({"count": 3, "label": "ready"}).model_dump() == {
        "count": 3,
        "label": "ready",
    }
    with pytest.raises(ValidationError):
        model.model_validate({})


def test_safe_mcp_args_model_preserves_additional_properties_policy() -> None:
    forbidden = safe_mcp_args_model(
        {"type": "object", "additionalProperties": False},
        model_name="ForbiddenExtras",
    )
    allowed = safe_mcp_args_model(
        {"type": "object", "additionalProperties": True},
        model_name="AllowedExtras",
    )
    typed = safe_mcp_args_model(
        {"type": "object", "additionalProperties": {"type": "integer"}},
        model_name="TypedExtras",
    )

    with pytest.raises(ValidationError):
        forbidden.model_validate({"unknown": 1})
    assert allowed.model_validate({"unknown": {"nested": True}}).model_dump() == {"unknown": {"nested": True}}
    assert typed.model_validate({"first": 1}).model_dump() == {"first": 1}
    with pytest.raises(ValidationError):
        typed.model_validate({"first": []})


def test_safe_mcp_args_model_expands_local_defs_references() -> None:
    model = safe_mcp_args_model(
        {
            "type": "object",
            "properties": {
                "item": {"$ref": "#/$defs/nested"},
            },
            "required": ["item"],
            "additionalProperties": False,
            "$defs": {
                "nested": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                    "additionalProperties": False,
                }
            },
        },
        model_name="ReferencedArgs",
    )

    assert model.model_validate({"item": {"name": "value"}}).model_dump() == {"item": {"name": "value"}}


def test_safe_mcp_args_model_rejects_recursive_reference() -> None:
    schema = {
        "type": "object",
        "properties": {"node": {"$ref": "#/$defs/node"}},
        "$defs": {
            "node": {
                "type": "object",
                "properties": {"next": {"$ref": "#/$defs/node"}},
            }
        },
    }

    with pytest.raises(ValueError, match="MCP tool schema reference is recursive"):
        safe_mcp_args_model(schema, model_name="RecursiveArgs")


def test_safe_mcp_args_model_rejects_cyclic_containers() -> None:
    schema: dict[str, object] = {"type": "object"}
    schema["properties"] = {"loop": schema}

    with pytest.raises(ValueError, match="MCP tool schema mapping is invalid"):
        safe_mcp_args_model(schema, model_name="CyclicArgs")


def test_safe_mcp_args_model_rejects_forbidden_schema_keys() -> None:
    schema = {
        "type": "object",
        "properties": {
            "value": {
                "type": "string",
                "pattern": "^unsafe$",
            }
        },
    }

    with pytest.raises(ValueError, match="MCP tool schema key is invalid"):
        safe_mcp_args_model(schema, model_name="ForbiddenKeyArgs")
