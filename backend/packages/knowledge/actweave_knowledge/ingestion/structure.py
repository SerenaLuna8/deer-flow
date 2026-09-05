"""Small Markdown structure units, not a second document-format parser."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, replace
from html.entities import html5
from string import punctuation

from markdown_it import MarkdownIt

from actweave_knowledge.extraction.contracts import AttachmentOccurrence, Document, SourceSpan

from .index_text import build_index_text
from .source_mapping import clip_attachments, clip_source_spans, edit_document, shift_source_spans

_MARKDOWN_ENTITY = re.compile(r"&(?:#[0-9]{1,7}|#[xX][0-9A-Fa-f]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")
_FRAGMENT_BLOCK_MARKER = re.compile(r"[ \t\n]*(?P<marker>[-+*](?=[ \t\n]|$)|[0-9]{1,9}[.)](?=[ \t\n]|$)|#{1,6}(?=[ \t\n]|$)|>|(?:[-*_][ \t]*){3,}(?=\n|$)|~{3,})")
_FRAGMENT_INDENT = re.compile(r" {4}| {0,3}\t")


@dataclass(frozen=True, slots=True)
class StructureUnit:
    content: str
    source_spans: tuple[SourceSpan, ...] = ()
    heading_path: tuple[str, ...] = ()
    kind: str = "paragraph"
    attachments: tuple[AttachmentOccurrence, ...] = ()
    preserve_leading: bool = False


def slice_unit(unit: StructureUnit, start: int, end: int) -> StructureUnit:
    return replace(unit, content=unit.content[start:end], source_spans=clip_source_spans(unit.source_spans, start, end), attachments=clip_attachments(unit.attachments, start, end))


def slice_fragment(unit: StructureUnit, start: int, end: int) -> StructureUnit:
    """Keep an ordinary slice's new block start literal before measuring it."""
    fragment = slice_unit(unit, start, end)
    if not start or not fragment.content or unit.kind not in {"paragraph", "markdown", "page", "text_fragment"}:
        return fragment
    edits = []
    if _FRAGMENT_INDENT.match(unit.content, start):
        edits.append((0, 1, "&#9;" if fragment.content[0] == "\t" else "&#32;"))
    if match := _FRAGMENT_BLOCK_MARKER.match(unit.content, start):
        marker = match["marker"]
        offset = match.start("marker") - start + (len(marker) - 1 if marker[0].isdigit() else 0)
        if offset < len(fragment.content):
            edits.append((offset, offset, "\\"))
    if not edits:
        return fragment
    protected = edit_document(Document(page_content=fragment.content, source_spans=fragment.source_spans, attachments=fragment.attachments), edits)
    return replace(fragment, content=protected.page_content, source_spans=protected.source_spans, attachments=protected.attachments)


def trim_unit(unit: StructureUnit) -> StructureUnit:
    start = 0 if unit.preserve_leading or unit.kind in {"code", "indented_code"} else len(unit.content) - len(unit.content.lstrip())
    return slice_unit(unit, start, len(unit.content.rstrip()))


def join_units(units: list[StructureUnit] | tuple[StructureUnit, ...], separator: str = "\n\n") -> StructureUnit:
    parts, spans, attachments = [], [], []
    offset = 0
    for unit in units:
        if not unit.content:
            continue
        if parts:
            parts.append(separator)
            offset += len(separator)
        parts.append(unit.content)
        for span in shift_source_spans(unit.source_spans, offset):
            if spans and spans[-1].end == span.start and (spans[-1].block_id, spans[-1].location, spans[-1].role) == (span.block_id, span.location, span.role):
                spans[-1] = spans[-1].model_copy(update={"end": span.end})
            else:
                spans.append(span)
        attachments.extend(item.model_copy(update={"source": shift_source_spans((item.source,), offset)[0]}) for item in unit.attachments)
        offset += len(unit.content)
    nonempty = [unit for unit in units if unit.content]
    kind = nonempty[0].kind if len(nonempty) == 1 else "paragraph"
    preserve_leading = bool(nonempty and (nonempty[0].preserve_leading or nonempty[0].kind in {"code", "indented_code"}))
    return StructureUnit("".join(parts), tuple(spans), kind=kind, attachments=tuple(attachments), preserve_leading=preserve_leading)


