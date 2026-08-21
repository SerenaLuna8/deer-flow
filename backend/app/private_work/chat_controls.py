"""Project-scoped chat controls that never restore the legacy global API."""

from __future__ import annotations

import copy
import json
import logging
import uuid
from collections.abc import Mapping
from typing import Any

from langchain_core.messages import BaseMessage
from langgraph.types import Overwrite
from sqlalchemy import or_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import deerflow.utils.llm_text as llm_text
from app.personalization.repository import AccountPersonalizationRepository
from app.private_work.authorization import PrivateRequestAuthorizationBoundary
from app.private_work.checkpoint_lineage import (
    CheckpointLineageError,
    find_settled_checkpoint_before_message,
)
from app.private_work.checkpoint_state import (
    bind_scoped_checkpoint_state,
    bind_transaction_checkpoint_state,
    checkpoint_config,
    snapshot_checkpoint_id,
)
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
from app.private_work.run_repository import PrivateRunRecord, PrivateRunRepository
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
from app.shared_assets.model_refs import ConfiguredModelRefResolver
from app.shared_assets.models import AssetKind, AssetSelection, ResolvedAgentSnapshot
from app.shared_assets.resolver import ProjectAssetResolver
from app.system_settings import (
    SystemModelMaterializationUnavailable,
    SystemModelMaterializer,
)
from deerflow.agents.memory.snip import SnipArchiveContext
from deerflow.config.app_config import AppConfig, get_app_config
from deerflow.mcp_definition_policy import McpEndpointPolicy
from deerflow.models import ModelRuntimeProfile
from deerflow.persistence.private_work.memory_document_repository import (
    DEFAULT_MEMORY_NAMESPACE,
)
from deerflow.persistence.run.model import RunRow
from deerflow.runtime.context_compaction import (
    ContextCompactionDisabled,
    ContextCompactionFailed,
    ThreadCompactionResult,
    ThreadContextUsage,
    commit_thread_compaction,
    has_complete_turns,
    measure_thread_context_usage,
    prepare_thread_compaction,
)
from deerflow.runtime.events.store import RunEventStore
from deerflow.runtime.goal import DEFAULT_MAX_GOAL_CONTINUATIONS, build_goal_state
from deerflow.sandbox.sandbox import AuthorizationRevoked
from deerflow.utils.messages import ORIGINAL_USER_CONTENT_KEY, get_original_user_content_text, message_to_text
from deerflow.utils.oneshot_llm import run_oneshot_llm

logger = logging.getLogger(__name__)

