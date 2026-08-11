"""Hostile real-container conformance for the Workflow Python Code profile."""

# ruff: noqa: E402, I001 -- bootstrap backend imports before loading fixtures.

from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pytest

from conformance.workflow_code.conftest import MANAGED_LABEL, docker
from deerflow.workflows.code_execution import (
    CODE_NETWORK_POLICY,
    CODE_RUNTIME_CONTRACT,
    DEFAULT_CODE_LIMITS,
    CodeExecutionControl,
    CodeProvisioningHandle,
    FrozenCodeLimits,
    IsolatedCodeExecutionLease,
    IsolatedCodeExecutionRequest,
)
from deerflow.workflows.code_execution.docker_provider import (
    DockerIsolatedCodeExecutionProvider,
    _provisioning_lease_path,
)

NODE_ID = "22222222-2222-4222-8222-222222222222"


def limits(**updates: int) -> FrozenCodeLimits:
    payload = DEFAULT_CODE_LIMITS.model_dump()
    payload.update(updates)
    return FrozenCodeLimits.model_validate(payload)


def request(
    provider: DockerIsolatedCodeExecutionProvider,
    source: str,
    *,
    activation_id: str = "activation-1",
    execution_limits: FrozenCodeLimits = DEFAULT_CODE_LIMITS,
    inputs: dict | None = None,
) -> IsolatedCodeExecutionRequest:
    return IsolatedCodeExecutionRequest(
        runtime_contract=CODE_RUNTIME_CONTRACT,
        activation={
            "project_id": "project-1",
            "owner_user_id": "owner-1",
            "workflow_run_id": "workflow-run-1",
            "node_id": NODE_ID,
            "activation_id": activation_id,
            "attempt": 1,
        },
        profile_digest=provider.attest().profile_digest,
        source=source,
        source_digest=hashlib.sha256(source.encode()).hexdigest(),
        inputs=inputs or {},
        limits=execution_limits,
        network_policy=CODE_NETWORK_POLICY,
    )


def managed_container_names(
    provider: DockerIsolatedCodeExecutionProvider,
) -> list[str]:
    completed = docker(
        "container",
        "ls",
        "--all",
        "--filter",
        f"label={MANAGED_LABEL}",
        "--filter",
        f"label=org.actweave.workflow-code.owner-id={provider._owner_lease.owner_id}",
        "--format",
        "{{.Names}}",
    )
    return sorted(filter(None, completed.stdout.splitlines()))


def _crash_after_starting_container(
    image_id: str,
    runner_digest: str,
    connection,
) -> None:
    provider = DockerIsolatedCodeExecutionProvider(
        image_id=image_id,
        runner_digest=runner_digest,
    )
    source = "def main(inputs):\n    while True:\n        pass\n"
    execution_request = request(provider, source)
    lease = provider.acquire(execution_request)
    docker("container", "start", lease.resource_id)
    connection.send(lease.model_dump(mode="json"))
    connection.close()
    os._exit(91)


def _sigkill_after_reserved_acquire(
    image_id: str,
    runner_digest: str,
    connection,
    lease_id: str,
    reconciliation_key: str,
) -> None:
    provider = DockerIsolatedCodeExecutionProvider(
        image_id=image_id,
        runner_digest=runner_digest,
    )
    source = "def main(inputs):\n    return {'ok': True}\n"
    lease = provider.acquire_reserved(
        request(provider, source, activation_id="reserved-crash"),
        CodeProvisioningHandle(
            lease_id=lease_id,
            reconciliation_key=reconciliation_key,
        ),
    )
    connection.send(lease.model_dump(mode="json"))
    connection.close()
    os.kill(os.getpid(), signal.SIGKILL)


