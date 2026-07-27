# DeerFlow 上游能力语义移植总体设计

- 日期：2026-07-27
- 状态：规划中
- 当前完成度：P0–P8 尚未开始，共 0/9（0%）
- 目标仓库：`/Users/jiangfeng/deer-flow`
- 目标基线：`dev@7117d703563358fb351b4cd93fb28b50139573b2`
- 目标工作区：包含大量未提交和未跟踪修改，执行前由 P0 冻结精确快照
- 参考仓库：`/Users/jiangfeng/Documents/deer-flow`
- 参考基线：`main@26ba0b9e6af6f8a5428802176596b3733f5692be`
- 移植方式：按能力语义重写、测试先行、独立分支和小批量合并
- 权限边界：项目、所有者、Worker 运行租约和 PostgreSQL 持久化约束

## 1. 文档目的

本文档是 DeerFlow 项目优先、多用户 SaaS 基线吸收上游近期 Agentic Browser Control、Memory、
Checkpoint、恢复、运行租约、前端体验和流式性能能力的唯一移植计划。

目标仓库和参考仓库已经形成不同的运行架构。参考仓库将 LangGraph-compatible Agent runtime
嵌入 Gateway；目标仓库则要求 Gateway 只负责认证、准入、查询和持久化 SSE，只有独立 Worker
可以执行 Agent graph。因此，本次工作不是 Git 历史同步，也不是解决若干文本冲突，而是把上游
行为、协议、测试和安全规则重新实现到目标架构。

P0–P8 按从简单到困难排序。每个阶段必须可以独立评审、测试、回滚和交付。阶段完成状态以目标
仓库当前代码、提交记录和自动化测试证据为准，不能以上游提交存在、补丁可以应用或历史测试结果
作为完成依据。

## 2. 已冻结决策

本次移植采用以下不可变更基线；如需改变，必须先修订本文档并重新评审：

1. `/Users/jiangfeng/deer-flow` 是唯一目标仓库，`/Users/jiangfeng/Documents/deer-flow`
   只作为只读参考实现。
2. 参考基线固定为 `main@26ba0b9e6af6f8a5428802176596b3733f5692be`。参考仓库后续变化
   必须另行评审，不能静默进入本次范围。
3. 不合并参考仓库分支，不批量 cherry-pick，不覆盖目标目录，不以解决文本冲突代替架构适配。
4. 每项能力先提取行为合同和测试，再在目标架构中实现。
5. Gateway 不执行 Agent graph。`RunAgentPrivateExecutor` 和 `run_agent()` 的生产调用继续只发生
   在 Worker。
6. Gateway 继续拥有认证、project/admin API、事务性 Run 准入、查询和持久化 SSE replay。
7. Worker 继续拥有 job claim、运行租约、项目与所有者重新验证、checkpoint 访问、持久化 stream
   publication 和 terminal settlement。
8. PostgreSQL 是应用数据、任务、事件、检查点、Memory、文件、配额和审计的唯一持久化权威。
9. 所有私有资源继续绑定 `project_id + owner_user_id`；资源 ID、thread ID、run ID 或 browser
   session ID 不能单独构成授权依据。
10. 项目外访问和跨所有者私有访问继续返回公共 `404`；项目内能力不足继续返回 `403`。
11. `0001_project_saas_baseline` 保持冻结。任何新增 schema 从 `0002` 起线性追加，不修改、压缩、
    stamp 或重写历史。
12. 运行时启动和 `make check-db` 继续只读 schema，不允许为移植能力隐式创建或修复数据库对象。
13. 上游文件型 DeerMem 不作为目标仓库默认 Memory 存储。
14. 上游按 `(agent_name, user_id)` 分桶的接口不能替代目标仓库的项目、所有者、namespace 和 thread
    完整作用域。
15. Memory 异步更新在真正写入前继续重新验证项目状态、成员状态和 membership version。
16. 上游 process-local BrowserSessionManager 不能直接承担跨 Gateway/Worker 的持久会话。
17. Browser 第一阶段只承诺单个 Run 内的工具会话；跨 Run 会话和 Live 人工接管必须通过单独设计
    的共享 Browser Session 边界实现。
18. P7 如引入 Browser Session Service，它只作为受控运行时辅助组件，不拥有账号、项目、成员、
    Run 或其他业务数据权威，也不能取代 Gateway 授权和 Worker 运行租约。
19. Browser URL、重定向和 CDP 连接默认 fail-closed；允许私网地址和无保护 CDP 必须是显式的
    operator 配置。
20. Worker 租约失效后，旧执行者不得继续 model、tool、MCP、sandbox、file、checkpoint、stream
    或 terminal settlement 副作用。
21. Delta Checkpoint 是独立大项。未启用时 `full` 模式行为必须保持不变。
22. `full -> delta` 可以设计为平滑升级；`delta -> full` 必须显式转换或稳定拒绝，不能静默返回
    空消息或部分状态。
23. SSE replay gap 是控制事件，不表示 Run 完成或取消。
24. 前端继续使用 `/projects/{project_slug}` 项目路由和 project-scoped API，不复制参考仓库的
    `/workspace/chats` 路由所有权。
25. 前端不能根据角色名称自行推导授权，只使用服务端返回的 capability 和 project context。
26. 每个阶段使用独立分支和小型 PR；不得把 Browser、Memory、Checkpoint 和前端重构塞入一个
    不可拆分提交。
27. 自动化生成的代码必须经过人工审查。ChatGPT Codex 可以加速对照、测试和实现，但不能替代
    安全边界、迁移兼容性和发布证据的确认。

## 3. 产品和工程目标

### 3.1 目标

