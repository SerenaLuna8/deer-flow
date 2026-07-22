from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field, model_validator

from scripts.release_acceptance.contracts import (
    canonical_digest,
    discover_scoped_surface,
    load_isolation_matrix,
)
from scripts.release_acceptance.models import (
    M8_REVIEW_BASE_COMMIT,
    MatrixSummary,
    SecuritySummary,
    StageId,
    StageSummary,
    StrictModel,
    TestSummary,
)
from scripts.release_acceptance.security import SecretScanner

_COMMAND_ID = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_ENVIRONMENT_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_SAFE_BASE_ENVIRONMENT = frozenset({"HOME", "LANG", "LC_ALL", "PATH", "PWD", "TMPDIR", "TZ"})
_DETERMINISTIC_MODEL_KEY = "m8-deterministic-placeholder"
_DETERMINISTIC_DATABASE_URL = "postgresql://m8-deterministic@127.0.0.1:5432/m8-deterministic"
_BACKEND_TEST_ENVIRONMENT = frozenset(
    {
        "AUTH_JWT_SECRET",
        "CI",
        "DATABASE_URL",
        "DEEPSEEK_API_KEY",
        "DEER_FLOW_AUDIT_ACTIVE_KEY_ID",
        "DEER_FLOW_AUDIT_KEYRING_JSON",
        "DEER_FLOW_CONFIG_PATH",
        "DEER_FLOW_HOME",
    }
)
_OUTPUT_LIMIT = 128 * 1024
_PYTEST_COUNT = re.compile(r"(?P<count>\d+)\s+(?P<kind>passed|failed|skipped|xfailed|xpassed|error|errors)\b")
_RELEASE_STATS = re.compile(
    r"M1-M[78] release stats: collected=(?P<collected>\d+) "
    r"passed=(?P<passed>\d+) failed=(?P<failed>\d+) skipped=(?P<skipped>\d+)"
)
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_VITEST_TESTS = re.compile(
    r"^[ \t]*Tests[ \t]+(?=\d+[ \t]+(?:failed|passed|skipped)\b)"
    r"(?:(?P<failed>\d+)[ \t]+failed(?:[ \t]*\|[ \t]*)?)?"
    r"(?:(?P<passed>\d+)[ \t]+passed(?:[ \t]*\|[ \t]*)?)?"
    r"(?:(?P<skipped>\d+)[ \t]+skipped)?[ \t]*$",
    re.MULTILINE,
)
_PLAYWRIGHT_TESTS = re.compile(
    r"^[ \t]*(?P<count>\d+)[ \t]+(?P<kind>passed|failed|skipped|flaky)"
    r"(?:[ \t]+\([^\r\n]+\))?[ \t]*$",
    re.MULTILINE,
)
SUPPORT_BUNDLE_OUTPUT_TOKEN = "{runtime_root}/support-bundle.zip"
SUPPORT_BUNDLE_ARGV = (
    "uv",
    "run",
    "--directory",
    "backend",
    "python",
    "../scripts/support_bundle.py",
    "--include-doctor",
    "--out",
    SUPPORT_BUNDLE_OUTPUT_TOKEN,
)


