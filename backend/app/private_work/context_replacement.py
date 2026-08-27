"""Checkpoint adapters for branch, history replacement, and compaction."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import uuid
from collections.abc import Mapping, Sequence

from langchain_core.messages import BaseMessage, message_to_dict

from deerflow.persistence.context_evidence import (
    ContextEvidenceRecord,
    ContextSubjectRef,
)
from deerflow.runtime.context_evidence import (
    CheckpointLinkedV1,
    CompactionAuthority,
    CompactionProjection,
    ContextCheckpointEstimator,
    ContextCheckpointProjectionSnapshot,
    ContextCompactionCheckpointReceipt,
    ContextContribution,
    ContextLane,
    ContextModelProjection,
    ContextProjectionSource,
    ContextSubject,
    ContextWindowGeneration,
    FinalRequestMeasurement,
    ProjectionFreshness,
    ProjectionPhase,
    RequestPreparedV1,
    TokenEstimate,
    VisualCostStrategy,
    VisualDetail,
    VisualMeasurementMetadata,
    WindowOpenedV1,
)

_DYNAMIC_LANES = frozenset(
    {
        ContextLane.SUMMARIZED_CONVERSATION,
        ContextLane.CONVERSATION,
        ContextLane.VISUAL_MEDIA,
        ContextLane.PROVIDER_OVERHEAD,
    }
)
_VISUAL_BLOCK_TYPES = frozenset({"image", "image_url", "input_image"})
_SUPPORTED_IMAGE_MIME_TYPES = frozenset({"image/gif", "image/jpeg", "image/png", "image/webp"})
_UNKNOWN_CONTEXT_MODEL_IDENTITY = hashlib.sha256(
    b"unknown-context-model-v1",
).hexdigest()
_UNKNOWN_FIXED_CLOSURE = (
    "system_prompt",
    "agent_instructions",
    "tool_definitions",
    "skills",
    "mcp_dynamic_tools",
    "subagent_definitions",
    "provider_overhead",
)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def deterministic_generation_id(
    *,
    thread_id: str,
    operation: str,
    source_checkpoint_id: str,
    result_checkpoint_id: str,
) -> uuid.UUID:
    """Derive a retry-stable generation identity from one replacement."""

    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        ":".join(
            (
                "actweave-context-generation-v1",
                thread_id,
                operation,
                source_checkpoint_id,
                result_checkpoint_id,
            )
        ),
    )


def history_digest(
    *,
    source_thread_id: str,
    source_checkpoint_id: str,
    result_checkpoint_id: str,
    checkpoint_values: Mapping[str, object] | None = None,
) -> str:
    """Hash replacement history without retaining any message or summary."""

    values = checkpoint_values or {}
    messages = values.get("messages")
    summary = values.get("summary_text")
    return _digest(
        {
            "source_thread_id": source_thread_id,
            "source_checkpoint_id": source_checkpoint_id,
            "result_checkpoint_id": result_checkpoint_id,
            "messages_digest": _digest(_message_payloads(messages)),
            "summary_digest": hashlib.sha256((summary if isinstance(summary, str) else "").encode("utf-8")).hexdigest(),
        }
    )


def _message_payload(message: object) -> dict[str, object]:
    if isinstance(message, BaseMessage):
        return message_to_dict(message)
    if isinstance(message, Mapping):
        return dict(message)
    raise TypeError("Context replacement messages must be LangChain messages")


def _message_payloads(value: object) -> list[dict[str, object]]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("Context replacement checkpoint messages are invalid")
    return [_message_payload(message) for message in value]


def _visual_url(block: Mapping[str, object]) -> str | None:
    raw = block.get("image_url")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, Mapping) and isinstance(raw.get("url"), str):
        return raw["url"]
    for key in ("url", "data"):
        value = block.get(key)
        if isinstance(value, str):
            return value
    source = block.get("source")
    if isinstance(source, Mapping):
        value = source.get("data") or source.get("url")
        if isinstance(value, str):
            return value
    return None


def _visual_metadata(block: Mapping[str, object]) -> VisualMeasurementMetadata:
    raw_url = _visual_url(block)
    declared_mime = block.get("mime_type") or block.get("media_type")
    source = block.get("source")
    if declared_mime is None and isinstance(source, Mapping):
        declared_mime = source.get("media_type")
    mime_type = str(declared_mime) if declared_mime in _SUPPORTED_IMAGE_MIME_TYPES else "image/png"
    image_bytes: bytes | None = None
    if isinstance(raw_url, str) and raw_url.startswith("data:"):
        header, separator, encoded = raw_url.partition(",")
        media_type = header[5:].split(";", 1)[0].lower()
        if media_type in _SUPPORTED_IMAGE_MIME_TYPES:
            mime_type = media_type
        if separator and ";base64" in header.lower():
            try:
                image_bytes = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                pass
    raw_detail = block.get("detail")
    image_url = block.get("image_url")
    if raw_detail is None and isinstance(image_url, Mapping):
        raw_detail = image_url.get("detail")
    try:
        detail = VisualDetail(str(raw_detail or "auto").lower())
    except ValueError:
        detail = VisualDetail.AUTO
    image_digest = (
        hashlib.sha256(image_bytes).hexdigest()
        if image_bytes is not None
        else _digest(
            {
                "block_type": block.get("type"),
                "url_digest": hashlib.sha256((raw_url or "").encode("utf-8")).hexdigest(),
            }
        )
    )
    return VisualMeasurementMetadata(
        image_digest=image_digest,
        mime_type=mime_type,  # type: ignore[arg-type]
        size_bytes=(len(image_bytes) if image_bytes is not None else None),
        detail=detail,
        strategy=VisualCostStrategy.UNMEASURED,
    )


def _without_visuals(
    payload: dict[str, object],
) -> tuple[dict[str, object] | None, tuple[Mapping[str, object], ...]]:
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return payload, ()
    content = data.get("content")
    if not isinstance(content, list):
        return payload, ()
    visuals = tuple(block for block in content if isinstance(block, Mapping) and str(block.get("type", "")).lower() in _VISUAL_BLOCK_TYPES)
    if not visuals:
        return payload, ()
    retained = [block for block in content if not (isinstance(block, Mapping) and str(block.get("type", "")).lower() in _VISUAL_BLOCK_TYPES)]
    if not retained:
        return None, visuals
    projected = dict(payload)
    projected["data"] = {**dict(data), "content": retained}
    return projected, visuals


def _bounded_estimate(
    material_bytes: int,
    *,
    error_allowance_ratio: float,
) -> TokenEstimate:
    projected = math.ceil(material_bytes / 4)
    return TokenEstimate.bounded(
        projected_tokens=projected,
        lower_bound_tokens=projected,
        safety_upper_bound_tokens=(projected + math.ceil(projected * error_allowance_ratio)),
    )


def _contribution(
    *,
    lane: ContextLane,
    material: object,
    generation: ContextWindowGeneration,
    estimator: ContextCheckpointEstimator,
) -> ContextContribution:
    source_identity = _digest(
        {
            "generation": generation.generation_id,
            "lane": lane,
            "material_digest": _digest(material),
        }
    )
    material_bytes = len(_canonical_json(material))
    return ContextContribution(
        contribution_id=_digest(
            {
                "adapter_revision": "checkpoint-replacement-v1",
                "lane": lane,
                "source_identity": source_identity,
            }
        ),
        source_identity_digest=source_identity,
        lane=lane,
        model_visible_bytes=material_bytes,
        token_estimate=_bounded_estimate(
            material_bytes,
            error_allowance_ratio=estimator.error_allowance_ratio,
        ),
    )


def _bootstrap_estimator() -> ContextCheckpointEstimator:
    """Bound checkpoint-owned text without inventing Provider framing facts."""

    return ContextCheckpointEstimator(
        error_allowance_ratio=0.25,
        provider_fixed_overhead_tokens=0,
        provider_per_message_overhead_tokens=0,
        provider_per_tool_overhead_tokens=0,
        fixed_message_count=0,
        tool_count=0,
    )


def _unknown_fixed_closure_contribution(
    generation: ContextWindowGeneration,
) -> ContextContribution:
    source_identity = _digest(
        {
            "generation": generation.generation_id,
            "unknown_fixed_closure": _UNKNOWN_FIXED_CLOSURE,
        }
    )
    return ContextContribution(
        contribution_id=_digest(
            {
                "adapter_revision": "checkpoint-bootstrap-v1",
                "lane": ContextLane.SYSTEM_PROMPT,
                "source_identity": source_identity,
            }
        ),
        source_identity_digest=source_identity,
        lane=ContextLane.SYSTEM_PROMPT,
        model_visible_bytes=0,
        token_estimate=TokenEstimate.unmeasured(item_count=1),
    )


def bootstrap_checkpoint_projection_snapshot(
    *,
    generation: ContextWindowGeneration,
    checkpoint_values: Mapping[str, object],
    model: ContextModelProjection | None = None,
    compaction: CompactionProjection | None = None,
    estimator: ContextCheckpointEstimator | None = None,
) -> ContextCheckpointProjectionSnapshot:
    """Build partial V2 authority for a legal pre-Provider checkpoint.

    Only persisted dynamic history is measurable at this boundary.  The
    Provider-shaped fixed closure stays explicitly unmeasured, so callers can
    publish a lower bound without treating missing Agent, Skill, Tool, MCP, or
    framing material as zero.  Final Provider capacity protection never uses
    this checkpoint adapter; the final shaped-request guard measures again.
    """

    resolved_estimator = estimator or _bootstrap_estimator()
    fixed_closure = _unknown_fixed_closure_contribution(generation)
    seed = ContextCheckpointProjectionSnapshot(
        generation=generation,
        model=model
        or ContextModelProjection(
            identity_digest=_UNKNOWN_CONTEXT_MODEL_IDENTITY,
            context_window_tokens=None,
        ),
        measurement=FinalRequestMeasurement(
            request_fingerprint=_digest(
                {
                    "adapter_revision": "checkpoint-bootstrap-v1",
                    "fixed_closure": fixed_closure.to_safe_mapping(),
                    "generation": generation.generation_id,
                }
            ),
            adapter_revision="checkpoint-bootstrap-v1",
            contributions=(fixed_closure,),
        ),
        compaction=compaction
        or CompactionProjection(
            enabled=False,
            reached=False,
        ),
        estimator=resolved_estimator,
    )
    return remeasure_replacement_checkpoint(
        seed,
        generation=generation,
        checkpoint_values=checkpoint_values,
    )


def remeasure_replacement_checkpoint(
    source: ContextCheckpointProjectionSnapshot,
    *,
    generation: ContextWindowGeneration,
    checkpoint_values: Mapping[str, object],
) -> ContextCheckpointProjectionSnapshot:
    """Remeasure mutable history while retaining frozen fixed contributions."""

    estimator = source.estimator
    contributions = [item for item in source.measurement.contributions if item.lane not in _DYNAMIC_LANES]
    message_payloads: list[dict[str, object]] = []
    visual_contributions: list[ContextContribution] = []
    for message_index, payload in enumerate(_message_payloads(checkpoint_values.get("messages"))):
        retained, visuals = _without_visuals(payload)
        if retained is not None:
            message_payloads.append(retained)
        for visual_index, block in enumerate(visuals):
            metadata = _visual_metadata(block)
            source_identity = _digest(
                {
                    "generation": generation.generation_id,
                    "image_digest": metadata.image_digest,
                    "message_index": message_index,
                    "visual_index": visual_index,
                }
            )
            visual_contributions.append(
                ContextContribution(
                    contribution_id=_digest(
                        {
                            "adapter_revision": "checkpoint-replacement-v1",
                            "lane": ContextLane.VISUAL_MEDIA,
                            "source_identity": source_identity,
                        }
                    ),
                    source_identity_digest=source_identity,
                    lane=ContextLane.VISUAL_MEDIA,
                    model_visible_bytes=0,
                    token_estimate=TokenEstimate.unmeasured(item_count=1),
                    visual=metadata,
                )
            )
    if message_payloads:
        contributions.append(
            _contribution(
                lane=ContextLane.CONVERSATION,
                material=message_payloads,
                generation=generation,
                estimator=estimator,
            )
        )
    summary = checkpoint_values.get("summary_text")
    if isinstance(summary, str) and summary:
        contributions.append(
            _contribution(
                lane=ContextLane.SUMMARIZED_CONVERSATION,
                material={"summary_text": summary},
                generation=generation,
                estimator=estimator,
            )
        )
    contributions.extend(visual_contributions)
    message_count = estimator.fixed_message_count + len(message_payloads)
    if isinstance(summary, str) and summary:
        message_count += 1
    overhead_tokens = estimator.provider_fixed_overhead_tokens + message_count * estimator.provider_per_message_overhead_tokens + estimator.tool_count * estimator.provider_per_tool_overhead_tokens
    overhead_source = _digest(
        {
            "fixed_message_count": estimator.fixed_message_count,
            "generation": generation.generation_id,
            "message_count": message_count,
            "tool_count": estimator.tool_count,
        }
    )
    if overhead_tokens:
        contributions.append(
            ContextContribution(
                contribution_id=_digest(
                    {
                        "adapter_revision": "checkpoint-replacement-v1",
                        "lane": ContextLane.PROVIDER_OVERHEAD,
                        "source_identity": overhead_source,
                    }
                ),
                source_identity_digest=overhead_source,
                lane=ContextLane.PROVIDER_OVERHEAD,
                model_visible_bytes=0,
                token_estimate=TokenEstimate.bounded(
                    projected_tokens=overhead_tokens,
                    lower_bound_tokens=overhead_tokens,
                    safety_upper_bound_tokens=(overhead_tokens + 256),
                ),
            )
        )
    contributions.sort(key=lambda item: (tuple(ContextLane).index(item.lane), item.contribution_id))
    measurement = FinalRequestMeasurement(
        request_fingerprint=_digest(
            {
                "adapter_revision": "checkpoint-replacement-v1",
                "contributions": [item.to_safe_mapping() for item in contributions],
                "generation": generation.generation_id,
            }
        ),
        adapter_revision="checkpoint-replacement-v1",
        contributions=tuple(contributions),
    )
    return ContextCheckpointProjectionSnapshot(
        generation=generation,
        model=source.model,
        measurement=measurement,
        compaction=CompactionProjection(
            enabled=source.compaction.enabled,
            threshold_tokens=source.compaction.threshold_tokens,
            reached=bool(source.compaction.enabled and source.compaction.threshold_tokens is not None and measurement.projected_tokens >= source.compaction.threshold_tokens),
            authority=source.compaction.authority,
            blocked_reason=source.compaction.blocked_reason,
        ),
        estimator=estimator,
    )


def compaction_checkpoint_receipt(
    source: ContextCheckpointProjectionSnapshot,
    *,
    source_checkpoint_id: str,
    checkpoint_values: Mapping[str, object],
    result_generation: ContextWindowGeneration,
    phase: ProjectionPhase = ProjectionPhase.IDLE,
    origin_run_id: str | None = None,
) -> ContextCompactionCheckpointReceipt:
    result = remeasure_replacement_checkpoint(
        source,
        generation=result_generation,
        checkpoint_values=checkpoint_values,
    )
    summary = checkpoint_values.get("summary_text")
    summary_digest = hashlib.sha256((summary if isinstance(summary, str) else "").encode("utf-8")).hexdigest()
    receipt_id = _digest(
        {
            "result_generation": result_generation.generation_id,
            "source_checkpoint_id": source_checkpoint_id,
            "summary_digest": summary_digest,
        }
    )
    return ContextCompactionCheckpointReceipt(
        receipt_id=receipt_id,
        source_checkpoint_id=source_checkpoint_id,
        source_generation=source.generation,
        result_generation=result_generation,
        source_tokens=source.measurement.projected_tokens,
        result_tokens=result.measurement.projected_tokens,
        summary_digest=summary_digest,
        projection_snapshot=result,
        phase=phase,
        origin_run_id=origin_run_id,
    )


def source_from_checkpoint_snapshot(
    snapshot: ContextCheckpointProjectionSnapshot,
    *,
    subject: ContextSubject,
    checkpoint_id: str,
    phase: ProjectionPhase,
) -> ContextProjectionSource:
    return ContextProjectionSource(
        subject=subject,
        phase=phase,
        generation=snapshot.generation,
        checkpoint_id=checkpoint_id,
        model=snapshot.model,
        measurement=snapshot.measurement,
        current_provider_call_id=None,
        compaction=snapshot.compaction,
        freshness=ProjectionFreshness.CURRENT,
    )


def checkpoint_snapshot_from_evidence(
    records: Sequence[ContextEvidenceRecord],
    *,
    subject: ContextSubjectRef,
    checkpoint_id: str,
    estimator: ContextCheckpointEstimator,
) -> ContextCheckpointProjectionSnapshot:
    """Recover one linked request snapshot from immutable source Evidence."""

    matching = [record for record in records if record.subject == subject]
    linked = next(
        (
            CheckpointLinkedV1.model_validate_json(
                json.dumps(record.payload, separators=(",", ":")),
            )
            for record in reversed(matching)
            if record.event_type == "checkpoint.linked.v1" and record.checkpoint_id == checkpoint_id
        ),
        None,
    )
    if linked is None:
        raise LookupError("Checkpoint has no linked Context Evidence")
    prepared = next(
        (
            RequestPreparedV1.model_validate_json(
                json.dumps(record.payload, separators=(",", ":")),
            )
            for record in reversed(matching)
            if record.event_type == "request.prepared.v1" and record.provider_call_id == linked.provider_call_id
        ),
        None,
    )
    if prepared is None:
        raise LookupError("Checkpoint Context Evidence has no prepared request")
    opened = next(
        (
            WindowOpenedV1.model_validate_json(
                json.dumps(record.payload, separators=(",", ":")),
            )
            for record in reversed(matching)
            if record.event_type == "context.window.opened.v1" and record.context_window_generation == uuid.UUID(prepared.provider_call.generation.generation_id)
        ),
        None,
    )
    if opened is None:
        raise LookupError("Checkpoint Context Evidence has no model window")
    return ContextCheckpointProjectionSnapshot(
        generation=prepared.provider_call.generation,
        model=ContextModelProjection(
            identity_digest=opened.model_identity_digest,
            context_window_tokens=opened.context_window_tokens,
        ),
        measurement=prepared.measurement,
        compaction=CompactionProjection(
            enabled=opened.compaction_enabled,
            threshold_tokens=opened.compaction_threshold_tokens,
            reached=bool(opened.compaction_enabled and opened.compaction_threshold_tokens is not None and prepared.measurement.projected_tokens >= opened.compaction_threshold_tokens),
            authority=(CompactionAuthority(opened.compaction_authority) if opened.compaction_authority is not None else None),
        ),
        estimator=estimator,
    )


def branch_projection_source(
    source: ContextCheckpointProjectionSnapshot,
    *,
    target_thread_id: str,
    result_checkpoint_id: str,
    generation: ContextWindowGeneration,
) -> ContextProjectionSource:
    target_snapshot = source.without_provider_response_authority().model_copy(
        update={"generation": generation},
    )
    return source_from_checkpoint_snapshot(
        target_snapshot,
        subject=ContextSubject.lead_thread(thread_id=target_thread_id),
        checkpoint_id=result_checkpoint_id,
        phase=ProjectionPhase.IDLE,
    )


def idle_compaction_projection_source(
    receipt: ContextCompactionCheckpointReceipt,
    *,
    thread_id: str,
    result_checkpoint_id: str,
) -> ContextProjectionSource:
    snapshot = receipt.projection_snapshot
    compaction = snapshot.compaction
    if compaction.enabled and receipt.phase is ProjectionPhase.IDLE:
        compaction = compaction.model_copy(
            update={"authority": CompactionAuthority.IDLE_HISTORY},
        )
    snapshot = snapshot.model_copy(update={"compaction": compaction})
    return source_from_checkpoint_snapshot(
        snapshot,
        subject=ContextSubject.lead_thread(thread_id=thread_id),
        checkpoint_id=result_checkpoint_id,
        phase=receipt.phase,
    )


def idle_checkpoint_projection_source(
    snapshot: ContextCheckpointProjectionSnapshot,
    *,
    thread_id: str,
    checkpoint_id: str,
) -> ContextProjectionSource:
    """Project a checkpoint snapshot under idle Thread authority."""

    compaction = snapshot.compaction
    if compaction.enabled:
        compaction = compaction.model_copy(
            update={"authority": CompactionAuthority.IDLE_HISTORY},
        )
    return source_from_checkpoint_snapshot(
        snapshot.model_copy(update={"compaction": compaction}),
        subject=ContextSubject.lead_thread(thread_id=thread_id),
        checkpoint_id=checkpoint_id,
        phase=ProjectionPhase.IDLE,
    )


__all__ = [
    "bootstrap_checkpoint_projection_snapshot",
    "branch_projection_source",
    "checkpoint_snapshot_from_evidence",
    "compaction_checkpoint_receipt",
    "deterministic_generation_id",
    "history_digest",
    "idle_checkpoint_projection_source",
    "idle_compaction_projection_source",
    "remeasure_replacement_checkpoint",
    "source_from_checkpoint_snapshot",
]
