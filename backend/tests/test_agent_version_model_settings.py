from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.chat_models import BaseChatModel
from pydantic import Field, ValidationError

from app.private_work.asset_runtime import PrivateAgentManifest, PrivateAgentRuntime
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.agent_service import AgentService
from app.shared_assets.errors import AssetValidationFailed
from app.shared_assets.models import (
    AgentModelSettings,
    AgentPayload,
    AssetKind,
    AssetScope,
    ResolvedAgentSnapshot,
)
from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig
from deerflow.config.sandbox_config import SandboxConfig
from deerflow.persistence.shared_assets import AgentRow, AgentVersionRow


def _context() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=1,
        request_id="req-agent-model-settings",
    )


def _payload(settings: AgentModelSettings | None = None) -> AgentPayload:
    return AgentPayload(
        description="exact agent",
        soul="exact soul",
        model_ref="test-model",
        tool_groups=("task",),
        skill_version_ids=(),
        mcp_version_ids=(),
        agents_instructions="exact agents",
        identity="exact identity",
        user_context="exact user",
        model_settings=settings or AgentModelSettings(),
    )


def test_agent_model_settings_are_strict_and_bounded() -> None:
    settings = AgentModelSettings(
        temperature=0.2,
        max_tokens=12_000,
        thinking_enabled=False,
        reasoning_effort="high",
    )

    assert settings.model_dump(exclude_none=True) == {
        "temperature": 0.2,
        "max_tokens": 12_000,
        "thinking_enabled": False,
        "reasoning_effort": "high",
    }
    assert AgentModelSettings(temperature=-0.0).temperature == 0.0
    assert (
        json.dumps(
            AgentModelSettings(temperature=-0.0).model_dump(exclude_none=True),
        )
        == '{"temperature": 0.0}'
    )
    with pytest.raises(ValidationError):
        AgentModelSettings.model_validate({"top_p": 0.9})
    with pytest.raises(ValidationError):
        AgentModelSettings(temperature=2.1)
    with pytest.raises(ValidationError):
        AgentModelSettings(max_tokens=0)
    with pytest.raises(ValidationError):
        AgentModelSettings(thinking_enabled=0)
    with pytest.raises(ValidationError):
        AgentModelSettings(reasoning_effort="turbo")


def test_project_agent_builder_accepts_only_strict_optional_model_settings() -> None:
    from app.gateway.routers.project_agent_builder import (
        AgentDesignBlueprintRequest,
        AgentDesignBlueprintResponse,
    )

    raw = {
        "description": "exact agent",
        "model_ref": "test-model",
        "tool_groups": ["task"],
        "skill_version_ids": [],
        "mcp_version_ids": [],
        "agents_instructions": "exact agents",
        "soul": "exact soul",
        "identity": "exact identity",
        "user_context": "exact user",
    }
    legacy = AgentDesignBlueprintRequest.model_validate(raw)
    assert legacy.model_settings.is_empty
    accepted = AgentDesignBlueprintRequest.model_validate(
        {
            **raw,
            "model_settings": {
                "temperature": 0.2,
                "thinking_enabled": False,
            },
        }
    )
    assert accepted.model_settings.thinking_enabled is False
    with pytest.raises(ValidationError):
        AgentDesignBlueprintRequest.model_validate(
            {
                **raw,
                "model_settings": {"top_p": 0.9},
            }
        )

    response = AgentDesignBlueprintResponse(
        **{
            **raw,
            "tool_groups": ("task",),
            "skill_version_ids": (),
            "mcp_version_ids": (),
        },
        model_settings=AgentModelSettings(
            temperature=0.2,
            thinking_enabled=False,
        ),
    )
    assert response.model_dump(mode="json")["model_settings"] == {
        "temperature": 0.2,
        "thinking_enabled": False,
    }
    assert AgentModelSettings().model_dump(mode="json") == {}


