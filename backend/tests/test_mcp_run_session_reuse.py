"""U3: project MCP Run-level session reuse.

Acceptance pins from the upgrade plan:

- two calls in the same Run perform exactly one initialize handshake;
- cache-key drift (checksum / grant-closure digest) never reuses a session;
- after a transport error the session is rebuilt exactly once, then the
  existing public error path takes over;
- Run end closes every connection and clears the derived-secret closure;
- calls on one session are serialized (MCP sessions are not
  concurrency-safe) and an idle session closes proactively.

The fake opener stands in for the initialize handshake, so "initialize
count" == "open count" without a network server.
"""

import asyncio
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.tools import ToolException

from app.private_work.errors import PrivateWorkAssetStale, PrivateWorkUnavailable
from app.private_work.mcp_run_sessions import McpRunSessionCache


def _key(version: uuid.UUID | None = None, checksum: str = "c1", digest: str = "g1"):
    return (version or uuid.uuid4(), checksum, digest)


class _FakeClient:
    def __init__(self) -> None:
        self.close_count = 0

    async def aclose(self) -> None:
        self.close_count += 1


class _Opener:
    """Counting session factory standing in for the initialize handshake."""

    def __init__(self) -> None:
        self.clients: list[_FakeClient] = []
        self.secret_lists: list[list[str]] = []

    async def __call__(self):
        client = _FakeClient()
        secrets = ["secret-token-value"]
        self.clients.append(client)
        self.secret_lists.append(secrets)
        return client, (object(),), secrets

    @property
    def count(self) -> int:
        return len(self.clients)


async def _ok(_tools: tuple, _secrets: list) -> str:
    return "ok"


# ---------------------------------------------------------------------------
# Cache behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_calls_in_one_run_perform_one_initialize() -> None:
    cache, opener, key = McpRunSessionCache(), _Opener(), _key()

    assert await cache.call(key, opener, _ok) == "ok"
    assert await cache.call(key, opener, _ok) == "ok"

    assert opener.count == 1


@pytest.mark.asyncio
async def test_cache_key_drift_builds_a_fresh_session() -> None:
    cache, opener = McpRunSessionCache(), _Opener()
    version = uuid.uuid4()

    await cache.call(_key(version, digest="g1"), opener, _ok)
    await cache.call(_key(version, digest="g2"), opener, _ok)  # grant rotation
    await cache.call(_key(version, checksum="c2", digest="g1"), opener, _ok)

    assert opener.count == 3


@pytest.mark.asyncio
async def test_a_transport_error_rebuilds_exactly_once_and_recovers() -> None:
    cache, opener, key = McpRunSessionCache(), _Opener(), _key()
    failures = iter([ConnectionError("broken pipe")])

    async def operation(_tools: tuple, _secrets: list) -> str:
        failure = next(failures, None)
        if failure is not None:
            raise failure
        return "recovered"

    assert await cache.call(key, opener, operation) == "recovered"
    assert opener.count == 2
    assert opener.clients[0].close_count == 1  # broken session was discarded
    assert opener.clients[1].close_count == 0


@pytest.mark.asyncio
async def test_a_persistent_transport_failure_raises_after_one_rebuild() -> None:
    cache, opener, key = McpRunSessionCache(), _Opener(), _key()

    async def operation(_tools: tuple, _secrets: list) -> str:
        raise ConnectionError("still broken")

    with pytest.raises(ConnectionError):
        await cache.call(key, opener, operation)

    assert opener.count == 2
    assert all(client.close_count == 1 for client in opener.clients)


@pytest.mark.asyncio
async def test_application_errors_never_tear_down_the_session() -> None:
    cache, opener, key = McpRunSessionCache(), _Opener(), _key()

    async def stale(_tools: tuple, _secrets: list) -> str:
        raise PrivateWorkAssetStale("unknown")

    async def tool_error(_tools: tuple, _secrets: list) -> str:
        raise ToolException("tool reported an error")

    with pytest.raises(PrivateWorkAssetStale):
        await cache.call(key, opener, stale)
    with pytest.raises(ToolException):
        await cache.call(key, opener, tool_error)
    assert await cache.call(key, opener, _ok) == "ok"

    assert opener.count == 1
    assert opener.clients[0].close_count == 0


@pytest.mark.asyncio
async def test_a_hung_call_discards_the_session_without_a_retry() -> None:
    cache, opener, key = McpRunSessionCache(), _Opener(), _key()

    async def hang(_tools: tuple, _secrets: list) -> str:
        await asyncio.sleep(30)
        return "never"

    with pytest.raises(TimeoutError):
        await cache.call(key, opener, hang, call_timeout_seconds=0.05)

    assert opener.count == 1
    assert opener.clients[0].close_count == 1

    # The next call rebuilds instead of reusing the poisoned session.
    assert await cache.call(key, opener, _ok) == "ok"
    assert opener.count == 2


