from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from support.m4_private_threads import OpenProjectCutoverGuard

from app.private_work.errors import PrivateWorkCutover


class _RejectingLegacyCutoverGuard:
    async def require_legacy_open(self) -> None:
        raise PrivateWorkCutover("req-legacy-system-asset")


def _legacy_run_body() -> SimpleNamespace:
    return SimpleNamespace(
        assistant_id="lead_agent",
        input={"messages": [{"role": "user", "content": "hello"}]},
        command=None,
        metadata={},
        config=None,
        context={},
        checkpoint_id=None,
        checkpoint=None,
        on_disconnect="cancel",
        multitask_strategy="reject",
        stream_mode=None,
        stream_subgraphs=False,
        interrupt_before=None,
        interrupt_after=None,
    )


@pytest.mark.anyio
async def test_legacy_start_rejects_project_agent_before_checkpoint_or_run_launch(
    monkeypatch,
) -> None:
    from fastapi import HTTPException

    from app.gateway import services

    thread_store = SimpleNamespace(
        get=AsyncMock(
            return_value={
                "thread_id": "project-thread",
                "agent_scope": "project",
                "agent_asset_id": str(uuid.uuid4()),
            }
        ),
        update_status=AsyncMock(),
    )
    manager = SimpleNamespace(create_or_reject=AsyncMock())
    checkpoint = AsyncMock()
    launch = MagicMock()
    monkeypatch.setattr(services, "get_stream_bridge", lambda _request: object())
    monkeypatch.setattr(services, "get_run_manager", lambda _request: manager)
    monkeypatch.setattr(
        services,
        "get_run_context",
        lambda _request: SimpleNamespace(thread_store=thread_store),
    )
    monkeypatch.setattr(services, "apply_checkpoint_to_run_config", checkpoint)
    monkeypatch.setattr(services, "_launch_registered_run", launch)
    monkeypatch.setattr(
        services,
        "resolve_trusted_internal_owner_for_attribution",
        AsyncMock(return_value=None),
    )
    request = SimpleNamespace(
        state=SimpleNamespace(user=None, auth_source=None),
        headers={},
        app=SimpleNamespace(
            state=SimpleNamespace(
                private_work_cutover_guard=_RejectingLegacyCutoverGuard(),
            )
        ),
    )

    with pytest.raises(HTTPException) as captured:
        await services.start_run(_legacy_run_body(), "project-thread", request)

    assert captured.value.status_code == 409
    assert captured.value.detail["code"] == "PRIVATE_WORK_CUTOVER"
    checkpoint.assert_not_awaited()
    manager.create_or_reject.assert_not_awaited()
    launch.assert_not_called()


@pytest.mark.anyio
async def test_legacy_start_preserves_system_agent_runtime(
    monkeypatch,
) -> None:
    from app.gateway import services

    thread_store = SimpleNamespace(
        get=AsyncMock(
            return_value={
                "thread_id": "system-thread",
                "agent_scope": "system",
                "agent_asset_id": str(uuid.uuid4()),
            }
        ),
        update_status=AsyncMock(),
    )
    record = SimpleNamespace(run_id=str(uuid.uuid4()))
    manager = SimpleNamespace(create_or_reject=AsyncMock(return_value=record))
    checkpoint = AsyncMock()
    launch = MagicMock()
    monkeypatch.setattr(services, "get_stream_bridge", lambda _request: object())
    monkeypatch.setattr(services, "get_run_manager", lambda _request: manager)
    monkeypatch.setattr(
        services,
        "get_run_context",
        lambda _request: SimpleNamespace(thread_store=thread_store),
    )
    monkeypatch.setattr(services, "apply_checkpoint_to_run_config", checkpoint)
    monkeypatch.setattr(services, "_launch_registered_run", launch)
    monkeypatch.setattr(
        services,
        "resolve_trusted_internal_owner_for_attribution",
        AsyncMock(return_value=None),
    )
    request = SimpleNamespace(
        state=SimpleNamespace(user=None, auth_source=None),
        headers={},
        app=SimpleNamespace(
            state=SimpleNamespace(
                private_work_cutover_guard=OpenProjectCutoverGuard(),
            )
        ),
    )

    result = await services.start_run(_legacy_run_body(), "system-thread", request)

    assert result is record
    checkpoint.assert_awaited_once()
    manager.create_or_reject.assert_awaited_once()
    launch.assert_called_once()


