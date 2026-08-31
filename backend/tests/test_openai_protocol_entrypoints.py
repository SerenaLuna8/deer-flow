"""M9 OpenAI dual protocol entrypoints: fixed wire per adapter identity.

``openai`` speaks Chat Completions and ``openai_responses`` speaks Responses;
both reuse the native ``langchain_openai:ChatOpenAI``. The protocol switch is
pinned by materialization — never authored, never left for SDK auto-selection.
Mock HTTP transports record the actual URL path and body for each entry, and
the Responses reasoning-summary shape must round-trip into the shared
extraction helpers used by RunJournal, oneshot and Builder activity.
"""

from __future__ import annotations

import json

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from app.system_settings.validation import (
    ModelSettingsInvalid,
    materialize_effective_model_settings,
    validate_model_settings,
)
from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig
from deerflow.models.factory import create_chat_model
from deerflow.runtime.journal import RunJournal
from deerflow.utils.messages import reasoning_block_text
from deerflow.utils.oneshot_llm import _structured_reasoning

_LOOKUP_TOOL = {
    "type": "function",
    "function": {
        "name": "lookup",
        "description": "Look up a value.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
}


def _chat_completion_body(text: str) -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-5.2",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text, "refusal": None},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
    }


def _responses_body(output: list[dict]) -> dict:
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "max_output_tokens": None,
        "model": "gpt-5.2",
        "output": output,
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
            "input_tokens": 10,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 3,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 13,
        },
        "user": None,
        "metadata": {},
    }


def _output_message(text: str) -> dict:
    return {
        "id": "msg_1",
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [
            {"type": "output_text", "annotations": [], "logprobs": [], "text": text},
        ],
    }


class _Recorder:
    """Record every request path and JSON body a model actually sends."""

    def __init__(self, respond) -> None:
        self.requests: list[tuple[str, dict]] = []
        self._respond = respond

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append((request.url.path, body))
        return self._respond(request, body)


def _build_model(provider_adapter: str, recorder: _Recorder, **overrides) -> ChatOpenAI:
    """Construct ChatOpenAI exactly as materialization pins the protocol."""
    settings = materialize_effective_model_settings({}, provider_adapter=provider_adapter)
    protocol: dict[str, object] = {"use_responses_api": settings["use_responses_api"]}
    if "output_version" in settings:
        protocol["output_version"] = settings["output_version"]
    transport = httpx.MockTransport(recorder)
    return ChatOpenAI(
        api_key="test-only",
        # A model name in the family the SDK may auto-route to Responses; the
        # pinned switch must win over any name-based inference.
        model="gpt-5.2",
        max_retries=0,
        base_url="https://provider.test/v1",
        http_client=httpx.Client(transport=transport),
        http_async_client=httpx.AsyncClient(transport=transport),
        **protocol,
        **overrides,
    )


def test_materialization_pins_definite_protocol_for_both_entrypoints() -> None:
    chat = materialize_effective_model_settings({}, provider_adapter="openai")
    responses = materialize_effective_model_settings({}, provider_adapter="openai_responses")

    assert chat["use_responses_api"] is False
    assert "output_version" not in chat
    assert responses["use_responses_api"] is True
    assert responses["output_version"] == "responses/v1"


def _responses_model_via_materialization(
    recorder: _Recorder,
    *,
    authored_settings: dict[str, object],
    supports_reasoning_effort: bool = True,
    **factory_kwargs: object,
):
    """Build a model through the real authoring -> materialization -> factory chain."""

    validated = validate_model_settings(
        authored_settings,
        provider_adapter="openai_responses",
    )
    effective = materialize_effective_model_settings(
        validated,
        provider_adapter="openai_responses",
    )
    model = ModelConfig(
        name="responses-summary-model",
        display_name="Responses summary model",
        description="",
        use="langchain_openai:ChatOpenAI",
        model="gpt-5.2",
        max_input_tokens=64_000,
        api_key=SecretStr("test-only"),
        supports_reasoning_effort=supports_reasoning_effort,
        **effective,
    )
    model._system_provider_adapter = "openai_responses"
    app_config = AppConfig(
        models=[model],
        sandbox={"use": "deerflow.sandbox.local:LocalSandboxProvider"},
    )
    transport = httpx.MockTransport(recorder)
    return create_chat_model(
        name=model.name,
        app_config=app_config,
        attach_tracing=False,
        http_client=httpx.Client(transport=transport),
        http_async_client=httpx.AsyncClient(transport=transport),
        **factory_kwargs,
    )


