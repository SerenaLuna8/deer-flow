from __future__ import annotations

import json
import os
import signal
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError

from scripts.release_acceptance.host_stack import (
    HostInventory,
    HostProcess,
    OwnedHostStack,
    ServiceProcessTree,
    SocketPortProbe,
)
from scripts.release_acceptance.live_probe import ChromiumJourneyRunner, HostReadiness, M8BrowserResult
from scripts.release_acceptance.ownership import CleanupAction, OwnedDatabase, OwnedProcess

_ADMIN_DATABASE_URL = "postgresql://admin:synthetic@127.0.0.1/postgres"
_APP_DATABASE_URL = "postgresql://deerflow_app:synthetic@127.0.0.1/deerflow"


class FakeDatabaseManager:
    def __init__(self) -> None:
        self.created: list[tuple[str, str, str]] = []

    async def create(self, *, name: str, owner: str, marker_digest: str) -> None:
        self.created.append((name, owner, marker_digest))


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

    class FakeSocket:
        def __init__(self, family: int, _kind: int) -> None:
            self.family = family

        def setsockopt(self, *_args) -> None:
            return None

        def bind(self, _address) -> None:
            attempts.append(self.family)
            if self.family == host_stack_module.socket.AF_INET6:
                raise OSError("synthetic IPv6 listener")

        def close(self) -> None:
            return None

    monkeypatch.setattr(host_stack_module.socket, "socket", FakeSocket)
    assert SocketPortProbe().is_free(2026) is False
    assert attempts == [
        host_stack_module.socket.AF_INET,
        host_stack_module.socket.AF_INET6,
    ]


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
    def __init__(self, *, failures: int = 0) -> None:
        self.failures = failures
        self.calls: list[tuple[str, tuple[str, ...]]] = []

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
        Path(environment["M8_BROWSER_RESULT_PATH"]).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "boundaries_passed": 8,
                    "failures": self.failures,
                    "contexts": 6,
                    "projects": 2,
                    "private_denials": 4,
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
