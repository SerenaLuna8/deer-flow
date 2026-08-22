# 上下文、压缩与用量显示深度审查报告

> 审查日期：2026-08-23（Asia/Shanghai）
>
> 审查基线：`d528280f2374`，审查对象为当前未提交工作树
>
> 性质：只读故障审查；本文不是修复实现或配置变更
>
> 注意：未提交工作树在审查期间仍有并行用户变更，因而不是不可变快照。本文记录各项
> 取证当时的源码与运行状态；测试数量是时点证据，不应当作固定基线。

## 1. 结论

管理员策略版本化、普通 Run 的不可变策略快照、Worker 精确物化、手工压缩的两阶段
锁/CAS，以及 full/delta checkpoint replacement 的主干设计是完整的。前后端 API 的
授权和类型边界也基本健全。

但是，当前不能判定“上下文压缩正常工作”或“管理员上下文配置对所有执行路径生效”：

| 编号 | 级别 | 结论                                                                      | 当前状态                                    |
| ---- | ---- | ------------------------------------------------------------------------- | ------------------------------------------- |
| C-01 | P1   | 一个超大的最老完整 turn 会永久阻塞自动、手工、Seal 和 Dream Prepare 压缩  | 当前真实会话正在发生                        |
| C-02 | P1   | Context gauge 和自动 trigger 没有测量真实 provider 请求，现场低估约一半   | 当前真实会话已确认                          |
| C-03 | P1   | Memory Seal / Dream Prepare 没有应用管理员数据库中的 summarization policy | 当前配置已经命中                            |
| C-04 | P1   | `fraction` trigger 使用摘要模型窗口，而不是实际 Lead 模型窗口             | 确定性探针确认；当前策略使用 tokens trigger |
| C-05 | P2   | Usage API 不知道 composer 选中模型或活动 Run 的冻结快照                   | 代码路径确认                                |
| C-06 | P2   | `token_usage.enabled=false` 仍会持久化 Lead/Sub-Agent Token 用量          | 确定性探针确认；当前设置为 true             |
| C-07 | P2   | 同一逻辑 Run 重试/重入时，进程内 Token Budget 可从零重新计数              | 契约探针确认；真实 reclaim 影响未实跑       |
| C-08 | P3   | 管理员更新不主动失效已挂载的 context-usage query                          | 代码风险；未做双标签浏览器复现              |

C-01 与 C-02 已有生产形态的本地实时证据，不是仅从单测推断。

## 2. 审查范围和证据定义

本报告覆盖：

- 平台管理员 `agent_runtime` 中 summarization、token usage、token budget 配置；
- 管理员更新、版本、current pointer、审计和生效范围；
- Run admission policy snapshot、Worker 执行期 overlay 和 Run-local 配置隔离；
- 自动压缩、手工 `/compact`、Dream Prepare、Memory Seal；
- full/delta checkpoint replacement 与 Memory receipt 激活；
- context usage API、前端 gauge、累计 Token 用量显示和 query invalidation；
- Harness、会话、Skill、MCP、Sub-Agent 重构后对真实模型输入和用量归集的影响。

证据分类：

- **已确认事实**：当前源码、当前 PostgreSQL/日志/浏览器只读证据，或本轮可重复
  判错探针直接证明。
- **合理推测**：触发链完整，但没有对业务环境执行到期、重试回收或破坏性配置操作。
- **未验证假设**：缺少真实运行时证据；本文不把它列为已确认 BUG。

严重性口径：P1 表示核心压缩/保护路径失效或管理员配置在关键消费者中不生效；P2
表示可达的语义或治理偏差；P3 表示尚未现场复现的低风险刷新/可观测性问题。

## 3. 模块关系与执行流程

### 3.1 管理员策略到普通 Run

