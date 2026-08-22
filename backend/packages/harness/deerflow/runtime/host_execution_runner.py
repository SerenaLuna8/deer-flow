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
import threading
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
    HostExecutionOutputDeliveryPort,
    HostExecutionPlan,
    HostExecutionRetrySafetyFencePort,
)
from deerflow.runtime.secret_context import (
    ACTIVE_SECRET_SOURCES_CONTEXT_KEY,
    ACTIVE_SECRETS_CONTEXT_KEY,
    SECRETS_CONTEXT_KEY,
    SKILL_SECRET_PROVIDER_CONTEXT_KEY,
    resolve_provider_active_secrets,
)
from deerflow.sandbox.local.local_sandbox import (
    LocalProcessSpawnAuthorizationFailed,
    LocalProcessSpawnDeadlineExpired,
)
from deerflow.sandbox.sandbox import (
    AuthorizationBoundaryFenceUncertain,
    check_authorization_boundary,
    resolve_authorization_boundary_fence,
)
from deerflow.sandbox.sandbox_provider import get_sandbox_provider
from deerflow.sandbox.security import (
    HostBashExecutionMode,
    resolve_host_bash_execution_mode,
)
from deerflow.sandbox.tools import (
    _prepare_local_host_execution,
    _truncate_bash_output,
    mask_local_paths_in_output,
    mask_secret_values,
)


class HostExecutionContinuationError(RuntimeError):
    """The frozen continuation could not be safely settled."""


class _HostExecutionEnvironmentBindingUnavailable(RuntimeError):
    """The exact per-command Skill secret closure could not be refreshed."""


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
    *,
    runtime_context: Mapping[str, Any] | None = None,
    retry_safety_fence: object | None = None,
) -> None:
    try:
        if (
            outcome.status in {"finished", "launch_failed"}
            and retry_safety_fence is not None
            and isinstance(
                port,
                HostExecutionRetrySafetyFencePort,
            )
        ):
            await port.complete_host_execution_with_retry_safety_fence(
                approval_id,
                outcome,
                retry_safety_fence,
            )
            retry_safety_fence = None
        else:
            await port.complete_host_execution(approval_id, outcome)
    except Exception as error:
        # The app adapter marks the claimed execution unknown on its own
        # persistence failure.  Never return to a path that could spawn again.
        raise HostExecutionContinuationError(
            "Host execution completion could not be persisted",
        ) from error
    if outcome.status in {"finished", "launch_failed"}:
        try:
            await resolve_authorization_boundary_fence(
                runtime_context,
                "resolve_sandbox_exec_fence",
                retry_safety_fence,
            )
        except Exception as error:
            # The durable receipt prevents another spawn, but an unresolved
            # local fence must still fail closed rather than erasing a newer
            # or unrelated ambiguous side effect.
            raise HostExecutionContinuationError(
                "Host execution retry-safety fence could not be resolved",
            ) from error


async def _output_delivery_requirement_paths(
    port: HostExecutionContinuationPort,
) -> tuple[str, ...]:
    if not isinstance(port, HostExecutionOutputDeliveryPort):
        return ()
    try:
        paths = await port.output_delivery_requirement_paths()
    except Exception as error:
        raise HostExecutionContinuationError(
            "Output delivery requirement could not be loaded",
        ) from error
    if type(paths) is not tuple or len(paths) > 256:
        raise HostExecutionContinuationError(
            "Output delivery requirement is invalid",
        )
    if any(not _valid_output_delivery_path(path) for path in paths):
        raise HostExecutionContinuationError(
            "Output delivery requirement is invalid",
        )
    return tuple(dict.fromkeys(paths))


def _valid_output_delivery_path(path: object) -> bool:
    prefix = "/mnt/user-data/outputs/"
    return bool(type(path) is str and path.startswith(prefix) and len(path) > len(prefix) and "\\" not in path and "//" not in path and all(part not in {"", ".", ".."} for part in path.split("/")[4:]))


