"""Convert actual Unstructured element metadata without invented locations."""

from __future__ import annotations

from ..builtin.html_extractor import html_to_documents
from ..contracts import Document, ParseWarning, SourceSpan


def elements_to_documents(elements, *, kind: str) -> list[Document]:
    documents = []
    headings: list[tuple[int, str]] = []
    for index, element in enumerate(elements, 1):
        text = element.text or ""
        metadata = element.metadata
        page = getattr(metadata, "page_number", None)
        location = {"element": index}
        warnings = ()
        if type(page) is int and page > 0:
            location["slide" if kind == "slide" else "page"] = page
        elif kind == "slide":
            warnings = (ParseWarning(code="SOURCE_POSITION_UNAVAILABLE", message="解析库未提供页码", source_position=location),)
        category = getattr(element, "category", "")
        if category == "Title":
            depth = getattr(metadata, "category_depth", None)
            level = depth if type(depth) is int and depth >= 0 else 0
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, text))
        heading_path = tuple(title for _, title in headings)
        if category == "Table":
            html = getattr(metadata, "text_as_html", None)
            if html:
                converted = html_to_documents(html)
                if converted:
                    for doc in converted:
                        spans = tuple(s.model_copy(update={"block_id": f"{kind}:element:{index}:{s.block_id}", "location": location}) for s in doc.source_spans)
                        local_warnings = tuple(w.model_copy(update={"source_position": location}) for w in doc.warnings)
                        documents.append(doc.model_copy(update={"source_spans": spans, "heading_path": heading_path, "warnings": warnings + local_warnings}))
                    continue
            warnings += (ParseWarning(code="TABLE_STRUCTURE_UNAVAILABLE", message="解析库未提供表格结构", source_position=location),)
        documents.append(
            Document(page_content=text, kind=kind if category != "Table" else "table", heading_path=heading_path, warnings=warnings, source_spans=(SourceSpan(block_id=f"{kind}:element:{index}", start=0, end=len(text), location=location),))
        )
    return documents
