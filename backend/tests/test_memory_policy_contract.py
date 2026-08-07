"""Strict platform Memory policy contract."""

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
    "idle_seal_minutes",
    "episode_retention_days",
}


def test_memory_policy_has_only_the_final_six_fields() -> None:
    policy = MemoryPolicy()

    assert set(type(policy).model_fields) == FINAL_MEMORY_FIELDS
    assert policy.model_dump(mode="json") == {
        "enabled": True,
        "model_name": None,
        "dream_interval_minutes": 120,
        "max_injection_tokens": 2_000,
        "idle_seal_minutes": 1_440,
        "episode_retention_days": 365,
    }


def test_materialized_memory_config_matches_the_final_policy_contract() -> None:
    config = MemoryConfig()

    assert set(type(config).model_fields) == FINAL_MEMORY_FIELDS
    assert config.model_dump(mode="json") == {
        "enabled": True,
        "model_name": None,
        "dream_interval_minutes": 120,
        "max_injection_tokens": 2_000,
        "idle_seal_minutes": 1_440,
        "episode_retention_days": 365,
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
            "idle_seal_minutes": 1_440,
            "episode_retention_days": 365,
            field: False,
        }
    }

    with pytest.raises(RuntimePolicyInvalid):
        parse_policy_value("agent_runtime", payload)


@pytest.mark.parametrize("value", [14, 1_441])
def test_dream_interval_is_bounded(value: int) -> None:
    with pytest.raises(ValueError):
        MemoryPolicy(dream_interval_minutes=value)


@pytest.mark.parametrize("value", [-1, 1, 29, 10_081])
def test_idle_seal_minutes_must_be_zero_or_in_range(value: int) -> None:
    with pytest.raises(ValueError):
        MemoryPolicy(idle_seal_minutes=value)


@pytest.mark.parametrize("value", [0, 30, 1_440, 10_080])
def test_idle_seal_minutes_accepts_zero_and_the_documented_range(value: int) -> None:
    assert MemoryPolicy(idle_seal_minutes=value).idle_seal_minutes == value
    assert MemoryConfig(idle_seal_minutes=value).idle_seal_minutes == value


@pytest.mark.parametrize("value", [-1, 1, 29, 3_651])
def test_episode_retention_days_must_be_zero_or_in_range(value: int) -> None:
    with pytest.raises(ValueError):
        MemoryPolicy(episode_retention_days=value)
    with pytest.raises(ValueError):
        MemoryConfig(episode_retention_days=value)


@pytest.mark.parametrize("value", [0, 30, 365, 3_650])
def test_episode_retention_days_accepts_zero_and_the_documented_range(value: int) -> None:
    assert MemoryPolicy(episode_retention_days=value).episode_retention_days == value
    assert MemoryConfig(episode_retention_days=value).episode_retention_days == value
