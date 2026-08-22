"""Unified ``ModelRuntime`` coverage for ``inspect_image``."""

from __future__ import annotations

import asyncio
import io
import json
import uuid
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from PIL import Image

from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig
from deerflow.config.sandbox_config import SandboxConfig
from deerflow.models.runtime import ModelRuntimeProfile
from deerflow.runtime.context_keys import RuntimeContextKeys
from deerflow.tools.builtins.inspect_image_tool import (
    _analysis_text,
    _InspectImageCallFailure,
    build_inspect_image_tool,
)
from deerflow.vision.contracts import InspectImageResult, VisionUsageReceipt
from deerflow.vision.dispatch import VisionDispatchAttempt, VisionDispatchDenied

VISION_MODEL_REF = "00000000-0000-4000-8000-000000000406"
ANALYSIS_GOAL = "Analyze the content layout and visual hierarchy."


class _Sandbox:
    id = "sandbox-model-runtime"

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
    Image.new("RGB", (4, 3), "blue").save(output, format="PNG")
    return output.getvalue()


def _app_config() -> AppConfig:
    vision = ModelConfig(
        name=VISION_MODEL_REF,
        display_name="Any configured multimodal model",
        description="",
        use="langchain_openai:ChatOpenAI",
        model="provider-owned-model-id",
        api_key="test-only",
        supports_vision=True,
    )
    vision._system_model_config_id = uuid.uuid4()
    vision._system_provider_adapter = "openai"
    return AppConfig(
        sandbox=SandboxConfig(use="deerflow.sandbox.local:LocalSandboxProvider"),
        models=[vision],
        vision_bridge={
            "model_name": VISION_MODEL_REF,
            "timeout_seconds": 20,
            "contract_version": "vision.bridge.v1",
        },
    )


class _Authority:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    async def before_attempt(
        self,
        *,
        normalized_bytes: int,
        normalized_pixels: int,
    ) -> VisionDispatchAttempt:
        self.events.append(
            ("before", (normalized_bytes, normalized_pixels)),
        )
        return VisionDispatchAttempt()

    async def after_attempt(
        self,
        *,
        attempt: VisionDispatchAttempt,
        usage_receipt: VisionUsageReceipt,
        error_code: str | None,
    ) -> None:
        assert isinstance(attempt, VisionDispatchAttempt)
        self.events.append(("after", (usage_receipt, error_code)))


class _Journal:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def record_vision_usage(self, **kwargs: object) -> None:
        self.records.append(dict(kwargs))


class _Runtime:
    def __init__(self, response: AIMessage | BaseException) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    async def ainvoke(self, input_: object, **kwargs: object) -> AIMessage:
        self.calls.append({"input": input_, **kwargs})
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def _tool_context(
    authority: _Authority,
    journal: _Journal,
    *,
    server_abort_event: asyncio.Event | None = None,
) -> dict[str, object]:
    return {
        "run_id": "run-model-runtime",
        RuntimeContextKeys.VISION_DISPATCH_AUTHORITY: authority,
        RuntimeContextKeys.RUN_JOURNAL: journal,
        RuntimeContextKeys.SERVER_ABORT_EVENT: (server_abort_event if server_abort_event is not None else asyncio.Event()),
    }


def _patch_private_image(monkeypatch: pytest.MonkeyPatch) -> None:
    import deerflow.tools.builtins.inspect_image_tool as module

    monkeypatch.setattr(module, "image_sandbox", lambda _runtime: _Sandbox(_png_bytes()))
    monkeypatch.setattr(
        module,
        "current_private_scope",
        lambda _runtime: SimpleNamespace(project_id="project"),
    )


@pytest.mark.parametrize(
    "response_metadata",
    [
        {},
        {"model_provider": "test-fake"},
        {"status": "completed"},
        {"finish_reason": "stop"},
        {"stop_reason": "end_turn"},
        {"stop_reason": "stop_sequence"},
        {
            "status": "completed",
            "finish_reason": "stop",
            "stop_reason": "end_turn",
        },
    ],
)
def test_analysis_text_accepts_only_known_success_terminal_metadata(
    response_metadata: dict[str, object],
) -> None:
    text, receipt = _analysis_text(
        AIMessage(
            content="complete visual analysis",
            response_metadata=response_metadata,
        )
    )

    assert text == "complete visual analysis"
    assert receipt.request_dispatched is True


