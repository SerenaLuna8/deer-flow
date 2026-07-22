from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from scripts.release_acceptance.models import CleanupSummary

_TEST_DATABASE_NAME = re.compile(r"^deerflow_test_[a-z0-9]{6,64}$")
_ROLE_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class OwnershipError(ValueError):
    """An attempted resource registration is not safely invocation-owned."""


@dataclass(frozen=True, slots=True)
class OwnedProcess:
    pid: int
    pgid: int
    start_identity: str


@dataclass(frozen=True, slots=True)
class OwnedDatabase:
    name: str
    owner: str
    marker_digest: str


@dataclass(frozen=True, slots=True)
class DatabaseIdentity:
    owner: str
    marker_digest: str


@dataclass(frozen=True, slots=True)
class OwnedPath:
    anchor: Literal["repository"]
    relative_token: str
    device: int
    inode: int
    kind: Literal["file", "directory"]
    disposition: Literal["temporary", "retained_evidence"]


@dataclass(frozen=True, slots=True)
class CleanupAction:
    status: Literal["removed", "absent", "signalled", "identity_mismatch", "failed"]


class ProcessProbe(Protocol):
    def start_identity(self, pid: int) -> str | None: ...

    def process_group(self, pid: int) -> int | None: ...

    def group_members(self, pgid: int) -> tuple[int, ...]: ...

    def signal_group(self, pgid: int, signal_number: int) -> None: ...


class DatabaseProbe(Protocol):
    async def identity(self, name: str) -> DatabaseIdentity | None: ...

    async def drop(self, owned: OwnedDatabase) -> None: ...


class NullDatabaseProbe:
    async def identity(self, _name: str) -> DatabaseIdentity | None:
        return None

    async def drop(self, _owned: OwnedDatabase) -> None:
        raise RuntimeError("OWNED_DATABASE_PROBE_REQUIRED")


