from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import re
import signal
import socket
import subprocess
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlsplit, urlunsplit

import asyncpg

from scripts.release_acceptance.commands import (
    SUPPORT_BUNDLE_ARGV,
    SUPPORT_BUNDLE_OUTPUT_TOKEN,
)
from scripts.release_acceptance.live_probe import HostReadiness
from scripts.release_acceptance.models import HostCommandTiming
from scripts.release_acceptance.ownership import CleanupAction, DatabaseIdentity, OwnedDatabase, OwnedProcess, OwnershipLedger
from scripts.release_acceptance.security import SecretScanner

_TEST_DATABASE_NAME = re.compile(r"^deerflow_test_[a-z0-9]{6,64}$")
_RESTORE_DATABASE_NAME = re.compile(r"^deerflow_restore_[0-9]+_[0-9a-f]{32}$")
_ROLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_SAFE_HOST_ENV = frozenset(
    {
        "AUTH_JWT_SECRET",
        "CI",
        "DATABASE_URL",
        "DEEPSEEK_API_KEY",
        "DEER_FLOW_AUDIT_ACTIVE_KEY_ID",
        "DEER_FLOW_AUDIT_KEYRING_JSON",
        "DEER_FLOW_BACKUP_KEY",
        "DEER_FLOW_CONFIG_PATH",
        "DEER_FLOW_HOME",
        "DEER_FLOW_NEXT_DIST_DIR",
        "DEER_FLOW_RUNTIME_ROOT",
        "HOME",
        "LANG",
        "LC_ALL",
        "M8_LIVE_ACCEPTANCE",
        "M8_BROWSER_OUTPUT_ROOT",
        "M8_BROWSER_RESULT_PATH",
        "M8_PLAYWRIGHT_OUTPUT_DIR",
        "PATH",
        "POSTGRES_ADMIN_URL",
        "TMPDIR",
        "TZ",
    }
)
_STARTUP_MARKERS = {
    "Gateway": "gateway",
    "Worker": "worker",
    "Scheduler": "scheduler",
    "Frontend": "frontend",
    "Nginx": "nginx",
}
_HOST_PORTS = (2026, 3000, 8001)


@dataclass(frozen=True, slots=True)
class HostProcess:
    pid: int
    pgid: int
    start_identity: str


@dataclass(frozen=True, slots=True)
class ServiceProcessTree:
    role: str
    root: HostProcess
    members: tuple[HostProcess, ...]


@dataclass(frozen=True, slots=True)
class HostInventory:
    services: tuple[ServiceProcessTree, ...]

    @property
    def roles(self) -> frozenset[str]:
        return frozenset(item.role for item in self.services)

    def service(self, role: str) -> ServiceProcessTree:
        matches = tuple(item for item in self.services if item.role == role)
        if len(matches) != 1:
            raise RuntimeError("HOST_PROCESS_INVENTORY_INVALID")
        return matches[0]


class HostDatabaseManager(Protocol):
    async def create(self, *, name: str, owner: str, marker_digest: str) -> None: ...

    async def identity(self, name: str) -> DatabaseIdentity | None: ...


class HostCommandRunner(Protocol):
    async def run(self, command_id: str, argv: tuple[str, ...], environment: dict[str, str], *, timeout_seconds: int) -> None: ...

    async def start(self, command_id: str, argv: tuple[str, ...], environment: dict[str, str]) -> HostProcess: ...

    async def reap(self, process: HostProcess) -> None: ...

    def startup_markers(self, process: HostProcess) -> frozenset[str]: ...


class ReadinessProbe(Protocol):
    async def wait_ready(self, *, scheduler_enabled: bool) -> None: ...


class PortProbe(Protocol):
    def is_free(self, port: int) -> bool: ...


class ProcessInspector(Protocol):
    def discover(self, *, pgid: int, scheduler_enabled: bool) -> HostInventory: ...

    def stop_tree(self, tree: ServiceProcessTree) -> None: ...

    def assert_absent(self, inventory: HostInventory) -> None: ...


