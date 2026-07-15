# M4 项目私有工作专项设计

- 日期：2026-07-14
- 状态：实现与全量门禁候选完成，待独立审查
- 对应总体设计：`2026-07-12-project-first-saas-design.md`
- 前置里程碑：M1、M2、M3 已完成
- 里程碑：M4 — 私有对话、运行、文件、记忆和连接
- 数据库：PostgreSQL-only
- 权限边界：认证身份、不可变项目上下文、强制作用域 repository、数据库复合约束；不使用 PostgreSQL RLS

## 1. 文档目的

本文档定义 DeerFlow M4 的产品边界、作用域模型、持久化结构、运行链路、文件与沙箱、Memory、IM connection、API、Frontend、迁移与发布门禁。M4 把现有按用户隔离但不含项目边界的 Thread、run、checkpoint、文件、Memory 和 connection 改造成项目与所有者双重隔离的私有工作。

M4 保留现有 LangGraph runtime、流式响应、输入区、文件侧栏、运行控制、goal、compact、branch 和 human-input 体验。项目私有工作通过同一运行生命周期执行，不建设第二套 runtime。

M4 完成后，项目成员可以在项目内使用 M3 已发布并获准执行的 Agent、Skill 和 MCP 创建自己的私有对话。其他项目、同项目其他成员、项目 Admin 和平台 `system_admin` 均不能通过普通产品路径读取这些私有内容。

M4 不交付 scheduled task、Worker 租约、持久化 SSE、配额、审计平台、备份恢复或 legacy 路径删除。上述能力分别属于 M5、M6 和 M7。

当前 runnable-first 实现已经提供 project/owner scoped Chats、run、file/artifact、Memory、
Connections、frontend cache/client、cutover guard 与真实 PostgreSQL migration gate。产品入口仍由
final schema + `cutover_complete` marker + readiness + capability 共同控制，静态构建不暴露入口。
staged migrator 首版只迁 PostgreSQL legacy Thread/run/event/feedback 与 checkpoint metadata marker；
非空 legacy filesystem、Memory、file/artifact 或 connection source 在 DDL 前拒绝。当前
`--backup-dir` 为保留参数且 CLI 不消费 `DEER_FLOW_M4_BACKUP_KEY`，operator backup proof 是外部运维
前置条件，不把它描述为脚本内置的通用备份恢复。以上限制与独立审查结果决定 M4 是否最终完成。

## 2. 已冻结决策

1. M4 是一个里程碑，内部按四个验收门推进；全部通过前不开放项目私有工作。
2. 所有项目私有根资源必须具有非空 `project_id + owner_user_id`。
3. owner 永远来自认证身份和服务端项目上下文，不能从请求正文、query、header metadata 或 LangGraph configurable 中信任。
4. M4 建立统一的 `PrivateWorkContext`；它由服务端解析的 `ProjectContext` 派生。
5. `PrivateWorkContext` 固定包含 user、project、membership、role、capabilities、membership version 和 request ID。
6. 项目私有 repository 的公开业务方法只接受可信 context，不提供裸 `project_id` 或可选 owner 绕过。
7. 跨项目、跨所有者、失效 membership 和不存在统一返回 404。
8. 已确认处于当前项目但缺少 capability 返回 403。
9. Viewer 可以读取、导出和删除自己的既有私有数据，但不能创建 Thread、启动 run、上传新文件或建立新 connection。
10. Admin、Editor 和 Runner 可以在 capability 允许时创建自己的私有工作并执行共享资产。
11. 项目 Admin 和平台 `system_admin` 不获得用户私有内容读取权。
12. 每个项目 Thread 必须选择一个当前项目可执行的逻辑 Agent。
13. 系统 Agent 必须存在该项目 enabled binding；项目 Agent 必须属于当前项目且已发布。
14. 没有可执行 Agent 时不创建空白 Thread，Frontend 引导进入项目 Agent 页面。
15. 每个 run 在 admission 时消费 M3 resolver，并持久化精确 Agent、Skill、MCP version、checksum、grant ID 和 catalog generation。
16. credential 明文只能通过 M3 materializer 在当前工具调用前短暂出现，不能持久化到 M4 任何资源。
17. 现有 RunManager、LangGraph graph、checkpointer、stream bridge 和运行控制继续使用。
18. LangGraph checkpoint 表继续由 LangGraph 管理，不由 Alembic 添加项目列。
19. checkpoint 通过项目作用域适配器、全局唯一 Thread ID、服务端 scope marker 和 Thread 交叉校验实现 fail-closed 访问。
20. 项目路由和项目 run 不能取得或调用裸 checkpointer。
21. PostgreSQL 是上传、workspace 文件和产物的权威副本；沙箱目录只是运行期临时副本。
22. 文件整体和每个 chunk 都保存 SHA-256；上传、下载、恢复和写回均流式处理。
23. run 在安全文件写回成功前不能标记为 success。
24. Memory 按项目和所有者隔离；提取和注入只使用同一项目、同一用户的私有内容。
25. IM connection 在项目内创建；入站消息从 connection 解析可信 project 和 owner 后，才进入私有 Thread/run 链路。
26. 一组外部 provider identity 同时只能路由到一个 connected connection，避免入站项目歧义。
27. 成员退出或被移除后，private work 与 connection 依据现有 membership `retention_until` 冻结；重新加入后恢复。
28. M4 不物理清理冻结数据；物理清理、备份墓碑和恢复演练属于后续里程碑。
29. membership 失效、降为 Viewer、项目暂停或进入 pending deletion 时，失去执行权的活动 run 必须终止。
30. 权限撤销以数据库 cancellation marker 为权威，本地 RunManager 取消为低延迟优化；其他 worker 在下一模型、工具或副作用边界 fail closed。
31. 项目 API 使用明确的 UUID path scope，不使用可伪造 owner header 作为授权边界。
32. Project LangGraph client 和所有 Query key 同时按 account 与 project 隔离。
33. legacy 私有 API 保留到 M7，但 private-work cutover 后不能读取项目数据。
34. Embedded client 缺少可信项目作用域时，在 cutover 后 fail closed。
35. M4 使用 staged schema migration、显式 owner map、加密备份、幂等 ledger 和 cutover marker，不长期双写。

