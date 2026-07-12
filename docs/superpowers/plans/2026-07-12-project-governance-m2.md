# M2 工作空间与项目治理实施计划

> **面向执行代理：** REQUIRED SUB-SKILL: 使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`，逐项执行本计划。所有步骤使用复选框跟踪。

**目标：** 交付无项目级侧栏的多项目工作空间、绑定 `ProjectContext` 的项目壳层，以及成员、邀请、角色、退出、移除、删除和恢复治理闭环。

**架构：** PostgreSQL 通过约束、行锁和版本字段保证成员与项目生命周期不变量；`app.projects` 按成员、邀请和生命周期拆分 repository/service，Gateway 路由只负责认证、输入验证和错误映射。前端保留账户作用域 QueryClient，在 `/workspace` 提供全局项目入口，在 `/projects/{slug}` 内提供项目上下文和项目级导航。

**技术栈：** Python 3.12+、FastAPI、Pydantic、SQLAlchemy Async、Alembic、asyncpg、PostgreSQL、Next.js 16、React 19、TypeScript、TanStack Query、Rstest、Playwright。

## 全局约束

- PostgreSQL 是唯一运行数据库；所有持久化测试使用临时 `deerflow_test_*` 数据库。
- 不使用 PostgreSQL RLS；授权只依赖认证身份、不可变 `ProjectContext`、作用域 repository 和数据库约束。
- 应用数据库连接不得使用 superuser；只有显式 setup/migration 脚本属于 trusted operations。
- 前端不能从角色推导能力，所有治理可见性使用服务端返回的 capabilities。
- 邀请 token 只在创建响应返回一次；数据库、日志、URL query、localStorage 和 sessionStorage 都不得保存明文。
- 任何成员变更都递增 membership `version` 和 project `membership_version`。
- 任何事务都不能使 active 项目失去最后一名 active Admin。
- M2 不开放项目私有工作，不执行成员私有数据或项目数据的物理清除。
- 后端按 TDD 实施；每个任务先运行目标失败测试，再写最小实现。
- 每个任务只修改列出的相关文件，不混入无关重构。

---

## 文件与模块结构

### 后端持久化

- `backend/packages/harness/deerflow/persistence/projects/model.py`：扩展 project 和 membership 生命周期字段。
- `backend/packages/harness/deerflow/persistence/projects/invitation_model.py`：邀请 ORM 和约束。
- `backend/packages/harness/deerflow/persistence/migrations/versions/0006_project_governance.py`：M2 schema migration。

### 后端业务域

- `backend/app/projects/membership_models.py`：成员命令、视图和分页值对象。
- `backend/app/projects/membership_repository.py`：成员查询、锁和状态写入。
- `backend/app/projects/membership_service.py`：角色、最后一名 Admin、退出和移除不变量。
- `backend/app/projects/invitation_models.py`：邀请命令和一次性创建结果。
- `backend/app/projects/invitation_repository.py`：邀请 hash 查询、行锁和兑换写入。
- `backend/app/projects/invitation_service.py`：token、邮箱、过期、撤销和兑换规则。
- `backend/app/gateway/auth/invitation_claim.py`：短期签名 claim cookie 的编码和验证。
- `backend/app/projects/lifecycle_repository.py`：项目删除/恢复的锁和状态写入。
- `backend/app/projects/lifecycle_service.py`：30 天窗口和状态规则。
- `backend/app/gateway/routers/project_members.py`：成员 API。
- `backend/app/gateway/routers/project_invitations.py`：邀请 API。
- `backend/app/gateway/routers/project_lifecycle.py`：删除恢复 API。

### 前端

- `frontend/src/app/workspace/page.tsx`：非静态模式直接渲染工作空间。
- `frontend/src/app/workspace/projects/page.tsx`：兼容重定向。
- `frontend/src/app/workspace/workspace-route-frame.tsx`：全局工作空间与旧版兼容壳层分流。
- `frontend/src/app/projects/[project_slug]/layout.tsx`：项目壳层入口。
- `frontend/src/components/projects/project-context.tsx`：项目解析、enter 和上下文。
- `frontend/src/components/projects/project-shell.tsx`：项目级侧栏和页面框架。
- `frontend/src/components/projects/members/`：成员与邀请界面。
- `frontend/src/components/projects/settings/`：项目删除恢复界面。
- `frontend/src/app/invite/page.tsx`：邀请兑换入口。
- `frontend/src/core/projects/`：成员、邀请、生命周期 contract、API、query key 和 hooks。

---

### Task 1（任务 1）：M2 PostgreSQL schema 与 migration

**文件：**

- 修改：`backend/packages/harness/deerflow/persistence/projects/model.py`
- 新建：`backend/packages/harness/deerflow/persistence/projects/invitation_model.py`
- 修改：`backend/packages/harness/deerflow/persistence/models/__init__.py`
- 新建：`backend/packages/harness/deerflow/persistence/migrations/versions/0006_project_governance.py`
- 修改：`backend/tests/test_project_schema_postgres.py`
- 新建：`backend/tests/test_project_governance_schema_postgres.py`

**接口：**

- 产生：`ProjectInvitationRow`。
- 扩展：`ProjectRow.status` 为 `active|pending_deletion`。
- 扩展：`ProjectMembershipRow.status` 为 `active|left|removed`。
- 后续依赖：任务 2 至任务 5 只通过这些 ORM 写入治理状态。

- [ ] **步骤 1：编写 schema 失败测试**

```python
async def test_m2_schema_has_governance_constraints(migrated_postgres_url):
    async with create_async_engine(migrated_postgres_url).connect() as conn:
        tables = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
        invitation_indexes = await conn.run_sync(
            lambda sync: inspect(sync).get_indexes("project_invitations")
        )
    assert "project_invitations" in tables
    assert "uq_project_invitations_pending_email" in {
        index["name"] for index in invitation_indexes
    }
