"""M6 reliability readiness service."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from app.reliability.errors import ReliabilityCutover, ReliabilityDatabaseUnavailable
from app.reliability.models import ReliabilityReadiness
from app.reliability.process_readiness import ProcessReadinessSnapshot


class _GatewayGuard(Protocol):
    @property
    def request_id(self) -> str: ...

    async def require_gateway_open(self) -> None: ...


class ReliabilityReadinessService:
    """Report independent component state without leaking process internals."""

    def __init__(
        self,
        guard: _GatewayGuard,
        *,
        worker_fleet: Callable[[], str] | None = None,
        scheduler: Callable[[], str] | None = None,
        stream: Callable[[], str] | None = None,
        recovery: Callable[[], str] | None = None,
        quota: Callable[[], str] | None = None,
        audit: Callable[[], str] | None = None,
        process: ProcessReadinessSnapshot | None = None,
    ) -> None:
        self._guard = guard
        self._providers = {
            "worker_fleet": worker_fleet,
            "scheduler": scheduler,
            "stream": stream,
            "recovery": recovery,
            "quota": quota,
            "audit": audit,
        }
        self._process = process

    def _component(self, name: str) -> str:
        provider = self._providers[name]
        if provider is not None:
            return provider()
        return "disabled" if name == "scheduler" else "unavailable"

    def _closed(
        self,
        *,
        database: str,
        schema: str,
        request_id: str,
    ) -> ReliabilityReadiness:
        return ReliabilityReadiness(
            status="closed",
            database=database,
            schema=schema,
            worker_fleet="closed",
            scheduler="closed",
            stream="closed",
            recovery="closed",
            quota="closed",
            audit="closed",
            request_id=request_id,
        )

    async def read(self) -> ReliabilityReadiness:
        try:
            await self._guard.require_gateway_open()
        except ReliabilityCutover as error:
            return self._closed(
                database="ready",
                schema="migration_required",
                request_id=error.request_id,
            )
        except ReliabilityDatabaseUnavailable as error:
            return self._closed(
                database="unavailable",
                schema="unknown",
                request_id=error.request_id,
            )

        components = {name: self._component(name) for name in self._providers}
        process = self._process
        if process is not None:
            components["worker_fleet"] = process.worker_fleet
            components["scheduler"] = process.scheduler
        healthy = all(status == "ready" or (name == "scheduler" and status == "disabled") for name, status in components.items())
        return ReliabilityReadiness(
            status="ready" if healthy else "degraded",
            database="ready",
            schema="ready",
            request_id=self._guard.request_id,
            role=process.role if process is not None else "gateway",
            worker_count=process.worker_count if process is not None else 0,
            worker_capacity=process.worker_capacity if process is not None else 0,
            worker_oldest_heartbeat_age_seconds=(process.worker_oldest_heartbeat_age_seconds if process is not None else None),
            scheduler_ownership=(process.scheduler_ownership if process is not None else "unavailable"),
            cutover=process.cutover if process is not None else "ready",
            **components,
        )


__all__ = ["ReliabilityReadinessService"]
