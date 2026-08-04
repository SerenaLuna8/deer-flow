# ActWeave Helm Chart

This chart deploys the final project-scoped process topology:

- Nginx is the only public entry point.
- Frontend serves the Next.js application.
- Gateway owns authenticated HTTP admission, queries and durable SSE replay.
- Worker is the only Agent-graph executor.
- Scheduler is an optional, single-owner Automation admission process.
- Provisioner is an optional, in-cluster sandbox control plane.

Gateway never executes an Agent graph. PostgreSQL is authoritative for private
project data, jobs, streams, checkpoints, quotas and audit records.

## Required inputs

The default chart deliberately has no usable database target. Choose one:

1. Recommended: set `postgresql.external.existingSecret` to a Secret containing
   `database-url`; or
2. set `postgresql.external.databaseUrl` for controlled evaluation; or
3. explicitly set `postgresql.enabled=true` to create a bundled, **empty**
   PostgreSQL instance.

The database must have been initialized from an empty target by the checkout's
sole supported `make setup-db` workflow before Gateway, Worker or Scheduler
starts. Runtime Pods only validate `full_schema_v1`; they never create, migrate,
stamp, repair or delete schema. An older, unknown or non-empty unmanaged
database must be replaced with a new empty database rather than upgraded in
place.

You must also provide:

- an AIO-compatible sandbox image when Provisioner mode is enabled;
- a ReadWriteMany `persistence.home` volume when Gateway, Worker and sandbox
  Pods can be scheduled on different nodes.

After the initialized release is reachable, a system admin must sign in at
`/admin/settings/models`, create at least one active model, bind any required
provider Credential, and select the default. Model definitions and provider
secrets are PostgreSQL-backed; they are not Helm ConfigMap entries or provider
environment variables.

## Install with an external database

Create an application Secret through your normal secret manager:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: deer-flow-database
  namespace: deer-flow
type: Opaque
stringData:
  database-url: postgresql://postgres:REDACTED@postgres.example:5432/deerflow
```

Initialize that empty target from the exact checkout, then install:

```bash
POSTGRES_ADMIN_URL=postgresql://postgres:...@postgres.example:5432/postgres \
DATABASE_URL=postgresql://postgres:...@postgres.example:5432/deerflow \
  make setup-db
DATABASE_URL=postgresql://... make check-db

helm install deer-flow deploy/helm/deer-flow \
  --namespace deer-flow \
  --create-namespace \
  --set postgresql.external.existingSecret=deer-flow-database \
  -f my-values.yaml
```

`POSTGRES_ADMIN_URL` is a transient setup authority pointing at PostgreSQL's
`postgres` maintenance database; it is not an application runtime connection.
It may use the same PostgreSQL role as `DATABASE_URL`. Runtime Pods receive only
`DATABASE_URL`.

Do not put a production DSN in a committed values file. The
`postgresql.external.databaseUrl` convenience value is rendered into a Secret
manifest and is intended only for controlled environments.

## Bundled PostgreSQL is empty by design

`postgresql.enabled=true` creates a single PostgreSQL StatefulSet, not an
initialized ActWeave database. There is intentionally no Helm migration hook,
init container or runtime `create_all`.

For evaluation, install with Gateway and Worker scaled to zero, point the
checkout's `make setup-db` at the bundled database (for example through a
temporary port-forward), verify it with `make check-db`, then upgrade the
release to the desired replicas. Never start writers against an uninitialized
or legacy target.

## Worker and Scheduler

Worker uses the backend image with:

```text
python -m app.worker.app
```

It has no Service and no Kubernetes API token. `worker.replicas` scales the
lease-authorized execution fleet. `worker.terminationGracePeriodSeconds` must
remain longer than `config.worker.shutdown_grace_seconds` plus the bounded
Memory flush window.

Scheduler is absent by default. Setting:

```yaml
scheduler:
  enabled: true
```

both renders its Deployment and projects `scheduler.enabled=true` into the
shared AppConfig. The Deployment remains at one replica because PostgreSQL
grants a single process-lifetime Scheduler owner.

ConfigMap changes roll Gateway, Worker and enabled Scheduler through
`checksum/config` annotations.

## Secret boundaries

When `existingAppSecret` is empty, the chart generates independent values for:

- `AUTH_JWT_SECRET`
- `BETTER_AUTH_SECRET`
- `DEER_FLOW_AUDIT_ACTIVE_KEY_ID` and `DEER_FLOW_AUDIT_KEYRING_JSON`
- `DEER_FLOW_CREDENTIAL_ACTIVE_KEY_ID` and
  `DEER_FLOW_CREDENTIAL_KEYRING_JSON`
- `DEER_FLOW_INTERNAL_AUTH_TOKEN`
- `DEER_FLOW_PROXY_AUTH_TOKEN`
- `PROVISIONER_API_KEY`

`lookup` preserves each value independently across Helm upgrades. Uninstalling
the release can still delete the Secret, so production operators should supply
and back up an `existingAppSecret`, especially for credential-encryption and
audit keyrings. It must contain every key above.

Gateway, Worker and Scheduler load one AppConfig and receive only the database
Secret plus the explicit platform key domains above. There is no provider
`envFrom` Secret: exact provider Credentials remain encrypted in PostgreSQL and
are materialized only for the admitted model version at the execution boundary.
Frontend receives only Better Auth material, Nginx receives only
proxy-attestation material, and Provisioner receives only its control API key.
Provisioner rejects all `/api/*` requests when that key is absent or mismatched.

## Sandbox network exposure

`provisioner.sandboxServiceType` defaults to `ClusterIP`. Per-sandbox Services
then return an in-cluster DNS URL and do not bind the code-execution surface on
node interfaces. `NODE_HOST` is absent from the Provisioner Pod in this mode.

`NodePort` is an explicit hybrid-mode escape hatch for a Worker outside the
cluster:

```yaml
provisioner:
  sandboxServiceType: NodePort
  nodeHost: 192.0.2.10
```

Only this mode injects `NODE_HOST`. The Provisioner control Service itself
always remains `ClusterIP`.

Provisioner is the only workload with a ServiceAccount token and Kubernetes
RBAC. Its namespaced Role can get/create/delete sandbox Pods and Services; its
ClusterRole is limited to ensuring the configured namespace exists. Other
workloads disable service-account token automounting.

## Render and verify

Render checks require an explicit database choice:

```bash
helm lint deploy/helm/deer-flow \
  --set postgresql.external.databaseUrl=postgresql://postgres:test@db.invalid/deerflow

helm template deer-flow deploy/helm/deer-flow \
  --set postgresql.external.databaseUrl=postgresql://postgres:test@db.invalid/deerflow

helm template deer-flow deploy/helm/deer-flow \
  --set postgresql.external.databaseUrl=postgresql://postgres:test@db.invalid/deerflow \
  --set scheduler.enabled=true \
  --set provisioner.sandboxServiceType=NodePort
```

After installation, verify the final schema first, then confirm that Gateway is
ready, at least one Worker heartbeat is current, and Scheduler ownership is
either `disabled` or `owned`. A healthy Gateway without a Worker is not a
healthy ActWeave execution topology.
