"""Test-only chat models that satisfy the governed Runtime contract."""

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class FakeVisionBridgeChatModel(BaseChatModel):
    """A deterministic model that can never perform external I/O."""

    max_retries: int = 0

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del messages, stop, kwargs
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="OK"))],
        )

    @property
    def _llm_type(self) -> str:
        return "deerflow-vision-bridge-fake"


class GovernedFakeListChatModel(FakeListChatModel):
    """LangChain's list fake with the Runtime-owned retry field declared."""

    max_retries: int = 0


__all__ = ["FakeVisionBridgeChatModel", "GovernedFakeListChatModel"]