## 3. 目标与非目标

### 3.1 目标

- 为 Thread、run、event、feedback、checkpoint access、file、artifact、Memory 和 connection 建立项目与 owner 双重隔离。
- 让项目成员从项目页面创建和继续自己的私有对话。
- 让每个 run 记录完整且可复现的 M3 asset version closure。
- 让项目 MCP credential 只在获权项目 run 内短暂 materialize。
- 把上传、workspace 和 output 的权威副本迁入 PostgreSQL。
- 把现有 per-user file Memory 迁为 per-project per-owner PostgreSQL Memory。
- 把 user-owned IM connection 和 conversation 绑定到明确项目。
- 在成员权限撤销后阻止新工作并终止已失去授权的活动运行。
- 迁移 existing private work 到明确默认项目，保留 owner 和来源验证信息。
- 交付真实 PostgreSQL 隔离矩阵和 Frontend account/project cache 隔离测试。

### 3.2 非目标

- Scheduled task、scheduled task run 和 automation UI；属于 M5。
- 通用 jobs、job attempts、dead jobs、Worker 租约和至少一次任务投递；属于 M5。
- M5 目标形态的持久化 SSE reconnect 和跨 worker stream ownership。
- 项目配额、用量 ledger、审计查询和平台运营面板；属于 M6。
- 通用数据库备份恢复、删除墓碑重放和灾难恢复演练；属于 M6。
- 删除 legacy API、legacy filesystem 或兼容页面；属于 M7。
- 共享 Thread、共享文件、共享 Memory 或资源级 ACL。
- 项目 Admin 浏览成员私有工作。
- 外部对象存储、Redis、Kafka 或独立向量数据库。
- 自定义角色、组织层或跨项目私有数据迁移。

## 4. 术语与上下文

- **PrivateWorkContext**：从真实 `ProjectContext` 派生的不可变私有工作授权上下文。
- **owner**：认证用户本人；私有资源只能由 owner 读取。
- **project selector**：客户端路径中的项目 UUID，仅用于选择服务端要解析的项目，不携带可信授权信息。
- **project-scoped repository**：所有 SQL 条件都固定 project、owner、membership version 和项目状态的 repository。
- **run snapshot**：某个 run admission 时保存的精确 Agent、Skill、MCP version 与 credential grant 引用。
- **authority copy**：PostgreSQL 中可恢复、可校验的文件内容和元数据。
- **sandbox working copy**：从 authority copy 恢复的运行期临时文件。
- **frozen private work**：membership 已结束、仍在 30 天 retention window 内且不能从项目入口访问的私有数据。
- **private-work cutover**：迁移、约束、probe 和发布门禁全部通过后，允许项目私有入口并拒绝 legacy 私有访问的不可逆标记。

## 5. 总体架构

```text
authenticated user
  -> resolve immutable ProjectContext
  -> derive PrivateWorkContext
  -> require private-work capability
  -> scoped private-work service
  -> scoped repository / scoped checkpoint adapter
  -> M3 ProjectAssetResolver
  -> persist exact run snapshot
  -> materialize short-lived MCP secrets
  -> existing RunManager + LangGraph runtime
  -> PostgreSQL file finalization
```

M4 分为四个内部验收门：

1. 私有数据模型、复合约束、作用域 repository 和 staged migration。
2. Thread、run、checkpoint access、权限撤销与 M3 run snapshot。
3. PostgreSQL 文件、Memory、IM connection 和成员冻结恢复。
4. Project chat Frontend、account/project cache、legacy cutover 和完整隔离 gate。

