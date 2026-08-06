from __future__ import annotations

import re

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from deerflow.agents.memory.snip import (
    MAX_SNIP_OUTPUT_CHARS,
    SNIP_ARCHIVE_PROMPT,
    SNIP_ARCHIVE_PROMPT_VERSION,
    SNIP_NOTHING,
    SnipOutputInvalid,
    compute_snip_source_digest,
    normalize_snip_output,
    validate_snip_output,
)

EXPECTED_SNIP_ARCHIVE_PROMPT = """Extract key facts from this conversation. For each fact, annotate its memory attributes.

Only SNIP facts deserve a non-[skip] mark:
- Signal: would the user need to repeat this if forgotten?
- Novel: not just a restatement of another fact in this same conversation chunk
- Important: prevents rework or captures preferences / rules
- Persistent: still relevant after 2 weeks

Output one fact per line in this format:
- [mark] fact content

Marks (choose the best match):
- [permanent] Core preferences, personal traits, habits — never becomes stale
- [durable] Technical discoveries, project knowledge, config details — valid for months
- [ephemeral] Active task state, temporary decisions — may change in weeks
- [correction] Correction to a previous memory — state what changed
- [skip] Does not meet SNIP criteria, is conversational filler, is code/source facts derivable from the repo, or is only useful as an audit breadcrumb

Priority: user corrections and preferences > solutions > decisions > events > environment facts. The most valuable memory prevents the user from having to repeat themselves.

Do not mark something [skip] merely because it might already exist in long-term memory; Dream handles long-term-memory deduplication later.

Output concise bullet points only. No preamble, no commentary.
If nothing noteworthy happened, output: (nothing)

The input contains a Previous Summary and a New Conversation Segment.
Return one complete replacement summary that covers both.
Keep the final output within 1000 characters.
When space is limited, retain corrections and preferences first, then permanent
facts, durable decisions/solutions, and only the newest active ephemeral state.
Drop stale events, environment details, and skip items first.

Input:
{messages}
"""


def test_snip_prompt_and_version_are_fixed_snapshots() -> None:
    assert SNIP_ARCHIVE_PROMPT_VERSION == "snip-archive-prompt-v1"
    assert SNIP_ARCHIVE_PROMPT == EXPECTED_SNIP_ARCHIVE_PROMPT
    assert SNIP_ARCHIVE_PROMPT.count("{messages}") == 1


def test_normalize_snip_output_changes_only_line_endings_and_outer_whitespace() -> None:
    raw = " \r\n- [permanent] User prefers Chinese.  \r- [durable] Use PostgreSQL.\r\n\t"

    assert normalize_snip_output(raw) == ("- [permanent] User prefers Chinese.  \n- [durable] Use PostgreSQL.")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  (nothing)\r\n", SNIP_NOTHING),
        (
            "- [permanent] User prefers concise Chinese.\n"
            "- [durable] PostgreSQL is the only application database.\n"
            "- [ephemeral] PR2 is in progress.\n"
            "- [correction] Replace SQLite with PostgreSQL.\n"
            "- [skip] The greeting has no lasting value.",
            "- [permanent] User prefers concise Chinese.\n"
            "- [durable] PostgreSQL is the only application database.\n"
            "- [ephemeral] PR2 is in progress.\n"
            "- [correction] Replace SQLite with PostgreSQL.\n"
            "- [skip] The greeting has no lasting value.",
        ),
        (
            "- [permanent] User prefers concise Chinese.\n\n- [durable] PostgreSQL is the only application database.",
            "- [permanent] User prefers concise Chinese.\n\n- [durable] PostgreSQL is the only application database.",
        ),
    ],
)
def test_validate_snip_output_accepts_only_the_contract(raw: str, expected: str) -> None:
    assert validate_snip_output(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        " \r\n\t ",
        "Nothing noteworthy happened.",
        "Preamble\n- [durable] A fact.",
        "```text\n- [durable] A fact.\n```",
        "- [unknown] A fact.",
        "- [permanent]",
        "- [permanent]   ",
        "- [ permanent] A fact.",
        "* [durable] A fact.",
        "- [durable]  Extra leading content whitespace.",
        "- [durable] A fact.  \n- [skip] Another fact.",
        "- [durable] A fact.\n \n- [skip] Another fact.",
        "(nothing)\n- [skip] Extra output.",
    ],
)
def test_validate_snip_output_rejects_invalid_shapes(raw: str) -> None:
    with pytest.raises(SnipOutputInvalid):
        validate_snip_output(raw)


