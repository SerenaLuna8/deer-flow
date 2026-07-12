# M2 工作空间与项目治理专项规格

- 日期：2026-07-12
- 状态：规划中
- 所属总体设计：`2026-07-12-project-first-saas-design.md`
- 当前完成度：0

## 1. 目标

M2 把 M1 的项目基础扩展为可用的项目治理闭环：登录后进入只展示多个项目的全局工作空间，进入项目后才加载绑定 `ProjectContext` 的项目壳层和左侧菜单；项目 Admin 可以管理成员、邀请、角色和项目删除恢复，普通成员可以查看成员并退出项目。

M2 完成后，项目身份的新增、变更和撤销都能立即影响后续授权。M2 仍不开放项目内私有对话，也不宣称完成成员私有数据的冻结、恢复或清除；这些动作必须等 M4 完成 Thread、run、file、memory 和 connection 的项目与 owner 双重隔离后才能闭环。因此 M2 仍不能作为完整多用户 SaaS 发布。

## 2. 范围

### 2.1 范围内

- `/workspace` 作为登录后的多项目工作空间；
- `/workspace/projects` 兼容重定向到 `/workspace`；
- 工作空间不显示项目级左侧菜单；
- `/projects/{project_slug}` 项目壳层和项目级左侧菜单；
- 项目概览、成员与邀请、项目设置三个 M2 菜单入口；
- 成员列表、角色变更、移除成员和主动退出；
- 最后一名有效 Admin 保护；
- 七天有效、一次性兑换、可撤销的邀请链接；
- 项目进入 `pending_deletion`、30 天恢复窗口和恢复操作；
- 成员退出或移除后的失效时间、原因和保留期限元数据；
- 作用域 repository、稳定错误码、并发事务和 PostgreSQL 约束；
- 账户、项目和成员版本共同参与前端缓存隔离；
- 后端 PostgreSQL 集成测试、前端单元测试和 Playwright E2E。

### 2.2 范围外

- 邀请邮件、邮件验证和密码重置邮件；
- 组织、团队目录和批量成员同步；
- Agent、Skill、MCP 的项目化和版本管理；
- 项目内私有对话、运行、文件、记忆和连接；
- 成员私有数据的实际冻结、恢复、导出和清除；
- 到期项目的跨业务域物理清除；
- 配额、审计、账单和平台管理；
- PostgreSQL RLS。

## 3. 信息架构

### 3.1 工作空间

`/workspace` 是跨项目层级，页面只包含：

- 产品标识和当前账户菜单；
- 创建项目；
- 搜索、置顶和按最近进入排序；
- 多个项目卡片；
- 当前账户邮箱匹配的待兑换邀请入口；
- Admin 可恢复项目区域；
- 无项目时的创建和兑换邀请引导。

工作空间不显示 Agent、Skill、MCP、对话、Memory、Tools、自动化或项目设置入口。旧版 `/workspace/chats` 等兼容页面可以继续使用旧壳层，但不能出现在项目优先工作空间的主导航中。

### 3.2 项目壳层

进入 `/projects/{project_slug}` 时，前端先按当前账户解析项目，再调用 enter API 更新最近进入时间。成功后项目壳层提供：

- 项目标识、名称和当前角色；
- 项目概览；
- 成员与邀请；
- 项目设置；
- 返回工作空间；
- 当前账户菜单。

M2 不提前显示尚未实现的 Agent、Skill、MCP、私有工作或自动化菜单项。项目壳层的数据源是服务端返回的项目和能力，前端不得从 `role` 推导权限。

### 3.3 路由兼容

- 非静态模式下 `/workspace` 直接渲染工作空间，不再二次跳转；
- `/workspace/projects` 使用服务端重定向到 `/workspace`；
- 静态演示继续使用既有 demo chat 路径，不调用项目 API；
- 项目路由保持 `/projects/{project_slug}`；
- 未登录访问工作空间或项目时进入登录流程，登录后回到原目标；
- `/invite` 保持可公开加载，以便先从 fragment 接收 token 并换取短期 claim cookie，再进入登录流程。

## 4. 成员模型

### 4.1 状态

`project_memberships.status` 扩展为：

- `active`：当前有效成员；
- `left`：成员主动退出；
- `removed`：由 Admin 移除。

同一 `(project_id, user_id)` 始终只有一行。重新加入时复用原 membership，恢复为 `active`，保留 membership ID 并递增 `version`，避免历史引用漂移。

