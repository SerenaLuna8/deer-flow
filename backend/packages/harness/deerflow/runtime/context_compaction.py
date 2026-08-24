"""Manual thread-context compaction helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any

from langgraph.types import Overwrite

from deerflow.agents.memory.snip import (
    MEMORY_ARCHIVE_CONTEXT_KEY,
    SnipArchiveContext,
)
from deerflow.agents.middlewares.provider_request_usage import (
    ProviderRequestUsageUnsupported,
    measure_profile_snapshot_context,
)
from deerflow.agents.middlewares.summarization_middleware import (
    ContextTriggerUsage,
    DeerFlowSummarizationMiddleware,
    SnipCompactionFailed,
    SnipPromptBudgetTooSmall,
    SnipSourceTooLarge,
    create_summarization_middleware,
)
from deerflow.agents.provider_request_contract import (
    PROVIDER_REQUEST_MEASUREMENT_STATE_KEY,
    PROVIDER_REQUEST_PROFILE_STATE_KEY,
)
from deerflow.config.app_config import AppConfig, get_app_config
from deerflow.models import ModelRuntime, ModelRuntimeProfile
from deerflow.runtime.checkpoint_state import CheckpointStateAccessor


class ContextCompactionDisabled(RuntimeError):
    """Raised when manual compaction is requested while summarization is disabled."""


class ContextCompactionFailed(RuntimeError):
    """Raised when a compressible thread cannot be summarized."""


class ContextUsageUnsupported(ContextCompactionFailed):
    """Raised when the Gauge cannot construct its declared safety contract."""


@dataclass(frozen=True)
class ThreadCompactionResult:
    """Result returned after a manual context-compaction attempt."""

    thread_id: str
    compacted: bool
    reason: str | None = None
    removed_message_count: int = 0
    preserved_message_count: int = 0
    summary_updated: bool = False
    checkpoint_id: str | None = None
    total_tokens: int = 0


@dataclass(frozen=True)
class PreparedThreadCompaction:
    """A compaction result prepared from one immutable source checkpoint."""

    thread_id: str
    source_checkpoint_id: str
    result: ThreadCompactionResult
    write_config: dict[str, Any] | None = None
    update_values: dict[str, Any] | None = None


@dataclass(frozen=True)
class ThreadContextUsage:
    """Current retained Thread context measured against automatic triggers."""

    enabled: bool
    estimated_tokens: int
    error_allowance_tokens: int
    safety_bound_tokens: int
    provider_input_tokens: int | None
    estimator_revision: str | None
    error_contract: str | None
    components: dict[str, dict[str, int]]
    fixed_over_trigger: bool
    message_count: int
    summary_present: bool
    context_window_tokens: int | None
    triggers: tuple[ContextTriggerUsage, ...]
    primary_trigger: ContextTriggerUsage | None


def _create_compaction_middleware(
    *,
    app_config: AppConfig,
    keep: tuple[str, int | float] | None,
) -> DeerFlowSummarizationMiddleware:
    middleware = create_summarization_middleware(app_config=app_config, keep=keep)
    if middleware is None:
        raise ContextCompactionDisabled("Context compaction is disabled.")
    return middleware


def _checkpoint_id(snapshot: Any) -> str:
    config = getattr(snapshot, "config", {}) or {}
    configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
    value = configurable.get("checkpoint_id") if isinstance(configurable, dict) else None
    if not isinstance(value, str) or not value:
        raise ContextCompactionFailed("Compaction source checkpoint has no identity.")
    return value


def has_complete_turns(messages: object) -> bool:
    """Return whether a materialized Thread state has an archivable turn."""

    return isinstance(messages, list) and bool(DeerFlowSummarizationMiddleware._complete_turn_ranges(messages))


def measure_thread_context_usage(
    snapshot: Any,
    *,
    app_config: AppConfig,
    context_model_name: str | None = None,
    provider_request_profile: Mapping[str, object] | None = None,
    expected_authority_identity: str | None = None,
    require_provider_request_profile: bool = False,
) -> ThreadContextUsage:
    """Inspect one materialized checkpoint without invoking or mutating it."""

    channel_values = getattr(snapshot, "values", None) or {}
    if not isinstance(channel_values, dict):
        raise ContextCompactionFailed("Context checkpoint values are invalid.")
    messages = channel_values.get("messages")
    if messages is None:
        messages = []
    if not isinstance(messages, list):
        raise ContextCompactionFailed("Context checkpoint messages are invalid.")
    summary_value = channel_values.get("summary_text")
    summary_text = summary_value if isinstance(summary_value, str) else None
    raw_profile = provider_request_profile if provider_request_profile is not None else channel_values.get(PROVIDER_REQUEST_PROFILE_STATE_KEY)
    profile = raw_profile if isinstance(raw_profile, Mapping) else None
    if profile is None and expected_authority_identity is not None:
        raise ContextCompactionFailed("The active Run has not persisted its provider request profile.")
    if profile is None and require_provider_request_profile:
        raise ContextUsageUnsupported("Context Gauge requires an immutable provider request profile.")
    if profile is not None:
        if provider_request_profile is None and expected_authority_identity is None and profile.get("authority_identity") is not None:
            raise ContextCompactionFailed("A completed Run profile cannot authorize an idle Gauge.")
        if expected_authority_identity is not None and profile.get("authority_identity") != expected_authority_identity:
            raise ContextCompactionFailed("Provider request profile authority does not match the active Run.")
        if context_model_name is not None and profile.get("model_name") != context_model_name:
            raise ContextCompactionFailed("Provider request profile model does not match the selected Lead model.")
        try:
            profile_measurement = measure_profile_snapshot_context(
                profile,
                channel_values,
            )
        except ProviderRequestUsageUnsupported as exc:
            raise ContextUsageUnsupported("Provider request profile cannot measure this Thread safely.") from exc
    else:
        profile_measurement = None

    context_model = (
        ModelRuntime(app_config=app_config).build_chat_model(
            profile=ModelRuntimeProfile.AGENT_GRAPH,
            model_name=context_model_name,
            thinking_enabled=False,
        )
        if context_model_name is not None and profile_measurement is None
        else None
    )
    middleware = create_summarization_middleware(
        app_config=app_config,
        context_model=context_model,
    )
    if middleware is None:
        if profile_measurement is not None:
            context_window = profile.get("max_input_tokens")
            return ThreadContextUsage(
                enabled=False,
                estimated_tokens=profile_measurement.estimated_tokens,
                error_allowance_tokens=profile_measurement.error_allowance_tokens,
                safety_bound_tokens=profile_measurement.safety_bound_tokens,
                provider_input_tokens=_profile_provider_input_tokens(
                    channel_values,
                    profile,
                    expected_authority_identity=expected_authority_identity,
                ),
                estimator_revision=str(profile["estimator_revision"]),
                error_contract=str(profile["error_contract"]),
                components={name: component.snapshot() for name, component in profile_measurement.components.items()},
                fixed_over_trigger=False,
                message_count=profile_measurement.message_count,
                summary_present=bool(summary_text),
                context_window_tokens=(context_window if isinstance(context_window, int) and context_window > 0 else None),
                triggers=(),
                primary_trigger=None,
            )
        return ThreadContextUsage(
            enabled=False,
            estimated_tokens=0,
            error_allowance_tokens=0,
            safety_bound_tokens=0,
            provider_input_tokens=None,
            estimator_revision=None,
            error_contract=None,
            components={},
            fixed_over_trigger=False,
            message_count=0,
            summary_present=bool(summary_text),
            context_window_tokens=None,
            triggers=(),
            primary_trigger=None,
        )

    if profile_measurement is None:
        measurement = middleware.measure_context_usage(
            list(messages),
            summary_text=summary_text,
        )
        error_allowance_tokens = 0
        safety_bound_tokens = measurement.estimated_tokens
        provider_input_tokens = None
        estimator_revision = None
        error_contract = None
        components: dict[str, dict[str, int]] = {}
        fixed_over_trigger = False
    else:
        context_window = profile.get("max_input_tokens")
        measurement = middleware.measure_provider_request_usage(
            profile_measurement,
            summary_present=bool(summary_text),
            context_window_tokens=(context_window if isinstance(context_window, int) and context_window > 0 else None),
        )
        error_allowance_tokens = profile_measurement.error_allowance_tokens
        safety_bound_tokens = profile_measurement.safety_bound_tokens
        provider_input_tokens = _profile_provider_input_tokens(
            channel_values,
            profile,
            expected_authority_identity=expected_authority_identity,
        )
        estimator_revision = str(profile["estimator_revision"])
        error_contract = str(profile["error_contract"])
        components = {name: component.snapshot() for name, component in profile_measurement.components.items()}
        fixed_over_trigger = middleware.fixed_component_over_trigger(
            profile,
            profile_measurement,
        )
    return ThreadContextUsage(
        enabled=True,
        estimated_tokens=measurement.estimated_tokens,
        error_allowance_tokens=error_allowance_tokens,
        safety_bound_tokens=safety_bound_tokens,
        provider_input_tokens=provider_input_tokens,
        estimator_revision=estimator_revision,
        error_contract=error_contract,
        components=components,
        fixed_over_trigger=fixed_over_trigger,
        message_count=measurement.message_count,
        summary_present=measurement.summary_present,
        context_window_tokens=measurement.context_window_tokens,
        triggers=measurement.triggers,
        primary_trigger=measurement.primary_trigger,
    )


def _profile_provider_input_tokens(
    state: Mapping[str, object],
    profile: Mapping[str, object],
    *,
    expected_authority_identity: str | None,
) -> int | None:
    if profile.get("capture_provider_input_tokens") is not True:
        return None
    raw = state.get(PROVIDER_REQUEST_MEASUREMENT_STATE_KEY)
    if not isinstance(raw, Mapping):
        return None
    if (
        raw.get("version") != 1
        or raw.get("profile_fingerprint") != profile.get("profile_fingerprint")
        or raw.get("model_name") != profile.get("model_name")
        or raw.get("authority_identity") != profile.get("authority_identity")
        or (expected_authority_identity is not None and raw.get("run_id") != expected_authority_identity)
    ):
        return None
    value = raw.get("provider_input_tokens")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


async def prepare_thread_compaction(
    accessor: CheckpointStateAccessor,
    thread_id: str,
    *,
    keep: tuple[str, int | float] | None = None,
    force: bool = True,
    user_id: str | None = None,
    agent_name: str | None = None,
    app_config: AppConfig | None = None,
    snapshot: Any | None = None,
    authorization_boundary: object | None = None,
    memory_archive_context: SnipArchiveContext | None = None,
) -> PreparedThreadCompaction:
    """Summarize one checkpoint without persisting the prepared replacement."""
    resolved_app_config = app_config or get_app_config()
    middleware = _create_compaction_middleware(app_config=resolved_app_config, keep=keep)

    read_config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    if snapshot is None:
        snapshot = await accessor.aget(read_config)
    source_checkpoint_id = _checkpoint_id(snapshot)
    if memory_archive_context is not None:
        if type(memory_archive_context) is not SnipArchiveContext:
            raise ContextCompactionFailed(
                "Memory archive context is invalid.",
            )
        if memory_archive_context.source_checkpoint_id is not None and memory_archive_context.source_checkpoint_id != source_checkpoint_id:
            raise ContextCompactionFailed(
                "Memory archive source checkpoint changed.",
            )
        memory_archive_context = replace(
            memory_archive_context,
            source_checkpoint_id=source_checkpoint_id,
        )

    channel_values = snapshot.values or {}
    messages = channel_values.get("messages")
    if not isinstance(messages, list) or not messages:
        return PreparedThreadCompaction(
            thread_id=thread_id,
            source_checkpoint_id=source_checkpoint_id,
            result=ThreadCompactionResult(thread_id=thread_id, compacted=False, reason="not_enough_messages"),
        )

    state = {
        "messages": list(messages),
        "summary_text": channel_values.get("summary_text"),
    }

    runtime_context = {"thread_id": thread_id, "user_id": user_id}
    if memory_archive_context is not None:
        runtime_context[MEMORY_ARCHIVE_CONTEXT_KEY] = memory_archive_context
    if agent_name:
        runtime_context["agent_name"] = agent_name
    if authorization_boundary is not None:
        runtime_context["__authorization_boundary"] = authorization_boundary
    runtime = SimpleNamespace(context=runtime_context)
    try:
        result = await middleware.acompact_state(state, runtime, force=force)  # type: ignore[arg-type]
    except SnipCompactionFailed:
        return PreparedThreadCompaction(
            thread_id=thread_id,
            source_checkpoint_id=source_checkpoint_id,
            result=ThreadCompactionResult(
                thread_id=thread_id,
                compacted=False,
                reason="compaction_failed",
            ),
        )
    except SnipPromptBudgetTooSmall:
        return PreparedThreadCompaction(
            thread_id=thread_id,
            source_checkpoint_id=source_checkpoint_id,
            result=ThreadCompactionResult(
                thread_id=thread_id,
                compacted=False,
                reason="prompt_budget_too_small",
            ),
        )
    except SnipSourceTooLarge:
        return PreparedThreadCompaction(
            thread_id=thread_id,
            source_checkpoint_id=source_checkpoint_id,
            result=ThreadCompactionResult(
                thread_id=thread_id,
                compacted=False,
                reason="source_too_large",
            ),
        )
    if result is None:
        reason = "not_enough_messages"
        if keep is not None and keep[0] == "messages" and keep[1] == 0 and has_complete_turns(messages):
            # Dream's keep=messages:0 request is an explicit archive barrier. If
            # an eligible complete turn exists, returning "not enough" would let
            # the caller mistake a prompt-budget, model, or SNIP validation
            # failure for successful exhaustion and continue into Dream. Keep the
            # failure observable so that workflow can fail closed.
            reason = "compaction_failed"
        return PreparedThreadCompaction(
            thread_id=thread_id,
            source_checkpoint_id=source_checkpoint_id,
            result=ThreadCompactionResult(
                thread_id=thread_id,
                compacted=False,
                reason=reason,
            ),
        )

    return PreparedThreadCompaction(
        thread_id=thread_id,
        source_checkpoint_id=source_checkpoint_id,
        result=ThreadCompactionResult(
            thread_id=thread_id,
            compacted=True,
            removed_message_count=len(result.messages_to_summarize),
            preserved_message_count=len(result.preserved_messages),
            summary_updated=True,
            total_tokens=result.total_tokens,
        ),
        write_config=dict(snapshot.config or read_config),
        update_values={
            "messages": Overwrite(list(result.preserved_messages)),
            "summary_text": result.summary_text,
            "memory_archive_receipt": result.memory_archive_receipt,
        },
    )


async def commit_thread_compaction(
    accessor: CheckpointStateAccessor,
    prepared: PreparedThreadCompaction,
) -> ThreadCompactionResult:
    """Persist a prepared replacement after the caller validates its source."""
    if not prepared.result.compacted:
        return prepared.result
    if prepared.write_config is None or prepared.update_values is None:
        raise ContextCompactionFailed("Prepared compaction is incomplete.")
    new_config = await accessor.aupdate(
        prepared.write_config,
        prepared.update_values,
        as_node="manual_compaction",
    )
    new_checkpoint_id = None
    if isinstance(new_config, dict):
        new_checkpoint_id = new_config.get("configurable", {}).get("checkpoint_id")
    return replace(prepared.result, checkpoint_id=new_checkpoint_id)
