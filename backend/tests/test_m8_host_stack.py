from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from scripts.release_acceptance.commands import (
    SUPPORT_BUNDLE_ARGV,
    SUPPORT_BUNDLE_OUTPUT_TOKEN,
)
from scripts.release_acceptance.host_stack import (
    AsyncpgHostDatabaseManager,
    HostInventory,
    HostProcess,
    OwnedHostStack,
    ServiceProcessTree,
    SocketPortProbe,
    SubprocessHostCommandRunner,
)
from scripts.release_acceptance.live_probe import ChromiumJourneyRunner, HostReadiness, M8BrowserResult
from scripts.release_acceptance.ownership import CleanupAction, DatabaseIdentity, OwnedDatabase, OwnedProcess
from scripts.release_acceptance.preflight import AcceptanceModel

_ADMIN_DATABASE_URL = "postgresql://admin:synthetic@127.0.0.1/postgres"
_APP_DATABASE_URL = "postgresql://deerflow_app:synthetic@127.0.0.1/deerflow"


@pytest.mark.asyncio
async def test_host_database_creation_uses_pristine_template0() -> None:
    connection = AsyncMock()
    connection.fetchval.return_value = None
    connection.fetchrow.return_value = {
        "rolcanlogin": True,
        "rolsuper": False,
        "rolcreatedb": False,
        "rolcreaterole": False,
        "rolreplication": False,
        "rolbypassrls": False,
    }
    manager = AsyncpgHostDatabaseManager(_ADMIN_DATABASE_URL)
    manager._connect = AsyncMock(return_value=connection)  # type: ignore[method-assign]

    await manager.create(
        name="deerflow_test_111111",
        owner="deerflow_app",
        marker_digest="a" * 64,
    )

    statements = [call.args[0] for call in connection.execute.await_args_list]
    assert ('CREATE DATABASE "deerflow_test_111111" OWNER "deerflow_app" TEMPLATE template0 ENCODING \'UTF8\'') in statements


class FakeDatabaseManager:
    def __init__(self) -> None:
        self.created: list[tuple[str, str, str]] = []
        self.identities: dict[str, DatabaseIdentity] = {}

    async def create(self, *, name: str, owner: str, marker_digest: str) -> None:
        self.created.append((name, owner, marker_digest))
        self.identities[name] = DatabaseIdentity(owner=owner, marker_digest=marker_digest)

    async def identity(self, name: str) -> DatabaseIdentity | None:
        return self.identities.get(name)


class FakeLedger:
    def __init__(self) -> None:
        self.databases: list[OwnedDatabase] = []
        self.processes: list[OwnedProcess] = []
        self.signalled_groups: list[tuple[int, signal.Signals]] = []
        self.ports: list[int] = []

    def register_database(self, *, name: str, owner: str, marker_digest: str) -> OwnedDatabase:
        owned = OwnedDatabase(name=name, owner=owner, marker_digest=marker_digest)
        self.databases.append(owned)
        return owned

    def register_process(self, *, pid: int, pgid: int, start_identity: str) -> OwnedProcess:
        owned = OwnedProcess(pid=pid, pgid=pgid, start_identity=start_identity)
        self.processes.append(owned)
        return owned

    def stop_process(self, owned: OwnedProcess) -> CleanupAction:
        self.signalled_groups.append((owned.pgid, signal.SIGTERM))
        return CleanupAction(status="removed")

    def reserve_port(self, port: int) -> None:
        self.ports.append(port)


class FakePortProbe:
    def __init__(self, *, busy: set[int] | None = None) -> None:
        self.busy = busy or set()

    def is_free(self, port: int) -> bool:
        return port not in self.busy


class DelayedReleasePortProbe(FakePortProbe):
    def __init__(self) -> None:
        super().__init__()
        self.calls: dict[int, int] = {}

    def is_free(self, port: int) -> bool:
        self.calls[port] = self.calls.get(port, 0) + 1
        return not (port == 2026 and self.calls[port] == 2)


def _tree(role: str, pid: int, pgid: int = 51001) -> ServiceProcessTree:
    process = HostProcess(pid=pid, pgid=pgid, start_identity=f"{role}-start")
    return ServiceProcessTree(role=role, root=process, members=(process,))


