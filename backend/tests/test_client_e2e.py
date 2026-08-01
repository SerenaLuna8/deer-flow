"""End-to-end tests for DeerFlowClient.

Middle tier of the test pyramid:
- Top:    test_client_live.py  — real LLM, needs API key
- Middle: test_client_e2e.py   — real LLM + real modules  ← THIS FILE
- Bottom: test_client.py       — unit tests, mock everything

Core principle: use the real LLM from config.yaml, let config, middleware
chain, tool registration, file I/O, and event serialization all run for real.
Only DEER_FLOW_HOME is redirected to tmp_path for filesystem isolation.

Tests that call the LLM are marked ``requires_llm`` and skipped in CI.
File-management tests (upload/list/delete) don't need LLM and run everywhere.
"""

import os
import uuid
from pathlib import Path

import pytest
from dotenv import load_dotenv

from deerflow.client import DeerFlowClient, StreamEvent
from deerflow.config.app_config import AppConfig

# Load .env from project root (for OPENAI_API_KEY etc.)
load_dotenv(os.path.join(os.path.dirname(__file__), "../../.env"))

# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------

requires_llm = pytest.mark.skipif(
    os.getenv("CI", "").lower() in ("true", "1") or not os.getenv("OPENAI_API_KEY"),
    reason="Requires LLM API key — skipped in CI or when OPENAI_API_KEY is unset",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_e2e_config() -> AppConfig:
    """Build a minimal AppConfig using real LLM credentials from environment.

    All LLM connection details come from environment variables so that both
    internal CI and external contributors can run the tests:

    - ``E2E_MODEL_NAME``  (default: ``volcengine-ark``)
    - ``E2E_MODEL_USE``   (default: ``langchain_openai:ChatOpenAI``)
    - ``E2E_MODEL_ID``    (default: ``ep-20251211175242-llcmh``)
    - ``E2E_BASE_URL``    (default: ``https://ark-cn-beijing.bytedance.net/api/v3``)
    - ``OPENAI_API_KEY``  (required for LLM tests)

    Note: We use model_validate with a raw dict (not AppConfig(models=[ModelConfig(...)]))
    because passing already-validated Pydantic instances triggers a pydantic-core
    shortcut that returns stale cached data when another AppConfig was previously
    loaded from disk in the same process. Dict-based validation is always correct.
    """
    return AppConfig.model_validate(
        {
            "models": [
                {
                    "name": os.getenv("E2E_MODEL_NAME", "volcengine-ark"),
                    "display_name": "E2E Test Model",
                    "use": os.getenv("E2E_MODEL_USE", "langchain_openai:ChatOpenAI"),
                    "model": os.getenv("E2E_MODEL_ID", "ep-20251211175242-llcmh"),
                    "base_url": os.getenv("E2E_BASE_URL", "https://ark-cn-beijing.bytedance.net/api/v3"),
                    "api_key": os.getenv("OPENAI_API_KEY", ""),
                    "max_tokens": 512,
                    "temperature": 0.7,
                    "supports_thinking": False,
                    "supports_reasoning_effort": False,
                    "supports_vision": False,
                }
            ],
            "sandbox": {
                "use": "deerflow.sandbox.local:LocalSandboxProvider",
                "allow_host_bash": True,
            },
            "memory": {"enabled": False},
        }
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def e2e_env(tmp_path, monkeypatch):
    """Isolated filesystem environment for E2E tests.

    - DEER_FLOW_HOME → tmp_path (all thread data lands in a temp dir)
    - DEER_FLOW_PROJECT_ROOT → repository root (shared skills/config assets
      still resolve correctly when tests run from backend/)
    - Singletons reset so they pick up the new env
    - Title/memory/summarization disabled to avoid extra LLM calls
    - AppConfig built programmatically (avoids config.yaml param-name issues)
    """
    # 1. Filesystem isolation
    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
    monkeypatch.setenv(
        "DEER_FLOW_PROJECT_ROOT",
        str(Path(__file__).resolve().parents[2]),
    )
    monkeypatch.setattr("deerflow.config.paths._paths", None)
    monkeypatch.setattr("deerflow.sandbox.sandbox_provider._default_sandbox_provider", None)

    # 2. Inject a clean AppConfig. We must reset _app_config to None BEFORE
    # calling _make_e2e_config() because AppConfig() constructor misbehaves when
    # a disk config is already cached: it returns the cached model list instead
    # of the provided one. Clearing first ensures the test config is correct.
    monkeypatch.setattr("deerflow.config.app_config._app_config", None)
    monkeypatch.setattr("deerflow.config.app_config._app_config_is_custom", False)
    config = _make_e2e_config()
    monkeypatch.setattr("deerflow.config.app_config._app_config", config)
    monkeypatch.setattr("deerflow.config.app_config._app_config_is_custom", True)
    monkeypatch.setattr("deerflow.client.get_app_config", lambda: config)

    # 3. Disable title generation (extra LLM call, non-deterministic)
    from deerflow.config.title_config import TitleConfig

    monkeypatch.setattr("deerflow.config.title_config._title_config", TitleConfig(enabled=False))

    # 4. Memory is disabled in the admitted AppConfig above so middleware never
    # queues background writes.

    # 5. Ensure summarization is off (default, but be explicit)
    from deerflow.config.summarization_config import SummarizationConfig

    monkeypatch.setattr("deerflow.config.summarization_config._summarization_config", SummarizationConfig(enabled=False))

    # 6. Exclude TitleMiddleware from the chain.
    #    It triggers an extra LLM call to generate a thread title, which adds
    #    non-determinism and cost to E2E tests (title generation is already
    #    disabled via TitleConfig above, but the middleware still participates
    #    in the chain and can interfere with event ordering).
    from deerflow.agents.lead_agent.agent import build_middlewares as _original_build_middlewares
    from deerflow.agents.middlewares.title_middleware import TitleMiddleware

    def _sync_safe_build_middlewares(*args, **kwargs):
        mws = _original_build_middlewares(*args, **kwargs)
        return [m for m in mws if not isinstance(m, TitleMiddleware)]

    monkeypatch.setattr("deerflow.client.build_middlewares", _sync_safe_build_middlewares)

    return {"tmp_path": tmp_path}


@pytest.fixture()
def client(e2e_env):
    """A DeerFlowClient wired to the isolated e2e_env."""
    return DeerFlowClient(checkpointer=None, thinking_enabled=False)


# ---------------------------------------------------------------------------
# Step 2: Basic streaming (requires LLM)
# ---------------------------------------------------------------------------


class TestBasicChat:
    """Basic chat and streaming behavior with real LLM."""

    @requires_llm
    def test_basic_chat(self, client):
        """chat() returns a non-empty text response."""
        result = client.chat("Say exactly: pong")
        assert isinstance(result, str)
        assert len(result) > 0

    @requires_llm
    def test_stream_event_sequence(self, client):
        """stream() yields events: messages-tuple, values, and end."""
        events = list(client.stream("Say hi"))

        types = [e.type for e in events]
        assert types[-1] == "end"
        assert "messages-tuple" in types
        assert "values" in types

    @requires_llm
    def test_stream_event_data_format(self, client):
        """Each event type has the expected data structure."""
        events = list(client.stream("Say hello"))

        for event in events:
            assert isinstance(event, StreamEvent)
            assert isinstance(event.type, str)
            assert isinstance(event.data, dict)

            if event.type == "messages-tuple" and event.data.get("type") == "ai":
                assert "content" in event.data
                assert "id" in event.data
            elif event.type == "values":
                assert "messages" in event.data
                assert "artifacts" in event.data
            elif event.type == "end":
                # end event may contain usage stats after token tracking was added
                assert isinstance(event.data, dict)

    @requires_llm
    def test_multi_turn_stateless(self, client):
        """Without checkpointer, two calls to the same thread_id are independent."""
        tid = str(uuid.uuid4())

        r1 = client.chat("Remember the number 42", thread_id=tid)
        # Reset so agent is recreated (simulates no cross-turn state)
        client.reset_agent()
        r2 = client.chat("What number did I say?", thread_id=tid)

        # Without a checkpointer the second call has no memory of the first.
        # We can't assert exact content, but both should be non-empty.
        assert isinstance(r1, str) and len(r1) > 0
        assert isinstance(r2, str) and len(r2) > 0


# ---------------------------------------------------------------------------
# Step 3: Tool call flow (requires LLM)
# ---------------------------------------------------------------------------


class TestToolCallFlow:
    """Verify the LLM actually invokes tools through the real agent pipeline."""

    @requires_llm
    def test_tool_call_produces_events(self, client):
        """When the LLM decides to use a tool, we see tool call + result events."""
        # Give a clear instruction that forces a tool call
        events = list(client.stream("Use the bash tool to run: echo hello_e2e_test"))

        types = [e.type for e in events]
        assert types[-1] == "end"

        # Should have at least one tool call event
        tool_call_events = [e for e in events if e.type == "messages-tuple" and e.data.get("tool_calls")]
        tool_result_events = [e for e in events if e.type == "messages-tuple" and e.data.get("type") == "tool"]
        assert len(tool_call_events) >= 1, "Expected at least one tool_call event"
        assert len(tool_result_events) >= 1, "Expected at least one tool result event"

    @requires_llm
    def test_tool_call_event_structure(self, client):
        """Tool call events contain name, args, and id fields."""
        events = list(client.stream("Use the read_file tool to read /mnt/user-data/workspace/nonexistent.txt"))

        tc_events = [e for e in events if e.type == "messages-tuple" and e.data.get("tool_calls")]
        if tc_events:
            tc = tc_events[0].data["tool_calls"][0]
            assert "name" in tc
            assert "args" in tc
            assert "id" in tc


# ---------------------------------------------------------------------------
# Step 4: File upload integration (no LLM needed for most)
# ---------------------------------------------------------------------------


class TestFileUploadIntegration:
    """Upload, list, and delete files through the real client path."""

    def test_upload_files(self, e2e_env, tmp_path):
        """upload_files() copies files and returns metadata."""
        test_file = tmp_path / "source" / "readme.txt"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("Hello world")

        c = DeerFlowClient(checkpointer=None, thinking_enabled=False)
        tid = str(uuid.uuid4())

        result = c.upload_files(tid, [test_file])
        assert result["success"] is True
        assert len(result["files"]) == 1
        assert result["files"][0]["filename"] == "readme.txt"

        # Physically exists
        from deerflow.config.paths import get_paths
        from deerflow.runtime.user_context import get_effective_user_id

        assert (get_paths().sandbox_uploads_dir(tid, user_id=get_effective_user_id()) / "readme.txt").exists()

    def test_upload_duplicate_rename(self, e2e_env, tmp_path):
        """Uploading two files with the same name auto-renames the second."""
        d1 = tmp_path / "dir1"
        d2 = tmp_path / "dir2"
        d1.mkdir()
        d2.mkdir()
        (d1 / "data.txt").write_text("content A")
        (d2 / "data.txt").write_text("content B")

        c = DeerFlowClient(checkpointer=None, thinking_enabled=False)
        tid = str(uuid.uuid4())

        result = c.upload_files(tid, [d1 / "data.txt", d2 / "data.txt"])
        assert result["success"] is True
        assert len(result["files"]) == 2

        filenames = {f["filename"] for f in result["files"]}
        assert "data.txt" in filenames
        assert "data_1.txt" in filenames

    def test_upload_list_and_delete(self, e2e_env, tmp_path):
        """Upload → list → delete → list lifecycle."""
        test_file = tmp_path / "lifecycle.txt"
        test_file.write_text("lifecycle test")

        c = DeerFlowClient(checkpointer=None, thinking_enabled=False)
        tid = str(uuid.uuid4())

        c.upload_files(tid, [test_file])

        listing = c.list_uploads(tid)
        assert listing["count"] == 1
        assert listing["files"][0]["filename"] == "lifecycle.txt"

        del_result = c.delete_upload(tid, "lifecycle.txt")
        assert del_result["success"] is True

        listing = c.list_uploads(tid)
        assert listing["count"] == 0

    @requires_llm
    def test_upload_then_chat(self, e2e_env, tmp_path):
        """Upload a file then ask the LLM about it — UploadsMiddleware injects file info."""
        test_file = tmp_path / "source" / "notes.txt"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("The secret code is 7749.")

        c = DeerFlowClient(checkpointer=None, thinking_enabled=False)
        tid = str(uuid.uuid4())

        c.upload_files(tid, [test_file])
        # Chat — the middleware should inject <uploaded_files> context
        response = c.chat("What files are available?", thread_id=tid)
        assert isinstance(response, str) and len(response) > 0


# ---------------------------------------------------------------------------
# Step 5: Lifecycle and configuration (no LLM needed)
# ---------------------------------------------------------------------------


class TestLifecycleAndConfig:
    """Agent recreation and configuration behavior."""

    @requires_llm
    def test_agent_recreation_on_config_change(self, client):
        """Changing thinking_enabled triggers agent recreation (different config key)."""
        list(client.stream("hi"))
        key1 = client._agent_config_key

        # Stream with a different config override
        client.reset_agent()
        list(client.stream("hi", thinking_enabled=True))
        key2 = client._agent_config_key

        # thinking_enabled changed: False → True → keys differ
        assert key1 != key2

    def test_reset_agent_clears_state(self, e2e_env):
        """reset_agent() sets the internal agent to None."""
        c = DeerFlowClient(checkpointer=None, thinking_enabled=False)
        # Before any call, agent is None
        assert c._agent is None

        c.reset_agent()
        assert c._agent is None
        assert c._agent_config_key is None

    def test_plan_mode_config_key(self, e2e_env):
        """plan_mode is part of the config key tuple."""
        c = DeerFlowClient(checkpointer=None, plan_mode=False)
        cfg1 = c._get_runnable_config("test-thread")
        key1 = (
            cfg1["configurable"]["model_name"],
            cfg1["configurable"]["thinking_enabled"],
            cfg1["configurable"]["is_plan_mode"],
            cfg1["configurable"]["subagent_enabled"],
        )

        c2 = DeerFlowClient(checkpointer=None, plan_mode=True)
        cfg2 = c2._get_runnable_config("test-thread")
        key2 = (
            cfg2["configurable"]["model_name"],
            cfg2["configurable"]["thinking_enabled"],
            cfg2["configurable"]["is_plan_mode"],
            cfg2["configurable"]["subagent_enabled"],
        )

        assert key1 != key2
        assert key1[2] is False
        assert key2[2] is True


# ---------------------------------------------------------------------------
# Step 6: Middleware chain verification (requires LLM)
# ---------------------------------------------------------------------------


class TestMiddlewareChain:
    """Verify middleware side effects through real execution."""

    @requires_llm
    def test_thread_data_paths_in_state(self, client):
        """After streaming, thread directory paths are computed correctly."""
        tid = str(uuid.uuid4())
        events = list(client.stream("hi", thread_id=tid))

        # The values event should contain messages
        values_events = [e for e in events if e.type == "values"]
        assert len(values_events) >= 1

        # ThreadDataMiddleware should have set paths in the state.
        # We verify the paths singleton can resolve the thread dir.
        from deerflow.config.paths import get_paths

        thread_dir = get_paths().thread_dir(tid)
        assert str(thread_dir).endswith(tid)

    @requires_llm
    def test_stream_completes_without_middleware_errors(self, client):
        """Full middleware chain (ThreadData, Uploads, Sandbox, DanglingToolCall,
        Memory, Clarification) executes without errors."""
        events = list(client.stream("What is 1+1?"))

        types = [e.type for e in events]
        assert types[-1] == "end"
        # Should have at least one AI response
        ai_events = [e for e in events if e.type == "messages-tuple" and e.data.get("type") == "ai"]
        assert len(ai_events) >= 1


# ---------------------------------------------------------------------------
# Step 7: Error and boundary conditions
# ---------------------------------------------------------------------------


class TestErrorAndBoundary:
    """Error propagation and edge cases."""

    def test_upload_nonexistent_file_raises(self, e2e_env):
        """Uploading a file that doesn't exist raises FileNotFoundError."""
        c = DeerFlowClient(checkpointer=None, thinking_enabled=False)
        with pytest.raises(FileNotFoundError):
            c.upload_files("test-thread", ["/nonexistent/file.txt"])

    def test_delete_nonexistent_upload_raises(self, e2e_env):
        """Deleting a file that doesn't exist raises FileNotFoundError."""
        c = DeerFlowClient(checkpointer=None, thinking_enabled=False)
        tid = str(uuid.uuid4())
        # Ensure the uploads dir exists first
        c.list_uploads(tid)
        with pytest.raises(FileNotFoundError):
            c.delete_upload(tid, "ghost.txt")

    def test_artifact_path_traversal_blocked(self, e2e_env):
        """get_artifact blocks path traversal attempts."""
        c = DeerFlowClient(checkpointer=None, thinking_enabled=False)
        with pytest.raises(ValueError):
            c.get_artifact("test-thread", "../../etc/passwd")

    def test_upload_directory_rejected(self, e2e_env, tmp_path):
        """Uploading a directory (not a file) is rejected."""
        d = tmp_path / "a_directory"
        d.mkdir()
        c = DeerFlowClient(checkpointer=None, thinking_enabled=False)
        with pytest.raises(ValueError, match="not a file"):
            c.upload_files("test-thread", [d])

    @requires_llm
    def test_empty_message_still_gets_response(self, client):
        """Even an empty-ish message should produce a valid event stream."""
        events = list(client.stream(" "))
        types = [e.type for e in events]
        assert types[-1] == "end"


# ---------------------------------------------------------------------------
# Step 8: Artifact access (no LLM needed)
# ---------------------------------------------------------------------------


class TestArtifactAccess:
    """Read artifacts through get_artifact() with real filesystem."""

    def test_get_artifact_happy_path(self, e2e_env):
        """Write a file to outputs, then read it back via get_artifact()."""
        from deerflow.config.paths import get_paths
        from deerflow.runtime.user_context import get_effective_user_id

        c = DeerFlowClient(checkpointer=None, thinking_enabled=False)
        tid = str(uuid.uuid4())

        # Create an output file in the thread's outputs directory
        outputs_dir = get_paths().sandbox_outputs_dir(tid, user_id=get_effective_user_id())
        outputs_dir.mkdir(parents=True, exist_ok=True)
        (outputs_dir / "result.txt").write_text("hello artifact")

        data, mime = c.get_artifact(tid, "mnt/user-data/outputs/result.txt")
        assert data == b"hello artifact"
        assert "text" in mime

    def test_get_artifact_nested_path(self, e2e_env):
        """Artifacts in subdirectories are accessible."""
        from deerflow.config.paths import get_paths
        from deerflow.runtime.user_context import get_effective_user_id

        c = DeerFlowClient(checkpointer=None, thinking_enabled=False)
        tid = str(uuid.uuid4())

        outputs_dir = get_paths().sandbox_outputs_dir(tid, user_id=get_effective_user_id())
        sub = outputs_dir / "charts"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "data.json").write_text('{"x": 1}')

        data, mime = c.get_artifact(tid, "mnt/user-data/outputs/charts/data.json")
        assert b'"x"' in data
        assert "json" in mime

    def test_get_artifact_nonexistent_raises(self, e2e_env):
        """Reading a nonexistent artifact raises FileNotFoundError."""
        c = DeerFlowClient(checkpointer=None, thinking_enabled=False)
        with pytest.raises(FileNotFoundError):
            c.get_artifact("test-thread", "mnt/user-data/outputs/ghost.txt")

    def test_get_artifact_traversal_within_prefix_blocked(self, e2e_env):
        """Path traversal within the valid prefix is still blocked."""
        c = DeerFlowClient(checkpointer=None, thinking_enabled=False)
        with pytest.raises((PermissionError, ValueError, FileNotFoundError)):
            c.get_artifact("test-thread", "mnt/user-data/outputs/../../etc/passwd")


# ---------------------------------------------------------------------------
# Step 10: Configuration management (no LLM needed)
# ---------------------------------------------------------------------------


class TestConfigManagement:
    """Config queries and updates through real code paths."""

    def test_list_models_returns_injected_config(self, e2e_env):
        """list_models() returns the model from the injected AppConfig."""
        expected_model_name = os.getenv("E2E_MODEL_NAME", "volcengine-ark")
        c = DeerFlowClient(checkpointer=None, thinking_enabled=False)
        result = c.list_models()
        assert "models" in result
        assert len(result["models"]) == 1
        assert result["models"][0]["name"] == expected_model_name
        assert result["models"][0]["display_name"] == "E2E Test Model"

    def test_get_model_found(self, e2e_env):
        """get_model() returns the model when it exists."""
        expected_model_name = os.getenv("E2E_MODEL_NAME", "volcengine-ark")
        c = DeerFlowClient(checkpointer=None, thinking_enabled=False)
        model = c.get_model(expected_model_name)
        assert model is not None
        assert model["name"] == expected_model_name
        assert model["supports_thinking"] is False

    def test_get_model_not_found(self, e2e_env):
        """get_model() returns None for nonexistent model."""
        c = DeerFlowClient(checkpointer=None, thinking_enabled=False)
        assert c.get_model("nonexistent-model") is None
