from __future__ import annotations

import asyncio
import hashlib
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import Field

from deerflow.agents.memory.snip import (
    MEMORY_ARCHIVE_CONTEXT_KEY,
    SNIP_ARCHIVE_PROMPT,
    SNIP_ARCHIVE_PROMPT_VERSION,
    SNIP_NOTHING,
    SNIP_RETRY_REINFORCEMENT,
    SnipArchiveContext,
    compute_snip_source_digest,
)
from deerflow.agents.middlewares import summarization_middleware as summarization_module
from deerflow.agents.middlewares.summarization_middleware import (
    DeerFlowSummarizationMiddleware,
)
from deerflow.config.model_execution import SystemModelExecutionProvenance
from deerflow.models import ModelRuntimeProfile
from deerflow.runtime import context_compaction as context_compaction_module
from deerflow.runtime.context_compaction import prepare_thread_compaction
from deerflow.runtime.context_keys import RuntimeContextKeys


class _RecordingModel(FakeListChatModel):
    prompts: list[str] = Field(default_factory=list)
    call_count: int = 0

    def _call(self, *args: Any, **kwargs: Any) -> str:
        messages = args[0]
        self.prompts.append("\n".join(str(message.content) for message in messages))
        self.call_count += 1
        return super()._call(*args, **kwargs)


class _LowBudgetModel(_RecordingModel):
    max_tokens: int | None = 512


def _dual(continuity: str, tagged: str) -> str:
    return f"<continuity>\n{continuity}\n</continuity>\n{tagged}"


def _model(*responses: str) -> _RecordingModel:
    return _RecordingModel(
        responses=list(responses),
        custom_get_token_ids=lambda text: list(range(len(text))),
    )


def _middleware(model: _RecordingModel, *, keep: int = 1) -> DeerFlowSummarizationMiddleware:
    return DeerFlowSummarizationMiddleware(
        model=model,
        trigger=("messages", 1),
        keep=("messages", keep),
        trim_tokens_to_summarize=20_000,
        summary_prompt=SNIP_ARCHIVE_PROMPT,
    )


def _archive_context(
    *,
    source_checkpoint_id: str | None = "checkpoint-source",
) -> SnipArchiveContext:
    return SnipArchiveContext(
        enabled=True,
        project_id=uuid.UUID("10000000-0000-4000-8000-000000000001"),
        owner_user_id="20000000-0000-4000-8000-000000000002",
        namespace="default",
        preference_version=7,
        summary_model=SystemModelExecutionProvenance(
            model_config_id=uuid.UUID("30000000-0000-4000-8000-000000000003"),
            payload_checksum="a" * 64,
            secret_generation_id=uuid.UUID("40000000-0000-4000-8000-000000000004"),
            secret_envelope_digest="b" * 64,
        ),
        source_checkpoint_id=source_checkpoint_id,
    )


def _runtime(
    *,
    archive_context: SnipArchiveContext | None = None,
    execution_checkpoint_id: str | None = None,
    server_abort_event: asyncio.Event | None = None,
) -> SimpleNamespace:
    context: dict[str, object] = {"thread_id": "thread-1"}
    if archive_context is not None:
        context[MEMORY_ARCHIVE_CONTEXT_KEY] = archive_context
    if server_abort_event is not None:
        context[RuntimeContextKeys.SERVER_ABORT_EVENT] = server_abort_event
    execution_info = None if execution_checkpoint_id is None else SimpleNamespace(checkpoint_id=execution_checkpoint_id)
    return SimpleNamespace(
        context=context,
        execution_info=execution_info,
    )


