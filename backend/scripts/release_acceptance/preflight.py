from __future__ import annotations

import asyncio
import errno
import os
import platform
import shutil
import socket
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import unquote, urlsplit

import asyncpg
import yaml
from pydantic import Field

from deerflow.config.app_config import LEGACY_CONFIG_TOMBSTONES, AppConfig
from scripts.release_acceptance.contracts import canonical_digest
from scripts.release_acceptance.models import StrictModel

_GIT_COMMIT = r"^[0-9a-f]{40,64}$"
_SHA256 = r"^[0-9a-f]{64}$"
_REQUIRED_PORTS = (2026, 3000, 8001)
_TOOL_COMMANDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("node", ("node", "--version")),
    ("pnpm", ("pnpm", "--version")),
    ("uv", ("uv", "--version")),
    ("psql", ("psql", "--version")),
    ("nginx", ("nginx", "-v")),
)


class AcceptanceModel(StrictModel):
    logical_name: str = Field(min_length=1, max_length=128)
    provider_model_id: str = Field(min_length=1, max_length=128)
    provider: Literal["deepseek"]


@dataclass(frozen=True, slots=True)
class AcceptanceConfig:
    version: int
    current_version: int
    models: tuple[AcceptanceModel, ...]
    removed_keys: tuple[str, ...] = ()
    public_digest: str | None = None


@dataclass(frozen=True, slots=True)
class GitState:
    commit: str
    clean: bool
    detached: bool


@dataclass(frozen=True, slots=True)
class ToolchainState:
    versions: Mapping[str, str]
    missing: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DatabaseAuthorityState:
    maintenance_can_create_database: bool
    app_role_safe: bool


class PreflightFailure(StrictModel):
    ok: Literal[False] = False
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$", max_length=96)
    model: None = None
    secret_present: bool = False


class PreflightSuccess(StrictModel):
    ok: Literal[True] = True
    code: Literal["OK"] = "OK"
    git_commit: str = Field(pattern=_GIT_COMMIT)
    config_digest: str = Field(pattern=_SHA256)
    toolchain_digest: str = Field(pattern=_SHA256)
    model: AcceptanceModel
    secret_present: Literal[True] = True


PreflightResult = PreflightFailure | PreflightSuccess


class GitProbe(Protocol):
    def snapshot(self, repository: Path) -> GitState: ...


class PortProbe(Protocol):
    def busy_ports(self, ports: tuple[int, ...]) -> tuple[int, ...]: ...


class DatabaseProbe(Protocol):
    async def check(self, admin_url: str, database_url: str) -> DatabaseAuthorityState: ...


class ToolProbe(Protocol):
    def snapshot(self, repository: Path) -> ToolchainState: ...


