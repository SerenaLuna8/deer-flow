"""Shared helper for one-shot, non-graph LLM text requests.

Gateway auxiliary routes and Builder generation do the same thing: build a chat
model from config, invoke it once with a system + user message pair under the
private one-shot profile, and pull the plain text back out of the response.
Centralizing that sequence keeps privacy, deadline, retry, and invocation
behavior from drifting between callers.

Response-text *cleaning* (think-block / code-fence stripping, JSON parsing) is
intentionally left to each caller because their post-processing differs; this
helper stops at the extracted raw text.
"""

from __future__ import annotations

from collections.abc import Mapping

from langchain_core.messages import HumanMessage, SystemMessage

from deerflow.config.app_config import AppConfig
from deerflow.models.runtime import (
    AsyncAbortEvent,
    ModelRuntime,
    ModelRuntimeProfile,
)
from deerflow.utils.llm_text import extract_response_text


async def run_oneshot_llm(
    *,
    system_instruction: str,
    user_content: str,
    run_name: str,
    app_config: AppConfig,
    model_name: str | None = None,
    thinking_enabled: bool = False,
    reasoning_effort: str | None = None,
    model_overrides: Mapping[str, object] | None = None,
    profile: ModelRuntimeProfile = ModelRuntimeProfile.PRIVATE_ONESHOT,
    deadline_monotonic: float | None = None,
    abort_event: AsyncAbortEvent | None = None,
) -> str:
    """Run a single non-graph system+user LLM turn and return the raw text.

    Args:
        system_instruction: System message content.
        user_content: Human message content.
        run_name: Internal LangChain run name for the call.
        app_config: Application config used to build the model.
        model_name: Optional model override; ``None`` uses the default model.
        thinking_enabled: Enable the model's extended-thinking mode when supported.
        reasoning_effort: Optional explicit reasoning effort; the shared model
            factory drops it when the selected model does not support it.
        model_overrides: Optional bounded request sampling overrides accepted by
            the shared model factory.
        profile: Closed runtime profile controlling privacy, Provider retries,
            and the platform total deadline.
        deadline_monotonic: Optional absolute monotonic deadline.
        abort_event: Optional server-owned asynchronous cancellation signal.

    Returns:
        The extracted plain-text content of the model response (uncleaned).
    """
    runtime = ModelRuntime(app_config=app_config)
    # One-shot prompts are private by default and never export raw content to
    # model-level tracing.
    invoke_config: dict = {"run_name": run_name}
    response = await runtime.ainvoke(
        [
            SystemMessage(content=system_instruction),
            HumanMessage(content=user_content),
        ],
        profile=profile,
        model_name=model_name,
        thinking_enabled=thinking_enabled,
        reasoning_effort=reasoning_effort,
        model_overrides=model_overrides,
        config=invoke_config,
        deadline_monotonic=deadline_monotonic,
        abort_event=abort_event,
    )
    return extract_response_text(response.content)
