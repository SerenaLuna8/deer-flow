# 10. Auth/Authz 模块：身份会话、项目权限与可插拔策略

## 1. 分析边界与结论

本文把两个容易混淆的概念分开：

- **Auth**：用户是谁，JWT/cookie/session 是否仍有效；
- **Authz**：用户在某个项目、资源或动作上能做什么。

本文所说的 `ProjectContext`、`PrivateWorkContext`、`project_id + owner_user_id` 和
404/403 隐藏语义，**只适用于项目私有资源和相关项目路由**。它们不自动描述：

- `/api/v1/auth/login|register|initialize` 等公共认证入口；
- system-admin 管理面；
- 无项目归属的公共页面或健康检查；
- 未来可能存在的其他全局资源。

结论先行：

1. `dev@8a91e957` 的项目私有授权比 `main@e317f7b8` 的旧
   `Principal + owner_check` 更强：authority 来自 PostgreSQL membership，封装为 issued-only
   context，并在每个 side-effect 锁边界重校验。
2. `main` 的通用 `AuthorizationProvider`/RBAC 可以作为“项目 capability 未表达的策略维度”
   参考，例如全局 route、tool/model 可见性或外部策略属性；它不能替代 ProjectContext，
   也不能让客户端 runtime context 成为项目权限来源。
3. `main` 的大小写不敏感邮箱修复是移植前 `dev` 的确认风险；当前工作树已经在 PostgreSQL
   repository、ORM 和 `full_schema.sql` 落地 canonical email，详见第 16 节。
4. 当前工作树已经移植可关闭的本地自助注册开关，并让 setup-status 与前端 fail closed；
   first-admin 初始化保持独立。
5. “保持登录”是产品能力，不是认证 authority。当前工作树只复用 cookie policy，
   没有退回 `main` 的纯 token-version、SQLite 会话模型，`dev` 的 durable `sid` session
   完整保留。
6. 任何邮箱唯一性 schema 变化必须写入唯一 `full_schema.sql` 并按“新空库重建”生命周期处理，
   不能新增增量 migration 或在运行时修表。

基线：

- 共同祖先：`3be3969f8fc3f2d2b6d36ef5c26fa5593d916f2a`
- `main`：`e317f7b8d9b2afb4c3925812d4774da602c9f8f3`
- `dev`：`8a91e95799c9b345d9540c7e201b33c603e7870c`

> **阅读口径**：第 2–15 节记录上述提交上的移植前源码分析，不代表当前工作树仍然缺少
> 对应能力。当前工作树已经按可移植落点完成的内容、明确未移植的内容、自动化验证和真实
> 浏览器证据统一记录在第 16 节。

## 2. 源码地图

### 2.1 `main` Auth

| 领域 | 文件 | 职责 |
| --- | --- | --- |
| 配置 | `main:backend/app/gateway/auth/config.py` | JWT secret、token lifetime |
| 应用认证配置 | `main:backend/packages/harness/deerflow/config/auth_config.py` | local/OIDC、`allow_registration` |
| JWT | `main:backend/app/gateway/auth/jwt.py` | `sub/exp/iat/ver` |
| 密码 | `main:backend/app/gateway/auth/password.py` | hash/verify/rehash |
| 本地 Provider | `main:backend/app/gateway/auth/local_provider.py` | password/OAuth 用户操作 |
| 用户 Repository | `main:backend/app/gateway/auth/repositories/sqlite.py` | SQLite 用户存储、大小写规范 |
| Auth router | `main:backend/app/gateway/routers/auth.py` | login/register/logout/setup/OIDC |
| Cookie policy | `main:backend/app/gateway/auth/session_cookie.py` | remember-me、HTTPS/localhost/public HTTP |
| Middleware | `main:backend/app/gateway/auth_middleware.py` | cookie 到 user |
| CSRF | `main:backend/app/gateway/csrf_middleware.py` | double-submit |
| OIDC | `main:backend/app/gateway/auth/oidc.py`、`oidc_state.py` | 登录跳转、state、provisioning |

`main` 的 JWT payload 没有 `sid`，只有 `sub/exp/iat/ver`。它以用户 `token_version`
做全局失效，没有 `dev` 的逐会话 durable revoke。

### 2.2 `main` Authz

