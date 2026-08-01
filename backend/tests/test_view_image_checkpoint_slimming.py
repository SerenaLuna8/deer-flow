from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import threading
from types import SimpleNamespace
from typing import Annotated, Any, TypedDict
from unittest.mock import MagicMock

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import (
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import Runnable
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from deerflow.agents.middlewares.view_image_middleware import ViewImageMiddleware
from deerflow.agents.thread_state import ThreadState, normalize_viewed_images
from deerflow.private_scope import PrivateResourceScope
from deerflow.sandbox.exceptions import SandboxNotFoundError
from deerflow.tools.builtins.view_image_tool import view_image_tool

view_image_middleware_module = importlib.import_module("deerflow.agents.middlewares.view_image_middleware")
view_image_tool_module = importlib.import_module("deerflow.tools.builtins.view_image_tool")

PNG_BYTES = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
IMAGE_PATH = "/mnt/user-data/uploads/sample.png"


class _MemorySandbox:
    def __init__(self, data: bytes, *, sandbox_id: str = "sandbox-run-1") -> None:
        self.id = sandbox_id
        self.data = data
        self.opened_paths: list[str] = []
        self.closed_handles: list[str] = []
        self._offset = 0

    def open_regular_file(self, path: str) -> str:
        self.opened_paths.append(path)
        self._offset = 0
        return "opaque-reader"

    def read_regular_file(self, handle: str, max_bytes: int) -> bytes:
        assert handle == "opaque-reader"
        chunk = self.data[self._offset : self._offset + max_bytes]
        self._offset += len(chunk)
        return chunk

    def close_regular_file(self, handle: str) -> None:
        self.closed_handles.append(handle)


class _BlockingMemorySandbox(_MemorySandbox):
    def __init__(self, data: bytes, *, sandbox_id: str = "sandbox-run-1") -> None:
        super().__init__(data, sandbox_id=sandbox_id)
        self.read_started = threading.Event()
        self.release_read = threading.Event()
        self.reader_closed = threading.Event()

    def read_regular_file(self, handle: str, max_bytes: int) -> bytes:
        self.read_started.set()
        if not self.release_read.wait(timeout=5):
            raise TimeoutError("test did not release image read")
        return super().read_regular_file(handle, max_bytes)

    def close_regular_file(self, handle: str) -> None:
        super().close_regular_file(handle)
        self.reader_closed.set()


def _scope(
    *,
    project_id: str = "project-1",
    owner_user_id: str = "owner-1",
) -> PrivateResourceScope:
    return PrivateResourceScope(
        project_id=project_id,
        owner_user_id=owner_user_id,
        membership_version=1,
    )


def _runtime(
    sandbox: _MemorySandbox,
    *,
    run_id: str = "run-1",
    scope: PrivateResourceScope | None = None,
) -> SimpleNamespace:
    private_scope = scope or _scope()
    authority = SimpleNamespace(sandbox_id=sandbox.id)
    return SimpleNamespace(
        state={
            "sandbox": {"sandbox_id": sandbox.id, "run_id": run_id},
            "thread_data": {
                "workspace_path": "/mnt/user-data/workspace",
                "uploads_path": "/mnt/user-data/uploads",
                "outputs_path": "/mnt/user-data/outputs",
            },
        },
        context={
            "thread_id": "thread-1",
            "run_id": run_id,
            "private_scope": private_scope,
            "__file_authority": authority,
        },
        config={},
    )


def _ready_state(viewed_image: dict[str, object]) -> dict:
    assistant = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "view_image",
                "id": "call-image",
                "args": {"image_path": IMAGE_PATH},
            }
        ],
    )
    return {
        "messages": [
            assistant,
            ToolMessage(
                content="Successfully read image",
                tool_call_id="call-image",
            ),
        ],
        "viewed_images": {IMAGE_PATH: viewed_image},
    }


def _model_request(state: dict, runtime: SimpleNamespace) -> ModelRequest:
    return ModelRequest(
        model=MagicMock(),
        messages=list(state["messages"]),
        tools=[],
        state=state,
        runtime=runtime,
    )


def test_view_image_checkpoints_only_run_bound_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _MemorySandbox(PNG_BYTES)
    runtime = _runtime(sandbox)
    monkeypatch.setattr(
        view_image_tool_module,
        "sandbox_from_runtime",
        lambda _runtime: sandbox,
    )

    result = view_image_tool.func(
        runtime=runtime,
        image_path=IMAGE_PATH,
        tool_call_id="call-image",
    )

    viewed_image = result.update["viewed_images"][IMAGE_PATH]
    assert viewed_image == {
        "mime_type": "image/png",
        "size": len(PNG_BYTES),
        "sha256": hashlib.sha256(PNG_BYTES).hexdigest(),
        "file_ref": {
            "path": IMAGE_PATH,
            "sandbox_id": "sandbox-run-1",
            "run_id": "run-1",
            "project_id": "project-1",
            "owner_user_id": "owner-1",
        },
    }
    assert "base64" not in viewed_image
    assert sandbox.opened_paths == [IMAGE_PATH]
    assert sandbox.closed_handles == ["opaque-reader"]


