import asyncio
import threading
from collections.abc import Callable, Mapping

from deerflow.agents.thread_state import ThreadDataState
from deerflow.file_authority import require_private_file_authority
from deerflow.runtime.secret_context import (
    ACTIVE_SECRETS_CONTEXT_KEY,
    SKILL_SECRET_EXEC_READY_CONTEXT_KEY,
    SKILL_SECRET_PROVIDER_CONTEXT_KEY,
    active_provider_secret_request,
    resolve_provider_active_secrets,
)
from deerflow.runtime.user_context import resolve_runtime_user_id
from deerflow.sandbox.exceptions import (
    SandboxError,
    SandboxNotFoundError,
    SandboxRuntimeError,
)
from deerflow.sandbox.overwrite import unwrap_sandbox
from deerflow.sandbox.sandbox import Sandbox
from deerflow.sandbox.sandbox_provider import (
    RunScopedReadOnlyMount,
    get_sandbox_provider,
)
from deerflow.sandbox.tooling.path_mapping import mask_local_paths_in_output
from deerflow.tools.types import Runtime

__all__ = [
    "ensure_sandbox_initialized",
    "ensure_sandbox_initialized_async",
    "ensure_thread_directories_exist",
    "get_thread_data",
    "is_local_sandbox",
    "sandbox_from_runtime",
]


def _sanitize_error(error: Exception, runtime: Runtime | None = None) -> str:
    """Sanitize an error message to avoid leaking host filesystem paths.

    In local-sandbox mode, resolved host paths in the error string are masked
    back to their virtual equivalents so that user-visible output never exposes
    the host directory layout.
    """
    msg = f"{type(error).__name__}: {error}"
    if runtime is not None and is_local_sandbox(runtime):
        thread_data = get_thread_data(runtime)
        msg = mask_local_paths_in_output(msg, thread_data)
    return msg


def get_thread_data(runtime: Runtime | None) -> ThreadDataState | None:
    """Extract thread_data from runtime state."""
    if runtime is None:
        return None
    if runtime.state is None:
        return None
    return runtime.state.get("thread_data")


def is_local_sandbox(runtime: Runtime | None) -> bool:
    """Check if the current sandbox is a local sandbox.

    Accepts the generic id ``"local"`` (acquire with no thread context), the
    per-thread ``"local:{user_id}:{thread_id}"`` format, and the trusted
    per-run ``"local-run:{user_id}:{thread_id}:{run_id}"`` format.
    """
    if runtime is None:
        return False
    if runtime.state is None:
        return False
    sandbox_state, _ = unwrap_sandbox(runtime.state.get("sandbox"))
    if sandbox_state is None:
        return False
    sandbox_id = sandbox_state.get("sandbox_id")
    if not isinstance(sandbox_id, str):
        return False
    return sandbox_id == "local" or sandbox_id.startswith("local:") or sandbox_id.startswith("local-run:")


def sandbox_from_runtime(
    runtime: Runtime | None = None,
    *,
    state: Mapping[str, object] | None = None,
) -> Sandbox:
    """Extract sandbox instance from tool runtime.

    DEPRECATED: Use ensure_sandbox_initialized() for lazy initialization support.
    This function assumes sandbox is already initialized and will raise error if not.
    Model-call middleware receives a plain ``langgraph.runtime.Runtime``, which
    intentionally has no ``state`` attribute, so those callers must pass their
    explicit ``ModelRequest.state`` through ``state``. Tool callers continue to
    use ``ToolRuntime.state`` when the explicit argument is absent.

    Raises:
        SandboxRuntimeError: If runtime is not available or sandbox state is missing.
        SandboxNotFoundError: If sandbox with the given ID cannot be found.
    """
    if runtime is None:
        raise SandboxRuntimeError("Tool runtime not available")
    runtime_state = state
    if runtime_state is None:
        runtime_state = getattr(runtime, "state", None)
    if runtime_state is None:
        raise SandboxRuntimeError("Tool runtime state not available")
    sandbox_state, _ = unwrap_sandbox(runtime_state.get("sandbox"))
    if sandbox_state is None:
        raise SandboxRuntimeError("Sandbox state not initialized in runtime")
    sandbox_id = sandbox_state.get("sandbox_id")
    if sandbox_id is None:
        raise SandboxRuntimeError("Sandbox ID not found in state")
    sandbox = get_sandbox_provider().get(sandbox_id)
    if sandbox is None:
        raise SandboxNotFoundError(f"Sandbox with ID '{sandbox_id}' not found", sandbox_id=sandbox_id)

    if runtime.context is not None:
        runtime.context["sandbox_id"] = sandbox_id  # Ensure sandbox_id is in context for downstream use
    return sandbox


