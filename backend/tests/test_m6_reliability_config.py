from __future__ import annotations

from importlib.util import find_spec
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from deerflow.config.app_config import AppConfig
from deerflow.config.quota_config import QuotaConfig
from deerflow.config.recovery_config import RecoveryConfig
from deerflow.config.sandbox_config import SandboxConfig
from deerflow.config.worker_config import WorkerConfig


def test_m6_reliability_config_modules_exist() -> None:
    assert find_spec("deerflow.config.worker_config") is not None
    assert find_spec("deerflow.config.quota_config") is not None
    assert find_spec("deerflow.config.recovery_config") is not None


def test_worker_config_pins_defaults_and_timing_invariants() -> None:
    config = WorkerConfig()
    assert config.model_dump() == {
        "enabled": True,
        "poll_interval_seconds": 0.5,
        "lease_seconds": 90,
        "heartbeat_seconds": 20,
        "max_concurrent_jobs": 4,
        "shutdown_grace_seconds": 30,
        "default_max_attempts": 3,
        "retry_initial_seconds": 2,
        "retry_max_seconds": 300,
    }
    with pytest.raises(ValidationError, match="less than one third"):
        WorkerConfig(lease_seconds=30, heartbeat_seconds=10)
    assert WorkerConfig(lease_seconds=31, heartbeat_seconds=10).max_concurrent_jobs == 4
    with pytest.raises(ValidationError, match="must not exceed"):
        WorkerConfig(retry_initial_seconds=301, retry_max_seconds=300)


def test_quota_config_pins_defaults_ceiling_and_threshold() -> None:
    config = QuotaConfig()
    assert (
        config.default_member_limit,
        config.default_storage_bytes_limit,
        config.default_concurrent_run_limit,
        config.default_mcp_calls_daily_limit,
        config.warning_threshold,
    ) == (20, 5_368_709_120, 3, 10_000, 0.8)
    with pytest.raises(ValidationError, match="deployment maximum"):
        QuotaConfig(default_member_limit=21, max_member_limit=20)
    with pytest.raises(ValidationError):
        QuotaConfig(warning_threshold=1)


def test_recovery_config_has_only_non_secret_paths_and_limits() -> None:
    config = RecoveryConfig()
    assert config.archive_chunk_bytes == 1_048_576
    assert config.retention_days == 30
    assert config.journal_fsync_policy == "always"
    assert not {name for name in RecoveryConfig.model_fields if any(fragment in name for fragment in ("key", "secret", "token", "password"))}
    with pytest.raises(ValidationError, match="must differ"):
        RecoveryConfig(archive_root=Path("same"), tombstone_journal_path=Path("same"))


def test_app_config_registers_restart_required_m6_sections() -> None:
    config = AppConfig(sandbox=SandboxConfig(use="test"))
    assert config.worker == WorkerConfig()
    assert config.quotas == QuotaConfig()
    assert config.recovery == RecoveryConfig()
    for name in ("worker", "quotas", "recovery"):
        assert (AppConfig.model_fields[name].description or "").startswith("startup-only:")


def test_config_example_bumps_version_and_documents_m6_sections() -> None:
    example_path = Path(__file__).resolve().parents[2] / "config.example.yaml"
    data = yaml.safe_load(example_path.read_text(encoding="utf-8"))
    assert data["config_version"] == 23
    assert data["worker"] == WorkerConfig().model_dump(mode="json")
    assert data["quotas"] == QuotaConfig().model_dump(mode="json")
    assert data["recovery"] == RecoveryConfig().model_dump(mode="json")
