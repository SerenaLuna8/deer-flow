"""Lead agent factory.

INVARIANT — tracing callback placement
======================================

Tracing callbacks (Langfuse, LangSmith) are attached at the **graph
invocation root** in :func:`_make_lead_agent` (see the
``build_tracing_callbacks()`` block that appends to ``config["callbacks"]``).
Every model built inside this module — and inside any
middleware reachable from this graph (e.g. ``TitleMiddleware``) — MUST pass
through the ``AGENT_GRAPH`` ModelRuntime profile.

Forgetting that flag emits duplicate spans (one rooted at the graph, one at
the model) AND prevents the Langfuse handler's ``propagate_attributes``
path from firing, so ``session_id`` / ``user_id`` never reach the trace.
The current sites are the lead agent, recovery model, summarization middleware,
and the async path inside ``TitleMiddleware``. Any new in-graph model must use
the same profile.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from html import escape
from pathlib import Path

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool

from deerflow.agents.lead_agent.prompt import apply_prompt_template
from deerflow.agents.middlewares.assembly import (
    append_final_provider_request_guard,
    assemble_agent_middlewares,
    build_host_execution_batch_barrier,
    build_lead_runtime_middlewares,
)
from deerflow.agents.middlewares.clarification_middleware import ClarificationMiddleware
from deerflow.agents.middlewares.provider_request_usage import (
    FinalProviderRequestGuard,
    build_provider_request_profile,
    collect_custom_middleware_request_contract,
    collect_middleware_system_prompts,
    collect_middleware_tools,
    provider_request_runtime_policy_identity,
)
from deerflow.agents.middlewares.safety_finish_reason_middleware import SafetyFinishReasonMiddleware
from deerflow.agents.middlewares.subagent_limit_middleware import SubagentLimitMiddleware
from deerflow.agents.middlewares.summarization_middleware import DeerFlowSummarizationMiddleware, create_summarization_middleware
from deerflow.agents.middlewares.title_middleware import TitleMiddleware
from deerflow.agents.middlewares.todo_middleware import TodoMiddleware
from deerflow.agents.middlewares.token_usage_middleware import TokenUsageMiddleware
from deerflow.agents.middlewares.tool_call_control import (
    FixedToolCallControlScope,
    GraphToolCallControlTopology,
    PerInvocationToolCallControlScope,
    ResolvedGraphToolCallControlProfile,
    ToolCallControlObserver,
    default_graph_tool_call_control_profile,
)
from deerflow.agents.middlewares.view_image_middleware import ViewImageMiddleware
from deerflow.agents.thread_state import (
    get_thread_state_schema,
    normalize_middleware_state_schemas,
)
from deerflow.assets.catalog import trusted_asset_context
from deerflow.config.agents_config import load_agent_config, validate_agent_name
from deerflow.config.app_config import AppConfig, get_app_config
from deerflow.models import ModelRuntime, ModelRuntimeProfile
from deerflow.runtime.checkpoint_mode import (
    INTERNAL_CHECKPOINT_MODE_KEY,
    freeze_checkpoint_channel_mode,
    freeze_checkpoint_snapshot_frequency,
    frozen_checkpoint_channel_mode,
    inject_checkpoint_mode,
)
from deerflow.sandbox.security import requires_host_bash_approval
from deerflow.skills.types import Skill
from deerflow.subagents.binding import (
    AgentGraphExecutionInputs,
    ConfiguredLeadParentExecutionProfile,
    ParentExecutionBindingFactory,
    PrivateRunParentExecutionProfile,
    bind_task_tool_in_tools,
)
from deerflow.subagents.runtime_catalog import trusted_runtime_agent_catalog
from deerflow.tracing import build_tracing_callbacks

logger = logging.getLogger(__name__)

_NON_INTERACTIVE_DISABLED_TOOL_NAMES = frozenset({"ask_clarification"})
_PRIVATE_RUNTIME_DEFAULT_MODEL_REF = "default"
_CANONICAL_BOUNDED_OVERLAY_UTF8_BYTES = 32 * 1024


@dataclass(frozen=True, slots=True)
class TrustedLeadAgentExtension:
    """Worker-owned additions to one canonical private lead Agent graph.

    This object is accepted only as an explicit Python argument to the graph
    factory.  It is deliberately never recovered from RunnableConfig, request
    metadata, checkpoints, or model output.
    """

    extra_tools: tuple[BaseTool, ...] = ()
    excluded_tool_names: frozenset[str] = frozenset()
    custom_middlewares: tuple[AgentMiddleware, ...] = ()
    output_limit_recovery_override: AgentMiddleware | None = None
    system_prompt_override: str | None = None

    def __post_init__(self) -> None:
        if type(self.extra_tools) is not tuple or any(not isinstance(tool, BaseTool) for tool in self.extra_tools):
            raise TypeError("Trusted lead Agent extra tools must be a tuple of BaseTool instances")
        tool_names = tuple(tool.name for tool in self.extra_tools)
        if any(not isinstance(name, str) or not name for name in tool_names) or len(tool_names) != len(set(tool_names)):
            raise ValueError("Trusted lead Agent extra tool names must be non-empty and unique")
        if type(self.excluded_tool_names) is not frozenset or any(not isinstance(name, str) or not name for name in self.excluded_tool_names):
            raise TypeError("Trusted lead Agent excluded tool names must be a frozenset of non-empty strings")
        if set(tool_names) & self.excluded_tool_names:
            raise ValueError("Trusted lead Agent tools cannot be both added and excluded")
        if type(self.custom_middlewares) is not tuple or any(not isinstance(middleware, AgentMiddleware) for middleware in self.custom_middlewares):
            raise TypeError("Trusted lead Agent custom middlewares must be a tuple of AgentMiddleware instances")
        if self.output_limit_recovery_override is not None and not isinstance(self.output_limit_recovery_override, AgentMiddleware):
            raise TypeError("Trusted lead Agent output-limit override must be AgentMiddleware")
        if self.system_prompt_override is not None and (type(self.system_prompt_override) is not str or not self.system_prompt_override.strip()):
            raise TypeError(
                "Trusted lead Agent system-prompt override must be a non-empty string",
            )


def _get_runtime_config(config: RunnableConfig) -> dict:
    """Merge legacy configurable options with LangGraph runtime context."""
    cfg = dict(config.get("configurable", {}) or {})
    context = config.get("context", {}) or {}
    if isinstance(context, dict):
        cfg.update(context)
    return cfg


def _resolve_runtime_option(
    cfg: dict,
    key: str,
    agent_value: object,
    default: object,
) -> object:
    """Resolve ``request > exact Agent Definition > global`` without losing false."""

    if key in cfg:
        return cfg[key]
    if agent_value is not None:
        return agent_value
    return default


def _trusted_runtime_asset_context(config: dict) -> object | None:
    """Select only an opaque app-supplied context; never trust client dicts."""

    return trusted_asset_context(config.get("project_context") or config.get("asset_context"))


def _resolve_model_name(requested_model_name: str | None = None, *, app_config: AppConfig | None = None) -> str:
    """Resolve a runtime model name safely, falling back to default if invalid. Returns None if no models are configured."""
    app_config = app_config or get_app_config()
    default_model_name = app_config.models[0].name if app_config.models else None
    if default_model_name is None:
        raise ValueError("No chat models are available. A platform administrator must configure an active model in System Settings.")

    if requested_model_name and app_config.get_model_config(requested_model_name):
        return requested_model_name

    if requested_model_name and requested_model_name != default_model_name:
        logger.warning(f"Model '{requested_model_name}' not found in config; fallback to default model '{default_model_name}'.")
    return default_model_name


def _resolve_private_runtime_model_name(
    *,
    model_ref: object,
    requested_model_name: str | None,
    app_config: AppConfig,
) -> str:
    """Resolve the exact model authorized for a private Agent runtime.

    An explicit admitted ``model_ref`` is authoritative and cannot be
    overridden by request metadata.  The reserved ``default`` alias is the
    sole exception: admission has already resolved it to the exact logical
    model persisted on the Run and injects that value as ``model_name``.
    Both paths still fail closed against the Worker process's AppConfig.
    """

    model_name = requested_model_name if model_ref == _PRIVATE_RUNTIME_DEFAULT_MODEL_REF else model_ref
    if not isinstance(model_name, str) or not model_name or app_config.get_model_config(model_name) is None:
        raise ValueError("Exact project Agent model is not configured")
    return model_name


def _create_summarization_middleware(
    *,
    app_config: AppConfig | None = None,
    context_model: BaseChatModel | None = None,
) -> DeerFlowSummarizationMiddleware | None:
    """Create and configure the summarization middleware from config."""
    return create_summarization_middleware(
        app_config=app_config,
        context_model=context_model,
    )


def _create_todo_list_middleware(is_plan_mode: bool) -> TodoMiddleware | None:
    """Create and configure the TodoList middleware.

    Args:
        is_plan_mode: Whether to enable plan mode with TodoList middleware.

    Returns:
        TodoMiddleware instance if plan mode is enabled, None otherwise.
    """
    if not is_plan_mode:
        return None

    # Custom prompts matching ActWeave's style
    system_prompt = """
