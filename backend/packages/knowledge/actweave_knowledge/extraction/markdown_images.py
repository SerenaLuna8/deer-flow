"""Locate inert Markdown image syntax with original character intervals.

Shared by normalization and manifest closure checks. This is a source locator,
not a renderer or executable MDX parser. Balanced MDX expressions and tag
attributes remain literal; CommonMark supplies paragraph/code/HTML boundaries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from markdown_it import MarkdownIt
from markdown_it.rules_inline import backtick as backtick_rule
from markdown_it.rules_inline import image as image_rule


class MarkdownImagePositionError(ValueError):
    """Source mapping failed; callers must fail closed, never drop images."""


@dataclass(frozen=True)
class MarkdownImage:
    source: str
    alt_text: str
    start: int
    end: int


def _parser() -> MarkdownIt:
    parser = MarkdownIt("commonmark")
    # Recognize unsafe destinations too, so consumers can reject/replace them.
    # Nothing is rendered, executed, or fetched.
    parser.validateLink = lambda url: True
    return parser


def markdown_references(content: str) -> dict:
    environment: dict = {}
    _parser().parse(content, environment)
    return environment.get("references", {})


_TAG_START = re.compile(r"</?[A-Za-z][\w.:-]*(?=[\s/>])")


def _literal_mask(content: str, protected: bytearray) -> bytearray:
    """Conservatively protect balanced expressions and quoted JSX attributes.

    Only lexical boundaries (braces, quotes, escapes and comments) matter here;
    no JS/MDX AST, imports, template expansion or evaluation is used.
    """
    mask = bytearray(len(content))
    cursor = 0
    while cursor < len(content):
        if protected[cursor]:
            cursor += 1
            continue
        if content[cursor] == "\\":
            cursor += 2
            continue
        is_expression = content[cursor] == "{"
        is_tag = content[cursor] == "<" and _TAG_START.match(content, cursor) is not None
        if not is_expression and not is_tag:
            cursor += 1
            continue
        end = cursor + 1
        depth = 1 if is_expression else 0
        quote = None
        while end < len(content):
            char = content[end]
            if quote:
                if char == "\\":
                    end += 2
                    continue
                if char == quote:
                    quote = None
            elif char in {'"', "'", "`"}:
                quote = char
            elif content.startswith("/*", end):
                closing = content.find("*/", end + 2)
                if closing < 0:
                    break
                end = closing + 2
                continue
            elif depth and content.startswith("//", end):
                newline = content.find("\n", end + 2)
                if newline < 0:
                    break
                end = newline
                continue
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if is_expression and depth == 0:
                    end += 1
                    mask[cursor:end] = b"\x01" * (end - cursor)
                    break
            elif is_tag and char == ">" and depth == 0:
                end += 1
                mask[cursor:end] = b"\x01" * (end - cursor)
                break
            end += 1
        cursor = max(cursor + 1, end)
    return mask


def _inline_positions(content: str, inline: str, start: int, end: int) -> list[int]:
    """Map de-indented inline text back to its original physical source lines."""
    positions: list[int] = []
    cursor = start
    for index, line in enumerate(inline.split("\n")):
        if index:
            positions.append(cursor - 1)  # The preceding physical LF.
        line_end = content.find("\n", cursor, end)
        if line_end < 0:
            line_end = end
        offset = content.find(line, cursor, line_end)
        if offset < 0:
            # CommonMark can expand a partially consumed indentation tab to
            # spaces. Map that leading whitespace to the original tab, while
            # keeping every subsequent content character at its exact offset.
            body = line.lstrip(" \t")
            offset = content.find(body, cursor, line_end)
            if offset < 0 or offset == cursor or not content[cursor:offset].endswith("\t"):
                raise MarkdownImagePositionError("Markdown source position unavailable")
            positions.extend([offset - 1] * (len(line) - len(body)))
            line = body
        positions.extend(range(offset, offset + len(line)))
        cursor = line_end + 1
    return positions


def find_markdown_images(content: str, *, references: dict | None = None) -> tuple[MarkdownImage, ...]:
    if "![" not in content:
        return ()
    parser = _parser()
    environment = {"references": dict(references or {})}
    blocks = parser.parse(content, environment)
    line_offsets = [0, *(index + 1 for index, char in enumerate(content) if char == "\n"), len(content)]
    protected = bytearray(len(content))
    inlines = []
    for block in blocks:
        if block.map is None:
            continue
        start, end = line_offsets[block.map[0]], line_offsets[block.map[1]]
        if block.type in {"html_block", "fence", "code_block"}:
            protected[start:end] = b"\x01" * (end - start)
        if block.type != "inline":
            continue
        positions = _inline_positions(content, block.content, start, end)
        if not positions:
            continue
        source = block.content

        def record_code(state, silent):
            begin = state.pos
            count = len(state.tokens)
            matched = backtick_rule(state, silent)
            if matched and not silent and state.src is source and len(state.tokens) > count and state.tokens[-1].type == "code_inline":
                left, right = positions[begin], positions[state.pos - 1] + 1
                protected[left:right] = b"\x01" * (right - left)
            return matched

        parser.inline.ruler.at("backticks", record_code)
        parser.inline.parse(source, parser, environment, [])
        inlines.append((source, positions))
    parser.inline.ruler.at("backticks", backtick_rule)
    mask = _literal_mask(content, protected)
    images = []
    # Only inline-bearing blocks are scanned: raw HTML, fences and indented code
    # stay inert, and backticks cannot join separate paragraphs.
    for source, positions in inlines:
        if "![" not in source:
            continue
        inline = "".join(" " if mask[position] and char != "\n" else char for char, position in zip(source, positions, strict=True))

        def record_image(state, silent):
            start = state.pos
            root = state.src is inline
            # Masking decides where image syntax may start, never its payload.
            # Parse an actual image from the original equal-length inline text
            # so labels, destinations and bracket boundaries are unmodified.
            if root:
                state.src = source
            try:
                matched = image_rule(state, silent)
            finally:
                if root:
                    state.src = inline
            if matched and not silent and root:
                token = state.tokens[-1]
                images.append(MarkdownImage(source=token.attrGet("src") or "", alt_text=token.content, start=positions[start], end=positions[state.pos - 1] + 1))
            return matched

        parser.inline.ruler.at("image", record_image)
        parser.inline.parse(inline, parser, environment, [])
    return tuple(images)