def test_model_injection_rereads_through_current_sandbox_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _MemorySandbox(PNG_BYTES)
    runtime = _runtime(sandbox)
    viewed_image = {
        "mime_type": "image/png",
        "size": len(PNG_BYTES),
        "sha256": hashlib.sha256(PNG_BYTES).hexdigest(),
        "file_ref": {
            "path": IMAGE_PATH,
            "sandbox_id": sandbox.id,
            "run_id": "run-1",
            "project_id": "project-1",
            "owner_user_id": "owner-1",
        },
    }
    monkeypatch.setattr(
        view_image_middleware_module,
        "sandbox_from_runtime",
        lambda _runtime: sandbox,
    )
    state = _ready_state(viewed_image)
    request = _model_request(state, runtime)
    seen: dict[str, ModelRequest] = {}

    def handler(injected: ModelRequest):
        seen["request"] = injected
        return "ok"

    result = ViewImageMiddleware().wrap_model_call(request, handler)

    assert result == "ok"
    image_blocks = [block for message in seen["request"].messages if getattr(message, "additional_kwargs", {}).get("deerflow_view_image_context") for block in message.content if isinstance(block, dict) and block.get("type") == "image_url"]
    assert image_blocks == [
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")},
        }
    ]
    assert state["messages"] == request.state["messages"]
    assert all("base64" not in str(message.content) for message in state["messages"])


@pytest.mark.parametrize(
    ("runtime_kwargs", "replacement_data", "path_is_visible"),
    [
        ({"run_id": "run-2"}, PNG_BYTES, False),
        ({"scope": _scope(project_id="project-2")}, PNG_BYTES, False),
        ({}, b"\x89PNG\r\n\x1a\nchanged", True),
    ],
)
def test_model_injection_fails_closed_for_wrong_run_scope_or_version(
    monkeypatch: pytest.MonkeyPatch,
    runtime_kwargs: dict[str, object],
    replacement_data: bytes,
    path_is_visible: bool,
) -> None:
    sandbox = _MemorySandbox(replacement_data)
    runtime = _runtime(sandbox, **runtime_kwargs)
    viewed_image = {
        "mime_type": "image/png",
        "size": len(PNG_BYTES),
        "sha256": hashlib.sha256(PNG_BYTES).hexdigest(),
        "file_ref": {
            "path": IMAGE_PATH,
            "sandbox_id": sandbox.id,
            "run_id": "run-1",
            "project_id": "project-1",
            "owner_user_id": "owner-1",
        },
    }
    monkeypatch.setattr(
        view_image_middleware_module,
        "sandbox_from_runtime",
        lambda _runtime: sandbox,
    )
    state = _ready_state(viewed_image)
    captured: dict[str, ModelRequest] = {}

    def handler(injected: ModelRequest):
        captured["request"] = injected
        return "ok"

    ViewImageMiddleware().wrap_model_call(
        _model_request(state, runtime),
        handler,
    )

    assert all(not (isinstance(block, dict) and block.get("type") == "image_url") for message in captured["request"].messages for block in (message.content if isinstance(message.content, list) else []))
    injected_context = [message for message in captured["request"].messages if getattr(message, "additional_kwargs", {}).get("deerflow_view_image_context")]
    assert (IMAGE_PATH in str(injected_context)) is path_is_visible


def test_missing_sandbox_is_sanitized_as_unavailable_for_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _MemorySandbox(PNG_BYTES)
    runtime = _runtime(sandbox)
    viewed_image = {
        "mime_type": "image/png",
        "size": len(PNG_BYTES),
        "sha256": hashlib.sha256(PNG_BYTES).hexdigest(),
        "file_ref": {
            "path": IMAGE_PATH,
            "sandbox_id": sandbox.id,
            "run_id": "run-1",
            "project_id": "project-1",
            "owner_user_id": "owner-1",
        },
    }
    monkeypatch.setattr(
        view_image_middleware_module,
        "sandbox_from_runtime",
        lambda _runtime: (_ for _ in ()).throw(SandboxNotFoundError(sandbox_id="private-sandbox-secret")),
    )
    captured: dict[str, ModelRequest] = {}

    def handler(injected: ModelRequest):
        captured["request"] = injected
        return "ok"

    assert (
        ViewImageMiddleware().wrap_model_call(
            _model_request(_ready_state(viewed_image), runtime),
            handler,
        )
        == "ok"
    )
    rendered = str(captured["request"].messages)
    assert "private-sandbox-secret" not in rendered
    assert "image_url" not in rendered


