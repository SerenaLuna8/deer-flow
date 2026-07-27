from __future__ import annotations

import dataclasses
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.private_work.errors import (
    PrivateWorkAssetStale,
    PrivateWorkUnavailable,
)
from app.private_work.runtime_context import prepare_private_run_config
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from deerflow.runtime import (
    RunContext,
    RunManager,
    RunStatus,
    run_agent,
)


class _OpaqueScope:
    def __repr__(self) -> str:
        return "<opaque-private-scope>"


def test_private_runtime_context_strips_client_authority_and_server_values_win() -> None:
    opaque = _OpaqueScope()
    config = prepare_private_run_config(
        thread_id="trusted-thread",
        opaque_scope=opaque,
        request_config={
            "thread_id": "forged-top-thread",
            "run_id": "forged-top-run",
            "sandbox_id": "forged-other-sandbox",
            "context": {
                "thread_id": "forged-context-thread",
                "run_id": "forged-context-run",
                "model_name": "forged-model",
                "is_bootstrap": True,
                "subagent_enabled": True,
                "is_plan_mode": True,
                "project_id": "attacker-project",
                "private_scope": {"owner_user_id": "attacker"},
                "deerflow_private_scope": {"project_id": "attacker"},
                "oauth_provider": "forged-provider",
                "oauth_id": "forged-oauth-id",
                "channel_user_id": "forged-channel-user",
                "auth_source": "forged-source",
                "channel_name": "forged-channel",
                "sandbox_id": "forged-context-sandbox",
                "authorization_checker": "forged-checker",
                "file_authority": "forged-file-authority",
                "private_agent_runtime": "forged-runtime",
                "asset_context": "forged-asset-context",
                "trusted_asset_context": "forged-trusted-asset-context",
            },
            "configurable": {
                "thread_id": "forged-configurable-thread",
                "run_id": "forged-configurable-run",
                "owner_user_id": "attacker",
                "__private_scope": {"project_id": "attacker"},
                "deerflow_private_scope": {"project_id": "attacker"},
                "sandbox_id": "forged-configurable-sandbox",
            },
        },
        metadata={"project_context": {"role": "admin"}, "safe": "value"},
        body_context={
            "capabilities": ["private_work.create"],
            "thinking_enabled": False,
            "disable_clarification": True,
            "thread_id": "forged-body-thread",
            "run_id": "forged-body-run",
        },
    )

    assert config["configurable"]["thread_id"] == "trusted-thread"
    assert "thread_id" not in config
    assert "run_id" not in config
    assert "run_id" not in config["configurable"]
    assert "run_id" not in config["context"]
    assert "private_scope" not in config["configurable"]
    assert "deerflow_private_scope" not in config["configurable"]
    assert config["context"]["private_scope"] is opaque
    assert "model_name" not in config["context"]
    assert "is_bootstrap" not in config["context"]
    assert "subagent_enabled" not in config["context"]
    assert "is_plan_mode" not in config["context"]
    assert "disable_clarification" not in config["context"]
    assert config["context"]["thinking_enabled"] is False
    assert config["metadata"] == {"safe": "value"}
    serialized = json.dumps(config, default=str)
    assert "forged-" not in serialized


def test_private_runtime_context_is_secret_and_marker_free_except_opaque_hook() -> None:
    sentinel = "task5-secret-sentinel"
    config = prepare_private_run_config(
        thread_id="trusted-thread",
        opaque_scope=_OpaqueScope(),
        request_config={
            "context": {
                "credential_envelope": {"ciphertext": sentinel},
                "checkpoint_scope_marker": sentinel,
                "api_token": sentinel,
            },
            "configurable": {"private_scope": sentinel, "key_id": sentinel},
        },
        metadata={"storage_locator": sentinel},
        body_context={"owner_user_id": sentinel},
    )

    serializable = {
        "configurable": config["configurable"],
        "metadata": config.get("metadata", {}),
        "context": {key: value for key, value in config["context"].items() if key != "private_scope"},
    }
    serialized = json.dumps(serializable, default=str).lower()
    assert sentinel not in serialized
    for forbidden in ("credential_envelope", "ciphertext", "key_id", "storage_locator", "checkpoint_scope_marker"):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(10_000_000, 1000, id="huge-clamped"),
        pytest.param(True, 100, id="bool-defaulted"),
        pytest.param(-1, 100, id="negative-defaulted"),
    ],
)
def test_private_runtime_context_clamps_client_recursion_limit(
    value,
    expected,
) -> None:
    config = prepare_private_run_config(
        thread_id="trusted-thread",
        opaque_scope=_OpaqueScope(),
        request_config={"recursion_limit": value},
        metadata=None,
        body_context=None,
    )

    assert config["recursion_limit"] == expected


