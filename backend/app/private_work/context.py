from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from app.private_work.errors import PrivateWorkNotFound
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from deerflow.runtime.private_scope import PrivateResourceScope

_CLIENT_AUTHORITY_FIELDS = frozenset(
    {
        "capability",
        "capabilities",
        "membership_id",
        "membership_version",
        "owner",
        "owner_id",
        "owner_user_id",
        "private_scope",
        "private_work_context",
        "project_context",
        "project_id",
        "project_role",
        "project_slug",
        "resource_scope",
        "role",
        "system_role",
        "user_id",
    }
)


def strip_private_client_fields(client_fields: Mapping[str, object]) -> dict[str, object]:
    """Drop client-controlled fields that could be mistaken for private authority."""

    return {key: value for key, value in client_fields.items() if isinstance(key, str) and key not in _CLIENT_AUTHORITY_FIELDS and not key.startswith("__")}


@dataclass(frozen=True, slots=True, init=False)
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
        return private_context

    @property
    def resource_scope(self) -> PrivateResourceScope:
        return PrivateResourceScope(
            project_id=str(self.project_id),
            owner_user_id=str(self.user_id),
            membership_version=self.membership_version,
        )
