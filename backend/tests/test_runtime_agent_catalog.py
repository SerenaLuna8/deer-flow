from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from deerflow.config.agents_config import AgentModelSettings
from deerflow.skills.types import Skill, SkillCategory
from deerflow.subagents.runtime_catalog import (
    RuntimeAgentCatalog,
    RuntimeAgentProfile,
    build_runtime_agent_catalog,
    build_runtime_agent_profile,
    trusted_runtime_agent_catalog,
)


class _Args(BaseModel):
    value: str


async def _invoke(value: str) -> str:
    return value


def _skill(tmp_path: Path) -> Skill:
    skill_dir = tmp_path / "exact"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("---\nname: exact\ndescription: exact\n---\n", encoding="utf-8")
    return Skill(
        name="exact",
        description="Exact skill",
        license=None,
        skill_dir=skill_dir,
        skill_file=skill_file,
        relative_path=Path("exact"),
        category=SkillCategory.CUSTOM,
        enabled=True,
        runtime_read_only=True,
    )


def _mcp_tool() -> StructuredTool:
    return StructuredTool.from_function(
        coroutine=_invoke,
        name="exact_mcp",
        description="Exact MCP",
        args_schema=_Args,
        metadata={"deerflow_mcp": True, "deerflow_private_mcp": True},
    )


def test_runtime_agent_catalog_is_factory_sealed_and_immutable(tmp_path: Path) -> None:
    skill = _skill(tmp_path)
    tool = _mcp_tool()
    prompt_bundle = object()
    settings = AgentModelSettings(temperature=0.2, thinking_enabled=True)
    profile = build_runtime_agent_profile(
        key="project/researcher",
        description="Research project data",
        model_name="exact-model",
        model_settings=settings,
        tool_groups=("file:read",),
        prompt_bundle=prompt_bundle,
        runtime_skills=(skill,),
        mcp_tools=(tool,),
    )
    catalog = build_runtime_agent_catalog((profile,))

    assert catalog.get("project/researcher") is profile
    assert catalog.names == ("project/researcher",)
    assert profile.runtime_skills == (skill,)
    assert profile.mcp_tools == (tool,)
    assert trusted_runtime_agent_catalog(catalog) is catalog
    assert trusted_runtime_agent_catalog({"project/researcher": profile}) is None
    assert "Research project data" not in repr(profile)
    assert "prompt_bundle" not in repr(profile)

    with pytest.raises(TypeError):
        RuntimeAgentProfile()
    with pytest.raises(TypeError):
        RuntimeAgentCatalog()
    with pytest.raises(AttributeError):
        catalog.names = ()


@pytest.mark.parametrize(
    "key",
    [
        "researcher",
        "other/researcher",
        "project/Researcher",
        "project/-researcher",
        "project/researcher-",
    ],
)
def test_runtime_agent_profile_rejects_non_namespaced_keys(key: str) -> None:
    with pytest.raises(ValueError):
        build_runtime_agent_profile(
            key=key,
            description="description",
            model_name="exact-model",
            model_settings=AgentModelSettings(),
            tool_groups=(),
            prompt_bundle=object(),
            runtime_skills=(),
            mcp_tools=(),
        )


def test_runtime_agent_catalog_rejects_duplicate_keys() -> None:
    profile = build_runtime_agent_profile(
        key="system/researcher",
        description="description",
        model_name="exact-model",
        model_settings=AgentModelSettings(),
        tool_groups=(),
        prompt_bundle=object(),
        runtime_skills=(),
        mcp_tools=(),
    )

    with pytest.raises(ValueError):
        build_runtime_agent_catalog((profile, profile))
