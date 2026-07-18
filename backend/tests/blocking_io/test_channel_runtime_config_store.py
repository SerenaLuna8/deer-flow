"""Regression anchors: project provider reads must not block the event loop.

The global runtime-config mutation API is intentionally gone. Project provider
discovery may still consume operator-managed local provider configuration, so
store construction and its initial JSON read must remain off the event loop.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest import mock

import pytest
from fastapi import FastAPI, Request

from app.channels.runtime_config_store import ChannelRuntimeConfigStore
from app.gateway.routers.project_connections import _provider_config
from deerflow.config.app_config import AppConfig, reset_app_config, set_app_config
from deerflow.config.channel_connections_config import ChannelConnectionsConfig

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _stub_app_config():
    set_app_config(AppConfig.model_validate({"sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"}}))
    yield
    reset_app_config()


def _make_request() -> Request:
    app = FastAPI()
    app.state.channel_connections_config = ChannelConnectionsConfig.model_validate(
        {
            "enabled": True,
            "slack": {"enabled": True},
        }
    )
    app.state.channels_config = {"slack": {"enabled": True}}
    return Request({"type": "http", "app": app, "headers": []})


async def test_project_provider_config_constructs_store_off_event_loop(
    tmp_path,
    monkeypatch,
) -> None:
    request = _make_request()
    store = await asyncio.to_thread(
        ChannelRuntimeConfigStore,
        tmp_path / "channels" / "runtime-config.json",
    )
    to_thread = mock.AsyncMock(return_value=store)
    monkeypatch.setattr(
        "app.gateway.routers.project_connections.asyncio.to_thread",
        to_thread,
    )

    configured, channels = await _provider_config(request)

    assert configured.enabled is True
    assert channels == {"slack": {"enabled": True}}
    to_thread.assert_awaited_once_with(ChannelRuntimeConfigStore)
    assert request.app.state.channel_runtime_config_store is store


async def test_runtime_config_store_file_is_owner_only(tmp_path) -> None:
    path = tmp_path / "channels" / "runtime-config.json"
    store = await asyncio.to_thread(ChannelRuntimeConfigStore, path)

    await asyncio.to_thread(
        store.set_provider_config,
        "slack",
        {"enabled": True, "bot_token": "xoxb-ui", "app_token": "xapp-ui"},
    )

    mode = await asyncio.to_thread(lambda: path.stat().st_mode & 0o777)
    assert mode == 0o600


async def test_runtime_config_store_overwrites_loose_existing_file(tmp_path) -> None:
    """A pre-existing world-readable file is tightened to 0o600 after a save.

    ``NamedTemporaryFile`` would yield 0o600 on a fresh path regardless of the
    code under test, so seed the destination at 0o644 first: only the store's
    atomic 0o600-temp + replace path produces an owner-only file here.
    """
    path = tmp_path / "channels" / "runtime-config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o644)

    store = await asyncio.to_thread(ChannelRuntimeConfigStore, path)
    await asyncio.to_thread(
        store.set_provider_config,
        "slack",
        {"enabled": True, "bot_token": "xoxb-ui"},
    )

    mode = await asyncio.to_thread(lambda: path.stat().st_mode & 0o777)
    assert mode == 0o600


async def test_runtime_config_store_chmod_failure_is_logged_not_fatal(tmp_path, caplog) -> None:
    """A chmod failure on the temp file is logged at debug and never aborts the save.

    This is the line the previous owner-only assertion could not protect: with the
    pre-rename chmod patched to raise, the save must still persist the secret and
    the destination must still end up owner-only (via the temp file's mkstemp mode
    that ``Path.replace`` preserves). If the chmod call were dropped, the expected
    debug record would be absent and this test would fail.
    """
    path = tmp_path / "channels" / "runtime-config.json"
    store = await asyncio.to_thread(ChannelRuntimeConfigStore, path)

    real_chmod = Path.chmod

    def chmod_spy(self: Path, mode: int, *args, **kwargs):
        if self.suffix == ".tmp":
            raise OSError("chmod unsupported on this filesystem")
        return real_chmod(self, mode, *args, **kwargs)

    def _save_with_failing_temp_chmod() -> None:
        with caplog.at_level(logging.DEBUG, logger="app.channels.runtime_config_store"), mock.patch.object(Path, "chmod", chmod_spy):
            store.set_provider_config("slack", {"enabled": True, "bot_token": "xoxb-ui"})

    await asyncio.to_thread(_save_with_failing_temp_chmod)

    assert any("Unable to chmod temporary channel runtime config store" in record.getMessage() for record in caplog.records)
    mode = await asyncio.to_thread(lambda: path.stat().st_mode & 0o777)
    assert mode == 0o600
    assert await asyncio.to_thread(store.get_provider_config, "slack") == {"enabled": True, "bot_token": "xoxb-ui"}
