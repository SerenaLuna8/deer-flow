"""Typed, non-serializable assembly of authoritative runtime context."""

from __future__ import annotations

import copy
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any

from deerflow.guardrails.provider import copy_guardrail_attribution
from deerflow.runtime.context_keys import RuntimeContextKeys


def sanitize_caller_runtime_context(
    caller_context: object,
) -> dict[str, Any]:
    """Copy caller extensions while removing every server-owned key."""

    if not isinstance(caller_context, Mapping):
        return {}
    return {key: value for key, value in caller_context.items() if isinstance(key, str) and not key.startswith(RuntimeContextKeys.RESERVED_PREFIX) and key not in RuntimeContextKeys.SERVER_OWNED_KEYS}


@dataclass(frozen=True, slots=True, repr=False)
class RuntimeContextCarrier:
    """Opaque authoritative values installed into one runtime context.

    This is deliberately not a wire model: authority objects and Skill secret
    values have no JSON/dict export, do not appear in ``repr``, and reject
    pickle serialization.  Call :meth:`build` or :meth:`install_into` only at a
    trusted composition boundary.
    """

    thread_id: str | None = None
    run_id: str | None = None
    app_config: object | None = None
    model_name: str | None = None
    user_id: str | None = None
    user_role: str | None = None
    oauth_provider: str | None = None
    oauth_id: str | None = None
    channel_user_id: str | None = None
    is_subagent: bool | None = None
    private_scope: object | None = None
    authorization_checker: object | None = None
    authorization_boundary: object | None = None
    file_authority: object | None = None
    memory_authority: object | None = None
    guardrail_attribution: Mapping[str, object] | None = None
    run_read_only_mounts: tuple[object, ...] | None = None
    agent_prompt_bundle: object | None = None
    runtime_skills: tuple[object, ...] | None = None
    runtime_mcp_tools: tuple[object, ...] | None = None
    runtime_agent_catalog: object | None = None
    skill_scoped_secrets: Mapping[str, Mapping[str, str]] | None = None
    skill_secret_provider: object | None = None
    current_run_pre_existing_message_ids: frozenset[str] | None = None
    trace_id: str | None = None
    run_journal: object | None = None
    server_abort_event: object | None = None
    vision_dispatch_authority: object | None = None
    memory_archive_context: object | None = None
    host_execution_approval_port: object | None = None
    host_execution_agent_path: tuple[str, ...] | None = None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<opaque>)"

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("runtime authority carrier is not serializable")

    def install_into(
        self,
        context: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        """Install only non-``None`` authority values into *context*."""

        values = (
            (RuntimeContextKeys.THREAD_ID, self.thread_id),
            (RuntimeContextKeys.RUN_ID, self.run_id),
            (RuntimeContextKeys.APP_CONFIG, self.app_config),
            (RuntimeContextKeys.MODEL_NAME, self.model_name),
            (RuntimeContextKeys.USER_ID, self.user_id),
            (RuntimeContextKeys.USER_ROLE, self.user_role),
            (RuntimeContextKeys.OAUTH_PROVIDER, self.oauth_provider),
            (RuntimeContextKeys.OAUTH_ID, self.oauth_id),
            (RuntimeContextKeys.CHANNEL_USER_ID, self.channel_user_id),
            (RuntimeContextKeys.IS_SUBAGENT, self.is_subagent),
            (RuntimeContextKeys.PRIVATE_SCOPE, self.private_scope),
            (
                RuntimeContextKeys.AUTHORIZATION_CHECKER,
                self.authorization_checker,
            ),
            (
                RuntimeContextKeys.AUTHORIZATION_BOUNDARY,
                self.authorization_boundary,
            ),
            (RuntimeContextKeys.FILE_AUTHORITY, self.file_authority),
            (RuntimeContextKeys.MEMORY_AUTHORITY, self.memory_authority),
            (
                RuntimeContextKeys.RUN_READ_ONLY_MOUNTS,
                self.run_read_only_mounts,
            ),
            (RuntimeContextKeys.AGENT_PROMPT_BUNDLE, self.agent_prompt_bundle),
            (RuntimeContextKeys.RUNTIME_SKILLS, self.runtime_skills),
            (RuntimeContextKeys.RUNTIME_MCP_TOOLS, self.runtime_mcp_tools),
            (
                RuntimeContextKeys.RUNTIME_AGENT_CATALOG,
                self.runtime_agent_catalog,
            ),
            (
                RuntimeContextKeys.SKILL_SECRET_PROVIDER,
                self.skill_secret_provider,
            ),
            (
                RuntimeContextKeys.CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS,
                self.current_run_pre_existing_message_ids,
            ),
            (RuntimeContextKeys.TRACE_ID, self.trace_id),
            (RuntimeContextKeys.RUN_JOURNAL, self.run_journal),
            (
                RuntimeContextKeys.SERVER_ABORT_EVENT,
                self.server_abort_event,
            ),
            (
                RuntimeContextKeys.VISION_DISPATCH_AUTHORITY,
                self.vision_dispatch_authority,
            ),
            (
                RuntimeContextKeys.MEMORY_ARCHIVE_CONTEXT,
                self.memory_archive_context,
            ),
            (
                RuntimeContextKeys.HOST_EXECUTION_APPROVAL_PORT,
                self.host_execution_approval_port,
            ),
            (
                RuntimeContextKeys.HOST_EXECUTION_AGENT_PATH,
                self.host_execution_agent_path,
            ),
        )
        for key, value in values:
            if value is not None:
                context[key] = value

        if self.skill_scoped_secrets is not None:
            context[RuntimeContextKeys.SKILL_SCOPED_SECRETS] = copy.deepcopy(
                {path: dict(values) for path, values in self.skill_scoped_secrets.items()},
            )

        attribution = copy_guardrail_attribution(
            self.guardrail_attribution,
            is_subagent=self.is_subagent,
        )
        if attribution is not None:
            context[RuntimeContextKeys.GUARDRAIL_ATTRIBUTION] = attribution
        return context

    def build(
        self,
        caller_context: object = None,
    ) -> dict[str, Any]:
        """Return a sanitized caller context with this authority installed."""

        context = sanitize_caller_runtime_context(caller_context)
        self.install_into(context)
        return context


__all__ = [
    "RuntimeContextCarrier",
    "sanitize_caller_runtime_context",
]
