"""Local adaptation of upstream's PDF extractor at 9c16c865977e.

Retains load/parse and the PDFium image-object extraction loop. Host storage,
UploadFile, tenant IDs, and the lossy joined-string cache are deliberately absent.
See extraction/UPSTREAM.md and patches.md for source and license provenance.
"""

from __future__ import annotations

import io
import math
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pypdfium2
import pypdfium2.raw as pdfium_c

from actweave_knowledge.contracts import KNOWLEDGE_QUOTA_EXCEEDED, KnowledgeError

from ..base import BaseExtractor
from ..contracts import AttachmentOccurrence, Document, ExtractionContext, ExtractionError, ExtractSetting, ParseWarning, SourceSpan
from ..images import ImageRejected, work_directory_bytes
from ..literal import escape_literal_text


class _BoundedImageBuffer(io.BytesIO):
    """Bound PDFium's intermediate output before it reaches the shared sink."""

    def __init__(self, maximum: int):
        super().__init__()
        self.maximum = maximum

    def write(self, data: bytes) -> int:
        if self.tell() + len(data) > self.maximum:
            raise ExtractionError("PARSER_WORK_DIR_LIMIT_EXCEEDED")
        return super().write(data)


class PdfExtractor(BaseExtractor):
    """Extract one Document per page, including empty and image-only pages."""

    def extract(self, setting: ExtractSetting, context: ExtractionContext) -> list[Document]:
        return list(self.load(setting, context))

    def load(self, setting: ExtractSetting, context: ExtractionContext) -> Iterator[Document]:
        yield from self.parse(setting, context)

    def parse(self, setting: ExtractSetting, context: ExtractionContext) -> Iterator[Document]:
        context.check_cancelled()
        pdf_reader = pypdfium2.PdfDocument(setting.source_path)
        total = 0
        try:
            for page_number in range(1, len(pdf_reader) + 1):
                context.check_cancelled()
                page = pdf_reader[page_number - 1]
                try:
                    text_page = page.get_textpage()
                    try:
                        # Check before the native text API allocates the Python string.
                        if total + text_page.count_chars() > context.limits.max_text_chars:
                            raise KnowledgeError(KNOWLEDGE_QUOTA_EXCEEDED, "提取正文长度超过限制")
                        context.check_cancelled()
                        content = text_page.get_text_range().replace("\r\n", "\n")
                        content = escape_literal_text(content)
                    finally:
                        text_page.close()
                    context.check_cancelled()
                    total += len(content)
                    if total > context.limits.max_text_chars:
                        raise KnowledgeError(KNOWLEDGE_QUOTA_EXCEEDED, "提取正文长度超过限制")
                    spans = [SourceSpan(block_id=f"page:{page_number}", start=0, end=len(content), location={"page": page_number})]
                    content, images, warnings, total = self._extract_images(page, page_number, content, context, total)
                    spans.extend(image.source for image in images)
                    yield Document(page_content=content, kind="page", source_spans=tuple(spans), attachments=tuple(images), warnings=tuple(warnings))
                finally:
                    page.close()
        finally:
            pdf_reader.close()

    def _extract_images(self, page, page_number: int, content: str, context: ExtractionContext, total: int):
        image_content: list[AttachmentOccurrence] = []
        warnings: list[ParseWarning] = []
        image_objects = page.get_objects(filter=(pdfium_c.FPDF_PAGEOBJ_IMAGE,))
        for image_index, obj in enumerate(image_objects, 1):
            location = {"page": page_number, "image_index": image_index}
            source = SourceSpan(block_id=f"page:{page_number}:image:{image_index}", start=len(content) + (1 if content else 0), end=len(content) + (1 if content else 0), location=location)
            try:
                context.check_cancelled()
                width, height = obj.get_px_size()
                if width * height > context.limits.max_image_pixels:
                    warnings.append(ParseWarning(code="IMAGE_LIMIT_EXCEEDED", message="图片超过安全上限", source_position=location))
                    continue
                remaining = context.limits.max_work_dir_bytes - work_directory_bytes(context.work_dir)
                if remaining <= 0:
                    raise ExtractionError("PARSER_WORK_DIR_LIMIT_EXCEEDED")
                # Retain upstream direct DCT/JPX extraction and PNG fallback; image
                # safety and all deduplication belong exclusively to the sink.
                with _BoundedImageBuffer(remaining) as img_byte_arr:
                    try:
                        direct_extracted = True
                        try:
                            obj.extract(img_byte_arr, fb_format="png")
                        except OSError as error:
                            # This call writes only to the bounded memory buffer.
                            # Pillow cannot encode some valid source modes (CMYK)
                            # as PNG; let the same object render to RGB(A) below.
                            # Real I/O failures retain their original semantics.
                            if error.errno is not None:
                                raise
                            direct_extracted = False
                        # extract() omits separately stored soft masks. Render
                        # this image object (never the page) to inspect alpha.
                        # Predict the library's scale_to_original allocation
                        # before asking native code to create that bitmap.
                        left, bottom, right, top = obj.get_bounds()
                        content_width, content_height = abs(right - left), abs(top - bottom)
                        pixel_width, pixel_height = width, height
                        if not content_width or not content_height:
                            warnings.append(ParseWarning(code="IMAGE_CORRUPT", message="图片无法安全解码", source_position=location))
                            continue
                        if (width < height) != (content_width < content_height):
                            pixel_width, pixel_height = height, width
                        scale = max(pixel_width / content_width, pixel_height / content_height)
                        scaled_width, scaled_height = content_width * scale, content_height * scale
                        if not math.isfinite(scaled_width) or not math.isfinite(scaled_height) or math.ceil(scaled_width) * math.ceil(scaled_height) > context.limits.max_image_pixels:
                            warnings.append(ParseWarning(code="IMAGE_LIMIT_EXCEEDED", message="图片超过安全上限", source_position=location))
                            continue
                        context.check_cancelled()
                        bitmap = obj.get_bitmap(render=True)
                        try:
                            context.check_cancelled()
                            with bitmap.to_pil() as rendered:
                                transparent = False
                                if "A" in rendered.getbands():
                                    with rendered.getchannel("A") as alpha:
                                        transparent = alpha.getextrema()[0] < 255
                                if not direct_extracted or transparent:
                                    img_byte_arr.seek(0)
                                    img_byte_arr.truncate()
                                    rendered.save(img_byte_arr, format="PNG")
                        finally:
                            bitmap.close()
                    except pypdfium2.PdfiumError:
                        warnings.append(ParseWarning(code="IMAGE_CORRUPT", message="图片无法安全解码", source_position=location))
                        continue
                    context.check_cancelled()
                    if not img_byte_arr.tell():
                        continue
                    with tempfile.NamedTemporaryFile(dir=context.work_dir, suffix=".image") as image_file:
                        image_file.write(img_byte_arr.getbuffer())
                        image_file.flush()
                        try:
                            attachment = context.sink.accept(Path(image_file.name), alt_text="本页图片", source=source)
                        except ImageRejected as error:
                            warnings.append(error.warning)
                            continue
                context.check_cancelled()
                markdown = f"![本页图片](knowledge-attachment:{attachment.ref})"
                added = ("\n" if content else "") + markdown
                if total + len(added) > context.limits.max_text_chars:
                    raise KnowledgeError(KNOWLEDGE_QUOTA_EXCEEDED, "提取正文长度超过限制")
                total += len(added)
                source = source.model_copy(update={"end": source.start + len(markdown)})
                content += added
                image_content.append(AttachmentOccurrence(ref=attachment.ref, alt_text="本页图片", source=source))
            finally:
                obj.close()
        return content, image_content, warnings, total