def test_private_runtime_context_defaults_recursion_and_strips_internal_modes() -> None:
    config = prepare_private_run_config(
        thread_id="trusted-thread",
        opaque_scope=_OpaqueScope(),
        request_config={
            "non_interactive": True,
            "context": {"non_interactive": True},
            "configurable": {"non_interactive": True},
        },
        metadata={"nested": {"non_interactive": True}},
        body_context={"non_interactive": True},
    )

    assert config["recursion_limit"] == 100
    serialized = json.dumps(
        {key: value for key, value in config.items() if key != "context"} | {"context": {key: value for key, value in config["context"].items() if key != "private_scope"}},
        default=str,
    )
    assert "non_interactive" not in serialized


def test_worker_private_context_overwrites_forged_runtime_user_id() -> None:
    from deerflow.runtime.runs.worker import _install_runtime_context

    config = {"context": {"user_id": "forged-client-owner"}}
    _install_runtime_context(
        config,
        {
            "thread_id": "exact-thread",
            "run_id": "exact-run",
            "private_scope": object(),
            "user_id": "exact-admitted-owner",
        },
    )

    assert config["context"]["user_id"] == "exact-admitted-owner"
    assert config["context"]["thread_id"] == "exact-thread"
    assert config["context"]["run_id"] == "exact-run"


def test_worker_installs_private_prompt_skills_and_mcp_tools_as_internal_context_only() -> None:
    from deerflow.agents.lead_agent.prompt import AgentPromptBundle
    from deerflow.runtime.runs.worker import _install_runtime_context

    exact_bundle = AgentPromptBundle(
        payload_schema_version=2,
        agents_instructions="agents-context-sentinel",
        soul="soul-context-sentinel",
        identity="identity-context-sentinel",
        user_context="user-context-sentinel",
    )
    exact_skills = (object(),)
    exact_mcp_tools = (object(),)
    config = {
        "context": {
            "__agent_prompt_bundle": "forged-bundle",
            "__runtime_skills": ("forged-skill",),
            "__runtime_mcp_tools": ("forged-mcp-tool",),
        },
        "metadata": {"safe": "value"},
    }

    _install_runtime_context(
        config,
        {
            "thread_id": "exact-thread",
            "run_id": "exact-run",
            "private_scope": object(),
            "__agent_prompt_bundle": exact_bundle,
            "__runtime_skills": exact_skills,
            "__runtime_mcp_tools": exact_mcp_tools,
        },
    )

    assert config["context"]["__agent_prompt_bundle"] is exact_bundle
    assert config["context"]["__runtime_skills"] is exact_skills
    assert config["context"]["__runtime_mcp_tools"] is exact_mcp_tools
    assert config["metadata"] == {"safe": "value"}


@pytest.mark.anyio
async def test_worker_passes_private_runtime_to_supported_factory_off_loop() -> None:
    from deerflow.runtime.runs.worker import _call_agent_factory_off_loop

    private_runtime = object()
    calls: list[tuple[object, object]] = []

    def factory(*, config, private_runtime):
        calls.append((config, private_runtime))
        return "graph"

    assert (
        await _call_agent_factory_off_loop(
            factory,
            {"configurable": {}},
            None,
            private_runtime,
        )
        == "graph"
    )
    assert calls == [({"configurable": {}}, private_runtime)]


