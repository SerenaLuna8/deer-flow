from __future__ import annotations

import dataclasses
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.tools import tool

from deerflow.agents.lead_agent import agent as lead_agent_module
from deerflow.agents.lead_agent import prompt
from deerflow.agents.middlewares.skill_activation_middleware import (
    SkillActivationMiddleware,
)
from deerflow.config.app_config import AppConfig
from deerflow.skills.types import Skill, SkillCategory


def _runtime_skill(root: Path) -> Skill:
    skill_dir = root / "exact-skill"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("---\nname: exact-skill\ndescription: exact\n---\n", encoding="utf-8")
    return Skill(
        name="exact-skill",
        description="exact",
        license=None,
        skill_dir=skill_dir,
        skill_file=skill_file,
        relative_path=Path("exact-skill"),
        category=SkillCategory.CUSTOM,
        enabled=True,
        runtime_read_only=True,
    )


def test_prompt_module_has_no_file_backed_skill_discovery() -> None:
    source = inspect.getsource(prompt)
    assert "get_or_new_" + "skill_storage" not in source
    assert "get_or_new_" + "user_skill_storage" not in source
    assert prompt.get_enabled_skills_for_config() == []


def test_slash_activation_reads_only_exact_runtime_skill(tmp_path: Path) -> None:
    skill = _runtime_skill(tmp_path)
    middleware = SkillActivationMiddleware(
        runtime_skills=(skill,),
        runtime_skills_root=tmp_path,
        runtime_skills_container_path="/mnt/skills",
    )

    resolution = middleware._resolve_activation("/exact-skill do it")

    assert resolution is not None
    assert resolution.activation is not None
    assert resolution.activation.skill_name == "exact-skill"
    assert resolution.activation.editable is False


def test_slash_activation_without_run_snapshot_exposes_nothing() -> None:
    middleware = SkillActivationMiddleware()
    resolution = middleware._resolve_activation("/unknown do it")
    assert resolution is not None
    assert resolution.failure_message == "Skill `/unknown` is not installed."


def test_subagent_executor_has_no_storage_lookup() -> None:
    executor_path = Path(__file__).parents[1] / "packages/harness/deerflow/subagents/executor.py"
    source = executor_path.read_text(encoding="utf-8")
    assert "get_or_new_" + "skill_storage" not in source
    assert "self._runtime_skills" in source


