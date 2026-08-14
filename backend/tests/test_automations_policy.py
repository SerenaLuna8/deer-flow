from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.gateway.routers.admin_system_settings import (
    _section_response,
    _update_response,
)
from app.system_runtime_settings.models import (
    AutomationsPolicyValue,
    RuntimePolicySection,
    RuntimePolicyUpdateResult,
    RuntimePolicyView,
    default_policy_value,
)
from app.system_runtime_settings.validation import (
    RuntimePolicyInvalid,
    canonical_policy_payload,
)
from deerflow.config.app_config import AppConfig

_DEFAULT_POLICY_VERSION_ID = "b83a268d-e534-50c5-80a3-61155aede852"
_DEFAULT_POLICY_CHECKSUM = "cd4eae7f36175c2eda25d142cb6d816becfa6d0984a3735a27f3d76465a1975f"

_POLICY_VERSION_NAMESPACE = uuid.UUID("e80287de-83d9-5d3a-a4c8-df0eeaa2a955")


def test_automations_policy_defaults_match_the_schema_seed() -> None:
    assert RuntimePolicySection.AUTOMATIONS.value == "automations"
    policy = default_policy_value(RuntimePolicySection.AUTOMATIONS)

    assert isinstance(policy, AutomationsPolicyValue)
    assert policy.enabled is True
    assert policy.poll_interval_seconds == 5
    assert policy.max_concurrent_runs == 3
    assert policy.min_once_delay_seconds == 60
    canonical = canonical_policy_payload(RuntimePolicySection.AUTOMATIONS, policy)
    assert canonical.checksum == _DEFAULT_POLICY_CHECKSUM
    assert canonical.value == {
        "enabled": True,
        "max_concurrent_runs": 3,
        "min_once_delay_seconds": 60,
        "poll_interval_seconds": 5,
    }
    assert str(uuid.uuid5(_POLICY_VERSION_NAMESPACE, "automations:version:1")) == _DEFAULT_POLICY_VERSION_ID


def test_automations_policy_rejects_out_of_range_values() -> None:
    with pytest.raises(ValidationError):
        AutomationsPolicyValue(poll_interval_seconds=0)
    with pytest.raises(ValidationError):
        AutomationsPolicyValue(max_concurrent_runs=33)
    with pytest.raises(ValidationError):
        AutomationsPolicyValue(min_once_delay_seconds=-1)
    with pytest.raises(RuntimePolicyInvalid):
        canonical_policy_payload(
            RuntimePolicySection.AUTOMATIONS,
            {
                "enabled": True,
                "poll_interval_seconds": 5,
                "max_concurrent_runs": 3,
                "min_once_delay_seconds": 60,
                "lease_seconds": 120,
            },
        )


def test_example_config_has_no_scheduler_yaml_authority() -> None:
    payload = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "config.example.yaml").read_text(
            encoding="utf-8",
        )
    )

    assert "scheduler" not in payload


def test_yaml_scheduler_config_is_a_removed_legacy_source() -> None:
    with pytest.raises(ValidationError, match="LEGACY_CONFIG_REMOVED: scheduler"):
        AppConfig.model_validate(
            {
                "scheduler": {
                    "enabled": True,
                    "poll_interval_seconds": 5,
                    "max_concurrent_runs": 3,
                    "min_once_delay_seconds": 60,
                }
            },
            context={"config_source": "yaml"},
        )


def test_admin_system_settings_maps_automations_update_strictly() -> None:
    now = datetime.now(UTC)
    view = RuntimePolicyView(
        section=RuntimePolicySection.AUTOMATIONS,
        revision=2,
        schema_version=2,
        value=AutomationsPolicyValue(
            enabled=False,
            poll_interval_seconds=10,
            max_concurrent_runs=4,
            min_once_delay_seconds=120,
        ),
        effect_scope="new_requests",
        effective_revision=2,
        updated_at=now,
    )

    assert _section_response(view).model_dump(mode="json") == {
        "revision": 2,
        "schema_version": 2,
        "value": {
            "enabled": False,
            "poll_interval_seconds": 10,
            "max_concurrent_runs": 4,
            "min_once_delay_seconds": 120,
        },
        "section": "automations",
        "effect_scope": "new_requests",
        "effective_revision": 2,
        "updated_at": now.isoformat().replace("+00:00", "Z"),
    }
    response = _update_response(
        RuntimePolicyUpdateResult(
            catalog_revision=3,
            policy=view,
            effective_at=now,
        )
    )
    assert response.section == "automations"
    assert response.effect_scope == "new_requests"
    assert response.pending_roles == []
