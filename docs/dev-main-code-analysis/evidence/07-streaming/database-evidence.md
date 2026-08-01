# 07 Streaming 真实验收数据库证据

## 1. 隔离边界

- 测试日期：2026-07-30
- 浏览器入口：`http://127.0.0.1:12026`
- 测试数据库：独立新建的 `deerflow_test_stream07_019faca6`
- Thread：`920b1777-7981-4d8a-9345-2bc429186b54`
- Agent：`streaming-e2e`
- 原业务数据库与 `localhost:2026` 的既有服务未被修改。
- 本文件不记录密码、数据库连接串、Credential 或模型密钥。

## 2. 真实模型 Run

| 轮次 | Run ID | 场景 | Run / Job | LLM 调用 | 输入 Tokens | 输出 Tokens | 总 Tokens | 事件数 | seq 范围 | `stream.end` |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| A | `7f1a7057-8e86-4943-a82b-7da6752ad1be` | 10 节 SSE 技术文档与正式文件呈现 | `success / succeeded` | 3 | 34,906 | 2,394 | 37,300 | 2,434 | 1–2,434 | 1 |
| B | `ab997b78-7410-4c5d-8bda-8f11daf87fe9` | 不读文件的多轮上下文回忆 | `success / succeeded` | 1 | 12,799 | 535 | 13,334 | 566 | 2,435–3,000 | 1 |
| C | `5c069efd-4b90-4412-ae57-093c83f4ca71` | 第一次大文件工具流，故意保留真实失败 | `error / dead` | 3 | 63,694 | 9,840 | 73,534 | 9,908 | 3,001–12,908 | 1 |
| C2 | `510ed83c-3cc9-49f6-a907-8dda7976682a` | 修复后的单次 `write_file` 大文件批流 | `success / succeeded` | 8 | 132,875 | 10,989 | 143,864 | 2,354 | 12,909–15,262 | 1 |
| D | `d42bb58b-02f3-4e9c-92c9-471a45cb22a7` | 两个真实 Subagent 与根图汇总 | `success / succeeded` | 2 | 67,519 | 4,328 | 71,847 | 1,336 | 15,263–16,598 | 1 |
| E | `1567ddfc-d253-457f-8128-aa6ff3299d55` | 两个 Subagent 执行中离页并重连 | `success / succeeded` | 4 | 85,604 | 10,395 | 95,999 | 5,490 | 16,599–22,088 | 1 |
| F | `919f749b-88af-4bbf-93bc-1d9afee431d2` | 重连后不使用工具的上下文续问 | `success / succeeded` | 1 | 25,301 | 852 | 26,153 | 883 | 22,089–22,971 | 1 |
| G | `7c4e884c-de07-497d-a3ee-58088372aac8` | 两个 Subagent 运行中离页，等 Run 在页面外终态后再返回 | `success / succeeded` | 2 | 68,753 | 10,512 | 79,265 | 5,311 | 22,972–28,282 | 1 |
| H | `a2942a17-a18e-4aa7-b905-ee3f384e4580` | 终态重放后的上下文续问，并触发长会话压缩 | `success / succeeded` | 3 | 51,512 | 3,079 | 54,591 | 2,503 | 28,283–30,785 | 1 |
| I | `e7c501f4-aff6-4e7c-a8b1-786973fe812b` | 修复稀疏消息投影后再次实时触发压缩与续接 | `success / succeeded` | 2 | 27,858 | 2,835 | 30,693 | 3,258 | 30,786–34,043 | 1 |

合计：

- 10 个真实 Run；
- 29 次真实模型调用；
- 输入 570,821 Tokens，输出 55,759 Tokens，共 626,580 Tokens；
- 34,043 条持久化事件。

## 3. 游标和唯一终态

对该 Thread 的 `run_events` 做只读聚合：

```text
COUNT(*)                  = 34043
COUNT(DISTINCT seq)       = 34043
MIN(seq)                  = 1
MAX(seq)                  = 34043
MAX - MIN + 1 - COUNT     = 0
```

结论：

