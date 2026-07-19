# M3 系统与项目共享资产实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Agent、Skill、MCP 从文件与全局配置迁移为 PostgreSQL 中可治理的系统级/项目级不可变资产，交付 credential approval、系统绑定、精确 resolver 和管理 UI，同时保持 M3 不创建项目私有运行。

**Architecture:** 使用 Agent、Skill、MCP 三套 typed table 与 domain service，应用层通过不可变 `ProjectContext` 或 `SystemAssetGovernanceContext` 授权，repository 强制 scope 并由数据库约束兜底。credential 采用 AES-GCM envelope encryption；harness 只依赖 `AssetCatalogProvider` 协议，Gateway 注入 PostgreSQL provider。显式迁移写入 cutover marker 后，PostgreSQL 成为唯一运行时权威来源。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy 2 async、Alembic、PostgreSQL 17、Pydantic、cryptography `AESGCM`、pytest、Next.js App Router、React 19、TypeScript、Zod、TanStack Query、Playwright。

## Global Constraints

- 只使用 PostgreSQL；不增加 SQLite runtime，也不使用 PostgreSQL RLS。
- 系统资产为 `scope=system, project_id IS NULL`；项目资产为 `scope=project, project_id IS NOT NULL`。
- 系统资产只有 `system_admin` 可写；项目 Admin/Editor 可写项目资产，credential MCP 必须由项目 Admin 审批。
- `system_admin` override 只治理共享资产与 credential/grant，不能读取成员或私有 Thread、run、file、Memory、automation。
- 三类资产使用 typed table/domain service；禁止建设通用 JSONB asset registry。
- version payload、checksum、version number、父 version 和创建者不可变；内容变更必须创建新 version。
- 系统绑定固定 published version，不自动升级；系统与项目同名资产不覆盖。
- Skill version 是完整目录快照，未压缩内容上限为 100 MiB；禁止绝对路径、`..`、symlink 和可执行二进制。
- credential 明文上限为 64 KiB；AES-GCM key 为 32 bytes，nonce 为 12 bytes；API、日志、异常、checkpoint 和文件不得出现明文。
- retired credential version 不能创建新 grant，但已有 grant 继续有效；revoke 立即失效。
- M3 只交付 resolver 和 legacy 系统资产适配；项目页面不得提供 run CTA，项目私有运行属于 M4。
- 迁移和 key rotation 必须显式、幂等、支持 dry-run；应用启动不得自动导入 legacy 资产。
- 每个实现任务遵循 TDD：先写失败测试，确认失败原因，再写最小实现；每个任务独立提交。
- 执行时从最新 `dev` 创建 `codex/m3-shared-assets`；不要在 `dev` 上直接实现。

---

## 文件结构

### Backend persistence 与 domain

- `backend/packages/harness/deerflow/persistence/shared_assets/agent_model.py`：Agent 与 Agent version/ref ORM。
- `backend/packages/harness/deerflow/persistence/shared_assets/skill_model.py`：Skill、Skill version 与文件快照 ORM。
- `backend/packages/harness/deerflow/persistence/shared_assets/mcp_model.py`：MCP、MCP version 与 credential slot ORM。
- `backend/packages/harness/deerflow/persistence/shared_assets/credential_model.py`：credential/version/envelope/grant ORM。
- `backend/packages/harness/deerflow/persistence/shared_assets/binding_model.py`：三类 project system binding 与 catalog state/cutover ORM。
- `backend/packages/harness/deerflow/persistence/migrations/versions/0007_project_shared_assets.py`：M3 schema、复合约束、partial index、immutability trigger。
- `backend/app/shared_assets/models.py`：domain command/view/snapshot 类型。
- `backend/app/shared_assets/errors.py`：稳定 domain error。
- `backend/app/shared_assets/contexts.py`：平台治理 context 与项目 scope helper。
- `backend/app/shared_assets/governance_events.py`：平台 override 的脱敏治理事件 protocol 与默认 structured-log sink。
- `backend/app/shared_assets/agent_repository.py`、`agent_service.py`：Agent version 生命周期。
- `backend/app/shared_assets/skill_repository.py`、`skill_service.py`：Skill archive、scan 与 version 生命周期。
- `backend/app/shared_assets/mcp_repository.py`、`mcp_service.py`：MCP definition、slot、审批与发布。
- `backend/app/shared_assets/credential_repository.py`、`credential_service.py`：credential/version/envelope/grant。
- `backend/app/shared_assets/binding_repository.py`、`binding_service.py`：系统资产绑定、升级与回退。
- `backend/app/shared_assets/catalog_state_repository.py`：单例 catalog generation、cutover marker 与 cache invalidation 事务 helper。
- `backend/app/shared_assets/keyring.py`、`crypto.py`：环境 keyring 与 AES-GCM。
- `backend/app/shared_assets/resolver.py`：项目 asset snapshot resolver 与 MCP secret materializer。
- `backend/app/shared_assets/catalog_provider.py`：harness 协议的 PostgreSQL 实现及 cutover 行为。
- `backend/app/gateway/routers/admin_assets.py`：`/api/admin/assets` 与平台 override API。
- `backend/app/gateway/routers/project_assets.py`：`/api/projects/{project_id}` 下的资产、credential、binding API。
- `backend/packages/harness/deerflow/assets/catalog.py`：`AssetCatalogProvider` 协议、registry 和安全 snapshot 类型。
- `backend/scripts/migrate_assets.py`：inventory、导入、校验与 cutover。
- `backend/scripts/rotate_credentials.py`：credential envelope 主密钥轮换。

### Frontend

- `frontend/src/core/shared-assets/types.ts`：Zod contract 和 TypeScript 类型。
- `frontend/src/core/shared-assets/api.ts`：平台/项目 asset API client。
- `frontend/src/core/shared-assets/query-keys.ts`、`hooks.ts`：account/project 隔离的 TanStack Query 层。
- `frontend/src/components/assets/`：复用的列表、版本、diff、状态、credential 元数据组件。
- `frontend/src/components/admin/assets/`：系统资产与平台 override 管理组件。
- `frontend/src/components/projects/assets/`：项目资产、系统绑定、审批组件。
- `frontend/src/app/admin/assets/**`：平台管理路由。
- `frontend/src/app/projects/[project_slug]/{agents,skills,mcp,credentials}/page.tsx`：项目资产路由。

---

### Task 1: 建立 M3 PostgreSQL schema 与 typed ORM

**Files:**
- Create: `backend/packages/harness/deerflow/persistence/shared_assets/__init__.py`
- Create: `backend/packages/harness/deerflow/persistence/shared_assets/agent_model.py`
- Create: `backend/packages/harness/deerflow/persistence/shared_assets/skill_model.py`
- Create: `backend/packages/harness/deerflow/persistence/shared_assets/mcp_model.py`
- Create: `backend/packages/harness/deerflow/persistence/shared_assets/credential_model.py`
- Create: `backend/packages/harness/deerflow/persistence/shared_assets/binding_model.py`
- Create: `backend/packages/harness/deerflow/persistence/migrations/versions/0007_project_shared_assets.py`
- Modify: `backend/packages/harness/deerflow/persistence/models/__init__.py`
- Test: `backend/tests/test_m3_shared_assets_schema_postgres.py`

**Interfaces:**
- Consumes: `deerflow.persistence.base.Base`、`projects.id`、`users.id`、Alembic revision `0006_project_governance`。
- Produces: `AgentRow/AgentVersionRow`、`SkillRow/SkillVersionRow/SkillVersionFileRow`、`McpServerRow/McpServerVersionRow/McpCredentialSlotRow`、`CredentialRow/CredentialVersionRow/CredentialEnvelopeRow/CredentialGrantRow`、三类 binding row、`AssetCatalogStateRow`。

- [ ] **Step 1: 写 schema 失败测试**

