import logging
import os
import shlex
from collections.abc import Mapping

from langchain_core.messages import ToolMessage
from langgraph.graph import END
from langgraph.types import Command

from deerflow.config import get_app_config
from deerflow.runtime.context_keys import RuntimeContextKeys
from deerflow.runtime.host_execution_approval import (
    HOST_EXECUTION_AGENT_PATH_CONTEXT_KEY,
    HOST_EXECUTION_APPROVAL_CONTEXT_KEY,
    HOST_EXECUTION_MAX_CHANNEL_USER_ID_LENGTH,
    HOST_EXECUTION_MAX_REQUESTED_COMMAND_BYTES,
    HOST_EXECUTION_MAX_TOOL_CALL_ID_BYTES,
    HostExecutionApprovalPort,
    HostExecutionChannelIdentityMode,
    HostExecutionPlan,
    HostExecutionSkillSecretSource,
)
from deerflow.runtime.secret_context import (
    ACTIVE_SECRET_SOURCES_CONTEXT_KEY,
    SKILL_SECRET_PROVIDER_CONTEXT_KEY,
    active_provider_environment_keys,
    active_provider_secret_request,
    extract_request_secrets,
    normalize_active_secret_declarations,
    read_active_secrets,
    resolve_provider_active_secrets,
)
from deerflow.sandbox.exceptions import SandboxError, SandboxRuntimeError
from deerflow.sandbox.security import (
    HostBashExecutionMode,
    resolve_host_bash_execution_mode,
    resolve_local_host_bash_execution_mode,
    uses_local_sandbox_provider,
)
from deerflow.sandbox.tooling.bash_policy import (
    _apply_cwd_prefix,
    replace_virtual_paths_in_command,
    validate_local_bash_command_paths,
)
from deerflow.sandbox.tooling.runtime import (
    _sanitize_error,
    ensure_sandbox_initialized_async,
    ensure_thread_directories_exist,
    get_thread_data,
    is_local_sandbox,
)
from deerflow.tools.types import Runtime

logger = logging.getLogger(__name__)

_SECRET_REDACTION = "[redacted]"


def mask_secret_values(output: str, injected_env: dict[str, str] | None) -> str:
    """Redact injected secret values from bash output before it re-enters context.

    Skill scripts receive request-scoped secrets as env vars (#3861). If a script
    echoes one (debugging, ``set -x``, an error dump), the value would otherwise
    flow into the tool result — and thus into the prompt and the trace. This is
    the skill-specific fifth leak surface (the bash tool returns subprocess
    stdout, unlike MCP tools). Replace every non-empty secret value with a
    redaction marker, including short PINs. A short value can cause false-positive
    masking in otherwise benign output, but confidentiality takes precedence:
    once a value has been admitted as a secret there is no reliable way to
    distinguish an echoed secret from an identical ordinary token. Longest
    values are replaced first so overlapping values are never partially exposed.
    """
    if not injected_env or not output:
        return output
    for value in sorted(
        {value for value in injected_env.values() if value},
        key=len,
        reverse=True,
    ):
        output = output.replace(value, _SECRET_REDACTION)
    return output


def _truncate_bash_output(output: str, max_chars: int) -> str:
    """Middle-truncate bash output, preserving head and tail (50/50 split).

    bash output may have errors at either end (stderr/stdout ordering is
    non-deterministic), so both ends are preserved equally.

    The returned string (including the truncation marker) is guaranteed to be
    no longer than max_chars characters. Pass max_chars=0 to disable truncation
    and return the full output unchanged.
    """
    if max_chars == 0:
        return output
    if len(output) <= max_chars:
        return output
    total_len = len(output)
    # Compute the exact worst-case marker length: skipped chars is at most
    # total_len, so this is a tight upper bound.
    marker_max_len = len(f"\n... [middle truncated: {total_len} chars skipped] ...\n")
    kept = max(0, max_chars - marker_max_len)
    if kept == 0:
        return output[:max_chars]
    head_len = kept // 2
    tail_len = kept - head_len
    skipped = total_len - kept
    marker = f"\n... [middle truncated: {skipped} chars skipped] ...\n"
    return f"{output[:head_len]}{marker}{output[-tail_len:] if tail_len > 0 else ''}"