@pytest.mark.parametrize(
    ("response_metadata", "expected_code"),
    [
        ({"status": "incomplete"}, "VISION_SCHEMA_MISMATCH"),
        ({"status": "failed"}, "VISION_SCHEMA_MISMATCH"),
        ({"status": "in_progress"}, "VISION_SCHEMA_MISMATCH"),
        ({"status": "cancelled"}, "VISION_SCHEMA_MISMATCH"),
        ({"status": "queued"}, "VISION_SCHEMA_MISMATCH"),
        ({"status": "future_status"}, "VISION_SCHEMA_MISMATCH"),
        ({"status": None}, "VISION_SCHEMA_MISMATCH"),
        ({"finish_reason": "length"}, "VISION_SCHEMA_MISMATCH"),
        ({"finish_reason": "max_tokens"}, "VISION_SCHEMA_MISMATCH"),
        ({"finish_reason": "tool_calls"}, "VISION_SCHEMA_MISMATCH"),
        ({"finish_reason": "function_call"}, "VISION_SCHEMA_MISMATCH"),
        ({"finish_reason": "future_reason"}, "VISION_SCHEMA_MISMATCH"),
        ({"finish_reason": None}, "VISION_SCHEMA_MISMATCH"),
        ({"stop_reason": "max_tokens"}, "VISION_SCHEMA_MISMATCH"),
        ({"stop_reason": "tool_use"}, "VISION_SCHEMA_MISMATCH"),
        ({"stop_reason": "pause_turn"}, "VISION_SCHEMA_MISMATCH"),
        ({"stop_reason": "future_reason"}, "VISION_SCHEMA_MISMATCH"),
        ({"stop_reason": None}, "VISION_SCHEMA_MISMATCH"),
        ({"finish_reason": "content_filter"}, "VISION_CONTENT_BLOCKED"),
        ({"finish_reason": "refusal"}, "VISION_CONTENT_BLOCKED"),
        ({"stop_reason": "refusal"}, "VISION_CONTENT_BLOCKED"),
        (
            {"status": "completed", "stop_reason": "pause_turn"},
            "VISION_SCHEMA_MISMATCH",
        ),
    ],
)
def test_analysis_text_rejects_nonterminal_blocked_or_unknown_terminal_metadata(
    response_metadata: dict[str, object],
    expected_code: str,
) -> None:
    with pytest.raises(_InspectImageCallFailure) as exc_info:
        _analysis_text(
            AIMessage(
                content="partial or ambiguous visual analysis",
                response_metadata=response_metadata,
            )
        )

    assert exc_info.value.code == expected_code


