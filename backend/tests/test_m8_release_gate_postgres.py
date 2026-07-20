"""Contracts for the single ordered M1-M8 release gate and deterministic CI."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from support.release_gate_plugin import pytest_sessionfinish

from scripts.release_acceptance.commands import COMMANDS

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_M1_M7_PREFIX = (
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
    "tests/test_m7_backup_restore_postgres.py",
    "tests/test_m6_restore_postgres.py",
    "tests/test_m6_worker_crash_recovery_postgres.py",
    "tests/test_m6_gateway_reconnect_process.py",
    "tests/test_m7_process_boundary.py",
    "tests/test_m7_source_absence.py",
    "tests/test_m7_release_gate_postgres.py",
)
EXPECTED_M8_SUFFIX = (
    "tests/test_m8_isolation_matrix_postgres.py",
    "tests/test_m8_capacity_postgres.py",
    "tests/test_m8_recovery_switch_postgres.py",
    "tests/test_m8_release_gate_postgres.py",
)
EXPECTED_COMMAND_IDS = (
    "contracts.schemas",
    "contracts.matrix",
    "contracts.docs",
    "contracts.git_diff",
    "postgres.m1_m8",
    "backend.full",
    "backend.blocking_io",
    "backend.format",
    "backend.lint",
    "frontend.install_frozen",
    "frontend.unit",
    "frontend.check",
    "frontend.e2e_deterministic",
    "frontend.build_production",
    "frontend.build_static",
    "security.python_dependencies",
    "security.frontend_dependencies",
    "security.tracked_tree",
    "security.review_diff",
    "security.git_history",
    "host.setup_db",
    "host.check_db",
    "host.doctor",
    "host.make_help",
    "host.support_bundle",
    "host.make_start",
    "chromium.host_journey",
    "deepseek.live_journey",
    "recovery.full_switch",
    "cleanup.evidence_log_security",
    "cleanup.residual_audit",
)


def _make_words(target: str) -> tuple[str, ...]:
    result = subprocess.run(
        ("make", "--no-print-directory", target),
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(result.stdout.split())


def test_m1_m8_gate_is_exact_m1_m7_prefix_plus_m8_suffix() -> None:
    assert _make_words("print-project-foundation-postgres-tests") == EXPECTED_M1_M7_PREFIX
    assert _make_words("print-project-saas-postgres-tests") == (
        *EXPECTED_M1_M7_PREFIX,
        *EXPECTED_M8_SUFFIX,
    )


def test_root_targets_and_help_use_final_gate_without_mutating_release_environment() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "test: test-project-saas-postgres" in makefile
    assert "make test-project-saas-postgres" in makefile
    assert "release-acceptance:\n\t@cd backend && uv run python scripts/run_release_acceptance.py" in makefile
    release_recipe = makefile.split("release-acceptance:", 1)[1].split("\n\n", 1)[0]
    assert "ARGS" not in release_recipe
    assert "docker" not in release_recipe.casefold()
    assert "helm" not in release_recipe.casefold()
    assert "M8_LIVE_ACCEPTANCE" not in release_recipe


def test_ci_uses_makefile_authority_without_live_or_duplicated_paths() -> None:
    workflow_path = REPO_ROOT / ".github/workflows/project-saas-release-gates.yml"
    workflow = workflow_path.read_text(encoding="utf-8")
    assert "fetch-depth: 0" in workflow
    assert "make test-project-saas-postgres" in workflow
    assert "DEEPSEEK_API_KEY" not in workflow
    assert "M8_LIVE_ACCEPTANCE" not in workflow
    assert "recovery.full_switch" not in workflow
    assert "docker" not in workflow.casefold()
    assert "helm" not in workflow.casefold()
    job_environment = workflow.split("    env:\n", 1)[1].split("\n\n    steps:", 1)[0]
    assert "POSTGRES_TEST_URL" not in job_environment
    assert workflow.count("          POSTGRES_TEST_URL: ") == 2
    for test_file in EXPECTED_M8_SUFFIX:
        assert test_file not in workflow
    assert not (REPO_ROOT / ".github/workflows/project-foundation-postgres-tests.yml").exists()


def test_release_gate_plugin_requires_exact_label_and_zero_skips() -> None:
    environment = dict(os.environ)
    environment["POSTGRES_TEST_URL"] = "postgresql://synthetic.invalid/postgres"
    environment["DEER_FLOW_RELEASE_GATE_LABEL"] = "invalid"
    result = subprocess.run(
        (sys.executable, "tests/support/release_gate_plugin.py", "--collect-only", "-q"),
        cwd=REPO_ROOT / "backend",
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 4
    assert result.stderr.strip() == "DEER_FLOW_RELEASE_GATE_LABEL must be M1-M7 or M1-M8"


def test_full_release_command_manifest_is_exact_and_contains_no_container_stage() -> None:
    assert tuple(command.command_id for command in COMMANDS) == EXPECTED_COMMAND_IDS
    flattened = " ".join(item for command in COMMANDS for item in command.argv).casefold()
    assert "docker" not in flattened
    assert "helm" not in flattened
    assert len({command.command_id for command in COMMANDS}) == len(COMMANDS)


def test_backend_commands_force_ci_and_remove_postgres_gate_environment() -> None:
    commands = {command.command_id: command for command in COMMANDS}
    postgres = commands["postgres.m1_m8"]
    assert postgres.fixed_environment == (
        (
            "DATABASE_URL",
            "postgresql://m8-deterministic@127.0.0.1:5432/m8-deterministic",
        ),
        ("DEEPSEEK_API_KEY", "m8-deterministic-placeholder"),
    )
    assert postgres.allowed_environment == frozenset({"DATABASE_URL", "DEEPSEEK_API_KEY", "POSTGRES_TEST_URL"})
    for command_id in ("backend.full", "backend.blocking_io"):
        command = commands[command_id]
        assert command.fixed_environment == (
            ("CI", "1"),
            (
                "DATABASE_URL",
                "postgresql://m8-deterministic@127.0.0.1:5432/m8-deterministic",
            ),
            ("DEEPSEEK_API_KEY", "m8-deterministic-placeholder"),
        )
        assert command.removed_environment == frozenset(
            {
                "DEER_FLOW_CONFIG_PATH",
                "POSTGRES_ADMIN_URL",
                "POSTGRES_TEST_URL",
            }
        )


def test_release_gate_stats_count_both_pytest_error_buckets(monkeypatch) -> None:
    lines: list[str] = []
    reporter = SimpleNamespace(
        stats={"passed": [object()], "failed": [object()], "error": [object()], "errors": [object()]},
        write_line=lines.append,
    )
    session = SimpleNamespace(
        testscollected=4,
        exitstatus=0,
        config=SimpleNamespace(pluginmanager=SimpleNamespace(get_plugin=lambda _name: reporter)),
    )
    monkeypatch.setenv("DEER_FLOW_REQUIRE_ZERO_SKIPS", "1")
    monkeypatch.setenv("DEER_FLOW_RELEASE_GATE_LABEL", "M1-M8")

    pytest_sessionfinish(session, 0)

    assert lines == ["M1-M8 release stats: collected=4 passed=1 failed=3 skipped=0"]


def test_m8_runbook_covers_candidate_review_final_and_scope_without_secrets() -> None:
    runbook = (REPO_ROOT / "docs/operations/m8-host-release-acceptance.md").read_text(encoding="utf-8")
    for required in (
        "candidate_ready",
        "final_pass",
        "M8_CANDIDATE_MANIFEST",
        "M8_REVIEW_REPORT_PATH",
        "evidence_relative_locator",
        "3f574b89..HEAD",
        "Critical",
        "Important",
        "Minor",
        "Docker Compose",
        "Kubernetes",
        "Helm",
        "Firefox",
        "Safari/WebKit",
        "credential",
        "轮换",
        "保留策略",
    ):
        assert required in runbook
    assert "sk-" not in runbook
    assert "postgresql://" not in runbook
