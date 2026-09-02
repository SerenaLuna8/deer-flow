"""Worker stream delivery: frame batching, publishing, modes, and root-lane markers."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import ToolMessage

from deerflow.public_error_codes import (
    LLM_PUBLIC_ERROR_CODES,
    llm_error_code_for_reason,
)
from deerflow.runtime.events.stream_base import StreamBridge
from deerflow.runtime.public_token_usage import (
    project_public_sse_payload,
    project_public_subagent_event,
)
from deerflow.runtime.serialization import serialize

from .checkpoint_rollback import _message_id

logger = logging.getLogger(__name__)

# Valid stream_mode values for LangGraph's graph.astream()
_VALID_LG_MODES = {"values", "updates", "checkpoints", "tasks", "debug", "messages", "custom"}
# Only the parent graph's materialized state may authorize a Run-level model
# failure. LangGraph may forward nested-graph messages through the root
# ``messages`` transport even when ``subgraphs=False``, so neither transport
# metadata nor an absent namespace proves Lead ownership. ``values`` is the
# sole lane whose root chunk represents the parent graph's semantic state.
_LLM_ERROR_FALLBACK_AUTHORITY_MODES = frozenset({"values"})
# Keep this streaming policy separate from middleware write-authorization sets.
_TOOL_CALL_CHUNK_BATCH_SIZE = 32
_MESSAGE_TRANSPORT_METADATA_KEYS = frozenset({"model_provider"})


class _PublicTokenUsageBridge:
    """Apply one Run's frozen token-tracking policy at the SSE boundary."""

    def __init__(
        self,
        bridge: StreamBridge,
        *,
        tracking_enabled: bool,
    ) -> None:
        self._bridge = bridge
        self._tracking_enabled = tracking_enabled

    async def publish(
        self,
        run_id: str,
        event: str,
        payload: Any,
    ) -> None:
        await self._bridge.publish(
            run_id,
            event,
            project_public_sse_payload(
                event,
                payload,
                tracking_enabled=self._tracking_enabled,
            ),
        )

    async def publish_end(self, run_id: str) -> None:
        await self._bridge.publish_end(run_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bridge, name)


