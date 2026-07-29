"""Ephemerally inject securely re-read image bytes before a model call."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import threading
from collections.abc import Awaitable, Callable, Mapping
from typing import override
from uuid import uuid4

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    ToolMessage,
)
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime

from deerflow.agents.thread_state import (
    ThreadState,
    ViewedImageData,
    normalize_viewed_images,
)
from deerflow.file_authority import require_private_file_authority
from deerflow.private_scope import PrivateResourceScope
from deerflow.sandbox.exceptions import SandboxError
from deerflow.sandbox.tools import sandbox_from_runtime
from deerflow.tools.builtins.view_image_tool import (
    _MAX_IMAGE_BYTES,
    _detect_image_mime,
    _is_allowed_image_virtual_path,
    _read_bounded_image_bytes,
)

logger = logging.getLogger(__name__)

_IMAGE_CONTEXT_MESSAGE_ID_PREFIX = "view-image-context:"
_IMAGE_CONTEXT_MESSAGE_MARKER_KEY = "deerflow_view_image_context"


class ViewImageMiddlewareState(ThreadState):
    """Reuse the thread state so reducer-backed keys keep their annotations."""


class ViewImageMiddleware(AgentMiddleware[ViewImageMiddlewareState]):
    """Re-read Run-bound image references and inject bytes outside graph state.

    ``view_image`` stores only a virtual path, current Run/sandbox/scope
    coordinates, size, MIME, and digest. This middleware revalidates all of
    them through the current sandbox authority immediately before the model
    call. The resulting base64 message exists only on the overridden
    ``ModelRequest`` and therefore cannot enter a checkpoint, including when
    the model call fails or is cancelled.
    """

    state_schema = ViewImageMiddlewareState

    def __init__(self, *, enable_injection: bool = True) -> None:
        super().__init__()
        self.enable_injection = enable_injection

    @classmethod
    def _legacy_image_message(cls, message: object) -> bool:
        if isinstance(message, Mapping):
            additional_kwargs = message.get("additional_kwargs")
            content = message.get("content")
        else:
            additional_kwargs = getattr(message, "additional_kwargs", None)
            content = getattr(message, "content", None)
        if not (isinstance(additional_kwargs, Mapping) and additional_kwargs.get("hide_from_ui") is True and isinstance(content, list)):
            return False
        for block in content:
            if not isinstance(block, Mapping) or block.get("type") != "image_url":
                continue
            image_url = block.get("image_url")
            if isinstance(image_url, Mapping) and isinstance(image_url.get("url"), str) and image_url["url"].startswith("data:"):
                return True
        return False

    @classmethod
    def _checkpoint_cleanup(
        cls,
        state: ViewImageMiddlewareState,
        runtime: Runtime,
    ) -> dict[str, object] | None:
        """Actively rewrite legacy/malformed image channels after restore.

        LangGraph does not invoke a reducer merely because an already-persisted
        channel was loaded under a newer state schema. An explicit update is
        therefore required to remove legacy base64 observations from the next
        checkpoint.
        """

        updates: dict[str, object] = {}
        if "viewed_images" in state:
            raw_images = state.get("viewed_images")
            normalized = normalize_viewed_images(raw_images)
            current_images = {
                path: image_data
                for path, image_data in normalized.items()
                if cls._reference_matches_current_runtime(
                    runtime,
                    image_data["file_ref"],
                )
            }
            if raw_images != current_images:
                updates["viewed_images"] = current_images

        messages = list(state.get("messages", []))
        retained_messages = [message for message in messages if not cls._legacy_image_message(message)]
        if len(retained_messages) != len(messages):
            updates["messages"] = [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                *retained_messages,
            ]
        return updates or None

    @override
    def before_model(
        self,
        state: ViewImageMiddlewareState,
        runtime: Runtime,
    ) -> dict[str, object] | None:
        return self._checkpoint_cleanup(state, runtime)

    @override
    async def abefore_model(
        self,
        state: ViewImageMiddlewareState,
        runtime: Runtime,
    ) -> dict[str, object] | None:
        return self._checkpoint_cleanup(state, runtime)

    @staticmethod
    def _get_last_assistant_message(messages: list) -> AIMessage | None:
        for message in reversed(messages):
            if isinstance(message, AIMessage):
                return message
        return None

    @staticmethod
    def _has_view_image_tool(message: AIMessage) -> bool:
        if not hasattr(message, "tool_calls") or not message.tool_calls:
            return False
        return any(tool_call.get("name") == "view_image" for tool_call in message.tool_calls)

    @staticmethod
    def _all_tools_completed(
        messages: list,
        assistant_message: AIMessage,
    ) -> bool:
        if not hasattr(assistant_message, "tool_calls") or not assistant_message.tool_calls:
            return False
        tool_call_ids = {tool_call.get("id") for tool_call in assistant_message.tool_calls if tool_call.get("id")}
        try:
            assistant_index = messages.index(assistant_message)
        except ValueError:
            return False
        completed_tool_ids = {message.tool_call_id for message in messages[assistant_index + 1 :] if isinstance(message, ToolMessage) and message.tool_call_id}
        return tool_call_ids.issubset(completed_tool_ids)

    @staticmethod
    def _is_image_context_message(message: object) -> bool:
        return isinstance(message, HumanMessage) and bool(message.id) and message.id.startswith(_IMAGE_CONTEXT_MESSAGE_ID_PREFIX) and message.additional_kwargs.get(_IMAGE_CONTEXT_MESSAGE_MARKER_KEY) is True

    def _should_inject_image_message(
        self,
        state: ViewImageMiddlewareState,
    ) -> bool:
        if not self.enable_injection:
            return False
        messages = state.get("messages", [])
        if not messages:
            return False
        assistant_message = self._get_last_assistant_message(messages)
        if assistant_message is None or not self._has_view_image_tool(assistant_message) or not self._all_tools_completed(messages, assistant_message):
            return False

        assistant_index = messages.index(assistant_message)
        for message in messages[assistant_index + 1 :]:
            if not isinstance(message, HumanMessage):
                continue
            if self._is_image_context_message(message):
                return False
            content = str(message.content)
            if "Here are the images you've viewed" in content or "Here are the details of the images you've viewed" in content:
                return False
        return True

    @staticmethod
    def _runtime_run_id(runtime: Runtime) -> str | None:
        context = runtime.context
        if not isinstance(context, Mapping):
            return None
        run_id = context.get("run_id")
        if isinstance(run_id, str) and run_id:
            return run_id
        thread_id = context.get("thread_id")
        if isinstance(thread_id, str) and thread_id:
            return f"legacy:{thread_id}"
        return None

    @staticmethod
    def _matches_current_scope(
        runtime: Runtime,
        file_ref: Mapping[str, object],
    ) -> bool:
        context = runtime.context
        if not isinstance(context, Mapping):
            return False
        if file_ref.get("run_id") != ViewImageMiddleware._runtime_run_id(runtime):
            return False

        private_scope = context.get("private_scope")
        if private_scope is None:
            return "project_id" not in file_ref and "owner_user_id" not in file_ref
        if type(private_scope) is not PrivateResourceScope:
            return False
        if file_ref.get("project_id") != private_scope.project_id or file_ref.get("owner_user_id") != private_scope.owner_user_id:
            return False

        try:
            authority = require_private_file_authority(context)
        except RuntimeError:
            return False
        authority_sandbox_id = getattr(authority, "sandbox_id", None)
        return isinstance(authority_sandbox_id, str) and authority_sandbox_id and authority_sandbox_id == file_ref.get("sandbox_id")

    @staticmethod
    def _reference_matches_current_runtime(
        runtime: Runtime,
        file_ref: Mapping[str, object],
    ) -> bool:
        if not ViewImageMiddleware._matches_current_scope(runtime, file_ref):
            return False
        state = runtime.state
        if not isinstance(state, Mapping):
            return False
        sandbox_state = state.get("sandbox")
        return isinstance(sandbox_state, Mapping) and sandbox_state.get("sandbox_id") == file_ref.get("sandbox_id")

    @staticmethod
    def _read_image_as_data_url(
        runtime: Runtime,
        image_path: str,
        image_data: ViewedImageData,
        *,
        cancel_event: threading.Event | None = None,
    ) -> str | None:
        """Reauthorize and revalidate one immutable image observation."""

        file_ref = image_data.get("file_ref")
        mime_type = image_data.get("mime_type")
        expected_size = image_data.get("size")
        expected_sha256 = image_data.get("sha256")
        if (
            not isinstance(file_ref, Mapping)
            or not isinstance(mime_type, str)
            or not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or not 0 <= expected_size <= _MAX_IMAGE_BYTES
            or not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
            or file_ref.get("path") != image_path
            or not _is_allowed_image_virtual_path(image_path)
            or not ViewImageMiddleware._reference_matches_current_runtime(
                runtime,
                file_ref,
            )
        ):
            return None

        try:
            sandbox = sandbox_from_runtime(runtime)
            if sandbox.id != file_ref.get("sandbox_id") or sandbox.id != (runtime.state.get("sandbox") or {}).get("sandbox_id"):
                return None
            image_bytes = _read_bounded_image_bytes(
                sandbox,
                image_path,
                max_bytes=_MAX_IMAGE_BYTES,
                cancel_event=cancel_event,
            )
        except SandboxError:
            logger.debug("Run-bound image sandbox is unavailable")
            return None
        except (OSError, PermissionError, RuntimeError, ValueError):
            logger.debug(
                "Run-bound image reference could not be re-read",
            )
            return None

        if len(image_bytes) != expected_size or hashlib.sha256(image_bytes).hexdigest() != expected_sha256 or _detect_image_mime(image_bytes) != mime_type:
            return None
        encoded = base64.b64encode(image_bytes).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    def _create_image_details_message(
        self,
        state: ViewImageMiddlewareState,
        runtime: Runtime,
        *,
        cancel_event: threading.Event | None = None,
    ) -> list[str | dict]:
        viewed_images = state.get("viewed_images", {})
        if not viewed_images:
            return [
                {
                    "type": "text",
                    "text": "No images have been viewed.",
                }
            ]

        content_blocks: list[str | dict] = [
            {
                "type": "text",
                "text": "Here are the images you've viewed:",
            }
        ]
        for image_path, image_data in viewed_images.items():
            file_ref = image_data.get("file_ref")
            if not isinstance(
                file_ref,
                Mapping,
            ) or not self._reference_matches_current_runtime(
                runtime,
                file_ref,
            ):
                continue
            mime_type = image_data.get("mime_type", "unknown")
            content_blocks.append(
                {
                    "type": "text",
                    "text": f"\n- **{image_path}** ({mime_type})",
                }
            )
            data_url = self._read_image_as_data_url(
                runtime,
                image_path,
                image_data,
                cancel_event=cancel_event,
            )
            if data_url is None:
                content_blocks.append(
                    {
                        "type": "text",
                        "text": "  (file unavailable, unauthorized, or changed)",
                    }
                )
                continue
            content_blocks.append(
                {
                    "type": "image_url",
                    "image_url": {"url": data_url},
                }
            )
        return content_blocks

    @staticmethod
    def _create_image_context_message(
        content: list[str | dict],
    ) -> HumanMessage:
        return HumanMessage(
            id=f"{_IMAGE_CONTEXT_MESSAGE_ID_PREFIX}{uuid4().hex}",
            content=content,
            additional_kwargs={
                "hide_from_ui": True,
                _IMAGE_CONTEXT_MESSAGE_MARKER_KEY: True,
            },
        )

    def _inject_request(
        self,
        request: ModelRequest,
        cancel_event: threading.Event | None = None,
    ) -> ModelRequest:
        state = request.state or {}
        runtime = request.runtime
        if runtime is None or not self._should_inject_image_message(state):
            return request
        image_content = self._create_image_details_message(
            state,
            runtime,
            cancel_event=cancel_event,
        )
        image_message = self._create_image_context_message(image_content)
        return request.override(
            messages=[*request.messages, image_message],
        )

    async def _inject_request_joined(
        self,
        request: ModelRequest,
    ) -> ModelRequest:
        """Offload secure reads while deferring cancellation until close."""

        cancel_event = threading.Event()
        worker = asyncio.create_task(
            asyncio.to_thread(
                self._inject_request,
                request,
                cancel_event,
            )
        )
        pending_cancellation: asyncio.CancelledError | None = None
        while True:
            try:
                injected = await asyncio.shield(worker)
                break
            except asyncio.CancelledError as exc:
                if worker.cancelled():
                    raise
                cancel_event.set()
                if pending_cancellation is None:
                    pending_cancellation = exc
            except BaseException:
                if pending_cancellation is not None:
                    raise pending_cancellation
                raise
        if pending_cancellation is not None:
            raise pending_cancellation
        return injected

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return handler(self._inject_request(request))

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        injected = await self._inject_request_joined(request)
        return await handler(injected)