class SubprocessGitProbe:
    @staticmethod
    def _git(repository: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ("git", *args),
            cwd=repository,
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )

    def snapshot(self, repository: Path) -> GitState:
        commit = self._git(repository, "rev-parse", "HEAD").stdout.strip()
        branch = self._git(repository, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        status = self._git(repository, "status", "--porcelain=v1", "--untracked-files=all").stdout.strip()
        return GitState(commit=commit, clean=not status, detached=branch.returncode != 0 or not branch.stdout.strip())


class SocketPortProbe:
    def busy_ports(self, ports: tuple[int, ...]) -> tuple[int, ...]:
        busy: list[int] = []
        for port in ports:
            free = True
            for family, address in (
                (socket.AF_INET, ("0.0.0.0", port)),
                (socket.AF_INET6, ("::", port, 0, 0)),
            ):
                try:
                    handle = socket.socket(family, socket.SOCK_STREAM)
                except OSError as exc:
                    if exc.errno in {errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT}:
                        continue
                    free = False
                    break
                try:
                    handle.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
                    handle.bind(address)
                except OSError:
                    free = False
                    break
                finally:
                    handle.close()
            if not free:
                busy.append(port)
        return tuple(busy)


class AsyncpgDatabaseProbe:
    async def check(self, admin_url: str, database_url: str) -> DatabaseAuthorityState:
        parsed_admin = urlsplit(admin_url.replace("postgresql+asyncpg://", "postgresql://", 1))
        parsed_database = urlsplit(database_url.replace("postgresql+asyncpg://", "postgresql://", 1))
        if parsed_admin.path != "/postgres" or not parsed_database.username or parsed_admin.hostname != parsed_database.hostname or (parsed_admin.port or 5432) != (parsed_database.port or 5432):
            return DatabaseAuthorityState(maintenance_can_create_database=False, app_role_safe=False)
        connection = await asyncpg.connect(admin_url.replace("postgresql+asyncpg://", "postgresql://", 1), timeout=10)
        try:
            admin = await connection.fetchrow("SELECT rolsuper, rolcreatedb FROM pg_roles WHERE rolname = current_user")
            app = await connection.fetchrow(
                """SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole,
                          rolreplication, rolbypassrls
                     FROM pg_roles WHERE rolname = $1""",
                unquote(parsed_database.username),
            )
        finally:
            await connection.close()
        maintenance_safe = bool(admin and (admin["rolsuper"] or admin["rolcreatedb"]))
        app_safe = bool(app and app["rolcanlogin"] and not app["rolsuper"] and not app["rolcreatedb"] and not app["rolcreaterole"] and not app["rolreplication"] and not app["rolbypassrls"])
        return DatabaseAuthorityState(maintenance_can_create_database=maintenance_safe, app_role_safe=app_safe)


class HostToolProbe:
    @staticmethod
    def _version(argv: tuple[str, ...], *, cwd: Path) -> str | None:
        if shutil.which(argv[0]) is None:
            return None
        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        value = completed.stdout.strip().splitlines()
        return value[-1][:128] if value else None

    @staticmethod
    def _executable_path(argv: tuple[str, ...], *, cwd: Path) -> str | None:
        if shutil.which(argv[0]) is None:
            return None
        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        value = completed.stdout.strip().splitlines()
        if not value or len(value[-1]) > 4096:
            return None
        return value[-1]

    def snapshot(self, repository: Path) -> ToolchainState:
        versions: dict[str, str] = {
            "python": platform.python_version(),
            "os": platform.system(),
            "architecture": platform.machine(),
        }
        missing: list[str] = []
        for name, argv in _TOOL_COMMANDS:
            version = self._version(argv, cwd=repository)
            if version is None:
                missing.append(name)
            else:
                versions[name] = version
        frontend = repository / "frontend"
        chromium = self._version(("pnpm", "exec", "playwright", "--version"), cwd=frontend)
        executable = self._executable_path(
            (
                "node",
                "-e",
                "const {chromium}=require('@playwright/test');process.stdout.write(chromium.executablePath())",
            ),
            cwd=frontend,
        )
        if chromium is None or executable is None or not Path(executable).is_file() or not os.access(executable, os.X_OK):
            missing.append("chromium")
        else:
            versions["chromium"] = chromium
        return ToolchainState(versions=versions, missing=tuple(sorted(missing)))


ConfigLoader = Callable[[Path, Mapping[str, str]], AcceptanceConfig]


def _resolve_environment(value: object, env: Mapping[str, str]) -> object:
    if isinstance(value, str) and value.startswith("$") and len(value) > 1:
        return env.get(value[1:], value)
    if isinstance(value, dict):
        return {key: _resolve_environment(item, env) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_environment(item, env) for item in value]
    return value


def _public_config(value: object, *, key: str = "") -> object:
    normalized = key.casefold().replace("-", "_")
    sensitive = any(token in normalized.split("_") for token in ("key", "token", "secret", "password", "passwd", "cookie", "credential", "nonce", "ciphertext"))
    location = normalized.endswith(("_path", "_url", "_uri")) or normalized in {"url", "path", "host_path", "container_path"}
    if sensitive or location:
        return {"present": value is not None and value != "" and value != () and value != [] and value != {}}
    if isinstance(value, dict):
        return {str(child_key): _public_config(item, key=str(child_key)) for child_key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, list):
        return [_public_config(item) for item in value]
    return value


def load_acceptance_config(repository: Path, env: Mapping[str, str]) -> AcceptanceConfig:
    config_path = Path(env.get("DEER_FLOW_CONFIG_PATH", repository / "config.yaml"))
    example_path = repository / "config.example.yaml"
    with config_path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    with example_path.open(encoding="utf-8") as handle:
        example = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict) or not isinstance(example, dict):
        raise ValueError("CONFIG_INVALID")
    removed = tuple(sorted((LEGACY_CONFIG_TOMBSTONES | {"checkpointer"}).intersection(raw)))
    version = int(raw.get("config_version", 0))
    current_version = int(example.get("config_version", 0))
    if removed:
        return AcceptanceConfig(version=version, current_version=current_version, models=(), removed_keys=removed)
    resolved = _resolve_environment(raw, env)
    validated = AppConfig.model_validate(resolved)
    models = tuple(AcceptanceModel(logical_name=model.name, provider_model_id=model.model, provider="deepseek") for model in validated.models if model.model == "deepseek-v4-pro" and "deepseek" in model.use.casefold())
    return AcceptanceConfig(
        version=version,
        current_version=current_version,
        models=models,
        removed_keys=removed,
        public_digest=canonical_digest(_public_config(resolved)),
    )