```python
EXPECTED_TABLES = {
    "agents", "agent_versions", "agent_version_skill_refs",
    "agent_version_mcp_refs", "skills", "skill_versions",
    "skill_version_files", "mcp_servers", "mcp_server_versions",
    "mcp_version_credential_slots", "credentials", "credential_versions",
    "credential_envelopes", "credential_grants",
    "project_system_agent_bindings", "project_system_skill_bindings",
    "project_system_mcp_bindings", "asset_catalog_state",
}

async def test_m3_schema_has_all_typed_tables(migrated_postgres_database_url):
    engine = create_async_engine(migrated_postgres_database_url)
    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
        revision = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
    assert EXPECTED_TABLES <= tables
    assert revision == "0007_project_shared_assets"
```

同时写入 scope CHECK、partial unique index、复合 FK、immutable trigger、同名 system/project 可并存、已引用 version 禁止 DELETE 的 PostgreSQL 测试。

- [ ] **Step 2: 运行 schema 测试并确认失败**

Run: `cd backend && uv run pytest tests/test_m3_shared_assets_schema_postgres.py -q`

Expected: FAIL，缺少 revision `0007_project_shared_assets` 或 M3 tables；若因缺少 `POSTGRES_TEST_URL` 而 skip，不得进入实现，先使用本地 PostgreSQL test admin URL 重跑。

- [ ] **Step 3: 创建 typed ORM 和 Alembic revision**

每个逻辑资产表使用以下 scope 约束和 optimistic version；每个 version 表使用 `(asset_id, version_number)` unique constraint。不要使用通用 `assets` 表。

```python
CheckConstraint(
    "(scope = 'system' AND project_id IS NULL) OR "
    "(scope = 'project' AND project_id IS NOT NULL)",
    name="ck_agents_scope_project",
)
Index(
    "uq_agents_system_slug",
    func.lower(slug),
    unique=True,
    postgresql_where=text("scope = 'system'"),
)
Index(
    "uq_agents_project_slug",
    project_id,
    func.lower(slug),
    unique=True,
    postgresql_where=text("scope = 'project'"),
)
```

在 migration 中为 version payload columns 建 trigger function；只允许 workflow status 与审批元数据更新，禁止修改 payload/checksum/version number/supersedes/creator。`asset_catalog_state` 只保留 `id=1` 的单例行，包含 `generation BIGINT >= 1` 和 nullable `cutover_at`；所有可影响解析的 publish/archive/suspend/binding/grant/revoke 事务必须递增 generation。downgrade 检测任一 M3 table 有数据时必须拒绝。

- [ ] **Step 4: 注册 ORM 并运行 schema gate**

Run: `cd backend && uv run pytest tests/test_m3_shared_assets_schema_postgres.py tests/test_persistence_migrations_env.py -q`

Expected: PASS，revision 为 `0007_project_shared_assets`，ORM metadata 包含全部 M3 tables。

- [ ] **Step 5: 提交 schema**

```bash
git add backend/packages/harness/deerflow/persistence backend/tests/test_m3_shared_assets_schema_postgres.py
git commit -m "feat: add M3 shared asset schema"
```

---

### Task 2: 增加资产授权 context、capability 与稳定错误

**Files:**
- Create: `backend/app/shared_assets/__init__.py`
- Create: `backend/app/shared_assets/models.py`
- Create: `backend/app/shared_assets/errors.py`
- Create: `backend/app/shared_assets/contexts.py`
- Create: `backend/app/shared_assets/governance_events.py`
- Modify: `backend/app/projects/capabilities.py`
- Test: `backend/tests/test_shared_asset_contexts.py`
- Test: `backend/tests/test_project_capabilities.py`

**Interfaces:**
- Consumes: `ProjectContext.require()`、authenticated user `id/system_role`。
- Produces: `AssetScope`、`AssetKind`、`WorkflowStatus`、`AgentPayload`、`SkillArchiveFile`、`AssetSelection`、`ResolvedAssetSnapshot`、三类 typed resolved snapshot、`SystemAssetGovernanceContext`、`SharedAssetGovernanceEventSink`、`resolve_asset_actor()`。

- [ ] **Step 1: 写 capability 和 override 失败测试**

```python
def test_only_admin_manages_system_bindings_and_credentials():
    assert Capability.SHARED_ASSETS_MANAGE_BINDINGS in capabilities_for(ProjectRole.ADMIN)
    assert Capability.MCP_CREDENTIALS_APPROVE in capabilities_for(ProjectRole.ADMIN)
    for role in (ProjectRole.EDITOR, ProjectRole.RUNNER, ProjectRole.VIEWER):
        assert Capability.SHARED_ASSETS_MANAGE_BINDINGS not in capabilities_for(role)
        assert Capability.MCP_CREDENTIALS_APPROVE not in capabilities_for(role)

def test_system_override_does_not_construct_project_context():
    actor = resolve_asset_actor(system_admin_user, project_id=PROJECT_ID, request_id="r1")
    assert isinstance(actor, SystemAssetGovernanceContext)
    assert not isinstance(actor, ProjectContext)

def test_override_event_contains_only_governance_metadata(event_sink):
    event_sink.write_override(actor=SYSTEM_ADMIN_ID, project_id=PROJECT_ID, asset_id=ASSET_ID, version_id=VERSION_ID, action="publish", request_id="r1")
    event = event_sink.events[0]
    assert set(event) == {"actor_user_id", "project_id", "asset_id", "version_id", "action", "request_id"}
```

- [ ] **Step 2: 运行测试确认缺少新 capability/context**

Run: `cd backend && uv run pytest tests/test_shared_asset_contexts.py tests/test_project_capabilities.py -q`

Expected: FAIL，`SHARED_ASSETS_MANAGE_BINDINGS` 或 `SystemAssetGovernanceContext` 尚不存在。

- [ ] **Step 3: 实现不可变 domain contract**

```python
@dataclass(frozen=True)
class SystemAssetGovernanceContext:
    user_id: uuid.UUID
    request_id: str
    project_id: uuid.UUID | None = None

@dataclass(frozen=True)
class AssetSelection:
    kind: AssetKind
    asset_id: uuid.UUID
    version_id: uuid.UUID | None = None

@dataclass(frozen=True)
class AgentPayload:
    description: str
    soul: str
    model_ref: str
    tool_groups: tuple[str, ...]
    skill_version_ids: tuple[uuid.UUID, ...]
    mcp_version_ids: tuple[uuid.UUID, ...]

@dataclass(frozen=True)
class SkillArchiveFile:
    path: str
    content: bytes
    media_type: str = "application/octet-stream"

@dataclass(frozen=True)
class ResolvedAssetSnapshot:
    kind: AssetKind
    scope: AssetScope
    asset_id: uuid.UUID
    version_id: uuid.UUID
    checksum: str
    catalog_generation: int
    dependency_version_ids: tuple[uuid.UUID, ...]

@dataclass(frozen=True)
class ResolvedAgentSnapshot(ResolvedAssetSnapshot):
    payload: AgentPayload

@dataclass(frozen=True)
class ResolvedSkillSnapshot(ResolvedAssetSnapshot):
    files: tuple[SkillArchiveFile, ...]
    secret_requirements: tuple[str, ...]

@dataclass(frozen=True)
class ResolvedMcpSnapshot(ResolvedAssetSnapshot):
    definition: Mapping[str, object]
    credential_grant_ids: tuple[uuid.UUID, ...]
```

增加稳定错误：`AssetNotFound`→404、`AssetForbidden`→403、`AssetConflict`→409、`AssetValidationFailed`→422、`AssetStorageUnavailable`→503。错误只携带公共 code 与 `request_id`。

`SharedAssetGovernanceEventSink` 只接受 actor/project/asset/version/action/request_id；默认实现写 structured log，不接收 payload、diff、credential metadata 或私有资源 ID。M6 可替换为持久化 audit sink，不改变 service 接口。

- [ ] **Step 4: 运行授权测试**

Run: `cd backend && uv run pytest tests/test_shared_asset_contexts.py tests/test_project_capabilities.py -q`

Expected: PASS；Editor 保留 `shared_assets.edit`，Admin 独占 binding 与 credential approval。

- [ ] **Step 5: 提交授权 contract**

```bash
git add backend/app/shared_assets backend/app/projects/capabilities.py backend/tests/test_shared_asset_contexts.py backend/tests/test_project_capabilities.py
git commit -m "feat: define shared asset authorization contracts"
```

