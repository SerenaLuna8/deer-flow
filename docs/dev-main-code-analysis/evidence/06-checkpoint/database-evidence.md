# 06 Checkpoint 真实 PostgreSQL 证据

采集时间：2026-07-30（Asia/Shanghai）

本文件只记录本地 `make dev` 环境中两个真实浏览器线程的只读聚合结果。查询未输出数据库 URL、
密码、Credential、完整消息正文或 Checkpoint 原文。

## 测试对象

| 角色 | Thread ID | 用途 |
| --- | --- | --- |
| Source | `96d942fc-d0ff-420e-8657-1707a75324fa` | 23 个 Run 的长对话、刷新、两次压缩、源/分支隔离及分页修复复测 |
| Branch | `684d0680-315e-48a4-9b06-613f7ed2cdeb` | 从 Source 当前 head 分支后恢复 A/B，并新增只属于分支的 C |

两个 `threads_meta` 行最终均为：

```text
status=idle
version=1
checkpoint_delete_status=not_requested
```

## Run 与事件

| Thread | Run 总数 | success | interrupted | LLM calls | 输入 Token | 输出 Token | 总 Token |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Source | 23 | 22 | 1 | 23 | 227,169 | 2,558 | 229,727 |
| Branch | 1 | 1 | 0 | 1 | 9,475 | 472 | 9,947 |

Source 的 22 个成功 Run 每个都有 2 条持久化 `message` 事件；唯一的 `interrupted` Run 有 0 条消息
事件。该中断来自浏览器测试助手首次提前结束的一次提交；Gateway 日志明确记录了
`cancel?action=interrupt`，Worker 随后按授权撤销边界拒绝剩余 Checkpoint/file/journal 写入。它不是
未知 Checkpoint 损坏，后续同一用例已重新执行并成功。

| Thread | `run_events` 总数 | `message` | `stream` | seq 范围 |
| --- | ---: | ---: | ---: | --- |
| Source | 3,271 | 44 | 3,183 | 1…3,271 |
| Branch | 503 | 2 | 499 | 1…503 |

修复后的最后两次真实模型 Run：

| Run ID | 状态 | 模型 | 输入 Token | 输出 Token | 总 Token | LLM calls |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| `5e7bab84-ada5-4aa1-944a-af5e17c38c9d` | success | `deepseek-v4` | 10,206 | 144 | 10,350 | 1 |
| `bb3cf64a-8f7e-495e-ace3-2f0c08c44ed6` | success | `deepseek-v4` | 10,333 | 230 | 10,563 | 1 |

第二个 Run 在浏览器刷新后创建，准确确认前一轮 `FINAL06-1` 存在，并再次恢复 A、B、D。

## Checkpoint delta 形态

| Thread | Checkpoint | delta marker | Blob 总数 | message Blob | Blob bytes | message Blob bytes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Source | 465 | 464 | 90 | 9 | 120,219 | 105,046 |
| Branch | 22 | 22 | 12 | 1 | 6,003 | 5,307 |

Source 的 465 个 Checkpoint 中只有最初的 legacy/full seed 没有
`deerflow_checkpoint_channel_mode=delta`；之后 464 个均带 delta marker。这是受支持的
full → delta 单向演进，不是 marker 丢失。

| Thread | pending writes 总数 | message writes | Write blob bytes | message write bytes |
| --- | ---: | ---: | ---: | ---: |
| Source | 598 | 97 | 351,000 | 339,224 |
| Branch | 31 | 5 | 19,364 | 18,773 |

这里的 byte 数来自 PostgreSQL `bytea` 的 `octet_length`，不是估算值。Checkpoint 与 metadata
本身是 `jsonb`；本次另行核对了其序列化文本长度，但不把该长度冒充为 PostgreSQL 实际磁盘占用。

## 压缩边界：原始持久化与模型摘要必须分开判断

第二次 `/compact` 前只出现一次的低显著性标记 `CP-E-EARLY-6A2F`，在压缩后没有被模型摘要保留。
但只读原始存储核对结果为：

```text
checkpoint_writes 中包含 E：4 行
checkpoint_blobs 中包含 E：2 行
```

因此本用例证明的是：

1. 原始 Run 事件、Checkpoint Blob 和 pending writes 没有丢失 E；
2. `/compact` 使用模型生成有损摘要，低显著性事实可能不进入新的活跃上下文；
3. A、B、D 被摘要保留，刷新后及后续两次真实模型调用均能准确恢复；
4. 不能把“模型未从摘要回忆 E”错误描述成“Checkpoint 数据丢失”。

## 前端长历史缺陷与修复后的数据库对应关系

修复前，SDK `runs.list(threadId)` 默认只请求 10 条 Run。浏览器的“加载更多”只能遍历这 10 条，
虽然 PostgreSQL 已持久化 Source 的 23 条 Run 和全部 44 条消息事件。

修复后，前端按 Gateway 的 `limit/offset` 契约分页读取全部 Run 元数据，并仍按需逐 Run 读取消息正文。
浏览器连续加载 12 次后显示到最早的 `ACK-1`、第二轮 A/B 和刷新后的第三轮 A/B，最终“加载更多”
按钮消失。这与数据库中的 23 条 Run、22 个成功 Run × 2 条消息事件完全一致。
