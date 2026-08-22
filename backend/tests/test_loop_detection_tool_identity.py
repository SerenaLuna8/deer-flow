"""Regression tests for content-sensitive loop detection tool identities."""

import asyncio
from collections.abc import Callable, Iterable
from typing import override

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from deerflow.agents.middlewares.loop_detection_middleware import (
    LoopDetectionMiddleware,
    _current_read_marks,
    _hash_tool_calls,
)
from deerflow.agents.middlewares.read_before_write_middleware import (
    READ_MARK_KEY,
    ReadBeforeWriteMiddleware,
)
from deerflow.runtime.context_keys import RuntimeContextKeys
from deerflow.runtime.journal import RunJournal
from deerflow.runtime.runs.execution_contracts import RunSemanticStopRecorder


class _ToolBindingFakeModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        del tools, kwargs
        return self


class _RecordingEventStore:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def put_batch(self, events: list[dict], **_kwargs: object) -> None:
        self.events.extend(events)


class _ModelRequestToolsProbe(AgentMiddleware):
    def __init__(self) -> None:
        self.tool_names_by_call: list[list[str]] = []

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        self.tool_names_by_call.append([tool.name for tool in request.tools])
        return handler(request)


def _model(responses: Iterable[AIMessage]) -> _ToolBindingFakeModel:
    return _ToolBindingFakeModel(messages=iter(responses))


def _candidate_upsert(*, content: str, checksum: str | None) -> list[dict]:
    return [
        {
            "name": "upsert_candidate_file",
            "args": {
                "path": "SKILL.md",
                "media_type": "text/markdown",
                "content": content,
                "mode": "replace" if checksum is None else "append",
                "expected_draft_checksum": checksum,
                "expected_file_size_bytes": 0 if checksum is None else 6,
                "expected_file_sha256": checksum,
            },
        }
    ]


def test_candidate_file_chunks_have_distinct_loop_detection_identities() -> None:
    first = _candidate_upsert(content="first\n", checksum=None)
    second = _candidate_upsert(content="second\n", checksum="a" * 64)

    assert _hash_tool_calls(first) != _hash_tool_calls(second)


def test_identical_candidate_file_upserts_keep_stable_identity() -> None:
    call = _candidate_upsert(content="same\n", checksum=None)

    assert _hash_tool_calls(call) == _hash_tool_calls(call)


def _read_call(path: str = "report.md") -> list[dict]:
    return [
        {
            "name": "read_file",
            "args": {"path": path, "line_start": 1, "line_end": 30},
        }
    ]


def test_authenticated_read_versions_define_productive_loop_progress() -> None:
    call = _read_call()

    first = _hash_tool_calls(call, read_marks={"report.md": "a" * 64})
    repeated = _hash_tool_calls(call, read_marks={"report.md": "a" * 64})
    changed = _hash_tool_calls(call, read_marks={"report.md": "b" * 64})

    assert repeated == first
    assert changed != first


@pytest.mark.parametrize(
    ("name", "mark"),
    [
        ("bash", {"path": "report.md", "hash": "a" * 64}),
        ("read_file", {"path": "report.md", "hash": []}),
        ("read_file", {"path": "report.md", "hash": "not-a-sha256"}),
    ],
)
def test_only_valid_read_file_marks_are_trusted(name: str, mark: object) -> None:
    message = ToolMessage(
        content="observed content",
        tool_call_id="call-1",
        name=name,
        additional_kwargs={READ_MARK_KEY: mark},
    )

    assert _current_read_marks([message]) == {}


def test_loop_hard_stop_clears_valid_and_invalid_tool_call_payloads() -> None:
    proposal = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "lookup",
                "args": {"value": "same"},
                "id": "call-valid",
            }
        ],
        invalid_tool_calls=[
            {
                "name": "lookup",
                "args": "{",
                "id": "call-invalid",
                "error": "invalid arguments",
            }
        ],
    )

    update = LoopDetectionMiddleware._build_hard_stop_update(
        proposal,
        "forced stop",
    )

    assert update["tool_calls"] == []
    assert update["invalid_tool_calls"] == []


