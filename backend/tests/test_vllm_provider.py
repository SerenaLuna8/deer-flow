from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_openai import ChatOpenAI

from deerflow.models.vllm_provider import VllmChatModel


def _make_model(*, cumulative_stream_usage: bool = False) -> VllmChatModel:
    return VllmChatModel(
        model="Qwen/QwQ-32B",
        api_key="dummy",
        base_url="http://localhost:8000/v1",
        cumulative_stream_usage=cumulative_stream_usage,
    )


def _stream_chunk(
    *,
    completion_id: str | None,
    prompt_tokens: int,
    completion_tokens: int,
    content: str = "",
    reasoning: str | None = None,
    finish_reason: str | None = None,
) -> dict:
    delta = {"role": "assistant", "content": content}
    if reasoning is not None:
        delta["reasoning"] = reasoning
    chunk = {
        "model": "Qwen/QwQ-32B",
        "choices": [
            {
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
    if completion_id is not None:
        chunk["id"] = completion_id
    return chunk


def _convert_stream_chunk(model: VllmChatModel, chunk: dict):
    return model._convert_chunk_to_generation_chunk(
        chunk,
        AIMessageChunk,
        {},
    )


def _assert_usage(
    message,
    *,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
) -> None:
    assert message.usage_metadata is not None
    assert message.usage_metadata["input_tokens"] == input_tokens
    assert message.usage_metadata["output_tokens"] == output_tokens
    assert message.usage_metadata["total_tokens"] == total_tokens


def test_vllm_provider_restores_reasoning_in_request_payload():
    model = _make_model()
    payload = model._get_request_payload(
        [
            AIMessage(
                content="",
                tool_calls=[{"name": "bash", "args": {"cmd": "pwd"}, "id": "tool-1", "type": "tool_call"}],
                additional_kwargs={"reasoning": "Need to inspect the workspace first."},
            ),
            HumanMessage(content="Continue"),
        ]
    )

    assistant_message = payload["messages"][0]
    assert assistant_message["role"] == "assistant"
    assert assistant_message["reasoning"] == "Need to inspect the workspace first."
    assert assistant_message["tool_calls"][0]["function"]["name"] == "bash"


def test_vllm_provider_normalizes_legacy_thinking_kwarg_to_enable_thinking():
    model = VllmChatModel(
        model="qwen3",
        api_key="dummy",
        base_url="http://localhost:8000/v1",
        extra_body={"chat_template_kwargs": {"thinking": True}},
    )

    payload = model._get_request_payload([HumanMessage(content="Hello")])

    assert payload["extra_body"]["chat_template_kwargs"] == {"enable_thinking": True}


def test_vllm_provider_preserves_explicit_enable_thinking_kwarg():
    model = VllmChatModel(
        model="qwen3",
        api_key="dummy",
        base_url="http://localhost:8000/v1",
        extra_body={"chat_template_kwargs": {"enable_thinking": False, "foo": "bar"}},
    )

    payload = model._get_request_payload([HumanMessage(content="Hello")])

    assert payload["extra_body"]["chat_template_kwargs"] == {
        "enable_thinking": False,
        "foo": "bar",
    }


def test_vllm_provider_preserves_reasoning_in_chat_result():
    model = _make_model()
    result = model._create_chat_result(
        {
            "model": "Qwen/QwQ-32B",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "42",
                        "reasoning": "I compared the two numbers directly.",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )

    message = result.generations[0].message
    assert message.additional_kwargs["reasoning"] == "I compared the two numbers directly."
    assert message.additional_kwargs["reasoning_content"] == "I compared the two numbers directly."


def test_vllm_provider_preserves_reasoning_in_streaming_chunks():
    model = _make_model()
    chunk = model._convert_chunk_to_generation_chunk(
        {
            "model": "Qwen/QwQ-32B",
            "choices": [
                {
                    "delta": {
                        "role": "assistant",
                        "reasoning": "First, call the weather tool.",
                        "content": "Calling tool...",
                    },
                    "finish_reason": None,
                }
            ],
        },
        AIMessageChunk,
        {},
    )

    assert chunk is not None
    assert chunk.message.additional_kwargs["reasoning"] == "First, call the weather tool."
    assert chunk.message.additional_kwargs["reasoning_content"] == "First, call the weather tool."
    assert chunk.message.content == "Calling tool..."


def test_vllm_provider_preserves_empty_reasoning_values_in_streaming_chunks():
    model = _make_model()
    chunk = model._convert_chunk_to_generation_chunk(
        {
            "model": "Qwen/QwQ-32B",
            "choices": [
                {
                    "delta": {
                        "role": "assistant",
                        "reasoning": "",
                        "content": "Still replying...",
                    },
                    "finish_reason": None,
                }
            ],
        },
        AIMessageChunk,
        {},
    )

    assert chunk is not None
    assert "reasoning" in chunk.message.additional_kwargs
    assert chunk.message.additional_kwargs["reasoning"] == ""
    assert "reasoning_content" not in chunk.message.additional_kwargs
    assert chunk.message.content == "Still replying..."


def test_vllm_provider_converts_cumulative_stream_usage_to_deltas():
    model = _make_model(cumulative_stream_usage=True)

    first = _convert_stream_chunk(
        model,
        _stream_chunk(
            completion_id="chatcmpl-1",
            prompt_tokens=10,
            completion_tokens=1,
            content="A",
            reasoning="Inspect the evidence.",
        ),
    )
    second = _convert_stream_chunk(
        model,
        _stream_chunk(
            completion_id="chatcmpl-1",
            prompt_tokens=10,
            completion_tokens=3,
            content="B",
            finish_reason="stop",
        ),
    )
    terminal = _convert_stream_chunk(
        model,
        {
            "id": "chatcmpl-1",
            "model": "Qwen/QwQ-32B",
            "choices": [],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 3,
                "total_tokens": 13,
            },
        },
    )

    assert first is not None
    _assert_usage(
        first.message,
        input_tokens=10,
        output_tokens=1,
        total_tokens=11,
    )
    assert second is not None
    _assert_usage(
        second.message,
        input_tokens=0,
        output_tokens=2,
        total_tokens=2,
    )
    assert terminal is not None
    _assert_usage(
        terminal.message,
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
    )
    combined = first + second + terminal
    _assert_usage(
        combined.message,
        input_tokens=10,
        output_tokens=3,
        total_tokens=13,
    )
    assert combined.message.content == "AB"
    assert combined.message.additional_kwargs["reasoning"] == "Inspect the evidence."
    assert not model._cumulative_usage_by_completion


def test_vllm_provider_leaves_standard_usage_only_terminal_frame_unchanged():
    model = _make_model(cumulative_stream_usage=True)

    terminal = _convert_stream_chunk(
        model,
        {
            "id": "chatcmpl-terminal",
            "model": "Qwen/QwQ-32B",
            "choices": [],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 4,
                "total_tokens": 14,
            },
        },
    )

    assert terminal is not None
    _assert_usage(
        terminal.message,
        input_tokens=10,
        output_tokens=4,
        total_tokens=14,
    )
    assert not model._cumulative_usage_by_completion


def test_vllm_provider_clears_snapshot_on_terminal_frame_without_usage():
    model = _make_model(cumulative_stream_usage=True)

    _convert_stream_chunk(
        model,
        _stream_chunk(
            completion_id="chatcmpl-no-terminal-usage",
            prompt_tokens=10,
            completion_tokens=2,
        ),
    )
    terminal = _convert_stream_chunk(
        model,
        {
            "id": "chatcmpl-no-terminal-usage",
            "model": "Qwen/QwQ-32B",
            "choices": [],
        },
    )

    assert terminal is not None
    assert terminal.message.usage_metadata is None
    assert not model._cumulative_usage_by_completion


def test_vllm_provider_clears_snapshot_on_finish_reason_with_usage():
    model = _make_model(cumulative_stream_usage=True)

    _convert_stream_chunk(
        model,
        _stream_chunk(
            completion_id="chatcmpl-finish-usage",
            prompt_tokens=10,
            completion_tokens=1,
        ),
    )
    terminal = _convert_stream_chunk(
        model,
        _stream_chunk(
            completion_id="chatcmpl-finish-usage",
            prompt_tokens=10,
            completion_tokens=3,
            finish_reason="stop",
        ),
    )

    assert terminal is not None
    _assert_usage(
        terminal.message,
        input_tokens=0,
        output_tokens=2,
        total_tokens=2,
    )
    assert not model._cumulative_usage_by_completion


def test_vllm_provider_clears_snapshot_on_finish_reason_without_usage():
    model = _make_model(cumulative_stream_usage=True)

    _convert_stream_chunk(
        model,
        _stream_chunk(
            completion_id="chatcmpl-finish-no-usage",
            prompt_tokens=10,
            completion_tokens=1,
        ),
    )
    terminal = _convert_stream_chunk(
        model,
        {
            "id": "chatcmpl-finish-no-usage",
            "model": "Qwen/QwQ-32B",
            "choices": [
                {
                    "delta": {
                        "role": "assistant",
                        "content": "",
                    },
                    "finish_reason": "stop",
                }
            ],
        },
    )

    assert terminal is not None
    assert terminal.message.usage_metadata is None
    assert not model._cumulative_usage_by_completion


def test_vllm_provider_deduplicates_usage_after_finish_without_usage():
    model = _make_model(cumulative_stream_usage=True)

    first = _convert_stream_chunk(
        model,
        _stream_chunk(
            completion_id="chatcmpl-finish-then-usage",
            prompt_tokens=10,
            completion_tokens=1,
        ),
    )
    finish = _convert_stream_chunk(
        model,
        {
            "id": "chatcmpl-finish-then-usage",
            "model": "Qwen/QwQ-32B",
            "choices": [
                {
                    "delta": {
                        "role": "assistant",
                        "content": "",
                    },
                    "finish_reason": "stop",
                }
            ],
        },
    )
    terminal = _convert_stream_chunk(
        model,
        {
            "id": "chatcmpl-finish-then-usage",
            "model": "Qwen/QwQ-32B",
            "choices": [],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 3,
                "total_tokens": 13,
            },
        },
    )

    assert first is not None
    _assert_usage(
        first.message,
        input_tokens=10,
        output_tokens=1,
        total_tokens=11,
    )
    assert finish is not None
    assert finish.message.usage_metadata is None
    assert terminal is not None
    _assert_usage(
        terminal.message,
        input_tokens=0,
        output_tokens=2,
        total_tokens=2,
    )


def test_vllm_provider_deduplicates_replayed_usage_only_terminal():
    model = _make_model(cumulative_stream_usage=True)

    first = _convert_stream_chunk(
        model,
        {
            "id": "chatcmpl-terminal-replay",
            "model": "Qwen/QwQ-32B",
            "choices": [],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 3,
                "total_tokens": 13,
            },
        },
    )
    replay = _convert_stream_chunk(
        model,
        {
            "id": "chatcmpl-terminal-replay",
            "model": "Qwen/QwQ-32B",
            "choices": [],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 3,
                "total_tokens": 13,
            },
        },
    )

    assert first is not None
    _assert_usage(
        first.message,
        input_tokens=10,
        output_tokens=3,
        total_tokens=13,
    )
    assert replay is not None
    _assert_usage(
        replay.message,
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
    )


