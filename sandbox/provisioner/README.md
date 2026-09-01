# ActWeave Sandbox Provisioner

The Sandbox Provisioner is an optional FastAPI control service for the
Kubernetes Sandbox provider. It creates one Pod and Service per sandbox for the
Worker; it is not the Agent executor and is not a Kubernetes deployment target
for the complete ActWeave stack.

Run this service independently, then configure `AioSandboxProvider` with a
Worker-reachable `provisioner_url`.

```yaml
sandbox:
  use: deerflow.community.aio_sandbox:AioSandboxProvider
  provisioner_url: http://127.0.0.1:8002
  provisioner_api_key: $PROVISIONER_API_KEY
```

## Architecture

```text
Worker ---- control API ----> Provisioner :8002 ----> Kubernetes API
  |                                  |                    |
  +---------- sandbox HTTP ----------+--------------> Pod + Service
```

1. Worker sends a scoped sandbox identity to the Provisioner.
2. Provisioner creates a Pod, mounts Skills and user-data, and creates a
   `ClusterIP` or `NodePort` Service.
3. Worker calls the returned sandbox URL directly.
4. Release deletes both Service and Pod.

The Kubernetes Python client is synchronous. Sandbox CRUD handlers therefore
remain synchronous FastAPI handlers so Starlette runs them in its worker pool;
do not convert them to `async def` without using an async client or explicit
offloading.

## Security boundary

- `GET /health` is public. Every `/api/*` request requires `X-API-Key` matching
  `PROVISIONER_API_KEY`; an empty, missing, or mismatched key fails with `401`.
- Do not expose port `8002` to browsers or the public network. Normal lifecycle
  calls originate from Worker over a private host or cluster network.
- The mounted kubeconfig or service account can read/create namespaces and
  create/delete Pods and Services. Give it only the required cluster
  permissions and run the service in a trusted environment.
- HostPath mounts expose node files. Prefer controlled PVCs for durable or
  multi-node environments and validate every storage boundary on the target
  cluster.
- The current Pod sets `privileged=false` but permits privilege escalation. This
  is not a hardened untrusted multi-tenant profile; add and validate the target
  cluster's SecurityContext, admission, NetworkPolicy, and runtime isolation.
- The example image uses `:latest`. Pin a reviewed tag or digest outside local
  development.
- Setting `K8S_API_SERVER` disables TLS certificate verification in the current
  implementation. Use that override only for a trusted local cluster; do not use
  it across an untrusted network.

## Requirements

- A reachable Kubernetes cluster (Docker Desktop, OrbStack, minikube, kind,
  k3s, or an in-cluster deployment).
- A kubeconfig mounted as a regular file, or a valid in-cluster service account.
- Permissions to read/create the namespace; create/read/delete Pods and
  Services; and list Services in the configured namespace.
- HostPath directories visible to the Kubernetes node, or pre-created PVCs.
- The same non-empty `PROVISIONER_API_KEY` in Worker and Provisioner.

The client loads `KUBECONFIG_PATH` first and falls back to in-cluster config when
the file is absent.

## Configuration

Set environment variables in the standalone process or in the external
Kubernetes workload that runs the Provisioner.

| Variable                      | Default                | Purpose                                                      |
| ----------------------------- | ---------------------- | ------------------------------------------------------------ |
| `K8S_NAMESPACE`               | `deer-flow`            | Namespace for sandbox resources                              |
| `SANDBOX_IMAGE`               | AIO `:latest` image    | Pod image; pin for controlled environments                   |
| `SANDBOX_CONTAINER_PORT`      | `8080`                 | AIO sandbox HTTP port                                        |
| `SANDBOX_SERVICE_TYPE`        | `ClusterIP`            | `ClusterIP` or `NodePort`                                    |
| `NODE_HOST`                   | `host.docker.internal` | Worker-visible host for NodePort mode                        |
| `KUBECONFIG_PATH`             | `/root/.kube/config`   | Kubeconfig path inside the container                         |
| `K8S_API_SERVER`              | unset                  | Trusted-local API-server override; disables TLS verification |
| `PROVISIONER_API_KEY`         | empty                  | Shared control key; empty disables all `/api/*` calls        |
| `SKILLS_HOST_PATH`            | `/skills`              | HostPath base for public and optional legacy Skills          |
| `ACT_WEAVE_HOST_BASE_DIR`     | `/.deer-flow`          | HostPath base for per-user custom Skills                     |
| `THREADS_HOST_PATH`           | `/.deer-flow/threads`  | HostPath base for thread user-data                           |
| `SKILLS_PVC_NAME`             | empty                  | PVC replacing Skill HostPaths                                |
| `SKILLS_PVC_SUBPATH_TEMPLATE` | empty                  | Optional Skill PVC subpath using `{user_id}`/`{thread_id}`   |
| `USERDATA_PVC_NAME`           | empty                  | PVC for user-data with scoped subpath                        |

