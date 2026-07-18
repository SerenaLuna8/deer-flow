# M6 项目可靠执行、治理与恢复设计

- 日期：2026-07-16
- 状态：已完成（2026-07-18）
- 总体规格：`docs/superpowers/specs/2026-07-12-project-first-saas-design.md`
- 前置里程碑：M1、M2、M3、M4、M5 已正式完成
- 当前总体进度：6/8（75%）
- 后续里程碑：M7 legacy cleanup、M8 完整发布验收

## 1. 文档目的

本文档定义 DeerFlow M6 的专项产品、架构、数据、授权、迁移、运维和验收边界。M6 把 M4/M5
仍依赖单 Gateway 内存所有权的运行链升级为 PostgreSQL 驱动的独立 Worker 与持久化 SSE，补齐项目
配额、正式审计、平台运营面和可演练的通用备份恢复。

M6 采用“可靠运行链优先”的交付顺序：先建设通用任务与独立 Worker，再切换运行和 Automation，随后
交付持久化 SSE、配额、审计/平台管理，最后交付恢复链和总门禁。M6 是一个里程碑，但内部按可独立
审查的切片推进；每个切片必须保持已完成里程碑的隔离与数据完整性。

本文档不改变总体规格的冻结决策。发生冲突时优先级为：

1. 本文档明确引用的总体冻结决策；
2. 本文档的 M6 专项决策；
3. M5、M4、M3、M2、M1 专项规格中仍适用的既有契约；
4. 实施计划和代码注释。

## 2. 前置状态与 M6 边界

M5 已交付 project/owner-scoped Automation、持久化 occurrence、单 Gateway Scheduler、手动/自动触发、
restart reconciliation 和 staged migration。M5 的 occurrence lease 只协调 Automation 定义及其 occurrence，
不是通用 Worker 队列。M5 的 Gateway 仍直接创建 `asyncio` Agent task；项目 SSE 仍依赖进程内
`StreamBridge` 获得实时帧；多 Gateway 无法接管该内存所有权。

M6 接管以下职责：

- `jobs`、`job_attempts`、`dead_jobs` 和独立 Worker lease；
- Gateway、Worker、Scheduler 三种独立进程；
- 手动项目运行、Automation 运行和保留清理任务的统一入队；
- PostgreSQL 权威的 SSE 帧、`Last-Event-ID` 重放和跨 Gateway 重连；
- 项目成员、存储、并发运行和每日 MCP 调用配额；
- `project_usage_ledger`、原子用量计数和 80% 阈值状态；
- 只追加 `audit_logs`、项目 Admin 治理视图和 `system_admin` 平台运营面；
- PostgreSQL 通用加密备份、空库恢复、删除墓碑重放和恢复演练；
- M6 staged migration、cutover guard、readiness 和发布门禁。

以下职责继续不属于 M6：

- 删除最终 legacy source、legacy API、兼容 router 和旧配置项；这些属于 M7；
- 完整发布前的最终渗透、安全、隔离、容量和运维验收；这些属于 M8；
- 计费、订阅、发票、按量收费和支付；
- Redis、Kafka、外部消息总线、对象存储、跨区域复制和微服务拆分；
- 在线 PITR 产品、跨云备份编排和零数据丢失承诺；
- 自定义角色、自定义配额维度和项目间共享配额；
- 允许平台管理员读取提示词、消息、记忆、文件、产物或运行日志正文；
- 对任意第三方副作用承诺 exactly-once。

## 3. M6 目标与成功标准

M6 完成时必须满足：

1. Gateway 只负责认证、授权、事务 admission、入队、查询和 SSE，不执行 Agent graph。
2. Worker 是唯一 Agent graph 执行者；Scheduler 只计算到期 Automation 并创建 occurrence/job。
3. API、Worker、Scheduler 可以独立重启；已排队 job、已持久化事件和安全可恢复的执行不会丢失。
4. 多个 Gateway 可以同时服务项目 API 和 SSE，不依赖请求落到执行该 run 的进程。
5. 通用 job 采用至少一次投递，使用 lease token、心跳、幂等键和有界重试。
6. 已进入不确定外部副作用窗口的 run 不自动盲目重放；系统 fail closed 并记录可操作的 dead 状态。
7. SSE 的每一帧先持久化再唤醒消费者，重连按单调游标继续，不丢帧、不重复展示终态。
8. 项目配额在同一数据库事务内检查和预占；并发竞争不能突破硬限制。
9. 达到硬限制拒绝下一个消耗操作但不中断已启动运行；公共错误稳定为 `429`。
10. 审计只记录治理与运行元数据，数据库层拒绝更新或删除审计行。
11. 项目 Admin 只能查看当前项目的用量和审计；`system_admin` 不能借运营面读取私有内容。
12. 备份经过认证加密和完整性验证；恢复只写入新的空数据库，并在开放服务前重放备份后的删除墓碑。
13. M1–M6 的真实 PostgreSQL、前端、迁移、恢复和隔离门禁全部通过且 M6 为 0 skip。

## 4. 冻结决策

以下决策在 M6 实施期间视为冻结；改变任何一项必须先修订本文档并重新确认。

