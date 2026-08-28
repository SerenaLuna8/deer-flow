# Harness 执行链路审计:问题清单、修复方案与验收

> **修复状态(2026-08-28 当前本地修复切片)**:S1–S4、M1–M9 及 D1–D4 对应修复均已按 TDD 落地。最终 backend 核心全量门禁为 `4519 passed / 0 failed / 0 skipped`(另有 5 个 `provider_integration` 用例按门禁定义 deselected);frontend 为 `1022 passed / 0 failed / 0 skipped`,类型、ESLint、Prettier 与 production build 全绿。real-backend Chromium 在 immediate 模式为 `6 passed / 5 skipped`,对应 5 项在 delayed 模式同轮 `5/5 passed`;终态 REST 接管的空帧敏感用例在最终补强后另连续 `3/3 passed`。mocked dynamic Chromium 为 `36 passed / 5 failed`,static Chromium 为 `2/2 passed`;前者 4 项在 HEAD 精确复现,另 1 项在 HEAD 复跑 5 次出现 3 次失败,现有证据不足以把这些失败归因于本轮 Harness 修复。仍未关闭的外部/生产边界见“验证账本”。

- 日期:2026-08-28
- 范围:Run 准入 → Worker 认领 → 沙箱拉起 → Skill 物化/密钥注入 → 模型调用与中间件 → 工具调用 → 子代理委派 → SKILL/MCP 执行 → 上下文压缩 → 终态结算,及平台运行时策略。
- 方法:六段并行深读(逐文件通读 + 契约比对)→ 主审逐环复核 → 两轮外部独立复核(含真实 PostgreSQL 事务复现、中间件复现与异常注入)交叉校准。严重性与后果链为**三轮校准后**结论。

## 审计基线(复现凭据)

- 基线提交:`c62939a75dfb7c2208714810c06664d44fdcbb57`
- 审计时工作树状态:已跟踪改动 patch 摘要(sha256 前 16 位)`063700f8b6b9c238`,`git status --porcelain` 共 88 个条目(含 5 个未跟踪路径;压缩/summarization、ToolCallControl 为进行中改动)。当前最终工作树仍未提交,且混有用户的其他在途改动;不能把整个工作树都归为本审计修复。
- 文中行号对应本日工作树,会随提交漂移;每条发现标注"存在性基线"(HEAD / 工作树)与"复现状态"(代码推演 / 真实执行复现)。
- 异常注入与离线复现不等价于真实外部 Provider、浏览器或生产监督进程验证。

## 结论摘要

确认 **4 个严重缺陷(S1–S4)**、**9 个中等问题(M1–M9)**、**4 个跨项设计主题(D1–D4;是 S/M 项的根因归纳,不重复计数)**。四个严重缺陷的机制在 HEAD 即存在;S2/S3 位于当前压缩改动主路径,属**合入门禁**;S1 由"本地审批模式"正常功能路径确定性触发,属**生产热修**。沙箱与文件边界、Skill 闭包/密钥作用域,以及排除 M7 loop/gate 与 M9 公平性后的子代理终态首胜仲裁,本次审计未发现其他严重问题(见"审计边界")。

## 当前工作树修复裁决

下表是最终实现状态;后文各条的“修复/修复后验收”保留原始处方与审计推理,不再表示待办。

