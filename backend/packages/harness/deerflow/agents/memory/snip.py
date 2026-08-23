"""Fixed SNIP prompt, output contract, and source identity helpers."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from importlib import resources
from typing import Any, Literal, TypedDict

from deerflow.config.model_execution import SystemModelExecutionProvenance
from deerflow.memory_contract.history import (
    MAX_SNIP_OUTPUT_CHARS,
    SNIP_NOTHING,
    SnipOutputInvalid,
    compute_snip_content_digest,
    normalize_snip_output,
    validate_snip_line,
    validate_snip_output,
)

logger = logging.getLogger(__name__)

MAX_CONTINUITY_CHARS = 2000
SNIP_ARCHIVE_PROMPT_VERSION = "snip-archive-prompt-v2"
MEMORY_ARCHIVE_CONTEXT_KEY = "__memory_archive_context"
MEMORY_ARCHIVE_RECEIPT_KEY = "memory_archive_receipt"
MEMORY_ARCHIVE_RECEIPT_VERSION = "memory-archive-receipt-v1"

_SOURCE_DIGEST_DOMAIN = "deerflow.snip.source.v1"
_CONTINUITY_OPEN = "<continuity>"
_CONTINUITY_CLOSE = "</continuity>"
_CONTINUITY_TRUNCATION_MARKER = "\n…\n"

# Appended verbatim for the single bounded repair retry after SnipOutputInvalid.
# It restates the output contract and never echoes the invalid output.
SNIP_RETRY_REINFORCEMENT = (
    "Your previous response did not match the required output format. "
    "Respond again from scratch with exactly two segments. First output one "
    f'"{_CONTINUITY_OPEN}" block containing a non-empty free-prose task-continuity '
    f'summary within {MAX_CONTINUITY_CHARS} characters, closed by "{_CONTINUITY_CLOSE}". '
    "After the closing tag, output only lines of the exact form "
    '"- [permanent|durable|ephemeral|correction|skip] fact text", or the '
    f'single line "{SNIP_NOTHING}" if there is nothing worth keeping. '
    f"Keep the tagged lines within {MAX_SNIP_OUTPUT_CHARS} characters. "
    "No other prose, no code fences, no headings."
)


def _load_prompt() -> str:
    return resources.files("deerflow.agents.memory").joinpath("prompts", "snip_archive.md").read_text(encoding="utf-8")


SNIP_ARCHIVE_PROMPT = _load_prompt()


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
    summary_model_config_id: str
    summary_model_payload_checksum: str
    summary_model_secret_generation_id: str | None
    summary_model_secret_envelope_digest: str | None


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
    summary_model: SystemModelExecutionProvenance | None
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
        if self.summary_model is not None and not isinstance(
            self.summary_model,
            SystemModelExecutionProvenance,
        ):
            raise ValueError("SNIP summary model provenance is invalid")
        if self.enabled and self.summary_model is None:
            raise ValueError("Enabled SNIP archive requires model provenance")
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "owner_user_id", owner_user_id)
        object.__setattr__(self, "namespace", namespace)


def parse_snip_dual_output(raw: str) -> tuple[str, str]:
    """Split one dual-segment SNIP response into ``(continuity, tagged_text)``.

    The continuity segment is bounded free prose for Thread continuation and
    only ever flows into ``summary_text``. Every tagged fact keeps the exact
    single-segment line grammar. If an otherwise-valid multi-line tagged segment
    exceeds its aggregate bound, only its complete-line prefix is retained; an
    overlong individual line or invalid tail still fails closed.
    """

    normalized = normalize_snip_output(raw)
    if not normalized.startswith(_CONTINUITY_OPEN):
        raise SnipOutputInvalid("SNIP output does not start with a continuity segment")
    close_index = normalized.find(_CONTINUITY_CLOSE)
    if close_index < 0:
        raise SnipOutputInvalid("SNIP continuity segment is unterminated")
    continuity = normalized[len(_CONTINUITY_OPEN) : close_index].strip()
    remainder = normalized[close_index + len(_CONTINUITY_CLOSE) :]
    if not continuity:
        raise SnipOutputInvalid("SNIP continuity segment is empty")
    if _CONTINUITY_OPEN in continuity or _CONTINUITY_OPEN in remainder or _CONTINUITY_CLOSE in remainder:
        raise SnipOutputInvalid("SNIP output has more than one continuity segment")
    tagged_text = _validate_and_bound_tagged_text(remainder)
    if len(continuity) > MAX_CONTINUITY_CHARS:
        logger.warning(
            "SNIP continuity exceeded the character limit; applying bounded head-tail fallback (actual=%d, max=%d)",
            len(continuity),
            MAX_CONTINUITY_CHARS,
        )
        content_budget = MAX_CONTINUITY_CHARS - len(
            _CONTINUITY_TRUNCATION_MARKER,
        )
        head_length = content_budget * 2 // 3
        tail_length = content_budget - head_length
        continuity = continuity[:head_length] + _CONTINUITY_TRUNCATION_MARKER + continuity[-tail_length:]
    return continuity, tagged_text


def _validate_and_bound_tagged_text(raw: str) -> str:
    """Validate every tagged line before bounding an oversized line sequence."""

    normalized = normalize_snip_output(raw)
    if len(normalized) <= MAX_SNIP_OUTPUT_CHARS:
        return validate_snip_output(normalized)

    lines = normalized.split("\n")
    for line in lines:
        if line:
            validate_snip_line(line)

    kept_lines: list[str] = []
    kept_length = 0
    for line in lines:
        added_length = len(line) + (1 if kept_lines else 0)
        if kept_length + added_length > MAX_SNIP_OUTPUT_CHARS:
            break
        kept_lines.append(line)
        kept_length += added_length

    bounded = "\n".join(kept_lines)
    if not bounded:
        raise SnipOutputInvalid("SNIP output is empty or over the character limit")
    logger.warning(
        "SNIP tagged facts exceeded the character limit; retaining a bounded complete-line prefix (actual=%d, max=%d, kept_lines=%d, total_lines=%d)",
        len(normalized),
        MAX_SNIP_OUTPUT_CHARS,
        len(kept_lines),
        len(lines),
    )
    return validate_snip_output(bounded)


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
    if context.summary_model is None:
        raise ValueError("SNIP archive model provenance is missing")
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
        summary_model_config_id=str(context.summary_model.model_config_id),
        summary_model_payload_checksum=context.summary_model.payload_checksum,
        summary_model_secret_generation_id=(str(context.summary_model.secret_generation_id) if context.summary_model.secret_generation_id is not None else None),
        summary_model_secret_envelope_digest=(context.summary_model.secret_envelope_digest),
    )


__all__ = [
    "MAX_CONTINUITY_CHARS",
    "MAX_SNIP_OUTPUT_CHARS",
    "MEMORY_ARCHIVE_CONTEXT_KEY",
    "MEMORY_ARCHIVE_RECEIPT_KEY",
    "MEMORY_ARCHIVE_RECEIPT_VERSION",
    "MemoryArchiveReceipt",
    "SNIP_ARCHIVE_PROMPT",
    "SNIP_ARCHIVE_PROMPT_VERSION",
    "SNIP_NOTHING",
    "SNIP_RETRY_REINFORCEMENT",
    "SnipOutputInvalid",
    "SnipArchiveContext",
    "build_memory_archive_receipt",
    "compute_snip_content_digest",
    "compute_snip_source_digest",
    "normalize_snip_output",
    "parse_snip_dual_output",
    "validate_snip_line",
    "validate_snip_output",
]