| 文件 | 关键类型/入口 |
| --- | --- |
| `main:backend/packages/harness/deerflow/authz/provider.py` | `Principal`、`AuthzRequest`、`AuthzDecision`、`AuthorizationProvider` |
| `main:backend/packages/harness/deerflow/authz/principal.py` | `build_principal_from_context()` |
| `main:backend/packages/harness/deerflow/authz/rbac.py` | `RbacAuthorizationProvider` |
| `main:backend/packages/harness/deerflow/authz/runtime.py` | 按 class path 解析 provider |
| `main:backend/packages/harness/deerflow/authz/tool_filter.py` | Agent assembly 时过滤工具 |
| `main:backend/packages/harness/deerflow/authz/adapter.py` | execution-time Guardrail adapter |
| `main:backend/packages/harness/deerflow/authz/enforcement.py` | 双层授权组装 |
| `main:backend/packages/harness/deerflow/config/authorization_config.py` | enabled/fail_closed/default_role/provider |
| `main:backend/app/gateway/authz.py` | route permission decorator 和 provider 决策 |

### 2.3 当前 `dev` Auth/Authz

| 领域 | 文件 | 职责 |
| --- | --- | --- |
| 用户 Repository | `dev:backend/app/gateway/auth/repositories/sql.py` | PostgreSQL `SQLUserRepository` |
| JWT session | `dev:backend/app/gateway/auth/sessions.py` | 生成/哈希 `sid`、持久化、验证、撤销 |
| Session Repository | `dev:backend/packages/harness/deerflow/persistence/auth_sessions/sql.py` | PostgreSQL session authority |
| Auth router | `dev:backend/app/gateway/routers/auth.py` | login/register/logout/password change |
| Rate limit | `dev:backend/app/gateway/auth/rate_limit.py` | PostgreSQL 原子准入 |
| HTTP user dependency | `dev:backend/app/gateway/deps.py` | JWT、user、session 三重验证 |
| Project context | `dev:backend/app/projects/context.py` | Project+Membership 一致解析 |
| Capability | `dev:backend/app/projects/capabilities.py` | project role → capability |
| Private context | `dev:backend/app/private_work/context.py` | issued-only authority |
| Side-effect revalidation | `dev:backend/app/private_work/revalidation.py` | membership/version/capability 再校验 |
| Private error | `dev:backend/app/private_work/error_mapping.py` | 404/403/409/429/503 |
| Schema | `dev:backend/packages/harness/deerflow/persistence/full_schema.sql` | 唯一 PostgreSQL schema 来源 |

## 3. 当前 `dev` 的认证链

### 3.1 登录和会话发行

```text
POST /api/v1/auth/login/local
  -> PostgreSQL rate-limit admit
  -> LocalAuthProvider.authenticate()
  -> SQLUserRepository.get_user_by_email()
  -> password verify / opportunistic rehash
  -> clear login counter
  -> issue_access_session()
     -> generate 256-bit sid
     -> hash_session_id(domain-separated SHA-256)
     -> 先 INSERT auth session
     -> 后签发含 sid + token_version 的 JWT
  -> HttpOnly cookie
```

`issue_access_session()` 的顺序是安全边界：只有 session row 成功落库后才返回 JWT。
若 session storage 失败，登录返回 503，不会发一个无法撤销或无法验证的 cookie。

JWT 里包含原始随机 `sid`，数据库只保存 domain-separated hash，不保存原始 sid 或完整 JWT。

### 3.2 每次请求验证

```text
access_token cookie
  -> decode_token()
  -> SQLUserRepository.get_user_by_id()
  -> payload.ver == user.token_version
  -> validate_access_session(payload)
     -> hash sid
     -> PostgreSQL 验证 user_id/token_version/expiry/revoked_at
  -> request.state.user / dependency user
```

任一环节失败都不能降级为“仅签名有效即可”：

- token 无效/过期/session 已撤销：401；
- auth session PostgreSQL 不可用：503；
- 用户不存在或 token_version 变化：401。

### 3.3 Logout 和密码变更

`logout()`：

1. 验证 token；
2. `revoke_access_session()` 只撤销当前 `sid`；
3. 清浏览器 cookie。

若 durable revoke 失败，接口返回 503，但仍发送删除 cookie，准确表达“本浏览器已退出，
复制的 token 可能尚未撤销”。

密码变更：

1. 校验当前密码；
2. 更新 hash；
3. 增加 `token_version`；
4. `revoke_all_access_sessions(user_id)`；
5. 发行一个新 session。

因此旧设备的 token 即使还在有效期内，也会同时被 token version 和 session row 拒绝。

### 3.4 移植前 Cookie 和 CSRF

当前 `dev` `_set_session_cookie()`：

- `HttpOnly=True`；
- `SameSite=lax`；
- HTTPS 时 `Secure=True` 且持久到 token lifetime；
- HTTP 时为 session cookie；
- CSRF 使用 double-submit，登录/注册/initialize 等明确豁免，受保护 POST 必须匹配。

这里没有 remember-me 用户选择。它是当前行为，不是“变量没生效”。

