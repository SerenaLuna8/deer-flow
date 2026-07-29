# 04. Subagent 模块：main 实现、dev 对照与落地边界

## 1. 分析基线与范围

- `main`：`e317f7b8d9b2afb4c3925812d4774da602c9f8f3`
- `dev`：`8a91e95799c9b345d9540c7e201b33c603e7870c`
- main 演进区间：`3be3969f..e317f7b8`
- 范围：`task` 工具、Subagent 配置/注册/执行、状态契约、step event、并发/总量限制、超时/取消、回退与持久化。

Agent 与 Skill 在本篇只作为 Subagent 的输入边界出现，不把它们混成一个模块。

## 2. 结论

main 的 Subagent 是“父 Agent 通过 `task` 工具启动的、进程内隔离执行单元”，不是独立服务。它运行在进程内持久化 daemon event-loop thread 上，使用独立 graph state 和工具集合，但继承经过筛选的父运行身份、模型/工具组、Skill allowlist、trace 与 namespace。

需要精确理解四点：

1. 继承不是全量复制：model 可覆盖，tools 经 allowlist/denylist，skills 取父子 allowlist 的交集。
2. step capture 不是“总 O(1)”：按 ID 去重平均 O(1)，每个 values chunk 只扫新增尾部 `O(k_new)`，整次 append-only 执行为 `O(total_messages)`；id-less 内容去重仍可能线性。
3. `GraphRecursionError` 在 dev 已有正常分类；main 新增的是“带显式 marker 的 LLM fallback”在正常和 recursion 两条路径都必须判失败，以及 guard stop reason 优先级。
4. dev 的 project/owner scope、exact admitted Skill/MCP、owner-loop proxy 是安全边界，不能被 main 的通用执行器覆盖。可移植的是纯算法修复和契约扩展。

## 3. main 源码地图

| 职责 | 路径 | 关键符号 |
| --- | --- | --- |
| 工具入口 | `backend/packages/harness/deerflow/tools/builtins/task_tool.py` | `task_tool()`, `_merge_skill_allowlists()` |
| 配置 | `backend/packages/harness/deerflow/subagents/config.py` | `SubagentConfig`, `resolve_subagent_model_name()` |
| 注册 | `backend/packages/harness/deerflow/subagents/registry.py` | built-in/custom agent 解析 |
| 执行 | `backend/packages/harness/deerflow/subagents/executor.py` | `SubagentExecutor`, `SubagentResult`, `_copy_isolated_subagent_context()` |
| step 捕获 | `backend/packages/harness/deerflow/subagents/step_events.py` | `capture_new_step_messages()`, `subagent_run_event()` |
| 状态契约 | `backend/packages/harness/deerflow/subagents/status_contract.py` | `make_subagent_additional_kwargs()`, `normalize_token_usage()` |
| token | `backend/packages/harness/deerflow/subagents/token_collector.py` | `SubagentTokenCollector` |
| 并发/总量限制 | `backend/packages/harness/deerflow/agents/middlewares/subagent_limit_middleware.py` | `SubagentLimitMiddleware` |
| durable ledger | `backend/packages/harness/deerflow/agents/middlewares/durable_context_middleware.py` | delegation ledger merge/terminal |
| 运行事件持久化 | `backend/packages/harness/deerflow/runtime/runs/worker.py` | `_SubagentEventBuffer` |

## 4. main 精确类型与签名

### 4.1 `SubagentConfig`

```python
@dataclass
class SubagentConfig:
    name: str
    description: str
    system_prompt: str | None = None
    tools: list[str] | None = None
    disallowed_tools: list[str] | None = ["task"]
    skills: list[str] | None = None
    model: str = "inherit"
    max_turns: int = 50
    timeout_seconds: int = 900
```

内置 agent 会由 registry 叠加全局配置；当前说明中的 general-purpose 和 bash 默认上限分别是 150/60 turns，执行超时通常由全局 `subagents.timeout_seconds` 覆盖。

```python
resolve_subagent_model_name(
    config: SubagentConfig,
    parent_model: str | None,
    *,
    app_config: AppConfig | None = None,
) -> str
```

解析顺序：显式 child model > parent model > AppConfig 第一个模型。

### 4.2 工具入口

```python
@tool("task")
async def task_tool(
    runtime: Runtime,
    description: str,
    prompt: str,
    subagent_type: str,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> str | Command
```

`tool_call_id` 同时作为 background task ID，形成 UI、ToolMessage、custom event 和 persisted event 的关联键。

