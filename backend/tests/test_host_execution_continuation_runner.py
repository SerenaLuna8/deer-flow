"""Worker-side execution of a durable, frozen Local approval plan."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from collections.abc import Callable
from types import SimpleNamespace

import pytest

from deerflow.config.app_config import AppConfig
from deerflow.config.sandbox_config import SandboxConfig
from deerflow.runtime.host_execution_approval import (
    HostExecutionFrozenClaim,
    HostExecutionOutcome,
    HostExecutionPlan,
)
from deerflow.runtime.host_execution_runner import (
    HostExecutionContinuationError,
    execute_frozen_host_execution_continuation,
)
from deerflow.sandbox.local.local_sandbox import (
    LocalProcessSpawnDeadlineExpired,
)


def _config(
    *,
    use: str = "deerflow.sandbox.local:LocalSandboxProvider",
    mode: str = "approval_required",
) -> AppConfig:
    return AppConfig(
        sandbox=SandboxConfig(
            use=use,
            allow_host_bash=False,
            host_execution_approval={
                "mode": mode,
                "execution_domain_id": ("test-worker" if mode == "approval_required" else None),
            },
            bash_command_timeout=60,
            bash_output_max_chars=2_000,
        ),
    )


def _plan(**overrides: object) -> HostExecutionPlan:
    values: dict[str, object] = {
        "source_tool_call_id": "call-child-1",
        "source_run_id": "source-run",
        "source_thread_id": "thread-1",
        "description": "run script",
        "requested_command": "python /mnt/skills/demo/run.py",
        "effective_command": ("cd /source/private/workspace && python /source/transient/skills/demo/run.py"),
        "shell": "/bin/zsh",
        "cwd": "/mnt/user-data/workspace",
        "timeout_seconds": 60,
        "environment_keys": (),
        "agent_path": ("lead", "subagent:bash"),
    }
    values.update(overrides)
    return HostExecutionPlan(**values)


class _Port:
    def __init__(self, claim: HostExecutionFrozenClaim) -> None:
        self.claim = claim
        self.claim_count = 0
        self.completions: list[tuple[str, HostExecutionOutcome]] = []
        self.final_spawn_authorization_remaining_seconds: float | None = 30.0
        self.final_spawn_authorization_deadline_monotonic: float | None = None
        self.final_spawn_authorization_count = 0
        self.final_spawn_authorization_entered: asyncio.Event | None = None
        self.final_spawn_authorization_release: asyncio.Event | None = None

    async def claim_frozen_host_execution(self) -> HostExecutionFrozenClaim:
        self.claim_count += 1
        return self.claim

    async def complete_host_execution(
        self,
        approval_id: str,
        outcome: HostExecutionOutcome,
    ) -> None:
        self.completions.append((approval_id, outcome))

    def prepare_host_execution_environment(self) -> dict[str, str] | None:
        return {"PATH": "/usr/bin"}

    async def authorize_claimed_host_execution_spawn(
        self,
        approval_id: str,
    ) -> float | None:
        assert approval_id == self.claim.approval_id
        self.final_spawn_authorization_count += 1
        if self.final_spawn_authorization_entered is not None:
            self.final_spawn_authorization_entered.set()
        if self.final_spawn_authorization_release is not None:
            await self.final_spawn_authorization_release.wait()
        if self.final_spawn_authorization_deadline_monotonic is not None:
            remaining = self.final_spawn_authorization_deadline_monotonic - time.monotonic()
            return remaining if remaining > 0 else None
        return self.final_spawn_authorization_remaining_seconds


class _Sandbox:
    id = "local-run:scope:thread-1:continuation-run"

    def __init__(self) -> None:
        self.executed: list[
            tuple[
                str,
                str,
                dict[str, str] | None,
                dict[str, str] | None,
                float | None,
            ]
        ] = []

    def resolve_command_for_execution(self, command: str) -> str:
        return command.replace(
            "/mnt/skills",
            "/continuation/transient/skills",
        ).replace(
            "/mnt/user-data/workspace",
            "/continuation/private/workspace",
        )

    def get_execution_shell(self) -> str:
        return "/bin/zsh"

    def execute_prepared_command_result(
        self,
        command: str,
        *,
        shell: str,
        env: dict[str, str] | None = None,
        prepared_base_env: dict[str, str] | None = None,
        timeout: float | None = None,
        spawn_deadline_monotonic: float | None = None,
        spawn_authorization_guard: Callable[[], float] | None = None,
    ) -> SimpleNamespace:
        if spawn_authorization_guard is not None:
            spawn_deadline_monotonic = spawn_authorization_guard()
        if spawn_deadline_monotonic is not None and time.monotonic() >= spawn_deadline_monotonic:
            raise LocalProcessSpawnDeadlineExpired
        self.executed.append(
            (command, shell, env, prepared_base_env, timeout),
        )
        return SimpleNamespace(
            output="42\n",
            stdout="42\n",
            stderr="",
            exit_code=0,
        )


class _Authority:
    sandbox_id = _Sandbox.id

    def thread_data_paths(self) -> dict[str, str]:
        return {
            "workspace_path": "/mnt/user-data/workspace",
            "uploads_path": "/mnt/user-data/uploads",
            "outputs_path": "/mnt/user-data/outputs",
        }


def _context(config: AppConfig, port: _Port) -> dict[str, object]:
    return {
        "app_config": config,
        "thread_id": "thread-1",
        "run_id": "continuation-run",
        "private_scope": object(),
        "__file_authority": _Authority(),
        "__host_execution_approval_port": port,
        "__host_execution_agent_path": ("lead",),
    }


@pytest.mark.asyncio
async def test_worker_claims_rebases_and_spawns_frozen_plan_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan()
    port = _Port(HostExecutionFrozenClaim.claimed("approval-1", plan))
    sandbox = _Sandbox()
    provider = SimpleNamespace(get=lambda sandbox_id: sandbox if sandbox_id == sandbox.id else None)
    monkeypatch.setattr(
        "deerflow.runtime.host_execution_runner.get_sandbox_provider",
        lambda: provider,
    )

    graph_input = await execute_frozen_host_execution_continuation(
        approval_port=port,
        app_config=_config(),
        runtime_context=_context(_config(), port),
        file_authority=_Authority(),
        graph_input={"messages": [{"type": "human", "content": "retry bash"}]},
        continuation_required=True,
    )

    assert port.claim_count == 1
    assert len(sandbox.executed) == 1
    command, shell, env, prepared_base_env, timeout = sandbox.executed[0]
    assert "/source/transient" not in command
    assert "/continuation/transient/skills/demo/run.py" in command
    assert shell == "/bin/zsh"
    assert env is None
    assert prepared_base_env == {"PATH": "/usr/bin"}
    assert timeout == 60
    assert len(port.completions) == 1
    assert port.completions[0][1].status == "finished"
    assert port.completions[0][1].exit_code == 0
    assert graph_input["messages"][0]["additional_kwargs"]["hide_from_ui"] is True
    assert "retry bash" not in graph_input["messages"][0]["content"]
    assert "already been executed" in graph_input["messages"][0]["content"]
    assert "42" in graph_input["messages"][0]["content"]


@pytest.mark.asyncio
async def test_final_spawn_authorization_pauses_before_thread_and_denial_never_spawns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _Port(
        HostExecutionFrozenClaim.claimed("approval-1", _plan()),
    )
    port.final_spawn_authorization_remaining_seconds = None
    port.final_spawn_authorization_entered = asyncio.Event()
    port.final_spawn_authorization_release = asyncio.Event()
    sandbox = _Sandbox()
    monkeypatch.setattr(
        "deerflow.runtime.host_execution_runner.get_sandbox_provider",
        lambda: SimpleNamespace(get=lambda _sandbox_id: sandbox),
    )

    execution = asyncio.create_task(
        execute_frozen_host_execution_continuation(
            approval_port=port,
            app_config=_config(),
            runtime_context=_context(_config(), port),
            file_authority=_Authority(),
            graph_input={"messages": []},
            continuation_required=True,
        )
    )
    await asyncio.wait_for(
        port.final_spawn_authorization_entered.wait(),
        timeout=1,
    )
    assert sandbox.executed == []

    port.final_spawn_authorization_release.set()
    result = await asyncio.wait_for(execution, timeout=1)

    assert port.final_spawn_authorization_count == 1
    assert sandbox.executed == []
    assert port.completions[0][1].status == "launch_failed"
    assert port.completions[0][1].reason_code == ("pre_spawn_authorization_failed")
    assert "pre_spawn_authorization_failed" in result["messages"][0]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "revocation",
    ["capability", "run_cancel", "lease_expired"],
)
async def test_executor_queue_revocation_is_revalidated_at_spawn_and_never_spawns(
    monkeypatch: pytest.MonkeyPatch,
    revocation: str,
) -> None:
    port = _Port(
        HostExecutionFrozenClaim.claimed("approval-1", _plan()),
    )
    port.final_spawn_authorization_remaining_seconds = 30.0
    if revocation == "lease_expired":
        port.final_spawn_authorization_deadline_monotonic = time.monotonic() + 0.05
    sandbox = _Sandbox()
    monkeypatch.setattr(
        "deerflow.runtime.host_execution_runner.get_sandbox_provider",
        lambda: SimpleNamespace(get=lambda _sandbox_id: sandbox),
    )

    loop = asyncio.get_running_loop()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(executor)
    queue_blocked = threading.Event()
    release_queue = threading.Event()

    def occupy_executor() -> None:
        queue_blocked.set()
        release_queue.wait(timeout=2)

    blocker = loop.run_in_executor(None, occupy_executor)
    try:
        while not queue_blocked.is_set():
            await asyncio.sleep(0)
        execution = asyncio.create_task(
            execute_frozen_host_execution_continuation(
                approval_port=port,
                app_config=_config(),
                runtime_context=_context(_config(), port),
                file_authority=_Authority(),
                graph_input={"messages": []},
                continuation_required=True,
            )
        )
        await asyncio.sleep(0.1)
        assert port.final_spawn_authorization_count == 0
        assert sandbox.executed == []
        if revocation != "lease_expired":
            port.final_spawn_authorization_remaining_seconds = None
        release_queue.set()

        result = await asyncio.wait_for(execution, timeout=2)

        assert sandbox.executed == []
        assert port.completions[0][1].status == "launch_failed"
        assert port.completions[0][1].reason_code == ("pre_spawn_authorization_failed")
        assert "pre_spawn_authorization_failed" in result["messages"][0]["content"]
    finally:
        release_queue.set()
        await blocker
        executor.shutdown(wait=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plan", "continuation_identity", "expected_prefix"),
    [
        (
            _plan(
                channel_identity_mode="set",
                channel_user_id="source-channel-user",
            ),
            None,
            "export DEERFLOW_CHANNEL_USER_ID=source-channel-user; ",
        ),
        (
            _plan(channel_identity_mode="unset"),
            "forged-continuation-user",
            "unset DEERFLOW_CHANNEL_USER_ID; ",
        ),
        (
            _plan(),
            "forged-continuation-user",
            "",
        ),
    ],
)
async def test_continuation_rebinds_only_frozen_channel_identity(
    monkeypatch: pytest.MonkeyPatch,
    plan: HostExecutionPlan,
    continuation_identity: str | None,
    expected_prefix: str,
) -> None:
    port = _Port(HostExecutionFrozenClaim.claimed("approval-1", plan))
    sandbox = _Sandbox()
    monkeypatch.setattr(
        "deerflow.runtime.host_execution_runner.get_sandbox_provider",
        lambda: SimpleNamespace(get=lambda _sandbox_id: sandbox),
    )
    runtime_context = _context(_config(), port)
    if continuation_identity is not None:
        runtime_context["channel_user_id"] = continuation_identity

    await execute_frozen_host_execution_continuation(
        approval_port=port,
        app_config=_config(),
        runtime_context=runtime_context,
        file_authority=_Authority(),
        graph_input={"messages": []},
        continuation_required=True,
    )

    assert len(sandbox.executed) == 1
    command = sandbox.executed[0][0]
    assert command.startswith(expected_prefix)
    assert "forged-continuation-user" not in command


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "reason"),
    [
        (
            _config(use="deerflow.community.aio_sandbox:AioSandboxProvider"),
            "policy_drift",
        ),
        (_config(mode="disabled"), "policy_drift"),
    ],
)
async def test_claimed_continuation_fails_closed_on_provider_or_policy_drift(
    config: AppConfig,
    reason: str,
) -> None:
    port = _Port(HostExecutionFrozenClaim.claimed("approval-1", _plan()))

    result = await execute_frozen_host_execution_continuation(
        approval_port=port,
        app_config=config,
        runtime_context=_context(config, port),
        file_authority=_Authority(),
        graph_input={"messages": []},
        continuation_required=True,
    )

    assert len(port.completions) == 1
    assert port.completions[0][1].status == "launch_failed"
    assert port.completions[0][1].reason_code == reason
    assert "was not launched" in result["messages"][0]["content"]


@pytest.mark.asyncio
async def test_frozen_environment_binding_fails_closed_before_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _Port(
        HostExecutionFrozenClaim.claimed(
            "approval-1",
            _plan(environment_keys=("TOKEN",)),
        ),
    )
    sandbox = _Sandbox()
    monkeypatch.setattr(
        "deerflow.runtime.host_execution_runner.get_sandbox_provider",
        lambda: SimpleNamespace(get=lambda _sandbox_id: sandbox),
    )

    result = await execute_frozen_host_execution_continuation(
        approval_port=port,
        app_config=_config(),
        runtime_context=_context(_config(), port),
        file_authority=_Authority(),
        graph_input={"messages": []},
        continuation_required=True,
    )

    assert sandbox.executed == []
    assert port.completions[0][1].status == "launch_failed"
    assert port.completions[0][1].reason_code == "environment_binding_unavailable"
    assert "environment_binding_unavailable" in result["messages"][0]["content"]


@pytest.mark.asyncio
async def test_worker_environment_drift_fails_closed_before_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _Port(HostExecutionFrozenClaim.claimed("approval-1", _plan()))
    port.prepare_host_execution_environment = lambda: None  # type: ignore[method-assign]
    sandbox = _Sandbox()
    monkeypatch.setattr(
        "deerflow.runtime.host_execution_runner.get_sandbox_provider",
        lambda: SimpleNamespace(get=lambda _sandbox_id: sandbox),
    )

    result = await execute_frozen_host_execution_continuation(
        approval_port=port,
        app_config=_config(),
        runtime_context=_context(_config(), port),
        file_authority=_Authority(),
        graph_input={"messages": []},
        continuation_required=True,
    )

    assert sandbox.executed == []
    assert port.completions[0][1].status == "launch_failed"
    assert port.completions[0][1].reason_code == "host_environment_drift"
    assert "host_environment_drift" in result["messages"][0]["content"]


@pytest.mark.asyncio
async def test_spawn_exception_is_unknown_and_never_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _Port(HostExecutionFrozenClaim.claimed("approval-1", _plan()))
    sandbox = _Sandbox()

    def fail_after_possible_spawn(*_args: object, **_kwargs: object) -> object:
        sandbox.executed.append(
            ("attempt", "/bin/zsh", None, {"PATH": "/usr/bin"}, 60),
        )
        raise RuntimeError("outcome ambiguous")

    sandbox.execute_prepared_command_result = fail_after_possible_spawn  # type: ignore[method-assign]
    monkeypatch.setattr(
        "deerflow.runtime.host_execution_runner.get_sandbox_provider",
        lambda: SimpleNamespace(get=lambda _sandbox_id: sandbox),
    )

    with pytest.raises(HostExecutionContinuationError):
        await execute_frozen_host_execution_continuation(
            approval_port=port,
            app_config=_config(),
            runtime_context=_context(_config(), port),
            file_authority=_Authority(),
            graph_input={"messages": []},
            continuation_required=True,
        )

    assert len(sandbox.executed) == 1
    assert len(port.completions) == 1
    assert port.completions[0][1].status == "unknown"


@pytest.mark.asyncio
async def test_completion_adapter_failure_never_respawns_frozen_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingCompletionPort(_Port):
        def __init__(self) -> None:
            super().__init__(
                HostExecutionFrozenClaim.claimed("approval-1", _plan()),
            )
            self.completion_attempts = 0

        async def complete_host_execution(
            self,
            approval_id: str,
            outcome: HostExecutionOutcome,
        ) -> None:
            del approval_id, outcome
            self.completion_attempts += 1
            raise RuntimeError("receipt unavailable")

    port = FailingCompletionPort()
    sandbox = _Sandbox()
    monkeypatch.setattr(
        "deerflow.runtime.host_execution_runner.get_sandbox_provider",
        lambda: SimpleNamespace(get=lambda _sandbox_id: sandbox),
    )

    with pytest.raises(
        HostExecutionContinuationError,
        match="completion could not be persisted",
    ):
        await execute_frozen_host_execution_continuation(
            approval_port=port,
            app_config=_config(),
            runtime_context=_context(_config(), port),
            file_authority=_Authority(),
            graph_input={"messages": []},
            continuation_required=True,
        )

    assert len(sandbox.executed) == 1
    assert port.completion_attempts == 1


@pytest.mark.asyncio
async def test_non_continuation_keeps_original_graph_input() -> None:
    port = _Port(HostExecutionFrozenClaim.not_applicable())
    original = {"messages": [{"type": "human", "content": "hello"}]}

    result = await execute_frozen_host_execution_continuation(
        approval_port=port,
        app_config=_config(),
        runtime_context=_context(_config(), port),
        file_authority=_Authority(),
        graph_input=original,
        continuation_required=False,
    )

    assert result is original


@pytest.mark.asyncio
async def test_finished_receipt_retry_replays_result_without_provider_or_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _Port(
        HostExecutionFrozenClaim.replay(
            "approval-1",
            _plan(),
            HostExecutionOutcome(
                status="finished",
                exit_code=0,
                result_text="durable-result",
            ),
        ),
    )

    def provider_must_not_be_read() -> object:
        raise AssertionError("receipt replay must not access a provider")

    monkeypatch.setattr(
        "deerflow.runtime.host_execution_runner.get_sandbox_provider",
        provider_must_not_be_read,
    )
    result = await execute_frozen_host_execution_continuation(
        approval_port=port,
        # Policy drift after a completed receipt cannot authorize another
        # spawn and therefore does not block result-only recovery.
        app_config=_config(
            use="deerflow.community.aio_sandbox:AioSandboxProvider",
        ),
        runtime_context=_context(_config(), port),
        file_authority=None,
        graph_input={"messages": [{"type": "human", "content": "retry"}]},
        continuation_required=True,
    )

    assert "durable-result" in result["messages"][0]["content"]
    assert port.completions == []


@pytest.mark.asyncio
async def test_launch_failed_receipt_retry_never_spawns() -> None:
    port = _Port(
        HostExecutionFrozenClaim.replay(
            "approval-1",
            _plan(),
            HostExecutionOutcome(
                status="launch_failed",
                reason_code="policy_drift",
            ),
        ),
    )

    result = await execute_frozen_host_execution_continuation(
        approval_port=port,
        app_config=_config(),
        runtime_context=_context(_config(), port),
        file_authority=None,
        graph_input={"messages": []},
        continuation_required=True,
    )

    assert port.completions == []
    assert "policy_drift" in result["messages"][0]["content"]
