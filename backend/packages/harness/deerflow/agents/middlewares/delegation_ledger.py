"""Deterministic capture and rendering for task delegations."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from html import escape
from typing import Any, cast

from langchain_core.messages import AIMessage, AnyMessage, ToolMessage

from deerflow.agents.thread_state import (
    TERMINAL_STATUSES,
    DelegationEntry,
    delegation_occurrence,
)
from deerflow.subagents.status_contract import (
    read_subagent_result_metadata,
)

_RESULT_BRIEF_CAP = 2000
_DESCRIPTION_CAP = 200
_LEDGER_RENDER_CHAR_BUDGET = 6000
_LEDGER_ENTRY_RESULT_RENDER_CAP = 120
_STATUS_ONLY_RESULT_BRIEFS = {
    "failed": "Task failed.",
    "cancelled": "Task cancelled by user.",
    "timed_out": "Task timed out.",
    "polling_timed_out": "Task polling timed out.",
}


def cancelled_delegation_updates(
    entries: list[DelegationEntry] | list[dict],
    *,
    project_id: str,
    owner_user_id: str,
    run_id: str,
) -> list[DelegationEntry]:
    """Return terminal updates for unfinished delegations in one exact Run."""

    updates: list[DelegationEntry] = []
    for entry in entries:
        if entry.get("project_id") == project_id and entry.get("owner_user_id") == owner_user_id and entry.get("run_id") == run_id and entry.get("status") == "in_progress":
            updates.append(cast(DelegationEntry, {**entry, "status": "cancelled"}))
    return updates


def stale_delegation_updates(
    entries: list[DelegationEntry] | list[dict],
    *,
    project_id: str,
    owner_user_id: str,
    current_run_id: str,
) -> list[DelegationEntry]:
    """Converge prior Run-scoped unfinished entries before a new Run's model."""

    updates: list[DelegationEntry] = []
    for entry in entries:
        entry_run_id = entry.get("run_id")
        if entry.get("project_id") == project_id and entry.get("owner_user_id") == owner_user_id and isinstance(entry_run_id, str) and entry_run_id and entry_run_id != current_run_id and entry.get("status") == "in_progress":
            updates.append(cast(DelegationEntry, {**entry, "status": "cancelled"}))
    return updates


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _bound_text(text: str, cap: int = _RESULT_BRIEF_CAP) -> str:
    """Deterministic head/tail truncation. This is not an LLM summary."""
    if len(text) <= cap:
        return text
    if cap <= 0:
        return ""
    head = cap * 2 // 3
    omitted_marker = "\n...\n"
    if cap <= len(omitted_marker):
        return text[:cap]
    tail = cap - head - len(omitted_marker)
    if tail <= 0:
        return text[:cap]
    return f"{text[:head]}{omitted_marker}{text[-tail:]}"


def _escape_context_text(value: object) -> str:
    return escape(" ".join(str(value).split()), quote=False)


def _status_guidance(status: str, stop_reason: str | None = None) -> str:
    if stop_reason:
        # A guardrail cap ended this run early (#3875 Phase 2): the status is
        # still completed/failed, and ``stop_reason`` carries *why* it stopped
        # (token_capped / turn_capped / loop_capped). The old contract surfaced
        # this as a separate ``max_turns_reached`` status; the additive
        # ``stop_reason`` field replaced it so v1 consumers keep working.
        if status == "completed":
            return "hit a guardrail cap with a partial result; reuse the partial result, retry with a tighter scope, or raise the per-agent budget (max_turns / token_budget)"
        return "hit a guardrail cap with no usable result; retry with a tighter scope or raise the per-agent budget (max_turns / token_budget)"
    if status == "in_progress":
        return "already delegated; do NOT delegate again; wait for or build on the result"
    if status == "completed":
        return "completed result; do NOT delegate again; reuse this result"
    if status == "failed":
        return "failed attempt; may retry with a changed plan"
    if status == "cancelled":
        return "cancelled attempt; may retry with a changed plan"
    if status == "timed_out":
        return "timed-out attempt; may retry with a changed plan"
    if status == "polling_timed_out":
        return "polling timed-out attempt; may retry with a changed plan"
    return "prior attempt; inspect status before retrying"


def _tool_call_name(tool_call: dict[str, Any]) -> str:
    name = tool_call.get("name")
    if isinstance(name, str):
        return name
    function = tool_call.get("function")
    if isinstance(function, dict) and isinstance(function.get("name"), str):
        return function["name"]
    return ""


def _tool_call_id(tool_call: dict[str, Any]) -> str | None:
    tool_call_id = tool_call.get("id")
    return str(tool_call_id) if tool_call_id else None


def _tool_call_args(tool_call: dict[str, Any]) -> dict[str, Any]:
    args = tool_call.get("args")
    return args if isinstance(args, dict) else {}


