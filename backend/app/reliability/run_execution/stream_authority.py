"""Lease-authorized stream and event-store adapters."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from deerflow.runtime.events.models import (
    StoredStreamFrame,
    StreamFrame,
    StreamLeaseProof,
    StreamWriteAuthorizationRevoked,
    StreamWriteCancelled,
    StreamWriteLeaseLost,
)
from deerflow.runtime.private_scope import PrivateResourceScope
from deerflow.sandbox.sandbox import AuthorizationRevoked


class StreamExecutionBoundary(Protocol):
    def stream_lease_proof(self) -> StreamLeaseProof: ...

    def record_stream_authorization_revoked(self) -> None: ...

    def record_stream_lease_lost(self) -> None: ...

    def request_local_cancel(self) -> None: ...

    async def before_stream_publish(self) -> None: ...

    async def before_stream_terminal(self) -> None: ...

    async def stream_cleanup_allowed(self) -> bool: ...


class LeaseAuthorizedStreamBridge:
    """Guard every Worker-side stream mutation with current Run authority."""

    def __init__(
        self,
        bridge: Any,
        boundary: StreamExecutionBoundary,
        *,
        scope: PrivateResourceScope | None = None,
        thread_id: str | None = None,
        terminal_status: Callable[[], str] | None = None,
        terminal_error_code: Callable[[], str | None] | None = None,
    ) -> None:
        self._bridge = bridge
        self._boundary = boundary
        self._scope = scope
        self._thread_id = thread_id
        self._terminal_status = terminal_status
        self._terminal_error_code = terminal_error_code

    @property
    def supports_cross_process(self) -> bool:
        return bool(getattr(self._bridge, "supports_cross_process", False))

    async def publish(
        self,
        run_id: str,
        event: str,
        data: Any,
    ) -> None:
        publish_frame = getattr(self._bridge, "publish_frame", None)
        if callable(publish_frame) and self._scope is not None and self._thread_id is not None:
            try:
                await publish_frame(
                    self._scope,
                    self._thread_id,
                    run_id,
                    StreamFrame(event=event, data=data),
                    lease=self._boundary.stream_lease_proof(),
                )
            except StreamWriteAuthorizationRevoked:
                self._boundary.record_stream_authorization_revoked()
                raise AuthorizationRevoked from None
            except StreamWriteLeaseLost:
                self._boundary.record_stream_lease_lost()
                raise AuthorizationRevoked from None
            except StreamWriteCancelled:
                self._boundary.request_local_cancel()
                raise AuthorizationRevoked from None
            return
        await self._boundary.before_stream_publish()
        await self._bridge.publish(run_id, event, data)

    async def publish_end(self, run_id: str) -> None:
        publish_terminal = getattr(
            self._bridge,
            "publish_terminal",
            None,
        )
        if callable(publish_terminal) and self._scope is not None and self._thread_id is not None:
            try:
                stored = await publish_terminal(
                    self._scope,
                    self._thread_id,
                    run_id,
                    status=(self._terminal_status() if self._terminal_status is not None else "completed"),
                    error_code=(self._terminal_error_code() if self._terminal_error_code is not None else None),
                    lease=self._boundary.stream_lease_proof(),
                )
            except StreamWriteAuthorizationRevoked:
                self._boundary.record_stream_authorization_revoked()
                raise AuthorizationRevoked from None
            except StreamWriteLeaseLost:
                self._boundary.record_stream_lease_lost()
                raise AuthorizationRevoked from None
            if isinstance(stored, StoredStreamFrame) and isinstance(stored.data, Mapping) and stored.data.get("status") in {"cancelled", "interrupted"}:
                self._boundary.request_local_cancel()
            return
        await self._boundary.before_stream_terminal()
        await self._bridge.publish_end(run_id)

    def subscribe(self, *args, **kwargs):
        return self._bridge.subscribe(*args, **kwargs)

    async def cleanup(
        self,
        run_id: str,
        *,
        delay: float = 0,
    ) -> None:
        if delay > 0:
            await asyncio.sleep(delay)
        if await self._boundary.stream_cleanup_allowed():
            await self._bridge.cleanup(run_id, delay=0)


class LeaseAuthorizedRunEventStore:
    """Bind Worker journal events to the same atomic Job lease as SSE."""

    def __init__(
        self,
        store: Any,
        boundary: StreamExecutionBoundary,
        *,
        scope: PrivateResourceScope,
    ) -> None:
        self._store = store
        self._boundary = boundary
        self._scope = scope

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    async def put(self, **event: Any) -> dict:
        event.pop("scope", None)
        try:
            return await self._store.put(
                **event,
                scope=self._scope,
                lease=self._boundary.stream_lease_proof(),
            )
        except StreamWriteAuthorizationRevoked:
            self._boundary.record_stream_authorization_revoked()
            raise AuthorizationRevoked from None
        except StreamWriteLeaseLost:
            self._boundary.record_stream_lease_lost()
            raise AuthorizationRevoked from None
        except StreamWriteCancelled:
            self._boundary.request_local_cancel()
            raise AuthorizationRevoked from None

    async def put_batch(
        self,
        events: list[dict[str, Any]],
        *,
        scope: PrivateResourceScope | None = None,
    ) -> list[dict]:
        del scope
        try:
            return await self._store.put_batch(
                events,
                scope=self._scope,
                lease=self._boundary.stream_lease_proof(),
            )
        except StreamWriteAuthorizationRevoked:
            self._boundary.record_stream_authorization_revoked()
            raise AuthorizationRevoked from None
        except StreamWriteLeaseLost:
            self._boundary.record_stream_lease_lost()
            raise AuthorizationRevoked from None
        except StreamWriteCancelled:
            self._boundary.request_local_cancel()
            raise AuthorizationRevoked from None


__all__ = [
    "LeaseAuthorizedRunEventStore",
    "LeaseAuthorizedStreamBridge",
    "StreamExecutionBoundary",
]