每个门都必须具有独立单测和真实 PostgreSQL 证据。内部代码可以按门合入开发分支，但产品入口只在第四门全部通过后开放。

## 6. 授权模型

### 6.1 PrivateWorkContext

`PrivateWorkContext` 是 frozen value object，至少包含：

- `user_id UUID`
- `project_id UUID`
- `membership_id UUID`
- `role ProjectRole`
- `capabilities frozenset[Capability]`
- `membership_version int`
- `request_id str`

context 只能由认证 user ID、项目 UUID 和当前数据库 membership 解析。客户端传入的 owner、role、capability、membership version、system role、project context object 或双下划线内部字段全部丢弃。

### 6.2 Capability

M4 复用现有：

- `private_work.create`
- `private_work.read_own`
- `shared_assets.execute`

创建 Thread、上传、创建 connection 要求 `private_work.create`。启动 run 同时要求 `private_work.create` 和 `shared_assets.execute`。读取、导出和删除自己的现有数据要求 `private_work.read_own`。

Viewer 只有 read-own；Admin、Editor、Runner 具有 create 和 execute。Frontend 只读取服务端 capability，不从 role 推导。

### 6.3 Scope revalidation

resolver 完成后不把 context 视为永久授权。每个 repository mutation、run admission、模型调用边界、工具副作用边界、checkpoint state mutation 和文件 finalization 都重新验证：

- project 仍为 active；
- project 未暂停；
- membership 仍 active；
- membership version 与 context 一致；
- 所需 capability 仍存在；
- private resource 的 project 和 owner 与 context 完全一致。

失效 context 和不存在资源统一 404。资源已确认处于当前 scope 但 capability 不足返回 403。

## 7. 数据模型

### 7.1 通用 scope

所有私有根表统一具有：

- `project_id UUID NOT NULL`
- `owner_user_id VARCHAR(36) NOT NULL`
- 到 `projects(id)`、`users(id)` 的外键
- 到 `project_memberships(project_id, user_id)` 的复合外键

`project_memberships(project_id, user_id)` 已唯一，membership 结束只改变 status，不删除行，因此 private work 可以在 retention window 内保留并在重新加入后恢复。

既有 `threads_meta.user_id`、`runs.user_id`、`run_events.user_id` 和 `feedback.user_id` 在 finalize revision 中改为 `owner_user_id`。运行时接口可以在适配层保留历史参数名，但数据库不长期保留两套 owner 列。

### 7.2 Thread

物理表继续使用 `threads_meta`，避免为表名进行与 M4 无关的破坏性改写。它是 M4 的 Thread authority metadata，新增：

- `project_id`
- `owner_user_id`
- `agent_asset_id UUID`
- `agent_scope system|project`
- `frozen_at`
- `version BIGINT`

保持 `thread_id` 全局唯一，并增加 `UNIQUE(project_id, owner_user_id, thread_id)`。Thread 保存逻辑 Agent 引用，不保存固定 version；每个 run admission 重新解析当前可执行 version。

### 7.3 Run、event 与 feedback

`runs` 新增：

- `project_id`
- `owner_user_id`
- `authorization_cancel_requested_at`
- `authorization_cancel_reason`
- `finalization_status`

增加 `UNIQUE(project_id, owner_user_id, thread_id, run_id)`，并通过 `(project_id, owner_user_id, thread_id)` 复合外键关联 Thread。

`run_events` 和 `feedback` 增加相同 scope，并通过完整复合键关联 run。现有 `run_events(category='message')` 继续作为消息持久化和历史投影，不新增第二份 message 正文表。checkpoint 继续作为 graph resume state；两者目的不同，但所有访问都从同一 scoped Thread 开始。

### 7.4 Run asset snapshot

新增 `run_asset_versions`：

- `run_id` 与完整 private scope
- `asset_kind agent|skill|mcp`
- `asset_scope system|project`
- `asset_id`
- `version_id`
- `payload_checksum`
- `catalog_generation`
- `dependency_order`

新增 `run_mcp_grant_snapshots`：

- 完整 run scope
- `mcp_version_id`
- `credential_slot_id`
- `credential_grant_id`
- `credential_version_id`

snapshot 不保存 credential name、secret、envelope、ciphertext、nonce、key ID、storage locator 或 secret hash。历史 snapshot 在资产归档后仍可读取；suspend、revoke 或 membership 失效会阻止新执行。

### 7.5 File 与 artifact

新增 `files`：

- `id UUID`
- private scope 与 `thread_id`
- `kind upload|workspace|output`
- normalized POSIX logical path
- media type、size、whole-file SHA-256
- `status staging|ready|deleted`
- `version`
- `created_by_run_id` 可空
- created/updated timestamp

同一 Thread 的 active logical path 必须唯一。路径拒绝绝对路径、`..`、NUL、Windows drive、symlink 和越界 alias。

新增 `file_chunks`：

