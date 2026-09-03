# Python 生产代码渐进式模块拆分设计

> 状态：用户已确认的书面设计；本文不表示拆分已经开始。
> 日期：2026-09-02。
> 仓库基线：`0d421ede2d52a2cf22a5c8fedfdfbb10e6e1394c`。
> 基线状态：工作树干净；本设计审计的主要目标文件未被最近一次 Model/Schema 提交修改。
> 本文固化已逐节确认的架构设计；首批实施计划另文编写。

## 1. 目标

对后端生产 Python 中职责过多、变化原因混杂的模块进行渐进式拆分，使每个新模块具备一个清晰所有权、稳定接口和可独立验证的职责，同时保持现有业务行为不变。

本次拆分解决的主要问题不是物理行数本身，而是下列结构风险：

- 一个文件同时拥有 HTTP、Application Service、Worker、持久化或运行时多个边界。
- 单个编排函数同时承担准备、执行、终态判断和资源清理。
- 生产调用方依赖其他服务类的私有静态方法。
- Router、Tool、DTO、codec、校验和生命周期状态机混合在同一模块。
- 为规避循环导入而出现延迟导入或跨模块私有符号引用。

所有批次均使用“叶子模块优先、兼容入口保留、风险状态机后置”的渐进方案。每个子批次必须可独立验证、合并和回退。

## 2. 已确认事实、设计判断与未验证假设

### 2.1 已确认事实

- `backend/app/shared_assets/skill_design_service.py` 为 4,791 行，`SkillDesignService` 同时服务 Gateway Session 流程和 Worker Draft/Terminal Sink。
- `backend/app/shared_assets/agent_design_service.py` 为 3,359 行，一个 Service 同时承载 Turn、Generation、Blueprint、Commit、Cancel 和 Recovery。
- `backend/app/gateway/routers/private_work.py` 为 3,195 行，集中 Thread、Run、SSE、文件、Approval、Feedback 和 Context/Goal API。
- `backend/app/gateway/routers/project_assets.py` 为 2,648 行，集中 Agent、Skill、MCP、Binding、Secret 和 Catalog API。
- `backend/app/private_work/execution_approval.py` 为 2,836 行，同时包含 Provider Policy、Worker Port、Recovery 和 Gateway Service。
- `backend/packages/harness/deerflow/sandbox/tools.py` 为 2,780 行，同时包含路径映射、安全校验、Sandbox 初始化、Host Execution 和具体 Tool。
- `backend/packages/harness/deerflow/runtime/runs/worker.py` 为 3,515 行，其中 `run_agent()` 约 1,289 行。
- `backend/app/reliability/run_execution/executor.py` 仅 1,344 行，但 `_execute_with_trace()` 约 682 行，直接组合大量应用域与 Harness 运行时依赖。
- 测试和部分生产代码直接导入上述模块的私有符号，因此移动实现必须保留兼容 seam。
- 当前架构要求 `app.* -> deerflow.*`，Harness 不得导入 `app.*`；`actweave_knowledge` 不得导入 `app.*` 或 `deerflow.*`。
- Gateway 不执行 Agent Graph；Worker 是唯一执行者；Scheduler 只负责到期 Automation 的 admission。

### 2.2 设计判断

- 先提取 contracts、codec、validation 和纯计算，再拆 Router 和跨进程职责，可显著降低早期改动风险。
- `sandbox/tools.py` 的职责边界清楚，适合在 Worker 核心之前拆分，以便先稳定 Tool 和 Host Execution seam。
- `worker.py` 与 `executor.py` 是最高价值但最高风险的拆分对象，应在兼容测试、Sandbox seam 和前置模块稳定后处理。
- 大而内聚的状态机不应为了降低行数被机械拆开。

### 2.3 未验证假设

- 仓库外部没有依赖 `app.*` 私有符号的未登记消费者。仓库内调用可以审计；仓库外调用无法仅通过静态扫描证明不存在。
- 现有 focused tests 足以描述大部分兼容 seam，但每批实施前仍需确认是否缺少对象身份、路由顺序或事务行为特征测试。
- 结构移动不会改善或恶化运行时性能；若批次涉及 Stream 或 Provider 测量，必须使用现有行为测试验证，而不能从代码形态推断。

## 3. 范围与非目标

### 3.1 范围

- 生产 Python 模块的职责拆分和导入方向调整。
- 为对应生产批次迁移现有测试，或补充必要的兼容/特征测试。
- 保留旧模块作为兼容 façade/re-export。
- 为新的稳定文件边界更新 `backend/AGENTS.md` 和必要的 README 架构说明。