def test_loop_hard_stop_runs_one_tool_free_finalization_turn() -> None:
    calls: list[str] = []
    stop_recorder = RunSemanticStopRecorder()
    event_store = _RecordingEventStore()
    journal = RunJournal(
        "run-1",
        "thread-1",
        event_store,  # type: ignore[arg-type]
        flush_threshold=100,
        semantic_stop_recorder=stop_recorder,
    )

    @tool
    def lookup(value: str) -> str:
        """Return a deterministic value."""

        calls.append(value)
        return f"result:{value}"

    middleware = LoopDetectionMiddleware(
        warn_threshold=1,
        hard_limit=2,
        tool_freq_warn=100,
        tool_freq_hard_limit=100,
    )
    agent = create_agent(
        model=_model(
            [
                AIMessage(
                    id="proposal-1",
                    content="",
                    tool_calls=[
                        {
                            "name": "lookup",
                            "args": {"value": "same"},
                            "id": "call-1",
                        }
                    ],
                ),
                AIMessage(
                    id="proposal-2",
                    content="",
                    tool_calls=[
                        {
                            "name": "lookup",
                            "args": {"value": "same"},
                            "id": "call-2",
                        }
                    ],
                ),
                AIMessage(
                    id="final-1",
                    content="final answer from collected results",
                ),
            ]
        ),
        tools=[lookup],
        middleware=[middleware],
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="look it up and answer")]},
        config={"callbacks": [journal]},
        context={
            "thread_id": "thread-1",
            "run_id": "run-1",
            RuntimeContextKeys.RUN_SEMANTIC_STOP_RECORDER: stop_recorder,
        },
    )
    asyncio.run(journal.flush())

    assert calls == ["same"]
    assert result["messages"][-1].content == "final answer from collected results"
    assert stop_recorder.reason == "loop_capped"
    assert middleware.consume_stop_reason("run-1") is None

    persisted_messages = [event["content"] for event in event_store.events if event["category"] == "message"]
    latest_by_id = {message["id"]: message for message in persisted_messages if isinstance(message, dict) and isinstance(message.get("id"), str) and message["id"]}
    assert latest_by_id["proposal-2"]["additional_kwargs"]["hide_from_ui"] is True
    assert latest_by_id["proposal-2"]["tool_calls"] == []
    reconciled = [event for event in event_store.events if event["metadata"].get("source") == "loop_safety_reconciliation"]
    assert len(reconciled) == 1
    assert reconciled[0]["content"]["id"] == "proposal-2"
    run_end = [event for event in event_store.events if event["event_type"] == "run.end"]
    assert len(run_end) == 1
    assert run_end[0]["metadata"]["status"] == "error"
    visible_tool_call_ids = [tool_call["id"] for message in latest_by_id.values() if message.get("type") == "ai" and message.get("additional_kwargs", {}).get("hide_from_ui") is not True for tool_call in message.get("tool_calls", [])]
    assert visible_tool_call_ids == ["call-1"]


