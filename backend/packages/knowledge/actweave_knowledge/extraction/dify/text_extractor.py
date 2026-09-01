"""Text loader adapted from Dify 9c16c865977e9d89a9ec7ae0536e893f4385a758.

See ../UPSTREAM.md and ../patches.md for provenance and local corrections.
"""

from typing import override

from ..base import BaseExtractor
from ..contracts import Document, ExtractionContext, ExtractSetting, SourceSpan
from ..encoding import decode_text_file, source_lines


class TextExtractor(BaseExtractor):
    """Load text files without lossy decoding or trimming source whitespace."""

    @override
    def extract(self, setting: ExtractSetting, context: ExtractionContext) -> list[Document]:
        """Load from the admitted local path; never put that path in metadata."""
        context.check_cancelled()
        text, encoding, warnings = decode_text_file(setting.source_path)
        spans = []
        offset = 0
        for number, line in enumerate(source_lines(text), 1):
            spans.append(SourceSpan(block_id=f"line:{number}", start=offset, end=offset + len(line), location={"line": number, "encoding": encoding}))
            offset += len(line)
        context.check_cancelled()
        return [Document(page_content=text, source_spans=tuple(spans), kind="text", warnings=warnings)]
