"""Closed runtime profiles for governed chat-model construction and invocation.

Provider adapters remain the sole owners of wire protocols and model-owned API Key use.
This module chooses the platform-owned call profile, delegates construction to
the shared factory, and provides one cancellation/deadline boundary around the
adapter invocation.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast

from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langsmith.run_helpers import tracing_context

from deerflow.config.app_config import AppConfig
from deerflow.models.factory import create_chat_model


class ModelRuntimeProfile(StrEnum):
    """Closed platform call profiles; callers cannot tune transport policy."""

    AGENT_GRAPH = "agent_graph"
    PRIVATE_ONESHOT = "private_oneshot"
    SENSITIVE_MULTIMODAL = "sensitive_multimodal"
    ADMIN_PROBE = "admin_probe"


@dataclass(frozen=True, slots=True)
class _ProfilePolicy:
    attach_model_tracing: bool
    provider_max_retries: int
    default_timeout_seconds: float | None
    suppress_inherited_callbacks: bool = False


_PROFILE_POLICIES = {
    # The graph root owns tracing and LLMErrorHandlingMiddleware owns retries.
    ModelRuntimeProfile.AGENT_GRAPH: _ProfilePolicy(
        attach_model_tracing=False,
        provider_max_retries=0,
        default_timeout_seconds=None,
    ),
    # Standalone calls whose raw prompts must not be exported still retain the
    # bounded Provider retry behavior used by ordinary one-shot calls.
    ModelRuntimeProfile.PRIVATE_ONESHOT: _ProfilePolicy(
        attach_model_tracing=False,
        provider_max_retries=2,
        default_timeout_seconds=600.0,
        suppress_inherited_callbacks=True,
    ),
    # Image bytes, OCR and evidence must not enter model-level external traces.
    ModelRuntimeProfile.SENSITIVE_MULTIMODAL: _ProfilePolicy(
        attach_model_tracing=False,
        provider_max_retries=0,
        default_timeout_seconds=600.0,
        suppress_inherited_callbacks=True,
    ),
    # A connection probe must represent exactly one Provider invocation.
    ModelRuntimeProfile.ADMIN_PROBE: _ProfilePolicy(
        attach_model_tracing=False,
        provider_max_retries=0,
        default_timeout_seconds=600.0,
        suppress_inherited_callbacks=True,
    ),
}


class AsyncAbortEvent(Protocol):
    """Minimal server-owned asynchronous cancellation signal."""

    def is_set(self) -> bool: ...

    async def wait(self) -> object: ...


ModelFactory = Callable[..., BaseChatModel]


def _profile_policy(profile: ModelRuntimeProfile) -> _ProfilePolicy:
    if type(profile) is not ModelRuntimeProfile:
        raise TypeError("profile must be a ModelRuntimeProfile")
    return _PROFILE_POLICIES[profile]


async def _cancel_task(task: asyncio.Future[object]) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


class ModelRuntime:
    """Build and invoke governed models without interpreting Provider protocols."""

    def __init__(
        self,
        *,
        app_config: AppConfig,
        model_factory: ModelFactory | None = None,
    ) -> None:
        self._app_config = app_config
        self._model_factory = model_factory or create_chat_model

    def build_chat_model(
        self,
        *,
        profile: ModelRuntimeProfile,
        model_name: str | None = None,
        thinking_enabled: bool = False,
        reasoning_effort: str | None = None,
        model_overrides: Mapping[str, object] | None = None,
        provider_max_retries: int | None = None,
    ) -> BaseChatModel:
        """Construct one Provider model through the shared internal factory."""

        policy = _profile_policy(profile)
        retry_limit = policy.provider_max_retries
        if provider_max_retries is not None:
            if type(provider_max_retries) is not int or not 0 <= provider_max_retries <= retry_limit:
                raise ValueError("provider_max_retries must be a nonnegative integer within the profile policy")
            retry_limit = provider_max_retries
        factory_kwargs: dict[str, object] = {
            "name": model_name,
            "thinking_enabled": thinking_enabled,
            "app_config": self._app_config,
            "attach_tracing": policy.attach_model_tracing,
            "model_overrides": model_overrides,
            "runtime_overrides": {
                "max_retries": retry_limit,
            },
        }
        if reasoning_effort is not None:
            factory_kwargs["reasoning_effort"] = reasoning_effort
        return cast(BaseChatModel, self._model_factory(**factory_kwargs))

    async def ainvoke(
        self,
        input_: LanguageModelInput,
        *,
        profile: ModelRuntimeProfile,
        model_name: str | None = None,
        thinking_enabled: bool = False,
        reasoning_effort: str | None = None,
        model_overrides: Mapping[str, object] | None = None,
        provider_max_retries: int | None = None,
        config: RunnableConfig | None = None,
        deadline_monotonic: float | None = None,
        abort_event: AsyncAbortEvent | None = None,
    ) -> BaseMessage:
        """Invoke one model under the selected profile, deadline and abort signal.

        Timeout raises :class:`TimeoutError`. A server abort or caller task
        cancellation propagates :class:`asyncio.CancelledError` after the
        Provider task has been cancelled and joined.
        """

        policy = _profile_policy(profile)
        effective_deadline = deadline_monotonic
        if effective_deadline is None and policy.default_timeout_seconds is not None:
            # The platform total deadline is independent of the adapter's
            # per-request transport timeout and includes bounded Provider
            # retries. Callers with a stricter business deadline pass one
            # absolute monotonic value, which always takes precedence.
            effective_deadline = time.monotonic() + policy.default_timeout_seconds
        if effective_deadline is not None and effective_deadline <= time.monotonic():
            raise TimeoutError
        if abort_event is not None and abort_event.is_set():
            raise asyncio.CancelledError

        model = self.build_chat_model(
            profile=profile,
            model_name=model_name,
            thinking_enabled=thinking_enabled,
            reasoning_effort=reasoning_effort,
            model_overrides=model_overrides,
            provider_max_retries=provider_max_retries,
        )
        return cast(
            BaseMessage,
            await self.ainvoke_runnable(
                model,
                input_,
                profile=profile,
                config=config,
                deadline_monotonic=effective_deadline,
                abort_event=abort_event,
            ),
        )

    async def astream(
        self,
        input_: LanguageModelInput,
        *,
        profile: ModelRuntimeProfile,
        model_name: str | None = None,
        thinking_enabled: bool = False,
        reasoning_effort: str | None = None,
        model_overrides: Mapping[str, object] | None = None,
        config: RunnableConfig | None = None,
        deadline_monotonic: float | None = None,
        abort_event: AsyncAbortEvent | None = None,
    ) -> AsyncIterator[BaseMessage]:
        """Stream one governed model call with the same privacy and abort policy."""

        policy = _profile_policy(profile)
        effective_deadline = deadline_monotonic
        if effective_deadline is None and policy.default_timeout_seconds is not None:
            effective_deadline = time.monotonic() + policy.default_timeout_seconds
        if effective_deadline is not None and effective_deadline <= time.monotonic():
            raise TimeoutError
        if abort_event is not None and abort_event.is_set():
            raise asyncio.CancelledError

        model = self.build_chat_model(
            profile=profile,
            model_name=model_name,
            thinking_enabled=thinking_enabled,
            reasoning_effort=reasoning_effort,
            model_overrides=model_overrides,
        )
        effective_config = dict(config or {})
        if policy.suppress_inherited_callbacks:
            effective_config["callbacks"] = []
        queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue()

        async def produce() -> None:
            try:
                async for chunk in model.astream(input_, config=effective_config):
                    await queue.put(("chunk", chunk))
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 - relayed to consumer
                await queue.put(("error", exc))
            else:
                await queue.put(("done", None))

        if policy.suppress_inherited_callbacks:
            with tracing_context(enabled=False):
                producer_task = asyncio.create_task(produce())
        else:
            producer_task = asyncio.create_task(produce())
        abort_task = asyncio.create_task(abort_event.wait()) if abort_event is not None else None
        try:
            while True:
                next_task = asyncio.create_task(queue.get())
                waiters: set[asyncio.Future[object]] = {next_task}
                if abort_task is not None:
                    waiters.add(abort_task)
                timeout = None
                if effective_deadline is not None:
                    timeout = max(0.0, effective_deadline - time.monotonic())
                done, _ = await asyncio.wait(
                    waiters,
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    await _cancel_task(next_task)
                    raise TimeoutError
                if abort_task is not None and (abort_task in done or (abort_event is not None and abort_event.is_set())):
                    await _cancel_task(next_task)
                    raise asyncio.CancelledError
                kind, value = next_task.result()
                if kind == "done":
                    return
                if kind == "error":
                    if isinstance(value, BaseException):
                        raise value
                    raise RuntimeError("stream failed without an exception")
                if not isinstance(value, BaseMessage):
                    raise TypeError("model stream must yield BaseMessage chunks")
                yield value
        finally:
            await _cancel_task(producer_task)
            if abort_task is not None:
                await _cancel_task(abort_task)

    @staticmethod
    async def ainvoke_runnable(
        runnable: object,
        input_: object,
        *,
        profile: ModelRuntimeProfile,
        config: RunnableConfig | None = None,
        deadline_monotonic: float | None = None,
        abort_event: AsyncAbortEvent | None = None,
    ) -> object:
        """Invoke an already-built Runnable under one governed call profile.

        This is the execution half of :meth:`ainvoke`. It exists for bound
        models, such as a model after ``bind_tools``, without letting those
        callers bypass the profile's tracing, callback, timeout, abort, and
        provider-task cleanup rules.

        Timeout raises :class:`TimeoutError`. A server abort or caller task
        cancellation propagates :class:`asyncio.CancelledError` after the
        Provider task has been cancelled and joined.
        """

        policy = _profile_policy(profile)
        effective_deadline = deadline_monotonic
        if effective_deadline is None and policy.default_timeout_seconds is not None:
            effective_deadline = time.monotonic() + policy.default_timeout_seconds
        if effective_deadline is not None and effective_deadline <= time.monotonic():
            raise TimeoutError
        if abort_event is not None and abort_event.is_set():
            raise asyncio.CancelledError

        invoke = getattr(runnable, "ainvoke", None)
        if not callable(invoke):
            raise TypeError("runnable must provide ainvoke")
        effective_config = dict(config or {})
        if policy.suppress_inherited_callbacks:
            # LangChain otherwise merges callbacks from the parent Runnable
            # context even when the model instance itself has no callbacks.
            effective_config["callbacks"] = []

        def create_invocation_task() -> asyncio.Future[object]:
            invocation = invoke(input_, config=effective_config)
            if not isinstance(invocation, Awaitable):
                raise TypeError("runnable ainvoke must return an awaitable")
            return asyncio.ensure_future(invocation)

        if policy.suppress_inherited_callbacks:
            # ``callbacks=[]`` only removes inherited handlers. LangSmith can
            # also be enabled by an ambient tracing context or environment
            # setting, so create the Provider task inside an explicit disabled
            # branch. Context variables are copied into the new asyncio task.
            with tracing_context(enabled=False):
                invocation_task = create_invocation_task()
        else:
            invocation_task = create_invocation_task()
        abort_task: asyncio.Task[object] | None = None
        if abort_event is not None:
            abort_task = asyncio.create_task(abort_event.wait())

        async def wait_for_result() -> object:
            if abort_task is None:
                return await invocation_task
            done, _ = await asyncio.wait(
                {invocation_task, abort_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            # A server-owned abort wins even when the Provider happens to
            # finish in the same event-loop turn. Returning the response in
            # that race would execute work after durable authority was revoked.
            if abort_task in done or (abort_event is not None and abort_event.is_set()):
                await _cancel_task(invocation_task)
                raise asyncio.CancelledError
            return invocation_task.result()

        try:
            if effective_deadline is None:
                return await wait_for_result()
            remaining = effective_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining):
                return await wait_for_result()
        finally:
            await _cancel_task(invocation_task)
            if abort_task is not None:
                await _cancel_task(abort_task)

    @staticmethod
    def invoke_runnable(
        runnable: object,
        input_: object,
        *,
        profile: ModelRuntimeProfile,
        config: RunnableConfig | None = None,
    ) -> object:
        """Synchronously invoke an in-graph Runnable through the runtime boundary.

        This entry exists only for LangGraph's synchronous middleware path.
        Profiles with runtime-owned deadlines or callback suppression must use
        :meth:`ainvoke_runnable`, where Provider work can be cancelled and
        joined without nesting an event loop or abandoning a worker thread.
        """

        policy = _profile_policy(profile)
        if profile is not ModelRuntimeProfile.AGENT_GRAPH:
            raise ValueError(
                "synchronous Runnable invocation only supports AGENT_GRAPH",
            )
        if policy.default_timeout_seconds is not None or policy.suppress_inherited_callbacks:
            raise AssertionError("AGENT_GRAPH sync policy must remain graph-owned")

        invoke = getattr(runnable, "invoke", None)
        if not callable(invoke):
            raise TypeError("runnable must provide invoke")
        return invoke(input_, config=dict(config or {}))


__all__ = [
    "AsyncAbortEvent",
    "ModelRuntime",
    "ModelRuntimeProfile",
]