@dataclass(frozen=True, slots=True)
class CommandSpec:
    command_id: str
    stage: StageId
    argv: tuple[str, ...]
    cwd: Literal["root", "backend", "frontend"]
    timeout_seconds: int
    allowed_environment: frozenset[str]
    summary_parser: Literal["pytest", "vitest", "playwright", "security", "matrix", "exit_code"] = "pytest"
    execution: Literal["subprocess", "host", "cleanup"] = "subprocess"
    fixed_environment: tuple[tuple[str, str], ...] = ()
    removed_environment: frozenset[str] = frozenset()
    require_zero_skips: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "stage", StageId(self.stage))
        if _COMMAND_ID.fullmatch(self.command_id) is None:
            raise ValueError("COMMAND_ID_INVALID")
        if not self.argv or any(not item or "\0" in item for item in self.argv):
            raise ValueError("COMMAND_ARGV_INVALID")
        if self.timeout_seconds < 1 or self.timeout_seconds > 7200:
            raise ValueError("COMMAND_TIMEOUT_INVALID")
        if any(_ENVIRONMENT_NAME.fullmatch(name) is None for name in self.allowed_environment):
            raise ValueError("COMMAND_ENVIRONMENT_INVALID")
        fixed_names = tuple(name for name, _value in self.fixed_environment)
        if (
            len(fixed_names) != len(set(fixed_names))
            or any(_ENVIRONMENT_NAME.fullmatch(name) is None for name in fixed_names)
            or any(not value or "\0" in value for _name, value in self.fixed_environment)
            or any(_ENVIRONMENT_NAME.fullmatch(name) is None for name in self.removed_environment)
            or set(fixed_names) & self.removed_environment
            or not set(fixed_names).issubset(self.allowed_environment)
        ):
            raise ValueError("COMMAND_ENVIRONMENT_INVALID")


class CommandOutcome(StrictModel):
    status: Literal["passed", "failed"]
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    skipped: int = Field(ge=0)
    summary: StageSummary

    @model_validator(mode="after")
    def validate_counts(self) -> CommandOutcome:
        if self.status == "passed" and self.failed:
            raise ValueError("passed command cannot contain failures")
        if self.status == "failed" and not self.failed:
            raise ValueError("failed command must contain a failure")
        return self