1. Thread 级游标严格唯一；
2. 10 个 Run 之间的边界连续，没有回退、重复或缺口；
3. 每个 Run 恰有一个 `category=stream,event_type=stream.end`；
4. 每个 Run 的最大 `seq` 所在行均为该唯一 `stream.end`；
5. 失败 Run C 也只形成一个受控终态，没有重复 terminal。

## 4. 大文件批流

第一次大文件 Run C 真实创建了文件，但模型没有按正式文件交付协议呈现链接。交付守卫正确地把存在写入副作用、重试安全未知的 Job 置为 `dead / SIDE_EFFECT_STATE_UNKNOWN`，没有自动重试写操作。

同时，该轮暴露出 DeepSeek 会在工具参数 delta 之间插入仅含
`response_metadata.model_provider` 的 transport-noise chunk。修复前这些空可见载荷会冲刷 batch：

```text
C:  总事件 9908，stream/messages 9840，LLM 调用 3
```

修复 `_LargeFileToolChunkBatcher` 后，C2 使用更多模型调用和更多输出 Tokens，仍显著减少持久化帧：

```text
C2: 总事件 2354，stream/messages 2265，LLM 调用 8
```

消息帧减少 `7,575` 条，降幅约 `76.98%`。C2 的 2,265 条消息帧中：

- 406 条包含文件工具参数 delta；
- 546 条包含可见 reasoning；
- transport-metadata-only noise 不再单独发布，也不会冲刷同一工具 identity 的 pending batch。

最终文件元数据：

| logical path | 状态 | 文件大小 | chunk 数 | chunk 总字节 | 创建 Run |
| --- | --- | ---: | ---: | ---: | --- |
| `outputs/sse-persistence-correctness.md` | `ready` | 7,161 | 1 | 7,161 | A |
| `outputs/stream07-large-payload.txt` | `ready` | 16,509 | 1 | 16,509 | C |
| `outputs/stream07-large-payload-pass.txt` | `ready` | 16,511 | 1 | 16,511 | C2 |

C2 在浏览器刷新后仍能恢复正式文件卡片和预览，首行
`S07-FILE2-FIRST`、160 条数据行和末行 `S07-FILE2-LAST` 均存在。

## 5. Subagent 根图/子图隔离

Run D 与 Run E 均真实创建两个 Subagent。数据库分别保存：

```text
每个 Run：
  subagent.start = 2
  subagent.step  = 2
  subagent.end   = 2
```

每组 `start → step → end` 使用同一个服务端 task ID；两个子任务 task ID 不同。浏览器只在根对话中显示两张 SubtaskCard 和 Lead Agent 的最终汇总，子图原始 model/tool frame 没有替换 Lead 对话。

## 6. 执行中断线与恢复

Run E 已在两个 Subagent 均处于“子任务运行中”时执行：

1. 从会话页导航到项目概览；
2. 离页约 8 秒；
3. 返回完全相同的 Thread URL；
4. 页面从 PostgreSQL 持久流恢复用户请求、两张仍在运行的 SubtaskCard 和既有文件预览；
5. 没有重新提交 Run；
6. Run 后续正常完成，唯一结束标记 `S07-END-E` 在 UI 中只出现一次；
7. 数据库最终 `seq=22088` 为该 Run 唯一 `stream.end`。

Run F 随后不调用任何工具，仅根据恢复后的 Thread 上下文准确回忆：

- `S07-E-20260730-EPSILON`
- `S07-END-E`
- `S07-E-CHILD-ONE`
- `S07-E-CHILD-TWO`
- 两个子任务各两条核心结论
- 主回答共有 10 个编号小节

这证明重连不只是恢复视觉上的终态，也保留了下一轮模型所需的会话状态。

Run G 将条件收紧为“Run 必须在页面外先达到终态”：

1. 两个真实 Subagent 都显示运行中后离开会话页；
2. 页面外停留约 165 秒；
3. 数据库先确认 `Run=success`、`finalization_status=complete`、两个
   `subagent.start/end` 都已提交且唯一 `stream.end` 已落库；
4. 此后才返回原 Thread；
5. 新页面在约 2 秒内从 durable cursor `0` 重建完整回答、两个子任务标记和 `S07-END-G`；
6. 没有历史加载错误、`PROJECT_STREAM_INCOMPLETE` 或重复终态。