def test_missing_sandbox_is_sanitized_as_unavailable_for_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _MemorySandbox(PNG_BYTES)
    runtime = _runtime(sandbox)
    monkeypatch.setattr(
        view_image_tool_module,
        "sandbox_from_runtime",
        lambda _runtime: (_ for _ in ()).throw(SandboxNotFoundError(sandbox_id="private-sandbox-secret")),
    )

    result = view_image_tool.func(
        runtime=runtime,
        image_path=IMAGE_PATH,
        tool_call_id="call-image",
    )

    rendered = str(result.update["messages"][0].content)
    assert "unavailable" in rendered.lower()
    assert "private-sandbox-secret" not in rendered
    assert "viewed_images" not in result.update


def test_transient_image_message_is_not_persisted_when_model_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _MemorySandbox(PNG_BYTES)
    runtime = _runtime(sandbox)
    viewed_image = {
        "mime_type": "image/png",
        "size": len(PNG_BYTES),
        "sha256": hashlib.sha256(PNG_BYTES).hexdigest(),
        "file_ref": {
            "path": IMAGE_PATH,
            "sandbox_id": sandbox.id,
            "run_id": "run-1",
            "project_id": "project-1",
            "owner_user_id": "owner-1",
        },
    }
    monkeypatch.setattr(
        view_image_middleware_module,
        "sandbox_from_runtime",
        lambda _runtime: sandbox,
    )
    state = _ready_state(viewed_image)
    original_messages = list(state["messages"])

    def handler(_injected: ModelRequest):
        raise RuntimeError("model failed")

    with pytest.raises(RuntimeError, match="model failed"):
        ViewImageMiddleware().wrap_model_call(
            _model_request(state, runtime),
            handler,
        )

    assert state["messages"] == original_messages
    assert "base64" not in str(state)


@pytest.mark.anyio
async def test_transient_image_message_is_not_persisted_when_model_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _MemorySandbox(PNG_BYTES)
    runtime = _runtime(sandbox)
    viewed_image = {
        "mime_type": "image/png",
        "size": len(PNG_BYTES),
        "sha256": hashlib.sha256(PNG_BYTES).hexdigest(),
        "file_ref": {
            "path": IMAGE_PATH,
            "sandbox_id": sandbox.id,
            "run_id": "run-1",
            "project_id": "project-1",
            "owner_user_id": "owner-1",
        },
    }
    monkeypatch.setattr(
        view_image_middleware_module,
        "sandbox_from_runtime",
        lambda _runtime: sandbox,
    )
    state = _ready_state(viewed_image)
    original_messages = list(state["messages"])

    async def handler(_injected: ModelRequest):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await ViewImageMiddleware().awrap_model_call(
            _model_request(state, runtime),
            handler,
        )

    assert state["messages"] == original_messages
    assert "base64" not in str(state)


@pytest.mark.anyio
async def test_cancelled_async_reread_waits_until_secure_reader_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _BlockingMemorySandbox(PNG_BYTES)
    runtime = _runtime(sandbox)
    viewed_image = {
        "mime_type": "image/png",
        "size": len(PNG_BYTES),
        "sha256": hashlib.sha256(PNG_BYTES).hexdigest(),
        "file_ref": {
            "path": IMAGE_PATH,
            "sandbox_id": sandbox.id,
            "run_id": "run-1",
            "project_id": "project-1",
            "owner_user_id": "owner-1",
        },
    }
    monkeypatch.setattr(
        view_image_middleware_module,
        "sandbox_from_runtime",
        lambda _runtime: sandbox,
    )

    async def handler(_injected: ModelRequest):
        return "ok"

    task = asyncio.create_task(
        ViewImageMiddleware().awrap_model_call(
            _model_request(_ready_state(viewed_image), runtime),
            handler,
        )
    )
    assert await asyncio.to_thread(sandbox.read_started.wait, 1)

    task.cancel()
    await asyncio.sleep(0.05)
    cancellation_returned_before_close = task.done()
    sandbox.release_read.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cancellation_returned_before_close is False
    assert sandbox.reader_closed.is_set()
    assert sandbox.closed_handles == ["opaque-reader"]


class _LegacyImageState(TypedDict):
    messages: Annotated[list, add_messages]
    viewed_images: Annotated[
        dict[str, dict[str, str]],
        lambda existing, new: {**(existing or {}), **(new or {})},
    ]


class _RecordingImageCleanupModel(FakeMessagesListChatModel):
    def __init__(self) -> None:
        super().__init__(responses=[AIMessage(content="clean")])
        object.__setattr__(self, "received", [])

    def bind_tools(
        self,
        tools: Any,
        *,
        tool_choice: Any = None,
        **kwargs: Any,
    ) -> Runnable:
        del tools, tool_choice, kwargs
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.received.append(list(messages))
        return super()._generate(
            messages,
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        )


