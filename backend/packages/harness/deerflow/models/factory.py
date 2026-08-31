import logging
from collections.abc import Mapping

from langchain.chat_models import BaseChatModel
from langchain_openai.chat_models.base import BaseChatOpenAI

from deerflow.config import get_app_config
from deerflow.config.app_config import AppConfig
from deerflow.reflection import resolve_class
from deerflow.tracing import build_tracing_callbacks

logger = logging.getLogger(__name__)

_AGENT_MODEL_SETTING_FIELDS = frozenset(
    {
        "temperature",
        "max_tokens",
    }
)
_RUNTIME_MODEL_OVERRIDE_FIELDS = frozenset({"max_retries"})
_AGENT_MAX_TOKENS_PROVIDER_FIELDS = (
    "max_tokens",
    "max_output_tokens",
)
_DEEPSEEK_PROVIDER_ADAPTERS = frozenset({"deepseek"})
_DEEPSEEK_RUNTIME_REASONING_EFFORTS = {
    "low": "low",
    "medium": "high",
    "high": "max",
}


class AgentModelSettingsUnsupported(ValueError):
    """The exact provider cannot honor immutable Agent sampling settings."""


class RuntimeModelSettingsUnsupported(ValueError):
    """The exact provider cannot honor a closed runtime call profile."""


def _deep_merge_dicts(base: dict | None, override: dict) -> dict:
    """Recursively merge two dictionaries without mutating the inputs."""
    merged = dict(base or {})
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def _vllm_disable_chat_template_kwargs(chat_template_kwargs: dict) -> dict:
    """Build the disable payload for vLLM/Qwen chat template kwargs."""
    disable_kwargs: dict[str, bool] = {}
    if "thinking" in chat_template_kwargs:
        disable_kwargs["thinking"] = False
    if "enable_thinking" in chat_template_kwargs:
        disable_kwargs["enable_thinking"] = False
    return disable_kwargs


def _declares_api_base(model_class: type) -> bool:
    """Return whether a provider declares ``api_base`` as its own field."""

    return "api_base" in getattr(model_class, "model_fields", {})


def _normalize_openai_base_url(
    model_class: type,
    model_settings_from_config: dict,
) -> None:
    """Map the catalog ``base_url`` to the provider's real constructor field.

    ``BaseChatOpenAI`` subclasses accept the OpenAI endpoint override as
    ``base_url``. ChatDeepSeek also declares its own ``api_base`` field and
    resolves that field from ``DEEPSEEK_API_BASE``; passing only ``base_url``
    lets the host environment replace the catalog's recipient-bound origin.
    Translate in both directions so the catalog remains canonical while every
    constructor receives the field that actually controls its client.
    """
    if not issubclass(model_class, BaseChatOpenAI):
        return
    if _declares_api_base(model_class):
        if "base_url" in model_settings_from_config:
            model_settings_from_config["api_base"] = model_settings_from_config.pop("base_url")
        return
    if "api_base" not in model_settings_from_config:
        return
    if "base_url" in model_settings_from_config or "openai_api_base" in model_settings_from_config:
        # Canonical key already present; drop the alias to avoid a duplicate-intent kwarg.
        model_settings_from_config.pop("api_base", None)
        logger.warning("Model config sets both an endpoint key (base_url/openai_api_base) and 'api_base'; using the former and ignoring 'api_base'.")
        return
    model_settings_from_config["base_url"] = model_settings_from_config.pop("api_base")
    logger.debug("Normalized model config key 'api_base' -> 'base_url' for OpenAI-compatible client.")