def ensure_sandbox_initialized(runtime: Runtime | None = None) -> Sandbox:
    """Ensure sandbox is initialized, acquiring lazily if needed.

    On first call, acquires a sandbox from the provider and stores it in runtime state.
    Subsequent calls return the existing sandbox.

    Thread-safety is guaranteed by the provider's internal locking mechanism.

    Args:
        runtime: Tool runtime containing state and context.

    Returns:
        Initialized sandbox instance.

    Raises:
        SandboxRuntimeError: If runtime is not available or thread_id is missing.
        SandboxNotFoundError: If sandbox acquisition fails.
    """
    if runtime is None:
        raise SandboxRuntimeError("Tool runtime not available")

    if runtime.state is None:
        raise SandboxRuntimeError("Tool runtime state not available")

    raw_mounts = (runtime.context or {}).get("__run_read_only_mounts", ())
    mounts = raw_mounts if isinstance(raw_mounts, tuple) and all(type(item) is RunScopedReadOnlyMount for item in raw_mounts) else ()

    # A checkpointed legacy sandbox is never authoritative for a private run.
    # Exact mounts force a fresh run-scoped acquisition and overwrite state.
    sandbox_state, _ = unwrap_sandbox(runtime.state.get("sandbox"))
    if not mounts and sandbox_state is not None:
        sandbox_id = sandbox_state.get("sandbox_id")
        if sandbox_id is not None:
            sandbox = get_sandbox_provider().get(sandbox_id)
            if sandbox is not None:
                if runtime.context is not None:
                    runtime.context["sandbox_id"] = sandbox_id  # Ensure sandbox_id is in context for releasing in after_agent
                return sandbox
            # Sandbox was released, fall through to acquire new one

    # Lazy acquisition: get thread_id and acquire sandbox
    thread_id = runtime.context.get("thread_id") if runtime.context else None
    if thread_id is None:
        thread_id = runtime.config.get("configurable", {}).get("thread_id") if runtime.config else None
    if thread_id is None:
        raise SandboxRuntimeError("Thread ID not available in runtime context")

    provider = get_sandbox_provider()
    sandbox_id = (
        provider.acquire_with_mounts(
            thread_id,
            user_id=resolve_runtime_user_id(runtime),
            mounts=mounts,
        )
        if mounts
        else provider.acquire(thread_id, user_id=resolve_runtime_user_id(runtime))
    )

    # Update runtime state - this persists across tool calls
    runtime.state["sandbox"] = {"sandbox_id": sandbox_id}

    # Retrieve and return the sandbox
    sandbox = provider.get(sandbox_id)
    if sandbox is None:
        raise SandboxNotFoundError("Sandbox not found after acquisition", sandbox_id=sandbox_id)

    if runtime.context is not None:
        runtime.context["sandbox_id"] = sandbox_id  # Ensure sandbox_id is in context for releasing in after_agent
    return sandbox


async def ensure_sandbox_initialized_async(runtime: Runtime | None = None) -> Sandbox:
    """Async counterpart to ``ensure_sandbox_initialized`` for tool runtimes.

    This keeps lazy sandbox acquisition on the async provider hook, so AIO
    sandbox startup and readiness polling do not fall back to synchronous
    ``provider.acquire()`` during async tool execution.
    """
    if runtime is None:
        raise SandboxRuntimeError("Tool runtime not available")

    if runtime.state is None:
        raise SandboxRuntimeError("Tool runtime state not available")

    raw_mounts = (runtime.context or {}).get("__run_read_only_mounts", ())
    mounts = raw_mounts if isinstance(raw_mounts, tuple) and all(type(item) is RunScopedReadOnlyMount for item in raw_mounts) else ()

    sandbox_state, _ = unwrap_sandbox(runtime.state.get("sandbox"))
    if not mounts and sandbox_state is not None:
        sandbox_id = sandbox_state.get("sandbox_id")
        if sandbox_id is not None:
            sandbox = get_sandbox_provider().get(sandbox_id)
            if sandbox is not None:
                if runtime.context is not None:
                    runtime.context["sandbox_id"] = sandbox_id
                return sandbox

    thread_id = runtime.context.get("thread_id") if runtime.context else None
    if thread_id is None:
        thread_id = runtime.config.get("configurable", {}).get("thread_id") if runtime.config else None
    if thread_id is None:
        raise SandboxRuntimeError("Thread ID not available in runtime context")

    provider = get_sandbox_provider()
    sandbox_id = (
        await provider.acquire_with_mounts_async(
            thread_id,
            user_id=resolve_runtime_user_id(runtime),
            mounts=mounts,
        )
        if mounts
        else await provider.acquire_async(
            thread_id,
            user_id=resolve_runtime_user_id(runtime),
        )
    )

    runtime.state["sandbox"] = {"sandbox_id": sandbox_id}

    sandbox = provider.get(sandbox_id)
    if sandbox is None:
        raise SandboxNotFoundError("Sandbox not found after acquisition", sandbox_id=sandbox_id)

    if runtime.context is not None:
        runtime.context["sandbox_id"] = sandbox_id
    return sandbox


