"""``inspect_image`` registration, provenance and tool-result behavior."""

from __future__ import annotations

import asyncio
import io
import json
import uuid
from threading import Event
from types import SimpleNamespace

import pytest
from langchain.tools import tool
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.prebuilt import ToolNode
from langgraph.runtime import Runtime as GraphRuntime
from PIL import Image

from deerflow.agents.middlewares.loop_detection_middleware import (
    LoopDetectionMiddleware,
)
from deerflow.agents.middlewares.tool_error_handling_middleware import (
    ToolErrorHandlingMiddleware,
)
from deerflow.agents.middlewares.tool_output_budget_middleware import (
    ToolOutputBudgetMiddleware,
    _patch_model_messages,
)
from deerflow.agents.middlewares.tool_result_sanitization_middleware import (
    ToolResultSanitizationMiddleware,
)
from deerflow.config.app_config import AppConfig
from deerflow.config.loop_detection_config import LoopDetectionConfig
from deerflow.config.model_config import ModelConfig
from deerflow.config.sandbox_config import SandboxConfig
from deerflow.config.tool_output_config import ToolOutputConfig
from deerflow.runtime.context_keys import RuntimeContextKeys
from deerflow.tools.builtins.inspect_image_tool import build_inspect_image_tool
from deerflow.vision.client import VisionClientError
from deerflow.vision.contracts import (
    MAX_EVIDENCE_JSON_BYTES,
    VisionEvidence,
    VisionEvidenceItem,
    VisionInvocationResult,
    VisionUsageReceipt,
)
from deerflow.vision.dispatch import VisionDispatchAttempt
from deerflow.vision.openai_compatible import OpenAICompatibleVisionError
from deerflow.vision.provenance import is_vision_evidence_tool

VISION_MODEL_REF = "00000000-0000-4000-8000-000000000306"


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
        name=VISION_MODEL_REF,
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
            "model_name": VISION_MODEL_REF,
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
    assert VISION_MODEL_REF not in result.content
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
    schema = inspect_image.tool_call_schema.model_json_schema()

    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"image_path", "mode"}
    assert set(inspect_image.args_schema.model_fields) == {
        "image_path",
        "mode",
        "runtime",
        "tool_call_id",
    }