def test_private_lead_agent_uses_only_run_exact_assets_without_global_caches(
    monkeypatch,
    tmp_path,
) -> None:
    from langchain_core.tools import StructuredTool

    import deerflow.tools as tools_module
    from deerflow.agents.lead_agent import agent as lead_agent_module
    from deerflow.agents.lead_agent.prompt import _get_cached_skills_prompt_section
    from deerflow.config.acp_config import ACPAgentConfig
    from deerflow.config.app_config import AppConfig
    from deerflow.config.model_config import ModelConfig
    from deerflow.config.sandbox_config import SandboxConfig
    from deerflow.config.skill_evolution_config import SkillEvolutionConfig
    from deerflow.mcp import cache
    from deerflow.skills.parser import parse_skill_file
    from deerflow.skills.types import SkillCategory

    skill_root = tmp_path / "custom" / "exact"
    skill_root.mkdir(parents=True)
    skill_file = skill_root / "SKILL.md"
    skill_file.write_text(
        "---\nname: exact-private\ndescription: exact private skill\n---\nbody\n",
        encoding="utf-8",
    )
    skill = parse_skill_file(
        skill_file,
        SkillCategory.CUSTOM,
        skill_root.relative_to(tmp_path / "custom"),
    )
    assert skill is not None
    mcp_tool = StructuredTool.from_function(
        func=lambda value: value,
        name="exact_private_mcp",
        description="exact private MCP tool",
    )
    runtime = SimpleNamespace(
        model_ref="exact-model",
        tool_groups=("exact-group",),
        skills=(skill,),
        skill_root=tmp_path,
        mcp_tools=(mcp_tool,),
        soul="exact private soul",
    )
    app_config = AppConfig(
        models=[
            ModelConfig(
                name="exact-model",
                display_name="Exact",
                use="langchain_openai:ChatOpenAI",
                model="exact-model",
                supports_thinking=False,
                supports_vision=False,
            )
        ],
        sandbox=SandboxConfig(use="deerflow.sandbox.local:LocalSandboxProvider"),
        skill_evolution=SkillEvolutionConfig(enabled=True),
        acp_agents={
            "global-acp": ACPAgentConfig(
                command="global-acp",
                description="global optional ACP agent",
            )
        },
    )
    available_calls: list[dict[str, object]] = []
    middleware_calls: list[dict[str, object]] = []
    builtin_tool = StructuredTool.from_function(
        func=lambda: "ok",
        name="builtin_exact_group",
        description="builtin",
    )
    skill_manage = StructuredTool.from_function(
        func=lambda: "mutated",
        name="skill_manage",
        description="global skill mutation",
    )
    invoke_acp = StructuredTool.from_function(
        func=lambda: "delegated",
        name="invoke_acp_agent",
        description="global ACP delegation",
    )

    def load_tools(**kwargs):
        available_calls.append(kwargs)
        loaded = [builtin_tool]
        if kwargs.get("include_skill_manage", True):
            loaded.append(skill_manage)
        if kwargs.get("include_acp", True):
            loaded.append(invoke_acp)
        return loaded

    monkeypatch.setattr(
        tools_module,
        "get_available_tools",
        load_tools,
    )
    monkeypatch.setattr(
        lead_agent_module,
        "load_agent_config",
        lambda *_args: (_ for _ in ()).throw(AssertionError("legacy agent loader")),
    )
    monkeypatch.setattr(lead_agent_module, "build_tracing_callbacks", lambda: [])
    monkeypatch.setattr(
        lead_agent_module,
        "create_chat_model",
        lambda **kwargs: ("model", kwargs),
    )
    monkeypatch.setattr(
        lead_agent_module,
        "build_middlewares",
        lambda *_args, **kwargs: middleware_calls.append(kwargs) or [],
    )
    monkeypatch.setattr(lead_agent_module, "create_agent", lambda **kwargs: kwargs)
    prompt_cache_before = _get_cached_skills_prompt_section.cache_info()
    mcp_cache_before = (cache._mcp_tools_cache, cache._cache_initialized, cache._config_mtime)

    result = lead_agent_module._make_lead_agent(
        {
            "configurable": {
                "agent_name": "forged-global-agent",
                "model_name": "forged-global-model",
                "is_bootstrap": True,
                "subagent_enabled": True,
                "is_plan_mode": True,
            }
        },
        app_config=app_config,
        private_runtime=runtime,
    )

    assert result["model"][1]["name"] == "exact-model"
    assert [tool.name for tool in result["tools"]] == [
        "builtin_exact_group",
        "exact_private_mcp",
    ]
    assert "exact private soul" in result["system_prompt"]
    assert "forged-global-agent" not in result["system_prompt"]
    assert available_calls == [
        {
            "model_name": "exact-model",
            "groups": ["exact-group"],
            "include_mcp": False,
            "include_skill_manage": False,
            "include_acp": False,
            "subagent_enabled": False,
            "app_config": app_config,
            "asset_context": None,
        }
    ]
    assert middleware_calls[0]["runtime_skills"] == (skill,)
    assert middleware_calls[0]["runtime_skills_root"] == tmp_path
    assert middleware_calls[0]["runtime_skills_container_path"] == "/mnt/skills"
    assert "/mnt/skills/custom/exact/SKILL.md" in result["system_prompt"]
    assert str(tmp_path) not in result["system_prompt"]
    assert _get_cached_skills_prompt_section.cache_info() == prompt_cache_before
    assert (cache._mcp_tools_cache, cache._cache_initialized, cache._config_mtime) == mcp_cache_before


@pytest.fixture(autouse=True)
def _clear_catalog_provider():
    from deerflow.assets.catalog import set_asset_catalog_provider

    set_asset_catalog_provider(None)
    yield
    set_asset_catalog_provider(None)


class _AgentProvider:
    def __init__(self, *, cutover: bool, snapshot=None, unavailable: bool = False) -> None:
        self.cutover = cutover
        self.snapshot = snapshot
        self.unavailable = unavailable

    async def is_cutover_enabled(self) -> bool:
        return self.cutover

    async def get_system_agent(self, slug: str):
        from deerflow.assets.catalog import AssetCatalogUnavailable

        if self.unavailable or self.snapshot is None or self.snapshot.slug != slug:
            raise AssetCatalogUnavailable("missing")
        return self.snapshot

    async def list_system_agents(self):
        return () if self.snapshot is None else (self.snapshot,)

    async def list_system_skills(self):
        return ()

    async def list_system_mcp(self):
        return ()

    def run_sync(self, operation: str, *args):
        if operation == "is_cutover_enabled":
            return self.cutover
        if operation == "get_system_agent":
            if self.unavailable or self.snapshot is None or self.snapshot.slug != args[0]:
                from deerflow.assets.catalog import AssetCatalogUnavailable

                raise AssetCatalogUnavailable("missing")
            return self.snapshot
        if operation == "list_system_agents":
            return () if self.snapshot is None else (self.snapshot,)
        if operation == "list_system_skills":
            return ()
        if operation == "list_system_mcp":
            return ()
        raise AssertionError(operation)


class _SkillProvider(_AgentProvider):
    def __init__(self, snapshot) -> None:
        super().__init__(cutover=True)
        self.skill_snapshot = snapshot

    async def list_system_skills(self):
        return (self.skill_snapshot,)

    def run_sync(self, operation: str, *args):
        if operation == "list_system_skills":
            return (self.skill_snapshot,)
        return super().run_sync(operation, *args)


class _McpProvider(_AgentProvider):
    def __init__(self, snapshot, *, materialized=None, events=None) -> None:
        super().__init__(cutover=True)
        self.mcp_snapshot = snapshot
        self.materialized = materialized or {}
        self.materialize_calls = 0
        self.events = events

    async def list_system_mcp(self):
        return () if self.mcp_snapshot is None else (self.mcp_snapshot,)

    async def materialize_mcp_secrets(self, context, snapshot):
        assert context is not None
        assert snapshot is self.mcp_snapshot
        self.materialize_calls += 1
        if self.events is not None:
            self.events.append("materialize")
        return self.materialized

    def run_sync(self, operation: str, *args):
        if operation == "list_system_mcp":
            return () if self.mcp_snapshot is None else (self.mcp_snapshot,)
        if operation == "materialize_mcp_secrets":
            context, snapshot = args
            assert context is not None
            assert snapshot is self.mcp_snapshot
            self.materialize_calls += 1
            if self.events is not None:
                self.events.append("materialize")
            return self.materialized
        return super().run_sync(operation, *args)


