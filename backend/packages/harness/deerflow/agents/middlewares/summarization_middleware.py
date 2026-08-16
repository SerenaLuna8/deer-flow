"""Summarization middleware extensions for ActWeave."""

from __future__ import annotations

import html
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, NotRequired, TypedDict, cast, override

from langchain.agents import AgentState
from langchain.agents.middleware import SummarizationMiddleware
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
    get_buffer_string,
)
from langgraph.config import get_config
from langgraph.constants import TAG_NOSTREAM
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime

from deerflow.agents.human_input import read_human_input_response
from deerflow.agents.memory.snip import (
    MEMORY_ARCHIVE_CONTEXT_KEY,
    SNIP_ARCHIVE_PROMPT,
    SNIP_RETRY_REINFORCEMENT,
    MemoryArchiveReceipt,
    SnipArchiveContext,
    SnipOutputInvalid,
    build_memory_archive_receipt,
    parse_snip_dual_output,
    validate_snip_output,
)
from deerflow.agents.middlewares.dynamic_context_middleware import is_dynamic_context_reminder
from deerflow.config.app_config import get_app_config
from deerflow.config.summarization_config import validate_summary_prompt_template
from deerflow.models import ModelRuntime, ModelRuntimeProfile
from deerflow.models.runtime import AsyncAbortEvent
from deerflow.runtime.context_keys import RuntimeContextKeys
from deerflow.sandbox.sandbox import AuthorizationRevoked, check_authorization_boundary
from deerflow.utils.messages import SUMMARY_MESSAGE_NAME, is_real_user_message

logger = logging.getLogger(__name__)
_SUMMARY_TRIGGER_MESSAGE_NAME = "summary"
_ASK_CLARIFICATION_TOOL_NAME = "ask_clarification"
MIN_SNIP_SUMMARY_OUTPUT_TOKENS = 4096


def _server_abort_event(runtime_context: object | None) -> AsyncAbortEvent | None:
    if not isinstance(runtime_context, Mapping):
        return None
    candidate = runtime_context.get(RuntimeContextKeys.SERVER_ABORT_EVENT)
    if not callable(getattr(candidate, "is_set", None)) or not callable(
        getattr(candidate, "wait", None),
    ):
        return None
    return cast(AsyncAbortEvent, candidate)


def _ensure_snip_summary_output_budget(model: Any) -> Any:
    """Raise a provider-declared output cap enough for the dual SNIP contract."""

    model_fields = getattr(type(model), "model_fields", {})
    for field_name in ("max_tokens", "max_output_tokens"):
        if field_name not in model_fields:
            continue
        current = getattr(model, field_name, None)
        if isinstance(current, int) and not isinstance(current, bool) and current >= MIN_SNIP_SUMMARY_OUTPUT_TOKENS:
            return model
        return model.model_copy(
            update={field_name: MIN_SNIP_SUMMARY_OUTPUT_TOKENS},
        )
    # Some providers (for example the Codex Responses endpoint) deliberately
    # expose no supported output-cap field. Keep their provider-owned behavior.
    return model


@dataclass(frozen=True)
class ContextCompactionResult:
    """Result of summarizing old context and retaining the active tail."""

    summary_text: str
    messages_to_summarize: tuple[AnyMessage, ...]
    preserved_messages: tuple[AnyMessage, ...]
    total_tokens: int
    memory_archive_receipt: MemoryArchiveReceipt | None


@dataclass(frozen=True)
class _SnipSummary:
    """One validated SNIP response split into its two destinations.

    ``continuity`` only ever becomes the Thread ``summary_text``; ``tagged_text``
    is the sole input to the memory archive receipt. Under the single-segment
    contract (custom summary prompts) both fields hold the same tagged document.
    """

    continuity: str
    tagged_text: str


class ContextTriggerUsage(TypedDict):
    """One configured OR trigger measured against the current retained context."""

    type: Literal["fraction", "tokens", "messages"]
    configured_value: int | float
    current_value: int | float
    threshold_value: int | float
    remaining_value: int | float
    progress_percent: float
    reached: bool
    context_window_tokens: NotRequired[int]
    threshold_tokens: NotRequired[int]