新增字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `ended_at` | TIMESTAMPTZ | 退出或移除时间 |
| `retention_until` | TIMESTAMPTZ | 固定为 `ended_at + 30 days` |
| `ended_by_user_id` | VARCHAR(36) | 主动退出时为本人，移除时为执行 Admin |
| `end_reason` | VARCHAR(16) | `left` 或 `removed` |

恢复为 active 时清空上述字段。M2 只记录保留窗口，不操作尚未项目化的私有业务数据。

### 4.2 角色变更

- Admin 可以在 `admin|editor|runner|viewer` 之间变更其他有效成员角色；
- 邀请只能创建 `editor|runner|viewer`，不能直接邀请 Admin；
- Admin 提升必须通过成员角色变更接口显式执行；
- 每次角色或状态变更都递增 membership `version` 和 project `membership_version`；
- 变更请求必须携带期望 membership `version`，不匹配返回 `409`；
- 任何事务都不能使项目失去最后一名 active Admin；
- Admin 自降级、退出或被移除时同样执行最后一名 Admin 检查。

### 4.3 读取规则

- 所有 active 成员可以查看项目内 active 成员的账户邮箱（`account_email`）、角色和加入时间；
- 只有具备 `project.members.manage` 的成员可以查看邀请列表、创建/撤销邀请、变更角色和移除成员；
- left/removed 成员不能解析 `ProjectContext`，项目内读取统一返回 `404`；
- 项目外用户不能根据 membership ID 判断成员是否存在。

## 5. 邀请模型

新增 `project_invitations`：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `id` | UUID | 主键 |
| `project_id` | UUID | 非空，外键到 projects |
| `invited_email` | VARCHAR(320) | 非空，保存去空格后的小写值 |
| `role` | VARCHAR(16) | `editor|runner|viewer` |
| `token_hash` | CHAR(64) | SHA-256 十六进制，唯一 |
| `status` | VARCHAR(16) | `pending|redeemed|revoked|expired` |
| `expires_at` | TIMESTAMPTZ | 创建时间加七天 |
| `version` | BIGINT | 从 1 开始 |
| `created_by_user_id` | VARCHAR(36) | 创建邀请的 Admin |
| `redeemed_by_user_id` | VARCHAR(36) | 可空 |
| `redeemed_at` | TIMESTAMPTZ | 可空 |
| `revoked_at` | TIMESTAMPTZ | 可空 |
| `created_at` | TIMESTAMPTZ | 非空 |

同一项目、同一邮箱最多存在一条 pending 邀请，使用 PostgreSQL partial unique index 约束。兑换时仍以 `expires_at` 为权威判断；创建同邮箱新邀请时先锁定 project，把已经到期的 pending 邀请标记为 expired，再创建新邀请，不依赖后台定时任务。

创建邀请时使用 `secrets.token_urlsafe(32)` 生成明文 token，只在创建响应中返回一次；数据库只保存 SHA-256 hash。前端生成 `/invite#token=<token>`，不得把 token 放入 query、日志、分析事件或持久化存储。

兑换流程：

1. 邀请页从 URL fragment 读取 token，并立即使用 `history.replaceState` 清除 fragment；
2. 页面调用未认证的 claim API，把 token 放入 POST body；
3. 服务端 hash token、校验邀请仍可尝试兑换，并签发十分钟有效的 `HttpOnly`、`SameSite=Lax` claim cookie；
4. 未登录用户进入登录流程，`next` 只包含 `/invite`，不包含 token；
5. 登录完成后页面调用 redeem API，服务端验证 claim cookie，并按 claim 中的 invitation ID 和 token hash 做一次不加锁定位，只取得 `project_id`；
6. 服务端先用 `SELECT ... FOR UPDATE` 锁定 project，再按 `project_id + invitation_id + token_hash` 锁定并重验 invitation，校验 pending、未过期、未撤销和当前账户 email 精确匹配规范值；
7. 在同一 project 锁下锁定或创建 membership，创建新 membership 或重新激活原 membership；
8. 标记 invitation 为 redeemed，递增 project `membership_version` 并清除 claim cookie；
9. 事务提交后返回项目 slug，前端进入项目。

