"""Hidden goal continuation: evaluate the active goal and queue the next turn."""

from __future__ import annotations

import asyncio
import copy
import logging
from typing import Any

from langgraph.types import Overwrite

from deerflow.agents.goal_state import GoalEvaluation, GoalState
from deerflow.config.app_config import AppConfig
from deerflow.runtime.checkpoint_state import (
    CheckpointStateAccessor,
    build_state_mutation_graph,
    graph_state_schema,
)
from deerflow.runtime.events.stream_base import StreamBridge
from deerflow.runtime.goal import (
    DEFAULT_MAX_GOAL_CONTINUATIONS,
    DEFAULT_MAX_NO_PROGRESS_CONTINUATIONS,
    GoalWriteConflict,
    _call_checkpointer_method,
    _is_visible_message,
    _message_type,
    attach_goal_evaluation,
    compute_no_progress_count,
    evaluate_goal_completion,
    goal_thread_lock,
    latest_visible_assistant_signature,
    make_goal_continuation_message,
    read_thread_goal,
    should_continue_goal,
    visible_conversation_signature,
    write_thread_goal,
)
from deerflow.runtime.serialization import serialize
from deerflow.sandbox.sandbox import AuthorizationRevoked
from deerflow.utils.messages import message_to_text

from .checkpoint_rollback import (
    _checkpoint_id,
    _materialized_checkpoint_messages,
    _materialized_checkpoint_snapshot,
    _read_checkpoint_messages,
    _snapshot_values,
)

logger = logging.getLogger(__name__)


def _goal_instance_matches(left: GoalState | None, right: GoalState | None) -> bool:
    if not left or not right:
        return False
    same_status = left.get("status") == right.get("status") == "active"
    same_objective = left.get("objective") == right.get("objective")
    same_created_at = left.get("created_at") == right.get("created_at")
    return same_status and same_objective and same_created_at


async def _materialized_checkpoint_goal(
    accessor: CheckpointStateAccessor,
    thread_id: str,
) -> GoalState | None:
    values = _snapshot_values(await _materialized_checkpoint_snapshot(accessor, thread_id))
    goal = values.get("goal")
    return copy.deepcopy(goal) if isinstance(goal, dict) else None


def _build_run_local_mutation_accessor(
    *,
    accessor: CheckpointStateAccessor,
    checkpointer: Any,
    as_node: str,
    snapshot_frequency: int | None,
) -> CheckpointStateAccessor:
    mutation_graph = build_state_mutation_graph(
        as_node,
        accessor.mode,
        graph_state_schema(accessor.graph),
        snapshot_frequency=snapshot_frequency,
    )
    return CheckpointStateAccessor.bind(
        mutation_graph,
        checkpointer,
        mode=accessor.mode,
    )


async def _write_materialized_goal(
    *,
    accessor: CheckpointStateAccessor,
    checkpointer: Any,
    thread_id: str,
    goal: GoalState | None,
    as_node: str,
    expected_checkpoint_id: str | None,
    snapshot_frequency: int | None,
) -> dict[str, Any]:
    """Replace the goal through a run-local, mode-matched state graph."""

    snapshot = await _materialized_checkpoint_snapshot(accessor, thread_id)
    current_checkpoint_id = _checkpoint_id(snapshot)
    if current_checkpoint_id is None:
        raise LookupError(f"Thread {thread_id} checkpoint not found")
    if expected_checkpoint_id is not None and current_checkpoint_id != expected_checkpoint_id:
        raise GoalWriteConflict(f"Thread {thread_id} goal checkpoint changed while preparing write")

    mutation_accessor = _build_run_local_mutation_accessor(
        accessor=accessor,
        checkpointer=checkpointer,
        as_node=as_node,
        snapshot_frequency=snapshot_frequency,
    )
    await mutation_accessor.aupdate(
        {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": "",
                "checkpoint_id": current_checkpoint_id,
            }
        },
        {"goal": Overwrite(copy.deepcopy(goal))},
        as_node=as_node,
    )
    return _snapshot_values(await _materialized_checkpoint_snapshot(accessor, thread_id))


def _read_checkpoint_goal(checkpoint_tuple: Any) -> GoalState | None:
    checkpoint = getattr(checkpoint_tuple, "checkpoint", {}) or {}
    channel_values = checkpoint.get("channel_values", {}) if isinstance(checkpoint, dict) else {}
    raw_goal = channel_values.get("goal") if isinstance(channel_values, dict) else None
    return copy.deepcopy(raw_goal) if isinstance(raw_goal, dict) else None


