import base64
import hashlib
import importlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from deerflow.tools.builtins.view_image_tool import view_image_tool

view_image_module = importlib.import_module("deerflow.tools.builtins.view_image_tool")

PNG_BYTES = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def _make_thread_data(tmp_path: Path) -> dict[str, str]:
    user_data = tmp_path / "threads" / "thread-1" / "user-data"
    workspace = user_data / "workspace"
    uploads = user_data / "uploads"
    outputs = user_data / "outputs"
    for directory in (workspace, uploads, outputs):
        directory.mkdir(parents=True)

    return {
        "workspace_path": str(workspace),
        "uploads_path": str(uploads),
        "outputs_path": str(outputs),
    }


def _make_runtime(thread_data: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(
        state={"thread_data": thread_data},
        context={"thread_id": "thread-1"},
        config={},
    )


class _PathBackedSandbox:
    def __init__(self, thread_data: dict[str, str]) -> None:
        self.id = "sandbox-test"
        self._thread_data = thread_data
        self._readers: dict[str, object] = {}

    def _actual_path(self, virtual_path: str) -> Path:
        roots = {
            "/mnt/user-data/workspace": "workspace_path",
            "/mnt/user-data/uploads": "uploads_path",
            "/mnt/user-data/outputs": "outputs_path",
        }
        for virtual_root, state_key in roots.items():
            if virtual_path == virtual_root or virtual_path.startswith(f"{virtual_root}/"):
                relative = virtual_path[len(virtual_root) :].lstrip("/")
                path = Path(self._thread_data[state_key]) / relative
                if path.is_symlink():
                    raise PermissionError(f"path traversal rejected: {virtual_path}")
                return path
        raise PermissionError(f"path traversal rejected: {virtual_path}")

    def open_regular_file(self, path: str) -> str:
        actual_path = self._actual_path(path)
        reader = open(actual_path, "rb")
        handle = f"reader-{len(self._readers)}"
        self._readers[handle] = reader
        return handle

    def read_regular_file(self, handle: str, max_bytes: int) -> bytes:
        return self._readers[handle].read(max_bytes)

    def close_regular_file(self, handle: str) -> None:
        reader = self._readers.pop(handle)
        reader.close()


@pytest.fixture(autouse=True)
def _use_path_backed_sandbox(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        view_image_module,
        "_image_sandbox",
        lambda runtime: _PathBackedSandbox(runtime.state["thread_data"]),
    )


def _message_content(result) -> str:
    return result.update["messages"][0].content


def test_view_image_rejects_external_absolute_path(tmp_path: Path) -> None:
    thread_data = _make_thread_data(tmp_path)
    outside_image = tmp_path / "outside.png"
    outside_image.write_bytes(PNG_BYTES)

    result = view_image_tool.func(
        runtime=_make_runtime(thread_data),
        image_path=str(outside_image),
        tool_call_id="tc-external",
    )

    assert "Only image paths under /mnt/user-data" in _message_content(result)
    assert "viewed_images" not in result.update


def test_view_image_reads_virtual_uploads_path(tmp_path: Path) -> None:
    thread_data = _make_thread_data(tmp_path)
    image_path = Path(thread_data["uploads_path"]) / "sample.png"
    image_path.write_bytes(PNG_BYTES)

    result = view_image_tool.func(
        runtime=_make_runtime(thread_data),
        image_path="/mnt/user-data/uploads/sample.png",
        tool_call_id="tc-uploads",
    )

    assert _message_content(result) == "Successfully read image"
    viewed_image = result.update["viewed_images"]["/mnt/user-data/uploads/sample.png"]
    assert viewed_image == {
        "mime_type": "image/png",
        "size": len(PNG_BYTES),
        "sha256": hashlib.sha256(PNG_BYTES).hexdigest(),
        "file_ref": {
            "path": "/mnt/user-data/uploads/sample.png",
            "sandbox_id": "sandbox-test",
            "run_id": "legacy:thread-1",
        },
    }


def test_view_image_rejects_spoofed_extension(tmp_path: Path) -> None:
    thread_data = _make_thread_data(tmp_path)
    image_path = Path(thread_data["uploads_path"]) / "not-really.png"
    image_path.write_bytes(b"not an image")

    result = view_image_tool.func(
        runtime=_make_runtime(thread_data),
        image_path="/mnt/user-data/uploads/not-really.png",
        tool_call_id="tc-spoofed",
    )

    assert "contents do not match" in _message_content(result)
    assert "viewed_images" not in result.update


def test_view_image_rejects_mismatched_magic_bytes(tmp_path: Path) -> None:
    thread_data = _make_thread_data(tmp_path)
    image_path = Path(thread_data["uploads_path"]) / "jpeg-named-png.png"
    image_path.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")

    result = view_image_tool.func(
        runtime=_make_runtime(thread_data),
        image_path="/mnt/user-data/uploads/jpeg-named-png.png",
        tool_call_id="tc-mismatch",
    )

    assert "file extension indicates image/png" in _message_content(result)
    assert "viewed_images" not in result.update


def test_view_image_rejects_oversized_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    thread_data = _make_thread_data(tmp_path)
    image_path = Path(thread_data["uploads_path"]) / "sample.png"
    image_path.write_bytes(PNG_BYTES)
    monkeypatch.setattr(view_image_module, "_MAX_IMAGE_BYTES", len(PNG_BYTES) - 1)

    result = view_image_tool.func(
        runtime=_make_runtime(thread_data),
        image_path="/mnt/user-data/uploads/sample.png",
        tool_call_id="tc-oversized",
    )

    assert "Image file is too large" in _message_content(result)
    assert "viewed_images" not in result.update


def test_view_image_sanitizes_read_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    thread_data = _make_thread_data(tmp_path)
    image_path = Path(thread_data["uploads_path"]) / "sample.png"
    image_path.write_bytes(PNG_BYTES)

    def _open(*args, **kwargs):
        raise PermissionError(f"permission denied: {image_path}")

    monkeypatch.setattr("builtins.open", _open)

    result = view_image_tool.func(
        runtime=_make_runtime(thread_data),
        image_path="/mnt/user-data/uploads/sample.png",
        tool_call_id="tc-read-error",
    )

    message = _message_content(result)
    assert "Error reading image file" in message
    assert str(image_path) not in message
    assert str(Path(thread_data["uploads_path"])) not in message
    assert "/mnt/user-data/uploads/sample.png" in message
    assert "viewed_images" not in result.update


@pytest.mark.skipif(os.name == "nt", reason="symlink semantics differ on Windows")
def test_view_image_rejects_uploads_symlink_escape(tmp_path: Path) -> None:
    thread_data = _make_thread_data(tmp_path)
    outside_image = tmp_path / "outside-target.png"
    outside_image.write_bytes(PNG_BYTES)

    link_path = Path(thread_data["uploads_path"]) / "escape.png"
    try:
        link_path.symlink_to(outside_image)
    except OSError as exc:
        pytest.skip(f"symlink creation failed: {exc}")

    result = view_image_tool.func(
        runtime=_make_runtime(thread_data),
        image_path="/mnt/user-data/uploads/escape.png",
        tool_call_id="tc-symlink",
    )

    assert "path traversal" in _message_content(result)
    assert "viewed_images" not in result.update
