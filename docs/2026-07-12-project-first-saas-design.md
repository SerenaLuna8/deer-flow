# 项目优先的多用户 SaaS 总体设计

- 日期：2026-07-12
- 状态：已完成
- 当前完成度：M1–M8 已正式完成，共 8/8（100%）
- 代码仓库：ActWeave 单体仓库
- 数据库：PostgreSQL
- 权限边界：业务层、仓储层和数据库模型约束

## 1. 文档目的

本文档是 ActWeave 项目优先、多用户 SaaS V1 的唯一设计基线。当前实现事实以代码、PostgreSQL
`full_schema_v1` 完整快照和自动化发布门禁为准。

M1–M8 已正式完成。M7 已收敛为 PostgreSQL schema 基线、project/admin API、project-scoped
frontend、独立 Worker/Scheduler、durable SSE/quota/audit 和运行时只读 schema 边界；M8 进一步完成完整
隔离矩阵、安全与容量门禁以及真实宿主机/Chromium/DeepSeek 验收。关闭前完整分支审查结论为
0 Critical / 0 Important / 0 Minor。项目优先、多用户 SaaS V1 已在 M8 限定的宿主机范围内通过发布验收。

## 2. 已冻结决策

V1 采用以下不可变更基线；如需改变，必须先修订本文档并重新评审：

1. 项目是最高级别的租户、协作、配额和安全边界，V1 不设置组织层。
2. 用户可以创建和加入多个项目；所有终端用户工作都发生在项目内。
3. 项目只共享 Agent、Skill 和 MCP 定义及其不可变版本。
4. 对话、消息、运行、检查点、文件、产物、记忆和自动化按项目分区，并且只对所有者可见。
5. 项目角色固定为 Admin、Editor、Runner 和 Viewer，不提供自定义角色。
6. 平台角色固定为 `system_admin` 和 `user`，平台角色与项目角色相互独立。
7. `system_admin` 和项目 Admin 不能通过普通产品功能读取其他用户的私有内容。
8. PostgreSQL 是唯一持久化数据库，完全移除 SQLite 和持久化内存后端。
9. PostgreSQL 不启用 RLS。授权由业务服务、强制作用域仓储和数据库复合约束共同实施。
10. 本地 PostgreSQL 运行在 Docker 容器中，并通过 `127.0.0.1:5432` 暴露给宿主机进程。
11. 数据库名称为 `deerflow`。数据库创建、迁移和初始化由仓库脚本完成，不要求用户手工执行数据库命令。
12. 数据库密码只从环境变量读取，不写入代码、YAML、日志、测试快照或版本控制。
13. V1 不依赖 Redis、Kafka、对象存储或独立向量数据库。
14. 上传和产物的权威副本存入 PostgreSQL；沙箱和本地目录只保存运行期间的临时副本。
15. V1 不接入邮件发送服务。邀请通过一次性链接传递。
16. 项目删除撤销窗口和成员退出后的私有数据冻结期均为 30 天。
17. 保留现有 ActWeave 对话、流式响应、输入区、文件侧栏、运行控制和长任务体验。
18. Agent、Skill、MCP 分为系统级和项目级；系统资产仍由平台保存并通过项目绑定固定具体版本。项目 Agent 页面只展示项目自建 Agent，不展示系统 Agent；新会话固定使用当前项目已启用的系统 Main Agent。
19. 系统 Agent、Skill 和 MCP 只由 packaged、版本化且 digest 校验的 bootstrap catalog 写入 PostgreSQL；运行时不扫描仓库或用户目录。
20. 系统 MCP 使用系统 credential，启用该 MCP 的项目共享该 credential；项目 MCP 只能使用同项目 credential。
21. `system_admin` 可以只读治理系统 Agent、Skill、MCP 的 catalog 元数据，管理系统 credential/grant，并 override 任意项目的共享资产及 credential/grant；系统 Agent、Skill、MCP 定义和版本只能通过 packaged bootstrap catalog 写入，不能在线创建、修改、发布、归档或停用。该平台 override 不授予成员管理或用户私有内容访问权。
22. 数据库级数据保护和生命周期管理由部署基础设施负责；ActWeave 只负责应用 schema 初始化与运行时读写。
23. 系统通知是账号级持久化资源。工作区顶部使用铃铛和未读角标展示；向已注册邮箱发出的项目邀请生成可操作站内通知，未注册邮箱不预建站内通知，仍通过一次性邀请链接完成兑换。