### 3.2 非目标

- 不进行全仓测试文件整理或测试框架重构。
- 不修改业务规则、HTTP payload、数据库 Schema、错误码、错误文本或用户界面。
- 不统一 Agent Builder 与 Skill Builder 为公共基类。
- 不引入新的 Repository commit 所有权、事务拆分或锁顺序。
- 不改变 Gateway、Worker、Scheduler 或 Provisioner 的进程责任。
- 不以统一最大行数作为验收门，也不为了“小文件”创建无业务含义的碎片模块。
- 不在结构移动中顺便修复发现的行为问题；必要的行为修改必须另行设计。

## 4. 方案选择

### 4.1 采用：叶子模块优先的渐进式拆分

顺序为：契约冻结 -> 叶子模块 -> HTTP Router -> 跨进程服务 -> Sandbox Tool -> Worker Execution -> 次级热点复审。

优点：

- 每个批次可独立审查和回退。
- 先建立兼容层与测试，再进入高风险并发/终态代码。
- 当前调用方无需在一个巨大变更中同时迁移。

代价：

- 一段时间内会同时存在新 owning module 和旧兼容入口。
- 总体实施周期长于一次性目录重组。

### 4.2 未采用：风险热点优先

直接从 `executor.py` 和 `worker.py` 开始能最快降低核心函数体积，但会把第一批变更放在终态、取消、Checkpoint 和资源清理的最高风险区，不适合作为本次渐进重构的起点。

### 4.3 未采用：一次性 package 重组

一次性移动所有大模块并全量修改导入可以更快得到整齐目录，但 Diff 难以审查、无法细粒度回退，且容易混淆结构问题和行为回归。

## 5. 全局架构约束

### 5.1 兼容 façade

- 旧模块保留原导入路径，并从新 owning module 原样导出类型、函数、Router 或 Tool。
- façade 不复制业务逻辑，不重新装饰 Tool，不创建 wrapper、subclass 或第二份 Router。
- 对对象身份敏感的 seam 必须满足旧路径与新路径导出同一个对象。
- 已被测试或生产调用的私有静态方法暂时保留为兼容别名或等价薄入口；删除属于独立后续决策。
- 不增加运行时弃用 warning，避免改变日志和错误行为。

### 5.2 依赖方向

- `app.*` 可以依赖 `deerflow.*`，反向依赖禁止。
- `app.knowledge` 可以依赖 `actweave_knowledge`；Knowledge package 不得依赖宿主应用或 Harness。
- 一个新叶子模块不得反向导入其 façade。
- Router 组合模块只做确定顺序的注册；路由模块不得成为领域事务所有者。
- Worker 辅助模块不得反向导入 `runtime.runs.worker`。

### 5.3 事务与授权

- Repository 不新增隐式 commit。
- Project、Membership、业务资源、Job/Run/Attempt 的现有锁序保持不变。
- `ProjectContext`、`PrivateWorkContext`、capability、lease 和 secret authority 继续在执行副作用的事务中重验。
- Builder Validate/Commit/Cancel、Execution Approval stage/claim/complete/decide、Run Snapshot admission 和 Checkpoint write 不得拆成跨事务半流程。

### 5.4 完成标准

拆分不以文件行数为唯一标准。一个 owning module 完成拆分必须同时满足：

- 只有一个可解释的变化原因。
- 调用方无需阅读其内部实现即可理解输入、输出和依赖。
- 内部实现可以替换而不改变兼容入口与消费者契约。
- 没有新增循环导入、反向依赖、重复对象或隐式全局状态。

## 6. 批次总览

| 批次 | 目标 | 主要对象 |
| --- | --- | --- |
| 0 | 冻结兼容契约 | imports、identity、Router/OpenAPI、错误、进程边界 |
| 1 | 提取低风险叶子模块 | Builder contracts/codec/validation、Skill integrity、Provider Request |
| 2 | 按资源族拆 HTTP Router | Private Work、Project Assets；Knowledge Gateway 后置 |
| 3 | 分离跨进程职责 | Skill Builder、Execution Approval、Agent Builder generation |
| 4 | 拆 Sandbox Tool | path、runtime、host execution、bash/file/search tools |
| 5 | 拆 Worker Execution | stream、runtime binding、goal、rollback、executor preparation/outcome |
| 6 | 复审次级热点 | Summarization、Snapshot、Checkpointer、Job contracts |