class Preflight:
    def __init__(
        self,
        *,
        repository: Path,
        env: Mapping[str, str] | None = None,
        config_loader: ConfigLoader = load_acceptance_config,
        git_probe: GitProbe | None = None,
        port_probe: PortProbe | None = None,
        database_probe: DatabaseProbe | None = None,
        tool_probe: ToolProbe | None = None,
    ) -> None:
        self._repository = repository.resolve()
        self._env = dict(os.environ if env is None else env)
        self._config_loader = config_loader
        self._git_probe = git_probe or SubprocessGitProbe()
        self._port_probe = port_probe or SocketPortProbe()
        self._database_probe = database_probe or AsyncpgDatabaseProbe()
        self._tool_probe = tool_probe or HostToolProbe()

    @staticmethod
    def _failure(code: str) -> PreflightFailure:
        return PreflightFailure(code=code)

    async def check(self) -> PreflightResult:
        if self._env.get("M8_LIVE_ACCEPTANCE") != "1":
            return self._failure("M8_LIVE_ACCEPTANCE_REQUIRED")
        try:
            git = await asyncio.to_thread(self._git_probe.snapshot, self._repository)
        except Exception:
            return self._failure("GIT_PREFLIGHT_FAILED")
        if git.detached:
            return self._failure("GIT_HEAD_DETACHED")
        if not git.clean:
            return self._failure("GIT_TREE_NOT_CLEAN")
        try:
            config = await asyncio.to_thread(self._config_loader, self._repository, self._env)
        except Exception:
            return self._failure("CONFIG_INVALID")
        if config.version != config.current_version:
            return self._failure("CONFIG_VERSION_MISMATCH")
        if config.removed_keys:
            return self._failure("CONFIG_REMOVED_KEY_PRESENT")
        if len(config.models) != 1:
            return self._failure("DEEPSEEK_MODEL_NOT_UNIQUE")
        if not self._env.get("DEEPSEEK_API_KEY", "").strip():
            return self._failure("DEEPSEEK_API_KEY_MISSING")
        try:
            toolchain = await asyncio.to_thread(self._tool_probe.snapshot, self._repository)
        except Exception:
            return self._failure("TOOLCHAIN_PREFLIGHT_FAILED")
        if toolchain.missing:
            return self._failure("REQUIRED_TOOL_MISSING")
        try:
            busy_ports = await asyncio.to_thread(self._port_probe.busy_ports, _REQUIRED_PORTS)
        except Exception:
            return self._failure("PORT_PREFLIGHT_FAILED")
        if busy_ports:
            return self._failure("REQUIRED_PORT_BUSY")
        admin_url = self._env.get("POSTGRES_ADMIN_URL", "")
        database_url = self._env.get("DATABASE_URL", "")
        if not admin_url:
            return self._failure("POSTGRES_ADMIN_URL_MISSING")
        if not database_url:
            return self._failure("DATABASE_URL_MISSING")
        try:
            authority = await self._database_probe.check(admin_url, database_url)
        except Exception:
            return self._failure("DATABASE_PREFLIGHT_FAILED")
        if not authority.maintenance_can_create_database:
            return self._failure("DATABASE_MAINTENANCE_AUTHORITY_REQUIRED")
        if not authority.app_role_safe:
            return self._failure("DATABASE_APP_ROLE_UNSAFE")
        model = config.models[0]
        config_digest = config.public_digest or canonical_digest(
            {
                "config_version": config.version,
                "deepseek": model.model_dump(mode="json"),
                "secret_present": True,
            }
        )
        toolchain_digest = canonical_digest(dict(sorted(toolchain.versions.items())))
        return PreflightSuccess(
            git_commit=git.commit,
            config_digest=config_digest,
            toolchain_digest=toolchain_digest,
            model=model,
            secret_present=True,
        )