def _has_durable_goal_turn_receipt(checkpoint_tuple: Any, messages: list[Any]) -> bool:
    """Return true when a completed visible assistant turn is safely checkpointed.

    ``pending_writes`` is the durability signal: a ``CheckpointTuple`` carries no
    ``tasks`` field (those live on a ``StateSnapshot``), so the presence of any
    queued writes is what tells us the turn is still in flight.
    """
    if _checkpoint_id(checkpoint_tuple) is None:
        return False
    if getattr(checkpoint_tuple, "pending_writes", None):
        return False
    visible_messages = []
    for message in messages:
        if _is_visible_message(message) and message_to_text(message).strip():
            visible_messages.append(message)
    if not visible_messages:
        return False
    return _message_type(visible_messages[-1]) == "ai"


def _stand_down_reason(goal: GoalState, evaluation: GoalEvaluation, no_progress_count: int) -> str | None:
    if evaluation["satisfied"]:
        return None
    if evaluation["blocker"] != "goal_not_met_yet":
        return f"blocked:{evaluation['blocker']}"
    # Default caps mirror should_continue_goal so the two gate functions agree on
    # a goal dict that is missing these fields.
    if int(goal.get("continuation_count", 0)) >= int(goal.get("max_continuations", DEFAULT_MAX_GOAL_CONTINUATIONS)):
        return "max_continuations_reached"
    if no_progress_count >= int(goal.get("max_no_progress_continuations", DEFAULT_MAX_NO_PROGRESS_CONTINUATIONS)):
        return "no_progress_detected"
    return None


async def _persist_goal_evaluation(
    *,
    bridge: StreamBridge,
    checkpointer: Any,
    accessor: CheckpointStateAccessor | None = None,
    thread_id: str,
    run_id: str,
    goal: GoalState,
    evaluation: GoalEvaluation,
    no_progress_count: int,
    continuation_count: int | None = None,
    stand_down_reason: str | None = None,
    evidence_signature: str = "",
    snapshot_frequency: int | None = None,
) -> GoalState | None:
    try:
        async with goal_thread_lock(thread_id):
            if accessor is not None:
                snapshot = await _materialized_checkpoint_snapshot(
                    accessor,
                    thread_id,
                )
                current_goal = _snapshot_values(snapshot).get("goal")
                current_goal = copy.deepcopy(current_goal) if isinstance(current_goal, dict) else None
                expected_checkpoint_id = _checkpoint_id(snapshot)
            else:
                checkpoint_tuple = await _call_checkpointer_method(
                    checkpointer,
                    "aget_tuple",
                    "get_tuple",
                    {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": "",
                        }
                    },
                )
                if checkpoint_tuple is None:
                    return None
                current_goal = _read_checkpoint_goal(checkpoint_tuple)
                expected_checkpoint_id = _checkpoint_id(checkpoint_tuple)
            if current_goal is None or not _goal_instance_matches(goal, current_goal):
                return None
            # The caller may have computed its next count before another
            # continuation committed. Advance from the fresh, locked value so
            # a stale writer cannot overwrite or collapse a real attempt.
            if continuation_count is not None:
                current_count = int(current_goal.get("continuation_count", 0))
                continuation_count = max(continuation_count, current_count + 1)
            updated_goal = attach_goal_evaluation(
                current_goal,
                evaluation,
                run_id=run_id,
                continuation_count=continuation_count,
                no_progress_count=no_progress_count,
                stand_down_reason=stand_down_reason,
                evidence_signature=evidence_signature,
            )
            if accessor is not None:
                values = await _write_materialized_goal(
                    accessor=accessor,
                    checkpointer=checkpointer,
                    thread_id=thread_id,
                    goal=updated_goal,
                    as_node="goal_evaluator",
                    expected_checkpoint_id=expected_checkpoint_id,
                    snapshot_frequency=snapshot_frequency,
                )
            else:
                values = await write_thread_goal(
                    checkpointer,
                    thread_id,
                    updated_goal,
                    as_node="goal_evaluator",
                    expected_checkpoint_id=expected_checkpoint_id,
                )
        await bridge.publish(run_id, "values", serialize(values, mode="values"))
        return updated_goal
    except GoalWriteConflict:
        return None
    except Exception:
        logger.warning("Could not persist goal evaluation for thread %s", thread_id, exc_info=True)
        return None