def _hold_operation_lock_after_docker_create(
    image_id: str,
    runner_digest: str,
    connection,
    lease_id: str,
    reconciliation_key: str,
) -> None:
    provider = DockerIsolatedCodeExecutionProvider(
        image_id=image_id,
        runner_digest=runner_digest,
    )
    original_run_cli = provider._run_cli

    def run_cli_then_hold(arguments, *, check=True, timeout=15.0):
        completed = original_run_cli(arguments, check=check, timeout=timeout)
        if arguments[:2] == ["container", "create"]:
            resource_id = arguments[arguments.index("--name") + 1]
            connection.send(resource_id)
            while True:
                time.sleep(1)
        return completed

    provider._run_cli = run_cli_then_hold
    source = "def main(inputs):\n    return {'ok': True}\n"
    provider.acquire_reserved(
        request(provider, source, activation_id="reserved-in-flight"),
        CodeProvisioningHandle(
            lease_id=lease_id,
            reconciliation_key=reconciliation_key,
        ),
    )


def test_real_profile_attests_and_created_resource_has_exact_hardening(
    docker_provider: DockerIsolatedCodeExecutionProvider,
) -> None:
    attestation = docker_provider.attest()
    assert attestation.runtime_contract == "python3.12-v1"
    assert attestation.network_policy == "deny_all"
    assert attestation.image_digest
    assert attestation.runner_digest
    code = "def main(inputs):\n    return {'ok': True}\n"
    execution_request = request(docker_provider, code)
    lease = docker_provider.acquire(execution_request)
    try:
        inspected = json.loads(docker("container", "inspect", lease.resource_id).stdout)[0]
        host = inspected["HostConfig"]
        assert inspected["Mounts"] == []
        assert inspected["Config"]["User"] == "65532:65532"
        assert inspected["Config"]["Entrypoint"][:2] == ["/usr/bin/env", "-i"]
        assert host["NetworkMode"] == "none"
        assert host["ReadonlyRootfs"] is True
        assert host["Privileged"] is False
        assert host["CapDrop"] == ["ALL"]
        assert "no-new-privileges=true" in host["SecurityOpt"]
        assert host["PidsLimit"] == 32
        assert host["Memory"] == 256 * 1024 * 1024
        assert host["MemorySwap"] == 256 * 1024 * 1024
        assert host["NanoCpus"] == 1_000_000_000
        assert set(host["Tmpfs"]) == {"/tmp"}
        assert "noexec" in host["Tmpfs"]["/tmp"]
        assert host["IpcMode"] == "none"
        assert host["RestartPolicy"]["Name"] == "no"
    finally:
        receipt = docker_provider.cleanup(lease, reason="failed")
    assert receipt.state == "destroyed_confirmed"
    assert docker("container", "inspect", lease.resource_id, check=False).returncode != 0


def test_success_is_fresh_empty_environment_mount_free_and_destroy_confirmed(
    docker_provider: DockerIsolatedCodeExecutionProvider,
) -> None:
    source = """
def main(inputs):
    import os
    import pathlib
    import socket

    def exists(path):
        return pathlib.Path(path).exists()

    root_read_only = False
    try:
        pathlib.Path('/actweave-root-write-proof').write_text('forbidden')
    except OSError:
        root_read_only = True
    pathlib.Path('/tmp/allowed').write_text('ok')
    dns_denied = False
    try:
        socket.getaddrinfo('example.com', 443)
    except OSError:
        dns_denied = True
    public_connect_denied = False
    probe = socket.socket()
    probe.settimeout(0.2)
    try:
        probe.connect(('1.1.1.1', 443))
    except OSError:
        public_connect_denied = True
    finally:
        probe.close()
    return {
        'effective_uid': os.geteuid(),
        'environment': dict(os.environ),
        'proc_environment': open('/proc/self/environ', 'rb').read().decode(),
        'root_read_only': root_read_only,
        'tmp_writable': pathlib.Path('/tmp/allowed').read_text() == 'ok',
        'dns_denied': dns_denied,
        'public_connect_denied': public_connect_denied,
        'thread_mount': exists('/mnt/user-data'),
        'skill_mount': exists('/mnt/skills'),
        'custom_mount': exists('/mnt/custom'),
        'docker_socket': exists('/var/run/docker.sock'),
        'containerd_socket': exists('/run/containerd/containerd.sock'),
        'kubernetes_token': exists('/var/run/secrets/kubernetes.io/serviceaccount/token'),
        'host_projection': exists('/host') or exists('/host_mnt'),
    }
"""
    first = docker_provider.run(request(docker_provider, source, activation_id="fresh-1"))
    second = docker_provider.run(request(docker_provider, source, activation_id="fresh-2"))
    assert first.lease.lease_id != second.lease.lease_id
    assert first.lease.resource_id != second.lease.resource_id
    assert first.cleanup.state == second.cleanup.state == "destroyed_confirmed"
    assert first.result.result == second.result.result
    observed = first.result.result
    assert observed is not None
    assert observed["effective_uid"] == 65532
    assert observed["environment"] == {}
    assert observed["proc_environment"] == ""
    assert observed["root_read_only"] is True
    assert observed["tmp_writable"] is True
    assert observed["dns_denied"] is True
    assert observed["public_connect_denied"] is True
    for absent in (
        "thread_mount",
        "skill_mount",
        "custom_mount",
        "docker_socket",
        "containerd_socket",
        "kubernetes_token",
        "host_projection",
    ):
        assert observed[absent] is False
    assert managed_container_names(docker_provider) == []