def test_loop_finalization_rejects_an_adversarial_third_tool_call() -> None:
    calls: list[str] = []
    tools_probe = _ModelRequestToolsProbe()
    stop_recorder = RunSemanticStopRecorder()
    event_store = _RecordingEventStore()
    journal = RunJournal(
        "run-adversarial",
        "thread-adversarial",
        event_store,  # type: ignore[arg-type]
        flush_threshold=100,
        semantic_stop_recorder=stop_recorder,
    )

    @tool
    def lookup(value: str) -> str:
        """Return a deterministic value."""

        calls.append(value)
        return f"result:{value}"

    agent = create_agent(
        model=_model(
            [
                AIMessage(
                    id="proposal-1",
                    content="",
                    tool_calls=[
                        {
                            "name": "lookup",
                            "args": {"value": "same"},
                            "id": "call-1",
                        }
                    ],
                ),
                AIMessage(
                    id="proposal-2",
                    content="",
                    tool_calls=[
                        {
                            "name": "lookup",
                            "args": {"value": "same"},
                            "id": "call-2",
                        }
                    ],
                ),
                AIMessage(
                    id="adversarial-3",
                    content="",
                    tool_calls=[
                        {
                            "name": "lookup",
                            "args": {"value": "must-not-run"},
                            "id": "call-3",
                        }
                    ],
                ),
            ]
        ),
        tools=[lookup],
        middleware=[
            LoopDetectionMiddleware(
                warn_threshold=1,
                hard_limit=2,
                tool_freq_warn=100,
                tool_freq_hard_limit=100,
            ),
            tools_probe,
        ],
    )

    with pytest.raises(
        RuntimeError,
        match="Loop finalization attempted another tool call",
    ):
        agent.invoke(
            {"messages": [HumanMessage(content="look it up and answer")]},
            config={"callbacks": [journal]},
            context={
                "thread_id": "thread-adversarial",
                "run_id": "run-adversarial",
                RuntimeContextKeys.RUN_JOURNAL: journal,
                RuntimeContextKeys.RUN_SEMANTIC_STOP_RECORDER: stop_recorder,
            },
        )
    asyncio.run(journal.flush())

    assert calls == ["same"]
    assert tools_probe.tool_names_by_call == [["lookup"], ["lookup"], []]
    persisted_messages = [event["content"] for event in event_store.events if event["category"] == "message"]
    latest_by_id = {message["id"]: message for message in persisted_messages if isinstance(message, dict) and isinstance(message.get("id"), str) and message["id"]}
    assert latest_by_id["proposal-2"]["additional_kwargs"]["hide_from_ui"] is True
    assert latest_by_id["proposal-2"]["tool_calls"] == []
    assert latest_by_id["adversarial-3"]["additional_kwargs"]["hide_from_ui"] is True
    assert latest_by_id["adversarial-3"]["tool_calls"] == []
    run_errors = [event for event in event_store.events if event["event_type"] == "run.error"]
    assert len(run_errors) == 1


@pytest.mark.asyncio
async def test_loop_hard_stop_async_runs_one_tool_free_finalization_turn() -> None:
    calls: list[str] = []

    @tool
    async def lookup(value: str) -> str:
        """Return a deterministic value."""

        calls.append(value)
        return f"result:{value}"

    middleware = LoopDetectionMiddleware(
        warn_threshold=1,
        hard_limit=2,
        tool_freq_warn=100,
        tool_freq_hard_limit=100,
    )
    agent = create_agent(
        model=_model(
            [
                AIMessage(
                    content="",
                    tool_calls=[{"name": "lookup", "args": {"value": "same"}, "id": "call-1"}],
                ),
                AIMessage(
                    content="",
                    tool_calls=[{"name": "lookup", "args": {"value": "same"}, "id": "call-2"}],
                ),
                AIMessage(content="async final answer from collected results"),
            ]
        ),
        tools=[lookup],
        middleware=[middleware],
    )

    result = await agent.ainvoke(
        {"messages": [HumanMessage(content="look it up and answer")]},
        context={"thread_id": "thread-1", "run_id": "run-async"},
    )

    assert calls == ["same"]
    assert result["messages"][-1].content == "async final answer from collected results"
    assert middleware.consume_stop_reason("run-async") == "loop_capped"


def test_fresh_read_after_each_successful_write_is_productive_progress() -> None:
    files = {"report.md": "0"}

    @tool
    def read_file(path: str) -> str:
        """Read one text file."""

        return files[path]

    @tool
    def str_replace(path: str, old_str: str, new_str: str) -> str:
        """Replace one exact string in a text file."""

        assert files[path] == old_str
        files[path] = new_str
        return "updated"

    responses: list[AIMessage] = []
    for version in range(5):
        responses.extend(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_file",
                            "args": {"path": "report.md"},
                            "id": f"read-{version}",
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "str_replace",
                            "args": {
                                "path": "report.md",
                                "old_str": str(version),
                                "new_str": str(version + 1),
                            },
                            "id": f"write-{version}",
                        }
                    ],
                ),
            ]
        )
    responses.append(AIMessage(content="all five revisions completed"))

    agent = create_agent(
        model=_model(responses),
        tools=[read_file, str_replace],
        middleware=[
            ReadBeforeWriteMiddleware(content_reader=lambda _runtime, path: files[path]),
            LoopDetectionMiddleware(
                warn_threshold=3,
                hard_limit=5,
                tool_freq_warn=100,
                tool_freq_hard_limit=100,
            ),
        ],
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="apply five verified revisions")]},
        context={"thread_id": "thread-1", "run_id": "run-1"},
    )

    assert files["report.md"] == "5"
    assert result["messages"][-1].content == "all five revisions completed"
