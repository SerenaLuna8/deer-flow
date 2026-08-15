"""Authorized, bounded image reading and metadata-free normalization."""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from pathlib import PurePosixPath
from threading import Event
from typing import TYPE_CHECKING

from PIL import Image, ImageOps, UnidentifiedImageError

from deerflow.agents.thread_state import ThreadDataState
from deerflow.config.paths import VIRTUAL_PATH_PREFIX
from deerflow.file_authority import require_private_file_authority
from deerflow.private_scope import PrivateResourceScope
from deerflow.sandbox.sandbox import (
    PRIVATE_FILE_IO_CHUNK_SIZE,
    Sandbox,
)

if TYPE_CHECKING:
    from deerflow.tools.types import Runtime

ALLOWED_IMAGE_VIRTUAL_ROOTS = (
    f"{VIRTUAL_PATH_PREFIX}/workspace",
    f"{VIRTUAL_PATH_PREFIX}/uploads",
    f"{VIRTUAL_PATH_PREFIX}/outputs",
)
ALLOWED_IMAGE_VIRTUAL_ROOTS_TEXT = ", ".join(ALLOWED_IMAGE_VIRTUAL_ROOTS)
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_NORMALIZED_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_DIMENSION = 8_192
MAX_IMAGE_PIXELS = 40_000_000
MAX_NORMALIZED_DIMENSION = 4_096
EXTENSION_TO_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
INSPECT_IMAGE_EXTENSION_TO_MIME = {extension: mime_type for extension, mime_type in EXTENSION_TO_MIME.items() if mime_type != "image/gif"}


class ImageTooLargeError(ValueError):
    """The compressed image exceeds its fixed read budget."""


