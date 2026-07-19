# AGENTS.md

This file provides guidance to AI coding agents (Claude Code, Codex, and others) when working with code in this repository. It is the source of truth; the sibling `CLAUDE.md` imports it via `@AGENTS.md`.

It is the **monorepo orientation layer**: it maps the whole repo and points to the
module guides that own the depth. For anything inside a module, read that module's
guide rather than expecting full detail here:

- **[backend/AGENTS.md](backend/AGENTS.md)** — backend depth: harness/app split, agent &
  middleware chain, sandbox, MCP, skills, memory, IM channels, persistence/migrations,
  config system, test layout.
- **[frontend/AGENTS.md](frontend/AGENTS.md)** — frontend depth: Next.js App Router layout,
  thread/streaming data flow, code style, commands.

## What is DeerFlow

DeerFlow is a LangGraph-based AI super-agent system with a full-stack architecture. The
backend runs a "super agent" with sandboxed execution, persistent memory, subagent
delegation, and extensible tools (built-in, MCP, community), all per-thread isolated. The
frontend is a Next.js chat UI. External IM platforms (Feishu, Slack, Telegram, Discord,
DingTalk) bridge into the same agent through the Gateway.

## Service Topology

A single `make dev` / Docker stack runs four cooperating services:

| Service         | Port   | Role                                                                 |
| --------------- | ------ | ------------------------------------------------------------------- |
| **Nginx**       | `2026` | Unified reverse-proxy entry point — open this in the browser        |
| **Gateway API** | `8001` | FastAPI project/account/admin REST API                              |
| **Frontend**    | `3000` | Next.js web interface                                               |
| **Provisioner** | `8002` | Optional — only when sandbox is configured for provisioner/K8s mode |

Nginx is the single public entry: it serves the frontend and proxies `/api/*`
directly to Gateway REST routers. Project Run admission is Gateway-owned, while
execution remains Worker-only. See
[backend/AGENTS.md](backend/AGENTS.md) for the runtime and router detail.

## Repository Map

```
deer-flow/
├── Makefile                        # Root orchestration: drives the full stack (dev/start/stop, docker, setup)
├── config.example.yaml             # Template → copy to config.yaml (gitignored) at repo root
├── backend/                        # Python backend — see backend/AGENTS.md
│   ├── Makefile                    # Per-module backend commands (dev, gateway, test, lint, migrate-rev)
│   ├── packages/harness/           # deerflow-harness package (import: deerflow.*) — agent framework
│   └── app/                        # FastAPI Gateway + IM channels (import: app.*)
├── frontend/                       # Next.js frontend (pnpm) — see frontend/AGENTS.md
├── docker/                         # docker-compose files, nginx config, provisioner
├── skills/                         # Agent skills: public/ (committed), custom/ (gitignored)
├── contracts/                      # Cross-component JSON contracts (e.g. subagent status)
├── scripts/                        # Root orchestration scripts invoked by the Makefile (check, configure, doctor, support_bundle, serve, nginx, docker, deploy, setup_wizard)
├── tests/                          # Root-level tests (currently tests/skills/ — public skill tests)
└── docs/                           # Cross-cutting docs, plans, and design notes
```

Runtime config lives at the **repo root**: copy `config.example.yaml` to `config.yaml`.
`DATABASE_URL` is the only application persistence connection. PostgreSQL owns application
data, checkpoints, stores, durable jobs, streams, quotas, audit records, and recovery proof.
Config schema and resolution are documented in [backend/AGENTS.md](backend/AGENTS.md).

## Final M7 runtime boundary

- `/workspace` is the authenticated multi-project landing page. Project work lives under
  `/projects/{project_slug}`; platform governance lives under `/admin`.
- Every private request is authorized from server-issued account, project, membership, and owner
  context. Private repositories always bind `project_id + owner_user_id`; project outsiders receive
  public 404 and members without a required capability receive 403.
- Gateway owns authentication, project/admin APIs, transactional Run admission, queries, and
  durable SSE replay. It never executes an Agent graph.
- Worker is the only Agent-graph executor. It claims PostgreSQL jobs with lease authority,
  revalidates project/owner capability at every side-effect boundary, and persists stream frames
  before notification.
- Scheduler only finds due project Automations and atomically admits occurrence, Run, snapshot,
  and job rows. `scheduler.enabled` controls the independent Scheduler process; it never executes
  graph code.
- System Agent, Skill, and MCP definitions are seeded from the packaged, digest-checked catalog
  during explicit database setup. Runtime processes read only PostgreSQL catalog rows and exact
  admitted snapshots. The seed creates no project binding, membership, Credential, or secret.
- System-admin operations expose bounded readiness and governance metadata only. They never return
  prompts, messages, Memory, file/artifact bodies, Run output, credentials, locators, or raw errors.

## Fresh database and recovery boundary

The supported install sequence is: provision a new empty PostgreSQL database, run
`make setup-db`, then run `make start`. Setup installs the sole application revision
`0001_project_saas_baseline`, the packaged system catalog, LangGraph schema, and the default
project. Runtime startup validates the target and never creates or repairs it.

An old revision or unknown nonempty schema fails before DDL with `M7_RECREATE_REQUIRED`.
The operator must preserve that database if needed, point `DATABASE_URL` at a newly created empty
database, and repeat setup. No command upgrades the old database in place.

Recovery accepts only authenticated archive schema version 7 at
`0001_project_saas_baseline`. Restore always targets a distinct, nonexistent new database, replays
the external tombstone journal, verifies the exact schema, and writes proof before a separate manual
traffic switch. See [docs/operations/m6-backup-recovery.md](docs/operations/m6-backup-recovery.md).

