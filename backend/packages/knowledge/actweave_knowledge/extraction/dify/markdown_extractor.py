"""Markdown loader adapted from Dify 9c16c865977e9d89a9ec7ae0536e893f4385a758.

Retains extract -> parse_tups -> markdown_to_tups and its line/fence grouping.
Tuple outputs become Documents so original locations survive section grouping.
See ../UPSTREAM.md and ../patches.md for provenance and local corrections.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import override

from ..base import BaseExtractor
from ..contracts import Document, ExtractionContext, ExtractSetting, SourceSpan
from ..encoding import decode_text_file, source_lines
from ..normalizer import normalize_documents

ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?)[ \t]*|[ \t]*)$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def parse_heading(line: str) -> tuple[int, str] | None:
    match = ATX.match(line.rstrip("\n"))
    if match is None:
        return None
    title = re.sub(r"[ \t]+#+[ \t]*$", "", match.group(2) or "")
    return len(match.group(1)), title


class MarkdownExtractor(BaseExtractor):
    """Load Markdown/MDX as data; never evaluate HTML or expressions."""

    @override
    def extract(self, setting: ExtractSetting, context: ExtractionContext) -> list[Document]:
        context.check_cancelled()
        documents = self.parse_tups(setting.source_path)
        context.check_cancelled()
        return documents

    def markdown_to_tups(self, markdown_text: str, *, encoding: str = "utf-8") -> list[Document]:
        """Group original lines under headings without copying ancestor text."""
        markdown_text = markdown_text.replace("\r\n", "\n").replace("\r", "\n")
        markdown_tups: list[Document] = []
        lines = source_lines(markdown_text)
        current_header: list[tuple[int, str]] = []
        current_text: list[str] = []
        current_spans: list[SourceSpan] = []
        offset = 0
        code_block_flag: tuple[str, int] | None = None

        def append_section():
            if current_text:
                markdown_tups.append(Document(page_content="".join(current_text), source_spans=tuple(current_spans), heading_path=tuple(title for _, title in current_header), kind="markdown"))

        for number, line in enumerate(lines, 1):
            fence = FENCE.match(line.rstrip("\n"))
            inside_fence = code_block_flag is not None
            if code_block_flag is not None:
                if fence and fence.group(1)[0] == code_block_flag[0] and len(fence.group(1)) >= code_block_flag[1] and not fence.group(2).strip():
                    code_block_flag = None
            elif fence and not (fence.group(1)[0] == "`" and "`" in fence.group(2)):
                code_block_flag = (fence.group(1)[0], len(fence.group(1)))
                inside_fence = True
            header_match = None if inside_fence else parse_heading(line)
            if header_match:
                append_section()
                level, title = header_match
                while current_header and current_header[-1][0] >= level:
                    current_header.pop()
                current_header.append((level, title))
                current_text = []
                current_spans = []
                offset = 0
            current_text.append(line)
            current_spans.append(SourceSpan(block_id=f"line:{number}", start=offset, end=offset + len(line), location={"line": number, "encoding": encoding}))
            offset += len(line)
        append_section()
        return normalize_documents(markdown_tups)

    def parse_tups(self, filepath: str | Path) -> list[Document]:
        """Load with shared bounded decoding before building section offsets."""
        content, encoding, warnings = decode_text_file(Path(filepath))
        documents = self.markdown_to_tups(content, encoding=encoding)
        return [document.model_copy(update={"warnings": warnings + document.warnings}) for document in documents]


def markdown_sections(text: str, *, encoding: str = "utf-8") -> list[Document]:
    return MarkdownExtractor().markdown_to_tups(text, encoding=encoding)
