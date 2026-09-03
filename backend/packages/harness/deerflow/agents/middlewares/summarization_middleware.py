"""Summarization middleware extensions for ActWeave."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any, Literal, NotRequired, TypedDict, cast, override

from langchain.agents import AgentState
from langchain.agents.middleware import SummarizationMiddleware
from langchain_core.messages import (
    AnyMessage,
    HumanMessage,
    RemoveMessage,
)
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.config import get_config
from langgraph.constants import TAG_NOSTREAM
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime

from deerflow.agents.context_compaction_warning import (
    CONTEXT_COMPACTION_WARNING_STATE_KEY,
    ContextCompactionFailureReason,
    ContextCompactionMiddlewareState,
    clear_context_compaction_warning,
    context_compaction_warning_update,
)
from deerflow.agents.memory.snip import (
    SNIP_ARCHIVE_PROMPT,
    SNIP_RETRY_REINFORCEMENT,
    MemoryArchiveReceipt,
    SnipOutputInvalid,
    parse_snip_dual_output,
    validate_snip_output,
)
from deerflow.agents.middlewares.compaction_receipts import (  # noqa: F401 - compatibility exports
    ContextCompactionResult,
    _resolve_thread_id,
    acontext_compaction_update,
    build_compaction_receipt,
    context_compaction_update,
    require_receipt_preconditions,
)
from deerflow.agents.middlewares.provider_request_measurement import (
    measure_profile_snapshot_context,
)
from deerflow.agents.middlewares.provider_request_profile import (
    ProviderRequestContextMeasurement,
    ProviderRequestProfile,
    ProviderRequestUsageUnsupported,
)
from deerflow.agents.middlewares.snip_planner import (  # noqa: F401 - compatibility exports
    MAX_SNIP_HIERARCHICAL_LEAVES,
    MAX_SNIP_HIERARCHICAL_MODEL_CALLS,
    MIN_SNIP_SUMMARY_OUTPUT_TOKENS,
    SnipCompactionFailed,
    SnipModelOutputInvalid,
    SnipPromptBudget,
    SnipPromptBudgetTooSmall,
    SnipSourceTooLarge,
    _ensure_snip_summary_output_budget,
    _ProjectionField,
    _ProjectionFragment,
    _SnipPromptPlan,
    _SnipReductionStep,
    _SnipSummary,
    build_snip_prompt_plan,
    build_summary_prompt,
    plan_reduction_step,
)
from deerflow.agents.middlewares.turn_compaction import (  # noqa: F401 - compatibility exports
    _ASK_CLARIFICATION_TOOL_NAME,
    _SUMMARY_TRIGGER_MESSAGE_NAME,
    _PreparedCompaction,
    complete_turn_ranges,
    context_progress,
    messages_for_trigger_count,
    summary_count_message,
)
from deerflow.agents.middlewares.turn_compaction import (
    candidate_cutoffs as _turn_candidate_cutoffs,
)
from deerflow.agents.middlewares.turn_compaction import (
    snip_messages as _turn_snip_messages,
)
from deerflow.agents.provider_request_contract import (
    PROVIDER_REQUEST_PROFILE_STATE_KEY,
)
from deerflow.config.app_config import get_app_config
from deerflow.config.summarization_config import (
    MIN_TRIM_TOKENS_TO_SUMMARIZE,
    EffectiveCompactionPolicy,
    effective_compaction_trigger_tokens,
    resolve_effective_compaction_policy,
    validate_summary_prompt_template,
)
from deerflow.models import ModelRuntime, ModelRuntimeProfile
from deerflow.models.runtime import AsyncAbortEvent
from deerflow.runtime.context_keys import RuntimeContextKeys
from deerflow.sandbox.sandbox import AuthorizationRevoked, check_authorization_boundary

logger = logging.getLogger(__name__)


def _context_model_token_counter(model: Any) -> Any:
    """Mirror LangChain's public approximate counter tuning for the Lead model."""

    llm_type = getattr(model, "_llm_type", "")
    if isinstance(llm_type, str) and llm_type.startswith("anthropic-chat"):
        return partial(
            count_tokens_approximately,
            use_usage_metadata_scaling=True,
            chars_per_token=3.3,
        )
    return partial(
        count_tokens_approximately,
        use_usage_metadata_scaling=True,
    )


def _server_abort_event(runtime_context: object | None) -> AsyncAbortEvent | None:
    if not isinstance(runtime_context, Mapping):
        return None
    candidate = runtime_context.get(RuntimeContextKeys.SERVER_ABORT_EVENT)
    if not callable(getattr(candidate, "is_set", None)) or not callable(
        getattr(candidate, "wait", None),
    ):
        return None
    return cast(AsyncAbortEvent, candidate)


