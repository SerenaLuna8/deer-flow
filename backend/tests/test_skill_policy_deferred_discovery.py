from __future__ import annotations

from pathlib import Path

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import Field

from deerflow.agents.middlewares.deferred_tool_filter_middleware import (
    DeferredToolFilterMiddleware,
)
from deerflow.agents.middlewares.skill_tool_policy_middleware import (
    SkillToolPolicyMiddleware,
)
from deerflow.agents.thread_state import ThreadState
from deerflow.runtime.secret_context import write_slash_skill_source_path
from deerflow.skills.types import Skill, SkillCategory
from deerflow.tools.builtins.tool_search import build_deferred_tool_setup
from deerflow.tools.mcp_metadata import tag_mcp_tool

_CONTAINER_ROOT = "/mnt/skills"
_SLASH_SOURCE_OWNER_TOKEN = "deferred-test-owner"
_CALC_CALLS: list[str] = []
_DENIED_CALLS: list[str] = []


@tool
def policy_calc(expression: str) -> str:
    """Evaluate one expression allowed by the active Skill."""

    _CALC_CALLS.append(expression)
    return "4"


@tool
def policy_denied_lookup(query: str) -> str:
    """Run a lookup denied by the active Skill."""

    _DENIED_CALLS.append(query)
    return "denied"


class RecordingModel(GenericFakeChatModel):
    bound_tool_names: list[list[str]] = Field(default_factory=list)

    def __init__(self, responses: list[AIMessage]):
        super().__init__(messages=iter(responses))

    def bind_tools(self, tools, **kwargs):
        self.bound_tool_names.append([getattr(candidate, "name", "") for candidate in tools])
        return self


def test_exact_active_policy_closes_all_three_deferred_tool_boundaries():
    skill_dir = Path("/run/exact-skills/restrictive")
    restrictive = Skill(
        name="restrictive",
        description="Allow only the calculator",
        license="MIT",
        skill_dir=skill_dir,
        skill_file=skill_dir / "SKILL.md",
        relative_path=Path("restrictive"),
        category=SkillCategory.CUSTOM,
        allowed_tools=(policy_calc.name,),
        enabled=True,
        runtime_read_only=True,
    )
    policy = SkillToolPolicyMiddleware(
        runtime_skills=(restrictive,),
        runtime_skill_version_ids=("postgres-skill-version",),
        runtime_skills_container_path=_CONTAINER_ROOT,
        slash_source_owner_token=_SLASH_SOURCE_OWNER_TOKEN,
    )
    context: dict[str, object] = {}
    write_slash_skill_source_path(
        context,
        restrictive.get_container_file_path(_CONTAINER_ROOT),
        owner_token=_SLASH_SOURCE_OWNER_TOKEN,
    )
    setup = build_deferred_tool_setup(
        [
            tag_mcp_tool(policy_calc),
            tag_mcp_tool(policy_denied_lookup),
        ],
        enabled=True,
    )
    model = RecordingModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "tool_search",
                        "args": {"query": (f"select:{policy_calc.name},{policy_denied_lookup.name}")},
                        "id": "search-call",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": policy_denied_lookup.name,
                        "args": {"query": "secret"},
                        "id": "denied-call",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": policy_calc.name,
                        "args": {"expression": "2 + 2"},
                        "id": "calc-call",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    _CALC_CALLS.clear()
    _DENIED_CALLS.clear()
    graph = create_agent(
        model=model,
        tools=[
            policy_calc,
            policy_denied_lookup,
            setup.tool_search_tool,
        ],
        middleware=[
            policy,
            DeferredToolFilterMiddleware(
                setup.deferred_names,
                setup.catalog_hash,
            ),
        ],
        state_schema=ThreadState,
    )

    result = graph.invoke(
        {
            "messages": [
                HumanMessage(content="try both deferred tools"),
            ]
        },
        context=context,
    )

    assert model.bound_tool_names[0] == ["tool_search"]
    assert all(policy_denied_lookup.name not in names for names in model.bound_tool_names)
    assert _DENIED_CALLS == []
    assert _CALC_CALLS == ["2 + 2"]
    assert result["promoted"] == {
        "catalog_hash": setup.catalog_hash,
        "names": [policy_calc.name],
    }
    search_result = next(message for message in result["messages"] if isinstance(message, ToolMessage) and message.tool_call_id == "search-call")
    assert policy_calc.name in search_result.content
    assert policy_denied_lookup.name not in search_result.content
    denied_result = next(message for message in result["messages"] if isinstance(message, ToolMessage) and message.tool_call_id == "denied-call")
    assert denied_result.status == "error"
    assert "not allowed by the active skill policy" in denied_result.content