@pytest.mark.asyncio
async def test_run_end_closes_connections_and_clears_secrets() -> None:
    cache, opener, key = McpRunSessionCache(), _Opener(), _key()
    await cache.call(key, opener, _ok)
    assert opener.secret_lists[0] == ["secret-token-value"]

    await cache.aclose()

    assert opener.clients[0].close_count == 1
    assert opener.secret_lists[0] == []
    with pytest.raises(PrivateWorkUnavailable):
        await cache.call(key, opener, _ok)


@pytest.mark.asyncio
async def test_concurrent_calls_share_one_session_and_are_serialized() -> None:
    cache, opener, key = McpRunSessionCache(), _Opener(), _key()
    active = 0
    max_active = 0

    async def operation(_tools: tuple, _secrets: list) -> str:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return "ok"

    results = await asyncio.gather(*(cache.call(key, opener, operation) for _ in range(4)))

    assert results == ["ok"] * 4
    assert opener.count == 1
    assert max_active == 1  # per-session lock: MCP sessions are not concurrency-safe


@pytest.mark.asyncio
async def test_an_idle_session_closes_after_the_idle_window() -> None:
    cache, opener, key = McpRunSessionCache(idle_close_seconds=0.05), _Opener(), _key()
    await cache.call(key, opener, _ok)
    assert cache.active_session_count == 1

    await asyncio.sleep(0.3)

    assert cache.active_session_count == 0
    assert opener.clients[0].close_count == 1

    # A later call transparently reopens.
    assert await cache.call(key, opener, _ok) == "ok"
    assert opener.count == 2


# ---------------------------------------------------------------------------
# _invoke_exact_mcp wiring
# ---------------------------------------------------------------------------


class _RemoteTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.invocations = 0

    async def ainvoke(self, _args: dict) -> dict:
        self.invocations += 1
        return {"ok": True}


def _patched_open(monkeypatch: pytest.MonkeyPatch, tool: _RemoteTool) -> _Opener:
    from app.private_work.asset_runtime import PrivateAgentRuntime

    opener = _Opener()

    async def fake_open(
        version_id: uuid.UUID,
        definition: Any,
        material: Any,
        authorization_boundary: Any = None,
        *,
        http_client_factory: Any = None,
        discovery_timeout_seconds: int = 15,
    ):
        client = _FakeClient()
        secrets: list[str] = []
        opener.clients.append(client)
        opener.secret_lists.append(secrets)
        return client, (tool,), secrets

    monkeypatch.setattr(
        PrivateAgentRuntime,
        "_open_project_mcp_session",
        staticmethod(fake_open),
    )
    return opener


@pytest.mark.asyncio
async def test_invoke_exact_mcp_reuses_one_handshake_per_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.private_work.asset_runtime import PrivateAgentRuntime

    tool = _RemoteTool("project_ab_tool")
    opener = _patched_open(monkeypatch, tool)
    cache = McpRunSessionCache()
    key = _key()

    for _ in range(3):
        result = await PrivateAgentRuntime._invoke_exact_mcp(
            key[0],
            {},
            {},
            "project_ab_tool",
            {},
            session_cache=cache,
            session_key=key,
        )
        assert result == {"ok": True}

    assert opener.count == 1
    assert tool.invocations == 3


@pytest.mark.asyncio
async def test_invoke_exact_mcp_without_a_cache_stays_one_shot(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.private_work.asset_runtime import PrivateAgentRuntime

    tool = _RemoteTool("project_ab_tool")
    opener = _patched_open(monkeypatch, tool)

    for _ in range(2):
        result = await PrivateAgentRuntime._invoke_exact_mcp(
            uuid.uuid4(),
            {},
            {},
            "project_ab_tool",
            {},
        )
        assert result == {"ok": True}

    assert opener.count == 2
    assert all(client.close_count == 1 for client in opener.clients)


def test_the_reuse_toggle_defaults_on_and_propagates() -> None:
    from app.private_work.asset_runtime import PrivateAssetRuntime
    from deerflow.config.mcp_security_config import McpSecurityConfig

    assert McpSecurityConfig().run_session_reuse is True
    assert PrivateAssetRuntime(MagicMock())._run_session_reuse is True
    assert PrivateAssetRuntime(MagicMock(), run_session_reuse=False)._run_session_reuse is False
