# ActWeave 全栈代码深度审查

> 日期：2026-08-24
> 审查对象：`backend/app`、`backend/packages/harness/deerflow`、`frontend`、持久化、Nginx/Compose 与进程装配
> 基线：`d14e31efc3c89c71547f645e8e4681c213235923`（`feat: refine agent runtime controls and workspace UX`）
> 性质：只读代码审查。除本文档外，没有修改业务代码、测试、数据库或外部状态。

## 1. 结论摘要

项目的主架构方向是成立的：Nginx 是浏览器入口，Gateway 负责身份、项目权限、HTTP 准入和 SSE replay，Scheduler 只准入到期任务，Worker 独占 Agent graph 执行，PostgreSQL 持有 Run、Job、快照、事件、配额、审计和治理资产的权威状态。Harness 没有反向依赖 `app.*`，这是一个重要且正确的边界。

本次没有发现 P0，也没有发现前端可以直接绕过 Gateway 权限的证据。发现的主要问题是：

- 3 个 HEAD P1：
  - 首管理员与默认项目分两个事务创建，默认项目失败后 `/initialize` 无法重试。
  - Worker 在 claim 已提交但 handler 尚未启动时退出，会遗弃 lease；触发既包括停止/失去 fleet ownership，也包括生产 `_after_claim_commit` callback 异常，甚至会把明确未执行的 unsafe Job 错判为副作用未知。
  - 显式启用 Local Host Bash approval 后，approved Lead Bash 的后台进程没有 Run 级所有权回收；若命令携带 frozen Skill secret，进程可延长秘密生命周期。代码路径已确认，未做现场进程复现。
- 审查中间态曾出现 1 个 dirty-only TypeScript 编译错误（未定义 `cacheMarker`）；并发修改随后已修复，2026-08-24 03:06 的当前 `pnpm check` 通过，因此它不计入最终现存缺陷。
- 多个 P2 集中在：Project Audit 前后端契约、Channel 启动/暂停语义、断线取消可靠性、Builder Activity 与 Automation 分页、Thread offset authority、SSR Project exact lookup、私有 Skill fail-closed、流式背压、动态 catalog 一致性、Follow-up 请求竞态和部署 readiness。
- 实际的“过度设计”主要是遗留或假想 Adapter、伪 SDK seam、无 I/O 的异步 query、职责混杂的 RunManager；大量安全代码、状态机和事务代码不应仅因体量大而删除。
- 另一个更普遍的问题是“浅而宽/欠模块化”：Shared Asset facade、Gateway composition、`InputBox`、`useThreadStream`、Admin settings 等文件太大，接口面接近实现复杂度，降低了 Locality，但这不等于需要再引入一套通用框架。

建议修复顺序：先处理 3 个 HEAD P1，并同步 T01 恢复 backend release gate；再修复 A01 Audit 契约和 Thread/Builder/Automation 的确定性截断与 cursor authority；随后处理 shutdown/readiness/Channel 生命周期；最后做模块 deepening、协议卫生和遗留 seam 清理。

## 2. 审查方法、证据等级与边界

### 2.1 证据等级

- **已确认事实**：当前源码、契约或测试直接证明。
- **强推断**：完整触发链可从源码确定，但本次没有用真实数据库、浏览器、外部 provider 或模型现场复现。
- **产品语义待确认**：代码行为和规则/用户预期不一致，但现有测试或文案表明它可能是有意设计，不能直接称为 bug。
- **未验证假设**：只有风险线索，不足以列为现存缺陷；本文不会把它写成确定结论。

严重度：

- **P0**：可直接造成大范围越权、秘密暴露、不可恢复数据破坏或系统性不可用。
- **P1**：高影响的数据/执行语义错误、安全生命周期错误或主流程阻断。
- **P2**：确定性功能缺陷、可靠性/资源问题或在现实边界下可触发的错误。
- **P3**：维护性、可观测性、低频边界或长期演进风险。

### 2.2 工作树边界

任务开始时已经有 4 个用户改动：

- `backend/app/private_work/chat_controls.py`
- `backend/tests/test_chat_control_context_usage.py`
- `frontend/src/core/project-governance/audit.ts`
- `frontend/tests/unit/core/project-governance/audit.test.ts`

审查期间又出现了并发修改。2026-08-24 03:01:26 +0800 的中间快照为：

```text
 M backend/app/private_work/chat_controls.py
 M backend/app/private_work/snapshot_repository.py
 M backend/app/shared_assets/models.py
 M backend/app/shared_assets/resolver.py
 M backend/packages/harness/deerflow/agents/middlewares/provider_request_usage.py
 M backend/packages/harness/deerflow/runtime/context_compaction.py
 M backend/tests/test_chat_control_context_usage.py
 M backend/tests/test_context_usage.py
 M backend/tests/test_provider_request_usage.py
 M frontend/src/core/project-governance/audit.ts
 M frontend/src/core/threads/context-usage.ts
 M frontend/src/core/threads/thread-actions.ts
 M frontend/tests/unit/core/project-governance/audit.test.ts
 M frontend/tests/unit/core/threads/context-authority-hook.test.ts
 M frontend/tests/unit/core/threads/context-usage.test.ts
?? backend/tests/test_run_asset_facts.py
```

HEAD 结论均通过 `git show HEAD:<path>` 或未修改文件核对；dirty 文件的当前内容没有被冒充为 HEAD 证据，dirty-only 问题单列在第 8 节。本文没有恢复、暂存、提交或覆盖上述修改。

该工作树持续变化：03:01 快照中的 `cacheMarker` 编译错误在 03:06 前已被并发修改修正，复跑 `pnpm check` 通过。本文对 dirty 状态采用“带时间的观察”，不把已经消失的中间态当成最终缺陷。

### 2.3 本次没有证明的内容

- 没有做真实外部模型、MCP、Slack/GitHub/OIDC provider 或 Sandbox provider 验证。
- 没有完成真实浏览器验收或多浏览器矩阵。
- 没有把静态检查或 mock 测试当作生产部署证书。
- Local Bash daemon、动态 offset 漏项和 Follow-up A/B 竞态有完整代码链，但未做现场运行复现。
- 日志中存在原始 provider 数据和 PII 是事实；“某一次真实 provider 必然把 token 写入日志”不是已确认事实。

## 3. 规模、模块和总依赖图

粗略生产代码体量约 37.5 万行：`backend/app` 约 13.3 万行，Harness 约 11 万行，前端 `core` 约 5.4 万行，应用/组件约 7.7 万行。体量最大的 backend 领域是 Shared Assets、Private Work 和 Gateway；Harness 中是 agents、persistence、runtime；前端中是 threads、shared-assets、private-work 和 Builder。

```text
Browser
  │
  ▼
Nginx :2026
  ├─► Next.js Frontend
  └─► Gateway :8001
        ├─ Auth / CSRF / ProjectContext / Capability
        ├─ Project & System Admin APIs
        ├─ Thread / Run admission
        ├─ durable SSE replay
        └─ PostgreSQL authority
             ├─ Project / Membership / Asset Versions / Secrets
             ├─ Thread / Run / exact Snapshot / Job / Attempt
             ├─ Checkpoint / Store / Stream Events
             ├─ Quota / Audit / Notification
             └─ Automation / Channel instance lease

Scheduler ──due admission──► PostgreSQL Job queue
Channel Provider ─► MessageBus ─► Project inbound ─► Run admission
Worker ──claim/lease──► Job
  └─► exact runtime materialization
       └─► Harness graph / middleware / tools
            ├─ Model Runtime
            ├─ MCP Run session
            ├─ Sandbox / File authority
            └─ Subagent lifecycle

Optional Provisioner :8002 ─► Kubernetes Sandbox control only
```

### 3.1 关键依赖判断

