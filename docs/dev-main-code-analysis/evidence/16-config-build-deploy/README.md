# Module 16 浏览器与运行时验收证据

## 验收范围

- 时间：2026-07-31（Asia/Shanghai）
- 分支：`dev`
- 基线提交：`785be51341c1`
- 入口：`http://localhost:2026`
- 模型：`deepseek-v4`
- 隔离数据库：`deerflow_test_m16_acceptance_20260731_0125`
- 账号：使用本轮指定的验收账号，证据不记录邮箱和密码

隔离库从空库执行完整 `make setup-db` 后使用。验收结束时已经停止全部
Gateway、Worker、Scheduler、Frontend、Nginx 进程，并删除该精确命名的测试库；
业务数据库没有被连接、初始化、清空或修改。

## 截图

| 文件 | 证明内容 |
| --- | --- |
| `01-isolated-workspace.jpg` | 新初始化账号进入唯一的 `default-project` 工作区；顶部账号区域已经裁掉 |
| `02-real-model-two-rounds.jpg` | 两轮真实 `deepseek-v4` 调用；第 2 轮返回 `M16-FINAL-ROUND-2-OK` 并准确复述第 1 轮标记 |
| `03-large-upload-real-model.jpg` | 3.1 MiB 附件经 Nginx 上传，Worker 真实读取首行并返回文件内标记 |
| `04-gateway-restart-replay.jpg` | Gateway 重启后不重新读取文件，模型仅根据 PostgreSQL 重放上下文准确复述标记 |
| `05-runtime-readiness.jpg` | Admin Operations 显示 Database、Schema、Worker fleet、Scheduler、Stream、Quota、Audit 全部 Ready；Worker 1/容量 4；Scheduler ownership 为 Owned |

截图均为应用内浏览器的真实页面 JPEG，未使用 mock 页面或历史截图。为避免账号
信息进入仓库，只进行了机械裁切，没有改写页面内容。

## 真实 Run

最终接受的关键 Run：

| Run ID | Thread ID | 结果 | 模型统计 |
| --- | --- | --- | --- |
| `bdfdb034-eed3-404b-b70d-6ff15abb12b9` | `a119fe58-2d43-45bc-9ad5-5c20cb630d15` | 最终多轮第 1 轮成功 | input 9,487 / output 59 / total 9,546 |
| `6398e1f0-8873-4faf-a45c-5913d5fbee91` | `a119fe58-2d43-45bc-9ad5-5c20cb630d15` | 最终多轮第 2 轮成功并复述上一轮 | input 9,546 / output 69 / total 9,615 |
| `68ac83c6-1cb5-4a8c-af48-796fbf125508` | `e1b486e8-c949-4368-ae3e-795d624e3409` | 3.25 MB 上传和首行读取成功 | input 19,385 / output 295 / total 19,680 |
| `0f576764-c421-413e-b71f-b7c3dca01f3c` | `e1b486e8-c949-4368-ae3e-795d624e3409` | Gateway 重启后重放成功 | input 10,206 / output 148 / total 10,354 |

数据库最终记录 7 个技术状态为 `success` 的 Run 和 7 个 `succeeded` Job；
`run_events` 共 3,768 条。Worker 日志逐个记录了 Run 注册、执行和终态，Gateway
日志记录了重启前后两个独立进程启动周期。

Nginx 访问日志记录两次大文件上传请求均返回 `201`，普通 Frontend 请求响应头为
`Connection: keep-alive`，没有被固定为 `upgrade`。

## 压力尝试与边界

第一次 3.25 MB 验收同时要求读取文件首尾。模型为寻找最后一行执行了 20 次模型
调用，累计 441,064 tokens；虽然 Run 的技术终态为 `success`，最终回答在超长上下文
压缩后答非所问，因此该次尝试明确判定为**语义验收失败**，没有拿来充当通过证据。

随后把部署边界和文件随机访问能力拆开：

1. 仍上传同一个 3.25 MB 文件；
2. 只读取第一行；
3. 1 次文件工具步骤返回真实标记；
4. 重启 Gateway；
5. 再次调用模型并从持久化上下文复述标记。

这个结果证明了 Module 16 的 Nginx 大请求、Gateway admission/replay、Worker-only
执行和 PostgreSQL 持久化边界。首尾随机访问效率和超长工具链压缩问题不属于
Module 16 部署边界，保留为后续工具/上下文模块风险，未在本轮扩展范围修复。

## 自动化结果

- Module 16 deployment contract：11 passed；
- 完整后端 pytest：0 failure；
- M1-M7 固定 PostgreSQL release gate：276 passed、0 skipped；
- 后端 Ruff check 与 format check：passed；
- 前端单测：188 files、1,350 passed、0 skipped；
- 前端 `check`：ESLint 与 TypeScript passed；
- 前端 production build：78 routes 编译通过；
- 前端 static build：passed；
- 确定性 Chromium E2E：109 passed；
- Nginx local/production syntax：passed；
- Compose prod/dev 与 DooD/CLI overlay config：passed；
- Helm config version、sandbox service、runtime topology、lint/template：passed。

真实 Docker 容器启动和目标 Kubernetes 集群仍是目标环境发布验证，不由本地截图
替代。
