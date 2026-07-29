# `dev` / `main` 模块级代码分析索引

## 1. 文档目标

这组文档回答的不是“`main` 多了哪些 commit”，而是：

- `main` 的每个模块最终是怎样实现的；
- 具体入口、类、函数、数据结构和调用链是什么；
- 状态保存在哪里，如何跨请求、进程或重启；
- 并发、异常、安全和租户隔离边界是什么；
- `dev` 已经采用了什么不同架构；
- 哪些 `main` 改动可以直接移植，哪些要重写，哪些不应合并。

每个模块独立成文件。Agent 与 Skill 已明确拆开，Run/Worker、Checkpoint、Streaming 也分别分析，不再把不同问题混到同一个章节。

## 2. 固定比较基准

| 基准 | Commit | 含义 |
| --- | --- | --- |
| 共同祖先 | `3be3969f8fc3f2d2b6d36ef5c26fa5593d916f2a` | 判断两个分支各自演进的起点 |
| `dev` | `8a91e95799c9b345d9540c7e201b33c603e7870c` | 当前 Project SaaS / Worker-only 实现 |
| `main` | `e317f7b8d9b2afb4c3925812d4774da602c9f8f3` | 本次要求深入读取的主分支实现 |

文档中的“新增/删除”都相对共同祖先或另一个固定 commit，不随之后分支移动而改变。

## 3. 模块导航

| 编号 | 模块文档 | 重点 |
| --- | --- | --- |
| 01 | [Agent](01-agent.md) | Lead Agent、middleware 顺序、Agent 定义/存储、模型覆盖与运行构造 |
| 02 | [Skill](02-skill.md) | Skill 发现、激活、allowed-tools、审查、演进与 `dev` catalog/binding |
| 03 | [Memory](03-memory.md) | Memory 提取、队列、合并、过期、注入和 `dev` 私有持久化 |
| 04 | [Subagent](04-subagent.md) | delegation、registry、executor、并发/总量/token 限制和父子状态 |
| 05 | [Run / Worker](05-run-worker.md) | Run admission、执行、取消、lease、重试和 Worker-only 边界 |
| 06 | [Checkpoint](06-checkpoint.md) | LangGraph checkpointer、full/delta、Store 和当前 PostgreSQL schema |
| 07 | [Streaming](07-streaming.md) | SSE、stream bridge、durable frames、cursor replay 与 reconnect |
| 08 | [Frontend](08-frontend.md) | Workspace、Project 路由、消息流、clarification、browser UI 和状态层 |
| 09 | [Gateway API](09-gateway-api.md) | Gateway lifespan、router、依赖注入、Run admission 与查询边界 |
| 10 | [Auth / Authz](10-auth-authz.md) | 登录、OIDC、cookie/CSRF、RBAC/capability、404/403 和可信代理 |
| 11 | [Security](11-security.md) | 输入/输出防护、secret、路径、SSRF、审计与跨租户风险 |
| 12 | [MCP](12-mcp.md) | MCP discovery、transport、Credential、快照、工具调用和 egress |
| 13 | [Sandbox](13-sandbox.md) | Provider、AIO/E2B/BoxLite、暖池、ownership、reconcile 与私有 lease |
| 14 | [Channels](14-channels.md) | Feishu/Slack/Telegram/Discord/DingTalk/WeChat、绑定与 dedupe |
| 15 | [Tools / Models](15-tools-models.md) | 模型工厂、Provider、工具输出、图片、ACP、Browserless/Playwright |
| 16 | [Config / Build / Deploy](16-config-build-deploy.md) | 配置 schema、单数据库、Make/serve、Nginx、Docker、Helm、CI |

## 4. 建议阅读顺序

### 4.1 先理解 `dev` 的最终运行边界

推荐：

```text
09 Gateway API
  -> 05 Run / Worker
  -> 07 Streaming
  -> 06 Checkpoint
  -> 10 Auth / Authz
```

这条线先说明：

- Gateway 只做认证、准入、查询与 durable SSE；
- Worker 才执行 Agent graph；
- Scheduler 只准入 Automation occurrence；
- PostgreSQL 是 Run、Job、stream、checkpoint 和业务数据的共同事实来源；
- Project/Owner capability 在副作用点重验。

如果不先确定这条边界，很容易把 `main` 的 Gateway 内 graph execution、Redis stream bridge 或进程内 session 直接移进 `dev`。

### 4.2 再理解 Agent 能力面

推荐：