```

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && uv run pytest tests/test_project_governance_schema_postgres.py -q`

预期：失败，提示 `project_invitations` 不存在。

- [ ] **步骤 3：扩展 ORM 并新增邀请模型**

```python
class ProjectInvitationRow(Base):
    __tablename__ = "project_invitations"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    invited_email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    created_by_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    redeemed_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
```

migration 使用 partial unique index：

```python
op.create_index(
    "uq_project_invitations_pending_email",
    "project_invitations",
    ["project_id", "invited_email"],
    unique=True,
    postgresql_where=sa.text("status = 'pending'"),
)
```

- [ ] **步骤 4：验证空库与 M1 数据库升级**

运行：`cd backend && uv run pytest tests/test_project_schema_postgres.py tests/test_project_governance_schema_postgres.py -q`

预期：全部通过；Alembic head 为 `0006_project_governance`。

- [ ] **步骤 5：提交**

```bash
git add backend/packages/harness/deerflow/persistence/projects backend/packages/harness/deerflow/persistence/models/__init__.py backend/packages/harness/deerflow/persistence/migrations/versions/0006_project_governance.py backend/tests/test_project_schema_postgres.py backend/tests/test_project_governance_schema_postgres.py
git commit -m "feat(projects): add M2 governance schema"
```

### Task 2（任务 2）：成员 repository 与业务不变量

**文件：**

- 新建：`backend/app/projects/membership_models.py`
- 新建：`backend/app/projects/membership_repository.py`
- 新建：`backend/app/projects/membership_service.py`
- 修改：`backend/app/projects/errors.py`
- 修改：`backend/app/projects/context.py`
- 新建：`backend/tests/test_project_membership_service.py`
- 新建：`backend/tests/test_project_membership_repository_postgres.py`

**接口：**

- 产生：`MembershipView(membership_id, user_id, account_email, role, status, version, joined_at)`。
- 产生：`MembershipService.list_members(context)`。
- 产生：`MembershipService.change_role(context, membership_id, role, expected_version)`。
- 产生：`MembershipService.remove(context, membership_id, expected_version)`。
- 产生：`MembershipService.leave(context, expected_version)`。

- [ ] **步骤 1：编写最后一名 Admin 和跨项目失败测试**

