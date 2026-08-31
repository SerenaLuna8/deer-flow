"""ChatDeepSeek with reasoning_content replay in multi-turn conversations.

This is the sole implementation behind the ``deepseek`` adapter. The stock
ChatDeepSeek stores reasoning_content in additional_kwargs but drops it from
subsequent request payloads. Per the official thinking-mode guide, requests
that carry tools must echo historical assistant reasoning_content back
(including turns that produced no tool_calls); requests without tools ignore
the field. Replaying it unconditionally therefore satisfies the strict case
and stays harmless in the tolerant one.
"""

from typing import Any

from langchain_core.language_models import LanguageModelInput
from langchain_deepseek import ChatDeepSeek

from deerflow.models.assistant_payload_replay import restore_assistant_payloads, restore_reasoning_content


class PatchedChatDeepSeek(ChatDeepSeek):
    """ChatDeepSeek with reasoning_content replay on outgoing payloads.

    Requests that carry tools require reasoning_content on every historical
    assistant message; requests without tools accept and ignore it. This
    subclass restores the stored reasoning_content into the request payload
    unconditionally, covering both cases without inspecting the tool set.
    """

    @classmethod
    def is_lc_serializable(cls) -> bool:
        return True

    @property
    def lc_secrets(self) -> dict[str, str]:
        return {"api_key": "DEEPSEEK_API_KEY", "openai_api_key": "DEEPSEEK_API_KEY"}

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        """Get request payload with reasoning_content preserved.

        Overrides the parent method to inject reasoning_content from
        additional_kwargs into assistant messages in the payload.
        """
        # Get the original messages before conversion
        original_messages = self._convert_input(input_).to_messages()

        # Call parent to get the base payload
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)

        restore_assistant_payloads(
            payload.get("messages", []),
            original_messages,
            restore_reasoning_content,
        )

        return payload
