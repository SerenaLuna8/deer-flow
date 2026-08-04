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


def _is_registered_upload_read(request: ToolCallRequest) -> bool:
    """Trust only the canonical read tool plus a canonical uploads path."""

    from deerflow.sandbox.tools import read_file_tool

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

        return request.tool_call.get("name") in _REMOTE_CONTENT_TOOL_NAMES or is_private_mcp_tool(getattr(request, "tool", None)) or _is_registered_upload_read(request)

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        result = handler(request)
        if not self._should_sanitize(request):
            return result
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
        return _sanitize_result(result)