class ContextTriggerUsage(TypedDict):
    """The configured token trigger measured against the retained context."""

    type: Literal["tokens"]
    configured_value: int | float
    current_value: int | float
    threshold_value: int | float
    remaining_value: int | float
    progress_percent: float
    reached: bool
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


class DeerFlowSummarizationMiddleware(SummarizationMiddleware):
    """SNIP compaction that removes only complete conversation turns."""

    state_schema = ContextCompactionMiddlewareState

    def __init__(
        self,
        *args,
        compact_all_complete_turns: bool = False,
        context_model: Any | None = None,
        context_compaction_observer: object | None = None,
        dual_output_contract: bool | None = None,
        **kwargs,
    ) -> None:
        # The summary model owns SNIP generation only. Retained-context
        # reporting must use the Lead model that will receive the provider
        # request.
        self._context_model = context_model
        self._context_compaction_observer = context_compaction_observer
        requested_token_counter = kwargs.get(
            "token_counter",
            count_tokens_approximately,
        )
        use_context_model_counter = context_model is not None and requested_token_counter is count_tokens_approximately
        super().__init__(*args, **kwargs)
        if use_context_model_counter:
            # Keep the framework's provider-specific approximation semantics,
            # but bind them to the model that receives the Lead request rather
            # than the separate model that generates the SNIP summary.
            self.token_counter = _context_model_token_counter(context_model)
            self._partial_token_counter = partial(
                self.token_counter,
                use_usage_metadata_scaling=False,
            )
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

    @override
    def _get_profile_limits(self) -> int | None:
        model = self._context_model
        if model is None:
            return super()._get_profile_limits()
        profile = getattr(model, "profile", None)
        if not isinstance(profile, Mapping):
            return None
        max_input_tokens = profile.get("max_input_tokens")
        return max_input_tokens if isinstance(max_input_tokens, int) else None

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

    def _prompt_within_budget(self, prompt: str) -> bool:
        if self.trim_tokens_to_summarize is None:
            return True
        return self.token_counter([HumanMessage(content=prompt)]) <= max(
            1,
            self.trim_tokens_to_summarize,
        )

    def _prompt_with_repair_within_budget(self, prompt: str) -> bool:
        """Reserve room for the fixed bounded repair before planning a leaf."""

        return self._prompt_within_budget(
            f"{prompt}\n\n{SNIP_RETRY_REINFORCEMENT}",
        )

    def _invoke_snip_prompt(
        self,
        prompt: str,
        *,
        call_budget: list[int],
    ) -> _SnipSummary | None:
        for attempt_index, attempt_prompt in enumerate(
            (prompt, f"{prompt}\n\n{SNIP_RETRY_REINFORCEMENT}"),
            start=1,
        ):
            if not self._prompt_within_budget(attempt_prompt):
                logger.warning(
                    "SNIP retry prompt exceeds configured token budget",
                )
                raise SnipPromptBudgetTooSmall
            if call_budget[0] >= MAX_SNIP_HIERARCHICAL_MODEL_CALLS:
                logger.warning("SNIP hierarchical model-call budget exhausted")
                raise SnipSourceTooLarge
            call_budget[0] += 1
            try:
                response = ModelRuntime.invoke_runnable(
                    self._summary_model,
                    attempt_prompt,
                    profile=ModelRuntimeProfile.AGENT_GRAPH,
                    config={"metadata": {"lc_source": "summarization"}},
                )
                return self._parse_snip_response(response.text)
            except SnipOutputInvalid as exc:
                logger.warning(
                    "SNIP model output failed validation on attempt %d/2: %s",
                    attempt_index,
                    exc,
                )
                continue
            except Exception as exc:
                logger.exception("SNIP generation failed; skipping compaction this turn")
                raise SnipCompactionFailed from exc
        logger.warning("SNIP model returned invalid output twice; skipping compaction this turn")
        raise SnipModelOutputInvalid

    async def _ainvoke_snip_prompt(
        self,
        prompt: str,
        *,
        authorization_context: object | None,
        call_budget: list[int],
    ) -> _SnipSummary | None:
        for attempt_index, attempt_prompt in enumerate(
            (prompt, f"{prompt}\n\n{SNIP_RETRY_REINFORCEMENT}"),
            start=1,
        ):
            if not self._prompt_within_budget(attempt_prompt):
                logger.warning(
                    "SNIP retry prompt exceeds configured token budget",
                )
                raise SnipPromptBudgetTooSmall
            if call_budget[0] >= MAX_SNIP_HIERARCHICAL_MODEL_CALLS:
                logger.warning("SNIP hierarchical model-call budget exhausted")
                raise SnipSourceTooLarge
            call_budget[0] += 1
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
            except SnipOutputInvalid as exc:
                logger.warning(
                    "SNIP model output failed validation on attempt %d/2: %s",
                    attempt_index,
                    exc,
                )
                continue
            except Exception as exc:
                logger.exception("SNIP generation failed; skipping compaction this turn")
                raise SnipCompactionFailed from exc
        logger.warning("SNIP model returned invalid output twice; skipping compaction this turn")
        raise SnipModelOutputInvalid

    def _snip_prompt_budget(self) -> SnipPromptBudget:
        return SnipPromptBudget(
            summary_prompt=self.summary_prompt,
            dual_output_contract=self._dual_output_contract,
            prompt_within_budget=self._prompt_within_budget,
            prompt_with_repair_within_budget=self._prompt_with_repair_within_budget,
        )

    def _plan_reduction_step(self, summaries: list[_SnipSummary], *, previous_summary: str | None) -> _SnipReductionStep:
        return plan_reduction_step(self._snip_prompt_budget(), summaries, previous_summary=previous_summary)

    def _build_snip_prompt_plan(self, messages_to_summarize: list[AnyMessage], *, previous_summary: str | None) -> _SnipPromptPlan | None:
        return build_snip_prompt_plan(self._snip_prompt_budget(), messages_to_summarize, previous_summary=previous_summary)

    def _build_summary_prompt(self, messages_to_summarize: list[AnyMessage], previous_summary: str | None = None) -> str | None:
        return build_summary_prompt(self._snip_prompt_budget(), messages_to_summarize, previous_summary=previous_summary)

    def _reduce_snip_summaries(
        self,
        summaries: list[_SnipSummary],
        *,
        previous_summary: str | None,
        call_budget: list[int],
    ) -> _SnipSummary | None:
        current = summaries
        while True:
            if len(current) == 1 and previous_summary is None:
                return current[0]
            step = self._plan_reduction_step(
                current,
                previous_summary=previous_summary,
            )
            reduced: list[_SnipSummary] = []
            for prompt in step.prompts:
                result = self._invoke_snip_prompt(
                    prompt,
                    call_budget=call_budget,
                )
                if result is None:
                    return None
                reduced.append(result)
            if step.final:
                return reduced[0]
            current = reduced

    async def _areduce_snip_summaries(
        self,
        summaries: list[_SnipSummary],
        *,
        previous_summary: str | None,
        authorization_context: object | None,
        call_budget: list[int],
    ) -> _SnipSummary | None:
        current = summaries
        while True:
            if len(current) == 1 and previous_summary is None:
                return current[0]
            step = self._plan_reduction_step(
                current,
                previous_summary=previous_summary,
            )
            reduced: list[_SnipSummary] = []
            for prompt in step.prompts:
                result = await self._ainvoke_snip_prompt(
                    prompt,
                    authorization_context=authorization_context,
                    call_budget=call_budget,
                )
                if result is None:
                    return None
                reduced.append(result)
            if step.final:
                return reduced[0]
            current = reduced

    def _summarize_with(self, messages_to_summarize: list[AnyMessage], previous_summary: str | None = None) -> _SnipSummary | None:
        """Mirror the parent ``_create_summary`` but invoke the nostream-tagged model.

        We do not swap ``self.model`` at the instance level: the agent/middleware is
        cached and reused across concurrent runs, so a temporary swap would leak the
        ``RunnableBinding`` to other coroutines during ``await`` and break parent logic
        that inspects the raw model (``profile`` / ``_get_ls_params``).
        """
        if not messages_to_summarize:
            return None
        plan = self._build_snip_prompt_plan(
            messages_to_summarize,
            previous_summary=previous_summary,
        )
        if plan is None:
            return None
        call_budget = [0]
        summaries: list[_SnipSummary] = []
        for prompt in plan.prompts:
            summary = self._invoke_snip_prompt(
                prompt,
                call_budget=call_budget,
            )
            if summary is None:
                return None
            summaries.append(summary)
        if not plan.hierarchical:
            return summaries[0]
        return self._reduce_snip_summaries(
            summaries,
            previous_summary=previous_summary,
            call_budget=call_budget,
        )

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
        plan = self._build_snip_prompt_plan(
            messages_to_summarize,
            previous_summary=previous_summary,
        )
        if plan is None:
            return None
        call_budget = [0]
        summaries: list[_SnipSummary] = []
        for prompt in plan.prompts:
            summary = await self._ainvoke_snip_prompt(
                prompt,
                authorization_context=authorization_context,
                call_budget=call_budget,
            )
            if summary is None:
                return None
            summaries.append(summary)
        if not plan.hierarchical:
            return summaries[0]
        return await self._areduce_snip_summaries(
            summaries,
            previous_summary=previous_summary,
            authorization_context=authorization_context,
            call_budget=call_budget,
        )

    _summary_count_message = staticmethod(summary_count_message)
    _messages_for_trigger_count = staticmethod(messages_for_trigger_count)
    _context_progress = staticmethod(context_progress)
    _complete_turn_ranges = staticmethod(complete_turn_ranges)
    _candidate_cutoffs = staticmethod(_turn_candidate_cutoffs)
    _snip_messages = staticmethod(_turn_snip_messages)

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
        triggers, primary_trigger = self._measure_trigger_usage(
            token_value=estimated_tokens,
            reported_token_messages=trigger_messages,
        )
        return ContextUsageMeasurement(
            estimated_tokens=estimated_tokens,
            message_count=message_count,
            summary_present=bool(summary_text),
            context_window_tokens=context_window_tokens,
            triggers=triggers,
            primary_trigger=primary_trigger,
        )

    def _measure_trigger_usage(
        self,
        *,
        token_value: int,
        reported_token_messages: list[AnyMessage] | None = None,
    ) -> tuple[tuple[ContextTriggerUsage, ...], ContextTriggerUsage | None]:
        triggers: list[ContextTriggerUsage] = []
        for trigger_type, configured_value in self._trigger_conditions:
            if trigger_type != "tokens":
                raise ValueError("Summarization triggers support tokens only")
            threshold_tokens = int(configured_value)
            reached = token_value >= threshold_tokens or (
                reported_token_messages is not None
                and self._should_summarize_based_on_reported_tokens(
                    reported_token_messages,
                    float(threshold_tokens),
                )
            )
            triggers.append(
                ContextTriggerUsage(
                    type="tokens",
                    configured_value=threshold_tokens,
                    current_value=token_value,
                    threshold_value=threshold_tokens,
                    remaining_value=max(0, threshold_tokens - token_value),
                    progress_percent=self._context_progress(
                        token_value,
                        threshold_tokens,
                    ),
                    reached=reached,
                    threshold_tokens=threshold_tokens,
                )
            )

        return tuple(triggers), max(
            triggers,
            key=lambda trigger: trigger["progress_percent"],
            default=None,
        )

    def measure_provider_request_usage(
        self,
        measurement: ProviderRequestContextMeasurement,
        *,
        summary_present: bool,
        context_window_tokens: int | None,
    ) -> ContextUsageMeasurement:
        """Expose the exact safety value used by automatic profile triggers."""

        if context_window_tokens is not None and context_window_tokens <= 0:
            raise ValueError("Model max_input_tokens must be positive")
        triggers, primary_trigger = self._measure_trigger_usage(
            token_value=measurement.safety_bound_tokens,
        )
        return ContextUsageMeasurement(
            estimated_tokens=measurement.estimated_tokens,
            message_count=measurement.message_count,
            summary_present=summary_present,
            context_window_tokens=context_window_tokens,
            triggers=triggers,
            primary_trigger=primary_trigger,
        )

    def _count_rendered_summary_prompt_tokens(self, input_text: str) -> int | None:
        """Count the exact rendered prompt without exposing its durable contents."""
        try:
            validate_summary_prompt_template(self.summary_prompt)
            rendered_prompt = self.summary_prompt.format(messages=input_text).rstrip()
            return self.token_counter([HumanMessage(content=rendered_prompt)])
        except Exception:
            logger.debug("Failed to count rendered summary prompt; skipping compaction safely", exc_info=True)
            return None

    def before_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._maybe_summarize(state, runtime)

    async def abefore_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return await self._amaybe_summarize(state, runtime)

    def _requested_cutoff(self, messages: list[AnyMessage]) -> int:
        if self._compact_all_complete_turns:
            return len(messages)
        return self._determine_cutoff_index(messages)

    def _provider_safe_retention_cutoff(
        self,
        state: Mapping[str, object],
        profile_snapshot: Mapping[str, object],
        *,
        protect_latest_complete_turn: bool,
    ) -> int | None:
        """Choose the least aggressive whole-turn cutoff safe for dispatch.

        The configured ``keep`` remains the approximate recent-history target,
        but it is not a Provider-wire upper bound: one character may occupy
        several UTF-8 bytes, each message and image has adapter framing, and a
        single retained message can itself exceed keep.  A candidate therefore
        qualifies only when both the approximate keep target and the frozen
        Provider profile agree, with room for the replacement summary.
        """

        raw_messages = state.get("messages")
        if not isinstance(raw_messages, list) or self.keep[0] != "tokens":
            return None
        messages = cast("list[AnyMessage]", raw_messages)
        trigger_tokens = [int(value) for trigger_type, value in self._trigger_conditions if trigger_type == "tokens"]
        if not trigger_tokens:
            return None
        threshold = min(trigger_tokens)
        complete_turns = self._complete_turn_ranges(messages)
        if protect_latest_complete_turn and complete_turns:
            protected_latest = complete_turns[-1]
            complete_turns = complete_turns[:-1]
            if protected_latest[1] < len(messages):
                # An open follow-up makes the previous complete turn eligible
                # only as the last-resort capacity candidate. This preserves
                # the normal continuity rule while retaining the existing
                # oversized-turn progress contract.
                complete_turns = (*complete_turns, protected_latest)

        expected_start = 0
        for start, end in complete_turns:
            if start != expected_start:
                break
            expected_start = end
            preserved = messages[end:]
            if self._partial_token_counter(preserved) > int(self.keep[1]):
                continue
            projected_state = dict(state)
            projected_state["messages"] = preserved
            projected_state["summary_text"] = None
            try:
                measurement = measure_profile_snapshot_context(
                    profile_snapshot,
                    projected_state,
                )
            except ProviderRequestUsageUnsupported:
                return None
            if measurement.safety_bound_tokens + MIN_SNIP_SUMMARY_OUTPUT_TOKENS < threshold:
                return end
        return None

    def _compacted_result_fits_provider_profile(
        self,
        state: Mapping[str, object],
        result: ContextCompactionResult,
    ) -> bool:
        """Recheck the generated summary at the same Provider boundary."""

        profile_snapshot = state.get(PROVIDER_REQUEST_PROFILE_STATE_KEY)
        if not isinstance(profile_snapshot, Mapping):
            return True
        projected_state = dict(state)
        projected_state["messages"] = list(result.preserved_messages)
        projected_state["summary_text"] = result.summary_text
        try:
            measurement = measure_profile_snapshot_context(
                profile_snapshot,
                projected_state,
            )
        except ProviderRequestUsageUnsupported:
            return False
        return all(trigger_type != "tokens" or measurement.safety_bound_tokens < int(configured_value) for trigger_type, configured_value in self._trigger_conditions)

    def _overbudget_progress_cutoff(
        self,
        messages: list[AnyMessage],
        *,
        previous_summary: str | None,
        preserve_latest_complete_turn: bool,
    ) -> int | None:
        """Choose one safe prefix when keep blocks requested compaction."""

        chronological_cutoffs = tuple(
            reversed(
                self._candidate_cutoffs(
                    messages,
                    len(messages),
                )
            )
        )
        previous_fitting_cutoff: int | None = None
        for cutoff_index in chronological_cutoffs:
            snip_messages = self._snip_messages(messages[:cutoff_index])
            if not snip_messages:
                continue
            if (
                self._build_summary_prompt(
                    snip_messages,
                    previous_summary=previous_summary,
                )
                is None
            ):
                candidate = previous_fitting_cutoff or cutoff_index
                if preserve_latest_complete_turn and candidate >= len(messages):
                    # Automatic compaction protects a lone latest complete turn
                    # until a follow-up exists. Explicit force requests may
                    # compact that turn immediately.
                    return None
                return candidate
            previous_fitting_cutoff = cutoff_index
        return None

    def _profile_trigger_reached(
        self,
        profile_snapshot: object,
        measurement: ProviderRequestContextMeasurement,
    ) -> bool:
        """Evaluate the automatic token trigger from the safety measurement."""

        if not isinstance(profile_snapshot, Mapping):
            return False
        for trigger_type, configured_value in self._trigger_conditions:
            if trigger_type != "tokens":
                raise ValueError("Summarization triggers support tokens only")
            if measurement.safety_bound_tokens >= int(configured_value):
                return True
        return False

    def fixed_component_over_trigger(
        self,
        profile_snapshot: object,
        measurement: ProviderRequestContextMeasurement,
    ) -> bool:
        """Return whether immutable request material alone crosses a token trigger."""

        if not isinstance(profile_snapshot, Mapping):
            return False
        fixed = measurement.components.get("fixed")
        if fixed is None:
            return False
        return any(trigger_type == "tokens" and fixed.safety_bound_tokens >= int(configured_value) for trigger_type, configured_value in self._trigger_conditions)

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
        profile_measurement: ProviderRequestContextMeasurement | None = None
        profile_snapshot = state.get(PROVIDER_REQUEST_PROFILE_STATE_KEY)
        if not force and isinstance(profile_snapshot, Mapping):
            try:
                profile_measurement = measure_profile_snapshot_context(
                    profile_snapshot,
                    state,
                )
            except ProviderRequestUsageUnsupported as exc:
                # Approximate fallback cannot prove a Provider-safe cutoff.
                # The automatic hook maps this typed failure to a one-turn
                # warning; the final Provider guard still owns dispatch.
                raise SnipCompactionFailed(
                    reason=(ContextCompactionFailureReason.CHECKPOINT_UNMEASURABLE),
                ) from exc
        total_tokens = profile_measurement.safety_bound_tokens if profile_measurement is not None else self.token_counter(trigger_messages)
        should_summarize = self._profile_trigger_reached(profile_snapshot, profile_measurement) if profile_measurement is not None else self._should_summarize(trigger_messages, total_tokens)
        if not force and not should_summarize:
            return None
        if (
            not force
            and profile_measurement is not None
            and self.fixed_component_over_trigger(
                profile_snapshot,
                profile_measurement,
            )
        ):
            return None

        provider_profile_qualified = not force and profile_measurement is not None and isinstance(profile_snapshot, Mapping)
        if provider_profile_qualified:
            provider_cutoff = self._provider_safe_retention_cutoff(
                state,
                profile_snapshot,
                protect_latest_complete_turn=True,
            )
            candidate_cutoffs = (provider_cutoff,) if provider_cutoff is not None else ()
        else:
            requested_cutoff = self._requested_cutoff(messages)
            candidate_cutoffs = (
                self._candidate_cutoffs(
                    messages,
                    requested_cutoff,
                    protect_latest_complete_turn=not force,
                )
                if requested_cutoff > 0
                else ()
            )
        if not candidate_cutoffs and not provider_profile_qualified:
            progress_cutoff = self._overbudget_progress_cutoff(
                messages,
                previous_summary=previous_summary,
                preserve_latest_complete_turn=not force,
            )
            if progress_cutoff is not None:
                # Honor keep when possible. Once a complete prefix cannot fit
                # one direct prompt, however, first archive the largest earlier
                # fitting prefix (or hierarchically project the oversized head)
                # so a reached token trigger cannot stall forever.
                candidate_cutoffs = (progress_cutoff,)
        if not candidate_cutoffs:
            return None
        for cutoff_index in candidate_cutoffs:
            source_messages = messages[:cutoff_index]
            snip_messages = self._snip_messages(source_messages)
            if not snip_messages:
                continue
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

        # No whole-turn prefix fits one prompt. The packaged dual-output SNIP
        # contract projects exactly the oldest eligible turn so automatic,
        # manual, Seal, and Prepare paths make bounded pass-by-pass progress.
        # A custom prompt has no safe reduction contract and terminates with a
        # typed source error instead of retrying the same impossible input.
        for cutoff_index in reversed(candidate_cutoffs):
            source_messages = messages[:cutoff_index]
            snip_messages = self._snip_messages(source_messages)
            if not snip_messages:
                continue
            plan = self._build_snip_prompt_plan(
                snip_messages,
                previous_summary=previous_summary,
            )
            if plan is None:
                continue
            return _PreparedCompaction(
                source_messages=tuple(source_messages),
                snip_messages=tuple(snip_messages),
                preserved_messages=tuple(messages[cutoff_index:]),
                previous_summary=previous_summary,
                total_tokens=total_tokens,
            )
        return None

    def _receipt(self, prepared: _PreparedCompaction, tagged_text: str, runtime: Runtime) -> MemoryArchiveReceipt | None:
        return build_compaction_receipt(prepared, tagged_text, runtime)

    def _require_receipt_preconditions(self, state: AgentState, runtime: Runtime, *, asynchronous: bool) -> None:
        require_receipt_preconditions(self._context_compaction_observer, state, runtime, asynchronous=asynchronous)

    def _context_compaction_update(self, state: AgentState, result: ContextCompactionResult, runtime: Runtime) -> dict[str, object]:
        return context_compaction_update(self._context_compaction_observer, state, result, runtime)

    async def _acontext_compaction_update(self, state: AgentState, result: ContextCompactionResult, runtime: Runtime) -> dict[str, object]:
        return await acontext_compaction_update(self._context_compaction_observer, self.token_counter, state, result, runtime)

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
        self._require_receipt_preconditions(
            state,
            runtime,
            asynchronous=False,
        )
        try:
            summary = self._summarize_with(
                list(prepared.snip_messages),
                previous_summary=prepared.previous_summary,
            )
        except SnipCompactionFailed:
            if force:
                raise
            return None
        if summary is None:
            return None
        try:
            receipt = self._receipt(prepared, summary.tagged_text, runtime)
        except (SnipOutputInvalid, ValueError) as exc:
            logger.warning("SNIP receipt identity invalid; skipping compaction this turn")
            if force:
                raise SnipCompactionFailed from exc
            return None
        result = ContextCompactionResult(
            summary_text=summary.continuity,
            messages_to_summarize=prepared.source_messages,
            preserved_messages=prepared.preserved_messages,
            total_tokens=prepared.total_tokens,
            memory_archive_receipt=receipt,
        )
        if not force and not self._compacted_result_fits_provider_profile(
            state,
            result,
        ):
            logger.warning(
                "Compacted Context still exceeds the frozen Provider profile; skipping unsafe result",
            )
            return None
        return result

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
        self._require_receipt_preconditions(
            state,
            runtime,
            asynchronous=True,
        )
        try:
            summary = await self._asummarize_with(
                list(prepared.snip_messages),
                previous_summary=prepared.previous_summary,
                authorization_context=runtime.context,
            )
        except SnipCompactionFailed:
            if force:
                raise
            return None
        if summary is None:
            return None
        try:
            receipt = self._receipt(prepared, summary.tagged_text, runtime)
        except (SnipOutputInvalid, ValueError) as exc:
            logger.warning("SNIP receipt identity invalid; skipping compaction this turn")
            if force:
                raise SnipCompactionFailed from exc
            return None
        result = ContextCompactionResult(
            summary_text=summary.continuity,
            messages_to_summarize=prepared.source_messages,
            preserved_messages=prepared.preserved_messages,
            total_tokens=prepared.total_tokens,
            memory_archive_receipt=receipt,
        )
        if not force and not self._compacted_result_fits_provider_profile(
            state,
            result,
        ):
            logger.warning(
                "Compacted Context still exceeds the frozen Provider profile; skipping unsafe result",
            )
            return None
        return result

    def _maybe_summarize(self, state: AgentState, runtime: Runtime) -> dict | None:
        try:
            result = self.compact_state(state, runtime, force=False)
            if result is None:
                return clear_context_compaction_warning(state)
            context_update = self._context_compaction_update(
                state,
                result,
                runtime,
            )
        except (SnipCompactionFailed, SnipPromptBudgetTooSmall, SnipSourceTooLarge) as error:
            return context_compaction_warning_update(error.reason)
        return {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                *result.preserved_messages,
            ],
            "summary_text": result.summary_text,
            "memory_archive_receipt": result.memory_archive_receipt,
            CONTEXT_COMPACTION_WARNING_STATE_KEY: None,
            **context_update,
        }

    async def _amaybe_summarize(self, state: AgentState, runtime: Runtime) -> dict | None:
        try:
            result = await self.acompact_state(state, runtime, force=False)
            if result is None:
                return clear_context_compaction_warning(state)
            context_update = await self._acontext_compaction_update(
                state,
                result,
                runtime,
            )
        except (SnipCompactionFailed, SnipPromptBudgetTooSmall, SnipSourceTooLarge) as error:
            return context_compaction_warning_update(error.reason)
        return {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                *result.preserved_messages,
            ],
            "summary_text": result.summary_text,
            "memory_archive_receipt": result.memory_archive_receipt,
            CONTEXT_COMPACTION_WARNING_STATE_KEY: None,
            **context_update,
        }


