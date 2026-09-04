"""Derive deterministic, visible-text-only input for retrieval indexes."""

from __future__ import annotations

import re
from collections.abc import Callable
from functools import lru_cache
from typing import override

from markdown_it import MarkdownIt
from markdown_it.rules_block import StateBlock, table
from markdown_it.rules_inline import StateInline, autolink, backtick, emphasis, entity, escape
from markdown_it.token import Token

from actweave_knowledge.extraction.contracts import Document

_BLANK_LINES = re.compile(r"\n{3,}")


@lru_cache(maxsize=1)
def _parser() -> MarkdownIt:
    parser = MarkdownIt("commonmark", {"html": False}).enable("table")
    # Compile all lazy rule chains before sharing the parser between threads.
    for ruler in (parser.core.ruler, parser.block.ruler, parser.inline.ruler, parser.inline.ruler2):
        ruler.getRules("")
    return parser


def _inline_text(token: Token, *, include_image_alt: bool) -> str:
    parts: list[str] = []
    for child in token.children or ():
        if child.type in {"text", "code_inline", "html_inline"}:
            parts.append(child.content)
        elif child.type in {"softbreak", "hardbreak"}:
            parts.append("\n")
        elif child.type == "image" and include_image_alt:
            parts.append(_inline_text(child, include_image_alt=True) or child.content)
    return "".join(parts)


def _visible_blocks(markdown: str, *, include_image_alt: bool) -> list[str]:
    blocks: list[str] = []
    for token in _parser().parse(markdown):
        if token.type in {"fence", "code_block"}:
            blocks.append(token.content)
        elif token.type == "inline":
            text = _inline_text(token, include_image_alt=include_image_alt)
            if text:
                blocks.append(text)
    return blocks


def _normalize_blocks(blocks: list[str]) -> str:
    normalized = ("\n".join(block.rstrip() for block in blocks if block.strip())).strip()
    return _BLANK_LINES.sub("\n\n", normalized)


def build_index_text(markdown: str) -> str:
    """Keep visible Markdown text while excluding destinations and control syntax."""

    return _normalize_blocks(_visible_blocks(markdown, include_image_alt=True))


def _line_offsets(markdown: str) -> tuple[int, ...]:
    return (0, *(index + 1 for index, character in enumerate(markdown) if character == "\n"), len(markdown))


def _mapped_range(token: Token, offsets: tuple[int, ...], text_length: int) -> tuple[int, int] | None:
    if token.map is None:
        return None
    start_line, end_line = token.map
    if start_line < 0 or end_line < start_line or end_line >= len(offsets):
        return None
    return offsets[start_line], offsets[end_line]


def _inline_positions(markdown: str, token: Token, offsets: tuple[int, ...]) -> tuple[int, ...] | None:
    """Map an inline token's de-indented source back to original characters."""

    mapped = _mapped_range(token, offsets, len(markdown))
    if mapped is None:
        return None
    start, end = mapped
    cursor = start
    positions: list[int] = []
    for line_number, line in enumerate(token.content.split("\n")):
        if line_number:
            positions.append(cursor - 1)
        line_end = markdown.find("\n", cursor, end)
        if line_end < 0:
            line_end = end
        position = markdown.find(line, cursor, line_end)
        if position < 0:
            body = line.lstrip(" \t")
            position = markdown.find(body, cursor, line_end)
            if position < 0:
                return None
            positions.extend([position] * (len(line) - len(body)))
            line = body
        positions.extend(range(position, position + len(line)))
        cursor = line_end + 1
    return tuple(positions)


def _source_covers(document: Document, positions: tuple[int, ...]) -> bool:
    return any(span.role == "source" and span.start <= position < span.end for position in positions if not document.page_content[position].isspace() for span in document.source_spans)


class _SourceInlineState(StateInline):
    """Keep raw pending-text positions before inline rules decode their input."""

    _pending = ""
    _pending_start = 0

    @property
    def pending(self) -> str:
        return self._pending

    @pending.setter
    def pending(self, value: str) -> None:
        # CommonMark only appends original characters to pending text, or trims
        # its trailing whitespace. Links may move pos before flushing it.
        if value and not self._pending:
            self._pending_start = self.pos
        self._pending = value

    @override
    def pushPending(self) -> Token:
        interval = (self._pending_start, self._pending_start + len(self.pending))
        token = super().pushPending()
        token.meta["source_range"] = interval
        return token


def _record_inline_source(name: str, rule: Callable[[StateInline, bool], bool]) -> Callable[[StateInline, bool], bool]:
    def record(state: StateInline, silent: bool) -> bool:
        start, count = state.pos, len(state.tokens)
        matched = rule(state, silent)
        if matched and not silent and isinstance(state, _SourceInlineState):
            cursor = start
            for child in state.tokens[count:]:
                if "source_range" in child.meta or child.type not in {"text", "text_special", "code_inline"}:
                    continue
                left, right = start, state.pos
                if name == "emphasis":
                    left, right = cursor, cursor + len(child.content)
                    cursor = right
                elif child.type == "code_inline":
                    left += len(child.markup)
                    right -= len(child.markup)
                elif name == "autolink":
                    left += 1
                    right -= 1
                child.meta["source_range"] = (left, right)
        return matched

    return record