---

### Task 3: 实现 credential keyring 与 AES-GCM envelope encryption

**Files:**
- Create: `backend/app/shared_assets/keyring.py`
- Create: `backend/app/shared_assets/crypto.py`
- Test: `backend/tests/test_shared_asset_credential_crypto.py`

**Interfaces:**
- Produces: `CredentialKeyring.from_environment()`、`encrypt_credential_payload()`、`decrypt_credential_payload()`、`EncryptedEnvelope`。
- Environment: `DEER_FLOW_CREDENTIAL_ACTIVE_KEY_ID`、`DEER_FLOW_CREDENTIAL_KEYRING_JSON`。

- [ ] **Step 1: 写 round-trip、tamper 和脱敏失败测试**

```python
def test_encrypts_with_12_byte_nonce_and_bound_aad(monkeypatch):
    monkeypatch.setenv("DEER_FLOW_CREDENTIAL_ACTIVE_KEY_ID", "k1")
    monkeypatch.setenv("DEER_FLOW_CREDENTIAL_KEYRING_JSON", json.dumps({"k1": b64encode(b"1" * 32).decode()}))
    keyring = CredentialKeyring.from_environment()
    envelope = encrypt_credential_payload(PAYLOAD, SCOPE, PROJECT_ID, VERSION_ID, keyring)
    assert envelope.key_id == "k1"
    assert len(envelope.nonce) == 12
    assert decrypt_credential_payload(envelope, SCOPE, PROJECT_ID, VERSION_ID, keyring) == PAYLOAD

def test_tamper_and_wrong_scope_fail_without_secret_in_error(monkeypatch):
    configure_test_keyring(monkeypatch)
    keyring = CredentialKeyring.from_environment()
    envelope = encrypt_credential_payload(PAYLOAD, "project", PROJECT_ID, VERSION_ID, keyring)
    tampered = dataclasses.replace(envelope, ciphertext=envelope.ciphertext[:-1] + b"0")
    with pytest.raises(CredentialDecryptFailed) as error:
        decrypt_credential_payload(tampered, "system", None, VERSION_ID, keyring)
    assert "token-value" not in str(error.value)
```

还要覆盖 key 非 32 bytes、未知 `key_id`、明文超过 64 KiB、payload schema 非 `env/headers/oauth`、日志不包含 key JSON。

- [ ] **Step 2: 运行 crypto 测试确认失败**

Run: `cd backend && uv run pytest tests/test_shared_asset_credential_crypto.py -q`

Expected: FAIL，crypto 模块不存在。

- [ ] **Step 3: 实现 canonical JSON、AAD 和 AESGCM**

```python
def _aad(version_id: UUID, scope: AssetScope, project_id: UUID | None) -> bytes:
    owner = str(project_id) if project_id else "system"
    return f"deerflow-credential:v1:{version_id}:{scope}:{owner}".encode()

def encrypt_credential_payload(payload, scope, project_id, version_id, keyring):
    plaintext = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    validate_payload_and_size(payload, plaintext)
    nonce = os.urandom(12)
    ciphertext = AESGCM(keyring.active_key).encrypt(nonce, plaintext, _aad(version_id, scope, project_id))
    return EncryptedEnvelope(key_id=keyring.active_key_id, nonce=nonce, ciphertext=ciphertext)
```

任何异常对外统一为稳定错误；禁止在 dataclass `repr` 中暴露 ciphertext、nonce 或 key material。

- [ ] **Step 4: 运行 crypto 测试**

Run: `cd backend && uv run pytest tests/test_shared_asset_credential_crypto.py -q`

Expected: PASS，包括 tamper、wrong AAD、oversize 和日志扫描。

- [ ] **Step 5: 提交 crypto**

```bash
git add backend/app/shared_assets/keyring.py backend/app/shared_assets/crypto.py backend/tests/test_shared_asset_credential_crypto.py
git commit -m "feat: encrypt shared asset credentials"
```

---

### Task 4: 实现 Agent typed repository 与 version service

**Files:**
- Create: `backend/app/shared_assets/agent_repository.py`
- Create: `backend/app/shared_assets/agent_service.py`
- Test: `backend/tests/test_shared_asset_agent_service.py`
- Test: `backend/tests/integration/test_m3_agent_assets_postgres.py`

**Interfaces:**
- Consumes: `ProjectContext|SystemAssetGovernanceContext`、Task 1 rows、Task 2 errors/models。
- Produces: `AgentService.create_asset()`、`create_version()`、`publish()`、`archive()`、`suspend()`、`list_visible()`、`get_version_history()`。

- [ ] **Step 1: 写 service 与真实 PostgreSQL 隔离失败测试**

```python
async def test_project_agent_publish_pins_dependency_versions(service, editor_context):
    asset = await service.create_asset(editor_context, CreateAgent(slug="analyst", display_name="Analyst"))
    payload = AgentPayload(
        description="Research analyst",
        soul="Verify sources before answering.",
        model_ref="default",
        tool_groups=("research",),
        skill_version_ids=(SKILL_VERSION_ID,),
        mcp_version_ids=(MCP_VERSION_ID,),
    )
    draft = await service.create_version(editor_context, asset.id, payload, expected_asset_version=1)
    published = await service.publish(editor_context, asset.id, draft.id, expected_asset_version=2)
    assert published.workflow_status == WorkflowStatus.PUBLISHED
    assert published.skill_version_ids == (SKILL_VERSION_ID,)

async def test_project_agent_repository_hides_other_project(service, other_project_context, first_project_asset_id):
    with pytest.raises(AssetNotFound):
        await service.get(other_project_context, first_project_asset_id)
```

覆盖 system-only write、system Agent 引用 project dependency 返回 422、项目内 dependency、已绑定 system dependency、archived/suspended dependency、optimistic conflict、payload immutable。

- [ ] **Step 2: 运行 Agent tests 确认失败**

Run: `cd backend && uv run pytest tests/test_shared_asset_agent_service.py tests/integration/test_m3_agent_assets_postgres.py -q`

Expected: FAIL，Agent service/repository 尚不存在。

- [ ] **Step 3: 实现强制 scope repository 和事务发布**

```python
class AgentRepository:
    async def get_project_asset(self, context: ProjectContext, asset_id: UUID, *, for_update: bool = False) -> AgentRow:
        stmt = select(AgentRow).where(
            AgentRow.id == asset_id,
            AgentRow.scope == "project",
            AgentRow.project_id == context.project_id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise AssetNotFound()
        return row
```

`publish()` 在同一事务锁 asset，校验 expected version、workflow transition 和 dependency closure，最后移动 `current_published_version_id`；禁止 repository 暴露无 scope 的 project lookup。

- [ ] **Step 4: 运行 Agent tests**

Run: `cd backend && uv run pytest tests/test_shared_asset_agent_service.py tests/integration/test_m3_agent_assets_postgres.py -q`

Expected: PASS，跨项目统一 404，并发发布只有一个成功。

- [ ] **Step 5: 提交 Agent domain**

```bash
git add backend/app/shared_assets/agent_* backend/tests/test_shared_asset_agent_service.py backend/tests/integration/test_m3_agent_assets_postgres.py
git commit -m "feat: add versioned Agent assets"
```

---

### Task 5: 实现 Skill 完整目录快照、scan 与发布

**Files:**
- Create: `backend/app/shared_assets/skill_repository.py`
- Create: `backend/app/shared_assets/skill_service.py`
- Test: `backend/tests/test_shared_asset_skill_service.py`
- Test: `backend/tests/integration/test_m3_skill_assets_postgres.py`
- Reuse: `backend/packages/harness/deerflow/skills/` 中现有 parser、validation 与 security scan。

**Interfaces:**
- Produces: `SkillService.create_version_from_archive()`、`preview_archive()`、`publish()`、`load_version_files()`。

- [ ] **Step 1: 写 archive 安全和 checksum 失败测试**

