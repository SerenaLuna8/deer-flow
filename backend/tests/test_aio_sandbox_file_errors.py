from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from deerflow.community.aio_sandbox.aio_sandbox import AioSandbox
from deerflow.sandbox.exceptions import SandboxFileError


class _FakeFileApi:
    def __init__(
        self,
        *,
        content: str = "",
        read_error: Exception | None = None,
        write_error: Exception | None = None,
    ) -> None:
        self.content = content
        self.read_error = read_error
        self.write_error = write_error
        self.writes: list[tuple[str, str]] = []

    def read_file(self, *, file: str):
        if self.read_error is not None:
            raise self.read_error
        return SimpleNamespace(data=SimpleNamespace(content=self.content))

    def write_file(self, *, file: str, content: str) -> None:
        if self.write_error is not None:
            raise self.write_error
        self.writes.append((file, content))


def _sandbox(file_api: _FakeFileApi) -> AioSandbox:
    sandbox = object.__new__(AioSandbox)
    sandbox._id = "test-aio"
    sandbox._client = SimpleNamespace(file=file_api)
    sandbox._lock = threading.Lock()
    return sandbox


def test_read_file_raises_typed_error_instead_of_returning_error_text() -> None:
    sandbox = _sandbox(_FakeFileApi(read_error=RuntimeError("transport failed")))

    with pytest.raises(SandboxFileError) as exc_info:
        sandbox.read_file("/mnt/user-data/workspace/input.txt")

    assert exc_info.value.path == "/mnt/user-data/workspace/input.txt"
    assert exc_info.value.operation == "read"
    assert "transport failed" not in str(exc_info.value)


def test_append_preserves_legitimate_content_starting_with_error_prefix() -> None:
    file_api = _FakeFileApi(content="Error: this is legitimate file content\n")
    sandbox = _sandbox(file_api)

    sandbox.write_file(
        "/mnt/user-data/workspace/output.txt",
        "appended\n",
        append=True,
    )

    assert file_api.writes == [
        (
            "/mnt/user-data/workspace/output.txt",
            "Error: this is legitimate file content\nappended\n",
        )
    ]


def test_append_aborts_when_existing_content_cannot_be_read() -> None:
    file_api = _FakeFileApi(read_error=RuntimeError("transport failed"))
    sandbox = _sandbox(file_api)

    with pytest.raises(SandboxFileError) as exc_info:
        sandbox.write_file(
            "/mnt/user-data/workspace/output.txt",
            "replacement that must not be written",
            append=True,
        )

    assert exc_info.value.operation == "read"
    assert file_api.writes == []


def test_write_file_wraps_provider_errors_without_exposing_raw_details() -> None:
    file_api = _FakeFileApi(write_error=RuntimeError("secret provider detail"))
    sandbox = _sandbox(file_api)

    with pytest.raises(SandboxFileError) as exc_info:
        sandbox.write_file(
            "/mnt/user-data/workspace/output.txt",
            "content",
        )

    assert exc_info.value.operation == "write"
    assert "secret provider detail" not in str(exc_info.value)
