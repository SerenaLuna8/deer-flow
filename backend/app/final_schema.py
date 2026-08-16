"""Marker-free final PostgreSQL schema readiness contract."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.bootstrap import CURRENT_SCHEMA_REVISION

M7_FINAL_SCHEMA_REVISION = CURRENT_SCHEMA_REVISION

FINAL_REQUIRED_RELATIONS = (
    "projects",
    "project_memberships",
    "project_default_agents",
    "agents",
    "skills",
    "skill_versions",
    "skill_design_sessions",
    "skill_design_operations",
    "skill_design_draft_files",
    "project_skill_credential_configs",
    "project_skill_credential_bindings",
    "project_channel_instances",
    "project_channel_credential_bindings",
    "project_channel_instance_leases",
    "project_channel_group_binding_challenges",
    "project_channel_group_bindings",
    "channel_external_principals",
    "run_skill_credential_snapshots",
    "mcp_servers",
    "mcp_tool_discovery_attempts",
    "project_mcp_tool_inventories",
    "threads_meta",
    "runs",
    "scheduled_tasks",
    "scheduled_task_runs",
    "jobs",
    "memory_history_entries",
    "memory_documents",
    "memory_dream_runs",
    "memory_dream_prepare_runs",
    "memory_document_versions",
    "memory_episodes",
    "run_memory_context_snapshots",
    "run_event_invariants",
    "run_event_partition_state",
    "run_events",
    "execution_approval_output_delivery_obligations",
    "execution_approval_output_delivery_candidates",
    "project_usage_ledger",
    "audit_logs",
    "system_runtime_policy_catalog_state",
    "system_runtime_policies",
    "system_runtime_policy_versions",
    "run_runtime_policy_snapshots",
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