def _complete_tool_turn(prefix: str) -> list[object]:
    return [
        HumanMessage(id=f"{prefix}-human", content=f"{prefix} user"),
        AIMessage(
            id=f"{prefix}-tool-call",
            content="",
            tool_calls=[
                {
                    "name": "lookup",
                    "args": {"value": prefix},
                    "id": f"{prefix}-call",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(
            id=f"{prefix}-tool-result",
            content=f"{prefix} result",
            tool_call_id=f"{prefix}-call",
        ),
        AIMessage(id=f"{prefix}-assistant", content=f"{prefix} answer"),
    ]


def _many_tool_call_turn(count: int = 40) -> list[object]:
    messages: list[object] = [
        HumanMessage(id="many-human", content="Research all sources."),
    ]
    for index in range(count):
        call_id = f"many-call-{index}"
        messages.extend(
            [
                AIMessage(
                    id=f"many-ai-{index}",
                    content="thinking-" + "x" * 60,
                    tool_calls=[
                        {
                            "name": "lookup",
                            "args": {"index": index},
                            "id": call_id,
                            "type": "tool_call",
                        }
                    ],
                ),
                ToolMessage(
                    id=f"many-tool-{index}",
                    name="lookup",
                    content="result-" + "y" * 100,
                    tool_call_id=call_id,
                    status="success",
                ),
            ]
        )
    messages.append(AIMessage(id="many-final", content="Final answer."))
    return messages


def _clarification_request_messages(prefix: str) -> list[object]:
    tool_call_id = f"{prefix}-call"
    request_id = f"clarification:{tool_call_id}"
    return [
        HumanMessage(id=f"{prefix}-human", content=f"{prefix} user"),
        AIMessage(
            id=f"{prefix}-tool-call",
            content="",
            tool_calls=[
                {
                    "name": "lookup",
                    "args": {"value": prefix},
                    "id": f"{prefix}-lookup-call",
                    "type": "tool_call",
                },
                {
                    "name": "ask_clarification",
                    "args": {"question": "Which environment?"},
                    "id": tool_call_id,
                    "type": "tool_call",
                },
            ],
        ),
        ToolMessage(
            id=f"{prefix}-lookup-result",
            content=f"{prefix} lookup result",
            tool_call_id=f"{prefix}-lookup-call",
        ),
        ToolMessage(
            id=request_id,
            content="Which environment?",
            tool_call_id=tool_call_id,
            name="ask_clarification",
            artifact={
                "human_input": {
                    "version": 1,
                    "kind": "human_input_request",
                    "source": "ask_clarification",
                    "request_id": request_id,
                    "tool_call_id": tool_call_id,
                    "question": "Which environment?",
                    "input_mode": "free_text",
                }
            },
        ),
    ]


def _clarification_response(
    prefix: str,
    *,
    request_id: str | None = None,
) -> HumanMessage:
    resolved_request_id = request_id or f"clarification:{prefix}-call"
    return HumanMessage(
        id=f"{prefix}-response",
        content='For your clarification "Which environment?", my answer is: staging',
        additional_kwargs={
            "hide_from_ui": True,
            "human_input_response": {
                "version": 1,
                "kind": "human_input_response",
                "source": "ask_clarification",
                "request_id": resolved_request_id,
                "response_kind": "text",
                "value": "staging",
            },
        },
    )


class _SnapshotAccessor:
    def __init__(self, messages: list[object]) -> None:
        self.snapshot = SimpleNamespace(
            values={"messages": messages},
            config={
                "configurable": {
                    "thread_id": "thread-1",
                    "checkpoint_ns": "",
                    "checkpoint_id": "checkpoint-source",
                }
            },
        )

    async def aget(self, _config: dict[str, object]) -> SimpleNamespace:
        return self.snapshot


def _keep_zero_middleware(
    model: _RecordingModel,
    *,
    trim_tokens_to_summarize: int = 20_000,
) -> DeerFlowSummarizationMiddleware:
    return DeerFlowSummarizationMiddleware(
        model=model,
        trigger=("messages", 1),
        keep=("messages", 1),
        trim_tokens_to_summarize=trim_tokens_to_summarize,
        summary_prompt=SNIP_ARCHIVE_PROMPT,
        compact_all_complete_turns=True,
    )


def test_compaction_uses_complete_turns_excludes_dynamic_reminders_and_calls_once() -> None:
    continuity = "Goal: answer both lookups. Turns one and two are complete; turn three is still open."
    tagged = "- [durable] The verified result is retained."
    model = _model(_dual(continuity, tagged), "must-not-be-called")
    middleware = _middleware(model)
    messages = [
        SystemMessage(
            id="turn-1-reminder",
            content="hidden date one",
            additional_kwargs={
                "hide_from_ui": True,
                "dynamic_context_reminder": True,
            },
        ),
        *_complete_tool_turn("turn-1"),
        SystemMessage(
            id="turn-2-reminder",
            content="hidden date two",
            additional_kwargs={
                "hide_from_ui": True,
                "dynamic_context_reminder": True,
            },
        ),
        HumanMessage(
            id="turn-2-memory",
            content="hidden memory two",
            additional_kwargs={
                "hide_from_ui": True,
                "dynamic_context_reminder": True,
            },
        ),
        HumanMessage(id="turn-2-human", content="turn-2 user"),
        AIMessage(id="turn-2-assistant", content="turn-2 answer"),
        HumanMessage(id="turn-3-human", content="unfinished turn"),
    ]

    result = middleware.compact_state(
        {"messages": messages, "summary_text": "- [permanent] Existing preference."},
        _runtime(archive_context=_archive_context()),
        force=True,
    )

    assert result is not None
    assert model.call_count == 1
    assert tuple(message.id for message in result.preserved_messages) == ("turn-3-human",)
    assert tuple(message.id for message in result.messages_to_summarize) == tuple(message.id for message in messages[:-1])
    assert "hidden date one" not in model.prompts[0]
    assert "hidden date two" not in model.prompts[0]
    assert "hidden memory two" not in model.prompts[0]
    assert "turn-1 user" in model.prompts[0]
    assert "turn-2 answer" in model.prompts[0]
    assert result.summary_text == continuity
    assert result.memory_archive_receipt is not None
    assert result.memory_archive_receipt["tagged_text"] == tagged
    assert result.memory_archive_receipt["thread_id"] == "thread-1"
    assert result.memory_archive_receipt["source_checkpoint_id"] == "checkpoint-source"
    assert result.memory_archive_receipt["snip_prompt_version"] == SNIP_ARCHIVE_PROMPT_VERSION
    assert result.memory_archive_receipt["source_digest"] == compute_snip_source_digest(
        previous_summary="- [permanent] Existing preference.",
        source_checkpoint_id="checkpoint-source",
        messages=messages[:-1],
    )


def test_sync_compaction_invokes_summary_through_model_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _model("must-not-be-called-directly")
    middleware = _middleware(model)
    observed: dict[str, object] = {}

    def invoke_runnable(
        runnable: object,
        input_: object,
        *,
        profile: ModelRuntimeProfile,
        config: object,
    ) -> SimpleNamespace:
        observed.update(
            runnable=runnable,
            input=input_,
            profile=profile,
            config=config,
        )
        return SimpleNamespace(
            text=_dual("Synchronous continuity.", "- [durable] Sync fact."),
        )

    monkeypatch.setattr(
        summarization_module.ModelRuntime,
        "invoke_runnable",
        staticmethod(invoke_runnable),
    )

    result = middleware.compact_state(
        {
            "messages": [
                HumanMessage(id="old-human", content="old"),
                AIMessage(id="old-ai", content="old answer"),
                HumanMessage(id="current-human", content="current"),
            ],
        },
        _runtime(),
        force=True,
    )

    assert result is not None
    assert result.summary_text == "Synchronous continuity."
    assert observed["runnable"] is middleware._summary_model
    assert observed["profile"] is ModelRuntimeProfile.AGENT_GRAPH
    assert observed["config"] == {
        "metadata": {"lc_source": "summarization"},
    }
    assert isinstance(observed["input"], str)
    assert model.call_count == 0


@pytest.mark.asyncio
async def test_async_compaction_invokes_runtime_and_propagates_server_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _model("must-not-be-called-directly")
    middleware = _middleware(model)
    abort_event = asyncio.Event()
    observed: dict[str, object] = {}

    async def ainvoke_runnable(
        runnable: object,
        input_: object,
        *,
        profile: ModelRuntimeProfile,
        config: object,
        abort_event: object,
    ) -> SimpleNamespace:
        observed.update(
            runnable=runnable,
            input=input_,
            profile=profile,
            config=config,
            abort_event=abort_event,
        )
        return SimpleNamespace(
            text=_dual("Asynchronous continuity.", "- [durable] Async fact."),
        )

    monkeypatch.setattr(
        summarization_module.ModelRuntime,
        "ainvoke_runnable",
        staticmethod(ainvoke_runnable),
    )

    result = await middleware.acompact_state(
        {
            "messages": [
                HumanMessage(id="old-human", content="old"),
                AIMessage(id="old-ai", content="old answer"),
                HumanMessage(id="current-human", content="current"),
            ],
        },
        _runtime(server_abort_event=abort_event),
        force=True,
    )

    assert result is not None
    assert result.summary_text == "Asynchronous continuity."
    assert observed["runnable"] is middleware._summary_model
    assert observed["profile"] is ModelRuntimeProfile.AGENT_GRAPH
    assert observed["config"] == {
        "metadata": {"lc_source": "summarization"},
    }
    assert observed["abort_event"] is abort_event
    assert isinstance(observed["input"], str)
    assert model.call_count == 0


def test_completed_clarification_continuation_is_one_compactable_logical_turn() -> None:
    model = _model(
        _dual(
            "Deployment target clarified as staging; both turns are complete.",
            "- [durable] The completed clarification is retained.",
        )
    )
    middleware = _keep_zero_middleware(model)
    messages = [
        SystemMessage(
            id="clarification-prefix",
            content="hidden source-run context",
            additional_kwargs={
                "hide_from_ui": True,
                "dynamic_context_reminder": True,
            },
        ),
        *_clarification_request_messages("clarification"),
        SystemMessage(
            id="continuation-reminder",
            content="hidden continuation context",
            additional_kwargs={
                "hide_from_ui": True,
                "dynamic_context_reminder": True,
            },
        ),
        _clarification_response("clarification"),
        AIMessage(id="clarification-final", content="Deploying to staging."),
        HumanMessage(id="later-human", content="What is the status?"),
        AIMessage(id="later-ai", content="The deployment is ready."),
    ]

    assert middleware._complete_turn_ranges(messages) == ((0, 8), (8, 10))

    result = middleware.compact_state(
        {"messages": messages},
        _runtime(),
        force=True,
    )

    assert result is not None
    assert tuple(message.id for message in result.messages_to_summarize) == tuple(message.id for message in messages)
    assert result.preserved_messages == ()
    assert model.call_count == 1
    assert "hidden source-run context" not in model.prompts[0]
    assert "hidden continuation context" not in model.prompts[0]
    assert "Deploying to staging." in model.prompts[0]


def test_unanswered_clarification_remains_uncompacted() -> None:
    model = _model("- [durable] Must not be reached.")
    middleware = _keep_zero_middleware(model)
    messages = _clarification_request_messages("unanswered")

    assert middleware._complete_turn_ranges(messages) == ()
    assert (
        middleware.compact_state(
            {"messages": messages},
            _runtime(),
            force=True,
        )
        is None
    )
    assert model.call_count == 0


def test_answered_clarification_without_final_assistant_remains_uncompacted() -> None:
    model = _model("- [durable] Must not be reached.")
    middleware = _keep_zero_middleware(model)
    messages = [
        *_clarification_request_messages("awaiting-final"),
        _clarification_response("awaiting-final"),
    ]

    assert middleware._complete_turn_ranges(messages) == ()
    assert (
        middleware.compact_state(
            {"messages": messages},
            _runtime(),
            force=True,
        )
        is None
    )
    assert model.call_count == 0


def test_clarification_continuation_does_not_bypass_multi_tool_completion() -> None:
    middleware = _keep_zero_middleware(_model("- [durable] Must not be reached."))
    source_messages = _clarification_request_messages("missing-tool-result")
    messages = [
        source_messages[0],
        source_messages[1],
        source_messages[3],
        _clarification_response("missing-tool-result"),
        AIMessage(id="missing-tool-result-final", content="Must remain uncompacted."),
    ]

    assert middleware._complete_turn_ranges(messages) == ()


def test_mismatched_clarification_response_does_not_close_source_turn() -> None:
    model = _model("- [durable] Must not be reached.")
    middleware = _keep_zero_middleware(model)
    messages = [
        *_clarification_request_messages("mismatched"),
        _clarification_response(
            "mismatched",
            request_id="clarification:different-call",
        ),
        AIMessage(id="mismatched-final", content="Must remain outside the source turn."),
    ]

    assert middleware._complete_turn_ranges(messages) == ()
    assert (
        middleware.compact_state(
            {"messages": messages},
            _runtime(),
            force=True,
        )
        is None
    )
    assert model.call_count == 0


def test_malformed_clarification_artifact_fails_closed() -> None:
    middleware = _keep_zero_middleware(_model("- [durable] Must not be reached."))
    source_messages = _clarification_request_messages("malformed")
    request_message = source_messages[-1]
    assert isinstance(request_message, ToolMessage)
    assert isinstance(request_message.artifact, dict)
    request_message.artifact["human_input"]["version"] = []
    messages = [
        *source_messages,
        _clarification_response("malformed"),
        AIMessage(id="malformed-final", content="Must remain uncompacted."),
    ]

    assert middleware._complete_turn_ranges(messages) == ()


def test_visible_structured_response_does_not_gain_continuation_authority() -> None:
    middleware = _keep_zero_middleware(_model("- [durable] Must not be reached."))
    response = _clarification_response("visible")
    response.additional_kwargs.pop("hide_from_ui")
    messages = [
        *_clarification_request_messages("visible"),
        response,
        AIMessage(id="visible-final", content="Must remain a separate turn."),
    ]

    assert middleware._complete_turn_ranges(messages) == ()


def test_runtime_execution_checkpoint_authors_automatic_receipt() -> None:
    model = _model(
        _dual(
            "The old exchange is archived under the runtime checkpoint identity.",
            "- [durable] Runtime checkpoint identity is authoritative.",
        )
    )
    middleware = _middleware(model)
    archive_context = _archive_context(source_checkpoint_id=None)

    result = middleware.compact_state(
        {
            "messages": [
                HumanMessage(id="old-human", content="old"),
                AIMessage(id="old-ai", content="old answer"),
                HumanMessage(id="current-human", content="current"),
            ]
        },
        _runtime(
            archive_context=archive_context,
            execution_checkpoint_id="runtime-checkpoint",
        ),
        force=True,
    )

    assert result is not None
    assert result.memory_archive_receipt is not None
    assert result.memory_archive_receipt["source_checkpoint_id"] == ("runtime-checkpoint")


def test_runtime_checkpoint_must_match_explicit_manual_source() -> None:
    model = _model(
        _dual(
            "The identity mismatch must abort the archive.",
            "- [durable] Mismatched identity must not be archived.",
        )
    )
    middleware = _middleware(model)

    result = middleware.compact_state(
        {
            "messages": [
                HumanMessage(id="old-human", content="old"),
                AIMessage(id="old-ai", content="old answer"),
                HumanMessage(id="current-human", content="current"),
            ]
        },
        _runtime(
            archive_context=_archive_context(
                source_checkpoint_id="explicit-checkpoint",
            ),
            execution_checkpoint_id="different-runtime-checkpoint",
        ),
        force=True,
    )

    assert result is None
    assert model.call_count == 1


def test_compaction_never_splits_a_dangling_tool_turn() -> None:
    model = _model(_dual("Only the first complete turn is archived.", "- [durable] First turn only."))
    middleware = _middleware(model)
    messages = [
        HumanMessage(id="first-human", content="first"),
        AIMessage(id="first-ai", content="first answer"),
        HumanMessage(id="second-human", content="second"),
        AIMessage(
            id="second-ai",
            content="",
            tool_calls=[
                {
                    "name": "lookup",
                    "args": {},
                    "id": "dangling-call",
                    "type": "tool_call",
                }
            ],
        ),
        HumanMessage(id="third-human", content="current"),
    ]

    result = middleware.compact_state(
        {"messages": messages},
        _runtime(),
        force=True,
    )

    assert result is not None
    assert tuple(message.id for message in result.messages_to_summarize) == (
        "first-human",
        "first-ai",
    )
    assert tuple(message.id for message in result.preserved_messages) == (
        "second-human",
        "second-ai",
        "third-human",
    )


def test_tool_result_without_final_assistant_remains_uncompacted() -> None:
    model = _model(_dual("Only the first complete turn is archived.", "- [durable] First turn only."))
    middleware = _middleware(model)
    messages = [
        HumanMessage(id="first-human", content="first"),
        AIMessage(id="first-ai", content="first answer"),
        HumanMessage(id="second-human", content="second"),
        AIMessage(
            id="second-ai",
            content="",
            tool_calls=[
                {
                    "name": "lookup",
                    "args": {},
                    "id": "second-call",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(
            id="second-tool",
            content="result not yet consumed by the model",
            tool_call_id="second-call",
        ),
    ]

    result = middleware.compact_state(
        {"messages": messages},
        _runtime(),
        force=True,
    )

    assert result is not None
    assert tuple(message.id for message in result.messages_to_summarize) == (
        "first-human",
        "first-ai",
    )
    assert tuple(message.id for message in result.preserved_messages) == (
        "second-human",
        "second-ai",
        "second-tool",
    )


def test_automatic_compaction_preserves_the_only_complete_turn_for_active_follow_up() -> None:
    model = _model(
        _dual(
            "Must not summarize the immediately referenced answer.",
            "- [durable] Must not be reached.",
        )
    )
    middleware = _middleware(model)
    messages = [
        HumanMessage(id="report-request", content="Write the complete report."),
        AIMessage(id="complete-report", content="The complete report body."),
        HumanMessage(
            id="follow-up",
            content="Write your previous complete report to a file verbatim.",
        ),
    ]

    result = middleware.compact_state(
        {"messages": messages},
        _runtime(),
        force=False,
    )

    assert result is None
    assert model.call_count == 0


@pytest.mark.parametrize(
    "keep",
    [
        ("messages", 10),
        ("tokens", 100_000),
    ],
)
def test_automatic_compaction_projects_the_only_complete_turn_when_it_exceeds_the_prompt_budget(
    keep: tuple[str, int],
) -> None:
    model = _model(
        _dual(
            "The oversized report remains available for the active follow-up.",
            "- [durable] The oversized report was completed.",
        )
    )
    middleware = DeerFlowSummarizationMiddleware(
        model=model,
        trigger=("messages", 1),
        keep=keep,
        trim_tokens_to_summarize=3_000,
        summary_prompt=SNIP_ARCHIVE_PROMPT,
    )
    messages = [
        HumanMessage(id="oversized-report-request", content="Write the report."),
        AIMessage(id="oversized-complete-report", content="report-" + "x" * 30_000),
        HumanMessage(id="oversized-follow-up", content="What are its three main findings?"),
    ]

    result = middleware.compact_state(
        {"messages": messages},
        _runtime(),
        force=False,
    )

    assert result is not None
    assert tuple(message.id for message in result.messages_to_summarize) == (
        "oversized-report-request",
        "oversized-complete-report",
    )
    assert tuple(message.id for message in result.preserved_messages) == ("oversized-follow-up",)
    assert 1 < model.call_count <= 8


def test_automatic_compaction_projects_an_oversized_oldest_turn_even_when_keep_exceeds_message_count() -> None:
    model = _model(
        _dual(
            "The oversized oldest report is archived while the recent turn stays verbatim.",
            "- [durable] The oldest oversized report was completed.",
        )
    )
    middleware = DeerFlowSummarizationMiddleware(
        model=model,
        trigger=("messages", 1),
        keep=("messages", 10),
        trim_tokens_to_summarize=3_000,
        summary_prompt=SNIP_ARCHIVE_PROMPT,
    )
    messages = [
        HumanMessage(id="old-report-request", content="Write the old report."),
        AIMessage(id="old-report", content="old-report-" + "x" * 30_000),
        HumanMessage(id="recent-request", content="Give a recent answer."),
        AIMessage(id="recent-answer", content="This recent answer must remain verbatim."),
        HumanMessage(id="open-follow-up", content="Compare the two answers."),
    ]

    result = middleware.compact_state(
        {"messages": messages},
        _runtime(),
        force=False,
    )

    assert result is not None
    assert tuple(message.id for message in result.messages_to_summarize) == (
        "old-report-request",
        "old-report",
    )
    assert tuple(message.id for message in result.preserved_messages) == (
        "recent-request",
        "recent-answer",
        "open-follow-up",
    )
    assert 1 < model.call_count <= 8


def test_automatic_compaction_advances_to_a_later_oversized_turn_when_keep_blocks_normal_candidates() -> None:
    response = _dual(
        "The bounded pass preserves continuity across the small and oversized turns.",
        "- [durable] Both completed turns remain represented after bounded passes.",
    )
    model = _model(*([response] * 16))
    middleware = DeerFlowSummarizationMiddleware(
        model=model,
        trigger=("messages", 1),
        keep=("messages", 10),
        trim_tokens_to_summarize=3_000,
        summary_prompt=SNIP_ARCHIVE_PROMPT,
    )
    messages = [
        HumanMessage(id="small-request", content="Give a short answer."),
        AIMessage(id="small-answer", content="Short answer."),
        HumanMessage(id="later-report-request", content="Write the later report."),
        AIMessage(id="later-oversized-report", content="later-report-" + "x" * 30_000),
        HumanMessage(id="later-open-follow-up", content="What is the key conclusion?"),
    ]

    first = middleware.compact_state(
        {"messages": messages},
        _runtime(),
        force=False,
    )

    assert first is not None
    assert tuple(message.id for message in first.messages_to_summarize) == (
        "small-request",
        "small-answer",
    )
    assert tuple(message.id for message in first.preserved_messages) == (
        "later-report-request",
        "later-oversized-report",
        "later-open-follow-up",
    )

    second = middleware.compact_state(
        {
            "messages": list(first.preserved_messages),
            "summary_text": first.summary_text,
        },
        _runtime(),
        force=False,
    )

    assert second is not None
    assert tuple(message.id for message in second.messages_to_summarize) == (
        "later-report-request",
        "later-oversized-report",
    )
    assert tuple(message.id for message in second.preserved_messages) == ("later-open-follow-up",)
    assert 2 < model.call_count <= 9


def test_automatic_compaction_fails_stably_when_packaged_prompt_cannot_fit() -> None:
    model = _model("- [durable] Must not be reached.")
    middleware = DeerFlowSummarizationMiddleware(
        model=model,
        trigger=("messages", 1),
        keep=("messages", 1),
        trim_tokens_to_summarize=1,
        summary_prompt=SNIP_ARCHIVE_PROMPT,
    )
    messages = [
        HumanMessage(id="old-human", content="old request"),
        AIMessage(id="old-ai", content="old answer"),
        HumanMessage(id="recent-human", content="recent request"),
        AIMessage(id="recent-ai", content="recent answer"),
        HumanMessage(id="open-human", content="current request"),
    ]

    with pytest.raises(summarization_module.SnipPromptBudgetTooSmall):
        middleware.before_model(
            {"messages": messages},
            _runtime(),
        )

    assert model.call_count == 0


def test_automatic_compaction_projects_an_oversized_tool_turn_without_splitting_its_durable_source() -> None:
    model = _model(
        _dual(
            "The first research turn and its tool evidence are archived.",
            "- [durable] The first research result remains available as Memory.",
        )
    )
    budget = 3_000
    middleware = DeerFlowSummarizationMiddleware(
        model=model,
        trigger=("messages", 1),
        keep=("messages", 1),
        trim_tokens_to_summarize=budget,
        summary_prompt=SNIP_ARCHIVE_PROMPT,
    )
    oversized_tool_content = "primary-source-evidence " + "x" * 30_000
    oversized_turn = [
        HumanMessage(id="research-human", content="Research the primary sources."),
        AIMessage(
            id="research-tool-call",
            content="",
            tool_calls=[
                {
                    "name": "web_fetch",
                    "args": {"url": "https://example.test/primary"},
                    "id": "research-call",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(
            id="research-tool-result",
            name="web_fetch",
            content=oversized_tool_content,
            tool_call_id="research-call",
            status="success",
        ),
        AIMessage(id="research-final", content="The verified research answer."),
    ]
    messages = [
        *oversized_turn,
        HumanMessage(id="delivery-human", content="Deliver the existing report."),
        AIMessage(id="delivery-final", content="The report was delivered."),
        HumanMessage(id="open-human", content="What should we do next?"),
    ]

    result = middleware.compact_state(
        {"messages": messages},
        _runtime(archive_context=_archive_context()),
        force=False,
    )

    assert result is not None
    assert tuple(message.id for message in result.messages_to_summarize) == tuple(message.id for message in oversized_turn)
    assert tuple(message.id for message in result.preserved_messages) == (
        "delivery-human",
        "delivery-final",
        "open-human",
    )
    assert model.call_count > 1
    assert model.call_count <= 8
    assert all(middleware.token_counter([HumanMessage(content=prompt)]) <= budget for prompt in model.prompts)
    assert any("research-call" in prompt and "research-tool-result" in prompt and "web_fetch" in prompt and 'status="success"' in prompt and hashlib.sha256(oversized_tool_content.encode()).hexdigest() in prompt for prompt in model.prompts)
    assert result.memory_archive_receipt is not None
    assert result.memory_archive_receipt["source_digest"] == compute_snip_source_digest(
        previous_summary=None,
        source_checkpoint_id="checkpoint-source",
        messages=oversized_turn,
    )


def test_keep_zero_projects_only_the_oldest_turn_when_no_whole_prefix_fits() -> None:
    model = _model(
        _dual(
            "The oldest oversized turn is archived first.",
            "- [durable] Archive barriers make bounded forward progress.",
        )
    )
    middleware = _keep_zero_middleware(
        model,
        trim_tokens_to_summarize=3_000,
    )
    oversized_turn = [
        HumanMessage(id="oversized-human", content="Produce the full report."),
        AIMessage(id="oversized-final", content="report " + "x" * 30_000),
    ]
    later_turn = [
        HumanMessage(id="later-human", content="Deliver the report."),
        AIMessage(id="later-final", content="Delivered."),
    ]

    result = middleware.compact_state(
        {"messages": [*oversized_turn, *later_turn]},
        _runtime(),
        force=True,
    )

    assert result is not None
    assert tuple(message.id for message in result.messages_to_summarize) == (
        "oversized-human",
        "oversized-final",
    )
    assert tuple(message.id for message in result.preserved_messages) == (
        "later-human",
        "later-final",
    )
    assert all(middleware.token_counter([HumanMessage(content=prompt)]) <= 3_000 for prompt in model.prompts)


def test_oversized_terminal_answer_is_hierarchically_summarized_with_bounded_prompts() -> None:
    model = _model(
        _dual(
            "The complete oversized report is archived for continuation.",
            "- [durable] The oversized report was completed.",
        )
    )
    middleware = _keep_zero_middleware(
        model,
        trim_tokens_to_summarize=3_000,
    )
    terminal_answer = "REPORT-BEGIN\n" + "x" * 30_000 + "\nREPORT-END"
    source = [
        HumanMessage(id="report-human", content="Produce the full report."),
        AIMessage(id="report-final", content=terminal_answer),
    ]

    result = middleware.compact_state(
        {"messages": source},
        _runtime(archive_context=_archive_context()),
        force=True,
    )

    assert result is not None
    assert tuple(message.id for message in result.messages_to_summarize) == (
        "report-human",
        "report-final",
    )
    assert result.preserved_messages == ()
    assert 1 < model.call_count <= 8
    assert all(middleware.token_counter([HumanMessage(content=prompt)]) <= 3_000 for prompt in model.prompts)
    assert any("REPORT-BEGIN" in prompt for prompt in model.prompts)
    assert any("REPORT-END" in prompt for prompt in model.prompts)
    assert result.memory_archive_receipt is not None
    assert result.memory_archive_receipt["source_digest"] == compute_snip_source_digest(
        previous_summary=None,
        source_checkpoint_id="checkpoint-source",
        messages=source,
    )


def test_hierarchical_leaf_reserves_budget_for_one_repair_retry() -> None:
    repaired = _dual(
        "The oversized report is retained after one bounded repair.",
        "- [durable] Hierarchical SNIP preserves its repair retry.",
    )
    model = _model("invalid output", *([repaired] * 16))
    budget = 3_000
    middleware = _keep_zero_middleware(
        model,
        trim_tokens_to_summarize=budget,
    )
    source = [
        HumanMessage(id="repair-human", content="Archive the full report."),
        AIMessage(id="repair-final", content="REPORT\n" + "x" * 30_000),
    ]

    result = middleware.compact_state(
        {"messages": source},
        _runtime(),
        force=True,
    )

    assert result is not None
    assert model.call_count > 2
    assert model.prompts[1].endswith(SNIP_RETRY_REINFORCEMENT)
    assert all(middleware.token_counter([HumanMessage(content=prompt)]) <= budget for prompt in model.prompts)


@pytest.mark.asyncio
async def test_async_hierarchical_leaf_reserves_budget_for_one_repair_retry() -> None:
    repaired = _dual(
        "The async oversized report is retained after one bounded repair.",
        "- [durable] Async hierarchical SNIP preserves its repair retry.",
    )
    model = _model("invalid output", *([repaired] * 16))
    budget = 3_000
    middleware = _keep_zero_middleware(
        model,
        trim_tokens_to_summarize=budget,
    )

    result = await middleware.acompact_state(
        {
            "messages": [
                HumanMessage(id="async-repair-human", content="Archive the full report."),
                AIMessage(
                    id="async-repair-final",
                    content="ASYNC-REPORT\n" + "x" * 30_000,
                ),
            ]
        },
        _runtime(),
        force=True,
    )

    assert result is not None
    assert model.call_count > 2
    assert model.prompts[1].endswith(SNIP_RETRY_REINFORCEMENT)
    assert all(middleware.token_counter([HumanMessage(content=prompt)]) <= budget for prompt in model.prompts)


def test_hierarchical_reduction_that_cannot_fit_terminates_with_stable_budget_error() -> None:
    legal_maximum = _dual(
        "c" * 2_000,
        "- [durable] " + "t" * 988,
    )
    model = _model(*([legal_maximum] * 16))
    middleware = _keep_zero_middleware(
        model,
        trim_tokens_to_summarize=1_500,
    )

    with pytest.raises(summarization_module.SnipPromptBudgetTooSmall):
        middleware.compact_state(
            {
                "messages": [
                    HumanMessage(id="reduction-human", content="Archive the report."),
                    AIMessage(id="reduction-final", content="x" * 8_000),
                ]
            },
            _runtime(),
            force=True,
        )

    assert 1 <= model.call_count <= summarization_module.MAX_SNIP_HIERARCHICAL_MODEL_CALLS


def test_hierarchical_structure_that_cannot_fit_is_typed_source_too_large() -> None:
    model = _model(_dual("unused", SNIP_NOTHING))
    middleware = _keep_zero_middleware(
        model,
        trim_tokens_to_summarize=3_000,
    )

    with pytest.raises(summarization_module.SnipSourceTooLarge):
        middleware.compact_state(
            {"messages": _many_tool_call_turn()},
            _runtime(),
            force=True,
        )

    assert model.call_count == 0


@pytest.mark.asyncio
async def test_async_hierarchical_structure_that_cannot_fit_is_typed_source_too_large() -> None:
    model = _model(_dual("unused", SNIP_NOTHING))
    middleware = _keep_zero_middleware(
        model,
        trim_tokens_to_summarize=3_000,
    )

    with pytest.raises(summarization_module.SnipSourceTooLarge):
        await middleware.acompact_state(
            {"messages": _many_tool_call_turn()},
            _runtime(),
            force=True,
        )

    assert model.call_count == 0


def test_unprojectable_oversized_turn_is_typed_source_too_large() -> None:
    model = _model(_dual("unused", SNIP_NOTHING))
    middleware = _keep_zero_middleware(
        model,
        trim_tokens_to_summarize=3_000,
    )
    call_id = "unprojectable-call"
    messages = [
        HumanMessage(id="unprojectable-human", content="Archive this tool turn."),
        AIMessage(
            id="unprojectable-tool-call",
            content="analysis-" + "x" * 20_000,
            tool_calls=[
                {
                    "name": "lookup",
                    "args": {"value": object()},
                    "id": call_id,
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(
            id="unprojectable-tool-result",
            name="lookup",
            content="result",
            tool_call_id=call_id,
        ),
        AIMessage(id="unprojectable-final", content="Final answer."),
    ]

    with pytest.raises(summarization_module.SnipSourceTooLarge):
        middleware.compact_state(
            {"messages": messages},
            _runtime(),
            force=True,
        )

    assert model.call_count == 0


def test_invalid_direct_snip_without_repair_headroom_is_typed_budget_error() -> None:
    model = _model("invalid SNIP output")
    middleware = _keep_zero_middleware(model)
    source = [
        HumanMessage(id="direct-human", content="Archive this answer."),
        AIMessage(id="direct-ai", content="answer-" + "x" * 400),
    ]
    prompt = middleware._build_summary_prompt(source)
    assert prompt is not None
    exact_budget = middleware.token_counter([HumanMessage(content=prompt)])
    middleware.trim_tokens_to_summarize = exact_budget
    assert middleware._prompt_within_budget(prompt)
    assert not middleware._prompt_with_repair_within_budget(prompt)

    with pytest.raises(summarization_module.SnipPromptBudgetTooSmall):
        middleware.compact_state(
            {"messages": source},
            _runtime(),
            force=True,
        )

    assert model.call_count == 1


@pytest.mark.asyncio
async def test_async_invalid_direct_snip_without_repair_headroom_is_typed_budget_error() -> None:
    model = _model("invalid SNIP output")
    middleware = _keep_zero_middleware(model)
    source = [
        HumanMessage(id="async-direct-human", content="Archive this answer."),
        AIMessage(id="async-direct-ai", content="answer-" + "x" * 400),
    ]
    prompt = middleware._build_summary_prompt(source)
    assert prompt is not None
    exact_budget = middleware.token_counter([HumanMessage(content=prompt)])
    middleware.trim_tokens_to_summarize = exact_budget
    assert middleware._prompt_within_budget(prompt)
    assert not middleware._prompt_with_repair_within_budget(prompt)

    with pytest.raises(summarization_module.SnipPromptBudgetTooSmall):
        await middleware.acompact_state(
            {"messages": source},
            _runtime(),
            force=True,
        )

    assert model.call_count == 1


def test_hierarchical_call_budget_exhaustion_is_typed_source_too_large() -> None:
    model = _model(_dual("Must not be reached.", SNIP_NOTHING))
    middleware = _keep_zero_middleware(model)

    with pytest.raises(summarization_module.SnipSourceTooLarge):
        middleware._invoke_snip_prompt(
            "small prompt",
            call_budget=[summarization_module.MAX_SNIP_HIERARCHICAL_MODEL_CALLS],
        )

    assert model.call_count == 0


@pytest.mark.asyncio
async def test_async_hierarchical_call_budget_exhaustion_is_typed_source_too_large() -> None:
    model = _model(_dual("Must not be reached.", SNIP_NOTHING))
    middleware = _keep_zero_middleware(model)

    with pytest.raises(summarization_module.SnipSourceTooLarge):
        await middleware._ainvoke_snip_prompt(
            "small prompt",
            authorization_context=None,
            call_budget=[summarization_module.MAX_SNIP_HIERARCHICAL_MODEL_CALLS],
        )

    assert model.call_count == 0


def test_prompt_budget_falls_back_to_an_earlier_whole_turn() -> None:
    model = _model(_dual("The prompt budget only admits the first turn.", "- [durable] Only the first complete turn fits."))
    middleware = _middleware(model)
    first_turn = [
        HumanMessage(id="first-human", content="first-user-unique"),
        AIMessage(id="first-ai", content="first-answer-unique"),
    ]
    second_turn = [
        HumanMessage(id="second-human", content="second-user-unique"),
        AIMessage(
            id="second-ai",
            content="second-answer-unique " + "x" * 400,
        ),
    ]
    first_prompt = middleware._build_summary_prompt(first_turn)
    assert first_prompt is not None
    middleware.trim_tokens_to_summarize = middleware.token_counter([HumanMessage(content=first_prompt)])

    result = middleware.compact_state(
        {
            "messages": [
                *first_turn,
                *second_turn,
                HumanMessage(id="current-human", content="current"),
            ]
        },
        _runtime(),
        force=True,
    )

    assert result is not None
    assert tuple(message.id for message in result.messages_to_summarize) == (
        "first-human",
        "first-ai",
    )
    assert "first-user-unique" in model.prompts[0]
    assert "first-answer-unique" in model.prompts[0]
    assert "second-user-unique" not in model.prompts[0]


def test_keep_zero_archives_every_complete_turn_but_preserves_open_tail() -> None:
    model = _model(_dual("Both complete turns are archived; one question is still open.", "- [durable] All complete turns are cumulative."))
    middleware = DeerFlowSummarizationMiddleware(
        model=model,
        trigger=("messages", 1),
        keep=("messages", 1),
        trim_tokens_to_summarize=20_000,
        summary_prompt=SNIP_ARCHIVE_PROMPT,
        compact_all_complete_turns=True,
    )
    messages = [
        HumanMessage(id="first-human", content="first"),
        AIMessage(id="first-ai", content="first answer"),
        HumanMessage(id="second-human", content="second"),
        AIMessage(id="second-ai", content="second answer"),
        HumanMessage(id="open-human", content="not answered"),
    ]

    result = middleware.compact_state(
        {"messages": messages},
        _runtime(),
        force=True,
    )

    assert result is not None
    assert tuple(message.id for message in result.messages_to_summarize) == (
        "first-human",
        "first-ai",
        "second-human",
        "second-ai",
    )
    assert tuple(message.id for message in result.preserved_messages) == ("open-human",)


@pytest.mark.asyncio
async def test_prepare_keep_zero_reports_invalid_snip_as_compaction_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _model("not a valid tagged SNIP document")
    middleware = _keep_zero_middleware(model)
    monkeypatch.setattr(
        context_compaction_module,
        "_create_compaction_middleware",
        lambda **_kwargs: middleware,
    )

    prepared = await prepare_thread_compaction(
        _SnapshotAccessor(
            [
                HumanMessage(id="complete-human", content="complete"),
                AIMessage(id="complete-ai", content="complete answer"),
            ]
        ),
        "thread-1",
        keep=("messages", 0),
        app_config=object(),  # type: ignore[arg-type]
    )

    assert prepared.result.compacted is False
    assert prepared.result.reason == "compaction_failed"
    # Bounded repair: the invalid output is retried exactly once.
    assert model.call_count == 2


@pytest.mark.asyncio
async def test_prepare_keep_zero_reports_prompt_budget_too_small(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _model("- [durable] Must not be reached.")
    middleware = _keep_zero_middleware(
        model,
        trim_tokens_to_summarize=1,
    )
    monkeypatch.setattr(
        context_compaction_module,
        "_create_compaction_middleware",
        lambda **_kwargs: middleware,
    )

    prepared = await prepare_thread_compaction(
        _SnapshotAccessor(
            [
                HumanMessage(id="complete-human", content="complete"),
                AIMessage(id="complete-ai", content="complete answer"),
            ]
        ),
        "thread-1",
        keep=("messages", 0),
        app_config=object(),  # type: ignore[arg-type]
    )

    assert prepared.result.compacted is False
    assert prepared.result.reason == "prompt_budget_too_small"
    assert model.call_count == 0


@pytest.mark.asyncio
async def test_prepare_keep_zero_reports_hierarchical_source_too_large(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _model("- [durable] Must not be reached.")
    middleware = _keep_zero_middleware(
        model,
        trim_tokens_to_summarize=3_000,
    )
    monkeypatch.setattr(
        context_compaction_module,
        "_create_compaction_middleware",
        lambda **_kwargs: middleware,
    )

    prepared = await prepare_thread_compaction(
        _SnapshotAccessor(
            [
                HumanMessage(id="huge-human", content="Archive this full answer."),
                AIMessage(id="huge-ai", content="x" * 100_000),
            ]
        ),
        "thread-1",
        keep=("messages", 0),
        app_config=object(),  # type: ignore[arg-type]
    )

    assert prepared.result.compacted is False
    assert prepared.result.reason == "source_too_large"
    assert model.call_count == 0


@pytest.mark.asyncio
async def test_prepare_keep_zero_reports_oversized_structure_as_source_too_large(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _model(_dual("unused", SNIP_NOTHING))
    middleware = _keep_zero_middleware(
        model,
        trim_tokens_to_summarize=3_000,
    )
    monkeypatch.setattr(
        context_compaction_module,
        "_create_compaction_middleware",
        lambda **_kwargs: middleware,
    )

    prepared = await prepare_thread_compaction(
        _SnapshotAccessor(_many_tool_call_turn()),
        "thread-1",
        keep=("messages", 0),
        app_config=object(),  # type: ignore[arg-type]
    )

    assert prepared.result.compacted is False
    assert prepared.result.reason == "source_too_large"
    assert model.call_count == 0


@pytest.mark.asyncio
async def test_prepare_force_projects_a_lone_oversized_turn_despite_policy_keep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _dual(
        "The explicit API compaction archived the lone oversized completed turn.",
        "- [durable] The lone oversized turn was compacted through the API path.",
    )
    model = _model(*([response] * 8))
    middleware = DeerFlowSummarizationMiddleware(
        model=model,
        trigger=("tokens", 32_000),
        keep=("messages", 10),
        trim_tokens_to_summarize=3_000,
        summary_prompt=SNIP_ARCHIVE_PROMPT,
    )
    monkeypatch.setattr(
        context_compaction_module,
        "_create_compaction_middleware",
        lambda **_kwargs: middleware,
    )

    prepared = await prepare_thread_compaction(
        _SnapshotAccessor(
            [
                HumanMessage(id="api-oversized-human", content="Write the report."),
                AIMessage(id="api-oversized-ai", content="report-" + "x" * 30_000),
            ]
        ),
        "thread-1",
        keep=None,
        force=True,
        app_config=object(),  # type: ignore[arg-type]
    )

    assert prepared.result.compacted is True
    assert prepared.result.removed_message_count == 2
    assert prepared.result.preserved_message_count == 0
    assert 1 < model.call_count <= 8


@pytest.mark.asyncio
async def test_prepare_force_reports_invalid_model_output_as_compaction_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _model("invalid first output", "invalid repair output")
    middleware = _middleware(model)
    monkeypatch.setattr(
        context_compaction_module,
        "_create_compaction_middleware",
        lambda **_kwargs: middleware,
    )

    prepared = await prepare_thread_compaction(
        _SnapshotAccessor(
            [
                HumanMessage(id="complete-human", content="complete"),
                AIMessage(id="complete-ai", content="complete answer"),
            ]
        ),
        "thread-1",
        keep=None,
        force=True,
        app_config=object(),  # type: ignore[arg-type]
    )

    assert prepared.result.compacted is False
    assert prepared.result.reason == "compaction_failed"
    assert model.call_count == 2


@pytest.mark.asyncio
async def test_prepare_keep_zero_reports_model_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _model()
    middleware = _keep_zero_middleware(model)
    monkeypatch.setattr(
        context_compaction_module,
        "_create_compaction_middleware",
        lambda **_kwargs: middleware,
    )

    prepared = await prepare_thread_compaction(
        _SnapshotAccessor(
            [
                HumanMessage(id="complete-human", content="complete"),
                AIMessage(id="complete-ai", content="complete answer"),
            ]
        ),
        "thread-1",
        keep=("messages", 0),
        app_config=object(),  # type: ignore[arg-type]
    )

    assert prepared.result.compacted is False
    assert prepared.result.reason == "compaction_failed"
    assert model.call_count == 1


@pytest.mark.asyncio
async def test_prepare_keep_zero_uses_not_enough_only_without_complete_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _model("- [durable] Must not be reached.")
    middleware = _keep_zero_middleware(model)
    monkeypatch.setattr(
        context_compaction_module,
        "_create_compaction_middleware",
        lambda **_kwargs: middleware,
    )

    prepared = await prepare_thread_compaction(
        _SnapshotAccessor(
            [
                HumanMessage(id="open-human", content="still open"),
            ]
        ),
        "thread-1",
        keep=("messages", 0),
        app_config=object(),  # type: ignore[arg-type]
    )

    assert prepared.result.compacted is False
    assert prepared.result.reason == "not_enough_messages"
    assert model.call_count == 0


def test_invalid_snip_output_repairs_once_with_reinforced_prompt() -> None:
    continuity = "The repaired continuity summary is retained."
    tagged = "- [durable] The repaired result is retained."
    model = _model("Preamble\n- [durable] Invalid.", _dual(continuity, tagged), "must-not-be-called")
    middleware = _middleware(model)

    result = middleware.compact_state(
        {
            "messages": [
                HumanMessage(id="old-human", content="old"),
                AIMessage(id="old-ai", content="old answer"),
                HumanMessage(id="current-human", content="current"),
            ],
            "summary_text": "- [permanent] Keep me.",
        },
        _runtime(archive_context=_archive_context()),
        force=True,
    )

    assert result is not None
    assert result.summary_text == continuity
    assert result.memory_archive_receipt is not None
    assert result.memory_archive_receipt["tagged_text"] == tagged
    assert model.call_count == 2
    assert SNIP_RETRY_REINFORCEMENT not in model.prompts[0]
    assert model.prompts[1].endswith(SNIP_RETRY_REINFORCEMENT)
    # The retry reuses the same input and never echoes the invalid output.
    assert model.prompts[1].startswith(model.prompts[0])
    assert "Preamble" not in model.prompts[1]


def test_invalid_tagged_segment_with_valid_continuity_repairs_once() -> None:
    continuity = "The tagged segment was repaired on the second attempt."
    tagged = "- [durable] The repaired tagged segment is retained."
    model = _model(
        _dual("A valid continuity summary.", "prose instead of tagged lines"),
        _dual(continuity, tagged),
        "must-not-be-called",
    )
    middleware = _middleware(model)

    result = middleware.compact_state(
        {
            "messages": [
                HumanMessage(id="old-human", content="old"),
                AIMessage(id="old-ai", content="old answer"),
                HumanMessage(id="current-human", content="current"),
            ],
        },
        _runtime(archive_context=_archive_context()),
        force=True,
    )

    assert result is not None
    assert result.summary_text == continuity
    assert result.memory_archive_receipt is not None
    assert result.memory_archive_receipt["tagged_text"] == tagged
    assert model.call_count == 2
    assert model.prompts[1].endswith(SNIP_RETRY_REINFORCEMENT)


def test_custom_summary_prompt_keeps_single_segment_semantics() -> None:
    output = "- [durable] Legacy single-segment output stays authoritative."
    model = _model(output, "must-not-be-called")
    middleware = DeerFlowSummarizationMiddleware(
        model=model,
        trigger=("messages", 1),
        keep=("messages", 1),
        trim_tokens_to_summarize=20_000,
        summary_prompt="Summarize the conversation.\n\n{messages}",
    )

    result = middleware.compact_state(
        {
            "messages": [
                HumanMessage(id="old-human", content="old"),
                AIMessage(id="old-ai", content="old answer"),
                HumanMessage(id="current-human", content="current"),
            ],
        },
        _runtime(archive_context=_archive_context()),
        force=True,
    )

    assert result is not None
    assert model.call_count == 1
    assert result.summary_text == output
    assert result.memory_archive_receipt is not None
    assert result.memory_archive_receipt["tagged_text"] == output


def test_oversized_custom_summary_prompt_fails_with_typed_source_error_without_projection() -> None:
    model = _model("- [durable] Must not be reached.")
    middleware = DeerFlowSummarizationMiddleware(
        model=model,
        trigger=("messages", 1),
        keep=("messages", 10),
        trim_tokens_to_summarize=1_000,
        summary_prompt="Custom summary contract.\n\n{messages}",
    )

    with pytest.raises(summarization_module.SnipSourceTooLarge):
        middleware.compact_state(
            {
                "messages": [
                    HumanMessage(id="custom-human", content="custom request"),
                    AIMessage(id="custom-ai", content="x" * 20_000),
                ]
            },
            _runtime(),
            force=True,
        )

    assert model.call_count == 0


def test_forced_compaction_projects_a_lone_oversized_turn_despite_keep() -> None:
    response = _dual(
        "The explicit compaction archived the lone oversized completed turn.",
        "- [durable] The lone oversized turn was explicitly compacted.",
    )
    model = _model(*([response] * 8))
    middleware = DeerFlowSummarizationMiddleware(
        model=model,
        trigger=("tokens", 32_000),
        keep=("messages", 10),
        trim_tokens_to_summarize=3_000,
        summary_prompt=SNIP_ARCHIVE_PROMPT,
    )
    messages = [
        HumanMessage(id="forced-oversized-human", content="Write the report."),
        AIMessage(id="forced-oversized-ai", content="report-" + "x" * 30_000),
    ]

    result = middleware.compact_state(
        {"messages": messages},
        _runtime(),
        force=True,
    )

    assert result is not None
    assert tuple(message.id for message in result.messages_to_summarize) == (
        "forced-oversized-human",
        "forced-oversized-ai",
    )
    assert result.preserved_messages == ()
    assert 1 < model.call_count <= 8


def test_automatic_oversized_custom_prompt_bypasses_keep_with_typed_source_error() -> None:
    model = _model("- [durable] Must not be reached.")
    middleware = DeerFlowSummarizationMiddleware(
        model=model,
        trigger=("messages", 1),
        keep=("messages", 10),
        trim_tokens_to_summarize=1_000,
        summary_prompt="Custom summary contract.\n\n{messages}",
    )

    with pytest.raises(summarization_module.SnipSourceTooLarge):
        middleware.compact_state(
            {
                "messages": [
                    HumanMessage(id="automatic-custom-human", content="custom request"),
                    AIMessage(id="automatic-custom-ai", content="x" * 20_000),
                    HumanMessage(id="automatic-custom-follow-up", content="follow up"),
                ]
            },
            _runtime(),
            force=False,
        )

    assert model.call_count == 0


def test_configured_custom_prompt_reaches_the_production_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom_prompt = "Summarize this deployment conversation.\n\n{messages}"
    config = SimpleNamespace(
        enabled=True,
        model_name=None,
        trigger=None,
        keep=SimpleNamespace(to_tuple=lambda: ("messages", 1)),
        trim_tokens_to_summarize=20_000,
        summary_prompt=custom_prompt,
    )
    app_config = SimpleNamespace(summarization=config)

    build_calls: list[dict[str, Any]] = []

    def build_model(_self: object, **kwargs: Any) -> _RecordingModel:
        build_calls.append(kwargs)
        return _model("- [durable] Custom prompt output.")

    monkeypatch.setattr(
        summarization_module.ModelRuntime,
        "build_chat_model",
        build_model,
    )

    middleware = summarization_module.create_summarization_middleware(
        app_config=app_config,
    )

    assert middleware is not None
    assert build_calls[0]["profile"] is ModelRuntimeProfile.AGENT_GRAPH
    assert middleware.summary_prompt == custom_prompt
    assert middleware._dual_output_contract is False


def test_explicit_custom_prompt_equal_to_packaged_text_stays_single_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prompt provenance, not accidental string equality, selects the contract."""
    config = SimpleNamespace(
        enabled=True,
        model_name=None,
        trigger=None,
        keep=SimpleNamespace(to_tuple=lambda: ("messages", 1)),
        trim_tokens_to_summarize=20_000,
        summary_prompt=SNIP_ARCHIVE_PROMPT,
    )
    app_config = SimpleNamespace(summarization=config)
    monkeypatch.setattr(
        summarization_module.ModelRuntime,
        "build_chat_model",
        lambda _self, **_kwargs: _model("- [durable] Explicit custom output."),
    )
    monkeypatch.setattr(
        summarization_module,
        "_ensure_snip_summary_output_budget",
        lambda _model: (_ for _ in ()).throw(
            AssertionError("custom prompts must not receive the packaged SNIP budget"),
        ),
    )

    middleware = summarization_module.create_summarization_middleware(
        app_config=app_config,
    )

    assert middleware is not None
    assert middleware.summary_prompt == SNIP_ARCHIVE_PROMPT
    assert middleware._dual_output_contract is False


def test_packaged_prompt_budget_reaches_the_production_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = SimpleNamespace(
        enabled=True,
        model_name=None,
        trigger=None,
        keep=SimpleNamespace(to_tuple=lambda: ("messages", 1)),
        trim_tokens_to_summarize=20_000,
        summary_prompt=None,
    )
    app_config = SimpleNamespace(summarization=config)
    model = _model(_dual("Continue the task.", "(nothing)"))
    observed: list[_RecordingModel] = []
    monkeypatch.setattr(
        summarization_module.ModelRuntime,
        "build_chat_model",
        lambda _self, **_kwargs: model,
    )

    def record_budget(candidate: _RecordingModel) -> _RecordingModel:
        observed.append(candidate)
        return candidate

    monkeypatch.setattr(
        summarization_module,
        "_ensure_snip_summary_output_budget",
        record_budget,
    )

    middleware = summarization_module.create_summarization_middleware(
        app_config=app_config,
    )

    assert middleware is not None
    assert observed == [model]
    assert middleware.summary_prompt == SNIP_ARCHIVE_PROMPT
    assert middleware._dual_output_contract is True


def test_snip_summary_model_output_budget_is_raised_for_dual_output() -> None:
    model = _LowBudgetModel(
        responses=["unused"],
        custom_get_token_ids=lambda text: list(range(len(text))),
    )

    raised = summarization_module._ensure_snip_summary_output_budget(model)

    assert raised.max_tokens == summarization_module.MIN_SNIP_SUMMARY_OUTPUT_TOKENS
    assert raised.max_tokens > model.max_tokens


def test_twice_invalid_snip_output_preserves_state_after_two_calls(
    caplog: pytest.LogCaptureFixture,
) -> None:
    model = _model(
        "Preamble\n- [durable] Invalid.",
        "Still invalid output",
        "must-not-be-called",
    )
    middleware = _middleware(model)

    result = middleware.compact_state(
        {
            "messages": [
                HumanMessage(id="old-human", content="old"),
                AIMessage(id="old-ai", content="old answer"),
                HumanMessage(id="current-human", content="current"),
            ],
            "summary_text": "- [permanent] Keep me.",
        },
        _runtime(archive_context=_archive_context()),
        force=True,
    )

    assert result is None
    assert model.call_count == 2
    validation_logs = [record.getMessage() for record in caplog.records if "SNIP model output failed validation" in record.getMessage()]
    assert validation_logs == [
        "SNIP model output failed validation on attempt 1/2: SNIP output does not start with a continuity segment",
        "SNIP model output failed validation on attempt 2/2: SNIP output does not start with a continuity segment",
    ]
    assert "Preamble" not in "\n".join(validation_logs)
    assert "Still invalid output" not in "\n".join(validation_logs)


def test_nothing_still_updates_continuity_summary_but_clears_receipt() -> None:
    continuity = "Greetings exchanged; nothing durable happened yet."
    model = _model(_dual(continuity, SNIP_NOTHING))
    middleware = _middleware(model)

    result = middleware.compact_state(
        {
            "messages": [
                HumanMessage(id="old-human", content="hello"),
                AIMessage(id="old-ai", content="hello"),
                HumanMessage(id="current-human", content="current"),
            ],
            "memory_archive_receipt": {"stale": True},
        },
        _runtime(archive_context=_archive_context()),
        force=True,
    )

    assert result is not None
    assert result.summary_text == continuity
    assert result.memory_archive_receipt is None


def test_memory_disabled_still_compacts_without_receipt() -> None:
    model = _model(_dual("The thread summary survives without memory.", "- [durable] Thread summary remains available."))
    middleware = _middleware(model)
    disabled = SnipArchiveContext(
        enabled=False,
        project_id=uuid.UUID("10000000-0000-4000-8000-000000000001"),
        owner_user_id="20000000-0000-4000-8000-000000000002",
        namespace="default",
        preference_version=7,
        summary_model=None,
        source_checkpoint_id="checkpoint-source",
    )

    result = middleware.compact_state(
        {
            "messages": [
                HumanMessage(id="old-human", content="old"),
                AIMessage(id="old-ai", content="old answer"),
                HumanMessage(id="current-human", content="current"),
            ]
        },
        _runtime(archive_context=disabled),
        force=True,
    )

    assert result is not None
    assert result.memory_archive_receipt is None
