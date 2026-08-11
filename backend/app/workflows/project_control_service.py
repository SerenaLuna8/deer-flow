"""Project Workflow control-plane readiness and Node Catalog projection.

The caller must first resolve current project membership from a server-issued
``ProjectContext`` and require ``workflow.read``.  This service deliberately
accepts no project, owner, role or capability strings from transport input: it
starts at schema/policy readiness and consumes only the two server-derived
Catalog capability booleans.
"""

from __future__ import annotations

import re
from typing import Protocol

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.final_schema import FinalSchemaProbe, FinalSchemaRequired, FinalSchemaUnavailable
from app.system_runtime_settings.errors import SystemRuntimePolicyUnavailable
from app.system_runtime_settings.materializer import SystemRuntimePolicyMaterializer
from app.system_runtime_settings.models import LockedWorkflowRuntimePolicy
from app.system_runtime_settings.workflow_runtime import (
    WorkflowRuntimeConvergence,
    WorkflowRuntimeFacetReadinessV1,
    create_workflow_runtime_facet_readiness,
)
from app.workflows.catalog_contracts import (
    NodeCatalogResponseV1,
    WorkflowCatalogCapabilityProjectionV1,
    build_project_node_catalog_v1,
)
from app.workflows.contracts import (
    WorkflowControlPlaneReadyV1,
    WorkflowDisabledV1,
    WorkflowPolicyUnavailableV1,
    WorkflowProjectReadinessV1,
    WorkflowSchemaUnavailableV1,
)
from app.workflows.errors import WorkflowUnavailable

_REQUEST_ID = re.compile(r"^[\x20-\x7e]{1,512}$")


class WorkflowSchemaProbePort(Protocol):
    async def require_ready(self, session: AsyncSession) -> object: ...


class WorkflowCurrentPolicyReaderPort(Protocol):
    async def read_current(
        self,
        session: AsyncSession,
    ) -> LockedWorkflowRuntimePolicy: ...


class WorkflowRuntimeFacetReaderPort(Protocol):
    async def read_facets_in_session(
        self,
        session: AsyncSession,
        locked: LockedWorkflowRuntimePolicy,
    ) -> WorkflowRuntimeFacetReadinessV1: ...


class _PostgresWorkflowCurrentPolicyReader:
    async def read_current(
        self,
        session: AsyncSession,
    ) -> LockedWorkflowRuntimePolicy:
        return await SystemRuntimePolicyMaterializer.materialize_workflow_runtime_current_locked_in_session(
            session,
        )


class WorkflowProjectControlService:
    """Read safe project Workflow control state in one caller-owned session."""

    def __init__(
        self,
        *,
        schema_probe: WorkflowSchemaProbePort | None = None,
        policy_reader: WorkflowCurrentPolicyReaderPort | None = None,
        convergence: WorkflowRuntimeFacetReaderPort | None = None,
    ) -> None:
        self._schema_probe = schema_probe if schema_probe is not None else FinalSchemaProbe()
        self._policy_reader = policy_reader if policy_reader is not None else _PostgresWorkflowCurrentPolicyReader()
        self._convergence = convergence if convergence is not None else WorkflowRuntimeConvergence()

    @staticmethod
    def _require_request_id(request_id: str) -> str:
        if type(request_id) is not str or _REQUEST_ID.fullmatch(request_id) is None:
            raise ValueError("Workflow project control requires a safe request ID")
        return request_id

    async def _schema_ready(self, session: AsyncSession) -> bool:
        try:
            await self._schema_probe.require_ready(session)
        except (FinalSchemaRequired, FinalSchemaUnavailable):
            return False
        return True

    async def _policy(
        self,
        session: AsyncSession,
    ) -> LockedWorkflowRuntimePolicy | None:
        try:
            locked = await self._policy_reader.read_current(session)
        except (SystemRuntimePolicyUnavailable, SQLAlchemyError, RuntimeError, TypeError, ValueError):
            return None
        if type(locked) is not LockedWorkflowRuntimePolicy:
            return None
        return locked

    async def _facets(
        self,
        session: AsyncSession,
        locked: LockedWorkflowRuntimePolicy,
    ) -> WorkflowRuntimeFacetReadinessV1:
        if not locked.value.enabled:
            return create_workflow_runtime_facet_readiness(
                generic_ready=False,
                code_ready=False,
                http_ready=False,
            )
        try:
            facets = await self._convergence.read_facets_in_session(session, locked)
        except (SystemRuntimePolicyUnavailable, SQLAlchemyError, RuntimeError, TypeError, ValueError):
            return create_workflow_runtime_facet_readiness(
                generic_ready=False,
                code_ready=False,
                http_ready=False,
            )
        if type(facets) is not WorkflowRuntimeFacetReadinessV1:
            return create_workflow_runtime_facet_readiness(
                generic_ready=False,
                code_ready=False,
                http_ready=False,
            )
        return facets

    async def read_readiness(
        self,
        session: AsyncSession,
        *,
        request_id: str,
    ) -> WorkflowProjectReadinessV1:
        """Return the strict four-state project readiness union.

        Authorization happens before entry.  Schema is then checked before
        policy so simultaneous failures deterministically report schema
        unavailable.  Generic Worker readiness affects only admission, never
        control-plane navigation.
        """

        safe_request_id = self._require_request_id(request_id)
        if not await self._schema_ready(session):
            return WorkflowSchemaUnavailableV1(
                status="unavailable",
                code="WORKFLOW_SCHEMA_UNAVAILABLE",
                workflow_enabled=False,
                schema_ready=False,
                admission_ready=False,
                request_id=safe_request_id,
            )
        locked = await self._policy(session)
        if locked is None:
            return WorkflowPolicyUnavailableV1(
                status="unavailable",
                code="WORKFLOW_POLICY_UNAVAILABLE",
                workflow_enabled=False,
                schema_ready=True,
                admission_ready=False,
                request_id=safe_request_id,
            )
        if not locked.value.enabled:
            return WorkflowDisabledV1(
                status="ready",
                code="WORKFLOW_DISABLED",
                workflow_enabled=False,
                schema_ready=True,
                admission_ready=False,
                request_id=safe_request_id,
            )
        facets = await self._facets(session, locked)
        return WorkflowControlPlaneReadyV1(
            status="ready",
            code="WORKFLOW_CONTROL_PLANE_READY",
            workflow_enabled=True,
            schema_ready=True,
            admission_ready=locked.value.admission_enabled and facets.generic_ready,
            request_id=safe_request_id,
        )

    async def read_node_catalog(
        self,
        session: AsyncSession,
        *,
        request_id: str,
        capabilities: WorkflowCatalogCapabilityProjectionV1,
    ) -> NodeCatalogResponseV1:
        """Return an exact nine-entry Catalog or one stable unavailable error."""

        safe_request_id = self._require_request_id(request_id)
        if type(capabilities) is not WorkflowCatalogCapabilityProjectionV1:
            raise TypeError("server-derived Workflow Catalog capabilities are required")
        if not await self._schema_ready(session):
            raise WorkflowUnavailable(safe_request_id)
        locked = await self._policy(session)
        if locked is None:
            raise WorkflowUnavailable(safe_request_id)
        facets = await self._facets(session, locked)
        return build_project_node_catalog_v1(
            locked=locked,
            capabilities=capabilities,
            facets=facets,
        )


__all__ = [
    "WorkflowCurrentPolicyReaderPort",
    "WorkflowProjectControlService",
    "WorkflowRuntimeFacetReaderPort",
    "WorkflowSchemaProbePort",
]
