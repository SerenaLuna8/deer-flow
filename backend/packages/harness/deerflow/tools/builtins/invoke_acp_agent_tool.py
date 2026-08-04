"""Built-in tool for invoking external ACP-compatible agents."""

import asyncio
import logging
import os
import shutil
import signal
import sys
from typing import Annotated, Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, InjectedToolArg, StructuredTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_POSIX_EXEC_WRAPPER = "import os, sys\nos.setsid()\nos.execvpe(sys.argv[1], sys.argv[1:], os.environ)"
_UNCLEAN_LIFECYCLE_TASKS: set[asyncio.Task[Any]] = set()


class _InvokeACPAgentInput(BaseModel):
    agent: str = Field(description="Name of the ACP agent to invoke")
    prompt: str = Field(description="The concise task prompt to send to the agent")


def _get_work_dir(thread_id: str | None) -> str:
    """Get the per-thread ACP workspace directory.

    Each thread gets an isolated workspace under
    ``{base_dir}/threads/{thread_id}/acp-workspace/`` so that concurrent
    sessions cannot read or overwrite each other's ACP agent outputs.

    Falls back to the legacy global ``{base_dir}/acp-workspace/`` when
    ``thread_id`` is not available (e.g. embedded / direct invocation).

    The directory is created automatically if it does not exist.

    Returns:
        An absolute physical filesystem path to use as the working directory.
    """
    from deerflow.config.paths import get_paths
    from deerflow.runtime.user_context import get_effective_user_id

    paths = get_paths()
    if thread_id:
        try:
            work_dir = paths.acp_workspace_dir(thread_id, user_id=get_effective_user_id())
        except ValueError:
            logger.warning("Invalid thread_id %r for ACP workspace, falling back to global", thread_id)
            work_dir = paths.base_dir / "acp-workspace"
    else:
        work_dir = paths.base_dir / "acp-workspace"

    work_dir.mkdir(parents=True, exist_ok=True)
    logger.info("ACP agent work_dir: %s", work_dir)
    return str(work_dir)


def _build_mcp_servers() -> dict[str, dict[str, Any]]:
    """Reject legacy process-local MCP configuration for ACP agents."""
    return {}


def _build_acp_mcp_servers() -> list[dict[str, Any]]:
    """Reject ambient MCP servers from extensions configuration.

    M7 does not bridge process-local extensions configuration into an admitted
    run. ACP invocation therefore receives no ambient MCP servers.
    """
    return []


def _build_permission_response(options: list[Any], *, auto_approve: bool) -> Any:
    """Build an ACP permission response.

    When ``auto_approve`` is True, selects the first ``allow_once`` (preferred)
    or ``allow_always`` option.  When False (the default), always cancels —
    permission requests must be handled by the ACP agent's own policy or the
    agent must be configured to operate without requesting permissions.
    """
    from acp import RequestPermissionResponse
    from acp.schema import AllowedOutcome, DeniedOutcome

    if auto_approve:
        for preferred_kind in ("allow_once", "allow_always"):
            for option in options:
                if getattr(option, "kind", None) != preferred_kind:
                    continue

                option_id = getattr(option, "option_id", None)
                if option_id is None:
                    option_id = getattr(option, "optionId", None)
                if option_id is None:
                    continue

                return RequestPermissionResponse(
                    outcome=AllowedOutcome(outcome="selected", optionId=option_id),
                )

    return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))


def _format_invocation_error(agent: str, cmd: str, exc: Exception) -> str:
    """Return a user-facing ACP invocation error with actionable remediation."""
    if not isinstance(exc, FileNotFoundError):
        return f"Error invoking ACP agent '{agent}': {exc}"

    message = f"Error invoking ACP agent '{agent}': Command '{cmd}' was not found on PATH."
    if cmd == "codex-acp" and shutil.which("codex"):
        return f"{message} The installed `codex` CLI does not speak ACP directly. Install a Codex ACP adapter (for example `npx @zed-industries/codex-acp`) or update `acp_agents.codex.command` and `args` in config.yaml."

    return f"{message} Install the agent binary or update `acp_agents.{agent}.command` in config.yaml."