def _warn_unknown_model_settings(
    model_class,
    model_name: str,
    model_settings_from_config: dict,
) -> None:
    """Warn about config keys the OpenAI client will silently divert into ``model_kwargs``.

    ``ModelConfig`` is ``extra="allow"``, so a typo'd key (e.g. ``maxx_tokens``) is not caught at
    config-load time. LangChain's OpenAI client does not reject an unknown constructor kwarg — it
    emits a ``UserWarning`` and transfers the key into ``model_kwargs``, which is then spread into
    every ``Completions.create()`` call and rejected by the OpenAI SDK at *request* time with an
    opaque ``unexpected keyword argument`` error that is very hard to trace back to a config typo.

    This turns that latent failure into an explicit, actionable log line at model-build time. It is
    scoped to the ``BaseChatOpenAI`` family, where the divert-and-crash behavior
    is implemented. Other providers route extra kwargs differently and would
    false-positive. The warning logs key names only, never their values.
    """
    if not issubclass(model_class, BaseChatOpenAI):
        return
    known = getattr(model_class, "model_fields", None)
    if not known:
        return
    valid_names = set(known.keys())
    for field in known.values():
        alias = getattr(field, "alias", None)
        if alias:
            valid_names.add(alias)
    # Standard kwargs the factory injects or the OpenAI client accepts beyond declared fields.
    valid_names |= {
        "model",
        "model_kwargs",
        "extra_body",
        "default_headers",
        "default_query",
        "stream_usage",
        "stream_chunk_timeout",
        "reasoning_effort",
    }
    unknown = sorted(k for k in model_settings_from_config if k not in valid_names)
    if unknown:
        logger.warning(
            "Model '%s' (%s): config key(s) %s are not recognized parameters of the model class and will be forwarded as-is; this may raise at request time. Check for typos (e.g. 'maxx_tokens' -> 'max_tokens').",
            model_name,
            getattr(model_class, "__name__", "?"),
            unknown,
        )


# Default chunk-gap budget for OpenAI-compatible streaming responses.
#
# langchain-openai raises ``StreamChunkTimeoutError`` after this many seconds
# without receiving a chunk. Its own default is 60s, which is too aggressive for
# reasoning models (DeepSeek-R1, Doubao-thinking, GPT-5) whose first chunk can
# legitimately take 90~150s. We default to 240s so the streaming layer rarely
# trips on long thinking pauses; the LLMErrorHandlingMiddleware still retries
# (budget=2) if a real stall happens. Platform administrators can override
# this per model in the PostgreSQL-backed System Settings catalog.
_DEFAULT_STREAM_CHUNK_TIMEOUT_SECONDS: float = 240.0


def _apply_stream_chunk_timeout_default(
    model_class: type,
    model_settings_from_config: dict,
) -> None:
    """Inject a generous ``stream_chunk_timeout`` for OpenAI-compatible clients.

    The ``stream_chunk_timeout`` field is declared by ``BaseChatOpenAI`` and
    inherited by every compatible provider. Other providers must not receive it.

    * ``BaseChatOpenAI`` subclass: an explicit database-backed model setting is preserved.
      An explicit ``null`` is dropped upstream by ``model_dump(exclude_none=True)``
      and therefore treated as "unset", so the default is injected.
    * Non-OpenAI path: drop the key so it is never forwarded to an incompatible
      constructor (which would raise ``TypeError: unexpected keyword argument``).
    """
    if not issubclass(model_class, BaseChatOpenAI):
        model_settings_from_config.pop("stream_chunk_timeout", None)
        return
    if "stream_chunk_timeout" in model_settings_from_config:
        return
    model_settings_from_config["stream_chunk_timeout"] = _DEFAULT_STREAM_CHUNK_TIMEOUT_SECONDS


def _provider_model_field_names(model_class: type[BaseChatModel]) -> set[str]:
    names: set[str] = set()
    for name, field in getattr(model_class, "model_fields", {}).items():
        names.add(name)
        alias = getattr(field, "alias", None)
        if isinstance(alias, str):
            names.add(alias)
    return names


def model_supports_temperature(
    name: str | None = None,
    *,
    app_config: AppConfig | None = None,
) -> bool:
    """Return whether the configured provider declares a temperature field."""

    config = app_config or get_app_config()
    if name is None:
        name = config.models[0].name
    model_config = config.get_model_config(name)
    if model_config is None:
        raise ValueError(f"Model {name} not found in config") from None
    model_class = resolve_class(model_config.use, BaseChatModel)
    return "temperature" in _provider_model_field_names(model_class)


