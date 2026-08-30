from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain_core.messages import HumanMessage

ORIGINAL_USER_CONTENT_KEY = "original_user_content"
SUMMARY_MESSAGE_NAME = "summary"


def message_content_to_text(content: Any) -> str:
    """Extract text from LangChain message content shapes."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part for part in parts if part)
    return str(content)


def message_to_text(message: Any, *, text_attribute_fallback: bool = False) -> str:
    """Extract display text from a whole message (``BaseMessage`` or dict-shaped).

    Reads ``content`` from either an attribute (``BaseMessage``) or a mapping key
    (``run_events`` rows are dicts), then walks the mixed ``content`` shapes:
    plain string; a list of string / ``{"text": ...}`` / nested ``{"content": ...}``
    blocks joined without a separator; or a mapping with a ``text``/``content`` key.
    Set ``text_attribute_fallback=True`` to fall back to ``message.text`` when
    content yields nothing (matches ``RunJournal._message_text``).

    Unlike :func:`message_content_to_text` (which takes raw ``content`` and joins
    list blocks with newlines), this keeps the no-separator join and the broader
    shape handling that several call sites had each reimplemented.
    """
    content = message.get("content") if isinstance(message, Mapping) else getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
                else:
                    nested = block.get("content")
                    if isinstance(nested, str):
                        parts.append(nested)
        return "".join(parts)
    if isinstance(content, Mapping):
        for key in ("text", "content"):
            value = content.get(key)
            if isinstance(value, str):
                return value
    if text_attribute_fallback:
        text = getattr(message, "text", None)
        if isinstance(text, str):
            return text
    return ""


def reasoning_block_text(block: Mapping[str, Any]) -> str:
    """Extract provider-supplied reasoning text from one content block.

    Covers direct text keys used by Anthropic/DeepSeek-style blocks and the
    OpenAI Responses shape ``{"type": "reasoning", "summary": [{"type":
    "summary_text", "text": ...}]}`` (streamed chunks carry the same nesting).
    Returns an empty string when the block is not reasoning or carries none.
    """
    if block.get("type") not in {"thinking", "reasoning"}:
        return ""
    for key in ("thinking", "reasoning", "text", "content"):
        value = block.get(key)
        if isinstance(value, str) and value:
            return value
    summary = block.get("summary")
    if not isinstance(summary, list):
        return ""
    parts: list[str] = []
    for item in summary:
        if not isinstance(item, Mapping) or item.get("type") != "summary_text":
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    # Settled summary entries are complete paragraphs (streaming deltas are
    # aggregated per entry upstream), so join with a paragraph break instead
    # of gluing the last word of one paragraph to the first of the next.
    return "\n\n".join(parts)


def get_original_user_content_text(content: Any, additional_kwargs: Mapping[str, Any] | None) -> str:
    """Return pre-middleware user text when available, otherwise content text."""
    original_content = (additional_kwargs or {}).get(ORIGINAL_USER_CONTENT_KEY)
    if isinstance(original_content, str):
        return original_content
    return message_content_to_text(content)


def is_real_user_message(message: object) -> bool:
    """Return whether ``message`` is a real user-authored HumanMessage.

    Middleware-injected hidden HumanMessages and summarization markers should not
    drive user-intent features such as slash-skill activation or MCP routing.
    """
    if not isinstance(message, HumanMessage):
        return False
    if message.name == SUMMARY_MESSAGE_NAME:
        return False
    if message.additional_kwargs.get("hide_from_ui"):
        return False
    return True