@pytest.mark.anyio
async def test_private_worker_preflights_mount_support_before_factory_and_cleans_runtime(
    tmp_path,
    caplog,
) -> None:
    from deerflow.runtime.private_scope import PrivateResourceScope
    from deerflow.runtime.user_context import (
        reset_current_user,
        reset_runtime_storage_user_id,
        set_current_user,
        set_runtime_storage_user_id,
    )
    from deerflow.sandbox.sandbox_provider import (
        SandboxProvider,
        reset_sandbox_provider,
        set_sandbox_provider,
    )

    run_manager = RunManager()
    record = await run_manager.create("private-thread")
    record.scope = PrivateResourceScope(
        project_id="exact-project",
        owner_user_id="exact-admitted-owner",
        membership_version=1,
    )
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )
    calls = 0

    class Runtime:
        closed = False
        skill_root = tmp_path

        async def aclose(self):
            self.closed = True
            raise OSError(f"cleanup failed at {tmp_path / 'private-host-path-sentinel'}")

    runtime = Runtime()

    class UnsupportedProvider(SandboxProvider):
        validated_users: list[str] = []
        released_users: list[str] = []

        def acquire(self, thread_id=None, *, user_id=None):
            del thread_id, user_id
            return "unsupported"

        def get(self, sandbox_id):
            del sandbox_id
            return None

        def release(self, sandbox_id):
            del sandbox_id

        def validate_run_scoped_mounts(
            self,
            thread_id,
            *,
            user_id,
            mounts,
        ):
            del thread_id
            assert mounts
            self.validated_users.append(user_id)
            return super().validate_run_scoped_mounts(
                "private-thread",
                user_id=user_id,
                mounts=mounts,
            )

        def release_run_scoped_mounts(self, thread_id, *, user_id, mounts):
            del thread_id
            assert mounts
            self.released_users.append(user_id)

    def legacy_factory(*, config):
        nonlocal calls
        del config
        calls += 1
        raise AssertionError("legacy factory must not run for private assets")

    provider = UnsupportedProvider()
    set_sandbox_provider(provider)
    owner_token = set_current_user(SimpleNamespace(id="forged-ambient-owner"))
    storage_token = set_runtime_storage_user_id("forged-ambient-storage")
    try:
        await run_agent(
            bridge,
            run_manager,
            record,
            ctx=RunContext(
                checkpointer=None,
                private_agent_runtime=runtime,
                app_config=SimpleNamespace(skills=SimpleNamespace(container_path="/mnt/skills")),
            ),
            agent_factory=legacy_factory,
            graph_input={},
            config={},
        )
    finally:
        reset_runtime_storage_user_id(storage_token)
        reset_current_user(owner_token)
        reset_sandbox_provider()

    assert calls == 0
    assert runtime.closed is True
    assert provider.validated_users == ["exact-admitted-owner"]
    assert provider.released_users == ["exact-admitted-owner"]
    assert record.status == RunStatus.error
    assert record.error == ("Configured sandbox provider does not support run-scoped read-only mounts")
    assert "private-host-path-sentinel" not in caplog.text
    bridge.publish_end.assert_awaited_once_with(record.run_id)


@pytest.mark.anyio
async def test_private_factory_never_falls_back_to_legacy_callable() -> None:
    from deerflow.runtime.runs.worker import (
        PrivateRuntimeFactoryUnavailable,
        _call_agent_factory_off_loop,
    )

    calls = 0

    def legacy_factory(*, config):
        nonlocal calls
        del config
        calls += 1

    with pytest.raises(PrivateRuntimeFactoryUnavailable):
        await _call_agent_factory_off_loop(
            legacy_factory,
            {"configurable": {}},
            None,
            SimpleNamespace(aclose=object()),
        )
    assert calls == 0