def test_vllm_provider_does_not_advance_usage_for_discarded_null_delta():
    model = _make_model(cumulative_stream_usage=True)

    first = _convert_stream_chunk(
        model,
        _stream_chunk(
            completion_id="chatcmpl-null-delta",
            prompt_tokens=10,
            completion_tokens=1,
        ),
    )
    discarded = _convert_stream_chunk(
        model,
        {
            "id": "chatcmpl-null-delta",
            "model": "Qwen/QwQ-32B",
            "choices": [
                {
                    "delta": None,
                    "finish_reason": None,
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "total_tokens": 12,
            },
        },
    )
    next_chunk = _convert_stream_chunk(
        model,
        _stream_chunk(
            completion_id="chatcmpl-null-delta",
            prompt_tokens=10,
            completion_tokens=3,
        ),
    )

    assert first is not None
    assert discarded is None
    assert next_chunk is not None
    _assert_usage(
        next_chunk.message,
        input_tokens=0,
        output_tokens=2,
        total_tokens=2,
    )


def test_vllm_provider_tracks_concurrent_streams_by_completion_id():
    model = _make_model(cumulative_stream_usage=True)

    stream_a_first = _convert_stream_chunk(
        model,
        _stream_chunk(
            completion_id="chatcmpl-a",
            prompt_tokens=10,
            completion_tokens=1,
        ),
    )
    stream_b_first = _convert_stream_chunk(
        model,
        _stream_chunk(
            completion_id="chatcmpl-b",
            prompt_tokens=20,
            completion_tokens=2,
        ),
    )
    stream_a_second = _convert_stream_chunk(
        model,
        _stream_chunk(
            completion_id="chatcmpl-a",
            prompt_tokens=10,
            completion_tokens=5,
        ),
    )

    assert stream_a_first is not None
    assert stream_a_first.message.usage_metadata["total_tokens"] == 11
    assert stream_b_first is not None
    assert stream_b_first.message.usage_metadata["total_tokens"] == 22
    assert stream_a_second is not None
    _assert_usage(
        stream_a_second.message,
        input_tokens=0,
        output_tokens=4,
        total_tokens=4,
    )


def test_vllm_provider_preserves_reasoning_when_converting_cumulative_usage():
    model = _make_model(cumulative_stream_usage=True)

    chunk = _convert_stream_chunk(
        model,
        _stream_chunk(
            completion_id="chatcmpl-reasoning",
            prompt_tokens=12,
            completion_tokens=2,
            content="Answer",
            reasoning="Check the evidence first.",
        ),
    )

    assert chunk is not None
    assert chunk.message.additional_kwargs["reasoning"] == "Check the evidence first."
    assert chunk.message.additional_kwargs["reasoning_content"] == "Check the evidence first."
    assert chunk.message.content == "Answer"
    _assert_usage(
        chunk.message,
        input_tokens=12,
        output_tokens=2,
        total_tokens=14,
    )


def test_vllm_provider_leaves_cumulative_usage_unchanged_by_default():
    model = _make_model()

    _convert_stream_chunk(
        model,
        _stream_chunk(
            completion_id="chatcmpl-default",
            prompt_tokens=10,
            completion_tokens=1,
        ),
    )
    second = _convert_stream_chunk(
        model,
        _stream_chunk(
            completion_id="chatcmpl-default",
            prompt_tokens=10,
            completion_tokens=3,
        ),
    )

    assert second is not None
    _assert_usage(
        second.message,
        input_tokens=10,
        output_tokens=3,
        total_tokens=13,
    )
    assert model.cumulative_stream_usage is False
    assert not model._cumulative_usage_by_completion


def test_vllm_provider_leaves_usage_unchanged_without_stable_completion_id():
    model = _make_model(cumulative_stream_usage=True)

    _convert_stream_chunk(
        model,
        _stream_chunk(
            completion_id=None,
            prompt_tokens=10,
            completion_tokens=1,
        ),
    )
    second = _convert_stream_chunk(
        model,
        _stream_chunk(
            completion_id=None,
            prompt_tokens=10,
            completion_tokens=3,
        ),
    )

    assert second is not None
    _assert_usage(
        second.message,
        input_tokens=10,
        output_tokens=3,
        total_tokens=13,
    )
    assert not model._cumulative_usage_by_completion


def test_vllm_provider_tracker_has_a_hard_capacity(
    monkeypatch,
):
    monkeypatch.setattr(
        "deerflow.models.vllm_provider._CUMULATIVE_USAGE_TRACKER_CAPACITY",
        2,
    )
    now = [0.0]
    monkeypatch.setattr(
        "deerflow.models.vllm_provider.time.monotonic",
        lambda: now[0],
    )
    model = _make_model(cumulative_stream_usage=True)

    for index in range(3):
        _convert_stream_chunk(
            model,
            _stream_chunk(
                completion_id=f"chatcmpl-{index}",
                prompt_tokens=10,
                completion_tokens=1,
            ),
        )

    assert list(model._cumulative_usage_by_completion) == [
        "chatcmpl-1",
        "chatcmpl-2",
    ]
    assert len(model._cumulative_usage_by_completion) == 2

    now[0] = 1.0
    next_chunk = _convert_stream_chunk(
        model,
        _stream_chunk(
            completion_id="chatcmpl-0",
            prompt_tokens=10,
            completion_tokens=5,
        ),
    )

    assert next_chunk is not None
    _assert_usage(
        next_chunk.message,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
    )
    assert len(model._cumulative_usage_by_completion) == 2


def test_vllm_provider_evicts_idle_streams_before_active_entries(
    monkeypatch,
):
    monkeypatch.setattr(
        "deerflow.models.vllm_provider._CUMULATIVE_USAGE_TRACKER_CAPACITY",
        2,
    )
    monkeypatch.setattr(
        "deerflow.models.vllm_provider._CUMULATIVE_USAGE_TRACKER_IDLE_SECONDS",
        10,
    )
    now = [0.0]
    monkeypatch.setattr(
        "deerflow.models.vllm_provider.time.monotonic",
        lambda: now[0],
    )
    model = _make_model(cumulative_stream_usage=True)

    for index in range(3):
        _convert_stream_chunk(
            model,
            _stream_chunk(
                completion_id=f"chatcmpl-{index}",
                prompt_tokens=10,
                completion_tokens=1,
            ),
        )

    now[0] = 11.0
    _convert_stream_chunk(
        model,
        _stream_chunk(
            completion_id="chatcmpl-3",
            prompt_tokens=10,
            completion_tokens=1,
        ),
    )

    assert list(model._cumulative_usage_by_completion) == [
        "chatcmpl-2",
        "chatcmpl-3",
    ]


def test_vllm_provider_terminal_deduplication_tracker_has_a_hard_capacity(
    monkeypatch,
):
    monkeypatch.setattr(
        "deerflow.models.vllm_provider._CUMULATIVE_USAGE_TRACKER_CAPACITY",
        2,
    )
    model = _make_model(cumulative_stream_usage=True)

    for index in range(3):
        _convert_stream_chunk(
            model,
            _stream_chunk(
                completion_id=f"chatcmpl-terminal-{index}",
                prompt_tokens=10,
                completion_tokens=1,
            ),
        )
        _convert_stream_chunk(
            model,
            _stream_chunk(
                completion_id=f"chatcmpl-terminal-{index}",
                prompt_tokens=10,
                completion_tokens=2,
                finish_reason="stop",
            ),
        )

    assert not model._cumulative_usage_by_completion
    assert list(model._terminal_usage_by_completion) == [
        "chatcmpl-terminal-1",
        "chatcmpl-terminal-2",
    ]


def test_vllm_provider_releases_stream_usage_after_parent_error(monkeypatch):
    model = _make_model(cumulative_stream_usage=True)

    def _failing_parent_stream(self, *args, **kwargs):
        del args, kwargs
        yield _convert_stream_chunk(
            self,
            _stream_chunk(
                completion_id="chatcmpl-error",
                prompt_tokens=10,
                completion_tokens=1,
            ),
        )
        raise RuntimeError("provider stream failed")

    monkeypatch.setattr(ChatOpenAI, "_stream", _failing_parent_stream)

    stream = model._stream([HumanMessage(content="Hello")])
    assert next(stream) is not None
    assert model._cumulative_usage_by_completion
    with pytest.raises(RuntimeError, match="provider stream failed"):
        next(stream)

    assert not model._cumulative_usage_by_completion
    assert not model._terminal_usage_by_completion


@pytest.mark.asyncio
async def test_vllm_provider_releases_stream_usage_after_cancellation(
    monkeypatch,
):
    model = _make_model(cumulative_stream_usage=True)
    blocked = asyncio.Event()

    async def _blocking_parent_stream(self, *args, **kwargs):
        del args, kwargs
        yield _convert_stream_chunk(
            self,
            _stream_chunk(
                completion_id="chatcmpl-cancel",
                prompt_tokens=10,
                completion_tokens=1,
            ),
        )
        await blocked.wait()

    monkeypatch.setattr(ChatOpenAI, "_astream", _blocking_parent_stream)

    stream = model._astream([HumanMessage(content="Hello")])
    assert await anext(stream) is not None
    assert model._cumulative_usage_by_completion

    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    assert not model._cumulative_usage_by_completion
    assert not model._terminal_usage_by_completion


@pytest.mark.asyncio
async def test_vllm_provider_isolates_concurrent_reused_completion_ids(
    monkeypatch,
):
    model = _make_model(cumulative_stream_usage=True)
    started = {
        "stream-a": asyncio.Event(),
        "stream-b": asyncio.Event(),
    }
    release = asyncio.Event()

    async def _parent_stream(self, messages, *args, **kwargs):
        del args, kwargs
        label = messages[0].content
        prompt_tokens = 10 if label == "stream-a" else 20
        yield _convert_stream_chunk(
            self,
            _stream_chunk(
                completion_id="provider-reused-id",
                prompt_tokens=prompt_tokens,
                completion_tokens=1,
            ),
        )
        started[label].set()
        await release.wait()
        yield _convert_stream_chunk(
            self,
            _stream_chunk(
                completion_id="provider-reused-id",
                prompt_tokens=prompt_tokens,
                completion_tokens=3,
                finish_reason="stop",
            ),
        )

    monkeypatch.setattr(ChatOpenAI, "_astream", _parent_stream)

    async def _consume(label: str):
        return [
            chunk
            async for chunk in model._astream(
                [HumanMessage(content=label)],
            )
        ]

    stream_a = asyncio.create_task(_consume("stream-a"))
    stream_b = asyncio.create_task(_consume("stream-b"))
    await asyncio.gather(
        started["stream-a"].wait(),
        started["stream-b"].wait(),
    )
    assert len(model._cumulative_usage_by_completion) == 2

    release.set()
    chunks_a, chunks_b = await asyncio.gather(stream_a, stream_b)

    _assert_usage(
        chunks_a[1].message,
        input_tokens=0,
        output_tokens=2,
        total_tokens=2,
    )
    _assert_usage(
        chunks_b[1].message,
        input_tokens=0,
        output_tokens=2,
        total_tokens=2,
    )
    assert not model._cumulative_usage_by_completion
    assert not model._terminal_usage_by_completion
