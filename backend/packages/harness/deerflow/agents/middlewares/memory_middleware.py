"""Middleware for memory mechanism."""

import logging
from typing import TYPE_CHECKING, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langgraph.config import get_config
from langgraph.runtime import Runtime

from deerflow.agents.memory.message_processing import detect_correction, detect_reinforcement, filter_messages_for_memory
from deerflow.agents.memory.queue import get_project_memory_queue
from deerflow.config.app_config import AppConfig, is_trace_correlation_enabled
from deerflow.config.memory_config import MemoryConfig
from deerflow.private_scope import PrivateResourceScope
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

    def __init__(
        self,
        agent_name: str | None = None,
        *,
        memory_config: "MemoryConfig | None" = None,
        app_config: AppConfig | None = None,
    ):
        """Initialize the MemoryMiddleware.

        Args:
            agent_name: Optional project Memory namespace suffix.
            memory_config: Explicit memory processing configuration.
        """
        super().__init__()
        self._agent_name = agent_name
        self._memory_config = memory_config
        self._app_config = app_config

    @override
    def after_agent(self, state: MemoryMiddlewareState, runtime: Runtime) -> dict | None:
        """Queue conversation for memory update after agent completes.

        Args:
            state: The current agent state.
            runtime: The runtime context.

        Returns:
            None (no state changes needed from this middleware).
        """
        # PostgreSQL project Memory is async-only. A sync hook has no trusted
        # project transaction boundary, so it must never persist anything.
        logger.debug("Project memory updates require the async middleware hook")
        return None

    @override
    async def aafter_agent(self, state: MemoryMiddlewareState, runtime: Runtime) -> dict | None:
        """Use the asyncio project queue for admitted private runs.

        Non-project executions fail closed and do not persist Memory.
        """

        runtime_context = runtime.context if isinstance(runtime.context, dict) else {}
        private_scope = runtime_context.get("private_scope")
        if not isinstance(private_scope, PrivateResourceScope):
            return None

        config = self._memory_config
        if type(config) is not MemoryConfig:
            return None
        if not config.enabled:
            return None
        app_config = runtime_context.get("app_config") or self._app_config
        if type(app_config) is not AppConfig:
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
            memory_config=config,
            app_config=app_config,
            correction_detected=correction_detected,
            reinforcement_detected=reinforcement_detected,
            deerflow_trace_id=deerflow_trace_id,
            langfuse_trace_correlation_enabled=is_trace_correlation_enabled(
                runtime_context.get("app_config"),
            ),
        )
        return None
