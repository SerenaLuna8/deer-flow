"""Read-only UI projections for scoped private conversation messages."""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any


def compute_run_durations(records: Iterable[object]) -> dict[str, int]:
    """Return bounded whole wall-clock seconds keyed by scoped Run id."""

    durations: dict[str, int] = {}
    for record in records:
        run_id = getattr(record, "run_id", None)
        status = getattr(record, "status", None)
        created_at = getattr(record, "created_at", None)
        updated_at = getattr(record, "updated_at", None)
        if not isinstance(run_id, str) or not run_id or status != "success" or not isinstance(created_at, datetime) or not isinstance(updated_at, datetime):
            continue
        durations[run_id] = max(0, int((updated_at - created_at).total_seconds()))
    return durations


def _visible_ai_payload(message: Mapping[str, Any]) -> bool:
    if message.get("type") not in {"ai", "assistant"}:
        return False
    additional_kwargs = message.get("additional_kwargs")
    return not (isinstance(additional_kwargs, Mapping) and additional_kwargs.get("hide_from_ui") is True)


def _stamp_payload(payload: dict[str, Any], duration: int) -> None:
    additional_kwargs = payload.get("additional_kwargs")
    projected_kwargs = dict(additional_kwargs) if isinstance(additional_kwargs, Mapping) else {}
    projected_kwargs["turn_duration"] = duration
    payload["additional_kwargs"] = projected_kwargs


def project_event_message_durations(
    rows: list[dict[str, Any]],
    *,
    run_durations: Mapping[str, int],
    last_visible_ai_seq_by_run: Mapping[str, int],
) -> list[dict[str, Any]]:
    """Copy event rows and stamp only each Run's authoritative final AI row."""

    projected = copy.deepcopy(rows)
    for row in projected:
        run_id = row.get("run_id")
        seq = row.get("seq")
        duration = run_durations.get(run_id) if isinstance(run_id, str) else None
        if type(seq) is not int or type(duration) is not int or last_visible_ai_seq_by_run.get(run_id) != seq:
            continue
        metadata = row.get("metadata")
        caller = metadata.get("caller") if isinstance(metadata, Mapping) else None
        if isinstance(caller, str) and caller.startswith(("middleware:", "subagent:")):
            continue
        payload = row.get("content")
        if isinstance(payload, dict) and _visible_ai_payload(payload):
            _stamp_payload(payload, duration)
    return projected


def project_checkpoint_message_durations(
    messages: list[dict[str, Any]],
    *,
    run_durations: Mapping[str, int],
) -> list[dict[str, Any]]:
    """Copy checkpoint messages and stamp the final visible AI in each turn."""

    projected = copy.deepcopy(messages)
    current_run_id: str | None = None
    final_ai_by_run: dict[str, dict[str, Any]] = {}
    for message in projected:
        message_type = message.get("type")
        if message_type in {"human", "user"}:
            additional_kwargs = message.get("additional_kwargs")
            run_id = additional_kwargs.get("run_id") if isinstance(additional_kwargs, Mapping) else None
            current_run_id = run_id if isinstance(run_id, str) and run_id else None
            continue
        if current_run_id is not None and _visible_ai_payload(message) and current_run_id in run_durations:
            final_ai_by_run[current_run_id] = message

    for run_id, message in final_ai_by_run.items():
        _stamp_payload(message, run_durations[run_id])
    return projected


__all__ = [
    "compute_run_durations",
    "project_checkpoint_message_durations",
    "project_event_message_durations",
]
