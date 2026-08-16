"""Strict Runtime Policy and compatibility contracts for Vision Bridge."""

from __future__ import annotations

import pytest

from app.system_runtime_settings.materializer import _materialize_exact
from app.system_runtime_settings.models import (
    DEFAULT_VISION_BRIDGE_MODEL_NAME,
    AgentRuntimePolicyValue,
    RuntimePolicySection,
    VisionBridgePolicy,
    default_policy_value,
)
from app.system_runtime_settings.validation import (
    LEGACY_RUNTIME_POLICY_SCHEMA_VERSION,
    RUNTIME_POLICY_SCHEMA_VERSION,
    RuntimePolicyInvalid,
    canonical_policy_payload,
    canonical_policy_payload_for_schema,
    parse_policy_value,
)
from deerflow.config.app_config import AppConfig
from deerflow.config.sandbox_config import SandboxConfig
from deerflow.config.vision_bridge_config import VisionBridgeConfig

VISION_BRIDGE_CONTRACT_V1 = "vision.bridge.v1"

VISION_MODEL_REF = "00000000-0000-4000-8000-000000000306"


def _app_config() -> AppConfig:
    return AppConfig(
        sandbox=SandboxConfig(
            use="deerflow.sandbox.local:LocalSandboxProvider",
        ),
    )


def test_policy_defaults_to_no_bridge_without_a_second_enablement_flag() -> None:
    policy = AgentRuntimePolicyValue()

    assert set(type(policy.vision_bridge).model_fields) == {
        "model_name",
        "timeout_seconds",
        "contract_version",
    }
    assert policy.vision_bridge.model_dump(mode="json") == {
        "model_name": None,
        "timeout_seconds": 60,
        "contract_version": VISION_BRIDGE_CONTRACT_V1,
    }
    assert VisionBridgeConfig().timeout_seconds == 60


def test_fresh_install_policy_selects_the_existing_luna_model() -> None:
    policy = default_policy_value(RuntimePolicySection.AGENT_RUNTIME)

    assert isinstance(policy, AgentRuntimePolicyValue)
    assert policy.vision_bridge.model_name == DEFAULT_VISION_BRIDGE_MODEL_NAME
    assert policy.vision_bridge.timeout_seconds == 60


def test_policy_accepts_canonical_uuid_refs_and_rejects_legacy_names() -> None:
    uuid_v7_ref = "01890f4e-7b6d-7000-8000-000000000001"

    assert VisionBridgePolicy(model_name=uuid_v7_ref).model_name == uuid_v7_ref
    with pytest.raises(ValueError):
        VisionBridgePolicy(model_name="gpt-5.6-luna")
    with pytest.raises(ValueError):
        VisionBridgePolicy(model_name=uuid_v7_ref.upper())


@pytest.mark.parametrize("field", ["enabled", "project_egress_grant"])
def test_policy_rejects_removed_bridge_gates(field: str) -> None:
    with pytest.raises(RuntimePolicyInvalid):
        parse_policy_value(
            "agent_runtime",
            {"vision_bridge": {field: True}},
        )


@pytest.mark.parametrize("timeout_seconds", [4, 121])
def test_policy_rejects_out_of_range_total_deadline(
    timeout_seconds: int,
) -> None:
    with pytest.raises(ValueError):
        VisionBridgePolicy(timeout_seconds=timeout_seconds)


def test_policy_rejects_unknown_contract_version() -> None:
    with pytest.raises(ValueError):
        VisionBridgePolicy(contract_version="vision.bridge.v2")


def test_legacy_v2_payload_materializes_with_bridge_disabled() -> None:
    legacy_value = AgentRuntimePolicyValue().model_dump(mode="json")
    legacy_value.pop("vision_bridge")
    canonical = canonical_policy_payload_for_schema(
        "agent_runtime",
        legacy_value,
        schema_version=LEGACY_RUNTIME_POLICY_SCHEMA_VERSION,
    )

    materialized = _materialize_exact(
        "agent_runtime",
        schema_version=canonical.schema_version,
        value=canonical.value,
        checksum=canonical.checksum,
    )

    assert isinstance(materialized, AgentRuntimePolicyValue)
    assert materialized.vision_bridge.model_name is None


def test_new_policy_payload_uses_schema_v3_and_includes_bridge() -> None:
    canonical = canonical_policy_payload(
        "agent_runtime",
        AgentRuntimePolicyValue(),
    )

    assert canonical.schema_version == RUNTIME_POLICY_SCHEMA_VERSION == 3
    assert canonical.value["vision_bridge"] == {
        "model_name": None,
        "timeout_seconds": 60,
        "contract_version": VISION_BRIDGE_CONTRACT_V1,
    }


def test_runtime_overlay_materializes_bridge_but_yaml_rejects_it() -> None:
    runtime = _app_config().with_runtime_policy(
        {
            "vision_bridge": {
                "model_name": VISION_MODEL_REF,
                "timeout_seconds": 25,
                "contract_version": VISION_BRIDGE_CONTRACT_V1,
            }
        }
    )

    assert runtime.vision_bridge.model_name == VISION_MODEL_REF
    assert runtime.vision_bridge.timeout_seconds == 25
    with pytest.raises(ValueError, match="LEGACY_CONFIG_REMOVED"):
        AppConfig.model_validate(
            {
                "sandbox": {
                    "use": "deerflow.sandbox.local:LocalSandboxProvider",
                },
                "vision_bridge": {"model_name": VISION_MODEL_REF},
            },
            context={"config_source": "yaml"},
        )


def test_policy_does_not_encode_a_provider_wire_protocol() -> None:
    dumped = VisionBridgePolicy(
        model_name=VISION_MODEL_REF,
    ).model_dump(mode="json")

    assert set(dumped) == {
        "model_name",
        "timeout_seconds",
        "contract_version",
    }
    assert not any(
        key in dumped
        for key in (
            "provider_adapter",
            "use_responses_api",
            "base_url",
            "provider_protocol",
        )
    )