```text
system-admin PUT /admin/system-settings
              |
              v
严格 schema + 模型引用校验 + expected revision/CAS
              |
              v
policy version + current pointer + audit 同事务提交
              |
       +------+--------------------------+
       |                                 |
       v                                 v
新 Gateway 辅助请求                    新 Run admission
读取 current policy                    锁定 version/checksum
       |                                 |
       |                                 v
       |                         run_runtime_policy_snapshot
       |                                 |
       |                                 v
       |                         Worker 精确 materialize snapshot
       |                                 |
       +-----------------------> AppConfig.with_runtime_policy
                                         |
                                         v
                                  Run-local ContextVar
                                  finally 恢复，不跨 Run 泄漏
```

关键位置：

- 管理员路由与权限：`backend/app/gateway/routers/admin_system_settings.py:24-30,130-173`
- policy 模型：`backend/app/system_runtime_settings/models.py:112-155`
- 更新事务：`backend/app/system_runtime_settings/service.py:217-339`
- current/snapshot materializer：`backend/app/system_runtime_settings/materializer.py:94-225`
- Run admission：`backend/app/private_work/snapshot_repository.py:876-904`
- Worker overlay：`backend/app/reliability/run_execution/executor.py:573-582`
- 递归 overlay 与 Run-local 隔离：
  `backend/packages/harness/deerflow/config/app_config.py:583-630,707-739,794-809`

生效语义：

- 新 Gateway 请求读取 current policy，没有应用级长期缓存；
- 新 Run 冻结当时的精确版本；已经 admitted/running 的 Run 继续使用原快照；
- 管理员页面将 `agent_runtime` 标为 `new_requests_and_runs`，与这两个语义一致；
- checkpoint full/delta mode 和 delta snapshot frequency 是进程冻结部署配置，不属于
  在线管理员 policy；相关进程必须保持一致并一起重启。

当前只读数据库证据：

```text
current policy revision/version = 1/1
summarization.enabled            = true
summarization.trigger            = tokens:32000
summarization.keep               = messages:10
trim_tokens_to_summarize         = 15564
Run policy snapshots             = 22，均指向 version 1
```

当前浏览器管理员页面显示的值与数据库一致，且没有表单脏状态或控制台错误。普通 Run
也确实使用该 policy：当前 Worker 日志出现 32K trigger 之后的 compaction 尝试。

### 3.2 自动压缩

```text
Run snapshot policy
  -> 选择 dedicated summary model；否则使用运行时默认模型
  -> checkpoint messages + raw summary 做 token/message 估算
  -> tokens / fraction / messages 任一 trigger reached
  -> 找到从开头连续的完整 turn 边界
  -> 从最大候选向更早候选检查 summary prompt budget
  -> 调摘要模型，校验 SNIP 双段输出
  -> RemoveAll + preserved tail
  -> 写 summary_text + memory_archive_receipt + replacement checkpoint
```

实现：
`backend/packages/harness/deerflow/agents/middlewares/summarization_middleware.py:201-280,297-398,448-479,581-704,820-896`。

完整 turn 约束可防止拆开 AI tool call 与对应 ToolMessage；问题不是这个约束本身，
而是当前没有在“第一个完整 turn 已经超预算”时保证进展的第二条安全路径。

### 3.3 手工压缩

```text
POST /threads/{id}/compact
  -> project/capability/Thread 校验
  -> Thread 行锁，拒绝活动 Run，记录 source checkpoint
  -> 释放事务后调用 summary model
  -> 再加锁并重新授权
  -> source checkpoint CAS
  -> Overwrite(messages) + summary + receipt
  -> full/delta checkpoint replacement
```

关键位置：

- 路由：`backend/app/gateway/routers/private_work.py:1362-1395`
- 两阶段锁和 CAS：`backend/app/private_work/chat_controls.py:218-335`
- 模型物化：`backend/app/private_work/chat_controls.py:381-410`
- replacement：`backend/packages/harness/deerflow/runtime/context_compaction.py:141-255`
- full/delta 统一 whole-state replace：
  `backend/packages/harness/deerflow/runtime/checkpoint_state.py:199-238`