- `file_id`
- `chunk_index`
- `content BYTEA`
- `size`
- `sha256`

主键为 `(file_id, chunk_index)`。chunk 默认目标大小由实现常量固定，上传和下载不能依赖一次性完整内容。

新增 `artifacts`：

- `id UUID`
- 完整 project、owner、thread、run scope
- `file_id`
- public display metadata
- `created_at`

artifact 同时通过复合约束证明 run、Thread 和 file 属于相同 project 与 owner。

### 7.6 Memory

新增 `user_project_memories`：

- `id UUID`
- `project_id + owner_user_id`
- `namespace`
- context summary JSON
- `version`
- timestamps

`UNIQUE(project_id, owner_user_id, namespace)`。默认 namespace 表示项目级个人 Memory；legacy per-agent Memory 使用明确 namespace 迁移，不与默认 Memory 合并猜测。

新增 `user_project_memory_facts`，保存 fact ID、content、category、confidence、source Thread/run 和 timestamps，并通过完整 scope 关联 Memory。

向量能力保持可选。部署启用 pgvector 时，向量行必须同时带 project、owner 和 memory fact scope；未启用时不创建伪向量 fallback，也不影响结构化 Memory。

### 7.7 Channel connection

`channel_connections`、`channel_oauth_states` 和 `channel_conversations` 增加 `project_id`。owner 列统一为 `owner_user_id`。

connection owner unique 加入 project：

```text
(project_id, owner_user_id, provider, external_account_id, workspace_id)
```

外部 identity 的 partial unique 只允许一个 `status='connected'` 路由目标。冻结 connection 不接收入站工作，也不阻止同一 identity 重新绑定到另一个项目。重新加入原项目时，只有 identity 未被其他 connected connection 占用才自动恢复；否则要求用户重新连接。

`channel_conversations` 通过完整 scope 同时关联 connection 和 Thread，禁止 connection 把入站消息路由到其他项目或其他 owner 的 Thread。

### 7.8 Checkpoint 例外边界

LangGraph 的 `checkpoints`、`checkpoint_blobs`、`checkpoint_writes` 和 migration 表继续由 LangGraph provider 管理。M4 不为这些上游表添加 Alembic 列或依赖其内部 schema。

补偿控制：

- Thread ID 全局唯一并由 scoped Thread authority 创建；
- `ProjectScopedCheckpointer` 的每个 get/list/put/delete 先验证 Thread scope；
- 新 checkpoint metadata 写入服务端 project/owner scope marker；
- 读取时 marker 与当前 Thread 行必须完全一致；
- legacy checkpoint 在迁移时覆盖写入可信 marker；
- 项目 router、run service、goal、compact、branch 和 state update 不能取得裸 saver；
- Gateway 内部 graph run 只接收服务端重建的 scope，client configurable 同名字段被覆盖或丢弃。

裸数据库连接、LangGraph setup 和 migration 属于 trusted operations。普通应用 route 不存在 unscoped checkpoint 入口。

## 8. Thread 生命周期

### 8.1 创建

创建 Thread 的事务顺序：

1. 解析并锁定 active project 与 membership。
2. 要求 `private_work.create`。
3. 验证逻辑 Agent 对当前项目可见且可执行。
4. 创建 scoped Thread row。
5. 通过 scoped checkpointer 创建 root checkpoint。
6. root checkpoint 失败时回滚或补偿删除 Thread，不能留下可枚举的半成品。

客户端可以提供 thread UUID 以支持 SDK idempotency，但服务端必须检查全局冲突；其他 scope 已使用同一 ID 时返回 404 或稳定 conflict，不回显 owner。

### 8.2 读取和 mutation

search、get、state、history、patch、goal、compact、branch、delete 和 export 都从 scoped Thread repository 开始。禁止先按 `thread_id` 全局读取再做 Python owner 判断。

branch 只复制同 scope 文件。历史 turn branch 继续遵守现有 workspace clone 规则，但复制源改为 PostgreSQL authority rows，不从不可信宿主目录猜测。

### 8.3 删除

owner 删除 Thread 时，在同一应用事务边界标记 Thread、file 和 artifact deleted，并删除或排队删除 checkpoint。M4 不建设通用异步 job，因此 checkpoint 删除失败时 Thread 保持不可见并记录安全重试状态；不能因为 saver 暂时不可用重新暴露 Thread。

## 9. Run admission 与执行

### 9.1 Admission

run admission 顺序：

1. 解析新的 `PrivateWorkContext`。
2. 锁定 scoped Thread 并检查没有不允许并发的 active run。
3. 要求 `private_work.create + shared_assets.execute`。
4. 调用 M3 `ProjectAssetResolver` 解析 Agent 完整 closure。
5. 在 run transaction 写 `runs`、`run_asset_versions` 和 `run_mcp_grant_snapshots`。
6. 在启动 graph 前再次比较 catalog generation。
7. 通过 M3 materializer 重验 credential closure 并产生短生命 secret。
8. 创建现有 RunManager record 并进入现有 worker。

