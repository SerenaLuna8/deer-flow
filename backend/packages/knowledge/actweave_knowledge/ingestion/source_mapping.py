"""Character-offset operations shared by cleaning and structural splitting."""

from __future__ import annotations

from actweave_knowledge.extraction.contracts import AttachmentOccurrence, Document, SourceSpan


def clip_source_spans(spans: tuple[SourceSpan, ...], start: int, end: int) -> tuple[SourceSpan, ...]:
    """Intersect with the consumed source interval, rebased to its new origin."""
    return tuple(span.model_copy(update={"start": max(start, span.start) - start, "end": min(end, span.end) - start}) for span in spans if max(start, span.start) < min(end, span.end))


def shift_source_spans(spans: tuple[SourceSpan, ...], offset: int, *, context: bool = False) -> tuple[SourceSpan, ...]:
    return tuple(span.model_copy(update={"start": span.start + offset, "end": span.end + offset, **({"role": "context_prefix"} if context else {})}) for span in spans)


def clip_attachments(attachments: tuple[AttachmentOccurrence, ...], start: int, end: int) -> tuple[AttachmentOccurrence, ...]:
    """Atoms belong only to the chunk containing their complete occurrence."""
    return tuple(item.model_copy(update={"source": shift_source_spans((item.source,), -start)[0]}) for item in attachments if start <= item.source.start < item.source.end <= end)


def edit_document(document: Document, edits: list[tuple[int, int, str]]) -> Document:
    """Apply ordered disjoint edits, retaining only surviving source coverage."""
    if not edits:
        return document
    parts: list[str] = []
    spans: list[SourceSpan] = []
    offset = cursor = 0
    for start, end, replacement in [*edits, (len(document.page_content), len(document.page_content), "")]:
        if start < cursor or end < start:
            raise ValueError("overlapping text edits")
        text = document.page_content[cursor:start]
        parts.append(text)
        spans.extend(shift_source_spans(clip_source_spans(document.source_spans, cursor, start), offset))
        offset += len(text)
        parts.append(replacement)
        if replacement:
            for span in clip_source_spans(document.source_spans, start, end):
                spans.append(span.model_copy(update={"start": offset, "end": offset + len(replacement)}))
        offset += len(replacement)
        cursor = end
    attachments = []
    for item in document.attachments:
        if any(start < item.source.end and end > item.source.start for start, end, _ in edits):
            raise ValueError("cannot edit an attachment occurrence")
        delta = sum(len(value) - (end - start) for start, end, value in edits if end <= item.source.start)
        attachments.append(item.model_copy(update={"source": shift_source_spans((item.source,), delta)[0]}))
    return document.model_copy(update={"page_content": "".join(parts), "source_spans": tuple(spans), "attachments": tuple(attachments)})