def test_syntax_runtime_result_and_log_limits_are_structured_and_destroyed(
    docker_provider: DockerIsolatedCodeExecutionProvider,
) -> None:
    syntax = docker_provider.run(request(docker_provider, "def main(inputs)\n    return {}\n"))
    assert syntax.result.outcome == "syntax_error"
    assert syntax.cleanup.state == "destroyed_confirmed"
    runtime = docker_provider.run(request(docker_provider, "def main(inputs):\n    raise RuntimeError('boom')\n"))
    assert runtime.result.outcome == "runtime_error"
    assert "RuntimeError" in runtime.result.stderr_tail
    flood_logs = "def main(inputs):\n    print('x' * 200000)\n    return {'ok': True}\n"
    logged = docker_provider.run(
        request(
            docker_provider,
            flood_logs,
            execution_limits=limits(max_stdout_bytes=4096, max_stderr_bytes=4096),
        )
    )
    assert logged.result.outcome == "succeeded"
    assert len(logged.result.stdout_tail.encode()) <= 4096
    assert logged.result.truncated is True
    flood_result = "def main(inputs):\n    return {'value': 'x' * 10000}\n"
    oversized = docker_provider.run(
        request(
            docker_provider,
            flood_result,
            execution_limits=limits(max_result_bytes=4096),
        )
    )
    assert oversized.result.outcome == "output_limit"
    direct_fd_flood = "def main(inputs):\n    import os\n    os.write(1, b'x' * 3000000)\n    return {'ok': True}\n"
    fd_limited = docker_provider.run(request(docker_provider, direct_fd_flood))
    assert fd_limited.result.outcome == "output_limit"
    assert fd_limited.result.truncated is True
    assert managed_container_names(docker_provider) == []