Skill allowlist 合并语义：

```text
parent is None -> child
child is None  -> copy(parent)
both present   -> [child item if parent permits]
```

因此 child 不能通过配置扩大父 Agent 可发现 Skill。

### 4.3 执行器

`SubagentExecutor.__init__()` 的关键参数包括：

```python
config, tools, app_config=None, parent_model=None,
sandbox_state=None, thread_data=None, thread_id=None,
trace_id=None, user_id=None, user_role=None,
oauth_provider=None, oauth_id=None, run_id=None,
channel_user_id=None, is_internal=False,
authz_attributes=None, deerflow_trace_id=None
```

主要方法：

```python
async def _build_initial_state(task: str)
async def _aexecute(task: str, result_holder: SubagentResult | None = None)
def execute(task: str, result_holder: SubagentResult | None = None)
def execute_async(task: str, task_id: str | None = None) -> str
request_cancel_background_task(task_id: str) -> None
get_background_task_result(task_id: str) -> SubagentResult | None
cleanup_background_task(task_id: str) -> None
```

`SubagentResult.try_set_terminal(...)` 在 lock 内 first-terminal-wins，防止 timeout、cancel、正常结束并发覆盖。

## 5. main 完整调用链

```text
lead Agent AIMessage(tool_call="task")
  -> SubagentLimitMiddleware.after_model()
       按当前 run_id 读取 delegation ledger
       截断超过 concurrent/total cap 的 task calls
  -> task_tool(runtime, ...)
       校验 subagent_type 与 bash availability
       读取服务端 runtime identity / parent model / tool_groups
       _merge_skill_allowlists(parent, child)
       get_available_tools(
           groups=parent_tool_groups,
           subagent_enabled=False,
           include_upload_tool=False
       )
       SubagentExecutor(config, filtered context...)
       execute_async(prompt, task_id=tool_call_id)
  -> persistent isolated event loop thread
       _copy_isolated_subagent_context()
       _aexecute()
         -> _build_initial_state()
              lazy load allowed Skills
              authorization filter
              deferred tool setup
              SystemMessage + HumanMessage(task)
              sandbox/thread_data
         -> _create_agent(..., checkpointer=False)
         -> run_config = recursion_limit + collector/tracing callbacks/tags
         -> agent.astream(..., stream_mode="values")
         -> capture_new_step_messages(...)
         -> terminal classification
  -> task_tool 每 5 秒轮询
       emit task_started/task_running/task_* custom events
       汇总 effective model + token usage
       返回 Command(update.messages=[ToolMessage(...additional_kwargs...)])
  -> lead graph 继续
  -> run worker 把 custom task events 批量写入 RunEventStore
```

`checkpointer=False` 表示 child graph 不建立自己的持久化 checkpoint 权威；但
`_copy_isolated_subagent_context()` 保留 ambient LangGraph runnable config 的 namespace/streaming context，只删除标记为 `deerflow_loop_bound` 的 callback，避免父 `RunJournal` 在另一个 loop 被错误调用，同时保留框架 streaming callback。

## 6. 模型、工具与 Skill 边界

### 6.1 模型

- `model != "inherit"`：使用 child 配置；
- 否则使用 parent metadata 的 effective model；
- parent 未提供时才回退 AppConfig default；
- terminal ToolMessage 和 task end event 可携带 effective `model_name`；
- `SubagentTokenCollector` 归一化累计 `input_tokens/output_tokens/total_tokens`。

### 6.2 工具

1. 先按 parent `tool_groups` 构造候选；
2. 强制 `subagent_enabled=False`，阻止递归 task；
3. 强制 `include_upload_tool=False`，因为 child state 没有当前 run upload 排除状态；
4. `SubagentExecutor._filter_tools()` 再应用 child allowlist；
5. 最后应用 `disallowed_tools`。

“继承父工具”因此只表示在父可用集合内继承，不表示复制全部系统工具。

### 6.3 Skill

- 父 metadata 的 `available_skills` 与 child `config.skills` 合并；
- `_load_skills()` 只加载 enabled 且在最终 allowlist 中的 Skill；
- bodies/policy 在 child session 内延迟激活；
- `available_skills` 精确传给 Skill policy middleware，不能激活未披露 Skill。

## 7. step 状态与复杂度

`agent.astream(..., stream_mode="values")` 每个 super-step 会重新给出完整 message history。

main 使用：

