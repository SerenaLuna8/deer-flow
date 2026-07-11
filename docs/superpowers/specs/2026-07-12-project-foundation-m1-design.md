# M1 项目基础专项规格

- 日期：2026-07-12
- 状态：待评审
- 所属总体设计：`2026-07-12-project-first-saas-design.md`
- 当前完成度：0

## 1. 目标

M1 建立项目优先 SaaS 的项目治理基础闭环：系统只使用 PostgreSQL；用户登录后进入项目工作台；用户可以创建、查看、更新、进入和置顶自己的项目；所有项目 API 通过可信 `ProjectContext` 和作用域仓储授权。

M1 完成后，系统仍在旧版模式保留现有私有 Agent 对话体验，但尚不交付邀请、成员管理、共享资产版本、完整私有数据迁移、配额执行或项目删除。由于 Thread、run、file、memory 和 automation 尚未形成项目与所有者双重隔离，M1 不能单独作为多用户 SaaS 发布。

## 2. 范围

### 2.1 范围内

- PostgreSQL-only 配置和启动检查；
- 数据库创建、迁移和检查脚本；
- 平台角色 `system_admin | user`；
- Admin、Editor、Runner、Viewer 能力模型；
- `projects` 和 `project_memberships`；
- `ProjectContext` 解析和业务授权；
- 强制作用域项目仓储；
- 当前账户的默认项目初始化；
- 项目创建、列表、详情、更新、进入和置顶 API；
- `/workspace/projects` 项目工作台；
- `/projects/{project_slug}` 项目主页；
- 项目作用域前端查询键和过期响应防护；
- PostgreSQL 集成测试、前端单元测试和 E2E 隔离测试；
- 对应的 README 和 AGENTS 文档更新。

### 2.2 范围外

- 邀请、成员列表、角色变更、退出和移除；
- 项目删除、恢复和隐私中心；
- Agent、Skill、MCP 项目持久化和版本；
- 项目凭据；
- 现有 Thread、run、file、memory、automation 的项目字段回填；
- PostgreSQL 任务队列和 Worker 租约重构；
- 配额、审计和 `/system` 管理页面；
- pgvector；
- 邮件服务。

## 3. 用户体验

### 3.1 登录后路由

- 已登录用户默认进入 `/workspace/projects`。
- 用户没有项目时显示创建项目引导。
- 用户至少有一个项目时显示项目卡片网格。
- 不自动进入最近项目，不提供项目下拉切换器。
- 未登录访问项目页面时进入现有登录流程，并在登录后返回项目工作台。

### 3.2 项目工作台

项目卡片显示：

- 项目名称、图标和描述；
- 当前用户角色；
- 成员数量；
- 最近进入时间；
- 是否置顶；
- Agent、Skill 和 MCP 数量，M1 固定显示为 0；
- 项目状态，M1 只支持 `active`。

支持：

- 创建项目；
- 按名称或 slug 搜索；
- 置顶和取消置顶；
- 进入项目；
- Admin 修改显示名称、图标和描述。

### 3.3 项目主页

项目主页显示：

- 项目标识和描述；
- 当前用户角色；
- “开始私有对话”主要操作及未开放状态；
- “对话和记忆私有，Agent、Skill 和 MCP 共享”的边界提示；
- 返回项目工作台；
- Agent、Skill、MCP 的占位入口，标记为后续里程碑交付。

“开始私有对话”在 M1 中由 `project_private_workspace` 功能门禁控制，默认关闭并显示“私有工作区将在后续里程碑开放”。M1 不允许从项目页面创建未绑定项目的 Thread。现有 `/workspace/chats` 只在旧版模式保留，不属于项目优先 SaaS 的可发布入口。

## 4. 角色与能力

M1 建立完整能力常量，但只在项目 CRUD 和进入流程中使用相关能力。

建议能力标识：

- `project.read`
- `project.update`
- `project.enter`
- `project.pin`
- `project.members.manage`
- `shared_assets.read`
- `shared_assets.execute`
- `shared_assets.edit`
- `mcp.credentials.approve`
- `private_work.create`
- `private_work.read_own`
- `automation.manage_own`
- `project.audit.read`
- `project.usage.read`
- `project.lifecycle.manage`

映射集中在后端单一模块。前端可以复用服务端返回的能力列表控制可见性，但不能自行推导授权。

M1 实际行为：

- 所有有效成员可以读取、进入和置顶项目；
- Admin 可以更新项目；
- Editor、Runner、Viewer 更新项目返回 `403`；
- 创建项目的用户自动成为 Admin；
- M1 没有创建其他角色成员的产品入口。

## 5. 数据库设计

### 5.1 用户角色变更