def test_responses_reasoning_summary_folds_into_the_reasoning_object() -> None:
    """An authored summary must reach the wire inside ``reasoning`` only.

    The Responses endpoint returns reasoning summaries only when the request
    carries ``reasoning.summary``; the SDK stops rewriting the flat
    ``reasoning_effort`` spelling once ``reasoning`` is present, so the factory
    must fold both into the one object and drop the flat keys.
    """

    recorder = _Recorder(lambda _req, _body: httpx.Response(200, json=_responses_body([_output_message("ok")])))
    model = _responses_model_via_materialization(
        recorder,
        authored_settings={"reasoning_summary": "auto", "reasoning_effort": "high"},
    )

    payload = model._get_request_payload([HumanMessage(content="question")])
    assert payload["reasoning"] == {"effort": "high", "summary": "auto"}
    assert "reasoning_effort" not in payload
    assert "reasoning_summary" not in payload

    result = model.invoke([HumanMessage(content="question")])
    assert result.text == "ok"
    ((path, body),) = recorder.requests
    assert path == "/v1/responses"
    assert body["reasoning"] == {"effort": "high", "summary": "auto"}
    assert "reasoning_effort" not in body
    assert "reasoning_summary" not in body


def test_responses_reasoning_summary_without_effort_sends_summary_only() -> None:
    recorder = _Recorder(lambda _req, _body: httpx.Response(200, json=_responses_body([_output_message("ok")])))
    model = _responses_model_via_materialization(
        recorder,
        authored_settings={"reasoning_summary": "detailed"},
        supports_reasoning_effort=False,
    )

    payload = model._get_request_payload([HumanMessage(content="question")])
    assert payload["reasoning"] == {"summary": "detailed"}
    assert "reasoning_effort" not in payload
    assert "reasoning_summary" not in payload


def test_reasoning_summary_stays_out_of_other_entrypoints_and_rejects_bad_values() -> None:
    with pytest.raises(ModelSettingsInvalid):
        validate_model_settings({"reasoning_summary": "auto"}, provider_adapter="openai")
    with pytest.raises(ModelSettingsInvalid):
        validate_model_settings({"reasoning_summary": "auto"}, provider_adapter="vllm")
    with pytest.raises(ModelSettingsInvalid):
        validate_model_settings({"reasoning_summary": "always"}, provider_adapter="openai_responses")


def test_chat_entrypoint_posts_only_chat_completions_sync() -> None:
    recorder = _Recorder(lambda _req, _body: httpx.Response(200, json=_chat_completion_body("chat answer")))
    model = _build_model("openai", recorder)

    result = model.invoke([HumanMessage(content="question")])

    assert result.text == "chat answer"
    ((path, body),) = recorder.requests
    assert path == "/v1/chat/completions"
    assert "messages" in body
    assert "input" not in body


@pytest.mark.anyio
async def test_chat_entrypoint_posts_only_chat_completions_async() -> None:
    recorder = _Recorder(lambda _req, _body: httpx.Response(200, json=_chat_completion_body("chat answer")))
    model = _build_model("openai", recorder)

    result = await model.ainvoke([HumanMessage(content="question")])

    assert result.text == "chat answer"
    ((path, body),) = recorder.requests
    assert path == "/v1/chat/completions"
    assert "messages" in body
    assert "input" not in body