<todo_list_system>
You have access to the `write_todos` tool to help you manage and track complex multi-step objectives.

**CRITICAL RULES:**
- Mark todos as completed IMMEDIATELY after finishing each step - do NOT batch completions
- Keep EXACTLY ONE task as `in_progress` at any time (unless tasks can run in parallel)
- Update the todo list in REAL-TIME as you work - this gives users visibility into your progress
- DO NOT use this tool for simple tasks (< 3 steps) - just complete them directly

**When to Use:**
This tool is designed for complex objectives that require systematic tracking:
- Complex multi-step tasks requiring 3+ distinct steps
- Non-trivial tasks needing careful planning and execution
- User explicitly requests a todo list
- User provides multiple tasks (numbered or comma-separated list)
- The plan may need revisions based on intermediate results

**When NOT to Use:**
- Single, straightforward tasks
- Trivial tasks (< 3 steps)
- Purely conversational or informational requests
- Simple tool calls where the approach is obvious

**Best Practices:**
- Break down complex tasks into smaller, actionable steps
- Use clear, descriptive task names
- Remove tasks that become irrelevant
- Add new tasks discovered during implementation
- Don't be afraid to revise the todo list as you learn more

**Task Management:**
Writing todos takes time and tokens - use it when helpful for managing complex problems, not for simple requests.
</todo_list_system>
"""

    tool_description = """Use this tool to create and manage a structured task list for complex work sessions.

**IMPORTANT: Only use this tool for complex tasks (3+ steps). For simple requests, just do the work directly.**

## When to Use

Use this tool in these scenarios:
1. **Complex multi-step tasks**: When a task requires 3 or more distinct steps or actions
2. **Non-trivial tasks**: Tasks requiring careful planning or multiple operations
3. **User explicitly requests todo list**: When the user directly asks you to track tasks
4. **Multiple tasks**: When users provide a list of things to be done
5. **Dynamic planning**: When the plan may need updates based on intermediate results

## When NOT to Use

Skip this tool when:
1. The task is straightforward and takes less than 3 steps
2. The task is trivial and tracking provides no benefit
3. The task is purely conversational or informational
4. It's clear what needs to be done and you can just do it

## How to Use

1. **Starting a task**: Mark it as `in_progress` BEFORE beginning work
2. **Completing a task**: Mark it as `completed` IMMEDIATELY after finishing
3. **Updating the list**: Add new tasks, remove irrelevant ones, or update descriptions as needed
4. **Multiple updates**: You can make several updates at once (e.g., complete one task and start the next)

## Task States

- `pending`: Task not yet started
- `in_progress`: Currently working on (can have multiple if tasks run in parallel)
- `completed`: Task finished successfully

## Task Completion Requirements

**CRITICAL: Only mark a task as completed when you have FULLY accomplished it.**

Never mark a task as completed if:
- There are unresolved issues or errors
- Work is partial or incomplete
- You encountered blockers preventing completion
- You couldn't find necessary resources or dependencies
- Quality standards haven't been met

If blocked, keep the task as `in_progress` and create a new task describing what needs to be resolved.

## Best Practices