def context_unit(unit: StructureUnit) -> StructureUnit:
    return replace(unit, source_spans=shift_source_spans(unit.source_spans, 0, context=True), attachments=())


def inline_atoms(text: str, *, include_text_escapes: bool = False) -> list[tuple[int, int]]:
    """Protect code spans, links and images, including balanced destinations."""
    ranges = []
    index = 0
    while index < len(text):
        if text[index] == "\\" and index + 1 < len(text) and text[index + 1] in punctuation:
            if include_text_escapes:
                ranges.append((index, index + 2))
            index += 2
            continue
        if include_text_escapes and text[index] == "&":
            entity = _MARKDOWN_ENTITY.match(text, index)
            if entity and (entity[0].startswith("&#") or entity[0][1:] in html5):
                ranges.append((index, entity.end()))
                index = entity.end()
                continue
        if text[index] == "`":
            stop = index + 1
            while stop < len(text) and text[stop] == "`":
                stop += 1
            close = text.find(text[index:stop], stop)
            if close >= 0:
                ranges.append((index, close + stop - index))
                index = ranges[-1][1]
                continue
        opening = index + 1 if text.startswith("![", index) else index
        if text[opening : opening + 1] == "[":
            label_end = text.find("](", opening + 1)
            if label_end >= 0:
                depth, cursor = 1, label_end + 2
                while cursor < len(text) and depth:
                    if text[cursor] == "\\":
                        cursor += 2
                        continue
                    if text[cursor] == "(":
                        depth += 1
                    elif text[cursor] == ")":
                        depth -= 1
                    cursor += 1
                if not depth:
                    ranges.append((index, cursor))
                    index = cursor
                    continue
        index += 1
    return ranges


def block_units(document: Document) -> list[StructureUnit]:
    """Map Markdown parser line ranges back to the exact normalized source."""
    full = StructureUnit(document.page_content, document.source_spans, document.heading_path, document.kind, document.attachments)
    lines = document.page_content.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    tokens = MarkdownIt("commonmark", {"html": False}).enable("table").parse(document.page_content)
    result = []
    consumed = 0
    for token in tokens:
        if token.map is None or token.level != 0 or token.type == "inline":
            continue
        start, end = (offsets[line] for line in token.map)
        if start < consumed:
            continue
        if start > consumed and document.page_content[consumed:start].strip():
            result.append(trim_unit(slice_unit(full, consumed, start)))
        kind = {"heading_open": "heading", "table_open": "table", "fence": "code", "code_block": "indented_code", "bullet_list_open": "list", "ordered_list_open": "list"}.get(token.type, document.kind)
        if token.type == "paragraph_open" and document.kind not in {"table_header", "table_row", "fields"}:
            kind = "paragraph"
        unit = trim_unit(replace(slice_unit(full, start, end), kind=kind))
        if kind == "list" and any(span.role == "context_prefix" for span in unit.source_spans) and re.match(r"-\s+[^:：\n]+[:：]", unit.content):
            unit = replace(unit, kind="fields")
        result.append(unit)
        consumed = end
    if document.page_content[consumed:].strip():
        result.append(trim_unit(slice_unit(full, consumed, len(full.content))))
    return result


def table_identity(document: Document) -> tuple[object, ...]:
    location = next((span.location for span in document.source_spans if "table" in span.location or "sheet" in span.location), {})
    return (location.get("table"), location.get("table_path"), location.get("sheet"))


def _heading_level(unit: StructureUnit) -> int:
    match = re.match(r"^(#{1,6})\s", unit.content)
    if match:
        return len(match[1])
    return 1 if unit.content.splitlines()[-1].startswith("=") else 2


