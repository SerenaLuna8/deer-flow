from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath, PureWindowsPath

from app.private_work.errors import PrivateWorkInvalid

_MIME_TOKEN = r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+"
_MIME_QUOTED_VALUE = r'"(?:[\x20-\x21\x23-\x5B\x5D-\x7E]|\\[\x20-\x7E])*"'
_MIME_TYPE_PATTERN = re.compile(
    rf"{_MIME_TOKEN}/{_MIME_TOKEN}(?: *; *{_MIME_TOKEN} *= *(?:{_MIME_TOKEN}|{_MIME_QUOTED_VALUE}))*\Z",
    re.ASCII,
)


def normalize_private_logical_path(raw: str, *, request_id: str) -> str:
    """Return one unambiguous relative POSIX logical path or fail closed."""

    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise PrivateWorkInvalid(request_id)
    if any(unicodedata.category(character).startswith("C") for character in raw):
        raise PrivateWorkInvalid(request_id)
    raw = unicodedata.normalize("NFC", raw)
    if len(raw.encode("utf-8")) > 1024:
        raise PrivateWorkInvalid(request_id)
    if PureWindowsPath(raw).drive:
        raise PrivateWorkInvalid(request_id)
    raw_parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise PrivateWorkInvalid(request_id)
    if any(len(part.encode("utf-8")) > 255 for part in raw_parts):
        raise PrivateWorkInvalid(request_id)
    path = PurePosixPath(raw)
    if path.is_absolute() or path.as_posix() != raw:
        raise PrivateWorkInvalid(request_id)
    return path.as_posix()


def is_safe_private_media_type(raw: object) -> bool:
    """Return whether a value is a bounded, unambiguous ASCII MIME value."""

    return isinstance(raw, str) and 0 < len(raw) <= 255 and raw.isascii() and _MIME_TYPE_PATTERN.fullmatch(raw) is not None


def validate_private_media_type(raw: object, *, request_id: str) -> str:
    if not is_safe_private_media_type(raw):
        raise PrivateWorkInvalid(request_id)
    return raw
