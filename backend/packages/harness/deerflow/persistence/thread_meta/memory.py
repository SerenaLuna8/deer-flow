"""In-memory ThreadMetaStore backed by LangGraph BaseStore.

Used as a lightweight test double. Delegates to the LangGraph Store's
``("threads",)`` namespace — the same namespace used by the Gateway
router for thread records.
"""

from __future__ import annotations

import uuid
from typing import Any

from langgraph.store.base import BaseStore

from deerflow.persistence.thread_meta.base import ThreadMetaStore
from deerflow.runtime.private_scope import PrivateResourceScope
from deerflow.runtime.user_context import AUTO, _AutoSentinel, resolve_user_id
from deerflow.utils.time import coerce_iso, now_iso

THREADS_NS: tuple[str, ...] = ("threads",)


class MemoryThreadMetaStore(ThreadMetaStore):
    def __init__(self, store: BaseStore) -> None:
        self._store = store

    async def _get_owned_record(
        self,
        thread_id: str,
        user_id: str | None | _AutoSentinel,
        method_name: str,
    ) -> dict | None:
        """Fetch a record and verify ownership. Returns a mutable copy, or None."""
        resolved = resolve_user_id(user_id, method_name=method_name)
        item = await self._store.aget(THREADS_NS, thread_id)
        if item is None:
            return None
        record = dict(item.value)
        if resolved is not None and record.get("user_id") != resolved:
            return None
        return record

    @staticmethod
    def _matches_scope(
        record: dict,
        scope: PrivateResourceScope | None,
    ) -> bool:
        return scope is None or (record.get("project_id") == scope.project_id and record.get("user_id") == scope.owner_user_id)

    @staticmethod
    def _is_active_scope_record(
        record: dict,
        scope: PrivateResourceScope | None,
    ) -> bool:
        return record.get("deleted_at") is None and record.get("frozen_at") is None

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
        resolved_user_id = scope.owner_user_id if scope is not None else resolve_user_id(user_id, method_name="MemoryThreadMetaStore.create")
        now = now_iso()
        record: dict[str, Any] = {
            "thread_id": thread_id,
            "assistant_id": assistant_id,
            "user_id": resolved_user_id,
            "display_name": display_name,
            "status": "idle",
            "metadata": metadata or {},
            "values": {},
            "created_at": now,
            "updated_at": now,
            "project_id": None if scope is None else scope.project_id,
            "agent_asset_id": None if agent_asset_id is None else str(agent_asset_id),
            "agent_scope": agent_scope,
            "frozen_at": None,
            "deleted_at": None,
            "checkpoint_delete_status": "not_requested",
            "version": 1,
        }
        await self._store.aput(THREADS_NS, thread_id, record)
        return record

    async def get(
        self,
        thread_id: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ) -> dict | None:
        record = await self._get_owned_record(
            thread_id,
            scope.owner_user_id if scope is not None else user_id,
            "MemoryThreadMetaStore.get",
        )
        if record is None or not self._matches_scope(record, scope) or not self._is_active_scope_record(record, scope):
            return None
        return record

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
        resolved_user_id = None if scope is not None else resolve_user_id(user_id, method_name="MemoryThreadMetaStore.search")
        filter_dict: dict[str, Any] = {}
        if metadata:
            filter_dict.update(metadata)
        if status:
            filter_dict["status"] = status
        if resolved_user_id is not None:
            filter_dict["user_id"] = resolved_user_id
        if scope is not None:
            filter_dict["project_id"] = scope.project_id
            filter_dict["user_id"] = scope.owner_user_id
            filter_dict["deleted_at"] = None
            filter_dict["frozen_at"] = None

        items = await self._store.asearch(
            THREADS_NS,
            filter=filter_dict or None,
            limit=limit,
            offset=offset,
        )
        return [self._item_to_dict(item) for item in items if self._is_active_scope_record(item.value, scope)]

    async def check_access(
        self,
        thread_id: str,
        user_id: str,
        *,
        require_existing: bool = False,
        scope: PrivateResourceScope | None = None,
    ) -> bool:
        item = await self._store.aget(THREADS_NS, thread_id)
        if item is None:
            return False if scope is not None else not require_existing
        if not self._is_active_scope_record(item.value, scope):
            return False
        record_user_id = item.value.get("user_id")
        if scope is not None:
            return record_user_id == user_id == scope.owner_user_id and item.value.get("project_id") == scope.project_id and self._is_active_scope_record(item.value, scope)
        if record_user_id is None:
            return True
        return record_user_id == user_id

    async def update_display_name(
        self,
        thread_id: str,
        display_name: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ) -> None:
        record = await self._get_owned_record(
            thread_id,
            scope.owner_user_id if scope is not None else user_id,
            "MemoryThreadMetaStore.update_display_name",
        )
        if record is None or not self._matches_scope(record, scope) or not self._is_active_scope_record(record, scope):
            return
        record["display_name"] = display_name
        record["updated_at"] = now_iso()
        if scope is not None:
            record["version"] = int(record.get("version", 1)) + 1
        await self._store.aput(THREADS_NS, thread_id, record)

    async def update_status(
        self,
        thread_id: str,
        status: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ) -> None:
        record = await self._get_owned_record(
            thread_id,
            scope.owner_user_id if scope is not None else user_id,
            "MemoryThreadMetaStore.update_status",
        )
        if record is None or not self._matches_scope(record, scope) or not self._is_active_scope_record(record, scope):
            return
        record["status"] = status
        record["updated_at"] = now_iso()
        if scope is not None:
            record["version"] = int(record.get("version", 1)) + 1
        await self._store.aput(THREADS_NS, thread_id, record)

    async def update_metadata(
        self,
        thread_id: str,
        metadata: dict,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ) -> None:
        record = await self._get_owned_record(
            thread_id,
            scope.owner_user_id if scope is not None else user_id,
            "MemoryThreadMetaStore.update_metadata",
        )
        if record is None or not self._matches_scope(record, scope) or not self._is_active_scope_record(record, scope):
            return
        merged = dict(record.get("metadata") or {})
        merged.update(metadata)
        record["metadata"] = merged
        record["updated_at"] = now_iso()
        if scope is not None:
            record["version"] = int(record.get("version", 1)) + 1
        await self._store.aput(THREADS_NS, thread_id, record)

    async def update_owner(
        self,
        thread_id: str,
        owner_user_id: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ) -> None:
        if scope is not None:
            raise RuntimeError("scoped project thread ownership is immutable")
        record = await self._get_owned_record(thread_id, user_id, "MemoryThreadMetaStore.update_owner")
        if record is None:
            return
        record["user_id"] = owner_user_id
        record["updated_at"] = now_iso()
        await self._store.aput(THREADS_NS, thread_id, record)

    async def delete(
        self,
        thread_id: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ) -> None:
        record = await self._get_owned_record(
            thread_id,
            scope.owner_user_id if scope is not None else user_id,
            "MemoryThreadMetaStore.delete",
        )
        if record is None or not self._matches_scope(record, scope) or not self._is_active_scope_record(record, scope):
            return
        await self._store.adelete(THREADS_NS, thread_id)

    async def mark_deleted(
        self,
        thread_id: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ) -> bool:
        record = await self._get_owned_record(
            thread_id,
            scope.owner_user_id if scope is not None else user_id,
            "MemoryThreadMetaStore.mark_deleted",
        )
        if record is None or not self._matches_scope(record, scope) or not self._is_active_scope_record(record, scope):
            return False
        now = now_iso()
        record["deleted_at"] = now
        record["checkpoint_delete_status"] = "pending"
        record["updated_at"] = now
        record["version"] = int(record.get("version", 1)) + 1
        await self._store.aput(THREADS_NS, thread_id, record)
        return True

    async def set_checkpoint_delete_status(
        self,
        thread_id: str,
        status: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ) -> bool:
        if status not in {"pending", "complete", "retry_required"}:
            raise ValueError("invalid checkpoint delete status")
        record = await self._get_owned_record(
            thread_id,
            scope.owner_user_id if scope is not None else user_id,
            "MemoryThreadMetaStore.set_checkpoint_delete_status",
        )
        if record is None or record.get("deleted_at") is None or not self._matches_scope(record, scope):
            return False
        record["checkpoint_delete_status"] = status
        record["updated_at"] = now_iso()
        await self._store.aput(THREADS_NS, thread_id, record)
        return True

    @staticmethod
    def _item_to_dict(item) -> dict[str, Any]:
        """Convert a Store SearchItem to the dict format expected by callers."""
        val = item.value
        return {
            "thread_id": item.key,
            "assistant_id": val.get("assistant_id"),
            "user_id": val.get("user_id"),
            "display_name": val.get("display_name"),
            "status": val.get("status", "idle"),
            "metadata": val.get("metadata", {}),
            "project_id": val.get("project_id"),
            "agent_asset_id": val.get("agent_asset_id"),
            "agent_scope": val.get("agent_scope"),
            "frozen_at": val.get("frozen_at"),
            "deleted_at": val.get("deleted_at"),
            "checkpoint_delete_status": val.get(
                "checkpoint_delete_status",
                "not_requested",
            ),
            "version": val.get("version", 1),
            # ``coerce_iso`` heals legacy unix-second values written by
            # earlier Gateway versions that called ``str(time.time())``.
            "created_at": coerce_iso(val.get("created_at", "")),
            "updated_at": coerce_iso(val.get("updated_at", "")),
        }
