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

Runtime config lives at the **repo root**: copy `config.example.yaml` → `config.yaml`.
M7 makes PostgreSQL the only Agent/Skill/MCP authority and removes the legacy
extensions/MCP JSON configuration surface. Config schema and resolution order are documented in
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
暴露入口。Project private API 现在只依赖 final-schema readiness 与 capability；legacy
Thread/run/Memory/channel connection/upload/artifact HTTP 与 shared `start_run` 已删除。M4 已于
2026-07-16 完成实现、迁移正向链、单次独立审查修复与全量门禁。M5 project Automation 也已于
2026-07-16 完成：definition、occurrence、API/UI 和迁移均以认证 account、
project 与 owner 为 authority，Viewer 只读自己的定义与历史；自动和手动触发都先持久化唯一
occurrence 再进入 M4 private run admission，已 admitted 的 run 在 crash recovery 中只协调终态、
绝不自动重放。M6 已完成通用 durable job、独立 Worker、Worker-only private Run、Automation
原子 admission、独立 Scheduler、PostgreSQL durable stream writer/reader 与 terminal invariant、Gateway
SSE reconnect 和按 account/project/thread 隔离的前端 cursor/dedupe，以及项目配额的原子
counter/append-only ledger、平台默认值收紧、80% 单次阈值和 dry-run/execute reconciliation core。成员加入/
退出、文件 finalize/delete/branch/finalization/Thread delete、private/Automation Run admission 与所有终态、实际
MCP dispatch 均已在原业务事务边界执行 quota；429 使用稳定错误与 `Retry-After`，已运行任务不因后续收紧而中断。
M6 审计、外部认证加密 backup、带 PostgreSQL 权威 head/source anchor 的 journal-first retention purge、
持有同一 purge authority 到 proof 完成的 new-DB restore、连续 tombstone replay、M1–M6 probes、
绑定 journal ID/最终 sequence/head digest 的 restore proof 和 disposable drill 已实现；敏感临时文件在
proof 前按 inode 身份清理并 fsync，未知文件拒绝认领；body 失败时仍在 purge authority 内清理，可靠
unlock 后的取消也会重抛并删除本次 target。drill 仅在不可伪造的成功 ownership handoff 后删除其随机
target。显式 M6 staged migration、认证 backup attestation、逐资源 exact quota/job
backfill、aggregate-only reconciliation 拒绝、process readiness 与 Gateway+Worker+可选 Scheduler
本地/Docker 编排均已交付。Task 19 固定 20 文件 M1–M6 PostgreSQL gate，并以真实 Scheduler/Worker/Gateway
多进程、SSE reconnect、Frontend static/cache 和新库 restore 覆盖发布边界；Task 20 的全量门禁和独立关闭
审查于 2026-07-18 完成。M7 Task 8 已把数据库历史重置为唯一 fresh-install baseline
`0001_project_saas_baseline`。运行期只接受空数据库或精确 M7 schema；旧 revision、未知非空 schema
和 legacy migration/cutover control tables 均在任何 DDL 前以 `M7_RECREATE_REQUIRED` 拒绝，必须创建
全新空数据库。旧迁移 CLI、Make target、ledger 与 marker 已删除，baseline downgrade 固定拒绝。
M7 其余 legacy source/API 清理与 M8 完整发布验收
尚未交付，因此当前仍不能
作为完整多用户 SaaS 发布。

M7 Task 2 已建立严格、digest 校验、单事务且幂等的 packaged system asset bootstrap；固定 non-login
builtin principal 只写 PostgreSQL published system Agent/Skill/MCP rows，不创建项目 membership、binding
或 credential。`AssetCatalogStateRow` 与 provider 不再有 asset cutover 状态，harness lookup 必须有
PostgreSQL provider。运行期 Skill/MCP 只来自 admission 持久化的 exact snapshot 与 run-local
read-only materialization；repo/user/custom/extensions scan、archive install、`skill_manage` 文件 mutation
以及 `/api/agents`、`/api/skills`、`/api/mcp/config`、`/api/features` 已删除。M7 仍未整体完成。

M7 Task 3 已删除 Gateway global Thread/Run/Memory/upload/artifact/feedback HTTP surface、
in-process agent runtime、orphan Thread startup migration 和 `/api/langgraph/*` rewrite。
Gateway 只初始化 project-scoped checkpointer、PostgreSQL private Run/event stores 及
quota/audit/asset/project services；`app.private_work.http_runtime` 只负责 project Run admission
和 SSE frame formatting，执行仍由独立 Worker 完成。M7 仍未整体完成。

