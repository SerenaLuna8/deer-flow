"""Abstract interface for thread metadata storage.

Production uses the PostgreSQL-backed ``ThreadMetaRepository``. The
``MemoryThreadMetaStore`` remains available as a lightweight test double.

All mutating and querying methods accept a ``user_id`` parameter with
three-state semantics (see :mod:`deerflow.runtime.user_context`):

- ``AUTO`` (default): resolve from the request-scoped contextvar.
- Explicit ``str``: use the provided value verbatim.
- Explicit ``None``: bypass owner filtering through the trusted legacy adapter
  (migration/CLI reads or repairs only). Final-schema creates still require a
  non-null owner plus explicit project and Agent authority.
"""

from __future__ import annotations

import abc
import uuid
from typing import Any

from deerflow.runtime.private_scope import PrivateResourceScope
from deerflow.runtime.user_context import AUTO, _AutoSentinel


class InvalidMetadataFilterError(ValueError):
    """Raised when all client-supplied metadata filter keys are rejected."""


class LegacyThreadCreateAuthorityUnavailable(RuntimeError):
    """Raised when a legacy create has no explicit final-schema authority."""


class ThreadMetaStore(abc.ABC):
    @abc.abstractmethod
    async def create(
        self,
        thread_id: str,
        *,
        assistant_id: str | None = None,
        user_id: str | None | _AutoSentinel = AUTO,
        display_name: str | None = None,
        metadata: dict | None = None,
        scope: PrivateResourceScope | None = None,
        agent_asset_id: uuid.UUID | None = None,
        agent_scope: str | None = None,
    ) -> dict:
        pass

    @abc.abstractmethod
    async def get(
        self,
        thread_id: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ) -> dict | None:
        pass

    @abc.abstractmethod
    async def search(
        self,
        *,
        metadata: dict[str, Any] | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ) -> list[dict[str, Any]]:
        pass

    @abc.abstractmethod
    async def update_display_name(
        self,
        thread_id: str,
        display_name: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ) -> None:
        pass

    @abc.abstractmethod
    async def update_status(
        self,
        thread_id: str,
        status: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ) -> None:
        pass

    @abc.abstractmethod
    async def update_metadata(
        self,
        thread_id: str,
        metadata: dict,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ) -> None:
        """Merge ``metadata`` into the thread's metadata field.

        Existing keys are overwritten by the new values; keys absent from
        ``metadata`` are preserved. No-op if the thread does not exist
        or the owner check fails.
        """
        pass

    @abc.abstractmethod
    async def update_owner(
        self,
        thread_id: str,
        owner_user_id: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ) -> None:
        """Move a thread metadata row to a new owner.

        Intended for trusted internal repair/migration paths. No-op if the
        row does not exist or the caller fails the owner check.
        """
        pass

    @abc.abstractmethod
    async def check_access(
        self,
        thread_id: str,
        user_id: str,
        *,
        require_existing: bool = False,
        scope: PrivateResourceScope | None = None,
    ) -> bool:
        """Check if ``user_id`` has access to ``thread_id``."""
        pass

    @abc.abstractmethod
    async def delete(
        self,
        thread_id: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ) -> None:
        pass

    @abc.abstractmethod
    async def mark_deleted(
        self,
        thread_id: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ) -> bool:
        """Persist an invisible checkpoint-cleanup tombstone."""
        pass

    @abc.abstractmethod
    async def set_checkpoint_delete_status(
        self,
        thread_id: str,
        status: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ) -> bool:
        """Update cleanup state on an already tombstoned thread."""
        pass


class TrustedUnscopedThreadMetaStore:
    """Explicit compatibility boundary for legacy, non-project thread routes."""

    def __init__(
        self,
        store: ThreadMetaStore,
        *,
        create_project_id: uuid.UUID | None = None,
        create_agent_asset_id: uuid.UUID | None = None,
        create_agent_scope: str | None = None,
        membership_version: int = 1,
    ) -> None:
        self._store = store
        self._create_project_id = create_project_id
        self._create_agent_asset_id = create_agent_asset_id
        self._create_agent_scope = create_agent_scope
        self._membership_version = membership_version

    async def create(self, *args: Any, **kwargs: Any) -> dict:
        from deerflow.runtime.user_context import resolve_user_id

        if self._create_project_id is None or self._create_agent_asset_id is None or self._create_agent_scope not in {"system", "project"}:
            raise LegacyThreadCreateAuthorityUnavailable("trusted legacy thread creation requires explicit final-schema authority")
        user_id = kwargs.get("user_id", AUTO)
        owner_user_id = resolve_user_id(
            user_id,
            method_name="TrustedUnscopedThreadMetaStore.create",
        )
        if owner_user_id is None:
            raise ValueError("final-schema threads require an owner")
        kwargs["user_id"] = owner_user_id
        kwargs["scope"] = PrivateResourceScope(
            project_id=str(self._create_project_id),
            owner_user_id=owner_user_id,
            membership_version=self._membership_version,
        )
        kwargs["agent_asset_id"] = self._create_agent_asset_id
        kwargs["agent_scope"] = self._create_agent_scope
        return await self._store.create(*args, **kwargs)

    async def get(self, *args: Any, **kwargs: Any) -> dict | None:
        return await self._store.get(*args, **kwargs)

    async def search(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return await self._store.search(*args, **kwargs)

    async def update_display_name(self, *args: Any, **kwargs: Any) -> None:
        await self._store.update_display_name(*args, **kwargs)

    async def update_status(self, *args: Any, **kwargs: Any) -> None:
        await self._store.update_status(*args, **kwargs)

    async def update_metadata(self, *args: Any, **kwargs: Any) -> None:
        await self._store.update_metadata(*args, **kwargs)

    async def update_owner(self, *args: Any, **kwargs: Any) -> None:
        await self._store.update_owner(*args, **kwargs)

    async def check_access(self, *args: Any, **kwargs: Any) -> bool:
        return await self._store.check_access(*args, **kwargs)

    async def delete(self, *args: Any, **kwargs: Any) -> None:
        await self._store.delete(*args, **kwargs)

    async def mark_deleted(self, *args: Any, **kwargs: Any) -> bool:
        return await self._store.mark_deleted(*args, **kwargs)

    async def set_checkpoint_delete_status(self, *args: Any, **kwargs: Any) -> bool:
        return await self._store.set_checkpoint_delete_status(*args, **kwargs)
