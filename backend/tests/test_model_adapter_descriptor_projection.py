"""The backend adapter registry is the admin UI descriptor authority."""

from __future__ import annotations

from types import MappingProxyType

import pytest

from app.gateway.routers import admin_model_settings
from app.gateway.routers.admin_model_settings import _catalog_response
from app.system_settings import validation as model_validation
from app.system_settings.models import SystemModelCatalogView
from app.system_settings.validation import (
    ModelSettingsInvalid,
    ProviderAdapterSpec,
    ProviderSettingFieldSpec,
    materialize_effective_model_settings,
    validate_materialized_model_settings,
    validate_model_settings,
)


def test_admin_catalog_projects_only_authorable_builtin_adapters() -> None:
    response = _catalog_response(
        SystemModelCatalogView(
            catalog_revision=7,
            default_model_config_id=None,
            items=(),
        ),
        "adapter-descriptor-test",
    )

    descriptors = {item.id: item for item in response.provider_adapters}
    assert set(descriptors) == {
        "anthropic",
        "deepseek",
        "openai",
        "openai_responses",
        "vllm",
    }
    assert "vision_bridge_fake" not in descriptors
    assert "vision_openai_compatible_v1" not in descriptors
    assert descriptors["openai"].api_key_required is True
    openai_fields = {field.name: field for field in descriptors["openai"].setting_fields}
    anthropic_fields = {field.name: field for field in descriptors["anthropic"].setting_fields}
    # The wire protocol is fixed by the adapter identity; neither OpenAI entry
    # exposes a protocol switch or output-version knob as an authoring field.
    for openai_adapter in ("openai", "openai_responses"):
        field_names = {field.name for field in descriptors[openai_adapter].setting_fields}
        assert "use_responses_api" not in field_names
        assert "output_version" not in field_names
    # Both entries share the OpenAI-compatible fields; only the Responses
    # entry adds the protocol-specific reasoning-summary opt-in.
    chat_field_names = {field.name for field in descriptors["openai"].setting_fields}
    responses_fields = {field.name: field for field in descriptors["openai_responses"].setting_fields}
    assert set(responses_fields) - chat_field_names == {"reasoning_summary"}
    assert chat_field_names - set(responses_fields) == set()
    assert responses_fields["reasoning_summary"].options == ["auto", "concise", "detailed"]
    assert responses_fields["reasoning_summary"].advanced is True
    assert responses_fields["reasoning_summary"].default_mode == "provider"
    assert responses_fields["reasoning_summary"].default_value is None
    assert openai_fields["max_tokens"].minimum == 1
    assert openai_fields["max_tokens"].maximum == 2_000_000
    assert openai_fields["max_tokens"].advanced is False
    assert openai_fields["max_tokens"].form_control == "input"
    assert openai_fields["max_tokens"].default_mode == "provider"
    assert openai_fields["max_tokens"].default_value is None
    assert openai_fields["base_url"].form_control == "input"
    assert openai_fields["base_url"].default_mode == "platform"
    assert openai_fields["base_url"].default_value == "https://api.openai.com/v1"
    assert openai_fields["request_timeout"].advanced is True
    assert openai_fields["request_timeout"].form_control == "input"
    assert openai_fields["stream_chunk_timeout"].default_mode == "platform"
    assert openai_fields["stream_chunk_timeout"].default_value == 240.0
    assert openai_fields["timeout"].form_control == "preserve"
    assert openai_fields["extra_body"].form_control == "preserve"
    assert openai_fields["when_thinking_enabled"].form_control == "preserve"
    assert openai_fields["when_thinking_disabled"].form_control == "preserve"
    assert anthropic_fields["thinking"].input_type == "json"
    assert anthropic_fields["thinking"].advanced is True
    assert anthropic_fields["thinking"].form_control == "preserve"
    assert anthropic_fields["default_request_timeout"].form_control == "input"
    assert anthropic_fields["request_timeout"].form_control == "preserve"
    assert anthropic_fields["timeout"].form_control == "preserve"
    for descriptor in descriptors.values():
        assert {field.name for field in descriptor.setting_fields if not field.advanced} == {
            "base_url",
            "max_tokens",
        }
        assert next(field for field in descriptor.setting_fields if field.name == "temperature").advanced is True
        assert all(field.form_control == "preserve" for field in descriptor.setting_fields if field.input_type == "json")
    assert all("max_retries" not in {field.name for field in descriptor.setting_fields} for descriptor in descriptors.values())
    assert response.request_id == "adapter-descriptor-test"


def test_deepseek_reasoning_effort_is_provider_specific() -> None:
    response = _catalog_response(
        SystemModelCatalogView(
            catalog_revision=7,
            default_model_config_id=None,
            items=(),
        ),
        "deepseek-reasoning-effort-test",
    )
    descriptors = {item.id: item for item in response.provider_adapters}

    fields = {field.name: field for field in descriptors["deepseek"].setting_fields}
    assert fields["reasoning_effort"].options == ["low", "high", "max"]
    for effort in ("low", "high", "max"):
        assert validate_model_settings(
            {"reasoning_effort": effort},
            provider_adapter="deepseek",
        ) == {"reasoning_effort": effort}
    for effort in ("none", "minimal", "medium", "xhigh"):
        with pytest.raises(ModelSettingsInvalid):
            validate_model_settings(
                {"reasoning_effort": effort},
                provider_adapter="deepseek",
            )

    openai_compatible_options = ["none", "low", "medium", "high", "xhigh", "max"]
    for adapter in ("openai", "openai_responses", "vllm"):
        fields = {field.name: field for field in descriptors[adapter].setting_fields}
        assert fields["reasoning_effort"].options == openai_compatible_options
        for effort in openai_compatible_options:
            assert validate_model_settings(
                {"reasoning_effort": effort},
                provider_adapter=adapter,
            ) == {"reasoning_effort": effort}
        with pytest.raises(ModelSettingsInvalid):
            validate_model_settings(
                {"reasoning_effort": "minimal"},
                provider_adapter=adapter,
            )

    assert "reasoning_effort" not in {field.name for field in descriptors["anthropic"].setting_fields}


