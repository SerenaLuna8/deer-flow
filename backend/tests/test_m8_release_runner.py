from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import scripts.release_acceptance.runner as runner_module
from scripts.release_acceptance.commands import (
    AsyncCommandExecutor,
    CommandOutcome,
    CommandSpec,
    diagnostic_stages,
    manifest_digest,
)
from scripts.release_acceptance.models import (
    M8_REVIEW_BASE_COMMIT,
    CleanupSummary,
    LiveModelSummary,
    RecoverySummary,
    ReviewReport,
    StageId,
)
from scripts.release_acceptance.models import TestSummary as AcceptanceTestSummary
from scripts.release_acceptance.preflight import AcceptanceModel, PreflightSuccess
from scripts.release_acceptance.runner import (
    DiagnosticResult,
    HostCommandTiming,
    ReleaseRunner,
)


class FakePreflight:
    def __init__(self) -> None:
        self.result = PreflightSuccess(
            code="OK",
            git_commit="a" * 40,
            config_digest="b" * 64,
            toolchain_digest="c" * 64,
            model=AcceptanceModel(logical_name="deepseek-live", provider_model_id="deepseek-v4-pro", provider="deepseek"),
            secret_present=True,
        )

    async def check(self) -> PreflightSuccess:
        return self.result


class FakeGitProbe:
    def __init__(self, commits: tuple[str, ...] = ("a" * 40,), *, clean: bool = True) -> None:
        self.commits = list(commits)
        self.clean = clean

    def exact_commit_and_clean(self, _repository: Path) -> tuple[str, bool]:
        commit = self.commits.pop(0) if len(self.commits) > 1 else self.commits[0]
        return commit, self.clean


class FakeLedger:
    def __init__(self, cleanup: CleanupSummary | None = None) -> None:
        self.cleanup_calls = 0
        self.cleanup_result = cleanup or CleanupSummary(
            residual_processes=0,
            residual_ports=0,
            residual_databases=0,
            residual_paths=0,
            retained_evidence=1,
        )

    async def cleanup(self) -> CleanupSummary:
        self.cleanup_calls += 1
        return self.cleanup_result


@dataclass
class FakeExecutor:
    fail_command: str | None = None
    raw_error: str = "raw private error"

    def __post_init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, command: CommandSpec, _cancel_event) -> CommandOutcome:
        self.calls.append(command.command_id)
        if command.command_id == self.fail_command:
            raise RuntimeError(self.raw_error)
        return CommandOutcome(
            status="passed",
            passed=1,
            failed=0,
            skipped=0,
            summary=AcceptanceTestSummary(collected=1, passed=1, failed=0, skipped=0),
        )


class BlockingExecutor:
    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def execute(self, _command: CommandSpec, _cancel_event) -> CommandOutcome:
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class SkipExecutor:
    async def execute(self, _command: CommandSpec, _cancel_event) -> CommandOutcome:
        return CommandOutcome(
            status="passed",
            passed=0,
            failed=0,
            skipped=1,
            summary=AcceptanceTestSummary(
                collected=1,
                passed=0,
                failed=0,
                skipped=1,
            ),
        )


def _commands() -> tuple[CommandSpec, ...]:
    return (
        CommandSpec(command_id="contracts.schemas", stage="contracts", argv=("fixed",), cwd="backend", timeout_seconds=10, allowed_environment=frozenset()),
        CommandSpec(command_id="backend.unit", stage="backend", argv=("fixed",), cwd="backend", timeout_seconds=10, allowed_environment=frozenset()),
    )


