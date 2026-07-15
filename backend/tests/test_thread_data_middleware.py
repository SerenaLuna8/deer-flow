from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langgraph.runtime import Runtime

from deerflow.agents.middlewares.thread_data_middleware import ThreadDataMiddleware


def _as_posix(path: str) -> str:
    return path.replace("\\", "/")


class TestThreadDataMiddleware:
    def test_project_mode_uses_only_opaque_authority_paths(self, tmp_path, monkeypatch):
        middleware = ThreadDataMiddleware(base_dir=str(tmp_path), lazy_init=False)
        expected = {
            "workspace_path": "/mnt/user-data/workspace",
            "uploads_path": "/mnt/user-data/uploads",
            "outputs_path": "/mnt/user-data/outputs",
        }
        authority = SimpleNamespace(thread_data_paths=Mock(return_value=expected))

        monkeypatch.setattr(
            middleware,
            "_create_thread_directories",
            Mock(side_effect=AssertionError("project mode touched host paths")),
        )
        result = middleware.before_agent(
            state={},
            runtime=Runtime(
                context={
                    "thread_id": "thread-private",
                    "private_scope": object(),
                    "__file_authority": authority,
                }
            ),
        )

        assert result is not None
        assert result["thread_data"] == expected
        authority.thread_data_paths.assert_called_once_with()

    def test_project_mode_missing_opaque_authority_fails_closed(self, tmp_path):
        middleware = ThreadDataMiddleware(base_dir=str(tmp_path), lazy_init=True)

        with pytest.raises(RuntimeError, match="Private file authority"):
            middleware.before_agent(
                state={},
                runtime=Runtime(context={"thread_id": "thread-private", "private_scope": object()}),
            )

    def test_before_agent_returns_paths_when_thread_id_present_in_context(self, tmp_path):
        middleware = ThreadDataMiddleware(base_dir=str(tmp_path), lazy_init=True)

        result = middleware.before_agent(state={}, runtime=Runtime(context={"thread_id": "thread-123"}))

        assert result is not None
        assert _as_posix(result["thread_data"]["workspace_path"]).endswith("threads/thread-123/user-data/workspace")
        assert _as_posix(result["thread_data"]["uploads_path"]).endswith("threads/thread-123/user-data/uploads")
        assert _as_posix(result["thread_data"]["outputs_path"]).endswith("threads/thread-123/user-data/outputs")

    def test_before_agent_uses_thread_id_from_configurable_when_context_is_none(self, tmp_path, monkeypatch):
        middleware = ThreadDataMiddleware(base_dir=str(tmp_path), lazy_init=True)
        runtime = Runtime(context=None)
        monkeypatch.setattr(
            "deerflow.agents.middlewares.thread_data_middleware.get_config",
            lambda: {"configurable": {"thread_id": "thread-from-config"}},
        )

        result = middleware.before_agent(state={}, runtime=runtime)

        assert result is not None
        assert _as_posix(result["thread_data"]["workspace_path"]).endswith("threads/thread-from-config/user-data/workspace")
        assert runtime.context is None

    def test_before_agent_uses_thread_id_from_configurable_when_context_missing_thread_id(self, tmp_path, monkeypatch):
        middleware = ThreadDataMiddleware(base_dir=str(tmp_path), lazy_init=True)
        runtime = Runtime(context={})
        monkeypatch.setattr(
            "deerflow.agents.middlewares.thread_data_middleware.get_config",
            lambda: {"configurable": {"thread_id": "thread-from-config"}},
        )

        result = middleware.before_agent(state={}, runtime=runtime)

        assert result is not None
        assert _as_posix(result["thread_data"]["uploads_path"]).endswith("threads/thread-from-config/user-data/uploads")
        assert runtime.context == {}

    def test_before_agent_raises_clear_error_when_thread_id_missing_everywhere(self, tmp_path, monkeypatch):
        middleware = ThreadDataMiddleware(base_dir=str(tmp_path), lazy_init=True)
        monkeypatch.setattr(
            "deerflow.agents.middlewares.thread_data_middleware.get_config",
            lambda: {"configurable": {}},
        )

        with pytest.raises(ValueError, match="Thread ID is required in runtime context or config.configurable"):
            middleware.before_agent(state={}, runtime=Runtime(context=None))
