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
| **Gateway API** | `8001` | FastAPI REST API + embedded LangGraph-compatible agent runtime      |
| **Frontend**    | `3000` | Next.js web interface                                               |
| **Provisioner** | `8002` | Optional — only when sandbox is configured for provisioner/K8s mode |

Nginx is the single public entry: it serves the frontend and proxies `/api/langgraph/*`
to the Gateway's LangGraph runtime, rewriting it to Gateway's native `/api/*` routes; all
other `/api/*` go straight to the Gateway REST routers. See
[backend/AGENTS.md](backend/AGENTS.md) for the runtime and router detail.

## Repository Map

```
deer-flow/
├── Makefile                        # Root orchestration: drives the full stack (dev/start/stop, docker, setup)
├── config.example.yaml             # Template → copy to config.yaml (gitignored) at repo root
├── extensions_config.example.json  # Template → copy to extensions_config.json (gitignored): MCP servers + skills
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

Runtime config lives at the **repo root**: copy `config.example.yaml` → `config.yaml`
(main app config) and `extensions_config.example.json` → `extensions_config.json` (MCP
servers + skills). Both real files are gitignored and may be edited at runtime via the
Gateway API. Config schema and resolution order are documented in
[backend/AGENTS.md](backend/AGENTS.md).

Persistence configuration is PostgreSQL-only: `database.url` resolves from
`DATABASE_URL`, PostgreSQL drivers are default dependencies, and a standalone
`checkpointer` section is rejected. ORM sessions, schema bootstrap, LangGraph
checkpointers, and LangGraph stores are all PostgreSQL implementations; runtime
startup validates but never creates the target database.
M1/M2/M3 不使用 PostgreSQL RLS。应用授权依赖认证身份、不可变 `ProjectContext` 和强制作用域
repository；应用连接使用普通非 superuser role，只有显式 setup/migration 脚本属于 trusted
operations。登录后 `/workspace` 是没有项目级侧栏的多项目卡片工作空间；进入
`/projects/{project_slug}` 后才显示项目菜单。M2 邀请只生成一次性 fragment 链接，不发送
邮件；成员退出/移除和项目删除只记录 30 天窗口，不物理清除私有或项目数据。M4 已挂载
project private-work、Memory 和 connection backend API，覆盖 Thread、run/stream/feed、file/artifact、
项目 Memory 管理和 IM connection/OAuth/inbound 文本执行链，并保持项目与 owner 双重隔离。项目
run/feed 的消息与事件固定写入 PostgreSQL，不受 legacy `run_events.backend=memory` 配置影响。M4
提供 runnable-first 的显式 private-work migration：可 dry-run，并把 PostgreSQL legacy
Thread/run/event/feedback 与 checkpoint metadata marker 迁入显式 owner→active project scope；非空 legacy
filesystem、Memory 或 connection source 当前会安全拒绝，留待后续迁移。Frontend 已接入
account/project-scoped frontend client、项目 Chats、Memory、Connections 以及 chat 内
file/artifact/sidecar；所有项目私有数据 URL 均从当前 `ProjectPrivateWorkProvider` 派生，Viewer 只获得
服务端 capability 允许的只读与 own-delete 操作。`PROJECT_PRIVATE_WORKSPACE` 已编译期开启，但 Chats、
Memory、Connections 和 recent-work 入口仍必须同时通过服务端 readiness 与 capability gate；静态构建不
暴露入口。M4 已接入 singleton
`private_work_cutover_state` guard：final schema 且 marker 完成后开放 project private API，同时关闭
legacy Thread/run/Memory/channel connection/upload/artifact HTTP 与 shared `start_run`。M4 已于
2026-07-16 完成实现、迁移正向链、单次独立审查修复与全量门禁。M5 project Automation 也已于
2026-07-16 完成：definition、occurrence、API/UI 和迁移均以认证 account、
project 与 owner 为 authority，Viewer 只读自己的定义与历史；自动和手动触发都先持久化唯一
occurrence 再进入 M4 private run admission，已 admitted 的 run 在 crash recovery 中只协调终态、
绝不自动重放。M6 当前已实现通用 durable job、独立 Worker、Worker-only private Run、Automation
原子 admission、独立 Scheduler、PostgreSQL durable stream writer/reader 与 terminal invariant，以及 Gateway
SSE reconnect 和按 account/project/thread 隔离的前端 cursor/dedupe；配额、完整审计和通用备份恢复仍待后续 M6 task。
里程碑进度仍为 5/8（62.5%），因为 M6 尚未整体关闭；M6–M8
尚未交付，因此当前仍不能
作为完整多用户 SaaS 发布。

Scheduled-task note:
- The scheduled-task MVP adds a workspace page at `/workspace/scheduled-tasks`; under final M6 cutover, `config.yaml -> scheduler.enabled` gates an independent Scheduler process rather than a Gateway lifespan task.
- Scheduled background runs are intentionally non-interactive: they execute through the normal Worker run lifecycle, but the lead-agent toolset excludes `ask_clarification` when `context.non_interactive=true`. `AutomationDispatcher` writes that flag as server-owned admission data in the atomic occurrence/Run/job transaction; client-supplied `context.non_interactive` is dropped.
- Project Automation occurrence, private Run/snapshot, and `automation_run` job now commit atomically. Gateway retains manual admission but never constructs Scheduler ownership or a poller. When enabled, `make scheduler` owns the process-lifetime PostgreSQL session advisory lock on a dedicated connection; each poll verifies the same backend PID and existing lock without reacquiring it, and ownership loss exits polling/process lifetime. A competing Scheduler may take over only after PostgreSQL releases the old session lock. Worker startup always reconciles already-admitted terminal Runs before claiming jobs, and enabled Scheduler startup performs the same idempotent reconciliation before polling; neither path interrupts or replays active Worker work. Disabled Scheduler mode takes no lock or poll task while manual APIs and Worker restart reconciliation remain available.

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
make setup-m4-migration-db  # 创建/验证固定在0007的legacy SQLite private-work迁移库
make migrate-db  # 升级已存在 PostgreSQL 目标库
make migrate-sqlite ARGS="..."  # 只读预检、备份并迁移 legacy SQLite；private rows需--m4-staging-target
make migrate-assets ARGS="--dry-run ..."  # 脱敏 inventory；execute 前必须先 dry-run
make migrate-private-work ARGS="--dry-run ..."  # 显式 owner map 的 M4 private-work staged migration
make migrate-automations ARGS="--dry-run ..."  # 显式 owner/Agent map 的 M5 Automation staged migration
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
- Project overview & usage → **[README.md](README.md)** (translations: `README_zh.md`,
  `README_ja.md`, `README_fr.md`, `README_ru.md`)
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
- **PostgreSQL release gate** — `.github/workflows/project-foundation-postgres-tests.yml`
  固定运行 M1 cutover、project isolation、M2 governance、M3 shared-assets、M4 private-work、
  M4 private-work migration、M5 project Automation 和 M5 Automation migration 八个真实 PostgreSQL
  集成文件；每个测试使用临时 `deerflow_test_*` 数据库。缺少 `POSTGRES_TEST_URL` 只能在本地日常
  测试中明确 skip，M1–M5 release evidence 必须提供该变量并保持 0 skip；CI 必须在进入 pytest 前硬失败。
- **M4 private-work cutover 运维** — `migrate-private-work` 使用显式 owner UUID→active project UUID
  map；先 dry-run，再在停止 Gateway/Scheduler/channel/embedded writers 的维护窗口 execute，随后运行
  `make check-db` 与 M1–M4 probes。M4 marker 完成后，若 legacy Automation 任一表非空，命令固定停在
  `0011` 并保留原行，由后续 `migrate-automations` 独占 `0012/0013`；仅 Automation 空域可继续完成
  head bootstrap。当前 runnable-first CLI 不写 `--backup-dir`、不消费
  `DEER_FLOW_M4_BACKUP_KEY`，因此 operator 必须在仓库外保留数据库备份证明；完整故障决策见
  `docs/operations/m4-private-work-migration.md`。
- **M3 asset cutover 运维** — `migrate-assets` 扫描 repo 默认/system Agent、`skills/public`、
  canonical extensions config 和 `.deer-flow` 用户目录；project 来源必须通过 owner map 显式给出
  active default project，system 来源必须给出 system-admin actor。执行前先 dry-run；execute 先做
  全量 scope/dependency 预检和认证加密 backup/脱敏 ledger，四类 probe 全通过才写 cutover marker。
  `rotate-credentials` 要求目标 key 已是 active key，使用 gap-safe `SKIP LOCKED` 分批重扫；cursor 只作
  审计 checkpoint，tamper 回滚当前批。真实测试只准使用随机 `deerflow_test_*`，绝不连接业务库。
- **M3 平台资产管理** — `/admin/assets` 由 server layout 强制限制为 `system_admin`，普通用户返回
  404。Credential create/replace 使用 imperative authenticated API，不得把 secret-bearing input
  放入 TanStack Query/Mutation cache；MCP Credential slot 只能走 submit/approve。轮换状态 GET
  使用 rotation CLI 相同 eligibility，只返回 eligible/current/pending 聚合与状态，不返回 key ID、
  nonce、ciphertext 或 storage locator。
