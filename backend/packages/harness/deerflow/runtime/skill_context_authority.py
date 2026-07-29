"""Run-scoped authority evidence for successfully read Skill entry files.

``ThreadState.skill_context`` is durable, user-observable conversation state.
It is useful as a reminder, but it is not an authorization source because a
private Run request can supply graph state.  This module keeps the separate
ephemeral proof used by the tool policy and secret-binding boundary.
"""

from __future__ import annotations

import posixpath
import secrets
from typing import Any

from deerflow.private_scope import PrivateResourceScope

VERIFIED_SKILL_SOURCE_CONTEXT_KEY = "__verified_skill_read_sources"

_EVIDENCE_VERSION = 1
_MAX_ACTIVE_SKILLS = 8

type _RunIdentity = tuple[str, str, str, str]


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


def read_verified_skill_source_paths(
    context: Any,
    *,
    owner_token: str,
) -> tuple[str, ...] | None:
    """Read authenticated exact-Run Skill paths.

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

    paths = evidence.get("paths")
    if not isinstance(paths, list) or not paths or len(paths) > _MAX_ACTIVE_SKILLS or not all(isinstance(path, str) and path and posixpath.normpath(path) == path for path in paths) or len(paths) != len(set(paths)):
        return None
    return tuple(paths)


def write_verified_skill_source_path(
    context: Any,
    path: str,
    *,
    owner_token: str,
) -> bool:
    """Record one successfully executed exact Skill read for the current Run."""

    identity = _run_identity(context)
    if identity is None or not isinstance(context, dict) or not isinstance(path, str) or not path or posixpath.normpath(path) != path or not isinstance(owner_token, str) or not owner_token:
        return False

    existing = read_verified_skill_source_paths(
        context,
        owner_token=owner_token,
    )
    paths = [] if existing is None else list(existing)
    if path in paths:
        paths.remove(path)
    paths.append(path)
    paths = paths[-_MAX_ACTIVE_SKILLS:]
    context[VERIFIED_SKILL_SOURCE_CONTEXT_KEY] = {
        "version": _EVIDENCE_VERSION,
        "owner_token": owner_token,
        "identity": list(identity),
        "paths": paths,
    }
    return True
