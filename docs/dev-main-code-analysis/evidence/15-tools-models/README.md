# Module 15 Tools / Models 移植验收证据

验收窗口：2026-07-30 至 2026-07-31（Asia/Shanghai）

## 基线与结论

- 分支：`dev`
- HEAD：`785be51341c1c3ddaa073b76aaa4421bee0ac136`
- 状态：验收基于包含 Module 01–15 变更的未提交工作树；本轮没有提交或推送
- 入口：`http://localhost:2026`
- 交互验收：Codex in-app Browser
- 自动化 E2E：系统 Chrome
- 模型：`deepseek-v4 / DeepSeek V4 Pro`
- 推理深度：高
- 测试账号：`d***@gmail.com`；本文和截图均不记录密码

Module 15 的模型工厂、vLLM/MindIE 兼容、ToolResult 预算和私有外置、图片 checkpoint
瘦身、历史上传、Browserless、ACP POSIX 生命周期、公开序列化与文件聚合审计已通过对应
自动化门禁。真实浏览器另完成 9 个 Run、20 次外部模型调用；其中 6 个正式验收步骤通过，
3 个探索性 probe 如实排除在通过数之外。

## 隔离数据库

真实验收使用：

```text
deerflow_test_m15_acceptance_20260730
```

创建后确认 marker 为 `full_schema_v1`。所有验收 Run 完成后，先停止隔离 Gateway、Worker、
Scheduler、Frontend 和 Nginx，再精确删除该数据库；随后查询
`pg_database.datname = 'deerflow_test_m15_acceptance_20260730'` 返回计数 `0`。没有删除、
补表、迁移或改写业务数据库 `deerflow`。

隔离栈在 2026-07-31 00:43:36 CST 正常关闭，端口 `2026`、`3000`、`8001` 全部释放；
关闭日志没有 pending-task warning。

## 自动化门禁

验收期间各命令按顺序串行执行。早期聚焦命令的每条独立开始/结束时间没有另行落盘，因此
不补造时间；能从测试输出确认的持续时间在结果中保留。

| 门禁 | 执行命令 | 结果 |
| --- | --- | --- |
| Module 15 聚焦 | `cd backend && PYTHONPATH=. uv run pytest <15.1 所列聚焦文件> -q` | `633 passed, 17 skipped`；17 项均是无数据库环境下的 finalizer 用例 |
| finalizer 真实 PostgreSQL | `cd backend && POSTGRES_TEST_URL=<redacted> PYTHONPATH=. uv run pytest tests/test_private_file_finalizer.py -q` | `25 passed, 0 skipped` |
| 后端全量 | `cd backend && make test` | `7841 passed, 1021 skipped, 0 failed`，约 125 秒 |
| Python 格式与 lint | `cd backend && uv run ruff format --check . && uv run ruff check .` | 1151 个文件格式通过，lint 通过 |
| 固定 M1–M7 PostgreSQL gate | `POSTGRES_TEST_URL=<redacted> make test-project-foundation-postgres` | `275 passed, 0 skipped, 0 failed`，约 211 秒 |
| 前端单元测试 | `cd frontend && pnpm test` | 188 个文件，`1349 passed, 0 skipped` |
| 前端检查 | `cd frontend && pnpm check` | ESLint 与 TypeScript 通过 |
| production build | `cd frontend && pnpm build:production` | 78/78 页面完成 |
| static build | `cd frontend && pnpm build:static` | 78/78 页面完成 |
| 完整 Chromium E2E | `cd frontend && env -u PLAYWRIGHT_SKIP_WEB_SERVER -u PLAYWRIGHT_BASE_URL CI=1 PLAYWRIGHT_USE_SYSTEM_CHROME=1 pnpm test:e2e` | `109 passed`，约 3.1 分钟 |

完整 E2E 之前曾错误地以 `PLAYWRIGHT_SKIP_WEB_SERVER=1` 指向真实 `2026` 服务；该模式无法注入
E2E 专用的 auth-disabled SSR 环境，登录用例因此失败。该次命令不计为产品门禁结果；恢复
Playwright 专用 webServer 后完整 109 项一次通过。分组复核另得到 assets `2/2`、
automations `12/12`、governance `11/11`、data/isolation/projects `14/14`、chat `46/46`。

