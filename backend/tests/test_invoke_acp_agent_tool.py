"""Tests for the built-in ACP invocation tool."""

import asyncio
import logging
import os
import shutil
import signal
import sys
import time
from types import SimpleNamespace

import pytest

from deerflow.config.acp_config import ACPAgentConfig
from deerflow.tools.builtins.invoke_acp_agent_tool import (
    _build_acp_mcp_servers,
    _build_mcp_servers,
    _build_permission_response,
    _get_work_dir,
    build_invoke_acp_agent_tool,
)
from deerflow.tools.tools import get_available_tools

_REAL_SHUTIL_WHICH = shutil.which


class _ReapedDummyProcess:
    """Minimal process contract for successful fake ACP contexts."""

    returncode = 0

    async def wait(self):
        return self.returncode


@pytest.fixture(autouse=True)
def _resolve_fake_acp_commands(monkeypatch):
    """Unit-test process contexts receive an isolated, absolute fake command."""

    monkeypatch.setattr(
        "deerflow.tools.builtins.invoke_acp_agent_tool.shutil.which",
        lambda command, path=None: command if os.path.isabs(command) else f"/resolved/{os.path.basename(command)}",
    )


def test_build_mcp_servers_rejects_ambient_extensions_config():
    assert _build_mcp_servers() == {}


def test_build_acp_mcp_servers_rejects_ambient_extensions_config():
    assert _build_acp_mcp_servers() == []


def test_acp_agent_timeout_defaults_and_validation():
    config = ACPAgentConfig(command="agent", description="Agent")
    assert config.timeout_seconds == 1800
    assert config.cleanup_timeout_seconds == 5.0
    with pytest.raises(ValueError):
        ACPAgentConfig(
            command="agent",
            description="Agent",
            timeout_seconds=0,
        )
    with pytest.raises(ValueError):
        ACPAgentConfig(
            command="agent",
            description="Agent",
            cleanup_timeout_seconds=0,
        )
    with pytest.raises(ValueError):
        ACPAgentConfig(
            command="agent",
            description="Agent",
            cleanup_timeout_seconds=31,
        )


def test_build_permission_response_prefers_allow_once():
    response = _build_permission_response(
        [
            SimpleNamespace(kind="reject_once", optionId="deny"),
            SimpleNamespace(kind="allow_always", optionId="always"),
            SimpleNamespace(kind="allow_once", optionId="once"),
        ],
        auto_approve=True,
    )

    assert response.outcome.outcome == "selected"
    assert response.outcome.option_id == "once"


def test_build_permission_response_denies_when_no_allow_option():
    response = _build_permission_response(
        [
            SimpleNamespace(kind="reject_once", optionId="deny"),
            SimpleNamespace(kind="reject_always", optionId="deny-forever"),
        ],
        auto_approve=True,
    )

    assert response.outcome.outcome == "cancelled"


def test_build_permission_response_denies_when_auto_approve_false():
    """P1.2: When auto_approve=False, permission is always denied regardless of options."""
    response = _build_permission_response(
        [
            SimpleNamespace(kind="allow_once", optionId="once"),
            SimpleNamespace(kind="allow_always", optionId="always"),
        ],
        auto_approve=False,
    )

    assert response.outcome.outcome == "cancelled"


@pytest.mark.anyio
async def test_build_invoke_tool_description_and_unknown_agent_error():
    tool = build_invoke_acp_agent_tool(
        {
            "codex": ACPAgentConfig(command="codex-acp", description="Codex CLI"),
            "claude_code": ACPAgentConfig(command="claude-code-acp", description="Claude Code"),
        }
    )

    assert "Available agents:" in tool.description
    assert "- codex: Codex CLI" in tool.description
    assert "- claude_code: Claude Code" in tool.description
    assert "Do NOT include /mnt/user-data paths" in tool.description
    assert "/mnt/acp-workspace/" in tool.description

    result = await tool.coroutine(agent="missing", prompt="do work")
    assert result == "Error: Unknown agent 'missing'. Available: codex, claude_code"


def test_get_work_dir_uses_base_dir_when_no_thread_id(monkeypatch, tmp_path):
    """_get_work_dir(None) uses {base_dir}/acp-workspace/ (global fallback)."""
    from deerflow.config import paths as paths_module

    monkeypatch.setattr(paths_module, "get_paths", lambda: paths_module.Paths(base_dir=tmp_path))
    result = _get_work_dir(None)
    expected = tmp_path / "acp-workspace"
    assert result == str(expected)
    assert expected.exists()


def test_get_work_dir_uses_per_thread_path_when_thread_id_given(monkeypatch, tmp_path):
    """P1.1: _get_work_dir(thread_id) uses {base_dir}/threads/{thread_id}/acp-workspace/."""
    from deerflow.config import paths as paths_module
    from deerflow.runtime import user_context as uc_module

    monkeypatch.setattr(paths_module, "get_paths", lambda: paths_module.Paths(base_dir=tmp_path))
    monkeypatch.setattr(uc_module, "get_effective_user_id", lambda: None)
    result = _get_work_dir("thread-abc-123")
    expected = tmp_path / "threads" / "thread-abc-123" / "acp-workspace"
    assert result == str(expected)
    assert expected.exists()


def test_get_work_dir_falls_back_to_global_for_invalid_thread_id(monkeypatch, tmp_path):
    """P1.1: Invalid thread_id (e.g. path traversal chars) falls back to global workspace."""
    from deerflow.config import paths as paths_module

    monkeypatch.setattr(paths_module, "get_paths", lambda: paths_module.Paths(base_dir=tmp_path))
    result = _get_work_dir("../../evil")
    expected = tmp_path / "acp-workspace"
    assert result == str(expected)
    assert expected.exists()


