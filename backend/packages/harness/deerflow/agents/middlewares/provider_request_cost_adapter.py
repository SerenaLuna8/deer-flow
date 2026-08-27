"""Positive lane attribution for the final LangChain ``ModelRequest``.

The Adapter works at the innermost provider seam.  It hashes request material,
returns only content-free measurement facts, and never persists a prompt,
message, tool schema, image URL, or image bytes.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, Self, runtime_checkable

from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import BaseMessage, SystemMessage
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool

from deerflow.models.provider_wire import (
    SUPPORTED_PROVIDER_WIRE_ADAPTERS,
    provider_visible_message_payload,
    provider_visible_messages_payload,
)
from deerflow.runtime.context_evidence import (
    STABLE_CONTEXT_LANES,
    ContextContribution,
    ContextLane,
    FinalRequestMeasurement,
    TokenEstimate,
    VisualCostStrategy,
    VisualDetail,
    VisualMeasurementMetadata,
)

MODEL_REQUEST_COST_ADAPTER_REVISION = "provider-wire-request-cost-v5"
_SERIALIZATION_FRAMING_UTF8_BYTES = 1_024
_MESSAGE_LANE_PROVENANCE_ATTRIBUTE = "_deerflow_message_lane_provenance"
_VISUAL_BLOCK_TYPES = frozenset({"image", "image_url", "input_image"})
_SUPPORTED_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})


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


def _tool_payload(tool: BaseTool | dict[str, Any]) -> dict[str, Any]:
    converted = convert_to_openai_tool(tool)
    if not isinstance(converted, dict):
        raise TypeError("provider tool conversion returned a non-mapping")
    return converted


def _tool_name(tool: BaseTool | dict[str, Any]) -> str:
    if isinstance(tool, BaseTool):
        return tool.name
    function = tool.get("function")
    if isinstance(function, Mapping) and isinstance(function.get("name"), str):
        return function["name"]
    name = tool.get("name")
    if isinstance(name, str):
        return name
    raise ValueError("provider tool has no stable name")


def _canonical_tools(
    tools: Sequence[BaseTool | dict[str, Any]],
) -> tuple[BaseTool | dict[str, Any], ...]:
    by_name: dict[str, BaseTool | dict[str, Any]] = {}
    for tool in tools:
        by_name[_tool_name(tool)] = tool
    return tuple(by_name.values())


def _is_frozen_mcp_tool(tool: BaseTool | dict[str, Any]) -> bool:
    if not isinstance(tool, BaseTool):
        return False
    # Lazy import preserves the tools -> task_tool -> subagent executor import
    # boundary while still using the single metadata predicate.
    from deerflow.tools.mcp_metadata import is_mcp_tool

    return is_mcp_tool(tool)


class ProviderRequestFragmentKind(StrEnum):
    SYSTEM_PROMPT = "system_prompt"
    MESSAGE = "message"
    TOOL_DEFINITION = "tool_definition"


@dataclass(frozen=True, slots=True, repr=False)
class ProviderRequestFragment:
    """One exact, positively present final-request fragment.

    ``material`` is intentionally process-local.  The Adapter hashes and sizes
    it, while Context Evidence receives only ``ContextContribution`` facts.
    """

    kind: ProviderRequestFragmentKind
    source_name: str
    index: int
    material: object
    model_visible_bytes: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_name, str) or not self.source_name:
            raise ValueError("provider request fragment source_name is required")
        if type(self.index) is not int or self.index < 0:
            raise ValueError("provider request fragment index must be a non-negative integer")
        if self.model_visible_bytes is not None and (type(self.model_visible_bytes) is not int or self.model_visible_bytes < 0):
            raise ValueError("provider request fragment model_visible_bytes must be a non-negative integer")

    def __repr__(self) -> str:
        measured = self.model_visible_bytes
        if measured is None:
            measured = len(_canonical_json(self.material))
        return f"ProviderRequestFragment(kind={self.kind.value!r}, source_name={self.source_name!r}, index={self.index}, model_visible_bytes={measured})"


@dataclass(frozen=True, slots=True, repr=False)
class SystemPromptLaneSpan:
    """One render-time, process-local source interval in the Lead prompt."""

    source_name: str
    lane: ContextLane
    start: int
    end: int

    def __post_init__(self) -> None:
        if not isinstance(self.source_name, str) or not self.source_name:
            raise ValueError("system-prompt span source_name is required")
        if self.lane not in STABLE_CONTEXT_LANES:
            raise ValueError("system-prompt span lane is not a stable Context lane")
        if type(self.start) is not int or type(self.end) is not int or self.start < 0 or self.end <= self.start:
            raise ValueError("system-prompt span must be a non-empty character interval")

    def __repr__(self) -> str:
        return f"SystemPromptLaneSpan(source_name={self.source_name!r}, lane={self.lane.value!r}, length={self.end - self.start})"


@dataclass(frozen=True, slots=True, repr=False)
class SystemPromptProvenance:
    """Exact Lead prompt plus content-free render spans, held only in memory.

    The prompt body is deliberately excluded from ``repr`` and never enters
    Context Evidence.  A final request can consume these spans only while the
    exact rendered prompt remains one unambiguous block in the final system
    material; any drift falls back to the ``system_prompt`` lane instead of
    guessing.
    """

    system_prompt: str
    spans: tuple[SystemPromptLaneSpan, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.system_prompt, str) or not self.system_prompt:
            raise ValueError("system-prompt provenance requires exact rendered text")
        if type(self.spans) is not tuple:
            raise TypeError("system-prompt provenance spans must be a tuple")
        previous_end = 0
        seen_sources: set[str] = set()
        for span in self.spans:
            if not isinstance(span, SystemPromptLaneSpan):
                raise TypeError("system-prompt provenance contains an invalid span")
            if span.end > len(self.system_prompt) or span.start < previous_end:
                raise ValueError("system-prompt provenance spans overlap or exceed the rendered prompt")
            if span.source_name in seen_sources:
                raise ValueError("system-prompt provenance source names must be unique")
            previous_end = span.end
            seen_sources.add(span.source_name)

    def __repr__(self) -> str:
        return f"SystemPromptProvenance(utf8_bytes={len(self.system_prompt.encode('utf-8'))}, sources={tuple(span.source_name for span in self.spans)!r})"

    def spans_for_final_content(self, content: object) -> tuple[SystemPromptLaneSpan, ...]:
        """Translate spans only when the exact frozen prompt occurs once."""

        if not isinstance(content, str):
            return ()
        prompt_start = content.find(self.system_prompt)
        if prompt_start < 0 or content.find(self.system_prompt, prompt_start + 1) >= 0:
            return ()
        if prompt_start == 0:
            return self.spans
        return tuple(
            SystemPromptLaneSpan(
                source_name=span.source_name,
                lane=span.lane,
                start=prompt_start + span.start,
                end=prompt_start + span.end,
            )
            for span in self.spans
        )


@dataclass(frozen=True, slots=True, repr=False)
class MessageLaneProvenance:
    """Exact, process-local lane spans for one model-visible message.

    The message body stays in the ephemeral request object. This companion is
    attached as a private Python attribute, which LangChain does not serialize
    into message dictionaries or Provider payloads.
    """

    exact_content: str
    spans: tuple[SystemPromptLaneSpan, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.exact_content, str) or not self.exact_content:
            raise ValueError("message provenance requires exact rendered text")
        if type(self.spans) is not tuple:
            raise TypeError("message provenance spans must be a tuple")
        previous_end = 0
        seen_sources: set[str] = set()
        for span in self.spans:
            if not isinstance(span, SystemPromptLaneSpan):
                raise TypeError("message provenance contains an invalid span")
            if span.end > len(self.exact_content) or span.start < previous_end:
                raise ValueError("message provenance spans overlap or exceed the rendered message")
            if span.source_name in seen_sources:
                raise ValueError("message provenance source names must be unique")
            previous_end = span.end
            seen_sources.add(span.source_name)

    def __repr__(self) -> str:
        return f"MessageLaneProvenance(utf8_bytes={len(self.exact_content.encode('utf-8'))}, sources={tuple(span.source_name for span in self.spans)!r})"

    def spans_for_final_content(
        self,
        content: object,
    ) -> tuple[SystemPromptLaneSpan, ...]:
        if content != self.exact_content:
            return ()
        return self.spans


def attach_message_lane_provenance(
    message: BaseMessage,
    provenance: MessageLaneProvenance,
) -> BaseMessage:
    """Attach non-serializable attribution to one ephemeral request message."""

    if not isinstance(message, BaseMessage):
        raise TypeError("message lane provenance requires a BaseMessage")
    if not isinstance(provenance, MessageLaneProvenance):
        raise TypeError("message lane provenance has an invalid type")
    object.__setattr__(
        message,
        _MESSAGE_LANE_PROVENANCE_ATTRIBUTE,
        provenance,
    )
    return message


def message_lane_provenance(
    message: BaseMessage,
) -> MessageLaneProvenance | None:
    provenance = getattr(message, _MESSAGE_LANE_PROVENANCE_ATTRIBUTE, None)
    return provenance if isinstance(provenance, MessageLaneProvenance) else None


@runtime_checkable
class ProviderRequestLaneResolver(Protocol):
    """Assembly-owned provenance hook for known Agent/Skill/tool sources.

    Resolvers classify an already-present fragment; they cannot add material or
    subtract a fallback estimate.  This lets assembly register Agent, Skill,
    MCP, and Sub-Agent provenance without this Adapter guessing from an adapter
    name, compatibility list, or mutable catalog state.
    """

    def resolve_lane(
        self,
        request: ModelRequest,
        fragment: ProviderRequestFragment,
        /,
    ) -> ContextLane | None: ...


def _is_visual_block(value: object) -> bool:
    return isinstance(value, Mapping) and str(value.get("type", "")).lower() in _VISUAL_BLOCK_TYPES


def _message_fragment_without_visuals(
    message: BaseMessage,
    *,
    provider_adapter: str,
) -> tuple[dict[str, object] | None, tuple[Mapping[str, object], ...]]:
    payload = provider_visible_message_payload(
        message,
        provider_adapter=provider_adapter,
    )
    content = payload.get("content")
    if not isinstance(content, list):
        return payload, ()
    visual_blocks = tuple(block for block in content if _is_visual_block(block))
    if not visual_blocks:
        return payload, ()
    retained = [block for block in content if not _is_visual_block(block)]
    if not retained:
        return None, visual_blocks
    projected = deepcopy(payload)
    projected["content"] = retained
    return projected, visual_blocks


def _payload_without_content(
    payload: Mapping[str, object],
) -> dict[str, object]:
    projected = dict(payload)
    projected["content"] = ""
    return projected


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


def _visual_detail(block: Mapping[str, object]) -> VisualDetail:
    raw_image_url = block.get("image_url")
    raw = block.get("detail")
    if raw is None and isinstance(raw_image_url, Mapping):
        raw = raw_image_url.get("detail")
    try:
        return VisualDetail(str(raw or "auto").lower())
    except ValueError:
        return VisualDetail.AUTO


def _visual_bytes_and_mime(
    block: Mapping[str, object],
) -> tuple[bytes | None, str]:
    raw_url = _visual_url(block)
    declared_mime = block.get("mime_type") or block.get("media_type")
    source = block.get("source")
    if declared_mime is None and isinstance(source, Mapping):
        declared_mime = source.get("media_type")
    mime_type = declared_mime if declared_mime in _SUPPORTED_IMAGE_MIME_TYPES else None
    if isinstance(raw_url, str) and raw_url.startswith("data:"):
        header, separator, encoded = raw_url.partition(",")
        media_type = header[5:].split(";", 1)[0].lower()
        if media_type in _SUPPORTED_IMAGE_MIME_TYPES:
            mime_type = media_type
        if separator and ";base64" in header.lower():
            try:
                return base64.b64decode(encoded, validate=True), mime_type or "image/png"
            except (binascii.Error, ValueError):
                pass
    # ActWeave's admitted image path always carries a supported MIME.  The
    # fallback is metadata-only and remains unmeasured; it does not assert a
    # Provider visual algorithm or Token cost.
    return None, mime_type or "image/png"


def _visual_metadata(
    block: Mapping[str, object],
) -> VisualMeasurementMetadata:
    image_bytes, mime_type = _visual_bytes_and_mime(block)
    raw_url = _visual_url(block)
    digest_material: object = image_bytes if image_bytes is not None else {"block_type": block.get("type"), "url_digest": hashlib.sha256((raw_url or "").encode("utf-8")).hexdigest()}
    if isinstance(digest_material, bytes):
        image_digest = hashlib.sha256(digest_material).hexdigest()
    else:
        image_digest = _digest(digest_material)
    return VisualMeasurementMetadata(
        image_digest=image_digest,
        mime_type=mime_type,
        size_bytes=len(image_bytes) if image_bytes is not None else None,
        detail=_visual_detail(block),
        strategy=VisualCostStrategy.UNMEASURED,
    )


class ProviderModelRequestCostAdapter:
    """Concrete ``FinalShapedRequestCostAdapter[ModelRequest]``."""

    def __init__(
        self,
        *,
        provider_adapter: str | None,
        error_allowance_ratio: float,
        provider_fixed_overhead_tokens: int,
        provider_per_message_overhead_tokens: int,
        provider_per_tool_overhead_tokens: int,
        lane_resolvers: Sequence[ProviderRequestLaneResolver] = (),
        system_prompt_provenance: SystemPromptProvenance | None = None,
        mcp_dynamic_tools: Sequence[BaseTool | dict[str, Any]] = (),
        adapter_revision: str = MODEL_REQUEST_COST_ADAPTER_REVISION,
    ) -> None:
        if provider_adapter is not None and provider_adapter not in SUPPORTED_PROVIDER_WIRE_ADAPTERS:
            raise ValueError("provider adapter has no message projection")
        if not 0 <= error_allowance_ratio <= 1:
            raise ValueError("error allowance ratio is outside 0..1")
        if any(
            type(value) is not int or value < 0
            for value in (
                provider_fixed_overhead_tokens,
                provider_per_message_overhead_tokens,
                provider_per_tool_overhead_tokens,
            )
        ):
            raise ValueError("provider overhead declarations must be non-negative integers")
        self._error_allowance_ratio = error_allowance_ratio
        self._provider_adapter = provider_adapter
        self._provider_fixed_overhead_tokens = provider_fixed_overhead_tokens
        self._provider_per_message_overhead_tokens = provider_per_message_overhead_tokens
        self._provider_per_tool_overhead_tokens = provider_per_tool_overhead_tokens
        self._lane_resolvers = tuple(lane_resolvers)
        if system_prompt_provenance is not None and not isinstance(system_prompt_provenance, SystemPromptProvenance):
            raise TypeError("system_prompt_provenance must be a SystemPromptProvenance")
        self._system_prompt_provenance = system_prompt_provenance
        self._mcp_dynamic_tool_schema_digests = frozenset(_digest(_tool_payload(tool)) for tool in _canonical_tools(tuple(mcp_dynamic_tools)) if _is_frozen_mcp_tool(tool))
        self._adapter_revision = adapter_revision

    @classmethod
    def from_profile(
        cls,
        profile: object,
        *,
        lane_resolvers: Sequence[ProviderRequestLaneResolver] = (),
        system_prompt_provenance: SystemPromptProvenance | None = None,
        mcp_dynamic_tools: Sequence[BaseTool | dict[str, Any]] = (),
    ) -> Self:
        return cls(
            provider_adapter=getattr(profile, "provider_adapter", None),
            error_allowance_ratio=float(getattr(profile, "error_allowance_ratio")),
            provider_fixed_overhead_tokens=int(getattr(profile, "provider_fixed_overhead_tokens")),
            provider_per_message_overhead_tokens=int(getattr(profile, "provider_per_message_overhead_tokens")),
            provider_per_tool_overhead_tokens=int(getattr(profile, "provider_per_tool_overhead_tokens")),
            lane_resolvers=lane_resolvers,
            system_prompt_provenance=system_prompt_provenance,
            mcp_dynamic_tools=mcp_dynamic_tools,
        )

    def _resolved_lane(
        self,
        request: ModelRequest,
        fragment: ProviderRequestFragment,
        *,
        fallback: ContextLane,
    ) -> ContextLane:
        for resolver in self._lane_resolvers:
            lane = resolver.resolve_lane(request, fragment)
            if lane is not None:
                if lane not in STABLE_CONTEXT_LANES:
                    raise ValueError("lane resolver returned a non-v1 Context lane")
                return lane
        return fallback

    @staticmethod
    def _default_message_lane(
        request: ModelRequest,
        message: BaseMessage,
    ) -> ContextLane:
        # Sub-Agent assembly injects its frozen system material as the first
        # state message instead of through ``request.system_message``.  Treat
        # the concrete message role as positive provenance so those bytes are
        # never attributed to conversation history.
        if isinstance(message, SystemMessage):
            return ContextLane.SYSTEM_PROMPT
        del request
        return ContextLane.CONVERSATION

    def _material_estimate(self, material_bytes: int) -> TokenEstimate:
        projected = math.ceil(material_bytes / 4)
        return TokenEstimate.bounded(
            projected_tokens=projected,
            lower_bound_tokens=projected,
            safety_upper_bound_tokens=(projected + math.ceil(projected * self._error_allowance_ratio)),
        )

    def _system_prompt_fragments(
        self,
        request: ModelRequest,
        system_message: BaseMessage,
    ) -> tuple[tuple[ContextLane, ProviderRequestFragment], ...]:
        payload = provider_visible_message_payload(
            system_message,
            provider_adapter=self._provider_adapter,
        )
        provenance = self._system_prompt_provenance
        spans = provenance.spans_for_final_content(system_message.content) if provenance is not None else ()
        if not spans or not isinstance(system_message.content, str):
            fragment = ProviderRequestFragment(
                kind=ProviderRequestFragmentKind.SYSTEM_PROMPT,
                source_name="system_prompt",
                index=0,
                material=payload,
            )
            return (
                (
                    self._resolved_lane(
                        request,
                        fragment,
                        fallback=ContextLane.SYSTEM_PROMPT,
                    ),
                    fragment,
                ),
            )

        content = system_message.content
        content_bytes = len(content.encode("utf-8"))
        payload_bytes = len(_canonical_json(payload))
        if payload_bytes < content_bytes:
            raise ValueError("serialized system message is smaller than its visible content")

        fragments: list[tuple[ContextLane, ProviderRequestFragment]] = []
        cursor = 0
        residual_segments: list[str] = []
        known_bytes = 0
        for index, span in enumerate(spans):
            if cursor < span.start:
                residual_segments.append(content[cursor : span.start])
            material = content[span.start : span.end]
            material_bytes = len(material.encode("utf-8"))
            known_bytes += material_bytes
            fragment = ProviderRequestFragment(
                kind=ProviderRequestFragmentKind.SYSTEM_PROMPT,
                source_name=span.source_name,
                index=index,
                material=material,
                model_visible_bytes=material_bytes,
            )
            fragments.append(
                (
                    self._resolved_lane(
                        request,
                        fragment,
                        fallback=span.lane,
                    ),
                    fragment,
                )
            )
            cursor = span.end
        if cursor < len(content):
            residual_segments.append(content[cursor:])

        residual_bytes = payload_bytes - known_bytes
        residual_fragment = ProviderRequestFragment(
            kind=ProviderRequestFragmentKind.SYSTEM_PROMPT,
            source_name="system_prompt_remainder",
            index=len(spans),
            material={
                "content_segments": residual_segments,
                "message_envelope_digest": _digest(
                    _payload_without_content(payload),
                ),
            },
            model_visible_bytes=residual_bytes,
        )
        fragments.append(
            (
                self._resolved_lane(
                    request,
                    residual_fragment,
                    fallback=ContextLane.SYSTEM_PROMPT,
                ),
                residual_fragment,
            )
        )
        return tuple(fragments)

    def _message_provenance_fragments(
        self,
        request: ModelRequest,
        message: BaseMessage,
        payload: dict[str, object],
        provenance: MessageLaneProvenance,
        *,
        message_index: int,
    ) -> tuple[tuple[ContextLane, ProviderRequestFragment], ...]:
        spans = provenance.spans_for_final_content(message.content)
        if not spans or not isinstance(message.content, str):
            return ()

        content = message.content
        content_bytes = len(content.encode("utf-8"))
        payload_bytes = len(_canonical_json(payload))
        if payload_bytes < content_bytes:
            raise ValueError("serialized message is smaller than its visible content")

        fragments: list[tuple[ContextLane, ProviderRequestFragment]] = []
        residual_segments: list[str] = []
        cursor = 0
        known_bytes = 0
        for span_index, span in enumerate(spans):
            if cursor < span.start:
                residual_segments.append(content[cursor : span.start])
            material = content[span.start : span.end]
            material_bytes = len(material.encode("utf-8"))
            known_bytes += material_bytes
            fragment = ProviderRequestFragment(
                kind=ProviderRequestFragmentKind.MESSAGE,
                source_name=span.source_name,
                index=span_index,
                material=material,
                model_visible_bytes=material_bytes,
            )
            fragments.append(
                (
                    self._resolved_lane(
                        request,
                        fragment,
                        fallback=span.lane,
                    ),
                    fragment,
                )
            )
            cursor = span.end
        if cursor < len(content):
            residual_segments.append(content[cursor:])

        residual_bytes = payload_bytes - known_bytes
        residual_fragment = ProviderRequestFragment(
            kind=ProviderRequestFragmentKind.MESSAGE,
            source_name=f"{message.id or f'message-{message_index}'}:remainder",
            index=len(spans),
            material={
                "content_segments": residual_segments,
                "message_envelope_digest": _digest(
                    _payload_without_content(payload),
                ),
            },
            model_visible_bytes=residual_bytes,
        )
        fragments.append(
            (
                self._resolved_lane(
                    request,
                    residual_fragment,
                    fallback=ContextLane.CONVERSATION,
                ),
                residual_fragment,
            )
        )
        return tuple(fragments)

    def measure_final_request(
        self,
        request: ModelRequest,
        /,
    ) -> FinalRequestMeasurement:
        messages = list(request.messages)
        system_message = request.system_message
        tools = _canonical_tools(tuple(request.tools or ()))
        all_messages = [system_message, *messages] if system_message is not None else messages
        raw_messages = list(
            provider_visible_messages_payload(
                all_messages,
                provider_adapter=self._provider_adapter,
            )
        )
        raw_tools = [_tool_payload(tool) for tool in tools]
        request_material = _canonical_json({"messages": raw_messages, "tools": raw_tools})
        request_fingerprint = hashlib.sha256(request_material).hexdigest()

        lane_fragments: dict[ContextLane, list[ProviderRequestFragment]] = {}
        visual_contributions: list[ContextContribution] = []
        if system_message is not None:
            for lane, fragment in self._system_prompt_fragments(request, system_message):
                lane_fragments.setdefault(lane, []).append(fragment)

        for index, message in enumerate(messages):
            projected_payload, visual_blocks = _message_fragment_without_visuals(
                message,
                provider_adapter=self._provider_adapter,
            )
            if projected_payload is not None:
                if isinstance(message, SystemMessage):
                    attributed_fragments = self._system_prompt_fragments(
                        request,
                        message,
                    )
                else:
                    provenance = message_lane_provenance(message)
                    attributed_fragments = (
                        self._message_provenance_fragments(
                            request,
                            message,
                            projected_payload,
                            provenance,
                            message_index=index,
                        )
                        if provenance is not None
                        else ()
                    )
                if attributed_fragments:
                    for lane, fragment in attributed_fragments:
                        lane_fragments.setdefault(lane, []).append(fragment)
                else:
                    fragment = ProviderRequestFragment(
                        kind=ProviderRequestFragmentKind.MESSAGE,
                        source_name=str(message.id or f"message-{index}"),
                        index=index,
                        material=projected_payload,
                    )
                    lane = self._resolved_lane(
                        request,
                        fragment,
                        fallback=self._default_message_lane(request, message),
                    )
                    lane_fragments.setdefault(lane, []).append(fragment)
            for visual_index, block in enumerate(visual_blocks):
                metadata = _visual_metadata(block)
                source_identity = _digest(
                    {
                        "kind": "visual",
                        "message_index": index,
                        "visual_index": visual_index,
                        "image_digest": metadata.image_digest,
                    }
                )
                visual_contributions.append(
                    ContextContribution(
                        contribution_id=_digest(
                            {
                                "adapter_revision": self._adapter_revision,
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

        for index, (tool, payload) in enumerate(zip(tools, raw_tools, strict=True)):
            fragment = ProviderRequestFragment(
                kind=ProviderRequestFragmentKind.TOOL_DEFINITION,
                source_name=_tool_name(tool),
                index=index,
                material=payload,
            )
            lane = self._resolved_lane(
                request,
                fragment,
                fallback=(ContextLane.MCP_DYNAMIC_TOOLS if _digest(payload) in self._mcp_dynamic_tool_schema_digests else ContextLane.TOOL_DEFINITIONS),
            )
            lane_fragments.setdefault(lane, []).append(fragment)

        contributions: list[ContextContribution] = []
        for lane in STABLE_CONTEXT_LANES:
            fragments = lane_fragments.get(lane, ())
            if not fragments:
                continue
            identities = [
                {
                    "kind": fragment.kind,
                    "source_name": fragment.source_name,
                    "index": fragment.index,
                    "material_digest": _digest(fragment.material),
                }
                for fragment in fragments
            ]
            source_identity = _digest({"lane": lane, "fragment_identities": identities})
            material_bytes = sum(fragment.model_visible_bytes if fragment.model_visible_bytes is not None else len(_canonical_json(fragment.material)) for fragment in fragments)
            contributions.append(
                ContextContribution(
                    contribution_id=_digest(
                        {
                            "adapter_revision": self._adapter_revision,
                            "lane": lane,
                            "source_identity": source_identity,
                        }
                    ),
                    source_identity_digest=source_identity,
                    lane=lane,
                    model_visible_bytes=material_bytes,
                    token_estimate=self._material_estimate(material_bytes),
                )
            )

        contributions.extend(visual_contributions)
        provider_overhead_tokens = self._provider_fixed_overhead_tokens + (len(raw_messages) * self._provider_per_message_overhead_tokens) + (len(tools) * self._provider_per_tool_overhead_tokens)
        overhead_source_identity = _digest(
            {
                "kind": "declared_provider_framing",
                "message_count": len(raw_messages),
                "tool_count": len(tools),
            }
        )
        framing_upper_allowance = math.ceil(_SERIALIZATION_FRAMING_UTF8_BYTES / 4)
        contributions.append(
            ContextContribution(
                contribution_id=_digest(
                    {
                        "adapter_revision": self._adapter_revision,
                        "lane": ContextLane.PROVIDER_OVERHEAD,
                        "source_identity": overhead_source_identity,
                    }
                ),
                source_identity_digest=overhead_source_identity,
                lane=ContextLane.PROVIDER_OVERHEAD,
                model_visible_bytes=0,
                token_estimate=TokenEstimate.bounded(
                    projected_tokens=provider_overhead_tokens,
                    lower_bound_tokens=provider_overhead_tokens,
                    safety_upper_bound_tokens=(provider_overhead_tokens + framing_upper_allowance),
                ),
            )
        )
        contributions.sort(
            key=lambda item: (
                STABLE_CONTEXT_LANES.index(item.lane),
                item.contribution_id,
            )
        )
        return FinalRequestMeasurement(
            request_fingerprint=request_fingerprint,
            adapter_revision=self._adapter_revision,
            contributions=tuple(contributions),
        )


__all__ = [
    "MODEL_REQUEST_COST_ADAPTER_REVISION",
    "MessageLaneProvenance",
    "ProviderModelRequestCostAdapter",
    "ProviderRequestFragment",
    "ProviderRequestFragmentKind",
    "ProviderRequestLaneResolver",
    "SystemPromptLaneSpan",
    "SystemPromptProvenance",
    "attach_message_lane_provenance",
    "message_lane_provenance",
    "provider_visible_message_payload",
    "provider_visible_messages_payload",
]