Choose `NodePort` only when a Worker outside the cluster must reach Sandbox
services. When Worker and Provisioner run inside the same cluster, keep the
safer `ClusterIP` default and enforce NetworkPolicy.

### Custom sandbox image

Provisioner Pods use the `SANDBOX_IMAGE` environment variable, not
`sandbox.image` from `config.yaml`. A custom image must implement the AIO
sandbox HTTP contract consumed by `agent-sandbox`, listen on
`SANDBOX_CONTAINER_PORT`, and keep `/mnt/user-data` writable. Prefer extending
the default image and pin the reviewed result by tag or digest.

### Mount layout

HostPath mode mounts:

- `SKILLS_HOST_PATH/public` -> `/mnt/skills/public` read-only;
- `ACT_WEAVE_HOST_BASE_DIR/users/{user_id}/skills/custom` ->
  `/mnt/skills/custom` read-only;
- optional `SKILLS_HOST_PATH/custom` -> `/mnt/skills/legacy` read-only when
  `include_legacy_skills=true`;
- `THREADS_HOST_PATH/{thread_id}/user-data` -> `/mnt/user-data` read-write.

Skill PVC mode currently uses one read-only `/mnt/skills` mount; a configured
subpath template may scope it. User-data PVC mode uses
`deer-flow/users/{user_id}/threads/{thread_id}/user-data`.

## Control API

| Method   | Path                          | Purpose                           |
| -------- | ----------------------------- | --------------------------------- |
| `GET`    | `/health`                     | Public process health             |
| `POST`   | `/api/sandboxes`              | Idempotently create Pod + Service |
| `GET`    | `/api/sandboxes/{sandbox_id}` | Read status and access URL        |
| `GET`    | `/api/sandboxes`              | List managed sandboxes            |
| `DELETE` | `/api/sandboxes/{sandbox_id}` | Delete Service + Pod              |

Create request:

```json
{
  "sandbox_id": "sandbox-001",
  "thread_id": "thread-001",
  "user_id": "user-001",
  "include_legacy_skills": false
}
```

`thread_id` and `user_id` accept only letters, digits, `_`, and `-`.
`user_id` defaults to `default` for compatibility. The response contains
`sandbox_id`, `sandbox_url`, and Kubernetes phase. Reusing an existing
`sandbox_id` returns the existing resource.

## Start and smoke check

Build and start the standalone Provisioner container, then configure the Worker
with the same shared key:

```bash
docker build -t actweave-sandbox-provisioner:local sandbox/provisioner
docker run --rm --name actweave-sandbox-provisioner \
  -p 127.0.0.1:8002:8002 \
  -e PROVISIONER_API_KEY \
  -e KUBECONFIG_PATH=/root/.kube/config \
  -v "$HOME/.kube/config:/root/.kube/config:ro" \
  actweave-sandbox-provisioner:local
curl -fsS http://127.0.0.1:8002/health
kubectl get pod,svc -n deer-flow -l app=deer-flow-sandbox
```

For an in-cluster installation, deploy the same image with a scoped service
account and expose it only to Worker through a private Service. A local
kubeconfig whose server is loopback may require the trusted-local
`K8S_API_SERVER` override described above.

## Troubleshooting

| Symptom                      | Check                                                                                |
| ---------------------------- | ------------------------------------------------------------------------------------ |
| Kubeconfig missing/directory | Mount a real file at `KUBECONFIG_PATH`, or provide in-cluster auth                   |
| Kubernetes API refused       | Inspect the kubeconfig server; use `K8S_API_SERVER` only for a trusted local cluster |
| Pod create rejected          | Verify namespace RBAC, image, resource policy, and absolute HostPath values          |
| Pod stuck creating           | Inspect `kubectl describe pod`, image pull, volumes, and node readiness              |
| Worker cannot reach sandbox  | Verify NodePort `NODE_HOST`, or ClusterIP DNS/NetworkPolicy from Worker              |
| Control API returns `401`    | Ensure both processes use the same non-empty `PROVISIONER_API_KEY`                   |

There is no automated real-cluster gate dedicated to this service. Treat source
tests and documentation checks as local evidence only; Kubernetes connectivity,
RBAC, storage, image compatibility, networking, and isolation require a smoke
test in the target cluster.
