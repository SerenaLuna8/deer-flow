from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.release_acceptance.preflight import (
    AcceptanceConfig,
    AcceptanceModel,
    DatabaseAuthorityState,
    GitState,
    Preflight,
    SubprocessGitProbe,
    ToolchainState,
    load_acceptance_config,
)


class FakeGitProbe:
    def __init__(self, state: GitState | None = None, *, error: bool = False) -> None:
        self.state = state or GitState(commit="a" * 40, clean=True, detached=False)
        self.error = error
        self.calls = 0

    def snapshot(self, _repository: Path) -> GitState:
        self.calls += 1
        if self.error:
            raise RuntimeError("raw git path /private/example")
        return self.state


class FakePortProbe:
    def __init__(self, busy: tuple[int, ...] = ()) -> None:
        self.busy = busy
        self.bind_calls = 0

    def busy_ports(self, ports: tuple[int, ...]) -> tuple[int, ...]:
        self.bind_calls += 1
        return tuple(port for port in ports if port in self.busy)


class FakeDatabaseProbe:
    def __init__(self, state: DatabaseAuthorityState | None = None, *, error: bool = False) -> None:
        self.state = state or DatabaseAuthorityState(maintenance_can_create_database=True, app_role_safe=True)
        self.error = error
        self.connect_calls = 0

    async def check(self, _admin_url: str, _database_url: str) -> DatabaseAuthorityState:
        self.connect_calls += 1
        if self.error:
            raise RuntimeError("raw database URL and password")
        return self.state


class FakeToolProbe:
    def __init__(self, missing: tuple[str, ...] = ()) -> None:
        self.missing = missing
        self.calls = 0

    def snapshot(self, _repository: Path) -> ToolchainState:
        self.calls += 1
        return ToolchainState(
            versions={"python": "3.12", "node": "22", "pnpm": "10", "uv": "0.8", "psql": "16", "nginx": "1.29", "chromium": "140"},
            missing=self.missing,
        )


def _config(*models: AcceptanceModel, version: int = 23, current_version: int = 23, removed_keys: tuple[str, ...] = ()) -> AcceptanceConfig:
    return AcceptanceConfig(
        version=version,
        current_version=current_version,
        models=models or (AcceptanceModel(logical_name="deepseek-live", provider_model_id="deepseek-v4-pro", provider="deepseek"),),
        removed_keys=removed_keys,
    )


def _env() -> dict[str, str]:
    return {
        "M8_LIVE_ACCEPTANCE": "1",
        "DEEPSEEK_API_KEY": "present-but-never-serialized",
        "POSTGRES_ADMIN_URL": "postgresql://admin:secret@127.0.0.1/postgres",
        "DATABASE_URL": "postgresql://app:secret@127.0.0.1/deerflow",
    }


def _preflight(
    tmp_path: Path,
    *,
    env: dict[str, str] | None = None,
    config: AcceptanceConfig | None = None,
    git: FakeGitProbe | None = None,
    ports: FakePortProbe | None = None,
    database: FakeDatabaseProbe | None = None,
    tools: FakeToolProbe | None = None,
) -> Preflight:
    return Preflight(
        repository=tmp_path,
        env=_env() if env is None else env,
        config_loader=lambda _repository, _env: config or _config(),
        git_probe=git or FakeGitProbe(),
        port_probe=ports or FakePortProbe(),
        database_probe=database or FakeDatabaseProbe(),
        tool_probe=tools or FakeToolProbe(),
    )


@pytest.mark.asyncio
async def test_missing_live_switch_fails_before_any_side_effect(tmp_path: Path) -> None:
    ports = FakePortProbe()
    database = FakeDatabaseProbe()
    result = await _preflight(tmp_path, env={}, ports=ports, database=database).check()
    assert result.code == "M8_LIVE_ACCEPTANCE_REQUIRED"
    assert ports.bind_calls == 0
    assert database.connect_calls == 0


@pytest.mark.asyncio
async def test_provider_model_id_is_unique_but_logical_name_may_differ(tmp_path: Path) -> None:
    result = await _preflight(tmp_path, config=_config(AcceptanceModel(logical_name="deepseek-live", provider_model_id="deepseek-v4-pro", provider="deepseek"))).check()
    assert result.code == "OK"
    assert result.model is not None
    assert result.model.logical_name == "deepseek-live"
    assert result.model.provider_model_id == "deepseek-v4-pro"
    assert result.secret_present is True


@pytest.mark.asyncio
async def test_duplicate_provider_model_id_fails_without_echo(tmp_path: Path) -> None:
    config = _config(
        AcceptanceModel(logical_name="a", provider_model_id="deepseek-v4-pro", provider="deepseek"),
        AcceptanceModel(logical_name="b", provider_model_id="deepseek-v4-pro", provider="deepseek"),
    )
    result = await _preflight(tmp_path, config=config).check()
    assert result.code == "DEEPSEEK_MODEL_NOT_UNIQUE"
    encoded = result.model_dump_json()
    assert "present-but-never-serialized" not in encoded
    assert "postgresql://" not in encoded


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "code"),
    [
        (GitState(commit="a" * 40, clean=False, detached=False), "GIT_TREE_NOT_CLEAN"),
        (GitState(commit="a" * 40, clean=True, detached=True), "GIT_HEAD_DETACHED"),
    ],
)
async def test_git_identity_failures_are_stable(tmp_path: Path, state: GitState, code: str) -> None:
    result = await _preflight(tmp_path, git=FakeGitProbe(state)).check()
    assert result.code == code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "code"),
    [
        (_config(version=22), "CONFIG_VERSION_MISMATCH"),
        (_config(removed_keys=("stream_bridge",)), "CONFIG_REMOVED_KEY_PRESENT"),
    ],
)
async def test_stale_or_tombstoned_config_fails_closed(tmp_path: Path, config: AcceptanceConfig, code: str) -> None:
    result = await _preflight(tmp_path, config=config).check()
    assert result.code == code


