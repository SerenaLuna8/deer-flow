from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from langgraph.checkpoint.base import empty_checkpoint
from sqlalchemy import or_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.private_work.checkpoint_state import (
    bind_scoped_checkpoint_state,
    bind_transaction_checkpoint_state,
    checkpoint_config,
    snapshot_checkpoint_id,
)
from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.context import (
    PrivateWorkContext,
    require_issued_private_work_context,
    strip_private_client_fields,
)
from app.private_work.errors import (
    PrivateWorkConflict,
    PrivateWorkDefaultAgentUnavailable,
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
from app.projects.context import ProjectContext
from app.shared_assets.default_agent_service import ProjectDefaultAgentService
from app.shared_assets.errors import AssetStorageUnavailable, SharedAssetError
from app.shared_assets.models import (
    AssetKind,
    AssetScope,
    AssetSelection,
    ResolvedAgentSnapshot,
)
from app.shared_assets.resolver import ProjectAssetResolver
from deerflow.agents.memory.snip import MEMORY_ARCHIVE_RECEIPT_KEY
from deerflow.agents.middlewares.token_budget_middleware import (
    TOKEN_BUDGET_USAGE_STATE_KEY,
)
from deerflow.agents.provider_request_contract import (
    PROVIDER_REQUEST_MEASUREMENT_STATE_KEY,
    PROVIDER_REQUEST_PROFILE_STATE_KEY,
)
from deerflow.config.app_config import AppConfig, get_app_config
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.shared_assets import AgentRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.runtime.private_scope import PrivateResourceScope

_BUILTIN_MAIN_AGENT_SOURCE_KEY = "builtin:agent:project-assistant"
_BRANCH_EXCLUDED_STATE_KEYS = frozenset(
    {
        "sandbox",
        "thread_data",
        MEMORY_ARCHIVE_RECEIPT_KEY,
        PROVIDER_REQUEST_MEASUREMENT_STATE_KEY,
        PROVIDER_REQUEST_PROFILE_STATE_KEY,
        TOKEN_BUDGET_USAGE_STATE_KEY,
    }
)


def _copyable_branch_state_values(
    values: Mapping[str, Any],
) -> dict[str, Any]:
    """Exclude Run-bound private authorities from a new Thread branch."""

    return {key: value for key, value in values.items() if key not in _BRANCH_EXCLUDED_STATE_KEYS}


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
        *,
        expected_target_created_at: datetime,
        expected_target_deleted_at: datetime,
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
        self._asset_resolver = ProjectAssetResolver(session_factory)
        self._default_agent_service = ProjectDefaultAgentService(
            session_factory,
            resolver=self._asset_resolver,
        )

    async def create(
        self,
        context: PrivateWorkContext,
        *,
        thread_id: str,
        agent: ThreadAgentRef | None,
        display_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> PrivateThreadRecord:
        context = require_issued_private_work_context(context)
        clean_metadata = {} if metadata is None else strip_private_client_fields(metadata)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    current_project = await self._revalidator.require(
                        session,
                        context,
                        Capability.PRIVATE_WORK_CREATE,
                        lock=True,
                    )
                    selected_agent = agent
                    if selected_agent is None:
                        selected_agent = await self._resolve_default_agent(
                            session,
                            context,
                            current_project,
                        )
                    else:
                        await self._require_executable_agent(
                            session,
                            context,
                            selected_agent,
                        )
                    record = await PrivateThreadRepository(session).create(
                        scope=context.resource_scope,
                        thread_id=thread_id,
                        agent=selected_agent,
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
            await self._compensate_create(
                context,
                thread_id,
                expected_created_at=record.created_at,
            )
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

    async def is_initialized(
        self,
        context: PrivateWorkContext,
        thread_id: str,
    ) -> bool:
        """Return whether Thread metadata and its initial checkpoint both exist."""

        context = require_issued_private_work_context(context)
        if await self.get(context, thread_id) is None:
            return False
        item = await self._project_scoped_checkpointer.for_context(context).aget_tuple(self._checkpoint_config(thread_id))
        return self._checkpoint_tuple_id(item) is not None

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
        replay_base_checkpoint_id: str,
        app_config: AppConfig | None = None,
        display_name: str | None = None,
    ) -> PrivateThreadRecord:
        context = require_issued_private_work_context(context)
        saver = self._project_scoped_checkpointer.for_context(context)
        resolved_app_config = app_config or get_app_config()

        record: PrivateThreadRecord
        source_snapshot: Any
        replay_base_snapshot: Any
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
                    replay_base_item = await saver.aget_tuple_already_authorized(
                        self._checkpoint_config(
                            source_thread_id,
                            checkpoint_id=replay_base_checkpoint_id,
                        ),
                        session=session,
                    )
                    if replay_base_item is None or self._checkpoint_tuple_id(replay_base_item) != replay_base_checkpoint_id:
                        raise PrivateWorkNotFound(context.request_id)
                    state = bind_transaction_checkpoint_state(
                        saver,
                        session,
                        resolved_app_config,
                        as_node="branch",
                    )
                    source_snapshot = await state.aget(
                        checkpoint_config(
                            source_thread_id,
                            checkpoint_id=checkpoint_id,
                        )
                    )
                    if snapshot_checkpoint_id(source_snapshot) != checkpoint_id:
                        raise PrivateWorkNotFound(context.request_id)
                    replay_base_snapshot = await state.aget(
                        checkpoint_config(
                            source_thread_id,
                            checkpoint_id=replay_base_checkpoint_id,
                        )
                    )
                    if snapshot_checkpoint_id(replay_base_snapshot) != replay_base_checkpoint_id:
                        raise PrivateWorkNotFound(context.request_id)
                    try:
                        latest_snapshot = await state.aget(checkpoint_config(source_thread_id))
                    except PrivateWorkError:
                        latest_snapshot = None
                    selection = self._classify_branch_checkpoint(
                        checkpoint_id,
                        source_snapshot,
                        latest_snapshot,
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

        try:
            target_state = bind_scoped_checkpoint_state(
                self._project_scoped_checkpointer,
                context,
                resolved_app_config,
                as_node="branch",
            )
            base_values = _copyable_branch_state_values(
                replay_base_snapshot.values,
            )
            selected_values = _copyable_branch_state_values(
                source_snapshot.values,
            )
            await target_state.aupdate(
                checkpoint_config(target_thread_id),
                target_state.replacement_values(
                    base_values,
                    current_values={},
                ),
                as_node="branch",
            )
            await target_state.aupdate(
                checkpoint_config(target_thread_id),
                target_state.replacement_values(
                    selected_values,
                    current_values=base_values,
                ),
                as_node="branch",
            )
        except Exception as exc:
            await self._compensate_create(
                context,
                target_thread_id,
                expected_created_at=record.created_at,
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
        snapshot_values = getattr(item, "values", None)
        if not isinstance(snapshot_values, Mapping):
            return None
        messages = snapshot_values.get("messages", []) or []
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
        expected_created_at: datetime,
        source_thread_id: str | None = None,
    ) -> None:
        saver = self._project_scoped_checkpointer.for_context(context)
        tombstone = None
        try:
            tombstone = await saver.atombstone_compensated_create(
                thread_id,
                expected_version=1,
                expected_created_at=expected_created_at,
            )
        except Exception:
            pass

        tombstone_deleted_at = tombstone.deleted_at if tombstone is not None else None
        authority_clean = tombstone_deleted_at is not None
        if tombstone is not None and tombstone_deleted_at is not None and source_thread_id is not None and self._branch_copy_hook is not None:
            try:
                await self._branch_copy_hook.rollback_branch_authority(
                    context.resource_scope,
                    source_thread_id,
                    thread_id,
                    expected_target_created_at=tombstone.created_at,
                    expected_target_deleted_at=tombstone_deleted_at,
                )
            except Exception:
                authority_clean = False

        checkpoint_clean = False
        if tombstone is not None and tombstone_deleted_at is not None and authority_clean:
            try:
                checkpoint_clean = await saver.acleanup_compensated_create(
                    thread_id,
                    expected_created_at=tombstone.created_at,
                    expected_deleted_at=tombstone_deleted_at,
                )
            except Exception:
                checkpoint_clean = False

        try:
            async with self._session_factory() as session:
                async with session.begin():
                    if tombstone is not None and tombstone_deleted_at is not None and authority_clean and checkpoint_clean:
                        await PrivateThreadRepository(
                            session,
                        ).purge_compensated_create(
                            scope=context.resource_scope,
                            thread_id=thread_id,
                            expected_created_at=tombstone.created_at,
                            expected_deleted_at=tombstone_deleted_at,
                        )
        except Exception:
            raise PrivateWorkUnavailable(context.request_id) from None
        if tombstone is None or tombstone_deleted_at is None or not authority_clean or not checkpoint_clean:
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

    async def _resolve_default_agent(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        actor: ProjectContext,
    ) -> ThreadAgentRef:
        builtin_main_fallback = False
        try:
            configured = await self._default_agent_service.resolve_configured_agent_in_session(
                session,
                actor,
            )
        except AssetStorageUnavailable:
            raise PrivateWorkUnavailable(context.request_id) from None
        except SharedAssetError:
            raise PrivateWorkDefaultAgentUnavailable(context.request_id) from None

        if configured is not None:
            selected = ThreadAgentRef(configured.asset_id, "project")
        else:
            builtin_main_fallback = True
            builtin_main_id = (
                await session.execute(
                    select(AgentRow.id)
                    .where(
                        AgentRow.scope == "system",
                        AgentRow.project_id.is_(None),
                        AgentRow.source_key == _BUILTIN_MAIN_AGENT_SOURCE_KEY,
                    )
                    .with_for_update(read=True, of=AgentRow)
                )
            ).scalar_one_or_none()
            if builtin_main_id is None:
                raise PrivateWorkDefaultAgentUnavailable(context.request_id)
            try:
                resolved_main = await self._asset_resolver.resolve_project_asset_snapshot_in_session(
                    session,
                    actor,
                    AssetSelection(AssetKind.AGENT, builtin_main_id),
                )
            except AssetStorageUnavailable:
                raise PrivateWorkUnavailable(context.request_id) from None
            except SharedAssetError:
                raise PrivateWorkDefaultAgentUnavailable(context.request_id) from None
            if not isinstance(resolved_main, ResolvedAgentSnapshot) or resolved_main.scope is not AssetScope.SYSTEM or resolved_main.asset_id != builtin_main_id:
                raise PrivateWorkDefaultAgentUnavailable(context.request_id)
            selected = ThreadAgentRef(resolved_main.asset_id, "system")

        if not builtin_main_fallback:
            try:
                await require_executable_agent(session, context, selected)
            except PrivateWorkNotFound:
                raise PrivateWorkDefaultAgentUnavailable(context.request_id) from None
        return selected

    @staticmethod
    async def _require_executable_agent(
        session: AsyncSession,
        context: PrivateWorkContext,
        agent: ThreadAgentRef,
    ) -> None:
        await require_executable_agent(session, context, agent)
