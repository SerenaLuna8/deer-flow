from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import Field

from scripts.release_acceptance.models import StrictModel, TestSummary


class M8BrowserResult(StrictModel):
    schema_version: int = Field(default=1, ge=1, le=1)
    boundaries_passed: int = Field(ge=0)
    failures: int = Field(ge=0)
    contexts: int = Field(ge=0)
    projects: int = Field(ge=0)
    private_denials: int = Field(ge=0)


class BrowserCommandRunner(Protocol):
    async def run(
        self,
        command_id: str,
        argv: tuple[str, ...],
        environment: dict[str, str],
        *,
        timeout_seconds: int,
    ) -> None: ...


class ChromiumJourneyRunner:
    def __init__(
        self,
        *,
        command_runner: BrowserCommandRunner,
        environment: Mapping[str, str],
        runtime_root: Path,
    ) -> None:
        self._command_runner = command_runner
        self._environment = dict(environment)
        self._runtime_root = runtime_root.resolve()

    async def run(self) -> TestSummary:
        output = self._runtime_root / "playwright"
        result_path = self._runtime_root / "browser-result.json"
        await asyncio.to_thread(output.mkdir, parents=True, mode=0o700, exist_ok=False)
        environment = {name: value for name, value in self._environment.items() if name in {"CI", "HOME", "LANG", "LC_ALL", "PATH", "TMPDIR", "TZ"}}
        environment.update(
            {
                "M8_BROWSER_OUTPUT_ROOT": str(self._runtime_root),
                "M8_BROWSER_RESULT_PATH": str(result_path),
                "M8_PLAYWRIGHT_OUTPUT_DIR": str(output),
            }
        )
        try:
            await self._command_runner.run(
                "chromium.journey",
                ("pnpm", "--dir", "frontend", "test:e2e:m8"),
                environment,
                timeout_seconds=900,
            )
            result = await asyncio.to_thread(load_browser_result, result_path)
            if result.failures != 0 or result.boundaries_passed < 1 or result.contexts < 2 or result.projects < 2 or result.private_denials < 1:
                raise RuntimeError("M8_BROWSER_RESULT_FAILED")
            return TestSummary(
                collected=result.boundaries_passed,
                passed=result.boundaries_passed,
                failed=0,
                skipped=0,
            )
        finally:
            if await asyncio.to_thread(output.exists):
                await asyncio.to_thread(shutil.rmtree, output)
            try:
                await asyncio.to_thread(os.unlink, result_path)
            except FileNotFoundError:
                pass


class HostReadiness:
    def __init__(self, *, base_url: str = "http://127.0.0.1:2026", attempts: int = 180, interval_seconds: float = 1.0) -> None:
        self._base_url = base_url
        self._attempts = attempts
        self._interval_seconds = interval_seconds

    async def wait_ready(self, *, scheduler_enabled: bool) -> None:
        del scheduler_enabled  # Process-role readiness is checked after the admin session exists.
        async with httpx.AsyncClient(
            base_url=self._base_url,
            timeout=3.0,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            for _attempt in range(self._attempts):
                try:
                    health = await client.get("/health")
                    projects = await client.get("/api/projects")
                    if health.status_code == 200 and projects.status_code == 401:
                        payload = health.json()
                        if payload == {"status": "healthy", "service": "deer-flow-gateway"}:
                            return
                except (httpx.HTTPError, ValueError, TypeError):
                    pass
                await asyncio.sleep(self._interval_seconds)
        raise RuntimeError("HOST_READINESS_FAILED")


def load_browser_result(path: Path) -> M8BrowserResult:
    try:
        data = path.read_bytes()
    except OSError:
        raise RuntimeError("M8_BROWSER_RESULT_MISSING") from None
    if len(data) > 16 * 1024:
        raise RuntimeError("M8_BROWSER_RESULT_INVALID")
    try:
        return M8BrowserResult.model_validate_json(data)
    except ValueError:
        raise RuntimeError("M8_BROWSER_RESULT_INVALID") from None
