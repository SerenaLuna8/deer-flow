"""Project-scoped chat controls that never restore the legacy global API."""

from __future__ import annotations

import copy
import json
import logging
import uuid
from collections.abc import Mapping
from typing import Any

from langchain_core.messages import BaseMessage
from langgraph.checkpoint.base import CheckpointTuple, uuid6
from sqlalchemy import or_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import deerflow.utils.llm_text as llm_text
from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.context import PrivateWorkContext, require_issued_private_work_context
from app.private_work.errors import (
    PrivateWorkAssetStale,
    PrivateWorkConflict,
    PrivateWorkError,
    PrivateWorkInvalid,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from app.private_work.revalidation import PrivateWorkRevalidator
from app.private_work.snapshot_repository import RunSnapshotAssetStale, RunSnapshotRepository
from app.private_work.thread_repository import PrivateThreadRecord, PrivateThreadRepository
from app.private_work.thread_service import PrivateThreadService
from app.projects.capabilities import Capability
from app.shared_assets.errors import (
    AssetForbidden,
    AssetResolutionUnavailable,
    AssetStorageUnavailable,
    AssetValidationFailed,
)
from app.shared_assets.models import AssetKind, AssetSelection, ResolvedAgentSnapshot
from app.shared_assets.resolver import ProjectAssetResolver
from deerflow.config.app_config import AppConfig
from deerflow.mcp_definition_policy import McpEndpointPolicy
from deerflow.persistence.run.model import RunRow
from deerflow.runtime.context_compaction import (
    ContextCompactionDisabled,
    ContextCompactionFailed,
    ThreadCompactionResult,
    commit_thread_compaction,
    prepare_thread_compaction,
)
from deerflow.runtime.events.store import RunEventStore
from deerflow.runtime.goal import DEFAULT_MAX_GOAL_CONTINUATIONS, build_goal_state
from deerflow.utils.messages import ORIGINAL_USER_CONTENT_KEY, get_original_user_content_text, message_to_text
from deerflow.utils.oneshot_llm import run_oneshot_llm
from deerflow.utils.time import now_iso

logger = logging.getLogger(__name__)

_HISTORY_SCAN_LIMIT = 200
_SUGGESTION_MESSAGE_LIMIT = 6
_SUGGESTION_MESSAGE_CHARS = 4000
_SUGGESTION_TOTAL_CHARS = 12000


class _CapturedCheckpointReader:
    """Expose one immutable checkpoint to the compaction prepare phase."""

    def __init__(self, item: CheckpointTuple, saver: Any) -> None:
        self._item = item
        self._saver = saver

    async def aget_tuple(self, _config: object) -> CheckpointTuple:
        return self._item

    def get_next_version(self, current: object, channel: object) -> object:
        return self._saver.get_next_version(current, channel)


class _LockedCheckpointWriter:
    """Commit through a saver while its caller-owned Thread lock is held."""

    def __init__(self, saver: Any, session: AsyncSession) -> None:
        self._saver = saver
        self._session = session

    async def aput(
        self,
        config: dict[str, Any],
        checkpoint: dict[str, Any],
        metadata: dict[str, Any],
        new_versions: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._saver.aput_already_authorized(
            config,
            checkpoint,
            metadata,
            new_versions,
            session=self._session,
        )


class ProjectChatControlService:
    """Authority boundary for Goal, Compact, Branch, Regenerate, and Follow-up."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        project_scoped_checkpointer: ProjectScopedCheckpointer,
        thread_service: PrivateThreadService,
        run_event_store: RunEventStore,
        *,
        resolver: ProjectAssetResolver | None = None,
        endpoint_policy: McpEndpointPolicy | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._project_scoped_checkpointer = project_scoped_checkpointer
        self._thread_service = thread_service
        self._run_event_store = run_event_store
        self._resolver = resolver or ProjectAssetResolver(session_factory)
        self._snapshots = RunSnapshotRepository(
            session_factory,
            endpoint_policy=endpoint_policy,
        )
        self._revalidator = PrivateWorkRevalidator()

    async def get_goal(
        self,
        context: PrivateWorkContext,
        thread_id: str,
    ) -> dict[str, Any] | None:
        context = require_issued_private_work_context(context)
        item = await self._saver(context).aget_tuple(self._checkpoint_config(thread_id))
        if item is None:
            raise PrivateWorkNotFound(context.request_id)
        channel_values = self._channel_values(item)
        goal = channel_values.get("goal")
        return copy.deepcopy(goal) if isinstance(goal, dict) else None

    async def set_goal(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        *,
        objective: str,
        max_continuations: int = DEFAULT_MAX_GOAL_CONTINUATIONS,
    ) -> dict[str, Any]:
        context = require_issued_private_work_context(context)
        try:
            goal = build_goal_state(
                objective,
                max_continuations=max_continuations,
            )
        except (TypeError, ValueError):
            raise PrivateWorkInvalid(context.request_id) from None
        await self._write_goal(context, thread_id, goal)
        return goal

    async def clear_goal(
        self,
        context: PrivateWorkContext,
        thread_id: str,
    ) -> None:
        context = require_issued_private_work_context(context)
        await self._write_goal(context, thread_id, None)

    async def _write_goal(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        goal: dict[str, Any] | None,
    ) -> None:
        saver = self._saver(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._lock_thread(
                    session,
                    context,
                    thread_id,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.SHARED_ASSETS_EXECUTE,
                    reject_incomplete_run=True,
                )
                item = await saver.aget_tuple_already_authorized(
                    self._checkpoint_config(thread_id),
                    session=session,
                )
                if item is None:
                    raise PrivateWorkNotFound(context.request_id)
                checkpoint = copy.deepcopy(getattr(item, "checkpoint", {}) or {})
                metadata = copy.deepcopy(getattr(item, "metadata", {}) or {})
                channel_values = dict(checkpoint.get("channel_values", {}) or {})
                if goal is None:
                    channel_values.pop("goal", None)
                else:
                    channel_values["goal"] = copy.deepcopy(goal)
                channel_versions = dict(checkpoint.get("channel_versions", {}) or {})
                next_version = saver.get_next_version(channel_versions.get("goal"), None)
                channel_versions["goal"] = next_version
                checkpoint["channel_values"] = channel_values
                checkpoint["channel_versions"] = channel_versions
                checkpoint["id"] = str(uuid6())
                metadata["updated_at"] = now_iso()
                metadata["source"] = "update"
                current_step = metadata.get("step")
                metadata["step"] = current_step + 1 if isinstance(current_step, int) else 1
                metadata["writes"] = {"goal": {"goal": goal}}
                await saver.aput_already_authorized(
                    self._checkpoint_config(thread_id),
                    checkpoint,
                    metadata,
                    {"goal": next_version},
                    session=session,
                )
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def compact(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        *,
        force: bool,
        keep: tuple[str, int | float] | None,
        app_config: AppConfig,
    ) -> ThreadCompactionResult:
        """Prepare outside locks, then compare-and-swap under a second lock."""

        context = require_issued_private_work_context(context)
        saver = self._saver(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._lock_thread(
                    session,
                    context,
                    thread_id,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.SHARED_ASSETS_EXECUTE,
                    reject_incomplete_run=True,
                )
                source = await saver.aget_tuple_already_authorized(
                    self._checkpoint_config(thread_id),
                    session=session,
                )
                if source is None:
                    raise PrivateWorkNotFound(context.request_id)

            prepared = await prepare_thread_compaction(
                _CapturedCheckpointReader(source, saver),
                thread_id,
                keep=keep,
                force=force,
                user_id=str(context.user_id),
                app_config=app_config,
            )
            if not prepared.result.compacted:
                return prepared.result

            async with self._session_factory() as session, session.begin():
                await self._lock_thread(
                    session,
                    context,
                    thread_id,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.SHARED_ASSETS_EXECUTE,
                    reject_incomplete_run=True,
                )
                current = await saver.aget_tuple_already_authorized(
                    self._checkpoint_config(thread_id),
                    session=session,
                )
                if current is None:
                    raise PrivateWorkNotFound(context.request_id)
                if self._checkpoint_id(current) != prepared.source_checkpoint_id:
                    raise PrivateWorkConflict(context.request_id)
                return await commit_thread_compaction(
                    _LockedCheckpointWriter(saver, session),
                    prepared,
                )
        except ContextCompactionDisabled:
            raise PrivateWorkConflict(context.request_id) from None
        except ContextCompactionFailed:
            raise PrivateWorkUnavailable(context.request_id) from None
        except LookupError:
            raise PrivateWorkNotFound(context.request_id) from None
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception:
            logger.exception(
                "Project context compaction failed: request_id=%s",
                context.request_id,
            )
            raise PrivateWorkUnavailable(context.request_id) from None

    async def branch(
        self,
        context: PrivateWorkContext,
        source_thread_id: str,
        *,
        message_id: str,
        message_ids: list[str],
        title: str | None,
    ) -> tuple[PrivateThreadRecord, str]:
        context = require_issued_private_work_context(context)
        source = await self._thread_service.get(context, source_thread_id)
        if source is None:
            raise PrivateWorkNotFound(context.request_id)
        target_ids = {message_id, *message_ids}
        selected = None
        saver = self._saver(context)
        async for item in saver.alist(
            self._checkpoint_config(source_thread_id),
            limit=_HISTORY_SCAN_LIMIT,
        ):
            if self._matches_branch_target(self._messages(item), target_ids):
                selected = item
                break
        checkpoint_id = self._checkpoint_id(selected)
        if checkpoint_id is None:
            raise PrivateWorkConflict(context.request_id)
        target_thread_id = str(uuid.uuid4())
        record = await self._thread_service.branch(
            context,
            source_thread_id=source_thread_id,
            target_thread_id=target_thread_id,
            checkpoint_id=checkpoint_id,
            expected_source_version=source.version,
            display_name=title or source.display_name,
        )
        return record, checkpoint_id

    async def prepare_regenerate(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        *,
        message_id: str,
    ) -> dict[str, Any]:
        context = require_issued_private_work_context(context)
        await self._validate_control_authority(
            context,
            thread_id,
            reject_incomplete_run=True,
        )
        saver = self._saver(context)
        latest = await saver.aget_tuple(self._checkpoint_config(thread_id))
        if latest is None:
            raise PrivateWorkNotFound(context.request_id)
        messages = self._messages(latest)
        target_index = next(
            (index for index, message in enumerate(messages) if self._message_id(message) == message_id),
            None,
        )
        if target_index is None:
            raise PrivateWorkNotFound(context.request_id)
        target = messages[target_index]
        if not self._is_visible_ai(target):
            raise PrivateWorkConflict(context.request_id)
        latest_ai = next((message for message in reversed(messages) if self._is_visible_ai(message)), None)
        if self._message_id(latest_ai) != message_id:
            raise PrivateWorkConflict(context.request_id)
        human = next(
            (message for message in reversed(messages[:target_index]) if self._is_visible_human(message)),
            None,
        )
        human_id = self._message_id(human)
        if human is None or human_id is None:
            raise PrivateWorkConflict(context.request_id)
        base = await self._find_checkpoint_before_message(
            saver,
            thread_id,
            human_id,
            context.request_id,
        )
        target_run_id = await self._find_target_run_id(
            context,
            thread_id,
            message_id,
        )
        checkpoint = self._checkpoint_response(base, context.request_id)
        return {
            "input": {"messages": [self._clean_human_message(human)]},
            "checkpoint": checkpoint,
            "metadata": {
                "regenerate_from_message_id": message_id,
                "regenerate_from_run_id": target_run_id,
                "regenerate_checkpoint_id": checkpoint["checkpoint_id"],
            },
            "target_run_id": target_run_id,
        }

    async def suggest(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        *,
        n: int,
        app_config: AppConfig,
    ) -> list[str]:
        context = require_issued_private_work_context(context)
        if not app_config.suggestions.enabled:
            return []
        saver = self._saver(context)
        item = await saver.aget_tuple(self._checkpoint_config(thread_id))
        if item is None:
            raise PrivateWorkNotFound(context.request_id)
        conversation = self._suggestion_conversation(self._messages(item))
        if not conversation:
            return []
        resolved = await self._resolve_agent_authority(context, thread_id)
        system_instruction = (
            "You are generating follow-up questions to help the user continue the conversation.\n"
            f"Based on the conversation below, produce EXACTLY {n} short questions the user might ask next.\n"
            "Requirements:\n"
            "- Questions must be relevant to the preceding conversation.\n"
            "- Questions must be written in the same language as the user.\n"
            "- Keep each question concise (ideally <= 20 words / <= 40 Chinese characters).\n"
            "- Do NOT include numbering, markdown, or any extra text.\n"
            "- Output MUST be a JSON array of strings only."
        )
        try:
            raw = await run_oneshot_llm(
                system_instruction=system_instruction,
                user_content=f"Conversation Context:\n{conversation}\n\nGenerate {n} follow-up questions",
                run_name="project_suggest_agent",
                app_config=app_config,
                model_name=resolved.payload.model_ref,
                thread_id=thread_id,
            )
        except Exception:
            logger.exception(
                "Project suggestion model call failed: request_id=%s",
                context.request_id,
            )
            return []
        suggestions = self._parse_json_string_list(raw) or []
        return [text.replace("\n", " ").strip() for text in suggestions if text.strip()][:n]

    async def _validate_control_authority(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        *,
        reject_incomplete_run: bool,
    ) -> None:
        try:
            async with self._session_factory() as session, session.begin():
                await self._lock_thread(
                    session,
                    context,
                    thread_id,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.SHARED_ASSETS_EXECUTE,
                    reject_incomplete_run=reject_incomplete_run,
                )
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def _resolve_agent_authority(
        self,
        context: PrivateWorkContext,
        thread_id: str,
    ) -> ResolvedAgentSnapshot:
        try:
            async with self._session_factory() as session, session.begin():
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
                resolved = await self._resolver.resolve_project_asset_snapshot_in_session(
                    session,
                    current,
                    AssetSelection(AssetKind.AGENT, thread.agent_asset_id),
                )
                if type(resolved) is not ResolvedAgentSnapshot or resolved.scope.value != thread.agent_scope:
                    raise PrivateWorkAssetStale(context.request_id)
                await self._snapshots.validate_agent_closure_in_session(
                    session,
                    context,
                    resolved,
                )
                return resolved
        except PrivateWorkError:
            raise
        except (AssetForbidden, AssetValidationFailed, AssetResolutionUnavailable, RunSnapshotAssetStale):
            raise PrivateWorkAssetStale(context.request_id) from None
        except (AssetStorageUnavailable, DBAPIError):
            raise PrivateWorkUnavailable(context.request_id) from None

    async def _lock_thread(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        thread_id: str,
        *capabilities: Capability,
        reject_incomplete_run: bool,
    ) -> PrivateThreadRecord:
        await self._revalidator.require(
            session,
            context,
            *capabilities,
            lock=True,
        )
        thread = await PrivateThreadRepository(session).get(
            scope=context.resource_scope,
            thread_id=thread_id,
            lock=True,
        )
        if thread is None:
            raise PrivateWorkNotFound(context.request_id)
        if reject_incomplete_run:
            incomplete = (
                await session.execute(
                    select(RunRow.run_id)
                    .where(
                        RunRow.project_id == context.project_id,
                        RunRow.owner_user_id == str(context.user_id),
                        RunRow.thread_id == thread_id,
                        or_(
                            RunRow.status.in_(("pending", "running")),
                            RunRow.finalization_status == "finalizing",
                        ),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if incomplete is not None:
                raise PrivateWorkConflict(context.request_id)
        return thread

    async def _find_checkpoint_before_message(
        self,
        saver: Any,
        thread_id: str,
        message_id: str,
        request_id: str,
    ) -> CheckpointTuple:
        checkpoints = [
            item
            async for item in saver.alist(
                self._checkpoint_config(thread_id),
                limit=_HISTORY_SCAN_LIMIT,
            )
        ]
        previous = None
        for item in reversed(checkpoints):
            if message_id in {self._message_id(message) for message in self._messages(item)}:
                if previous is None:
                    raise PrivateWorkConflict(request_id)
                return previous
            if self._checkpoint_id(item) is not None:
                previous = item
        raise PrivateWorkConflict(request_id)

    async def _find_target_run_id(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        message_id: str,
    ) -> str:
        try:
            rows = await self._run_event_store.list_messages(
                thread_id,
                limit=_HISTORY_SCAN_LIMIT,
                scope=context.resource_scope,
            )
        except Exception:
            raise PrivateWorkUnavailable(context.request_id) from None
        for row in reversed(rows):
            if row.get("event_type") not in {"ai_message", "llm.ai.response"}:
                continue
            if self._event_message_id(row) != message_id:
                continue
            run_id = row.get("run_id")
            if isinstance(run_id, str) and run_id:
                return run_id
        raise PrivateWorkConflict(context.request_id)

    @classmethod
    def _suggestion_conversation(cls, messages: list[Any]) -> str:
        parts: list[str] = []
        total = 0
        visible = [message for message in messages if cls._is_visible_human(message) or cls._is_visible_ai(message)][-_SUGGESTION_MESSAGE_LIMIT:]
        for message in visible:
            content = message_to_text(message).strip()[:_SUGGESTION_MESSAGE_CHARS]
            if not content:
                continue
            role = "User" if cls._is_visible_human(message) else "Assistant"
            remaining = _SUGGESTION_TOTAL_CHARS - total
            if remaining <= 0:
                break
            line = f"{role}: {content}"[:remaining]
            parts.append(line)
            total += len(line)
        return "\n".join(parts)

    @staticmethod
    def _parse_json_string_list(text: str) -> list[str] | None:
        candidate = llm_text.strip_markdown_code_fence(llm_text.strip_think_blocks(text))
        start = candidate.find("[")
        end = candidate.rfind("]")
        if start == -1 or end <= start:
            return None
        try:
            payload = json.loads(candidate[start : end + 1])
        except Exception:
            return None
        if not isinstance(payload, list):
            return None
        return [item.strip() for item in payload if isinstance(item, str) and item.strip()]

    @staticmethod
    def _checkpoint_config(thread_id: str) -> dict[str, Any]:
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": "",
            }
        }

    @staticmethod
    def _checkpoint_id(item: object | None) -> str | None:
        if item is None:
            return None
        config = getattr(item, "config", {}) or {}
        configurable = config.get("configurable", {}) if isinstance(config, Mapping) else {}
        value = configurable.get("checkpoint_id") if isinstance(configurable, Mapping) else None
        if isinstance(value, str) and value:
            return value
        checkpoint = getattr(item, "checkpoint", {}) or {}
        value = checkpoint.get("id") if isinstance(checkpoint, Mapping) else None
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _channel_values(item: object) -> dict[str, Any]:
        checkpoint = getattr(item, "checkpoint", {}) or {}
        values = checkpoint.get("channel_values", {}) if isinstance(checkpoint, Mapping) else {}
        return dict(values) if isinstance(values, Mapping) else {}

    @classmethod
    def _messages(cls, item: object) -> list[Any]:
        messages = cls._channel_values(item).get("messages", [])
        return list(messages) if isinstance(messages, list) else []

    @staticmethod
    def _message_id(message: object | None) -> str | None:
        if message is None:
            return None
        value = message.get("id") if isinstance(message, Mapping) else getattr(message, "id", None)
        return str(value) if value else None

    @staticmethod
    def _message_type(message: object | None) -> str | None:
        if message is None:
            return None
        if isinstance(message, Mapping):
            value = message.get("type") or message.get("role")
        else:
            value = getattr(message, "type", None)
        if value == "assistant":
            return "ai"
        if value == "user":
            return "human"
        return str(value) if value else None

    @staticmethod
    def _message_name(message: object) -> str | None:
        value = message.get("name") if isinstance(message, Mapping) else getattr(message, "name", None)
        return str(value) if value else None

    @staticmethod
    def _message_content(message: object) -> object:
        return message.get("content") if isinstance(message, Mapping) else getattr(message, "content", None)

    @staticmethod
    def _additional_kwargs(message: object) -> dict[str, Any]:
        if isinstance(message, Mapping):
            value = message.get("additional_kwargs")
        else:
            value = getattr(message, "additional_kwargs", None)
        return dict(value) if isinstance(value, Mapping) else {}

    @classmethod
    def _hidden(cls, message: object) -> bool:
        return cls._message_type(message) == "remove" or cls._message_name(message) == "summary" or cls._additional_kwargs(message).get("hide_from_ui") is True

    @classmethod
    def _is_visible_human(cls, message: object) -> bool:
        return cls._message_type(message) == "human" and not cls._hidden(message)

    @classmethod
    def _is_visible_ai(cls, message: object) -> bool:
        return cls._message_type(message) == "ai" and not cls._hidden(message)

    @classmethod
    def _matches_branch_target(
        cls,
        messages: list[Any],
        target_ids: set[str],
    ) -> bool:
        if not target_ids:
            return False
        indices = {message_id: index for index, message in enumerate(messages) if (message_id := cls._message_id(message)) is not None}
        if not target_ids.issubset(indices):
            return False
        if any(not cls._is_visible_ai(messages[indices[message_id]]) for message_id in target_ids):
            return False
        target_end = max(indices[message_id] for message_id in target_ids)
        return not any(cls._is_visible_human(message) or cls._is_visible_ai(message) for message in messages[target_end + 1 :])

    @classmethod
    def _clean_human_message(cls, message: object) -> dict[str, Any]:
        additional_kwargs = cls._additional_kwargs(message)
        content = get_original_user_content_text(
            cls._message_content(message),
            additional_kwargs,
        )
        additional_kwargs.pop(ORIGINAL_USER_CONTENT_KEY, None)
        additional_kwargs.pop("hide_from_ui", None)
        clean: dict[str, Any] = {
            "type": "human",
            "content": [{"type": "text", "text": content}],
            "additional_kwargs": additional_kwargs,
        }
        if message_id := cls._message_id(message):
            clean["id"] = message_id
        if name := cls._message_name(message):
            clean["name"] = name
        return clean

    @classmethod
    def _event_message_id(cls, row: dict[str, Any]) -> str | None:
        content = row.get("content")
        if isinstance(content, (BaseMessage, Mapping)):
            return cls._message_id(content)
        return None

    @classmethod
    def _checkpoint_response(
        cls,
        item: object,
        request_id: str,
    ) -> dict[str, Any]:
        config = getattr(item, "config", {}) or {}
        configurable = config.get("configurable", {}) if isinstance(config, Mapping) else {}
        checkpoint_id = configurable.get("checkpoint_id") if isinstance(configurable, Mapping) else None
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            raise PrivateWorkConflict(request_id)
        return {
            "checkpoint_ns": str(configurable.get("checkpoint_ns") or ""),
            "checkpoint_id": checkpoint_id,
            "checkpoint_map": configurable.get("checkpoint_map"),
        }

    def _saver(self, context: PrivateWorkContext) -> Any:
        return self._project_scoped_checkpointer.for_context(context)


__all__ = ["ProjectChatControlService"]
