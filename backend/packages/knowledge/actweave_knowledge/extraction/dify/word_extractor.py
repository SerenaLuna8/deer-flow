"""Local adaptation of Dify's Word extractor at 9c16c865977e.

Retains parse_docx, row/cell traversal and run/hyperlink/legacy-field/drawing
branches. Replaces host persistence with a sink and emits ordered source spans.
See extraction/UPSTREAM.md and patches.md for source and license provenance.
"""

from __future__ import annotations

import re
import tempfile
from bisect import bisect_left
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlparse

from docx import Document as DocxDocument
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from docx.text.run import Run

from actweave_knowledge.contracts import KNOWLEDGE_QUOTA_EXCEEDED, KnowledgeError

from ..base import BaseExtractor
from ..contracts import AttachmentOccurrence, Document, ExtractionContext, ExtractionError, ExtractSetting, ParseWarning, SourceSpan
from ..images import ImageRejected, work_directory_bytes


def _literal(text: str) -> str:
    # Word text is not Markdown source: keep markup-looking text inert without
    # stripping any run spaces (including spaces between formatting changes).
    return re.sub(r"([\\`*_\[\]<>#!|~])", r"\\\1", text)


def _safe_url(url: str) -> str | None:
    if any(ord(character) <= 32 or ord(character) == 127 for character in url):
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https", "mailto"}:
        return None
    if parsed.scheme.lower() in {"http", "https"} and not parsed.netloc:
        return None
    return quote(url, safe=":/?#@!$&'*+,;=%~-._")


@dataclass
class _State:
    context: ExtractionContext
    total: int = 0
    headings: list[tuple[int, str]] = field(default_factory=list)
    table_number: int = 0
    image_indices: dict[str, int] = field(default_factory=dict)

    def charge(self, length: int) -> None:
        self.context.check_cancelled()
        if self.total + length > self.context.limits.max_text_chars:
            raise KnowledgeError(KNOWLEDGE_QUOTA_EXCEEDED, "提取正文长度超过限制")
        self.total += length

    @property
    def heading_path(self) -> tuple[str, ...]:
        return tuple(text for _, text in self.headings)


@dataclass
class _Block:
    text: str = ""
    spans: list[SourceSpan] = field(default_factory=list)
    images: list[AttachmentOccurrence] = field(default_factory=list)
    warnings: list[ParseWarning] = field(default_factory=list)

    def append(self, text: str, state: _State) -> None:
        state.charge(len(text))
        self.text += text

    def extend(self, block: _Block) -> None:
        offset = len(self.text)
        self.spans.extend(span.model_copy(update={"start": span.start + offset, "end": span.end + offset}) for span in block.spans)
        self.images.extend(image.model_copy(update={"source": image.source.model_copy(update={"start": image.source.start + offset, "end": image.source.end + offset})}) for image in block.images)
        self.warnings.extend(block.warnings)
        self.text += block.text

    def protect_indentation(self, state: _State) -> None:
        # Preserve leading whitespace as a character entity only where CommonMark
        # would otherwise turn a Word paragraph (and its images) into code.
        edits = [(match.start(), "&#9;" if match[0] == "\t" else "&#32;") for match in re.finditer(r"(?m)^(?= {4}|\t)[ \t]", self.text)]
        if not edits:
            return
        state.charge(sum(len(replacement) - 1 for _, replacement in edits))

        positions = [position for position, _ in edits]
        shifts = [0]
        pieces = []
        cursor = 0
        for position, replacement in edits:
            pieces.extend((self.text[cursor:position], replacement))
            shifts.append(shifts[-1] + len(replacement) - 1)
            cursor = position + 1
        pieces.append(self.text[cursor:])

        def shifted(offset: int) -> int:
            return offset + shifts[bisect_left(positions, offset)]

        self.spans = [span.model_copy(update={"start": shifted(span.start), "end": shifted(span.end)}) for span in self.spans]
        self.images = [image.model_copy(update={"source": image.source.model_copy(update={"start": shifted(image.source.start), "end": shifted(image.source.end)})}) for image in self.images]
        self.text = "".join(pieces)

    def document(self, state: _State, kind: str) -> Document:
        return Document(page_content=self.text, source_spans=tuple(self.spans), attachments=tuple(self.images), warnings=tuple(self.warnings), heading_path=state.heading_path, kind=kind)


