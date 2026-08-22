"""Middleware for logging token usage and annotating step attribution."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Mapping
from typing import Any, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.todo import Todo
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.runtime import Runtime

from deerflow.subagents.status_contract import (
    SUBAGENT_USAGE_RECEIPT_STATE_KEY,
    SUBAGENT_USAGE_RECEIPT_STATE_VERSION,
    normalize_token_usage,
    read_subagent_usage_receipt,
    read_subagent_usage_receipt_state,
)

logger = logging.getLogger(__name__)

TOKEN_USAGE_ATTRIBUTION_KEY = "token_usage_attribution"


def _string_arg(value: Any) -> str | None:
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None
    return None


def _normalize_todos(value: Any) -> list[Todo]:
    if not isinstance(value, list):
        return []

    normalized: list[Todo] = []
    for item in value:
        if not isinstance(item, dict):
            continue

        todo: Todo = {}
        content = _string_arg(item.get("content"))
        status = item.get("status")

        if content is not None:
            todo["content"] = content
        if status in {"pending", "in_progress", "completed"}:
            todo["status"] = status

        normalized.append(todo)

    return normalized


def _todo_action_kind(previous: Todo | None, current: Todo) -> str:
    status = current.get("status")
    previous_content = previous.get("content") if previous else None
    current_content = current.get("content")

    if previous is None:
        if status == "completed":
            return "todo_complete"
        if status == "in_progress":
            return "todo_start"
        return "todo_update"

    if previous_content != current_content:
        return "todo_update"

    if status == "completed":
        return "todo_complete"
    if status == "in_progress":
        return "todo_start"
    return "todo_update"


def _build_todo_actions(previous_todos: list[Todo], next_todos: list[Todo]) -> list[dict[str, Any]]:
    # This is the single source of truth for precise write_todos token
    # attribution. The frontend intentionally falls back to a generic
    # "Update to-do list" label when this metadata is missing or malformed.
    previous_by_content: dict[str, list[tuple[int, Todo]]] = defaultdict(list)
    matched_previous_indices: set[int] = set()

    for index, todo in enumerate(previous_todos):
        content = todo.get("content")
        if isinstance(content, str) and content:
            previous_by_content[content].append((index, todo))

    actions: list[dict[str, Any]] = []

    for index, todo in enumerate(next_todos):
        content = todo.get("content")
        if not isinstance(content, str) or not content:
            continue

        previous_match: Todo | None = None
        content_matches = previous_by_content.get(content)
        if content_matches:
            while content_matches and content_matches[0][0] in matched_previous_indices:
                content_matches.pop(0)
            if content_matches:
                previous_index, previous_match = content_matches.pop(0)
                matched_previous_indices.add(previous_index)

        if previous_match is None and content not in previous_by_content and index < len(previous_todos) and index not in matched_previous_indices:
            previous_match = previous_todos[index]
            matched_previous_indices.add(index)

        if previous_match is not None:
            previous_content = previous_match.get("content")
            previous_status = previous_match.get("status")
            if previous_content == content and previous_status == todo.get("status"):
                continue

        actions.append(
            {
                "kind": _todo_action_kind(previous_match, todo),
                "content": content,
            }
        )

    for index, todo in enumerate(previous_todos):
        if index in matched_previous_indices:
            continue

        content = todo.get("content")
        if not isinstance(content, str) or not content:
            continue

        actions.append(
            {
                "kind": "todo_remove",
                "content": content,
            }
        )

    return actions


def _describe_tool_call(tool_call: dict[str, Any], todos: list[Todo]) -> list[dict[str, Any]]:
    name = _string_arg(tool_call.get("name")) or "unknown"
    args = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}
    tool_call_id = _string_arg(tool_call.get("id"))

    if name == "write_todos":
        next_todos = _normalize_todos(args.get("todos"))
        actions = _build_todo_actions(todos, next_todos)
        if not actions:
            return [
                {
                    "kind": "tool",
                    "tool_name": name,
                    "tool_call_id": tool_call_id,
                }
            ]
        return [
            {
                **action,
                "tool_call_id": tool_call_id,
            }
            for action in actions
        ]

    if name == "task":
        return [
            {
                "kind": "subagent",
                "description": _string_arg(args.get("description")),
                "subagent_type": _string_arg(args.get("subagent_type")),
                "tool_call_id": tool_call_id,
            }
        ]

    if name in {"web_search", "image_search"}:
        query = _string_arg(args.get("query"))
        return [
            {
                "kind": "search",
                "tool_name": name,
                "query": query,
                "tool_call_id": tool_call_id,
            }
        ]

    if name == "present_files":
        return [
            {
                "kind": "present_files",
                "tool_call_id": tool_call_id,
            }
        ]

    if name == "ask_clarification":
        return [
            {
                "kind": "clarification",
                "tool_call_id": tool_call_id,
            }
        ]

    return [
        {
            "kind": "tool",
            "tool_name": name,
            "description": _string_arg(args.get("description")),
            "tool_call_id": tool_call_id,
        }
    ]


def _infer_step_kind(message: AIMessage, actions: list[dict[str, Any]]) -> str:
    if actions:
        first_kind = actions[0].get("kind")
        if len(actions) == 1 and first_kind in {"todo_start", "todo_complete", "todo_update", "todo_remove"}:
            return "todo_update"
        if len(actions) == 1 and first_kind == "subagent":
            return "subagent_dispatch"
        return "tool_batch"

    if message.content:
        return "final_answer"
    return "thinking"


def _has_tool_call(message: AIMessage, tool_call_id: str) -> bool:
    """Return True if the AIMessage contains a tool_call with the given id."""
    for tc in message.tool_calls or []:
        if isinstance(tc, dict):
            if tc.get("id") == tool_call_id:
                return True
        elif hasattr(tc, "id") and tc.id == tool_call_id:
            return True
    return False


def _set_subagent_usage_receipts(
    message: AIMessage,
    *,
    receipts: Mapping[str, dict[str, int]],
    conflicts: frozenset[str],
) -> AIMessage:
    state = read_subagent_usage_receipt_state(message.additional_kwargs)
    if state is None:
        if SUBAGENT_USAGE_RECEIPT_STATE_KEY in message.additional_kwargs:
            logger.warning("Ignoring subagent token usage because its persisted receipt state is malformed")
            return message
        if message.usage_metadata is None:
            baseline = {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            }
        else:
            baseline = normalize_token_usage(message.usage_metadata)
            if baseline is None:
                logger.warning("Ignoring subagent token usage because parent usage metadata cannot form a durable baseline")
                return message
        if not receipts and not conflicts:
            return message
    else:
        baseline, _persisted_contributions, _persisted_conflicts = state

    if set(receipts) & conflicts:
        raise ValueError("Sub-Agent Task receipt cannot be both accepted and conflicted")

    contributions = {receipt_id: dict(receipts[receipt_id]) for receipt_id in sorted(receipts)}

    merged_usage = dict(message.usage_metadata or {})
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        merged_usage[key] = baseline[key] + sum(contribution[key] for contribution in contributions.values())

    receipt_state_payload = {
        "version": SUBAGENT_USAGE_RECEIPT_STATE_VERSION,
        "baseline": baseline,
        "contributions": [
            {
                "receipt_id": applied_receipt_id,
                "usage": contributions[applied_receipt_id],
            }
            for applied_receipt_id in sorted(contributions)
        ],
    }
    if conflicts:
        receipt_state_payload["conflicts"] = sorted(conflicts)
    if merged_usage == dict(message.usage_metadata or {}) and message.additional_kwargs.get(SUBAGENT_USAGE_RECEIPT_STATE_KEY) == receipt_state_payload:
        return message

    additional_kwargs = dict(message.additional_kwargs or {})
    additional_kwargs[SUBAGENT_USAGE_RECEIPT_STATE_KEY] = receipt_state_payload
    return message.model_copy(
        update={
            "usage_metadata": merged_usage,
            "additional_kwargs": additional_kwargs,
        }
    )


def _build_attribution(message: AIMessage, todos: list[Todo]) -> dict[str, Any]:
    tool_calls = getattr(message, "tool_calls", None) or []
    actions: list[dict[str, Any]] = []
    current_todos = list(todos)

    for raw_tool_call in tool_calls:
        if not isinstance(raw_tool_call, dict):
            continue

        described_actions = _describe_tool_call(raw_tool_call, current_todos)
        actions.extend(described_actions)

        if raw_tool_call.get("name") == "write_todos":
            args = raw_tool_call.get("args") if isinstance(raw_tool_call.get("args"), dict) else {}
            current_todos = _normalize_todos(args.get("todos"))

    tool_call_ids: list[str] = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue

        tool_call_id = _string_arg(tool_call.get("id"))
        if tool_call_id is not None:
            tool_call_ids.append(tool_call_id)

    return {
        # Schema changes should remain additive where possible so older
        # frontends can ignore unknown fields and fall back safely.
        "version": 1,
        "kind": _infer_step_kind(message, actions),
        "shared_attribution": len(actions) > 1,
        "tool_call_ids": tool_call_ids,
        "actions": actions,
    }


class TokenUsageMiddleware(AgentMiddleware):
    """Logs token usage from model responses and annotates the AI step."""

    def _apply(self, state: AgentState) -> dict | None:
        messages = state.get("messages", [])
        if not messages:
            return None

        # Annotate Sub-Agent Task token usage onto the AIMessage that dispatched
        # it. Durable receipt metadata is authoritative; its immutable baseline
        # and per-receipt contributions make checkpoint replay deterministic.
        # Legacy ToolMessages without receipts are intentionally ignored: a
        # process cache cannot be reconstructed during checkpoint replay.
        # Reduce the whole transcript so receipt identity is global, not scoped
        # to one dispatch or one hook invocation. The earliest dispatch owns a
        # repeated same-value receipt; conflicting values remove that receipt
        # from every dispatch deterministically.
        state_updates: dict[int, AIMessage] = {}
        if len(messages) >= 2:
            occurrences: dict[
                str,
                dict[tuple[int, int, int, int], dict[str, int]],
            ] = {}
            conflict_dispatches: dict[str, set[int]] = {}
            state_dispatches: set[int] = set()

            def record_occurrence(
                receipt_id: str,
                usage: dict[str, int],
                dispatch_idx: int,
            ) -> None:
                signature = (
                    dispatch_idx,
                    usage["input_tokens"],
                    usage["output_tokens"],
                    usage["total_tokens"],
                )
                occurrences.setdefault(receipt_id, {})[signature] = usage

            for dispatch_idx, candidate in enumerate(messages):
                if not isinstance(candidate, AIMessage):
                    continue
                receipt_state = read_subagent_usage_receipt_state(
                    candidate.additional_kwargs,
                )
                if receipt_state is not None:
                    state_dispatches.add(dispatch_idx)
                    _baseline, contributions, conflicts = receipt_state
                    for receipt_id, usage in contributions.items():
                        record_occurrence(receipt_id, usage, dispatch_idx)
                    for receipt_id in conflicts:
                        conflict_dispatches.setdefault(receipt_id, set()).add(
                            dispatch_idx,
                        )
                elif SUBAGENT_USAGE_RECEIPT_STATE_KEY in candidate.additional_kwargs:
                    logger.warning(
                        "Ignoring malformed persisted Sub-Agent Task receipt state: message_id=%s",
                        candidate.id,
                    )

            for tool_idx, tool_msg in enumerate(messages):
                if not isinstance(tool_msg, ToolMessage) or not tool_msg.tool_call_id:
                    continue
                usage_receipt = read_subagent_usage_receipt(
                    tool_msg.additional_kwargs,
                )
                if usage_receipt is None:
                    continue
                dispatch_idx = tool_idx - 1
                while dispatch_idx >= 0:
                    candidate = messages[dispatch_idx]
                    if isinstance(candidate, AIMessage) and _has_tool_call(
                        candidate,
                        tool_msg.tool_call_id,
                    ):
                        receipt_id, subagent_usage = usage_receipt
                        record_occurrence(
                            receipt_id,
                            subagent_usage,
                            dispatch_idx,
                        )
                        break
                    dispatch_idx -= 1

            desired_by_dispatch: dict[int, dict[str, dict[str, int]]] = {}
            conflicts_by_dispatch: dict[int, set[str]] = {}
            for receipt_id in sorted(occurrences.keys() | conflict_dispatches.keys()):
                receipt_occurrences = occurrences.get(receipt_id, {})
                usage_values = {signature[1:] for signature in receipt_occurrences}
                owner_candidates = {signature[0] for signature in receipt_occurrences} | conflict_dispatches.get(receipt_id, set())
                if not owner_candidates:
                    continue
                if receipt_id in conflict_dispatches or len(usage_values) != 1:
                    logger.warning(
                        "Ignoring conflicting Sub-Agent Task usage receipt transcript: receipt_id=%s",
                        receipt_id,
                    )
                    for owner_dispatch in owner_candidates:
                        conflicts_by_dispatch.setdefault(owner_dispatch, set()).add(
                            receipt_id,
                        )
                    continue
                owner_dispatch = min(owner_candidates)
                usage = next(iter(receipt_occurrences.values()))
                desired_by_dispatch.setdefault(owner_dispatch, {})[receipt_id] = usage

            for dispatch_idx in sorted(
                state_dispatches | desired_by_dispatch.keys() | conflicts_by_dispatch.keys(),
            ):
                candidate = messages[dispatch_idx]
                if not isinstance(candidate, AIMessage):
                    continue
                updated = _set_subagent_usage_receipts(
                    candidate,
                    receipts=desired_by_dispatch.get(dispatch_idx, {}),
                    conflicts=frozenset(
                        conflicts_by_dispatch.get(dispatch_idx, set()),
                    ),
                )
                if updated is not candidate:
                    state_updates[dispatch_idx] = updated

        last = messages[-1]
        if not isinstance(last, AIMessage):
            if state_updates:
                return {"messages": [state_updates[idx] for idx in sorted(state_updates)]}
            return None

        usage = getattr(last, "usage_metadata", None)
        if usage:
            input_token_details = usage.get("input_token_details") or {}
            output_token_details = usage.get("output_token_details") or {}
            detail_parts = []
            if input_token_details:
                detail_parts.append(f"input_token_details={input_token_details}")
            if output_token_details:
                detail_parts.append(f"output_token_details={output_token_details}")
            detail_suffix = f" {' '.join(detail_parts)}" if detail_parts else ""
            logger.info(
                "LLM token usage: input=%s output=%s total=%s%s",
                usage.get("input_tokens", "?"),
                usage.get("output_tokens", "?"),
                usage.get("total_tokens", "?"),
                detail_suffix,
            )

        todos = state.get("todos") or []
        attribution = _build_attribution(last, todos if isinstance(todos, list) else [])
        last_index = len(messages) - 1
        last_for_update = state_updates.get(last_index, last)
        additional_kwargs = dict(getattr(last_for_update, "additional_kwargs", {}) or {})

        if additional_kwargs.get(TOKEN_USAGE_ATTRIBUTION_KEY) == attribution:
            return {"messages": [state_updates[idx] for idx in sorted(state_updates)]} if state_updates else None

        additional_kwargs[TOKEN_USAGE_ATTRIBUTION_KEY] = attribution
        updated_msg = last_for_update.model_copy(update={"additional_kwargs": additional_kwargs})
        state_updates[last_index] = updated_msg
        return {"messages": [state_updates[idx] for idx in sorted(state_updates)]}

    @override
    def after_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._apply(state)

    @override
    async def aafter_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._apply(state)

    @override
    def after_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._apply(state)

    @override
    async def aafter_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._apply(state)