def test_exact_skill_prompt_cache_is_keyed_by_run_read_only_metadata(tmp_path) -> None:
    from deerflow.agents.lead_agent.prompt import (
        _get_cached_skills_prompt_section,
        apply_prompt_template,
    )
    from deerflow.skills.parser import parse_skill_file
    from deerflow.skills.types import SkillCategory

    skill_root = tmp_path / "custom" / "exact"
    skill_root.mkdir(parents=True)
    skill_file = skill_root / "SKILL.md"
    skill_file.write_text("---\nname: exact\ndescription: exact project skill\n---\nbody\n", encoding="utf-8")
    parsed = parse_skill_file(skill_file, SkillCategory.CUSTOM, skill_root.relative_to(tmp_path / "custom"))
    assert parsed is not None
    skill = dataclasses.replace(parsed, runtime_read_only=True)
    _get_cached_skills_prompt_section.cache_clear()
    before = _get_cached_skills_prompt_section.cache_info()

    prompt = apply_prompt_template(
        exact_soul="exact soul",
        exact_skills=(skill,),
        exact_skills_container_path=str(tmp_path),
        available_skills={"exact"},
    )

    after = _get_cached_skills_prompt_section.cache_info()
    assert after.currsize == before.currsize + 1
    assert after.misses == before.misses + 1
    apply_prompt_template(
        exact_soul="exact soul",
        exact_skills=(skill,),
        exact_skills_container_path=str(tmp_path),
        available_skills={"exact"},
    )
    assert _get_cached_skills_prompt_section.cache_info().hits == after.hits + 1
    assert "exact soul" in prompt
    assert "[run exact, read-only]" in prompt
    assert str(tmp_path / "custom" / "exact" / "SKILL.md") in prompt


def test_exact_runtime_slash_skill_is_read_only_and_never_uses_catalog_storage(
    monkeypatch,
    tmp_path,
) -> None:
    from deerflow.agents.middlewares.skill_activation_middleware import (
        SkillActivationMiddleware,
    )
    from deerflow.skills.parser import parse_skill_file
    from deerflow.skills.types import SkillCategory

    skill_root = tmp_path / "custom" / "exact"
    skill_root.mkdir(parents=True)
    skill_file = skill_root / "SKILL.md"
    skill_file.write_text(
        "---\nname: exact\ndescription: exact project skill\n---\nbody\n",
        encoding="utf-8",
    )
    parsed = parse_skill_file(
        skill_file,
        SkillCategory.CUSTOM,
        skill_root.relative_to(tmp_path / "custom"),
    )
    assert parsed is not None
    skill = dataclasses.replace(parsed, enabled=True, runtime_read_only=True)
    middleware = SkillActivationMiddleware(
        available_skills={"exact"},
        runtime_skills=(skill,),
        runtime_skills_root=tmp_path,
    )
    resolution = middleware._resolve_activation("/exact do the exact task")

    assert resolution is not None
    assert resolution.activation is not None
    assert resolution.activation.editable is False
    reminder = middleware._build_activation_reminder(resolution.activation)
    assert 'editable="false"' in reminder
    assert str(skill_file) in reminder


def test_exact_runtime_root_is_used_by_durable_skill_capture(tmp_path) -> None:
    from deerflow.agents.lead_agent.agent import build_middlewares
    from deerflow.agents.middlewares.durable_context_middleware import (
        DurableContextMiddleware,
    )
    from deerflow.config.app_config import AppConfig
    from deerflow.config.model_config import ModelConfig
    from deerflow.config.sandbox_config import SandboxConfig

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
    )

    middlewares = build_middlewares(
        {"configurable": {}},
        model_name="exact-model",
        app_config=app_config,
        runtime_skills=(),
        runtime_skills_root=tmp_path,
    )

    durable = next(middleware for middleware in middlewares if isinstance(middleware, DurableContextMiddleware))
    assert durable._skills_root == tmp_path.as_posix()


