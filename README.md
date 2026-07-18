# 🦌 DeerFlow - 2.0

English | [中文](./README_zh.md) | [日本語](./README_ja.md) | [Français](./README_fr.md) | [Русский](./README_ru.md)

[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](./backend/pyproject.toml)
[![Node.js](https://img.shields.io/badge/Node.js-22%2B-339933?logo=node.js&logoColor=white)](./Makefile)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

<a href="https://trendshift.io/repositories/14699" target="_blank"><img src="https://trendshift.io/api/badge/repositories/14699" alt="bytedance%2Fdeer-flow | Trendshift" style="width: 250px; height: 55px;" width="250" height="55"/></a>
> On February 28th, 2026, DeerFlow claimed the 🏆 #1 spot on GitHub Trending following the launch of version 2. Thanks a million to our incredible community — you made this happen! 💪🔥

DeerFlow (**D**eep **E**xploration and **E**fficient **R**esearch **Flow**) is an open-source **super agent harness** that orchestrates **sub-agents**, **memory**, and **sandboxes** to do almost anything — powered by **extensible skills**.

https://github.com/user-attachments/assets/a8bcadc4-e040-4cf2-8fda-dd768b999c18

> [!NOTE]
> **DeerFlow 2.0 is a ground-up rewrite.** It shares no code with v1. If you're looking for the original Deep Research framework, it's maintained on the [`1.x` branch](https://github.com/bytedance/deer-flow/tree/main-1.x) — contributions there are still welcome. Active development has moved to 2.0.

## Official Website

Learn more and see **real demos** on our [**official website**](https://deerflow.tech).

## Coding Plan from ByteDance Volcengine

- We strongly recommend using Doubao-Seed-2.0-Code, DeepSeek v3.2 and Kimi 2.5 to run DeerFlow
- [Learn more](https://www.byteplus.com/en/activity/codingplan?utm_campaign=deer_flow&utm_content=deer_flow&utm_medium=devrel&utm_source=OWO&utm_term=deer_flow)
- [中国大陆地区的开发者请点击这里](https://www.volcengine.com/activity/codingplan?utm_campaign=deer_flow&utm_content=deer_flow&utm_medium=devrel&utm_source=OWO&utm_term=deer_flow)

## InfoQuest

DeerFlow has newly integrated the intelligent search and crawling toolset independently developed by BytePlus--[InfoQuest (supports free online experience)](https://docs.byteplus.com/en/docs/InfoQuest/What_is_Info_Quest)

<a href="https://docs.byteplus.com/en/docs/InfoQuest/What_is_Info_Quest" target="_blank">
  <img
    src="https://sf16-sg.tiktokcdn.com/obj/eden-sg/hubseh7bsbps/20251208-160108.png"   alt="InfoQuest_banner"
  />
</a>

---

## Table of Contents

- [🦌 DeerFlow - 2.0](#-deerflow---20)
  - [Official Website](#official-website)
  - [Coding Plan from ByteDance Volcengine](#coding-plan-from-bytedance-volcengine)
  - [InfoQuest](#infoquest)
  - [Table of Contents](#table-of-contents)
  - [One-Line Agent Setup](#one-line-agent-setup)
  - [Quick Start](#quick-start)
    - [Configuration](#configuration)
    - [Running the Application](#running-the-application)
      - [Deployment Sizing](#deployment-sizing)
      - [Option 1: Docker (Recommended)](#option-1-docker-recommended)
      - [Option 2: Local Development](#option-2-local-development)
    - [Advanced](#advanced)
      - [Sandbox Mode](#sandbox-mode)
      - [MCP Server](#mcp-server)
      - [IM Channels](#im-channels)
      - [LangSmith Tracing](#langsmith-tracing)
      - [Langfuse Tracing](#langfuse-tracing)
      - [Using Both Providers](#using-both-providers)
  - [From Deep Research to Super Agent Harness](#from-deep-research-to-super-agent-harness)
  - [Core Features](#core-features)
    - [Skills \& Tools](#skills--tools)
      - [Claude Code Integration](#claude-code-integration)
    - [Session Goals](#session-goals)
    - [Manual Context Compaction](#manual-context-compaction)
    - [Sub-Agents](#sub-agents)
    - [Sandbox \& File System](#sandbox--file-system)
    - [Context Engineering](#context-engineering)
    - [Long-Term Memory](#long-term-memory)
  - [Recommended Models](#recommended-models)
  - [Embedded Python Client](#embedded-python-client)
  - [Project Automations](#project-automations)
  - [Terminal Workbench (TUI)](#terminal-workbench-tui)
  - [Documentation](#documentation)
  - [⚠️ Security Notice](#️-security-notice)
    - [Improper Deployment May Introduce Security Risks](#improper-deployment-may-introduce-security-risks)
    - [Security Recommendations](#security-recommendations)
  - [Contributing](#contributing)
  - [License](#license)
  - [Acknowledgments](#acknowledgments)
    - [Key Contributors](#key-contributors)
  - [Star History](#star-history)

## One-Line Agent Setup

If you use Claude Code, Codex, Cursor, Windsurf, or another coding agent, you can hand it the setup instructions in one sentence:

```text
Help me clone DeerFlow if needed, then bootstrap it for local development by following https://raw.githubusercontent.com/bytedance/deer-flow/main/Install.md
```

That prompt is intended for coding agents. It tells the agent to clone the repo if needed, choose Docker when available, and stop with the exact next command plus any missing config the user still needs to provide.

## Quick Start

### Configuration

1. **Clone the DeerFlow repository**

   ```bash
   git clone https://github.com/bytedance/deer-flow.git
   cd deer-flow
   ```

2. **Run the setup wizard**

   From the project root directory (`deer-flow/`), run:

   ```bash
   make setup
   ```

   This launches an interactive wizard that guides you through choosing an LLM provider, optional web search, and execution/safety preferences such as sandbox mode, bash access, and file-write tools. It generates a minimal `config.yaml` and writes your keys to `.env`. Takes about 2 minutes.

   The wizard also lets you configure an optional web search provider, or skip it for now.

   Run `make doctor` at any time to verify your setup and get actionable fix hints.
   If you are opening a GitHub issue about a local setup or runtime problem, run
   `make support-bundle`. The command prints reporter next steps, writes a
   `*-issue-summary.md` file to paste into the issue, a `*-issue-draft.md` file
   for AI-assisted issue filing, and an optional evidence zip under
   `.deer-flow/support-bundles/`. If an AI assistant files the issue, start from
   the draft and replace every REQUIRED placeholder instead of inventing missing
   facts. Attach the zip only if a maintainer asks for it, or if the summary
   alone is not enough. Maintainers and AI triage tools can start with
   `triage.json`; the bundle includes redacted diagnostics and file manifests
   only, and does not include `.env`, raw conversation messages, or user file
   contents.

   > **Advanced / manual configuration**: If you prefer to edit `config.yaml` directly, run `make config` instead to copy the full template. See `config.example.yaml` for the complete reference including CLI-backed providers (Codex CLI, Claude Code OAuth), OpenRouter, Responses API, and more.

   <details>
   <summary>Manual model configuration examples</summary>

   ```yaml
   models:
     - name: gpt-4o
       display_name: GPT-4o
       use: langchain_openai:ChatOpenAI
       model: gpt-4o
       api_key: $OPENAI_API_KEY

     - name: openrouter-gemini-2.5-flash
       display_name: Gemini 2.5 Flash (OpenRouter)
       use: langchain_openai:ChatOpenAI
       model: google/gemini-2.5-flash-preview
       api_key: $OPENROUTER_API_KEY
       base_url: https://openrouter.ai/api/v1

     - name: gpt-5-responses
       display_name: GPT-5 (Responses API)
       use: langchain_openai:ChatOpenAI
       model: gpt-5
       api_key: $OPENAI_API_KEY
       use_responses_api: true
       output_version: responses/v1

     - name: qwen3-32b-vllm
       display_name: Qwen3 32B (vLLM)
       use: deerflow.models.vllm_provider:VllmChatModel
       model: Qwen/Qwen3-32B
       api_key: $VLLM_API_KEY
       base_url: http://localhost:8000/v1
       supports_thinking: true
       when_thinking_enabled:
         extra_body:
           chat_template_kwargs:
             enable_thinking: true
   ```

   OpenRouter and similar OpenAI-compatible gateways should be configured with `langchain_openai:ChatOpenAI` plus `base_url`. If you prefer a provider-specific environment variable name, point `api_key` at that variable explicitly (for example `api_key: $OPENROUTER_API_KEY`).

   To route OpenAI models through `/v1/responses`, keep using `langchain_openai:ChatOpenAI` and set `use_responses_api: true` with `output_version: responses/v1`.

   For vLLM 0.19.0, use `deerflow.models.vllm_provider:VllmChatModel`. For Qwen-style reasoning models, DeerFlow toggles reasoning with `extra_body.chat_template_kwargs.enable_thinking` and preserves vLLM's non-standard `reasoning` field across multi-turn tool-call conversations. Legacy `thinking` configs are normalized automatically for backward compatibility. Reasoning models may also require the server to be started with `--reasoning-parser ...`. If your local vLLM deployment accepts any non-empty API key, you can still set `VLLM_API_KEY` to a placeholder value.

   CLI-backed provider examples:

   ```yaml
   models:
     - name: gpt-5.4
       display_name: GPT-5.4 (Codex CLI)
       use: deerflow.models.openai_codex_provider:CodexChatModel
       model: gpt-5.4
       supports_thinking: true
       supports_reasoning_effort: true

     - name: claude-sonnet-4.6
       display_name: Claude Sonnet 4.6 (Claude Code OAuth)
       use: deerflow.models.claude_provider:ClaudeChatModel
       model: claude-sonnet-4-6
       max_tokens: 4096
       supports_thinking: true
   ```

   - Codex CLI reads `~/.codex/auth.json`
   - Claude Code accepts `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_CREDENTIALS_PATH`, or `~/.claude/.credentials.json`
   - ACP agent entries are separate from model providers — if you configure `acp_agents.codex`, point it at a Codex ACP adapter such as `npx -y @zed-industries/codex-acp`
   - On macOS, export Claude Code auth explicitly if needed:

   ```bash
   eval "$(python3 scripts/export_claude_code_oauth.py --print-export)"
   ```

   API keys can also be set manually in `.env` (recommended) or exported in your shell:

   ```bash
   OPENAI_API_KEY=your-openai-api-key
   TAVILY_API_KEY=your-tavily-api-key
   ```

   </details>

### Running the Application

#### Deployment Sizing

Use the table below as a practical starting point when choosing how to run DeerFlow:

| Deployment target | Starting point | Recommended | Notes |
|---------|-----------|------------|-------|
| Local evaluation / `make dev` | 4 vCPU, 8 GB RAM, 20 GB free SSD | 8 vCPU, 16 GB RAM | Good for one developer or one light session with hosted model APIs. `2 vCPU / 4 GB` is usually not enough. |
| Docker development / `make docker-start` | 4 vCPU, 8 GB RAM, 25 GB free SSD | 8 vCPU, 16 GB RAM | Image builds, bind mounts, and sandbox containers need more headroom than pure local dev. |
| Long-running server / `make up` | 8 vCPU, 16 GB RAM, 40 GB free SSD | 16 vCPU, 32 GB RAM | Preferred for shared use, multi-agent runs, report generation, or heavier sandbox workloads. |

- These numbers cover DeerFlow itself. If you also host a local LLM, size that service separately.
- Linux plus Docker is the recommended deployment target for a persistent server. macOS and Windows are best treated as development or evaluation environments.
- If CPU or memory usage stays pinned, reduce concurrent runs first, then move to the next sizing tier.

#### Option 1: Docker (Recommended)

**Development** (hot-reload, source mounts):

```bash
make docker-init    # Pull sandbox image (only once or when image updates)
make docker-start   # Start services (auto-detects sandbox mode from config.yaml)
```

`make docker-start` starts `provisioner` only when `config.yaml` uses provisioner mode (`sandbox.use: deerflow.community.aio_sandbox:AioSandboxProvider` with `provisioner_url`).

Docker builds use the upstream `uv` registry by default. If you need faster mirrors in restricted networks, export `UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple` and `NPM_REGISTRY=https://registry.npmmirror.com` before running `make docker-init` or `make docker-start`.

Backend processes automatically pick up `config.yaml` changes on the next config access, so model metadata updates do not require a manual restart during development.

> [!TIP]
> On Linux, if Docker-based commands fail with `permission denied while trying to connect to the Docker daemon socket at unix:///var/run/docker.sock`, add your user to the `docker` group and re-login before retrying. See [CONTRIBUTING.md](CONTRIBUTING.md#linux-docker-daemon-permission-denied) for the full fix.

**Production** (builds images locally, mounts runtime config and data):

```bash
make up     # Build images and start all production services
make down   # Stop and remove containers
```

Access: http://localhost:2026

DeerFlow requires PostgreSQL for persistence. Set `DATABASE_URL` to a
`postgresql://...` or `postgresql+asyncpg://...` connection URL; the unified
`database` section supplies the LangGraph checkpointer, LangGraph Store, and
DeerFlow application data. PostgreSQL drivers are installed by default, and
the former independent `checkpointer` section is no longer accepted. Runtime
startup validates the configured database but does not create it; provision the
target database before starting DeerFlow.

### PostgreSQL 初始化与 SQLite 切换（M1）

M1 运行期只支持 PostgreSQL，不提供 SQLite 回退，也不依赖 PostgreSQL RLS。授权边界由
认证身份、`ProjectContext` 和强制作用域 repository 共同执行；应用连接使用普通非
superuser role，建库与 migration 属于显式 trusted operations。

本地可启动独立 PostgreSQL 容器（当前 DeerFlow compose stack 不会自动创建数据库）：

```bash
export POSTGRES_PASSWORD='<local-password>'
docker run --name deerflow-postgres -d -p 5432:5432 \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
  -e POSTGRES_DB=postgres postgres:17-alpine
docker exec deerflow-postgres psql -U postgres -d postgres \
  -c "CREATE ROLE deerflow LOGIN PASSWORD '<app-password>'"
export POSTGRES_ADMIN_URL='postgresql+asyncpg://postgres:<url-encoded-local-password>@127.0.0.1:5432/postgres'
export DATABASE_URL='postgresql+asyncpg://deerflow:<url-encoded-app-password>@127.0.0.1:5432/deerflow'
make setup-db
make check-db
```

已有目标库只需运行 `make migrate-db`。从 legacy SQLite 切换时，先进入维护窗口，保留
repo 外只读备份，并先 dry-run。下面的普通路径只适用于不含 legacy private Thread/run/
event/feedback 的来源：

```bash
make migrate-sqlite ARGS="--source /path/legacy.db --backup-dir /path/backups --dry-run"
make migrate-sqlite ARGS="--source /path/legacy.db --backup-dir /path/backups"
make check-db
```

如果 SQLite 含上述 private rows，不要先运行会把目标直接升到 head 的 `make setup-db` 或
`make migrate-db`；必须使用下方 M4 的 0007 staging 顺序和 `--m4-staging-target`。

迁移器不会修改 SQLite `db`/`wal`/`shm` 来源；失败时保持旧版本和只读备份，修复后重跑。
产生 PostgreSQL 新写入后使用前向 migration 修复，不执行破坏性 downgrade。M1 只交付
PostgreSQL 与项目基础；项目私有入口必须等到 M4 的 final schema、cutover marker 和
readiness gate 都就绪后才开放。

### 工作空间与项目治理（M2）

登录后的默认入口 `/workspace` 是多项目工作空间：页面展示多个项目卡片、待兑换邀请和
可恢复项目，但不显示项目级左侧菜单。只有进入 `/projects/{project_slug}` 后才加载项目
菜单，并提供项目概览、成员与邀请、项目设置以及返回工作空间入口。成员、邀请、角色、
退出、移除和项目删除恢复都由服务端返回的能力与作用域 repository 授权，前端不根据
角色推导权限。

M2 不接入邮件服务；Admin 创建邀请后只获得一次性 fragment 链接。M2 不使用
PostgreSQL RLS，也不物理清除成员私有数据或项目数据，只记录 30 天保留或恢复窗口。

### 共享资产治理 API（M3）

项目成员通过 `/api/projects/{project_id}/agents|skills|mcp-servers|credentials` 查看明确分开的
系统资产与项目资产，并按 capability 创建版本、发布、审批、管理 credential 和固定 system
binding。`system_admin` 使用 `/api/admin/assets/*` 管理系统资产，或使用
`/api/admin/projects/{project_id}/assets/*` 对目标项目共享资产执行显式治理 override；该权限不会
产生项目 membership，也不会开放成员或私有 Thread、run、file、Memory、automation 数据。

Credential API 只返回名称、类型、状态、版本和时间等脱敏元数据。plaintext、ciphertext、nonce、
key ID、storage locator 与 secret hash 永远不会出现在 HTTP 响应中。

平台管理员可从 `/admin/assets` 进入独立的系统资产管理区，管理 Agent、Skill、MCP 与
Credential 的创建、版本、发布和生命周期。含 Credential slot 的 MCP 版本只能经过
submit/approve 流程发布；Credential 页面不提供明文查看或复制，并显示与 rotation CLI
相同 eligibility 口径的 envelope 聚合状态。该状态接口只返回 eligible/current/pending 数量
与状态，不暴露 key ID 或 envelope 存储字段。

进入项目后，`/projects/{project_slug}/{agents,skills,mcp,credentials}` 分别展示系统资产与项目
资产；系统资产只允许按 capability 固定、升级、回退或停用 binding，项目资产保留独立版本历史。
旧 `/workspace/{agents,skills,tools}` 入口已删除；Agent、Skill、MCP 和 Credential 只通过项目页或
`/admin/assets` 管理。所有 M3 项目页都不提供运行或开始对话入口。

完成 catalog cutover 后，系统 Agent、Skill 与 MCP 的运行时和旧版 GET 接口只读取 PostgreSQL
已发布版本；旧文件即使仍存在也不会回退使用。旧版文件写接口统一返回
`409 ASSET_CATALOG_CUTOVER`，请改用 `/admin/assets` 管理系统资产。
M3 的兼容运行不会构造项目运行身份：credential materialization 只接受应用内部解析出的真实、
不可变 `ProjectContext`。

### 项目私有工作（M4 已完成）

进入 `/projects/{project_slug}` 后，服务端 readiness 为 `ready` 且当前成员具有
`private_work.read_own` 时，项目菜单会显示 Chats、Memory 和 Connections。Chats 支持创建和继续
自己的私有对话、运行与停止 Agent、上传文件、下载或删除自己的文件/产物，以及 chat sidecar；
项目 Memory 可列出和导出，具有写能力的成员还可 reload/import/update/delete。Connections 绑定
项目内可执行 Agent，普通入站文本复用同一项目私有 run lifecycle。

Viewer 只能读取、导出和删除自己的既有 Thread、file 与 Memory 数据，不能创建 Thread、启动 run、
上传文件、修改 Memory 或创建 connection。其他项目成员、项目 Admin 和平台 `system_admin` 不会因此
获得 owner 私有内容读取权。所有页面和请求同时按 authenticated account UUID 与 project UUID 分区；
切换账号或项目时先取消查询，再清理 cache、reconnect metadata 和 LangGraph client。

Project-private backend API 基路径
`/api/projects/{project_id}/private-work` 提供 readiness、Thread、run/stream/wait、feed
（messages/events/token usage/feedback）、file 和 artifact 接口；项目 Memory 管理位于
`/api/projects/{project_id}/memory`。这些接口复用 Gateway 的 run lifecycle，并按项目与 owner
双重隔离；项目 run/feed 的消息与事件始终使用 PostgreSQL，因此 Gateway 重启后仍可读取，即使
legacy `run_events.backend` 保持默认 `memory`。runnable-first 显式迁移命令首版覆盖
PostgreSQL legacy Thread/run/event/feedback 与 checkpoint scope marker；要求每个 legacy owner UUID
显式映射到 active project UUID，不推断 default/recent/unique project。运行期 cutover guard 只有 final schema 与
`private_work_cutover_state.stage=cutover_complete` 同时满足时项目私有 API 才开放；marker 完成后，
旧 Thread/run/Memory/channel connection/upload/artifact HTTP 入口与 shared `start_run` 会统一返回
`409 PRIVATE_WORK_CUTOVER`。非空 legacy filesystem、Memory 或 connection source 当前会在 execute 前
安全拒绝。完整操作顺序与故障决策见
[M4 private-work migration runbook](docs/operations/m4-private-work-migration.md)。

进入维护窗口并先完成外部 PostgreSQL 备份。owner map 是一个只包含 UUID→UUID 的 JSON object；
`--backup-dir` 是当前 CLI 的保留运维参数，首版不会在其中写文件：

```bash
export DATABASE_URL='postgresql+asyncpg://...'
make migrate-private-work ARGS="--dry-run --owner-map /secure/private-work-owner-map.json --backup-dir /secure/backups"
make migrate-private-work ARGS="--execute --owner-map /secure/private-work-owner-map.json --backup-dir /secure/backups"
```

如果 private rows 仍在 legacy SQLite，目标必须先停在 0007，不能先创建 final/head schema。
`DATABASE_URL` 应指向专用的新库或已验证的 0007 库；新库由 `POSTGRES_ADMIN_URL` 创建：

```bash
export POSTGRES_ADMIN_URL='postgresql+asyncpg://postgres:<encoded-password>@127.0.0.1:5432/postgres'
export DATABASE_URL='postgresql+asyncpg://deerflow:<encoded-password>@127.0.0.1:5432/deerflow_m4_cutover'
make setup-m4-migration-db
make migrate-sqlite ARGS="--m4-staging-target --source /path/legacy.db --backup-dir /secure/sqlite-backups --dry-run"
make migrate-sqlite ARGS="--m4-staging-target --source /path/legacy.db --backup-dir /secure/sqlite-backups"
```

随后在同一 0007 库中确认每个 migrated user 已有 active project membership，且每个 legacy
`assistant_id` 对应一个已发布的 system/project Agent（先完成适用的 M2/M3 authority provisioning）。
再生成 owner map 并执行上面的 `migrate-private-work` dry-run/execute。SQLite importer 会验证目标
revision 恰为 `0007_project_shared_assets`，按实际 0007 表结构写入并保留 ledger；任何 head/final
目标都会在写入前拒绝。完整顺序见 runbook。

dry-run 只输出脱敏 counts 与稳定 source hash，不升级 schema、不写 ledger/marker，也不创建 backup
目录。execute 固定按 0008 expand、分域 ledger、0009 finalize、0010/0011、最后
`cutover_complete` marker 的顺序执行；同一已完成 cutover 再执行会直接 no-op。

M4 已于 2026-07-16 完成实现、迁移正向链、单次独立审查修复与全量门禁。M5 project Automation
也已于 2026-07-16 完成实现、迁移、全量门禁与独立关闭审查。M6 已于 2026-07-18 完成 durable job、
独立 Worker、Worker-only private run、Automation 原子 admission、独立 Scheduler、PostgreSQL durable
stream/SSE reconnect、配额、审计、平台运营、认证加密 backup、外部删除 journal、新库 restore/drill、
显式 cutover 和真实多进程/Frontend/M1–M6 发布门禁。当前进度为 6/8（75%）；M7 legacy source/API
清理和 M8 完整发布验收仍未交付，因此 DeerFlow 仍不能作为完整多用户 SaaS 发布。

#### M3 共享资产迁移与 credential 轮换

迁移不会随应用启动自动执行。`DATABASE_URL` 指向资产权威 PostgreSQL 数据库；credential keyring
只从以下两个环境变量读取，数据库只保存带 key ID 的 AES-GCM envelope，不保存主密钥。先 dry-run
核对脱敏 inventory，再进入维护窗口执行：

```bash
export DEER_FLOW_CREDENTIAL_ACTIVE_KEY_ID='m3-current'
export DEER_FLOW_CREDENTIAL_KEYRING_JSON='{"m3-current":"<32-byte-key-base64>"}'
make migrate-assets ARGS="--dry-run --actor-user-id <system-admin-uuid> --owner-map /secure/owner-map.json"
make migrate-assets ARGS="--execute --actor-user-id <system-admin-uuid> --owner-map /secure/owner-map.json"
```

来源包含项目根 `config.yaml` 对应的默认 Agent、repo system Agent、`skills/public`、canonical
`extensions_config.json`/`mcp_config.json`，以及 `.deer-flow`（或 `DEER_FLOW_HOME`）下的用户
Agent/Skill。`owner-map.json` 的 `default_projects` 必须显式映射 user UUID 到其 active default
project UUID；legacy shared custom 来源还必须设置 `legacy_shared_owner`，不得自动归入 system 或
任意项目。`system_actor` 可写在 map 中，也可用 `--actor-user-id` 覆盖。

execute 在 `.deer-flow/migrations/assets/<run-id>/` 写 0700 目录与 0600 文件；含 legacy secret 的
source snapshot 使用认证加密，脱敏 ledger 保存原始 checksum、大小和可精确恢复的相对路径，不输出
明文、ciphertext 或 nonce。全部 scope/owner/dependency 预检成功后才写资产；counts、canonical
checksums、dependency closure、credential decrypt 四类 probe 全部通过后才写 cutover marker。

轮换时 keyring 必须同时保留旧 key 和目标 key，且目标 `--key-id` 必须是 active key：

```bash
make rotate-credentials ARGS="--dry-run --key-id m3-next --batch-size 100"
make rotate-credentials ARGS="--execute --key-id m3-next --batch-size 100"
```

轮换按独立事务分批并使用 gap-safe `SKIP LOCKED` 重扫；`resume_cursor` 仅是审计 checkpoint，完成由
“不存在仍使用非目标 key 的 envelope”证明，暂时被锁的小 UUID 不会被越过。认证失败或 payload
tamper 会回滚当前批，已提交批保持可安全续跑；旧 envelope 仅在新 envelope 验证成功后变为 inactive。
真实 PostgreSQL 回归只能使用具备建库/删库权限的隔离测试 maintenance URL，并且只创建随机
`deerflow_test_*` 数据库；禁止把业务库或 production URL 用作 `POSTGRES_TEST_URL`。

The unified nginx endpoint is same-origin by default and does not emit browser CORS headers. If you run a split-origin or port-forwarded browser client, set `GATEWAY_CORS_ORIGINS` to comma-separated exact origins such as `http://localhost:3000`; the Gateway then applies the CORS allowlist and matching CSRF origin checks.

> [!IMPORTANT]
> Under final M6 cutover, project-private and Automation runs are durable jobs consumed by the independent Worker; Gateway no longer executes them or owns Automation polling. Start the backend roles separately with `make gateway`, `make worker`, and—when `scheduler.enabled=true`—`make scheduler`. The Scheduler owns and verifies one PostgreSQL session advisory lock on its original backend connection; lock/PID/session loss fail-stops that process. Worker startup always performs terminal-only Automation reconciliation before claiming jobs, and enabled Scheduler startup repeats that idempotent check before polling; neither path replays or interrupts active Worker work. Worker stream frames are store-first PostgreSQL facts with one terminal frame per Run; Gateway SSE replay uses canonical `Last-Event-ID`, and the frontend persists and deduplicates cursors per account/project/thread. M6 service orchestration, quotas, audit, recovery, and release gates are complete; M7 legacy cleanup and M8 final release acceptance still prevent a complete production SaaS claim.

See [CONTRIBUTING.md](CONTRIBUTING.md) for detailed Docker development guide.

#### Option 2: Local Development

If you prefer running services locally:

Prerequisite: complete the "Configuration" steps above first (`make setup`). `make dev` requires a valid `config.yaml` in the project root. Set `DEER_FLOW_PROJECT_ROOT` to define that root explicitly, or `DEER_FLOW_CONFIG_PATH` to point at a specific config file. Runtime state defaults to `.deer-flow` under the project root and can be moved with `DEER_FLOW_HOME`; skills default to `skills/` under the project root and can be moved with `DEER_FLOW_SKILLS_PATH`. Run `make doctor` to verify your setup before starting.
On Windows, run the local development flow from Git Bash. Native `cmd.exe` and PowerShell shells are not supported for the bash-based service scripts, and WSL is not guaranteed because some scripts rely on Git for Windows utilities such as `cygpath`.

1. **Check prerequisites**:
   ```bash
   make check  # Verifies Node.js 22+, pnpm, uv, nginx
   ```

2. **Install dependencies**:
   ```bash
   make install  # Install backend + frontend dependencies + pre-commit hooks
   ```

3. **(Optional) Pre-pull sandbox image**:
   ```bash
   # Recommended if using Docker/Container-based sandbox
   make setup-sandbox
   ```

4. **(Optional) Load sample memory data for local review**:
   ```bash
   python scripts/load_memory_sample.py
   ```
   This copies the sample fixture into the default local runtime memory file. Project Memory review uses the entered project's `/projects/{project_slug}/memory` page.
   See [backend/docs/MEMORY_SETTINGS_REVIEW.md](backend/docs/MEMORY_SETTINGS_REVIEW.md) for the shortest review flow.

5. **Start services**:
   ```bash
   make dev
   ```

6. **Access**: http://localhost:2026

#### Startup Modes

DeerFlow runs the agent runtime inside the Gateway API. Development mode enables hot-reload; production mode uses a pre-built frontend.

| | **Local Foreground** | **Local Daemon** | **Docker Dev** | **Docker Prod** |
|---|---|---|---|---|
| **Dev** | `./scripts/serve.sh --dev`<br/>`make dev` | `./scripts/serve.sh --dev --daemon`<br/>`make dev-daemon` | `./scripts/docker.sh start`<br/>`make docker-start` | — |
| **Prod** | `./scripts/serve.sh --prod`<br/>`make start` | `./scripts/serve.sh --prod --daemon`<br/>`make start-daemon` | — | `./scripts/deploy.sh`<br/>`make up` |

| Action | Local | Docker Dev | Docker Prod |
|---|---|---|---|
| **Stop** | `./scripts/serve.sh --stop`<br/>`make stop` | `./scripts/docker.sh stop`<br/>`make docker-stop` | `./scripts/deploy.sh down`<br/>`make down` |
| **Restart** | `./scripts/serve.sh --restart [flags]` | `./scripts/docker.sh restart` | — |

Gateway owns `/api/langgraph/*` and translates those public LangGraph-compatible paths to its native `/api/*` routers behind nginx.

#### Docker Production Deployment

`deploy.sh` supports building and starting separately:

```bash
# One-step (build + start)
deploy.sh

# Two-step (build once, start later)
deploy.sh build              # build all images
deploy.sh start              # start pre-built images

# Stop
deploy.sh down
```

### Advanced
#### Sandbox Mode

DeerFlow supports multiple sandbox execution modes:
- **Local Execution** (runs sandbox code directly on the host machine)
- **Docker Execution** (runs sandbox code in isolated Docker containers)
- **Docker Execution with Kubernetes** (runs sandbox code in Kubernetes pods via provisioner service)

For Docker development, service startup follows `config.yaml` sandbox mode. In Local/Docker modes, `provisioner` is not started.

See the [Sandbox Configuration Guide](backend/docs/CONFIGURATION.md#sandbox) to configure your preferred mode.

#### MCP Server

DeerFlow supports configurable MCP servers and skills to extend its capabilities.
For HTTP/SSE MCP servers, OAuth token flows are supported (`client_credentials`, `refresh_token`).
For stdio MCP servers, per-tool call timeouts can be configured with `tool_call_timeout`.
MCP routing hints can also prefer a specific MCP tool for matching requests without forbidding other tools. When `tool_search` defers MCP schemas, matching routing metadata can auto-promote up to `tool_search.auto_promote_top_k` deferred schemas before the model call.
See the [MCP Server Guide](backend/docs/MCP_SERVER.md) for detailed instructions.

#### IM Channels

DeerFlow supports receiving tasks from messaging apps. Channels auto-start when configured — no public IP required for any of them.

DeerFlow supports user-owned IM channel connections and reuses the existing outbound `channels.*` transports, so no public IP or provider callback URL is required. Connections and provider availability use only `/api/projects/{project_id}/connections*`; bound text runs in that exact PostgreSQL account, project, owner, and connection scope. The project Connections page uses this project-only API, and the global channel binding API has been removed. See [IM Channel Connections](backend/docs/IM_CHANNEL_CONNECTIONS.md) for setup and operational notes.

| Channel | Transport | Difficulty |
|---------|-----------|------------|
| Telegram | Bot API (long-polling) | Easy |
| Slack | Socket Mode | Moderate |
| Feishu / Lark | WebSocket | Moderate |
| WeChat | Tencent iLink (long-polling) | Moderate |
| WeCom | WebSocket | Moderate |
| DingTalk | Stream Push (WebSocket) | Moderate |

**Configuration in `config.yaml`:**

```yaml
channels:
  # LangGraph-compatible Gateway API base URL (default: http://localhost:8001/api)
  langgraph_url: http://localhost:8001/api
  # Gateway API URL (default: http://localhost:8001)
  gateway_url: http://localhost:8001

  # Optional: global session defaults for all mobile channels
  session:
    assistant_id: lead_agent  # or a custom agent name; custom agents are routed via lead_agent + agent_name
    config:
      recursion_limit: 100
    context:
      thinking_enabled: true
      is_plan_mode: false
      subagent_enabled: false

  feishu:
    enabled: true
    app_id: $FEISHU_APP_ID
    app_secret: $FEISHU_APP_SECRET
    # domain: https://open.feishu.cn       # China (default)
    # domain: https://open.larksuite.com   # International

  wecom:
    enabled: true
    bot_id: $WECOM_BOT_ID
    bot_secret: $WECOM_BOT_SECRET

  slack:
    enabled: true
    bot_token: $SLACK_BOT_TOKEN     # xoxb-...
    app_token: $SLACK_APP_TOKEN     # xapp-... (Socket Mode)
    allowed_users: []               # empty = allow all

  telegram:
    enabled: true
    bot_token: $TELEGRAM_BOT_TOKEN
    allowed_users: []               # empty = allow all

  wechat:
    enabled: false
    bot_token: $WECHAT_BOT_TOKEN
    ilink_bot_id: $WECHAT_ILINK_BOT_ID
    qrcode_login_enabled: true      # optional: allow first-time QR bootstrap when bot_token is absent
    allowed_users: []               # empty = allow all
    polling_timeout: 35
    state_dir: ./.deer-flow/wechat/state
    max_inbound_image_bytes: 20971520
    max_outbound_image_bytes: 20971520
    max_inbound_file_bytes: 52428800
    max_outbound_file_bytes: 52428800

    # Optional: per-channel / per-user session settings
    session:
      assistant_id: mobile-agent  # custom agent names are also supported here
      context:
        thinking_enabled: false
      users:
        "123456789":
          assistant_id: vip-agent
          config:
            recursion_limit: 150
          context:
            thinking_enabled: true
            subagent_enabled: true

  dingtalk:
    enabled: true
    client_id: $DINGTALK_CLIENT_ID             # Client ID of your DingTalk application
    client_secret: $DINGTALK_CLIENT_SECRET     # Client Secret of your DingTalk application
    allowed_users: []                          # empty = allow all
    card_template_id: ""                       # Optional: AI Card template ID for streaming typewriter effect
```

Notes:
- `assistant_id: lead_agent` calls the default LangGraph assistant directly.
- If `assistant_id` is set to a custom agent name, DeerFlow still routes through `lead_agent` and injects that value as `agent_name`, so the custom agent's SOUL/config takes effect for IM channels.
- Project-bound IM channel workers resolve the persisted connection row and admit the exact project-private Run directly through Gateway's internal admission service; they do not call a global LangGraph-compatible API or derive project authority from message fields.

Set the corresponding API keys in your `.env` file:

```bash
# Telegram
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrSTUvwxYZ

# Slack
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...

# Feishu / Lark
FEISHU_APP_ID=cli_xxxx
FEISHU_APP_SECRET=your_app_secret

# WeChat iLink
WECHAT_BOT_TOKEN=your_ilink_bot_token
WECHAT_ILINK_BOT_ID=your_ilink_bot_id

# WeCom
WECOM_BOT_ID=your_bot_id
WECOM_BOT_SECRET=your_bot_secret

# DingTalk
DINGTALK_CLIENT_ID=your_client_id
DINGTALK_CLIENT_SECRET=your_client_secret
```

**Telegram Setup**

1. Chat with [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the HTTP API token.
2. Set `TELEGRAM_BOT_TOKEN` in `.env` and enable the channel in `config.yaml`.

**Slack Setup**

1. Create a Slack App at [api.slack.com/apps](https://api.slack.com/apps) → Create New App → From scratch.
2. Under **OAuth & Permissions**, add Bot Token Scopes: `app_mentions:read`, `chat:write`, `im:history`, `im:read`, `im:write`, `files:write`.
3. Enable **Socket Mode** → generate an App-Level Token (`xapp-…`) with `connections:write` scope.
4. Under **Event Subscriptions**, subscribe to bot events: `app_mention`, `message.im`.
5. Set `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` in `.env` and enable the channel in `config.yaml`.

**Feishu / Lark Setup**

1. Create an app on [Feishu Open Platform](https://open.feishu.cn/) → enable **Bot** capability.
2. Add permissions: `im:message`, `im:message.p2p_msg:readonly`, `im:resource`.
3. Under **Events**, subscribe to `im.message.receive_v1` and select **Long Connection** mode.
4. Copy the App ID and App Secret. Set `FEISHU_APP_ID` and `FEISHU_APP_SECRET` in `.env` and enable the channel in `config.yaml`.

**WeChat Setup**

1. Enable the `wechat` channel in `config.yaml`.
2. Either set `WECHAT_BOT_TOKEN` in `.env`, or set `qrcode_login_enabled: true` for first-time QR bootstrap.
3. When `bot_token` is absent and QR bootstrap is enabled, watch backend logs for the QR content returned by iLink and complete the binding flow.
4. After the QR flow succeeds, DeerFlow persists the acquired token under `state_dir` for later restarts.
5. For Docker Compose deployments, keep `state_dir` on a persistent volume so the `get_updates_buf` cursor and saved auth state survive restarts.

**WeCom Setup**

1. Create a bot on the WeCom AI Bot platform and obtain the `bot_id` and `bot_secret`.
2. Enable `channels.wecom` in `config.yaml` and fill in `bot_id` / `bot_secret`.
3. Set `WECOM_BOT_ID` and `WECOM_BOT_SECRET` in `.env`.
4. Make sure backend dependencies include `wecom-aibot-python-sdk`. The channel uses a WebSocket long connection and does not require a public callback URL.
5. The current integration supports inbound text, image, and file messages. Final images/files generated by the agent are also sent back to the WeCom conversation.

**DingTalk Setup**

1. Create a DingTalk application in the [DingTalk Developer Console](https://open.dingtalk.com/) and enable **Robot** capability.
2. Set the message receiving mode to **Stream Mode** in the robot configuration page.
3. Copy the `Client ID` and `Client Secret`, set `DINGTALK_CLIENT_ID` and `DINGTALK_CLIENT_SECRET` in `.env`, and enable the channel in `config.yaml`.
4. *(Optional)* To enable streaming AI Card replies (typewriter effect), create an **AI Card** template on the [DingTalk Card Platform](https://open.dingtalk.com/document/dingstart/typewriter-effect-streaming-ai-card), then set `card_template_id` in `config.yaml` to the template ID. You also need to apply for the `Card.Streaming.Write` and `Card.Instance.Write` permissions.


When DeerFlow runs in Docker Compose, IM channels execute inside the `gateway` container. The read-only `/models` channel command uses `channels.gateway_url`, which must target the Gateway service name (for example `http://gateway:8001`), not `localhost`. Project-bound Run admission is process-local and does not use `channels.langgraph_url`.

**Commands**

Once a channel is connected, you can interact with DeerFlow directly from the chat:

| Command | Description |
|---------|-------------|
| `/models` | List available models |
| `/help` | Show help |
| `/<skill-name> <task>` | Activate an enabled skill for one project-scoped turn |

> Messages without a command prefix are treated as regular project chat. Removed legacy commands (`/bootstrap`, `/goal`, `/new`, `/status`, and `/memory`) return an unsupported-command response and are never submitted as ordinary prompts.

#### Request Trace Correlation

Gateway request trace correlation is disabled by default so existing HTTP responses and log formats stay unchanged. To enable it, set:

```yaml
logging:
  enhance:
    enabled: true
    format: text
```

When enabled, every Gateway HTTP response includes `X-Trace-Id`, logs include `trace_id`, and Langfuse traces created by that request include `metadata.deerflow_trace_id` with the same value.

#### LangSmith Tracing

DeerFlow has built-in [LangSmith](https://smith.langchain.com) integration for observability. When enabled, all LLM calls, agent runs, and tool executions are traced and visible in the LangSmith dashboard.

Add the following to your `.env` file:

```bash
LANGSMITH_TRACING=true
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
LANGSMITH_API_KEY=lsv2_pt_xxxxxxxxxxxxxxxx
LANGSMITH_PROJECT=xxx
```

#### Langfuse Tracing

DeerFlow also supports [Langfuse](https://langfuse.com) observability for LangChain-compatible runs.

Add the following to your `.env` file:

```bash
LANGFUSE_TRACING=true
LANGFUSE_PUBLIC_KEY=pk-lf-xxxxxxxxxxxxxxxx
LANGFUSE_SECRET_KEY=sk-lf-xxxxxxxxxxxxxxxx
LANGFUSE_BASE_URL=https://cloud.langfuse.com
```

If you are using a self-hosted Langfuse instance, set `LANGFUSE_BASE_URL` to your deployment URL.

**Trace correlation fields.** Every agent run is annotated with Langfuse's reserved trace attributes so the Sessions and Users pages light up automatically:

- `session_id` = LangGraph `thread_id` — groups every trace of the same conversation
- `user_id` = effective user from `get_effective_user_id()` (falls back to `default` in no-auth mode)
- `trace_name` = assistant id (defaults to `lead-agent`)
- `tags` = `[env:<DEER_FLOW_ENV>, model:<model_name>]` (omitted when not set)
- `metadata.deerflow_trace_id` = DeerFlow request correlation id, matching `X-Trace-Id` when request trace correlation is enabled

These are injected into `RunnableConfig.metadata` at the graph invocation root for both the gateway path (`runtime/runs/worker.py::run_agent`) and the embedded path (`client.py::DeerFlowClient.stream`), so any LangChain-compatible callback can read them. Set `DEER_FLOW_ENV` (or `ENVIRONMENT`) to tag traces by deployment environment.

#### Using Both Providers

If both LangSmith and Langfuse are enabled, DeerFlow attaches both tracing callbacks and reports the same model activity to both systems.

If a provider is explicitly enabled but missing required credentials, or if its callback fails to initialize, DeerFlow fails fast when tracing is initialized during model creation and the error message names the provider that caused the failure.

For Docker deployments, tracing is disabled by default. Set `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` in your `.env` to enable it.

## From Deep Research to Super Agent Harness

DeerFlow started as a Deep Research framework — and the community ran with it. Since launch, developers have pushed it far beyond research: building data pipelines, generating slide decks, spinning up dashboards, automating content workflows. Things we never anticipated.

That told us something important: DeerFlow wasn't just a research tool. It was a **harness** — a runtime that gives agents the infrastructure to actually get work done.

So we rebuilt it from scratch.

DeerFlow 2.0 is no longer a framework you wire together. It's a super agent harness — batteries included, fully extensible. Built on LangGraph and LangChain, it ships with everything an agent needs out of the box: a filesystem, memory, skills, sandbox-aware execution, and the ability to plan and spawn sub-agents for complex, multi-step tasks.

Use it as-is. Or tear it apart and make it yours.

## Core Features

### Skills & Tools

Skills are what make DeerFlow do *almost anything*.

A standard Agent Skill is a structured capability module — a Markdown file that defines a workflow, best practices, and references to supporting resources. DeerFlow ships with built-in skills for research, report generation, slide creation, web pages, image and video generation, and more. But the real power is extensibility: add your own skills, replace the built-in ones, or combine them into compound workflows.

Skills are loaded progressively — only when the task needs them, not all at once. This keeps the context window lean and makes DeerFlow work well even with token-sensitive models.

In the Web UI, administrators can open a read-only rendered `SKILL.md` preview directly from the workspace Skills list without leaving it.

Users can explicitly activate an enabled skill for a single turn by starting the request with `/skill-name`, for example `/data-analysis analyze uploads/foo.csv`. DeerFlow loads that skill's `SKILL.md` as hidden current-turn context while leaving the base prompt limited to skill metadata. Slash activation respects disabled skills, custom-agent skill whitelists, and the final read-only channel commands `/models` and `/help`.

When you install `.skill` archives through the Gateway, DeerFlow accepts standard optional frontmatter metadata such as `version`, `author`, and `compatibility` instead of rejecting otherwise valid external skills.

Skill installs and agent-managed skill edits run through **SkillScan**, a native deterministic safety scanner before the LLM-based skill scanner. Phase 1 runs offline with no Semgrep/OpenGrep dependency, blocks high-confidence `CRITICAL` findings such as private keys or shell execution, and passes warning findings to the LLM scanner for contextual review. Set `skill_scan.enabled: false` in `config.yaml` to disable only the deterministic analyzers; safe archive extraction and the LLM scanner still run.

Tools follow the same philosophy. DeerFlow comes with a core toolset — web search, web fetch, rendered web capture, file operations, bash execution — and supports custom tools via MCP servers and Python functions. Swap anything. Add anything.

Gateway-generated follow-up suggestions now normalize both plain-string model output and block/list-style rich content before parsing the JSON array response, so provider-specific content wrappers do not silently drop suggestions.

The Web UI composer can polish draft input before sending. The rewrite runs as a short Gateway LLM request using the `input_polish` model configuration, keeps slash skill prefixes such as `/data-analysis`, and only replaces the local draft after the user clicks the polish button; it does not create a thread run or persist a message.

Interrupted first-turn runs still persist a fallback conversation title, so stopping a streaming response does not leave the thread as "Untitled" after refresh.

In the Web UI, completed assistant turns can be branched into a new main conversation. The new thread starts from that turn's checkpoint. Because workspace files are not checkpointed, the branch only receives a best-effort copy of the current workspace when you branch from the latest turn; branching from an older turn keeps just the restored message history so the branch never inherits files that were created in a later part of the conversation.

```
# Paths inside the sandbox container
/mnt/skills/public
├── research/SKILL.md
├── report-generation/SKILL.md
├── slide-creation/SKILL.md
├── web-page/SKILL.md
└── image-generation/SKILL.md

/mnt/skills/custom
└── your-custom-skill/SKILL.md      ← yours
```

#### Claude Code Integration

The `claude-to-deerflow` skill lets you interact with a running DeerFlow instance directly from [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Send research tasks, check status, manage threads — all without leaving the terminal.

**Install the skill**:

```bash
npx skills add https://github.com/bytedance/deer-flow --skill claude-to-deerflow
```

Then make sure DeerFlow is running (default at `http://localhost:2026`) and use the `/claude-to-deerflow` command in Claude Code.

**What you can do**:
- Send messages to DeerFlow and get streaming responses
- Choose execution modes: flash (fast), standard, pro (planning), ultra (sub-agents)
- Check DeerFlow health, list models/skills/agents
- Manage threads and conversation history
- Upload files for analysis

**Environment variables** (optional, for custom endpoints):

```bash
DEERFLOW_URL=http://localhost:2026            # Unified proxy base URL
DEERFLOW_GATEWAY_URL=http://localhost:2026    # Gateway API
DEERFLOW_LANGGRAPH_URL=http://localhost:2026/api/langgraph  # LangGraph API
```

See [`skills/public/claude-to-deerflow/SKILL.md`](skills/public/claude-to-deerflow/SKILL.md) for the full API reference.

### Session Goals

Use `/goal <completion condition>` to attach one active completion condition to the current thread. The goal is thread-scoped state, not a skill activation, so it stays active across turns until DeerFlow determines it has been satisfied or you clear it.

Supported commands:

```text
/goal finish the implementation and make all tests pass
/goal              # show the active goal
/goal clear        # clear it
```

After each Gateway-backed run, DeerFlow evaluates the visible conversation against the active goal with a non-thinking evaluator model. The evaluator must return a typed blocker (`missing_evidence`, `needs_user_input`, `run_failed`, `external_wait`, or `goal_not_met_yet`) plus visible evidence. DeerFlow only injects a hidden continuation when the latest assistant turn is durably checkpointed, the blocker is `goal_not_met_yet`, the thread did not change during evaluation, and the no-progress breaker has not fired. The safety cap defaults to 8 hidden continuations, and repeated identical non-progress evaluations stop after 2 attempts. `/goal clear` and any user-authored new input win over queued continuations. When the goal is satisfied, DeerFlow clears it automatically and publishes the updated thread state.

The Web UI shows the active goal above the composer, and the same command is available from the TUI. IM channels do not expose `/goal`; project-bound IM runs use ordinary messages or an enabled slash skill.

### Manual Context Compaction

Use `/compact` in the Web UI composer to summarize older context for the current thread. DeerFlow keeps the full chat visible, but future model calls use the compacted summary plus recent messages. The command is ignored when there is not enough history to compact, and it is blocked while the thread has a run in flight.

### Sub-Agents

Complex tasks rarely fit in a single pass. DeerFlow decomposes them.

The lead agent can spawn sub-agents on the fly — each with its own scoped context, tools, and termination conditions. Sub-agents run in parallel when possible, report back structured results, and the lead agent synthesizes everything into a coherent output. When token usage tracking is enabled, completed sub-agent usage is attributed back to the dispatching step.

This is how DeerFlow handles tasks that take minutes to hours: a research task might fan out into a dozen sub-agents, each exploring a different angle, then converge into a single report — or a website — or a slide deck with generated visuals. One harness, many hands.

### Sandbox & File System

DeerFlow doesn't just *talk* about doing things. It has its own computer.

Each task gets its own execution environment with a full filesystem view — skills, workspace, uploads, outputs. The agent reads, writes, and edits files. It can view images and, when configured safely, execute shell commands.

After each run, DeerFlow records a workspace change summary for the run-owned `workspace` and `outputs` directories. The Web UI shows a compact "files changed" badge on the assistant turn; opening it reveals created, modified, and deleted files with text diffs when safe to display. Uploads are excluded because they are user inputs, not agent-generated changes. Large, binary, or sensitive-looking files are shown as metadata only.

With `AioSandboxProvider`, shell execution runs inside isolated containers. With `LocalSandboxProvider`, file tools still map to per-thread directories on the host, but host `bash` is disabled by default because it is not a secure isolation boundary. Re-enable host bash only for fully trusted local workflows. Host bash commands have a wall-clock timeout, and long-lived processes should be started in the background with output redirected to a workspace log.

This is the difference between a chatbot with tool access and an agent with an actual execution environment.

```
# Paths inside the sandbox container
/mnt/user-data/
├── uploads/          ← your files
├── workspace/        ← agents' working directory
└── outputs/          ← final deliverables
```

### Context Engineering

**Isolated Sub-Agent Context**: Each sub-agent runs in its own isolated context. This means that the sub-agent will not be able to see the context of the main agent or other sub-agents. This is important to ensure that the sub-agent is able to focus on the task at hand and not be distracted by the context of the main agent or other sub-agents.

**Summarization**: Within a session, DeerFlow manages context aggressively — summarizing completed sub-tasks, offloading intermediate results to the filesystem, compressing what's no longer immediately relevant. This lets it stay sharp across long, multi-step tasks without blowing the context window.

**Strict Tool-Call Recovery**: When a provider or middleware interrupts a tool-call loop, DeerFlow now strips provider-level raw tool-call metadata on forced-stop assistant messages and injects placeholder tool results for dangling calls before the next model invocation. This keeps OpenAI-compatible reasoning models that strictly validate `tool_call_id` sequences from failing with malformed history errors.

### Long-Term Memory

Most agents forget everything the moment a conversation ends. DeerFlow remembers.

Across sessions, DeerFlow builds a persistent memory of your profile, preferences, and accumulated knowledge. The more you use it, the better it knows you — your writing style, your technical stack, your recurring workflows. Memory is stored locally and stays under your control.

Memory updates now skip duplicate fact entries at apply time, so repeated preferences and context do not accumulate endlessly across sessions.

## Recommended Models

DeerFlow is model-agnostic — it works with any LLM that implements the OpenAI-compatible API. That said, it performs best with models that support:

- **Long context windows** (100k+ tokens) for deep research and multi-step tasks
- **Reasoning capabilities** for adaptive planning and complex decomposition
- **Multimodal inputs** for image understanding and video comprehension
- **Strong tool-use** for reliable function calling and structured outputs

## Embedded Python Client

DeerFlow can be used as an embedded Python library without running the full HTTP services. The `DeerFlowClient` provides direct in-process access to all agent and Gateway capabilities, returning the same response schemas as the HTTP Gateway API. The HTTP Gateway also exposes `DELETE /api/threads/{thread_id}` to remove DeerFlow-managed local thread data after the LangGraph thread itself has been deleted:

```python
from deerflow.client import DeerFlowClient

client = DeerFlowClient()

# Chat
response = client.chat("Analyze this paper for me", thread_id="my-thread")

# Streaming (LangGraph SSE protocol: values, messages-tuple, end)
for event in client.stream("hello"):
    if event.type == "messages-tuple" and event.data.get("type") == "ai":
        print(event.data["content"])

# Configuration & management — returns Gateway-aligned dicts
models = client.list_models()        # {"models": [...]}
skills = client.list_skills()        # {"skills": [...]}
client.update_skill("web-search", enabled=True)
client.upload_files("thread-1", ["./report.pdf"])  # {"success": True, "files": [...]}
client.set_goal("thread-1", "finish the implementation and make all tests pass")
client.get_goal("thread-1")       # {"goal": {...}} or {"goal": None}
client.clear_goal("thread-1")
```

All dict-returning methods are validated against Gateway Pydantic response models in CI (`TestGatewayConformance`), ensuring the embedded client stays in sync with the HTTP API schemas. See `backend/packages/harness/deerflow/client.py` for full API documentation.

## Project Automations

M5 adds project Automation at
`/projects/{project_slug}/automations`. Each definition and occurrence is private to
the authenticated account and entered project. Admins, Editors, and Runners with the
server-issued capability can create, edit, pause, resume, manually trigger, inspect,
and delete their own Automations. Viewers can only inspect their own definitions and
run history.

Project Automation supports `once` and five-field `cron` schedules, a fixed `skip`
overlap policy, and either a reused private thread or a fresh private thread per run.
Every automatic or manual trigger atomically commits its durable occurrence, private
Run/snapshot, and `automation_run` job. The independent Worker consumes that job;
Worker startup and enabled Scheduler startup reconcile terminal state only and never
replay or interrupt an active admitted Run.

Current limits:

- No conversation-created `schedule_task` tool yet
- No text-only notification jobs
- No channel or GitHub dispatch targets
- No `interval` schedule type in this first cut

Enable background polling with `config.yaml -> scheduler.enabled` and run the backend
Scheduler role with `cd backend && make scheduler`. It—not Gateway—holds the PostgreSQL
scheduler ownership lock and only admits jobs. Disabling polling leaves the project API
and manual trigger available; manual trigger uses the same atomic occurrence/Run/job
path. The legacy global `/api/scheduled-tasks*` API has been removed; only the
project-scoped Automation API remains, while the existing `scheduled_tasks` and
`scheduled_task_runs` table names stay as private persistence details. PostgreSQL durable
stream writing/reading, terminal uniqueness, Gateway SSE reconnect,
frontend cursor/dedupe, and the atomic project quota core are implemented. Member, storage,
concurrent-Run, and actual MCP-dispatch quota enforcement are also wired across Gateway,
Worker, and Scheduler with stable 429/`Retry-After` responses. Tasks 16–17 add operator-only
encrypted PostgreSQL backup, journal-first purge, restore, and drill commands. Set a distinct 32-byte
base64 `DEER_FLOW_BACKUP_KEY`, `DATABASE_URL`, and the existing
`DEER_FLOW_AUDIT_ACTIVE_KEY_ID` / `DEER_FLOW_AUDIT_KEYRING_JSON` audit environment, then
write only to an external secure directory (never this repository):

```bash
make backup-db ARGS="--output /secure/backups"
```

The command exports one read-only repeatable-read PostgreSQL snapshot, derives a privacy-safe
source identity from PostgreSQL system/database authority, and binds that same snapshot to
fixed `pg_dump --format=custom --no-owner --no-acl --snapshot=...` argv. The database role
must be allowed to read `pg_control_system()`; otherwise backup fails closed. Connection
credentials use a temporary `0600` libpq passfile, never process argv, and reach `pg_dump` only
through an inherited `/dev/fd/N` descriptor; mutable absolute passfile paths are never handed to
the child. The file is removed before publication through a retained fd-relative directory handle.
Every archive-path ancestor is opened with no-follow directory semantics, writer work is settled
before cancellation cleanup, and transient identity-check/unlink/directory-fsync failures retain
passfile ownership for safe cleanup retry. Passfile ownership begins with the pinned parent, so
open or later write/fsync/lseek/validation failure cannot bypass that cleanup. When
`AUTH_JWT_SECRET` is absent, key separation reads the existing
`DEER_FLOW_HOME/.jwt_secret` without creating or rotating it; missing, unsafe, or unreadable Auth
material fails closed. The archive records the actual Alembic revision, `pg_dump` version,
non-empty byte/table counts, and a proven contiguous tombstone cursor. It uses per-archive keys
with counter nonces, no-clobber publication, and a bounded authenticated plaintext spool. The
format permits at most 65,536 chunks of at most 1 MiB (64 GiB plaintext) and a 16 MiB manifest;
writer and reader enforce the same limits. A failed or uncommitted audit removes the archive,
while the successful audit commit is the durable operation commit point: later cancellation or
engine disposal cannot delete the valid audited archive. Output remains limited to archive ID,
schema revision, chunk count, and a truncated checksum.

Existing M5 databases use the explicit forward-only
[M6 reliability migration runbook](docs/operations/m6-reliability-migration.md). First create the exact-0013 archive and its
externally committed sibling receipt, complete an isolated restore rehearsal, create the
authenticated no-clobber attestation, review the zero-write dry-run, stop every writer, and execute:

```bash
make backup-db ARGS="--output /secure/m6 --pre-m6-cutover"
make migrate-reliability ARGS="--attest-backup --archive /secure/m6/<archive_id>.dfba --backup-commit /secure/m6/<archive_id>.dfba.commit.json --proof-output /secure/m6/pre-cutover-proof.json --restore-verified"
make migrate-reliability ARGS="--dry-run --backup-proof /secure/m6/pre-cutover-proof.json"
make migrate-reliability ARGS="--execute --maintenance-acknowledged --backup-proof /secure/m6/pre-cutover-proof.json"
```

The migration attaches pending work to durable Jobs and writes the same per-resource member,
ready-file, and pending-Run reservations used by online release. It refuses aggregate-only quota
history, records the pre-M6 receipt through the trusted backup audit sink after `0014`, is resumable
there, and applies `0015` plus the final marker only after every probe.
Local launch starts Gateway and Worker separately and starts Scheduler only when
`scheduler.enabled=true`; Docker uses the same roles and Scheduler profile. System-admin readiness
returns only aggregate role/fleet/ownership/cutover state and never PIDs, lock keys, URLs, or tokens.

Retention purge additionally requires a separate base64 32-byte
`DEER_FLOW_RECOVERY_JOURNAL_KEY` and an operator-owned journal outside this repository. Each
encrypted, hash-chained tombstone is fsynced before physical deletion. File and project candidates
must pass the exact 30-day retention recheck. The recovery-only account workflow requires every
membership to be inactive and expired, deletes only the owner's private data in the exact retained
project set, and preserves the User row plus governance, job, audit, and recovery evidence. An
authenticated journal header binds the PostgreSQL installation identity; the singleton database
anchor stores its journal ID, committed sequence, and complete envelope-head digest. Purge compares
the full database prefix with that anchor and updates both in one transaction after journal fsync.

Restore only targets a nonexistent, distinct database named
`deerflow_restore_<pid>_<32hex>`. It authenticates the full archive, restores it, replays the journal
without sequence gaps, runs M1–M6 probes, and writes a restore proof bound to the frozen journal ID,
final sequence, and head digest. Restore holds the same PostgreSQL advisory authority as purge from
source-anchor verification through replay, probes, proof, and sensitive workspace cleanup, so a
concurrent tombstone cannot be omitted. The authenticated dump, passfile, and owned workspace are
identity-checked, removed, and directory-fsynced before proof; cleanup failure drops the invocation's
new target and cannot return verified. Unknown workspace files are never adopted or removed. Source
authority release is explicit and cancellation-settled: cancellation during unlock is rethrown only
after reliable release, then the invocation-owned target is removed rather than returned as verified.
The drill drops its generated target only after the same `Restorer` instance hands off an unforgeable
verified ownership token; pre-create failure, a pre-existing target, or a forged result cannot trigger
DROP. Restore never changes
`DATABASE_URL`, starts services, overwrites a database, or cuts traffic. For example:

```bash
make restore-db ARGS="--archive /secure/backups/<archive> --target-url postgresql://operator@db/deerflow_restore_1234_0123456789abcdef0123456789abcdef --journal /secure/recovery/tombstones.jsonl --execute"
make drill-restore ARGS="--archive /secure/backups/<archive> --journal /secure/recovery/tombstones.jsonl"
```

The complete operator sequence and failure decisions are in the
[M6 backup and recovery runbook](docs/operations/m6-backup-recovery.md). The drill uses one generated restore database and removes only that database after verification.
Command output contains only public proof metadata; operators must verify the proof before a
separate, manual traffic switch.

The project-scoped backend API is available at
`/api/projects/{project_id}/automations`. It provides strict create, list, read,
update, pause, resume, delete, manual-trigger, run-history, thread-filter, and
readiness endpoints. Manual trigger requires a UUID `Idempotency-Key`; disabling
background polling does not disable manual runs. During the M5 expand window the
legacy read routes remain outside the mutation freeze, while legacy mutations
return a migration-required conflict; after cutover every legacy route rejects
requests in favor of the project API. M5 completed its Task 18 full-stack gates and
independent closure review on 2026-07-16. M6 completed its reliability, governance,
recovery, migration, and release gates on 2026-07-18. Overall progress is 6/8 (75%);
M7 and M8 remain open, so DeerFlow is still not a complete releasable multi-user SaaS.

## Terminal Workbench (TUI)

`deerflow` is a terminal-native workbench for people who live in the shell. It runs **embedded** over `DeerFlowClient` — no Gateway, frontend, nginx, or Docker required — while honoring the same `config.yaml`, checkpointer, skills, memory, MCP, and sandbox settings as the rest of DeerFlow.

![DeerFlow TUI](docs/tui/tui-preview.svg)

```bash
uv pip install 'deerflow-harness[tui]'        # optional 'textual' dependency

deerflow                                      # launch the terminal UI (TTY required)
deerflow --continue                           # resume the most recent thread
deerflow --resume THREAD                      # resume a thread by id
deerflow --print "summarize this repo"        # headless one-shot answer to stdout
deerflow --json  "hello"                       # headless newline-delimited StreamEvents
```

A keyboard-driven chat surface with a streaming transcript (Markdown-rendered answers), compact tool-activity cards, a `/` slash-command palette, `/goal` goal management, `/model` and `/threads` pickers, input history, and `Esc` / `Ctrl+C` interrupt. Sessions opened in the TUI also appear in the Web UI sidebar — it writes the shared thread store under the local default user, so terminal and web stay in sync **without running the Gateway**.

See [backend/docs/TUI.md](backend/docs/TUI.md) for the full guide.

## Documentation

- [Contributing Guide](CONTRIBUTING.md) - Development environment setup and workflow
- [Configuration Guide](backend/docs/CONFIGURATION.md) - Setup and configuration instructions
- [Architecture Overview](backend/CLAUDE.md) - Technical architecture details
- [Backend Architecture](backend/README.md) - Backend architecture and API reference

## ⚠️ Security Notice

### Improper Deployment May Introduce Security Risks

DeerFlow has key high-privilege capabilities including **system command execution, resource operations, and business logic invocation**, and is designed by default to be **deployed in a local trusted environment (accessible only via the 127.0.0.1 loopback interface)**. If you deploy the agent in untrusted environments — such as LAN networks, public cloud servers, or other multi-endpoint accessible environments — without strict security measures, it may introduce security risks, including:

- **Unauthorized illegal invocation**: Agent functionality could be discovered by unauthorized third parties or malicious internet scanners, triggering bulk unauthorized requests that execute high-risk operations such as system commands and file read/write, potentially causing serious security consequences.
- **Compliance and legal risks**: If the agent is illegally invoked to conduct cyberattacks, data theft, or other illegal activities, it may result in legal liability and compliance risks.

### Security Recommendations

**Note: We strongly recommend deploying DeerFlow in a local trusted network environment.** If you need cross-device or cross-network deployment, you must implement strict security measures, such as:

- **IP allowlist**: Use `iptables`, or deploy hardware firewalls / switches with Access Control Lists (ACL), to **configure IP allowlist rules** and deny access from all other IP addresses.
- **Authentication gateway**: Configure a reverse proxy (e.g., nginx) and **enable strong pre-authentication**, blocking any unauthenticated access.
- **Network isolation**: Where possible, place the agent and trusted devices in the **same dedicated VLAN**, isolated from other network devices.
- **Stay updated**: Continue to follow DeerFlow's security feature updates.

## Contributing

We welcome contributions! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, workflow, and guidelines.

Regression coverage includes Docker sandbox mode detection and provisioner kubeconfig-path handling tests in `backend/tests/`.
Backend blocking-IO diagnostics are available from the repository root with
`make detect-blocking-io`: it statically scans backend business code for
blocking IO that may run on the backend event loop, prints a concise summary,
and writes complete JSON findings to `.deer-flow/blocking-io-findings.json`.
The JSON includes compact review records with `priority`, `location`,
`blocking_call`, `event_loop_exposure`, `reason`, and `code`.
Gateway artifact serving now forces active web content types (`text/html`, `application/xhtml+xml`, `image/svg+xml`) to download as attachments instead of inline rendering, reducing XSS risk for generated artifacts.

## License

This project is open source and available under the [MIT License](./LICENSE).

## Acknowledgments

DeerFlow is built upon the incredible work of the open-source community. We are deeply grateful to all the projects and contributors whose efforts have made DeerFlow possible. Truly, we stand on the shoulders of giants.

We would like to extend our sincere appreciation to the following projects for their invaluable contributions:

- **[LangChain](https://github.com/langchain-ai/langchain)**: Their exceptional framework powers our LLM interactions and chains, enabling seamless integration and functionality.
- **[LangGraph](https://github.com/langchain-ai/langgraph)**: Their innovative approach to multi-agent orchestration has been instrumental in enabling DeerFlow's sophisticated workflows.

These projects exemplify the transformative power of open-source collaboration, and we are proud to build upon their foundations.

### Key Contributors

A heartfelt thank you goes out to the core authors of `DeerFlow`, whose vision, passion, and dedication have brought this project to life:

- **[Daniel Walnut](https://github.com/hetaoBackend/)**
- **[Henry Li](https://github.com/magiccube/)**

Your unwavering commitment and expertise have been the driving force behind DeerFlow's success. We are honored to have you at the helm of this journey.

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=bytedance/deer-flow&type=Date)](https://star-history.com/#bytedance/deer-flow&Date)
