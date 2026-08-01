"""Remote sandbox commands must start from the virtual workspace."""

from langchain.tools import ToolRuntime

from deerflow.sandbox.sandbox import Sandbox
from deerflow.sandbox.sandbox_provider import (
    SandboxProvider,
    reset_sandbox_provider,
    set_sandbox_provider,
)
from deerflow.sandbox.search import GrepMatch
from deerflow.sandbox.tools import bash_tool


class _RecordingSandbox(Sandbox):
    def __init__(self) -> None:
        super().__init__("remote-1")
        self.commands: list[str] = []

    def execute_command(
        self,
        command: str,
        env: dict | None = None,
        timeout: float | None = None,
    ) -> str:
        del env, timeout
        self.commands.append(command)
        return "OK"

    def read_file(self, path: str) -> str:
        del path
        return ""

    def download_file(self, path: str) -> bytes:
        del path
        return b""

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
    def __init__(self, sandbox: _RecordingSandbox) -> None:
        self.sandbox = sandbox

    def acquire(
        self,
        thread_id: str | None = None,
        *,
        user_id: str | None = None,
    ) -> str:
        del thread_id, user_id
        return self.sandbox.id

    def get(self, sandbox_id: str) -> Sandbox | None:
        return self.sandbox if sandbox_id == self.sandbox.id else None

    def release(self, sandbox_id: str) -> None:
        del sandbox_id


def test_remote_bash_uses_fail_closed_workspace_cwd() -> None:
    sandbox = _RecordingSandbox()
    set_sandbox_provider(_Provider(sandbox))
    try:
        runtime = ToolRuntime(
            state={"sandbox": {"sandbox_id": sandbox.id}},
            context={"thread_id": "thread-1"},
            config={"configurable": {}},
            stream_writer=lambda _: None,
            tools=[],
            tool_call_id="call-1",
            store=None,
        )
        result = bash_tool.func(
            runtime=runtime,
            description="check remote cwd",
            command="pwd",
        )
    finally:
        reset_sandbox_provider()

    assert result == "OK"
    assert sandbox.commands == ["cd -- /mnt/user-data/workspace && pwd"]
