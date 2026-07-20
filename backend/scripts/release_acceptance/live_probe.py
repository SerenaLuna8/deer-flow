from __future__ import annotations

import asyncio
import os
import shutil
import sys
import uuid
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Literal, Protocol

import asyncpg
import httpx
from pydantic import Field

from scripts.release_acceptance.models import (
    LiveModelSummary,
    StrictModel,
    TestSummary,
)
from scripts.release_acceptance.preflight import LiveModelRef

_LIVE_EVENT_COUNTS_SQL = """
SELECT
  count(*) FILTER (WHERE category = 'stream') AS frame_count,
  count(*) FILTER (WHERE event_type = 'llm.tool.result') AS tool_count,
  count(*) FILTER (
    WHERE category = 'stream' AND event_type = 'stream.end'
  ) AS terminal_count,
  count(DISTINCT seq) FILTER (WHERE category = 'stream') AS cursor_count
FROM run_events
WHERE project_id = $1
  AND owner_user_id = $2
  AND thread_id = $3
  AND run_id = $4
"""

_LIVE_ARTIFACT_COUNTS_SQL = """
SELECT count(*) AS artifact_count, min(artifacts.id::text) AS artifact_id
FROM artifacts
JOIN files
  ON files.project_id = artifacts.project_id
 AND files.owner_user_id = artifacts.owner_user_id
 AND files.thread_id = artifacts.thread_id
 AND files.id = artifacts.file_id
WHERE artifacts.project_id = $1
  AND artifacts.owner_user_id = $2
  AND artifacts.thread_id = $3
  AND artifacts.run_id = $4
  AND artifacts.deleted_at IS NULL
  AND files.deleted_at IS NULL
  AND files.status = 'ready'
  AND files.created_by_run_id = $4
"""


class LiveProbeRequest(StrictModel):
    project_id: uuid.UUID
    owner_user_id: uuid.UUID
    thread_id: uuid.UUID
    run_id: uuid.UUID


class LiveProbeResult(StrictModel):
    frame_count: int = Field(gt=1)
    tool_call_count: int = Field(ge=1)
    terminal_count: int = Field(ge=1, le=1)
    cursor_count: int = Field(gt=1)
    artifact_id: uuid.UUID


class LiveProbeHandoffSuccess(LiveProbeResult):
    status: Literal["passed"] = "passed"


class LiveProbeHandoffFailure(StrictModel):
    status: Literal["failed"] = "failed"
    code: Literal["M8_LIVE_PROBE_FAILED"] = "M8_LIVE_PROBE_FAILED"


LiveProbeHandoffResult = LiveProbeHandoffSuccess | LiveProbeHandoffFailure


class LiveProbeConnection(Protocol):
    async def fetchrow(self, query: str, *args: object): ...

    async def close(self) -> None: ...


LiveConnect = Callable[[str], Awaitable[LiveProbeConnection]]


async def _connect_live_database(database_url: str) -> LiveProbeConnection:
    return await asyncpg.connect(database_url)


class PostgresLiveProbe:
    def __init__(self, *, connect: LiveConnect = _connect_live_database) -> None:
        self._connect = connect

    async def inspect(
        self,
        database_url: str,
        request: LiveProbeRequest,
    ) -> LiveProbeResult:
        normalized_url = database_url.replace(
            "postgresql+asyncpg://",
            "postgresql://",
            1,
        )
        connection = await self._connect(normalized_url)
        parameters = (
            request.project_id,
            str(request.owner_user_id),
            str(request.thread_id),
            str(request.run_id),
        )
        try:
            events = await connection.fetchrow(
                _LIVE_EVENT_COUNTS_SQL,
                *parameters,
            )
            artifacts = await connection.fetchrow(
                _LIVE_ARTIFACT_COUNTS_SQL,
                *parameters,
            )
            if events is None or artifacts is None:
                raise RuntimeError("M8_LIVE_PROBE_FAILED")
            artifact_count = int(artifacts["artifact_count"] or 0)
            artifact_id = artifacts["artifact_id"]
            if artifact_count < 1 or artifact_id is None:
                raise RuntimeError("M8_LIVE_PROBE_FAILED")
            try:
                return LiveProbeResult(
                    frame_count=int(events["frame_count"] or 0),
                    tool_call_count=int(events["tool_count"] or 0),
                    terminal_count=int(events["terminal_count"] or 0),
                    cursor_count=int(events["cursor_count"] or 0),
                    artifact_id=artifact_id,
                )
            except ValueError:
                raise RuntimeError("M8_LIVE_PROBE_FAILED") from None
        finally:
            await connection.close()


async def run_live_probe_handoff(
    raw_request: bytes,
    environment: Mapping[str, str],
    *,
    probe: PostgresLiveProbe | None = None,
) -> LiveProbeHandoffResult:
    try:
        if len(raw_request) > 4096:
            raise ValueError("request too large")
        database_url = environment.get("M8_LIVE_DATABASE_URL", "").strip()
        if not database_url:
            raise ValueError("database missing")
        request = LiveProbeRequest.model_validate_json(raw_request)
        result = await (probe or PostgresLiveProbe()).inspect(
            database_url,
            request,
        )
        return LiveProbeHandoffSuccess(**result.model_dump())
    except Exception:
        return LiveProbeHandoffFailure()


def main() -> int:
    raw_request = sys.stdin.buffer.read(4097)
    result = asyncio.run(run_live_probe_handoff(raw_request, os.environ))
    sys.stdout.write(result.model_dump_json() + "\n")
    return 0 if isinstance(result, LiveProbeHandoffSuccess) else 1