async def _settle_before_spawn_failure(
    port: HostExecutionContinuationPort,
    approval_id: str,
    *,
    source_tool_call_id: str,
    agent_path: tuple[str, ...],
    reason_code: str,
    runtime_context: Mapping[str, Any] | None = None,
    retry_safety_fence: object | None = None,
) -> dict[str, object]:
    await _complete_or_raise(
        port,
        approval_id,
        HostExecutionOutcome(
            status="launch_failed",
            reason_code=reason_code,
        ),
        runtime_context=runtime_context,
        retry_safety_fence=retry_safety_fence,
    )
    required_output_paths = await _output_delivery_requirement_paths(port)
    return _hidden_failure_input(
        approval_id=approval_id,
        source_tool_call_id=source_tool_call_id,
        agent_path=agent_path,
        reason_code=reason_code,
        required_output_paths=required_output_paths,
    )


def _hidden_result_input(
    *,
    approval_id: str,
    source_tool_call_id: str,
    agent_path: tuple[str, ...],
    exit_code: int,
    result_text: str,
    required_output_paths: tuple[str, ...] = (),
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
    delivery_instruction = _output_delivery_instruction(
        required_output_paths,
    )
    return {
        "messages": [
            {
                "type": "human",
                "content": (f"The exact host command approved by the user has already been executed once by the Worker. Do not execute or retry that command. Continue from this trusted result:\n{model_receipt}{delivery_instruction}"),
                "additional_kwargs": {
                    "hide_from_ui": True,
                    "host_execution_continuation": {
                        "schema_version": 1,
                        "approval_id": approval_id,
                        "source_tool_call_id": source_tool_call_id,
                        "agent_path": list(agent_path),
                        "status": "finished",
                        "exit_code": exit_code,
                        "required_output_paths": list(required_output_paths),
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
    required_output_paths: tuple[str, ...] = (),
) -> dict[str, object]:
    delivery_instruction = _output_delivery_instruction(
        required_output_paths,
    )
    message = " ".join(
        (
            "The exact host command approved by the user was not launched.",
            "The approval has been consumed and must not be executed or retried.",
            f"Explain this trusted failure result to the user: {reason_code}",
        )
    )
    return {
        "messages": [
            {
                "type": "human",
                "content": f"{message}{delivery_instruction}",
                "additional_kwargs": {
                    "hide_from_ui": True,
                    "host_execution_continuation": {
                        "schema_version": 1,
                        "approval_id": approval_id,
                        "source_tool_call_id": source_tool_call_id,
                        "agent_path": list(agent_path),
                        "status": "launch_failed",
                        "reason_code": reason_code,
                        "required_output_paths": list(required_output_paths),
                    },
                },
            }
        ]
    }


def _output_delivery_instruction(
    required_output_paths: tuple[str, ...],
) -> str:
    if not required_output_paths:
        return ""
    encoded = json.dumps(
        list(required_output_paths),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "\n" + " ".join(
        (
            "Before your final response, you must call present_files with at least one of these server-owned pending output paths:",
            f"{encoded}.",
            "This delivery requirement does not authorize rerunning the command.",
        )
    )


def _replay_result_input(
    claim: HostExecutionFrozenClaim,
    *,
    required_output_paths: tuple[str, ...] = (),
) -> dict[str, object]:
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
            required_output_paths=required_output_paths,
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
        required_output_paths=required_output_paths,
    )


def _structured_execute(
    sandbox: object,
    *,
    effective_command: str,
    shell: str,
    timeout_seconds: int,
    thread_data: Mapping[str, object],
    prepared_base_env: Mapping[str, str],
    injected_env_provider: Callable[[], dict[str, str]],
    cancellation_requested: threading.Event,
    max_chars: int,
    spawn_authorization_guard: Callable[[], float],
) -> tuple[str, int, str, str]:
    execute = getattr(sandbox, "execute_prepared_command_result", None)
    if not callable(execute):
        raise RuntimeError("Local sandbox cannot execute a frozen command")
    injected_env: dict[str, str] = {}
    try:
        if cancellation_requested.is_set():
            raise LocalProcessSpawnAuthorizationFailed
        injected_env = injected_env_provider()
        if cancellation_requested.is_set():
            raise LocalProcessSpawnAuthorizationFailed

        def guarded_spawn_authorization() -> float:
            if cancellation_requested.is_set():
                raise LocalProcessSpawnAuthorizationFailed
            deadline = spawn_authorization_guard()
            if cancellation_requested.is_set():
                raise LocalProcessSpawnAuthorizationFailed
            return deadline

        result = execute(
            effective_command,
            shell=shell,
            # Exact Skill values are a one-command overlay materialized only
            # after this executor slot is running. The verified Worker-local
            # base environment is passed through unchanged; LocalSandbox must
            # not re-read process environment before spawning.
            env=injected_env or None,
            prepared_base_env=prepared_base_env,
            timeout=timeout_seconds,
            spawn_authorization_guard=guarded_spawn_authorization,
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
                mask_secret_values(
                    mask_local_paths_in_output(value, thread_data),
                    injected_env,
                ),
                max_chars,
            )

        return bounded(output), exit_code, bounded(stdout), bounded(stderr)
    finally:
        injected_env.clear()


def _frozen_skill_secret_request(
    frozen: HostExecutionPlan,
) -> (
    tuple[
        dict[str, frozenset[str]],
        tuple[
            tuple[str, str, tuple[tuple[str, str], ...], bool],
            ...,
        ],
    ]
    | None
):
    """Validate and rebuild the secret-free activation plan for one command."""

    if frozen.legacy_environment_keys:
        return None
    expected_targets: set[str] = set()
    bindings_by_path: dict[str, tuple[tuple[str, str], ...]] = {}
    requested: dict[str, set[str]] = {}
    activation_sources: list[tuple[str, str, tuple[tuple[str, str], ...], bool]] = []
    for source in frozen.skill_secret_sources:
        prior = bindings_by_path.setdefault(
            source.skill_path,
            source.secret_bindings,
        )
        if prior != source.secret_bindings:
            return None
        expected_targets.update(source.target_envs)
        requested.setdefault(source.skill_path, set()).update(
            source.secret_names,
        )
        activation_sources.append(
            (
                "frozen-skill",
                source.skill_path,
                source.secret_bindings,
                source.explicit,
            ),
        )
    if frozen.environment_keys != tuple(sorted(expected_targets)):
        # v2 approvals, request-scoped secrets, and GitHub token carriers have
        # names but no exact Skill source closure. They remain fail closed.
        return None
    return (
        {path: frozenset(names) for path, names in requested.items()},
        tuple(activation_sources),
    )


def _clear_scoped_secret_carrier(carrier: object) -> None:
    if not isinstance(carrier, dict):
        return
    for values in carrier.values():
        if isinstance(values, dict):
            values.clear()
    carrier.clear()


async def _materialize_frozen_skill_secrets(
    runtime_context: Mapping[str, Any],
    frozen: HostExecutionPlan,
) -> dict[str, str] | None:
    prepared = _frozen_skill_secret_request(frozen)
    if prepared is None:
        return None
    requested, activation_sources = prepared
    if not requested:
        return {}
    if "private_scope" not in runtime_context:
        return None
    provider = runtime_context.get(SKILL_SECRET_PROVIDER_CONTEXT_KEY)
    if not callable(provider):
        return None
    fresh_scoped: object = None
    try:
        fresh_scoped = await provider(requested)
        if not isinstance(fresh_scoped, dict) or set(fresh_scoped) != set(
            requested,
        ):
            return None
        if any(not isinstance(values, dict) for values in fresh_scoped.values()):
            return None
        selection_context = {
            ACTIVE_SECRET_SOURCES_CONTEXT_KEY: activation_sources,
        }
        return resolve_provider_active_secrets(
            selection_context,
            fresh_scoped,
        )
    except Exception:
        return None
    finally:
        _clear_scoped_secret_carrier(fresh_scoped)


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
        required_output_paths = await _output_delivery_requirement_paths(
            approval_port,
        )
        return _replay_result_input(
            claim,
            required_output_paths=required_output_paths,
        )

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
    if _frozen_skill_secret_request(frozen) is None:
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
    # Rebuild the exact secret-free source plan captured before approval.
    # Continuation input cannot contribute request/GitHub values or a different
    # activation selection to the execution digest.
    continuation_context.pop(SECRETS_CONTEXT_KEY, None)
    continuation_context.pop(ACTIVE_SECRETS_CONTEXT_KEY, None)
    continuation_context.pop("github_token", None)
    if frozen.skill_secret_sources:
        continuation_context[ACTIVE_SECRET_SOURCES_CONTEXT_KEY] = tuple(
            (
                "frozen-skill",
                source.skill_path,
                source.secret_bindings,
                source.explicit,
            )
            for source in frozen.skill_secret_sources
        )
    else:
        continuation_context.pop(ACTIVE_SECRET_SOURCES_CONTEXT_KEY, None)
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
    if rebound.execution_digest_for_schema(frozen.schema_version) != frozen.execution_digest:
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

    retry_safety_fence: object | None = None
    try:
        retry_safety_fence = await check_authorization_boundary(
            runtime_context,
            "before_sandbox_exec",
        )
    except AuthorizationBoundaryFenceUncertain as error:
        return await _settle_before_spawn_failure(
            approval_port,
            approval_id,
            source_tool_call_id=frozen.source_tool_call_id,
            agent_path=frozen.agent_path,
            reason_code="pre_spawn_authorization_failed",
            runtime_context=runtime_context,
            retry_safety_fence=error.fence,
        )
    except Exception:
        return await _settle_before_spawn_failure(
            approval_port,
            approval_id,
            source_tool_call_id=frozen.source_tool_call_id,
            agent_path=frozen.agent_path,
            reason_code="pre_spawn_authorization_failed",
            runtime_context=runtime_context,
            retry_safety_fence=retry_safety_fence,
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
            runtime_context=runtime_context,
            retry_safety_fence=retry_safety_fence,
        )

    owner_loop = asyncio.get_running_loop()

    def materialize_environment_in_owner_loop() -> dict[str, str]:
        try:
            materialization = asyncio.run_coroutine_threadsafe(
                _materialize_frozen_skill_secrets(
                    runtime_context,
                    frozen,
                ),
                owner_loop,
            )
        except Exception:
            raise _HostExecutionEnvironmentBindingUnavailable from None
        try:
            injected = materialization.result()
        except Exception:
            materialization.cancel()
            raise _HostExecutionEnvironmentBindingUnavailable from None
        if injected is None:
            raise _HostExecutionEnvironmentBindingUnavailable
        return injected

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

    cancellation_requested = threading.Event()
    try:
        output, exit_code, stdout, stderr = await asyncio.to_thread(
            _structured_execute,
            sandbox,
            effective_command=rebound.effective_command,
            shell=rebound.shell,
            timeout_seconds=rebound.timeout_seconds,
            thread_data=thread_data,
            prepared_base_env=prepared_base_env,
            injected_env_provider=materialize_environment_in_owner_loop,
            cancellation_requested=cancellation_requested,
            max_chars=max_chars,
            spawn_authorization_guard=authorize_spawn_in_owner_loop,
        )
    except _HostExecutionEnvironmentBindingUnavailable:
        return await _settle_before_spawn_failure(
            approval_port,
            approval_id,
            source_tool_call_id=frozen.source_tool_call_id,
            agent_path=frozen.agent_path,
            reason_code="environment_binding_unavailable",
            runtime_context=runtime_context,
            retry_safety_fence=retry_safety_fence,
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
            runtime_context=runtime_context,
            retry_safety_fence=retry_safety_fence,
        )
    except Exception:
        await _complete_or_raise(
            approval_port,
            approval_id,
            HostExecutionOutcome(
                status="unknown",
                reason_code="process_outcome_unknown",
            ),
            runtime_context=runtime_context,
            retry_safety_fence=retry_safety_fence,
        )
        raise HostExecutionContinuationError(
            "Approved host execution outcome is unknown",
        ) from None
    finally:
        cancellation_requested.set()

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
        runtime_context=runtime_context,
        retry_safety_fence=retry_safety_fence,
    )
    required_output_paths = await _output_delivery_requirement_paths(
        approval_port,
    )
    return _hidden_result_input(
        approval_id=approval_id,
        source_tool_call_id=frozen.source_tool_call_id,
        agent_path=frozen.agent_path,
        exit_code=exit_code,
        result_text=output,
        required_output_paths=required_output_paths,
    )


__all__ = [
    "HostExecutionContinuationError",
    "HostExecutionFinalSpawnAuthorizationPort",
    "execute_frozen_host_execution_continuation",
]
