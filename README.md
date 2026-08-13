# ActWeave

Weave intelligence into action.

[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](./backend/pyproject.toml)
[![Node.js](https://img.shields.io/badge/Node.js-22%2B-339933?logo=node.js&logoColor=white)](./Makefile)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

ActWeave 是一个面向多账户、多项目协作的全栈 Super Agent 系统。它以
LangGraph Agent harness 为执行核心，提供项目级权限、Agent/Skill/MCP 资产、长期
Memory、Sub-Agent、Sandbox、Automation、外部 Channel 和可恢复的持久化会话。

系统采用 project-first 架构：Gateway 负责认证、授权和 Run 准入，Worker 独占
Agent graph 执行，Scheduler 只负责到期 Automation 准入；PostgreSQL 保存应用状态、
运行记录和受治理的资产版本。

> 当前代码线源自 DeerFlow 2 的重写，与最初的 Deep Research 实现不共用代码。
> 原始版本见 ByteDance 上游 [`main-1.x`](https://github.com/bytedance/deer-flow/tree/main-1.x)。

## 核心能力

- 多账户、多项目工作区，包含成员、角色、邀请、配额、审计和通知。
- 项目私有 Thread/Run、持久化 SSE、断线恢复、取消、重试和文件交付。
- System/Project Agent、Skill、MCP 与 Credential 的不可变版本和准入快照。
- 长期 Memory、上下文压缩、Dream 整理、归档检索和账号级个性化控制。
- Sub-Agent、Guardrail、Tool Search、循环检测和可扩展工具链。
- Local、容器、BoxLite 和可选 Provisioner/Kubernetes Sandbox provider。
- 一次性或 Cron Automation，以及 Feishu、Slack、Telegram 等外部 Channel。
- 平台管理员的系统设置、模型目录、资产治理和运维界面。

## 运行架构

| 组件        |   默认端口 | 职责                                        |
| ----------- | ---------: | ------------------------------------------- |
| Nginx       |     `2026` | 唯一浏览器入口，代理前端和 `/api/*`         |
| Frontend    |     `3000` | Next.js Web UI                              |
| Gateway     |     `8001` | 认证、项目 API、Run 准入、查询和 SSE replay |
| Worker      | 无公开端口 | 唯一 Agent graph 执行进程                   |
| Scheduler   | 无公开端口 | 可选 Automation 轮询与准入进程              |
| Provisioner |     `8002` | 仅特定 Kubernetes Sandbox 模式需要          |

```text
Browser / Channel
        |
        v
Nginx :2026 ----> Frontend :3000
        |
        +--------> Gateway :8001 ----> PostgreSQL
                                      ^          ^
                                      |          |
                                   Worker    Scheduler
```

Gateway 不执行 Agent graph；Worker 不提供浏览器业务 API。私有资源按账户、项目和
owner 作用域隔离，浏览器状态和请求字段都不是授权依据。

## 快速开始

### 环境要求

- Python 3.12+
- Node.js 22+
- pnpm 10.26.2+
- `uv`
- PostgreSQL
- 本地全栈模式所需的 Nginx

从获准的内部代码源取得项目并进入仓库根目录：

```bash
make check
make install
make setup
```

`make setup` 会引导生成根目录 `config.yaml` 和所需环境配置。不要提交
`config.yaml`、`.env`、数据库密码或 provider key。完整字段和升级规则见
[配置参考](./backend/docs/CONFIGURATION.md)。

### 初始化数据库

为一个新的空 PostgreSQL 目标配置 `DATABASE_URL`、`POSTGRES_ADMIN_URL` 和初始化
所需的 Credential 环境变量，然后运行：

```bash
make setup-db
make check-db
```

- `make setup-db` 只初始化空目标库。
- 初始化会为应用表、Alembic 版本表、LangGraph 表及每个 `run_events` 物理分区写入
  非空的中文表注释和字段注释；缺失或漂移的注释会使 schema 校验安全失败。
- 已知旧版本必须先备份，再通过 `make upgrade-db` 显式升级。
- 未知 marker、未纳管的非空 schema 或 catalog drift 会安全失败。
- Gateway、Worker 和 Scheduler 从不自动迁移或修复 schema。
- 打包 System Asset 有新增不可变 release 时，先停止运行服务，在维护窗口执行
  `make upgrade-system-assets`；该命令从标准运行环境读取 `DATABASE_URL`，保留历史版本与
  既有项目 pin，并且可幂等重跑。
- 全局管理员可对已发布的 System Skill 版本执行不可逆治理撤销。撤销保留发布内容、历史、
  当前指针和既有项目 pin；新绑定、新 Run 及 Worker 重试/恢复会拒绝该版本，项目管理员需
  显式迁移到仍可绑定的版本或停用绑定。已经完成物化并正在运行的图不会被强制中断。
- 生产运维应通过平台外部的 cron、systemd timer 或编排器每日运行（至少每个 UTC 月
  成功一次）`make prepare-run-event-partitions`；该幂等命令只在当前 schema head 上
  预创建 UTC 当前月至 N+2 月分区，锁等待超时后可安全重试。不要把它挂到 Gateway、
  Worker 或 Scheduler 的启动路径。

详细准备步骤见 [Install.md](./Install.md)。

### 启动与停止

```bash
make dev      # 热更新开发模式
make start    # 本地优化构建模式
make stop
```

浏览器访问 <http://localhost:2026>。常见入口：

- `/workspace`：账户级多项目工作区。
- `/projects/{project_slug}`：项目会话、资产、Memory、Automation 和设置。
- `/admin`：仅 system admin 可访问的平台治理与运维页面。

## Docker 与部署

Docker 开发环境：

```bash
make docker-init
make docker-start
make docker-stop
```

本地 Compose 构建与运行：

```bash
make up
make down
```

当前仓库提供本地和 Docker Compose 整栈运行，不提供 Kubernetes/Helm 整栈部署
资源。`docker/provisioner/` 是可选的 Kubernetes Sandbox provider，不是完整应用的
部署方案。任何生产环境都需要独立验证容量、网络、安全、存储和故障恢复。

## 常用开发命令

| 命令                                              | 用途                           |
| ------------------------------------------------- | ------------------------------ |
| `make doctor`                                     | 检查配置、数据库和运行环境     |
| `make support-bundle`                             | 生成脱敏诊断包和内部事件草稿   |
| `make gateway` / `make worker` / `make scheduler` | 单独启动后端进程               |
| `make test`                                       | 使用隔离测试库运行后端核心测试 |
| `cd backend && make lint && make format`          | 后端静态检查和格式化           |
| `cd frontend && pnpm check && pnpm test`          | 前端 lint、类型检查和单元测试  |

完整命令见 `make help`。本私有仓库没有托管 CI；集成或发布前必须针对当前
checkout 手工执行相关 PostgreSQL、前端、浏览器、安全、容器和目标环境门禁。

## 安全边界

- 对外只开放受 TLS 和网络策略保护的 Nginx；不要直接公开 Gateway、Frontend 或
  Provisioner 端口。
- `LocalSandboxProvider` 在宿主机执行命令，不是强隔离边界；只用于可信环境。
- API key、Cookie、Credential、数据库密码和完整连接 URL 不得进入代码、日志、
  截图、浏览器缓存或诊断材料。
- System admin 不自动拥有项目权限；项目访问必须服从服务端返回的 membership 和
  capability。

## 文档导航

- [安装流程](./Install.md)
- [后端概览](./backend/README.md)
- [后端文档索引](./backend/docs/README.md)
- [系统架构](./backend/docs/ARCHITECTURE.md)
- [API 路由](./backend/docs/API.md)
- [配置参考](./backend/docs/CONFIGURATION.md)
- [前端概览](./frontend/README.md)
- [后端开发约定](./backend/AGENTS.md)
- [前端开发约定](./frontend/AGENTS.md)

## 许可证与致谢

本项目采用 [MIT License](./LICENSE)。感谢
[ByteDance DeerFlow 上游项目](https://github.com/bytedance/deer-flow)及其贡献者奠定的
Agent harness、工具和前端基础。
