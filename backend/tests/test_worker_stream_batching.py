from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

from deerflow.runtime.runs.manager import RunManager
from deerflow.runtime.runs.worker import (
    RunContext,
    _LargeFileToolChunkBatcher,
    _publish_stream_item,
    run_agent,
)


@pytest.mark.parametrize("tool_name", ["write_file", "str_replace"])
def test_large_file_tool_chunk_batcher_streams_bounded_batches(
    tool_name: str,
) -> None:
    batcher = _LargeFileToolChunkBatcher(batch_size=2)
    first = AIMessageChunk(
        content="",
        id="ai-1",
        tool_call_chunks=[
            {
                "id": "call-1",
                "index": 0,
                "name": tool_name,
                "args": ('{"path":"/mnt/user-data/outputs/report.md","content":"Hel'),
            }
        ],
    )
    continuation = AIMessageChunk(
        content="",
        id="ai-1",
        tool_call_chunks=[
            {
                "index": 0,
                "name": None,
                "args": 'lo"}',
            }
        ],
    )

    assert batcher.push((first, {})) == []
    published = batcher.push((continuation, {}))

    assert len(published) == 1
    message, metadata = published[0]
    assert metadata == {}
    assert message.tool_calls[0]["args"]["content"] == "Hello"
    assert batcher.flush() == []


def test_large_file_tool_chunk_batcher_preserves_visible_and_non_file_chunks() -> None:
    batcher = _LargeFileToolChunkBatcher()
    visible_text = AIMessageChunk(
        content="Writing the report now.",
        id="ai-1",
    )
    search_tool = AIMessageChunk(
        content="",
        id="ai-2",
        tool_call_chunks=[
            {
                "id": "call-2",
                "index": 0,
                "name": "web_search",
                "args": '{"query":"vector databases"}',
            }
        ],
    )
    write_with_reasoning = AIMessageChunk(
        content="",
        id="ai-3",
        additional_kwargs={"reasoning_content": "Choosing a filename."},
        tool_call_chunks=[
            {
                "id": "call-3",
                "index": 0,
                "name": "write_file",
                "args": '{"path":"/mnt/user-data/outputs/report.md"}',
            }
        ],
    )

    assert batcher.push((visible_text, {})) == [(visible_text, {})]
    assert batcher.push((search_tool, {})) == [(search_tool, {})]

    visible_reasoning = batcher.push((write_with_reasoning, {}))

    assert len(visible_reasoning) == 1
    filtered_message, filtered_metadata = visible_reasoning[0]
    assert filtered_metadata == {}
    assert filtered_message.additional_kwargs == {"reasoning_content": "Choosing a filename."}
    assert filtered_message.tool_call_chunks == []
    pending_file_chunks = batcher.flush()
    assert len(pending_file_chunks) == 1
    assert pending_file_chunks[0][0].tool_call_chunks[0]["name"] == "write_file"


def test_large_file_tool_chunk_batcher_separates_metadata_namespaces() -> None:
    batcher = _LargeFileToolChunkBatcher()
    first = AIMessageChunk(
        content="",
        id="shared-ai-id",
        tool_call_chunks=[
            {
                "id": "call-a",
                "index": 0,
                "name": "write_file",
                "args": '{"path":"a.md","content":"A',
            }
        ],
    )
    second = AIMessageChunk(
        content="",
        id="shared-ai-id",
        tool_call_chunks=[
            {
                "id": "call-b",
                "index": 0,
                "name": "write_file",
                "args": '{"path":"b.md","content":"B',
            }
        ],
    )

    assert batcher.push((first, {"langgraph_checkpoint_ns": "task-a"})) == []
    published = batcher.push((second, {"langgraph_checkpoint_ns": "task-b"}))

    assert len(published) == 1
    assert published[0][1]["langgraph_checkpoint_ns"] == "task-a"
    assert batcher.flush()[0][1]["langgraph_checkpoint_ns"] == "task-b"


@pytest.mark.parametrize("metadata", [None, "not-a-dict"])
def test_large_file_tool_chunk_batcher_accepts_non_dict_metadata(
    metadata: Any,
) -> None:
    batcher = _LargeFileToolChunkBatcher(batch_size=1)
    message = AIMessageChunk(
        content="",
        id="ai-file",
        tool_call_chunks=[
            {
                "id": "call-file",
                "index": 0,
                "name": "write_file",
                "args": '{"path":"report.md","content":"draft"}',
            }
        ],
    )

    published = batcher.push((message, metadata))

    assert len(published) == 1
    assert published[0][0].tool_call_chunks[0]["args"] == '{"path":"report.md","content":"draft"}'
    assert published[0][1] == {}