| 编号 | 状态 | 最终落地结果 |
|---|---|---|
| S1 / D2 | 已完成 | 新写入经唯一类型化 `RunStatus → stream terminal` 适配器统一为规范终态,读侧接受历史 `success ≡ completed`;不可重试/Attempt 耗尽的死 Job 仅凭内部 terminal candidate 或历史终态创建 settlement-only successor。fallback 用每页 100 条的 `(dead_at, job_id)` process-local keyset 游标并有界回绕;稳定 Worker 进程内的连续 fallback 不会被固定前 100 条饿死,跨进程正确性仍由行锁与唯一 lineage 保证。 |
| S2 | 已完成 | 有快照与 bootstrap 两条分支都以同一 estimator、同一 checkpoint 表示重测源/结果;冻结的 per-image 上界随 estimator 进入视觉 checkpoint,未声明视觉成本则类型化为不可测,不再把图片按 0 Token 做单调比较;真实长线程 Seal 回归覆盖首批 drain。 |
| S3 | 已完成 | 自动压缩把回执构建纳入 typed skip-this-turn 边界;静态不可执行条件在 SNIP 前失败,手动/Seal/Dream 强制路径继续返回类型化错误。 |
| S4 / D3 | 已完成 | `Provider 结果分类 × 适配器重试安全证明` 两维正交;三家适配器的失败应答矩阵、外层恢复、Evidence 与原始异常传播保持一致,不把 502/504 伪装成“未执行证明”。 |
| M1 | 已完成 | output-limit 在 durable response barrier 后写内部 terminal candidate;普通 Stop < durable response < authorization revocation。撤权后的唯一公开 `interrupted` 终态由精确 Run/Job/Attempt 结算事务签发的私有 authority 完成,不重新授予执行权。 |
| M2 | 已完成 | commit ACK 结果不明只放弃当前 Job 租约并交由精确接管;领域冲突/lease loss 与编程不变式错误继续分层处理,不做盲目 commit 重试。 |
| M3 | 已完成 | checkpoint raw write 在同一 psycopg 事务内于写前、写后校验精确 Job/Run/Attempt lease,关闭 TOCTOU。 |
| M4 | 已完成 | System MCP OAuth token URL 经过端点策略,复用显式 egress proxy/timeout、`trust_env=False`、`follow_redirects=False` 的加固客户端。 |
| M5 | 已完成 | 实现与公开契约统一为最多 4 张、每张最多 20 MiB;不再施加未声明的 20 MiB 合计上限,也不静默截断。 |
| M6 | 已完成 | 子代理有可用 partial 时以 `completed + output_truncated` 保留文本并贯穿 lifecycle、回执、Lead 可见元数据,不伪造 error;只有没有可用文本时才 `failed + MODEL_OUTPUT_LIMIT`。 |
| M7 | 已完成 | 每条 execution 与 default executor 都绑定不可变 scheduler epoch 与实际 admission lease;旧 gate retire 后不能释放或关闭新 epoch 资源。替代循环在原 loop 上取消 source task并等待真实 graph/finalizer/inherited-operation 收据,随后关闭旧 loop;`aclose` 不再用伪造的 graph-quiescent 收据提前返回。 |
| M8 | 已完成 | 父 Run Stop 在授权边界内把仍进行中的 delegation ledger 条目终态化为 cancelled;撤权时不伪造 checkpoint 写入,由下一合法 Run 到达 DurableContext `before_model` 并提交更新时收敛。 |
| M9 / D4 | 已完成 | 调度门按 Run key 预留槽位并轮转;排队耗时进入生产 JSON/text 白名单字段。等待 ≥5 秒或 queue timeout 发 warning,由首次终态裁决者只记一次;timeout 已赢时的迟到 gate admission 被同一 record lock 拒绝,不再补打虚假 admitted warning。 |
| D1 | 已完成 | 静态冻结只证明 fixed/overlay + summary headroom 可容纳;`keep=64000` 明确是近似选择目标,不是可闭式换算的 Provider 上界。自动压缩逐个 complete-turn 候选用同一冻结 Provider profile 同时校验 approximate keep 与 effective trigger,生成 summary 后再按实际内容复测;无法安全保留时由最终容量守卫类型化失败。关闭 summarization 时不再求值或拒绝不适用的 trigger/keep 联合不变式。 |

---

## 一、严重缺陷

### S1. 审批悬挂 Run 的流终态拼写冲突:首次结算必回滚并击穿当前 Worker 进程