- 正确的单向边界：`backend/app -> backend/packages/harness`；未发现 Harness 反向导入 `app.*`。
- 最强的业务耦合在 `private_work`、`shared_assets`、`projects`：静态导入统计约为 `private_work -> projects` 44、`private_work -> shared_assets` 43、`projects -> private_work` 11、`shared_assets -> private_work` 6。这里存在领域级双向依赖。
- 双向依赖有一部分是真实事务关系：Project membership/suspend 要清理 Private Work，Private Work 又必须事务内重验 Project authority；Run admission 需要 Asset resolver，Skill Builder Activity 又借用 Private Work scope。
- 更好的 seam 不是把它们合并成一个超大领域，而是抽出少量中性 contract：
  - issued Project/Owner context 与事务重验 contract；
  - lifecycle cleanup/admission port；
  - Job lease/outcome contract；
  - Activity feed transport kernel。
- 不要把真实 execution definition resolution 从 Shared Assets 移走，也不要把 Run/Job 原子事务从 Private Work 拆散。

## 4. 核心模块逐项深度分析

### M1. Nginx、Gateway、Auth 与 Composition Root

**职责和接口**

- Nginx 是单一浏览器入口；Gateway 持有 HTTP admission、认证、CSRF、Project/System API、Run admission 与 SSE replay。
- `backend/app/gateway/deps.py:187` 的 `gateway_platform_runtime` 统一装配 persistence、checkpointer、store、Audit、Quota、Private Work、Assets、Models、Automation 和 Channel。
- `AsyncExitStack` 保持基础设施的单一关闭顺序权威，这是合理复杂度。

**依赖和关联**

- 上游：Nginx/Browser。
- 下游：几乎全部 app 领域、PostgreSQL、Harness persistence/checkpointer/store。
- 代码中约有 221 处 `app.state`/动态读取，且 `get_local_provider()` 使用模块级缓存（`gateway/deps.py:647-669`）。

**设计判断**

- Gateway 体量大主要因为它是 Composition Root，不应为了文件短而引入通用 DI 容器。
- 当前问题是动态 `app.state` 接口过宽、模块级 singleton 脱离 FastAPI app 生命周期，错误组合只能在运行时暴露。
- 更简单的方案：让每个 domain builder 返回一个不可变、类型化 runtime bundle，最终汇总为 `GatewayRuntime`；路由通过窄 getter 访问，仍由一个 composition root 管理关闭顺序。

**缺陷和优化**

- B01（P3）、B02、B03、B04、B07、B09、B10、O01、O02。
- `get_local_provider` 的模块级缓存应由 app-instance runtime 拥有，测试/同进程重启时避免保留旧 session factory。
- public path 应使用 exact path 或 delimiter-safe subtree，不应裸 `startswith`。

### M2. Project、Membership、Capability 与 Authority

**职责和接口**

- `ProjectContext` 是项目权限根；Gateway 发放，Mutation/Run admission 在事务中重新解析/重验。
- 管理 Project、Membership、Invitation、Capability、Suspend/Delete/Restore 和默认项目 bootstrap。

**依赖和关联**

- Shared Assets、Private Work、Quota、Audit、Automation、Channel 都依赖 Project authority。
- Project lifecycle 又反向调用 Private Work 的 freeze/revoke/retention，形成领域级双向依赖。

**设计判断**

- issued context、capability 和事务重验不属于过度设计；它阻止 request metadata、浏览器状态和模型输出重新获得权威。
- 可优化的是 seam 位置：将 neutral project lifecycle cleanup port 放到低层 contract，避免 `projects` 直接绑定 Private Work 实现。

**缺陷和优化**

- B02：首管理员/default project 原子性。
- F01：SSR slug lookup 与项目 cursor contract 不一致。
- B06：Project suspend 是否应让 Project Channel 全静默，需要产品决策。
- 前端 capability union 当前重复了 `shared_assets.manage_bindings`（`frontend/src/core/projects/types.ts:8-27`）；Zod 仍可工作，但应由单一契约生成或用 contract fixture 防漂移。

### M3. Private Work：Thread、Run、SSE、Files、Approval

**职责和接口**

- 管理 owner-private Thread、Run admission、Run snapshot、checkpoint、durable SSE、文件、workspace change、execution approval、Chat control 和 Memory 接入。
- `run_admission.py` 把 Project/membership lock、exact Asset/Model/Runtime snapshot、Run、Job、Quota、Audit 放在同一事务中，是项目中最重要的 Deep Module 之一。

**依赖和关联**

- 依赖 Project authority、Shared Asset resolver、System policies、Quota/Audit 和 Job persistence。
- 被 Browser Chat、Automation、Channel inbound、Memory Dream/Seal 调用。

**设计判断**

- 不应把 admission 原子事务拆成多个“微服务式”步骤，也不应恢复从 request metadata 获取 owner/project authority。
- `gateway/routers/private_work.py` 约 2,955 行，是欠模块化：HTTP parse、replay、SSE consumer、files、Run control 混在一个 router。应按 route family 拆文件，保持同一 service contract。

**缺陷和优化**

- B01（P3）：自定义客户端在 POST 人为携带越界 cursor 时，校验晚于 admission；官方 SDK 不走该路径且可按 `Location` reconnect。
- B04：断线 cancel 持久化失败被静默吞掉。
- F02、F08：前端 Thread/Run catalog 与 pagination authority；F06/F07 是 P3 的 signal/schema/seam deepening。
- reconnect state 是否跨 Project navigation 保留属于产品契约冲突，不是已确认越权，见 D02。

### M4. Shared Assets：Agent、Skill、MCP、Builder、Secret

**职责和接口**

- 管理 System/Project Agent、Skill、MCP，immutable Current/Candidate/Historical Version、Activation、binding、resolver、Builder Session/Activity 与 consumer-owned secret generations。
- Run admission 冻结 exact closure，Worker 只消费冻结事实，不重新采用浏览器或最新版本。

**依赖和关联**

- 管理面依赖 Project capability、Quota、Audit。
- 执行面被 Private Work/Worker 调用；Skill Builder Activity 又使用 Private Work context，形成反向耦合。

**设计判断**

- 不可变版本、Activation 分离、recipient-bound encryption、独立 secret generation、exact snapshot 都是必要复杂度。
- `SkillDesignService` 约 4,825 行/93 方法，`AgentDesignService` 约 3,365 行/82 方法，`SkillService`、`McpService` 各约 50 方法；接口面已经接近实现复杂度，是浅而宽的 facade。
- 简化方向不是“通用 Asset DSL”，而是按真实 use case 拆 lifecycle、version activation、design session、activity、file/secret command/query，保留薄兼容 facade。

**缺陷和优化**

- F03：Skill Builder durable Activity 只读首 500 条。
- H01：Private Skill 已严格物化，但子 Agent 二次读取失败会静默丢指令。
- Builder 两套领域不应合并；只共享 decimal cursor、分页、monotonic merge、SSE join 的 Activity transport kernel。
- mutation service 的 Noop quota/audit 默认会隐藏 composition 错误，标准生产构造应强制注入；测试显式注入 Noop。

### M5. Harness Graph、Middleware、Tools、MCP、Sandbox、Subagent

**职责和接口**

- `run_agent()` 组织 lead graph、ordered middleware、tool-call control、model、MCP、Sandbox/File authority、Subagent lifecycle、checkpoint/store 和事件输出。
- 对 app 提供较小的执行接口；内部实现非常深。

**依赖和关联**

- 只依赖 Harness 内部 contract/第三方库，不依赖 `app.*`。
- Worker 先用 app domain materialize exact runtime，再把经过验证的资源交给 Harness。

**设计判断**

- Middleware 顺序、ToolCallControl、checkpoint full/delta、private file finalization、Subagent 真正 quiescence 都有安全语义，不能按代码量删除。
- `RunContext` 和 `_SubagentGraphRunner` 接收过多 `Any | None`/半合法组合；composition root 应生成少量不可变的 resolved resources、graph policy 和 private authority bundle。
- eager `tools/builtins/__init__.py` 造成真实循环导入，迫使 executor/worker 运行时 lazy import；应直接导入 leaf module，将共享 contract 下沉，不要增加新的 service 层。