## 3. 产品目标

### 3.1 目标

- 登录后的默认入口是展示多个项目的工作空间。
- 没有有效项目成员关系的用户不能进入 Agent 工作区。
- 同一项目的成员可以使用相同的 Agent、Skill 和 MCP 定义。
- 任何成员都不能枚举或读取其他成员的私有工作。
- 资源标识符不能单独构成授权依据。
- 成员移除或降级后，立即拒绝不再获准的新工作，并终止已经失去授权的活动工作。
- 服务重启后，已排队任务可继续执行，运行状态和持久化事件仍可读取。

### 3.2 非目标

- 组织、团队目录和跨项目继承。
- 单资源 ACL、自定义角色和资源级分享。
- 共享对话、共享文件、共享运行历史或共享知识库。
- 计费、订阅、发票和按量付费。
- 匿名公开分享。
- 微服务拆分和外部消息总线。
- 跨项目安装 Agent、Skill 或 MCP。
- V1 性能压测指标；但有限并发下必须保持事务正确性。

## 4. 产品模型

### 4.1 项目

- 项目使用不可变 UUID 作为授权和关联键。
- slug 全局唯一、不区分大小写，创建后不可修改。
- 显示名称、图标和描述可以修改。
- 创建项目与创建者 Admin 成员关系必须处于同一事务。
- 有效项目始终至少存在一名有效 Admin。
- 项目生命周期为 `active -> pending_deletion -> active`。
- 平台暂停是独立状态，不改写项目生命周期。

### 4.2 工作空间

登录后进入 `/workspace`。工作空间是跨项目入口，只负责项目发现、创建和进入，不承载任何项目级业务菜单。页面包含：

- 创建项目入口；
- 搜索、筛选和置顶；
- 项目名称、图标、描述和当前角色；
- 成员数量及 Agent、Skill、MCP 数量；
- 最近进入时间、项目状态和配额摘要；
- 待删除项目的撤销入口；
- 无项目时的创建和接受邀请引导。
- 顶部账号级通知铃铛、持久化未读角标和通知面板。

工作空间使用简洁的顶部导航，不显示 Agent、Skill、MCP、对话、Memory、自动化或项目设置等左侧菜单。`/workspace/projects` 作为兼容地址重定向到 `/workspace`。

### 4.3 项目壳层

用户进入 `/projects/{project_slug}` 后才加载项目壳层和左侧菜单。项目壳层必须绑定服务端解析的 `ProjectContext`，菜单项根据服务端返回的能力显示或禁用，不能由前端根据角色自行推导授权。

项目壳层提供项目概览、成员与邀请、项目设置、Agent、Skill、MCP、私有工作、自动化和返回工作空间入口。菜单按服务端 capability 显示；项目内不提供跨项目下拉切换器，用户通过返回工作空间切换项目。

### 4.4 项目主页

项目主页位于 `/projects/{project_slug}`，用于定位和继续工作，不做通用分析仪表板。页面显示：

- 项目标识、描述和当前用户角色；
- 创建私有对话的主要操作；
- 私有工作与共享资产边界提示；
- 当前用户最近的私有工作；
- Agent、Skill 和 MCP 共享资产入口；
- 返回工作空间的明确操作。

### 4.5 角色与能力

| 能力 | Admin | Editor | Runner | Viewer |
| --- | --- | --- | --- | --- |
| 查看共享 Agent、Skill、MCP | 是 | 是 | 是 | 是 |
| 执行共享资产 | 是 | 是 | 是 | 否 |
| 创建自己的私有工作 | 是 | 是 | 是 | 否 |
| 管理自己的自动化 | 是 | 是 | 是 | 否 |
| 编辑共享 Agent 和 Skill | 是 | 是 | 否 | 否 |
| 起草和编辑共享 MCP | 是 | 是 | 否 | 否 |
| 批准携带凭据的 MCP 版本 | 是 | 否 | 否 | 否 |
| 管理成员、邀请和角色 | 是 | 否 | 否 | 否 |
| 管理项目凭据 | 是 | 否 | 否 | 否 |
| 查看审计和用量 | 是 | 否 | 否 | 否 |
| 管理项目生命周期 | 是 | 否 | 否 | 否 |