catalog、binding、grant 或 credential 在 snapshot 后变化时，不允许用 stale snapshot materialize。尚未创建 run 时返回 409；run 已创建但启动前失效时安全标记 error，不执行模型或工具。

### 9.2 Runtime context

Gateway 把真实 opaque project context 放入内部 runtime context。以下 client 字段全部不可信：

- `context.project_id`
- `context.owner_user_id`
- `context.role`
- `context.capabilities`
- `configurable.project_context`
- 任意 `__private_*` 字段

harness 保持 app import firewall。harness 可以定义安全 protocol 或接收 opaque object，但不能 import `app.projects`。M3 resolver/materializer 的 app adapter 仍由 Gateway 安装。

### 9.3 Asset runtime

项目 Agent 从 run snapshot 构建 runtime configuration：

- Agent prompt 和配置使用 exact Agent version；
- Skill 只物化 snapshot 中 exact Skill version；
- MCP definition 使用 exact MCP version；
- credential grant 必须与 snapshot 和当前 generation 一致；
- project Skill materialization 使用 run-scoped temporary directory；
- project MCP config 和 secret 不进入全局 extensions config 或跨项目 cache。

legacy 对话只能使用 M3 允许的系统资产适配。项目资产不能在 legacy 无项目上下文路径执行。

### 9.4 权限撤销

membership service、project lifecycle service 和 private run service 共享一个取消接口。撤销执行能力时：

- transaction 内为相关 pending/running run 写 `authorization_cancel_requested_at` 和公共 reason；
- commit 后 best-effort 通知本进程 RunManager；
- 模型、工具、sandbox 副作用、MCP 调用和 file finalization 前检查 DB marker；
- 运行终态为 interrupted，并使用安全 `authorization_revoked` reason；
- 已完成且已提交的副作用不回滚猜测，后续副作用不再执行。

Admin 降为 Editor 或 Runner 时仍保留 execute capability，活动 run 可以继续。降为 Viewer、membership left/removed、project suspended 或 pending deletion 时终止。

## 10. 文件与沙箱

### 10.1 上传

HTTP upload 先创建 staging file row，再按固定 chunk 大小流式写入。每个 chunk 在写入前计算 SHA-256，结束时计算 whole-file SHA-256、总大小和文件数限制。全部验证成功后，在一个 transaction 中把 file 置为 ready。

请求取消、转换失败、大小超限或 DB 错误必须删除 staging rows。文档转换产物作为独立 file row 保存，并记录与原文件的安全关联，不覆盖原文件。

### 10.2 沙箱恢复

沙箱 acquisition 输入完整 private scope。物理临时路径至少包含：

```text
projects/{project_id}/users/{owner_user_id}/threads/{thread_id}
```

provider 只恢复当前 run 获权的 ready files。每个 file 按 chunk streaming 写入临时目录，写完复核 whole-file hash，再允许工具访问。hash 不一致时删除临时副本并使 run fail closed。

### 10.3 写回与 finalization

运行前记录 authority manifest。运行结束、取消或 interruption finalization 时扫描 workspace/output：

- 拒绝 symlink、越界 path、敏感保留目录和超限内容；
- 按文件流式 chunk；
- 与 pre-run manifest 比较；
- 为合法新版本写 authority rows；
- 为 presented output 创建 artifact；
- 所有需要交付的文件提交后，run 才能从 finalizing 进入 success 或 interrupted。

文件持久化失败时 run 进入 error，不允许 response 宣称 artifact 已保存。临时目录在 finalization 后清理；清理失败只记录脱敏运维错误，不改变已提交 authority。

### 10.4 下载与 branch

download 先验证 scoped file/artifact，再按 chunk stream response。不能一次把完整 100 MiB 文件读入内存。

branch 通过数据库 `INSERT ... SELECT` 或等价 streaming 复制 authority，不依赖当前沙箱目录。复制后的 file row 属于新 Thread，并重新建立完整 project/owner/thread constraint。

## 11. Memory

现有 MemoryMiddleware 保留 LLM 提取和 debounce 体验，但 queue item 在入队时捕获：

- project ID
- owner user ID
- Thread ID
- run ID
- namespace
- membership version

Timer/worker 执行时不能从 ContextVar 重新猜测 scope。写入前重验 project、membership 和 owner。只允许同 scope 的 user/AI visible messages 进入提取，hidden system context、credential、tool secret 和其他用户消息不能进入。

prompt injection 只读取当前 project、owner 和 namespace，继续执行 token budget。Memory API 移到项目路径。legacy global Memory API 在 cutover 后返回稳定 conflict，不把多个项目 Memory 合并。

成员退出或被移除后停止提取与注入。重新加入后使用原 membership row 恢复相同 project Memory。M4 不执行 retention 到期物理清理。

## 12. IM connection

