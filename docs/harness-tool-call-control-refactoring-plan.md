# Harness Tool-Call Control 改造方案

> 状态：Proposed，待实施批准
>
> 日期：2026-08-23
>
> 范围：Harness Execution 中重复工具调用保护、单工具调用预算、Run
> Workload Profile、Run Event 与用户可见失败原因
>
> 证据基线：当前源码、focused tests，以及 2026-08-23 本地验收中记录的
> Run/Event/Tool 行为；实施所需的关键事实已在本文中自包含。

## 1. 决策摘要

采用“**双内核、单接入点**”设计：

- `RepeatedCallGuard` 只识别规范化后完全相同的工具调用集合，拥有真正的
  loop detection 语义；
- `ToolCallBudget` 只管理每种工具获得执行准入的调用次数，拥有资源预算语义；
- `ToolCallControl` 是两者在 Agent Graph 中的唯一外部 Interface，位于模型提出
  完整工具调用批次之后、ToolNode 执行之前；
- 两个内核共同扫描一次完整批次，并由一个 Adapter 统一修改 `AIMessage`、同步
  provider metadata、保存 checkpoint receipt、注入 warning 和生成 observation；
- `SubagentLimitMiddleware`、进程级 Sub-Agent scheduler gate、Token Budget、
  approval、timeout、Read-before-write 与 tool-output budget 保持独立。

这个方案修复当前 `LoopDetectionMiddleware` 同时拥有循环检测和工具频率预算的
ownership 混合，同时避免引入包含所有执行限制的“大 Guard”或通用规则 DSL。

## 2. 事实、判断与待验证假设

### 2.1 已确认事实

1. 当前 [`LoopDetectionConfig`](../backend/packages/harness/deerflow/config/loop_detection_config.py)
   同时包含：

   - 相同调用集合的 `warn_threshold`、`hard_limit` 与 `window_size`；
   - 同名工具累计次数的 `tool_freq_warn`、`tool_freq_hard_limit` 与
     `tool_freq_overrides`。

