from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.private_work.context import (
    PrivateWorkContext,
    require_issued_private_work_context,
    strip_private_client_fields,
)
from app.private_work.errors import (
    PrivateWorkAssetStale,
    PrivateWorkConflict,
    PrivateWorkError,
    PrivateWorkForbidden,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from app.private_work.revalidation import PrivateWorkRevalidator
from app.private_work.run_repository import PrivateRunConflict, PrivateRunCreate, PrivateRunRecord, PrivateRunRepository
from app.private_work.snapshot_repository import (
    RunAssetSnapshot,
    RunMcpGrantSnapshot,
    RunSnapshotAssetStale,
    RunSnapshotRepository,
)
from app.private_work.thread_repository import PrivateThreadRepository
from app.projects.capabilities import Capability
from app.reliability.jobs import (
    AdmittedJobRecord,
    JobIdempotencyConflict,
    JobScope,
    PrivateRunJobRepository,
)
from app.shared_assets.errors import (
    AssetForbidden,
    AssetResolutionUnavailable,
    AssetStorageUnavailable,
    AssetValidationFailed,
)
from app.shared_assets.models import AssetKind, AssetSelection, ResolvedAgentSnapshot
from app.shared_assets.resolver import ProjectAssetResolver
from deerflow.runtime.private_scope import PrivateResourceScope


@dataclass(frozen=True, slots=True)
class PersistedRunSnapshot:
    assets: tuple[RunAssetSnapshot, ...]
    mcp_grants: tuple[RunMcpGrantSnapshot, ...]
    catalog_generation: int


@dataclass(frozen=True, slots=True)
class PrivateRunAdmissionServerContext:
    """Typed authority supplied only by an authenticated server caller."""

    non_interactive: bool = False

    def __post_init__(self) -> None:
        if type(self.non_interactive) is not bool:
            raise TypeError("non_interactive must be a boolean")


@dataclass(frozen=True, slots=True)
class AdmittedPrivateRun:
    run: PrivateRunRecord
    job: AdmittedJobRecord
    snapshot: PersistedRunSnapshot
    opaque_runtime_scope: PrivateResourceScope

    @property
    def thread_id(self) -> str:
        return self.run.thread_id


class PrivateRunAdmissionQuotaPort(Protocol):
    async def reserve_concurrent_run(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        run: PrivateRunRecord,
    ) -> None: ...


class PrivateRunAdmissionAuditPort(Protocol):
    async def run_admitted(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        run: PrivateRunRecord,
        job: AdmittedJobRecord,
    ) -> None: ...


class _NoopPrivateRunAdmissionQuota:
    async def reserve_concurrent_run(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        run: PrivateRunRecord,
    ) -> None:
        del session, context, run


class _NoopPrivateRunAdmissionAudit:
    async def run_admitted(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        run: PrivateRunRecord,
        job: AdmittedJobRecord,
    ) -> None:
        del session, context, run, job


class PrivateRunAdmissionService:
    """Admit one project-private run under the frozen database lock order."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        resolver: ProjectAssetResolver | None = None,
        revalidator: PrivateWorkRevalidator | None = None,
        snapshots: RunSnapshotRepository | None = None,
        quota: PrivateRunAdmissionQuotaPort | None = None,
        audit: PrivateRunAdmissionAuditPort | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._resolver = resolver or ProjectAssetResolver(session_factory)
        self._revalidator = revalidator or PrivateWorkRevalidator()
        self._snapshots = snapshots or RunSnapshotRepository(session_factory)
        self._quota = quota or _NoopPrivateRunAdmissionQuota()
        self._audit = audit or _NoopPrivateRunAdmissionAudit()

    async def _persisted_snapshot(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        run_id: str,
    ) -> PersistedRunSnapshot:
        assets = await self._snapshots.list_assets_in_session(
            session,
            context,
            run_id,
            lock=True,
        )
        grants = await self._snapshots.list_mcp_grants_in_session(
            session,
            context,
            run_id,
            lock=True,
        )
        if not assets or assets[0].asset_kind != AssetKind.AGENT.value:
            raise RunSnapshotAssetStale
        generations = {asset.catalog_generation for asset in assets}
        if len(generations) != 1:
            raise RunSnapshotAssetStale
        return PersistedRunSnapshot(
            assets=assets,
            mcp_grants=grants,
            catalog_generation=generations.pop(),
        )

    @staticmethod
    def _is_same_request(
        run: PrivateRunRecord,
        *,
        thread_id: str,
        request: PrivateRunCreate,
    ) -> bool:
        return run.thread_id == thread_id and run.multitask_strategy == request.multitask_strategy and run.metadata == request.metadata and run.kwargs == request.kwargs

    @staticmethod
    def _server_kwargs(
        kwargs: dict[str, object],
        server_context: PrivateRunAdmissionServerContext | None,
    ) -> dict[str, object]:
        if server_context is None:
            return kwargs
        if type(server_context) is not PrivateRunAdmissionServerContext:
            raise TypeError("PrivateRunAdmissionServerContext is required")
        if not server_context.non_interactive:
            return kwargs
        result = dict(kwargs)
        config_value = result.get("config")
        config = dict(config_value) if isinstance(config_value, dict) else {}
        context_value = config.get("context")
        runtime_context = dict(context_value) if isinstance(context_value, dict) else {}
        runtime_context["non_interactive"] = True
        config["context"] = runtime_context
        result["config"] = config
        return result

    async def admit(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        request: PrivateRunCreate,
        *,
        server_context: PrivateRunAdmissionServerContext | None = None,
    ) -> AdmittedPrivateRun:
        context = require_issued_private_work_context(context)
        if type(request) is not PrivateRunCreate or not isinstance(thread_id, str) or not thread_id or request.multitask_strategy != "reject":
            raise PrivateWorkConflict(context.request_id)
        safe_kwargs = strip_private_client_fields(request.kwargs)
        server_request = replace(
            request,
            assistant_id=None,
            status="pending",
            multitask_strategy="reject",
            metadata=strip_private_client_fields(request.metadata),
            kwargs=self._server_kwargs(safe_kwargs, server_context),
            model_name=None,
        )
        try:
            async with self._session_factory() as session, session.begin():
                # The revalidator issues the project then membership FOR UPDATE
                # statements.  Every lock below is subordinate to that scope.
                current = await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.SHARED_ASSETS_EXECUTE,
                    lock=True,
                )
                thread = await PrivateThreadRepository(session).get(
                    scope=context.resource_scope,
                    thread_id=thread_id,
                    lock=True,
                )
                if thread is None:
                    raise PrivateWorkNotFound(context.request_id)

                runs = PrivateRunRepository(session)
                jobs = PrivateRunJobRepository(session)
                job_scope = JobScope(
                    project_id=context.project_id,
                    owner_user_id=str(context.user_id),
                )
                existing = await runs.get(
                    scope=context.resource_scope,
                    run_id=server_request.run_id,
                    lock=True,
                )
                if existing is not None:
                    if (
                        not self._is_same_request(
                            existing,
                            thread_id=thread_id,
                            request=server_request,
                        )
                        or existing.job_id is None
                    ):
                        raise PrivateWorkConflict(context.request_id)
                    existing_job = await jobs.get(
                        scope=job_scope,
                        run_id=existing.run_id,
                        job_id=existing.job_id,
                    )
                    if existing_job is None:
                        raise PrivateWorkConflict(context.request_id)
                    return AdmittedPrivateRun(
                        run=existing,
                        snapshot=await self._persisted_snapshot(
                            session,
                            context,
                            existing.run_id,
                        ),
                        opaque_runtime_scope=context.resource_scope,
                        job=existing_job,
                    )
                if await runs.has_conflicting_active_run(
                    scope=context.resource_scope,
                    thread_id=thread_id,
                ):
                    raise PrivateWorkConflict(context.request_id)

                resolved = await self._resolver.resolve_project_asset_snapshot_in_session(
                    session,
                    current,
                    AssetSelection(AssetKind.AGENT, thread.agent_asset_id),
                )
                if type(resolved) is not ResolvedAgentSnapshot or resolved.scope.value != thread.agent_scope:
                    raise RunSnapshotAssetStale

                run = await self._snapshots.create_run_with_snapshot_in_session(
                    session,
                    context,
                    thread_id,
                    server_request,
                    resolved,
                )
                job = await jobs.enqueue(scope=job_scope, run_id=run.run_id)
                run = await runs.attach_job(
                    scope=context.resource_scope,
                    run_id=run.run_id,
                    job_id=job.job_id,
                )
                await self._quota.reserve_concurrent_run(session, context, run)
                await self._audit.run_admitted(session, context, run, job)
                snapshot = await self._persisted_snapshot(
                    session,
                    context,
                    run.run_id,
                )
                if snapshot.catalog_generation != resolved.catalog_generation:
                    raise RunSnapshotAssetStale
                return AdmittedPrivateRun(
                    run=run,
                    snapshot=snapshot,
                    opaque_runtime_scope=context.resource_scope,
                    job=job,
                )
        except (RunSnapshotAssetStale, AssetResolutionUnavailable):
            raise PrivateWorkAssetStale(context.request_id) from None
        except AssetForbidden:
            raise PrivateWorkForbidden(context.request_id) from None
        except AssetValidationFailed:
            raise PrivateWorkConflict(context.request_id) from None
        except AssetStorageUnavailable:
            raise PrivateWorkUnavailable(context.request_id) from None
        except (JobIdempotencyConflict, PrivateRunConflict):
            raise PrivateWorkConflict(context.request_id) from None
        except PrivateWorkError as error:
            raise type(error)(context.request_id) from None
        except IntegrityError:
            raise PrivateWorkConflict(context.request_id) from None
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