def test_wall_memory_pid_and_tmpfs_limits_are_real_and_cleanup_descendants(
    docker_provider: DockerIsolatedCodeExecutionProvider,
) -> None:
    busy = "def main(inputs):\n    while True:\n        pass\n"
    timeout_result = docker_provider.run(
        request(
            docker_provider,
            busy,
            execution_limits=limits(wall_timeout_ms=500),
        )
    )
    assert timeout_result.result.outcome == "timeout"
    assert timeout_result.result.duration_ms < 5_000

    cpu_cgroup = """
def main(inputs):
    with open('/sys/fs/cgroup/cpu.max') as source:
        quota, period = source.read().strip().split()
    return {'quota': int(quota), 'period': int(period)}
"""
    cpu_result = docker_provider.run(
        request(
            docker_provider,
            cpu_cgroup,
            execution_limits=limits(cpu_millicores=250),
        )
    )
    assert cpu_result.result.outcome == "succeeded"
    assert cpu_result.result.result is not None
    quota = cpu_result.result.result["quota"]
    period = cpu_result.result.result["period"]
    assert isinstance(quota, int) and isinstance(period, int)
    assert 0 < quota / period <= 0.25

    memory = "def main(inputs):\n    value = bytearray(256 * 1024 * 1024)\n    return {'size': len(value)}\n"
    memory_result = docker_provider.run(
        request(
            docker_provider,
            memory,
            execution_limits=limits(memory_bytes=64 * 1024 * 1024),
        )
    )
    assert memory_result.result.outcome == "resource_exhausted"

    pids = """
def main(inputs):
    import os
    import time
    created = 0
    denied = False
    while True:
        try:
            child = os.fork()
        except OSError:
            denied = True
            break
        if child == 0:
            time.sleep(20)
            os._exit(0)
        created += 1
    return {'created': created, 'denied': denied}
"""
    pid_result = docker_provider.run(
        request(
            docker_provider,
            pids,
            execution_limits=limits(max_pids=8, wall_timeout_ms=5_000),
        )
    )
    assert pid_result.result.outcome == "succeeded"
    assert pid_result.result.result is not None
    assert pid_result.result.result["denied"] is True
    assert 1 <= pid_result.result.result["created"] <= 7

    disk = """
def main(inputs):
    denied = False
    try:
        with open('/tmp/fill', 'wb') as target:
            target.write(b'x' * (4 * 1024 * 1024))
            target.flush()
    except OSError:
        denied = True
    return {'denied': denied}
"""
    disk_result = docker_provider.run(
        request(
            docker_provider,
            disk,
            execution_limits=limits(tmpfs_bytes=1024 * 1024),
        )
    )
    assert disk_result.result.outcome == "succeeded"
    assert disk_result.result.result == {"denied": True}
    assert managed_container_names(docker_provider) == []


@pytest.mark.asyncio
async def test_task_cancel_kills_long_loop_waits_for_destroy_and_leaves_no_orphan(
    docker_provider: DockerIsolatedCodeExecutionProvider,
) -> None:
    source = "def main(inputs):\n    while True:\n        pass\n"
    task = asyncio.create_task(docker_provider.run_async(request(docker_provider, source)))
    deadline = time.monotonic() + 10
    while not managed_container_names(docker_provider) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert managed_container_names(docker_provider)
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < 5
    assert managed_container_names(docker_provider) == []
    assert docker_provider.reconcile_orphans() == ()


def test_lease_loss_kills_execution_and_returns_only_after_destroy(
    docker_provider: DockerIsolatedCodeExecutionProvider,
) -> None:
    lease_current = threading.Event()
    lease_current.set()
    control = CodeExecutionControl(lease_is_current=lease_current.is_set)
    source = "def main(inputs):\n    while True:\n        pass\n"
    execution_request = request(docker_provider, source)
    outcome: list = []

    def execute() -> None:
        outcome.append(docker_provider.run(execution_request, control=control))

    worker = threading.Thread(target=execute)
    worker.start()
    deadline = time.monotonic() + 10
    while not managed_container_names(docker_provider) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert managed_container_names(docker_provider)
    lease_current.clear()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(outcome) == 1
    assert outcome[0].result.outcome == "cancelled"
    assert outcome[0].result.interruption == "lease_lost"
    assert outcome[0].cleanup.state == "destroyed_confirmed"
    assert managed_container_names(docker_provider) == []