```python
processed_message_count: int
seen_message_ids: set[str]
capture_new_step_messages(messages, captured, seen_ids, processed_count)
```

复杂度应准确表述为：

- ID 去重：平均 `O(1)` lookup；
- history 增长：只扫描 `messages[processed_count:total]`，该 chunk 为 `O(k_new)`；
- append-only 全程：每条消息通常只扫描一次，aggregate `O(total_messages)`；
- history 长度不变：只复查 tail；
- history 因 summarization 收缩：cursor 重置到新 tail，随后靠 ID/content dedup；
- id-less message 使用内容 membership fallback，最坏可随 captured 数量线性，不能宣称严格总 O(1)。

当前成立的关键 invariant：compaction 将 summary 放到单独 `summary_text`，只保留已见 tail，不会在 reset cursor 以下插入新的可捕获 AI/ToolMessage。未来若中间件破坏它，需要完整重扫或 generation-aware cursor。

step payload：

- `kind` 为 `ai` 或 `tool`；
- text 与 tool args 各自最多 8192 字符；
- 大 args 变成截断后的序列化 preview，并标 `args_truncated`；
- `message_index` 非负；
- 非法 task event 被 `subagent_run_event()` 丢弃；
- terminal result/error 同样有边界，完整结果仍在 terminal ToolMessage。

## 8. 并发、限制、取消与恢复

### 8.1 限制

`SubagentLimitMiddleware`：

- concurrent cap 规范化到 `[1, 4]`，默认 3；
- per-run total cap 规范化到 `[1, 50]`，默认 6；
- ledger 仅统计当前 `run_id` 的 delegation；
- 超限时删除多余 task calls，并给模型追加可见说明；
- total 耗尽时写 `stop_reason="subagent_limit_capped"`；
- durable ledger 的 terminal 状态不可被旧 running 更新降级。

### 8.2 执行与取消

- 一个进程只有一个持久化 isolated event loop thread，执行请求通过
  `run_coroutine_threadsafe()` 提交；
- background registry 持有 `SubagentResult`；
- timeout/cancel/normal completion 都竞争 `try_set_terminal()`；
- cooperative cancel 只在 `astream` yield 边界检查，单个长工具调用不会被立即打断；
- `task_tool` 轮询预算是执行 timeout + 60 秒，轮询本身超时时安排 deferred cleanup。

### 8.3 terminal 分类

正常完成：

1. `_extract_llm_error_fallback(final_state)` 只检查最后一个 AIMessage；
2. 必须有结构化 `deerflow_error_fallback=True` marker；
3. marker 命中 => `FAILED`，不会按“看起来像错误”的普通文本误判；
4. 否则提取结果，guard middleware 的 `token_capped/loop_capped` 作为 additive stop reason；
5. status 仍为 `completed`。

`GraphRecursionError`：

1. 先消费 guard stop reason，否则为 `turn_capped`；
2. 再检查 marked fallback，命中 => `failed + stop_reason`；
3. 无 fallback 时，最后一条非空 AI 输出 => `completed + partial result + stop_reason`；
4. 无可用输出 => `failed + stop_reason`。

dev 已经实现第 3/4 类普通 recursion 分类；main 的新增价值是 marker 分支和 guard 原因优先。

## 9. 状态与事件契约

`ToolMessage.additional_kwargs` 的稳定字段：

- `subagent_status`
- `subagent_stop_reason`（可选）
- `subagent_error`（非 completed）
- `subagent_result_brief` / `subagent_result_sha256`
- `subagent_model_name`
- `subagent_token_usage`

status 枚举：

```text
completed, failed, cancelled, timed_out, polling_timed_out
```

stop reason：

```text
token_capped, turn_capped, loop_capped
```

旧 checkpoint 中的 `max_turns_reached` 在读侧归一化为 `turn_capped`，避免历史 delegation 永远停在 in-progress。

live custom event：

```text
task_started -> task_running* -> task_completed/task_failed/task_cancelled/task_timed_out
```

persisted event：

```text
subagent.start -> subagent.step* -> subagent.end
```

`_SubagentEventBuffer` 阈值 25；terminal event eager flush。main 在
`put_batch()` 失败时把失败 batch 放回 pending 前部，保序等待下次 flush。事件 content 是 JSON，
增加 model/token 字段不要求数据库 schema 变更，但必须同步 Python validator 和前端 reader。

## 10. main 测试与契约

