# Memory 与 Dream 深度审查报告

> 审查日期：2026-08-23（Asia/Shanghai）
>
> 审查基线：`d528280f2374`，审查对象为当前未提交工作树
>
> 性质：只读故障审查；本文不是修复实现、ADR 或数据迁移方案
>
> 注意：未提交工作树在审查期间仍有并行用户变更，因而不是不可变快照。本文记录各项
> 取证当时的源码与运行状态；测试数量是时点证据，不应当作固定基线。
>
> **复验更新（2026-08-23）：** 第 1 至第 8 节保留原始审查快照，不能再作为当前
> 缺陷状态使用。修复后的代码、测试与运行时复验见第 9 节；真实外部模型浏览器验收
> 尚未执行，因而本文还没有宣称端到端验收完成。

## 1. 结论

Memory 的核心数据模型、Run 快照注入、SNIP receipt 事务激活以及直接
Dream 的冻结、执行、发布状态机有较完整的权限、幂等和并发保护；当前业务库也有
一次真实成功的自动 Dream 记录。但是，整个端到端功能**不能判定为正常**：

| 编号        | 级别 | 结论                                                                           | 原审查时状态                           |
| ----------- | ---- | ------------------------------------------------------------------------------ | -------------------------------------- |
| M-01        | P1   | Memory Seal 与会话 Dream Prepare 没有应用 PostgreSQL 中的 `agent_runtime` 策略 | 当前配置已经命中                       |
| M-02        | P1   | 永久不可执行的 Seal 被结算成成功 `noop`，随后可被新 ordinal 重复准入           | 缺陷可稳定复现；当前尚未到闲置时间门槛 |
| M-03        | P2   | Dream 模型不可用被伪装为 `nothing_pending`                                     | 可达且可稳定复现；当前默认模型可用     |
| M-04 / C-01 | P1   | 超大首个完整 turn 可永久阻塞 SNIP，进而阻断 receipt、Seal 和 Dream Prepare     | 已在当前真实会话发生                   |

因此应分别理解以下判定：

- **直接 Dream 主状态机**：当前证据健康。
- **新 Run 的 Memory 快照与低权限注入**：当前证据健康。
- **会话 `/Dream` Prepare**：当前配置下不能正常完成。
- **空闲 Memory Seal**：当前尚未触发，但到期后具备失败和重复续投的完整条件。
- **由会话压缩产生 SNIP Memory history**：当前至少有一个真实会话被压缩故障阻断。

## 2. 审查范围和证据分层

本报告覆盖：

- Memory 文档、版本、history、episode 和 reset/restore；
- Run admission 时的 Memory 快照、Worker 权限重验和动态注入；
- `remember_memory`、`recall_memory`；
- 自动/手工 SNIP 压缩产生 Memory archive receipt 的链路；
- Scheduler Dream、手工 Dream、会话 Dream Prepare、idle Memory Seal；
- Dream 模型冻结、受限工具、重试、取消、死信、发布和审计。

证据按以下含义使用：

- **已确认事实**：当前源码、当前 PostgreSQL 只读查询、当前日志、浏览器实际页面，或本轮确定性隔离探针直接证明。
- **合理推测**：已有完整触发链，但没有等待生产时间门槛或执行破坏性管理员操作。
- **未验证假设**：需要新的真实模型调用、真实 Scheduler 到期或业务数据写入才能证明；本文明确列在验证边界中。

严重性口径：P1 表示核心路径被阻断、存在无界后台放大或可能持续累积上下文；P2
表示可达的错误结果或可观测性失真，但当前没有核心状态机损坏证据。

历史上出现过的 Memory Seal Job storm 只用于提出检查假设；本报告的 M-01、M-02
均已按当前源码、当前配置和本轮探针重新验证，没有把历史状态冒充成当前状态。

## 3. 模块关系