```python
@pytest.mark.parametrize("path", ["/etc/passwd", "../escape", "a/../../escape"])
async def test_rejects_unsafe_skill_paths(service, editor_context, path):
    with pytest.raises(AssetValidationFailed):
        await service.create_version_from_archive(editor_context, ASSET_ID, [SkillArchiveFile(path, b"x")], expected_asset_version=1)

async def test_skill_checksum_is_order_independent(service, editor_context):
    files = [SkillArchiveFile("SKILL.md", b"---\nname: demo\n---\n"), SkillArchiveFile("scripts/run.py", b"print('ok')\n")]
    first = await service.preview_archive(editor_context, files)
    second = await service.preview_archive(editor_context, list(reversed(files)))
    assert first.checksum == second.checksum
```

覆盖 symlink、executable binary、重复路径、缺 `SKILL.md`、100 MiB 边界、frontmatter、scan reject/allow、跨项目读取。

增加一条 Skill secret requirement 测试：requirement metadata 可以进入 version，但数据库中不创建 credential/grant，也不保存 secret value。

- [ ] **Step 2: 运行 Skill tests 确认失败**

Run: `cd backend && uv run pytest tests/test_shared_asset_skill_service.py tests/integration/test_m3_skill_assets_postgres.py -q`

Expected: FAIL，Skill service 不存在。

- [ ] **Step 3: 实现规范化快照与复用现有 scan**

```python
def normalize_skill_files(files: Sequence[SkillArchiveFile]) -> tuple[SkillArchiveFile, ...]:
    normalized = tuple(sorted((validate_archive_file(item) for item in files), key=lambda item: item.path))
    if sum(len(item.content) for item in normalized) > 100 * 1024 * 1024:
        raise AssetValidationFailed("SKILL_ARCHIVE_TOO_LARGE")
    if not any(item.path == "SKILL.md" for item in normalized):
        raise AssetValidationFailed("SKILL_MANIFEST_REQUIRED")
    return normalized
```

每个 file 单独 SHA-256，version checksum 由规范路径、file checksum、size 计算。scan 结果只保存 decision、规则 ID 和脱敏摘要。

Skill version 可保存脱敏的 requirement 名称和 schema；不得保存 secret value，也不得创建 credential grant。`SkillService` 同时实现 archive/suspend，并遵循 archived 旧引用可用、suspended 立即不可解析的共同状态机。

- [ ] **Step 4: 运行 Skill tests**

Run: `cd backend && uv run pytest tests/test_shared_asset_skill_service.py tests/integration/test_m3_skill_assets_postgres.py -q`

Expected: PASS，完整目录可按 checksum 重建，危险 archive fail closed。

- [ ] **Step 5: 提交 Skill domain**

```bash
git add backend/app/shared_assets/skill_* backend/tests/test_shared_asset_skill_service.py backend/tests/integration/test_m3_skill_assets_postgres.py
git commit -m "feat: add immutable Skill snapshots"
```

---

### Task 6: 实现 MCP、credential version、grant 与审批

**Files:**
- Create: `backend/app/shared_assets/mcp_repository.py`
- Create: `backend/app/shared_assets/mcp_service.py`
- Create: `backend/app/shared_assets/credential_repository.py`
- Create: `backend/app/shared_assets/credential_service.py`
- Test: `backend/tests/test_shared_asset_mcp_service.py`
- Test: `backend/tests/test_shared_asset_credential_service.py`
- Test: `backend/tests/integration/test_m3_mcp_credentials_postgres.py`

**Interfaces:**
- Consumes: Task 3 crypto。
- Produces: `CredentialService.create/replace/revoke()`、`McpService.create_version/submit_approval/approve/publish/archive/suspend()`、`CredentialGrantView`。

- [ ] **Step 1: 写 secret 拆分、scope 与审批失败测试**

```python
async def test_editor_cannot_publish_credential_mcp(mcp_service, editor_context):
    draft = await mcp_service.create_version(editor_context, MCP_ID, MCP_WITH_REQUIRED_SLOT, expected_asset_version=1)
    pending = await mcp_service.submit_approval(editor_context, MCP_ID, draft.id, expected_asset_version=2)
    assert pending.workflow_status == WorkflowStatus.PENDING_APPROVAL
    with pytest.raises(AssetForbidden):
        await mcp_service.approve(editor_context, MCP_ID, draft.id, CREDENTIAL_VERSION_ID, expected_asset_version=3)

async def test_project_mcp_rejects_system_or_other_project_credential(mcp_service, admin_context):
    with pytest.raises(AssetValidationFailed):
        await mcp_service.approve(admin_context, MCP_ID, VERSION_ID, FOREIGN_CREDENTIAL_VERSION_ID, expected_asset_version=2)
```

覆盖 definition 中 secret 字段拒绝、slot schema、无 credential MCP 由 Editor 发布、system MCP 只用 system credential、retired 旧 grant 可用、revoke 立即失败、API view 无 ciphertext/nonce/key_id。

- [ ] **Step 2: 运行 MCP/credential tests 确认失败**

Run: `cd backend && uv run pytest tests/test_shared_asset_mcp_service.py tests/test_shared_asset_credential_service.py tests/integration/test_m3_mcp_credentials_postgres.py -q`

Expected: FAIL，service/repository 不存在。

- [ ] **Step 3: 实现固定锁序和审批事务**

```python
async def approve(self, context, asset_id, version_id, credential_version_id, expected_asset_version):
    context.require(Capability.MCP_CREDENTIALS_APPROVE)
    async with self.repository.transaction():
        project = await self.repository.lock_project(context.project_id)
        asset = await self.repository.lock_asset(context, asset_id)
        version = await self.repository.lock_version(asset, version_id)
        credential = await self.credentials.lock_credential(context, credential_version_id)
        self._validate_scope_slot_and_state(asset, version, credential)
        grant = await self.repository.create_grant(version, credential)
        return await self.repository.publish_approved(asset, version, grant, expected_asset_version)
```

锁序固定为 `project -> asset -> asset version -> credential -> credential version -> grant`。replace 创建新 semantic version/envelope；不自动移动旧 grant。

- [ ] **Step 4: 运行 MCP/credential tests**

Run: `cd backend && uv run pytest tests/test_shared_asset_mcp_service.py tests/test_shared_asset_credential_service.py tests/integration/test_m3_mcp_credentials_postgres.py -q`

Expected: PASS，并发 approval/replace 产生稳定 409 而非 500。

- [ ] **Step 5: 提交 MCP 与 credential domain**

```bash
git add backend/app/shared_assets/mcp_* backend/app/shared_assets/credential_* backend/tests/test_shared_asset_mcp_service.py backend/tests/test_shared_asset_credential_service.py backend/tests/integration/test_m3_mcp_credentials_postgres.py
git commit -m "feat: add MCP credential approval"
```

---

### Task 7: 实现系统绑定、精确 resolver 与 secret materializer

**Files:**
- Create: `backend/app/shared_assets/binding_repository.py`
- Create: `backend/app/shared_assets/binding_service.py`
- Create: `backend/app/shared_assets/catalog_state_repository.py`
- Create: `backend/app/shared_assets/resolver.py`
- Test: `backend/tests/test_shared_asset_binding_service.py`
- Test: `backend/tests/test_shared_asset_resolver.py`
- Test: `backend/tests/integration/test_m3_asset_resolution_postgres.py`

**Interfaces:**
- Produces: `BindingService.enable/upgrade/rollback/disable()`、`CatalogStateRepository.bump_generation()`、`resolve_project_asset_snapshot(context, selection)`、`materialize_mcp_secrets(resolved)`。

- [ ] **Step 1: 写 pinning、archive/suspend 与 fail-closed 测试**

```python
async def test_system_upgrade_does_not_move_existing_binding(binding_service, resolver, admin_context):
    binding = await binding_service.enable(admin_context, SYSTEM_AGENT_ID, VERSION_1)
    await publish_system_version(VERSION_2)
    resolved = await resolver.resolve_project_asset_snapshot(admin_context, AssetSelection(AssetKind.AGENT, SYSTEM_AGENT_ID))
    assert resolved.version_id == VERSION_1

async def test_archived_binding_resolves_but_suspended_asset_fails(system_asset_fixture):
    await system_asset_fixture.archive()
    assert (await system_asset_fixture.resolve()).version_id == VERSION_1
    await system_asset_fixture.suspend()
    with pytest.raises(AssetResolutionUnavailable):
        await system_asset_fixture.resolve()
```

