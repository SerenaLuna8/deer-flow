"""Memory review, budget-rewrite, and degradation guardrails."""

from __future__ import annotations

import uuid

import pytest

from app.worker.memory_dream import _deletion_ratio_bucket
from deerflow.agents.memory.dream import (
    EMPTY_MEMORY_DOCUMENT,
    MEMORY_DOCUMENT_SECTIONS,
    DreamHistoryInput,
    MemoryDreamInput,
    render_dream_input,
)
from deerflow.persistence.private_work.memory_document_repository import (
    BUDGET_REWRITE_HISTORY_DIGEST,
    MEMORY_REVIEW_DELETION_RATIO,
    MEMORY_REVIEW_MIN_LINES,
    MemoryDreamHistoryRecord,
    memory_document_deletion_ratio,
    memory_document_needs_review,
)


def _document(*lines: str) -> str:
    return "\n\n".join(
        (
            MEMORY_DOCUMENT_SECTIONS[0],
            *lines,
            MEMORY_DOCUMENT_SECTIONS[1],
            MEMORY_DOCUMENT_SECTIONS[2],
            MEMORY_DOCUMENT_SECTIONS[3],
        )
    )


def _history(*texts: str) -> tuple[MemoryDreamHistoryRecord, ...]:
    return tuple(
        MemoryDreamHistoryRecord(
            id=uuid.uuid4(),
            sequence=index + 1,
            tagged_text=text,
            content_digest="c" * 64,
        )
        for index, text in enumerate(texts)
    )


def test_deletion_ratio_needs_a_meaningful_previous_document() -> None:
    small = _document(*(f"- fact {index}" for index in range(MEMORY_REVIEW_MIN_LINES - 1)))
    assert memory_document_deletion_ratio(small, EMPTY_MEMORY_DOCUMENT) is None

    big = _document(*(f"- fact {index}" for index in range(10)))
    assert memory_document_deletion_ratio(big, big) == 0.0
    assert memory_document_deletion_ratio(big, EMPTY_MEMORY_DOCUMENT) == 1.0


def test_deletion_ratio_counts_only_pure_deletions_of_content_lines() -> None:
    previous = _document(*(f"- fact {index}" for index in range(10)))
    kept = _document(*(f"- fact {index}" for index in range(6)))

    ratio = memory_document_deletion_ratio(previous, kept)
    assert ratio == pytest.approx(0.4)
    # Section headings never count as content lines.
    assert memory_document_deletion_ratio(previous, previous) == 0.0


def test_needs_review_flags_large_deletions_without_a_correction() -> None:
    previous = _document(*(f"- fact {index}" for index in range(10)))
    shrunk = _document(*(f"- fact {index}" for index in range(5)))

    assert memory_document_needs_review(previous, shrunk, _history("- [durable] keep going")) is True
    assert memory_document_needs_review(previous, previous, _history("- [durable] keep going")) is False


def test_needs_review_respects_correction_batches_and_small_documents() -> None:
    previous = _document(*(f"- fact {index}" for index in range(10)))
    shrunk = _document("- fact 0")

    corrected = _history(
        "- [durable] unrelated",
        "- [correction] The old plan was abandoned.",
    )
    assert memory_document_needs_review(previous, shrunk, corrected) is False

    small_previous = _document(*(f"- fact {index}" for index in range(MEMORY_REVIEW_MIN_LINES - 1)))
    assert memory_document_needs_review(small_previous, EMPTY_MEMORY_DOCUMENT, ()) is False

    below_threshold = _document(*(f"- fact {index}" for index in range(10)))
    barely_kept = _document(*(f"- fact {index}" for index in range(7)))
    assert memory_document_deletion_ratio(below_threshold, barely_kept) == pytest.approx(0.3)
    assert MEMORY_REVIEW_DELETION_RATIO > 0.3
    assert memory_document_needs_review(below_threshold, barely_kept, ()) is False


def test_deletion_ratio_bucket_is_a_bounded_decile_label() -> None:
    assert _deletion_ratio_bucket(0.4) == "40-50%"
    assert _deletion_ratio_bucket(0.55) == "50-60%"
    assert _deletion_ratio_bucket(0.99) == "90-100%"
    assert _deletion_ratio_bucket(1.0) == "90-100%"


def test_budget_rewrite_dream_input_freezes_zero_history() -> None:
    value = MemoryDreamInput(
        document=EMPTY_MEMORY_DOCUMENT,
        document_version=4,
        history=(),
        max_tokens=100,
        budget_rewrite=True,
    )
    rendered = render_dream_input(value)

    assert "[H:" not in rendered
    assert "No new history entries." in rendered
    assert "budget rewrite" in rendered

    with pytest.raises(ValueError, match="budget rewrite"):
        MemoryDreamInput(
            document=EMPTY_MEMORY_DOCUMENT,
            document_version=4,
            history=(DreamHistoryInput(sequence=1, tagged_text="- [durable] x"),),
            max_tokens=100,
            budget_rewrite=True,
        )
    with pytest.raises(ValueError, match="history batch"):
        MemoryDreamInput(
            document=EMPTY_MEMORY_DOCUMENT,
            document_version=4,
            history=(),
            max_tokens=100,
        )


def test_render_annotates_tool_origin_history_rows_only() -> None:
    value = MemoryDreamInput(
        document=EMPTY_MEMORY_DOCUMENT,
        document_version=1,
        history=(
            DreamHistoryInput(sequence=3, tagged_text="- [durable] from snip"),
            DreamHistoryInput(
                sequence=4,
                tagged_text="- [permanent] from tool",
                origin="tool",
            ),
        ),
        max_tokens=1_000,
    )
    rendered = render_dream_input(value)

    assert "[H:3]\n- [durable] from snip" in rendered
    assert "[H:4] (origin=tool)\n- [permanent] from tool" in rendered

    with pytest.raises(ValueError, match="origin"):
        DreamHistoryInput(sequence=1, tagged_text="- [durable] x", origin="user")


def test_budget_rewrite_sentinel_digest_is_pinned() -> None:
    import hashlib

    assert BUDGET_REWRITE_HISTORY_DIGEST == hashlib.sha256(b"deerflow.dream.budget_rewrite.empty.v1").hexdigest()