固定 20 文件 PostgreSQL gate 与本轮必需验收用例要求 0 skip；全量套件中有明确环境原因的
既有 skip 不会被误写成 PostgreSQL gate 通过。

## 真实浏览器多轮验收

主线程：

```text
70bec0fd-f5c5-4cf9-8bbb-3e218b3e269c
```

隔离线程：

```text
b0ae802c-0de0-475f-a27f-545e8a3b18af
```

### 正式验收步骤

| 步骤 | Thread | Run ID | 实际工具与断言 | 证据 |
| --- | --- | --- | --- | --- |
| R1 | 主线程 | `4db3cf8c-496d-439a-8656-4435f2840b56` | 当前上传后由 `read_file` 读取精确 marker | `01-current-upload-read.jpg` |
| R2 | 主线程 | `e4a5bed8-d4cd-4efe-9d4c-988fb087d07b` | `list_uploaded_files` 找到历史上传，再由 `read_file` 读取 | `02-historical-upload-list.jpg` |
| R3 | 主线程 | `6f0705c0-0b74-49b5-b192-51b94afa7085` | 刷新页面后历史列表和读取能力仍恢复 | `03-refresh-history-restored.jpg` |
| R4 | 隔离线程 | `b2033957-0c3f-4bda-88e8-72cfaf61b5c9` | 新线程历史上传列表为 0，证明线程隔离 | `04-new-thread-isolation.jpg` |
| R5 | 主线程 | `eb9868db-2acb-4f93-b2ac-6dd6507eec39` | `grep` 产生 23,794 字符结果并触发私有外置 | `05-grep-toolresult-externalized.jpg` |
| R6 | 主线程 | `7c6c381f-66be-42ab-b15a-e5b711ece31b` | 再次刷新后用 `read_file` 读取外置结果 | `06-refresh-externalized-read.jpg` |

R5 使用已上传的 `backend/uv.lock`，调用：

```text
grep(path=/mnt/user-data/uploads/uv.lock, pattern="url =", max_results=100)
```

外置阈值为 12,000 字符，实际结果为 23,794 字符，写入：

```text
/mnt/user-data/workspace/.tool-results/grep-06ac1acd5bff-5f8143e3b089.txt
```

该路径位于 workspace 的内部 `.tool-results`，不是用户可见 outputs。`read_file` 与兼容名
`read_file_tool` 默认豁免 ToolOutputBudget，避免“读取外置文件后再次外置”的循环；
`sandbox.read_file_output_max_chars` 默认限制为 50,000 字符。

### Run 与模型调用统计

| 类型 | Run ID | LLM 调用数 | 页面累计 Tokens | finalization |
| --- | --- | ---: | ---: | --- |
| R1 | `4db3cf8c-496d-439a-8656-4435f2840b56` | 2 | 19,370 | complete |
| R2 | `e4a5bed8-d4cd-4efe-9d4c-988fb087d07b` | 3 | 31,484 | complete |
| R3 | `6f0705c0-0b74-49b5-b192-51b94afa7085` | 3 | 33,995 | complete |
| R4 | `b2033957-0c3f-4bda-88e8-72cfaf61b5c9` | 2 | 19,387 | complete |
| probe：`read_file` 豁免 | `e5011fa8-cf26-4bca-921a-76a927fbd564` | 2 | 33,881 | complete |
| probe：host bash 不开放 | `ae237957-fd56-4ef3-9fe8-c37c331d882c` | 2 | 45,411 | complete |
| probe：`web_fetch` 内部截断 | `0f723306-0e57-4e00-b837-c43cecd46652` | 2 | 48,256 | complete |
| R5 | `eb9868db-2acb-4f93-b2ac-6dd6507eec39` | 2 | 52,321 | complete |
| R6 | `7c6c381f-66be-42ab-b15a-e5b711ece31b` | 2 | 66,180 | complete |

9 个 Run 均完成 finalization，共 20 次真实外部 LLM 调用。三个 probe 的 Run 自身执行完成，
但没有满足外置验收前提，因此不计为能力通过：

