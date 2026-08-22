"""Request-scoped secret carrier in the run context (issue #3861).

Legacy callers pass per-request secrets out-of-band in
``config.context.secrets`` as a mapping of name -> value. The private Worker may
instead provide exact admitted Skill bindings in ``__skill_scoped_secrets`` as
container ``SKILL.md`` path -> name -> value, which isolates Skills that declare
the same environment-variable name. Values never enter the prompt, tool
arguments, or executed command string; they are injected as environment
variables into a Skill's sandbox subprocess only when an activated Skill
declares them via the ``required-secrets`` frontmatter field.

This module centralises the reserved key name and safe extraction so the carrier
contract lives in one place, consumed by the skill-activation middleware (to
build the per-turn injection set) and the tracing redactor (to strip it from
trace payloads).
"""

from __future__ import annotations

import posixpath
from typing import Any

from deerflow.runtime.skill_context_authority import (
    VERIFIED_SKILL_SOURCE_CONTEXT_KEY,
)

# Reserved sub-key of the run context that holds request-scoped secrets supplied
# by the caller. Source of truth for what a skill *may* receive.
SECRETS_CONTEXT_KEY = "secrets"

# Private Worker-to-harness carrier for exact admitted Skill bindings. Values are
# isolated by the Skill's canonical container ``SKILL.md`` path so two Skills
# may safely bind the same environment-variable name to different secret
# values. This key is internal runtime state, never a client input contract.
SKILL_SCOPED_SECRETS_CONTEXT_KEY = "__skill_scoped_secrets"

# Private Worker-to-harness callable that revalidates the admitted Skill
# secret closure and returns a fresh path-scoped carrier for one sandbox
# command.  The callable is opaque app-owned runtime state, never a client
# contract or serializable value.
SKILL_SECRET_PROVIDER_CONTEXT_KEY = "__skill_secret_provider"

# Name-only activation plan produced by SkillActivationMiddleware.  A private
# provider uses this immediately before bash execution to select values from
# the freshly materialized path-scoped carrier.  It intentionally contains no
# secret values.
ACTIVE_SECRET_SOURCES_CONTEXT_KEY = "__active_skill_secret_sources"

# Ephemeral marker set only by the async bash wrapper after a private provider
# succeeds.  It makes a direct synchronous private bash invocation fail closed
# instead of accidentally reusing stale context state.
SKILL_SECRET_EXEC_READY_CONTEXT_KEY = "__skill_secret_exec_ready"

# Reserved sub-key holding the secrets resolved for the currently activated skill
# (binding point A). Written by the skill-activation middleware, read by the bash
# tool. Both reserved keys are stripped from trace payloads (see tracing redactor).
ACTIVE_SECRETS_CONTEXT_KEY = "__active_skill_secrets"

# Reserved sub-key holding the active Skill tool-policy decision for one model
# step. The decision includes a middleware-instance owner token that prevents a
# caller from forging an allow-all decision in its mergeable run context, so the
# entire value must be stripped from every observable serialization surface.
SKILL_TOOL_POLICY_DECISION_CONTEXT_KEY = "__skill_tool_policy_decision"


