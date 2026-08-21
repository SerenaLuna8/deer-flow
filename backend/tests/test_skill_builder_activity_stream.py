from __future__ import annotations

import pytest

from app.shared_assets.skill_builder_activity_stream import (
    SkillBuilderActivityStreamBridge,
)
from app.shared_assets.skill_design_activity import SkillDesignActivityKind


class _Bridge:
    def __init__(self) -> None:
        self.frames: list[tuple[str, str, object]] = []

    async def publish(self, run_id: str, event: str, data: object) -> None:
        self.frames.append((run_id, event, data))

    async def publish_end(self, run_id: str) -> None:
        self.frames.append((run_id, "end", None))


class _Emitter:
    def __init__(self) -> None:
        self.events: list[tuple[SkillDesignActivityKind, dict[str, object], str | None]] = []

    async def append(
        self,
        kind: SkillDesignActivityKind,
        *,
        payload: dict[str, object] | None = None,
        source_event_id: str | None = None,
    ) -> None:
        self.events.append((kind, dict(payload or {}), source_event_id))


@pytest.mark.asyncio
async def test_stream_bridge_keeps_only_real_reasoning_and_safe_tool_lifecycle() -> None:
    raw = _Bridge()
    emitter = _Emitter()
    bridge = SkillBuilderActivityStreamBridge(raw, emitter)  # type: ignore[arg-type]

    await bridge.publish(
        "run-1",
        "messages",
        [
            {
                "type": "ai",
                "content": "visible answer is not activity reasoning",
                "additional_kwargs": {
                    "reasoning_content": "真实思考",
                    "provider_response": "must-not-project",
                },
                "tool_calls": [
                    {
                        "id": "call-1",
                        "name": "read_candidate_file",
                        "args": {"secret": "must-not-project"},
                    },
                    {"id": "call-2", "name": "bash", "args": {}},
                ],
            },
            {"langgraph_node": "agent"},
        ],
    )
    await bridge.publish(
        "run-1",
        "messages",
        [
            {
                "type": "tool",
                "tool_call_id": "call-1",
                "name": "read_candidate_file",
                "content": "must-not-project",
            },
            {},
        ],
    )

    assert raw.frames[0][1] == "messages"
    assert emitter.events == [
        (
            SkillDesignActivityKind.REASONING,
            {"text": "真实思考"},
            None,
        ),
        (
            SkillDesignActivityKind.TOOL_STARTED,
            {
                "tool_call_id": "call-1",
                "tool_name": "read_candidate_file",
            },
            "tool-started:call-1",
        ),
        (
            SkillDesignActivityKind.TOOL_COMPLETED,
            {
                "tool_call_id": "call-1",
                "tool_name": "read_candidate_file",
            },
            "tool-completed:call-1",
        ),
    ]
    assert "must-not-project" not in repr(emitter.events)


@pytest.mark.asyncio
async def test_stream_bridge_does_not_invent_reasoning() -> None:
    emitter = _Emitter()
    bridge = SkillBuilderActivityStreamBridge(_Bridge(), emitter)  # type: ignore[arg-type]

    await bridge.publish(
        "run-1",
        "messages",
        [{"type": "ai", "content": "普通回答"}, {}],
    )

    assert emitter.events == []