- Create specific, actionable items
- Break complex tasks into smaller, manageable steps
- Use clear, descriptive task names
- Update task status in real-time as you work
- Mark tasks complete IMMEDIATELY after finishing (don't batch completions)
- Remove tasks that are no longer relevant
- **IMPORTANT**: When you write the todo list, mark your first task(s) as `in_progress` immediately
- **IMPORTANT**: Unless all tasks are completed, always have at least one task `in_progress` to show progress

Being proactive with task management demonstrates thoroughness and ensures all requirements are completed successfully.

**Remember**: If you only need a few tool calls to complete a task and it's clear what to do, it's better to just do the task directly and NOT use this tool at all.
"""

    return TodoMiddleware(system_prompt=system_prompt, tool_description=tool_description)


def _dynamic_context_config(
    app_config: AppConfig,
    *,
    private_runtime: bool,
) -> AppConfig:
    """Return the frozen Run config; Memory data comes only from its snapshot."""

    del private_runtime
    return app_config


# ThreadDataMiddleware must be before SandboxMiddleware to ensure thread_id is available
# UploadsMiddleware should be after ThreadDataMiddleware to access thread_id
# DanglingToolCallMiddleware patches missing ToolMessages before model sees the history
# SummarizationMiddleware should be early to reduce context before other processing
# TodoListMiddleware should be before ClarificationMiddleware to allow todo management
# TitleMiddleware generates title after first exchange
# ViewImageMiddleware should be before ClarificationMiddleware to inject image details before LLM
# ToolErrorHandlingMiddleware should be before ClarificationMiddleware to convert tool exceptions to ToolMessages
# ClarificationMiddleware should be last to intercept clarification requests after model calls
def build_middlewares(
    config: RunnableConfig,
    model_name: str | None,
    agent_name: str | None = None,
    custom_middlewares: list[AgentMiddleware] | None = None,
    *,
    context_model: BaseChatModel | None = None,
    available_skills: set[str] | None = None,
    app_config: AppConfig | None = None,
    deferred_setup=None,
    mcp_routing_middleware: AgentMiddleware | None = None,
    user_id: str | None = None,
    runtime_skills: tuple[Skill, ...] | None = None,
    runtime_skill_version_ids: tuple[str, ...] | None = None,
    runtime_skills_root: Path | None = None,
    runtime_skills_container_path: str | None = None,
    resolved_subagent_enabled: bool | None = None,
    resolved_max_concurrent_subagents: int | None = None,
    resolved_max_total_subagents: int | None = None,
    tool_call_control: AgentMiddleware | None = None,
    output_limit_recovery_model: BaseChatModel | None = None,
    output_limit_recovery_override: AgentMiddleware | None = None,
):
    """Build the lead-agent middleware chain based on runtime configuration.

    Public entry point for the lead agent's full middleware composition. Used by
    ``make_lead_agent`` and by the embedded ``DeerFlowClient`` (a lead-agent variant
    that needs the identical chain). Keep this name stable: it is imported across a
    module boundary, so renames/signature changes ripple into ``client.py``.

    Args:
        config: Runtime configuration containing configurable options like is_plan_mode.
        model_name: Resolved runtime model name; gates vision-only middleware.
        agent_name: Optional agent namespace used by context middlewares.
        custom_middlewares: Optional list of custom middlewares to inject into the chain.
        app_config: Explicit AppConfig; falls back to ``get_app_config()`` when omitted.
        deferred_setup: Optional deferred-MCP-tool setup that attaches
            ``DeferredToolFilterMiddleware`` when ``tool_search`` is enabled.
        mcp_routing_middleware: Optional PR2 middleware that auto-promotes
            deferred MCP schemas before the deferred filter runs.
        user_id: Effective user ID for user-scoped skill loading. Passed through
            to ``SkillActivationMiddleware`` so it can resolve per-user custom skills.
        resolved_subagent_enabled: Server-resolved task-tool availability.
            Private Runs pass this explicitly because their sanitized request
            config intentionally contains no client-controlled authority flag.
        resolved_max_concurrent_subagents: Server-resolved per-batch limit.
        resolved_max_total_subagents: Server-resolved per-execution delegation
            total. It remains owned by ``SubagentLimitMiddleware``.
        tool_call_control: The already-bound repeated-call and scoped
            tool-call-limit Adapter for this graph execution profile.

    Returns:
        List of middleware instances.
    """
    resolved_app_config = app_config or get_app_config()
    runtime_middlewares = build_lead_runtime_middlewares(
        app_config=resolved_app_config,
        lazy_init=True,
    )
    before_summarization: list[AgentMiddleware] = []

    # Always inject current date as a <system-reminder>. Private long-term
    # Memory is available only through the Worker-issued Run snapshot authority.
    from deerflow.agents.middlewares.dynamic_context_middleware import DynamicContextMiddleware

    before_summarization.append(
        DynamicContextMiddleware(
            agent_name=agent_name,
            app_config=_dynamic_context_config(
                resolved_app_config,
                private_runtime=runtime_skills is not None,
            ),
        )
    )

    # Deterministically load a full SKILL.md when the user starts the turn with
    # /skill-name. This keeps the base system prompt metadata-only while giving
    # explicit user activation priority over model-side relevance guessing.
    from deerflow.agents.middlewares.skill_activation_middleware import SkillActivationMiddleware

    slash_source_owner_token = secrets.token_urlsafe(24)
    before_summarization.append(
        SkillActivationMiddleware(
            available_skills=available_skills,
            app_config=resolved_app_config,
            user_id=user_id,
            runtime_skills=runtime_skills,
            runtime_skills_root=runtime_skills_root,
            runtime_skills_container_path=runtime_skills_container_path,
            slash_source_owner_token=slash_source_owner_token,
        )
    )

    if runtime_skills is not None:
        from deerflow.agents.middlewares.skill_tool_policy_middleware import (
            SkillToolPolicyMiddleware,
        )

        exact_version_ids = () if not runtime_skills and runtime_skill_version_ids is None else runtime_skill_version_ids
        if exact_version_ids is None:
            raise ValueError("runtime_skill_version_ids are required for exact runtime Skills")
        exact_container_path = runtime_skills_container_path or (str(runtime_skills_root) if runtime_skills_root is not None else resolved_app_config.skills.container_path)
        before_summarization.append(
            SkillToolPolicyMiddleware(
                runtime_skills=runtime_skills,
                runtime_skill_version_ids=exact_version_ids,
                runtime_skills_container_path=exact_container_path,
                available_skills=available_skills,
                slash_source_owner_token=slash_source_owner_token,
                skill_file_read_tool_names=(resolved_app_config.summarization.skill_file_read_tool_names),
                read_evidence_ttl_calls=resolved_app_config.skills.read_evidence_ttl_calls,
            )
        )

    # Capture completed task delegations and loaded skill files before
    # summarization can compact them, then inject durable context channels
    # (summary + ledger + skills) into model calls.
    from deerflow.agents.middlewares.durable_context_middleware import DurableContextMiddleware

    before_summarization.append(
        DurableContextMiddleware(
            skills_container_path=(runtime_skills_container_path or (str(runtime_skills_root) if runtime_skills_root is not None else resolved_app_config.skills.container_path)),
            skill_file_read_tool_names=resolved_app_config.summarization.skill_file_read_tool_names,
        )
    )

    # Resolve feature-owned phases; the shared composer below owns their order.
    summarization_middleware = _create_summarization_middleware(
        app_config=resolved_app_config,
        context_model=context_model,
    )

    cfg = _get_runtime_config(config)
    is_plan_mode = cfg.get("is_plan_mode", False)
    todo_list_middleware = _create_todo_list_middleware(is_plan_mode)

    token_usage_middleware = TokenUsageMiddleware() if resolved_app_config.token_usage.enabled else None
    title_middleware = TitleMiddleware(app_config=resolved_app_config)

    # Always install checkpoint cleanup. Text-only models disable ephemeral
    # image injection but still purge legacy base64 channels/messages.
    model_config = resolved_app_config.get_model_config(model_name) if model_name else None
    vision_middleware = ViewImageMiddleware(enable_injection=bool(model_config is not None and model_config.supports_vision))

    # Auto-promote deferred MCP schemas from PR1 routing metadata before the
    # deferred filter decides which schemas to hide for this model call.
    routing_middlewares: list[AgentMiddleware] = []
    if mcp_routing_middleware is not None:
        routing_middlewares.append(mcp_routing_middleware)

    # Hide deferred tool schemas from model binding until tool_search promotes them.
    # The deferred set + catalog hash come from the build-time setup (assembled
    # after tool-policy filtering); promotion is read from graph state.
    if deferred_setup is not None and deferred_setup.deferred_names:
        from deerflow.agents.middlewares.deferred_tool_filter_middleware import DeferredToolFilterMiddleware

        routing_middlewares.append(
            DeferredToolFilterMiddleware(
                deferred_setup.deferred_names,
                deferred_setup.catalog_hash,
            )
        )

    # Coalesce every SystemMessage into a single leading one before the request
    # reaches the provider. Strict backends (vLLM, SGLang, Qwen, Anthropic)
    # reject non-leading SystemMessages. See system_message_coalescing_middleware.py.
    from deerflow.agents.middlewares.system_message_coalescing_middleware import SystemMessageCoalescingMiddleware

    system_message_middleware = SystemMessageCoalescingMiddleware()

    # Add SubagentLimitMiddleware to truncate excess parallel task calls
    effective_subagent_enabled = bool(cfg.get("subagent_enabled", False)) if resolved_subagent_enabled is None else resolved_subagent_enabled
    subagent_middleware = None
    if effective_subagent_enabled:
        effective_max_concurrent = cfg.get("max_concurrent_subagents", 3) if resolved_max_concurrent_subagents is None else resolved_max_concurrent_subagents
        effective_max_total = resolved_app_config.subagents.max_total_per_run if resolved_max_total_subagents is None else resolved_max_total_subagents
        subagent_middleware = SubagentLimitMiddleware(
            max_concurrent=effective_max_concurrent,
            max_total=effective_max_total,
        )

    # TokenBudgetMiddleware - enforce per-run token limits
    token_budget_config = resolved_app_config.token_budget
    token_budget_middleware = None
    if token_budget_config.enabled:
        from deerflow.agents.middlewares.token_budget_middleware import TokenBudgetMiddleware

        token_budget_middleware = TokenBudgetMiddleware.from_config(token_budget_config)

    output_limit_recovery_middleware = output_limit_recovery_override
    if output_limit_recovery_middleware is None and output_limit_recovery_model is not None:
        from deerflow.agents.middlewares.output_limit_recovery_middleware import (
            OutputLimitRecoveryMiddleware,
        )

        output_limit_recovery_middleware = OutputLimitRecoveryMiddleware(
            recovery_model=output_limit_recovery_model,
            budget_hard_stopped=(token_budget_middleware.is_hard_stopped if token_budget_middleware is not None else None),
        )

    # SafetyFinishReasonMiddleware — suppress tool execution when the provider
    # safety-terminated the response. The shared assembly owns the protected
    # reverse-dispatch arbitration band; ToolCallControl therefore sees only
    # calls still eligible to reach ToolNode.
    safety_config = resolved_app_config.safety_finish_reason
    safety_middleware = SafetyFinishReasonMiddleware.from_config(safety_config) if safety_config.enabled else None

    middlewares = assemble_agent_middlewares(
        runtime=tuple(runtime_middlewares),
        before_summarization=tuple(before_summarization),
        summarization=summarization_middleware,
        planning=todo_list_middleware,
        output_limit_recovery=output_limit_recovery_middleware,
        token_usage=token_usage_middleware,
        title=title_middleware,
        vision=vision_middleware,
        routing=tuple(routing_middlewares),
        system_message=system_message_middleware,
        tool_call_control=tool_call_control,
        host_execution_batch_barrier=(build_host_execution_batch_barrier(app_config=resolved_app_config)),
        subagent=subagent_middleware,
        token_budget=token_budget_middleware,
        custom=tuple(custom_middlewares or ()),
        safety=safety_middleware,
        clarification=ClarificationMiddleware(),
    )

    if deferred_setup is not None and deferred_setup.deferred_names:
        from deerflow.agents.middlewares.mcp_routing_middleware import (
            assert_mcp_routing_before_deferred_filter,
        )

        assert_mcp_routing_before_deferred_filter(middlewares)

    return middlewares


def _available_skill_names(agent_config) -> set[str] | None:
    if agent_config and agent_config.skills is not None:
        return set(agent_config.skills)
    return None


def _load_enabled_available_skills(available_skills: set[str] | None, *, app_config: AppConfig, user_id: str | None = None) -> list[Skill]:
    try:
        from deerflow.agents.lead_agent.prompt import get_enabled_skills_for_config

        skills = get_enabled_skills_for_config(app_config, user_id=user_id)
    except Exception:
        logger.exception("Failed to load enabled Skills for discovery")
        raise

    if available_skills is None:
        return skills
    return [skill for skill in skills if skill.name in available_skills]


def _exact_runtime_skill_version_ids(
    private_runtime,
    runtime_skills: tuple[Skill, ...],
) -> tuple[str, ...]:
    """Align exact runtime Skill objects with their admitted version IDs."""

    if not runtime_skills:
        return ()
    safe_manifest = getattr(private_runtime, "safe_manifest", None)
    manifests = tuple(getattr(safe_manifest, "skills", ()))
    if len(manifests) != len(runtime_skills):
        raise RuntimeError("Private runtime Skill manifest does not match exact runtime Skills")

    version_ids: list[str] = []
    for manifest, skill in zip(manifests, runtime_skills, strict=True):
        if getattr(manifest, "relative_root", None) != skill.relative_path.as_posix():
            raise RuntimeError("Private runtime Skill manifest path does not match exact runtime Skill")
        version_id = str(getattr(manifest, "version_id", ""))
        if not version_id:
            raise RuntimeError("Private runtime Skill manifest is missing an exact version ID")
        version_ids.append(version_id)
    return tuple(version_ids)


def _max_slash_skill_overlay_utf8_bytes(
    skills: list[Skill] | tuple[Skill, ...],
) -> int:
    """Return the largest exact one-Skill activation wrapper material."""

    maximum = 0
    for skill in skills:
        content = skill.skill_file.read_text(encoding="utf-8")
        rendered = (
            f'<slash_skill_activation><skill name="{escape(skill.name, quote=True)}" '
            f'category="{escape(str(skill.category), quote=True)}" '
            f'path="{escape(skill.get_container_file_path(), quote=True)}">'
            f"{escape(content, quote=False)}</skill></slash_skill_activation>"
        )
        maximum = max(maximum, len(rendered.encode("utf-8")))
    return maximum


def make_lead_agent(config: RunnableConfig):
    """LangGraph graph factory; keep the signature compatible with LangGraph Server."""
    runtime_config = _get_runtime_config(config)
    runtime_app_config = runtime_config.get("app_config")
    return _make_lead_agent(
        config,
        app_config=runtime_app_config or get_app_config(),
    )


def _make_lead_agent(
    config: RunnableConfig,
    *,
    app_config: AppConfig,
    private_runtime=None,
    trusted_extension: TrustedLeadAgentExtension | None = None,
    tool_call_control_profile: ResolvedGraphToolCallControlProfile | None = None,
    tool_call_control_scope_id: str | None = None,
    tool_call_control_observer: ToolCallControlObserver | None = None,
    resolved_max_concurrent_subagents: int | None = None,
    resolved_max_total_subagents: int | None = None,
):
    # Lazy import to avoid circular dependency
    from deerflow.tools import get_available_tools
    from deerflow.tools.builtins.tool_search import assemble_deferred_tools, build_mcp_routing_middleware, get_mcp_routing_hints_prompt_section

    if trusted_extension is not None and type(trusted_extension) is not TrustedLeadAgentExtension:
        raise TypeError("trusted_extension must be a TrustedLeadAgentExtension")
    extension = trusted_extension or TrustedLeadAgentExtension()
    resolved_app_config = app_config
    frozen_mode = frozen_checkpoint_channel_mode()
    if frozen_mode is None:
        requested_mode = resolved_app_config.database.checkpoint_channel_mode
    else:
        requested_mode = (config.get("configurable", {}) or {}).get(
            INTERNAL_CHECKPOINT_MODE_KEY,
            resolved_app_config.database.checkpoint_channel_mode,
        )
    mode = freeze_checkpoint_channel_mode(requested_mode)
    snapshot_frequency = freeze_checkpoint_snapshot_frequency(resolved_app_config.database.checkpoint_delta.snapshot_frequency)
    inject_checkpoint_mode(config, mode)
    cfg = _get_runtime_config(config)

    # Extract user_id for user-scoped skill loading.
    # LangGraph gateway injects user_id into config["configurable"];
    # fall back to the runtime contextvar when not present.
    from deerflow.runtime.user_context import get_effective_user_id

    runtime_user_id = cfg.get("user_id")
    resolved_user_id = str(runtime_user_id) if runtime_user_id else get_effective_user_id()

    thinking_enabled = cfg.get("thinking_enabled", True)
    reasoning_effort = cfg.get("reasoning_effort", None)
    requested_model_name: str | None = cfg.get("model_name") or cfg.get("model")
    is_plan_mode = cfg.get("is_plan_mode", False)
    subagent_enabled = cfg.get("subagent_enabled", False)
    max_concurrent_subagents = cfg.get("max_concurrent_subagents", 3)
    non_interactive = bool(cfg.get("non_interactive", False))
    agent_name = None if private_runtime is not None else validate_agent_name(cfg.get("agent_name"))
    if private_runtime is not None:
        is_plan_mode = False
        subagent_enabled = "task" in tuple(getattr(private_runtime, "tool_groups", ()))
        max_concurrent_subagents = 3 if resolved_max_concurrent_subagents is None else resolved_max_concurrent_subagents
    elif resolved_max_concurrent_subagents is not None:
        max_concurrent_subagents = resolved_max_concurrent_subagents

    admitted_control_profile = tool_call_control_profile is not None
    if tool_call_control_profile is None:
        tool_call_control_profile = default_graph_tool_call_control_profile(
            "interactive",
            repeated_calls_enabled=resolved_app_config.loop_detection.enabled,
        )
    elif not isinstance(
        tool_call_control_profile,
        ResolvedGraphToolCallControlProfile,
    ):
        raise TypeError(
            "tool_call_control_profile must be a ResolvedGraphToolCallControlProfile",
        )

    if tool_call_control_scope_id is not None:
        control_scope = FixedToolCallControlScope(tool_call_control_scope_id)
    elif admitted_control_profile and private_runtime is not None:
        raise ValueError(
            "An admitted Private Run tool-call control profile requires its exact run scope",
        )
    else:
        configured_run_id = cfg.get("run_id")
        control_scope = FixedToolCallControlScope(configured_run_id) if isinstance(configured_run_id, str) and configured_run_id else PerInvocationToolCallControlScope()
    tool_call_control_topology = GraphToolCallControlTopology(
        profile=tool_call_control_profile,
        lead_scope=control_scope,
    )
    tool_call_control = tool_call_control_topology.build_lead(
        observer=tool_call_control_observer,
    )
    agent_config = load_agent_config(agent_name) if private_runtime is None else None
    agent_definition_model_settings = getattr(private_runtime, "model_settings", None) if private_runtime is not None else getattr(agent_config, "model_settings", None)
    if agent_definition_model_settings is not None:
        thinking_enabled = bool(
            _resolve_runtime_option(
                cfg,
                "thinking_enabled",
                getattr(
                    agent_definition_model_settings,
                    "thinking_enabled",
                    None,
                ),
                True,
            )
        )
        reasoning_effort = _resolve_runtime_option(
            cfg,
            "reasoning_effort",
            getattr(
                agent_definition_model_settings,
                "reasoning_effort",
                None,
            ),
            None,
        )

    runtime_skills = tuple(getattr(private_runtime, "skills", ())) if private_runtime is not None else None
    runtime_skill_version_ids = _exact_runtime_skill_version_ids(private_runtime, runtime_skills) if runtime_skills is not None else None
    available_skills = {skill.name for skill in runtime_skills} if runtime_skills is not None else _available_skill_names(agent_config)
    # Custom agent model from agent config (if any), or None to let _resolve_model_name pick the default
    agent_model_name = getattr(private_runtime, "model_ref", None) if private_runtime is not None else agent_config.model if agent_config and agent_config.model else None

    # Final model name resolution: request → agent config → global default, with fallback for unknown names
    if private_runtime is not None:
        model_name = _resolve_private_runtime_model_name(
            model_ref=agent_model_name,
            requested_model_name=requested_model_name,
            app_config=resolved_app_config,
        )
    else:
        model_name = _resolve_model_name(
            requested_model_name or agent_model_name,
            app_config=resolved_app_config,
        )

    model_config = resolved_app_config.get_model_config(model_name)

    if model_config is None:
        raise ValueError("No chat model could be resolved. A platform administrator must configure an active model in System Settings, and the request must reference its model UUID.")
    exact_agent_thinking = getattr(agent_definition_model_settings, "thinking_enabled", None) if agent_definition_model_settings is not None else None
    exact_agent_reasoning = getattr(agent_definition_model_settings, "reasoning_effort", None) if agent_definition_model_settings is not None else None
    if "thinking_enabled" not in cfg and exact_agent_thinking is True and not model_config.supports_thinking:
        raise ValueError(f"Model {model_name} does not support exact Agent thinking")
    if "reasoning_effort" not in cfg and exact_agent_reasoning is not None and not model_config.supports_reasoning_effort:
        raise ValueError(f"Model {model_name} does not support exact Agent reasoning effort")
    if thinking_enabled and not model_config.supports_thinking:
        logger.warning(f"Thinking mode is enabled but model '{model_name}' does not support it; fallback to non-thinking mode.")
        thinking_enabled = False

    logger.info(
        "Create Agent(%s) -> thinking_enabled: %s, reasoning_effort: %s, model_name: %s, is_plan_mode: %s, subagent_enabled: %s, max_concurrent_subagents: %s",
        agent_name or "default",
        thinking_enabled,
        reasoning_effort,
        model_name,
        is_plan_mode,
        subagent_enabled,
        max_concurrent_subagents,
    )

    # Inject run metadata for LangSmith trace tagging
    if "metadata" not in config:
        config["metadata"] = {}

    config["metadata"].update(
        {
            "agent_name": agent_name or "default",
            "model_name": model_name or "default",
            "thinking_enabled": thinking_enabled,
            "reasoning_effort": reasoning_effort,
            "is_plan_mode": is_plan_mode,
            "subagent_enabled": subagent_enabled,
            "tool_groups": list(getattr(private_runtime, "tool_groups", ())) if private_runtime is not None else agent_config.tool_groups if agent_config else None,
            "available_skills": sorted(available_skills) if available_skills is not None else None,
        }
    )

    # Inject tracing callbacks at the graph invocation root so a single LangGraph
    # run produces one trace with all node / LLM / tool calls as child spans,
    # AND so the Langfuse handler sees ``on_chain_start(parent_run_id=None)`` and
    # actually propagates ``langfuse_session_id`` / ``langfuse_user_id`` from
    # ``config["metadata"]`` onto the trace. Without root-level attachment the
    # model is a nested observation and the handler strips ``langfuse_*`` keys.
    tracing_callbacks = build_tracing_callbacks()
    if tracing_callbacks:
        existing = config.get("callbacks") or []
        if not isinstance(existing, list):
            existing = list(existing)
        config["callbacks"] = [*existing, *tracing_callbacks]

    available_skill_catalog = (
        list(runtime_skills)
        if runtime_skills is not None
        else _load_enabled_available_skills(
            available_skills,
            app_config=resolved_app_config,
            user_id=resolved_user_id,
        )
    )

    # Build skill search setup (deferred skill discovery).
    # Controlled by skills.deferred_discovery — independent from tool_search.enabled.
    from deerflow.skills.describe import build_skill_search_setup

    skill_search_enabled = resolved_app_config.skills.deferred_discovery
    container_base_path = resolved_app_config.skills.container_path if private_runtime is not None else resolved_app_config.skills.container_path
    runtime_skills_root = Path(getattr(private_runtime, "skill_root")) if private_runtime is not None and runtime_skills else None

    # Build discovery from the Agent-available Skill catalog. Availability is
    # not activation: only the exact-runtime middleware may apply allowed-tools.
    skill_setup = build_skill_search_setup(
        available_skill_catalog,
        enabled=skill_search_enabled,
        container_base_path=container_base_path,
    )
    # Default lead agent (unchanged behavior)
    asset_context = _trusted_runtime_asset_context(cfg)
    tool_kwargs = {
        "model_name": model_name,
        "groups": list(getattr(private_runtime, "tool_groups", ())) if private_runtime is not None else agent_config.tool_groups if agent_config else None,
        "subagent_enabled": subagent_enabled,
        "app_config": resolved_app_config,
        "asset_context": asset_context,
    }
    if private_runtime is not None:
        tool_kwargs["include_mcp"] = False
        tool_kwargs["include_acp"] = False
    raw_tools = get_available_tools(**tool_kwargs)
    private_mcp_tools = list(getattr(private_runtime, "mcp_tools", ())) if private_runtime is not None else []
    candidate_tools = [tool for tool in (*raw_tools, *private_mcp_tools) if tool.name not in extension.excluded_tool_names]
    existing_names = {tool.name for tool in candidate_tools}
    duplicate_extension_names = existing_names & {tool.name for tool in extension.extra_tools}
    if duplicate_extension_names:
        raise ValueError("Trusted lead Agent extra tools conflict with canonical tools: " + ",".join(sorted(duplicate_extension_names)))
    candidate_tools.extend(extension.extra_tools)
    if any(tool.name == "inspect_image" for tool in candidate_tools):
        raise ValueError(
            "Reserved platform tool name 'inspect_image' conflicts with a configured runtime tool",
        )
    if private_runtime is not None and not model_config.supports_vision and resolved_app_config.vision_bridge.model_name is not None:
        from deerflow.tools.builtins.inspect_image_tool import (
            build_inspect_image_tool,
        )

        candidate_tools.append(
            build_inspect_image_tool(app_config=resolved_app_config),
        )
    configured_tools = candidate_tools
    if non_interactive:
        configured_tools = [tool for tool in configured_tools if tool.name not in _NON_INTERACTIVE_DISABLED_TOOL_NAMES]
        if requires_host_bash_approval(resolved_app_config):
            configured_tools = [tool for tool in configured_tools if tool.name != "bash"]
    final_tools, setup = assemble_deferred_tools(
        configured_tools,
        enabled=resolved_app_config.tool_search.enabled,
    )
    mcp_routing_middleware = build_mcp_routing_middleware(
        final_tools,
        setup,
        top_k=resolved_app_config.tool_search.auto_promote_top_k,
    )
    mcp_routing_hints_section = get_mcp_routing_hints_prompt_section(
        configured_tools,
        deferred_names=setup.deferred_names,
    )
    if skill_setup.describe_skill_tool:
        final_tools.append(skill_setup.describe_skill_tool)
    # Lead-only memory tools: each exists only when the Worker installed a
    # Memory authority with that capability in this Run's context. Subagent
    # tool assembly never sees the parent runtime context, so the tools stay
    # invisible to subagents by construction.
    from deerflow.agents.memory.authority_resolution import resolve_memory_authority
    from deerflow.tools.builtins import recall_memory_tool, remember_tool

    if resolve_memory_authority(cfg, method="search_episodes") is not None:
        final_tools.append(recall_memory_tool)
    if resolve_memory_authority(cfg, method="propose_entry") is not None:
        final_tools.append(remember_tool)
    final_tools = [tool for tool in final_tools if tool.name not in extension.excluded_tool_names]
    private_prompt_bundle = getattr(private_runtime, "prompt_bundle", None) if private_runtime is not None else None
    runtime_agent_catalog = trusted_runtime_agent_catalog(getattr(private_runtime, "agent_catalog", None)) if private_runtime is not None else None
    raw_capability_notice = getattr(private_runtime, "capability_notice", "") if private_runtime is not None else ""
    runtime_capability_notice = raw_capability_notice if isinstance(raw_capability_notice, str) else ""
    agent_model_overrides: dict[str, object] = {}
    if agent_definition_model_settings is not None:
        sampling_overrides = getattr(
            agent_definition_model_settings,
            "sampling_overrides",
            None,
        )
        if callable(sampling_overrides):
            agent_model_overrides.update(sampling_overrides())
    for key in ("temperature", "max_tokens"):
        if key in cfg:
            if cfg[key] is None:
                agent_model_overrides.pop(key, None)
            else:
                agent_model_overrides[key] = cfg[key]
    create_model_kwargs: dict[str, object] = {
        "model_name": model_name,
        "thinking_enabled": thinking_enabled,
        "reasoning_effort": reasoning_effort,
    }
    if agent_model_overrides:
        create_model_kwargs["model_overrides"] = agent_model_overrides
    model_runtime = ModelRuntime(app_config=resolved_app_config)
    lead_model = model_runtime.build_chat_model(
        profile=ModelRuntimeProfile.AGENT_GRAPH,
        **create_model_kwargs,
    )
    output_limit_recovery_model = None
    if private_runtime is not None:
        recovery_model_kwargs = {
            **create_model_kwargs,
            "thinking_enabled": False,
            "reasoning_effort": None,
        }
        output_limit_recovery_model = model_runtime.build_chat_model(
            profile=ModelRuntimeProfile.AGENT_GRAPH,
            **recovery_model_kwargs,
        )
    effective_middleware = normalize_middleware_state_schemas(
        build_middlewares(
            config,
            model_name=model_name,
            context_model=lead_model,
            agent_name=agent_name,
            available_skills=available_skills,
            app_config=resolved_app_config,
            deferred_setup=setup,
            mcp_routing_middleware=mcp_routing_middleware,
            user_id=resolved_user_id,
            runtime_skills=runtime_skills,
            runtime_skill_version_ids=runtime_skill_version_ids,
            runtime_skills_root=(runtime_skills_root if runtime_skills is not None else None),
            runtime_skills_container_path=(container_base_path if runtime_skills is not None else None),
            resolved_subagent_enabled=subagent_enabled,
            resolved_max_concurrent_subagents=max_concurrent_subagents,
            resolved_max_total_subagents=resolved_max_total_subagents,
            tool_call_control=tool_call_control,
            output_limit_recovery_model=output_limit_recovery_model,
            output_limit_recovery_override=(extension.output_limit_recovery_override),
            custom_middlewares=list(extension.custom_middlewares),
        ),
        mode,
        snapshot_frequency,
    )
    system_prompt = (
        extension.system_prompt_override
        if extension.system_prompt_override is not None
        else apply_prompt_template(
            subagent_enabled=subagent_enabled,
            max_concurrent_subagents=max_concurrent_subagents,
            agent_name=agent_name,
            available_skills=available_skills,
            app_config=resolved_app_config,
            deferred_names=setup.deferred_names,
            mcp_routing_hints_section=mcp_routing_hints_section,
            user_id=resolved_user_id,
            skill_names=skill_setup.skill_names or None,
            exact_soul=str(getattr(private_runtime, "soul")) if private_runtime is not None and private_prompt_bundle is None else None,
            exact_agent_prompt=private_prompt_bundle,
            exact_skills=runtime_skills,
            exact_skills_container_path=container_base_path if runtime_skills is not None else None,
            runtime_agent_catalog=runtime_agent_catalog,
            inspect_image_available=any(tool.name == "inspect_image" for tool in final_tools),
            runtime_capability_notice=runtime_capability_notice,
        )
    )
    state_schema = get_thread_state_schema(mode, snapshot_frequency)
    graph_inputs = AgentGraphExecutionInputs(
        model=lead_model,
        tools=tuple(final_tools),
        middleware=tuple(effective_middleware),
        system_prompt=system_prompt,
        state_schema=state_schema,
        name=agent_name,
    )
    if private_runtime is not None:
        parent_profile = PrivateRunParentExecutionProfile(
            graph=graph_inputs,
            app_config=resolved_app_config,
            asset_context=asset_context,
            private_runtime=private_runtime,
            model_name=model_name,
            thinking_enabled=bool(thinking_enabled),
            reasoning_effort=reasoning_effort,
            runtime_skills=tuple(runtime_skills or ()),
            runtime_agent_catalog=runtime_agent_catalog,
            tool_groups=tuple(getattr(private_runtime, "tool_groups", ())),
        )
    else:
        parent_profile = ConfiguredLeadParentExecutionProfile(
            graph=graph_inputs,
            app_config=resolved_app_config,
            asset_context=asset_context,
            agent_config=agent_config,
            model_name=model_name,
            thinking_enabled=bool(thinking_enabled),
            reasoning_effort=reasoning_effort,
            plan_mode=bool(is_plan_mode),
            subagent_enabled=bool(subagent_enabled),
            agent_name=agent_name,
            available_skills=(tuple(sorted(available_skills)) if available_skills is not None else None),
        )
    final_tools = bind_task_tool_in_tools(
        final_tools,
        ParentExecutionBindingFactory(
            parent_profile,
            tool_call_control_topology=tool_call_control_topology,
            tool_call_control_observer=tool_call_control_observer,
        ),
    )
    configured_run_id = cfg.get("run_id")
    (
        custom_overlay_material,
        custom_overlay_message_count,
        custom_request_unsupported_reason,
    ) = collect_custom_middleware_request_contract(extension.custom_middlewares)
    provider_request_profile = build_provider_request_profile(
        model=lead_model,
        model_name=model_name,
        provider_adapter=model_config.system_provider_adapter,
        provider_class_path=model_config.use,
        system_prompt=system_prompt,
        tools=(
            *collect_middleware_tools(effective_middleware),
            *final_tools,
        ),
        middleware_system_prompts=collect_middleware_system_prompts(
            effective_middleware,
        ),
        bounded_overlay_material=custom_overlay_material,
        bounded_overlay_utf8_bytes=(_CANONICAL_BOUNDED_OVERLAY_UTF8_BYTES + _max_slash_skill_overlay_utf8_bytes(available_skill_catalog)),
        bounded_overlay_message_count=8 + custom_overlay_message_count,
        supports_vision=model_config.supports_vision,
        unsupported_reason=custom_request_unsupported_reason,
        authority_identity=(configured_run_id if isinstance(configured_run_id, str) and configured_run_id else None),
        capture_provider_input_tokens=bool(
            resolved_app_config.token_usage.enabled,
        ),
        closure_identity=(getattr(private_runtime, "provider_request_closure_identity", None) if private_runtime is not None else None),
        mcp_closure_present=bool(getattr(private_runtime, "provider_request_mcp_closure_present", False)),
        runtime_policy_identity=provider_request_runtime_policy_identity(
            resolved_app_config,
        ),
        workload_profile=tool_call_control_profile.workload_profile,
    )
    effective_middleware = append_final_provider_request_guard(
        effective_middleware,
        FinalProviderRequestGuard(provider_request_profile),
    )
    return create_agent(
        model=lead_model,
        tools=final_tools,
        middleware=effective_middleware,
        system_prompt=system_prompt,
        state_schema=state_schema,
    )


def _make_lead_agent_with_private_runtime(
    *,
    config: RunnableConfig,
    private_runtime,
    app_config: AppConfig | None = None,
    trusted_extension: TrustedLeadAgentExtension | None = None,
    tool_call_control_profile: ResolvedGraphToolCallControlProfile | None = None,
    tool_call_control_scope_id: str | None = None,
    tool_call_control_observer: ToolCallControlObserver | None = None,
    resolved_max_concurrent_subagents: int | None = None,
    resolved_max_total_subagents: int | None = None,
):
    runtime_config = _get_runtime_config(config)
    runtime_app_config = runtime_config.get("app_config")
    return _make_lead_agent(
        config,
        app_config=app_config or runtime_app_config or get_app_config(),
        private_runtime=private_runtime,
        trusted_extension=trusted_extension,
        tool_call_control_profile=tool_call_control_profile,
        tool_call_control_scope_id=tool_call_control_scope_id,
        tool_call_control_observer=tool_call_control_observer,
        resolved_max_concurrent_subagents=resolved_max_concurrent_subagents,
        resolved_max_total_subagents=resolved_max_total_subagents,
    )


setattr(make_lead_agent, "private_runtime_factory", _make_lead_agent_with_private_runtime)
