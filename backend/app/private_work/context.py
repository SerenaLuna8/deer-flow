from __future__ import annotations

import uuid
import weakref
from collections.abc import Mapping
from dataclasses import dataclass
from threading import Lock
from typing import TypeGuard

from app.private_work.errors import PrivateWorkNotFound
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from deerflow.runtime.private_scope import PrivateResourceScope

_CLIENT_AUTHORITY_FIELDS = frozenset(
    {
        "capability",
        "capabilities",
        "agent",
        "agent_asset_id",
        "agent_id",
        "agent_name",
        "assistant_id",
        "asset_context",
        "available_skills",
        "membership_id",
        "membership_version",
        "mcp_servers",
        "mcps",
        "model",
        "model_name",
        "owner",
        "owner_id",
        "owner_user_id",
        "private_scope",
        "private_resource_scope",
        "private_work_context",
        "project_context",
        "project_id",
        "project_role",
        "project_slug",
        "resource_scope",
        "role",
        "skill_ids",
        "skills",
        "system_role",
        "tool_groups",
        "trusted_asset_context",
        "user_id",
        "user_role",
    }
)


def strip_private_client_fields(
    client_fields: Mapping[str, object],
    *,
    preserve_fields: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """Recursively drop client fields that could be mistaken for authority."""

    return {
        key: _strip_private_client_value(value, preserve_fields=preserve_fields)
        for key, value in client_fields.items()
        if isinstance(key, str) and (key not in _CLIENT_AUTHORITY_FIELDS or key in preserve_fields) and not key.startswith("__")
    }


def _strip_private_client_value(
    value: object,
    *,
    preserve_fields: frozenset[str],
) -> object:
    if isinstance(value, Mapping):
        return strip_private_client_fields(value, preserve_fields=preserve_fields)
    if isinstance(value, list):
        return [_strip_private_client_value(item, preserve_fields=preserve_fields) for item in value]
    if isinstance(value, tuple):
        return tuple(_strip_private_client_value(item, preserve_fields=preserve_fields) for item in value)
    return value


_CLONE_ERROR = "PrivateWorkContext cannot be cloned or serialized"


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class PrivateWorkContext:
    user_id: uuid.UUID
    project_id: uuid.UUID
    membership_id: uuid.UUID
    role: ProjectRole
    capabilities: frozenset[Capability]
    membership_version: int
    request_id: str

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("PrivateWorkContext must be derived from ProjectContext")

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("PrivateWorkContext cannot be subclassed")

    def __copy__(self) -> PrivateWorkContext:
        raise TypeError(_CLONE_ERROR)

    def __deepcopy__(self, memo: dict[int, object]) -> PrivateWorkContext:
        del memo
        raise TypeError(_CLONE_ERROR)

    def __getstate__(self) -> object:
        raise TypeError(_CLONE_ERROR)

    def __setstate__(self, state: object) -> None:
        del state
        raise TypeError(_CLONE_ERROR)

    def __reduce__(self) -> object:
        raise TypeError(_CLONE_ERROR)

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError(_CLONE_ERROR)

    @classmethod
    def from_project(cls, context: ProjectContext) -> PrivateWorkContext:
        if cls is not PrivateWorkContext:
            raise TypeError("PrivateWorkContext cannot be subclassed")
        if type(context) is not ProjectContext:
            request_id = getattr(context, "request_id", "unknown")
            if not isinstance(request_id, str) or not request_id:
                request_id = "unknown"
            raise PrivateWorkNotFound(request_id)
        private_context = object.__new__(PrivateWorkContext)
        object.__setattr__(private_context, "user_id", context.user_id)
        object.__setattr__(private_context, "project_id", context.project_id)
        object.__setattr__(private_context, "membership_id", context.membership_id)
        object.__setattr__(private_context, "role", context.role)
        object.__setattr__(private_context, "capabilities", context.capabilities)
        object.__setattr__(private_context, "membership_version", context.membership_version)
        object.__setattr__(private_context, "request_id", context.request_id)
        _register_issued_context(private_context)
        return private_context

    @property
    def resource_scope(self) -> PrivateResourceScope:
        require_issued_private_work_context(self)
        return PrivateResourceScope(
            project_id=str(self.project_id),
            owner_user_id=str(self.user_id),
            membership_version=self.membership_version,
        )


_ContextSnapshot = tuple[
    uuid.UUID,
    uuid.UUID,
    uuid.UUID,
    ProjectRole,
    frozenset[Capability],
    int,
    str,
]
_ISSUED_CONTEXTS: dict[int, tuple[weakref.ReferenceType[PrivateWorkContext], _ContextSnapshot]] = {}
_ISSUED_CONTEXTS_LOCK = Lock()


def _context_snapshot(context: PrivateWorkContext) -> _ContextSnapshot:
    return (
        context.user_id,
        context.project_id,
        context.membership_id,
        context.role,
        context.capabilities,
        context.membership_version,
        context.request_id,
    )


def _register_issued_context(context: PrivateWorkContext) -> None:
    identity = id(context)

    def discard(reference: weakref.ReferenceType[PrivateWorkContext]) -> None:
        with _ISSUED_CONTEXTS_LOCK:
            current = _ISSUED_CONTEXTS.get(identity)
            if current is not None and current[0] is reference:
                del _ISSUED_CONTEXTS[identity]

    reference = weakref.ref(context, discard)
    with _ISSUED_CONTEXTS_LOCK:
        _ISSUED_CONTEXTS[identity] = (reference, _context_snapshot(context))


def is_issued_private_work_context(context: object) -> TypeGuard[PrivateWorkContext]:
    """Return whether context is the unchanged, same-process object issued by the factory."""

    if type(context) is not PrivateWorkContext:
        return False
    with _ISSUED_CONTEXTS_LOCK:
        issued = _ISSUED_CONTEXTS.get(id(context))
    if issued is None or issued[0]() is not context:
        return False
    try:
        return _context_snapshot(context) == issued[1]
    except AttributeError:
        return False


def require_issued_private_work_context(context: object) -> PrivateWorkContext:
    """Fail closed without reading authority from an unissued or modified context."""

    if not is_issued_private_work_context(context):
        raise PrivateWorkNotFound("unknown")
    return context
