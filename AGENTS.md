# AGENTS.md

This file provides guidance to AI coding agents (Claude Code, Codex, and others) when working with code in this repository. It is the source of truth.

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

A single `make dev` / Docker stack runs the following cooperating processes and services:

| Service         | Port   | Role                                                                 |
| --------------- | ------ | ------------------------------------------------------------------- |
| **Nginx**       | `2026` | Unified reverse-proxy entry point — open this in the browser        |
| **Gateway API** | `8001` | FastAPI project/account/admin REST API                              |
| **Frontend**    | `3000` | Next.js web interface                                               |
| **Worker**      | —      | Required Agent-graph executor; no public port                       |
| **Scheduler**   | —      | Optional Automation admission process; no public port               |
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
│   └── app/                        # Gateway, Worker, Scheduler + business domains (import: app.*)
├── frontend/                       # Next.js frontend (pnpm) — see frontend/AGENTS.md
├── docker/                         # docker-compose files, nginx config, provisioner
├── deploy/helm/                    # Kubernetes/Helm resources
├── skills/public/                  # Reviewable and importable Skill sources
├── contracts/                      # Cross-component JSON contracts (e.g. subagent status)
├── scripts/                        # Root orchestration scripts invoked by the Makefile (check, configure, doctor, support_bundle, serve, nginx, docker, deploy, setup_wizard)
└── docs/                           # Cross-cutting docs, plans, and design notes
```

Runtime config lives at the **repo root**: copy `config.example.yaml` to `config.yaml`.
`DATABASE_URL` is the only application persistence connection. PostgreSQL owns application
data, checkpoints, stores, durable jobs, streams, quotas, and audit records.
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

## PostgreSQL schema lifecycle

For a new installation, provision an empty PostgreSQL database, run `make setup-db`, then run
`make start`. The immutable `0001_project_saas_baseline` remains the frozen ancestor; the single
current head is `0002_skill_design_builder`, which adds private Skill Builder sessions, pinned
system `skill-creator` versions, idempotent operations, and durable candidate files. Setup applies
the complete linear chain, seeds the packaged system catalog, initializes the LangGraph schema,
and bootstraps the default project.

An existing database for the current release must either match the exact current `0002` catalog
or the exact frozen `0001` catalog. For the latter, stop application processes, run
`make migrate-db`, run `make check-db`, and then restart. Migration never creates a database,
seeds the catalog, initializes LangGraph, or bootstraps a project. Runtime startup and `check-db`
are read-only schema consumers and never create, migrate, stamp, or repair database objects.

An unknown revision, an unversioned nonempty schema, or catalog drift is rejected without
automatic repair. Downgrade, manual stamp, automatic deletion, and destructive reset are
unsupported.

Release readiness is checkout-sensitive. Historical milestone evidence does not certify the
current worktree; run the current focused gates and `make release-acceptance` for a release candidate.
Docker Compose, Kubernetes/Helm, browsers, model providers, and Sandbox modes require separate
target-environment validation.

## Commands: Root vs. Module

**Root `make` targets drive the whole stack** (run from the repo root):

```bash
make setup       # Interactive setup wizard (recommended for new users)
make doctor      # Check configuration and system requirements
make support-bundle  # Generate redacted troubleshooting summary, AI issue draft, and optional zip
make config      # Generate local config files from the examples
make check       # Check that required tools are installed
make install     # Install backend and frontend dependencies
make setup-db    # 显式创建并完整初始化 PostgreSQL 目标库
make migrate-db  # 停服后从精确冻结祖先显式执行已提交的 pending migrations
make rotate-credentials ARGS="--dry-run --key-id m3-next"  # 分批轮换 credential envelope
make check-db    # 只读检查连接、Alembic head 与必需表
make release-acceptance  # M8 宿主机完整 candidate/review/final 验收（要求显式 live 环境）
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
- Setup & install → **[Install.md](Install.md)**
- Project overview & usage → **[README.md](README.md)**

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
  是固定 20 文件 M1–M7 真实 PostgreSQL gate 的唯一有序来源；覆盖 M7 baseline/bootstrap、M2–M5
  runtime integration、M6 process/job/stream/quota/audit/retention，以及真实 Gateway/Scheduler/Worker
  lease、Worker-only graph、Gateway restart cursor 和跨 account/project/owner 隔离。生产 source-absence
  gate 只扫描 app/harness/scripts/frontend runtime/nginx roots，历史 docs/tests 不参与；已移除 config
  key 只允许出现在精确 validator allowlist。每个数据库测试只创建
  随机 `deerflow_test_*` 数据库。Release evidence 必须通过
  `POSTGRES_TEST_URL=... make test-project-foundation-postgres` 运行并保持 0 skip；跨平台 Python runner 和
  `.github/workflows/project-saas-release-gates.yml` 会在变量缺失时于 pytest 前硬失败。
- **M8 final PostgreSQL gate** — `M8_RELEASE_POSTGRES_TESTS` 只允许在上述 20 文件前缀后追加
  M8 isolation、capacity 和 release-contract 三个文件；`make test` 与
  `make test-project-saas-postgres` 使用该 23 文件 0-skip 清单。完整 live 验收使用
  `make release-acceptance`。
- **Consolidated deterministic CI** — `.github/workflows/project-saas-release-gates.yml` 是后端完整
  pytest（已递归收集 `tests/blocking_io/`）、固定 23 文件 PostgreSQL 门禁、前端单元测试、确定性
  Chromium E2E、构建与安全检查的唯一 CI 编排。不要为这些命令再新增独立重复 workflow；Replay E2E、
  发布、容器、Helm Chart 和版本检查仍保持专用 workflow。
- **Forward-only schema migrations** — `0001_project_saas_baseline.py` 是禁止改写的冻结基线；
  当前单一 head `0002_skill_design_builder.py` 线性增加 Skill Builder 会话、操作和候选文件表。
  未来 schema 变化从 `0003` 起继续线性追加 revision，绝不重写、压缩或 stamp 历史。
  `make setup-db` 在空库应用当前 head 并初始化 builtin catalog、LangGraph schema 与 default
  project；`make migrate-db` 保留给未来已提交 revision，且不执行这些 bootstrap side effects；
  运行时和 `make check-db` 只读校验。未知 revision 或 catalog drift 拒绝自动处理。真实测试只准
  使用随机 `deerflow_test_*`，绝不连接业务库。
- **平台资产管理** — `/admin/assets` 由 server layout 强制限制为 `system_admin`，普通用户返回
  404。Credential create/replace 使用 imperative authenticated API，不得把 secret-bearing input
  放入 TanStack Query/Mutation cache；MCP Credential slot 只能走 submit/approve。轮换状态 GET
  使用 rotation CLI 相同 eligibility，只返回 eligible/current/pending 聚合与状态，不返回 key ID、
  nonce、ciphertext 或 storage locator。