def test_command_environment_can_force_ci_and_remove_separately_gated_database_url(
    tmp_path: Path,
) -> None:
    command = CommandSpec(
        command_id="backend.full",
        stage="backend",
        argv=("fixed",),
        cwd="backend",
        timeout_seconds=10,
        allowed_environment=frozenset({"CI", "DEEPSEEK_API_KEY", "POSTGRES_TEST_URL"}),
        fixed_environment=(("CI", "1"),),
        removed_environment=frozenset({"POSTGRES_TEST_URL"}),
    )
    executor = AsyncCommandExecutor(
        repository=tmp_path,
        env={
            "CI": "operator-value",
            "DEEPSEEK_API_KEY": "present-but-not-serialized",
            "POSTGRES_TEST_URL": "must-not-pass",
            "PWD": str(tmp_path),
            "UNRELATED": "must-not-pass",
        },
    )
    child = executor._child_environment(command)
    assert child["CI"] == "1"
    assert child["DEEPSEEK_API_KEY"] == "present-but-not-serialized"
    assert child["PWD"] == str(tmp_path)
    assert "POSTGRES_TEST_URL" not in child
    assert "UNRELATED" not in child


def test_pytest_summary_prefers_exact_release_stats_without_double_counting() -> None:
    output = b"M1-M8 release stats: collected=322 passed=322 failed=0 skipped=0\n======================= 322 passed in 246.74s =======================\n"

    outcome = AsyncCommandExecutor._test_summary(output, returncode=0)

    assert outcome.summary == AcceptanceTestSummary(
        collected=322,
        passed=322,
        failed=0,
        skipped=0,
    )


def test_rstest_summary_ignores_run_tests_stack_frames_after_success() -> None:
    output = b" Test Files 123 passed\n      Tests 893 passed\n   Duration 3.48s\nat runTests (file:///workspace/node_modules/@rstest/core/dist/api.js:100:20)\n"

    outcome = AsyncCommandExecutor._vitest_summary(output, returncode=0)

    assert outcome.summary == AcceptanceTestSummary(
        collected=893,
        passed=893,
        failed=0,
        skipped=0,
    )


def test_playwright_summary_records_the_complete_test_inventory() -> None:
    output = b"Running 79 tests using 7 workers\n  79 passed (39.5s)\n"

    outcome = AsyncCommandExecutor._playwright_summary(output, returncode=0)

    assert outcome.summary == AcceptanceTestSummary(
        collected=79,
        passed=79,
        failed=0,
        skipped=0,
    )


def test_playwright_summary_rejects_retry_flakes_even_with_zero_exit() -> None:
    output = b"Running 79 tests using 7 workers\n  1 flaky\n  78 passed (41.5s)\n"

    outcome = AsyncCommandExecutor._playwright_summary(output, returncode=0)

    assert outcome.status == "failed"
    assert outcome.summary == AcceptanceTestSummary(
        collected=79,
        passed=78,
        failed=1,
        skipped=0,
    )


def test_deterministic_command_output_is_secret_scanned_in_memory() -> None:
    assert AsyncCommandExecutor._runtime_log_is_safe(b"ordinary bounded test output")
    assert not AsyncCommandExecutor._runtime_log_is_safe(b"provider returned sk-" + b"a" * 32)


def test_security_summary_records_dependency_package_count() -> None:
    output = json.dumps(
        {
            "database_timestamp": "2026-07-21T01:02:03Z",
            "effective_findings": 0,
            "exclusion_ids": ["GHSA-synthetic-absence-proof"],
            "results": [
                {
                    "ecosystem": "python",
                    "database_timestamp": "2026-07-21T01:02:03Z",
                    "effective_findings": 0,
                    "exclusion_ids": ["GHSA-synthetic-absence-proof"],
                    "findings": [],
                    "scanned_packages": 202,
                }
            ],
            "schema_version": 1,
        }
    ).encode()

    outcome = AsyncCommandExecutor._security_summary(output, returncode=0)

    assert outcome.status == "passed"
    assert outcome.summary.scanned == 202
    assert outcome.summary.effective_findings == 0
    assert outcome.summary.database_timestamp == datetime(2026, 7, 21, 1, 2, 3, tzinfo=UTC)
    assert outcome.summary.exclusion_ids == ("GHSA-synthetic-absence-proof",)