def _build_spawn_command(
    cmd: str,
    args: list[str],
    agent_env: dict[str, str] | None,
    *,
    cwd: str,
) -> tuple[str, list[str], bool]:
    """Resolve and wrap a POSIX command in an isolated process session."""
    if os.name != "posix":
        return cmd, args, False

    physical_cwd = os.path.abspath(cwd)
    configured_path = agent_env.get("PATH") if agent_env and "PATH" in agent_env else os.environ.get("PATH")
    search_entries: list[str] = []
    search_path_value = configured_path if configured_path is not None else os.defpath
    for entry in search_path_value.split(os.pathsep):
        if not entry:
            entry = physical_cwd
        elif not os.path.isabs(entry):
            entry = os.path.join(physical_cwd, entry)
        search_entries.append(os.path.abspath(entry))
    search_path = os.pathsep.join(search_entries)

    if os.path.dirname(cmd):
        command_candidate = cmd if os.path.isabs(cmd) else os.path.join(physical_cwd, cmd)
        resolved_cmd = shutil.which(os.path.abspath(command_candidate))
    else:
        resolved_cmd = shutil.which(cmd, path=search_path)
    if resolved_cmd is None:
        raise FileNotFoundError(cmd)

    return (
        sys.executable,
        ["-I", "-S", "-c", _POSIX_EXEC_WRAPPER, os.path.abspath(resolved_cmd), *args],
        True,
    )


def _consume_task_result(task: asyncio.Task[Any]) -> BaseException | None:
    """Retrieve a task result so lifecycle failures are always observed."""
    try:
        task.result()
    except BaseException as exc:
        return exc
    return None


def _quarantine_lifecycle_task(task: asyncio.Task[Any]) -> None:
    """Keep a strong, observable reference to an uncooperative lifecycle task."""
    if task.done():
        _consume_task_result(task)
        return
    if task in _UNCLEAN_LIFECYCLE_TASKS:
        return

    _UNCLEAN_LIFECYCLE_TASKS.add(task)

    def _task_finished(done_task: asyncio.Task[Any]) -> None:
        _consume_task_result(done_task)
        _UNCLEAN_LIFECYCLE_TASKS.discard(done_task)

    task.add_done_callback(_task_finished)


def _create_lifecycle_task(
    lifecycle: dict[str, Any],
    awaitable: Any,
    *,
    preserve_on_timeout: bool = False,
) -> asyncio.Task[Any]:
    """Create a task whose ownership remains visible to the cleanup coordinator."""
    task = asyncio.create_task(awaitable)
    owned_tasks: set[asyncio.Task[Any]] = lifecycle.setdefault("tasks", set())
    owned_tasks.add(task)
    preserved_tasks: set[asyncio.Task[Any]] = lifecycle.setdefault(
        "preserved_tasks",
        set(),
    )
    if preserve_on_timeout:
        preserved_tasks.add(task)

    def _task_finished(done_task: asyncio.Task[Any]) -> None:
        _consume_task_result(done_task)
        owned_tasks.discard(done_task)
        preserved_tasks.discard(done_task)

    task.add_done_callback(_task_finished)
    return task


async def _wait_until(
    task: asyncio.Task[Any],
    deadline: float,
) -> bool:
    """Wait for a task until an absolute event-loop deadline."""
    if task.done():
        return True
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        return task.done()
    done, _ = await asyncio.wait({task}, timeout=remaining)
    return bool(done)


async def _cancel_task_until(
    task: asyncio.Task[Any],
    deadline: float,
) -> bool:
    """Cancel a task and confirm it stopped before the cleanup deadline."""
    if task.done():
        _consume_task_result(task)
        return True
    task.cancel()
    # Cancellation is delivered only when the event loop gets a turn. Always
    # provide one turn even when the wall-clock deadline has just elapsed.
    await asyncio.sleep(0)
    await _wait_until(task, deadline)
    if task.done():
        _consume_task_result(task)
        return True
    _quarantine_lifecycle_task(task)
    return False