每个顶层批次由若干独立子批次组成，不作为一个巨大提交实施。

## 7. 批次 0：冻结兼容契约

本批不拆生产实现，只建立后续移动必须满足的可执行基线。

### 7.1 冻结内容

- 仓库内旧导入路径及直接导入的私有符号。
- 关键类型、Router 和 LangChain Tool 的对象身份。
- HTTP method、path、route name、status code、response model、route class、依赖和注册顺序。
- 当前错误码、错误文本、JSON/DTO 投影和 Provider fingerprint。
- Gateway、Worker、Scheduler 和 Harness 的 import boundary。

### 7.2 测试策略

增加少量定向测试，例如模块兼容契约和规范化 Router manifest。Router 比较不保存脆弱的全量 OpenAPI JSON，而比较经过规范化的业务相关字段，并额外验证 OpenAPI 没有 operation id/path 漂移。

若当前基线已有失败，必须记录其命令和错误，并在开始子批次前区分历史失败与本批失败。

## 8. 批次 1：低风险叶子模块

### 8.1 Skill Builder 纯模块

新建：

- `backend/app/shared_assets/skill_design_contracts.py`
- `backend/app/shared_assets/skill_design_codec.py`
- `backend/app/shared_assets/skill_design_validation.py`

`skill_design_service.py` 暂时保留 Session、Validate、Commit、Cancel 和事务编排。现有 `SkillDesignService._validate_turn`、文件投影和 Draft 校验等测试 seam 暂时保留兼容入口。

### 8.2 Agent Builder 纯模块

新建：

- `backend/app/shared_assets/agent_design_contracts.py`
- `backend/app/shared_assets/agent_design_codec.py`
- `backend/app/shared_assets/agent_design_validation.py`

`agent_design_service.py` 暂时保留 Session、Generation、Blueprint、Commit、Cancel 和事务编排。

### 8.3 Skill Package Integrity

新建 `backend/app/shared_assets/skill_package_integrity.py`，收纳路径规范化、文件变化、checksum、Archive 分析和持久文件完整性检查。

`SkillService` 保留 Project Skill 的 Version、Activation、Suspension、Deletion、quota 和 audit 生命周期。生产调用方不再依赖 `SkillService._verified_archive_files`，旧静态入口仍兼容。

### 8.4 Provider Request

新建：

- `backend/packages/harness/deerflow/agents/middlewares/provider_request_profile.py`
- `backend/packages/harness/deerflow/agents/middlewares/provider_request_measurement.py`
- `backend/packages/harness/deerflow/agents/middlewares/provider_request_guard.py`

依赖方向固定为 profile -> measurement -> guard。原 `provider_request_usage.py` 保留现有 `__all__` 的兼容导出。Provider fingerprint、token/byte 计算、middleware 顺序和 cancellation 后 Evidence 顺序不得改变。

## 9. 批次 2：HTTP Router 模块化

为了保留旧 `.py` 模块并避免同名 file/package 解析冲突，新实现使用不同目录名。

### 9.1 Private Work Router

```text
backend/app/gateway/routers/
├── private_work.py
└── private_work_routes/
    ├── contracts.py
    ├── dependencies.py
    ├── streaming.py
    ├── context_controls.py
    ├── files.py
    ├── approvals.py
    ├── runs.py
    ├── feedback.py
    ├── threads.py
    └── router.py
```

`private_work_routes/router.py` 按当前顺序组合子路由。旧 `private_work.py` 只导出组合后的同一个 Router 和仓库内仍使用的兼容符号。迁移顺序为 contracts/dependencies、Context/File/Approval、Run/SSE、Feedback/Thread。

### 9.2 Project Asset Router

```text
backend/app/gateway/routers/
├── project_assets.py
└── project_asset_routes/
    ├── contracts.py
    ├── common.py
    ├── catalog.py
    ├── agents.py
    ├── skills.py
    ├── mcp.py
    ├── bindings.py
    └── router.py
```

`register_asset_routes()` 第一阶段整体移动，不同时重写其动态注册逻辑。`admin_assets.py` 先继续经旧入口导入；新结构稳定后再迁移内部生产调用。MCP Secret、URL redaction 和请求错误映射必须保持原 owning module 的单一实现。

### 9.3 Knowledge Gateway

Knowledge Gateway 属于组织性膨胀而非业务层错位，且基线刚完成 Model/Schema 管理变更。本批不处理它；待相关代码稳定后单独审计和设计。

