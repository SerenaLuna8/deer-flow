from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import secrets
import signal
import subprocess
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import unquote, urlsplit

from app.recovery.archive import load_backup_key
from app.reliability.owner_refs import AuditHmacKeyring
from scripts.release_acceptance.commands import (
    COMMANDS,
    AsyncCommandExecutor,
    CommandOutcome,
    CommandSpec,
    manifest_digest,
)
from scripts.release_acceptance.evidence import EvidenceWriter
from scripts.release_acceptance.host_stack import (
    AsyncpgHostDatabaseManager,
    OwnedHostStack,
    SubprocessHostCommandRunner,
)
from scripts.release_acceptance.live_probe import ChromiumJourneyRunner, RecoveryBrowserProbe
from scripts.release_acceptance.models import (
    M8_REVIEW_BASE_COMMIT,
    CleanupSummary,
    LiveModelSummary,
    RecoverySummary,
    ReleaseEvidence,
    ReviewReport,
    SecuritySummary,
    StageEvidence,
    StageId,
    StrictModel,
    TestSummary,
)
from scripts.release_acceptance.ownership import OwnershipLedger
from scripts.release_acceptance.preflight import Preflight, PreflightFailure, PreflightResult, PreflightSuccess
from scripts.release_acceptance.recovery_drill import PostgresRecoveryOperations, RecoverySwitchDrill


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


class DiagnosticResult(StrictModel):
    status: Literal["passed", "failed"]
    code: str
    host_setup_passed: bool
    chromium: TestSummary | None = None
    deepseek: LiveModelSummary | None = None
    recovery: RecoverySummary | None = None
    evidence_log_security_passed: bool = True
    cleanup: CleanupSummary


HostAcceptanceRunner = Callable[..., Awaitable[DiagnosticResult | PreflightFailure]]


def _has_residual(cleanup: CleanupSummary) -> bool:
    return any(
        (
            cleanup.residual_processes,
            cleanup.residual_ports,
            cleanup.residual_databases,
            cleanup.residual_paths,
        )
    )


def _write_owned_frontend_tsconfig(frontend_runtime_root: Path) -> None:
    with (frontend_runtime_root / "tsconfig.json").open("x", encoding="utf-8") as handle:
        handle.write('{"extends":"../tsconfig.json"}\n')


def _audit_keyring(environment: dict[str, str]) -> AuditHmacKeyring:
    try:
        active = environment["DEER_FLOW_AUDIT_ACTIVE_KEY_ID"]
        raw = json.loads(environment["DEER_FLOW_AUDIT_KEYRING_JSON"])
        if not isinstance(raw, dict):
            raise ValueError
        keys = {key_id: base64.b64decode(value, validate=True) for key_id, value in raw.items() if isinstance(key_id, str) and isinstance(value, str)}
        if len(keys) != len(raw):
            raise ValueError
        return AuditHmacKeyring(active_key_id=active, _keys=keys)
    except (KeyError, ValueError, TypeError, json.JSONDecodeError, binascii.Error):
        raise RuntimeError("M8_AUDIT_KEYRING_INVALID") from None