Viewer 可以查看、导出和删除自己已有的私有数据，但不能创建新工作、启动运行或修改自动化。

## 5. 共享与隐私边界

| 数据类型 | 作用域 | 可读取者 |
| --- | --- | --- |
| Agent 定义和版本 | 项目共享 | 有效项目成员 |
| Skill 定义、内容和版本 | 项目共享 | 有效项目成员 |
| MCP 定义和已批准版本 | 项目共享 | 有效项目成员 |
| 系统 Agent、Skill、MCP 定义和版本 | 系统共享 | 所有有效项目成员；只有 `system_admin` 可修改 |
| 项目 Agent、Skill、MCP 定义和版本 | 项目共享 | 当前项目有效成员；依据 capability 修改 |
| 系统 credential 明文 | 平台治理 | 永不通过 API 返回；只在系统 MCP 执行前短暂解密 |
| 项目凭据明文 | 项目治理 | 永不通过 API 返回 |
| 凭据元数据和授权 | 项目治理 | Admin；其他角色只接收必要状态 |
| 对话和消息 | 项目 + 用户私有 | 所有者 |
| 上传和产物 | 项目 + 用户私有 | 所有者 |
| 运行、事件、日志和检查点 | 项目 + 用户私有 | 所有者 |
| 自动化和运行结果 | 项目 + 用户私有 | 所有者 |
| 个人记忆和向量 | 项目 + 用户私有 | 所有者 |
| 系统通知及已读状态 | 账号私有 | 当前认证账号 |
| 成员关系和项目设置 | 项目治理 | 依据能力 |
| 审计 | 项目治理元数据 | Admin，但不含私有内容 |

跨项目访问和访问其他成员的私有资源统一返回 `404`。已确认资源处于当前项目且调用者缺少治理能力时返回 `403`。

通知列表、未读计数、标记已读和通知内操作必须从服务端认证身份解析账号，仓储查询和更新始终包含接收者账号作用域。客户端不得提交可信接收者 ID，也不得通过通知 ID 枚举其他账号的通知。

审计不得包含提示词、消息正文、记忆正文、文件名、附件内容、运行日志或产物内容。

## 6. 总体架构

```text
Next.js Web
    |
FastAPI Gateway
    |-- API process
    |-- Worker process
    |-- Scheduler process
    |-- Agent and sandbox runtime
    |
PostgreSQL + optional pgvector
```

系统继续采用模块化单体。API、Worker 和 Scheduler 是独立进程，但复用同一业务模块和 PostgreSQL。

### 6.1 请求授权链路

```text
authenticated user
  -> resolve immutable project_id
  -> load active membership
  -> calculate capabilities
  -> create ProjectContext
  -> authorize in domain service
  -> query through scoped repository
  -> enforce composite constraints
  -> append audit metadata
```

`ProjectContext` 包含：

- `user_id`
- `project_id`
- `membership_id`
- `role`
- `capabilities`
- `membership_version`
- `request_id`

客户端不能提供可信角色、能力或所有者标识。

### 6.2 仓储隔离规则

- 项目共享资源的所有查询和写入必须包含 `project_id`。
- 私有资源必须同时包含 `project_id` 和 `owner_user_id`。
- `owner_user_id` 必须来自 `ProjectContext.user_id`，不能来自请求正文。
- 更新和删除使用同样的完整作用域条件。
- 普通业务代码不能获得未限定作用域的查询接口。
- 全局迁移和运维接口必须位于独立模块，并使用显式的 `system` 或 `unscoped` 命名。
- 禁止先按资源 ID 查询，再在应用内判断所有权。

### 6.3 数据库模型约束

