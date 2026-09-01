"""upstream's local partition_md branch with a checked source-fidelity side channel.

Unstructured's HTML intermediate discards Markdown punctuation and source lines.
Token-map blocks therefore carry unique delimiters; dangerous/literal blocks are
masked before partitioning. Only blocks acknowledged exactly once, in order by
partition_md are restored from the source map. This is never a parser fallback.
"""

from __future__ import annotations

import hashlib
import re
import tempfile
from typing import override

from markdown_it import MarkdownIt

from ..base import BaseExtractor
from ..contracts import Document, ExtractionContext, ExtractionError, ExtractSetting, SourceSpan
from ..encoding import decode_text_file, source_lines
from ..normalizer import normalize_documents
from ..runtime_resources import prepare_local_parser


def _mapped_blocks(text: str):
    """Use markdown-it's actual source map, including fences/inline code/HTML."""
    tokens = MarkdownIt("commonmark", {"html": True}).parse(text)
    lines = source_lines(text)
    starts = {0, len(lines)}
    headings = {}
    literal_lines = set()
    for index, token in enumerate(tokens):
        if token.map is None:
            continue
        start, end = token.map
        starts.update((start, end))
        if token.type == "heading_open":
            inline = tokens[index + 1]
            headings[start] = (int(token.tag[1:]), inline.content)
        if token.type in {"fence", "code_block", "html_block"} or any(child.type in {"code_inline", "html_inline"} for child in token.children or ()):
            literal_lines.update(range(start, end))
    # Do not cut a literal code/HTML block at nested element boundaries.
    boundaries = sorted(starts)
    blocks = []
    for start, end in zip(boundaries, boundaries[1:]):
        if start == end:
            continue
        value = "".join(lines[start:end])
        literal = any(line in literal_lines for line in range(start, end)) or bool(re.search(r"[<>{}]", value))
        blocks.append((start, end, value, literal, headings.get(start)))
    return lines, blocks


class UnstructuredMarkdownExtractor(BaseExtractor):
    @override
    def extract(self, setting: ExtractSetting, context: ExtractionContext) -> list[Document]:
        context.check_cancelled()
        text, encoding, warnings = decode_text_file(setting.source_path)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines, blocks = _mapped_blocks(text)
        if not blocks:
            return []
        prefix = "ACTWEAVE" + hashlib.sha256(text.encode()).hexdigest().upper()
        while prefix in text:
            prefix += "X"
        protected = []
        markers = []
        for index, (_, _, value, literal, _) in enumerate(blocks):
            begin = f"{prefix}B{index}Z"
            end = f"{prefix}E{index}Z"
            markers.extend((begin, end))
            # Markers delimit the block even if partition emits several elements
            # (lists/tables). Literal payload never enters its HTML converter.
            middle = f"{prefix}L{index}Z" if literal else value
            protected.append(f"{begin}\n\n{middle}\n\n{end}\n\n")
        prepare_local_parser("unstructured.markdown")
        from unstructured.partition.md import partition_md

        with tempfile.NamedTemporaryFile(suffix=".md", dir=context.work_dir) as safe:
            safe.write("".join(protected).encode())
            safe.flush()
            elements = partition_md(filename=safe.name, languages=[""])
        context.check_cancelled()
        returned = "\n".join(element.text or "" for element in elements)
        cursor = 0
        for marker in markers:
            if returned.count(marker) != 1:
                raise ExtractionError("FORMAT_SIGNATURE_MISMATCH")
            position = returned.find(marker, cursor)
            if position < 0:
                raise ExtractionError("FORMAT_SIGNATURE_MISMATCH")
            cursor = position + len(marker)
        for index, (_, _, _, literal, _) in enumerate(blocks):
            if literal:
                marker = f"{prefix}L{index}Z"
                begin, end = markers[index * 2 : index * 2 + 2]
                position = returned.find(marker)
                if returned.count(marker) != 1 or not returned.find(begin) < position < returned.find(end):
                    raise ExtractionError("FORMAT_SIGNATURE_MISMATCH")
        # Emit original source intervals, not guessed Unstructured line metadata.
        # This deliberately preserves Markdown syntax the library throws away.
        documents = []
        heading_stack = []
        content = []
        spans = []
        offset = 0

        def emit():
            if content:
                documents.append(Document(page_content="".join(content), kind="markdown", heading_path=tuple(h for _, h in heading_stack), source_spans=tuple(spans), warnings=warnings))

        for start, end, _, _, heading in blocks:
            if heading:
                emit()
                content = []
                spans = []
                offset = 0
                level, title = heading
                while heading_stack and heading_stack[-1][0] >= level:
                    heading_stack.pop()
                heading_stack.append((level, title))
            for number in range(start, end):
                value = lines[number]
                spans.append(SourceSpan(block_id=f"line:{number + 1}", start=offset, end=offset + len(value), location={"line": number + 1, "encoding": encoding}))
                content.append(value)
                offset += len(value)
        emit()
        return normalize_documents(documents)