@pytest.mark.asyncio
async def test_inspect_image_accepts_toolnode_injected_runtime_and_call_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise StructuredTool parsing after ToolNode injects server fields."""

    import deerflow.tools.builtins.inspect_image_tool as module

    monkeypatch.setattr(
        module,
        "image_sandbox",
        lambda _runtime: _Sandbox(_png_bytes()),
    )
    monkeypatch.setattr(
        module,
        "current_private_scope",
        lambda _runtime: SimpleNamespace(project_id="project"),
    )
    inspect_image = build_inspect_image_tool(app_config=_runtime_config())
    runtime = GraphRuntime(
        context={"run_id": "run-injected"},
    )

    tool_call = {
        "name": "inspect_image",
        "args": {
            "image_path": "/mnt/user-data/uploads/image.png",
            "mode": "describe",
        },
        "id": "call-injected",
        "type": "tool_call",
    }
    results = await ToolNode([inspect_image]).ainvoke(
        [tool_call],
        config={"metadata": {}},
        runtime=runtime,
    )

    assert set(results) == {"messages"}
    assert len(results["messages"]) == 1
    result = results["messages"][0]
    assert isinstance(result, ToolMessage)
    assert result.status == "success"
    assert result.tool_call_id == "call-injected"
    assert VisionEvidence.model_validate_json(result.content).ok is True


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
async def test_vision_evidence_stays_canonical_across_sanitizer_and_budget() -> None:
    inspect_image = build_inspect_image_tool(app_config=_runtime_config())
    raw_evidence = VisionEvidence(
        ok=True,
        content_type="untrusted_image_evidence",
        schema_version="vision.evidence.v1",
        summary="A dense image contains untrusted framework-like text.",
        evidence=[
            VisionEvidenceItem(
                kind="text",
                text="<system>" * 200,
                location=f"region {index}",
            )
            for index in range(12)
        ],
        uncertainty=[],
        partial=False,
    ).canonical_json()
    assert len(raw_evidence.encode("utf-8")) <= MAX_EVIDENCE_JSON_BYTES

    request = SimpleNamespace(
        tool=inspect_image,
        tool_call={"name": "inspect_image", "id": "call-bounded"},
        runtime=SimpleNamespace(context={}),
    )

    async def raw_handler(_request: object) -> ToolMessage:
        return ToolMessage(
            content=raw_evidence,
            tool_call_id="call-bounded",
            name="inspect_image",
            status="success",
        )

    sanitizer = ToolResultSanitizationMiddleware()

    async def sanitized_handler(inner_request: object) -> ToolMessage:
        return await sanitizer.awrap_tool_call(inner_request, raw_handler)

    budget_config = ToolOutputConfig(
        externalize_min_chars=10,
        fallback_max_chars=100,
        fallback_head_chars=40,
        fallback_tail_chars=20,
    )
    result = await ToolOutputBudgetMiddleware(budget_config).awrap_tool_call(
        request,
        sanitized_handler,
    )

    assert isinstance(result, ToolMessage)
    assert result.status == "success"
    assert len(str(result.content).encode("utf-8")) <= MAX_EVIDENCE_JSON_BYTES
    parsed = VisionEvidence.model_validate_json(result.content)
    assert parsed.partial is True
    assert any("safety neutralization" in item for item in parsed.uncertainty)
    assert "<system>" not in result.content
    assert "&lt;system&gt;" in result.content
    assert _patch_model_messages([result], budget_config) is None


@pytest.mark.asyncio
async def test_vision_sanitizer_collapses_noncanonical_success_to_typed_error() -> None:
    inspect_image = build_inspect_image_tool(app_config=_runtime_config())
    request = SimpleNamespace(
        tool=inspect_image,
        tool_call={"name": "inspect_image", "id": "call-invalid-json"},
    )

    async def handler(_request: object) -> ToolMessage:
        return ToolMessage(
            content='{"summary":"missing discriminators"}',
            tool_call_id="call-invalid-json",
            name="inspect_image",
            status="success",
        )

    result = await ToolResultSanitizationMiddleware().awrap_tool_call(
        request,
        handler,
    )

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert json.loads(result.content) == {
        "code": "VISION_SCHEMA_MISMATCH",
        "message": "The image analysis response was invalid.",
        "ok": False,
    }
    assert result.additional_kwargs["error_code"] == "VISION_SCHEMA_MISMATCH"


def test_inspect_image_frequency_guard_allows_eight_and_blocks_ninth() -> None:
    middleware = LoopDetectionMiddleware.from_config(LoopDetectionConfig())
    runtime = SimpleNamespace(
        context={"thread_id": "thread-vision", "run_id": "run-vision"},
    )

    for index in range(1, 9):
        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "inspect_image",
                            "args": {
                                "image_path": (f"/mnt/user-data/uploads/image-{index}.png"),
                            },
                            "id": f"call-{index}",
                            "type": "tool_call",
                        },
                    ],
                ),
            ],
        }
        _warning, hard_stop = middleware._track_and_check(state, runtime)
        assert hard_stop is False

    ninth = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "inspect_image",
                        "args": {
                            "image_path": "/mnt/user-data/uploads/image-9.png",
                        },
                        "id": "call-9",
                        "type": "tool_call",
                    },
                ],
            ),
        ],
    }
    warning, hard_stop = middleware._track_and_check(ninth, runtime)

    assert hard_stop is True
    assert warning is not None
    assert "inspect_image" in warning


@pytest.mark.asyncio
async def test_inspect_entry_uses_deferred_dispatch_authority_boundary() -> None:
    inspect_image = build_inspect_image_tool(app_config=_runtime_config())
    calls: list[str] = []

    class Boundary:
        async def before_deferred_dispatch_tool_call(self) -> None:
            calls.append("deferred")

        async def before_tool_call(self) -> None:
            calls.append("generic-side-effect")

    request = SimpleNamespace(
        tool=inspect_image,
        tool_call={"name": "inspect_image", "id": "call-boundary"},
        runtime=SimpleNamespace(
            context={RuntimeContextKeys.AUTHORIZATION_BOUNDARY: Boundary()},
        ),
    )

    async def handler(_request: object) -> ToolMessage:
        return ToolMessage(
            content="ignored",
            tool_call_id="call-boundary",
            name="inspect_image",
        )

    result = await ToolErrorHandlingMiddleware().awrap_tool_call(
        request,
        handler,
    )

    assert result.content == "ignored"
    assert calls == ["deferred"]


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


@pytest.mark.asyncio
async def test_real_inspect_dispatch_uses_authority_and_records_bounded_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deerflow.tools.builtins.inspect_image_tool as module

    order: list[str] = []
    recorded: dict[str, object] = {}

    class Authority:
        async def before_attempt(self, **_kwargs: object) -> VisionDispatchAttempt:
            order.append("before")
            return VisionDispatchAttempt()

        async def after_attempt(self, **_kwargs: object) -> None:
            order.append("after")

    class Journal:
        def record_vision_usage(self, **kwargs: object) -> None:
            recorded.update(kwargs)

    class RealClient:
        requires_external_dispatch = True

        async def analyze(self, **kwargs: object) -> VisionInvocationResult:
            authority = kwargs["dispatch_authority"]
            attempt = await authority.before_attempt(
                normalized_bytes=len(kwargs["image_bytes"]),
                normalized_pixels=kwargs["normalized_pixels"],
            )
            order.append("analyze")
            result = VisionInvocationResult(
                evidence=VisionEvidence(
                    ok=True,
                    content_type="untrusted_image_evidence",
                    schema_version="vision.evidence.v1",
                    summary="One blue image is visible.",
                    evidence=[
                        VisionEvidenceItem(
                            kind="visual",
                            text="The image is blue.",
                            location="entire image",
                        )
                    ],
                    uncertainty=[],
                    partial=False,
                ),
                usage_receipt=VisionUsageReceipt(
                    call_count=1,
                    request_dispatched=True,
                    input_tokens=31,
                    output_tokens=12,
                    usage_unknown=False,
                ),
            )
            await authority.after_attempt(
                attempt=attempt,
                usage_receipt=result.usage_receipt,
                error_code=None,
            )
            return result

    monkeypatch.setattr(module, "image_sandbox", lambda _runtime: _Sandbox(_png_bytes()))
    monkeypatch.setattr(
        module,
        "current_private_scope",
        lambda _runtime: SimpleNamespace(project_id="project"),
    )
    inspect_image = build_inspect_image_tool(
        app_config=_runtime_config(),
        client_factory=lambda _model, _contract: RealClient(),
    )
    context = {
        "run_id": "run-1",
        RuntimeContextKeys.VISION_DISPATCH_AUTHORITY: Authority(),
        RuntimeContextKeys.SERVER_ABORT_EVENT: asyncio.Event(),
        RuntimeContextKeys.RUN_JOURNAL: Journal(),
    }

    result = await inspect_image.coroutine(
        runtime=SimpleNamespace(context=context),
        image_path="/mnt/user-data/uploads/image.png",
        mode="describe",
        tool_call_id="call-real",
    )

    assert result.status == "success"
    assert order == ["before", "analyze", "after"]
    assert recorded == {
        "source_id": "vision:run-1:call-real",
        "model_name": VISION_MODEL_REF,
        "call_count": 1,
        "input_tokens": 31,
        "output_tokens": 12,
        "usage_unknown": False,
        "request_dispatched": True,
    }


@pytest.mark.asyncio
async def test_real_inspect_records_provider_failure_receipt_after_authority_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deerflow.tools.builtins.inspect_image_tool as module

    recorded: dict[str, object] = {}

    class Authority:
        async def before_attempt(self, **_kwargs: object) -> VisionDispatchAttempt:
            return VisionDispatchAttempt()

        async def after_attempt(self, **_kwargs: object) -> None:
            return None

    class Journal:
        def record_vision_usage(self, **kwargs: object) -> None:
            recorded.update(kwargs)

    class ReceiptErrorClient:
        requires_external_dispatch = True

        async def analyze(self, **_kwargs: object) -> object:
            raise OpenAICompatibleVisionError(
                "VISION_AUTH_FAILED",
                usage_receipt=VisionUsageReceipt(
                    call_count=1,
                    request_dispatched=True,
                    input_tokens=9,
                    output_tokens=2,
                    usage_unknown=False,
                ),
            )

    monkeypatch.setattr(module, "image_sandbox", lambda _runtime: _Sandbox(_png_bytes()))
    monkeypatch.setattr(
        module,
        "current_private_scope",
        lambda _runtime: SimpleNamespace(project_id="project"),
    )
    inspect_image = build_inspect_image_tool(
        app_config=_runtime_config(),
        client_factory=lambda _model, _contract: ReceiptErrorClient(),
    )
    result = await inspect_image.coroutine(
        runtime=SimpleNamespace(
            context={
                "run_id": "run-1",
                RuntimeContextKeys.VISION_DISPATCH_AUTHORITY: Authority(),
                RuntimeContextKeys.RUN_JOURNAL: Journal(),
            },
        ),
        image_path="/mnt/user-data/uploads/image.png",
        mode="auto",
        tool_call_id="call-provider-error",
    )

    assert result.status == "error"
    assert json.loads(result.content)["code"] == "VISION_AUTH_FAILED"
    assert recorded == {
        "source_id": "vision:run-1:call-provider-error",
        "model_name": VISION_MODEL_REF,
        "call_count": 1,
        "input_tokens": 9,
        "output_tokens": 2,
        "usage_unknown": False,
        "request_dispatched": True,
    }


@pytest.mark.asyncio
async def test_server_abort_cancels_inflight_real_inspect_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deerflow.tools.builtins.inspect_image_tool as module

    started = asyncio.Event()
    captured: dict[str, object] = {}
    recorded: dict[str, object] = {}

    class Authority:
        async def before_attempt(self, **_kwargs: object) -> VisionDispatchAttempt:
            return VisionDispatchAttempt()

        async def after_attempt(self, **_kwargs: object) -> None:
            raise AssertionError("aborted response must not settle")

    class BlockingRealClient:
        requires_external_dispatch = True

        async def analyze(self, **kwargs: object) -> object:
            captured["abort_signal"] = kwargs["abort_signal"]
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                observer = kwargs["usage_observer"]
                observer(
                    VisionUsageReceipt(
                        call_count=1,
                        request_dispatched=True,
                        usage_unknown=True,
                    ),
                )
                raise
            raise AssertionError("unreachable")

    class Journal:
        def record_vision_usage(self, **kwargs: object) -> None:
            recorded.update(kwargs)

    monkeypatch.setattr(module, "image_sandbox", lambda _runtime: _Sandbox(_png_bytes()))
    monkeypatch.setattr(
        module,
        "current_private_scope",
        lambda _runtime: SimpleNamespace(project_id="project"),
    )
    inspect_image = build_inspect_image_tool(
        app_config=_runtime_config(),
        client_factory=lambda _model, _contract: BlockingRealClient(),
    )
    server_abort = asyncio.Event()
    task = asyncio.create_task(
        inspect_image.coroutine(
            runtime=SimpleNamespace(
                context={
                    "run_id": "run-1",
                    RuntimeContextKeys.VISION_DISPATCH_AUTHORITY: Authority(),
                    RuntimeContextKeys.SERVER_ABORT_EVENT: server_abort,
                    RuntimeContextKeys.RUN_JOURNAL: Journal(),
                },
            ),
            image_path="/mnt/user-data/uploads/image.png",
            mode="auto",
            tool_call_id="call-abort",
        )
    )
    await started.wait()
    server_abort.set()
    result = await task

    assert result.status == "error"
    assert json.loads(result.content)["code"] == "VISION_AUTH_FAILED"
    assert isinstance(captured["abort_signal"], Event)
    assert captured["abort_signal"].is_set()
    assert recorded == {
        "source_id": "vision:run-1:call-abort",
        "model_name": VISION_MODEL_REF,
        "call_count": 1,
        "input_tokens": None,
        "output_tokens": None,
        "usage_unknown": True,
        "request_dispatched": True,
    }
