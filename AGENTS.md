# AGENTS.md

This file is the repository-level guide for coding agents. Keep it short and
navigational. Module-specific architecture and invariants belong to:

- [backend/AGENTS.md](backend/AGENTS.md) — runtime, authorization, persistence,
  configuration, assets, and backend tests.
- [frontend/AGENTS.md](frontend/AGENTS.md) — routes, client scope, data flow,
  UI ownership, and frontend tests.

For setup and operator-facing usage, read [README.md](README.md) and
[Install.md](Install.md).

> The three repository `AGENTS.md` files are development-time guidance only.
> They are not packaged or read by ActWeave at runtime. A project Agent's
> `AGENTS.md`, `SOUL.md`, `IDENTITY.md`, and `USER.md` are immutable database
> fields, not these filesystem guides. Likewise, `skills/public/*/SKILL.md`
> contains runtime Skill assets; local coding-agent skills do not.

## System overview

ActWeave is a project-first full-stack agent system. Nginx is the single browser
entry, Gateway owns HTTP admission and authorization, Worker alone executes
Agent graphs, and Scheduler only admits due Automations.

| Service     |   Port | Responsibility                                              |
| ----------- | -----: | ----------------------------------------------------------- |
| Nginx       | `2026` | Public entry; frontend plus `/api/*` proxy                  |
| Frontend    | `3000` | Next.js web UI                                              |
| Gateway     | `8001` | Auth, project/admin APIs, Run admission, durable SSE replay |
| Worker      |      — | Sole Agent-graph executor and job worker                    |
| Scheduler   |      — | Optional Automation admission process                       |
| Provisioner | `8002` | Optional Kubernetes Sandbox control service                 |

Gateway never executes an Agent graph. Worker exposes no browser business API.
Provisioner is a Sandbox provider, not a Kubernetes deployment target for the
complete application.

## Repository map

| Path                        | Owner                                                             |
| --------------------------- | ----------------------------------------------------------------- |
| `backend/app/`              | Gateway, Worker, Scheduler, and application domains               |
| `backend/packages/harness/` | Agent harness, tools, sandbox, and persistence primitives         |
| `frontend/`                 | Next.js application and browser tests                             |
| `docker/`                   | Compose, Nginx, and optional Sandbox Provisioner                  |
| `skills/public/`            | Sole source of packaged System Skill definitions                  |
| `scripts/`                  | Setup, diagnostics, local runtime, and Compose deployment helpers |
| `backend/docs/`             | Current backend architecture, API, and operations references      |
| `config.example.yaml`       | Root runtime configuration template                               |

Runtime configuration is resolved from the repository-root `config.yaml` (or an
explicit `DEER_FLOW_CONFIG_PATH`). PostgreSQL is the authority for application
metadata, project/private state, jobs, streams, checkpoints, audit, and governed
asset versions. System model, runtime, authentication, Memory-template, and
quota policies are administered in PostgreSQL, not duplicated in YAML.

Fresh schema installation and explicit upgrades are separate operator actions.
The runtime never creates, upgrades, stamps, or repairs the application schema;
see [backend/AGENTS.md](backend/AGENTS.md) before changing persistence.

## Command boundary

Run whole-application commands from the repository root:

| Command                   | Purpose                                                           |
| ------------------------- | ----------------------------------------------------------------- |
| `make setup`              | Interactive local setup                                           |
| `make doctor`             | Check tools, configuration, and database readiness                |
| `make install`            | Install backend and frontend dependencies                         |
| `make setup-db`           | Initialize a new empty PostgreSQL target                          |
| `make upgrade-db`         | Explicitly upgrade a known older schema after backup              |
| `make check-db`           | Read-only schema/readiness check                                  |
| `make upgrade-system-assets` | Apply packaged System Asset releases during maintenance        |
| `make dev` / `make start` | Start the local full stack                                        |
| `make stop`               | Stop local services                                               |
| `make up` / `make down`   | Build/start or stop the Compose stack                             |
| `make support-bundle`     | Generate a redacted diagnostic bundle and internal incident draft |
| `make test`               | Run the backend core suite with isolated test databases           |

Use `make help` for operator and maintenance commands. Run module commands from
their module directories, for example `cd backend && make lint` or
`cd frontend && pnpm check`.

## Repository-wide rules

- Preserve unrelated user changes in a dirty worktree. Never reset, restore,
  stage, commit, or push them without explicit authorization.
- Update the user-facing `README.md` and the owning module guide when behavior or
  architecture changes. Do not turn the root guide into a feature changelog.
- Features and bug fixes require focused tests. Backend tests live under
  `backend/tests/`; frontend tests live under `frontend/tests/`.
- Authorization comes from authenticated identity, server-issued project
  context, capabilities, and owner scope. Never trust IDs or authority copied
  from request metadata, browser state, or model output.
- Secrets must not enter source, logs, browser storage, query caches, API
  responses, snapshots, or diagnostic bundles. Use governed Credential paths.
- Schema changes require ORM, full-schema SQL, migration-chain, and parity-test
  updates. Never patch or stamp a database manually.
- Format and validate the changed module before handoff. Use
  `cd backend && make format` and `cd frontend && pnpm check` as applicable.
- This private repository has no hosted CI. Run the relevant local backend,
  PostgreSQL, frontend, browser, security, container, and deployment gates for
  the current checkout; historical results are not release evidence.
- Report what was actually verified. A focused or offline test does not certify
  a live database, external model, browser matrix, Sandbox provider, or target
  deployment environment.
