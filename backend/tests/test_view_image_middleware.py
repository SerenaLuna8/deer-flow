"""Unit tests for ViewImageMiddleware's turn gating and ephemeral formatting."""

from types import SimpleNamespace

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from deerflow.agents.middlewares.view_image_middleware import (
    ViewImageMiddleware,
)


def _view_image_call(
    call_id: str = "call_1",
    path: str = "/mnt/user-data/uploads/img.png",
) -> dict:
    return {
        "name": "view_image",
        "id": call_id,
        "args": {"image_path": path},
    }


def _other_tool_call(
    call_id: str = "call_other",
    name: str = "bash",
) -> dict:
    return {
        "name": name,
        "id": call_id,
        "args": {"command": "ls"},
    }


def _runtime() -> SimpleNamespace:
    return SimpleNamespace(
        state={"sandbox": {"sandbox_id": "sandbox-1", "run_id": "run-1"}},
        context={"thread_id": "thread-1", "run_id": "run-1"},
    )


def _viewed_image(
    path: str = "/mnt/user-data/uploads/img.png",
) -> dict:
    return {
        "mime_type": "image/png",
        "size": 8,
        "sha256": "0" * 64,
        "file_ref": {
            "path": path,
            "sandbox_id": "sandbox-1",
            "run_id": "run-1",
        },
    }


class TestGetLastAssistantMessage:
    def test_returns_none_without_ai_message(self):
        middleware = ViewImageMiddleware()
        assert middleware._get_last_assistant_message([]) is None
        assert (
            middleware._get_last_assistant_message(
                [
                    SystemMessage(content="sys"),
                    HumanMessage(content="hi"),
                ]
            )
            is None
        )

    def test_returns_most_recent_ai_message(self):
        middleware = ViewImageMiddleware()
        older = AIMessage(content="older")
        newer = AIMessage(content="newer")
        messages = [
            HumanMessage(content="q"),
            older,
            HumanMessage(content="q2"),
            newer,
        ]
        assert middleware._get_last_assistant_message(messages) is newer


class TestHasViewImageTool:
    def test_requires_nonempty_matching_tool_call(self):
        middleware = ViewImageMiddleware()
        assert middleware._has_view_image_tool(SimpleNamespace(content="text")) is False
        assert middleware._has_view_image_tool(AIMessage(content="")) is False
        assert middleware._has_view_image_tool(AIMessage(content="", tool_calls=[_other_tool_call()])) is False
        assert (
            middleware._has_view_image_tool(
                AIMessage(
                    content="",
                    tool_calls=[
                        _other_tool_call(),
                        _view_image_call("image-call"),
                    ],
                )
            )
            is True
        )


class TestAllToolsCompleted:
    def test_all_ids_must_complete_after_assistant(self):
        middleware = ViewImageMiddleware()
        assistant = AIMessage(
            content="",
            tool_calls=[
                _view_image_call("c1"),
                _view_image_call("c2"),
            ],
        )
        assert (
            middleware._all_tools_completed(
                [
                    assistant,
                    ToolMessage(content="ok", tool_call_id="c1"),
                ],
                assistant,
            )
            is False
        )
        assert (
            middleware._all_tools_completed(
                [
                    assistant,
                    ToolMessage(content="ok", tool_call_id="c1"),
                    ToolMessage(content="ok", tool_call_id="c2"),
                ],
                assistant,
            )
            is True
        )

    def test_stale_completion_and_missing_assistant_do_not_count(self):
        middleware = ViewImageMiddleware()
        assistant = AIMessage(
            content="",
            tool_calls=[_view_image_call("c1")],
        )
        assert (
            middleware._all_tools_completed(
                [
                    ToolMessage(content="stale", tool_call_id="c1"),
                    assistant,
                ],
                assistant,
            )
            is False
        )
        assert (
            middleware._all_tools_completed(
                [HumanMessage(content="hi")],
                assistant,
            )
            is False
        )


class TestShouldInjectImageMessage:
    def test_requires_completed_view_image_turn(self):
        middleware = ViewImageMiddleware()
        assert middleware._should_inject_image_message({}) is False
        assistant = AIMessage(
            content="",
            tool_calls=[_view_image_call("c1")],
        )
        assert middleware._should_inject_image_message({"messages": [assistant]}) is False
        assert (
            middleware._should_inject_image_message(
                {
                    "messages": [
                        assistant,
                        ToolMessage(content="ok", tool_call_id="c1"),
                    ],
                    "viewed_images": {"/mnt/user-data/uploads/img.png": _viewed_image()},
                }
            )
            is True
        )

    def test_deduplicates_trusted_and_legacy_context_messages(self):
        middleware = ViewImageMiddleware()
        assistant = AIMessage(
            content="",
            tool_calls=[_view_image_call("c1")],
        )
        completed = ToolMessage(content="ok", tool_call_id="c1")
        trusted = middleware._create_image_context_message([{"type": "text", "text": "image"}])
        assert middleware._should_inject_image_message({"messages": [assistant, completed, trusted]}) is False
        legacy = HumanMessage(content="Here are the details of the images you've viewed")
        assert middleware._should_inject_image_message({"messages": [assistant, completed, legacy]}) is False


class TestCreateImageDetailsMessage:
    def test_empty_state_has_placeholder(self):
        middleware = ViewImageMiddleware()
        assert middleware._create_image_details_message({}, _runtime()) == [
            {
                "type": "text",
                "text": "No images have been viewed.",
            }
        ]

    def test_formats_available_and_unavailable_references(
        self,
        monkeypatch,
    ):
        middleware = ViewImageMiddleware()
        paths = [
            "/mnt/user-data/uploads/img.png",
            "/mnt/user-data/uploads/changed.png",
        ]
        monkeypatch.setattr(
            middleware,
            "_read_image_as_data_url",
            lambda runtime, path, data, **_kwargs: "data:image/png;base64,AAAA" if path == paths[0] else None,
        )
        blocks = middleware._create_image_details_message(
            {
                "viewed_images": {
                    paths[0]: _viewed_image(paths[0]),
                    paths[1]: _viewed_image(paths[1]),
                }
            },
            _runtime(),
        )

        assert any(isinstance(block, dict) and block.get("type") == "image_url" for block in blocks)
        assert any("unauthorized, or changed" in block.get("text", "") for block in blocks if isinstance(block, dict))


def test_image_context_message_is_hidden_and_identifiable():
    message = ViewImageMiddleware._create_image_context_message([{"type": "text", "text": "image"}])
    assert message.additional_kwargs["hide_from_ui"] is True
    assert message.additional_kwargs["deerflow_view_image_context"] is True
    assert ViewImageMiddleware._is_image_context_message(message) is True
