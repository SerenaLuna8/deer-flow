"""Run event capture via LangChain callbacks.

RunJournal sits between LangChain's callback mechanism and the pluggable
RunEventStore. It standardizes callback data into RunEvent records and
handles token usage accumulation.

Key design decisions:
- on_llm_new_token observes lead-Agent reasoning/output boundaries; complete
  messages are still persisted only via on_llm_end
- on_chat_model_start captures structured prompts as llm_request (OpenAI format) and
  extracts the first human message for run.input, because it is more reliable than
  on_chain_start (fires on every node) — messages here are fully structured.
- on_chain_start with parent_run_id=None emits a run.start trace marking root invocation.
- on_llm_end emits llm_response in OpenAI Chat Completions format
- Token usage accumulated in memory, written to RunRow on run completion
- Caller identification via tags injection (lead_agent / subagent:{name} / middleware:{name})
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from deerflow.agents.human_input import read_human_input_response
from deerflow.agents.middlewares.subagent_limit_middleware import (
    SUBAGENT_LIMIT_EVENT_REASON,
    SubagentLimitObservation,
)
from deerflow.agents.middlewares.token_budget_middleware import (
    TOKEN_BUDGET_STATUS_KEY,
    read_token_budget_status,
)
from deerflow.agents.middlewares.tool_call_control import (
    TOOL_CALL_CONTROL_LOOP_REPLACEMENT_KEY,
    RepeatedCallObservation,
    ToolCallBudgetObservation,
    ToolCallControlObservation,
)
from deerflow.public_error_codes import (
    LLM_PUBLIC_ERROR_CODES,
    llm_error_code_for_reason,
)
from deerflow.runtime.recovered_llm_failures import (
    RECOVERED_LLM_FAILURES_KEY,
    RunRecoveredLLMFailureRecorder,
    build_recovered_llm_failures_receipt,
    read_recovered_llm_failures,
)
from deerflow.runtime.runs.execution_contracts import RunSemanticStopRecorder
from deerflow.utils.messages import message_to_text

if TYPE_CHECKING:
    from deerflow.runtime.events.store.base import RunEventStore
    from deerflow.runtime.private_scope import PrivateResourceScope

logger = logging.getLogger(__name__)

_LEGACY_SUMMARY_MESSAGE_NAME = "summary"
_RECONCILED_TOOL_MESSAGE_NAMES = frozenset({"ask_clarification"})
_PERSISTED_HIDDEN_HUMAN_INPUT_RESPONSE_SOURCES = frozenset({"ask_clarification"})
_REASONING_DURATION_KEY = "reasoning_duration_ms"
_MAX_REASONING_DURATION_MS = 24 * 60 * 60 * 1000
_SAFE_EXCEPTION_CLASS_MAX_LENGTH = 96
_SAFE_TOOL_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_TOOL_CALL_CONTROL_DISPOSITIONS = frozenset(
    {
        "advisory",
        "tool_free_finalization",
        "truncate_tool_calls",
        "exhaust_tool",
    }
)
_REPEATED_CALL_REASON_CODES = frozenset(
    {
        "repeated_call_warning",
        "repeated_call_limit",
    }
)
_TOOL_CALL_BUDGET_REASON_CODES = frozenset(
    {
        "tool_budget_warning",
        "tool_budget_exhausted",
    }
)


@dataclass
class _ReasoningWindow:
    """Server-observed reasoning interval for one LangChain model call."""

    started_at: float | None = None
    ended_at: float | None = None
    inline_buffer: str = ""
    inline_think_open: bool = False


def _should_persist_human_input_message(message: BaseMessage) -> bool:
    if not isinstance(message, HumanMessage):
        return False
    if message.name == _LEGACY_SUMMARY_MESSAGE_NAME:
        return False
    if message.additional_kwargs.get("hide_from_ui") is not True:
        return True
    response = read_human_input_response(message.additional_kwargs)
    return response is not None and response["source"] in _PERSISTED_HIDDEN_HUMAN_INPUT_RESPONSE_SOURCES


def _safe_exception_class(error: BaseException) -> str:
    """Return a bounded classifier without persisting exception text."""

    name = type(error).__name__
    if not name or len(name) > _SAFE_EXCEPTION_CLASS_MAX_LENGTH or not name.replace("_", "").isalnum():
        return "Exception"
    return name


class RunJournal(BaseCallbackHandler):
    """LangChain callback handler that captures events to RunEventStore."""

    def __init__(
        self,
        run_id: str,
        thread_id: str,
        event_store: RunEventStore,
        *,
        track_token_usage: bool = True,
        flush_threshold: int = 20,
        progress_reporter: Callable[[dict], Awaitable[None]] | None = None,
        progress_flush_interval: float = 5.0,
        scope: PrivateResourceScope | None = None,
        recovered_llm_failure_recorder: RunRecoveredLLMFailureRecorder | None = None,
        semantic_stop_recorder: RunSemanticStopRecorder | None = None,
    ):
        super().__init__()
        self.run_id = run_id
        self.thread_id = thread_id
        self._store = event_store
        self._track_tokens = track_token_usage
        self._flush_threshold = flush_threshold
        self._progress_reporter = progress_reporter
        self._progress_flush_interval = progress_flush_interval
        self._scope = scope
        self._recovered_llm_failure_recorder = recovered_llm_failure_recorder or RunRecoveredLLMFailureRecorder()
        self._semantic_stop_recorder = semantic_stop_recorder

        # Write buffer
        self._buffer: list[dict] = []
        self._pending_flush_tasks: set[asyncio.Task[None]] = set()
        self._pending_progress_task: asyncio.Task[None] | None = None
        self._pending_progress_delayed = False
        self._progress_dirty = False
        self._last_progress_flush = 0.0

        # Token accumulators
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._total_tokens = 0
        self._llm_call_count = 0

        # Caller-bucketed token accumulators
        self._lead_agent_tokens = 0
        self._subagent_tokens = 0
        self._middleware_tokens = 0

        # Per-model token accumulator
        self._tokens_by_model: dict[str, dict[str, int]] = {}

        # Dedup: LangChain may fire on_llm_end multiple times for the same run_id
        self._counted_llm_run_ids: set[str] = set()
        self._counted_external_source_ids: set[str] = set()
        self._counted_message_llm_run_ids: set[str] = set()

        # Convenience fields
        self._last_ai_msg: str | None = None
        self._first_human_msg: str | None = None
        self._msg_count = 0
        self._had_llm_error_fallback = False
        self._llm_error_fallback_message: str | None = None
        self._llm_error_fallback_code: str | None = None

        # Latency tracking
        self._llm_start_times: dict[str, float] = {}  # langchain run_id -> start time
        self._reasoning_windows: dict[str, _ReasoningWindow] = {}
        self._finalized_reasoning_durations: dict[str, int] = {}

        # LLM request/response tracking
        self._llm_call_index = 0
        self._seen_llm_starts: set[str] = set()  # langchain run_ids that fired on_chat_model_start
        self._current_run_tool_call_names: dict[str, str] = {}
        self._tool_call_callers: dict[str, str] = {}
        self._tool_run_callers: dict[str, str] = {}
        self._persisted_tool_message_identities: set[str] = set()
        self._persisted_recovered_llm_failure_count = 0
        self._latest_lead_ai_observation_by_id: dict[
            str,
            tuple[str, int],
        ] = {}
        self._lead_tool_call_messages_by_id: dict[str, AIMessage] = {}
        self._reconciled_recovered_llm_observations: set[tuple[str, str, int]] = set()
        self._reconciled_loop_safety_message_ids: set[str] = set()
        self._tool_call_control_observation_ids: set[str] = set()
        self._subagent_limit_observation_ids: set[str] = set()

    # -- Lifecycle callbacks --

    @staticmethod
    def _message_text(message: BaseMessage) -> str:
        """Extract displayable text from a message's mixed content shape."""
        return message_to_text(message, text_attribute_fallback=True)

    def _record_message_summary(self, message: BaseMessage, *, caller: str | None = None) -> None:
        """Update run-level convenience fields for persisted run rows."""
        if caller not in (None, "lead_agent"):
            return
        self._msg_count += 1

        # ``last_ai_message`` should represent the lead agent's user-facing
        # answer. Middleware/subagent model calls and empty tool-call-only
        # AI messages must not overwrite the last useful assistant text.
        is_ai_message = isinstance(message, AIMessage) or getattr(message, "type", None) == "ai"
        if is_ai_message and (caller is None or caller == "lead_agent"):
            text = self._message_text(message).strip()
            if text:
                self._last_ai_msg = text[:2000]

    def on_chain_start(
        self,
        serialized: dict[str, Any],
        inputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        caller = self._identify_caller(tags)
        if parent_run_id is None:
            # Root graph invocation — emit a single trace event for the run start.
            chain_name = (serialized or {}).get("name", "unknown")
            self._put(
                event_type="run.start",
                category="trace",
                content={"chain": chain_name},
                metadata={"caller": caller, **(metadata or {})},
            )

    def on_chain_end(
        self,
        outputs: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        # Nested chain ends fire for internal graph nodes; only the root chain
        # represents the user-visible run lifecycle.
        if parent_run_id is not None:
            return
        self._reconcile_final_error_fallback(outputs)
        self._reconcile_suppressed_loop_proposals()
        self._reconcile_final_recovered_llm_failures(outputs)
        self._reconcile_final_token_budget_message(outputs)
        self._reconcile_final_tool_messages(outputs)
        terminal_status = "error" if self._had_llm_error_fallback or (self._semantic_stop_recorder is not None and self._semantic_stop_recorder.reason == "loop_capped") else "success"
        self._put(
            event_type="run.end",
            category="outputs",
            content=outputs,
            metadata={"status": terminal_status},
        )
        self._flush_sync()

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        if parent_run_id is not None:
            return
        self._reconcile_suppressed_loop_proposals()
        self._put(
            event_type="run.error",
            category="error",
            content="Run execution failed",
            metadata={"error_type": _safe_exception_class(error)},
        )
        self._flush_sync()

    # -- LLM callbacks --

    def on_chat_model_start(
        self,
        serialized: dict,
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        """Capture structured prompt messages for llm_request event.

        This is also the canonical place to extract the first human message:
        messages are fully structured here, it fires only on real LLM calls,
        and the content is never compressed by checkpoint trimming.
        """
        rid = str(run_id)
        self._llm_start_times[rid] = time.monotonic()
        self._llm_call_index += 1
        self._seen_llm_starts.add(rid)

        logger.debug(
            "on_chat_model_start %s: tags=%s num_batches=%d message_counts=%s",
            run_id,
            tags,
            len(messages),
            [len(batch) for batch in messages],
        )

        # Capture the first user message sent to the lead agent in this run.
        caller = self._identify_caller(tags)
        if caller == "lead_agent" and not self._first_human_msg and messages:
            for batch in reversed(messages):
                for m in reversed(batch):
                    if _should_persist_human_input_message(m):
                        self.set_first_human_message(m.text)
                        self._put(
                            event_type="llm.human.input",
                            category="message",
                            content=m.model_dump(),
                            metadata={"caller": caller},
                        )
                        self._record_message_summary(m, caller=caller)
                        break
                if self._first_human_msg:
                    break

    def on_llm_start(self, serialized: dict, prompts: list[str], *, run_id: UUID, parent_run_id: UUID | None = None, tags: list[str] | None = None, metadata: dict[str, Any] | None = None, **kwargs: Any) -> None:
        # Fallback: on_chat_model_start is preferred. This just tracks latency.
        self._llm_start_times[str(run_id)] = time.monotonic()

    def on_llm_new_token(
        self,
        token: str | list[str | dict[str, Any]],
        *,
        chunk: Any = None,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        """Observe the lead Agent's reasoning boundary without persisting deltas.

        The duration begins with the first non-empty reasoning delta and ends
        when the first visible answer delta arrives. A reasoning-only tool-call
        step is closed in ``on_llm_end``. Subagent and middleware calls are
        deliberately excluded.
        """

        del token, parent_run_id, kwargs
        rid = str(run_id)
        if self._identify_caller(tags) != "lead_agent":
            self._reasoning_windows.pop(rid, None)
            return
        if rid in self._finalized_reasoning_durations:
            return

        message = getattr(chunk, "message", None)
        if message is None:
            return
        window = self._reasoning_windows.setdefault(rid, _ReasoningWindow())
        has_reasoning = self._message_has_structured_reasoning(message)
        content = getattr(message, "content", None)
        if isinstance(content, str) and not has_reasoning:
            (
                has_inline_reasoning,
                has_visible_content,
            ) = self._classify_inline_reasoning_delta(window, content)
            has_reasoning = has_inline_reasoning
        else:
            has_visible_content = self._message_has_visible_content(message)
        if not has_reasoning and not has_visible_content:
            return

        observed_at = time.monotonic()
        if has_reasoning and window.started_at is None:
            window.started_at = observed_at
        if has_visible_content and window.started_at is not None and window.ended_at is None:
            window.ended_at = observed_at

    def on_llm_end(
        self,
        response: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        messages: list[AnyMessage] = []
        logger.debug("on_llm_end %s: tags=%s", run_id, tags)
        # LangChain groups candidate generations per model input. The graph
        # consumes the first candidate from each group; alternatives must not
        # enter ActWeave's authoritative conversation history.
        for candidates in response.generations:
            if not candidates:
                continue
            selected = candidates[0]
            if hasattr(selected, "message"):
                messages.append(selected.message)
            else:
                logger.warning(
                    "on_llm_end %s: selected generation has no message attribute: %s",
                    run_id,
                    selected,
                )

        caller = self._identify_caller(tags)
        rid = str(run_id)
        start = self._llm_start_times.pop(rid, None)
        observed_at = time.monotonic()
        latency_ms = int((observed_at - start) * 1000) if start else None
        selected_reasoning_duration_ms = self._complete_reasoning_window(
            rid=rid,
            caller=caller,
            observed_at=observed_at,
        )

        for message_index, message in enumerate(messages):
            self._remember_current_run_tool_calls(message, caller=caller)

            # A batched callback can contain more than one selected message,
            # but the single observed stream window belongs only to its first
            # message. Reusing it across the remaining batch would fabricate a
            # duration for content that was not observed on that stream.
            reasoning_duration_ms = selected_reasoning_duration_ms if message_index == 0 else None
            self._replace_reasoning_duration(
                message,
                reasoning_duration_ms,
            )

            # Token usage from message
            usage = getattr(message, "usage_metadata", None)
            usage_dict = dict(usage) if usage else {}
            self._remember_error_fallback(message)

            # Resolve call index
            call_index = self._llm_call_index
            if rid not in self._seen_llm_starts:
                # Fallback: on_chat_model_start was not called
                self._llm_call_index += 1
                call_index = self._llm_call_index
                self._seen_llm_starts.add(rid)

            # Trace event: llm_response (OpenAI completion format)
            self._put(
                event_type="llm.ai.response",
                category="message" if caller == "lead_agent" else "trace",
                content=self._message_payload(
                    message,
                    reasoning_duration_ms=reasoning_duration_ms,
                ),
                metadata={
                    "caller": caller,
                    "usage": usage_dict,
                    "latency_ms": latency_ms,
                    "llm_call_index": call_index,
                },
            )
            if caller == "lead_agent" and isinstance(message, AIMessage):
                message_id = message.id
                if isinstance(message_id, str) and message_id.strip():
                    observation = (rid, message_index)
                    self._latest_lead_ai_observation_by_id[message_id] = observation
                    if message.tool_calls:
                        self._lead_tool_call_messages_by_id[message_id] = message.model_copy(deep=True)
                    else:
                        # Some providers may reuse one message id across model
                        # calls. A later plain final answer supersedes any old
                        # tool proposal with that id. Retaining the proposal
                        # would let loop reconciliation append a hidden same-id
                        # row after the answer and erase it during replay dedupe.
                        self._lead_tool_call_messages_by_id.pop(message_id, None)
            if rid not in self._counted_message_llm_run_ids:
                self._record_message_summary(message, caller=caller)

            # Token accumulation (dedup by langchain run_id to avoid double-counting
            # when the callback fires more than once for the same response)
            if self._track_tokens:
                input_tk = usage_dict.get("input_tokens", 0) or 0
                output_tk = usage_dict.get("output_tokens", 0) or 0
                total_tk = usage_dict.get("total_tokens", 0) or 0
                if total_tk == 0:
                    total_tk = input_tk + output_tk
                if total_tk > 0 and rid not in self._counted_llm_run_ids:
                    self._counted_llm_run_ids.add(rid)
                    self._total_input_tokens += input_tk
                    self._total_output_tokens += output_tk
                    self._total_tokens += total_tk
                    self._llm_call_count += 1

                    if caller.startswith("subagent:"):
                        self._subagent_tokens += total_tk
                    elif caller.startswith("middleware:"):
                        self._middleware_tokens += total_tk
                    else:
                        self._lead_agent_tokens += total_tk

                    # Per-model bucket
                    response_metadata = getattr(message, "response_metadata", None) or {}
                    per_call_model: str | None = None
                    if isinstance(response_metadata, Mapping):
                        per_call_model = response_metadata.get("model_name") or response_metadata.get("model")
                    self._record_model_usage(per_call_model, input_tk, output_tk, total_tk, self._extract_cache_read(usage_dict))

                    self._schedule_progress_flush()

        if messages:
            self._counted_message_llm_run_ids.add(str(run_id))

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        rid = str(run_id)
        self._llm_start_times.pop(rid, None)
        self._reasoning_windows.pop(rid, None)
        self._finalized_reasoning_durations.pop(rid, None)
        self._put(
            event_type="llm.error",
            category="trace",
            content="LLM request failed",
            metadata={"exception_class": _safe_exception_class(error)},
        )

    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, tags=None, metadata=None, inputs=None, **kwargs):
        """Cache the exact callback-run caller for later tool-result correlation."""
        tool_run_id = str(run_id)
        self._tool_run_callers[tool_run_id] = self._identify_caller(tags)
        logger.debug("Tool start for node %s, tool_run_id=%s, tags=%s", run_id, tool_run_id, tags)

    def on_tool_end(self, output, *, run_id, parent_run_id=None, tags=None, **kwargs):
        """Handle tool end event, append message and clear node data"""
        try:
            if isinstance(output, ToolMessage):
                msg = cast(ToolMessage, output)
                self._persist_tool_result_message(msg, caller=self._tool_message_caller(msg, run_id=run_id, tags=tags))
            elif isinstance(output, Command):
                cmd = cast(Command, output)
                messages = cmd.update.get("messages", [])
                for message in messages:
                    if isinstance(message, BaseMessage):
                        self._persist_tool_result_message(message, caller=self._tool_message_caller(message, run_id=run_id, tags=tags))
                    else:
                        logger.warning(f"on_tool_end {run_id}: command update message is not BaseMessage: {type(message)}")
            else:
                logger.warning(f"on_tool_end {run_id}: output is not ToolMessage: {type(output)}")
        finally:
            self._tool_run_callers.pop(str(run_id), None)
            logger.debug("Tool end for node %s", run_id)

    # -- Internal methods --

    @staticmethod
    def _non_empty_text(value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip())

    @classmethod
    def _message_has_structured_reasoning(cls, message: Any) -> bool:
        additional_kwargs = getattr(message, "additional_kwargs", None)
        if isinstance(additional_kwargs, Mapping) and cls._non_empty_text(additional_kwargs.get("reasoning_content")):
            return True

        content = getattr(message, "content", None)
        if not isinstance(content, list):
            return False
        for block in content:
            if not isinstance(block, Mapping):
                continue
            if block.get("type") not in {"thinking", "reasoning"}:
                continue
            if any(cls._non_empty_text(block.get(key)) for key in ("thinking", "reasoning", "text", "content")):
                return True
        return False

    @staticmethod
    def _partial_tag_suffix(value: str, marker: str) -> str:
        lowered = value.lower()
        max_length = min(len(lowered), len(marker) - 1)
        for length in range(max_length, 0, -1):
            if lowered.endswith(marker[:length]):
                return value[-length:]
        return ""

    @classmethod
    def _classify_inline_reasoning_delta(
        cls,
        window: _ReasoningWindow,
        content: str,
    ) -> tuple[bool, bool]:
        """Classify incremental ``<think>`` text, including split tags."""

        data = f"{window.inline_buffer}{content}"
        window.inline_buffer = ""
        has_reasoning = False
        has_visible_content = False

        while data:
            lowered = data.lower()
            if window.inline_think_open:
                has_reasoning = True
                close_index = lowered.find("</think")
                if close_index < 0:
                    window.inline_buffer = cls._partial_tag_suffix(
                        data,
                        "</think",
                    )
                    return has_reasoning, has_visible_content
                close_end = lowered.find(">", close_index + len("</think"))
                if close_end < 0:
                    window.inline_buffer = data[close_index:]
                    return has_reasoning, has_visible_content
                window.inline_think_open = False
                data = data[close_end + 1 :]
                continue

            open_index = lowered.find("<think")
            if open_index < 0:
                partial = cls._partial_tag_suffix(data, "<think")
                visible = data[: len(data) - len(partial)] if partial else data
                has_visible_content = has_visible_content or bool(visible.strip())
                window.inline_buffer = partial
                return has_reasoning, has_visible_content

            has_visible_content = has_visible_content or bool(data[:open_index].strip())
            open_end = lowered.find(">", open_index + len("<think"))
            if open_end < 0:
                window.inline_buffer = data[open_index:]
                return has_reasoning, has_visible_content
            window.inline_think_open = True
            has_reasoning = True
            data = data[open_end + 1 :]

        return has_reasoning, has_visible_content

    @classmethod
    def _message_has_visible_content(cls, message: Any) -> bool:
        content = getattr(message, "content", None)
        if isinstance(content, str):
            stripped = content.strip()
            return bool(stripped) and "<think" not in stripped.lower()
        if not isinstance(content, list):
            return False
        for block in content:
            if isinstance(block, str) and block.strip():
                return True
            if not isinstance(block, Mapping):
                continue
            if block.get("type") in {"thinking", "reasoning"}:
                continue
            if any(cls._non_empty_text(block.get(key)) for key in ("text", "content", "output_text")):
                return True
        return False

    def _complete_reasoning_window(
        self,
        *,
        rid: str,
        caller: str,
        observed_at: float,
    ) -> int | None:
        finalized = self._finalized_reasoning_durations.get(rid)
        if finalized is not None:
            return finalized
        window = self._reasoning_windows.pop(rid, None)
        if caller != "lead_agent" or window is None:
            return None

        started_at = window.started_at
        if started_at is None:
            return None

        ended_at = window.ended_at if window.ended_at is not None else observed_at
        elapsed_ms = int(round(max(0.0, ended_at - started_at) * 1000))
        duration_ms = min(elapsed_ms, _MAX_REASONING_DURATION_MS)
        self._finalized_reasoning_durations[rid] = duration_ms
        return duration_ms

    @staticmethod
    def _replace_reasoning_duration(
        message: Any,
        duration_ms: int | None,
    ) -> None:
        additional_kwargs = getattr(message, "additional_kwargs", None)
        projected_kwargs = dict(additional_kwargs) if isinstance(additional_kwargs, Mapping) else {}
        projected_kwargs.pop(_REASONING_DURATION_KEY, None)
        if duration_ms is not None:
            projected_kwargs[_REASONING_DURATION_KEY] = duration_ms
        try:
            message.additional_kwargs = projected_kwargs
        except (AttributeError, TypeError, ValueError):
            logger.debug(
                "Could not attach observed reasoning duration to model message",
                exc_info=True,
            )

    @staticmethod
    def _message_payload(
        message: Any,
        *,
        reasoning_duration_ms: int | None,
    ) -> dict[str, Any]:
        payload = message.model_dump()
        if not isinstance(payload, Mapping):
            return {}
        projected = dict(payload)
        additional_kwargs = projected.get("additional_kwargs")
        projected_kwargs = dict(additional_kwargs) if isinstance(additional_kwargs, Mapping) else {}
        projected_kwargs.pop(_REASONING_DURATION_KEY, None)
        if reasoning_duration_ms is not None:
            projected_kwargs[_REASONING_DURATION_KEY] = reasoning_duration_ms
        projected["additional_kwargs"] = projected_kwargs
        return projected

    @staticmethod
    def _message_identity(message: BaseMessage) -> str | None:
        tool_call_id = getattr(message, "tool_call_id", None)
        if isinstance(tool_call_id, str) and tool_call_id:
            return f"tool:{tool_call_id}"
        message_id = getattr(message, "id", None)
        if isinstance(message_id, str) and message_id:
            return f"message:{message_id}"
        return None

    @staticmethod
    def _tool_call_value(tool_call: Any, key: str) -> Any:
        if isinstance(tool_call, Mapping):
            return tool_call.get(key)
        return getattr(tool_call, key, None)

    def _remember_current_run_tool_calls(self, message: AnyMessage, *, caller: str) -> None:
        is_ai_message = isinstance(message, AIMessage) or getattr(message, "type", None) == "ai"
        if not is_ai_message:
            return
        tool_calls = getattr(message, "tool_calls", None) or []
        if not isinstance(tool_calls, list):
            return
        for tool_call in tool_calls:
            tool_call_id = self._tool_call_value(tool_call, "id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                continue
            self._tool_call_callers[tool_call_id] = caller
            name = self._tool_call_value(tool_call, "name")
            if caller == "lead_agent":
                self._current_run_tool_call_names[tool_call_id] = str(name or "")

    def _tool_message_caller(self, message: BaseMessage, *, run_id: UUID, tags: list[str] | None) -> str:
        caller = self._tool_run_callers.get(str(run_id))
        if caller is not None:
            return caller
        tool_call_id = getattr(message, "tool_call_id", None)
        if isinstance(tool_call_id, str) and tool_call_id:
            caller = self._tool_call_callers.get(tool_call_id)
            if caller is not None:
                return caller
        return self._identify_caller(tags)

    def _persist_tool_result_message(self, message: BaseMessage, *, caller: str = "lead_agent") -> None:
        self._put(
            event_type="llm.tool.result",
            category="message" if caller == "lead_agent" else "trace",
            content=message.model_dump(),
            metadata={"caller": caller},
        )
        if caller == "lead_agent":
            identity = self._message_identity(message)
            if identity:
                self._persisted_tool_message_identities.add(identity)
        self._record_message_summary(message, caller=caller)

    def _final_output_messages(self, outputs: Any) -> list[Any]:
        if isinstance(outputs, Mapping):
            messages = outputs.get("messages", [])
            return messages if isinstance(messages, list) else []
        return []

    def _remember_error_fallback(self, message: Any) -> bool:
        if isinstance(message, Mapping):
            additional_kwargs = message.get("additional_kwargs")
            content = message.get("content")
            fallback_text = content.strip() if isinstance(content, str) else ""
        else:
            additional_kwargs = getattr(message, "additional_kwargs", None)
            fallback_text = self._message_text(message).strip() if isinstance(message, BaseMessage) else ""
        if not (isinstance(additional_kwargs, Mapping) and additional_kwargs.get("deerflow_error_fallback") is True):
            return False
        self._had_llm_error_fallback = True
        detail = additional_kwargs.get("error_detail")
        reason = additional_kwargs.get("error_reason")
        raw_error_code = additional_kwargs.get("error_code")
        legacy_error_code = additional_kwargs.get("error_detail")
        self._llm_error_fallback_code = (
            raw_error_code
            if isinstance(raw_error_code, str) and raw_error_code in LLM_PUBLIC_ERROR_CODES
            else (legacy_error_code if isinstance(legacy_error_code, str) and legacy_error_code in LLM_PUBLIC_ERROR_CODES else llm_error_code_for_reason(reason))
        )
        if isinstance(detail, str) and detail.strip():
            self._llm_error_fallback_message = detail.strip()
        elif isinstance(reason, str) and reason.strip():
            self._llm_error_fallback_message = reason.strip()
        elif fallback_text:
            self._llm_error_fallback_message = fallback_text[:2000]
        return True

    def _reconcile_final_error_fallback(self, outputs: Any) -> None:
        for message in reversed(self._final_output_messages(outputs)):
            if isinstance(message, Mapping):
                message_type = message.get("type") or message.get("role")
            else:
                message_type = getattr(message, "type", None)
            if message_type in {"human", "user"}:
                return
            if message_type not in {"ai", "assistant"}:
                continue
            self._remember_error_fallback(message)
            return

    def _should_reconcile_tool_message(self, message: ToolMessage) -> bool:
        if message.additional_kwargs.get("hide_from_ui") is True:
            return False
        tool_call_id = getattr(message, "tool_call_id", None)
        if not isinstance(tool_call_id, str) or not tool_call_id:
            return False
        tool_call_name = self._current_run_tool_call_names.get(tool_call_id)
        if tool_call_name is None:
            return False
        message_name = getattr(message, "name", None)
        if message_name not in _RECONCILED_TOOL_MESSAGE_NAMES and tool_call_name not in _RECONCILED_TOOL_MESSAGE_NAMES:
            return False
        identity = self._message_identity(message)
        return identity is not None and identity not in self._persisted_tool_message_identities

    def _reconcile_final_tool_messages(self, outputs: Any) -> None:
        for message in self._final_output_messages(outputs):
            if not isinstance(message, ToolMessage):
                continue
            if self._should_reconcile_tool_message(message):
                self._persist_tool_result_message(message, caller="lead_agent")

    def _reconcile_suppressed_loop_proposals(self) -> None:
        """Supersede only proposals suppressed by the server-owned loop guard."""

        recorder = self._semantic_stop_recorder
        if recorder is None:
            return
        for message_id in recorder.suppressed_ai_message_ids:
            if message_id in self._reconciled_loop_safety_message_ids:
                continue
            original = self._lead_tool_call_messages_by_id.get(message_id)
            if original is None or not original.tool_calls:
                continue
            additional_kwargs = dict(original.additional_kwargs or {})
            additional_kwargs.pop("tool_calls", None)
            additional_kwargs.pop("function_call", None)
            additional_kwargs["hide_from_ui"] = True
            additional_kwargs[TOOL_CALL_CONTROL_LOOP_REPLACEMENT_KEY] = True
            response_metadata = dict(original.response_metadata or {})
            if response_metadata.get("finish_reason") == "tool_calls":
                response_metadata["finish_reason"] = "stop"
            replacement = original.model_copy(
                deep=True,
                update={
                    "tool_calls": [],
                    "invalid_tool_calls": [],
                    "additional_kwargs": additional_kwargs,
                    "response_metadata": response_metadata,
                },
            )
            self._put(
                event_type="llm.ai.response",
                category="message",
                content=self._message_payload(
                    replacement,
                    reasoning_duration_ms=None,
                ),
                metadata={
                    "caller": "lead_agent",
                    "source": "loop_safety_reconciliation",
                },
            )
            self._reconciled_loop_safety_message_ids.add(message_id)

    def _reconcile_final_recovered_llm_failures(self, outputs: Any) -> None:
        """Persist safe recovered-attempt facts added after on_llm_end fired."""

        recovered_failures = self._recovered_llm_failure_recorder.snapshot()

        if not recovered_failures:
            return

        persisted_count = self._persisted_recovered_llm_failure_count
        new_failures = recovered_failures[persisted_count:]
        if new_failures:
            self._put(
                event_type="run.recovered_issue",
                category="trace",
                content={
                    "kind": "llm_retry_recovered",
                    **build_recovered_llm_failures_receipt(
                        new_failures,
                    ),
                },
                metadata={"caller": "lead_agent"},
            )
            self._persisted_recovered_llm_failure_count = len(recovered_failures)
        aggregate = tuple(recovered_failures)
        for message in reversed(self._final_output_messages(outputs)):
            if isinstance(message, HumanMessage):
                break
            if not isinstance(message, AIMessage):
                continue
            if message.additional_kwargs.get("hide_from_ui") is True:
                continue
            message_id = message.id
            if not isinstance(message_id, str) or not message_id.strip():
                break
            observation = self._latest_lead_ai_observation_by_id.get(
                message_id,
            )
            if observation is None:
                break
            reconciliation_key = (message_id, *observation)
            if reconciliation_key in self._reconciled_recovered_llm_observations:
                break
            message_failures = read_recovered_llm_failures(
                message.additional_kwargs.get(
                    RECOVERED_LLM_FAILURES_KEY,
                )
            )
            if message_failures != aggregate:
                break
            raw_reasoning_duration = message.additional_kwargs.get(
                _REASONING_DURATION_KEY,
            )
            reasoning_duration_ms = raw_reasoning_duration if type(raw_reasoning_duration) is int and 0 <= raw_reasoning_duration <= _MAX_REASONING_DURATION_MS else None
            self._put(
                event_type="llm.ai.response",
                category="message",
                content=self._message_payload(
                    message,
                    reasoning_duration_ms=reasoning_duration_ms,
                ),
                metadata={
                    "caller": "lead_agent",
                    "source": "recovered_llm_failures",
                },
            )
            self._reconciled_recovered_llm_observations.add(
                reconciliation_key,
            )
            break

    def _reconcile_final_token_budget_message(self, outputs: Any) -> None:
        for message in reversed(self._final_output_messages(outputs)):
            if not isinstance(message, AIMessage):
                continue
            response_metadata = message.response_metadata
            status = read_token_budget_status(
                response_metadata.get(TOKEN_BUDGET_STATUS_KEY),
            )
            if status is None:
                continue
            self._put(
                event_type="llm.ai.response",
                category="message",
                content=message.model_dump(),
                metadata={
                    "caller": "lead_agent",
                    "source": "token_budget_status",
                },
            )
            return

    def _tool_call_control_payload(
        self,
        observation: ToolCallControlObservation,
    ) -> dict[str, object]:
        if not isinstance(
            observation,
            (RepeatedCallObservation, ToolCallBudgetObservation),
        ):
            raise TypeError(
                "observation must be a repeated-call or tool-budget observation",
            )
        if isinstance(observation, RepeatedCallObservation):
            if observation.reason_code not in _REPEATED_CALL_REASON_CODES:
                raise ValueError("repeated-call reason code is invalid")
            tool_name = None
        else:
            if observation.reason_code not in _TOOL_CALL_BUDGET_REASON_CODES:
                raise ValueError("tool-budget reason code is invalid")
            tool_name = observation.tool_name
        if observation.role not in {"lead", "subagent"}:
            raise ValueError("tool-call control role is invalid")
        if observation.workload_profile not in {"interactive", "research"}:
            raise ValueError("tool-call control workload profile is invalid")
        if observation.disposition not in _TOOL_CALL_CONTROL_DISPOSITIONS:
            raise ValueError("tool-call control disposition is invalid")
        if not isinstance(observation.observation_id, str) or re.fullmatch(r"[0-9a-f]{64}", observation.observation_id) is None:
            raise ValueError("tool-call control observation ID is invalid")
        if not isinstance(observation.scope_id, str) or not observation.scope_id:
            raise ValueError("tool-call control scope is invalid")
        if observation.role == "lead" and observation.scope_id != self.run_id:
            raise ValueError("lead tool-call control scope does not match Run")
        values = (
            observation.count_before,
            observation.proposed,
            observation.admitted,
            observation.rejected,
            observation.count_after,
            observation.warn_threshold,
            observation.hard_limit,
        )
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in values):
            raise ValueError("tool-call control counters are invalid")
        if observation.warn_threshold < 1 or observation.hard_limit < 1:
            raise ValueError("tool-call control thresholds are invalid")
        if observation.hard_limit < observation.warn_threshold:
            raise ValueError("tool-call control threshold order is invalid")
        if tool_name is not None and (not isinstance(tool_name, str) or _SAFE_TOOL_NAME.fullmatch(tool_name) is None):
            tool_name = "unknown"
        execution_id = (
            hashlib.sha256(
                f"{self.run_id}:{observation.scope_id}".encode(),
            ).hexdigest()[:32]
            if observation.role == "subagent"
            else None
        )
        payload: dict[str, object] = {
            "schema_version": 1,
            "reason_code": observation.reason_code,
            "workload_profile": observation.workload_profile,
            "role": observation.role,
            "run_id": self.run_id,
            "execution_id": execution_id,
            "count_before": observation.count_before,
            "proposed": observation.proposed,
            "admitted": observation.admitted,
            "rejected": observation.rejected,
            "count_after": observation.count_after,
            "warn_threshold": observation.warn_threshold,
            "hard_limit": observation.hard_limit,
            "disposition": observation.disposition,
            "observation_id": observation.observation_id,
        }
        if isinstance(observation, ToolCallBudgetObservation):
            payload["tool_name"] = tool_name
        return payload

    def record_tool_call_control_observation(
        self,
        observation: ToolCallControlObservation,
    ) -> None:
        """Persist one safe, idempotent control fact on the Run owner loop."""

        payload = self._tool_call_control_payload(observation)
        observation_id = observation.observation_id
        if observation_id in self._tool_call_control_observation_ids:
            return
        self._tool_call_control_observation_ids.add(observation_id)
        self._put(
            event_type=("middleware:repeated_call" if isinstance(observation, RepeatedCallObservation) else "middleware:tool_call_budget"),
            category="middleware",
            content=payload,
            metadata={
                "reason_code": observation.reason_code,
                "observation_id": observation_id,
            },
        )

    def record_subagent_limit_observation(
        self,
        observation: SubagentLimitObservation,
    ) -> None:
        """Persist one safe, replay-idempotent delegation-limit fact."""

        if not isinstance(observation, SubagentLimitObservation):
            raise TypeError("observation must be SubagentLimitObservation")
        if observation.reason_code != SUBAGENT_LIMIT_EVENT_REASON:
            raise ValueError("subagent-limit reason code is invalid")
        if observation.role != "lead":
            raise ValueError("subagent-limit role is invalid")
        if observation.run_id != self.run_id:
            raise ValueError("subagent-limit observation does not match Run")
        if observation.disposition != "truncate_tool_calls":
            raise ValueError("subagent-limit disposition is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", observation.observation_id) is None:
            raise ValueError("subagent-limit observation ID is invalid")
        values = (
            observation.count_before,
            observation.proposed,
            observation.admitted,
            observation.rejected,
            observation.count_after,
            observation.hard_limit,
        )
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in values):
            raise ValueError("subagent-limit counters are invalid")
        if observation.hard_limit < 1:
            raise ValueError("subagent-limit hard limit is invalid")
        if observation.admitted + observation.rejected != observation.proposed:
            raise ValueError("subagent-limit proposal accounting is invalid")
        if observation.count_after != observation.count_before + observation.admitted:
            raise ValueError("subagent-limit admitted accounting is invalid")
        if observation.count_after > observation.hard_limit:
            raise ValueError("subagent-limit count exceeds hard limit")
        observation_id = observation.observation_id
        if observation_id in self._subagent_limit_observation_ids:
            return
        self._subagent_limit_observation_ids.add(observation_id)
        self._put(
            event_type="middleware:subagent_limit",
            category="middleware",
            content=observation.payload(),
            metadata={
                "reason_code": observation.reason_code,
                "observation_id": observation_id,
            },
        )

    def _put(self, *, event_type: str, category: str, content: str | dict = "", metadata: dict | None = None) -> None:
        self._buffer.append(
            {
                "thread_id": self.thread_id,
                "run_id": self.run_id,
                "event_type": event_type,
                "category": category,
                "content": content,
                "metadata": metadata or {},
                "created_at": datetime.now(UTC).isoformat(),
            }
        )
        if len(self._buffer) >= self._flush_threshold:
            self._flush_sync()

    def _flush_sync(self) -> None:
        """Best-effort flush of buffer to RunEventStore.

        BaseCallbackHandler methods are synchronous.  If an event loop is
        running we schedule an async ``put_batch``; otherwise the events
        stay in the buffer and are flushed later by the async ``flush()``
        call in the worker's ``finally`` block.
        """
        if not self._buffer:
            return
        # Skip if a flush is already in flight — avoids concurrent writes
        # from multiple fire-and-forget tasks.
        if self._pending_flush_tasks:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No event loop — keep events in buffer for later async flush.
            return
        batch = self._buffer.copy()
        self._buffer.clear()
        task = loop.create_task(self._flush_async(batch))
        self._pending_flush_tasks.add(task)
        task.add_done_callback(self._on_flush_done)

    async def _flush_async(self, batch: list[dict]) -> None:
        try:
            if self._scope is None:
                await self._store.put_batch(batch)
            else:
                await self._store.put_batch(batch, scope=self._scope)
        except Exception:
            logger.warning(
                "Failed to flush %d events for run %s — returning to buffer",
                len(batch),
                self.run_id,
                exc_info=True,
            )
            # Return failed events to buffer for retry on next flush
            self._buffer = batch + self._buffer

    def _on_flush_done(self, task: asyncio.Task) -> None:
        self._pending_flush_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.warning("Journal flush task failed: %s", exc)

    def _identify_caller(self, tags: list[str] | None) -> str:
        _tags = tags or []
        for tag in _tags:
            if isinstance(tag, str) and (tag.startswith("subagent:") or tag.startswith("middleware:")):
                return tag
        if "lead_agent" in _tags:
            return "lead_agent"
        # Default to lead_agent: the main agent graph does not inject
        # callback tags, while subagents and middleware explicitly tag
        # themselves.
        return "lead_agent"

    def _record_model_usage(
        self,
        model_name: str | None,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
        cache_read_tokens: int = 0,
    ) -> None:
        """Add a single LLM call's token usage to the per-model accumulator.

        Missing / empty ``model_name`` collapses into a shared ``"unknown"``
        bucket so the breakdown stays usable when a provider doesn't surface
        ``response_metadata.model_name``.

        ``cache_read_tokens`` (prompt-cache hits, from
        ``usage_metadata.input_token_details.cache_read``) is stored as a
        sparse bucket key — only written when non-zero — so buckets from
        providers without cache reporting keep their historical shape.
        """
        if total_tokens <= 0:
            return
        bucket = self._tokens_by_model.setdefault(
            model_name or "unknown",
            {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        )
        bucket["input_tokens"] += int(input_tokens or 0)
        bucket["output_tokens"] += int(output_tokens or 0)
        bucket["total_tokens"] += int(total_tokens)
        if cache_read_tokens > 0:
            bucket["cache_read_tokens"] = bucket.get("cache_read_tokens", 0) + int(cache_read_tokens)

    @staticmethod
    def _extract_cache_read(usage_dict: dict) -> int:
        """Prompt-cache-hit input tokens from LangChain's normalized usage."""
        details = usage_dict.get("input_token_details") or {}
        if not isinstance(details, Mapping):
            return 0
        try:
            return max(int(details.get("cache_read") or 0), 0)
        except (TypeError, ValueError):
            return 0

    # -- Public methods (called by worker) --

    def record_external_llm_usage_records(
        self,
        records: list[dict[str, int | str | None]],
    ) -> None:
        """Record token usage from external sources (e.g., subagents).

        Each record should contain:
            source_run_id: Unique identifier to prevent double-counting
            caller: Caller tag (e.g. "subagent:general-purpose")
            model_name: Real per-call model name (str or None; falls back to
                ``"unknown"`` bucket when missing)
            input_tokens: Input token count
            output_tokens: Output token count
            total_tokens: Total token count (computed from input+output if 0/missing)
            cache_read_tokens: Optional prompt-cache-hit input tokens
        """
        if not self._track_tokens:
            return
        for record in records:
            source_id = str(record.get("source_run_id", ""))
            if not source_id:
                continue
            if source_id in self._counted_external_source_ids:
                continue

            total_tk = record.get("total_tokens", 0) or 0
            if total_tk <= 0:
                input_tk = record.get("input_tokens", 0) or 0
                output_tk = record.get("output_tokens", 0) or 0
                total_tk = input_tk + output_tk
            if total_tk <= 0:
                continue

            input_tk = record.get("input_tokens", 0) or 0
            output_tk = record.get("output_tokens", 0) or 0

            self._counted_external_source_ids.add(source_id)
            self._total_input_tokens += input_tk
            self._total_output_tokens += output_tk
            self._total_tokens += total_tk

            caller = str(record.get("caller", ""))
            if caller.startswith("subagent:"):
                self._subagent_tokens += total_tk
            elif caller.startswith("middleware:"):
                self._middleware_tokens += total_tk
            else:
                self._lead_agent_tokens += total_tk

            cache_read_tk = record.get("cache_read_tokens", 0) or 0
            self._record_model_usage(record.get("model_name"), input_tk, output_tk, total_tk, int(cache_read_tk))

            self._schedule_progress_flush()

    def record_vision_usage(
        self,
        *,
        source_id: str,
        model_name: str,
        call_count: int,
        input_tokens: int | None,
        output_tokens: int | None,
        usage_unknown: bool,
        request_dispatched: bool,
    ) -> None:
        """Account one bounded Vision Bridge receipt without persisting content."""

        if not self._track_tokens or not source_id or source_id in self._counted_external_source_ids or type(call_count) is not int or call_count < 1 or request_dispatched is not True:
            return
        self._counted_external_source_ids.add(source_id)
        self._llm_call_count += call_count
        input_tk = input_tokens if type(input_tokens) is int and input_tokens >= 0 else 0
        output_tk = output_tokens if type(output_tokens) is int and output_tokens >= 0 else 0
        total_tk = input_tk + output_tk
        self._total_input_tokens += input_tk
        self._total_output_tokens += output_tk
        self._total_tokens += total_tk
        self._middleware_tokens += total_tk
        if total_tk > 0:
            self._record_model_usage(
                model_name,
                input_tk,
                output_tk,
                total_tk,
                0,
            )
        self._put(
            event_type="vision.usage",
            category="trace",
            content={
                "call_count": call_count,
                "input_tokens": input_tokens if type(input_tokens) is int else None,
                "output_tokens": output_tokens if type(output_tokens) is int else None,
                "usage_unknown": usage_unknown is True,
            },
            metadata={
                "caller": "middleware:vision_bridge",
                "model_name": model_name,
            },
        )
        self._schedule_progress_flush()

    def set_first_human_message(self, content: str) -> None:
        """Record the first human message for convenience fields."""
        self._first_human_msg = content[:2000] if content else None

    def record_middleware(self, tag: str, *, name: str, hook: str, action: str, changes: dict) -> None:
        """Record a middleware state-change event.

        Called by middleware implementations when they perform a meaningful
        state change (e.g., title generation, summarization, HITL approval).
        Pure-observation middleware should not call this.

        Args:
            tag: Short identifier for the middleware (e.g., "title", "summarize",
                 "guardrail"). Used to form event_type="middleware:{tag}".
            name: Full middleware class name.
            hook: Lifecycle hook that triggered the action (e.g., "after_model").
            action: Specific action performed (e.g., "generate_title").
            changes: Dict describing the state changes made.
        """
        self._put(
            event_type=f"middleware:{tag}",
            category="middleware",
            content={"name": name, "hook": hook, "action": action, "changes": changes},
        )

    async def flush(self) -> None:
        """Force flush remaining buffer. Called in worker's finally block."""
        if self._pending_flush_tasks:
            await asyncio.gather(*tuple(self._pending_flush_tasks), return_exceptions=True)
        while self._pending_progress_task is not None and not self._pending_progress_task.done():
            if self._pending_progress_delayed:
                self._pending_progress_task.cancel()
                await asyncio.gather(self._pending_progress_task, return_exceptions=True)
                self._progress_dirty = False
                self._pending_progress_delayed = False
                break
            await asyncio.gather(self._pending_progress_task, return_exceptions=True)

        while self._buffer:
            batch = self._buffer[: self._flush_threshold]
            del self._buffer[: self._flush_threshold]
            try:
                if self._scope is None:
                    await self._store.put_batch(batch)
                else:
                    await self._store.put_batch(batch, scope=self._scope)
            except Exception:
                self._buffer = batch + self._buffer
                raise

    def _schedule_progress_flush(self) -> None:
        """Best-effort throttled progress snapshot for active run visibility."""
        if self._progress_reporter is None:
            return
        now = time.monotonic()
        elapsed = now - self._last_progress_flush
        if elapsed < self._progress_flush_interval:
            self._progress_dirty = True
            self._schedule_delayed_progress_flush(self._progress_flush_interval - elapsed)
            return
        if self._pending_progress_task is not None and not self._pending_progress_task.done():
            self._progress_dirty = True
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._progress_dirty = False
        self._pending_progress_task = loop.create_task(self._flush_progress_async(snapshot=self.get_completion_data()))

    def _schedule_delayed_progress_flush(self, delay: float) -> None:
        if self._pending_progress_task is not None and not self._pending_progress_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        delay = max(0.0, delay)
        self._pending_progress_delayed = delay > 0
        self._pending_progress_task = loop.create_task(self._flush_progress_async(delay=delay))

    async def _flush_progress_async(self, *, snapshot: dict | None = None, delay: float = 0.0) -> None:
        if self._progress_reporter is None:
            return
        if delay > 0:
            self._pending_progress_delayed = True
            await asyncio.sleep(delay)
            self._pending_progress_delayed = False
        dirty_before_write = self._progress_dirty
        self._progress_dirty = False
        snapshot_to_write = snapshot or self.get_completion_data()
        try:
            await self._progress_reporter(snapshot_to_write)
            self._last_progress_flush = time.monotonic()
        except Exception:
            logger.warning("Failed to persist progress snapshot for run %s", self.run_id, exc_info=True)
        if dirty_before_write or self._progress_dirty:
            self._progress_dirty = False
            self._pending_progress_task = None
            self._schedule_delayed_progress_flush(self._progress_flush_interval)

    def get_completion_data(self) -> dict:
        """Return accumulated token and message data for run completion."""
        return {
            "total_input_tokens": self._total_input_tokens,
            "total_output_tokens": self._total_output_tokens,
            "total_tokens": self._total_tokens,
            "llm_call_count": self._llm_call_count,
            "lead_agent_tokens": self._lead_agent_tokens,
            "subagent_tokens": self._subagent_tokens,
            "middleware_tokens": self._middleware_tokens,
            "token_usage_by_model": {model: dict(usage) for model, usage in self._tokens_by_model.items()},
            "message_count": self._msg_count,
            "last_ai_message": self._last_ai_msg,
            "first_human_message": self._first_human_msg,
        }

    @property
    def had_llm_error_fallback(self) -> bool:
        return self._had_llm_error_fallback

    @property
    def llm_error_fallback_message(self) -> str | None:
        return self._llm_error_fallback_message

    @property
    def llm_error_fallback_code(self) -> str | None:
        return self._llm_error_fallback_code


class RunJournalToolCallControlObserver:
    """Marshal Harness control facts onto the parent Run's owner loop."""

    __slots__ = ("_journal", "_lock", "_observed", "_owner_loop")

    def __init__(
        self,
        journal: RunJournal,
        *,
        owner_loop: asyncio.AbstractEventLoop,
    ) -> None:
        if not isinstance(journal, RunJournal):
            raise TypeError("journal must be RunJournal")
        if not isinstance(owner_loop, asyncio.AbstractEventLoop):
            raise TypeError("owner_loop must be an asyncio event loop")
        self._journal = journal
        self._owner_loop = owner_loop
        self._lock = threading.Lock()
        self._observed: set[str] = set()

    def observe(self, observation: ToolCallControlObservation) -> None:
        """Attempt one safe SSE/journal projection without changing enforcement."""

        try:
            payload = self._journal._tool_call_control_payload(observation)
            with self._lock:
                if observation.observation_id in self._observed:
                    return
                self._observed.add(observation.observation_id)

            if observation.role == "lead":
                try:
                    from langgraph.config import get_stream_writer

                    get_stream_writer()(
                        {
                            "type": (
                                "repeated_call"
                                if isinstance(
                                    observation,
                                    RepeatedCallObservation,
                                )
                                else "tool_call_budget"
                            ),
                            **payload,
                        }
                    )
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "Tool-call control live event unavailable",
                        exc_info=True,
                    )

            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None
            if current_loop is self._owner_loop:
                self._journal.record_tool_call_control_observation(observation)
                return
            if self._owner_loop.is_closed() or not self._owner_loop.is_running():
                return
            self._owner_loop.call_soon_threadsafe(
                self._journal.record_tool_call_control_observation,
                observation,
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "Tool-call control observation delivery failed",
                exc_info=True,
            )