```text
PostgreSQL system runtime policy + account preference + current Memory document
                              |
                              v
Run admission ----> run_memory_context_snapshot ----> Run/Job/lease snapshot
     |                                                   |
     |                                                   v
     |                                      PrivateRunMemoryAuthority
     |                                      每次模型边界重新验证权限
     |                                                   |
     |                                                   v
     |                                      DynamicContextMiddleware
     |                                      隐藏 HumanMessage、低权限数据
     |                                                   |
     |                         +-------------------------+------------------+
     |                         |                                            |
     |                         v                                            v
     |                  remember_memory                              recall_memory
     |                  写 pending history                          查 consumed episode
     |
     v
Summarization / manual compact / idle seal / Dream Prepare
     |
     v
SNIP dual output: continuity summary + tagged receipt
     |
     v
checkpoint 持久化事务 ----> activate_history(receipt) ----> pending history
                                                          |
                         Scheduler / manual Dream --------+
                                                          v
                                      pending -> processing (最老 20 条)
                                                          |
                                                          v
                                      frozen policy/model/secret generation
                                                          |
                                                          v
                                      restricted Dream model + 两个草稿工具
                                                          |
                          +-------------------------------+----------------+
                          |                                                |
                          v                                                v
                     成功发布                                           失败/取消
       document version + episode + consumed                 retry 或 processing -> pending
```

主要实现位置：

- Run Memory 快照：`backend/app/private_work/snapshot_repository.py:301-458,1037-1045`
- Worker Run 策略物化：`backend/app/reliability/run_execution/executor.py:569-582`
- Run Memory authority：`backend/app/private_work/memory_authority.py:161-447`
- 动态低权限注入：`backend/packages/harness/deerflow/agents/middlewares/dynamic_context_middleware.py:236-354`
- checkpoint receipt 激活：`backend/app/private_work/checkpointer.py:393-406,440-483`
- receipt 幂等与偏好版本核对：`backend/packages/harness/deerflow/persistence/private_work/memory_repository_parts.py:116-222`
- Dream admission：`backend/app/private_work/memory_dream_service.py:88-218`
- Dream Worker：`backend/app/worker/memory_dream.py:272-598`
- Dream 持久化状态机：`backend/packages/harness/deerflow/persistence/private_work/memory_dream_store.py:379-1030`
- idle seal：`backend/app/private_work/memory_seal_service.py:90-343`、`backend/app/worker/memory_seal.py:133-250`

## 4. 执行流程审查

### 4.1 新 Run 的 Memory 注入

1. Run admission 在同一授权边界内锁定当前 `agent_runtime` policy、账号 Memory
   偏好和当前 Memory 文档，并写入不可变 Run Memory snapshot。
2. Worker 读取该 Run 的冻结 policy，而不是读取执行时最新设置。
3. `PrivateRunMemoryAuthority` 在模型调用边界重新验证 Project、Membership、Thread、
   Run、Job 和 lease；失去权限时 fail closed。
4. `DynamicContextMiddleware` 将 Memory 作为隐藏 HumanMessage 注入，并明确标为
   user-private data，而不是系统指令。
5. 生产 Sub-Agent 不继承父 Run 的 Memory authority；Memory 工具仅在具备 authority
   时注册。

结论：范围归属来自服务端 authority，模型不能指定 project/owner/namespace；该链路未
发现可复现的越权或错绑问题。

### 4.2 history 的两种来源

`remember_memory`：

- 写入 owner-private pending history；
- 每 Run 最多 5 条，每个 scope 最多 200 条 pending；
- source digest 提供幂等保护；达到上限后返回 `backlog_full`。

SNIP archive receipt：

1. summarization 输出 continuity summary 和 tagged facts；
2. 中间件把 tagged facts 封装成带 scope、source checkpoint、模型和 digest 的 receipt；
3. 只有 replacement checkpoint 真正持久化时，`ProjectScopedCheckpointer` 才在同一
   事务中激活 history；
4. 重复 receipt、回滚后的修复、reset 后的 stale receipt 均有 PostgreSQL 回归覆盖。

结论：receipt 没有在“模型已经返回但 checkpoint 尚未提交”的时间窗提前生效，事务
边界合理。M-04 会在 receipt 生成之前阻断这条链，但不是 receipt 激活本身的缺陷。

### 4.3 Dream admission、执行和发布

1. Scheduler 或手工 API 锁定当前 policy、账号偏好、文档和模型；选择最老 20 条
   pending history。
2. admission 把 history 改为 processing，绑定 `active_dream_job_id`，冻结 policy
   revision、模型 payload/checksum、secret generation/digest、文档基线和 history digest。