def test_matrix_summary_records_coverage_and_zero_uncovered() -> None:
    repository = Path(__file__).resolve().parents[2]
    outcome = AsyncCommandExecutor(repository=repository)._matrix_summary(b"7 passed in 0.31s\n", returncode=0)

    assert outcome.status == "passed"
    assert outcome.passed == 7
    assert outcome.failed == 0
    assert outcome.summary.coverage_count > 0
    assert outcome.summary.selector_count > 0
    assert outcome.summary.uncovered_count == 0


def _runner(
    tmp_path: Path,
    *,
    executor: FakeExecutor | BlockingExecutor | None = None,
    ledger: FakeLedger | None = None,
    git: FakeGitProbe | None = None,
    review_report: ReviewReport | None = None,
    commands: tuple[CommandSpec, ...] | None = None,
    host_acceptance_runner=None,
) -> ReleaseRunner:
    return ReleaseRunner(
        repository=tmp_path,
        preflight=FakePreflight(),
        commands=commands or _commands(),
        executor=executor or FakeExecutor(),
        ledger=ledger or FakeLedger(),
        git_probe=git or FakeGitProbe(),
        acceptance_run_id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
        review_report=review_report,
        **({"host_acceptance_runner": host_acceptance_runner} if host_acceptance_runner is not None else {}),
    )


@pytest.mark.asyncio
async def test_runner_failure_still_cleans_exact_resources_and_redacts_error(tmp_path: Path) -> None:
    executor = FakeExecutor(fail_command="contracts.schemas")
    ledger = FakeLedger()
    evidence = await _runner(tmp_path, executor=executor, ledger=ledger).run()
    assert evidence.status == "failed"
    assert evidence.cleanup.residual_processes == 0
    assert ledger.cleanup_calls == 1
    assert executor.calls == ["contracts.schemas"]
    encoded = evidence.model_dump_json()
    assert "raw private error" not in encoded
    assert json.loads((tmp_path / ".release-evidence" / str(evidence.acceptance_run_id) / "manifest.json").read_text(encoding="utf-8"))["status"] == "failed"