def _signal_process(
    process: Any,
    *,
    isolated_process_group: bool,
    force_kill: bool,
) -> bool:
    """Signal an isolated process group, or fall back to the direct child."""
    sent_signal = getattr(signal, "SIGKILL", signal.SIGTERM) if force_kill else signal.SIGTERM
    if isolated_process_group and os.name == "posix":
        pid = getattr(process, "pid", None)
        if isinstance(pid, int) and pid > 1 and pid != os.getpgrp():
            try:
                os.killpg(pid, sent_signal)
            except ProcessLookupError:
                # The wrapper may not have reached setsid() yet; fall back to
                # the direct child so the startup race cannot leak a process.
                pass
            except OSError:
                # If group signaling is unavailable, still attempt the child.
                pass
            else:
                return True

    if getattr(process, "returncode", None) is not None:
        return False
    method_name = "kill" if force_kill else "terminate"
    method = getattr(process, method_name, None)
    if not callable(method):
        return False
    try:
        method()
    except ProcessLookupError:
        return False
    except Exception as exc:
        logger.warning(
            "ACP subprocess %s failed (%s)",
            method_name,
            type(exc).__name__,
        )
        return False
    return True


async def _force_reap_process(
    process: Any,
    *,
    lifecycle: dict[str, Any],
    isolated_process_group: bool,
    deadline: float,
) -> str | None:
    """Send TERM/KILL and return an error unless wait confirms child reaping."""
    if process is None:
        return "subprocess handle was unavailable"

    wait_method = getattr(process, "wait", None)
    if not callable(wait_method):
        _signal_process(
            process,
            isolated_process_group=isolated_process_group,
            force_kill=False,
        )
        _signal_process(
            process,
            isolated_process_group=isolated_process_group,
            force_kill=True,
        )
        return "subprocess does not expose a wait method"

    loop = asyncio.get_running_loop()
    wait_task = _create_lifecycle_task(
        lifecycle,
        wait_method(),
        preserve_on_timeout=True,
    )

    if getattr(process, "returncode", None) is None:
        _signal_process(
            process,
            isolated_process_group=isolated_process_group,
            force_kill=False,
        )

    remaining = max(0.0, deadline - loop.time())
    term_deadline = min(deadline, loop.time() + min(1.0, remaining / 2))
    await _wait_until(wait_task, term_deadline)

    must_kill_group = isolated_process_group and os.name == "posix"
    if must_kill_group or not wait_task.done() or getattr(process, "returncode", None) is None:
        _signal_process(
            process,
            isolated_process_group=isolated_process_group,
            force_kill=True,
        )

    await _wait_until(wait_task, deadline)
    if not wait_task.done():
        _quarantine_lifecycle_task(wait_task)
        return "subprocess wait did not finish"

    wait_failure = _consume_task_result(wait_task)
    if wait_failure is not None and not isinstance(
        wait_failure,
        asyncio.CancelledError,
    ):
        return f"subprocess wait failed ({type(wait_failure).__name__})"
    if isinstance(wait_failure, asyncio.CancelledError):
        return "subprocess wait was cancelled"
    if getattr(process, "returncode", None) is None:
        return "subprocess wait returned without a final return code"
    return None


