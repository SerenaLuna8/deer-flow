from __future__ import annotations

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
from deerflow.runtime import context_compaction as context_compaction_module
from deerflow.runtime.context_compaction import prepare_thread_compaction


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
        summary_model_ref=uuid.UUID("30000000-0000-4000-8000-000000000003"),
        source_checkpoint_id=source_checkpoint_id,
    )


def _runtime(
    *,
    archive_context: SnipArchiveContext | None = None,
    execution_checkpoint_id: str | None = None,
) -> SimpleNamespace:
    context: dict[str, object] = {"thread_id": "thread-1"}
    if archive_context is not None:
        context[MEMORY_ARCHIVE_CONTEXT_KEY] = archive_context
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
async def test_prepare_keep_zero_reports_prompt_budget_failure(
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
    assert prepared.result.reason == "compaction_failed"
    assert model.call_count == 0


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
    monkeypatch.setattr(
        summarization_module,
        "create_chat_model",
        lambda **_kwargs: _model("- [durable] Custom prompt output."),
    )

    middleware = summarization_module.create_summarization_middleware(
        app_config=app_config,
    )

    assert middleware is not None
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
        summarization_module,
        "create_chat_model",
        lambda **_kwargs: _model("- [durable] Explicit custom output."),
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
        summarization_module,
        "create_chat_model",
        lambda **_kwargs: model,
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


def test_twice_invalid_snip_output_preserves_state_after_two_calls() -> None:
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
        summary_model_ref=None,
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
