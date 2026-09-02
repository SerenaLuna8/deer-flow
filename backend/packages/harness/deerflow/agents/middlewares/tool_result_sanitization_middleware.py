"""Neutralize prompt-injection control tokens in untrusted tool results.

ActWeave already treats the genuine user message as untrusted and neutralizes
framework/injection tags in it (see ``InputSanitizationMiddleware``). Remote
content that the agent *fetches* — web page bodies and search snippets returned
by ``web_fetch`` / ``web_search`` / ``image_search`` — is equally untrusted, yet
it entered the model context verbatim. A page the attacker controls could embed
a forged ``<system-reminder>`` block (or a ``--- END USER INPUT ---`` marker) and
have it reach the model as authoritative framework context.

This middleware narrows that gap by applying the *same* structural
neutralization (``neutralize_untrusted_tags``) to first-party network-tool
results, Worker-registered project-private MCP proxy results, and canonical
upload reads. A fetched ``<system-reminder>`` is escaped to
``&lt;system-reminder&gt;`` exactly like it would be in direct user input.
Other local tool output (for example bash and ordinary file reads) is left
untouched so legitimate code and log content is never mangled.

Scope note: built-in network tools use a name allowlist; project-private MCP
tools use server-owned registered-tool metadata, never model-supplied tool-call
metadata. Non-private MCP tools under arbitrary names are not broadened by that
provenance rule.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace as dc_replace
from pathlib import PurePosixPath
from typing import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

logger = logging.getLogger(__name__)

# Tool names whose results are attacker-influenceable remote content. All
# first-party web providers normalize to these names (see community/*/tools.py),
# so the set stays provider-agnostic.
#
# Worker-created project-private MCP proxies are covered separately through
# registered-tool provenance in ``_should_sanitize``. Non-private MCP tools
# under arbitrary names are not matched here. A name heuristic (matching
# fetch/search/crawl substrings) is intentionally avoided because it would also
# mangle legitimate local output such as ``file_search`` results.
_REMOTE_CONTENT_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "web_capture",
        "web_fetch",
        "web_search",
        "image_search",
    }
)

_UPLOADS_VIRTUAL_PREFIX = ("/", "mnt", "user-data", "uploads")

_VISION_SANITIZATION_TRUNCATION = "Some image evidence was truncated after safety neutralization to keep the result within the bounded evidence contract."


def _is_registered_upload_read(request: ToolCallRequest) -> bool:
    """Trust only the canonical read tool plus a canonical uploads path."""

    from deerflow.sandbox.tooling.files import read_file_tool

    if getattr(request, "tool", None) is not read_file_tool:
        return False
    args = request.tool_call.get("args")
    path = args.get("path") if isinstance(args, dict) else None
    if not isinstance(path, str):
        return False
    parsed = PurePosixPath(path)
    return ".." not in parsed.parts and parsed.parts[:4] == _UPLOADS_VIRTUAL_PREFIX


def _neutralize_content(content: object) -> object:
    """Return *content* with untrusted tags neutralized, preserving its shape.

    Handles the two shapes a ToolMessage content can take:

    * plain ``str`` (what every web tool returns today);
    * a list of content blocks — bare ``str`` elements and
      ``{"type": "text", "text": ...}`` text blocks are rewritten; non-text
      blocks (images, etc.) pass through untouched. The bare-``str`` case
      mirrors ``ToolOutputBudgetMiddleware._message_text``, which already
      anticipates ``str`` items inside a content list.
    """
    # Imported lazily so this module can be loaded even when a test stubs the
    # input-sanitization module, and to mirror the codebase's deferred-import style.
    from deerflow.agents.middlewares.input_sanitization_middleware import neutralize_untrusted_tags

    if isinstance(content, str):
        return neutralize_untrusted_tags(content)
    if isinstance(content, list):
        rebuilt: list[object] = []
        for block in content:
            if isinstance(block, str):
                rebuilt.append(neutralize_untrusted_tags(block))
            elif isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                rebuilt.append({**block, "text": neutralize_untrusted_tags(block["text"])})
            else:
                rebuilt.append(block)
        return rebuilt
    return content


def _sanitize_tool_message(message: ToolMessage) -> ToolMessage:
    """Return a copy of *message* with its content neutralized, or the original."""
    new_content = _neutralize_content(message.content)
    if new_content == message.content:
        return message
    return message.model_copy(update={"content": new_content})


def _encoded_vision_evidence(data: dict[str, object]) -> bytes:
    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _truncate_utf8_prefix(
    value: str,
    *,
    target_bytes: int,
    min_chars: int,
) -> str:
    """Return the longest character-safe prefix within ``target_bytes``."""

    minimum = value[:min_chars]
    if len(minimum.encode("utf-8")) >= target_bytes:
        return minimum
    low = min_chars
    high = len(value)
    while low < high:
        midpoint = (low + high + 1) // 2
        if len(value[:midpoint].encode("utf-8")) <= target_bytes:
            low = midpoint
        else:
            high = midpoint - 1
    return value[:low]


def _compact_sanitized_vision_evidence(
    evidence: object,
) -> str:
    """Shrink sanitized strings without ever producing a JSON fragment.

    The Provider result has already passed the canonical 24KB bound. Escaping
    an untrusted tag (``<system>`` -> ``&lt;system&gt;``) can nevertheless make
    the safe representation larger. This reducer keeps the strict object
    shape, records the loss through ``partial``/``uncertainty`` and trims only
    data strings until the canonical byte ceiling is met.
    """

    from deerflow.vision.contracts import (
        MAX_EVIDENCE_JSON_BYTES,
        VisionEvidence,
    )

    assert isinstance(evidence, VisionEvidence)
    data = evidence.model_dump(mode="json", exclude_none=True)
    data["partial"] = True
    uncertainty = data["uncertainty"]
    assert isinstance(uncertainty, list)
    if _VISION_SANITIZATION_TRUNCATION not in uncertainty:
        if len(uncertainty) >= 32:
            del uncertainty[-1]
        uncertainty.append(_VISION_SANITIZATION_TRUNCATION)

    while True:
        encoded = _encoded_vision_evidence(data)
        if len(encoded) <= MAX_EVIDENCE_JSON_BYTES:
            return VisionEvidence.model_validate(data).canonical_json()

        overflow = len(encoded) - MAX_EVIDENCE_JSON_BYTES
        candidates: list[tuple[int, dict[str, object] | list[object], str | int, int, bool]] = []

        summary = data.get("summary")
        if isinstance(summary, str) and len(summary) > 1:
            candidates.append(
                (len(summary.encode("utf-8")), data, "summary", 1, False),
            )

        evidence_items = data.get("evidence")
        if isinstance(evidence_items, list):
            for item in evidence_items:
                if not isinstance(item, dict):
                    continue
                for field in ("text", "location"):
                    value = item.get(field)
                    if isinstance(value, str) and len(value) > 1:
                        candidates.append(
                            (
                                len(value.encode("utf-8")),
                                item,
                                field,
                                1,
                                False,
                            ),
                        )

        ocr = data.get("ocr")
        if isinstance(ocr, dict):
            full_text = ocr.get("full_text")
            if isinstance(full_text, str) and full_text:
                candidates.append(
                    (
                        len(full_text.encode("utf-8")),
                        ocr,
                        "full_text",
                        0,
                        True,
                    ),
                )

        if isinstance(uncertainty, list):
            for index, value in enumerate(uncertainty):
                if isinstance(value, str) and value != _VISION_SANITIZATION_TRUNCATION and len(value) > 1:
                    candidates.append(
                        (
                            len(value.encode("utf-8")),
                            uncertainty,
                            index,
                            1,
                            False,
                        ),
                    )

        if not candidates:
            raise ValueError("VISION_RESPONSE_TOO_LARGE")

        current_bytes, container, key, min_chars, is_ocr = max(
            candidates,
            key=lambda candidate: candidate[0],
        )
        value = container[key]  # type: ignore[index]
        assert isinstance(value, str)
        target_bytes = max(
            len(value[:min_chars].encode("utf-8")),
            current_bytes - max(overflow + 128, 1),
        )
        replacement = _truncate_utf8_prefix(
            value,
            target_bytes=target_bytes,
            min_chars=min_chars,
        )
        if replacement == value:
            replacement = value[: max(min_chars, len(value) - 1)]
        container[key] = replacement  # type: ignore[index]
        if is_ocr:
            assert isinstance(container, dict)
            container["truncated"] = True


def _sanitize_vision_evidence_content(content: object) -> str:
    """Structurally sanitize one canonical success payload and re-bound it."""

    from deerflow.agents.middlewares.input_sanitization_middleware import (
        neutralize_untrusted_tags,
    )
    from deerflow.vision.contracts import (
        MAX_EVIDENCE_TEXT_CHARS,
        MAX_LOCATION_CHARS,
        MAX_OCR_TEXT_CHARS,
        MAX_SUMMARY_CHARS,
        MAX_UNCERTAINTY_CHARS,
        VisionEvidence,
        VisionEvidenceItem,
        VisionOcrEvidence,
    )

    if not isinstance(content, str):
        raise ValueError("VISION_SCHEMA_MISMATCH")
    source = VisionEvidence.model_validate_json(content)
    truncated = False

    def visible(value: str, limit: int) -> str:
        nonlocal truncated
        sanitized = neutralize_untrusted_tags(value).strip()
        if not sanitized:
            raise ValueError("VISION_SCHEMA_MISMATCH")
        if len(sanitized) > limit:
            sanitized = sanitized[:limit]
            truncated = True
        return sanitized

    safe_summary = visible(source.summary, MAX_SUMMARY_CHARS)
    safe_items = [
        VisionEvidenceItem(
            kind=item.kind,
            text=visible(item.text, MAX_EVIDENCE_TEXT_CHARS),
            location=visible(item.location, MAX_LOCATION_CHARS),
        )
        for item in source.evidence
    ]
    safe_uncertainty = [visible(item, MAX_UNCERTAINTY_CHARS) for item in source.uncertainty]

    safe_ocr = None
    if source.ocr is not None:
        safe_full_text = neutralize_untrusted_tags(source.ocr.full_text)
        ocr_truncated = source.ocr.truncated
        if len(safe_full_text) > MAX_OCR_TEXT_CHARS:
            safe_full_text = safe_full_text[:MAX_OCR_TEXT_CHARS]
            ocr_truncated = True
            truncated = True
        safe_ocr = VisionOcrEvidence(
            full_text=safe_full_text,
            truncated=ocr_truncated,
        )

    if truncated:
        if len(safe_uncertainty) >= 32:
            safe_uncertainty.pop()
        safe_uncertainty.append(_VISION_SANITIZATION_TRUNCATION)

    safe = VisionEvidence(
        ok=True,
        content_type="untrusted_image_evidence",
        schema_version="vision.evidence.v1",
        summary=safe_summary,
        evidence=safe_items,
        ocr=safe_ocr,
        uncertainty=safe_uncertainty,
        partial=source.partial or truncated,
    )
    try:
        return safe.canonical_json()
    except ValueError as error:
        if str(error) != "VISION_RESPONSE_TOO_LARGE":
            raise
    return _compact_sanitized_vision_evidence(safe)


def _sanitize_vision_analysis_content(content: object) -> str:
    """Sanitize one Provider-neutral v2 image analysis result."""

    from deerflow.agents.middlewares.input_sanitization_middleware import (
        neutralize_untrusted_tags,
    )
    from deerflow.vision.contracts import (
        MAX_EVIDENCE_JSON_BYTES,
        MAX_IMAGE_ANALYSIS_TEXT_CHARS,
        InspectImageResult,
    )

    if not isinstance(content, str):
        raise ValueError("VISION_SCHEMA_MISMATCH")
    source = InspectImageResult.model_validate_json(content)
    text = neutralize_untrusted_tags(source.text).strip()
    if not text:
        raise ValueError("VISION_SCHEMA_MISMATCH")
    truncated = source.truncated
    if len(text) > MAX_IMAGE_ANALYSIS_TEXT_CHARS:
        text = text[:MAX_IMAGE_ANALYSIS_TEXT_CHARS]
        truncated = True

    while True:
        result = InspectImageResult(
            ok=True,
            schema_version="inspect_image.result.v2",
            content_type="untrusted_image_analysis",
            mode=source.mode,
            text=text,
            truncated=truncated,
        )
        encoded = _encoded_vision_evidence(
            result.model_dump(mode="json"),
        )
        if len(encoded) <= MAX_EVIDENCE_JSON_BYTES:
            return result.canonical_json()
        if len(text) <= 1:
            raise ValueError("VISION_RESPONSE_TOO_LARGE")
        overflow = len(encoded) - MAX_EVIDENCE_JSON_BYTES
        target_bytes = max(1, len(text.encode("utf-8")) - overflow - 16)
        reduced = _truncate_utf8_prefix(
            text,
            target_bytes=target_bytes,
            min_chars=1,
        )
        if reduced == text:
            reduced = text[:-1]
        text = reduced
        truncated = True


def _vision_schema_error_message(message: ToolMessage) -> ToolMessage:
    from deerflow.vision.contracts import VisionErrorResult

    error = VisionErrorResult(
        ok=False,
        code="VISION_SCHEMA_MISMATCH",
        message="The image analysis response was invalid.",
    )
    additional_kwargs = dict(message.additional_kwargs or {})
    additional_kwargs.update(
        {
            "content_type": "untrusted_image_evidence_error",
            "error_code": "VISION_SCHEMA_MISMATCH",
        },
    )
    return message.model_copy(
        update={
            "content": _encoded_vision_evidence(
                error.model_dump(mode="json"),
            ).decode("utf-8"),
            "status": "error",
            "additional_kwargs": additional_kwargs,
        },
    )


def _safe_vision_validation_diagnostics(error: Exception) -> tuple[tuple[object, ...], ...]:
    """Return only validation locations and stable error types, never values."""

    errors = getattr(error, "errors", None)
    if not callable(errors):
        return ()
    try:
        details = errors(include_input=False, include_url=False)
    except (TypeError, ValueError):
        return ()
    if not isinstance(details, list):
        return ()
    return tuple(
        (
            tuple(detail.get("loc", ())),
            detail.get("type"),
        )
        for detail in details
        if isinstance(detail, dict)
    )


def _sanitize_vision_tool_message(message: ToolMessage) -> ToolMessage:
    """Sanitize a Vision result as typed data or collapse it fail-closed."""

    from deerflow.vision.contracts import VisionErrorResult

    if message.status == "error":
        try:
            if not isinstance(message.content, str):
                raise ValueError("VISION_SCHEMA_MISMATCH")
            error = VisionErrorResult.model_validate_json(message.content)
        except (TypeError, ValueError) as error:
            content_bytes = len(message.content.encode("utf-8")) if isinstance(message.content, str) else None
            metadata = message.additional_kwargs or {}
            logger.info(
                "vision_error_sanitization_failed name=%s content_type=%s content_bytes=%s metadata_keys=%s source_error_code=%s error_type=%s validation=%s",
                message.name,
                type(message.content).__name__,
                content_bytes,
                tuple(sorted(metadata.keys())),
                metadata.get("error_code"),
                type(error).__name__,
                _safe_vision_validation_diagnostics(error),
            )
            return _vision_schema_error_message(message)
        canonical = _encoded_vision_evidence(
            error.model_dump(mode="json"),
        ).decode("utf-8")
        additional_kwargs = dict(message.additional_kwargs or {})
        additional_kwargs.update(
            {
                "content_type": "untrusted_image_evidence_error",
                "error_code": error.code,
            },
        )
        if canonical == message.content and additional_kwargs == message.additional_kwargs:
            return message
        return message.model_copy(
            update={
                "content": canonical,
                "additional_kwargs": additional_kwargs,
            },
        )

    metadata = message.additional_kwargs or {}
    schema_version = metadata.get("schema_version")
    try:
        if schema_version == "inspect_image.result.v2":
            content = _sanitize_vision_analysis_content(message.content)
            content_type = "untrusted_image_analysis"
        elif schema_version in {None, "vision.evidence.v1"}:
            content = _sanitize_vision_evidence_content(message.content)
            content_type = "untrusted_image_evidence"
            schema_version = "vision.evidence.v1"
        else:
            raise ValueError("VISION_SCHEMA_MISMATCH")
    except (TypeError, ValueError) as error:
        content_bytes = len(message.content.encode("utf-8")) if isinstance(message.content, str) else None
        logger.info(
            "vision_evidence_sanitization_failed status=%s name=%s content_type=%s content_bytes=%s metadata_keys=%s error_type=%s validation=%s",
            message.status,
            message.name,
            type(message.content).__name__,
            content_bytes,
            tuple(sorted((message.additional_kwargs or {}).keys())),
            type(error).__name__,
            _safe_vision_validation_diagnostics(error),
        )
        return _vision_schema_error_message(message)
    additional_kwargs = dict(message.additional_kwargs or {})
    additional_kwargs.update(
        {
            "content_type": content_type,
            "schema_version": schema_version,
        },
    )
    if content == message.content and additional_kwargs == message.additional_kwargs:
        return message
    return message.model_copy(
        update={
            "content": content,
            "additional_kwargs": additional_kwargs,
        },
    )


def _sanitize_vision_result(
    result: ToolMessage | Command,
) -> ToolMessage | Command:
    if isinstance(result, ToolMessage):
        return _sanitize_vision_tool_message(result)
    update = getattr(result, "update", None)
    if not isinstance(update, dict):
        return result
    messages = update.get("messages")
    if isinstance(messages, ToolMessage):
        sanitized_message = _sanitize_vision_tool_message(messages)
        if sanitized_message is messages:
            return result
        return dc_replace(
            result,
            update={**update, "messages": sanitized_message},
        )
    if isinstance(messages, (list, tuple)):
        sanitized = tuple(_sanitize_vision_tool_message(message) if isinstance(message, ToolMessage) else message for message in messages)
        if sanitized == tuple(messages):
            return result
        rebuilt = list(sanitized) if isinstance(messages, list) else sanitized
        return dc_replace(result, update={**update, "messages": rebuilt})
    return result


def _sanitize_result(result: ToolMessage | Command) -> ToolMessage | Command:
    """Neutralize a tool-call result (``ToolMessage`` or ``Command``)."""
    if isinstance(result, ToolMessage):
        return _sanitize_tool_message(result)
    update = getattr(result, "update", None)
    if isinstance(update, dict):
        messages = update.get("messages")
        if isinstance(messages, ToolMessage):
            new_messages = _sanitize_tool_message(messages)
            if new_messages != messages:
                return dc_replace(result, update={**update, "messages": new_messages})
        elif isinstance(messages, (list, tuple)) and any(isinstance(message, ToolMessage) for message in messages):
            sanitized = tuple(_sanitize_tool_message(message) if isinstance(message, ToolMessage) else message for message in messages)
            new_messages = list(sanitized) if isinstance(messages, list) else sanitized
            if new_messages != messages:
                return dc_replace(
                    result,
                    update={**update, "messages": new_messages},
                )
    return result


class ToolResultSanitizationMiddleware(AgentMiddleware[AgentState]):
    """Escape injection/framework tags in remote tool results before the model sees them.

    Results of first-party network tools, Worker-registered project-private MCP
    proxies, and canonical upload reads are rewritten. Other tool output is
    returned unchanged. This mirrors the user-input guardrail so untrusted
    indirect content and direct user input receive the same structural
    neutralization.

    Built-in web tools are recognized by ``_REMOTE_CONTENT_TOOL_NAMES``.
    Project-private MCP tools are recognized from the actual registered tool
    object, not model-controlled tool-call metadata.
    """

    def _should_sanitize(self, request: ToolCallRequest) -> bool:
        from deerflow.tools.mcp_metadata import is_private_mcp_tool
        from deerflow.vision.provenance import is_vision_evidence_tool

        tool = getattr(request, "tool", None)
        return request.tool_call.get("name") in _REMOTE_CONTENT_TOOL_NAMES or is_private_mcp_tool(tool) or is_vision_evidence_tool(tool) or _is_registered_upload_read(request)

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        result = handler(request)
        if not self._should_sanitize(request):
            return result
        from deerflow.vision.provenance import is_vision_evidence_tool

        if is_vision_evidence_tool(getattr(request, "tool", None)):
            return _sanitize_vision_result(result)
        return _sanitize_result(result)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        result = await handler(request)
        if not self._should_sanitize(request):
            return result
        from deerflow.vision.provenance import is_vision_evidence_tool

        if is_vision_evidence_tool(getattr(request, "tool", None)):
            return _sanitize_vision_result(result)
        return _sanitize_result(result)