| 测试 | 覆盖 |
| --- | --- |
| `test_subagent_executor.py` | tool filtering、namespace/callback context、fallback、recursion、cancel、skill loading |
| `test_task_tool_core_logic.py` | polling、Command/ToolMessage、model/token metadata |
| `test_subagent_step_events.py` | 多 ToolMessage、cursor、截断、event validator |
| `test_subagent_limit_middleware.py` | concurrent + total cap、run-scoped ledger |
| `test_durable_context_middleware.py` | terminal monotonicity、run_id 隔离 |
| `test_worker_subagent_persistence.py` | batching、eager flush、失败 re-buffer |
| `test_tool_error_handling_middleware.py` | child middleware 顺序、summarization、loop detection |

## 11. main 关键提交的实现演进

| 提交 | 实际变化 |
| --- | --- |
| `f1632cc3` / `4a2ecd43` | 定义 task lifecycle custom event 与持久化 event contract |
| `266883b3` | child 继承 summarization 组合；捕获每个新增 Tool/AI step；加入 cursor/去重与 cap reason |
| `bbb3deb2` | 正常完成时识别显式 LLM fallback marker 并判 failed |
| `2bd0f56a` | recursion recovery 路径也先识别 fallback，避免错误文本成为“成功的部分结果” |
| `aafd5077` | terminal metadata/event/frontend task card 增加 effective model 与 token usage |
| `4e209827` | 增加 per-run total delegation cap，并给 ledger 增加 run_id |
| `18c32bea` | `put_batch` 失败后 re-buffer，不再永久丢 step batch |
| `de55982c` | 不再清空 ambient checkpoint namespace，保留父子 namespace lineage |
| `a5059b82` | 仅剔除 loop-bound callback，保留 framework streaming；Skill 改为 child session lazy activation |
| `126fc9ea` | 统一配置、middleware 和 prompt 对 limit 的 clamp |

## 12. dev 对应实现与调用链

dev 仍使用相同主文件：

- `backend/packages/harness/deerflow/tools/builtins/task_tool.py`
- `backend/packages/harness/deerflow/subagents/executor.py`
- `backend/packages/harness/deerflow/subagents/step_events.py`
- `backend/packages/harness/deerflow/subagents/status_contract.py`
- `backend/packages/harness/deerflow/agents/middlewares/subagent_limit_middleware.py`

但 private run 增加了关键参数与代理：

- `private_scope`
- `__authorization_boundary` / checker
- file authority 与 read-only mounts
- immutable `runtime_skills`
- opaque `agent_prompt_bundle`
- skill secret provider
- exact admitted private MCP tools

dev 的链路：

```text
Worker exact admitted runtime
  -> lead task_tool()
  -> 从 runtime 读取 PrivateResourceScope / exact Skill / exact MCP / boundary
  -> owner_loop = asyncio.get_running_loop()
  -> 将 auth boundary、checker、skill secret provider 包成 owner-loop proxy
  -> 将 exact private MCP tool 包成 owner-loop dispatch
  -> private run 禁用 global MCP/ACP discovery
  -> SubagentExecutor(private_scope, runtime_skills, mounts, prompt bundle...)
  -> isolated loop child graph
  -> 每个 private side effect 回到 Worker owner loop revalidate
```

`_copy_detached_subagent_context()` 当前会清空整个 `var_child_runnable_config`。
这阻止父 callback 在 isolated loop 运行，但同时丢失 parent namespace 和 framework streaming callback。
dev 又显式构造 child `run_config.configurable.thread_id`，所以其 namespace/stream lineage 与 main 不同。

## 13. main 与 dev 精确差异

| 维度 | main | dev |
| --- | --- | --- |
| private scope | 通用 user/auth attributes | project + owner opaque scope |
| Skill 来源 | 当前 user registry，child session lazy load | admission 时冻结的 exact immutable Skill snapshot |
| MCP | parent tool groups/deferred discovery | exact admitted MCP，owner-loop proxy；private run 禁全局 discovery |
| secrets | 通用运行环境 | exact Skill credential 引用，按边界解密/注入 |
| callbacks | 剔除 loop-bound，保留 streaming | 清空整个 child runnable config |
| namespace | 保留 ambient parent namespace | 当前 detached config 丢失 parent namespace |
| step cursor 收缩 | 已 reset | 缺少 main 的 reset 修复 |
| LLM fallback | 正常/recursion 均检查 marker | 普通 fallback 扫描存在，但 child executor 缺 main 两条精确分类 |
| recursion 普通输出 | partial completed / empty failed | 已有相同基本语义 |
| total cap | concurrent + per-run total | 只有旧 concurrent cap，最小值语义也较旧 |
| status metadata | model + token usage | 尚无完整字段 |
| event flush failure | re-buffer | 清空后仅日志，失败 batch 丢失 |