@dataclass
class _ToolCallChunkBatcher:
    """Batch tool-argument deltas to bound durable rows and browser parsing.

    Assistant text remains token-streamed. Tool arguments still update
    progressively, but every tool uses bounded batches so a long ``task`` or
    another non-file argument cannot delay the terminal Run frame behind tens
    of thousands of individually persisted deltas.
    """

    batch_size: int = _TOOL_CALL_CHUNK_BATCH_SIZE
    pending_identity: tuple[str, str] | None = None
    pending_message: Any | None = None
    pending_metadata: dict[str, Any] = field(default_factory=dict)
    pending_count: int = 0

    @staticmethod
    def _visible_payload(message: Any) -> tuple[Any, bool]:
        additional_kwargs = getattr(message, "additional_kwargs", None)
        sanitized_additional_kwargs = additional_kwargs
        if isinstance(additional_kwargs, dict) and ("function_call" in additional_kwargs or "tool_calls" in additional_kwargs):
            sanitized_additional_kwargs = {key: value for key, value in additional_kwargs.items() if key not in {"function_call", "tool_calls"}}
        response_metadata = getattr(message, "response_metadata", None)
        meaningful_response_metadata = {key: value for key, value in response_metadata.items() if key not in _MESSAGE_TRANSPORT_METADATA_KEYS} if isinstance(response_metadata, dict) else response_metadata
        has_visible_payload = bool(getattr(message, "content", None) or sanitized_additional_kwargs or getattr(message, "usage_metadata", None) or meaningful_response_metadata or getattr(message, "chunk_position", None) == "last")
        return sanitized_additional_kwargs, has_visible_payload

    def push(self, chunk: Any) -> list[Any]:
        if not isinstance(chunk, tuple) or len(chunk) != 2:
            return [*self.flush(), chunk]

        message, metadata = chunk
        message_id = getattr(message, "id", None)
        tool_call_chunks = getattr(message, "tool_call_chunks", None)
        if not isinstance(message_id, str) or not message_id or not isinstance(tool_call_chunks, list):
            return [*self.flush(), chunk]

        raw_namespace = None
        if isinstance(metadata, dict):
            raw_namespace = metadata.get(
                "langgraph_checkpoint_ns",
            ) or metadata.get("checkpoint_ns")
        namespace = raw_namespace if isinstance(raw_namespace, str) else ""
        identity = (namespace, message_id)
        if not tool_call_chunks:
            if self.pending_identity == identity:
                _, has_visible_payload = self._visible_payload(message)
                has_visible_payload = bool(has_visible_payload or getattr(message, "tool_calls", None) or getattr(message, "invalid_tool_calls", None))
                if not has_visible_payload:
                    # Some providers emit a transport-metadata-only chunk for
                    # every tool-argument token. It is not visible UI data and
                    # must not flush the pending tool batch.
                    return []
            return [*self.flush(), chunk]
        if not all(isinstance(tool_chunk, dict) for tool_chunk in tool_call_chunks):
            return [*self.flush(), chunk]

        model_copy = getattr(message, "model_copy", None)
        if not callable(model_copy):
            return [*self.flush(), chunk]
        sanitized_additional_kwargs, has_non_tool_payload = self._visible_payload(
            message,
        )
        outputs: list[Any] = []
        if self.pending_identity is not None and self.pending_identity != identity:
            outputs.extend(self.flush())
        tool_only_message = model_copy(
            update={
                "additional_kwargs": {},
                "chunk_position": None,
                "content": "",
                "invalid_tool_calls": [],
                "response_metadata": {},
                "tool_calls": [],
                "usage_metadata": None,
            }
        )
        self.pending_identity = identity
        self.pending_message = tool_only_message if self.pending_message is None else self.pending_message + tool_only_message
        if isinstance(metadata, dict):
            self.pending_metadata.update(metadata)
        self.pending_count += 1
        if has_non_tool_payload:
            # Keep all tool bytes ahead of visible content, finish metadata, or
            # usage receipts from the same provider frame.
            outputs.extend(self.flush())
            visible_message = model_copy(
                update={
                    "additional_kwargs": sanitized_additional_kwargs,
                    "invalid_tool_calls": [],
                    "tool_call_chunks": [],
                    "tool_calls": [],
                }
            )
            outputs.append((visible_message, metadata))
        elif self.pending_count >= self.batch_size:
            outputs.extend(self.flush())
        return outputs

    def flush(self) -> list[Any]:
        if self.pending_message is None:
            return []
        chunk = (self.pending_message, self.pending_metadata)
        self.pending_identity = None
        self.pending_message = None
        self.pending_metadata = {}
        self.pending_count = 0
        return [chunk]

    def finish(self) -> list[Any]:
        """Flush at a values or end-of-stream boundary."""
        return self.flush()


_TEXT_DELTA_FLUSH_BYTES = 4096
# response_metadata keys that mark the end of a provider message stream.
_TEXT_DELTA_FINISH_KEYS = ("finish_reason", "stop_reason", "done_reason")
_TEXT_DELTA_FLUSH_DUE = object()


