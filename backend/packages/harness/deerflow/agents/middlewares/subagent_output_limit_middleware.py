"""Observe Provider output truncation for one delegated Agent Graph."""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable
from typing import override

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    ExtendedModelResponse,
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AIMessage

from deerflow.agents.middlewares.output_limit_recovery_middleware import (
    message_reports_output_limit,
)


def _model_response(result: ModelCallResult) -> ModelResponse:
    if isinstance(result, ExtendedModelResponse):
        return result.model_response
    if isinstance(result, AIMessage):
        return ModelResponse(result=[result])
    return result


class SubagentOutputLimitMiddleware(AgentMiddleware):
    """Turn a raw Provider output-limit reason into one consumable receipt.

    Each Sub-Agent Task builds a fresh middleware instance. The executor
    consumes the observation after the Graph returns, preserving usable
    partial text while preventing that response from becoming a clean success.
    """

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._output_truncated = False

    def _observe(self, result: ModelCallResult) -> ModelCallResult:
        response = _model_response(result)
        if any(isinstance(message, AIMessage) and message_reports_output_limit(message) for message in response.result):
            with self._lock:
                self._output_truncated = True
        return result

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelCallResult],
    ) -> ModelCallResult:
        return self._observe(handler(request))

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelCallResult]],
    ) -> ModelCallResult:
        return self._observe(await handler(request))

    def consume_stop_reason(self, _scope_id: str | None) -> str | None:
        """Consume the task-local output-limit observation exactly once."""

        with self._lock:
            if not self._output_truncated:
                return None
            self._output_truncated = False
        return "output_truncated"


__all__ = ["SubagentOutputLimitMiddleware"]