覆盖项目 asset current pointer、依赖 closure、未绑定 system asset、wrong scope、revoked grant、Admin-only binding、optimistic upgrade conflict。

增加类型边界测试：`materialize_mcp_secrets()` 传入 `ResolvedSkillSnapshot` 必须返回 `AssetValidationFailed`，M3 不为 Skill materialize secret。

- [ ] **Step 2: 运行 resolver tests 确认失败**

Run: `cd backend && uv run pytest tests/test_shared_asset_binding_service.py tests/test_shared_asset_resolver.py tests/integration/test_m3_asset_resolution_postgres.py -q`

Expected: FAIL，binding/resolver 不存在。

- [ ] **Step 3: 实现无明文 snapshot 与内部 materializer**

```python
async def resolve_project_asset_snapshot(context: ProjectContext, selection: AssetSelection) -> ResolvedAssetSnapshot:
    context.require(Capability.SHARED_ASSETS_READ)
    if await repository.is_project_asset(context.project_id, selection.asset_id):
        version = await repository.current_project_version(context.project_id, selection)
    else:
        version = await repository.pinned_system_version(context.project_id, selection)
    await repository.assert_resolvable(version)
    return build_snapshot(version)  # IDs/checksums/grant refs only
```

`materialize_mcp_secrets()` 只接受已解析的 MCP snapshot，重新校验 credential/grant 状态后解密；返回短生命周期对象，`repr=False`，不得 cache。

所有影响 resolver 的 service 在业务事务提交前调用 `CatalogStateRepository.bump_generation()`。resolver snapshot 带 generation；provider/cache 只在数据库 generation 相同的时间窗复用结果。

- [ ] **Step 4: 运行 resolver tests**

Run: `cd backend && uv run pytest tests/test_shared_asset_binding_service.py tests/test_shared_asset_resolver.py tests/integration/test_m3_asset_resolution_postgres.py -q`

Expected: PASS，snapshot 精确、无 secret，suspend/revoke 立即 fail closed。

- [ ] **Step 5: 提交 binding 与 resolver**

```bash
git add backend/app/shared_assets/binding_* backend/app/shared_assets/catalog_state_repository.py backend/app/shared_assets/resolver.py backend/tests/test_shared_asset_binding_service.py backend/tests/test_shared_asset_resolver.py backend/tests/integration/test_m3_asset_resolution_postgres.py
git commit -m "feat: resolve pinned project assets"
```

---

### Task 8: 暴露项目资产和平台治理 API

**Files:**
- Create: `backend/app/gateway/routers/project_assets.py`
- Create: `backend/app/gateway/routers/admin_assets.py`
- Modify: `backend/app/gateway/app.py`
- Modify: `backend/app/gateway/routers/agents.py`
- Modify: `backend/app/gateway/routers/skills.py`
- Modify: `backend/app/gateway/routers/mcp.py`
- Test: `backend/tests/test_project_assets_router.py`
- Test: `backend/tests/test_admin_assets_router.py`

**Interfaces:**
- Consumes: Tasks 2、4–7 services。
- Produces: `/api/projects/{project_id}/agents`、`/skills`、`/mcp-servers`、`/credentials`、三类 system binding routes，以及 `/api/admin/assets/*`、`/api/admin/projects/{project_id}/assets/*`。

- [ ] **Step 1: 写 router contract 和错误映射失败测试**

```python
def test_project_asset_list_separates_scopes(client, member_cookie):
    response = client.get(f"/api/projects/{PROJECT_ID}/agents", cookies=member_cookie)
    assert response.status_code == 200
    assert set(response.json()) == {"system_items", "project_items", "request_id"}

def test_non_system_admin_cannot_access_admin_assets(client, user_cookie):
    response = client.get("/api/admin/assets/agents", cookies=user_cookie)
    assert response.status_code == 403

def test_credential_response_never_contains_envelope_fields(client, admin_cookie):
    response = client.get("/api/admin/assets/credentials", cookies=admin_cookie)
    assert response.status_code == 200
    body = response.json()
    assert not ({"ciphertext", "nonce", "key_id"} & recursive_keys(body))
```

- [ ] **Step 2: 运行 router tests 确认 404/模块缺失**

Run: `cd backend && uv run pytest tests/test_project_assets_router.py tests/test_admin_assets_router.py -q`

Expected: FAIL，routes 尚未注册。

- [ ] **Step 3: 实现 thin routers 和统一 response models**

```python
project_router = APIRouter(prefix="/api/projects/{project_id}", tags=["project-assets"])
admin_router = APIRouter(prefix="/api/admin/assets", tags=["admin-assets"])
admin_project_router = APIRouter(prefix="/api/admin/projects/{project_id}/assets", tags=["admin-project-assets"])

def raise_asset_domain(exc: SharedAssetError, request_id: str) -> NoReturn:
    status_code = {AssetNotFound: 404, AssetForbidden: 403, AssetConflict: 409,
                   AssetValidationFailed: 422, AssetStorageUnavailable: 503}[type(exc)]
    raise HTTPException(status_code, detail={"code": exc.code, "message": exc.public_message, "request_id": request_id})
```

项目端点先 `resolve_project_context`；平台端点使用 `require_admin_user` 后构建 `SystemAssetGovernanceContext`。Pydantic models 全部 `extra="forbid"`。

每次平台 override 成功后调用 Task 2 的 `SharedAssetGovernanceEventSink`；测试确保未加入项目的 `system_admin` 可以治理项目共享资产，但相同 context 无法传入 membership/private-work repository。

- [ ] **Step 4: 运行 router 和现有项目 router tests**

Run: `cd backend && uv run pytest tests/test_project_assets_router.py tests/test_admin_assets_router.py tests/test_projects_router.py -q`

Expected: PASS；404/403/409/422/503 contract 稳定，响应不含 secret storage metadata。

- [ ] **Step 5: 提交 API**

```bash
git add backend/app/gateway/routers/project_assets.py backend/app/gateway/routers/admin_assets.py backend/app/gateway/app.py backend/tests/test_project_assets_router.py backend/tests/test_admin_assets_router.py
git commit -m "feat: expose shared asset APIs"
```

---

### Task 9: 建立 harness catalog protocol 并切换 legacy 系统资产 runtime

**Files:**
- Create: `backend/packages/harness/deerflow/assets/__init__.py`
- Create: `backend/packages/harness/deerflow/assets/catalog.py`
- Create: `backend/app/shared_assets/catalog_provider.py`
- Modify: `backend/app/gateway/app.py`
- Modify: `backend/packages/harness/deerflow/config/agents_config.py`
- Modify: `backend/packages/harness/deerflow/agents/lead_agent/prompt.py`
- Modify: `backend/packages/harness/deerflow/skills/storage/__init__.py`
- Modify: `backend/packages/harness/deerflow/mcp/tools.py`
- Test: `backend/tests/test_asset_catalog_provider.py`
- Test: `backend/tests/test_harness_boundary.py`
- Test: `backend/tests/test_legacy_system_asset_runtime.py`

**Interfaces:**
- Produces: `AssetCatalogProvider` Protocol、`set_asset_catalog_provider()`、`get_asset_catalog_provider()`；Gateway lifespan 安装 `PostgresAssetCatalogProvider`。

- [ ] **Step 1: 写 provider boundary 与 cutover 失败测试**

```python
class AssetCatalogProvider(Protocol):
    async def get_system_agent(self, slug: str) -> ResolvedAgentSnapshot:
        raise NotImplementedError

    async def list_system_skills(self) -> tuple[ResolvedSkillSnapshot, ...]:
        raise NotImplementedError

    async def list_system_mcp(self) -> tuple[ResolvedMcpSnapshot, ...]:
        raise NotImplementedError

async def test_cutover_marker_forbids_file_fallback(provider):
    await provider.mark_cutover_for_test()
    await delete_catalog_rows_for_test()
    with pytest.raises(AssetCatalogUnavailable):
        await provider.list_system_skills()
```

