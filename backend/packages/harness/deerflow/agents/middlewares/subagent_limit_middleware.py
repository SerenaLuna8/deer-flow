"""Middleware enforcing concurrent and per-Run subagent tool-call limits."""

import logging
from typing import Any, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime

from deerflow.agents.middlewares.tool_call_metadata import clone_ai_message_with_tool_calls
from deerflow.agents.thread_state import delegation_identity
from deerflow.config.subagents_config import (
    DEFAULT_MAX_TOTAL_SUBAGENTS_PER_RUN,
    MAX_TOTAL_SUBAGENTS_PER_RUN,
    MIN_TOTAL_SUBAGENTS_PER_RUN,
    clamp_total_subagents_per_run,
)
from deerflow.private_scope import PrivateResourceScope
from deerflow.subagents.executor import MAX_CONCURRENT_SUBAGENTS

logger = logging.getLogger(__name__)

# Valid range for max_concurrent_subagents
MIN_SUBAGENT_LIMIT = 2
MAX_SUBAGENT_LIMIT = 4
DEFAULT_MAX_TOTAL_SUBAGENTS = DEFAULT_MAX_TOTAL_SUBAGENTS_PER_RUN
MIN_SUBAGENT_TOTAL_LIMIT = MIN_TOTAL_SUBAGENTS_PER_RUN
MAX_SUBAGENT_TOTAL_LIMIT = MAX_TOTAL_SUBAGENTS_PER_RUN
SUBAGENT_LIMIT_EVENT_REASON = "subagent_total_limit"

_TOTAL_LIMIT_STOP_MSG = (
    "[SUBAGENT LIMIT REACHED] The subagent delegation limit for this run has "
    "been reached. Continue using the subagent results already collected, "
    "execute remaining simple work directly, or summarize the remaining work "
    "instead of launching more subagents."
)


def _clamp_subagent_limit(value: int) -> int:
    """Clamp subagent limit to valid range [2, 4]."""
    return max(MIN_SUBAGENT_LIMIT, min(MAX_SUBAGENT_LIMIT, value))


def _clamp_total_subagent_limit(value: int) -> int:
    """Clamp a per-Run delegation total to a bounded positive range."""
    return clamp_total_subagents_per_run(value)


def _append_text(content: Any, text: str) -> Any:
    if content is None:
        return text
    if isinstance(content, str):
        return f"{content}\n\n{text}" if content else text
    if isinstance(content, list):
        return [*content, {"type": "text", "text": f"\n\n{text}"}]
    return f"{content}\n\n{text}"


def _sync_raw_tool_call_occurrences(
    message: AIMessage,
    *,
    indices_to_drop: set[int],
) -> AIMessage:
    """Keep raw provider metadata aligned when call IDs are reused."""
    additional_kwargs = dict(message.additional_kwargs or {})
    raw_tool_calls = additional_kwargs.get("tool_calls")
    decoded_tool_calls = message.tool_calls or []
    if not isinstance(raw_tool_calls, list):
        return message

    if len(raw_tool_calls) == len(decoded_tool_calls):
        additional_kwargs["tool_calls"] = [raw_tool_call for index, raw_tool_call in enumerate(raw_tool_calls) if index not in indices_to_drop]
        return message.model_copy(update={"additional_kwargs": additional_kwargs})

    kept_ids = {tool_call.get("id") for index, tool_call in enumerate(decoded_tool_calls) if index not in indices_to_drop}
    if any(decoded_tool_calls[index].get("id") in kept_ids for index in indices_to_drop):
        # ID-only filtering cannot identify the retained occurrence.
        additional_kwargs.pop("tool_calls", None)
        return message.model_copy(update={"additional_kwargs": additional_kwargs})
    return message


DelegationScope = tuple[str | None, str | None, str]


def _entry_scope(entry: object) -> DelegationScope | None:
    if not isinstance(entry, dict):
        return None
    run_id = entry.get("run_id")
    if not run_id:
        return None
    project_id = entry.get("project_id")
    owner_user_id = entry.get("owner_user_id")
    if (project_id is None) != (owner_user_id is None):
        return None
    return (
        str(project_id) if project_id is not None else None,
        str(owner_user_id) if owner_user_id is not None else None,
        str(run_id),
    )


def _runtime_scope(
    runtime: Runtime | None,
    delegations: object,
) -> DelegationScope | None:
    context = getattr(runtime, "context", None)
    if not isinstance(context, dict):
        return None
    run_id = context.get("run_id")
    if not run_id:
        return None

    private_scope = context.get("private_scope")
    if isinstance(private_scope, PrivateResourceScope):
        return (
            str(private_scope.project_id),
            str(private_scope.owner_user_id),
            str(run_id),
        )
    if private_scope is not None:
        return None

    # A private checkpoint must never be interpreted as non-private merely
    # because an expected server-issued scope disappeared from runtime context.
    if isinstance(delegations, list) and any(isinstance(entry, dict) and (entry.get("project_id") is not None or entry.get("owner_user_id") is not None) for entry in delegations):
        return None
    return (None, None, str(run_id))


def _delegation_id(entry: object) -> str | None:
    if not isinstance(entry, dict):
        return None
    entry_id = entry.get("id")
    return str(entry_id) if entry_id else None