@dataclass
class _TextDeltaCoalescer:
    """Merge consecutive root assistant text deltas into bounded frames (U2).

    Per-token durable frames dominate ``run_events`` row volume. Deltas that
    belong to the same root assistant message are merged with the same ``+``
    operator SDK clients use for accumulation, so the reassembled text is
    byte-identical — only frame boundaries change. Namespaced (subgraph)
    frames and every non-text frame are untouched.

    Flush discipline mirrors ``_ToolCallChunkBatcher``: any
    non-coalescible frame flushes the buffer before passing through, so
    inter-frame order is preserved. Additional bounds: a time window
    (``worker.stream.text_delta_flush_ms``), 4 KiB of accumulated content,
    message-identity switches, and provider finish markers. When the buffer
    is empty and the last flush is at least one window old, a delta ships
    immediately (leading edge) so slow token streams keep per-token latency.
    """

    window_seconds: float
    max_pending_bytes: int = _TEXT_DELTA_FLUSH_BYTES
    pending_message: Any | None = None
    pending_metadata: dict[str, Any] = field(default_factory=dict)
    pending_message_id: str | None = None
    pending_bytes: int = 0
    window_started_at: float = 0.0
    last_flush_at: float = float("-inf")

    @staticmethod
    def _content_size(content: Any) -> int:
        if isinstance(content, str):
            return len(content.encode("utf-8", errors="ignore"))
        return sum(len(str(block).encode("utf-8", errors="ignore")) for block in content)

    @classmethod
    def _message_size(cls, message: Any) -> int:
        """Count UTF-8 payload bytes that grow while message chunks merge."""

        size = cls._content_size(getattr(message, "content", ""))
        additional_kwargs = getattr(message, "additional_kwargs", None)
        if isinstance(additional_kwargs, dict):
            reasoning = additional_kwargs.get("reasoning_content")
            if isinstance(reasoning, (str, list)):
                size += cls._content_size(reasoning)
        return size

    @classmethod
    def _text_delta_parts(cls, chunk: Any) -> tuple[Any, dict[str, Any], str, bool] | None:
        """Return ``(message, metadata, message_id, is_final)`` for a pure text delta."""
        if not isinstance(chunk, tuple) or len(chunk) != 2:
            return None
        message, metadata = chunk
        message_id = getattr(message, "id", None)
        if not isinstance(message_id, str) or not message_id:
            return None
        # Only AIMessageChunk carries tool_call_chunks; requiring the list to
        # be empty keeps tool-argument streams on the file-batcher path.
        tool_call_chunks = getattr(message, "tool_call_chunks", None)
        if not isinstance(tool_call_chunks, list) or tool_call_chunks:
            return None
        if getattr(message, "tool_calls", None) or getattr(message, "invalid_tool_calls", None):
            return None
        # Provider extras such as DeepSeek's reasoning_content merge with the
        # same associative ``+`` the SDK applies client-side; only tool-call
        # assembly fragments must stay per-frame.
        additional_kwargs = getattr(message, "additional_kwargs", None)
        if isinstance(additional_kwargs, dict):
            if "function_call" in additional_kwargs or "tool_calls" in additional_kwargs:
                return None
        elif additional_kwargs is not None:
            return None
        content = getattr(message, "content", None)
        if not isinstance(content, (str, list)):
            return None
        response_metadata = getattr(message, "response_metadata", None)
        is_final = bool(getattr(message, "usage_metadata", None))
        if isinstance(response_metadata, dict) and any(response_metadata.get(key) for key in _TEXT_DELTA_FINISH_KEYS):
            is_final = True
        return message, metadata if isinstance(metadata, dict) else {}, message_id, is_final

    def push(self, chunk: Any) -> list[Any]:
        parts = self._text_delta_parts(chunk)
        if parts is None:
            return [*self.flush(), chunk]
        message, metadata, message_id, is_final = parts

        now = time.monotonic()
        outputs: list[Any] = []
        if self.pending_message_id is not None and self.pending_message_id != message_id:
            outputs.extend(self.flush())

        if self.pending_message is None:
            if is_final or now - self.last_flush_at >= self.window_seconds:
                # Leading edge: the stream is not bursting, ship directly.
                self.last_flush_at = now
                outputs.append(chunk)
                return outputs
            self.pending_message = message
            self.pending_metadata = dict(metadata)
            self.pending_message_id = message_id
            self.pending_bytes = self._message_size(message)
            self.window_started_at = now
        else:
            self.pending_message = self.pending_message + message
            self.pending_metadata.update(metadata)
            self.pending_bytes += self._message_size(message)

        if is_final or now - self.window_started_at >= self.window_seconds or self.pending_bytes >= self.max_pending_bytes:
            outputs.extend(self.flush())
        return outputs

    def flush(self) -> list[Any]:
        if self.pending_message is None:
            return []
        chunk = (self.pending_message, self.pending_metadata)
        self.pending_message = None
        self.pending_metadata = {}
        self.pending_message_id = None
        self.pending_bytes = 0
        self.last_flush_at = time.monotonic()
        return [chunk]

    def pending_flush_delay(self) -> float | None:
        """Seconds until the buffered frame's hard deadline, if any."""
        if self.pending_message is None:
            return None
        deadline = self.window_started_at + self.window_seconds
        return max(0.0, deadline - time.monotonic())