async def _cleanup_process_context(
    lifecycle: dict[str, Any],
    request_task: asyncio.Task[Any],
    *,
    isolated_process_group: bool,
    cleanup_timeout_seconds: float,
    force: bool,
    exit_error: BaseException | None,
) -> tuple[str, ...]:
    """Close the ACP context and report every unverifiable lifecycle boundary."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + cleanup_timeout_seconds
    exit_task: asyncio.Task[Any] | None = None
    failures: list[str] = []

    if not request_task.done():
        request_task.cancel()
        await asyncio.sleep(0)

    if not lifecycle["entered"]:
        if not await _cancel_task_until(request_task, deadline):
            failures.append("process context entry did not stop")
        return tuple(failures)

    context = lifecycle["context"]
    exit_task = _create_lifecycle_task(
        lifecycle,
        context.__aexit__(
            type(exit_error) if exit_error is not None else None,
            exit_error,
            exit_error.__traceback__ if exit_error is not None else None,
        ),
    )

    if not force:
        graceful_deadline = min(
            deadline,
            loop.time() + cleanup_timeout_seconds / 2,
        )
        await _wait_until(exit_task, graceful_deadline)
        if exit_task.done():
            exit_failure = _consume_task_result(exit_task)
            if exit_failure is None:
                process = lifecycle["process"]
                process_failure = await _force_reap_process(
                    process,
                    lifecycle=lifecycle,
                    isolated_process_group=isolated_process_group,
                    deadline=deadline,
                )
                if process_failure is not None:
                    failures.append(process_failure)
                if not request_task.done() and not await _cancel_task_until(
                    request_task,
                    deadline,
                ):
                    failures.append("request task did not stop")
                return tuple(failures)
            failures.append(f"context exit failed ({type(exit_failure).__name__})")

    process_failure = await _force_reap_process(
        lifecycle["process"],
        lifecycle=lifecycle,
        isolated_process_group=isolated_process_group,
        deadline=deadline,
    )
    if process_failure is not None:
        failures.append(process_failure)

    if not exit_task.done():
        await _wait_until(exit_task, deadline)
    if not exit_task.done():
        if not await _cancel_task_until(exit_task, deadline):
            failures.append("context exit did not stop")
        else:
            failures.append("context exit did not complete")
    else:
        exit_failure = _consume_task_result(exit_task)
        if exit_failure is not None and not any(failure.startswith("context exit failed") for failure in failures):
            failures.append(f"context exit failed ({type(exit_failure).__name__})")

    if not request_task.done() and not await _cancel_task_until(
        request_task,
        deadline,
    ):
        failures.append("request task did not stop")

    return tuple(dict.fromkeys(failures))


async def _run_bounded_cleanup(
    lifecycle: dict[str, Any],
    request_task: asyncio.Task[Any],
    *,
    isolated_process_group: bool,
    cleanup_timeout_seconds: float,
    force: bool,
    exit_error: BaseException | None,
) -> tuple[bool, tuple[str, ...]]:
    """Run cleanup to a hard deadline even if the caller is cancelled again."""
    cleanup_task = _create_lifecycle_task(
        lifecycle,
        _cleanup_process_context(
            lifecycle,
            request_task,
            isolated_process_group=isolated_process_group,
            cleanup_timeout_seconds=cleanup_timeout_seconds,
            force=force,
            exit_error=exit_error,
        ),
    )
    deadline = asyncio.get_running_loop().time() + cleanup_timeout_seconds
    cancelled_while_cleaning = False
    failures: tuple[str, ...] = ()

    while not cleanup_task.done():
        try:
            if not await _wait_until(cleanup_task, deadline):
                break
        except asyncio.CancelledError:
            cancelled_while_cleaning = True
            continue

    if cleanup_task.done():
        cleanup_failure = _consume_task_result(cleanup_task)
        if cleanup_failure is None:
            failures = cleanup_task.result()
        elif isinstance(cleanup_failure, asyncio.CancelledError):
            failures = ("cleanup coordinator was cancelled",)
        else:
            failures = (f"cleanup coordinator failed ({type(cleanup_failure).__name__})",)
    else:
        preserved_tasks = lifecycle.get("preserved_tasks", set())
        for owned_task in tuple(lifecycle.get("tasks", ())):
            if owned_task is not cleanup_task and not owned_task.done():
                if owned_task in preserved_tasks:
                    _quarantine_lifecycle_task(owned_task)
                else:
                    owned_task.cancel()
        await asyncio.sleep(0)
        if not await _cancel_task_until(cleanup_task, deadline):
            failures = ("cleanup coordinator did not stop",)
        else:
            failures = ("cleanup coordinator exceeded its deadline",)

    if failures:
        for owned_task in tuple(lifecycle.get("tasks", ())):
            if not owned_task.done():
                _quarantine_lifecycle_task(owned_task)

    return cancelled_while_cleaning, failures


def _format_cleanup_error(
    agent: str,
    *,
    failures: tuple[str, ...],
    completed: bool,
    timed_out_after: int | None = None,
) -> str:
    """Return a bounded cleanup error without exposing process or model data."""
    reason = ", ".join(failures)
    if completed:
        return f"Error: ACP agent '{agent}' completed, but cleanup could not be verified ({reason}). The result was discarded. Restart the Worker before retrying."
    if timed_out_after is not None:
        return f"Error: ACP agent '{agent}' timed out after {timed_out_after} seconds, and cleanup could not be verified ({reason}). Restart the Worker before retrying."
    return f"Error: ACP agent '{agent}' failed, and cleanup could not be verified ({reason}). Restart the Worker before retrying."


def build_invoke_acp_agent_tool(agents: dict) -> BaseTool:
    """Create the ``invoke_acp_agent`` tool with a description generated from configured agents.

    The tool description includes the list of available agents so that the LLM
    knows which agents it can invoke without requiring hardcoded names.

    Args:
        agents: Mapping of agent name -> ``ACPAgentConfig``.

    Returns:
        A LangChain ``BaseTool`` ready to be included in the tool list.
    """
    agent_lines = "\n".join(f"- {name}: {cfg.description}" for name, cfg in agents.items())
    description = (
        "Invoke an external ACP-compatible agent and return its final response.\n\n"
        "Available agents:\n"
        f"{agent_lines}\n\n"
        "IMPORTANT: ACP agents operate in their own independent workspace. "
        "Do NOT include /mnt/user-data paths in the prompt. "
        "Give the agent a self-contained task description — it will produce results in its own workspace. "
        "After the agent completes, its output files are accessible at /mnt/acp-workspace/ (read-only)."
    )

    # Capture agents in closure so the function can reference it
    _agents = dict(agents)

    async def _invoke(agent: str, prompt: str, config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
        logger.info("Invoking ACP agent %s (prompt length: %d)", agent, len(prompt))
        if agent not in _agents:
            available = ", ".join(_agents.keys())
            return f"Error: Unknown agent '{agent}'. Available: {available}"
        if any(not task.done() for task in _UNCLEAN_LIFECYCLE_TASKS):
            return "Error: ACP lifecycle cleanup from a previous invocation is still incomplete. Restart the Worker before invoking another ACP agent."

        agent_config = _agents[agent]
        thread_id: str | None = ((config or {}).get("configurable") or {}).get("thread_id")

        try:
            from acp import PROTOCOL_VERSION, Client, text_block
            from acp.schema import ClientCapabilities, Implementation
        except ImportError:
            return "Error: agent-client-protocol package is not installed. Run `uv sync` to install project dependencies."

        class _CollectingClient(Client):
            """Minimal ACP Client that collects streamed text from session updates."""

            def __init__(self) -> None:
                self._chunks: list[str] = []

            @property
            def collected_text(self) -> str:
                return "".join(self._chunks)

            async def session_update(self, session_id: str, update, **kwargs) -> None:  # type: ignore[override]
                try:
                    from acp.schema import TextContentBlock

                    if hasattr(update, "content") and isinstance(update.content, TextContentBlock):
                        self._chunks.append(update.content.text)
                except Exception:
                    pass

            async def request_permission(self, options, session_id: str, tool_call, **kwargs):  # type: ignore[override]
                response = _build_permission_response(options, auto_approve=agent_config.auto_approve_permissions)
                outcome = response.outcome.outcome
                if outcome == "selected":
                    logger.info("ACP permission auto-approved for tool call %s in session %s", tool_call.tool_call_id, session_id)
                else:
                    logger.warning("ACP permission denied for tool call %s in session %s (set auto_approve_permissions: true in config.yaml to enable)", tool_call.tool_call_id, session_id)
                return response

        client = _CollectingClient()
        cmd = agent_config.command
        args = agent_config.args or []
        physical_cwd = _get_work_dir(thread_id)
        try:
            mcp_servers = _build_acp_mcp_servers()
        except ValueError as exc:
            logger.warning(
                "Invalid MCP server configuration for ACP agent '%s'; continuing without MCP servers: %s",
                agent,
                exc,
            )
            mcp_servers = []
        agent_env: dict[str, str] | None = None
        if agent_config.env:
            agent_env = {k: (os.environ.get(v[1:], "") if v.startswith("$") else v) for k, v in agent_config.env.items()}

        try:
            from acp import spawn_agent_process

            spawn_cmd, spawn_args, isolated_process_group = _build_spawn_command(
                cmd,
                args,
                agent_env,
                cwd=physical_cwd,
            )
            process_context = spawn_agent_process(
                client,
                spawn_cmd,
                *spawn_args,
                env=agent_env,
                cwd=physical_cwd,
            )
            lifecycle: dict[str, Any] = {
                "context": process_context,
                "entered": False,
                "process": None,
                "tasks": set(),
            }

            async def _run_request() -> None:
                conn, process = await process_context.__aenter__()
                lifecycle["process"] = process
                lifecycle["entered"] = True
                logger.info(
                    "Spawning ACP agent '%s' with command '%s' in its isolated workspace",
                    agent,
                    cmd,
                )
                await conn.initialize(
                    protocol_version=PROTOCOL_VERSION,
                    client_capabilities=ClientCapabilities(),
                    client_info=Implementation(
                        name="deerflow",
                        title="ActWeave",
                        version="0.1.0",
                    ),
                )
                session_kwargs: dict[str, Any] = {
                    "cwd": physical_cwd,
                    "mcp_servers": mcp_servers,
                }
                if agent_config.model:
                    session_kwargs["model"] = agent_config.model
                session = await conn.new_session(**session_kwargs)
                await conn.prompt(
                    session_id=session.session_id,
                    prompt=[text_block(prompt)],
                )

            request_task = _create_lifecycle_task(
                lifecycle,
                _run_request(),
            )
            try:
                done, _ = await asyncio.wait(
                    {request_task},
                    timeout=agent_config.timeout_seconds,
                )
            except asyncio.CancelledError as exc:
                await _run_bounded_cleanup(
                    lifecycle,
                    request_task,
                    isolated_process_group=isolated_process_group,
                    cleanup_timeout_seconds=agent_config.cleanup_timeout_seconds,
                    force=True,
                    exit_error=exc,
                )
                raise

            if not done:
                timeout_error = TimeoutError(f"ACP request exceeded {agent_config.timeout_seconds} seconds")
                cancelled_while_cleaning, cleanup_failures = await _run_bounded_cleanup(
                    lifecycle,
                    request_task,
                    isolated_process_group=isolated_process_group,
                    cleanup_timeout_seconds=agent_config.cleanup_timeout_seconds,
                    force=True,
                    exit_error=timeout_error,
                )
                if cancelled_while_cleaning:
                    raise asyncio.CancelledError
                if cleanup_failures:
                    return _format_cleanup_error(
                        agent,
                        failures=cleanup_failures,
                        completed=False,
                        timed_out_after=agent_config.timeout_seconds,
                    )
                logger.error(
                    "ACP agent '%s' timed out after %s seconds; bounded subprocess cleanup completed",
                    agent,
                    agent_config.timeout_seconds,
                )
                return f"Error: ACP agent '{agent}' timed out after {agent_config.timeout_seconds} seconds. The subprocess was terminated. Increase acp_agents.{agent}.timeout_seconds only if this agent legitimately needs more time."

            try:
                request_task.result()
            except asyncio.CancelledError as exc:
                await _run_bounded_cleanup(
                    lifecycle,
                    request_task,
                    isolated_process_group=isolated_process_group,
                    cleanup_timeout_seconds=agent_config.cleanup_timeout_seconds,
                    force=True,
                    exit_error=exc,
                )
                raise
            except Exception as exc:
                cancelled_while_cleaning, cleanup_failures = await _run_bounded_cleanup(
                    lifecycle,
                    request_task,
                    isolated_process_group=isolated_process_group,
                    cleanup_timeout_seconds=agent_config.cleanup_timeout_seconds,
                    force=True,
                    exit_error=exc,
                )
                if cancelled_while_cleaning:
                    raise asyncio.CancelledError
                if cleanup_failures:
                    return _format_cleanup_error(
                        agent,
                        failures=cleanup_failures,
                        completed=False,
                    )
                logger.error(
                    "ACP agent '%s' invocation failed (%s)",
                    agent,
                    type(exc).__name__,
                )
                return _format_invocation_error(agent, cmd, exc)

            cancelled_while_cleaning, cleanup_failures = await _run_bounded_cleanup(
                lifecycle,
                request_task,
                isolated_process_group=isolated_process_group,
                cleanup_timeout_seconds=agent_config.cleanup_timeout_seconds,
                force=False,
                exit_error=None,
            )
            if cancelled_while_cleaning:
                raise asyncio.CancelledError
            if cleanup_failures:
                return _format_cleanup_error(
                    agent,
                    failures=cleanup_failures,
                    completed=True,
                )
            result = client.collected_text
            logger.info("ACP agent '%s' returned %d characters", agent, len(result))
            return result or "(no response)"
        except Exception as e:
            logger.error(
                "ACP agent '%s' invocation failed (%s)",
                agent,
                type(e).__name__,
            )
            return _format_invocation_error(agent, cmd, e)

    return StructuredTool.from_function(
        name="invoke_acp_agent",
        description=description,
        coroutine=_invoke,
        args_schema=_InvokeACPAgentInput,
    )
