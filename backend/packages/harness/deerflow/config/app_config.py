import hashlib
import logging
import os
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationInfo,
    model_validator,
)

from deerflow.config.acp_config import ACPAgentConfig, load_acp_config_from_dict
from deerflow.config.auth_config import AuthAppConfig
from deerflow.config.channel_connections_config import ChannelConnectionsConfig
from deerflow.config.database_config import DatabaseConfig
from deerflow.config.guardrails_config import GuardrailsConfig, load_guardrails_config_from_dict
from deerflow.config.input_polish_config import InputPolishConfig
from deerflow.config.loop_detection_config import LoopDetectionConfig
from deerflow.config.mcp_security_config import McpSecurityConfig
from deerflow.config.memory_config import MemoryConfig, load_memory_config_from_dict
from deerflow.config.model_config import ModelConfig
from deerflow.config.quota_config import QuotaConfig
from deerflow.config.read_before_write_config import ReadBeforeWriteConfig
from deerflow.config.reload_boundary import format_field_description
from deerflow.config.safety_finish_reason_config import SafetyFinishReasonConfig
from deerflow.config.sandbox_config import SandboxConfig
from deerflow.config.scheduler_config import SchedulerConfig
from deerflow.config.skills_config import SkillsConfig
from deerflow.config.subagents_config import SubagentsAppConfig, load_subagents_config_from_dict
from deerflow.config.suggestions_config import SuggestionsConfig
from deerflow.config.summarization_config import SummarizationConfig, load_summarization_config_from_dict
from deerflow.config.title_config import TitleConfig, load_title_config_from_dict
from deerflow.config.token_budget_config import TokenBudgetConfig
from deerflow.config.token_usage_config import TokenUsageConfig
from deerflow.config.tool_config import ToolConfig, ToolGroupConfig
from deerflow.config.tool_output_config import ToolOutputConfig
from deerflow.config.tool_progress_config import ToolProgressConfig
from deerflow.config.tool_search_config import ToolSearchConfig, load_tool_search_config_from_dict
from deerflow.config.worker_config import WorkerConfig

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[5]
LEGACY_CONFIG_TOMBSTONES = frozenset(
    {
        "agents_api",
        "authorization",
        "run_events",
        "stream_bridge",
        "extensions",
        "extensions_config",
        "mcp_config",
        "mcp_config_path",
        "legacy_run_store",
        "legacy_event_store",
        "recovery",
        "skill_evolution",
        "skill_scan",
    }
)

LEGACY_CONFIG_PATH_TOMBSTONES = frozenset(
    {
        "uploads.max_files",
        "uploads.max_file_size",
        "uploads.max_total_size",
        "uploads.auto_convert_documents",
        "scheduler.lease_seconds",
        "worker.default_max_attempts",
        "quotas.max_member_limit",
        "quotas.max_storage_bytes_limit",
        "quotas.max_concurrent_run_limit",
        "quotas.max_mcp_calls_daily_limit",
    }
)

YAML_CONFIG_TOMBSTONES = frozenset({"models"})

