from __future__ import annotations

from collections import deque
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langsmith.run_helpers import get_tracing_context, tracing_context

from deerflow.agents.memory.dream import (
    DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
    DREAM_PROMPT,
    DREAM_PROMPT_VERSION,
    EMPTY_MEMORY_DOCUMENT,
    MEMORY_DOCUMENT_SECTIONS,
    DreamHistoryInput,
    MemoryDocumentInvalid,
    MemoryDocumentOverBudget,
    MemoryDreamError,
    MemoryDreamInput,
    MemoryDreamResult,
    MemoryDreamRunner,
    _MemoryDraft,
    build_dream_tools,
    estimate_memory_tokens,
    render_dream_input,
    render_dream_prompt,
    render_empty_memory_document,
    validate_memory_document,
)
from deerflow.models.runtime import ModelRuntime


def _document(*lines: str) -> str:
    return "\n\n".join(
        (
            MEMORY_DOCUMENT_SECTIONS[0],
            "- User prefers concise Chinese.",
            MEMORY_DOCUMENT_SECTIONS[1],
            *lines,
            MEMORY_DOCUMENT_SECTIONS[2],
            "- PostgreSQL is the only application database.",
            MEMORY_DOCUMENT_SECTIONS[3],
            "- Complete the Memory refactor.",
        )
    )


def test_dream_prompt_and_document_sections_are_fixed() -> None:
    assert DREAM_PROMPT_VERSION == "dream-prompt-v4"
    assert "Use only read_memory_document and replace_memory_document." in DREAM_PROMPT
    assert "You must not create or update an account-global profile" in DREAM_PROMPT
    assert "transfer memory from another project or namespace." in DREAM_PROMPT
    assert "Treat <target-token-limit> as the required writing budget." in DREAM_PROMPT
    assert "The complete document must not exceed <target-character-limit>." in DREAM_PROMPT
    assert "Never resubmit an unchanged rejected draft." in DREAM_PROMPT
    assert "History entries marked origin=tool are model-proposed hints." in DREAM_PROMPT
    assert "remain searchable as archived episodes" in DREAM_PROMPT
    assert "this is a budget\nrewrite session" in DREAM_PROMPT
    assert tuple(line for line in DREAM_PROMPT.splitlines() if line.startswith("# ")) == MEMORY_DOCUMENT_SECTIONS
    assert EMPTY_MEMORY_DOCUMENT == "\n\n".join(MEMORY_DOCUMENT_SECTIONS)


def test_memory_document_contract_renders_and_validates_frozen_custom_sections() -> None:
    sections = ("协作偏好", "交付边界", "当前目标")
    content = render_empty_memory_document(sections)

    assert content == "# 协作偏好\n\n# 交付边界\n\n# 当前目标"
    assert validate_memory_document(content, 8_000, sections=sections) == content
    with pytest.raises(MemoryDocumentInvalid, match="sections are invalid"):
        validate_memory_document(content, 8_000)

    value = MemoryDreamInput(
        document=content,
        document_version=0,
        history=(DreamHistoryInput(sequence=1, tagged_text="- [durable] 保持简洁。"),),
        max_tokens=8_000,
        sections=sections,
    )
    rendered = render_dream_input(value)
    assert "<document-sections>\n# 协作偏好\n# 交付边界\n# 当前目标\n</document-sections>" in rendered
    prompt = render_dream_prompt(sections)
    assert tuple(line for line in prompt.splitlines() if line.startswith("# ")) == (
        "# 协作偏好",
        "# 交付边界",
        "# 当前目标",
    )
    assert "{{MEMORY_DOCUMENT_SECTIONS}}" not in prompt
    assert render_empty_memory_document(DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES) == EMPTY_MEMORY_DOCUMENT