_HISTORY_SCAN_LIMIT = 200
_SUGGESTION_MESSAGE_LIMIT = 6
_SUGGESTION_MESSAGE_CHARS = 4000
_SUGGESTION_TOTAL_CHARS = 12000


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
        model_materializer: SystemModelMaterializer | None = None,
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
        self._model_materializer = model_materializer

    async def get_goal(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        *,
        app_config: AppConfig | None = None,
    ) -> dict[str, Any] | None:
        context = require_issued_private_work_context(context)
        snapshot = await self._state(
            context,
            app_config or get_app_config(),
            as_node="goal",
        ).aget(checkpoint_config(thread_id))
        if snapshot_checkpoint_id(snapshot) is None:
            raise PrivateWorkNotFound(context.request_id)
        goal = (snapshot.values or {}).get("goal")
        return copy.deepcopy(goal) if isinstance(goal, dict) else None

    async def set_goal(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        *,
        objective: str,
        max_continuations: int = DEFAULT_MAX_GOAL_CONTINUATIONS,
        app_config: AppConfig | None = None,
    ) -> dict[str, Any]:
        context = require_issued_private_work_context(context)
        try:
            goal = build_goal_state(
                objective,
                max_continuations=max_continuations,
            )
        except (TypeError, ValueError):
            raise PrivateWorkInvalid(context.request_id) from None
        await self._write_goal(
            context,
            thread_id,
            goal,
            app_config=app_config or get_app_config(),
        )
        return goal

    async def clear_goal(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        *,
        app_config: AppConfig | None = None,
    ) -> None:
        context = require_issued_private_work_context(context)
        await self._write_goal(
            context,
            thread_id,
            None,
            app_config=app_config or get_app_config(),
        )

    async def _write_goal(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        goal: dict[str, Any] | None,
        *,
        app_config: AppConfig,
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
                state = bind_transaction_checkpoint_state(
                    saver,
                    session,
                    app_config,
                    as_node="goal",
                )
                snapshot = await state.aget(checkpoint_config(thread_id))
                parent_checkpoint_id = snapshot_checkpoint_id(snapshot)
                if parent_checkpoint_id is None:
                    raise PrivateWorkNotFound(context.request_id)
                await state.aupdate(
                    snapshot.config,
                    {"goal": Overwrite(copy.deepcopy(goal))},
                    as_node="goal",
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
        authorization_boundary = PrivateRequestAuthorizationBoundary(
            lambda: self._validate_control_authority(
                context,
                thread_id,
                reject_incomplete_run=True,
            ),
            request_id=context.request_id,
        )
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
                locked_state = bind_transaction_checkpoint_state(
                    saver,
                    session,
                    app_config,
                    as_node="manual_compaction",
                )
                source = await locked_state.aget(checkpoint_config(thread_id))
                source_checkpoint_id = snapshot_checkpoint_id(source)
                if source_checkpoint_id is None:
                    raise PrivateWorkNotFound(context.request_id)
                preference = await AccountPersonalizationRepository(
                    session,
                ).read_memory(context.user_id)

            runtime_config = await self._materialize_compaction_config(
                context,
                thread_id,
                app_config,
            )
            archive_context = self._compaction_archive_context(
                context,
                runtime_config,
                preference_version=preference.version,
                preference_enabled=preference.memory_enabled,
                source_checkpoint_id=source_checkpoint_id,
            )
            prepared = await prepare_thread_compaction(
                self._state(
                    context,
                    runtime_config,
                    as_node="manual_compaction",
                ),
                thread_id,
                keep=keep,
                force=force,
                user_id=str(context.user_id),
                app_config=runtime_config,
                snapshot=source,
                authorization_boundary=authorization_boundary,
                memory_archive_context=archive_context,
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
                locked_state = bind_transaction_checkpoint_state(
                    saver,
                    session,
                    runtime_config,
                    as_node="manual_compaction",
                )
                current = await locked_state.aget(checkpoint_config(thread_id))
                if snapshot_checkpoint_id(current) is None:
                    raise PrivateWorkNotFound(context.request_id)
                if snapshot_checkpoint_id(current) != prepared.source_checkpoint_id:
                    raise PrivateWorkConflict(context.request_id)
                return await commit_thread_compaction(
                    locked_state,
                    prepared,
                )
        except AuthorizationRevoked:
            raise authorization_boundary.private_error() from None
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

    async def context_usage(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        *,
        app_config: AppConfig,
    ) -> ThreadContextUsage:
        """Measure the authorized materialized Thread head without writing it."""

        context = require_issued_private_work_context(context)
        try:
            await self._validate_control_authority(
                context,
                thread_id,
                reject_incomplete_run=False,
            )
            runtime_config = await self._materialize_compaction_config(
                context,
                thread_id,
                app_config,
            )
            snapshot = await self._state(
                context,
                runtime_config,
                as_node="context_usage",
            ).aget(checkpoint_config(thread_id))
            if snapshot_checkpoint_id(snapshot) is None:
                raise PrivateWorkNotFound(context.request_id)
            return measure_thread_context_usage(
                snapshot,
                app_config=runtime_config,
            )
        except PrivateWorkError:
            raise
        except (ContextCompactionFailed, ValueError):
            raise PrivateWorkUnavailable(context.request_id) from None
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception:
            logger.exception(
                "Project context usage measurement failed: request_id=%s",
                context.request_id,
            )
            raise PrivateWorkUnavailable(context.request_id) from None

    async def _materialize_compaction_config(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        app_config: AppConfig,
    ) -> AppConfig:
        """Bind manual compaction to one current, exact model definition.

        A dedicated summarization model wins when the current database policy
        selects one. Otherwise the thread's current Agent model is the
        authoritative fallback. Production composition always supplies the
        system materializer; the unmaterialized branch exists only for
        isolated service tests that inject an exact ``AppConfig`` directly.
        """

        model_ref = app_config.summarization.model_name
        if model_ref is None:
            resolved = await self._resolve_agent_authority(context, thread_id)
            model_ref = resolved.payload.model_ref
        if self._model_materializer is None:
            return app_config
        try:
            runtime_model = await self._model_materializer.materialize_active(
                model_ref,
            )
        except SystemModelMaterializationUnavailable:
            raise PrivateWorkUnavailable(context.request_id) from None
        if runtime_model.name != model_ref and model_ref != "default":
            raise PrivateWorkUnavailable(context.request_id)
        return app_config.with_runtime_models((runtime_model,))

    def _compaction_archive_context(
        self,
        context: PrivateWorkContext,
        app_config: AppConfig,
        *,
        preference_version: int,
        preference_enabled: bool,
        source_checkpoint_id: str,
    ) -> SnipArchiveContext:
        requested_enabled = bool(app_config.memory.enabled and preference_enabled)
        effective_enabled = requested_enabled
        summary_model_ref: uuid.UUID | None = None
        if requested_enabled:
            model_name = app_config.summarization.model_name
            model = app_config.get_model_config(model_name) if model_name is not None else (app_config.models[0] if app_config.models else None)
            summary_model_ref = getattr(
                model,
                "_system_model_config_version_id",
                None,
            )
            if not isinstance(summary_model_ref, uuid.UUID):
                if self._model_materializer is not None:
                    raise PrivateWorkUnavailable(context.request_id)
                # Isolated service tests may inject an unmaterialized AppConfig.
                # They retain Thread compaction but cannot author durable
                # history without an exact PostgreSQL model-version identity.
                effective_enabled = False
        return SnipArchiveContext(
            enabled=effective_enabled,
            project_id=context.project_id,
            owner_user_id=str(context.user_id),
            namespace=DEFAULT_MEMORY_NAMESPACE,
            preference_version=preference_version,
            summary_model_ref=summary_model_ref,
            source_checkpoint_id=source_checkpoint_id,
        )

    async def lock_and_verify_dream_archive_ready(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        thread_id: str,
        *,
        app_config: AppConfig,
    ) -> bool:
        """Lock one Thread and verify its current head has no complete turns.

        The checkpoint read uses the caller-owned SQL transaction, so any
        carried archive receipt is repaired before this method returns. The
        caller must keep the transaction open through Dream admission; every
        production checkpoint writer also locks the Thread row and therefore
        cannot race a new head between this proof and admission.
        """

        context = require_issued_private_work_context(context)
        if not session.in_transaction():
            raise PrivateWorkUnavailable(context.request_id)
        await self._lock_thread(
            session,
            context,
            thread_id,
            Capability.PRIVATE_WORK_CREATE,
            Capability.SHARED_ASSETS_EXECUTE,
            reject_incomplete_run=True,
        )
        state = bind_transaction_checkpoint_state(
            self._saver(context),
            session,
            app_config,
            as_node="dream_archive_barrier",
        )
        snapshot = await state.aget(checkpoint_config(thread_id))
        if snapshot_checkpoint_id(snapshot) is None:
            raise PrivateWorkNotFound(context.request_id)
        messages = (snapshot.values or {}).get("messages")
        if messages is None:
            return True
        if not isinstance(messages, list):
            raise PrivateWorkUnavailable(context.request_id)
        return not has_complete_turns(messages)

    async def branch(
        self,
        context: PrivateWorkContext,
        source_thread_id: str,
        *,
        message_id: str,
        message_ids: list[str],
        title: str | None,
        app_config: AppConfig | None = None,
    ) -> tuple[PrivateThreadRecord, str]:
        context = require_issued_private_work_context(context)
        resolved_app_config = app_config or get_app_config()
        source = await self._thread_service.get(context, source_thread_id)
        if source is None:
            raise PrivateWorkNotFound(context.request_id)
        target_ids = {message_id, *message_ids}
        selected = None
        state = self._state(
            context,
            resolved_app_config,
            as_node="branch",
        )
        for snapshot in await state.ahistory(
            checkpoint_config(source_thread_id),
            limit=_HISTORY_SCAN_LIMIT,
        ):
            if self._matches_branch_target(
                self._messages(snapshot),
                target_ids,
            ):
                selected = snapshot
                break
        checkpoint_id = snapshot_checkpoint_id(selected)
        if checkpoint_id is None:
            raise PrivateWorkConflict(context.request_id)
        selected_messages = self._messages(selected)
        target_indices = [index for index, message in enumerate(selected_messages) if self._message_id(message) in target_ids]
        first_target_index = min(target_indices) if target_indices else -1
        branch_human = next(
            (message for message in reversed(selected_messages[:first_target_index]) if self._is_visible_human(message)),
            None,
        )
        branch_human_id = self._message_id(branch_human)
        if branch_human_id is None:
            raise PrivateWorkConflict(context.request_id)
        replay_base = await self._find_checkpoint_before_message(
            state,
            source_thread_id,
            branch_human_id,
            context.request_id,
            head=selected,
        )
        replay_base_checkpoint_id = snapshot_checkpoint_id(replay_base)
        if replay_base_checkpoint_id is None:
            raise PrivateWorkConflict(context.request_id)
        target_thread_id = str(uuid.uuid4())
        record = await self._thread_service.branch(
            context,
            source_thread_id=source_thread_id,
            target_thread_id=target_thread_id,
            checkpoint_id=checkpoint_id,
            replay_base_checkpoint_id=replay_base_checkpoint_id,
            expected_source_version=source.version,
            app_config=resolved_app_config,
            display_name=title or source.display_name,
        )
        return record, checkpoint_id

    async def prepare_regenerate(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        *,
        message_id: str,
        app_config: AppConfig | None = None,
    ) -> dict[str, Any]:
        context = require_issued_private_work_context(context)
        await self._validate_control_authority(
            context,
            thread_id,
            reject_incomplete_run=True,
        )
        state = self._state(
            context,
            app_config or get_app_config(),
            as_node="regenerate",
        )
        latest = await state.aget(checkpoint_config(thread_id))
        if snapshot_checkpoint_id(latest) is None:
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
            state,
            thread_id,
            human_id,
            context.request_id,
            head=latest,
        )
        target_run_id = await self._find_target_run_id(
            context,
            thread_id,
            message_id,
        )
        checkpoint = self._checkpoint_response(base, context.request_id)
        replay_human = self._clean_human_message(human)
        replay_human["id"] = self._replay_input_message_id(base, human_id)
        regenerate_input: dict[str, Any] = {"messages": [replay_human]}
        latest_title = self._channel_values(latest).get("title")
        if isinstance(latest_title, str) and latest_title:
            regenerate_input["title"] = latest_title
        return {
            "input": regenerate_input,
            "checkpoint": checkpoint,
            "metadata": {
                "regenerate_from_message_id": message_id,
                "regenerate_from_run_id": target_run_id,
                "regenerate_checkpoint_id": checkpoint["checkpoint_id"],
            },
            "target_run_id": target_run_id,
        }

    async def prepare_edit_regenerate(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        *,
        human_message_id: str,
        replacement_text: str,
        replacement_base_id: str | None = None,
        app_config: AppConfig | None = None,
    ) -> dict[str, Any]:
        """Prepare one strict edit replay without accepting client authority."""

        context = require_issued_private_work_context(context)
        normalized_text = replacement_text.strip()
        if not normalized_text:
            raise PrivateWorkConflict(context.request_id)
        await self._validate_control_authority(
            context,
            thread_id,
            reject_incomplete_run=True,
        )
        state = self._state(
            context,
            app_config or get_app_config(),
            as_node="edit_regenerate",
        )
        latest = await state.aget(checkpoint_config(thread_id))
        if snapshot_checkpoint_id(latest) is None:
            raise PrivateWorkNotFound(context.request_id)
        latest_values = self._channel_values(latest)
        goal = latest_values.get("goal")
        if isinstance(goal, Mapping) and goal.get("status") == "active":
            raise PrivateWorkConflict(context.request_id)

        source_human, source_ai, source_message_ids = self._latest_editable_turn(
            self._messages(latest),
            human_message_id,
            context.request_id,
        )
        source_text = get_original_user_content_text(
            self._message_content(source_human),
            self._additional_kwargs(source_human),
        ).strip()
        if source_text == normalized_text:
            raise PrivateWorkConflict(context.request_id)
        source_human_id = self._message_id(source_human)
        source_ai_id = self._message_id(source_ai)
        if source_human_id is None or source_ai_id is None:
            raise PrivateWorkConflict(context.request_id)

        base = await self._find_checkpoint_before_message(
            state,
            thread_id,
            source_human_id,
            context.request_id,
            head=latest,
        )
        target_run_id = await self._find_target_run_id(
            context,
            thread_id,
            source_ai_id,
        )
        source_run = await self._require_successful_source_run(
            context,
            thread_id,
            target_run_id,
        )
        checkpoint = self._checkpoint_response(base, context.request_id)
        edit_messages, replacement_human_message_id = self._edit_replay_message_plan(
            base,
            source_human,
            replacement_text=normalized_text,
            replacement_base_id=(replacement_base_id or str(uuid.uuid4())),
        )
        edit_version_group_id = source_run.metadata.get("edit_version_group_id")
        if not isinstance(edit_version_group_id, str) or not edit_version_group_id:
            edit_version_group_id = source_human_id

        edit_input: dict[str, Any] = {"messages": edit_messages}
        base_title = self._channel_values(base).get("title")
        latest_title = latest_values.get("title")
        if isinstance(base_title, str) and base_title and isinstance(latest_title, str) and latest_title:
            edit_input["title"] = latest_title

        return {
            "input": edit_input,
            "checkpoint": checkpoint,
            "metadata": {
                "replay_kind": "edit",
                "regenerate_from_message_id": source_ai_id,
                "regenerate_from_run_id": target_run_id,
                "regenerate_checkpoint_id": checkpoint["checkpoint_id"],
                "edit_from_message_id": source_human_id,
                "edit_message_id": replacement_human_message_id,
                "edit_version_group_id": edit_version_group_id,
            },
            "target_run_id": target_run_id,
            "replacement_human_message_id": replacement_human_message_id,
            "source_message_ids": source_message_ids,
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
        snapshot = await self._state(
            context,
            app_config,
            as_node="suggest",
        ).aget(checkpoint_config(thread_id))
        if snapshot_checkpoint_id(snapshot) is None:
            raise PrivateWorkNotFound(context.request_id)
        conversation = self._suggestion_conversation(self._messages(snapshot))
        if not conversation:
            return []
        resolved = await self._resolve_agent_authority(context, thread_id)
        runtime_config = app_config
        if self._model_materializer is not None:
            try:
                runtime_model = await self._model_materializer.materialize_active(
                    resolved.payload.model_ref,
                )
            except SystemModelMaterializationUnavailable:
                logger.warning(
                    "Project suggestion model is unavailable: request_id=%s",
                    context.request_id,
                )
                return []
            runtime_config = app_config.with_runtime_models((runtime_model,))
            model_name = runtime_model.name
        else:
            model_name = ConfiguredModelRefResolver(app_config).resolve(
                resolved.payload.model_ref,
            )
        if model_name is None:
            logger.warning(
                "Project suggestion model is unavailable: request_id=%s",
                context.request_id,
            )
            return []
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
                app_config=runtime_config,
                model_name=model_name,
                profile=ModelRuntimeProfile.PRIVATE_ONESHOT,
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
        state: Any,
        thread_id: str,
        message_id: str,
        request_id: str,
        *,
        head: object,
    ) -> Any:
        del thread_id
        try:
            return await find_settled_checkpoint_before_message(
                state,
                head,
                message_id,
                max_depth=_HISTORY_SCAN_LIMIT * 2,
            )
        except CheckpointLineageError:
            raise PrivateWorkConflict(request_id) from None

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

    async def _require_successful_source_run(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        run_id: str,
    ) -> PrivateRunRecord:
        """Revalidate an edit source under the exact private coordinates."""

        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(
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
                record = await PrivateRunRepository(session).get(
                    scope=context.resource_scope,
                    run_id=run_id,
                    lock=True,
                )
                if record is None or record.thread_id != thread_id or record.status != "success":
                    raise PrivateWorkConflict(context.request_id)
                return record
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

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
        snapshot_values = getattr(item, "values", None)
        if isinstance(snapshot_values, Mapping):
            return dict(snapshot_values)
        return {}

    @classmethod
    def _messages(cls, item: object) -> list[Any]:
        messages = cls._channel_values(item).get("messages", [])
        return list(messages) if isinstance(messages, list) else []

    @classmethod
    def _replay_input_message_id(
        cls,
        base: object,
        visible_message_id: str,
    ) -> str:
        """Reuse the pre-injection ID already present in a replay checkpoint.

        Dynamic context replaces the first user message with a hidden reminder
        plus a visible ``<id>__user`` copy. The replay base is the settled input
        checkpoint immediately before that replacement, so it already contains
        the original ``<id>`` message. Reusing that ID lets the messages reducer
        replace the checkpointed input instead of appending the same turn twice.
        """

        suffix = "__user"
        if not visible_message_id.endswith(suffix):
            return visible_message_id
        pre_injection_id = visible_message_id[: -len(suffix)]
        if any(cls._is_visible_human(message) and cls._message_id(message) == pre_injection_id for message in cls._messages(base)):
            return pre_injection_id
        return visible_message_id

    @classmethod
    def _edit_replay_message_plan(
        cls,
        base: object,
        source_human: object,
        *,
        replacement_text: str,
        replacement_base_id: str,
    ) -> tuple[list[dict[str, Any]], str]:
        source_message_id = cls._message_id(source_human)
        if source_message_id is None:
            raise ValueError("source human message requires a stable id")
        pre_injection_id = cls._replay_input_message_id(
            base,
            source_message_id,
        )
        replacement = cls._clean_human_message_for_edit(
            source_human,
            replacement_id=replacement_base_id,
            replacement_text=replacement_text,
        )
        if pre_injection_id == source_message_id:
            return [replacement], replacement_base_id
        return (
            [
                {"type": "remove", "id": pre_injection_id},
                replacement,
            ],
            f"{replacement_base_id}__user",
        )

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
    def _message_tool_calls(cls, message: object) -> list[Any]:
        value = message.get("tool_calls") if isinstance(message, Mapping) else getattr(message, "tool_calls", None)
        if not isinstance(value, list):
            value = cls._additional_kwargs(message).get("tool_calls")
        return list(value) if isinstance(value, list) else []

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
    def _latest_editable_turn(
        cls,
        messages: list[Any],
        human_message_id: str,
        request_id: str,
    ) -> tuple[Any, Any, list[str]]:
        latest_human_index = next(
            (index for index in range(len(messages) - 1, -1, -1) if cls._is_visible_human(messages[index])),
            None,
        )
        if latest_human_index is None or cls._message_id(messages[latest_human_index]) != human_message_id:
            raise PrivateWorkConflict(request_id)
        source_human = messages[latest_human_index]
        last_ai_index: int | None = None
        for index, message in enumerate(
            messages[latest_human_index + 1 :],
            start=latest_human_index + 1,
        ):
            if cls._is_visible_human(message):
                break
            if cls._is_visible_ai(message):
                last_ai_index = index
        if last_ai_index is None:
            raise PrivateWorkConflict(request_id)
        source_ai = messages[last_ai_index]
        if not message_to_text(source_ai).strip() or cls._message_tool_calls(source_ai):
            raise PrivateWorkConflict(request_id)
        source_message_ids = [message_id for message in messages[latest_human_index : last_ai_index + 1] if (message_id := cls._message_id(message)) is not None]
        return source_human, source_ai, source_message_ids

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
    def _clean_human_message_for_edit(
        cls,
        message: object,
        *,
        replacement_id: str,
        replacement_text: str,
    ) -> dict[str, Any]:
        source_kwargs = cls._additional_kwargs(message)
        additional_kwargs = {key: copy.deepcopy(source_kwargs[key]) for key in ("files", "referenced_message_contexts") if key in source_kwargs}
        clean: dict[str, Any] = {
            "type": "human",
            "id": replacement_id,
            "content": [{"type": "text", "text": replacement_text}],
            "additional_kwargs": additional_kwargs,
        }
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

    def _state(
        self,
        context: PrivateWorkContext,
        app_config: AppConfig,
        *,
        as_node: str,
    ) -> Any:
        return bind_scoped_checkpoint_state(
            self._project_scoped_checkpointer,
            context,
            app_config,
            as_node=as_node,
        )


__all__ = ["ProjectChatControlService"]