def freeze_summarization_profile(
    middlewares: Sequence[object],
    profile: ProviderRequestProfile,
) -> EffectiveCompactionPolicy | None:
    """Bind the one compactor to the final frozen Provider request profile."""

    compactors = tuple(middleware for middleware in middlewares if isinstance(middleware, DeerFlowSummarizationMiddleware))
    if not compactors:
        return None
    if len(compactors) != 1:
        raise ValueError("Only one summarization middleware may be installed")
    compactor = compactors[0]
    token_triggers = [int(value) for trigger_type, value in compactor._trigger_conditions if trigger_type == "tokens"]
    if not token_triggers or compactor.keep[0] != "tokens":
        return None
    snapshot = profile.snapshot()
    baseline = measure_profile_snapshot_context(
        snapshot,
        {"messages": []},
    )
    policy = resolve_effective_compaction_policy(
        trigger_tokens=min(token_triggers),
        keep_tokens=int(compactor.keep[1]),
        context_window_tokens=profile.max_input_tokens,
        # The profile measurement is already the Provider safety upper bound:
        # it includes exact fixed system/tool material, bounded overlays,
        # provider framing, and the adapter's error allowance. The packaged
        # dual-output contract reserves its existing 4096-token output budget
        # for the replacement summary rather than inventing another margin.
        fixed_noncompressible_safety_tokens=baseline.safety_bound_tokens,
        summary_headroom_tokens=MIN_SNIP_SUMMARY_OUTPUT_TOKENS,
        # ``keep`` is a character-counter selection target, not a finite
        # Provider-wire upper bound (single-message, per-message, visual, and
        # serialization costs are content dependent). Runtime candidate
        # selection remeasures each retained tail with this frozen profile.
        retained_context_safety_tokens=0,
    )
    trigger = ("tokens", policy.trigger_tokens) if policy.trigger_tokens is not None else None
    compactor.trigger = trigger
    compactor._trigger_clauses = compactor._normalize_trigger(trigger)
    compactor._trigger_conditions = compactor._legacy_trigger_conditions(trigger)
    freeze_observer = getattr(
        compactor._context_compaction_observer,
        "freeze_compaction_policy",
        None,
    )
    if callable(freeze_observer):
        freeze_observer(policy)
    return policy


