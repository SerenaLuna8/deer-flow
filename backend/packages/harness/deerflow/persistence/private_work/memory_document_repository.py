"""Scoped repository for the final Memory document model."""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.agents.memory.dream import (
    MAX_MEMORY_DOCUMENT_CHARS,
    render_empty_memory_document,
    validate_memory_document,
    validate_memory_document_sections,
)
from deerflow.agents.memory.snip import (
    SNIP_NOTHING,
    compute_snip_content_digest,
    validate_snip_line,
    validate_snip_output,
)
from deerflow.persistence.jobs.model import JobRow
from deerflow.persistence.jobs.sql import EnqueueJob, JobRepository, JobScope
from deerflow.persistence.private_work.memory_document_model import (
    MemoryDocumentRow,
    MemoryDocumentVersionRow,
    MemoryDreamRunRow,
    MemoryEpisodeRow,
    MemoryHistoryEntryRow,
    RunMemoryContextSnapshotRow,
)
from deerflow.persistence.system_settings import SystemModelConfigVersionRow
from deerflow.persistence.user.model import UserRow

DEFAULT_MEMORY_NAMESPACE = "default"
# Platform default until `episode_retention_days` ships as a Memory policy
# field; ``0`` keeps episodes forever.
DEFAULT_EPISODE_RETENTION_DAYS = 365
_EPISODE_PRUNE_BATCH_LIMIT = 500
# Closed tag vocabulary shared by the recall tool, the episodes API, and the
# SNIP line contract (``skip`` never reaches storage).
EPISODE_SEARCH_TAGS: tuple[str, ...] = (
    "permanent",
    "durable",
    "ephemeral",
    "correction",
)
MAX_EPISODE_QUERY_CHARS = 200
# Explicit floor instead of the pg_trgm GUC threshold (0.3): recall favors
# finding loosely related notes, and an explicit constant keeps the filter
# deterministic across environments.
EPISODE_SIMILARITY_FLOOR = 0.1
# Contract for tool-originated history rows written by the `remember` tool.
REMEMBER_PROMPT_VERSION = "remember-tool-v1"
MAX_REMEMBER_CONTENT_CHARS = 500
REMEMBER_RUN_LIMIT = 5
REMEMBER_BACKLOG_LIMIT = 200
_REMEMBER_SOURCE_DOMAIN = "deerflow.remember.source.v1"
# One Dream consumes at most this many pending entries; a full batch makes a
# scope due immediately instead of waiting out the interval.
DREAM_HISTORY_BATCH_SIZE = 20
# A pending `remember` proposal makes its scope due after this many minutes,
# so explicit user requests reach the document within one scheduler cycle
# plus this grace window.
TOOL_ENTRY_DUE_MINUTES = 10
# A published version is flagged for review when the previous document had at
# least this many content lines and at least this fraction of them were purely
# deleted, unless the consumed batch carried an explicit `[correction]` line.
MEMORY_REVIEW_MIN_LINES = 8
MEMORY_REVIEW_DELETION_RATIO = 0.4
# `budget_rewrite` freezes zero history rows; its digest column carries this
# domain-separated sentinel instead of a batch hash.
BUDGET_REWRITE_HISTORY_DIGEST = hashlib.sha256(b"deerflow.dream.budget_rewrite.empty.v1").hexdigest()


def compute_remember_source_digest(
    *,
    run_id: str,
    tool_call_id: str,
    content: str,
) -> str:
    """Hash the server-bound identity of one remember proposal.

    Canonical JSON keeps field boundaries unambiguous, so no concatenation of
    ``run_id``/``tool_call_id``/``content`` can collide across fields.
    """

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


def validate_episode_retention_days(days: int) -> int:
    """Enforce the platform retention contract: ``0`` or ``30..3650`` days."""

    if type(days) is not int or (days != 0 and not 30 <= days <= 3650):
        raise ValueError("Episode retention days are out of contract")
    return days


def _document_content_lines(content: str) -> list[str]:
    """Content lines of a document: non-empty and not a section heading."""

    return [line.strip() for line in content.splitlines() if line.strip() and not line.lstrip().startswith("# ")]


def memory_document_deletion_ratio(previous: str, replacement: str) -> float | None:
    """Fraction of previous content lines that vanished from the replacement.

    Returns ``None`` when the previous document is too small for the ratio to
    be meaningful (fewer than ``MEMORY_REVIEW_MIN_LINES`` content lines).
    """

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
    history: tuple[MemoryDreamHistoryRecord, ...],
) -> bool:
    """Zero-cost heuristic for flagging a large-deletion Dream settlement.

    An explicit ``[correction]`` line anywhere in the consumed batch means the
    user asked for the removal, so the flag stays off.
    """

    ratio = memory_document_deletion_ratio(previous, replacement)
    if ratio is None or ratio < MEMORY_REVIEW_DELETION_RATIO:
        return False
    for item in history:
        text = item.tagged_text or ""
        for line in text.splitlines():
            stripped = line.lstrip()
            stripped = stripped.removeprefix("- ")
            if stripped.startswith("[correction]"):
                return False
    return True


def _escape_like_pattern(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _validated_episode_tags(tags: object) -> tuple[str, ...]:
    if not isinstance(tags, (tuple, list)):
        raise ValueError("Episode tags must be a sequence")
    normalized: list[str] = []
    for tag in tags:
        if tag not in EPISODE_SEARCH_TAGS:
            raise ValueError("Episode tag is out of contract")
        if tag not in normalized:
            normalized.append(tag)
    return tuple(normalized)


class MemoryDocumentNotFound(LookupError):
    pass


class MemoryDocumentConflict(RuntimeError):
    pass


MemoryDreamTrigger = Literal["auto_dream", "manual_dream", "budget_rewrite"]
MemoryDreamAdmissionDisposition = Literal[
    "queued",
    "already_running",
    "nothing_pending",
]
MemoryDreamAdmissionKind = Literal["history", "budget_rewrite"]
MemoryHistoryActivationStatus = Literal[
    "created",
    "pending",
    "processing",
    "consumed",
    "stale",
]

_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class MemoryDocumentScope:
    project_id: uuid.UUID
    owner_user_id: str
    namespace: str = DEFAULT_MEMORY_NAMESPACE

    def __post_init__(self) -> None:
        try:
            project_id = uuid.UUID(str(self.project_id))
            owner_user_id = str(uuid.UUID(str(self.owner_user_id)))
        except (TypeError, ValueError):
            raise ValueError("Memory scope requires project and owner UUIDs") from None
        namespace = self.namespace.strip() if isinstance(self.namespace, str) else ""
        if not namespace or len(namespace) > 255:
            raise ValueError("Memory scope requires a bounded namespace")
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "owner_user_id", owner_user_id)
        object.__setattr__(self, "namespace", namespace)


