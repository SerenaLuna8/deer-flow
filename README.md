# DeerFlow

[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](./backend/pyproject.toml)
[![Node.js](https://img.shields.io/badge/Node.js-22%2B-339933?logo=node.js&logoColor=white)](./Makefile)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

DeerFlow 是一个面向多账户、多项目协作的开源 super agent 系统。它以 LangGraph Agent harness 为执行核心，提供项目级认证授权、Agent/Skill/MCP 资产、长期 Memory、Sub-Agent、Sandbox、Automation、IM Channel 和持久化流式会话。

当前实现是 project-first SaaS 架构：浏览器和外部渠道先进入 Gateway，Gateway 完成认证、项目授权和 Run 准入，Worker 独占 Agent graph 执行，Scheduler 只负责 Automation 到期准入。应用数据、运行状态和资产版本统一存储在 PostgreSQL。

项目 Memory 保存在 PostgreSQL，并始终受 account、project 与 owner 作用域约束。

> DeerFlow 2 是一次重写，与最初的 Deep Research 实现不共用代码。原始版本见上游 [`main-1.x`](https://github.com/bytedance/deer-flow/tree/main-1.x) 分支。

## 核心能力

- 项目工作区：账户、成员、角色、邀请、配额、审计和项目生命周期。
- 项目用量：具备用量权限的项目管理员可在概览查看全项目最近 24 个小时的 Token 消耗趋势。
- 系统通知：工作区顶部铃铛集中展示账号级通知和未读数量；已注册用户收到项目邀请后可直接在通知中接受，未注册邮箱仍使用一次性邀请链接。
- Agent 运行：持久化 Thread/Run、durable SSE、断线重连、取消、重试和 Worker lease。
- 会话管理：新会话固定使用系统 Main Agent，不再弹出 Agent 选择框；会话列表支持手动重命名，并仅在首轮成功完成后由 Worker 自动生成一次标题。
- 资产治理：System/Project Agent、Skill、MCP 和 Credential 的版本化发布、绑定与准入快照。
- Agent harness：Sub-Agent、Plan Mode、上下文压缩、长期 Memory、Guardrail、Tool Search 和循环检测。
- Sandbox：支持 Local、容器和 Provisioner/Kubernetes provider；具体隔离能力取决于所选 provider。
- 项目自动化：一次性或 Cron Automation，由独立 Scheduler 准入、Worker 执行。
- IM Channel：Feishu/Lark、Slack、Telegram、Discord、DingTalk、WeChat 和企业微信等项目绑定连接。

## 运行架构

| 组件 | 默认端口 | 职责 |
| --- | ---: | --- |
| Nginx | `2026` | 唯一对外入口，代理前端和 `/api/*` |
| Frontend | `3000` | Next.js Web UI |
| Gateway | `8001` | 认证、项目 API、Run 准入、查询和 SSE replay |
| Worker | 无公开端口 | 唯一 Agent graph 执行进程 |
| Scheduler | 无公开端口 | 可选 Automation 轮询与准入进程 |
| Provisioner | `8002` | 仅特定 Sandbox/集群模式需要 |

```text
Browser / IM
     │
     ▼
Nginx :2026 ─────► Frontend :3000
     │
     └───────────► Gateway :8001 ─────► PostgreSQL
                                      ▲          ▲
                                      │          │
                                  Worker     Scheduler
```

Gateway 不执行 Agent graph；Worker 不提供面向浏览器的业务 API。私有资源始终绑定 `account + project + owner`，授权由业务层和数据库模型共同保证，不使用 PostgreSQL RLS。

## 快速开始

### 1. 准备环境

- Python 3.12+
- Node.js 22+
- pnpm 10.26.2+
- `uv`
- PostgreSQL
- 本地全栈模式需要 Nginx

```bash
git clone https://github.com/SerenaLuna8/deer-flow.git
cd deer-flow
make check
make install
```

### 2. 生成配置

推荐使用交互式向导：

```bash
make setup
```

也可以从示例生成后手工编辑：

```bash
make config
```

运行配置位于仓库根目录 `config.yaml`，密钥通过根目录 `.env` 或进程环境变量提供；两者都不应提交。完整字段见 [`config.example.yaml`](./config.example.yaml) 和[后端配置说明](./backend/docs/CONFIGURATION.md)。

### 3. 初始化 PostgreSQL

`0001_project_saas_baseline` 是不可改写的冻结基线和单一当前 head。它已包含项目 Skill 整包硬删除与项目内大小写不敏感名字唯一、Agent 的 `AGENTS.md`、`SOUL.md`、`IDENTITY.md`、`USER.md` 四个逻辑文档，以及私有 Agent Builder 会话和幂等操作表。系统支持在全新 PostgreSQL 目标库中安装这份当前基线；运行时不会建库、执行 migration、stamp 或修复 schema。应用 role 需要预先存在，并建议使用非 superuser role。

```bash
# 全新数据库
export POSTGRES_ADMIN_URL='<连接 postgres maintenance database 的管理员 URL>'
export DATABASE_URL='<DeerFlow 目标库 URL>'
make setup-db
make check-db
```

`make setup-db` 从空库安装单一当前 baseline，并初始化系统资产 catalog、LangGraph schema 和默认项目；`make check-db` 只读校验 current/head revision 与必需对象。当前版本的已有数据库必须已经精确匹配 `0001_project_saas_baseline` catalog。`make migrate-db` 保留给未来从 `0002` 起追加的已提交 forward-only revision，只执行 pending migrations，不创建数据库，也不重复执行 catalog、LangGraph 或默认项目初始化。未知 revision、未纳管非空 schema 或 catalog drift 会拒绝自动处理。命令不会输出完整连接 URL 或密码。

### 4. 启动

本地开发模式：

```bash
make dev
```

本地优化构建模式：

```bash
make start
```

访问 <http://localhost:2026>。本地全栈默认把运行状态写入 `backend/.deer-flow`，日志写入 `logs/`；停止服务使用：

```bash
make stop
```

## Docker 与部署

Docker 开发环境支持源码挂载和热更新，但不会自动提供 PostgreSQL：

```bash
make docker-init
make docker-start
make docker-stop
```

本地构建 Compose 镜像可使用：

```bash
make up
make down
```

Kubernetes/Helm 资源位于 `deploy/helm/`。Docker Compose、Kubernetes 和不同 Sandbox provider 的生产使用都需要在目标环境单独完成容量、安全和故障恢复验收，不能仅凭本地启动成功视为生产认证。

## 产品入口

- `/workspace`：登录后的多项目工作区。
- `/projects/{project_slug}`：项目会话、Agent、Skill、MCP、Credential、Memory、Automation、成员和设置。
- `/admin`：仅 system admin 可访问的平台资产与运维页面。

工作区顶部提供账号级通知铃铛和未读角标。向已注册邮箱发出的项目邀请会产生站内通知，接收者可在通知中直接接受并加入项目；通知列表、已读状态和接受操作严格绑定当前服务端认证账号。未注册邮箱不预建站内通知，仍通过不含服务端明文 token 的一次性邀请链接完成注册和兑换。通知 API 不返回邀请 token、token hash 或其他账号的通知数据。

System Agent、Skill 和 MCP 在显式数据库 setup 过程中由受校验的 packaged catalog 写入 PostgreSQL。运行进程只读取数据库中的资产版本和 Run 准入时固定的 snapshot。仓库内 `skills/public/` 的 21 个完整 Skill 目录会在开发期生成带 digest 的 packaged system Skill archive，并在 `make setup-db` 时作为系统资产写入；`make migrate-db` 只执行 pending schema migrations，不重复 seed。Setup 不会为项目自动绑定这些 Skill，项目管理员可在“系统提供”列表中逐项启用或停用，也不应再把同一目录重复导入为项目 Skill。

项目 MCP 采用默认拒绝的执行策略：项目不能直接启动 `stdio` 子进程，只能使用平台运维在 `mcp_security.project_remote_allowed_endpoints` 中批准的精确 HTTPS `http`/`sse` 地址。任意环境变量、静态请求头和 OAuth 配置不会作为普通版本字段保存，认证值必须通过加密 Credential 的 header slot 提供。Worker 对工具发现和每次调用重新校验快照与端点、禁用重定向和环境代理，并执行平台级硬超时；生产环境还应配置 `mcp_security.egress_proxy_url`，由受控出口独立阻断私网、链路本地和云元数据地址。历史不兼容版本仍可审计读取，但 Project API 只返回远程 HTTPS origin，不回放可能携带凭据的路径或查询参数，也不能用于新的 Agent Run。

项目 Agent 页面使用项目自建 Agent 卡片：卡片主体进入详情，已配置、启用且具备执行权限的 Agent 可从卡片直接创建绑定到该 Agent 的私有对话。新建入口会先创建一个仅绑定当前项目与当前账号的可恢复设计会话，再通过对话和澄清让模型生成 `AGENTS.md`、`SOUL.md`、`IDENTITY.md` 与 `USER.md` 四项候选设定；模型不能直接写库、发布或启用资产。用户预览、修改并最终确认后，后端才在一个事务里创建默认停用的 Agent、写入首份完整内部配置并结束设计会话；确认响应只返回设计会话和 Agent，不暴露内部 revision。同一项目的 Agent slug 不可重复，中断的生成可重试，设计消息和候选稿随精确项目/账号隐私范围清理。详情顶部只显示名称与最近更新时间，不提供 Agent 归档；卡片与详情为具备权限的停用态 Agent 提供启用动作，启用态详情提供停用动作。列表不提供删除入口；项目自建 Agent 仅在详情页经过二次确认和 5 秒等待后才可永久删除整个 Agent 及全部设置。已有对话、自动化或 Run snapshot 引用时返回 `409`，不会级联删除私有历史；系统 Agent 永远不可删除。四个名称只是映射到 Agent 内部配置字段的固定逻辑文档，不创建物理文件或独立文件版本。保存会在同一事务中复制当前运行配置、写入新的内部 revision 并移动当前指针；停用状态不会阻止继续编辑。后续新准入 Run 会立即使用新设置，无需重启服务；已经物化的运行持有当次精确 bundle，尚未物化的旧准入或后续副作用仍会重验当前 Agent 指针与权限，并在漂移时按既有安全边界 fail closed。四项内容位于其他项目可配置提示之后，是最高的项目可配置提示层，并紧邻最终平台关键提醒之前；平台安全、授权与隔离规则无论位于模板何处都始终优先，子 Agent 继承相同的准入快照。

Agent 不向用户提供创建版本、选择版本或发布版本的操作。项目与管理员代管项目 API 只保留内部 revision 历史的只读查询；Builder 确认和四项指令保存由后端内部原子维护不可变 revision，Run snapshot 仍固定实际使用的精确配置。

项目 Skill 的显示名称在同一项目内大小写不敏感且不可重复，不同项目可使用相同名称；`SKILL.md` frontmatter `name` 必须与资产 slug 完全一致。从列表新建 Skill 时，后端会在同一事务中创建默认停用的资产、版本 1 草稿以及根目录 `SKILL.md` 基础模板，不会留下没有版本文件的半成品，也不会自动发布；只有已发布版本才能通过列表或详情开关启用。详情不再提供空白版本入口，后续版本统一从当前选中版本点击“创建新版本”，修改并另存为新的不可变草稿。版本文件按真实目录树展示，只打开当前选中文件；新建文件需指定目标文件夹，并可在流程中创建嵌套目录。创建时也可用 `multipart/form-data` 上传 `.zip`、`.skill`（ZIP）、`.tar`、`.tar.gz` 或 `.tgz`；单一外层目录会自动剥离，资产创建和首版发布在同一事务完成，资产仍保持停用。单个 archive 及批量导入均限制为合计 100 MiB、最多 16384 个文件。Gateway 和统一 Nginx 入口只在 Skill archive 创建路由上允许最多 160 MiB 的 JSON/base64 或 multipart wire body，并在 JSON/Pydantic 或 multipart 路由处理前拒绝越界请求。每个不可变项目 Skill 版本的完整文件大小都会计入项目 `storage_bytes` 配额。项目自建 Skill 不提供归档或暂停：详情页二次确认并等待 5 秒后执行整包永久删除，原子删除全部文件和版本并释放对应配额；仍被 Agent 或已准入 Run 引用时返回 `409`，系统 Skill 永远不可删除。Worker 把系统 Skill 投影到 `/mnt/skills/public/<name>`，把项目 Skill 投影到 `/mnt/skills/custom/<asset_uuid>`；执行前按准入时固定的精确版本、checksum 和绑定重新校验，其他项目的 catalog 更新不会使当前 Run 失效。

## 项目结构

```text
deer-flow/
├── backend/
│   ├── app/                         # Gateway、Worker、Scheduler 与业务域
│   ├── packages/harness/deerflow/   # Agent harness、tools、sandbox、persistence
│   ├── scripts/                     # 数据库、验收和运维脚本
│   └── tests/                       # 后端单元、集成和 PostgreSQL 门禁
├── frontend/
│   ├── src/                         # Next.js 页面、组件和前端领域模块
│   └── tests/                       # 单元测试与 Playwright E2E
├── docker/                          # Compose、Nginx 和 Provisioner
├── deploy/helm/                     # Kubernetes/Helm 资源
├── scripts/                         # 根级安装、启动、诊断和部署编排
├── skills/public/                   # 生成 packaged system Skill archives 的 21 个源目录
├── contracts/                       # 跨进程与发布验收契约
├── docs/                            # 跨模块设计文档
├── config.example.yaml              # 配置模板
├── Install.md                       # 面向 Coding Agent 的安装流程
└── Makefile                         # 全栈命令入口
```

模块实现细节分别见 [`backend/AGENTS.md`](./backend/AGENTS.md) 和 [`frontend/AGENTS.md`](./frontend/AGENTS.md)。

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `make setup` | 运行交互式初始化向导 |
| `make doctor` | 检查配置、数据库和运行环境 |
| `make support-bundle` | 生成脱敏诊断材料 |
| `make dev` / `make start` | 启动本地全栈 |
| `make gateway` / `make worker` / `make scheduler` | 单独启动后端进程 |
| `make setup-db` | 在空库安装当前 head 并初始化 PostgreSQL |
| `make migrate-db` | 未来新增 revision 后显式执行已提交的 pending migrations |
| `make check-db` | 只读检查 PostgreSQL revision 与必需对象 |
| `cd backend && make lint && make test` | 后端格式、静态检查与测试 |
| `cd frontend && pnpm check && pnpm test` | 前端 lint、类型检查与单元测试 |
| `POSTGRES_TEST_URL=... make test-project-saas-postgres` | 运行真实 PostgreSQL 发布门禁；只能使用可丢弃测试实例 |

完整命令列表运行 `make help`。

GitHub Actions 由 `.github/workflows/project-saas-release-gates.yml` 统一执行完整后端测试（包含
`tests/blocking_io/`）、固定 23 文件 PostgreSQL 门禁、前端单元测试、确定性 Chromium E2E、构建和安全检查。
Replay E2E、发布、容器、Helm Chart 与版本校验继续使用各自的专用工作流。

## 文档

- [安装流程](./Install.md)
- [Project-first SaaS 设计](./docs/2026-07-12-project-first-saas-design.md)
- [后端文档索引](./backend/docs/README.md)
- [系统架构](./backend/docs/ARCHITECTURE.md)
- [API 路由](./backend/docs/API.md)
- [配置参考](./backend/docs/CONFIGURATION.md)
- [IM Channel Connections](./backend/docs/IM_CHANNEL_CONNECTIONS.md)
- [前端开发指南](./frontend/AGENTS.md)

## 安全边界

- 只对外开放 Nginx 入口，并在真实部署中配置网络访问控制和 TLS；不要直接暴露 Gateway、Frontend 或 Provisioner 端口。
- `LocalSandboxProvider` 在宿主环境执行命令，不是强隔离边界；只应在可信环境使用。面向不可信任务时应选择并验证容器或集群级 Sandbox。
- 不要把 API key、Cookie、Credential、数据库密码或完整连接 URL 写入代码、日志、截图和 issue。
- System admin 与项目能力遵循最小权限原则。项目外资源返回 404，项目成员缺少所需 capability 时返回 403。

## 参与贡献

提交前请阅读 [`backend/CONTRIBUTING.md`](./backend/CONTRIBUTING.md)，并运行与改动范围相匹配的后端、前端和 PostgreSQL 门禁。

## 许可证与致谢

本项目采用 [MIT License](./LICENSE)。感谢 [DeerFlow 上游项目](https://github.com/bytedance/deer-flow)及所有贡献者奠定的 Agent harness、工具和前端基础。