另写 legacy runtime 只能解析 system asset、项目 asset 拒绝，以及 harness source 不导入 `app.*`。

再写 compatibility router 测试：cutover 后 legacy GET 只返回 PostgreSQL system assets；legacy file mutation endpoint 返回稳定 `409 ASSET_CATALOG_CUTOVER` 并指向 `/admin/assets`，不得继续写文件。

- [ ] **Step 2: 运行 provider/runtime tests 确认失败**

Run: `cd backend && uv run pytest tests/test_asset_catalog_provider.py tests/test_legacy_system_asset_runtime.py tests/test_harness_boundary.py -q`

Expected: FAIL，catalog protocol/provider 尚不存在。

- [ ] **Step 3: 实现 app→harness 注入和 marker 双阶段行为**

```python
_provider: AssetCatalogProvider | None = None

def set_asset_catalog_provider(provider: AssetCatalogProvider | None) -> None:
    global _provider
    _provider = provider

def get_asset_catalog_provider() -> AssetCatalogProvider | None:
    return _provider
```

marker 前保持现有 file loader；marker 后 Agent、Skill、MCP loader 与 legacy GET router 必须从 provider 取得 published system snapshots。legacy file mutation endpoint 在 cutover 后拒绝写入。MCP secret 通过 Task 7 internal materializer 注入单次 client construction，不写 `ExtensionsConfig`、checkpoint 或 cache。

provider 每次 cache lookup 读取 `asset_catalog_state.generation`；generation 变化时整体丢弃 Agent/Skill/MCP cache。测试 publish、suspend 和 grant revoke 都会使旧 cache 失效。

- [ ] **Step 4: 运行 focused runtime regressions**

Run: `cd backend && uv run pytest tests/test_asset_catalog_provider.py tests/test_legacy_system_asset_runtime.py tests/test_harness_boundary.py tests/test_lead_agent_prompt.py tests/test_mcp_client_config.py tests/test_skill_catalog.py -q`

Expected: PASS；marker 后删文件不影响 runtime，删 DB row 会安全失败且不回退文件。

- [ ] **Step 5: 提交 runtime cutover adapter**

```bash
git add backend/packages/harness/deerflow/assets backend/app/shared_assets/catalog_provider.py backend/app/gateway/app.py backend/app/gateway/routers/agents.py backend/app/gateway/routers/skills.py backend/app/gateway/routers/mcp.py backend/packages/harness/deerflow/config/agents_config.py backend/packages/harness/deerflow/agents/lead_agent/prompt.py backend/packages/harness/deerflow/skills/storage/__init__.py backend/packages/harness/deerflow/mcp/tools.py backend/tests/test_asset_catalog_provider.py backend/tests/test_legacy_system_asset_runtime.py backend/tests/test_harness_boundary.py
git commit -m "feat: load system assets from PostgreSQL"
```

---

### Task 10: 实现 asset migration、cutover 和 credential rotation 脚本

**Files:**
- Create: `backend/scripts/migrate_assets.py`
- Create: `backend/scripts/rotate_credentials.py`
- Modify: `backend/Makefile`
- Modify: `Makefile`
- Test: `backend/tests/test_migrate_assets.py`
- Test: `backend/tests/integration/test_m3_asset_migration_postgres.py`
- Test: `backend/tests/test_rotate_credentials.py`

**Interfaces:**
- Produces: `make migrate-assets ARGS="--dry-run|--execute"`、`make rotate-credentials ARGS="--dry-run --key-id m3-next"`。
- Sources: repo Agent/default config、`skills/public`、`extensions_config.json`、user custom Agent/Skill directories。

- [ ] **Step 1: 写 inventory、幂等、secret 和 rotation 失败测试**

```python
async def test_migration_is_idempotent_and_cutover_requires_validation(asset_migration_fixture):
    run_migration = asset_migration_fixture.run
    first = await run_migration("--execute")
    second = await run_migration("--execute")
    assert first.created_versions > 0
    assert second.created_versions == 0
    assert second.noop_versions == first.created_versions
    assert await cutover_marker_exists()

def test_migration_output_never_contains_mcp_secret(capsys, source_tree):
    run_inventory(source_tree)
    assert "plain-token" not in capsys.readouterr().out
```

覆盖缺默认项目、source ownership 不明、checksum 变化创建新 version、active key 缺失、rotation dry-run、resume cursor、tamper 中止、旧 envelope retired。

覆盖 legacy shared custom directory：预检必须把它列为 `unresolved_owner` 并拒绝 execute，除非源清单中已有明确 user→default project 映射；禁止自动归入 system 或任意项目。

- [ ] **Step 2: 运行 scripts tests 确认失败**

Run: `cd backend && uv run pytest tests/test_migrate_assets.py tests/integration/test_m3_asset_migration_postgres.py tests/test_rotate_credentials.py -q`

Expected: FAIL，scripts/Make targets 不存在。

- [ ] **Step 3: 实现显式 CLI 和 ledger**

```python
mode = parser.add_mutually_exclusive_group(required=True)
mode.add_argument("--dry-run", action="store_true")
mode.add_argument("--execute", action="store_true")
parser.add_argument("--resume-cursor")
parser.add_argument("--batch-size", type=int, default=100)
```

迁移先输出脱敏 inventory；execute 前在 `.deer-flow/migrations/assets/` 下生成 UUID4 run ID 目录并把所有源文件复制到其 `backup/` 子目录，目录 mode 固定 0700、文件固定 0600，并拒绝 symlink。备份目录已被 gitignore，日志只输出 run ID、路径、数量和 checksum，不能输出内容。随后按 `source_key + checksum` 幂等写入；只有 counts、checksums、dependency、decrypt probe 全部通过才写单例 cutover marker。rotation 按 credential version UUID 排序并 `FOR UPDATE SKIP LOCKED`，每批独立事务，成功后切 active envelope。

- [ ] **Step 4: 运行 scripts tests 和 CLI help**

Run: `cd backend && uv run pytest tests/test_migrate_assets.py tests/integration/test_m3_asset_migration_postgres.py tests/test_rotate_credentials.py -q && uv run python scripts/migrate_assets.py --help && uv run python scripts/rotate_credentials.py --help`

Expected: PASS；help 明确 dry-run 默认值，stdout/stderr 不含 secret。

- [ ] **Step 5: 提交运维脚本**

```bash
git add backend/scripts/migrate_assets.py backend/scripts/rotate_credentials.py backend/Makefile Makefile backend/tests/test_migrate_assets.py backend/tests/integration/test_m3_asset_migration_postgres.py backend/tests/test_rotate_credentials.py
git commit -m "feat: migrate and rotate shared assets"
```

---

### Task 11: 建立 Frontend shared-assets contract、API 和 query isolation

**Files:**
- Create: `frontend/src/core/shared-assets/index.ts`
- Create: `frontend/src/core/shared-assets/types.ts`
- Create: `frontend/src/core/shared-assets/api.ts`
- Create: `frontend/src/core/shared-assets/query-keys.ts`
- Create: `frontend/src/core/shared-assets/hooks.ts`
- Modify: `frontend/src/core/projects/types.ts`
- Test: `frontend/tests/unit/core/shared-assets/types.test.ts`
- Test: `frontend/tests/unit/core/shared-assets/api.test.ts`
- Test: `frontend/tests/unit/core/shared-assets/query-keys.test.ts`

**Interfaces:**
- Produces: `AssetSummary`、`AssetVersion`、`CredentialMetadata`、`SystemBinding`、`listProjectAssets()`、`listAdminAssets()`、mutation hooks。

- [ ] **Step 1: 写严格 Zod 和 query-key 失败测试**

```typescript
test("rejects secret storage fields", () => {
  expect(() => credentialMetadataSchema.parse({
    id: ID, name: "github", status: "active", version_number: 1,
    ciphertext: "forbidden",
  })).toThrow();
});

test("keys include account and project", () => {
  expect(projectAssetKey("u1", "p1", "agents")).not.toEqual(
    projectAssetKey("u2", "p1", "agents"),
  );
  expect(projectAssetKey("u1", "p1", "agents")).not.toEqual(
    projectAssetKey("u1", "p2", "agents"),
  );
});
```