def test_validate_snip_output_rejects_non_string_and_over_1000_characters() -> None:
    with pytest.raises(SnipOutputInvalid):
        validate_snip_output(None)  # type: ignore[arg-type]

    prefix = "- [durable] "
    assert len(prefix + ("x" * (MAX_SNIP_OUTPUT_CHARS - len(prefix)))) == 1000
    assert validate_snip_output(prefix + ("x" * (MAX_SNIP_OUTPUT_CHARS - len(prefix))))

    with pytest.raises(SnipOutputInvalid):
        validate_snip_output(prefix + ("x" * (MAX_SNIP_OUTPUT_CHARS - len(prefix) + 1)))


def test_compute_snip_source_digest_is_stable_and_snapshot_locked() -> None:
    messages = (
        HumanMessage(id="human-1", content="Use PostgreSQL only."),
        AIMessage(
            id="assistant-1",
            content=[
                {"type": "text", "text": "Understood."},
                {"args": {"port": 5432, "database": "deerflow"}, "type": "tool_call"},
            ],
        ),
    )

    digest = compute_snip_source_digest(
        previous_summary="- [durable] Existing decision.",
        source_checkpoint_id="1f6bde52-3f6f-4d92-bef4-1c4441f62e42",
        messages=messages,
    )

    assert digest == "8d7fe5577e805813271fdadf54d1c3f28580baba976f02cb395f859f550a0c6d"
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert digest == compute_snip_source_digest(
        previous_summary="- [durable] Existing decision.",
        source_checkpoint_id="1f6bde52-3f6f-4d92-bef4-1c4441f62e42",
        messages=messages,
    )


def test_compute_snip_source_digest_binds_all_source_identity_and_order() -> None:
    first = HumanMessage(id="human-1", content="first")
    second = AIMessage(id="assistant-1", content="second")
    baseline = compute_snip_source_digest(
        previous_summary="summary",
        source_checkpoint_id="checkpoint-a",
        messages=(first, second),
    )

    variants = {
        compute_snip_source_digest(
            previous_summary="changed summary",
            source_checkpoint_id="checkpoint-a",
            messages=(first, second),
        ),
        compute_snip_source_digest(
            previous_summary="summary",
            source_checkpoint_id="checkpoint-b",
            messages=(first, second),
        ),
        compute_snip_source_digest(
            previous_summary="summary",
            source_checkpoint_id="checkpoint-a",
            messages=(second, first),
        ),
        compute_snip_source_digest(
            previous_summary="summary",
            source_checkpoint_id="checkpoint-a",
            messages=(HumanMessage(id="human-2", content="first"), second),
        ),
        compute_snip_source_digest(
            previous_summary="summary",
            source_checkpoint_id="checkpoint-a",
            messages=(HumanMessage(id="human-1", content="changed"), second),
        ),
    }

    assert baseline not in variants
    assert len(variants) == 5


def test_compute_snip_source_digest_canonicalizes_content_mapping_order() -> None:
    one = HumanMessage(
        id="human-1",
        content=[{"type": "text", "metadata": {"b": 2, "a": 1}, "text": "hello"}],
    )
    two = HumanMessage(
        id="human-1",
        content=[{"text": "hello", "metadata": {"a": 1, "b": 2}, "type": "text"}],
    )

    assert compute_snip_source_digest(
        previous_summary=None,
        source_checkpoint_id="checkpoint-a",
        messages=(one,),
    ) == compute_snip_source_digest(
        previous_summary=None,
        source_checkpoint_id="checkpoint-a",
        messages=(two,),
    )


@pytest.mark.parametrize(
    ("previous_summary", "checkpoint_id", "messages"),
    [
        (1, "checkpoint-a", (HumanMessage(id="human-1", content="hello"),)),
        (None, "", (HumanMessage(id="human-1", content="hello"),)),
        (None, "checkpoint-a", (object(),)),
    ],
)
def test_compute_snip_source_digest_rejects_invalid_source(
    previous_summary: object,
    checkpoint_id: str,
    messages: tuple[object, ...],
) -> None:
    with pytest.raises(ValueError):
        compute_snip_source_digest(
            previous_summary=previous_summary,  # type: ignore[arg-type]
            source_checkpoint_id=checkpoint_id,
            messages=messages,  # type: ignore[arg-type]
        )