3. Worker 重新验证权限和所有冻结版本；retry 不重新绑定“此刻的默认模型”。
4. Dream runner 仅暴露 `read_memory_document` 和 `replace_memory_document`，不拥有
   文件、Skill、MCP 或普通 Agent 工具。
5. 输出必须整体替换文档，并通过固定 sections、字符数和 token 预算校验。
6. 成功事务同时产生 current document/version 和 episode，将 processing history 标为
   consumed，并清理 active job；失败重试同一冻结批次，dead/cancel 时 history 回到
   pending。

结论：直接 Dream 的冻结与发布状态机在代码、测试和当前一条真实成功记录中一致。

### 4.4 会话 Dream Prepare 与 idle Seal

两者都复用手工压缩 barrier，以 `keep=(messages, 0)` 连续排空完整 turn，然后验证
checkpoint head 和活动 Run，再进入 Dream admission 或写 `memory_sealed_at`。这个复用
本来避免了第二套压缩语义，但生产装配把错误的基础配置传给了两个后台消费者，形成
M-01；错误又被冲突映射掩盖，形成 M-02。

## 5. 发现的问题

### M-01 — P1：后台 Memory 压缩没有应用数据库运行时策略

**事实。** 当前数据库策略为：

```text
revision=1, version=1
memory.enabled=true
memory.idle_seal_minutes=1440
summarization.enabled=true
summarization.trigger=[tokens:32000]
summarization.trim_tokens_to_summarize=15564
```

同一进程通过 `get_app_config()` 读取的基础配置则为：

```text
memory.enabled=true
memory.idle_seal_minutes=1440
summarization.enabled=false
summarization.model_name=null
```

**根因。**

- Worker 启动只读取一次基础 `AppConfig`：`backend/app/worker/app.py:104-107`。
- `MemoryDreamPrepareJobHandler` 和 `MemorySealJobHandler` 接收该原始对象：
  `backend/app/worker/app.py:295-310`。
- 两个 handler 原样转发给 compaction barrier：
  `backend/app/worker/memory_dream_prepare.py:223-229`、
  `backend/app/worker/memory_seal.py:157-163`。
- `ProjectChatControlService._materialize_compaction_config()` 只解析模型，不叠加当前
  `agent_runtime` policy：`backend/app/private_work/chat_controls.py:381-410`。
- 普通 Run 的正确对照是读取 admission snapshot 后调用
  `with_runtime_policy()`：`backend/app/reliability/run_execution/executor.py:573-582`。

确定性探针：

```text
base_summarization_enabled=False
db_policy_summarization_enabled=True
overlaid_summarization_enabled=True
base_compactor_materialized=False
seal_receives_raw_worker_config=True
prepare_receives_raw_worker_config=True
```

**影响。**

- 会话 Dream Prepare 将“摘要关闭”映射为 `MEMORY_DREAM_PREPARE_THREAD_BUSY`，随后
  retry/dead；用户看到的“线程忙”不是根因。
- idle Seal 无法生成 SNIP history，也不能写 seal stamp。
- 管理员页面显示摘要已启用，但该配置对这两个后台消费者不生效。

**测试缺口。** 现有 Seal PostgreSQL 测试直接构造
`summarization.enabled=true` 的配置；Dream Prepare 测试注入替身配置，没有覆盖
“基础配置 + 数据库策略 + 生产 Worker 装配”。

### M-02 — P1：永久配置错误被成功 `noop`，可重复准入 Seal

当前代码的完整链路为：

1. disabled compactor 抛 `ContextCompactionDisabled`：
   `backend/packages/harness/deerflow/runtime/context_compaction.py:70-77`。
2. Chat Control 无差别改写为 `PrivateWorkConflict`：
   `backend/app/private_work/chat_controls.py:317-320`。
3. Seal 把所有该冲突解释为“活动 Run 抢占”，返回成功 `noop`：
   `backend/app/worker/memory_seal.py:166-168`。
4. `noop` 仍 `settle_success`，但仅 `sealed` 会写 `memory_sealed_at`：
   `backend/app/worker/memory_seal.py:202-250`。