class FakeProcessInspector:
    def __init__(self, *, replacement: bool = False, residual: bool = False) -> None:
        self.replacement = replacement
        self.residual = residual
        self.discover_calls: list[tuple[int, bool]] = []
        self.stopped: list[str] = []
        self.asserted: list[tuple[str, ...]] = []

    def discover(self, *, pgid: int, scheduler_enabled: bool) -> HostInventory:
        self.discover_calls.append((pgid, scheduler_enabled))
        gateway_identity = "gateway-replaced" if self.replacement else "gateway-start"
        gateway = ServiceProcessTree(
            role="gateway",
            root=HostProcess(pid=52001, pgid=pgid, start_identity=gateway_identity),
            members=(HostProcess(pid=52001, pgid=pgid, start_identity=gateway_identity),),
        )
        services = [
            gateway,
            _tree("worker", 52002, pgid),
            _tree("frontend", 52003, pgid),
            _tree("nginx", 52004, pgid),
        ]
        if scheduler_enabled:
            services.append(_tree("scheduler", 52005, pgid))
        return HostInventory(services=tuple(services))

    def stop_tree(self, tree: ServiceProcessTree) -> None:
        if self.replacement:
            raise RuntimeError("PROCESS_IDENTITY_MISMATCH")
        self.stopped.append(tree.role)

    def assert_absent(self, inventory: HostInventory) -> None:
        self.asserted.append(tuple(item.role for item in inventory.services))
        if self.residual:
            raise RuntimeError("HOST_PROCESS_RESIDUAL")


@dataclass
class FakeCommandRunner:
    fail_command: str | None = None
    markers: frozenset[str] = frozenset({"gateway", "worker", "scheduler", "frontend", "nginx"})

    def __post_init__(self) -> None:
        self.command_ids: list[str] = []
        self.argv: list[tuple[str, ...]] = []
        self.environments: list[dict[str, str]] = []

    async def run(self, command_id: str, argv: tuple[str, ...], environment: dict[str, str], *, timeout_seconds: int) -> None:
        self.command_ids.append(command_id)
        self.argv.append(argv)
        self.environments.append(environment)
        if command_id == self.fail_command:
            raise RuntimeError("raw command failure")

    async def start(self, command_id: str, argv: tuple[str, ...], environment: dict[str, str]) -> HostProcess:
        self.command_ids.append(command_id)
        self.argv.append(argv)
        self.environments.append(environment)
        if command_id == self.fail_command:
            raise RuntimeError("raw start failure")
        pid = 51002 if command_id == "host.gateway_restart" else 51001
        return HostProcess(pid=pid, pgid=pid, start_identity=f"owned-start-{pid}")

    async def reap(self, process: HostProcess) -> None:
        del process

    def startup_markers(self, process: HostProcess) -> frozenset[str]:
        del process
        return self.markers


class FakeReadiness:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def wait_ready(self, *, scheduler_enabled: bool) -> None:
        self.calls += 1
        if self.fail:
            raise RuntimeError("raw readiness body")


@pytest.mark.asyncio
async def test_startup_marker_drain_checks_bounded_line_suffix() -> None:
    lines = iter((b"x" * 700 + b"Frontend started on localhost:3000\n", b""))

    class Reader:
        async def readline(self) -> bytes:
            return next(lines)

    markers: set[str] = set()
    assert await SubprocessHostCommandRunner._drain(Reader(), markers) == {"frontend"}


@pytest.mark.asyncio
async def test_host_log_drain_marks_secret_output_without_retaining_it() -> None:
    lines = iter((b"provider returned sk-" + b"a" * 32 + b"\n", b""))

    class Reader:
        async def readline(self) -> bytes:
            return next(lines)

    markers: set[str] = set()
    failures: list[bool] = []
    assert await SubprocessHostCommandRunner._drain(Reader(), markers, failures) == set()
    assert failures == [True]


class SequencedMarkerCommandRunner(FakeCommandRunner):
    def __init__(self, marker_sequence: tuple[frozenset[str], ...]) -> None:
        super().__init__()
        self.marker_sequence = marker_sequence
        self.marker_calls = 0

    def startup_markers(self, process: HostProcess) -> frozenset[str]:
        del process
        index = min(self.marker_calls, len(self.marker_sequence) - 1)
        self.marker_calls += 1
        return self.marker_sequence[index]


