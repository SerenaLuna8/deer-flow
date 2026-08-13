"""Pure Memory document DTOs and diff helpers."""

from __future__ import annotations

import difflib
import hashlib
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime

MAX_MEMORY_UNIFIED_DIFF_CHARS = 64_000
MAX_MEMORY_DOCUMENT_CHARS = 16_000
DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES = (
    "用户偏好与协作方式",
    "项目背景",
    "长期约束与架构决策",
    "当前仍有效的目标",
)
MEMORY_DOCUMENT_SECTIONS = tuple(f"# {title}" for title in DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES)
EMPTY_MEMORY_DOCUMENT = "\n\n".join(MEMORY_DOCUMENT_SECTIONS)
MAX_MEMORY_DOCUMENT_SECTIONS = 8
MIN_MEMORY_DOCUMENT_SECTIONS = 2
MAX_MEMORY_DOCUMENT_SECTION_TITLE_CHARS = 80

_CJK_RANGES = (
    ("\u3400", "\u4dbf"),
    ("\u4e00", "\u9fff"),
    ("\u3040", "\u30ff"),
    ("\uac00", "\ud7a3"),
)
_FORBIDDEN_HISTORY_MARKER = re.compile(
    r"\[H:\d+\]|\[(?:skip|correction|permanent|durable|ephemeral)\]",
    re.IGNORECASE,
)


class MemoryDocumentInvalid(ValueError):
    """The complete Memory document does not satisfy its frozen contract."""


class MemoryDocumentOverBudget(MemoryDocumentInvalid):
    """The complete Memory document exceeds its frozen token budget."""

    def __init__(
        self,
        *,
        estimated_tokens: int,
        limit_tokens: int,
        target_tokens: int,
        actual_characters: int,
        target_characters: int,
    ) -> None:
        self.estimated_tokens = estimated_tokens
        self.limit_tokens = limit_tokens
        self.target_tokens = target_tokens
        self.actual_characters = actual_characters
        self.target_characters = target_characters
        self.overage_tokens = estimated_tokens - limit_tokens
        self.reduction_tokens = estimated_tokens - target_tokens
        self.reduction_characters = actual_characters - target_characters
        character_guidance = f" Document chars {actual_characters}, target-character-limit {target_characters}"
        if self.reduction_characters > 0:
            character_guidance += f"; remove at least {self.reduction_characters} characters before retrying"
        super().__init__(
            "Memory document exceeds the token budget "
            f"(estimated {estimated_tokens}, limit {limit_tokens}, "
            f"overage {self.overage_tokens}). Target <= {target_tokens} estimated "
            "tokens (90% of limit); remove at least "
            f"{self.reduction_tokens} estimated tokens before retrying."
            f"{character_guidance}"
        )


def validate_memory_document_sections(sections: object) -> tuple[str, ...]:
    """Validate one immutable ordered list of plain section titles."""

    if not isinstance(sections, (tuple, list)) or not (MIN_MEMORY_DOCUMENT_SECTIONS <= len(sections) <= MAX_MEMORY_DOCUMENT_SECTIONS):
        raise MemoryDocumentInvalid("Memory document sections are invalid")
    validated: list[str] = []
    for title in sections:
        if (
            not isinstance(title, str)
            or title != title.strip()
            or not title
            or len(title) > MAX_MEMORY_DOCUMENT_SECTION_TITLE_CHARS
            or title.startswith("#")
            or len(title.splitlines()) != 1
            or any((category := unicodedata.category(character)).startswith("C") or category in {"Zl", "Zp"} for character in title)
            or _FORBIDDEN_HISTORY_MARKER.search(title) is not None
        ):
            raise MemoryDocumentInvalid("Memory document sections are invalid")
        validated.append(title)
    if len(validated) != len(set(validated)):
        raise MemoryDocumentInvalid("Memory document sections are invalid")
    return tuple(validated)


def render_empty_memory_document(sections: object) -> str:
    return "\n\n".join(f"# {title}" for title in validate_memory_document_sections(sections))