- delta message reducer：`backend/packages/harness/deerflow/agents/thread_state.py:464-536`
- receipt 激活与修复：`backend/app/private_work/checkpointer.py:334-406,440-488,569-679`

模型工作不持有数据库事务；失败不替换 checkpoint；并发写由 Thread 锁和 source
checkpoint CAS 阻止覆盖。这些边界通过聚焦测试。

### 3.4 Context gauge

```text
GET /threads/{id}/context-usage
  -> project/Thread authority
  -> current admin policy
  -> dedicated summary model；否则 Thread Agent 当前默认模型
  -> checkpoint messages + synthetic raw summary 估算
  -> triggers / primary / threshold / remaining
  -> 前端 Zod strict parse
  -> 输入框进度环和详情卡
```

关键位置：

- 后端服务：`backend/app/private_work/chat_controls.py:336-410`
- 测量：`backend/packages/harness/deerflow/runtime/context_compaction.py:96-138`、
  `backend/packages/harness/deerflow/agents/middlewares/summarization_middleware.py:286-398`
- API schema：`backend/app/gateway/private_work_schemas.py:215-265`
- 前端 query：`frontend/src/core/threads/context-usage.ts:7-95`、
  `frontend/src/core/threads/thread-actions.ts:200-225`
- UI：`frontend/src/components/workspace/context-window-indicator.tsx:110-322`

前后端对当前实现的 schema 和 trigger 算法是一致的；C-02 的问题在于这个共同算法
没有测量最后送给 provider 的请求。

### 3.5 累计 Token 用量

Provider callback 和外部 Sub-Agent receipt 进入 Run Journal，按稳定 run/receipt identity
去重；attempt outcome 返回 Lead/Sub-Agent/middleware/by-model 分项；持久化结算在 Job
lease 下把每个 attempt 累加到逻辑 Run。当前开启状态下，浏览器累计用量与数据库 Run
汇总一致，未发现 Sub-Agent 丢计或重复计费。

C-06 是“关闭设置没有传到 Journal”，C-07 是“预算中间件没有恢复逻辑 Run 已结算的
前序 attempt 用量”；二者不是当前开启状态下的去重错误。

### 3.6 Skill/MCP 重构与上下文的交叉边界

Run admission 将 Skill 和 MCP 的 version、payload checksum、catalog generation 与完整
snapshot 写入 Run asset rows：
`backend/app/private_work/snapshot_repository.py:1082-1129`。Worker 按 admitted snapshot
物化一次 `private_runtime`，并把该 Run 的 Skill root 作为只读 mount：
`backend/app/reliability/run_execution/executor.py:704-745`。

Lead Graph 使用精确 runtime Skills 构造 slash activation、Skill tool policy 和 Durable
Context；MCP routing/deferred filter 在模型调用前决定暴露哪些 schema：
`backend/packages/harness/deerflow/agents/lead_agent/agent.py:390-473`。已加载 Skill 引用和
Sub-Agent delegation 结果会在压缩前捕获，并在后续模型请求中作为 Durable Context
恢复。

本轮装配、delegated context、checkpoint replacement 与 usage 去重测试未发现 Skill/MCP
版本错绑、上下文丢失或明确重复计费。这里的主要交叉缺陷是 C-02：最终暴露的工具
schema 和恢复的 Skill/delegation context 会占用真实 provider 输入，却没有进入 Gauge
和自动 trigger 的计量口径。该健康判定针对 audited snapshot；本轮没有新发起真实远程
MCP 调用。

## 4. 已确认问题

### C-01 — P1：超大最老完整 turn 永久阻塞压缩

**触发条件。** 最老的第一个完整 conversation turn 本身大于
`trim_tokens_to_summarize` 所允许的完整 summary prompt。

**根因。**