**缺陷和优化**

- R02：Local Host Bash approval daemon/frozen Skill secret 生命周期。
- H01：子 Agent Skill 读取失败静默降级。
- H02：malformed/非标准最终 state 没有 AIMessage 时，过宽 fallback 仍可标 COMPLETED；生产可达性未验证，按 P3 处理。
- H03：Model streaming 使用无界 Queue。
- H04：Subagent quiescence 高频轮询并可能无限拖住 shutdown。
- H05：usage settlement 的 at-most-once 取舍在 delivery 失败时可能少记；重试必须先具备 receipt 幂等。
- Global MCP discovery 已永久 fail-closed，但仍保留 legacy session pool/cache seam；确认外部兼容承诺后应删除假想 Adapter，把仍使用的 catalog/secret helper 移到中性模块。

### M6. Worker、Job Lease、Run Execution 与 Persistence

**职责和接口**

- Worker 是唯一 graph executor；`claim_next -> mark_running -> heartbeat -> handler -> settlement` 形成持久执行权威。
- PostgreSQL 保存 Job、Attempt、lease token hash、Run、Snapshot、Stream Event、Checkpoint/Store。

**依赖和关联**

- Worker 依赖 Private Work/Shared Assets/System policy materializer 和 Harness executor。
- Scheduler/Gateway 只 admission，不直接执行 graph。

**设计判断**

- lease token、fencing、retry safety 和 heartbeat 是处理崩溃/重复执行的必要复杂度。
- 当前 seam 放置有误：`app/reliability/run_execution/*` 反向导入 `app/worker/service.py` 的 `JobLeaseAuthority/JobOutcome/JobSettlement`。应把这些 contract 移入 reliability/jobs，让 Worker 依赖 domain contract，而不是 domain 依赖 Worker application。

**缺陷和优化**

- R01：claim commit 后、handler start 前退出会遗弃 claim。
- T01：runtime policy 已升级 v5，但部分测试仍固化 v4，使 backend release gate 失败。
- RunManager 混合 execution-local registry、跨 Run finalizing barrier 和可选 RunStore；生产每 Run 新建无 store manager，`wait_for_prior_finalizing()` 实际为空承诺。应明确为 execution-local coordinator；跨进程串行继续由 PostgreSQL authority 持有。
- RunStore `list_by_thread()` 的 memory/store 去重可能 underfill limit；若保留外部 API，应按 cursor 拉到 distinct 数量满足 limit。

### M7. Memory、Context Compaction、Dream 与 Seal

**职责和接口**

- owner-private Memory、上下文 compaction、Dream/Seal admission、archive barrier、Worker handler 和 personalization。

**依赖和关联**

- Scheduler 发现 due work，写 Job；Worker 执行；Private Work/Thread 持有 owner authority 和 activity epoch。

**设计判断**

- Dream/Seal 分离、archive barrier、terminal-failure suppression 有真实防重和恢复语义，不属于过度设计。
- 当前 HEAD 已有 terminal-failure suppression；旧的 disabled-summarization job storm 不能作为当前缺陷重复上报。

**缺陷和优化**

- H04/O01 会影响 Memory Worker 的关闭和运维可见性。
- 当前与 context/provider usage 相关文件在审查期间发生并发 dirty 修改，本报告没有把其未完成状态当作 HEAD 结论。

### M8. Quota、Audit、Notification 与 System Settings

**职责和接口**

- Quota：原子 counter、reserve/consume/release、append-only ledger、HMAC idempotency。
- Audit：typed metadata、actor/target authorization、HMAC refs、project/platform read。
- System settings：Model、Auth、Memory、Quota、Automation、Agent Runtime policy 的 PostgreSQL 权威。

**依赖和关联**

- 几乎所有 mutation domain 依赖 Quota/Audit；Gateway/Worker/Scheduler 读取 runtime policy。

**设计判断**

- Quota row lock、ledger/HMAC、Audit typed metadata 不应简化成普通计数或自由 JSON。
- `OperationalAuditSink` 约 1,479 行/43 方法，存在机械重复；可以 table-driven 化 action/target adapter，但保留 typed metadata contract。
- Quota 对 key rotation 的多个 source ref 逐个查询，可用一次 `IN`/tuple lookup 批量化，不牺牲锁和幂等权威。

**缺陷和优化**

- T01：policy schema v5 与测试 v4 漂移。
- B11：`SystemAssetGovernanceContext` 可直接构造且 repository 主要做 `isinstance`；当前 HTTP adapter 仍重验 admin，未发现现存绕过。应复用 issued System context 或事务内重验 system role。
- B12：生产 mutation constructor 的 Noop quota/audit 默认会让装配错误静默降级。
- A01：HEAD 的前端共享 Audit strict contract 落后于后端；Project Audit 中现有 Skill/MCP/Channel secret 事件即可使整页解析失败，Model secret 事件还会影响复用该 parser 的 Admin Audit。
- 前端/后端 audit contract 仍靠手写同步；应生成安全、删减过的 schema slice 或维护双向 contract fixtures，而不是把内部模型全部暴露给前端。

### M9. Automation 与 Scheduler

**职责和接口**

- Automation definition、occurrence、due admission、Run/Job 创建、reconcile；Scheduler 用 PostgreSQL advisory ownership 保持单所有者。

**依赖和关联**

- 依赖 Project/Asset/Runtime policy/Quota/Audit；执行交给 Worker。

**设计判断**

- occurrence idempotency、单所有者、事务和 Worker 分离都合理。
- Scheduler 应把 ownership verification 作为每轮单一前置门禁，不应散落在各 lane。

**缺陷和优化**

- B05：policy disabled 时 ownership loss 被检测但不退出。
- F04：前端永远只展示首 50 个 task/run。
- H06：Scheduler 没有调用统一 `configure_logging`，与 Gateway/Worker 不一致。
- O01：Compose health 只看 PID，无法证明 Scheduler 持锁或 Worker 可接单。

### M10. Channel、Connection、MessageBus 与 Project Channel Runtime

**职责和接口**

- Provider Adapter、MessageBus、connection identity/group binding、project-owned secret、DB-managed runtime instance lease/fencing、inbound Run admission 和 outbound delivery。

**依赖和关联**

- Provider -> Bus -> Project/Connection authority -> Private Work admission -> Worker -> Bus outbound。
- 当前 Channel domain 直接导入 Gateway CSRF/internal auth，Private Work inbound builder 也内部导入 Gateway；这是 seam 方向错误。

**设计判断**

- 外部 Adapter、connection authority、lease/fencing 是必要复杂度。
- `ChannelManager`/`ChannelService` 同时承担 provider lifecycle、mapping、ack、inbound resolution、Run admission、outbound delivery，Locality 较差。
- Gateway 应在 composition root 注入 internal-request/launcher adapter，Channel/Private Work 不应反向依赖 Gateway 模块。

**缺陷和优化**

- B03：初次 reconcile 串行且没有 timeout，可阻塞 Gateway startup。
- B06：Project suspend 后 adapter/lease 仍在线并发送错误的固定连接提示；Run admission 本身 fail-closed。
- B07：全局 singleton 在 start 异常/取消时可残留半启动对象；stop 的问题是失败 adapter 引用可能被丢弃，不能简单地在所有 stop 异常后清空全局引用。
- B08：per-connection credential cipher 在标准 composition 未接通；在确认产品承诺前不要删除 `channel_credentials`，应接入 Channel-owned secret envelope 或明确移除不可达分支。
- B09：通用 retry/SDK exception 可能把原始 provider response 写入日志。

### M11. Frontend Shell、Auth、Project Scope 与 Query Ownership

**职责和接口**

- Next layouts、AuthProvider、Project slug resolution、ProjectContextProvider、`account UUID + project UUID` client scope、TanStack query ownership 和 static mode。