@pytest.mark.asyncio
async def test_inspect_image_uses_selected_model_through_sensitive_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_private_image(monkeypatch)
    model_runtime = _Runtime(
        AIMessage(
            content="A blue rectangle is visible.",
            usage_metadata={
                "input_tokens": 31,
                "output_tokens": 7,
                "total_tokens": 38,
            },
        )
    )
    authority = _Authority()
    journal = _Journal()
    inspect_image = build_inspect_image_tool(
        app_config=_app_config(),
        model_runtime_factory=lambda _config: model_runtime,
    )

    message = await inspect_image.coroutine(
        runtime=SimpleNamespace(context=_tool_context(authority, journal)),
        image_path="/mnt/user-data/uploads/image.png",
        mode="describe",
        analysis_goal=ANALYSIS_GOAL,
        tool_call_id="call-unified-runtime",
    )

    assert message.status == "success"
    result = InspectImageResult.model_validate_json(message.content)
    assert result.text == "A blue rectangle is visible."
    assert result.mode == "describe"
    assert result.truncated is False
    assert message.additional_kwargs == {
        "content_type": "untrusted_image_analysis",
        "schema_version": "inspect_image.result.v2",
    }
    assert len(model_runtime.calls) == 1
    call = model_runtime.calls[0]
    assert call["profile"] is ModelRuntimeProfile.SENSITIVE_MULTIMODAL
    assert call["model_name"] == VISION_MODEL_REF
    assert call["deadline_monotonic"] is not None
    assert call["abort_event"] is not None
    messages = call["input"]
    assert isinstance(messages, list)
    assert isinstance(messages[0], SystemMessage)
    assert isinstance(messages[1], HumanMessage)
    assert ANALYSIS_GOAL not in str(messages[0].content)
    human_content = messages[1].content
    assert isinstance(human_content, list)
    text_blocks = [block for block in human_content if isinstance(block, dict) and block.get("type") == "text"]
    assert len(text_blocks) == 2
    assert ANALYSIS_GOAL not in text_blocks[0]["text"]
    assert ANALYSIS_GOAL in text_blocks[1]["text"]
    image_block = human_content[2]
    assert image_block["type"] == "image"
    assert image_block["mime_type"] == "image/jpeg"
    assert isinstance(image_block["base64"], str)
    assert image_block["base64"]
    assert [event[0] for event in authority.events] == ["before", "after"]
    receipt, error_code = authority.events[1][1]
    assert error_code is None
    assert receipt == VisionUsageReceipt(
        call_count=1,
        request_dispatched=True,
        input_tokens=31,
        output_tokens=7,
        usage_unknown=False,
    )
    assert journal.records == [
        {
            "source_id": "vision:run-model-runtime:call-unified-runtime",
            "model_name": VISION_MODEL_REF,
            "call_count": 1,
            "input_tokens": 31,
            "output_tokens": 7,
            "usage_unknown": False,
            "request_dispatched": True,
        }
    ]


@pytest.mark.asyncio
async def test_inspect_image_rejects_model_tool_calls_and_settles_unknown_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_private_image(monkeypatch)
    model_runtime = _Runtime(
        AIMessage(
            content="I will call a tool",
            tool_calls=[
                {
                    "name": "bash",
                    "args": {"command": "ignored"},
                    "id": "provider-tool-call",
                    "type": "tool_call",
                }
            ],
        )
    )
    authority = _Authority()
    journal = _Journal()
    inspect_image = build_inspect_image_tool(
        app_config=_app_config(),
        model_runtime_factory=lambda _config: model_runtime,
    )

    message = await inspect_image.coroutine(
        runtime=SimpleNamespace(context=_tool_context(authority, journal)),
        image_path="/mnt/user-data/uploads/image.png",
        mode="auto",
        analysis_goal=ANALYSIS_GOAL,
        tool_call_id="call-tool-call",
    )

    assert message.status == "error"
    assert json.loads(message.content)["code"] == "VISION_SCHEMA_MISMATCH"
    receipt, error_code = authority.events[1][1]
    assert error_code == "VISION_SCHEMA_MISMATCH"
    assert receipt.request_dispatched is True
    assert receipt.usage_unknown is True


@pytest.mark.asyncio
async def test_inspect_image_bounds_long_multibyte_model_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_private_image(monkeypatch)
    model_runtime = _Runtime(AIMessage(content="图" * 50_000))
    inspect_image = build_inspect_image_tool(
        app_config=_app_config(),
        model_runtime_factory=lambda _config: model_runtime,
    )

    message = await inspect_image.coroutine(
        runtime=SimpleNamespace(context=_tool_context(_Authority(), _Journal())),
        image_path="/mnt/user-data/uploads/image.png",
        mode="ocr",
        analysis_goal=ANALYSIS_GOAL,
        tool_call_id="call-long",
    )

    assert message.status == "success"
    result = InspectImageResult.model_validate_json(message.content)
    assert result.truncated is True
    assert len(message.content.encode("utf-8")) <= 24_000
    assert result.text