## 10. 批次 3：跨进程职责

### 10.1 Skill Builder

```text
backend/app/shared_assets/
├── skill_design_service.py
├── skill_design_lifecycle.py
└── skill_builder_draft_sink.py
```

先整体提取 Worker Draft/Terminal Sink，再让 Worker Executor 直接构造新 Sink，同时保留 `SkillDesignService.terminal_sink()` 兼容入口。最后把 Gateway Session/Validate/Commit/Cancel 移入 lifecycle owning module。

不得把 Validate、Commit、Cancel 的一个事务拆成多个协作者自行提交的事务，也不得为 Agent/Skill Builder 创建公共生命周期基类。

### 10.2 Execution Approval

```text
backend/app/private_work/
├── execution_approval.py
├── execution_approval_policy.py
├── execution_approval_codec.py
├── execution_approval_worker.py
├── execution_approval_recovery.py
└── execution_approval_service.py
```

`WorkerHostExecutionApprovalPort`、Recovery 顶层函数和 `ExecutionApprovalService` 分别整体移动。`_stage()`、claim、spawn authorization、complete 和 `decide()` 的内部阶段不拆散。

### 10.3 Agent Builder

新建 `backend/app/shared_assets/agent_design_generation_lifecycle.py`，承接 generation prepare、finish、stop 和 stale recovery。原 Service 保留 Session、Blueprint、Commit 和 Cancel。本项优先级低于 Skill Builder 和 Execution Approval。

## 11. 批次 4：Sandbox Tool 模块化

保留 `backend/packages/harness/deerflow/sandbox/tools.py` 作为兼容入口，实现迁入：

```text
backend/packages/harness/deerflow/sandbox/tooling/
├── path_mapping.py
├── bash_policy.py
├── runtime.py
├── host_execution.py
├── bash.py
├── files.py
└── search_tools.py
```

迁移顺序：

1. 虚拟路径、Sub-Agent output 映射和路径遮罩。
2. Sandbox 解析、初始化和异步包装。
3. Host Execution identity、Secret 扫描和 Approval Plan。
4. File/Search Tool。
5. Bash Tool。

`deerflow.sandbox.security` 继续作为安全策略权威。每个 LangChain Tool 只装饰并创建一次；旧路径和新路径必须导出同一个 Tool，对应 `.func`、`.coroutine`、name、错误文本、截断和 Secret 遮罩顺序保持一致。

`host_execution_runner.py` 改为依赖公开的 `tooling.host_execution`，不再导入 façade 的私有函数。

## 12. 批次 5：Worker Execution 模块化

### 12.1 Harness Execution

```text
backend/packages/harness/deerflow/runtime/runs/
├── worker.py
├── stream_delivery.py
├── runtime_binding.py
├── goal_continuation.py
└── checkpoint_rollback.py
```

先整体移动已有顶层辅助代码，再逐段提取 `run_agent()` 的连续阶段。新模块不得导入 `worker.py`；旧私有名称继续由 `worker.py` 导出，且 `run_agent()` 继续通过测试可 monkeypatch 的模块级 seam 调用。

`worker.py` 最终仍拥有：

1. 运行准备的总编排。
2. Agent Graph 调用和流消费。
3. 业务终态优先级。
4. File Finalization 与资源清理顺序。

必须保持根 `values` 流的语义权威、Durable terminal 与 Stop/Revocation 的既定优先级、资源所有权单次转移和终态发布顺序。

### 12.2 Private Run Executor

```text
backend/app/reliability/run_execution/
├── executor.py
├── preparation.py
└── outcome_mapping.py
```

`preparation.py` 负责冻结策略/模型、物化资产、构造 File Authority、Checkpointer 和 `RunContext`；`outcome_mapping.py` 只承接无需数据库或 authority side effect 的纯 Outcome 映射。

`RunAgentPrivateExecutor` 继续拥有 lease boundary、Worker 调用、异常优先级和最终 cleanup。不得用无类型大字典替代显式依赖和返回类型。

## 13. 批次 6：次级热点复审

本批不是默认全部执行。前五批稳定后，对以下对象逐项重新审计：

### 13.1 Summarization

- `snip_planner.py`：Prompt 规划和多阶段归并。
- `turn_compaction.py`：完整 Turn、保留边界和 cutoff。
- `compaction_receipts.py`：Archive、Context Evidence 和 receipt。