### 4.1 进程与基础设施

1. API、Worker 和 Scheduler 是独立进程，复用同一 Python 业务模块和同一 PostgreSQL。
2. Gateway 不创建 `run_agent()` task，也不持有执行 lease；它只创建持久化 Run 和 job。
3. Worker 通过 `FOR UPDATE SKIP LOCKED` 领取 job；PostgreSQL 是队列与所有权的唯一权威。
4. Scheduler 保留 M5 的 PostgreSQL session advisory lock，但锁由 Scheduler 进程持有，不再由 Gateway 持有。
5. M6 不引入 Redis。`LISTEN/NOTIFY` 只可作为低延迟提示，轮询和表中事实始终是权威。
6. Gateway 可以多进程和多副本；Worker 可以多进程；Scheduler 同一时刻只允许一个有效 owner。
7. `make dev`、本地 production 和 Docker 都必须启动 Gateway、Worker、Scheduler、Frontend、Nginx；
   `scheduler.enabled=false` 时不启动自动轮询，但手动 Automation API 仍可入队。
8. Worker、Scheduler、quota 和 recovery 配置属于 restart-required 配置，不能热重载活跃基础设施。

### 4.2 通用任务与运行所有权

9. M6 首批 job type 固定为 `private_run`、`automation_run` 和 `retention_purge`。
10. job payload 只保存不可变资源引用和公共控制字段，不复制提示词、消息、文件名、凭据或正文。
11. 每个 job 使用调用者生成或服务端派生的唯一幂等键；相同键只产生一个逻辑 job。
12. Run admission 和 job 创建位于同一事务。API 不能先返回 Run 再异步尝试入队。
13. Automation occurrence、Run 和 job 的关联在一个事务边界内完成；Scheduler 不直接调用运行时。
14. Worker 每次 claim 生成不可猜测的 lease token；heartbeat、完成、重试和 dead transition 都必须携带该 token。
15. 过期 owner 不能提交终态。旧 Worker 在 lease 丢失后必须停止新副作用并放弃完成写入。
16. job 采用至少一次投递；重复领取必须复用同一 Run 和同一不可变 asset/version snapshot。
17. `private_run` 和 `automation_run` 在首次执行前允许对暂时性数据库、模型初始化或 sandbox 获取失败做有界重试。
18. 已经产生 durable checkpoint 的 run 可以从最后一个确认 checkpoint 接管；不能证明副作用边界安全时转入
    `dead_jobs`，公共原因固定为 `SIDE_EFFECT_STATE_UNKNOWN`，不自动重放。
19. 外部副作用使用 `(run_id, checkpoint_id, tool_call_id)` 作为 DeerFlow 内部幂等坐标。支持上游幂等键的
    provider 必须传递该坐标；不支持的 provider 在 crash ambiguity 下采用 fail-closed。
20. dead job 只保存 job/run/attempt 引用、公共错误码、时间和可重试安全级别，不保存异常正文或输入。
21. 平台界面只允许重新入队 `retry_safety='safe'` 的 dead job；`unknown` 和 `unsafe` 必须由资源所有者重新发起新 Run。
22. cancel 先持久化 cancellation request，再由当前 lease owner 中止；没有 owner 时下一次 claim 直接结算 cancelled。

### 4.3 持久化 SSE

23. M6 将 SSE 帧作为 `run_events` 中 `category='stream'` 的只追加事件持久化，不新增第二套事件真相。
24. SSE 事件 ID 使用当前私有 Thread 内的单调 `seq` 十进制字符串；过滤到单个 Run 后允许出现正常间隔。
25. `Last-Event-ID` 只接受规范非负十进制整数；过长、负数、溢出或非数字值返回稳定 `400 STREAM_CURSOR_INVALID`。
26. Worker 必须先提交 SSE 事件事务，再发送可选 PostgreSQL notify。消费者收到 notify 后仍重新查询表。
27. terminal Run 必须持久化唯一 `stream.end` 事件。重复完成、lease takeover 和重试不能写入第二个终止帧。
28. Gateway 按页读取 `seq > cursor` 的事件；空页等待 notify 或轮询，到 heartbeat 间隔发送 SSE comment。
29. 慢消费者不占用 Worker 内存队列。客户端断线后使用最后确认 ID 重新读取 PostgreSQL。
30. SSE 查询必须使用 `project_id + owner_user_id + thread_id + run_id` 完整作用域。
31. stream event 与 Run 使用相同保留期；删除 Run 时按既有私有级联边界删除相应事件。
32. M6 cutover 前的历史 Run 保留消息/事件查询能力，但不伪造不存在的实时 stream 帧。

### 4.4 配额和用量

33. 平台默认上限固定为：有效成员 20、项目存储 5 GiB、并发运行 3、MCP 调用每天 10,000 次。
34. `system_admin` 可以设置不超过部署级绝对上限的平台默认值或单项目 override。
35. 项目 Admin 只能设置比当前平台有效值更严格的项目值，不能放宽平台上限。
36. `project_quotas` 保存策略；`project_usage_counters` 保存可锁定的当前计数；`project_usage_ledger` 只追加增量事实，
    数据库 trigger 拒绝修改或删除已提交 ledger。