M7 is currently a closure candidate awaiting the final independent branch review. M8 full release
acceptance remains pending, so DeerFlow must not yet be described as a complete releasable
multi-user SaaS.

## Commands: Root vs. Module

**Root `make` targets drive the whole stack** (run from the repo root):

```bash
make setup       # Interactive setup wizard (recommended for new users)
make doctor      # Check configuration and system requirements
make support-bundle  # Generate redacted troubleshooting summary, AI issue draft, and optional zip
make config      # Generate local config files from the examples
make check       # Check that required tools are installed
make install     # Install all dependencies (frontend + backend + pre-commit hooks)
make setup-db    # 显式创建并完整初始化 PostgreSQL 目标库
make backup-db ARGS="--output /secure/backups"  # 外部认证加密 PostgreSQL archive
make restore-db ARGS="--archive /secure/backups/<archive> --target-url <new-deerflow_restore-url> --journal /secure/recovery/tombstones.jsonl --execute"  # 恢复、重放、probe 并写 proof；不切换 DATABASE_URL
make drill-restore ARGS="--archive /secure/backups/<archive> --journal /secure/recovery/tombstones.jsonl"  # 随机恢复库演练，仅清理该库
make rotate-credentials ARGS="--dry-run --key-id m3-next"  # 分批轮换 credential envelope
make check-db    # 只读检查连接、Alembic head 与必需表
make dev         # Start all services with hot-reload (Gateway + Frontend + Nginx)
make start       # Start all services in production mode (local, optimized)
make stop        # Stop all running services
make up / down   # Build/stop the production Docker stack (browser at localhost:2026)
make docker-start / docker-stop / docker-logs   # Docker development environment
```

Run `make help` for the full list.

**Per-module commands drive a single module** (run inside that module):

```bash
# Backend (see backend/AGENTS.md for the full set)
cd backend && make dev        # Gateway API with reload (port 8001)
cd backend && make test       # Backend test suite
cd backend && make lint       # ruff check
cd backend && make format     # ruff format

# Frontend (see frontend/AGENTS.md for the full set)
cd frontend && pnpm dev       # Dev server with Turbopack (port 3000)
cd frontend && pnpm check     # Lint + type check (run before committing)
cd frontend && pnpm test      # Unit tests
```

Rule of thumb: **root `make` = the full application**; **`backend/Makefile` and `frontend/`
(`pnpm`) = per-module work.**

## Where to Go Next

- Backend work → **[backend/AGENTS.md](backend/AGENTS.md)**
- Frontend work → **[frontend/AGENTS.md](frontend/AGENTS.md)**
- Setup & install → **[Install.md](Install.md)**, **[CONTRIBUTING.md](CONTRIBUTING.md)**
- Project overview & usage → **[README.md](README.md)**
- Security policy → **[SECURITY.md](SECURITY.md)**
- Changes → **[CHANGELOG.md](CHANGELOG.md)**
- Cutting a release → **[RELEASING.md](RELEASING.md)**

## Cross-Cutting Conventions

These apply repo-wide; module guides own the module-specific detail.

- **Documentation update policy** — keep docs in sync with code: update `README.md` for
  user-facing changes and the relevant `AGENTS.md` for development/architecture changes in
  the same change set.
- **Test-driven development** — features and bug fixes ship with tests. Backend tests live
  in `backend/tests/` (TDD is mandatory there; see [backend/AGENTS.md](backend/AGENTS.md));
  frontend tests live in `frontend/tests/`.
- **Format before pushing** — run `make format` (backend) / `pnpm check` (frontend). Backend
  CI enforces `ruff format --check`, so formatting must be clean before a push.
- **PostgreSQL release gate** — root `Makefile` 的 `PROJECT_FOUNDATION_POSTGRES_TESTS`
  是固定 22 文件 M1–M7 真实 PostgreSQL gate 的唯一有序来源；覆盖 M7 baseline/bootstrap、M2–M5
  runtime integration、M6 process/job/stream/quota/audit/recovery，以及真实 Gateway/Scheduler/Worker
  lease、Worker-only graph、Gateway restart cursor 和跨 account/project/owner 隔离。生产 source-absence
  gate 只扫描 app/harness/scripts/frontend runtime/nginx roots，历史 docs/tests 不参与；已移除 config
  key 只允许出现在精确 validator allowlist。每个数据库测试只创建
  随机 `deerflow_test_*`/`deerflow_restore_*` 数据库。Release evidence 必须通过
  `POSTGRES_TEST_URL=... make test-project-foundation-postgres` 运行并保持 0 skip；跨平台 Python runner 和
  `.github/workflows/project-foundation-postgres-tests.yml` 都会在变量缺失时于 pytest 前硬失败。
- **M7 fresh-install baseline** — `migrations/versions/` 只允许
  `0001_project_saas_baseline.py`；`down_revision=None`，downgrade 永远拒绝。`make setup-db` 在空库安装
  final application schema、builtin catalog、LangGraph schema 与 default project；`make check-db` 只读验证
  exact revision 和必需表。真实测试只准使用随机 `deerflow_test_*`，绝不连接业务库。
- **平台资产管理** — `/admin/assets` 由 server layout 强制限制为 `system_admin`，普通用户返回
  404。Credential create/replace 使用 imperative authenticated API，不得把 secret-bearing input
  放入 TanStack Query/Mutation cache；MCP Credential slot 只能走 submit/approve。轮换状态 GET
  使用 rotation CLI 相同 eligibility，只返回 eligible/current/pending 聚合与状态，不返回 key ID、
  nonce、ciphertext 或 storage locator。
