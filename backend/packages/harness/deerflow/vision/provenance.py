"""Identity-only provenance marker for canonical Vision Bridge tools."""

from __future__ import annotations

_VISION_TOOL_MARKER = object()


def mark_vision_evidence_tool(tool: object) -> object:
    for attribute in ("coroutine", "func"):
        implementation = getattr(tool, attribute, None)
        if callable(implementation):
            setattr(
                implementation,
                "__deerflow_vision_evidence_tool__",
                _VISION_TOOL_MARKER,
            )
            return tool
    raise TypeError("Vision evidence tools require a registered callable")


def is_vision_evidence_tool(tool: object) -> bool:
    return any(
        getattr(
            getattr(tool, attribute, None),
            "__deerflow_vision_evidence_tool__",
            None,
        )
        is _VISION_TOOL_MARKER
        for attribute in ("coroutine", "func")
    )


__all__ = ["is_vision_evidence_tool", "mark_vision_evidence_tool"]