claim cookie 名为 `project_invitation_claim`。服务端使用带固定 domain-separation label 的 SHA-256 从现有 Auth JWT secret 派生 32-byte AES-GCM key；每个 cookie 使用随机 12-byte nonce，并以 cookie 名和格式版本作为固定 AAD，对严格只含 invitation UUID、token hash、`iat` 和 `exp` 的 JSON payload 做认证加密。cookie 输出仅为 base64url(nonce + ciphertext + tag)，客户端不可读且任何篡改都会验证失败。cookie 固定十分钟有效，使用 `HttpOnly`、`SameSite=Lax`、path `/api/project-invitations`，`Secure` 跟随现有 `is_secure_request`。认证失败、过期或 invitation 不匹配时统一拒绝，redeem 无论成功或失败都用相同 path 清除 cookie。

claim API 对有效、无效和已受限 token 返回完全相同的 status、body 和同形状 opaque cookie。无效或受限路径签发带随机 invitation UUID 和输入 token hash 的不可用 claim；认证加密使客户端不能读取或对比 invitation UUID。claim 不返回 invitation 状态、邮箱、项目或任何 token 存在性信息。

邀请 claim/redeem 失败限流使用独立 PostgreSQL 表 `project_invitation_rate_limits`，固定窗口阈值沿用登录规则：5 次、5 分钟。表只保存 SHA-256 key、失败次数、窗口起止时间和更新时间；claim key 对 action + 可信客户端 IP 整体 hash，redeem key 再加入当前账户规范化 email 后整体 hash，不保存原始 IP 或 email。失败通过 `INSERT ... ON CONFLICT DO UPDATE` 原子累加，检查在事务中锁定单行；成功清除对应计数。claim 受限时仍返回上述通用响应和不可用同形状 cookie，redeem 受限时统一安全失败为 `PROJECT_INVITATION_INVALID`，不新增可用于枚举的公共错误语义。

并发兑换只有一个事务成功，其他请求返回稳定 `409`，不能创建重复 membership。

## 6. 项目删除与恢复

`projects.status` 扩展为 `active|pending_deletion`，新增：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `deletion_requested_at` | TIMESTAMPTZ | 请求删除时间 |
| `deletion_effective_at` | TIMESTAMPTZ | 固定为请求时间加 30 天 |
| `deletion_requested_by_user_id` | VARCHAR(36) | 执行 Admin |

请求删除必须具备 `project.lifecycle.manage`。事务将 active 项目改为 pending_deletion 并递增 `membership_version`。pending_deletion 项目：

- 不允许进入、更新、创建邀请或执行成员变更；
- active 成员的项目内访问返回 `404`；
- 只在 active Admin 的工作空间恢复区域可见；
- 在 `deletion_effective_at` 前允许恢复为 active；
- 恢复时清空删除字段并再次递增 `membership_version`；
- 到期后拒绝恢复并保持不可访问。

M2 不执行物理删除。跨业务域清除必须等相关数据完成项目化后由后续里程碑统一实现，避免 M2 删除尚未纳入项目边界的旧数据。

## 7. API

### 7.1 成员

- `GET /api/projects/{project_id}/members`
- `PATCH /api/projects/{project_id}/members/{membership_id}`，body：`role`、`version`
- `DELETE /api/projects/{project_id}/members/{membership_id}`，body：`version`
- `POST /api/projects/{project_id}/leave`，body：`version`

### 7.2 邀请

- `GET /api/project-invitations/mine`
- `GET /api/projects/{project_id}/invitations`
- `POST /api/projects/{project_id}/invitations`，body：`email`、`role`
- `DELETE /api/projects/{project_id}/invitations/{invitation_id}`，body：`version`
- `POST /api/project-invitations/claim`，未认证，body：`token`，成功时设置短期 claim cookie
- `POST /api/project-invitations/redeem`，已认证，从 claim cookie 读取兑换凭据

邀请创建响应额外返回一次性 `invite_url_fragment`（`/invite#token=...`）；普通 invitation response、项目邀请列表和 `mine` 均不返回明文 token 或 token hash。`mine` 只按当前认证账户的规范化 email 返回仍可兑换邀请元数据。

### 7.3 生命周期

- `POST /api/projects/{project_id}/deletion`
- `POST /api/projects/{project_id}/restore`

项目列表响应增加 `deletion_effective_at`。普通列表只返回 active 项目；`include_recoverable=true` 只额外返回当前用户是 active Admin 且仍在恢复窗口内的 pending_deletion 项目。

## 8. 错误语义