def test_responses_entrypoint_posts_only_responses_sync() -> None:
    recorder = _Recorder(lambda _req, _body: httpx.Response(200, json=_responses_body([_output_message("responses answer")])))
    model = _build_model("openai_responses", recorder)

    result = model.invoke([HumanMessage(content="question")])

    assert result.text == "responses answer"
    ((path, body),) = recorder.requests
    assert path == "/v1/responses"
    assert "input" in body
    assert "messages" not in body


@pytest.mark.anyio
async def test_responses_entrypoint_posts_only_responses_async() -> None:
    recorder = _Recorder(lambda _req, _body: httpx.Response(200, json=_responses_body([_output_message("responses answer")])))
    model = _build_model("openai_responses", recorder)

    result = await model.ainvoke([HumanMessage(content="question")])

    assert result.text == "responses answer"
    ((path, body),) = recorder.requests
    assert path == "/v1/responses"
    assert "input" in body
    assert "messages" not in body


def test_chat_entrypoint_streaming_stays_on_chat_protocol() -> None:
    chunks = [
        {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "gpt-5.2",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "streamed answer"},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "gpt-5.2",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
    ]
    sse = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    recorder = _Recorder(
        lambda _req, _body: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse.encode(),
        )
    )
    model = _build_model("openai", recorder)

    merged = None
    for chunk in model.stream([HumanMessage(content="question")]):
        merged = chunk if merged is None else merged + chunk

    assert merged is not None
    assert merged.text == "streamed answer"
    ((path, body),) = recorder.requests
    assert path == "/v1/chat/completions"
    assert body["stream"] is True
    assert "messages" in body
    assert "input" not in body


def test_responses_entrypoint_streaming_stays_on_responses_protocol() -> None:
    completed = _responses_body([_output_message("streamed answer")])
    in_progress = dict(completed, status="in_progress", output=[], usage=None)
    events = [
        ("response.created", {"type": "response.created", "response": in_progress, "sequence_number": 0}),
        (
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "id": "msg_1",
                    "type": "message",
                    "status": "in_progress",
                    "role": "assistant",
                    "content": [],
                },
                "sequence_number": 1,
            },
        ),
        (
            "response.output_text.delta",
            {
                "type": "response.output_text.delta",
                "item_id": "msg_1",
                "output_index": 0,
                "content_index": 0,
                "delta": "streamed answer",
                "logprobs": [],
                "sequence_number": 2,
            },
        ),
        (
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": _output_message("streamed answer"),
                "sequence_number": 3,
            },
        ),
        ("response.completed", {"type": "response.completed", "response": completed, "sequence_number": 4}),
    ]
    sse = "".join(f"event: {name}\ndata: {json.dumps(payload)}\n\n" for name, payload in events)
    recorder = _Recorder(
        lambda _req, _body: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse.encode(),
        )
    )
    model = _build_model("openai_responses", recorder)

    merged = None
    for chunk in model.stream([HumanMessage(content="question")]):
        merged = chunk if merged is None else merged + chunk

    assert merged is not None
    assert merged.text == "streamed answer"
    ((path, body),) = recorder.requests
    assert path == "/v1/responses"
    assert body["stream"] is True
    assert "input" in body
    assert "messages" not in body


def test_chat_entrypoint_tool_round_trip_uses_chat_shapes() -> None:
    recorder = _Recorder(lambda _req, _body: httpx.Response(200, json=_chat_completion_body("done")))
    model = _build_model("openai", recorder)
    messages = [
        HumanMessage(content="orchestrate"),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "lookup", "args": {"query": "one"}, "id": "call-1", "type": "tool_call"},
            ],
        ),
        ToolMessage(content="result-one", tool_call_id="call-1"),
    ]

    model.invoke(messages, tools=[_LOOKUP_TOOL])

    ((path, body),) = recorder.requests
    assert path == "/v1/chat/completions"
    assert body["tools"] == [_LOOKUP_TOOL]
    roles = [message["role"] for message in body["messages"]]
    assert roles == ["user", "assistant", "tool"]
    assistant = body["messages"][1]
    assert assistant["tool_calls"][0]["type"] == "function"
    assert assistant["tool_calls"][0]["id"] == "call-1"
    assert body["messages"][2]["tool_call_id"] == "call-1"