- 所有项目业务表的 `project_id` 非空。
- 所有私有表的 `owner_user_id` 非空。
- `project_memberships(project_id, user_id)` 唯一。
- 私有父子关系使用包含项目和所有者的复合外键。
- 共享资产版本必须与逻辑资产属于同一项目。
- 任务、运行、事件、文件和产物的关联必须保持项目和所有者一致。
- slug、版本号、邀请 token hash 和幂等键使用唯一约束。
- 最后一名 Admin、邀请兑换和成员版本通过事务、行锁和条件更新维护。

不使用 RLS 意味着数据库直连、迁移脚本和 DBA 属于可信运维范围。应用数据库账号不得拥有超级用户权限。

## 7. PostgreSQL-only 基线

### 7.1 配置

所有持久化组件使用同一个 `DATABASE_URL`：

- 应用 ORM；
- LangGraph checkpointer；
- 运行事件；
- Scheduler 和 Worker；
- 完整 schema 初始化器；
- 文件和向量数据。

宿主机运行 ActWeave 时连接 `127.0.0.1:5432`。容器内运行时使用 Docker Compose 服务名，不能使用容器自身的 `127.0.0.1`。

应用启动时如果 PostgreSQL 不可用、目标数据库不存在或 schema 版本不兼容，应立即失败，不允许降级。

### 7.2 初始化脚本

仓库提供：

- `make setup-db`：检查 PostgreSQL、创建空的 `deerflow` 目标库、原子执行 `full_schema.sql`，再初始化 catalog、LangGraph schema 和默认项目；
- `make check-db`：只读检查连接、`full_schema_v1` marker、扩展和必需对象。

脚本必须幂等、脱敏错误，并且不提供删除数据库或清空数据功能。Gateway、Worker 和 Scheduler
启动不得执行 DDL、schema 安装或自动修复。

### 7.3 数据库账号

- `deerflow_app`：API、Worker 和 Scheduler 的日常读写；
- PostgreSQL 管理员：仅供显式 `setup-db` 创建空目标库，不参与应用运行；
- PostgreSQL 超级用户不用于应用运行。

本地开发初期允许通过现有 `postgres` 用户执行初始化，但应用部署规格仍以最小权限账号为目标。

## 8. 持久化模型

### 8.1 平台和项目

- `users`
- `auth_sessions`
- `user_notifications`
- `projects`
- `project_memberships`
- `project_invites`
- `invite_redemptions`
- `deletion_requests`

### 8.2 共享资产

- `project_agents`、`agent_versions`
- `project_skills`、`skill_versions`
- `project_mcp_servers`、`mcp_server_versions`
- `project_credentials`、`credential_grants`

已发布版本不可变。编辑生成新版本，发布通过事务移动有效版本指针。逻辑删除不会破坏历史运行引用。

### 8.3 私有工作

- `threads`、`messages`
- `runs`、`run_events`、LangGraph checkpoint 表
- `files`、`file_chunks`、`artifacts`
- `scheduled_tasks`、`scheduled_task_runs`
- `user_project_memories`
- `user_project_connections`
- 私有向量行

### 8.4 任务和治理

- `jobs`、`job_attempts`、`dead_jobs`
- `project_quotas`、`project_usage_ledger`
- `audit_logs`
- migration 和回填台账

## 9. 文件与沙箱

- 上传和产物的权威数据存储在 PostgreSQL。
- 大文件拆分为有序 `file_chunks`，每块保存哈希；文件表保存整体哈希、大小、类型和状态。
- 沙箱启动时只投影当前运行获授权的文件。
- 沙箱输出经过大小、类型和路径检查后持久化回 PostgreSQL。
- 运行结束后清理临时目录。
- V1 默认单文件上限 100 MiB，单项目存储上限 5 GiB；平台可配置更严格或更宽松的部署上限。
- 不允许数据库响应一次性加载完整大文件；上传、下载和沙箱投影采用流式分块处理。

## 10. 共享资产与凭据

