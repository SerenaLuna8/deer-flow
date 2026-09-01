"""Conservative normalization with explicit old-to-new character intervals.

Markdown/MDX is data. No renderer, template engine or HTML stripping runs here.
Format adapters produce canonical text before attaching source positions.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass

from .contracts import Document, ExtractionError, ParseWarning, SourceSpan
from .markdown_images import MarkdownImagePositionError, find_markdown_images, markdown_references


def external_image_placeholder(alt_text: str) -> str:
    """Keep the visible label without letting it introduce new Markdown images."""
    alt = re.sub(r"([\\`*_\[\]!])", r"\\\1", alt_text.replace("\n", " "))
    return f"（外部图片未获取：{alt}）" if alt else "（外部图片未获取）"


@dataclass(frozen=True)
class _Edit:
    start: int
    end: int
    text: str
    synthetic: bool = False


def _rewrite(document: Document, edits: list[_Edit]) -> Document:
    if not edits:
        return document
    edits.sort(key=lambda edit: edit.start)
    starts = [edit.start for edit in edits]
    ends = [edit.end for edit in edits]
    shifts = [0]
    parts = []
    cursor = 0
    for edit in edits:
        parts.extend((document.page_content[cursor : edit.start], edit.text))
        cursor = edit.end
        shifts.append(shifts[-1] + len(edit.text) - (edit.end - edit.start))
    parts.append(document.page_content[cursor:])

    def mapped(position: int, *, end: bool = False) -> int:
        index = bisect_right(ends, position)
        if index < len(edits) and edits[index].start < position < edits[index].end:
            return edits[index].start + shifts[index] + (len(edits[index].text) if end else 0)
        return position + shifts[index]

    spans = []
    replacements: dict[int, SourceSpan] = {}
    for span in document.source_spans:
        cursor = span.start
        first = bisect_right(ends, span.start)
        last = bisect_left(starts, span.end)
        for index in range(first, last):
            edit = edits[index]
            if not edit.synthetic:
                continue
            if cursor < edit.start:
                spans.append(span.model_copy(update={"start": mapped(cursor), "end": mapped(edit.start)}))
            if index not in replacements:
                replacements[index] = SourceSpan(block_id=span.block_id, start=mapped(edit.start), end=mapped(edit.end), location=span.location, role="context_prefix")
                spans.append(replacements[index])
            cursor = min(span.end, edit.end)
        if cursor < span.end or span.start == span.end:
            spans.append(span.model_copy(update={"start": mapped(cursor), "end": mapped(span.end, end=True)}))
    for index, edit in enumerate(edits):
        if edit.synthetic and index not in replacements:
            span = SourceSpan(block_id="image:unlocated", start=mapped(edit.start), end=mapped(edit.end), role="context_prefix")
            replacements[index] = span
            spans.append(span)
    warnings = list(document.warnings)
    warnings.extend(ParseWarning(code="EXTERNAL_IMAGE_NOT_FETCHED", message="外部图片未获取", source_position=replacements[index].location) for index, edit in enumerate(edits) if edit.synthetic)
    attachments = tuple(occurrence.model_copy(update={"source": occurrence.source.model_copy(update={"start": mapped(occurrence.source.start), "end": mapped(occurrence.source.end, end=True)})}) for occurrence in document.attachments)
    return Document(page_content="".join(parts), source_spans=tuple(spans), heading_path=document.heading_path, kind=document.kind, attachments=attachments, warnings=tuple(warnings))


def _image_edits(document: Document, references: dict) -> list[_Edit]:
    owned = {(item.source.start, item.source.end, f"knowledge-attachment:{item.ref}") for item in document.attachments}
    try:
        images = find_markdown_images(document.page_content, references=references)
    except MarkdownImagePositionError:
        raise ExtractionError("MARKDOWN_SOURCE_POSITION_FAILED") from None
    return [_Edit(image.start, image.end, external_image_placeholder(image.alt_text), synthetic=True) for image in images if (image.start, image.end, image.source) not in owned]


def normalize_documents(documents: list[Document]) -> list[Document]:
    """Preserve unchanged bytes/spans; remap newlines and external image edits.

    Logical image syntax is retained only for an actual AttachmentOccurrence,
    including separate occurrences of the same ref. Raw HTML/MDX stays literal;
    safe UI rendering is a separate host responsibility.
    """
    normalized = [_rewrite(document, [_Edit(match.start(), match.end(), "\n") for match in re.finditer(r"\r\n?", document.page_content)]) for document in documents]
    if not any("![" in document.page_content and document.kind != "text" for document in normalized):
        return normalized
    references = markdown_references("\n\n".join(document.page_content for document in normalized))
    return [_rewrite(document, _image_edits(document, references)) if document.kind != "text" and "![" in document.page_content else document for document in normalized]
