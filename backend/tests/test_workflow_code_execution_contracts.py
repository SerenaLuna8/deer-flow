from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from deerflow.sandbox.local import LocalSandboxProvider
from deerflow.workflows.canonical import canonical_json_value
from deerflow.workflows.code_execution import (
    CODE_NETWORK_POLICY,
    CODE_RUNTIME_CONTRACT,
    DEFAULT_CODE_LIMITS,
    CodeCleanupReceipt,
    CodeExecutionControl,
    FrozenCodeLimits,
    IsolatedCodeCleanupPending,
    IsolatedCodeExecutionLease,
    IsolatedCodeExecutionProvider,
    IsolatedCodeExecutionRequest,
    IsolatedCodeExecutionResult,
    IsolatedCodeProfileAttestation,
)
from deerflow.workflows.code_execution.docker_provider import DockerIsolatedCodeExecutionProvider

NODE_ID = "11111111-1111-4111-8111-111111111111"
IMAGE_DIGEST = "b" * 64
RUNNER_DIGEST = "c" * 64


def _request(
    *,
    source: str = "def main(inputs):\n    return {'value': inputs['value']}\n",
    limits: FrozenCodeLimits = DEFAULT_CODE_LIMITS,
    extra: dict | None = None,
) -> IsolatedCodeExecutionRequest:
    payload = {
        "runtime_contract": CODE_RUNTIME_CONTRACT,
        "activation": {
            "project_id": "project-1",
            "owner_user_id": "owner-1",
            "workflow_run_id": "workflow-run-1",
            "node_id": NODE_ID,
            "activation_id": "activation-1",
            "attempt": 1,
        },
        "profile_digest": _attestation().profile_digest,
        "source": source,
        "source_digest": hashlib.sha256(source.encode()).hexdigest(),
        "inputs": {"value": 7},
        "limits": limits,
        "network_policy": CODE_NETWORK_POLICY,
    }
    payload.update(extra or {})
    return IsolatedCodeExecutionRequest.model_validate(payload)


def _lease(request: IsolatedCodeExecutionRequest) -> IsolatedCodeExecutionLease:
    return IsolatedCodeExecutionLease(
        lease_id="lease-1",
        activation_digest=request.activation.digest(),
        profile_digest=request.profile_digest,
        resource_id="resource-1",
    )


def _attestation() -> IsolatedCodeProfileAttestation:
    payload = {
        "profile_key": "docker-python3.12-v1",
        "runtime_contract": CODE_RUNTIME_CONTRACT,
        "image_digest": IMAGE_DIGEST,
        "runner_digest": RUNNER_DIGEST,
        "network_policy": CODE_NETWORK_POLICY,
        "fresh_activation": True,
        "no_mounts": True,
        "empty_environment": True,
        "non_root": True,
        "read_only_rootfs": True,
        "no_new_privileges": True,
        "capabilities_dropped": True,
        "destroy_confirmation": True,
        "orphan_reconciliation": True,
        "orphan_fence_contract": "local-posix-flock-v1",
        "maximum_limits": DEFAULT_CODE_LIMITS,
    }
    serialized = {key: value.model_dump(mode="json") if isinstance(value, FrozenCodeLimits) else value for key, value in payload.items()}
    profile_digest = hashlib.sha256(canonical_json_value(serialized).encode()).hexdigest()
    return IsolatedCodeProfileAttestation(**payload, profile_digest=profile_digest)


class RecordingProvider(IsolatedCodeExecutionProvider):
    def __init__(
        self,
        *,
        cleanup_state: str = "destroyed_confirmed",
        block: bool = False,
        execute_error: BaseException | None = None,
        cleanup_callback=None,
    ) -> None:
        self.cleanup_state = cleanup_state
        self.block = block
        self.execute_error = execute_error
        self.cleanup_callback = cleanup_callback
        self.events: list[str] = []

    def attest(self) -> IsolatedCodeProfileAttestation:
        return _attestation()

    def acquire(self, request: IsolatedCodeExecutionRequest) -> IsolatedCodeExecutionLease:
        self.events.append("acquire")
        return _lease(request)

    def execute(
        self,
        lease: IsolatedCodeExecutionLease,
        request: IsolatedCodeExecutionRequest,
        control: CodeExecutionControl,
    ) -> IsolatedCodeExecutionResult:
        del lease, request
        self.events.append("execute")
        if self.execute_error is not None:
            raise self.execute_error
        if self.block:
            while control.interruption() is None:
                time.sleep(0.005)
            self.events.append("interrupted")
            return IsolatedCodeExecutionResult(
                outcome="cancelled",
                exit_code=137,
                result=None,
                stdout_tail="",
                stderr_tail="",
                truncated=False,
                duration_ms=1,
                interruption=control.interruption(),
            )
        return IsolatedCodeExecutionResult(
            outcome="succeeded",
            exit_code=0,
            result={"value": 7},
            stdout_tail="",
            stderr_tail="",
            truncated=False,
            duration_ms=1,
        )

    def cleanup(self, lease: IsolatedCodeExecutionLease, *, reason: str) -> CodeCleanupReceipt:
        self.events.append(f"cleanup:{reason}")
        if self.cleanup_callback is not None:
            self.cleanup_callback()
        return CodeCleanupReceipt(
            lease_id=lease.lease_id,
            state=self.cleanup_state,
            reason=reason,
        )

    def reconcile_orphans(self) -> tuple[CodeCleanupReceipt, ...]:
        return ()