**依赖和关联**

- 所有 project feature 都在 `ProjectPrivateWorkProvider` 发放的 scope 下使用 API client/query root。
- `transitionPrivateWorkScope` 集中取消和删除 Private Work、Builder、Automation、Governance、Shared Asset roots。

**设计判断**

- scope registry 是必要 Deep Module，不应让每个页面自行 new client、维护 AbortController 或 SSE。
- 当前 root 列表手工集中注册（`scope-registry.ts:157-176`）是可维护性热点。更简单的长期方案是让所有 project query key 都落在统一 `['account', accountId, 'project', projectId]` 根下，再按 domain 分层；迁移前必须保证 reconnect 的显式例外语义。

**缺陷和优化**

- F01：SSR exact slug 只查模糊首 100 项。
- F06（P3）：部分 Thread queries 不透传 TanStack AbortSignal，token usage/branch 等手写响应跳过 strict Zod；metadata adapter 本身已有 strict parse。
- D02：scope dispose 保留 reconnect Run ID/cursor 与当前开发规范冲突，但已有测试保护，需产品决策。
- 未发现以 slug 作为私有 runtime cache authority，也未发现跨 account/project query key 混用。

### M12. Frontend Chat、Assets、Builders、Automation、Admin

**职责和接口**

- Chat/Thread/Run/SSE/message/file/task/approval UI。
- Agent/Skill/MCP version governance 与 Builder。
- Automation、Governance、Connections、Memory、Admin operations/settings。

**设计判断**

- canonical decimal `seq`、strict contracts、Current/Candidate/Activation、imperative secret writes 都合理。
- 欠模块化热点：
  - `use-thread-stream.ts` 约 2,503 行；
  - `input-box.tsx` 约 2,237 行；
  - `shared-assets/api.ts`/`hooks.ts` 各接近 2,000 行；
  - project asset shell/detail 各约 2,000 行；
  - admin system settings 页面约 3,070 行。
- 拆分应按 Run lifecycle、message projection、catalog sync、composer controller、Agent/Skill/MCP kind policy、policy section；不要再造通用页面 DSL。

**缺陷和优化**

- F02：Thread infinite page Symbol 丢失。
- F03：Skill Builder Activity 截断。
- F04：Automation 截断。
- A01：共享 Audit strict contract 落后于后端，单条合法新格式事件会使对应 Project/Admin Audit 整页失败。
- F07/F08：伪 SDK seam 与动态 offset 一致性；当前 caller 有有限 limit，不能把 F07 写成现存无界请求。
- F09：Follow-up A/B race。
- F10：常量 suggestions config 被包装为无 I/O 异步 query。
- F11：首次 Thread history 全量 staging 后才发布，长 Run 延迟首屏。

## 5. P1 详细发现

### B02 — 首管理员与默认项目 bootstrap 非原子

**等级：P1；已确认事实；HEAD。**

证据链：

1. `/initialize` 在 advisory lock 下检查 admin 数量（`backend/app/gateway/routers/auth.py:783-800`）。
2. `create_user()` 经 `backend/app/gateway/auth/repositories/sql.py:76-92` 使用自己的 session 并 commit。
3. 之后才在另一 session 执行 `bootstrap_default_project()`（`auth.py:824-839`、`backend/app/projects/bootstrap.py:52-126`）。
4. Project、Membership、system Skill binding 或 quota 任一步失败会返回 503，但 admin commit 不回滚。
5. 第二次 `/initialize` 会因 admin_count > 0 固定返回 `SYSTEM_ALREADY_INITIALIZED`。
6. Gateway startup 只检查 admin，不修复 default project；数据库已经是 current Schema 时，`make setup-db` 会在 `backend/scripts/setup_postgres.py:573-589` 校验后直接返回，不会进入包含 default-project bootstrap 的完整安装阶段。管理员仍可登录并通过普通 Project API 手动建项目，所以不是账号锁死，但首次安装向导进入不一致状态且不能自修复。

最小方案：密码 hash 放在事务外；拿到初始化 advisory lock 后，用同一个 DB session/transaction 创建 User、Project、Membership、bindings、quota。不要用失败后 compensating delete 代替原子性。

### R01 — Worker 在 claim 后、start 前退出会留下孤儿租约

**等级：P1；已确认事实；HEAD。**

证据链：

1. `backend/app/worker/service.py:295-305` 的 `_claim_next()` 离开 repository transaction 后 claim 已提交。
2. `backend/packages/harness/deerflow/persistence/jobs/sql.py:864-887` 已把 Job 设为 `leased`、增加 `attempt_count`、写 `JobAttemptRow` 和 lease expiry。
3. `_fill_capacity()` 在 claim 返回后再次检查 stop/fleet ownership，若停止直接 `break`（`worker/service.py:447-456`），没有 handler、settlement 或 release。
4. `_claim_next()` 还会在 commit 后调用 `_after_claim_commit`（`worker/service.py:303-305`）；生产 callback 是 deferred Automation terminal reconcile，失败会重新抛出（`backend/app/worker/app.py:184-194,319`），同样发生在 handler start 前且没有 release。
5. lease 到期后，`retry_safety != safe` 的 Job 会被标记 `SIDE_EFFECT_STATE_UNKNOWN` 并 dead（`jobs/sql.py:819-836`）；safe Job 至少等待 lease TTL、可能额外消耗 attempt，`max_attempts=1` 时还可能直接 `ATTEMPTS_EXHAUSTED`（`jobs/sql.py:841-887`）。
6. 现有 `test_stop_during_claim_does_not_start_claimed_handler` 只验证 handler/mark_running 未调用，没有验证 DB claim 被归还。

问题本质：lease-expiry 逻辑用于“Worker 可能已产生副作用”的崩溃场景；这里 Worker 明确知道 handler 从未启动，不应伪装成未知副作用。

最小方案：增加 lease-token guarded 的 `release_unstarted_claim`/`abandon_before_start` 原子操作，把 attempt 记为 `not_started`，重新排队且不消耗业务重试次数；post-claim stop/fleet-loss 和 callback exception 等所有 start 前退出路径必须调用它。

### R02 — Local Host Bash daemon 跨 Run 存活并延长秘密生命周期

**等级：P1（条件性）；代码路径已确认，运行影响为强推断；HEAD。**

适用范围：Local Sandbox 的 `approval_required` Host Bash；默认配置关闭（`backend/packages/harness/deerflow/config/sandbox_config.py:43-49`、`backend/packages/harness/deerflow/sandbox/security.py:132-147`）。主要风险在 Lead Bash，私有 Subagent Bash 已禁用。用户批准了精确命令，但 approval 不解决 spawn 后资源归属。普通 legacy `allow_host_bash=true` 与私有只读 Skill mount 会被 `backend/packages/harness/deerflow/sandbox/local/local_sandbox_provider.py:446-468` 拒绝，因此不能把该 legacy 路径当作 frozen Skill secret 的证据。

证据链：

1. Bash tool 明确指导后台启动长进程（`backend/packages/harness/deerflow/sandbox/tools.py:2108-2119`）。
2. 实际可达的秘密路径是 approval continuation：`backend/packages/harness/deerflow/runtime/host_execution_runner.py:468-502,749-813` 在 executor slot 中重新物化 frozen Skill secret；`:343-407` 随后把该 exact one-command overlay 注入 Local Sandbox、执行并清空 Python 字典。本文不把 GitHub/request-scoped carrier 归入同一风险范围。
3. Local Sandbox 合并环境并传给 `Popen`（`sandbox/local/local_sandbox.py:1279-1317`、`:1417-1425`）。
4. 前台 shell 返回后，仍存活的后台 process group 不会被 kill；只有整条命令 timeout 才 kill group（`:1392-1461`、`:1468-1488`）。
5. Run sandbox release/reset/shutdown 只关闭 file authority，没有 process registry 或 process-group cleanup（`sandbox/local/local_sandbox_provider.py:541-587`）。