2. 当前 warning 文案明确要求模型停止调用工具并立即回答，不是普通提醒：
   [`loop_detection_middleware.py`](../backend/packages/harness/deerflow/agents/middlewares/loop_detection_middleware.py#L240)。

3. 当前 frequency 扫描在一个并行 tool-call batch 中遇到首个 warning 或 hard
   limit 后立即返回，后续 occurrence 没有完整计数：
   [`loop_detection_middleware.py`](../backend/packages/harness/deerflow/agents/middlewares/loop_detection_middleware.py#L539)。
   这是配置 hard limit 为 10、实际执行出现 11 或 12 次的直接原因。

4. 当前计数 key 是 `thread_id`，不是明确的 Run、Sub-Agent Task 或 SDK
   invocation scope。Private Run 通常重新建图，因此尚未证明 Private 路径存在
   跨 Run 泄漏；但是 SDK/Embedded 缓存图可能让计数跨 invocation 累积，新设计
   不能继续依赖 middleware 实例生命周期形成的偶然 scope。

5. 当前 PostgreSQL System Runtime Policy schema version 为 3，并保留 version 2
   reader：[`validation.py`](../backend/app/system_runtime_settings/validation.py#L24)。
   Run Admission 冻结 exact runtime-policy version，Worker 从 Run Snapshot
   materialize，而不是执行时读取当前策略。

6. 仓库已有 `RequestedRunExecutionProfile` 与 `EffectiveRunExecutionProfile`，
   专门表示 model、thinking 与 reasoning effort：
   [`execution_profile.py`](../backend/app/private_work/execution_profile.py#L11)。
   因此 `interactive/research` 不能继续使用 `Run Execution Profile` 这个名称。

7. 当前 Lead 与 Sub-Agent 分别装配新的 `LoopDetectionMiddleware` 实例，但消费
   同一个 `AppConfig.loop_detection` 数值策略：

   - Lead：[`lead_agent/agent.py`](../backend/packages/harness/deerflow/agents/lead_agent/agent.py#L482)；
   - Sub-Agent：[`middlewares/assembly.py`](../backend/packages/harness/deerflow/agents/middlewares/assembly.py#L773)。

8. `SubagentLimitMiddleware` 管理的是每次响应并发委托和每 Run 委托总量；进程级
   scheduler 管理的是执行容量。两者不是工具调用预算，也不属于
   `SubagentTaskLifecycle` 的同一个 policy ownership。

### 2.2 设计判断

- Harness 是 enforcement 的正确位置，因为它位于模型 proposal 与实际工具执行
  之间，可以在副作用发生前阻止调用。
- PostgreSQL System Runtime Policy 继续拥有阈值；Run Admission 继续拥有冻结；
  Harness 只消费不可变的 resolved policy。
- warning 必须是 advisory；只有 hard decision 才能改变工具执行集合。
- 工具预算耗尽不等于循环，不能自动把 Lead Run 终止为
  `LOOP_SAFETY_LIMIT`。
- 工具预算达到 hard limit 后，只禁用耗尽的工具；`write_file`、
  `present_files` 等仍应可用，以便 Lead 整理和交付已有结果。

### 2.3 未验证假设

- `research` Lead 的 Web 阈值 20/30、Sub-Agent 的 12/20、每 Run 最多 9 个
  Sub-Agent Task 只是首轮验收值，不是已经证明的最优值。
- 一次深度研究 Run 证明当前全局 Web 6/10 会误伤正常工作，但不能单独证明新的
  阈值在完成率、成本和耗时之间最优。
- 是否需要把 `research` 作为所有 Project 的默认可用产品动作，仍需结合产品配额
  与权限政策确认；本方案只要求选择来源必须受 Gateway 校验。

## 3. 目标与非目标

### 3.1 目标

- 将循环检测与工具调用预算拆成两个拥有独立语义和原因码的内核；
- 对一个完整 tool-call batch 原子、确定性地计算准入与拒绝集合；
- 精确保证每种工具的 admitted occurrence 不超过 hard limit；
- 对 checkpoint replay、Job Attempt 恢复和并行 Sub-Agent 保持幂等与 scope
  隔离；
- warning 不再命令停止工具；
- 预算耗尽后 Lead 仍能写报告和交付文件；
- Run Event、Sub-Agent outcome 和 UI 能明确显示直接原因；
- Private、configured Lead、SDK、Embedded 与 Sub-Agent 共用一个 Harness
  Interface；
- 保持 v2/v3 Run Snapshot 可读取，并以一次 ownership cutover 删除旧 frequency
  enforcement 生产路径。

### 3.2 非目标

- 不重新打开 Candidate 05 的 Agent Graph construction profile 重构；
- 不改变已完成的 Sub-Agent Task lifecycle ownership；
- 不把 `SubagentLimitMiddleware` 并入 `ToolCallControl`；
- 不把 process-wide scheduler gate 并入 Harness graph policy；
- 不修改 token、recursion、approval、timeout、provider retry 或文件 authority
  语义；
- 不建立管理员可编程规则 DSL、规则继承、工具 taxonomy 或 PostgreSQL 调用计数
  ledger；
- 不根据提示词、Skill 名称、`private_scope` 或模型输出推断 research；
- 不通过运行时自动写入、迁移或修复 PostgreSQL 策略。

## 4. 备选设计比较

| 方案 | Depth 与 Locality | 结论 |
| --- | --- | --- |
| 两个完全独立 Middleware | 实现简单，但两个 Middleware 都要扫描并修改同一个 `AIMessage`，排序、metadata 同步和 finalization 仍分散 | 不采用 |
| 一个包含 loop、tool budget、delegation、token 和 safety 的统一 Guard | 外部入口少，但 Interface 必须公开多种 scope、结算与 terminal 语义，形成新的 ownership 混合 | 不采用 |
| 编译式 Tool Governance DSL | 对未来任意规则最灵活，但需要规则语言、冲突优先级、taxonomy、compiler 和 snapshot program；当前没有第三类已确认规则 | 不采用 |
| **双内核、单 `ToolCallControl` Adapter** | 两个内核语义独立；批次改写、replay receipt、warning 与 observation 集中在一个真实 seam | **采用** |

删除 `ToolCallControl` 后，完整批次仲裁、raw metadata 同步、checkpoint receipt、
warning 队列、scope 管理和 stop projection 会重新散落到 Lead、Sub-Agent、SDK
与 Embedded 构图路径，因此它能通过 deletion test。

## 5. 领域术语与 ownership

### 5.1 新术语

**Run Workload Profile**：Run Admission 签发并冻结的工作负载政策选择。首版只允许
`interactive` 与 `research`，控制角色相关的工具调用预算和 Sub-Agent 委托总量。
它不是新的 Run 类型，也不同于控制 model/thinking 的 Run Execution Profile。

实施时应把该术语加入 [`CONTEXT.md`](../CONTEXT.md)。

### 5.2 ownership

```text
System Administrator
        │ 编辑 immutable System Runtime Policy version
        ▼
app.system_runtime_settings
        │ 拥有 policy model、schema、校验和默认值
        ▼
Run Admission
        │ 校验 RequestedRunWorkloadProfile
        │ 冻结 EffectiveRunWorkloadProfile + exact policy version/checksum
        ▼
Worker materializer
        │ 只从 Run Snapshot 解析 ResolvedToolCallControlPolicy
        ▼
ToolCallControl（Harness）
        │ 执行 repeated-call guard 与 per-tool budget
        ▼
ToolNode / Sub-Agent lifecycle observation / Run Journal
```

不变量：

- `app.*` 可以依赖 `deerflow.*`，Harness 不能导入 `app.*`；
- Harness 不查 PostgreSQL、不选择 workload profile、不知道管理员或 policy
  revision；
- `allow` 只表示通过本 Module，不能授予不存在的工具、capability、approval、
  Sandbox 或文件 authority；
- Run Snapshot 无法解析、checksum 不符或 profile 非法时，在 Harness Execution
  前 fail closed；
- observation 失败不能重新放行工具，也不能制造第二个 semantic outcome；
- Sub-Agent scheduler loop 不能直接调用 owner-loop Run Journal；observation 通过
  lifecycle/binding 交回 parent owner loop。

## 6. Harness Module 设计

### 6.1 外部 Interface

```python
from dataclasses import dataclass
from typing import Literal, Protocol

type ToolCallControlRole = Literal["lead", "subagent"]


@dataclass(frozen=True, slots=True)
class ToolCallControlBinding:
    role: ToolCallControlRole
    scope_id: str
    observer: "ToolCallControlObserver"


class ToolCallControlObserver(Protocol):
    def observe(self, observation: "ToolCallControlObservation") -> None: ...


def build_tool_call_control(
    policy: "ResolvedToolCallControlPolicy",
    binding: ToolCallControlBinding,
) -> "AgentMiddleware": ...
```

`build_tool_call_control()` 是构图调用方和 Interface acceptance 的主要入口。
调用方不需要知道 fingerprint、counter、warning queue、proposal receipt、
metadata rewrite 或 finalization state。

实现内部可以使用下列纯值决策 Interface，但不把它们暴露给构图调用方：

```python
@dataclass(frozen=True, slots=True)
class ToolCallDecision:
    admitted_calls: tuple[ToolCall, ...]
    rejected_calls: tuple[RejectedToolCall, ...]
    warnings: tuple[ToolCallWarning, ...]
    exhausted_tools: frozenset[str]
    loop_stop: LoopStopReceipt | None
    observations: tuple[ToolCallControlObservation, ...]
    next_state: ToolCallControlState
```

### 6.2 Interface 后隐藏的 Implementation

- 保留当前经过测试的 tool identity normalization；
- 对完整批次计算 order-independent multiset fingerprint；
- 按模型原始顺序为每个 occurrence 分配稳定 proposal receipt；
- 完整扫描所有工具调用，不在 warning/hard 命中时提前返回；
- 确定性保留每种工具仍有额度的前缀；
- 同步 LangChain decoded `tool_calls`、`invalid_tool_calls` 和 provider raw
  metadata；
- 在 ToolMessage pairing 完成后注入 advisory；
- 对耗尽工具更新下一次 model request 的可见工具集合；
- 保存 warning crossing、exhausted tool、loop finalization 与 replay receipt；
- 投影 Lead/Sub-Agent stop receipt 和安全、低基数 observation；
- 在 full/delta checkpoint 模式下提供相同 materialized state 语义。

建议首版使用一个 `tool_call_control.py` Module。只有当 Implementation 规模确实
要求拆文件时，才在同一 package 内拆成 private files；不要把内部两个内核变成
新的公共 seam。

## 7. 执行顺序与批次算法

### 7.1 有效执行顺序

LangGraph `after_model` 的 dispatch 顺序与注册顺序相反，实施时应以 assembly
contract test 锁定下列有效顺序，而不是仅依赖注释：

```text
Safety/terminal response cleanup
        ↓
Token Budget hard-stop cleanup（如已触发）
        ↓
SubagentLimitMiddleware（先过滤不可能执行的 task proposal）
        ↓
ToolCallControl
        ↓
ToolNode
```

不变量：

- 被 Safety、Token Budget 或 delegation policy 删除的调用不消费普通工具预算；
- `task` 不进入 `ToolCallBudget`，但被允许的 `task` 仍可进入 repeated-call
  fingerprint；
- `ToolCallControl` 修改后，ToolNode 不得看到任何被拒 occurrence；
- 一个 `AIMessage` 最多由 `ToolCallControl` 做一次最终批次改写。

### 7.2 RepeatedCallGuard

- 输入：上游 policy 已过滤后的完整有效批次；
- identity：复用当前已覆盖测试的规范化规则；
- warning：同一 fingerprint 在窗口内第 3 次出现；
- hard stop：同一 fingerprint 在窗口内第 5 次出现；
- hard stop 拒绝整个批次，保留现有一次且仅一次 tool-free finalization；
- Lead 记录 `LOOP_SAFETY_LIMIT`；
- Sub-Agent 产生 `completed + loop_capped`；
- permutation 相同但参数完全一致的批次仍视为同一集合；不同 query、URL 或
  authenticated content mark 不视为相同调用。

### 7.3 ToolCallBudget

预算单位定义为“获得 ToolNode 执行准入的 tool-call occurrence”：

- admitted occurrence 立即消费一次预算；
- 后续工具执行失败不退款；
- rejected occurrence 不消费；
- 同一 proposal 的 checkpoint replay 不重复消费；
- tool-call ID 只是 correlation metadata，不能单独作为 replay identity；
- `web_search` 与 `web_fetch` 分别计数，不合并为 Web 总数；
- 一个工具达到 hard limit 不影响同批其他工具。

假设 `web_search.hard_limit=10`：

| prior | 当前 batch 中 `web_search` 数量 | admitted | rejected | after |
| ---: | ---: | ---: | ---: | ---: |
| 5 | 3 | 3 | 0 | 8 |
| 9 | 3 | 1 | 2 | 10 |
| 10 | 2 | 0 | 2 | 10 |

算法：

1. 对完整有效批次建立 occurrence 列表；
2. 先执行 RepeatedCallGuard；若 loop hard stop，整个批次拒绝且该 semantic
   outcome 胜出；
3. 否则逐 occurrence 解析角色相关的有效 per-tool limit；
4. 按模型原始顺序保留仍有额度的确定性前缀；
5. 完整扫描剩余 occurrence，生成所有 rejected receipt 和 observation，不提前
   返回；
6. 统一改写一次 decoded calls 与 raw provider metadata；
7. 先执行保留项；ToolMessage pairing 完成后，在下一次 model request 注入一次
   advisory/exhaustion notice；
8. 后续 model request 不再暴露已经耗尽的工具，但继续暴露未耗尽工具；
9. 如果没有任何 admitted call，Adapter 直接发起一次后续 model turn，使模型能
   用现有证据回答，而不是把内部拒绝 placeholder 当作最终回答。

预算耗尽不自动发起“禁用所有工具”的 finalization。只有真正的 loop hard stop
拥有全工具禁用 finalization。

## 8. Scope、checkpoint 与重放

| 调用场景 | scope | 生命周期 |
| --- | --- | --- |
| Private Lead | exact `run_id` 对应的 Run scope | 跨同一 Run 的 Graph Turn、Goal Continuation 和 Job Attempt 恢复保持；新 Run 清零 |
| Sub-Agent | `SubagentTaskLifecycle` 生成的内部 `execution_id` | 每个 Sub-Agent Task 独立；不得使用公开 `task_id/tool_call_id` 作为 registry identity |
| SDK | graph invocation ID | 每次 `invoke/stream` 清零 |
| Embedded | graph invocation ID | 每次调用清零；不能退化为共享 `default` 或 Thread scope |

Lead 私有状态至少包含：

```text
policy contract version
scope ID
recent repetition fingerprints
per-tool admitted counts
warning receipts
exhausted tools
proposal replay receipts
loop finalization phase
```

实现要求：

- 状态必须是明确的 private graph-state channel；
- 新 state channel 必须有 full/delta 兼容测试；
- Job Attempt 恢复读取 checkpoint，而不是依赖进程内 `_history/_tool_freq`；
- 移除 `max_tracked_threads` 及其 LRU 作为产品策略的必要性；
- Sub-Agent 没有独立 durable Run，但其 Task 内状态必须绑定内部
  `execution_id`，并在 lifecycle outcome 结算前保持可用；
- replay receipt 使用内部 occurrence identity，不假设 tool-call ID 全局唯一。

## 9. Run Workload Profile 与 Policy v4

### 9.1 Admission 类型

建议在 `app.private_work` 新增独立 Module，例如
`workload_profile.py`：

```python
type RunWorkloadProfileName = Literal["interactive", "research"]

RUN_WORKLOAD_PROFILE_KWARG = "__run_workload_profile"


@dataclass(frozen=True, slots=True)
class RequestedRunWorkloadProfile:
    name: RunWorkloadProfileName = "interactive"


@dataclass(frozen=True, slots=True)
class EffectiveRunWorkloadProfile:
    name: RunWorkloadProfileName
```

`Requested` 只是非权威请求；Gateway 根据 authenticated identity、Project
context、capability 和 quota policy 校验后，才生成 `Effective` 并写入
server-owned kwargs。公共 metadata、model output 和 tool args 中的同名字段一律
不能成为 authority。

普通 Private Run 默认 `interactive`。明确的“深度研究”产品动作请求
`research`；Continuation Run 复制 source Run 的 effective profile。Automation、
Channel 与 Skill Builder 首版继续默认 `interactive`，除非它们以后建立各自明确
的 admission 选择。

### 9.2 System Runtime Policy schema v4

建议形状：

```yaml
schema_version: 4
value:
  agent_runtime:
    loop_detection:
      enabled: true
      identical_calls:
        warn_threshold: 3
        hard_limit: 5
        window_size: 20

    tool_call_budget:
      profiles:
        interactive:
          lead:
            default: {warn: 30, hard_limit: 50}
            tools:
              web_search: {warn: 6, hard_limit: 10}
              web_fetch: {warn: 6, hard_limit: 10}
              recall_memory: {warn: 6, hard_limit: 10}
              inspect_image: {warn: 6, hard_limit: 9}
          subagent:
            default: {warn: 30, hard_limit: 50}
            tools:
              web_search: {warn: 6, hard_limit: 10}
              web_fetch: {warn: 6, hard_limit: 10}
              recall_memory: {warn: 6, hard_limit: 10}
              inspect_image: {warn: 6, hard_limit: 9}

        research:
          lead:
            default: {warn: 30, hard_limit: 50}
            tools:
              web_search: {warn: 20, hard_limit: 30}
              web_fetch: {warn: 20, hard_limit: 30}
              recall_memory: {warn: 6, hard_limit: 10}
              inspect_image: {warn: 6, hard_limit: 9}
          subagent:
            default: {warn: 30, hard_limit: 50}
            tools:
              web_search: {warn: 12, hard_limit: 20}
              web_fetch: {warn: 12, hard_limit: 20}
              recall_memory: {warn: 6, hard_limit: 10}
              inspect_image: {warn: 6, hard_limit: 9}

    subagents:
      max_concurrent: 3
      max_total_per_run_by_workload:
        interactive: 6
        research: 9
```

首版使用显式、强类型、完整展开的 profile，不建立 `extends`、priority、正则或
任意 prompt 配置。

### 9.3 配置不变量

- `warn >= 1`；
- `hard_limit >= warn`；
- 每个 profile 必须同时提供 Lead 与 Sub-Agent policy；
- 未知 profile、未知字段和 malformed override fail closed；
- 未知工具使用该角色的 `default`；
- `task` 明确排除在 `ToolCallBudget` 外；
- `inspect_image` 的有效 hard limit 为 policy 值与下层真实技术 cap 的较小值；
- Sub-Agent concurrency 继续受当前 `1..4` canonical clamp；
- exact Agent Version 声明的并发委托值只能收紧 System Runtime Policy ceiling，
  Run Admission 冻结两者的较小值；
- per-Run total 继续受当前 `1..50` canonical clamp；
- Harness 只接收选中 profile 的 resolved、frozen value，不接收整个 catalog 后再
  自行选择。

## 10. Warning、hard decision 与 outcome

### 10.1 Warning

Warning 必须是中性 advisory，例如：

```text
You have used 8 of 10 web_search calls for this execution.
Review existing evidence and reserve the remaining calls for material gaps.
Continue when another call adds new evidence.
```

重复调用 warning：

```text
The same tool-call set has appeared 3 times.
Change strategy or arguments, or finish with the evidence already collected.
```

禁止包含 `Stop calling tools`、`produce your final answer now` 等强制语言。

Warning 必须在当前批次的 ToolMessage pairing 完成后，通过下一次 model request
注入；不能修改尚未配对的 `AIMessage` content，也不能在 AI tool calls 和
ToolMessage 之间插入普通消息。

### 10.2 Hard decision

| 情况 | 执行动作 | Lead | Sub-Agent |
| --- | --- | --- | --- |
| repeated-call hard stop | 拒绝整个批次；一次 tool-free finalization | terminal `LOOP_SAFETY_LIMIT` | `completed + loop_capped` |
| 单工具预算耗尽 | 拒绝该工具超额 occurrence；后续隐藏该工具；其他工具继续 | 不自动 terminal；记录 budget receipt | `completed`，并携带 `tool_budget_capped` |
| loop finalization 再次调用工具 | 不执行；fail closed | `LOOP_FINALIZATION_FAILED`，保留 loop direct cause | `failed` |
| policy/scope state 无法解析 | Harness Execution 前拒绝或 graph fail closed | `RUN_POLICY_STALE` / stable state code | Task 不创建或 `failed` |

如果预算事件之后又发生 model/provider、tool、approval 或 file finalization 失败，
最终 terminal 的 direct cause 必须使用更具体的失败；budget receipt 仅作为
contributing observation，不能覆盖根因。

## 11. Run Event、诊断与用户文案

### 11.1 稳定原因码

至少区分：

```text
repeated_call_warning
repeated_call_limit
tool_budget_warning
tool_budget_exhausted
subagent_total_limit
tool_execution_failed
model_provider_failed
run_policy_stale
tool_call_control_state_invalid
```

### 11.2 Tool budget observation

建议 Run Event kind：`middleware:tool_call_budget`。

安全 payload：

```text
schema_version
reason_code
workload_profile
role
run_id
execution_id（Sub-Agent 时使用内部 ID 的安全投影）
tool_name
count_before
proposed
admitted
rejected
count_after
warn_threshold
hard_limit
disposition
```

禁止记录：

- tool args；
- query、URL、shell command；
- tool output 或 prompt；
- secret、storage locator、raw exception；
- 可导致进程 metrics 高基数的任意 Agent 展示名称。

`observation_id` 应从 execution、proposal occurrence、rule kind 与 action
确定性生成，使 Run Journal 可以幂等去重。只能保证同一 receipt 的 observer 最多
尝试一次，不能宣称没有持久化确认的 durable exactly-once delivery。

### 11.3 用户可见失败原因

Adapter presentation 负责把稳定原因码转换为用户文案；Harness lifecycle 不拥有
中文或英文产品文案。

UI 必须明确区分：

- 检测到真正的重复循环；
- 某种工具额度已用尽，但 Run 仍在整理结果；
- Sub-Agent 委托总数达到上限；
- 模型/provider 失败；
- 工具自身失败；
- Run Snapshot 或 policy 无法物化。

## 12. 兼容、部署与回滚

### 12.1 Policy 兼容

- 保持 v2/v3 的 canonical JSON shape 和 checksum 计算完全不变；
- 添加独立 v2/v3 decoder，把旧 `loop_detection` 数值映射为新的内部
  `ResolvedToolCallControlPolicy`；
- 旧 payload 一律映射为 `interactive`，不能猜测 `research`；
- 兼容 decoder 保留旧数值，但使用修复后的 advisory warning 和精确批次语义；
- 不修改历史 policy row 或 Run Snapshot；
- v4 是 runtime-policy JSON schema 演进，不是 PostgreSQL Application Schema V1
  物理变更；按本方案不新增表或列。

### 12.2 一次 enforcement 路径

切换完成后：

- 新 `ToolCallControl` 同时接管 repetition、per-tool budget、scope、warning、
  exact batch arbitration 和 replay receipt；
- 旧 `LoopDetectionMiddleware` 的 frequency fields 和生产执行路径删除；
- 不允许 `ToolCallControl` 再调用旧 `LoopDetectionMiddleware`，否则只是 shallow
  wrapper；
- `SubagentLimitMiddleware` 继续单独装配；
- 旧测试由新的 Interface acceptance 取代，保留必要 wire/compatibility tests。

### 12.3 SDK/Embedded

- `RuntimeFeatures.loop_detection=True` 在一个兼容窗口内映射为默认
  `ToolCallControl`；
- `False` 保留现有明确关闭行为；
- 自定义 middleware 使用者迁移到 `extra_middleware` 或显式 full takeover；
- SDK/Embedded 可以显式选择 `research`，但这只是 caller-owned、单次 invocation
  配置，不能称作 server-issued Run Snapshot；
- Sub-Agent profile 通过已完成的 profile-specific binding factory 传递，不能从
  `private_scope` 重建。

### 12.4 部署顺序

1. drain 当前 Worker，避免同一 Run 在新旧 warning 语义之间跨版本恢复；
2. 部署能读取 v2/v3/v4 的 Gateway 与 Worker；
3. 验证 v2/v3 当前策略 materialization；
4. 通过管理员显式创建并激活 v4；
5. 运行 interactive 与 research smoke acceptance；
6. 观察完成率、预算事件与成本后再调整阈值。

旧 Worker 不理解 v4，因此 v4 激活后不能直接回滚到不支持 v4 的二进制。回滚前
必须先停止新 Run Admission，并恢复一个仍受新旧 Worker 共同理解的 active policy
version；不得手工修改数据库行或 checksum。

## 13. 分阶段实施计划

### 阶段 0：锁定当前 wire 与缺陷

先写失败测试并观察预期失败：

- warning 文案会命令停止工具；
- `prior=9, batch=3, hard=10` 会越过 hard limit；
- 一个 batch 中首个 warning 使后续 occurrence 未计数；
- 当前 Lead/Sub-Agent stop reason 与 Run Event sequence；
- 当前 middleware registration 与反向 dispatch 顺序；
- SDK/Embedded 图复用下的 invocation scope。

这一步只锁定外部行为和已确认缺陷，不把错误行为永久化为新规范。

### 阶段 1：Policy v4 与 Run Workload Profile

- 新增 typed `interactive/research` catalog；
- 新增 Requested/Effective Run Workload Profile；
- 将 selector 写入独立 server-owned kwarg，并纳入 admission idempotency 比较；
- 更新 System Runtime Policy model、validation、materializer、bootstrap payload、
  管理员 draft/edit UI 与 tests；
- 保持 v2/v3 checksum fixture 不变；
- 普通路径先固定 `interactive`，没有受信任产品选择时不得自动进入 research。

### 阶段 2：离线建立 ToolCallControl

- 先实现 immutable policy、state、decision、receipt 与 observation；
- 实现完整批次扫描、deterministic prefix、metadata 同步和 replay 幂等；
- 实现 advisory queue、exhausted-tool filtering 与 loop finalization；
- 使用真实小图和 in-memory checkpointer 从外部 Interface 验证；
- 不连接生产构图路径，不在旧 Module 外包一层。

### 阶段 3：一次 ownership cutover

- Lead、Sub-Agent、configured Lead、SDK 与 Embedded 同时切换到
  `build_tool_call_control()`；
- 在同一阶段删除旧 tool-frequency 生产路径；
- 保留 `SubagentLimitMiddleware` 与 scheduler gate；
- 更新 middleware assembly golden/contract tests；
- Sub-Agent outcome 增加 `tool_budget_capped`，保持 frozen/closed invariants。

### 阶段 4：Run Event 与 UI

- 新增稳定、安全、幂等的 tool budget observation；
- Run Journal 在 terminal event 之前投影 observation；
- UI 分开展示 loop、budget、delegation 和 provider/tool failure；
- 管理页面按 workload profile 与 Lead/Sub-Agent 展示生效值；
- research 产品动作由 Gateway 校验并冻结，不接受 metadata authority。

### 阶段 5：策略激活与复杂验收

- 管理员显式激活 v4；
- 先跑受控低阈值 negative acceptance，证明 hard limit 精确；
- 再跑真实 research acceptance，证明合法深度研究不会被旧 6/10 错误截停；
- 根据多个 Run 的完成率、耗时、token、Web 调用和 Artifact 交付结果调整阈值；
- 完成后删除已过兼容窗口的 SDK legacy field 和旧 config 类型。

## 14. 预计修改范围

### Application / Run Admission

- [`backend/app/system_runtime_settings/models.py`](../backend/app/system_runtime_settings/models.py)
- [`backend/app/system_runtime_settings/validation.py`](../backend/app/system_runtime_settings/validation.py)
- [`backend/app/system_runtime_settings/materializer.py`](../backend/app/system_runtime_settings/materializer.py)
- 新 `backend/app/private_work/workload_profile.py`
- Private Run Admission 的请求、idempotency 与 server-owned kwargs 组装路径

### Harness

- 新 `backend/packages/harness/deerflow/agents/middlewares/tool_call_control.py`
- [`backend/packages/harness/deerflow/agents/middlewares/loop_detection_middleware.py`](../backend/packages/harness/deerflow/agents/middlewares/loop_detection_middleware.py)
  的 production replacement/deletion
- [`backend/packages/harness/deerflow/agents/middlewares/assembly.py`](../backend/packages/harness/deerflow/agents/middlewares/assembly.py)
- [`backend/packages/harness/deerflow/agents/lead_agent/agent.py`](../backend/packages/harness/deerflow/agents/lead_agent/agent.py)
- [`backend/packages/harness/deerflow/agents/factory.py`](../backend/packages/harness/deerflow/agents/factory.py)
- [`backend/packages/harness/deerflow/agents/features.py`](../backend/packages/harness/deerflow/agents/features.py)
- [`backend/packages/harness/deerflow/config/app_config.py`](../backend/packages/harness/deerflow/config/app_config.py)
- [`backend/packages/harness/deerflow/config/loop_detection_config.py`](../backend/packages/harness/deerflow/config/loop_detection_config.py)
  的 compatibility/deletion path
- Sub-Agent binding、status contract 与 lifecycle observation Adapter

### Frontend 与文档

- [`frontend/src/components/admin/settings/admin-system-settings-page.tsx`](../frontend/src/components/admin/settings/admin-system-settings-page.tsx)
- chat composer 与 private-work request types 中的受限 research 产品动作
- Run failure/budget presentation 与 i18n
- [`CONTEXT.md`](../CONTEXT.md)
- [`backend/AGENTS.md`](../backend/AGENTS.md)
- Harness Execution 与管理员配置说明

### 测试

- 新 `backend/tests/test_tool_call_control.py`
- 新/更新 runtime-policy v2/v3/v4、Run Workload Profile 与 materializer tests
- 更新 middleware assembly、Lead、Sub-Agent、SDK、Embedded acceptance
- 更新 Run Journal、terminal outcome 与 browser E2E tests

实际实施前先用 `rg` 定位每条生产装配路径；这里的文件清单是预计范围，不授权
删除或覆盖工作区中的其他未提交修改。

## 15. 自动化验收矩阵

### 15.1 Repeated-call

- 同一调用集合第 3 次 warning、第 5 次 hard stop；
- permutation 不改变 fingerprint；
- 不同 query、URL、arguments 不构成 identical loop；
- 现有 read/write/tool identity 特殊规则保持；
- loop hard stop 只有一次 tool-free finalization；
- finalization 再次提出工具 fail closed；
- Lead 与 Sub-Agent stop reason 投影正确。

### 15.2 Tool budget

- `prior=5, batch=3, warn=6`：全部执行，after=8，只产生一次 advisory；
- `prior=9, batch=3, hard=10`：只准入 1、拒绝 2、after 精确为 10；
- mixed `web_search/web_fetch/write_file` 分别计数并保留可执行调用；
- 同批多个工具跨阈值时仍完整扫描；
- 工具执行失败消费已准入预算；
- rejected occurrence 不消费；
- exhausted Web 工具不阻止 `write_file/present_files`；
- 任意批次与 replay 序列始终满足 `admitted_count <= hard_limit`。

### 15.3 Scope 与 replay

- 同一 Run 的新 Graph Turn 延续计数；
- Goal Continuation 延续同一 Run 计数；
- 新 Run 即使属于同一 Thread 也清零；
- 三个 Sub-Agent Task 使用三个内部 execution scope；
- 同一 proposal checkpoint replay 不重复消费；
- tool-call ID 重用不造成 receipt 碰撞；
- SDK/Embedded 每个 graph invocation 清零；
- full/delta checkpoint mode 行为一致。

### 15.4 Policy 与 authority

- v2/v3 checksum 与 canonical fixture 不变；
- v4 round-trip、unknown field、unknown profile、边界值与 checksum tests；
- current policy 变化不影响已 admission 的 Run；
- request metadata、model output、tool args 无法选择 research；
- Continuation Run 继承 source Run profile；
- malformed/forged profile fail closed；
- `inspect_image` 无法通过 policy 放宽底层 cap。

### 15.5 Event 与 outcome

- warning 不含停止命令；
- observation 幂等且在 terminal 前有序发布；
- observation 不含 args、URL、command、prompt、secret 或 raw exception；
- tool budget exhaustion 不把成功 Lead Run 改成 `LOOP_SAFETY_LIMIT`；
- Sub-Agent `completed + tool_budget_capped` 被父 Lead 保留；
- model/provider failure 不被较早 budget receipt 覆盖；
- observer 失败不改变 enforcement decision 或 semantic outcome。

## 16. 复杂真实验收

### 16.1 前置条件

- 使用本地验收管理员；
- 用户已同意向本地配置的外部模型和 Web provider 发送测试提示词；
- 新建独立 Thread 和 Run，不复用历史验收 Run；
- 明确选择 `research`；
- 记录 Thread、Run、Job、Job Attempt 与 Sub-Agent Task 坐标；
- 浏览器证据、Run Event/PostgreSQL 证据、代码证据和 provider 证据分开记录；
- 不把 offline/mock test 当作真实外部模型或 Web 验收。

### 16.2 测试提示词

```text
请深度调研智能 Agent 从早期符号主义、规划系统、BDI 与多智能体，
到强化学习 Agent、ReAct/tool use、LLM Agent、Multi-Agent orchestration
和 Computer Use 的发展过程。

要求：
1. 建立从 1950 年代至今的时间线；
2. 解释关键范式变化、代表系统、能力边界与主要争议；
3. 至少完成 4 个可独立核验的 Sub-Agent Task；
4. 引用至少 24 个可信的一手或官方来源，并区分论文、官方文档与二手资料；
5. 对每一次模型或工具失败写明直接原因，不确定时明确标为未验证；
6. 生成结构化 Markdown 报告，写入 outputs/agent-evolution-research.md；
7. 校验文件行数、UTF-8 字节数、标题结构、URL 数和 SHA-256；
8. 使用 present_files 交付文件。
```

### 16.3 正向验收标准

- 至少 4 个 Sub-Agent Task 被实际 admission 并产生 start/end；
- 合法、参数不同的 Web 调用超过旧的 10 次上限时不被判为 loop；
- 所有 per-tool admitted count 都不超过 research hard limit；
- warning 为 advisory，Lead 不因 warning 停止所有工具；
- Lead 最终执行 `write_file` 和 `present_files`；
- 至少 24 个来源经独立抽查仍属于一手/官方来源；
- Run、Job、Job Attempt、Run Event 与 browser terminal 一致；
- 文件 preview 可打开，刷新页面后仍存在；
- PostgreSQL/Run Journal 的 usage、task receipt 与 aggregate 不重复计费；
- 没有把 Sub-Agent failure 错误提升为 Lead terminal direct cause。

### 16.4 负向验收

使用一个明确受控、验收后恢复的低阈值 policy version：

- repeated-call 第 5 次精确 hard stop；
- per-tool prior=limit-1 且 batch>1 时只准入剩余 1 次；
- hard limit 后同批 `write_file` 仍能执行；
- Run Event 精确记录 proposed/admitted/rejected；
- 刷新后 observation 和 terminal 不重复；
- 恢复原 active policy version 后再做一次 readback。

## 17. 失败调查与报告模板

复杂验收中的每一次失败都必须单独输出：

```text
Observed failure:
  用户或系统实际观察到什么

Execution phase:
  Run Admission / materialization / Lead model / Sub-Agent Task /
  tool execution / checkpoint / File Finalization / Run Settlement / browser

Stable reason code:
  一个闭合原因码

Confirmed direct cause:
  由代码、durable event、数据库或真实 provider evidence 直接支持的原因

Evidence:
  精确 Run/Task/event/test/file 坐标，不包含 secret 或 raw private content

Contributing observations:
  warning、budget receipt、recovered retry 等非 terminal 根因

Unverified hypotheses:
  尚无证据支持的可能性；不能写成已确认原因

Fix:
  实际修改了什么 ownership、interface 或 implementation

Re-verification:
  focused test / full backend / PostgreSQL / browser / external provider
  中哪些已经重新通过
```

原则：如果只有 `Connection error.` 之类经过 redaction 的事实，就只能报告
“无法区分 provider、proxy 或 host network”；不能自行补全根因。一个后续更具体的
terminal failure 必须覆盖较早的 warning/budget receipt 作为 direct cause。

## 18. 验证命令与交付门槛

实施阶段遵循 backend strict TDD：先写 focused failing test、观察失败、完成最终
生产路径、再运行 focused/affected tests。

至少运行：

```bash
cd backend
uv run pytest tests/test_tool_call_control.py -q
uv run pytest <affected policy/profile/journal/lifecycle tests> -q
uvx ruff format --check .
uvx ruff check .
make detect-blocking-io
make test

cd ../frontend
pnpm check
pnpm test <affected unit tests>

cd ..
git diff --check
```

`make test` 必须使用非生产 development PostgreSQL，并以 core suite zero skips
完成。之后再运行本地真实浏览器 acceptance；focused/offline tests 不能替代
PostgreSQL、浏览器、外部模型或 Web provider 证据。

## 19. Definition of Done

只有同时满足以下条件，改造才算完成：

- 循环检测和工具预算拥有不同类型、事件、文案和 outcome 语义；
- 所有生产 Agent Graph profile 都只通过一个 `ToolCallControl` Interface；
- 旧 tool-frequency enforcement 不再存在于生产路径；
- 完整批次不会因首个 warning/hard 命中而提前停止计数；
- property/acceptance tests 证明 admitted occurrence 永不超过 hard limit；
- Run、Sub-Agent Task、SDK/Embedded invocation scope 明确且 replay 幂等；
- warning 不再要求停止工具；
- Web 预算耗尽不阻止报告写入和文件交付；
- v2/v3 Run Snapshot 保持可读且 checksum 不漂移；
- v4 由管理员显式激活，没有 runtime database mutation；
- 所有失败都具有稳定原因码和证据支持的 direct cause；
- focused tests、完整 backend gate、frontend gate、PostgreSQL readback 与真实浏览器
  complex acceptance 全部通过；
- 文档、管理员 UI 和用户可见文案与最终 Implementation 一致。