def _host(
    tmp_path: Path,
    *,
    commands: FakeCommandRunner | None = None,
    environment: dict[str, str] | None = None,
    readiness: FakeReadiness | None = None,
    ports: FakePortProbe | None = None,
    processes: FakeProcessInspector | None = None,
    scheduler_enabled: bool = False,
) -> tuple[OwnedHostStack, FakeCommandRunner, FakeLedger, FakeDatabaseManager]:
    command_runner = commands or FakeCommandRunner()
    ledger = FakeLedger()
    database = FakeDatabaseManager()
    host_environment = {
        "POSTGRES_ADMIN_URL": _ADMIN_DATABASE_URL,
        "DEER_FLOW_CONFIG_PATH": str(tmp_path / "config.yaml"),
    }
    host_environment.update(environment or {})
    host = OwnedHostStack(
        repository=tmp_path,
        env=host_environment,
        acceptance_run_id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
        app_role="deerflow_app",
        ledger=ledger,
        database_manager=database,
        command_runner=command_runner,
        readiness=readiness or FakeReadiness(),
        port_probe=ports or FakePortProbe(),
        process_inspector=processes or FakeProcessInspector(),
        scheduler_enabled=scheduler_enabled,
        startup_marker_attempts=2,
        startup_marker_interval_seconds=0,
        shutdown_port_attempts=2,
        shutdown_port_interval_seconds=0,
    )
    return host, command_runner, ledger, database


@pytest.mark.asyncio
async def test_host_stack_invokes_only_certified_setup_start_path(tmp_path: Path) -> None:
    host, command_probe, ledger, database = _host(tmp_path)
    await host.start(_APP_DATABASE_URL)
    assert command_probe.command_ids == ["host.setup_db", "host.check_db", "host.make_start"]
    flattened = " ".join(item for argv in command_probe.argv for item in argv)
    assert "docker" not in flattened
    assert "helm" not in flattened
    assert command_probe.argv == [("make", "setup-db"), ("make", "check-db"), ("make", "start")]
    assert len(database.created) == len(ledger.databases) == 1
    assert database.created[0][0].startswith("deerflow_test_")
    assert ledger.processes == [OwnedProcess(pid=51001, pgid=51001, start_identity="owned-start-51001")]
    assert ledger.ports == [2026, 3000, 8001]
    assert all("deerflow_test_" in environment["DATABASE_URL"] for environment in command_probe.environments)


