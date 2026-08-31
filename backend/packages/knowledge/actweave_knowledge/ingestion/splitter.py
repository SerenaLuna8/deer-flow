"""Pure text cleaning and chunk splitting; no I/O.

Cleaning normalizes newlines, trims trailing whitespace per line (leading
indentation stays meaningful for Markdown and code), compresses runs of blank
lines, and trims the whole text.

Splitting is recursive-by-separator: the user separator (default ``\\n\\n``)
is tried first, then the fallback sequence ``\\n\\n`` → ``\\n`` → ``。`` →
``. `` → space → character level. Pieces keep their trailing separator so
chunks read like the source; adjacent pieces are packed up to ``chunk_size``
and up to ``chunk_overlap`` trailing characters carry over into the next
chunk at piece granularity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..contracts import (
    KNOWLEDGE_DEFAULT_CHILD_CHUNK_SEPARATOR,
    KNOWLEDGE_DEFAULT_CHUNK_SEPARATOR,
)
from .extractor import ExtractedBlock

_LINE_BREAKS = re.compile(r"\r\n?")
_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")

# Fallback boundaries tried in order when a piece is still oversized; the
# empty string means character-level packing and always terminates. Mirrors
# Dify's FixedRecursiveCharacterTextSplitter fallback sequence.
_FALLBACK_SEPARATORS: tuple[str, ...] = ("\n\n", "\n", "。", ". ", " ", "")

_SEPARATOR_ESCAPES = {"\\n": "\n", "\\t": "\t", "\\r": "\r"}
_SEPARATOR_ESCAPE_RE = re.compile(r"\\[ntr]")


@dataclass(frozen=True, slots=True)
class SegmentDraft:
    """One publishable segment: contiguous position, content, and origin.

    ``children`` carries the parent_child-mode second-level chunks in order;
    it stays empty in general mode.
    """

    position: int
    content: str
    source_position: dict[str, Any] = field(default_factory=dict)
    children: tuple[str, ...] = ()


def normalize_text(text: str) -> str:
    """Unify newlines, right-trim lines, compress blank runs, trim the text."""

    unified = _LINE_BREAKS.sub("\n", text)
    right_trimmed = "\n".join(line.rstrip() for line in unified.split("\n"))
    return _EXCESS_BLANK_LINES.sub("\n\n", right_trimmed).strip()


def decode_separator(raw: str) -> str:
    """Decode the user-typed escaped form (``\\n``/``\\t``/``\\r`` only).

    Anything else passes through verbatim, so Chinese punctuation and literal
    backslashes survive unchanged (unlike ``unicode_escape``, which mangles
    non-ASCII input).
    """

    return _SEPARATOR_ESCAPE_RE.sub(lambda match: _SEPARATOR_ESCAPES[match.group(0)], raw)


def split_blocks(
    blocks: list[ExtractedBlock],
    *,
    chunk_size: int,
    chunk_overlap: int,
    separator: str = KNOWLEDGE_DEFAULT_CHUNK_SEPARATOR,
) -> list[SegmentDraft]:
    """Split cleaned blocks into drafts with positions starting at 1.

    Each draft inherits the ``source_position`` of the block it came from, so
    a chunk can always be traced back to its page, paragraph, or row.
    ``separator`` is the escaped form as stored on the document row.
    """

    decoded = decode_separator(separator)
    separators = [decoded] + [candidate for candidate in _FALLBACK_SEPARATORS if candidate != decoded]
    drafts: list[SegmentDraft] = []
    position = 1
    for block in blocks:
        text = normalize_text(block.text)
        if not text:
            continue
        for chunk in _recursive_chunks(text, separators, chunk_size=chunk_size, chunk_overlap=chunk_overlap):
            drafts.append(
                SegmentDraft(
                    position=position,
                    content=chunk,
                    source_position=dict(block.source_position),
                )
            )
            position += 1
    return drafts


def split_child_chunks(
    content: str,
    *,
    child_chunk_size: int,
    child_chunk_separator: str = KNOWLEDGE_DEFAULT_CHILD_CHUNK_SEPARATOR,
) -> tuple[str, ...]:
    """Second-level split of one parent chunk; children never overlap.

    Children exist to carry vectors, so overlapping them would only recall the
    same parent twice. The separator uses the same escaped form and fallback
    sequence as the parent splitter.
    """

    decoded = decode_separator(child_chunk_separator)
    separators = [decoded] + [candidate for candidate in _FALLBACK_SEPARATORS if candidate != decoded]
    return tuple(_recursive_chunks(content, separators, chunk_size=child_chunk_size, chunk_overlap=0))


def attach_children(
    drafts: list[SegmentDraft],
    *,
    child_chunk_size: int,
    child_chunk_separator: str = KNOWLEDGE_DEFAULT_CHILD_CHUNK_SEPARATOR,
) -> list[SegmentDraft]:
    """Return parent drafts with their parent_child-mode children populated."""

    return [
        SegmentDraft(
            position=draft.position,
            content=draft.content,
            source_position=dict(draft.source_position),
            children=split_child_chunks(
                draft.content,
                child_chunk_size=child_chunk_size,
                child_chunk_separator=child_chunk_separator,
            ),
        )
        for draft in drafts
    ]


def _split_keep_suffix(text: str, separator: str) -> list[str]:
    """Split on ``separator`` keeping it attached to the preceding piece."""

    if separator == "":
        return list(text)
    parts = text.split(separator)
    return [part + separator for part in parts[:-1]] + [parts[-1]]


def _recursive_chunks(
    text: str,
    separators: list[str],
    *,
    chunk_size: int,
    chunk_overlap: int,
) -> list[str]:
    """Split by the first separator present; recurse into oversized pieces."""

    separator = separators[-1]
    deeper: list[str] = []
    for index, candidate in enumerate(separators):
        if candidate == "" or candidate in text:
            separator = candidate
            deeper = separators[index + 1 :]
            break
    pieces = [piece for piece in _split_keep_suffix(text, separator) if piece]
    chunks: list[str] = []
    pending: list[str] = []
    for piece in pieces:
        if len(piece) <= chunk_size:
            pending.append(piece)
            continue
        if pending:
            chunks.extend(_pack_pieces(pending, chunk_size=chunk_size, chunk_overlap=chunk_overlap))
            pending = []
        if deeper:
            chunks.extend(_recursive_chunks(piece, deeper, chunk_size=chunk_size, chunk_overlap=chunk_overlap))
        else:  # pragma: no cover - the "" separator always yields single characters
            chunks.append(piece.strip())
    if pending:
        chunks.extend(_pack_pieces(pending, chunk_size=chunk_size, chunk_overlap=chunk_overlap))
    return chunks


def _pack_pieces(pieces: list[str], *, chunk_size: int, chunk_overlap: int) -> list[str]:
    """Pack separator-suffixed pieces into chunks of at most ``chunk_size``.

    The retained tail (at most ``chunk_overlap`` characters of whole pieces)
    forms the overlap. A chunk is only emitted while the window holds content
    that no earlier chunk covered (``fresh``), so a window of pure carry-over
    never re-emits as its own chunk.
    """

    chunks: list[str] = []
    window: list[str] = []
    window_length = 0
    fresh = False
    for piece in pieces:
        if window and window_length + len(piece) > chunk_size:
            if fresh:
                chunk = "".join(window).strip()
                if chunk:
                    chunks.append(chunk)
                fresh = False
            while window and (window_length > chunk_overlap or window_length + len(piece) > chunk_size):
                window_length -= len(window.pop(0))
        window.append(piece)
        window_length += len(piece)
        if piece.strip():
            fresh = True
    if fresh:
        tail = "".join(window).strip()
        if tail:
            chunks.append(tail)
    return chunks