def estimate_memory_tokens(content: str) -> int:
    """Return the deterministic conservative Memory token estimate."""

    if not isinstance(content, str):
        raise TypeError("Memory document must be text")
    cjk = sum(1 for character in content if any(start <= character <= end for start, end in _CJK_RANGES))
    non_cjk = len(content) - cjk
    return cjk + ((non_cjk + 3) // 4)


def target_memory_token_limit(max_tokens: int) -> int:
    return (max_tokens * 9) // 10


def target_memory_character_limit(max_tokens: int) -> int:
    return min(MAX_MEMORY_DOCUMENT_CHARS, target_memory_token_limit(max_tokens))


def validate_memory_document_structure(
    content: str,
    *,
    sections: object = DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
) -> str:
    """Validate only the bounded structure of a complete Memory document."""

    if not isinstance(content, str):
        raise MemoryDocumentInvalid("Memory document must be text")
    if not content or len(content) > MAX_MEMORY_DOCUMENT_CHARS:
        raise MemoryDocumentInvalid("Memory document exceeds the character contract")
    headings = tuple(f"# {title}" for title in validate_memory_document_sections(sections))
    top_level = tuple(line for line in content.splitlines() if line.startswith("# "))
    if top_level != headings:
        raise MemoryDocumentInvalid("Memory document sections are invalid")
    if _FORBIDDEN_HISTORY_MARKER.search(content) is not None:
        raise MemoryDocumentInvalid("Memory document contains a history marker")
    return content


def validate_memory_document(
    content: str,
    max_tokens: int,
    *,
    sections: object = DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
) -> str:
    """Validate a complete Memory document without normalizing or truncating."""

    if type(max_tokens) is not int or max_tokens < 1:
        raise ValueError("Memory token budget must be positive")
    validate_memory_document_structure(content, sections=sections)
    estimated_tokens = estimate_memory_tokens(content)
    if estimated_tokens > max_tokens:
        raise MemoryDocumentOverBudget(
            estimated_tokens=estimated_tokens,
            limit_tokens=max_tokens,
            target_tokens=target_memory_token_limit(max_tokens),
            actual_characters=len(content),
            target_characters=target_memory_character_limit(max_tokens),
        )
    return content


@dataclass(frozen=True, slots=True)
class MemoryDocumentRecord:
    content: str
    content_digest: str
    sections: tuple[str, ...]
    sections_policy_version_id: uuid.UUID | None
    version: int
    dream_cursor: int
    active_dream_job_id: uuid.UUID | None
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class MemoryDocumentState:
    document: MemoryDocumentRecord
    pending_count: int


@dataclass(frozen=True, slots=True)
class MemoryDocumentVersionRecord:
    version: int
    content: str
    content_digest: str
    unified_diff: str
    trigger: str
    dream_job_id: uuid.UUID | None
    history_from: int | None
    history_to: int | None
    history_count: int | None
    prompt_version: str | None
    model_ref: uuid.UUID | None
    needs_review: bool
    created_at: datetime


def memory_document_digest(content: str) -> str:
    if not isinstance(content, str):
        raise TypeError("Memory document must be text")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def memory_document_unified_diff(before: str, after: str) -> str:
    if not isinstance(before, str) or not isinstance(after, str):
        raise TypeError("Memory document diff requires text")
    if before == after:
        return ""
    lines = tuple(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile="memory-before.md",
            tofile="memory-after.md",
            lineterm="",
        )
    )
    return "\n".join(lines) + "\n"


def _utf16_code_units(value: str) -> int:
    return sum(2 if ord(character) > 0xFFFF else 1 for character in value)


def memory_document_diff_preview(
    unified_diff: str,
    *,
    legacy_utf16: bool = False,
) -> tuple[str, bool]:
    """Return a line-safe public preview without changing the stored diff."""

    if not isinstance(unified_diff, str):
        raise TypeError("Memory document diff must be text")
    measure = _utf16_code_units if legacy_utf16 else len
    if measure(unified_diff) <= MAX_MEMORY_UNIFIED_DIFF_CHARS:
        return unified_diff, False
    preview: list[str] = []
    preview_units = 0
    for line in unified_diff.splitlines(keepends=True):
        line_units = measure(line)
        if preview_units + line_units > MAX_MEMORY_UNIFIED_DIFF_CHARS:
            break
        preview.append(line)
        preview_units += line_units
    return "".join(preview), True


__all__ = [
    "DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES",
    "EMPTY_MEMORY_DOCUMENT",
    "MAX_MEMORY_DOCUMENT_CHARS",
    "MAX_MEMORY_DOCUMENT_SECTIONS",
    "MAX_MEMORY_DOCUMENT_SECTION_TITLE_CHARS",
    "MAX_MEMORY_UNIFIED_DIFF_CHARS",
    "MEMORY_DOCUMENT_SECTIONS",
    "MIN_MEMORY_DOCUMENT_SECTIONS",
    "MemoryDocumentRecord",
    "MemoryDocumentState",
    "MemoryDocumentVersionRecord",
    "MemoryDocumentInvalid",
    "MemoryDocumentOverBudget",
    "estimate_memory_tokens",
    "memory_document_diff_preview",
    "memory_document_digest",
    "memory_document_unified_diff",
    "render_empty_memory_document",
    "target_memory_character_limit",
    "target_memory_token_limit",
    "validate_memory_document",
    "validate_memory_document_structure",
    "validate_memory_document_sections",
]
