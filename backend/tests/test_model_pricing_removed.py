from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.system_settings.validation import ModelSettingsInvalid, validate_model_settings
from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig
from deerflow.models.factory import create_chat_model


def test_model_config_rejects_removed_pricing_metadata() -> None:
    with pytest.raises(ValidationError, match="pricing metadata is not supported"):
        ModelConfig.model_validate(
            {
                "name": "text-model",
                "display_name": "Text model",
                "description": "",
                "use": "example:Model",
                "model": "provider-model",
                "pricing": {"input": 1, "output": 2},
            }
        )


def test_managed_model_settings_reject_removed_pricing_metadata() -> None:
    with pytest.raises(ModelSettingsInvalid):
        validate_model_settings(
            {"pricing": {"input": 1, "output": 2}},
            provider_adapter="openai",
        )


def test_factory_drops_legacy_pricing_metadata_if_validation_is_bypassed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CaptureModel:
        model_fields: dict[str, object] = {}

        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    monkeypatch.setattr(
        "deerflow.models.factory.resolve_class",
        lambda *_args, **_kwargs: CaptureModel,
    )
    model = ModelConfig(
        name="text-model",
        display_name="Text model",
        description="",
        use="example:Model",
        model="provider-model",
    ).model_copy(update={"pricing": {"input": 1, "output": 2}})
    app_config = AppConfig(
        models=[model],
        sandbox={"use": "deerflow.sandbox.local:LocalSandboxProvider"},
    )

    instance = create_chat_model(
        name=model.name,
        app_config=app_config,
        attach_tracing=False,
    )

    assert "pricing" not in instance.kwargs  # type: ignore[attr-defined]