def test_empty_model_settings_preserve_legacy_builder_request_checksum() -> None:
    from app.shared_assets.agent_design_service import (
        AgentDesignBlueprint,
        AgentDesignBlueprintTurn,
        AgentDesignService,
    )

    session_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
    blueprint = AgentDesignBlueprint(
        description="exact agent",
        model_ref="test-model",
        tool_groups=("task",),
        skill_version_ids=(),
        mcp_version_ids=(),
        agents_instructions="exact agents",
        soul="exact soul",
        identity="exact identity",
        user_context="exact user",
    )
    request = {
        "session_id": session_id,
        "expected_revision": 3,
        "input": AgentDesignBlueprintTurn(
            kind="blueprint_update",
            blueprint=blueprint,
        ),
    }
    legacy_document = {
        "expected_revision": 3,
        "input": {
            "blueprint": {
                "agents_instructions": "exact agents",
                "description": "exact agent",
                "identity": "exact identity",
                "mcp_version_ids": [],
                "model_ref": "test-model",
                "skill_version_ids": [],
                "soul": "exact soul",
                "tool_groups": ["task"],
                "user_context": "exact user",
            },
            "kind": "blueprint_update",
        },
        "session_id": str(session_id),
    }
    expected = (
        __import__("hashlib")
        .sha256(
            json.dumps(
                legacy_document,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        )
        .hexdigest()
    )

    assert AgentDesignService._request_checksum(request) == expected


def test_agent_payload_checksum_v3_includes_exact_model_settings_and_v2_is_legacy_stable() -> None:
    base = _payload()
    explicit_false = _payload(AgentModelSettings(thinking_enabled=False))
    explicit_true = _payload(AgentModelSettings(thinking_enabled=True))

    legacy_document = {
        "agents_instructions": base.agents_instructions,
        "description": base.description,
        "identity": base.identity,
        "mcp_version_ids": [],
        "model_ref": base.model_ref,
        "skill_version_ids": [],
        "soul": base.soul,
        "tool_groups": ["task"],
        "user_context": base.user_context,
    }
    expected_v2 = (
        __import__("hashlib")
        .sha256(
            json.dumps(
                legacy_document,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        )
        .hexdigest()
    )

    assert AgentService._payload_checksum(base, payload_schema_version=2) == expected_v2
    assert AgentService._payload_checksum(explicit_false, payload_schema_version=3) != AgentService._payload_checksum(
        explicit_true,
        payload_schema_version=3,
    )
    assert AgentService._payload_checksum(base, payload_schema_version=3) != expected_v2


def test_legacy_payload_schemas_reject_nonempty_model_settings() -> None:
    with pytest.raises(AssetValidationFailed):
        AgentService._validate_payload(
            _context(),
            _payload(AgentModelSettings(temperature=0.2)),
            payload_schema_version=2,
        )


def test_agent_service_revalidates_constructed_model_settings() -> None:
    malformed = AgentModelSettings.model_construct(max_tokens=0)
    with pytest.raises(AssetValidationFailed):
        AgentService._validate_payload(
            _context(),
            _payload(malformed),
            payload_schema_version=3,
        )


@pytest.mark.asyncio
async def test_agent_service_persists_model_settings_as_v3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.shared_assets import agent_service as service_module

    actor = _context()
    now = datetime.now(UTC)
    session = SimpleNamespace(flush=AsyncMock())
    asset = SimpleNamespace(
        id=uuid.uuid4(),
        scope="project",
        project_id=actor.project_id,
        slug="exact-agent",
        display_name="Exact Agent",
        status="active",
        current_published_version_id=None,
        version=1,
        source_key=None,
        created_by_user_id=str(actor.user_id),
        created_at=now,
        updated_at=now,
    )
    repository = SimpleNamespace(
        session=session,
        create_project_asset=AsyncMock(return_value=asset),
        resolve_project_skill_versions=AsyncMock(return_value=()),
        resolve_project_mcp_versions=AsyncMock(return_value=()),
        lock_skill_version_slugs=AsyncMock(return_value=()),
    )

    async def create_version(
        _actor,
        _asset_id,
        row,
        skill_ids,
        mcp_ids,
    ):
        row.id = uuid.uuid4()
        row.created_at = now
        return service_module.AgentVersionRecord(
            row,
            tuple(skill_ids),
            tuple(mcp_ids),
        )

    repository.create_project_version = AsyncMock(side_effect=create_version)
    monkeypatch.setattr(
        service_module,
        "AgentRepository",
        lambda _session: repository,
    )
    service = AgentService(
        lambda: session,
        governance_sink=SimpleNamespace(append_project=AsyncMock()),
    )
    settings = AgentModelSettings(
        temperature=0.2,
        max_tokens=12_000,
        thinking_enabled=False,
        reasoning_effort="high",
    )

    result = await service.create_project_from_design_in_session(
        session,
        actor,
        service_module.CreateAgent("exact-agent", "Exact Agent"),
        _payload(settings),
    )

    row = repository.create_project_version.await_args.args[2]
    assert row.payload_schema_version == 3
    assert row.model_settings == settings.model_dump(exclude_none=True)
    assert result.version.model_settings == settings
    assert row.payload_checksum == AgentService._payload_checksum(
        _payload(settings),
        payload_schema_version=3,
    )


@pytest.mark.asyncio
async def test_agent_snapshot_carries_exact_version_model_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.shared_assets import resolver as resolver_module

    context = _context()
    agent_id = uuid.uuid4()
    version_id = uuid.uuid4()
    asset = AgentRow(
        id=agent_id,
        scope="project",
        project_id=context.project_id,
        slug="exact-agent",
        display_name="Exact Agent",
        status="active",
        created_by_user_id=str(context.user_id),
    )
    version = AgentVersionRow(
        id=version_id,
        agent_id=agent_id,
        version_number=1,
        workflow_status="published",
        description="exact description",
        payload_schema_version=3,
        agents_instructions="exact agents instructions",
        soul="exact soul",
        identity="exact identity",
        user_context="exact user context",
        model_ref="test-model",
        model_settings={
            "temperature": 0.2,
            "max_tokens": 12_000,
            "thinking_enabled": False,
            "reasoning_effort": "high",
        },
        tool_groups=["task"],
        payload_checksum="a" * 64,
        created_by_user_id=str(context.user_id),
    )

    class EmptyResult:
        def scalars(self):
            return self

        def all(self):
            return []

    class Session:
        async def execute(self, _statement):
            return EmptyResult()

    resolver = resolver_module.ProjectAssetResolver(lambda: None)
    monkeypatch.setattr(
        resolver,
        "_lock_credential_closures",
        AsyncMock(return_value={}),
    )

    snapshot = await resolver._agent_snapshot(  # noqa: SLF001
        Session(),  # type: ignore[arg-type]
        context,
        resolver_module._ResolvedRecord(  # noqa: SLF001
            AssetScope.PROJECT,
            asset,
            version,
        ),
        3,
    )

    assert snapshot.payload.payload_schema_version == 3
    assert snapshot.payload.model_settings == AgentModelSettings(
        temperature=0.2,
        max_tokens=12_000,
        thinking_enabled=False,
        reasoning_effort="high",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload_schema_version", "has_model_settings"),
    [(1, False), (2, False), (3, False), (3, True)],
)
async def test_system_catalog_reads_and_verifies_all_agent_checksum_schemas(
    payload_schema_version: int,
    has_model_settings: bool,
) -> None:
    from app.shared_assets.catalog_provider import (
        PostgresAssetCatalogProvider,
    )

    settings = (
        AgentModelSettings(
            temperature=0.2,
            max_tokens=12_000,
            thinking_enabled=False,
            reasoning_effort="high",
        )
        if has_model_settings
        else AgentModelSettings()
    )
    payload = AgentPayload(
        **{
            **_payload(settings).__dict__,
            "payload_schema_version": payload_schema_version,
        }
    )
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()
    asset = AgentRow(
        id=asset_id,
        scope="system",
        project_id=None,
        slug="exact-system-agent",
        display_name="Exact System Agent",
        status="active",
        current_published_version_id=version_id,
        created_by_user_id="system",
    )
    version = AgentVersionRow(
        id=version_id,
        agent_id=asset_id,
        version_number=1,
        workflow_status="published",
        description=payload.description,
        payload_schema_version=payload_schema_version,
        agents_instructions=payload.agents_instructions,
        soul=payload.soul,
        identity=payload.identity,
        user_context=payload.user_context,
        model_ref=payload.model_ref,
        model_settings=settings.model_dump(exclude_none=True),
        tool_groups=list(payload.tool_groups),
        payload_checksum=AgentService._payload_checksum(
            payload,
            payload_schema_version=payload_schema_version,
        ),
        created_by_user_id="system",
    )

    class Result:
        def __init__(self, values):
            self.values = values

        def all(self):
            return self.values

        def scalars(self):
            return self

    class Session:
        def __init__(self):
            self.results = iter(
                (
                    [(asset, version)],
                    [],
                    [],
                    [],
                    [],
                )
            )

        async def execute(self, _statement):
            return Result(next(self.results))

    snapshots = await PostgresAssetCatalogProvider._load_agents(  # noqa: SLF001
        Session(),  # type: ignore[arg-type]
        7,
    )

    assert len(snapshots) == 1
    assert snapshots[0].payload_schema_version == payload_schema_version
    assert dict(snapshots[0].model_settings) == settings.model_dump(exclude_none=True)


def test_private_runtime_exposes_only_exact_snapshotted_model_settings(
    tmp_path: Path,
) -> None:
    settings = AgentModelSettings(
        temperature=0.2,
        max_tokens=12_000,
        thinking_enabled=False,
        reasoning_effort="high",
    )
    manifest = PrivateAgentManifest(
        agent_asset_id=uuid.uuid4(),
        agent_version_id=uuid.uuid4(),
        checksum="a" * 64,
        catalog_generation=1,
        description="",
        payload_schema_version=3,
        agents_instructions="",
        soul="",
        identity="",
        user_context="",
        model_ref="test-model",
        tool_groups=(),
        skills=(),
        mcps=(),
        model_settings=settings,
    )
    runtime = PrivateAgentRuntime(
        context=object(),  # type: ignore[arg-type]
        run_id="exact-run",
        resolver=object(),  # type: ignore[arg-type]
        session_factory=object(),  # type: ignore[arg-type]
        safe_manifest=manifest,
        skill_root=tmp_path,
        skills=(),
        mcp_snapshots=(),
        authorization_boundary=object(),
    )

    assert runtime.model_settings is settings
    assert "temperature" not in repr(runtime)
    assert "max_tokens" not in repr(runtime)


def test_exact_resolved_snapshot_builds_private_manifest_without_lookup() -> None:
    from app.private_work.asset_runtime import _private_agent_manifest

    settings = AgentModelSettings(
        temperature=0.2,
        max_tokens=12_000,
        thinking_enabled=False,
        reasoning_effort="high",
    )
    snapshot = ResolvedAgentSnapshot(
        kind=AssetKind.AGENT,
        scope=AssetScope.PROJECT,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        checksum="a" * 64,
        catalog_generation=9,
        dependency_version_ids=(),
        payload=_payload(settings),
    )

    manifest = _private_agent_manifest(snapshot, skills=(), mcps=())

    assert manifest.agent_version_id == snapshot.version_id
    assert manifest.checksum == snapshot.checksum
    assert manifest.model_settings is settings


def test_runtime_option_precedence_preserves_explicit_false() -> None:
    from deerflow.agents.lead_agent.agent import _resolve_runtime_option

    assert (
        _resolve_runtime_option(
            {"thinking_enabled": False},
            "thinking_enabled",
            True,
            True,
        )
        is False
    )
    assert (
        _resolve_runtime_option(
            {},
            "thinking_enabled",
            False,
            True,
        )
        is False
    )
    assert (
        _resolve_runtime_option(
            {},
            "thinking_enabled",
            None,
            True,
        )
        is True
    )


def test_exact_agent_runtime_model_settings_use_request_agent_global_precedence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import deerflow.tools as tools_module
    from deerflow.agents.lead_agent import agent as lead_agent_module

    app_config = AppConfig(
        models=[
            ModelConfig(
                name="test-model",
                display_name="Test",
                description=None,
                use="tests.fake:SamplingChatModel",
                model="provider-model",
                supports_thinking=True,
                supports_reasoning_effort=True,
                temperature=0.9,
                max_output_tokens=4096,
            )
        ],
        sandbox=SandboxConfig(
            use="deerflow.sandbox.local:LocalSandboxProvider",
        ),
    )
    private_runtime = SimpleNamespace(
        model_ref="test-model",
        model_settings=AgentModelSettings(
            temperature=0.2,
            max_tokens=12_000,
            thinking_enabled=True,
            reasoning_effort="high",
        ),
        soul="exact soul",
        tool_groups=(),
        skills=(),
        skill_root=tmp_path,
        mcp_tools=(),
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(tools_module, "get_available_tools", lambda **_kwargs: [])
    monkeypatch.setattr(
        lead_agent_module,
        "build_middlewares",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        lead_agent_module,
        "apply_prompt_template",
        lambda **_kwargs: "exact prompt",
    )
    monkeypatch.setattr(
        lead_agent_module,
        "build_tracing_callbacks",
        lambda: [],
    )

    def fake_model(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(lead_agent_module, "create_chat_model", fake_model)
    monkeypatch.setattr(
        lead_agent_module,
        "create_agent",
        lambda **kwargs: kwargs,
    )

    lead_agent_module._make_lead_agent(  # noqa: SLF001
        {
            "configurable": {
                "temperature": 0.4,
                "thinking_enabled": False,
                "reasoning_effort": "low",
            }
        },
        app_config=app_config,
        private_runtime=private_runtime,
    )

    assert captured["model_overrides"] == {
        "temperature": 0.4,
        "max_tokens": 12_000,
    }
    assert captured["thinking_enabled"] is False
    assert captured["reasoning_effort"] == "low"


def test_explicit_agent_runtime_setting_rejects_unsupported_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from deerflow.agents.lead_agent import agent as lead_agent_module

    app_config = AppConfig(
        models=[
            ModelConfig(
                name="test-model",
                display_name="Test",
                description=None,
                use="tests.fake:SamplingChatModel",
                model="provider-model",
                supports_thinking=False,
                supports_reasoning_effort=False,
            )
        ],
        sandbox=SandboxConfig(
            use="deerflow.sandbox.local:LocalSandboxProvider",
        ),
    )
    private_runtime = SimpleNamespace(
        model_ref="test-model",
        model_settings=AgentModelSettings(thinking_enabled=True),
        soul="exact soul",
        tool_groups=(),
        skills=(),
        skill_root=tmp_path,
        mcp_tools=(),
    )

    with pytest.raises(ValueError, match="does not support exact Agent thinking"):
        lead_agent_module._make_lead_agent(  # noqa: SLF001
            {},
            app_config=app_config,
            private_runtime=private_runtime,
        )


class _SamplingChatModel(BaseChatModel):
    temperature: float | None = None
    max_output_tokens: int | None = Field(default=None)
    captured_kwargs: dict[str, object] = {}

    def __init__(self, **kwargs):
        _SamplingChatModel.captured_kwargs = dict(kwargs)
        super().__init__(**kwargs)

    @property
    def _llm_type(self) -> str:
        return "sampling-test"

    def _generate(self, *args, **kwargs):  # type: ignore[override]
        raise NotImplementedError


def _app_config() -> AppConfig:
    return AppConfig(
        models=[
            ModelConfig(
                name="test-model",
                display_name="Test",
                description=None,
                use="tests.fake:SamplingChatModel",
                model="provider-model",
                temperature=0.9,
                max_output_tokens=4096,
            )
        ],
        sandbox=SandboxConfig(use="deerflow.sandbox.local:LocalSandboxProvider"),
    )


def test_model_factory_maps_and_merges_agent_sampling_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.models import factory as factory_module

    monkeypatch.setattr(
        factory_module,
        "resolve_class",
        lambda _path, _base: _SamplingChatModel,
    )
    monkeypatch.setattr(factory_module, "build_tracing_callbacks", lambda: [])
    _SamplingChatModel.captured_kwargs = {}

    factory_module.create_chat_model(
        name="test-model",
        app_config=_app_config(),
        model_overrides={"temperature": 0.2, "max_tokens": 12_000},
    )

    assert _SamplingChatModel.captured_kwargs["temperature"] == 0.2
    assert _SamplingChatModel.captured_kwargs["max_output_tokens"] == 12_000
    assert "max_tokens" not in _SamplingChatModel.captured_kwargs


def test_agent_sampling_overrides_win_after_global_thinking_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.models import factory as factory_module

    config = _app_config()
    config.models[0].supports_thinking = True
    config.models[0].when_thinking_enabled = {
        "temperature": 1.5,
        "max_output_tokens": 2048,
    }
    monkeypatch.setattr(
        factory_module,
        "resolve_class",
        lambda _path, _base: _SamplingChatModel,
    )
    monkeypatch.setattr(factory_module, "build_tracing_callbacks", lambda: [])
    _SamplingChatModel.captured_kwargs = {}

    factory_module.create_chat_model(
        name="test-model",
        app_config=config,
        thinking_enabled=True,
        model_overrides={"temperature": 0.2, "max_tokens": 12_000},
    )

    assert _SamplingChatModel.captured_kwargs["temperature"] == 0.2
    assert _SamplingChatModel.captured_kwargs["max_output_tokens"] == 12_000


def test_model_factory_rejects_unsupported_or_unknown_agent_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.models import factory as factory_module

    class NoSamplingChatModel(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "no-sampling-test"

        def _generate(self, *args, **kwargs):  # type: ignore[override]
            raise NotImplementedError

    monkeypatch.setattr(
        factory_module,
        "resolve_class",
        lambda _path, _base: NoSamplingChatModel,
    )
    monkeypatch.setattr(factory_module, "build_tracing_callbacks", lambda: [])

    with pytest.raises(ValueError, match="does not support Agent model setting"):
        factory_module.create_chat_model(
            name="test-model",
            app_config=_app_config(),
            model_overrides={"max_tokens": 100},
        )
    with pytest.raises(ValueError, match="Unsupported Agent model setting"):
        factory_module.create_chat_model(
            name="test-model",
            app_config=_app_config(),
            model_overrides={"top_p": 0.8},
        )


def test_codex_rejects_agent_max_tokens_instead_of_dropping_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.models import factory as factory_module
    from deerflow.models.openai_codex_provider import CodexChatModel

    monkeypatch.setattr(
        factory_module,
        "resolve_class",
        lambda _path, _base: CodexChatModel,
    )
    monkeypatch.setattr(factory_module, "build_tracing_callbacks", lambda: [])

    with pytest.raises(
        ValueError,
        match="does not support Agent model setting max_tokens",
    ):
        factory_module.create_chat_model(
            name="test-model",
            app_config=_app_config(),
            model_overrides={"max_tokens": 100},
        )


def test_full_schema_is_the_only_source_and_contains_strict_model_settings() -> None:
    schema = (Path(__file__).parents[1] / "packages" / "harness" / "deerflow" / "persistence" / "full_schema.sql").read_text(encoding="utf-8")

    assert "model_settings JSONB DEFAULT '{}'::jsonb NOT NULL" in schema
    assert "ck_agent_versions_model_settings" in schema
    assert "payload_schema_version IN (1, 2, 3)" in schema
    assert "payload_schema_version = 3" in schema
    assert "OR model_settings = '{}'::jsonb" in schema
    immutable_function = schema.split(
        "CREATE OR REPLACE FUNCTION prevent_shared_asset_version_payload_update()",
        1,
    )[1].split("$$ LANGUAGE plpgsql;", 1)[0]
    assert "'model_settings'" not in immutable_function
    assert "CREATE TRIGGER trg_agent_versions_immutable BEFORE UPDATE ON agent_versions" in schema