- 候选必须是从消息开头连续的完整 turn 前缀：
  `summarization_middleware.py:640-653`。
- prompt 超预算直接返回 `None`：同文件 `448-473`。
- 更靠后的候选仍包含同一个最老 turn：同文件 `678-704`。
- 没有对单 turn 的安全拆分、分层摘要、外部化或明确终止策略。

**当前真实证据。**

- 浏览器 Thread `2d57b2a3-3624-49e0-af4f-484b316fbdd7` 显示 estimated
  context 约 48.5K、阈值 32K、progress 100%。
- `logs/worker.log` 在 03:00:09–03:01:51 连续 11 次记录：

  ```text
  Rendered summary prompt exceeds configured token budget; skipping compaction this turn
  ```

- 同一 Run `f6b37437-80c3-4c27-8b3d-864ce3723508` 仍继续进行 16 次 provider
  调用并成功结束，证明不是一次偶发日志，而是每次模型边界重复失败。
- 两次独立 fake-model 探针均为 `compacted=false, model_calls=0`；断言
  `OVERSIZED_FIRST_COMPLETE_TURN_PERMANENTLY_BLOCKS_COMPACTION` 稳定失败。

**影响。**

- 自动压缩无法降低上下文，之后每次模型调用只会重复同一失败；
- 手工压缩使用同一 factory 和候选算法，也无法恢复；
- Dream Prepare/Seal 的 `keep=(messages,0)` 同样失败；
- summary、Memory receipt 和 replacement checkpoint 都不会产生。

**错误可观测性。** 普通 `/compact` 将所有 `acompact_state() is None` 默认报告为
`not_enough_messages`；只有 Dream 的 `keep=(messages,0)` 才改为
`compaction_failed`：`backend/packages/harness/deerflow/runtime/context_compaction.py:199-216`。
前端忽略 reason，仅提示“跳过”：
`frontend/src/components/workspace/use-input-box-commands.ts:220-235`。因此用户主动恢复时
也看不到真实故障。

**现有测试缺口。** 测试覆盖“最大候选超预算时退到更早完整 turn”，但没有覆盖“第一
个完整 turn 自身已经超预算且不存在更早边界”。

### C-02 — P1：Context gauge 与自动 trigger 严重低估真实输入

**代码确认的测量口径缺口。** 当前测量只对 checkpoint messages 加一条 synthetic raw
summary 调用 token counter：`summarization_middleware.py:286-312`。真实模型请求随后还
会加入：

- 静态 system prompt；
- tool schemas，包括当前装配的 Skill/MCP/内置工具；
- Durable Context authority contract；
- bounded summary、delegation ledger 和已加载 Skill context；
- 其他 `wrap_model_call` request shaping。

Durable Context 的后置注入见
`backend/packages/harness/deerflow/agents/middlewares/durable_context_middleware.py:324-346`。
测量使用 raw summary，实际 durable 注入使用 bounded summary，故理论上既可能低估也
可能高估；当前现场是显著低估。

**当前真实证据。** 同一 Thread 在浏览器显示约 48.5K；同一后续 Run 的只读
`run_events` 中，provider `llm.ai.response` input tokens 从 76,818 逐步增长，最后一次
为 100,863。最后一次真实输入约为显示值的 **2.08 倍**，少报约 52.4K。

该 2.08 倍差异是同一会话的现场端到端结果；本轮没有把约 52.4K 差值逐 Token 拆分到
system prompt、tool schema、Durable Context 或 provider 计数细节。已逐项确认的是这些
组成不在 Gauge 的当前计量输入中。

UI 确实使用“Estimated context”措辞；单纯近似不是 P1 的理由。P1 来自：
**tokens/fraction 自动压缩 trigger 复用同一个不完整估算**，因此它也是模型窗口保护
机制，而不仅是展示值。若额外 system/tool/durable 内容足够大，真实 provider 输入可在
trigger 之前先达到窗口。

