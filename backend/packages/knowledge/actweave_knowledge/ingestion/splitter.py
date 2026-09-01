"""Pure Knowledge chunking with source mapping and deterministic Token budgets.

``split_documents`` protects Markdown structures and creates canonical parent
and child drafts. The historical character recursion remains behind
``split_blocks``, ``split_child_chunks`` and the explicit character profile.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from itertools import chain

from ..contracts import (
    KNOWLEDGE_DEFAULT_CHILD_CHUNK_SEPARATOR,
    KNOWLEDGE_DEFAULT_CHUNK_SEPARATOR,
    KNOWLEDGE_MAX_SEGMENTS_PER_DOCUMENT,
    KNOWLEDGE_QUOTA_EXCEEDED,
    KnowledgeError,
)
from ..extraction.contracts import AttachmentOccurrence, ChunkProfile, Document, ExtractionError, SourceSpan
from .extractor import ExtractedBlock
from .index_text import build_index_text, has_indexable_source_text
from .source_mapping import clip_source_spans
from .structure import StructureUnit, context_unit, inline_atoms, join_units, slice_unit, structure_groups, trim_unit
from .tokenizer import TOKENIZER_PROFILE_ID, count_knowledge_tokens, tokenizer_fingerprint

_LINE_BREAKS = re.compile(r"\r\n?")
_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")

# Fallback boundaries tried in order when a piece is still oversized; the
# empty string means character-level packing and always terminates. Mirrors
# upstream's FixedRecursiveCharacterTextSplitter fallback sequence.
_FALLBACK_SEPARATORS: tuple[str, ...] = ("\n\n", "\n", "。", ". ", " ", "")

_SEPARATOR_ESCAPES = {"\\n": "\n", "\\t": "\t", "\\r": "\r"}
_SEPARATOR_ESCAPE_RE = re.compile(r"\\[ntr]")


@dataclass(frozen=True, slots=True)
class ChildDraft:
    content: str
    index_text: str
    token_count: int
    source_spans: tuple[SourceSpan, ...] = ()


@dataclass(frozen=True, slots=True)
class SegmentDraft:
    """One publishable segment: contiguous position, content, and origin.

    ``children`` carries the parent_child-mode second-level chunks in order;
    it stays empty in general mode.
    """

    position: int
    content: str
    index_text: str = ""
    token_count: int = 0
    source_position: dict[str, str | int] = field(default_factory=dict)
    source_spans: tuple[SourceSpan, ...] = ()
    attachments: tuple[AttachmentOccurrence, ...] = ()
    children: tuple[ChildDraft, ...] = ()


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
                    index_text=build_index_text(chunk),
                    token_count=count_knowledge_tokens(build_index_text(chunk)),
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

    result = []
    for draft in drafts:
        children = []
        cursor = 0
        for content in split_child_chunks(draft.content, child_chunk_size=child_chunk_size, child_chunk_separator=child_chunk_separator):
            start = draft.content.find(content, cursor)
            cursor = start + len(content)
            index_text = build_index_text(content)
            children.append(ChildDraft(content, index_text, count_knowledge_tokens(index_text), clip_source_spans(draft.source_spans, start, cursor)))
        result.append(replace(draft, children=tuple(children)))
    return result


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


def fits_chunk(markdown: str, token_limit: int) -> bool:
    """Both display and actual index input must fit; characters remain capped."""
    return len(markdown) <= 4000 and count_knowledge_tokens(markdown) <= token_limit and count_knowledge_tokens(build_index_text(markdown)) <= token_limit


def _prefix_error() -> ExtractionError:
    return ExtractionError("CONTEXT_PREFIX_EXCEEDS_BUDGET", "标题或表头超过分段预算，请增大预算或调整来源内容")


def _render(prefix: StructureUnit, units: list[StructureUnit], separator: str) -> StructureUnit:
    return join_units([prefix, join_units(units, separator)], separator)


def _split_ranges(unit: StructureUnit, separators: list[str], fits: Callable[[StructureUnit], bool]) -> Iterator[StructureUnit]:
    """User separator first, then fallback, never inside a Markdown atom.

    Unicode fallback searches at most one 4000-character window at a time;
    it never materializes a character object for the whole source document.
    """
    if fits(unit):
        yield unit
        return
    atoms = inline_atoms(unit.content)
    for index, separator in enumerate(separators):
        if not separator:
            start = 0
            while start < len(unit.content):
                ceiling = min(start + 4000, len(unit.content))
                boundaries = [end for end in range(start + 1, ceiling + 1) if not any(a < end < b for a, b in atoms)]
                if not boundaries or not fits(slice_unit(unit, start, boundaries[0])):
                    raise ExtractionError("ATOMIC_CONTENT_EXCEEDS_BUDGET", "不可拆分的链接或图片超过分段预算")
                low, high = 0, len(boundaries) - 1
                while low < high:
                    middle = (low + high + 1) // 2
                    if fits(slice_unit(unit, start, boundaries[middle])):
                        low = middle
                    else:
                        high = middle - 1
                end = boundaries[low]
                yield slice_unit(unit, start, end)
                start = end
            return
        start = cursor = 0
        found_boundary = False
        while (found := unit.content.find(separator, cursor)) >= 0:
            stop = found + len(separator)
            cursor = stop
            if any(a < stop < b or a <= found < b for a, b in atoms):
                continue
            found_boundary = True
            piece = slice_unit(unit, start, stop)
            yield from _split_ranges(piece, separators[index + 1 :], fits)
            start = stop
        if found_boundary:
            if start < len(unit.content):
                yield from _split_ranges(slice_unit(unit, start, len(unit.content)), separators[index + 1 :], fits)
            return
    raise _prefix_error()


def _indented_code_body(unit: StructureUnit) -> StructureUnit:
    """Remove the four Markdown indentation columns, not code indentation.

    Slice each physical line so source spans retain only the actual payload;
    a leading tab advances to the next four-column tab stop.
    """
    parts = []
    offset = 0
    for line in unit.content.splitlines(keepends=True):
        columns = cut = 0
        while cut < len(line) and columns < 4 and line[cut] in " \t":
            columns = columns + 1 if line[cut] == " " else columns + 4 - columns % 4
            cut += 1
        parts.append(slice_unit(unit, offset + cut, offset + len(line)))
        offset += len(line)
    return join_units(parts, "")


def _code_pieces(unit: StructureUnit, prefix: StructureUnit, limit: int, separator: str, *, following: tuple[StructureUnit, ...] = ()) -> Iterator[StructureUnit]:
    lines = unit.content.splitlines(keepends=True)
    fence = re.match(r"^\s*(`{3,}|~{3,})[^\n]*", lines[0]) if unit.kind == "code" else None
    if fence:
        opening_end = len(lines[0])
        has_closing = len(lines) > 1 and re.fullmatch(r"\s*" + re.escape(fence[1][0]) + "{" + str(len(fence[1])) + r",}\s*", lines[-1])
        body_end = len(unit.content) - len(lines[-1]) if has_closing else len(unit.content)
        opening = slice_unit(unit, 0, opening_end)
        closing = slice_unit(unit, body_end, len(unit.content)) if has_closing else StructureUnit(fence[1])
        body = slice_unit(unit, opening_end, body_end)
    else:
        opening, closing = StructureUnit("```\n"), StructureUnit("```")
        body = _indented_code_body(unit) if unit.kind == "indented_code" else unit

    def wrap(value: StructureUnit) -> StructureUnit:
        return join_units([context_unit(opening), value, StructureUnit("" if value.content.endswith("\n") else "\n"), context_unit(closing)], "")

    if not fits_chunk(_render(prefix, [wrap(StructureUnit("x")), *following], separator).content, limit):
        raise _prefix_error()

    def fits(value: StructureUnit) -> bool:
        return fits_chunk(_render(prefix, [wrap(value), *following], separator).content, limit)

    pieces = _split_ranges(body, ["\n", ""], fits)
    pending = []
    for piece in pieces:
        if pending and not fits(join_units([*pending, piece], "")):
            yield replace(wrap(join_units(pending, "")), kind="code")
            pending = []
        pending.append(piece)
    if pending:
        yield replace(wrap(join_units(pending, "")), kind="code")


def _table_cells(unit: StructureUnit) -> list[StructureUnit]:
    atoms = inline_atoms(unit.content)
    cuts = [m.start() for m in re.finditer(r"(?<!\\)\|", unit.content) if not any(start <= m.start() < end for start, end in atoms)]
    if not cuts:
        return []
    cuts = [-1, *cuts, len(unit.content)]
    cells = [trim_unit(slice_unit(unit, a + 1, b)) for a, b in zip(cuts, cuts[1:])]
    if not cells[0].content:
        cells.pop(0)
    if cells and not cells[-1].content:
        cells.pop()
    return cells


def _field_pieces(unit: StructureUnit, prefix: StructureUnit, limit: int, separator: str, separators: list[str]) -> Iterator[StructureUnit]:
    fields: list[tuple[StructureUnit, StructureUnit]] = []
    if unit.kind == "table_row":
        header_lines = [(m.start(), m.end()) for m in re.finditer(r"(?m)^\|.*$", prefix.content)]
        header = slice_unit(prefix, *header_lines[0]) if header_lines else StructureUnit("")
        labels = _table_cells(header)
        for index, cell in enumerate(_table_cells(unit)):
            label = context_unit(labels[index]) if index < len(labels) else StructureUnit(f"列{index + 1}")
            fields.append((join_units([StructureUnit("- "), label, StructureUnit(": ")], ""), cell))
    else:
        offset = 0
        for line in unit.content.splitlines(keepends=True):
            field = slice_unit(unit, offset, offset + len(line))
            label = re.match(r"(?:-\s+)?[^:：\n]+[:：]\s*", line)
            label_end = label.end() if label else 0
            fields.append((context_unit(slice_unit(field, 0, label_end)), slice_unit(field, label_end, len(line))))
            offset += len(line)
    for label, value in fields:

        def render(value: StructureUnit) -> StructureUnit:
            return join_units([label, value], "")

        def fits(value: StructureUnit) -> bool:
            return fits_chunk(_render(prefix, [render(value)], separator).content, limit)

        if not fits(StructureUnit("x")):
            raise _prefix_error()
        pending: list[StructureUnit] = []
        for piece in _split_ranges(value, separators, fits):
            if pending and not fits(join_units([*pending, piece], "")):
                yield replace(render(join_units(pending, "")), kind="fields")
                pending = []
            pending.append(piece)
        if pending:
            yield replace(render(join_units(pending, "")), kind="fields")


def _append_piece(pending: list[StructureUnit] | tuple[StructureUnit, ...], piece: StructureUnit, *, continuation: bool) -> list[StructureUnit]:
    if continuation and pending:
        return [*pending[:-1], replace(join_units([pending[-1], piece], ""), kind="text_fragment")]
    return [*pending, piece]


def _pack_group(prefix: StructureUnit, units: list[StructureUnit], separator: str, *, limit: int, overlap: int, user_separator: str) -> Iterator[StructureUnit]:
    if prefix.content and not fits_chunk(_render(prefix, [StructureUnit("x")], separator).content, limit):
        raise _prefix_error()
    if not units:
        if prefix.content:
            yield trim_unit(prefix)
        return
    separators = [user_separator] + [candidate for candidate in _FALLBACK_SEPARATORS if candidate != user_separator]
    pending: list[StructureUnit] = []
    fresh = False
    for unit in units:

        def fits(value: StructureUnit) -> bool:
            return fits_chunk(_render(prefix, [value], separator).content, limit)

        fragmented = not fits(unit)
        if not fragmented:
            pieces = [unit]
        elif unit.kind == "heading":
            raise _prefix_error()
        elif unit.kind in {"code", "indented_code"}:
            pieces = _code_pieces(unit, prefix, limit, separator)
        elif unit.kind in {"table_row", "fields"}:
            pieces = _field_pieces(unit, prefix, limit, separator, separators)
        else:
            pieces = _split_ranges(unit, separators, fits)
        # Ordinary split fragments remain adjacent; separators between original
        # blocks are added only once, not between every word or Unicode scalar.
        if fragmented and unit.kind not in {"code", "indented_code", "table_row", "fields"}:
            pieces = (replace(piece, kind="text_fragment") for piece in pieces)
        piece_stream = iter(enumerate(pieces))
        while True:
            try:
                piece_index, piece = next(piece_stream)
            except StopIteration:
                break
            continuation = piece.kind == "text_fragment" and piece_index > 0
            candidate = _append_piece(pending, piece, continuation=continuation)
            if pending and not fits_chunk(_render(prefix, candidate, separator).content, limit):
                if not _has_source_text(join_units(pending, separator)) and _has_source_text(piece):
                    # A leading image is still awaiting real text. Subdivide
                    # the incoming source with that image's budget reserved,
                    # then feed the smaller pieces through the same packer.
                    def fits_leading(value: StructureUnit, leading=tuple(pending), context=prefix, continuing=continuation) -> bool:
                        return fits_chunk(_render(context, _append_piece(leading, value, continuation=continuing), separator).content, limit)

                    reserved = _render(prefix, pending, separator)
                    if piece.kind in {"code", "indented_code"}:
                        subdivisions = _code_pieces(piece, reserved, limit, separator)
                        replacements = ((0, part) for part in subdivisions)
                    elif piece.kind in {"table_row", "fields"}:
                        subdivisions = _field_pieces(piece, reserved, limit, separator, separators)
                        replacements = ((0, part) for part in subdivisions)
                    else:
                        subdivisions = _split_ranges(piece, separators, fits_leading)
                        replacements = ((piece_index if index == 0 else 1, replace(part, kind="text_fragment")) for index, part in enumerate(subdivisions))
                    piece_stream = chain(replacements, piece_stream)
                    continue
                if not _has_source_text(piece) and inline_atoms(piece.content):
                    # Keep a trailing image with actual source text. Move a
                    # suffix from the previous paragraph instead of dropping
                    # the occurrence or emitting an alt-only index chunk.
                    images = [piece]
                    while pending and not _has_source_text(pending[-1]):
                        images.insert(0, pending.pop())
                    if not pending:
                        raise ExtractionError("ATOMIC_CONTENT_EXCEEDS_BUDGET", "图片与正文无法同时放入分段预算")
                    last = pending.pop()

                    def fits_tail(value: StructureUnit) -> bool:
                        return fits_chunk(_render(prefix, [value, *images], separator).content, limit)

                    tail = []
                    part_separator = "\n\n" if last.kind in {"code", "indented_code"} else ""
                    parts = list(_code_pieces(last, prefix, limit, separator, following=tuple(images))) if part_separator else list(_split_ranges(last, separators, fits_tail))
                    while parts and fits_tail(join_units([parts[-1], *tail], part_separator)):
                        tail.insert(0, parts.pop())
                    if not tail:
                        raise _prefix_error()
                    if parts:
                        pending.append(join_units(parts, part_separator))
                    if pending:
                        yield trim_unit(_render(prefix, pending, separator))
                        prefix = context_unit(prefix)
                    pending = [join_units(tail, part_separator), *images]
                    fresh = True
                    continue
                if fresh:
                    yield trim_unit(_render(prefix, pending, separator))
                prefix = context_unit(prefix)
                retained = []
                if overlap and all(value.kind in {"paragraph", "markdown", "list"} and not value.attachments for value in pending) and piece.kind not in {"table_row", "fields", "code"}:
                    for value in reversed(pending):
                        tail = [value, *retained]
                        if count_knowledge_tokens(join_units(tail, separator).content) > overlap:
                            break
                        retained = tail
                pending = retained
                while pending and not fits_chunk(_render(prefix, [*pending, piece], separator).content, limit):
                    pending.pop(0)
                fresh = False
                candidate = [*pending, piece]
            pending = candidate
            fresh = True
    if pending and fresh:
        yield trim_unit(_render(prefix, pending, separator))


def _has_source_text(unit: StructureUnit) -> bool:
    return has_indexable_source_text((Document(page_content=unit.content, source_spans=unit.source_spans),))


def _split_token_units(documents: tuple[Document, ...], *, limit: int, overlap: int, separator: str) -> Iterator[StructureUnit]:
    for prefix, units, joiner in structure_groups(documents):
        yield from _pack_group(prefix, units, joiner, limit=limit, overlap=overlap, user_separator=decode_separator(separator))


def _check_hard_limit(count: int) -> None:
    if count > KNOWLEDGE_MAX_SEGMENTS_PER_DOCUMENT:
        raise KnowledgeError(KNOWLEDGE_QUOTA_EXCEEDED, "分段或向量条目超过知识库固定上限")


def split_documents(documents: tuple[Document, ...], *, profile: ChunkProfile) -> list[SegmentDraft]:
    """Derive immutable display/index drafts without extraction, I/O or fallback."""
    from .cleaner import clean_documents

    if profile.unit == "character":
        return _split_character_documents(documents, profile)
    if not 200 <= profile.size <= 4000 or not 0 <= profile.overlap <= 500 or profile.overlap >= profile.size or (profile.mode == "parent_child" and (not 100 <= profile.child_size <= 2000 or profile.child_size >= profile.size)):
        raise ValueError("invalid Knowledge token chunk limits")
    if profile.tokenizer_profile_id != TOKENIZER_PROFILE_ID or profile.tokenizer_digest != tokenizer_fingerprint():
        raise ExtractionError("TOKENIZER_UNAVAILABLE", "知识库 Tokenizer 配置不可用")
    cleaned = clean_documents(documents, remove_extra_spaces=profile.remove_extra_spaces, remove_urls_emails=profile.remove_urls_emails)
    result = []
    vector_count = 0
    for unit in _split_token_units(cleaned, limit=profile.size, overlap=profile.overlap, separator=profile.separator):
        document = Document(page_content=unit.content, source_spans=unit.source_spans, attachments=unit.attachments)
        if not has_indexable_source_text((document,)):
            continue
        _check_hard_limit(len(result) + 1)
        index_text = build_index_text(unit.content)
        children = []
        if profile.mode == "parent_child":
            for child in _split_token_units((document,), limit=profile.child_size, overlap=0, separator=profile.child_separator):
                if not has_indexable_source_text((Document(page_content=child.content, source_spans=child.source_spans),)):
                    continue
                vector_count += 1
                _check_hard_limit(vector_count)
                child_index = build_index_text(child.content)
                children.append(ChildDraft(child.content, child_index, count_knowledge_tokens(child_index), child.source_spans))
        result.append(
            SegmentDraft(
                position=len(result) + 1,
                content=unit.content,
                index_text=index_text,
                token_count=count_knowledge_tokens(index_text),
                source_position=dict(next((s.location for s in unit.source_spans if s.role == "source"), unit.source_spans[0].location if unit.source_spans else {})),
                source_spans=unit.source_spans,
                attachments=unit.attachments,
                children=tuple(children),
            )
        )
    return result


def _split_character_documents(documents: tuple[Document, ...], profile: ChunkProfile) -> list[SegmentDraft]:
    """The frozen character profile retains the original recursive algorithm."""
    from .cleaner import clean_character_document

    result = []
    separators = [decode_separator(profile.separator)] + [candidate for candidate in _FALLBACK_SEPARATORS if candidate != decode_separator(profile.separator)]
    for document in documents:
        # The legacy cleaner deliberately preserves its original byte behavior.
        document = clean_character_document(document, remove_extra_spaces=profile.remove_extra_spaces, remove_urls_emails=profile.remove_urls_emails)
        text = document.page_content
        cursor = 0
        for content in _recursive_chunks(text, separators, chunk_size=profile.size, chunk_overlap=profile.overlap):
            start = text.find(content, max(0, cursor - profile.overlap))
            cursor = start + len(content)
            _check_hard_limit(len(result) + 1)
            spans = clip_source_spans(document.source_spans, start, cursor)
            index_text = build_index_text(content)
            result.append(
                SegmentDraft(
                    position=len(result) + 1,
                    content=content,
                    index_text=index_text,
                    token_count=count_knowledge_tokens(index_text),
                    source_position=dict(document.source_spans[0].location) if document.source_spans else {},
                    source_spans=spans,
                )
            )
    if profile.mode == "parent_child":
        vector_count = 0
        for index, draft in enumerate(result):
            attached = attach_children([draft], child_chunk_size=profile.child_size, child_chunk_separator=profile.child_separator)[0]
            vector_count += len(attached.children)
            _check_hard_limit(vector_count)
            result[index] = attached
    return result
