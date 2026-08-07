"""U2 Phase 1: root text-delta micro-batching.

Pins the three acceptance properties of ``_TextDeltaCoalescer``:

- **micro-batch boundaries** — window expiry, 4 KiB cap, message-identity
  switch, non-text frames (flush first, pass through in order), provider
  finish markers, and the leading edge that keeps slow streams per-token;
- **byte equivalence** — accumulating the coalesced frames with the same
  ``+`` operator SDK clients use yields exactly the text (and DeepSeek
  ``reasoning_content``) of the original per-token stream;
- **toggle fallback** — with the coalescer disabled the publish path stays
  frame-for-frame identical to the previous behavior.
"""

import operator
from functools import reduce
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessageChunk, ToolMessage

import deerflow.runtime.runs.worker as worker_module
from deerflow.config.worker_config import DEFAULT_TEXT_DELTA_FLUSH_MS, WorkerStreamConfig
from deerflow.runtime.runs.worker import (
    _publish_stream_item,
    _TextDeltaCoalescer,
)

_WINDOW = 0.075


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(worker_module, "time", SimpleNamespace(monotonic=fake.monotonic))
    return fake


def _delta(text: str, *, message_id: str = "m1", **kwargs: Any) -> tuple[AIMessageChunk, dict[str, Any]]:
    return AIMessageChunk(content=text, id=message_id, **kwargs), {"langgraph_node": "agent"}


def _merge(frames: list[tuple[Any, Any]]) -> AIMessageChunk:
    """Accumulate message chunks exactly like an SDK stream consumer."""
    return reduce(operator.add, (message for message, _metadata in frames))


# ---------------------------------------------------------------------------
# Micro-batch boundaries
# ---------------------------------------------------------------------------


def test_the_leading_edge_ships_an_idle_streams_delta_immediately(clock: _Clock) -> None:
    coalescer = _TextDeltaCoalescer(window_seconds=_WINDOW)

    chunk = _delta("hello")
    assert coalescer.push(chunk) == [chunk]


def test_burst_deltas_merge_into_one_frame_per_window(clock: _Clock) -> None:
    coalescer = _TextDeltaCoalescer(window_seconds=_WINDOW)
    outputs: list[Any] = []

    outputs.extend(coalescer.push(_delta("a")))  # leading edge, ships alone
    for text, advance in (("b", 0.02), ("c", 0.02), ("d", 0.06)):
        clock.advance(advance)
        outputs.extend(coalescer.push(_delta(text)))

    # 4 inputs became 2 frames: "a", then the merged "bcd" on window expiry.
    assert len(outputs) == 2
    assert outputs[0][0].content == "a"
    assert outputs[1][0].content == "bcd"
    assert outputs[1][0].id == "m1"


def test_slow_streams_keep_per_token_latency(clock: _Clock) -> None:
    coalescer = _TextDeltaCoalescer(window_seconds=_WINDOW)

    first = _delta("a")
    assert coalescer.push(first) == [first]
    clock.advance(0.5)
    second = _delta("b")
    assert coalescer.push(second) == [second]


def test_the_size_cap_flushes_before_the_window(clock: _Clock) -> None:
    coalescer = _TextDeltaCoalescer(window_seconds=60.0)

    coalescer.push(_delta("a"))  # leading edge
    clock.advance(0.001)
    assert coalescer.push(_delta("b")) == []
    clock.advance(0.001)
    flushed = coalescer.push(_delta("x" * 5000))

    assert len(flushed) == 1
    assert flushed[0][0].content == "b" + "x" * 5000


def test_a_message_identity_switch_flushes_the_pending_frame(clock: _Clock) -> None:
    coalescer = _TextDeltaCoalescer(window_seconds=60.0)

    coalescer.push(_delta("a"))  # leading edge
    clock.advance(0.001)
    assert coalescer.push(_delta("b")) == []
    clock.advance(0.001)
    outputs = coalescer.push(_delta("c", message_id="m2"))

    assert [message.content for message, _metadata in outputs] == ["b"]
    assert coalescer.pending_message_id == "m2"


def test_non_text_frames_flush_first_and_pass_through_in_order(clock: _Clock) -> None:
    coalescer = _TextDeltaCoalescer(window_seconds=60.0)
    coalescer.push(_delta("a"))
    clock.advance(0.001)
    coalescer.push(_delta("b"))
    tool_frame = (ToolMessage(content="ok", tool_call_id="tc1"), {})

    outputs = coalescer.push(tool_frame)

    assert [type(message).__name__ for message, _metadata in outputs] == ["AIMessageChunk", "ToolMessage"]
    assert outputs[0][0].content == "b"
    assert outputs[1] is tool_frame


def test_tool_call_chunks_stay_on_the_file_batcher_path(clock: _Clock) -> None:
    coalescer = _TextDeltaCoalescer(window_seconds=60.0)
    tool_delta = (
        AIMessageChunk(
            content="",
            id="m1",
            tool_call_chunks=[{"name": "write_file", "args": '{"path": "a"', "id": "tc1", "index": 0, "type": "tool_call_chunk"}],
        ),
        {},
    )

    outputs = coalescer.push(tool_delta)

    assert outputs == [tool_delta]
    assert coalescer.pending_message is None


def test_provider_finish_markers_flush_immediately(clock: _Clock) -> None:
    coalescer = _TextDeltaCoalescer(window_seconds=60.0)
    coalescer.push(_delta("a"))
    clock.advance(0.001)
    assert coalescer.push(_delta("b")) == []
    clock.advance(0.001)

    final = _delta("", response_metadata={"finish_reason": "stop"})
    outputs = coalescer.push(final)

    assert len(outputs) == 1
    assert outputs[0][0].content == "b"
    assert outputs[0][0].response_metadata.get("finish_reason") == "stop"
    assert coalescer.pending_message is None


