from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import pytest

from scripts.release_acceptance.commands import AsyncCommandExecutor, CommandOutcome, CommandSpec
from scripts.release_acceptance.models import CleanupSummary
from scripts.release_acceptance.models import TestSummary as AcceptanceTestSummary
from scripts.release_acceptance.preflight import AcceptanceModel, PreflightSuccess
from scripts.release_acceptance.runner import ReleaseRunner

pytestmark = pytest.mark.asyncio


class PreflightStub:
    async def check(self) -> PreflightSuccess:
        return PreflightSuccess(
            code="OK",
            git_commit="a" * 40,
            config_digest="b" * 64,
            toolchain_digest="c" * 64,
            model=AcceptanceModel(logical_name="deepseek-live", provider_model_id="deepseek-v4-pro", provider="deepseek"),
            secret_present=True,
        )


class ExecutorStub:
    async def execute(self, _command: CommandSpec, _cancel_event) -> CommandOutcome:
        return CommandOutcome(
            status="passed",
            passed=1,
            failed=0,
            skipped=0,
            summary=AcceptanceTestSummary(collected=1, passed=1, failed=0, skipped=0),
        )


class LedgerStub:
    async def cleanup(self) -> CleanupSummary:
        return CleanupSummary(residual_processes=0, residual_ports=0, residual_databases=0, residual_paths=0, retained_evidence=1)


class GitStub:
    def exact_commit_and_clean(self, _repository: Path) -> tuple[str, bool]:
        return "a" * 40, True


async def test_runner_offloads_atomic_evidence_filesystem_work(tmp_path: Path) -> None:
    command = CommandSpec(
        command_id="backend.blocking_io",
        stage="backend",
        argv=("fixed",),
        cwd="backend",
        timeout_seconds=10,
        allowed_environment=frozenset(),
    )
    runner = ReleaseRunner(
        repository=tmp_path,
        preflight=PreflightStub(),
        commands=(command,),
        executor=ExecutorStub(),
        ledger=LedgerStub(),
        git_probe=GitStub(),
        acceptance_run_id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
    )
    evidence = await runner.run()
    assert evidence.status == "candidate_ready"


async def test_command_executor_uses_async_subprocess_streams(tmp_path: Path) -> None:
    command = CommandSpec(
        command_id="backend.async_subprocess",
        stage="backend",
        argv=(sys.executable, "-c", "print('1 passed')"),
        cwd="root",
        timeout_seconds=10,
        allowed_environment=frozenset(),
    )
    outcome = await AsyncCommandExecutor(repository=tmp_path, env={}).execute(command, asyncio.Event())
    assert outcome.status == "passed"
    assert outcome.passed == 1


async def test_success_exit_without_required_summary_fails_closed(tmp_path: Path) -> None:
    command = CommandSpec(
        command_id="backend.unparseable",
        stage="backend",
        argv=(sys.executable, "-c", "print('completed without test counts')"),
        cwd="root",
        timeout_seconds=10,
        allowed_environment=frozenset(),
    )
    outcome = await AsyncCommandExecutor(repository=tmp_path, env={}).execute(command, asyncio.Event())
    assert outcome.status == "failed"
    assert outcome.failed == 1