class SocketPortProbe:
    @staticmethod
    def _has_listener(port: int) -> bool:
        try:
            completed = subprocess.run(
                ("lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"),
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return True
        if completed.returncode == 0:
            return bool(completed.stdout.strip())
        return completed.returncode != 1

    def is_free(self, port: int) -> bool:
        if self._has_listener(port):
            return False
        for family, address in (
            (socket.AF_INET, ("0.0.0.0", port)),
            (socket.AF_INET6, ("::", port, 0, 0)),
        ):
            try:
                handle = socket.socket(family, socket.SOCK_STREAM)
            except OSError as exc:
                if exc.errno in {errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT}:
                    continue
                return False
            try:
                handle.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                handle.bind(address)
                handle.listen(1)
            except OSError:
                return False
            finally:
                handle.close()
        return True


@dataclass(frozen=True, slots=True)
class _ProcessRow:
    pid: int
    ppid: int
    pgid: int
    command: str


class SystemProcessInspector:
    _ROLE_FINGERPRINTS = {
        "gateway": ("uvicorn", "app.gateway.app:app"),
        "worker": ("python", "-m app.worker.app"),
        "scheduler": ("python", "-m app.scheduler.app"),
        "frontend": ("next",),
        "nginx": ("nginx",),
    }
    _ROLE_PORTS = {"gateway": 8001, "frontend": 3000, "nginx": 2026}

    def __init__(self) -> None:
        from scripts.release_acceptance.ownership import HostProcessProbe

        self._identity = HostProcessProbe()

    @staticmethod
    def _snapshot() -> dict[int, _ProcessRow]:
        try:
            completed = subprocess.run(
                ("ps", "-axo", "pid=,ppid=,pgid=,command="),
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            raise RuntimeError("HOST_PROCESS_INVENTORY_FAILED") from None
        rows: dict[int, _ProcessRow] = {}
        for line in completed.stdout.splitlines():
            values = line.strip().split(maxsplit=3)
            if len(values) != 4:
                continue
            try:
                pid, ppid, pgid = (int(value) for value in values[:3])
            except ValueError:
                continue
            rows[pid] = _ProcessRow(pid=pid, ppid=ppid, pgid=pgid, command=values[3])
        return rows

    @staticmethod
    def _listeners(port: int) -> frozenset[int]:
        try:
            completed = subprocess.run(
                ("lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"),
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            raise RuntimeError("HOST_PROCESS_INVENTORY_FAILED") from None
        if completed.returncode not in {0, 1}:
            raise RuntimeError("HOST_PROCESS_INVENTORY_FAILED")
        return frozenset(int(value) for value in completed.stdout.split() if value.isdigit())

    @staticmethod
    def _is_descendant(rows: dict[int, _ProcessRow], pid: int, ancestor: int) -> bool:
        visited: set[int] = set()
        while pid in rows and pid not in visited:
            if pid == ancestor:
                return True
            visited.add(pid)
            pid = rows[pid].ppid
        return False

    def _representative(self, rows: dict[int, _ProcessRow], *, role: str, pgid: int) -> int:
        fragments = self._ROLE_FINGERPRINTS[role]
        candidates = {row.pid for row in rows.values() if row.pgid == pgid and all(fragment in row.command for fragment in fragments)}
        port = self._ROLE_PORTS.get(role)
        if port is not None:
            listeners = self._listeners(port)
            listening_candidates = candidates & listeners
            if not listening_candidates:
                raise RuntimeError("HOST_PROCESS_INVENTORY_INVALID")
            candidates = listening_candidates
        if not candidates:
            raise RuntimeError("HOST_PROCESS_INVENTORY_INVALID")
        return min(candidates)

    def _service_root(
        self,
        rows: dict[int, _ProcessRow],
        *,
        representative: int,
        representatives: Mapping[str, int],
        pgid: int,
    ) -> int:
        root = representative
        while root in rows:
            parent = rows[root].ppid
            if parent not in rows or rows[parent].pgid != pgid:
                break
            represented = {role for role, pid in representatives.items() if self._is_descendant(rows, pid, parent)}
            if len(represented) != 1:
                break
            root = parent
        return root

    def _host_process(self, row: _ProcessRow) -> HostProcess:
        identity = self._identity.start_identity(row.pid)
        if identity is None:
            raise RuntimeError("HOST_PROCESS_IDENTITY_FAILED")
        return HostProcess(pid=row.pid, pgid=row.pgid, start_identity=identity)

    def discover(self, *, pgid: int, scheduler_enabled: bool) -> HostInventory:
        rows = self._snapshot()
        expected = ["gateway", "worker", "frontend", "nginx"]
        if scheduler_enabled:
            expected.append("scheduler")
        representatives = {role: self._representative(rows, role=role, pgid=pgid) for role in expected}
        scheduler_candidates = [row for row in rows.values() if row.pgid == pgid and all(fragment in row.command for fragment in self._ROLE_FINGERPRINTS["scheduler"])]
        if not scheduler_enabled and scheduler_candidates:
            raise RuntimeError("HOST_PROCESS_INVENTORY_INVALID")
        services: list[ServiceProcessTree] = []
        claimed: set[int] = set()
        for role in expected:
            root_pid = self._service_root(
                rows,
                representative=representatives[role],
                representatives=representatives,
                pgid=pgid,
            )
            member_rows = tuple(row for row in rows.values() if row.pgid == pgid and self._is_descendant(rows, row.pid, root_pid))
            member_pids = {row.pid for row in member_rows}
            if claimed & member_pids:
                raise RuntimeError("HOST_PROCESS_INVENTORY_INVALID")
            claimed.update(member_pids)
            members = tuple(self._host_process(row) for row in sorted(member_rows, key=lambda item: item.pid))
            root = next((item for item in members if item.pid == root_pid), None)
            if root is None:
                raise RuntimeError("HOST_PROCESS_INVENTORY_INVALID")
            services.append(ServiceProcessTree(role=role, root=root, members=members))
        return HostInventory(services=tuple(services))

    def _verify_tree(self, tree: ServiceProcessTree) -> None:
        rows = self._snapshot()
        for member in tree.members:
            row = rows.get(member.pid)
            identity = self._identity.start_identity(member.pid)
            if row is None or row.pgid != member.pgid or identity != member.start_identity:
                raise RuntimeError("PROCESS_IDENTITY_MISMATCH")
            if not self._is_descendant(rows, member.pid, tree.root.pid):
                raise RuntimeError("PROCESS_IDENTITY_MISMATCH")

    def stop_tree(self, tree: ServiceProcessTree) -> None:
        self._verify_tree(tree)
        pids = tuple(member.pid for member in reversed(tree.members))
        for signal_number, attempts, delay in ((signal.SIGTERM, 30, 0.1), (signal.SIGKILL, 20, 0.05)):
            for pid in pids:
                try:
                    os.kill(pid, signal_number)
                except ProcessLookupError:
                    pass
                except OSError:
                    raise RuntimeError("HOST_PROCESS_STOP_FAILED") from None
            for _attempt in range(attempts):
                if all(self._identity.start_identity(member.pid) is None for member in tree.members):
                    return
                time.sleep(delay)
        raise RuntimeError("HOST_PROCESS_STOP_FAILED")

    def assert_absent(self, inventory: HostInventory) -> None:
        if any(self._identity.start_identity(member.pid) is not None for service in inventory.services for member in service.members):
            raise RuntimeError("HOST_PROCESS_RESIDUAL")


def _quote_identifier(value: str) -> str:
    if _ROLE_NAME.fullmatch(value) is None:
        raise ValueError("POSTGRES_IDENTIFIER_INVALID")
    return '"' + value.replace('"', '""') + '"'


class AsyncpgHostDatabaseManager:
    def __init__(self, admin_url: str) -> None:
        self._admin_url = admin_url.replace("postgresql+asyncpg://", "postgresql://", 1)

    async def _connect(self):
        return await asyncpg.connect(self._admin_url, timeout=10)

    async def create(self, *, name: str, owner: str, marker_digest: str) -> None:
        if _TEST_DATABASE_NAME.fullmatch(name) is None or re.fullmatch(r"[0-9a-f]{64}", marker_digest) is None:
            raise RuntimeError("OWNED_DATABASE_IDENTITY_INVALID")
        database_identifier = _quote_identifier(name)
        owner_identifier = _quote_identifier(owner)
        connection = await self._connect()
        created = False
        try:
            if await connection.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name):
                raise RuntimeError("OWNED_DATABASE_ALREADY_EXISTS")
            role = await connection.fetchrow(
                "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname = $1",
                owner,
            )
            if not role or not role["rolcanlogin"] or any(role[field] for field in ("rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls")):
                raise RuntimeError("DATABASE_APP_ROLE_UNSAFE")
            await connection.execute(f"CREATE DATABASE {database_identifier} OWNER {owner_identifier} TEMPLATE template0 ENCODING 'UTF8'")
            created = True
            await connection.execute(f"COMMENT ON DATABASE {database_identifier} IS 'deerflow-m8:{marker_digest}'")
        except BaseException:
            if created:
                await connection.execute(f"DROP DATABASE IF EXISTS {database_identifier}")
            raise
        finally:
            await connection.close()

    async def identity(self, name: str) -> DatabaseIdentity | None:
        connection = await self._connect()
        try:
            row = await connection.fetchrow(
                """SELECT owner.rolname AS owner,
                          shobj_description(database.oid, 'pg_database') AS marker
                     FROM pg_database AS database
                     JOIN pg_roles AS owner ON owner.oid = database.datdba
                    WHERE database.datname = $1""",
                name,
            )
        finally:
            await connection.close()
        if not row or not isinstance(row["marker"], str) or not row["marker"].startswith("deerflow-m8:"):
            return None
        return DatabaseIdentity(owner=row["owner"], marker_digest=row["marker"].removeprefix("deerflow-m8:"))

    async def claim_verified_restore(
        self,
        *,
        name: str,
        marker_digest: str,
    ) -> DatabaseIdentity:
        if _RESTORE_DATABASE_NAME.fullmatch(name) is None or re.fullmatch(r"[0-9a-f]{64}", marker_digest) is None:
            raise RuntimeError("OWNED_DATABASE_IDENTITY_INVALID")
        database_identifier = _quote_identifier(name)
        connection = await self._connect()
        try:
            row = await connection.fetchrow(
                """SELECT owner.rolname AS owner,
                          shobj_description(database.oid, 'pg_database') AS marker
                     FROM pg_database AS database
                     JOIN pg_roles AS owner ON owner.oid = database.datdba
                    WHERE database.datname = $1""",
                name,
            )
            if not row or row["marker"] is not None:
                raise RuntimeError("RESTORE_DATABASE_CLAIM_REJECTED")
            await connection.execute(f"COMMENT ON DATABASE {database_identifier} IS 'deerflow-m8:{marker_digest}'")
            return DatabaseIdentity(owner=row["owner"], marker_digest=marker_digest)
        finally:
            await connection.close()

    async def grant_runtime_access(self, *, name: str, app_role: str) -> None:
        if (_TEST_DATABASE_NAME.fullmatch(name) is None and _RESTORE_DATABASE_NAME.fullmatch(name) is None) or _ROLE_NAME.fullmatch(app_role) is None:
            raise RuntimeError("RESTORE_DATABASE_GRANT_REJECTED")
        database_identifier = _quote_identifier(name)
        role_identifier = _quote_identifier(app_role)
        maintenance = await self._connect()
        try:
            role = await maintenance.fetchrow(
                "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname = $1",
                app_role,
            )
            if (
                not role
                or not role["rolcanlogin"]
                or any(
                    role[field]
                    for field in (
                        "rolsuper",
                        "rolcreatedb",
                        "rolcreaterole",
                        "rolreplication",
                        "rolbypassrls",
                    )
                )
            ):
                raise RuntimeError("DATABASE_APP_ROLE_UNSAFE")
            await maintenance.execute(f"GRANT CONNECT, TEMPORARY ON DATABASE {database_identifier} TO {role_identifier}")
        finally:
            await maintenance.close()
        parsed = urlsplit(self._admin_url)
        target_url = urlunsplit((parsed.scheme, parsed.netloc, f"/{name}", parsed.query, parsed.fragment))
        target = await asyncpg.connect(target_url, timeout=10)
        try:
            await target.execute(f"GRANT USAGE, CREATE ON SCHEMA public TO {role_identifier}")
            await target.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role_identifier}")
            await target.execute(f"GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {role_identifier}")
            await target.execute(f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO {role_identifier}")
        finally:
            await target.close()

    async def drop(self, owned: OwnedDatabase) -> None:
        if _TEST_DATABASE_NAME.fullmatch(owned.name) is None and _RESTORE_DATABASE_NAME.fullmatch(owned.name) is None:
            raise RuntimeError("OWNED_DATABASE_NAME_INVALID")
        database_identifier = _quote_identifier(owned.name)
        connection = await self._connect()
        try:
            row = await connection.fetchrow(
                """SELECT owner.rolname AS owner,
                          shobj_description(database.oid, 'pg_database') AS marker
                     FROM pg_database AS database
                     JOIN pg_roles AS owner ON owner.oid = database.datdba
                    WHERE database.datname = $1""",
                owned.name,
            )
            if not row or row["owner"] != owned.owner or row["marker"] != f"deerflow-m8:{owned.marker_digest}":
                raise RuntimeError("OWNED_DATABASE_IDENTITY_MISMATCH")
            await connection.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid()", owned.name)
            await connection.execute(f"DROP DATABASE {database_identifier}")
        finally:
            await connection.close()


class SubprocessHostCommandRunner:
    def __init__(self, *, repository: Path, ledger: OwnershipLedger) -> None:
        self._repository = repository
        self._ledger = ledger
        self._processes: dict[int, asyncio.subprocess.Process] = {}
        self._drainers: dict[int, asyncio.Task[set[str]]] = {}
        self._markers: dict[int, set[str]] = {}
        self._timings: dict[str, HostCommandTiming] = {}
        self._pending_timing_starts: dict[str, datetime] = {}
        self._log_security_failures: list[bool] = []

    @property
    def timings(self) -> tuple[HostCommandTiming, ...]:
        return tuple(self._timings.values())

    def timing_for(self, command_id: str) -> HostCommandTiming | None:
        return self._timings.get(command_id)

    @property
    def logs_security_passed(self) -> bool:
        return not self._log_security_failures

    def _record_timing(self, command_id: str, started_at: datetime, finished_at: datetime) -> None:
        self._timings.setdefault(
            command_id,
            HostCommandTiming(
                command_id=command_id,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=int((finished_at - started_at).total_seconds() * 1000),
            ),
        )

    def finish_timing(self, command_id: str) -> None:
        started_at = self._pending_timing_starts.pop(command_id, None)
        if started_at is not None:
            self._record_timing(command_id, started_at, datetime.now(UTC))

    @staticmethod
    async def _drain(
        stream: asyncio.StreamReader,
        markers: set[str],
        security_failures: list[bool] | None = None,
    ) -> set[str]:
        scanner = SecretScanner()
        while line := await stream.readline():
            if len(line) > 128 * 1024 or scanner.scan_bytes(
                line,
                scope="runtime_logs",
                locator="host-command-output",
            ):
                if security_failures is not None:
                    security_failures.append(True)
            bounded = (line[:512] + line[-512:]).decode("utf-8", errors="replace")
            for label, marker in _STARTUP_MARKERS.items():
                if f"{label} started" in bounded or f"{label} started on" in bounded:
                    markers.add(marker)
        return markers

    async def _spawn(self, argv: tuple[str, ...], environment: dict[str, str]) -> tuple[asyncio.subprocess.Process, HostProcess]:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self._repository,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        if process.stdout is None:
            process.kill()
            await process.wait()
            raise RuntimeError("HOST_PROCESS_START_FAILED")
        identity = await asyncio.to_thread(self._ledger.process_start_identity, process.pid)
        if identity is None:
            process.kill()
            await process.wait()
            raise RuntimeError("HOST_PROCESS_IDENTITY_FAILED")
        pgid = await asyncio.to_thread(os.getpgid, process.pid)
        host_process = HostProcess(pid=process.pid, pgid=pgid, start_identity=identity)
        self._processes[process.pid] = process
        markers: set[str] = set()
        self._markers[process.pid] = markers
        self._drainers[process.pid] = asyncio.create_task(
            self._drain(
                process.stdout,
                markers,
                self._log_security_failures,
            )
        )
        return process, host_process

    async def run(self, command_id: str, argv: tuple[str, ...], environment: dict[str, str], *, timeout_seconds: int) -> None:
        started_at = datetime.now(UTC)
        process, host_process = await self._spawn(argv, environment)
        owned = self._ledger.register_process(pid=host_process.pid, pgid=host_process.pgid, start_identity=host_process.start_identity)
        try:
            returncode = await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
        except TimeoutError:
            await asyncio.to_thread(self._ledger.stop_process, owned)
            raise RuntimeError("HOST_COMMAND_TIMEOUT") from None
        finally:
            await asyncio.gather(self._drainers.pop(process.pid), return_exceptions=True)
            self._processes.pop(process.pid, None)
            self._markers.pop(process.pid, None)
            self._record_timing(command_id, started_at, datetime.now(UTC))
        if returncode != 0:
            raise RuntimeError("HOST_COMMAND_FAILED")

    async def start(self, command_id: str, argv: tuple[str, ...], environment: dict[str, str]) -> HostProcess:
        self._pending_timing_starts.setdefault(command_id, datetime.now(UTC))
        process, host_process = await self._spawn(argv, environment)
        await asyncio.sleep(0)
        if process.returncode is not None:
            await asyncio.gather(self._drainers.pop(process.pid), return_exceptions=True)
            self._processes.pop(process.pid, None)
            raise RuntimeError("HOST_PROCESS_START_FAILED")
        return host_process

    def startup_markers(self, process: HostProcess) -> frozenset[str]:
        return frozenset(self._markers.get(process.pid, set()))

    async def reap(self, process: HostProcess) -> None:
        child = self._processes.pop(process.pid, None)
        drainer = self._drainers.pop(process.pid, None)
        if child is not None:
            try:
                await asyncio.wait_for(child.wait(), timeout=10)
            except TimeoutError:
                raise RuntimeError("HOST_PROCESS_REAP_FAILED") from None
        if drainer is not None:
            await asyncio.gather(drainer, return_exceptions=True)
        self._markers.pop(process.pid, None)


class OwnedHostStack:
    def __init__(
        self,
        *,
        repository: Path,
        env: Mapping[str, str],
        acceptance_run_id: uuid.UUID,
        app_role: str,
        ledger: OwnershipLedger,
        database_manager: HostDatabaseManager,
        command_runner: HostCommandRunner,
        readiness: ReadinessProbe | None = None,
        port_probe: PortProbe | None = None,
        process_inspector: ProcessInspector | None = None,
        scheduler_enabled: bool,
        startup_marker_attempts: int = 100,
        startup_marker_interval_seconds: float = 0.05,
        shutdown_port_attempts: int = 100,
        shutdown_port_interval_seconds: float = 0.05,
    ) -> None:
        self._repository = repository.resolve()
        self._env = dict(env)
        self._acceptance_run_id = acceptance_run_id
        self._app_role = app_role
        self._ledger = ledger
        self._database_manager = database_manager
        self._command_runner = command_runner
        self._readiness = readiness or HostReadiness()
        self._port_probe = port_probe or SocketPortProbe()
        self._process_inspector = process_inspector or SystemProcessInspector()
        self._scheduler_enabled = scheduler_enabled
        self._startup_marker_attempts = startup_marker_attempts
        self._startup_marker_interval_seconds = startup_marker_interval_seconds
        self._shutdown_port_attempts = shutdown_port_attempts
        self._shutdown_port_interval_seconds = shutdown_port_interval_seconds
        self._owned_process: OwnedProcess | None = None
        self._host_process: HostProcess | None = None
        self._gateway_process: HostProcess | None = None
        self._gateway_owned_process: OwnedProcess | None = None
        self._last_pgid: int | None = None
        self._database_url: str | None = None
        self._application_url_template: str | None = None
        self._owned_database: OwnedDatabase | None = None
        self._inventory: HostInventory | None = None
        self._ports_reserved = False

    async def _wait_startup_markers(self, process: HostProcess, expected: set[str]) -> None:
        for attempt in range(self._startup_marker_attempts):
            if expected.issubset(self._command_runner.startup_markers(process)):
                return
            if attempt + 1 < self._startup_marker_attempts:
                await asyncio.sleep(self._startup_marker_interval_seconds)
        raise RuntimeError("HOST_STARTUP_MARKERS_MISSING")

    async def _wait_ports_free(self) -> None:
        for attempt in range(self._shutdown_port_attempts):
            free = await asyncio.gather(*(asyncio.to_thread(self._port_probe.is_free, port) for port in _HOST_PORTS))
            if all(free):
                return
            if attempt + 1 < self._shutdown_port_attempts:
                await asyncio.sleep(self._shutdown_port_interval_seconds)
        raise RuntimeError("HOST_PORT_RESIDUAL")

    @property
    def pgid(self) -> int:
        if self._owned_process is not None:
            return self._owned_process.pgid
        if self._last_pgid is None:
            raise RuntimeError("HOST_STACK_NOT_STARTED")
        return self._last_pgid

    @property
    def inventory(self) -> HostInventory:
        if self._inventory is None:
            raise RuntimeError("HOST_STACK_NOT_STARTED")
        return self._inventory

    @property
    def database_url(self) -> str:
        if self._database_url is None:
            raise RuntimeError("HOST_STACK_NOT_STARTED")
        return self._database_url

    @property
    def owned_database(self) -> OwnedDatabase:
        if self._owned_database is None:
            raise RuntimeError("HOST_STACK_NOT_STARTED")
        return self._owned_database

    def url_for_owned(self, owned: OwnedDatabase) -> str:
        if self._application_url_template is None:
            raise RuntimeError("HOST_STACK_NOT_STARTED")
        parsed = urlsplit(self._application_url_template)
        return urlunsplit((parsed.scheme, parsed.netloc, f"/{owned.name}", parsed.query, parsed.fragment))

    def _environment(self, database_url: str) -> dict[str, str]:
        values = {name: value for name, value in self._env.items() if name in _SAFE_HOST_ENV}
        values["DATABASE_URL"] = database_url
        return values

    def _source_database(self, database_url: str) -> tuple[str, str]:
        parsed = urlsplit(database_url.replace("postgresql+asyncpg://", "postgresql://", 1))
        if parsed.scheme != "postgresql" or not parsed.hostname or unquote(parsed.username or "") != self._app_role:
            raise RuntimeError("DATABASE_APP_ROLE_MISMATCH")
        name = f"deerflow_test_{self._acceptance_run_id.hex}"
        source_url = urlunsplit((parsed.scheme, parsed.netloc, f"/{name}", parsed.query, parsed.fragment))
        return name, source_url

    async def _reserve_and_check_ports(self) -> None:
        for port in _HOST_PORTS:
            if not await asyncio.to_thread(self._port_probe.is_free, port):
                raise RuntimeError("HOST_PORT_BUSY")
            if not self._ports_reserved:
                self._ledger.reserve_port(port)
        self._ports_reserved = True

    async def _prepare_process(self, database_url: str, *, setup: bool) -> None:
        self._database_url = database_url
        environment = self._environment(database_url)
        if setup:
            await self._command_runner.run(
                "host.setup_db",
                ("make", "setup-db"),
                environment,
                timeout_seconds=900,
            )
        await self._command_runner.run(
            "host.check_db",
            ("make", "check-db"),
            environment,
            timeout_seconds=300,
        )

    async def launch(self) -> None:
        if self._database_url is None or self._owned_process is not None:
            raise RuntimeError("HOST_STACK_NOT_PREPARED")
        environment = self._environment(self._database_url)
        try:
            process = await self._command_runner.start("host.make_start", ("make", "start"), environment)
            self._host_process = process
            self._owned_process = self._ledger.register_process(
                pid=process.pid,
                pgid=process.pgid,
                start_identity=process.start_identity,
            )
            self._last_pgid = process.pgid
            await self._readiness.wait_ready(scheduler_enabled=self._scheduler_enabled)
            expected_markers = {"gateway", "worker", "frontend", "nginx"}
            if self._scheduler_enabled:
                expected_markers.add("scheduler")
            await self._wait_startup_markers(process, expected_markers)
            self._inventory = await asyncio.to_thread(
                self._process_inspector.discover,
                pgid=process.pgid,
                scheduler_enabled=self._scheduler_enabled,
            )
        except BaseException:
            if self._owned_process is not None:
                await self.stop()
            raise RuntimeError("HOST_READINESS_FAILED") from None
        finally:
            finish_timing = getattr(self._command_runner, "finish_timing", None)
            if finish_timing is not None:
                finish_timing("host.make_start")

    async def prepare(self, database_url: str) -> None:
        if self._owned_process is not None:
            raise RuntimeError("HOST_STACK_ALREADY_STARTED")
        if self._database_url is not None:
            raise RuntimeError("HOST_STACK_ALREADY_PREPARED")
        await self._reserve_and_check_ports()
        name, source_url = self._source_database(database_url)
        marker = hashlib.sha256(f"deerflow-m8-database\0{self._acceptance_run_id}\0{name}\0{self._app_role}".encode()).hexdigest()
        await self._database_manager.create(name=name, owner=self._app_role, marker_digest=marker)
        self._owned_database = self._ledger.register_database(name=name, owner=self._app_role, marker_digest=marker)
        self._application_url_template = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        await self._prepare_process(source_url, setup=True)

    async def run_release_checks(self) -> None:
        if self._database_url is None or self._owned_process is not None:
            raise RuntimeError("HOST_STACK_NOT_PREPARED")
        environment = self._environment(self._database_url)
        await self._command_runner.run("host.doctor", ("make", "doctor"), environment, timeout_seconds=300)
        await self._command_runner.run("host.make_help", ("make", "help"), environment, timeout_seconds=300)
        runtime_value = environment.get("DEER_FLOW_RUNTIME_ROOT", "")
        if not runtime_value:
            raise RuntimeError("HOST_RUNTIME_ROOT_MISSING")
        support_output = Path(runtime_value) / "support-bundle.zip"
        support_argv = tuple(str(support_output) if item == SUPPORT_BUNDLE_OUTPUT_TOKEN else item for item in SUPPORT_BUNDLE_ARGV)
        await self._command_runner.run(
            "host.support_bundle",
            support_argv,
            environment,
            timeout_seconds=300,
        )

    async def start(self, database_url: str) -> None:
        await self.prepare(database_url)
        await self.launch()

    async def start_existing(self, database_url: str, owned: OwnedDatabase) -> None:
        if self._owned_process is not None:
            raise RuntimeError("HOST_STACK_ALREADY_STARTED")
        parsed = urlsplit(database_url.replace("postgresql+asyncpg://", "postgresql://", 1))
        if parsed.scheme != "postgresql" or unquote(parsed.username or "") != self._app_role or parsed.path != f"/{owned.name}":
            raise RuntimeError("DATABASE_APP_ROLE_MISMATCH")
        identity = await self._database_manager.identity(owned.name)
        if identity != DatabaseIdentity(owner=owned.owner, marker_digest=owned.marker_digest):
            raise RuntimeError("OWNED_DATABASE_IDENTITY_MISMATCH")
        await self._reserve_and_check_ports()
        if self._application_url_template is None:
            self._application_url_template = database_url
        self._owned_database = owned
        await self._prepare_process(database_url, setup=False)
        await self.launch()

    async def restart_gateway(self) -> None:
        if self._inventory is None or self._database_url is None or self._gateway_owned_process is not None:
            raise RuntimeError("HOST_GATEWAY_RESTART_INVALID")
        try:
            gateway = self._inventory.service("gateway")
            await asyncio.to_thread(self._process_inspector.stop_tree, gateway)
            environment = self._environment(self._database_url)
            process = await self._command_runner.start(
                "host.gateway_restart",
                ("make", "gateway"),
                environment,
            )
            self._gateway_process = process
            self._gateway_owned_process = self._ledger.register_process(
                pid=process.pid,
                pgid=process.pgid,
                start_identity=process.start_identity,
            )
            await self._readiness.wait_ready(scheduler_enabled=self._scheduler_enabled)
        except BaseException:
            if self._gateway_owned_process is not None:
                owned, self._gateway_owned_process = self._gateway_owned_process, None
                await asyncio.to_thread(self._ledger.stop_process, owned)
            raise RuntimeError("HOST_GATEWAY_RESTART_FAILED") from None

    async def stop(self) -> None:
        if self._owned_process is None:
            return
        if self._gateway_owned_process is not None:
            gateway_owned, self._gateway_owned_process = self._gateway_owned_process, None
            gateway_result: CleanupAction = await asyncio.to_thread(self._ledger.stop_process, gateway_owned)
            if self._gateway_process is not None:
                await self._command_runner.reap(self._gateway_process)
                self._gateway_process = None
            if gateway_result.status not in {"removed", "absent"}:
                raise RuntimeError("HOST_STOP_FAILED")
        owned, self._owned_process = self._owned_process, None
        result = await asyncio.to_thread(self._ledger.stop_process, owned)
        if self._host_process is not None:
            await self._command_runner.reap(self._host_process)
            self._host_process = None
        if result.status not in {"removed", "absent"}:
            raise RuntimeError("HOST_STOP_FAILED")
        try:
            if self._inventory is not None:
                await asyncio.to_thread(self._process_inspector.assert_absent, self._inventory)
            await self._wait_ports_free()
        except BaseException:
            raise RuntimeError("HOST_STOP_RESIDUAL") from None
