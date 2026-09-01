"""Safe, bounded raster projection for stateless preview responses."""

from __future__ import annotations

import base64
import hashlib
import io
import math
import os
import stat
import warnings
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from ..extraction.contracts import LocalAttachment

MAX_PREVIEW_ATTACHMENTS = 20
MAX_PREVIEW_ATTACHMENT_BYTES = 128 * 1024
MAX_PREVIEW_ATTACHMENTS_BYTES = 2 * 1024 * 1024
_SAFE_MEDIA_TYPE = "image/png"


def _ordered_unique(values: tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _verified_path(asset: LocalAttachment, work_dir: Path) -> Path | None:
    try:
        root = work_dir.resolve(strict=True)
        candidate = (work_dir / asset.relative_path).resolve(strict=True)
        if not candidate.is_relative_to(root) or candidate.is_symlink():
            return None
        info = os.stat(candidate, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_size != asset.attachment.size_bytes:
            return None
        payload = candidate.read_bytes()
        if hashlib.sha256(payload).hexdigest() != asset.attachment.ref:
            return None
        return candidate
    except (OSError, RuntimeError):
        return None


def _encode_png(image: Image.Image) -> bytes:
    with io.BytesIO() as output:
        image.save(output, format="PNG", compress_level=9, optimize=True)
        return output.getvalue()


def _thumbnail(path: Path, *, expected_width: int, expected_height: int) -> bytes | None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as opened:
                if opened.size != (expected_width, expected_height):
                    return None
                opened.seek(0)
                with ImageOps.exif_transpose(opened) as oriented:
                    mode = "RGBA" if "A" in oriented.getbands() else "RGB"
                    current = oriented.convert(mode)
        try:
            for _ in range(16):
                payload = _encode_png(current)
                if len(payload) <= MAX_PREVIEW_ATTACHMENT_BYTES:
                    return payload
                ratio = min(0.9, math.sqrt(MAX_PREVIEW_ATTACHMENT_BYTES / len(payload)) * 0.9)
                width = max(1, int(current.width * ratio))
                height = max(1, int(current.height * ratio))
                if (width, height) == current.size:
                    width = max(1, width - 1)
                    height = max(1, height - 1)
                resized = current.resize((width, height), Image.Resampling.LANCZOS)
                current.close()
                current = resized
            payload = _encode_png(current)
            return payload if len(payload) <= MAX_PREVIEW_ATTACHMENT_BYTES else None
        finally:
            current.close()
    except (Image.DecompressionBombWarning, Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError, SyntaxError):
        return None


def make_preview_assets(
    assets: tuple[LocalAttachment, ...] | list[LocalAttachment],
    *,
    work_dir: Path,
    selected_refs: tuple[str, ...],
) -> tuple[list[dict[str, str]], int]:
    """Return selected safe thumbnails and the number omitted for any reason."""

    requested = _ordered_unique(selected_refs)
    omitted = max(0, len(requested) - MAX_PREVIEW_ATTACHMENTS)
    requested = requested[:MAX_PREVIEW_ATTACHMENTS]
    by_ref = {asset.attachment.ref: asset for asset in assets}
    projected: list[dict[str, str]] = []
    total_bytes = 0
    for ref in requested:
        asset = by_ref.get(ref)
        path = _verified_path(asset, work_dir) if asset is not None else None
        payload = (
            _thumbnail(
                path,
                expected_width=asset.attachment.width,
                expected_height=asset.attachment.height,
            )
            if asset is not None and path is not None
            else None
        )
        if payload is None or total_bytes + len(payload) > MAX_PREVIEW_ATTACHMENTS_BYTES:
            omitted += 1
            continue
        total_bytes += len(payload)
        projected.append(
            {
                "ref": ref,
                "media_type": _SAFE_MEDIA_TYPE,
                "data_base64": base64.b64encode(payload).decode("ascii"),
            }
        )
    return projected, omitted