def _count_prior_delegations(
    delegations: object,
    *,
    scope: DelegationScope | None,
) -> int:
    if not isinstance(delegations, list):
        return 0
    identities: set[tuple[str | None, str | None, str | None, str, int]] = set()
    for entry in delegations:
        entry_id = _delegation_id(entry)
        if entry_id is None:
            continue
        entry_scope = _entry_scope(entry)
        if scope is not None and entry_scope != scope:
            continue
        identities.add(delegation_identity(entry))
    return len(identities)


class SubagentLimitMiddleware(AgentMiddleware[AgentState]):
    """Truncate excess ``task`` calls from one response or Run.

    When an LLM generates more than max_concurrent parallel task tool calls
    in one response, this middleware keeps only the first max_concurrent and
    discards the rest. The durable delegation ledger additionally enforces a
    total across every model turn in the exact server-issued private
    ``project_id + owner_user_id + run_id`` scope.

    Args:
        max_concurrent: Maximum number of concurrent subagent calls allowed.
            Defaults to MAX_CONCURRENT_SUBAGENTS (3). Clamped to [2, 4].
        max_total: Maximum task delegations admitted across one Run. Defaults
            to 6 and is clamped to [1, 50].
    """

    def __init__(
        self,
        max_concurrent: int = MAX_CONCURRENT_SUBAGENTS,
        max_total: int = DEFAULT_MAX_TOTAL_SUBAGENTS,
    ):
        super().__init__()
        self.max_concurrent = _clamp_subagent_limit(max_concurrent)
        self.max_total = _clamp_total_subagent_limit(max_total)

    def _record_total_limit_event(
        self,
        runtime: Runtime | None,
        *,
        prior_delegations: int,
        admitted_calls: int,
        dropped_calls: int,
    ) -> None:
        """Persist a bounded, argument-free RunJournal event when available."""

        context = getattr(runtime, "context", None)
        if not isinstance(context, dict):
            return
        journal = context.get("__run_journal")
        record = getattr(journal, "record_middleware", None)
        if not callable(record):
            return
        try:
            record(
                tag="subagent_limit",
                name=type(self).__name__,
                hook="after_model",
                action="truncate_tool_calls",
                changes={
                    "reason": SUBAGENT_LIMIT_EVENT_REASON,
                    "max_total": self.max_total,
                    "prior_delegations": prior_delegations,
                    "admitted_task_calls": admitted_calls,
                    "dropped_task_calls": dropped_calls,
                },
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "Failed to record middleware:subagent_limit event",
                exc_info=True,
            )

    def _truncate_task_calls(
        self,
        state: AgentState,
        runtime: Runtime | None = None,
    ) -> dict | None:
        messages = state.get("messages", [])
        if not messages:
            return None

        last_msg = messages[-1]
        if getattr(last_msg, "type", None) != "ai":
            return None

        tool_calls = getattr(last_msg, "tool_calls", None)
        if not tool_calls:
            return None

        task_indices = [i for i, tc in enumerate(tool_calls) if tc.get("name") == "task"]
        if not task_indices:
            return None

        delegations = state.get("delegations")
        scope = _runtime_scope(runtime, delegations)
        if scope is None and delegations:
            logger.warning("Subagent limit middleware could not resolve the exact private delegation scope; counting all thread delegations as prior usage (fail-restrictive).")
        prior_delegation_count = _count_prior_delegations(
            delegations,
            scope=scope,
        )
        remaining_total = max(0, self.max_total - prior_delegation_count)
        allowed_task_calls = min(
            len(task_indices),
            self.max_concurrent,
            remaining_total,
        )
        if len(task_indices) <= allowed_task_calls:
            return None

        indices_to_drop = set(task_indices[allowed_task_calls:])
        truncated_tool_calls = [tc for i, tc in enumerate(tool_calls) if i not in indices_to_drop]

        dropped_count = len(indices_to_drop)
        logger.warning(
            "Truncated %s excess task tool call(s) from model response (concurrent limit: %s; total limit: %s; prior delegations: %s)",
            dropped_count,
            self.max_concurrent,
            self.max_total,
            prior_delegation_count,
        )

        # Attribute only the calls that the total cap drops *in addition to*
        # the concurrent cap. A five-call proposal with concurrent=2 and
        # remaining_total=3 is solely a concurrent truncation.
        total_limit_dropped = max(
            0,
            min(len(task_indices), self.max_concurrent) - allowed_task_calls,
        )
        if total_limit_dropped:
            self._record_total_limit_event(
                runtime,
                prior_delegations=prior_delegation_count,
                admitted_calls=allowed_task_calls,
                dropped_calls=total_limit_dropped,
            )

        content = _append_text(last_msg.content, _TOTAL_LIMIT_STOP_MSG) if total_limit_dropped else None
        updated_msg = clone_ai_message_with_tool_calls(
            _sync_raw_tool_call_occurrences(
                last_msg,
                indices_to_drop=indices_to_drop,
            ),
            truncated_tool_calls,
            content=content,
        )
        return {"messages": [updated_msg]}

    @override
    def after_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._truncate_task_calls(state, runtime)

    @override
    async def aafter_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._truncate_task_calls(state, runtime)
