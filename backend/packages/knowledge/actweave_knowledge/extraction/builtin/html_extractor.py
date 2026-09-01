"""HTML loader adapted from upstream 9c16c865977e9d89a9ec7ae0536e893f4385a758.

Keeps the binary-file -> BeautifulSoup flow. Structural Markdown replaces the
upstream soup.get_text()/strip so headings, lists, tables and code survive.
See ../UPSTREAM.md and ../patches.md for provenance and local corrections.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import override
from urllib.parse import quote, urlsplit

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from ..base import BaseExtractor
from ..contracts import Document, ExtractionContext, ExtractionError, ExtractSetting, ParseWarning, SourceSpan
from ..encoding import read_source_bytes
from ..normalizer import external_image_placeholder

_ACTIVE = {"script", "style", "iframe", "object", "embed", "template", "noscript", "svg", "math"}
_BLOCKS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table", "pre", "blockquote", "hr"}
_CONTAINERS = {"html", "body", "main", "article", "section", "div", "header", "footer", "nav", "aside"}
Parts = list[tuple[str, bool]]


def _literal(text: str) -> str:
    return re.sub(r"([\\`*_\[\]!])", r"\\\1", re.sub(r"\s+", " ", text))


def _safe_link(href: str) -> str | None:
    if any(ord(char) <= 32 or ord(char) == 127 for char in href):
        return None
    try:
        parsed = urlsplit(href)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https", "mailto"}:
        return None
    if parsed.scheme.lower() in {"http", "https"} and not parsed.netloc:
        return None
    return quote(href, safe=":/?#[]@!$&'*+,;=%~.-_")


def _inline(node) -> Parts:
    if isinstance(node, Comment):
        return []
    if isinstance(node, NavigableString):
        return [(_literal(str(node)), False)]
    if not isinstance(node, Tag) or node.name in _ACTIVE:
        return []
    if node.name == "img":
        return [(external_image_placeholder(str(node.get("alt", ""))), True)]
    if node.name == "br":
        return [("\n", False)]
    if node.name == "code":
        text = node.get_text().replace("\r\n", "\n").replace("\r", "\n")
        fence = "`" * max(1, 1 + max((len(run) for run in re.findall(r"`+", text)), default=0))
        pad = " " if text.startswith("`") or text.endswith("`") else ""
        return [(f"{fence}{pad}{text}{pad}{fence}", False)]
    # Inline edge spaces belong to adjacent siblings. Only actual block
    # descendants need the trimming/separation performed by block flow.
    if any(_structural(child) for child in node.children):
        parts = _flow_parts(node)
    else:
        parts = [part for child in node.children for part in _inline(child)]
    if node.name == "a":
        target = _safe_link(str(node.get("href", "")))
        return [("[", False), *parts, (f"](<{target}>)", False)] if target else parts
    if node.name in {"strong", "b", "em", "i"}:
        marker = "**" if node.name in {"strong", "b"} else "*"
        return [(marker, False), *parts, (marker, False)]
    return parts


def _trim(parts: Parts) -> Parts:
    # Trim only HTML layout whitespace, before offsets are assigned.
    while parts and not parts[0][0].strip():
        parts.pop(0)
    while parts and not parts[-1][0].strip():
        parts.pop()
    if parts:
        parts[0] = (parts[0][0].lstrip(), parts[0][1])
        parts[-1] = (parts[-1][0].rstrip(), parts[-1][1])
    return parts


def _structural(node) -> bool:
    return isinstance(node, Tag) and (node.name in _BLOCKS or node.name in _CONTAINERS or node.find(_BLOCKS) is not None)


def _flow_chunks(container):
    pending: Parts = []
    for child in container.children:
        if _structural(child):
            pending = _trim(pending)
            if pending:
                yield "paragraph", pending
            pending = []
            parts = _block_parts(child)
            if parts:
                yield child.name, parts
        else:
            pending.extend(_inline(child))
    pending = _trim(pending)
    if pending:
        yield "paragraph", pending


def _flow_parts(container) -> Parts:
    parts: Parts = []
    for kind, chunk in _flow_chunks(container):
        if parts:
            parts.append(("\n" if kind in {"ol", "ul"} else "\n\n", False))
        parts.extend(chunk)
    return parts


def _prefix_lines(parts: Parts, first: str, rest: str) -> Parts:
    """Indent rendered blocks without marking prefixes as image source text."""
    lines: list[Parts] = [[]]
    for value, synthetic in parts:
        for index, line in enumerate(value.split("\n")):
            if index:
                lines.append([])
            if line:
                lines[-1].append((line, synthetic))
    output: Parts = []
    for index, line in enumerate(lines):
        if index:
            output.append(("\n", False))
        prefix = rest if index else first
        if not line and prefix.startswith(">"):
            prefix = prefix.rstrip()
        output.append((prefix, False))
        output.extend(line)
    return output


def _block_parts(node: Tag) -> Parts:
    if node.name == "pre":
        text = node.get_text().replace("\r\n", "\n").replace("\r", "\n")
        fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", text)), default=0))
        return [(f"{fence}\n{text}" + ("" if text.endswith("\n") else "\n") + fence, False)]
    if node.name in {"ol", "ul"}:
        return _list(node)
    if node.name == "table":
        return _table(node)
    if node.name == "blockquote":
        return _prefix_lines(_flow_parts(node), "> ", "> ")
    if node.name == "hr":
        return [("---", False)]
    if re.fullmatch(r"h[1-6]", node.name):
        return [("#" * int(node.name[1]) + " ", False), *_flow_parts(node)]
    return _flow_parts(node)


def _list(node: Tag) -> Parts:
    parts: Parts = []
    try:
        number = int(node.get("start", 1))
    except (ValueError, TypeError):
        number = 1
    for item in node.find_all("li", recursive=False):
        if parts:
            parts.append(("\n", False))
        marker = f"{number}. " if node.name == "ol" else "- "
        parts.extend(_prefix_lines(_flow_parts(item), marker, " " * len(marker)))
        number += 1
    return parts


def _table(node: Tag) -> Parts:
    parts: Parts = []
    rows = [row for row in node.find_all("tr") if row.find_parent("table") is node]
    for index, row in enumerate(rows):
        cells = row.find_all(["th", "td"], recursive=False)
        if not cells:
            continue
        if parts:
            parts.append(("\n", False))
        parts.append(("| ", False))
        for position, cell in enumerate(cells):
            if position:
                parts.append((" | ", False))
            parts.extend((text.replace("|", "\\|").replace("\n", " "), synthetic) for text, synthetic in _trim(_inline(cell)))
        parts.append((" |", False))
        if index == 0:
            parts.append(("\n| " + " | ".join("---" for _ in cells) + " |", False))
    return parts


def html_to_documents(markup: bytes | str) -> list[Document]:
    """Use HTML's own encoding declarations; never fetch linked resources."""
    soup = BeautifulSoup(markup, "html.parser")
    if soup.contains_replacement_characters:
        raise ExtractionError("TEXT_DECODING_FAILED")
    for node in soup.find_all(_ACTIVE):
        node.decompose()
    for node in soup.find_all(True):
        for attr in list(node.attrs):
            if attr.lower().startswith("on"):
                del node.attrs[attr]
    for head in soup.find_all("head"):
        head.decompose()
    documents: list[Document] = []
    headings: list[tuple[int, str]] = []

    def emit(parts: Parts, kind: str):
        parts = _trim(parts)
        if not parts:
            return
        number = len(documents) + 1
        location = {"element": number}
        spans = []
        warnings = []
        text = ""
        for value, synthetic in parts:
            if not value:
                continue
            role = "context_prefix" if synthetic else "source"
            # Coalesce ordinary parts into a single block, without concealing a
            # synthetic image placeholder inside a source interval.
            if spans and spans[-1].role == role and not synthetic:
                spans[-1] = spans[-1].model_copy(update={"end": len(text) + len(value)})
            else:
                spans.append(SourceSpan(block_id=f"html:{number}", start=len(text), end=len(text) + len(value), location=location, role=role))
            text += value
            if synthetic:
                warnings.append(ParseWarning(code="EXTERNAL_IMAGE_NOT_FETCHED", message="外部图片未获取", source_position=location))
        documents.append(Document(page_content=text, source_spans=tuple(spans), heading_path=tuple(title for _, title in headings), kind=kind, warnings=tuple(warnings)))

    def visit(container):
        pending: Parts = []
        for node in container.children:
            if _structural(node):
                emit(pending, "paragraph")
                pending = []
                if node.name not in _BLOCKS:
                    visit(node)
                elif re.fullmatch(r"h[1-6]", node.name):
                    level = int(node.name[1])
                    title = node.get_text().strip()
                    while headings and headings[-1][0] >= level:
                        headings.pop()
                    headings.append((level, title))
                    emit([("#" * level + " ", False), *_inline(node)], "heading")
                elif node.name in {"ol", "ul"}:
                    emit(_list(node), "list")
                elif node.name == "table":
                    emit(_table(node), "table")
                elif node.name in {"pre", "hr", "blockquote"}:
                    kind = {"pre": "code", "hr": "separator", "blockquote": "blockquote"}[node.name]
                    emit(_block_parts(node), kind)
                else:
                    emit(_inline(node), "paragraph")
            else:
                pending.extend(_inline(node))
        emit(pending, "paragraph")

    visit(soup.body or soup)
    return documents


class HtmlExtractor(BaseExtractor):
    """Load HTML from bytes with the original BeautifulSoup adapter flow."""

    @override
    def extract(self, setting: ExtractSetting, context: ExtractionContext) -> list[Document]:
        context.check_cancelled()
        documents = self._load_as_text(setting.source_path)
        context.check_cancelled()
        return documents

    def _load_as_text(self, file_path: Path) -> list[Document]:
        return html_to_documents(read_source_bytes(file_path))