37. 每个 ledger 写入使用唯一幂等键；计数更新和 ledger append 必须在同一事务。
38. 并发 Run 在 job 创建事务中预占，进入任何终态时恰好释放一次；排队和 retry-wait 都占用并发名额。
39. 存储在文件/产物成功持久化时增加，在物理清除后减少；上传 staging 不计入已用量但受单文件上限限制。
40. MCP 每日计数使用项目时区无关的 UTC 自然日 bucket，在真正发起 provider 调用前原子增加。
41. 成员配额在邀请兑换、新成员恢复和重新加入事务中检查；已有成员不会因降低上限被自动移除。
42. 达到 80% 时写入去重的阈值治理事件并向项目 Admin 显示提醒；同一维度同一阈值窗口不重复刷屏。
43. 达到硬限制返回 `429` 和稳定 code：`MEMBER_QUOTA_EXCEEDED`、`STORAGE_QUOTA_EXCEEDED`、
    `RUN_QUOTA_EXCEEDED` 或 `MCP_QUOTA_EXCEEDED`。
44. 配额耗尽不停止已经获得 lease 的运行；权限撤销和项目暂停仍可以终止活动运行。
45. 非 Admin 成员在 workspace 卡片只看到 `normal`、`near_limit` 或 `blocked`；只有项目 Admin 看到精确用量。

### 4.5 审计与平台管理

46. `audit_logs` 是只追加治理账本；数据库 trigger 拒绝普通 `UPDATE` 和 `DELETE`。
47. 审计更正通过新的 `audit.corrected` 补偿记录完成，不改写原行。
48. 审计允许字段固定为 actor、platform role、project、action、target kind、不可逆 target hash、outcome、
    public error code、request ID、job/attempt 公共坐标和白名单计数。
49. 审计禁止提示词、消息、记忆、运行日志、checkpoint、文件名、路径、附件、产物、credential 元数据、
    token、OAuth state、原始异常和任意请求/响应 payload。
50. 私有资源标识进入审计前使用部署级 audit key 做域分离 HMAC；API 不返回原始私有 ID。
51. M3 的 `SharedAssetGovernanceEventSink` 接入正式 audit sink，但接口仍只接受既有最小字段。
52. 项目治理、成员/邀请、生命周期、共享资产、Automation、配额、Run admission/cancel/terminal、job dead/requeue、
    备份与恢复都写审计；高频 token、stream frame 和工具输出不逐条写审计。
53. 项目 Admin 通过 project-scoped repository 查看本项目审计和精确用量；其他项目角色没有该 capability。
54. `system_admin` 的平台运营面可以查看项目元数据、暂停状态、配额、Worker/Scheduler 健康、job 聚合、
    dead job 公共状态、审计元数据和恢复演练结果。
55. 平台运营面不能查询 Thread、消息、Memory、文件、产物、checkpoint、stream content 或 Run 输入/输出。
56. 平台 override 使用显式 system governance context，不能伪造 ProjectContext 或 owner 身份。
57. 浏览器界面不执行数据库 restore，也不展示备份密钥、archive locator 或恢复连接串。

### 4.6 备份、删除墓碑与恢复

58. 通用备份使用 `pg_dump` custom format 作为数据库快照输入，并流式封装为 DeerFlow 认证加密 archive。
59. archive 使用独立 `DEER_FLOW_BACKUP_KEY`；它不能与数据库密码、Auth secret 或 credential keyring 共用。
60. archive 采用分块 AEAD，每块绑定 archive ID、chunk index、schema revision 和 source installation ID；
    manifest 保存版本、非敏感计数、密文 chunk hash、工具版本和删除墓碑 high-watermark。
61. 备份目录必须在仓库外，目录权限 0700、文件权限 0600；临时文件原子发布，失败时不留下可误认的完成 archive。
62. `backup-db` 是只读 trusted operation；它不暂停业务，但只有通过 PostgreSQL 一致性快照完成的 archive 才有效。
63. 物理保留清理在删除数据库行前，必须先把加密删除墓碑追加到仓库外的 recovery journal 并 `fsync`。
64. recovery journal 是 hash-chained、单调编号、认证加密的追加文件；明文标识只存在于解密后的 operator 内存中。
65. journal 写成功而数据库清理失败是安全可重试状态；journal 写失败时禁止数据库物理清理。
66. `retention_purge` 只处理已超过 30 天窗口且再次锁定验证的数据，使用墓碑编号作为幂等键。
67. restore 只允许目标为新的空数据库；命令拒绝覆盖已有 DeerFlow 表或连接当前业务数据库。
68. restore 顺序固定为：验证 manifest 和完整链 -> 解密并 `pg_restore` -> 校验 revision/表/约束 ->
    重放 archive high-watermark 之后的全部删除墓碑 -> 运行 M1–M6 data probes -> 写恢复证明。