DATABASE_RUNTIME_POLICY_PATHS = frozenset(
    {
        "input_polish.enabled",
        "input_polish.max_chars",
        "input_polish.model_name",
        "loop_detection.enabled",
        "loop_detection.hard_limit",
        "loop_detection.max_tracked_threads",
        "loop_detection.tool_freq_hard_limit",
        "loop_detection.tool_freq_overrides",
        "loop_detection.tool_freq_warn",
        "loop_detection.warn_threshold",
        "loop_detection.window_size",
        "max_recursion_limit",
        "memory.debounce_seconds",
        "memory.enabled",
        "memory.candidate_retention_days",
        "memory.consolidation_interval_minutes",
        "memory.fact_confidence_threshold",
        "memory.guaranteed_categories",
        "memory.guaranteed_token_budget",
        "memory.injection_enabled",
        "memory.max_facts",
        "memory.max_injection_tokens",
        "memory.model_name",
        "memory.pipeline_mode",
        "memory.search_enabled",
        "memory.staleness_age_days",
        "memory.staleness_max_removals_per_cycle",
        "memory.staleness_min_candidates",
        "memory.staleness_protected_categories",
        "memory.staleness_review_enabled",
        "memory.token_counting",
        "read_before_write.enabled",
        "safety_finish_reason.enabled",
        "subagents.max_total_per_run",
        "suggestions.enabled",
        "summarization.enabled",
        "summarization.keep",
        "summarization.model_name",
        "summarization.skill_file_read_tool_names",
        "summarization.trigger",
        "summarization.trim_tokens_to_summarize",
        "title.enabled",
        "title.max_chars",
        "title.max_words",
        "title.model_name",
        "token_budget.enabled",
        "token_budget.hard_stop_threshold",
        "token_budget.max_input_tokens",
        "token_budget.max_output_tokens",
        "token_budget.max_tokens",
        "token_budget.warn_threshold",
        "token_usage.enabled",
        "tool_output.enabled",
        "tool_output.exempt_tools",
        "tool_output.externalize_min_chars",
        "tool_output.fallback_head_chars",
        "tool_output.fallback_max_chars",
        "tool_output.fallback_tail_chars",
        "tool_output.preview_head_chars",
        "tool_output.preview_tail_chars",
        "tool_output.tool_overrides",
        "tool_search.auto_promote_top_k",
        "tool_search.enabled",
    }
)

# These leaves are authoritative PostgreSQL system settings. They remain valid
# for programmatic Run-policy overlays, but accepting them from config.yaml
# would create two competing sources of truth.
DATABASE_RUNTIME_YAML_PATH_TOMBSTONES = DATABASE_RUNTIME_POLICY_PATHS | frozenset(
    {
        "auth.local.allow_registration",
        "quotas.default_concurrent_run_limit",
        "quotas.default_mcp_calls_daily_limit",
        "quotas.default_member_limit",
        "quotas.default_storage_bytes_limit",
        "quotas.warning_threshold",
    }
)
DATABASE_RUNTIME_YAML_TOP_LEVEL_TOMBSTONES = frozenset(path.split(".", 1)[0] for path in DATABASE_RUNTIME_YAML_PATH_TOMBSTONES)

DYNAMIC_MIDDLEWARE_CONFIG_TOMBSTONES = frozenset(
    {
        "agent_middlewares",
        "configured_middlewares",
        "trusted_middlewares",
    }
)


class CircuitBreakerConfig(BaseModel):
    """Configuration for the LLM Circuit Breaker."""

    failure_threshold: int = Field(default=5, description="Number of consecutive failures before tripping the circuit")
    recovery_timeout_sec: int = Field(default=60, description="Time in seconds before attempting to recover the circuit")


class LoggingEnhanceConfig(BaseModel):
    """Request trace logging enhancement settings."""

    enabled: bool = Field(default=False, description="Enable request-level trace ids in Gateway response headers and log records.")
    format: Literal["text", "json"] = Field(default="text", description="Enhanced log output format.")


class LoggingConfig(BaseModel):
    """Logging configuration."""

    enhance: LoggingEnhanceConfig = Field(default_factory=LoggingEnhanceConfig, description="Request trace correlation logging settings.")


def is_trace_correlation_enabled(config: Any) -> bool:
    """Return ``True`` when ``logging.enhance.enabled`` is set on *config*.

    Single source of truth for externally observable request correlation:
    Gateway response headers/log enrichment and Langfuse
    ``deerflow_trace_id`` metadata. Trusted Worker ``origin_trace_id``
    authority remains bound internally even when this returns ``False``.
    Accepts any object exposing ``logging.enhance.enabled`` via ``getattr``
    chains (``AppConfig``, ``SimpleNamespace`` fixtures, etc.); missing
    intermediate attributes silently degrade to ``False``.
    """
    logging_config = getattr(config, "logging", None)
    enhance = getattr(logging_config, "enhance", None)
    return bool(getattr(enhance, "enabled", False))


