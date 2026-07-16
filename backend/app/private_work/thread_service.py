from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from langgraph.checkpoint.base import empty_checkpoint
from sqlalchemy import or_, select
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
from app.private_work.executable_agent import require_executable_agent
from app.private_work.revalidation import PrivateWorkRevalidator
from app.private_work.thread_repository import (
    PrivateThreadRecord,
    PrivateThreadRepository,
    ThreadAgentRef,
)
from app.projects.capabilities import Capability
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.runtime.private_scope import PrivateResourceScope


class BranchAuthorityCopyHook(Protocol):
    async def copy_branch_authority(
        self,
        context: PrivateWorkContext,
        source_thread_id: str,
        target_thread_id: str,
        *,
        session: AsyncSession | None = None,
    ) -> None: ...

    async def rollback_branch_authority(
        self,
        scope: PrivateResourceScope,
        source_thread_id: str,
        target_thread_id: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class BranchCheckpointSelection:
    requested_checkpoint_id: str
    source_head_checkpoint_id: str | None
    source_visible_tail_message_id: str | None
    workspace_clone_mode: Literal[
        "current_thread_authority_copy",
        "historical_skip",
    ]


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

        record: PrivateThreadRecord
        source_item: Any
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
                        lock=True,
                    )
                    if source is None:
                        raise PrivateWorkNotFound(context.request_id)
                    if source.version != expected_source_version:
                        raise PrivateWorkConflict(context.request_id)
                    incomplete_run = (
                        await session.execute(
                            select(RunRow.run_id)
                            .where(
                                RunRow.project_id == context.project_id,
                                RunRow.owner_user_id == str(context.user_id),
                                RunRow.thread_id == source_thread_id,
                                or_(
                                    RunRow.status.in_(("pending", "running")),
                                    RunRow.finalization_status == "finalizing",
                                ),
                            )
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if incomplete_run is not None:
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
                    source_item = await saver.aget_tuple_already_authorized(
                        self._checkpoint_config(
                            source_thread_id,
                            checkpoint_id=checkpoint_id,
                        ),
                        session=session,
                    )
                    if source_item is None or self._checkpoint_tuple_id(source_item) != checkpoint_id:
                        raise PrivateWorkNotFound(context.request_id)
                    try:
                        latest_item = await saver.aget_tuple_already_authorized(
                            self._checkpoint_config(source_thread_id),
                            session=session,
                        )
                    except PrivateWorkError:
                        latest_item = None
                    selection = self._classify_branch_checkpoint(
                        checkpoint_id,
                        source_item,
                        latest_item,
                    )
                    branch_metadata = {
                        "branch_parent_thread_id": source_thread_id,
                        "branch_parent_checkpoint_id": checkpoint_id,
                        "branch_source_head_checkpoint_id": selection.source_head_checkpoint_id,
                        "workspace_clone_mode": selection.workspace_clone_mode,
                    }
                    if selection.source_visible_tail_message_id is not None:
                        branch_metadata["branch_parent_visible_tail_message_id"] = selection.source_visible_tail_message_id
                    await self._raise_if_branch_target_exists(
                        session,
                        context,
                        target_thread_id,
                    )
                    try:
                        async with session.begin_nested():
                            record = await repository.create(
                                scope=context.resource_scope,
                                thread_id=target_thread_id,
                                agent=agent,
                                display_name=display_name,
                                metadata=branch_metadata,
                            )
                    except PrivateWorkConflict:
                        await self._raise_if_branch_target_exists(
                            session,
                            context,
                            target_thread_id,
                        )
                        raise
                    if selection.workspace_clone_mode == "current_thread_authority_copy" and self._branch_copy_hook is not None:
                        await self._branch_copy_hook.copy_branch_authority(
                            context,
                            source_thread_id,
                            target_thread_id,
                            session=session,
                        )
        except PrivateWorkConflict:
            raise PrivateWorkConflict(context.request_id) from None
        except PrivateWorkError:
            raise
        except Exception:
            raise PrivateWorkUnavailable(context.request_id) from None

        target_config = self._checkpoint_config(target_thread_id)
        source_channel_versions = source_item.checkpoint.get("channel_versions", {})
        if not isinstance(source_channel_versions, Mapping):
            source_channel_versions = {}
        try:
            await saver.aput(
                target_config,
                source_item.checkpoint,
                source_item.metadata,
                dict(source_channel_versions),
            )
        except Exception as exc:
            await self._compensate_create(
                context,
                target_thread_id,
                source_thread_id=source_thread_id,
            )
            if isinstance(exc, PrivateWorkError):
                raise
            raise PrivateWorkUnavailable(context.request_id) from None
        return record

    @staticmethod
    async def _raise_if_branch_target_exists(
        session: AsyncSession,
        context: PrivateWorkContext,
        target_thread_id: str,
    ) -> None:
        coordinates = (
            await session.execute(
                select(
                    ThreadMetaRow.project_id,
                    ThreadMetaRow.owner_user_id,
                )
                .where(ThreadMetaRow.thread_id == target_thread_id)
                .with_for_update(of=ThreadMetaRow)
            )
        ).one_or_none()
        if coordinates is None:
            return
        if coordinates == (context.project_id, str(context.user_id)):
            raise PrivateWorkConflict(context.request_id)
        raise PrivateWorkNotFound(context.request_id)

    @classmethod
    def _classify_branch_checkpoint(
        cls,
        requested_checkpoint_id: str,
        requested_item: object,
        head_item: object | None,
    ) -> BranchCheckpointSelection:
        source_head_checkpoint_id = cls._checkpoint_tuple_id(head_item)
        requested_visible_tail = cls._visible_tail_message(requested_item)
        head_visible_tail = cls._visible_tail_message(head_item)
        selected_assistant_id = cls._message_id(requested_visible_tail) if cls._message_type(requested_visible_tail) == "ai" else None
        head_visible_tail_id = cls._message_id(head_visible_tail)
        is_current_visible_turn = source_head_checkpoint_id is not None and selected_assistant_id is not None and selected_assistant_id == head_visible_tail_id
        return BranchCheckpointSelection(
            requested_checkpoint_id=requested_checkpoint_id,
            source_head_checkpoint_id=source_head_checkpoint_id,
            source_visible_tail_message_id=selected_assistant_id,
            workspace_clone_mode=("current_thread_authority_copy" if is_current_visible_turn else "historical_skip"),
        )

    @classmethod
    def _visible_tail_message(cls, item: object | None) -> object | None:
        if item is None:
            return None
        checkpoint = getattr(item, "checkpoint", {}) or {}
        if not isinstance(checkpoint, Mapping):
            return None
        channel_values = checkpoint.get("channel_values", {}) or {}
        if not isinstance(channel_values, Mapping):
            return None
        messages = channel_values.get("messages", []) or []
        if not isinstance(messages, list):
            return None
        for message in reversed(messages):
            if cls._message_is_visible(message):
                return message
        return None

    @classmethod
    def _message_is_visible(cls, message: object) -> bool:
        if isinstance(message, Mapping):
            additional_kwargs = message.get("additional_kwargs", {}) or {}
        else:
            additional_kwargs = getattr(message, "additional_kwargs", {}) or {}
        if isinstance(additional_kwargs, Mapping) and additional_kwargs.get("hide_from_ui") is True:
            return False
        return cls._message_type(message) in {"human", "ai"}

    @staticmethod
    def _message_type(message: object | None) -> str | None:
        if message is None:
            return None
        if isinstance(message, Mapping):
            value = message.get("type")
        else:
            value = getattr(message, "type", None)
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _message_id(message: object | None) -> str | None:
        if message is None:
            return None
        if isinstance(message, Mapping):
            value = message.get("id")
        else:
            value = getattr(message, "id", None)
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _checkpoint_tuple_id(item: object | None) -> str | None:
        if item is None:
            return None
        config = getattr(item, "config", {}) or {}
        if isinstance(config, Mapping):
            configurable = config.get("configurable", {})
            if isinstance(configurable, Mapping):
                value = configurable.get("checkpoint_id")
                if isinstance(value, str):
                    return value
        checkpoint = getattr(item, "checkpoint", {}) or {}
        if isinstance(checkpoint, Mapping):
            value = checkpoint.get("id")
            if isinstance(value, str):
                return value
        return None

    async def _compensate_create(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        *,
        source_thread_id: str | None = None,
    ) -> None:
        checkpoint_clean = True
        try:
            await self._project_scoped_checkpointer.for_context(context).adelete_thread(thread_id, expected_version=1)
        except Exception:
            checkpoint_clean = False

        authority_clean = True
        if source_thread_id is not None and self._branch_copy_hook is not None:
            try:
                await self._branch_copy_hook.rollback_branch_authority(
                    context.resource_scope,
                    source_thread_id,
                    thread_id,
                )
            except Exception:
                authority_clean = False

        try:
            async with self._session_factory() as session:
                async with session.begin():
                    repository = PrivateThreadRepository(session)
                    if checkpoint_clean and authority_clean:
                        await repository.purge_compensated_create(
                            scope=context.resource_scope,
                            thread_id=thread_id,
                        )
                    else:
                        await repository.set_checkpoint_delete_status(
                            scope=context.resource_scope,
                            thread_id=thread_id,
                            status="retry_required",
                        )
        except Exception:
            raise PrivateWorkUnavailable(context.request_id) from None
        if not checkpoint_clean or not authority_clean:
            raise PrivateWorkUnavailable(context.request_id)

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
        await require_executable_agent(session, context, agent)
