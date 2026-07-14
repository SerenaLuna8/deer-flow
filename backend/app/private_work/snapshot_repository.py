from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.private_work.context import PrivateWorkContext, require_issued_private_work_context
from app.private_work.errors import PrivateWorkConflict, PrivateWorkError, PrivateWorkUnavailable
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRecord, PrivateRunRepository
from app.shared_assets.models import AssetKind, AssetScope, ResolvedAgentSnapshot
from deerflow.persistence.private_work.model import RunAssetVersionRow, RunMcpGrantSnapshotRow
from deerflow.persistence.shared_assets.agent_model import (
    AgentRow,
    AgentVersionMcpRefRow,
    AgentVersionRow,
    AgentVersionSkillRefRow,
)
from deerflow.persistence.shared_assets.binding_model import AssetCatalogStateRow
from deerflow.persistence.shared_assets.credential_model import (
    CredentialGrantRow,
    CredentialRow,
    CredentialVersionRow,
)
from deerflow.persistence.shared_assets.mcp_model import McpCredentialSlotRow, McpServerRow, McpServerVersionRow
from deerflow.persistence.shared_assets.skill_model import SkillRow, SkillVersionRow

_FORBIDDEN_PERSISTED_KEY_PARTS = (
    "secret",
    "envelope",
    "key_id",
    "nonce",
    "ciphertext",
    "storage_locator",
)


@dataclass(frozen=True, slots=True)
class RunAssetSnapshot:
    asset_kind: str
    dependency_order: int
    asset_scope: str
    asset_id: uuid.UUID
    version_id: uuid.UUID
    payload_checksum: str
    catalog_generation: int


@dataclass(frozen=True, slots=True)
class RunMcpGrantSnapshot:
    mcp_version_id: uuid.UUID
    credential_slot_id: uuid.UUID
    credential_grant_id: uuid.UUID
    credential_version_id: uuid.UUID