async def _iter_with_text_delta_deadline(source: Any, coalescer: _TextDeltaCoalescer | None):
    """Yield stream items plus a timer marker when pending text must flush.

    Merely checking elapsed time from ``push()`` leaves a buffered final token
    parked until the provider emits another frame.  Keep one ``__anext__``
    task alive while a timeout races it; a timeout never cancels the provider
    iterator and therefore cannot truncate the graph stream.
    """

    iterator = source.__aiter__()
    if coalescer is None:
        # The explicit opt-out restores the old direct-iteration path. Besides
        # avoiding one Task allocation per frame, this keeps cancellation and
        # ContextVar execution in the caller task exactly as before U2.
        try:
            async for item in iterator:
                yield item
        finally:
            close = getattr(iterator, "aclose", None)
            if callable(close):
                await close()
        return

    pending_next: asyncio.Future[Any] | None = None
    try:
        while True:
            if pending_next is None:
                pending_next = asyncio.ensure_future(iterator.__anext__())

            flush_delay = coalescer.pending_flush_delay() if coalescer is not None else None
            if flush_delay is None:
                done, _pending = await asyncio.wait({pending_next})
            else:
                done, _pending = await asyncio.wait(
                    {pending_next},
                    timeout=flush_delay,
                )
                if not done:
                    yield _TEXT_DELTA_FLUSH_DUE
                    continue

            completed = pending_next
            pending_next = None
            try:
                yield completed.result()
            except StopAsyncIteration:
                return
    finally:
        if pending_next is not None:
            if not pending_next.done():
                pending_next.cancel()
            # A provider can finish (or fail) while this generator is suspended
            # after yielding the timer marker. Always retrieve that outcome so
            # abort/close paths cannot leak an unobserved Task exception.
            await asyncio.gather(pending_next, return_exceptions=True)
        close = getattr(iterator, "aclose", None)
        if callable(close):
            await close()


class _SubagentEventBuffer:
    """Buffer subagent ``task_*`` step events and flush them in one locked batch (#3779).

    The live SSE bridge already forwards these events for real-time display; this
    additionally writes them so the subtask card's step history survives a reload.

    ``RunEventStore.put`` is documented as a low-frequency path — on Postgres each
    call opens its own transaction and takes a per-thread advisory lock. A deep
    subagent (``general-purpose`` runs up to ``max_turns=150``) emits hundreds of
    ``task_running`` steps on the hot stream loop, so persisting each with
    ``put()`` would serialize against the run's own message-batch writer. This
    accumulates recognized subagent events and writes them with ``put_batch``,
    which acquires the lock once per batch, honoring the store's contract.

    Best-effort: a missing store (run_events not configured) or an unrecognized
    chunk is a no-op, flush failures are logged but never propagate into the
    stream loop, and terminal ``subagent.end`` events flush eagerly so a completed
    subagent's step history is durable promptly rather than only at run end.
    """

    #: Flush once this many events are buffered, bounding memory and reload lag on
    #: a single deep subagent without paying a per-step lock.
    FLUSH_THRESHOLD = 25

    def __init__(
        self,
        event_store: Any | None,
        thread_id: str,
        run_id: str,
        scope: Any | None = None,
        *,
        token_usage_tracking_enabled: bool = True,
    ) -> None:
        self._event_store = event_store
        self._thread_id = thread_id
        self._run_id = run_id
        self._scope = scope
        self._token_usage_tracking_enabled = token_usage_tracking_enabled
        self._pending: list[dict[str, Any]] = []

    async def add(self, chunk: Any) -> None:
        """Buffer one custom stream chunk; flush on a terminal event or threshold."""
        if self._event_store is None:
            return
        # Lazy import: importing deerflow.subagents at module load triggers its
        # package __init__ (executor → agents → tools → task_tool), which imports
        # back from deerflow.subagents and deadlocks at gateway startup. Deferring
        # it to call time (after all modules are loaded) breaks that cycle.
        from deerflow.subagents.step_events import subagent_run_event

        record = subagent_run_event(
            project_public_subagent_event(
                chunk,
                tracking_enabled=self._token_usage_tracking_enabled,
            )
        )
        if record is None:
            return
        self._pending.append({"thread_id": self._thread_id, "run_id": self._run_id, **record})
        if record["event_type"] == "subagent.end" or len(self._pending) >= self.FLUSH_THRESHOLD:
            await self.flush()

    async def flush(self) -> None:
        """Persist buffered events in one ``put_batch`` call; swallow store errors."""
        if self._event_store is None or not self._pending:
            return
        batch = self._pending
        self._pending = []
        try:
            if self._scope is None:
                await self._event_store.put_batch(batch)
            else:
                await self._event_store.put_batch(batch, scope=self._scope)
        except asyncio.CancelledError:
            self._pending = batch + self._pending
            raise
        except Exception:
            self._pending = batch + self._pending
            logger.warning("Run %s: failed to persist %d subagent step event(s)", self._run_id, len(batch), exc_info=True)