5. Scheduler 只检查数据库中的 Memory/idle 设置，不检查 compactor 是否可执行：
   `backend/app/private_work/memory_seal_service.py:90-103`。
6. 下一轮按历史 Job 数量生成新的 ordinal/idempotency key：
   `backend/app/private_work/memory_seal_service.py:246-277`。

本轮隔离探针重复两次均得到：

```text
permanent_failure_settles=succeeded
audit_disposition=noop
seal_stamp_written=false
next_poll_re_admits=true
```

当前业务库并未处于 storm：`memory_seal` Job 为 0、due candidate 为 0；8 个未删除
Thread 中最老约 289 分钟，尚未达到 1440 分钟。这里的正确表述是：**生产触发尚未
发生，但当前代码和当前配置已经具备到期后重复续投的完整条件。**

### M-03 — P2：模型不可用被错误报告为没有 pending Memory

**根因。**

- `_platform_runtime()` 解析不到 active/default Dream model 时返回 `None`：
  `backend/app/private_work/memory_dream_service.py:104-137`。
- 手工和定时 admission 都将该结果转换为
  `nothing_pending/history_count=0`：同文件 `139-155,265-295`。
- 管理员可以合法挂起默认模型，此时 `default_model_config_id` 被清空：
  `backend/app/system_settings/service.py:654-684`。
- 当 `memory.model_name=null` 时，没有显式模型引用进入 policy 更新校验：
  `backend/app/system_runtime_settings/service.py:121-134,247-278`。

确定性探针在 `pending_rows=3, active_model=null` 时仍返回：

```text
disposition=nothing_pending
history_count=0
```

**影响。** UI 会告诉用户“没有等待整理的记忆”，实际 backlog 保留；自动 Dream
静默跳过且不产生失败 Job/失败审计。若积压达到 200，后续 remember 将被拒绝。

当前默认模型可用，故这是已确认的可达错误分支，而不是当前正在发生的业务故障。

### M-04 / C-01 — P1：超大首个完整 turn 阻断 SNIP 到 Memory 的输入

当前真实 Thread `2d57b2a3-3624-49e0-af4f-484b316fbdd7` 已超过自动压缩阈值；
Worker 在一个 Run 中连续 11 次记录：

```text
Rendered summary prompt exceeds configured token budget; skipping compaction this turn
```

完整 turn 选择要求不拆分工具调用；当最老的第一个完整 turn 本身已经大于
`trim_tokens_to_summarize` 时，不存在更早、更小的合法边界。确定性 fake-model 探针
重复两次均为 `compacted=false, model_calls=0`。后续每个模型调用会重试同一个不可能
前缀。

这会同时阻断 continuity summary 和 tagged receipt，因此：

- 该 Thread 不会通过 SNIP 生成新的 pending history；
- 手工 `/compact` 只显示“跳过”；
- Dream Prepare/Seal 的 `keep=(messages,0)` 会 fail closed；
- Dream 只能整理其他来源已经存在的 history，不能补救未生成的 receipt。

完整根因与上下文侧证据见
[`context-system-deep-audit-2026-08-23.md`](context-system-deep-audit-2026-08-23.md)。

## 6. 已验证健康项

- 非 PostgreSQL Memory 聚焦测试：442 passed。
- PostgreSQL Memory 聚焦测试：44 passed；使用临时 `deerflow_test_*` 数据库，不是
  业务库。
- PostgreSQL 17.11、Schema V1 与 `pg_trgm` readiness 检查通过。
- 当前业务库有 1 个 `memory_dream` succeeded；Memory 文档从 v0 发布到 v1，1 条
  history consumed，1 条 episode 已生成。
- 当前不变量查询为 0：无孤立 active Dream、无孤立 processing history、无 active
  preparation 缺少 active Job。
- Dream admission 冻结模型 payload、checksum、secret generation/digest；retry 不改绑
  当前模型。
- Dream 只拥有两个受限草稿工具。
- 发布事务复核当前 policy revision、偏好版本、文档 version/digest、history digest 和
  Job lease。
- dead/cancel 释放 processing history；成功才生成 episode 并消费 history。
- Memory scope 来自服务端 authority；生产 Sub-Agent 不继承 Memory authority。
- 浏览器 Memory 页面可读取当前 v1 文档和 archive episode；pending 为 0，控制台无
  页面错误。