def test_request_is_strict_digest_bound_and_carries_no_generic_execution_fields() -> None:
    request = _request()
    assert set(request.runner_envelope()) == {
        "inputs",
        "limits",
        "runtime_contract",
        "source",
        "source_digest",
    }
    for forbidden in ("command", "args", "env", "mounts", "secrets", "thread_id", "image"):
        with pytest.raises(ValidationError):
            _request(extra={forbidden: "forbidden"})
    with pytest.raises(ValidationError, match="source digest mismatch"):
        _request(extra={"source_digest": "0" * 64})
    with pytest.raises(ValidationError, match="UTF-8 byte limit"):
        _request(source="x" * (64 * 1024 + 1))
    with pytest.raises(ValidationError, match="canonical inputs exceed"):
        _request(extra={"inputs": {"value": "x" * (1024 * 1024)}})


def test_limits_are_strict_finite_and_cannot_exceed_profile_maxima() -> None:
    payload = DEFAULT_CODE_LIMITS.model_dump()
    for field in payload:
        with pytest.raises(ValidationError):
            FrozenCodeLimits.model_validate({**payload, field: True})
    with pytest.raises(ValidationError):
        FrozenCodeLimits.model_validate({**payload, "wall_timeout_ms": 30_001})
    with pytest.raises(ValidationError):
        FrozenCodeLimits.model_validate({**payload, "memory_bytes": 256 * 1024 * 1024 + 1})
    with pytest.raises(ValidationError):
        FrozenCodeLimits.model_validate({**payload, "max_pids": 33})
    with pytest.raises(ValidationError, match="total Code log budget"):
        FrozenCodeLimits.model_validate({**payload, "max_total_log_bytes": 64 * 1024})


def test_attested_boolean_capabilities_reject_integer_aliases() -> None:
    payload = _attestation().model_dump()
    for field in (
        "fresh_activation",
        "no_mounts",
        "empty_environment",
        "non_root",
        "read_only_rootfs",
        "no_new_privileges",
        "capabilities_dropped",
        "destroy_confirmation",
        "orphan_reconciliation",
    ):
        with pytest.raises(ValidationError):
            IsolatedCodeProfileAttestation.model_validate({**payload, field: 1})
    with pytest.raises(ValidationError, match="attestation digest mismatch"):
        IsolatedCodeProfileAttestation.model_validate({**payload, "profile_digest": "0" * 64})


def test_provider_rejects_request_for_another_attested_profile_before_acquire() -> None:
    provider = RecordingProvider()
    request = _request()
    mismatched = IsolatedCodeExecutionRequest.model_validate({**request.model_dump(mode="json"), "profile_digest": "0" * 64})
    with pytest.raises(ValueError, match="profile does not match"):
        provider.run(mismatched)
    assert provider.events == []


def test_provider_returns_candidate_only_after_destroy_confirmation() -> None:
    provider = RecordingProvider()
    completion = provider.run(_request())
    assert completion.result.result == {"value": 7}
    assert completion.cleanup.state == "destroyed_confirmed"
    assert provider.events == ["acquire", "execute", "cleanup:completed"]


def test_cleanup_pending_never_returns_the_candidate_result() -> None:
    provider = RecordingProvider(cleanup_state="cleanup_pending")
    with pytest.raises(IsolatedCodeCleanupPending) as captured:
        provider.run(_request())
    assert captured.value.receipt.state == "cleanup_pending"
    assert provider.events == ["acquire", "execute", "cleanup:completed"]


