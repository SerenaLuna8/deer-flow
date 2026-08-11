"""Hardened one-container-per-activation Workflow Code provider.

This adapter uses only Docker lifecycle operations.  It does not instantiate
``AioSandboxProvider`` and never calls the generic ``Sandbox.execute_command``
boundary.  The image, runner and isolation profile are immutable constructor
inputs selected by trusted Worker admission from a frozen runtime snapshot.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from deerflow.workflows.canonical import canonical_json_value
from deerflow.workflows.code_execution.contracts import (
    CODE_NETWORK_POLICY,
    CODE_RUNTIME_CONTRACT,
    DEFAULT_CODE_LIMITS,
    CodeCleanupReceipt,
    CodeProvisioningHandle,
    FrozenCodeLimits,
    IsolatedCodeExecutionLease,
    IsolatedCodeExecutionRequest,
    IsolatedCodeExecutionResult,
    IsolatedCodeProfileAttestation,
)
from deerflow.workflows.code_execution.provider import (
    CodeExecutionControl,
    IsolatedCodeExecutionProvider,
)
from deerflow.workflows.contracts import JsonValue

try:
    import fcntl
except ImportError:  # pragma: no cover - this certified Docker profile is POSIX-only
    fcntl = None  # type: ignore[assignment]

_MANAGED_LABEL = "org.actweave.workflow-code.managed"
_CONTRACT_LABEL = "org.actweave.workflow-code.contract"
_PROFILE_LABEL = "org.actweave.workflow-code.profile-digest"
_ACTIVATION_LABEL = "org.actweave.workflow-code.activation-digest"
_LEASE_LABEL = "org.actweave.workflow-code.lease-id"
_RECONCILIATION_LABEL = "org.actweave.workflow-code.reconciliation-key-sha256"
_OWNER_LABEL = "org.actweave.workflow-code.owner-id"
_RUNNER_LABEL = "org.actweave.workflow-code.runner-digest"
_CONTAINER_PREFIX = "actweave-wf-code-"
_IMAGE_ID_PREFIX = "sha256:"
_RUNNER_ENTRYPOINT = [
    "/usr/bin/env",
    "-i",
    "/usr/local/bin/python3.12",
    "-I",
    "-S",
    "-B",
    "/opt/actweave/runner.py",
]
_MAX_DOCKER_OUTPUT_BYTES = 2 * 1024 * 1024


class DockerCodeProfileError(RuntimeError):
    """The local Docker capability cannot prove the frozen profile."""


class _ProviderOwnerLease:
    """Cross-process liveness fence for a local Docker Worker instance."""

    def __init__(self) -> None:
        if fcntl is None:
            raise DockerCodeProfileError("the Docker Workflow Code profile requires POSIX advisory locks")
        self.owner_id = uuid.uuid4().hex
        uid = os.getuid() if hasattr(os, "getuid") else 0
        root = Path(tempfile.gettempdir()) / f"actweave-workflow-code-owner-leases-{uid}"
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root_stat = root.lstat()
        if not stat.S_ISDIR(root_stat.st_mode) or root_stat.st_uid != uid:
            raise DockerCodeProfileError("Workflow Code owner lease directory is not trusted")
        root.chmod(0o700)
        self._root = root
        self._path = root / f"{self.owner_id}.lock"
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self._path, flags, 0o600)
        self._file = os.fdopen(descriptor, "r+")
        fcntl.flock(self._file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self._closed = False

    def owner_is_inactive(self, owner_id: str) -> bool:
        if owner_id == self.owner_id:
            return False
        if len(owner_id) != 32 or any(character not in "0123456789abcdef" for character in owner_id):
            return False
        path = self._root / f"{owner_id}.lock"
        try:
            probe = path.open("a+")
        except OSError:
            return False
        try:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            fcntl.flock(probe, fcntl.LOCK_UN)
            return True
        finally:
            probe.close()

    def close(self) -> None:
        if self._closed:
            return
        fcntl.flock(self._file, fcntl.LOCK_UN)
        self._file.close()
        self._closed = True


@contextmanager
def _provisioning_operation_lease(
    lease_id: str,
    reconciliation_key_hash: str,
    *,
    blocking: bool,
):
    """Cross-process fence spanning Docker create RPC and exact reconcile."""

    if fcntl is None:
        raise DockerCodeProfileError("the Docker Workflow Code profile requires POSIX advisory locks")
    path, uid = _provisioning_lease_path(lease_id, reconciliation_key_hash)

    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    lease_file = os.fdopen(descriptor, "r+")
    acquired = False
    try:
        operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(lease_file, operation)
            acquired = True
        except BlockingIOError:
            acquired = False
        stat_result = os.fstat(lease_file.fileno())
        if not stat.S_ISREG(stat_result.st_mode) or stat_result.st_uid != uid:
            raise DockerCodeProfileError("Workflow Code provisioning lease file is not trusted")
        yield acquired
    finally:
        if acquired:
            fcntl.flock(lease_file, fcntl.LOCK_UN)
        lease_file.close()


def _provisioning_lease_path(
    lease_id: str,
    reconciliation_key_hash: str,
) -> tuple[Path, int]:
    uid = os.getuid() if hasattr(os, "getuid") else 0
    root = Path(tempfile.gettempdir()) / f"actweave-workflow-code-provisioning-leases-{uid}"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root_stat = root.lstat()
    if not stat.S_ISDIR(root_stat.st_mode) or root_stat.st_uid != uid:
        raise DockerCodeProfileError("Workflow Code provisioning lease directory is not trusted")
    root.chmod(0o700)
    name = hashlib.sha256(f"{lease_id}\0{reconciliation_key_hash}".encode()).hexdigest()
    return root / f"{name}.lock", uid


class _BoundedPipeReader(threading.Thread):
    def __init__(self, pipe, *, limit: int, overflow: threading.Event) -> None:
        super().__init__(daemon=True)
        self._pipe = pipe
        self._limit = limit
        self._overflow = overflow
        self._buffer = bytearray()

    def run(self) -> None:
        try:
            while True:
                chunk = self._pipe.read(16 * 1024)
                if not chunk:
                    return
                remaining = self._limit - len(self._buffer)
                if remaining > 0:
                    self._buffer.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    self._overflow.set()
        finally:
            self._pipe.close()

    def value(self) -> bytes:
        return bytes(self._buffer)


def _require_image_id(value: str) -> str:
    if not value.startswith(_IMAGE_ID_PREFIX) or len(value) != len(_IMAGE_ID_PREFIX) + 64:
        raise ValueError("Workflow Code image must be an immutable sha256 image ID")
    digest = value.removeprefix(_IMAGE_ID_PREFIX)
    if any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("Workflow Code image must be an immutable sha256 image ID")
    return digest


def _require_digest(value: str, *, name: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


class DockerIsolatedCodeExecutionProvider(IsolatedCodeExecutionProvider):
    """Fresh, mount-free Docker execution for the fixed Python 3.12 runner."""

    def __init__(
        self,
        *,
        image_id: str,
        runner_digest: str,
        docker_binary: str = "docker",
    ) -> None:
        self._image_id = image_id
        self._image_digest = _require_image_id(image_id)
        self._runner_digest = _require_digest(runner_digest, name="runner_digest")
        if docker_binary != "docker" and not Path(docker_binary).is_absolute():
            raise ValueError("docker_binary must be 'docker' or an absolute trusted path")
        self._docker_binary = docker_binary
        self._owner_lease = _ProviderOwnerLease()
        self._attestation: IsolatedCodeProfileAttestation | None = None
        self._active_resources: set[str] = set()
        self._lock = threading.RLock()

    def _run_cli(
        self,
        arguments: list[str],
        *,
        check: bool = True,
        timeout: float = 15.0,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [self._docker_binary, *arguments],
                check=check,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DockerCodeProfileError("Docker control operation failed") from exc

    def _inspect_image(self) -> dict[str, Any]:
        completed = self._run_cli(["image", "inspect", self._image_id])
        try:
            payload = json.loads(completed.stdout)
            if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
                raise ValueError
            return cast(dict[str, Any], payload[0])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DockerCodeProfileError("Docker returned invalid image inspection data") from exc

    def attest(self) -> IsolatedCodeProfileAttestation:
        with self._lock:
            if self._attestation is not None:
                return self._attestation
            inspected = self._inspect_image()
            config = inspected.get("Config")
            if inspected.get("Id") != self._image_id or inspected.get("Os") != "linux" or not isinstance(config, dict):
                raise DockerCodeProfileError("Workflow Code image identity is not attested")
            labels = config.get("Labels") or {}
            if not isinstance(labels, dict):
                raise DockerCodeProfileError("Workflow Code image labels are invalid")
            if labels.get(_CONTRACT_LABEL) != CODE_RUNTIME_CONTRACT or labels.get(_RUNNER_LABEL) != self._runner_digest:
                raise DockerCodeProfileError("Workflow Code runner label does not match the frozen contract")
            if config.get("Entrypoint") != _RUNNER_ENTRYPOINT:
                raise DockerCodeProfileError("Workflow Code image entrypoint is not the fixed env-empty runner")
            if config.get("User") != "65532:65532" or config.get("WorkingDir") != "/tmp":
                raise DockerCodeProfileError("Workflow Code image must use the fixed non-root identity and workdir")
            profile_payload: dict[str, JsonValue] = {
                "capabilities_dropped": True,
                "destroy_confirmation": True,
                "empty_environment": True,
                "fresh_activation": True,
                "image_digest": self._image_digest,
                "maximum_limits": DEFAULT_CODE_LIMITS.model_dump(mode="json"),
                "network_policy": CODE_NETWORK_POLICY,
                "no_mounts": True,
                "no_new_privileges": True,
                "non_root": True,
                "orphan_fence_contract": "local-posix-flock-v1",
                "orphan_reconciliation": True,
                "profile_key": "docker-python3.12-v1",
                "read_only_rootfs": True,
                "runner_digest": self._runner_digest,
                "runtime_contract": CODE_RUNTIME_CONTRACT,
            }
            import hashlib

            profile_digest = hashlib.sha256(canonical_json_value(profile_payload).encode("utf-8")).hexdigest()
            self._attestation = IsolatedCodeProfileAttestation(
                **profile_payload,
                profile_digest=profile_digest,
            )
            return self._attestation

    @staticmethod
    def _tmpfs_spec(limits: FrozenCodeLimits) -> str:
        return f"/tmp:rw,noexec,nosuid,nodev,size={limits.tmpfs_bytes},uid=65532,gid=65532,mode=0700"

    def _create_arguments(
        self,
        *,
        name: str,
        request: IsolatedCodeExecutionRequest,
        lease_id: str,
        reconciliation_key_hash: str | None = None,
    ) -> list[str]:
        limits = request.limits
        cpu_count = limits.cpu_millicores / 1000
        arguments = [
            "container",
            "create",
            "--name",
            name,
            "--pull",
            "never",
            "--label",
            f"{_MANAGED_LABEL}=true",
            "--label",
            f"{_CONTRACT_LABEL}={CODE_RUNTIME_CONTRACT}",
            "--label",
            f"{_PROFILE_LABEL}={request.profile_digest}",
            "--label",
            f"{_ACTIVATION_LABEL}={request.activation.digest()}",
            "--label",
            f"{_LEASE_LABEL}={lease_id}",
            "--label",
            f"{_OWNER_LABEL}={self._owner_lease.owner_id}",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--user",
            "65532:65532",
            "--workdir",
            "/tmp",
            "--hostname",
            "workflow-code",
            "--ipc",
            "none",
            "--pids-limit",
            str(limits.max_pids),
            "--cpus",
            f"{cpu_count:.3f}",
            "--memory",
            str(limits.memory_bytes),
            "--memory-swap",
            str(limits.memory_bytes),
            "--tmpfs",
            self._tmpfs_spec(limits),
            "--ulimit",
            "nofile=64:64",
            "--ulimit",
            "core=0:0",
            "--stop-timeout",
            "1",
            "--restart",
            "no",
            "--interactive",
            self._image_id,
        ]
        if reconciliation_key_hash is not None:
            label_position = arguments.index("--network")
            arguments[label_position:label_position] = [
                "--label",
                f"{_RECONCILIATION_LABEL}={reconciliation_key_hash}",
            ]
        return arguments

    def _inspect_container(self, resource_id: str) -> dict[str, Any] | None:
        completed = self._run_cli(
            ["container", "inspect", resource_id],
            check=False,
        )
        if completed.returncode != 0:
            combined = f"{completed.stdout}\n{completed.stderr}".lower()
            if "no such" in combined:
                return None
            raise DockerCodeProfileError("Docker container inspection failed")
        try:
            payload = json.loads(completed.stdout)
            if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
                raise ValueError
            return cast(dict[str, Any], payload[0])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DockerCodeProfileError("Docker returned invalid container inspection data") from exc

    def _verify_created_container(
        self,
        resource_id: str,
        request: IsolatedCodeExecutionRequest,
        lease_id: str,
        reconciliation_key_hash: str | None = None,
    ) -> None:
        inspected = self._inspect_container(resource_id)
        if inspected is None:
            raise DockerCodeProfileError("Workflow Code container disappeared during acquisition")
        config = inspected.get("Config") or {}
        host = inspected.get("HostConfig") or {}
        labels = config.get("Labels") or {}
        mounts = inspected.get("Mounts") or []
        expected_labels = {
            _MANAGED_LABEL: "true",
            _CONTRACT_LABEL: CODE_RUNTIME_CONTRACT,
            _PROFILE_LABEL: request.profile_digest,
            _ACTIVATION_LABEL: request.activation.digest(),
            _LEASE_LABEL: lease_id,
            _OWNER_LABEL: self._owner_lease.owner_id,
        }
        if reconciliation_key_hash is not None:
            expected_labels[_RECONCILIATION_LABEL] = reconciliation_key_hash
        if not all(labels.get(key) == value for key, value in expected_labels.items()):
            raise DockerCodeProfileError("Workflow Code container labels do not match the lease")
        if inspected.get("Image") != self._image_id or config.get("Image") != self._image_id:
            raise DockerCodeProfileError("Workflow Code container image drifted")
        if mounts or config.get("Env") is None:
            # Docker image ENV exists in OCI metadata, but env -i is the fixed
            # entrypoint and the real process environment is proven by the
            # conformance suite.  The mounts list must remain exactly empty.
            if mounts:
                raise DockerCodeProfileError("Workflow Code container unexpectedly has mounts")
        if (
            host.get("NetworkMode") != "none"
            or host.get("ReadonlyRootfs") is not True
            or host.get("CapDrop") != ["ALL"]
            or host.get("Privileged") is not False
            or host.get("PidsLimit") != request.limits.max_pids
            or host.get("NanoCpus") != request.limits.cpu_millicores * 1_000_000
            or host.get("Memory") != request.limits.memory_bytes
            or host.get("MemorySwap") != request.limits.memory_bytes
            or host.get("IpcMode") != "none"
            or host.get("RestartPolicy", {}).get("Name") != "no"
        ):
            raise DockerCodeProfileError("Workflow Code container hardening options drifted")
        security_options = host.get("SecurityOpt") or []
        if "no-new-privileges=true" not in security_options:
            raise DockerCodeProfileError("Workflow Code container lacks no-new-privileges")
        tmpfs = host.get("Tmpfs") or {}
        if set(tmpfs) != {"/tmp"}:
            raise DockerCodeProfileError("Workflow Code container writable filesystem drifted")

    def acquire(self, request: IsolatedCodeExecutionRequest) -> IsolatedCodeExecutionLease:
        attestation = self.attest()
        if request.profile_digest != attestation.profile_digest:
            raise DockerCodeProfileError("request profile digest does not match this Worker")
        lease_id = uuid.uuid4().hex
        resource_id = f"{_CONTAINER_PREFIX}{uuid.uuid4().hex}"
        arguments = self._create_arguments(name=resource_id, request=request, lease_id=lease_id)
        try:
            self._run_cli(arguments, timeout=30.0)
            self._verify_created_container(resource_id, request, lease_id)
        except BaseException:
            try:
                self._run_cli(["container", "rm", "--force", "--volumes", resource_id], check=False)
            except Exception:
                pass
            raise
        with self._lock:
            self._active_resources.add(resource_id)
        return IsolatedCodeExecutionLease(
            lease_id=lease_id,
            activation_digest=request.activation.digest(),
            profile_digest=request.profile_digest,
            resource_id=resource_id,
        )

    def acquire_reserved(
        self,
        request: IsolatedCodeExecutionRequest,
        handle: CodeProvisioningHandle,
    ) -> IsolatedCodeExecutionLease:
        if not isinstance(handle, CodeProvisioningHandle):
            raise TypeError("handle must be CodeProvisioningHandle")
        attestation = self.attest()
        if request.profile_digest != attestation.profile_digest:
            raise DockerCodeProfileError("request profile digest does not match this Worker")
        key_hash = handle.reconciliation_key_hash
        resource_id = f"{_CONTAINER_PREFIX}{key_hash[:32]}"
        arguments = self._create_arguments(
            name=resource_id,
            request=request,
            lease_id=handle.lease_id,
            reconciliation_key_hash=key_hash,
        )
        with _provisioning_operation_lease(
            handle.lease_id,
            key_hash,
            blocking=True,
        ) as acquired:
            if not acquired:  # pragma: no cover - blocking lock must acquire or raise
                raise DockerCodeProfileError("Workflow Code provisioning lease unavailable")
            try:
                self._run_cli(arguments, timeout=30.0)
                self._verify_created_container(
                    resource_id,
                    request,
                    handle.lease_id,
                    reconciliation_key_hash=key_hash,
                )
            except BaseException:
                try:
                    self._run_cli(
                        ["container", "rm", "--force", "--volumes", resource_id],
                        check=False,
                    )
                except Exception:
                    pass
                raise
            with self._lock:
                self._active_resources.add(resource_id)
            return IsolatedCodeExecutionLease(
                lease_id=handle.lease_id,
                activation_digest=request.activation.digest(),
                profile_digest=request.profile_digest,
                resource_id=resource_id,
            )

    @staticmethod
    def _feed_stdin(pipe, payload: bytes) -> None:
        try:
            pipe.write(payload)
            pipe.close()
        except (BrokenPipeError, OSError):
            try:
                pipe.close()
            except OSError:
                pass

    def _kill(self, resource_id: str) -> None:
        self._run_cli(["container", "kill", resource_id], check=False, timeout=10.0)

    def _terminal_state(self, resource_id: str) -> tuple[bool, int | None]:
        inspected = self._inspect_container(resource_id)
        if inspected is None:
            return False, None
        state = inspected.get("State") or {}
        return bool(state.get("OOMKilled")), state.get("ExitCode") if isinstance(state.get("ExitCode"), int) else None

    def _structured_runner_result(
        self,
        raw: bytes,
        *,
        request: IsolatedCodeExecutionRequest,
        duration_ms: int,
        container_exit_code: int | None,
    ) -> IsolatedCodeExecutionResult:
        try:
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
            expected = {
                "duration_ms",
                "exit_code",
                "outcome",
                "result",
                "stderr_tail",
                "stdout_tail",
                "truncated",
            }
            if set(payload) != expected:
                raise ValueError
            result = IsolatedCodeExecutionResult(
                outcome=payload["outcome"],
                exit_code=payload["exit_code"],
                result=payload["result"],
                stdout_tail=payload["stdout_tail"],
                stderr_tail=payload["stderr_tail"],
                truncated=payload["truncated"],
                duration_ms=duration_ms,
            )
            if len(result.stdout_tail.encode("utf-8")) > request.limits.max_stdout_bytes:
                raise ValueError
            if len(result.stderr_tail.encode("utf-8")) > request.limits.max_stderr_bytes:
                raise ValueError
            if len(result.stdout_tail.encode("utf-8")) + len(result.stderr_tail.encode("utf-8")) > request.limits.max_total_log_bytes:
                raise ValueError
            if result.result is not None:
                result_size = len(canonical_json_value(result.result).encode("utf-8"))
                if result_size > request.limits.max_result_bytes:
                    return IsolatedCodeExecutionResult(
                        outcome="output_limit",
                        exit_code=1,
                        result=None,
                        stdout_tail=result.stdout_tail,
                        stderr_tail=result.stderr_tail,
                        truncated=True,
                        duration_ms=duration_ms,
                    )
            if container_exit_code not in {None, 0}:
                raise ValueError
            return result
        except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return IsolatedCodeExecutionResult(
                outcome="runtime_error",
                exit_code=container_exit_code,
                result=None,
                stdout_tail="",
                stderr_tail="runner returned an invalid structured result",
                truncated=len(raw) >= _MAX_DOCKER_OUTPUT_BYTES,
                duration_ms=duration_ms,
            )

    def execute(
        self,
        lease: IsolatedCodeExecutionLease,
        request: IsolatedCodeExecutionRequest,
        control: CodeExecutionControl,
    ) -> IsolatedCodeExecutionResult:
        if lease.activation_digest != request.activation.digest() or lease.profile_digest != request.profile_digest or lease.resource_id not in self._active_resources:
            raise DockerCodeProfileError("invalid or inactive Workflow Code lease")
        payload = canonical_json_value(request.runner_envelope()).encode("utf-8")
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                [self._docker_binary, "container", "start", "--attach", "--interactive", lease.resource_id],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise DockerCodeProfileError("Docker execution did not start") from exc
        if process.stdin is None or process.stdout is None or process.stderr is None:
            self._kill(lease.resource_id)
            raise DockerCodeProfileError("Docker execution pipes were not created")
        overflow = threading.Event()
        stdout_reader = _BoundedPipeReader(process.stdout, limit=_MAX_DOCKER_OUTPUT_BYTES, overflow=overflow)
        stderr_reader = _BoundedPipeReader(process.stderr, limit=64 * 1024, overflow=overflow)
        stdin_writer = threading.Thread(target=self._feed_stdin, args=(process.stdin, payload), daemon=True)
        stdout_reader.start()
        stderr_reader.start()
        stdin_writer.start()
        interruption = None
        timed_out = False
        while process.poll() is None:
            interruption = control.interruption()
            if interruption is not None or overflow.is_set():
                self._kill(lease.resource_id)
                break
            if (time.monotonic() - started) * 1000 >= request.limits.wall_timeout_ms:
                timed_out = True
                self._kill(lease.resource_id)
                break
            time.sleep(0.02)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._kill(lease.resource_id)
            try:
                process.kill()
            except OSError:
                pass
            process.wait(timeout=5)
        stdout_reader.join(timeout=5)
        stderr_reader.join(timeout=5)
        stdin_writer.join(timeout=1)
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        oom_killed, container_exit_code = self._terminal_state(lease.resource_id)
        if interruption is not None:
            return IsolatedCodeExecutionResult(
                outcome="cancelled",
                exit_code=container_exit_code,
                result=None,
                stdout_tail="",
                stderr_tail="",
                truncated=False,
                duration_ms=duration_ms,
                interruption=interruption,
            )
        if timed_out:
            return IsolatedCodeExecutionResult(
                outcome="timeout",
                exit_code=container_exit_code,
                result=None,
                stdout_tail="",
                stderr_tail="",
                truncated=False,
                duration_ms=duration_ms,
            )
        if oom_killed:
            return IsolatedCodeExecutionResult(
                outcome="resource_exhausted",
                exit_code=container_exit_code,
                result=None,
                stdout_tail="",
                stderr_tail="",
                truncated=False,
                duration_ms=duration_ms,
            )
        if overflow.is_set():
            return IsolatedCodeExecutionResult(
                outcome="output_limit",
                exit_code=container_exit_code,
                result=None,
                stdout_tail="",
                stderr_tail="",
                truncated=True,
                duration_ms=duration_ms,
            )
        return self._structured_runner_result(
            stdout_reader.value(),
            request=request,
            duration_ms=duration_ms,
            container_exit_code=container_exit_code,
        )

    def cleanup(self, lease: IsolatedCodeExecutionLease, *, reason: str) -> CodeCleanupReceipt:
        allowed_reasons = {
            "completed",
            "failed",
            "timeout",
            "cancelled",
            "lease_lost",
            "worker_crash_reconcile",
            "cleanup_retry",
        }
        if reason not in allowed_reasons:
            raise ValueError("invalid Workflow Code cleanup reason")
        try:
            self._run_cli(
                ["container", "rm", "--force", "--volumes", lease.resource_id],
                check=False,
                timeout=15.0,
            )
            absent = self._inspect_container(lease.resource_id) is None
        except DockerCodeProfileError:
            absent = False
        # Once cleanup has been attempted the execution barrier owns no live
        # candidate.  A failed/uncertain destroy must become reconcilable in
        # this same process rather than remaining hidden as an active lease.
        with self._lock:
            self._active_resources.discard(lease.resource_id)
        if absent:
            return CodeCleanupReceipt(
                lease_id=lease.lease_id,
                state="destroyed_confirmed",
                reason=reason,
            )
        return CodeCleanupReceipt(
            lease_id=lease.lease_id,
            state="cleanup_pending",
            reason=reason,
        )

    def reconcile_orphans(self) -> tuple[CodeCleanupReceipt, ...]:
        completed = self._run_cli(
            [
                "container",
                "ls",
                "--all",
                "--filter",
                f"label={_MANAGED_LABEL}=true",
                "--filter",
                f"label={_CONTRACT_LABEL}={CODE_RUNTIME_CONTRACT}",
                "--format",
                "{{.Names}}",
            ]
        )
        receipts: list[CodeCleanupReceipt] = []
        for resource_id in sorted(filter(None, completed.stdout.splitlines())):
            with self._lock:
                if resource_id in self._active_resources:
                    continue
            inspected = self._inspect_container(resource_id)
            if inspected is None:
                continue
            labels = (inspected.get("Config") or {}).get("Labels") or {}
            owner_id = labels.get(_OWNER_LABEL)
            if owner_id != self._owner_lease.owner_id and not self._owner_lease.owner_is_inactive(owner_id):
                continue
            lease_id = labels.get(_LEASE_LABEL)
            activation_digest = labels.get(_ACTIVATION_LABEL)
            profile_digest = labels.get(_PROFILE_LABEL)
            try:
                lease = IsolatedCodeExecutionLease(
                    lease_id=lease_id,
                    activation_digest=activation_digest,
                    profile_digest=profile_digest,
                    resource_id=resource_id,
                )
            except Exception:
                # It still carries the exact managed+contract labels. Remove
                # the malformed orphan fail-closed, but do not fabricate a
                # lease receipt that could be persisted against another run.
                self._run_cli(["container", "rm", "--force", "--volumes", resource_id], check=False)
                continue
            receipts.append(self.cleanup(lease, reason="worker_crash_reconcile"))
        return tuple(receipts)

    def reconcile_provisioning(
        self,
        *,
        lease_id: str,
        reconciliation_key_hash: str,
    ) -> CodeCleanupReceipt:
        """Reconcile exactly one journaled pre-running allocation."""

        if (
            not isinstance(lease_id, str)
            or not lease_id
            or len(lease_id) > 128
            or not isinstance(reconciliation_key_hash, str)
            or len(reconciliation_key_hash) != 64
            or any(character not in "0123456789abcdef" for character in reconciliation_key_hash)
        ):
            raise ValueError("invalid Workflow Code provisioning reconciliation handle")
        with _provisioning_operation_lease(
            lease_id,
            reconciliation_key_hash,
            blocking=False,
        ) as acquired:
            if not acquired:
                return CodeCleanupReceipt(
                    lease_id=lease_id,
                    state="cleanup_pending",
                    reason="worker_crash_reconcile",
                )
            return self._reconcile_provisioning_locked(
                lease_id=lease_id,
                reconciliation_key_hash=reconciliation_key_hash,
            )

    def _reconcile_provisioning_locked(
        self,
        *,
        lease_id: str,
        reconciliation_key_hash: str,
    ) -> CodeCleanupReceipt:
        all_absent = False
        # The reservation flock proves no create RPC can still be in flight.
        # Repeated exact-label absence then protects against transient daemon
        # visibility errors without opening a late-create race.
        for observation in range(3):
            completed = self._run_cli(
                [
                    "container",
                    "ls",
                    "--all",
                    "--filter",
                    f"label={_MANAGED_LABEL}=true",
                    "--filter",
                    f"label={_CONTRACT_LABEL}={CODE_RUNTIME_CONTRACT}",
                    "--filter",
                    f"label={_LEASE_LABEL}={lease_id}",
                    "--filter",
                    f"label={_RECONCILIATION_LABEL}={reconciliation_key_hash}",
                    "--format",
                    "{{.Names}}",
                ]
            )
            resources = sorted(filter(None, completed.stdout.splitlines()))
            all_absent = not resources
            for resource_id in resources:
                inspected = self._inspect_container(resource_id)
                if inspected is None:
                    all_absent = False
                    continue
                labels = (inspected.get("Config") or {}).get("Labels") or {}
                if labels.get(_LEASE_LABEL) != lease_id or labels.get(_RECONCILIATION_LABEL) != reconciliation_key_hash:
                    raise DockerCodeProfileError("provisioning reconciliation labels drifted")
                self._run_cli(
                    ["container", "rm", "--force", "--volumes", resource_id],
                    check=False,
                    timeout=15.0,
                )
                if self._inspect_container(resource_id) is not None:
                    all_absent = False
                with self._lock:
                    self._active_resources.discard(resource_id)
            if observation < 2:
                time.sleep(0.25)
        return CodeCleanupReceipt(
            lease_id=lease_id,
            state="destroyed_confirmed" if all_absent else "cleanup_pending",
            reason="worker_crash_reconcile",
        )

    def release_provisioning_handle(
        self,
        *,
        lease_id: str,
        reconciliation_key_hash: str,
    ) -> None:
        """Best-effort unlink after the database has committed ``destroyed``."""

        path, uid = _provisioning_lease_path(lease_id, reconciliation_key_hash)
        if not path.exists():
            return
        flags = os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        lease_file = os.fdopen(descriptor, "r+")
        try:
            file_stat = os.fstat(lease_file.fileno())
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_uid != uid:
                raise DockerCodeProfileError("Workflow Code provisioning lease file is not trusted")
            try:
                fcntl.flock(lease_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            path_stat = path.lstat()
            if path_stat.st_ino != file_stat.st_ino or path_stat.st_dev != file_stat.st_dev:
                raise DockerCodeProfileError("Workflow Code provisioning lease file changed")
            path.unlink()
        finally:
            lease_file.close()

    def close(self) -> None:
        """Release the process-owner fence after all resources are settled."""

        with self._lock:
            if self._active_resources:
                raise DockerCodeProfileError("cannot close a provider with active Workflow Code resources")
            self._owner_lease.close()


__all__ = [
    "DockerCodeProfileError",
    "DockerIsolatedCodeExecutionProvider",
]
