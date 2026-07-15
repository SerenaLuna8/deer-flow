"""Middleware for memory mechanism."""

import logging
from typing import TYPE_CHECKING, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langgraph.config import get_config
from langgraph.runtime import Runtime

from deerflow.agents.memory.message_processing import detect_correction, detect_reinforcement, filter_messages_for_memory
from deerflow.agents.memory.queue import get_memory_queue, get_project_memory_queue
from deerflow.config.memory_config import get_memory_config
from deerflow.private_scope import PrivateResourceScope
from deerflow.runtime.user_context import get_effective_user_id
from deerflow.trace_context import DEERFLOW_TRACE_METADATA_KEY, get_current_trace_id, normalize_trace_id

if TYPE_CHECKING:
    from deerflow.config.memory_config import MemoryConfig

logger = logging.getLogger(__name__)


class MemoryMiddlewareState(AgentState):
    """Compatible with the `ThreadState` schema."""

    pass


class MemoryMiddleware(AgentMiddleware[MemoryMiddlewareState]):
    """Middleware that queues conversation for memory update after agent execution.

    This middleware:
    1. After each agent execution, queues the conversation for memory update
    2. Only includes user inputs and final assistant responses (ignores tool calls)
    3. The queue uses debouncing to batch multiple updates together
    4. Memory is updated asynchronously via LLM summarization
    """

    state_schema = MemoryMiddlewareState

    def __init__(self, agent_name: str | None = None, *, memory_config: "MemoryConfig | None" = None):
        """Initialize the MemoryMiddleware.

        Args:
            agent_name: If provided, memory is stored per-agent. If None, uses global memory.
            memory_config: Explicit memory config. When omitted, legacy global
                config fallback is used.
        """
        super().__init__()
        self._agent_name = agent_name
        self._memory_config = memory_config

    @override
    def after_agent(self, state: MemoryMiddlewareState, runtime: Runtime) -> dict | None:
        """Queue conversation for memory update after agent completes.

        Args:
            state: The current agent state.
            runtime: The runtime context.

        Returns:
            None (no state changes needed from this middleware).
        """
        runtime_context = runtime.context if isinstance(runtime.context, dict) else {}
        if isinstance(runtime_context.get("private_scope"), PrivateResourceScope):
            # Private runs use ``aafter_agent`` so PostgreSQL work stays on the
            # runtime event loop. A sync-only invocation must never leak into
            # the legacy per-user file store.
            logger.debug("Private memory updates require the async middleware hook")
            return None

        config = self._memory_config or get_memory_config()
        if not config.enabled:
            return None

        # Get thread ID from runtime context first, then fall back to LangGraph's configurable metadata
        thread_id = runtime.context.get("thread_id") if runtime.context else None
        if thread_id is None:
            config_data = get_config()
            thread_id = config_data.get("configurable", {}).get("thread_id")
        if not thread_id:
            logger.debug("No thread_id in context, skipping memory update")
            return None

        # Get messages from state
        messages = state.get("messages", [])
        if not messages:
            logger.debug("No messages in state, skipping memory update")
            return None

        # Filter to only keep user inputs and final assistant responses
        filtered_messages = filter_messages_for_memory(messages)

        # Only queue if there's meaningful conversation
        # At minimum need one user message and one assistant response
        user_messages = [m for m in filtered_messages if getattr(m, "type", None) == "human"]
        assistant_messages = [m for m in filtered_messages if getattr(m, "type", None) == "ai"]

        if not user_messages or not assistant_messages:
            return None

        # Queue the filtered conversation for memory update
        correction_detected = detect_correction(filtered_messages)
        reinforcement_detected = not correction_detected and detect_reinforcement(filtered_messages)
        # Capture user_id at enqueue time while the request context is still alive.
        # threading.Timer fires on a different thread where ContextVar values are not
        # propagated, so we must store user_id explicitly in ConversationContext.
        user_id = get_effective_user_id()
        runtime_context = runtime.context if isinstance(runtime.context, dict) else {}
        deerflow_trace_id = normalize_trace_id(runtime_context.get(DEERFLOW_TRACE_METADATA_KEY))
        if deerflow_trace_id is None:
            try:
                config_data = get_config()
            except RuntimeError:
                config_data = {}
            config_metadata = config_data.get("metadata", {}) if isinstance(config_data.get("metadata"), dict) else {}
            deerflow_trace_id = normalize_trace_id(config_metadata.get(DEERFLOW_TRACE_METADATA_KEY))
        if deerflow_trace_id is None:
            deerflow_trace_id = get_current_trace_id()
        queue = get_memory_queue()
        queue.add(
            thread_id=thread_id,
            messages=filtered_messages,
            agent_name=self._agent_name,
            user_id=user_id,
            deerflow_trace_id=deerflow_trace_id,
            correction_detected=correction_detected,
            reinforcement_detected=reinforcement_detected,
        )

        return None

    @override
    async def aafter_agent(self, state: MemoryMiddlewareState, runtime: Runtime) -> dict | None:
        """Use the asyncio project queue for admitted private runs.

        Non-project executions retain the original synchronous file-memory
        queue path through :meth:`after_agent`.
        """

        runtime_context = runtime.context if isinstance(runtime.context, dict) else {}
        private_scope = runtime_context.get("private_scope")
        if not isinstance(private_scope, PrivateResourceScope):
            return self.after_agent(state, runtime)

        config = self._memory_config or get_memory_config()
        if not config.enabled:
            return None
        thread_id = runtime_context.get("thread_id")
        run_id = runtime_context.get("run_id")
        if not isinstance(thread_id, str) or not thread_id or not isinstance(run_id, str) or not run_id:
            logger.debug("Private memory update requires thread_id and run_id")
            return None

        messages = state.get("messages", [])
        if not messages:
            return None
        filtered_messages = filter_messages_for_memory(messages)
        if not any(getattr(message, "type", None) == "human" for message in filtered_messages) or not any(getattr(message, "type", None) == "ai" for message in filtered_messages):
            return None

        correction_detected = detect_correction(filtered_messages)
        reinforcement_detected = not correction_detected and detect_reinforcement(filtered_messages)
        deerflow_trace_id = normalize_trace_id(runtime_context.get(DEERFLOW_TRACE_METADATA_KEY))
        if deerflow_trace_id is None:
            try:
                config_data = get_config()
            except RuntimeError:
                config_data = {}
            metadata = config_data.get("metadata", {}) if isinstance(config_data.get("metadata"), dict) else {}
            deerflow_trace_id = normalize_trace_id(metadata.get(DEERFLOW_TRACE_METADATA_KEY))
        if deerflow_trace_id is None:
            deerflow_trace_id = get_current_trace_id()

        namespace = "default" if self._agent_name is None else f"agent:{self._agent_name}"
        get_project_memory_queue().enqueue(
            scope=private_scope,
            thread_id=thread_id,
            run_id=run_id,
            namespace=namespace,
            messages=filtered_messages,
            correction_detected=correction_detected,
            reinforcement_detected=reinforcement_detected,
            deerflow_trace_id=deerflow_trace_id,
        )
        return None
