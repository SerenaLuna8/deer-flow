from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

import scripts.release_acceptance.runner as runner_module
from scripts.release_acceptance.commands import CommandOutcome, CommandSpec, manifest_digest
from scripts.release_acceptance.models import CleanupSummary
from scripts.release_acceptance.models import TestSummary as AcceptanceTestSummary
from scripts.release_acceptance.preflight import AcceptanceModel, PreflightSuccess
from scripts.release_acceptance.runner import ReleaseRunner


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


def _commands() -> tuple[CommandSpec, ...]:
    return (
        CommandSpec(command_id="contracts.schemas", stage="contracts", argv=("fixed",), cwd="backend", timeout_seconds=10, allowed_environment=frozenset()),
        CommandSpec(command_id="backend.unit", stage="backend", argv=("fixed",), cwd="backend", timeout_seconds=10, allowed_environment=frozenset()),
    )


def _runner(
    tmp_path: Path,
    *,
    executor: FakeExecutor | BlockingExecutor | None = None,
    ledger: FakeLedger | None = None,
    git: FakeGitProbe | None = None,
) -> ReleaseRunner:
    return ReleaseRunner(
        repository=tmp_path,
        preflight=FakePreflight(),
        commands=_commands(),
        executor=executor or FakeExecutor(),
        ledger=ledger or FakeLedger(),
        git_probe=git or FakeGitProbe(),
        acceptance_run_id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
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
async def test_requested_cancellation_stops_business_stages_but_runs_cleanup(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    runner.request_cancel()
    evidence = await runner.run()
    assert evidence.status == "failed"
    assert evidence.cleanup.residual_processes == 0


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
    with pytest.raises(SystemExit):
        _parse_args(["--stage", "chromium"])
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
