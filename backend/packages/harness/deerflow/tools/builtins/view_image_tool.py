import hashlib
from collections.abc import Mapping
from pathlib import PurePosixPath
from threading import Event
from typing import Annotated

from langchain.tools import InjectedToolCallId, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from deerflow.agents.thread_state import ThreadDataState
from deerflow.config.paths import VIRTUAL_PATH_PREFIX
from deerflow.file_authority import require_private_file_authority
from deerflow.private_scope import PrivateResourceScope
from deerflow.sandbox.sandbox import PRIVATE_FILE_IO_CHUNK_SIZE, Sandbox
from deerflow.tools.types import Runtime

_ALLOWED_IMAGE_VIRTUAL_ROOTS = (
    f"{VIRTUAL_PATH_PREFIX}/workspace",
    f"{VIRTUAL_PATH_PREFIX}/uploads",
    f"{VIRTUAL_PATH_PREFIX}/outputs",
)
_ALLOWED_IMAGE_VIRTUAL_ROOTS_TEXT = ", ".join(_ALLOWED_IMAGE_VIRTUAL_ROOTS)
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_EXTENSION_TO_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


class _ImageTooLargeError(ValueError):
    pass


def ensure_sandbox_initialized(runtime: Runtime) -> Sandbox:
    """Lazy import avoids the sandbox-tools ↔ builtin-tools package cycle."""

    from deerflow.sandbox.tools import ensure_sandbox_initialized as initialize

    return initialize(runtime)


def sandbox_from_runtime(runtime: Runtime) -> Sandbox:
    """Lazy import avoids the sandbox-tools ↔ builtin-tools package cycle."""

    from deerflow.sandbox.tools import sandbox_from_runtime as resolve

    return resolve(runtime)


def _is_allowed_image_virtual_path(image_path: str) -> bool:
    return any(image_path == root or image_path.startswith(f"{root}/") for root in _ALLOWED_IMAGE_VIRTUAL_ROOTS)


def _detect_image_mime(image_data: bytes) -> str | None:
    if image_data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(image_data) >= 12 and image_data.startswith(b"RIFF") and image_data[8:12] == b"WEBP":
        return "image/webp"
    if image_data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return None


def _sanitize_image_error(error: Exception, thread_data: ThreadDataState | None) -> str:
    from deerflow.sandbox.tools import mask_local_paths_in_output

    return mask_local_paths_in_output(f"{type(error).__name__}: {error}", thread_data)


def _read_bounded_image_bytes(
    sandbox: Sandbox,
    image_path: str,
    *,
    max_bytes: int | None = None,
    cancel_event: Event | None = None,
) -> bytes:
    """Read one regular sandbox file through the secure, bounded authority API."""

    limit = _MAX_IMAGE_BYTES if max_bytes is None else max_bytes
    handle = sandbox.open_regular_file(image_path)
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("Private image read was cancelled")
            chunk = sandbox.read_regular_file(handle, PRIVATE_FILE_IO_CHUNK_SIZE)
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("Private image read was cancelled")
            if not isinstance(chunk, bytes):
                raise OSError("Private image reader returned invalid data")
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise _ImageTooLargeError
            chunks.append(chunk)
    finally:
        sandbox.close_regular_file(handle)
    return b"".join(chunks)


def _image_sandbox(runtime: Runtime) -> Sandbox:
    """Resolve the current sandbox without replacing a private Run lease."""

    context = runtime.context or {}
    authority = require_private_file_authority(context)
    if authority is None:
        return ensure_sandbox_initialized(runtime)

    sandbox = sandbox_from_runtime(runtime)
    authority_sandbox_id = getattr(authority, "sandbox_id", None)
    if not isinstance(authority_sandbox_id, str) or not authority_sandbox_id or authority_sandbox_id != sandbox.id:
        raise RuntimeError("Private file authority is unavailable")
    return sandbox