## 4. 项目私有授权链

### 4.1 HTTP 入口

`private_work_context()`：

```text
authenticated user
  -> UUID(user.id)
  -> resolve_project_context(session, user_id, project_id, request_id)
  -> ProjectContext
  -> PrivateWorkContext.from_project()
```

`resolve_project_context()` 用 Project 和 active Membership 的一致查询解析：

```py
@dataclass(frozen=True)
class ProjectContext:
    user_id: UUID
    project_id: UUID
    membership_id: UUID
    role: ProjectRole
    capabilities: frozenset[Capability]
    membership_version: int
    request_id: str
```

system-admin 身份不会自动构造项目 membership，也不会隐式绕过 project-private scope。

### 4.2 Capability

当前角色映射：

- Admin：全部 Project capability；
- Editor：own private work + shared asset read/execute/edit；
- Runner：own private work + shared asset read/execute；
- Viewer：own private work read + shared asset read；
- 所有角色的基础集包含 project read/enter/pin。

这不是全局 RBAC；它只表达 Project 中的能力。

### 4.3 `PrivateWorkContext` 防伪造

`PrivateWorkContext`：

- `init=False`，直接构造抛错；
- 只能从精确 `ProjectContext` 类型派生；
- 不能 subclass；
- copy/deepcopy/pickle/state hooks 全部拒绝；
- 发行时把对象 identity 和不可变 snapshot 登记到进程内 weak registry；
- `require_issued_private_work_context()` 要求同一个对象、同一 snapshot、未篡改。

`resource_scope` 只在 issued context 上产生：

```text
PrivateResourceScope(
  project_id,
  owner_user_id=user_id,
  membership_version
)
```

这防止应用内部某段代码用一个“字段看起来相同”的 dataclass 冒充 authority。

### 4.4 Side-effect 再校验

`PrivateWorkRevalidator.require(session, context, *capabilities, lock=False)`：

1. 验证 issued object；
2. 在调用者事务内重新解析当前 Project 和 Membership；
3. 对 side effect 使用 project → membership 的固定 `FOR UPDATE` 顺序；
4. 比较 user/project/membership ID/version；
5. stale、ended、替换 membership 返回 404；
6. 当前 membership 仍存在但 capability 不足返回 403。

所以“请求进入时有权限”不等于“执行时永远有权限”。Run admission、Worker side effect、
file/artifact、Credential 和 channel inbound 都必须在自己的安全边界重校验。

## 5. `main` 的通用 AuthorizationProvider

### 5.1 协议

```py
@dataclass
class Principal:
    user_id: str | None
    role: str | None
    oauth_provider: str | None
    oauth_id: str | None
    channel_user_id: str | None
    is_internal: bool
    attributes: dict[str, Any]

@dataclass
class AuthzRequest:
    principal: Principal
    resource: str
    action: str
    target: str
    context: dict[str, Any]

class AuthorizationProvider(Protocol):
    authorize(request) -> AuthzDecision
    async aauthorize(request) -> AuthzDecision
    filter_resources(principal, resource_type, candidates) -> list[str]
```

`build_principal_from_context()` 是 `main` 唯一 sanctioned builder。它要求
`authz_attributes` 是 Mapping，并对每次执行重新构造 Principal，避免缓存 stale identity。

### 5.2 双层工具授权

`main` 使用同一 policy 两次：

1. Agent assembly 时 `filter_resources()` 移除模型永远不应看到的工具；
2. execution-time adapter 在真正调用前再次 `authorize()`，覆盖动态资源和参数。

这比只在工具执行时拒绝更好，因为模型和 `tool_search` 也看不到被禁工具。

### 5.3 内置 RBAC 语义

`RbacAuthorizationProvider`：

- 显式映射 `tool/model/skill/sandbox/mcp_server/route` 到配置键；
- deny 永远优先；
- unknown role 抛错，交由 fail-closed/open 策略；
- policy 中 typo、null、非法 allow/deny 在构造时拒绝；
- `allow` 缺失表示 allow-all，再应用 deny；
- 某 role/resource 完全没有 policy 表示 unrestricted；
- `action` 存在于协议，但当前内置 RBAC 不把它作为规则维度。

最后一点很重要：不能写成“main 内置 RBAC 可以按 action 精细控制”。当前实现实际按
`role + resource + target` 决策。

### 5.4 Gateway route permissions

`main:backend/app/gateway/authz.py` 的 `resolve_route_permissions()` 对六个旧全局权限并行调用
provider：

- threads read/write/delete；
- runs create/read/cancel。