因此后台进程可以在 Run 结束后保留其环境副本。清空 Python 字典不会清除已启动子进程环境。

最小方案：Local Sandbox 建立 Run-owned process-group registry；Run release 时 terminate/join。带 secret 的命令禁止无所有者 detach；若需要 daemon，返回显式带租约 receipt，并用最小、无秘密环境重启 daemon。增加进程级测试，证明 Run 结束后 PID 消失且 secret 不再可读。

## 6. P2 详细发现

### B03 — Project Channel 初次 reconcile 串行且无 timeout

**已确认代码结构；真实 SDK 永久挂起为未验证假设。** Gateway 在 lifespan `yield` 前等待 Channel runtime（`gateway/app.py:173`）；runtime 对 enabled instances 串行 reconcile（`project_channels/runtime.py:200-217`），最终无 timeout 等待 `channel.start()`（`channels/service.py:565-607`）。heartbeat repair 反而已有 `asyncio.wait_for`（`project_channels/runtime.py:530`）。单个 provider hang 可阻止 Gateway serving 和后续 instance 启动。应复用 TTL timeout、使用受限并发，并把 provider readiness 与 Gateway core readiness 分开。

### B04 — `on_disconnect=cancel` 持久化失败静默退化为继续执行

**已确认事实。** `_persist_private_disconnect_cancel()` 吞掉 `PrivateWorkError`，没有 retry、durable intent 或 operational signal（`gateway/routers/private_work.py:1391-1414`）。瞬时 DB 错误会让用户选择的 cancel 未持久化，Worker 继续运行。至少需要有界后台 retry 和结构化告警；若契约要求强保证，应使用 durable cancellation intent/outbox。

### B05 — Automation disabled 时 Scheduler 丢锁不退出

**已确认事实；不会造成 split-brain。** policy disabled 会跳过 automation lane（`scheduler/app.py:83-94`）；Dream/Seal 的 `ownership.verify()` 检测到 lost 后被 broad exception 捕获并继续（`:115-138`）。production 总装配两者（`:219-233`）。结果是 stale Scheduler 永久轮询/记录错误并呈现错误健康状态，各 lane 仍 fail-closed。应每轮最前只 verify 一次，失败立即 return。

### B07 — Channel singleton 在启动异常后保留半启动对象

**已确认结构；P2。** `channels/service.py:723-740` 在 `ChannelService.start()` 完成前发布全局 singleton；`start()` 先启动 manager、设置 `_running=True`，之后才做 readiness。异常或取消会留下部分启动对象，后续调用因 singleton 非空直接返回；Gateway 又会捕获 Channel 启动异常并继续 serving（`gateway/app.py:178-205`），所以它不只是进程退出瞬态。需要区分：单个 provider 的常规 start 失败大多被 `_start_channel()` 转为降级启动，这是有意行为；而全局 start 未完成的残留不是。用局部对象完成启动后再 publish，并建模 `starting/running/stopping/failed`。停止时不能机械在 `finally` 丢引用；当前 `ChannelService.stop()` 会吞单个 adapter stop 异常后清 `_channels`，更应保留 cleanup-failed receipt/引用以便重试。

### B09 — OIDC/Channel provider 原始数据和 PII 进入日志

**原始数据/PII 已确认；真实 token 泄漏未确认。** OIDC token endpoint 非 2xx 时把 response body 前 200 字符放进异常（`gateway/auth/oidc.py:205-217`），callback 又记录整个异常（`gateway/routers/auth.py:1081-1095`）。公开 callback 在 provider/enable 校验前记录 `provider/error/error_description`（`:1036-1050`），文本日志下可造成日志伪造/不受控数据进入日志；JSON formatter 会转义换行，是现有缓解。Channel retry 直接格式化 SDK exception（`channels/base.py:80`），Slack response 的字符串包含 response data；password rehash failure 记录用户 email 和 traceback（`gateway/auth/local_provider.py:55-62`）。统一只记录稳定 error code、HTTP status、经校验的 provider id 和 request ref；禁止 provider body、error_description、邮箱明文。

### H01 — 子 Agent 不可变 Skill 读取失败静默降级

**强推断。** Private Skill 在 Run 开始时已严格物化、解析并只读（`backend/app/private_work/private_skill_runtime.py:57-129`），但子 Agent 启动时重新读取 `SKILL.md`，所有异常仅 debug 后继续（`backend/packages/harness/deerflow/subagents/executor.py:847-882`）。Tool allowlist 已按 Skill metadata 生效，可能形成“权限受 Skill 限制，但 Skill 指令缺失”的不一致。private/runtime-read-only Skill 读取失败或空内容应 typed fail closed；仅明确 optional catalog 可降级。

### H03 — Model streaming 使用无界 Queue

**已确认事实。** `harness/deerflow/models/runtime.py:189-275` 创建无 `maxsize` 的 `asyncio.Queue()`；provider producer 可在慢消费者暂停时积累剩余响应。取消清理是正确的，缺的是背压。用 `maxsize=1..16` 的小队列，并测试慢消费、提前停止、deadline/abort。

### H04 — Subagent quiescence 高频轮询且可无限拖住 Worker shutdown

**行为已确认；卡死影响为强推断。** 不可取消底层线程需要真实 completion receipt，这个设计正确；但 receipt 每 2ms 轮询（`subagents/lifecycle.py:418-452`），跨 loop delivery 也轮询（`:1211-1230`），barrier 异常永久重试（`:1283-1314`），`aclose()` 无上限等待（`:1606-1638`）。应以 Event/Future bridge 取代轮询，给外部 Adapter 设置操作级 deadline，并在进程层提供 hard-shutdown watchdog；不能虚假标记 quiescent 或提前释放 DB/MCP/Sandbox。

### O01 — Compose health 是 PID liveness，不是 readiness

**已确认设计缺口。** Worker/Scheduler health 只执行 `kill -0 1`（`docker/docker-compose.yaml:130-155`；dev compose 同样）；Gateway `/health` 静态返回 healthy（`backend/app/gateway/app.py:399-406`）。进程可能活着但 Worker 未注册 capacity、Scheduler 未持 ownership。仓库已有 `backend/app/reliability/process_readiness.py:64-160` 可检查 schema、fresh Worker 和 Scheduler ownership。保留 `/health` 做 liveness，新增低敏 `/ready` 或本地 CLI，Compose/Kubernetes readiness 使用后者。

### F01 — SSR Project capability gate 只查模糊首 100 项

**已确认事实，影响为确定性推断。** `frontend/src/core/projects/server-capability.ts:38-58` 固定 `query=slug&limit=100`，只在首批找 exact slug；backend query 是 display name/slug `%query%` 模糊搜索，排序不优先 exact（`backend/app/projects/repository.py:401-417`）。直接打开受保护页面可能假 404，而客户端 `findProjectBySlug` 会分页成功（`frontend/src/core/projects/api.ts:285-321`）。最佳方案是 Gateway exact-slug endpoint；至少让 SSR resolver 分页并做 cursor progress 防护。

### F02 — Infinite Thread 页把 offset authority 放在数组 Symbol 上

**已确认事实。** `thread-lists.ts:41-101` 把 raw next offset 写入数组 Symbol；Sidecar 过滤使 raw offset 大于可见长度。`mapInfiniteThreadsCache`/`filterInfiniteThreadsCache` 创建新数组而丢 Symbol（`:123-147`），rename/delete/SSE title/upsert 都调用这些 helper。后续从可见长度恢复 offset，会重扫、重复、漏项；删到 49 项还会错误判定终页。页类型应改为 `{items, nextOffset}`，cache updater 只改 `items`。

### F03 — Skill Builder durable Activity 只读首 500 条