def test_execute_error_with_cleanup_pending_preserves_cleanup_authority() -> None:
    provider = RecordingProvider(
        cleanup_state="cleanup_pending",
        execute_error=RuntimeError("runner transport failed"),
    )
    with pytest.raises(IsolatedCodeCleanupPending) as captured:
        provider.run(_request())
    assert isinstance(captured.value.__cause__, RuntimeError)
    assert captured.value.receipt.state == "cleanup_pending"
    assert provider.events == ["acquire", "execute", "cleanup:failed"]


def test_execute_error_is_re_raised_only_after_confirmed_destroy() -> None:
    provider = RecordingProvider(execute_error=RuntimeError("runner transport failed"))
    with pytest.raises(RuntimeError, match="runner transport failed"):
        provider.run(_request())
    assert provider.events == ["acquire", "execute", "cleanup:failed"]


@pytest.mark.asyncio
async def test_async_task_cancellation_signals_immediately_then_joins_cleanup_barrier() -> None:
    provider = RecordingProvider(block=True)
    task = asyncio.create_task(provider.run_async(_request()))
    await asyncio.sleep(0.03)
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < 0.5
    assert provider.events == ["acquire", "execute", "interrupted", "cleanup:cancelled"]


def test_lease_loss_is_polled_and_cleanup_reason_is_fenced() -> None:
    provider = RecordingProvider(block=True)
    completion = provider.run(
        _request(),
        control=CodeExecutionControl(lease_is_current=lambda: False),
    )
    assert completion.result.outcome == "cancelled"
    assert completion.result.interruption == "lease_lost"
    assert provider.events == ["acquire", "execute", "interrupted", "cleanup:lease_lost"]


@pytest.mark.parametrize("probe_result", [0, 1, None, "true"])
def test_lease_probe_requires_a_real_boolean(probe_result: object) -> None:
    control = CodeExecutionControl(lease_is_current=lambda: probe_result)  # type: ignore[arg-type,return-value]
    with pytest.raises(TypeError, match="lease_is_current must return bool"):
        control.interruption()


def test_lease_loss_during_cleanup_discards_candidate_after_destroy_confirmation() -> None:
    lease_current = True

    def lose_lease() -> None:
        nonlocal lease_current
        lease_current = False

    provider = RecordingProvider(cleanup_callback=lose_lease)
    completion = provider.run(
        _request(),
        control=CodeExecutionControl(lease_is_current=lambda: lease_current),
    )
    assert completion.result.outcome == "cancelled"
    assert completion.result.interruption == "lease_lost"
    assert completion.result.result is None
    assert completion.cleanup.state == "destroyed_confirmed"
    assert completion.cleanup.reason == "lease_lost"
    assert provider.events == ["acquire", "execute", "cleanup:completed"]


def test_local_sandbox_and_generic_execute_command_are_not_code_capabilities() -> None:
    assert not issubclass(LocalSandboxProvider, IsolatedCodeExecutionProvider)
    assert "execute_command" not in IsolatedCodeExecutionProvider.__dict__
    assert "acquire" in IsolatedCodeExecutionProvider.__dict__
    assert "cleanup" in IsolatedCodeExecutionProvider.__dict__


def test_application_code_cannot_use_unjournaled_provider_run_compatibility_path() -> None:
    app_root = Path(__file__).resolve().parents[1] / "app"
    for path in app_root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "deerflow.workflows.code_execution" not in source:
            continue
        assert ".run(" not in source
        assert ".run_async(" not in source


def test_docker_create_contract_has_no_mount_env_shell_or_unconfined_escape() -> None:
    request = _request()
    provider = DockerIsolatedCodeExecutionProvider(
        image_id=f"sha256:{IMAGE_DIGEST}",
        runner_digest=RUNNER_DIGEST,
    )
    arguments = provider._create_arguments(name="resource-1", request=request, lease_id="lease-1")
    joined = " ".join(arguments)
    assert arguments[-1] == f"sha256:{IMAGE_DIGEST}"
    assert "--network none" in joined
    assert "--read-only" in arguments
    assert "--cap-drop ALL" in joined
    assert "--security-opt no-new-privileges=true" in joined
    assert "--pids-limit 32" in joined
    assert "--cpus 1.000" in joined
    assert f"--memory {256 * 1024 * 1024}" in joined
    assert f"--memory-swap {256 * 1024 * 1024}" in joined
    assert "noexec,nosuid,nodev" in joined
    for forbidden in (
        "--mount",
        "--volume",
        "--env",
        "--privileged",
        "seccomp=unconfined",
        request.source,
        "execute_command",
        "bash",
    ):
        assert forbidden not in arguments
        assert forbidden not in joined
    provider.close()