@pytest.mark.asyncio
async def test_release_checks_run_on_owned_database_and_write_support_bundle_to_runtime(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    host, command_probe, _ledger, _database = _host(
        tmp_path,
        environment={"DEER_FLOW_RUNTIME_ROOT": str(runtime_root)},
    )
    await host.prepare(_APP_DATABASE_URL)
    await host.run_release_checks()
    await host.launch()
    assert command_probe.command_ids == [
        "host.setup_db",
        "host.check_db",
        "host.doctor",
        "host.make_help",
        "host.support_bundle",
        "host.make_start",
    ]
    assert all("deerflow_test_" in environment["DATABASE_URL"] for environment in command_probe.environments)
    support_argv = command_probe.argv[4]
    assert support_argv == tuple(str(runtime_root / "support-bundle.zip") if item == SUPPORT_BUNDLE_OUTPUT_TOKEN else item for item in SUPPORT_BUNDLE_ARGV)


@pytest.mark.asyncio
async def test_host_stack_forwards_required_audit_keyring_only_to_owned_processes(tmp_path: Path) -> None:
    host, command_probe, _ledger, _database = _host(
        tmp_path,
        environment={
            "DEER_FLOW_AUDIT_ACTIVE_KEY_ID": "m8-audit-v1",
            "DEER_FLOW_AUDIT_KEYRING_JSON": "synthetic-keyring-json",
            "M8_UNSAFE_EXTRA": "must-not-pass",
        },
    )
    await host.start(_APP_DATABASE_URL)
    for environment in command_probe.environments:
        assert environment["DEER_FLOW_AUDIT_ACTIVE_KEY_ID"] == "m8-audit-v1"
        assert environment["DEER_FLOW_AUDIT_KEYRING_JSON"] == "synthetic-keyring-json"
        assert "M8_UNSAFE_EXTRA" not in environment


@pytest.mark.asyncio
async def test_host_stop_never_calls_broad_make_stop(tmp_path: Path) -> None:
    host, command_probe, ledger, _database = _host(tmp_path)
    await host.start(_APP_DATABASE_URL)
    await host.stop()
    assert ledger.signalled_groups == [(host.pgid, signal.SIGTERM)]
    assert ("make", "stop") not in command_probe.argv


@pytest.mark.asyncio
async def test_host_stop_waits_for_ports_to_become_reusable(tmp_path: Path) -> None:
    ports = DelayedReleasePortProbe()
    host, _commands, _ledger, _database = _host(tmp_path, ports=ports)
    await host.start(_APP_DATABASE_URL)
    await host.stop()
    assert ports.calls[2026] == 3


@pytest.mark.asyncio
async def test_partial_start_failure_stops_only_started_group(tmp_path: Path) -> None:
    host, _commands, ledger, _database = _host(tmp_path, readiness=FakeReadiness(fail=True))
    with pytest.raises(RuntimeError, match="HOST_READINESS_FAILED"):
        await host.start(_APP_DATABASE_URL)
    assert ledger.signalled_groups == [(51001, signal.SIGTERM)]


@pytest.mark.asyncio
async def test_missing_bounded_startup_marker_fails_and_stops_group(tmp_path: Path) -> None:
    commands = FakeCommandRunner(markers=frozenset({"gateway", "worker"}))
    host, _commands, ledger, _database = _host(tmp_path, commands=commands)
    with pytest.raises(RuntimeError, match="HOST_READINESS_FAILED"):
        await host.start(_APP_DATABASE_URL)
    assert ledger.signalled_groups == [(51001, signal.SIGTERM)]


@pytest.mark.asyncio
async def test_host_stack_waits_for_late_startup_markers(tmp_path: Path) -> None:
    commands = SequencedMarkerCommandRunner(
        (
            frozenset({"gateway", "worker"}),
            frozenset({"gateway", "worker", "frontend", "nginx"}),
        )
    )
    host, _commands, _ledger, _database = _host(tmp_path, commands=commands)
    await host.start(_APP_DATABASE_URL)
    assert commands.marker_calls == 2


@pytest.mark.asyncio
async def test_busy_port_fails_before_database_or_process_creation(tmp_path: Path) -> None:
    host, commands, ledger, database = _host(tmp_path, ports=FakePortProbe(busy={3000}))
    with pytest.raises(RuntimeError, match="HOST_PORT_BUSY"):
        await host.start(_APP_DATABASE_URL)
    assert database.created == []
    assert commands.command_ids == []
    assert ledger.ports == [2026]


@pytest.mark.asyncio
@pytest.mark.parametrize(("enabled", "expected"), [(False, False), (True, True)])
async def test_scheduler_inventory_follows_configuration(tmp_path: Path, enabled: bool, expected: bool) -> None:
    processes = FakeProcessInspector()
    host, _commands, _ledger, _database = _host(
        tmp_path,
        processes=processes,
        scheduler_enabled=enabled,
    )
    await host.start(_APP_DATABASE_URL)
    assert ("scheduler" in host.inventory.roles) is expected
    assert processes.discover_calls == [(51001, enabled)]


@pytest.mark.asyncio
async def test_gateway_restart_stops_only_verified_gateway_tree(tmp_path: Path) -> None:
    processes = FakeProcessInspector()
    host, commands, ledger, _database = _host(tmp_path, processes=processes)
    await host.start(_APP_DATABASE_URL)
    await host.restart_gateway()
    assert processes.stopped == ["gateway"]
    assert commands.command_ids[-1] == "host.gateway_restart"
    assert commands.argv[-1] == ("make", "gateway")
    assert ledger.processes[-1] == OwnedProcess(
        pid=51002,
        pgid=51002,
        start_identity="owned-start-51002",
    )


@pytest.mark.asyncio
async def test_gateway_restart_rejects_child_identity_substitution(tmp_path: Path) -> None:
    processes = FakeProcessInspector(replacement=True)
    host, commands, _ledger, _database = _host(tmp_path, processes=processes)
    await host.start(_APP_DATABASE_URL)
    with pytest.raises(RuntimeError, match="HOST_GATEWAY_RESTART_FAILED"):
        await host.restart_gateway()
    assert "host.gateway_restart" not in commands.command_ids


@pytest.mark.asyncio
async def test_stop_fails_when_verified_descendant_or_listener_remains(tmp_path: Path) -> None:
    processes = FakeProcessInspector(residual=True)
    host, _commands, _ledger, _database = _host(tmp_path, processes=processes)
    await host.start(_APP_DATABASE_URL)
    with pytest.raises(RuntimeError, match="HOST_STOP_RESIDUAL"):
        await host.stop()


def test_browser_result_is_closed_and_contains_counts_only() -> None:
    result = M8BrowserResult(boundaries_passed=8, failures=0, contexts=3, projects=2, private_denials=4)
    assert result.failures == 0
    with pytest.raises(ValidationError):
        M8BrowserResult.model_validate({**result.model_dump(), "email": "synthetic@example.invalid"})
    with pytest.raises(ValidationError):
        M8BrowserResult.model_validate({**result.model_dump(), "project_id": str(uuid.uuid4())})


def test_host_port_probe_detects_ipv6_only_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.release_acceptance.host_stack as host_stack_module

    attempts: list[int] = []
    reuse_values: list[int] = []
    monkeypatch.setattr(
        host_stack_module.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 1, stdout=""),
    )

    class FakeSocket:
        def __init__(self, family: int, _kind: int) -> None:
            self.family = family

        def setsockopt(self, _level: int, _option: int, value: int) -> None:
            reuse_values.append(value)

        def bind(self, _address) -> None:
            attempts.append(self.family)
            if self.family == host_stack_module.socket.AF_INET6:
                raise OSError("synthetic IPv6 listener")

        def listen(self, _backlog: int) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(host_stack_module.socket, "socket", FakeSocket)
    assert SocketPortProbe().is_free(2026) is False
    assert attempts == [
        host_stack_module.socket.AF_INET,
        host_stack_module.socket.AF_INET6,
    ]
    assert reuse_values == [1, 1]