- 在不回退项目优先 SaaS 架构的前提下吸收上游近期高价值能力。
- 降低长流式回答的前端渲染频率和重复计算。
- 提升 Memory 信号识别、增量提取、背压和可观测性。
- 验证并补齐 Worker 运行租约在异常、阻塞和竞争条件下的 fail-stop 行为。
- 在持久化 SSE 历史被裁剪时提供明确、可恢复的 replay gap 协议。
- 提供受项目和运行租约保护的 Agentic Browser 工具。
- 为长会话引入可选 DeltaChannel，避免消息 checkpoint 随轮数出现二次方增长。
- 在共享 Browser Session 边界完成后提供 Live Browser 人工接管体验。
- 为每项能力提供聚焦测试、跨项目/所有者隔离测试和最终发布门禁证据。

### 3.2 非目标

- 将目标仓库重新改造成 Gateway 内嵌 Agent runtime。
- 同步参考仓库最近一周的全部提交。
- 保持与参考仓库完全相同的模块、类名、目录结构或 API 路由。
- 为了减少改造量而取消独立 Worker、Scheduler 或 durable PostgreSQL stream。
- 引入 Redis、Kafka 或外部消息总线作为本次必需依赖。
- 用文件系统替代项目 PostgreSQL Memory。
- 在 Browser 工具基础阶段承诺跨 Worker、跨 Run 或跨进程会话连续性。
- 在 Delta 模式首版支持任意 checkpoint 分叉语义。
- 修改冻结的 `0001_project_saas_baseline`。
- 在一个阶段内同时完成所有性能优化、功能移植和架构清理。
- 以 Codex 的代码生成结果、mock 测试或静态检查代替真实 PostgreSQL 和目标浏览器验证。

## 4. 参考能力与提交基线

### 4.1 Agentic Browser Control

| 提交 | 内容 | 目标处理方式 |
| --- | --- | --- |
| `fa496c0c` | Agentic Browser Control 主实现 | 提取工具合同、安全规则和测试；重新设计会话所有权 |
| `183280eb` | Browser extra YAML 检测 | 适配目标配置和安装脚本 |
| `2e5c8da2` | 本地 AIO traffic 绕过代理 | 只在目标 Browser/Sandbox 拓扑需要时移植 |

主实现包括 `navigate`、`snapshot`、`click`、`type`、`get_text`、`back`、`screenshot` 和
`close` 工具，稳定元素 ref、SSRF 检查、CDP 安全门、会话容量、Live WebSocket 和前端 Browser
面板。目标仓库必须拆成 P5 工具基础和 P7 持久会话/Live 两个阶段。

### 4.2 Memory 连续重构

| 提交 | 内容 | 目标处理方式 |
| --- | --- | --- |
| `de024646` | Memory template 外置 | 选择性移植模板边界 |
| `01a89f23` | 可插拔 MemoryManager | 参考接口思想，先实现项目 PostgreSQL backend |
| `4bf028d0` | 增量 agent-scoped Markdown facts | 移植增量和 fact 语义，不移植默认文件权威 |
| `8145d66a` | Memory message processing | 移植六类信号、过滤、watermark、背压和 metrics |
| `9eebc6a9` | Memory config reload 同步 | 对照目标 config 生命周期后选择性实现 |
| `8511fa6a` | consolidated fact 有效期继承 | 移植到目标 fact 合并逻辑 |

### 4.3 Checkpoint、恢复和运行租约

| 提交 | 内容 | 目标处理方式 |
| --- | --- | --- |
| `42baed8c` | `full/delta` 双模式和 DeltaChannel | P6 独立重构 |
| `244ce773` | delta resume 线性化 | 在 P6 Delta 基础完成后移植行为 |
| `090e80c1` | 无法确认 lease 时 fail-stop | P3 先做差异审计，只补缺口 |
| `1cd5dea3` | replay history gap 信号 | P4 重写到 durable PostgreSQL SSE |

### 4.4 前端体验与流式性能

| 提交 | 内容 | 目标处理方式 |
| --- | --- | --- |
| `39cc26dd` | 按线程恢复 composer draft | P1 适配项目路由 |
| `bb008812` | 防止流式单词动画重放 | P1 移植 |
| `97ca7f88` | Artifact 和 Markdown 加固 | P1 选择性移植 |
| `b5d2a504` | Tool call result 索引 | P1 优先移植 |
| `adac3e18` | 避免每个 chunk 重算消息内容 | P1 优先移植 |
| `f090f018` | 按 frame budget 合并流式渲染 | P1 优先移植 |
| `16b612cf` | 消息图片 maxWidth | P1 移植 |
| `d4fdc275` | Run duration 展示澄清 | P1 对照现有语义 |
| `6aad6805` | current uploads context 解析 | P1 对照目标上传链路 |
| `90f3a622` | 保留 leading orphan tool messages | P1 移植并补历史测试 |
| `55c21530` | 恢复 Artifact/Sidecar panel resize | P1 移植通用布局 |
| `68c0ffda` | 最近对话置顶 | 需要后端元数据时延后为独立 PR |
| `e17aff57` | 非 localhost 开发访问 | 独立配置 PR |
| `cb698832` | 本地化 AI disclaimer | 可独立移植 |
| `1db84354` | reasoning effort 默认标签 | 可独立移植 |

## 5. 目标与参考架构差异

### 5.1 服务拓扑

参考仓库：

```text
Frontend
   |
Gateway
   |-- REST
   |-- LangGraph-compatible runtime
   |-- RunManager
   |-- Agent tools
   |-- process-local Browser sessions
   |
Database / Stream backends
```

目标仓库：

