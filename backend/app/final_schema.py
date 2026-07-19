"""Marker-free final PostgreSQL schema readiness contract."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

M7_FINAL_SCHEMA_REVISION = "0001_project_saas_baseline"

FINAL_REQUIRED_RELATIONS = (
    "projects",
    "project_memberships",
    "agents",
    "skills",
    "mcp_servers",
    "threads_meta",
    "runs",
    "scheduled_tasks",
    "scheduled_task_runs",
    "jobs",
    "run_events",
    "project_usage_ledger",
    "audit_logs",
    "deletion_tombstones",
    "restore_proofs",
)


@dataclass(frozen=True, slots=True)
class FinalSchemaState:
    revision: str | None
    missing_relations: tuple[str, ...]
    ready: bool


class FinalSchemaError(Exception):
    """Base error with no database or private payload."""


class FinalSchemaRequired(FinalSchemaError):
    def __init__(self, state: FinalSchemaState) -> None:
        self.state = state
        super().__init__("The final database schema is required.")


class FinalSchemaUnavailable(FinalSchemaError):
    def __init__(self) -> None:
        super().__init__("Final schema readiness is unavailable.")


class FinalSchemaProbe:
    def __init__(
        self,
        *,
        accepted_revisions: tuple[str, ...] = (M7_FINAL_SCHEMA_REVISION,),
        required_relations: tuple[str, ...] = FINAL_REQUIRED_RELATIONS,
    ) -> None:
        self._accepted_revisions = accepted_revisions
        self._required_relations = required_relations

    async def read(self, session: AsyncSession) -> FinalSchemaState:
        revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        present = await session.scalar(
            text(
                """SELECT array_agg(name ORDER BY name)
                   FROM unnest(CAST(:names AS text[])) name
                   WHERE to_regclass(name) IS NOT NULL"""
            ),
            {"names": list(self._required_relations)},
        )
        present_set = set(self._required_relations if present is True else present or ())
        missing = tuple(name for name in self._required_relations if name not in present_set)
        normalized_revision = None if revision is None else str(revision)
        return FinalSchemaState(
            revision=normalized_revision,
            missing_relations=missing,
            ready=normalized_revision in self._accepted_revisions and not missing,
        )

    async def require_ready(self, session: AsyncSession) -> FinalSchemaState:
        try:
            state = await self.read(session)
        except SQLAlchemyError:
            raise FinalSchemaUnavailable() from None
        if not state.ready:
            raise FinalSchemaRequired(state)
        return state


__all__ = [
    "FINAL_REQUIRED_RELATIONS",
    "M7_FINAL_SCHEMA_REVISION",
    "FinalSchemaError",
    "FinalSchemaProbe",
    "FinalSchemaRequired",
    "FinalSchemaState",
    "FinalSchemaUnavailable",
]
