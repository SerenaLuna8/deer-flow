"""Project MCP Run-level session reuse.

Acceptance pins from the upgrade plan:

- two calls in the same Run perform exactly one initialize handshake;
- cache-key drift (checksum / grant-closure digest) never reuses a session;
- after a transport error the session is rebuilt exactly once, then the
  existing public error path takes over;
- Run end closes every connection and clears the derived-secret closure;
- calls on one session are serialized (MCP sessions are not
  concurrency-safe) and an idle session closes proactively.

Most cache races use a small fake opener. A separate in-process Streamable
HTTP MCP server drives the real MCP SDK and LangChain adapter, proving five
tool calls use one initialized ``ClientSession`` instead of caching wrappers
that reconnect for every invocation.
"""

import asyncio
import json
import time
import uuid
from collections import Counter
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from langchain_core.tools import ToolException
from mcp.server.fastmcp import FastMCP

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


@pytest.mark.asyncio
async def test_http_reuse_binds_tools_to_one_initialized_client_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch the adapter's connection-bound wrapper false positive.

    ``MultiServerMCPClient.get_tools`` only caches LangChain wrappers: its
    wrappers open and initialize a new ClientSession for every invocation.
    A Run cache must instead enter ``client.session`` once and load tools
    against that live session.
    """
    from app.private_work.asset_runtime import PrivateAgentRuntime

    counts = {
        "initialize": 0,
        "session_exit": 0,
        "get_tools": 0,
        "connection_calls": 0,
        "session_calls": 0,
    }
    owner_tasks: dict[str, asyncio.Task[Any] | None] = {}
    fake_session = object()
    server_name = ""

    class ConnectionBoundTool:
        name = ""

        async def ainvoke(self, _args: dict) -> dict:
            # This models the adapter wrapper returned by get_tools(): every
            # call opens and initializes another transport session.
            counts["initialize"] += 1
            counts["connection_calls"] += 1
            return {"ok": True}

    class SessionBoundTool:
        name = ""

        async def ainvoke(self, _args: dict) -> dict:
            counts["session_calls"] += 1
            return {"ok": True}

    class FakeSessionContext:
        async def __aenter__(self) -> object:
            owner_tasks["enter"] = asyncio.current_task()
            counts["initialize"] += 1
            return fake_session

        async def __aexit__(self, *_exc_info: object) -> None:
            owner_tasks["exit"] = asyncio.current_task()
            counts["session_exit"] += 1

    class FakeAdapterClient:
        def __init__(self, connections: dict[str, object], **_kwargs: object) -> None:
            nonlocal server_name
            server_name = next(iter(connections))
            ConnectionBoundTool.name = f"{server_name}_echo"
            SessionBoundTool.name = f"{server_name}_echo"
            self.callbacks = object()
            self.tool_interceptors: list[object] = []

        async def get_tools(self, *, server_name: str) -> list[ConnectionBoundTool]:
            counts["get_tools"] += 1
            counts["initialize"] += 1
            return [ConnectionBoundTool()]

        def session(self, server_name: str) -> FakeSessionContext:
            return FakeSessionContext()

    async def fake_load_mcp_tools(
        session: object,
        **_kwargs: object,
    ) -> list[SessionBoundTool]:
        assert session is fake_session
        return [SessionBoundTool()]

    monkeypatch.setattr(
        "langchain_mcp_adapters.client.MultiServerMCPClient",
        FakeAdapterClient,
    )
    monkeypatch.setattr(
        "langchain_mcp_adapters.tools.load_mcp_tools",
        fake_load_mcp_tools,
    )

    version_id = uuid.uuid4()
    key = _key(version_id)
    cache = McpRunSessionCache()
    definition = {
        "transport": "http",
        "url": "https://mcp.example.test/tools",
    }
    expected_name = f"project_{version_id.hex[:16]}_echo"

    for _ in range(2):
        assert await PrivateAgentRuntime._invoke_exact_mcp(
            version_id,
            definition,
            {},
            expected_name,
            {},
            session_cache=cache,
            session_key=key,
        ) == {"ok": True}

    # Run-end cleanup need not originate from the task that first called the
    # tool. The session owner must still exit the anyio context in its own task.
    await asyncio.create_task(cache.aclose())

    assert server_name == f"project_{version_id.hex[:16]}"
    assert owner_tasks["enter"] is owner_tasks["exit"]
    assert counts == {
        "initialize": 1,
        "session_exit": 1,
        "get_tools": 0,
        "connection_calls": 0,
        "session_calls": 2,
    }


@pytest.mark.asyncio
async def test_five_real_streamable_http_calls_reuse_one_initialized_session() -> None:
    """Exercise the actual MCP SDK, adapter loader, and production invoke path."""
    from app.private_work.asset_runtime import PrivateAgentRuntime

    server = FastMCP(
        "run-session-probe",
        stateless_http=True,
        log_level="ERROR",
    )

    @server.tool()
    def echo(value: str) -> str:
        return value

    app = server.streamable_http_app()
    methods: Counter[str] = Counter()

    async def counting_app(scope, receive, send) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return

        received: list[dict[str, Any]] = []
        body = b""
        while True:
            message = await receive()
            received.append(message)
            if message["type"] != "http.request":
                break
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        try:
            payload = json.loads(body)
            requests = payload if isinstance(payload, list) else [payload]
            for request in requests:
                method = request.get("method")
                if isinstance(method, str):
                    methods[method] += 1
                    if method == "initialize":
                        # Make handshake overhead deterministic enough to preserve
                        # a comparison record without relying on real network.
                        await asyncio.sleep(0.02)
        except (TypeError, ValueError):
            pass

        index = 0

        async def replay_receive():
            nonlocal index
            if index < len(received):
                message = received[index]
                index += 1
                return message
            return await receive()

        await app(scope, replay_receive, send)

    def http_client_factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=counting_app),
            base_url="http://localhost:8000",
            headers=headers,
            timeout=timeout,
            auth=auth,
        )

    version_id = uuid.uuid4()
    definition = {
        "transport": "http",
        "url": "http://localhost:8000/mcp",
    }
    tool_name = f"project_{version_id.hex[:16]}_echo"
    key = _key(version_id, checksum="a" * 64, digest="b" * 64)

    async with app.router.lifespan_context(app):
        cache = McpRunSessionCache()
        reused_started = time.monotonic()
        reused_results = [
            await PrivateAgentRuntime._invoke_exact_mcp(
                version_id,
                definition,
                {},
                tool_name,
                {"value": str(index)},
                http_client_factory=http_client_factory,
                session_cache=cache,
                session_key=key,
            )
            for index in range(5)
        ]
        reused_elapsed = time.monotonic() - reused_started
        await cache.aclose()
        reused_methods = methods.copy()

        methods.clear()
        one_shot_started = time.monotonic()
        one_shot_results = [
            await PrivateAgentRuntime._invoke_exact_mcp(
                version_id,
                definition,
                {},
                tool_name,
                {"value": str(index)},
                http_client_factory=http_client_factory,
            )
            for index in range(5)
        ]
        one_shot_elapsed = time.monotonic() - one_shot_started

    assert [result[0]["text"] for result in reused_results] == [str(index) for index in range(5)]
    assert [result[0]["text"] for result in one_shot_results] == [str(index) for index in range(5)]
    assert reused_methods == Counter(
        {
            "initialize": 1,
            "notifications/initialized": 1,
            "tools/list": 1,
            "tools/call": 5,
        }
    )
    # The previous adapter-wrapper path initializes once to list tools and once
    # again to call the selected tool, for every call in the five-call sample.
    assert methods == Counter(
        {
            "initialize": 10,
            "notifications/initialized": 10,
            "tools/list": 10,
            "tools/call": 5,
        }
    )
    assert one_shot_elapsed > reused_elapsed + 0.10


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_new_run_materializes_superseded_exact_mcp_version_and_reuses_session(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin admission, PostgreSQL revalidation, proxy dispatch, and Run cleanup."""
    from sqlalchemy import text
    from support.private_thread_seed import TEST_MODEL_REF, seed_private_thread_database

    from app.private_work.asset_runtime import PrivateAgentRuntime, PrivateAssetRuntime
    from app.private_work.authorization import PrivateRunAuthorizationService
    from app.private_work.run_admission import PrivateRunAdmissionService
    from app.private_work.run_repository import PrivateRunCreate
    from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
    from app.shared_assets.agent_payload_checksum import agent_payload_checksum
    from app.shared_assets.mcp_secret_closure import lock_mcp_secret_closure
    from app.shared_assets.mcp_secret_store import McpSecretStore
    from app.shared_assets.mcp_service import (
        McpDefinition,
        McpSecretSlot,
        McpService,
    )
    from app.shared_assets.mcp_tool_inventory_repository import (
        McpToolInventoryRepository,
    )
    from app.shared_assets.models import AgentPayload
    from app.shared_assets.resolver import ProjectAssetResolver
    from deerflow.mcp_definition_policy import NetworkMcpEndpointPolicy
    from deerflow.persistence.shared_assets import McpSecretSlotRow
    from deerflow.secrets import SecretKey

    server = FastMCP(
        "admitted-run-session-probe",
        stateless_http=True,
        log_level="ERROR",
    )

    @server.tool()
    def echo(value: str) -> str:
        return value

    app = server.streamable_http_app()
    methods: Counter[str] = Counter()
    authorization_headers: list[bytes | None] = []

    async def counting_app(scope, receive, send) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        authorization_headers.append(dict(scope.get("headers", ())).get(b"authorization"))

        received: list[dict[str, Any]] = []
        body = b""
        while True:
            message = await receive()
            received.append(message)
            if message["type"] != "http.request":
                break
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        try:
            payload = json.loads(body)
            requests = payload if isinstance(payload, list) else [payload]
            for request in requests:
                method = request.get("method")
                if isinstance(method, str):
                    methods[method] += 1
                    if method == "initialize":
                        await asyncio.sleep(0.02)
        except (TypeError, ValueError):
            pass

        index = 0

        async def replay_receive():
            nonlocal index
            if index < len(received):
                message = received[index]
                index += 1
                return message
            return await receive()

        await app(scope, replay_receive, send)

    def http_client_factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=counting_app),
            base_url="http://127.0.0.1:8000",
            headers=headers,
            timeout=timeout,
            auth=auth,
        )

    endpoint = "http://127.0.0.1:8000/mcp"
    current_endpoint = "http://127.0.0.1:8001/mcp"
    authorization_value = "superseded-run-version-secret"
    mcp_definition = McpDefinition(
        description="admitted run probe",
        transport="http",
        url=endpoint,
        secret_slots=(
            McpSecretSlot(
                name="authorization",
                purpose="Authenticate the exact superseded Version",
                payload_schema={"headers": ("Authorization",)},
                required=True,
            ),
        ),
    )
    mcp_checksum = McpService._checksum(mcp_definition)
    current_mcp_checksum = McpService._checksum(
        McpDefinition(
            description="current replacement",
            transport="http",
            url=current_endpoint,
        )
    )
    endpoint_policy = NetworkMcpEndpointPolicy(("127.0.0.0/8",))
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    mcp_id = uuid.uuid4()
    mcp_version_id = uuid.uuid4()
    mcp_slot_id = uuid.uuid4()
    current_mcp_version_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    agent_version_id = uuid.uuid4()
    agent_checksum = agent_payload_checksum(
        AgentPayload(
            description="",
            soul="mcp run agent",
            model_ref=TEST_MODEL_REF,
            tool_groups=(),
            skill_refs=(),
            mcp_version_ids=(mcp_version_id,),
        )
    )
    try:
        async with seed.factory() as session, session.begin():
            await session.execute(
                text(
                    """INSERT INTO mcp_servers
                    (id,scope,project_id,slug,display_name,status,version,created_by_user_id)
                    VALUES (:id,'project',:project_id,:slug,'Admitted Run MCP','active',1,:owner)"""
                ),
                {
                    "id": mcp_id,
                    "project_id": seed.owner_a.project_id,
                    "slug": f"admitted-run-mcp-{mcp_id.hex[:12]}",
                    "owner": str(seed.owner_a.user_id),
                },
            )
            await session.execute(
                text(
                    """INSERT INTO mcp_server_versions
                    (id,mcp_server_id,version_number,workflow_status,description,
                     transport,url,payload_checksum,created_by_user_id)
                    VALUES (:id,:mcp_id,1,'published','admitted run probe',
                            'http',:url,:checksum,:owner)"""
                ),
                {
                    "id": mcp_version_id,
                    "mcp_id": mcp_id,
                    "url": endpoint,
                    "checksum": mcp_checksum,
                    "owner": str(seed.owner_a.user_id),
                },
            )
            await session.execute(
                text(
                    """SELECT set_config(
                        'deerflow.asset_version_assembly',
                        :version_id,
                        true
                    )"""
                ),
                {"version_id": str(mcp_version_id)},
            )
            slot = McpSecretSlotRow(
                id=mcp_slot_id,
                mcp_server_version_id=mcp_version_id,
                name="authorization",
                purpose="Authenticate the exact superseded Version",
                payload_schema={"headers": ["Authorization"]},
                required=True,
            )
            session.add(slot)
            await session.flush()
            await McpSecretStore(
                session,
                secret_key=SecretKey(b"m" * 32),
            ).replace(
                project_id=seed.owner_a.project_id,
                mcp_server_id=mcp_id,
                mcp_server_version_id=mcp_version_id,
                slots=(slot,),
                slot_name="authorization",
                payload={"headers": {"Authorization": authorization_value}},
                actor_user_id=str(seed.owner_a.user_id),
                request_id=seed.owner_a.request_id,
            )
            await session.execute(
                text(
                    """INSERT INTO mcp_server_versions
                    (id,mcp_server_id,version_number,workflow_status,description,
                     transport,url,supersedes_version_id,payload_checksum,
                     created_by_user_id)
                    VALUES (:id,:mcp_id,2,'published','current replacement',
                            'http',:url,:supersedes,:checksum,:owner)"""
                ),
                {
                    "id": current_mcp_version_id,
                    "mcp_id": mcp_id,
                    "url": current_endpoint,
                    "supersedes": mcp_version_id,
                    "checksum": current_mcp_checksum,
                    "owner": str(seed.owner_a.user_id),
                },
            )
            await session.execute(
                text("UPDATE mcp_servers SET current_published_version_id=:version_id WHERE id=:mcp_id"),
                {"version_id": current_mcp_version_id, "mcp_id": mcp_id},
            )
            await session.execute(
                text(
                    """INSERT INTO agents
                    (id,scope,project_id,slug,display_name,status,revision,created_by_user_id)
                    VALUES (:id,'project',:project_id,:slug,'MCP Run Agent','active',1,:owner)"""
                ),
                {
                    "id": agent_id,
                    "project_id": seed.owner_a.project_id,
                    "slug": f"mcp-run-agent-{agent_id.hex[:12]}",
                    "owner": str(seed.owner_a.user_id),
                },
            )
            await session.execute(
                text(
                    """INSERT INTO agent_versions
                    (id,agent_id,version_number,description,soul,
                     model_ref,tool_groups,payload_checksum,created_by_user_id)
                    VALUES (:id,:agent_id,1,'','mcp run agent',:model_ref,
                            '[]'::jsonb,:checksum,:owner)"""
                ),
                {
                    "id": agent_version_id,
                    "agent_id": agent_id,
                    "model_ref": TEST_MODEL_REF,
                    "checksum": agent_checksum,
                    "owner": str(seed.owner_a.user_id),
                },
            )
            await session.execute(
                text(
                    """SELECT set_config(
                        'deerflow.asset_version_assembly',
                        :version_id,
                        true
                    )"""
                ),
                {"version_id": str(agent_version_id)},
            )
            await session.execute(
                text(
                    """INSERT INTO agent_version_mcp_refs
                    (agent_version_id,mcp_server_version_id,sort_order)
                    VALUES (:agent_version_id,:mcp_version_id,0)"""
                ),
                {
                    "agent_version_id": agent_version_id,
                    "mcp_version_id": mcp_version_id,
                },
            )
            await session.execute(
                text("UPDATE agents SET current_version_id=:version_id WHERE id=:agent_id"),
                {"version_id": agent_version_id, "agent_id": agent_id},
            )
            closure = await lock_mcp_secret_closure(
                session,
                project_id=seed.owner_a.project_id,
                mcp_server_id=mcp_id,
                mcp_server_version_id=mcp_version_id,
                slots=(slot,),
                request_id=seed.owner_a.request_id,
            )
            assert len(closure.materials) == 1
            closure_material = closure.materials[0]
            await McpToolInventoryRepository(session).record_success(
                project_id=seed.owner_a.project_id,
                mcp_server_id=mcp_id,
                mcp_server_version_id=mcp_version_id,
                payload_checksum=mcp_checksum,
                secret_digest=closure.digest,
                tools=({"name": "echo", "description": ""},),
            )

        original_is_active = PrivateRunAuthorizationService.is_active
        original_materialize_call = PrivateAgentRuntime._materialize_mcp_call
        revalidation_calls = 0
        materialize_calls = 0

        async def counted_is_active(session, **kwargs):
            nonlocal revalidation_calls
            revalidation_calls += 1
            return await original_is_active(session, **kwargs)

        async def counted_materialize_call(self, snapshot):
            nonlocal materialize_calls
            materialize_calls += 1
            return await original_materialize_call(self, snapshot)

        monkeypatch.setattr(
            PrivateRunAuthorizationService,
            "is_active",
            staticmethod(counted_is_active),
        )
        monkeypatch.setattr(
            PrivateAgentRuntime,
            "_materialize_mcp_call",
            counted_materialize_call,
        )

        elapsed_by_reuse: dict[bool, float] = {}
        methods_by_reuse: dict[bool, Counter[str]] = {}
        async with app.router.lifespan_context(app):
            for reuse in (True, False):
                thread_id = f"mcp-run-session-{reuse}-{uuid.uuid4()}"
                async with seed.factory() as session, session.begin():
                    await PrivateThreadRepository(session).create(
                        scope=seed.owner_a_scope,
                        thread_id=thread_id,
                        agent=ThreadAgentRef(agent_id, "project"),
                    )
                admitted = await PrivateRunAdmissionService(
                    seed.factory,
                    endpoint_policy=endpoint_policy,
                ).admit(
                    seed.owner_a,
                    thread_id,
                    PrivateRunCreate(run_id=f"mcp-run-{reuse}-{uuid.uuid4()}"),
                )
                mcp_assets = tuple(asset for asset in admitted.snapshot.assets if asset.asset_kind == "mcp")
                assert len(mcp_assets) == 1
                assert mcp_assets[0].version_id == mcp_version_id
                assert mcp_assets[0].payload_checksum == mcp_checksum
                assert all(asset.version_id != current_mcp_version_id for asset in admitted.snapshot.assets)
                assert len(admitted.snapshot.mcp_secrets) == 1
                persisted_secret = admitted.snapshot.mcp_secrets[0]
                assert persisted_secret.mcp_server_id == mcp_id
                assert persisted_secret.mcp_server_version_id == mcp_version_id
                assert persisted_secret.slot_id == mcp_slot_id
                assert persisted_secret.secret_generation_id == closure_material.generation_id
                assert persisted_secret.secret_generation_digest == closure_material.generation_digest

                methods.clear()
                authorization_headers.clear()
                resolver = ProjectAssetResolver(
                    seed.factory,
                    secret_key=SecretKey(b"m" * 32),
                )
                runtime = await PrivateAssetRuntime(
                    seed.factory,
                    resolver=resolver,
                    endpoint_policy=endpoint_policy,
                    http_client_factory=http_client_factory,
                    run_session_reuse=reuse,
                ).materialize(seed.owner_a, admitted)
                assert len(runtime.mcp_tools) == 1
                assert len(runtime.safe_manifest.mcps) == 1
                assert runtime.safe_manifest.mcps[0].version_id == mcp_version_id
                assert runtime.safe_manifest.mcps[0].definition["url"] == endpoint
                materialize_baseline = materialize_calls
                revalidation_baseline = revalidation_calls
                started = time.monotonic()
                results = [await runtime.mcp_tools[0].ainvoke({"value": str(index)}) for index in range(5)]
                elapsed_by_reuse[reuse] = time.monotonic() - started
                methods_by_reuse[reuse] = methods.copy()

                assert [result[0]["text"] for result in results] == [str(index) for index in range(5)]
                assert authorization_headers
                assert set(authorization_headers) == {authorization_value.encode("utf-8")}
                assert materialize_calls - materialize_baseline == 5
                # Every proxy call revalidates before materialization, inside
                # the locked materialization transaction, and before dispatch.
                # One-shot mode performs one additional check while opening its
                # per-call transport; reuse performs that check during discovery.
                assert revalidation_calls - revalidation_baseline == (15 if reuse else 20)
                cache = runtime._mcp_run_sessions
                if reuse:
                    assert cache is not None
                    assert cache.active_session_count == 1
                else:
                    assert cache is None
                await runtime.aclose()
                assert runtime._closed is True
                if cache is not None:
                    assert cache.active_session_count == 0

        assert methods_by_reuse[True] == Counter(
            {
                "initialize": 1,
                "notifications/initialized": 1,
                "tools/list": 1,
                "tools/call": 5,
            }
        )
        assert methods_by_reuse[False] == Counter(
            {
                "initialize": 11,
                "notifications/initialized": 11,
                "tools/list": 11,
                "tools/call": 5,
            }
        )
        assert elapsed_by_reuse[False] > elapsed_by_reuse[True] + 0.15
    finally:
        await seed.engine.dispose()


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
    monkeypatch.setattr(
        PrivateAgentRuntime,
        "_open_reused_project_mcp_session",
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


def test_session_cache_key_is_limited_to_project_http_and_sse() -> None:
    from app.private_work.asset_runtime import PrivateAgentRuntime
    from app.shared_assets.models import AssetKind, AssetScope, ResolvedMcpSnapshot

    def snapshot(scope: AssetScope, transport: str) -> ResolvedMcpSnapshot:
        return ResolvedMcpSnapshot(
            kind=AssetKind.MCP,
            scope=scope,
            asset_id=uuid.uuid4(),
            version_id=uuid.uuid4(),
            checksum=f"{scope.value}-{transport}",
            catalog_generation=1,
            dependency_version_ids=(),
            definition={"transport": transport},
            secret_generation_ids=(),
            secret_digest="a" * 64,
        )

    project_http = snapshot(AssetScope.PROJECT, "http")
    project_sse = snapshot(AssetScope.PROJECT, "sse")
    project_stdio = snapshot(AssetScope.PROJECT, "stdio")
    system_http = snapshot(AssetScope.SYSTEM, "http")
    runtime = object.__new__(PrivateAgentRuntime)
    runtime._mcp_run_sessions = McpRunSessionCache()
    runtime._mcp_snapshots = (
        project_http,
        project_sse,
        project_stdio,
        system_http,
    )

    assert runtime._mcp_session_key(project_http.version_id) is not None
    assert runtime._mcp_session_key(project_sse.version_id) is not None
    assert runtime._mcp_session_key(project_stdio.version_id) is None
    assert runtime._mcp_session_key(system_http.version_id) is None


def test_the_reuse_toggle_defaults_on_and_propagates() -> None:
    from app.private_work.asset_runtime import PrivateAssetRuntime
    from deerflow.config.mcp_security_config import McpSecurityConfig

    assert McpSecurityConfig().run_session_reuse is True
    assert PrivateAssetRuntime(MagicMock())._run_session_reuse is True
    assert PrivateAssetRuntime(MagicMock(), run_session_reuse=False)._run_session_reuse is False