@pytest.mark.asyncio
async def test_generated_manifest_is_scanned_before_atomic_publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def scan_manifest(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        nonlocal calls
        calls += 1
        return (object(),) if calls == 1 else ()

    monkeypatch.setattr(
        runner_module.SecretScanner,
        "scan_bytes",
        scan_manifest,
    )

    evidence = await _runner(tmp_path).run()

    assert calls == 2
    assert evidence.status == "failed"
    assert evidence.stages[-1].stage is StageId.CLEANUP
    assert evidence.stages[-1].status == "failed"
    manifest = tmp_path / ".release-evidence" / str(evidence.acceptance_run_id) / "manifest.json"
    assert json.loads(manifest.read_text(encoding="utf-8"))["status"] == "failed"


@pytest.mark.asyncio
async def test_cleanup_failure_overrides_candidate_status(tmp_path: Path) -> None:
    cleanup = CleanupSummary(residual_processes=1, residual_ports=0, residual_databases=0, residual_paths=0, retained_evidence=1)
    evidence = await _runner(tmp_path, ledger=FakeLedger(cleanup)).run()
    assert evidence.status == "failed"
    assert evidence.cleanup.residual_processes == 1


@pytest.mark.asyncio
async def test_commit_change_after_cleanup_fails_closed(tmp_path: Path) -> None:
    evidence = await _runner(tmp_path, git=FakeGitProbe(("d" * 40,))).run()
    assert evidence.status == "failed"


@pytest.mark.asyncio
async def test_dirty_tree_after_cleanup_fails_closed(tmp_path: Path) -> None:
    evidence = await _runner(tmp_path, git=FakeGitProbe(clean=False)).run()
    assert evidence.status == "failed"


@pytest.mark.asyncio
async def test_success_pins_immutable_manifest_and_candidate_commit(tmp_path: Path) -> None:
    commands = _commands()
    runner = _runner(tmp_path)
    evidence = await runner.run()
    assert evidence.status == "candidate_ready"
    assert evidence.git_commit == "a" * 40
    assert evidence.stage_manifest_digest == manifest_digest(commands)
    with pytest.raises(Exception):
        commands[0].argv += ("injected",)


@pytest.mark.asyncio
async def test_matching_review_report_reexecutes_fresh_stages_and_seals_final(tmp_path: Path) -> None:
    (tmp_path / "candidate").mkdir()
    (tmp_path / "final").mkdir()
    candidate_executor = FakeExecutor()
    candidate = await _runner(tmp_path / "candidate", executor=candidate_executor).run()
    report = ReviewReport.for_candidate(
        candidate,
        review_base_commit=M8_REVIEW_BASE_COMMIT,
        review_range=f"{M8_REVIEW_BASE_COMMIT}..{candidate.git_commit}",
        critical=0,
        important=0,
        minor=0,
    )
    final_executor = FakeExecutor()
    final = await _runner(
        tmp_path / "final",
        executor=final_executor,
        review_report=report,
    ).run()
    assert final.status == "final_pass"
    assert final_executor.calls == ["contracts.schemas", "backend.unit"]
    assert final.review == report
    assert final.candidate_evidence_digest != report.candidate_evidence_digest


@pytest.mark.asyncio
async def test_review_mismatch_fails_before_commands_or_evidence_directory(tmp_path: Path) -> None:
    (tmp_path / "candidate").mkdir()
    (tmp_path / "final").mkdir()
    candidate = await _runner(tmp_path / "candidate").run()
    report = ReviewReport.for_candidate(
        candidate,
        review_base_commit=M8_REVIEW_BASE_COMMIT,
        review_range=f"{M8_REVIEW_BASE_COMMIT}..{candidate.git_commit}",
        critical=0,
        important=0,
        minor=0,
    ).model_copy(update={"candidate_commit": "e" * 40})
    executor = FakeExecutor()
    final_root = tmp_path / "final"
    result = await _runner(final_root, executor=executor, review_report=report).run()
    assert result.code == "REVIEW_BINDING_MISMATCH"
    assert executor.calls == []
    assert not (final_root / ".release-evidence").exists()


@pytest.mark.asyncio
async def test_review_range_must_start_at_fixed_m8_baseline(tmp_path: Path) -> None:
    (tmp_path / "candidate").mkdir()
    (tmp_path / "final").mkdir()
    candidate = await _runner(tmp_path / "candidate").run()
    short_base = "e" * 40
    report = ReviewReport.for_candidate(
        candidate,
        review_base_commit=short_base,
        review_range=f"{short_base}..{candidate.git_commit}",
        critical=0,
        important=0,
        minor=0,
    )
    executor = FakeExecutor()
    result = await _runner(
        tmp_path / "final",
        executor=executor,
        review_report=report,
    ).run()
    assert result.code == "REVIEW_RANGE_MISMATCH"
    assert executor.calls == []
    assert not (tmp_path / "final" / ".release-evidence").exists()


@pytest.mark.asyncio
async def test_host_manifest_is_dispatched_once_without_executing_internal_commands(
    tmp_path: Path,
) -> None:
    commands = (
        *_commands(),
        CommandSpec(command_id="host.setup_db", stage="host_setup", argv=("make", "setup-db"), cwd="root", timeout_seconds=10, allowed_environment=frozenset(), summary_parser="exit_code", execution="host"),
        CommandSpec(command_id="host.check_db", stage="host_setup", argv=("make", "check-db"), cwd="root", timeout_seconds=10, allowed_environment=frozenset(), summary_parser="exit_code", execution="host"),
        CommandSpec(command_id="chromium.host_journey", stage="chromium", argv=("internal", "chromium"), cwd="root", timeout_seconds=10, allowed_environment=frozenset(), summary_parser="exit_code", execution="host"),
        CommandSpec(command_id="deepseek.live_journey", stage="deepseek", argv=("internal", "deepseek"), cwd="root", timeout_seconds=10, allowed_environment=frozenset(), summary_parser="exit_code", execution="host"),
        CommandSpec(command_id="recovery.full_switch", stage="recovery", argv=("internal", "recovery"), cwd="root", timeout_seconds=10, allowed_environment=frozenset(), summary_parser="exit_code", execution="host"),
        CommandSpec(command_id="cleanup.evidence_log_security", stage="cleanup", argv=("internal", "security"), cwd="root", timeout_seconds=10, allowed_environment=frozenset(), summary_parser="exit_code", execution="cleanup"),
        CommandSpec(command_id="cleanup.residual_audit", stage="cleanup", argv=("internal", "residual"), cwd="root", timeout_seconds=10, allowed_environment=frozenset(), summary_parser="exit_code", execution="cleanup"),
    )
    host_calls: list[tuple[StageId, ...]] = []

    async def host_acceptance_runner(**kwargs):
        host_calls.append(kwargs["stages"])
        started = datetime(2026, 7, 21, 1, 2, 3, tzinfo=UTC)

        def timing(command_id: str, offset: int) -> HostCommandTiming:
            command_started = started + timedelta(seconds=offset)
            return HostCommandTiming(
                command_id=command_id,
                started_at=command_started,
                finished_at=command_started + timedelta(milliseconds=250),
                duration_ms=250,
            )

        return DiagnosticResult(
            status="passed",
            code="OK",
            host_setup_passed=True,
            chromium=AcceptanceTestSummary(collected=4, passed=4, failed=0, skipped=0),
            deepseek=LiveModelSummary(
                provider="deepseek",
                logical_model_name="release-live",
                provider_model_id="deepseek-v4-pro",
                outcome="completed",
                frame_count=3,
                tool_call_count=1,
                terminal_count=1,
                cursor_count=3,
                duration_ms=5,
            ),
            recovery=RecoverySummary(
                archive_schema_version=7,
                schema_revision="0001_project_saas_baseline",
                tombstone_count=1,
                proof_digest="f" * 64,
                rto_ms=5,
                rpo_outcome="archive_point_confirmed",
                restored_count=1,
            ),
            cleanup=CleanupSummary(
                residual_processes=0,
                residual_ports=0,
                residual_databases=0,
                residual_paths=0,
                retained_evidence=0,
            ),
            timings=(
                timing("host.setup_db", 0),
                timing("host.check_db", 1),
                timing("chromium.host_journey", 2),
                timing("deepseek.live_journey", 3),
                timing("recovery.full_switch", 4),
            ),
        )

    executor = FakeExecutor()
    evidence = await _runner(
        tmp_path,
        commands=commands,
        executor=executor,
        host_acceptance_runner=host_acceptance_runner,
    ).run()
    assert evidence.status == "candidate_ready"
    assert executor.calls == ["contracts.schemas", "backend.unit"]
    assert host_calls == [(StageId.HOST_SETUP, StageId.CHROMIUM, StageId.DEEPSEEK, StageId.RECOVERY)]
    assert tuple(stage.command_id for stage in evidence.stages) == (
        "preflight.host",
        "contracts.schemas",
        "backend.unit",
        "host.setup_db",
        "host.check_db",
        "chromium.host_journey",
        "deepseek.live_journey",
        "recovery.full_switch",
        "cleanup.evidence_log_security",
        "cleanup.residual_audit",
    )
    host_evidence = {stage.command_id: stage for stage in evidence.stages if stage.command_id.startswith(("host.", "chromium.", "deepseek.", "recovery."))}
    assert all(item.duration_ms == 250 for item in host_evidence.values())
    assert host_evidence["host.setup_db"].started_at != host_evidence["host.check_db"].started_at


@pytest.mark.asyncio
async def test_requested_cancellation_stops_business_stages_but_runs_cleanup(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    runner.request_cancel()
    evidence = await runner.run()
    assert evidence.status == "failed"
    assert evidence.cleanup.residual_processes == 0


@pytest.mark.asyncio
async def test_only_explicit_zero_skip_commands_reject_expected_platform_skips(
    tmp_path: Path,
) -> None:
    allowed = (
        CommandSpec(
            command_id="backend.full",
            stage="backend",
            argv=("fixed",),
            cwd="backend",
            timeout_seconds=10,
            allowed_environment=frozenset(),
        ),
    )
    rejected = (
        CommandSpec(
            command_id="postgres.m1_m8",
            stage="postgres",
            argv=("fixed",),
            cwd="root",
            timeout_seconds=10,
            allowed_environment=frozenset(),
            require_zero_skips=True,
        ),
    )
    allowed_root = tmp_path / "allowed"
    rejected_root = tmp_path / "rejected"
    allowed_root.mkdir()
    rejected_root.mkdir()
    allowed_evidence = await _runner(
        allowed_root,
        commands=allowed,
        executor=SkipExecutor(),
    ).run()
    rejected_evidence = await _runner(
        rejected_root,
        commands=rejected,
        executor=SkipExecutor(),
    ).run()
    assert allowed_evidence.status == "candidate_ready"
    assert rejected_evidence.status == "failed"


@pytest.mark.asyncio
async def test_task_cancellation_uses_same_cleanup_and_failed_evidence_path(tmp_path: Path) -> None:
    executor = BlockingExecutor()
    runner = _runner(tmp_path, executor=executor)
    task = asyncio.create_task(runner.run())
    await executor.entered.wait()
    task.cancel()
    evidence = await task
    assert evidence.status == "failed"
    assert evidence.cleanup.residual_processes == 0


def test_cli_rejects_resume_and_arbitrary_command() -> None:
    from scripts.run_release_acceptance import _parse_args

    with pytest.raises(SystemExit):
        _parse_args(["--resume"])
    with pytest.raises(SystemExit):
        _parse_args(["--command", "make", "stop"])


def test_cli_accepts_only_fixed_diagnostic_stage_prefixes() -> None:
    from scripts.run_release_acceptance import _parse_args

    assert _parse_args(["--stage", "host_setup"]).stage == ["host_setup"]
    assert _parse_args(["--stage", "host_setup", "--stage", "chromium"]).stage == [
        "host_setup",
        "chromium",
    ]
    assert _parse_args(
        [
            "--stage",
            "host_setup",
            "--stage",
            "chromium",
            "--stage",
            "deepseek",
        ]
    ).stage == ["host_setup", "chromium", "deepseek"]
    assert diagnostic_stages(("host_setup", "chromium", "deepseek")) == (StageId.HOST_SETUP, StageId.CHROMIUM, StageId.DEEPSEEK)
    assert _parse_args(
        [
            "--stage",
            "host_setup",
            "--stage",
            "chromium",
            "--stage",
            "deepseek",
            "--stage",
            "recovery",
        ]
    ).stage == ["host_setup", "chromium", "deepseek", "recovery"]
    assert diagnostic_stages(("host_setup", "chromium", "deepseek", "recovery")) == (
        StageId.HOST_SETUP,
        StageId.CHROMIUM,
        StageId.DEEPSEEK,
        StageId.RECOVERY,
    )
    with pytest.raises(SystemExit):
        _parse_args(["--stage", "chromium"])
    with pytest.raises(SystemExit):
        _parse_args(["--stage", "deepseek"])
    with pytest.raises(SystemExit):
        _parse_args(["--stage", "backend"])


def test_cli_direct_script_entrypoint_bootstraps_backend_imports() -> None:
    backend = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        (sys.executable, "scripts/run_release_acceptance.py", "--help"),
        cwd=backend,
        env=environment,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stdout
    assert "ModuleNotFoundError" not in completed.stdout


def test_owned_frontend_tsconfig_extends_tracked_configuration(tmp_path: Path) -> None:
    runtime_root = tmp_path / ".m8-next-11111111111111111111111111111111"
    runtime_root.mkdir()
    writer = getattr(runner_module, "_write_owned_frontend_tsconfig", None)
    assert writer is not None
    writer(runtime_root)
    assert json.loads((runtime_root / "tsconfig.json").read_text(encoding="utf-8")) == {"extends": "../tsconfig.json"}