这些健康项证明具体子链路，不抵消 M-01 至 M-04。

可重复测试命令：

```bash
cd backend
PYTHONPATH=app:packages/harness uv run python scripts/run_runtime.py -- \
  pytest -q $(rg --files tests | rg '/?test_memory.*\.py$' | rg -v postgres)

PYTHONPATH=app:packages/harness uv run python scripts/run_runtime.py -- \
  pytest -q $(rg --files tests | rg '/?test_memory.*postgres\.py$')
```

## 7. 验证边界

本轮没有：

- 调用会写业务数据的 `/dream`、`/dream-preparations`、新建 Run 或 reset/restore API；
- 修改管理员设置、挂起默认模型、等待 1440 分钟或清理历史 Job；
- 发起新的外部 Dream LLM 调用；
- 修改业务代码、运行配置或业务数据库。

当前成功 Dream 是既有持久化记录，不等同于本轮重新调用 provider。M-02 的生产到期
影响和 M-03 的真实管理员 UI 表现分别属于“触发链已确认、未对业务环境执行破坏性
触发”。

## 8. 建议的修复顺序和回归门槛

本报告不实施修复。后续实现应先建立失败测试，再按以下顺序处理：

1. 统一 Seal/Prepare 的 policy 物化语义，确保其拿到明确的当前或冻结 policy，而不是
   Worker 基础配置。
2. 让 compaction disabled、活动 Run 抢占、模型/SNIP 失败成为不同的机器可判别结果；
   Seal 只有真正的瞬时抢占才可成功 `noop`。
3. 为同一 due Thread 建立不会因终态失败而无限生成新 ordinal 的 admission 契约。
4. Dream model unavailable 必须与 `nothing_pending` 分开，并产生可观测的失败状态。
5. 与上下文修复共同覆盖“首个完整 turn 超预算”的降级策略，保持工具 turn 完整性，
   同时保证一定能取得进展。

最低回归矩阵：

- 基础配置摘要关闭 + DB 策略开启 -> Seal 与 Prepare 可执行；
- 永久配置失败 + 连续 Scheduler poll -> 不重复准入；
- 活动 Run 抢占 -> 可安全 noop，且之后可重试；
- pending > 0 + default/dedicated model 不可用 -> 明确 unavailable，不是 empty；
- oversized first complete turn -> 自动、手工、Seal、Prepare 均有可判别且可前进的结果；
- full/delta checkpoint -> receipt 仅随 replacement checkpoint 原子激活。

## 9. 修复复验（2026-08-23）

### 9.1 当前结论

本文共提出 **4 项主缺陷**；`M-04 / C-01` 是同一个跨 Memory/Context 的发现，不应
拆算成两项。当前冻结工作树的代码审查、确定性反例探针与自动化门禁均表明四项缺陷
已经修复，但需要区分“代码级闭环”和“真实 provider 端到端验收”：

| 编号        | 当前代码判定 | 修复后的行为 |
| ----------- | ------------ | ------------ |
| M-01        | 已修复       | Seal 与 Dream Prepare 在授权边界读取当前 PostgreSQL `agent_runtime`，仅投影 Memory/Summarization 策略，并让同一冻结 `AppConfig` 贯穿 drain 和最终 barrier。 |
| M-02        | 已修复       | 只有真实的活动 Run 抢占可以成功 `noop`；disabled、SNIP 失败及其他冲突均失败结算。同一 Thread activity epoch 的终态失败不会被连续 Scheduler poll 重新准入。 |
| M-03        | 已修复       | 存在 pending history 或 budget rewrite 时，Dream 模型不可用会返回/记录稳定的 `MEMORY_DREAM_MODEL_UNAVAILABLE`，不再伪装成 `nothing_pending`。 |
| M-04 / C-01 | 已修复       | 默认 SNIP 对超大完整 turn 使用有界分层投影，保持工具 turn 与原始 receipt identity 完整；手工 `force=True` 可压缩单个超长完整 turn，不可规划输入返回类型化错误。 |

主要实现位置：