def test_local_run_scoped_mount_supports_configured_skills_root_and_masks_legacy(
    monkeypatch,
    tmp_path,
) -> None:
    from deerflow.sandbox.local import LocalSandboxProvider
    from deerflow.sandbox.local.local_sandbox import PathMapping
    from deerflow.sandbox.sandbox_provider import RunScopedReadOnlyMount

    configured_root = "/opt/deerflow/skills"
    monkeypatch.setattr(
        "deerflow.config.get_app_config",
        lambda: SimpleNamespace(skills=SimpleNamespace(container_path=configured_root)),
    )
    global_root = tmp_path / "global"
    exact_root = tmp_path / "exact"
    global_file = global_root / "custom" / "global-only" / "SKILL.md"
    exact_file = exact_root / "custom" / "exact-only" / "SKILL.md"
    global_file.parent.mkdir(parents=True)
    exact_file.parent.mkdir(parents=True)
    global_file.write_text("global", encoding="utf-8")
    exact_file.write_text("exact", encoding="utf-8")

    provider = LocalSandboxProvider()
    monkeypatch.setattr(
        provider,
        "_path_mappings",
        [
            PathMapping(
                container_path=f"{configured_root}/custom",
                local_path=str(global_root / "custom"),
                read_only=True,
            )
        ],
    )
    monkeypatch.setattr(
        provider,
        "_build_thread_path_mappings",
        lambda *_args, **_kwargs: [],
    )

    legacy_id = provider.acquire("thread", user_id="owner")
    legacy = provider.get(legacy_id)
    assert legacy is not None
    assert legacy.read_file(f"{configured_root}/custom/global-only/SKILL.md") == "global"

    exact_id = provider.acquire_with_mounts(
        "thread",
        user_id="owner",
        mounts=(
            RunScopedReadOnlyMount(
                run_id="run-exact",
                container_path=configured_root,
                host_path=str(exact_root),
            ),
        ),
    )
    exact = provider.get(exact_id)
    assert exact is not None
    assert exact.read_file(f"{configured_root}/custom/exact-only/SKILL.md") == "exact"
    with pytest.raises(OSError):
        exact.read_file(f"{configured_root}/custom/global-only/SKILL.md")
    with pytest.raises(OSError, match="Read-only file system"):
        exact.write_file(
            f"{configured_root}/custom/exact-only/SKILL.md",
            "forged",
        )
    assert provider.get(legacy_id) is legacy
    assert legacy.read_file(f"{configured_root}/custom/global-only/SKILL.md") == "global"
    provider.release_run_scoped_mounts(
        "thread",
        user_id="owner",
        mounts=(
            RunScopedReadOnlyMount(
                run_id="run-exact",
                container_path=configured_root,
                host_path=str(exact_root),
            ),
        ),
    )
    assert provider.get(exact_id) is None
    assert provider.get(legacy_id) is legacy


def test_exact_run_skill_read_and_list_never_touch_global_skill_state(
    monkeypatch,
    tmp_path,
) -> None:
    from deerflow.sandbox.local import LocalSandboxProvider
    from deerflow.sandbox.sandbox_provider import (
        RunScopedReadOnlyMount,
        reset_sandbox_provider,
        set_sandbox_provider,
    )
    from deerflow.sandbox.tools import ls_tool, read_file_tool

    exact_root = tmp_path / "exact"
    exact_skill = exact_root / "custom" / "asset-uuid" / "SKILL.md"
    exact_reference = exact_skill.parent / "references" / "detail.txt"
    exact_reference.parent.mkdir(parents=True)
    exact_skill.write_text("exact project content", encoding="utf-8")
    exact_reference.write_text("exact detail", encoding="utf-8")
    mount = RunScopedReadOnlyMount(
        run_id="run-exact",
        container_path="/mnt/skills",
        host_path=str(exact_root),
    )
    global_skills_root = tmp_path / "global-skills"
    global_skills_root.mkdir()
    config = SimpleNamespace(
        skills=SimpleNamespace(
            container_path="/mnt/skills",
            get_skills_path=lambda: global_skills_root,
        ),
        sandbox=SimpleNamespace(
            allow_host_bash=False,
            mounts=[],
            read_file_output_max_chars=50000,
            ls_output_max_chars=50000,
        ),
    )
    monkeypatch.setattr("deerflow.config.get_app_config", lambda: config)
    monkeypatch.setattr("deerflow.config.app_config.get_app_config", lambda: config)
    provider = LocalSandboxProvider()
    monkeypatch.setattr(
        provider,
        "_build_thread_path_mappings",
        lambda *_args, **_kwargs: [],
    )
    sandbox_id = provider.acquire_with_mounts(
        "thread-exact",
        user_id="owner-exact",
        mounts=(mount,),
    )
    runtime = SimpleNamespace(
        state={
            "sandbox": {"sandbox_id": sandbox_id},
            "thread_data": {
                "workspace_path": str(tmp_path / "workspace"),
                "uploads_path": str(tmp_path / "uploads"),
                "outputs_path": str(tmp_path / "outputs"),
            },
        },
        context={
            "thread_id": "thread-exact",
            "run_id": "run-exact",
            "user_id": "owner-exact",
            "__run_read_only_mounts": (mount,),
        },
        config={},
    )
    set_sandbox_provider(provider)
    try:
        assert (
            read_file_tool.func(
                runtime=runtime,
                description="read exact skill",
                path="/mnt/skills/custom/asset-uuid/SKILL.md",
            )
            == "exact project content"
        )
        listing = ls_tool.func(
            runtime=runtime,
            description="list exact skill",
            path="/mnt/skills/custom/asset-uuid",
        )
        assert "SKILL.md" in listing
        assert "references" in listing
    finally:
        reset_sandbox_provider()