### C-03 — P1：管理员摘要策略未覆盖两个后台消费者

当前 PostgreSQL 为 `summarization.enabled=true, trigger=32000, keep=10,
trim=15564`；同一 Worker 的基础 `get_app_config()` 却为
`enabled=false, trigger=None, keep=20, trim=4000`。

普通 Run 正确叠加冻结 policy；Memory Seal 和 Dream Prepare 则直接持有 Worker 启动
时的基础配置：

- `backend/app/worker/app.py:104-107,295-310`
- `backend/app/worker/memory_seal.py:157-168`
- `backend/app/worker/memory_dream_prepare.py:223-237`
- `backend/app/private_work/chat_controls.py:317-320,381-410`

确定性装配探针证明 DB overlay 可生成 enabled compactor，但两个 handler 收到和转发的
仍是 disabled base config。

次生影响：

- Seal 把永久 disabled 错误当活动 Run 冲突，成功 `noop`；没有 seal stamp 后，
  Scheduler 用新 ordinal/idempotency 重复准入。
- Dream Prepare 将同一错误标为 `THREAD_BUSY`，随后 retry/dead。

当前业务库没有 Seal storm，仅因为最老 Thread 约 289 分钟，尚未达到 1440 分钟。
完整 Memory 影响见
[`memory-system-deep-audit-2026-08-23.md`](memory-system-deep-audit-2026-08-23.md)。

### C-04 — P1：fraction trigger 使用摘要模型窗口

middleware 首先构造的是摘要模型：
`summarization_middleware.py:847-896`；`measure_context_usage()` 再从该 middleware model
的 profile 取得 `max_input_tokens`：同文件 `309-312`。Run admission/Worker 明明分别
物化 Lead、summary、Memory 等模型：
`backend/app/reliability/run_execution/executor.py:589-689`，但 trigger 没有接收 Lead
窗口。

确定性探针：

```text
summary max_input_tokens = 200000
lead max_input_tokens    = 64000
fraction                 = 0.5
estimated_tokens         = 40000

当前实现 threshold = 100000, reached=false
按 Lead 窗口应为 32000, reached=true
```

摘要窗口大于 Lead 时可能晚于 Lead overflow；小于 Lead 时会无必要地过早压缩。Gauge
返回的 `context_window_tokens` 也会展示错误对象的窗口。当前 policy 使用绝对 tokens
trigger，故当前现场并非由该问题触发，但管理员可以配置 fraction，属于确定可达缺陷。

## 5. 其他配置与用量问题

### C-05 — P2：Gauge 不知道选中模型或活动 Run 快照

- API 只接收 Thread ID：`backend/app/gateway/routers/private_work.py:1398-1422`。
- query key 也只有 project scope + Thread：
  `frontend/src/core/threads/context-usage.ts:72-95`。
- composer 可独立选择下一 Run 的模型：
  `frontend/src/components/workspace/input-box.tsx:2127-2136`。
- 后端 fallback 读取 Thread Agent 当前默认模型，并读取管理员 current policy：
  `backend/app/private_work/chat_controls.py:336-410`。

因此，composer 选中模型不等于 Agent 默认模型时，Gauge 窗口错误；活动旧 Run 使用冻结
policy，而 endpoint 仍展示 current policy。自动 Run 和 UI 可能针对不同模型、不同
阈值计算。

### C-06 — P2：关闭 Token tracking 后 Journal 仍记录

管理员文案是“在任务与子 Agent 中记录并展示输入、输出和总 Token”：
`frontend/src/components/admin/settings/admin-system-settings-page.tsx:195-200`。

实际只有 Lead Agent 的 `TokenUsageMiddleware` 按开关装配：
`backend/packages/harness/deerflow/agents/lead_agent/agent.py:448`。生产 Executor 将
`run_events_config` 固定为 `None`：
`backend/app/reliability/run_execution/executor.py:843-852`；`run_agent()` 对 None 默认
`track_token_usage=true`：
`backend/packages/harness/deerflow/runtime/runs/worker.py:885-899`。Journal 因而继续记录
provider callback 和外部 Sub-Agent receipt，并由 outcome/settlement 持久化。