def test_large_file_tool_chunk_batcher_does_not_retain_non_file_names() -> None:
    batcher = _LargeFileToolChunkBatcher()

    for index in range(100):
        message = AIMessageChunk(
            content="",
            id=f"ai-{index}",
            tool_call_chunks=[
                {
                    "id": f"call-{index}",
                    "index": 0,
                    "name": "web_search",
                    "args": '{"query":"deerflow"}',
                }
            ],
        )

        assert batcher.push((message, {})) == [(message, {})]

    assert batcher.tool_names == {}


def test_large_file_tool_chunk_batcher_starts_after_split_name_matches() -> None:
    batcher = _LargeFileToolChunkBatcher(batch_size=1)
    name_prefix = AIMessageChunk(
        content="",
        id="ai-file",
        tool_call_chunks=[
            {
                "id": "call-file",
                "index": 0,
                "name": "write_",
                "args": "",
            }
        ],
    )
    name_suffix = AIMessageChunk(
        content="",
        id="ai-file",
        tool_call_chunks=[
            {
                "index": 0,
                "name": "file",
                "args": '{"path":"report.md","content":"draft"}',
            }
        ],
    )

    assert batcher.push((name_prefix, {})) == [(name_prefix, {})]
    assert set(batcher.tool_names.values()) == {"write_"}
    assert len(batcher.push((name_suffix, {}))) == 1
    assert set(batcher.tool_names.values()) == {"write_file"}


def test_large_file_tool_chunk_batcher_keeps_identity_until_finish() -> None:
    batcher = _LargeFileToolChunkBatcher(batch_size=1)
    first = AIMessageChunk(
        content="",
        id="ai-file",
        tool_call_chunks=[
            {
                "id": "call-file",
                "index": 0,
                "name": "write_file",
                "args": '{"path":"report.md","content":"Hel',
            }
        ],
    )
    continuation = AIMessageChunk(
        content="",
        id="ai-file",
        tool_call_chunks=[
            {
                "index": 0,
                "name": None,
                "args": 'lo"}',
            }
        ],
    )

    assert len(batcher.push((first, {}))) == 1
    assert len(batcher.push((continuation, {}))) == 1
    assert set(batcher.tool_names.values()) == {"write_file"}

    assert batcher.finish() == []
    assert batcher.tool_names == {}


def test_large_file_tool_chunk_batcher_ignores_provider_transport_noise() -> None:
    batcher = _LargeFileToolChunkBatcher(batch_size=2)
    metadata = {"langgraph_checkpoint_ns": "model:root"}
    first = AIMessageChunk(
        content="",
        id="ai-file",
        response_metadata={"model_provider": "deepseek"},
        tool_call_chunks=[
            {
                "id": "call-file",
                "index": 0,
                "name": "write_file",
                "args": '{"path":"report.md","content":"Hel',
            },
        ],
    )
    transport_noise = AIMessageChunk(
        content="",
        id="ai-file",
        response_metadata={"model_provider": "deepseek"},
    )
    continuation = AIMessageChunk(
        content="",
        id="ai-file",
        response_metadata={"model_provider": "deepseek"},
        tool_call_chunks=[
            {
                "index": 0,
                "name": None,
                "args": 'lo"}',
            },
        ],
    )

    assert batcher.push((first, metadata)) == []
    assert batcher.push((transport_noise, metadata)) == []
    published = batcher.push((continuation, metadata))

    assert len(published) == 1
    message, published_metadata = published[0]
    assert published_metadata == metadata
    assert message.tool_calls[0]["args"]["content"] == "Hello"
    assert message.response_metadata == {}


@pytest.mark.anyio
async def test_namespaced_frames_bypass_root_file_batcher() -> None:
    batcher = _LargeFileToolChunkBatcher()
    root_chunk = (
        AIMessageChunk(
            content="",
            id="ai-root",
            tool_call_chunks=[
                {
                    "id": "call-root",
                    "index": 0,
                    "name": "write_file",
                    "args": '{"path":"root.md","content":"root',
                }
            ],
        ),
        {},
    )
    child_chunk = (
        AIMessageChunk(
            content="",
            id="ai-child",
            tool_call_chunks=[
                {
                    "id": "call-child",
                    "index": 0,
                    "name": "write_file",
                    "args": '{"path":"child.md","content":"child"}',
                }
            ],
        ),
        {},
    )
    bridge = SimpleNamespace(publish=AsyncMock())
    subagent_events = SimpleNamespace(add=AsyncMock())

    assert batcher.push(root_chunk) == []
    await _publish_stream_item(
        bridge=bridge,
        run_id="run-1",
        mode="messages",
        chunk=child_chunk,
        namespace=("tools:child",),
        file_tool_chunk_batcher=batcher,
        subagent_events=subagent_events,
    )

    bridge.publish.assert_awaited_once()
    published_run_id, event, payload = bridge.publish.await_args.args
    assert published_run_id == "run-1"
    assert event == "messages|tools:child"
    assert payload[0]["tool_call_chunks"][0]["args"] == ('{"path":"child.md","content":"child"}')
    assert batcher.pending_message is not None
    subagent_events.add.assert_not_awaited()