```text
Frontend
   |
Gateway
   |-- authentication
   |-- project/private REST
   |-- Run admission and queries
   |-- durable SSE replay
   |
PostgreSQL jobs and run_events
   |
Worker
   |-- job claim and heartbeat
   |-- RunAgentPrivateExecutor
   |-- Agent graph and tools
   |-- project/owner revalidation
   |-- checkpoint and stream publication
```

这一区别决定了 Browser、Checkpoint、恢复和租约代码不能直接复制。

### 5.2 Browser 会话冲突

参考实现的 BrowserSessionManager 是 process-local singleton，并按 `thread_id` 保存会话。Agent 工具、
REST 导航和 Live WebSocket 在同一 Gateway 进程中复用它。

在目标仓库中直接复制会产生两个互不相同的会话：

```text
Agent tool -> Worker process BrowserSessionManager
Live UI    -> Gateway process BrowserSessionManager
```

因此：

- P5 只实现单个 Run 内由 Worker 持有的 Browser 工具会话；
- P7 再引入 Gateway 和 Worker 都能访问的 Browser Session Service；
- 任何跨 Run 连续性都必须有显式 session owner、lease、routing 和 cleanup 合同。

### 5.3 Memory 作用域冲突

参考实现主要按 `(agent_name, user_id)` 分桶，并使用可插拔 manager 和 DeerMem 文件 backend。
目标实现按项目私有边界存储：

```text
project_id
+ owner_user_id
+ namespace
+ thread_id
+ membership_version
```

P2 只移植信号、过滤、增量和队列算法；存储、作用域和 revalidation 继续以目标实现为准。

### 5.4 Checkpoint 和 Run ownership 冲突

参考实现的 RunManager、ownership heartbeat 和 checkpoint mutation 都位于 Gateway runtime。
目标实现的 job lease、Run execution lease 和 side-effect fence 位于 Worker 和 PostgreSQL repository。

因此：

- P3 只对照 lease 场景和测试，不搬 RunManager；
- P6 将 DeltaChannel 接入目标 `ProjectScopedCheckpointer` 和 Worker；
- 所有 checkpoint materialization 和 mutation 都必须携带项目私有作用域；
- lease loss 后的 checkpoint write 必须 fail-closed。

### 5.5 前端路由和缓存冲突

参考前端拥有 `/workspace/chats/{thread_id}` 和 `/workspace/agents/...` 路由。目标前端的项目工作位于
`/projects/{project_slug}`，所有请求和缓存都必须绑定 account/project。

P1 和 P4 只能移植组件算法、状态机和协议，不能复制参考页面的 route ownership、query key 或
API client。

## 6. Codex 移植工作协议

### 6.1 每个任务的固定步骤

1. 阅读根目录 `AGENTS.md` 和目标模块 `AGENTS.md`。
2. 固定目标仓库和参考 commit，不从网络上的浮动 `main` 获取行为。
3. 使用 `git show`、路径列表和 no-index diff 提取行为差异。
4. 写明本任务的目标、非目标、权限边界和失败语义。
5. 先增加会失败的聚焦测试。
6. 用目标仓库的模块和作用域重新实现。
7. 运行聚焦测试、格式、类型检查和必要的真实 PostgreSQL 测试。
8. 检查是否误改无关工作区文件。
9. 更新相关 README、AGENTS 或设计文档。
10. 输出参考提交、修改文件、测试证据、未移植内容和剩余风险。

### 6.2 分支和 PR 原则

- 集成分支建议使用 `codex/port-upstream-capabilities`。
- 每个能力分支从同一已冻结目标基线创建。
- 一个 PR 只承载一个可验证行为边界。
- 纯重构和功能变化尽量分开。
- schema、runtime、frontend protocol 不放在同一个不可拆分 commit。
- 不使用长期 stash 作为现有工作区的唯一备份。
- 不对用户已有未提交修改执行 reset、checkout 覆盖或批量格式化。

### 6.3 Codex 完成报告

每个任务必须报告：

- 参考 commit；
- 目标行为合同；
- 修改文件；
- 新增和修改测试；
- 实际执行的命令与结果；
- 是否涉及 PostgreSQL schema；
- 是否改变 API 或 stream contract；
- 与 project/owner/lease 边界的关系；
- 未覆盖的目标环境；
- 下一阶段依赖。

## 7. 目标运行架构

### 7.1 Memory

```text
Agent messages
  -> project memory signal processing
  -> project-scoped asyncio debounce/backpressure queue
  -> membership revalidation
  -> ProjectPostgresMemoryManager
  -> user_project_memories / project-private facts
```

可插拔 MemoryManager 必须把 `PrivateResourceScope` 或等价的不可伪造作用域作为一等参数，不能要求
调用者分别传入可信 `project_id` 和 `owner_user_id` 字符串。

### 7.2 Durable stream recovery

```text
Worker append durable frame with lease proof
  -> PostgreSQL run_events
  -> Gateway reads retained interval
  -> normal frames OR id-less gap control frame
  -> Frontend reloads durable state and rejoins retained tail
```

gap payload 只返回恢复所需的稳定公共字段，不返回数据库内部错误、其他项目 cursor 或私有内容。

### 7.3 Delta Checkpoint

```text
config checkpoint_channel_mode
  -> process-frozen mode
  -> mode-matched state schema
  -> CheckpointStateAccessor
  -> ProjectScopedCheckpointer
  -> PostgreSQL LangGraph checkpoint tables
```

任何 Gateway 查询和 Worker 写入都经过统一 accessor，不允许业务代码直接把 raw checkpoint tuple
解释成完整线程状态。

### 7.4 Browser 工具基础

