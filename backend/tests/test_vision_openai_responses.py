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


def _model() -> ModelConfig:
    model = ModelConfig(
        name="gpt-5.6-luna",
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
            "usage": None,
        },
    ]

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payloads.pop(0))

    client = OpenAIResponsesVisionEvidenceClient(
        _model(),
        transport=httpx.MockTransport(handler),
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


def test_client_factory_routes_using_selected_models_responses_setting() -> None:
    client = build_vision_evidence_client(_model(), "vision.bridge.v1")

    assert isinstance(client, OpenAIResponsesVisionEvidenceClient)
