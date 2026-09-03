"""Compaction receipts, Context Evidence preconditions, and compaction state updates."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import cast

from langchain.agents import AgentState
from langchain_core.messages import AnyMessage, message_to_dict
from langgraph.config import get_config
from langgraph.runtime import Runtime
from pydantic import ValidationError

from deerflow.agents.context_compaction_warning import ContextCompactionFailureReason
from deerflow.agents.memory.snip import MEMORY_ARCHIVE_CONTEXT_KEY, MemoryArchiveReceipt, SnipArchiveContext, build_memory_archive_receipt
from deerflow.agents.middlewares.provider_request_profile import contains_visual_material
from deerflow.agents.middlewares.snip_planner import SnipCompactionFailed
from deerflow.agents.middlewares.turn_compaction import _PreparedCompaction, messages_for_trigger_count, summary_count_message
from deerflow.agents.provider_request_contract import CONTEXT_COMPACTION_RECEIPT_STATE_KEY, CONTEXT_PROJECTION_SNAPSHOT_STATE_KEY, PROVIDER_REQUEST_PROFILE_STATE_KEY
from deerflow.runtime.context_evidence import ContextCheckpointEstimator, ContextCheckpointProjectionSnapshot, ContextCompactionCheckpointReceipt, VisualTokenCostContractError


@dataclass(frozen=True)
class ContextCompactionResult:
    """Result of summarizing old context and retaining the active tail."""

    summary_text: str
    messages_to_summarize: tuple[AnyMessage, ...]
    preserved_messages: tuple[AnyMessage, ...]
    total_tokens: int
    memory_archive_receipt: MemoryArchiveReceipt | None


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


def read_archive_context(runtime: Runtime) -> SnipArchiveContext | None:
    runtime_context = runtime.context
    if not isinstance(runtime_context, dict):
        return None
    value = runtime_context.get(MEMORY_ARCHIVE_CONTEXT_KEY)
    return value if type(value) is SnipArchiveContext else None


def resolve_source_checkpoint_id(
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


def build_compaction_receipt(
    prepared: _PreparedCompaction,
    tagged_text: str,
    runtime: Runtime,
) -> MemoryArchiveReceipt | None:
    archive_context = read_archive_context(runtime)
    if archive_context is None or not archive_context.enabled:
        return None
    thread_id = _resolve_thread_id(runtime)
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("SNIP archive Thread identity is unavailable")
    return build_memory_archive_receipt(
        archive_context,
        thread_id=thread_id,
        source_checkpoint_id=resolve_source_checkpoint_id(
            runtime,
            archive_context,
        ),
        previous_summary=prepared.previous_summary,
        messages=prepared.source_messages,
        tagged_text=tagged_text,
    )


def resolve_compaction_estimator(
    state: AgentState,
) -> tuple[Mapping[str, object] | None, ContextCheckpointEstimator]:
    """Resolve the receipt estimator or raise a typed compaction failure."""

    raw_source_snapshot = state.get(CONTEXT_PROJECTION_SNAPSHOT_STATE_KEY)
    source_snapshot = raw_source_snapshot if isinstance(raw_source_snapshot, Mapping) else None
    if source_snapshot is not None:
        try:
            estimator = ContextCheckpointProjectionSnapshot.from_safe_mapping(
                source_snapshot,
            ).estimator
        except (TypeError, ValueError):
            raise SnipCompactionFailed(
                reason=(ContextCompactionFailureReason.CHECKPOINT_UNMEASURABLE),
            ) from None
        return source_snapshot, estimator
    profile = state.get(PROVIDER_REQUEST_PROFILE_STATE_KEY)
    if not isinstance(profile, Mapping):
        raise SnipCompactionFailed(
            reason=(ContextCompactionFailureReason.CHECKPOINT_UNMEASURABLE),
        )
    try:
        estimator = ContextCheckpointEstimator(
            error_allowance_ratio=float(profile["error_allowance_ratio"]),
            provider_fixed_overhead_tokens=int(profile["provider_fixed_overhead_tokens"]),
            provider_per_message_overhead_tokens=int(profile["provider_per_message_overhead_tokens"]),
            provider_per_tool_overhead_tokens=int(profile["provider_per_tool_overhead_tokens"]),
            visual_max_tokens_per_image=profile.get(
                "visual_max_tokens_per_image",
            ),
            fixed_message_count=(int(profile["bounded_overlay_message_count"]) + 1),
            tool_count=int(profile["full_tool_count"]),
        )
    except (KeyError, TypeError, ValueError):
        raise SnipCompactionFailed(
            reason=(ContextCompactionFailureReason.CHECKPOINT_UNMEASURABLE),
        ) from None
    return None, estimator


def require_receipt_preconditions(
    observer: object | None,
    state: AgentState,
    runtime: Runtime,
    *,
    asynchronous: bool,
) -> None:
    """Fail the static receipt inputs before any SNIP model call.

    Receipt construction itself still runs after the summary exists; this
    guard only rejects configurations that could never commit, so a doomed
    compaction cannot first consume its bounded SNIP model-call budget.
    """

    observer = observer
    if observer is None:
        return
    prepare_receipt = getattr(
        observer,
        "prepare_compaction_checkpoint_receipt",
        None,
    )
    if not callable(prepare_receipt):
        if callable(
            getattr(
                observer,
                "record_ephemeral_compaction_committed",
                None,
            )
        ):
            if not asynchronous:
                raise SnipCompactionFailed(
                    reason=(ContextCompactionFailureReason.OBSERVER_UNSUPPORTED),
                )
            if not isinstance(state.get("messages"), list):
                raise SnipCompactionFailed(
                    reason=(ContextCompactionFailureReason.CHECKPOINT_UNMEASURABLE),
                )
            return
        raise SnipCompactionFailed(
            reason=ContextCompactionFailureReason.OBSERVER_UNSUPPORTED,
        )
    if not isinstance(state.get("messages"), list):
        raise SnipCompactionFailed(
            reason=(ContextCompactionFailureReason.CHECKPOINT_UNMEASURABLE),
        )
    _, estimator = resolve_compaction_estimator(state)
    messages = cast("list[AnyMessage]", state["messages"])
    if estimator.visual_max_tokens_per_image is None and contains_visual_material(messages):
        raise SnipCompactionFailed(
            reason=(ContextCompactionFailureReason.CHECKPOINT_UNMEASURABLE),
        )
    if (
        resolve_source_checkpoint_id(
            runtime,
            read_archive_context(runtime),
        )
        is None
    ):
        raise SnipCompactionFailed(
            reason=(ContextCompactionFailureReason.CHECKPOINT_UNMEASURABLE),
        )


def context_compaction_update(
    observer: object | None,
    state: AgentState,
    result: ContextCompactionResult,
    runtime: Runtime,
) -> dict[str, object]:
    observer = observer
    if observer is None:
        return {}
    prepare_receipt = getattr(
        observer,
        "prepare_compaction_checkpoint_receipt",
        None,
    )
    if not callable(prepare_receipt):
        if callable(
            getattr(
                observer,
                "record_ephemeral_compaction_committed",
                None,
            )
        ):
            raise SnipCompactionFailed(
                reason=ContextCompactionFailureReason.OBSERVER_UNSUPPORTED,
            )
        raise SnipCompactionFailed(
            reason=ContextCompactionFailureReason.OBSERVER_UNSUPPORTED,
        )
    source_messages = state.get("messages")
    if not isinstance(source_messages, list):
        raise SnipCompactionFailed(
            reason=(ContextCompactionFailureReason.CHECKPOINT_UNMEASURABLE),
        )
    source_summary = state.get("summary_text") if isinstance(state.get("summary_text"), str) else None
    source_state_digest = context_state_digest(
        source_messages,
        source_summary,
    )
    source_snapshot, estimator = resolve_compaction_estimator(state)
    source_checkpoint_id = resolve_source_checkpoint_id(
        runtime,
        read_archive_context(runtime),
    )
    if source_checkpoint_id is None:
        raise SnipCompactionFailed(
            reason=(ContextCompactionFailureReason.CHECKPOINT_UNMEASURABLE),
        )
    try:
        receipt = prepare_receipt(
            source_checkpoint_id=source_checkpoint_id,
            source_snapshot=source_snapshot,
            estimator=estimator,
            source_state_digest=source_state_digest,
            source_values={
                "messages": list(source_messages),
                "summary_text": source_summary,
            },
            result_values={
                "messages": list(result.preserved_messages),
                "summary_text": result.summary_text,
            },
        )
    except VisualTokenCostContractError as exc:
        raise SnipCompactionFailed(
            reason=(ContextCompactionFailureReason.CHECKPOINT_UNMEASURABLE),
        ) from exc
    except ValidationError as exc:
        raise SnipCompactionFailed(
            reason=ContextCompactionFailureReason.RECEIPT_INVALID,
        ) from exc
    if not isinstance(receipt, ContextCompactionCheckpointReceipt):
        raise SnipCompactionFailed(
            reason=ContextCompactionFailureReason.RECEIPT_INVALID,
        )
    return {
        CONTEXT_PROJECTION_SNAPSHOT_STATE_KEY: (receipt.projection_snapshot.to_safe_mapping()),
        CONTEXT_COMPACTION_RECEIPT_STATE_KEY: receipt.to_safe_mapping(),
    }


async def acontext_compaction_update(
    observer: object | None,
    token_counter: Callable[..., int],
    state: AgentState,
    result: ContextCompactionResult,
    runtime: Runtime,
) -> dict[str, object]:
    observer = observer
    if observer is None or callable(getattr(observer, "prepare_compaction_checkpoint_receipt", None)):
        return context_compaction_update(observer, state, result, runtime)
    record_ephemeral = getattr(
        observer,
        "record_ephemeral_compaction_committed",
        None,
    )
    if not callable(record_ephemeral):
        raise SnipCompactionFailed(
            reason=ContextCompactionFailureReason.OBSERVER_UNSUPPORTED,
        )
    source_messages = state.get("messages")
    if not isinstance(source_messages, list):
        raise SnipCompactionFailed(
            reason=(ContextCompactionFailureReason.CHECKPOINT_UNMEASURABLE),
        )
    source_summary = state.get("summary_text") if isinstance(state.get("summary_text"), str) else None
    summary_tokens = token_counter([summary_count_message(result.summary_text)])
    result_tokens = token_counter(
        messages_for_trigger_count(
            list(result.preserved_messages),
            result.summary_text,
        )
    )
    if result_tokens > result.total_tokens:
        raise SnipCompactionFailed(
            "Ephemeral Context compaction did not reduce retained state",
        )

    await record_ephemeral(
        source_state_digest=context_state_digest(
            source_messages,
            source_summary,
        ),
        result_state_digest=context_state_digest(
            list(result.preserved_messages),
            result.summary_text,
        ),
        source_tokens=result.total_tokens,
        result_tokens=result_tokens,
        summary_tokens=min(summary_tokens, result_tokens),
        summary_digest=hashlib.sha256(result.summary_text.encode("utf-8")).hexdigest(),
    )
    return {}


def context_state_digest(
    messages: list[AnyMessage],
    summary: str | None,
) -> str:
    material = json.dumps(
        {
            "messages": [message_to_dict(message) for message in messages],
            "summary": summary,
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()
