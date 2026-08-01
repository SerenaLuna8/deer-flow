# 09 Gateway/API 数据库验收证据

- 日期：2026-07-30
- 分支：`dev`
- checkout HEAD：`785be51341c1`
- 应用数据库：本机 `deerflow`
- schema marker：`full_schema_v1`
- 测试会话：默认项目中当前账号私有 Thread
- 安全说明：本文不记录数据库口令、Cookie、raw trace、审计 HMAC 或 Credential。

## 真实浏览器 Run/Job/审计只读核验

浏览器实际完成四轮模型调用：前三轮由聊天 UI 发起，第四轮由已登录同源页面直接调用
project-private Run API。第四轮请求故意附带客户端 `X-Trace-Id`，当前
`logging.enhance.enabled=false`，因此该 header 不对外回显，也不能进入业务 payload。

只读 SQL 对该 Thread 的四个 Run 逐行核验：

| 检查项 | 结果 |
| --- | --- |
| Run 数量 | 4 |
| Run 终态为 `success` | 4 / 4 |
| Job 终态为 `succeeded` | 4 / 4 |
| `runs.origin_trace_id = jobs.origin_trace_id` | 4 / 4 |
| durable trace 为规范 32 字符 | 4 / 4 |
| durable trace 不等于客户端测试值 | 4 / 4 |
| public metadata/kwargs 不含客户端测试值 | 4 / 4 |
| `run.admitted` 审计记录 | 每个 Run 1 条 |
| `run.terminal` 审计记录 | 每个 Run 1 条 |
| 同一 Run 的 admission/terminal request HMAC 相等 | 4 / 4 |

公开 Run API 另由浏览器确认响应中没有 `origin_trace_id`，证据见
[`02-live-api-matrix.png`](./02-live-api-matrix.png)。

## 浏览器分页数据

同源浏览器页通过真实 upload API 创建 101 个小型文本文件，并以
`limit=100&offset=0` 开始读取：

- 第一页 100 行，`X-Next-Offset: 100`；
- 第二页 1 行，无 next header；
- 101 个创建 ID 全部出现；
- 没有重复 ID；
- 验证后仅按本次记录的 ID 删除 101 个文件；
- 再次全量读取确认测试 ID 残留为 0。

## 固定 PostgreSQL release gate

执行命令使用 `.env` 中的维护连接作为 `POSTGRES_TEST_URL`，随后调用：

```text
make test-project-foundation-postgres
```

当前 checkout 结果：

| 指标 | 结果 |
| --- | --- |
| 根 Makefile 固定文件数 | 20 |
| collected | 269 |
| passed | 269 |
| failed | 0 |
| skipped | 0 |
| duration | 203.57 秒 |

测试 runner 仅创建并清理随机 `deerflow_test_*` 数据库，没有把破坏性测试连接到
业务数据库。trace 全链所在的 `test_m6_audit_integration_postgres.py` 在这次固定 gate
中实际执行。
