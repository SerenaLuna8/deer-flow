import shlex

from langchain.tools import tool
from langgraph.types import Command

from deerflow.config import get_app_config
from deerflow.runtime.secret_context import (
    SKILL_SECRET_EXEC_READY_CONTEXT_KEY,
    SKILL_SECRET_PROVIDER_CONTEXT_KEY,
    read_active_secrets,
)
from deerflow.sandbox.exceptions import SandboxError
from deerflow.sandbox.security import (
    LOCAL_HOST_BASH_DISABLED_MESSAGE,
    HostBashExecutionMode,
)
from deerflow.sandbox.tooling.bash_policy import (
    _apply_cwd_prefix,
    replace_virtual_paths_in_command,
    validate_local_bash_command_paths,
)
from deerflow.sandbox.tooling.host_execution import (
    _approval_required_bash,
    _channel_identity_prefix,
    _github_env_from_runtime,
    _is_windows,
    _runtime_app_config,
    _runtime_host_bash_execution_mode,
    _truncate_bash_output,
    mask_secret_values,
)
from deerflow.sandbox.tooling.path_mapping import (
    VIRTUAL_PATH_PREFIX,
    mask_local_paths_in_output,
)
from deerflow.sandbox.tooling.runtime import (
    _run_sync_tool_after_async_sandbox_init,
    _sanitize_error,
    ensure_sandbox_initialized,
    ensure_thread_directories_exist,
    get_thread_data,
)
from deerflow.tools.types import Runtime

__all__ = ["bash_tool"]


@tool("bash", parse_docstring=True)
def bash_tool(runtime: Runtime, description: str, command: str) -> str:
    """Execute a bash command in a Linux environment.


    - Use `python` to run Python code.
    - Prefer a thread-local virtual environment in `/mnt/user-data/workspace/.venv`.
    - Use `python -m pip` (inside the virtual environment) to install Python packages.
    - To start a long-lived process such as a web server, ALWAYS run it in the background with its
      output redirected, e.g. `your-command > /mnt/user-data/workspace/server.log 2>&1 &`, then check
      the log file or poll the port. A long-lived process run in the foreground blocks the turn until
      it is killed at the command timeout.

    Args:
        description: Explain why you are running this command in short words. ALWAYS PROVIDE THIS PARAMETER FIRST.
        command: The bash command to execute. Always use absolute paths for files and directories.
    """
    # Resolve the request-scoped carrier before sandbox initialization so even
    # initialization/permission failures can be scrubbed with the same values.
    # Never return an exception-derived string without passing this redactor.
    runtime_context = getattr(runtime, "context", None)
    private_skill_provider = runtime_context.get(SKILL_SECRET_PROVIDER_CONTEXT_KEY) if isinstance(runtime_context, dict) and "private_scope" in runtime_context else None
    if callable(private_skill_provider) and runtime_context.get(SKILL_SECRET_EXEC_READY_CONTEXT_KEY) is not True:
        return "Error: Skill secret material is unavailable"
    injected_env = read_active_secrets(runtime_context) or None
    try:
        sandbox = ensure_sandbox_initialized(runtime)
        # Request-scoped secrets resolved for the active skill (#3861), plus a
        # short-lived GitHub App installation token threaded through by the
        # GitHub channel. Both are injected as per-call env into the subprocess,
        # never placed in the command string.
        identity_prefix = _channel_identity_prefix(runtime)
        github_env = _github_env_from_runtime(runtime)
        if github_env:
            injected_env = {**(injected_env or {}), **github_env}
        runtime_app_config = _runtime_app_config(runtime)
        if runtime_app_config is None:
            runtime_app_config = get_app_config()
        host_execution_mode = _runtime_host_bash_execution_mode(
            runtime,
            runtime_app_config,
        )
        if host_execution_mode is HostBashExecutionMode.LOCAL_APPROVAL_REQUIRED:
            return "Error: Host execution approval requires the asynchronous bash path"
        if host_execution_mode is HostBashExecutionMode.LOCAL_DISABLED:
            return f"Error: {LOCAL_HOST_BASH_DISABLED_MESSAGE}"
        if host_execution_mode is HostBashExecutionMode.LOCAL_LEGACY_ALLOW:
            ensure_thread_directories_exist(runtime)
            thread_data = get_thread_data(runtime)
            validate_local_bash_command_paths(command, thread_data)
            command = replace_virtual_paths_in_command(command, thread_data)
            command = _apply_cwd_prefix(command, thread_data)
            # POSIX-only: the Windows local sandbox may execute via
            # PowerShell/cmd.exe where `export` is not valid syntax.
            if identity_prefix and not _is_windows():
                command = identity_prefix + command
            try:
                sandbox_cfg = getattr(runtime_app_config, "sandbox", None)
                max_chars = sandbox_cfg.bash_output_max_chars if sandbox_cfg else 20000
                command_timeout = sandbox_cfg.bash_command_timeout if sandbox_cfg else None
            except Exception:
                max_chars = 20000
                command_timeout = None
            output = sandbox.execute_command(command, env=injected_env, timeout=command_timeout)
            return _truncate_bash_output(
                mask_secret_values(mask_local_paths_in_output(output, thread_data), injected_env),
                max_chars,
            )
        ensure_thread_directories_exist(runtime)
        command = f"cd -- {shlex.quote(f'{VIRTUAL_PATH_PREFIX}/workspace')} && {command}"
        if identity_prefix:
            command = identity_prefix + command
        try:
            sandbox_cfg = getattr(runtime_app_config, "sandbox", None)
            max_chars = sandbox_cfg.bash_output_max_chars if sandbox_cfg else 20000
        except Exception:
            max_chars = 20000
        return _truncate_bash_output(mask_secret_values(sandbox.execute_command(command, env=injected_env), injected_env), max_chars)
    except SandboxError as e:
        return mask_secret_values(f"Error: {e}", injected_env)
    except PermissionError as e:
        return mask_secret_values(f"Error: {e}", injected_env)
    except Exception as e:
        return mask_secret_values(
            f"Error: Unexpected error executing command: {_sanitize_error(e, runtime)}",
            injected_env,
        )
    finally:
        if injected_env and callable(private_skill_provider):
            injected_env.clear()


async def _bash_tool_async(
    runtime: Runtime,
    description: str,
    command: str,
) -> str | Command:
    runtime_app_config = _runtime_app_config(runtime)
    if runtime_app_config is None:
        runtime_app_config = get_app_config()
    host_execution_mode = _runtime_host_bash_execution_mode(
        runtime,
        runtime_app_config,
    )
    if host_execution_mode is HostBashExecutionMode.LOCAL_APPROVAL_REQUIRED:
        return await _approval_required_bash(
            runtime,
            description,
            command,
        )
    if host_execution_mode is HostBashExecutionMode.LOCAL_DISABLED:
        return f"Error: {LOCAL_HOST_BASH_DISABLED_MESSAGE}"
    return await _run_sync_tool_after_async_sandbox_init(
        bash_tool.func,
        runtime,
        description,
        command,
        authorization_operation="before_sandbox_exec",
    )


bash_tool.coroutine = _bash_tool_async