69. recovery journal 缺失、链断裂、key 不匹配、high-watermark 回退或 tombstone gap 时 restore 必须 fail closed。
70. restore 完成不自动切换 `DATABASE_URL`，不自动启动 Gateway/Worker/Scheduler；operator 显式切换后再运行 `make check-db`。
71. 每次 release gate 必须从新 archive 恢复到随机 `deerflow_test_*` 数据库并证明已清除数据不会复活。

## 5. 目标进程拓扑

```text
Browser / IM
    |
Nginx
    |
FastAPI Gateway replicas
    |-- auth + ProjectContext
    |-- admission + quota reservation
    |-- create Run + Job transaction
    |-- durable SSE reader
    |
PostgreSQL
    |-- jobs / attempts / dead jobs / worker nodes
    |-- runs / run_events / checkpoints
    |-- automations / occurrences
    |-- quotas / usage ledger
    |-- audit / recovery metadata
    |
Worker processes
    |-- SKIP LOCKED claim
    |-- lease heartbeat
    |-- run_agent + sandbox
    |-- terminal settlement
    |
Scheduler process
    |-- singleton advisory lock
    |-- reserve occurrence + enqueue job
    |
Operator-only recovery commands
    |-- encrypted backup archive
    |-- external encrypted tombstone journal
```

Gateway、Worker 和 Scheduler 使用普通 `deerflow_app` role。建库、migration 使用 `deerflow_migrator`；
备份、恢复、恢复演练和 journal 管理使用受控 `deerflow_operator`。应用 role 不获得建库或 schema 变更权限。

## 6. 数据模型

### 6.1 `jobs`

核心字段：

- `id UUID` primary key；
- `job_type`：`private_run | automation_run | retention_purge`；
- `project_id`，项目治理任务非空；
- `owner_user_id`，私有运行非空；
- `run_id`，运行 job 非空；
- `automation_occurrence_id`，Automation job 非空；
- `idempotency_key`，全局唯一或按 job type 唯一；
- `status`：`queued | leased | running | retry_wait | succeeded | failed | cancelled | dead`；
- `available_at`、`priority`；
- `attempt_count`、`max_attempts`；
- `lease_owner_id`、`lease_token_hash`、`lease_expires_at`、`heartbeat_at`；
- `retry_safety`：`safe | unknown | unsafe`；
- `public_error_code`；
- `created_at`、`started_at`、`completed_at`、`updated_at`。

job 不存储 prompt、message、model response、credential、file path 或原始异常。运行所需私有输入继续位于现有
project/owner-scoped Run authority；Worker 领取后按完整作用域加载。

### 6.2 `job_attempts`

每次成功 claim 追加一行：

- `id UUID`；
- `job_id`；
- `attempt_number`，与 `(job_id, attempt_number)` 唯一；
- `worker_id`；
- `lease_token_hash`；
- `started_at`、`heartbeat_at`、`finished_at`；
- `outcome`：`succeeded | retry | cancelled | failed | lease_lost | dead`；
- `public_error_code`；
- `checkpoint_cursor` 和 `stream_cursor`，只保存非内容游标。

attempt 行不更新历史结果；活跃 heartbeat 写当前 attempt 的专用活跃字段，终态后 trigger 禁止再次修改。

### 6.3 `dead_jobs`

`dead_jobs` 对每个 dead job 保存一条不可变投影：`job_id`、project、owner hash、job type、attempt count、
retry safety、public error code 和 dead time。safe requeue 创建带 `predecessor_dead_job_id` 的新 job，并追加 audit
resolution event；它不更新或删除 dead 投影。系统管理 API 不返回 Run 私有输入。

### 6.4 `worker_nodes`

Worker 启动时注册随机 process-lifetime `worker_id`，定期写 `started_at`、`heartbeat_at`、版本、能力集合、
并发上限和 drain 状态。不得保存 hostname、容器 secret、环境变量或连接串。Gateway 使用 fresh heartbeat
判断是否有可接收 job 的 Worker；无健康 Worker 时新运行返回 `503 WORKER_UNAVAILABLE`，已有 job 仍保留。

### 6.5 `project_quotas`、`project_usage_counters`、`project_usage_ledger`

`project_quotas` 每项目一行，字段允许 null 表示继承平台值，包含 optimistic version。`project_usage_counters`
保存 member、storage、reserved run 和 UTC-day MCP 计数/日期。`project_usage_ledger` 追加 `dimension`、`delta`、
`bucket`、`source_kind`、source HMAC、idempotency key、request ID 和时间。

所有 counter 变化都必须同时追加 ledger。reconciliation 命令只允许从权威业务表重算 counter，并用新的
`usage.reconciled` ledger 记录差异，不能编辑旧 ledger。

### 6.6 `audit_logs`

审计行包含：

- `id UUID` 和单调 `occurred_at`；
- `actor_user_id` 或 trusted process actor；
- `actor_platform_role`；
- `project_id`；
- `action`、`target_kind`、`target_ref_key_id`、`target_ref_hmac`；
- `outcome`、`public_error_code`；
- `request_id`、`job_id`、`attempt_id`；
- 经过 schema allowlist 验证的 `metadata_json`。

repository 拒绝未知 metadata key。数据库 trigger 拒绝 update/delete。查询使用 `(project_id, occurred_at, id)`
或平台 `(occurred_at, id)` 游标，不提供全文搜索私有数据。