```text
Worker Run
  -> lease and authorization boundary
  -> Browser tool
  -> Run-local BrowserSessionManager
  -> Playwright / Chromium
  -> bounded screenshot or text result
  -> private file/artifact persistence
```

P5 Run 完成、取消、授权撤销或 lease loss 后关闭会话。

### 7.5 Browser 持久会话和 Live

推荐 P7 目标：

```text
Agent Browser Tool ----\
                        -> Browser Session Service -> Chromium
Gateway Live WebSocket -/
```

Browser Session Service 至少绑定：

- `project_id`
- `owner_user_id`
- `thread_id`
- `session_id`
- `agent_operation_lease`
- `live_viewer_lease`
- `expires_at`

Gateway 和 Worker 只能使用短期、受作用域限制的内部凭证访问会话。

## 8. 持久化和 schema 策略

- P1 前端优化不改变 schema。
- P2 优先复用现有项目 Memory 表；新增字段必须先证明无法使用现有 JSON/metadata 合同。
- P3 lease 审计优先不改变 schema。
- P4 replay gap 优先从现有 `run_events` retained interval 推导。
- P5 Run-local Browser 不持久化 Cookie、DOM、页面正文或 browser storage。
- P6 Delta 模式优先复用 LangGraph checkpoint 表，并通过 checkpoint metadata 标记模式。
- P7 如需 Browser session 元数据、路由或 cleanup job，新增 `0002` 或后续线性 revision。
- migration 必须 forward-only。
- migration 不读取或写入 credential 明文。
- migration 必须有空库 setup、旧 head upgrade、current/head check 和 unknown revision fail-closed 测试。
- Gateway、Worker 和 Scheduler 启动不得执行 migration。

## 9. 安全和隐私边界

### 9.1 通用

- 所有私有 API 从认证账号和服务端项目上下文解析 owner。
- 客户端不能提交可信 owner、role、capability 或 membership version。
- stream cursor、checkpoint ID、thread ID、browser session ID 和 fact ID 都不能绕过完整作用域查询。
- 错误和日志不得包含 prompt、Memory 正文、页面正文、Cookie、Authorization header、credential、
  checkpoint body 或原始 SQL 错误。

### 9.2 Memory

- 待处理 Memory item 捕获完整 `PrivateResourceScope` 和 membership version。
- 写入前重新验证 membership 和项目状态。
- 被移除、停用、降级或项目暂停后，不再提交失去授权的更新。
- Memory 搜索、更新和删除都包含项目和 owner 作用域。

### 9.3 Browser

- URL 在初始导航和每次重定向时执行 public HTTP/HTTPS 检查。
- DNS 解析和最终连接目标都需要防止 SSRF rebinding。
- `allow_private_addresses` 只允许 operator 显式配置。
- CDP 默认拒绝无法安装 request guard 的外部 context。
- Live viewer 一次只允许一个有效租约。
- Browser 截图和页面文本进入 Agent 前执行大小上限。
- Cookie、localStorage、sessionStorage 和认证 header 不写入日志、stream、checkpoint、Memory 或审计。
- Browser session cleanup 不依赖前端正常断开。

### 9.4 Checkpoint 和 lease

- checkpoint read/write 使用项目和 owner 完整作用域。
- lease proof 只保存在 Worker 内存，数据库保存 hash。
- side-effect boundary 检查不能延长已失效 lease。
- 旧 Worker 的晚到 completion、stream 和 checkpoint write 必须被拒绝。
- takeover 和 recovery 使用原子条件更新，不能先读后无条件写。

## 10. 错误语义

- 跨项目、跨 owner 或未知私有资源：`404`。
- 当前项目内缺少所需 capability：`403`。
- 活跃 Run、checkpoint reservation 或 Browser viewer 冲突：稳定 `409`。
- Browser session 容量耗尽：稳定 `429` 或明确的容量公共错误码。
- Browser、Worker、数据库或共享 Browser Service 暂时不可用：`503`。
- checkpoint mode 不兼容：稳定 `409`；运行中尝试改变进程冻结模式：`503`。
- replay cursor 早于 retained watermark：发送 id-less `gap` 控制帧，不把 Run 标记失败。
- lease lost：停止执行并拒绝 settlement，不向用户返回内部 token、worker ID 或数据库错误。
- 所有错误包含稳定公共错误码和 `request_id`。

## 11. P0：基线冻结和移植清单

- 难度：低
- 预计投入：1–2 工程日
- 依赖：无

### 11.1 工作内容

- 记录目标 commit、分支、未提交和未跟踪文件。
- 为当前大量工作区修改创建可恢复基线。
- 建立集成分支和阶段分支命名约定。
- 为四组上游能力生成文件清单、目标对应模块和脏文件重叠清单。
- 记录当前 backend、frontend、PostgreSQL gate 和构建基线。
- 确认参考仓库干净且 commit 可重复读取。

### 11.2 交付物

- 基线 commit、patch bundle 或等价的可恢复快照。
- 上游能力到目标模块的映射表。
- 初始测试结果。
- 已知失败和环境限制清单。

### 11.3 退出条件

- 可以恢复到移植前状态。
- 所有阶段都从明确基线创建。
- 未把参考仓库写入目标 Git 历史。
- 未丢失或覆盖用户已有修改。

## 12. P1：纯前端体验和流式渲染性能

- 难度：低到中
- 预计投入：3–5 工程日
- 依赖：P0

### 12.1 第一批

- 为 processing group 一次性构建 ToolResult lookup。
- 为 Browser preview 一次性构建 screenshot-bearing result lookup，但不启用 Live Browser。
- 避免每个 stream chunk 重新派生完整 message content。
- 把高频 stream updates 合并到浏览器 frame budget。
- 修复增长中 Markdown 的历史单词动画重放。
- 保留 leading orphan ToolMessage。

