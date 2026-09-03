"""Run Snapshot admission contracts: stale signal, secret DTOs, and admission ports."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.system_runtime_settings.models import LockedAgentRuntimePolicy


def agent_model_snapshot_purpose(definition_id: uuid.UUID) -> str:
    """Return the stable Run-model purpose for one delegated Agent Definition."""

    if not isinstance(definition_id, uuid.UUID):
        raise TypeError("Agent definition_id must be a UUID")
    return f"agent.{definition_id.hex}"


@dataclass(frozen=True, slots=True)
class RunMcpSecretSnapshot:
    mcp_server_id: uuid.UUID
    mcp_server_version_id: uuid.UUID
    slot_id: uuid.UUID
    secret_revision: int
    secret_generation_id: uuid.UUID
    secret_generation_digest: str


@dataclass(frozen=True, slots=True)
class RunSkillSecretSnapshot:
    skill_id: uuid.UUID
    skill_version_id: uuid.UUID
    secret_name: str
    secret_revision: int
    secret_generation_id: uuid.UUID
    secret_generation_digest: str


class RunSnapshotAssetStale(Exception):
    """Internal stale marker remapped at the request-context boundary."""


class AdmittedRunModelSnapshot(Protocol):
    """Minimum secret-free result required by Run admission."""

    model_ref: str
    provider_adapter: str
    provider_settings: Mapping[str, object]
    supports_thinking: bool
    supports_reasoning_effort: bool
    supports_vision: bool


class RunModelSnapshotAdmissionPort(Protocol):
    """Persist one exact database-backed model closure in the caller transaction."""

    async def admit_model_snapshot(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        thread_id: str,
        run_id: str,
        purpose: str,
        model_ref: str,
    ) -> AdmittedRunModelSnapshot: ...


class RunRuntimePolicyAdmissionPort(Protocol):
    """Lock and persist the exact agent runtime policy in the caller transaction."""

    async def lock_agent_runtime_for_admission(
        self,
        session: AsyncSession,
    ) -> LockedAgentRuntimePolicy: ...

    async def admit_run_snapshot(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        thread_id: str,
        run_id: str,
        locked_policy: LockedAgentRuntimePolicy | None = None,
    ) -> object: ...
