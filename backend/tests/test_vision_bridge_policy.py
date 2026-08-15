"""Strict Runtime Policy and compatibility contracts for Vision Bridge P1."""

from __future__ import annotations

import pytest

from app.system_runtime_settings.materializer import _materialize_exact
from app.system_runtime_settings.models import (
    AgentRuntimePolicyValue,
    VisionBridgePolicy,
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
from deerflow.vision.compatibility import (
    VISION_BRIDGE_CONTRACT_V1,
    VISION_BRIDGE_FAKE_ADAPTER,
    is_vision_bridge_adapter_compatible,
)


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
        "timeout_seconds": 20,
        "contract_version": VISION_BRIDGE_CONTRACT_V1,
    }


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
        "timeout_seconds": 20,
        "contract_version": VISION_BRIDGE_CONTRACT_V1,
    }


def test_runtime_overlay_materializes_bridge_but_yaml_rejects_it() -> None:
    runtime = _app_config().with_runtime_policy(
        {
            "vision_bridge": {
                "model_name": "vision-small-v1",
                "timeout_seconds": 25,
                "contract_version": VISION_BRIDGE_CONTRACT_V1,
            }
        }
    )

    assert runtime.vision_bridge.model_name == "vision-small-v1"
    assert runtime.vision_bridge.timeout_seconds == 25
    with pytest.raises(ValueError, match="LEGACY_CONFIG_REMOVED"):
        AppConfig.model_validate(
            {
                "sandbox": {
                    "use": "deerflow.sandbox.local:LocalSandboxProvider",
                },
                "vision_bridge": {"model_name": "vision-small-v1"},
            },
            context={"config_source": "yaml"},
        )


def test_p1_compatibility_allowlist_cannot_admit_real_provider() -> None:
    assert is_vision_bridge_adapter_compatible(
        VISION_BRIDGE_FAKE_ADAPTER,
        VISION_BRIDGE_CONTRACT_V1,
    )
    assert not is_vision_bridge_adapter_compatible(
        "openai",
        VISION_BRIDGE_CONTRACT_V1,
    )
    assert not is_vision_bridge_adapter_compatible(
        VISION_BRIDGE_FAKE_ADAPTER,
        "vision.bridge.v2",
    )
