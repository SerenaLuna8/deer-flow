"""Ephemerally inject securely re-read image bytes before a model call."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import threading
from collections.abc import Awaitable, Callable, Mapping
from pathlib import PurePosixPath
from typing import override
from uuid import UUID, uuid4

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
from deerflow.error_codes import CURRENT_UPLOAD_FAILURE_DETAIL
from deerflow.file_authority import (
    AuthorityManifestEntry,
    require_private_file_authority,
)
from deerflow.private_scope import PrivateResourceScope
from deerflow.sandbox.exceptions import SandboxError
from deerflow.sandbox.tooling.runtime import sandbox_from_runtime
from deerflow.vision.image_input import (
    EXTENSION_TO_MIME,
    MAX_IMAGE_BYTES,
    detect_image_mime,
    is_allowed_image_virtual_path,
    read_bounded_image_bytes,
    validate_image_payload,
)

logger = logging.getLogger(__name__)

_IMAGE_CONTEXT_MESSAGE_ID_PREFIX = "view-image-context:"
_IMAGE_CONTEXT_MESSAGE_MARKER_KEY = "deerflow_view_image_context"
_MAX_CURRENT_UPLOAD_IMAGES = 4
# The public contract is independently bounded on both dimensions: at most
# four unique images, each at most ``MAX_IMAGE_BYTES``. Keep an explicit
# aggregate allocation guard without silently tightening the per-image limit.
_MAX_CURRENT_UPLOAD_TOTAL_BYTES = _MAX_CURRENT_UPLOAD_IMAGES * MAX_IMAGE_BYTES


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
                    state=state,
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
        *,
        state: Mapping[str, object],
    ) -> bool:
        if not ViewImageMiddleware._matches_current_scope(runtime, file_ref):
            return False
        sandbox_state = state.get("sandbox")
        return isinstance(sandbox_state, Mapping) and sandbox_state.get("sandbox_id") == file_ref.get("sandbox_id")

    @staticmethod
    def _read_image_as_data_url(
        runtime: Runtime,
        image_path: str,
        image_data: ViewedImageData,
        *,
        state: Mapping[str, object],
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
            or not 0 <= expected_size <= MAX_IMAGE_BYTES
            or not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
            or file_ref.get("path") != image_path
            or not is_allowed_image_virtual_path(image_path)
            or not ViewImageMiddleware._reference_matches_current_runtime(
                runtime,
                file_ref,
                state=state,
            )
        ):
            return None

        try:
            from deerflow.sandbox.tooling.path_mapping import resolve_delegated_tool_path

            sandbox = sandbox_from_runtime(runtime, state=state)
            if sandbox.id != file_ref.get("sandbox_id"):
                return None
            effective_image_path = resolve_delegated_tool_path(
                runtime,
                image_path,
            )
            image_bytes = read_bounded_image_bytes(
                sandbox,
                effective_image_path,
                max_bytes=MAX_IMAGE_BYTES,
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

        if len(image_bytes) != expected_size or hashlib.sha256(image_bytes).hexdigest() != expected_sha256 or detect_image_mime(image_bytes) != mime_type:
            return None
        try:
            validate_image_payload(image_bytes, mime_type)
        except ValueError:
            return None
        encoded = base64.b64encode(image_bytes).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    def _create_image_details_message(
        self,
        state: ViewImageMiddlewareState,
        runtime: Runtime,
        *,
        cancel_event: threading.Event | None = None,
        excluded_sha256: frozenset[str] = frozenset(),
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
        excluded_any = False
        for image_path, image_data in viewed_images.items():
            if image_data.get("sha256") in excluded_sha256:
                excluded_any = True
                continue
            file_ref = image_data.get("file_ref")
            if not isinstance(
                file_ref,
                Mapping,
            ) or not self._reference_matches_current_runtime(
                runtime,
                file_ref,
                state=state,
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
                state=state,
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
        if len(content_blocks) == 1 and excluded_any:
            return []
        return content_blocks

    @staticmethod
    def _current_upload_virtual_path(entry: AuthorityManifestEntry) -> str:
        if type(entry.logical_path) is not str:
            raise RuntimeError(CURRENT_UPLOAD_FAILURE_DETAIL)
        logical_path = PurePosixPath(entry.logical_path)
        if entry.logical_path != logical_path.as_posix() or len(logical_path.parts) < 2 or logical_path.parts[0] != "uploads" or any(part in {"", ".", ".."} for part in logical_path.parts):
            raise RuntimeError(CURRENT_UPLOAD_FAILURE_DETAIL)
        return f"/mnt/user-data/uploads/{PurePosixPath(*logical_path.parts[1:]).as_posix()}"

    @staticmethod
    def _current_upload_file_ref(
        runtime: Runtime,
        image_path: str,
    ) -> dict[str, str]:
        context = runtime.context
        if not isinstance(context, Mapping):
            raise RuntimeError(CURRENT_UPLOAD_FAILURE_DETAIL)
        private_scope = context.get("private_scope")
        run_id = context.get("run_id")
        if type(private_scope) is not PrivateResourceScope or not isinstance(run_id, str) or not run_id:
            raise RuntimeError(CURRENT_UPLOAD_FAILURE_DETAIL)
        try:
            authority = require_private_file_authority(
                context,
                method="current_uploads",
            )
        except RuntimeError:
            raise RuntimeError(CURRENT_UPLOAD_FAILURE_DETAIL) from None
        sandbox_id = getattr(authority, "sandbox_id", None)
        if not isinstance(sandbox_id, str) or not sandbox_id:
            raise RuntimeError(CURRENT_UPLOAD_FAILURE_DETAIL)
        return {
            "path": image_path,
            "sandbox_id": sandbox_id,
            "run_id": run_id,
            "project_id": private_scope.project_id,
            "owner_user_id": private_scope.owner_user_id,
        }

    def _current_upload_images(
        self,
        runtime: Runtime,
    ) -> tuple[tuple[str, ViewedImageData], ...]:
        context = runtime.context
        if not isinstance(context, Mapping) or context.get("is_subagent") is True:
            return ()
        try:
            authority = require_private_file_authority(
                context,
                method="current_uploads",
            )
        except RuntimeError:
            raise RuntimeError(CURRENT_UPLOAD_FAILURE_DETAIL) from None
        if authority is None:
            return ()
        try:
            entries = authority.current_uploads()
        except (AttributeError, KeyError, RuntimeError, TypeError, ValueError):
            raise RuntimeError(CURRENT_UPLOAD_FAILURE_DETAIL) from None
        if type(entries) is not tuple:
            raise RuntimeError(CURRENT_UPLOAD_FAILURE_DETAIL)

        images: list[tuple[str, ViewedImageData]] = []
        seen_ids: set[str] = set()
        seen_content: set[tuple[str, int, str]] = set()
        total_bytes = 0
        for entry in entries:
            if type(entry) is not AuthorityManifestEntry or not isinstance(entry.file_id, UUID) or entry.kind != "upload" or type(entry.version) is not int or isinstance(entry.version, bool) or entry.version < 1:
                raise RuntimeError(CURRENT_UPLOAD_FAILURE_DETAIL)
            file_id = str(entry.file_id)
            if file_id in seen_ids:
                raise RuntimeError(CURRENT_UPLOAD_FAILURE_DETAIL)
            seen_ids.add(file_id)

            image_path = self._current_upload_virtual_path(entry)
            extension = PurePosixPath(image_path).suffix.lower()
            expected_mime = EXTENSION_TO_MIME.get(extension)
            is_declared_image = isinstance(entry.media_type, str) and entry.media_type.startswith("image/")
            if expected_mime is None and not is_declared_image:
                continue
            if (
                expected_mime is None
                or entry.media_type != expected_mime
                or not isinstance(entry.size, int)
                or isinstance(entry.size, bool)
                or not 0 < entry.size <= MAX_IMAGE_BYTES
                or not isinstance(entry.sha256, str)
                or len(entry.sha256) != 64
                or any(character not in "0123456789abcdef" for character in entry.sha256)
            ):
                raise RuntimeError(CURRENT_UPLOAD_FAILURE_DETAIL)

            identity = (entry.sha256, entry.size, entry.media_type)
            if identity in seen_content:
                continue
            seen_content.add(identity)
            total_bytes += entry.size
            if len(images) >= _MAX_CURRENT_UPLOAD_IMAGES or total_bytes > _MAX_CURRENT_UPLOAD_TOTAL_BYTES:
                raise RuntimeError(CURRENT_UPLOAD_FAILURE_DETAIL)
            images.append(
                (
                    image_path,
                    {
                        "mime_type": entry.media_type,
                        "size": entry.size,
                        "sha256": entry.sha256,
                        "file_ref": self._current_upload_file_ref(runtime, image_path),
                    },
                )
            )
        return tuple(images)

    def _create_current_upload_image_content(
        self,
        runtime: Runtime,
        *,
        state: Mapping[str, object],
        cancel_event: threading.Event | None = None,
    ) -> tuple[list[str | dict], frozenset[str]]:
        images = self._current_upload_images(runtime)
        if not images:
            return [], frozenset()

        content: list[str | dict] = [
            {
                "type": "text",
                "text": "Here are the images attached to the current user message:",
            }
        ]
        injected_sha256: set[str] = set()
        for index, (image_path, image_data) in enumerate(images, start=1):
            data_url = self._read_image_as_data_url(
                runtime,
                image_path,
                image_data,
                state=state,
                cancel_event=cancel_event,
            )
            if data_url is None:
                raise RuntimeError(CURRENT_UPLOAD_FAILURE_DETAIL)
            content.extend(
                (
                    {
                        "type": "text",
                        "text": f"\n- Current image attachment {index} ({image_data['mime_type']})",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url},
                    },
                )
            )
            injected_sha256.add(image_data["sha256"])
        return content, frozenset(injected_sha256)

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
        if runtime is None or not self.enable_injection:
            return request
        image_content, current_sha256 = self._create_current_upload_image_content(
            runtime,
            state=state,
            cancel_event=cancel_event,
        )
        if self._should_inject_image_message(state):
            image_content.extend(
                self._create_image_details_message(
                    state,
                    runtime,
                    cancel_event=cancel_event,
                    excluded_sha256=current_sha256,
                )
            )
        if not image_content:
            return request
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