provider resolution 或调用失败时按 `fail_closed` 决定空权限还是全权限。它仍配合 legacy
`owner_check=True`，没有 `dev` 的 Project membership/version/scope。

## 6. 通用 Authz 如何与 `dev` 共存

如果未来在 `dev` 引入 `AuthorizationProvider`，正确关系是“附加约束”，不是“替代”：

```text
项目私有允许 =
  authenticated durable session
  AND issued current ProjectContext
  AND current Project capability
  AND resource scope(project + owner)
  AND optional external/global policy allow
```

适合的使用点：

- 非项目私有的全局 route；
- Agent assembly 中 tool/model 的额外组织策略；
- 项目 capability 没表达的 provider-specific attributes；
- external policy engine 的 deny。

不适合的使用点：

- 从 `request.context.user_role/project_id` 构造项目 authority；
- 用 `Principal.role` 取代 membership；
- 用 `filter_resources()` 取代 exact admitted Agent/Skill/MCP snapshot；
- 在 Worker 中只看准入时 Principal，不重新校验 Project membership；
- 让 system-admin role 自动读任意项目 private work。

若引入，Principal 只能由 server-resolved authenticated user + ProjectContext 派生；
client `metadata/config/context` 中同名字段必须继续被剥离。

## 7. 邮箱大小写：确认风险

### 7.1 `main` 的修复

`main@b5cc3a81`：

- 写入前把整个邮箱 lowercase；
- 查询使用 `func.lower(UserRow.email) == normalized_email`；
- create 前先查 case-insensitive collision；
- update/OIDC provisioning 也走同一规则；
- 对历史可能重复记录使用确定性 order/limit，避免 `scalar_one` 崩溃。

### 7.2 移植前 `dev`

当前代码：

```py
select(UserRow).where(UserRow.email == email)
```

当前 schema：

```sql
CREATE UNIQUE INDEX ix_users_email ON users (email);
```

PostgreSQL 默认比较下，`User@Example.com` 和 `user@example.com` 可以是两行。
这会影响 local login、registration、OIDC 与邀请账号归并，是源码可确认的风险。

### 7.3 精确落点

1. 定义唯一 `_normalize_email()`，所有 local/OIDC/create/update/lookup 调用；
2. Repository 查询使用 `lower(email)`；
3. `full_schema.sql` 对新空库建立 `lower(email)` 唯一索引，或选定 `citext` 后统一 ORM/schema；
4. ORM 模型与 schema 保持一致；
5. setup/bootstrap/admin/OIDC/invitation 流全部使用相同规范；
6. 增加大小写碰撞测试。

当前项目不支持旧数据库增量升级。不能增加 migration 或运行时修复；已有业务库需要按项目
既定流程迁到新空库，而不是在线改 index。

## 8. 本地自助注册开关：移植前确认缺失

`main@09e25b8a` 增加：

```text
auth.local.allow_registration: bool = true
```

`_local_registration_enabled()` 在 `/register` 前检查，禁用时返回结构化 403；
`/setup-status` 也返回 registration enabled 状态，前端据此隐藏/禁用注册入口。

当前 `dev` `/api/v1/auth/register` 始终：

1. rate-limit；
2. create regular user；
3. issue session；
4. auto-login。

这是确认缺少部署开关。建议落点：

- `deerflow.config.auth_config.LocalAuthConfig`；
- `config.example.yaml` 和文档；
- `routers/auth.py` register gate；
- setup/status response；
- login 页面；
- restart-required/live-reload 边界明确化。

默认值是否保持 `true` 是兼容性决策；关闭时 initialize/管理员显式建号路径不能一起被关掉。

## 9. Keep me signed in：移植前产品能力

`main@a028dfd5` 的 `resolve_session_cookie_policy()` 按用户意图和部署环境决定：

| 条件 | `max_age` |
| --- | --- |
| remember=false | `None`，浏览器 session cookie |
| remember=true + HTTPS | token lifetime |
| remember=true + localhost HTTP | token lifetime |
| remember=true + operator 显式允许公网 HTTP | token lifetime |
| remember=true + 普通公网 HTTP | `None` |

另有 `deerflow_session_persistent` cookie，让 OIDC redirect/callback 保留用户选择。

若移植到 `dev`：

- 登录/注册/OIDC state 增加 `remember_me`；
- cookie lifetime policy 可复用；
- JWT/session row 的过期时间仍必须一致；
- durable sid create/validate/revoke 完全保留；
- CSRF cookie lifetime 与 access cookie 对齐；
- localhost 规则和 trusted proxy HTTPS 判断必须使用现有 `is_secure_request()`。

不能复制 `main` 的 stateless JWT 实现或 SQLite repository。

## 10. 并发、失败和缓存语义