### 12.2 第二批

- 按 account、project、agent 和 logical conversation scope 保存 composer draft。
- 恢复 Artifact 和 Sidecar 共用的 ResizablePanelGroup。
- 加固 Artifact auto-open timer cleanup。
- 统一图片 maxWidth。
- 澄清 run duration 与 reasoning duration。
- 对照目标上传链路解析 current uploads context。

### 12.3 暂缓

- Browser Live panel。
- replay gap recovery。
- 需要服务端持久字段的 chat pin。
- 参考仓库 `/workspace/chats` 页面复制。

### 12.4 测试

- `MessageGroup` lookup 只构建一次且保持第一条有效结果语义。
- 长流式回答按 frame 合并，不丢最终 chunk。
- Markdown 历史文字不重复动画。
- orphan ToolMessage 在历史和流式路径都可见。
- draft 在项目、账号和 thread 间隔离。
- panel drag 不触发逐帧插值和错误滚动到底部。
- `pnpm test`
- `pnpm check`
- 相关 deterministic Playwright 用例。

### 12.5 退出条件

- 不改变后端 API。
- 不引入跨项目 cache key。
- 现有 Artifact、Sidecar、输入区和消息历史行为不回退。

## 13. P2：Memory 算法增强

- 难度：中
- 预计投入：4–7 工程日
- 依赖：P0

### 13.1 工作内容

- 增加 correction、reinforcement、preference、identity、goal 和 decision 六类信号。
- 增加 trivial acknowledgement 过滤。
- 将 detected signals 作为 extraction hints 传递。
- 增加增量 watermark，失败时不提前推进。
- 增加 queue max depth 和非信号更新背压。
- 信号更新和 summarization emergency flush 不因普通背压丢失。
- 增加 pending、processed、dropped、failed 等 metrics。
- graceful shutdown 时有界 flush。
- consolidated fact 继承 source 的 expected validity。
- 保留 asyncio event-loop queue 和 membership revalidation。

### 13.2 可选 P2B

如业务需要多 Memory backend，再增加目标仓库自己的 `MemoryManager` 合同：

- tier 1：项目作用域 add/get_context；
- tier 2：search/export/delete；
- tier 3：fact create/update/delete；
- 默认 backend：`ProjectPostgresMemoryManager`；
- `noop` backend 只用于明确禁用；
- 不提供默认文件 backend。

### 13.3 测试

- 六类信号正例、反例和组合。
- ack、空内容、tool-only 和 transient message 过滤。
- watermark 成功、失败、重试和 emergency bypass。
- 队列同 key 合并、不同项目隔离和背压。
- 被停用成员的 pending item 在写前丢弃。
- 同 owner 跨项目隔离。
- 同项目跨 owner 隔离。
- fact 有效期和冲突更新。
- 真实 PostgreSQL Memory 聚焦测试 0 skip。

### 13.4 退出条件

- PostgreSQL 仍是 Memory 权威。
- 所有自动写入都重新验证成员资格。
- 不在日志、trace 或 metrics 中记录 Memory 正文。

## 14. P3：运行租约差异审计和补强

- 难度：中
- 预计投入：2–4 工程日
- 依赖：P0

### 14.1 审计矩阵

| 场景 | 预期行为 |
| --- | --- |
| heartbeat 返回 false | 立即 invalidate，停止 handler，不 settlement |
| heartbeat 抛出数据库异常 | 视为 lease lost，不继续副作用 |
| heartbeat 阻塞直到 lease expiry | deadline 后 fail-stop |
| handler 与 heartbeat 同时完成 | 只允许一个受 lease 保护的 terminal path |
| handler 不响应取消 | Worker 停止接收新工作 |
| 旧 Worker 晚到 completion | 数据库拒绝覆盖新 owner 或既有 terminal |
| 旧 Worker 晚到 stream append | 同事务 lease 校验拒绝 |
| 旧 Worker 晚到 checkpoint write | side-effect boundary 拒绝 |
| takeover 与原 owner heartbeat 竞争 | 原子条件更新决定唯一 owner |
| 已提交远程副作用 | 标记 ambiguous，不能伪装成安全重试 |

### 14.2 工作原则

- 先复用目标现有 `JobLeaseAuthority`、`WorkerService` 和 `PrivateRunRepository`。
- 只补缺失场景、deadline 或测试。
- 不复制参考 RunManager。
- 不把 lease authority 移回 Gateway。

### 14.3 退出条件

- lease loss 后 model/tool/MCP/sandbox/file/checkpoint/stream/settlement 全部 fail-closed。
- uncooperative handler 会使当前 Worker capacity fail-stop。
- 聚焦 job、Worker、lease、recovery 测试通过。

## 15. P4：SSE Replay Gap 和前端恢复

- 难度：中高
- 预计投入：3–5 工程日
- 依赖：P1、P3

### 15.1 后端

- 为 durable run events 明确 retained earliest seq 和 latest seq。
- 检查合法 `Last-Event-ID` 是否早于 retained interval。
- 发送无 SSE ID 的 `gap` 控制帧。
- payload 返回稳定 `stream_replay_gap` code 和恢复所需 tail 信息。
- gap 不写 terminal，不取消 Run。
- Gateway 重启后从 PostgreSQL 得出同样结果。

### 15.2 前端