class ProcessLedger(Protocol):
    def process_start_identity(self, pid: int) -> str | None: ...

    def register_process(self, *, pid: int, pgid: int, start_identity: str): ...

    def stop_process(self, owned): ...


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec(
        command_id="contracts.schemas",
        stage=StageId.CONTRACTS,
        argv=("uv", "run", "pytest", "tests/test_m8_acceptance_contract.py", "tests/test_m8_evidence.py", "-q", "-k", "schema"),
        cwd="backend",
        timeout_seconds=600,
        allowed_environment=frozenset(),
    ),
    CommandSpec(
        command_id="contracts.matrix",
        stage=StageId.CONTRACTS,
        argv=("uv", "run", "pytest", "tests/test_m8_isolation_matrix_postgres.py", "-q"),
        cwd="backend",
        timeout_seconds=600,
        allowed_environment=frozenset(),
        summary_parser="matrix",
    ),
    CommandSpec(
        command_id="contracts.docs",
        stage=StageId.CONTRACTS,
        argv=("uv", "run", "pytest", "tests/test_m8_release_gate_postgres.py", "-q", "-k", "runbook"),
        cwd="backend",
        timeout_seconds=600,
        allowed_environment=frozenset(),
    ),
    CommandSpec(
        command_id="contracts.git_diff",
        stage=StageId.CONTRACTS,
        argv=("git", "diff", "--check"),
        cwd="root",
        timeout_seconds=300,
        allowed_environment=frozenset(),
        summary_parser="exit_code",
    ),
    CommandSpec(
        command_id="postgres.m1_m8",
        stage=StageId.POSTGRES,
        argv=("make", "test-project-saas-postgres"),
        cwd="root",
        timeout_seconds=3600,
        allowed_environment=frozenset({"DATABASE_URL", "DEEPSEEK_API_KEY", "POSTGRES_TEST_URL"}),
        fixed_environment=(
            ("DATABASE_URL", _DETERMINISTIC_DATABASE_URL),
            ("DEEPSEEK_API_KEY", _DETERMINISTIC_MODEL_KEY),
        ),
        require_zero_skips=True,
    ),
    CommandSpec(
        command_id="backend.full",
        stage=StageId.BACKEND,
        argv=("uv", "run", "pytest", "-q"),
        cwd="backend",
        timeout_seconds=3600,
        allowed_environment=_BACKEND_TEST_ENVIRONMENT,
        fixed_environment=(
            ("CI", "1"),
            ("DATABASE_URL", _DETERMINISTIC_DATABASE_URL),
            ("DEEPSEEK_API_KEY", _DETERMINISTIC_MODEL_KEY),
        ),
        removed_environment=frozenset({"DEER_FLOW_CONFIG_PATH", "POSTGRES_TEST_URL", "POSTGRES_ADMIN_URL"}),
    ),
    CommandSpec(
        command_id="backend.blocking_io",
        stage=StageId.BACKEND,
        argv=("uv", "run", "pytest", "tests/blocking_io", "-q"),
        cwd="backend",
        timeout_seconds=1800,
        allowed_environment=_BACKEND_TEST_ENVIRONMENT,
        fixed_environment=(
            ("CI", "1"),
            ("DATABASE_URL", _DETERMINISTIC_DATABASE_URL),
            ("DEEPSEEK_API_KEY", _DETERMINISTIC_MODEL_KEY),
        ),
        removed_environment=frozenset({"DEER_FLOW_CONFIG_PATH", "POSTGRES_TEST_URL", "POSTGRES_ADMIN_URL"}),
    ),
    CommandSpec(
        command_id="backend.format",
        stage=StageId.BACKEND,
        argv=("uvx", "ruff", "format", "--check", "."),
        cwd="backend",
        timeout_seconds=900,
        allowed_environment=frozenset(),
        summary_parser="exit_code",
    ),
    CommandSpec(
        command_id="backend.lint",
        stage=StageId.BACKEND,
        argv=("uvx", "ruff", "check", "."),
        cwd="backend",
        timeout_seconds=900,
        allowed_environment=frozenset(),
        summary_parser="exit_code",
    ),
    CommandSpec(
        command_id="frontend.install_frozen",
        stage=StageId.FRONTEND,
        argv=("pnpm", "install", "--frozen-lockfile"),
        cwd="frontend",
        timeout_seconds=1800,
        allowed_environment=frozenset({"CI"}),
        fixed_environment=(("CI", "1"),),
        summary_parser="exit_code",
    ),
    CommandSpec(
        command_id="frontend.unit",
        stage=StageId.FRONTEND,
        argv=("pnpm", "test"),
        cwd="frontend",
        timeout_seconds=1800,
        allowed_environment=frozenset({"CI"}),
        fixed_environment=(("CI", "1"),),
        summary_parser="vitest",
    ),
    CommandSpec(
        command_id="frontend.check",
        stage=StageId.FRONTEND,
        argv=("pnpm", "check"),
        cwd="frontend",
        timeout_seconds=1800,
        allowed_environment=frozenset({"CI"}),
        fixed_environment=(("CI", "1"),),
        summary_parser="exit_code",
    ),
    CommandSpec(
        command_id="frontend.e2e_deterministic",
        stage=StageId.FRONTEND,
        argv=("pnpm", "test:e2e:m8:deterministic"),
        cwd="frontend",
        timeout_seconds=1800,
        allowed_environment=frozenset({"CI", "SKIP_ENV_VALIDATION"}),
        fixed_environment=(("CI", "1"), ("SKIP_ENV_VALIDATION", "1")),
        summary_parser="playwright",
    ),
    CommandSpec(
        command_id="frontend.build_production",
        stage=StageId.FRONTEND,
        argv=("pnpm", "build:production"),
        cwd="frontend",
        timeout_seconds=1800,
        allowed_environment=frozenset({"CI", "SKIP_ENV_VALIDATION"}),
        fixed_environment=(("CI", "1"), ("SKIP_ENV_VALIDATION", "1")),
        summary_parser="exit_code",
    ),
    CommandSpec(
        command_id="frontend.build_static",
        stage=StageId.FRONTEND,
        argv=("pnpm", "build:static"),
        cwd="frontend",
        timeout_seconds=1800,
        allowed_environment=frozenset({"CI", "SKIP_ENV_VALIDATION"}),
        fixed_environment=(("CI", "1"), ("SKIP_ENV_VALIDATION", "1")),
        summary_parser="exit_code",
    ),
    CommandSpec(
        command_id="security.python_dependencies",
        stage=StageId.SECURITY,
        argv=("uv", "run", "python", "-m", "scripts.release_acceptance.security", "--scope", "dependencies-backend"),
        cwd="backend",
        timeout_seconds=1800,
        allowed_environment=frozenset(),
        summary_parser="security",
    ),
    CommandSpec(
        command_id="security.frontend_dependencies",
        stage=StageId.SECURITY,
        argv=("uv", "run", "python", "-m", "scripts.release_acceptance.security", "--scope", "dependencies-frontend"),
        cwd="backend",
        timeout_seconds=1800,
        allowed_environment=frozenset(),
        summary_parser="security",
    ),
    CommandSpec(
        command_id="security.tracked_tree",
        stage=StageId.SECURITY,
        argv=("uv", "run", "python", "-m", "scripts.release_acceptance.security", "--scope", "tracked-tree"),
        cwd="backend",
        timeout_seconds=1800,
        allowed_environment=frozenset(),
        summary_parser="security",
    ),
    CommandSpec(
        command_id="security.review_diff",
        stage=StageId.SECURITY,
        argv=("uv", "run", "python", "-m", "scripts.release_acceptance.security", "--scope", "review-diff", "--review-base", M8_REVIEW_BASE_COMMIT),
        cwd="backend",
        timeout_seconds=1800,
        allowed_environment=frozenset(),
        summary_parser="security",
    ),
    CommandSpec(
        command_id="security.git_history",
        stage=StageId.SECURITY,
        argv=("uv", "run", "python", "-m", "scripts.release_acceptance.security", "--scope", "git-history"),
        cwd="backend",
        timeout_seconds=1800,
        allowed_environment=frozenset(),
        summary_parser="security",
    ),
    CommandSpec(command_id="host.setup_db", stage=StageId.HOST_SETUP, argv=("make", "setup-db"), cwd="root", timeout_seconds=900, allowed_environment=frozenset(), summary_parser="exit_code", execution="host"),
    CommandSpec(command_id="host.check_db", stage=StageId.HOST_SETUP, argv=("make", "check-db"), cwd="root", timeout_seconds=300, allowed_environment=frozenset(), summary_parser="exit_code", execution="host"),
    CommandSpec(command_id="host.doctor", stage=StageId.HOST_SETUP, argv=("make", "doctor"), cwd="root", timeout_seconds=300, allowed_environment=frozenset(), summary_parser="exit_code", execution="host"),
    CommandSpec(command_id="host.make_help", stage=StageId.HOST_SETUP, argv=("make", "help"), cwd="root", timeout_seconds=300, allowed_environment=frozenset(), summary_parser="exit_code", execution="host"),
    CommandSpec(command_id="host.support_bundle", stage=StageId.HOST_SETUP, argv=SUPPORT_BUNDLE_ARGV, cwd="root", timeout_seconds=300, allowed_environment=frozenset(), summary_parser="exit_code", execution="host"),
    CommandSpec(command_id="host.make_start", stage=StageId.HOST_SETUP, argv=("make", "start"), cwd="root", timeout_seconds=900, allowed_environment=frozenset(), summary_parser="exit_code", execution="host"),
    CommandSpec(command_id="chromium.host_journey", stage=StageId.CHROMIUM, argv=("internal", "chromium-host-journey"), cwd="root", timeout_seconds=1800, allowed_environment=frozenset(), summary_parser="exit_code", execution="host"),
    CommandSpec(command_id="deepseek.live_journey", stage=StageId.DEEPSEEK, argv=("internal", "deepseek-live-journey"), cwd="root", timeout_seconds=1800, allowed_environment=frozenset(), summary_parser="exit_code", execution="host"),
    CommandSpec(
        command_id="cleanup.evidence_log_security", stage=StageId.CLEANUP, argv=("internal", "evidence-log-security"), cwd="root", timeout_seconds=600, allowed_environment=frozenset(), summary_parser="exit_code", execution="cleanup"
    ),
    CommandSpec(command_id="cleanup.residual_audit", stage=StageId.CLEANUP, argv=("internal", "residual-audit"), cwd="root", timeout_seconds=600, allowed_environment=frozenset(), summary_parser="exit_code", execution="cleanup"),
)

