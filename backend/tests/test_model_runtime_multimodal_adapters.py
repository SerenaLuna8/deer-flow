"""Existing Provider adapters serialize the shared multimodal message."""

from __future__ import annotations

import json

import httpx
import pytest
from anthropic import AsyncAnthropic
from langchain_anthropic import ChatAnthropic
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI

from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig
from deerflow.config.sandbox_config import SandboxConfig
from deerflow.models.patched_deepseek import PatchedChatDeepSeek
from deerflow.models.patched_openai import PatchedChatOpenAI
from deerflow.models.runtime import ModelRuntime, ModelRuntimeProfile
from deerflow.models.vllm_provider import VllmChatModel
from deerflow.tools.builtins.inspect_image_tool import (
    _analysis_text,
    _inspect_image_messages,
)
from deerflow.vision.contracts import VisionUsageReceipt


@pytest.mark.parametrize(
    ("model", "payload_kind"),
    [
        (
            ChatOpenAI(api_key="test-only", model="chat-model", max_retries=0),
            "openai_chat",
        ),
        (
            ChatOpenAI(
                api_key="test-only",
                model="responses-model",
                max_retries=0,
                use_responses_api=True,
            ),
            "openai_responses",
        ),
        (
            ChatAnthropic(
                api_key="test-only",
                model="anthropic-model",
                max_retries=0,
            ),
            "anthropic_messages",
        ),
        (
            ChatDeepSeek(api_key="test-only", model="deepseek-model", max_retries=0),
            "openai_chat",
        ),
        (
            PatchedChatOpenAI(
                api_key="test-only",
                model="patched-openai-model",
                max_retries=0,
            ),
            "openai_chat",
        ),
        (
            PatchedChatDeepSeek(
                api_key="test-only",
                model="patched-deepseek-model",
                max_retries=0,
            ),
            "openai_chat",
        ),
        (
            VllmChatModel(
                api_key="test-only",
                base_url="https://example.invalid/v1",
                model="vllm-model",
                max_retries=0,
            ),
            "openai_chat",
        ),
    ],
)
def test_existing_adapter_serializes_provider_neutral_image_block(
    model: object,
    payload_kind: str,
) -> None:
    messages = _inspect_image_messages(
        image_bytes=b"normalized-image",
        mime_type="image/jpeg",
        mode="describe",
        analysis_goal="Analyze the visible layout.",
    )

    payload = model._get_request_payload(messages)  # type: ignore[attr-defined]

    if payload_kind == "openai_chat":
        user = payload["messages"][1]
        assert "Analyze the visible layout." in str(user["content"][1])
        assert user["content"][2] == {
            "type": "image_url",
            "image_url": {
                "url": "data:image/jpeg;base64,bm9ybWFsaXplZC1pbWFnZQ==",
            },
        }
        assert "input" not in payload
    elif payload_kind == "openai_responses":
        user = payload["input"][1]
        assert "Analyze the visible layout." in str(user["content"][1])
        assert user["content"][2] == {
            "type": "input_image",
            "image_url": "data:image/jpeg;base64,bm9ybWFsaXplZC1pbWFnZQ==",
        }
        assert "messages" not in payload
    else:
        assert payload["system"]
        user = payload["messages"][0]
        assert "Analyze the visible layout." in str(user["content"][1])
        assert user["content"][2] == {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": "bm9ybWFsaXplZC1pbWFnZQ==",
            },
        }