连接流程从项目页面开始。OAuth/connect state 保存 project、owner、provider、过期时间和安全 redirect metadata。provider 回调或 chat connect code 完成时重新验证 membership 与 create capability。

入站解析顺序：

1. provider identity 查找唯一 connected connection；
2. 读取 connection 的 project 与 owner；
3. 重新解析 active project membership；
4. 检查 execute capability；
5. 查找或创建同 scope channel conversation 与 Thread；
6. 通过项目 private-work run path 启动运行。

connection 不能直接把 owner header 当作完整授权。内部 owner/project 信息只在 connection 已通过数据库 scope constraint 后构建。

成员退出时 connection 变为 frozen，credential 保留在 retention window 内但不解密、不接收入站运行。重新加入时若外部 identity 未被其他 connected row占用则恢复；否则保持 frozen 并要求用户重新连接。

## 13. API

### 13.1 LangGraph 兼容项目 API

项目私有 SDK base URL：

```text
/api/projects/{project_id}/private-work
```

其下提供现有 Frontend 所需：

- Thread create/search/get/update/delete
- state/history/state update
- goal/compact/branch
- run create/stream/wait/list/get/join/cancel
- message/event/token usage/feedback
- upload/list/delete
- artifact get/download

project path 只接受 UUID。router 解析 context、转换 strict schema、调用 private-work service 和映射稳定错误；业务逻辑不复制到 router。

### 13.2 Memory 与 connection API

```text
/api/projects/{project_id}/memory
/api/projects/{project_id}/connections
```

Memory list/status/reload/import/export 和 connection provider/list/connect/disconnect 都要求 project context。secret-bearing connection input 不进入 TanStack Query 或 Mutation cache。

### 13.3 Legacy API

以下路径保留到 M7：

- `/api/threads`
- `/api/runs`
- `/api/memory`
- legacy global connection API

private-work cutover 前它们只服务 legacy records；marker 后统一返回 `409 PRIVATE_WORK_CUTOVER`，不得通过 default project 猜测或读取 project-scoped rows。

受信内部 IM 使用 connection-resolved project path。Embedded client 在 cutover 后必须被调用方注入真实 private scope；缺少 scope 或 client-shaped dict 在 tool loading/checkpoint access 前失败。

## 14. Frontend

### 14.1 路由

新增：

- `/projects/[project_slug]/chats`
- `/projects/[project_slug]/chats/[thread_id]`
- `/projects/[project_slug]/memory`
- `/projects/[project_slug]/connections`

项目 layout 继续是 slug resolution 和 enter mutation 的唯一 owner。nested pages 只消费 `useCurrentProject()`，不重复查询项目列表或根据 slug 调 UUID API。

### 14.2 Project LangGraph client

新增 project-scoped client factory，cache key 至少包含 `account_id + project_id`。不能复用当前 module-level default client 处理项目请求。

所有 private-work TanStack keys 都以 account、project 开头。账户切换、logout、项目切换和 provider unmount 必须先取消 in-flight query，再清理对应 Query 与 scoped client，late response 不能进入新 account/project cache。

### 14.3 页面行为

项目首页显示当前 owner 最近 Thread 和开始私有对话 CTA：

- 有 create/execute capability 且有可执行 Agent：打开 Agent selector 并创建 Thread；
- 有能力但无可执行 Agent：跳转项目 Agents；
- Viewer：显示只读解释，不发送 create request；
- cutover 未完成：入口 disabled，不离开页面。

项目 chats 列表和 chat 页复用现有 workspace 组件，但数据源全部切到 project client。Memory 与 connections 进入项目侧栏。files 继续在 chat sidecar 展示，不建设独立文件管理页。

static demo 不暴露项目私有入口。直接访问错误项目、其他 owner Thread 或已冻结数据统一展示 404。

### 14.4 Viewer 与删除

Viewer 可以打开自己的既有 Thread、下载 artifact、导出 Memory 和删除自己的 private resource。Viewer 不能：

- 创建或 branch Thread；
- 启动、regenerate 或继续 run；
- 上传文件；
- 新建或重新绑定 connection；
- 修改 Memory。

删除动作仍使用服务端返回 capability，不按 role 字符串推导。

## 15. Staged migration 与 cutover

### 15.1 Schema revisions

M4 使用两个 Alembic revision：

- `0008_project_private_work_expand.py`：新增 nullable 回填列、新表、索引、ledger 和 marker table；项目私有入口仍关闭。
- `0009_project_private_work_finalize.py`：验证回填完成，rename owner 列，增加 NOT NULL、CHECK、unique 和复合外键。

`0009` 在 legacy rows 未完成 scope 回填、ledger/probe 不完整或 finalize prerequisite record 不满足时必须在任何 DDL 前失败。cutover marker 只能在 `0009` 成功后写入，不能作为 `0009` 的前置条件。