- 存在性:HEAD;复现:真实 PostgreSQL 事务复现。
- 位置:`app/reliability/run_execution/executor.py:1025`(live 侧 `terminal_status=lambda: str(record.status)` → `"success"`);`app/reliability/run_execution/handler.py:793-802`(结算侧强制 `status="completed"`);`deerflow/runtime/events/store/db.py:712-721,761-766`(严格字节比较,不一致即抛 `StreamWriteAuthorityRequired`)。
- 机制:`Run 状态 → 流终态状态` 映射(`success→completed`)只存在于结算修复路径;live 路径透传 `StrEnum` 拼写,合法终态集合(`events/models.py:18-28`)同时收两种拼写,掩盖分歧。悬挂成功路径(`handler.py:961-972`)无条件 `ensure_stream_terminal=True`,撞上已存在的 `success` 帧。
- 实际影响(校准后):首次结算事务回滚,当前 Worker 进程退出——**全部在途任务进入 drain,宽限期内完成者不受影响,未完成者中断**;Run/Job 保持 running、审批保持 staged。**恢复是有条件的**:lease 过期后,新 Attempt 只有在 `retry_safety == "safe"` 且 `attempt_count < max_attempts` 时才会被认领并进入既有终态帧恢复分支(`handler.py:511-537`,不重跑图,`_terminal_result` 已兼容 `success/completed`,`handler.py:667`);否则 claim 层先把 Job 判死(`SIDE_EFFECT_STATE_UNKNOWN` / `ATTEMPTS_EXHAUSTED`,`deerflow/persistence/jobs/sql.py:1013-1044`)。结算延迟为"剩余 lease 时间 + 调度时间"。
- 原始修复处方:
  1. Run→Stream 边界建一处类型化适配(`RunStatus → StreamTerminalStatus`),所有新写入共用;
  2. 修复 `ensure_settled_stream_terminal` 的严格幂等比较,接受语义等价(`success ≡ completed`)——读侧 `_terminal_result` 已兼容,缺口只在这里;
  3. 对已被 Retry-Safety/Attempt 门槛判死的存量 Job,提供精确的 settlement-only reconciliation(读侧兼容救不了它们)。
- 原始验收要求:悬挂成功且 live 帧已存在的结算测试;历史 `success` 帧幂等等价测试;真实 PostgreSQL 全生命周期(首次结算失败 → lease 过期 → 条件恢复 / 判死两个分支)。

### S2. 压缩回执不变式跨口径比较,合法压缩被误判"越压越大"

- 存在性:HEAD;复现:真实中间件执行复现(样本中 Provider 口径 1422→429,checkpoint 全元数据口径重测 5459,被误判为增长;膨胀系数为该样本值,不外推)。
- 位置:`deerflow/runtime/context_evidence/checkpoint.py:128-129`(`result_tokens > source_tokens` 即拒绝);`app/private_work/context_replacement.py:360 起`(结果侧用同一 estimator 对 `message_to_dict` 全元数据重测);`app/private_work/context_evidence_observer.py:237-294`(源侧:有快照时取旧 Provider wire 口径快照,**无快照分支同样以调用方传入的 `result.total_tokens`(wire/近似口径)合成源**——两条分支都不与结果侧同口径)。
- 影响:非必现,但对同一 checkpoint **确定性、粘滞**。Seal drain 首批只归档 trim 预算内的小前缀、保留线程主体,长线程大概率落入失败区间;Seal 最多 5 个 Attempt、Dream Prepare 默认 3 个,耗尽后该线程封存持续失败直到状态改变;自动路径叠加 S3 终止当前 Run,后续 Run 在同一 checkpoint 重复失败。
- 原始修复处方:两条分支统一——对压缩前、后的 checkpoint 状态用**同一 estimator、同一口径**重测再比较。前置校验的拆分要准确:observer/estimator/checkpoint identity 等**静态条件**可移到 SNIP 模型调用之前(避免每次失败先烧最多 32 次模型调用);`result_tokens <= source_tokens` 依赖 summary 产物,**必须在 SNIP 之后**校验。
- 原始验收要求:带"快照后追加工具结果"的回执回归测试;无快照(bootstrap)分支同口径测试;Seal 首批 drain 端到端。

### S3. 自动压缩的回执构建阶段在异常时终止 Run(相对新契约实现不完整)

