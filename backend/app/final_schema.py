"""Marker-free final PostgreSQL schema readiness contract."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.bootstrap import CURRENT_SCHEMA_REVISION
from deerflow.persistence.final_schema_contract import FINAL_APP_TABLES

SCHEMA_V1_REVISION = CURRENT_SCHEMA_REVISION

# The ORM-backed Schema V1 catalog is the sole application relation authority.
FINAL_REQUIRED_RELATIONS = tuple(sorted(FINAL_APP_TABLES))


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
        accepted_revisions: tuple[str, ...] = (SCHEMA_V1_REVISION,),
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
    "SCHEMA_V1_REVISION",
    "FinalSchemaError",
    "FinalSchemaProbe",
    "FinalSchemaRequired",
    "FinalSchemaState",
    "FinalSchemaUnavailable",
]
