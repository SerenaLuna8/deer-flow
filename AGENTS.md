# AGENTS.md

This file provides guidance to AI coding agents (Claude Code, Codex, and others) when working with code in this repository. It is the source of truth.

It is the **monorepo orientation layer**: it maps the whole repo and points to the
module guides that own the depth. Keep it navigational — runtime, authorization,
persistence, and UI invariants belong in the module guide that owns them, not here.
For anything inside a module, read that module's guide rather than expecting full detail here:

- **[backend/AGENTS.md](backend/AGENTS.md)** — backend depth: harness/app split, agent &
  middleware chain, sandbox, MCP, skills, memory, IM channels, persistence/schema initialization,
  runtime and authorization boundaries, config system, test layout.
- **[frontend/AGENTS.md](frontend/AGENTS.md)** — frontend depth: Next.js App Router layout,
  final route model, thread/streaming data flow, cache isolation, code style, commands.

> **Naming boundary** — these three repository `AGENTS.md` files are development-time guidance
> for coding agents. They are never packaged, never shipped, and never read by ActWeave at
> runtime. They are unrelated to the product feature of the same name: a project Agent's
> `AGENTS.md`, `SOUL.md`, `IDENTITY.md`, and `USER.md` are logical documents backed by columns
> on the immutable Agent version row. The same distinction separates `skills/public/*/SKILL.md`
> (packaged runtime assets) from any local coding-agent skill directory.

## What is ActWeave

ActWeave is a LangGraph-based AI super-agent system with a full-stack architecture. The
backend runs a "super agent" with sandboxed execution, persistent memory, subagent
delegation, and extensible tools (built-in, MCP, community), all per-thread isolated. The
frontend is a Next.js chat UI. External IM platforms (Feishu, Slack, Telegram, Discord,
DingTalk) bridge into the same agent through the Gateway.

## Service Topology

A single `make dev` / Docker stack runs the following cooperating processes and services:

| Service         | Port   | Role                                                                 |
| --------------- | ------ | ------------------------------------------------------------------- |
| **Nginx**       | `2026` | Unified reverse-proxy entry point — open this in the browser        |
| **Gateway API** | `8001` | FastAPI project/account/admin REST API                              |
| **Frontend**    | `3000` | Next.js web interface                                               |
| **Worker**      | —      | Required Agent-graph executor; no public port                       |
| **Scheduler**   | —      | Optional Automation admission process; no public port               |
| **Provisioner** | `8002` | Optional — only when sandbox is configured for provisioner/K8s mode |

Nginx is the single public entry: it serves the frontend and proxies `/api/*` directly to
Gateway REST routers. Gateway owns authentication, project/admin APIs, Run admission, and
durable SSE replay but never executes an Agent graph; Worker is the only graph executor;
Scheduler only admits due Automations. See [backend/AGENTS.md](backend/AGENTS.md) for the
runtime, authorization, and router detail.

## Repository Map

```
deer-flow/
├── Makefile                        # Root orchestration: drives the full stack (dev/start/stop, docker, setup)
├── config.example.yaml             # Template → copy to config.yaml (gitignored) at repo root
├── backend/                        # Python backend — see backend/AGENTS.md
│   ├── Makefile                    # Per-module backend commands (dev, gateway, test, lint)
│   ├── packages/harness/           # deerflow-harness package (import: deerflow.*) — agent framework
│   └── app/                        # Gateway, Worker, Scheduler + business domains (import: app.*)
├── frontend/                       # Next.js frontend (pnpm) — see frontend/AGENTS.md
├── docker/                         # docker-compose files, nginx config, provisioner
├── deploy/helm/                    # Kubernetes/Helm resources
├── skills/public/                  # Sole source of packaged System Skill definitions
├── scripts/                        # Root orchestration scripts invoked by the Makefile (check, configure, doctor, support_bundle, serve, nginx, docker, deploy, setup_wizard)
└── docs/                           # Cross-cutting docs, plans, and design notes
```

Runtime config lives at the **repo root**: copy `config.example.yaml` to `config.yaml`.
`DATABASE_URL` is the only application persistence connection — PostgreSQL owns application
data, checkpoints, stores, durable jobs, streams, quotas, and audit records. Model definitions,
Agent runtime policy, auth policy, project-quota defaults, and provider Credentials are
PostgreSQL system settings managed by a system admin under `/admin/settings/*`; they are not
`config.yaml` keys and not ambient process environment variables.