`users.system_role` 只允许：

- `system_admin`
- `user`

现有平台 `admin` 在 migration 中映射为 `system_admin`。平台角色不授予任意项目成员身份。

### 5.2 `projects`

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `id` | UUID | 主键 |
| `slug` | VARCHAR(63) | 非空，保存小写规范值 |
| `display_name` | VARCHAR(120) | 非空 |
| `description` | VARCHAR(500) | 非空，默认空字符串 |
| `icon` | VARCHAR(32) | 非空，默认产品图标 |
| `status` | VARCHAR(32) | 非空，M1 仅允许 `active` |
| `is_suspended` | BOOLEAN | 非空，默认 false |
| `membership_version` | BIGINT | 非空，默认 1 |
| `created_by_user_id` | UUID | 外键至 `users.id` |
| `created_at` | TIMESTAMPTZ | 非空 |
| `updated_at` | TIMESTAMPTZ | 非空 |

约束：

- `slug = lower(slug)`；
- slug 只允许小写字母、数字和单连字符；
- slug 长度 3 至 63；
- slug 全局唯一；
- `status` 使用 CHECK constraint；
- `membership_version >= 1`。

不依赖 `citext` 扩展；服务端规范化 slug，数据库使用小写检查和唯一约束双重保证。

### 5.3 `project_memberships`

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `id` | UUID | 主键 |
| `project_id` | UUID | 非空，外键至 `projects.id` |
| `user_id` | UUID | 非空，外键至 `users.id` |
| `role` | VARCHAR(16) | `admin|editor|runner|viewer` |
| `status` | VARCHAR(16) | M1 仅允许 `active` |
| `version` | BIGINT | 非空，默认 1 |
| `is_pinned` | BOOLEAN | 非空，默认 false |
| `last_entered_at` | TIMESTAMPTZ | 可空 |
| `created_at` | TIMESTAMPTZ | 非空 |
| `updated_at` | TIMESTAMPTZ | 非空 |

约束：

- `(project_id, user_id)` 唯一；
- `version >= 1`；
- role 和 status 使用 CHECK constraint；
- 删除项目时成员关系级联删除，但普通产品 API 不执行物理删除。

M1 将置顶和最近进入记录保存在成员关系上，因为它们是用户对项目的个人偏好，不是项目共享设置。

### 5.4 migration 规则

- 新 migration 只面向 PostgreSQL。
- migration 不包含 SQLite batch mode 或方言分支。
- schema 变更只通过 Alembic 执行。
- `Base.metadata.create_all()` 不作为运行时 schema 初始化方式。
- migration 必须支持在空数据库和现有 DeerFlow 数据库上执行。
- migration 不删除现有业务数据。

## 6. PostgreSQL 初始化

### 6.1 配置

使用 `DATABASE_URL`。密码中的特殊字符必须进行 URL 编码。日志只能打印脱敏后的 host、port 和 database。

移除：

- `database.backend`；
- SQLite 文件路径；
- SQLite checkpointer 配置；
- SQLite WAL、busy timeout 和本地数据库锁；
- 持久化内存后端的生产入口。

### 6.2 脚本

`backend/scripts/setup_postgres.py`：

1. 解析并验证 PostgreSQL URL；
2. 连接维护数据库 `postgres`；
3. 检查目标数据库 `deerflow`；
4. 不存在时创建，存在时继续；
5. 检查服务器版本；
6. 执行 Alembic upgrade；
7. 执行默认账户和项目初始化；
8. 输出脱敏结果。

脚本只允许固定或严格校验后的数据库名称，避免把标识符直接拼接为任意 SQL。脚本重复执行不得创建重复项目、成员关系或管理员。

`backend/scripts/check_postgres.py`：

- 验证连接；
- 验证目标数据库；
- 验证 migration revision；
- 验证必需表和约束；
- 不修改数据。

Makefile 入口：

- `make setup-db`
- `make migrate-db`
- `make check-db`

## 7. 默认项目初始化

M1 初始化规则：

1. 查询现有用户；
2. 如果没有用户，只完成 schema 初始化，等待首个管理员创建；
3. 如果恰好一个现有平台管理员，为其创建默认项目和 Admin 成员关系；
4. 如果存在多个用户但没有明确系统管理员，停止数据引导并输出脱敏错误；
5. 首次 `/initialize` 创建 `system_admin` 后，在同一业务流程中创建默认项目；
6. 普通新注册用户不自动获得默认项目，登录后进入空项目工作台；
7. 默认项目 slug 为 `default-project`；发生冲突时停止初始化，不自动猜测新 slug。

初始化使用幂等标识和唯一约束，重复执行返回现有项目，不创建副本。