- [ ] **Step 2: 运行 Frontend contract tests 确认失败**

Run: `cd frontend && pnpm test -- tests/unit/core/shared-assets/types.test.ts tests/unit/core/shared-assets/api.test.ts tests/unit/core/shared-assets/query-keys.test.ts`

Expected: FAIL，module 不存在。

- [ ] **Step 3: 实现 strict contract 与 capability-only client**

```typescript
export const assetSummarySchema = z.object({
  id: z.string().uuid(),
  scope: z.enum(["system", "project"]),
  kind: z.enum(["agent", "skill", "mcp"]),
  slug: z.string().min(1),
  status: z.enum(["active", "archived", "suspended"]),
  current_published_version_id: z.string().uuid().nullable(),
  pinned_version_id: z.string().uuid().nullable(),
  capabilities: z.array(z.string()),
  request_id: z.string().min(1),
}).strict();
```

所有 API 使用 authenticated fetcher，安全解析公共错误；mutation 成功只失效当前 account/project/kind keys。

- [ ] **Step 4: 运行 Frontend contract tests**

Run: `cd frontend && pnpm test -- tests/unit/core/shared-assets/types.test.ts tests/unit/core/shared-assets/api.test.ts tests/unit/core/shared-assets/query-keys.test.ts`

Expected: PASS，跨 account/project query key 不碰撞。

- [ ] **Step 5: 提交 Frontend data layer**

```bash
git add frontend/src/core/shared-assets frontend/src/core/projects/types.ts frontend/tests/unit/core/shared-assets
git commit -m "feat: add shared asset frontend contracts"
```

---

### Task 12: 实现 `/admin/assets` 平台管理区

**Files:**
- Create: `frontend/src/app/admin/layout.tsx`
- Create: `frontend/src/app/admin/assets/layout.tsx`
- Create: `frontend/src/app/admin/assets/page.tsx`
- Create: `frontend/src/app/admin/assets/agents/page.tsx`
- Create: `frontend/src/app/admin/assets/skills/page.tsx`
- Create: `frontend/src/app/admin/assets/mcp/page.tsx`
- Create: `frontend/src/app/admin/assets/credentials/page.tsx`
- Create: `frontend/src/components/admin/assets/admin-assets-shell.tsx`
- Create: `frontend/src/components/admin/assets/admin-asset-page.tsx`
- Create: `frontend/src/components/assets/asset-version-history.tsx`
- Create: `frontend/src/components/assets/asset-version-diff.tsx`
- Create: `frontend/src/components/assets/asset-status-badge.tsx`
- Test: `frontend/tests/unit/components/admin/admin-assets.test.tsx`
- Test: `frontend/tests/e2e/admin-assets.spec.ts`

**Interfaces:**
- Consumes: `getServerSideUser()`、Task 11 hooks。
- Produces: server-gated platform UI，系统资产版本、发布、归档、suspend、credential replace 与 rotation status。

- [ ] **Step 1: 写 system role gate 与无 secret UI 失败测试**

```typescript
test("admin layout redirects ordinary users", async () => {
  mockServerUser({ system_role: "user" });
  await expect(renderAdminLayout()).rejects.toMatchObject({ digest: expect.stringContaining("NEXT_REDIRECT") });
});

test("credential card renders metadata but no secret controls", () => {
  const html = renderToStaticMarkup(<CredentialCard credential={credential} />);
  expect(html).toContain("替换凭据");
  expect(html).not.toContain("显示明文");
  expect(html).not.toContain("复制密钥");
});
```

- [ ] **Step 2: 运行 admin UI tests 确认失败**

Run: `cd frontend && pnpm test -- tests/unit/components/admin/admin-assets.test.tsx && pnpm exec playwright test tests/e2e/admin-assets.spec.ts`

Expected: FAIL，admin routes/components 不存在。

- [ ] **Step 3: 实现 server layout、共享组件和五个页面**

```tsx
export default async function AdminLayout({ children }: PropsWithChildren) {
  const auth = await getServerSideUser();
  if (auth.tag !== "authenticated") redirect(buildLoginUrl("/admin/assets"));
  if (auth.user.system_role !== "system_admin") notFound();
  return <AdminAssetsShell user={auth.user}>{children}</AdminAssetsShell>;
}
```

所有动作按钮依据 API item capabilities/status，不在 client 端自行推导权限。diff 展示结构化 payload/file checksum 变化，credential 仅展示 name/type/status/version/time。

- [ ] **Step 4: 运行 admin UI tests**

Run: `cd frontend && pnpm test -- tests/unit/components/admin/admin-assets.test.tsx && pnpm exec playwright test tests/e2e/admin-assets.spec.ts`

Expected: PASS，普通用户无法进入，页面无 reveal/copy secret 行为。

- [ ] **Step 5: 提交平台管理 UI**

```bash
git add frontend/src/app/admin frontend/src/components/admin frontend/src/components/assets frontend/tests/unit/components/admin frontend/tests/e2e/admin-assets.spec.ts
git commit -m "feat: add system asset administration"
```

---

### Task 13: 实现项目资产页面、系统绑定和审批 UI

**Files:**
- Create: `frontend/src/app/projects/[project_slug]/agents/page.tsx`
- Create: `frontend/src/app/projects/[project_slug]/skills/page.tsx`
- Create: `frontend/src/app/projects/[project_slug]/mcp/page.tsx`
- Create: `frontend/src/app/projects/[project_slug]/credentials/page.tsx`
- Create: `frontend/src/components/projects/assets/project-assets-page.tsx`
- Create: `frontend/src/components/projects/assets/system-asset-section.tsx`
- Create: `frontend/src/components/projects/assets/project-asset-section.tsx`
- Create: `frontend/src/components/projects/assets/system-binding-dialog.tsx`
- Create: `frontend/src/components/projects/assets/mcp-approval-dialog.tsx`
- Modify: `frontend/src/components/projects/project-nav.tsx`
- Modify: `frontend/src/components/projects/project-shell.tsx`
- Modify: `frontend/src/app/workspace/agents/page.tsx`
- Modify: `frontend/src/app/workspace/skills/page.tsx`
- Modify: `frontend/src/app/workspace/tools/page.tsx`
- Test: `frontend/tests/unit/components/projects/project-assets.test.tsx`
- Test: `frontend/tests/unit/components/projects/project-shell.test.tsx`
- Test: `frontend/tests/e2e/project-assets.spec.ts`

**Interfaces:**
- Consumes: Project context/current capabilities、Task 11 data layer。
- Produces: `/projects/{slug}/agents|skills|mcp|credentials`，明确 system/project badge、version pin、upgrade/rollback、approval，无 run CTA。

- [ ] **Step 1: 写 capability-only、同名资产和无 run CTA 失败测试**

```typescript
test("same-name system and project assets remain separate", () => {
  const html = renderAssets({ systemItems: [systemAnalyst], projectItems: [projectAnalyst] });
  expect(html.match(/Analyst/g)).toHaveLength(2);
  expect(html).toContain("系统");
  expect(html).toContain("项目");
});

test("M3 project pages never render run actions", () => {
  const html = renderAssets(adminFixture);
  expect(html).not.toContain("运行 Agent");
  expect(html).not.toContain("开始对话");
});
```

覆盖 Viewer/Runner 无 edit，Editor 可发布 Agent/Skill/无 credential MCP，Admin 独占 binding/credential approval，系统 upgrade 不自动变化。

- [ ] **Step 2: 运行 project asset UI tests 确认失败**

Run: `cd frontend && pnpm test -- tests/unit/components/projects/project-assets.test.tsx tests/unit/components/projects/project-shell.test.tsx && pnpm exec playwright test tests/e2e/project-assets.spec.ts`

Expected: FAIL，routes/nav/items 尚不存在。

- [ ] **Step 3: 实现四个页面和项目导航**

```tsx
const PROJECT_ASSET_NAV = [
  { capability: "shared_assets.read", label: "Agent", segment: "agents" },
  { capability: "shared_assets.read", label: "Skill", segment: "skills" },
  { capability: "shared_assets.read", label: "MCP", segment: "mcp" },
  { capability: "shared_assets.read", label: "凭据", segment: "credentials" },
] as const;
```

