from __future__ import annotations

import pytest

from app.system_runtime_settings.models import MemoryPolicy
from app.system_runtime_settings.validation import RuntimePolicyInvalid, parse_policy_value
from deerflow.config.memory_config import MemoryConfig

FINAL_MEMORY_FIELDS = {
    "enabled",
    "model_name",
    "dream_interval_minutes",
    "max_injection_tokens",
}


def test_memory_policy_has_only_the_final_four_fields() -> None:
    policy = MemoryPolicy()

    assert set(type(policy).model_fields) == FINAL_MEMORY_FIELDS
    assert policy.model_dump(mode="json") == {
        "enabled": True,
        "model_name": None,
        "dream_interval_minutes": 120,
        "max_injection_tokens": 2_000,
    }


def test_materialized_memory_config_matches_the_final_policy_contract() -> None:
    config = MemoryConfig()

    assert set(type(config).model_fields) == FINAL_MEMORY_FIELDS
    assert config.model_dump(mode="json") == {
        "enabled": True,
        "model_name": None,
        "dream_interval_minutes": 120,
        "max_injection_tokens": 2_000,
    }


@pytest.mark.parametrize(
    "field",
    [
        "pipeline_mode",
        "consolidation_interval_minutes",
        "candidate_retention_days",
        "search_enabled",
        "debounce_seconds",
        "max_facts",
        "fact_confidence_threshold",
        "injection_enabled",
        "token_counting",
        "guaranteed_categories",
        "guaranteed_token_budget",
        "staleness_review_enabled",
        "staleness_age_days",
        "staleness_min_candidates",
        "staleness_max_removals_per_cycle",
        "staleness_protected_categories",
    ],
)
def test_runtime_policy_strictly_rejects_every_removed_memory_field(field: str) -> None:
    payload = {
        "memory": {
            "enabled": True,
            "model_name": None,
            "dream_interval_minutes": 120,
            "max_injection_tokens": 2_000,
            field: False,
        }
    }

    with pytest.raises(RuntimePolicyInvalid):
        parse_policy_value("agent_runtime", payload)


@pytest.mark.parametrize("value", [14, 1_441])
def test_dream_interval_is_bounded(value: int) -> None:
    with pytest.raises(ValueError):
        MemoryPolicy(dream_interval_minutes=value)