- 存在性:逃逸机制 HEAD 即有;**契约归属需区分**——HEAD 时点 `backend/AGENTS.md` 规定 "permanent planning failures terminate with a typed reason"(HEAD 文件 478-479 行),类型化终止是当时契约;"降级为 skip-this-turn"是本工作树新增契约,新增的降级捕获未覆盖回执构建阶段,属**新契约实现不完整**。
- 位置:`deerflow/agents/middlewares/summarization_middleware.py:1594-1640`——捕获只包住 `compact_state`;返回 dict 时拼接的 `_context_compaction_update(...)` 在 try 之外,其 `SnipCompactionFailed` 与 S2 的 pydantic `ValidationError` 都逃逸 `before_model`。
- 影响:失败发生在状态提交前,线程状态不变,后续 Run 在同一触发点重复失败;每次先消耗最多 32 次 SNIP 模型调用(见 S2 前置校验拆分)。
- 原始修复处方:把回执构建纳入降级边界;捕获需覆盖 pydantic `ValidationError`(或在观察者边界统一转成类型化错误);"不可测 checkpoint"(estimator/快照缺失)按新契约降级。显式强制路径(手动/Seal/Dream)保持类型化终止。
- 原始验收要求:观察者缺 API、estimator 缺失、回执校验失败三种注入下,自动路径 `before_model` 返回 None 且 Run 继续;强制路径保持类型化错误。

### S4. 私有 chat Run 上 Provider 状态错误一次即判 ambiguous 终态

- 存在性:HEAD;复现:脚本实证(同一 429,带 Observer 一次即终态;无 Observer 路径正常)。
- 位置:`deerflow/models/provider_outcome.py:49-70`("无响应证明"只认 openai 家族连接期三类异常,anthropic/vllm 恒返回 None);`deerflow/agents/middlewares/provider_request_usage.py:1537-1562`(兜底 `except Exception` 在有 Observer 时一律抛 `ProviderDispatchOutcomeAmbiguous`);`app/reliability/run_execution/executor.py:891`(Observer 仅装配于 `runtime_kind == "chat"`)。
- 机制与范围(校准后):Agent Graph 档 `provider_max_retries=0`(`deerflow/models/runtime.py:48`),**chat 路径没有任何重试层**——一次 429/500/401 直接经守卫转成 `ProviderDispatchOutcomeAmbiguous`(GraphBubbleUp,外层显式透传),落不可重试终态 `CONTEXT_PROVIDER_CALL_AMBIGUOUS`。范围:私有 chat Run 与子代理;Skill Builder 不装 Observer 不受影响。openai 家族连接期错误仍可证明"未送达"并进入外层恢复与 `run.recovered_issue`,因此外层体系不是整体死代码——**状态类错误(429/5xx)的重试表、Retry-After 在 chat 路径不可达**,连接类在 anthropic/vllm 上同样不可达。
- 语义问题:HTTP 错误应答是 Provider 的确定性回答,归入"结局不明"污染 Evidence 语义(见 D3)。
- 原始修复处方:见 D3——扩展并正确使用**现有**双维度契约。
- 原始验收要求:三适配器 × {429, 500, 502, 504, 连接错误} 真值表,断言分类、重试行为、`recovered_issue`、Evidence 记录四者一致;Skill Builder 对照组。

---

## 二、中等问题

