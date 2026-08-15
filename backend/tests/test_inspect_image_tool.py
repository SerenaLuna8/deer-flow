"""P1 ``inspect_image`` registration, provenance and tool-result behavior."""

from __future__ import annotations

import asyncio
import io
import json
import uuid
from threading import Event
from types import SimpleNamespace

import pytest
from langchain.tools import tool
from PIL import Image

from deerflow.agents.middlewares.tool_result_sanitization_middleware import (
    ToolResultSanitizationMiddleware,
)
from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig
from deerflow.config.sandbox_config import SandboxConfig
from deerflow.tools.builtins.inspect_image_tool import build_inspect_image_tool
from deerflow.vision.client import VisionClientError
from deerflow.vision.provenance import is_vision_evidence_tool


class _Sandbox:
    id = "sandbox-1"

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    def open_regular_file(self, path: str) -> object:
        assert path == "/mnt/user-data/uploads/image.png"
        self._offset = 0
        return object()

    def read_regular_file(self, handle: object, size: int) -> bytes:
        del handle
        start = self._offset
        self._offset += size
        return self._data[start : start + size]

    def close_regular_file(self, handle: object) -> None:
        del handle


def _png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (3, 2), "blue").save(output, format="PNG")
    return output.getvalue()


def _runtime_config() -> AppConfig:
    lead = ModelConfig(
        name="lead-text",
        display_name="Lead text",
        description="",
        use="deerflow.vision.fake_chat_model:FakeVisionBridgeChatModel",
        model="fake-lead",
        supports_vision=False,
    )
    vision = ModelConfig(
        name="vision-small-v1",
        display_name="Vision fake",
        description="",
        use="deerflow.vision.fake_chat_model:FakeVisionBridgeChatModel",
        model="fake-vision",
        supports_vision=True,
    )
    vision._system_model_config_version_id = uuid.uuid4()
    vision._system_provider_adapter = "vision_bridge_fake"
    return AppConfig(
        sandbox=SandboxConfig(
            use="deerflow.sandbox.local:LocalSandboxProvider",
        ),
        models=[lead, vision],
        vision_bridge={
            "model_name": "vision-small-v1",
            "timeout_seconds": 20,
            "contract_version": "vision.bridge.v1",
        },
    )


@pytest.mark.asyncio
async def test_inspect_image_returns_bounded_untrusted_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deerflow.tools.builtins.inspect_image_tool as module

    monkeypatch.setattr(module, "image_sandbox", lambda _runtime: _Sandbox(_png_bytes()))
    monkeypatch.setattr(
        module,
        "current_private_scope",
        lambda _runtime: SimpleNamespace(project_id="project"),
    )
    inspect_image = build_inspect_image_tool(app_config=_runtime_config())

    result = await inspect_image.coroutine(
        runtime=SimpleNamespace(context={"run_id": "run-1"}),
        image_path="/mnt/user-data/uploads/image.png",
        mode="describe",
        tool_call_id="call-1",
    )
    payload = json.loads(result.content)

    assert result.status == "success"
    assert payload["ok"] is True
    assert payload["content_type"] == "untrusted_image_evidence"
    assert payload["schema_version"] == "vision.evidence.v1"
    assert "/mnt/" not in result.content
    assert "vision-small-v1" not in result.content
    assert is_vision_evidence_tool(inspect_image)


@pytest.mark.asyncio
async def test_inspect_image_collapses_noncanonical_path_without_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deerflow.tools.builtins.inspect_image_tool as module

    monkeypatch.setattr(
        module,
        "current_private_scope",
        lambda _runtime: SimpleNamespace(project_id="project"),
    )
    monkeypatch.setattr(
        module,
        "image_sandbox",
        lambda _runtime: (_ for _ in ()).throw(
            AssertionError("invalid path must not reach Sandbox"),
        ),
    )
    inspect_image = build_inspect_image_tool(app_config=_runtime_config())

    result = await inspect_image.coroutine(
        runtime=SimpleNamespace(context={"run_id": "run-1"}),
        image_path="/mnt/user-data/uploads/../workspace/secret.png",
        mode="auto",
        tool_call_id="call-2",
    )

    assert result.status == "error"
    assert json.loads(result.content)["code"] == "IMAGE_UNAVAILABLE"
    assert result.additional_kwargs["error_code"] == "IMAGE_UNAVAILABLE"


def test_tool_schema_excludes_runtime_authority_and_provider_parameters() -> None:
    inspect_image = build_inspect_image_tool(app_config=_runtime_config())
    schema = inspect_image.args_schema.model_json_schema()

    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"image_path", "mode"}