M7 Task 4 已删除 global `/api/scheduled-tasks*`、legacy Automation read adapter 和
marker-derived Automation cutover guard。`/api/projects/{project_id}/automations*` 是唯一
Automation HTTP surface；底层 `scheduled_tasks` / `scheduled_task_runs` 表和 scoped repositories
继续作为最终 project Automation persistence。独立 Scheduler 通过
`AutomationSchedulerService` 的 caller-owned transaction 操作完成 terminal reconciliation 与
due occurrence/Run/job 原子 admission。M7 仍未整体完成。

M7 Task 5 已删除 global channel/channel-connections/console/input-polish HTTP surface 和
legacy channel connection repository。项目连接只使用
`/api/projects/{project_id}/connections*`，input polish 只使用
`/api/projects/{project_id}/private-work/input-polish` 并在模型调用前重新验证
`private_work.create`、`shared_assets.execute`、Thread Agent snapshot 与 Credential grant
closure。IM inbound 只从 PostgreSQL connected row 恢复 exact account/project/owner/connection
authority，不允许 default/recent/unique-membership/auth-disabled fallback；system operations
只返回脱敏 provider health 聚合。M7 仍未整体完成。

Scheduled-task note:
- Project Automation is available only under `/projects/{project_slug}/automations` and `/api/projects/{project_id}/automations*`; the global scheduled-task HTTP API has been removed. `config.yaml -> scheduler.enabled` gates an independent Scheduler process rather than a Gateway lifespan task.
- Scheduled background runs are intentionally non-interactive: they execute through the normal Worker run lifecycle, but the lead-agent toolset excludes `ask_clarification` when `context.non_interactive=true`. `AutomationDispatcher` writes that flag as server-owned admission data in the atomic occurrence/Run/job transaction; client-supplied `context.non_interactive` is dropped.
- Project Automation occurrence, private Run/snapshot, and `automation_run` job now commit atomically. Gateway retains manual admission but never constructs Scheduler ownership or a poller. When enabled, `make scheduler` owns the process-lifetime PostgreSQL session advisory lock on a dedicated connection; each poll verifies the same backend PID and existing lock without reacquiring it, and ownership loss exits polling/process lifetime. A competing Scheduler may take over only after PostgreSQL releases the old session lock. Worker startup always reconciles already-admitted terminal Runs before claiming jobs, and enabled Scheduler startup performs the same idempotent reconciliation before polling; neither path interrupts or replays active Worker work. Disabled Scheduler mode takes no lock or poll task while manual APIs and Worker restart reconciliation remain available.
- Database setup installs the only M7 baseline on a new empty database; in-place M1–M6 upgrades are unsupported.
- Normal final-M6 backup, external tombstone journal, new-database restore, failure decisions, and the separate restore drill follow `docs/operations/m6-backup-recovery.md`; M6 never downgrades or restores in place.

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
make migrate-db  # 验证或初始化空 PostgreSQL 目标库；旧库必须重建
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
- **PostgreSQL release gate** — root `Makefile` 的 `PROJECT_FOUNDATION_POSTGRES_TESTS`
  是真实 PostgreSQL gate 的唯一有序来源；覆盖 M7 baseline、M2–M5 runtime integration 以及 M6
  process/job/stream/quota/audit/recovery/multi-process release 边界。每个数据库测试只创建
  随机 `deerflow_test_*`/`deerflow_restore_*` 数据库。Release evidence 必须通过
  `POSTGRES_TEST_URL=... make test-project-foundation-postgres` 运行并保持 0 skip；跨平台 Python runner 和
  `.github/workflows/project-foundation-postgres-tests.yml` 都会在变量缺失时于 pytest 前硬失败。
- **M7 fresh-install baseline** — `migrations/versions/` 只允许
  `0001_project_saas_baseline.py`；`down_revision=None`，downgrade 永远拒绝。`make setup-db` 在空库安装
  final application schema、builtin catalog、LangGraph schema 与 default project；`make check-db` 只读验证
  exact revision 和必需表。真实测试只准使用随机 `deerflow_test_*`，绝不连接业务库。
- **M3 平台资产管理** — `/admin/assets` 由 server layout 强制限制为 `system_admin`，普通用户返回
  404。Credential create/replace 使用 imperative authenticated API，不得把 secret-bearing input
  放入 TanStack Query/Mutation cache；MCP Credential slot 只能走 submit/approve。轮换状态 GET
  使用 rotation CLI 相同 eligibility，只返回 eligible/current/pending 聚合与状态，不返回 key ID、
  nonce、ciphertext 或 storage locator。