# Fixed env var exposing the IM-channel platform user id (Feishu open_id,
# Slack Uxxx, ...) to sandbox commands, so skills can act on the current end
# user's channel identity (#3914). An identifier, not a secret.
CHANNEL_USER_ID_ENV = "ACT_WEAVE_CHANNEL_USER_ID"

_CHANNEL_USER_ID_CONTEXT_KEY = "channel_user_id"


def _is_windows() -> bool:
    return os.name == "nt"


def _channel_identity_state(
    runtime: Runtime,
) -> tuple[HostExecutionChannelIdentityMode, str | None]:
    """Read the trusted runtime carrier's exact channel identity state."""

    context = getattr(runtime, "context", None)
    if not isinstance(context, dict) or _CHANNEL_USER_ID_CONTEXT_KEY not in context:
        return "absent", None
    channel_user_id = context.get(_CHANNEL_USER_ID_CONTEXT_KEY)
    if isinstance(channel_user_id, str) and 0 < len(channel_user_id) <= HOST_EXECUTION_MAX_CHANNEL_USER_ID_LENGTH:
        return "set", channel_user_id
    return "unset", None


def _channel_identity_prefix_from_state(
    mode: HostExecutionChannelIdentityMode,
    channel_user_id: str | None,
) -> str | None:
    if mode == "absent":
        return None
    if mode == "set":
        if channel_user_id is None:  # pragma: no cover - plan/state gate
            raise ValueError("set channel identity requires channel_user_id")
        return f"export {CHANNEL_USER_ID_ENV}={shlex.quote(channel_user_id)}; "
    return f"unset {CHANNEL_USER_ID_ENV}; "


def _channel_identity_prefix(runtime: Runtime) -> str | None:
    """Build the command prefix that sets or clears the channel-user-id env var.

    Returns ``None`` for a non-IM run (no ``channel_user_id`` key in context) so
    the command is left untouched. For an IM run the prefix is always emitted:

    - valid id (non-empty str within the length cap) → ``export VAR=<quoted>; ``
    - unusable id (empty / non-str / over the cap) → ``unset VAR; ``

    The id deliberately rides the command string instead of the
    ``execute_command(env=...)`` channel: a non-empty ``env`` switches
    ``AioSandbox`` to the ``bash.exec`` API (fresh session per call, image
    >= 1.9.3 required), which is reserved for request-scoped secrets. Emitting an
    explicit ``export``-or-``unset`` on every IM command makes per-call identity
    correct **without depending on the AIO shell's session semantics**: the AIO
    no-env path reuses a persistent shell session (the reason for the class lock,
    #1433), so a bare command could otherwise resolve a stale value exported by
    an earlier sender in a shared group-chat sandbox. The ``unset`` closes the
    window the length/type guard would otherwise open — a sender whose id is
    dropped inherits the previous sender's value. Values are identifiers, not
    secrets, so keeping them in the audit-visible command string is fine.
    """
    return _channel_identity_prefix_from_state(*_channel_identity_state(runtime))


def _github_env_from_runtime(runtime: Runtime) -> dict[str, str] | None:
    """Build a per-call env overlay carrying a GitHub App installation token.

    The GitHub channel mints a short-lived installation token in the
    ``ChannelManager`` (app layer) and threads it through ``run_context``
    so it lands in ``runtime.context["github_token"]``. We expose it to
    the agent's bash as both ``GH_TOKEN`` (what the ``gh`` CLI reads) and
    ``GITHUB_TOKEN`` (the conventional name). Returning ``None`` when no
    token is present keeps non-GitHub runs identical to before.

    The value at ``runtime.context["github_token"]`` may be either:

    * a ``str`` — the captured token, the simple shape used by tests and
      by older code paths that don't need refresh; or
    * a zero-arg sync callable returning ``str`` — a provider that re-mints
      transparently when the underlying installation token's 1h TTL is
      nearing expiry. The provider's cache logic lives app-side (see
      ``app.gateway.github.app_auth.mint_installation_token`` for the
      cache + leeway semantics); the harness just calls it.

    The callable path is what lets long autonomous runs survive past the
    60-minute installation-token life: every bash invocation re-asks the
    provider, which returns the cached token until ~55 min, then mints a
    fresh one. Without this, a coder agent doing a multi-hour refactor
    would do most of the work and then 401 on the final ``git push``.

    The token still crosses the harness/app boundary as opaque data — the
    harness never imports the app-layer minting code, preserving the package
    dependency firewall.
    """
    context = runtime.context if runtime.context is not None else None
    value = context.get("github_token") if context else None
    if callable(value):
        try:
            token = value()
        except Exception:
            logger.warning("github_token provider raised; skipping env overlay", exc_info=True)
            return None
    else:
        token = value
    if not isinstance(token, str) or not token:
        return None
    return {"GH_TOKEN": token, "GITHUB_TOKEN": token}


