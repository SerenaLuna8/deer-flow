from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from langgraph.checkpoint.base import empty_checkpoint
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.context import (
    PrivateWorkContext,
    require_issued_private_work_context,
    strip_private_client_fields,
)
from app.private_work.errors import (
    PrivateWorkConflict,
    PrivateWorkError,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from app.private_work.revalidation import PrivateWorkRevalidator
from app.private_work.thread_repository import (
    PrivateThreadRecord,
    PrivateThreadRepository,
    ThreadAgentRef,
)
from app.projects.capabilities import Capability
from deerflow.persistence.shared_assets import (
    AgentRow,
    AgentVersionRow,
    ProjectSystemAgentBindingRow,
)
from deerflow.runtime.private_scope import PrivateResourceScope


class BranchAuthorityCopyHook(Protocol):
    async def copy_branch_authority(
        self,
        scope: PrivateResourceScope,
        source_thread_id: str,
        target_thread_id: str,
    ) -> None: ...


class PrivateThreadService:
    """Authority transaction boundary for project-owned threads."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        project_scoped_checkpointer: ProjectScopedCheckpointer,
        *,
        branch_copy_hook: BranchAuthorityCopyHook | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._project_scoped_checkpointer = project_scoped_checkpointer
        self._branch_copy_hook = branch_copy_hook
        self._revalidator = PrivateWorkRevalidator()

    async def create(
        self,
        context: PrivateWorkContext,
        *,
        thread_id: str,
        agent: ThreadAgentRef,
        display_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> PrivateThreadRecord:
        context = require_issued_private_work_context(context)
        clean_metadata = {} if metadata is None else strip_private_client_fields(metadata)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await self._revalidator.require(
                        session,
                        context,
                        Capability.PRIVATE_WORK_CREATE,
                        lock=True,
                    )
                    await self._require_executable_agent(
                        session,
                        context,
                        agent,
                    )
                    record = await PrivateThreadRepository(session).create(
                        scope=context.resource_scope,
                        thread_id=thread_id,
                        agent=agent,
                        display_name=display_name,
                        metadata=clean_metadata,
                    )
        except PrivateWorkConflict:
            raise PrivateWorkConflict(context.request_id) from None
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

        saver = self._project_scoped_checkpointer.for_context(context)
        try:
            await saver.aput(
                self._checkpoint_config(thread_id),
                empty_checkpoint(),
                {"source": "input", "step": -1, "parents": {}},
                {},
            )
        except Exception as exc:
            await self._compensate_create(context, thread_id)
            if isinstance(exc, PrivateWorkError):
                raise
            raise PrivateWorkUnavailable(context.request_id) from None
        return record

    async def search(
        self,
        context: PrivateWorkContext,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[PrivateThreadRecord, ...]:
        context = require_issued_private_work_context(context)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await self._revalidator.require(
                        session,
                        context,
                        Capability.PRIVATE_WORK_READ_OWN,
                    )
                    return await PrivateThreadRepository(session).search(
                        scope=context.resource_scope,
                        limit=limit,
                        offset=offset,
                    )
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def get(
        self,
        context: PrivateWorkContext,
        thread_id: str,
    ) -> PrivateThreadRecord | None:
        context = require_issued_private_work_context(context)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await self._revalidator.require(
                        session,
                        context,
                        Capability.PRIVATE_WORK_READ_OWN,
                    )
                    return await PrivateThreadRepository(session).get(
                        scope=context.resource_scope,
                        thread_id=thread_id,
                    )
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def patch(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        *,
        expected_version: int,
        display_name: str | None,
    ) -> PrivateThreadRecord:
        context = require_issued_private_work_context(context)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await self._revalidator.require(
                        session,
                        context,
                        Capability.PRIVATE_WORK_CREATE,
                        lock=True,
                    )
                    repository = PrivateThreadRepository(session)
                    if (
                        await repository.get(
                            scope=context.resource_scope,
                            thread_id=thread_id,
                        )
                        is None
                    ):
                        raise PrivateWorkNotFound(context.request_id)
                    return await repository.patch(
                        scope=context.resource_scope,
                        thread_id=thread_id,
                        expected_version=expected_version,
                        display_name=display_name,
                    )
        except PrivateWorkConflict:
            raise PrivateWorkConflict(context.request_id) from None
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def delete(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        *,
        expected_version: int,
    ) -> None:
        context = require_issued_private_work_context(context)
        await self._project_scoped_checkpointer.for_context(context).adelete_thread(
            thread_id,
            expected_version=expected_version,
        )

    async def branch(
        self,
        context: PrivateWorkContext,
        *,
        source_thread_id: str,
        target_thread_id: str,
        checkpoint_id: str,
        expected_source_version: int,
        display_name: str | None = None,
    ) -> PrivateThreadRecord:
        context = require_issued_private_work_context(context)
        saver = self._project_scoped_checkpointer.for_context(context)
        source_item = await saver.aget_tuple(self._checkpoint_config(source_thread_id, checkpoint_id=checkpoint_id))
        if source_item is None:
            raise PrivateWorkNotFound(context.request_id)

        branch_metadata = {
            "branch_parent_thread_id": source_thread_id,
            "branch_parent_checkpoint_id": checkpoint_id,
        }
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await self._revalidator.require(
                        session,
                        context,
                        Capability.PRIVATE_WORK_CREATE,
                        lock=True,
                    )
                    repository = PrivateThreadRepository(session)
                    source = await repository.get(
                        scope=context.resource_scope,
                        thread_id=source_thread_id,
                    )
                    if source is None:
                        raise PrivateWorkNotFound(context.request_id)
                    if source.version != expected_source_version:
                        raise PrivateWorkConflict(context.request_id)
                    agent = ThreadAgentRef(
                        asset_id=source.agent_asset_id,
                        scope=source.agent_scope,
                    )
                    await self._require_executable_agent(
                        session,
                        context,
                        agent,
                    )
                    record = await repository.create(
                        scope=context.resource_scope,
                        thread_id=target_thread_id,
                        agent=agent,
                        display_name=display_name,
                        metadata=branch_metadata,
                    )
        except PrivateWorkConflict:
            raise PrivateWorkConflict(context.request_id) from None
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

        target_config = self._checkpoint_config(target_thread_id)
        try:
            await saver.aput(
                target_config,
                source_item.checkpoint,
                source_item.metadata,
                {},
            )
            if self._branch_copy_hook is not None:
                await self._branch_copy_hook.copy_branch_authority(
                    context.resource_scope,
                    source_thread_id,
                    target_thread_id,
                )
        except Exception as exc:
            await self._compensate_create(context, target_thread_id)
            if isinstance(exc, PrivateWorkError):
                raise
            raise PrivateWorkUnavailable(context.request_id) from None
        return record

    async def _compensate_create(
        self,
        context: PrivateWorkContext,
        thread_id: str,
    ) -> None:
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await PrivateThreadRepository(session).compensate_create(
                        scope=context.resource_scope,
                        thread_id=thread_id,
                    )
        except Exception:
            raise PrivateWorkUnavailable(context.request_id) from None

    @staticmethod
    def _checkpoint_config(
        thread_id: str,
        *,
        checkpoint_id: str | None = None,
    ) -> dict[str, Any]:
        configurable: dict[str, str] = {
            "thread_id": thread_id,
            "checkpoint_ns": "",
        }
        if checkpoint_id is not None:
            configurable["checkpoint_id"] = checkpoint_id
        return {"configurable": configurable}

    @staticmethod
    async def _require_executable_agent(
        session: AsyncSession,
        context: PrivateWorkContext,
        agent: ThreadAgentRef,
    ) -> None:
        if agent.scope == "project":
            statement = (
                select(AgentRow.id)
                .join(
                    AgentVersionRow,
                    AgentVersionRow.id == AgentRow.current_published_version_id,
                )
                .where(
                    AgentRow.id == agent.asset_id,
                    AgentRow.scope == "project",
                    AgentRow.project_id == context.project_id,
                    AgentRow.status == "active",
                    AgentVersionRow.agent_id == AgentRow.id,
                    AgentVersionRow.workflow_status == "published",
                )
            )
        elif agent.scope == "system":
            statement = (
                select(AgentRow.id)
                .join(
                    ProjectSystemAgentBindingRow,
                    ProjectSystemAgentBindingRow.system_agent_id == AgentRow.id,
                )
                .join(
                    AgentVersionRow,
                    AgentVersionRow.id == ProjectSystemAgentBindingRow.agent_version_id,
                )
                .where(
                    AgentRow.id == agent.asset_id,
                    AgentRow.scope == "system",
                    AgentRow.status == "active",
                    ProjectSystemAgentBindingRow.project_id == context.project_id,
                    ProjectSystemAgentBindingRow.enabled.is_(True),
                    AgentVersionRow.agent_id == AgentRow.id,
                    AgentVersionRow.workflow_status == "published",
                )
            )
        else:
            raise PrivateWorkNotFound(context.request_id)
        if (await session.execute(statement)).scalar_one_or_none() is None:
            raise PrivateWorkNotFound(context.request_id)