async def _run_sync_tool_after_async_sandbox_init(
    func: Callable[..., str] | None,
    runtime: Runtime,
    *args: object,
    authorization_operation: str | None = None,
) -> str:
    """Initialize lazily via async provider, then run sync tool body off-thread."""
    try:
        await ensure_sandbox_initialized_async(runtime)
    except SandboxError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: Unexpected error initializing sandbox: {_sanitize_error(e, runtime)}"

    if func is None:
        return "Error: Tool implementation not available"

    if authorization_operation is not None:
        from deerflow.sandbox.sandbox import check_authorization_boundary

        await check_authorization_boundary(
            getattr(runtime, "context", None),
            authorization_operation,
        )

    context = getattr(runtime, "context", None)
    private_skill_provider = context.get(SKILL_SECRET_PROVIDER_CONTEXT_KEY) if (authorization_operation == "before_sandbox_exec" and isinstance(context, dict) and "private_scope" in context) else None
    if callable(private_skill_provider):
        # Each command gets an isolated context overlay. Parallel bash calls
        # must never clear or replace one another's ready marker or carrier.
        call_context = dict(context)
        call_context.pop(ACTIVE_SECRETS_CONTEXT_KEY, None)
        requested = active_provider_secret_request(call_context)
        call_runtime = _RuntimeContextOverlay(runtime, call_context)
        owner_loop = asyncio.get_running_loop()
        cancellation_requested = threading.Event()

        def invoke_with_fresh_skill_secrets() -> str:
            fresh_scoped: object = None
            active: dict[str, str] = {}
            try:
                if cancellation_requested.is_set():
                    return "Error: Skill secret material is unavailable"
                if requested:
                    try:
                        materialization = asyncio.run_coroutine_threadsafe(
                            private_skill_provider(requested),
                            owner_loop,
                        )
                    except Exception:
                        return "Error: Skill secret material is unavailable"
                    try:
                        fresh_scoped = materialization.result()
                    except Exception:
                        materialization.cancel()
                        return "Error: Skill secret material is unavailable"
                    if not isinstance(fresh_scoped, dict) or set(fresh_scoped) != set(requested) or any(not isinstance(values, dict) for values in fresh_scoped.values()):
                        return "Error: Skill secret material is unavailable"
                    active = resolve_provider_active_secrets(
                        call_context,
                        fresh_scoped,
                    )
                if cancellation_requested.is_set():
                    return "Error: Skill secret material is unavailable"
                if active:
                    call_context[ACTIVE_SECRETS_CONTEXT_KEY] = active
                call_context[SKILL_SECRET_EXEC_READY_CONTEXT_KEY] = True
                return func(call_runtime, *args)
            finally:
                call_context.pop(SKILL_SECRET_EXEC_READY_CONTEXT_KEY, None)
                call_context.pop(ACTIVE_SECRETS_CONTEXT_KEY, None)
                active.clear()
                if isinstance(fresh_scoped, dict):
                    for values in fresh_scoped.values():
                        if isinstance(values, dict):
                            values.clear()
                    fresh_scoped.clear()

        try:
            return await asyncio.to_thread(invoke_with_fresh_skill_secrets)
        finally:
            cancellation_requested.set()

    return await asyncio.to_thread(func, runtime, *args)


class _RuntimeContextOverlay:
    """Delegate Runtime state/config while replacing only per-call context."""

    def __init__(self, target: Runtime, context: dict) -> None:
        self._target = target
        self.context = context

    def __getattr__(self, name: str):
        return getattr(self._target, name)


def ensure_thread_directories_exist(runtime: Runtime | None) -> None:
    """Ensure thread data directories (workspace, uploads, outputs) exist.

    This function is called lazily when any sandbox tool is first used.
    For local sandbox, it creates the directories on the filesystem.
    For other sandboxes (like aio), directories are already mounted in the container.

    Args:
        runtime: Tool runtime containing state and context.
    """
    if runtime is None:
        return

    # Project-private runs restore their sandbox projection before the agent
    # starts.  Their thread-data paths intentionally remain container paths
    # (``/mnt/user-data/...``), even for LocalSandboxProvider, because the
    # provider owns the host mapping.  Creating those paths on the gateway
    # host would bypass that mapping and fails on hosts where ``/mnt`` is
    # read-only (for example macOS).
    if require_private_file_authority(runtime.context or {}) is not None:
        return

    # Only create directories for local sandbox
    if not is_local_sandbox(runtime):
        return

    thread_data = get_thread_data(runtime)
    if thread_data is None:
        return

    # Check if directories have already been created
    if runtime.state.get("thread_directories_created"):
        return

    # Create the three directories
    import os

    for key in ["workspace_path", "uploads_path", "outputs_path"]:
        path = thread_data.get(key)
        if path:
            os.makedirs(path, exist_ok=True)

    # Mark as created to avoid redundant operations
    runtime.state["thread_directories_created"] = True
