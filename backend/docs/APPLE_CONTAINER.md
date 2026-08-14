# Apple Container Sandbox

ActWeave can run Agent shell and Python work inside Apple Container instead of
directly in the macOS Worker process. This is the recommended local setup on a
supported Apple-silicon Mac when Agent-generated code must execute.

## Security boundary

Apple Container gives every Linux container its own lightweight virtual
machine. The Agent command, Python interpreter, processes, and writable root
filesystem therefore run outside the macOS host process namespace.

This is real process/filesystem isolation, but it is not an unrestricted trust
boundary:

- Every configured bind mount exposes that host path to the container with the
  configured read/write mode. Run-scoped Skill mounts are read-only.
- The default container network permits outbound traffic; ActWeave does not yet
  apply a per-Run egress policy.
- The host-side Worker can reach the AIO control API on Apple Container's
  private VM network. Other devices cannot route into that network by default.
- `allow_host_bash: false` keeps `LocalSandboxProvider` host Bash disabled. It
  does not disable Bash inside the AIO container.
- AIO Bash does not enter the Local host-execution approval flow. AIO startup or
  command failure is reported as a failure and never falls back to Local host Bash.

Apple documents the dedicated-IP model in its
[container tutorial](https://github.com/apple/container/blob/main/docs/tutorial.md).

## Requirements

- Apple silicon
- macOS 26 or newer
- Apple Container 1.0 or newer
- Native execution of the Gateway and Worker on macOS. A Worker running inside
  Linux/Compose selects Docker instead.

Check and start the runtime:

```bash
container --version
container system status
container system start
```

`container system start` can perform one-time system preparation. Follow its
interactive instructions before starting ActWeave.

## ActWeave configuration

Use an explicit image tag. The `1.11.0` image includes the structured
`/v1/bash/exec` API required for per-command environment injection.

```yaml
sandbox:
  use: deerflow.community.aio_sandbox:AioSandboxProvider
  image: enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:1.11.0
  allow_host_bash: false
```

The image is multi-architecture; Apple Container selects its Linux/ARM64
manifest natively. ActWeave does not enable Rosetta for this image.

Pre-pull it before the first Agent Run:

```bash
container image pull \
  enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:1.11.0

container image inspect \
  enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:1.11.0
```

The image is large. Its first download and unpack can take several minutes;
subsequent container starts are much faster.

After changing `sandbox.use` or `sandbox.image`, restart at least the Worker.
For the local development stack, restart the full process set:

```bash
./scripts/serve.sh --restart --dev --skip-install
```

## Runtime behavior

On Darwin, the local AIO backend selects Apple Container when
`container --version` succeeds; otherwise it falls back to Docker. CLI
installation and service readiness are different checks: an installed CLI with
a stopped service produces an actionable runtime failure rather than silently
switching to Docker.

Apple Container 1.x is not Docker CLI-compatible. ActWeave uses:

- `container list --format json` for enumeration;
- `container inspect <name>` for state and network discovery;
- managed labels to distinguish ActWeave containers from prefix collisions;
- `container stop <name>` for cleanup; `--rm` removes the stopped container.

ActWeave intentionally does not publish the AIO API with `-p` on Apple
Container. Each container receives an address such as `192.168.64.5/24` on the
private `default` network, and the Worker connects directly to
`http://192.168.64.5:8080`. This follows Apple Container's dedicated-IP model
and avoids exposing a control port on the Mac. Ambient HTTP proxy variables are
ignored for loopback/private sandbox control traffic.

Consequences:

- `sandbox.port` and `DEER_FLOW_SANDBOX_BIND_HOST` apply to the Docker local
  backend, not Apple Container.
- A container without a valid managed label, running state, or default-network
  IPv4 address is not adopted after restart.
- Private Run containers are never adopted into the ordinary warm pool.

## Verification

Configuration and offline lifecycle contracts:

```bash
make doctor

cd backend
uv run pytest \
  tests/test_aio_local_container_backend.py \
  tests/test_aio_sandbox_file_errors.py -q
```

Those tests do not prove the local daemon or image is usable. A live check must
also confirm all of the following:

1. `container inspect` reports `status.state=running`, a default-network IPv4,
   and no published host port.
2. `GET http://<container-ip>:8080/v1/sandbox` returns HTTP 200 from the Mac.
3. File read/write and a Python command succeed through `AioSandboxProvider`.
4. A read-only Skill mount can be read but cannot be modified.
5. Stopping the test container removes it from `container list --all`.

## Cleanup

```bash
./scripts/cleanup-containers.sh deer-flow-sandbox
container list --all
```

The cleanup script only targets the exact configured prefix. Do not reuse that
prefix for unrelated containers.

## Troubleshooting

### CLI detected, but container creation fails

```bash
container system status
container system start
```

ActWeave does not treat a stopped Apple service as permission to run commands
on the host.

### Readiness cannot reach the container IP

Inspect the assigned address:

```bash
container list --format json
container inspect <container-name>
```

Apple's default network commonly uses `192.168.64.0/24`. A VPN or LAN route
using the same subnet can intercept host-to-container traffic. Resolve the
Apple Container network/subnet conflict; do not work around it by publishing
the unauthenticated AIO API on `0.0.0.0`.

### `/v1/bash/exec` returns 404

The selected image is too old. Pin `sandbox.image` to `1.11.0`, pull it, clean
up old `deer-flow-sandbox-*` containers, and restart the Worker. Containers
already in the warm pool retain the image with which they were created.

### Multiple Worker processes

Local creation is serialized, but warm-container ownership is process-local.
Use one native local Worker for this desktop setup. Multi-Worker production
deployments should use a provider with durable lifecycle leases, such as the
Kubernetes Provisioner mode.

## References

- [Apple Container project](https://github.com/apple/container)
- [Apple Container command reference](https://github.com/apple/container/blob/main/docs/command-reference.md)
- [OCI image specification](https://github.com/opencontainers/image-spec)