def test_usage_metadata_counts_as_a_finish_marker(clock: _Clock) -> None:
    coalescer = _TextDeltaCoalescer(window_seconds=60.0)
    usage = {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}

    outputs = coalescer.push(_delta("tail", usage_metadata=usage))

    assert len(outputs) == 1
    assert outputs[0][0].usage_metadata == usage


# ---------------------------------------------------------------------------
# Byte equivalence
# ---------------------------------------------------------------------------


def test_reassembled_text_is_byte_identical(clock: _Clock) -> None:
    coalescer = _TextDeltaCoalescer(window_seconds=_WINDOW)
    inputs = [_delta(f"token-{index} ") for index in range(50)]

    outputs: list[Any] = []
    for index, chunk in enumerate(inputs):
        outputs.extend(coalescer.push(chunk))
        # Mixed cadence: bursts, window expiries, and idle gaps.
        clock.advance((0.01, 0.03, 0.2)[index % 3])
    outputs.extend(coalescer.flush())

    assert len(outputs) < len(inputs)
    assert _merge(outputs).content == _merge(inputs).content
    assert _merge(outputs).id == "m1"


def test_reasoning_content_merges_byte_identically(clock: _Clock) -> None:
    """DeepSeek reasoner streams reasoning via additional_kwargs."""
    coalescer = _TextDeltaCoalescer(window_seconds=_WINDOW)
    inputs = [_delta("", additional_kwargs={"reasoning_content": f"thought-{index} "}) for index in range(20)]

    outputs: list[Any] = []
    for chunk in inputs:
        outputs.extend(coalescer.push(chunk))
        clock.advance(0.02)
    outputs.extend(coalescer.flush())

    assert len(outputs) < len(inputs)
    assert _merge(outputs).additional_kwargs["reasoning_content"] == _merge(inputs).additional_kwargs["reasoning_content"]


# ---------------------------------------------------------------------------
# Publish-path composition and toggle fallback
# ---------------------------------------------------------------------------


class _Bridge:
    def __init__(self) -> None:
        self.published: list[tuple[str, Any]] = []

    async def publish(self, run_id: str, event: str, payload: Any) -> None:
        self.published.append((event, payload))


def _subagent_events() -> Any:
    async def add(chunk: Any) -> None:
        added.append(chunk)

    added: list[Any] = []
    return SimpleNamespace(add=add, added=added)


async def _publish(bridge: _Bridge, coalescer: _TextDeltaCoalescer | None, *, mode: str, chunk: Any, namespace: tuple[str, ...] = ()) -> None:
    await _publish_stream_item(
        bridge=bridge,
        run_id="run-1",
        mode=mode,
        chunk=chunk,
        namespace=namespace,
        file_tool_chunk_batcher=None,
        text_delta_coalescer=coalescer,
        subagent_events=_subagent_events(),
    )


@pytest.mark.asyncio
async def test_root_text_deltas_coalesce_across_publish_calls(clock: _Clock) -> None:
    bridge = _Bridge()
    coalescer = _TextDeltaCoalescer(window_seconds=60.0)

    await _publish(bridge, coalescer, mode="messages", chunk=_delta("a"))  # leading edge
    clock.advance(0.001)
    await _publish(bridge, coalescer, mode="messages", chunk=_delta("b"))  # buffered

    assert [event for event, _payload in bridge.published] == ["messages"]
    assert bridge.published[0][1][0]["content"] == "a"


@pytest.mark.asyncio
async def test_a_root_values_frame_flushes_pending_text_first(clock: _Clock) -> None:
    bridge = _Bridge()
    coalescer = _TextDeltaCoalescer(window_seconds=60.0)
    await _publish(bridge, coalescer, mode="messages", chunk=_delta("a"))
    clock.advance(0.001)
    await _publish(bridge, coalescer, mode="messages", chunk=_delta("b"))

    await _publish(bridge, coalescer, mode="values", chunk={"messages": []})

    events = [event for event, _payload in bridge.published]
    assert events == ["messages", "messages", "values"]
    assert bridge.published[1][1][0]["content"] == "b"


@pytest.mark.asyncio
async def test_namespaced_frames_do_not_disturb_root_batching(clock: _Clock) -> None:
    bridge = _Bridge()
    coalescer = _TextDeltaCoalescer(window_seconds=60.0)
    await _publish(bridge, coalescer, mode="messages", chunk=_delta("a"))
    clock.advance(0.001)
    await _publish(bridge, coalescer, mode="messages", chunk=_delta("b"))

    await _publish(bridge, coalescer, mode="messages", chunk=_delta("sub", message_id="child"), namespace=("task:1",))

    assert [event for event, _payload in bridge.published] == ["messages", "messages|task:1"]
    assert coalescer.pending_message is not None  # root "b" still pending


@pytest.mark.asyncio
async def test_disabled_coalescer_publishes_every_frame_unchanged(clock: _Clock) -> None:
    """text_delta_flush_ms=0 → coalescer is None → per-token frames as before."""
    bridge = _Bridge()

    for text in ("a", "b", "c"):
        await _publish(bridge, None, mode="messages", chunk=_delta(text))

    assert [payload[0]["content"] for _event, payload in bridge.published] == ["a", "b", "c"]


def test_the_flush_window_config_defaults_to_75ms_and_supports_opt_out() -> None:
    assert DEFAULT_TEXT_DELTA_FLUSH_MS == 75
    assert WorkerStreamConfig().text_delta_flush_ms == 75
    assert WorkerStreamConfig(text_delta_flush_ms=0).text_delta_flush_ms == 0
    with pytest.raises(ValueError):
        WorkerStreamConfig(text_delta_flush_ms=-1)
