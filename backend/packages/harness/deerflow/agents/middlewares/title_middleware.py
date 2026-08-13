"""Middleware for automatic thread title generation."""

import logging
import re
from typing import TYPE_CHECKING, Any, NotRequired, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langgraph.config import get_config
from langgraph.constants import TAG_NOSTREAM
from langgraph.runtime import Runtime

from deerflow.agents.middlewares.dynamic_context_middleware import is_dynamic_context_reminder
from deerflow.config.title_config import get_title_config
from deerflow.models import create_chat_model
from deerflow.sandbox.sandbox import AuthorizationRevoked, check_authorization_boundary

if TYPE_CHECKING:
    from deerflow.config.app_config import AppConfig
    from deerflow.config.title_config import TitleConfig

logger = logging.getLogger(__name__)


class TitleMiddlewareState(AgentState):
    """Compatible with the `ThreadState` schema."""

    title: NotRequired[str | None]


class TitleMiddleware(AgentMiddleware[TitleMiddlewareState]):
    """Automatically generate a title for the thread after the first user message."""

    state_schema = TitleMiddlewareState

    def __init__(self, *, app_config: "AppConfig | None" = None, title_config: "TitleConfig | None" = None):
        super().__init__()
        self._app_config = app_config
        self._title_config = title_config

    def _get_title_config(self):
        if self._title_config is not None:
            return self._title_config
        if self._app_config is not None:
            return self._app_config.title
        return get_title_config()

    def _normalize_content(self, content: object) -> str:
        if isinstance(content, str):
            return content

        if isinstance(content, list):
            parts = [self._normalize_content(item) for item in content]
            return "\n".join(part for part in parts if part)

        if isinstance(content, dict):
            text_value = content.get("text")
            if isinstance(text_value, str):
                return text_value

            nested_content = content.get("content")
            if nested_content is not None:
                return self._normalize_content(nested_content)

        return ""

    @staticmethod
    def _message_type(message: object) -> str | None:
        message_type = getattr(message, "type", None)
        if message_type is None and isinstance(message, dict):
            message_type = message.get("type") or message.get("role")
        if message_type == "user":
            return "human"
        if message_type == "assistant":
            return "ai"
        return message_type if isinstance(message_type, str) else None

    @staticmethod
    def _message_content(message: object) -> object:
        if isinstance(message, dict):
            return message.get("content", "")
        return getattr(message, "content", "")

    @staticmethod
    def _is_dynamic_context_reminder_message(message: object) -> bool:
        if is_dynamic_context_reminder(message):
            return True
        if isinstance(message, dict):
            additional_kwargs = message.get("additional_kwargs")
            return isinstance(additional_kwargs, dict) and bool(additional_kwargs.get("dynamic_context_reminder"))
        return False

    @staticmethod
    def _is_hidden_from_ui_message(message: object) -> bool:
        if isinstance(message, dict):
            additional_kwargs = message.get("additional_kwargs")
        else:
            additional_kwargs = getattr(message, "additional_kwargs", None)
        return isinstance(additional_kwargs, dict) and additional_kwargs.get("hide_from_ui") is True

    @staticmethod
    def _is_user_message_for_title(message: object) -> bool:
        return TitleMiddleware._message_type(message) == "human" and not TitleMiddleware._is_dynamic_context_reminder_message(message) and not TitleMiddleware._is_hidden_from_ui_message(message)

    @staticmethod
    def _message_has_tool_calls(message: object) -> bool:
        if isinstance(message, dict):
            tool_calls = message.get("tool_calls")
            if tool_calls:
                return True
            additional_kwargs = message.get("additional_kwargs")
            return isinstance(additional_kwargs, dict) and bool(additional_kwargs.get("tool_calls"))
        return bool(getattr(message, "tool_calls", None))

    def _get_title_user_message(self, state: TitleMiddlewareState) -> str:
        messages = state.get("messages") or []
        user_msg_content = next((self._message_content(m) for m in messages if self._is_user_message_for_title(m)), "")
        return self._normalize_content(user_msg_content)

    def _should_generate_title(self, state: TitleMiddlewareState) -> bool:
        """Check if we should generate a title for this thread."""
        config = self._get_title_config()
        if not config.enabled:
            return False

        # Check if thread already has a title in state
        if state.get("title"):
            return False

        # Check if this is the first turn (has at least one user message and one assistant response).
        # Defensively coerce a None ``messages`` channel (possible when reading a
        # partially-initialized checkpoint) into an empty list so ``len()`` is safe.
        messages = state.get("messages") or []
        if len(messages) < 2:
            return False

        # Count user and assistant messages
        user_messages = [m for m in messages if self._is_user_message_for_title(m)]
        assistant_messages = [m for m in messages if self._message_type(m) == "ai"]

        if len(user_messages) != 1:
            return False
        if not assistant_messages:
            return False

        final_assistant = assistant_messages[-1]
        if self._message_has_tool_calls(final_assistant):
            return False
        final_content = self._strip_think_tags(
            self._normalize_content(
                self._message_content(final_assistant),
            )
        )
        return bool(final_content)

    def _build_title_prompt(self, state: TitleMiddlewareState) -> tuple[str, str]:
        """Extract user/assistant messages and build the title prompt.

        Returns (prompt_string, user_msg) so callers can use user_msg as fallback.
        """
        config = self._get_title_config()
        messages = state.get("messages") or []

        assistant_msg_content = next((self._message_content(m) for m in messages if self._message_type(m) == "ai"), "")

        user_msg = self._get_title_user_message(state)
        assistant_msg = self._strip_think_tags(self._normalize_content(assistant_msg_content))

        prompt = config.prompt_template.format(
            max_words=config.max_words,
            user_msg=user_msg[:500],
            assistant_msg=assistant_msg[:500],
        )
        return prompt, user_msg

    def _strip_think_tags(self, text: str) -> str:
        """Remove <think>...</think> blocks emitted by reasoning models (e.g. minimax, DeepSeek-R1)."""
        return re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE).strip()

    def _parse_title(self, content: object) -> str:
        """Normalize model output into a clean title string."""
        config = self._get_title_config()
        title_content = self._normalize_content(content)
        title_content = self._strip_think_tags(title_content)
        title = title_content.strip().strip('"').strip("'")
        return title[: config.max_chars] if len(title) > config.max_chars else title

    def _fallback_title(self, user_msg: str) -> str:
        config = self._get_title_config()
        max_chars = max(config.max_chars, 0)
        fallback_chars = min(max_chars, 50)
        if len(user_msg) > fallback_chars:
            ellipsis = "..."
            if max_chars <= len(ellipsis):
                return ellipsis[:max_chars]
            body_chars = min(fallback_chars, max_chars - len(ellipsis))
            return user_msg[:body_chars].rstrip() + ellipsis
        fallback = user_msg if user_msg else "New Conversation"
        return fallback[:max_chars]

    def _get_runnable_config(self) -> dict[str, Any]:
        """Inherit the parent RunnableConfig and add middleware tag.

        This ensures RunJournal identifies LLM calls from this middleware
        as ``middleware:title`` instead of ``lead_agent``.
        """
        try:
            parent = get_config()
        except Exception:
            parent = {}
        config = {**parent}
        config["run_name"] = "title_agent"
        config["tags"] = [
            *(config.get("tags") or []),
            "middleware:title",
            TAG_NOSTREAM,
        ]
        return config

    def _generate_title_result(self, state: TitleMiddlewareState) -> dict | None:
        """Generate a local fallback title without blocking on an LLM call."""
        if not self._should_generate_title(state):
            return None

        user_msg = self._get_title_user_message(state)
        return {"title": self._fallback_title(user_msg)}

    async def _agenerate_title_result(
        self,
        state: TitleMiddlewareState,
        runtime_context: object | None = None,
    ) -> dict | None:
        """Generate a configured LLM title asynchronously and fall back locally."""
        if not self._should_generate_title(state):
            return None

        config = self._get_title_config()
        user_msg = self._get_title_user_message(state)

        try:
            prompt, user_msg = self._build_title_prompt(state)
            # attach_tracing=False because ``_get_runnable_config()`` inherits
            # the graph-level RunnableConfig (set in ``_make_lead_agent``) whose
            # callbacks already carry tracing handlers; binding them again at
            # the model level would emit duplicate spans.
            model_kwargs = {"thinking_enabled": False, "attach_tracing": False}
            if self._app_config is not None:
                model_kwargs["app_config"] = self._app_config
            # ``model_name is None`` uses the AppConfig default model (catalog
            # default after Worker overlay, otherwise models[0]).
            model = create_chat_model(name=config.model_name, **model_kwargs)
            await check_authorization_boundary(
                runtime_context,
                "before_model_call",
            )
            response = await model.ainvoke(prompt, config=self._get_runnable_config())
            title = self._parse_title(response.content)
            if title:
                return {"title": title}
        except AuthorizationRevoked:
            raise
        except Exception:
            logger.debug("Failed to generate async title; falling back to local title", exc_info=True)
        return {"title": self._fallback_title(user_msg)}

    @override
    def after_model(self, state: TitleMiddlewareState, runtime: Runtime) -> dict | None:
        return self._generate_title_result(state)

    @override
    async def aafter_model(self, state: TitleMiddlewareState, runtime: Runtime) -> dict | None:
        return await self._agenerate_title_result(state, runtime.context)