@dataclass(frozen=True)
class ContextUsageMeasurement:
    """Read-only context measurement using the automatic compactor's counter."""

    estimated_tokens: int
    message_count: int
    summary_present: bool
    context_window_tokens: int | None
    triggers: tuple[ContextTriggerUsage, ...]
    primary_trigger: ContextTriggerUsage | None


@dataclass(frozen=True)
class _PreparedCompaction:
    source_messages: tuple[AnyMessage, ...]
    snip_messages: tuple[AnyMessage, ...]
    preserved_messages: tuple[AnyMessage, ...]
    previous_summary: str | None
    total_tokens: int


def _resolve_thread_id(runtime: Runtime) -> str | None:
    """Resolve the current thread ID from runtime context or LangGraph config."""
    thread_id = runtime.context.get("thread_id") if runtime.context else None
    if thread_id is None:
        try:
            config_data = get_config()
        except RuntimeError:
            return None
        thread_id = config_data.get("configurable", {}).get("thread_id")
    return thread_id


class DeerFlowSummarizationMiddleware(SummarizationMiddleware):
    """SNIP compaction that removes only complete conversation turns."""

    def __init__(
        self,
        *args,
        compact_all_complete_turns: bool = False,
        dual_output_contract: bool | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._compact_all_complete_turns = compact_all_complete_turns
        # The summary LLM call runs inside a LangGraph middleware hook, so its token
        # stream would otherwise be captured by the messages-tuple stream callback and
        # broadcast to the frontend as a phantom AI message. Tag a dedicated model copy
        # with TAG_NOSTREAM so the streaming handler skips it.
        # Keep self.model untagged so the parent's profile / ls_params inspection still works.
        #
        # Preserve any tags already bound on the model (e.g. "middleware:summarize" set in
        # lead_agent/agent.py for RunJournal attribution): RunnableBinding.with_config does a
        # shallow merge that would otherwise overwrite the existing tags list entirely.
        existing_tags = list((getattr(self.model, "config", None) or {}).get("tags") or [])
        merged_tags = [*existing_tags, TAG_NOSTREAM] if TAG_NOSTREAM not in existing_tags else existing_tags
        self._summary_model = self.model.with_config(tags=merged_tags)
        # The dual-segment contract (continuity prose + tagged facts) only
        # applies to the packaged SNIP prompt. Deployments that construct this
        # middleware with a custom summary prompt keep the original
        # single-segment semantics.
        self._dual_output_contract = self.summary_prompt == SNIP_ARCHIVE_PROMPT if dual_output_contract is None else dual_output_contract

    def _parse_snip_response(self, raw: str) -> _SnipSummary:
        if self._dual_output_contract:
            continuity, tagged_text = parse_snip_dual_output(raw)
            return _SnipSummary(continuity=continuity, tagged_text=tagged_text)
        tagged_text = validate_snip_output(raw)
        return _SnipSummary(continuity=tagged_text, tagged_text=tagged_text)

    @override
    def _create_summary(self, messages_to_summarize: list[AnyMessage]) -> str | None:
        summary = self._summarize_with(messages_to_summarize)
        return None if summary is None else summary.continuity

    @override
    async def _acreate_summary(self, messages_to_summarize: list[AnyMessage]) -> str | None:
        summary = await self._asummarize_with(messages_to_summarize)
        return None if summary is None else summary.continuity

    def _summarize_with(self, messages_to_summarize: list[AnyMessage], previous_summary: str | None = None) -> _SnipSummary | None:
        """Mirror the parent ``_create_summary`` but invoke the nostream-tagged model.

        We do not swap ``self.model`` at the instance level: the agent/middleware is
        cached and reused across concurrent runs, so a temporary swap would leak the
        ``RunnableBinding`` to other coroutines during ``await`` and break parent logic
        that inspects the raw model (``profile`` / ``_get_ls_params``).
        """
        if not messages_to_summarize:
            return None
        prompt = self._build_summary_prompt(messages_to_summarize, previous_summary=previous_summary)
        if prompt is None:
            return None
        # Bounded repair: at most two model calls per compaction attempt. The
        # retry reuses the same input plus a format reinforcement block and
        # never echoes the invalid output.
        for attempt_prompt in (prompt, f"{prompt}\n\n{SNIP_RETRY_REINFORCEMENT}"):
            try:
                response = ModelRuntime.invoke_runnable(
                    self._summary_model,
                    attempt_prompt,
                    profile=ModelRuntimeProfile.AGENT_GRAPH,
                    config={"metadata": {"lc_source": "summarization"}},
                )
                return self._parse_snip_response(response.text)
            except SnipOutputInvalid:
                continue
            except Exception:
                logger.exception("SNIP generation failed; skipping compaction this turn")
                return None
        logger.warning("SNIP model returned invalid output twice; skipping compaction this turn")
        return None

    async def _asummarize_with(
        self,
        messages_to_summarize: list[AnyMessage],
        previous_summary: str | None = None,
        *,
        authorization_context: object | None = None,
    ) -> _SnipSummary | None:
        """Async counterpart of :meth:`_summarize_with` using the nostream model."""
        if not messages_to_summarize:
            return None
        prompt = self._build_summary_prompt(messages_to_summarize, previous_summary=previous_summary)
        if prompt is None:
            return None
        # Bounded repair: at most two model calls per compaction attempt. The
        # retry reuses the same input plus a format reinforcement block and
        # never echoes the invalid output. The authorization boundary is
        # re-checked before every model call.
        for attempt_prompt in (prompt, f"{prompt}\n\n{SNIP_RETRY_REINFORCEMENT}"):
            try:
                try:
                    parent_config = get_config()
                except RuntimeError:
                    parent_config = {}
                effective_authorization_context = authorization_context or parent_config.get("context")
                await check_authorization_boundary(
                    effective_authorization_context,
                    "before_model_call",
                )
                response = await ModelRuntime.ainvoke_runnable(
                    self._summary_model,
                    attempt_prompt,
                    profile=ModelRuntimeProfile.AGENT_GRAPH,
                    config={"metadata": {"lc_source": "summarization"}},
                    abort_event=_server_abort_event(
                        effective_authorization_context,
                    ),
                )
                return self._parse_snip_response(response.text)
            except AuthorizationRevoked:
                raise
            except SnipOutputInvalid:
                continue
            except Exception:
                logger.exception("SNIP generation failed; skipping compaction this turn")
                return None
        logger.warning("SNIP model returned invalid output twice; skipping compaction this turn")
        return None

    @staticmethod
    def _summary_count_message(summary_text: str) -> HumanMessage:
        return HumanMessage(content=summary_text, name=_SUMMARY_TRIGGER_MESSAGE_NAME)

    def _messages_for_trigger_count(self, messages: list[AnyMessage], summary_text: str | None) -> list[AnyMessage]:
        if not summary_text:
            return messages
        return [*messages, self._summary_count_message(summary_text)]

    @staticmethod
    def _context_progress(current: int | float, threshold: int | float) -> float:
        if threshold <= 0:
            raise ValueError("Context trigger threshold must be positive")
        return round(min(100.0, max(0.0, float(current) / float(threshold) * 100.0)), 2)

    def measure_context_usage(
        self,
        messages: list[AnyMessage],
        *,
        summary_text: str | None,
    ) -> ContextUsageMeasurement:
        """Measure the same retained input used by automatic trigger evaluation.

        This path only invokes the configured token counter and model-profile
        inspection. It never invokes the summary model or mutates Agent state.
        """

        trigger_messages = self._messages_for_trigger_count(messages, summary_text)
        estimated_tokens = self.token_counter(trigger_messages)
        message_count = len(trigger_messages)
        context_window_tokens = self._get_profile_limits()
        if context_window_tokens is not None and context_window_tokens <= 0:
            raise ValueError("Model max_input_tokens must be positive")

        triggers: list[ContextTriggerUsage] = []
        for trigger_type, configured_value in self._trigger_conditions:
            if trigger_type == "messages":
                threshold = int(configured_value)
                current = message_count
                triggers.append(
                    ContextTriggerUsage(
                        type="messages",
                        configured_value=threshold,
                        current_value=current,
                        threshold_value=threshold,
                        remaining_value=max(0, threshold - current),
                        progress_percent=self._context_progress(current, threshold),
                        reached=current >= threshold,
                    )
                )
                continue

            if trigger_type == "tokens":
                threshold_tokens = int(configured_value)
                reached = estimated_tokens >= threshold_tokens or self._should_summarize_based_on_reported_tokens(
                    trigger_messages,
                    float(threshold_tokens),
                )
                triggers.append(
                    ContextTriggerUsage(
                        type="tokens",
                        configured_value=threshold_tokens,
                        current_value=estimated_tokens,
                        threshold_value=threshold_tokens,
                        remaining_value=max(0, threshold_tokens - estimated_tokens),
                        progress_percent=self._context_progress(
                            estimated_tokens,
                            threshold_tokens,
                        ),
                        reached=reached,
                        threshold_tokens=threshold_tokens,
                    )
                )
                continue

            if context_window_tokens is None:
                raise ValueError("Model max_input_tokens is required for fraction triggers")
            threshold_fraction = float(configured_value)
            threshold_tokens = max(1, int(context_window_tokens * threshold_fraction))
            current_fraction = round(estimated_tokens / context_window_tokens, 6)
            reached = estimated_tokens >= threshold_tokens or self._should_summarize_based_on_reported_tokens(
                trigger_messages,
                float(threshold_tokens),
            )
            triggers.append(
                ContextTriggerUsage(
                    type="fraction",
                    configured_value=threshold_fraction,
                    current_value=current_fraction,
                    threshold_value=threshold_fraction,
                    remaining_value=round(
                        max(0.0, threshold_fraction - current_fraction),
                        6,
                    ),
                    progress_percent=self._context_progress(
                        current_fraction,
                        threshold_fraction,
                    ),
                    reached=reached,
                    context_window_tokens=context_window_tokens,
                    threshold_tokens=threshold_tokens,
                )
            )

        primary_trigger = max(
            triggers,
            key=lambda trigger: trigger["progress_percent"],
            default=None,
        )
        return ContextUsageMeasurement(
            estimated_tokens=estimated_tokens,
            message_count=message_count,
            summary_present=bool(summary_text),
            context_window_tokens=context_window_tokens,
            triggers=tuple(triggers),
            primary_trigger=primary_trigger,
        )

    @staticmethod
    def _assemble_summary_input_text(
        escaped_new_messages: str,
        escaped_previous_summary: str,
    ) -> str | None:
        """Assemble already-escaped summary sections into the prompt payload."""
        parts: list[str] = []
        if escaped_previous_summary:
            parts.extend(
                [
                    "<existing_summary>",
                    escaped_previous_summary,
                    "</existing_summary>",
                    "",
                ]
            )
        if escaped_new_messages:
            parts.extend(
                [
                    "<new_messages>",
                    escaped_new_messages,
                    "</new_messages>",
                ]
            )
        if not parts:
            return None
        return "\n".join(parts)

    def _count_rendered_summary_prompt_tokens(self, input_text: str) -> int | None:
        """Count the exact rendered prompt without exposing its durable contents."""
        try:
            validate_summary_prompt_template(self.summary_prompt)
            rendered_prompt = self.summary_prompt.format(messages=input_text).rstrip()
            return self.token_counter([HumanMessage(content=rendered_prompt)])
        except Exception:
            logger.debug("Failed to count rendered summary prompt; skipping compaction safely", exc_info=True)
            return None

    def _build_summary_input_text(self, formatted_messages: str, previous_summary: str | None = None) -> str | None:
        """Escape and assemble the exact selected source without truncation."""

        escaped_new_messages = html.escape(formatted_messages, quote=False)
        escaped_previous_summary = html.escape(previous_summary.strip(), quote=False) if previous_summary else ""
        return self._assemble_summary_input_text(
            escaped_new_messages,
            escaped_previous_summary,
        )

    def _build_summary_prompt(self, messages_to_summarize: list[AnyMessage], previous_summary: str | None = None) -> str | None:
        """Build the summary prompt, returning ``None`` when trimming leaves nothing."""
        try:
            validate_summary_prompt_template(self.summary_prompt)
        except (KeyError, IndexError, TypeError, ValueError):
            # Direct middleware construction can bypass ``SummarizationConfig``.
            # Fail closed before any template rendering and without logging
            # prompt or conversation contents.
            logger.exception("Invalid summary prompt template; skipping compaction this turn")
            return None

        if not messages_to_summarize:
            return None
        formatted_messages = get_buffer_string(messages_to_summarize)
        formatted_messages = self._build_summary_input_text(formatted_messages, previous_summary=previous_summary)
        if not formatted_messages:
            return None
        try:
            prompt = self.summary_prompt.format(messages=formatted_messages).rstrip()
            if self.trim_tokens_to_summarize is not None:
                rendered_tokens = self._count_rendered_summary_prompt_tokens(formatted_messages)
                if rendered_tokens is None:
                    return None
                if rendered_tokens > max(1, self.trim_tokens_to_summarize):
                    logger.warning("Rendered summary prompt exceeds configured token budget; skipping compaction this turn")
                    return None
            return prompt
        except (KeyError, IndexError, TypeError, ValueError):
            # Keep the final assembly fail-closed if formatting behavior changes
            # after validation.
            logger.exception("Invalid summary prompt template; skipping compaction this turn")
            return None

    def before_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._maybe_summarize(state, runtime)

    async def abefore_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return await self._amaybe_summarize(state, runtime)

    @staticmethod
    def _is_turn_user(message: AnyMessage) -> bool:
        if is_real_user_message(message):
            return True
        return (
            isinstance(message, HumanMessage)
            and message.name != SUMMARY_MESSAGE_NAME
            and not is_dynamic_context_reminder(message)
            and isinstance(
                message.additional_kwargs.get("human_input_response"),
                dict,
            )
        )

    @classmethod
    def _turn_prefix_start(
        cls,
        messages: list[AnyMessage],
        user_index: int,
    ) -> int:
        index = user_index
        while index > 0:
            candidate = messages[index - 1]
            hidden_prefix = isinstance(candidate, (HumanMessage, SystemMessage)) and bool(candidate.additional_kwargs.get("hide_from_ui")) and not cls._is_turn_user(candidate)
            if not is_dynamic_context_reminder(candidate) and not hidden_prefix:
                break
            index -= 1
        return index

    @staticmethod
    def _clarification_request_tool_call_id(
        message: AnyMessage,
        request_id: str,
    ) -> str | None:
        if not isinstance(message, ToolMessage) or message.name != _ASK_CLARIFICATION_TOOL_NAME or message.id != request_id:
            return None
        tool_call_id = message.tool_call_id
        if not isinstance(tool_call_id, str) or not tool_call_id:
            return None
        artifact = message.artifact
        if not isinstance(artifact, Mapping):
            return None
        payload = artifact.get("human_input")
        if not isinstance(payload, Mapping):
            return None
        version = payload.get("version")
        if (
            type(version) is not int
            or version not in (1, 2)
            or payload.get("kind") != "human_input_request"
            or payload.get("source") != _ASK_CLARIFICATION_TOOL_NAME
            or payload.get("request_id") != request_id
            or payload.get("tool_call_id") != tool_call_id
        ):
            return None
        return tool_call_id

    @classmethod
    def _is_clarification_continuation(
        cls,
        messages: list[AnyMessage],
        *,
        turn_start: int,
        response_index: int,
    ) -> bool:
        """Match one server-hidden reply to its exact request ToolMessage and tool call."""

        response_message = messages[response_index]
        if not isinstance(response_message, HumanMessage) or response_message.additional_kwargs.get("hide_from_ui") is not True:
            return False
        response = read_human_input_response(response_message.additional_kwargs)
        if response is None or response["source"] != _ASK_CLARIFICATION_TOOL_NAME:
            return False

        request_index = next(
            (index for index in range(response_index - 1, turn_start - 1, -1) if isinstance(messages[index], ToolMessage) and messages[index].name == _ASK_CLARIFICATION_TOOL_NAME),
            None,
        )
        if request_index is None:
            return False
        tool_call_id = cls._clarification_request_tool_call_id(
            messages[request_index],
            response["request_id"],
        )
        if tool_call_id is None:
            return False
        matching_tool_calls = 0
        for message in messages[turn_start:request_index]:
            if not isinstance(message, AIMessage):
                continue
            matching_tool_calls += sum(1 for tool_call in message.tool_calls if isinstance(tool_call, Mapping) and tool_call.get("id") == tool_call_id and tool_call.get("name") == _ASK_CLARIFICATION_TOOL_NAME)
        return matching_tool_calls == 1

    @classmethod
    def _complete_turn_ranges(
        cls,
        messages: list[AnyMessage],
    ) -> tuple[tuple[int, int], ...]:
        """Return contiguous complete user turns from the state head."""

        user_indexes = [index for index, message in enumerate(messages) if cls._is_turn_user(message)]
        if not user_indexes:
            return ()

        starts = [0]
        seen_assistant = False
        for index in range(user_indexes[0] + 1, len(messages)):
            message = messages[index]
            if isinstance(message, AIMessage):
                seen_assistant = True
                continue
            if cls._is_turn_user(message) and seen_assistant:
                if cls._is_clarification_continuation(
                    messages,
                    turn_start=starts[-1],
                    response_index=index,
                ):
                    continue
                starts.append(cls._turn_prefix_start(messages, index))
                seen_assistant = False

        ranges: list[tuple[int, int]] = []
        for position, start in enumerate(starts):
            end = starts[position + 1] if position + 1 < len(starts) else len(messages)
            turn = messages[start:end]
            first_user = next(
                (index for index, message in enumerate(turn) if cls._is_turn_user(message)),
                None,
            )
            if first_user is None:
                break
            assistant_messages = [message for message in turn[first_user + 1 :] if isinstance(message, AIMessage)]
            if not assistant_messages:
                break
            tool_calls = [tool_call for message in assistant_messages for tool_call in message.tool_calls]
            if any(not isinstance(tool_call, dict) or not isinstance(tool_call.get("id"), str) or not tool_call.get("id") for tool_call in tool_calls):
                break
            expected_tool_calls = {tool_call["id"] for tool_call in tool_calls}
            completed_tool_calls = {message.tool_call_id for message in turn[first_user + 1 :] if isinstance(message, ToolMessage) and isinstance(message.tool_call_id, str) and message.tool_call_id}
            response_tail = next(
                (message for message in reversed(turn[first_user + 1 :]) if isinstance(message, (AIMessage, ToolMessage))),
                None,
            )
            if expected_tool_calls != completed_tool_calls or not isinstance(response_tail, AIMessage):
                break
            ranges.append((start, end))
        return tuple(ranges)

    def _requested_cutoff(self, messages: list[AnyMessage]) -> int:
        if self._compact_all_complete_turns:
            return len(messages)
        return self._determine_cutoff_index(messages)

    def _candidate_cutoffs(
        self,
        messages: list[AnyMessage],
        requested_cutoff: int,
    ) -> tuple[int, ...]:
        cutoffs: list[int] = []
        expected_start = 0
        for start, end in self._complete_turn_ranges(messages):
            if start != expected_start:
                break
            expected_start = end
            if end <= requested_cutoff:
                cutoffs.append(end)
        return tuple(reversed(cutoffs))

    @staticmethod
    def _snip_messages(messages: list[AnyMessage]) -> list[AnyMessage]:
        return [message for message in messages if not is_dynamic_context_reminder(message)]

    def _prepare_compaction(
        self,
        state: AgentState,
        *,
        force: bool = False,
    ) -> _PreparedCompaction | None:
        messages = state["messages"]
        self._ensure_message_ids(messages)

        previous_summary = state.get("summary_text") if isinstance(state.get("summary_text"), str) else None
        trigger_messages = self._messages_for_trigger_count(messages, previous_summary)
        total_tokens = self.token_counter(trigger_messages)
        if not force and not self._should_summarize(trigger_messages, total_tokens):
            return None

        requested_cutoff = self._requested_cutoff(messages)
        if requested_cutoff <= 0:
            return None

        for cutoff_index in self._candidate_cutoffs(
            messages,
            requested_cutoff,
        ):
            source_messages = messages[:cutoff_index]
            snip_messages = self._snip_messages(source_messages)
            if not snip_messages:
                continue
            # Rendering the exact prompt is also the budget test. Overflow
            # moves to the preceding whole-turn boundary; selected text is
            # never partially token- or character-trimmed.
            if (
                self._build_summary_prompt(
                    snip_messages,
                    previous_summary=previous_summary,
                )
                is None
            ):
                continue
            return _PreparedCompaction(
                source_messages=tuple(source_messages),
                snip_messages=tuple(snip_messages),
                preserved_messages=tuple(messages[cutoff_index:]),
                previous_summary=previous_summary,
                total_tokens=total_tokens,
            )
        return None

    @staticmethod
    def _archive_context(runtime: Runtime) -> SnipArchiveContext | None:
        runtime_context = runtime.context
        if not isinstance(runtime_context, dict):
            return None
        value = runtime_context.get(MEMORY_ARCHIVE_CONTEXT_KEY)
        return value if type(value) is SnipArchiveContext else None

    @staticmethod
    def _source_checkpoint_id(
        runtime: Runtime,
        archive_context: SnipArchiveContext | None,
    ) -> str | None:
        explicit_checkpoint_id = archive_context.source_checkpoint_id if archive_context is not None else None
        execution_info = getattr(runtime, "execution_info", None)
        runtime_checkpoint_id = getattr(execution_info, "checkpoint_id", None)
        if isinstance(runtime_checkpoint_id, str) and runtime_checkpoint_id:
            if explicit_checkpoint_id is not None and explicit_checkpoint_id != runtime_checkpoint_id:
                raise ValueError(
                    "SNIP archive runtime checkpoint does not match its explicit source",
                )
            return runtime_checkpoint_id
        if explicit_checkpoint_id is not None:
            return explicit_checkpoint_id
        try:
            configurable = get_config().get("configurable", {})
        except RuntimeError:
            return None
        value = configurable.get("checkpoint_id")
        return value if isinstance(value, str) and value else None

    def _receipt(
        self,
        prepared: _PreparedCompaction,
        tagged_text: str,
        runtime: Runtime,
    ) -> MemoryArchiveReceipt | None:
        archive_context = self._archive_context(runtime)
        if archive_context is None or not archive_context.enabled:
            return None
        thread_id = _resolve_thread_id(runtime)
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("SNIP archive Thread identity is unavailable")
        return build_memory_archive_receipt(
            archive_context,
            thread_id=thread_id,
            source_checkpoint_id=self._source_checkpoint_id(
                runtime,
                archive_context,
            ),
            previous_summary=prepared.previous_summary,
            messages=prepared.source_messages,
            tagged_text=tagged_text,
        )

    def compact_state(
        self,
        state: AgentState,
        runtime: Runtime,
        *,
        force: bool = False,
    ) -> ContextCompactionResult | None:
        prepared = self._prepare_compaction(state, force=force)
        if prepared is None:
            return None
        summary = self._summarize_with(
            list(prepared.snip_messages),
            previous_summary=prepared.previous_summary,
        )
        if summary is None:
            return None
        try:
            receipt = self._receipt(prepared, summary.tagged_text, runtime)
        except (SnipOutputInvalid, ValueError):
            logger.warning("SNIP receipt identity invalid; skipping compaction this turn")
            return None
        return ContextCompactionResult(
            summary_text=summary.continuity,
            messages_to_summarize=prepared.source_messages,
            preserved_messages=prepared.preserved_messages,
            total_tokens=prepared.total_tokens,
            memory_archive_receipt=receipt,
        )

    async def acompact_state(
        self,
        state: AgentState,
        runtime: Runtime,
        *,
        force: bool = False,
    ) -> ContextCompactionResult | None:
        prepared = self._prepare_compaction(state, force=force)
        if prepared is None:
            return None
        summary = await self._asummarize_with(
            list(prepared.snip_messages),
            previous_summary=prepared.previous_summary,
            authorization_context=runtime.context,
        )
        if summary is None:
            return None
        try:
            receipt = self._receipt(prepared, summary.tagged_text, runtime)
        except (SnipOutputInvalid, ValueError):
            logger.warning("SNIP receipt identity invalid; skipping compaction this turn")
            return None
        return ContextCompactionResult(
            summary_text=summary.continuity,
            messages_to_summarize=prepared.source_messages,
            preserved_messages=prepared.preserved_messages,
            total_tokens=prepared.total_tokens,
            memory_archive_receipt=receipt,
        )

    def _maybe_summarize(self, state: AgentState, runtime: Runtime) -> dict | None:
        result = self.compact_state(state, runtime, force=False)
        if result is None:
            return None
        return {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                *result.preserved_messages,
            ],
            "summary_text": result.summary_text,
            "memory_archive_receipt": result.memory_archive_receipt,
        }

    async def _amaybe_summarize(self, state: AgentState, runtime: Runtime) -> dict | None:
        result = await self.acompact_state(state, runtime, force=False)
        if result is None:
            return None
        return {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                *result.preserved_messages,
            ],
            "summary_text": result.summary_text,
            "memory_archive_receipt": result.memory_archive_receipt,
        }