@pytest.mark.asyncio
async def test_inspect_image_maps_provider_failure_without_leaking_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_private_image(monkeypatch)

    class RateLimitError(RuntimeError):
        status_code = 429

    model_runtime = _Runtime(RateLimitError("secret provider response"))
    authority = _Authority()
    inspect_image = build_inspect_image_tool(
        app_config=_app_config(),
        model_runtime_factory=lambda _config: model_runtime,
    )

    message = await inspect_image.coroutine(
        runtime=SimpleNamespace(context=_tool_context(authority, _Journal())),
        image_path="/mnt/user-data/uploads/image.png",
        mode="chart",
        analysis_goal=ANALYSIS_GOAL,
        tool_call_id="call-rate-limit",
    )

    assert message.status == "error"
    assert json.loads(message.content)["code"] == "VISION_RATE_LIMITED"
    assert "secret provider response" not in message.content
    receipt, error_code = authority.events[1][1]
    assert error_code == "VISION_RATE_LIMITED"
    assert receipt.usage_unknown is True


@pytest.mark.asyncio
async def test_inspect_image_requires_durable_authority_before_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_private_image(monkeypatch)
    model_runtime = _Runtime(AIMessage(content="must not run"))
    inspect_image = build_inspect_image_tool(
        app_config=_app_config(),
        model_runtime_factory=lambda _config: model_runtime,
    )

    message = await inspect_image.coroutine(
        runtime=SimpleNamespace(context={"run_id": "run-no-authority"}),
        image_path="/mnt/user-data/uploads/image.png",
        mode="ui",
        analysis_goal=ANALYSIS_GOAL,
        tool_call_id="call-no-authority",
    )

    assert message.status == "error"
    assert json.loads(message.content)["code"] == "VISION_CONFIGURATION_ERROR"
    assert model_runtime.calls == []


@pytest.mark.asyncio
async def test_server_abort_cancels_runtime_and_settles_reserved_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_private_image(monkeypatch)
    started = asyncio.Event()

    class BlockingRuntime:
        async def ainvoke(self, _input: object, **kwargs: object) -> AIMessage:
            started.set()
            abort_event = kwargs["abort_event"]
            await abort_event.wait()
            raise asyncio.CancelledError

    authority = _Authority()
    journal = _Journal()
    server_abort = asyncio.Event()
    inspect_image = build_inspect_image_tool(
        app_config=_app_config(),
        model_runtime_factory=lambda _config: BlockingRuntime(),
    )
    task = asyncio.create_task(
        inspect_image.coroutine(
            runtime=SimpleNamespace(
                context=_tool_context(
                    authority,
                    journal,
                    server_abort_event=server_abort,
                )
            ),
            image_path="/mnt/user-data/uploads/image.png",
            mode="auto",
            analysis_goal=ANALYSIS_GOAL,
            tool_call_id="call-server-abort",
        )
    )
    await started.wait()
    server_abort.set()

    message = await task

    assert message.status == "error"
    assert json.loads(message.content)["code"] == "VISION_AUTH_FAILED"
    receipt, code = authority.events[1][1]
    assert code == "VISION_AUTH_FAILED"
    assert receipt.usage_unknown is True
    assert len(journal.records) == 1


@pytest.mark.asyncio
async def test_caller_cancellation_propagates_after_attempt_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_private_image(monkeypatch)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class BlockingRuntime:
        async def ainvoke(self, _input: object, **_kwargs: object) -> AIMessage:
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    authority = _Authority()
    journal = _Journal()
    inspect_image = build_inspect_image_tool(
        app_config=_app_config(),
        model_runtime_factory=lambda _config: BlockingRuntime(),
    )
    task = asyncio.create_task(
        inspect_image.coroutine(
            runtime=SimpleNamespace(context=_tool_context(authority, journal)),
            image_path="/mnt/user-data/uploads/image.png",
            mode="chart",
            analysis_goal=ANALYSIS_GOAL,
            tool_call_id="call-cancelled",
        )
    )
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert cancelled.is_set()
    assert [event[0] for event in authority.events] == ["before", "after"]
    receipt, code = authority.events[1][1]
    assert code == "VISION_AUTH_FAILED"
    assert receipt.usage_unknown is True
    assert len(journal.records) == 1


