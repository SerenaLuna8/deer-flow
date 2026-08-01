"""Memory updater for reading, writing, and updating memory data."""

import asyncio
import copy
import html
import json
import logging
import math
import os
import re
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import Any

from deerflow.agents.memory.prompt import (
    MEMORY_UPDATE_PROMPT,
    STALENESS_REVIEW_PROMPT,
    format_conversation_for_update,
)
from deerflow.agents.memory.storage import utc_now_iso_z
from deerflow.config.app_config import AppConfig
from deerflow.config.memory_config import MemoryConfig
from deerflow.models import create_chat_model
from deerflow.private_scope import PrivateResourceScope
from deerflow.trace_context import request_trace_context
from deerflow.tracing import inject_langfuse_metadata

logger = logging.getLogger(__name__)


def _coerce_source_confidence(fact: dict[str, Any]) -> float:
    """Return a finite, bounded confidence for persisted/imported facts."""

    raw = fact.get("confidence")
    if raw is None or isinstance(raw, bool):
        return 0.5
    try:
        confidence = float(raw)
    except (TypeError, ValueError):
        return 0.5
    if not math.isfinite(confidence):
        return 0.5
    return max(0.0, min(confidence, 1.0))


def _trim_facts_to_max(
    facts: list[dict[str, Any]],
    max_facts: int,
) -> list[dict[str, Any]]:
    """Keep the highest-confidence facts without trusting stored types."""

    if len(facts) <= max_facts:
        return facts
    return sorted(
        facts,
        key=_coerce_source_confidence,
        reverse=True,
    )[:max_facts]