**已确认事实；线上数据量未查询。** Skill client 只请求一次且不传 limit（`skill-builder/api.ts:235-247`），hook 从 `0` 读一次再订阅 SSE（`hooks.ts:223-268`）；backend 默认 limit 500（`gateway/routers/project_skill_builder.py:918-939`），没有 per-session 500 行硬限制。稳定 SSE 从 0 最终可补齐，所以不是所有情况下永久丢失；但 REST durable snapshot 确定不完整，弱网/SSE 不可用时历史缺失。复用 Agent Builder 的 bounded decimal-seq paginator，只抽 Activity transport，不合并两种 Builder 业务。

### F04 — Automation 页面只展示首 50 个 task/run

**已确认事实。** 默认 `limit=50, offset=0`（`project-automations/types.ts:149-154`），页面对 task/run 都传空 filter，并只在这 50 项上搜索（`project-automations-page.tsx:179-223`）。Gateway 支持 offset，但 response/UI 没有 total/next/load-more。第 51 个任务和旧 Run 不可见；Thread selector 也只取最近 50。应提供 cursor/next/total 和分页 UI；若产品配额确实 <=50，应把它变成服务端硬契约和 UI 文案。

### F08 — 动态排序 catalog 使用 offset，不能形成一致快照

**强推断。** Thread 客户端 offset 读取见 `frontend/src/core/threads/thread-search-query.ts:78-108`，后端按可变化的 `updated_at DESC` 排序（`backend/app/private_work/thread_repository.py:213-227`）；已经位于后页的 Thread 在分页间更新并前移时，可以确定漏掉旧项。Run catalog 使用 `frontend/src/core/threads/thread-runs.ts:149-214`，后端按 `created_at DESC, run_id DESC`（`backend/app/private_work/run_repository.py:999-1017`）；分页间顶部插入新 Run 会移动边界、造成重复，并让本次读取无法表示一个一致时点的全集，但单次插入不必然漏掉分页开始前的旧 Run。seen-id 只能去重复，不能恢复被移动越过 offset 的项。应改 keyset cursor 或 snapshot upper bound；短期必须把结果定义为 best-effort 并在完成后刷新首屏。

### F09 — Follow-up suggestions 旧请求可清空新请求状态

**强推断。** `frontend/src/components/workspace/input-box.tsx:1749-1835` 每次 effect 只创建局部 AbortController，没有 generation；旧请求 A cleanup 后的 rejection/finally 可以在新请求 B 启动后执行 `setFollowups([])` 和 `loading=false`。抽出 `useFollowupSuggestions`，用递增 generation/controller ref，只有当前请求可写状态，并增加 deferred Promise A/B 测试。

### A01 — 前端共享 Audit 契约落后于后端，合法事件可使整页失败

**已确认事实；HEAD。** HEAD 前端的 `assetOperationSchema` 只覆盖 Agent、部分 Skill/MCP 和 binding 操作，`asset_kind` 只接受 `agent/skill/mcp`，metadata 也只允许 `asset_kind/operation/version_number`（通过 `git show HEAD:frontend/src/core/project-governance/audit.ts` 核对，原文件 `:80-163`）。后端 canonical `AssetAuditMetadata` 已支持 Skill/MCP secret 的 configure/copy/invalidate、Model/Channel secret，以及 `version_id/slot_id/secret_name/generation_id/revision/result/reason/readiness`（`backend/app/audit/models.py:836-906`）。这不是未来接口：生产路径会写入这些事件，例如 `shared_assets/skill_secret_service.py:622-651`、`shared_assets/mcp_secret_service.py:418-447`、`project_channels/service.py:318-335` 和 `system_settings/service.py:500-665`。前三类是 project-scoped，Model secret 是 platform-scoped；Admin Audit 复用同一个 `auditItemSchema`（`frontend/src/core/admin-operations/types.ts:3,223-294`）。

前端对单条 `auditItemSchema` 做 action-specific strict metadata 校验，再把整个 `items` 数组交给 `auditPageSchema` 一次解析（HEAD `audit.ts:453-507`）；`fetchProjectAudit()` 直接返回该整页解析结果（`:517-530`）。因此 Project Audit 中只要出现一条合法 Skill/MCP/Channel secret 事件，整页 query 就会失败；Admin Audit 遇到合法 Model secret 事件也会在共享 parser 处失败，而不只是丢失该行。应保留 strict parsing，但从后端 canonical contract 生成一个明确的 client-safe schema slice，或至少增加双向 fixture：后端每种 canonical audit metadata 都必须被前端 parser 接受，前端多出的 operation 也必须被后端拒绝测试捕获。当前 dirty 修改正在扩展该契约，但仍有 kind matcher 缺口，见 D03。

### T01 — Runtime policy 已升级 v5，backend 测试仍固化 v4

**已确认 release-gate 缺陷。** 生产常量是 v5（`app/system_runtime_settings/validation.py:30-33`），workload profile 支持 `{2,3,4,5}`（`app/private_work/workload_profile.py:24-26`），但 `test_run_workload_profile.py:462-477` 把 5 当未知，`:594` 断言 schema 4；`test_vision_bridge_policy.py:93-103` 也断言 v4。全量 backend tests 因此 3 个失败。这里是测试/版本同步缺陷，不是已证实运行时 crash；Schema/Policy 版本变更应让 canonical contract tests 同步修改。

## 7. P3、设计债务和优化项

### 7.1 确认的 P3