def _lg_mode_to_sse_event(mode: str) -> str:
    """Map LangGraph internal stream_mode name to SSE event name.

    LangGraph's ``astream(stream_mode="messages")`` produces message
    tuples.  The SSE protocol calls this ``messages-tuple`` when the
    client explicitly requests it, but the default SSE event name used
    by LangGraph Platform is simply ``"messages"``.
    """
    # All LG modes map 1:1 to SSE event names — "messages" stays "messages"
    return mode


def _namespaced_sse_event(mode: str, namespace: tuple[str, ...]) -> str:
    """Encode a LangGraph namespace in the SSE event name.

    The LangGraph SDK treats ``<mode>|<namespace segment>|...`` as the
    subgraph form of a stream event. Keeping the payload unchanged while
    suffixing the event name prevents child ``values`` and ``messages`` chunks
    from being projected into the lead-agent conversation.
    """
    event = _lg_mode_to_sse_event(mode)
    if not namespace:
        return event
    return "|".join((event, *namespace))


async def _publish_stream_item(
    *,
    bridge: Any,
    run_id: str,
    mode: str,
    chunk: Any,
    namespace: tuple[str, ...],
    tool_call_chunk_batcher: _ToolCallChunkBatcher | None,
    text_delta_coalescer: _TextDeltaCoalescer | None,
    subagent_events: _SubagentEventBuffer,
) -> None:
    """Publish one frame without letting child data enter root consumers.

    Every emitted frame still goes through the injected StreamBridge. Private
    Workers therefore retain the existing lease check, PostgreSQL commit, and
    post-commit notification boundary; batching only reduces how often root
    tool-argument deltas and root text deltas cross that boundary.
    """

    sse_event = _namespaced_sse_event(mode, namespace)
    if namespace:
        # Subgraph frames keep current behavior and never disturb root
        # batching: they render in their own UI lane, so holding root text
        # for at most one window does not reorder anything a consumer can
        # observe per message id.
        await bridge.publish(
            run_id,
            sse_event,
            serialize(chunk, mode=mode),
        )
        return

    if mode != "messages":
        # Root mode switch: flush coalesced text first, routed through the
        # tool batcher so an older pending argument batch stays ahead.
        pending_chunks: list[Any] = []
        if text_delta_coalescer is not None:
            for frame in text_delta_coalescer.flush():
                pending_chunks.extend(tool_call_chunk_batcher.push(frame) if tool_call_chunk_batcher is not None else [frame])
        if tool_call_chunk_batcher is not None:
            pending_chunks.extend(tool_call_chunk_batcher.finish() if mode == "values" else tool_call_chunk_batcher.flush())
        for publish_chunk in pending_chunks:
            await bridge.publish(
                run_id,
                "messages",
                serialize(publish_chunk, mode="messages"),
            )

    frames = text_delta_coalescer.push(chunk) if mode == "messages" and text_delta_coalescer is not None else [chunk]
    for frame in frames:
        chunks_to_publish = tool_call_chunk_batcher.push(frame) if mode == "messages" and tool_call_chunk_batcher is not None else [frame]
        for publish_chunk in chunks_to_publish:
            await bridge.publish(
                run_id,
                sse_event,
                serialize(publish_chunk, mode=mode),
            )
    if mode == "custom":
        await subagent_events.add(chunk)


@dataclass(frozen=True, slots=True)
class _LLMErrorFallback:
    message: str
    error_code: str