def _runtime_app_config(runtime: Runtime) -> object | None:
    context = getattr(runtime, "context", None)
    if isinstance(context, Mapping):
        return context.get(RuntimeContextKeys.APP_CONFIG)
    return None


def _runtime_host_bash_execution_mode(
    runtime: Runtime,
    app_config: object,
) -> HostBashExecutionMode:
    """Resolve bash authority from both configuration and actual runtime kind.

    A Local provider can be re-exported, subclassed, or temporarily disagree
    with the configured class path.  Once the trusted runtime identifies a
    Local sandbox, never let a configuration-only ``isolated_direct`` result
    bypass the Local approval policy.
    """

    if is_local_sandbox(runtime) or uses_local_sandbox_provider(app_config):
        return resolve_local_host_bash_execution_mode(app_config)
    return resolve_host_bash_execution_mode(app_config)


def _host_execution_agent_path(context: object) -> tuple[str, ...]:
    if isinstance(context, Mapping):
        raw_path = context.get(HOST_EXECUTION_AGENT_PATH_CONTEXT_KEY)
        if isinstance(raw_path, tuple) and raw_path and all(isinstance(part, str) and part for part in raw_path):
            return raw_path
        if context.get(RuntimeContextKeys.IS_SUBAGENT) is True:
            return ("subagent",)
    return ("lead",)


def _host_execution_environment_keys(context: object) -> tuple[str, ...]:
    names = set(extract_request_secrets(context))
    names.update(read_active_secrets(context))
    names.update(active_provider_environment_keys(context))
    if isinstance(context, Mapping) and context.get("github_token") is not None:
        names.update({"GH_TOKEN", "GITHUB_TOKEN"})
    return tuple(sorted(names))


def _host_execution_skill_secret_sources(
    context: object,
) -> tuple[HostExecutionSkillSecretSource, ...]:
    """Freeze only middleware-owned path/name/activation semantics."""

    if not isinstance(context, Mapping):
        return ()
    raw_sources = context.get(ACTIVE_SECRET_SOURCES_CONTEXT_KEY)
    if not isinstance(raw_sources, tuple):
        return ()
    sources: set[HostExecutionSkillSecretSource] = set()
    for source in raw_sources:
        if not isinstance(source, tuple) or len(source) != 4 or not isinstance(source[0], str) or not source[0] or not isinstance(source[1], str) or not source[1] or not isinstance(source[2], tuple) or type(source[3]) is not bool:
            continue
        declarations = tuple(sorted(normalize_active_secret_declarations(source[2])))
        if not declarations:
            continue
        try:
            sources.add(
                HostExecutionSkillSecretSource(
                    skill_path=source[1],
                    # Frontmatter preserves author order, while the frozen
                    # execution plan treats declarations as a set. Canonicalize
                    # that valid order here; the typed source still rejects
                    # duplicates and malformed names.
                    secret_names=tuple(name for name, _target in declarations),
                    explicit=source[3],
                    target_envs=tuple(target for _name, target in declarations),
                ),
            )
        except ValueError:
            continue
    return tuple(sorted(sources, key=lambda source: source.sort_key))


