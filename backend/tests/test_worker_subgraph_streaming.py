from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from deerflow.persistence.models.run_event import RunEventRow
from deerflow.runtime.events.models import StreamFrame
from deerflow.runtime.events.store.db import DbRunEventStore
from deerflow.runtime.runs.manager import RunManager
from deerflow.runtime.runs.worker import RunContext, run_agent


class _SubgraphStreamAgent:
    def __init__(self) -> None:
        self.metadata: dict[str, Any] = {}
        self.checkpointer = None
        self.store = None
        self.interrupt_before_nodes: list[str] = []
        self.interrupt_after_nodes: list[str] = []
        self.stream_subgraphs: bool | None = None

    async def astream(
        self,
        _graph_input: object,
        *,
        config: dict[str, Any],
        stream_mode: list[str],
        subgraphs: bool,
    ):
        del config, stream_mode
        self.stream_subgraphs = subgraphs
        yield (), "values", {"messages": [{"type": "ai", "content": "lead"}]}
        yield ("tools:call-1",), "values", {"messages": [{"type": "ai", "content": "nested"}]}
        yield (
            ("tools:call-1",),
            "messages",
            ({"type": "ai", "content": "nested token", "id": "nested-1"}, {"tags": []}),
        )
        yield (
            (),
            "messages",
            ({"type": "ai", "content": "lead token", "id": "lead-1"}, {"tags": []}),
        )


class _CaptureBridge:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    async def publish(self, _run_id: str, event: str, payload: object) -> None:
        self.events.append((event, payload))

    async def publish_end(self, _run_id: str) -> None:
        self.events.append(("end", None))

    async def cleanup(self, _run_id: str, *, delay: int = 0) -> None:
        del delay


@pytest.mark.anyio
async def test_run_agent_preserves_subgraph_namespace_in_sse_event_name() -> None:
    run_manager = RunManager()
    record = await run_manager.create("thread-1")
    agent = _SubgraphStreamAgent()
    bridge = _CaptureBridge()

    await run_agent(
        bridge,
        run_manager,
        record,
        ctx=RunContext(checkpointer=None),
        agent_factory=lambda config: agent,
        graph_input={"messages": []},
        config={"configurable": {"thread_id": "thread-1"}},
        stream_modes=["values", "messages-tuple"],
        stream_subgraphs=True,
    )

    assert agent.stream_subgraphs is True
    assert [event for event, _payload in bridge.events] == [
        "metadata",
        "values",
        "values|tools:call-1",
        "messages|tools:call-1",
        "messages",
        "end",
    ]


def test_namespaced_stream_event_round_trips_through_bounded_event_type() -> None:
    event = f"messages|tools:{uuid.uuid4()}|model:{uuid.uuid4()}"
    frame = StreamFrame(event=event, data={"content": "nested"})

    event_type, metadata = DbRunEventStore._stream_event_storage(frame)

    assert event_type == "messages"
    assert len(event_type) <= 32
    assert metadata == {
        "stream_terminal": False,
        "stream_event_name": event,
    }

    row = RunEventRow(
        id=1,
        thread_id="thread-1",
        run_id="run-1",
        project_id=uuid.uuid4(),
        owner_user_id=str(uuid.uuid4()),
        event_type=event_type,
        category="stream",
        content='{"content":"nested"}',
        event_metadata={
            **metadata,
            "content_is_json": True,
            "content_is_dict": True,
        },
        seq=1,
        created_at=datetime.now(UTC),
    )

    restored = DbRunEventStore._stream_row(row, created=False)

    assert restored.event == event
    assert restored.data == {"content": "nested"}
    assert restored.created is False


def test_stream_event_replay_is_backward_compatible_and_ignores_corrupt_metadata() -> None:
    legacy = RunEventRow(
        id=1,
        thread_id="thread-1",
        run_id="run-1",
        project_id=uuid.uuid4(),
        owner_user_id=str(uuid.uuid4()),
        event_type="values",
        category="stream",
        content="legacy",
        event_metadata={"stream_terminal": False},
        seq=1,
        created_at=datetime.now(UTC),
    )
    corrupt = RunEventRow(
        id=2,
        thread_id="thread-1",
        run_id="run-1",
        project_id=legacy.project_id,
        owner_user_id=legacy.owner_user_id,
        event_type="values",
        category="stream",
        content="corrupt",
        event_metadata={
            "stream_terminal": False,
            "stream_event_name": "messages|tools:wrong-base",
        },
        seq=2,
        created_at=datetime.now(UTC),
    )

    assert DbRunEventStore._stream_row(legacy, created=False).event == "values"
    assert DbRunEventStore._stream_row(corrupt, created=False).event == "values"


@pytest.mark.parametrize(
    "event",
    (
        "values|",
        "values||tools:call-1",
        "values|tools:call-1\nerror",
        f"values|{'x' * 257}",
    ),
)
def test_namespaced_stream_event_rejects_unsafe_namespace(event: str) -> None:
    with pytest.raises(ValueError, match="event is invalid"):
        StreamFrame(event=event, data={})