| HTTP | 公共错误码 | 场景 |
| --- | --- | --- |
| 403 | `PROJECT_MEMBERSHIP_FORBIDDEN` | 当前项目内缺少治理能力 |
| 404 | `PROJECT_OR_MEMBER_NOT_FOUND` | 项目外、失效成员或跨项目资源 |
| 409 | `PROJECT_LAST_ADMIN` | 操作会移除最后一名 active Admin |
| 409 | `PROJECT_MEMBERSHIP_VERSION_CONFLICT` | membership version 过期 |
| 409 | `PROJECT_INVITATION_CONFLICT` | 重复邀请或并发兑换 |
| 409 | `PROJECT_INVITATION_INVALID` | 已兑换、撤销、过期或邮箱不匹配 |
| 409 | `PROJECT_DELETION_STATE_CONFLICT` | 删除或恢复状态不允许 |
| 422 | `PROJECT_VALIDATION_FAILED` | email、role、token 或请求体无效 |
| 503 | `DATABASE_UNAVAILABLE` | PostgreSQL 暂时不可用 |

错误响应只包含稳定 code、通用 message 和 request_id，不泄露目标项目、成员、邮箱或 token 是否存在。

## 9. 事务与并发

- 角色变更、移除、退出和重新激活使用同一事务锁定 project 和目标 membership；
- 最后一名 Admin 计数在锁内执行；
- 邀请创建依赖 partial unique index 作为最终并发防线；
- 邀请失败限流依赖 PostgreSQL 主键冲突上的原子 upsert；并发写不能丢失计数，检查必须在数据库事务中执行，不使用进程内 dict 或 Redis；
- invitation mutation 统一遵循 `project -> invitation -> membership` 锁序；create/revoke 到 invitation 为止，redeem 才继续锁定或创建 membership，禁止 create 与 redeem 使用反向锁序；
- redeem 可先按 claim 中的 invitation ID + token hash 做不加锁定位以取得 `project_id`，随后必须锁 project，再按 `project_id + invitation_id + token_hash` 锁定并重验 invitation；
- 删除和恢复锁定 project；
- 所有成员变更同时递增 project `membership_version`；
- `ProjectContext` 只解析 active membership 和 active project；
- 不使用 PostgreSQL RLS，不使用 superuser 应用连接。

## 10. 前端状态与缓存

- 所有 query key 以账户 ID 开头；
- 项目内 query key 同时包含 project ID；
- 成员详情和 mutation 使用 membership ID 与 version；
- 角色变更、移除、退出、兑换邀请、删除和恢复成功后，取消并失效相关 project、member、invitation 和 workspace queries；
- 当前账户退出项目或项目进入 pending_deletion 后立即跳回 `/workspace`；
- 账户切换或退出登录继续先取消请求，再清空 provider-owned QueryClient；
- 迟到响应不得把旧账户、旧项目或旧 membership version 写入当前页面。

## 11. 测试门禁

后端 PostgreSQL 集成测试至少覆盖：

- 同项目两名 Admin、最后一名 Admin 保护和并发降级；
- Editor/Runner/Viewer 的治理拒绝；
- 跨项目 membership ID 的读取、变更和删除统一 404；
- invitation token 只存 hash、claim cookie 签名与过期、七天过期、撤销、邮箱不匹配和并发兑换；
- left/removed membership 立即不能解析 ProjectContext；
- 重新加入复用 membership ID 并递增 version；
- pending_deletion 禁止进入和治理、窗口内恢复、窗口后拒绝恢复；
- migration 在空库与 M1 数据库上升级；
- 所有数据库测试使用临时 `deerflow_test_*` PostgreSQL 数据库。

前端单元测试和 Playwright 至少覆盖：

- 登录后 `/workspace` 显示多个项目卡片且没有项目级左侧菜单；
- `/workspace/projects` 重定向；
- 进入项目后出现项目级菜单；
- 非 Admin 不显示治理操作，但服务端仍是授权来源；
- 创建邀请后复制 fragment 链接，页面清除 fragment、不写入 storage，并通过 claim cookie 跨越登录流程；
- 角色冲突、最后一名 Admin、移除、退出、删除和恢复的可恢复错误状态；
- 账户切换和项目切换没有缓存串扰；
- 静态 demo 不调用项目 API。

## 12. 完成标准

- 总体设计的 M2 范围与本规格一致；
- PostgreSQL schema、约束、migration 和真实数据库测试完成；
- 工作空间与项目壳层边界在路由和视觉层级上成立；
- 成员、邀请、角色、退出、移除、删除和恢复形成端到端闭环；
- 最后一名 Admin 和并发兑换不可被竞态绕过；
- 所有 token、数据库错误和跨项目标识均不泄露；
- README、AGENTS 和中文用户文档同步；
- M2 状态更新为已完成前，专项测试证据和审查结论已经记录。