@pytest.mark.parametrize(
    "sections",
    [
        ("只有一个",),
        ("重复", "重复"),
        ("# 非纯标题", "合法"),
        ("包含\n换行", "合法"),
        ("包含\u2028行分隔符", "合法"),
        ("包含\u2029段落分隔符", "合法"),
        ("[durable] 非法标记", "合法"),
        ("含 [H:12] 历史编号", "合法"),
    ],
)
def test_memory_document_sections_fail_closed(sections: tuple[str, ...]) -> None:
    with pytest.raises((MemoryDocumentInvalid, ValueError)):
        render_empty_memory_document(sections)


def test_memory_token_estimate_is_deterministic_and_cjk_conservative() -> None:
    assert estimate_memory_tokens("abcd中文") == 3
    assert estimate_memory_tokens("abcd中文") == estimate_memory_tokens("abcd中文")


@pytest.mark.parametrize(
    "content",
    [
        "",
        "\n\n".join(reversed(MEMORY_DOCUMENT_SECTIONS)),
        f"{EMPTY_MEMORY_DOCUMENT}\n\n# Extra",
        f"{EMPTY_MEMORY_DOCUMENT}\n\n- [durable] leaked tag",
        f"{EMPTY_MEMORY_DOCUMENT}\n\n[H:12] leaked identity",
        "x" * 16_001,
    ],
)
def test_memory_document_validation_fails_closed_without_truncation(
    content: str,
) -> None:
    with pytest.raises(MemoryDocumentInvalid):
        validate_memory_document(content, 8_000)


def test_memory_document_validation_enforces_token_budget() -> None:
    content = _document("- " + ("x" * 800))
    with pytest.raises(MemoryDocumentOverBudget, match="token budget") as raised:
        validate_memory_document(content, 100)
    assert isinstance(raised.value, MemoryDocumentInvalid)
    assert raised.value.estimated_tokens == estimate_memory_tokens(content)
    assert raised.value.limit_tokens == 100
    assert raised.value.target_tokens == 90
    assert raised.value.actual_characters == len(content)
    assert raised.value.target_characters == 90
    assert raised.value.overage_tokens == estimate_memory_tokens(content) - 100
    assert raised.value.reduction_tokens == estimate_memory_tokens(content) - 90
    assert raised.value.reduction_characters == len(content) - 90
    assert validate_memory_document(content, 8_000) == content


def test_dream_input_keeps_all_twenty_history_items_without_truncation() -> None:
    history = tuple(
        DreamHistoryInput(
            sequence=index,
            tagged_text=f"- [durable] history-{index}-" + ("x" * 950),
        )
        for index in range(1, 21)
    )
    rendered = render_dream_input(
        MemoryDreamInput(
            document=EMPTY_MEMORY_DOCUMENT,
            document_version=0,
            history=history,
            max_tokens=8_000,
        )
    )

    for item in history:
        assert f"[H:{item.sequence}]\n{item.tagged_text}" in rendered
    assert rendered.count("[H:") == 20
    assert "<target-token-limit>7200</target-token-limit>" in rendered
    assert "<target-character-limit>7200</target-character-limit>" in rendered
    assert rendered.index("<target-token-limit>") < rendered.index("<current-memory>")
    assert rendered.index("<target-character-limit>") < rendered.index("<current-memory>")


def test_dream_input_accepts_a_structurally_valid_document_above_the_new_budget() -> None:
    previous_document = _document("- " + ("x" * 800))
    assert estimate_memory_tokens(previous_document) > 100

    value = MemoryDreamInput(
        document=previous_document,
        document_version=4,
        history=(
            DreamHistoryInput(
                sequence=8,
                tagged_text="- [correction] The document must shrink.",
            ),
        ),
        max_tokens=100,
    )

    assert value.document == previous_document
    assert "<token-limit>100</token-limit>" in render_dream_input(value)
    assert "<target-token-limit>90</target-token-limit>" in render_dream_input(value)
    assert "<target-character-limit>90</target-character-limit>" in render_dream_input(value)

    capped_target = render_dream_input(
        MemoryDreamInput(
            document=EMPTY_MEMORY_DOCUMENT,
            document_version=0,
            history=value.history,
            max_tokens=20_000,
        )
    )
    assert "<target-token-limit>18000</target-token-limit>" in capped_target
    assert "<target-character-limit>16000</target-character-limit>" in capped_target