@pytest.mark.asyncio
async def test_post_response_authority_revocation_hides_result_but_keeps_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_private_image(monkeypatch)

    class RevokedAuthority(_Authority):
        async def after_attempt(self, **kwargs: object) -> None:
            await super().after_attempt(**kwargs)
            raise VisionDispatchDenied("VISION_AUTH_FAILED")

    model_runtime = _Runtime(
        AIMessage(
            content="must not reach lead model",
            usage_metadata={
                "input_tokens": 9,
                "output_tokens": 4,
                "total_tokens": 13,
            },
        )
    )
    authority = RevokedAuthority()
    journal = _Journal()
    inspect_image = build_inspect_image_tool(
        app_config=_app_config(),
        model_runtime_factory=lambda _config: model_runtime,
    )

    message = await inspect_image.coroutine(
        runtime=SimpleNamespace(context=_tool_context(authority, journal)),
        image_path="/mnt/user-data/uploads/image.png",
        mode="document",
        analysis_goal=ANALYSIS_GOAL,
        tool_call_id="call-revoked",
    )

    assert message.status == "error"
    assert json.loads(message.content)["code"] == "VISION_AUTH_FAILED"
    assert "must not reach" not in message.content
    assert journal.records[0]["input_tokens"] == 9
    assert journal.records[0]["output_tokens"] == 4
    assert journal.records[0]["usage_unknown"] is False


@pytest.mark.asyncio
async def test_provider_refusal_is_content_blocked_and_usage_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_private_image(monkeypatch)
    model_runtime = _Runtime(
        AIMessage(
            content="",
            additional_kwargs={"refusal": "provider refused"},
            usage_metadata={
                "input_tokens": 5,
                "output_tokens": 0,
                "total_tokens": 5,
            },
        )
    )
    authority = _Authority()
    journal = _Journal()
    inspect_image = build_inspect_image_tool(
        app_config=_app_config(),
        model_runtime_factory=lambda _config: model_runtime,
    )

    message = await inspect_image.coroutine(
        runtime=SimpleNamespace(context=_tool_context(authority, journal)),
        image_path="/mnt/user-data/uploads/image.png",
        mode="ocr",
        analysis_goal=ANALYSIS_GOAL,
        tool_call_id="call-refusal",
    )

    assert message.status == "error"
    assert json.loads(message.content)["code"] == "VISION_CONTENT_BLOCKED"
    receipt, code = authority.events[1][1]
    assert code == "VISION_CONTENT_BLOCKED"
    assert receipt.input_tokens == 5
    assert receipt.output_tokens == 0
    assert receipt.usage_unknown is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        AIMessage(
            content="partial output",
            response_metadata={"status": "incomplete"},
        ),
        AIMessage(
            content=[
                {
                    "type": "refusal",
                    "refusal": "blocked by provider",
                }
            ],
        ),
    ],
)
async def test_provider_incomplete_or_refusal_blocks_visual_result(
    monkeypatch: pytest.MonkeyPatch,
    response: AIMessage,
) -> None:
    _patch_private_image(monkeypatch)
    authority = _Authority()
    inspect_image = build_inspect_image_tool(
        app_config=_app_config(),
        model_runtime_factory=lambda _config: _Runtime(response),
    )

    message = await inspect_image.coroutine(
        runtime=SimpleNamespace(
            context=_tool_context(authority, _Journal()),
        ),
        image_path="/mnt/user-data/uploads/image.png",
        mode="describe",
        analysis_goal=ANALYSIS_GOAL,
        tool_call_id="call-provider-terminal-state",
    )

    assert message.status == "error"
    code = json.loads(message.content)["code"]
    expected = "VISION_CONTENT_BLOCKED" if isinstance(response.content, list) else "VISION_SCHEMA_MISMATCH"
    assert code == expected
