"""Fixed SNIP prompt, output contract, and source identity helpers."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from importlib import resources
from typing import Any, Literal, TypedDict

MAX_SNIP_OUTPUT_CHARS = 1000
SNIP_ARCHIVE_PROMPT_VERSION = "snip-archive-prompt-v1"
SNIP_NOTHING = "(nothing)"
MEMORY_ARCHIVE_CONTEXT_KEY = "__memory_archive_context"
MEMORY_ARCHIVE_RECEIPT_KEY = "memory_archive_receipt"
MEMORY_ARCHIVE_RECEIPT_VERSION = "memory-archive-receipt-v1"

_SOURCE_DIGEST_DOMAIN = "deerflow.snip.source.v1"
_VALID_SNIP_LINE = re.compile(r"^- \[(?:permanent|durable|ephemeral|correction|skip)\] \S(?:.*\S)?$")


def _load_prompt() -> str:
    return resources.files("deerflow.agents.memory").joinpath("prompts", "snip_archive.md").read_text(encoding="utf-8")


SNIP_ARCHIVE_PROMPT = _load_prompt()


class SnipOutputInvalid(ValueError):
    """Raised when a model response does not satisfy the fixed SNIP contract."""


class MemoryArchiveReceipt(TypedDict):
    """Server-authored checkpoint handshake for one committed SNIP output."""

    version: Literal["memory-archive-receipt-v1"]
    project_id: str
    owner_user_id: str
    namespace: str
    thread_id: str
    source_checkpoint_id: str
    source_digest: str
    tagged_text: str
    content_digest: str
    preference_version: int
    snip_prompt_version: str
    summary_model_ref: str


@dataclass(frozen=True, slots=True)
class SnipArchiveContext:
    """Unforgeable per-run inputs used to author a checkpoint receipt.

    This object is installed only by Gateway/Worker code. JSON graph input can
    never recreate its exact type, which keeps receipt authority separate from
    client state while still allowing Memory-disabled compaction.
    """

    enabled: bool
    project_id: uuid.UUID
    owner_user_id: str
    namespace: str
    preference_version: int
    summary_model_ref: uuid.UUID | None
    source_checkpoint_id: str | None = None

    def __post_init__(self) -> None:
        try:
            project_id = uuid.UUID(str(self.project_id))
            owner_user_id = str(uuid.UUID(str(self.owner_user_id)))
        except (TypeError, ValueError):
            raise ValueError("SNIP archive scope is invalid") from None
        namespace = self.namespace.strip() if isinstance(self.namespace, str) else ""
        if (
            type(self.enabled) is not bool
            or not namespace
            or len(namespace) > 255
            or type(self.preference_version) is not int
            or self.preference_version < 1
            or (self.source_checkpoint_id is not None and (not isinstance(self.source_checkpoint_id, str) or not self.source_checkpoint_id or len(self.source_checkpoint_id) > 128))
        ):
            raise ValueError("SNIP archive context is invalid")
        summary_model_ref = self.summary_model_ref
        if summary_model_ref is not None:
            try:
                summary_model_ref = uuid.UUID(str(summary_model_ref))
            except (TypeError, ValueError):
                raise ValueError("SNIP summary model reference is invalid") from None
        if self.enabled and summary_model_ref is None:
            raise ValueError("Enabled SNIP archive requires an exact model version")
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "owner_user_id", owner_user_id)
        object.__setattr__(self, "namespace", namespace)
        object.__setattr__(self, "summary_model_ref", summary_model_ref)


def normalize_snip_output(raw: str) -> str:
    """Apply the only permitted SNIP output normalization.

    Newlines are converted to LF and whitespace surrounding the complete output
    is removed. Individual lines and fact ordering are otherwise untouched.
    """

    if not isinstance(raw, str):
        raise SnipOutputInvalid("SNIP output must be text")
    return raw.replace("\r\n", "\n").replace("\r", "\n").strip()


def validate_snip_output(raw: str) -> str:
    """Return the normalized SNIP text or fail closed on any contract drift."""

    normalized = normalize_snip_output(raw)
    if not normalized or len(normalized) > MAX_SNIP_OUTPUT_CHARS:
        raise SnipOutputInvalid("SNIP output is empty or over the character limit")
    if normalized == SNIP_NOTHING:
        return normalized
    if any(line and _VALID_SNIP_LINE.fullmatch(line) is None for line in normalized.split("\n")):
        raise SnipOutputInvalid("SNIP output has an invalid line")
    return normalized


def _message_identity(message: object) -> dict[str, Any]:
    if isinstance(message, Mapping):
        if "id" not in message or "content" not in message:
            raise ValueError("SNIP source message is invalid")
        message_id = message["id"]
        content = message["content"]
    else:
        try:
            message_id = getattr(message, "id")
            content = getattr(message, "content")
        except AttributeError:
            raise ValueError("SNIP source message is invalid") from None

    if message_id is not None and (not isinstance(message_id, str) or not message_id):
        raise ValueError("SNIP source message ID is invalid")
    if not isinstance(content, str | list):
        raise ValueError("SNIP source message content is invalid")
    return {"content": content, "id": message_id}


def compute_snip_source_digest(
    *,
    previous_summary: str | None,
    source_checkpoint_id: str,
    messages: Iterable[object],
) -> str:
    """Hash the exact ordered source used by one SNIP compaction attempt.

    Only server-bound source identity is included: the prior cumulative summary,
    source checkpoint ID, and each selected message's ID and content in order.
    Canonical JSON makes mapping key order irrelevant while preserving list and
    message order. Unsupported/non-JSON values fail closed instead of acquiring
    a process-dependent ``repr``.
    """

    if previous_summary is not None and not isinstance(previous_summary, str):
        raise ValueError("SNIP previous summary is invalid")
    if not isinstance(source_checkpoint_id, str) or not source_checkpoint_id:
        raise ValueError("SNIP source checkpoint ID is invalid")
    if isinstance(messages, str | bytes | Mapping) or not isinstance(messages, Iterable):
        raise ValueError("SNIP source messages are invalid")

    identities = [_message_identity(message) for message in messages]
    if not identities:
        raise ValueError("SNIP source messages are empty")
    payload = {
        "domain": _SOURCE_DIGEST_DOMAIN,
        "messages": identities,
        "previous_summary": previous_summary,
        "source_checkpoint_id": source_checkpoint_id,
    }
    try:
        canonical = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ValueError("SNIP source content is not canonical JSON") from None
    return hashlib.sha256(canonical).hexdigest()


def compute_snip_content_digest(tagged_text: str) -> str:
    """Return the identity retained after a history entry becomes a tombstone."""

    normalized = validate_snip_output(tagged_text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def build_memory_archive_receipt(
    context: SnipArchiveContext | None,
    *,
    thread_id: str,
    source_checkpoint_id: str | None,
    previous_summary: str | None,
    messages: Iterable[object],
    tagged_text: str,
) -> MemoryArchiveReceipt | None:
    """Build one server-bound receipt, or ``None`` when archiving is disabled."""

    normalized = validate_snip_output(tagged_text)
    if context is None or not context.enabled or normalized == SNIP_NOTHING:
        return None
    if type(context) is not SnipArchiveContext:
        raise ValueError("SNIP archive context is invalid")
    if not isinstance(thread_id, str) or not thread_id or len(thread_id) > 64:
        raise ValueError("SNIP archive Thread ID is invalid")
    exact_source_checkpoint_id = source_checkpoint_id or context.source_checkpoint_id
    if not isinstance(exact_source_checkpoint_id, str) or not exact_source_checkpoint_id or len(exact_source_checkpoint_id) > 128:
        raise ValueError("SNIP archive source checkpoint is invalid")
    if context.summary_model_ref is None:
        raise ValueError("SNIP archive model version is missing")
    return MemoryArchiveReceipt(
        version=MEMORY_ARCHIVE_RECEIPT_VERSION,
        project_id=str(context.project_id),
        owner_user_id=context.owner_user_id,
        namespace=context.namespace,
        thread_id=thread_id,
        source_checkpoint_id=exact_source_checkpoint_id,
        source_digest=compute_snip_source_digest(
            previous_summary=previous_summary,
            source_checkpoint_id=exact_source_checkpoint_id,
            messages=messages,
        ),
        tagged_text=normalized,
        content_digest=compute_snip_content_digest(normalized),
        preference_version=context.preference_version,
        snip_prompt_version=SNIP_ARCHIVE_PROMPT_VERSION,
        summary_model_ref=str(context.summary_model_ref),
    )


__all__ = [
    "MAX_SNIP_OUTPUT_CHARS",
    "MEMORY_ARCHIVE_CONTEXT_KEY",
    "MEMORY_ARCHIVE_RECEIPT_KEY",
    "MEMORY_ARCHIVE_RECEIPT_VERSION",
    "MemoryArchiveReceipt",
    "SNIP_ARCHIVE_PROMPT",
    "SNIP_ARCHIVE_PROMPT_VERSION",
    "SNIP_NOTHING",
    "SnipOutputInvalid",
    "SnipArchiveContext",
    "build_memory_archive_receipt",
    "compute_snip_content_digest",
    "compute_snip_source_digest",
    "normalize_snip_output",
    "validate_snip_output",
]