```python
async def test_last_admin_cannot_leave(member_service, admin_context):
    with pytest.raises(ProjectLastAdmin):
        await member_service.leave(admin_context, expected_version=1)

async def test_cross_project_membership_is_not_found(member_service, admin_context, other_membership_id):
    with pytest.raises(ProjectNotFound):
        await member_service.change_role(
            admin_context, other_membership_id, ProjectRole.VIEWER, expected_version=1
        )
```

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && uv run pytest tests/test_project_membership_service.py tests/test_project_membership_repository_postgres.py -q`

预期：测试收集失败，membership 模块不存在。

- [ ] **步骤 3：实现作用域锁与服务规则**

```python
class MembershipService:
    async def change_role(
        self,
        context: ProjectContext,
        membership_id: uuid.UUID,
        role: ProjectRole,
        expected_version: int,
    ) -> MembershipView:
        context.require(Capability.PROJECT_MEMBERS_MANAGE)
        async with self.repository.transaction():
            project, target = await self.repository.lock_project_and_member(
                context.project_id, membership_id
            )
            if target.version != expected_version:
                raise ProjectMembershipVersionConflict()
            if target.role == ProjectRole.ADMIN and role != ProjectRole.ADMIN:
                await self.repository.require_another_active_admin(project.id, target.id)
            return await self.repository.set_role(project, target, role)
```

`remove` 和 `leave` 在同一锁内写入 `ended_at`、`retention_until`、`ended_by_user_id` 和 `end_reason`，并递增两个 version。

- [ ] **步骤 4：运行成员测试**

运行：`cd backend && uv run pytest tests/test_project_membership_service.py tests/test_project_membership_repository_postgres.py tests/test_project_context.py -q`

预期：全部通过；left/removed membership 无法解析 `ProjectContext`。

- [ ] **步骤 5：提交**

```bash
git add backend/app/projects backend/tests/test_project_membership_service.py backend/tests/test_project_membership_repository_postgres.py backend/tests/test_project_context.py
git commit -m "feat(projects): enforce membership lifecycle invariants"
```

### Task 3（任务 3）：邀请创建、撤销和一次性兑换

**文件：**

- 新建：`backend/app/projects/invitation_models.py`
- 新建：`backend/app/projects/invitation_repository.py`
- 新建：`backend/app/projects/invitation_service.py`
- 新建：`backend/tests/test_project_invitation_service.py`
- 新建：`backend/tests/test_project_invitation_repository_postgres.py`

**接口：**

- 产生：`InvitationService.create(context, email, role, now) -> CreatedInvitation`。
- 产生：`InvitationService.revoke(context, invitation_id, expected_version, now)`。
- 产生：`InvitationService.claim(token, now) -> InvitationClaim`。
- 产生：`InvitationService.redeem(user_id, user_email, claim, now) -> RedeemedInvitation`。

- [ ] **步骤 1：编写 token、过期和并发测试**

```python
async def test_create_returns_plaintext_once_and_persists_only_hash(invitation_service, admin_context):
    created = await invitation_service.create(
        admin_context, "member@example.com", ProjectRole.EDITOR, NOW
    )
    assert created.token
    stored = await invitation_service.repository.get(created.invitation.id)
    assert stored.token_hash == hashlib.sha256(created.token.encode()).hexdigest()
    assert created.token not in repr(stored)

async def test_expired_invitation_cannot_be_redeemed(invitation_service, invite):
    with pytest.raises(ProjectInvitationInvalid):
        claim = await invitation_service.claim(invite.token, invite.created_at)
        await invitation_service.redeem(USER_ID, invite.email, claim, invite.expires_at)
```

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && uv run pytest tests/test_project_invitation_service.py tests/test_project_invitation_repository_postgres.py -q`

预期：失败，邀请 service/repository 不存在。

- [ ] **步骤 3：实现 hash 和锁定兑换**

```python
def hash_invitation_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

class InvitationService:
    async def create(self, context, email, role, now):
        context.require(Capability.PROJECT_MEMBERS_MANAGE)
        if role is ProjectRole.ADMIN:
            raise ProjectValidationFailed("invalid_invitation_role")
        token = secrets.token_urlsafe(32)
        row = await self.repository.create(
            context, normalize_email(email), role, hash_invitation_token(token), now + timedelta(days=7)
        )
        return CreatedInvitation(invitation=row, token=token)
```