def _host_execution_legacy_environment_keys(context: object) -> tuple[str, ...]:
    """Identify name-only carriers that cannot be replayed as exact authority."""

    names = set(extract_request_secrets(context))
    names.update(read_active_secrets(context))
    if isinstance(context, Mapping) and context.get("github_token") is not None:
        names.update({"GH_TOKEN", "GITHUB_TOKEN"})
    return tuple(sorted(names))


async def _approval_scan_secrets(runtime: Runtime) -> dict[str, str]:
    """Materialize only enough secret plaintext to reject command embedding."""

    context = getattr(runtime, "context", None)
    secrets = {
        **extract_request_secrets(context),
        **read_active_secrets(context),
    }
    github_env = _github_env_from_runtime(runtime)
    if github_env:
        secrets.update(github_env)
    if not isinstance(context, dict) or "private_scope" not in context:
        return secrets
    provider = context.get(SKILL_SECRET_PROVIDER_CONTEXT_KEY)
    requested = active_provider_secret_request(context)
    if not callable(provider) or not requested:
        return secrets

    fresh_scoped = await provider(requested)
    try:
        secrets.update(resolve_provider_active_secrets(context, fresh_scoped))
    finally:
        if isinstance(fresh_scoped, dict):
            for values in fresh_scoped.values():
                if isinstance(values, dict):
                    values.clear()
            fresh_scoped.clear()
    return secrets


def _command_contains_secret(
    requested_command: str,
    effective_command: str,
    secrets: Mapping[str, str],
) -> bool:
    return any(value and (value in requested_command or value in effective_command) for value in secrets.values() if isinstance(value, str))


def _prepare_local_host_execution(
    runtime: Runtime,
    sandbox: object,
    *,
    description: str,
    requested_command: str,
) -> tuple[HostExecutionPlan, int]:
    """Freeze the exact Local command, shell, timeout, and path mappings."""

    if not isinstance(requested_command, str) or not requested_command:
        raise SandboxRuntimeError("Host command is required")
    if len(requested_command.encode("utf-8", errors="surrogatepass")) > HOST_EXECUTION_MAX_REQUESTED_COMMAND_BYTES:
        raise SandboxRuntimeError("Host command exceeds the approval limit")
    tool_call_id = getattr(runtime, "tool_call_id", None)
    if not isinstance(tool_call_id, str) or not tool_call_id:
        raise SandboxRuntimeError("Host execution tool call id is unavailable")
    if len(tool_call_id.encode("utf-8", errors="surrogatepass")) > HOST_EXECUTION_MAX_TOOL_CALL_ID_BYTES:
        raise SandboxRuntimeError("Host execution tool call id exceeds the approval limit")
    ensure_thread_directories_exist(runtime)
    thread_data = get_thread_data(runtime)
    validate_local_bash_command_paths(requested_command, thread_data)
    command = replace_virtual_paths_in_command(requested_command, thread_data)
    command = _apply_cwd_prefix(command, thread_data)
    channel_identity_mode, channel_user_id = _channel_identity_state(runtime)
    identity_prefix = _channel_identity_prefix_from_state(
        channel_identity_mode,
        channel_user_id,
    )
    if identity_prefix and not _is_windows():
        command = identity_prefix + command

    resolve_command = getattr(sandbox, "resolve_command_for_execution", None)
    resolve_shell = getattr(sandbox, "get_execution_shell", None)
    execute_prepared = getattr(sandbox, "execute_prepared_command_result", None)
    if not callable(resolve_command) or not callable(resolve_shell) or not callable(execute_prepared):
        raise SandboxRuntimeError(
            "Local sandbox does not support frozen host execution plans",
        )
    effective_command = resolve_command(command)
    shell = resolve_shell()
    if not isinstance(effective_command, str) or not effective_command:
        raise SandboxRuntimeError("Local sandbox returned an invalid prepared command")
    if not isinstance(shell, str) or not shell:
        raise SandboxRuntimeError("Local sandbox returned an invalid shell")

    app_config = _runtime_app_config(runtime)
    if app_config is None:
        app_config = get_app_config()
    sandbox_config = getattr(app_config, "sandbox", None)
    configured_timeout = int(getattr(sandbox_config, "bash_command_timeout", 600))
    approval_config = getattr(sandbox_config, "host_execution_approval", None)
    approval_timeout = int(getattr(approval_config, "max_timeout_seconds", configured_timeout))
    timeout_seconds = min(configured_timeout, approval_timeout)
    max_chars = int(getattr(sandbox_config, "bash_output_max_chars", 20000))

    context = getattr(runtime, "context", None)
    if not isinstance(context, Mapping):
        raise SandboxRuntimeError("Trusted runtime context is unavailable")
    run_id = context.get(RuntimeContextKeys.RUN_ID)
    thread_id = context.get(RuntimeContextKeys.THREAD_ID)
    if not isinstance(run_id, str) or not run_id:
        raise SandboxRuntimeError("Host execution source run id is unavailable")
    if not isinstance(thread_id, str) or not thread_id:
        raise SandboxRuntimeError("Host execution source thread id is unavailable")

    cwd = thread_data.get("workspace_path") if thread_data else None
    return (
        HostExecutionPlan(
            source_tool_call_id=tool_call_id,
            source_run_id=run_id,
            source_thread_id=thread_id,
            description=description,
            requested_command=requested_command,
            effective_command=effective_command,
            shell=shell,
            cwd=cwd if isinstance(cwd, str) and cwd else None,
            timeout_seconds=timeout_seconds,
            environment_keys=_host_execution_environment_keys(context),
            skill_secret_sources=_host_execution_skill_secret_sources(
                context,
            ),
            legacy_environment_keys=_host_execution_legacy_environment_keys(
                context,
            ),
            agent_path=_host_execution_agent_path(context),
            channel_identity_mode=channel_identity_mode,
            channel_user_id=channel_user_id,
        ),
        max_chars,
    )