```text
01 Agent
  -> 02 Skill
  -> 04 Subagent
  -> 12 MCP
  -> 15 Tools / Models
  -> 13 Sandbox
```

这条线说明一个 admitted Agent snapshot 最终怎样得到模型、Skill、MCP、工具和 Sandbox 能力，以及为什么运行中不能重新读取可变定义。

### 4.3 最后看产品和交付

推荐：

```text
08 Frontend
  -> 14 Channels
  -> 03 Memory
  -> 11 Security
  -> 16 Config / Build / Deploy
```

最后一组把用户交互、跨渠道入口、长期记忆、整体威胁面和实际部署连起来。

## 5. 全局架构差异

```text
main
Client
  -> Nginx
  -> Gateway
       -> Agent graph
       -> checkpointer / run store
       -> Redis stream bridge（多实例时）
       -> process-local or leased runtime resources

dev
Client
  -> Nginx
  -> Gateway
       -> PostgreSQL transactional admission
       -> PostgreSQL durable stream query/replay
  -> Worker
       -> claim PostgreSQL Job lease
       -> execute Agent graph
       -> persist frames before notify
  -> Scheduler
       -> atomically admit due Automation
```

`main` 的很多修复本身正确，但默认资源 owner 是 Gateway 进程或 `user_id + thread_id`。`dev` 的资源 key 至少要包含 Project/Owner，执行副作用还要受 Run/Job lease 约束。

## 6. 统一判断标准

每份文档都按以下标准给出移植结论：

### A. 可直接移植

满足：

- 不改变 `dev` 的 Project/Owner/Worker-only 边界；
- 局部算法或正确性修复；
- 有明确测试；
- 不引入旧存储/旧配置源。

例如：Provider 构造竞态、模型类型判断、路径 containment、Nginx conditional Upgrade。

### B. 借鉴设计、按 `dev` 重写

满足：

- 能力有价值；
- `main` 实现绑定 Gateway、Host 目录、Redis bridge 或 user/thread；
- 需要加入 Project/Owner/Run/Job lease、snapshot、quota/audit。

例如：E2B reconcile、browser session、历史上传、工具结果外部化、跨进程 runtime ownership。

### C. 不应移植

会破坏当前最终边界，例如：

- Gateway 执行 graph；
- 独立 `checkpointer`/`stream_bridge` 配置；
- runtime 从文件系统读取 Agent/Skill/MCP 定义；
- 跨私有 Run 暖池复用；
- 返回 Host path、secret、storage locator；
- 恢复增量 migration 或运行时修复 schema；
- 重复的 CI workflow。

## 7. 证据规则

分析使用以下证据优先级：

1. 固定 commit 的最终源码；
2. 固定 commit 的自动化测试；
3. 从共同祖先到分支 head 的实现提交；
4. README/计划文档仅作背景，不用来替代代码事实。

文中路径写成：

```text
main:backend/...
dev:backend/...
```

表示应使用 `git show <ref>:<path>` 读取固定版本，而不是把当前工作区文件误认为另一个分支的内容。

本次没有切换当前工作区分支。分析通过 `git show main:<path>` 和
`git cat-file blob main:<path>` 读取 `main` commit 中的完整 Git blob；两种读取方式的字节
校验一致。大文件按连续行段读取，避免把一次终端回显的长度截断误当作文件结尾。这样既能看到
`main` 的完整源码，又不会覆盖 `dev` 工作区中的文档或未跟踪文件。

## 8. 当前工作区说明

本次只新增/修改分析文档，没有修改业务代码、`.env`、数据库、运行服务或依赖。

工作区原有的未跟踪项：

- `docker/`
- `docs/2026-07-29-dev-main-module-code-analysis.md`

其中：

- `docker/` 是用户恢复出的内容，分析中会明确区分“工作区存在”和“`dev` Git 树已跟踪”；
- 单文件总报告保留，当前目录是更细的模块化版本，不擅自删除旧文档。

## 9. 使用方式

如果后续准备真正合并代码，建议不要按“模块整体 cherry-pick”，而是从每份文档的移植矩阵选择一个最小修复单元：

1. 固定一个问题；
2. 读取对应 `main` 最终实现和测试；
3. 写 `dev` 边界下的失败测试；
4. 移植或重写；
5. 跑模块测试；
6. 对数据库/私有资源改动追加 PostgreSQL isolation gate；
7. 最后运行相关的 M1-M7 foundation 与通用 E2E 门禁。

这样可以吸收 `main` 的成熟实现，同时不倒退 `dev` 已完成的 Project SaaS、Worker-only 和私有资源安全边界。
