from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from deerflow.runtime.run_config_security import (
    DEFAULT_RECURSION_LIMIT,
    clamp_recursion_limit,
    resolve_max_recursion_limit,
)

_AUTHORITY_KEYS = frozenset(
    {
        "capability",
        "capabilities",
        "agent",
        "agent_asset_id",
        "agent_id",
        "agent_name",
        "assistant_id",
        "asset_context",
        "auth_source",
        "authorization_checker",
        "available_skills",
        "channel_name",
        "channel_user_id",
        "is_bootstrap",
        "is_plan_mode",
        "file_authority",
        "membership_id",
        "membership_version",
        "mcp_servers",
        "mcps",
        "model",
        "model_name",
        "max_concurrent_subagents",
        "non_interactive",
        "oauth_id",
        "oauth_provider",
        "owner",
        "owner_id",
        "owner_user_id",
        "private_resource_scope",
        "private_agent_runtime",
        "private_scope",
        "private_work_context",
        "project_context",
        "project_id",
        "project_role",
        "project_slug",
        "resource_scope",
        "role",
        "sandbox_id",
        "skill_ids",
        "skills",
        "subagent_enabled",
        "system_role",
        "tool_groups",
        "trusted_asset_context",
        "user_id",
        "user_role",
        "deerflow_private_scope",
        "disable_clarification",
        "run_id",
        "thread_id",
    }
)
_SECRET_KEY_PARTS = (
    "secret",
    "access_key",
    "api_key",
    "authorization",
    "cookie",
    "credential_envelope",
    "ciphertext",
    "password",
    "private_key",
    "key_id",
    "nonce",
    "storage_locator",
    "token",
)


def _is_private_client_key(key: object) -> bool:
    if not isinstance(key, str):
        return True
    normalized = key.lower()
    if normalized.startswith("__") or normalized in _AUTHORITY_KEYS:
        return True
    if any(part in normalized for part in _SECRET_KEY_PARTS):
        return True
    return "checkpoint" in normalized and any(part in normalized for part in ("scope", "marker", "private"))


def _sanitize(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _sanitize(item) for key, item in value.items() if not _is_private_client_key(key)}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize(item) for item in value)
    return value


def _mapping(value: object) -> dict[str, Any]:
    sanitized = _sanitize(value)
    return dict(sanitized) if isinstance(sanitized, Mapping) else {}


def prepare_private_run_config(
    *,
    thread_id: str,
    opaque_scope: object,
    request_config: Mapping[str, object] | None,
    metadata: Mapping[str, object] | None,
    body_context: Mapping[str, object] | None,
) -> dict[str, Any]:
    """Build a secret-free config with server-owned private authority hooks."""

    sanitized_request = _mapping(request_config or {})
    raw_recursion_limit = sanitized_request.pop(
        "recursion_limit",
        DEFAULT_RECURSION_LIMIT,
    )
    caller_context = _mapping(sanitized_request.pop("context", {}))
    caller_configurable = _mapping(sanitized_request.pop("configurable", {}))
    caller_context.update(_mapping(body_context or {}))

    config: dict[str, Any] = sanitized_request
    config["recursion_limit"] = clamp_recursion_limit(
        raw_recursion_limit,
        resolve_max_recursion_limit(),
    )
    config["metadata"] = _mapping(metadata or {})
    config["configurable"] = caller_configurable
    config["context"] = caller_context

    # These writes are intentionally unconditional: client values never win.
    config["configurable"]["thread_id"] = thread_id
    config["context"]["thread_id"] = thread_id
    config["context"]["private_scope"] = opaque_scope
    return config
