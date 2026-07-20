from __future__ import annotations

import asyncio
import signal
import subprocess
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from scripts.release_acceptance.commands import (
    COMMANDS,
    AsyncCommandExecutor,
    CommandOutcome,
    CommandSpec,
    manifest_digest,
)
from scripts.release_acceptance.evidence import EvidenceWriter
from scripts.release_acceptance.models import (
    CleanupSummary,
    ReleaseEvidence,
    SecuritySummary,
    StageEvidence,
    StageId,
    TestSummary,
)
from scripts.release_acceptance.ownership import OwnershipLedger
from scripts.release_acceptance.preflight import Preflight, PreflightFailure, PreflightResult, PreflightSuccess


class StageExecutor(Protocol):
    async def execute(self, command: CommandSpec, cancel_event: asyncio.Event) -> CommandOutcome: ...


class RunnerGitProbe(Protocol):
    def exact_commit_and_clean(self, repository: Path) -> tuple[str, bool]: ...


class SubprocessRunnerGitProbe:
    def exact_commit_and_clean(self, repository: Path) -> tuple[str, bool]:
        commit = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=repository,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ("git", "status", "--porcelain=v1", "--untracked-files=all"),
            cwd=repository,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ).stdout
        branch = subprocess.run(
            ("git", "symbolic-ref", "--quiet", "--short", "HEAD"),
            cwd=repository,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return commit, not status and branch.returncode == 0


def _stage_evidence(
    *,
    stage: StageId,
    command_id: str,
    started_at: datetime,
    outcome: CommandOutcome,
) -> StageEvidence:
    finished_at = datetime.now(UTC)
    return StageEvidence(
        stage=stage,
        command_id=command_id,
        status=outcome.status,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=int((finished_at - started_at).total_seconds() * 1000),
        passed=outcome.passed,
        failed=outcome.failed,
        skipped=outcome.skipped,
        summary=outcome.summary,
    )


def _failed_outcome(stage: StageId) -> CommandOutcome:
    if stage is StageId.SECURITY:
        summary = SecuritySummary(scanned=0, effective_findings=1)
    else:
        summary = TestSummary(collected=1, passed=0, failed=1, skipped=0)
    return CommandOutcome(status="failed", passed=0, failed=1, skipped=0, summary=summary)


class ReleaseRunner:
    def __init__(
        self,
        *,
        repository: Path,
        preflight: Preflight,
        commands: Sequence[CommandSpec],
        executor: StageExecutor,
        ledger: OwnershipLedger,
        git_probe: RunnerGitProbe,
        acceptance_run_id: uuid.UUID | None = None,
    ) -> None:
        self._repository = repository.resolve()
        self._preflight = preflight
        self._commands = tuple(commands)
        self._executor = executor
        self._ledger = ledger
        self._git_probe = git_probe
        self._acceptance_run_id = acceptance_run_id or uuid.uuid4()
        self._cancel_event = asyncio.Event()
        self._installed_signals: list[signal.Signals] = []

    @classmethod
    def default(cls, repository: Path) -> ReleaseRunner:
        repository = repository.resolve()
        run_id = uuid.uuid4()
        ledger = OwnershipLedger(repository=repository, acceptance_run_id=run_id)
        return cls(
            repository=repository,
            preflight=Preflight(repository=repository),
            commands=COMMANDS,
            executor=AsyncCommandExecutor(repository=repository, ledger=ledger),
            ledger=ledger,
            git_probe=SubprocessRunnerGitProbe(),
            acceptance_run_id=run_id,
        )

    def request_cancel(self) -> None:
        self._cancel_event.set()

    def _install_signals(self) -> None:
        loop = asyncio.get_running_loop()
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signal_number, self.request_cancel)
            except (NotImplementedError, RuntimeError, ValueError):
                continue
            self._installed_signals.append(signal_number)

    def _remove_signals(self) -> None:
        loop = asyncio.get_running_loop()
        for signal_number in self._installed_signals:
            loop.remove_signal_handler(signal_number)
        self._installed_signals.clear()

    async def _prepare_writer(self) -> EvidenceWriter:
        writer = EvidenceWriter(self._repository / ".release-evidence", acceptance_run_id=self._acceptance_run_id)
        run_directory = await asyncio.to_thread(writer.prepare)
        register_path = getattr(self._ledger, "register_path", None)
        if register_path is not None:
            await asyncio.to_thread(register_path, run_directory, disposition="retained_evidence")
        return writer

    async def run(self) -> ReleaseEvidence | PreflightFailure:
        preflight_started = datetime.now(UTC)
        preflight_result: PreflightResult = await self._preflight.check()
        if not isinstance(preflight_result, PreflightSuccess):
            return preflight_result
        writer = await self._prepare_writer()
        self._install_signals()
        stages: list[StageEvidence] = []
        failed = False
        stages.append(
            _stage_evidence(
                stage=StageId.PREFLIGHT,
                command_id="preflight.host",
                started_at=preflight_started,
                outcome=CommandOutcome(
                    status="passed",
                    passed=1,
                    failed=0,
                    skipped=0,
                    summary=TestSummary(collected=1, passed=1, failed=0, skipped=0),
                ),
            )
        )
        try:
            for command in self._commands:
                if failed:
                    break
                started_at = datetime.now(UTC)
                if self._cancel_event.is_set():
                    outcome = _failed_outcome(command.stage)
                else:
                    try:
                        outcome = await self._executor.execute(command, self._cancel_event)
                    except BaseException:
                        outcome = _failed_outcome(command.stage)
                stages.append(
                    _stage_evidence(
                        stage=command.stage,
                        command_id=command.command_id,
                        started_at=started_at,
                        outcome=outcome,
                    )
                )
                failed = outcome.status == "failed" or outcome.skipped > 0 or self._cancel_event.is_set()
        finally:
            cleanup_started = datetime.now(UTC)
            try:
                cleanup = await asyncio.shield(self._ledger.cleanup())
            except BaseException:
                cleanup = CleanupSummary(
                    residual_processes=1,
                    residual_ports=1,
                    residual_databases=1,
                    residual_paths=1,
                    retained_evidence=1,
                )
            cleanup_failed = any(
                (
                    cleanup.residual_processes,
                    cleanup.residual_ports,
                    cleanup.residual_databases,
                    cleanup.residual_paths,
                )
            )
            cleanup_finished = datetime.now(UTC)
            stages.append(
                StageEvidence(
                    stage=StageId.CLEANUP,
                    command_id="cleanup.owned_resources",
                    status="failed" if cleanup_failed else "passed",
                    started_at=cleanup_started,
                    finished_at=cleanup_finished,
                    duration_ms=int((cleanup_finished - cleanup_started).total_seconds() * 1000),
                    passed=0 if cleanup_failed else 1,
                    failed=1 if cleanup_failed else 0,
                    skipped=0,
                    summary=cleanup,
                )
            )
            self._remove_signals()
        try:
            final_commit, final_clean = await asyncio.to_thread(self._git_probe.exact_commit_and_clean, self._repository)
        except BaseException:
            final_commit, final_clean = "", False
        failed = failed or self._cancel_event.is_set() or cleanup_failed or not final_clean or final_commit != preflight_result.git_commit
        digest = manifest_digest(self._commands)
        if failed:
            evidence = ReleaseEvidence.failed(
                acceptance_run_id=self._acceptance_run_id,
                git_commit=preflight_result.git_commit,
                stage_manifest_digest=digest,
                public_config_digest=preflight_result.config_digest,
                toolchain_digest=preflight_result.toolchain_digest,
                stages=tuple(stages),
            )
        else:
            evidence = ReleaseEvidence.candidate(
                acceptance_run_id=self._acceptance_run_id,
                git_commit=preflight_result.git_commit,
                stage_manifest_digest=digest,
                public_config_digest=preflight_result.config_digest,
                toolchain_digest=preflight_result.toolchain_digest,
                stages=tuple(stages),
            )
        await asyncio.to_thread(writer.write, evidence)
        return evidence