### 10.1 认证并发

- public login/register rate-limit 存 PostgreSQL，并在密码 hash/verify 前原子 admit；
- 成功登录只清对应 action/IP 的 counter；
- initialize 由数据库锁序列化；
- session 先持久后发 JWT；
- password change 通过 token_version + revoke-all 封闭并发旧会话。

### 10.2 项目授权并发

- Project/Membership side effect 统一 `FOR UPDATE`；
- `membership_version` 防止 remove/re-add 后旧 context 复活；
- repository scope 防止只凭资源 ID 越权；
- Worker 在真正副作用前再校验，不依赖 HTTP 请求时的快照。

### 10.3 前端缓存边界

项目私有前端 cache root 含 `accountId + projectId`，Provider 切换时 abort/清理旧 scope。
这只覆盖项目私有数据。Auth `/me`、login 状态等全局身份 cache 应在登录、退出、密码变更后
显式失效，但不能混进某个 Project query root。

## 11. 关键提交演化

| 提交 | 日期 | 最终行为 |
| --- | --- | --- |
| `1300c6d3` | 2026-07 | AuthorizationProvider 协议和配置骨架 |
| `10890e10` | 2026-07-17 | trusted Principal 上下文传播 |
| `a028dfd5` | 2026-07-18 | keep-me-signed-in cookie policy |
| `92c8f2f0` | 2026-07 | 内置 RBAC provider |
| `09e25b8a` | 2026-07-20 | 部署可关闭 local self-registration |
| `b5cc3a81` | 2026-07-25 | 邮箱全链大小写不敏感 |
| `6091ce75` | 2026-07-27 | Gateway route permissions 由 provider 派生 |
| `7857fa0c` | 2026-07 | tool assembly + execution 双层授权 |

这些提交属于 `main` 的 legacy global route/user storage 演化。可移植的是策略和行为，
不是 repository、owner_check 或 runtime context authority。

## 12. 确认缺失、风险与已替代

### 12.1 移植前已确认

- `dev` 邮箱查询/唯一索引大小写敏感；
- `dev` 没有关闭 local registration 的部署开关；
- `dev` 没有 remember-me 用户选择；
- `dev` 没有通用 AuthorizationProvider。

其中前三项已经在当前工作树按第 16 节的边界实现；通用 provider 仍取决于是否存在
Project capability 之外的真实策略需求，因此没有为了“看起来完整”而移植。

### 12.2 已替代或更强

- `dev` PostgreSQL durable sid session 强于 `main` stateless token-version；
- `dev` ProjectContext/PrivateWorkContext 强于 legacy user role + owner_check；
- `dev` resource scope 和 side-effect revalidation 已覆盖项目私有 TOCTOU；
- `dev` PostgreSQL rate-limit 可跨进程/Pod；
- `dev` strict error mapping 避免项目私有资源枚举。

### 12.3 移植前待产品/架构决定

- generic Authz provider 要保护哪些非项目或额外资源；
- RBAC 与 Project capability 的优先关系；建议任何 deny 都能收紧，不能扩大；
- remember-me 默认值；
- registration gate 默认 true 还是 false；
- 邮箱规范是 lowercase functional index 还是 `citext`。

当前工作树已经选择 remember-me 默认 `true`、registration gate 默认 `true` 和
lowercase functional index；generic provider 的真实资源与 deny 合并规则仍未定义，因此明确
不移植。

## 13. 禁止直接合并

- `main:backend/app/gateway/auth/repositories/sqlite.py`；
- `main` stateless `jwt.py` 覆盖 `dev` sid payload；
- legacy `gateway/authz.py` 的 `owner_check` 作为项目授权；
- 从 runtime config/context 构造项目 Principal；
- 把 generic role 当 Project membership；
- 用 system-admin 绕过 Project membership；
- 为邮箱索引新增 incremental migration；
- 注册 gate 绕过 initialize/受控 provisioning；
- remember-me 让 JWT 比 durable session row 活得更久。

## 14. 测试与契约

### 14.1 `main` 证据

- `backend/tests/test_authorization_route_permissions.py`
  - provider allow/deny、async、fail-open/closed、internal Principal；
- `backend/tests/test_auth.py`
  - case-insensitive lookup/create/update/OIDC collision；
- `backend/tests/test_local_registration_gate.py`
  - register 403、setup status、默认值；
- `backend/tests/test_auth_type_system.py`
  - remember-me、cookie、CSRF；
- Authz 包内测试
  - RBAC typo/null/deny-wins/unknown role、tool 双层 enforcement。

### 14.2 当前 `dev` 基础