def _agent_snapshot(scope=None):
    from deerflow.assets.catalog import AssetCatalogAgentSnapshot, AssetCatalogScope

    return AssetCatalogAgentSnapshot(
        slug="catalog-agent",
        scope=scope or AssetCatalogScope.SYSTEM,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        generation=7,
        checksum="a" * 64,
        description="from postgres",
        soul="database soul",
        model_ref="database-model",
        tool_groups=("web",),
        skill_version_ids=(),
        mcp_version_ids=(),
        skill_slugs=("catalog-skill",),
    )


def _mcp_snapshot(*, credential_grant_ids=()):
    from deerflow.assets.catalog import AssetCatalogMcpSnapshot, AssetCatalogScope

    return AssetCatalogMcpSnapshot(
        slug="catalog-mcp",
        scope=AssetCatalogScope.SYSTEM,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        generation=7,
        checksum="c" * 64,
        definition={
            "description": "from database",
            "transport": "stdio",
            "command": "db-command",
            "args": ("--db",),
            "env": {"PUBLIC_VALUE": "database"},
            "headers": {},
            "oauth": {"token_url": "https://token.invalid"},
            "routing": {},
            "tool_overrides": {},
            "timeout_seconds": 30,
            "credential_slots": (),
        },
        credential_grant_ids=credential_grant_ids,
    )


@pytest.mark.asyncio
async def test_mcp_runtime_uses_database_definition_without_extensions_file() -> None:
    from deerflow.assets.catalog import set_asset_catalog_provider
    from deerflow.mcp.tools import get_mcp_tools

    provider = _McpProvider(_mcp_snapshot())
    set_asset_catalog_provider(provider)
    client = MagicMock()
    client.get_tools = AsyncMock(return_value=[])
    with (
        patch("deerflow.config.extensions_config.ExtensionsConfig.from_file", side_effect=AssertionError("file fallback")) as from_file,
        patch("langchain_mcp_adapters.client.MultiServerMCPClient", return_value=client) as client_type,
        patch("deerflow.mcp.tools.get_initial_oauth_headers", new_callable=AsyncMock, return_value={}),
        patch("deerflow.mcp.tools.build_oauth_tool_interceptor", return_value=None),
    ):
        await get_mcp_tools()

    from_file.assert_not_called()
    servers = client_type.call_args.args[0]
    assert servers == {
        "catalog-mcp": {
            "transport": "stdio",
            "command": "db-command",
            "args": ["--db"],
            "env": {"PUBLIC_VALUE": "database"},
        }
    }
    assert provider.materialize_calls == 0


@pytest.mark.asyncio
async def test_mcp_runtime_materializes_secret_once_only_for_client_construction() -> None:
    from deerflow.assets.catalog import set_asset_catalog_provider
    from deerflow.mcp import cache
    from deerflow.mcp.client import build_servers_config
    from deerflow.mcp.tools import get_mcp_tools

    cache.reset_mcp_tools_cache()
    secret = "one-construction-only"
    snapshot = _mcp_snapshot(credential_grant_ids=(uuid.uuid4(),))
    snapshot = replace(
        snapshot,
        definition={
            **snapshot.definition,
            "transport": "http",
            "command": None,
            "args": (),
            "url": "https://mcp.invalid",
            "env": {},
            "oauth": {
                "token_url": "https://token.invalid",
                "client_id": "public-client",
            },
        },
    )
    events = []
    provider = _McpProvider(
        snapshot,
        materialized={
            "primary": {
                "headers": {"X-Secret": secret},
                "oauth": {"client_secret": secret},
            }
        },
        events=events,
    )
    set_asset_catalog_provider(provider)
    client = MagicMock()
    client.get_tools = AsyncMock(return_value=[])
    token_manager = MagicMock()
    token_manager.oauth_server_names.return_value = ["catalog-mcp"]

    async def authorization(_server_name):
        events.append("oauth")
        return "Bearer local-token"

    token_manager.get_authorization_header = AsyncMock(side_effect=authorization)
    context = object()
    with (
        patch("deerflow.config.extensions_config.ExtensionsConfig.from_file", side_effect=AssertionError("file fallback")) as from_file,
        patch(
            "langchain_mcp_adapters.client.MultiServerMCPClient",
            side_effect=lambda *args, **kwargs: events.append("client") or client,
        ) as client_type,
        patch("deerflow.mcp.tools.get_initial_oauth_headers", new_callable=AsyncMock, return_value={}) as oauth_headers,
        patch("deerflow.mcp.tools.build_oauth_tool_interceptor", return_value=None),
        patch("deerflow.mcp.tools.build_servers_config", wraps=build_servers_config) as server_builder,
        patch("deerflow.mcp.tools.OAuthTokenManager", return_value=token_manager) as token_manager_type,
    ):
        await get_mcp_tools(asset_context=context)

    assert provider.materialize_calls == 1
    assert events == ["materialize", "oauth", "client"]
    assert client_type.call_count == 1
    assert client_type.call_args.args[0]["catalog-mcp"]["headers"]["X-Secret"] == secret
    assert client_type.call_args.args[0]["catalog-mcp"]["headers"]["Authorization"] == "Bearer local-token"
    assert "oauth" not in client_type.call_args.args[0]["catalog-mcp"]
    oauth_headers.assert_not_awaited()
    assert secret not in repr(server_builder.call_args.args[0])
    assert token_manager_type.call_args.args[0]["catalog-mcp"].client_secret == secret
    from_file.assert_not_called()
    assert secret not in repr(snapshot)
    assert cache._mcp_tools_cache is None