async def _reread_goal_and_checkpoint(
    checkpointer: Any,
    thread_id: str,
    *,
    accessor: CheckpointStateAccessor | None = None,
) -> tuple[GoalState | None, Any]:
    """Re-read the goal and latest checkpoint together for a concurrency re-check."""
    goal = await _materialized_checkpoint_goal(accessor, thread_id) if accessor is not None else await read_thread_goal(checkpointer, thread_id)
    checkpoint_tuple = await _call_checkpointer_method(
        checkpointer,
        "aget_tuple",
        "get_tuple",
        {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
    )
    return goal, checkpoint_tuple


async def _prepare_goal_continuation_input(
    *,
    bridge: StreamBridge,
    checkpointer: Any,
    accessor: CheckpointStateAccessor | None = None,
    thread_id: str,
    run_id: str,
    model_name: str | None,
    app_config: AppConfig | None,
    snapshot_frequency: int | None = None,
    evaluator_model_factory: Any | None = None,
    abort_event: asyncio.Event | None = None,
    authorization_boundary: object | None = None,
) -> dict[str, Any] | None:
    """Evaluate the active goal and return a hidden continuation input if needed.

    NOTE: The re-reads below catch a racing user message or ``/goal clear``
    before we queue a continuation. Goal writes then serialize per thread and
    pass the checkpoint id they read from, so stale evaluator writes stand down
    instead of clobbering a newer goal change.
    """
    if checkpointer is None:
        return None
    if abort_event is not None and abort_event.is_set():
        return None

    try:
        goal = await _materialized_checkpoint_goal(accessor, thread_id) if accessor is not None else await read_thread_goal(checkpointer, thread_id)
    except Exception:
        logger.warning("Could not read goal for thread %s after run %s", thread_id, run_id, exc_info=True)
        return None
    if not goal or goal.get("status") != "active":
        return None

    async def _persist(
        goal: GoalState,
        evaluation: GoalEvaluation,
        no_progress_count: int,
        *,
        stand_down_reason: str | None = None,
        continuation_count: int | None = None,
    ) -> GoalState | None:
        """Record the evaluation against the still-current goal instance."""
        return await _persist_goal_evaluation(
            bridge=bridge,
            checkpointer=checkpointer,
            accessor=accessor,
            thread_id=thread_id,
            run_id=run_id,
            goal=goal,
            evaluation=evaluation,
            no_progress_count=no_progress_count,
            continuation_count=continuation_count,
            stand_down_reason=stand_down_reason,
            evidence_signature=evidence_signature,
            snapshot_frequency=snapshot_frequency,
        )

    try:
        checkpoint_tuple = await _call_checkpointer_method(
            checkpointer,
            "aget_tuple",
            "get_tuple",
            {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
        )
        if checkpoint_tuple is None:
            return None
        checkpoint_id_before = _checkpoint_id(checkpoint_tuple)
        messages = await _materialized_checkpoint_messages(accessor, thread_id) if accessor is not None else _read_checkpoint_messages(checkpoint_tuple)
        conversation_signature_before = visible_conversation_signature(messages)
        evidence_signature = latest_visible_assistant_signature(messages)

        if not _has_durable_goal_turn_receipt(checkpoint_tuple, messages):
            evaluation = GoalEvaluation(
                satisfied=False,
                blocker="run_failed",
                reason="No durable assistant end-of-turn receipt was available.",
                evidence_summary="",
            )
            no_progress_count = compute_no_progress_count(goal, evaluation, evidence_signature=evidence_signature)
            await _persist(goal, evaluation, no_progress_count, stand_down_reason="no_durable_end_of_turn")
            return None

        if abort_event is not None and abort_event.is_set():
            return None
        evaluator_model = evaluator_model_factory() if evaluator_model_factory is not None else None
        evaluation = await evaluate_goal_completion(
            goal,
            messages,
            model=evaluator_model,
            model_name=model_name,
            app_config=app_config,
            authorization_boundary=authorization_boundary,
            abort_event=abort_event,
        )
        if abort_event is not None and abort_event.is_set():
            return None
    except AuthorizationRevoked:
        raise
    except Exception:
        logger.warning("Goal evaluator failed for thread %s after run %s", thread_id, run_id, exc_info=True)
        return None

    no_progress_count = compute_no_progress_count(goal, evaluation, evidence_signature=evidence_signature)

    # Re-check that neither the goal nor the visible conversation changed while the
    # evaluator ran — a user message or /goal clear racing the evaluation must win.
    try:
        current_goal, current_checkpoint_tuple = await _reread_goal_and_checkpoint(
            checkpointer,
            thread_id,
            accessor=accessor,
        )
    except Exception:
        logger.warning("Could not re-check goal state for thread %s after evaluation", thread_id, exc_info=True)
        return None

    if not _goal_instance_matches(goal, current_goal) or current_checkpoint_tuple is None:
        return None

    checkpoint_changed = _checkpoint_id(current_checkpoint_tuple) != checkpoint_id_before
    current_messages = await _materialized_checkpoint_messages(accessor, thread_id) if accessor is not None else _read_checkpoint_messages(current_checkpoint_tuple)
    messages_changed = visible_conversation_signature(current_messages) != conversation_signature_before
    if checkpoint_changed or messages_changed:
        await _persist(current_goal, evaluation, no_progress_count, stand_down_reason="thread_changed_after_evaluation")
        return None

    if evaluation["satisfied"]:
        try:
            async with goal_thread_lock(thread_id):
                latest_checkpoint_tuple = await _call_checkpointer_method(
                    checkpointer,
                    "aget_tuple",
                    "get_tuple",
                    {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
                )
                if latest_checkpoint_tuple is None:
                    return None
                latest_goal = (
                    await _materialized_checkpoint_goal(
                        accessor,
                        thread_id,
                    )
                    if accessor is not None
                    else _read_checkpoint_goal(latest_checkpoint_tuple)
                )
                if latest_goal is None or not _goal_instance_matches(goal, latest_goal):
                    return None
                if accessor is not None:
                    values = await _write_materialized_goal(
                        accessor=accessor,
                        checkpointer=checkpointer,
                        thread_id=thread_id,
                        goal=None,
                        as_node="goal_evaluator",
                        expected_checkpoint_id=_checkpoint_id(latest_checkpoint_tuple),
                        snapshot_frequency=snapshot_frequency,
                    )
                else:
                    values = await write_thread_goal(
                        checkpointer,
                        thread_id,
                        None,
                        as_node="goal_evaluator",
                        expected_checkpoint_id=_checkpoint_id(latest_checkpoint_tuple),
                    )
            await bridge.publish(run_id, "values", serialize(values, mode="values"))
        except GoalWriteConflict:
            return None
        except Exception:
            logger.warning("Could not clear satisfied goal for thread %s", thread_id, exc_info=True)
        return None

    stand_down_reason = _stand_down_reason(goal, evaluation, no_progress_count)
    if stand_down_reason is not None or not should_continue_goal(goal, evaluation, no_progress_count=no_progress_count):
        await _persist(goal, evaluation, no_progress_count, stand_down_reason=stand_down_reason)
        return None

    next_count = int(goal.get("continuation_count", 0)) + 1
    updated_goal = await _persist(goal, evaluation, no_progress_count, continuation_count=next_count)
    if updated_goal is None:
        return None

    # Final guard: the persist above bumped the checkpoint id, so only the visible
    # conversation signature is meaningful for detecting a racing user turn here.
    try:
        latest_goal, latest_checkpoint_tuple = await _reread_goal_and_checkpoint(
            checkpointer,
            thread_id,
            accessor=accessor,
        )
    except Exception:
        logger.warning("Could not verify queued goal continuation for thread %s", thread_id, exc_info=True)
        return None
    if not _goal_instance_matches(updated_goal, latest_goal) or latest_checkpoint_tuple is None:
        return None
    latest_messages = await _materialized_checkpoint_messages(accessor, thread_id) if accessor is not None else _read_checkpoint_messages(latest_checkpoint_tuple)
    if visible_conversation_signature(latest_messages) != conversation_signature_before:
        # The first persist already counted this continuation attempt. This
        # second write only records why delivery stood down; passing the same
        # count again would make the fresh-count race guard add a second unit.
        await _persist(
            latest_goal,
            evaluation,
            no_progress_count,
            stand_down_reason="thread_changed_before_continuation",
        )
        return None

    logger.info(
        "Run %s continuing thread %s for active goal (%d/%d)",
        run_id,
        thread_id,
        updated_goal.get("continuation_count", next_count),
        updated_goal.get("max_continuations", 0),
    )
    return {"messages": [make_goal_continuation_message(updated_goal, evaluation)]}
