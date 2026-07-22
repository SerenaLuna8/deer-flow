from __future__ import annotations

import pytest
from pydantic import ValidationError

from deerflow.config.app_config import AppConfig
from deerflow.config.scheduler_config import SchedulerConfig


def test_scheduler_config_uses_conservative_disabled_defaults() -> None:
    config = SchedulerConfig()

    assert config.model_dump() == {
        "enabled": False,
        "poll_interval_seconds": 5,
        "max_concurrent_runs": 3,
        "min_once_delay_seconds": 60,
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"poll_interval_seconds": 0},
        {"lease_seconds": 120},
        {"max_concurrent_runs": 0},
        {"min_once_delay_seconds": -1},
    ],
)
def test_scheduler_config_rejects_unsafe_boundaries(overrides: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        SchedulerConfig(**overrides)


def test_scheduler_config_accepts_zero_once_delay() -> None:
    config = SchedulerConfig(min_once_delay_seconds=0)

    assert config.min_once_delay_seconds == 0


def test_scheduler_config_rejects_unknown_policy_fields() -> None:
    with pytest.raises(ValidationError):
        SchedulerConfig(retry_policy="always")


def test_scheduler_config_values_support_standard_env_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_AUTOMATION_SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("TEST_AUTOMATION_POLL_SECONDS", "15")
    monkeypatch.setenv("TEST_AUTOMATION_MAX_RUNS", "7")
    monkeypatch.setenv("TEST_AUTOMATION_ONCE_DELAY", "0")

    resolved = AppConfig.resolve_env_variables(
        {
            "scheduler": {
                "enabled": "$TEST_AUTOMATION_SCHEDULER_ENABLED",
                "poll_interval_seconds": "$TEST_AUTOMATION_POLL_SECONDS",
                "max_concurrent_runs": "$TEST_AUTOMATION_MAX_RUNS",
                "min_once_delay_seconds": "$TEST_AUTOMATION_ONCE_DELAY",
            }
        }
    )
    config = SchedulerConfig.model_validate(resolved["scheduler"])

    assert config == SchedulerConfig(
        enabled=True,
        poll_interval_seconds=15,
        max_concurrent_runs=7,
        min_once_delay_seconds=0,
    )
