"""Sandbox lookup must tolerate fork-restored ``Overwrite`` values."""

from __future__ import annotations

import pytest
from langchain.tools import ToolRuntime
from langgraph.types import Overwrite

from deerflow.sandbox.sandbox import Sandbox
from deerflow.sandbox.sandbox_provider import (
    SandboxProvider,
    reset_sandbox_provider,
    set_sandbox_provider,
)
from deerflow.sandbox.search import GrepMatch
from deerflow.sandbox.tools import (
    ensure_sandbox_initialized,
    ensure_sandbox_initialized_async,
)


class _StubSandbox(Sandbox):
    def execute_command(
        self,
        command: str,
        env: dict | None = None,
        timeout: float | None = None,
    ) -> str:
        del command, env, timeout
        return "OK"

    def read_file(self, path: str) -> str:
        del path
        return "content"

    def download_file(self, path: str) -> bytes:
        del path
        return b"content"

    def list_dir(self, path: str, max_depth: int = 2) -> list[str]:
        del path, max_depth
        return []

    def write_file(
        self,
        path: str,
        content: str,
        append: bool = False,
    ) -> None:
        del path, content, append

    def glob(
        self,
        path: str,
        pattern: str,
        *,
        include_dirs: bool = False,
        max_results: int = 200,
    ) -> tuple[list[str], bool]:
        del path, pattern, include_dirs, max_results
        return [], False

    def grep(
        self,
        path: str,
        pattern: str,
        *,
        glob: str | None = None,
        literal: bool = False,
        case_sensitive: bool = False,
        max_results: int = 100,
    ) -> tuple[list[GrepMatch], bool]:
        del path, pattern, glob, literal, case_sensitive, max_results
        return [], False

    def update_file(self, path: str, content: bytes) -> None:
        del path, content


class _Provider(SandboxProvider):
    def __init__(self, *, parent_exists: bool) -> None:
        self.parent_exists = parent_exists
        self.sandbox = _StubSandbox("stub")
        self.acquired: list[str | None] = []

    def acquire(
        self,
        thread_id: str | None = None,
        *,
        user_id: str | None = None,
    ) -> str:
        del user_id
        self.acquired.append(thread_id)
        return "fresh-sandbox"

    async def acquire_async(
        self,
        thread_id: str | None = None,
        *,
        user_id: str | None = None,
    ) -> str:
        del user_id
        self.acquired.append(thread_id)
        return "fresh-sandbox"

    def get(self, sandbox_id: str) -> Sandbox | None:
        if sandbox_id == "parent-sandbox" and self.parent_exists:
            return self.sandbox
        if sandbox_id == "fresh-sandbox":
            return self.sandbox
        return None

    def release(self, sandbox_id: str) -> None:
        del sandbox_id


def _runtime(provider: _Provider) -> ToolRuntime:
    set_sandbox_provider(provider)
    return ToolRuntime(
        state={"sandbox": Overwrite({"sandbox_id": "parent-sandbox"})},
        context={"thread_id": "t-1"},
        config={"configurable": {}},
        stream_writer=lambda _: None,
        tools=[],
        tool_call_id="call-1",
        store=None,
    )


def test_sync_lookup_unwraps_existing_parent_without_taking_ownership() -> None:
    provider = _Provider(parent_exists=True)
    runtime = _runtime(provider)
    try:
        sandbox = ensure_sandbox_initialized(runtime)
    finally:
        reset_sandbox_provider()

    assert sandbox is provider.sandbox
    assert provider.acquired == []
    assert runtime.context["sandbox_id"] == "parent-sandbox"
    assert isinstance(runtime.state["sandbox"], Overwrite)


@pytest.mark.anyio
async def test_async_lookup_unwraps_existing_parent() -> None:
    provider = _Provider(parent_exists=True)
    runtime = _runtime(provider)
    try:
        sandbox = await ensure_sandbox_initialized_async(runtime)
    finally:
        reset_sandbox_provider()

    assert sandbox is provider.sandbox
    assert provider.acquired == []


@pytest.mark.parametrize("async_lookup", [False, True])
@pytest.mark.anyio
async def test_missing_wrapped_parent_is_replaced_by_fresh_sandbox(
    async_lookup: bool,
) -> None:
    provider = _Provider(parent_exists=False)
    runtime = _runtime(provider)
    try:
        if async_lookup:
            sandbox = await ensure_sandbox_initialized_async(runtime)
        else:
            sandbox = ensure_sandbox_initialized(runtime)
    finally:
        reset_sandbox_provider()

    assert sandbox is provider.sandbox
    assert provider.acquired == ["t-1"]
    assert runtime.state["sandbox"] == {"sandbox_id": "fresh-sandbox"}