def test_make_lead_agent_uses_only_exact_private_runtime_contract(
    tmp_path: Path,
    monkeypatch,
) -> None:
    skill = _runtime_skill(tmp_path)

    @tool
    def exact_builtin(value: str) -> str:
        """Exact admitted builtin tool."""

        return value

    @tool
    def exact_mcp(value: str) -> str:
        """Exact admitted MCP proxy."""

        return value

    skill = dataclasses.replace(
        skill,
        # Merely admitting a Skill must not activate its tool allowlist. Both
        # exact candidate tools remain in the graph; the request-time policy
        # middleware narrows them only after slash/skill_context activation.
        allowed_tools=("exact_builtin",),
    )
    exact_config = AppConfig.model_validate(
        {
            "models": [
                {
                    "name": "exact-model",
                    "use": "tests.fake:Model",
                    "model": "exact-provider-model",
                    "supports_thinking": True,
                }
            ],
            "sandbox": {
                "use": "deerflow.sandbox.local:LocalSandboxProvider",
            },
            "skills": {
                "container_path": "/mnt/exact-skills",
                "deferred_discovery": False,
            },
            "tool_search": {"enabled": False},
        }
    )
    forged_config = AppConfig.model_validate(
        {
            "models": [
                {
                    "name": "forged-global-model",
                    "use": "tests.fake:Model",
                    "model": "forged-provider-model",
                }
            ],
            "sandbox": {
                "use": "deerflow.sandbox.local:LocalSandboxProvider",
            },
        }
    )
    private_runtime = SimpleNamespace(
        model_ref="exact-model",
        soul="exact admitted soul",
        tool_groups=("exact-group",),
        skills=(skill,),
        skill_root=tmp_path,
        mcp_tools=(exact_mcp,),
        safe_manifest=SimpleNamespace(
            skills=(
                SimpleNamespace(
                    relative_root=skill.relative_path.as_posix(),
                    version_id="exact-skill-version",
                ),
            ),
        ),
    )
    config = {
        "configurable": {
            "app_config": forged_config,
            "model_name": "forged-global-model",
            "agent_name": "forged-global-agent",
            "is_plan_mode": True,
            "subagent_enabled": True,
            "user_id": "exact-owner",
        }
    }
    captured: dict[str, object] = {}
    available_tool_builds = 0

    def forbidden_global_fallback(*_args, **_kwargs):
        raise AssertionError("private runtime must not consult global asset state")

    def fake_available_tools(**kwargs):
        nonlocal available_tool_builds
        available_tool_builds += 1
        captured["tool_kwargs"] = kwargs
        return [exact_builtin]

    def fake_model(**kwargs):
        captured["model"] = kwargs
        return "exact-model-instance"

    def fake_middlewares(*args, **kwargs):
        captured["middleware_args"] = args
        captured["middlewares"] = kwargs
        return ["exact-middleware"]

    def fake_prompt(**kwargs):
        captured["prompt"] = kwargs
        return "exact-system-prompt"

    def fake_create_agent(**kwargs):
        captured["create_agent"] = kwargs
        return "exact-agent"

    import deerflow.tools as tools_module

    monkeypatch.setattr(lead_agent_module, "get_app_config", forbidden_global_fallback)
    monkeypatch.setattr(lead_agent_module, "load_agent_config", forbidden_global_fallback)
    monkeypatch.setattr(
        lead_agent_module,
        "_load_enabled_available_skills",
        forbidden_global_fallback,
    )
    monkeypatch.setattr(prompt, "get_enabled_skills_for_config", forbidden_global_fallback)
    monkeypatch.setattr(tools_module, "get_available_tools", fake_available_tools)
    monkeypatch.setattr(lead_agent_module, "create_chat_model", fake_model)
    monkeypatch.setattr(lead_agent_module, "build_middlewares", fake_middlewares)
    monkeypatch.setattr(lead_agent_module, "apply_prompt_template", fake_prompt)
    monkeypatch.setattr(lead_agent_module, "create_agent", fake_create_agent)
    monkeypatch.setattr(lead_agent_module, "build_tracing_callbacks", lambda: [])

    result = lead_agent_module._make_lead_agent(
        config,
        app_config=exact_config,
        private_runtime=private_runtime,
    )

    assert result == "exact-agent"
    assert available_tool_builds == 1
    assert captured["model"] == {
        "name": "exact-model",
        "thinking_enabled": True,
        "reasoning_effort": None,
        "app_config": exact_config,
        "attach_tracing": False,
    }
    assert captured["tool_kwargs"] == {
        "model_name": "exact-model",
        "groups": ["exact-group"],
        "subagent_enabled": False,
        "app_config": exact_config,
        "asset_context": None,
        "include_mcp": False,
        "include_acp": False,
    }
    create_kwargs = captured["create_agent"]
    assert create_kwargs["model"] == "exact-model-instance"
    assert [tool.name for tool in create_kwargs["tools"]] == [
        "exact_builtin",
        "exact_mcp",
    ]
    assert create_kwargs["system_prompt"] == "exact-system-prompt"
    assert captured["middlewares"]["runtime_skills"] == (skill,)
    assert captured["middlewares"]["runtime_skill_version_ids"] == ("exact-skill-version",)
    assert captured["middlewares"]["runtime_skills_root"] == tmp_path
    assert captured["middlewares"]["runtime_skills_container_path"] == "/mnt/exact-skills"
    assert captured["prompt"]["exact_soul"] == "exact admitted soul"
    assert captured["prompt"]["exact_skills"] == (skill,)
    assert captured["prompt"]["exact_skills_container_path"] == "/mnt/exact-skills"
    assert config["metadata"]["model_name"] == "exact-model"
    assert config["metadata"]["tool_groups"] == ["exact-group"]
    assert config["metadata"]["subagent_enabled"] is False


@pytest.mark.parametrize("is_bootstrap", [False, True])
def test_passive_discoverable_skills_never_filter_lead_candidates(
    is_bootstrap: bool,
    tmp_path: Path,
    monkeypatch,
) -> None:
    skill = dataclasses.replace(
        _runtime_skill(tmp_path),
        name="bootstrap",
        relative_path=Path("bootstrap"),
        allowed_tools=("allowed_candidate",),
    )

    @tool
    def allowed_candidate(value: str) -> str:
        """Candidate named by the passive Skill."""

        return value

    @tool
    def other_candidate(value: str) -> str:
        """Candidate not named by the passive Skill."""

        return value

    app_config = AppConfig.model_validate(
        {
            "models": [
                {
                    "name": "test-model",
                    "use": "tests.fake:Model",
                    "model": "provider-model",
                }
            ],
            "sandbox": {
                "use": "deerflow.sandbox.local:LocalSandboxProvider",
            },
            "skills": {"deferred_discovery": False},
            "tool_search": {"enabled": False},
        }
    )
    import deerflow.tools as tools_module

    monkeypatch.setattr(lead_agent_module, "load_agent_config", lambda _name: None)
    monkeypatch.setattr(
        lead_agent_module,
        "_load_enabled_available_skills",
        lambda *_args, **_kwargs: [skill],
    )
    monkeypatch.setattr(
        tools_module,
        "get_available_tools",
        lambda **_kwargs: [allowed_candidate, other_candidate],
    )
    monkeypatch.setattr(
        lead_agent_module,
        "create_chat_model",
        lambda **_kwargs: "model",
    )
    monkeypatch.setattr(
        lead_agent_module,
        "build_middlewares",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        lead_agent_module,
        "apply_prompt_template",
        lambda **_kwargs: "prompt",
    )
    monkeypatch.setattr(
        lead_agent_module,
        "create_agent",
        lambda **kwargs: kwargs,
    )
    monkeypatch.setattr(
        lead_agent_module,
        "build_tracing_callbacks",
        lambda: [],
    )

    result = lead_agent_module._make_lead_agent(
        {"configurable": {"is_bootstrap": is_bootstrap}},
        app_config=app_config,
    )

    assert [candidate.name for candidate in result["tools"]] == [
        "allowed_candidate",
        "other_candidate",
    ]