@pytest.mark.asyncio
@pytest.mark.parametrize("catalog_state", ["empty", "project"])
async def test_mcp_runtime_fails_closed_for_empty_or_project_catalog(catalog_state: str) -> None:
    from deerflow.assets.catalog import AssetCatalogScope, AssetCatalogUnavailable, set_asset_catalog_provider
    from deerflow.mcp.tools import get_mcp_tools

    snapshot = None if catalog_state == "empty" else replace(_mcp_snapshot(), scope=AssetCatalogScope.PROJECT)
    set_asset_catalog_provider(_McpProvider(snapshot))

    with (
        patch("deerflow.config.extensions_config.ExtensionsConfig.from_file", side_effect=AssertionError("file fallback")) as from_file,
        pytest.raises(AssetCatalogUnavailable),
    ):
        await get_mcp_tools()

    from_file.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_runtime_catalog_lookup_stays_on_provider_owner_loop() -> None:
    from app.shared_assets.catalog_provider import PostgresAssetCatalogProvider
    from deerflow.assets.catalog import set_asset_catalog_provider
    from deerflow.mcp.tools import get_mcp_tools

    snapshot = _mcp_snapshot()
    provider = PostgresAssetCatalogProvider.for_test(
        generation=snapshot.generation,
        cutover=True,
        mcp=(snapshot,),
    )
    set_asset_catalog_provider(provider)
    owner_loop_id = id(asyncio.get_running_loop())
    client = MagicMock()
    client.get_tools = AsyncMock(return_value=[])
    with (
        patch("langchain_mcp_adapters.client.MultiServerMCPClient", return_value=client),
        patch("deerflow.mcp.tools.get_initial_oauth_headers", new_callable=AsyncMock, return_value={}),
        patch("deerflow.mcp.tools.build_oauth_tool_interceptor", return_value=None),
    ):
        await get_mcp_tools()

    assert provider.last_lookup_loop_id_for_test() == owner_loop_id


def test_available_tools_passes_asset_context_without_extensions_file(monkeypatch) -> None:
    from deerflow.tools import tools

    context = object()
    seen = []
    monkeypatch.setattr(tools, "is_host_bash_allowed", lambda _config: False)
    monkeypatch.setattr(
        "deerflow.config.extensions_config.ExtensionsConfig.from_file",
        lambda: (_ for _ in ()).throw(AssertionError("file read")),
    )
    monkeypatch.setattr(
        "deerflow.mcp.cache.get_cached_mcp_tools",
        lambda *, asset_context=None: seen.append(asset_context) or [],
    )
    config = SimpleNamespace(
        tools=[],
        models=[],
        skill_evolution=SimpleNamespace(enabled=False),
        acp_agents={},
        get_model_config=lambda _name: None,
    )

    tools.get_available_tools(app_config=config, asset_context=context)

    assert seen == [context]


@pytest.mark.asyncio
async def test_contextual_mcp_initialization_is_not_written_to_global_cache(monkeypatch) -> None:
    from deerflow.mcp import cache

    context = object()
    loader = AsyncMock(return_value=[])
    monkeypatch.setattr("deerflow.mcp.tools.get_mcp_tools", loader)
    cache.reset_mcp_tools_cache()

    assert await cache.initialize_mcp_tools(asset_context=context) == []
    loader.assert_awaited_once_with(asset_context=context)
    assert cache._mcp_tools_cache is None
    assert cache._cache_initialized is False


@pytest.mark.asyncio
async def test_cutover_mcp_cache_rechecks_provider_instead_of_reusing_global_tools(monkeypatch) -> None:
    from deerflow.assets.catalog import set_asset_catalog_provider
    from deerflow.mcp import cache

    set_asset_catalog_provider(_McpProvider(_mcp_snapshot()))
    loader = AsyncMock(return_value=[])
    monkeypatch.setattr("deerflow.mcp.tools.get_mcp_tools", loader)
    cache._mcp_tools_cache = [MagicMock(name="stale-tool")]
    cache._cache_initialized = True

    assert cache.get_cached_mcp_tools() == []
    assert cache.get_cached_mcp_tools() == []
    assert loader.await_count == 2
    cache.reset_mcp_tools_cache()


@pytest.mark.asyncio
async def test_agent_loader_keeps_file_before_cutover(monkeypatch, tmp_path: Path) -> None:
    from deerflow.assets.catalog import set_asset_catalog_provider
    from deerflow.config import agents_config

    agent_dir = tmp_path / "file-agent"
    agent_dir.mkdir()
    (agent_dir / "config.yaml").write_text("name: file-agent\nmodel: file-model\n", encoding="utf-8")
    monkeypatch.setattr(agents_config, "resolve_agent_dir", lambda *_args, **_kwargs: agent_dir)
    set_asset_catalog_provider(_AgentProvider(cutover=False, snapshot=_agent_snapshot()))

    assert agents_config.load_agent_config("file-agent").model == "file-model"


@pytest.mark.asyncio
async def test_agent_loader_uses_database_only_after_cutover(monkeypatch, tmp_path: Path) -> None:
    from deerflow.assets.catalog import set_asset_catalog_provider
    from deerflow.config import agents_config

    missing = tmp_path / "deleted-file-agent"
    monkeypatch.setattr(agents_config, "resolve_agent_dir", lambda *_args, **_kwargs: missing)
    set_asset_catalog_provider(_AgentProvider(cutover=True, snapshot=_agent_snapshot()))

    config = agents_config.load_agent_config("catalog-agent")
    assert config is not None
    assert config.model == "database-model"
    assert config.skills == ["catalog-skill"]
    assert agents_config.load_agent_soul("catalog-agent") == "database soul"


@pytest.mark.asyncio
async def test_agent_loader_never_falls_back_when_database_row_is_missing(monkeypatch, tmp_path: Path) -> None:
    from deerflow.assets.catalog import AssetCatalogUnavailable, set_asset_catalog_provider
    from deerflow.config import agents_config

    agent_dir = tmp_path / "catalog-agent"
    agent_dir.mkdir()
    (agent_dir / "config.yaml").write_text("name: catalog-agent\nmodel: forbidden-fallback\n", encoding="utf-8")
    monkeypatch.setattr(agents_config, "resolve_agent_dir", lambda *_args, **_kwargs: agent_dir)
    set_asset_catalog_provider(_AgentProvider(cutover=True, unavailable=True))

    with pytest.raises(AssetCatalogUnavailable):
        agents_config.load_agent_config("catalog-agent")