| # | 问题 | 位置 | 校准说明与修复方向 |
|---|---|---|---|
| M1 | output-limit 终态与用户 Stop 竞态:存储层把终态帧改写为 interrupted 并丢 error_code,结算侧坚持 error,最终状态取决于崩溃时机 | 事件存储/结算 | **不得用"先落库者为准"**。仓库已有语义优先级(越过 durable response barrier 的 output-limit 胜过普通 Stop;rollback 与授权撤销更强,见 `backend/AGENTS.md`)。修复=持久化仲裁事实并执行显式优先级矩阵。涉及终态权威,优先级应靠前 |
| M2 | 结算 commit 阶段任意异常无分类处理,单 Job 故障放大为 Worker 进程退出(S1 的爆炸半径放大器;不会自动判死 Job) | `handler.py` 结算路径 | **不与 S1 合并提交、不加宽泛 catch**。commit 可能"已提交但 ACK 丢失",须区分:已知领域冲突 / lease loss / 结果不明(按不明处置) / 编程不变式错误(允许进程级失败) |
| M3 | checkpoint 写入的 lease 校验是独立事务预检,raw 写入走另一条连接,存在 lease 到期沿 TOCTOU | checkpointer | 契约明确"每次写入在同一事务校验 exact lease",**必须**把校验并入写入事务;无"接受并记录窗口"选项。涉及 lease 权威,优先级应靠前 |
| M4 | MCP OAuth 令牌获取用裸 `httpx.AsyncClient`(`trust_env=True`、绕过 egress 代理与端点策略),`client_secret` 有经环境代理外泄的条件性风险;限 system 域 MCP | `deerflow/mcp/oauth.py:90` 对比 `mcp/http_security.py:171-209` | 令牌端点复用加固客户端工厂,并补三件事:`token_url` 过端点策略校验、显式 egress 代理、显式 `follow_redirects=False`(不依赖 httpx 默认) |
| M5 | 图片注入实现了未声明的"合计 ≤20MiB"上限,超 4 张/超总量直接抛错终结 Run;文档契约是"每张 20MiB、至多 4 张";错误文案误导 | `view_image_middleware.py:490-496` | 先做资源预算决策。**不采用静默"只取前四张/截断"**:要么按预算调整文档与限额并给准确的拒绝文案,要么放宽实现;用户可见行为必须与文档一致 |
| M6 | 子代理链无输出截断(finish_reason=length)处理,残缺文本结果以"成功"回执交给 Lead;Lead 侧同场景是终态错误(代码缺口分析,无端到端复现) | `assembly.py` 子代理链 | 为子代理接线输出限制观察,至少在回执上标记截断 |
| M7 | 子代理调度循环死亡重启后旧 `_gate` 绑定死循环,永久任务失败并可拖挂 drain(低概率路径分析) | `subagents/lifecycle.py` | 重启路径重建/校验 gate 绑定 |
| M8 | 父 Run Stop 后 delegation ledger 条目永久滞留 in_progress,给后续模型错误指引 | `delegation_ledger.py` | Stop 结算时终态化台账条目 |
| M9 | 16 槽进程调度门无 Run 级公平性,饱和时单 Lead 可同步阻塞约 31 分钟(公平性缺失是事实;"租户饥饿"为影响推断) | `subagents/lifecycle.py` | 见 D4 |

---

## 三、跨项设计主题(S/M 项的根因归纳,不重复计数)

### D1. 压缩触发、模型容量、保留量之间缺少联合不变式(S2/S3 的相邻风险,320k 默认值的合入门禁)

- 精确保护缺口:`capacity < occupancy < trigger` 区间——占用已越过容量守卫(`>`,`provider_request_usage.py:1396`)但未达触发(`>=`,`summarization_middleware.py:1332`),Run 以 `CONTEXT_CAPACITY_EXCEEDED` 失败而压缩从未参与。若占用一次跃升到 `>= trigger`(如超大工具结果),压缩仍会先运行,所以不是"永不触发"。
- 基线:HEAD 默认 trigger 32k,暴露面较小(容量下限 1,矛盾组合仍可配);**工作树改为 320k 并移除 fraction 触发,把风险扩大到全部容量 <320k 的模型组合**——合入前必须解决。当前默认模型容量 1,000,000,默认组合未失效。
- 已落地修复:触发判断、候选 tail 与 summary 结果都使用同一冻结 Provider profile。静态冻结校验 fixed/overlay + 4096 summary headroom;`keep(64k)`仅作为近似近期历史选择目标,候选还必须以实际消息数、多字节/JSON 展开、视觉成本和 Provider framing 重测并严格低于 effective trigger。单条超长消息不存在只依赖 `keep` 的有限可靠闭式上界,因此不再把百分比或 UTF-8 倍数公式伪装成证明。若当前 Run 的 summarization 已关闭,执行器直接关闭自动压缩触发,不再验证或钳制这组不适用的 trigger/keep 配置;real-backend Replay 的 64k 模型已覆盖该分支。

### D2. Run 状态到流终态状态没有单一权威映射(S1 根因)