class HostProcessProbe:
    def start_identity(self, pid: int) -> str | None:
        try:
            completed = subprocess.run(
                (
                    "ps",
                    "-o",
                    "state=",
                    "-o",
                    "lstart=",
                    "-o",
                    "pgid=",
                    "-o",
                    "command=",
                    "-p",
                    str(pid),
                ),
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            raise OwnershipError("PROCESS_IDENTITY_PROBE_FAILED") from None
        if completed.returncode not in {0, 1}:
            raise OwnershipError("PROCESS_IDENTITY_PROBE_FAILED")
        output = completed.stdout.strip()
        if not output:
            return None
        values = output.split(maxsplit=7)
        if len(values) < 7:
            raise OwnershipError("PROCESS_IDENTITY_PROBE_FAILED")
        if values[0].startswith("Z"):
            return None
        identity = " ".join(values[1:7])
        return hashlib.sha256(identity.encode()).hexdigest()

    def process_group(self, pid: int) -> int | None:
        try:
            return os.getpgid(pid)
        except ProcessLookupError:
            return None
        except OSError:
            raise OwnershipError("PROCESS_GROUP_PROBE_FAILED") from None

    def group_members(self, pgid: int) -> tuple[int, ...]:
        try:
            completed = subprocess.run(
                ("ps", "-axo", "pid=,pgid=,state="),
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            raise OwnershipError("PROCESS_GROUP_PROBE_FAILED") from None
        members = []
        for row in completed.stdout.splitlines():
            values = row.split()
            if len(values) == 3 and values[1] == str(pgid) and not values[2].startswith("Z"):
                members.append(int(values[0]))
        return tuple(sorted(members))

    def signal_group(self, pgid: int, signal_number: int) -> None:
        os.killpg(pgid, signal_number)


class OwnershipLedger:
    def __init__(
        self,
        *,
        repository: Path,
        acceptance_run_id: uuid.UUID,
        process_probe: ProcessProbe | None = None,
        database_probe: DatabaseProbe | None = None,
    ) -> None:
        self.repository = repository.resolve()
        self.acceptance_run_id = acceptance_run_id
        self._process_probe = process_probe or HostProcessProbe()
        self._database_probe = database_probe or NullDatabaseProbe()
        self._processes: list[OwnedProcess] = []
        self._databases: list[OwnedDatabase] = []
        self._paths: list[OwnedPath] = []
        self._ports: set[int] = set()

    def process_start_identity(self, pid: int) -> str | None:
        return self._process_probe.start_identity(pid)

    def register_process(self, *, pid: int, pgid: int, start_identity: str) -> OwnedProcess:
        if pid <= 1 or pgid <= 1 or not start_identity or any(item.pid == pid for item in self._processes):
            raise OwnershipError("OWNED_PROCESS_IDENTITY_INVALID")
        owned = OwnedProcess(pid=pid, pgid=pgid, start_identity=start_identity)
        self._processes.append(owned)
        return owned

    def register_database(self, *, name: str, owner: str, marker_digest: str) -> OwnedDatabase:
        if _TEST_DATABASE_NAME.fullmatch(name) is None:
            raise OwnershipError("OWNED_DATABASE_NAME_INVALID")
        if _ROLE_NAME.fullmatch(owner) is None or _SHA256.fullmatch(marker_digest) is None:
            raise OwnershipError("OWNED_DATABASE_IDENTITY_INVALID")
        if any(item.name == name for item in self._databases):
            raise OwnershipError("OWNED_DATABASE_DUPLICATE")
        owned = OwnedDatabase(name=name, owner=owner, marker_digest=marker_digest)
        self._databases.append(owned)
        return owned

    def _register_anchored_path(
        self,
        path: Path,
        *,
        anchor: Literal["repository"],
        disposition: Literal["temporary", "retained_evidence"],
    ) -> OwnedPath:
        root = self.repository
        candidate = path if path.is_absolute() else root / path
        absolute = Path(os.path.abspath(candidate))
        try:
            relative = absolute.relative_to(root).as_posix()
        except ValueError:
            raise OwnershipError("OWNED_PATH_OUTSIDE_REPOSITORY") from None
        token = PurePosixPath(relative)
        if relative in {"", "."} or str(token) != relative or any(part in {"", ".", ".."} for part in token.parts):
            raise OwnershipError("OWNED_PATH_TOKEN_INVALID")
        current = root
        for part in token.parts:
            current /= part
            info = os.lstat(current)
            if stat.S_ISLNK(info.st_mode):
                raise OwnershipError("OWNED_PATH_SYMLINK_REJECTED")
        expected_evidence = f".release-evidence/{self.acceptance_run_id}"
        if disposition == "retained_evidence" and relative != expected_evidence:
            raise OwnershipError("RETAINED_PATH_NOT_EVIDENCE")
        info = os.lstat(absolute)
        if stat.S_ISLNK(info.st_mode):
            raise OwnershipError("OWNED_PATH_SYMLINK_REJECTED")
        if stat.S_ISREG(info.st_mode):
            kind: Literal["file", "directory"] = "file"
        elif stat.S_ISDIR(info.st_mode):
            kind = "directory"
        else:
            raise OwnershipError("OWNED_PATH_KIND_INVALID")
        owned = OwnedPath(
            anchor=anchor,
            relative_token=relative,
            device=info.st_dev,
            inode=info.st_ino,
            kind=kind,
            disposition=disposition,
        )
        if any(item.anchor == anchor and item.relative_token == relative for item in self._paths):
            raise OwnershipError("OWNED_PATH_DUPLICATE")
        self._paths.append(owned)
        return owned

    def register_path(self, path: Path, *, disposition: Literal["temporary", "retained_evidence"]) -> OwnedPath:
        return self._register_anchored_path(
            path,
            anchor="repository",
            disposition=disposition,
        )

    def reserve_port(self, port: int) -> None:
        if port < 1 or port > 65535 or port in self._ports:
            raise OwnershipError("OWNED_PORT_INVALID")
        self._ports.add(port)

    def stop_process(self, owned: OwnedProcess) -> CleanupAction:
        current = self._process_probe.start_identity(owned.pid)
        members = self._process_probe.group_members(owned.pgid)
        if current is None and not members:
            return CleanupAction(status="absent")
        if current is not None and (current != owned.start_identity or self._process_probe.process_group(owned.pid) != owned.pgid):
            return CleanupAction(status="identity_mismatch")
        try:
            self._process_probe.signal_group(owned.pgid, signal.SIGTERM)
            for _ in range(20):
                time.sleep(0.1)
                members = self._process_probe.group_members(owned.pgid)
                if not members:
                    return CleanupAction(status="removed")
            self._process_probe.signal_group(owned.pgid, signal.SIGKILL)
            for _ in range(20):
                time.sleep(0.05)
                members = self._process_probe.group_members(owned.pgid)
                if not members:
                    return CleanupAction(status="removed")
        except (OSError, ValueError):
            return CleanupAction(status="failed")
        return CleanupAction(status="failed")

    def remove_path(self, owned: OwnedPath) -> CleanupAction:
        root = self.repository
        target = root / owned.relative_token
        current = root
        try:
            for part in PurePosixPath(owned.relative_token).parts:
                current /= part
                current_info = os.lstat(current)
                if stat.S_ISLNK(current_info.st_mode):
                    return CleanupAction(status="identity_mismatch")
            info = os.lstat(target)
        except FileNotFoundError:
            return CleanupAction(status="absent")
        except OSError:
            return CleanupAction(status="failed")
        if stat.S_ISLNK(info.st_mode) or (info.st_dev, info.st_ino) != (owned.device, owned.inode):
            return CleanupAction(status="identity_mismatch")
        expected_kind = stat.S_ISDIR(info.st_mode) if owned.kind == "directory" else stat.S_ISREG(info.st_mode)
        if not expected_kind:
            return CleanupAction(status="identity_mismatch")
        try:
            if owned.kind == "directory":
                if not shutil.rmtree.avoids_symlink_attacks:
                    return CleanupAction(status="failed")
                shutil.rmtree(target)
            else:
                target.unlink()
        except OSError:
            return CleanupAction(status="failed")
        return CleanupAction(status="removed")

    async def drop_database(self, owned: OwnedDatabase) -> CleanupAction:
        try:
            current = await self._database_probe.identity(owned.name)
        except BaseException:
            return CleanupAction(status="failed")
        if current is None:
            return CleanupAction(status="absent")
        if current.owner != owned.owner or current.marker_digest != owned.marker_digest:
            return CleanupAction(status="identity_mismatch")
        try:
            await self._database_probe.drop(owned)
        except BaseException:
            return CleanupAction(status="failed")
        return CleanupAction(status="removed")

    @staticmethod
    def _port_has_listener(port: int) -> bool:
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

    @staticmethod
    def _port_is_free(port: int) -> bool:
        if OwnershipLedger._port_has_listener(port):
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

    async def cleanup(self) -> CleanupSummary:
        process_results: list[CleanupAction] = []
        for item in reversed(self._processes):
            try:
                process_results.append(await asyncio.to_thread(self.stop_process, item))
            except BaseException:
                process_results.append(CleanupAction(status="failed"))
        database_results: list[CleanupAction] = []
        for item in reversed(self._databases):
            try:
                database_results.append(await self.drop_database(item))
            except BaseException:
                database_results.append(CleanupAction(status="failed"))
        temporary_paths = [item for item in reversed(self._paths) if item.disposition == "temporary"]
        path_results: list[CleanupAction] = []
        for item in temporary_paths:
            try:
                path_results.append(await asyncio.to_thread(self.remove_path, item))
            except BaseException:
                path_results.append(CleanupAction(status="failed"))
        port_results = await asyncio.gather(
            *(asyncio.to_thread(self._port_is_free, port) for port in sorted(self._ports)),
            return_exceptions=True,
        )
        bad = {"identity_mismatch", "failed"}
        return CleanupSummary(
            residual_processes=sum(item.status in bad for item in process_results),
            residual_ports=sum(free is not True for free in port_results),
            residual_databases=sum(item.status in bad for item in database_results),
            residual_paths=sum(item.status in bad for item in path_results),
            retained_evidence=sum(item.disposition == "retained_evidence" for item in self._paths),
        )