async def _approval_required_bash(
    runtime: Runtime,
    description: str,
    command: str,
) -> str | Command:
    """Stage or consume one exact Local host-execution approval."""

    runtime_context = getattr(runtime, "context", None)
    if isinstance(runtime_context, Mapping) and runtime_context.get(RuntimeContextKeys.NON_INTERACTIVE) is True:
        return "Error: Host execution approval is unavailable for non-interactive runs"

    try:
        sandbox = await ensure_sandbox_initialized_async(runtime)
        plan, max_chars = _prepare_local_host_execution(
            runtime,
            sandbox,
            description=description,
            requested_command=command,
        )
        scan_secrets = await _approval_scan_secrets(runtime)
        try:
            if _command_contains_secret(
                plan.requested_command,
                plan.effective_command,
                scan_secrets,
            ):
                return "Error: Host command contains secret plaintext and cannot be staged"
        finally:
            scan_secrets.clear()
    except (SandboxError, PermissionError) as error:
        return f"Error: {_sanitize_error(error, runtime)}"
    except Exception as error:
        return f"Error: Unexpected error preparing host execution: {_sanitize_error(error, runtime)}"

    context = getattr(runtime, "context", None)
    port = context.get(HOST_EXECUTION_APPROVAL_CONTEXT_KEY) if isinstance(context, Mapping) else None
    if not isinstance(port, HostExecutionApprovalPort):
        return "Error: Host execution approval is unavailable"

    try:
        decision = await port.request_host_execution(plan)
    except Exception:
        return "Error: Host execution approval is unavailable"
    if decision.status == "denied":
        return "Error: Host execution request was denied"
    if decision.status == "pending":
        artifact = decision.artifact
        if artifact is None or artifact.source_run_id != plan.source_run_id or artifact.source_tool_call_id != plan.source_tool_call_id:
            return "Error: Host execution approval returned an invalid artifact"
        message = ToolMessage(
            content="Host command execution requires approval.",
            tool_call_id=plan.source_tool_call_id,
            name="bash",
            artifact={
                "host_execution_approval": artifact.to_payload(),
            },
        )
        return Command(update={"messages": [message]}, goto=END)
    return "Error: Host execution approval returned an invalid decision"


prepare_local_host_execution = _prepare_local_host_execution
truncate_bash_output = _truncate_bash_output

__all__ = [
    "CHANNEL_USER_ID_ENV",
    "mask_secret_values",
    "prepare_local_host_execution",
    "truncate_bash_output",
]