def test_resuming_legacy_checkpoint_actively_removes_persisted_base64() -> None:
    saver = InMemorySaver()
    config = {
        "configurable": {
            "thread_id": "legacy-view-image-checkpoint",
            "checkpoint_ns": "",
        }
    }

    legacy_builder = StateGraph(_LegacyImageState)
    legacy_builder.add_node(
        "seed_legacy_image",
        lambda _state: {
            "messages": [
                HumanMessage(
                    content=[
                        {
                            "type": "text",
                            "text": "Here are the images you've viewed:",
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": ("data:image/png;base64,LEGACY_PERSISTED_MESSAGE_BYTES")},
                        },
                    ],
                    additional_kwargs={"hide_from_ui": True},
                )
            ],
            "viewed_images": {
                IMAGE_PATH: {
                    "mime_type": "image/png",
                    "base64": "LEGACY_PERSISTED_IMAGE_BYTES",
                }
            },
        },
    )
    legacy_builder.add_edge(START, "seed_legacy_image")
    legacy_builder.add_edge("seed_legacy_image", END)
    legacy_builder.compile(checkpointer=saver).invoke({}, config)

    resumed_builder = StateGraph(ThreadState)
    middleware = ViewImageMiddleware()
    resumed_builder.add_node(
        "sanitize_checkpoint",
        lambda state, runtime: middleware.before_model(state, runtime) or {},
    )
    resumed_builder.add_edge(START, "sanitize_checkpoint")
    resumed_builder.add_edge("sanitize_checkpoint", END)
    resumed_builder.compile(checkpointer=saver).invoke({}, config)

    latest = saver.get_tuple(config)
    assert latest is not None
    assert latest.checkpoint["channel_values"]["viewed_images"] == {}
    assert "LEGACY_PERSISTED_IMAGE_BYTES" not in str(latest.checkpoint["channel_values"])
    assert "LEGACY_PERSISTED_MESSAGE_BYTES" not in str(latest.checkpoint["channel_values"])


def test_resumed_legacy_image_message_never_reaches_text_model() -> None:
    saver = InMemorySaver()
    config = {
        "configurable": {
            "thread_id": "legacy-image-text-model",
            "checkpoint_ns": "",
        }
    }
    legacy_builder = StateGraph(_LegacyImageState)
    legacy_builder.add_node(
        "seed",
        lambda _state: {
            "messages": [
                HumanMessage(
                    content=[
                        {
                            "type": "image_url",
                            "image_url": {"url": ("data:image/png;base64,LEGACY_MODEL_VISIBLE_BYTES")},
                        }
                    ],
                    additional_kwargs={"hide_from_ui": True},
                )
            ],
            "viewed_images": {
                IMAGE_PATH: {
                    "mime_type": "image/png",
                    "base64": "LEGACY_CHANNEL_BYTES",
                }
            },
        },
    )
    legacy_builder.add_edge(START, "seed")
    legacy_builder.add_edge("seed", END)
    legacy_builder.compile(checkpointer=saver).invoke({}, config)

    model = _RecordingImageCleanupModel()
    agent = create_agent(
        model=model,
        tools=[],
        middleware=[ViewImageMiddleware(enable_injection=False)],
        state_schema=ThreadState,
        checkpointer=saver,
    )
    agent.invoke(
        {"messages": [HumanMessage(content="continue", id="current-user-message")]},
        config,
    )

    assert len(model.received) == 1
    assert "LEGACY_MODEL_VISIBLE_BYTES" not in str(model.received[0])
    latest = saver.get_tuple(config)
    assert latest is not None
    assert "LEGACY_MODEL_VISIBLE_BYTES" not in str(latest.checkpoint["channel_values"])
    assert latest.checkpoint["channel_values"]["viewed_images"] == {}


def test_malformed_image_references_are_not_checkpoint_safe() -> None:
    malformed = {
        "/Users/private/secret.svg": {
            "mime_type": "image/svg+xml",
            "size": 21 * 1024 * 1024,
            "sha256": "a" * 64,
            "file_ref": {
                "path": "/Users/private/secret.svg",
                "sandbox_id": "sandbox-1",
                "run_id": "run-1",
            },
        }
    }

    assert normalize_viewed_images(malformed) == {}


def test_gif_reference_is_checkpoint_safe_without_image_bytes() -> None:
    image_path = "/mnt/user-data/uploads/proof.gif"
    image = {
        image_path: {
            "mime_type": "image/gif",
            "size": 43,
            "sha256": "a" * 64,
            "file_ref": {
                "path": image_path,
                "sandbox_id": "sandbox-gif",
                "run_id": "run-gif",
            },
        }
    }

    assert normalize_viewed_images(image) == image