系统区和项目区分别渲染；同名不合并。系统 action 使用 binding capability；项目 action 使用 item capabilities。Editor 可以看到 credential 名称、类型、状态、version 和更新时间，但替换/审批按钮只依据 `mcp.credentials.approve` 显示。MCP Editor 提交后显示“等待 Admin 审批”，不显示可绕过的 publish action。

legacy `/workspace/agents`、`/workspace/skills`、`/workspace/tools` 在 cutover 后只显示 system assets，并提供前往 `/admin/assets` 的管理入口；不再呈现 file-backed 创建、编辑或启用开关。

- [ ] **Step 4: 运行 project asset UI tests**

Run: `cd frontend && pnpm test -- tests/unit/components/projects/project-assets.test.tsx tests/unit/components/projects/project-shell.test.tsx && pnpm exec playwright test tests/e2e/project-assets.spec.ts`

Expected: PASS，所有 M3 项目页面无 run CTA，project/account 切换不显示上一作用域数据。

- [ ] **Step 5: 提交项目资产 UI**

```bash
git add frontend/src/app/projects frontend/src/app/workspace/agents/page.tsx frontend/src/app/workspace/skills/page.tsx frontend/src/app/workspace/tools/page.tsx frontend/src/components/projects frontend/tests/unit/components/projects frontend/tests/e2e/project-assets.spec.ts
git commit -m "feat: add project shared asset pages"
```

---

### Task 14: 完成 cutover 演练、发布门禁与文档同步

**Files:**
- Modify: `.github/workflows/project-foundation-postgres-tests.yml`
- Create: `backend/tests/integration/test_m3_shared_assets_postgres.py`
- Modify: `README.md`
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `backend/AGENTS.md`
- Modify: `frontend/AGENTS.md`
- Modify: `docs/superpowers/specs/2026-07-12-project-first-saas-design.md`
- Modify: `docs/superpowers/specs/2026-07-13-project-shared-assets-m3-design.md`
- Create: `backend/tests/support/m3_shared_assets.py`

**Interfaces:**
- Consumes: Tasks 1–13 全部交付物。
- Produces: `M3Scenario` 测试 helper、M3 PostgreSQL release gate、运维说明、最终 `3/8（37.5%）` 进度更新。

- [ ] **Step 1: 写真实 PostgreSQL 端到端 gate**

```python
async def test_m3_end_to_end_shared_asset_governance(migrated_postgres_database_url):
    scenario = await M3Scenario.create(migrated_postgres_database_url)
    published = await scenario.publish_system_catalog()
    binding = await scenario.bind_system_agent(published.agent_v1)
    await scenario.publish_system_agent_v2()
    assert binding.version_id == published.agent_v1
    assert (await scenario.resolve_bound_agent()).version_id == published.agent_v1
    with pytest.raises(AssetForbidden):
        await scenario.editor_approve_project_mcp()
    with pytest.raises(AssetNotFound):
        await scenario.other_project_read_project_agent()
    await scenario.suspend_bound_system_agent()
    with pytest.raises(AssetResolutionUnavailable):
        await scenario.resolve_bound_agent()
    snapshot = await scenario.resolve_project_mcp_before_revoke()
    assert snapshot.credential_grant_ids
    assert "secret" not in snapshot.to_safe_dict()
    await scenario.revoke_project_credential()
    with pytest.raises(AssetResolutionUnavailable):
        await scenario.resolve_project_mcp()
```

在 `backend/tests/support/m3_shared_assets.py` 实现 `M3Scenario`，公开上述测试实际调用的 `create`、`publish_system_catalog`、`bind_system_agent`、`publish_system_agent_v2`、`resolve_bound_agent`、`editor_approve_project_mcp`、`other_project_read_project_agent`、`suspend_bound_system_agent`、`resolve_project_mcp_before_revoke`、`revoke_project_credential`、`resolve_project_mcp`。helper 只能组合生产 service/repository，不得绕过授权直接修改 M3 表。

此测试使用随机 `deerflow_test_*` database；缺少 `POSTGRES_TEST_URL` 时本地明确 skip，CI 必须硬失败。

- [ ] **Step 2: 运行 M3 focused release gate**

Run: `cd backend && uv run pytest tests/integration/test_m3_shared_assets_postgres.py tests/integration/test_m3_asset_migration_postgres.py tests/test_harness_boundary.py -q`

Expected: PASS，0 failure；若 skip，不能更新 M3 进度。

- [ ] **Step 3: 更新 CI 与文档，但暂不先标完成**

CI job 加入 `test_m3_shared_assets_postgres.py`，名称改为 “M1, M2 and M3 PostgreSQL isolation gates”。README/AGENTS 记录 `make migrate-assets`、`make rotate-credentials`、keyring env、DB authority、M3/M4 boundary 和 `/admin/assets`。

- [ ] **Step 4: 运行完整验证门禁**

Run:

```bash
cd backend
make lint
uv run pytest tests/ -q
cd ../frontend
pnpm check
pnpm test
pnpm exec playwright test
```

Expected: Backend lint clean；Backend/Frontend/Playwright 全部 0 failure。PostgreSQL gate 不得 skip。任何失败必须先修复并重新运行对应完整命令。

- [ ] **Step 5: 演练 migration 与 rotation**

Run:

```bash
make migrate-assets ARGS="--dry-run"
make migrate-assets ARGS="--execute"
make migrate-assets ARGS="--execute"
make rotate-credentials ARGS="--dry-run --key-id m3-next"
```

Expected: 第一次 execute 完成导入与 cutover；第二次为 no-op；rotation dry-run 只报告数量与 key ID，不打印明文。执行前必须通过安全的本地环境配置把一个新的 32-byte key 注册为 `m3-next`，同时保持当前 key 为 active；计划和仓库都不保存 key material。

- [ ] **Step 6: 只在全部门禁通过后更新里程碑**

把总体设计 M3 状态改为“已完成”，当前完成度改为 `M1、M2、M3 已完成（3/8，37.5%）`；保留“项目私有 Thread、run、file、Memory 和 automation 尚未完成，系统不可作为完整多用户 SaaS 发布”。专项设计状态改为“已实施并验证”。

- [ ] **Step 7: 提交发布门禁与文档**

```bash
git add .github/workflows/project-foundation-postgres-tests.yml backend/tests/support/m3_shared_assets.py backend/tests/integration/test_m3_shared_assets_postgres.py README.md AGENTS.md backend/AGENTS.md frontend/AGENTS.md docs/superpowers/specs/2026-07-12-project-first-saas-design.md docs/superpowers/specs/2026-07-13-project-shared-assets-m3-design.md
git commit -m "docs: complete M3 shared asset milestone"
```

---

## 执行顺序与审查门禁

1. Task 1–3 建立 schema、授权和 crypto 基础；任何 Critical/Important 问题修复后再进入 domain service。
2. Task 4、5 可在 Task 1–3 后并行；Task 6 依赖 Task 3；每个任务独立进行 spec compliance review 和 code quality review。
3. Task 7 依赖 Task 4–6；Task 8 依赖 Task 7；Task 9 依赖 Task 7–8。
4. Task 10 依赖 schema、domain service、provider；cutover marker 未验证前禁止删除或停用 file loader。
5. Task 11 可在 Task 8 API contract 稳定后开始；Task 12、13 可在 Task 11 后并行。
6. Task 14 是唯一允许更新 M3 进度的任务；完整门禁未通过时 M3 必须保持“未开始/进行中”。

## 明确禁止

- 不在 M3 增加项目 Thread/run/file/Memory/automation 表或页面。
- 不给项目资产页面增加“运行”“开始对话”或隐式执行入口。
- 不允许 system/project 同名时 shadow 或按 slug 猜测引用。
- 不允许 repository 接受裸 `project_id` 后执行项目写入；必须接受可信 context。
- 不允许 API 返回 ciphertext、nonce、key ID、secret hash 或解密失败细节。
- 不允许 cutover 后读 legacy 文件兜底。
- 不允许 application startup 自动运行 asset migration 或 credential rotation。
