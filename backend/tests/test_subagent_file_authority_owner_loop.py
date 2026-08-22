"""Owner-loop boundary for delegated private Run file operations."""

from __future__ import annotations

import asyncio
from types import MappingProxyType, SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from deerflow.agents.middlewares.tool_output_budget_middleware import (
    ToolOutputBudgetMiddleware,
)
from deerflow.config.tool_output_config import ToolOutputConfig
from deerflow.subagents.binding import (
    AgentGraphExecutionInputs,
    ConfiguredLeadParentExecutionProfile,
    ParentExecutionBarrier,
    ParentExecutionBinding,
)
from deerflow.subagents.delegated_context import _OwnerLoopFileAuthorityProxy


class _LoopBoundFileAuthority:
    def __init__(self, owner_loop: asyncio.AbstractEventLoop) -> None:
        self.owner_loop = owner_loop
        self.calls: list[tuple[str, asyncio.AbstractEventLoop, str, bytes]] = []

    async def write_internal(self, relative_path: str, content: bytes) -> str:
        self.calls.append(
            ("write_internal", asyncio.get_running_loop(), relative_path, content),
        )
        return f"/mnt/user-data/workspace/{relative_path}"

    async def write_output(self, relative_path: str, content: bytes) -> str:
        self.calls.append(
            ("write_output", asyncio.get_running_loop(), relative_path, content),
        )
        return f"/mnt/user-data/outputs/{relative_path}"


def _binding(owner_loop: asyncio.AbstractEventLoop) -> ParentExecutionBinding:
    return ParentExecutionBinding(
        profile=ConfiguredLeadParentExecutionProfile(
            graph=AgentGraphExecutionInputs(
                model=object(),
                tools=(),
                middleware=(),
                system_prompt=None,
                state_schema=dict,
            ),
            app_config=object(),
            asset_context=None,
            agent_config=None,
            model_name="test-model",
            thinking_enabled=False,
            reasoning_effort=None,
            plan_mode=False,
            subagent_enabled=True,
            agent_name="lead",
            available_skills=None,
        ),
        state=MappingProxyType({}),
        context=MappingProxyType({}),
        config=MappingProxyType({}),
        owner_loop=owner_loop,
        store=None,
        barrier=ParentExecutionBarrier(),
    )


@pytest.mark.asyncio
async def test_large_subagent_tool_output_uses_file_authority_owner_loop() -> None:
    owner_loop = asyncio.get_running_loop()
    authority = _LoopBoundFileAuthority(owner_loop)
    proxy = _OwnerLoopFileAuthorityProxy(  # type: ignore[arg-type]
        authority,
        _binding(owner_loop),
    )
    large_result = "x" * 12_001
    request = SimpleNamespace(
        tool=SimpleNamespace(),
        tool_call={"name": "get_lower_carrier_tree", "id": "call-lower"},
        runtime=SimpleNamespace(
            context={
                "private_scope": object(),
                "__file_authority": proxy,
            },
        ),
    )

    async def run_on_subagent_loop() -> tuple[ToolMessage, str]:
        async def handler(_request: object) -> ToolMessage:
            return ToolMessage(
                content=large_result,
                tool_call_id="call-lower",
                name="get_lower_carrier_tree",
            )

        patched = await ToolOutputBudgetMiddleware(
            ToolOutputConfig(),
        ).awrap_tool_call(request, handler)
        output_path = await proxy.write_output("answer.txt", b"answer")
        assert isinstance(patched, ToolMessage)
        return patched, output_path

    patched, output_path = await asyncio.to_thread(
        lambda: asyncio.run(run_on_subagent_loop()),
    )

    assert "/mnt/user-data/workspace/.tool-results/" in str(patched.content)
    assert output_path == "/mnt/user-data/outputs/answer.txt"
    assert [call[0] for call in authority.calls] == [
        "write_internal",
        "write_output",
    ]
    assert all(call[1] is owner_loop for call in authority.calls)
    assert authority.calls[0][3] == large_result.encode("utf-8")
