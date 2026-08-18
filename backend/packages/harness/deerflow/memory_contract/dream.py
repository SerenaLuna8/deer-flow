"""Pure Dream state and frozen-input contracts."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from deerflow.memory_contract.common import MemoryDocumentScope

BUDGET_REWRITE_HISTORY_DIGEST = hashlib.sha256(b"deerflow.dream.budget_rewrite.empty.v1").hexdigest()
DREAM_PROMPT_VERSION = "dream-prompt-v5"

MemoryDreamTrigger = Literal["auto_dream", "manual_dream", "budget_rewrite"]
MemoryDreamAdmissionDisposition = Literal[
    "queued",
    "already_running",
    "nothing_pending",
]
MemoryDreamAdmissionKind = Literal["history", "budget_rewrite"]
MemoryDreamReleaseDisposition = Literal[
    "already_published",
    "retry_wait",
    "cancelled",
    "dead",
]

# A published version is flagged for review when the previous document had at
# least this many content lines and at least this fraction were purely deleted,
# unless the consumed batch carried explicit correction evidence.
MEMORY_REVIEW_MIN_LINES = 8
MEMORY_REVIEW_DELETION_RATIO = 0.4


@dataclass(frozen=True, slots=True)
class MemoryBudgetRewriteScanCursor:
    updated_at: datetime
    project_id: uuid.UUID
    owner_user_id: str
    namespace: str

    def __post_init__(self) -> None:
        if not isinstance(self.updated_at, datetime) or self.updated_at.tzinfo is None:
            raise ValueError("Budget rewrite cursor timestamp is invalid")
        scope = MemoryDocumentScope(
            project_id=self.project_id,
            owner_user_id=self.owner_user_id,
            namespace=self.namespace,
        )
        object.__setattr__(self, "project_id", scope.project_id)
        object.__setattr__(self, "owner_user_id", scope.owner_user_id)
        object.__setattr__(self, "namespace", scope.namespace)


@dataclass(frozen=True, slots=True)
class MemoryBudgetRewriteScopePage:
    scopes: tuple[MemoryDocumentScope, ...]
    next_cursor: MemoryBudgetRewriteScanCursor | None


@dataclass(frozen=True, slots=True)
class MemoryDreamFrozenRuntime:
    preference_version: int
    policy_revision: int
    model_config_id: uuid.UUID
    model_version_id: uuid.UUID
    model_payload_checksum: str
    prompt_version: str

    def __post_init__(self) -> None:
        if (
            type(self.preference_version) is not int
            or self.preference_version < 1
            or type(self.policy_revision) is not int
            or self.policy_revision < 1
            or not isinstance(self.model_config_id, uuid.UUID)
            or not isinstance(self.model_version_id, uuid.UUID)
            or not isinstance(self.model_payload_checksum, str)
            or len(self.model_payload_checksum) != 64
            or not isinstance(self.prompt_version, str)
            or not self.prompt_version
            or len(self.prompt_version) > 64
        ):
            raise ValueError("Dream frozen runtime is invalid")


@dataclass(frozen=True, slots=True)
class MemoryDreamAdmissionRecord:
    disposition: MemoryDreamAdmissionDisposition
    job_id: uuid.UUID | None
    history_count: int
    admission_kind: MemoryDreamAdmissionKind = "history"


@dataclass(frozen=True, slots=True)
class MemoryDreamHistoryRecord:
    id: uuid.UUID
    sequence: int
    tagged_text: str | None
    content_digest: str
    origin: str = "snip"


@dataclass(frozen=True, slots=True)
class MemoryDreamWork:
    job_id: uuid.UUID
    project_id: uuid.UUID
    owner_user_id: str
    namespace: str
    trigger: MemoryDreamTrigger
    history_from: int | None
    history_to: int | None
    history_count: int
    history_digest: str
    base_document_version: int
    base_content: str
    base_content_digest: str
    sections: tuple[str, ...]
    sections_policy_version_id: uuid.UUID
    preference_version: int
    policy_revision: int
    model_config_id: uuid.UUID
    model_version_id: uuid.UUID
    model_payload_checksum: str
    prompt_version: str
    result_version: int | None
    cancel_requested: bool
    job_status: str
    history: tuple[MemoryDreamHistoryRecord, ...]


@dataclass(frozen=True, slots=True)
class MemoryDreamReleaseResult:
    disposition: MemoryDreamReleaseDisposition

    def __post_init__(self) -> None:
        if self.disposition not in {
            "already_published",
            "retry_wait",
            "cancelled",
            "dead",
        }:
            raise ValueError("Dream release disposition is invalid")


def compute_dream_history_digest(
    history: tuple[MemoryDreamHistoryRecord, ...],
) -> str:
    if not history or len(history) > 20:
        raise ValueError("Dream history batch is invalid")
    if any(current.sequence >= following.sequence for current, following in zip(history, history[1:], strict=False)):
        raise ValueError("Dream history batch is not strictly ordered")
    payload = [
        {
            "content_digest": item.content_digest,
            "id": str(item.id),
            "sequence": item.sequence,
            "tagged_text": item.tagged_text,
        }
        for item in history
    ]
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _document_content_lines(content: str) -> list[str]:
    return [line.strip() for line in content.splitlines() if line.strip() and not line.lstrip().startswith("# ")]


def memory_document_deletion_ratio(
    previous: str,
    replacement: str,
) -> float | None:
    """Return the fraction of previous content lines that disappeared."""

    if not isinstance(previous, str) or not isinstance(replacement, str):
        raise TypeError("Memory documents must be text")
    previous_lines = _document_content_lines(previous)
    if len(previous_lines) < MEMORY_REVIEW_MIN_LINES:
        return None
    replacement_lines = set(_document_content_lines(replacement))
    deleted = sum(1 for line in previous_lines if line not in replacement_lines)
    return deleted / len(previous_lines)


def memory_document_needs_review(
    previous: str,
    replacement: str,
    history: Iterable[MemoryDreamHistoryRecord],
) -> bool:
    """Flag a large deletion unless the batch requested a correction."""

    ratio = memory_document_deletion_ratio(previous, replacement)
    if ratio is None or ratio < MEMORY_REVIEW_DELETION_RATIO:
        return False
    for item in history:
        text = item.tagged_text or ""
        for line in text.splitlines():
            stripped = line.lstrip().removeprefix("- ")
            if stripped.startswith("[correction]"):
                return False
    return True


__all__ = [
    "BUDGET_REWRITE_HISTORY_DIGEST",
    "DREAM_PROMPT_VERSION",
    "MEMORY_REVIEW_DELETION_RATIO",
    "MEMORY_REVIEW_MIN_LINES",
    "MemoryBudgetRewriteScanCursor",
    "MemoryBudgetRewriteScopePage",
    "MemoryDreamAdmissionDisposition",
    "MemoryDreamAdmissionKind",
    "MemoryDreamAdmissionRecord",
    "MemoryDreamFrozenRuntime",
    "MemoryDreamHistoryRecord",
    "MemoryDreamReleaseDisposition",
    "MemoryDreamReleaseResult",
    "MemoryDreamTrigger",
    "MemoryDreamWork",
    "compute_dream_history_digest",
    "memory_document_deletion_ratio",
    "memory_document_needs_review",
]