### 6.7 `deletion_tombstones` 与 `reliability_cutover_state`

数据库内 `deletion_tombstones` 保存 journal sequence、ciphertext digest、resource kind、committed time 和 purge
状态，不保存 journal 明文。外部 recovery journal 保存真正可重放的加密坐标。

`reliability_cutover_state` 是 singleton marker，至少记录：stage、migration run、source probe、active-run probe、
quota backfill probe、job relation probe、audit trigger probe、stream probe、recovery probe、cutover time 和 revision。

## 7. Job 状态机与并发规则

```text
queued / retry_wait
        |
        | claim: SKIP LOCKED + lease token
        v
      leased
        |
        | execution CAS
        v
      running ---------------------> succeeded
        |   |                           |
        |   +---- cancel request ------> cancelled
        |
        +---- retryable before unsafe boundary ----> retry_wait
        |
        +---- attempts exhausted / unsafe ambiguity -> dead
```

claim 在一个短事务中完成：选择 `available_at <= now()` 的候选、锁定、验证项目状态和 cancellation、创建 attempt、
设置 lease。运行事务不能长期持有数据库锁。heartbeat 只延长当前 token 的 lease；Worker 必须在 `lease_seconds / 3`
内心跳。lease 到期后，新 Worker 可以领取；旧 Worker 的下一次 authority check 失败并停止。

Worker 在以下边界重新验证项目、成员、能力、asset snapshot、credential grant 和配额 reservation：claim 后、sandbox
启动前、每个外部副作用前和 terminal settle 前。权限撤销走 M4 authorization cancellation；配额不在运行中途撤销。

## 8. Run 与 Automation 切换

### 8.1 手动项目 Run

`start_private_run()` 被拆分为纯 admission/enqueue 与 Worker execution 两部分：

1. Gateway 验证 ProjectContext、Thread、asset selection 和请求结构；
2. 事务锁定 quota counter 并预占 run slot；
3. 创建 pending Run、asset/grant snapshot 和 `private_run` job；
4. 提交后返回 pending Run；
5. Worker claim 后重新验证并 materialize runtime；
6. Worker 绑定 Run execution lease，执行 `run_agent()`；
7. terminal settle 同时完成 Run、job、quota release、stream end 和 audit metadata。

### 8.2 Automation

Scheduler 仍先持久化唯一 occurrence。M6 把 occurrence admission 改为在同一事务内创建 Run、quota reservation 和
`automation_run` job。Scheduler 不拥有 Run task，不等待 Agent 结束。Worker terminal hook 继续通过 M5
`AutomationReconciler` 的 authoritative relation 结算 occurrence，但结算必须携带当前 execution lease。

Scheduler restart reconciliation 只修复 occurrence/job 关系，不自动重放已有 Run。一个 admitted occurrence 只能关联
一个 Run 和一个逻辑 job。手动 trigger 与自动 trigger 使用相同链。

### 8.3 保留清理

`retention_purge` 由 Scheduler 创建，由具备 recovery journal capability 的 Worker 执行。普通 Worker 未配置 journal
时不得 claim 该类型。清理先验证 30 天窗口、当前 membership/project 状态和恢复/重新加入竞争，再写外部墓碑并清理。

## 9. 持久化 SSE 协议

M6 保持现有 SSE event 名和 data envelope，避免前端重写消息语义。变化只在 transport authority：

- producer 调用 PostgreSQL stream writer；
- writer 将 frame 写入同一 `run_events` 表；
- subscribe 按作用域和游标分页；
- optional notify 只缩短等待；
- `stream.end` 作为权威终止；
- reconnect 可落到任意 Gateway。

响应继续返回 `Cache-Control: no-cache`、`Connection: keep-alive` 和 `X-Accel-Buffering: no`。创建并 stream 的
请求在 job 入队后立即订阅；Worker 尚未 claim 时 Gateway 发送 heartbeat。`on_disconnect=cancel` 只持久化 cancel request，
不能尝试取消本地 task。

若客户端提供的 cursor 小于已清理最小 cursor，返回 `409 STREAM_CURSOR_EXPIRED` 并指向权威消息/Run 状态 reload；
不得从当前内存状态猜测缺失帧。

## 10. 配额服务与产品行为

### 10.1 权威检查点

- member：邀请兑换、重新加入、成员恢复；
- storage：文件/产物 finalize 前；
- concurrent run：Run+job 创建事务；
- MCP daily call：provider network call 前。

项目 Admin 的 quota 页面显示 limit、used、reserved、percentage、threshold state 和最近 ledger 聚合。页面不展示
其他用户的用量明细。MCP 显示项目日聚合，不显示 server、tool arguments 或调用内容。

### 10.2 降低上限

新上限低于当前用量时允许保存，但项目进入 `blocked` 对应维度；不删除成员、文件或活动运行。下一次增加消耗被拒绝，
减少消耗允许执行。提高项目自定义上限不能超过平台有效值。

### 10.3 Counter reconciliation

