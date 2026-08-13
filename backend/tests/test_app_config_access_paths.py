"""Behavior and I/O boundaries for ``get_app_config`` access paths."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

from deerflow.config import app_config as app_config_module
from deerflow.config.app_config import AppConfig
from deerflow.config.database_config import DatabaseConfig
from deerflow.config.sandbox_config import SandboxConfig


@pytest.fixture(autouse=True)
def _restore_app_config_state() -> Iterator[None]:
    cached_state = (
        app_config_module._app_config,
        app_config_module._app_config_path,
        app_config_module._app_config_mtime,
        app_config_module._app_config_signature,
        app_config_module._app_config_is_custom,
    )
    current_token = app_config_module._current_app_config.set(None)
    stack_token = app_config_module._current_app_config_stack.set(())
    try:
        yield
    finally:
        (
            app_config_module._app_config,
            app_config_module._app_config_path,
            app_config_module._app_config_mtime,
            app_config_module._app_config_signature,
            app_config_module._app_config_is_custom,
        ) = cached_state
        app_config_module._current_app_config_stack.reset(stack_token)
        app_config_module._current_app_config.reset(current_token)


def _config(name: str) -> AppConfig:
    return AppConfig(
        sandbox=SandboxConfig(
            use="deerflow.sandbox.local:LocalSandboxProvider",
        ),
        database=DatabaseConfig(
            url=f"postgresql://config-test@localhost/{name}",
        ),
    )


def _seed_file_cache(
    config: AppConfig,
    *,
    path: Path,
    signature: tuple[float | None, int | None, str | None],
) -> None:
    app_config_module._app_config = config
    app_config_module._app_config_path = path
    app_config_module._app_config_mtime = signature[0]
    app_config_module._app_config_signature = signature
    app_config_module._app_config_is_custom = False


def _fail_path_resolution(
    cls: type[AppConfig],
    config_path: str | None = None,
) -> Path:
    del cls, config_path
    raise AssertionError("the fast path must not resolve or read config.yaml")


def test_file_backed_cache_rechecks_the_content_signature(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("config_version: 1\n", encoding="utf-8")
    monkeypatch.setenv("DEER_FLOW_CONFIG_PATH", str(path))
    signature = (path.stat().st_mtime, path.stat().st_size, "unchanged")
    cached = _config("cached")
    _seed_file_cache(cached, path=path, signature=signature)
    signature_checks = 0

    def get_signature(candidate: Path):
        nonlocal signature_checks
        assert candidate == path
        signature_checks += 1
        return signature

    monkeypatch.setattr(
        app_config_module,
        "_get_config_signature",
        get_signature,
    )

    assert app_config_module.get_app_config() is cached
    assert app_config_module.get_app_config() is cached
    assert signature_checks == 2


def test_same_metadata_with_a_new_digest_still_reloads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("config_version: 1\n", encoding="utf-8")
    monkeypatch.setenv("DEER_FLOW_CONFIG_PATH", str(path))
    previous_signature = (123.0, 18, "old-digest")
    current_signature = (123.0, 18, "new-digest")
    _seed_file_cache(
        _config("previous"),
        path=path,
        signature=previous_signature,
    )
    reloaded = _config("reloaded")

    monkeypatch.setattr(
        app_config_module,
        "_get_config_mtime",
        lambda candidate: 123.0,
    )
    monkeypatch.setattr(
        app_config_module,
        "_get_config_signature",
        lambda candidate: current_signature,
    )

    def reload(candidate: str | None = None) -> AppConfig:
        assert candidate == str(path)
        _seed_file_cache(
            reloaded,
            path=path,
            signature=current_signature,
        )
        return reloaded

    monkeypatch.setattr(
        app_config_module,
        "_load_and_cache_app_config",
        reload,
    )

    assert app_config_module.get_app_config() is reloaded


def test_context_override_bypasses_path_resolution_and_hashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    override = _config("runtime-override")
    app_config_module.push_current_app_config(override)
    monkeypatch.setattr(
        AppConfig,
        "resolve_config_path",
        classmethod(_fail_path_resolution),
    )
    monkeypatch.setattr(
        app_config_module,
        "_get_config_signature",
        lambda candidate: (_ for _ in ()).throw(
            AssertionError(f"unexpected signature read: {candidate}"),
        ),
    )

    try:
        assert app_config_module.get_app_config() is override
    finally:
        app_config_module.pop_current_app_config()


def test_custom_singleton_bypasses_path_resolution_and_hashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom = _config("custom-singleton")
    app_config_module.set_app_config(custom)
    monkeypatch.setattr(
        AppConfig,
        "resolve_config_path",
        classmethod(_fail_path_resolution),
    )
    monkeypatch.setattr(
        app_config_module,
        "_get_config_signature",
        lambda candidate: (_ for _ in ()).throw(
            AssertionError(f"unexpected signature read: {candidate}"),
        ),
    )

    assert app_config_module.get_app_config() is custom


@pytest.mark.asyncio
async def test_context_override_is_isolated_between_asyncio_tasks() -> None:
    base = _config("base")
    override = _config("task-override")
    app_config_module.set_app_config(base)
    override_ready = asyncio.Event()
    release_override = asyncio.Event()

    async def scoped_reader() -> None:
        app_config_module.push_current_app_config(override)
        try:
            override_ready.set()
            await release_override.wait()
            assert app_config_module.get_app_config() is override
        finally:
            app_config_module.pop_current_app_config()
        assert app_config_module.get_app_config() is base

    task = asyncio.create_task(scoped_reader())
    await override_ready.wait()
    assert app_config_module.get_app_config() is base
    release_override.set()
    await task