- API client 包装 initial 和 joined streams。
- 识别 gap 并清除 stale reconnect metadata。
- 发出内部 `stream_replay_gap` event。
- 清理 optimistic、transient 和 subtask 临时状态。
- 失效 durable history 和 thread values cache。
- 重新读取 durable state。
- 从 retained tail 重新 join。
- 设置有限 recovery rejoin 次数。
- 展示本地化非阻塞恢复提示。

### 15.3 测试

- cursor 位于 retained interval。
- cursor 恰好等于 earliest 前一个位置。
- cursor 早于 watermark。
- 无 cursor 的 live tail。
- gap 与新 frame 并发。
- Gateway restart。
- Run 在 gap 后继续并最终 terminal。
- 连续 gap 到达上限。
- 不重复消息、tool result 或 terminal。
- cursor 不能跨 project/owner 使用。

### 15.4 退出条件

- 前后端 gap contract 固定。
- gap 不被当成普通 stream completion。
- durable history reload 后 UI 与 PostgreSQL 一致。

## 16. P5：Run-local Agentic Browser 工具

- 难度：高
- 预计投入：5–8 工程日
- 依赖：P3

### 16.1 工作内容

- 引入可选 Playwright browser extra。
- 实现 Run-local BrowserSessionManager。
- 实现 navigate、snapshot、click、type、get_text、back、screenshot 和 close。
- snapshot 为交互元素生成稳定 numeric ref。
- 每次操作返回新 snapshot，避免持有陈旧 selector/handle。
- 引入 URL 和 redirect SSRF guard。
- 默认拒绝无保护 CDP。
- 限制 timeout、viewport、screenshot size、text length 和 session capacity。
- screenshot 通过目标私有文件/Artifact 边界持久化。
- lease loss、authorization revoke、cancel、Run terminal 和 Worker shutdown 都触发 cleanup。

### 16.2 生命周期限制

P5 明确只保证：

```text
同一个 Worker 执行的同一个 Run 内会话连续
```

P5 不保证：

- 下一轮 Run 命中同一个 Worker；
- Browser Cookie 跨 Run 保留；
- Gateway REST 导航复用该会话；
- Live WebSocket 人工接管。

### 16.3 测试

- mocked Playwright 工具测试。
- real Chromium 可选集成测试。
- 稳定 ref 和 stale ref 拒绝。
- private IP、loopback、link-local、redirect 和 DNS rebinding。
- CDP fail-closed。
- capacity、LRU、pinned operation 和 cleanup。
- lease loss 后禁止下一次 browser action。
- 截图大小、文件作用域和内容脱敏。
- Browser extra 未安装时 feature discovery 返回不可用且启动行为明确。

### 16.4 退出条件

- Browser 工具不会破坏 Worker-only graph。
- 不依赖 Gateway process-local singleton。
- P5 限制在产品和配置中明确展示。

## 17. P6：Delta Checkpoint、恢复和线性化

- 难度：极高
- 预计投入：10–15 工程日
- 依赖：P3、P4

### 17.1 子阶段

1. 增加 `checkpoint_channel_mode: full | delta`，默认 `full`。
2. 在进程首次编译 graph 时冻结模式，运行中改变返回稳定错误。
3. 增加 full 和 delta 对应的 ThreadState schema。
4. 为 messages 使用 DeltaChannel 和线性合并函数。
5. 为所有 state read/write/history 增加 `CheckpointStateAccessor`。
6. 写入 checkpoint metadata mode marker。
7. 在读取前执行模式兼容检查。
8. 支持 delta 读取旧 full checkpoint。
9. full 读取 delta checkpoint 必须 fail-closed。
10. 实现 delta resume 线性化。
11. 对 branch/fork 采取明确拒绝或线性 head 转换。
12. 将 accessor 接入 Worker `ProjectScopedCheckpointer`、Gateway read API、compact、goal 和 state mutation。

### 17.2 恢复合同

- Worker crash 后，新 Worker 从最后一个已提交 checkpoint 恢复。
- 未提交远程副作用不假定完成。
- 已标记 ambiguous side effect 的 Run 不自动无条件重放。
- resume 前重新验证 project、owner、membership、asset snapshot、credential closure 和 lease。
- checkpoint write 与 durable stream 顺序必须有明确合同。
- takeover 后旧 Worker 的 checkpoint write 被 lease fence 拒绝。

### 17.3 测试

- full 模式回归。
- full -> delta。
- delta -> full 拒绝。
- mode marker 缺失的旧 checkpoint。
- raw delta blob 不被误读为空消息。
- 长消息历史的 checkpoint 体积增长。
- resume 线性时间和结果一致性。
- fork/branch。
- compact、goal、manual state write 与 active Run 冲突。
- Worker crash、lease expiry、takeover 和旧 Worker 晚写。
- 项目和 owner 隔离。
- memory/sqlite 只用于 harness 单元语义时必须与生产路径隔离；生产验收使用 PostgreSQL。

### 17.4 退出条件

- 默认 full 模式无行为回退。
- Delta 的兼容性和迁移方向文档完整。
- 所有生产 state access 通过统一 accessor。
- 真实 PostgreSQL checkpoint/recovery 测试 0 skip。

## 18. P7：持久 Browser Session 和 Live 人工接管

- 难度：最高
- 预计投入：12–20 工程日
- 依赖：P4、P5；建议在 P6 稳定后进入最终集成

### 18.1 架构决策记录

实施前必须比较：

1. 独立 Browser Session Service；
2. Worker sticky routing；
3. Provisioner/Browser container 托管。

默认推荐独立 Browser Session Service，因为它能同时服务 Worker tool 和 Gateway Live WebSocket，
避免依赖 Uvicorn worker affinity 或 job sticky scheduling。

### 18.2 服务合同