- **B01 POST 人为携带 ahead cursor 时校验晚于 admission**：代码顺序成立：POST 只做 cursor 语法解析（`backend/app/gateway/routers/private_work.py:1211-1218`），随后提交 Run/Job/Quota（`:2021-2050`、`backend/app/private_work/run_admission.py:734-1032`），到 stream generator 首次读页才做 high-watermark 校验（`private_work.py:1331-1337`、`backend/packages/harness/deerflow/runtime/events/store/db.py:555-561`）。但这不应称为“客户端不可见幽灵 Run”：当前 lockfile 固定的 `@langchain/langgraph-sdk` 1.9.27（`frontend/pnpm-lock.yaml:700`）在 create-stream POST 不发送 `Last-Event-ID`，该 header 只用于 GET `joinStream`；POST headers 明确提供重连 `Location`（`private_work.py:1233-1246`），SDK 保存后若 body 失败会从 cursor 0 GET reconnect；产品前端又明确使用 `onDisconnect: "continue"`（`frontend/src/core/threads/stream-events.ts:16-27`），durable Run 继续是有意契约。确认问题只剩非标准客户端人为发送 ahead cursor 时没有在 admission 前收到 4xx，且影响调用者自身 scope/quota，故降为 P3 protocol hygiene。最简单是 POST 忽略/拒绝非零 cursor，历史 replay 只走 GET。
- **B08 Channel credential 不可达分支**：repository 支持 optional cipher，但 Gateway/ChannelService 标准装配都没注入，生产无 `store_credentials()` 调用。先确认产品承诺；接通 Channel-owned secret envelope 或移除不可达能力，不能直接删表。
- **B10 public prefix fail-open 风险**：Auth/CSRF public paths 用裸 `startswith`；当前未发现碰撞路由，但未来 `/health-admin`/`/docs-private` 会误判 public。改 exact/delimiter-safe/route metadata。
- **B11 System Asset context 内部边界偏弱**：公开 dataclass 可任意构造，repository 主要 `isinstance`；当前 HTTP adapter 仍 admin-check，未发现 exploit。复用 issued System context 或事务重验。
- **B12 Noop quota/audit 默认**：标准 composition 大多注入，但构造错误可静默失去治理。生产 mutation constructor 强制依赖，测试显式 Noop。
- **H05 Subagent usage settlement 的 at-most-once 取舍可能少记 usage**：contract 明确是 single-winner/每生命周期一次尝试（`backend/packages/harness/deerflow/subagents/lifecycle.py:219-228`），hook 前就 claim receipt，owner loop/取消/hook 失败后不再投递（`:1234-1280`）。这是显式 at-most-once 取舍，不应简单复位内存布尔量，否则 hook 部分成功时可能重复计费。若业务要求重试，先让 settlement 以 `receipt_id` 持久幂等，或持久化 delivery state，再重试。
- **H02 Subagent 无最终 assistant response 仍可标 COMPLETED**：`backend/packages/harness/deerflow/subagents/executor.py:352-389` 在最终 state 完全没有 AIMessage 时回退到最后任意消息，甚至空 state 也返回 sentinel；普通完成路径在 `:1148-1179` 仍标 `COMPLETED`。标准 ReAct 的 ToolMessage 前通常已有发起 tool call 的 AIMessage，内置 Subagent 又禁用 `ask_clarification`，所以 malformed/非标准 state 的生产可达性未验证，降为 P3。应要求最终 non-tool-call assistant response；未来若真有 Subagent return-direct Adapter，再显式白名单。
- **H06 Scheduler 日志配置不一致**：Gateway/Worker 调 `configure_logging`，Scheduler 没有；应在任何初始化日志前配置并测试。
- **O02 forwarded-header trust 边界不显式**：Nginx 接受并转发客户端 `X-Forwarded-Proto`，CSRF scheme/origin 直接信 forwarded headers。当前部署依赖 Nginx 单入口假设；应由 edge 覆盖而非继承，或在应用中只信认证 proxy。
- **F10 suggestions 常量异步壳**：API 只返回 `{enabled:true}` 却经过全局 TanStack loading。恒开就用常量/prop；若未来由 policy 控制，做真实 project-scoped query。
- **F11 Thread history 首次全量 staging**：初始 Run window 全部读完才首次发布，长 Run 延迟首屏。设置初始 page/message budget 并渐进发布，保留 cursor progress 校验。
- **F06 query cancellation/strict schema 不完整**：HEAD 的 metadata、token-usage、run-detail query 没有逐 query 透传 TanStack signal，违反前端规则；scope-level controller 和独立 query key 已缓解跨 scope 晚写。`useThreadMetadata` 虽在 hook 内 cast，但 adapter 的 `getPrivateThread` 已经过 strict `privateThreadSchema`，不能算 raw response；真正缺 runtime schema 的是 token usage 和 branch（`frontend/src/core/threads/api.ts:178-225`）。统一透传 signal，并只给这些手写 response 补 strict schema。
- **F07 Thread 搜索伪 SDK seam**：公共类型暴露 metadata/sort/select 等 LangGraph SDK 参数，Project adapter/Gateway 实际只支持 limit/offset并静默忽略其余参数。当前两个生产 caller 都显式给了有限 limit，所以没有证据证明现存无界请求；风险是未来 caller 误信 seam。收窄为真实 `ProjectThreadCatalog.listPage`，统一进度边界。
- **前端 capability 重复 literal**：`shared_assets.manage_bindings` 重复，当前不破坏运行，但显示手写契约易漂移。
- **HEAD audit action 漂移**：前端 HEAD 的 strict enum 接受 `mcp.approve`（`frontend/src/core/project-governance/audit.ts:104`），后端 `AssetAuditMetadata.operation` canonical union 不包含它（`backend/app/audit/models.py:838-886`）。当前生产后端不会发该 action，因此主要是契约卫生/未来兼容风险；应删除或用 contract fixture 证明其 legacy 来源。

### 7.2 真正的过度设计/假想 seam

| 位置 | 问题 | 更简单方案 |
| --- | --- | --- |
| Harness Global MCP | global discovery 已永久 fail-closed，但 legacy `get_mcp_tools`、stdio session pool、空 cache tombstone 仍存在 | 确认外部兼容后删除 global Adapter；保留的 catalog/secret helper 移到中性模块 |
| `RunManager` | execution-local registry、跨 Run barrier、可选持久历史三种职责混合；生产每 Run 新建导致 prior barrier 空操作 | 明确为 execution-local coordinator；跨 Run 权威交给 PostgreSQL；外部 store 另设窄接口 |
| Thread SDK Adapter | 暴露后端实际不支持的 SDK 参数并静默忽略 | 改为真实 Project Thread catalog contract |
| suggestions config | 无 I/O 却做 async query/cache/loading | 常量；或实现真实 policy query |
| per-connection credential | 代码和表存在但标准 composition 不可达 | 接通真实 consumer-owned secret lifecycle，或明确删除未承诺能力 |
| eager builtins barrel | 为导出便利引入循环依赖和 lazy-import 补丁 | 调用方直接导入 leaf module，共享 contract 下沉 |

### 7.3 欠模块化，不是过度设计

- Gateway composition root、Private Work router、Shared Asset facade。
- Harness `runtime/runs/worker.py`、`RunContext`、Subagent runner。
- Frontend `InputBox`、`useThreadStream`、Shared Asset API/hooks、Admin settings、Asset shell/detail。

原则：按真实 use case 和 lifecycle 拆分，提高 Locality；不要再添加通用 Repository/Service/Port/DSL，除非至少存在两个真实 Adapter 或明确的进程/权限边界。

### 7.4 明确不应简化掉的复杂度

- issued `PrivateWorkContext` 与事务重验。
- Run admission 中 Project lock、exact Snapshot、Run/Job、Quota、Audit 单事务。
- immutable Asset Version、Candidate/Current/Activation、Run pinning。
- consumer-owned secret generations、recipient-bound encryption 和 tombstone。
- Job lease token/fencing、Worker-only graph execution。
- durable stream commit-before-notify、global monotonic decimal cursor。
- ToolCallControl、middleware order、checkpoint full/delta/rollback。
- Subagent 真实 completion receipt/quiescence。
- Quota counter + append-only ledger + HMAC idempotency。
- Audit typed metadata、actor/target authorization、HMAC refs。
- Scheduler advisory ownership、Automation occurrence idempotency。
- Memory Dream/Seal archive barrier 与 terminal-failure suppression。

## 8. 产品语义待确认与 dirty-only 问题

### D02 — scope dispose 保留 reconnect state

**产品语义待确认，不是已确认越权。** reconnect key 包含 account/project/thread，没有跨 scope key 冲突；Run ID/cursor 保存在 `sessionStorage`。`scope-registry.dispose()` 不调用已有的 `clearProjectReconnectStorage`，并有单测明确保护“离开 scope 后仍可恢复”。这与 `frontend/AGENTS.md:69,86-88` 的“不保留 runtime authority/transition 删除 reconnect state”冲突。

二选一：

1. 若跨导航恢复是产品要求：把它写成规范例外，并加 TTL、数量上限、logout/terminal 清理和 stale reconnect 测试。
2. 若退出 scope 就是所有权边界：transition 清 reconnect state，并修改当前测试。

### B06 — Project suspend 是否应停止 Channel 外部副作用

**产品语义待确认，不是已确认授权绕过。** suspend 会 freeze private work、阻止新 Run、revoke active Run authority；Project Channel list/claim/renew/side-effect authorization 不检查 `ProjectRow.is_suspended`，所以 adapter/lease 继续在线。Run resolver 会 fail-closed，但 Manager 会把错误转为“Connect this channel...”并发回外部。现有文案只承诺冻结 private work，没有承诺 Channel 全静默；测试还保护普通 connection 保持 connected。

如果 platform suspend 的产品含义是“停止全部外部副作用”，应把 active/not-suspended 纳入 Channel instance list/claim/renew/authorize 并主动 reconcile；否则至少修正误导性回复并在文案中明确 Channel 仍在线。

### D01 — 审查中间态曾出现 TypeScript 编译失败，现已消失

**历史观察，不计入最终现存缺陷；不属于 HEAD。** 03:01 左右的 `frontend/src/core/threads/thread-actions.ts:176-178` 曾在 `useThreadMetadata` retry 中使用未定义的 `cacheMarker`；它只在后面的 context-usage hook 内定义，retry 逻辑被放进了错误 query。

当时验证：