DIAGNOSTIC_STAGE_SEQUENCES: tuple[tuple[StageId, ...], ...] = (
    (StageId.HOST_SETUP,),
    (StageId.HOST_SETUP, StageId.CHROMIUM),
    (StageId.HOST_SETUP, StageId.CHROMIUM, StageId.DEEPSEEK),
)


def diagnostic_stages(values: tuple[str, ...]) -> tuple[StageId, ...]:
    stages = tuple(StageId(value) for value in values)
    if stages not in DIAGNOSTIC_STAGE_SEQUENCES:
        raise ValueError("DIAGNOSTIC_STAGE_SEQUENCE_INVALID")
    return stages


def manifest_digest(commands: tuple[CommandSpec, ...]) -> str:
    return canonical_digest(
        [
            {
                "command_id": command.command_id,
                "stage": command.stage.value,
                "argv": list(command.argv),
                "cwd": command.cwd,
                "timeout_seconds": command.timeout_seconds,
                "allowed_environment": sorted(command.allowed_environment),
                "summary_parser": command.summary_parser,
                "execution": command.execution,
                "fixed_environment": [list(item) for item in command.fixed_environment],
                "removed_environment": sorted(command.removed_environment),
                "require_zero_skips": command.require_zero_skips,
            }
            for command in commands
        ]
    )