@pytest.mark.anyio
async def test_invoke_acp_agent_uses_fixed_acp_workspace(monkeypatch, tmp_path):
    """ACP agent uses {base_dir}/acp-workspace/ when no thread_id is available (no config)."""
    from deerflow.config import paths as paths_module

    monkeypatch.setattr(paths_module, "get_paths", lambda: paths_module.Paths(base_dir=tmp_path))
    captured: dict[str, object] = {}

    class DummyClient:
        def __init__(self) -> None:
            self._chunks: list[str] = []

        @property
        def collected_text(self) -> str:
            return "".join(self._chunks)

        async def session_update(self, session_id: str, update, **kwargs) -> None:
            if hasattr(update, "content") and hasattr(update.content, "text"):
                self._chunks.append(update.content.text)

        async def request_permission(self, options, session_id: str, tool_call, **kwargs):
            raise AssertionError("request_permission should not be called in this test")

    class DummyConn:
        async def initialize(self, **kwargs):
            captured["initialize"] = kwargs

        async def new_session(self, **kwargs):
            captured["new_session"] = kwargs
            return SimpleNamespace(session_id="session-1")

        async def prompt(self, **kwargs):
            captured["prompt"] = kwargs
            client = captured["client"]
            await client.session_update(
                "session-1",
                SimpleNamespace(content=text_content_block("ACP result")),
            )

    class DummyProcessContext:
        def __init__(self, client, cmd, *args, cwd):
            captured["client"] = client
            captured["spawn"] = {"cmd": cmd, "args": list(args), "cwd": cwd}

        async def __aenter__(self):
            return DummyConn(), _ReapedDummyProcess()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class DummyRequestError(Exception):
        @staticmethod
        def method_not_found(method: str):
            return DummyRequestError(method)

    monkeypatch.setitem(
        sys.modules,
        "acp",
        SimpleNamespace(
            PROTOCOL_VERSION="2026-03-24",
            Client=DummyClient,
            RequestError=DummyRequestError,
            spawn_agent_process=lambda client, cmd, *args, env=None, cwd: DummyProcessContext(client, cmd, *args, cwd=cwd),
            text_block=lambda text: {"type": "text", "text": text},
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "acp.schema",
        SimpleNamespace(
            ClientCapabilities=lambda: {"supports": []},
            Implementation=lambda **kwargs: kwargs,
            TextContentBlock=type(
                "TextContentBlock",
                (),
                {"__init__": lambda self, text: setattr(self, "text", text)},
            ),
        ),
    )
    text_content_block = sys.modules["acp.schema"].TextContentBlock

    expected_cwd = str(tmp_path / "acp-workspace")

    tool = build_invoke_acp_agent_tool(
        {
            "codex": ACPAgentConfig(
                command="codex-acp",
                args=["--json"],
                description="Codex CLI",
                model="gpt-5-codex",
            )
        }
    )

    try:
        result = await tool.coroutine(
            agent="codex",
            prompt="Implement the fix",
        )
    finally:
        sys.modules.pop("acp", None)
        sys.modules.pop("acp.schema", None)

    assert result == "ACP result"
    spawn = captured["spawn"]
    assert spawn["cmd"] == sys.executable
    assert spawn["args"][:3] == ["-I", "-S", "-c"]
    assert spawn["args"][4:] == ["/resolved/codex-acp", "--json"]
    assert spawn["cwd"] == expected_cwd
    assert captured["new_session"] == {
        "cwd": expected_cwd,
        "mcp_servers": [],
        "model": "gpt-5-codex",
    }
    assert captured["prompt"] == {
        "session_id": "session-1",
        "prompt": [{"type": "text", "text": "Implement the fix"}],
    }


@pytest.mark.anyio
async def test_invoke_acp_agent_uses_per_thread_workspace_when_thread_id_in_config(monkeypatch, tmp_path):
    """P1.1: When thread_id is in the RunnableConfig, ACP agent uses per-thread workspace."""
    from deerflow.config import paths as paths_module
    from deerflow.runtime import user_context as uc_module

    monkeypatch.setattr(paths_module, "get_paths", lambda: paths_module.Paths(base_dir=tmp_path))
    monkeypatch.setattr(uc_module, "get_effective_user_id", lambda: None)

    captured: dict[str, object] = {}

    class DummyClient:
        def __init__(self) -> None:
            self._chunks: list[str] = []

        @property
        def collected_text(self) -> str:
            return "".join(self._chunks)

        async def session_update(self, session_id, update, **kwargs):
            pass

        async def request_permission(self, options, session_id, tool_call, **kwargs):
            raise AssertionError("should not be called")

    class DummyConn:
        async def initialize(self, **kwargs):
            pass

        async def new_session(self, **kwargs):
            captured["new_session"] = kwargs
            return SimpleNamespace(session_id="s1")

        async def prompt(self, **kwargs):
            pass

    class DummyProcessContext:
        def __init__(self, client, cmd, *args, cwd):
            captured["cwd"] = cwd

        async def __aenter__(self):
            return DummyConn(), _ReapedDummyProcess()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class DummyRequestError(Exception):
        @staticmethod
        def method_not_found(method):
            return DummyRequestError(method)

    monkeypatch.setitem(
        sys.modules,
        "acp",
        SimpleNamespace(
            PROTOCOL_VERSION="2026-03-24",
            Client=DummyClient,
            RequestError=DummyRequestError,
            spawn_agent_process=lambda client, cmd, *args, env=None, cwd: DummyProcessContext(client, cmd, *args, cwd=cwd),
            text_block=lambda text: {"type": "text", "text": text},
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "acp.schema",
        SimpleNamespace(
            ClientCapabilities=lambda: {},
            Implementation=lambda **kwargs: kwargs,
            TextContentBlock=type("TextContentBlock", (), {"__init__": lambda self, text: setattr(self, "text", text)}),
        ),
    )

    thread_id = "thread-xyz-789"
    expected_cwd = str(tmp_path / "threads" / thread_id / "acp-workspace")

    tool = build_invoke_acp_agent_tool({"codex": ACPAgentConfig(command="codex-acp", description="Codex CLI")})

    try:
        await tool.coroutine(
            agent="codex",
            prompt="Do something",
            config={"configurable": {"thread_id": thread_id}},
        )
    finally:
        sys.modules.pop("acp", None)
        sys.modules.pop("acp.schema", None)

    assert captured["cwd"] == expected_cwd


@pytest.mark.anyio
async def test_invoke_acp_agent_passes_env_to_spawn(monkeypatch, tmp_path):
    """env map in ACPAgentConfig is passed to spawn_agent_process; $VAR values are resolved."""
    from deerflow.config import paths as paths_module

    monkeypatch.setattr(paths_module, "get_paths", lambda: paths_module.Paths(base_dir=tmp_path))
    monkeypatch.setenv("TEST_OPENAI_KEY", "sk-from-env")

    captured: dict[str, object] = {}

    class DummyClient:
        def __init__(self) -> None:
            self._chunks: list[str] = []

        @property
        def collected_text(self) -> str:
            return ""

        async def session_update(self, session_id, update, **kwargs):
            pass

        async def request_permission(self, options, session_id, tool_call, **kwargs):
            raise AssertionError("should not be called")

    class DummyConn:
        async def initialize(self, **kwargs):
            pass

        async def new_session(self, **kwargs):
            return SimpleNamespace(session_id="s1")

        async def prompt(self, **kwargs):
            pass

    class DummyProcessContext:
        def __init__(self, client, cmd, *args, env=None, cwd):
            captured["env"] = env

        async def __aenter__(self):
            return DummyConn(), _ReapedDummyProcess()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class DummyRequestError(Exception):
        @staticmethod
        def method_not_found(method):
            return DummyRequestError(method)

    monkeypatch.setitem(
        sys.modules,
        "acp",
        SimpleNamespace(
            PROTOCOL_VERSION="2026-03-24",
            Client=DummyClient,
            RequestError=DummyRequestError,
            spawn_agent_process=lambda client, cmd, *args, env=None, cwd: DummyProcessContext(client, cmd, *args, env=env, cwd=cwd),
            text_block=lambda text: {"type": "text", "text": text},
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "acp.schema",
        SimpleNamespace(
            ClientCapabilities=lambda: {},
            Implementation=lambda **kwargs: kwargs,
            TextContentBlock=type("TextContentBlock", (), {"__init__": lambda self, text: setattr(self, "text", text)}),
        ),
    )

    tool = build_invoke_acp_agent_tool(
        {
            "codex": ACPAgentConfig(
                command="codex-acp",
                description="Codex CLI",
                env={"OPENAI_API_KEY": "$TEST_OPENAI_KEY", "FOO": "bar"},
            )
        }
    )

    try:
        await tool.coroutine(agent="codex", prompt="Do something")
    finally:
        sys.modules.pop("acp", None)
        sys.modules.pop("acp.schema", None)

    assert captured["env"] == {"OPENAI_API_KEY": "sk-from-env", "FOO": "bar"}


@pytest.mark.anyio
async def test_invoke_acp_agent_skips_invalid_mcp_servers(monkeypatch, tmp_path, caplog):
    """Invalid MCP config should be logged and skipped instead of failing ACP invocation."""
    from deerflow.config import paths as paths_module

    monkeypatch.setattr(paths_module, "get_paths", lambda: paths_module.Paths(base_dir=tmp_path))
    monkeypatch.setattr(
        "deerflow.tools.builtins.invoke_acp_agent_tool._build_acp_mcp_servers",
        lambda: (_ for _ in ()).throw(ValueError("missing command")),
    )

    captured: dict[str, object] = {}

    class DummyClient:
        def __init__(self) -> None:
            self._chunks: list[str] = []

        @property
        def collected_text(self) -> str:
            return ""

        async def session_update(self, session_id, update, **kwargs):
            pass

        async def request_permission(self, options, session_id, tool_call, **kwargs):
            raise AssertionError("should not be called")

    class DummyConn:
        async def initialize(self, **kwargs):
            pass

        async def new_session(self, **kwargs):
            captured["new_session"] = kwargs
            return SimpleNamespace(session_id="s1")

        async def prompt(self, **kwargs):
            pass

    class DummyProcessContext:
        def __init__(self, client, cmd, *args, env=None, cwd=None):
            captured["spawn"] = {"cmd": cmd, "args": list(args), "env": env, "cwd": cwd}

        async def __aenter__(self):
            return DummyConn(), _ReapedDummyProcess()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class DummyRequestError(Exception):
        @staticmethod
        def method_not_found(method):
            return DummyRequestError(method)

    monkeypatch.setitem(
        sys.modules,
        "acp",
        SimpleNamespace(
            PROTOCOL_VERSION="2026-03-24",
            Client=DummyClient,
            RequestError=DummyRequestError,
            spawn_agent_process=lambda client, cmd, *args, env=None, cwd: DummyProcessContext(client, cmd, *args, env=env, cwd=cwd),
            text_block=lambda text: {"type": "text", "text": text},
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "acp.schema",
        SimpleNamespace(
            ClientCapabilities=lambda: {},
            Implementation=lambda **kwargs: kwargs,
            TextContentBlock=type("TextContentBlock", (), {"__init__": lambda self, text: setattr(self, "text", text)}),
        ),
    )

    tool = build_invoke_acp_agent_tool({"codex": ACPAgentConfig(command="codex-acp", description="Codex CLI")})
    caplog.set_level("WARNING")

    try:
        await tool.coroutine(agent="codex", prompt="Do something")
    finally:
        sys.modules.pop("acp", None)
        sys.modules.pop("acp.schema", None)

    assert captured["new_session"]["mcp_servers"] == []
    assert "continuing without MCP servers" in caplog.text
    assert "missing command" in caplog.text


@pytest.mark.anyio
async def test_invoke_acp_agent_passes_none_env_when_not_configured(monkeypatch, tmp_path):
    """When env is empty, None is passed to spawn_agent_process (subprocess inherits parent env)."""
    from deerflow.config import paths as paths_module

    monkeypatch.setattr(paths_module, "get_paths", lambda: paths_module.Paths(base_dir=tmp_path))
    captured: dict[str, object] = {}

    class DummyClient:
        def __init__(self) -> None:
            self._chunks: list[str] = []

        @property
        def collected_text(self) -> str:
            return ""

        async def session_update(self, session_id, update, **kwargs):
            pass

        async def request_permission(self, options, session_id, tool_call, **kwargs):
            raise AssertionError("should not be called")

    class DummyConn:
        async def initialize(self, **kwargs):
            pass

        async def new_session(self, **kwargs):
            return SimpleNamespace(session_id="s1")

        async def prompt(self, **kwargs):
            pass

    class DummyProcessContext:
        def __init__(self, client, cmd, *args, env=None, cwd):
            captured["env"] = env

        async def __aenter__(self):
            return DummyConn(), _ReapedDummyProcess()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class DummyRequestError(Exception):
        @staticmethod
        def method_not_found(method):
            return DummyRequestError(method)

    monkeypatch.setitem(
        sys.modules,
        "acp",
        SimpleNamespace(
            PROTOCOL_VERSION="2026-03-24",
            Client=DummyClient,
            RequestError=DummyRequestError,
            spawn_agent_process=lambda client, cmd, *args, env=None, cwd: DummyProcessContext(client, cmd, *args, env=env, cwd=cwd),
            text_block=lambda text: {"type": "text", "text": text},
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "acp.schema",
        SimpleNamespace(
            ClientCapabilities=lambda: {},
            Implementation=lambda **kwargs: kwargs,
            TextContentBlock=type("TextContentBlock", (), {"__init__": lambda self, text: setattr(self, "text", text)}),
        ),
    )

    tool = build_invoke_acp_agent_tool({"codex": ACPAgentConfig(command="codex-acp", description="Codex CLI")})

    try:
        await tool.coroutine(agent="codex", prompt="Do something")
    finally:
        sys.modules.pop("acp", None)
        sys.modules.pop("acp.schema", None)

    assert captured["env"] is None


@pytest.mark.anyio
async def test_invoke_acp_agent_times_out_full_lifecycle_and_exits_process(
    monkeypatch,
    tmp_path,
):
    """A hung initialize is bounded and the process context is always exited."""

    from deerflow.config import paths as paths_module

    monkeypatch.setattr(
        paths_module,
        "get_paths",
        lambda: paths_module.Paths(base_dir=tmp_path),
    )
    exited = asyncio.Event()

    class DummyClient:
        @property
        def collected_text(self) -> str:
            return ""

        async def session_update(self, session_id, update, **kwargs):
            return None

        async def request_permission(
            self,
            options,
            session_id,
            tool_call,
            **kwargs,
        ):
            raise AssertionError("should not be called")

    class HungConn:
        async def initialize(self, **kwargs):
            await asyncio.Event().wait()

        async def new_session(self, **kwargs):
            raise AssertionError("initialize must time out first")

        async def prompt(self, **kwargs):
            raise AssertionError("initialize must time out first")

    class DummyProcessContext:
        async def __aenter__(self):
            return HungConn(), _ReapedDummyProcess()

        async def __aexit__(self, exc_type, exc, tb):
            exited.set()
            return False

    monkeypatch.setitem(
        sys.modules,
        "acp",
        SimpleNamespace(
            PROTOCOL_VERSION="2026-03-24",
            Client=DummyClient,
            spawn_agent_process=lambda client, cmd, *args, env=None, cwd: DummyProcessContext(),
            text_block=lambda text: {"type": "text", "text": text},
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "acp.schema",
        SimpleNamespace(
            ClientCapabilities=lambda: {},
            Implementation=lambda **kwargs: kwargs,
            TextContentBlock=type(
                "TextContentBlock",
                (),
                {
                    "__init__": lambda self, text: setattr(
                        self,
                        "text",
                        text,
                    )
                },
            ),
        ),
    )
    tool = build_invoke_acp_agent_tool(
        {
            "hung": ACPAgentConfig(
                command="hung-acp",
                description="Hung test agent",
                timeout_seconds=1,
            )
        }
    )

    try:
        result = await asyncio.wait_for(
            tool.coroutine(agent="hung", prompt="do work"),
            timeout=5,
        )
    finally:
        sys.modules.pop("acp", None)
        sys.modules.pop("acp.schema", None)

    assert "timed out after 1 seconds" in result
    assert exited.is_set()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group behavior")
@pytest.mark.anyio
async def test_successful_acp_result_is_discarded_when_context_exit_hangs(
    monkeypatch,
    tmp_path,
):
    """A model result is not returned unless context cleanup is also verified."""

    from deerflow.config import paths as paths_module

    monkeypatch.setattr(
        paths_module,
        "get_paths",
        lambda: paths_module.Paths(base_dir=tmp_path),
    )
    monkeypatch.setattr(
        "deerflow.tools.builtins.invoke_acp_agent_tool.shutil.which",
        lambda command, path=None: "/resolved/acp-agent",
    )
    captured: dict[str, object] = {}
    exit_started = asyncio.Event()
    reaped = asyncio.Event()

    class DummyProcess:
        pid = 43210
        returncode = None

        async def wait(self):
            await reaped.wait()
            return self.returncode

        def terminate(self):
            raise AssertionError("isolated POSIX process must be terminated by group")

        def kill(self):
            raise AssertionError("isolated POSIX process must be killed by group")

    process = DummyProcess()
    signals: list[int] = []

    def fake_killpg(process_group_id: int, sent_signal: int) -> None:
        assert process_group_id == process.pid
        signals.append(sent_signal)
        if sent_signal == signal.SIGKILL:
            process.returncode = -sent_signal
            reaped.set()

    monkeypatch.setattr(
        "deerflow.tools.builtins.invoke_acp_agent_tool.os.killpg",
        fake_killpg,
    )

    class DummyClient:
        def __init__(self) -> None:
            self._chunks: list[str] = []

        @property
        def collected_text(self) -> str:
            return "".join(self._chunks)

        async def session_update(self, session_id, update, **kwargs):
            self._chunks.append(update.content.text)

        async def request_permission(self, options, session_id, tool_call, **kwargs):
            raise AssertionError("should not be called")

    class TextContentBlock:
        def __init__(self, text: str) -> None:
            self.text = text

    class DummyConn:
        async def initialize(self, **kwargs):
            return None

        async def new_session(self, **kwargs):
            return SimpleNamespace(session_id="session-1")

        async def prompt(self, **kwargs):
            await captured["client"].session_update(
                "session-1",
                SimpleNamespace(content=TextContentBlock("done")),
            )

    class DummyProcessContext:
        def __init__(self, client, cmd, *args, env=None, cwd=None):
            captured["client"] = client
            captured["cmd"] = cmd
            captured["args"] = list(args)

        async def __aenter__(self):
            return DummyConn(), process

        async def __aexit__(self, exc_type, exc, tb):
            exit_started.set()
            await asyncio.Event().wait()

    monkeypatch.setitem(
        sys.modules,
        "acp",
        SimpleNamespace(
            PROTOCOL_VERSION="2026-03-24",
            Client=DummyClient,
            spawn_agent_process=lambda client, cmd, *args, env=None, cwd=None: DummyProcessContext(
                client,
                cmd,
                *args,
                env=env,
                cwd=cwd,
            ),
            text_block=lambda text: {"type": "text", "text": text},
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "acp.schema",
        SimpleNamespace(
            ClientCapabilities=lambda: {},
            Implementation=lambda **kwargs: kwargs,
            TextContentBlock=TextContentBlock,
        ),
    )
    tool = build_invoke_acp_agent_tool(
        {
            "test": ACPAgentConfig(
                command="acp-agent",
                args=["--json"],
                description="Test agent",
                timeout_seconds=10,
                cleanup_timeout_seconds=0.2,
            )
        }
    )

    started_at = time.monotonic()
    result = await asyncio.wait_for(
        tool.coroutine(agent="test", prompt="do work"),
        timeout=1,
    )
    elapsed = time.monotonic() - started_at

    assert "cleanup could not be verified" in result
    assert "result was discarded" in result
    assert result != "done"
    assert elapsed < 0.8
    assert exit_started.is_set()
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert captured["cmd"] == sys.executable
    spawn_args = captured["args"]
    assert spawn_args[:3] == ["-I", "-S", "-c"]
    assert "os.setsid()" in spawn_args[3]
    assert spawn_args[4:] == ["/resolved/acp-agent", "--json"]


@pytest.mark.anyio
async def test_successful_context_exit_rejects_opaque_process_handle():
    """A result cannot pass when the process handle cannot prove reaping."""

    from deerflow.tools.builtins.invoke_acp_agent_tool import (
        _cleanup_process_context,
    )

    class SuccessfulExitContext:
        async def __aexit__(self, exc_type, exc, tb):
            return False

    request_task = asyncio.create_task(asyncio.sleep(0))
    await request_task
    failures = await _cleanup_process_context(
        {
            "context": SuccessfulExitContext(),
            "entered": True,
            "process": object(),
            "tasks": set(),
        },
        request_task,
        isolated_process_group=True,
        cleanup_timeout_seconds=0.1,
        force=False,
        exit_error=None,
    )

    assert failures == ("subprocess does not expose a wait method",)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group behavior")
@pytest.mark.anyio
async def test_real_posix_process_is_killed_reaped_and_result_discarded(
    monkeypatch,
    tmp_path,
):
    """A real TERM-resistant child is group-killed and wait-confirmed."""

    from deerflow.config import paths as paths_module

    monkeypatch.setattr(
        paths_module,
        "get_paths",
        lambda: paths_module.Paths(base_dir=tmp_path),
    )
    process_holder: dict[str, asyncio.subprocess.Process] = {}

    class DummyClient:
        def __init__(self) -> None:
            self._chunks: list[str] = []

        @property
        def collected_text(self) -> str:
            return "".join(self._chunks)

        async def session_update(self, session_id, update, **kwargs):
            self._chunks.append(update.content.text)

        async def request_permission(self, options, session_id, tool_call, **kwargs):
            raise AssertionError("should not be called")

    class TextContentBlock:
        def __init__(self, text: str) -> None:
            self.text = text

    class DummyConn:
        async def initialize(self, **kwargs):
            return None

        async def new_session(self, **kwargs):
            return SimpleNamespace(session_id="session-1")

        async def prompt(self, **kwargs):
            await client_holder["client"].session_update(
                "session-1",
                SimpleNamespace(content=TextContentBlock("real-process-result")),
            )

    client_holder: dict[str, DummyClient] = {}

    class RealProcessContext:
        def __init__(self, client):
            client_holder["client"] = client

        async def __aenter__(self):
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-S",
                "-c",
                ("import signal, sys, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nprint('READY', flush=True)\ntime.sleep(60)\n"),
                stdout=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            process_holder["process"] = process
            assert process.stdout is not None
            assert (
                await asyncio.wait_for(
                    process.stdout.readline(),
                    timeout=1,
                )
                == b"READY\n"
            )
            return DummyConn(), process

        async def __aexit__(self, exc_type, exc, tb):
            await asyncio.Event().wait()

    monkeypatch.setitem(
        sys.modules,
        "acp",
        SimpleNamespace(
            PROTOCOL_VERSION="2026-03-24",
            Client=DummyClient,
            spawn_agent_process=lambda client, cmd, *args, env=None, cwd=None: RealProcessContext(client),
            text_block=lambda text: {"type": "text", "text": text},
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "acp.schema",
        SimpleNamespace(
            ClientCapabilities=lambda: {},
            Implementation=lambda **kwargs: kwargs,
            TextContentBlock=TextContentBlock,
        ),
    )
    tool = build_invoke_acp_agent_tool(
        {
            "real": ACPAgentConfig(
                command="real-acp",
                description="Real cleanup test agent",
                timeout_seconds=10,
                cleanup_timeout_seconds=0.4,
            )
        }
    )

    try:
        result = await asyncio.wait_for(
            tool.coroutine(agent="real", prompt="do work"),
            timeout=2,
        )
        process = process_holder["process"]

        assert "cleanup could not be verified" in result
        assert "result was discarded" in result
        assert "real-process-result" not in result
        assert process.returncode == -signal.SIGKILL
    finally:
        process = process_holder.get("process")
        if process is not None and process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


@pytest.mark.skipif(os.name != "posix", reason="POSIX command resolution")
@pytest.mark.anyio
async def test_posix_relative_path_is_resolved_from_acp_workspace(
    monkeypatch,
    tmp_path,
):
    """A relative PATH entry is anchored to the ACP workspace, not Gateway cwd."""

    from deerflow.tools.builtins.invoke_acp_agent_tool import _build_spawn_command

    monkeypatch.setattr(
        "deerflow.tools.builtins.invoke_acp_agent_tool.shutil.which",
        _REAL_SHUTIL_WHICH,
    )
    work_dir = tmp_path / "acp-workspace"
    bin_dir = work_dir / "bin"
    bin_dir.mkdir(parents=True)
    executable = bin_dir / "relative-acp"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)

    spawn_cmd, spawn_args, isolated = _build_spawn_command(
        "relative-acp",
        ["--json"],
        {"PATH": "bin"},
        cwd=str(work_dir),
    )

    assert spawn_cmd == sys.executable
    assert spawn_args[:3] == ["-I", "-S", "-c"]
    assert spawn_args[4:] == [str(executable.resolve()), "--json"]
    assert isolated is True

    process_env = dict(os.environ)
    process_env["PATH"] = "bin"
    process = await asyncio.create_subprocess_exec(
        spawn_cmd,
        *spawn_args,
        cwd=work_dir,
        env=process_env,
    )
    assert await process.wait() == 0


@pytest.mark.skipif(os.name != "posix", reason="POSIX Python wrapper")
@pytest.mark.anyio
async def test_posix_wrapper_ignores_pythonpath_sitecustomize(tmp_path):
    """Workspace Python startup hooks cannot run before the wrapper calls setsid."""

    from deerflow.tools.builtins.invoke_acp_agent_tool import _build_spawn_command

    marker = tmp_path / "wrapper-compromised"
    sitecustomize = tmp_path / "sitecustomize.py"
    sitecustomize.write_text("import os\nwith open(os.environ['ACP_WRAPPER_MARKER'], 'w', encoding='utf-8') as marker:\n    marker.write('injected')\n")
    true_command = "/usr/bin/true"
    if not os.path.exists(true_command):
        pytest.skip("/usr/bin/true is unavailable")

    spawn_cmd, spawn_args, isolated = _build_spawn_command(
        true_command,
        [],
        {
            "PYTHONPATH": str(tmp_path),
            "ACP_WRAPPER_MARKER": str(marker),
        },
        cwd=str(tmp_path),
    )
    process_env = dict(os.environ)
    process_env.update(
        {
            "PYTHONPATH": str(tmp_path),
            "ACP_WRAPPER_MARKER": str(marker),
        }
    )
    process = await asyncio.create_subprocess_exec(
        spawn_cmd,
        *spawn_args,
        cwd=tmp_path,
        env=process_env,
    )

    assert await process.wait() == 0
    assert isolated is True
    assert not marker.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group behavior")
@pytest.mark.anyio
async def test_request_timeout_plus_cleanup_has_hard_bound(monkeypatch, tmp_path):
    """Request timeout and cleanup timeout are independent hard bounds."""

    from deerflow.config import paths as paths_module

    monkeypatch.setattr(
        paths_module,
        "get_paths",
        lambda: paths_module.Paths(base_dir=tmp_path),
    )
    monkeypatch.setattr(
        "deerflow.tools.builtins.invoke_acp_agent_tool.shutil.which",
        lambda command, path=None: "/resolved/hung-acp",
    )
    reaped = asyncio.Event()

    class DummyProcess:
        pid = 43211
        returncode = None

        async def wait(self):
            await reaped.wait()
            return self.returncode

    process = DummyProcess()
    signals: list[int] = []

    def fake_killpg(process_group_id: int, sent_signal: int) -> None:
        assert process_group_id == process.pid
        signals.append(sent_signal)
        if sent_signal == signal.SIGKILL:
            process.returncode = -sent_signal
            reaped.set()

    monkeypatch.setattr(
        "deerflow.tools.builtins.invoke_acp_agent_tool.os.killpg",
        fake_killpg,
    )

    class DummyClient:
        @property
        def collected_text(self) -> str:
            return ""

        async def session_update(self, session_id, update, **kwargs):
            return None

        async def request_permission(self, options, session_id, tool_call, **kwargs):
            raise AssertionError("should not be called")

    class HungConn:
        async def initialize(self, **kwargs):
            await asyncio.Event().wait()

    class DummyProcessContext:
        async def __aenter__(self):
            return HungConn(), process

        async def __aexit__(self, exc_type, exc, tb):
            await asyncio.Event().wait()

    monkeypatch.setitem(
        sys.modules,
        "acp",
        SimpleNamespace(
            PROTOCOL_VERSION="2026-03-24",
            Client=DummyClient,
            spawn_agent_process=lambda client, cmd, *args, env=None, cwd=None: DummyProcessContext(),
            text_block=lambda text: {"type": "text", "text": text},
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "acp.schema",
        SimpleNamespace(
            ClientCapabilities=lambda: {},
            Implementation=lambda **kwargs: kwargs,
            TextContentBlock=type("TextContentBlock", (), {}),
        ),
    )
    tool = build_invoke_acp_agent_tool(
        {
            "hung": ACPAgentConfig(
                command="hung-acp",
                description="Hung agent",
                timeout_seconds=1,
                cleanup_timeout_seconds=0.2,
            )
        }
    )

    started_at = time.monotonic()
    result = await asyncio.wait_for(
        tool.coroutine(agent="hung", prompt="do work"),
        timeout=1.8,
    )
    elapsed = time.monotonic() - started_at

    assert "timed out after 1 seconds" in result
    assert elapsed < 1.6
    assert signals == [signal.SIGTERM, signal.SIGKILL]


@pytest.mark.skipif(os.name != "posix", reason="POSIX lifecycle cleanup")
@pytest.mark.anyio
async def test_hanging_aenter_is_tracked_and_reported_as_cleanup_failure(
    monkeypatch,
    tmp_path,
):
    """An uncooperative __aenter__ is quarantined and never silently detached."""

    from deerflow.config import paths as paths_module

    monkeypatch.setattr(
        paths_module,
        "get_paths",
        lambda: paths_module.Paths(base_dir=tmp_path),
    )
    monkeypatch.setattr(
        "deerflow.tools.builtins.invoke_acp_agent_tool.shutil.which",
        lambda command, path=None: "/resolved/hung-enter-acp",
    )
    release_enter = asyncio.Event()
    enter_cancelled = asyncio.Event()

    class DummyClient:
        @property
        def collected_text(self) -> str:
            return ""

        async def session_update(self, session_id, update, **kwargs):
            return None

        async def request_permission(self, options, session_id, tool_call, **kwargs):
            raise AssertionError("should not be called")

    class HangingEnterContext:
        async def __aenter__(self):
            while not release_enter.is_set():
                try:
                    await release_enter.wait()
                except asyncio.CancelledError:
                    enter_cancelled.set()
            raise asyncio.CancelledError

        async def __aexit__(self, exc_type, exc, tb):
            raise AssertionError("__aexit__ cannot run before __aenter__ returns")

    monkeypatch.setitem(
        sys.modules,
        "acp",
        SimpleNamespace(
            PROTOCOL_VERSION="2026-03-24",
            Client=DummyClient,
            spawn_agent_process=lambda client, cmd, *args, env=None, cwd=None: HangingEnterContext(),
            text_block=lambda text: {"type": "text", "text": text},
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "acp.schema",
        SimpleNamespace(
            ClientCapabilities=lambda: {},
            Implementation=lambda **kwargs: kwargs,
            TextContentBlock=type("TextContentBlock", (), {}),
        ),
    )
    tool = build_invoke_acp_agent_tool(
        {
            "hung": ACPAgentConfig(
                command="hung-enter-acp",
                description="Hung enter agent",
                timeout_seconds=1,
                cleanup_timeout_seconds=0.05,
            )
        }
    )

    from deerflow.tools.builtins.invoke_acp_agent_tool import (
        _UNCLEAN_LIFECYCLE_TASKS,
    )

    try:
        result = await asyncio.wait_for(
            tool.coroutine(agent="hung", prompt="do work"),
            timeout=2,
        )

        assert "cleanup could not be verified" in result
        assert enter_cancelled.is_set()
        assert any(not task.done() for task in _UNCLEAN_LIFECYCLE_TASKS)
    finally:
        release_enter.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert not [task for task in _UNCLEAN_LIFECYCLE_TASKS if not task.done()]


@pytest.mark.skipif(os.name != "posix", reason="POSIX lifecycle cleanup")
@pytest.mark.anyio
async def test_hanging_process_wait_is_tracked_and_forces_explicit_failure(
    monkeypatch,
    tmp_path,
):
    """TERM/KILL is insufficient: cleanup fails unless wait confirms reaping."""

    from deerflow.config import paths as paths_module

    monkeypatch.setattr(
        paths_module,
        "get_paths",
        lambda: paths_module.Paths(base_dir=tmp_path),
    )
    monkeypatch.setattr(
        "deerflow.tools.builtins.invoke_acp_agent_tool.shutil.which",
        lambda command, path=None: "/resolved/hung-wait-acp",
    )
    release_wait = asyncio.Event()
    exit_started = asyncio.Event()

    class DummyProcess:
        pid = 43213
        returncode = None

        async def wait(self):
            await release_wait.wait()
            self.returncode = -signal.SIGKILL
            return self.returncode

    process = DummyProcess()
    signals: list[int] = []

    def fake_killpg(process_group_id: int, sent_signal: int) -> None:
        assert process_group_id == process.pid
        signals.append(sent_signal)

    monkeypatch.setattr(
        "deerflow.tools.builtins.invoke_acp_agent_tool.os.killpg",
        fake_killpg,
    )

    class DummyClient:
        def __init__(self) -> None:
            self._chunks = ["model-success-must-not-escape"]

        @property
        def collected_text(self) -> str:
            return "".join(self._chunks)

        async def session_update(self, session_id, update, **kwargs):
            return None

        async def request_permission(self, options, session_id, tool_call, **kwargs):
            raise AssertionError("should not be called")

    class DummyConn:
        async def initialize(self, **kwargs):
            return None

        async def new_session(self, **kwargs):
            return SimpleNamespace(session_id="session-1")

        async def prompt(self, **kwargs):
            return None

    class HangingExitContext:
        async def __aenter__(self):
            return DummyConn(), process

        async def __aexit__(self, exc_type, exc, tb):
            exit_started.set()
            return False

    monkeypatch.setitem(
        sys.modules,
        "acp",
        SimpleNamespace(
            PROTOCOL_VERSION="2026-03-24",
            Client=DummyClient,
            spawn_agent_process=lambda client, cmd, *args, env=None, cwd=None: HangingExitContext(),
            text_block=lambda text: {"type": "text", "text": text},
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "acp.schema",
        SimpleNamespace(
            ClientCapabilities=lambda: {},
            Implementation=lambda **kwargs: kwargs,
            TextContentBlock=type("TextContentBlock", (), {}),
        ),
    )
    tool = build_invoke_acp_agent_tool(
        {
            "hung": ACPAgentConfig(
                command="hung-wait-acp",
                description="Hung wait agent",
                timeout_seconds=10,
                cleanup_timeout_seconds=0.1,
            )
        }
    )

    from deerflow.tools.builtins.invoke_acp_agent_tool import (
        _UNCLEAN_LIFECYCLE_TASKS,
    )

    try:
        result = await asyncio.wait_for(
            tool.coroutine(agent="hung", prompt="do work"),
            timeout=1,
        )

        assert "cleanup could not be verified" in result
        assert "result was discarded" in result
        assert "model-success-must-not-escape" not in result
        assert exit_started.is_set()
        assert signals == [signal.SIGTERM, signal.SIGKILL]
        assert any(not task.done() for task in _UNCLEAN_LIFECYCLE_TASKS)
        blocked_result = await tool.coroutine(agent="hung", prompt="second call")
        assert "previous invocation is still incomplete" in blocked_result
    finally:
        release_wait.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert not [task for task in _UNCLEAN_LIFECYCLE_TASKS if not task.done()]


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group behavior")
@pytest.mark.anyio
async def test_cancellation_reaps_then_reraises_cancelled_error(monkeypatch, tmp_path):
    """Caller cancellation waits only for bounded cleanup, then remains cancellation."""

    from deerflow.config import paths as paths_module

    monkeypatch.setattr(
        paths_module,
        "get_paths",
        lambda: paths_module.Paths(base_dir=tmp_path),
    )
    monkeypatch.setattr(
        "deerflow.tools.builtins.invoke_acp_agent_tool.shutil.which",
        lambda command, path=None: "/resolved/hung-acp",
    )
    prompt_started = asyncio.Event()
    reaped = asyncio.Event()

    class DummyProcess:
        pid = 43212
        returncode = None

        async def wait(self):
            await reaped.wait()
            return self.returncode

    process = DummyProcess()
    signals: list[int] = []

    def fake_killpg(process_group_id: int, sent_signal: int) -> None:
        assert process_group_id == process.pid
        signals.append(sent_signal)
        if sent_signal == signal.SIGKILL:
            process.returncode = -sent_signal
            reaped.set()

    monkeypatch.setattr(
        "deerflow.tools.builtins.invoke_acp_agent_tool.os.killpg",
        fake_killpg,
    )

    class DummyClient:
        @property
        def collected_text(self) -> str:
            return ""

        async def session_update(self, session_id, update, **kwargs):
            return None

        async def request_permission(self, options, session_id, tool_call, **kwargs):
            raise AssertionError("should not be called")

    class HungConn:
        async def initialize(self, **kwargs):
            return None

        async def new_session(self, **kwargs):
            return SimpleNamespace(session_id="session-1")

        async def prompt(self, **kwargs):
            prompt_started.set()
            await asyncio.Event().wait()

    class DummyProcessContext:
        async def __aenter__(self):
            return HungConn(), process

        async def __aexit__(self, exc_type, exc, tb):
            await asyncio.Event().wait()

    monkeypatch.setitem(
        sys.modules,
        "acp",
        SimpleNamespace(
            PROTOCOL_VERSION="2026-03-24",
            Client=DummyClient,
            spawn_agent_process=lambda client, cmd, *args, env=None, cwd=None: DummyProcessContext(),
            text_block=lambda text: {"type": "text", "text": text},
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "acp.schema",
        SimpleNamespace(
            ClientCapabilities=lambda: {},
            Implementation=lambda **kwargs: kwargs,
            TextContentBlock=type("TextContentBlock", (), {}),
        ),
    )
    tool = build_invoke_acp_agent_tool(
        {
            "hung": ACPAgentConfig(
                command="hung-acp",
                description="Hung agent",
                timeout_seconds=10,
                cleanup_timeout_seconds=0.2,
            )
        }
    )
    invoke_task = asyncio.create_task(
        tool.coroutine(agent="hung", prompt="do work"),
    )
    await asyncio.wait_for(prompt_started.wait(), timeout=1)

    started_at = time.monotonic()
    invoke_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(invoke_task, timeout=1)
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.8
    assert signals == [signal.SIGTERM, signal.SIGKILL]


@pytest.mark.anyio
async def test_invoke_acp_agent_logs_no_prompt_or_result_content(
    monkeypatch,
    tmp_path,
    caplog,
):
    """ACP prompts and streamed results never enter application logs."""

    from deerflow.config import paths as paths_module

    monkeypatch.setattr(
        paths_module,
        "get_paths",
        lambda: paths_module.Paths(base_dir=tmp_path),
    )
    prompt_secret = "PROMPT-SECRET-0f84"
    result_secret = "RESULT-SECRET-8a16"
    captured: dict[str, object] = {}

    class DummyClient:
        def __init__(self) -> None:
            self._chunks: list[str] = []

        @property
        def collected_text(self) -> str:
            return "".join(self._chunks)

        async def session_update(self, session_id, update, **kwargs):
            self._chunks.append(update.content.text)

        async def request_permission(self, options, session_id, tool_call, **kwargs):
            raise AssertionError("should not be called")

    class TextContentBlock:
        def __init__(self, text: str) -> None:
            self.text = text

    class DummyConn:
        async def initialize(self, **kwargs):
            return None

        async def new_session(self, **kwargs):
            return SimpleNamespace(session_id="session-1")

        async def prompt(self, **kwargs):
            await captured["client"].session_update(
                "session-1",
                SimpleNamespace(content=TextContentBlock(result_secret)),
            )

    class DummyProcessContext:
        def __init__(self, client):
            captured["client"] = client

        async def __aenter__(self):
            return DummyConn(), _ReapedDummyProcess()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setitem(
        sys.modules,
        "acp",
        SimpleNamespace(
            PROTOCOL_VERSION="2026-03-24",
            Client=DummyClient,
            spawn_agent_process=lambda client, cmd, *args, env=None, cwd=None: DummyProcessContext(client),
            text_block=lambda text: {"type": "text", "text": text},
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "acp.schema",
        SimpleNamespace(
            ClientCapabilities=lambda: {},
            Implementation=lambda **kwargs: kwargs,
            TextContentBlock=TextContentBlock,
        ),
    )
    tool = build_invoke_acp_agent_tool(
        {
            "test": ACPAgentConfig(
                command="test-acp",
                description="Test agent",
            )
        }
    )
    caplog.set_level(
        logging.DEBUG,
        logger="deerflow.tools.builtins.invoke_acp_agent_tool",
    )

    result = await tool.coroutine(agent="test", prompt=prompt_secret)

    assert result == result_secret
    assert prompt_secret not in caplog.text
    assert result_secret not in caplog.text


def test_get_available_tools_includes_invoke_acp_agent_when_agents_configured(monkeypatch):
    from deerflow.config.acp_config import load_acp_config_from_dict

    load_acp_config_from_dict(
        {
            "codex": {
                "command": "codex-acp",
                "args": [],
                "description": "Codex CLI",
            }
        }
    )

    fake_config = SimpleNamespace(
        tools=[],
        models=[],
        tool_search=SimpleNamespace(enabled=False),
        get_model_config=lambda name: None,
    )
    monkeypatch.setattr("deerflow.tools.tools.get_app_config", lambda: fake_config)
    tools = get_available_tools(include_mcp=True, subagent_enabled=False)
    assert "invoke_acp_agent" in [tool.name for tool in tools]

    load_acp_config_from_dict({})


def test_get_available_tools_sync_invoke_acp_agent_preserves_thread_workspace(monkeypatch, tmp_path):
    from deerflow.config import paths as paths_module
    from deerflow.runtime import user_context as uc_module

    monkeypatch.setattr(paths_module, "get_paths", lambda: paths_module.Paths(base_dir=tmp_path))
    monkeypatch.setattr(uc_module, "get_effective_user_id", lambda: None)
    monkeypatch.setattr("deerflow.tools.tools.is_host_bash_allowed", lambda config=None: True)

    captured: dict[str, object] = {}

    class DummyClient:
        @property
        def collected_text(self) -> str:
            return "ok"

        async def session_update(self, session_id, update, **kwargs):
            pass

        async def request_permission(self, options, session_id, tool_call, **kwargs):
            raise AssertionError("should not be called")

    class DummyConn:
        async def initialize(self, **kwargs):
            pass

        async def new_session(self, **kwargs):
            return SimpleNamespace(session_id="s1")

        async def prompt(self, **kwargs):
            pass

    class DummyProcessContext:
        def __init__(self, client, cmd, *args, env=None, cwd):
            captured["cwd"] = cwd

        async def __aenter__(self):
            return DummyConn(), _ReapedDummyProcess()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setitem(
        sys.modules,
        "acp",
        SimpleNamespace(
            PROTOCOL_VERSION="2026-03-24",
            Client=DummyClient,
            spawn_agent_process=lambda client, cmd, *args, env=None, cwd: DummyProcessContext(client, cmd, *args, env=env, cwd=cwd),
            text_block=lambda text: {"type": "text", "text": text},
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "acp.schema",
        SimpleNamespace(
            ClientCapabilities=lambda: {},
            Implementation=lambda **kwargs: kwargs,
            TextContentBlock=type("TextContentBlock", (), {"__init__": lambda self, text: setattr(self, "text", text)}),
        ),
    )

    explicit_config = SimpleNamespace(
        tools=[],
        models=[],
        tool_search=SimpleNamespace(enabled=False),
        sandbox=SimpleNamespace(),
        get_model_config=lambda name: None,
        acp_agents={"codex": ACPAgentConfig(command="codex-acp", description="Codex CLI")},
    )
    tools = get_available_tools(include_mcp=False, subagent_enabled=False, app_config=explicit_config)
    tool = next(tool for tool in tools if tool.name == "invoke_acp_agent")

    thread_id = "thread-sync-123"
    tool.invoke(
        {"agent": "codex", "prompt": "Do something"},
        config={"configurable": {"thread_id": thread_id}},
    )

    assert captured["cwd"] == str(tmp_path / "threads" / thread_id / "acp-workspace")


def test_get_available_tools_uses_explicit_app_config_for_acp_agents(monkeypatch):
    explicit_agents = {"codex": ACPAgentConfig(command="codex-acp", description="Codex CLI")}
    explicit_config = SimpleNamespace(
        tools=[],
        models=[],
        tool_search=SimpleNamespace(enabled=False),
        get_model_config=lambda name: None,
        acp_agents=explicit_agents,
    )
    sentinel_tool = SimpleNamespace(name="invoke_acp_agent")
    captured: dict[str, object] = {}

    def fail_get_acp_agents():
        raise AssertionError("ambient get_acp_agents() must not be used when app_config is explicit")

    def fake_build_invoke_acp_agent_tool(agents):
        captured["agents"] = agents
        return sentinel_tool

    monkeypatch.setattr("deerflow.tools.tools.is_host_bash_allowed", lambda config=None: True)
    monkeypatch.setattr("deerflow.config.acp_config.get_acp_agents", fail_get_acp_agents)
    monkeypatch.setattr("deerflow.tools.builtins.invoke_acp_agent_tool.build_invoke_acp_agent_tool", fake_build_invoke_acp_agent_tool)

    tools = get_available_tools(include_mcp=False, subagent_enabled=False, app_config=explicit_config)

    assert captured["agents"] is explicit_agents
    assert "invoke_acp_agent" in [tool.name for tool in tools]
