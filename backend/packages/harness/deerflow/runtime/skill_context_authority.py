"""Run-scoped authority evidence for successfully read Skill entry files.

``ThreadState.skill_context`` is durable, user-observable conversation state.
It is useful as a reminder, but it is not an authorization source because a
private Run request can supply graph state.  This module keeps the separate
ephemeral proof used by the tool policy and secret-binding boundary.

Each evidence entry records the lead model-call ordinal at capture time so
consumers can expire stale reads: ``allowed-tools`` only ever narrows, so an
expired entry simply restores the pre-activation default tool set (D10).
"""

from __future__ import annotations

import posixpath
import secrets
from typing import Any

from deerflow.private_scope import PrivateResourceScope

VERIFIED_SKILL_SOURCE_CONTEXT_KEY = "__verified_skill_read_sources"

# Run-scoped ordinal of lead model calls, advanced exactly once per lead model
# call by SkillActivationMiddleware (the outermost skill middleware). Only the
# difference between values matters, so a caller-supplied starting value can
# never extend or shorten an evidence TTL window.
LEAD_MODEL_CALL_SEQ_CONTEXT_KEY = "__lead_model_call_seq"

_EVIDENCE_VERSION = 2
_MAX_ACTIVE_SKILLS = 8

type _RunIdentity = tuple[str, str, str, str]


def advance_lead_model_call_seq(context: Any) -> int:
    """Advance and return the run-scoped lead model-call ordinal."""

    if not isinstance(context, dict):
        return 0
    raw = context.get(LEAD_MODEL_CALL_SEQ_CONTEXT_KEY)
    seq = raw + 1 if type(raw) is int and raw >= 0 else 1
    context[LEAD_MODEL_CALL_SEQ_CONTEXT_KEY] = seq
    return seq


def read_lead_model_call_seq(context: Any) -> int:
    """Return the current lead model-call ordinal (``0`` before the first call)."""

    if not isinstance(context, dict):
        return 0
    raw = context.get(LEAD_MODEL_CALL_SEQ_CONTEXT_KEY)
    return raw if type(raw) is int and raw >= 0 else 0


def _run_identity(context: Any) -> _RunIdentity | None:
    if not isinstance(context, dict):
        return None
    run_id = context.get("run_id")
    scope = context.get("private_scope")
    if not isinstance(run_id, str) or not run_id or type(scope) is not PrivateResourceScope or not scope.project_id or not scope.owner_user_id:
        return None
    return (
        "private-v1",
        scope.project_id,
        scope.owner_user_id,
        run_id,
    )


def _validated_entries(evidence: dict) -> tuple[tuple[str, int], ...] | None:
    entries = evidence.get("entries")
    if not isinstance(entries, list) or not entries or len(entries) > _MAX_ACTIVE_SKILLS:
        return None
    validated: list[tuple[str, int]] = []
    for entry in entries:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            return None
        path, seq = entry
        if not isinstance(path, str) or not path or posixpath.normpath(path) != path or type(seq) is not int or seq < 0:
            return None
        validated.append((path, seq))
    if len({path for path, _ in validated}) != len(validated):
        return None
    return tuple(validated)


def read_verified_skill_source_entries(
    context: Any,
    *,
    owner_token: str,
) -> tuple[tuple[str, int], ...] | None:
    """Read authenticated ``(path, capture_seq)`` evidence entries.

    ``()`` means no Skill has been read in this Run. ``None`` means a reserved
    evidence value was present but malformed, stale, or unauthenticated; callers
    must fail closed for that model/tool step.
    """

    if not isinstance(context, dict):
        return ()
    if VERIFIED_SKILL_SOURCE_CONTEXT_KEY not in context:
        return ()
    evidence = context.get(VERIFIED_SKILL_SOURCE_CONTEXT_KEY)
    identity = _run_identity(context)
    if (
        identity is None
        or not isinstance(owner_token, str)
        or not owner_token
        or not isinstance(evidence, dict)
        or type(evidence.get("version")) is not int
        or evidence["version"] != _EVIDENCE_VERSION
        or not isinstance(evidence.get("owner_token"), str)
        or not secrets.compare_digest(evidence["owner_token"], owner_token)
        or evidence.get("identity") != list(identity)
    ):
        return None
    return _validated_entries(evidence)


def read_verified_skill_source_paths(
    context: Any,
    *,
    owner_token: str,
    ttl_calls: int = 0,
) -> tuple[str, ...] | None:
    """Read authenticated exact-Run Skill paths that are still within TTL.

    ``ttl_calls`` bounds evidence age in lead model calls: an entry captured at
    call ``S`` is consumed only while ``current_seq - S < ttl_calls``. ``0``
    disables expiry, keeping evidence live for the whole Run. Expiry only ever
    widens back to the pre-activation default tool set; it never grants
    authority. ``()`` means no live evidence; ``None`` means malformed or
    unauthenticated evidence and callers must fail closed.
    """

    if type(ttl_calls) is not int or ttl_calls < 0:
        raise ValueError("ttl_calls must be a non-negative integer")
    entries = read_verified_skill_source_entries(context, owner_token=owner_token)
    if entries is None:
        return None
    if ttl_calls > 0:
        current_seq = read_lead_model_call_seq(context)
        entries = tuple(entry for entry in entries if current_seq - entry[1] < ttl_calls)
    return tuple(path for path, _ in entries)


def write_verified_skill_source_path(
    context: Any,
    path: str,
    *,
    owner_token: str,
) -> bool:
    """Record one successfully executed exact Skill read for the current Run.

    The entry captures the current lead model-call ordinal, so re-reading an
    already-active Skill refreshes its TTL window.
    """

    identity = _run_identity(context)
    if identity is None or not isinstance(context, dict) or not isinstance(path, str) or not path or posixpath.normpath(path) != path or not isinstance(owner_token, str) or not owner_token:
        return False

    existing = read_verified_skill_source_entries(
        context,
        owner_token=owner_token,
    )
    entries = [] if existing is None else [entry for entry in existing if entry[0] != path]
    entries.append((path, read_lead_model_call_seq(context)))
    entries = entries[-_MAX_ACTIVE_SKILLS:]
    context[VERIFIED_SKILL_SOURCE_CONTEXT_KEY] = {
        "version": _EVIDENCE_VERSION,
        "owner_token": owner_token,
        "identity": list(identity),
        "entries": [[entry_path, entry_seq] for entry_path, entry_seq in entries],
    }
    return True