def _string_pairs(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {key: value for key, value in raw.items() if isinstance(key, str) and isinstance(value, str)}


def extract_request_secrets(context: Any) -> dict[str, str]:
    """Return the caller-supplied request-scoped secrets mapping, or ``{}``.

    Only string-keyed, string-valued entries are kept; anything else is ignored
    so a malformed carrier can never crash secret resolution or injection.
    """
    if not isinstance(context, dict):
        return {}
    return _string_pairs(context.get(SECRETS_CONTEXT_KEY))


def extract_skill_scoped_secrets(context: Any) -> dict[str, dict[str, str]]:
    """Return exact admitted Skill-path secret mappings from internal context.

    The outer key is normalized as a POSIX container path and the inner mapping
    keeps only string pairs. Malformed entries are ignored so a damaged internal
    carrier fails closed instead of crashing the run. An explicitly present
    empty mapping is retained: it deliberately suppresses legacy flat fallback
    for that Skill.
    """
    if not isinstance(context, dict):
        return {}
    raw = context.get(SKILL_SCOPED_SECRETS_CONTEXT_KEY)
    if not isinstance(raw, dict):
        return {}

    scoped: dict[str, dict[str, str]] = {}
    for path, values in raw.items():
        if not isinstance(path, str) or not path or not isinstance(values, dict):
            continue
        scoped[posixpath.normpath(path)] = _string_pairs(values)
    return scoped


def read_active_secrets(context: Any) -> dict[str, str]:
    """Return the secrets resolved for the active skill (the per-run injection
    set), or ``{}``. Read by the bash tool to build the subprocess env."""
    if not isinstance(context, dict):
        return {}
    return _string_pairs(context.get(ACTIVE_SECRETS_CONTEXT_KEY))


def write_slash_skill_source_path(
    context: Any,
    path: str,
    *,
    owner_token: str,
) -> None:
    """Persist an authenticated slash-activated Skill path in run context."""

    if isinstance(context, dict) and isinstance(path, str) and path and isinstance(owner_token, str) and owner_token:
        context[_SLASH_SECRET_SOURCE_KEY] = {
            "path": path,
            "owner_token": owner_token,
        }


def read_slash_skill_source_path(
    context: Any,
    *,
    owner_token: str,
) -> str | None:
    """Return the authenticated slash-activated Skill path, if well formed."""

    if not isinstance(context, dict):
        return None
    source = context.get(_SLASH_SECRET_SOURCE_KEY)
    if not isinstance(source, dict):
        return None
    path = source.get("path")
    source_owner_token = source.get("owner_token")
    if not isinstance(owner_token, str) or not owner_token or source_owner_token != owner_token:
        return None
    return path if isinstance(path, str) and path else None


def resolve_provider_active_secrets(
    context: Any,
    scoped_secrets: Any,
) -> dict[str, str]:
    """Select active values from one freshly materialized private carrier.

    The name-only source plan preserves the existing activation semantics:
    explicit slash activation wins for names it declares; autonomous Skills
    may share a name only when every source supplies the same value.  Malformed
    provider output or source metadata is ignored in the fail-closed direction.
    """

    if not isinstance(context, dict):
        return {}
    raw_sources = context.get(ACTIVE_SECRET_SOURCES_CONTEXT_KEY)
    if not isinstance(raw_sources, tuple):
        return {}
    scoped = {posixpath.normpath(path): _string_pairs(values) for path, values in scoped_secrets.items() if isinstance(path, str) and path and isinstance(values, dict)} if isinstance(scoped_secrets, dict) else {}

    claims: dict[str, list[tuple[str | None, bool, bool]]] = {}
    for source in raw_sources:
        if not isinstance(source, tuple) or len(source) != 4 or not isinstance(source[0], str) or not isinstance(source[1], str) or not isinstance(source[2], tuple) or not isinstance(source[3], bool):
            continue
        _skill_name, path, names, is_explicit = source
        values = scoped.get(posixpath.normpath(path), {})
        for name in names:
            if not isinstance(name, str) or not name:
                continue
            supplied = name in values
            claims.setdefault(name, []).append(
                (
                    values.get(name),
                    supplied,
                    is_explicit,
                )
            )

    injected: dict[str, str] = {}
    for name, secret_claims in claims.items():
        explicit = [claim for claim in secret_claims if claim[2]]
        if explicit:
            value, supplied, _ = explicit[-1]
            if supplied and isinstance(value, str):
                injected[name] = value
            continue
        supplied_values = {value for value, supplied, _ in secret_claims if supplied and isinstance(value, str)}
        if all(supplied for _value, supplied, _explicit in secret_claims) and len(supplied_values) == 1:
            injected[name] = next(iter(supplied_values))
    return injected


def active_provider_secret_request(
    context: Any,
) -> dict[str, frozenset[str]]:
    """Return the validated path/name subset needed by the next command."""

    if not isinstance(context, dict):
        return {}
    raw_sources = context.get(ACTIVE_SECRET_SOURCES_CONTEXT_KEY)
    if not isinstance(raw_sources, tuple):
        return {}
    requested: dict[str, set[str]] = {}
    for source in raw_sources:
        if not isinstance(source, tuple) or len(source) != 4 or not isinstance(source[1], str) or not source[1] or not isinstance(source[2], tuple):
            continue
        path = posixpath.normpath(source[1])
        names = requested.setdefault(path, set())
        names.update(name for name in source[2] if isinstance(name, str) and name)
    return {path: frozenset(names) for path, names in requested.items()}


# Private run-context keys the skill-activation middleware uses to carry secret
# bindings across a run. ``secrets``, ``__skill_scoped_secrets``, and
# ``__active_skill_secrets`` hold values; the binding-source and audit keys hold
# names only. All are listed so the redaction allowlist stays a complete guard
# even if a future edit starts storing a value under one of the name-only keys.
_SLASH_SECRET_SOURCE_KEY = "__slash_skill_secret_source"
_SECRETS_BINDING_AUDIT_KEY = "__skill_secrets_binding_audit"

# Authenticated identity of the latest slash message that already activated in
# this private Run. The reminder is injected only into a per-call request
# override, so it is absent from graph state on the next model step. This
# runtime-only marker prevents a repeated Skill read, reminder, and activation
# audit while secret bindings continue to be recomputed on every model call.
# It contains coordinates and a message id/digest, never a secret value.
_SLASH_SKILL_ACTIVATION_RUN_KEY = "__slash_skill_activation_run"

# Run-context keys whose values are request-scoped secrets and must be stripped
# before a context mapping is serialized anywhere observable (traces, logs).
REDACTED_CONTEXT_KEYS = frozenset(
    {
        SECRETS_CONTEXT_KEY,
        SKILL_SCOPED_SECRETS_CONTEXT_KEY,
        SKILL_SECRET_PROVIDER_CONTEXT_KEY,
        ACTIVE_SECRET_SOURCES_CONTEXT_KEY,
        SKILL_SECRET_EXEC_READY_CONTEXT_KEY,
        ACTIVE_SECRETS_CONTEXT_KEY,
        _SLASH_SECRET_SOURCE_KEY,
        _SECRETS_BINDING_AUDIT_KEY,
        _SLASH_SKILL_ACTIVATION_RUN_KEY,
        SKILL_TOOL_POLICY_DECISION_CONTEXT_KEY,
        VERIFIED_SKILL_SOURCE_CONTEXT_KEY,
    }
)


def redact_secret_context_keys(context: Any) -> Any:
    """Return a shallow copy of ``context`` with secret-bearing keys removed.

    Defensive helper for any code path that serializes the run context into an
    observable surface. ActWeave's own trace-metadata builder never copies the
    context, so this is belt-and-suspenders for future call sites and custom
    tracer configurations.
    """
    if not isinstance(context, dict):
        return context
    return {key: value for key, value in context.items() if key not in REDACTED_CONTEXT_KEYS}


def redact_config_secrets(config: Any) -> Any:
    """Return a copy of a run config safe to persist or echo back to clients.

    The request config (``body.config``) is stored verbatim on the run record
    (``runs.kwargs_json``) and echoed by the run API. Strip the secret-bearing
    keys from its ``context`` so a request-scoped secret is never persisted or
    returned, while the live config that drives the run (built separately) keeps
    them. Non-dict / context-less configs pass through unchanged.
    """
    if not isinstance(config, dict):
        return config
    context = config.get("context")
    if not isinstance(context, dict):
        return config
    redacted = dict(config)
    redacted["context"] = redact_secret_context_keys(context)
    return redacted
