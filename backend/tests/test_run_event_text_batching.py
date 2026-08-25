"""Root text-delta micro-batching.

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

import asyncio
import gc
import operator
import time
import uuid
from functools import reduce
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessageChunk, ToolMessage

import deerflow.runtime.runs.worker as worker_module
from deerflow.config.worker_config import DEFAULT_TEXT_DELTA_FLUSH_MS, WorkerStreamConfig
from deerflow.runtime.runs.worker import (
    _TEXT_DELTA_FLUSH_DUE,
    _iter_with_text_delta_deadline,
    _LargeFileToolChunkBatcher,
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


@pytest.mark.asyncio
async def test_a_buffered_delta_flushes_at_the_hard_deadline_without_a_next_frame() -> None:
    """The 75ms bound is a timer, not a check deferred until the next token."""

    async def stalled_stream():
        yield _delta("a")
        yield _delta("b")
        await asyncio.sleep(5)

    coalescer = _TextDeltaCoalescer(window_seconds=_WINDOW)
    started = time.monotonic()
    flushed: list[Any] = []
    async for item in _iter_with_text_delta_deadline(stalled_stream(), coalescer):
        if item is _TEXT_DELTA_FLUSH_DUE:
            flushed.extend(coalescer.flush())
            break
        flushed.extend(coalescer.push(item))

    elapsed = time.monotonic() - started
    assert [message.content for message, _metadata in flushed] == ["a", "b"]
    assert _WINDOW * 0.5 <= elapsed < 0.5


@pytest.mark.asyncio
async def test_deadline_iterator_ends_cleanly_when_batching_is_disabled() -> None:
    async def finite_stream():
        yield "first"
        yield "second"

    observed = [item async for item in _iter_with_text_delta_deadline(finite_stream(), None)]

    assert observed == ["first", "second"]


@pytest.mark.asyncio
async def test_deadline_iterator_active_batching_exhausts_without_a_task_leak() -> None:
    baseline = asyncio.all_tasks()
    source_closed = asyncio.Event()

    async def finite_stream():
        try:
            yield _delta("a")
            yield _delta("b")
        finally:
            source_closed.set()

    coalescer = _TextDeltaCoalescer(window_seconds=60.0)
    observed: list[Any] = []
    async for item in _iter_with_text_delta_deadline(finite_stream(), coalescer):
        observed.extend(coalescer.push(item))
    observed.extend(coalescer.flush())
    await asyncio.sleep(0)

    assert source_closed.is_set()
    assert [message.content for message, _metadata in observed] == ["a", "b"]
    assert not (asyncio.all_tasks() - baseline)


@pytest.mark.asyncio
async def test_disabled_batching_does_not_spawn_per_frame_iterator_tasks() -> None:
    """The 0 switch restores direct iteration, not just identical payloads."""

    caller = asyncio.current_task()
    source_tasks: list[asyncio.Task[Any] | None] = []

    async def finite_stream():
        source_tasks.append(asyncio.current_task())
        yield "first"
        source_tasks.append(asyncio.current_task())
        yield "second"

    observed = [item async for item in _iter_with_text_delta_deadline(finite_stream(), None)]

    assert observed == ["first", "second"]
    assert source_tasks == [caller, caller]


@pytest.mark.parametrize("batching_enabled", [False, True])
@pytest.mark.asyncio
async def test_deadline_iterator_propagates_source_exceptions(
    batching_enabled: bool,
) -> None:
    async def broken_stream():
        yield "first"
        raise RuntimeError("source failed")

    coalescer = _TextDeltaCoalescer(window_seconds=_WINDOW) if batching_enabled else None
    observed: list[str] = []
    with pytest.raises(RuntimeError, match="source failed"):
        async for item in _iter_with_text_delta_deadline(broken_stream(), coalescer):
            observed.append(item)
            if coalescer is not None:
                coalescer.push(item)

    assert observed == ["first"]


@pytest.mark.asyncio
async def test_deadline_iterator_cancellation_closes_the_pending_source_task() -> None:
    entered_wait = asyncio.Event()
    source_closed = asyncio.Event()
    blocker = asyncio.Event()

    async def stalled_stream():
        try:
            yield _delta("a")
            yield _delta("b")
            entered_wait.set()
            await blocker.wait()
        finally:
            source_closed.set()

    coalescer = _TextDeltaCoalescer(window_seconds=_WINDOW)

    async def consume() -> None:
        async for item in _iter_with_text_delta_deadline(stalled_stream(), coalescer):
            if item is _TEXT_DELTA_FLUSH_DUE:
                coalescer.flush()
            else:
                coalescer.push(item)

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(entered_wait.wait(), timeout=0.5)
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    await asyncio.wait_for(source_closed.wait(), timeout=0.5)


@pytest.mark.asyncio
async def test_closing_after_a_timer_flush_reaps_a_late_source_failure() -> None:
    """A provider task finishing while the marker is yielded must be observed."""

    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    unhandled: list[dict[str, Any]] = []
    source_closed = asyncio.Event()

    def record_unhandled(_loop, context: dict[str, Any]) -> None:
        unhandled.append(context)

    async def late_failure_stream():
        try:
            yield _delta("a")
            yield _delta("b")
            await asyncio.sleep(_WINDOW * 1.2)
            raise RuntimeError("late provider failure")
        finally:
            source_closed.set()

    loop.set_exception_handler(record_unhandled)
    try:
        coalescer = _TextDeltaCoalescer(window_seconds=_WINDOW)
        wrapped = _iter_with_text_delta_deadline(late_failure_stream(), coalescer)
        while True:
            item = await anext(wrapped)
            if item is _TEXT_DELTA_FLUSH_DUE:
                coalescer.flush()
                break
            coalescer.push(item)

        # Let the in-flight __anext__ task fail while the wrapper is suspended
        # at the timer marker, then abandon the stream like an aborting caller.
        await asyncio.sleep(_WINDOW)
        await wrapped.aclose()
        del wrapped
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert source_closed.is_set()
    assert not unhandled


def test_the_size_cap_flushes_before_the_window(clock: _Clock) -> None:
    coalescer = _TextDeltaCoalescer(window_seconds=60.0)

    coalescer.push(_delta("a"))  # leading edge
    clock.advance(0.001)
    assert coalescer.push(_delta("b")) == []
    clock.advance(0.001)
    flushed = coalescer.push(_delta("x" * 5000))

    assert len(flushed) == 1
    assert flushed[0][0].content == "b" + "x" * 5000


def test_the_size_cap_counts_utf8_bytes_in_structured_content(clock: _Clock) -> None:
    block = {"type": "text", "text": "你好"}
    character_size = len(str(block))
    coalescer = _TextDeltaCoalescer(
        window_seconds=60.0,
        max_pending_bytes=character_size + 1,
    )
    coalescer.push(_delta("leading"))

    structured = (
        AIMessageChunk(content=[block], id="m1"),
        {"langgraph_node": "agent"},
    )
    outputs = coalescer.push(structured)

    assert outputs == [structured]
    assert coalescer.pending_message is None


def test_the_size_cap_counts_reasoning_content(clock: _Clock) -> None:
    coalescer = _TextDeltaCoalescer(
        window_seconds=60.0,
        max_pending_bytes=4,
    )
    coalescer.push(_delta("leading"))

    reasoning = _delta(
        "",
        additional_kwargs={"reasoning_content": "思考"},
    )
    outputs = coalescer.push(reasoning)

    assert outputs == [reasoning]
    assert coalescer.pending_message is None


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


def test_bursty_replay_reduces_text_frames_by_at_least_one_order_of_magnitude(
    clock: _Clock,
) -> None:
    """The deterministic replay sample pins the write-amplification target."""
    inputs = [_delta(f"{index:03d}") for index in range(100)]
    coalescer = _TextDeltaCoalescer(window_seconds=_WINDOW)
    outputs: list[Any] = []

    for chunk in inputs:
        outputs.extend(coalescer.push(chunk))
        clock.advance(0.0005)
    outputs.extend(coalescer.flush())

    assert len(outputs) <= len(inputs) // 10
    assert _merge(outputs).content == _merge(inputs).content


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_same_burst_persists_one_order_fewer_durable_rows_with_exact_bytes(
    clock: _Clock,
    migrated_postgres_database_url: str,
) -> None:
    """Close the acceptance loop at the actual ``run_events`` row boundary."""

    from support.private_thread_seed import seed_private_thread_database
    from support.run_closure import add_sealed_test_run

    from deerflow.persistence.run.model import RunRow
    from deerflow.persistence.thread_meta.model import ThreadMetaRow
    from deerflow.runtime.events.models import StreamFrame
    from deerflow.runtime.events.store.db import DbRunEventStore

    seed = await seed_private_thread_database(migrated_postgres_database_url)
    expected = "".join(f"{index:03d}" for index in range(100))

    async def seed_run(label: str) -> tuple[str, str]:
        thread_id = f"batch-{label}-{uuid.uuid4()}"
        run_id = f"batch-{label}-run-{uuid.uuid4()}"
        async with seed.factory() as session, session.begin():
            session.add(
                ThreadMetaRow(
                    thread_id=thread_id,
                    assistant_id=str(seed.project_agent_id),
                    owner_user_id=seed.owner_a_scope.owner_user_id,
                    display_name=f"Batching {label}",
                    status="idle",
                    metadata_json={},
                    project_id=seed.owner_a.project_id,
                    agent_asset_id=seed.project_agent_id,
                    agent_scope="project",
                )
            )
            await session.flush()
            await add_sealed_test_run(
                session,
                RunRow(
                    run_id=run_id,
                    thread_id=thread_id,
                    assistant_id=str(seed.project_agent_id),
                    owner_user_id=seed.owner_a_scope.owner_user_id,
                    status="running",
                    model_name="test-model",
                    multitask_strategy="reject",
                    metadata_json={},
                    kwargs_json={},
                    origin_trace_id=("a" if label == "raw" else "b") * 32,
                    project_id=seed.owner_a.project_id,
                ),
            )
        return thread_id, run_id

    async def replay(coalescer: _TextDeltaCoalescer | None) -> _Bridge:
        bridge = _Bridge()
        for index in range(100):
            await _publish(
                bridge,
                coalescer,
                mode="messages",
                chunk=_delta(f"{index:03d}"),
            )
            clock.advance(0.0005)
        if coalescer is not None:
            for pending in coalescer.flush():
                await _publish(
                    bridge,
                    None,
                    mode="messages",
                    chunk=pending,
                )
        return bridge

    try:
        raw_thread, raw_run = await seed_run("raw")
        batched_thread, batched_run = await seed_run("batched")
        raw = await replay(None)
        batched = await replay(_TextDeltaCoalescer(window_seconds=_WINDOW))
        store = DbRunEventStore(seed.factory, run_event_notify_enabled=False)

        for thread_id, run_id, bridge in (
            (raw_thread, raw_run, raw),
            (batched_thread, batched_run, batched),
        ):
            async with seed.factory() as session, session.begin():
                for event, data in bridge.published:
                    await store.append_stream_frame(
                        session,
                        scope=seed.owner_a_scope,
                        thread_id=thread_id,
                        run_id=run_id,
                        frame=StreamFrame(event=event, data=data),
                    )

        async with seed.factory() as session:
            raw_rows = await store.list_stream_frames(
                session,
                scope=seed.owner_a_scope,
                thread_id=raw_thread,
                cursor=0,
                limit=200,
                run_id=raw_run,
            )
            batched_rows = await store.list_stream_frames(
                session,
                scope=seed.owner_a_scope,
                thread_id=batched_thread,
                cursor=0,
                limit=200,
                run_id=batched_run,
            )

        assert len(raw_rows) == 100
        assert len(batched_rows) <= len(raw_rows) // 10
        assert "".join(frame.data[0]["content"] for frame in raw_rows) == expected
        assert "".join(frame.data[0]["content"] for frame in batched_rows) == expected
    finally:
        await seed.engine.dispose()


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
async def test_non_text_boundary_preserves_text_then_file_then_values_order(
    clock: _Clock,
) -> None:
    """Text batching composes with the older file-tool batcher without reordering."""

    bridge = _Bridge()
    coalescer = _TextDeltaCoalescer(window_seconds=60.0)
    file_batcher = _LargeFileToolChunkBatcher()
    subagent_events = _subagent_events()

    async def publish(mode: str, chunk: Any) -> None:
        await _publish_stream_item(
            bridge=bridge,
            run_id="run-1",
            mode=mode,
            chunk=chunk,
            namespace=(),
            file_tool_chunk_batcher=file_batcher,
            text_delta_coalescer=coalescer,
            subagent_events=subagent_events,
        )

    await publish("messages", _delta("a"))  # leading edge
    clock.advance(0.001)
    await publish("messages", _delta("b"))  # buffered text
    tool_delta = (
        AIMessageChunk(
            content="",
            id="m1",
            tool_call_chunks=[
                {
                    "name": "write_file",
                    "args": '{"path":"out.txt","content":"x"}',
                    "id": "tc1",
                    "index": 0,
                    "type": "tool_call_chunk",
                }
            ],
        ),
        {},
    )
    await publish("messages", tool_delta)

    # The non-text tool frame first flushes "b"; its own body remains in the
    # file batcher until the values boundary.
    assert [event for event, _payload in bridge.published] == ["messages", "messages"]
    assert bridge.published[1][1][0]["content"] == "b"

    await publish("values", {"messages": []})

    assert [event for event, _payload in bridge.published] == [
        "messages",
        "messages",
        "messages",
        "values",
    ]
    assert bridge.published[2][1][0]["tool_call_chunks"][0]["name"] == "write_file"


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