def test_new_provider_reconciles_durable_labeled_worker_crash_orphan(
    runner_image_id: str,
    runner_digest: str,
) -> None:
    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe(duplex=False)
    crashed_worker = context.Process(
        target=_crash_after_starting_container,
        args=(runner_image_id, runner_digest, child_connection),
    )
    crashed_worker.start()
    child_connection.close()
    assert parent_connection.poll(20)
    abandoned = IsolatedCodeExecutionLease.model_validate(parent_connection.recv())
    parent_connection.close()
    crashed_worker.join(timeout=10)
    assert crashed_worker.exitcode == 91
    inspected = json.loads(docker("container", "inspect", abandoned.resource_id).stdout)[0]
    assert inspected["State"]["Running"] is True

    restarted_worker_provider = DockerIsolatedCodeExecutionProvider(
        image_id=runner_image_id,
        runner_digest=runner_digest,
    )
    receipts = restarted_worker_provider.reconcile_orphans()
    assert len(receipts) == 1
    assert receipts[0].lease_id == abandoned.lease_id
    assert receipts[0].state == "destroyed_confirmed"
    assert receipts[0].reason == "worker_crash_reconcile"
    assert docker("container", "inspect", abandoned.resource_id, check=False).returncode != 0
    restarted_worker_provider.close()


def test_reserved_provisioning_is_exactly_reconciled_after_sigkill(
    runner_image_id: str,
    runner_digest: str,
) -> None:
    lease_id = "31000000-0000-4000-8000-000000000077"
    reconciliation_key = "reserved-reconciliation-secret-key-00000001"
    handle = CodeProvisioningHandle(
        lease_id=lease_id,
        reconciliation_key=reconciliation_key,
    )
    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe(duplex=False)
    crashed_worker = context.Process(
        target=_sigkill_after_reserved_acquire,
        args=(
            runner_image_id,
            runner_digest,
            child_connection,
            lease_id,
            reconciliation_key,
        ),
    )
    crashed_worker.start()
    child_connection.close()
    assert parent_connection.poll(20)
    abandoned = IsolatedCodeExecutionLease.model_validate(parent_connection.recv())
    parent_connection.close()
    crashed_worker.join(timeout=10)
    assert crashed_worker.exitcode == -signal.SIGKILL

    inspected = json.loads(docker("container", "inspect", abandoned.resource_id).stdout)[0]
    labels = inspected["Config"]["Labels"]
    assert labels["org.actweave.workflow-code.lease-id"] == lease_id
    assert labels["org.actweave.workflow-code.reconciliation-key-sha256"] == handle.reconciliation_key_hash
    assert reconciliation_key not in json.dumps(inspected, sort_keys=True)

    restarted = DockerIsolatedCodeExecutionProvider(
        image_id=runner_image_id,
        runner_digest=runner_digest,
    )
    receipt = restarted.reconcile_provisioning(
        lease_id=lease_id,
        reconciliation_key_hash=handle.reconciliation_key_hash,
    )
    assert receipt.state == "destroyed_confirmed"
    assert receipt.reason == "worker_crash_reconcile"
    assert docker("container", "inspect", abandoned.resource_id, check=False).returncode != 0
    restarted.release_provisioning_handle(
        lease_id=lease_id,
        reconciliation_key_hash=handle.reconciliation_key_hash,
    )
    lock_path, _ = _provisioning_lease_path(lease_id, handle.reconciliation_key_hash)
    assert not lock_path.exists()
    restarted.close()