class AsyncCommandExecutor:
    def __init__(
        self,
        *,
        repository: Path,
        env: dict[str, str] | None = None,
        ledger: ProcessLedger | None = None,
    ) -> None:
        self._repository = repository.resolve()
        self._env = dict(os.environ if env is None else env)
        self._ledger = ledger

    def _cwd(self, command: CommandSpec) -> Path:
        return {
            "root": self._repository,
            "backend": self._repository / "backend",
            "frontend": self._repository / "frontend",
        }[command.cwd]

    def _child_environment(self, command: CommandSpec) -> dict[str, str]:
        permitted = _SAFE_BASE_ENVIRONMENT | command.allowed_environment
        values = {name: value for name, value in self._env.items() if name in permitted and name not in command.removed_environment}
        values.update(command.fixed_environment)
        return values

    @staticmethod
    async def _bounded_read(stream: asyncio.StreamReader) -> tuple[bytes, bool]:
        retained = bytearray()
        overlap = b""
        safe = True
        while block := await stream.read(64 * 1024):
            safe = safe and AsyncCommandExecutor._runtime_log_is_safe(overlap + block)
            overlap = (overlap + block)[-512:]
            retained.extend(block)
            if len(retained) > _OUTPUT_LIMIT:
                del retained[: len(retained) - _OUTPUT_LIMIT]
        return bytes(retained), safe

    @staticmethod
    def _runtime_log_is_safe(output: bytes) -> bool:
        return not SecretScanner().scan_bytes(
            output,
            scope="runtime_logs",
            locator="bounded-command-output",
        )

    @staticmethod
    def _test_summary(output: bytes, *, returncode: int) -> CommandOutcome:
        text = output.decode("utf-8", errors="replace")
        release_matches = tuple(_RELEASE_STATS.finditer(text))
        if release_matches:
            release = release_matches[-1]
            counts = {name: int(release.group(name)) for name in ("passed", "failed", "skipped")}
            collected = int(release.group("collected"))
        else:
            counts = {"passed": 0, "failed": 0, "skipped": 0}
            for match in _PYTEST_COUNT.finditer(text):
                kind = match.group("kind")
                count = int(match.group("count"))
                if kind == "passed":
                    counts["passed"] += count
                elif kind == "skipped":
                    counts["skipped"] += count
                else:
                    counts["failed"] += count
            collected = sum(counts.values())
        if returncode and counts["failed"] == 0:
            counts["failed"] = 1
        if not returncode and counts["passed"] == counts["failed"] == counts["skipped"] == 0:
            counts["failed"] = 1
        summary = TestSummary(
            collected=collected,
            passed=counts["passed"],
            failed=counts["failed"],
            skipped=counts["skipped"],
        )
        return CommandOutcome(
            status="passed" if returncode == 0 and summary.failed == 0 else "failed",
            passed=summary.passed,
            failed=summary.failed,
            skipped=summary.skipped,
            summary=summary,
        )

    @staticmethod
    def _vitest_summary(output: bytes, *, returncode: int) -> CommandOutcome:
        text = _ANSI_ESCAPE.sub("", output.decode("utf-8", errors="replace"))
        matches = tuple(_VITEST_TESTS.finditer(text))
        match = matches[-1] if matches else None
        if match is None:
            counts = {"passed": 0, "failed": 1, "skipped": 0}
        else:
            counts = {name: int(match.group(name) or 0) for name in ("passed", "failed", "skipped")}
            if returncode and counts["failed"] == 0:
                counts["failed"] = 1
            if not returncode and counts["passed"] == counts["failed"] == counts["skipped"] == 0:
                counts["failed"] = 1
        summary = TestSummary(collected=sum(counts.values()), **counts)
        return CommandOutcome(
            status="passed" if returncode == 0 and summary.failed == 0 else "failed",
            passed=summary.passed,
            failed=summary.failed,
            skipped=summary.skipped,
            summary=summary,
        )

    @staticmethod
    def _exit_code_summary(*, returncode: int) -> CommandOutcome:
        summary = TestSummary(
            collected=1,
            passed=1 if returncode == 0 else 0,
            failed=0 if returncode == 0 else 1,
            skipped=0,
        )
        return CommandOutcome(
            status="passed" if returncode == 0 else "failed",
            passed=summary.passed,
            failed=summary.failed,
            skipped=0,
            summary=summary,
        )

    def _parse_summary(self, command: CommandSpec, output: bytes, *, returncode: int) -> CommandOutcome:
        if command.summary_parser == "security":
            return self._security_summary(output, returncode=returncode)
        if command.summary_parser == "matrix":
            return self._matrix_summary(output, returncode=returncode)
        if command.summary_parser == "vitest":
            return self._vitest_summary(output, returncode=returncode)
        if command.summary_parser == "playwright":
            return self._playwright_summary(output, returncode=returncode)
        if command.summary_parser == "exit_code":
            return self._exit_code_summary(returncode=returncode)
        return self._test_summary(output, returncode=returncode)

    @staticmethod
    def _security_summary(output: bytes, *, returncode: int) -> CommandOutcome:
        scanned = 0
        findings = 1 if returncode else 0
        database_timestamp = datetime.now(UTC)
        exclusion_ids: tuple[str, ...] = ()
        try:
            payload = json.loads(output.decode("utf-8"))
            scanned = sum(int(item.get("scanned", item.get("scanned_packages", 0))) for item in payload.get("results", []))
            findings = int(payload["effective_findings"])
            database_timestamp = datetime.fromisoformat(str(payload["database_timestamp"]).replace("Z", "+00:00"))
            exclusion_ids = tuple(sorted(set(payload["exclusion_ids"])))
        except (
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            UnicodeError,
        ):
            if returncode == 0:
                findings = 1
        summary = SecuritySummary(
            scanned=scanned,
            effective_findings=findings,
            database_timestamp=database_timestamp,
            exclusion_ids=exclusion_ids,
        )
        return CommandOutcome(
            status="passed" if returncode == 0 and findings == 0 else "failed",
            passed=1 if returncode == 0 and findings == 0 else 0,
            failed=0 if returncode == 0 and findings == 0 else 1,
            skipped=0,
            summary=summary,
        )

    @staticmethod
    def _playwright_summary(output: bytes, *, returncode: int) -> CommandOutcome:
        text = _ANSI_ESCAPE.sub("", output.decode("utf-8", errors="replace"))
        counts = {"passed": 0, "failed": 0, "skipped": 0}
        for match in _PLAYWRIGHT_TESTS.finditer(text):
            kind = match.group("kind")
            counts["failed" if kind == "flaky" else kind] += int(match.group("count"))
        if returncode and counts["failed"] == 0:
            counts["failed"] = 1
        if not returncode and sum(counts.values()) == 0:
            counts["failed"] = 1
        summary = TestSummary(collected=sum(counts.values()), **counts)
        return CommandOutcome(
            status="passed" if returncode == 0 and summary.failed == 0 else "failed",
            passed=summary.passed,
            failed=summary.failed,
            skipped=summary.skipped,
            summary=summary,
        )

    def _matrix_summary(self, output: bytes, *, returncode: int) -> CommandOutcome:
        test_outcome = self._test_summary(output, returncode=returncode)
        coverage_count = 0
        selector_count = 0
        uncovered_count = 1
        try:
            matrix = load_isolation_matrix(self._repository / "contracts" / "m8_isolation_matrix.json")
            discovered = discover_scoped_surface(self._repository)
            coverage_count = len(matrix.cases)
            selector_count = len({*matrix.pytest_selectors(), *matrix.playwright_selectors()})
            uncovered_count = sum(
                (
                    len(matrix.uncovered_dimensions()),
                    len(matrix.unmapped_surface(discovered)),
                    len(matrix.orphaned_surface_cases(discovered)),
                    int(matrix.surface_manifest.count != len(discovered)),
                    int(matrix.surface_manifest.sha256 != matrix.discovered_surface_digest(discovered)),
                    int(returncode != 0),
                )
            )
        except (OSError, TypeError, ValueError):
            uncovered_count = 1
        summary = MatrixSummary(
            coverage_count=coverage_count,
            uncovered_count=uncovered_count,
            selector_count=selector_count,
        )
        return CommandOutcome(
            status="passed" if uncovered_count == 0 else "failed",
            passed=test_outcome.passed,
            failed=max(test_outcome.failed, int(uncovered_count > 0)),
            skipped=test_outcome.skipped,
            summary=summary,
        )

    async def execute(self, command: CommandSpec, cancel_event: asyncio.Event) -> CommandOutcome:
        process = await asyncio.create_subprocess_exec(
            *command.argv,
            cwd=self._cwd(command),
            env=self._child_environment(command),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        if process.stdout is None or process.stderr is None:
            process.kill()
            await process.wait()
            return self._test_summary(b"", returncode=1)
        owned = None
        if self._ledger is not None:
            identity = await asyncio.to_thread(self._ledger.process_start_identity, process.pid)
            if identity is None:
                process.kill()
                await process.wait()
                return self._test_summary(b"", returncode=1)
            owned = self._ledger.register_process(pid=process.pid, pgid=process.pid, start_identity=identity)
        stdout_task = asyncio.create_task(self._bounded_read(process.stdout))
        stderr_task = asyncio.create_task(self._bounded_read(process.stderr))
        wait_task = asyncio.create_task(process.wait())
        cancel_task = asyncio.create_task(cancel_event.wait())
        done, _pending = await asyncio.wait(
            {wait_task, cancel_task},
            timeout=command.timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if wait_task not in done:
            if owned is not None and self._ledger is not None:
                await asyncio.to_thread(self._ledger.stop_process, owned)
            else:
                process.kill()
            try:
                await asyncio.wait_for(asyncio.shield(wait_task), timeout=10)
            except TimeoutError:
                wait_task.cancel()
                stdout_task.cancel()
                stderr_task.cancel()
                await asyncio.gather(wait_task, stdout_task, stderr_task, return_exceptions=True)
                cancel_task.cancel()
                await asyncio.gather(cancel_task, return_exceptions=True)
                return self._parse_summary(command, b"", returncode=1)
        cancel_task.cancel()
        await asyncio.gather(cancel_task, return_exceptions=True)
        (stdout, stdout_safe), (stderr, stderr_safe) = await asyncio.gather(stdout_task, stderr_task)
        output = stdout + b"\n" + stderr
        returncode = process.returncode if wait_task in done and not cancel_event.is_set() else 1
        if not stdout_safe or not stderr_safe:
            returncode = 1
            output = b""
        return self._parse_summary(command, output, returncode=returncode)