def _table_cell_positions(state: StateBlock, line: int) -> list[tuple[int, ...]]:
    """Track the table rule's pipe splitting and de-escaping before inline parsing."""

    start, end = state.bMarks[line] + state.tShift[line], state.eMarks[line]
    while start < end and state.src[start].isspace():
        start += 1
    while end > start and state.src[end - 1].isspace():
        end -= 1
    cells: list[list[int]] = [[]]
    for position in range(start, end):
        if state.src[position] == "|":
            if position > start and state.src[position - 1] == "\\":
                cells[-1].pop()  # escapedSplit consumes the preceding backslash.
            else:
                cells.append([])
                continue
        cells[-1].append(position)
    if not cells[0]:
        cells.pop(0)
    if cells and not cells[-1]:
        cells.pop()
    result = []
    for cell in cells:
        left, right = 0, len(cell)
        while left < right and state.src[cell[left]].isspace():
            left += 1
        while right > left and state.src[cell[right - 1]].isspace():
            right -= 1
        result.append(tuple(cell[left:right]))
    return result


def _record_table_source(state: StateBlock, start_line: int, end_line: int, silent: bool) -> bool:
    count = len(state.tokens)
    matched = table(state, start_line, end_line, silent)
    if matched and not silent:
        current_line = -1
        cells: list[tuple[int, ...]] = []
        column = 0
        for token in state.tokens[count:]:
            if token.type != "inline" or token.map is None:
                continue
            if token.map[0] != current_line:
                current_line = token.map[0]
                cells = _table_cell_positions(state, current_line)
                column = 0
            token.meta["source_positions"] = cells[column] if column < len(cells) else ()
            column += 1
    return matched


@lru_cache(maxsize=1)
def _source_parser() -> MarkdownIt:
    parser = MarkdownIt("commonmark", {"html": False}).enable("table")
    # Parse all block syntax and references first. Inline attribution happens
    # before text merging can discard the fragments' individual source ranges.
    parser.core.ruler.disable(["inline", "text_join"])
    parser.inline.ruler2.disable("fragments_join")
    parser.block.ruler.at("table", _record_table_source, {"alt": ["paragraph", "reference"]})
    for name, rule in (("escape", escape), ("entity", entity), ("backticks", backtick), ("emphasis", emphasis.tokenize), ("autolink", autolink)):
        parser.inline.ruler.at(name, _record_inline_source(name, rule))
    # Source rule mutations invalidate caches; rebuild them before sharing.
    for ruler in (parser.core.ruler, parser.block.ruler, parser.inline.ruler, parser.inline.ruler2):
        ruler.getRules("")
    return parser


def _visible_inline_has_source(document: Document, token: Token, offsets: tuple[int, ...], parser: MarkdownIt, environment: dict) -> bool:
    positions = token.meta.get("source_positions")
    if positions is None:
        positions = _inline_positions(document.page_content, token, offsets)
    if positions is None or len(positions) != len(token.content):
        return False
    state = _SourceInlineState(token.content, parser, environment, [])
    parser.inline.tokenize(state)
    for rule in parser.inline.ruler2.getRules(""):
        rule(state)
    for child in state.tokens:
        if child.type not in {"text", "text_special", "code_inline"} or not child.content.strip():
            continue
        start, end = child.meta["source_range"]
        if _source_covers(document, positions[start:end]):
            return True
    return False


def _code_block_has_source(document: Document, token: Token, offsets: tuple[int, ...]) -> bool:
    if not token.content.strip():
        return False
    mapped = _mapped_range(token, offsets, len(document.page_content))
    if mapped is None:
        return False
    start, end = mapped
    if token.type == "fence":
        start_line = token.map[0] + 1
        if start_line >= len(offsets):
            return False
        start = offsets[start_line]
        closing_line = token.map[1] - 1
        if closing_line >= start_line:
            closing = document.page_content[offsets[closing_line] : offsets[closing_line + 1]].lstrip(" ")
            marker = re.escape(token.markup[0])
            if re.fullmatch(rf"{marker}{{{len(token.markup)},}}[ \t]*\n?", closing):
                end = offsets[closing_line]
    return any(span.role == "source" and span.start < end and start < span.end and document.page_content[max(start, span.start) : min(end, span.end)].strip() for span in document.source_spans)


def has_indexable_source_text(documents: tuple[Document, ...]) -> bool:
    """Whether actual source spans contain visible non-image text.

    Markdown is parsed once per complete Document. Each visible inline text or
    code node is then mapped back to the existing source positions; formatting,
    link destinations, image alt text and context prefixes cannot supply text
    authority by themselves.
    """

    for document in documents:
        parser = _source_parser()
        environment: dict = {}
        offsets = _line_offsets(document.page_content)
        for token in parser.parse(document.page_content, environment):
            if token.type in {"fence", "code_block"} and _code_block_has_source(document, token, offsets):
                return True
            if token.type == "inline" and _visible_inline_has_source(document, token, offsets, parser, environment):
                return True
    return False