def _error_fallback_from_metadata(
    metadata: dict[str, Any],
    content: Any,
) -> _LLMErrorFallback:
    detail = metadata.get("error_detail")
    if isinstance(detail, str) and detail.strip():
        message = detail.strip()
    else:
        reason = metadata.get("error_reason")
        if isinstance(reason, str) and reason.strip():
            message = reason.strip()
        elif isinstance(content, str) and content.strip():
            message = content.strip()[:2000]
        else:
            message = "LLM provider failed after retries"

    raw_error_code = metadata.get("error_code")
    legacy_error_code = metadata.get("error_detail")
    error_code = (
        raw_error_code
        if isinstance(raw_error_code, str) and raw_error_code in LLM_PUBLIC_ERROR_CODES
        else (legacy_error_code if isinstance(legacy_error_code, str) and legacy_error_code in LLM_PUBLIC_ERROR_CODES else llm_error_code_for_reason(metadata.get("error_reason")))
    )
    return _LLMErrorFallback(message=message, error_code=error_code)


def _current_run_host_execution_approval_id(
    value: Any,
    run_id: str,
) -> str | None:
    """Find a trusted host-approval anchor owned by the current Run.

    Values-mode chunks replay the thread's full message history, so merely
    finding an approval artifact would make every later Run stop on an old
    request.  The app-owned approval port stamps the source Run into the
    artifact; require that exact coordinate before treating the chunk as a
    suspension boundary.
    """

    seen: set[int] = set()

    def walk(obj: Any) -> str | None:
        oid = id(obj)
        if oid in seen:
            return None
        seen.add(oid)

        if isinstance(obj, ToolMessage):
            artifact = obj.artifact
            approval = artifact.get("host_execution_approval") if isinstance(artifact, dict) else None
            if (
                isinstance(approval, dict)
                and approval.get("schema_version") == 1
                and approval.get("kind") == "local_shell"
                and approval.get("source_run_id") == run_id
                and isinstance(approval.get("approval_id"), str)
                and bool(approval["approval_id"])
                and isinstance(approval.get("source_tool_call_id"), str)
                and bool(approval["source_tool_call_id"])
            ):
                return approval["approval_id"]

        if isinstance(obj, dict):
            for item in obj.values():
                approval_id = walk(item)
                if approval_id is not None:
                    return approval_id
            return None
        if isinstance(obj, (list, tuple, set)):
            for item in obj:
                approval_id = walk(item)
                if approval_id is not None:
                    return approval_id
        return None

    return walk(value)


def _contains_current_run_host_execution_approval(
    value: Any,
    run_id: str,
) -> bool:
    """Compatibility predicate backed by the exact typed approval anchor."""

    return _current_run_host_execution_approval_id(value, run_id) is not None


def _try_extract_llm_error_fallback(
    obj: Any,
    pre_existing_ids: set[str] | None = None,
) -> _LLMErrorFallback | None:
    """Try to extract fallback marker from a single message object or dict.

    Messages whose id appears in ``pre_existing_ids`` are skipped — those are
    history checkpointed by a *prior* run on this thread and any fallback
    marker on them was already accounted for when that earlier run finished.
    Without this filter, a single past run that ended with a fallback marker
    would mark every subsequent run on the same thread as ``error``, because
    LangGraph replays the full message history through ``stream_mode="values"``.
    """
    if pre_existing_ids:
        msg_id = _message_id(obj)
        if msg_id is not None and msg_id in pre_existing_ids:
            return None

    additional_kwargs = getattr(obj, "additional_kwargs", None)
    if isinstance(additional_kwargs, dict) and additional_kwargs.get("deerflow_error_fallback"):
        return _error_fallback_from_metadata(additional_kwargs, getattr(obj, "content", None))

    if isinstance(obj, dict):
        nested_kwargs = obj.get("additional_kwargs")
        if isinstance(nested_kwargs, dict) and nested_kwargs.get("deerflow_error_fallback"):
            return _error_fallback_from_metadata(nested_kwargs, obj.get("content"))
    return None