def test_only_typed_matching_run_mount_marks_exact_skill_path() -> None:
    from deerflow.sandbox.sandbox_provider import RunScopedReadOnlyMount
    from deerflow.sandbox.tools import _is_trusted_run_scoped_skill_path

    path = "/mnt/skills/custom/asset-uuid/SKILL.md"
    mount = RunScopedReadOnlyMount(
        run_id="run-exact",
        container_path="/mnt/skills",
        host_path="/tmp/exact-skills",
    )
    trusted = SimpleNamespace(
        context={
            "run_id": "run-exact",
            "__run_read_only_mounts": (mount,),
        }
    )
    forged = SimpleNamespace(
        context={
            "run_id": "run-exact",
            "__run_read_only_mounts": (
                {
                    "run_id": "run-exact",
                    "container_path": "/mnt/skills",
                    "host_path": "/tmp/exact-skills",
                },
            ),
        }
    )
    wrong_run = SimpleNamespace(
        context={
            "run_id": "other-run",
            "__run_read_only_mounts": (mount,),
        }
    )

    assert _is_trusted_run_scoped_skill_path(trusted, path) is True
    assert _is_trusted_run_scoped_skill_path(forged, path) is False
    assert _is_trusted_run_scoped_skill_path(wrong_run, path) is False


@pytest.mark.anyio
async def test_private_worker_rejects_local_host_bash_before_model_factory(
    monkeypatch,
    tmp_path,
) -> None:
    from deerflow.config.app_config import AppConfig
    from deerflow.config.sandbox_config import SandboxConfig
    from deerflow.runtime.private_scope import PrivateResourceScope
    from deerflow.sandbox.local import LocalSandboxProvider
    from deerflow.sandbox.sandbox_provider import (
        reset_sandbox_provider,
        set_sandbox_provider,
    )

    app_config = AppConfig(
        sandbox=SandboxConfig(
            use="deerflow.sandbox.local:LocalSandboxProvider",
            allow_host_bash=True,
        )
    )
    monkeypatch.setattr("deerflow.config.get_app_config", lambda: app_config)
    run_manager = RunManager()
    record = await run_manager.create("private-thread")
    record.scope = PrivateResourceScope(
        project_id="exact-project",
        owner_user_id="exact-owner",
        membership_version=1,
    )
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )
    factory_calls = 0

    class Runtime:
        skill_root = tmp_path

        async def aclose(self):
            return None

    def model_factory(*_args, **_kwargs):
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("model factory must not run")

    provider = LocalSandboxProvider()
    set_sandbox_provider(provider)
    try:
        await run_agent(
            bridge,
            run_manager,
            record,
            ctx=RunContext(
                checkpointer=None,
                private_agent_runtime=Runtime(),
                app_config=app_config,
            ),
            agent_factory=model_factory,
            graph_input={},
            config={},
        )
    finally:
        reset_sandbox_provider()

    assert factory_calls == 0
    assert record.status == RunStatus.error
    assert record.error == ("Local private runtime cannot enforce read-only mounts when host bash is enabled")


