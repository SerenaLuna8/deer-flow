"""Workflow runtime policy identity, durable convergence and safe readiness.

The PostgreSQL ``workflow_runtime`` value is product-policy authority. Worker
policy identity is durable, exact and content-free; it is never inferred from
capabilities, versions or execution profiles. Likewise,
``worker_nodes.runtime_profile_digests_json`` remains a set of real execution-
profile digests and is deliberately not reused for policy identity.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Annotated, Final, Literal

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.final_schema import M7_FINAL_SCHEMA_REVISION
from app.system_runtime_settings.errors import SystemRuntimePolicyUnavailable
from app.system_runtime_settings.models import LockedWorkflowRuntimePolicy
from app.workflows.runtime_policy import (
    WorkflowRuntimeAdminPolicyV1,
    WorkflowRuntimeDisabledV1,
    WorkflowRuntimeEffectivePolicyV1,
    WorkflowRuntimePendingV1,
    WorkflowRuntimePolicyV1,
    WorkflowRuntimeReadyV1,
    create_workflow_runtime_admin_policy,
    create_workflow_runtime_stored_policy,
)
from deerflow.persistence.jobs import WorkerNodeRow
from deerflow.workflows.compiler import (
    CURRENT_COMPILER_CONTRACT_VERSION,
    GRAPH_SCHEMA_VERSION_V1,
)

type WorkflowRuntimeRole = Literal["gateway", "worker", "scheduler"]
_WORKER_FRESH_FOR = timedelta(seconds=90)

# G32 owns installation of the one real Workflow Job handler.  Until that
# implementation exists, neither exact durable identity nor a forged/stale
# ``worker_nodes`` row may make Workflow admission executable.
# This is deliberately a code capability, not a setting or deployment flag.
WORKFLOW_RUN_HANDLER_INSTALLED: Final[Literal[False]] = False

# G40/G43 own the production node execution adapters.  A policy-selected
# profile and a forged Worker row cannot stand in for those code boundaries.
WORKFLOW_CODE_EXECUTION_HANDLER_INSTALLED: Final[Literal[False]] = False
WORKFLOW_HTTP_EXECUTION_HANDLER_INSTALLED: Final[Literal[False]] = False


def workflow_generic_runtime_profile_digest_v1() -> str:
    """Return the exact generic Workflow executor contract digest.

    This is deployment capability metadata, not product policy.  It binds the
    current database schema, graph/compiler contracts, durable Job type and
    real handler contract into one opaque Worker-advertised digest.
    """

    payload = json.dumps(
        [
            "actweave.workflow.generic-runtime-profile.v1",
            M7_FINAL_SCHEMA_REVISION,
            GRAPH_SCHEMA_VERSION_V1,
            CURRENT_COMPILER_CONTRACT_VERSION,
            "workflow_run",
            "workflow-run-handler-v1",
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


WORKFLOW_GENERIC_RUNTIME_PROFILE_DIGEST_V1: Final = workflow_generic_runtime_profile_digest_v1()
_WORKFLOW_UNPROFILED_JOB_SENTINEL_DIGEST: Final = "0" * 64


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
    )


class WorkflowRuntimeMaterializedIdentity(_StrictModel):
    """Exact, secret-free identity of one validated immutable policy version."""

    policy_version_id: uuid.UUID
    revision: Annotated[StrictInt, Field(ge=1, le=9_007_199_254_740_991)]
    schema_version: Literal[1]
    payload_checksum: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @classmethod
    def from_locked(
        cls,
        policy: LockedWorkflowRuntimePolicy,
    ) -> WorkflowRuntimeMaterializedIdentity:
        if type(policy) is not LockedWorkflowRuntimePolicy:
            raise TypeError("locked Workflow runtime policy is required")
        return cls(
            policy_version_id=policy.policy_version_id,
            revision=policy.revision,
            schema_version=policy.schema_version,
            payload_checksum=policy.payload_checksum,
        )


def _facet_generation(
    *,
    generic_ready: bool,
    code_ready: bool,
    http_ready: bool,
) -> str:
    payload = json.dumps(
        [
            "actweave.workflow.runtime-facets.v1",
            generic_ready,
            code_ready,
            http_ready,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


class WorkflowRuntimeFacetReadinessV1(_StrictModel):
    """Safe dynamic execution-facet projection.

    It deliberately contains no Worker/profile/policy identifiers, heartbeat,
    provider name or infrastructure locator.  Code and HTTP are independent
    facets and neither can make the generic executor unavailable.
    """

    generic_ready: StrictBool
    code_ready: StrictBool
    http_ready: StrictBool
    generation: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @model_validator(mode="after")
    def validate_generation(self) -> WorkflowRuntimeFacetReadinessV1:
        if not self.generic_ready and (self.code_ready or self.http_ready):
            raise ValueError("specialized Workflow runtime facets require generic readiness")
        expected = _facet_generation(
            generic_ready=self.generic_ready,
            code_ready=self.code_ready,
            http_ready=self.http_ready,
        )
        if self.generation != expected:
            raise ValueError("Workflow runtime facet generation does not match its safe flags")
        return self


def create_workflow_runtime_facet_readiness(
    *,
    generic_ready: bool,
    code_ready: bool,
    http_ready: bool,
) -> WorkflowRuntimeFacetReadinessV1:
    if any(type(value) is not bool for value in (generic_ready, code_ready, http_ready)):
        raise TypeError("Workflow runtime facet readiness requires real booleans")
    return WorkflowRuntimeFacetReadinessV1(
        generic_ready=generic_ready,
        code_ready=code_ready,
        http_ready=http_ready,
        generation=_facet_generation(
            generic_ready=generic_ready,
            code_ready=code_ready,
            http_ready=http_ready,
        ),
    )


class WorkflowRuntimeConvergence:
    """Exact policy-identity and independent Worker-profile convergence."""

    def __init__(
        self,
        *,
        worker_fresh_for: timedelta = _WORKER_FRESH_FOR,
        code_profile_digest_resolver: Callable[[WorkflowRuntimePolicyV1], str | None] | None = None,
        http_profile_digest_resolver: Callable[[WorkflowRuntimePolicyV1], str | None] | None = None,
    ) -> None:
        if not isinstance(worker_fresh_for, timedelta) or not timedelta(seconds=1) <= worker_fresh_for <= timedelta(hours=1):
            raise ValueError("Workflow Worker freshness must be between one second and one hour")
        self._worker_fresh_for = worker_fresh_for
        self._code_profile_digest_resolver = code_profile_digest_resolver
        self._http_profile_digest_resolver = http_profile_digest_resolver

    @staticmethod
    def _validated_profile_digest(value: object) -> str | None:
        if type(value) is not str or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            return None
        return value

    @classmethod
    def _safe_profile_digest(
        cls,
        resolver: Callable[[WorkflowRuntimePolicyV1], str | None] | None,
        policy: WorkflowRuntimePolicyV1,
    ) -> str | None:
        if resolver is None:
            return None
        try:
            value = resolver(policy)
        except Exception:
            # Deployment-profile resolution is an optional readiness input.
            # Ordinary resolver failures close only that facet; cancellation
            # and process-control BaseExceptions still propagate.
            return None
        digest = cls._validated_profile_digest(value)
        if digest in {
            WORKFLOW_GENERIC_RUNTIME_PROFILE_DIGEST_V1,
            _WORKFLOW_UNPROFILED_JOB_SENTINEL_DIGEST,
        }:
            return None
        return digest

    async def _fresh_exact_worker_profiles(
        self,
        session: AsyncSession,
        *,
        desired: WorkflowRuntimeMaterializedIdentity,
    ) -> tuple[frozenset[str], ...]:
        database_clock = sa.func.statement_timestamp()
        rows = (
            await session.execute(
                sa.select(
                    WorkerNodeRow.id,
                    WorkerNodeRow.capabilities_json,
                    WorkerNodeRow.runtime_profile_digests_json,
                ).where(
                    WorkerNodeRow.workflow_runtime_policy_section == "workflow_runtime",
                    WorkerNodeRow.workflow_runtime_policy_version_id == desired.policy_version_id,
                    WorkerNodeRow.workflow_runtime_policy_revision == desired.revision,
                    WorkerNodeRow.workflow_runtime_policy_schema_version == desired.schema_version,
                    WorkerNodeRow.workflow_runtime_policy_checksum == desired.payload_checksum,
                    WorkerNodeRow.draining.is_(False),
                    WorkerNodeRow.heartbeat_at >= database_clock - self._worker_fresh_for,
                    WorkerNodeRow.heartbeat_at <= database_clock,
                )
            )
        ).all()
        candidates: list[frozenset[str]] = []
        for _worker_id, capabilities, profiles in rows:
            if type(capabilities) is not list or any(type(item) is not str for item in capabilities) or "workflow_run" not in capabilities:
                continue
            if type(profiles) is not list:
                continue
            validated = tuple(self._validated_profile_digest(item) for item in profiles)
            if any(item is None for item in validated) or len(validated) != len(set(validated)):
                continue
            candidates.append(frozenset(item for item in validated if item is not None))
        return tuple(candidates)

    async def read_facets_in_session(
        self,
        session: AsyncSession,
        locked: LockedWorkflowRuntimePolicy,
    ) -> WorkflowRuntimeFacetReadinessV1:
        """Resolve generic, Code and HTTP readiness independently.

        One query reads the exact-policy, PostgreSQL-clock-fresh candidate set.
        Each specialized Worker may be different, while every candidate must
        also attest the generic schema/compiler/Job/handler profile.
        """

        if not isinstance(session, AsyncSession) or not session.in_transaction():
            raise SystemRuntimePolicyUnavailable
        if type(locked) is not LockedWorkflowRuntimePolicy:
            raise SystemRuntimePolicyUnavailable
        policy = locked.value
        if not policy.enabled or not WORKFLOW_RUN_HANDLER_INSTALLED:
            return create_workflow_runtime_facet_readiness(
                generic_ready=False,
                code_ready=False,
                http_ready=False,
            )

        desired = WorkflowRuntimeMaterializedIdentity.from_locked(locked)
        workers = await self._fresh_exact_worker_profiles(
            session,
            desired=desired,
        )
        generic_ready = any(WORKFLOW_GENERIC_RUNTIME_PROFILE_DIGEST_V1 in profiles for profiles in workers)

        code_digest: str | None = None
        if policy.code.enabled and WORKFLOW_CODE_EXECUTION_HANDLER_INSTALLED:
            code_digest = self._safe_profile_digest(
                self._code_profile_digest_resolver,
                policy,
            )

        http_digest: str | None = None
        if policy.http.enabled and WORKFLOW_HTTP_EXECUTION_HANDLER_INSTALLED:
            resolved_http = self._safe_profile_digest(
                self._http_profile_digest_resolver,
                policy,
            )
            if resolved_http == policy.http.egress_profile_digest:
                http_digest = resolved_http
        if code_digest is not None and code_digest == http_digest:
            # Code isolation and controlled HTTP egress are different trust
            # domains. One opaque digest must never satisfy both facets.
            code_digest = None
            http_digest = None

        code_ready = code_digest is not None and any(
            {
                WORKFLOW_GENERIC_RUNTIME_PROFILE_DIGEST_V1,
                code_digest,
            }
            <= profiles
            for profiles in workers
        )
        http_ready = http_digest is not None and any(
            {
                WORKFLOW_GENERIC_RUNTIME_PROFILE_DIGEST_V1,
                http_digest,
            }
            <= profiles
            for profiles in workers
        )
        return create_workflow_runtime_facet_readiness(
            generic_ready=generic_ready,
            code_ready=code_ready,
            http_ready=http_ready,
        )

    async def project_in_session(
        self,
        session: AsyncSession,
        locked: LockedWorkflowRuntimePolicy,
    ) -> WorkflowRuntimeAdminPolicyV1:
        if not isinstance(session, AsyncSession) or not session.in_transaction():
            raise SystemRuntimePolicyUnavailable
        desired = WorkflowRuntimeMaterializedIdentity.from_locked(locked)
        pending: tuple[WorkflowRuntimeRole, ...] = ()
        facets = await self.read_facets_in_session(session, locked)
        if locked.value.admission_enabled and not facets.generic_ready:
            pending = ("worker",)

        stored = create_workflow_runtime_stored_policy(
            policy_version_id=desired.policy_version_id,
            revision=desired.revision,
            schema_version=desired.schema_version,
            payload_checksum=desired.payload_checksum,
            value=locked.value,
        )
        # Reaching this projection proves that Gateway materialized the exact
        # PostgreSQL current pointer. Worker convergence is an independent
        # admission/claim condition and must not hide the control-plane
        # effective policy from navigation or Definition authoring.
        effective = WorkflowRuntimeEffectivePolicyV1(
            policy_version_id=desired.policy_version_id,
            revision=desired.revision,
            schema_version=desired.schema_version,
            payload_checksum=desired.payload_checksum,
        )
        if pending:
            readiness = WorkflowRuntimePendingV1(
                status="pending",
                code="WORKFLOW_RUNTIME_PENDING",
                admission_ready=False,
            )
        elif not locked.value.enabled:
            readiness = WorkflowRuntimeDisabledV1(
                status="ready",
                code="WORKFLOW_RUNTIME_DISABLED",
                admission_ready=False,
            )
        else:
            readiness = WorkflowRuntimeReadyV1(
                status="ready",
                code="WORKFLOW_RUNTIME_READY",
                admission_ready=locked.value.admission_enabled,
            )
        return create_workflow_runtime_admin_policy(
            stored=stored,
            effective=effective,
            pending_roles=pending,
            readiness=readiness,
        )

    async def require_admission_ready_in_session(
        self,
        session: AsyncSession,
        locked: LockedWorkflowRuntimePolicy,
    ) -> WorkflowRuntimeAdminPolicyV1:
        projection = await self.project_in_session(session, locked)
        if projection.readiness.code != "WORKFLOW_RUNTIME_READY" or not projection.readiness.admission_ready:
            raise SystemRuntimePolicyUnavailable
        return projection


__all__ = [
    "WORKFLOW_CODE_EXECUTION_HANDLER_INSTALLED",
    "WORKFLOW_GENERIC_RUNTIME_PROFILE_DIGEST_V1",
    "WORKFLOW_HTTP_EXECUTION_HANDLER_INSTALLED",
    "WORKFLOW_RUN_HANDLER_INSTALLED",
    "WorkflowRuntimeConvergence",
    "WorkflowRuntimeFacetReadinessV1",
    "WorkflowRuntimeMaterializedIdentity",
    "WorkflowRuntimeRole",
    "create_workflow_runtime_facet_readiness",
    "workflow_generic_runtime_profile_digest_v1",
]