- 创建、获取、关闭和回收 session。
- 绑定 project、owner、thread 和 session ID。
- Agent operation lease。
- 单一 Live viewer lease。
- 短期签名内部访问令牌。
- capacity、LRU、idle timeout 和 hard lifetime。
- frame capture 与 input dispatch 分离。
- Gateway/Worker health 和 readiness。
- crash orphan cleanup。
- 会话不存在、已过期、被其他 viewer 占用和 lease lost 的稳定错误。

### 18.3 前端

- `/api/features` 控制 Browser UI 是否显示。
- 项目 chat page 拥有 Browser trigger 和 panel。
- Browser panel 与 Artifact、Sidecar 共用一个 ResizablePanelGroup。
- URL bar、frame、pointer、wheel 和 keyboard。
- 一个 physical click 只发送一个 click action。
- 非 move 输入触发有界后台 frame refresh。
- 断线重连不绕过 viewer lease。
- project/thread 切换立即关闭旧连接并清理帧。

### 18.4 测试

- Worker tool 与 Gateway Live 观察同一 session。
- 多 Gateway、多 Worker 和会话路由。
- 同一 session 只允许一个 Live viewer。
- project/owner/thread 隔离。
- agent operation 与人工输入竞争。
- lease expiry、Worker crash、Gateway restart 和 Browser Service restart。
- session capacity、cleanup 和 orphan recovery。
- WebSocket CSRF/origin/auth。
- 浏览器输入和 frame rate。
- Chromium 真实端到端。

### 18.5 退出条件

- 不要求 `GATEWAY_WORKERS=1` 才保持正确性，或者部署限制被明确冻结并由启动门拒绝不安全配置。
- Agent 和 Live UI 确认操作同一个 Chromium session。
- session 生命周期不依赖单个 Gateway 或 Worker 进程。
- 不持久化 Browser secret state。

## 19. P8：集成、发布门禁和目标环境验收

- 难度：高
- 预计投入：4–7 工程日
- 依赖：计划发布的全部前序阶段

### 19.1 分层验证

日常 PR：

- 相关聚焦单元测试；
- backend Ruff format/check 或 frontend `pnpm check`；
- 修改涉及的真实 PostgreSQL 聚焦文件；
- `git diff --check`。

阶段结束：

- backend 完整 pytest 或 frontend 完整 unit tests；
- 相关 deterministic Playwright；
- 生产构建；
- source-absence 和架构边界检查。

P4、P6、P7：

- `POSTGRES_TEST_URL=... make test-project-foundation-postgres`
- 完整候选阶段使用 `make test-project-saas-postgres`
- PostgreSQL gate 必须 0 skip。

最终候选：

- `make release-acceptance`
- 宿主机 Gateway、Worker、Scheduler、Frontend 和 Nginx。
- Chromium Browser 工具和 Live。
- Gateway restart SSE cursor/gap。
- Worker crash、lease takeover 和 checkpoint resume。
- 两个项目、同项目两个 owner 和一个项目外账号的隔离矩阵。

### 19.2 发布阻断条件

以下任一情况存在时不得发布：

- Gateway 重新出现生产 Agent graph 执行。
- Memory、checkpoint、stream 或 Browser 可以跨项目/owner 读取。
- lease loss 后仍可提交副作用。
- gap 被误处理为 Run completion。
- full 模式无法读取旧 checkpoint。
- Delta checkpoint 被 full 模式静默误读。
- Browser 能访问未授权私网地址或泄露 Cookie/credential。
- PostgreSQL gate 存在 skip。
- `0001_project_saas_baseline` 被修改。

## 20. PR 和交付拆分

建议形成 8–12 个可独立审查的 PR：

| PR | 内容 | 阶段 |
| --- | --- | --- |
| 1 | 基线、能力映射和测试清单 | P0 |
| 2 | MessageGroup lookup 和 orphan tool message | P1 |
| 3 | stream render coalescing 和 Markdown animation | P1 |
| 4 | draft、panel 和小型前端体验 | P1 |
| 5 | Memory signals、filter、watermark 和 backpressure | P2 |
| 6 | lease gap tests 和必要补强 | P3 |
| 7 | durable SSE gap backend | P4 |
| 8 | frontend gap recovery | P4 |
| 9 | Run-local Browser tools | P5 |
| 10 | DeltaChannel schema/accessor | P6 |
| 11 | Delta resume/recovery/branch semantics | P6 |
| 12 | Browser Session Service 和 Live UI | P7 |

如果单个 PR 超出可审查范围，应继续按行为边界拆分，而不是为了符合数量表强行合并。

## 21. 交付里程碑

| 里程碑 | 内容 | 难度 | 当前状态 |
| --- | --- | --- | --- |
| P0 | 基线冻结、能力映射和测试证据 | 低 | 未开始 |
| P1 | 纯前端体验与流式渲染性能 | 低到中 | 未开始 |
| P2 | 项目 PostgreSQL Memory 算法增强 | 中 | 未开始 |
| P3 | Worker 运行租约差异审计与补强 | 中 | 未开始 |
| P4 | Durable SSE replay gap 和前端恢复 | 中高 | 未开始 |
| P5 | Run-local Agentic Browser 工具 | 高 | 未开始 |
| P6 | Delta Checkpoint、恢复和线性化 | 极高 | 未开始 |
| P7 | 持久 Browser Session 和 Live 接管 | 最高 | 未开始 |
| P8 | 集成、发布门禁和目标环境验收 | 高 | 未开始 |

阶段完成状态必须同时满足代码、测试、文档和目标环境要求。只完成代码生成或局部 mock 测试不能更新
为“已完成”。

## 22. 排期和优先级

建议优先完成 P0–P4。它们可以在不引入新基础服务的情况下获得以下收益：