@pytest.mark.asyncio
async def test_agent_loader_rejects_project_snapshot_after_cutover() -> None:
    from deerflow.assets.catalog import AssetCatalogScope, AssetCatalogUnavailable, set_asset_catalog_provider
    from deerflow.config.agents_config import load_agent_config

    set_asset_catalog_provider(
        _AgentProvider(
            cutover=True,
            snapshot=_agent_snapshot(AssetCatalogScope.PROJECT),
        )
    )

    with pytest.raises(AssetCatalogUnavailable):
        load_agent_config("catalog-agent")


@pytest.mark.asyncio
async def test_skill_prompt_runtime_uses_database_snapshot_without_file_loader(monkeypatch, tmp_path: Path) -> None:
    from deerflow.agents.lead_agent import prompt
    from deerflow.assets.catalog import (
        AssetCatalogScope,
        AssetCatalogSkillFile,
        AssetCatalogSkillSnapshot,
        set_asset_catalog_provider,
    )
    from deerflow.config.skills_config import SkillsConfig

    snapshot = AssetCatalogSkillSnapshot(
        slug="catalog-skill",
        scope=AssetCatalogScope.SYSTEM,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        generation=4,
        checksum="b" * 64,
        description="database skill",
        files=(AssetCatalogSkillFile("SKILL.md", b"---\nname: catalog-skill\ndescription: database skill\n---\n"),),
    )
    set_asset_catalog_provider(_SkillProvider(snapshot))
    monkeypatch.setattr(prompt, "get_or_new_skill_storage", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("file fallback")))
    monkeypatch.setattr(prompt, "get_or_new_user_skill_storage", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("file fallback")))

    app_config = SimpleNamespace(skills=SkillsConfig(path=str(tmp_path)))
    skills = prompt.get_enabled_skills_for_config(app_config, user_id="user")
    assert [(skill.name, skill.description) for skill in skills] == [("catalog-skill", "database skill")]


@pytest.mark.asyncio
async def test_skill_prompt_runtime_rejects_project_snapshot(tmp_path: Path) -> None:
    from deerflow.agents.lead_agent.prompt import get_enabled_skills_for_config
    from deerflow.assets.catalog import (
        AssetCatalogScope,
        AssetCatalogSkillFile,
        AssetCatalogSkillSnapshot,
        AssetCatalogUnavailable,
        set_asset_catalog_provider,
    )
    from deerflow.config.skills_config import SkillsConfig

    snapshot = AssetCatalogSkillSnapshot(
        slug="project-skill",
        scope=AssetCatalogScope.PROJECT,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        generation=4,
        checksum="b" * 64,
        description="project",
        files=(AssetCatalogSkillFile("SKILL.md", b"project"),),
    )
    set_asset_catalog_provider(_SkillProvider(snapshot))
    app_config = SimpleNamespace(skills=SkillsConfig(path=str(tmp_path)))

    with pytest.raises(AssetCatalogUnavailable):
        get_enabled_skills_for_config(app_config, user_id="user")


@pytest.mark.asyncio
async def test_gateway_worker_builds_sync_agent_factory_off_event_loop() -> None:
    from deerflow.runtime.runs.worker import _call_agent_factory_off_loop

    event_loop_thread = threading.get_ident()

    def factory(*, config):
        return threading.get_ident(), config

    factory_thread, config = await _call_agent_factory_off_loop(factory, {"configurable": {}}, None)
    assert factory_thread != event_loop_thread
    assert config == {"configurable": {}}


@pytest.mark.asyncio
async def test_slash_skill_reads_materialized_database_content_after_legacy_file_delete(tmp_path: Path) -> None:
    from deerflow.agents.middlewares.skill_activation_middleware import SkillActivationMiddleware
    from deerflow.assets.catalog import (
        AssetCatalogScope,
        AssetCatalogSkillFile,
        AssetCatalogSkillSnapshot,
        set_asset_catalog_provider,
    )
    from deerflow.config.skills_config import SkillsConfig

    legacy_dir = tmp_path / "public" / "catalog-skill"
    legacy_dir.mkdir(parents=True)
    legacy_file = legacy_dir / "SKILL.md"
    legacy_file.write_text("legacy file content", encoding="utf-8")
    legacy_file.unlink()
    database_content = "---\nname: catalog-skill\ndescription: db\n---\ndatabase content"
    snapshot = AssetCatalogSkillSnapshot(
        slug="catalog-skill",
        scope=AssetCatalogScope.SYSTEM,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        generation=9,
        checksum="d" * 64,
        description="db",
        files=(AssetCatalogSkillFile("SKILL.md", database_content.encode()),),
    )
    set_asset_catalog_provider(_SkillProvider(snapshot))
    app_config = SimpleNamespace(skills=SkillsConfig(path=str(tmp_path)))

    resolution = SkillActivationMiddleware(app_config=app_config)._resolve_activation("/catalog-skill do it")

    assert resolution is not None
    assert resolution.activation is not None
    assert resolution.activation.skill_content == database_content
    assert ".asset-catalog/9/catalog-skill/SKILL.md" in resolution.activation.container_file_path
    assert (tmp_path / "custom" / ".asset-catalog" / "9" / "catalog-skill" / "SKILL.md").is_file()
    assert not (tmp_path / "public" / ".asset-catalog").exists()