- Agent、Skill、MCP 同时支持 `system` 和 `project` scope；项目 Skill 显示名称在同一项目内大小写不敏感且不可重复，不同项目仍可同名；所有引用使用 UUID 和 scope。
- 系统 Agent、Skill、MCP 由 packaged bootstrap catalog 管理，运行期只向 `system_admin` 提供 catalog 治理元数据；`skills/public/` 中的 14 个完整多文件目录是全部 System Skill 的唯一来源，开发期生成 digest 校验的 archives，并只在全新数据库 setup 时写入 catalog。每个新建项目显式固定并默认启用当时全部 System Skill 的当前已发布版本；catalog 更新不补绑或升级既有项目，也不覆盖管理员的停用选择。项目 Agent 页面隐藏系统 Agent，以卡片展示项目自建 Agent；卡片可进入详情或为可执行的已发布 Agent 创建绑定对话。Agent 不暴露归档 mutation，生命周期在界面统一显示为启用/停用。列表不提供删除入口；详情页经过二次确认和 5 秒等待后可永久删除未被引用的项目 Agent 及全部版本。任何 Thread、Automation 或 Run snapshot 引用都返回冲突，不级联删除私有历史；系统 Agent 不可删除。
- 项目资产由当前项目 Admin、Editor 按 capability 创建和发布；使用 credential 的项目 MCP 必须由项目 Admin 审批。
- 项目 Skill 的 manifest name 必须等于资产 slug；每个 archive 及一次批量导入合计最多 100 MiB、16384 个文件。项目创建支持 multipart `.zip`、`.skill`（ZIP）、`.tar`、`.tar.gz` 和 `.tgz`，安全解析后剥离单一外层目录，并在一个事务中创建默认停用的资产和已发布首版。Gateway 与统一 Nginx 入口仅对 archive 创建路由开放 160 MiB JSON/base64 或 multipart wire body，并在 JSON/Pydantic 或 multipart 路由处理前拒绝越界请求。所有不可变项目 Skill 版本文件均计入项目 `storage_bytes`，Gateway 与显式导入 CLI 共用部署配额配置。项目 Skill 不提供归档；详情页经过二次确认和 5 秒等待后永久删除整个包及全部版本，并在同一事务释放逐版本配额。系统 Skill 或仍被 Agent/Run snapshot 引用的项目 Skill 拒绝删除。
- Agent、Skill 和 MCP 每次执行记录准确版本 ID。
- Run 只按准入 snapshot 中的精确资产版本、checksum、绑定和授权闭包重新校验；全局 catalog generation 只作诊断元数据，其他项目的资产写入不会使已准入 Run 失效。
- 新项目不复制或自动启用系统资产；项目 Admin 通过显式绑定启用系统资产并固定具体 version。
- MCP 凭据授权绑定到不可变 MCP 版本，而不是逻辑服务器 ID。
- Editor 可以起草 MCP 变更；携带凭据的版本必须由 Admin 审批。
- 系统 MCP 绑定系统 credential；项目 MCP 绑定同项目 credential，两个 scope 不得交叉授权。
- `system_admin` 只能通过专用 grant 操作为当前已发布的 packaged 系统 MCP 配置系统 credential；该操作只替换授权，不修改定义、workflow、checksum、发布指针或资产 revision。
- `system_admin` 可以 override 任意项目的共享资产和 credential/grant，但不能借此读取用户私有内容。
- 凭据使用应用主密钥加密，主密钥不存入数据库。
- 主密钥使用带 `key_id` 的环境 keyring，并通过显式 trusted script 轮换。
- API 创建凭据后不返回明文；Admin 只能替换，不能读回。
- 凭据不能进入提示词、检查点、日志、审计、文件或产物。

## 11. 私有运行、记忆和自动化

- 每次运行绑定 `project_id + owner_user_id + thread_id`。
- 运行只能加载同一项目、同一用户的私有对话和记忆。
- 自动记忆提取只能使用该用户在当前项目中的私有内容。
- 自动化以用户绑定主体运行，不使用项目级服务身份。
- Scheduler 触发前重新验证项目、成员、角色、共享版本、凭据授权和配额。
- 权限撤销终止不再获准的活动工作；配额耗尽只阻止新工作。

## 12. PostgreSQL 任务与事件