空数据库由 `Base.metadata.create_all` 创建最终 schema，初始化流程在确认无 legacy source 后写 empty-install cutover marker。已有数据库必须使用显式 staged command，不能让普通 Gateway startup 猜测 owner map。

### 15.2 命令

新增：

```bash
make migrate-private-work ARGS="--dry-run --owner-map /secure/private-work-owner-map.json --backup-dir /secure/backups"
make migrate-private-work ARGS="--execute --owner-map /secure/private-work-owner-map.json --backup-dir /secure/backups"
```

owner map 为每个 legacy owner 指定一个 active project UUID。脚本禁止按 email、role、最近访问、唯一 project 或 default slug 静默推断。owner 必须是目标 project 的有效 membership；缺失或歧义立即失败。

dry-run 是只读操作，可以在服务运行时执行，但其 source fingerprint 只用于预检。execute 必须在 maintenance window 内运行：Gateway、Scheduler、channel workers 和所有 embedded/TUI writer 均已停止；脚本在 expand 前、数据回填前和 finalize 前重复检查数据库与 filesystem fingerprint。检测到任何并发写入立即回滚当前 domain 并失败，不尝试追增量。

### 15.3 Inventory

inventory 覆盖：

- `threads_meta`
- `runs`
- `run_events`
- `feedback`
- LangGraph checkpoint/blobs/writes
- thread uploads/workspace/outputs 目录
- global 与 per-agent Memory files
- channel connection、credential、OAuth state 和 conversation

清点输出只包含 counts、source fingerprint、stable key hash、大小、scope target 和公共 conflict code；不包含提示词、消息、Memory、文件名、文件内容、credential 或连接明文。

### 15.4 Backup

execute 前对 filesystem private source 建立认证加密 backup。backup key 不存数据库和仓库。目录权限 0700、文件权限 0600。ledger 保存 source fingerprint、size、SHA-256、加密 backup locator 的安全相对值和 decision digest，不保存明文或 encryption material。

数据库备份要求使用 operator 提供的受控备份路径；M4 脚本只验证备份证明，不建设 M6 通用备份产品。

### 15.5 执行

执行顺序：

1. upgrade 到 expand revision；
2. 锁定 migration run；
3. 再次验证 source fingerprint 和 owner map；
4. 按 Thread scope 回填 run/event/feedback；
5. 为 checkpoint metadata 写 server scope marker，不改 checkpoint/blob payload；
6. 按 thread path 流式导入 files/chunks/artifacts；
7. 导入 Memory namespace 和 facts；
8. 回填 connection/OAuth/conversation scope；
9. 对每个 domain 写同事务 ledger；
10. 比较 counts、FK graph、hash、Memory semantic digest 和 connection routing；
11. 执行跨项目、跨 owner probe；
12. upgrade 到 finalize revision；
13. 写 private-work cutover marker。

每个 domain 可幂等重跑。相同 fingerprint 和相同 target digest 为 no-op；不同 source 或 target 内容冲突 fail closed，禁止覆盖。

legacy source 在 M4 保持原样只读。cutover 后 runtime 不双写；M7 再删除 legacy source 与 API。

## 16. Error、并发与隐私

### 16.1 稳定错误

| 场景 | HTTP |
| --- | --- |
| 跨项目、跨 owner、不存在、失效 membership | 404 |
| 当前项目内 capability 不足 | 403 |
| Thread busy、asset generation stale、cutover 未完成、migration conflict | 409 |
| 文件超过部署大小上限 | 413 |
| UUID、路径、payload 或请求 schema 非法 | 422 |
| PostgreSQL、checkpoint、file authority 或 materializer unavailable | 503 |

响应只含公共 code、message 和 request ID。不得包含 SQL、URL、提示词、消息、Memory、文件内容、credential、envelope 或原始 provider error。

### 16.2 锁序

run admission：

```text
project -> membership -> thread -> active run -> asset resolver closure -> run snapshot
```

file finalization：

```text
thread -> run -> logical file paths sorted -> file rows -> chunks -> artifacts
```

connection inbound：

```text
external identity -> connection -> project -> membership -> conversation -> thread
```

同一批多文件和多 credential 继续使用稳定 UUID/path 排序，避免交错锁。M3 credential closure 的锁序保持不变。

### 16.3 隐私与日志

治理审计和普通日志不能记录：

- prompt 或消息正文；
- Memory 正文；
- 文件名、路径或内容；
- run tool output；
- credential metadata 或 secret；
- private resource ID 与其他用户身份的组合。

允许记录 request ID、public error code、project ID、owner 自身在内部安全日志中的 stable hash、run/thread stable hash 和 counts。M6 持久化 audit 仍必须遵守相同内容禁令。

## 17. 测试与发布门禁

### 17.1 真实 PostgreSQL

新增 `backend/tests/integration/test_m4_private_work_postgres.py`，至少使用：