@pytest.mark.asyncio
async def test_dream_tools_are_only_scoped_read_and_in_memory_replace() -> None:
    replacement = _document("- ActWeave is the project.")
    draft = _MemoryDraft(
        original=EMPTY_MEMORY_DOCUMENT,
        content=EMPTY_MEMORY_DOCUMENT,
        max_tokens=8_000,
    )
    tools = build_dream_tools(draft)

    assert [tool.name for tool in tools] == [
        "read_memory_document",
        "replace_memory_document",
    ]
    assert "at most 7200 characters" in tools[1].description
    assert await tools[0].ainvoke({}) == EMPTY_MEMORY_DOCUMENT
    assert await tools[1].ainvoke({"content": replacement}) == "memory draft replaced"
    assert draft.content == replacement


@pytest.mark.asyncio
async def test_dream_read_tool_returns_the_document_only_once() -> None:
    draft = _MemoryDraft(
        original=EMPTY_MEMORY_DOCUMENT,
        content=EMPTY_MEMORY_DOCUMENT,
        max_tokens=8_000,
    )
    read_tool, _replace_tool = build_dream_tools(draft)

    assert await read_tool.ainvoke({}) == EMPTY_MEMORY_DOCUMENT
    assert await read_tool.ainvoke({}) == ("memory document already read; do not call read_memory_document again. Call replace_memory_document with a complete valid document, or finish without tools only if no change is needed.")


class _BoundModel:
    def __init__(self, responses: deque[AIMessage]) -> None:
        self.responses = responses
        self.messages: list[list[object]] = []
        self.configs: list[object] = []
        self.tracing_enabled: list[object] = []

    async def ainvoke(self, messages: list[object], *, config: object) -> AIMessage:
        self.messages.append(list(messages))
        self.configs.append(config)
        self.tracing_enabled.append(get_tracing_context()["enabled"])
        return self.responses.popleft()


class _Model:
    def __init__(self, *responses: AIMessage) -> None:
        self.bound = _BoundModel(deque(responses))
        self.tool_names: list[str] = []

    def bind_tools(self, tools):
        self.tool_names = [tool.name for tool in tools]
        return self.bound


def _runner(model: _Model, **kwargs: object) -> MemoryDreamRunner:
    runtime = ModelRuntime(
        app_config=SimpleNamespace(),  # type: ignore[arg-type]
        model_factory=lambda **_kwargs: object(),
    )
    return MemoryDreamRunner(
        model,
        model_runtime=runtime,
        **kwargs,
    )


def _input() -> MemoryDreamInput:
    return MemoryDreamInput(
        document=EMPTY_MEMORY_DOCUMENT,
        document_version=0,
        history=(
            DreamHistoryInput(
                sequence=7,
                tagged_text="- [permanent] User prefers concise Chinese.",
            ),
        ),
        max_tokens=8_000,
    )


@pytest.mark.asyncio
async def test_dream_runner_exposes_exactly_two_tools_and_returns_complete_draft() -> None:
    replacement = _document("- ActWeave is the project.")
    model = _Model(
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_memory_document",
                    "args": {},
                    "id": "read-1",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "replace_memory_document",
                    "args": {"content": replacement},
                    "id": "replace-1",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content="done"),
    )

    with tracing_context(enabled=True, client=MagicMock()):
        result = await _runner(model).run(_input())

    assert model.tool_names == ["read_memory_document", "replace_memory_document"]
    assert result.content == replacement
    assert result.replaced is True
    assert len(model.bound.messages) == 2
    assert model.bound.configs == [{"callbacks": []}, {"callbacks": []}]
    assert model.bound.tracing_enabled == [False, False]


