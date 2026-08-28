from __future__ import annotations

from typing import Any

import pytest
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.runtime import Runtime

from deerflow.agents.middlewares.subagent_output_limit_middleware import (
    SubagentOutputLimitMiddleware,
)


def _request() -> ModelRequest:
    return ModelRequest(
        model=object(),
        messages=[HumanMessage(content="question")],
        tools=[],
        tool_choice=None,
        response_format=None,
        state={},
        runtime=Runtime(context={"run_id": "parent-run"}),
        model_settings={},
    )


@pytest.mark.parametrize(
    "provider_metadata",
    [
        {"finish_reason": "length"},
        {"stop_reason": "max_tokens"},
        {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
        },
    ],
)
def test_raw_provider_output_limit_becomes_exact_subagent_stop_reason(
    provider_metadata: dict[str, Any],
) -> None:
    middleware = SubagentOutputLimitMiddleware()
    response = ModelResponse(
        result=[
            AIMessage(
                content="usable partial result",
                response_metadata=provider_metadata,
            )
        ]
    )

    assert middleware.wrap_model_call(_request(), lambda _request: response) is response
    assert middleware.consume_stop_reason("parent-run") == "output_truncated"
    assert middleware.consume_stop_reason("parent-run") is None


def test_clean_provider_response_does_not_create_a_stop_reason() -> None:
    middleware = SubagentOutputLimitMiddleware()

    middleware.wrap_model_call(
        _request(),
        lambda _request: ModelResponse(result=[AIMessage(content="complete", response_metadata={"finish_reason": "stop"})]),
    )

    assert middleware.consume_stop_reason("parent-run") is None


@pytest.mark.asyncio
async def test_async_raw_provider_response_uses_the_same_closed_classifier() -> None:
    middleware = SubagentOutputLimitMiddleware()
    response = ModelResponse(
        result=[
            AIMessage(
                content="usable partial result",
                additional_kwargs={"stop_reason": "max_tokens"},
            )
        ]
    )

    async def handler(_request: ModelRequest) -> ModelResponse:
        return response

    assert await middleware.awrap_model_call(_request(), handler) is response
    assert middleware.consume_stop_reason("parent-run") == "output_truncated"