确定性探针在 policy 为 false 时仍得到一次调用、11 input、7 output、18 total。若这个
开关只想隐藏 UI，当前命名和文案错误；按现有“Track”契约，它没有生效。

### C-07 — P2：逻辑 Run 重试时 Token Budget 可重置

`TokenBudgetMiddleware` 的累计量只存在进程内、按 run_id 的 bounded dict：
`backend/packages/harness/deerflow/agents/middlewares/token_budget_middleware.py:96-116`。
新 graph/attempt 的 `before_agent()` 将 checkpoint 中已有消息标为“已见”，明确不计入
本 Run：同文件 `274-303`。但持久层将多个 retry attempt 视为同一逻辑 Run 并累加真实
成本：`backend/app/private_work/run_repository.py:898-913`。

重建探针：

```text
attempt 1 checkpoint usage = 900
attempt 2 new usage        = 200
logical Run total          = 1100
re-entered middleware sees = 200
hard-stop update           = None
```

“Worker lease 丢失/reclaim 后实际绕过预算”尚未用真实 Worker 实跑，所以生产影响记为
强推断；中间件与持久层对“per-Run”的定义不一致则已确认。压缩会删除原始 usage
message，使仅靠 checkpoint 恢复更加不可靠。

### C-08 — P3：管理员更新后已挂载 Gauge 可能保持旧值

管理员 mutation 只失效 system settings 和 models query：
`frontend/src/core/admin-settings/system/hooks.ts:80-86`；context usage query 设置
`refetchOnWindowFocus=false`：
`frontend/src/core/threads/thread-actions.ts:200-225`。Run、stop、error、compact、Dream
terminal 会触发主要 invalidation，但一个已挂载页面或跨标签页在仅修改管理员设置后
可能继续显示旧阈值。

新挂载页面通常会重新取 current policy。本轮未做双标签修改实测，因此把它记录为
P3 风险，而不是已复现的用户故障。

## 6. 已验证健康项

- 管理员更新有 system-admin 权限、严格 schema、模型引用校验、expected revision、版本
  checksum 和同事务审计。
- 普通 Run admission 与 Worker 执行使用精确 policy/model/Skill/MCP 快照；管理员后续
  修改不会让已 admitted Run 漂移。
- Run-local AppConfig 覆盖会在 finally 恢复，没有跨 Run 泄漏证据。
- 手工压缩具备两阶段锁、重新授权和 source checkpoint CAS。
- full/delta whole-state replacement 会删除只存在于旧状态的 channel；delta reducer
  正确处理 `REMOVE_ALL_MESSAGES`。
- receipt 仅随 durable checkpoint 激活，重复与修复路径有 PostgreSQL 覆盖。
- context usage 后端响应和前端 Zod 均严格；权限来自服务端 project context。
- Run、stop、error、manual compact、Dream terminal 等主流程会失效 context query。
- 当前开启状态下，Journal 对 provider callback 和外部 Sub-Agent receipt 有稳定 identity
  去重；当前真实 Run 的 Lead/Sub-Agent/总量闭合。
- Skill/MCP 在 admission 时冻结版本和 per-Agent snapshot；Task Tool/Sub-Agent 上下文传递
  与生命周期关闭未发现 wiring omission。

这些健康项说明基础机制可运行，不会抵消 C-01 至 C-08 的跨模块缺口。

## 7. 验证结果

环境 readiness：

```text
make check-db
PostgreSQL 17.11，127.0.0.1:9432 healthy
schema_v1 ready，pg_trgm installed
Nginx/Gateway/Frontend health 可达
```

后端 Context 聚焦测试：103 passed。覆盖：

