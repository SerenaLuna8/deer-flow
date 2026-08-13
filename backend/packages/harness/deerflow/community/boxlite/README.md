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
  environment:
    PYTHONUNBUFFERED: "1"
```

`replicas` is a soft cap across active and warm VMs owned by the provider.
Warm VMs are evicted before active ones. `idle_timeout: 0` disables idle
reaping; `health_check_skip_seconds: 0` keeps validation on every warm reuse.

## Lifecycle and isolation

BoxLite's SDK is async and event-loop-affine while ActWeave's `Sandbox`
interface is synchronous. `BoxliteProvider` owns one private asyncio loop on a
daemon thread and marshals SDK calls to it.

- Legacy thread-scoped acquisition derives a deterministic identity from user
  and thread and may reclaim a validated warm VM.
- Private Run acquisition derives a fresh identity from project, owner, thread,
  Run, and a nonce; it mounts admitted read-only resources and requires strict
  destruction instead of warm reuse.
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