Run H 随后继续无工具上下文追问，后端以 3 次真实模型调用成功完成并准确回忆 G。该轮同时触发长会话
summarization，真实暴露了一个前端 SDK 投影缺陷：`RemoveMessage(__remove_all__)` 后 SDK 可短暂形成
稀疏消息数组，而 DeerFlow 原来的 `{...thread}` 会提前执行可枚举 `toolCalls` getter，最终读取空槽的
`.type` 并使页面崩溃。数据库与持久帧均完整，问题只在客户端 getter 的急切求值。

修复后，DeerFlow 通过复制属性描述符保留 SDK getter 的惰性，仅覆盖已经归一化的
`messages/values/stop`。重新进入页面可完整看到 H 和唯一 `S07-END-H`；Run I 再次在同一条已压缩长
会话中实时执行，2 次模型调用后正常显示 `S07-END-I`，证明修复不仅能恢复历史，也能承受下一次实时
压缩与消息重建。

## 7. 浏览器截图

- [01-live-multiturn.jpg](./01-live-multiturn.jpg)：A/B 多轮回忆与 Tokens。
- [02-large-write-batched-preview.jpg](./02-large-write-batched-preview.jpg)：C2 正式文件卡片和大文件预览。
- [03-subagent-root-events.jpg](./03-subagent-root-events.jpg)：D 的两张真实 Subagent 完成卡片与根图汇总。
- [04a-active-before-detach.jpg](./04a-active-before-detach.jpg)：E 离页前的运行中状态。
- [04-midstream-reconnect.jpg](./04-midstream-reconnect.jpg)：重新进入 Thread 后恢复出的两个运行中 Subagent。
- [04b-post-reconnect-terminal.jpg](./04b-post-reconnect-terminal.jpg)：重连后的唯一终态和 71.7K Tokens 回答。
- [05-post-reconnect-followup.jpg](./05-post-reconnect-followup.jpg)：F 对重连前后上下文的准确续问。
- [06a-updated-run-before-away.jpg](./06a-updated-run-before-away.jpg)：G 的两个 Subagent 均运行中，随后离页。
- [06-terminal-after-away-replay.jpg](./06-terminal-after-away-replay.jpg)：数据库已终态后返回，完整重放 G 的两个子任务标记和唯一结束标记。
- [07-compaction-replay-followup-fixed.jpg](./07-compaction-replay-followup-fixed.jpg)：修复 SDK 稀疏消息投影后恢复 H 的完整压缩后回答。
- [08-live-after-compaction-fix.jpg](./08-live-after-compaction-fix.jpg)：I 在修复后再次实时完成压缩与续接，无页面崩溃。

## 8. 数据库与进程门禁

本轮在真实 PostgreSQL 上另外复跑：

```text
test_scheduler_enabled_requires_owned_session_and_loss_fails_closed
test_real_process_roles_keep_graph_worker_only_and_reconnect_isolated
2 passed, 0 skipped
```

聚焦大文件 batcher、Gateway/Scheduler import 边界与 lazy export：

```text
19 passed
```

完整测试结果和已知非 Streaming 门禁项记录在
`docs/dev-main-code-analysis/07-streaming.md` 的实际迁移结果中。

修复所有真实发现后，固定 M1–M7 20 文件真实 PostgreSQL 门禁最终复跑：

```text
266 collected
266 passed
0 failed
0 skipped
```

## 9. 当前工作树完整 Backend 回归

全量测试先真实暴露 `PrivateWorkContext` 弱引用登记表在 GC 回调同线程重入时会对普通 `Lock`
自死锁；改为 `RLock` 并增加精确回归后，不再停在原执行位置。其后 16 个失败均确认是旧测试夹具
未同步检查点冻结字段、安全类型边界和 `memory_search` 工具契约，定向复跑为
`74 passed, 1 skipped`。

最终结果：

```text
uv run pytest -q
7358 passed, 977 skipped, 0 failed

ruff format --check
1119 files already formatted

ruff check
All checks passed
```