启动不做无界全表重算。operator 命令和定期低优先级 job 可以按项目重算；发现差异追加审计和 ledger，修正 counter。
release gate 必须证明重复 reconciliation 幂等且并发业务写不会丢失。

## 11. 审计、项目治理和平台运营面

### 11.1 项目页面

项目菜单新增：

- `/projects/{project_slug}/settings/usage`；
- `/projects/{project_slug}/settings/audit`。

两页都由服务端 capability gate 限制为 Admin。审计提供时间、actor、action、target kind、outcome、request ID 和公共
错误码筛选，不提供任意 JSON/全文查询。项目 Admin 不能看到平台级或其他项目记录。

### 11.2 平台页面

`/admin` 扩展为平台壳层，保留 `/admin/assets`，新增：

- `/admin/operations`：Gateway/Worker/Scheduler readiness、queue/dead 聚合、恢复演练状态；
- `/admin/projects`：项目元数据、暂停/恢复、有效配额和阈值状态；
- `/admin/audit`：平台和项目治理元数据查询；
- `/admin/jobs`：dead job 公共字段及 safe requeue。

`frontend/src/app/admin/layout.tsx` 继续服务端强制 `system_admin`，普通用户返回 404。平台页面所有数据由专用
admin repository 返回，不能复用 unscoped private repositories。

### 11.3 平台动作

平台 Admin 可以：暂停/恢复项目、设置平台允许范围内的 quota override、safe requeue dead job、查看恢复证明。
平台 Admin 不能：进入用户 Thread、下载文件、查看 Memory、读取 Run kwargs、修改 occurrence owner、以用户身份执行 Run、
从 UI 发起 restore 或显示 backup/journal locator。

## 12. 配置和命令

### 12.1 配置

新增 restart-required `worker`：

- `enabled`；
- `poll_interval_seconds`；
- `lease_seconds`；
- `heartbeat_seconds`；
- `max_concurrent_jobs`；
- `shutdown_grace_seconds`；
- `default_max_attempts`；
- `retry_initial_seconds`；
- `retry_max_seconds`。

新增 `quotas`：平台四项默认值、部署绝对上限和 threshold（固定默认 0.8）。新增 `recovery`：archive chunk size、
operator archive root、external journal path 和 journal fsync policy。secret 只从环境变量读取，配置文件只保存路径和数值。

### 12.2 命令

根命令新增：

```bash
make worker
make scheduler
make migrate-reliability ARGS="--dry-run --backup-proof /secure/m6-proof.json"
make migrate-reliability ARGS="--execute --maintenance-acknowledged --backup-proof /secure/m6-proof.json"
make backup-db ARGS="--output /secure/backups"
make restore-db ARGS="--archive /secure/backups/<archive> --target-url <new-db-url> --journal /secure/recovery/tombstones --execute"
make reconcile-usage ARGS="--dry-run"
make reconcile-usage ARGS="--execute --project-id <uuid>"
make drill-restore ARGS="--archive /secure/backups/<archive> --journal /secure/recovery/tombstones"
```

restore、usage execute 和 migration execute 都是 trusted operations。命令输出只包含 archive ID、revision、聚合计数、
状态、截断 checksum 和证明路径；不得打印数据库密码、private ID、owner map、archive 明文或 tombstone 坐标。

## 13. Migration 与 cutover

M6 使用 expand/finalize 两阶段 Alembic revision，计划 ID 为 `0014_project_reliability_expand` 和
`0015_project_reliability_finalize`。最终 ID 在实施计划中保持一致，不允许不同文件使用漂移名称。

### 13.1 Dry-run

dry-run 必须零 schema write、零 ledger write、零 marker write，输出：

- 当前 revision 和 M4/M5 marker；
- active/pending Run 与 active Automation occurrence 聚合；
- 项目、成员、文件/产物、MCP 当日可回填计数；
- job/run/occurrence 关系预检；
- audit sink 和 recovery 配置可用性；
- operator backup/restore proof 状态；
- 稳定 source fingerprint。

### 13.2 维护窗口

execute 前停止 Gateway、旧 M5 Scheduler、Worker、IM inbound 和所有 embedded writer。创建并验证独立 PostgreSQL
backup/restore proof。非终态 legacy Run 或已 admitted 但未终态 occurrence 必须先由 M5 reconciliation 结算；迁移不猜测
是否安全重放。仍存在不确定活动执行时 fail closed。

### 13.3 Expand 与回填

expand 创建 M6 表、索引、trigger 和 nullable relation。回填：

- 每项目 quota policy 和 usage counter；
- member/storage/current-day MCP 可证明的当前计数；
- Run/job relation 只对 M6 cutover 后的新 Run 生效，不为历史完成 Run 伪造 job；
- audit 从 cutover 时刻开始，不伪造历史 actor/action；
- stream replay 从 cutover 后 Run 开始，不伪造历史 frame。

### 13.4 Finalize

finalize 验证：M4/M5 final marker、active legacy execution 为零、quota count、复合关系、append-only trigger、Worker claim、
stream replay、backup restore 和 tombstone replay。随后收紧非空/外键/唯一约束并写 `cutover_complete`。