原 Middleware 保留 LangChain hooks、同步/异步调用和结果组装。

### 13.2 Run Snapshot

只提取 Snapshot 计划、编码和纯校验。`create_run_with_snapshot_in_session()` 继续拥有完整原子事务。

### 13.3 Checkpointer

可以提取 Context/Memory repair 和 Thread cleanup 协作者，但 Checkpoint write、lease 验证和 Approval 删除事务仍由原 Saver 控制。

### 13.4 Job

最多先提取 `jobs/contracts.py`。`JobRepository`、claim、heartbeat、settlement 和 requeue 状态机保持集中。

### 13.5 继续暂缓

- `deerflow.subagents.lifecycle`
- `run_skill_tree_materializer.py`
- Knowledge Retrieval Service
- Model Registry Service

## 14. 验证策略

### 14.1 每个子批次

1. 补充缺失的兼容或特征测试。
2. 证明测试能够捕捉预期 seam，或记录当前行为基线。
3. 执行最小代码移动，不同时改名和改行为。
4. 运行对应 focused tests。
5. 验证新旧导出 identity、serialization、error 和 call result。
6. 运行 Ruff 和阻塞 IO 静态检查。
7. 检查 `git diff` 与 `git status`，确保不包含其他工作。

### 14.2 每个顶层批次

```bash
cd backend
make lint
make detect-blocking-io
make test
```

领域附加门：

- Router：规范化 route manifest、OpenAPI、授权 dependency、无重复注册、Gateway import/startup contract。
- Builder、Approval、Snapshot、Checkpointer：相关 PostgreSQL 原子性、锁竞争和失败回滚测试。
- Sandbox：Tool identity、路径安全、Secret masking、并发文件锁和同步/异步初始化。
- Worker：Stream batching、Goal Continuation、Rollback、Host Approval pause、Run Outcome、取消、Context Evidence 和资源所有权。
- Dependency：静态检查 Harness 不导入 `app.*`，Knowledge package 不导入宿主或 Harness。

focused/offline tests 不证明真实 Provider、外部 Sandbox、PostgreSQL 目标部署、浏览器矩阵或生产并发环境已经通过。交付时必须准确说明实际验证边界。

## 15. 失败与回退

出现以下任一情况，停止当前子批次：

- 新旧 identity 不同。
- Router method/path/name/response、dependency 或注册顺序发生非预期变化。
- 新增循环导入或破坏现有延迟导入。
- Repository commit 所有者、事务数量或锁序改变。
- Tool name、错误文本、serialization、fingerprint 或 Secret masking 改变。
- Worker terminal、cancellation 或 cleanup 顺序改变。
- 无法区分历史失败与当前子批次失败。

回退单位是整个子批次，不在结构重构中以行为补丁向前修。实施应使用用户确认的隔离工作树或原子提交；回退使用丢弃隔离工作树或可审查的 revert，禁止使用破坏性 `git reset --hard`。

## 16. Façade 生命周期

- `deerflow.sandbox.tools`、`deerflow.runtime` 等可能被仓库外使用的入口默认长期保留。
- `app.*` 内部 façade 只有在生产调用归零、测试迁移完成、完整验证通过且得到明确授权后才能删除。
- 删除 façade 必须是独立批次，不能夹在 owning module 拆分中。
- 无法证明仓库外没有消费者时，保留一个只做 re-export 的入口优于删除。

## 17. 实施计划拆分

本设计是总架构规格，不生成一个覆盖全部批次的巨大执行清单。实施计划按真实创建日期分别命名，至少分为：

1. foundations：批次 0–1。
2. gateway-routes：批次 2。
3. process-boundaries：批次 3。
4. sandbox-tools：批次 4。
5. worker-runtime：批次 5。
6. secondary-hotspots：批次 6，仅在重新审计并再次批准后创建。

设计文档经用户书面复核后，第一份实施计划只覆盖批次 0–1。后续计划在前一批完成、工作树状态重新确认且相关代码没有漂移后再生成。

## 18. 长期维护影响

该设计会增加少量 owning modules 和兼容入口，但降低单文件认知负担、跨域导入和变更波及面。最重要的长期收益是把 HTTP、Application Service、Worker、Tool 和 Harness Execution 的责任边界显式化。

维护成本主要来自临时 façade 和更多模块文件。通过稳定的依赖方向、明确 `__all__`、兼容 identity 测试以及按需保留 façade，可以避免新结构退化成仅仅“把同一段复杂度分散到更多文件”。
