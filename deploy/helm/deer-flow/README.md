# DeerFlow Helm Chart

This chart deploys the final project-scoped topology: nginx/frontend, Gateway, Worker, optional Scheduler, PostgreSQL connectivity and optional Provisioner.

## Required values

- `config.database.url` or the chart PostgreSQL Secret.
- at least one model definition and its provider secret.
- sandbox mode and Provisioner settings when Kubernetes sandboxes are used.
- independent Gateway/Worker replica and resource settings.

The target database must already exist and be migrated. Application processes validate schema but do not create or upgrade the database.

## Install

```bash
helm install deer-flow deploy/helm/deer-flow \
  --namespace deer-flow \
  --create-namespace
```

## Process ownership

- Gateway owns HTTP admission, queries and SSE reading.
- Worker owns Agent graph execution and durable stream writing.
- Scheduler, when enabled, owns its PostgreSQL session lock and Automation polling.
- PostgreSQL is authoritative for private work, jobs, streams, quota and audit state.

Horizontal scaling uses the same PostgreSQL authority and process readiness probes.

## Storage

Sandbox/object bytes may use configured persistent storage, while project metadata and Memory remain in PostgreSQL.

## Upgrade

Follow the current release notes and migration guide for the version being installed. Stop writers during schema maintenance, run database checks and required probes, then restart Gateway, Worker and Scheduler.