```text
src/core/threads/thread-actions.ts(177,58): error TS2304:
Cannot find name 'cacheMarker'.
```

03:06 的文件已把 retry 放回 `useThreadContextUsage`，`pnpm check` 通过。保留此记录只是为了说明测试快照为何前后不同，不要求再次修复。

### D03 — 当前 dirty audit contract 漂移

**P3；工作树未完成状态，不属于 HEAD。** 当前 dirty 修改正在修复 A01，把 `asset_kind` 扩展到 model/channel 并补齐 secret metadata；focused tests 已覆盖合法 configuration-secret lifecycle rows。但 kind matcher 仍只把 agent/skill/mcp 视为需要 domain 匹配，因此错误的 model/channel operation-kind 组合不会被拒绝。由于文件在任务开始前已 dirty，本报告只记录这个新增校验缺口，不判断用户修改的最终意图。`mcp.approve` 则已经存在于 HEAD，已移到第 7.1 节作为独立 HEAD P3。

## 9. 验证结果

| 验证 | 结果 | 边界 |
| --- | --- | --- |
| `cd backend && make lint` | 通过 | 运行于审查早期工作树快照 |
| `cd backend && make test` | 3724 项：3721 通过、3 失败、0 skip | 失败均为 runtime policy v5 与旧 v4 测试漂移；不能当成 clean release gate |
| `pytest -q tests/test_private_work_stream_router.py tests/test_scheduler_app.py` | 23 通过、1 skip | 现有 focused tests 通过，但未覆盖 POST ahead-of-watermark、disabled-policy ownership loss |
| Worker post-claim stop focused test | 1 通过 | 只证明 handler 未启动；现有断言不检查 claim 是否归还 |
| `cd frontend && pnpm check`（早期） | 通过 | 在后续并发 Thread dirty 修改出现前 |
| `cd frontend && pnpm test`（早期） | 183 files、930 tests 全部通过 | 在后续并发 dirty 修改出现前；不是浏览器验收 |
| 前端 6 个 focused suites | 28 tests 通过 | 覆盖 scope、module contract、Builders、Automation hooks、capability routes；没有覆盖本文的 >500、第二页 exact slug 等反例 |
| Project Audit focused suite（当前 dirty） | 1 file、7 tests 通过 | 证明进行中的修改接受 configuration-secret lifecycle rows；不能反向证明 HEAD 无 A01，也尚未覆盖 D03 的 model/channel kind mismatch |
| `cd frontend && pnpm check`（03:06 当前态） | 通过 | 并发 dirty 修改已消除先前 `cacheMarker` TS2304；仍不等于 HEAD clean-room 验证 |
| `python3 scripts/detect_thread_boundaries.py --min-severity FAIL` | 无 findings | 静态边界脚本，不证明运行时行为 |

静态复杂度探查发现大量高复杂度函数，但该规则不是仓库现有 gate，因此本文没有把告警数量当作失败证据。代码行数和圈复杂度只用于定位浅模块，最终判断仍基于真实职责、接口和依赖。

## 10. 修复路线图

### 第一批：阻断高影响错误

1. B02：首管理员/default project 同事务 bootstrap。
2. R01：未启动 claim 的 token-guarded release/requeue。
3. R02：Local Bash Run-owned process registry/daemon lease。
4. T01：同步 runtime policy v5 contract tests，恢复 backend release gate。

### 第二批：确定性截断、cursor 与状态竞态

1. F02：结构化 Thread page `{items,nextOffset}`。
2. F03：Skill Builder complete REST replay + final cursor SSE。
3. F04：Automation task/run 真分页。
4. F01：Project exact slug endpoint/resolver。
5. A01：由后端 canonical Audit contract 生成 client-safe schema/双向 fixture。
6. F08：keyset/snapshot catalog 一致性。
7. F09：Follow-up request generation。
8. H01/H03：私有 Skill fail-closed 和 model backpressure。

### 第三批：生命周期和运维正确性

1. B03/B07：Channel startup timeout、受限并发、singleton publish/cleanup。
2. B04：durable disconnect cancellation intent。
3. B05/H06/O01：Scheduler ownership gate、统一日志、真正 readiness。
4. H04/H05：event-based quiescence、shutdown watchdog、usage settlement receipt。
5. 明确 D02/B06 两个产品语义并同步规则、测试和 UI 文案。

### 第四批：模块 deepening

1. Shared Asset services 按 use case/lifecycle 拆分。
2. 收窄 Thread adapter，删除 Global MCP/无 I/O suggestion 等假 seam。
3. 修正 Reliability -> Worker、Channel/Private Work -> Gateway 的依赖方向。
4. `GatewayRuntime` typed bundle 取代散落动态 `app.state`。
5. Frontend 按 controller/lifecycle 拆 `InputBox`、`useThreadStream`、Asset/Admin 超大文件。

## 11. 准确性二次复核

本文完成后按“是否把保护机制误报为复杂、是否把强推断写成现场事实、是否忽略缓解”重新检查了一遍，得到以下校正：

1. **Channel suspend 降级为产品语义缺口。** Run admission 是 fail-closed，没有证据证明越权执行；确认的问题是 adapter/lease 继续存在并可能发送错误回复。
2. **Scheduler 丢锁不是 split-brain。** ownership verify 会 fail-closed；问题是 disabled policy 下检测后不退出、日志风暴和错误健康状态。
3. **Skill Builder 不是所有情况下永久丢 Activity。** REST 确定截断，稳定 SSE 从 0 最终可追平；缺陷是 durable replay 契约和弱网完整性。
4. **Local Host Bash 风险有条件。** 默认关闭，frozen Skill secret 的确认路径是 Local `approval_required` Lead Bash；legacy allow 与私有只读 Skill mount 会被拒绝。代码确认 approved daemon 不归 Run 回收，秘密实际被后续进程读取未现场复现。
5. **Provider 日志不宣称已证实 token 泄漏。** 已确认的是原始 response/error description/PII 进入日志，真实 secret 内容取决于 provider。
6. **Reconnect 保留是有测试保护的意图。** 它和当前工程规则冲突，但不能在产品决策前称为越权。
7. **动态 offset 与 Follow-up A/B race 是强推断。** 时序可由代码构造，但本次无真实数据库/browser trace。
8. **Job lease、Asset snapshot、secret generation、Quota/Audit、checkpoint、quiescence 不列为过度设计。** 它们隐藏了调用方不应重复实现的权限、事务、重放和资源安全不变量。
9. **大文件不自动等于过度设计。** Gateway composition 大但位置正确；Shared Asset/前端巨型模块的问题是接口宽和 Locality 差，应按真实 use case 拆分，不应引入更多假抽象。
10. **没有把历史缺陷套到当前 HEAD。** 旧 Memory Seal job storm 在当前代码已有 terminal-failure suppression，不列为现存问题。
11. **Usage settlement 是 at-most-once 设计取舍。** 已确认失败可能少记，但没有建议在非幂等 hook 上盲目重试；只有持久 receipt/delivery 幂等后才能安全补偿。
12. **POST ahead cursor 不是官方客户端主路径。** SDK 新建 Run 的 POST 不发 `Last-Event-ID`，并会保存 response `Location` 后 GET reconnect；因此删除“幽灵/不可见/P1”表述，只保留自定义客户端协议校验过晚的 P3。
13. **Project Audit 是 HEAD 契约故障，不是 dirty 修改造成。** 结论通过 `git show HEAD` 核对；当前 dirty 文件和 7 个 focused tests 正在补齐合法 secret lifecycle rows，但不能抹去 HEAD 缺陷，也尚未覆盖 model/channel 的 operation-kind 反例。

综合判断：本报告对 HEAD 的静态事实置信度高；3 个 P1 都有完整代码链。需要 live PostgreSQL/多 Worker/真实 provider/浏览器才能把部分 P2 强推断升级为运行时复现结论。修复后应为每个 P1 增加跨事务或进程级回归测试，而不是只补 mock 单测。
