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
        "patched_deepseek",
        "patched_openai",
        "vllm",
    }
    assert "vision_bridge_fake" not in descriptors
    assert "vision_openai_compatible_v1" not in descriptors
    assert descriptors["openai"].credential_required is True
    openai_fields = {field.name: field for field in descriptors["openai"].setting_fields}
    anthropic_fields = {field.name: field for field in descriptors["anthropic"].setting_fields}
    assert openai_fields["use_responses_api"].input_type == "boolean"
    assert openai_fields["max_tokens"].minimum == 1
    assert openai_fields["max_tokens"].maximum == 2_000_000
    assert openai_fields["max_tokens"].advanced is False
    assert anthropic_fields["thinking"].input_type == "json"
    assert anthropic_fields["thinking"].advanced is True
    assert all("max_retries" not in {field.name for field in descriptor.setting_fields} for descriptor in descriptors.values())
    assert response.request_id == "adapter-descriptor-test"


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
        "credential_required": False,
        "setting_fields": [
            {
                "name": "vendor_quality",
                "label": "Vendor quality",
                "input_type": "integer",
                "advanced": False,
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
