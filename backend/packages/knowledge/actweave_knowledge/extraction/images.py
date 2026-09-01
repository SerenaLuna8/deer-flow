"""Bounded, metadata-free raster normalization inside the parser process."""

from __future__ import annotations

import hashlib
import io
import os
import stat
import warnings
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .contracts import Attachment, ExtractionError, ExtractionLimits, LocalAttachment, ParseWarning, SourceSpan

_RASTER_FORMATS = {"PNG", "JPEG", "GIF", "TIFF", "WEBP", "BMP", "ICO"}


class ImageRejected(Exception):
    """Only an individual image-content/policy failure may be downgraded."""

    def __init__(self, warning: ParseWarning) -> None:
        super().__init__(warning.code)
        self.warning = warning


def _rejected(code: str) -> ImageRejected:
    message = "图片超过安全上限" if code == "IMAGE_LIMIT_EXCEEDED" else "图片无法安全解码"
    return ImageRejected(ParseWarning(code=code, message=message))


def work_directory_bytes(work_dir: Path) -> int:
    """Count allocated file contents without following links or opening devices.

    The runtime must additionally enforce the whole sandbox budget while parsers
    create conversion files; this check is the synchronous image I/O boundary.
    """

    def failed(error: OSError) -> None:
        raise error

    total = 0
    if not work_dir.exists():
        return total
    for root, _, names in os.walk(work_dir, followlinks=False, onerror=failed):
        for name in names:
            info = os.stat(Path(root) / name, follow_symlinks=False)
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total


class _BoundedBuffer(io.BytesIO):
    def __init__(self, maximum: int) -> None:
        super().__init__()
        self.maximum = maximum

    def write(self, data: bytes) -> int:
        if self.tell() + len(data) > self.maximum:
            raise _rejected("IMAGE_LIMIT_EXCEEDED")
        return super().write(data)


def _normalize(source_path: Path, target_dir: Path, limits: ExtractionLimits) -> tuple[LocalAttachment, bool]:
    used = work_directory_bytes(target_dir)
    if used > limits.max_work_dir_bytes:
        raise ExtractionError("PARSER_WORK_DIR_LIMIT_EXCEEDED")

    # Opening belongs outside the decoder-error boundary: missing inputs and
    # filesystem permissions are orchestration failures, not corrupt images.
    fd = os.open(source_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ExtractionError("PARSER_OUTPUT_INVALID")
        # Extracted rasters are intermediates, not the original uploaded document.
        if info.st_size > limits.max_work_dir_bytes:
            raise ExtractionError("PARSER_WORK_DIR_LIMIT_EXCEEDED")
        if not source_path.resolve().is_relative_to(target_dir.resolve()):
            used += info.st_size
        if used > limits.max_work_dir_bytes:
            raise ExtractionError("PARSER_WORK_DIR_LIMIT_EXCEEDED")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            try:
                with Image.open(source) as opened:
                    if opened.format not in _RASTER_FORMATS:
                        raise _rejected("IMAGE_CORRUPT")
                    width, height = opened.size
                    if width * height > limits.max_image_pixels:
                        raise _rejected("IMAGE_LIMIT_EXCEEDED")
                    # Do not enumerate/decode every frame to discover animation.
                    try:
                        opened.seek(1)
                    except EOFError:
                        multiple_frames = False
                    else:
                        multiple_frames = True
                    opened.seek(0)
                    with ImageOps.exif_transpose(opened) as oriented, oriented.convert("RGBA") as frame:
                        clean = Image.new("RGBA", frame.size)
                        try:
                            clean.paste(frame)
                        except BaseException:
                            clean.close()
                            raise
            except (Image.DecompressionBombWarning, Image.DecompressionBombError):
                raise _rejected("IMAGE_LIMIT_EXCEEDED") from None
            except (UnidentifiedImageError, ValueError, SyntaxError):
                raise _rejected("IMAGE_CORRUPT") from None
            except OSError as error:
                if error.errno is not None:
                    raise
                raise _rejected("IMAGE_CORRUPT") from None

    with clean, _BoundedBuffer(limits.max_image_bytes) as output:
        clean.save(output, format="PNG", compress_level=9, optimize=False)
        data = output.getvalue()
        width, height = clean.size
    ref = hashlib.sha256(data).hexdigest()
    target = target_dir / f"{ref}.png"
    if not target.exists():
        if used + len(data) > limits.max_work_dir_bytes:
            raise ExtractionError("PARSER_WORK_DIR_LIMIT_EXCEEDED")
        target_dir.mkdir(parents=True, exist_ok=True)
        created = False
        try:
            with target.open("xb") as stream:
                created = True
                stream.write(data)
        except BaseException:
            if created:
                target.unlink(missing_ok=True)
            raise
    return LocalAttachment(attachment=Attachment(ref=ref, media_type="image/png", size_bytes=len(data), width=width, height=height), relative_path=target.name), multiple_frames


def normalize_image(source_path: Path, target_dir: Path, limits: ExtractionLimits) -> LocalAttachment:
    """Normalize one local raster; use the sink to collect occurrence warnings."""
    return _normalize(source_path, target_dir, limits)[0]


class LocalAttachmentSink:
    """Deduplicate image bytes; adapters retain each separate occurrence span.

    ``work_dir`` is the child's asset directory. Local paths are relative to it;
    the child IPC adapter prefixes its ``child/`` directory before transmission.
    """

    def __init__(self, work_dir: Path, limits: ExtractionLimits) -> None:
        self.work_dir = work_dir
        self.limits = limits
        self.assets: list[LocalAttachment] = []
        self.warnings: list[ParseWarning] = []
        self._accepted: dict[str, Attachment] = {}
        self._total_bytes = 0

    def accept(self, source_path: Path, *, alt_text: str, source: SourceSpan) -> Attachment:
        try:
            asset, multiple_frames = _normalize(source_path, self.work_dir, self.limits)
            attachment = asset.attachment
            if attachment.ref not in self._accepted:
                if len(self.assets) >= self.limits.max_images or self._total_bytes + attachment.size_bytes > self.limits.max_total_image_bytes:
                    (self.work_dir / asset.relative_path).unlink()
                    raise _rejected("IMAGE_LIMIT_EXCEEDED")
                self.assets.append(asset)
                self._accepted[attachment.ref] = attachment
                self._total_bytes += attachment.size_bytes
        except ImageRejected as error:
            raise ImageRejected(error.warning.model_copy(update={"source_position": dict(source.location)})) from None
        if multiple_frames:
            self.warnings.append(ParseWarning(code="IMAGE_FIRST_FRAME_ONLY", message="多帧图片仅保留首帧", source_position=source.location))
        return attachment