def logging_level_from_config(name: str | None) -> int:
    """Map ``config.yaml`` ``log_level`` string to a :mod:`logging` level constant."""
    mapping = logging.getLevelNamesMapping()
    return mapping.get((name or "info").strip().upper(), logging.INFO)


def apply_logging_level(name: str | None) -> None:
    """Resolve *name* to a logging level and apply it to the ``deerflow``/``app`` logger hierarchies.

    Only the ``deerflow`` and ``app`` logger levels are changed so that
    third-party library verbosity (e.g. uvicorn, sqlalchemy) is not
    affected. Root handler levels are lowered (never raised) so that
    messages from the configured loggers can propagate through without
    being filtered, while preserving handler thresholds that may be
    intentionally restrictive for third-party log output.
    """
    level = logging_level_from_config(name)
    for logger_name in ("deerflow", "app"):
        logging.getLogger(logger_name).setLevel(level)
    for handler in logging.root.handlers:
        if level < handler.level:
            handler.setLevel(level)


class AppConfig(BaseModel):
    """Config for the ActWeave application."""

    log_level: str = Field(
        default="info",
        description=format_field_description(
            "log_level",
            field_doc="Logging level for deerflow and app modules (debug/info/warning/error); third-party libraries are not affected.",
        ),
    )
    logging: LoggingConfig = Field(
        default_factory=LoggingConfig,
        description=format_field_description(
            "logging",
            field_doc="Structured logging and request trace correlation settings.",
        ),
    )
    token_usage: TokenUsageConfig = Field(default_factory=TokenUsageConfig, description="Token usage tracking configuration")
    token_budget: TokenBudgetConfig = Field(default_factory=TokenBudgetConfig, description="Token Budget tracking and limits configuration.")
    max_recursion_limit: int = Field(
        default=1000,
        ge=1,
        description="Hard server-side ceiling for a client-supplied run recursion_limit. Client values above this are clamped; prevents runaway LangGraph super-steps (LLM cost / DoS).",
    )
    models: list[ModelConfig] = Field(default_factory=list, description="Available models")
    sandbox: SandboxConfig = Field(
        description=format_field_description(
            "sandbox",
            field_doc="Sandbox provider configuration (local filesystem or Docker-based aio sandbox).",
        ),
    )
    tools: list[ToolConfig] = Field(default_factory=list, description="Available tools")
    tool_groups: list[ToolGroupConfig] = Field(default_factory=list, description="Available tool groups")
    skills: SkillsConfig = Field(default_factory=SkillsConfig, description="Skills configuration")
    tool_output: ToolOutputConfig = Field(default_factory=ToolOutputConfig, description="Tool output budget protection configuration")
    tool_search: ToolSearchConfig = Field(default_factory=ToolSearchConfig, description="Tool search / deferred loading configuration")
    title: TitleConfig = Field(default_factory=TitleConfig, description="Automatic title generation configuration")
    summarization: SummarizationConfig = Field(default_factory=SummarizationConfig, description="Conversation summarization configuration")
    memory: MemoryConfig = Field(default_factory=MemoryConfig, description="Memory subsystem configuration")
    mcp_security: McpSecurityConfig = Field(
        default_factory=McpSecurityConfig,
        description=format_field_description(
            "mcp_security",
            field_doc="Operator-owned project MCP network, egress, discovery, and tool-call policy.",
        ),
    )
    acp_agents: dict[str, ACPAgentConfig] = Field(default_factory=dict, description="ACP-compatible agent configuration")
    subagents: SubagentsAppConfig = Field(default_factory=SubagentsAppConfig, description="Subagent runtime configuration")
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig, description="Guardrail middleware configuration")
    input_polish: InputPolishConfig = Field(default_factory=InputPolishConfig, description="Pre-send input polishing configuration.")
    suggestions: SuggestionsConfig = Field(default_factory=SuggestionsConfig, description="Follow-up suggestions configuration.")
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig, description="LLM circuit breaker configuration")
    channel_connections: ChannelConnectionsConfig = Field(
        default_factory=ChannelConnectionsConfig,
        description=format_field_description(
            "channel_connections",
            field_doc=("Legacy deployment-config IM channel connection provider availability; does not gate database-backed project channel instances."),
        ),
    )
    loop_detection: LoopDetectionConfig = Field(default_factory=LoopDetectionConfig, description="Loop detection middleware configuration")
    tool_progress: ToolProgressConfig = Field(default_factory=ToolProgressConfig, description="Tool progress state machine middleware configuration")
    read_before_write: ReadBeforeWriteConfig = Field(default_factory=ReadBeforeWriteConfig, description="Read-before-write file gate middleware configuration")
    safety_finish_reason: SafetyFinishReasonConfig = Field(default_factory=SafetyFinishReasonConfig, description="Provider safety-filter finish_reason interception middleware configuration")
    auth: AuthAppConfig = Field(default_factory=AuthAppConfig, description="Authentication configuration (local + OIDC SSO)")
    model_config = ConfigDict(extra="allow")
    database: DatabaseConfig = Field(
        default_factory=DatabaseConfig,
        description=format_field_description(
            "database",
            field_doc="PostgreSQL connection shared by LangGraph persistence and ActWeave application data.",
        ),
    )
    scheduler: SchedulerConfig = Field(
        default_factory=SchedulerConfig,
        description=format_field_description(
            "scheduler",
            field_doc="Scheduled task runtime configuration (background poller for one-time and cron agent runs).",
        ),
    )
    worker: WorkerConfig = Field(
        default_factory=WorkerConfig,
        description=format_field_description(
            "worker",
            field_doc="Independent Worker process polling, leasing, concurrency, and retry configuration.",
        ),
    )
    quotas: QuotaConfig = Field(
        default_factory=QuotaConfig,
        description=("Compatibility shape for database-backed platform quota defaults and warning policy. Authoritative checks materialize the latest committed system quota policy inside their transaction."),
    )
    # Name -> config lookup tables, (re)built after validation by
    # ``_build_name_indexes``. They make ``get_model_config`` / ``get_tool_config``
    # / ``get_tool_group_config`` O(1) instead of an O(n) ``next(...)`` scan per
    # call. Private attrs are excluded from serialization.
    _models_by_name: dict[str, ModelConfig] = PrivateAttr(default_factory=dict)
    _tools_by_name: dict[str, ToolConfig] = PrivateAttr(default_factory=dict)
    _tool_groups_by_name: dict[str, ToolGroupConfig] = PrivateAttr(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def reject_removed_legacy_config(
        cls,
        value: object,
        info: ValidationInfo,
    ) -> object:
        if isinstance(value, Mapping):
            unsupported_middlewares = set(DYNAMIC_MIDDLEWARE_CONFIG_TOMBSTONES.intersection(value))
            if unsupported_middlewares:
                raise ValueError("DYNAMIC_MIDDLEWARE_CONFIG_UNSUPPORTED: " + ",".join(sorted(unsupported_middlewares)))
            removed = set(LEGACY_CONFIG_TOMBSTONES.intersection(value))
            if info.context and info.context.get("config_source") == "yaml":
                removed.update(YAML_CONFIG_TOMBSTONES.intersection(value))
            for field_path in LEGACY_CONFIG_PATH_TOMBSTONES:
                section, key = field_path.split(".", 1)
                section_value = value.get(section)
                if isinstance(section_value, Mapping) and key in section_value:
                    removed.add(field_path)
            if info.context and info.context.get("config_source") == "yaml":
                for field_path in DATABASE_RUNTIME_YAML_PATH_TOMBSTONES:
                    current: object = value
                    parts = field_path.split(".")
                    for part in parts[:-1]:
                        if not isinstance(current, Mapping) or part not in current:
                            break
                        current = current[part]
                    else:
                        if isinstance(current, Mapping) and parts[-1] in current:
                            removed.add(field_path)
            if removed:
                raise ValueError(f"LEGACY_CONFIG_REMOVED: {','.join(sorted(removed))}")
        return value

    @model_validator(mode="before")
    @classmethod
    def _drop_null_config_sections(
        cls,
        data: Any,
        info: ValidationInfo,
    ) -> Any:
        """Treat a present-but-null config section as absent so its default applies.

        Commenting out every entry under a top-level YAML key — e.g. ``tools:``
        (a list) or ``memory:`` (an object), with only comments beneath it as
        shipped throughout ``config.example.yaml`` — makes PyYAML parse the value
        as ``None``. Without this, the documented ``cp config.example.yaml
        config.yaml`` first-run flow crashes with an opaque ``Input should be a
        valid list`` / ``valid dictionary`` pydantic error for that section.

        Dropping the ``None`` lets each field fall back to its default: list
        sections become ``[]`` via ``default_factory=list`` and object sections
        get their default config. This generalizes the earlier list-only
        Required sections without a default (``sandbox``) intentionally still
        error when null — there is nothing to fall back to.
        """
        if isinstance(data, Mapping):
            copied_data = dict(data)
            if "checkpointer" in copied_data:
                raise ValueError("the independent checkpointer configuration has been removed; configure database.url instead")
            yaml_source = bool(info.context and info.context.get("config_source") == "yaml")
            return {key: value for key, value in copied_data.items() if value is not None or key in LEGACY_CONFIG_TOMBSTONES or (yaml_source and key in (YAML_CONFIG_TOMBSTONES | DATABASE_RUNTIME_YAML_TOP_LEVEL_TOMBSTONES))}
        return data

    @classmethod
    def resolve_config_path(cls, config_path: str | None = None) -> Path:
        """Resolve the config file path.

        Priority:
        1. If provided `config_path` argument, use it.
        2. If provided `DEER_FLOW_CONFIG_PATH` environment variable, use it.
        3. Otherwise, use ``config.yaml`` at the ActWeave repository root.
        """
        if config_path:
            path = Path(config_path)
            if not Path.exists(path):
                raise FileNotFoundError(f"Config file specified by param `config_path` not found at {path}")
            return path
        elif os.getenv("DEER_FLOW_CONFIG_PATH"):
            path = Path(os.getenv("DEER_FLOW_CONFIG_PATH"))
            if not Path.exists(path):
                raise FileNotFoundError(f"Config file specified by environment variable `DEER_FLOW_CONFIG_PATH` not found at {path}")
            return path
        path = REPO_ROOT / "config.yaml"
        if path.exists():
            return path
        raise FileNotFoundError(f"`config.yaml` file not found at the repository root: {path}")

    @classmethod
    def from_file(cls, config_path: str | None = None) -> Self:
        """Load config from YAML file.

        See `resolve_config_path` for more details.

        Args:
            config_path: Path to the config file.

        Returns:
            AppConfig: The loaded config.
        """
        resolved_path = cls.resolve_config_path(config_path)
        with open(resolved_path, encoding="utf-8") as f:
            config_data = yaml.safe_load(f) or {}

        # Check config version before processing
        cls._check_config_version(config_data, resolved_path)

        config_data = cls.resolve_env_variables(config_data)
        # Load circuit_breaker config if present
        if "circuit_breaker" in config_data:
            config_data["circuit_breaker"] = config_data["circuit_breaker"]

        result = cls.model_validate(
            config_data,
            context={"config_source": "yaml"},
        )
        acp_agents = cls._validate_acp_agents(config_data.get("acp_agents", {}))
        cls._apply_singleton_configs(result, acp_agents)
        return result

    @classmethod
    def _validate_acp_agents(
        cls,
        config_data: Mapping[str, Mapping[str, object]] | None,
    ) -> dict[str, ACPAgentConfig]:
        if config_data is None:
            config_data = {}
        return {name: ACPAgentConfig(**cfg) for name, cfg in config_data.items()}

    @classmethod
    def _apply_singleton_configs(cls, config: Self, acp_agents: dict[str, ACPAgentConfig]) -> None:
        load_title_config_from_dict(config.title.model_dump())
        load_summarization_config_from_dict(config.summarization.model_dump())
        load_memory_config_from_dict(config.memory.model_dump())
        load_subagents_config_from_dict(config.subagents.model_dump())
        load_tool_search_config_from_dict(config.tool_search.model_dump())
        load_guardrails_config_from_dict(config.guardrails.model_dump())
        load_acp_config_from_dict({name: agent.model_dump() for name, agent in acp_agents.items()})

    @classmethod
    def _check_config_version(cls, config_data: dict, config_path: Path) -> None:
        """Check if the user's config.yaml is outdated compared to config.example.yaml.

        Emits a warning if the user's config_version is lower than the example's.
        Missing config_version is treated as version 0 (pre-versioning).
        """
        try:
            user_version = int(config_data.get("config_version", 0))
        except (TypeError, ValueError):
            user_version = 0

        # Find config.example.yaml by searching config.yaml's directory and its parents
        example_path = None
        search_dir = config_path.parent
        for _ in range(5):  # search up to 5 levels
            candidate = search_dir / "config.example.yaml"
            if candidate.exists():
                example_path = candidate
                break
            parent = search_dir.parent
            if parent == search_dir:
                break
            search_dir = parent
        if example_path is None:
            return

        try:
            with open(example_path, encoding="utf-8") as f:
                example_data = yaml.safe_load(f)
            raw = example_data.get("config_version", 0) if example_data else 0
            try:
                example_version = int(raw)
            except (TypeError, ValueError):
                example_version = 0
        except Exception:
            return

        if user_version < example_version:
            logger.warning(
                "Your config.yaml (version %d) is outdated — the latest version is %d. Run `make config-upgrade` to merge new fields into your config.",
                user_version,
                example_version,
            )

    @classmethod
    def resolve_env_variables(cls, config: Any) -> Any:
        """Recursively resolve environment variables in the config.

        Environment variables are resolved using the `os.getenv` function. Example: $DATABASE_URL

        Args:
            config: The config to resolve environment variables in.

        Returns:
            The config with environment variables resolved.
        """
        if isinstance(config, str):
            if config.startswith("$"):
                env_value = os.getenv(config[1:])
                if env_value is None:
                    raise ValueError(f"Environment variable {config[1:]} not found for config value {config}")
                return env_value
            return config
        elif isinstance(config, dict):
            return {k: cls.resolve_env_variables(v) for k, v in config.items()}
        elif isinstance(config, list):
            return [cls.resolve_env_variables(item) for item in config]
        return config

    @model_validator(mode="after")
    def _build_name_indexes(self) -> "AppConfig":
        """Build name -> config lookup tables for O(1) ``get_*_config``.

        ``get_tool_config`` runs 2-3x per community-tool invocation (e.g.
        web_search) and ``get_model_config`` several times per agent build, so
        the previous O(n) ``next(...)`` scans sat on hot paths. Rebuilt here so a
        config reload (which constructs a fresh ``AppConfig``) refreshes them.
        ``setdefault`` keeps the first entry on duplicate names, preserving the
        prior ``next(...)`` first-match semantics.
        """
        models_by_name: dict[str, ModelConfig] = {}
        for model in self.models:
            models_by_name.setdefault(model.name, model)
        tools_by_name: dict[str, ToolConfig] = {}
        for tool in self.tools:
            tools_by_name.setdefault(tool.name, tool)
        tool_groups_by_name: dict[str, ToolGroupConfig] = {}
        for group in self.tool_groups:
            tool_groups_by_name.setdefault(group.name, group)
        self._models_by_name = models_by_name
        self._tools_by_name = tools_by_name
        self._tool_groups_by_name = tool_groups_by_name
        return self

    def get_model_config(self, name: str) -> ModelConfig | None:
        """Get the model config by name.

        Args:
            name: The name of the model to get the config for.

        Returns:
            The model config if found, otherwise None.
        """
        return self._models_by_name.get(name)

    def with_runtime_models(
        self,
        models: Sequence[ModelConfig],
    ) -> "AppConfig":
        """Return an isolated config carrying database-materialized models.

        Model definitions are no longer accepted from ``config.yaml``. The
        application layer resolves an exact PostgreSQL catalog version,
        decrypts its bound Credential only at the execution boundary, and
        injects the resulting ``ModelConfig`` objects through this helper.
        The source infrastructure config remains secret-free and unchanged.
        """

        if any(not isinstance(model, ModelConfig) for model in models):
            raise TypeError("runtime models must be ModelConfig instances")
        runtime = self.model_copy(deep=True)
        runtime.models = [model.model_copy(deep=True) for model in models]
        runtime._build_name_indexes()
        return runtime

    def with_runtime_policy(self, policy: object) -> "AppConfig":
        """Return an isolated config with one admitted runtime-policy overlay.

        The database policy models intentionally expose only the runtime-owned
        leaves.  A recursive merge preserves deployment-owned siblings such as
        ``title.prompt_template`` and ``tool_output.storage_subdir`` while the
        final ``AppConfig`` validation keeps every nested bound authoritative.
        """

        if isinstance(policy, Mapping):
            raw_policy = dict(policy)
        else:
            model_dump = getattr(policy, "model_dump", None)
            if not callable(model_dump):
                raise TypeError("runtime policy must be a mapping or Pydantic model")
            raw_policy = model_dump(mode="python")
        if not isinstance(raw_policy, Mapping):
            raise TypeError("runtime policy must materialize to a mapping")

        supplied_paths: set[str] = set()
        for section, value in raw_policy.items():
            if isinstance(value, Mapping):
                supplied_paths.update(f"{section}.{leaf}" for leaf in value)
            else:
                supplied_paths.add(str(section))
        if supplied_paths - DATABASE_RUNTIME_POLICY_PATHS:
            raise ValueError(
                "runtime policy contains a deployment-owned runtime policy field",
            )

        def merge(
            base: dict[str, object],
            overlay: Mapping[str, object],
        ) -> dict[str, object]:
            merged = dict(base)
            for key, value in overlay.items():
                current = merged.get(key)
                if isinstance(current, Mapping) and isinstance(value, Mapping):
                    merged[key] = merge(dict(current), value)
                else:
                    merged[key] = value
            return merged

        data = merge(
            self.model_dump(mode="python"),
            raw_policy,
        )
        return type(self).model_validate(data)

    def get_tool_config(self, name: str) -> ToolConfig | None:
        """Get the tool config by name.

        Args:
            name: The name of the tool to get the config for.

        Returns:
            The tool config if found, otherwise None.
        """
        return self._tools_by_name.get(name)

    def get_tool_group_config(self, name: str) -> ToolGroupConfig | None:
        """Get the tool group config by name.

        Args:
            name: The name of the tool group to get the config for.

        Returns:
            The tool group config if found, otherwise None.
        """
        return self._tool_groups_by_name.get(name)


# Compatibility singleton layer for code paths that have not yet been
# migrated to explicit ``AppConfig`` threading. New composition roots should
# prefer constructing ``AppConfig`` once and passing it down directly.
_app_config: AppConfig | None = None
_app_config_path: Path | None = None
_app_config_mtime: float | None = None
_ConfigSignature = tuple[float | None, int | None, str | None]
_app_config_signature: _ConfigSignature | None = None
_app_config_is_custom = False
_current_app_config: ContextVar[AppConfig | None] = ContextVar("deerflow_current_app_config", default=None)
_current_app_config_stack: ContextVar[tuple[AppConfig | None, ...]] = ContextVar("deerflow_current_app_config_stack", default=())


def _get_config_mtime(config_path: Path) -> float | None:
    """Get the modification time of a config file if it exists."""
    try:
        return config_path.stat().st_mtime
    except OSError:
        return None


def _get_config_signature(config_path: Path) -> _ConfigSignature | None:
    """Get cache metadata for a config file, including a content digest."""
    try:
        stat_result = config_path.stat()
    except OSError:
        return None

    digest = hashlib.sha256()
    try:
        with config_path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return (stat_result.st_mtime, stat_result.st_size, None)

    return (stat_result.st_mtime, stat_result.st_size, digest.hexdigest())


def _load_and_cache_app_config(config_path: str | None = None) -> AppConfig:
    """Load config from disk and refresh cache metadata."""
    global _app_config, _app_config_path, _app_config_mtime, _app_config_signature, _app_config_is_custom

    resolved_path = AppConfig.resolve_config_path(config_path)
    _app_config = AppConfig.from_file(str(resolved_path))
    _app_config_path = resolved_path
    _app_config_mtime = _get_config_mtime(resolved_path)
    _app_config_signature = _get_config_signature(resolved_path)
    _app_config_is_custom = False
    return _app_config


def get_app_config() -> AppConfig:
    """Get the ActWeave config instance.

    Returns a cached singleton instance and automatically reloads it when the
    underlying config file path or content signature changes. Use
    `reload_app_config()` to force a reload, or `reset_app_config()` to clear
    the cache.
    """
    global _app_config, _app_config_path, _app_config_mtime, _app_config_signature

    runtime_override = _current_app_config.get()
    if runtime_override is not None:
        return runtime_override

    if _app_config is not None and _app_config_is_custom:
        return _app_config

    resolved_path = AppConfig.resolve_config_path()
    current_mtime = _get_config_mtime(resolved_path)
    current_signature = _get_config_signature(resolved_path)

    should_reload = _app_config is None or _app_config_path != resolved_path or _app_config_signature != current_signature
    if should_reload:
        if _app_config_path == resolved_path and _app_config_mtime is not None and current_mtime is not None and _app_config_mtime != current_mtime:
            logger.info(
                "Config file has been modified (mtime: %s -> %s), reloading AppConfig",
                _app_config_mtime,
                current_mtime,
            )
        elif _app_config_path == resolved_path and _app_config_signature != current_signature:
            logger.info("Config file content signature changed, reloading AppConfig")
        _load_and_cache_app_config(str(resolved_path))
    return _app_config


def reload_app_config(config_path: str | None = None) -> AppConfig:
    """Reload the config from file and update the cached instance.

    This is useful when the config file has been modified and you want
    to pick up the changes without restarting the application.

    Args:
        config_path: Optional path to config file. If not provided,
                     uses the default resolution strategy.

    Returns:
        The newly loaded AppConfig instance.
    """
    return _load_and_cache_app_config(config_path)


def reset_app_config() -> None:
    """Reset the cached config instance.

    This clears the singleton cache, causing the next call to
    `get_app_config()` to reload from file. Useful for testing
    or when switching between different configurations.
    """
    global _app_config, _app_config_path, _app_config_mtime, _app_config_signature, _app_config_is_custom
    _app_config = None
    _app_config_path = None
    _app_config_mtime = None
    _app_config_signature = None
    _app_config_is_custom = False


def set_app_config(config: AppConfig) -> None:
    """Set a custom config instance.

    This allows injecting a custom or mock config for testing purposes.

    Args:
        config: The AppConfig instance to use.
    """
    global _app_config, _app_config_path, _app_config_mtime, _app_config_signature, _app_config_is_custom
    _app_config = config
    _app_config_path = None
    _app_config_mtime = None
    _app_config_signature = None
    _app_config_is_custom = True


def peek_current_app_config() -> AppConfig | None:
    """Return the runtime-scoped AppConfig override, if one is active."""
    return _current_app_config.get()


def push_current_app_config(config: AppConfig) -> None:
    """Push a runtime-scoped AppConfig override for the current execution context."""
    stack = _current_app_config_stack.get()
    _current_app_config_stack.set(stack + (_current_app_config.get(),))
    _current_app_config.set(config)


def pop_current_app_config() -> None:
    """Pop the latest runtime-scoped AppConfig override for the current execution context."""
    stack = _current_app_config_stack.get()
    if not stack:
        _current_app_config.set(None)
        return
    previous = stack[-1]
    _current_app_config_stack.set(stack[:-1])
    _current_app_config.set(previous)
