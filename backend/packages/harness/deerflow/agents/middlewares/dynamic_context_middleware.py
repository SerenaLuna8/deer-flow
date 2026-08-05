"""Middleware to inject dynamic context (memory, current date) as a system-reminder.

The system prompt is kept fully static for maximum prefix-cache reuse across users
and sessions. The current date is always injected. Per-user Memory is injected when
``memory.injection_enabled`` is true. The legacy path keeps its Thread-frozen
behavior; the v2 path replaces one hidden HumanMessage from a stable per-Run
snapshot and revalidates its deletion overlay before every model call.

When a conversation spans midnight the middleware detects the date change and injects
a lightweight date-update reminder as a separate SystemMessage before the current turn.
This correction is persisted so subsequent turns on the new day see a consistent history
and do not re-inject.

Reminder format:

    <system-reminder>
    <memory>...</memory>

    <current_date>2026-05-08, Friday</current_date>
    </system-reminder>

Date-update format:

    <system-reminder>
    <current_date>2026-05-09, Saturday</current_date>
    </system-reminder>
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, override

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage, RemoveMessage, SystemMessage
from langgraph.runtime import Runtime

if TYPE_CHECKING:
    from deerflow.agents.memory.storage import ProjectMemoryStorage
    from deerflow.config.app_config import AppConfig
    from deerflow.private_scope import PrivateResourceScope

logger = logging.getLogger(__name__)

# Upper bound (seconds) for a single _inject() offload.  If the warm-up at
# gateway startup failed silently, the first request may still hit a cold
# tiktoken BPE download that blocks until the OS TCP timeout (~26 min).
# This cap ensures the request degrades gracefully instead of hanging.
_INJECT_TIMEOUT_SECONDS = 5.0

_DATE_RE = re.compile(r"<current_date>([^<]+)</current_date>")
_DYNAMIC_CONTEXT_REMINDER_KEY = "dynamic_context_reminder"
# Authoritative injected date, carried in additional_kwargs of the date
# SystemMessage. Detection reads this instead of regex-parsing message content,
# so it is never exposed to user-influenceable memory content.
_REMINDER_DATE_KEY = "reminder_date"
_PROJECT_MEMORY_LOADED_KEY = "project_memory_loaded"
_PROJECT_MEMORY_MODE_KEY = "project_memory_mode"
_PROJECT_MEMORY_SNAPSHOT_ID_KEY = "project_memory_snapshot_id"
_PROJECT_MEMORY_SNAPSHOT_VERSION_KEY = "project_memory_snapshot_version"
_PROJECT_MEMORY_SNAPSHOT_DIGEST_KEY = "project_memory_snapshot_digest"
_SUMMARY_MESSAGE_NAME = "summary"
_LEGACY_MEMORY = object()


class ProjectMemoryRevalidator(Protocol):
    async def is_active(self, scope: PrivateResourceScope) -> bool: ...


def _extract_date(content: str) -> str | None:
    """Return the first <current_date> value found in *content*, or None."""
    m = _DATE_RE.search(content)
    return m.group(1) if m else None


def is_dynamic_context_reminder(message: object) -> bool:
    """Return whether *message* is a hidden dynamic-context reminder."""
    # DEPRECATED: HumanMessage reminders only exist in pre-PR checkpoints.
    # Once all active checkpoints are migrated, the HumanMessage branch can be
    # removed and this function can check SystemMessage exclusively.
    return isinstance(message, (HumanMessage, SystemMessage)) and bool(message.additional_kwargs.get(_DYNAMIC_CONTEXT_REMINDER_KEY))


def _last_injected_date(messages: list) -> str | None:
    """Scan messages in reverse and return the most recently injected date.

    Detection uses the ``dynamic_context_reminder`` additional_kwargs flag rather
    than content substring matching, so user messages containing ``<system-reminder>``
    are not mistakenly treated as injected reminders.

    The authoritative date is the ``reminder_date`` value in additional_kwargs of
    the date SystemMessage. Reminders without it (the separate ``<memory>``
    HumanMessage, or any future dateless reminder) carry no date and are skipped,
    so they cannot shadow the real date reminder.
    """
    for msg in reversed(messages):
        if not is_dynamic_context_reminder(msg):
            continue
        structured = msg.additional_kwargs.get(_REMINDER_DATE_KEY)
        if isinstance(structured, str) and structured:
            return structured
        # Backward-compat for checkpoints written before reminder_date existed:
        # the date lived in content. Scope the regex to SystemMessage so it never
        # runs on the user-influenceable memory HumanMessage (preserves the OWASP
        # role separation from #3630 and closes the memory date-spoofing hole).
        if isinstance(msg, SystemMessage):
            content_str = msg.content if isinstance(msg.content, str) else str(msg.content)
            date = _extract_date(content_str)
            if date is not None:
                return date
    return None


def _project_memory_loaded(messages: list) -> bool:
    """Return whether a prior project-memory read completed successfully."""
    return any(is_dynamic_context_reminder(message) and message.additional_kwargs.get(_PROJECT_MEMORY_LOADED_KEY) is True for message in messages)


def _is_project_memory_message(message: object) -> bool:
    if not isinstance(message, HumanMessage) or not is_dynamic_context_reminder(message):
        return False
    if message.additional_kwargs.get(_PROJECT_MEMORY_LOADED_KEY) is not True:
        return False
    content = message.content if isinstance(message.content, str) else str(message.content)
    return message.additional_kwargs.get(_PROJECT_MEMORY_MODE_KEY) == "v2" or "<memory>" in content


def _has_v2_memory_marker(messages: list) -> bool:
    for message in reversed(messages):
        if _is_project_memory_message(message):
            return message.additional_kwargs.get(_PROJECT_MEMORY_MODE_KEY) == "v2"
    for message in reversed(messages):
        if not isinstance(message, SystemMessage) or not is_dynamic_context_reminder(message):
            continue
        if _last_injected_date([message]) is not None:
            return message.additional_kwargs.get(_PROJECT_MEMORY_MODE_KEY) == "v2"
    return False


def _is_user_injection_target(message: object) -> bool:
    """Return whether *message* can receive a dynamic-context reminder."""
    if not isinstance(message, HumanMessage):
        return False
    if is_dynamic_context_reminder(message):
        return False
    if message.name == _SUMMARY_MESSAGE_NAME:
        return False
    # Prevent recursive ID-swap: a message whose ID ends with "__user" was
    # produced by a prior _make_reminder_and_user_messages call and must not
    # be processed again — doing so causes unbounded suffix growth
    # (id__user__user__user...) and ghost-message re-execution.
    # Using endswith (not substring "in") avoids false positives on IDs that
    # happen to contain "__user" in the middle.
    if message.id and str(message.id).endswith("__user"):
        return False
    return True


class DynamicContextMiddleware(AgentMiddleware):
    """Inject the date plus low-authority hidden Human Memory context.

    First turn
    ----------
    The v1 path preserves the existing Thread-frozen reminder. The v2 path pins
    exact Revisions per Run and reconciles the single hidden Memory message at
    each model boundary while the framework-owned date remains a SystemMessage.

    Midnight crossing
    -----------------
    If the conversation spans midnight, the current date differs from the date that
    was injected earlier.  In that case a lightweight date-update reminder is prepended
    to the **current** (last) HumanMessage and persisted.  Subsequent turns on the new
    day see the corrected date in history and skip re-injection.
    """

    def __init__(
        self,
        agent_name: str | None = None,
        *,
        app_config: AppConfig | None = None,
        project_memory_storage: ProjectMemoryStorage | None = None,
        project_memory_revalidator: ProjectMemoryRevalidator | None = None,
    ):
        super().__init__()
        self._agent_name = agent_name
        self._app_config = app_config
        self._project_memory_storage = project_memory_storage
        self._project_memory_revalidator = project_memory_revalidator

    def _build_full_reminder(self) -> tuple[str, str | None]:
        """Return (date_reminder, memory_block | None).

        Framework-owned data (date) is separated from user-owned data (memory)
        so the downstream SystemMessage carries only framework authority and
        memory stays at role:user — preventing untrusted content from gaining
        system privilege (OWASP LLM01).

        Project Memory is loaded only by :meth:`_inject_private`, from an
        authenticated ``PrivateResourceScope`` backed by PostgreSQL.  The
        unscoped path deliberately injects the date only.  Keeping this helper
        independent from ``lead_agent.prompt`` also preserves the process
        boundary: Gateway-owned chat controls may reuse context-compaction
        helpers without importing the Worker-only lead-agent graph.
        """
        current_date = datetime.now().strftime("%Y-%m-%d, %A")

        date_reminder = "\n".join(
            [
                "<system-reminder>",
                f"<current_date>{current_date}</current_date>",
                "</system-reminder>",
            ]
        )

        return date_reminder, None

    def _build_date_update_reminder(self) -> str:
        current_date = datetime.now().strftime("%Y-%m-%d, %A")
        return "\n".join(
            [
                "<system-reminder>",
                f"<current_date>{current_date}</current_date>",
                "</system-reminder>",
            ]
        )

    @staticmethod
    def _make_reminder_and_user_messages(
        original: HumanMessage,
        reminder_content: str,
        memory_content: str | None = None,
        *,
        reminder_date: str | None = None,
        memory_loaded: bool = False,
        memory_metadata: dict[str, object] | None = None,
    ) -> list[SystemMessage | HumanMessage]:
        """Return messages using the ID-swap technique.

        SystemMessage carries framework-owned data (date, metadata) — takes
        the original ID so add_messages replaces it in-place.  *reminder_date*
        is recorded in its additional_kwargs as the authoritative injected date
        (``_last_injected_date`` reads it instead of parsing content).  Optional
        HumanMessage carries user-owned memory content with ``{id}__memory``.
        The actual user message gets ``{id}__user``.

        SystemMessage is used — system context must not masquerade as user
        input (#3630).  Memory is deliberately kept as HumanMessage so
        user-influenceable content does not gain system authority (OWASP LLM01)
        — and it deliberately never carries ``reminder_date``.
        """
        stable_id = original.id or str(uuid.uuid4())
        messages: list[SystemMessage | HumanMessage] = []

        reminder_kwargs = {"hide_from_ui": True, _DYNAMIC_CONTEXT_REMINDER_KEY: True}
        if reminder_date is not None:
            reminder_kwargs[_REMINDER_DATE_KEY] = reminder_date
        if memory_loaded:
            reminder_kwargs[_PROJECT_MEMORY_LOADED_KEY] = True
            if memory_metadata:
                reminder_kwargs.update(memory_metadata)
        messages.append(
            SystemMessage(
                content=reminder_content,
                id=stable_id,
                additional_kwargs=reminder_kwargs,
            )
        )

        if memory_content:
            memory_kwargs = {
                "hide_from_ui": True,
                _DYNAMIC_CONTEXT_REMINDER_KEY: True,
            }
            if memory_loaded:
                memory_kwargs[_PROJECT_MEMORY_LOADED_KEY] = True
                if memory_metadata:
                    memory_kwargs.update(memory_metadata)
            messages.append(
                HumanMessage(
                    content=memory_content,
                    id=f"{stable_id}__memory",
                    additional_kwargs=memory_kwargs,
                )
            )

        messages.append(
            HumanMessage(
                content=original.content,
                id=f"{stable_id}__user",
                name=original.name,
                additional_kwargs=original.additional_kwargs,
            )
        )
        return messages

    @staticmethod
    def _make_memory_and_user_messages(
        original: HumanMessage,
        memory_content: str,
        *,
        memory_metadata: dict[str, object] | None = None,
    ) -> list[HumanMessage]:
        """Replace the current user turn with hidden Memory followed by the user."""
        stable_id = original.id or str(uuid.uuid4())
        return [
            HumanMessage(
                content=memory_content,
                id=stable_id,
                additional_kwargs={
                    "hide_from_ui": True,
                    _DYNAMIC_CONTEXT_REMINDER_KEY: True,
                    _PROJECT_MEMORY_LOADED_KEY: True,
                    **(memory_metadata or {}),
                },
            ),
            HumanMessage(
                content=original.content,
                id=f"{stable_id}__user",
                name=original.name,
                additional_kwargs=original.additional_kwargs,
            ),
        ]

    @staticmethod
    def _mark_existing_reminder_memory_loaded(
        messages: list,
        *,
        memory_metadata: dict[str, object] | None = None,
    ) -> dict | None:
        for message in reversed(messages):
            if not isinstance(message, SystemMessage):
                continue
            if not is_dynamic_context_reminder(message):
                continue
            if _last_injected_date([message]) is None:
                continue
            additional_kwargs = dict(message.additional_kwargs)
            additional_kwargs[_PROJECT_MEMORY_LOADED_KEY] = True
            if memory_metadata:
                additional_kwargs.update(memory_metadata)
            if additional_kwargs == message.additional_kwargs:
                return None
            return {
                "messages": [
                    message.model_copy(
                        update={"additional_kwargs": additional_kwargs},
                    )
                ]
            }
        return None

    def _inject(
        self,
        state,
        *,
        memory_block_override: str | None | object = _LEGACY_MEMORY,
        memory_loaded: bool = False,
        memory_metadata: dict[str, object] | None = None,
    ) -> dict | None:
        messages = list(state.get("messages", []))
        if not messages:
            return None

        current_date = datetime.now().strftime("%Y-%m-%d, %A")
        last_date = _last_injected_date(messages)
        logger.debug(
            "DynamicContextMiddleware._inject: msg_count=%d last_date=%r current_date=%r",
            len(messages),
            last_date,
            current_date,
        )

        if last_date is None:
            # ── First available turn: inject full reminder ──────────────
            # A prior Run can fail before this middleware executes, leaving a
            # dateless checkpoint with multiple user messages. Target the
            # latest genuine user message so the ID swap cannot move an older
            # failed prompt behind the current turn.
            target_idx = next(
                (i for i in reversed(range(len(messages))) if _is_user_injection_target(messages[i])),
                None,
            )
            if target_idx is None:
                return None
            if memory_block_override is _LEGACY_MEMORY:
                date_reminder, memory_block = self._build_full_reminder()
            else:
                date_reminder = self._build_date_update_reminder()
                memory_block = memory_block_override
            logger.info(
                "DynamicContextMiddleware: injecting full reminder (has_memory=%s) into current HumanMessage id=%r",
                memory_block is not None,
                messages[target_idx].id,
            )
            result_msgs = self._make_reminder_and_user_messages(
                messages[target_idx],
                date_reminder,
                memory_block,
                reminder_date=current_date,
                memory_loaded=memory_loaded,
                memory_metadata=memory_metadata,
            )
            return {"messages": result_msgs}

        if last_date == current_date:
            # ── Same day: nothing to do ──────────────────────────────────────────
            if memory_loaded:
                if isinstance(memory_block_override, str) and memory_block_override:
                    target_idx = next(
                        (i for i in reversed(range(len(messages))) if _is_user_injection_target(messages[i])),
                        None,
                    )
                    if target_idx is None:
                        return None
                    return {
                        "messages": self._make_memory_and_user_messages(
                            messages[target_idx],
                            memory_block_override,
                            memory_metadata=memory_metadata,
                        )
                    }
                return self._mark_existing_reminder_memory_loaded(
                    messages,
                    memory_metadata=memory_metadata,
                )
            return None

        # ── Midnight crossed: inject date-update reminder as a SystemMessage ──
        last_human_idx = next((i for i in reversed(range(len(messages))) if _is_user_injection_target(messages[i])), None)
        if last_human_idx is None:
            return None

        memory_block = memory_block_override if isinstance(memory_block_override, str) and memory_block_override else None
        result_msgs = self._make_reminder_and_user_messages(
            messages[last_human_idx],
            self._build_date_update_reminder(),
            memory_block,
            reminder_date=current_date,
            memory_loaded=memory_loaded,
            memory_metadata=memory_metadata,
        )
        logger.info("DynamicContextMiddleware: midnight crossing detected — injected date update before current turn")
        return {"messages": result_msgs}

    @override
    def before_agent(self, state, runtime: Runtime) -> dict | None:
        # Project Memory requires async PostgreSQL access. The synchronous hook
        # always injects only framework-owned context and never global Memory.
        return self._inject(state, memory_block_override=None)

    def _resolve_project_memory_dependencies(
        self,
    ) -> tuple[ProjectMemoryStorage, ProjectMemoryRevalidator]:
        if self._project_memory_storage is None or self._project_memory_revalidator is None:
            from deerflow.agents.memory.storage import ProjectMemoryMembershipRevalidator, ProjectMemoryStorage
            from deerflow.persistence import get_session_factory

            session_factory = get_session_factory()
            self._project_memory_storage = ProjectMemoryStorage(session_factory)
            self._project_memory_revalidator = ProjectMemoryMembershipRevalidator(session_factory)
        return self._project_memory_storage, self._project_memory_revalidator

    def _project_memory_namespace(self) -> str:
        return f"agent:{self._agent_name}" if self._agent_name else "default"

    def _format_project_memory(self, memory_data: dict[str, Any]) -> str | None:
        from deerflow.agents.memory import format_memory_for_injection
        from deerflow.config.memory_config import get_memory_config

        config = self._app_config.memory if self._app_config else get_memory_config()
        if not config.enabled or not config.injection_enabled:
            return None
        memory_content = format_memory_for_injection(
            memory_data,
            max_tokens=config.max_injection_tokens,
            use_tiktoken=(config.token_counting == "tiktoken"),
            guaranteed_categories=getattr(config, "guaranteed_categories", None),
            guaranteed_token_budget=getattr(config, "guaranteed_token_budget", 500),
        )
        if not memory_content.strip():
            return None
        return f"<memory>\n{memory_content}\n</memory>"

    @staticmethod
    def _v2_authority(runtime: Runtime) -> object | None:
        runtime_context = runtime.context if isinstance(runtime.context, dict) else {}
        authority = runtime_context.get("__memory_authority")
        if authority is None or isinstance(authority, dict):
            return None
        if getattr(authority, "pipeline_mode", None) != "v2":
            return None
        if not callable(getattr(authority, "load_snapshot", None)):
            return None
        return authority

    @staticmethod
    def _v2_snapshot_view(
        snapshot: object | None,
    ) -> tuple[str | None, dict[str, object]]:
        metadata: dict[str, object] = {_PROJECT_MEMORY_MODE_KEY: "v2"}
        if snapshot is None:
            return None, metadata
        snapshot_id = getattr(snapshot, "id", None)
        version = getattr(snapshot, "version", None)
        digest = getattr(snapshot, "rendered_content_digest", None)
        rendered_content = getattr(snapshot, "rendered_content", None)
        if snapshot_id is None or type(version) is not int or version < 0:
            raise RuntimeError("project memory v2 snapshot is invalid")
        snapshot_id = str(snapshot_id)
        if not snapshot_id or not isinstance(digest, str) or len(digest) != 64:
            raise RuntimeError("project memory v2 snapshot is invalid")
        if rendered_content is not None and not isinstance(rendered_content, str):
            raise RuntimeError("project memory v2 snapshot is invalid")
        metadata.update(
            {
                _PROJECT_MEMORY_SNAPSHOT_ID_KEY: snapshot_id,
                _PROJECT_MEMORY_SNAPSHOT_VERSION_KEY: version,
                _PROJECT_MEMORY_SNAPSHOT_DIGEST_KEY: digest,
            }
        )
        return (
            rendered_content if isinstance(rendered_content, str) and rendered_content.strip() else None,
            metadata,
        )

    def _reconcile_v2_snapshot(
        self,
        state,
        snapshot: object | None,
    ) -> dict | None:
        messages = list(state.get("messages", []))
        if not messages:
            return None
        memory_block, metadata = self._v2_snapshot_view(snapshot)
        memory_messages = [message for message in messages if _is_project_memory_message(message)]

        if _last_injected_date(messages) is None:
            update = self._inject(
                state,
                memory_block_override=memory_block,
                memory_loaded=True,
                memory_metadata=metadata,
            )
            if update is None:
                return None
            stale = [RemoveMessage(id=message.id) for message in memory_messages if message.id is not None]
            return {"messages": [*stale, *update["messages"]]}

        operations: list[object] = []
        if memory_messages:
            keeper = memory_messages[-1]
            for stale in memory_messages[:-1]:
                if stale.id is not None:
                    operations.append(RemoveMessage(id=stale.id))
            if memory_block is None:
                if keeper.id is not None:
                    operations.append(RemoveMessage(id=keeper.id))
            else:
                additional_kwargs = dict(keeper.additional_kwargs)
                additional_kwargs.update(
                    {
                        "hide_from_ui": True,
                        _DYNAMIC_CONTEXT_REMINDER_KEY: True,
                        _PROJECT_MEMORY_LOADED_KEY: True,
                        **metadata,
                    }
                )
                if keeper.content != memory_block or additional_kwargs != keeper.additional_kwargs:
                    operations.append(
                        keeper.model_copy(
                            update={
                                "content": memory_block,
                                "additional_kwargs": additional_kwargs,
                            }
                        )
                    )
        elif memory_block is not None:
            created = self._inject(
                state,
                memory_block_override=memory_block,
                memory_loaded=True,
                memory_metadata=metadata,
            )
            if created is not None:
                operations.extend(created["messages"])
                return {"messages": operations}

        date_update = self._inject(
            state,
            memory_block_override=None,
            memory_loaded=True,
            memory_metadata=metadata,
        )
        if date_update is not None:
            operations.extend(date_update["messages"])
        return {"messages": operations} if operations else None

    async def _inject_private(
        self,
        state,
        scope: object,
    ) -> dict | None:
        from deerflow.private_scope import PrivateResourceScope

        messages = list(state.get("messages", []))
        if not messages:
            return None
        transitioning_from_v2 = _has_v2_memory_marker(messages)
        stale_v2 = [RemoveMessage(id=message.id) for message in messages if _is_project_memory_message(message) and message.additional_kwargs.get(_PROJECT_MEMORY_MODE_KEY) == "v2" and message.id is not None]

        def finish(
            update: dict | None,
            *,
            remove_all_memory: bool = False,
        ) -> dict | None:
            removals = [RemoveMessage(id=message.id) for message in messages if _is_project_memory_message(message) and message.id is not None] if remove_all_memory else stale_v2
            operations = [*removals, *((update or {}).get("messages", []))]
            return {"messages": operations} if operations else None

        config = self._app_config.memory if self._app_config else None
        if config is not None and (not config.enabled or not config.injection_enabled):
            return finish(
                self._inject(state, memory_block_override=None),
                remove_all_memory=True,
            )
        if _project_memory_loaded(messages) and not transitioning_from_v2:
            return self._inject(state, memory_block_override=None)
        if type(scope) is not PrivateResourceScope:
            return finish(self._inject(state, memory_block_override=None))

        try:
            storage, revalidator = self._resolve_project_memory_dependencies()
            if not await revalidator.is_active(scope):
                return finish(self._inject(state, memory_block_override=None))
            snapshot = await storage.load(
                scope=scope,
                namespace=self._project_memory_namespace(),
            )
            memory_block = await asyncio.to_thread(
                self._format_project_memory,
                snapshot.memory,
            )
            update = self._inject(
                state,
                memory_block_override=memory_block,
                memory_loaded=True,
                memory_metadata={_PROJECT_MEMORY_MODE_KEY: "v1"},
            )
            if memory_block and transitioning_from_v2:
                marker = self._mark_existing_reminder_memory_loaded(
                    messages,
                    memory_metadata={_PROJECT_MEMORY_MODE_KEY: "v1"},
                )
                if marker is not None:
                    update = {
                        "messages": [
                            *((update or {}).get("messages", [])),
                            *marker["messages"],
                        ]
                    }
            return finish(update)
        except Exception:
            logger.exception("DynamicContextMiddleware: failed to load project memory; injecting date only")
            return finish(self._inject(state, memory_block_override=None))

    @override
    async def abefore_agent(self, state, runtime: Runtime) -> dict | None:
        runtime_context = runtime.context if isinstance(runtime.context, dict) else {}
        if self._v2_authority(runtime) is not None:
            # v2 is loaded at every model boundary so hard-forget overlays can
            # replace checkpointed Memory before the next model call.
            return None
        if "private_scope" in runtime_context:
            try:
                return await asyncio.wait_for(
                    self._inject_private(state, runtime_context.get("private_scope")),
                    timeout=_INJECT_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                logger.warning(
                    "DynamicContextMiddleware: project memory injection timed out (%.1fs); injecting date only",
                    _INJECT_TIMEOUT_SECONDS,
                )
                return self._inject(state, memory_block_override=None)
        # Keep the unscoped compatibility hook off the event loop.  Production
        # no longer reads global file Memory here, but downstream extensions
        # may still provide synchronous reminder formatting; a blocking call
        # would starve concurrent HTTP handlers (auth, SSE heartbeats, etc.).
        #
        # Time-box compatibility formatting so the request degrades to no
        # injected context instead of hanging.
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._inject, state),
                timeout=_INJECT_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "DynamicContextMiddleware: injection timed out (%.1fs); skipping memory/date injection for this turn",
                _INJECT_TIMEOUT_SECONDS,
            )
            return None

    @override
    async def abefore_model(self, state, runtime: Runtime) -> dict | None:
        authority = self._v2_authority(runtime)
        if authority is None:
            return None
        config = self._app_config.memory if self._app_config is not None else None
        if config is not None and (not config.enabled or not config.injection_enabled):
            return self._reconcile_v2_snapshot(state, None)
        snapshot = await asyncio.wait_for(
            authority.load_snapshot(),
            timeout=_INJECT_TIMEOUT_SECONDS,
        )
        return self._reconcile_v2_snapshot(state, snapshot)