def create_summarization_middleware(
    *,
    app_config: Any | None = None,
    context_model: Any | None = None,
    keep: tuple[str, int] | None = None,
    context_compaction_observer: object | None = None,
) -> DeerFlowSummarizationMiddleware | None:
    """Create the configured summarization middleware.

    Both the lead-agent automatic path and the manual context-compaction path
    use this factory so model resolution, prompt compatibility, and retention
    defaults cannot drift.

    ``keep`` overrides the policy retention for one compaction. The policy and
    the public compact API are token-count-only (``("tokens", n)``); the sole
    non-token value is the internal ``("messages", 0)`` archive-all sentinel
    that manual compaction, Seal, and Dream pass for force drains.
    """
    resolved_app_config = app_config or get_app_config()
    config = resolved_app_config.summarization

    if not config.enabled:
        return None

    requested_keep: tuple[str, int] = keep or config.keep.to_tuple()
    compact_all_complete_turns = requested_keep[0] == "messages" and requested_keep[1] == 0
    effective_keep = ("messages", 1) if compact_all_complete_turns else requested_keep

    # Automatic summarization triggers on exactly one estimated-token
    # threshold; message-count and context-fraction triggers were removed.
    # The absolute policy value is clamped to the context model's declared
    # capacity so a small-context model cannot sit in the dead zone where the
    # final Provider guard rejects before the trigger is ever reached.
    context_profile = getattr(context_model, "profile", None)
    context_window_tokens = context_profile.get("max_input_tokens") if isinstance(context_profile, Mapping) else None
    if requested_keep[0] == "tokens":
        initial_policy = resolve_effective_compaction_policy(
            trigger_tokens=config.trigger_tokens,
            keep_tokens=requested_keep[1],
            context_window_tokens=(context_window_tokens if isinstance(context_window_tokens, int) else None),
        )
        trigger_tokens = initial_policy.trigger_tokens
    else:
        trigger_tokens = effective_compaction_trigger_tokens(
            config.trigger_tokens,
            context_window_tokens if isinstance(context_window_tokens, int) else None,
        )
    trigger = ("tokens", trigger_tokens) if trigger_tokens is not None else None

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

    kwargs: dict[str, Any] = {
        "model": model,
        "context_model": context_model,
        "context_compaction_observer": context_compaction_observer,
        "trigger": trigger,
        "keep": effective_keep,
        "compact_all_complete_turns": compact_all_complete_turns,
        "summary_prompt": summary_prompt,
        "dual_output_contract": dual_output_contract,
    }
    if config.trim_tokens_to_summarize is not None:
        # Values below the packaged-prompt floor could never plan a compaction
        # and previously terminated every triggered Thread. Authoring rejects
        # them; clamp here so legacy stored values stay operable.
        kwargs["trim_tokens_to_summarize"] = max(
            config.trim_tokens_to_summarize,
            MIN_TRIM_TOKENS_TO_SUMMARIZE,
        )

    return DeerFlowSummarizationMiddleware(**kwargs)
