"""Conformance tests for the narrow real Vision Bridge adapter."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from threading import Event

import httpx
import pytest
from pydantic import SecretStr

from app.system_settings.validation import (
    ModelSettingsInvalid,
    provider_credential_required,
    validate_model_settings,
)
from deerflow.config.model_config import ModelConfig
from deerflow.vision.client import VisionClientError, build_vision_evidence_client
from deerflow.vision.contracts import (
    VisionEvidence,
    VisionEvidenceItem,
    VisionUsageReceipt,
)
from deerflow.vision.dispatch import VisionDispatchAttempt, VisionDispatchDenied
from deerflow.vision.openai_compatible import (
    MAX_VISION_RESPONSE_BYTES,
    OpenAICompatibleVisionError,
    OpenAICompatibleVisionEvidenceClient,
)
from deerflow.vision.prompt import VISION_SYSTEM_PROMPT_V1

VISION_MODEL_REF = "00000000-0000-4000-8000-000000000306"


def _model() -> ModelConfig:
    model = ModelConfig(
        name=VISION_MODEL_REF,
        display_name="Vision small",
        description="",
        use="langchain_openai:ChatOpenAI",
        model="small-vlm",
        base_url="https://vision.example.test/v1",
        api_key=SecretStr("test-secret-value"),
        supports_vision=True,
    )
    model._system_model_config_version_id = uuid.uuid4()
    model._system_provider_adapter = "vision_openai_compatible_v1"
    return model


def _success_payload(*, extra_evidence_field: bool = False) -> dict[str, object]:
    evidence = json.loads(
        VisionEvidence(
            ok=True,
            content_type="untrusted_image_evidence",
            schema_version="vision.evidence.v1",
            summary="A blue diagram is visible.",
            evidence=[
                VisionEvidenceItem(
                    kind="visual",
                    text="The diagram contains one blue rectangle.",
                    location="center",
                )
            ],
            uncertainty=[],
            partial=False,
        ).canonical_json()
    )
    if extra_evidence_field:
        evidence["unexpected"] = "blocked"
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": json.dumps(evidence),
                },
            }
        ],
        "usage": {"prompt_tokens": 41, "completion_tokens": 17},
    }


class _AttemptAuthority:
    def __init__(
        self,
        *,
        deny_before_attempt: int | None = None,
        deny_after_attempt: int | None = None,
        abort_after_attempt: Event | None = None,
    ) -> None:
        self.deny_before_attempt = deny_before_attempt
        self.deny_after_attempt = deny_after_attempt
        self.abort_after_attempt = abort_after_attempt
        self.before_calls = 0
        self.after_calls: list[tuple[VisionUsageReceipt, str | None]] = []

    async def before_attempt(
        self,
        *,
        normalized_bytes: int,
        normalized_pixels: int,
    ) -> VisionDispatchAttempt:
        assert normalized_bytes > 0
        assert normalized_pixels > 0
        self.before_calls += 1
        if self.before_calls == self.deny_before_attempt:
            raise VisionDispatchDenied("VISION_AUTH_FAILED")
        return VisionDispatchAttempt()

    async def after_attempt(
        self,
        *,
        attempt: VisionDispatchAttempt,
        usage_receipt: VisionUsageReceipt,
        error_code: str | None,
    ) -> None:
        assert isinstance(attempt, VisionDispatchAttempt)
        self.after_calls.append((usage_receipt, error_code))
        if self.abort_after_attempt is not None:
            self.abort_after_attempt.set()
        if len(self.after_calls) == self.deny_after_attempt:
            raise VisionDispatchDenied("VISION_AUTH_FAILED")


@pytest.mark.asyncio
async def test_real_adapter_sends_only_fixed_single_image_schema_request() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_success_payload())

    client = OpenAICompatibleVisionEvidenceClient(
        _model(),
        transport=httpx.MockTransport(handler),
        transient_gate_key="vision-adapter-test",
    )
    result = await client.analyze(
        image_bytes=b"normalized-image",
        mime_type="image/jpeg",
        mode="chart",
        deadline_monotonic=time.monotonic() + 2,
        abort_signal=Event(),
    )

    assert result.evidence.summary == "A blue diagram is visible."
    assert result.usage_receipt.input_tokens == 41
    assert result.usage_receipt.output_tokens == 17
    assert result.usage_receipt.usage_unknown is False
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://vision.example.test/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer test-secret-value"
    payload = json.loads(request.content)
    assert set(payload) == {
        "max_tokens",
        "messages",
        "model",
        "response_format",
        "stream",
        "temperature",
    }
    assert payload["stream"] is False
    assert payload["temperature"] == 0
    assert payload["messages"][0]["role"] == "system"
    assert VISION_SYSTEM_PROMPT_V1 in payload["messages"][0]["content"]
    user_content = payload["messages"][1]["content"]
    assert [part["type"] for part in user_content] == ["text", "image_url"]
    assert user_content[1]["image_url"]["url"].startswith(
        "data:image/jpeg;base64,",
    )
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["strict"] is True
    schema = payload["response_format"]["json_schema"]["schema"]
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False
    assert "default" not in json.dumps(schema)


@pytest.mark.asyncio
async def test_real_adapter_retries_retryable_status_exactly_once() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, headers={"retry-after": "0"})
        return httpx.Response(200, json=_success_payload())

    client = OpenAICompatibleVisionEvidenceClient(
        _model(),
        transport=httpx.MockTransport(handler),
        transient_gate_key="vision-adapter-test",
    )

    result = await client.analyze(
        image_bytes=b"image",
        mime_type="image/png",
        mode="auto",
        deadline_monotonic=time.monotonic() + 2,
        abort_signal=Event(),
    )

    assert result.evidence.ok is True
    assert result.usage_receipt.call_count == 2
    assert calls == 2


@pytest.mark.asyncio
async def test_real_adapter_bounds_global_wait_queue_per_exact_model() -> None:
    release = asyncio.Event()
    four_started = asyncio.Event()
    started = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal started
        started += 1
        if started == 4:
            four_started.set()
        await release.wait()
        return httpx.Response(200, json=_success_payload())

    model = _model()
    transport = httpx.MockTransport(handler)

    async def invoke() -> object:
        return await OpenAICompatibleVisionEvidenceClient(
            model,
            transport=transport,
            transient_gate_key="vision-adapter-test",
        ).analyze(
            image_bytes=b"image",
            mime_type="image/png",
            mode="auto",
            deadline_monotonic=time.monotonic() + 5,
            abort_signal=Event(),
        )

    active = [asyncio.create_task(invoke()) for _ in range(4)]
    await four_started.wait()
    waiting = [asyncio.create_task(invoke()) for _ in range(16)]
    await asyncio.sleep(0)

    with pytest.raises(OpenAICompatibleVisionError) as caught:
        await invoke()
    assert caught.value.code == "VISION_BUSY"

    release.set()
    await asyncio.gather(*active, *waiting)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_code"),
    [(401, "VISION_AUTH_FAILED"), (302, "VISION_UNAVAILABLE")],
)
async def test_real_adapter_does_not_retry_non_retryable_status(
    status: int,
    expected_code: str,
) -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, headers={"location": "https://other.test"})

    client = OpenAICompatibleVisionEvidenceClient(
        _model(),
        transport=httpx.MockTransport(handler),
        transient_gate_key="vision-adapter-test",
    )

    with pytest.raises(OpenAICompatibleVisionError) as caught:
        await client.analyze(
            image_bytes=b"image",
            mime_type="image/png",
            mode="auto",
            deadline_monotonic=time.monotonic() + 2,
            abort_signal=Event(),
        )

    assert caught.value.code == expected_code
    assert caught.value.request_dispatched is True
    assert calls == 1


@pytest.mark.asyncio
async def test_real_adapter_rejects_oversized_response_before_buffering() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-length": str(MAX_VISION_RESPONSE_BYTES + 1)},
            content=b"{}",
        )

    client = OpenAICompatibleVisionEvidenceClient(
        _model(),
        transport=httpx.MockTransport(handler),
        transient_gate_key="vision-adapter-test",
    )

    with pytest.raises(OpenAICompatibleVisionError) as caught:
        await client.analyze(
            image_bytes=b"image",
            mime_type="image/png",
            mode="auto",
            deadline_monotonic=time.monotonic() + 2,
            abort_signal=Event(),
        )

    assert caught.value.code == "VISION_RESPONSE_TOO_LARGE"


@pytest.mark.asyncio
async def test_real_adapter_rejects_noncanonical_evidence() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_success_payload(extra_evidence_field=True),
        )

    client = OpenAICompatibleVisionEvidenceClient(
        _model(),
        transport=httpx.MockTransport(handler),
        transient_gate_key="vision-adapter-test",
    )

    with pytest.raises(OpenAICompatibleVisionError) as caught:
        await client.analyze(
            image_bytes=b"image",
            mime_type="image/png",
            mode="auto",
            deadline_monotonic=time.monotonic() + 2,
            abort_signal=Event(),
        )

    assert caught.value.code == "VISION_SCHEMA_MISMATCH"


@pytest.mark.asyncio
async def test_real_adapter_revalidates_every_retry_and_aggregates_attempt_usage() -> None:
    calls = 0
    authority = _AttemptAuthority()

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                503,
                headers={"retry-after": "0"},
                json={"usage": {"prompt_tokens": 5, "completion_tokens": 3}},
            )
        return httpx.Response(200, json=_success_payload())

    client = OpenAICompatibleVisionEvidenceClient(
        _model(),
        transport=httpx.MockTransport(handler),
    )
    result = await client.analyze(
        image_bytes=b"image",
        mime_type="image/png",
        mode="auto",
        deadline_monotonic=time.monotonic() + 2,
        abort_signal=Event(),
        dispatch_authority=authority,
        normalized_pixels=20,
    )

    assert calls == 2
    assert authority.before_calls == 2
    assert len(authority.after_calls) == 2
    assert [item[1] for item in authority.after_calls] == [
        "VISION_UNAVAILABLE",
        None,
    ]
    assert result.usage_receipt == VisionUsageReceipt(
        call_count=2,
        request_dispatched=True,
        input_tokens=46,
        output_tokens=20,
        usage_unknown=False,
    )


@pytest.mark.asyncio
async def test_real_adapter_marks_aggregate_unknown_when_any_attempt_usage_is_missing() -> None:
    calls = 0
    authority = _AttemptAuthority()

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, headers={"retry-after": "0"})
        return httpx.Response(200, json=_success_payload())

    result = await OpenAICompatibleVisionEvidenceClient(
        _model(),
        transport=httpx.MockTransport(handler),
    ).analyze(
        image_bytes=b"image",
        mime_type="image/png",
        mode="auto",
        deadline_monotonic=time.monotonic() + 2,
        abort_signal=Event(),
        dispatch_authority=authority,
        normalized_pixels=20,
    )

    assert result.usage_receipt.input_tokens == 41
    assert result.usage_receipt.output_tokens == 17
    assert result.usage_receipt.usage_unknown is True


@pytest.mark.asyncio
async def test_real_adapter_stops_retry_when_authority_is_revoked() -> None:
    calls = 0
    authority = _AttemptAuthority(deny_before_attempt=2)

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, headers={"retry-after": "0"})

    client = OpenAICompatibleVisionEvidenceClient(
        _model(),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(OpenAICompatibleVisionError) as caught:
        await client.analyze(
            image_bytes=b"image",
            mime_type="image/png",
            mode="auto",
            deadline_monotonic=time.monotonic() + 2,
            abort_signal=Event(),
            dispatch_authority=authority,
            normalized_pixels=20,
        )

    assert caught.value.code == "VISION_AUTH_FAILED"
    assert caught.value.usage_receipt is not None
    assert caught.value.usage_receipt.call_count == 1
    assert caught.value.usage_receipt.usage_unknown is True
    assert calls == 1
    assert authority.before_calls == 2
    assert len(authority.after_calls) == 1


@pytest.mark.asyncio
async def test_real_adapter_stops_retry_when_abort_arrives_after_first_attempt() -> None:
    calls = 0
    abort_signal = Event()
    authority = _AttemptAuthority(abort_after_attempt=abort_signal)

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, headers={"retry-after": "0"})

    client = OpenAICompatibleVisionEvidenceClient(
        _model(),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(OpenAICompatibleVisionError) as caught:
        await client.analyze(
            image_bytes=b"image",
            mime_type="image/png",
            mode="auto",
            deadline_monotonic=time.monotonic() + 2,
            abort_signal=abort_signal,
            dispatch_authority=authority,
            normalized_pixels=20,
        )

    assert caught.value.code == "VISION_DEADLINE_EXCEEDED"
    assert caught.value.usage_receipt is not None
    assert caught.value.usage_receipt.call_count == 1
    assert calls == 1
    assert authority.before_calls == 1


@pytest.mark.asyncio
async def test_real_adapter_preserves_receipt_when_after_authority_denies() -> None:
    authority = _AttemptAuthority(deny_after_attempt=1)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_success_payload())

    client = OpenAICompatibleVisionEvidenceClient(
        _model(),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(OpenAICompatibleVisionError) as caught:
        await client.analyze(
            image_bytes=b"image",
            mime_type="image/png",
            mode="auto",
            deadline_monotonic=time.monotonic() + 2,
            abort_signal=Event(),
            dispatch_authority=authority,
            normalized_pixels=20,
        )

    assert caught.value.code == "VISION_AUTH_FAILED"
    assert caught.value.usage_receipt == VisionUsageReceipt(
        call_count=1,
        request_dispatched=True,
        input_tokens=41,
        output_tokens=17,
        usage_unknown=False,
    )


@pytest.mark.asyncio
async def test_cancelled_inflight_attempt_finishes_authority_and_observes_receipt() -> None:
    started = asyncio.Event()
    authority = _AttemptAuthority()
    observed: list[VisionUsageReceipt] = []

    async def handler(_request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Future()
        raise AssertionError("unreachable")

    client = OpenAICompatibleVisionEvidenceClient(
        _model(),
        transport=httpx.MockTransport(handler),
    )
    task = asyncio.create_task(
        client.analyze(
            image_bytes=b"image",
            mime_type="image/png",
            mode="auto",
            deadline_monotonic=time.monotonic() + 2,
            abort_signal=Event(),
            dispatch_authority=authority,
            normalized_pixels=20,
            usage_observer=observed.append,
        ),
    )
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    expected = VisionUsageReceipt(
        call_count=1,
        request_dispatched=True,
        input_tokens=None,
        output_tokens=None,
        usage_unknown=True,
    )
    assert observed == [expected]
    assert authority.after_calls == [
        (expected, "VISION_DEADLINE_EXCEEDED"),
    ]


@pytest.mark.asyncio
async def test_outer_deadline_settles_inflight_unknown_usage() -> None:
    authority = _AttemptAuthority()
    observed: list[VisionUsageReceipt] = []

    async def handler(_request: httpx.Request) -> httpx.Response:
        await asyncio.Future()
        raise AssertionError("unreachable")

    client = OpenAICompatibleVisionEvidenceClient(
        _model(),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(OpenAICompatibleVisionError) as caught:
        await client.analyze(
            image_bytes=b"image",
            mime_type="image/png",
            mode="auto",
            deadline_monotonic=time.monotonic() + 0.05,
            abort_signal=Event(),
            dispatch_authority=authority,
            normalized_pixels=20,
            usage_observer=observed.append,
        )

    assert caught.value.code == "VISION_DEADLINE_EXCEEDED"
    assert caught.value.usage_receipt is not None
    assert caught.value.usage_receipt.usage_unknown is True
    assert caught.value.usage_receipt.call_count == 1
    assert observed == [caught.value.usage_receipt]
    assert len(authority.after_calls) == 1


@pytest.mark.asyncio
async def test_real_adapter_only_retries_confirmed_pre_dispatch_connect_failure() -> None:
    calls = 0
    authority = _AttemptAuthority()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("connect failed", request=request)
        return httpx.Response(200, json=_success_payload())

    result = await OpenAICompatibleVisionEvidenceClient(
        _model(),
        transport=httpx.MockTransport(handler),
    ).analyze(
        image_bytes=b"image",
        mime_type="image/png",
        mode="auto",
        deadline_monotonic=time.monotonic() + 2,
        abort_signal=Event(),
        dispatch_authority=authority,
        normalized_pixels=20,
    )

    assert calls == 2
    assert authority.before_calls == 2
    assert authority.after_calls[0][0].request_dispatched is False
    assert result.usage_receipt.call_count == 1
    assert result.usage_receipt.usage_unknown is False


@pytest.mark.asyncio
async def test_real_adapter_does_not_retry_ambiguous_read_failure() -> None:
    calls = 0
    authority = _AttemptAuthority()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadError("read failed", request=request)

    client = OpenAICompatibleVisionEvidenceClient(
        _model(),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(OpenAICompatibleVisionError) as caught:
        await client.analyze(
            image_bytes=b"image",
            mime_type="image/png",
            mode="auto",
            deadline_monotonic=time.monotonic() + 2,
            abort_signal=Event(),
            dispatch_authority=authority,
            normalized_pixels=20,
        )

    assert calls == 1
    assert authority.before_calls == 1
    assert len(authority.after_calls) == 1
    assert caught.value.usage_receipt == VisionUsageReceipt(
        call_count=1,
        request_dispatched=True,
        input_tokens=None,
        output_tokens=None,
        usage_unknown=True,
    )


@pytest.mark.asyncio
async def test_real_adapter_preserves_failure_usage_in_server_only_receipt() -> None:
    authority = _AttemptAuthority()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"usage": {"prompt_tokens": 7, "completion_tokens": 1}},
        )

    client = OpenAICompatibleVisionEvidenceClient(
        _model(),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(OpenAICompatibleVisionError) as caught:
        await client.analyze(
            image_bytes=b"image",
            mime_type="image/png",
            mode="auto",
            deadline_monotonic=time.monotonic() + 2,
            abort_signal=Event(),
            dispatch_authority=authority,
            normalized_pixels=20,
        )

    assert caught.value.code == "VISION_AUTH_FAILED"
    assert caught.value.usage_receipt == VisionUsageReceipt(
        call_count=1,
        request_dispatched=True,
        input_tokens=7,
        output_tokens=1,
        usage_unknown=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_code"),
    [(429, "VISION_RATE_LIMITED"), (503, "VISION_UNAVAILABLE")],
)
async def test_error_status_wins_over_oversized_error_body(
    status: int,
    expected_code: str,
) -> None:
    calls = 0
    authority = _AttemptAuthority()

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status,
            headers={
                "content-length": str(MAX_VISION_RESPONSE_BYTES + 1),
                "retry-after": "0",
            },
            content=b"{}",
        )

    client = OpenAICompatibleVisionEvidenceClient(
        _model(),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(OpenAICompatibleVisionError) as caught:
        await client.analyze(
            image_bytes=b"image",
            mime_type="image/png",
            mode="auto",
            deadline_monotonic=time.monotonic() + 2,
            abort_signal=Event(),
            dispatch_authority=authority,
            normalized_pixels=20,
        )

    assert caught.value.code == expected_code
    assert calls == 2
    assert caught.value.code != "VISION_RESPONSE_TOO_LARGE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message_patch", "expected_code"),
    [
        ({"role": "user"}, "VISION_SCHEMA_MISMATCH"),
        ({"tool_calls": []}, "VISION_SCHEMA_MISMATCH"),
        ({"refusal": "blocked"}, "VISION_CONTENT_BLOCKED"),
    ],
)
async def test_chat_response_fails_closed_for_wrong_role_tools_and_refusal(
    message_patch: dict[str, object],
    expected_code: str,
) -> None:
    payload = _success_payload()
    choices = payload["choices"]
    assert isinstance(choices, list)
    choice = choices[0]
    assert isinstance(choice, dict)
    message = choice["message"]
    assert isinstance(message, dict)
    message.update(message_patch)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = OpenAICompatibleVisionEvidenceClient(
        _model(),
        transport=httpx.MockTransport(handler),
        transient_gate_key="vision-adapter-test",
    )
    with pytest.raises(OpenAICompatibleVisionError) as caught:
        await client.analyze(
            image_bytes=b"image",
            mime_type="image/png",
            mode="auto",
            deadline_monotonic=time.monotonic() + 2,
            abort_signal=Event(),
        )

    assert caught.value.code == expected_code


def test_real_adapter_requires_https_exact_endpoint_and_credential() -> None:
    model = _model()
    model.base_url = "http://vision.example.test/v1"
    with pytest.raises(OpenAICompatibleVisionError) as caught:
        OpenAICompatibleVisionEvidenceClient(model)
    assert caught.value.code == "VISION_CONFIGURATION_ERROR"


def test_real_adapter_catalog_profile_is_exact_and_credential_bound() -> None:
    assert provider_credential_required("vision_openai_compatible_v1") is True
    assert validate_model_settings(
        {"base_url": "https://vision.example.test/v1"},
        provider_adapter="vision_openai_compatible_v1",
    ) == {"base_url": "https://vision.example.test/v1"}

    for settings in (
        {},
        {"base_url": "http://vision.example.test/v1"},
        {
            "base_url": "https://vision.example.test/v1",
            "max_retries": 7,
        },
    ):
        with pytest.raises(ModelSettingsInvalid):
            validate_model_settings(
                settings,
                provider_adapter="vision_openai_compatible_v1",
            )

    model = _model()
    model.api_key = None
    with pytest.raises(OpenAICompatibleVisionError) as caught:
        OpenAICompatibleVisionEvidenceClient(model)
    assert caught.value.code == "VISION_CONFIGURATION_ERROR"


def test_real_adapter_fails_closed_when_external_content_tracing_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("deerflow.config.is_tracing_enabled", lambda: True)

    with pytest.raises(VisionClientError) as caught:
        build_vision_evidence_client(_model(), "vision.bridge.v1")

    assert caught.value.code == "DATA_POLICY_BLOCKED"
