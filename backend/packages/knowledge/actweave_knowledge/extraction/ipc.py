"""Parent-side bounded reception of untrusted child image descriptors.

The runtime calls receive_asset with asyncio.to_thread and awaits already-started
receptions during cancellation. It must deny the child access to received/ and
serialize receptions with the single-asset ACK protocol. No host callback runs
inside this module or is converted into an image warning.
"""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
import warnings
from pathlib import Path
from typing import BinaryIO

from PIL import Image

from .contracts import Attachment, ExtractionError, ExtractionLimits, LocalAttachment
from .images import work_directory_bytes


def _invalid() -> ExtractionError:
    return ExtractionError("PARSER_OUTPUT_INVALID")


def open_child_regular(work_dir: Path, relative_path: str) -> int:
    """Open without following any child component; caller owns the returned FD."""
    # Split the original spelling: PurePath would erase forbidden empty/dot parts.
    parts = relative_path.split("/")
    if len(parts) < 2 or parts[0] != "child" or any(part in {"", ".", ".."} for part in parts) or "\\" in relative_path or "\x00" in relative_path:
        raise _invalid()
    directory = os.open(work_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            for part in parts[:-1]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = next_fd
            result = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                info = os.fstat(result)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise _invalid()
            except BaseException:
                os.close(result)
                raise
            return result
        except OSError as error:
            # Permission failures must not be mislabeled as malformed content.
            if isinstance(error, PermissionError):
                raise
            raise _invalid() from None
    finally:
        os.close(directory)


def _validate_png(stream: BinaryIO, attachment: Attachment, limits: ExtractionLimits) -> None:
    """Verify the copied bytes, rejecting metadata, animation and trailing data."""
    stream.seek(0)
    if stream.read(8) != b"\x89PNG\r\n\x1a\n":
        raise _invalid()
    while True:
        header = stream.read(8)
        if len(header) != 8:
            raise _invalid()
        size, kind = int.from_bytes(header[:4], "big"), header[4:]
        if kind not in {b"IHDR", b"IDAT", b"IEND"} or size > attachment.size_bytes - stream.tell() - 4:
            raise _invalid()
        stream.seek(size + 4, os.SEEK_CUR)
        if kind == b"IEND":
            if size or stream.read(1):
                raise _invalid()
            break
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            stream.seek(0)
            with Image.open(stream, formats=("PNG",)) as image:
                if image.size != (attachment.width, attachment.height) or image.width * image.height > limits.max_image_pixels:
                    raise _invalid()
                image.verify()
            # verify checks chunk integrity; load also validates the IDAT decoder.
            stream.seek(0)
            with Image.open(stream, formats=("PNG",)) as image:
                image.load()
        except (ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise _invalid() from None
        except OSError as error:
            if error.errno is not None:
                raise
            raise _invalid() from None


def receive_asset(asset: LocalAttachment, *, work_dir: Path, limits: ExtractionLimits, accepted: dict[str, Attachment]) -> LocalAttachment:
    """Validate and copy one child asset, then register its verified descriptor."""
    attachment = asset.attachment
    if attachment.media_type != "image/png":
        raise _invalid()
    with os.fdopen(open_child_regular(work_dir, asset.relative_path), "rb") as source:
        info = os.fstat(source.fileno())
        if info.st_size != attachment.size_bytes or info.st_size > limits.max_image_bytes or attachment.width * attachment.height > limits.max_image_pixels:
            raise _invalid()
        previous = accepted.get(attachment.ref)
        if previous is not None and previous != attachment:
            raise _invalid()
        if len(accepted) + (previous is None) > limits.max_images or sum(item.size_bytes for item in accepted.values()) + (attachment.size_bytes if previous is None else 0) > limits.max_total_image_bytes:
            raise ExtractionError("PARSER_IMAGE_LIMIT_EXCEEDED")
        remaining = limits.max_work_dir_bytes - work_directory_bytes(work_dir)
        if attachment.size_bytes > remaining:
            raise ExtractionError("PARSER_WORK_DIR_LIMIT_EXCEEDED")
        root_fd = os.open(work_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            try:
                os.mkdir("received", mode=0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
            try:
                received_fd = os.open("received", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            except OSError as error:
                if isinstance(error, PermissionError):
                    raise
                raise _invalid() from None
        finally:
            os.close(root_fd)
        temporary = f".{uuid.uuid4().hex}.tmp"
        created = False
        try:
            directory_info = os.fstat(received_fd)
            if directory_info.st_uid != os.getuid() or directory_info.st_mode & 0o077:
                raise _invalid()
            fd = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode=0o600, dir_fd=received_fd)
            created = True
            with os.fdopen(fd, "w+b") as destination:
                digest = hashlib.sha256()
                size = 0
                while data := source.read(min(64 * 1024, limits.max_image_bytes - size + 1)):
                    size += len(data)
                    if size > limits.max_image_bytes or size > remaining or size > attachment.size_bytes:
                        raise _invalid()
                    destination.write(data)
                    digest.update(data)
                final_info = os.fstat(source.fileno())
                if final_info.st_nlink != 1 or size != attachment.size_bytes or digest.hexdigest() != attachment.ref:
                    raise _invalid()
                destination.flush()
                _validate_png(destination, attachment, limits)
            filename = f"{attachment.ref}.png"
            if previous is None:
                os.replace(temporary, filename, src_dir_fd=received_fd, dst_dir_fd=received_fd)
                created = False
                accepted[attachment.ref] = attachment
            return LocalAttachment(attachment=attachment, relative_path=f"received/{filename}")
        finally:
            if created:
                os.unlink(temporary, dir_fd=received_fd)
            os.close(received_fd)