def test_admin_catalog_projects_new_adapter_custom_field_without_ui_code_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom_field = ProviderSettingFieldSpec(
        "vendor_quality",
        "Vendor quality",
        "integer",
        advanced=False,
        minimum=1,
        maximum=7,
        step=1,
    )
    monkeypatch.setattr(
        admin_model_settings,
        "BUILTIN_PROVIDER_ADAPTERS",
        MappingProxyType(
            {
                "new_vendor_v2": ProviderAdapterSpec(
                    "vendor.models:ChatVendor",
                    False,
                    fields=(custom_field,),
                )
            }
        ),
    )

    response = _catalog_response(
        SystemModelCatalogView(
            catalog_revision=8,
            default_model_config_id=None,
            items=(),
        ),
        "new-adapter-descriptor-test",
    )

    assert response.provider_adapters[0].model_dump() == {
        "id": "new_vendor_v2",
        "api_key_required": False,
        "setting_fields": [
            {
                "name": "vendor_quality",
                "label": "Vendor quality",
                "input_type": "integer",
                "advanced": False,
                "form_control": "input",
                "default_mode": "provider",
                "default_value": None,
                "minimum": 1,
                "maximum": 7,
                "step": 1,
                "options": [],
            }
        ],
    }

    monkeypatch.setitem(
        model_validation.PROVIDER_ADAPTERS,
        "new_vendor_v2",
        ProviderAdapterSpec(
            "vendor.models:ChatVendor",
            False,
            fields=(custom_field,),
        ),
    )
    assert validate_model_settings(
        {"vendor_quality": 5},
        provider_adapter="new_vendor_v2",
    ) == {"vendor_quality": 5}
    with pytest.raises(ModelSettingsInvalid):
        validate_model_settings(
            {"vendor_quality": 8},
            provider_adapter="new_vendor_v2",
        )


def test_provider_setting_descriptor_rejects_illegal_metadata() -> None:
    with pytest.raises(ValueError, match="descriptor invalid"):
        ProviderSettingFieldSpec(
            "vendor_payload",
            "Vendor payload",
            "json",
            advanced=False,
        )
    with pytest.raises(ValueError, match="descriptor invalid"):
        ProviderSettingFieldSpec(
            "vendor_payload",
            "Vendor payload",
            "json",
        )
    with pytest.raises(ValueError, match="descriptor invalid"):
        ProviderSettingFieldSpec(
            "vendor_quality",
            "Vendor quality",
            "integer",
            default_mode="platform",
            default_value=None,
        )
    with pytest.raises(ValueError, match="descriptor invalid"):
        ProviderSettingFieldSpec(
            "vendor_quality",
            "Vendor quality",
            "integer",
            default_mode="provider",
            default_value=3,
        )


@pytest.mark.parametrize(
    ("provider_adapter", "base_url", "extra_defaults"),
    [
        ("anthropic", "https://api.anthropic.com", {}),
        (
            "deepseek",
            "https://api.deepseek.com/v1",
            {"stream_chunk_timeout": 240.0},
        ),
        (
            "openai",
            "https://api.openai.com/v1",
            {
                "stream_chunk_timeout": 240.0,
                # Pinned by the adapter identity: Chat Completions never lets
                # the SDK auto-select the Responses protocol.
                "use_responses_api": False,
            },
        ),
        (
            "openai_responses",
            "https://api.openai.com/v1",
            {
                "stream_chunk_timeout": 240.0,
                # Pinned by the adapter identity, never authored by admins.
                "use_responses_api": True,
                "output_version": "responses/v1",
            },
        ),
        (
            "vllm",
            "https://api.openai.com/v1",
            {
                "stream_chunk_timeout": 240.0,
                "cumulative_stream_usage": False,
            },
        ),
    ],
)
def test_materialization_injects_only_platform_provider_defaults(
    provider_adapter: str,
    base_url: str,
    extra_defaults: dict[str, object],
) -> None:
    assert materialize_effective_model_settings(
        {"temperature": 0.25},
        provider_adapter=provider_adapter,
    ) == {
        "base_url": base_url,
        "temperature": 0.25,
        **extra_defaults,
    }


def test_materialization_preserves_explicit_platform_default_overrides() -> None:
    assert materialize_effective_model_settings(
        {
            "base_url": "https://gateway.example.test/v1",
            "stream_chunk_timeout": 30.0,
            "cumulative_stream_usage": True,
        },
        provider_adapter="vllm",
    ) == {
        "base_url": "https://gateway.example.test/v1",
        "stream_chunk_timeout": 30.0,
        "cumulative_stream_usage": True,
    }


def test_provider_retry_count_is_runtime_owned_not_catalog_authorable() -> None:
    with pytest.raises(ModelSettingsInvalid):
        validate_model_settings(
            {"max_retries": 9},
            provider_adapter="openai",
        )

    assert validate_materialized_model_settings(
        {
            "base_url": "https://provider.example.test/v1",
            "max_retries": 9,
        },
        provider_adapter="openai",
    ) == {"base_url": "https://provider.example.test/v1"}
