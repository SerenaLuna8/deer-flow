"""Pure Memory history and explicit remember contracts."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from deerflow.config.model_execution import SystemModelExecutionProvenance
from deerflow.memory_contract.common import MemoryDocumentScope

EPISODE_SEARCH_TAGS: tuple[str, ...] = (
    "permanent",
    "durable",
    "ephemeral",
    "correction",
)
MAX_REMEMBER_CONTENT_CHARS = 500
REMEMBER_RUN_LIMIT = 5
REMEMBER_BACKLOG_LIMIT = 200
REMEMBER_PROMPT_VERSION = "remember-tool-v1"
SNIP_NOTHING = "(nothing)"
MAX_SNIP_OUTPUT_CHARS = 1000
_REMEMBER_SOURCE_DOMAIN = "deerflow.remember.source.v1"
_VALID_SNIP_LINE = re.compile(r"^- \[(?:permanent|durable|ephemeral|correction|skip)\] \S(?:.*\S)?$")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class SnipOutputInvalid(ValueError):
    """Raised when a model response violates the fixed SNIP contract."""


def normalize_snip_output(raw: str) -> str:
    if not isinstance(raw, str):
        raise SnipOutputInvalid("SNIP output must be text")
    return raw.replace("\r\n", "\n").replace("\r", "\n").strip()


def validate_snip_output(raw: str) -> str:
    normalized = normalize_snip_output(raw)
    if not normalized or len(normalized) > MAX_SNIP_OUTPUT_CHARS:
        raise SnipOutputInvalid("SNIP output is empty or over the character limit")
    if normalized == SNIP_NOTHING:
        return normalized
    if any(line and _VALID_SNIP_LINE.fullmatch(line) is None for line in normalized.split("\n")):
        raise SnipOutputInvalid("SNIP output has an invalid line")
    return normalized


def validate_snip_line(line: str) -> str:
    if not isinstance(line, str) or len(line) > MAX_SNIP_OUTPUT_CHARS or _VALID_SNIP_LINE.fullmatch(line) is None:
        raise SnipOutputInvalid("SNIP line grammar is invalid")
    return line


def compute_snip_content_digest(tagged_text: str) -> str:
    normalized = validate_snip_output(tagged_text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def compute_remember_source_digest(
    *,
    run_id: str,
    tool_call_id: str,
    content: str,
) -> str:
    payload = {
        "content": content,
        "domain": _REMEMBER_SOURCE_DOMAIN,
        "run_id": run_id,
        "tool_call_id": tool_call_id,
    }
    canonical = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


MemoryHistoryActivationStatus = Literal[
    "created",
    "pending",
    "processing",
    "consumed",
    "stale",
]
MemoryProposalDisposition = Literal[
    "recorded",
    "duplicate",
    "memory_disabled",
    "run_limit_reached",
    "backlog_full",
]


@dataclass(frozen=True, slots=True)
class MemoryHistoryActivation:
    scope: MemoryDocumentScope
    thread_id: str
    source_checkpoint_id: str
    committed_checkpoint_id: str
    source_digest: str
    tagged_text: str
    content_digest: str
    preference_version: int
    snip_prompt_version: str
    summary_model: SystemModelExecutionProvenance

    def __post_init__(self) -> None:
        try:
            tagged_text = validate_snip_output(self.tagged_text)
        except (TypeError, ValueError):
            raise ValueError("Memory history activation is invalid") from None
        if (
            type(self.scope) is not MemoryDocumentScope
            or not isinstance(
                self.summary_model,
                SystemModelExecutionProvenance,
            )
            or not isinstance(self.thread_id, str)
            or not self.thread_id
            or len(self.thread_id) > 64
            or not isinstance(self.source_checkpoint_id, str)
            or not self.source_checkpoint_id
            or len(self.source_checkpoint_id) > 128
            or not isinstance(self.committed_checkpoint_id, str)
            or not self.committed_checkpoint_id
            or len(self.committed_checkpoint_id) > 128
            or not isinstance(self.source_digest, str)
            or _SHA256_HEX.fullmatch(self.source_digest) is None
            or tagged_text == SNIP_NOTHING
            or not isinstance(self.content_digest, str)
            or _SHA256_HEX.fullmatch(self.content_digest) is None
            or self.content_digest != compute_snip_content_digest(tagged_text)
            or type(self.preference_version) is not int
            or self.preference_version < 1
            or not isinstance(self.snip_prompt_version, str)
            or not self.snip_prompt_version
            or len(self.snip_prompt_version) > 64
        ):
            raise ValueError("Memory history activation is invalid")
        object.__setattr__(self, "tagged_text", tagged_text)


@dataclass(frozen=True, slots=True)
class MemoryHistoryActivationResult:
    status: MemoryHistoryActivationStatus
    entry_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class MemoryRememberProposal:
    scope: MemoryDocumentScope
    thread_id: str
    run_id: str
    tool_call_id: str
    kind: str
    content: str

    def __post_init__(self) -> None:
        if type(self.scope) is not MemoryDocumentScope:
            raise ValueError("Memory proposal requires a memory scope")
        if (
            not isinstance(self.thread_id, str)
            or not self.thread_id
            or len(self.thread_id) > 64
            or not isinstance(self.run_id, str)
            or not self.run_id
            or len(self.run_id) > 64
            or not isinstance(self.tool_call_id, str)
            or not self.tool_call_id
            or len(self.tool_call_id) > 128
            or self.kind not in EPISODE_SEARCH_TAGS
            or not isinstance(self.content, str)
            or _CONTROL_CHARS.search(self.content)
        ):
            raise ValueError("Memory proposal input is invalid")
        content = self.content.strip()
        if not content or len(content) > MAX_REMEMBER_CONTENT_CHARS:
            raise ValueError("Memory proposal content must be one bounded line")
        try:
            validate_snip_line(f"- [{self.kind}] {content}")
        except ValueError:
            raise ValueError("Memory proposal does not form a valid tagged line") from None
        object.__setattr__(self, "content", content)

    @property
    def tagged_text(self) -> str:
        return f"- [{self.kind}] {self.content}"

    @property
    def source_digest(self) -> str:
        return compute_remember_source_digest(
            run_id=self.run_id,
            tool_call_id=self.tool_call_id,
            content=self.content,
        )


@dataclass(frozen=True, slots=True)
class MemoryProposalOutcome:
    disposition: MemoryProposalDisposition
    entry_id: uuid.UUID | None
    tagged_text: str | None


@dataclass(frozen=True, slots=True)
class MemoryPendingEntryRecord:
    sequence: int
    origin: str
    tagged_text: str
    created_at: datetime


__all__ = [
    "EPISODE_SEARCH_TAGS",
    "MAX_REMEMBER_CONTENT_CHARS",
    "MemoryHistoryActivation",
    "MemoryHistoryActivationResult",
    "MemoryHistoryActivationStatus",
    "MemoryPendingEntryRecord",
    "MemoryProposalDisposition",
    "MemoryProposalOutcome",
    "MemoryRememberProposal",
    "REMEMBER_BACKLOG_LIMIT",
    "REMEMBER_PROMPT_VERSION",
    "REMEMBER_RUN_LIMIT",
    "SnipOutputInvalid",
    "compute_remember_source_digest",
    "compute_snip_content_digest",
    "normalize_snip_output",
    "validate_snip_line",
    "validate_snip_output",
]
