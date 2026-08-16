"""Conformance tests for model-selected OpenAI Responses vision dispatch."""

from __future__ import annotations

import json
import time
import uuid
from threading import Event

import httpx
import pytest
from pydantic import SecretStr

from deerflow.config.model_config import ModelConfig
from deerflow.vision.client import build_vision_evidence_client
from deerflow.vision.contracts import VisionEvidence, VisionEvidenceItem
from deerflow.vision.openai_compatible import (
    OpenAICompatibleVisionError,
    OpenAIResponsesVisionEvidenceClient,
)
from deerflow.vision.prompt import VISION_SYSTEM_PROMPT_V1

RESPONSES_MODEL_REF = "00000000-0000-4000-8000-000000000301"


def _model() -> ModelConfig:
    model = ModelConfig(
        name=RESPONSES_MODEL_REF,
        display_name="GPT 5.6 Luna",
        description="",
        use="langchain_openai:ChatOpenAI",
        model="gpt-5.6-luna",
        base_url="https://responses.example.test/v1",
        api_key=SecretStr("test-secret-value"),
        use_responses_api=True,
        output_version="responses/v1",
        request_timeout=600.0,
        max_retries=9,
        extra_body={"reasoning": {"effort": "high"}},
        supports_vision=True,
    )
    model._system_model_config_version_id = uuid.uuid4()
    model._system_provider_adapter = "openai"
    return model


def _evidence() -> dict[str, object]:
    return json.loads(
        VisionEvidence(
            ok=True,
            content_type="untrusted_image_evidence",
            schema_version="vision.evidence.v1",
            summary="A blue square is visible.",
            evidence=[
                VisionEvidenceItem(
                    kind="visual",
                    text="One blue square occupies the center.",
                    location="center",
                )
            ],
            uncertainty=[],
            partial=False,
        ).canonical_json()
    )


def _success_payload() -> dict[str, object]:
    return {
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "output": [
            {
                "type": "reasoning",
                "summary": [],
            },
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(_evidence()),
                        "annotations": [],
                    }
                ],
            },
        ],
        "usage": {
            "input_tokens": 73,
            "output_tokens": 19,
            "total_tokens": 92,
        },
    }


@pytest.mark.asyncio
async def test_responses_client_uses_selected_models_protocol_and_fixed_contract() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_success_payload())

    client = OpenAIResponsesVisionEvidenceClient(
        _model(),
        transport=httpx.MockTransport(handler),
        transient_gate_key="vision-responses-test",
    )
    result = await client.analyze(
        image_bytes=b"normalized-image",
        mime_type="image/jpeg",
        mode="chart",
        deadline_monotonic=time.monotonic() + 2,
        abort_signal=Event(),
    )

    assert result.evidence.summary == "A blue square is visible."
    assert result.usage_receipt.input_tokens == 73
    assert result.usage_receipt.output_tokens == 19
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://responses.example.test/v1/responses"
    assert request.headers["authorization"] == "Bearer test-secret-value"
    payload = json.loads(request.content)
    assert set(payload) == {
        "input",
        "instructions",
        "max_output_tokens",
        "model",
        "store",
        "stream",
        "text",
    }
    assert payload["stream"] is False
    assert payload["store"] is False
    assert VISION_SYSTEM_PROMPT_V1 in payload["instructions"]
    assert "extra_body" not in payload
    assert "reasoning" not in payload
    assert "temperature" not in payload
    content = payload["input"][0]["content"]
    assert [part["type"] for part in content] == ["input_text", "input_image"]
    assert content[1]["image_url"].startswith("data:image/jpeg;base64,")
    response_format = payload["text"]["format"]
    assert response_format["type"] == "json_schema"
    assert response_format["strict"] is True
    assert response_format["schema"]["additionalProperties"] is False


