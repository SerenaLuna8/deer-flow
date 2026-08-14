"""Canonical runtime-context key names and trust classifications.

The context mapping crosses caller, Gateway, Worker, middleware, tool, and
subagent boundaries.  Keep the string protocol in this module so an authority
key cannot be added to one merge branch while being omitted from another
branch's sanitization list.
"""

from typing import Final

from deerflow.agents.memory.authority_resolution import (
    MEMORY_AUTHORITY_CONTEXT_KEY,
)
from deerflow.agents.memory.snip import MEMORY_ARCHIVE_CONTEXT_KEY
from deerflow.guardrails.provider import GUARDRAIL_ATTRIBUTION_CONTEXT_KEY
from deerflow.runtime.host_execution_approval import (
    HOST_EXECUTION_AGENT_PATH_CONTEXT_KEY,
    HOST_EXECUTION_APPROVAL_CONTEXT_KEY,
)
from deerflow.runtime.secret_context import (
    REDACTED_CONTEXT_KEYS,
    SECRETS_CONTEXT_KEY,
    SKILL_SCOPED_SECRETS_CONTEXT_KEY,
    SKILL_SECRET_PROVIDER_CONTEXT_KEY,
)
from deerflow.runtime.skill_context_authority import (
    LEAD_MODEL_CALL_SEQ_CONTEXT_KEY,
    VERIFIED_SKILL_SOURCE_CONTEXT_KEY,
)
from deerflow.subagents.runtime_catalog import (
    RUNTIME_AGENT_CATALOG_CONTEXT_KEY,
)
from deerflow.trace_context import DEERFLOW_TRACE_METADATA_KEY

CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY: Final[str] = "__deerflow_pre_run_message_ids"


class RuntimeContextKeys:
    """Names and closed key sets for the runtime-context trust boundary."""

    RESERVED_PREFIX: Final[str] = "__"
    THREAD_ID: Final[str] = "thread_id"
    RUN_ID: Final[str] = "run_id"
    APP_CONFIG: Final[str] = "app_config"
    MODEL_NAME: Final[str] = "model_name"
    USER_ID: Final[str] = "user_id"
    USER_ROLE: Final[str] = "user_role"
    OAUTH_PROVIDER: Final[str] = "oauth_provider"
    OAUTH_ID: Final[str] = "oauth_id"
    CHANNEL_USER_ID: Final[str] = "channel_user_id"
    IS_SUBAGENT: Final[str] = "is_subagent"
    PRIVATE_SCOPE: Final[str] = "private_scope"
    AUTHORIZATION_CHECKER: Final[str] = "__authorization_checker"
    AUTHORIZATION_BOUNDARY: Final[str] = "__authorization_boundary"
    FILE_AUTHORITY: Final[str] = "__file_authority"
    MEMORY_AUTHORITY: Final[str] = MEMORY_AUTHORITY_CONTEXT_KEY
    LEGACY_MEMORY_AUTHORITY: Final[str] = "memory_authority"
    GUARDRAIL_ATTRIBUTION: Final[str] = GUARDRAIL_ATTRIBUTION_CONTEXT_KEY
    RUN_READ_ONLY_MOUNTS: Final[str] = "__run_read_only_mounts"
    AGENT_PROMPT_BUNDLE: Final[str] = "__agent_prompt_bundle"
    RUNTIME_SKILLS: Final[str] = "__runtime_skills"
    RUNTIME_MCP_TOOLS: Final[str] = "__runtime_mcp_tools"
    RUNTIME_AGENT_CATALOG: Final[str] = RUNTIME_AGENT_CATALOG_CONTEXT_KEY
    SKILL_SCOPED_SECRETS: Final[str] = SKILL_SCOPED_SECRETS_CONTEXT_KEY
    SKILL_SECRET_PROVIDER: Final[str] = SKILL_SECRET_PROVIDER_CONTEXT_KEY
    CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS: Final[str] = CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY
    TRACE_ID: Final[str] = DEERFLOW_TRACE_METADATA_KEY
    RUN_JOURNAL: Final[str] = "__run_journal"
    MEMORY_ARCHIVE_CONTEXT: Final[str] = MEMORY_ARCHIVE_CONTEXT_KEY
    STOP_REASON: Final[str] = "stop_reason"
    VERIFIED_SKILL_SOURCE: Final[str] = VERIFIED_SKILL_SOURCE_CONTEXT_KEY
    LEAD_MODEL_CALL_SEQ: Final[str] = LEAD_MODEL_CALL_SEQ_CONTEXT_KEY
    SANDBOX_ID: Final[str] = "sandbox_id"
    HOST_EXECUTION_APPROVAL_PORT: Final[str] = HOST_EXECUTION_APPROVAL_CONTEXT_KEY
    HOST_EXECUTION_AGENT_PATH: Final[str] = HOST_EXECUTION_AGENT_PATH_CONTEXT_KEY

    # Supported non-authority values that intentionally survive a Worker merge.
    # Unknown non-reserved extension keys remain pass-through compatible too;
    # this set documents the stable, application-owned subset.
    AGENT_NAME: Final[str] = "agent_name"
    DISABLE_CLARIFICATION: Final[str] = "disable_clarification"
    NON_INTERACTIVE: Final[str] = "non_interactive"
    SECRETS: Final[str] = SECRETS_CONTEXT_KEY

    INSTALL_KEYS: Final[frozenset[str]] = frozenset(
        {
            THREAD_ID,
            RUN_ID,
            APP_CONFIG,
            MODEL_NAME,
            USER_ID,
            USER_ROLE,
            OAUTH_PROVIDER,
            OAUTH_ID,
            CHANNEL_USER_ID,
            IS_SUBAGENT,
            PRIVATE_SCOPE,
            AUTHORIZATION_CHECKER,
            AUTHORIZATION_BOUNDARY,
            FILE_AUTHORITY,
            MEMORY_AUTHORITY,
            GUARDRAIL_ATTRIBUTION,
            RUN_READ_ONLY_MOUNTS,
            AGENT_PROMPT_BUNDLE,
            RUNTIME_SKILLS,
            RUNTIME_MCP_TOOLS,
            RUNTIME_AGENT_CATALOG,
            SKILL_SCOPED_SECRETS,
            SKILL_SECRET_PROVIDER,
            CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS,
            TRACE_ID,
            RUN_JOURNAL,
            MEMORY_ARCHIVE_CONTEXT,
            HOST_EXECUTION_APPROVAL_PORT,
            HOST_EXECUTION_AGENT_PATH,
        },
    )
    CALLER_PASSTHROUGH_KEYS: Final[frozenset[str]] = frozenset(
        {
            AGENT_NAME,
            DISABLE_CLARIFICATION,
            NON_INTERACTIVE,
            SECRETS,
        },
    )
    SERVER_OWNED_KEYS: Final[frozenset[str]] = frozenset(
        {
            *INSTALL_KEYS,
            LEGACY_MEMORY_AUTHORITY,
            STOP_REASON,
            VERIFIED_SKILL_SOURCE,
            LEAD_MODEL_CALL_SEQ,
            SANDBOX_ID,
            *(REDACTED_CONTEXT_KEYS - {SECRETS_CONTEXT_KEY}),
        },
    )


__all__ = [
    "CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY",
    "RuntimeContextKeys",
]