`redeem` 使用 `SELECT ... FOR UPDATE`，校验 email、状态和 `expires_at > now`，然后创建或重新激活 membership，并在同一事务标记 redeemed。
创建同项目同邮箱邀请时先锁定 project，把已经到期的 pending 邀请标记为 expired，再依赖 partial unique index 创建新邀请。

- [ ] **步骤 4：运行邀请测试**

运行：`cd backend && uv run pytest tests/test_project_invitation_service.py tests/test_project_invitation_repository_postgres.py -q`

预期：全部通过；并发兑换仅一项成功，数据库只有一条 membership。

- [ ] **步骤 5：提交**

```bash
git add backend/app/projects/invitation_models.py backend/app/projects/invitation_repository.py backend/app/projects/invitation_service.py backend/tests/test_project_invitation_service.py backend/tests/test_project_invitation_repository_postgres.py
git commit -m "feat(projects): add secure project invitations"
```

### Task 4（任务 4）：项目删除恢复生命周期

**文件：**

- 新建：`backend/app/projects/lifecycle_repository.py`
- 新建：`backend/app/projects/lifecycle_service.py`
- 修改：`backend/app/projects/repository.py`
- 修改：`backend/app/projects/models.py`
- 新建：`backend/tests/test_project_lifecycle_service.py`
- 新建：`backend/tests/test_project_lifecycle_repository_postgres.py`

**接口：**

- 产生：`ProjectLifecycleService.request_deletion(context, now)`。
- 产生：`ProjectLifecycleService.restore(user_id, project_id, request_id, now)`。
- 扩展：`ProjectService.list(..., include_recoverable: bool)`。

- [ ] **步骤 1：编写删除窗口失败测试**

```python
async def test_pending_project_cannot_resolve_context(lifecycle_service, admin_context, session):
    await lifecycle_service.request_deletion(admin_context, NOW)
    with pytest.raises(ProjectNotFound):
        await resolve_project_context(session, admin_context.user_id, admin_context.project_id, "r2")

async def test_restore_after_deadline_is_rejected(lifecycle_service, pending_project):
    with pytest.raises(ProjectDeletionStateConflict):
        await lifecycle_service.restore(
            pending_project.admin_id,
            pending_project.project_id,
            "r3",
            pending_project.effective_at,
        )
```

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && uv run pytest tests/test_project_lifecycle_service.py tests/test_project_lifecycle_repository_postgres.py -q`

预期：失败，lifecycle 模块不存在。

- [ ] **步骤 3：实现状态转换和恢复解析器**

```python
class ProjectLifecycleService:
    async def request_deletion(self, context: ProjectContext, now: datetime) -> ProjectView:
        context.require(Capability.PROJECT_LIFECYCLE_MANAGE)
        return await self.repository.mark_pending(
            context,
            requested_at=now,
            effective_at=now + timedelta(days=30),
        )

    async def restore(self, user_id, project_id, request_id, now):
        project = await self.repository.lock_recoverable_admin_project(user_id, project_id)
        if project.deletion_effective_at is None or now >= project.deletion_effective_at:
            raise ProjectDeletionStateConflict()
        return await self.repository.restore(project, request_id)