@pytest.mark.asyncio
async def test_dream_runner_no_replace_is_a_successful_unchanged_result() -> None:
    model = _Model(
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_memory_document",
                    "args": {},
                    "id": "read-1",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content="no change"),
    )

    result = await _runner(model).run(_input())

    assert result.content == EMPTY_MEMORY_DOCUMENT
    assert result.replaced is False


@pytest.mark.asyncio
async def test_dream_runner_lets_the_model_revise_a_rejected_draft(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger="deerflow.agents.memory.dream")
    sensitive_draft_text = "DO-NOT-LOG-DREAM-DRAFT"
    oversized = _document(f"- {sensitive_draft_text} " + ("x" * 8_000))
    replacement = _document("- ActWeave is the project.")
    model = _Model(
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_memory_document",
                    "args": {},
                    "id": "read-1",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "replace_memory_document",
                    "args": {"content": oversized},
                    "id": "replace-rejected",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "replace_memory_document",
                    "args": {"content": replacement},
                    "id": "replace-revised",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content="done"),
    )
    value = _input()
    value = MemoryDreamInput(
        document=value.document,
        document_version=value.document_version,
        history=value.history,
        max_tokens=2_000,
    )

    result = await _runner(model).run(value)

    assert result.content == replacement
    assert result.replaced is True
    assert len(model.bound.messages) == 3
    retry_messages = model.bound.messages[2]
    rejected = retry_messages[-2]
    assert rejected.type == "tool"
    assert rejected.tool_call_id == "replace-rejected"
    estimated = estimate_memory_tokens(oversized)
    target = 1_800
    assert rejected.content == (
        "memory draft rejected: Memory document exceeds the token budget "
        f"(estimated {estimated}, limit 2000, overage {estimated - 2_000}). "
        f"Target <= {target} estimated tokens (90% of limit); remove at least "
        f"{estimated - target} estimated tokens before retrying. Document chars "
        f"{len(oversized)}, target-character-limit 1800; remove at least "
        f"{len(oversized) - 1_800} characters before retrying. Submit a valid "
        "complete document by removing lower-priority, stale, superseded, or "
        "duplicate facts, then call replace_memory_document again."
    )
    revision_instruction = retry_messages[-1]
    assert revision_instruction.type == "human"
    assert revision_instruction.content == (
        "The rejected draft was not saved. Rewrite the complete memory document "
        "before calling replace_memory_document again. Do not resubmit the same "
        "content. Keep the rewrite at or below target-token-limit 1800 estimated "
        "tokens and target-character-limit 1800 characters by removing "
        "lower-priority, stale, superseded, or duplicate facts."
    )
    dream_logs = [record.getMessage() for record in caplog.records if record.name == "deerflow.agents.memory.dream"]
    assert any("Dream round 1/8 tool_names=read_memory_document" in message for message in dream_logs)
    assert any("Dream round 2/8 rejected replace_memory_document" in message and f"estimated {estimated}" in message for message in dream_logs)
    assert all(sensitive_draft_text not in message for message in dream_logs)


