"""Worker-only, durable MCP initialize/list-tools discovery jobs."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.private_work.asset_runtime import PrivateAgentRuntime
from app.private_work.errors import PrivateWorkAssetStale, PrivateWorkUnavailable
from app.private_work.mcp_runtime_contracts import (
    DiscoveredMcpTool,
    mcp_tool_inventory_payload,
    validate_project_mcp_material_policy,
    validate_project_mcp_snapshot_policy,
)
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext, resolve_project_context_in_transaction
from app.projects.errors import (
    ProjectDatabaseUnavailable,
    ProjectForbidden,
    ProjectNotFound,
)
from app.shared_assets.errors import (
    AssetForbidden,
    AssetResolutionUnavailable,
    AssetStorageUnavailable,
    AssetValidationFailed,
)
from app.shared_assets.mcp_discovery_repository import (
    McpToolDiscoveryAttemptRecord,
    McpToolDiscoveryAttemptRepository,
    McpToolDiscoveryErrorCode,
    McpToolDiscoveryResultStatus,
)
from app.shared_assets.mcp_tool_inventory_repository import (
    McpToolInventoryRepository,
    mcp_grant_closure_digest,
)
from app.shared_assets.models import (
    AssetKind,
    AssetScope,
    AssetSelection,
    ResolvedMcpSnapshot,
)
from app.shared_assets.resolver import MaterializedMcpSecrets, ProjectAssetResolver
from app.worker.service import (
    JobLeaseAuthority,
    JobOutcome,
    JobSettlement,
    LeaseLost,
)
from deerflow.mcp.http_security import SecureMcpHttpClientFactory
from deerflow.mcp_definition_policy import McpDefinitionPolicyError, McpEndpointPolicy
from deerflow.persistence.jobs.sql import JobClaim, JobRepository

_DISCOVERY_REQUEST_ID = "mcp-discovery-worker"


class _McpDiscoveryCancelled(Exception):
    """The admitted project/version/credential authority is no longer current."""


class _McpDiscoveryAuthorizationBoundary:
    """Lease plus current-closure check immediately before the remote call."""

    def __init__(
        self,
        handler: McpToolDiscoveryJobHandler,
        attempt: McpToolDiscoveryAttemptRecord,
        snapshot: ResolvedMcpSnapshot,
        authority: JobLeaseAuthority,
    ) -> None:
        self._handler = handler
        self._attempt = attempt
        self._snapshot = snapshot
        self._authority = authority

    async def before_mcp_call(self) -> None:
        await self._handler._revalidate_before_remote(
            self._attempt,
            self._snapshot,
            self._authority,
        )


class McpToolDiscoveryJobHandler:
    """Probe one exact MCP closure and atomically persist its observation.

    A discovery failure is the successful outcome of this diagnostic job: the
    attempt and inventory receive a stable public result while the durable Job
    settles successfully. The remote operation itself is never retried.
    """

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        endpoint_policy: McpEndpointPolicy,
        http_client_factory: SecureMcpHttpClientFactory,
        discovery_timeout_seconds: int,
        resolver: ProjectAssetResolver | None = None,
        job_repository_builder: Callable[[AsyncSession], JobRepository] = JobRepository,
    ) -> None:
        if type(discovery_timeout_seconds) is not int or not 1 <= discovery_timeout_seconds <= 300:
            raise ValueError("invalid MCP discovery timeout")
        self._sessions = session_factory
        self._endpoint_policy = endpoint_policy
        self._http_client_factory = http_client_factory
        self._discovery_timeout_seconds = discovery_timeout_seconds
        self._resolver = resolver or ProjectAssetResolver(session_factory)
        self._job_repository_builder = job_repository_builder

    async def __call__(
        self,
        claim: JobClaim,
        authority: JobLeaseAuthority,
    ) -> JobOutcome | JobSettlement:
        if claim.job_type != "mcp_discovery" or claim.scope.owner_user_id is None or claim.run_id is not None or claim.occurrence_id is not None:
            return JobOutcome.cancelled()

        await authority.heartbeat()
        attempt = await self._load_attempt(claim)
        if attempt is None:
            return JobOutcome.cancelled()
        if authority.cancel_requested:
            return await self._settlement(
                claim,
                attempt,
                result_status="cancelled",
                error_code=None,
                tools=None,
            )

        materialized: MaterializedMcpSecrets | None = None
        try:
            _context, snapshot, materialized = await self._resolve_and_materialize(attempt)
            tools = await self._discover(
                attempt,
                snapshot,
                materialized,
                authority,
            )
        except asyncio.CancelledError:
            raise
        except LeaseLost:
            raise
        except _McpDiscoveryCancelled:
            return await self._settlement(
                claim,
                attempt,
                result_status="cancelled",
                error_code=None,
                tools=None,
            )
        except PrivateWorkAssetStale:
            return await self._settlement(
                claim,
                attempt,
                result_status="failed",
                error_code="mcp_catalog_invalid",
                tools=None,
            )
        except PrivateWorkUnavailable:
            return await self._settlement(
                claim,
                attempt,
                result_status="failed",
                error_code="mcp_discovery_unavailable",
                tools=None,
            )
        except Exception:
            return await self._settlement(
                claim,
                attempt,
                result_status="failed",
                error_code="mcp_discovery_unavailable",
                tools=None,
            )
        finally:
            # The resolver returns immutable mappings, so promptly dropping the
            # last local reference is the available plaintext cleanup boundary.
            materialized = None

        return await self._settlement(
            claim,
            attempt,
            result_status="succeeded",
            error_code=None,
            tools=tools,
        )

    async def _load_attempt(
        self,
        claim: JobClaim,
    ) -> McpToolDiscoveryAttemptRecord | None:
        async with self._sessions() as session, session.begin():
            record = await McpToolDiscoveryAttemptRepository(session).get(
                claim.scope.project_id,
                claim.job_id,
            )
        if record is None or record.project_id != claim.scope.project_id or record.requested_by_user_id != claim.scope.owner_user_id or record.status != "running":
            return None
        return record

    async def _current_snapshot_in_session(
        self,
        session: AsyncSession,
        attempt: McpToolDiscoveryAttemptRecord,
    ) -> tuple[ProjectContext, ResolvedMcpSnapshot]:
        try:
            user_id = uuid.UUID(attempt.requested_by_user_id)
        except (AttributeError, TypeError, ValueError):
            raise _McpDiscoveryCancelled from None
        context = await resolve_project_context_in_transaction(
            session,
            user_id,
            attempt.project_id,
            _DISCOVERY_REQUEST_ID,
            lock=True,
        )
        context.require(Capability.SHARED_ASSETS_EXECUTE)
        resolved = await self._resolver.resolve_project_asset_snapshot_in_session(
            session,
            context,
            AssetSelection(
                AssetKind.MCP,
                attempt.mcp_server_id,
                attempt.mcp_server_version_id,
            ),
        )
        if not isinstance(resolved, ResolvedMcpSnapshot):
            raise PrivateWorkAssetStale(_DISCOVERY_REQUEST_ID)
        if not self._attempt_matches_snapshot(attempt, resolved):
            raise _McpDiscoveryCancelled
        return context, resolved

    @staticmethod
    def _attempt_matches_snapshot(
        attempt: McpToolDiscoveryAttemptRecord,
        snapshot: ResolvedMcpSnapshot,
    ) -> bool:
        return (
            snapshot.scope is AssetScope.PROJECT
            and snapshot.asset_id == attempt.mcp_server_id
            and snapshot.version_id == attempt.mcp_server_version_id
            and snapshot.checksum == attempt.payload_checksum
            and mcp_grant_closure_digest(snapshot.credential_grant_ids) == attempt.grant_digest
        )

    async def _resolve_and_materialize(
        self,
        attempt: McpToolDiscoveryAttemptRecord,
    ) -> tuple[ProjectContext, ResolvedMcpSnapshot, MaterializedMcpSecrets]:
        try:
            async with self._sessions() as session, session.begin():
                context, snapshot = await self._current_snapshot_in_session(
                    session,
                    attempt,
                )
                validate_project_mcp_snapshot_policy(
                    snapshot,
                    endpoint_policy=self._endpoint_policy,
                    http_client_factory=self._http_client_factory,
                )
            materialized = await self._resolver.materialize_mcp_secrets(
                context,
                snapshot,
            )
            validate_project_mcp_material_policy(snapshot, materialized.by_slot)
            return context, snapshot, materialized
        except _McpDiscoveryCancelled:
            raise
        except (ProjectNotFound, ProjectForbidden, AssetForbidden):
            raise _McpDiscoveryCancelled from None
        except (
            AssetResolutionUnavailable,
            AssetValidationFailed,
            McpDefinitionPolicyError,
        ):
            raise PrivateWorkAssetStale(_DISCOVERY_REQUEST_ID) from None
        except (
            ProjectDatabaseUnavailable,
            AssetStorageUnavailable,
            DBAPIError,
        ):
            raise PrivateWorkUnavailable(_DISCOVERY_REQUEST_ID) from None

    async def _revalidate_before_remote(
        self,
        attempt: McpToolDiscoveryAttemptRecord,
        snapshot: ResolvedMcpSnapshot,
        authority: JobLeaseAuthority,
    ) -> None:
        await authority.heartbeat()
        if authority.cancel_requested:
            raise _McpDiscoveryCancelled
        try:
            async with self._sessions() as session, session.begin():
                _context, current = await self._current_snapshot_in_session(
                    session,
                    attempt,
                )
                if current.scope is not snapshot.scope or current.definition != snapshot.definition or current.credential_grant_ids != snapshot.credential_grant_ids:
                    raise _McpDiscoveryCancelled
                validate_project_mcp_snapshot_policy(
                    current,
                    endpoint_policy=self._endpoint_policy,
                    http_client_factory=self._http_client_factory,
                )
        except _McpDiscoveryCancelled:
            raise
        except (ProjectNotFound, ProjectForbidden, AssetForbidden):
            raise _McpDiscoveryCancelled from None
        except (
            AssetResolutionUnavailable,
            AssetValidationFailed,
        ):
            raise _McpDiscoveryCancelled from None
        except McpDefinitionPolicyError:
            raise PrivateWorkAssetStale(_DISCOVERY_REQUEST_ID) from None
        except (
            ProjectDatabaseUnavailable,
            AssetStorageUnavailable,
            DBAPIError,
        ):
            raise PrivateWorkUnavailable(_DISCOVERY_REQUEST_ID) from None

    async def _discover(
        self,
        attempt: McpToolDiscoveryAttemptRecord,
        snapshot: ResolvedMcpSnapshot,
        materialized: MaterializedMcpSecrets,
        authority: JobLeaseAuthority,
    ) -> tuple[DiscoveredMcpTool, ...]:
        try:
            validate_project_mcp_snapshot_policy(
                snapshot,
                endpoint_policy=self._endpoint_policy,
                http_client_factory=self._http_client_factory,
            )
            validate_project_mcp_material_policy(
                snapshot,
                materialized.by_slot,
            )
            return await PrivateAgentRuntime._discover_exact_mcp(
                snapshot.version_id,
                snapshot.definition,
                materialized.by_slot,
                authorization_boundary=_McpDiscoveryAuthorizationBoundary(
                    self,
                    attempt,
                    snapshot,
                    authority,
                ),
                http_client_factory=self._http_client_factory,
                discovery_timeout_seconds=self._discovery_timeout_seconds,
            )
        except (_McpDiscoveryCancelled, PrivateWorkAssetStale, PrivateWorkUnavailable):
            raise
        except McpDefinitionPolicyError:
            raise PrivateWorkAssetStale(_DISCOVERY_REQUEST_ID) from None
        except Exception:
            raise PrivateWorkUnavailable(_DISCOVERY_REQUEST_ID) from None

    async def _settlement(
        self,
        claim: JobClaim,
        attempt: McpToolDiscoveryAttemptRecord,
        *,
        result_status: McpToolDiscoveryResultStatus,
        error_code: McpToolDiscoveryErrorCode | None,
        tools: tuple[DiscoveredMcpTool, ...] | None,
    ) -> JobSettlement:
        attempted_at = datetime.now(UTC)

        async def commit() -> None:
            final_status = result_status
            final_error = error_code
            async with self._sessions() as session, session.begin():
                if final_status != "cancelled":
                    try:
                        _context, current = await self._current_snapshot_in_session(
                            session,
                            attempt,
                        )
                        validate_project_mcp_snapshot_policy(
                            current,
                            endpoint_policy=self._endpoint_policy,
                            http_client_factory=self._http_client_factory,
                        )
                    except (
                        _McpDiscoveryCancelled,
                        ProjectNotFound,
                        ProjectForbidden,
                        AssetForbidden,
                        AssetResolutionUnavailable,
                        AssetValidationFailed,
                    ):
                        final_status = "cancelled"
                        final_error = None
                    except McpDefinitionPolicyError:
                        final_status = "failed"
                        final_error = "mcp_catalog_invalid"
                    else:
                        inventory = McpToolInventoryRepository(session)
                        common = {
                            "project_id": attempt.project_id,
                            "mcp_server_id": attempt.mcp_server_id,
                            "mcp_server_version_id": attempt.mcp_server_version_id,
                            "payload_checksum": attempt.payload_checksum,
                            "grant_digest": attempt.grant_digest,
                            "attempted_at": attempted_at,
                        }
                        if final_status == "succeeded" and tools is not None:
                            await inventory.record_success(
                                **common,
                                tools=mcp_tool_inventory_payload(tools),
                            )
                        elif final_status == "failed" and final_error is not None:
                            await inventory.record_failure(
                                **common,
                                public_error_code=final_error,
                            )
                        # The resolver is authoritative; keep the local name to
                        # make the settlement fence explicit to reviewers.
                        del current

                await McpToolDiscoveryAttemptRepository(session).mark_result(
                    attempt.attempt_id,
                    cast(McpToolDiscoveryResultStatus, final_status),
                    cast(McpToolDiscoveryErrorCode | None, final_error),
                )
                changed = await self._job_repository_builder(session).settle_success(
                    claim.job_id,
                    lease_token=claim.lease_token,
                    now=attempted_at,
                )
                if not changed:
                    raise LeaseLost(claim.job_id)

        return JobSettlement(JobOutcome.succeeded(), commit)


__all__ = ["McpToolDiscoveryJobHandler"]