def _validated_agent_model_overrides(
    model_class: type[BaseChatModel],
    model_name: str,
    overrides: Mapping[str, object] | None,
) -> dict[str, float | int]:
    """Validate and map the bounded Agent Definition sampling surface.

    A canonical Agent ``max_tokens`` setting maps only to a field explicitly
    declared by the selected provider class.  This prevents an immutable
    Definition from silently forwarding an unsupported key into a provider
    request body.
    """

    if overrides is None:
        return {}
    if not isinstance(overrides, Mapping):
        raise AgentModelSettingsUnsupported(
            "Agent model overrides must be a mapping",
        )
    unknown = set(overrides) - _AGENT_MODEL_SETTING_FIELDS
    if unknown:
        raise AgentModelSettingsUnsupported(
            f"Unsupported Agent model setting: {sorted(unknown)[0]}",
        )
    supported_fields = _provider_model_field_names(model_class)
    mapped: dict[str, float | int] = {}
    for key, value in overrides.items():
        if key == "temperature":
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not 0 <= value <= 2:
                raise AgentModelSettingsUnsupported(
                    "Agent model setting temperature must be between 0 and 2",
                )
            if "temperature" not in supported_fields:
                raise AgentModelSettingsUnsupported(
                    f"Model {model_name} does not support Agent model setting temperature",
                )
            mapped["temperature"] = value
            continue
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 200_000:
            raise AgentModelSettingsUnsupported(
                "Agent model setting max_tokens must be an integer between 1 and 200000",
            )
        provider_field = next(
            (candidate for candidate in _AGENT_MAX_TOKENS_PROVIDER_FIELDS if candidate in supported_fields),
            None,
        )
        if provider_field is None:
            raise AgentModelSettingsUnsupported(
                f"Model {model_name} does not support Agent model setting max_tokens",
            )
        mapped[provider_field] = value
    return mapped


def _validated_runtime_model_overrides(
    model_class: type[BaseChatModel],
    model_name: str,
    overrides: Mapping[str, object] | None,
) -> dict[str, int]:
    """Validate the small server-owned Provider override surface."""

    if overrides is None:
        return {}
    if not isinstance(overrides, Mapping):
        raise RuntimeModelSettingsUnsupported(
            "Runtime model overrides must be a mapping",
        )
    unknown = set(overrides) - _RUNTIME_MODEL_OVERRIDE_FIELDS
    if unknown:
        raise RuntimeModelSettingsUnsupported(
            f"Unsupported runtime model setting: {sorted(unknown)[0]}",
        )
    value = overrides.get("max_retries")
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 20:
        raise RuntimeModelSettingsUnsupported(
            "Runtime model setting max_retries must be an integer between 0 and 20",
        )
    if "max_retries" not in _provider_model_field_names(model_class):
        raise RuntimeModelSettingsUnsupported(
            f"Model {model_name} does not support runtime model setting max_retries",
        )
    return {"max_retries": value}


