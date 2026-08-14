"""Worker-owned runner for a durably approved Local host command.

The approval continuation never asks a model to reproduce the command.  The
app port atomically returns the owner-private frozen plan, this module remaps
its logical paths against the continuation Run's exact Local sandbox, launches
it once, persists the outcome, and only then supplies a bounded hidden receipt
to the Agent graph.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Callable, Mapping
from types import SimpleNamespace
from typing import Any, Protocol, runtime_checkable

from deerflow.file_authority import RunFileAuthority
from deerflow.runtime.context_keys import RuntimeContextKeys
from deerflow.runtime.host_execution_approval import (
    HOST_EXECUTION_AGENT_PATH_CONTEXT_KEY,
    HostExecutionContinuationPort,
    HostExecutionFrozenClaim,
    HostExecutionOutcome,
)
from deerflow.sandbox.local.local_sandbox import (
    LocalProcessSpawnAuthorizationFailed,
    LocalProcessSpawnDeadlineExpired,
)
from deerflow.sandbox.sandbox import check_authorization_boundary
from deerflow.sandbox.sandbox_provider import get_sandbox_provider
from deerflow.sandbox.security import (
    HostBashExecutionMode,
    resolve_host_bash_execution_mode,
)
from deerflow.sandbox.tools import (
    _prepare_local_host_execution,
    _truncate_bash_output,
    mask_local_paths_in_output,
)


class HostExecutionContinuationError(RuntimeError):
    """The frozen continuation could not be safely settled."""


@runtime_checkable
class HostExecutionFinalSpawnAuthorizationPort(Protocol):
    """App-owned final authority check immediately before process creation."""

    async def authorize_claimed_host_execution_spawn(
        self,
        approval_id: str,
    ) -> float | None:
        """Return a DB-derived immediate-spawn window, or deny with ``None``."""

        ...


_FINAL_SPAWN_AUTHORIZATION_TIMEOUT_SECONDS = 5.0


def _continuation_is_local_approval(app_config: object | None) -> bool:
    if app_config is None:
        return False
    try:
        return resolve_host_bash_execution_mode(app_config) is HostBashExecutionMode.LOCAL_APPROVAL_REQUIRED
    except Exception:
        return False


async def _complete_or_raise(
    port: HostExecutionContinuationPort,
    approval_id: str,
    outcome: HostExecutionOutcome,
) -> None:
    try:
        await port.complete_host_execution(approval_id, outcome)
    except Exception as error:
        # The app adapter marks the claimed execution unknown on its own
        # persistence failure.  Never return to a path that could spawn again.
        raise HostExecutionContinuationError(
            "Host execution completion could not be persisted",
        ) from error


async def _settle_before_spawn_failure(
    port: HostExecutionContinuationPort,
    approval_id: str,
    *,
    source_tool_call_id: str,
    agent_path: tuple[str, ...],
    reason_code: str,
) -> dict[str, object]:
    await _complete_or_raise(
        port,
        approval_id,
        HostExecutionOutcome(
            status="launch_failed",
            reason_code=reason_code,
        ),
    )
    return _hidden_failure_input(
        approval_id=approval_id,
        source_tool_call_id=source_tool_call_id,
        agent_path=agent_path,
        reason_code=reason_code,
    )


def _hidden_result_input(
    *,
    approval_id: str,
    source_tool_call_id: str,
    agent_path: tuple[str, ...],
    exit_code: int,
    result_text: str,
) -> dict[str, object]:
    model_receipt = json.dumps(
        {
            "schema_version": 1,
            "status": "finished",
            "exit_code": exit_code,
            "result": result_text,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "messages": [
            {
                "type": "human",
                "content": (f"The exact host command approved by the user has already been executed once by the Worker. Do not execute or retry that command. Continue from this trusted result:\n{model_receipt}"),
                "additional_kwargs": {
                    "hide_from_ui": True,
                    "host_execution_continuation": {
                        "schema_version": 1,
                        "approval_id": approval_id,
                        "source_tool_call_id": source_tool_call_id,
                        "agent_path": list(agent_path),
                        "status": "finished",
                        "exit_code": exit_code,
                    },
                },
            }
        ]
    }


def _hidden_failure_input(
    *,
    approval_id: str,
    source_tool_call_id: str,
    agent_path: tuple[str, ...],
    reason_code: str,
) -> dict[str, object]:
    return {
        "messages": [
            {
                "type": "human",
                "content": (f"The exact host command approved by the user was not launched. The approval has been consumed and must not be executed or retried. Explain this trusted failure result to the user: {reason_code}"),
                "additional_kwargs": {
                    "hide_from_ui": True,
                    "host_execution_continuation": {
                        "schema_version": 1,
                        "approval_id": approval_id,
                        "source_tool_call_id": source_tool_call_id,
                        "agent_path": list(agent_path),
                        "status": "launch_failed",
                        "reason_code": reason_code,
                    },
                },
            }
        ]
    }


def _replay_result_input(claim: HostExecutionFrozenClaim) -> dict[str, object]:
    approval_id = claim.approval_id
    plan = claim.plan
    outcome = claim.outcome
    if approval_id is None or plan is None or outcome is None:
        raise HostExecutionContinuationError(
            "Host execution continuation returned an invalid replay",
        )
    if outcome.status == "launch_failed":
        return _hidden_failure_input(
            approval_id=approval_id,
            source_tool_call_id=plan.source_tool_call_id,
            agent_path=plan.agent_path,
            reason_code=outcome.reason_code or "launch_failed",
        )
    if outcome.status != "finished" or outcome.exit_code is None:
        raise HostExecutionContinuationError(
            "Host execution continuation returned an invalid replay",
        )
    result_text = outcome.result_text
    if result_text is None:
        result_text = outcome.stdout or ""
        if outcome.stderr:
            result_text += f"\nStd Error:\n{outcome.stderr}" if result_text else outcome.stderr
        if not result_text:
            result_text = "(no output)"
    return _hidden_result_input(
        approval_id=approval_id,
        source_tool_call_id=plan.source_tool_call_id,
        agent_path=plan.agent_path,
        exit_code=outcome.exit_code,
        result_text=result_text,
    )


def _structured_execute(
    sandbox: object,
    *,
    effective_command: str,
    shell: str,
    timeout_seconds: int,
    thread_data: Mapping[str, object],
    prepared_base_env: Mapping[str, str],
    max_chars: int,
    spawn_authorization_guard: Callable[[], float],
) -> tuple[str, int, str, str]:
    execute = getattr(sandbox, "execute_prepared_command_result", None)
    if not callable(execute):
        raise RuntimeError("Local sandbox cannot execute a frozen command")
    result = execute(
        effective_command,
        shell=shell,
        # Secret-bearing plans currently fail closed before this point. The
        # verified Worker-local base environment is passed through unchanged;
        # LocalSandbox must not re-read process environment before spawning.
        env=None,
        prepared_base_env=prepared_base_env,
        timeout=timeout_seconds,
        spawn_authorization_guard=spawn_authorization_guard,
    )
    exit_code = getattr(result, "exit_code", None)
    output = getattr(result, "output", None)
    stdout = getattr(result, "stdout", None)
    stderr = getattr(result, "stderr", None)
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise RuntimeError("Local command returned no authoritative exit code")
    if not all(isinstance(value, str) for value in (output, stdout, stderr)):
        raise RuntimeError("Local command returned invalid structured output")

    def bounded(value: str) -> str:
        return _truncate_bash_output(
            mask_local_paths_in_output(value, thread_data),
            max_chars,
        )

    return bounded(output), exit_code, bounded(stdout), bounded(stderr)


async def execute_frozen_host_execution_continuation(
    *,
    approval_port: object | None,
    app_config: object | None,
    runtime_context: Mapping[str, Any],
    file_authority: RunFileAuthority | None,
    graph_input: dict,
    continuation_required: bool,
) -> dict:
    """Consume and execute a continuation plan before constructing the graph.

    ``continuation_required`` is derived from server-owned Run metadata.  It
    prevents a missing or incompatible app adapter from falling through to the
    legacy hidden prompt that asked the model to retry Bash.
    """

    if not isinstance(approval_port, HostExecutionContinuationPort):
        if continuation_required:
            raise HostExecutionContinuationError(
                "Host execution continuation authority is unavailable",
            )
        return graph_input

    try:
        claim = await approval_port.claim_frozen_host_execution()
    except Exception as error:
        raise HostExecutionContinuationError(
            "Host execution continuation could not be claimed",
        ) from error
    if not isinstance(claim, HostExecutionFrozenClaim):
        raise HostExecutionContinuationError(
            "Host execution continuation returned an invalid claim",
        )
    if claim.status == "not_applicable":
        if continuation_required:
            raise HostExecutionContinuationError(
                "Host execution continuation was not bound",
            )
        return graph_input
    if claim.status == "denied":
        raise HostExecutionContinuationError(
            "Host execution continuation was denied",
        )
    if claim.status == "replay":
        # A prior attempt durably completed before it crashed or lost its
        # stream. Receipt replay is input-only and must never touch a provider.
        return _replay_result_input(claim)

    approval_id = claim.approval_id
    frozen = claim.plan
    if approval_id is None or frozen is None:  # pragma: no cover - dataclass gate
        raise HostExecutionContinuationError(
            "Host execution continuation returned an invalid claim",
        )

    # Claim first, then settle every failure. This prevents an approved row
    # from remaining retryable if provider/configuration drift is detected.
    if not _continuation_is_local_approval(app_config):
        return await _settle_before_spawn_failure(
            approval_port,
            approval_id,
            source_tool_call_id=frozen.source_tool_call_id,
            agent_path=frozen.agent_path,
            reason_code="policy_drift",
        )
    if frozen.environment_keys:
        # Names alone cannot identify an exact Skill credential binding.  A
        # future contract may persist a secret-free binding closure; until then
        # silently rematerializing by name risks selecting different authority.
        return await _settle_before_spawn_failure(
            approval_port,
            approval_id,
            source_tool_call_id=frozen.source_tool_call_id,
            agent_path=frozen.agent_path,
            reason_code="environment_binding_unavailable",
        )
    current_thread_id = runtime_context.get("thread_id")
    if current_thread_id != frozen.source_thread_id:
        return await _settle_before_spawn_failure(
            approval_port,
            approval_id,
            source_tool_call_id=frozen.source_tool_call_id,
            agent_path=frozen.agent_path,
            reason_code="source_scope_mismatch",
        )
    if file_authority is None:
        return await _settle_before_spawn_failure(
            approval_port,
            approval_id,
            source_tool_call_id=frozen.source_tool_call_id,
            agent_path=frozen.agent_path,
            reason_code="private_file_authority_unavailable",
        )
    sandbox_id = getattr(file_authority, "sandbox_id", None)
    thread_data_method = getattr(file_authority, "thread_data_paths", None)
    if not isinstance(sandbox_id, str) or not sandbox_id.startswith("local-run:") or not callable(thread_data_method):
        return await _settle_before_spawn_failure(
            approval_port,
            approval_id,
            source_tool_call_id=frozen.source_tool_call_id,
            agent_path=frozen.agent_path,
            reason_code="local_sandbox_unavailable",
        )
    try:
        thread_data = thread_data_method()
        sandbox = get_sandbox_provider().get(sandbox_id)
    except Exception:
        thread_data = None
        sandbox = None
    if not isinstance(thread_data, dict) or sandbox is None:
        return await _settle_before_spawn_failure(
            approval_port,
            approval_id,
            source_tool_call_id=frozen.source_tool_call_id,
            agent_path=frozen.agent_path,
            reason_code="local_sandbox_unavailable",
        )

    # Rebase only the persisted logical command on the current private mount.
    # The frozen child path is restored because there is no child model/tool
    # invocation in this continuation.
    continuation_context = dict(runtime_context)
    continuation_context[HOST_EXECUTION_AGENT_PATH_CONTEXT_KEY] = frozen.agent_path
    # The continuation has no authority to select a channel identity. Rebuild
    # the exact source state frozen before approval, ignoring any missing or
    # conflicting value in continuation input/context.
    continuation_context.pop(RuntimeContextKeys.CHANNEL_USER_ID, None)
    if frozen.channel_identity_mode == "set":
        continuation_context[RuntimeContextKeys.CHANNEL_USER_ID] = frozen.channel_user_id
    elif frozen.channel_identity_mode == "unset":
        continuation_context[RuntimeContextKeys.CHANNEL_USER_ID] = None
    synthetic_runtime = SimpleNamespace(
        context=continuation_context,
        config={},
        state={
            "sandbox": {
                "sandbox_id": sandbox_id,
                "run_id": runtime_context.get("run_id"),
            },
            "thread_data": thread_data,
        },
        tool_call_id=frozen.source_tool_call_id,
    )
    try:
        rebound, max_chars = _prepare_local_host_execution(
            synthetic_runtime,
            sandbox,
            description=frozen.description,
            requested_command=frozen.requested_command,
        )
    except Exception:
        return await _settle_before_spawn_failure(
            approval_port,
            approval_id,
            source_tool_call_id=frozen.source_tool_call_id,
            agent_path=frozen.agent_path,
            reason_code="plan_rebase_failed",
        )
    if rebound.execution_digest != frozen.execution_digest:
        return await _settle_before_spawn_failure(
            approval_port,
            approval_id,
            source_tool_call_id=frozen.source_tool_call_id,
            agent_path=frozen.agent_path,
            reason_code="plan_drift",
        )

    try:
        prepared_base_env = approval_port.prepare_host_execution_environment()
    except Exception:
        prepared_base_env = None
    if prepared_base_env is None:
        return await _settle_before_spawn_failure(
            approval_port,
            approval_id,
            source_tool_call_id=frozen.source_tool_call_id,
            agent_path=frozen.agent_path,
            reason_code="host_environment_drift",
        )

    try:
        await check_authorization_boundary(
            runtime_context,
            "before_sandbox_exec",
        )
    except Exception:
        return await _settle_before_spawn_failure(
            approval_port,
            approval_id,
            source_tool_call_id=frozen.source_tool_call_id,
            agent_path=frozen.agent_path,
            reason_code="pre_spawn_authorization_failed",
        )

    if not isinstance(
        approval_port,
        HostExecutionFinalSpawnAuthorizationPort,
    ):
        return await _settle_before_spawn_failure(
            approval_port,
            approval_id,
            source_tool_call_id=frozen.source_tool_call_id,
            agent_path=frozen.agent_path,
            reason_code="pre_spawn_authorization_failed",
        )
    owner_loop = asyncio.get_running_loop()

    def authorize_spawn_in_owner_loop() -> float:
        # This synchronous callback is consumed inside LocalSandbox directly
        # before process creation. Successful authorization is the
        # linearization point for exactly one immediate spawn: there is no
        # queue, await, retry, or caller-controlled work between this return,
        # the monotonic deadline check, and subprocess.run/Popen.
        authorization_started_monotonic = time.monotonic()
        try:
            authorization = asyncio.run_coroutine_threadsafe(
                approval_port.authorize_claimed_host_execution_spawn(
                    approval_id,
                ),
                owner_loop,
            )
        except Exception as error:
            raise LocalProcessSpawnAuthorizationFailed from error
        try:
            spawn_window_seconds = authorization.result(
                timeout=_FINAL_SPAWN_AUTHORIZATION_TIMEOUT_SECONDS,
            )
        except Exception as error:
            authorization.cancel()
            raise LocalProcessSpawnAuthorizationFailed from error
        if isinstance(spawn_window_seconds, bool) or not isinstance(spawn_window_seconds, (int, float)) or not math.isfinite(spawn_window_seconds) or spawn_window_seconds <= 0:
            raise LocalProcessSpawnAuthorizationFailed
        spawn_deadline_monotonic = authorization_started_monotonic + float(spawn_window_seconds)
        if time.monotonic() >= spawn_deadline_monotonic:
            raise LocalProcessSpawnDeadlineExpired
        return spawn_deadline_monotonic

    try:
        output, exit_code, stdout, stderr = await asyncio.to_thread(
            _structured_execute,
            sandbox,
            effective_command=rebound.effective_command,
            shell=rebound.shell,
            timeout_seconds=rebound.timeout_seconds,
            thread_data=thread_data,
            prepared_base_env=prepared_base_env,
            max_chars=max_chars,
            spawn_authorization_guard=authorize_spawn_in_owner_loop,
        )
    except (
        LocalProcessSpawnAuthorizationFailed,
        LocalProcessSpawnDeadlineExpired,
    ):
        return await _settle_before_spawn_failure(
            approval_port,
            approval_id,
            source_tool_call_id=frozen.source_tool_call_id,
            agent_path=frozen.agent_path,
            reason_code="pre_spawn_authorization_failed",
        )
    except Exception as error:
        await _complete_or_raise(
            approval_port,
            approval_id,
            HostExecutionOutcome(
                status="unknown",
                reason_code="process_outcome_unknown",
            ),
        )
        raise HostExecutionContinuationError(
            "Approved host execution outcome is unknown",
        ) from error

    await _complete_or_raise(
        approval_port,
        approval_id,
        HostExecutionOutcome(
            status="finished",
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            result_text=output,
        ),
    )
    return _hidden_result_input(
        approval_id=approval_id,
        source_tool_call_id=frozen.source_tool_call_id,
        agent_path=frozen.agent_path,
        exit_code=exit_code,
        result_text=output,
    )


__all__ = [
    "HostExecutionContinuationError",
    "HostExecutionFinalSpawnAuthorizationPort",
    "execute_frozen_host_execution_continuation",
]