`success→completed` 映射原先散落于 live、结算修复、Gateway fallback 与 Thread 删除路径,合法集合靠双拼写掩盖。现由 `stream_terminal_status_for_run_settlement(RunStatus)` 唯一投影 Run settlement;`canonical_stream_terminal_status` 仅负责不可变历史流帧的拼写等价。Audit sink 与 Automation occurrence 的映射目标不是 stream terminal,不纳入此适配器。另有 `ensure` 语义等价幂等与存量死 Job settlement-only reconciliation(见 S1)。

### D3. "结局不明"与"可重试性"混淆(S4 根因)——扩展现有契约,不是从零建

修复前 Evidence 已有双维度雏形:`ProviderFailedV1`(含 `failure_code` + `retry_safety: ProviderRetrySafety`)与 `ProviderAmbiguousV1`(`evidence.py:184-199`),但缺少表达"**已收到失败应答且适配器证明可安全重试**"的取值与正确分类器;当前已按以下两维落地:

1. Provider 结果分类:成功 / 已知失败应答 / 结局不明;
2. 重试安全:适配器可证明安全 / 不安全 / 未知。

只有适配器逐状态证明安全的(如 openai 家族 429)才自动重试,且**不得伪装成 `NO_RESPONSE_PROVEN`**;网关型 502/504 不能证明上游推理/计费未发生,保持"结局不明"的保守准入。ambiguous 记录只对应真正的结局不明。

### D4. 共享子代理调度门是纯 FIFO 信号量,无 Run 级公平性(M9 的设计面)

多项目共享 Worker 形态下的可预见尾延迟来源。当前已落地按 Run key 的预留槽位与轮转,并补齐生产格式器、长等待和 timeout 告警;见 M9。

---

## 四、原始建议修复顺序(两条线)

以下保留审计完成时的依赖顺序建议,不是提交历史;当前本地修复混在未提交工作树中,没有形成“M2 单独提交”等独立 commit。最终实现状态以“当前工作树修复裁决”为准。

**线 A:当前压缩工作树的合入门禁**(不合格不合入)

1. S2 —— 源/结果同一 estimator 同口径重测(两条分支);静态前置移到 SNIP 前,尺寸不变式留在 SNIP 后。
2. S3 —— 回执构建纳入自动降级边界(含 `ValidationError`);强制路径保持类型化终止。
3. D1 —— 320k 默认值合入前建立联合不变式(含固定上下文/summary/keep/余量)。

**线 B:生产热修与后续**

1. S1 + D2(若启用本地审批模式则最先):统一写侧映射 + `ensure` 语义等价幂等 + 存量死 Job reconciliation。
2. M2 单独提交:结算异常分类(领域冲突 / lease loss / 结果不明 / 不变式错误)。
3. S4 + D3:先做三适配器 × 状态码真值表,再扩展 `ProviderFailedV1` 取值与分类器,后接通重试。
4. M1、M3 提前处理(分别涉及终态权威与 lease 权威,不排在 M 类末尾)。
5. M4(system 域凭据出站,含 token_url 策略/显式代理/显式重定向)→ M5(先做产品决策,不静默截断)→ M8/M6 → M7/M9(配合 D4)。

## 五、验证账本

### 已完成

