from __future__ import annotations

import logging
import threading
from types import SimpleNamespace

import httpx
import pytest
from agent_sandbox.core.api_error import ApiError
from agent_sandbox.types.response_file_read_result import ResponseFileReadResult
from pydantic import ValidationError

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


def test_private_aio_client_bypasses_environment_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_client(**kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        transport = kwargs["httpx_client"]
        return SimpleNamespace(
            _client_wrapper=SimpleNamespace(
                httpx_client=SimpleNamespace(httpx_client=transport),
            )
        )

    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.aio_sandbox.AioSandboxClient",
        fake_client,
    )

    sandbox = AioSandbox(id="test-aio", base_url="http://192.168.64.5:8080")

    transport = captured.get("httpx_client")
    assert isinstance(transport, httpx.Client)
    assert transport._trust_env is False
    sandbox.close()
    assert transport.is_closed is True


def test_read_file_raises_typed_error_instead_of_returning_error_text() -> None:
    sandbox = _sandbox(_FakeFileApi(read_error=RuntimeError("transport failed")))

    with pytest.raises(SandboxFileError) as exc_info:
        sandbox.read_file("/mnt/user-data/workspace/input.txt")

    assert exc_info.value.path == "/mnt/user-data/workspace/input.txt"
    assert exc_info.value.operation == "read"
    assert "transport failed" not in str(exc_info.value)


def test_read_file_normalizes_provider_missing_file_payload() -> None:
    path = "/mnt/user-data/outputs/new-report.md"
    with pytest.raises(ValidationError) as validation_error:
        ResponseFileReadResult.model_validate(
            {
                "data": {
                    "path": path,
                    "error_type": "not_found",
                    "exception_type": "FileNotFoundError",
                    "errno": 2,
                    "errno_name": "ENOENT",
                    "operation": "read",
                    "retryable": False,
                }
            }
        )
    sandbox = _sandbox(_FakeFileApi(read_error=validation_error.value))

    with pytest.raises(FileNotFoundError) as exc_info:
        sandbox.read_file(path)

    assert exc_info.value.filename == path


def test_read_file_keeps_unrecognized_provider_validation_error_typed() -> None:
    path = "/mnt/user-data/outputs/new-report.md"
    with pytest.raises(ValidationError) as validation_error:
        ResponseFileReadResult.model_validate(
            {
                "data": {
                    "path": path,
                    "error_type": "not_found",
                    "exception_type": "PermissionError",
                    "errno": 13,
                    "errno_name": "EACCES",
                    "operation": "read",
                    "retryable": False,
                }
            }
        )
    sandbox = _sandbox(_FakeFileApi(read_error=validation_error.value))

    with pytest.raises(SandboxFileError):
        sandbox.read_file(path)


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


def test_env_command_provider_error_never_logs_or_returns_secret_response_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "credential-must-not-enter-aio-logs"

    class FailingBashApi:
        def exec(self, **_kwargs: object) -> object:
            raise ApiError(
                status_code=422,
                headers={"content-type": "application/json"},
                body={"detail": {"input": secret}},
            )

    sandbox = object.__new__(AioSandbox)
    sandbox._id = "test-aio"
    sandbox._client = SimpleNamespace(bash=FailingBashApi())
    sandbox._lock = threading.Lock()
    sandbox._bash_exec_unsupported = False

    with caplog.at_level(
        logging.ERROR,
        logger="deerflow.community.aio_sandbox.aio_sandbox",
    ):
        result = sandbox.execute_command(
            'printf %s "$TOKEN"',
            env={"TOKEN": secret},
        )

    assert secret not in caplog.text
    assert secret not in result
    assert result == "Error: Sandbox command execution failed"
