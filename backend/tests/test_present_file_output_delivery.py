from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from deerflow.agents.middlewares.tool_error_handling_middleware import (
    ToolErrorHandlingMiddleware,
)
from deerflow.runtime.context_keys import RuntimeContextKeys
from deerflow.tools.builtins.present_file_tool import present_file_tool


class _Authority:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str]] = []

    async def record_presented_paths(
        self,
        paths: tuple[str, ...],
        *,
        tool_call_id: str,
    ) -> None:
        self.calls.append((paths, tool_call_id))


@pytest.mark.anyio
async def test_present_files_persists_intent_before_success_message() -> None:
    authority = _Authority()
    runtime = SimpleNamespace(
        context={
            "private_scope": object(),
            "__file_authority": authority,
        },
        state=None,
        config={},
    )

    command = await present_file_tool.coroutine(
        runtime=runtime,
        filepaths=["/mnt/user-data/outputs/report.txt"],
        tool_call_id="present-1",
    )

    assert authority.calls == [
        (("/mnt/user-data/outputs/report.txt",), "present-1"),
    ]
    assert command.update["artifacts"] == [
        "/mnt/user-data/outputs/report.txt",
    ]
    assert command.update["messages"][0].content == ("Successfully presented files")


@pytest.mark.anyio
async def test_present_files_never_reports_success_when_intent_persistence_fails() -> None:
    class FailingAuthority(_Authority):
        async def record_presented_paths(
            self,
            paths: tuple[str, ...],
            *,
            tool_call_id: str,
        ) -> None:
            del paths, tool_call_id
            raise RuntimeError("intent persistence unavailable")

    runtime = SimpleNamespace(
        context={
            "private_scope": object(),
            "__file_authority": FailingAuthority(),
        },
        state=None,
        config={},
    )

    with pytest.raises(RuntimeError, match="intent persistence unavailable"):
        await present_file_tool.coroutine(
            runtime=runtime,
            filepaths=["/mnt/user-data/outputs/report.txt"],
            tool_call_id="present-1",
        )


@pytest.mark.anyio
async def test_subagent_present_files_is_rejected_before_persisting_intent() -> None:
    authority = _Authority()
    runtime = SimpleNamespace(
        context={
            "private_scope": object(),
            "__file_authority": authority,
            RuntimeContextKeys.IS_SUBAGENT: True,
        },
        state=None,
        config={},
    )

    command = await present_file_tool.coroutine(
        runtime=runtime,
        filepaths=["/mnt/user-data/outputs/report.txt"],
        tool_call_id="present-subagent-1",
    )

    assert authority.calls == []
    assert "artifacts" not in command.update
    message = command.update["messages"][0]
    assert message.tool_call_id == "present-subagent-1"
    assert message.content == ("Error: Sub-Agent files must be promoted and presented by the Lead")


@pytest.mark.asyncio
async def test_present_files_uses_the_run_idempotent_authorization_boundary() -> None:
    calls: list[str] = []

    class Boundary:
        async def before_idempotent_tool_call(self) -> None:
            calls.append("idempotent")

        async def before_tool_call(self) -> None:
            calls.append("ambiguous")

    request = ToolCallRequest(
        tool_call={
            "id": "present-boundary",
            "name": "present_files",
            "args": {
                "filepaths": ["/mnt/user-data/outputs/report.txt"],
            },
            "type": "tool_call",
        },
        tool=present_file_tool,
        state={},
        runtime=SimpleNamespace(
            context={"__authorization_boundary": Boundary()},
        ),
    )

    async def handler(_request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(
            "Successfully presented files",
            tool_call_id="present-boundary",
            name="present_files",
        )

    result = await ToolErrorHandlingMiddleware().awrap_tool_call(
        request,
        handler,
    )

    assert isinstance(result, ToolMessage)
    assert calls == ["idempotent"]
