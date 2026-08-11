# Workflow Python Code runner

This image is the fixed `python3.12-v1` execution payload for the independent
Workflow Code provider. It is not the Agent AIO sandbox image and does not read
`config.yaml`, `sandbox.environment`, mounts, host Bash policy, or project
credentials.

The Dockerfile pins the Python 3.12 base manifest. The build must pass the
SHA-256 of `runner.py` as `RUNNER_DIGEST`; the resulting local immutable image
ID, runner digest, and hardening contract form the Worker profile attestation.
The platform administrator later selects that exact attested profile through
the PostgreSQL `workflow_runtime` System Setting. A tag is never a runtime
authority.

The real STOP-gate suite builds with `--pull=false --network none` and then
runs hostile source in fresh containers:

```bash
cd backend
.venv/bin/pytest -q conformance/workflow_code/test_docker_profile.py -vv
```

It verifies empty process environment, no host/Thread/Skill/custom mounts, no
Docker/containerd/Kubernetes token, deny-all networking and DNS, non-root,
read-only rootfs, default seccomp with all capabilities dropped, cgroup CPU,
memory and PID limits, bounded tmpfs/log/result/wall time, cancellation and
lease-loss kill, destroy confirmation, and crash-orphan reconciliation.

The certified Docker Spike profile uses a POSIX advisory owner lease to avoid
one local Worker reaping another live Worker's container. It is therefore
ready only when competing native Worker processes share the same local
temporary filesystem. Compose replicas with isolated filesystems and
Kubernetes Workers are deliberately **not** attested by this profile; they
must use a durable control-plane/Provisioner fence and their own real-cluster
gate before administrators can select them.

Do not add shell/command/argv/environment/mount options to this image or adapt
`Sandbox.execute_command()` to it. A future Kubernetes/Provisioner profile
must implement the same typed provider and pass a real cluster conformance
suite before it can advertise readiness.