- Project A 的 owner A；
- Project A 的另一成员 B；
- Project B 的 owner A 或另一用户；
- outsider；
- Viewer、Runner、Editor、Admin；
- system Agent binding 与 project Agent；
- 一个含 credential grant 的 MCP。

覆盖：

- Thread/run/event/feedback/file/artifact/Memory/connection scope；
- 读取、搜索、分页、更新、删除、导出和猜测 UUID；
- 同项目跨 owner 404；
- 跨项目 404；
- Viewer read-own 与 create/execute deny；
- 复合 FK 拒绝错误父子关联；
- checkpoint marker 伪造、缺失和 cross-scope；
- exact asset snapshot 与 generation stale；
- credential secret 零持久化；
- membership remove/downgrade 与 project suspend 的活动 run 终止；
- connection 入站只能进入绑定 project；
- frozen/rejoin 行为；
- file chunk、whole hash、tamper 和 finalization。

测试只使用随机 `deerflow_test_*` 数据库。缺 `POSTGRES_TEST_URL` 时本地明确 skip，CI 在 pytest 前硬失败。

### 17.2 Backend 单测

- PrivateWorkContext 构造与 client field stripping；
- scoped repository SQL 条件；
- stable error mapping；
- run admission 和 snapshot；
- authorization cancellation marker；
- scoped checkpointer adapter；
- upload streaming、path、chunk/hash 和 cleanup；
- sandbox restore/finalization；
- Memory queue scope capture；
- connection routing 与 freeze；
- migration inventory、owner map、backup proof、ledger、idempotency 和 tamper；
- harness import firewall；
- secret/private content log scan。

Backend 继续 mandatory TDD，先写 failing test，再写最小实现。

### 17.3 Frontend

- project client base URL 和 account/project cache key；
- logout、account switch、project switch 的 cancel-before-clear；
- Project routes 不重复 slug resolution；
- recent private work 只显示当前 owner；
- Agent selector、无 Agent 引导和 Viewer 门禁；
- chat streaming、stop、goal、compact、branch、human-input；
- upload、artifact 和 Memory project paths；
- connection project scope；
- 直接 URL 的跨项目/跨 owner 404；
- static demo 无 project private work。

运行 `pnpm check`、unit 和 Playwright。

### 17.4 Migration

- expand revision from M3 head；
- dry-run 零 target write；
- explicit owner map fail-closed；
- encrypted backup proof；
- execute 全域回填；
- 中断后幂等重跑；
- source fingerprint 变化拒绝；
- target tamper 拒绝；
- finalize revision prerequisite；
- cutover marker 前后 API/runtime 行为；
- legacy source 字节不变。

### 17.5 CI

现有 `.github/workflows/project-foundation-postgres-tests.yml` 固定运行 M1 cutover、M1 isolation、
M2 governance、M3 shared assets、M4 private work 和 M4 private-work migration 六个真实 PostgreSQL
integration 文件；CI 缺少 `POSTGRES_TEST_URL` 时在 pytest 前硬失败。

任何隔离、checkpoint、secret、file authority、migration 或 Frontend cache 测试失败时，不得开放 M4。

## 18. 文档与运维

实现 M4 时同步更新：

- 根 `README.md`：项目私有对话、文件、Memory 和 connection 的用户行为；
- 根 `AGENTS.md`：M4 状态、PostgreSQL release gate 和 staged migration；
- `backend/AGENTS.md`：PrivateWorkContext、repository、checkpoint、file、Memory、connection 和 migration；
- `frontend/AGENTS.md`：project chat routes、scoped client 和 cache ownership；
- 总体 SaaS 设计：完成度与 M5 边界；
- 运维文档：maintenance window、owner map、backup proof、dry-run、execute 和 rollback decision。

## 19. 完成标准

满足以下条件后才把 M4 标记为已完成：

- 所有私有根资源都有非空 project 与 owner scope；
- 所有业务访问通过可信 context 和 scoped repository；
- checkpoint 项目访问 fail closed；
- 项目 Thread/run 复用现有 LangGraph runtime；
- 每个 run 持久化 exact M3 asset snapshot；
- credential 明文在 M4 持久化面零泄漏；
- PostgreSQL 成为 file/artifact authority；
- Memory 和 connection 完成 project-owner 隔离；
- membership revoke 能阻止新运行并终止失权活动运行；
- staged migration、backup proof、ledger、finalize constraint 和 cutover marker 全部完成；
- Project chats、Memory、connections 和 cache isolation 完成；
- legacy private API 在 cutover 后不能读取 project data；
- M1 至 M4 PostgreSQL gate、Backend、Frontend 和安全测试全部通过；
- README、AGENTS、总体设计和运维说明同步；
- 独立审查无 Critical 或 Important finding。

完成后总体进度更新为 M1、M2、M3、M4 已完成（4/8，50%），同时继续声明：automation、Worker/SSE、quota/audit/backup、最终 legacy cleanup 和完整发布验收尚未完成，系统仍不可作为完整多用户 SaaS 发布。