- `backend/tests/test_auth_sessions.py`
- `backend/tests/test_auth_type_system.py`
- `backend/tests/test_auth_rate_limit.py`
- `backend/tests/test_auth.py`
- `backend/tests/test_project_context.py`
- `backend/tests/test_private_work_context.py`
- `backend/tests/test_private_run_authorization.py`
- `backend/tests/test_initialize_admin.py`
- PostgreSQL project isolation/integration gates。

## 15. 验证矩阵

| 场景 | 预期结果 |
| --- | --- |
| session row 写失败 | 不发 JWT/cookie，503 |
| 复制 token 后原浏览器 logout | 原浏览器清 cookie；复制 token 被 durable revoke 拒绝 |
| revoke 数据库失败 | 返回 503 且本地 cookie 仍删除 |
| 密码变更 | 所有旧 sid 与旧 token_version 失效，仅新 session 有效 |
| 登录/注册并发 rate limit | 跨进程共享计数，不超过窗口 |
| `User@X.com` 已存在后注册 `user@x.com` | 稳定重复账号错误，不新增行 |
| OIDC 与 local 邮箱仅大小写不同 | 命中同一确定账号或按明确 policy 拒绝，不建第二账号 |
| registration disabled | `/register` 403，initialize/管理员路径仍工作 |
| remember=false | access 与 CSRF 都是 session cookie |
| remember=true + HTTPS/localhost | cookie max-age 与 durable session expiry 对齐 |
| remember=true + 公网 HTTP | 除非 operator 显式允许，否则仍为 session cookie |
| Project outsider 请求已存在资源 | 404 |
| 当前成员缺 capability | 403 |
| membership remove/re-add | 旧 `membership_version` context 404 |
| client 伪造 role/project/capability | 被剥离，不能扩大权限 |
| Worker 准入后 membership 被撤销 | 下一 side-effect revalidation 失败 |
| generic provider deny tool | assembly 不可见，execution 仍再次拒绝 |
| generic provider allow、Project capability deny | 最终仍拒绝 |
| provider 异常且 fail-closed | 只收紧，不回退为 Project allow |
| system-admin 非项目成员 | 不能读取项目 private work |

通过这组矩阵后，才能认为 Auth、Project Authz 和可选通用策略三层没有被混成一个可伪造的
`role` 字段。

## 16. 当前工作树移植执行记录

### 16.1 实际可移植落点

本轮不是把 `main` 的认证目录整体复制到 `dev`，而是把四组可独立验证的行为落到
`dev` 现有 PostgreSQL session、ProjectContext 和前端 account/project cache 边界中：

1. **大小写不敏感的唯一邮箱身份**
   - 新增 `app.gateway.auth.email.normalize_email()`，统一执行 `strip + lowercase`；
   - `SQLUserRepository` 在 create、lookup、update 前规范化邮箱，lookup 使用
     `lower(users.email)`，并对可能存在的旧重复数据保持确定性顺序；
   - register、local login、change-password/change-email 和 OIDC provisioning 最终都经过
     同一 PostgreSQL repository；OIDC identity/admin 邮箱复用 canonical helper，allowed
     domain 也执行对应的 `strip + lowercase` 规范；项目邀请域原有的同语义比较继续保留；
   - ORM 与唯一 `full_schema.sql` 同步使用唯一 functional index
     `ix_users_email ON users (lower(email))`；
   - 并发 create/update 的数据库唯一冲突映射回稳定的重复邮箱错误，不向客户端暴露 SQL；
   - schema catalog signature 与 canonical digest 随完整 schema 更新。

2. **可关闭的本地自助注册**
   - `auth.local.allow_registration` 默认 `true`；
   - 关闭后 `POST /api/v1/auth/register` 在 rate-limit 和账号创建之前返回结构化
     `403 registration_disabled`；
   - `GET /api/v1/auth/setup-status` 的公开严格契约为
     `{needs_setup: boolean, registration_enabled: boolean}`；
   - setup-status 只缓存较稳定的 `needs_setup` 数据库结果，每次响应重新读取
     `registration_enabled`，配置从允许切为关闭时不会被旧 cache 隐藏；
   - Auth、Local、OIDC 和每个 OIDC provider 的 Pydantic 配置均
     `extra="forbid"`；安全字段拼错会在配置加载时失败，而不是退回默认允许；
   - first-admin `/initialize` 不受自助注册开关影响；OIDC 的受控 provisioning 也没有被错误地
     绑定到这个 local register gate；
   - 前端在 setup-status 检查中、不可用、响应畸形或明确关闭时一律不开放普通注册，并提供
     不把失败伪装成“可注册”的不可用/重试状态。

