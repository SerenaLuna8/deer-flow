# Task 5 Report — 成员、邀请和生命周期 API

## Status

DONE

## 实现摘要

- 实现成员 4 个、邀请 6 个、生命周期 2 个 endpoint，并挂载到 Gateway。
- 所有项目资源路由从认证身份生成 user ID/request ID，再通过不可变 `ProjectContext` 和 project-scoped repository 授权；restore 是 pending-deletion 特例，只使用认证 user ID 调用专用恢复 repository。
- 新增统一治理错误映射，覆盖专项规格第 8 节稳定 code、通用 message 和 request ID；跨项目成员/邀请保持 404 不可枚举。
- invitation create 只在一次性响应中返回 `/invite#token=...`；普通 invitation response、列表和 `mine` 均不含 token/hash。`mine` 只按当前认证账户规范化 email 查询仍可兑换邀请。
- `InvitationClaimSigner` 使用带固定 domain-separation label 的 SHA-256 从 Auth JWT secret 派生 AES-GCM key；每个 cookie 使用随机 12-byte nonce，AAD 绑定 cookie 名/版本，payload 严格只含 invitation UUID、token hash、`iat`、`exp`。输出是 opaque base64url(nonce+ciphertext+tag)。
- claim 对有效、无效和已限流 token 返回相同 200/body/同形状 cookie；无效路径使用随机 invitation UUID。cookie 固定十分钟、HttpOnly、SameSite=Lax、path `/api/project-invitations`，Secure 跟随 `is_secure_request`。
- redeem 已认证并把当前账户 email 交给 `InvitationService.redeem` 校验；成功、领域失败、限流和数据库失败响应都清除相同 path 的 claim cookie。
- 新增 PostgreSQL 共享失败限流表/repository，阈值 5 次/5 分钟。数据库只存 SHA-256 key；claim key 包含 action+可信客户端 IP，redeem key 再包含当前账户规范化 email。失败计数使用 PostgreSQL atomic upsert，并发测试确认不丢计数。
- 项目列表支持 `include_recoverable` 并返回 `deletion_effective_at`。

## RED

1. 首次运行 Task 5 目标套件：5 个收集错误，分别为缺失的三个路由、claim signer、PG rate-limit repository/model。
2. 安全自审把可读 signed JWT 提升为 opaque authenticated encryption 后，先改测试并运行：3 项失败，证明旧 JWT 不能满足 payload 不可读要求。
3. 补充跨站 claim 稳定错误测试：先观察到 403/plain detail，随后实现为 422 `PROJECT_VALIDATION_FAILED` + request ID。

## GREEN 与验证

- Task 5 目标 + PostgreSQL schema/rate-limit + project isolation：最终 26 passed。
- broader project governance + auth/CSRF 回归：136 passed。
- AES-GCM signer + invitation router 专项：11 passed；跨站错误专项：1 passed。
- 真实 PostgreSQL 使用 worktree `.env` 的 `DEER_FLOW_TEST_POSTGRES_ADMIN_URL`，仅在测试进程内映射为现有 fixture 的 `POSTGRES_TEST_URL`。
- Ruff format/check 和 `git diff --check`：通过。

## 文件

### Gateway/API

- `backend/app/gateway/auth/invitation_claim.py`
- `backend/app/gateway/auth/invitation_rate_limit.py`
- `backend/app/gateway/routers/project_governance.py`
- `backend/app/gateway/routers/project_members.py`
- `backend/app/gateway/routers/project_invitations.py`
- `backend/app/gateway/routers/project_lifecycle.py`
- `backend/app/gateway/routers/projects.py`
- `backend/app/gateway/routers/__init__.py`
- `backend/app/gateway/app.py`
- `backend/app/gateway/auth_middleware.py`
- `backend/app/gateway/csrf_middleware.py`

### Domain/persistence

- `backend/app/projects/invitation_repository.py`
- `backend/app/projects/invitation_service.py`
- `backend/packages/harness/deerflow/persistence/projects/invitation_rate_limit_model.py`
- `backend/packages/harness/deerflow/persistence/projects/__init__.py`
- `backend/packages/harness/deerflow/persistence/models/__init__.py`
- `backend/packages/harness/deerflow/persistence/migrations/versions/0006_project_governance.py`

### Tests/docs

- `backend/tests/test_project_members_router.py`
- `backend/tests/test_project_invitations_router.py`
- `backend/tests/test_project_lifecycle_router.py`
- `backend/tests/test_project_invitation_claim.py`
- `backend/tests/test_project_invitation_rate_limit_postgres.py`
- `backend/tests/test_project_governance_schema_postgres.py`
- `backend/AGENTS.md`
- `docs/superpowers/plans/2026-07-12-project-governance-m2.md`
- `docs/superpowers/specs/2026-07-12-project-governance-m2-design.md`

## 自审

- 逐项核对专项规格第 5/7/8/9 节和总体设计第 15 节。
- 确认所有 project-scoped member/invitation/deletion route 都通过认证 identity + `resolve_project_context`，没有从 body/query 接受用户或项目作用域。
- 确认 claim 是唯一公共 endpoint；仍执行同源/允许 origin 校验，AuthMiddleware 和 CSRF 只对该精确 path 放行。
- 确认 opaque cookie 不含明文 token、email、项目信息，且错误 key、篡改、过期、额外字段均统一失败。
- 确认 `mine`/普通 response model 无 token/hash，created token dataclass 字段继续 `repr=False`。
- 确认 limiter ORM/migration 只存 SHA-256 key，atomic upsert 的 12 并发 session 测试得到精确 failure_count=12。
- 确认 redeem 每个 route-owned 成功/失败响应均删除同名、同 path、同 Secure/SameSite 属性的 cookie。
- 确认未实现任何前端 Task 6+ 内容，也未暂存共享 worktree 中其他 task 的报告/审查文件。

## 顾虑

- 仅有现存 TestClient/httpx 与 per-request cookies deprecation warning；本任务没有新增功能性失败。
- 失败限流表按 hash key 保留未再次命中的过期行；当前最小实现会在该 key 再次失败时原子重置窗口。若未来遭遇高基数来源导致表增长，应增加独立的过期行维护任务，不应在请求热路径做全表清理。