def _run_bound_file_ref(
    runtime: Runtime,
    sandbox: Sandbox,
    image_path: str,
) -> dict[str, str]:
    """Build checkpoint-safe coordinates that must be reauthorized on read."""

    context = runtime.context
    if not isinstance(context, Mapping):
        raise RuntimeError("Image viewing requires runtime context")

    run_id = context.get("run_id")
    private_scope = context.get("private_scope")
    if private_scope is not None:
        if type(private_scope) is not PrivateResourceScope:
            raise RuntimeError("Private Run scope is unavailable")
        if not isinstance(run_id, str) or not run_id:
            raise RuntimeError("Private Run identity is unavailable")
        return {
            "path": image_path,
            "sandbox_id": sandbox.id,
            "run_id": run_id,
            "project_id": private_scope.project_id,
            "owner_user_id": private_scope.owner_user_id,
        }

    if not isinstance(run_id, str) or not run_id:
        thread_id = context.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            raise RuntimeError("Image viewing requires a Run identity")
        run_id = f"legacy:{thread_id}"
    return {
        "path": image_path,
        "sandbox_id": sandbox.id,
        "run_id": run_id,
    }


@tool("view_image", parse_docstring=True)
def view_image_tool(
    runtime: Runtime,
    image_path: str,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Read an image file.

    Use this tool to read an image file and make it available for display.

    When to use the view_image tool:
    - When you need to view an image file.

    When NOT to use the view_image tool:
    - For non-image files (use present_files instead)
    - For multiple files at once (use present_files instead)

    Args:
        image_path: Absolute /mnt/user-data virtual path to the image file. Common formats supported: jpg, jpeg, png, webp.
    """
    from deerflow.sandbox.exceptions import SandboxError
    from deerflow.sandbox.tools import get_thread_data

    thread_data = get_thread_data(runtime)

    if not _is_allowed_image_virtual_path(image_path):
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        f"Error: Only image paths under {_ALLOWED_IMAGE_VIRTUAL_ROOTS_TEXT} are allowed",
                        tool_call_id=tool_call_id,
                    )
                ]
            },
        )

    # Validate image extension
    extension = PurePosixPath(image_path).suffix.lower()
    expected_mime_type = _EXTENSION_TO_MIME.get(extension)
    if expected_mime_type is None:
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        f"Error: Unsupported image format: {extension}. Supported formats: {', '.join(_EXTENSION_TO_MIME)}",
                        tool_call_id=tool_call_id,
                    )
                ]
            },
        )

    try:
        sandbox = _image_sandbox(runtime)
        image_data = _read_bounded_image_bytes(sandbox, image_path)
        file_ref = _run_bound_file_ref(runtime, sandbox, image_path)
    except _ImageTooLargeError:
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        f"Error: Image file is too large. Maximum supported size is {_MAX_IMAGE_BYTES} bytes",
                        tool_call_id=tool_call_id,
                    )
                ]
            },
        )
    except SandboxError:
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        "Error reading image file: Sandbox is unavailable",
                        tool_call_id=tool_call_id,
                    )
                ]
            },
        )
    except (OSError, PermissionError, RuntimeError) as e:
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        f"Error reading image file: {_sanitize_image_error(e, thread_data)}",
                        tool_call_id=tool_call_id,
                    )
                ]
            },
        )

    detected_mime_type = _detect_image_mime(image_data)
    if detected_mime_type is None:
        return Command(
            update={"messages": [ToolMessage("Error: File contents do not match a supported image format", tool_call_id=tool_call_id)]},
        )
    if detected_mime_type != expected_mime_type:
        return Command(
            update={"messages": [ToolMessage(f"Error: Image contents are {detected_mime_type}, but file extension indicates {expected_mime_type}", tool_call_id=tool_call_id)]},
        )
    # Persist only a Run-bound, reauthorizable reference plus immutable content
    # metadata. Image bytes are injected ephemerally immediately before the
    # model call and never enter checkpoint state.
    new_viewed_images = {
        image_path: {
            "mime_type": detected_mime_type,
            "size": len(image_data),
            "sha256": hashlib.sha256(image_data).hexdigest(),
            "file_ref": file_ref,
        }
    }

    return Command(
        update={"viewed_images": new_viewed_images, "messages": [ToolMessage("Successfully read image", tool_call_id=tool_call_id)]},
    )