@pytest.mark.asyncio
async def test_slash_skill_fails_closed_when_database_catalog_is_empty(tmp_path: Path) -> None:
    from deerflow.agents.middlewares.skill_activation_middleware import SkillActivationMiddleware
    from deerflow.assets.catalog import AssetCatalogUnavailable, set_asset_catalog_provider
    from deerflow.config.skills_config import SkillsConfig

    stale = tmp_path / "custom" / ".asset-catalog" / "8" / "catalog-skill"
    stale.mkdir(parents=True)
    (stale / "SKILL.md").write_text("stale database cache", encoding="utf-8")
    set_asset_catalog_provider(_SkillProvider(None))
    app_config = SimpleNamespace(skills=SkillsConfig(path=str(tmp_path)))

    with pytest.raises(AssetCatalogUnavailable):
        SkillActivationMiddleware(app_config=app_config)._resolve_activation("/catalog-skill do it")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("slug", "file_path"),
    [
        ("../escape", "SKILL.md"),
        ("..\\escape", "SKILL.md"),
        ("catalog-skill", "../SKILL.md"),
        ("catalog-skill", "C:\\escape\\SKILL.md"),
        ("catalog-skill", "/escape/SKILL.md"),
    ],
)
async def test_skill_materializer_rejects_unsafe_slug_and_paths(
    tmp_path: Path,
    slug: str,
    file_path: str,
) -> None:
    from deerflow.assets.catalog import (
        AssetCatalogScope,
        AssetCatalogSkillFile,
        AssetCatalogSkillSnapshot,
        AssetCatalogUnavailable,
        set_asset_catalog_provider,
    )
    from deerflow.config.skills_config import SkillsConfig
    from deerflow.skills.storage import get_catalog_skills_if_cutover

    snapshot = AssetCatalogSkillSnapshot(
        slug="catalog-skill",
        scope=AssetCatalogScope.SYSTEM,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        generation=9,
        checksum="d" * 64,
        description="db",
        files=(AssetCatalogSkillFile("SKILL.md", b"database"),),
    )
    snapshot = replace(snapshot, slug=slug, files=(AssetCatalogSkillFile(file_path, b"database"),))
    set_asset_catalog_provider(_SkillProvider(snapshot))

    with pytest.raises(AssetCatalogUnavailable):
        get_catalog_skills_if_cutover(SimpleNamespace(skills=SkillsConfig(path=str(tmp_path))))

    assert not (tmp_path / "escape").exists()


@pytest.mark.asyncio
async def test_skill_materializer_rejects_managed_root_symlink(tmp_path: Path) -> None:
    from deerflow.assets.catalog import (
        AssetCatalogScope,
        AssetCatalogSkillFile,
        AssetCatalogSkillSnapshot,
        AssetCatalogUnavailable,
        set_asset_catalog_provider,
    )
    from deerflow.config.skills_config import SkillsConfig
    from deerflow.skills.storage import get_catalog_skills_if_cutover

    snapshot = AssetCatalogSkillSnapshot(
        slug="catalog-skill",
        scope=AssetCatalogScope.SYSTEM,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        generation=9,
        checksum="d" * 64,
        description="db",
        files=(AssetCatalogSkillFile("SKILL.md", b"database"),),
    )
    set_asset_catalog_provider(_SkillProvider(snapshot))
    outside = tmp_path / "outside"
    outside.mkdir()
    custom = tmp_path / "custom"
    custom.mkdir()
    (custom / ".asset-catalog").symlink_to(outside, target_is_directory=True)

    with pytest.raises(AssetCatalogUnavailable):
        get_catalog_skills_if_cutover(SimpleNamespace(skills=SkillsConfig(path=str(tmp_path))))

    assert not any(outside.iterdir())


@pytest.mark.asyncio
async def test_gateway_lifespan_installs_and_finally_clears_catalog_provider() -> None:
    from app.gateway.app import _asset_catalog_provider_lifespan
    from app.shared_assets.catalog_provider import PostgresAssetCatalogProvider
    from deerflow.assets.catalog import get_asset_catalog_provider

    def session_factory():
        return None

    async with _asset_catalog_provider_lifespan(session_factory):
        assert isinstance(get_asset_catalog_provider(), PostgresAssetCatalogProvider)
    assert get_asset_catalog_provider() is None


@pytest.mark.asyncio
async def test_gateway_keeps_catalog_provider_installed_through_runtime_drain() -> None:
    from contextlib import asynccontextmanager

    from fastapi import FastAPI

    from app.gateway import app as gateway_app
    from app.shared_assets.catalog_provider import PostgresAssetCatalogProvider
    from deerflow.assets.catalog import get_asset_catalog_provider

    events: list[str] = []

    @asynccontextmanager
    async def draining_runtime(_app, _startup_config):
        assert isinstance(get_asset_catalog_provider(), PostgresAssetCatalogProvider)
        events.append("runtime_enter")
        try:
            yield
        finally:
            events.append("runtime_drain")
            assert isinstance(get_asset_catalog_provider(), PostgresAssetCatalogProvider)

    with (
        patch.object(gateway_app, "langgraph_runtime", draining_runtime),
        patch(
            "deerflow.persistence.engine.get_session_factory",
            side_effect=AssertionError("session factory resolved before runtime initialization"),
        ),
    ):
        async with gateway_app._gateway_runtime_lifespan(FastAPI(), object()):
            events.append("body")

    assert events == ["runtime_enter", "body", "runtime_drain"]
    assert get_asset_catalog_provider() is None


@pytest.mark.asyncio
async def test_cutover_runtime_rejects_project_assets() -> None:
    from deerflow.assets.catalog import (
        AssetCatalogAgentSnapshot,
        AssetCatalogScope,
        AssetCatalogUnavailable,
        require_system_asset,
    )

    snapshot = AssetCatalogAgentSnapshot(
        slug="project-agent",
        scope=AssetCatalogScope.PROJECT,
        version_id="00000000-0000-0000-0000-000000000001",
        generation=1,
        description="",
        soul="",
        model_ref="default",
        tool_groups=(),
        skill_version_ids=(),
        mcp_version_ids=(),
    )

    with pytest.raises(AssetCatalogUnavailable):
        require_system_asset(snapshot)


def test_legacy_mutation_cutover_response_is_stable() -> None:
    from app.gateway.routers.asset_catalog_compat import cutover_conflict

    error = cutover_conflict()
    assert error.status_code == 409
    assert error.detail == {
        "code": "ASSET_CATALOG_CUTOVER",
        "message": "System assets are managed through /admin/assets after catalog cutover.",
    }


@pytest.mark.asyncio
async def test_agents_compat_gets_await_provider_without_file_access(monkeypatch) -> None:
    from app.gateway.routers import agents
    from deerflow.assets.catalog import set_asset_catalog_provider

    set_asset_catalog_provider(_AgentProvider(cutover=True, snapshot=_agent_snapshot()))
    monkeypatch.setattr(agents, "_require_agents_api_enabled", lambda: None)
    monkeypatch.setattr(agents, "list_custom_agents", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("file read")))
    monkeypatch.setattr(agents, "load_agent_config", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("file read")))
    monkeypatch.setattr(agents, "get_paths", lambda: (_ for _ in ()).throw(AssertionError("file check")))

    listed = await agents.list_agents()
    detail = await agents.get_agent("catalog-agent")
    availability = await agents.check_agent_name("catalog-agent")

    assert [item.name for item in listed.agents] == ["catalog-agent"]
    assert detail.soul == "database soul"
    assert availability == {"available": False, "name": "catalog-agent", "managed_at": "/admin/assets"}