@pytest.mark.asyncio
async def test_dream_runner_enters_exact_fresh_regeneration_context_after_two_over_budget_drafts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger="deerflow.agents.memory.dream")
    sensitive_draft_text = "DO-NOT-LOG-OR-CARRY-REJECTED-DRAFT"
    first_oversized = _document(f"- {sensitive_draft_text}-one " + ("x" * 800))
    second_oversized = _document(f"- {sensitive_draft_text}-two " + ("y" * 800))
    replacement = _document("- ActWeave is the project.")
    model = _Model(
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_memory_document",
                    "args": {},
                    "id": "read-before-repair",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "replace_memory_document",
                    "args": {"content": first_oversized},
                    "id": "replace-over-budget-1",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "replace_memory_document",
                    "args": {"content": second_oversized},
                    "id": "replace-over-budget-2",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_memory_document",
                    "args": {},
                    "id": "read-after-repair",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "replace_memory_document",
                    "args": {"content": replacement},
                    "id": "replace-valid-after-repair",
                    "type": "tool_call",
                }
            ],
        ),
    )
    original = _input()
    value = MemoryDreamInput(
        document=original.document,
        document_version=original.document_version,
        history=original.history,
        max_tokens=100,
    )

    result = await _runner(model).run(value)

    assert result == MemoryDreamResult(content=replacement, replaced=True)
    repair_context = model.bound.messages[3]
    assert [type(message) for message in repair_context] == [
        SystemMessage,
        HumanMessage,
        HumanMessage,
    ]
    assert repair_context[0].content == DREAM_PROMPT
    assert repair_context[1].content == render_dream_input(value)
    assert repair_context[2].content == (
        "Fresh-regeneration phase entered after 2 consecutive over-budget "
        "drafts. None of the rejected drafts were saved, and they are not "
        "included in this context. Start again from the exact frozen dream "
        "input above. Call read_memory_document before editing, then call "
        "replace_memory_document with one complete document at or below "
        "target-token-limit 90 estimated tokens and target-character-limit 90 "
        "characters. Do not reuse or reconstruct a prior rejected draft; prune "
        "lower-priority, stale, superseded, or duplicate facts."
    )
    assert first_oversized not in tuple(message.content for message in repair_context)
    assert second_oversized not in tuple(message.content for message in repair_context)
    assert not any(isinstance(message, (AIMessage, ToolMessage)) for message in repair_context)
    context_after_forced_read = model.bound.messages[4]
    assert isinstance(context_after_forced_read[-2], AIMessage)
    assert context_after_forced_read[-2].tool_calls[0]["name"] == "read_memory_document"
    assert isinstance(context_after_forced_read[-1], ToolMessage)
    assert context_after_forced_read[-1].content == value.document
    repair_logs = [record.getMessage() for record in caplog.records if "fresh-regeneration phase" in record.getMessage()]
    assert repair_logs == ["Dream entering fresh-regeneration phase: over_budget_count=2 target_tokens=90 target_characters=90"]
    assert all(sensitive_draft_text not in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_dream_runner_fresh_regeneration_is_used_at_most_once() -> None:
    oversized = tuple(_document(f"- oversized-{index} " + (character * 800)) for index, character in enumerate(("a", "b", "c", "d"), start=1))
    replacement = _document("- ActWeave is the project.")
    model = _Model(
        AIMessage(content="", tool_calls=[{"name": "read_memory_document", "args": {}, "id": "read-1", "type": "tool_call"}]),
        AIMessage(content="", tool_calls=[{"name": "replace_memory_document", "args": {"content": oversized[0]}, "id": "over-1", "type": "tool_call"}]),
        AIMessage(content="", tool_calls=[{"name": "replace_memory_document", "args": {"content": oversized[1]}, "id": "over-2", "type": "tool_call"}]),
        AIMessage(content="", tool_calls=[{"name": "read_memory_document", "args": {}, "id": "read-2", "type": "tool_call"}]),
        AIMessage(content="", tool_calls=[{"name": "replace_memory_document", "args": {"content": oversized[2]}, "id": "over-3", "type": "tool_call"}]),
        AIMessage(content="", tool_calls=[{"name": "replace_memory_document", "args": {"content": oversized[3]}, "id": "over-4", "type": "tool_call"}]),
        AIMessage(content="", tool_calls=[{"name": "replace_memory_document", "args": {"content": replacement}, "id": "replace-valid", "type": "tool_call"}]),
    )
    original = _input()
    value = MemoryDreamInput(
        document=original.document,
        document_version=original.document_version,
        history=original.history,
        max_tokens=100,
    )

    result = await _runner(model).run(value)

    assert result == MemoryDreamResult(content=replacement, replaced=True)
    repair_contexts = [
        messages
        for messages in model.bound.messages
        if len(messages) == 3 and isinstance(messages[0], SystemMessage) and isinstance(messages[1], HumanMessage) and isinstance(messages[2], HumanMessage) and str(messages[2].content).startswith("Fresh-regeneration phase")
    ]
    assert len(repair_contexts) == 1


@pytest.mark.asyncio
async def test_dream_runner_fresh_regeneration_requires_a_valid_replace() -> None:
    first_oversized = _document("- " + ("x" * 800))
    second_oversized = _document("- " + ("y" * 800))
    model = _Model(
        AIMessage(content="", tool_calls=[{"name": "read_memory_document", "args": {}, "id": "read-1", "type": "tool_call"}]),
        AIMessage(content="", tool_calls=[{"name": "replace_memory_document", "args": {"content": first_oversized}, "id": "over-1", "type": "tool_call"}]),
        AIMessage(content="", tool_calls=[{"name": "replace_memory_document", "args": {"content": second_oversized}, "id": "over-2", "type": "tool_call"}]),
        AIMessage(content="", tool_calls=[{"name": "read_memory_document", "args": {}, "id": "read-2", "type": "tool_call"}]),
        AIMessage(content="no replacement is needed"),
    )
    original = _input()
    value = MemoryDreamInput(
        document=original.document,
        document_version=original.document_version,
        history=original.history,
        max_tokens=100,
    )

    with pytest.raises(MemoryDreamError, match="MEMORY_DREAM_ROUND_LIMIT"):
        await _runner(model, max_rounds=5).run(value)


@pytest.mark.asyncio
async def test_dream_runner_non_budget_rejection_breaks_over_budget_streak() -> None:
    oversized = _document("- " + ("x" * 800))
    marker_invalid = f"{EMPTY_MEMORY_DOCUMENT}\n\n[H:12] leaked identity"
    replacement = _document("- ActWeave is the project.")
    model = _Model(
        AIMessage(content="", tool_calls=[{"name": "read_memory_document", "args": {}, "id": "read-1", "type": "tool_call"}]),
        AIMessage(content="", tool_calls=[{"name": "replace_memory_document", "args": {"content": oversized}, "id": "over-1", "type": "tool_call"}]),
        AIMessage(content="", tool_calls=[{"name": "replace_memory_document", "args": {"content": marker_invalid}, "id": "marker-invalid", "type": "tool_call"}]),
        AIMessage(content="", tool_calls=[{"name": "replace_memory_document", "args": {"content": oversized}, "id": "over-2", "type": "tool_call"}]),
        AIMessage(content="", tool_calls=[{"name": "replace_memory_document", "args": {"content": replacement}, "id": "replace-valid", "type": "tool_call"}]),
    )
    original = _input()
    value = MemoryDreamInput(
        document=original.document,
        document_version=original.document_version,
        history=original.history,
        max_tokens=100,
    )

    result = await _runner(model).run(value)

    assert result == MemoryDreamResult(content=replacement, replaced=True)
    assert not any(len(messages) == 3 and isinstance(messages[-1], HumanMessage) and str(messages[-1].content).startswith("Fresh-regeneration phase") for messages in model.bound.messages)


@pytest.mark.asyncio
async def test_dream_runner_requires_a_valid_replace_after_rejected_draft() -> None:
    oversized = _document("- " + ("x" * 800))
    replacement = _document("- ActWeave is the project.")
    model = _Model(
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_memory_document",
                    "args": {},
                    "id": "read-1",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "replace_memory_document",
                    "args": {"content": oversized},
                    "id": "replace-rejected",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content="done without replacing"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "replace_memory_document",
                    "args": {"content": replacement},
                    "id": "replace-revised",
                    "type": "tool_call",
                }
            ],
        ),
    )
    value = _input()
    value = MemoryDreamInput(
        document=value.document,
        document_version=value.document_version,
        history=value.history,
        max_tokens=100,
    )

    result = await _runner(model).run(value)

    assert result == MemoryDreamResult(content=replacement, replaced=True)
    retry_messages = model.bound.messages[3]
    retry_instruction = retry_messages[-1]
    assert retry_instruction.type == "human"
    assert retry_instruction.content == (
        "The rejected draft was not saved. Rewrite the complete memory document "
        "before calling replace_memory_document again. Do not resubmit the same "
        "content. Keep the rewrite at or below target-token-limit 90 estimated "
        "tokens and target-character-limit 90 characters by removing "
        "lower-priority, stale, superseded, or duplicate facts."
    )