def structure_groups(documents: tuple[Document, ...]) -> Iterator[tuple[StructureUnit, list[StructureUnit], str]]:
    """Only neighboring ordinary blocks in the same section/page may merge."""
    headings: dict[tuple[str, ...], StructureUnit] = {}
    active_path: tuple[str, ...] = ()
    heading_stack: list[tuple[int, str]] = []
    pending: list[StructureUnit] = []
    prefix = StructureUnit("")
    boundary: tuple[object, ...] | None = None
    table_headers: dict[tuple[object, ...], StructureUnit] = {}
    tables_with_rows = {table_identity(doc) for doc in documents if doc.kind == "table_row"}
    for document in documents:
        if document.kind == "title" and document.heading_path:
            # The adapter's Title stays literal; only its context gets a
            # heading marker, with the original serialized text and source.
            title = StructureUnit(document.page_content, document.source_spans, document.heading_path, "heading", document.attachments)
            units = [replace(join_units([StructureUnit("#" * min(len(document.heading_path), 6) + " "), title], ""), kind="heading")]
        else:
            units = block_units(document)
        page = tuple(sorted({s.location["page"] for s in document.source_spans if "page" in s.location}))
        own_boundary = (document.heading_path, page)
        is_row = document.kind in {"table_header", "table_row"}
        if pending and (is_row or own_boundary != boundary):
            yield prefix, pending, "\n\n"
            pending = []
            prefix = context_unit(prefix)
        boundary = own_boundary
        if document.heading_path != active_path:
            if not pending and any(span.role == "source" for span in prefix.source_spans):
                yield StructureUnit(""), [prefix], "\n\n"
            active_path = document.heading_path
            path_units = [headings.get(active_path[: i + 1], StructureUnit("#" * (i + 1) + " " + title, kind="heading")) for i, title in enumerate(active_path)]
            heading_stack = [(_heading_level(unit), title) for unit, title in zip(path_units, active_path, strict=True)]
            prefix = join_units([context_unit(unit) for unit in path_units])
        if document.kind == "table_header" and document.page_content.lstrip().startswith("|"):
            header = join_units(units, "\n")
            if table_identity(document) in tables_with_rows:
                table_headers[table_identity(document)] = header
            else:
                yield join_units([prefix, header]), [], "\n"
                prefix = context_unit(prefix)
            continue
        if document.kind == "table_row":
            header = table_headers.get(table_identity(document)) if document.page_content.lstrip().startswith("|") else None
            full = StructureUnit(document.page_content, document.source_spans, document.heading_path, "table_row" if header else "fields", document.attachments)
            yield join_units([context_unit(prefix), header] if header else [context_unit(prefix)]), [full], "\n" if header else "\n\n"
            if header:
                table_headers[table_identity(document)] = context_unit(header)
            continue
        for unit_index, unit in enumerate(units):
            if unit.kind == "heading":
                if pending:
                    yield prefix, pending, "\n\n"
                    pending = []
                elif any(s.role == "source" for s in prefix.source_spans):
                    yield StructureUnit(""), [prefix], "\n\n"
                match = re.match(r"^(#{1,6})\s+(.*?)(?:\s+#+)?$", unit.content)
                title = match[2] if match else unit.content.splitlines()[0]
                if unit_index == 0 and document.heading_path:
                    raw_leaf = document.heading_path[-1]
                    if document.kind == "title" or title == raw_leaf or build_index_text(title) == raw_leaf:
                        title = raw_leaf
                level = _heading_level(unit)
                # P1 paths contain titles only, with no gaps for skipped hN
                # levels. The first heading is already the section path's leaf.
                if unit_index == 0 and document.heading_path and title == document.heading_path[-1]:
                    heading_stack = heading_stack[:-1]
                while heading_stack and heading_stack[-1][0] >= level:
                    heading_stack.pop()
                heading_stack.append((level, title))
                active_path = tuple(title for _, title in heading_stack)
                headings[active_path] = unit
                prefix = join_units([context_unit(headings[path]) for i in range(len(active_path) - 1) if (path := active_path[: i + 1]) in headings] + [unit])
            elif unit.kind == "table":
                if pending:
                    yield prefix, pending, "\n\n"
                    pending = []
                    prefix = context_unit(prefix)
                lines = unit.content.splitlines(keepends=True)
                header_end = sum(map(len, lines[:2]))
                header = trim_unit(slice_unit(unit, 0, header_end))
                rows = []
                offset = header_end
                for line in lines[2:]:
                    rows.append(replace(trim_unit(slice_unit(unit, offset, offset + len(line))), kind="table_row"))
                    offset += len(line)
                table_prefix = join_units([prefix, header])
                if rows:
                    yield table_prefix, rows, "\n"
                else:
                    yield table_prefix, [], "\n"
                prefix = context_unit(prefix)
            else:
                pending.append(unit)
        if is_row and pending:
            yield prefix, pending, "\n\n"
            pending = []
    if pending:
        yield prefix, pending, "\n\n"
    elif prefix.content and any(s.role == "source" for s in prefix.source_spans):
        yield StructureUnit(""), [prefix], "\n\n"