def test_reaper_cannot_destroy_while_reserved_create_holds_operation_lock(
    runner_image_id: str,
    runner_digest: str,
) -> None:
    lease_id = "31000000-0000-4000-8000-000000000078"
    reconciliation_key = "reserved-reconciliation-secret-key-00000002"
    handle = CodeProvisioningHandle(
        lease_id=lease_id,
        reconciliation_key=reconciliation_key,
    )
    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe(duplex=False)
    acquiring_worker = context.Process(
        target=_hold_operation_lock_after_docker_create,
        args=(
            runner_image_id,
            runner_digest,
            child_connection,
            lease_id,
            reconciliation_key,
        ),
    )
    restarted = DockerIsolatedCodeExecutionProvider(
        image_id=runner_image_id,
        runner_digest=runner_digest,
    )
    resource_id = None
    try:
        acquiring_worker.start()
        child_connection.close()
        assert parent_connection.poll(20)
        resource_id = parent_connection.recv()
        assert docker("container", "inspect", resource_id, check=False).returncode == 0

        pending = restarted.reconcile_provisioning(
            lease_id=lease_id,
            reconciliation_key_hash=handle.reconciliation_key_hash,
        )
        assert pending.state == "cleanup_pending"
        assert docker("container", "inspect", resource_id, check=False).returncode == 0

        os.kill(acquiring_worker.pid, signal.SIGKILL)
        acquiring_worker.join(timeout=10)
        assert acquiring_worker.exitcode == -signal.SIGKILL
        destroyed = restarted.reconcile_provisioning(
            lease_id=lease_id,
            reconciliation_key_hash=handle.reconciliation_key_hash,
        )
        assert destroyed.state == "destroyed_confirmed"
        assert docker("container", "inspect", resource_id, check=False).returncode != 0
        restarted.release_provisioning_handle(
            lease_id=lease_id,
            reconciliation_key_hash=handle.reconciliation_key_hash,
        )
        lock_path, _ = _provisioning_lease_path(lease_id, handle.reconciliation_key_hash)
        assert not lock_path.exists()
    finally:
        if acquiring_worker.is_alive():
            os.kill(acquiring_worker.pid, signal.SIGKILL)
        acquiring_worker.join(timeout=10)
        parent_connection.close()
        child_connection.close()
        restarted.reconcile_provisioning(
            lease_id=lease_id,
            reconciliation_key_hash=handle.reconciliation_key_hash,
        )
        restarted.release_provisioning_handle(
            lease_id=lease_id,
            reconciliation_key_hash=handle.reconciliation_key_hash,
        )
        restarted.close()


def test_reconciler_does_not_destroy_another_live_worker_resource(
    docker_provider: DockerIsolatedCodeExecutionProvider,
    runner_image_id: str,
    runner_digest: str,
) -> None:
    source = "def main(inputs):\n    return {'ok': True}\n"
    execution_request = request(docker_provider, source)
    live_lease = docker_provider.acquire(execution_request)
    second_worker_provider = DockerIsolatedCodeExecutionProvider(
        image_id=runner_image_id,
        runner_digest=runner_digest,
    )
    try:
        assert second_worker_provider.reconcile_orphans() == ()
        assert docker("container", "inspect", live_lease.resource_id, check=False).returncode == 0
    finally:
        receipt = docker_provider.cleanup(live_lease, reason="failed")
        second_worker_provider.close()
    assert receipt.state == "destroyed_confirmed"


def test_cleanup_pending_is_reconcilable_by_the_same_provider(
    docker_provider: DockerIsolatedCodeExecutionProvider,
) -> None:
    source = "def main(inputs):\n    return {'ok': True}\n"
    execution_request = request(docker_provider, source)
    lease = docker_provider.acquire(execution_request)
    original_binary = docker_provider._docker_binary
    docker_provider._docker_binary = "/usr/bin/false"
    try:
        pending = docker_provider.cleanup(lease, reason="failed")
    finally:
        docker_provider._docker_binary = original_binary
    assert pending.state == "cleanup_pending"
    assert docker("container", "inspect", lease.resource_id, check=False).returncode == 0
    receipts = docker_provider.reconcile_orphans()
    assert len(receipts) == 1
    assert receipts[0].lease_id == lease.lease_id
    assert receipts[0].state == "destroyed_confirmed"
    assert managed_container_names(docker_provider) == []


def test_no_shell_host_python_or_generic_execute_command_fallback_in_provider_source() -> None:
    import inspect

    provider_source = inspect.getsource(DockerIsolatedCodeExecutionProvider)
    assert "shell=True" not in provider_source
    assert "execute_command" not in provider_source
    assert "LocalSandboxProvider" not in provider_source
    assert "AioSandboxProvider" not in provider_source
    assert "seccomp=unconfined" not in provider_source
    assert '"--volume",' not in provider_source
    assert '"--mount",' not in provider_source
    assert subprocess.run is not None
