"""Contract tests for the one ordered M1-M7 PostgreSQL release gate."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_M1_M7_GATE = (
    "tests/test_m7_final_baseline_postgres.py",
    "tests/test_m7_asset_bootstrap_postgres.py",
    "tests/integration/test_project_isolation_postgres.py",
    "tests/integration/test_m2_project_governance_postgres.py",
    "tests/integration/test_m3_shared_assets_postgres.py",
    "tests/integration/test_m3_mcp_credentials_postgres.py",
    "tests/integration/test_m4_private_work_postgres.py",
    "tests/integration/test_m5_project_automation_postgres.py",
    "tests/test_m6_process_readiness.py",
    "tests/test_m6_job_repository_postgres.py",
    "tests/test_m6_durable_stream_postgres.py",
    "tests/test_m6_quota_service_postgres.py",
    "tests/test_m6_audit_redaction.py",
    "tests/test_m6_audit_integration_postgres.py",
    "tests/test_m6_retention_purge_postgres.py",
    "tests/test_m6_worker_crash_recovery_postgres.py",
    "tests/test_m6_gateway_reconnect_process.py",
    "tests/test_m7_process_boundary.py",
    "tests/test_m7_source_absence.py",
    "tests/test_m7_release_gate_postgres.py",
)


def _make_gate_files() -> tuple[str, ...]:
    result = subprocess.run(
        ["make", "--no-print-directory", "print-project-foundation-postgres-tests"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(result.stdout.split())


def test_root_makefile_is_the_only_ordered_m1_m7_gate_source() -> None:
    assert _make_gate_files() == EXPECTED_M1_M7_GATE
    workflow = (REPO_ROOT / ".github/workflows/project-saas-release-gates.yml").read_text(encoding="utf-8")
    assert "make test-project-foundation-postgres" in workflow
    assert "M1-M7" in workflow
    for test_file in EXPECTED_M1_M7_GATE:
        assert test_file not in workflow


def test_release_gate_contains_only_existing_tests_and_no_deleted_migrations() -> None:
    gate = _make_gate_files()
    assert len(gate) == len(set(gate))
    assert all((REPO_ROOT / "backend" / path).is_file() for path in gate)
    assert not any("migration" in path or "cutover" in path for path in gate)


def test_release_runner_fails_before_pytest_when_postgres_url_is_missing() -> None:
    environment = dict(os.environ)
    environment.pop("POSTGRES_TEST_URL", None)
    result = subprocess.run(
        [sys.executable, "tests/support/release_gate_plugin.py", "--collect-only", "-q"],
        cwd=REPO_ROOT / "backend",
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 4
    assert result.stderr.strip() == "POSTGRES_TEST_URL is required for the PostgreSQL release gate"
    assert "collected" not in result.stdout
