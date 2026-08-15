import hashlib
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Annotated

from langchain.tools import InjectedToolCallId, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from deerflow.private_scope import PrivateResourceScope
from deerflow.sandbox.sandbox import Sandbox
from deerflow.tools.types import Runtime
from deerflow.vision.image_input import (
    ALLOWED_IMAGE_VIRTUAL_ROOTS_TEXT,
    EXTENSION_TO_MIME,
    MAX_IMAGE_BYTES,
    ImageTooLargeError,
    detect_image_mime,
    image_sandbox,
    is_allowed_image_virtual_path,
    read_bounded_image_bytes,
    sanitize_image_error,
)

# Backward-compatible source name pinned by the repository guide constant test.
# The shared image module is now the single value authority.
_MAX_IMAGE_BYTES = MAX_IMAGE_BYTES


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

    if not is_allowed_image_virtual_path(image_path):
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        f"Error: Only image paths under {ALLOWED_IMAGE_VIRTUAL_ROOTS_TEXT} are allowed",
                        tool_call_id=tool_call_id,
                    )
                ]
            },
        )

    # Validate image extension
    extension = PurePosixPath(image_path).suffix.lower()
    expected_mime_type = EXTENSION_TO_MIME.get(extension)
    if expected_mime_type is None:
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        f"Error: Unsupported image format: {extension}. Supported formats: {', '.join(EXTENSION_TO_MIME)}",
                        tool_call_id=tool_call_id,
                    )
                ]
            },
        )

    try:
        sandbox = image_sandbox(runtime)
        image_data = read_bounded_image_bytes(sandbox, image_path)
        file_ref = _run_bound_file_ref(runtime, sandbox, image_path)
    except ImageTooLargeError:
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        f"Error: Image file is too large. Maximum supported size is {MAX_IMAGE_BYTES} bytes",
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
                        f"Error reading image file: {sanitize_image_error(e, thread_data)}",
                        tool_call_id=tool_call_id,
                    )
                ]
            },
        )

    detected_mime_type = detect_image_mime(image_data)
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