## 8. 后端模块边界

### 8.1 领域模块

建议新增：

```text
backend/app/projects/
  capabilities.py
  context.py
  errors.py
  models.py
  repository.py
  service.py
```

职责：

- `capabilities.py`：角色到能力的唯一映射；
- `context.py`：从认证用户和项目标识解析 `ProjectContext`；
- `errors.py`：稳定领域错误；
- `models.py`：API 之外的领域值对象；
- `repository.py`：强制作用域接口和 PostgreSQL 实现；
- `service.py`：项目事务、授权和不变量。

SQLAlchemy 表模型可以位于 `deerflow.persistence.projects`，以加入现有统一 metadata。该模块只定义持久化模型，不依赖 `app.*`。业务服务位于 `app.*`，保持 `harness -> app` 导入禁令。

### 8.2 `ProjectContext`

`ProjectContext` 是不可变值对象。解析过程：

1. 从认证上下文读取用户；
2. 从路径 UUID 或 slug 解析项目；
3. 查询 `(project_id, user_id)` 的有效成员关系；
4. 检查项目 `active` 且未暂停；
5. 计算能力；
6. 生成请求 ID；
7. 返回上下文。

所有项目业务服务必须接收 `ProjectContext`，不得分别接收客户端提供的 `project_id`、`user_id` 和 role 作为授权依据。

### 8.3 仓储接口

允许：

```python
get_project(context, project_id)
update_project(context, project_id, changes)
list_projects_for_user(user_id)
```

禁止普通路由直接获得：

```python
get_project_unscoped(project_id)
list_all_projects()
```

创建项目是特殊事务：认证用户作为创建者，在同一事务中写入项目和 Admin 成员关系，然后生成上下文。

## 9. API 设计

统一前缀 `/api/projects`。

### 9.1 `POST /api/projects`

请求：

```json
{
  "slug": "research-lab",
  "display_name": "研究实验室",
  "description": "",
  "icon": "folder"
}
```

行为：创建项目和 Admin 成员关系。slug 冲突返回 `409 PROJECT_SLUG_CONFLICT`。

### 9.2 `GET /api/projects`

只返回当前用户拥有有效成员关系的项目。支持 `query`、`pinned` 和稳定游标分页。排序顺序：置顶、最近进入、创建时间、项目 UUID。

### 9.3 `GET /api/projects/{project_id}`

要求有效成员关系。不存在或非成员统一返回 `404 PROJECT_NOT_FOUND`。

### 9.4 `PATCH /api/projects/{project_id}`

只允许 Admin 修改 `display_name`、`description` 和 `icon`。slug 和创建者不可修改。

### 9.5 `POST /api/projects/{project_id}/enter`

要求 `project.enter`，更新当前成员关系的 `last_entered_at`，返回项目主页所需上下文。

### 9.6 `PUT /api/projects/{project_id}/pin`

请求包含 `pinned: boolean`，只更新当前用户成员关系。

### 9.7 响应契约

项目响应包含：

- 项目 UUID、slug、显示名称、描述和图标；
- 当前用户角色和能力；
- 是否置顶、最近进入时间；
- 成员数量；
- Agent、Skill、MCP 数量，M1 为 0；
- 项目状态和暂停状态；
- `membership_version`；
- `request_id`。

不返回其他成员身份或私有活动。

## 10. 前端设计

### 10.1 路由

新增：

- `/workspace/projects`
- `/projects/[project_slug]`

项目优先模式下，现有 `/workspace` 在认证后重定向到 `/workspace/projects`。现有对话路由只在旧版模式保留。`project_private_workspace` 默认关闭，在 M4 完成私有数据迁移与隔离前不得在项目优先模式启用。

### 10.2 查询键

账户级项目列表：

```text
["account", userId, "projects", filters]
```

项目详情：

```text
["account", userId, "project", projectId, "detail"]
```

所有后续项目域查询以：

```text
["account", userId, "project", projectId, ...domainKey]
```

开头。

退出登录时清空 QueryClient 和用户作用域本地状态。切换账户、项目或路由时中止旧请求；响应绑定的 user、project 或 route 不匹配时丢弃响应。

### 10.3 页面状态

工作台和项目主页必须覆盖：

- 加载骨架屏；
- 空项目引导；
- 搜索无结果；
- API 错误和重试；
- 创建冲突；
- 失去成员关系后的安全返回；
- 深色模式；
- 桌面和移动布局；
- 键盘操作和可见焦点。

## 11. 错误处理

