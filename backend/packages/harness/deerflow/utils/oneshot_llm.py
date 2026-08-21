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

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)

from deerflow.config.app_config import AppConfig
from deerflow.models.runtime import (
    AsyncAbortEvent,
    ModelRuntime,
    ModelRuntimeProfile,
)
from deerflow.utils.llm_text import extract_response_text

_REASONING_FLUSH_SECONDS = 0.075
_REASONING_FLUSH_BYTES = 4096


class _InlineReasoningExtractor:
    def __init__(self) -> None:
        self._buffer = ""
        self._inside = False

    @staticmethod
    def _partial_suffix(value: str, marker: str) -> str:
        lowered = value.lower()
        for length in range(min(len(value), len(marker) - 1), 0, -1):
            if lowered.endswith(marker[:length]):
                return value[-length:]
        return ""

    def push(self, text: str) -> str:
        data = f"{self._buffer}{text}"
        self._buffer = ""
        output: list[str] = []
        while data:
            lowered = data.lower()
            if self._inside:
                close_index = lowered.find("</think>")
                if close_index >= 0:
                    output.append(data[:close_index])
                    data = data[close_index + len("</think>") :]
                    self._inside = False
                    continue
                suffix = self._partial_suffix(data, "</think>")
                if suffix:
                    output.append(data[: -len(suffix)])
                    self._buffer = suffix
                else:
                    output.append(data)
                break
            open_index = lowered.find("<think>")
            if open_index >= 0:
                data = data[open_index + len("<think>") :]
                self._inside = True
                continue
            self._buffer = self._partial_suffix(data, "<think>")
            break
        return "".join(output)


def _structured_reasoning(message: BaseMessage) -> str:
    if not isinstance(message, AIMessage):
        return ""
    additional = getattr(message, "additional_kwargs", None)
    if isinstance(additional, Mapping):
        value = additional.get("reasoning_content")
        if isinstance(value, str):
            return value
    content = getattr(message, "content", None)
    if not isinstance(content, list):
        return ""
    result: list[str] = []
    for block in content:
        if not isinstance(block, Mapping) or block.get("type") not in {
            "thinking",
            "reasoning",
        }:
            continue
        for key in ("thinking", "reasoning", "text", "content"):
            value = block.get(key)
            if isinstance(value, str):
                result.append(value)
                break
    return "".join(result)


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
    on_reasoning_delta: Callable[[str], Awaitable[None]] | None = None,
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
    messages = [
        SystemMessage(content=system_instruction),
        HumanMessage(content=user_content),
    ]
    if on_reasoning_delta is None:
        response = await runtime.ainvoke(
            messages,
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

    response: BaseMessage | None = None
    inline = _InlineReasoningExtractor()
    pending_reasoning: list[str] = []
    pending_bytes = 0
    last_flush = time.monotonic()

    async def flush_reasoning() -> None:
        nonlocal pending_bytes, last_flush
        if not pending_reasoning:
            return
        delta = "".join(pending_reasoning)
        pending_reasoning.clear()
        pending_bytes = 0
        last_flush = time.monotonic()
        await on_reasoning_delta(delta)

    stream = runtime.astream(
        messages,
        profile=profile,
        model_name=model_name,
        thinking_enabled=thinking_enabled,
        reasoning_effort=reasoning_effort,
        model_overrides=model_overrides,
        config=invoke_config,
        deadline_monotonic=deadline_monotonic,
        abort_event=abort_event,
    ).__aiter__()
    next_chunk = asyncio.create_task(anext(stream))
    try:
        while True:
            timeout = None
            if pending_reasoning:
                timeout = max(
                    0.0,
                    _REASONING_FLUSH_SECONDS - (time.monotonic() - last_flush),
                )
            ready, _ = await asyncio.wait({next_chunk}, timeout=timeout)
            if not ready:
                await flush_reasoning()
                continue
            try:
                chunk = next_chunk.result()
            except StopAsyncIteration:
                break
            next_chunk = asyncio.create_task(anext(stream))
            response = chunk if response is None else response + chunk  # type: ignore[operator]
            reasoning = _structured_reasoning(chunk)
            if isinstance(chunk, AIMessage) and not reasoning:
                content = getattr(chunk, "content", None)
                if isinstance(content, str):
                    reasoning = inline.push(content)
            if reasoning:
                pending_reasoning.append(reasoning)
                pending_bytes += len(reasoning.encode("utf-8"))
            if pending_reasoning and pending_bytes >= _REASONING_FLUSH_BYTES:
                await flush_reasoning()
    finally:
        if not next_chunk.done():
            next_chunk.cancel()
            try:
                await next_chunk
            except asyncio.CancelledError:
                pass
    await flush_reasoning()
    if response is None:
        raise RuntimeError("model stream returned no response")
    return extract_response_text(response.content)
