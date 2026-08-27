"""Manual Thread-context compaction helpers.

Context Usage is owned by Context Projection v2.  This module contains only
the mutation workflow shared by manual compaction, Memory Seal, and Dream.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any

from langgraph.types import Overwrite

from deerflow.agents.memory.snip import (
    MEMORY_ARCHIVE_CONTEXT_KEY,
    SnipArchiveContext,
)
from deerflow.agents.middlewares.summarization_middleware import (
    DeerFlowSummarizationMiddleware,
    SnipCompactionFailed,
    SnipPromptBudgetTooSmall,
    SnipSourceTooLarge,
    create_summarization_middleware,
)
from deerflow.config.app_config import AppConfig, get_app_config
from deerflow.runtime.checkpoint_state import CheckpointStateAccessor


class ContextCompactionDisabled(RuntimeError):
    """Raised when manual compaction is requested while summarization is disabled."""


class ContextCompactionFailed(RuntimeError):
    """Raised when a compressible Thread cannot be summarized."""


@dataclass(frozen=True)
class ThreadCompactionResult:
    """Result returned after a manual Context compaction attempt."""

    thread_id: str
    compacted: bool
    reason: str | None = None
    removed_message_count: int = 0
    preserved_message_count: int = 0
    summary_updated: bool = False
    checkpoint_id: str | None = None
    total_tokens: int = 0


@dataclass(frozen=True)
class PreparedThreadCompaction:
    """A compaction result prepared from one immutable source checkpoint."""

    thread_id: str
    source_checkpoint_id: str
    result: ThreadCompactionResult
    write_config: dict[str, Any] | None = None
    update_values: dict[str, Any] | None = None


def _create_compaction_middleware(
    *,
    app_config: AppConfig,
    keep: tuple[str, int | float] | None,
) -> DeerFlowSummarizationMiddleware:
    middleware = create_summarization_middleware(app_config=app_config, keep=keep)
    if middleware is None:
        raise ContextCompactionDisabled("Context compaction is disabled.")
    return middleware


def _checkpoint_id(snapshot: Any) -> str:
    config = getattr(snapshot, "config", {}) or {}
    configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
    value = configurable.get("checkpoint_id") if isinstance(configurable, dict) else None
    if not isinstance(value, str) or not value:
        raise ContextCompactionFailed("Compaction source checkpoint has no identity.")
    return value


def has_complete_turns(messages: object) -> bool:
    """Return whether a materialized Thread state has an archivable turn."""

    return isinstance(messages, list) and bool(DeerFlowSummarizationMiddleware._complete_turn_ranges(messages))


async def prepare_thread_compaction(
    accessor: CheckpointStateAccessor,
    thread_id: str,
    *,
    keep: tuple[str, int | float] | None = None,
    force: bool = True,
    user_id: str | None = None,
    agent_name: str | None = None,
    app_config: AppConfig | None = None,
    snapshot: Any | None = None,
    authorization_boundary: object | None = None,
    memory_archive_context: SnipArchiveContext | None = None,
) -> PreparedThreadCompaction:
    """Summarize one checkpoint without persisting the prepared replacement."""

    resolved_app_config = app_config or get_app_config()
    middleware = _create_compaction_middleware(
        app_config=resolved_app_config,
        keep=keep,
    )

    read_config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    if snapshot is None:
        snapshot = await accessor.aget(read_config)
    source_checkpoint_id = _checkpoint_id(snapshot)
    if memory_archive_context is not None:
        if type(memory_archive_context) is not SnipArchiveContext:
            raise ContextCompactionFailed("Memory archive context is invalid.")
        if memory_archive_context.source_checkpoint_id is not None and memory_archive_context.source_checkpoint_id != source_checkpoint_id:
            raise ContextCompactionFailed("Memory archive source checkpoint changed.")
        memory_archive_context = replace(
            memory_archive_context,
            source_checkpoint_id=source_checkpoint_id,
        )

    channel_values = snapshot.values or {}
    messages = channel_values.get("messages")
    if not isinstance(messages, list) or not messages:
        return PreparedThreadCompaction(
            thread_id=thread_id,
            source_checkpoint_id=source_checkpoint_id,
            result=ThreadCompactionResult(
                thread_id=thread_id,
                compacted=False,
                reason="not_enough_messages",
            ),
        )

    state = {
        "messages": list(messages),
        "summary_text": channel_values.get("summary_text"),
    }
    runtime_context = {"thread_id": thread_id, "user_id": user_id}
    if memory_archive_context is not None:
        runtime_context[MEMORY_ARCHIVE_CONTEXT_KEY] = memory_archive_context
    if agent_name:
        runtime_context["agent_name"] = agent_name
    if authorization_boundary is not None:
        runtime_context["__authorization_boundary"] = authorization_boundary
    runtime = SimpleNamespace(context=runtime_context)
    try:
        result = await middleware.acompact_state(state, runtime, force=force)  # type: ignore[arg-type]
    except SnipCompactionFailed:
        return PreparedThreadCompaction(
            thread_id=thread_id,
            source_checkpoint_id=source_checkpoint_id,
            result=ThreadCompactionResult(
                thread_id=thread_id,
                compacted=False,
                reason="compaction_failed",
            ),
        )
    except SnipPromptBudgetTooSmall:
        return PreparedThreadCompaction(
            thread_id=thread_id,
            source_checkpoint_id=source_checkpoint_id,
            result=ThreadCompactionResult(
                thread_id=thread_id,
                compacted=False,
                reason="prompt_budget_too_small",
            ),
        )
    except SnipSourceTooLarge:
        return PreparedThreadCompaction(
            thread_id=thread_id,
            source_checkpoint_id=source_checkpoint_id,
            result=ThreadCompactionResult(
                thread_id=thread_id,
                compacted=False,
                reason="source_too_large",
            ),
        )
    if result is None:
        reason = "not_enough_messages"
        if keep is not None and keep[0] == "messages" and keep[1] == 0 and has_complete_turns(messages):
            # Dream's keep=messages:0 is an explicit archive barrier. An
            # eligible complete turn must not be mistaken for exhaustion when
            # the summary model or budget failed.
            reason = "compaction_failed"
        return PreparedThreadCompaction(
            thread_id=thread_id,
            source_checkpoint_id=source_checkpoint_id,
            result=ThreadCompactionResult(
                thread_id=thread_id,
                compacted=False,
                reason=reason,
            ),
        )

    return PreparedThreadCompaction(
        thread_id=thread_id,
        source_checkpoint_id=source_checkpoint_id,
        result=ThreadCompactionResult(
            thread_id=thread_id,
            compacted=True,
            removed_message_count=len(result.messages_to_summarize),
            preserved_message_count=len(result.preserved_messages),
            summary_updated=True,
            total_tokens=result.total_tokens,
        ),
        write_config=dict(snapshot.config or read_config),
        update_values={
            "messages": Overwrite(list(result.preserved_messages)),
            "summary_text": result.summary_text,
            "memory_archive_receipt": result.memory_archive_receipt,
        },
    )


async def commit_thread_compaction(
    accessor: CheckpointStateAccessor,
    prepared: PreparedThreadCompaction,
) -> ThreadCompactionResult:
    """Persist a prepared replacement after the caller validates its source."""

    if not prepared.result.compacted:
        return prepared.result
    if prepared.write_config is None or prepared.update_values is None:
        raise ContextCompactionFailed("Prepared compaction is incomplete.")
    new_config = await accessor.aupdate(
        prepared.write_config,
        prepared.update_values,
        as_node="manual_compaction",
    )
    new_checkpoint_id = None
    if isinstance(new_config, dict):
        new_checkpoint_id = new_config.get("configurable", {}).get("checkpoint_id")
    return replace(prepared.result, checkpoint_id=new_checkpoint_id)


__all__ = [
    "ContextCompactionDisabled",
    "ContextCompactionFailed",
    "PreparedThreadCompaction",
    "ThreadCompactionResult",
    "commit_thread_compaction",
    "has_complete_turns",
    "prepare_thread_compaction",
]