def test_host_port_probe_requires_reusable_listen(monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.release_acceptance.host_stack as host_stack_module

    monkeypatch.setattr(
        host_stack_module.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 1, stdout=""),
    )

    class FakeSocket:
        def __init__(self, _family: int, _kind: int) -> None:
            pass

        def setsockopt(self, _level: int, _option: int, _value: int) -> None:
            return None

        def bind(self, _address) -> None:
            return None

        def listen(self, _backlog: int) -> None:
            raise OSError("synthetic active listener")

        def close(self) -> None:
            return None

    monkeypatch.setattr(host_stack_module.socket, "socket", FakeSocket)
    assert SocketPortProbe().is_free(2026) is False


def test_host_port_probe_rejects_lsof_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.release_acceptance.host_stack as host_stack_module

    class FakeSocket:
        def __init__(self, _family: int, _kind: int) -> None:
            pass

        def setsockopt(self, _level: int, _option: int, _value: int) -> None:
            return None

        def bind(self, _address) -> None:
            return None

        def listen(self, _backlog: int) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        host_stack_module.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, stdout="41001\n"),
    )
    monkeypatch.setattr(host_stack_module.socket, "socket", FakeSocket)
    assert SocketPortProbe().is_free(2026) is False


def test_acceptance_next_config_uses_invocation_owned_tsconfig() -> None:
    repository = Path(__file__).resolve().parents[2]
    frontend = repository / "frontend"
    token = "1" * 32
    environment = dict(os.environ)
    environment.update(
        {
            "DEER_FLOW_NEXT_DIST_DIR": f".m8-next-{token}/.next",
            "SKIP_ENV_VALIDATION": "1",
        }
    )
    completed = subprocess.run(
        (
            "node",
            "-e",
            "import('./next.config.js').then(({default:config})=>process.stdout.write(String(config.typescript?.tsconfigPath ?? 'missing')))",
        ),
        cwd=frontend,
        env=environment,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=10,
    )
    assert completed.stdout.endswith(f".m8-next-{token}/tsconfig.json")


@pytest.mark.asyncio
async def test_host_readiness_never_uses_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        def __init__(self, status_code: int, payload: dict[str, str] | None = None) -> None:
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    class Client:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def get(self, path: str):
            if path == "/health":
                return Response(200, {"status": "healthy", "service": "deer-flow-gateway"})
            return Response(401)

    monkeypatch.setattr("scripts.release_acceptance.live_probe.httpx.AsyncClient", Client)
    await HostReadiness(attempts=1, interval_seconds=0).wait_ready(scheduler_enabled=False)
    assert captured["trust_env"] is False