@pytest.mark.asyncio
async def test_dream_runner_detects_the_same_rejected_draft_by_digest() -> None:
    rejected = f"{EMPTY_MEMORY_DOCUMENT}\n\n[H:12] leaked identity"
    replacement = _document("- ActWeave is the project.")
    model = _Model(
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_memory_document",
                    "args": {},
                    "id": "read-1",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "replace_memory_document",
                    "args": {"content": rejected},
                    "id": "replace-rejected-1",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "replace_memory_document",
                    "args": {"content": rejected},
                    "id": "replace-rejected-2",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "replace_memory_document",
                    "args": {"content": replacement},
                    "id": "replace-revised",
                    "type": "tool_call",
                }
            ],
        ),
    )
    value = _input()
    value = MemoryDreamInput(
        document=value.document,
        document_version=value.document_version,
        history=value.history,
        max_tokens=100,
    )

    result = await _runner(model).run(value)

    assert result == MemoryDreamResult(content=replacement, replaced=True)
    second_retry_messages = model.bound.messages[3]
    repeated_tool_feedback = second_retry_messages[-2]
    assert repeated_tool_feedback.type == "tool"
    assert repeated_tool_feedback.tool_call_id == "replace-rejected-2"
    assert repeated_tool_feedback.content.startswith("memory draft rejected: same rejected draft; it was not saved. ")
    repeated_instruction = second_retry_messages[-1]
    assert repeated_instruction.type == "human"
    assert repeated_instruction.content == (
        "This is the same rejected draft; it was not saved. Rewrite the complete "
        "memory document before calling replace_memory_document again. Do not "
        "resubmit the same content. Keep the rewrite at or below "
        "target-token-limit 90 estimated tokens and target-character-limit 90 "
        "characters by removing lower-priority, stale, superseded, or duplicate "
        "facts."
    )
    assert not any(len(messages) == 3 and isinstance(messages[-1], HumanMessage) and str(messages[-1].content).startswith("Fresh-regeneration phase") for messages in model.bound.messages)


@pytest.mark.asyncio
async def test_dream_runner_rejected_draft_then_no_tool_exhausts_round_limit() -> None:
    oversized = _document("- " + ("x" * 800))
    model = _Model(
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_memory_document",
                    "args": {},
                    "id": "read-1",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "replace_memory_document",
                    "args": {"content": oversized},
                    "id": "replace-rejected",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content="done without replacing"),
    )
    value = _input()
    value = MemoryDreamInput(
        document=value.document,
        document_version=value.document_version,
        history=value.history,
        max_tokens=100,
    )

    with pytest.raises(MemoryDreamError, match="MEMORY_DREAM_ROUND_LIMIT"):
        await _runner(model, max_rounds=3).run(value)


@pytest.mark.asyncio
async def test_dream_runner_rejects_any_ambient_tool_call() -> None:
    model = _Model(
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "bash",
                    "args": {"command": "true"},
                    "id": "bad-1",
                    "type": "tool_call",
                }
            ],
        )
    )

    with pytest.raises(MemoryDreamError, match="MEMORY_DREAM_TOOL_INVALID"):
        await _runner(model).run(_input())