def create_chat_model(
    name: str | None = None,
    thinking_enabled: bool = False,
    *,
    app_config: AppConfig | None = None,
    attach_tracing: bool = True,
    model_overrides: Mapping[str, object] | None = None,
    runtime_overrides: Mapping[str, object] | None = None,
    **kwargs,
) -> BaseChatModel:
    """Create a chat model instance from the config.

    Args:
        name: The name of the model to create. If None, the first model in the config will be used.
        thinking_enabled: Enable the model's extended-thinking mode when supported.
        app_config: Explicit application config; falls back to the cached global if omitted.
        attach_tracing: When True (default), attach tracing callbacks (Langfuse,
            LangSmith) directly to the model instance. Standalone callers — anything
            that invokes the model outside a LangGraph run that already wires tracing
            at the invocation root (ad-hoc utilities, background jobs, etc.) — keep
            this default so the model-level callback still produces traces. Callers
            that already attach tracing at the graph root (``make_lead_agent``, the
            in-graph ``TitleMiddleware``) MUST pass ``attach_tracing=False``; otherwise
            the same LLM call emits duplicate spans (one rooted at the graph, one at
            the model) and ``session_id`` / ``user_id`` metadata never reach the trace
            because the model becomes a nested observation whose ``langfuse_*`` keys
            get stripped.
        model_overrides: Exact per-Agent sampling defaults. Only
            ``temperature`` and canonical ``max_tokens`` are accepted, and
            each is mapped only when the selected provider declares support.
        runtime_overrides: Closed server-owned Provider settings selected by
            ``ModelRuntime``. Only ``max_retries`` is accepted.

    Returns:
        A chat model instance.
    """
    config = app_config or get_app_config()
    if name is None:
        name = config.models[0].name
    model_config = config.get_model_config(name)
    if model_config is None:
        raise ValueError(f"Model {name} not found in config") from None
    model_class = resolve_class(model_config.use, BaseChatModel)
    model_settings_from_config = model_config.model_dump(
        exclude_none=True,
        exclude={
            "use",
            "name",
            "display_name",
            "description",
            "max_input_tokens",
            "supports_thinking",
            "supports_reasoning_effort",
            "when_thinking_enabled",
            "when_thinking_disabled",
            "thinking",
            "supports_vision",
            # Deny the removed legacy metadata even if an in-process caller
            # constructed ModelConfig without validation.
            "pricing",
        },
    )
    agent_model_overrides = _validated_agent_model_overrides(
        model_class,
        name,
        model_overrides,
    )
    runtime_model_overrides = _validated_runtime_model_overrides(
        model_class,
        name,
        runtime_overrides,
    )
    # Compute effective when_thinking_enabled by merging in the `thinking` shortcut field.
    # The `thinking` shortcut is equivalent to setting when_thinking_enabled["thinking"].
    has_thinking_settings = (model_config.when_thinking_enabled is not None) or (model_config.thinking is not None)
    effective_wte: dict = dict(model_config.when_thinking_enabled) if model_config.when_thinking_enabled else {}
    if model_config.thinking is not None:
        merged_thinking = {**(effective_wte.get("thinking") or {}), **model_config.thinking}
        effective_wte = {**effective_wte, "thinking": merged_thinking}
    if thinking_enabled and has_thinking_settings:
        if not model_config.supports_thinking:
            raise ValueError(f"Model {name} does not support thinking. A platform administrator must enable `supports_thinking` for its version in System Settings.") from None
        if effective_wte:
            model_settings_from_config.update(effective_wte)
    if not thinking_enabled:
        if model_config.when_thinking_disabled is not None:
            # User-provided disable settings take full precedence
            model_settings_from_config.update(model_config.when_thinking_disabled)
        elif has_thinking_settings and effective_wte.get("extra_body", {}).get("thinking", {}).get("type"):
            # OpenAI-compatible gateway: thinking is nested under extra_body
            model_settings_from_config["extra_body"] = _deep_merge_dicts(
                model_settings_from_config.get("extra_body"),
                {"thinking": {"type": "disabled"}},
            )
            model_settings_from_config["reasoning_effort"] = "minimal"
        elif has_thinking_settings and (disable_chat_template_kwargs := _vllm_disable_chat_template_kwargs(effective_wte.get("extra_body", {}).get("chat_template_kwargs") or {})):
            # vLLM uses chat template kwargs to switch thinking on/off.
            model_settings_from_config["extra_body"] = _deep_merge_dicts(
                model_settings_from_config.get("extra_body"),
                {"chat_template_kwargs": disable_chat_template_kwargs},
            )
        elif has_thinking_settings and effective_wte.get("thinking", {}).get("type"):
            # Native langchain_anthropic: thinking is a direct constructor parameter
            model_settings_from_config["thinking"] = {"type": "disabled"}
    has_runtime_reasoning_effort = "reasoning_effort" in kwargs
    is_deepseek_adapter = model_config.system_provider_adapter in _DEEPSEEK_PROVIDER_ADAPTERS
    if not model_config.supports_reasoning_effort:
        kwargs.pop("reasoning_effort", None)
        model_settings_from_config.pop("reasoning_effort", None)
    elif is_deepseek_adapter and not thinking_enabled:
        # DeepSeek's OpenAI-format API disables thinking through the dedicated
        # thinking payload. Do not leak a catalog default effort into that call,
        # even when this non-thinking caller has no explicit Run profile.
        kwargs.pop("reasoning_effort", None)
        model_settings_from_config.pop("reasoning_effort", None)
    elif has_runtime_reasoning_effort:
        # The frontend supplies a per-run effort. Let it override the model
        # default instead of passing the same constructor argument twice.
        model_settings_from_config.pop("reasoning_effort", None)
        if is_deepseek_adapter:
            runtime_reasoning_effort = kwargs.pop("reasoning_effort")
            if thinking_enabled:
                try:
                    kwargs["reasoning_effort"] = _DEEPSEEK_RUNTIME_REASONING_EFFORTS[runtime_reasoning_effort]
                except (KeyError, TypeError):
                    raise RuntimeModelSettingsUnsupported(
                        "DeepSeek does not support the requested runtime reasoning effort",
                    ) from None

    # Apply exact Agent/request sampling values only after every global model
    # profile and thinking-mode merge. This preserves the public precedence
    # contract: request > exact Agent Definition > global configuration.
    if agent_model_overrides:
        if "max_tokens" in agent_model_overrides or "max_output_tokens" in agent_model_overrides:
            model_settings_from_config.pop("max_tokens", None)
            model_settings_from_config.pop("max_output_tokens", None)
        model_settings_from_config.update(agent_model_overrides)

    # Runtime policy is applied last and replaces both catalog values and any
    # legacy direct constructor kwarg, avoiding duplicate keyword arguments.
    if runtime_model_overrides:
        for key in runtime_model_overrides:
            model_settings_from_config.pop(key, None)
            kwargs.pop(key, None)
        model_settings_from_config.update(runtime_model_overrides)

    # Normalize the api_base -> base_url alias FIRST, so the downstream OpenAI-compatible
    # heuristics (stream_usage / stream_chunk_timeout) see the canonical endpoint key.
    _normalize_openai_base_url(model_class, model_settings_from_config)
    _apply_stream_chunk_timeout_default(model_class, model_settings_from_config)

    # Ensure stream_usage is enabled so that token usage metadata is available
    # in streaming responses.  LangChain's BaseChatOpenAI only defaults
    # stream_usage=True when no custom base_url/api_base is set, so models
    # hitting third-party endpoints (e.g. doubao, deepseek) silently lose
    # usage data.  We default it to True unless explicitly configured.
    if "stream_usage" not in model_settings_from_config and "stream_usage" not in kwargs:
        if "stream_usage" in getattr(model_class, "model_fields", {}):
            model_settings_from_config["stream_usage"] = True

    # ``openai_responses`` authoring exposes the reasoning summary as its own
    # enum field, but the Responses API accepts it only inside the single
    # ``reasoning`` object. Fold the summary and the effective effort together
    # and drop the flat keys: once ``reasoning`` is present the SDK no longer
    # rewrites ``reasoning_effort``, and the endpoint rejects the flat spelling.
    reasoning_summary = model_settings_from_config.pop("reasoning_summary", None)
    if reasoning_summary is not None and model_settings_from_config.get("use_responses_api") is True:
        catalog_reasoning_effort = model_settings_from_config.pop("reasoning_effort", None)
        effective_reasoning_effort = kwargs.pop("reasoning_effort", catalog_reasoning_effort)
        reasoning_payload: dict = {"summary": reasoning_summary}
        if effective_reasoning_effort is not None:
            reasoning_payload["effort"] = effective_reasoning_effort
        model_settings_from_config["reasoning"] = reasoning_payload

    _warn_unknown_model_settings(model_class, name, model_settings_from_config)

    model_instance = model_class(**kwargs, **model_settings_from_config)
    existing_profile = getattr(model_instance, "profile", None)
    model_instance.profile = {
        **(dict(existing_profile) if isinstance(existing_profile, Mapping) else {}),
        "max_input_tokens": model_config.max_input_tokens,
    }

    if attach_tracing:
        callbacks = build_tracing_callbacks()
        if callbacks:
            existing_callbacks = model_instance.callbacks or []
            model_instance.callbacks = [*existing_callbacks, *callbacks]
            logger.debug(f"Tracing attached to model '{name}' with providers={len(callbacks)}")
    return model_instance
