"""Lead-only proposal of one durable memory line for the next Dream.

The model supplies only the durability tag and one line of content; the
Worker-issued Memory authority owns project, owner, namespace, thread, and
Run identity, revalidates the live Run, and enforces the per-Run and backlog
caps server-side. Nothing is written to the memory document directly — the
proposal lands in the pending backlog and is consolidated by Dream.
"""

import re
from typing import Annotated

from langchain.tools import InjectedToolCallId, tool

from deerflow.agents.memory.authority_resolution import resolve_memory_authority
from deerflow.memory_contract import EPISODE_SEARCH_TAGS
from deerflow.tools.types import Runtime

# Compatibility alias for callers that imported the tool-local vocabulary.
REMEMBER_KINDS = EPISODE_SEARCH_TAGS
MAX_REMEMBER_CHARS = 500

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

_UNAVAILABLE_TEXT = "Project memory is unavailable in this run, so nothing can be remembered."
_DISABLED_TEXT = "Project memory is currently disabled, so nothing can be remembered."
_RUN_LIMIT_TEXT = "Error: this run already proposed the maximum number of memory entries; do not call remember again in this run."
_BACKLOG_FULL_TEXT = "Error: the memory backlog is full; pending entries must be organized before new ones can be proposed."


@tool("remember", parse_docstring=True)
async def remember_tool(
    runtime: Runtime,
    content: str,
    kind: str,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> str:
    """Propose one line to keep in the user's project memory.

    Use this only when the user states a durable fact, preference, or
    correction worth keeping. The entry is queued for the next memory
    organization pass; it does not change the current memory immediately.

    Args:
        content: One plain line describing the fact to remember
            (1-500 characters, no newlines).
        kind: Durability of the fact; one of "permanent", "durable",
            "ephemeral", or "correction".
    """

    if kind not in REMEMBER_KINDS:
        allowed = ", ".join(REMEMBER_KINDS)
        return f"Error: unknown kind {kind!r}; allowed kinds are {allowed}."

    if not isinstance(content, str):
        return "Error: content must be non-empty text."
    # Inspect the exact model-supplied value before trimming surrounding spaces.
    # Otherwise a trailing newline or tab would be stripped away and silently
    # accepted even though the contract permits one printable line only.
    if _CONTROL_CHARS.search(content):
        return "Error: content must be a single line without control characters."
    if not content.strip():
        return "Error: content must be non-empty text."
    normalized = content.strip()
    if len(normalized) > MAX_REMEMBER_CHARS:
        return f"Error: content must be at most {MAX_REMEMBER_CHARS} characters."

    authority = resolve_memory_authority(
        runtime.context if isinstance(runtime.context, dict) else {},
        method="propose_entry",
    )
    if authority is None:
        return _UNAVAILABLE_TEXT

    # AuthorizationRevoked intentionally propagates: a revoked Run must fail
    # closed instead of continuing with a soft tool error.
    outcome = await authority.propose_entry(
        kind=kind,
        content=normalized,
        tool_call_id=tool_call_id,
    )
    disposition = outcome.disposition
    if disposition == "memory_disabled":
        return _DISABLED_TEXT
    if disposition == "run_limit_reached":
        return _RUN_LIMIT_TEXT
    if disposition == "backlog_full":
        return _BACKLOG_FULL_TEXT
    return f"Remembered for the next organization pass: {outcome.tagged_text}"