def _extract_llm_error_fallback(
    value: Any,
    pre_existing_ids: set[str] | None = None,
) -> _LLMErrorFallback | None:
    """Find LLM fallback markers in streamed LangGraph chunks.

    Error fallback messages returned by model-call middleware are not guaranteed
    to pass through LLM end callbacks, but they do appear in graph state chunks.

    Messages whose id appears in ``pre_existing_ids`` are ignored — they are
    history from prior runs on the same thread (LangGraph replays the full
    messages channel in ``stream_mode="values"`` chunks), and any error
    fallback in that history was already resolved when its run finished.
    """
    # Fast path: large state chunks produced by stream_mode="values" have a
    # top-level "messages" list. Scanning only that list avoids expensive deep
    # recursion into large state dicts.
    if isinstance(value, dict):
        messages = value.get("messages")
        if isinstance(messages, (list, tuple)):
            for msg in messages:
                result = _try_extract_llm_error_fallback(msg, pre_existing_ids)
                if result is not None:
                    return result
            # Fallback marker is attached to an AI message in the messages
            # channel; it will never appear elsewhere in a values chunk.
            return None
        # No top-level "messages" — this is likely an "updates" chunk (small
        # dict keyed by node name). Fall through to deep walk, which is cheap
        # for these payloads.

    # Deep walk for updates / messages / tuple / list modes. Payloads are
    # small, so full recursion is acceptable here.
    seen: set[int] = set()

    def walk(obj: Any) -> _LLMErrorFallback | None:
        oid = id(obj)
        if oid in seen:
            return None
        seen.add(oid)

        result = _try_extract_llm_error_fallback(obj, pre_existing_ids)
        if result is not None:
            return result

        if isinstance(obj, dict):
            for item in obj.values():
                result = walk(item)
                if result is not None:
                    return result
            return None

        if isinstance(obj, (list, tuple, set)):
            for item in obj:
                result = walk(item)
                if result is not None:
                    return result
        return None

    return walk(value)


def _unpack_stream_item(
    item: Any,
    lg_modes: list[str],
    stream_subgraphs: bool,
) -> tuple[tuple[str, ...], str | None, Any]:
    """Unpack a multi-mode or subgraph item into (namespace, mode, chunk).

    Returns ``((), None, None)`` if the item cannot be parsed.
    """
    if stream_subgraphs:
        if isinstance(item, tuple) and len(item) == 3:
            namespace, mode, chunk = item
            return _normalize_stream_namespace(namespace), str(mode), chunk
        if isinstance(item, tuple) and len(item) == 2:
            first, chunk = item
            if isinstance(first, (list, tuple)):
                mode = lg_modes[0] if len(lg_modes) == 1 else None
                return _normalize_stream_namespace(first), mode, chunk
            return (), str(first), chunk
        return (), None, None

    if isinstance(item, tuple) and len(item) == 2:
        mode, chunk = item
        return (), str(mode), chunk

    # Fallback: single-element output from first mode
    return (), lg_modes[0] if lg_modes else None, item


def _normalize_stream_namespace(namespace: Any) -> tuple[str, ...]:
    """Return the generated LangGraph namespace as protocol segments."""
    if isinstance(namespace, str):
        return tuple(segment for segment in namespace.split("|") if segment)
    if isinstance(namespace, (list, tuple)):
        return tuple(str(segment) for segment in namespace if str(segment))
    return ()


@dataclass(frozen=True, slots=True)
class ResolvedStreamModes:
    """LangGraph modes the Worker consumes and the subset the caller receives."""

    lg_modes: list[str]
    published_lg_modes: frozenset[str]


def resolve_stream_modes(requested_modes: set[str]) -> ResolvedStreamModes:
    """Map requested SSE modes onto LangGraph modes and always consume ``values``.

    ``events`` is not a valid ``astream`` mode and is skipped; ``messages-tuple``
    maps to LangGraph ``messages``. Order is preserved and duplicates removed.
    The parent graph's ``values`` lane is always consumed for semantic
    authority even when the caller did not request it; ``published_lg_modes``
    records the caller-visible subset.
    """
    lg_modes: list[str] = []
    for m in requested_modes:
        if m == "messages-tuple":
            lg_modes.append("messages")
        elif m == "events":
            continue
        elif m in _VALID_LG_MODES:
            lg_modes.append(m)
    seen: set[str] = set()
    deduped: list[str] = []
    for m in lg_modes:
        if m not in seen:
            seen.add(m)
            deduped.append(m)
    published_lg_modes = frozenset(deduped)
    lg_modes = deduped or ["values"]
    if "values" not in lg_modes:
        lg_modes.append("values")
    return ResolvedStreamModes(lg_modes=lg_modes, published_lg_modes=published_lg_modes)


__all__ = ["ResolvedStreamModes", "resolve_stream_modes"]