@pytest.mark.anyio
async def test_public_state_stream_modes_project_private_image_state_before_publish() -> None:
    image_path = "/mnt/user-data/uploads/current.png"
    chunk = {
        "node": {
            "viewed_images": {
                image_path: {
                    "mime_type": "image/png",
                    "size": 42,
                    "sha256": "a" * 64,
                    "file_ref": {
                        "path": image_path,
                        "sandbox_id": "private-sandbox-locator",
                        "run_id": "private-run-locator",
                        "project_id": "private-project-locator",
                        "owner_user_id": "private-owner-locator",
                    },
                }
            }
        },
        "status": "running",
    }

    for mode in ("updates", "debug", "tasks", "checkpoints", "custom"):
        bridge = SimpleNamespace(publish=AsyncMock())
        subagent_events = SimpleNamespace(add=AsyncMock())

        await _publish_stream_item(
            bridge=bridge,
            run_id="run-1",
            mode=mode,
            chunk=chunk,
            namespace=(),
            file_tool_chunk_batcher=None,
            subagent_events=subagent_events,
        )

        published_run_id, event, payload = bridge.publish.await_args.args
        assert published_run_id == "run-1"
        assert event == mode
        assert payload == {
            "node": {
                "viewed_images": {
                    image_path: {
                        "mime_type": "image/png",
                        "size": 42,
                        "sha256": "a" * 64,
                    }
                }
            },
            "status": "running",
        }
        assert "private-sandbox-locator" not in str(payload)
        assert "private-run-locator" not in str(payload)
        assert "private-project-locator" not in str(payload)
        assert "private-owner-locator" not in str(payload)


@pytest.mark.anyio
async def test_run_agent_batches_file_args_and_keeps_complete_values() -> None:
    run_manager = RunManager()
    record = await run_manager.create("thread-file-stream")
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )
    complete_message = AIMessage(
        content="",
        id="ai-file",
        tool_calls=[
            {
                "id": "call-file",
                "name": "write_file",
                "args": {
                    "path": "/mnt/user-data/outputs/report.md",
                    "content": "Hello world",
                },
                "type": "tool_call",
            }
        ],
    )

    class DummyAgent:
        async def astream(
            self,
            graph_input: object,
            *,
            config: object,
            stream_mode: object,
            subgraphs: bool = False,
        ):
            del graph_input, config, stream_mode, subgraphs
            yield (
                "messages",
                (
                    AIMessageChunk(
                        content="",
                        id="ai-file",
                        tool_call_chunks=[
                            {
                                "id": "call-file",
                                "index": 0,
                                "name": "write_file",
                                "args": ('{"path":"/mnt/user-data/outputs/report.md","content":"Hello'),
                            }
                        ],
                    ),
                    {},
                ),
            )
            yield (
                "messages",
                (
                    AIMessageChunk(
                        content="",
                        id="ai-file",
                        tool_call_chunks=[
                            {
                                "index": 0,
                                "name": None,
                                "args": ' world"}',
                            }
                        ],
                    ),
                    {},
                ),
            )
            yield ("values", {"messages": [complete_message]})

    await run_agent(
        bridge,
        run_manager,
        record,
        ctx=RunContext(checkpointer=None),
        agent_factory=lambda **_kwargs: DummyAgent(),
        graph_input={},
        config={},
        stream_modes=["messages-tuple", "values"],
    )

    message_events = [call.args for call in bridge.publish.await_args_list if call.args[1] == "messages"]
    assert len(message_events) == 1
    assert message_events[0][2][0]["tool_calls"][0]["args"]["content"] == "Hello world"
    values_events = [call.args[2] for call in bridge.publish.await_args_list if call.args[1] == "values"]
    assert any(event["messages"][0]["tool_calls"][0]["args"]["content"] == "Hello world" for event in values_events)