- 前端长流式响应更平滑；
- Memory 提取更准确且有背压；
- lease 行为得到系统性证明；
- Gateway 重启或 retained history 裁剪后可以恢复 UI。

单名熟悉目标架构的工程师配合 ChatGPT Codex：

- P0–P4：约 3–4 周；
- P5：约 1–2 周；
- P6：约 2–3 周；
- P7：约 2–4 周；
- P8：约 1 周。

部分前端、Memory 和 Browser 原型可以并行，但 P3 是 P4/P5/P6 的运行安全前置，P5 是 P7 的行为
前置。单工程师顺序完成完整生产级范围预计 9–14 周；拆成前端/Memory 与 runtime/Browser 两条受控
工作流后，预计 6–10 个日历周。具体取决于 Browser Session Service 是否需要支持多节点部署以及
Delta checkpoint 是否需要历史数据转换。

## 23. 风险和回滚

| 风险 | 影响 | 控制和回滚 |
| --- | --- | --- |
| 目标工作区已有大量修改 | 合并覆盖用户工作 | P0 可恢复快照；小分支；禁止 reset 覆盖 |
| 复制参考 runtime | 破坏 Worker-only 边界 | source-absence gate；架构测试 |
| Memory 作用域退化 | 跨项目或 owner 泄露 | typed scope；真实 PostgreSQL 隔离测试 |
| replay gap 状态机错误 | 重复、丢消息或错误结束 | 后端和前端分 PR；协议测试；feature flag |
| Delta 误读旧数据 | 空消息或历史损坏 | 默认 full；mode marker；fail-closed |
| Browser process-local 会话 | Agent 和 UI 操作不同浏览器 | P5 明确 Run-local；P7 共享服务 |
| Browser SSRF | 内网和 metadata 泄露 | redirect/DNS guard；默认拒绝私网 |
| lease 竞争回归 | 双执行和重复副作用 | 原子 repository 条件；takeover tests |
| 大 PR 难以审查 | 隐藏安全回归 | 8–12 个行为 PR；每阶段退出条件 |

功能回滚优先通过配置和 feature flag：

- `checkpoint_channel_mode` 回到 `full` 只适用于尚未写入 delta checkpoint 的环境；
- Browser feature 可以整体禁用；
- Memory 新 signal 和 backpressure 可以用配置关闭，但不能绕过作用域；
- frontend optimization 可以独立回滚；
- 已执行的 schema migration 不 downgrade，通过后续 forward revision 修正。

## 24. 关键权衡

- 语义移植比直接 cherry-pick 慢，但避免把 Gateway runtime、文件 Memory 和 process-local Browser
  带入目标架构。
- P5 限制 Browser 为 Run-local，降低首版价值，但能先验证 Agent 工具、安全和资源消耗。
- 独立 Browser Session Service 增加一个运行组件，但为 Agent、Live UI、多 Worker 和 cleanup 提供
  清晰边界。
- DeltaChannel 降低长会话 checkpoint 存储增长，但增加兼容性、materialization 和 branch 复杂度。
- gap recovery 增加前后端状态机，但比静默丢失历史或错误完成 Run 更可靠。
- 可插拔 MemoryManager 有扩展价值，但目标仓库仍应把项目 PostgreSQL backend 作为默认和权威实现。
- 聚焦测试提高迭代速度，完整 PostgreSQL 和 release acceptance 只在阶段和最终候选运行，避免持续
  重复执行高成本门禁。

## 25. Codex 任务模板

每个移植 PR 使用以下基础任务描述：

> 对照 `/Users/jiangfeng/Documents/deer-flow@26ba0b9e6af6f8a5428802176596b3733f5692be`
> 的指定能力，将行为语义移植到
> `/Users/jiangfeng/deer-flow@7117d703563358fb351b4cd93fb28b50139573b2`。先完整阅读根目录和
> 相关模块的 `AGENTS.md`，先写失败测试，再实现。禁止合并参考分支、批量 cherry-pick 或覆盖目录；
> 禁止削弱 project/owner 作用域、Worker-only graph、PostgreSQL 权威、运行租约和 side-effect
> boundary。保留所有无关工作区修改。完成后报告参考提交、行为合同、修改文件、测试结果、未移植内容、
> 数据库/API/stream 变化和剩余风险。

针对核心运行时任务增加：

> 在写代码前输出当前调用链和目标调用链，标明 Gateway、Worker、PostgreSQL 和前端的所有权；如果参考
> 实现依赖 process-local singleton 或 Gateway 内嵌 runtime，先停止实现并提出目标架构适配方案，不得
> 通过全局变量、关闭多 Worker 或绕过权限检查隐藏冲突。

## 26. 最终验收摘要

本次移植完成后，DeerFlow 必须继续满足项目优先、多项目、私有工作按项目和所有者隔离、
PostgreSQL-only、Gateway 不执行 graph、Worker 持租约执行、持久化 stream 和 forward-only schema
基线。

新增能力应表现为：前端流式渲染在长回答中保持稳定；Memory 能识别六类重要信号并在项目作用域内增量
更新；lease 无法确认时执行 fail-stop；SSE 历史缺口可以显式恢复；Browser 工具受 SSRF、权限和运行
租约保护；Delta Checkpoint 可以在明确兼容合同下恢复长会话；Live Browser 的 Agent 和人工操作指向
同一受租约管理的会话。

Docker Compose、Kubernetes/Helm、多节点 Browser Session Service、Firefox、Safari/WebKit 和不同
模型供应商仍需分别记录目标环境验收结果，不能由宿主机 Chromium 或单节点验证自动推导。