## 14. 已确认缺陷与风险

1. **dev root/subgraph fallback 误判**：dev 的
   `runtime/runs/worker.py::_stream_once()` 在 multi-mode/subgraph 分支对所有 namespace 调
   `_extract_llm_error_fallback_message()`；child fallback 可把 parent Run 标为 error。main 只在
   `if not namespace` 时判断。
2. **dev step batch 丢失**：`_SubagentEventBuffer.flush()` 先清 pending，`put_batch()` 失败只日志，不 re-buffer。
3. **dev compaction cursor**：history 收缩后仍保留旧 cursor，新追加 step 在长度追平前可能被漏掉。
4. **dev namespace/callback 全清**：避免 loop-bound callback 是对的，但连 framework namespaced streaming 一起清除；直接复制 main 又可能让 owner-loop 私有 callback 越 loop。
5. **cooperative cancel 的共同限制**：长工具调用不 yield 时，取消不会即时生效；private side effect 必须继续由 dev boundary 在调用前重验证。
6. **总量限制缺失**：并发限制不能防止一次 run 串行委派无限多 task。

## 15. 可移植落点

建议按以下精确符号移植：

1. `subagents/executor.py::_extract_llm_error_fallback()` 与
   `SubagentExecutor._aexecute()` 的正常/recursion 分支。
2. `subagents/step_events.py::capture_new_step_messages()` 的 contraction reset 和边界化 tool args。
3. `runtime/runs/worker.py::_SubagentEventBuffer.flush()` 的失败 re-buffer。
4. `agents/middlewares/subagent_limit_middleware.py::SubagentLimitMiddleware` 的 per-run total cap，
   并同步 `durable_context_middleware.py` 的 run_id ledger。
5. `status_contract.py` 的 model/token 字段与前端 task reader/card 保持 wire 字段一致。
6. namespace 修复应重写 dev 的 `_copy_detached_subagent_context()`：只保留可跨 loop 的 framework
   streaming/namespace context，继续删除 private owner-loop callback；不能机械替换。
7. root fallback 修复落在
   `backend/packages/harness/deerflow/runtime/runs/worker.py::_stream_once()`，判断必须位于
   `_unpack_stream_item()` 后且仅 `namespace == ()` 生效。

## 16. 禁止直接合并

- 禁止用 main `task_tool()` 覆盖 dev 的 owner-loop boundary/checker/secret/MCP proxy。
- 禁止让 private child 重新发现 global MCP、ACP、Skill 或 Credential。
- 禁止把父 Agent 全部 tools/skills 原样复制给 child。
- 禁止把 client 提供的 project/owner/scope 放入 executor。
- 禁止为了保 namespace 而恢复 `RunJournal` 等 loop-bound callback。
- 禁止把 task model/token 字段做成新的 SQL 列；当前通用 event content/metadata JSON 足够。
- 禁止把 child marked fallback 用来决定 parent Run terminal；只应形成 `task_failed`。
- 禁止宣称 step capture 总复杂度为 O(1)。

## 17. 建议测试矩阵

| 场景 | 期望 |
| --- | --- |
| parent/child model | inherit、显式 override、missing parent fallback 都得到正确 effective model |
| tools | parent group、child allow/deny、task/upload 强制排除 |
| skills | `None/[]/交集` 三态；private child 只能见 exact admitted snapshot |
| MCP/secrets | private child 只能 owner-loop 调 exact MCP；撤权后下一次调用失败 |
| namespace | child frame 有 namespace；不污染 root message；parent journal 不跨 loop |
| root fallback | child marked fallback => task failed、parent 可继续；root marked fallback => parent error |
| recursion | partial/empty/marked fallback，分别覆盖 guard reason 与 turn reason |
| compaction | cursor 收缩后新 AI/Tool step 不漏、不重复 |
| batching | 阈值、terminal eager flush、首轮失败后次轮按原序写入 |
| limits | 并发 1–4、总量 1–50、跨 run ledger 不串计数 |
| cancel/timeout | pre-start、stream boundary、长工具、first-terminal-wins |
| wire fields | Python validator、TypeScript reader、legacy `max_turns_reached`、model/token validation |
| durable replay | reload 后 step 顺序、terminal 唯一、message feed 不混入内部 step |