- backend 核心全量门禁:独立 PostgreSQL 17、`en_US.UTF-8`、真实随机测试库。`4502/4502` 是最终语义补丁前的阶段基线;S2 visual、D1 cutoff/recheck、S3 visual preflight 及关闭 summarization 时跳过不适用联合不变式等后续补丁落地后,最终 checkout 重跑为 `4519 passed / 0 failed / 0 skipped`,另有 5 个 `provider_integration` 用例按门禁定义 deselected、24 条既有弃用 warning。最终 Ruff check 与 format check 全绿(`1205 files already formatted`)。
- 终态/恢复相邻验证:撤权后候选结算真实 PostgreSQL `4/4`;公平扫描 + Job owner lifecycle 真实 PostgreSQL `25/25`;公平扫描、Execution Approval PostgreSQL 与 Job owner 静态 contract 三文件混合集 `79/79`。禁用公平游标的敏感性运行准确恢复旧故障(`successor_claim is None`)。
- 子代理生命周期与日志:loop death、直接 `aclose`、Run 公平性、queue timeout、生产 JSON/text formatter 及 binding/adapter/host 相邻集 `83/83`。
- frontend 最终门禁:unit `1022/1022`(193 个文件、0 skipped),ESLint + TypeScript、全仓 Prettier 与 production build 全绿,build 生成 69 个页面。
- real-backend Chromium:immediate Worker 模式同轮 `6 passed / 5 skipped`;被模式选择跳过的 5 项在 delayed Worker 模式最终同轮 `5/5 passed`,两轮合计覆盖该项目全部 11 个场景。delayed 首次全量曾为 `4/5`:唯一失败来自测试辅助端点与 Worker 争锁形成的 PostgreSQL deadlock;该项隔离重跑 `1/1`,随后完整 delayed 套件同轮 `5/5`,最终通过结论以完整重跑为准。终态 REST 接管的空帧敏感用例在最终 ref-authority 补强后另连续 `3/3 passed`。
- real-backend 验收额外暴露并修复两项相邻阻断:(1) Regenerate 的公开请求投影此前拿去与含可信内部 `title` 的完整 prepared input 比较,合法请求返回 409;现改为只在公开投影上校验身份,执行仍使用完整 prepared input。(2) Thread 元数据后台刷新时,已有缓存数据仍被 `isFetching` 误判为 `loading`,会拆掉 history/stream 并造成终态答案空帧;现仅在无缓存数据时进入 loading,终态显示 latch 以 ref 为同步权威、revision 只负责通知渲染。第二项已在 HEAD `c62939a` 精确复现同型空帧,不是本轮修复新引入。
- 其他浏览器:隔离端口 mocked dynamic Chromium `36/41`;同一修复阶段 static Chromium `2/2`。dynamic 的 4 个失败在 HEAD 精确复现;Research 第二次发送用例在 HEAD 连跑 5 次为 2 pass / 3 fail,相关测试及直接实现相对 HEAD 无差异,现有证据不足以把波动归因于本轮。real-backend、static 与 mocked dynamic 是三套不同项目,未合写成“完整 Playwright 全绿”。
- 静态与格式:`make lint` 全绿(`1205 files already formatted`),`git diff --check` 全绿。blocking-I/O 扫描正常退出并列出 90 条当前库存;thread-boundary 扫描正常退出但仍有 INFO/WARN 库存——未做基线归属比对,且二者是清单型扫描,不能表述为“零发现”。

### 未覆盖或不构成当前通过证据

- 5 个真实外部 Provider 集成测试未运行;异常注入和适配器矩阵不能替代真实计费/网关行为。
- 未在生产监督进程、真实 Worker drain 或目标部署环境做故障演练。
- 项目 `.env` 原开发库 `127.0.0.1:9432` 在最终复跑时已不可连接;最终 PostgreSQL 证据来自隔离临时集群,不代表当前开发库 readiness。
- S1 公平游标是 process-local;未验证多进程竞争或每次推进前反复重启时的 liveness。
- real-backend fixture 在 Worker 关闭时仍记录 LangGraph batch task pending 的 asyncio warning;全部断言及 Worker start/stop 计数正常,但尚未单独证明该 warning 只属于测试夹具清理,不能当作生产 graceful-drain 已验证。

## 六、审计边界

以下环节本次审计**未发现**严重/中等问题(不等于被证明安全):沙箱拉起与文件终结(本次执行的既定路径穿越/符号链接/TOCTOU/scratch 清理/env 注入用例均 fail-closed;未覆盖真实 Sandbox Provider/container)、Skill 闭包冻结与密钥 Generation 锁定、MCP CIDR(仅 IP 字面量)、SKILL.md 证据链、中间件顺序契约(构造期断言)、ToolCallControl 回执重放、排除 M7 loop/gate 与 M9 公平性后的子代理槽位归还与首胜仲裁、准入原子性、事件先落库再 NOTIFY、重放地平线、Seal/Dream 策略冻结、手动压缩 CAS。
