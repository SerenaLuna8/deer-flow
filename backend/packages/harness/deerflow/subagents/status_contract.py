"""Structured subagent result metadata.

``task`` tool result text is model-visible display content. Runtime
consumers read the structured facts carried inside
``ToolMessage.additional_kwargs``:

- ``subagent_status``: one of ``SUBAGENT_STATUS_VALUES``.
- ``subagent_stop_reason`` (optional): why the run ended without a clean final
  response, one of ``SUBAGENT_STOP_REASON_VALUES``. A guardrail-capped run or
  Provider-truncated response that still produced usable partial work stays
  ``status=completed`` and carries the reason here; one with no usable output
  is ``status=failed`` + ``stop_reason``. Old frontends ignore the unknown
  field.
- ``subagent_error`` (optional): the human-readable error blob the
  backend recorded.
- ``subagent_result_brief`` / ``subagent_result_sha256`` (optional):
  bounded completed-result metadata plus a digest of the full result.
- ``subagent_model_name`` (optional): effective model identifier used by the
  delegated run.
- ``subagent_token_usage`` (optional): validated cumulative input/output/total
  token snapshot returned by the provider.
- ``subagent_usage_receipt_id`` (optional): stable identity for applying the
  aggregate usage exactly once when checkpointed messages are replayed. This is
  deliberately independent from the provider-owned, reusable tool-call ID.
  Historical messages that predate this field are not re-attributed because
  their aggregate may already have been folded into the parent message and no
  durable fact distinguishes the two cases.
- ``subagent_usage_completeness`` (optional): ``final_observed`` only after
  graph and inherited-operation quiescence; ``latest_observed`` identifies a
  bounded coordination cutoff whose aggregate usage may be incomplete.

"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any, Literal, NotRequired, TypedDict

SUBAGENT_STATUS_KEY = "subagent_status"
SUBAGENT_STOP_REASON_KEY = "subagent_stop_reason"
SUBAGENT_ERROR_KEY = "subagent_error"
SUBAGENT_RESULT_BRIEF_KEY = "subagent_result_brief"
SUBAGENT_RESULT_SHA256_KEY = "subagent_result_sha256"
SUBAGENT_MODEL_NAME_KEY = "subagent_model_name"
SUBAGENT_TOKEN_USAGE_KEY = "subagent_token_usage"
SUBAGENT_USAGE_RECEIPT_ID_KEY = "subagent_usage_receipt_id"
SUBAGENT_USAGE_RECEIPT_STATE_KEY = "subagent_usage_receipt_state"
SUBAGENT_USAGE_COMPLETENESS_KEY = "subagent_usage_completeness"
SUBAGENT_METADATA_TEXT_MAX_CHARS = 2000
SUBAGENT_USAGE_RECEIPT_ID_MAX_CHARS = 128
SUBAGENT_USAGE_RECEIPT_STATE_VERSION = 1

SubagentUsageCompletenessValue = Literal["final_observed", "latest_observed"]
SUBAGENT_USAGE_COMPLETENESS_VALUES: tuple[SubagentUsageCompletenessValue, ...] = (
    "final_observed",
    "latest_observed",
)

#: The producer always emits ``hashlib.sha256(...).hexdigest()`` — 64
#: lowercase hex chars. Readers enforce the same shape so a corrupted
#: relay value cannot masquerade as a digest.
_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")

SubagentStatusValue = Literal[
    "completed",
    "failed",
    "cancelled",
    "timed_out",
    "polling_timed_out",
]

#: Enumeration of every value ``subagent_status`` may take. Non-clean
#: completions do NOT get their own status value: usable partial work is
#: ``completed`` and no usable output is ``failed``, with the exact reason on
#: the additive ``subagent_stop_reason`` field so old consumers keep working.
SUBAGENT_STATUS_VALUES: tuple[SubagentStatusValue, ...] = (
    "completed",
    "failed",
    "cancelled",
    "timed_out",
    "polling_timed_out",
)

#: Why a run ended without a clean final response. Carried on the additive
#: ``subagent_stop_reason`` field, never as a status enum value.
SubagentStopReasonValue = Literal[
    "token_capped",
    "turn_capped",
    "loop_capped",
    "tool_budget_capped",
    "output_truncated",
]

SUBAGENT_STOP_REASON_VALUES: tuple[SubagentStopReasonValue, ...] = (
    "token_capped",
    "turn_capped",
    "loop_capped",
    "tool_budget_capped",
    "output_truncated",
)

#: Human-readable label folded into the model-visible result text when a cap
#: fired, e.g. ``Task Succeeded (capped: token budget). Result: ...``.
_STOP_REASON_LABELS: dict[SubagentStopReasonValue, str] = {
    "token_capped": "token budget",
    "turn_capped": "turn budget",
    "loop_capped": "repeated tool-call loop",
    "tool_budget_capped": "tool-call budget",
}

#: Statuses that carry a recoverable result in ``subagent_result_brief`` /
#: ``subagent_result_sha256``. Only ``completed`` carries usable work, including
#: a guardrail-capped or Provider-truncated partial result (+ ``stop_reason``).
#: Other non-completed statuses carry only ``subagent_error``.
_RESULT_BEARING_STATUSES: frozenset[SubagentStatusValue] = frozenset({"completed"})

#: Read-side normalization for status values that previously appeared in
#: checkpointed thread history but are no longer produced. ``max_turns_reached``
#: was emitted by Phase 1 (#3949) and lives in persisted
#: ``ToolMessage.additional_kwargs``; #3980 removed it from the producer and the
#: contract fixture, but the reader still maps it to its Phase 2 cap equivalent
#: so historical data resolves terminally (with the cap on ``stop_reason``)
#: instead of stranding as ``in_progress`` in the delegation ledger. The frontend
#: ``subtask-result.ts`` keeps a parallel deprecated alias for the same reason.
_LEGACY_STATUS_NORMALIZATION: dict[str, SubagentStopReasonValue] = {
    "max_turns_reached": "turn_capped",
}


class StructuredSubagentResult(TypedDict):
    status: SubagentStatusValue
    stop_reason: NotRequired[SubagentStopReasonValue]
    result_brief: NotRequired[str]
    result_sha256: NotRequired[str]
    error: NotRequired[str]


def _bound_metadata_text(text: str, cap: int = SUBAGENT_METADATA_TEXT_MAX_CHARS) -> str:
    cleaned = text.strip()
    if len(cleaned) <= cap:
        return cleaned
    marker = "\n...\n"
    if cap <= len(marker):
        return cleaned[:cap]
    head = cap * 2 // 3
    tail = cap - head - len(marker)
    if tail <= 0:
        return cleaned[:cap]
    return f"{cleaned[:head]}{marker}{cleaned[-tail:]}"


def make_subagent_additional_kwargs(
    status: SubagentStatusValue,
    *,
    result: str | None = None,
    error: str | None = None,
    stop_reason: SubagentStopReasonValue | None = None,
    model_name: str | None = None,
    token_usage: Mapping[str, object] | None = None,
    usage_receipt_id: str | None = None,
    usage_completeness: SubagentUsageCompletenessValue | None = None,
) -> dict[str, object]:
    """Build the ``additional_kwargs`` payload the middleware stamps.

    Drops the error field when blank so the JSON wire format never carries
    a misleading empty ``subagent_error: ""``. ``stop_reason`` is stamped
    only for a recognized non-clean completion reason (see
    :data:`SUBAGENT_STOP_REASON_VALUES`).

    Raises:
        ValueError: when ``status`` is not in :data:`SUBAGENT_STATUS_VALUES`,
            or ``stop_reason`` is not in :data:`SUBAGENT_STOP_REASON_VALUES`.
            We do not accept arbitrary strings: a typo would silently leak
            through to consumers as missing metadata rather than failing
            loudly at the producer boundary.
    """
    if status not in SUBAGENT_STATUS_VALUES:
        raise ValueError(f"invalid subagent status {status!r}; expected one of {SUBAGENT_STATUS_VALUES}")
    if stop_reason is not None and stop_reason not in SUBAGENT_STOP_REASON_VALUES:
        raise ValueError(f"invalid subagent stop_reason {stop_reason!r}; expected one of {SUBAGENT_STOP_REASON_VALUES}")
    if usage_completeness is not None and usage_completeness not in SUBAGENT_USAGE_COMPLETENESS_VALUES:
        raise ValueError(
            f"invalid subagent usage_completeness {usage_completeness!r}; expected one of {SUBAGENT_USAGE_COMPLETENESS_VALUES}",
        )
    payload: dict[str, object] = {SUBAGENT_STATUS_KEY: status}
    if status in _RESULT_BEARING_STATUSES and isinstance(result, str) and result.strip():
        payload[SUBAGENT_RESULT_BRIEF_KEY] = _bound_metadata_text(result)
        payload[SUBAGENT_RESULT_SHA256_KEY] = hashlib.sha256(result.encode("utf-8")).hexdigest()
    # Only ``completed`` (clean or with usable partial work) suppresses the
    # error blob; every other status carries it.
    if status != "completed" and isinstance(error, str) and error.strip():
        payload[SUBAGENT_ERROR_KEY] = _bound_metadata_text(error)
    if stop_reason is not None:
        payload[SUBAGENT_STOP_REASON_KEY] = stop_reason
    if isinstance(model_name, str) and model_name.strip():
        payload[SUBAGENT_MODEL_NAME_KEY] = model_name.strip()
    normalized_usage = normalize_token_usage(token_usage)
    if normalized_usage is not None:
        payload[SUBAGENT_TOKEN_USAGE_KEY] = normalized_usage
    if usage_receipt_id is not None:
        if not isinstance(usage_receipt_id, str):
            raise ValueError("usage_receipt_id must be a string")
        normalized_receipt_id = usage_receipt_id.strip()
        if not normalized_receipt_id:
            raise ValueError("usage_receipt_id must not be blank")
        if len(normalized_receipt_id) > SUBAGENT_USAGE_RECEIPT_ID_MAX_CHARS:
            raise ValueError(f"usage_receipt_id exceeds {SUBAGENT_USAGE_RECEIPT_ID_MAX_CHARS} characters")
        payload[SUBAGENT_USAGE_RECEIPT_ID_KEY] = normalized_receipt_id
    if usage_completeness is not None:
        payload[SUBAGENT_USAGE_COMPLETENESS_KEY] = usage_completeness
    return payload


def normalize_token_usage(value: Any) -> dict[str, int] | None:
    """Validate one cumulative token snapshot into the public wire shape."""
    if not isinstance(value, Mapping):
        return None
    normalized: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        amount = value.get(key)
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            return None
        normalized[key] = amount
    return normalized


def read_subagent_usage_receipt(
    metadata: Mapping[str, object] | None,
) -> tuple[str, dict[str, int]] | None:
    """Read one immutable aggregate receipt from ToolMessage metadata."""

    if not isinstance(metadata, Mapping):
        return None
    raw_receipt_id = metadata.get(SUBAGENT_USAGE_RECEIPT_ID_KEY)
    if isinstance(raw_receipt_id, str):
        receipt_id = raw_receipt_id.strip()
    else:
        receipt_id = ""
    usage = normalize_token_usage(metadata.get(SUBAGENT_TOKEN_USAGE_KEY))
    if not receipt_id or len(receipt_id) > SUBAGENT_USAGE_RECEIPT_ID_MAX_CHARS or usage is None:
        return None
    return receipt_id, usage


def read_subagent_usage_receipt_state(
    metadata: Mapping[str, object] | None,
) -> tuple[dict[str, int], dict[str, dict[str, int]], frozenset[str]] | None:
    """Read the replay-safe baseline and aggregate receipt contributions."""

    if not isinstance(metadata, Mapping):
        return None
    payload = metadata.get(SUBAGENT_USAGE_RECEIPT_STATE_KEY)
    if not isinstance(payload, Mapping) or type(payload.get("version")) is not int or payload.get("version") != SUBAGENT_USAGE_RECEIPT_STATE_VERSION:
        return None
    baseline = normalize_token_usage(payload.get("baseline"))
    raw_contributions = payload.get("contributions")
    if baseline is None or not isinstance(raw_contributions, list):
        return None

    contributions: dict[str, dict[str, int]] = {}
    for raw_contribution in raw_contributions:
        if not isinstance(raw_contribution, Mapping):
            return None
        raw_receipt_id = raw_contribution.get("receipt_id")
        receipt_id = raw_receipt_id.strip() if isinstance(raw_receipt_id, str) else ""
        usage = normalize_token_usage(raw_contribution.get("usage"))
        if not receipt_id or len(receipt_id) > SUBAGENT_USAGE_RECEIPT_ID_MAX_CHARS or usage is None or receipt_id in contributions:
            return None
        contributions[receipt_id] = usage

    raw_conflicts = payload.get("conflicts", [])
    if not isinstance(raw_conflicts, list):
        return None
    conflicts: set[str] = set()
    for raw_conflict in raw_conflicts:
        receipt_id = raw_conflict.strip() if isinstance(raw_conflict, str) else ""
        if not receipt_id or len(receipt_id) > SUBAGENT_USAGE_RECEIPT_ID_MAX_CHARS or receipt_id in conflicts or receipt_id in contributions:
            return None
        conflicts.add(receipt_id)
    return baseline, contributions, frozenset(conflicts)


def format_subagent_result_message(
    status: SubagentStatusValue,
    *,
    result: str | None = None,
    error: str | None = None,
    stop_reason: SubagentStopReasonValue | None = None,
) -> tuple[str, str | None]:
    """Return model-visible task content plus normalized metadata error.

    Guardrail caps use a short ``(capped: ...)`` note. Provider output
    truncation uses distinct partial-result wording so the Lead cannot mistake
    incomplete text for a successful answer.
    """
    result_text = "" if result is None else str(result)
    error_text = str(error).strip() if isinstance(error, str) else ""
    capped = _STOP_REASON_LABELS.get(stop_reason) if stop_reason is not None else None

    if status == "completed":
        if stop_reason == "output_truncated":
            return (
                f"Task output was truncated by the Provider. Partial result: {result_text}",
                None,
            )
        if capped:
            return f"Task Succeeded (capped: {capped}). Result: {result_text}", None
        return f"Task Succeeded. Result: {result_text}", None

    if status == "cancelled":
        detail = error_text or "Task cancelled by user."
        if detail == "Task cancelled by user.":
            return detail, detail
        return f"Task cancelled by user. Error: {detail}", detail

    if status == "timed_out":
        detail = error_text or "Task timed out."
        if detail == "Task timed out.":
            return detail, detail
        return f"Task timed out. Error: {detail}", detail

    if status == "polling_timed_out":
        detail = error_text or "Task polling timed out."
        return detail, detail

    # ``failed`` — including a turn-capped run that produced no usable output
    # (``stop_reason=turn_capped``): the cap note is folded in so the lead can
    # tell a broken subagent from one that simply ran out of turn budget.
    detail = error_text or "Task failed."
    if stop_reason == "output_truncated":
        if detail == "Task failed.":
            return "Task failed: Provider output was truncated before a usable result was produced.", detail
        return (
            f"Task failed: Provider output was truncated before a usable result was produced. Error: {detail}",
            detail,
        )
    if capped:
        if detail == "Task failed.":
            return f"Task failed (capped: {capped}).", detail
        return f"Task failed (capped: {capped}). Error: {detail}", detail
    if detail == "Task failed.":
        return detail, detail
    return f"Task failed. Error: {detail}", detail


def read_subagent_result_metadata(
    additional_kwargs: Mapping[str, object] | None,
) -> StructuredSubagentResult | None:
    if not additional_kwargs:
        return None
    raw_status = additional_kwargs.get(SUBAGENT_STATUS_KEY)
    # Legacy checkpointed values (#3949) are no longer produced (#3980) but
    # survive in persisted history. Normalize them before the validity check so
    # they resolve terminally instead of returning ``None`` (which would strand
    # the delegation entry as ``in_progress``). A legacy ``max_turns_reached``
    # carried a recovered partial, so a payload that still has ``result_brief``
    # maps to the Phase 2 ``completed + turn_capped`` shape (partial survives on
    # the wire); one with no result maps to ``failed + turn_capped``.
    legacy_stop_reason = _LEGACY_STATUS_NORMALIZATION.get(raw_status) if isinstance(raw_status, str) else None
    if legacy_stop_reason is not None:
        raw_result_brief = additional_kwargs.get(SUBAGENT_RESULT_BRIEF_KEY)
        status = "completed" if (isinstance(raw_result_brief, str) and raw_result_brief.strip()) else "failed"
    elif raw_status in SUBAGENT_STATUS_VALUES:
        status = raw_status
    else:
        return None
    payload: StructuredSubagentResult = {"status": status}
    raw_result = additional_kwargs.get(SUBAGENT_RESULT_BRIEF_KEY)
    raw_hash = additional_kwargs.get(SUBAGENT_RESULT_SHA256_KEY)
    raw_error = additional_kwargs.get(SUBAGENT_ERROR_KEY)
    if status in _RESULT_BEARING_STATUSES and isinstance(raw_result, str) and raw_result.strip():
        payload["result_brief"] = _bound_metadata_text(raw_result)
        if isinstance(raw_hash, str) and _SHA256_HEX_RE.fullmatch(raw_hash):
            payload["result_sha256"] = raw_hash
    if status != "completed" and isinstance(raw_error, str) and raw_error.strip():
        payload["error"] = _bound_metadata_text(raw_error)
    # An explicit stop_reason on the wire wins; else the synthesized legacy reason.
    raw_stop_reason = additional_kwargs.get(SUBAGENT_STOP_REASON_KEY)
    if isinstance(raw_stop_reason, str) and raw_stop_reason in SUBAGENT_STOP_REASON_VALUES:
        payload["stop_reason"] = raw_stop_reason
    elif legacy_stop_reason is not None:
        payload["stop_reason"] = legacy_stop_reason
    return payload
