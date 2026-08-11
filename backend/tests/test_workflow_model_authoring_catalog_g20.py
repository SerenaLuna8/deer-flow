from __future__ import annotations

import pytest

from app.gateway.routers.models import _public_response
from app.system_settings.models import PublicSystemModelView
from app.system_settings.validation import (
    ModelSettingsInvalid,
    workflow_model_authoring_capability,
)


def test_workflow_model_authoring_capability_is_provider_derived_and_bounded() -> None:
    openai = workflow_model_authoring_capability("openai")
    assert openai.modes == ("chat",)
    assert openai.supports_streaming is True
    assert tuple(item.name for item in openai.parameters) == (
        "temperature",
        "max_tokens",
    )
    assert openai.parameters[0].kind == "number"
    assert openai.parameters[0].minimum == -2
    assert openai.parameters[0].maximum == 2
    assert openai.parameters[1].kind == "integer"
    assert openai.parameters[1].minimum == 1
    assert openai.parameters[1].maximum == 2_000_000

    codex = workflow_model_authoring_capability("codex_cli")
    assert codex.modes == ("chat",)
    assert codex.supports_streaming is True
    assert codex.parameters == ()

    with pytest.raises(ModelSettingsInvalid):
        workflow_model_authoring_capability("future_provider")


def test_public_model_response_exposes_only_safe_workflow_authoring_capability() -> None:
    capability = workflow_model_authoring_capability("openai")
    response = _public_response(
        PublicSystemModelView(
            logical_name="primary-chat",
            display_name="Primary Chat",
            description="Safe public label",
            supports_thinking=True,
            supports_reasoning_effort=True,
            supports_vision=False,
            is_default=True,
            workflow_authoring=capability,
        )
    ).model_dump(mode="json")

    assert response["workflow_authoring"] == {
        "modes": ["chat"],
        "supports_streaming": True,
        "parameters": [
            {
                "name": "temperature",
                "kind": "number",
                "minimum": -2.0,
                "maximum": 2.0,
            },
            {
                "name": "max_tokens",
                "kind": "integer",
                "minimum": 1.0,
                "maximum": 2_000_000.0,
            },
        ],
    }
    serialized = repr(response).lower()
    assert "provider_adapter" not in serialized
    assert "provider_model" not in serialized
    assert "credential" not in serialized
    assert "settings" not in serialized
