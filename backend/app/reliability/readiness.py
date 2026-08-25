"""M6 reliability readiness service."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from sqlalchemy.ext.asyncio import AsyncSession

from app.final_schema import FinalSchemaProbe, FinalSchemaRequired, FinalSchemaUnavailable
from app.reliability.models import ReliabilityReadiness
from app.reliability.process_readiness import ProcessReadinessSnapshot


class ReliabilityReadinessService:
    """Report independent component state without leaking process internals."""

    def __init__(
        self,
        probe: FinalSchemaProbe,
        session: AsyncSession,
        request_id: str,
        *,
        worker_fleet: Callable[[], str] | None = None,
        scheduler: Callable[[], str] | None = None,
        stream: Callable[[], str] | None = None,
        quota: Callable[[], str] | None = None,
        audit: Callable[[], str] | None = None,
        process: ProcessReadinessSnapshot | None = None,
    ) -> None:
        self._probe = probe
        self._session = session
        self._request_id = request_id
        self._providers = {
            "worker_fleet": worker_fleet,
            "scheduler": scheduler,
            "stream": stream,
            "quota": quota,
            "audit": audit,
        }
        self._process = process
        from app.private_work.legacy_run_skill_snapshot_writer import (
            frozen_run_skill_snapshot_writer,
        )

        self._run_skill_writer = frozen_run_skill_snapshot_writer()

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
        process = self._process
        writer = self._run_skill_writer
        return ReliabilityReadiness(
            status="closed",
            database=database,
            schema=schema,
            worker_fleet=process.worker_fleet if process is not None else "closed",
            scheduler=process.scheduler if process is not None else "closed",
            stream="closed",
            quota="closed",
            audit="closed",
            request_id=request_id,
            role=process.role if process is not None else "gateway",
            worker_count=process.worker_count if process is not None else 0,
            worker_capacity=process.worker_capacity if process is not None else 0,
            worker_oldest_heartbeat_age_seconds=(process.worker_oldest_heartbeat_age_seconds if process is not None else None),
            private_run_worker_fleet=(process.private_run_worker_fleet if process is not None else "unavailable"),
            private_run_worker_count=(process.private_run_worker_count if process is not None else 0),
            private_run_worker_capacity=(process.private_run_worker_capacity if process is not None else 0),
            scheduler_ownership=(process.scheduler_ownership if process is not None else "unavailable"),
            schema_state=process.schema_state if process is not None else "unavailable",
            run_skill_writer_mode=writer.writer_mode,
            run_skill_writer_artifact_version=writer.artifact_version,
            run_skill_legacy_policy_digest=writer.legacy_policy_digest,
            run_skill_writer_ready=writer.ready,
        )

    async def read(self) -> ReliabilityReadiness:
        try:
            await self._probe.require_ready(self._session)
        except FinalSchemaRequired:
            return self._closed(
                database="ready",
                schema="unavailable",
                request_id=self._request_id,
            )
        except FinalSchemaUnavailable:
            return self._closed(
                database="unavailable",
                schema="unknown",
                request_id=self._request_id,
            )

        components = {name: self._component(name) for name in self._providers}
        process = self._process
        from app.private_work.run_skill_writer_cohort import (
            active_run_skill_writer_cohort_ready,
        )

        writer = replace(
            self._run_skill_writer,
            ready=await active_run_skill_writer_cohort_ready(
                self._session,
                self._run_skill_writer,
            ),
        )
        if process is not None:
            components["worker_fleet"] = process.worker_fleet
            components["scheduler"] = process.scheduler
        healthy = all(status == "ready" or (name == "scheduler" and status == "disabled") for name, status in components.items())
        if process is not None:
            healthy = healthy and process.private_run_worker_fleet == "ready"
        healthy = healthy and writer.ready
        return ReliabilityReadiness(
            status="ready" if healthy else "degraded",
            database="ready",
            schema="ready",
            request_id=self._request_id,
            role=process.role if process is not None else "gateway",
            worker_count=process.worker_count if process is not None else 0,
            worker_capacity=process.worker_capacity if process is not None else 0,
            worker_oldest_heartbeat_age_seconds=(process.worker_oldest_heartbeat_age_seconds if process is not None else None),
            private_run_worker_fleet=(process.private_run_worker_fleet if process is not None else "unavailable"),
            private_run_worker_count=(process.private_run_worker_count if process is not None else 0),
            private_run_worker_capacity=(process.private_run_worker_capacity if process is not None else 0),
            scheduler_ownership=(process.scheduler_ownership if process is not None else "unavailable"),
            schema_state=process.schema_state if process is not None else "ready",
            run_skill_writer_mode=writer.writer_mode,
            run_skill_writer_artifact_version=writer.artifact_version,
            run_skill_legacy_policy_digest=writer.legacy_policy_digest,
            run_skill_writer_ready=writer.ready,
            **components,
        )


__all__ = ["ReliabilityReadinessService"]