| 能力 | 实现 |
| --- | --- |
| 任务队列 | `jobs` + `FOR UPDATE SKIP LOCKED` |
| Worker 租约 | 租约所有者、过期时间和心跳 |
| 重试 | 尝试记录和下次执行时间 |
| 失败任务 | `dead_jobs` |
| 持久化事件 | 仅追加 `run_events` 和单调游标 |
| 唤醒 | 可选 `LISTEN/NOTIFY`，轮询为权威机制 |
| 检查点 | PostgreSQL LangGraph checkpointer |
| 向量 | 可选 pgvector |

任务采用至少一次交付。产生副作用的处理器必须使用幂等键。SSE 支持 `Last-Event-ID` 并从持久化事件继续读取。

## 13. 配额和审计

V1 初始默认值：

- 有效成员：20；
- 项目存储：5 GiB；
- 并发运行：3；
- MCP 调用：每天 10,000 次。

达到 80% 时提醒项目 Admin；达到硬限制时拒绝下一个消耗操作，不中断已经授权并启动的运行。项目 Admin 可以设置更严格限制，不能突破平台上限。

审计只记录治理和运行元数据，不记录私有内容。审计表仅允许追加；更正通过追加补偿记录完成。

## 14. 生命周期与数据保留

- 项目进入 `pending_deletion` 后停止进入、变更和新运行。
- 30 天内可以撤销删除，项目直接回到 `active`。
- 成员退出或被移除后，私有数据冻结 30 天。
- 30 天内重新加入可重新激活原私有工作区；超过期限后清除在线数据。
- 隐私中心允许前成员查看保留期限，并支持导出和提前删除私有数据。
- 项目清除优先于个人冻结期限。

## 15. 注册与邀请

- V1 保留开放注册和本地账户认证。
- V1 不发送验证邮件、邀请邮件或密码重置邮件。
- 注册、登录和邀请兑换使用 PostgreSQL 支持的限流。
- 邀请链接有效期固定为七天，只能成功兑换一次。
- 服务端只持久化 SHA-256 token hash，不保存明文 token。
- token 使用 URL fragment 传递，避免进入服务端访问日志和 referrer。
- 邀请只能授予 Editor、Runner 或 Viewer；Admin 由现有 Admin 显式提升。
- 创建邀请时，如果目标邮箱已对应注册账号，则在同一事务中为该账号写入项目邀请通知；通知保存接收账号、邀请引用、已读状态和必要的安全展示元数据，不保存或返回邀请明文 token、token hash。
- 如果目标邮箱尚未注册，则不创建悬空账号或站内通知，继续向邀请者返回可复制的一次性邀请链接；目标用户注册后通过该链接兑换。
- 已注册接收者可以在工作区通知面板中接受邀请。接受操作使用服务端认证账号和通知绑定的邀请引用完成校验，不信任客户端提供的邮箱或用户 ID；邀请过期、撤销、已兑换或版本冲突时返回稳定错误且不创建重复成员关系。
- 通知读取、未读计数和标记已读均持久化到 PostgreSQL。接受邀请成功后，对应通知进入已处理状态并从待处理计数中移除；同一邀请通过链接先行兑换、过期或被撤销时，通知也不得继续提供可执行操作。

## 16. 完整 Schema 初始化策略

`backend/packages/harness/deerflow/persistence/full_schema.sql` 是唯一业务 Schema 初始化快照，
marker 为 `full_schema_v1`。它直接描述当前所需的表、索引、约束、函数和触发器，不存在
Alembic revision 链或增量升级入口。

新安装顺序是：创建空数据库 → `make setup-db` → `make check-db` → `make start`。Setup 原子执行
完整快照，然后初始化 packaged system asset catalog、LangGraph schema 和 default project。

已有数据库只有在 marker 与 catalog 均精确匹配 `full_schema_v1` 时才可继续使用。旧 marker、
未纳管非空 schema 或 catalog drift 均拒绝自动处理，必须在停服、备份并由操作者确认目标后重建。
运行时启动和 `check-db` 始终只读；系统不执行隐式升级、自动修复、清空或删除。

## 17. 安全和错误语义