def _extract_text(content: Any) -> str:
    """Extract plain text from LLM response content (str or list of content blocks).

    Modern LLMs may return structured content as a list of blocks instead of a
    plain string, e.g. [{"type": "text", "text": "..."}]. Using str() on such
    content produces Python repr instead of the actual text, breaking JSON
    parsing downstream.

    String chunks are concatenated without separators to avoid corrupting
    chunked JSON/text payloads. Dict-based text blocks are treated as full text
    blocks and joined with newlines for readability.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        pending_str_parts: list[str] = []

        def flush_pending_str_parts() -> None:
            if pending_str_parts:
                pieces.append("".join(pending_str_parts))
                pending_str_parts.clear()

        for block in content:
            if isinstance(block, str):
                pending_str_parts.append(block)
            elif isinstance(block, dict):
                flush_pending_str_parts()
                text_val = block.get("text")
                if isinstance(text_val, str):
                    pieces.append(text_val)

        flush_pending_str_parts()
        return "\n".join(pieces)
    return str(content)


_REQUIRED_MEMORY_UPDATE_TOP_LEVEL_KEYS = frozenset({"user", "history", "newFacts", "factsToRemove"})


def _normalize_memory_update_fact(fact: Any) -> dict[str, Any] | None:
    """Normalize a single fact entry from a model-produced memory update."""
    if not isinstance(fact, dict):
        return None

    raw_content = fact.get("content")
    if not isinstance(raw_content, str):
        return None
    content = raw_content.strip()
    if not content:
        return None

    raw_category = fact.get("category")
    category = raw_category.strip() if isinstance(raw_category, str) and raw_category.strip() else "context"

    raw_confidence = fact.get("confidence", 0.5)
    if isinstance(raw_confidence, bool):
        return None
    if isinstance(raw_confidence, str):
        raw_confidence = raw_confidence.strip()
        if not raw_confidence:
            return None
        try:
            raw_confidence = float(raw_confidence)
        except ValueError:
            return None
    elif isinstance(raw_confidence, (int, float)):
        raw_confidence = float(raw_confidence)
    else:
        return None

    if not math.isfinite(raw_confidence):
        return None

    normalized_fact = {
        "content": content,
        "category": category,
        "confidence": raw_confidence,
    }
    source_error = fact.get("sourceError")
    if isinstance(source_error, str):
        normalized_source_error = source_error.strip()
        if normalized_source_error:
            normalized_fact["sourceError"] = normalized_source_error

    return normalized_fact


def _normalize_memory_update_data(update_data: dict[str, Any]) -> dict[str, Any]:
    """Coerce parsed memory update data into the shape consumed by _apply_updates."""
    user = update_data.get("user")
    history = update_data.get("history")
    new_facts = update_data.get("newFacts")
    facts_to_remove = update_data.get("factsToRemove")
    normalized_facts_to_remove = [fact_id for fact_id in facts_to_remove if isinstance(fact_id, str)] if isinstance(facts_to_remove, list) else []
    normalized_new_facts = []
    dropped_new_fact = not isinstance(new_facts, list)
    if isinstance(new_facts, list):
        for fact in new_facts:
            normalized_fact = _normalize_memory_update_fact(fact)
            if normalized_fact is not None:
                normalized_new_facts.append(normalized_fact)
            else:
                dropped_new_fact = True

    if normalized_facts_to_remove and dropped_new_fact:
        raise json.JSONDecodeError(
            "Unsafe partial memory update: factsToRemove with malformed newFacts",
            json.dumps(update_data, ensure_ascii=False),
            0,
        )

    # ── Normalize staleness review removals ──
    stale_removals_raw = update_data.get("staleFactsToRemove")
    normalized_stale_removals: list[dict[str, str]] = []
    if isinstance(stale_removals_raw, list):
        for entry in stale_removals_raw:
            if not isinstance(entry, dict):
                continue
            fact_id = entry.get("id")
            if not isinstance(fact_id, str) or not fact_id:
                continue
            reason = entry.get("reason", "")
            normalized_stale_removals.append(
                {
                    "id": fact_id,
                    "reason": reason if isinstance(reason, str) else "",
                }
            )

    return {
        "user": user if isinstance(user, dict) else {},
        "history": history if isinstance(history, dict) else {},
        "newFacts": normalized_new_facts,
        "factsToRemove": normalized_facts_to_remove,
        "staleFactsToRemove": normalized_stale_removals,
    }


def _parse_memory_update_response(response_content: Any) -> dict[str, Any]:
    """Parse the first valid memory-update JSON object from an LLM response.

    Some providers may wrap JSON in thinking traces, prose, or markdown fences
    even when prompted to return JSON only. This parser accepts safely
    extractable JSON objects but does not repair truncated or malformed JSON.
    """
    response_text = _extract_text(response_content).strip()
    decoder = json.JSONDecoder()

    for match in re.finditer(r"\{", response_text):
        try:
            parsed, _end = decoder.raw_decode(response_text[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and _REQUIRED_MEMORY_UPDATE_TOP_LEVEL_KEYS.issubset(parsed):
            return _normalize_memory_update_data(parsed)

    raise json.JSONDecodeError("No valid memory update JSON object found", response_text, 0)


# Matches sentences that describe a file-upload *event* rather than general
# file-related work.  Deliberately narrow to avoid removing legitimate facts
# such as "User works with CSV files" or "prefers PDF export".
_UPLOAD_SENTENCE_RE = re.compile(
    r"[^.!?]*\b(?:"
    r"upload(?:ed|ing)?(?:\s+\w+){0,3}\s+(?:file|files?|document|documents?|attachment|attachments?)"
    r"|file\s+upload"
    r"|/mnt/user-data/uploads/"
    r"|<uploaded_files>"
    r")[^.!?]*[.!?]?\s*",
    re.IGNORECASE,
)


def _strip_upload_mentions_from_memory(memory_data: dict[str, Any]) -> dict[str, Any]:
    """Remove sentences about file uploads from all memory summaries and facts.

    Uploaded files are session-scoped; persisting upload events in long-term
    memory causes the agent to search for non-existent files in future sessions.
    """
    # Scrub summaries in user/history sections
    for section in ("user", "history"):
        section_data = memory_data.get(section, {})
        for _key, val in section_data.items():
            if isinstance(val, dict) and "summary" in val:
                cleaned = _UPLOAD_SENTENCE_RE.sub("", val["summary"]).strip()
                cleaned = re.sub(r"  +", " ", cleaned)
                val["summary"] = cleaned

    # Also remove any facts that describe upload events
    facts = memory_data.get("facts", [])
    if facts:
        memory_data["facts"] = [f for f in facts if not _UPLOAD_SENTENCE_RE.search(f.get("content", ""))]

    return memory_data


def _fact_content_key(content: Any) -> str | None:
    if not isinstance(content, str):
        return None
    stripped = content.strip()
    if not stripped:
        return None
    return stripped.casefold()


# ── Staleness review helpers ──────────────────────────────────────────────


def _parse_fact_datetime(raw: str) -> datetime | None:
    """Parse an ISO-8601 datetime string from a fact's createdAt field.

    Returns ``None`` on any parse failure so callers can safely skip malformed facts.
    """
    if not raw:
        return None
    try:
        result = datetime.fromisoformat(raw)
        # Naive datetimes (no tzinfo) would cause TypeError when compared
        # with the timezone-aware cutoff.  Assume UTC for safety.
        if result.tzinfo is None:
            result = result.replace(tzinfo=UTC)
        return result
    except (ValueError, TypeError):
        return None


def _select_stale_candidates(
    current_memory: dict[str, Any],
    config: Any,
) -> list[dict[str, Any]]:
    """Return facts that are older than ``staleness_age_days`` and not protected.

    Protected categories (default: ``correction``) are excluded because they
    represent explicit user feedback that should not be auto-pruned by age.
    """
    cutoff = datetime.now(UTC) - timedelta(days=config.staleness_age_days)
    protected = frozenset(config.staleness_protected_categories)
    candidates: list[dict[str, Any]] = []
    for fact in current_memory.get("facts", []):
        if not isinstance(fact, dict):
            continue
        category = fact.get("category", "")
        if isinstance(category, str) and category in protected:
            continue
        created_at = _parse_fact_datetime(fact.get("createdAt", ""))
        if created_at is not None and created_at < cutoff:
            candidates.append(fact)
    return candidates


def _build_staleness_section(
    stale_candidates: list[dict[str, Any]],
    age_days: int,
) -> str:
    """Format the staleness review prompt section from candidate facts."""
    if not stale_candidates:
        return ""
    lines: list[str] = []
    for fact in stale_candidates:
        fid = fact.get("id", "?")
        cat = html.escape(
            str(fact.get("category", "context")).strip() or "context",
            quote=False,
        )
        conf = _coerce_source_confidence(fact)
        created_raw = fact.get("createdAt", "")
        created_short = created_raw[:10] if isinstance(created_raw, str) and len(created_raw) >= 10 else created_raw
        content = html.escape(str(fact.get("content", "")), quote=False)
        lines.append(f'- [{fid} | {cat} | {conf:.2f} | {created_short}] "{content}"')
    return STALENESS_REVIEW_PROMPT.format(
        stale_facts="\n".join(lines),
        age_days=age_days,
    )


def _escape_memory_for_prompt(memory: Any) -> Any:
    """Copy a memory value while escaping every string leaf for XML embedding."""

    if isinstance(memory, str):
        return html.escape(memory)
    if isinstance(memory, dict):
        return {(html.escape(key, quote=False) if isinstance(key, str) else key): _escape_memory_for_prompt(value) for key, value in memory.items()}
    if isinstance(memory, list):
        return [_escape_memory_for_prompt(item) for item in memory]
    return memory


class MemoryUpdater:
    """Updates memory using LLM based on conversation context."""

    def __init__(self, model_name: str | None = None):
        """Initialize the memory updater.

        Args:
            model_name: Optional model name to use. If None, uses config or default.
        """
        self._model_name = model_name

    def _get_model(
        self,
        memory_config: MemoryConfig,
        app_config: AppConfig,
    ):
        """Get the model for memory updates."""
        return create_chat_model(
            name=self._resolve_model_name(memory_config),
            thinking_enabled=False,
            app_config=app_config,
        )

    def _resolve_model_name(
        self,
        memory_config: MemoryConfig,
    ) -> str | None:
        """Return the configured model name for memory updates."""
        return self._model_name or memory_config.model_name

    def _build_correction_hint(
        self,
        correction_detected: bool,
        reinforcement_detected: bool,
    ) -> str:
        """Build optional prompt hints for correction and reinforcement signals."""
        correction_hint = ""
        if correction_detected:
            correction_hint = (
                "IMPORTANT: Explicit correction signals were detected in this conversation. "
                "Pay special attention to what the agent got wrong, what the user corrected, "
                "and record the correct approach as a fact with category "
                '"correction" and confidence >= 0.95 when appropriate.'
            )
        if reinforcement_detected:
            reinforcement_hint = (
                "IMPORTANT: Positive reinforcement signals were detected in this conversation. "
                "The user explicitly confirmed the agent's approach was correct or helpful. "
                "Record the confirmed approach, style, or preference as a fact with category "
                '"preference" or "behavior" and confidence >= 0.9 when appropriate.'
            )
            correction_hint = (correction_hint + "\n" + reinforcement_hint).strip() if correction_hint else reinforcement_hint

        return correction_hint

    async def aupdate_project_memory(
        self,
        *,
        storage: Any,
        scope: PrivateResourceScope,
        namespace: str,
        messages: tuple[Any, ...] | list[Any],
        thread_id: str,
        run_id: str,
        memory_config: MemoryConfig,
        app_config: AppConfig,
        correction_detected: bool = False,
        reinforcement_detected: bool = False,
        deerflow_trace_id: str | None = None,
        langfuse_trace_correlation_enabled: bool = False,
    ) -> bool:
        """Update PostgreSQL project memory without consulting user ContextVars."""

        trace_ctx = request_trace_context(deerflow_trace_id) if deerflow_trace_id else nullcontext()
        with trace_ctx:
            try:
                if type(memory_config) is not MemoryConfig:
                    raise TypeError("project memory update requires exact MemoryConfig")
                if type(app_config) is not AppConfig:
                    raise TypeError("project memory update requires exact AppConfig")
                config = memory_config
                if not config.enabled or not messages:
                    return False
                snapshot = await storage.load(scope=scope, namespace=namespace)
                current_memory = snapshot.memory
                conversation_text = format_conversation_for_update(list(messages))
                if not conversation_text.strip():
                    return False

                correction_hint = self._build_correction_hint(
                    correction_detected=correction_detected,
                    reinforcement_detected=reinforcement_detected,
                )
                staleness_section = ""
                if config.staleness_review_enabled:
                    stale_candidates = _select_stale_candidates(current_memory, config)
                    if len(stale_candidates) >= config.staleness_min_candidates:
                        staleness_section = _build_staleness_section(
                            stale_candidates,
                            config.staleness_age_days,
                        )
                prompt = MEMORY_UPDATE_PROMPT.format(
                    current_memory=json.dumps(
                        _escape_memory_for_prompt(current_memory),
                        indent=2,
                        ensure_ascii=False,
                    ),
                    conversation=conversation_text,
                    correction_hint=correction_hint,
                    staleness_review_section=staleness_section,
                )
                model_name = self._resolve_model_name(config)
                invoke_config: dict[str, Any] = {"run_name": "memory_agent"}
                inject_langfuse_metadata(
                    invoke_config,
                    thread_id=thread_id,
                    user_id=scope.owner_user_id,
                    assistant_id="memory_agent",
                    model_name=model_name,
                    environment=os.environ.get("DEER_FLOW_ENV") or os.environ.get("ENVIRONMENT"),
                    deerflow_trace_id=deerflow_trace_id,
                    include_deerflow_trace_id=langfuse_trace_correlation_enabled,
                )
                response = await asyncio.to_thread(
                    self._get_model(config, app_config).invoke,
                    prompt,
                    config=invoke_config,
                )
                update_data = _parse_memory_update_response(response.content)
                existing_fact_ids = {fact.get("id") for fact in current_memory.get("facts", []) if isinstance(fact, dict)}
                updated_memory = self._apply_updates(
                    copy.deepcopy(current_memory),
                    update_data,
                    thread_id,
                    memory_config=config,
                )
                for fact in updated_memory.get("facts", []):
                    if not isinstance(fact, dict) or fact.get("id") in existing_fact_ids:
                        continue
                    fact["sourceThreadId"] = thread_id
                    fact["sourceRunId"] = run_id
                updated_memory = _strip_upload_mentions_from_memory(updated_memory)
                await storage.save(
                    updated_memory,
                    scope=scope,
                    namespace=namespace,
                    expected_version=snapshot.version,
                )
                return True
            except json.JSONDecodeError as exc:
                logger.warning("Failed to parse LLM response for project memory update: %s", exc)
                return False
            except Exception as exc:
                logger.exception("Project memory update failed: %s", exc)
                return False

    def _apply_updates(
        self,
        current_memory: dict[str, Any],
        update_data: dict[str, Any],
        thread_id: str | None = None,
        *,
        memory_config: MemoryConfig,
    ) -> dict[str, Any]:
        """Apply LLM-generated updates to memory.

        Args:
            current_memory: Current memory data.
            update_data: Updates from LLM.
            thread_id: Optional thread ID for tracking.

        Returns:
            Updated memory data.
        """
        if type(memory_config) is not MemoryConfig:
            raise TypeError("memory updates require exact MemoryConfig")
        config = memory_config
        now = utc_now_iso_z()

        # Update user sections
        user_updates = update_data.get("user", {})
        for section in ["workContext", "personalContext", "topOfMind"]:
            section_data = user_updates.get(section, {})
            if section_data.get("shouldUpdate") and section_data.get("summary"):
                current_memory["user"][section] = {
                    "summary": section_data["summary"],
                    "updatedAt": now,
                }

        # Update history sections
        history_updates = update_data.get("history", {})
        for section in ["recentMonths", "earlierContext", "longTermBackground"]:
            section_data = history_updates.get(section, {})
            if section_data.get("shouldUpdate") and section_data.get("summary"):
                current_memory["history"][section] = {
                    "summary": section_data["summary"],
                    "updatedAt": now,
                }

        # Remove facts (contradiction-based)
        facts_to_remove = set(update_data.get("factsToRemove", []))
        if facts_to_remove:
            current_memory["facts"] = [f for f in current_memory.get("facts", []) if f.get("id") not in facts_to_remove]

        # ── Staleness review removals ──
        stale_removals = update_data.get("staleFactsToRemove", [])
        if isinstance(stale_removals, list) and stale_removals:
            stale_ids_to_remove = {entry["id"] for entry in stale_removals if isinstance(entry, dict) and "id" in entry}

            # Deterministic guardrail: intersect with actual staleness
            # candidates so an LLM slip that emits a protected-category or
            # non-aged fact id is silently rejected.  Runs unconditionally
            # so the apply-layer protection is independent of model behavior
            # AND of the staleness_review_enabled flag.
            candidate_ids = {fact["id"] for fact in _select_stale_candidates(current_memory, config) if fact.get("id") is not None}
            stale_ids_to_remove &= candidate_ids

            if not stale_ids_to_remove:
                # After intersection with candidate set, nothing to remove.
                stale_removals = []
            else:
                # Safety cap: limit max staleness removals per cycle.
                # When the LLM returns more than the cap, keep only the
                # lowest-confidence entries up to the limit so the most
                # questionable facts are removed first.
                max_stale = config.staleness_max_removals_per_cycle
                if len(stale_ids_to_remove) > max_stale:
                    stale_facts = [f for f in current_memory.get("facts", []) if f.get("id") in stale_ids_to_remove]
                    stale_facts.sort(key=_coerce_source_confidence)
                    stale_ids_to_remove = {f["id"] for f in stale_facts[:max_stale]}

                current_memory["facts"] = [f for f in current_memory.get("facts", []) if f.get("id") not in stale_ids_to_remove]

            # Log removals for observability
            for entry in stale_removals:
                if isinstance(entry, dict) and entry.get("id") in stale_ids_to_remove:
                    logger.info(
                        "Staleness review removed fact %s: %s",
                        entry["id"],
                        entry.get("reason", "no reason provided"),
                    )

        # Add new facts
        existing_fact_keys = {fact_key for fact_key in (_fact_content_key(fact.get("content")) for fact in current_memory.get("facts", [])) if fact_key is not None}
        new_facts = update_data.get("newFacts", [])
        for fact in new_facts:
            confidence = fact.get("confidence", 0.5)
            if confidence >= config.fact_confidence_threshold:
                raw_content = fact.get("content", "")
                if not isinstance(raw_content, str):
                    continue
                normalized_content = raw_content.strip()
                fact_key = _fact_content_key(normalized_content)
                if fact_key is None:
                    # Empty / whitespace-only content: skip it the same way the
                    # non-string guard above does, instead of appending a blank
                    # fact that violates the non-empty-content invariant.
                    continue
                if fact_key in existing_fact_keys:
                    continue

                fact_entry = {
                    "id": f"fact_{uuid.uuid4().hex[:8]}",
                    "content": normalized_content,
                    "category": fact.get("category", "context"),
                    "confidence": confidence,
                    "createdAt": now,
                    "source": thread_id or "unknown",
                }
                source_error = fact.get("sourceError")
                if isinstance(source_error, str):
                    normalized_source_error = source_error.strip()
                    if normalized_source_error:
                        fact_entry["sourceError"] = normalized_source_error
                current_memory["facts"].append(fact_entry)
                if fact_key is not None:
                    existing_fact_keys.add(fact_key)

        # Enforce max facts limit
        current_memory["facts"] = _trim_facts_to_max(
            current_memory["facts"],
            config.max_facts,
        )

        return current_memory