```

- [ ] **步骤 4：运行生命周期测试**

运行：`cd backend && uv run pytest tests/test_project_lifecycle_service.py tests/test_project_lifecycle_repository_postgres.py tests/test_project_repository_postgres.py -q`

预期：全部通过；pending 项目只对可恢复 Admin 出现在 recoverable 列表。

- [ ] **步骤 5：提交**

```bash
git add backend/app/projects backend/tests/test_project_lifecycle_service.py backend/tests/test_project_lifecycle_repository_postgres.py backend/tests/test_project_repository_postgres.py
git commit -m "feat(projects): add deletion recovery lifecycle"
```

### Task 5（任务 5）：成员、邀请和生命周期 API

**文件：**

- 新建：`backend/app/gateway/routers/project_members.py`
- 新建：`backend/app/gateway/routers/project_invitations.py`
- 新建：`backend/app/gateway/routers/project_lifecycle.py`
- 新建：`backend/app/gateway/auth/invitation_claim.py`
- 修改：`backend/app/gateway/routers/__init__.py`
- 修改：`backend/app/gateway/app.py`
- 修改：`backend/app/gateway/routers/projects.py`
- 新建：`backend/tests/test_project_members_router.py`
- 新建：`backend/tests/test_project_invitations_router.py`
- 新建：`backend/tests/test_project_lifecycle_router.py`
- 新建：`backend/tests/test_project_invitation_claim.py`

**接口：** 实现专项规格第 7 节全部 endpoint 和第 8 节稳定错误码。
- 产生：`InvitationClaimSigner.issue(claim, now) -> str` 和 `InvitationClaimSigner.verify(cookie, now) -> InvitationClaim`。

- [ ] **步骤 1：编写 API 授权和错误映射测试**

```python
def test_editor_cannot_create_invitation(editor_client, project_id):
    response = editor_client.post(
        f"/api/projects/{project_id}/invitations",
        json={"email": "new@example.com", "role": "viewer"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "PROJECT_MEMBERSHIP_FORBIDDEN"

def test_cross_project_member_patch_is_404(admin_client, project_id, other_membership_id):
    response = admin_client.patch(
        f"/api/projects/{project_id}/members/{other_membership_id}",
        json={"role": "viewer", "version": 1},
    )
    assert response.status_code == 404
```

- [ ] **步骤 2：运行路由测试确认失败**

运行：`cd backend && uv run pytest tests/test_project_members_router.py tests/test_project_invitations_router.py tests/test_project_lifecycle_router.py tests/test_project_invitation_claim.py -q`

预期：404 或测试收集失败，因为新路由尚未挂载。

- [ ] **步骤 3：实现薄路由和统一错误映射**

```python
@router.patch("/{project_id}/members/{membership_id}", response_model=MembershipResponse)
async def patch_member(project_id, membership_id, body, identity=Depends(authenticated_project_identity), session=Depends(project_session)):
    context = await resolve_project_context(session, identity[0], project_id, identity[1])
    service = MembershipService(MembershipRepository(session))
    return membership_response(
        await service.change_role(context, membership_id, body.role, body.version)
    )
```

邀请创建响应包含 `invite_url_fragment` 所需 token，但日志对象和普通 invitation response 不包含 token。
`POST /api/project-invitations/claim` 不要求登录，使用通用响应并设置十分钟有效的签名 `HttpOnly` claim cookie；`redeem` 要求登录、验证 cookie、完成兑换后清除 cookie。`GET /api/project-invitations/mine` 只按当前账户规范化 email 返回 invitation 元数据，不返回 token hash。

- [ ] **步骤 4：运行 API 和隔离测试**

运行：`cd backend && uv run pytest tests/test_project_members_router.py tests/test_project_invitations_router.py tests/test_project_lifecycle_router.py tests/test_project_invitation_claim.py tests/integration/test_project_isolation_postgres.py -q`

预期：全部通过。

- [ ] **步骤 5：提交**

```bash
git add backend/app/gateway backend/tests/test_project_members_router.py backend/tests/test_project_invitations_router.py backend/tests/test_project_lifecycle_router.py backend/tests/test_project_invitation_claim.py backend/tests/integration/test_project_isolation_postgres.py
git commit -m "feat(api): expose project governance endpoints"
```

### Task 6（任务 6）：登录后工作空间和旧版壳层分流

**文件：**

- 修改：`frontend/src/app/workspace/layout.tsx`
- 修改：`frontend/src/app/workspace/page.tsx`
- 修改：`frontend/src/app/workspace/projects/page.tsx`
- 新建：`frontend/src/app/workspace/workspace-route-frame.tsx`
- 修改：`frontend/src/components/projects/project-workbench.tsx`
- 修改：`frontend/src/components/projects/project-workbench-page.tsx`
- 修改：`frontend/src/core/projects/features.ts`
- 新建：`frontend/tests/unit/components/workspace/workspace-route-frame.test.tsx`
- 修改：`frontend/tests/e2e/projects.spec.ts`
- 修改：`frontend/tests/e2e/sidebar.spec.ts`

**接口：**

- `/workspace`：项目优先模式直接渲染 `ProjectWorkbenchPage`。
- `/workspace/projects`：重定向 `/workspace`。
- `WorkspaceRouteFrame`：只对旧版兼容路径包装 `WorkspaceContent`。

- [ ] **步骤 1：编写无侧栏工作空间失败测试**

```ts
test("workspace shows project cards without project navigation", async ({ page }) => {
  await page.goto("/workspace");
  await expect(page.getByTestId("project-workbench")).toBeVisible();
  await expect(page.getByRole("link", { name: "Agents" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Skills" })).toHaveCount(0);
});
```

- [ ] **步骤 2：运行测试确认失败**

运行：`cd frontend && pnpm exec playwright test tests/e2e/projects.spec.ts --grep "without project navigation"`

预期：失败，当前 `/workspace` 重定向且工作台仍处于旧侧栏壳层。

- [ ] **步骤 3：实现路由级壳层分流**

```tsx
export function WorkspaceRouteFrame({ children }: PropsWithChildren) {
  const pathname = usePathname();
  if (pathname === "/workspace" || pathname === "/workspace/projects") {
    return children;
  }
  return <WorkspaceContent>{children}</WorkspaceContent>;
}
```

非静态 `workspace/page.tsx` 返回 `<ProjectWorkbenchPage />`；静态模式保留 demo 路由。兼容页使用 `redirect("/workspace")`。

- [ ] **步骤 4：运行工作空间测试**

运行：`cd frontend && pnpm test -- --run && pnpm exec playwright test tests/e2e/projects.spec.ts tests/e2e/sidebar.spec.ts --workers=1`

预期：全部通过；静态 demo 不请求 `/api/projects`。

- [ ] **步骤 5：提交**

```bash
git add frontend/src/app/workspace frontend/src/components/projects frontend/src/core/projects/features.ts frontend/tests/unit/components/workspace frontend/tests/e2e/projects.spec.ts frontend/tests/e2e/sidebar.spec.ts
git commit -m "feat(frontend): make workspace the project entry"
```

### Task 7（任务 7）：绑定 ProjectContext 的项目壳层

**文件：**

- 新建：`frontend/src/app/projects/[project_slug]/layout.tsx`
- 新建：`frontend/src/components/projects/project-context.tsx`
- 新建：`frontend/src/components/projects/project-shell.tsx`
- 新建：`frontend/src/components/projects/project-nav.tsx`
- 修改：`frontend/src/components/projects/project-home-loader.tsx`
- 修改：`frontend/src/components/projects/project-home.tsx`
- 修改：`frontend/src/components/projects/project-header.tsx`
- 新建：`frontend/tests/unit/components/projects/project-context.test.tsx`
- 新建：`frontend/tests/unit/components/projects/project-shell.test.tsx`
- 修改：`frontend/tests/e2e/projects.spec.ts`

**接口：**

- 产生：`ProjectContextProvider` 和 `useCurrentProject()`。
- 产生：`useEnteredProjectBySlug(userId, slug) -> ProjectEntryState`，状态为 `loading|ready|error`。
- 项目菜单：概览、成员与邀请、项目设置、返回工作空间。
- 页面不重复调用 slug 解析和 enter mutation。

- [ ] **步骤 1：编写项目菜单和 stale identity 失败测试**

```tsx
it("renders only M2 project navigation from server capabilities", () => {
  render(<ProjectShell project={adminProject}>{children}</ProjectShell>);
  expect(screen.getByRole("link", { name: "项目概览" })).toBeVisible();
  expect(screen.getByRole("link", { name: "成员与邀请" })).toBeVisible();
  expect(screen.queryByRole("link", { name: "Agent" })).toBeNull();
});
```

- [ ] **步骤 2：运行测试确认失败**

运行：`cd frontend && pnpm test -- --run project-shell project-context`

预期：失败，新组件不存在。

- [ ] **步骤 3：实现单一项目上下文所有者**

```tsx
const CurrentProjectContext = createContext<Project | null>(null);

export function ProjectContextProvider({ slug, children }: Props) {
  const { user } = useAuth();
  const entry = useEnteredProjectBySlug(user?.id, slug);
  if (entry.status === "error") {
    return <main role="alert">无法进入项目</main>;
  }
  if (entry.status === "loading") {
    return <div data-testid="project-shell-loading" aria-busy="true" />;
  }
  return (
    <CurrentProjectContext value={entry.project}>
      <ProjectShell project={entry.project}>{children}</ProjectShell>
    </CurrentProjectContext>
  );
}
```

- [ ] **步骤 4：运行项目壳层测试**

运行：`cd frontend && pnpm test -- --run project-shell project-context project-home-state && pnpm exec playwright test tests/e2e/projects.spec.ts --workers=1`

预期：全部通过；切换 slug 时旧项目不会短暂渲染。

- [ ] **步骤 5：提交**

```bash
git add frontend/src/app/projects frontend/src/components/projects frontend/tests/unit/components/projects frontend/tests/e2e/projects.spec.ts
git commit -m "feat(frontend): add project-scoped navigation shell"
```

### Task 8（任务 8）：成员、邀请、兑换和删除恢复界面

**文件：**

- 修改：`frontend/src/core/projects/types.ts`
- 修改：`frontend/src/core/projects/api.ts`
- 修改：`frontend/src/core/projects/query-keys.ts`
- 修改：`frontend/src/core/projects/hooks.ts`
- 新建：`frontend/src/app/projects/[project_slug]/members/page.tsx`
- 新建：`frontend/src/app/projects/[project_slug]/settings/page.tsx`
- 新建：`frontend/src/app/invite/layout.tsx`
- 新建：`frontend/src/app/invite/page.tsx`
- 新建：`frontend/src/components/projects/members/project-members-page.tsx`
- 新建：`frontend/src/components/projects/members/create-invitation-dialog.tsx`
- 新建：`frontend/src/components/projects/members/member-role-dialog.tsx`
- 新建：`frontend/src/components/projects/settings/project-lifecycle-panel.tsx`
- 新建：`frontend/src/components/projects/invitation-redemption.tsx`
- 新建：`frontend/src/components/projects/workspace-invitations.tsx`
- 新建：`frontend/src/components/projects/workspace-recovery-section.tsx`
- 新建：`frontend/tests/unit/core/projects/governance-api.test.ts`
- 新建：`frontend/tests/unit/core/projects/governance-hooks.test.ts`
- 新建：`frontend/tests/e2e/project-governance.spec.ts`

**接口：** 实现专项规格第 7、8、10 节前端 contract 和交互；工作空间显示当前账户邮箱匹配的邀请，以及当前账户作为 Admin 可恢复的 pending_deletion 项目。
- `invite/layout.tsx` 对未登录结果不重定向，挂载可表示匿名身份的 AuthProvider；setup-required 和 gateway-unavailable 继续使用现有统一处理。

- [ ] **步骤 1：编写 fragment 安全和治理流程失败测试**

```ts
test("claims fragment token without persisting or retaining it in URL", async ({ page }) => {
  await page.goto("/invite#token=plain-secret-token");
  await expect.poll(() => page.url()).not.toContain("plain-secret-token");
  expect(await page.evaluate(() => localStorage.getItem("invitation-token"))).toBeNull();
  await expect(page.getByText("请登录后接受邀请")).toBeVisible();
});

test("workspace separates invitations and recoverable projects from active projects", async ({ page }) => {
  await page.goto("/workspace");
  await expect(page.getByRole("region", { name: "待接受邀请" })).toBeVisible();
  await expect(page.getByRole("region", { name: "可恢复项目" })).toBeVisible();
  await expect(page.getByTestId("project-grid").getByText("待删除项目")).toHaveCount(0);
});
```

- [ ] **步骤 2：运行测试确认失败**

运行：`cd frontend && pnpm exec playwright test tests/e2e/project-governance.spec.ts --workers=1`

预期：失败，邀请页和治理页面不存在。

- [ ] **步骤 3：实现 contract、hooks 和页面**

```tsx
useEffect(() => {
  const token = new URLSearchParams(window.location.hash.slice(1)).get("token");
  window.history.replaceState(null, "", window.location.pathname);
  if (token) claim.mutate({ token });
}, [claim]);
```

claim 成功且未登录时跳转 `/login?next=/invite`；已登录或登录返回后调用无 body 的 redeem API。mutation 成功后统一取消并失效：`projectKeys.workspace(userId)`、`projectKeys.detail(userId, projectId)`、`projectKeys.members(userId, projectId)` 和 `projectKeys.invitations(userId, projectId)`。退出或请求删除当前项目后使用 `router.replace("/workspace")`。

- [ ] **步骤 4：运行前端治理测试**

运行：`cd frontend && pnpm test -- --run && pnpm exec playwright test tests/e2e/project-governance.spec.ts tests/e2e/projects.spec.ts --workers=1 && pnpm check`

预期：全部通过；页面不从 role 推导能力，token 不出现在 storage 或 URL。

- [ ] **步骤 5：提交**

```bash
git add frontend/src/core/projects frontend/src/app/projects frontend/src/app/invite frontend/src/components/projects frontend/tests/unit/core/projects frontend/tests/e2e/project-governance.spec.ts frontend/tests/e2e/projects.spec.ts
git commit -m "feat(frontend): add project governance workflows"
```

### Task 9（任务 9）：M2 隔离门禁、文档和里程碑证据

**文件：**

- 新建：`backend/tests/integration/test_m2_project_governance_postgres.py`
- 修改：`.github/workflows/project-foundation-postgres-tests.yml`
- 修改：`README.md`
- 修改：`README_zh.md`
- 修改：`AGENTS.md`
- 修改：`backend/AGENTS.md`
- 修改：`frontend/AGENTS.md`
- 修改：`docs/superpowers/specs/2026-07-12-project-first-saas-design.md`
- 修改：`docs/superpowers/specs/2026-07-12-project-governance-m2-design.md`

**接口：** CI 在真实 PostgreSQL 上硬失败；M2 完成前总体设计状态保持“未开始”或“进行中”。

- [ ] **步骤 1：新增两项目并发隔离矩阵**

```python
@pytest.mark.anyio
async def test_m2_cross_project_and_last_admin_matrix(m2_postgres_fixture):
    result = await m2_postgres_fixture.exercise_matrix()
    assert result.cross_project_reads == 0
    assert result.cross_project_mutations == 0
    assert result.concurrent_invitation_successes == 1
    assert result.last_admin_violations == 0
```

- [ ] **步骤 2：把 M2 测试加入 PostgreSQL workflow**

workflow 命令固定为：

```yaml
- name: Run M1 and M2 PostgreSQL isolation gates
  run: >-
    uv run pytest
    tests/integration/test_m1_postgres_cutover.py
    tests/integration/test_project_isolation_postgres.py
    tests/integration/test_m2_project_governance_postgres.py
    -q
```

- [ ] **步骤 3：更新架构与用户文档**

文档必须明确：登录后 `/workspace` 无项目级侧栏；进入 `/projects/{slug}` 后才有项目菜单；邀请无邮件；M2 不执行私有数据或项目数据物理清除；M2 仍不可作为完整 SaaS 发布。

- [ ] **步骤 4：执行最终门禁**

运行：

```bash
cd backend && make lint
cd backend && make test
cd frontend && pnpm check
cd frontend && pnpm test -- --run
cd frontend && pnpm exec playwright test --workers=1
```

预期：后端 0 failure；前端 unit 0 failure；Playwright 0 failure；Ruff、TypeScript、ESLint 和 Prettier 均无错误。

- [ ] **步骤 5：更新里程碑状态并提交**

只有步骤 4 全部通过且审查结论无阻塞项时，才把总体设计和专项规格的 M2 状态改为“已完成”。

```bash
git add .github/workflows/project-foundation-postgres-tests.yml README.md README_zh.md AGENTS.md backend/AGENTS.md frontend/AGENTS.md backend/tests/integration/test_m2_project_governance_postgres.py docs/superpowers/specs/2026-07-12-project-first-saas-design.md docs/superpowers/specs/2026-07-12-project-governance-m2-design.md
git commit -m "docs: record M2 project governance completion"
```

---

## 执行顺序与审查点

1. 任务 1 完成后审查 schema、downgrade 和 M1 数据兼容性。
2. 任务 2 至 4 每个任务分别审查事务边界、行锁顺序和错误语义。
3. 任务 5 完成后做一次 API 契约审查，确认跨项目资源统一 404。
4. 任务 6 和 7 完成后做一次信息架构审查，确认工作空间与项目菜单层级没有混用。
5. 任务 8 完成后做 token 泄露和前端缓存隔离审查。
6. 任务 9 只负责最终门禁、文档和完成状态，不在该任务追加新功能。
