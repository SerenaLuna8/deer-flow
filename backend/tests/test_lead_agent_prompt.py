from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace

from deerflow.agents.lead_agent import prompt as prompt_module
from deerflow.config.subagents_config import CustomSubagentConfig, SubagentsAppConfig
from deerflow.skills.parser import parse_skill_file
from deerflow.skills.types import SkillCategory


def _prompt_config() -> SimpleNamespace:
    return SimpleNamespace(
        sandbox=SimpleNamespace(mounts=[]),
        skills=SimpleNamespace(container_path="/mnt/skills"),
        acp_agents={},
    )


def _exact_skill(tmp_path: Path, *, runtime_read_only: bool):
    skill_dir = tmp_path / "custom" / "exact"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\nname: exact\ndescription: exact project skill\n---\nbody\n",
        encoding="utf-8",
    )
    parsed = parse_skill_file(
        skill_file,
        SkillCategory.CUSTOM,
        skill_dir.relative_to(tmp_path / "custom"),
    )
    assert parsed is not None
    return dataclasses.replace(
        parsed,
        enabled=True,
        runtime_read_only=runtime_read_only,
    )


def test_lead_agent_accepts_only_opaque_runtime_asset_context() -> None:
    from deerflow.agents.lead_agent import agent as lead_agent_module

    opaque = object()
    assert lead_agent_module._trusted_runtime_asset_context({"project_context": opaque}) is opaque
    assert lead_agent_module._trusted_runtime_asset_context({"asset_context": opaque}) is opaque
    assert lead_agent_module._trusted_runtime_asset_context({"project_context": {"project_id": "forged"}}) is None


def test_self_update_section_is_removed_for_all_agents() -> None:
    assert prompt_module._build_self_update_section(None) == ""
    assert prompt_module._build_self_update_section("custom-agent") == ""


def test_exact_run_skill_prompt_is_labeled_read_only(tmp_path: Path) -> None:
    skill = _exact_skill(tmp_path, runtime_read_only=True)

    prompt = prompt_module.apply_prompt_template(
        app_config=_prompt_config(),
        exact_soul="exact soul",
        exact_skills=(skill,),
        exact_skills_container_path="/mnt/run-skills",
        available_skills={"exact"},
    )

    assert "exact soul" in prompt
    assert "[run exact, read-only]" in prompt
    assert "[custom, editable]" not in prompt
    assert "/mnt/run-skills/custom/exact/SKILL.md" in prompt


def test_skill_prompt_cache_signature_includes_runtime_read_only() -> None:
    prompt_module._get_cached_skills_prompt_section.cache_clear()
    editable = (("exact", "description", "custom", "/mnt/exact/SKILL.md", False),)
    read_only = (("exact", "description", "custom", "/mnt/exact/SKILL.md", True),)

    editable_section = prompt_module._get_cached_skills_prompt_section(
        editable,
        (),
        None,
        "/mnt/skills",
        "",
    )
    read_only_section = prompt_module._get_cached_skills_prompt_section(
        read_only,
        (),
        None,
        "/mnt/skills",
        "",
    )

    assert "[custom, editable]" in editable_section
    assert "[run exact, read-only]" in read_only_section
    assert prompt_module._get_cached_skills_prompt_section.cache_info().currsize == 2


def test_build_custom_mounts_section_returns_empty_when_no_mounts() -> None:
    config = SimpleNamespace(sandbox=SimpleNamespace(mounts=[]))

    assert prompt_module._build_custom_mounts_section(app_config=config) == ""


def test_build_custom_mounts_section_lists_configured_mounts() -> None:
    config = SimpleNamespace(
        sandbox=SimpleNamespace(
            mounts=[
                SimpleNamespace(
                    container_path="/home/user/shared",
                    read_only=False,
                ),
                SimpleNamespace(container_path="/mnt/reference", read_only=True),
            ]
        )
    )

    section = prompt_module._build_custom_mounts_section(app_config=config)

    assert "**Custom Mounted Directories:**" in section
    assert "`/home/user/shared`" in section
    assert "read-write" in section
    assert "`/mnt/reference`" in section
    assert "read-only" in section


def test_build_custom_mounts_uses_explicit_config_without_global_read(
    monkeypatch,
) -> None:
    config = SimpleNamespace(
        sandbox=SimpleNamespace(
            mounts=[
                SimpleNamespace(
                    container_path="/home/user/shared",
                    read_only=False,
                )
            ]
        )
    )

    def fail_get_app_config():
        raise AssertionError("explicit app_config must not read ambient config")

    monkeypatch.setattr("deerflow.config.get_app_config", fail_get_app_config)

    section = prompt_module._build_custom_mounts_section(app_config=config)

    assert "`/home/user/shared`" in section


def test_apply_prompt_template_includes_custom_mounts() -> None:
    config = _prompt_config()
    config.sandbox.mounts = [SimpleNamespace(container_path="/home/user/shared", read_only=False)]

    rendered = prompt_module.apply_prompt_template(
        app_config=config,
        exact_soul="",
        exact_skills=(),
    )

    assert "Custom Mounted Directories" in rendered
    assert "`/home/user/shared`" in rendered


def test_apply_prompt_template_includes_relative_path_guidance() -> None:
    rendered = prompt_module.apply_prompt_template(
        app_config=_prompt_config(),
        exact_soul="",
        exact_skills=(),
    )

    assert "Treat `/mnt/user-data/workspace` as your default current working directory" in rendered
    assert "`hello.txt`, `../uploads/data.csv`, and `../outputs/report.md`" in rendered