def create_summarization_middleware(
    *,
    app_config: Any | None = None,
    keep: tuple[str, int | float] | None = None,
) -> DeerFlowSummarizationMiddleware | None:
    """Create the configured summarization middleware.

    Both the lead-agent automatic path and the manual context-compaction path
    use this factory so model resolution, prompt compatibility, and retention
    defaults cannot drift.
    """
    resolved_app_config = app_config or get_app_config()
    config = resolved_app_config.summarization

    if not config.enabled:
        return None

    trigger = None
    if config.trigger is not None:
        if isinstance(config.trigger, list):
            trigger = [item.to_tuple() for item in config.trigger]
        else:
            trigger = config.trigger.to_tuple()

    model = ModelRuntime(app_config=resolved_app_config).build_chat_model(
        profile=ModelRuntimeProfile.AGENT_GRAPH,
        model_name=config.model_name or None,
        thinking_enabled=False,
    )
    dual_output_contract = config.summary_prompt is None
    summary_prompt = SNIP_ARCHIVE_PROMPT if dual_output_contract else config.summary_prompt
    if dual_output_contract:
        model = _ensure_snip_summary_output_budget(model)
    model = model.with_config(tags=["middleware:summarize"])

    requested_keep = keep or config.keep.to_tuple()
    compact_all_complete_turns = requested_keep[0] == "messages" and requested_keep[1] == 0
    effective_keep = ("messages", 1) if compact_all_complete_turns else requested_keep
    kwargs: dict[str, Any] = {
        "model": model,
        "trigger": trigger,
        "keep": effective_keep,
        "compact_all_complete_turns": compact_all_complete_turns,
        "summary_prompt": summary_prompt,
        "dual_output_contract": dual_output_contract,
    }
    if config.trim_tokens_to_summarize is not None:
        kwargs["trim_tokens_to_summarize"] = config.trim_tokens_to_summarize

    return DeerFlowSummarizationMiddleware(**kwargs)