- context usage service/API；
- manual compaction 和 SNIP；
- checkpoint replacement；
- runtime model refs；
- token middleware、Journal budget、terminal contract、Run execution profile。

独立交叉批次：

- checkpoint replacement、token usage、Agent assembly、runtime context、delegated
  context：59 passed；
- context/SNIP/API 的较小聚焦批次：38 passed。

这些批次有重叠，不能相加为独立测试总数。

前端：

- Context/Admin 聚焦：3 files / 21 tests passed；
- 主审查时一次完整 Rstest：163 files / 828 tests passed；独立复核稍后在继续变化的
  dirty worktree 上得到 164 files / 832 tests passed。两次均为 0 failure、0 snapshot
  change；数量漂移不是测试不稳定，而是审查期间工作树未冻结。

浏览器只读验收：

- 管理员 Context and summaries 页面值与数据库一致；Save disabled；console errors 0；
- 真实 Thread Gauge 可打开并显示 48.5K/32K/100%；
- 当前 Memory 页面可读，pending 0，已有 v1 Dream 文档和 archive episode。

绿色测试证明既有编码契约可执行，但现有 suite 没有覆盖：

- oversized first complete turn；
- 真实 provider request 与 Gauge 的差额；
- Lead/summary 异构窗口下的 fraction trigger；
- base config + DB policy + 后台 Memory handler 的生产装配；
- token tracking off 的 Journal/settlement；
- 同一逻辑 Run 跨 Worker attempt 的预算连续性。

## 8. 验证边界

本轮没有：

- 主动发起新的真实外部模型 Run；实时证据来自既有浏览器会话、Worker 日志和
  `run_events`；
- 修改管理员设置、业务数据库或运行配置；
- 等待 1440 分钟触发真实 Seal；
- 执行真实 Worker lease 丢失/reclaim；
- 做多进程 full↔delta 热切换或跨浏览器/双标签刷新测试；
- 验证产品型 Sub-Agent 自身长工具记录是否会达到窗口；它没有安装 summarization，
  但当前无现场 overflow，故只保留为未验证边界；
- 判定“summarization disabled 时 context API 返回全零”一定是 BUG；代码和测试明确
  这样设计，缺少产品规格说明是否仍应显示观察值。

## 9. 建议的修复顺序和回归门槛

本报告不实施修复。建议顺序：

1. 先修 C-01，建立在不破坏完整工具 turn 的前提下一定能取得进展的策略，并让手工
   API 返回真实失败原因。
2. 将“触发保护用量”和“UI 观察用量”定义为最终 provider request 的同一可审计
   计量口径，解决 C-02；明确 system prompt、tool schema、durable context、图片等如何
   预算。
3. 让 fraction trigger 和 Gauge 显式绑定实际 Lead/selected model，解决 C-04、C-05。
4. 统一后台 Seal/Prepare 的 policy materialization，解决 C-03，并与 Memory 报告中的
   防重投契约一起回归。
5. 明确 token tracking 开关是“停止持久化”还是“只隐藏 UI”，再闭合 Journal、
   Sub-Agent receipt、API 和 settlement。
6. 将逻辑 Run 已结算 attempt usage 纳入 Token Budget 的 durable authority，覆盖 Worker
   reclaim 和压缩后恢复。
7. 建立 system settings revision 到 context query 的跨页面失效信号。

最低验收应包含：

- oversized first turn 的自动、手工、Seal、Prepare 四路径；
- 实际 provider request 与 Gauge/trigger 的可解释误差上界；
- Lead/summary/selected model 三者窗口不同的 tokens/fraction 矩阵；
- 新 Run 使用新 revision、旧 Run 保持旧 snapshot、后台消费者采用明确 policy；
- token tracking off 后 Journal、Sub-Agent、Run totals 和 UI 的统一语义；
- 同一 Run 在 full/delta checkpoint、压缩、retry/reclaim 后预算连续且不重复计费。