class ImageNormalizationError(ValueError):
    """One stable image validation failure safe to map at the tool boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class NormalizedImage:
    data: bytes
    mime_type: str
    width: int
    height: int
    sha256: str


def ensure_sandbox_initialized(runtime: Runtime) -> Sandbox:
    """Lazy import avoids the sandbox-tools and builtin-tools package cycle."""

    from deerflow.sandbox.tools import ensure_sandbox_initialized as initialize

    return initialize(runtime)


def sandbox_from_runtime(runtime: Runtime) -> Sandbox:
    """Lazy import avoids the sandbox-tools and builtin-tools package cycle."""

    from deerflow.sandbox.tools import sandbox_from_runtime as resolve

    return resolve(runtime)


def is_allowed_image_virtual_path(image_path: str) -> bool:
    if type(image_path) is not str:
        return False
    parsed = PurePosixPath(image_path)
    if not parsed.is_absolute() or ".." in parsed.parts or parsed.as_posix() != image_path:
        return False
    return any(image_path == root or image_path.startswith(f"{root}/") for root in ALLOWED_IMAGE_VIRTUAL_ROOTS)


def expected_image_mime(
    image_path: str,
    *,
    for_inspection: bool = False,
) -> str | None:
    mapping = INSPECT_IMAGE_EXTENSION_TO_MIME if for_inspection else EXTENSION_TO_MIME
    return mapping.get(PurePosixPath(image_path).suffix.lower())


def detect_image_mime(image_data: bytes) -> str | None:
    if image_data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(image_data) >= 12 and image_data.startswith(b"RIFF") and image_data[8:12] == b"WEBP":
        return "image/webp"
    if image_data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return None


def sanitize_image_error(
    error: Exception,
    thread_data: ThreadDataState | None,
) -> str:
    from deerflow.sandbox.tools import mask_local_paths_in_output

    return mask_local_paths_in_output(
        f"{type(error).__name__}: {error}",
        thread_data,
    )


def read_bounded_image_bytes(
    sandbox: Sandbox,
    image_path: str,
    *,
    max_bytes: int | None = None,
    cancel_event: Event | None = None,
) -> bytes:
    """Read one regular sandbox file through the secure authority API."""

    limit = MAX_IMAGE_BYTES if max_bytes is None else max_bytes
    handle = sandbox.open_regular_file(image_path)
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("Private image read was cancelled")
            chunk = sandbox.read_regular_file(handle, PRIVATE_FILE_IO_CHUNK_SIZE)
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("Private image read was cancelled")
            if not isinstance(chunk, bytes):
                raise OSError("Private image reader returned invalid data")
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ImageTooLargeError
            chunks.append(chunk)
    finally:
        sandbox.close_regular_file(handle)
    return b"".join(chunks)


def image_sandbox(runtime: Runtime) -> Sandbox:
    """Resolve the current Sandbox without replacing a private Run lease."""

    context = runtime.context or {}
    authority = require_private_file_authority(context)
    if authority is None:
        return ensure_sandbox_initialized(runtime)

    sandbox = sandbox_from_runtime(runtime)
    authority_sandbox_id = getattr(authority, "sandbox_id", None)
    if not isinstance(authority_sandbox_id, str) or not authority_sandbox_id or authority_sandbox_id != sandbox.id:
        raise RuntimeError("Private file authority is unavailable")
    return sandbox


def current_private_scope(runtime: Runtime) -> PrivateResourceScope | None:
    """Return only the server-issued exact private scope, when present."""

    context = runtime.context
    if not isinstance(context, dict):
        return None
    scope = context.get("private_scope")
    if scope is None:
        return None
    if type(scope) is not PrivateResourceScope:
        raise RuntimeError("Private Run scope is unavailable")
    return scope


def _check_cancelled(cancel_event: Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("Private image processing was cancelled")


def _save_without_metadata(image: Image.Image) -> tuple[bytes, str]:
    output = io.BytesIO()
    has_alpha = image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info)
    if has_alpha:
        image.convert("RGBA").save(output, format="PNG", compress_level=6)
        return output.getvalue(), "image/png"
    image.convert("RGB").save(
        output,
        format="JPEG",
        quality=90,
        optimize=False,
        progressive=False,
    )
    return output.getvalue(), "image/jpeg"


def normalize_image(
    image_data: bytes,
    declared_mime_type: str,
    *,
    cancel_event: Event | None = None,
) -> NormalizedImage:
    """Decode one static image, apply orientation and strip all metadata."""

    _check_cancelled(cancel_event)
    detected_mime_type = detect_image_mime(image_data)
    if detected_mime_type is None or detected_mime_type != declared_mime_type or detected_mime_type == "image/gif":
        raise ImageNormalizationError("UNSUPPORTED_MEDIA")
    try:
        with Image.open(io.BytesIO(image_data)) as probe:
            if getattr(probe, "n_frames", 1) != 1:
                raise ImageNormalizationError("UNSUPPORTED_MEDIA")
            width, height = probe.size
            if width < 1 or height < 1 or width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION or width * height > MAX_IMAGE_PIXELS:
                raise ImageNormalizationError("IMAGE_PIXEL_LIMIT_EXCEEDED")
            probe.verify()
        _check_cancelled(cancel_event)
        with Image.open(io.BytesIO(image_data)) as source:
            source.load()
            normalized = ImageOps.exif_transpose(source)
            normalized.thumbnail(
                (MAX_NORMALIZED_DIMENSION, MAX_NORMALIZED_DIMENSION),
                Image.Resampling.LANCZOS,
            )
            _check_cancelled(cancel_event)
            wire_data, wire_mime_type = _save_without_metadata(normalized)
            width, height = normalized.size
    except ImageNormalizationError:
        raise
    except (Image.DecompressionBombError, UnidentifiedImageError):
        raise ImageNormalizationError("IMAGE_PIXEL_LIMIT_EXCEEDED") from None
    except (OSError, SyntaxError, ValueError):
        raise ImageNormalizationError("UNSUPPORTED_MEDIA") from None
    _check_cancelled(cancel_event)
    if len(wire_data) > MAX_NORMALIZED_IMAGE_BYTES:
        raise ImageNormalizationError("IMAGE_TOO_LARGE")
    return NormalizedImage(
        data=wire_data,
        mime_type=wire_mime_type,
        width=width,
        height=height,
        sha256=hashlib.sha256(wire_data).hexdigest(),
    )


__all__ = [
    "ALLOWED_IMAGE_VIRTUAL_ROOTS",
    "ALLOWED_IMAGE_VIRTUAL_ROOTS_TEXT",
    "EXTENSION_TO_MIME",
    "INSPECT_IMAGE_EXTENSION_TO_MIME",
    "ImageNormalizationError",
    "ImageTooLargeError",
    "MAX_IMAGE_BYTES",
    "MAX_IMAGE_DIMENSION",
    "MAX_IMAGE_PIXELS",
    "MAX_NORMALIZED_DIMENSION",
    "MAX_NORMALIZED_IMAGE_BYTES",
    "NormalizedImage",
    "current_private_scope",
    "detect_image_mime",
    "expected_image_mime",
    "image_sandbox",
    "is_allowed_image_virtual_path",
    "normalize_image",
    "read_bounded_image_bytes",
    "sanitize_image_error",
]