- M-01：`backend/app/system_runtime_settings/app_config_projection.py`、
  `backend/app/worker/memory_seal.py`、
  `backend/app/worker/memory_dream_prepare.py`；
- M-02：`backend/app/private_work/memory_seal_service.py`、
  `backend/app/worker/memory_seal.py`；
- M-03：`backend/app/private_work/memory_dream_service.py`、
  `backend/app/scheduler/app.py`、前端 Memory 错误映射；
- M-04 / C-01：
  `backend/packages/harness/deerflow/agents/middlewares/summarization_middleware.py`、
  `backend/packages/harness/deerflow/runtime/context_compaction.py`。

### 9.2 M-04 / C-01 的进展保证

修复不是拆断工具调用或篡改 checkpoint 源消息。默认提示超预算时，系统按完整工具
turn 建立有界叶片，每个叶片都重复保留工具身份、状态和结果摘要，再对叶片结果做
有界归并；checkpoint replacement 与 receipt digest 仍绑定原始完整消息。每个叶片、
归并和修复提示都必须先通过 token budget 检查，并受叶片数与最多 32 次模型调用限制。

若输入结构本身不可投影、预算小到无法容纳最低提示、归并不能收敛或耗尽调用上限，
系统分别使用 `SnipSourceTooLarge` / `SnipPromptBudgetTooSmall` 结算，而不是对同一个
不可能前缀永远返回静默 `None`。直接 `/compact`、Seal 和 Dream Prepare 会把这些
结果映射为稳定错误；前端不再把它们显示成“没有足够消息”或普通跳过。

### 9.3 本轮验证证据

修复后在同一冻结工作树完成：

- 非 PostgreSQL Memory 测试：`489 passed`；
- PostgreSQL Memory 测试：`45 passed`，使用隔离测试数据库；
- 前端单元测试：`889 passed`（173 个文件）；
- 前端 `pnpm check`：ESLint 与 `tsc --noEmit` 通过；
- 相关后端文件 Ruff check 与 format check 通过；
- 本地服务重启后，PostgreSQL 17.11、Schema V1、`pg_trgm` readiness 与 Gateway
  `/health` 均通过。

针对原报告中的真实 Thread
`2d57b2a3-3624-49e0-af4f-484b316fbdd7`，只读 checkpoint 探针确认：当前有 61 条
消息、两个完整范围；最老直接 SNIP 提示仍超过 `15564` token 预算。使用不调用外部
模型的确定性 fake model 执行新算法后，4 次模型调用即可完成有界投影，删除 29 条、
保留 32 条，最大提示为 `15428 <= 15564`，且原始 source identity 不变。这证明原来
的确定性规划死锁已经消除，但不等同于真实 provider 成功。

本地浏览器重启后只读复核仍显示该真实研究会话为 `55.9K Tokens`、自动压缩阈值
`32.0K Tokens`、进度 `100%`；没有执行 `/compact`、发送新消息或运行 `/Dream`，
因此样本尚未被验收动作改变。

### 9.4 尚未完成的端到端边界与已知权衡

真实浏览器验收将把该会话的研究内容、工具结果和生成的 Memory 发送给当前配置的
外部模型，并写入新的 checkpoint、Run/Job 和 Memory 记录。该动作需要明确授权，
目前尚未执行；在依次验证 `/compact`、有业务意义的后续追问及 `/Dream` 之前，不能
把第 9.1 节的“已修复”扩大解释为真实 provider 端到端验收通过。

以下是本轮审查确认的非阻断边界，不属于原四项缺陷仍然存在：

- Scheduler 遇到 Dream model unavailable 时有稳定结构化日志，但没有独立持久化的
  失败 Job/审计记录；
- 普通 Run 的自动 SNIP 若遇到不可约 source，当前最终可能显示通用 Run 失败，而非
  M-04 的专用 UI 错误；
- Seal 的终态失败会消费当前 activity epoch，以抑制 Job storm；管理员修复配置后需
  重新排队 dead Job，或等待 Thread 产生新活动；
- provider 异常或连续返回无效 SNIP 输出仍会由后续调用重试，当前没有按 checkpoint
  建立独立 circuit breaker；
- 未闭合/畸形工具 turn 与仍处于开放状态的超长用户输入不属于“完整 turn”压缩范围。
