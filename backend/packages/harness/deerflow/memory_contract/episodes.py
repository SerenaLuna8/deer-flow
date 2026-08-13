"""Pure episode tags, retention, DTO, and opaque cursor contracts."""

from __future__ import annotations

import base64
import binascii
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from deerflow.memory_contract.common import MemoryEpisodeCursorInvalid
from deerflow.memory_contract.history import EPISODE_SEARCH_TAGS

DEFAULT_EPISODE_RETENTION_DAYS = 365
MAX_EPISODE_QUERY_CHARS = 200
EPISODE_SIMILARITY_FLOOR = 0.1


@dataclass(frozen=True, slots=True)
class MemoryEpisodeRecord:
    id: uuid.UUID
    thread_id: str
    origin: str
    tagged_text: str
    occurred_at: datetime
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MemoryEpisodePage:
    items: tuple[MemoryEpisodeRecord, ...]
    next_cursor: str | None


def validate_episode_retention_days(days: int) -> int:
    if type(days) is not int or (days != 0 and not 30 <= days <= 3650):
        raise ValueError("Episode retention days are out of contract")
    return days


def escape_like_pattern(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def validated_episode_tags(tags: object) -> tuple[str, ...]:
    if not isinstance(tags, (tuple, list)):
        raise ValueError("Episode tags must be a sequence")
    normalized: list[str] = []
    for tag in tags:
        if tag not in EPISODE_SEARCH_TAGS:
            raise ValueError("Episode tag is out of contract")
        if tag not in normalized:
            normalized.append(tag)
    return tuple(normalized)


def encode_memory_episode_cursor(record: MemoryEpisodeRecord) -> str:
    if type(record) is not MemoryEpisodeRecord:
        raise TypeError("MemoryEpisodeRecord is required")
    payload = {"i": str(record.id), "t": record.occurred_at.isoformat(), "v": 1}
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("ascii")).decode("ascii").rstrip("=")


def decode_memory_episode_cursor(value: str) -> tuple[datetime, uuid.UUID]:
    try:
        if type(value) is not str or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
            raise ValueError
        raw = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
        if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != value:
            raise ValueError
        payload = json.loads(raw)
        if type(payload) is not dict or set(payload) != {"v", "t", "i"} or payload["v"] != 1:
            raise ValueError
        occurred_at = datetime.fromisoformat(payload["t"])
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError
        return occurred_at, uuid.UUID(payload["i"])
    except (binascii.Error, json.JSONDecodeError, TypeError, ValueError):
        raise MemoryEpisodeCursorInvalid("Memory episode cursor is invalid") from None


__all__ = [
    "DEFAULT_EPISODE_RETENTION_DAYS",
    "EPISODE_SEARCH_TAGS",
    "EPISODE_SIMILARITY_FLOOR",
    "MAX_EPISODE_QUERY_CHARS",
    "MemoryEpisodePage",
    "MemoryEpisodeRecord",
    "decode_memory_episode_cursor",
    "encode_memory_episode_cursor",
    "escape_like_pattern",
    "validate_episode_retention_days",
    "validated_episode_tags",
]
