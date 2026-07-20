from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field, model_validator

from scripts.release_acceptance.contracts import canonical_digest
from scripts.release_acceptance.models import (
    SecuritySummary,
    StageId,
    StageSummary,
    StrictModel,
    TestSummary,
)

_COMMAND_ID = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_ENVIRONMENT_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_SAFE_BASE_ENVIRONMENT = frozenset({"HOME", "LANG", "LC_ALL", "PATH", "TMPDIR", "TZ"})
_OUTPUT_LIMIT = 128 * 1024
_PYTEST_COUNT = re.compile(r"(?P<count>\d+)\s+(?P<kind>passed|failed|skipped|xfailed|xpassed|error|errors)\b")
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_VITEST_TESTS = re.compile(
    r"Tests\s+(?:(?P<failed>\d+)\s+failed(?:\s*\|\s*)?)?"
    r"(?:(?P<passed>\d+)\s+passed(?:\s*\|\s*)?)?"
    r"(?:(?P<skipped>\d+)\s+skipped)?"
)


@dataclass(frozen=True, slots=True)
class CommandSpec:
    command_id: str
    stage: StageId
    argv: tuple[str, ...]
    cwd: Literal["root", "backend", "frontend"]
    timeout_seconds: int
    allowed_environment: frozenset[str]
    summary_parser: Literal["pytest", "vitest", "security", "exit_code"] = "pytest"

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
        argv=("uv", "run", "pytest", "tests/test_m8_acceptance_contract.py", "tests/test_m8_evidence.py", "-q"),
        cwd="backend",
        timeout_seconds=600,
        allowed_environment=frozenset(),
    ),
    CommandSpec(
        command_id="postgres.m1_m8",
        stage=StageId.POSTGRES,
        argv=("make", "test-project-saas-postgres"),
        cwd="root",
        timeout_seconds=3600,
        allowed_environment=frozenset({"POSTGRES_TEST_URL"}),
    ),
    CommandSpec(
        command_id="backend.full",
        stage=StageId.BACKEND,
        argv=("uv", "run", "pytest", "-q"),
        cwd="backend",
        timeout_seconds=3600,
        allowed_environment=frozenset(),
    ),
    CommandSpec(
        command_id="frontend.unit",
        stage=StageId.FRONTEND,
        argv=("pnpm", "test"),
        cwd="frontend",
        timeout_seconds=1800,
        allowed_environment=frozenset({"CI"}),
        summary_parser="vitest",
    ),
    CommandSpec(
        command_id="security.full",
        stage=StageId.SECURITY,
        argv=("uv", "run", "python", "-m", "scripts.release_acceptance.security", "--scope", "tracked-tree", "--scope", "git-history"),
        cwd="backend",
        timeout_seconds=1800,
        allowed_environment=frozenset(),
        summary_parser="security",
    ),
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
        return {name: value for name, value in self._env.items() if name in permitted}

    @staticmethod
    async def _bounded_read(stream: asyncio.StreamReader) -> bytes:
        retained = bytearray()
        while block := await stream.read(64 * 1024):
            retained.extend(block)
            if len(retained) > _OUTPUT_LIMIT:
                del retained[: len(retained) - _OUTPUT_LIMIT]
        return bytes(retained)

    @staticmethod
    def _test_summary(output: bytes, *, returncode: int) -> CommandOutcome:
        text = output.decode("utf-8", errors="replace")
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
        if returncode and counts["failed"] == 0:
            counts["failed"] = 1
        if not returncode and counts["passed"] == counts["failed"] == counts["skipped"] == 0:
            counts["failed"] = 1
        summary = TestSummary(
            collected=sum(counts.values()),
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
        if command.summary_parser == "vitest":
            return self._vitest_summary(output, returncode=returncode)
        if command.summary_parser == "exit_code":
            return self._exit_code_summary(returncode=returncode)
        return self._test_summary(output, returncode=returncode)

    @staticmethod
    def _security_summary(output: bytes, *, returncode: int) -> CommandOutcome:
        scanned = 0
        findings = 1 if returncode else 0
        try:
            payload = json.loads(output.decode("utf-8"))
            scanned = sum(int(item.get("scanned", 0)) for item in payload.get("results", []))
            findings = int(payload["effective_findings"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeError):
            if returncode == 0:
                findings = 1
        summary = SecuritySummary(scanned=scanned, effective_findings=findings)
        return CommandOutcome(
            status="passed" if returncode == 0 and findings == 0 else "failed",
            passed=1 if returncode == 0 and findings == 0 else 0,
            failed=0 if returncode == 0 and findings == 0 else 1,
            skipped=0,
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
        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
        output = stdout + b"\n" + stderr
        returncode = process.returncode if wait_task in done and not cancel_event.is_set() else 1
        return self._parse_summary(command, output, returncode=returncode)