class WordExtractor(BaseExtractor):
    """Load only the admitted local DOCX; never resolve remote resources."""

    def extract(self, setting: ExtractSetting, context: ExtractionContext) -> list[Document]:
        return self.parse_docx(setting.source_path, context)

    def parse_docx(self, docx_path: Path, context: ExtractionContext) -> list[Document]:
        context.check_cancelled()
        doc = DocxDocument(docx_path)
        state = _State(context)
        documents = []
        paragraph_number = 0
        top_table_number = 0
        for block in doc.iter_inner_content():
            context.check_cancelled()
            if isinstance(block, Paragraph):
                paragraph_number += 1
                parsed = self._parse_cell_paragraph(block, {"paragraph": paragraph_number}, f"paragraph:{paragraph_number}", state)
                documents.append(parsed.document(state, "paragraph"))
            elif isinstance(block, Table):
                top_table_number += 1
                documents.extend(self._table_to_markdown(block, str(top_table_number), state))
        return documents

    def _table_to_markdown(self, table: Table, table_path: str, state: _State) -> list[Document]:
        state.table_number += 1
        table_number = state.table_number
        seen_cells = set()
        documents = []
        nested_number = 0
        first_row = table.rows[0] if table.rows else None
        markers = first_row._tr.xpath("./w:trPr/w:tblHeader") if first_row is not None else []
        has_header = bool(markers and markers[0].get(qn("w:val"), "true") not in {"0", "false", "off"})
        for row_number, row in enumerate(table.rows, 1):
            state.context.check_cancelled()
            row_block = _Block()
            first_cell = True
            for column_number, cell in enumerate(row.cells, 1):
                if cell._tc in seen_cells:
                    if has_header:
                        row_block.append("| " if first_cell else " | ", state)
                        first_cell = False
                    continue
                seen_cells.add(cell._tc)
                location = {"table": table_number, "table_path": table_path, "row": row_number, "column": column_number}
                block_id = f"table:{table_path}:row:{row_number}:column:{column_number}"
                cell_first = True
                paragraph_number = 0
                for block in cell.iter_inner_content():
                    state.context.check_cancelled()
                    if isinstance(block, Table):
                        if row_block.text or row_block.warnings:
                            if has_header:
                                row_block.append(" |", state)
                            documents.append(row_block.document(state, "table_row"))
                            row_block = _Block()
                            first_cell = True
                        nested_number += 1
                        documents.extend(self._table_to_markdown(block, f"{table_path}.{nested_number}", state))
                        cell_first = True
                    elif isinstance(block, Paragraph):
                        paragraph_number += 1
                        if cell_first:
                            prefix = ("| " if first_cell else " | ") if has_header else (("" if first_cell else "\n") + f"列{column_number}：")
                            row_block.append(prefix, state)
                            cell_first = False
                            first_cell = False
                        else:
                            row_block.append("; " if has_header else "\n", state)
                        parsed = self._parse_cell_paragraph(block, {**location, "paragraph": paragraph_number}, f"{block_id}:paragraph:{paragraph_number}", state, inline_table=has_header)
                        cell_span = SourceSpan(block_id=(f"table_header:{table_path}:column:{column_number}" if has_header and row_number == 1 else block_id), start=0, end=len(parsed.text), location=location)
                        parsed.spans.append(cell_span)
                        row_block.extend(parsed)
            if row_block.text or row_block.warnings:
                if has_header:
                    row_block.append(" |", state)
                    if row_number == 1:
                        row_block.append("\n| " + " | ".join("---" for _ in row.cells) + " |", state)
                documents.append(row_block.document(state, "table_header" if has_header and row_number == 1 else "table_row"))
        return documents

    def _extract_images_from_docx(self, paragraph, image_id: str, block: _Block, location: dict, block_id: str, state: _State) -> None:
        context = state.context
        context.check_cancelled()
        rel = paragraph.part.rels.get(image_id)
        if rel is None:
            return
        image_index = state.image_indices.get(block_id, 0) + 1
        state.image_indices[block_id] = image_index
        source = SourceSpan(block_id=f"{block_id}:image:{image_index}", start=len(block.text), end=len(block.text), location={**location, "image_index": image_index})
        if rel.is_external:
            block.append("（外部图片未获取）", state)
            block.warnings.append(ParseWarning(code="EXTERNAL_IMAGE_NOT_FETCHED", message="外部图片未获取", source_position=source.location))
            return
        data = rel.target_part.blob
        if work_directory_bytes(context.work_dir) + len(data) > context.limits.max_work_dir_bytes:
            raise ExtractionError("PARSER_WORK_DIR_LIMIT_EXCEEDED")
        with tempfile.NamedTemporaryFile(dir=context.work_dir, suffix=".image") as image_file:
            image_file.write(data)
            image_file.flush()
            try:
                attachment = context.sink.accept(Path(image_file.name), alt_text="图片", source=source)
            except ImageRejected as error:
                block.warnings.append(error.warning)
                return
        markdown = f"![图片](knowledge-attachment:{attachment.ref})"
        block.append(markdown, state)
        source = source.model_copy(update={"end": source.start + len(markdown)})
        block.spans.append(source)
        block.images.append(AttachmentOccurrence(ref=attachment.ref, alt_text="图片", source=source))

    def _parse_cell_paragraph(self, paragraph: Paragraph, location: dict, block_id: str, state: _State, *, inline_table: bool = False) -> _Block:
        paragraph_content = _Block()
        style = paragraph.style.name if paragraph.style is not None else ""
        heading = re.fullmatch(r"Heading ([1-6])", style)
        if heading:
            level = int(heading[1])
            state.headings = [(n, title) for n, title in state.headings if n < level]
            state.headings.append((level, paragraph.text))
            paragraph_content.append("#" * level + " ", state)

        def process_run(run: Run, target_buffer: _Block):
            # Unlike upstream, visit run children in XML order: a single run can
            # contain text both before and after its drawing.
            for child in run.element:
                state.context.check_cancelled()
                if child.tag == qn("w:t"):
                    text = child.text or ""
                    if inline_table:
                        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "; ")
                    target_buffer.append(_literal(text), state)
                elif child.tag == qn("w:tab"):
                    target_buffer.append("\t", state)
                elif child.tag in {qn("w:br"), qn("w:cr")}:
                    # GFM treats a raw newline as a new row. Serialize cell-local
                    # breaks with the existing paragraph delimiter before spans
                    # or subsequent image offsets are assigned; no HTML needed.
                    target_buffer.append("; " if inline_table else "\n", state)
                elif child.tag == qn("w:drawing"):
                    for blip in child.findall(".//" + qn("a:blip")):
                        image_id = blip.get(qn("r:embed")) or blip.get(qn("r:link"))
                        if image_id:
                            self._extract_images_from_docx(paragraph, image_id, target_buffer, location, block_id, state)
                elif child.tag == qn("w:pict"):
                    for image in child.iter():
                        if image.tag in {"{urn:schemas-microsoft-com:vml}imagedata", qn("w:binData")}:
                            image_id = image.get(qn("r:id")) or image.get("id")
                            if image_id:
                                self._extract_images_from_docx(paragraph, image_id, target_buffer, location, block_id, state)

        def append_link(target: _Block, linked: _Block, url: str | None):
            safe_url = _safe_url(url) if url else None
            if safe_url and linked.text:
                target.append("[", state)
                target.extend(linked)
                target.append(f"]({safe_url})", state)
            else:
                target.extend(linked)

        def process_hyperlink(element, target):
            linked = _Block()
            for run_elem in element.findall(qn("w:r")):
                process_run(Run(run_elem, paragraph), linked)
            rel = paragraph.part.rels.get(element.get(qn("r:id")))
            append_link(target, linked, rel.target_ref if rel is not None and rel.is_external else None)

        # Retain the upstream legacy HYPERLINK field state, but never strip its
        # display runs, and retain visible text from incomplete/unsafe fields.
        field_instruction = ""
        field_text = _Block()
        in_field = False
        collecting = False
        for child in paragraph._element:
            state.context.check_cancelled()
            if child.tag == qn("w:hyperlink"):
                process_hyperlink(child, field_text if collecting else paragraph_content)
            elif child.tag == qn("w:r"):
                for marker in child.findall(qn("w:fldChar")):
                    kind = marker.get(qn("w:fldCharType"))
                    if kind == "begin":
                        in_field, collecting, field_instruction, field_text = True, False, "", _Block()
                    elif kind == "separate":
                        collecting = True
                    elif kind == "end":
                        match = re.search(r'HYPERLINK\s+"([^"]+)"', field_instruction, re.IGNORECASE)
                        append_link(paragraph_content, field_text, match[1] if match else None)
                        in_field, collecting, field_text = False, False, _Block()
                for instruction in child.findall(qn("w:instrText")):
                    field_instruction += instruction.text or ""
                if not in_field or collecting:
                    process_run(Run(child, paragraph), field_text if collecting else paragraph_content)
            elif child.tag == qn("w:fldSimple"):
                linked = _Block()
                for run_elem in child.findall(qn("w:r")):
                    process_run(Run(run_elem, paragraph), linked)
                match = re.search(r'HYPERLINK\s+"([^"]+)"', child.get(qn("w:instr"), ""), re.IGNORECASE)
                append_link(paragraph_content, linked, match[1] if match else None)
        paragraph_content.extend(field_text)
        paragraph_content.protect_indentation(state)
        paragraph_content.spans.insert(0, SourceSpan(block_id=block_id, start=0, end=len(paragraph_content.text), location=location))
        return paragraph_content
