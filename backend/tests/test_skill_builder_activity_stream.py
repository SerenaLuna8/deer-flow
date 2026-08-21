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
                        "args": {
                            "path": "references/guide.md",
                            "secret": "must-not-project",
                        },
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
                "content": ('{"path":"references/guide.md","file_size_bytes":42,"content":"must-not-project"}'),
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
                "path": "references/guide.md",
            },
            "tool-started:call-1",
        ),
        (
            SkillDesignActivityKind.TOOL_COMPLETED,
            {
                "tool_call_id": "call-1",
                "tool_name": "read_candidate_file",
                "path": "references/guide.md",
                "size_bytes": 42,
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


@pytest.mark.asyncio
async def test_stream_bridge_never_projects_catalog_references() -> None:
    emitter = _Emitter()
    bridge = SkillBuilderActivityStreamBridge(_Bridge(), emitter)  # type: ignore[arg-type]

    await bridge.publish(
        "run-1",
        "messages",
        [
            {
                "type": "ai",
                "tool_calls": [
                    {
                        "id": "read-1",
                        "name": "read_skill_version",
                        "args": {"reference": "skill:project:private-name:v4"},
                    },
                    {
                        "id": "inspect-1",
                        "name": "inspect_mcp_tool",
                        "args": {"reference": "mcp:project:private-server:v2:secret"},
                    },
                ],
            },
            {},
        ],
    )
    await bridge.publish(
        "run-1",
        "messages",
        [
            {
                "type": "tool",
                "tool_call_id": "read-1",
                "name": "read_skill_version",
                "status": "failed",
                "content": "must-not-project",
            },
            {},
        ],
    )
    await bridge.publish(
        "run-1",
        "messages",
        [
            {
                "type": "tool",
                "tool_call_id": "inspect-1",
                "name": "inspect_mcp_tool",
                "content": ('{"reference":"mcp:project:private-server:v2:secret","server_name":"Public Server","tool_name":"Public Tool"}'),
            },
            {},
        ],
    )

    assert emitter.events == [
        (
            SkillDesignActivityKind.TOOL_STARTED,
            {"tool_call_id": "read-1", "tool_name": "read_skill_version"},
            "tool-started:read-1",
        ),
        (
            SkillDesignActivityKind.TOOL_STARTED,
            {"tool_call_id": "inspect-1", "tool_name": "inspect_mcp_tool"},
            "tool-started:inspect-1",
        ),
        (
            SkillDesignActivityKind.TOOL_FAILED,
            {"tool_call_id": "read-1", "tool_name": "read_skill_version"},
            "tool-failed:read-1",
        ),
        (
            SkillDesignActivityKind.TOOL_COMPLETED,
            {
                "tool_call_id": "inspect-1",
                "tool_name": "inspect_mcp_tool",
                "resource_name": "Public Server / Public Tool",
            },
            "tool-completed:inspect-1",
        ),
    ]
    assert "private" not in repr(emitter.events)