class FakeBrowserCommandRunner:
    def __init__(
        self,
        *,
        failures: int = 0,
        live: bool = False,
        request_restart=None,
    ) -> None:
        self.failures = failures
        self.live = live
        self.request_restart = request_restart
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.environments: list[dict[str, str]] = []

    async def run(
        self,
        command_id: str,
        argv: tuple[str, ...],
        environment: dict[str, str],
        *,
        timeout_seconds: int,
    ) -> None:
        del timeout_seconds
        self.calls.append((command_id, argv))
        self.environments.append(environment)
        live_model = None
        if self.live:
            if self.request_restart is not None:
                await self.request_restart()
            else:
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1",
                    int(environment["M8_GATEWAY_CONTROL_PORT"]),
                )
                writer.write((f"restart_gateway {environment['M8_GATEWAY_CONTROL_TOKEN']}\n").encode())
                await writer.drain()
                assert await reader.readline() == b"ok\n"
                writer.close()
                await writer.wait_closed()
            live_model = {
                "summary": {
                    "provider": "deepseek",
                    "logical_model_name": environment["M8_LOGICAL_MODEL_NAME"],
                    "provider_model_id": "deepseek-v4-pro",
                    "outcome": "completed",
                    "frame_count": 5,
                    "tool_call_count": 1,
                    "terminal_count": 1,
                    "cursor_count": 5,
                    "duration_ms": 1200,
                },
                "replay_passed": True,
                "private_denials": 4,
            }
        Path(environment["M8_BROWSER_RESULT_PATH"]).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "boundaries_passed": 8,
                    "failures": self.failures,
                    "contexts": 6,
                    "projects": 2,
                    "private_denials": 4,
                    "live_model": live_model,
                }
            ),
            encoding="utf-8",
        )


@pytest.mark.asyncio
async def test_chromium_runner_uses_fixed_command_and_deletes_raw_handoff(tmp_path: Path) -> None:
    command_runner = FakeBrowserCommandRunner()
    summary = await ChromiumJourneyRunner(
        command_runner=command_runner,
        environment={"PATH": "/synthetic/bin", "DEEPSEEK_API_KEY": "must-not-pass"},
        runtime_root=tmp_path,
    ).run()
    assert command_runner.calls == [("chromium.journey", ("pnpm", "--dir", "frontend", "test:e2e:m8"))]
    assert summary.passed == 8
    assert not (tmp_path / "playwright").exists()
    assert not (tmp_path / "browser-result.json").exists()


@pytest.mark.asyncio
async def test_chromium_runner_fails_closed_on_failed_result(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="M8_BROWSER_RESULT_FAILED"):
        await ChromiumJourneyRunner(
            command_runner=FakeBrowserCommandRunner(failures=1),
            environment={"PATH": "/synthetic/bin"},
            runtime_root=tmp_path,
        ).run()


@pytest.mark.asyncio
async def test_chromium_runner_live_mode_restarts_only_gateway_and_returns_closed_summary(
    tmp_path: Path,
) -> None:
    restarts = 0

    async def restart_gateway() -> None:
        nonlocal restarts
        restarts += 1

    class InMemoryGatewayControl:
        def __init__(self, callback) -> None:
            self.callback = callback
            self.port = 41000
            self.restart_count = 0
            self.token = "1" * 32

        async def start(self) -> None:
            return None

        async def request(self) -> None:
            self.restart_count += 1
            await self.callback()

        async def close(self) -> None:
            self.port = None

    control = InMemoryGatewayControl(restart_gateway)
    command_runner = FakeBrowserCommandRunner(
        live=True,
        request_restart=control.request,
    )

    result = await ChromiumJourneyRunner(
        command_runner=command_runner,
        environment={
            "PATH": "/synthetic/bin",
            "DEEPSEEK_API_KEY": "must-not-pass-to-playwright",
        },
        runtime_root=tmp_path,
        gateway_control_factory=lambda _callback: control,
    ).run(
        live_model=AcceptanceModel(
            logical_name="release-live",
            provider_model_id="deepseek-v4-pro",
            provider="deepseek",
        ),
        live_database_url="postgresql" + "://m8-app:secret@127.0.0.1/live",
        restart_gateway=restart_gateway,
    )

    assert result.passed == 8
    assert result.live_model is not None
    assert result.live_model.provider == "deepseek"
    assert result.live_model.logical_model_name == "release-live"
    assert result.live_model.tool_call_count == 1
    assert restarts == 1
    child_environment = command_runner.environments[0]
    assert child_environment["M8_DEEPSEEK_LIVE"] == "1"
    assert child_environment["M8_LIVE_DATABASE_URL"].endswith("/live")
    assert child_environment["M8_LIVE_PROBE_PYTHON"] == sys.executable
    assert "DEEPSEEK_API_KEY" not in child_environment
