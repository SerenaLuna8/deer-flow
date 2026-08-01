"""Strict event-loop isolation anchors for the current Sandbox runtime.

These tests intentionally use the dev branch's in-process provider contract.
They do not depend on main's removed cross-process ownership store.
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from blockbuster import BlockingError

from deerflow.community.aio_sandbox.aio_sandbox_provider import AioSandboxProvider
from deerflow.sandbox import middleware as middleware_module

pytestmark = pytest.mark.asyncio


def _make_memory_only_aio_provider():
    """Build the minimum real AIO provider state needed by ``get()``."""
    provider = AioSandboxProvider.__new__(AioSandboxProvider)
    sandbox = MagicMock(name="cached-sandbox")
    backend = MagicMock(name="sandbox-backend")
    provider._lock = threading.Lock()
    provider._sandboxes = {"sb-memory": sandbox}
    provider._last_activity = {"sb-memory": 0.0}
    provider._backend = backend
    return provider, sandbox, backend


class _BlockingReleaseProbe:
    """Provider-shaped probe whose release performs deterministic file I/O."""

    def __init__(self, probe_path: Path) -> None:
        self._probe_path = probe_path
        self._probe_path.write_text("release", encoding="utf-8")
        self.calls: list[tuple[str, int]] = []

    def release(self, sandbox_id: str) -> None:
        self.calls.append((sandbox_id, threading.get_ident()))
        self._probe_path.read_text(encoding="utf-8")


async def test_aio_get_is_memory_only_and_never_calls_the_backend() -> None:
    """A cached lookup must not perform backend, container, or network work."""
    provider, sandbox, backend = _make_memory_only_aio_provider()

    assert provider.get("sb-memory") is sandbox
    assert provider._last_activity["sb-memory"] > 0
    assert backend.mock_calls == []


async def test_aafter_agent_offloads_a_real_blocking_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The async hook must run a blocking provider release outside the loop."""
    provider = _BlockingReleaseProbe(tmp_path / "release-probe")
    monkeypatch.setattr(middleware_module, "get_sandbox_provider", lambda: provider)
    middleware = middleware_module.SandboxMiddleware()
    runtime = SimpleNamespace(context={})
    loop_thread_id = threading.get_ident()

    await middleware.aafter_agent(
        {"sandbox": {"sandbox_id": "sb-release"}},
        runtime,
    )

    assert len(provider.calls) == 1
    sandbox_id, release_thread_id = provider.calls[0]
    assert sandbox_id == "sb-release"
    assert release_thread_id != loop_thread_id


async def test_blocking_release_probe_trips_when_called_on_the_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove the release probe has teeth and the async anchor is not vacuous."""
    provider = _BlockingReleaseProbe(tmp_path / "release-probe")
    monkeypatch.setattr(middleware_module, "get_sandbox_provider", lambda: provider)
    middleware = middleware_module.SandboxMiddleware()
    runtime = SimpleNamespace(context={})

    with pytest.raises(BlockingError):
        middleware.after_agent(
            {"sandbox": {"sandbox_id": "sb-release"}},
            runtime,
        )

    assert provider.calls == [("sb-release", threading.get_ident())]