@pytest.mark.asyncio
async def test_missing_secret_binary_busy_port_and_unsafe_role_are_bounded(tmp_path: Path) -> None:
    missing_secret = _env()
    missing_secret.pop("DEEPSEEK_API_KEY")
    assert (await _preflight(tmp_path, env=missing_secret).check()).code == "DEEPSEEK_API_KEY_MISSING"
    assert (await _preflight(tmp_path, tools=FakeToolProbe(("chromium",))).check()).code == "REQUIRED_TOOL_MISSING"
    assert (await _preflight(tmp_path, ports=FakePortProbe((2026,))).check()).code == "REQUIRED_PORT_BUSY"
    unsafe = replace(DatabaseAuthorityState(maintenance_can_create_database=True, app_role_safe=True), app_role_safe=False)
    assert (await _preflight(tmp_path, database=FakeDatabaseProbe(unsafe)).check()).code == "DATABASE_APP_ROLE_UNSAFE"


@pytest.mark.asyncio
async def test_raw_probe_exception_is_replaced_by_stable_code(tmp_path: Path) -> None:
    result = await _preflight(tmp_path, git=FakeGitProbe(error=True)).check()
    encoded = json.dumps(result.model_dump(mode="json"))
    assert result.code == "GIT_PREFLIGHT_FAILED"
    assert "/private/example" not in encoded


def test_real_git_probe_reports_detached_head(tmp_path: Path) -> None:
    subprocess.run(("git", "init", "-q"), cwd=tmp_path, check=True)
    subprocess.run(("git", "config", "user.email", "m8@example.invalid"), cwd=tmp_path, check=True)
    subprocess.run(("git", "config", "user.name", "M8 Test"), cwd=tmp_path, check=True)
    (tmp_path / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(("git", "add", "tracked.txt"), cwd=tmp_path, check=True)
    subprocess.run(("git", "commit", "-qm", "fixture"), cwd=tmp_path, check=True)
    subprocess.run(("git", "checkout", "-q", "--detach", "HEAD"), cwd=tmp_path, check=True)
    state = SubprocessGitProbe().snapshot(tmp_path)
    assert state.detached is True
    assert state.clean is True


def test_default_config_loader_surfaces_removed_key_without_resolving_secrets(tmp_path: Path) -> None:
    (tmp_path / "config.example.yaml").write_text("config_version: 23\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        "config_version: 23\nstream_bridge:\n  url: postgresql://raw-secret\n",
        encoding="utf-8",
    )
    loaded = load_acceptance_config(tmp_path, {})
    assert loaded.removed_keys == ("stream_bridge",)
    assert loaded.version == loaded.current_version == 23


def test_default_config_loader_resolves_exact_live_model_without_serializing_key(tmp_path: Path) -> None:
    (tmp_path / "config.example.yaml").write_text("config_version: 23\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        """config_version: 23
models:
  - name: deepseek-live
    use: deerflow.models.patched_deepseek:PatchedChatDeepSeek
    model: deepseek-v4-pro
    api_key: $DEEPSEEK_API_KEY
sandbox:
  use: deerflow.sandbox.local:LocalSandboxProvider
database:
  url: $DATABASE_URL
""",
        encoding="utf-8",
    )
    loaded = load_acceptance_config(
        tmp_path,
        {
            "DEEPSEEK_API_KEY": "not-for-output",
            "DATABASE_URL": "postgresql://app:not-for-output@127.0.0.1/deerflow",
        },
    )
    assert loaded.removed_keys == ()
    assert [(model.logical_name, model.provider_model_id) for model in loaded.models] == [("deepseek-live", "deepseek-v4-pro")]
    assert "not-for-output" not in repr(loaded)
    first_digest = loaded.public_digest
    changed_secret = load_acceptance_config(
        tmp_path,
        {
            "DEEPSEEK_API_KEY": "different-secret-value",
            "DATABASE_URL": "postgresql://app:different-secret-value@127.0.0.1/deerflow",
        },
    )
    assert changed_secret.public_digest == first_digest

    config_path = tmp_path / "config.yaml"
    config_path.write_text(config_path.read_text(encoding="utf-8") + "max_recursion_limit: 999\n", encoding="utf-8")
    changed_public = load_acceptance_config(
        tmp_path,
        {
            "DEEPSEEK_API_KEY": "different-secret-value",
            "DATABASE_URL": "postgresql://app:different-secret-value@127.0.0.1/deerflow",
        },
    )
    assert changed_public.public_digest != first_digest