- 缺少可信项目或用户上下文时拒绝访问。
- 跨项目和跨用户私有访问返回 `404`。
- 当前项目内治理能力不足返回 `403`。
- 跨账号通知读取、计数、标记已读和执行操作统一返回 `404`，通知响应不返回邀请 token 或 token hash。
- slug 冲突、同项目 Skill 显示名称冲突、最后一名 Admin、邀请竞争和过期版本返回稳定 `409`。
- 配额限制返回稳定 `429`。
- 数据库或 Worker 暂时不可用返回 `503`。
- 错误响应包含稳定公共错误码和 `request_id`，不返回原始 SQL 错误。
- 登录 Cookie 使用 `HttpOnly`、`Secure` 和 `SameSite`，敏感变更保留 CSRF 防护。
- 数据库密码和应用主密钥分别管理。

## 18. 测试与发布门禁

发布测试至少包含两个项目、同项目两名成员和一名项目外用户。

必须覆盖：

- 角色能力矩阵；
- 所有共享和私有仓储的作用域查询；
- 跨项目、跨用户读取、搜索、分页、更新、删除和导出；
- 复合外键拒绝错误关联；
- 并发邀请兑换和最后一名 Admin 保护；
- 已注册邀请接收者的通知创建、未读持久化、通知内接受，以及跨账号通知枚举与操作拒绝；
- 未注册邮箱不创建站内通知、一次性邀请链接仍可兑换，以及通知响应不泄露 token/hash；
- Worker 租约、重试和重复交付；
- 权限撤销后的运行终止；
- SSE 断线或进程重启后的游标续传；
- 凭据加密、轮换和脱敏；
- 项目删除、撤销删除、成员退出、重新加入和数据清除；
- 空库安装单一当前 baseline 幂等、精确当前 head 只读校验，以及未知结构 fail-before-DDL；
- 前端账户、项目切换和退出登录后的缓存隔离。

除非隔离、隐私、schema 初始化、凭据和项目生命周期测试全部通过，否则不得发布。

## 19. 交付里程碑

| 里程碑 | 内容 | 当前状态 |
| --- | --- | --- |
| M1 | 决策冻结、威胁模型、数据清单、PostgreSQL-only 切换、项目基础和项目工作台 | 已完成 |
| M2 | 工作空间与项目壳层、成员、邀请、角色变更、退出、删除与撤销 | 已完成 |
| M3 | 系统与项目 Agent、Skill、MCP 版本、绑定、凭据审批和资产迁移 | 已完成 |
| M4 | 私有对话、运行、文件、记忆和连接 | 已完成 |
| M5 | 自动化项目化与持久化任务 | 已完成 |
| M6 | Worker/持久化 SSE、配额、审计和平台管理 | 已完成 |
| M7 | 最终 legacy source/API 清理、schema 基线与运行时只读校验收口 | 已完成 |
| M8 | 完整隔离矩阵、安全审查、宿主机验证和发布验收 | 已完成 |

里程碑完成状态以当前代码、已提交 migration 单一当前 head 和自动化门禁证据为准。

## 20. 关键权衡

- 不使用 RLS 降低连接池、迁移和调试复杂度，但要求所有业务读写强制经过作用域仓储。
- PostgreSQL-only 消除双后端差异，但本地开发和测试必须始终具备真实 PostgreSQL。
- PostgreSQL 文件存储减少基础设施依赖，但增加数据库容量和运维压力，必须执行大小和保留限制。
- 私有工作缩小协作范围，但形成清晰隐私承诺。
- 不可变版本增加存储成本，但保证历史运行可复现，并支撑 MCP 凭据审批。
- 不接入邮件服务降低 V1 范围，但邀请和密码找回体验有限。

## 21. 最终验收摘要

V1 已满足：项目优先、多项目、四种固定项目角色、三类共享 AI 资产、完整私有工作边界、PostgreSQL-only
持久化、业务层授权、强制作用域仓储、数据库复合约束、持久化任务与事件、可验证的空库安装和显式向前迁移。
M8 认证路径为全新 PostgreSQL 数据库、`make setup-db`、`make start`、桌面版 Chromium 和 DeepSeek
`deepseek-v4-pro`。Docker Compose、Kubernetes/Helm、Firefox、Safari/WebKit 和其他模型供应商未经过
M8 生产认证；M8 完成不等于已创建版本 tag、推送远端或发布制品。