@dataclass(frozen=True, slots=True)
class MemoryHistoryActivation:
    """Exact committed-checkpoint receipt accepted by the history repository."""

    scope: MemoryDocumentScope
    thread_id: str
    source_checkpoint_id: str
    committed_checkpoint_id: str
    source_digest: str
    tagged_text: str
    content_digest: str
    preference_version: int
    snip_prompt_version: str
    summary_model_ref: uuid.UUID

    def __post_init__(self) -> None:
        try:
            summary_model_ref = uuid.UUID(str(self.summary_model_ref))
        except (TypeError, ValueError):
            raise ValueError("Memory history model reference is invalid") from None
        try:
            tagged_text = validate_snip_output(self.tagged_text)
        except (TypeError, ValueError):
            raise ValueError("Memory history SNIP text is invalid") from None
        if (
            type(self.scope) is not MemoryDocumentScope
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
        object.__setattr__(self, "summary_model_ref", summary_model_ref)


@dataclass(frozen=True, slots=True)
class MemoryHistoryActivationResult:
    status: MemoryHistoryActivationStatus
    entry_id: uuid.UUID | None


MemoryProposalDisposition = Literal[
    "recorded",
    "duplicate",
    "memory_disabled",
    "run_limit_reached",
    "backlog_full",
]

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class MemoryRememberProposal:
    """One `remember` tool proposal bound to a live Run.

    The tagged line is derived server-side from the closed ``kind`` vocabulary
    and the single-line content, then revalidated against the SNIP line
    grammar so tool rows stay indistinguishable from SNIP rows for Dream.
    """

    scope: MemoryDocumentScope
    thread_id: str
    run_id: str
    tool_call_id: str
    kind: str
    content: str

    def __post_init__(self) -> None:
        if type(self.scope) is not MemoryDocumentScope:
            raise ValueError("Memory proposal requires a memory scope")
        if not isinstance(self.thread_id, str) or not self.thread_id or len(self.thread_id) > 64:
            raise ValueError("Memory proposal thread is invalid")
        if not isinstance(self.run_id, str) or not self.run_id or len(self.run_id) > 64:
            raise ValueError("Memory proposal run is invalid")
        if not isinstance(self.tool_call_id, str) or not self.tool_call_id or len(self.tool_call_id) > 128:
            raise ValueError("Memory proposal tool call is invalid")
        if self.kind not in EPISODE_SEARCH_TAGS:
            raise ValueError("Memory proposal kind is out of contract")
        if not isinstance(self.content, str):
            raise ValueError("Memory proposal content must be text")
        if _CONTROL_CHARS.search(self.content):
            raise ValueError("Memory proposal content must be one bounded line")
        content = self.content.strip()
        if not content or len(content) > MAX_REMEMBER_CONTENT_CHARS:
            raise ValueError("Memory proposal content must be one bounded line")
        try:
            validate_snip_line(f"- [{self.kind}] {content}")
        except (TypeError, ValueError):
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
class MemoryPendingEntryRecord:
    """One not-yet-consumed history entry, in Dream consumption order."""

    sequence: int
    origin: str
    tagged_text: str
    created_at: datetime


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
    # Origin is presentation metadata for Dream trust framing; it is
    # deliberately excluded from the frozen batch digest.
    origin: str = "snip"


@dataclass(frozen=True, slots=True)
class MemoryEpisodeRecord:
    """One archived episode returned by recall search or the browse API."""

    id: uuid.UUID
    thread_id: str
    origin: str
    tagged_text: str
    occurred_at: datetime
    created_at: datetime


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
class MemoryResetCounts:
    scopes_reset: int
    history_entries: int
    documents: int
    versions: int
    dream_runs: int
    snapshots: int
    episodes: int
    jobs_cancelled: int


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


class MemoryDocumentRepository:
    def __init__(
        self,
        session: AsyncSession,
        *,
        jobs: JobRepository | None = None,
    ) -> None:
        self.session = session
        self.jobs = jobs or JobRepository(session)

    @staticmethod
    def _scope_predicates(
        row_type,
        scope: MemoryDocumentScope,
    ) -> tuple[sa.ColumnElement[bool], ...]:
        return (
            row_type.project_id == scope.project_id,
            row_type.owner_user_id == scope.owner_user_id,
            row_type.namespace == scope.namespace,
        )

    @staticmethod
    def _latest_dream_activity(scope_row) -> sa.ColumnElement[datetime]:
        """Return the latest admission or Job transition for one Dream scope."""

        dream = MemoryDreamRunRow
        job = JobRow
        return (
            sa.select(sa.func.max(sa.func.greatest(dream.created_at, job.updated_at)))
            .select_from(dream)
            .join(job, job.id == dream.job_id)
            .where(
                dream.project_id == scope_row.project_id,
                dream.owner_user_id == scope_row.owner_user_id,
                dream.namespace == scope_row.namespace,
            )
            .correlate(scope_row)
            .scalar_subquery()
        )

    @staticmethod
    def _frozen_document_sections(
        row: MemoryDocumentRow,
    ) -> tuple[tuple[str, ...], uuid.UUID]:
        if row.sections_policy_section != "memory_document" or not isinstance(
            row.sections_policy_version_id,
            uuid.UUID,
        ):
            raise MemoryDocumentConflict("Memory document sections provenance is invalid")
        return (
            validate_memory_document_sections(row.sections),
            row.sections_policy_version_id,
        )

    @staticmethod
    def _document_record(row: MemoryDocumentRow | None) -> MemoryDocumentRecord:
        if row is None:
            return MemoryDocumentRecord(
                content="",
                content_digest="",
                sections=(),
                sections_policy_version_id=None,
                version=0,
                dream_cursor=0,
                active_dream_job_id=None,
                updated_at=None,
            )
        sections, sections_policy_version_id = MemoryDocumentRepository._frozen_document_sections(row)
        return MemoryDocumentRecord(
            content=row.content,
            content_digest=row.content_digest,
            sections=sections,
            sections_policy_version_id=sections_policy_version_id,
            version=int(row.version),
            dream_cursor=int(row.dream_cursor),
            active_dream_job_id=row.active_dream_job_id,
            updated_at=row.updated_at,
        )

    async def activate_history(
        self,
        activation: MemoryHistoryActivation,
    ) -> MemoryHistoryActivationResult:
        """Idempotently activate a receipt from one committed checkpoint.

        Account preference is locked and compared before touching history.  A
        reset or preference change therefore permanently stales receipts left
        in older checkpoints.  Existing processing/consumed entries are
        validated as immutable identities and are never moved backwards.
        """

        if type(activation) is not MemoryHistoryActivation:
            raise TypeError("MemoryHistoryActivation is required")
        preference = (
            await self.session.execute(
                sa.select(
                    UserRow.memory_enabled,
                    UserRow.preferences_version,
                )
                .where(UserRow.id == activation.scope.owner_user_id)
                .with_for_update(of=UserRow)
            )
        ).one_or_none()
        if preference is None or not bool(preference.memory_enabled) or int(preference.preferences_version) != activation.preference_version:
            return MemoryHistoryActivationResult(
                status="stale",
                entry_id=None,
            )

        inserted_id = await self.session.scalar(
            pg_insert(MemoryHistoryEntryRow)
            .values(
                project_id=activation.scope.project_id,
                owner_user_id=activation.scope.owner_user_id,
                namespace=activation.scope.namespace,
                thread_id=activation.thread_id,
                source_checkpoint_id=activation.source_checkpoint_id,
                committed_checkpoint_id=activation.committed_checkpoint_id,
                source_digest=activation.source_digest,
                status="pending",
                tagged_text=activation.tagged_text,
                content_digest=activation.content_digest,
                preference_version=activation.preference_version,
                snip_prompt_version=activation.snip_prompt_version,
                summary_model_ref=activation.summary_model_ref,
            )
            .on_conflict_do_nothing(
                index_elements=(
                    MemoryHistoryEntryRow.project_id,
                    MemoryHistoryEntryRow.owner_user_id,
                    MemoryHistoryEntryRow.namespace,
                    MemoryHistoryEntryRow.thread_id,
                    MemoryHistoryEntryRow.source_digest,
                )
            )
            .returning(MemoryHistoryEntryRow.id)
        )
        row = (
            await self.session.execute(
                sa.select(MemoryHistoryEntryRow)
                .where(
                    *self._scope_predicates(
                        MemoryHistoryEntryRow,
                        activation.scope,
                    ),
                    MemoryHistoryEntryRow.thread_id == activation.thread_id,
                    MemoryHistoryEntryRow.source_digest == activation.source_digest,
                )
                .with_for_update(of=MemoryHistoryEntryRow)
            )
        ).scalar_one_or_none()
        if row is None:
            raise MemoryDocumentConflict("Memory history activation disappeared")
        self._validate_activated_history(row, activation)
        await self.session.flush()
        if inserted_id is not None:
            return MemoryHistoryActivationResult(
                status="created",
                entry_id=row.id,
            )
        if row.status not in {"pending", "processing", "consumed"}:
            raise MemoryDocumentConflict("Memory history status is invalid")
        return MemoryHistoryActivationResult(
            status=row.status,
            entry_id=row.id,
        )

    @staticmethod
    def _validate_activated_history(
        row: MemoryHistoryEntryRow,
        activation: MemoryHistoryActivation,
    ) -> None:
        # ``committed_checkpoint_id`` intentionally keeps the first successful
        # child checkpoint. A retry from the same source may commit another
        # child while still carrying the identical source identity.
        if (
            row.project_id != activation.scope.project_id
            or row.owner_user_id != activation.scope.owner_user_id
            or row.namespace != activation.scope.namespace
            or row.thread_id != activation.thread_id
            or row.source_checkpoint_id != activation.source_checkpoint_id
            or row.source_digest != activation.source_digest
            or row.content_digest != activation.content_digest
            or int(row.preference_version) != activation.preference_version
            or row.snip_prompt_version != activation.snip_prompt_version
            or row.summary_model_ref != activation.summary_model_ref
            or row.status not in {"pending", "processing", "consumed"}
            or (row.status in {"pending", "processing"} and row.tagged_text != activation.tagged_text)
            or (row.status == "consumed" and row.tagged_text is not None)
        ):
            raise MemoryDocumentConflict("Memory history receipt conflicts")

    async def propose_entry(
        self,
        proposal: MemoryRememberProposal,
    ) -> MemoryProposalOutcome:
        """Record one tool-originated pending history entry, idempotently.

        The owner's preference row is locked first: this serializes every
        memory writer for the account, making the duplicate check and both
        caps race-free, and pins the ``preference_version`` recorded on the
        row. Replaying the same tool call is a ``duplicate`` before any cap
        applies, so retries never consume quota.
        """

        if type(proposal) is not MemoryRememberProposal:
            raise TypeError("MemoryRememberProposal is required")
        preference = (
            await self.session.execute(
                sa.select(
                    UserRow.memory_enabled,
                    UserRow.preferences_version,
                )
                .where(UserRow.id == proposal.scope.owner_user_id)
                .with_for_update(of=UserRow)
            )
        ).one_or_none()
        if preference is None or not bool(preference.memory_enabled):
            return MemoryProposalOutcome(
                disposition="memory_disabled",
                entry_id=None,
                tagged_text=None,
            )

        scope_predicates = self._scope_predicates(MemoryHistoryEntryRow, proposal.scope)
        existing_id = await self.session.scalar(
            sa.select(MemoryHistoryEntryRow.id).where(
                *scope_predicates,
                MemoryHistoryEntryRow.thread_id == proposal.thread_id,
                MemoryHistoryEntryRow.source_digest == proposal.source_digest,
            )
        )
        if existing_id is not None:
            return MemoryProposalOutcome(
                disposition="duplicate",
                entry_id=existing_id,
                tagged_text=proposal.tagged_text,
            )

        run_count = int(
            await self.session.scalar(
                sa.select(sa.func.count())
                .select_from(MemoryHistoryEntryRow)
                .where(
                    *scope_predicates,
                    MemoryHistoryEntryRow.origin == "tool",
                    MemoryHistoryEntryRow.source_run_id == proposal.run_id,
                )
            )
            or 0
        )
        if run_count >= REMEMBER_RUN_LIMIT:
            return MemoryProposalOutcome(
                disposition="run_limit_reached",
                entry_id=None,
                tagged_text=None,
            )

        pending_count = int(
            await self.session.scalar(
                sa.select(sa.func.count())
                .select_from(MemoryHistoryEntryRow)
                .where(
                    *scope_predicates,
                    MemoryHistoryEntryRow.status == "pending",
                )
            )
            or 0
        )
        if pending_count >= REMEMBER_BACKLOG_LIMIT:
            return MemoryProposalOutcome(
                disposition="backlog_full",
                entry_id=None,
                tagged_text=None,
            )

        inserted_id = await self.session.scalar(
            pg_insert(MemoryHistoryEntryRow)
            .values(
                project_id=proposal.scope.project_id,
                owner_user_id=proposal.scope.owner_user_id,
                namespace=proposal.scope.namespace,
                thread_id=proposal.thread_id,
                origin="tool",
                source_run_id=proposal.run_id,
                source_digest=proposal.source_digest,
                status="pending",
                tagged_text=proposal.tagged_text,
                content_digest=compute_snip_content_digest(proposal.tagged_text),
                preference_version=int(preference.preferences_version),
                snip_prompt_version=REMEMBER_PROMPT_VERSION,
            )
            .on_conflict_do_nothing(
                index_elements=(
                    MemoryHistoryEntryRow.project_id,
                    MemoryHistoryEntryRow.owner_user_id,
                    MemoryHistoryEntryRow.namespace,
                    MemoryHistoryEntryRow.thread_id,
                    MemoryHistoryEntryRow.source_digest,
                )
            )
            .returning(MemoryHistoryEntryRow.id)
        )
        if inserted_id is None:
            raise MemoryDocumentConflict("Memory proposal disappeared under lock")
        await self.session.flush()
        return MemoryProposalOutcome(
            disposition="recorded",
            entry_id=inserted_id,
            tagged_text=proposal.tagged_text,
        )

    async def read_state(
        self,
        scope: MemoryDocumentScope,
        *,
        for_update: bool = False,
    ) -> MemoryDocumentState:
        if type(scope) is not MemoryDocumentScope:
            raise TypeError("MemoryDocumentScope is required")
        statement = sa.select(MemoryDocumentRow).where(*self._scope_predicates(MemoryDocumentRow, scope))
        if for_update:
            statement = statement.with_for_update(of=MemoryDocumentRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        pending_count = int(
            await self.session.scalar(
                sa.select(sa.func.count())
                .select_from(MemoryHistoryEntryRow)
                .where(
                    *self._scope_predicates(MemoryHistoryEntryRow, scope),
                    MemoryHistoryEntryRow.status == "pending",
                )
            )
            or 0
        )
        return MemoryDocumentState(
            document=self._document_record(row),
            pending_count=pending_count,
        )

    async def list_pending_entries(
        self,
        scope: MemoryDocumentScope,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[MemoryPendingEntryRecord, ...]:
        """Bounded window over the pending backlog, oldest first.

        The order matches Dream consumption (ascending ``sequence``), so the
        first page is exactly what the next Dream will organize.
        """

        if type(scope) is not MemoryDocumentScope:
            raise TypeError("MemoryDocumentScope is required")
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 10_000:
            raise ValueError("Memory pending pagination is invalid")
        rows = (
            await self.session.execute(
                sa.select(MemoryHistoryEntryRow)
                .where(
                    *self._scope_predicates(MemoryHistoryEntryRow, scope),
                    MemoryHistoryEntryRow.status == "pending",
                )
                .order_by(MemoryHistoryEntryRow.sequence)
                .limit(limit)
                .offset(offset)
            )
        ).scalars()
        return tuple(
            MemoryPendingEntryRecord(
                sequence=int(row.sequence),
                origin=row.origin,
                tagged_text=row.tagged_text or "",
                created_at=row.created_at,
            )
            for row in rows
        )

    async def list_versions(
        self,
        scope: MemoryDocumentScope,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[MemoryDocumentVersionRecord, ...]:
        if type(scope) is not MemoryDocumentScope:
            raise TypeError("MemoryDocumentScope is required")
        if not 1 <= limit <= 100 or not 0 <= offset <= 10_000:
            raise ValueError("Memory version pagination is invalid")
        rows = (await self.session.execute(sa.select(MemoryDocumentVersionRow).where(*self._scope_predicates(MemoryDocumentVersionRow, scope)).order_by(MemoryDocumentVersionRow.version.desc()).limit(limit).offset(offset))).scalars()
        return tuple(self._version_record(row) for row in rows)

    async def read_version(
        self,
        scope: MemoryDocumentScope,
        version: int,
    ) -> MemoryDocumentVersionRecord:
        if type(scope) is not MemoryDocumentScope:
            raise TypeError("MemoryDocumentScope is required")
        if version < 1:
            raise MemoryDocumentNotFound
        row = (
            await self.session.execute(
                sa.select(MemoryDocumentVersionRow).where(
                    *self._scope_predicates(MemoryDocumentVersionRow, scope),
                    MemoryDocumentVersionRow.version == version,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise MemoryDocumentNotFound
        return self._version_record(row)

    @staticmethod
    def _episode_record(row: MemoryEpisodeRow) -> MemoryEpisodeRecord:
        return MemoryEpisodeRecord(
            id=row.id,
            thread_id=row.thread_id,
            origin=row.origin,
            tagged_text=row.tagged_text,
            occurred_at=row.occurred_at,
            created_at=row.created_at,
        )

    def _episode_predicates(
        self,
        scope: MemoryDocumentScope,
        *,
        tags: tuple[str, ...],
        retention_days: int,
        now: datetime,
    ) -> list[sa.ColumnElement[bool]]:
        predicates: list[sa.ColumnElement[bool]] = [
            *self._scope_predicates(MemoryEpisodeRow, scope),
        ]
        if retention_days:
            # Read-side retention keeps scopes that never trigger a Dream from
            # surfacing rows the settlement prune has not reached yet.
            predicates.append(MemoryEpisodeRow.occurred_at >= now - timedelta(days=retention_days))
        if tags:
            predicates.append(sa.or_(*(MemoryEpisodeRow.tagged_text.like(f"%[{tag}]%") for tag in tags)))
        return predicates

    @staticmethod
    def _episode_read_boundary(
        scope: MemoryDocumentScope,
        limit: int,
        retention_days: int,
        now: datetime,
    ) -> None:
        if type(scope) is not MemoryDocumentScope:
            raise TypeError("MemoryDocumentScope is required")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Episode limit is out of contract")
        validate_episode_retention_days(retention_days)
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("Episode read time must be timezone-aware")

    async def search_episodes(
        self,
        scope: MemoryDocumentScope,
        *,
        query: str,
        tags: tuple[str, ...] = (),
        limit: int = 5,
        retention_days: int = DEFAULT_EPISODE_RETENTION_DAYS,
        now: datetime,
    ) -> tuple[MemoryEpisodeRecord, ...]:
        """Deterministic ranked recall: exact substring, then trigram similarity."""

        self._episode_read_boundary(scope, limit, retention_days, now)
        if not isinstance(query, str):
            raise ValueError("Episode query must be a string")
        query = query.strip()
        if not query or len(query) > MAX_EPISODE_QUERY_CHARS:
            raise ValueError("Episode query is out of contract")
        normalized_tags = _validated_episode_tags(tags)

        pattern = f"%{_escape_like_pattern(query)}%"
        exact_hit = sa.case(
            (MemoryEpisodeRow.tagged_text.ilike(pattern, escape="\\"), 1),
            else_=0,
        )
        similarity = sa.func.similarity(MemoryEpisodeRow.tagged_text, query)
        statement = (
            sa.select(MemoryEpisodeRow)
            .where(
                *self._episode_predicates(
                    scope,
                    tags=normalized_tags,
                    retention_days=retention_days,
                    now=now,
                ),
                sa.or_(exact_hit == 1, similarity >= EPISODE_SIMILARITY_FLOOR),
            )
            .order_by(
                exact_hit.desc(),
                similarity.desc(),
                MemoryEpisodeRow.occurred_at.desc(),
                MemoryEpisodeRow.id.desc(),
            )
            .limit(limit)
        )
        rows = (await self.session.execute(statement)).scalars()
        return tuple(self._episode_record(row) for row in rows)

    async def list_episodes(
        self,
        scope: MemoryDocumentScope,
        *,
        tags: tuple[str, ...] = (),
        before: datetime | None = None,
        limit: int = 20,
        retention_days: int = DEFAULT_EPISODE_RETENTION_DAYS,
        now: datetime,
    ) -> tuple[MemoryEpisodeRecord, ...]:
        """Time-ordered browse with a strictly-before cursor for paging."""

        self._episode_read_boundary(scope, limit, retention_days, now)
        if before is not None and (not isinstance(before, datetime) or before.tzinfo is None):
            raise ValueError("Episode cursor must be timezone-aware")
        normalized_tags = _validated_episode_tags(tags)

        predicates = self._episode_predicates(
            scope,
            tags=normalized_tags,
            retention_days=retention_days,
            now=now,
        )
        if before is not None:
            predicates.append(MemoryEpisodeRow.occurred_at < before)
        statement = (
            sa.select(MemoryEpisodeRow)
            .where(*predicates)
            .order_by(
                MemoryEpisodeRow.occurred_at.desc(),
                MemoryEpisodeRow.id.desc(),
            )
            .limit(limit)
        )
        rows = (await self.session.execute(statement)).scalars()
        return tuple(self._episode_record(row) for row in rows)

    @classmethod
    def _due_condition(
        cls,
        history: type[MemoryHistoryEntryRow],
        document: type[MemoryDocumentRow],
        *,
        now: datetime,
        interval_minutes: int,
    ) -> sa.ColumnElement[bool]:
        """Three-way due rule over one scope's grouped pending entries.

        A scope is due when the interval has elapsed since the last Dream
        activity, when a full batch is already waiting, or when an explicit
        `remember` proposal has been pending longer than its grace window.
        """

        oldest_pending = sa.func.min(history.created_at)
        latest_dream_activity = cls._latest_dream_activity(history)
        due_anchor = sa.func.greatest(
            oldest_pending,
            sa.func.coalesce(document.updated_at, oldest_pending),
            sa.func.coalesce(latest_dream_activity, oldest_pending),
        )
        oldest_tool_pending = sa.func.min(sa.case((history.origin == "tool", history.created_at)))
        return sa.or_(
            due_anchor <= now - timedelta(minutes=interval_minutes),
            sa.func.count() >= DREAM_HISTORY_BATCH_SIZE,
            oldest_tool_pending <= now - timedelta(minutes=TOOL_ENTRY_DUE_MINUTES),
        )

    async def list_due_scopes(
        self,
        *,
        now: datetime,
        interval_minutes: int,
        limit: int = 100,
    ) -> tuple[MemoryDocumentScope, ...]:
        if not isinstance(now, datetime) or now.tzinfo is None or type(interval_minutes) is not int or not 15 <= interval_minutes <= 1_440 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Dream schedule boundary is invalid")
        document = MemoryDocumentRow
        history = MemoryHistoryEntryRow
        oldest_pending = sa.func.min(history.created_at)
        latest_dream_activity = self._latest_dream_activity(history)
        due_anchor = sa.func.greatest(
            oldest_pending,
            sa.func.coalesce(document.updated_at, oldest_pending),
            sa.func.coalesce(latest_dream_activity, oldest_pending),
        )
        rows = tuple(
            await self.session.execute(
                sa.select(
                    history.project_id,
                    history.owner_user_id,
                    history.namespace,
                )
                .outerjoin(
                    document,
                    sa.and_(
                        document.project_id == history.project_id,
                        document.owner_user_id == history.owner_user_id,
                        document.namespace == history.namespace,
                    ),
                )
                .where(
                    history.status == "pending",
                    document.active_dream_job_id.is_(None),
                )
                .group_by(
                    history.project_id,
                    history.owner_user_id,
                    history.namespace,
                    document.updated_at,
                    document.active_dream_job_id,
                )
                .having(
                    self._due_condition(
                        history,
                        document,
                        now=now,
                        interval_minutes=interval_minutes,
                    )
                )
                .order_by(due_anchor, history.project_id, history.owner_user_id)
                .limit(limit)
            )
        )
        return tuple(
            MemoryDocumentScope(
                project_id=row.project_id,
                owner_user_id=row.owner_user_id,
                namespace=row.namespace,
            )
            for row in rows
        )

    async def list_budget_rewrite_scopes(
        self,
        *,
        budget_tokens: int,
        limit: int = 100,
    ) -> tuple[MemoryDocumentScope, ...]:
        """Discover scopes that may need the empty-batch budget rescue.

        ``char_length(content) > budget_tokens`` is a necessary condition for
        being over budget (the token estimate never exceeds the character
        count), so this SQL prefilter can only over-approximate. Admission
        re-verifies the exact estimate per scope under locks.
        """

        if type(budget_tokens) is not int or not 100 <= budget_tokens <= 8_000 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Dream schedule boundary is invalid")
        document = MemoryDocumentRow
        history = MemoryHistoryEntryRow
        pending_exists = sa.exists(
            sa.select(sa.literal(1)).where(
                history.project_id == document.project_id,
                history.owner_user_id == document.owner_user_id,
                history.namespace == document.namespace,
                history.status == "pending",
            )
        )
        rows = tuple(
            await self.session.execute(
                sa.select(
                    document.project_id,
                    document.owner_user_id,
                    document.namespace,
                )
                .where(
                    document.active_dream_job_id.is_(None),
                    document.version >= 1,
                    sa.func.char_length(document.content) > budget_tokens,
                    ~pending_exists,
                )
                .order_by(document.updated_at)
                .limit(limit)
            )
        )
        return tuple(
            MemoryDocumentScope(
                project_id=row.project_id,
                owner_user_id=row.owner_user_id,
                namespace=row.namespace,
            )
            for row in rows
        )

    async def is_scope_due(
        self,
        scope: MemoryDocumentScope,
        *,
        now: datetime,
        interval_minutes: int,
    ) -> bool:
        """Recheck one scope against the interval frozen by its admission transaction."""

        if type(scope) is not MemoryDocumentScope or not isinstance(now, datetime) or now.tzinfo is None or type(interval_minutes) is not int or not 15 <= interval_minutes <= 1_440:
            raise ValueError("Dream schedule boundary is invalid")
        document = MemoryDocumentRow
        history = MemoryHistoryEntryRow
        row = await self.session.scalar(
            sa.select(sa.literal(True))
            .select_from(history)
            .outerjoin(
                document,
                sa.and_(
                    document.project_id == history.project_id,
                    document.owner_user_id == history.owner_user_id,
                    document.namespace == history.namespace,
                ),
            )
            .where(
                *self._scope_predicates(history, scope),
                history.status == "pending",
                document.active_dream_job_id.is_(None),
            )
            .group_by(
                history.project_id,
                history.owner_user_id,
                history.namespace,
                document.updated_at,
                document.active_dream_job_id,
            )
            .having(
                self._due_condition(
                    history,
                    document,
                    now=now,
                    interval_minutes=interval_minutes,
                )
            )
            .limit(1)
        )
        return row is True

    async def admit_dream(
        self,
        scope: MemoryDocumentScope,
        *,
        trigger: MemoryDreamTrigger,
        frozen: MemoryDreamFrozenRuntime,
        initial_content: str | None,
        initial_sections: tuple[str, ...] | None,
        sections_policy_version_id: uuid.UUID | None,
        now: datetime,
        max_attempts: int = 3,
    ) -> MemoryDreamAdmissionRecord:
        if (
            type(scope) is not MemoryDocumentScope
            or trigger not in {"auto_dream", "manual_dream", "budget_rewrite"}
            or type(frozen) is not MemoryDreamFrozenRuntime
            or not isinstance(now, datetime)
            or now.tzinfo is None
            or type(max_attempts) is not int
            or not 1 <= max_attempts <= 20
        ):
            raise ValueError("Dream admission input is invalid")
        supplied_creation_material = (
            initial_content is not None,
            initial_sections is not None,
            sections_policy_version_id is not None,
        )
        if any(supplied_creation_material) and not all(supplied_creation_material):
            raise ValueError("Dream document creation material is incomplete")
        creation_sections: tuple[str, ...] | None = None
        if all(supplied_creation_material):
            if not isinstance(initial_content, str) or not isinstance(sections_policy_version_id, uuid.UUID):
                raise ValueError("Dream document creation material is invalid")
            creation_sections = validate_memory_document_sections(initial_sections)
            if initial_content != render_empty_memory_document(creation_sections):
                raise ValueError("Dream initial document does not match its sections")

        document = (await self.session.execute(sa.select(MemoryDocumentRow).where(*self._scope_predicates(MemoryDocumentRow, scope)).with_for_update(of=MemoryDocumentRow))).scalar_one_or_none()
        if document is None:
            if creation_sections is None or initial_content is None or sections_policy_version_id is None:
                raise MemoryDocumentConflict("Dream document creation policy is unavailable")
            await self.session.execute(
                pg_insert(MemoryDocumentRow)
                .values(
                    project_id=scope.project_id,
                    owner_user_id=scope.owner_user_id,
                    namespace=scope.namespace,
                    content=initial_content,
                    content_digest=memory_document_digest(initial_content),
                    sections=list(creation_sections),
                    sections_policy_version_id=sections_policy_version_id,
                    version=0,
                    dream_cursor=0,
                    active_dream_job_id=None,
                    updated_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=(
                        MemoryDocumentRow.project_id,
                        MemoryDocumentRow.owner_user_id,
                        MemoryDocumentRow.namespace,
                    )
                )
            )
            document = (await self.session.execute(sa.select(MemoryDocumentRow).where(*self._scope_predicates(MemoryDocumentRow, scope)).with_for_update(of=MemoryDocumentRow))).scalar_one_or_none()
            if document is None:
                raise MemoryDocumentConflict("Dream document creation disappeared")
        document_sections, _sections_policy_version_id = self._frozen_document_sections(document)
        if memory_document_digest(document.content) != document.content_digest:
            raise MemoryDocumentConflict("Memory document digest changed")
        validate_memory_document(
            document.content,
            MAX_MEMORY_DOCUMENT_CHARS,
            sections=document_sections,
        )
        active = await self._active_dream(document, scope)
        if active is not None:
            return active

        rows = tuple(
            (
                await self.session.execute(
                    sa.select(MemoryHistoryEntryRow)
                    .where(
                        *self._scope_predicates(MemoryHistoryEntryRow, scope),
                        MemoryHistoryEntryRow.status == "pending",
                    )
                    .order_by(MemoryHistoryEntryRow.sequence)
                    .limit(DREAM_HISTORY_BATCH_SIZE)
                    .with_for_update(of=MemoryHistoryEntryRow)
                )
            ).scalars()
        )[:DREAM_HISTORY_BATCH_SIZE]
        if trigger == "budget_rewrite":
            # The rescue path is only legal against an empty backlog; a raced
            # `remember` proposal must surface as a conflict, never as a Dream
            # that silently ignores pending work.
            if rows:
                raise MemoryDocumentConflict("Dream budget rewrite requires an empty backlog")
            if int(document.version) < 1:
                raise MemoryDocumentConflict("Dream budget rewrite requires a published document")
            return await self._enqueue_dream(
                scope,
                document=document,
                trigger=trigger,
                frozen=frozen,
                history=(),
                history_digest=BUDGET_REWRITE_HISTORY_DIGEST,
                rows=(),
                now=now,
                max_attempts=max_attempts,
            )
        if not rows:
            return MemoryDreamAdmissionRecord(
                disposition="nothing_pending",
                job_id=None,
                history_count=0,
            )
        history = tuple(
            MemoryDreamHistoryRecord(
                id=row.id,
                sequence=int(row.sequence),
                tagged_text=row.tagged_text,
                content_digest=row.content_digest,
                origin=row.origin,
            )
            for row in rows
        )
        if any(item.tagged_text is None for item in history):
            raise MemoryDocumentConflict("Dream pending history is invalid")
        history_digest = compute_dream_history_digest(history)
        return await self._enqueue_dream(
            scope,
            document=document,
            trigger=trigger,
            frozen=frozen,
            history=history,
            history_digest=history_digest,
            rows=rows,
            now=now,
            max_attempts=max_attempts,
        )

    async def _enqueue_dream(
        self,
        scope: MemoryDocumentScope,
        *,
        document: MemoryDocumentRow,
        trigger: MemoryDreamTrigger,
        frozen: MemoryDreamFrozenRuntime,
        history: tuple[MemoryDreamHistoryRecord, ...],
        history_digest: str,
        rows: tuple[MemoryHistoryEntryRow, ...],
        now: datetime,
        max_attempts: int,
    ) -> MemoryDreamAdmissionRecord:
        prior_generations = int(
            await self.session.scalar(
                sa.select(sa.func.count())
                .select_from(MemoryDreamRunRow)
                .where(
                    *self._scope_predicates(MemoryDreamRunRow, scope),
                    MemoryDreamRunRow.base_document_version == int(document.version),
                    MemoryDreamRunRow.history_digest == history_digest,
                )
            )
            or 0
        )
        idempotency_key = hashlib.sha256(
            "\x1f".join(
                (
                    "memory_dream_v1",
                    str(scope.project_id),
                    scope.owner_user_id,
                    scope.namespace,
                    str(document.version),
                    history_digest,
                    str(prior_generations + 1),
                )
            ).encode("utf-8")
        ).hexdigest()
        job_id = await self.jobs.enqueue(
            EnqueueJob(
                job_type="memory_dream",
                scope=JobScope(scope.project_id, scope.owner_user_id),
                namespace=scope.namespace,
                idempotency_key=idempotency_key,
                run_id=None,
                occurrence_id=None,
                max_attempts=max_attempts,
                retry_safety="safe",
                priority=0 if trigger == "auto_dream" else 10,
            )
        )
        run = MemoryDreamRunRow(
            job_id=job_id,
            project_id=scope.project_id,
            owner_user_id=scope.owner_user_id,
            namespace=scope.namespace,
            trigger=trigger,
            history_from=history[0].sequence if history else None,
            history_to=history[-1].sequence if history else None,
            history_count=len(history),
            history_digest=history_digest,
            base_document_version=int(document.version),
            base_content_digest=document.content_digest,
            preference_version=frozen.preference_version,
            policy_revision=frozen.policy_revision,
            model_ref=frozen.model_version_id,
            prompt_version=frozen.prompt_version,
            result_version=None,
            created_at=now,
            completed_at=None,
        )
        self.session.add(run)
        await self.session.flush()
        for row in rows:
            row.status = "processing"
            row.dream_job_id = job_id
        document.active_dream_job_id = job_id
        await self.session.flush()
        return MemoryDreamAdmissionRecord(
            disposition="queued",
            job_id=job_id,
            history_count=len(history),
            admission_kind=("budget_rewrite" if trigger == "budget_rewrite" else "history"),
        )

    async def _active_dream(
        self,
        document: MemoryDocumentRow,
        scope: MemoryDocumentScope,
    ) -> MemoryDreamAdmissionRecord | None:
        job_id = document.active_dream_job_id
        if job_id is None:
            return None
        result = (
            await self.session.execute(
                sa.select(JobRow, MemoryDreamRunRow)
                .outerjoin(
                    MemoryDreamRunRow,
                    MemoryDreamRunRow.job_id == JobRow.id,
                )
                .where(
                    JobRow.id == job_id,
                    JobRow.project_id == scope.project_id,
                    JobRow.owner_user_id == scope.owner_user_id,
                    JobRow.namespace == scope.namespace,
                )
                .with_for_update(of=JobRow)
            )
        ).one_or_none()
        if result is None:
            raise MemoryDocumentConflict("Dream active Job is missing")
        job, run = result
        if job.status in {"queued", "leased", "running", "retry_wait"}:
            if run is None:
                raise MemoryDocumentConflict("Dream run is missing")
            return MemoryDreamAdmissionRecord(
                disposition="already_running",
                job_id=job.id,
                history_count=int(run.history_count),
                admission_kind=("budget_rewrite" if run.trigger == "budget_rewrite" else "history"),
            )
        if run is not None and run.result_version is not None:
            document.active_dream_job_id = None
            return None
        await self.session.execute(
            sa.update(MemoryHistoryEntryRow)
            .where(
                *self._scope_predicates(MemoryHistoryEntryRow, scope),
                MemoryHistoryEntryRow.status == "processing",
                MemoryHistoryEntryRow.dream_job_id == job_id,
            )
            .values(
                status="pending",
                dream_job_id=None,
                consumed_at=None,
            )
        )
        document.active_dream_job_id = None
        await self.session.flush()
        return None

    async def load_dream_work(
        self,
        scope: MemoryDocumentScope,
        job_id: uuid.UUID,
    ) -> MemoryDreamWork | None:
        if type(scope) is not MemoryDocumentScope or not isinstance(job_id, uuid.UUID):
            raise TypeError("Dream work authority is invalid")
        result = (
            await self.session.execute(
                sa.select(
                    MemoryDreamRunRow,
                    MemoryDocumentRow,
                    SystemModelConfigVersionRow,
                    JobRow,
                )
                .join(
                    MemoryDocumentRow,
                    sa.and_(
                        MemoryDocumentRow.project_id == MemoryDreamRunRow.project_id,
                        MemoryDocumentRow.owner_user_id == MemoryDreamRunRow.owner_user_id,
                        MemoryDocumentRow.namespace == MemoryDreamRunRow.namespace,
                    ),
                )
                .join(
                    SystemModelConfigVersionRow,
                    SystemModelConfigVersionRow.id == MemoryDreamRunRow.model_ref,
                )
                .join(JobRow, JobRow.id == MemoryDreamRunRow.job_id)
                .where(
                    MemoryDreamRunRow.job_id == job_id,
                    *self._scope_predicates(MemoryDreamRunRow, scope),
                )
            )
        ).one_or_none()
        if result is None:
            return None
        run, document, model_version, job = result
        history_rows = tuple(
            (
                await self.session.execute(
                    sa.select(MemoryHistoryEntryRow)
                    .where(
                        *self._scope_predicates(MemoryHistoryEntryRow, scope),
                        MemoryHistoryEntryRow.dream_job_id == job_id,
                    )
                    .order_by(MemoryHistoryEntryRow.sequence)
                )
            ).scalars()
        )
        history = tuple(
            MemoryDreamHistoryRecord(
                id=row.id,
                sequence=int(row.sequence),
                tagged_text=row.tagged_text,
                content_digest=row.content_digest,
                origin=row.origin,
            )
            for row in history_rows
        )
        document_sections, sections_policy_version_id = self._frozen_document_sections(document)
        return MemoryDreamWork(
            job_id=run.job_id,
            project_id=run.project_id,
            owner_user_id=run.owner_user_id,
            namespace=run.namespace,
            trigger=run.trigger,
            history_from=(None if run.history_from is None else int(run.history_from)),
            history_to=(None if run.history_to is None else int(run.history_to)),
            history_count=int(run.history_count),
            history_digest=run.history_digest,
            base_document_version=int(run.base_document_version),
            base_content=document.content if run.result_version is None else (await self.read_version(scope, int(run.result_version))).content,
            base_content_digest=run.base_content_digest,
            sections=document_sections,
            sections_policy_version_id=sections_policy_version_id,
            preference_version=int(run.preference_version),
            policy_revision=int(run.policy_revision),
            model_config_id=model_version.model_config_id,
            model_version_id=model_version.id,
            model_payload_checksum=model_version.payload_checksum,
            prompt_version=run.prompt_version,
            result_version=(None if run.result_version is None else int(run.result_version)),
            cancel_requested=job.cancel_requested_at is not None,
            job_status=job.status,
            history=history,
        )

    async def finalize_dream(
        self,
        scope: MemoryDocumentScope,
        *,
        job_id: uuid.UUID,
        lease_token: str,
        expected_history_digest: str,
        expected_base_version: int,
        expected_base_digest: str,
        expected_sections: tuple[str, ...],
        content: str,
        now: datetime,
        episode_retention_days: int = DEFAULT_EPISODE_RETENTION_DAYS,
    ) -> MemoryDocumentVersionRecord:
        validate_episode_retention_days(episode_retention_days)
        expected_sections = validate_memory_document_sections(expected_sections)
        existing = (
            await self.session.execute(
                sa.select(MemoryDocumentVersionRow).where(
                    *self._scope_predicates(MemoryDocumentVersionRow, scope),
                    MemoryDocumentVersionRow.dream_job_id == job_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return self._version_record(existing)
        document = (await self.session.execute(sa.select(MemoryDocumentRow).where(*self._scope_predicates(MemoryDocumentRow, scope)).with_for_update(of=MemoryDocumentRow))).scalar_one_or_none()
        run = (
            await self.session.execute(
                sa.select(MemoryDreamRunRow)
                .where(
                    MemoryDreamRunRow.job_id == job_id,
                    *self._scope_predicates(MemoryDreamRunRow, scope),
                )
                .with_for_update(of=MemoryDreamRunRow)
            )
        ).scalar_one_or_none()
        history_rows = tuple(
            (
                await self.session.execute(
                    sa.select(MemoryHistoryEntryRow)
                    .where(
                        *self._scope_predicates(MemoryHistoryEntryRow, scope),
                        MemoryHistoryEntryRow.dream_job_id == job_id,
                    )
                    .order_by(MemoryHistoryEntryRow.sequence)
                    .with_for_update(of=MemoryHistoryEntryRow)
                )
            ).scalars()
        )
        history = tuple(
            MemoryDreamHistoryRecord(
                id=row.id,
                sequence=int(row.sequence),
                tagged_text=row.tagged_text,
                content_digest=row.content_digest,
                origin=row.origin,
            )
            for row in history_rows
        )
        if (
            document is None
            or run is None
            or document.active_dream_job_id != job_id
            or int(document.version) != expected_base_version
            or document.content_digest != expected_base_digest
            or int(run.base_document_version) != expected_base_version
            or run.base_content_digest != expected_base_digest
            or self._frozen_document_sections(document)[0] != expected_sections
            or run.history_digest != expected_history_digest
            or int(run.history_count) != len(history)
            or any(row.status != "processing" for row in history_rows)
        ):
            raise MemoryDocumentConflict("Dream settlement contract changed")
        validate_memory_document(
            content,
            MAX_MEMORY_DOCUMENT_CHARS,
            sections=expected_sections,
        )
        if run.trigger == "budget_rewrite":
            if history or expected_history_digest != BUDGET_REWRITE_HISTORY_DIGEST:
                raise MemoryDocumentConflict("Dream settlement contract changed")
        elif (
            not history or run.history_from is None or run.history_to is None or int(run.history_from) != history[0].sequence or int(run.history_to) != history[-1].sequence or compute_dream_history_digest(history) != expected_history_digest
        ):
            raise MemoryDocumentConflict("Dream settlement contract changed")
        next_version = int(document.version) + 1
        unified_diff = memory_document_unified_diff(document.content, content)
        content_digest = memory_document_digest(content)
        version = MemoryDocumentVersionRow(
            project_id=scope.project_id,
            owner_user_id=scope.owner_user_id,
            namespace=scope.namespace,
            version=next_version,
            content=content,
            content_digest=content_digest,
            unified_diff=unified_diff,
            trigger=run.trigger,
            dream_job_id=job_id,
            history_from=run.history_from,
            history_to=run.history_to,
            history_count=run.history_count,
            prompt_version=run.prompt_version,
            model_ref=run.model_ref,
            needs_review=memory_document_needs_review(document.content, content, history),
            created_at=now,
        )
        self.session.add(version)
        for row in history_rows:
            # Archive the full text as an episode in the same transaction that
            # tombstones the history row.  Reusing the history UUID makes a
            # duplicate settlement collide on the primary key instead of
            # silently duplicating the archive.
            self.session.add(
                MemoryEpisodeRow(
                    id=row.id,
                    project_id=row.project_id,
                    owner_user_id=row.owner_user_id,
                    namespace=row.namespace,
                    thread_id=row.thread_id,
                    origin=row.origin,
                    tagged_text=row.tagged_text,
                    content_digest=row.content_digest,
                    occurred_at=row.created_at,
                    consumed_dream_job_id=job_id,
                    created_at=now,
                )
            )
            row.status = "consumed"
            row.tagged_text = None
            row.consumed_at = now
        document.content = content
        document.content_digest = content_digest
        document.version = next_version
        if run.history_to is not None:
            document.dream_cursor = max(int(document.dream_cursor), int(run.history_to))
        document.active_dream_job_id = None
        document.updated_at = now
        run.result_version = next_version
        run.completed_at = now
        await self.session.flush()
        await self._prune_expired_episodes(
            scope,
            now=now,
            episode_retention_days=episode_retention_days,
        )
        if not await self.jobs.settle_success(
            job_id,
            lease_token=lease_token,
            now=now,
        ):
            raise MemoryDocumentConflict("Dream Job lease changed")
        await self.session.flush()
        return self._version_record(version)

    async def _prune_expired_episodes(
        self,
        scope: MemoryDocumentScope,
        *,
        now: datetime,
        episode_retention_days: int,
    ) -> None:
        """Bounded same-transaction cleanup; there is no dedicated purge Job."""

        if episode_retention_days == 0:
            return
        cutoff = now - timedelta(days=episode_retention_days)
        expired = (
            sa.select(MemoryEpisodeRow.id)
            .where(
                *self._scope_predicates(MemoryEpisodeRow, scope),
                MemoryEpisodeRow.occurred_at < cutoff,
            )
            .order_by(MemoryEpisodeRow.occurred_at)
            .limit(_EPISODE_PRUNE_BATCH_LIMIT)
        )
        await self.session.execute(sa.delete(MemoryEpisodeRow).where(MemoryEpisodeRow.id.in_(expired)))

    async def release_dream(
        self,
        scope: MemoryDocumentScope,
        *,
        job_id: uuid.UUID,
        lease_token: str,
        now: datetime,
        cancelled: bool,
        public_error_code: str = "MEMORY_DREAM_FAILED",
        retryable: bool = True,
        retry_initial_seconds: int = 5,
        retry_max_seconds: int = 300,
    ) -> bool:
        if type(retryable) is not bool:
            raise TypeError("Dream retryable flag must be a boolean")
        completed_version = await self.session.scalar(
            sa.select(MemoryDreamRunRow.result_version).where(
                MemoryDreamRunRow.job_id == job_id,
                *self._scope_predicates(MemoryDreamRunRow, scope),
            )
        )
        if completed_version is not None:
            return True
        if cancelled:
            settled = await self.jobs.settle_cancelled(
                job_id,
                lease_token=lease_token,
                now=now,
            )
        else:
            settled = await self.jobs.retry_or_dead(
                job_id,
                lease_token=lease_token,
                public_error_code=public_error_code,
                retryable=retryable,
                retry_initial_seconds=retry_initial_seconds,
                retry_max_seconds=retry_max_seconds,
                now=now,
            )
        if not settled:
            raise MemoryDocumentConflict("Dream Job lease changed")
        job_status = await self.session.scalar(sa.select(JobRow.status).where(JobRow.id == job_id))
        if job_status == "retry_wait":
            # A retry owns the same frozen batch.  Keep both the document's
            # active pointer and the history rows in processing so no competing
            # Dream can consume or mutate them between attempts.
            return True
        if job_status not in {"cancelled", "dead", "failed"}:
            raise MemoryDocumentConflict("Dream Job terminal state is invalid")

        document = (await self.session.execute(sa.select(MemoryDocumentRow).where(*self._scope_predicates(MemoryDocumentRow, scope)).with_for_update(of=MemoryDocumentRow))).scalar_one_or_none()
        run = (
            await self.session.execute(
                sa.select(MemoryDreamRunRow)
                .where(
                    MemoryDreamRunRow.job_id == job_id,
                    *self._scope_predicates(MemoryDreamRunRow, scope),
                )
                .with_for_update(of=MemoryDreamRunRow)
            )
        ).scalar_one_or_none()
        if run is not None and run.result_version is not None:
            return True
        await self.session.execute(
            sa.update(MemoryHistoryEntryRow)
            .where(
                *self._scope_predicates(MemoryHistoryEntryRow, scope),
                MemoryHistoryEntryRow.status == "processing",
                MemoryHistoryEntryRow.dream_job_id == job_id,
            )
            .values(
                status="pending",
                dream_job_id=None,
                consumed_at=None,
            )
        )
        if document is not None and document.active_dream_job_id == job_id:
            document.active_dream_job_id = None
        await self.session.flush()
        return True

    async def restore_version(
        self,
        scope: MemoryDocumentScope,
        *,
        target_version: int,
        expected_current_version: int,
        expected_sections: tuple[str, ...],
        max_tokens: int,
        now: datetime,
    ) -> MemoryDocumentVersionRecord:
        expected_sections = validate_memory_document_sections(expected_sections)
        if (
            type(target_version) is not int
            or target_version < 1
            or type(expected_current_version) is not int
            or expected_current_version < 0
            or type(max_tokens) is not int
            or max_tokens < 1
            or not isinstance(now, datetime)
            or now.tzinfo is None
        ):
            raise ValueError("Memory restore input is invalid")
        document = (await self.session.execute(sa.select(MemoryDocumentRow).where(*self._scope_predicates(MemoryDocumentRow, scope)).with_for_update(of=MemoryDocumentRow))).scalar_one_or_none()
        if document is None:
            raise MemoryDocumentNotFound
        if document.active_dream_job_id is not None or int(document.version) != expected_current_version or self._frozen_document_sections(document)[0] != expected_sections:
            raise MemoryDocumentConflict("Memory restore CAS conflict")
        target = (
            await self.session.execute(
                sa.select(MemoryDocumentVersionRow).where(
                    *self._scope_predicates(MemoryDocumentVersionRow, scope),
                    MemoryDocumentVersionRow.version == target_version,
                )
            )
        ).scalar_one_or_none()
        if target is None:
            raise MemoryDocumentNotFound
        validate_memory_document(
            target.content,
            max_tokens,
            sections=expected_sections,
        )
        next_version = int(document.version) + 1
        restored = MemoryDocumentVersionRow(
            project_id=scope.project_id,
            owner_user_id=scope.owner_user_id,
            namespace=scope.namespace,
            version=next_version,
            content=target.content,
            content_digest=target.content_digest,
            unified_diff=memory_document_unified_diff(
                document.content,
                target.content,
            ),
            trigger="restore",
            dream_job_id=None,
            history_from=None,
            history_to=None,
            history_count=None,
            prompt_version=None,
            model_ref=None,
            created_at=now,
        )
        self.session.add(restored)
        document.content = target.content
        document.content_digest = target.content_digest
        document.version = next_version
        document.updated_at = now
        await self.session.flush()
        return self._version_record(restored)

    @staticmethod
    def _version_record(
        row: MemoryDocumentVersionRow,
    ) -> MemoryDocumentVersionRecord:
        return MemoryDocumentVersionRecord(
            version=int(row.version),
            content=row.content,
            content_digest=row.content_digest,
            unified_diff=row.unified_diff,
            trigger=row.trigger,
            dream_job_id=row.dream_job_id,
            history_from=(None if row.history_from is None else int(row.history_from)),
            history_to=None if row.history_to is None else int(row.history_to),
            history_count=(None if row.history_count is None else int(row.history_count)),
            prompt_version=row.prompt_version,
            model_ref=row.model_ref,
            needs_review=bool(row.needs_review),
            created_at=row.created_at,
        )

    async def reset_owner(
        self,
        owner_user_id: str,
        *,
        now: datetime,
    ) -> MemoryResetCounts:
        try:
            owner_user_id = str(uuid.UUID(str(owner_user_id)))
        except (TypeError, ValueError):
            raise ValueError("Memory reset requires an owner UUID") from None
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Memory reset time must be timezone-aware")

        scope_rows = set(
            (
                await self.session.execute(
                    sa.select(
                        MemoryDocumentRow.project_id,
                        MemoryDocumentRow.namespace,
                    ).where(MemoryDocumentRow.owner_user_id == owner_user_id)
                )
            ).all()
        )
        scope_rows.update(
            (
                await self.session.execute(
                    sa.select(
                        MemoryHistoryEntryRow.project_id,
                        MemoryHistoryEntryRow.namespace,
                    )
                    .where(MemoryHistoryEntryRow.owner_user_id == owner_user_id)
                    .distinct()
                )
            ).all()
        )

        counts = {
            "history_entries": await self._count(
                MemoryHistoryEntryRow,
                owner_user_id,
            ),
            "documents": await self._count(MemoryDocumentRow, owner_user_id),
            "versions": await self._count(
                MemoryDocumentVersionRow,
                owner_user_id,
            ),
            "dream_runs": await self._count(MemoryDreamRunRow, owner_user_id),
            "snapshots": await self._count(
                RunMemoryContextSnapshotRow,
                owner_user_id,
            ),
            "episodes": await self._count(MemoryEpisodeRow, owner_user_id),
        }

        active_jobs = tuple(
            (
                await self.session.execute(
                    sa.select(JobRow.id, JobRow.project_id, JobRow.owner_user_id)
                    .where(
                        JobRow.owner_user_id == owner_user_id,
                        JobRow.job_type.in_(("memory_dream", "memory_seal")),
                        JobRow.status.in_(("queued", "leased", "running", "retry_wait")),
                    )
                    .with_for_update(of=JobRow)
                )
            ).all()
        )
        jobs = JobRepository(self.session)
        jobs_cancelled = 0
        for job_id, project_id, job_owner_user_id in active_jobs:
            scope = JobScope(project_id, job_owner_user_id)
            requested = await jobs.request_cancel(
                scope,
                job_id,
                reason="memory_reset",
                now=now,
            )
            if requested:
                jobs_cancelled += 1
            await jobs.settle_requested_cancel(scope, job_id, now=now)

        await self.session.execute(sa.delete(RunMemoryContextSnapshotRow).where(RunMemoryContextSnapshotRow.owner_user_id == owner_user_id))
        await self.session.execute(sa.delete(MemoryHistoryEntryRow).where(MemoryHistoryEntryRow.owner_user_id == owner_user_id))
        await self.session.execute(sa.delete(MemoryEpisodeRow).where(MemoryEpisodeRow.owner_user_id == owner_user_id))
        await self.session.execute(sa.delete(MemoryDocumentRow).where(MemoryDocumentRow.owner_user_id == owner_user_id))
        await self.session.flush()
        return MemoryResetCounts(
            scopes_reset=len(scope_rows),
            history_entries=counts["history_entries"],
            documents=counts["documents"],
            versions=counts["versions"],
            dream_runs=counts["dream_runs"],
            snapshots=counts["snapshots"],
            episodes=counts["episodes"],
            jobs_cancelled=jobs_cancelled,
        )

    async def _count(self, row_type, owner_user_id: str) -> int:
        return int(await self.session.scalar(sa.select(sa.func.count()).select_from(row_type).where(row_type.owner_user_id == owner_user_id)) or 0)


__all__ = [
    "BUDGET_REWRITE_HISTORY_DIGEST",
    "DEFAULT_MEMORY_NAMESPACE",
    "MEMORY_REVIEW_DELETION_RATIO",
    "MEMORY_REVIEW_MIN_LINES",
    "MemoryDocumentConflict",
    "MemoryDocumentNotFound",
    "MemoryDocumentRecord",
    "MemoryDocumentRepository",
    "MemoryDocumentScope",
    "MemoryDocumentState",
    "MemoryDocumentVersionRecord",
    "MemoryDreamAdmissionDisposition",
    "MemoryDreamAdmissionKind",
    "MemoryDreamAdmissionRecord",
    "MemoryDreamFrozenRuntime",
    "MemoryDreamHistoryRecord",
    "MemoryDreamTrigger",
    "MemoryDreamWork",
    "MemoryHistoryActivation",
    "MemoryHistoryActivationResult",
    "MemoryHistoryActivationStatus",
    "MemoryResetCounts",
    "compute_dream_history_digest",
    "memory_document_deletion_ratio",
    "memory_document_digest",
    "memory_document_needs_review",
    "memory_document_unified_diff",
]