3. **保持登录的 cookie policy**
   - local login 表单发送 `remember_me`；register、initialize 发送同名 JSON boolean；
     change-password 可显式发送，省略时沿用当前 preference cookie；
   - OIDC login query 把选择写入签名 state，callback 使用已校验的 state 恢复 cookie policy；
   - `remember_me=false` 时 access、CSRF 和 preference cookie 都是 browser-session cookie；
   - `remember_me=true` 时，HTTPS 与 localhost HTTP 的 `Max-Age` 等于配置的 token lifetime；
     普通公网 HTTP 默认仍是 session cookie；
   - 只有 operator 显式设置
     `auth.local.allow_insecure_persistent_cookie=true`，公网明文 HTTP 才允许持久 cookie；
   - access cookie 仍为 `HttpOnly + SameSite=Lax`，CSRF 仍为 double-submit；middleware
     根据“本次响应是否实际签发 session cookie”统一重发 CSRF，因此 login、register、
     initialize、change-password 与 OIDC callback 的 secure/lifetime 都和 access policy 对齐；
   - logout 先撤销当前 PostgreSQL durable `sid`，并删除 access、CSRF、preference 三个
     cookie；即使 durable revoke 暂时失败并返回 503，也仍删除本浏览器 cookie；
   - remember-me 只决定浏览器是否持久保存 cookie，不替换 JWT 中的 `sid`、PostgreSQL
     session row、token-version 或密码变更后的 revoke-all。

4. **前端身份与项目授权回归边界**
   - User、setup-status 与 OIDC provider response 使用 strict Zod contract；
   - client 与 SSR `/auth/me` 都只有明确 401 才判定未认证并清除身份/account cache；
     网络错误、5xx、403 或畸形 200 被视为 availability failure；
   - `refreshUser()` 返回 `authenticated | unauthenticated | unavailable` 判别联合。OIDC
     callback 对 unavailable 显示可重试状态而不是伪报 SSO 失败；forced setup 也不会把瞬时
     refresh 故障误判为 authoritative logout；
   - `/workspace`、`/projects/*`、`/admin/*` 的未认证跳转统一保留经过校验的 pathname/query；
     client-side 跳转还保留不会发送到 server 的 fragment；auth probe/submit 有超时并在
     supersession/unmount 时 abort；
   - 浏览器 storage 只保存 remember preference 和可选邮箱，不保存密码、JWT、CSRF 或
     durable session 标识；
   - 项目私有授权继续完全由 server-issued ProjectContext、capability、owner scope 与
     side-effect revalidation 决定。outsider 404、当前成员缺 capability 403、客户端伪造
     role/project/capability 无效的语义没有被 generic role 替换。

### 16.2 明确未移植

以下 `main` 实现没有进入当前工作树：

- SQLite user repository 或 memory/sqlite persistence；
- 没有 durable `sid` 的 stateless JWT session；
- legacy `owner_check` 和全局 role 代替 Project membership；
- system-admin 自动绕过 Project membership；
- generic `AuthorizationProvider`、旧 Gateway route permission 或 tool RBAC。

最后一项当前没有一个 Project capability 之外的具体、可验证 policy 目标。为避免配置被
Pydantic 接受后实际忽略形成 fail-open 假象，配置 schema version 32 把旧顶层
`authorization:` 设为 tombstone：直接加载时失败，`make config-upgrade` 会删除该已不支持字段。
将来只有在先定义真实受保护资源、deny 合并规则、Worker 重校验和端到端测试后，才能单独引入
通用 provider；它只能收紧现有项目授权，不能扩大权限。

### 16.3 schema 和部署契约

- `config.example.yaml` 当前 `config_version` 为 32，并记录
  `auth.local.allow_registration` 与
  `auth.local.allow_insecure_persistent_cookie`；
- `lower(email)` 唯一索引属于完整 schema 变化。项目没有增量 migration，也不能在运行时
  `ALTER` 旧业务库；
- 已存在的本地或部署数据库必须换成新空目标后运行唯一入口 `make setup-db`。在完成空库重建前，
  旧数据库上的 Gateway schema readiness 失败是预期的 fail-closed 行为，不是可绕过警告。

### 16.4 当前自动化验证

当前工作树已经完成以下自动化验证：

- 修复后的完整后端：`7400 passed, 1012 skipped`；
- 固定 20 文件 M1–M7 真实 PostgreSQL gate：
  `270 passed, 0 failed, 0 skipped`；
- 新增模块 10 真实 PostgreSQL 聚焦测试：`7 passed, 152 deselected`；
- Auth 独立复审集合：`220 passed`；核心组合 `194 passed`；非 PostgreSQL 集合
  `94 passed`；
