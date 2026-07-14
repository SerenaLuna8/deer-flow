"""Run lifecycle service layer.

Centralizes the business logic for creating runs, formatting SSE
frames, and consuming stream bridge events.  Router modules
(``thread_runs``, ``runs``) are thin HTTP handlers that delegate here.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException, Request
from langchain_core.messages import BaseMessage
from langchain_core.messages.utils import convert_to_messages
from langgraph.types import Command

from app.gateway.auth_disabled import AUTH_SOURCE_INTERNAL
from app.gateway.deps import get_checkpointer, get_local_provider, get_project_checkpointer, get_run_context, get_run_manager, get_stream_bridge
from app.gateway.internal_auth import (
    INTERNAL_OWNER_USER_ID_HEADER_NAME,
    INTERNAL_SYSTEM_ROLE,
    get_internal_user,
    get_trusted_internal_owner_user_id,
    get_trusted_internal_runtime_user_id,
)
from app.gateway.utils import sanitize_log_param
from app.private_work.asset_runtime import PrivateAssetRuntime
from app.private_work.context import PrivateWorkContext, strip_private_client_fields
from app.private_work.error_mapping import private_work_http_exception
from app.private_work.errors import (
    PrivateWorkAssetStale,
    PrivateWorkConflict,
    PrivateWorkCutover,
    PrivateWorkError,
    PrivateWorkUnavailable,
)
from app.private_work.revalidation import PrivateWorkRevalidator
from app.private_work.run_admission import PrivateRunAdmissionService
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.runtime_context import prepare_private_run_config
from app.projects.capabilities import Capability
from deerflow.config.app_config import get_app_config
from deerflow.persistence.thread_meta import LegacyThreadCreateAuthorityUnavailable
from deerflow.runtime import (
    END_SENTINEL,
    HEARTBEAT_SENTINEL,
    ConflictError,
    DisconnectMode,
    RunManager,
    RunRecord,
    RunStatus,
    StreamBridge,
    UnsupportedStrategyError,
    run_agent,
)
from deerflow.runtime.goal import goal_thread_lock
from deerflow.runtime.run_config_security import (
    DEFAULT_RECURSION_LIMIT as _DEFAULT_RECURSION_LIMIT,
)
from deerflow.runtime.run_config_security import (
    clamp_recursion_limit as _clamp_recursion_limit,
)
from deerflow.runtime.run_config_security import (
    resolve_max_recursion_limit as _resolve_max_recursion_limit,
)
from deerflow.runtime.runs.naming import resolve_root_run_name
from deerflow.runtime.secret_context import redact_config_secrets
from deerflow.runtime.user_context import (
    reset_current_user,
    reset_runtime_storage_user_id,
    set_current_user,
    set_runtime_storage_user_id,
)
from deerflow.trace_context import generate_trace_id, get_current_trace_id

logger = logging.getLogger(__name__)

_TERMINAL_RUN_STATUSES = {
    RunStatus.success,
    RunStatus.error,
    RunStatus.timeout,
    RunStatus.interrupted,
}
_CHECKPOINT_MAP_VALIDATION_DETAIL = "checkpoint.checkpoint_map must be an object"


# ---------------------------------------------------------------------------
# SSE formatting
# ---------------------------------------------------------------------------


def format_sse(event: str, data: Any, *, event_id: str | None = None) -> str:
    """Format a single SSE frame.

    Field order: ``event:`` -> ``data:`` -> ``id:`` (optional) -> blank line.
    This matches the LangGraph Platform wire format consumed by the
    ``useStream`` React hook and the Python ``langgraph-sdk`` SSE decoder.
    """
    payload = json.dumps(data, default=str, ensure_ascii=False)
    parts = [f"event: {event}", f"data: {payload}"]
    if event_id:
        parts.append(f"id: {event_id}")
    parts.append("")
    parts.append("")
    return "\n".join(parts)


def _run_is_terminal(record: RunRecord) -> bool:
    return record.status in _TERMINAL_RUN_STATUSES


async def _terminal_record_stream_missing(bridge: StreamBridge, record: RunRecord) -> bool:
    """True when a terminal run has no retained stream on bridges that can tell."""
    if not _run_is_terminal(record):
        return False
    stream_exists = getattr(bridge, "stream_exists", None)
    if stream_exists is None:
        return False
    try:
        return not bool(await stream_exists(record.run_id))
    except Exception:
        logger.debug(
            "Failed to probe stream existence for terminal run %s",
            sanitize_log_param(record.run_id),
            exc_info=True,
        )
        return False


# ---------------------------------------------------------------------------
# Input / config helpers
# ---------------------------------------------------------------------------


def normalize_stream_modes(raw: list[str] | str | None) -> list[str]:
    """Normalize the stream_mode parameter to a list.

    Default matches what ``useStream`` expects: values + messages-tuple.
    """
    if raw is None:
        return ["values"]
    if isinstance(raw, str):
        return [raw]
    return raw if raw else ["values"]


def normalize_input(raw_input: dict[str, Any] | None) -> dict[str, Any]:
    """Convert LangGraph Platform input format to LangChain state dict.

    Delegates dict→message coercion to ``langchain_core.messages.utils.convert_to_messages``
    so that ``additional_kwargs`` (e.g. uploaded-file metadata — gh #3132), ``id``,
    ``name``, and non-human roles (ai/system/tool) survive unchanged.  An earlier
    hand-rolled version only forwarded ``content`` and collapsed every role to
    ``HumanMessage``, which silently stripped frontend-supplied attachments.

    Malformed message dicts (missing ``role``/``type``/``content``, unsupported
    role, etc.) raise ``HTTPException(400)`` with the offending index, instead
    of bubbling up as a 500.  The gateway is a system boundary, so per-entry
    validation errors are the right shape for clients to retry against.
    """
    if raw_input is None:
        return {}
    messages = raw_input.get("messages")
    if messages and isinstance(messages, list):
        converted: list[Any] = []
        for index, msg in enumerate(messages):
            if isinstance(msg, BaseMessage):
                converted.append(msg)
            elif isinstance(msg, dict):
                try:
                    converted.extend(convert_to_messages([msg]))
                except (ValueError, TypeError, NotImplementedError) as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid message at input.messages[{index}]: {exc}",
                    ) from exc
            else:
                converted.append(msg)
        return {**raw_input, "messages": converted}
    return raw_input


def _require_checkpoint_map_mapping(checkpoint_map: object) -> Mapping[str, object] | None:
    if checkpoint_map is None:
        return None
    if not isinstance(checkpoint_map, Mapping):
        raise HTTPException(status_code=400, detail=_CHECKPOINT_MAP_VALIDATION_DETAIL)
    return checkpoint_map


@dataclass(frozen=True)
class _NormalizedCheckpointControl:
    checkpoint_id: str | None
    checkpoint_ns: str
    checkpoint_map: dict[str, object] | None
    present: bool


def preflight_run_create(body: Any, thread_id: str | None = None) -> None:
    """Validate run-control shapes without reading dependencies or mutating input."""

    raw_config = getattr(body, "config", None)
    if raw_config is not None and not isinstance(raw_config, Mapping):
        raise HTTPException(status_code=400, detail="request config must be an object")
    raw_configurable = raw_config.get("configurable") if isinstance(raw_config, Mapping) else None
    if raw_configurable is not None and not isinstance(raw_configurable, Mapping):
        raise HTTPException(status_code=400, detail="request config configurable must be an object")
    if isinstance(raw_configurable, Mapping):
        _require_checkpoint_map_mapping(raw_configurable.get("checkpoint_map"))

    checkpoint = getattr(body, "checkpoint", None)
    if checkpoint is not None:
        if not isinstance(checkpoint, Mapping):
            raise HTTPException(status_code=400, detail="checkpoint must be an object")
        _require_checkpoint_map_mapping(checkpoint.get("checkpoint_map"))
        checkpoint_thread_id = checkpoint.get("thread_id")
        if thread_id is not None and checkpoint_thread_id is not None and str(checkpoint_thread_id) != thread_id:
            raise HTTPException(status_code=400, detail="checkpoint thread_id does not match request thread_id")


def preflight_run_create_route[**P, T](func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
    """Run pure request-shape validation outside authorization decorators."""

    signature = inspect.signature(func)

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        bound = signature.bind_partial(*args, **kwargs)
        body = bound.arguments.get("body")
        thread_id = bound.arguments.get("thread_id")
        preflight_run_create(body, str(thread_id) if thread_id is not None else None)
        return await func(**bound.arguments)

    return wrapper


def _normalize_run_checkpoint_inputs(body: Any, thread_id: str) -> tuple[dict[str, object] | None, _NormalizedCheckpointControl]:
    preflight_run_create(body, thread_id)
    raw_config = getattr(body, "config", None)
    raw_configurable: Mapping[str, object] = {}
    if isinstance(raw_config, Mapping):
        configurable_value = raw_config.get("configurable")
        if configurable_value is not None:
            if not isinstance(configurable_value, Mapping):
                raise HTTPException(status_code=400, detail="request config configurable must be an object")
            raw_configurable = configurable_value
    _require_checkpoint_map_mapping(raw_configurable.get("checkpoint_map"))

    sanitized_config = strip_private_client_fields(raw_config) if isinstance(raw_config, Mapping) else raw_config
    sanitized_configurable = sanitized_config.get("configurable") if isinstance(sanitized_config, Mapping) else None
    if not isinstance(sanitized_configurable, Mapping):
        sanitized_configurable = {}

    checkpoint_id = sanitized_configurable.get("checkpoint_id")
    checkpoint_id = str(checkpoint_id) if checkpoint_id else None
    checkpoint_ns_value = sanitized_configurable.get("checkpoint_ns")
    checkpoint_ns = str(checkpoint_ns_value) if checkpoint_ns_value is not None else ""
    checkpoint_map_value = sanitized_configurable.get("checkpoint_map")
    checkpoint_map = dict(checkpoint_map_value) if isinstance(checkpoint_map_value, Mapping) else None
    has_checkpoint_control = any(key in raw_configurable for key in ("checkpoint_id", "checkpoint_ns", "checkpoint_map"))

    checkpoint = getattr(body, "checkpoint", None)
    if checkpoint is not None:
        if not isinstance(checkpoint, Mapping):
            raise HTTPException(status_code=400, detail="checkpoint must be an object")
        checkpoint_thread_id = checkpoint.get("thread_id")
        if checkpoint_thread_id is not None and str(checkpoint_thread_id) != thread_id:
            raise HTTPException(status_code=400, detail="checkpoint thread_id does not match request thread_id")
        typed_checkpoint_map = _require_checkpoint_map_mapping(checkpoint.get("checkpoint_map"))
        checkpoint_map = strip_private_client_fields(typed_checkpoint_map) if typed_checkpoint_map is not None else None
        raw_checkpoint_id = checkpoint.get("checkpoint_id")
        body_checkpoint_id = getattr(body, "checkpoint_id", None)
        checkpoint_id = str(raw_checkpoint_id or body_checkpoint_id) if raw_checkpoint_id or body_checkpoint_id else None
        raw_checkpoint_ns = checkpoint.get("checkpoint_ns")
        checkpoint_ns = str(raw_checkpoint_ns) if raw_checkpoint_ns is not None else ""
        has_checkpoint_control = True
    else:
        body_checkpoint_id = getattr(body, "checkpoint_id", None)
        if body_checkpoint_id:
            checkpoint_id = str(body_checkpoint_id)
            has_checkpoint_control = True

    if has_checkpoint_control:
        normalized_config = dict(sanitized_config) if isinstance(sanitized_config, Mapping) else {}
        normalized_configurable = dict(sanitized_configurable)
        for key in ("checkpoint_id", "checkpoint_ns", "checkpoint_map"):
            normalized_configurable.pop(key, None)
        if checkpoint_id is not None:
            normalized_configurable["checkpoint_id"] = checkpoint_id
        normalized_configurable["checkpoint_ns"] = checkpoint_ns
        if checkpoint_map is not None:
            normalized_configurable["checkpoint_map"] = checkpoint_map
        normalized_config["configurable"] = normalized_configurable
        sanitized_config = normalized_config

    return sanitized_config, _NormalizedCheckpointControl(checkpoint_id, checkpoint_ns, checkpoint_map, has_checkpoint_control)


def _install_checkpoint_control(config: dict[str, Any], checkpoint_control: _NormalizedCheckpointControl, thread_id: str) -> None:
    if not checkpoint_control.present:
        return
    configurable = config.setdefault("configurable", {})
    if not isinstance(configurable, dict):
        raise HTTPException(status_code=400, detail="request config configurable must be an object")
    for key in ("checkpoint_id", "checkpoint_ns", "checkpoint_map"):
        configurable.pop(key, None)
    configurable["thread_id"] = thread_id
    if checkpoint_control.checkpoint_id is not None:
        configurable["checkpoint_id"] = checkpoint_control.checkpoint_id
    configurable["checkpoint_ns"] = checkpoint_control.checkpoint_ns
    if checkpoint_control.checkpoint_map is not None:
        configurable["checkpoint_map"] = checkpoint_control.checkpoint_map


_DEFAULT_ASSISTANT_ID = "lead_agent"


# Whitelist of run-context keys that the langgraph-compat layer forwards from
# ``body.context`` into the run config. ``config["context"]`` exists in
# LangGraph >=0.6, but these values must be written to both ``configurable``
# (for legacy ``_get_runtime_config`` consumers) and ``context`` because
# LangGraph >=1.1.9 no longer makes ``ToolRuntime.context`` fall back to
# ``configurable`` for consumers like ``setup_agent``.
_CONTEXT_CONFIGURABLE_KEYS: frozenset[str] = frozenset(
    {
        "model_name",
        "mode",
        "thinking_enabled",
        "reasoning_effort",
        "is_plan_mode",
        "subagent_enabled",
        "max_concurrent_subagents",
        "agent_name",
        "is_bootstrap",
    }
)

# Keys honored only for internally-authenticated callers (the scheduler path).
# ``non_interactive`` strips ``ask_clarification`` from the lead-agent toolset;
# arbitrary HTTP/IM clients must not be able to force autonomous execution.
_CONTEXT_INTERNAL_CALLER_KEYS: frozenset[str] = frozenset({"non_interactive"})

# Keys forwarded from ``body.context`` into ``config['context']`` ONLY (the
# runtime context that becomes ``ToolRuntime.context`` / ``runtime.context``),
# never into ``config['configurable']``. These are read by tools and
# middlewares from ``runtime.context`` and have no reason to live in
# ``configurable`` — and ``configurable`` is persisted in checkpoints, so
# keeping secrets like ``github_token`` out of it avoids writing a
# short-lived installation token into the checkpoint store.
#
#   ``github_token``         — App installation token minted by the GitHub
#                              channel; the bash tool exposes it as
#                              ``GH_TOKEN``/``GITHUB_TOKEN`` so ``gh`` and
#                              ``git`` push as the bot, not the host user.
#   ``disable_clarification`` — set for non-interactive channels (GitHub
#                              webhooks) so ClarificationMiddleware proceeds
#                              instead of dead-ending the run.
_CONTEXT_RUNTIME_ONLY_KEYS: frozenset[str] = frozenset({"github_token", "disable_clarification"})


def strip_internal_context_keys(config: dict[str, Any]) -> None:
    """Drop internal-only keys a non-internal caller smuggled into the run config.

    Gating :func:`merge_run_context_overrides` is not enough on its own:
    ``build_run_config`` copies a client-supplied ``body.config['context']`` /
    ``body.config['configurable']`` verbatim, so the same keys must be scrubbed
    from both sections after the config is assembled.
    """
    for key in _CONTEXT_INTERNAL_CALLER_KEYS:
        config.pop(key, None)
    for section in ("context", "configurable"):
        value = config.get(section)
        if isinstance(value, dict):
            for key in _CONTEXT_INTERNAL_CALLER_KEYS:
                value.pop(key, None)


def merge_run_context_overrides(config: dict[str, Any], context: Mapping[str, Any] | None, *, internal: bool = False) -> None:
    """Merge whitelisted keys from ``body.context`` into both ``config['configurable']``
    and ``config['context']`` so they are visible to legacy configurable readers and
    to LangGraph ``ToolRuntime.context`` consumers (e.g. the ``setup_agent`` tool —
    see issue #2677).

    Private-work authority fields, including ``user_id``, are removed recursively
    before the remaining allowlisted values are merged. Authenticated web identity
    and trusted internal owner attribution are stamped later by
    :func:`inject_authenticated_user_context`.

    :data:`_CONTEXT_INTERNAL_CALLER_KEYS`; those keys are dropped from client
    requests.

    A second set of keys (``_CONTEXT_RUNTIME_ONLY_KEYS`` — e.g. ``github_token``,
    ``disable_clarification``) is forwarded into ``config['context']`` only, never
    ``configurable``. These are secrets / runtime flags read by tools and middlewares
    from ``runtime.context``; keeping them out of ``configurable`` avoids persisting a
    short-lived token in the checkpoint store.
    """
    if not context:
        return
    context = strip_private_client_fields(context)
    configurable = config.setdefault("configurable", {})
    runtime_context = config.setdefault("context", {})
    keys = _CONTEXT_CONFIGURABLE_KEYS | _CONTEXT_INTERNAL_CALLER_KEYS if internal else _CONTEXT_CONFIGURABLE_KEYS
    for key in keys:
        if key in context:
            if isinstance(configurable, dict):
                configurable.setdefault(key, context[key])
            if isinstance(runtime_context, dict):
                runtime_context.setdefault(key, context[key])
    # Context-only keys (secrets / runtime flags) land in ``config['context']``
    # only — never ``configurable`` (which is persisted in checkpoints).
    for key in _CONTEXT_RUNTIME_ONLY_KEYS:
        if key in context and isinstance(runtime_context, dict):
            runtime_context.setdefault(key, context[key])
    # The raw platform user id from IM channels (Feishu open_id, Slack Uxxx, ...)
    # is runtime-only: tools may read it, but it never enters ``configurable``
    # (checkpointed with the thread).
    if "channel_user_id" in context and isinstance(runtime_context, dict):
        runtime_context.setdefault("channel_user_id", context["channel_user_id"])


async def resolve_trusted_internal_owner_for_attribution(request: Request, owner_user_id: str | None) -> Any | None:
    """Resolve the DeerFlow user used only for trusted internal attribution."""

    if not owner_user_id:
        return None
    user = getattr(request.state, "user", None)
    if getattr(user, "system_role", None) != INTERNAL_SYSTEM_ROLE:
        return None
    try:
        return await get_local_provider().get_user(owner_user_id)
    except Exception:
        logger.exception("Failed to resolve trusted internal owner %s", sanitize_log_param(owner_user_id))
        return None


def inject_authenticated_user_context(
    config: dict[str, Any],
    request: Request,
    *,
    internal_owner_user: Any | None = None,
    internal_runtime_user_id: str | None = None,
) -> None:
    """Stamp the authenticated user into the run context for background tools.

    Tool execution may happen after the request handler has returned, so tools
    that persist user-scoped files should not rely only on ambient ContextVars.
    The value comes from server-side auth state, never from client context.
    """

    user = getattr(request.state, "user", None)
    user_id = getattr(user, "id", None)
    if user_id is None:
        return

    if getattr(user, "system_role", None) == INTERNAL_SYSTEM_ROLE:
        runtime_context = config.setdefault("context", {})
        if not isinstance(runtime_context, dict):
            return
        if internal_owner_user is None:
            if internal_runtime_user_id is not None:
                runtime_context["user_id"] = internal_runtime_user_id
            runtime_context.pop("user_role", None)
            runtime_context.pop("oauth_provider", None)
            runtime_context.pop("oauth_id", None)
            return
        owner_user_id = getattr(internal_owner_user, "id", None)
        if owner_user_id is not None:
            runtime_context["user_id"] = str(owner_user_id)
        runtime_context["user_role"] = getattr(internal_owner_user, "system_role", None)
        runtime_context["oauth_provider"] = getattr(internal_owner_user, "oauth_provider", None)
        runtime_context["oauth_id"] = getattr(internal_owner_user, "oauth_id", None)
        return

    runtime_context = config.setdefault("context", {})
    if isinstance(runtime_context, dict):
        runtime_context["user_id"] = str(user_id)
        runtime_context["user_role"] = getattr(user, "system_role", None)
        runtime_context["oauth_provider"] = getattr(user, "oauth_provider", None)
        runtime_context["oauth_id"] = getattr(user, "oauth_id", None)


def resolve_agent_factory(assistant_id: str | None):
    """Resolve the agent factory callable from config.

    Custom agents are implemented as ``lead_agent`` + an ``agent_name``
    injected into ``configurable`` or ``context`` — see
    :func:`build_run_config`.  All ``assistant_id`` values therefore map to the
    same factory; the routing happens inside ``make_lead_agent`` when it reads
    ``cfg["agent_name"]``.
    """
    from deerflow.agents.lead_agent.agent import make_lead_agent

    return make_lead_agent


def build_run_config(
    thread_id: str,
    request_config: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
    *,
    assistant_id: str | None = None,
    client_fields_sanitized: bool = False,
) -> dict[str, Any]:
    """Build a RunnableConfig dict for the agent.

    When *assistant_id* refers to a custom agent (anything other than
    ``"lead_agent"`` / ``None``), the name is forwarded as ``agent_name`` in
    both ``configurable`` and ``context`` so it is visible to legacy
    configurable readers and to LangGraph ``ToolRuntime.context`` consumers
    (e.g. the ``setup_agent`` tool, which since LangGraph >=1.1.9 no longer
    falls back from ``context`` to ``configurable``).  An explicit
    ``agent_name`` in either container takes precedence over the value
    derived from ``assistant_id``.  ``make_lead_agent`` reads this key to
    load the matching ``agents/<name>/SOUL.md`` and per-agent config —
    without it the agent silently runs as the default lead agent.

    This mirrors the channel manager's ``_resolve_run_params`` logic so that
    the LangGraph Platform-compatible HTTP API and the IM channel path behave
    identically.
    """
    if request_config and not client_fields_sanitized:
        request_config = strip_private_client_fields(request_config)
    if metadata:
        metadata = strip_private_client_fields(metadata)
    # Lead-agent recursion budget (LangGraph super-steps for the lead graph
    # only). Independent of subagent depth: a `task()` dispatch runs the whole
    # subagent inside ONE lead tools-node step, and subagents enforce their own
    # limit via `subagents.max_turns`. Do not conflate this 100 with the
    # general-purpose subagent's max_turns.
    config: dict[str, Any] = {"recursion_limit": _DEFAULT_RECURSION_LIMIT}
    if request_config:
        # LangGraph >= 0.6.0 introduced ``context`` as the preferred way to
        # pass thread-level data and rejects requests that include both
        # ``configurable`` and ``context``.  If the caller already sends
        # ``context``, honour it and skip our own ``configurable`` dict.
        if "context" in request_config:
            if "configurable" in request_config:
                logger.warning(
                    "build_run_config: client sent both 'context' and 'configurable'; preferring 'context' (LangGraph >= 0.6.0). thread_id=%s, caller_configurable keys=%s",
                    thread_id,
                    list(request_config.get("configurable", {}).keys()),
                )
            context_value = request_config["context"]
            if context_value is None:
                context = {}
            elif isinstance(context_value, Mapping):
                # The recursive sanitizer already removed private-work authority
                # and ``__``-prefixed internal channels at every nesting depth.
                context = {key: value for key, value in context_value.items() if not (isinstance(key, str) and key.startswith("__"))}
            else:
                raise ValueError("request config 'context' must be a mapping or null.")
            context["thread_id"] = thread_id
            config["context"] = context
            # The checkpointer always scopes state by configurable["thread_id"],
            # regardless of whether the caller drives the run via context (e.g.
            # request-scoped secrets, #3861). thread_id comes from the URL path,
            # not caller config, so mirror it here while keeping secret-bearing
            # context keys out of configurable.
            config["configurable"] = {"thread_id": thread_id}
        else:
            configurable = {"thread_id": thread_id}
            configurable.update(request_config.get("configurable", {}))
            config["configurable"] = configurable
        for k, v in request_config.items():
            if k not in ("configurable", "context"):
                config[k] = v
        # Never trust a client-supplied recursion_limit verbatim: clamp it to a
        # safe server range so a single run cannot execute unbounded LangGraph
        # super-steps (runaway LLM cost / DoS). Applied after the passthrough so
        # it overrides whatever the client sent.
        if "recursion_limit" in request_config:
            max_limit = _resolve_max_recursion_limit()
            clamped = _clamp_recursion_limit(request_config["recursion_limit"], max_limit)
            if clamped != request_config["recursion_limit"]:
                logger.warning(
                    "build_run_config: clamped client recursion_limit %r -> %d (max %d). thread_id=%s",
                    request_config["recursion_limit"],
                    clamped,
                    max_limit,
                    thread_id,
                )
            config["recursion_limit"] = clamped
    else:
        config["configurable"] = {"thread_id": thread_id}

    # Inject custom agent name when the caller specified a non-default assistant.
    # Honour an explicit agent_name in either runtime options container.
    if assistant_id and assistant_id != _DEFAULT_ASSISTANT_ID:
        normalized = assistant_id.strip().lower().replace("_", "-")
        if not normalized or not re.fullmatch(r"[a-z0-9-]+", normalized):
            raise ValueError(f"Invalid assistant_id {assistant_id!r}: must contain only letters, digits, and hyphens after normalization.")
        configurable = config.setdefault("configurable", {})
        runtime_context = config.setdefault("context", {})
        explicit_agent_name: str | None = None
        if isinstance(configurable, dict) and isinstance(configurable.get("agent_name"), str):
            explicit_agent_name = configurable["agent_name"]
        elif isinstance(runtime_context, dict) and isinstance(runtime_context.get("agent_name"), str):
            explicit_agent_name = runtime_context["agent_name"]
        effective_agent_name = explicit_agent_name or normalized
        if isinstance(configurable, dict):
            configurable["agent_name"] = effective_agent_name
        if isinstance(runtime_context, dict):
            runtime_context["agent_name"] = effective_agent_name
        config.setdefault("run_name", resolve_root_run_name(config, normalized))
    if metadata:
        existing_metadata = config.get("metadata")
        merged_metadata = dict(existing_metadata) if isinstance(existing_metadata, Mapping) else {}
        merged_metadata.update(metadata)
        config["metadata"] = merged_metadata
    return config


async def apply_checkpoint_to_run_config(
    config: dict[str, Any],
    *,
    body: Any,
    thread_id: str,
    request: Request,
    checkpoint_control: _NormalizedCheckpointControl | None = None,
    checkpointer: Any | None = None,
) -> dict[str, Any]:
    """Validate an optional run checkpoint and attach it to RunnableConfig."""
    if checkpoint_control is None:
        normalized_config, checkpoint_control = _normalize_run_checkpoint_inputs(
            SimpleNamespace(
                config=config,
                checkpoint=getattr(body, "checkpoint", None),
                checkpoint_id=getattr(body, "checkpoint_id", None),
            ),
            thread_id,
        )
        config.clear()
        if normalized_config is not None:
            config.update(normalized_config)
    _install_checkpoint_control(config, checkpoint_control, thread_id)
    checkpoint_id = checkpoint_control.checkpoint_id
    checkpoint_ns = checkpoint_control.checkpoint_ns
    checkpoint_map = checkpoint_control.checkpoint_map

    if not checkpoint_id:
        return config

    read_config: dict[str, Any] = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
            "checkpoint_id": str(checkpoint_id),
        }
    }
    if checkpoint_map is not None:
        read_config["configurable"]["checkpoint_map"] = checkpoint_map

    if checkpointer is None:
        checkpointer = get_checkpointer(request)
    try:
        checkpoint_tuple = await checkpointer.aget_tuple(read_config)
    except Exception as exc:
        logger.exception("Failed to validate checkpoint %s for thread %s", checkpoint_id, sanitize_log_param(thread_id))
        raise HTTPException(status_code=500, detail="Failed to validate checkpoint") from exc
    if checkpoint_tuple is None:
        raise HTTPException(status_code=404, detail=f"Checkpoint {checkpoint_id} not found")

    return config


# ---------------------------------------------------------------------------
# Run lifecycle
# ---------------------------------------------------------------------------


def _launch_registered_run(
    *,
    bridge: StreamBridge,
    run_manager: RunManager,
    record: RunRecord,
    run_context: Any,
    agent_factory: Any,
    graph_input: dict[str, Any] | Command,
    config: dict[str, Any],
    stream_modes: list[str],
    stream_subgraphs: bool,
    interrupt_before: Any,
    interrupt_after: Any,
    owner_user_id: str | None = None,
    runtime_user_id: str | None = None,
) -> None:
    """Launch the sole ``run_agent`` worker for an already registered run."""

    owner_context_token = None
    storage_context_token = None
    if owner_user_id is not None:
        owner_context_token = set_current_user(SimpleNamespace(id=owner_user_id))
    if runtime_user_id is not None:
        storage_context_token = set_runtime_storage_user_id(runtime_user_id)
    try:
        task = asyncio.create_task(
            run_agent(
                bridge,
                run_manager,
                record,
                ctx=run_context,
                agent_factory=agent_factory,
                graph_input=graph_input,
                config=config,
                stream_modes=stream_modes,
                stream_subgraphs=stream_subgraphs,
                interrupt_before=interrupt_before,
                interrupt_after=interrupt_after,
            )
        )
    finally:
        if storage_context_token is not None:
            reset_runtime_storage_user_id(storage_context_token)
        if owner_context_token is not None:
            reset_current_user(owner_context_token)
    record.task = task


async def start_run(
    body: Any,
    thread_id: str,
    request: Request,
) -> RunRecord:
    """Create a RunRecord and launch the background agent task.

    Parameters
    ----------
    body : RunCreateRequest
        The validated request body (typed as Any to avoid circular import
        with the router module that defines the Pydantic model).
    thread_id : str
        Target thread.
    request : Request
        FastAPI request — used to retrieve singletons from ``app.state``.
    """
    sanitized_config, checkpoint_control = _normalize_run_checkpoint_inputs(body, thread_id)

    bridge = get_stream_bridge(request)
    run_mgr = get_run_manager(request)
    run_ctx = get_run_context(request)

    raw_metadata = getattr(body, "metadata", None)
    sanitized_metadata = strip_private_client_fields(raw_metadata) if isinstance(raw_metadata, Mapping) else {}
    raw_body_context = getattr(body, "context", None)
    sanitized_body_context = strip_private_client_fields(raw_body_context) if isinstance(raw_body_context, Mapping) else {}

    disconnect = DisconnectMode.cancel if body.on_disconnect == "cancel" else DisconnectMode.continue_

    model_name = sanitized_body_context.get("model_name")

    # Coerce non-string model_name values to str before truncation.
    if model_name is not None and not isinstance(model_name, str):
        model_name = str(model_name)

    owner_user_id = get_trusted_internal_owner_user_id(request)
    runtime_user_id = get_trusted_internal_runtime_user_id(request)
    # Stateless run endpoints carry thread_id in the request *body*, so the
    # @require_permission(owner_check=True) decorator -- which resolves ownership
    # from the path param -- cannot protect them. Enforce thread ownership here,
    # before any run is created, so one user cannot start runs on (or read /wait
    # checkpoint state from) another user's thread. Missing rows (auto-created
    # temp threads) and NULL-owner rows (shared / pre-auth data) stay accessible
    # via check_access; only a thread already owned by another user is rejected
    # with 404, matching thread_runs.py's anti-enumeration behaviour. Internal
    # channel runs act on behalf of the connection owner carried in
    # X-DeerFlow-Owner-User-Id, so they are scoped to that owner instead of
    # bypassing the check -- a leaked internal token must not grant cross-user
    # thread access.
    user = getattr(request.state, "user", None)
    if user is not None:
        allowed = await run_ctx.thread_store.check_access(thread_id, str(user.id))
        if not allowed and owner_user_id and getattr(user, "system_role", None) == INTERNAL_SYSTEM_ROLE:
            # Channel workers may also act for the connection owner named in
            # the trusted header (e.g. claiming a legacy default-owned channel
            # thread for its real owner).
            allowed = await run_ctx.thread_store.check_access(thread_id, owner_user_id)
        if not allowed:
            raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")

    # Validate model against the allowlist only after thread authorization.
    if model_name:
        app_config = get_app_config()
        resolved = app_config.get_model_config(model_name)
        if resolved is None:
            raise HTTPException(
                status_code=400,
                detail=f"Model {model_name!r} is not in the configured model allowlist",
            )

    agent_factory = resolve_agent_factory(body.assistant_id)
    command = getattr(body, "command", None)
    if command and command.get("resume") is not None:
        graph_input = Command(resume=command["resume"])
    else:
        graph_input = normalize_input(body.input)
    config = build_run_config(
        thread_id,
        sanitized_config,
        sanitized_metadata,
        assistant_id=body.assistant_id,
        client_fields_sanitized=True,
    )
    # Merge DeerFlow-specific context overrides into both ``configurable`` and ``context``.
    # The ``context`` field is a custom extension for the langgraph-compat layer
    # that carries agent configuration (model_name, thinking_enabled, etc.).
    # Only agent-relevant keys are forwarded; unknown keys (e.g. thread_id) are ignored.
    is_internal_caller = getattr(getattr(request, "state", None), "auth_source", None) == AUTH_SOURCE_INTERNAL
    merge_run_context_overrides(config, sanitized_body_context, internal=is_internal_caller)
    if not is_internal_caller:
        # ``body.config`` is free-form and copied verbatim by
        # ``build_run_config``; scrub internal-only keys smuggled there.
        strip_internal_context_keys(config)
    internal_owner_user = await resolve_trusted_internal_owner_for_attribution(request, owner_user_id)
    inject_authenticated_user_context(
        config,
        request,
        internal_owner_user=internal_owner_user,
        internal_runtime_user_id=runtime_user_id,
    )

    stream_modes = normalize_stream_modes(body.stream_mode)

    owner_context_token = set_current_user(SimpleNamespace(id=owner_user_id)) if owner_user_id else None
    try:
        # A run must never reach raw checkpoint validation, durable run
        # admission, or graph launch without a final-schema authority row.
        # Production intentionally has no guessed legacy project/Agent; until
        # cutover supplies explicit authority, missing rows fail closed with a
        # stable 409 instead of becoming cross-user raw state.
        existing = await run_ctx.thread_store.get(thread_id)
        created_authority = False
        if existing is None and owner_user_id:
            unscoped_existing = await run_ctx.thread_store.get(thread_id, user_id=None)
            if unscoped_existing is not None:
                if unscoped_existing.get("user_id") != owner_user_id:
                    await run_ctx.thread_store.update_owner(
                        thread_id,
                        owner_user_id,
                        user_id=None,
                    )
                existing = await run_ctx.thread_store.get(thread_id)
        if existing is None:
            try:
                existing = await run_ctx.thread_store.create(
                    thread_id,
                    assistant_id=body.assistant_id,
                    metadata=sanitized_metadata,
                )
                created_authority = True
            except LegacyThreadCreateAuthorityUnavailable:
                raise private_work_http_exception(PrivateWorkCutover(get_current_trace_id() or generate_trace_id())) from None

        if isinstance(existing, Mapping) and existing.get("agent_scope") == "project":
            if created_authority:
                try:
                    await run_ctx.thread_store.delete(thread_id)
                except Exception:
                    logger.warning(
                        "Failed to compensate project-agent legacy authority for %s",
                        sanitize_log_param(thread_id),
                        exc_info=True,
                    )
            raise private_work_http_exception(PrivateWorkCutover(get_current_trace_id() or generate_trace_id()))

        try:
            await apply_checkpoint_to_run_config(
                config,
                body=body,
                thread_id=thread_id,
                request=request,
                checkpoint_control=checkpoint_control,
            )
        except Exception:
            if created_authority:
                try:
                    await run_ctx.thread_store.delete(thread_id)
                except Exception:
                    logger.warning(
                        "Failed to compensate thread authority for %s",
                        sanitize_log_param(thread_id),
                        exc_info=True,
                    )
            raise

        try:
            async with goal_thread_lock(thread_id):
                record = await run_mgr.create_or_reject(
                    thread_id,
                    body.assistant_id,
                    on_disconnect=disconnect,
                    metadata=sanitized_metadata,
                    # Persist the authority-sanitized config after removing secrets:
                    # runs.kwargs_json is echoed by the run API. Graph input remains
                    # untouched because message ``role`` is legitimate input data.
                    kwargs={"input": body.input, "config": redact_config_secrets(sanitized_config)},
                    multitask_strategy=body.multitask_strategy,
                    model_name=model_name,
                    user_id=owner_user_id,
                )
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except UnsupportedStrategyError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc

        # Admission already required a durable authority row. Status remains a
        # denormalized UI hint, so a status-only failure is non-fatal.
        try:
            await run_ctx.thread_store.update_status(thread_id, "running")
        except Exception:
            logger.warning("Failed to update thread_meta for %s (non-fatal)", sanitize_log_param(thread_id))

        _launch_registered_run(
            bridge=bridge,
            run_manager=run_mgr,
            record=record,
            run_context=run_ctx,
            agent_factory=agent_factory,
            graph_input=graph_input,
            config=config,
            stream_modes=stream_modes,
            stream_subgraphs=body.stream_subgraphs,
            interrupt_before=body.interrupt_before,
            interrupt_after=body.interrupt_after,
            runtime_user_id=runtime_user_id,
        )

        # Title sync is handled by worker.py's finally block which reads the
        # title from the checkpoint and calls thread_store.update_display_name
        # after the run completes.

        return record
    finally:
        if owner_context_token is not None:
            reset_current_user(owner_context_token)


async def _mark_private_run_launch_failed(
    context: PrivateWorkContext,
    run_id: str,
) -> None:
    from deerflow.persistence.engine import get_session_factory

    try:
        session_factory = get_session_factory()
        async with session_factory() as session, session.begin():
            await PrivateRunRepository(session).update_status(
                scope=context.resource_scope,
                run_id=run_id,
                status="error",
                error="Private runtime launch failed",
            )
    except Exception:
        logger.warning("Failed to mark private run launch failure", exc_info=True)


async def start_private_run(
    body: Any,
    thread_id: str,
    request: Request,
    context: PrivateWorkContext,
    *,
    admission_service: PrivateRunAdmissionService | None = None,
    asset_runtime: PrivateAssetRuntime | None = None,
) -> RunRecord:
    """Admit exact project assets, register them, and launch the sole worker."""

    from deerflow.persistence.engine import get_session_factory

    session_factory = get_session_factory()
    admission_service = admission_service or PrivateRunAdmissionService(session_factory)
    asset_runtime = asset_runtime or PrivateAssetRuntime(session_factory)
    sanitized_config, checkpoint_control = _normalize_run_checkpoint_inputs(body, thread_id)
    raw_metadata = getattr(body, "metadata", None)
    raw_body_context = getattr(body, "context", None)
    config = prepare_private_run_config(
        thread_id=thread_id,
        opaque_scope=context.resource_scope,
        request_config=sanitized_config if isinstance(sanitized_config, Mapping) else None,
        metadata=raw_metadata if isinstance(raw_metadata, Mapping) else None,
        body_context=raw_body_context if isinstance(raw_body_context, Mapping) else None,
    )
    scoped_checkpointer = get_project_checkpointer(request, context)
    async with session_factory() as session, session.begin():
        await PrivateWorkRevalidator().require(
            session,
            context,
            Capability.PRIVATE_WORK_CREATE,
            Capability.SHARED_ASSETS_EXECUTE,
            lock=False,
        )
    await apply_checkpoint_to_run_config(
        config,
        body=body,
        thread_id=thread_id,
        request=request,
        checkpoint_control=checkpoint_control,
        checkpointer=scoped_checkpointer,
    )

    command = getattr(body, "command", None)
    graph_input = Command(resume=command["resume"]) if command and command.get("resume") is not None else normalize_input(body.input)
    persisted_context = dict(config.get("context", {}))
    persisted_context.pop("private_scope", None)
    persisted_config = {
        **config,
        "context": persisted_context,
        "configurable": dict(config.get("configurable", {})),
    }
    disconnect = DisconnectMode.cancel if getattr(body, "on_disconnect", "cancel") == "cancel" else DisconnectMode.continue_
    create_request = PrivateRunCreate(
        assistant_id=None,
        metadata=dict(config.get("metadata", {})),
        kwargs={
            "input": body.input,
            "config": redact_config_secrets(persisted_config),
        },
        multitask_strategy=getattr(body, "multitask_strategy", "reject"),
    )

    admitted = await admission_service.admit(context, thread_id, create_request)
    private_runtime = None
    record = None
    try:
        base_run_context = get_run_context(request)
        exact_model_name = admitted.run.model_name
        if base_run_context.app_config is None or exact_model_name is None or base_run_context.app_config.get_model_config(exact_model_name) is None:
            raise PrivateWorkAssetStale(context.request_id)
        private_runtime = await asset_runtime.materialize(context, admitted)
        if private_runtime.model_ref != exact_model_name:
            raise PrivateWorkAssetStale(context.request_id)
        private_run_context = replace(
            base_run_context,
            checkpointer=scoped_checkpointer,
            thread_store=None,
            private_scope=admitted.opaque_runtime_scope,
            private_agent_runtime=private_runtime,
        )
        run_manager = get_run_manager(request)
        record = await run_manager.register_persisted(
            run_id=admitted.run.run_id,
            thread_id=admitted.thread_id,
            assistant_id=admitted.run.assistant_id,
            on_disconnect=disconnect,
            metadata=admitted.run.metadata,
            kwargs=admitted.run.kwargs,
            multitask_strategy=admitted.run.multitask_strategy,
            model_name=private_runtime.model_ref,
            scope=admitted.opaque_runtime_scope,
            created_at=admitted.run.created_at.isoformat(),
        )
        _launch_registered_run(
            bridge=get_stream_bridge(request),
            run_manager=run_manager,
            record=record,
            run_context=private_run_context,
            agent_factory=resolve_agent_factory(None),
            graph_input=graph_input,
            config=config,
            stream_modes=normalize_stream_modes(getattr(body, "stream_mode", None)),
            stream_subgraphs=bool(getattr(body, "stream_subgraphs", False)),
            interrupt_before=getattr(body, "interrupt_before", None),
            interrupt_after=getattr(body, "interrupt_after", None),
            owner_user_id=admitted.run.owner_user_id,
            runtime_user_id=admitted.run.owner_user_id,
        )
        return record
    except Exception as error:
        if record is not None:
            try:
                await get_run_manager(request).set_status(
                    record.run_id,
                    RunStatus.error,
                    error="Private runtime launch failed",
                )
            except Exception:
                logger.warning("Failed to compensate private run manager state")
        else:
            await _mark_private_run_launch_failed(context, admitted.run.run_id)
        if private_runtime is not None:
            try:
                await private_runtime.aclose()
            except Exception:
                logger.warning("Failed to clean private runtime after launch failure")
        if isinstance(error, ConflictError):
            raise PrivateWorkConflict(context.request_id) from None
        if isinstance(error, PrivateWorkError):
            raise type(error)(context.request_id) from None
        raise PrivateWorkUnavailable(context.request_id) from None


async def launch_scheduled_thread_run(
    *,
    thread_id: str,
    assistant_id: str | None,
    prompt: str,
    request: Request | None = None,
    app: Any | None = None,
    owner_user_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if request is None:
        if app is None:
            raise ValueError("launch_scheduled_thread_run requires request or app")
        request = SimpleNamespace(
            app=app,
            headers=({INTERNAL_OWNER_USER_ID_HEADER_NAME: owner_user_id} if owner_user_id else {}),
            state=SimpleNamespace(
                user=get_internal_user(),
                auth_source=AUTH_SOURCE_INTERNAL,
            ),
            cookies={},
        )
    # SimpleNamespace stands in for the Pydantic run-request body that the
    # HTTP path parses. If start_run gains a new body.* attribute that it reads
    # directly, add the matching field here so the scheduler path stays in sync.
    body = SimpleNamespace(
        assistant_id=assistant_id,
        input={"messages": [{"role": "user", "content": prompt}]},
        command=None,
        metadata=metadata or {},
        config=None,
        # ``user_id`` mirrors what IM channels put in ``body.context`` so
        # runtime-context consumers without a ContextVar fallback (e.g.
        # user-scoped GuardrailMiddleware providers) see the owning user;
        # ``inject_authenticated_user_context`` skips the internal user.
        context=({"non_interactive": True, "user_id": owner_user_id} if owner_user_id else {"non_interactive": True}),
        webhook=None,
        checkpoint_id=None,
        checkpoint=None,
        interrupt_before=None,
        interrupt_after=None,
        stream_mode=None,
        stream_subgraphs=False,
        stream_resumable=None,
        on_disconnect="continue",
        on_completion="keep",
        multitask_strategy="reject",
        after_seconds=None,
        if_not_exists="reject",
        feedback_keys=None,
    )
    record = await start_run(body, thread_id, request)
    return {"run_id": record.run_id, "thread_id": record.thread_id}


async def sse_consumer(
    bridge: StreamBridge,
    record: RunRecord,
    request: Request,
    run_mgr: RunManager,
):
    """Async generator that yields SSE frames from the bridge.

    The ``finally`` block implements ``on_disconnect`` semantics:
    - ``cancel``: abort the background task on client disconnect.
    - ``continue``: let the task run; events are discarded.
    """
    last_event_id = request.headers.get("Last-Event-ID")
    if await _terminal_record_stream_missing(bridge, record):
        yield format_sse("end", None)
        return

    try:
        async for entry in bridge.subscribe(record.run_id, last_event_id=last_event_id):
            if await request.is_disconnected():
                break

            if entry is HEARTBEAT_SENTINEL:
                if await _terminal_record_stream_missing(bridge, record):
                    yield format_sse("end", None)
                    return
                yield ": heartbeat\n\n"
                continue

            if entry is END_SENTINEL:
                yield format_sse("end", None, event_id=entry.id or None)
                return

            yield format_sse(entry.event, entry.data, event_id=entry.id or None)

    finally:
        # store_only records are cross-worker runs hydrated from the RunStore; this
        # worker holds no in-memory task/abort state for them, so run_mgr.cancel()
        # cannot stop the task (it would 409). Skip on_disconnect cancellation for
        # those and only act on runs this worker actually owns.
        if not record.store_only and record.status in (RunStatus.pending, RunStatus.running):
            if record.on_disconnect == DisconnectMode.cancel:
                await run_mgr.cancel(record.run_id)


async def wait_for_run_completion(
    bridge: StreamBridge,
    record: RunRecord,
    request: Request,
    run_mgr: RunManager,
) -> bool:
    """Block until the run publishes ``END_SENTINEL``, honouring on_disconnect.

    The non-streaming ``/wait`` endpoints used to ``await record.task``
    directly with no disconnect handling.  When the client (or an
    intermediate HTTP proxy) timed out during a long tool call such as
    ``pip install``, the handler would swallow ``CancelledError`` and
    serialize whatever checkpoint happened to exist — masking a half-finished
    run as a normal completion (issue #3265).

    This helper consumes the same bridge that ``sse_consumer`` does so the
    wait path shares its disconnect semantics: each wake-up polls
    ``request.is_disconnected()``; on a real disconnect it cancels the
    background run when ``record.on_disconnect`` is ``cancel``.  The bridge's
    heartbeat sentinels guarantee at least one wake-up per
    ``heartbeat_interval`` even when the agent emits no events for a while.

    Returns:
        ``True`` when ``END_SENTINEL`` was observed (run reached a terminal
        state), ``False`` when the loop exited because the client
        disconnected.  Callers must skip checkpoint serialization on
        ``False`` so a partial checkpoint is not returned as a normal
        response.
    """
    completed = False
    if await _terminal_record_stream_missing(bridge, record):
        return True

    try:
        async for entry in bridge.subscribe(record.run_id):
            # END_SENTINEL means the run reached a terminal state; honour it
            # even if the client just disconnected so the caller still serializes
            # the real final checkpoint.
            if entry is END_SENTINEL:
                completed = True
                return True
            if entry is HEARTBEAT_SENTINEL and await _terminal_record_stream_missing(bridge, record):
                completed = True
                return True
            if await request.is_disconnected():
                break
            # Heartbeats and regular events: keep waiting for END_SENTINEL.
        return completed
    finally:
        if not completed and record.status in (RunStatus.pending, RunStatus.running):
            if record.on_disconnect == DisconnectMode.cancel:
                await run_mgr.cancel(record.run_id)