@pytest.mark.parametrize(
    ("use", "extra"),
    [
        ("langchain_openai:ChatOpenAI", {}),
        ("langchain_anthropic:ChatAnthropic", {}),
        ("langchain_deepseek:ChatDeepSeek", {}),
        ("deerflow.models.patched_openai:PatchedChatOpenAI", {}),
        ("deerflow.models.patched_deepseek:PatchedChatDeepSeek", {}),
        (
            "deerflow.models.vllm_provider:VllmChatModel",
            {"base_url": "https://example.invalid/v1"},
        ),
    ],
)
def test_sensitive_profile_builds_every_authorable_provider_adapter(
    use: str,
    extra: dict[str, object],
) -> None:
    model = ModelConfig(
        name="visual-model",
        display_name="Visual model",
        description="",
        use=use,
        model="provider-model",
        max_input_tokens=64_000,
        api_key="test-only",
        supports_vision=True,
        max_retries=9,
        **extra,
    )
    app_config = AppConfig(
        models=[model],
        sandbox=SandboxConfig(
            use="deerflow.sandbox.local:LocalSandboxProvider",
        ),
    )

    built = ModelRuntime(app_config=app_config).build_chat_model(
        profile=ModelRuntimeProfile.SENSITIVE_MULTIMODAL,
        model_name=model.name,
    )

    assert built.max_retries == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("provider_kind", "response_text", "input_tokens", "output_tokens"),
    [
        ("openai_chat", "chat round trip", 11, 3),
        ("openai_responses", "responses round trip", 12, 4),
        ("anthropic_messages", "anthropic round trip", 13, 5),
    ],
)
async def test_sensitive_runtime_round_trips_existing_adapter_response(
    provider_kind: str,
    response_text: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["path"] = request.url.path
        observed["request"] = json.loads(request.content)
        if provider_kind == "openai_chat":
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-round-trip",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "visual-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": response_text,
                                "refusal": None,
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": input_tokens,
                        "completion_tokens": output_tokens,
                        "total_tokens": input_tokens + output_tokens,
                    },
                },
            )
        if provider_kind == "openai_responses":
            return httpx.Response(
                200,
                json={
                    "id": "resp_round_trip",
                    "object": "response",
                    "created_at": 1,
                    "status": "completed",
                    "error": None,
                    "incomplete_details": None,
                    "instructions": None,
                    "max_output_tokens": None,
                    "model": "visual-model",
                    "output": [
                        {
                            "id": "msg_round_trip",
                            "type": "message",
                            "status": "completed",
                            "content": [
                                {
                                    "type": "output_text",
                                    "annotations": [],
                                    "logprobs": [],
                                    "text": response_text,
                                }
                            ],
                            "role": "assistant",
                        }
                    ],
                    "parallel_tool_calls": True,
                    "previous_response_id": None,
                    "reasoning": {"effort": None, "summary": None},
                    "store": True,
                    "temperature": 1.0,
                    "text": {"format": {"type": "text"}},
                    "tool_choice": "auto",
                    "tools": [],
                    "top_p": 1.0,
                    "truncation": "disabled",
                    "usage": {
                        "input_tokens": input_tokens,
                        "input_tokens_details": {"cached_tokens": 0},
                        "output_tokens": output_tokens,
                        "output_tokens_details": {"reasoning_tokens": 0},
                        "total_tokens": input_tokens + output_tokens,
                    },
                    "user": None,
                    "metadata": {},
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "msg_round_trip",
                "type": "message",
                "role": "assistant",
                "model": "visual-model",
                "content": [{"type": "text", "text": response_text}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                },
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
    ) as http_client:
        if provider_kind == "anthropic_messages":
            model = ChatAnthropic(
                api_key="test-only",
                base_url="https://provider.test",
                model="visual-model",
                max_retries=0,
            )
            model.__dict__["_async_client"] = AsyncAnthropic(
                api_key="test-only",
                base_url="https://provider.test",
                max_retries=0,
                http_client=http_client,
            )
        else:
            model = ChatOpenAI(
                api_key="test-only",
                base_url="https://provider.test/v1",
                model="visual-model",
                max_retries=0,
                http_async_client=http_client,
                use_responses_api=provider_kind == "openai_responses",
            )
        runtime = ModelRuntime(
            app_config=AppConfig(
                models=[],
                sandbox=SandboxConfig(
                    use="deerflow.sandbox.local:LocalSandboxProvider",
                ),
            ),
            model_factory=lambda **_kwargs: model,
        )
        response = await runtime.ainvoke(
            _inspect_image_messages(
                image_bytes=b"normalized-image",
                mime_type="image/jpeg",
                mode="describe",
                analysis_goal="Analyze the visible layout.",
            ),
            profile=ModelRuntimeProfile.SENSITIVE_MULTIMODAL,
            model_name="visual-model",
        )

    text, receipt = _analysis_text(response)
    assert text == response_text
    terminal_key, terminal_value = {
        "openai_chat": ("finish_reason", "stop"),
        "openai_responses": ("status", "completed"),
        "anthropic_messages": ("stop_reason", "end_turn"),
    }[provider_kind]
    assert response.response_metadata[terminal_key] == terminal_value
    assert receipt == VisionUsageReceipt(
        call_count=1,
        request_dispatched=True,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        usage_unknown=False,
    )
    expected_path = {
        "openai_chat": "/v1/chat/completions",
        "openai_responses": "/v1/responses",
        "anthropic_messages": "/v1/messages",
    }[provider_kind]
    assert observed["path"] == expected_path