[backend/AGENTS.md](backend/AGENTS.md) owns the config schema and resolution order, `.env`
handling, and the PostgreSQL schema lifecycle — including `make setup-db` for fresh installs,
the exact schema marker, and the explicit `make upgrade-db` migration chain for existing
databases.

## Commands: Root vs. Module

**Root `make` targets drive the whole stack** (run from the repo root):

```bash
make setup       # Interactive setup wizard (recommended for new users)
make doctor      # Check configuration and system requirements
make support-bundle  # Generate redacted troubleshooting summary, AI issue draft, and optional zip
make config      # Generate local config files from the examples
make check       # Check that required tools are installed
make install     # Install backend and frontend dependencies
make setup-db    # Explicitly create and fully initialize the PostgreSQL target database
make upgrade-db  # Explicitly migrate an existing database to the current chain head (backup first)
make rotate-credentials ARGS="--dry-run --key-id m3-next"  # Rotate credential envelopes in batches
make check-db    # Read-only check of connection, schema marker, and required tables
make dev         # Start all services with hot-reload (Gateway + Frontend + Nginx)
make start       # Start all services in production mode (local, optimized)
make stop        # Stop all running services
make up / down   # Build/start or stop the Compose stack (browser at localhost:2026)
make docker-start / docker-stop / docker-logs   # Docker development environment
```

Run `make help` for the full list.

**Per-module commands drive a single module** (run inside that module):

```bash
# Backend (see backend/AGENTS.md for the full set)
cd backend && make dev        # Gateway API with reload (port 8001)
POSTGRES_TEST_URL=... make test  # Backend core suite from the repository root
cd backend && make lint       # ruff check
cd backend && make format     # ruff format

# Frontend (see frontend/AGENTS.md for the full set)
cd frontend && pnpm dev       # Dev server with Turbopack (port 3000)
cd frontend && pnpm check     # Lint + type check (run before committing)
cd frontend && pnpm test      # Unit tests
```

Rule of thumb: **root `make` = the full application**; **`backend/Makefile` and `frontend/`
(`pnpm`) = per-module work.**

## Cross-Cutting Conventions

These apply repo-wide and have no module-level equivalent. Everything module-specific —
including the backend test contract, schema lifecycle, and asset/Credential boundaries —
lives in the module guide that owns it.

- **Documentation update policy** — keep docs in sync with code: update `README.md` for
  user-facing changes and the relevant `AGENTS.md` for development/architecture changes in
  the same change set. Put an invariant in the module guide that owns it; this file keeps
  only the pointer.
- **Test-driven development** — features and bug fixes ship with tests. Backend tests live
  in `backend/tests/` (TDD is mandatory there; see [backend/AGENTS.md](backend/AGENTS.md));
  frontend tests live in `frontend/tests/`.
- **Format before pushing** — run `make format` (backend) / `pnpm check` (frontend). Backend
  CI enforces `ruff format --check`, so formatting must be clean before a push.
- **Consolidated core CI** — `.github/workflows/project-saas-release-gates.yml` runs the
  backend core suite, the real-PostgreSQL core cases, frontend core unit tests, a small
  deterministic Chromium E2E set, and the format and security checks in one workflow. Do not
  add another workflow for these commands; Replay E2E, release, container, Helm chart, and
  version checks keep their dedicated workflows. This core set protects the main paths
  quickly — it does not certify external models, a browser matrix, or a deployment target.
- **Release readiness is checkout-sensitive** — historical milestone evidence does not certify
  the current worktree. Run the current focused gates, including the real PostgreSQL core
  tests when persistence or runtime boundaries change. Docker Compose, Kubernetes/Helm,
  browsers, model providers, and Sandbox modes each need separate target-environment
  validation that CI does not provide.

## Where to Go Next

- Backend work → **[backend/AGENTS.md](backend/AGENTS.md)**
- Frontend work → **[frontend/AGENTS.md](frontend/AGENTS.md)**
- Setup & install → **[Install.md](Install.md)**
- Project overview & usage → **[README.md](README.md)**