def test_apply_prompt_template_threads_explicit_config_to_subagents(
    monkeypatch,
) -> None:
    config = SimpleNamespace(
        sandbox=SimpleNamespace(
            use="deerflow.sandbox.local:LocalSandboxProvider",
            allow_host_bash=False,
            mounts=[],
        ),
        subagents=SubagentsAppConfig(
            custom_agents={
                "researcher": CustomSubagentConfig(
                    description="Research agent\nwith details",
                    system_prompt="You research.",
                )
            }
        ),
        skills=SimpleNamespace(container_path="/mnt/skills"),
        acp_agents={},
    )

    def fail_global_subagent_config():
        raise AssertionError("explicit app_config must not read ambient config")

    monkeypatch.setattr(
        "deerflow.config.subagents_config.get_subagents_app_config",
        fail_global_subagent_config,
    )

    rendered = prompt_module.apply_prompt_template(
        subagent_enabled=True,
        app_config=config,
        exact_soul="",
        exact_skills=(),
    )

    assert "**researcher**: Research agent" in rendered
    assert "**bash**" not in rendered


def test_build_acp_section_uses_explicit_config_without_global_read(
    monkeypatch,
) -> None:
    config = SimpleNamespace(acp_agents={"codex": object()})

    def fail_get_acp_agents():
        raise AssertionError("explicit app_config must not read ambient config")

    monkeypatch.setattr(
        "deerflow.config.acp_config.get_acp_agents",
        fail_get_acp_agents,
    )

    section = prompt_module._build_acp_section(app_config=config)

    assert "ACP Agent Tasks" in section
    assert "/mnt/acp-workspace/" in section


def test_get_memory_context_uses_explicit_config_without_global_read(
    monkeypatch,
) -> None:
    config = SimpleNamespace(
        memory=SimpleNamespace(
            enabled=True,
            injection_enabled=True,
            max_injection_tokens=1234,
            token_counting="tiktoken",
        )
    )
    captured: dict[str, object] = {}

    def fail_get_memory_config():
        raise AssertionError("explicit app_config must not read ambient config")

    def fake_get_memory_data(agent_name=None, *, user_id=None):
        captured["agent_name"] = agent_name
        captured["user_id"] = user_id
        return {"facts": []}

    def fake_format_memory_for_injection(
        memory_data,
        *,
        max_tokens,
        use_tiktoken=True,
        **_kwargs,
    ):
        captured["memory_data"] = memory_data
        captured["max_tokens"] = max_tokens
        captured["use_tiktoken"] = use_tiktoken
        return "remember this"

    monkeypatch.setattr(
        "deerflow.config.memory_config.get_memory_config",
        fail_get_memory_config,
    )
    monkeypatch.setattr(
        "deerflow.runtime.user_context.get_effective_user_id",
        lambda: "user-1",
    )
    monkeypatch.setattr(
        "deerflow.agents.memory.get_memory_data",
        fake_get_memory_data,
    )
    monkeypatch.setattr(
        "deerflow.agents.memory.format_memory_for_injection",
        fake_format_memory_for_injection,
    )

    context = prompt_module._get_memory_context("agent-a", app_config=config)

    assert "<memory>" in context
    assert "remember this" in context
    assert captured == {
        "agent_name": "agent-a",
        "user_id": "user-1",
        "memory_data": {"facts": []},
        "max_tokens": 1234,
        "use_tiktoken": True,
    }


def test_warm_enabled_skills_cache_logs_on_timeout(monkeypatch, caplog) -> None:
    import threading

    event = threading.Event()
    monkeypatch.setattr(prompt_module, "_ensure_enabled_skills_cache", lambda: event)

    with caplog.at_level("WARNING"):
        warmed = prompt_module.warm_enabled_skills_cache(timeout_seconds=0.01)

    assert warmed is False
    assert "Timed out waiting" in caplog.text


def test_system_prompt_template_contains_file_editing_workflow_rule() -> None:
    template = prompt_module.SYSTEM_PROMPT_TEMPLATE

    assert "File Editing Workflow" in template
    assert "str_replace" in template
    assert "append=True" in template


def test_system_prompt_template_preserves_placeholders() -> None:
    template = prompt_module.SYSTEM_PROMPT_TEMPLATE
    for placeholder in (
        "{agent_name}",
        "{soul}",
        "{self_update_section}",
        "{subagent_thinking}",
        "{skills_section}",
        "{deferred_tools_section}",
        "{subagent_section}",
        "{acp_section}",
        "{subagent_reminder}",
        "{skill_first_reminder}",
    ):
        assert placeholder in template


def test_prompt_without_deferred_skill_names_uses_tool_agnostic_reminder() -> None:
    rendered = prompt_module.apply_prompt_template(
        app_config=_prompt_config(),
        exact_soul="",
        exact_skills=(),
    )

    assert "Always load the relevant skill" in rendered
    assert "describe_skill(name)" not in rendered


def test_prompt_with_deferred_skill_names_mentions_describe_skill() -> None:
    rendered = prompt_module.apply_prompt_template(
        app_config=_prompt_config(),
        skill_names=frozenset({"data-analysis"}),
        exact_soul="",
        exact_skills=(),
    )

    assert "describe_skill(name)" in rendered
    assert "Always load the relevant skill" not in rendered