def test_responses_entrypoint_tool_round_trip_uses_responses_items() -> None:
    recorder = _Recorder(lambda _req, _body: httpx.Response(200, json=_responses_body([_output_message("done")])))
    model = _build_model("openai_responses", recorder)
    messages = [
        HumanMessage(content="orchestrate"),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "lookup", "args": {"query": "one"}, "id": "call-1", "type": "tool_call"},
            ],
        ),
        ToolMessage(content="result-one", tool_call_id="call-1"),
    ]

    model.invoke(messages, tools=[_LOOKUP_TOOL])

    ((path, body),) = recorder.requests
    assert path == "/v1/responses"
    # Responses tools are flat (no nested "function" object).
    (tool,) = body["tools"]
    assert tool["type"] == "function"
    assert tool["name"] == "lookup"
    item_types = [item.get("type") for item in body["input"]]
    assert "function_call" in item_types
    assert "function_call_output" in item_types
    function_call = next(item for item in body["input"] if item.get("type") == "function_call")
    call_output = next(item for item in body["input"] if item.get("type") == "function_call_output")
    assert function_call["call_id"] == "call-1"
    assert call_output["call_id"] == "call-1"
    assert call_output["output"] == "result-one"


def test_responses_reasoning_summary_round_trips_into_shared_extractors() -> None:
    # A settled summary list carries one complete paragraph per entry, so the
    # extractors must keep a paragraph break between them.
    reasoning_item = {
        "id": "rs_1",
        "type": "reasoning",
        "summary": [
            {"type": "summary_text", "text": "planned the answer"},
            {"type": "summary_text", "text": "then verified it"},
        ],
    }
    recorder = _Recorder(
        lambda _req, _body: httpx.Response(
            200,
            json=_responses_body([reasoning_item, _output_message("final answer")]),
        )
    )
    model = _build_model("openai_responses", recorder)

    result = model.invoke([HumanMessage(content="question")])

    assert result.text == "final answer"
    reasoning_blocks = [block for block in result.content if isinstance(block, dict) and block.get("type") == "reasoning"]
    assert reasoning_blocks, "Responses reasoning item must surface as a content block"
    extracted = "".join(reasoning_block_text(block) for block in reasoning_blocks)
    assert extracted == "planned the answer\n\nthen verified it"

    # The shared consumers must recognize the same shape without mutation.
    assert RunJournal._message_has_structured_reasoning(result) is True
    assert _structured_reasoning(result) == extracted


def test_reasoning_block_text_covers_direct_and_summary_shapes() -> None:
    assert reasoning_block_text({"type": "thinking", "thinking": "direct text"}) == "direct text"
    assert reasoning_block_text({"type": "reasoning", "reasoning": "direct text"}) == "direct text"
    # Settled summary entries are complete paragraphs: keep a paragraph break
    # between them instead of gluing the last word of one to the next.
    assert (
        reasoning_block_text(
            {
                "type": "reasoning",
                "summary": [
                    {"type": "summary_text", "text": "part one"},
                    {"type": "other", "text": "ignored"},
                    {"type": "summary_text", "text": "part two"},
                ],
            }
        )
        == "part one\n\npart two"
    )
    # No summary means nothing to show — never fabricate reasoning.
    assert reasoning_block_text({"type": "reasoning", "summary": []}) == ""
    assert reasoning_block_text({"type": "reasoning", "encrypted_content": "opaque"}) == ""
    assert reasoning_block_text({"type": "text", "text": "visible"}) == ""