marker 完成后项目 Run 与 Automation 只能走 job/Worker path。旧 in-Gateway execution helper 由 M6 guard 关闭但代码保留到
M7。M6 marker 前继续使用已发布 M5 路径；不长期双写。

## 14. Readiness、错误和降级

Readiness 分开报告：database、schema/cutover、gateway admission、worker fleet、scheduler owner、stream reader、recovery
journal 和 quota/audit sink。

- 数据库或 M6 schema 不可用：`503 DATABASE_UNAVAILABLE` / `503 RELIABILITY_CUTOVER`；
- 没有 fresh Worker：新 Run `503 WORKER_UNAVAILABLE`，查询和历史数据仍可用；
- Scheduler disabled：自动触发暂停，手动 Automation 和普通 Run 可用；
- stream notify 不可用：退回 PostgreSQL 轮询，不关闭 SSE；
- quota 超限：稳定 `429`；
- recovery journal 不可用：禁止 retention purge，不影响普通 Run；
- audit write 失败：治理 mutation、Run admission 和平台动作 fail closed；高频 stream 写不重复写 audit；
- lease ownership 丢失：Worker fail-stop 当前 attempt，新 owner 按安全级别决定接管或 dead。

公共错误响应继续包含 `request_id`，不返回 SQL、host、PID、lease token、archive path 或异常正文。

## 15. 安全和隐私约束

1. 所有 job、quota、audit 和 stream 私有查询都包含项目与 owner scope；平台聚合接口使用单独白名单查询。
2. Worker 不信任 job row 中的角色或 capability；每次从当前数据库事实重建执行上下文。
3. lease token 只保存 hash，原 token 只存在当前 Worker 内存。
4. job payload 和 audit metadata 使用严格 schema，禁止自由透传 request body。
5. `system_admin` override 不创建 owner context，也不能 materialize private runtime。
6. backup key、audit HMAC key、credential keyring 和 Auth secret 分离并支持独立轮换。
7. archive 和 recovery journal 是高敏感 operator 数据，不得放在 repo、Web 下载或普通应用日志。
8. restore target URL 只通过环境/命令 secret input 提供，输出必须脱敏。
9. `LISTEN/NOTIFY` payload 只包含公开内部游标或随机 channel token，不包含项目、owner、Run 内容。
10. Worker 日志只记录截断 job/Run hash、attempt number、公共状态和 request ID。

## 16. 测试策略

### 16.1 单元与服务测试

- job state machine、lease token、heartbeat、expiry、retry backoff 和 dead transition；
- duplicate idempotency key；
- Run/job/occurrence transactional admission；
- quota reserve/release、ledger 幂等、80% 阈值和 limit lowering；
- audit allowlist、HMAC、append-only trigger 和补偿记录；
- SSE cursor parse、pagination、terminal uniqueness 和 expired cursor；
- archive chunk AEAD、manifest、journal chain 和 tombstone replay；
- public error mapping 和 log redaction。

### 16.2 真实 PostgreSQL 并发测试

- 两个 Worker 同时 claim，单 job 只有一个有效 lease；
- 旧 token heartbeat/complete 被拒绝；
- Worker crash 后 safe job 被接管，unsafe ambiguity 进入 dead；
- 两个 Gateway 同时创建同幂等 Run 只产生一个 job；
- 三个并发名额下第四个请求稳定 `429`；
- quota release、cancel 和 terminal race 不产生负计数或泄漏；
- M5 occurrence 并发 reservation 只关联一个 job；
- audit/usage trigger 拒绝 update/delete；
- stream producer、Gateway 和数据库重启后按 Last-Event-ID 继续；
- 跨项目、跨 owner、project outsider 无法读取 stream、usage、audit 或 job。

### 16.3 多进程集成测试

至少启动两个 Gateway、两个 Worker 和一个 Scheduler：

- 在 Gateway A 创建，在 Worker B 执行，在 Gateway C 重连 SSE；
- 执行中关闭 Worker，验证 lease takeover 或 safe dead；
- Scheduler session 丢失后 fail-stop，第二 Scheduler 在 PostgreSQL 释放锁后接管；
- Gateway 重启不结算或中断 Worker 持有的 Run；
- on-disconnect cancel 在任意 Gateway 持久化并由 Worker 执行；
- 无健康 Worker 时 admission 返回 503 且不泄漏 quota reservation。

### 16.4 备份恢复测试

使用随机 `deerflow_test_*` 源库和新目标库：

- 生成 archive、验证权限/manifest/chunk；
- 拒绝错误 key、损坏 chunk、非空目标、当前业务库和旧 journal；
- 备份后物理删除私有数据并追加 tombstone；
- restore 后重放 tombstone，证明行、checkpoint、file chunk、Memory/connection/Automation 私有关联不复活；
- 执行 `make check-db`、M1–M6 PostgreSQL probes 和 application smoke；
- 生成不含 secret/private ID 的 restore proof。

### 16.5 前端与浏览器测试