- `read_file`/`read_file_tool` 默认豁免，且读取输出自身限制为 50,000 字符；
- `bash` 工具组虽被准入，但本地安全配置 `allow_host_bash=false`，所以 host bash 明确拒绝；
- `web_fetch` 在 ToolOutputBudget 之前已把响应截到阈值以下。

数据库中共有 9 条成功的 `run.files_finalized` 聚合审计。R5 为
`created_count=1, committed_bytes=23794`，其余 Run 的文件聚合为 0。审计不记录文件名、
路径、正文、locator 或原始 Run target。

## 截图清单

所有截图均为 JPEG、1280×720。OCR/文本脱敏扫描未发现账号原始前缀、完整邮箱、
认证口令或密码字段；截图也未显示 Cookie、token、数据库 URL、模型 key 或 Host locator。

| 文件 | 字节 | SHA-256 | 可见断言 |
| --- | ---: | --- | --- |
| [01-current-upload-read.jpg](01-current-upload-read.jpg) | 70,149 | `97934780b3b88bc50dd84b7e5b96c9e6cc13b681031b2fa0edac34f155e93cd9` | 当前上传被读取 |
| [02-historical-upload-list.jpg](02-historical-upload-list.jpg) | 56,191 | `0240a197f2e6deb021090abfbc71087db720089ff669314fee21e002e638ffed` | 历史上传可枚举并读取 |
| [03-refresh-history-restored.jpg](03-refresh-history-restored.jpg) | 70,836 | `3b0354c536f1f6fc7f1a6a210510c4a6d6f5370d517baf78178e27e83ced0dc9` | 刷新后 authority manifest 恢复 |
| [04-new-thread-isolation.jpg](04-new-thread-isolation.jpg) | 62,656 | `f011064d3575e9dd2bc3b4017cca31067498e7d799a0da475e58d7ba07ff6554` | 新线程看不到主线程历史上传 |
| [05-grep-toolresult-externalized.jpg](05-grep-toolresult-externalized.jpg) | 74,071 | `f8fd8b40d51ac90f755df91b56da1d922d2fc944767db500190884b5136e2ffe` | `grep` 结果超过阈值并写入内部路径 |
| [06-refresh-externalized-read.jpg](06-refresh-externalized-read.jpg) | 68,199 | `8d192c491cac45dea3c3e16ab34f29d3e0ccb764099b486295ea785cf329d38b` | 刷新后仍能读取外置结果 |

上传用的纯文本 fixture 为
[`m15-history-20260730.txt`](m15-history-20260730.txt)。

## 普通开发服务恢复结果

隔离环境清理后执行 `make dev-daemon`。依赖检查和同步通过，但 Gateway 按设计 fail closed：

```text
M7_RECREATE_REQUIRED
缺失表: channel_inbound_deliveries
```

随后只读执行 `make check-db`，确认业务库仍是 `deerflow`、用户仍是 `postgres`、marker 仍为
`full_schema_v1`，但当前 checkout 的精确 catalog 判定为 `recreate_required`。这不是隔离库
删除造成的连接失败，而是现有业务库缺少当前代码要求的 Module 14 表
`channel_inbound_deliveries`。仓库明确禁止增量补表、stamp 或复用漂移数据库，因此没有
自作主张删除/重建业务库，也没有临时修改 `.env` 指向别的库。

收尾时 `2026`、`3000`、`8001` 均未监听。要恢复普通服务，必须由用户明确授权换空库并重新
执行 `make setup-db`；在此之前保留 fail-closed 状态。

## 明确未宣称通过与剩余 P2

- process-local Playwright browser automation 未迁入；真实浏览器只是验收 DeerFlow UI 和
  已迁入的模型/工具路径；
- vLLM、MindIE、GIF、Browserless、ACP 是自动化覆盖，不冒充真实对应 Provider 集成；
- 私有 ACP asset bridge 未启用，`include_acp=False`；
- Windows ACP descendant process tree 仍需 Job Object 或等价机制；
- 独立导出的 `strip_data_url_image_blocks()` 没有遍历预算，但生产公共路径走有预算的
  `serialize()`；
- 第三方 `model_dump()`、`dict()`、`__str__()` 在最终裁剪前执行，最终公开输出有硬上界，
  但这些受信任进程内 hook 自身的执行成本尚未被限制。