- 修复后的完整前端：`188 files, 1344 passed`；
- `pnpm check` 通过；
- `pnpm build` 通过，静态页面 `78/78`；
- `make check-db` 通过，schema marker 为 `full_schema_v1`；
- mixed-case register/login/duplicate、registration gate/setup-status、initialize
  独立性、remember cookie 三种 transport policy、logout 全 cookie 删除、OIDC state、
  ProjectContext、PrivateWorkContext 与 private Run authorization 均包含在上述门禁中；
- 真实 PostgreSQL baseline 用并发大小写变体 insert 验证 functional unique index 只准入一行；
- 相关后端文件 Ruff format/check 通过。

独立审查发现的 Auth config typo fail-open、change-password CSRF lifetime、setup-status
registration cache、OIDC canonicalization、SSR 403、`refreshUser` 状态混淆、前端错误码和
strict E2E mock 均已修复并补测试。前端结构化错误码现在包含 `registration_disabled` 与
`rate_limited`；E2E user/setup-status mock 也满足 strict public schema；审查指出的 5 个
`prefer-optional-chain` lint 错误已经清理。

独立后端和前端复核均未发现剩余 P0/P1/P2 阻塞项。

审查另记录一个既存初始化恢复风险：system-admin row 与 default project 不在同一事务；后者失败
会留下“已有 admin、无默认项目”的部分状态。它不是本轮从 `main` 移植的 session/Authz
回归，但仍应在后续初始化生命周期工作中定义正式恢复契约，不能把重复 `/initialize`
（该入口会因已有 admin 拒绝）当成恢复路径。该项被明确接受为当前模块的**非阻塞既存风险**：
本轮没有改写初始化事务边界，空库真实 setup 和浏览器 initialize 已成功，模块 10 的
canonical email、registration、session 与 Project Authz 验收不依赖失败恢复路径。后续若改动
初始化生命周期，必须先为这一场景定义可重复、可审计的恢复契约。

### 16.5 真实浏览器与权限边界验收

数据库已按 full-schema 生命周期删除并重建，随后启动完整 Gateway、Worker、Scheduler、
Frontend 与 Nginx 服务。真实浏览器和 HTTP/数据库交叉验收按顺序完成：

- `[x]` 浏览器创建首个管理员和默认项目；
- `[x]` 使用全大写邮箱登录成功，工作空间显示同一规范化小写账号；
- `[x]` 大小写变体重复注册显示稳定的重复邮箱错误；
- `[x]` 真实 login/initialize 页面显示并提交 remember 开关；
- `[x]` 临时关闭 registration 后，UI 不显示普通注册入口，direct API 返回结构化
  `403 registration_disabled`；恢复配置并重启后注册入口恢复；
- `[x]` 带 query 的私有工作空间在明确 401 后跳回登录并保留安全返回路径；
- `[x]` outsider 请求已存在项目返回 `404 PROJECT_NOT_FOUND`；
- `[x]` 通过邀请加入的 Viewer 不显示管理入口，修改项目返回
  `403 PROJECT_FORBIDDEN`；
- `[x]` 同一请求伪造 project role、capability 和 system role 请求头后仍返回
  `403 PROJECT_FORBIDDEN`；
- `[x]` 精确撤销测试账号 durable session 后，已有浏览器访问私有项目被送回登录页；
- `[x]` 同一会话完成四轮真实 DeepSeek V4 Pro 调用：第三轮正确读取前两轮结果；
  完整服务重启后第四轮仍返回
  `RESTART-CONTEXT-PASS | prior=CONTEXT-CHAIN-PASS`；
- `[x]` PostgreSQL 交叉检查四个 Run 均为 `success`，finalization 均为 `complete`，
  job 均为 `succeeded`，每轮 `llm_call_count=1`、`stream.end=1`。

实际证据保存在
[`evidence/10-auth-authz/`](evidence/10-auth-authz/README.md)：

- [`01-uppercase-login-workspace-redacted.jpg`](evidence/10-auth-authz/01-uppercase-login-workspace-redacted.jpg)；
- [`02-real-model-context-after-restart-redacted.jpg`](evidence/10-auth-authz/02-real-model-context-after-restart-redacted.jpg)；
- [`03-login-remember-ui.jpg`](evidence/10-auth-authz/03-login-remember-ui.jpg)。

access、CSRF、preference Cookie 的 session/Max-Age、change-password 切换和 logout
删除属于自动化 HTTP `Set-Cookie` 契约验证，未把浏览器内部 Cookie 存储读取伪装成截图证据。

模块 10 范围内的完整门禁、独立代码审查和真实浏览器验收均无阻塞，状态为**已完成**。