async def run_host_diagnostic(
    *,
    repository: Path,
    stages: tuple[StageId, ...],
    env: dict[str, str] | None = None,
    acceptance_run_id: uuid.UUID | None = None,
) -> DiagnosticResult | PreflightFailure:
    repository = repository.resolve()
    environment = dict(os.environ if env is None else env)
    preflight = Preflight(repository=repository, env=environment)
    preflight_result = await preflight.check()
    if not isinstance(preflight_result, PreflightSuccess):
        return preflight_result

    run_id = acceptance_run_id or uuid.uuid4()
    runtime_root = repository / f".m8-runtime-{run_id.hex}"
    frontend_runtime_root = repository / "frontend" / f".m8-next-{run_id.hex}"
    recovery_root = Path(tempfile.gettempdir()).resolve() / f"deerflow-m8-recovery-{run_id.hex}"
    admin_url = environment.get("POSTGRES_ADMIN_URL", "")
    database_url = environment.get("DATABASE_URL", "")
    parsed = urlsplit(database_url.replace("postgresql+asyncpg://", "postgresql://", 1))
    app_role = unquote(parsed.username or "")
    database = AsyncpgHostDatabaseManager(admin_url)
    ledger = OwnershipLedger(
        repository=repository,
        acceptance_run_id=run_id,
        database_probe=database,
    )
    host: OwnedHostStack | None = None
    browser_summary: TestSummary | None = None
    live_summary: LiveModelSummary | None = None
    recovery_summary: RecoverySummary | None = None
    host_passed = False
    code = "HOST_DIAGNOSTIC_FAILED"
    evidence_log_security_passed = False
    try:
        await asyncio.to_thread(runtime_root.mkdir, mode=0o700)
        ledger.register_path(runtime_root, disposition="temporary")
        await asyncio.to_thread(frontend_runtime_root.mkdir, mode=0o700)
        ledger.register_path(frontend_runtime_root, disposition="temporary")
        if StageId.RECOVERY in stages:
            await asyncio.to_thread(recovery_root.mkdir, mode=0o700)
            ledger.register_external_path(recovery_root)
        await asyncio.to_thread(_write_owned_frontend_tsconfig, frontend_runtime_root)
        child_environment = dict(environment)
        child_environment.update(
            {
                "DEER_FLOW_HOME": str(runtime_root / "deer-flow-home"),
                "DEER_FLOW_RUNTIME_ROOT": str(runtime_root),
                "DEER_FLOW_NEXT_DIST_DIR": f"{frontend_runtime_root.name}/.next",
            }
        )
        from deerflow.config import get_app_config

        scheduler_enabled = await asyncio.to_thread(lambda: bool(get_app_config().scheduler.enabled))
        command_runner = SubprocessHostCommandRunner(repository=repository, ledger=ledger)
        host = OwnedHostStack(
            repository=repository,
            env=child_environment,
            acceptance_run_id=run_id,
            app_role=app_role,
            ledger=ledger,
            database_manager=database,
            command_runner=command_runner,
            scheduler_enabled=scheduler_enabled,
        )
        await host.prepare(database_url)
        await host.run_release_checks()
        await host.launch()
        host_passed = True
        if StageId.CHROMIUM in stages:
            journey_runner = ChromiumJourneyRunner(
                command_runner=command_runner,
                environment=child_environment,
                runtime_root=runtime_root,
            )
            journey = await journey_runner.run(
                live_model=(preflight_result.model if StageId.DEEPSEEK in stages else None),
                live_database_url=(host.database_url if StageId.DEEPSEEK in stages else None),
                restart_gateway=(host.restart_gateway if StageId.DEEPSEEK in stages else None),
                capture_recovery_authority=StageId.RECOVERY in stages,
            )
            browser_summary = journey.tests
            live_summary = journey.live_model
            if StageId.RECOVERY in stages:
                source_app_url = host.database_url
                backup_key = load_backup_key(
                    environment.get("DEER_FLOW_BACKUP_KEY"),
                    database_url=source_app_url,
                )
                journal_key = secrets.token_bytes(32)
                while journal_key == backup_key:
                    journal_key = secrets.token_bytes(32)
                authority = journey_runner.recovery_authority
                recovery_browser = RecoveryBrowserProbe(
                    command_runner=command_runner,
                    environment=child_environment,
                    runtime_root=runtime_root,
                    authority=authority,
                )
                recovery_summary = await RecoverySwitchDrill(
                    PostgresRecoveryOperations(
                        source_host=host,
                        database_manager=database,
                        ledger=ledger,
                        recovery_browser=recovery_browser,
                        recovery_root=recovery_root,
                        source_app_url=source_app_url,
                        postgres_admin_url=admin_url,
                        app_role=app_role,
                        authority=authority,
                        backup_key=backup_key,
                        journal_key=journal_key,
                        keyring=_audit_keyring(environment),
                    )
                ).run()
        code = "OK"
    except BaseException:
        code = "HOST_DIAGNOSTIC_FAILED"
    finally:
        if host is not None:
            try:
                await host.stop()
            except BaseException:
                code = "HOST_STOP_FAILED"
        try:
            from scripts.release_acceptance.security import (
                SecretScanner,
                load_secret_allowlist,
            )

            allowlist = await asyncio.to_thread(
                load_secret_allowlist,
                repository / "contracts" / "m8_secret_allowlist.json",
            )
            scanner = SecretScanner(allowlist=allowlist)
            findings = await asyncio.to_thread(
                scanner.scan_directory,
                runtime_root,
                scope="evidence",
            )
            support_findings = await asyncio.to_thread(
                scanner.scan_zip_archive,
                runtime_root / "support-bundle.zip",
                relative_path="support-bundle.zip",
            )
            findings = (*findings, *support_findings)
            evidence_log_security_passed = not findings
            if findings:
                code = "HOST_EVIDENCE_SECURITY_FAILED"
        except BaseException:
            evidence_log_security_passed = False
            code = "HOST_EVIDENCE_SECURITY_FAILED"
        try:
            cleanup = await asyncio.shield(ledger.cleanup())
        except BaseException:
            cleanup = CleanupSummary(
                residual_processes=1,
                residual_ports=1,
                residual_databases=1,
                residual_paths=1,
                retained_evidence=0,
            )
        if _has_residual(cleanup):
            code = "HOST_CLEANUP_FAILED"
    return DiagnosticResult(
        status="passed" if code == "OK" else "failed",
        code=code,
        host_setup_passed=host_passed,
        chromium=browser_summary,
        deepseek=live_summary,
        recovery=recovery_summary,
        evidence_log_security_passed=evidence_log_security_passed,
        cleanup=cleanup,
    )


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
        review_report: ReviewReport | None = None,
        review_report_path: Path | None = None,
        host_acceptance_runner: HostAcceptanceRunner = run_host_diagnostic,
    ) -> None:
        self._repository = repository.resolve()
        self._preflight = preflight
        self._commands = tuple(commands)
        self._executor = executor
        self._ledger = ledger
        self._git_probe = git_probe
        self._acceptance_run_id = acceptance_run_id or uuid.uuid4()
        self._review_report = review_report
        self._review_report_path = review_report_path
        self._host_acceptance_runner = host_acceptance_runner
        self._cancel_event = asyncio.Event()
        self._installed_signals: list[signal.Signals] = []

    @classmethod
    def default(cls, repository: Path) -> ReleaseRunner:
        repository = repository.resolve()
        run_id = uuid.uuid4()
        ledger = OwnershipLedger(repository=repository, acceptance_run_id=run_id)
        review_value = os.environ.get("M8_REVIEW_REPORT", "").strip()
        return cls(
            repository=repository,
            preflight=Preflight(repository=repository),
            commands=COMMANDS,
            executor=AsyncCommandExecutor(repository=repository, ledger=ledger),
            ledger=ledger,
            git_probe=SubprocessRunnerGitProbe(),
            acceptance_run_id=run_id,
            review_report_path=Path(review_value) if review_value else None,
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
        digest = manifest_digest(self._commands)
        review = self._review_report
        if review is None and self._review_report_path is not None:
            try:
                from scripts.create_m8_review_report import load_review_report

                review = await asyncio.to_thread(
                    load_review_report,
                    self._repository,
                    self._review_report_path,
                )
            except BaseException as exc:
                code = str(exc)
                if code not in {
                    "REVIEW_BINDING_MISMATCH",
                    "REVIEW_FINDINGS_PRESENT",
                }:
                    code = "REVIEW_REPORT_INVALID"
                return PreflightFailure(code=code)
        if review is not None:
            if review.candidate_commit != preflight_result.git_commit or review.stage_manifest_digest != digest:
                return PreflightFailure(code="REVIEW_BINDING_MISMATCH")
            if review.review_base_commit != M8_REVIEW_BASE_COMMIT or review.review_range != f"{M8_REVIEW_BASE_COMMIT}..{preflight_result.git_commit}":
                return PreflightFailure(code="REVIEW_RANGE_MISMATCH")
            if review.verdict != "passed" or review.critical or review.important or review.minor:
                return PreflightFailure(code="REVIEW_FINDINGS_PRESENT")
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
            host_result: DiagnosticResult | PreflightFailure | None = None
            for command in self._commands:
                if failed:
                    break
                if command.execution == "cleanup":
                    continue
                started_at = datetime.now(UTC)
                if self._cancel_event.is_set():
                    outcome = _failed_outcome(command.stage)
                elif command.execution == "host":
                    if host_result is None:
                        try:
                            host_result = await self._host_acceptance_runner(
                                repository=self._repository,
                                stages=(
                                    StageId.HOST_SETUP,
                                    StageId.CHROMIUM,
                                    StageId.DEEPSEEK,
                                    StageId.RECOVERY,
                                ),
                                acceptance_run_id=self._acceptance_run_id,
                            )
                        except BaseException:
                            host_result = None
                    outcome = _host_outcome(command, host_result)
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
                failed = outcome.status == "failed" or (command.require_zero_skips and outcome.skipped > 0) or self._cancel_event.is_set()
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
            cleanup_commands = tuple(command for command in self._commands if command.execution == "cleanup")
            if not cleanup_commands:
                cleanup_commands = (
                    CommandSpec(
                        command_id="cleanup.owned_resources",
                        stage=StageId.CLEANUP,
                        argv=("internal", "owned-resources"),
                        cwd="root",
                        timeout_seconds=1,
                        allowed_environment=frozenset(),
                        summary_parser="exit_code",
                        execution="cleanup",
                    ),
                )
            host_cleanup = (
                host_result.cleanup
                if isinstance(host_result, DiagnosticResult)
                else CleanupSummary(
                    residual_processes=0,
                    residual_ports=0,
                    residual_databases=0,
                    residual_paths=0,
                    retained_evidence=0,
                )
            )
            combined_cleanup = CleanupSummary(
                residual_processes=cleanup.residual_processes + host_cleanup.residual_processes,
                residual_ports=cleanup.residual_ports + host_cleanup.residual_ports,
                residual_databases=cleanup.residual_databases + host_cleanup.residual_databases,
                residual_paths=cleanup.residual_paths + host_cleanup.residual_paths,
                retained_evidence=cleanup.retained_evidence + host_cleanup.retained_evidence,
            )
            cleanup_failed = _has_residual(combined_cleanup) or (isinstance(host_result, DiagnosticResult) and not host_result.evidence_log_security_passed)
            for command in cleanup_commands:
                stages.append(
                    StageEvidence(
                        stage=StageId.CLEANUP,
                        command_id=command.command_id,
                        status="failed" if cleanup_failed else "passed",
                        started_at=cleanup_started,
                        finished_at=cleanup_finished,
                        duration_ms=int((cleanup_finished - cleanup_started).total_seconds() * 1000),
                        passed=0 if cleanup_failed else 1,
                        failed=1 if cleanup_failed else 0,
                        skipped=0,
                        summary=combined_cleanup,
                    )
                )
            self._remove_signals()
        try:
            final_commit, final_clean = await asyncio.to_thread(self._git_probe.exact_commit_and_clean, self._repository)
        except BaseException:
            final_commit, final_clean = "", False
        failed = failed or self._cancel_event.is_set() or cleanup_failed or not final_clean or final_commit != preflight_result.git_commit
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
            candidate = ReleaseEvidence.candidate(
                acceptance_run_id=self._acceptance_run_id,
                git_commit=preflight_result.git_commit,
                stage_manifest_digest=digest,
                public_config_digest=preflight_result.config_digest,
                toolchain_digest=preflight_result.toolchain_digest,
                stages=tuple(stages),
            )
            evidence = ReleaseEvidence.final(candidate=candidate, review=review) if review is not None else candidate
        await asyncio.to_thread(writer.write, evidence)
        return evidence


def _host_outcome(
    command: CommandSpec,
    result: DiagnosticResult | PreflightFailure | None,
) -> CommandOutcome:
    if not isinstance(result, DiagnosticResult) or result.status != "passed":
        return _failed_outcome(command.stage)
    if command.stage is StageId.HOST_SETUP:
        summary = TestSummary(collected=1, passed=1, failed=0, skipped=0)
        return CommandOutcome(
            status="passed",
            passed=1,
            failed=0,
            skipped=0,
            summary=summary,
        )
    if command.stage is StageId.CHROMIUM and result.chromium is not None:
        return CommandOutcome(
            status="passed",
            passed=result.chromium.passed,
            failed=result.chromium.failed,
            skipped=result.chromium.skipped,
            summary=result.chromium,
        )
    if command.stage is StageId.DEEPSEEK and result.deepseek is not None:
        passed = int(result.deepseek.outcome == "completed")
        return CommandOutcome(
            status="passed" if passed else "failed",
            passed=passed,
            failed=1 - passed,
            skipped=0,
            summary=result.deepseek,
        )
    if command.stage is StageId.RECOVERY and result.recovery is not None:
        passed = int(result.recovery.rpo_outcome == "archive_point_confirmed")
        return CommandOutcome(
            status="passed" if passed else "failed",
            passed=passed,
            failed=1 - passed,
            skipped=0,
            summary=result.recovery,
        )
    return _failed_outcome(command.stage)
