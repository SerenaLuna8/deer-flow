"""Hooks fired before summarization removes messages from state."""

from __future__ import annotations

from deerflow.agents.memory.message_processing import detect_correction, detect_reinforcement, filter_messages_for_memory
from deerflow.agents.memory.queue import get_project_memory_queue
from deerflow.agents.middlewares.summarization_middleware import SummarizationEvent
from deerflow.config.memory_config import get_memory_config
from deerflow.private_scope import PrivateResourceScope


def memory_flush_hook(event: SummarizationEvent) -> None:
    """Flush messages about to be summarized into the memory queue."""
    if not get_memory_config().enabled or not event.thread_id:
        return

    filtered_messages = filter_messages_for_memory(list(event.messages_to_summarize))
    user_messages = [message for message in filtered_messages if getattr(message, "type", None) == "human"]
    assistant_messages = [message for message in filtered_messages if getattr(message, "type", None) == "ai"]
    if not user_messages or not assistant_messages:
        return

    correction_detected = detect_correction(filtered_messages)
    reinforcement_detected = not correction_detected and detect_reinforcement(filtered_messages)
    runtime_context = getattr(event.runtime, "context", None)
    runtime_context = runtime_context if isinstance(runtime_context, dict) else {}
    private_scope = runtime_context.get("private_scope")
    if not isinstance(private_scope, PrivateResourceScope):
        return
    run_id = runtime_context.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return
    namespace = "default" if event.agent_name is None else f"agent:{event.agent_name}"
    get_project_memory_queue().enqueue(
        scope=private_scope,
        thread_id=event.thread_id,
        run_id=run_id,
        namespace=namespace,
        messages=filtered_messages,
        correction_detected=correction_detected,
        reinforcement_detected=reinforcement_detected,
    )