@pytest.mark.asyncio
async def test_agent_mutations_reject_before_file_access(monkeypatch) -> None:
    from app.gateway.routers import agents
    from app.gateway.routers.asset_catalog_compat import CUTOVER_DETAIL
    from deerflow.assets.catalog import set_asset_catalog_provider

    set_asset_catalog_provider(_AgentProvider(cutover=True, snapshot=_agent_snapshot()))
    monkeypatch.setattr(agents, "_require_agents_api_enabled", lambda: None)
    monkeypatch.setattr(agents, "get_paths", lambda: (_ for _ in ()).throw(AssertionError("file access")))

    calls = (
        agents.create_agent_endpoint(agents.AgentCreateRequest(name="new-agent")),
        agents.update_agent("catalog-agent", agents.AgentUpdateRequest(description="changed")),
        agents.delete_agent("catalog-agent"),
    )
    for call in calls:
        with pytest.raises(Exception) as raised:
            await call
        assert getattr(raised.value, "status_code", None) == 409
        assert getattr(raised.value, "detail", None) == CUTOVER_DETAIL


@pytest.mark.asyncio
async def test_skills_compat_gets_provider_bytes_and_hides_custom(monkeypatch, tmp_path: Path) -> None:
    from app.gateway.routers import skills
    from deerflow.assets.catalog import (
        AssetCatalogScope,
        AssetCatalogSkillFile,
        AssetCatalogSkillSnapshot,
        set_asset_catalog_provider,
    )
    from deerflow.config.skills_config import SkillsConfig

    content = b"---\nname: catalog-skill\ndescription: database skill\n---\ndb body"
    snapshot = AssetCatalogSkillSnapshot(
        slug="catalog-skill",
        scope=AssetCatalogScope.SYSTEM,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        generation=4,
        checksum="b" * 64,
        description="database skill",
        files=(AssetCatalogSkillFile("SKILL.md", content, "text/markdown"),),
    )
    set_asset_catalog_provider(_SkillProvider(snapshot))
    monkeypatch.setattr(skills, "_get_user_skill_storage", lambda *_args: (_ for _ in ()).throw(AssertionError("file read")))
    monkeypatch.setattr(skills, "require_admin_user", AsyncMock())
    config = SimpleNamespace(skills=SkillsConfig(path=str(tmp_path)))
    request = MagicMock()

    listed = await skills.list_skills(config)
    detail = await skills.get_skill("catalog-skill", config)
    rendered = await skills.get_skill_content("catalog-skill", request, config)
    custom = await skills.list_custom_skills(config)

    assert [item.name for item in listed.skills] == ["catalog-skill"]
    assert detail.category.value == "public"
    assert rendered.content == content.decode()
    assert custom.skills == []
    for read in (
        skills.get_custom_skill("catalog-skill", request, config),
        skills.get_custom_skill_history("catalog-skill", request, config),
    ):
        with pytest.raises(Exception) as raised:
            await read
        assert getattr(raised.value, "status_code", None) == 404


@pytest.mark.asyncio
async def test_skill_mutations_reject_before_file_or_extensions_access(monkeypatch, tmp_path: Path) -> None:
    from app.gateway.routers import skills
    from app.gateway.routers.asset_catalog_compat import CUTOVER_DETAIL
    from deerflow.assets.catalog import set_asset_catalog_provider
    from deerflow.config.skills_config import SkillsConfig

    set_asset_catalog_provider(_SkillProvider(None))
    monkeypatch.setattr(skills, "require_admin_user", AsyncMock())
    monkeypatch.setattr(skills, "_get_user_skill_storage", lambda *_args: (_ for _ in ()).throw(AssertionError("file access")))
    monkeypatch.setattr(skills.ExtensionsConfig, "resolve_config_path", lambda: (_ for _ in ()).throw(AssertionError("config access")))
    config = SimpleNamespace(skills=SkillsConfig(path=str(tmp_path)))
    request = MagicMock()
    calls = (
        skills.install_skill(request, skills.SkillInstallRequest(thread_id="t", path="/x"), config),
        skills.update_custom_skill("x", skills.CustomSkillUpdateRequest(content="x"), request, config),
        skills.delete_custom_skill("x", request, config),
        skills.rollback_custom_skill("x", skills.SkillRollbackRequest(), request, config),
        skills.update_skill("x", skills.SkillUpdateRequest(enabled=False), request, config),
    )
    for call in calls:
        with pytest.raises(Exception) as raised:
            await call
        assert getattr(raised.value, "status_code", None) == 409
        assert getattr(raised.value, "detail", None) == CUTOVER_DETAIL


@pytest.mark.asyncio
async def test_mcp_compat_get_uses_safe_provider_and_put_rejects_before_config(monkeypatch) -> None:
    from app.gateway.routers import mcp
    from app.gateway.routers.asset_catalog_compat import CUTOVER_DETAIL
    from deerflow.assets.catalog import set_asset_catalog_provider

    set_asset_catalog_provider(_McpProvider(_mcp_snapshot()))
    monkeypatch.setattr(mcp, "require_admin_user", AsyncMock())
    monkeypatch.setattr(mcp, "get_extensions_config", lambda: (_ for _ in ()).throw(AssertionError("config read")))
    monkeypatch.setattr(mcp.ExtensionsConfig, "resolve_config_path", lambda: (_ for _ in ()).throw(AssertionError("config write")))
    request = MagicMock()

    response = await mcp.get_mcp_configuration(request)
    assert response.mcp_servers["catalog-mcp"].command == "db-command"
    assert response.mcp_servers["catalog-mcp"].env == {"PUBLIC_VALUE": "***"}
    with pytest.raises(Exception) as raised:
        await mcp.update_mcp_configuration(request, mcp.McpConfigUpdateRequest(mcp_servers={}))
    assert getattr(raised.value, "status_code", None) == 409
    assert getattr(raised.value, "detail", None) == CUTOVER_DETAIL