def _reject_secret_bearing_keys(value: object, request_id: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower()
            if any(part in normalized for part in _FORBIDDEN_PERSISTED_KEY_PARTS):
                raise PrivateWorkConflict(request_id)
            _reject_secret_bearing_keys(item, request_id)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_secret_bearing_keys(item, request_id)


class RunSnapshotRepository:
    """Atomically persist a private run and its exact, secret-free asset closure."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _asset_allowed(
        *,
        asset_scope: str,
        asset_project_id: uuid.UUID | None,
        project_id: uuid.UUID,
    ) -> bool:
        return (asset_scope == AssetScope.SYSTEM.value and asset_project_id is None) or (asset_scope == AssetScope.PROJECT.value and asset_project_id == project_id)

    @staticmethod
    async def _agent(
        session: AsyncSession,
        snapshot: ResolvedAgentSnapshot,
        project_id: uuid.UUID,
    ) -> tuple[AgentRow, AgentVersionRow]:
        row = (
            await session.execute(
                select(AgentRow, AgentVersionRow)
                .join(AgentVersionRow, AgentVersionRow.agent_id == AgentRow.id)
                .where(
                    AgentRow.id == snapshot.asset_id,
                    AgentVersionRow.id == snapshot.version_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise PrivateWorkConflict("unknown")
        asset, version = row
        if (
            asset.scope != snapshot.scope.value
            or asset.status != "active"
            or version.workflow_status != "published"
            or version.payload_checksum != snapshot.checksum
            or not RunSnapshotRepository._asset_allowed(
                asset_scope=asset.scope,
                asset_project_id=asset.project_id,
                project_id=project_id,
            )
        ):
            raise PrivateWorkConflict("unknown")
        return asset, version

    @staticmethod
    async def _skills(
        session: AsyncSession,
        version_ids: tuple[uuid.UUID, ...],
        project_id: uuid.UUID,
    ) -> list[tuple[SkillRow, SkillVersionRow]]:
        rows: list[tuple[SkillRow, SkillVersionRow]] = []
        for version_id in version_ids:
            row = (await session.execute(select(SkillRow, SkillVersionRow).join(SkillVersionRow, SkillVersionRow.skill_id == SkillRow.id).where(SkillVersionRow.id == version_id))).one_or_none()
            if row is None:
                raise PrivateWorkConflict("unknown")
            asset, version = row
            if (
                not RunSnapshotRepository._asset_allowed(
                    asset_scope=asset.scope,
                    asset_project_id=asset.project_id,
                    project_id=project_id,
                )
                or asset.status != "active"
                or version.workflow_status != "published"
            ):
                raise PrivateWorkConflict("unknown")
            rows.append((asset, version))
        return rows

    @staticmethod
    async def _mcps(
        session: AsyncSession,
        version_ids: tuple[uuid.UUID, ...],
        project_id: uuid.UUID,
    ) -> list[tuple[McpServerRow, McpServerVersionRow]]:
        rows: list[tuple[McpServerRow, McpServerVersionRow]] = []
        for version_id in version_ids:
            row = (await session.execute(select(McpServerRow, McpServerVersionRow).join(McpServerVersionRow, McpServerVersionRow.mcp_server_id == McpServerRow.id).where(McpServerVersionRow.id == version_id))).one_or_none()
            if row is None:
                raise PrivateWorkConflict("unknown")
            asset, version = row
            if (
                not RunSnapshotRepository._asset_allowed(
                    asset_scope=asset.scope,
                    asset_project_id=asset.project_id,
                    project_id=project_id,
                )
                or asset.status != "active"
                or version.workflow_status != "published"
            ):
                raise PrivateWorkConflict("unknown")
            rows.append((asset, version))
        return rows

    @staticmethod
    async def _validate_dependency_order(
        session: AsyncSession,
        snapshot: ResolvedAgentSnapshot,
    ) -> None:
        skill_ids = tuple(
            (
                await session.execute(
                    select(AgentVersionSkillRefRow.skill_version_id)
                    .where(AgentVersionSkillRefRow.agent_version_id == snapshot.version_id)
                    .order_by(
                        AgentVersionSkillRefRow.sort_order,
                        AgentVersionSkillRefRow.skill_version_id,
                    )
                )
            ).scalars()
        )
        mcp_ids = tuple(
            (
                await session.execute(
                    select(AgentVersionMcpRefRow.mcp_server_version_id)
                    .where(AgentVersionMcpRefRow.agent_version_id == snapshot.version_id)
                    .order_by(
                        AgentVersionMcpRefRow.sort_order,
                        AgentVersionMcpRefRow.mcp_server_version_id,
                    )
                )
            ).scalars()
        )
        if skill_ids != snapshot.payload.skill_version_ids or mcp_ids != snapshot.payload.mcp_version_ids or snapshot.dependency_version_ids != (*skill_ids, *mcp_ids):
            raise PrivateWorkConflict("unknown")

    @staticmethod
    async def _grant_rows(
        session: AsyncSession,
        mcp_version_ids: tuple[uuid.UUID, ...],
        project_id: uuid.UUID,
    ) -> list[tuple[CredentialGrantRow, McpCredentialSlotRow]]:
        rows: list[tuple[CredentialGrantRow, McpCredentialSlotRow]] = []
        for mcp_version_id in mcp_version_ids:
            slots = (await session.execute(select(McpCredentialSlotRow).where(McpCredentialSlotRow.mcp_server_version_id == mcp_version_id).order_by(McpCredentialSlotRow.name, McpCredentialSlotRow.id))).scalars()
            for slot in slots:
                grant = (
                    await session.execute(
                        select(CredentialGrantRow).where(
                            CredentialGrantRow.mcp_server_version_id == mcp_version_id,
                            CredentialGrantRow.credential_slot_id == slot.id,
                            CredentialGrantRow.status == "active",
                        )
                    )
                ).scalar_one_or_none()
                if grant is None:
                    if slot.required:
                        raise PrivateWorkConflict("unknown")
                    continue
                credential = (
                    await session.execute(
                        select(CredentialRow, CredentialVersionRow)
                        .join(
                            CredentialVersionRow,
                            CredentialVersionRow.credential_id == CredentialRow.id,
                        )
                        .where(CredentialVersionRow.id == grant.credential_version_id)
                    )
                ).one_or_none()
                if (
                    credential is None
                    or not RunSnapshotRepository._asset_allowed(
                        asset_scope=credential[0].scope,
                        asset_project_id=credential[0].project_id,
                        project_id=project_id,
                    )
                    or credential[0].status != "active"
                    or credential[1].status != "active"
                ):
                    raise PrivateWorkConflict("unknown")
                rows.append((grant, slot))
        return rows

    async def create_run_with_snapshot(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        request: PrivateRunCreate,
        resolved_agent: ResolvedAgentSnapshot,
    ) -> PrivateRunRecord:
        context = require_issued_private_work_context(context)
        if type(request) is not PrivateRunCreate or type(resolved_agent) is not ResolvedAgentSnapshot:
            raise PrivateWorkConflict(context.request_id)
        if resolved_agent.kind is not AssetKind.AGENT or resolved_agent.catalog_generation < 0:
            raise PrivateWorkConflict(context.request_id)
        _reject_secret_bearing_keys(request.metadata, context.request_id)
        _reject_secret_bearing_keys(request.kwargs, context.request_id)
        project_id = context.project_id
        safe_request = replace(
            request,
            assistant_id=request.assistant_id or str(resolved_agent.asset_id),
        )
        try:
            async with self._session_factory() as session, session.begin():
                await self._agent(session, resolved_agent, project_id)
                await self._validate_dependency_order(session, resolved_agent)
                skills = await self._skills(
                    session,
                    resolved_agent.payload.skill_version_ids,
                    project_id,
                )
                mcps = await self._mcps(
                    session,
                    resolved_agent.payload.mcp_version_ids,
                    project_id,
                )
                grants = await self._grant_rows(
                    session,
                    resolved_agent.payload.mcp_version_ids,
                    project_id,
                )
                generation = await session.scalar(select(AssetCatalogStateRow.generation).where(AssetCatalogStateRow.id == 1).with_for_update())
                if generation != resolved_agent.catalog_generation:
                    raise PrivateWorkConflict(context.request_id)
                run = await PrivateRunRepository(session).create(
                    scope=context.resource_scope,
                    thread_id=thread_id,
                    request=safe_request,
                )
                asset_rows = [
                    RunAssetVersionRow(
                        project_id=context.project_id,
                        owner_user_id=str(context.user_id),
                        thread_id=thread_id,
                        run_id=run.run_id,
                        asset_kind=AssetKind.AGENT.value,
                        dependency_order=0,
                        asset_scope=resolved_agent.scope.value,
                        asset_id=resolved_agent.asset_id,
                        version_id=resolved_agent.version_id,
                        payload_checksum=resolved_agent.checksum,
                        catalog_generation=resolved_agent.catalog_generation,
                    )
                ]
                dependency_order = 1
                for asset, version in skills:
                    asset_rows.append(
                        RunAssetVersionRow(
                            project_id=context.project_id,
                            owner_user_id=str(context.user_id),
                            thread_id=thread_id,
                            run_id=run.run_id,
                            asset_kind=AssetKind.SKILL.value,
                            dependency_order=dependency_order,
                            asset_scope=asset.scope,
                            asset_id=asset.id,
                            version_id=version.id,
                            payload_checksum=version.payload_checksum,
                            catalog_generation=resolved_agent.catalog_generation,
                        )
                    )
                    dependency_order += 1
                for asset, version in mcps:
                    asset_rows.append(
                        RunAssetVersionRow(
                            project_id=context.project_id,
                            owner_user_id=str(context.user_id),
                            thread_id=thread_id,
                            run_id=run.run_id,
                            asset_kind=AssetKind.MCP.value,
                            dependency_order=dependency_order,
                            asset_scope=asset.scope,
                            asset_id=asset.id,
                            version_id=version.id,
                            payload_checksum=version.payload_checksum,
                            catalog_generation=resolved_agent.catalog_generation,
                        )
                    )
                    dependency_order += 1
                session.add_all(asset_rows)
                session.add_all(
                    RunMcpGrantSnapshotRow(
                        project_id=context.project_id,
                        owner_user_id=str(context.user_id),
                        thread_id=thread_id,
                        run_id=run.run_id,
                        mcp_version_id=grant.mcp_server_version_id,
                        credential_slot_id=grant.credential_slot_id,
                        credential_grant_id=grant.id,
                        credential_version_id=grant.credential_version_id,
                    )
                    for grant, _slot in grants
                )
                await session.flush()
                return run
        except PrivateWorkError:
            raise
        except IntegrityError:
            raise PrivateWorkConflict(context.request_id) from None
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def list_assets(
        self,
        context: PrivateWorkContext,
        run_id: str,
    ) -> tuple[RunAssetSnapshot, ...]:
        context = require_issued_private_work_context(context)
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(RunAssetVersionRow)
                    .where(
                        RunAssetVersionRow.project_id == context.project_id,
                        RunAssetVersionRow.owner_user_id == str(context.user_id),
                        RunAssetVersionRow.run_id == run_id,
                    )
                    .order_by(RunAssetVersionRow.dependency_order)
                )
            ).scalars()
            return tuple(
                RunAssetSnapshot(
                    asset_kind=row.asset_kind,
                    dependency_order=row.dependency_order,
                    asset_scope=row.asset_scope,
                    asset_id=row.asset_id,
                    version_id=row.version_id,
                    payload_checksum=row.payload_checksum,
                    catalog_generation=row.catalog_generation,
                )
                for row in rows
            )

    async def list_mcp_grants(
        self,
        context: PrivateWorkContext,
        run_id: str,
    ) -> tuple[RunMcpGrantSnapshot, ...]:
        context = require_issued_private_work_context(context)
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(RunMcpGrantSnapshotRow)
                    .where(
                        RunMcpGrantSnapshotRow.project_id == context.project_id,
                        RunMcpGrantSnapshotRow.owner_user_id == str(context.user_id),
                        RunMcpGrantSnapshotRow.run_id == run_id,
                    )
                    .order_by(
                        RunMcpGrantSnapshotRow.mcp_version_id,
                        RunMcpGrantSnapshotRow.credential_slot_id,
                    )
                )
            ).scalars()
            return tuple(
                RunMcpGrantSnapshot(
                    mcp_version_id=row.mcp_version_id,
                    credential_slot_id=row.credential_slot_id,
                    credential_grant_id=row.credential_grant_id,
                    credential_version_id=row.credential_version_id,
                )
                for row in rows
            )
