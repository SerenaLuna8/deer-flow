"""Input guardrail middleware for prompt-injection defense (issue #3630).

Escapes blocked XML-like tags in the last genuine user message (e.g.
``<system>`` → ``&lt;system&gt;``) so they render as literal text instead
of structured-context markers.  This preserves the user's intent ("how do
I use ActWeave's <think> tag?") while neutralizing injection attempts —
the same de-identify-don't-reject strategy as AWS Bedrock's PII ANONYMIZE.

Blocked: system-reserved tags (memory, analysis, etc.) + common injection
tags (system, instruction, role, etc.). Normal HTML/XML tags (<div>,
<span>) are NOT escaped.

Clean input is wrapped in plain-text boundary markers as a secondary
semantic defense (OWASP structured-prompt guidance).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import HumanMessage
from langgraph.errors import GraphBubbleUp

from deerflow.agents.human_input import read_human_input_response
from deerflow.utils.messages import ORIGINAL_USER_CONTENT_KEY, message_content_to_text

logger = logging.getLogger(__name__)

_SUMMARY_MESSAGE_NAME = "summary"

# Finite set of blocked tag names: system-reserved + common injection patterns.
#
# Keep this inventory aligned with every framework authority block that is
# rendered into model input.  The paired-tag drift test in
# ``test_input_sanitization_middleware.py`` forces each new block to be either
# blocked here or explicitly reviewed as a non-authority format.
_BLOCKED_TAG_NAMES: frozenset[str] = frozenset(
    {
        # Framework-injected structured/authority blocks.
        "system-reminder",
        "system_reminder",
        "memory",
        "current_date",
        "think",
        "analysis",
        "role",
        "soul",
        "self_update",
        "thinking_style",
        "clarification_system",
        "critical_reminders",
        "response_style",
        "citations",
        "agent_profile",
        "agent_profile_document",
        "subagent_system",
        "skill_system",
        "uploaded_files",
        "current_uploads",
        "skill_index",
        "available_skills",
        "disabled_skills",
        "memory_tool_system",
        "todo_list_system",
        "durable_context_data",
        "slash_skill_activation",
        "mcp_routing_hints",
        "available-deferred-tools",
        "goal_continuation",
        "file_editing_workflow",
        "guidelines",
        "output_format",
        "working_directory",
        "tool_restrictions",
        # Common prompt-injection tag patterns
        "system",
        "instruction",
        "important",
        "override",
        "ignore",
        "prompt",
    }
)

# Matches a full blocked tag: <tag>, </tag>, <tag attrs>, <tag/>, bare <tag
_BLOCKED_TAG_PATTERN = re.compile(
    r"<\s*/?\s*(?:" + "|".join(re.escape(t) for t in sorted(_BLOCKED_TAG_NAMES)) + r")\b[^>]*>?",
    re.IGNORECASE,
)

# Plain-text boundary markers (OWASP structured-prompt guidance).
_USER_INPUT_BEGIN = "--- BEGIN USER INPUT ---"
_USER_INPUT_END = "--- END USER INPUT ---"

# Neutralized forms injected when the user's text already contains a marker.
# These look visually similar but do not match the real boundary delimiters.
_NEUTRALIZED_BEGIN = "[BEGIN USER INPUT]"
_NEUTRALIZED_END = "[END USER INPUT]"

# Matches either boundary token as a standalone line or embedded in text.
_BOUNDARY_TOKEN_RE = re.compile(
    re.escape(_USER_INPUT_BEGIN) + r"|" + re.escape(_USER_INPUT_END),
)


def _escape_tag_match(match: re.Match) -> str:
    """Escape < and > in a blocked-tag match so it renders as literal text."""
    return match.group(0).replace("<", "&lt;").replace(">", "&gt;")


def _neutralize_boundary_tokens(text: str) -> str:
    """Replace real BEGIN/END USER INPUT markers with look-alike inert forms."""
    return _BOUNDARY_TOKEN_RE.sub(
        lambda m: _NEUTRALIZED_BEGIN if m.group(0) == _USER_INPUT_BEGIN else _NEUTRALIZED_END,
        text,
    )


def neutralize_untrusted_tags(text: str) -> str:
    """Neutralize framework/injection control tokens in untrusted text.

    Shared primitive for any content that originates outside the trust boundary
    and is about to enter the model context as *data* — currently the genuine
    user message (via :func:`_check_user_content`) and remote tool results
    (web_fetch / web_search and friends, via
    :class:`ToolResultSanitizationMiddleware`).

    Applies exactly the two structural defenses, and nothing else:

    * blocked framework/injection tags (e.g. ``<system-reminder>``) are
      HTML-escaped to ``&lt;system-reminder&gt;`` so they lose their structural
      meaning while staying human-readable;
    * the plain-text ``--- BEGIN/END USER INPUT ---`` boundary markers are
      neutralized so untrusted content cannot forge or break out of the
      user-input boundary.

    It intentionally does **not** wrap the text in boundary markers: that
    framing is specific to the user message. Empty/whitespace-only text is
    returned unchanged so callers do not emit marker noise.
    """
    if not text.strip():
        return text
    text = _BLOCKED_TAG_PATTERN.sub(_escape_tag_match, text)
    return _neutralize_boundary_tokens(text)


def _is_genuine_user_message(message: object) -> bool:
    """Return True for real user messages, excluding system-injected HumanMessages.

    ``hide_from_ui`` is also used by hidden UI replies from HumanInputCard, so
    only skip hidden HumanMessages that do not carry a valid user response.
    """
    if not isinstance(message, HumanMessage):
        return False
    if message.name == _SUMMARY_MESSAGE_NAME:
        return False
    if message.additional_kwargs.get("hide_from_ui") and read_human_input_response(message.additional_kwargs) is None:
        return False
    return True


def _check_user_content(text: str) -> str:
    """Sanitize user content: escape blocked tags, then wrap in boundary markers.

    * Empty/whitespace-only → return unchanged (no marker noise).
    * Blocked tags → HTML-escape ``<``/``>`` (e.g. ``<system>`` → ``&lt;system&gt;``).
    * Boundary tokens in user text → neutralized so they cannot forge boundaries.
    * Already wrapped (strict prefix+suffix) → return text unchanged (idempotent).
    * Otherwise → wrap in boundary markers.
    """
    if not text.strip():
        return text
    text = _BLOCKED_TAG_PATTERN.sub(_escape_tag_match, text)
    # Idempotency: only skip if text is *exactly* wrapped (prefix+suffix),
    # not if the user merely typed the begin token somewhere.
    if text.startswith(_USER_INPUT_BEGIN) and text.endswith(_USER_INPUT_END):
        # Still neutralize boundary tokens in the inner content — a user
        # can forge the outer wrapping to bypass the neutralization below
        # and inject inner boundary markers (break-out attack).
        inner = text[len(_USER_INPUT_BEGIN) : -len(_USER_INPUT_END)]
        neutralized_inner = _neutralize_boundary_tokens(inner)
        if neutralized_inner == inner:
            return text
        return f"{_USER_INPUT_BEGIN}{neutralized_inner}{_USER_INPUT_END}"
    # Neutralize any boundary tokens the user may have embedded, preventing
    # both self-suppression (begin token skips wrapping) and break-out
    # (end token creates a premature boundary inside the payload).
    text = _neutralize_boundary_tokens(text)
    return f"{_USER_INPUT_BEGIN}\n{text}\n{_USER_INPUT_END}"


class InputSanitizationMiddleware(AgentMiddleware[AgentState]):
    """Guardrail middleware that escapes prompt-injection tags in user input.

    Blocked tags are HTML-escaped (not rejected) so the user's intent is
    preserved while the tags lose their semantic significance. Clean input
    is wrapped in plain-text boundary markers. Transformation is temporary
    (wrap_model_call) — never written to state.
    """

    @staticmethod
    def _extract_text_from_content(
        content: str | list,
    ) -> tuple[str, list[int] | None]:
        """Extract concatenated text from a plain-string or content-block-list.

        Returns ``(text, text_positions)``. ``text_positions`` is ``None`` for
        a plain string, or the indexes of both supported bare-string and typed
        text blocks for list content.
        """
        if isinstance(content, str):
            return content, None
        if not isinstance(content, list):
            return "", None
        text_parts: list[str] = []
        text_positions: list[int] = []
        for position, block in enumerate(content):
            if isinstance(block, str):
                if not block:
                    continue
                text_parts.append(block)
                text_positions.append(position)
            elif isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                text = block["text"]
                if not text:
                    continue
                text_parts.append(text)
                text_positions.append(position)
        return "\n".join(text_parts), text_positions

    @staticmethod
    def _rebuild_content(
        original_content: list,
        processed_text: str,
        text_positions: list[int],
    ) -> list:
        """Replace text blocks with a single merged text block, preserving interleaved non-text blocks.

        For ``[text, image, text]`` the image block between the two text blocks
        is kept in place — only the text blocks are collapsed into one.
        """
        if not text_positions:
            return original_content
        text_position_set = set(text_positions)
        first = min(text_positions)
        last = max(text_positions)
        result: list = [*original_content[:first], {"type": "text", "text": processed_text}]
        # Re-insert any non-text blocks that sat between text blocks
        for i in range(first + 1, last + 1):
            if i not in text_position_set:
                result.append(original_content[i])
        result.extend(original_content[last + 1 :])
        return result

    def _process_request(self, request: ModelRequest) -> ModelRequest:
        """Return a request with every genuine user message sanitized.

        Blocked tags are HTML-escaped (not rejected) so the user's intent is
        preserved while the tags lose their semantic significance. Transformation
        is temporary — the original request is never mutated.
        """
        messages = list(request.messages)
        changed = False
        for i, msg in enumerate(messages):
            if not _is_genuine_user_message(msg):
                if isinstance(msg, HumanMessage):
                    logger.debug(
                        "Input guardrail skipped non-genuine HumanMessage position=%d has_name=%s hidden=%s",
                        i,
                        bool(msg.name),
                        bool(msg.additional_kwargs.get("hide_from_ui")),
                    )
                continue
            content = msg.content
            logger.debug(
                "Input guardrail found genuine user message position=%d content_type=%s",
                i,
                type(content).__name__,
            )

            text_content, text_positions = self._extract_text_from_content(content)

            # No text at all (e.g. image-only message) — pass through
            if not text_content and not isinstance(content, str):
                logger.debug("_process_request: no text content in message — passing through")
                continue

            preserved_kwargs = dict(msg.additional_kwargs or {})
            original_user_content = preserved_kwargs.get(ORIGINAL_USER_CONTENT_KEY)

            # UploadsMiddleware and trusted channel ingress preserve the genuine
            # user text before prepending a server-owned <uploaded_files> block.
            # Sanitize only that suffix so the trusted wrapper remains
            # structural. Private HTTP ingress strips this key from client input;
            # UploadsMiddleware then overwrites it with the actual message text.
            if isinstance(original_user_content, str):
                if not original_user_content:
                    processed = text_content
                elif text_content.endswith(original_user_content):
                    trusted_prefix = text_content[: -len(original_user_content)]
                    processed = trusted_prefix + _check_user_content(original_user_content)
                else:
                    # A malformed marker must never create a trusted prefix.
                    # Full-content sanitization may degrade the upload wrapper,
                    # but remains fail-safe.
                    logger.warning("security_event=input_guardrail_marker_mismatch disposition=sanitize_full_content")
                    processed = _check_user_content(text_content)
            else:
                processed = _check_user_content(text_content)

            if processed == text_content:
                # Already wrapped — continue checking the rest of the history.
                continue

            if text_positions:
                new_content = self._rebuild_content(content, processed, text_positions)
            else:
                new_content = processed

            # Preserve the pre-sanitization user text so downstream consumers that
            # must see the genuine input (slash skill activation, regenerate) can
            # recover it after the BEGIN/END wrapping. setdefault keeps an existing
            # value (e.g. set by UploadsMiddleware or an IM channel) authoritative.
            if not isinstance(original_user_content, str):
                if ORIGINAL_USER_CONTENT_KEY in preserved_kwargs:
                    logger.warning(
                        "security_event=input_guardrail_marker_repaired marker_type=%s",
                        type(original_user_content).__name__,
                    )
                preserved_kwargs[ORIGINAL_USER_CONTENT_KEY] = message_content_to_text(content)
            messages[i] = msg.model_copy(
                update={
                    "content": new_content,
                    "additional_kwargs": preserved_kwargs,
                }
            )
            changed = True
            logger.debug(
                "Input guardrail sanitized user message position=%d content_type=%s",
                i,
                type(content).__name__,
            )
        return request.override(messages=messages) if changed else request

    def _try_process(self, request: ModelRequest) -> ModelRequest:
        """Sanitize request; fail-open on unexpected errors.

        GraphBubbleUp propagates; other exceptions return the original request.
        """
        try:
            return self._process_request(request)
        except GraphBubbleUp:
            raise
        except Exception:
            logger.warning("security_event=input_guardrail_processing_failed disposition=fail_open")
            return request

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return handler(self._try_process(request))

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        return await handler(self._try_process(request))
