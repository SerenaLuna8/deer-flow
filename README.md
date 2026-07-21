# 🦌 DeerFlow - 2.0

[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](./backend/pyproject.toml)
[![Node.js](https://img.shields.io/badge/Node.js-22%2B-339933?logo=node.js&logoColor=white)](./Makefile)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

<a href="https://trendshift.io/repositories/14699" target="_blank"><img src="https://trendshift.io/api/badge/repositories/14699" alt="bytedance%2Fdeer-flow | Trendshift" style="width: 250px; height: 55px;" width="250" height="55"/></a>
> 2026 年 2 月 28 日，DeerFlow 2 发布后登上 GitHub Trending 第 1 名。非常感谢社区的支持，这是大家一起做到的。

DeerFlow（**D**eep **E**xploration and **E**fficient **R**esearch **Flow**）是一个开源的 **super agent harness**。它把 **sub-agents**、**memory** 和 **sandbox** 组织在一起，再配合可扩展的 **skills**，让 agent 可以完成几乎任何事情。

https://github.com/user-attachments/assets/a8bcadc4-e040-4cf2-8fda-dd768b999c18

> [!NOTE]
> **DeerFlow 2.0 是一次彻底重写。** 它和 v1 没有共用代码。如果你要找的是最初的 Deep Research 框架，可以前往 [`1.x` 分支](https://github.com/bytedance/deer-flow/tree/main-1.x)。那里仍然欢迎贡献；当前的主要开发已经转向 2.0。

## 官网

想了解更多，或者直接看**真实演示**，可以访问[**官网**](https://deerflow.tech)。

## 字节跳动火山引擎方舟 Coding Plan

- 我们推荐使用 Doubao-Seed-2.0-Code、DeepSeek v3.2 和 Kimi 2.5 运行 DeerFlow
- [现在就加入 Coding Plan](https://www.volcengine.com/activity/codingplan?utm_campaign=deer_flow&utm_content=deer_flow&utm_medium=devrel&utm_source=OWO&utm_term=deer_flow)
- [海外地区的开发者请点击这里](https://www.byteplus.com/en/activity/codingplan?utm_campaign=deer_flow&utm_content=deer_flow&utm_medium=devrel&utm_source=OWO&utm_term=deer_flow)

## InfoQuest

DeerFlow 新近集成了 BytePlus 自研的智能搜索与抓取工具集——[InfoQuest（支持免费在线体验）](https://docs.byteplus.com/en/docs/InfoQuest/What_is_Info_Quest)

<a href="https://docs.byteplus.com/en/docs/InfoQuest/What_is_Info_Quest" target="_blank">
  <img
    src="https://sf16-sg.tiktokcdn.com/obj/eden-sg/hubseh7bsbps/20251208-160108.png"   alt="InfoQuest_banner"
  />
</a>

## 目录

- [🦌 DeerFlow - 2.0](#-deerflow---20)
  - [官网](#官网)
  - [字节跳动火山引擎方舟 Coding Plan](#字节跳动火山引擎方舟-coding-plan)
  - [InfoQuest](#infoquest)
  - [目录](#目录)
  - [一句话交给 Coding Agent 安装](#一句话交给-coding-agent-安装)
  - [快速开始](#快速开始)
    - [配置](#配置)
    - [运行应用](#运行应用)
      - [部署建议与资源规划](#部署建议与资源规划)
      - [方式一：Docker（推荐）](#方式一docker推荐)
      - [方式二：本地开发](#方式二本地开发)
    - [进阶配置](#进阶配置)
      - [Sandbox 模式](#sandbox-模式)
      - [MCP Server](#mcp-server)
      - [IM 渠道](#im-渠道)
      - [LangSmith 链路追踪](#langsmith-链路追踪)
  - [从 Deep Research 到 Super Agent Harness](#从-deep-research-到-super-agent-harness)
  - [核心特性](#核心特性)
    - [Skills 与 Tools](#skills-与-tools)
    - [Session Goals](#session-goals)
    - [手动上下文压缩](#手动上下文压缩)
    - [Sub-Agents](#sub-agents)
    - [Sandbox 与文件系统](#sandbox-与文件系统)
    - [Context Engineering](#context-engineering)
    - [长期记忆](#长期记忆)
  - [推荐模型](#推荐模型)
  - [定时任务 (Scheduled Tasks)](#定时任务-scheduled-tasks)
  - [文档](#文档)
  - [⚠️ 安全使用](#️-安全使用)
  - [参与贡献](#参与贡献)
  - [许可证](#许可证)
  - [致谢](#致谢)
    - [核心贡献者](#核心贡献者)
  - [Star History](#star-history)

## 一句话交给 Coding Agent 安装

如果你在用 Claude Code、Codex、Cursor、Windsurf 或其他 coding agent，可以直接把下面这句话发给它：

```text
如果还没 clone DeerFlow，就先 clone，然后按照 https://raw.githubusercontent.com/bytedance/deer-flow/main/Install.md 把它的本地开发环境初始化好
```

这条提示词是给 coding agent 用的。它会在需要时先 clone 仓库，优先选择 Docker，完成初始化，并在结束时告诉你下一条启动命令，以及还缺哪些配置需要你补充。

## 快速开始

### 配置

1. **克隆 DeerFlow 仓库**

   ```bash
   git clone https://github.com/bytedance/deer-flow.git
   cd deer-flow
   ```

2. **运行安装向导（推荐）**

   在项目根目录（`deer-flow/`）执行：

   ```bash
   make setup
   ```

   这会启动一个交互式向导，引导你选择 LLM provider、可选的 web 搜索工具，以及 sandbox 模式、bash 权限、文件写入等执行/安全偏好。它会生成一份最小化的 `config.yaml`，并把 API key 写入 `.env`，大约 2 分钟完成。

   随时可以运行 `make doctor` 检查配置和系统环境，并获得可执行的修复建议。

   DeerFlow 运行期只使用 PostgreSQL。首次初始化数据库时，需要分别显式设置
   `POSTGRES_ADMIN_URL`（连接 `postgres` maintenance database，仅用于建库）和
   `DATABASE_URL`（目标数据库连接），然后执行：

   ```bash
   make setup-db
   make check-db
   make start
   ```

   `setup-db` 不会创建或修改 role；目标 role 必须已存在。它先初始化 ORM/Alembic，再用同一个
   显式 `DATABASE_URL` 幂等初始化 LangGraph checkpointer/store 完整 schema。`make check-db` 只读检查 PostgreSQL 版本、
   Alembic revision、业务表（包括 `projects`、`project_memberships`）以及
   checkpoint/store 必需表；只到 Alembic head 但缺少其中任一表仍会判定为不健康。
   命令输出不会显示 username、password 或完整 URL。
   Gateway、Worker 和 Scheduler 启动只验证已配置的目标数据库，不会自动建库或修复 schema。

   当前 DeerFlow compose stack 不会自动启动 PostgreSQL。本地可单独启动容器，以下值均为
   占位符，密码中的特殊字符写入 URL 前必须编码：

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

   ### PostgreSQL final baseline（M7）

   DeerFlow 只支持全新 PostgreSQL 数据库。受支持的安装顺序是：创建空数据库 →
   `make setup-db` → `make start`。`make setup-db` 在空库安装唯一 revision
   `0001_project_saas_baseline`，再初始化 builtin system asset catalog、LangGraph schema 和
   default project；`make check-db` 只读验证 revision 与必需表。

   旧 M1–M6 revision、无 Alembic 标记的非空 schema 或含未知 relation 的数据库都会在任何 DDL 前
   返回 `M7_RECREATE_REQUIRED`。系统不会原地迁移、删除或改写旧数据；请创建新的空数据库。
   baseline downgrade 永远拒绝，旧 SQLite、shared asset、private-work、Automation、reliability
   迁移命令已删除。

   ```bash
   export POSTGRES_ADMIN_URL="postgresql+asyncpg://postgres:<encoded-password>@127.0.0.1:5432/postgres"
   export DATABASE_URL="postgresql+asyncpg://deerflow:<encoded-password>@127.0.0.1:5432/deerflow"
   make setup-db
   make check-db
   make start
   ```

   如果你要提交本地安装、配置或运行问题，可以执行 `make support-bundle`。
   命令会直接打印 reporter 下一步建议，并在 `.deer-flow/support-bundles/` 下生成
   `*-issue-summary.md`、面向 AI 辅助提 issue 的 `*-issue-draft.md`，以及可选证据
   zip。提交 GitHub issue 时，先把 `*-issue-summary.md` 粘贴到 issue 正文；如果由
   AI 助手代填 issue，就从 `*-issue-draft.md` 开始，并先替换所有 REQUIRED 占位符，
   不要编造未知事实。只有维护者要求证据包，或摘要不足以诊断时，再附上 zip。维护者
   或 AI 辅助 triage 可以优先读取 `triage.json`；bundle 只包含脱敏后的诊断信息和
   文件 manifest，不包含 `.env`、原始对话消息或用户文件内容；提交前仍建议自己快速
   检查一遍。

   > **进阶 / 手动配置**：如果你更想直接编辑 `config.yaml`，可以改用 `make config` 复制完整的示例模板。完整参考见 `config.example.yaml`，其中包含 CLI-backed provider（Codex CLI、Claude Code OAuth）、OpenRouter、Responses API 等更多配置。

   <details>
   <summary>手动模型配置示例</summary>

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

   OpenRouter 以及类似的 OpenAI 兼容网关，建议通过 `langchain_openai:ChatOpenAI` 配合 `base_url` 来配置。如果你更想用 provider 自己的环境变量名，也可以直接把 `api_key` 指向对应变量，例如 `api_key: $OPENROUTER_API_KEY`。

   如果要让 OpenAI 模型走 `/v1/responses`，继续使用 `langchain_openai:ChatOpenAI`，并设置 `use_responses_api: true` 和 `output_version: responses/v1`。

   对于 vLLM 0.19.0，请使用 `deerflow.models.vllm_provider:VllmChatModel`。对于 Qwen 风格的推理模型，DeerFlow 通过 `extra_body.chat_template_kwargs.enable_thinking` 开关推理，并在多轮 tool-call 对话中保留 vLLM 非标准的 `reasoning` 字段。旧版 `thinking` 配置会自动规范化以保持向后兼容。推理模型可能还需要在启动 vLLM 服务时加上 `--reasoning-parser ...` 参数。如果你的本地 vLLM 部署接受任意非空 API key，可以把 `VLLM_API_KEY` 设为一个占位值。

   CLI-backed provider 配置示例：

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

   - Codex CLI 会读取 `~/.codex/auth.json`
   - Claude Code 支持 `CLAUDE_CODE_OAUTH_TOKEN`、`ANTHROPIC_AUTH_TOKEN`、`CLAUDE_CODE_CREDENTIALS_PATH`，或 `~/.claude/.credentials.json`
   - ACP agent 条目与 model provider 是分开配置的——如果你配置了 `acp_agents.codex`，请把它指向一个 Codex ACP 适配器，例如 `npx -y @zed-industries/codex-acp`
   - 在 macOS 上，如有需要可显式导出 Claude Code 的认证信息：

   ```bash
   eval "$(python3 scripts/export_claude_code_oauth.py --print-export)"
   ```

   API key 也可以手动写入 `.env` 文件（推荐）或在 shell 中导出：

   ```bash
   OPENAI_API_KEY=your-openai-api-key
   TAVILY_API_KEY=your-tavily-api-key
   ```

   </details>

### 运行应用

#### 部署建议与资源规划

可以先按下面的资源档位来选择 DeerFlow 的运行方式：

| 部署场景 | 起步配置 | 推荐配置 | 说明 |
|---------|-----------|------------|-------|
| 本地体验 / `make dev` | 4 vCPU、8 GB 内存、20 GB SSD 可用空间 | 8 vCPU、16 GB 内存 | 适合单个开发者或单个轻量会话，且模型走外部 API。`2 核 / 4 GB` 通常跑不稳。 |
| Docker 开发 / `make docker-start` | 4 vCPU、8 GB 内存、25 GB SSD 可用空间 | 8 vCPU、16 GB 内存 | 镜像构建、源码挂载和 sandbox 容器都会比纯本地模式更吃资源。 |
| 长期运行服务 / `make up` | 8 vCPU、16 GB 内存、40 GB SSD 可用空间 | 16 vCPU、32 GB 内存 | 更适合共享环境、多 agent 任务、报告生成或更重的 sandbox 负载。 |

- 上面的配置只覆盖 DeerFlow 本身；如果你还要本机部署本地大模型，请单独为模型服务预留资源。
- 持续运行的服务更推荐使用 Linux + Docker。macOS 和 Windows 更适合作为开发机或体验环境。
- 如果 CPU 或内存长期打满，先降低并发会话或重任务数量，再考虑升级到更高一档配置。

#### 方式一：Docker（推荐）

**开发模式**（支持热更新，挂载源码）：

```bash
make docker-init    # 拉取 sandbox 镜像（首次运行或镜像更新时执行）
make docker-start   # 启动服务（会根据 config.yaml 自动判断 sandbox 模式）
```

如果 `config.yaml` 使用的是 provisioner 模式（`sandbox.use: deerflow.community.aio_sandbox:AioSandboxProvider` 且配置了 `provisioner_url`），`make docker-start` 才会启动 `provisioner`。

**生产模式**（本地构建镜像，并挂载运行期配置与数据）：

```bash
make up     # 构建镜像并启动全部生产服务
make down   # 停止并移除容器
```

> [!NOTE]
> Gateway、Worker 和可选 Scheduler 是独立进程；nginx 将 `/api/*` 直接转发给 Gateway，Agent graph 只由 Worker 执行。

访问地址：http://localhost:2026

更完整的 Docker 开发说明见 [CONTRIBUTING.md](CONTRIBUTING.md)。

#### 方式二：本地开发

如果你更希望直接在本地启动各个服务：

前提：先完成上面的“配置”步骤（`make setup`）。`make dev` 默认只读取 canonical 仓库根目录下的 `config.yaml`；如需使用其他文件，只能通过 `DEER_FLOW_CONFIG_PATH` 显式指定。运行期状态默认写到 `.deer-flow`，可用 `DEER_FLOW_HOME` 覆盖。启动前先运行 `make doctor` 校验配置、final schema readiness 和服务前置条件。
在 Windows 上，请使用 Git Bash 运行本地开发流程。基于 bash 的服务脚本不支持直接在原生 `cmd.exe` 或 PowerShell 中执行，且 WSL 也不保证可用，因为部分脚本依赖 Git for Windows 的 `cygpath` 等工具。

1. **检查依赖环境**：
   ```bash
   make check  # 校验 Node.js 22+、pnpm、uv、nginx
   ```

2. **安装依赖**：
   ```bash
   make install  # 安装 backend + frontend 依赖
   ```

3. **（可选）预拉取 sandbox 镜像**：
   ```bash
   # 如果使用 Docker / Container sandbox，建议先执行
   make setup-sandbox
   ```

4. **启动服务**：
   ```bash
   make dev
   ```

5. **访问地址**：http://localhost:2026

### 进阶配置
#### Sandbox 模式

DeerFlow 支持多种 sandbox 执行方式：
- **本地执行**（直接在宿主机上运行 sandbox 代码）
- **Docker 执行**（在隔离的 Docker 容器里运行 sandbox 代码）
- **Docker + Kubernetes 执行**（通过 provisioner 服务在 Kubernetes Pod 中运行 sandbox 代码）

Docker 开发时，服务启动行为会遵循 `config.yaml` 里的 sandbox 模式。在 Local / Docker 模式下，不会启动 `provisioner`。

如果要配置你自己的模式，参见 [Sandbox 配置指南](backend/docs/CONFIGURATION.md#sandbox)。

#### MCP Server

System admin 通过 `/admin/assets/mcp` 发布系统 MCP；项目成员在
`/projects/{project_slug}/mcp` 管理项目 MCP 和固定的系统 binding。定义与不可变版本存入
PostgreSQL；Gateway admission 为每个 Run 固定 exact MCP/Credential-grant snapshot，Worker 只
materialize 该 snapshot。HTTP/SSE OAuth 和 stdio transport 继续支持，但 secret 只存入加密的
Credential envelope，不进入 MCP 定义或浏览器 cache。详细说明见
[MCP Server 指南](backend/docs/MCP_SERVER.md)。

#### IM 渠道

DeerFlow 支持从即时通讯应用接收任务。只要配置完成，对应渠道会自动启动，而且都不需要公网 IP。

DeerFlow 支持 project-bound IM 渠道连接，并复用配置的 `channels.*` 出站传输，因此不需要公网 IP 或 provider 回调地址。Connections 和 provider availability 使用 `/api/projects/{project_id}/connections*`；绑定后的文本消息只在 PostgreSQL 中精确解析出的 account、project、owner、Agent 和 connection 作用域运行。设置和运维说明参见 [IM Channel Connections](backend/docs/IM_CHANNEL_CONNECTIONS.md)。

| 渠道 | 传输方式 | 上手难度 |
|---------|-----------|------------|
| Telegram | Bot API（long-polling） | 简单 |
| Slack | Socket Mode | 中等 |
| Feishu / Lark | WebSocket | 中等 |
| WeChat | Tencent iLink（long-polling） | 中等 |
| 企业微信智能机器人 | WebSocket | 中等 |
| 钉钉 | Stream Push（WebSocket） | 中等 |

**`config.yaml` 中的配置示例：**

```yaml
channels:
  # 辅助 Gateway command base URL（默认：http://localhost:8001/api）
  langgraph_url: http://localhost:8001/api
  # Gateway API URL（默认：http://localhost:8001）
  gateway_url: http://localhost:8001

  feishu:
    enabled: true
    app_id: $FEISHU_APP_ID
    app_secret: $FEISHU_APP_SECRET
    # domain: https://open.feishu.cn       # 国内版（默认）
    # domain: https://open.larksuite.com   # 国际版

  wecom:
    enabled: true
    bot_id: $WECOM_BOT_ID
    bot_secret: $WECOM_BOT_SECRET

  slack:
    enabled: true
    bot_token: $SLACK_BOT_TOKEN     # xoxb-...
    app_token: $SLACK_APP_TOKEN     # xapp-...（Socket Mode）
    allowed_users: []               # 留空表示允许所有人

  telegram:
    enabled: true
    bot_token: $TELEGRAM_BOT_TOKEN
    allowed_users: []               # 留空表示允许所有人

  wechat:
    enabled: false
    bot_token: $WECHAT_BOT_TOKEN
    ilink_bot_id: $WECHAT_ILINK_BOT_ID
    qrcode_login_enabled: true      # 可选：bot_token 缺失时允许首次扫码登录引导
    allowed_users: []               # 留空表示允许所有人
    polling_timeout: 35
    state_dir: ./.deer-flow/wechat/state
    max_inbound_image_bytes: 20971520
    max_outbound_image_bytes: 20971520
    max_inbound_file_bytes: 52428800
    max_outbound_file_bytes: 52428800

  dingtalk:
    enabled: true
    client_id: $DINGTALK_CLIENT_ID             # 钉钉开放平台 ClientId
    client_secret: $DINGTALK_CLIENT_SECRET     # 钉钉开放平台 ClientSecret
    allowed_users: []                          # 留空表示允许所有人
    card_template_id: ""                       # 可选：AI 卡片模板 ID，用于流式打字机效果
```

Project-bound IM worker 会从 PostgreSQL connection row 恢复固定 Agent version，再通过 Gateway
内部 admission service 创建 exact project-private Run。消息字段和 provider-wide 配置都不能作为
project、owner、membership 或 Agent authority。

在 `.env` 里设置对应的 API key：

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

# 企业微信智能机器人
WECOM_BOT_ID=your_bot_id
WECOM_BOT_SECRET=your_bot_secret

# 钉钉
DINGTALK_CLIENT_ID=your_client_id
DINGTALK_CLIENT_SECRET=your_client_secret
```

**Telegram 配置**

1. 打开 [@BotFather](https://t.me/BotFather)，发送 `/newbot`，复制生成的 HTTP API token。
2. 在 `.env` 中设置 `TELEGRAM_BOT_TOKEN`，并在 `config.yaml` 里启用该渠道。

**Slack 配置**

1. 前往 [api.slack.com/apps](https://api.slack.com/apps) 创建 Slack App：Create New App → From scratch。
2. 在 **OAuth & Permissions** 中添加 Bot Token Scopes：`app_mentions:read`、`chat:write`、`im:history`、`im:read`、`im:write`、`files:write`。
3. 启用 **Socket Mode**，生成带 `connections:write` 权限的 App-Level Token（`xapp-...`）。
4. 在 **Event Subscriptions** 中订阅 bot events：`app_mention`、`message.im`。
5. 在 `.env` 中设置 `SLACK_BOT_TOKEN` 和 `SLACK_APP_TOKEN`，并在 `config.yaml` 中启用该渠道。

**Feishu / Lark 配置**

1. 在 [飞书开放平台](https://open.feishu.cn/) 创建应用，并启用 **Bot** 能力。
2. 添加权限：`im:message`、`im:message.p2p_msg:readonly`、`im:resource`。
3. 在 **事件订阅** 中订阅 `im.message.receive_v1`，连接方式选择 **长连接**。
4. 复制 App ID 和 App Secret，在 `.env` 中设置 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET`，并在 `config.yaml` 中启用该渠道。

**WeChat 配置**

1. 在 `config.yaml` 中启用 `wechat` 渠道。
2. 在 `.env` 中设置 `WECHAT_BOT_TOKEN`，或者把 `qrcode_login_enabled` 设为 `true` 以便首次扫码登录引导。
3. 当 `bot_token` 缺失且启用了扫码引导时，留意后端日志里 iLink 返回的二维码内容，并完成绑定流程。
4. 扫码流程成功后，DeerFlow 会把获取到的 token 持久化到 `state_dir`，便于后续重启复用。
5. Docker Compose 部署时，请把 `state_dir` 放在持久化卷上，这样 `get_updates_buf` 游标和已保存的登录状态才能在重启后保留。

**企业微信智能机器人配置**

1. 在企业微信智能机器人平台创建机器人，获取 `bot_id` 和 `bot_secret`。
2. 在 `config.yaml` 中启用 `channels.wecom`，并填入 `bot_id` / `bot_secret`。
3. 在 `.env` 中设置 `WECOM_BOT_ID` 和 `WECOM_BOT_SECRET`。
4. 安装后端依赖时确保包含 `wecom-aibot-python-sdk`，渠道会通过 WebSocket 长连接接收消息，无需公网回调地址。
5. 当前支持文本、图片和文件入站消息；agent 生成的最终图片/文件也会回传到企业微信会话中。

**钉钉配置**

1. 在 [钉钉开放平台](https://open.dingtalk.com/) 创建应用，并启用 **机器人** 能力。
2. 在机器人配置页面设置消息接收模式为 **Stream模式**。
3. 复制 `Client ID` 和 `Client Secret`，在 `.env` 中设置 `DINGTALK_CLIENT_ID` 和 `DINGTALK_CLIENT_SECRET`，并在 `config.yaml` 中启用该渠道。
4. *（可选）* 如需开启流式 AI 卡片回复（打字机效果），请在[钉钉卡片平台](https://open.dingtalk.com/document/dingstart/typewriter-effect-streaming-ai-card)创建 **AI 卡片**模板，然后在 `config.yaml` 中将 `card_template_id` 设为该模板 ID。同时需要申请 `Card.Streaming.Write` 和 `Card.Instance.Write` 权限。

**命令**

渠道连接完成后，你可以直接在聊天窗口里和 DeerFlow 交互：

| 命令 | 说明 |
|---------|-------------|
| `/models` | 列出可用模型 |
| `/help` | 查看帮助 |
| `/<skill-name> <task>` | 在项目作用域内激活一个技能执行本轮任务 |

> 没有命令前缀的消息会被当作普通项目聊天处理；不支持的命令不会作为普通 prompt 提交。

#### LangSmith 链路追踪

DeerFlow 内置了 [LangSmith](https://smith.langchain.com) 集成，用于可观测性。启用后，所有 LLM 调用、agent 运行和工具执行都会被追踪，并在 LangSmith 仪表盘中展示。

在 `.env` 文件中添加以下配置：

```bash
LANGSMITH_TRACING=true
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
LANGSMITH_API_KEY=lsv2_pt_xxxxxxxxxxxxxxxx
LANGSMITH_PROJECT=xxx
```

Docker 部署时，追踪默认关闭。在 `.env` 中设置 `LANGSMITH_TRACING=true` 和 `LANGSMITH_API_KEY` 即可启用。

## 从 Deep Research 到 Super Agent Harness

DeerFlow 最初是一个 Deep Research 框架，后来社区把它一路推到了更远的地方。上线之后，开发者拿它去做的事情早就不止研究：搭数据流水线、生成演示文稿、快速起 dashboard、自动化内容流程，很多方向一开始连我们自己都没想到。

这让我们意识到一件事：DeerFlow 不只是一个研究工具。它更像一个 **harness**，一个真正让 agents 把事情做完的运行时基础设施。

所以我们把它从头重做了一遍。

DeerFlow 2.0 不再是一个需要你自己拼装的 framework。它是一个开箱即用、同时又足够可扩展的 super agent harness。基于 LangGraph 和 LangChain 构建，默认就带上了 agent 真正会用到的关键能力：文件系统、memory、skills、sandbox 执行环境，以及为复杂多步骤任务做规划、拉起 sub-agents 的能力。

你可以直接拿来用，也可以拆开重组，改成你自己的样子。

## 核心特性

### Skills 与 Tools

Skill 是不可变、带版本的 project 或 system asset。System admin 在
`/admin/assets/skills` 发布系统 Skill；项目成员在 `/projects/{project_slug}/skills` 创建项目 Skill
并固定启用的系统版本。创建和发布会先运行 deterministic SkillScan 与 contextual scanner，再把
version 写入 PostgreSQL。

Gateway admission 会为每个 Run 固定 exact Agent、Skill、MCP version。Worker 只把该 admitted
snapshot 中的 Skill bytes materialize 到 run-owned read-only `/mnt/skills`；运行时服务不会发现或修改
ambient Skill 目录。`/data-analysis ...` 这类 slash activation 也只能选择 exact snapshot 中启用的 Skill。

最终 toolset 由内置 sandbox tools 和 admitted snapshot 中固定的 MCP tools 组成。Credential 只从同
scope 已批准的 grant 解析，并在 dispatch 时注入，不进入 prompt、API payload、browser cache、checkpoint
或日志。

### Session Goals

用 `/goal <完成条件>` 为当前 thread 绑定一个激活态的完成条件。这个 goal 是 thread 维度的状态，而不是技能激活，所以它会跨轮次持续生效，直到 DeerFlow 判定它已被满足、或者你手动清除它。

支持的命令：

```text
/goal finish the implementation and make all tests pass
/goal              # 查看当前激活的 goal
/goal clear        # 清除它
```

每次 Gateway 驱动的 run 结束后，DeerFlow 会用一个 non-thinking 的评估模型，把可见的对话内容拿去和激活的 goal 比对。评估模型必须返回一个带类型的 blocker（`missing_evidence`、`needs_user_input`、`run_failed`、`external_wait` 或 `goal_not_met_yet`），并附上可见证据。只有在最近一轮 assistant 回复已被持久化 checkpoint、blocker 是 `goal_not_met_yet`、评估期间 thread 没有变化、且无进展熔断器没有触发时，DeerFlow 才会注入一次 hidden continuation。安全上限默认是 8 次 hidden continuation；连续两次相同的无进展评估后就会停止。`/goal clear` 以及任何用户手动输入的新内容，优先级都高于排队中的 continuation。当 goal 被满足时，DeerFlow 会自动清除它，并发布更新后的 thread 状态。

Web UI 会在输入框上方展示当前激活的 goal，TUI 也支持同一命令。IM 渠道不再提供 `/goal`；项目绑定的 IM run 使用普通消息或已启用的 slash skill。

### 手动上下文压缩

在 Web UI 输入框中使用 `/compact`，可以把当前 thread 的早期上下文压缩成摘要。完整聊天记录仍会保留在界面上，但后续模型调用会基于压缩摘要和最近消息继续。当前历史不足时不会压缩；thread 正在运行任务时会阻止压缩。

### Sub-Agents

复杂任务通常不可能一次完成，DeerFlow 会先拆解，再执行。

lead agent 可以按需动态拉起 sub-agents。每个 sub-agent 都有自己独立的上下文、工具和终止条件。只要条件允许，它们就会并行运行，返回结构化结果，最后再由 lead agent 汇总成一份完整输出。

这也是 DeerFlow 能处理从几分钟到几小时任务的原因。比如一个研究任务，可以拆成十几个 sub-agents，分别探索不同方向，最后合并成一份报告，或者一个网站，或者一套带生成视觉内容的演示文稿。一个 harness，多路并行。

### Sandbox 与文件系统

DeerFlow 不只是“会说它能做”，它是真的有一台自己的“电脑”。

每个任务都运行在隔离的 Docker 容器里，里面有完整的文件系统，包括 skills、workspace、uploads、outputs。agent 可以读写和编辑文件，可以执行 bash 命令和代码，也可以查看图片。整个过程都在 sandbox 内完成，可审计、会隔离，不会在不同 session 之间互相污染。

这就是“带工具的聊天机器人”和“真正有执行环境的 agent”之间的差别。

```text
# sandbox 容器内的路径
/mnt/user-data/
├── uploads/          ← 你的文件
├── workspace/        ← agents 的工作目录
└── outputs/          ← 最终交付物
```

### Context Engineering

**隔离的 Sub-Agent Context**：每个 sub-agent 都在自己独立的上下文里运行。它看不到主 agent 的上下文，也看不到其他 sub-agents 的上下文。这样做的目的很直接，就是让它只聚焦当前任务，不被无关信息干扰。

**摘要压缩**：在单个 session 内，DeerFlow 会比较积极地管理上下文，包括总结已完成的子任务、把中间结果转存到文件系统、压缩暂时不重要的信息。这样在长链路、多步骤任务里，它也能保持聚焦，而不会轻易把上下文窗口打爆。

### 长期记忆

大多数 agents 会在对话结束后把一切都忘掉，DeerFlow 不一样。

跨 session 使用时，DeerFlow 会在当前项目内逐步积累持久 memory，包括你的个人偏好、知识背景，以及长期沉淀下来的工作习惯。项目 Memory 保存在 PostgreSQL，并按认证账号、项目和 owner 强制隔离；你用得越多，它越了解你的写作风格、技术栈和重复出现的工作流。

## 推荐模型

DeerFlow 对模型没有强绑定，只要实现了 OpenAI 兼容 API 的 LLM，理论上都可以接入。不过在下面这些能力上表现更强的模型，通常会更适合 DeerFlow：

- **长上下文窗口**（100k+ tokens），适合深度研究和多步骤任务
- **推理能力**，适合自适应规划和复杂拆解
- **多模态输入**，适合理解图片和视频
- **稳定的 tool use 能力**，适合可靠的函数调用和结构化输出

## 定时任务 (Scheduled Tasks)

M5 在 `/projects/{project_slug}/automations` 提供项目 Automation。
每个 definition 和 occurrence 都按认证账号与当前项目双重隔离。具备服务端下发能力的
Admin、Editor 和 Runner 可以创建、编辑、暂停、恢复、手动触发、查看和删除自己的
Automation；Viewer 只能查看自己的 definition 和运行历史。

项目 Automation 支持 `once` 和五字段 `cron`，固定使用 `skip` overlap policy，并可选择
复用私有 thread 或每次创建新的私有 thread。自动和手动触发都会先持久化唯一 occurrence，
再进入正常的项目私有 run admission。run 一旦 admitted，进程崩溃后只会记录并协调终态，
不会自动重放可能已经发生的副作用。

当前限制：

- 暂时还没有可在对话中创建任务的 `schedule_task` 工具
- 没有纯文本通知任务
- 没有渠道或 GitHub 分发目标
- 第一版没有 `interval` 调度类型

通过 `config.yaml -> scheduler.enabled` 开启后台轮询。M6 由独立 Scheduler 持有并持续验证
PostgreSQL session ownership lock；Gateway 不再持有 poller。关闭轮询不影响项目 API 和手动触发，
手动触发仍使用同一 occurrence/Run/job 原子 admission。独立 Worker、持久化 SSE、通用 jobs/retries、
配额、审计和通用备份恢复已在 M6 完成。

项目 API 位于 `/api/projects/{project_id}/automations`。M1–M8 已全部完成，总体进度为
8/8（100%）。DeerFlow 项目优先、多用户 SaaS V1 已通过限定的宿主机部署发布验收：全新
PostgreSQL 数据库、`make setup-db`、`make start`、桌面版 Chromium 和 DeepSeek
`deepseek-v4-pro`。Docker Compose、Kubernetes/Helm、Firefox、Safari/WebKit 和其他模型供应商
未经过 M8 生产认证；本次里程碑关闭也没有创建 tag、推送远端或发布制品。

## 文档

- [贡献指南](CONTRIBUTING.md) - 开发环境搭建与协作流程
- [配置指南](backend/docs/CONFIGURATION.md) - 安装与配置说明
- [架构概览](backend/CLAUDE.md) - 技术架构说明
- [后端架构](backend/README.md) - 后端架构与 API 参考

## ⚠️ 安全使用

### 不恰当的部署可能导致安全风险

DeerFlow 具备**系统指令执行、资源操作、业务逻辑调用**等关键高权限能力，默认设计为**部署在本地可信环境（仅本机 127.0.0.1 回环访问）**。若您将 agent 部署至不可信局域网、公网云服务器等可被多终端访问的网络环境，且未采取严格的安全防护措施，可能导致安全风险，例如：

- **未授权的非法调用**：agent 功能被未授权的第三方、公网恶意扫描程序探测到，进而发起批量非法调用请求，执行系统命令、文件读写等高危操作，可能导致安全后果。
- **合规与法律风险**：若 agent 被非法调用用于实施网络攻击、信息窃取等违法违规行为，可能产生法律责任与合规风险。

### 安全使用建议

**注意：建议您将 DeerFlow 部署在本地可信的网络环境下。** 若您有跨设备、跨网络的部署需求，必须加入严格的安全措施。例如，采取如下手段：

- **设置访问 IP 白名单**：使用 `iptables`，或部署硬件防火墙 / 带访问控制（ACL）功能的交换机等，**配置规则设置 IP 白名单**，拒绝其他所有 IP 进行访问。
- **前置身份验证**：配置反向代理（nginx 等），并**开启高强度的前置身份验证功能**，禁止无任何身份验证的访问。
- **网络隔离**：若有可能，建议将 agent 和可信设备划分到**同一个专用 VLAN**，与其他网络设备做隔离。
- **持续关注项目更新**：请持续关注 DeerFlow 项目的安全功能更新。

## 参与贡献

欢迎参与贡献。开发环境、工作流和相关规范见 [CONTRIBUTING.md](CONTRIBUTING.md)。

目前回归测试已经覆盖 Docker sandbox 模式识别，以及 `backend/tests/` 中 provisioner kubeconfig-path 处理相关测试。

## 许可证

本项目采用 [MIT License](./LICENSE) 开源发布。

## 致谢

DeerFlow 建立在开源社区大量优秀工作的基础上。所有让 DeerFlow 成为可能的项目和贡献者，我们都心怀感谢。毫不夸张地说，我们是站在巨人的肩膀上继续往前走。

特别感谢以下项目带来的关键支持：

- **[LangChain](https://github.com/langchain-ai/langchain)**：它们提供的优秀框架支撑了我们的 LLM 交互与 chains，让整体集成和能力编排顺畅可用。
- **[LangGraph](https://github.com/langchain-ai/langgraph)**：它们在多 agent 编排上的创新方式，是 DeerFlow 复杂工作流得以成立的重要基础。

这些项目体现了开源协作真正的力量，我们也很高兴能继续建立在这些基础之上。

### 核心贡献者

感谢 `DeerFlow` 的核心作者，是他们的判断、投入和持续推进，才让这个项目真正落地：

- **[Daniel Walnut](https://github.com/hetaoBackend/)**
- **[Henry Li](https://github.com/magiccube/)**

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=bytedance/deer-flow&type=Date)](https://star-history.com/#bytedance/deer-flow&Date)