- 项目 Admin usage/audit 页面和非 Admin 404/无入口；
- system_admin operations/projects/audit/jobs 页面；
- 平台管理员无法导航或请求用户私有内容；
- quota near-limit/blocked UI；
- 429/503 稳定错误与 retry behavior；
- 同一 SPA 跨项目切换后旧 usage/audit/stream response 不污染新项目；
- SSE 断线重连不重复最终 assistant turn。

## 17. 发布门禁

M6 完成前必须提供：

1. M6 专项任务逐项 TDD 和独立审查结论；
2. backend full suite；
3. frontend full unit suite、`pnpm check` 和完整 Playwright；
4. M1–M6 固定真实 PostgreSQL integration workflow，M6 文件 0 skip；
5. 多 Gateway/Worker/Scheduler crash/reconnect gate；
6. 新 archive 到新数据库的完整 restore drill；
7. 删除墓碑不复活证明；
8. Ruff、format、compileall、workflow YAML 和 `git diff --check`；
9. `make doctor`、`make check-db`、Worker/Scheduler readiness；
10. 一次独立 M6 closure review 和集中修复；
11. README、README_zh、CHANGELOG、根/后端/前端 AGENTS、总体规格、运维文档和进度同步。

完成上述门禁后只能把总体进度更新为 6/8（75%）。M7 legacy cleanup 和 M8 完整发布验收仍未完成时，系统不得描述为
完整可发布的多用户 SaaS。

M6 的 operator 顺序以 `docs/operations/m6-reliability-migration.md` 和
`docs/operations/m6-backup-recovery.md` 为准：backup proof → dry-run → maintenance → execute →
`make check-db` → 固定 M1–M6 probes → 独立 restore drill。`0014`/`0015` 后只允许前向修复或认证 archive
恢复到新数据库；no downgrade，且 restore 不自动切换 traffic。

## 18. 内部交付切片

M6 按以下依赖顺序实施：

1. final-state schema、expand/finalize migration 和 cutover contract；
2. job repository、lease state machine、Worker registry 和配置；
3. 独立 Worker runtime、private Run execution authority 和 cancellation；
4. M5 Automation enqueue/settlement 与独立 Scheduler；
5. PostgreSQL stream writer/reader、Last-Event-ID 和 Gateway SSE；
6. quota policy、counter、ledger 与成员/存储/Run/MCP admission；
7. audit sink、治理接线和 append-only enforcement；
8. project Admin usage/audit API/UI；
9. system_admin operations/projects/jobs/audit API/UI；
10. encrypted backup、external tombstone journal、retention purge 和 restore；
11. staged migration CLI、readiness、Make/Docker/dev orchestration；
12. PostgreSQL、多进程、前端和恢复 release gate；
13. 文档、全量验证和独立关闭审查。

每个切片完成时必须能单独测试，并在正式 M6 cutover 前保持已发布 M5 路径可运行。M6 marker 完成后只允许前向修复，
或按本规格恢复到新的数据库；不执行降级 migration。实现计划可进一步拆任务，但不得改变上述依赖方向。

## 19. 关键权衡

- 选择 PostgreSQL 队列而不是 Redis/Kafka，保持 V1 单基础设施，但数据库承担更多短事务和轮询压力。
- 选择先持久化 SSE 再 notify，延迟略高于纯内存广播，但获得跨 Gateway 重连和重启恢复。
- 选择至少一次投递加 fail-closed 副作用边界，而不宣称无法证明的 exactly-once。
- 选择 counter + append-only ledger，增加写放大，但使高并发 admission 可锁定且保留可核对历史。
- 选择专用平台运营 repository，而不允许 `system_admin` 复用私有 unscoped repository，减少功能便利但守住隐私承诺。
- 选择恢复到新空数据库而不是 in-place restore，增加 operator 步骤但显著降低覆盖业务库的风险。
- 选择外部加密 tombstone journal，增加部署持久卷要求，但避免旧备份恢复后复活已经物理清除的数据。

## 20. 验收摘要

M6 验收的核心不是“增加后台页面”，而是把 DeerFlow 从单 Gateway 内存执行所有权升级为可验证的 PostgreSQL
可靠运行系统：Run/Automation 先持久化再入队，独立 Worker 持有有期限的 execution authority，SSE 可以从任意 Gateway
按游标恢复；项目消耗受原子配额保护，治理行为进入不含私有内容的只追加审计；operator 可以从认证加密 archive 恢复到
新数据库，并在开放服务前重放删除墓碑。

只有这些能力、迁移链、真实 PostgreSQL 并发测试、多进程重启测试和恢复演练一起通过，M6 才可以标记完成。

验收结果（2026-07-18）：Tasks 1–19 的实现与逐任务独立审查完成；固定 20 文件 M1–M6 PostgreSQL
release gate 由跨平台 Python runner 硬校验 `POSTGRES_TEST_URL` 并强制 0 skip，真实覆盖 Scheduler session
takeover、Worker SIGKILL/lease takeover、unsafe/unknown dead、跨项目不变式、两 Gateway 有序 SSE replay、
archive tamper/journal gap/new-database restore、quota/audit 和 Frontend static/cache。Task 20 同步 operator、
用户与开发文档，并以 fresh whole-branch gates 和独立 closure review 关闭 M6；M7/M8 仍保持未完成。
