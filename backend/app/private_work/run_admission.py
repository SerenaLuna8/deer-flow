from __future__ import annotations

from dataclasses import dataclass, replace

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
class AdmittedPrivateRun:
    run: PrivateRunRecord
    snapshot: PersistedRunSnapshot
    opaque_runtime_scope: PrivateResourceScope

    @property
    def thread_id(self) -> str:
        return self.run.thread_id


class PrivateRunAdmissionService:
    """Admit one project-private run under the frozen database lock order."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        resolver: ProjectAssetResolver | None = None,
        revalidator: PrivateWorkRevalidator | None = None,
        snapshots: RunSnapshotRepository | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._resolver = resolver or ProjectAssetResolver(session_factory)
        self._revalidator = revalidator or PrivateWorkRevalidator()
        self._snapshots = snapshots or RunSnapshotRepository(session_factory)

    async def admit(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        request: PrivateRunCreate,
    ) -> AdmittedPrivateRun:
        context = require_issued_private_work_context(context)
        if type(request) is not PrivateRunCreate or not isinstance(thread_id, str) or not thread_id or request.multitask_strategy != "reject":
            raise PrivateWorkConflict(context.request_id)
        server_request = replace(
            request,
            assistant_id=None,
            status="pending",
            multitask_strategy="reject",
            metadata=strip_private_client_fields(request.metadata),
            kwargs=strip_private_client_fields(request.kwargs),
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
                assets = await self._snapshots.list_assets_in_session(
                    session,
                    context,
                    run.run_id,
                    lock=True,
                )
                grants = await self._snapshots.list_mcp_grants_in_session(
                    session,
                    context,
                    run.run_id,
                    lock=True,
                )
                if not assets or assets[0].asset_kind != AssetKind.AGENT.value:
                    raise RunSnapshotAssetStale
                generations = {asset.catalog_generation for asset in assets}
                if generations != {resolved.catalog_generation}:
                    raise RunSnapshotAssetStale
                return AdmittedPrivateRun(
                    run=run,
                    snapshot=PersistedRunSnapshot(
                        assets=assets,
                        mcp_grants=grants,
                        catalog_generation=resolved.catalog_generation,
                    ),
                    opaque_runtime_scope=context.resource_scope,
                )
        except (RunSnapshotAssetStale, AssetResolutionUnavailable):
            raise PrivateWorkAssetStale(context.request_id) from None
        except AssetForbidden:
            raise PrivateWorkForbidden(context.request_id) from None
        except AssetValidationFailed:
            raise PrivateWorkConflict(context.request_id) from None
        except AssetStorageUnavailable:
            raise PrivateWorkUnavailable(context.request_id) from None
        except PrivateRunConflict:
            raise PrivateWorkConflict(context.request_id) from None
        except PrivateWorkError as error:
            raise type(error)(context.request_id) from None
        except IntegrityError:
            raise PrivateWorkConflict(context.request_id) from None
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
