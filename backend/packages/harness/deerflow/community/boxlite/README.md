# BoxLite Sandbox Provider

This optional provider runs ActWeave sandboxes in
[BoxLite](https://github.com/boxlite-ai/boxlite) micro-VMs. It is not the
default provider and is available only on platforms where BoxLite publishes a
compatible runtime and the host can boot micro-VMs.

## Requirements

- Linux with KVM (including nested virtualization when required), or macOS with
  Hypervisor.framework.
- The optional harness extra:

```bash
pip install "deerflow-harness[boxlite]"
```

Unsupported hosts should select another Sandbox provider.

## Configuration

```yaml
sandbox:
  use: deerflow.community.boxlite:BoxliteProvider
  image: python:3.12-slim
  memory_mib: 1024
  cpus: 2
  replicas: 3
  idle_timeout: 600
  health_check_skip_seconds: 0.0
  boxlite_p04_v1_verified: false
  environment:
    PYTHONUNBUFFERED: "1"
```

`replicas` is a soft cap across active and warm VMs owned by the provider.
Warm VMs are evicted before active ones. `idle_timeout: 0` disables idle
reaping; `health_check_skip_seconds: 0` keeps validation on every warm reuse.
`boxlite_p04_v1_verified` is a strict, versioned release attestation. Keep it
`false` until the real P-04 provider-integration test succeeds on this exact
Linux/KVM target; a quoted string such as `"true"` is rejected. General legacy
BoxLite use may run on a supported macOS host, but that does not satisfy or
enable the release-specific P-04 v4 Run Skill gate.

## Lifecycle and isolation

BoxLite's SDK is async and event-loop-affine while ActWeave's `Sandbox`
interface is synchronous. `BoxliteProvider` owns one private asyncio loop on a
daemon thread and marshals SDK calls to it.

- Legacy thread-scoped acquisition derives a deterministic identity from user
  and thread and may reclaim a validated warm VM.
- Private Run acquisition derives a fresh identity from project, owner, thread,
  Run, and a nonce; it mounts admitted read-only resources and requires strict
  destruction instead of warm reuse.
- Typed v4 Run Skill acquisition accepts only a validated materializer-owned
  source, exposes it at `/mnt/skills`, and labels the Run VM with the exact
  opaque owner coordinate. Readback executes as the non-root `deerflow_agent`
  identity; release returns proof only after the BoxLite registry confirms the
  exact VM is absent. Owner reconciliation uses `list_info` plus exact remove
  and a second absence readback across Worker processes.
- Provider startup reconciles orphaned BoxLite instances it owns.
- `/mnt/user-data/{workspace,uploads,outputs}` and `/mnt/skills` are created in
  each VM before use.

## Supported operations

The provider implements command execution plus `read_file`, `write_file`,
`update_file`, `download_file`, `list_dir`, `glob`, and `grep`. File operations
run inside the VM, remain under ActWeave path validation, and apply the same
bounded-result contracts as other Sandbox providers.

## Operational limits

- Treat the configured OCI image as trusted infrastructure and pin a reviewed
  tag or digest in controlled environments.
- Host virtualization, image availability, and resource behavior are
  environment-specific; validate them on the target Linux/KVM or macOS host.
- Selecting BoxLite changes the execution isolation mechanism, not ActWeave's
  project/owner/Run authorization. Those checks remain server-side boundaries.