@pytest.mark.parametrize(
    ("stream_error", "flush_publish_error"),
    [
        (True, False),
        (True, True),
        (False, True),
    ],
)
@pytest.mark.anyio
async def test_run_agent_flushes_pending_file_args_on_error(
    stream_error: bool,
    flush_publish_error: bool,
) -> None:
    run_manager = RunManager()
    record = await run_manager.create("thread-file-stream-error")

    async def publish(_run_id: str, event: str, _data: Any) -> None:
        if flush_publish_error and event == "messages":
            raise RuntimeError("flush publish failed")

    bridge = SimpleNamespace(
        publish=AsyncMock(side_effect=publish),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )

    class DummyAgent:
        async def astream(
            self,
            graph_input: object,
            *,
            config: object,
            stream_mode: object,
            subgraphs: bool,
        ):
            del graph_input, config, stream_mode, subgraphs
            yield (
                "messages",
                (
                    AIMessageChunk(
                        content="",
                        id="ai-file",
                        tool_call_chunks=[
                            {
                                "id": "call-file",
                                "index": 0,
                                "name": "write_file",
                                "args": ('{"path":"report.md","content":"partial'),
                            }
                        ],
                    ),
                    {},
                ),
            )
            if stream_error:
                raise RuntimeError("stream failed")

    await run_agent(
        bridge,
        run_manager,
        record,
        ctx=RunContext(checkpointer=None),
        agent_factory=lambda **_kwargs: DummyAgent(),
        graph_input={},
        config={},
        stream_modes=["messages-tuple", "values"],
    )

    message_events = [call.args for call in bridge.publish.await_args_list if call.args[1] == "messages"]
    assert len(message_events) == 1
    assert message_events[0][2][0]["tool_call_chunks"][0]["args"].endswith('"content":"partial')
    error_events = [call.args for call in bridge.publish.await_args_list if call.args[1] == "error"]
    assert error_events[0][2] == {
        "message": "Run execution failed",
        "name": "RUN_EXECUTION_FAILED",
    }


@pytest.mark.anyio
async def test_run_agent_publishes_only_stable_error_contract_for_unhandled_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    run_manager = RunManager()
    record = await run_manager.create("thread-public-error-redaction")
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )
    private_detail = "provider-secret-sentinel DATA:image/png;base64,PRIVATE_BYTES /Users/private/provider/config.json"

    class DummyAgent:
        async def astream(self, *_args, **_kwargs):
            raise RuntimeError(private_detail)
            yield

    with caplog.at_level(
        "ERROR",
        logger="deerflow.runtime.runs.worker",
    ):
        await run_agent(
            bridge,
            run_manager,
            record,
            ctx=RunContext(checkpointer=None),
            agent_factory=lambda **_kwargs: DummyAgent(),
            graph_input={},
            config={},
        )

    persisted = await run_manager.get(record.run_id)
    assert persisted is not None
    assert persisted.error == "RUN_EXECUTION_FAILED"
    error_events = [call.args[2] for call in bridge.publish.await_args_list if call.args[1] == "error"]
    assert error_events == [
        {
            "message": "Run execution failed",
            "name": "RUN_EXECUTION_FAILED",
        }
    ]
    public_payload = str((persisted.error, error_events))
    assert "provider-secret-sentinel" not in public_payload
    assert "PRIVATE_BYTES" not in public_payload
    assert "/Users/private" not in public_payload
    assert "provider-secret-sentinel" not in caplog.text
    assert "PRIVATE_BYTES" not in caplog.text
    assert "/Users/private" not in caplog.text


@pytest.mark.anyio
async def test_run_agent_keeps_file_chunks_unbatched_without_values() -> None:
    run_manager = RunManager()
    record = await run_manager.create("thread-file-messages-only")
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )
    chunks = [
        AIMessageChunk(
            content="",
            id="ai-file",
            tool_call_chunks=[
                {
                    "id": "call-file",
                    "index": 0,
                    "name": "write_file",
                    "args": '{"path":"report.md","content":"Hel',
                }
            ],
        ),
        AIMessageChunk(
            content="",
            id="ai-file",
            tool_call_chunks=[
                {
                    "index": 0,
                    "name": None,
                    "args": 'lo"}',
                }
            ],
        ),
    ]

    class DummyAgent:
        async def astream(
            self,
            graph_input: object,
            *,
            config: object,
            stream_mode: object,
            subgraphs: bool = False,
        ):
            del graph_input, config, stream_mode, subgraphs
            for chunk in chunks:
                yield (chunk, {})

    await run_agent(
        bridge,
        run_manager,
        record,
        ctx=RunContext(checkpointer=None),
        agent_factory=lambda **_kwargs: DummyAgent(),
        graph_input={},
        config={},
        stream_modes=["messages-tuple"],
    )

    message_events = [call.args[2] for call in bridge.publish.await_args_list if call.args[1] == "messages"]
    assert len(message_events) == 2
    assert message_events[0][0]["tool_call_chunks"][0]["args"].endswith('"content":"Hel')
    assert message_events[1][0]["tool_call_chunks"][0]["args"] == 'lo"}'