def test_sanitizer_uses_registered_object_provenance_not_tool_name() -> None:
    inspect_image = build_inspect_image_tool(app_config=_runtime_config())

    @tool("inspect_image")
    def impostor(image_path: str) -> str:
        """Pretend to inspect an image."""

        return image_path

    middleware = ToolResultSanitizationMiddleware()

    assert middleware._should_sanitize(
        SimpleNamespace(
            tool=inspect_image,
            tool_call={"name": "renamed_by_model"},
        )
    )
    assert not middleware._should_sanitize(
        SimpleNamespace(
            tool=impostor,
            tool_call={"name": "inspect_image"},
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client_code", "expected_code"),
    [
        ("VISION_RATE_LIMITED", "VISION_RATE_LIMITED"),
        ("VISION_DEADLINE_EXCEEDED", "VISION_DEADLINE_EXCEEDED"),
        ("provider-secret-text", "VISION_UNAVAILABLE"),
    ],
)
async def test_inspect_image_collapses_client_failures_to_stable_codes(
    monkeypatch: pytest.MonkeyPatch,
    client_code: str,
    expected_code: str,
) -> None:
    import deerflow.tools.builtins.inspect_image_tool as module

    class ErrorClient:
        async def analyze(self, **_kwargs: object) -> object:
            raise VisionClientError(client_code)

    monkeypatch.setattr(module, "image_sandbox", lambda _runtime: _Sandbox(_png_bytes()))
    monkeypatch.setattr(
        module,
        "current_private_scope",
        lambda _runtime: SimpleNamespace(project_id="project"),
    )
    inspect_image = build_inspect_image_tool(
        app_config=_runtime_config(),
        client_factory=lambda _model, _contract: ErrorClient(),
    )

    result = await inspect_image.coroutine(
        runtime=SimpleNamespace(context={"run_id": "run-1"}),
        image_path="/mnt/user-data/uploads/image.png",
        mode="auto",
        tool_call_id="call-error",
    )

    assert result.status == "error"
    assert json.loads(result.content)["code"] == expected_code
    assert client_code not in result.content or client_code == expected_code


@pytest.mark.asyncio
async def test_inspect_image_rejects_noncanonical_client_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deerflow.tools.builtins.inspect_image_tool as module

    class InvalidEvidence:
        def canonical_json(self) -> str:
            raise ValueError("malformed provider output")

    class InvalidClient:
        async def analyze(self, **_kwargs: object) -> object:
            return SimpleNamespace(evidence=InvalidEvidence())

    monkeypatch.setattr(module, "image_sandbox", lambda _runtime: _Sandbox(_png_bytes()))
    monkeypatch.setattr(
        module,
        "current_private_scope",
        lambda _runtime: SimpleNamespace(project_id="project"),
    )
    inspect_image = build_inspect_image_tool(
        app_config=_runtime_config(),
        client_factory=lambda _model, _contract: InvalidClient(),
    )

    result = await inspect_image.coroutine(
        runtime=SimpleNamespace(context={"run_id": "run-1"}),
        image_path="/mnt/user-data/uploads/image.png",
        mode="document",
        tool_call_id="call-invalid",
    )

    assert result.status == "error"
    assert json.loads(result.content)["code"] == "VISION_SCHEMA_MISMATCH"


@pytest.mark.asyncio
async def test_cancelling_inspect_image_propagates_and_sets_abort_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deerflow.tools.builtins.inspect_image_tool as module

    started = asyncio.Event()
    captured: dict[str, object] = {}

    class BlockingClient:
        async def analyze(self, **kwargs: object) -> object:
            captured["abort_signal"] = kwargs["abort_signal"]
            started.set()
            await asyncio.Future()
            raise AssertionError("unreachable")

    monkeypatch.setattr(module, "image_sandbox", lambda _runtime: _Sandbox(_png_bytes()))
    monkeypatch.setattr(
        module,
        "current_private_scope",
        lambda _runtime: SimpleNamespace(project_id="project"),
    )
    inspect_image = build_inspect_image_tool(
        app_config=_runtime_config(),
        client_factory=lambda _model, _contract: BlockingClient(),
    )
    task = asyncio.create_task(
        inspect_image.coroutine(
            runtime=SimpleNamespace(context={"run_id": "run-1"}),
            image_path="/mnt/user-data/uploads/image.png",
            mode="chart",
            tool_call_id="call-cancel",
        )
    )
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert isinstance(captured["abort_signal"], Event)
    assert captured["abort_signal"].is_set()