| 场景 | HTTP | 公共错误码 |
| --- | --- | --- |
| 未登录 | 401 | `AUTH_REQUIRED` |
| 非成员或项目不存在 | 404 | `PROJECT_NOT_FOUND` |
| 缺少项目治理能力 | 403 | `PROJECT_FORBIDDEN` |
| slug 冲突 | 409 | `PROJECT_SLUG_CONFLICT` |
| 无效输入 | 422 | `PROJECT_VALIDATION_FAILED` |
| PostgreSQL 不可用 | 503 | `DATABASE_UNAVAILABLE` |

错误响应不包含 SQL、表名、数据库 URL、密码或内部堆栈。

## 12. 测试

### 12.1 单元测试

- 角色到能力映射；
- slug 规范化和验证；
- `ProjectContext` 成功与拒绝路径；
- 项目更新字段白名单；
- 查询键包含账户和项目；
- 过期响应丢弃。

### 12.2 PostgreSQL 集成测试

使用真实 PostgreSQL，不使用 SQLite 替代：

- 空数据库 migration；
- 现有数据库 migration；
- setup 脚本幂等；
- 项目与 Admin 成员关系原子创建；
- slug 并发冲突；
- 非成员不能读取项目；
- Editor、Runner、Viewer 不能更新项目；
- 置顶和最近进入只影响当前成员；
- 唯一约束和 CHECK constraint；
- 数据库错误不会泄露连接信息。

### 12.3 前端和 E2E

- 登录后进入项目工作台；
- 无项目用户创建项目；
- 项目卡片搜索、置顶和进入；
- 项目主页和返回工作台；
- Admin 更新项目；
- 非 Admin 不显示并且不能调用更新操作；
- 项目外用户访问 URL 返回安全错误；
- 账户切换无缓存泄露；
- 桌面、移动和深色模式；
- 旧版模式下现有私有对话核心流程不被 M1 页面改动破坏；
- 项目优先模式下，M1 不能从项目页面创建未绑定项目的 Thread。

## 13. 数据迁移交付物

虽然完整私有数据迁移属于后续里程碑，M1 必须交付：

- 用户数量和平台角色清单；
- 默认项目初始化报告；
- 每个用户的项目成员关系验证；
- 后续业务表回填使用的 migration ledger 基础；
- dry-run 模式；
- 重复执行验证；
- 失败时不留下只有项目、没有 Admin 成员关系的半成品。

## 14. 可观测性

M1 记录：

- 请求 ID；
- 项目创建、进入和更新结果；
- 授权拒绝的公共原因类别；
- setup 和 migration 的脱敏状态；
- PostgreSQL 连接池健康状态。

日志不得记录数据库密码、邀请 token、请求 Cookie 或用户私有内容。

## 15. 发布与回滚

M1 发布前：

1. 备份现有 PostgreSQL 数据库和本地用户数据；
2. 执行 `make check-db`；
3. dry-run 初始化；
4. 执行 migration 和默认项目初始化；
5. 运行 PostgreSQL 集成测试和项目 E2E；
6. 启动 Gateway、Frontend 和 Nginx；
7. 验证登录、工作台、创建、进入和现有对话。

如果正式开放前失败，恢复数据库备份和原应用版本。正式开放并产生新项目写入后，不执行破坏性 downgrade；使用前向 migration 修复。

## 16. M1 验收标准

M1 只有在以下条件全部满足时才算完成：

- 系统不再提供 SQLite 或持久化内存后端；
- 本地 Docker PostgreSQL 可以通过脚本创建和初始化 `deerflow`；
- 所有 migration 使用真实 PostgreSQL 验证；
- 平台角色与项目角色分离；
- 项目创建和 Admin 成员关系保持原子性；
- 非成员不能读取项目；
- 非 Admin 不能更新项目；
- 项目工作台和项目主页完成响应式、深色模式、空状态和错误状态；
- 项目查询缓存按账户和项目隔离；
- 旧版模式下现有私有对话核心体验没有回归；
- `project_private_workspace` 默认关闭，且在 M4 验收前不能启用；
- M1 不被标记或部署为完整多用户 SaaS 版本；
- README、根 AGENTS、backend/AGENTS 和 frontend/AGENTS 与实现一致；
- 所有单元、PostgreSQL 集成、前端和 E2E 门禁通过。

## 17. 后续里程碑接口

M1 为后续工作提供稳定基础：

- M2 复用 `ProjectContext`、能力映射、成员表和项目 shell；
- M3 复用项目作用域仓储和共享资产能力；
- M4 为所有私有表增加 `project_id + owner_user_id` 并迁移现有数据；
- M5 在执行前重新解析同一 `ProjectContext`；
- M6 复用项目生命周期、平台角色和治理入口；
- M7 汇总每个里程碑交付的 migration ledger 和验证结果。
