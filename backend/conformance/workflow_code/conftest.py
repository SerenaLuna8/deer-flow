"""Real-Docker fixtures for the Workflow Code STOP-gate suite.

This suite intentionally lives outside ``backend/tests``.  It is a deployment
capability gate that must fail when Docker or the fixed base image is absent;
it must never turn into a skipped/mock unit test in the zero-skip core suite.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from deerflow.workflows.code_execution.docker_provider import DockerIsolatedCodeExecutionProvider

try:
    import fcntl
except ImportError:  # pragma: no cover - this conformance profile is POSIX-only
    fcntl = None  # type: ignore[assignment]

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
RUNNER_CONTEXT = REPOSITORY_ROOT / "docker" / "workflow-code-runner"
RUNNER_SOURCE = RUNNER_CONTEXT / "runner.py"
IMAGE_TAG_PREFIX = "actweave/workflow-code-runner:python3.12-v1-conformance"
MANAGED_LABEL = "org.actweave.workflow-code.managed=true"


def docker(*arguments: str, check: bool = True, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *arguments],
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.fixture(scope="session")
def runner_digest() -> str:
    return hashlib.sha256(RUNNER_SOURCE.read_bytes()).hexdigest()


@pytest.fixture(scope="session", autouse=True)
def _workflow_code_conformance_session_fence() -> Iterator[None]:
    """Serialize host-global Docker lifecycle assertions across pytest sessions."""

    if fcntl is None:
        raise RuntimeError("Workflow Code Docker conformance requires POSIX advisory locks")
    uid = os.getuid() if hasattr(os, "getuid") else 0
    root = Path(tempfile.gettempdir()) / f"actweave-workflow-code-conformance-{uid}"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root_stat = root.lstat()
    if not stat.S_ISDIR(root_stat.st_mode) or root_stat.st_uid != uid:
        raise RuntimeError("Workflow Code conformance lock directory is not trusted")
    root.chmod(0o700)
    path = root / "session.lock"
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    lock_file = os.fdopen(descriptor, "r+")
    try:
        lock_stat = os.fstat(lock_file.fileno())
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_uid != uid:
            raise RuntimeError("Workflow Code conformance lock file is not trusted")
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


@pytest.fixture(scope="session")
def runner_image_id(
    runner_digest: str,
    _workflow_code_conformance_session_fence: None,
) -> Iterator[str]:
    del _workflow_code_conformance_session_fence
    session_image_tag = f"{IMAGE_TAG_PREFIX}-{os.getpid()}"
    docker("version", timeout=15)
    docker(
        "build",
        "--pull=false",
        "--network",
        "none",
        "--build-arg",
        f"RUNNER_DIGEST={runner_digest}",
        "--tag",
        session_image_tag,
        str(RUNNER_CONTEXT),
        timeout=120,
    )
    inspected = docker("image", "inspect", session_image_tag)
    payload = json.loads(inspected.stdout)
    assert isinstance(payload, list) and len(payload) == 1
    image_id = payload[0]["Id"]
    assert isinstance(image_id, str) and image_id.startswith("sha256:")
    yield image_id


@pytest.fixture
def docker_provider(
    runner_image_id: str,
    runner_digest: str,
) -> Iterator[DockerIsolatedCodeExecutionProvider]:
    provider = DockerIsolatedCodeExecutionProvider(
        image_id=runner_image_id,
        runner_digest=runner_digest,
    )
    provider.reconcile_orphans()
    yield provider
    provider.reconcile_orphans()
    if not provider._active_resources:
        provider.close()