def _apply_tool_results(
    entries: list[DelegationEntry],
    messages: list[AnyMessage],
) -> None:
    """Pair ToolMessages to unfinished occurrences in provider order."""
    pending_by_id: dict[str, list[DelegationEntry]] = {}
    for entry in entries:
        if entry["status"] in TERMINAL_STATUSES:
            continue
        pending_by_id.setdefault(entry["id"], []).append(entry)

    next_result_index: dict[str, int] = {}
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        tool_call_id = str(message.tool_call_id) if message.tool_call_id else ""
        pending = pending_by_id.get(tool_call_id)
        if not pending:
            continue
        result_index = next_result_index.get(tool_call_id, 0)
        if result_index >= len(pending):
            continue
        entry = pending[result_index]
        # A result consumes its provider-order occurrence even when it is not a
        # structured subagent terminal result.
        next_result_index[tool_call_id] = result_index + 1
        structured = read_subagent_result_metadata(message.additional_kwargs)
        if structured is None:
            continue
        entry["status"] = structured["status"]
        stop_reason = structured.get("stop_reason")
        if stop_reason:
            entry["stop_reason"] = stop_reason
        result_text = structured.get("result_brief") or structured.get("error") or _STATUS_ONLY_RESULT_BRIEFS.get(structured["status"])
        if result_text:
            result_sha256 = structured.get("result_sha256") or hashlib.sha256(result_text.encode("utf-8")).hexdigest()
            entry.update(
                {
                    "result_brief": _bound_text(result_text),
                    "result_sha256": result_sha256,
                    "result_ref": str(message.id or tool_call_id),
                }
            )


def extract_delegations(
    messages: list[AnyMessage],
    *,
    prior_entries: list[DelegationEntry] | None = None,
) -> list[DelegationEntry]:
    """Enumerate task dispatches and pair results by provider-call occurrence.

    ``prior_entries`` lets a resumed Run apply newly appended ToolMessages to
    in-progress delegations whose AI dispatch lives before the pre-Run message
    boundary.
    """
    entries = [cast(DelegationEntry, dict(entry)) for entry in (prior_entries or [])]
    next_occurrence_by_id: dict[str, int] = {}
    for entry in entries:
        next_occurrence_by_id[entry["id"]] = max(
            next_occurrence_by_id.get(entry["id"], 0),
            delegation_occurrence(entry),
        )

    dispatches: list[tuple[str, dict[str, Any], str | None]] = []
    dispatch_count_by_id: dict[str, int] = {}
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        message_id = str(message.id) if message.id else None
        for tool_call_index, tool_call in enumerate(message.tool_calls or []):
            if _tool_call_name(tool_call) != "task":
                continue
            tool_call_id = _tool_call_id(tool_call)
            if tool_call_id is None:
                continue
            dispatch_ref = f"{message_id}:{tool_call_index}" if message_id is not None else None
            dispatches.append((tool_call_id, _tool_call_args(tool_call), dispatch_ref))
            dispatch_count_by_id[tool_call_id] = dispatch_count_by_id.get(tool_call_id, 0) + 1

    now = _utc_now_iso()
    for tool_call_id, args, dispatch_ref in dispatches:
        if dispatch_ref is not None and any(entry["id"] == tool_call_id and entry.get("dispatch_ref") == dispatch_ref for entry in entries):
            continue
        occurrence = next_occurrence_by_id.get(tool_call_id, 0) + 1
        next_occurrence_by_id[tool_call_id] = occurrence
        entry: DelegationEntry = {
            "id": tool_call_id,
            "description": str(args.get("description") or args.get("prompt") or "")[:_DESCRIPTION_CAP],
            "subagent_type": str(args.get("subagent_type") or ""),
            "status": "in_progress",
            "created_at": now,
        }
        if occurrence > 1 or dispatch_count_by_id[tool_call_id] > 1:
            entry["occurrence"] = occurrence
        if dispatch_ref is not None:
            entry["dispatch_ref"] = dispatch_ref
        entries.append(entry)

    _apply_tool_results(entries, messages)
    return entries


def _fits_budget(lines: list[str], candidate: str, max_chars: int) -> bool:
    return len("\n".join([*lines, candidate])) <= max_chars


def _render_entry_line(entry: DelegationEntry) -> str:
    status = _escape_context_text(entry["status"])
    description = _escape_context_text(entry["description"])
    subagent_type = _escape_context_text(entry["subagent_type"])
    guidance = _status_guidance(entry["status"], entry.get("stop_reason"))
    line = f"- [{status}] {description} (via {subagent_type}; {guidance})"
    result_brief = entry.get("result_brief")
    if result_brief:
        line += f" -> {_escape_context_text(_bound_text(result_brief, _LEDGER_ENTRY_RESULT_RENDER_CAP))}"
    return line


def render_delegation_ledger(entries: list[DelegationEntry], *, max_chars: int = _LEDGER_RENDER_CHAR_BUDGET) -> str:
    """Render the delegation ledger as model-visible system context."""
    if not entries:
        return ""

    lines = [
        "## Work already delegated",
        "Newest entries are shown first. In-progress entries are already delegated. Completed entries are reusable results. Failed, cancelled, or timed-out entries are prior attempts.",
    ]
    omitted = 0
    for index, entry in enumerate(reversed(entries)):
        line = _render_entry_line(entry)
        if _fits_budget(lines, line, max_chars):
            lines.append(line)
            continue
        omitted = len(entries) - index
        break

    if omitted:
        omitted_line = f"- ... {omitted} older delegation entries omitted from this model view because of context budget"
        while len(lines) > 1 and not _fits_budget(lines, omitted_line, max_chars):
            lines.pop()
            omitted += 1
            omitted_line = f"- ... {omitted} older delegation entries omitted from this model view because of context budget"
        if _fits_budget(lines, omitted_line, max_chars):
            lines.append(omitted_line)

    rendered = "\n".join(lines)
    if len(rendered) <= max_chars:
        return rendered
    return rendered[: max(0, max_chars - 4)] + "\n..."