def test_setup_agent_tool_rejects_cutover_before_file_access(monkeypatch) -> None:
    from deerflow.assets.catalog import AssetCatalogUnavailable, set_asset_catalog_provider
    from deerflow.tools.builtins import setup_agent_tool

    set_asset_catalog_provider(_AgentProvider(cutover=True))
    monkeypatch.setattr(
        setup_agent_tool,
        "get_paths",
        lambda: (_ for _ in ()).throw(AssertionError("file access")),
    )
    runtime = SimpleNamespace(context={"agent_name": "test"}, tool_call_id="call-1")

    with pytest.raises(AssetCatalogUnavailable, match="ASSET_CATALOG_CUTOVER"):
        setup_agent_tool.setup_agent.func(soul="soul", description="description", runtime=runtime)


def test_update_agent_tool_rejects_cutover_before_config_or_file_access(monkeypatch) -> None:
    from deerflow.assets.catalog import AssetCatalogUnavailable, set_asset_catalog_provider
    from deerflow.tools.builtins import update_agent_tool

    set_asset_catalog_provider(_AgentProvider(cutover=True))
    monkeypatch.setattr(
        update_agent_tool,
        "get_app_config",
        lambda: (_ for _ in ()).throw(AssertionError("config access")),
    )
    monkeypatch.setattr(
        update_agent_tool,
        "get_paths",
        lambda: (_ for _ in ()).throw(AssertionError("file access")),
    )
    runtime = SimpleNamespace(
        context={"agent_name": "test", "user_id": "user-1"},
        tool_call_id="call-1",
    )

    with pytest.raises(AssetCatalogUnavailable, match="ASSET_CATALOG_CUTOVER"):
        update_agent_tool.update_agent.func(runtime=runtime, soul="new soul")


@pytest.mark.parametrize(
    "method,args,kwargs",
    [
        ("update_mcp_config", ({},), {}),
        ("update_skill", ("skill",), {"enabled": False}),
        ("install_skill", ("missing.skill",), {}),
    ],
)
def test_deerflow_client_mutations_reject_cutover_before_io(
    monkeypatch,
    method: str,
    args: tuple,
    kwargs: dict,
) -> None:
    import deerflow.client as client_module
    from deerflow.assets.catalog import AssetCatalogUnavailable, set_asset_catalog_provider
    from deerflow.client import DeerFlowClient

    set_asset_catalog_provider(_AgentProvider(cutover=True))
    monkeypatch.setattr(
        client_module.ExtensionsConfig,
        "resolve_config_path",
        lambda: (_ for _ in ()).throw(AssertionError("config access")),
    )
    monkeypatch.setattr(
        client_module,
        "get_or_new_user_skill_storage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("storage access")),
    )
    client = DeerFlowClient.__new__(DeerFlowClient)

    with pytest.raises(AssetCatalogUnavailable, match="ASSET_CATALOG_CUTOVER"):
        getattr(client, method)(*args, **kwargs)


@pytest.mark.asyncio
async def test_skill_manage_tool_rejects_cutover_before_storage_access(monkeypatch) -> None:
    from deerflow.assets.catalog import AssetCatalogUnavailable, set_asset_catalog_provider
    from deerflow.tools import skill_manage_tool

    set_asset_catalog_provider(_AgentProvider(cutover=True))
    monkeypatch.setattr(
        skill_manage_tool,
        "get_or_new_user_skill_storage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("storage access")),
    )
    runtime = SimpleNamespace(
        context={"user_id": "user-1", "thread_id": "thread-1"},
        tool_call_id="call-1",
    )

    with pytest.raises(AssetCatalogUnavailable, match="ASSET_CATALOG_CUTOVER"):
        await skill_manage_tool._skill_manage_impl(
            runtime=runtime,
            action="create",
            name="new-skill",
            content="---\nname: new-skill\ndescription: test\n---\n",
        )


@pytest.mark.parametrize("asset_context", [None, {"project_id": "forged"}])
def test_deerflow_client_requires_explicit_trusted_asset_context_when_provider_enabled(
    monkeypatch,
    asset_context,
) -> None:
    from deerflow.assets.catalog import AssetCatalogUnavailable, set_asset_catalog_provider
    from deerflow.client import DeerFlowClient

    set_asset_catalog_provider(_AgentProvider(cutover=True))
    monkeypatch.setattr("deerflow.client.get_app_config", lambda: MagicMock())
    available_tools = MagicMock(side_effect=AssertionError("tool loading must fail closed"))
    monkeypatch.setattr("deerflow.tools.get_available_tools", available_tools)

    with pytest.raises(AssetCatalogUnavailable, match="trusted asset context"):
        client = DeerFlowClient(asset_context=asset_context)
        client._get_tools(model_name=None, subagent_enabled=False)

    available_tools.assert_not_called()


def test_deerflow_client_forwards_explicit_opaque_asset_context(monkeypatch) -> None:
    from deerflow.assets.catalog import set_asset_catalog_provider
    from deerflow.client import DeerFlowClient

    context = object()
    set_asset_catalog_provider(_AgentProvider(cutover=True))
    monkeypatch.setattr("deerflow.client.get_app_config", lambda: MagicMock())
    available_tools = MagicMock(return_value=[])
    monkeypatch.setattr("deerflow.tools.get_available_tools", available_tools)

    client = DeerFlowClient(asset_context=context)

    assert client._get_tools(model_name="model", subagent_enabled=False) == []
    available_tools.assert_called_once_with(
        model_name="model",
        subagent_enabled=False,
        asset_context=context,
    )


def test_deerflow_client_contextless_tools_remain_compatible_before_cutover(monkeypatch) -> None:
    from deerflow.assets.catalog import set_asset_catalog_provider
    from deerflow.client import DeerFlowClient

    set_asset_catalog_provider(_AgentProvider(cutover=False))
    monkeypatch.setattr("deerflow.client.get_app_config", lambda: MagicMock())
    available_tools = MagicMock(return_value=[])
    monkeypatch.setattr("deerflow.tools.get_available_tools", available_tools)

    client = DeerFlowClient()

    assert client._get_tools(model_name=None, subagent_enabled=False) == []
    available_tools.assert_called_once_with(
        model_name=None,
        subagent_enabled=False,
        asset_context=None,
    )
