from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

from deerflow.agents.middlewares.summarization_middleware import (
    ContextCompactionResult,
    DeerFlowSummarizationMiddleware,
)


@pytest.mark.asyncio
async def test_subagent_compaction_records_ephemeral_state_identity_without_checkpoint() -> None:
    observer = SimpleNamespace(
        record_ephemeral_compaction_committed=AsyncMock(),
    )
    middleware = object.__new__(DeerFlowSummarizationMiddleware)
    middleware._context_compaction_observer = observer
    middleware.token_counter = lambda messages: len(messages) * 10
    source_messages = [HumanMessage(id=f"source-{index}", content=f"message {index}") for index in range(5)]
    result = ContextCompactionResult(
        summary_text="safe summary",
        messages_to_summarize=tuple(source_messages[:4]),
        preserved_messages=(source_messages[-1],),
        total_tokens=100,
        memory_archive_receipt=None,
    )

    update = await middleware._acontext_compaction_update(
        {"messages": source_messages},
        result,
        Runtime(context={}),
    )

    assert update == {}
    call = observer.record_ephemeral_compaction_committed.await_args.kwargs
    assert set(call) == {
        "source_state_digest",
        "result_state_digest",
        "source_tokens",
        "result_tokens",
        "summary_tokens",
        "summary_digest",
    }
    assert len(call["source_state_digest"]) == 64
    assert len(call["result_state_digest"]) == 64
    assert call["source_state_digest"] != call["result_state_digest"]
    assert call["source_tokens"] == 100
    assert call["result_tokens"] == 20
    assert call["summary_tokens"] == 10