@pytest.mark.asyncio
async def test_responses_client_rejects_refusal_and_incomplete_response() -> None:
    payloads = [
        {
            "status": "completed",
            "error": None,
            "incomplete_details": None,
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "refusal", "refusal": "blocked"}],
                }
            ],
            "usage": None,
        },
        {
            "status": "incomplete",
            "error": None,
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [],
            "usage": {"input_tokens": 11, "output_tokens": 4},
        },
    ]

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payloads.pop(0))

    client = OpenAIResponsesVisionEvidenceClient(
        _model(),
        transport=httpx.MockTransport(handler),
        transient_gate_key="vision-responses-test",
    )
    with pytest.raises(OpenAICompatibleVisionError) as refusal:
        await client.analyze(
            image_bytes=b"image",
            mime_type="image/png",
            mode="auto",
            deadline_monotonic=time.monotonic() + 2,
            abort_signal=Event(),
        )
    assert refusal.value.code == "VISION_CONTENT_BLOCKED"

    with pytest.raises(OpenAICompatibleVisionError) as incomplete:
        await client.analyze(
            image_bytes=b"image",
            mime_type="image/png",
            mode="auto",
            deadline_monotonic=time.monotonic() + 2,
            abort_signal=Event(),
        )
    assert incomplete.value.code == "VISION_SCHEMA_MISMATCH"
    assert incomplete.value.usage_receipt is not None
    assert incomplete.value.usage_receipt.input_tokens == 11
    assert incomplete.value.usage_receipt.output_tokens == 4
    assert incomplete.value.usage_receipt.usage_unknown is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message_patch", "expected_code"),
    [
        ({"role": "user"}, "VISION_SCHEMA_MISMATCH"),
        ({"status": "incomplete"}, "VISION_SCHEMA_MISMATCH"),
    ],
)
async def test_responses_message_fails_closed_for_wrong_role_or_incomplete(
    message_patch: dict[str, object],
    expected_code: str,
) -> None:
    payload = _success_payload()
    output = payload["output"]
    assert isinstance(output, list)
    message = output[1]
    assert isinstance(message, dict)
    message.update(message_patch)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = OpenAIResponsesVisionEvidenceClient(
        _model(),
        transport=httpx.MockTransport(handler),
        transient_gate_key="vision-responses-test",
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


@pytest.mark.asyncio
async def test_responses_rejects_tool_output_and_refusal_even_with_valid_text() -> None:
    with_tool = _success_payload()
    tool_output = with_tool["output"]
    assert isinstance(tool_output, list)
    tool_output.insert(
        1,
        {"type": "function_call", "name": "unsafe", "arguments": "{}"},
    )

    with_refusal = _success_payload()
    refusal_output = with_refusal["output"]
    assert isinstance(refusal_output, list)
    refusal_message = refusal_output[1]
    assert isinstance(refusal_message, dict)
    refusal_content = refusal_message["content"]
    assert isinstance(refusal_content, list)
    refusal_content.insert(0, {"type": "refusal", "refusal": "blocked"})
    payloads = [with_tool, with_refusal]

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payloads.pop(0))

    client = OpenAIResponsesVisionEvidenceClient(
        _model(),
        transport=httpx.MockTransport(handler),
        transient_gate_key="vision-responses-test",
    )
    with pytest.raises(OpenAICompatibleVisionError) as tool_call:
        await client.analyze(
            image_bytes=b"image",
            mime_type="image/png",
            mode="auto",
            deadline_monotonic=time.monotonic() + 2,
            abort_signal=Event(),
        )
    assert tool_call.value.code == "VISION_SCHEMA_MISMATCH"

    with pytest.raises(OpenAICompatibleVisionError) as refusal:
        await client.analyze(
            image_bytes=b"image",
            mime_type="image/png",
            mode="auto",
            deadline_monotonic=time.monotonic() + 2,
            abort_signal=Event(),
        )
    assert refusal.value.code == "VISION_CONTENT_BLOCKED"


@pytest.mark.parametrize("provider_adapter", ["openai", "patched_openai"])
def test_client_factory_routes_supported_adapters_to_responses(
    provider_adapter: str,
) -> None:
    model = _model()
    model._system_provider_adapter = provider_adapter

    client = build_vision_evidence_client(model, "vision.bridge.v1")

    assert isinstance(client, OpenAIResponsesVisionEvidenceClient)
