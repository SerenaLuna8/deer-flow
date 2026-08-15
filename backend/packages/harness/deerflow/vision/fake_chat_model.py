"""Non-networking chat-model shim for the P1 Vision Bridge fake adapter."""

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class FakeVisionBridgeChatModel(BaseChatModel):
    """A deterministic provider class that can never perform external I/O.

    It exists so administrators and catalog/materialization tests can exercise
    the standard System Model lifecycle in P1.  Vision analysis itself uses the
    dedicated fake ``VisionEvidenceClient`` rather than this generic chat path.
    """

    def __init__(self, **kwargs: Any) -> None:
        callbacks = kwargs.pop("callbacks", None)
        super().__init__(callbacks=callbacks)

    @property
    def _llm_type(self) -> str:
        return "deerflow-vision-bridge-fake"

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


__all__ = ["FakeVisionBridgeChatModel"]