@pytest.mark.parametrize(
    ("material_value", "remote_result"),
    [
        pytest.param(734921, {"content": [{"value": 734921}]}, id="numeric"),
        pytest.param(True, {"artifact": [False, {"value": True}]}, id="boolean"),
    ],
)
def test_mcp_result_scan_rejects_nested_json_scalar_credential_echo(
    material_value,
    remote_result,
) -> None:
    from app.private_work.asset_runtime import PrivateAgentRuntime

    with pytest.raises(PrivateWorkUnavailable) as captured:
        PrivateAgentRuntime._assert_mcp_result_secret_free(
            remote_result,
            {"slot": {"oauth": {"sentinel": material_value}}},
        )

    assert str(captured.value) == "Private work is unavailable."


def test_mcp_secret_scan_does_not_match_short_values_as_substrings() -> None:
    from app.private_work.asset_runtime import PrivateAgentRuntime

    assert not PrivateAgentRuntime._scalar_contains_secret(
        "search",
        ("a",),
    )
    assert PrivateAgentRuntime._scalar_contains_secret(
        "a",
        ("a",),
    )


@pytest.mark.anyio
async def test_mcp_discovery_scans_mixed_type_schema_without_type_errors(
    monkeypatch,
) -> None:
    from pydantic import BaseModel, Field

    from app.private_work.asset_runtime import PrivateAgentRuntime

    class SafeArgs(BaseModel):
        value: str

    class NumericLeakArgs(BaseModel):
        value: int = Field(default=734921)

    remote = SimpleNamespace(
        name="safe_tool",
        description="safe description",
        args_schema=SafeArgs,
    )

    async def one_shot(_version_id, _definition, _material, operation):
        return await operation((remote,))

    monkeypatch.setattr(
        PrivateAgentRuntime,
        "_with_one_shot_mcp_tools",
        staticmethod(one_shot),
    )
    material = {"slot": {"oauth": {"count": 734921, "blob": b"binary-sentinel"}}}
    discovered = await PrivateAgentRuntime._discover_exact_mcp(
        uuid.uuid4(),
        {},
        material,
    )
    assert [tool.name for tool in discovered] == ["safe_tool"]

    remote.args_schema = NumericLeakArgs
    with pytest.raises(PrivateWorkAssetStale):
        await PrivateAgentRuntime._discover_exact_mcp(uuid.uuid4(), {}, material)

    remote.args_schema = SafeArgs
    remote.description = "echoed binary-sentinel"
    with pytest.raises(PrivateWorkAssetStale):
        await PrivateAgentRuntime._discover_exact_mcp(uuid.uuid4(), {}, material)


def _private_context() -> object:
    from app.private_work.context import PrivateWorkContext

    return PrivateWorkContext.from_project(
        ProjectContext(
            user_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            membership_id=uuid.uuid4(),
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id="req-private-start",
        )
    )


def _private_body(*, checkpoint_id=None, assistant_id=None):
    return SimpleNamespace(
        assistant_id=assistant_id,
        input={"messages": [{"role": "user", "content": "hello"}]},
        command=None,
        metadata={"safe": "value"},
        config={"context": {"project_id": "forged"}},
        context={"thinking_enabled": False},
        checkpoint_id=checkpoint_id,
        checkpoint=None,
        on_disconnect="cancel",
        multitask_strategy="reject",
        stream_mode=["values"],
        stream_subgraphs=False,
        interrupt_before=None,
        interrupt_after=None,
    )


