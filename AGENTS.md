# AGENTS.md

This file provides guidance to AI coding agents (Claude Code, Codex, and others) when working with code in this repository. It is the source of truth.

It is the **monorepo orientation layer**: it maps the whole repo and points to the
module guides that own the depth. For anything inside a module, read that module's
guide rather than expecting full detail here:

- **[backend/AGENTS.md](backend/AGENTS.md)** — backend depth: harness/app split, agent &
  middleware chain, sandbox, MCP, skills, memory, IM channels, persistence/schema initialization,
  config system, test layout.
- **[frontend/AGENTS.md](frontend/AGENTS.md)** — frontend depth: Next.js App Router layout,
  thread/streaming data flow, code style, commands.

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
`DATABASE_URL` is the only application persistence connection. PostgreSQL owns application
data, checkpoints, stores, durable jobs, streams, quotas, and audit records.
The current process-config schema is `config_version: 35`. Version 35 replaces per-URL Project
MCP endpoint approval with startup-only CIDR networks; Gateway, Scheduler, and every Worker must
restart together after `mcp_security` changes. Model definitions, live Agent runtime policy,
self-registration policy, project-quota defaults, and provider Credentials are
PostgreSQL system settings, not `config.yaml` or ambient runtime provider environment variables.
System admins manage runtime/auth/quota policy at `/admin/settings/system`; new Runs freeze the
exact Agent policy while Gateway request-only features read the current committed policy. On an
empty local target, `make setup-db` reads `DEEPSEEK_API_KEY` and
the Credential keyring once from the root `.env` when that file exists (explicit environment
wins, and may be used without an `.env` file), stores the removed
example's DeepSeek V4 Pro as an encrypted active/default catalog entry, and runtime roles continue
to read only PostgreSQL. A system admin manages the resulting catalog at
`/admin/settings/models`.
Config schema and resolution are documented in [backend/AGENTS.md](backend/AGENTS.md).
Backend module role commands (`make dev`, `make gateway`, `make worker`, and
`make scheduler` from `backend/`) explicitly import non-provider settings from
the root `.env`, preserve an explicit process environment, and remove ambient
model-provider API keys before starting the role.

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
  admitted snapshots. `skills/public/` is the sole source of System Skill entries; the catalog
  generator replaces the complete Skill set while retaining packaged Agent and MCP entries. The
  catalog seed itself creates no project binding, membership, Credential, or secret. Each newly
  created project separately pins every current System Skill as an enabled project binding; an
  existing project's bindings and administrator-disabled choices are never reconciled by setup.
- System-admin operations expose bounded readiness and governance metadata only. They never return
  prompts, messages, Memory, file/artifact bodies, Run output, credentials, locators, or raw errors.

## PostgreSQL schema lifecycle

`make setup-db` is the only schema initialization entry point. It requires an empty PostgreSQL
target, executes the complete packaged `full_schema.sql`, records the exact
`full_schema_v3` marker, seeds the packaged system catalog, initializes the LangGraph schema,
bootstraps the default project, and atomically seeds the encrypted DeepSeek V4 Pro
Credential/model/default pointer. Missing `DEEPSEEK_API_KEY` or an invalid Credential keyring
fails preflight before the target database is created.

There is no incremental migration chain and no supported upgrade path for an older DeerFlow
database. A legacy marker, unknown marker, unversioned nonempty schema, or catalog drift is
rejected without mutation. Provision a new empty database and run `make setup-db`; never stamp,
patch, or reuse the old schema. Runtime startup and `make check-db` are read-only consumers and
never create, upgrade, repair, or delete database objects.

Release readiness is checkout-sensitive. Historical milestone evidence does not certify the
current worktree; run the current focused gates, including the real PostgreSQL core tests when
persistence or runtime boundaries change.
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
make rotate-credentials ARGS="--dry-run --key-id m3-next"  # 分批轮换 credential envelope
make check-db    # 只读检查连接、schema marker 与必需表
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
- **PostgreSQL core tests** — `POSTGRES_TEST_URL=... make test` 运行精简后的后端核心集合，
  其中保留真实 PostgreSQL 初始化/只读校验、Project Repository、系统运行配置、Human Input
  与跨 owner 隔离用例。每个数据库测试只创建随机 `deerflow_test_*` 数据库，绝不连接业务库；
  缺少测试 URL 时入口在 pytest 前失败，完整核心运行必须保持 0 skip。
- **Consolidated core CI** — `.github/workflows/project-saas-release-gates.yml` 统一运行后端核心
  测试、真实 PostgreSQL 核心用例、前端核心单元测试、少量确定性 Chromium E2E、格式和安全检查。
  不要为这些命令再新增重复 workflow；Replay E2E、发布、容器、Helm Chart 和版本检查仍使用专用
  workflow。该核心集合用于快速保护主路径，不代表外部模型、浏览器矩阵或部署环境的完整认证。
- **Single full-schema initialization** — `full_schema.sql` 是唯一完整 PostgreSQL schema
  来源，当前精确 marker 为 `full_schema_v3`。`make setup-db` 只接受空库，并在同一次显式初始化中
  安装完整 schema、builtin catalog、LangGraph schema 与 default project。仓库不提供增量升级；
  旧 marker、未知 marker、未纳管非空 schema 或 catalog drift 必须换空库重建。运行时和
  `make check-db` 只读校验。真实测试只准使用随机 `deerflow_test_*`，绝不连接业务库。
- **平台资产管理** — `/admin/assets` 由 server layout 强制限制为 `system_admin`，普通用户返回
  404。Credential create/replace 使用 imperative authenticated API，不得把 secret-bearing input
  放入 TanStack Query/Mutation cache；MCP Credential slot 只能走 submit/approve。轮换状态 GET
  使用 rotation CLI 相同 eligibility，只返回 eligible/current/pending 聚合与状态，不返回 key ID、
  nonce、ciphertext 或 storage locator。Skill 详情只绑定现有项目 Credential 版本；API 和 Run
  snapshot 只保存引用，Worker 在精确准入闭包校验后才解密，并仅向当前激活 Skill 的 sandbox
  subprocess 注入对应环境变量。