class M8LiveBrowserResult(StrictModel):
    summary: LiveModelSummary
    replay_passed: Literal[True]
    private_denials: int = Field(ge=4)


class M8BrowserResult(StrictModel):
    schema_version: int = Field(default=1, ge=1, le=1)
    boundaries_passed: int = Field(ge=0)
    failures: int = Field(ge=0)
    contexts: int = Field(ge=0)
    projects: int = Field(ge=0)
    private_denials: int = Field(ge=0)
    live_model: M8LiveBrowserResult | None = None


class ChromiumJourneyResult(StrictModel):
    tests: TestSummary
    live_model: LiveModelSummary | None = None

    @property
    def collected(self) -> int:
        return self.tests.collected

    @property
    def passed(self) -> int:
        return self.tests.passed

    @property
    def failed(self) -> int:
        return self.tests.failed

    @property
    def skipped(self) -> int:
        return self.tests.skipped


class BrowserCommandRunner(Protocol):
    async def run(
        self,
        command_id: str,
        argv: tuple[str, ...],
        environment: dict[str, str],
        *,
        timeout_seconds: int,
    ) -> None: ...


GatewayRestart = Callable[[], Awaitable[None]]


class GatewayControl(Protocol):
    port: int | None
    restart_count: int

    @property
    def token(self) -> str: ...

    async def start(self) -> None: ...

    async def close(self) -> None: ...


GatewayControlFactory = Callable[[GatewayRestart], GatewayControl]


class GatewayRestartControl:
    def __init__(
        self,
        *,
        restart_gateway: GatewayRestart,
    ) -> None:
        self._restart_gateway = restart_gateway
        self._server: asyncio.AbstractServer | None = None
        self._token = uuid.uuid4().hex
        self.port: int | None = None
        self.restart_count = 0

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        response = b"failed\n"
        try:
            command = await asyncio.wait_for(reader.readline(), timeout=10)
            expected = f"restart_gateway {self._token}\n".encode()
            if command == expected and self.restart_count == 0:
                self.restart_count = 1
                await self._restart_gateway()
                response = b"ok\n"
        except BaseException:
            response = b"failed\n"
        finally:
            writer.write(response)
            try:
                await writer.drain()
            except (ConnectionError, OSError):
                pass
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle,
            host="127.0.0.1",
            port=0,
        )
        sockets = self._server.sockets or ()
        if len(sockets) != 1:
            await self.close()
            raise RuntimeError("M8_GATEWAY_CONTROL_START_FAILED")
        self.port = int(sockets[0].getsockname()[1])

    @property
    def token(self) -> str:
        return self._token

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self.port = None


class ChromiumJourneyRunner:
    def __init__(
        self,
        *,
        command_runner: BrowserCommandRunner,
        environment: Mapping[str, str],
        runtime_root: Path,
        gateway_control_factory: GatewayControlFactory | None = None,
    ) -> None:
        self._command_runner = command_runner
        self._environment = dict(environment)
        self._runtime_root = runtime_root.resolve()
        self._gateway_control_factory = gateway_control_factory or (
            lambda callback: GatewayRestartControl(
                restart_gateway=callback,
            )
        )

    async def run(
        self,
        *,
        live_model: LiveModelRef | None = None,
        live_database_url: str | None = None,
        restart_gateway: GatewayRestart | None = None,
    ) -> ChromiumJourneyResult:
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
        control: GatewayControl | None = None
        try:
            if live_model is not None:
                if live_database_url is None or restart_gateway is None:
                    raise RuntimeError("M8_LIVE_BROWSER_CONFIG_INVALID")
                control = self._gateway_control_factory(restart_gateway)
                await control.start()
                if control.port is None:
                    raise RuntimeError("M8_GATEWAY_CONTROL_START_FAILED")
                environment.update(
                    {
                        "M8_DEEPSEEK_LIVE": "1",
                        "M8_LOGICAL_MODEL_NAME": live_model.logical_name,
                        "M8_LIVE_DATABASE_URL": live_database_url,
                        "M8_LIVE_PROBE_PYTHON": sys.executable,
                        "M8_LIVE_PROBE_CWD": str(self._runtime_root.parent / "backend"),
                        "M8_GATEWAY_CONTROL_PORT": str(control.port),
                        "M8_GATEWAY_CONTROL_TOKEN": control.token,
                    }
                )
            await self._command_runner.run(
                "chromium.journey",
                ("pnpm", "--dir", "frontend", "test:e2e:m8"),
                environment,
                timeout_seconds=900,
            )
            result = await asyncio.to_thread(load_browser_result, result_path)
            if result.failures != 0 or result.boundaries_passed < 1 or result.contexts < 2 or result.projects < 2 or result.private_denials < 1:
                raise RuntimeError("M8_BROWSER_RESULT_FAILED")
            tests = TestSummary(
                collected=result.boundaries_passed,
                passed=result.boundaries_passed,
                failed=0,
                skipped=0,
            )
            if live_model is None:
                if result.live_model is not None:
                    raise RuntimeError("M8_BROWSER_RESULT_FAILED")
                return ChromiumJourneyResult(tests=tests)
            if (
                result.live_model is None
                or control is None
                or control.restart_count != 1
                or result.live_model.summary.provider != live_model.provider
                or result.live_model.summary.logical_model_name != live_model.logical_name
                or result.live_model.summary.provider_model_id != live_model.provider_model_id
            ):
                raise RuntimeError("M8_BROWSER_RESULT_FAILED")
            return ChromiumJourneyResult(
                tests=tests,
                live_model=result.live_model.summary,
            )
        finally:
            if control is not None:
                await control.close()
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


if __name__ == "__main__":
    raise SystemExit(main())