@pytest.mark.anyio
async def test_worker_executor_derives_runtime_identities_from_admitted_owner() -> None:
    from datetime import UTC, datetime

    from app.private_work.run_admission import PersistedRunSnapshot
    from app.private_work.run_repository import PrivateRunRecord
    from app.reliability.execution import PrivateRunExecution, RunAgentPrivateExecutor
    from app.reliability.jobs import JobClaim, JobScope
    from deerflow.runtime.user_context import (
        get_current_user,
        get_runtime_storage_user_id,
        reset_current_user,
        reset_runtime_storage_user_id,
        set_current_user,
        set_runtime_storage_user_id,
    )

    context = _private_context()
    owner_user_id = str(context.user_id)
    now = datetime.now(UTC)
    job_id = uuid.uuid4()
    run = PrivateRunRecord(
        run_id=str(uuid.uuid4()),
        thread_id="private-thread",
        project_id=context.project_id,
        owner_user_id=owner_user_id,
        assistant_id=None,
        status="pending",
        multitask_strategy="reject",
        metadata={},
        kwargs={},
        error=None,
        model_name="test-model",
        created_at=now,
        updated_at=now,
        job_id=job_id,
    )
    execution = PrivateRunExecution(
        context=context,
        run=run,
        snapshot=PersistedRunSnapshot(
            assets=(),
            mcp_grants=(),
            catalog_generation=1,
        ),
        checkpoint_namespace=run.run_id,
        graph_input={},
        command=None,
        config={},
        interrupt_before=None,
        interrupt_after=None,
        stream_mode=[],
        stream_subgraphs=False,
    )
    claim = JobClaim(
        job_id=job_id,
        attempt_id=uuid.uuid4(),
        lease_token="worker-lease",
        job_type="private_run",
        scope=JobScope(
            project_id=context.project_id,
            owner_user_id=owner_user_id,
        ),
        run_id=run.run_id,
        occurrence_id=None,
        retry_safety="safe",
        cancel_requested=False,
    )
    captured: list[tuple[str | None, str | None, object]] = []

    async def runner(_bridge, run_manager, record, *, ctx, **_kwargs):
        current = get_current_user()
        captured.append(
            (
                str(current.id) if current is not None else None,
                get_runtime_storage_user_id(),
                ctx.private_scope,
            )
        )
        await run_manager.set_status(record.run_id, RunStatus.success)

    class AssetRuntime:
        async def materialize(self, passed_context, admitted, *, authorization_boundary):
            assert passed_context is context
            assert admitted.opaque_runtime_scope == context.resource_scope
            assert authorization_boundary.execution_job_id == job_id

            async def aclose() -> None:
                return None

            return SimpleNamespace(
                model_ref="test-model",
                skill_root=None,
                aclose=aclose,
            )

    class ProjectCheckpointer:
        def for_context(self, passed_context):
            assert passed_context is context
            return SimpleNamespace(set_authorization_boundary=lambda _boundary: None)

    class Authority:
        cancel_requested = False

        def __init__(self) -> None:
            self.claim = claim

        def bind_cancel_callback(self, callback) -> None:
            self.cancel_callback = callback

    executor = RunAgentPrivateExecutor(
        object(),
        app_config=SimpleNamespace(
            get_model_config=lambda name: SimpleNamespace(name=name) if name == "test-model" else None,
            skills=SimpleNamespace(container_path=None),
            run_events=SimpleNamespace(),
        ),
        bridge=object(),
        project_checkpointer=ProjectCheckpointer(),
        store=object(),
        event_store=object(),
        asset_runtime=AssetRuntime(),
        agent_factory=object(),
        runner=runner,
    )

    owner_token = set_current_user(SimpleNamespace(id="forged-ambient-owner"))
    storage_token = set_runtime_storage_user_id("forged-ambient-storage")
    try:
        result = await executor.execute(execution, Authority())
        assert result.status == "succeeded"
        assert captured == [
            (owner_user_id, owner_user_id, context.resource_scope),
        ]
        assert get_current_user().id == "forged-ambient-owner"
        assert get_runtime_storage_user_id() == "forged-ambient-storage"
    finally:
        reset_runtime_storage_user_id(storage_token)
        reset_current_user(owner_token)
