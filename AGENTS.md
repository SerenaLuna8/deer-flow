# AGENTS.md

This file owns repository orientation and cross-cutting gates. Read the owning
guide before changing a module:

- [backend/AGENTS.md](backend/AGENTS.md) — runtime, authorization, persistence,
  configuration, assets, and backend tests.
- [frontend/AGENTS.md](frontend/AGENTS.md) — routes, client scope, data flow,
  UI ownership, and frontend tests.
- [CONTEXT.md](CONTEXT.md) — domain vocabulary. Use its exact terms in code,
  tests, and docs; avoid the terms it marks _Avoid_.

For setup, local operation, and deployment, read [README.md](README.md) and
[Install.md](Install.md).

> The three repository `AGENTS.md` files are development-time guidance only.
> They are not packaged or read by ActWeave at runtime. A project Agent's
> `AGENTS.md`, `SOUL.md`, `IDENTITY.md`, and `USER.md` are database fields,
> not these filesystem guides. Likewise, `skills/public/*/SKILL.md` contains
> runtime Skill assets; local coding-agent skills do not.

## Architecture boundaries

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

The repository does not ship an application container deployment. Docker is an
optional Sandbox runtime only; Gateway, Worker, Scheduler, Frontend, and Nginx
run as host processes.

## Ownership map

| Path                          | Owner                                                     |
| ----------------------------- | --------------------------------------------------------- |
| `backend/app/`                | Gateway, Worker, Scheduler, and application domains       |
| `backend/packages/harness/`   | Agent harness, tools, sandbox, and persistence primitives |
| `backend/packages/knowledge/` | Optional host-agnostic RAG Knowledge module               |
| `frontend/`                   | Next.js application and browser tests                     |
| `nginx/`                      | Local Nginx entry configuration                           |
| `sandbox/`                    | Optional Sandbox Provisioner                              |
| `skills/public/`              | Sole source of packaged System Skill definitions          |
| `scripts/`                    | Setup, diagnostics, local runtime, and Sandbox helpers    |
| `config.example.yaml`         | Root runtime configuration template                       |

Runtime configuration is resolved from the repository-root `config.yaml` (or an
explicit `ACT_WEAVE_CONFIG_PATH`). PostgreSQL is the authority for application
metadata, project/private state, jobs, streams, checkpoints, audit, and governed
asset versions. System model, runtime, authentication, Memory-template, and
quota policies are administered in PostgreSQL, not duplicated in YAML.

## Command boundaries

- Run whole-application commands from the repository root; use `make help` as
  the current command index.
- Run backend targets from `backend/` and frontend targets from `frontend/`, for
  example `make lint` and `pnpm check`.
- `make setup-db`, `make upgrade-db`, and `make upgrade-system-assets` are
  explicit operator actions, never runtime startup steps. The runtime never
  creates, stamps, upgrades, or repairs the application schema; schema upgrades
  are forward-only maintenance-window actions, and `make check-db` is read-only
  readiness evidence. See [backend/AGENTS.md](backend/AGENTS.md) before
  changing persistence.

## Repository-wide rules

### Scope and documentation

- Preserve unrelated user changes in a dirty worktree. Never reset, restore,
  stage, commit, or push them without explicit authorization.
- Update the user-facing `README.md` and the owning module guide when behavior or
  architecture changes. Do not turn any guide into a feature changelog; feature
  behavior is authoritative in code and focused tests.
- Features and bug fixes require focused tests. Backend tests live under
  `backend/tests/`; frontend tests live under `frontend/tests/`.

### Authority, secrets, and persistence

- Authorization comes from authenticated identity, server-issued project
  context, capabilities, and owner scope. Never trust IDs or authority copied
  from request metadata, browser state, or model output.
- Secrets must not enter source, logs, browser storage, query caches, API
  responses, snapshots, or diagnostic bundles. Each consuming domain owns its
  encrypted values and uses the shared secret-envelope infrastructure.
- Schema changes require ORM, the Schema V1 structural SQL template, generated
  comments artifact, catalog digest, and focused schema tests to change
  together. Never patch or stamp a database manually.

### Verification and handoff

- Format and validate the changed module before handoff. Use
  `cd backend && make format` and `cd frontend && pnpm check` as applicable.
- This private repository has no hosted CI. Run the relevant local backend,
  PostgreSQL, frontend, browser, security, container, and deployment gates for
  the current checkout; historical results are not release evidence.
- Report what was actually verified. A focused or offline test does not certify
  a live database, external model, browser matrix, Sandbox provider, or target
  deployment environment.
