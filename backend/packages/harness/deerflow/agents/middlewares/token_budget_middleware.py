"""Middleware to enforce per-run token budget limits.
Tracks cumulative token usage (input, output, total) across model calls within
a single agent run and enforces configurable soft-warning and hard-stop
thresholds.
Detection strategy:
  1. After each model response, account for the parent model's immutable
     usage baseline and read Sub-Agent Task receipts directly from ToolMessages.
     This does not depend on middleware ordering: TokenUsageMiddleware may
     persist those receipts onto the dispatch AIMessage later in the same hook.
  2. Persist the cumulative per-Run baseline in a private checkpoint channel,
     so a recreated Job Attempt neither resets nor publicly reports the budget.
  3. If the highest fraction (input, output, or total) >= warn_threshold,
     queue a warning.
  4. If the highest fraction >= hard_stop_threshold, strip tool_calls.
Warning injection uses the deferred pattern:
  - after_model queues the warning (does NOT mutate state).
  - wrap_model_call injects it as a HumanMessage at the next model call.
This preserves AIMessage(tool_calls) → ToolMessage pairing.

Stop-reason surfacing (#3875 Phase 2):
  The hard stop does NOT raise — it strips tool_calls so the agent loop
  terminates naturally and produces a final answer. To let the caller (e.g.
  the subagent executor) distinguish a budget-capped completion from a clean
  one, the run that triggered the hard stop is recorded in ``_stop_reason``
  and exposed via :meth:`consume_stop_reason`. That dict is intentionally NOT
  cleared by ``after_agent``/``_clear_run_state`` so the executor can read it
  after the run returns; the bounded dict prevents unbounded growth on
  abandoned runs, and each subagent run builds a fresh middleware instance so
  there is no cross-run contamination.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal, NotRequired, TypedDict, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    ModelCallResult,
    ModelRequest,
    ModelResponse,
    PrivateStateAttr,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.runtime import Runtime

from deerflow.agents.middlewares._bounded_dict import BoundedDict
from deerflow.config.token_budget_config import TokenBudgetConfig
from deerflow.subagents.status_contract import (
    SUBAGENT_USAGE_RECEIPT_STATE_KEY,
    read_subagent_usage_receipt,
    read_subagent_usage_receipt_state,
)
from deerflow.token_budget_usage import (
    TOKEN_BUDGET_USAGE_RECORDER_CONTEXT_KEY,
    TokenBudgetUsageConflict,
    TokenBudgetUsageRecorder,
    TokenBudgetUsageSnapshot,
    dominant_token_budget_usage,
)

logger = logging.getLogger(__name__)

_BUDGET_WARNING_MSG = (
    "[TOKEN BUDGET WARNING] You have used {used:,} of your {budget:,} {reason} token budget ({percent:.0f}%). Wrap up your current work and produce a final answer. Avoid starting new tool calls unless absolutely necessary."
)
OUTPUT_LIMIT_BUDGET_HARD_STOP_STATE_KEY = "output_limit_budget_hard_stop"
TOKEN_BUDGET_STATUS_KEY = "token_budget_status"
TOKEN_BUDGET_USAGE_STATE_KEY = "token_budget_usage"


class TokenBudgetStatus(TypedDict):
    version: Literal[1]
    status: Literal["exceeded"]
    reason: Literal["total", "input", "output"]


class TokenBudgetUsageState(TypedDict):
    version: Literal[1]
    run_id: str
    input_tokens: int
    output_tokens: int
    total_tokens: int


def read_token_budget_status(value: object) -> TokenBudgetStatus | None:
    if not isinstance(value, dict) or set(value) != {"version", "status", "reason"}:
        return None
    reason = value.get("reason")
    if value.get("version") != 1 or value.get("status") != "exceeded" or reason not in {"total", "input", "output"}:
        return None
    return {
        "version": 1,
        "status": "exceeded",
        "reason": reason,
    }


class TokenBudgetState(AgentState):
    output_limit_budget_hard_stop: NotRequired[Annotated[dict[str, str] | None, PrivateStateAttr]]
    token_budget_usage: NotRequired[Annotated[TokenBudgetUsageState | None, PrivateStateAttr]]


@dataclass
class TokenUsage:
    input: int = 0
    output: int = 0
    total: int = 0


class TokenBudgetMiddleware(AgentMiddleware[TokenBudgetState]):
    """Enforce per-run token budget limits."""

    state_schema = TokenBudgetState

    def __init__(self, config: TokenBudgetConfig) -> None:
        super().__init__()
        self._config = config
        self._lock = threading.Lock()

        # Keyed strictly by run_id (clobber-safe) and bounded (leak-safe)
        self._warned: BoundedDict[str, bool] = BoundedDict(1000)
        self._pending_warnings: BoundedDict[str, list[str]] = BoundedDict(1000)
        self._seen_messages: BoundedDict[str, dict[str, tuple[int, int]]] = BoundedDict(1000)
        self._seen_subagent_receipts: BoundedDict[str, dict[str, tuple[int, int]]] = BoundedDict(1000)
        self._seen_subagent_conflicts: BoundedDict[str, set[str]] = BoundedDict(1000)
        self._cumulative_usage: BoundedDict[str, TokenUsage] = BoundedDict(1000)
        # Stop reason set when the hard-stop fires. NOT cleared by
        # ``_clear_run_state``/``after_agent`` so the executor can consume it
        # after the run returns; bounded so abandoned runs cannot leak.
        self._stop_reason: BoundedDict[str, str] = BoundedDict(1000)

    @classmethod
    def from_config(cls, config: TokenBudgetConfig) -> TokenBudgetMiddleware:
        return cls(config=config)

    def reset(self) -> None:
        with self._lock:
            self._warned.clear()
            self._pending_warnings.clear()
            self._seen_messages.clear()
            self._seen_subagent_receipts.clear()
            self._seen_subagent_conflicts.clear()
            self._cumulative_usage.clear()
            self._stop_reason.clear()

    def consume_stop_reason(self, run_id: str | None) -> str | None:
        """Pop and return the stop reason the hard-stop set for this run.

        Returns ``"token_capped"`` when the budget hard-stop fired during the
        run, otherwise ``None``. The executor calls this after the run returns
        to decide whether a completed subagent was actually budget-capped
        (and should carry ``stop_reason=token_capped`` to the lead). Popping
        keeps the dict from accumulating across runs on a reused instance.
        """
        with self._lock:
            return self._stop_reason.pop(run_id, None)

    def is_hard_stopped(self, run_id: str | None) -> bool:
        """Read the current hard-stop fact without consuming executor state."""

        with self._lock:
            return self._stop_reason.get(run_id) == "token_capped"

    @staticmethod
    def _get_run_id(runtime: Runtime) -> str:
        ctx = getattr(runtime, "context", None)
        if isinstance(ctx, dict) and "run_id" in ctx:
            return ctx["run_id"]
        # Fallback to runtime object ID to prevent collisions across embedded client runs
        return str(id(runtime))

    def _clear_run_state(self, run_id: str) -> None:
        with self._lock:
            self._warned.pop(run_id, None)
            self._pending_warnings.pop(run_id, None)
            self._seen_messages.pop(run_id, None)
            self._seen_subagent_receipts.pop(run_id, None)
            self._seen_subagent_conflicts.pop(run_id, None)
            self._cumulative_usage.pop(run_id, None)

    @staticmethod
    def _checkpoint_usage(
        state: AgentState,
        *,
        run_id: str,
    ) -> tuple[TokenUsage, bool]:
        raw = state.get(TOKEN_BUDGET_USAGE_STATE_KEY)
        if raw is None:
            return TokenUsage(), False
        if not isinstance(raw, Mapping) or set(raw) != {
            "version",
            "run_id",
            "input_tokens",
            "output_tokens",
            "total_tokens",
        }:
            raise RuntimeError("token budget checkpoint state is invalid")
        if raw.get("version") != 1 or not isinstance(raw.get("run_id"), str) or not raw["run_id"]:
            raise RuntimeError("token budget checkpoint state is invalid")
        values = (
            raw.get("input_tokens"),
            raw.get("output_tokens"),
            raw.get("total_tokens"),
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise RuntimeError("token budget checkpoint state is invalid")
        input_tokens, output_tokens, total_tokens = values
        if total_tokens != input_tokens + output_tokens:
            raise RuntimeError("token budget checkpoint state is invalid")
        if raw["run_id"] != run_id:
            return TokenUsage(), True
        return (
            TokenUsage(
                input=input_tokens,
                output=output_tokens,
                total=total_tokens,
            ),
            False,
        )

    @staticmethod
    def _checkpoint_update(
        run_id: str,
        usage: TokenUsage,
    ) -> dict[str, TokenBudgetUsageState]:
        return {
            TOKEN_BUDGET_USAGE_STATE_KEY: {
                "version": 1,
                "run_id": run_id,
                "input_tokens": usage.input,
                "output_tokens": usage.output,
                "total_tokens": usage.total,
            }
        }

    @staticmethod
    def _usage_snapshot(run_id: str, usage: TokenUsage) -> TokenBudgetUsageSnapshot:
        return TokenBudgetUsageSnapshot(
            run_id=run_id,
            input_tokens=usage.input,
            output_tokens=usage.output,
            total_tokens=usage.total,
        )

    @staticmethod
    def _token_usage(snapshot: TokenBudgetUsageSnapshot) -> TokenUsage:
        return TokenUsage(
            input=snapshot.input_tokens,
            output=snapshot.output_tokens,
            total=snapshot.total_tokens,
        )

    @staticmethod
    def _private_recorder(
        runtime: Runtime,
        *,
        run_id: str,
    ) -> TokenBudgetUsageRecorder | None:
        context = getattr(runtime, "context", None)
        if not isinstance(context, Mapping):
            return None
        raw = context.get(TOKEN_BUDGET_USAGE_RECORDER_CONTEXT_KEY)
        if raw is None:
            return None
        if type(raw) is not TokenBudgetUsageRecorder:
            raise RuntimeError("token budget private recorder is invalid")
        if raw.snapshot().run_id != run_id:
            raise RuntimeError("token budget private recorder belongs to another Run")
        return raw

    @classmethod
    def _record_private_usage(
        cls,
        runtime: Runtime,
        *,
        run_id: str,
        usage: TokenUsage,
    ) -> None:
        recorder = cls._private_recorder(runtime, run_id=run_id)
        if recorder is None:
            return
        try:
            recorder.merge(cls._usage_snapshot(run_id, usage))
        except TokenBudgetUsageConflict:
            raise RuntimeError(
                "token budget private usage is dimensionally inconsistent",
            ) from None

    @staticmethod
    def _model_usage(message: AIMessage) -> dict[str, int]:
        receipt_state = read_subagent_usage_receipt_state(
            message.additional_kwargs,
        )
        if receipt_state is not None:
            baseline, _contributions, _conflicts = receipt_state
            return baseline
        return message.usage_metadata or {}

    @staticmethod
    def _subagent_receipts(
        messages: list[Any],
    ) -> tuple[dict[str, dict[str, int]], frozenset[str]]:
        receipts: dict[str, dict[str, int]] = {}
        conflicts: set[str] = set()

        for message in messages:
            candidates: list[tuple[str, dict[str, int]]] = []
            if isinstance(message, ToolMessage):
                receipt = read_subagent_usage_receipt(
                    message.additional_kwargs,
                )
                if receipt is not None:
                    candidates.append(receipt)
            elif isinstance(message, AIMessage):
                receipt_state = read_subagent_usage_receipt_state(
                    message.additional_kwargs,
                )
                if receipt_state is not None:
                    _baseline, contributions, persisted_conflicts = receipt_state
                    conflicts.update(persisted_conflicts)
                    for receipt_id in persisted_conflicts:
                        receipts.pop(receipt_id, None)
                    candidates.extend(contributions.items())
                elif SUBAGENT_USAGE_RECEIPT_STATE_KEY in message.additional_kwargs:
                    logger.warning(
                        "Ignoring malformed persisted Sub-Agent Task budget receipt state: message_id=%s",
                        message.id,
                    )

            for receipt_id, usage in candidates:
                existing = receipts.get(receipt_id)
                if existing is not None and existing != usage:
                    receipts.pop(receipt_id, None)
                    conflicts.add(receipt_id)
                elif receipt_id not in conflicts:
                    receipts[receipt_id] = usage

        for receipt_id in sorted(conflicts):
            logger.warning(
                "Conflicting Sub-Agent Task budget receipt requires a hard stop: receipt_id=%s",
                receipt_id,
            )
        return receipts, frozenset(conflicts)

    def _capture_usage_locked(
        self,
        messages: list[Any],
        *,
        run_id: str,
    ) -> tuple[TokenUsage, bool]:
        seen = self._seen_messages.setdefault(run_id, {})
        seen_receipts = self._seen_subagent_receipts.setdefault(run_id, {})
        seen_conflicts = self._seen_subagent_conflicts.setdefault(run_id, set())
        usage_accum = self._cumulative_usage.setdefault(run_id, TokenUsage())

        for msg in messages:
            if isinstance(msg, AIMessage) and msg.id and hasattr(msg, "usage_metadata"):
                usage = self._model_usage(msg)
                input_tokens = usage.get("input_tokens", 0)
                output_tokens = usage.get("output_tokens", 0)
                prev_input, prev_output = seen.get(msg.id, (0, 0))
                diff_input = max(0, input_tokens - prev_input)
                diff_output = max(0, output_tokens - prev_output)
                if diff_input > 0 or diff_output > 0:
                    usage_accum.input += diff_input
                    usage_accum.output += diff_output
                    usage_accum.total += diff_input + diff_output
                    seen[msg.id] = (input_tokens, output_tokens)

        receipts, observed_conflicts = self._subagent_receipts(messages)
        new_conflicts = set(observed_conflicts) - seen_conflicts
        for receipt_id, usage in receipts.items():
            contribution = (
                usage["input_tokens"],
                usage["output_tokens"],
            )
            previous = seen_receipts.get(receipt_id)
            if previous is not None:
                if previous != contribution:
                    logger.warning(
                        "Ignoring conflicting replayed Sub-Agent Task budget receipt: receipt_id=%s",
                        receipt_id,
                    )
                    if receipt_id not in seen_conflicts:
                        new_conflicts.add(receipt_id)
                continue
            seen_receipts[receipt_id] = contribution
            usage_accum.input += contribution[0]
            usage_accum.output += contribution[1]
            usage_accum.total += contribution[0] + contribution[1]

        seen_conflicts.update(observed_conflicts)
        seen_conflicts.update(new_conflicts)
        return usage_accum, bool(new_conflicts)

    @override
    def before_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        if not self._config.enabled:
            return None

        run_id = self._get_run_id(runtime)
        checkpoint_usage, reset_checkpoint = self._checkpoint_usage(
            state,
            run_id=run_id,
        )
        checkpoint_present = state.get(TOKEN_BUDGET_USAGE_STATE_KEY) is not None
        recorder = self._private_recorder(runtime, run_id=run_id)
        baseline = recorder.snapshot() if recorder is not None else TokenBudgetUsageSnapshot.zero(run_id)
        checkpoint = self._usage_snapshot(run_id, checkpoint_usage)
        if reset_checkpoint:
            recovered = baseline
        else:
            try:
                recovered = dominant_token_budget_usage(baseline, checkpoint)
            except TokenBudgetUsageConflict:
                raise RuntimeError(
                    "token budget checkpoint and private baseline are dimensionally inconsistent",
                ) from None
        if recorder is not None:
            try:
                recorder.merge(recovered)
            except TokenBudgetUsageConflict:
                raise RuntimeError(
                    "token budget checkpoint and private baseline are dimensionally inconsistent",
                ) from None
        checkpoint_usage = self._token_usage(recovered)
        refresh_checkpoint = reset_checkpoint or ((not checkpoint_present and recovered.total_tokens > 0) or recovered != checkpoint)
        with self._lock:
            self._cumulative_usage.setdefault(run_id, checkpoint_usage)

        # Checkpoint history is already represented by the private cumulative
        # baseline. Mark it as seen so replay/reclaim cannot count it twice.
        messages = state.get("messages", [])
        if not messages:
            return self._checkpoint_update(run_id, checkpoint_usage) if refresh_checkpoint else None

        with self._lock:
            seen = self._seen_messages.setdefault(run_id, {})
            seen_receipts = self._seen_subagent_receipts.setdefault(run_id, {})
            seen_conflicts = self._seen_subagent_conflicts.setdefault(run_id, set())

            for msg in messages:
                if isinstance(msg, AIMessage) and msg.id and hasattr(msg, "usage_metadata"):
                    usage = self._model_usage(msg)
                    input_tokens = usage.get("input_tokens", 0)
                    output_tokens = usage.get("output_tokens", 0)
                    seen[msg.id] = (input_tokens, output_tokens)
            receipts, conflicts = self._subagent_receipts(messages)
            for receipt_id, usage in receipts.items():
                seen_receipts[receipt_id] = (
                    usage["input_tokens"],
                    usage["output_tokens"],
                )
            seen_conflicts.update(conflicts)
        return self._checkpoint_update(run_id, checkpoint_usage) if refresh_checkpoint else None

    @override
    async def abefore_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self.before_agent(state, runtime)

    @override
    def after_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        if not self._config.enabled:
            return None
        run_id = self._get_run_id(runtime)
        messages = state.get("messages", [])
        update = None
        if messages:
            with self._lock:
                usage, receipt_conflict = self._capture_usage_locked(
                    messages,
                    run_id=run_id,
                )
                self._record_private_usage(
                    runtime,
                    run_id=run_id,
                    usage=usage,
                )
                update = self._checkpoint_update(run_id, usage)
                if receipt_conflict:
                    logger.error(
                        "Token budget recorded a terminal conflict for run %s: conflicting Sub-Agent Task usage receipt",
                        run_id,
                    )
                    self._stop_reason[run_id] = "token_capped"
        self._clear_run_state(run_id)
        return update

    @override
    async def aafter_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self.after_agent(state, runtime)

    def _build_hard_stop_update(self, msg: AIMessage, reason: str) -> dict[str, Any]:
        """Build the state update dictionary for a hard stop."""
        kwargs = dict(msg.additional_kwargs) if msg.additional_kwargs else {}
        if "tool_calls" in kwargs:
            del kwargs["tool_calls"]
        if "function_call" in kwargs:
            del kwargs["function_call"]
        response_metadata = dict(getattr(msg, "response_metadata", {}) or {})
        response_metadata[TOKEN_BUDGET_STATUS_KEY] = {
            "version": 1,
            "status": "exceeded",
            "reason": reason,
        }

        if response_metadata.get("finish_reason") == "tool_calls":
            response_metadata["finish_reason"] = "stop"

        stopped_msg = msg.model_copy(update={"tool_calls": [], "additional_kwargs": kwargs, "response_metadata": response_metadata})
        return {"messages": [stopped_msg]}

    def _apply(self, state: AgentState, runtime: Runtime) -> dict | None:
        if not self._config.enabled:
            return None

        messages = state.get("messages", [])
        if not messages:
            return None

        last_msg = messages[-1]
        if not isinstance(last_msg, AIMessage):
            return None

        run_id = self._get_run_id(runtime)

        with self._lock:
            usage_accum, receipt_conflict = self._capture_usage_locked(
                messages,
                run_id=run_id,
            )
            self._record_private_usage(
                runtime,
                run_id=run_id,
                usage=usage_accum,
            )

            if receipt_conflict:
                logger.error(
                    "Token budget hard stop triggered for run %s: conflicting Sub-Agent Task usage receipt",
                    run_id,
                )
                self._stop_reason[run_id] = "token_capped"
                return {
                    **self._checkpoint_update(run_id, usage_accum),
                    **self._build_hard_stop_update(last_msg, "total"),
                    OUTPUT_LIMIT_BUDGET_HARD_STOP_STATE_KEY: {"run_id": run_id},
                }

            if usage_accum.total <= 0:
                return self._checkpoint_update(run_id, usage_accum)

            fractions = [("total", usage_accum.total, self._config.max_tokens)]
            if self._config.max_input_tokens:
                fractions.append(("input", usage_accum.input, self._config.max_input_tokens))
            if self._config.max_output_tokens:
                fractions.append(("output", usage_accum.output, self._config.max_output_tokens))

            highest_fraction = 0.0
            trigger_reason = ""
            trigger_used = 0
            trigger_budget = 0

            for reason, used, limit in fractions:
                frac = used / limit
                if frac > highest_fraction:
                    highest_fraction = frac
                    trigger_reason = reason
                    trigger_used = used
                    trigger_budget = limit

            if highest_fraction >= self._config.hard_stop_threshold:
                logger.warning("Token budget hard stop triggered for run %s: %s limit exceeded", run_id, trigger_reason)
                # Record the stop reason so the executor can surface
                # ``stop_reason=token_capped`` to the lead after the run
                # returns (the hard stop itself does not raise). See
                # ``consume_stop_reason``.
                self._stop_reason[run_id] = "token_capped"
                return {
                    **self._checkpoint_update(run_id, usage_accum),
                    **self._build_hard_stop_update(last_msg, trigger_reason),
                    OUTPUT_LIMIT_BUDGET_HARD_STOP_STATE_KEY: {"run_id": run_id},
                }

            if highest_fraction >= self._config.warn_threshold and not self._warned.get(run_id, False):
                self._warned[run_id] = True
                percent = highest_fraction * 100
                warn_text = _BUDGET_WARNING_MSG.format(reason=trigger_reason, used=trigger_used, budget=trigger_budget, percent=percent)
                logger.info("Token budget warning triggered for run %s: %s limit at %.1f%%", run_id, trigger_reason, percent)
                # queue warning for wrap_model_call
                warnings = self._pending_warnings.setdefault(run_id, [])
                warnings.append(warn_text)
                return self._checkpoint_update(run_id, usage_accum)

            return self._checkpoint_update(run_id, usage_accum)

    @override
    def after_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._apply(state, runtime)

    @override
    async def aafter_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._apply(state, runtime)

    def _drain_pending_warnings(self, runtime: Runtime) -> list[str]:
        if not self._config.enabled:
            return []

        run_id = self._get_run_id(runtime)
        with self._lock:
            warnings = self._pending_warnings.pop(run_id, None)
        return warnings or []

    def _inject_warnings(self, request: ModelRequest, warnings: list[str]) -> ModelRequest:
        if not warnings:
            return request

        merged_text = "\n\n".join(warnings)
        warning_msg = HumanMessage(content=merged_text, name="budget_warning")

        messages = getattr(request, "messages", [])
        new_messages = list(messages) + [warning_msg]
        return request.override(messages=new_messages)

    @override
    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]) -> ModelCallResult:

        warnings = self._drain_pending_warnings(request.runtime)
        request = self._inject_warnings(request, warnings)

        return handler(request)

    @override
    async def awrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]) -> ModelCallResult:
        warnings = self._drain_pending_warnings(request.runtime)
        request = self._inject_warnings(request, warnings)
        return await handler(request)
