# M3 系统与项目共享资产专项设计

- 日期：2026-07-13
- 状态：已确认，待实施
- 对应总体设计：`2026-07-12-project-first-saas-design.md`
- 里程碑：M3 — Agent、Skill、MCP version 与 credential approval
- 数据库：PostgreSQL-only
- 权限边界：业务服务、强制作用域 repository、数据库约束；不使用 PostgreSQL RLS

## 1. 文档目的

本文档定义 M3 的产品边界、数据模型、授权、版本发布、credential 安全、迁移和完成门禁。M3 将 Agent、Skill、MCP 从现有用户文件和全局配置迁移为 PostgreSQL 中的系统级或项目级共享资产，并交付后续运行所需的精确 version resolver。

M3 不建设项目私有 Thread、run、file、Memory 或 automation。现有 legacy 对话只允许继续执行系统资产；项目资产进入项目私有运行由 M4 完成。

## 2. 已冻结决策

1. Agent、Skill、MCP 分为 `system` 和 `project` 两种 scope。
2. 系统资产对所有项目可见，只能由 `system_admin` 创建、修改、发布、归档和紧急停用。
3. 项目资产只属于一个项目；项目 Admin 和 Editor 可以创建和修改，Runner 和 Viewer 只能按 capability 查看。
4. 当前仓库自带 Agent、`skills/public` 和全局 MCP 配置迁为系统资产。
5. 现有用户自定义 Agent、Skill 迁入该用户所属的默认项目，成为项目资产。
6. 系统资产发布后不会自动在项目启用。项目 Admin 显式创建项目绑定并固定具体系统 version。
7. 系统发布新 version 后，已有项目绑定不会自动升级；项目 Admin 显式升级或回退。
8. 系统与项目资产允许同名，但不覆盖。所有引用使用 asset UUID、version UUID 和 scope，UI 明确显示来源。
9. 项目 Agent、Skill 和不使用 credential 的 MCP 可由 Admin 或 Editor 直接发布。
10. 使用项目 credential 的 MCP 由 Editor 起草，必须由项目 Admin 审批后发布。
11. 系统 MCP 的 credential 属于系统 scope，由 `system_admin` 管理；启用该 MCP 的项目共用系统 credential。
12. 项目 MCP 的 credential 属于项目 scope，只能绑定同一项目的 MCP version。
13. credential 是独立资源，可以在同一 scope 内被多个 MCP version 通过显式 grant 复用。
14. `system_admin` 可以治理所有项目的共享资产和 credential/grant，不要求加入项目；该 override 不扩展到成员管理和任何用户私有内容。
15. 系统和项目资产都区分 `archived` 与 `suspended`。归档禁止新绑定或升级，既有绑定继续可用；紧急停用使所有解析立即失败。
16. credential revoke 立即使关联 grant 失效。
17. PostgreSQL 是资产运行时唯一权威来源。仓库文件只作为系统种子和灾备重建来源，不长期双写。
18. 使用显式、幂等的 trusted migration 脚本切换，不在应用启动时自动迁移。
19. credential 使用 AES-GCM 和带 `key_id` 的环境主密钥环加密，支持 dry-run 和断点续跑的显式轮换。
20. M3 交付 resolver 和 legacy 系统资产适配，不允许项目资产进入项目私有运行；后者属于 M4。

## 3. 目标与非目标

### 3.1 目标

- 在数据库中建立系统级和项目级 Agent、Skill、MCP 的逻辑资产与不可变 version。
- 强制系统 scope、项目 scope、version、binding、credential 和 grant 的数据库一致性。
- 提供系统资产平台管理区和项目资产管理页面。
- 提供 version 历史、结构化差异、发布、审批、归档、紧急停用、升级和回退。
- 让项目显式启用系统资产并固定 version。
- 加密保存系统和项目 credential，API 永不返回 secret 明文。
- 迁移现有资产并把 legacy 系统资产运行加载器切换到 PostgreSQL。
- 为 M4 提供可验证、fail-closed 的精确 version resolver。

### 3.2 非目标

- 项目私有对话、运行、检查点、文件、产物、Memory 或 automation。
- 在项目页面启动 Agent 或 MCP 调用。
- 将项目资产暴露给 legacy 无项目上下文的对话。
- 自定义项目角色或资源级 ACL。
- system credential 的项目级覆盖。
- 跨项目复制、安装或市场。
- 外部 KMS、Vault、Redis 或消息总线。
- 完整审计查询、用量、配额和告警产品；这些属于 M6。
- 删除 legacy 文件；旧文件仅保留为备份，最终清理属于 M7。

## 4. 术语与作用域

- **逻辑资产**：具有稳定 UUID、slug、显示名称、scope 和生命周期的 Agent、Skill 或 MCP。
- **asset version**：逻辑资产内容的不可变快照，具有独立 UUID、递增 version number 和 checksum。
- **系统资产**：`scope=system` 且 `project_id IS NULL`，所有项目可见。
- **项目资产**：`scope=project` 且 `project_id` 非空，只属于一个项目。
- **系统绑定**：项目对某个系统资产具体 published version 的显式启用记录。
- **credential**：可命名和替换的逻辑 secret 资源。
- **credential version**：一次 secret 内容替换形成的不可变语义版本。
- **credential envelope**：同一 credential version 在某个主密钥下的 AES-GCM 密文表示。
- **grant**：某个 MCP version 对某个 credential version 的显式授权。
- **平台治理 override**：`system_admin` 对系统资产和任意项目共享资产的治理权限，不创建虚假项目 membership。

## 5. 数据模型

M3 使用 Agent、Skill、MCP 三套 typed table 和对应 domain service，不建设通用 JSONB asset registry。三类模型共享 scope、version、binding 和授权规则，但各自内容由类型化列、子表和校验器表达。

### 5.1 通用 scope 约束

Agent、Skill、MCP 的逻辑资产表都包含：

- `id UUID PRIMARY KEY`
- `scope VARCHAR(16)`：`system|project`
- `project_id UUID NULL`
- `slug` 或 `name`
- `display_name`
- `status`：`active|archived|suspended`
- `current_published_version_id UUID NULL`
- `version BIGINT`：optimistic concurrency
- `source_key VARCHAR(...) NULL`
- `created_by_user_id`
- `created_at`、`updated_at`

数据库 CHECK 必须保证：

```text
(scope = 'system' AND project_id IS NULL)
OR
(scope = 'project' AND project_id IS NOT NULL)
```

系统 slug 使用 partial unique index 保证全局唯一；项目 slug 使用 `(project_id, lower(slug))` partial unique index 保证项目内唯一。`source_key` 只用于 migration 幂等，按 source type 唯一，不作为授权依据。

credential 使用相同的 `scope + project_id` CHECK 和 scope 内名称唯一约束，但使用独立的 `status` 与 `current_version_id` 字段，不套用资产发布状态。

### 5.2 Agent

表：

- `agents`
- `agent_versions`
- `agent_version_skill_refs`
- `agent_version_mcp_refs`

`agent_versions` 保存：description、SOUL、model reference、tool group 配置、workflow status、version number、`supersedes_version_id`、payload checksum 和创建元数据。

Agent 依赖始终固定到明确的 Skill/MCP version UUID：

- 系统 Agent 只能引用系统 Skill/MCP version；
- 项目 Agent 可以引用同项目 published version，或本项目已经绑定的系统 version；
- 发布时重新验证依赖仍可用；
- archived 的依赖可以服务已有 published version，但不能被新 version 引用；
- suspended 依赖使 resolver 立即失败。

### 5.3 Skill

表：

- `skills`
- `skill_versions`
- `skill_version_files`

Skill version 是完整目录快照，不只保存 `SKILL.md`。每个文件保存规范化相对路径、媒体类型、大小、SHA-256 和内容。禁止绝对路径、`..`、符号链接和可执行二进制；复用现有静态与 LLM security scan。单个 Skill version 的未压缩内容上限固定为 100 MiB。

`skill_versions` 保存 frontmatter 规范化结果、描述、兼容性、scan decision、汇总 checksum 和 workflow 元数据。前端只能显示经过脱敏的 scan 结果。

M3 credential grant 不覆盖 Skill secret requirement。Skill 可以保留 requirement 元数据，但在 M4 之前不能通过项目运行 materialize secret。

### 5.4 MCP

表：

- `mcp_servers`
- `mcp_server_versions`
- `mcp_version_credential_slots`

MCP version 保存无 secret 的 definition：transport、command、args、URL、非敏感 env/header、OAuth 非 secret 元数据、routing、tool override 和 timeout。任何识别为 secret 的 env、header、OAuth 字段必须拆入 credential payload，不能写入 MCP version。

credential slot 定义 version 所需的命名绑定、用途和允许的 payload schema。发布时必须验证必需 slot 已有有效 grant。

### 5.5 项目系统资产绑定

表：

- `project_system_agent_bindings`
- `project_system_skill_bindings`
- `project_system_mcp_bindings`

每条绑定包含 `project_id`、system asset ID、固定 published version ID、`enabled`、optimistic `version`、操作者和时间。复合外键必须证明 version 属于对应 system asset；CHECK 必须拒绝 project asset。

一个项目对一个系统逻辑资产最多一条绑定。升级和回退只移动绑定的 version pointer，不修改系统资产。

### 5.6 Credential 与 grant

表：

- `credentials`
- `credential_versions`
- `credential_envelopes`
- `credential_grants`

`credential_versions` 表示 secret 的语义版本；替换 secret 必须新建 credential version。grant 固定到 credential version，不自动跟随逻辑 credential 的 current pointer。

`credentials.status` 使用 `active|revoked`；`credential_versions.status` 使用 `active|retired|revoked`。逻辑 credential revoke 和 credential version revoke 都不可逆；正常替换只把旧 credential version 标为 `retired`。retired version 禁止创建新 grant，但既有 grant 继续有效，直到显式切换或 revoke。

`credential_envelopes` 保存：

- `credential_version_id`
- `envelope_generation`
- `key_id`
- 12-byte nonce
- ciphertext + authentication tag
- `is_active`
- 创建与轮换元数据

同一 credential version 只能存在一个 active envelope。主密钥轮换新建 envelope，验证可解密后切换 active 标记；不改变 credential version ID，也不改变 grant。

AES-GCM AAD 至少绑定：credential version UUID、scope、project UUID 或 system sentinel、payload schema version。credential payload 只允许结构化 `env`、`headers` 和 `oauth` secret 字段，总明文大小上限 64 KiB。

grant 复合约束必须保证：

- 系统 MCP version 只能绑定系统 credential version；
- 项目 MCP version 只能绑定同一项目 credential version；
- grant 指向 version 声明的 slot；
- revoked credential、revoked credential version 或 revoked grant 不能解析；retired credential version 只允许解析替换前已经存在的 grant。

## 6. Version 与生命周期状态机

### 6.1 Version 内容不可变

不可变字段包括 payload、文件内容、checksum、version number、父 version 和创建者。数据库 trigger 拒绝对这些字段执行 UPDATE；被 publication、binding、grant 或未来 run snapshot 引用的 version 禁止 DELETE。

workflow status 允许有限更新：

```text
draft -> published
draft -> pending_approval
pending_approval -> published
pending_approval -> rejected
```

任何内容修改都新增 version。旧 draft 可以保留在历史中，但不允许修改或发布一个已被更新 draft 取代且不再满足 expected version 的记录。

### 6.2 逻辑资产生命周期

```text
active -> archived
active -> suspended
archived -> active
archived -> suspended
suspended -> active|archived
```

- `active`：允许查看、发布和新绑定。
- `archived`：禁止新 version 发布、新项目绑定和绑定升级；既有绑定与历史引用继续解析。
- `suspended`：所有 resolver 立即失败，包括已有绑定。

普通归档和恢复使用 optimistic version。紧急停用由对应 scope 的治理者执行：系统资产只允许 `system_admin`；项目资产允许项目 Admin 或 `system_admin` override。

### 6.3 Credential 生命周期

- secret 替换创建新 credential version；已有 grant 继续固定旧 version，直到显式改 grant。
- credential revoke 立即使所有关联 grant 解析失败。
- key rotation 只改变 active envelope，不改变 secret 语义 version。
- API 不支持读取、导出或恢复 secret 明文。

## 7. 授权模型

### 7.1 项目成员 capability

- `shared_assets.read`：查看系统和项目资产、published version 与安全元数据。
- `shared_assets.edit`：创建项目资产、创建新 version、发布 Agent/Skill/无 credential MCP。
- `shared_assets.manage_bindings`：启用、升级、回退和关闭系统资产绑定；仅项目 Admin。
- `mcp.credentials.approve`：创建/替换项目 credential、审批项目 grant；仅项目 Admin。
- `shared_assets.execute`：预留给 M4 resolver 调用；M3 不创建项目运行。

前端只消费 Gateway 返回的 capabilities，禁止根据 role 推导。

### 7.2 平台治理 override

`system_admin`：

- 管理全部系统资产和系统 credential；
- 查看、创建、修改、发布、归档、紧急停用任意项目共享资产；
- 创建、替换、审批或 revoke 任意项目 credential/grant；
- 不需要项目 membership；
- 不能通过该 override 读取成员、私有 Thread、run、file、Memory、automation 或私有内容。

平台 override 使用显式 `SystemAssetGovernanceContext`，不能伪造 `ProjectContext`。所有 override 写入最小治理事件接口，包含操作者、项目、资产、version、动作和 `request_id`，供 M6 接入正式审计。

## 8. 发布与审批流程

### 8.1 Agent、Skill 和无 credential MCP

1. 锁定逻辑资产并校验 expected version。
2. 分配下一个 version number，插入不可变 draft。
3. 运行类型校验、依赖校验、checksum 和安全扫描。
4. Admin 或 Editor 发布项目 version；系统 version 只允许 `system_admin`。
5. 在同一事务更新 workflow status 和逻辑资产 current published pointer。

### 8.2 使用项目 credential 的 MCP

1. Editor 创建无 secret MCP version，并选择 credential 元数据和 slot。
2. 提交后进入 `pending_approval`。
3. 项目 Admin 或 `system_admin` override 锁定 MCP asset、MCP version、credential、credential version 和 grant。
4. 校验 scope、状态、slot 和 expected version。
5. 创建 grant，发布 MCP version，并移动 current published pointer。

固定锁序：

```text
project -> asset -> asset version -> credential -> credential version -> grant
```

系统 MCP 由 `system_admin` 直接完成同一流程，不需要项目审批。

## 9. 系统资产项目绑定

所有系统资产对有效项目成员可见，但只有项目 Admin 或 `system_admin` override 可以绑定。

绑定创建、升级或回退时必须：

1. 锁定 project；
2. 锁定绑定行或确认不存在；
3. 验证 system asset 和目标 version；
4. 拒绝 project asset、draft、pending、rejected、archived 新绑定或 suspended 资产；
5. 对 Agent 验证其固定依赖 version 在项目中可用；
6. 对 MCP 验证 credential grant 可用；
7. 使用 optimistic version 写入。

系统新 version 发布后只显示“可升级”，不自动修改任何绑定。

## 10. API 设计

### 10.1 平台管理 API

系统资产前缀：`/api/admin/assets`

- `/agents`
- `/skills`
- `/mcp-servers`
- `/credentials`
- `/{kind}/{asset_id}/versions`
- `/{kind}/{asset_id}/publish`
- `/{kind}/{asset_id}/archive`
- `/{kind}/{asset_id}/suspend`
- `/credentials/{credential_id}/versions`
- `/credentials/{credential_id}/rotate`

项目共享资产 override 使用独立前缀 `/api/admin/projects/{project_id}/assets`，复用对应 Agent、Skill、MCP 和 credential/grant 操作，不伪装成项目成员请求。

所有平台管理端点只接受 `system_admin`。项目 override 响应必须返回 `actor_scope=system_override`。

### 10.2 项目 API

前缀：`/api/projects/{project_id}`

- `/agents`
- `/skills`
- `/mcp-servers`
- `/credentials`
- `/{kind}/{asset_id}/versions`
- `/{kind}/{asset_id}/publish`
- `/mcp-servers/{asset_id}/submit-approval`
- `/mcp-servers/{asset_id}/approve`
- `/system-agent-bindings`
- `/system-skill-bindings`
- `/system-mcp-bindings`

列表分别返回 `system_items` 和 `project_items`，每项都包含 `scope`、asset UUID、published/pinned version UUID、状态、binding 状态、capabilities 和 `request_id`。同名项独立返回，不合并。

API 永不返回 credential ciphertext、nonce、key ID、secret hash 或明文。Editor 只能获得 credential ID、名称、类型、状态、version number、更新时间和 slot 兼容性。

### 10.3 错误语义

- 跨项目、错误 scope、错误 asset/version 组合：`404`
- 当前项目内缺 capability：`403`
- version、状态、发布、审批、binding 竞争：`409`
- payload、依赖、slot 或 version 引用非法：`422`
- keyring、数据库或 resolver 暂时不可用：`503`

错误响应包含稳定公共错误码和 `request_id`，不包含 SQL、路径、配置、secret 或解密原因。

## 11. Resolver 与运行边界

M3 提供：

```python
resolve_project_asset_snapshot(
    context: ProjectContext,
    selection: AssetSelection,
) -> ResolvedAssetSnapshot
```

规则：

- 项目资产解析本项目 current published version；
- 系统资产解析本项目绑定固定的 version；
- archived 的既有 published version 或 binding 可继续解析；
- suspended asset、revoked credential、无效 grant、错误 scope 或缺失依赖 fail closed；
- 返回精确 version UUID、checksum、依赖 version UUID 和 credential/grant 安全引用。

普通 resolver 不解密 secret。内部接口：

```python
materialize_mcp_secrets(
    resolved: ResolvedMcpSnapshot,
) -> MaterializedMcpSecrets
```

只在后续执行前短暂解密 env/header/OAuth secret。明文不得进入 API、日志、异常、全局 cache、checkpoint、run event、审计或文件。

M3 只测试该接口，不从项目页面创建运行。M4 在创建 run snapshot 时调用 resolver，并把实际 version UUID 写入 run。

## 12. Legacy 兼容运行

现有 `/workspace/agents` 和 legacy 对话在 M4 前继续存在，但只能解析系统 published asset。项目资产必须被拒绝。

为保持 harness/app 依赖方向：

- harness 定义只读 `AssetCatalogProvider` 协议和安全 snapshot 类型；
- app 提供 PostgreSQL 实现并在 Gateway runtime 装配；
- harness 不导入 `app.*`；
- catalog cache 以数据库 generation 为键，发布、停用、grant revoke 后失效；
- cutover marker 生效后不读取仓库 Agent、Skill 或 MCP 文件作为 fallback。

legacy 运行兼容不代表项目运行完成。项目页面不提供 run CTA，项目私有 CTA 继续禁用。

## 13. Frontend 信息架构

### 13.1 平台管理区

- `/admin/assets`
- `/admin/assets/agents`
- `/admin/assets/skills`
- `/admin/assets/mcp`
- `/admin/assets/credentials`

server layout 必须先验证 `system_admin`。页面提供系统资产列表、版本历史、结构化 diff、发布、归档、紧急停用、credential 替换和 key rotation 状态。

### 13.2 项目资产页面

- `/projects/{slug}/agents`
- `/projects/{slug}/skills`
- `/projects/{slug}/mcp`
- `/projects/{slug}/credentials`

项目菜单只在 M3 页面实现后增加入口。每页分别展示系统与项目区域，并显示 `系统`、`项目` badge。系统项展示绑定 version、可升级 version 和启用状态；项目项展示 draft、pending、published 和历史。

所有按钮只依据服务端 capabilities。Editor 可以查看 credential 安全元数据和发起审批，不能替换、批准或读取 secret。Runner、Viewer 不显示编辑入口。

M3 页面不提供运行按钮。现有 legacy `/workspace/agents`、`/workspace/skills` 和 MCP 入口变为系统资产兼容视图，不承担项目资产编辑。

## 14. Credential 密钥与轮换

环境变量：

- `DEER_FLOW_CREDENTIAL_ACTIVE_KEY_ID`
- `DEER_FLOW_CREDENTIAL_KEYRING_JSON`

keyring JSON 的 value 必须解码为 32-byte key。配置日志只报告 key ID 数量和 active key ID，不打印 key、长度错误原值或 JSON。

显式脚本：`make rotate-credentials`，支持：

- `--dry-run`
- 目标 `key_id`
- batch size
- resume cursor
- 每批独立事务
- 解密、schema、重新加密和抽样复验
- 前后数量与失败台账

rotation 为同一 credential version 新建 envelope；验证成功后切换 active envelope。旧 envelope 在确认窗口内保留为 retired，不参与正常解析。未知 key ID、认证失败或 payload schema 失败统一安全失败，不自动尝试猜测。

## 15. 资产迁移与 cutover

显式命令：`make migrate-assets`。

### 15.1 来源

- 仓库自带 Agent 和默认 Agent 配置：系统 Agent；
- `skills/public`：系统 Skill；
- 全局 `extensions_config.json` MCP：系统 MCP 和系统 credential；
- 用户目录中的 custom Agent、Skill：默认项目的项目资产；
- legacy shared custom 目录：必须在预检中明确归属，不能静默归入任意项目。

### 15.2 幂等与安全

- 首先只读 inventory，输出脱敏 source、scope、目标项目、checksum 和冲突；
- 用 `source_key + checksum` 判断已导入内容；
- 相同 checksum 重跑为 no-op；不同 checksum 新建 version，禁止覆盖已发布内容；
- MCP secret 读取后直接加密，不写临时明文文件；
- 日志不打印 env/header/OAuth value；
- 未找到默认项目、用户映射、active key、依赖或名称冲突时 fail closed；
- 原文件在迁移前备份，M3 不删除。

### 15.3 Cutover marker

迁移完成后校验：资产数量、version 链、文件 checksum、依赖、binding、credential 可解密性和 scope。全部通过才写入 `asset_catalog_cutover` marker。

marker 前 legacy loader 继续原行为；marker 后 loader 只读取 PostgreSQL。启动检测到 marker 与 schema/数据不一致时立即失败，不回退文件。

## 16. 并发与事务

- version number：锁定逻辑资产后分配。
- 发布：锁 asset，校验 expected version，再更新 published pointer。
- MCP 审批：`project -> asset -> asset version -> credential -> credential version -> grant`。
- 系统绑定：`project -> binding -> system asset -> system version`。
- credential replace：锁 credential，创建新 semantic version 和 envelope，再移动 current pointer。
- key rotation：按 credential version UUID 排序、批量 `FOR UPDATE SKIP LOCKED`，避免多 worker 重复处理。
- archive、suspend、revoke 与 resolver 并发时，resolver 在返回 snapshot 前重新验证状态。

任何 DBAPI 错误映射为稳定安全错误；编程错误不得伪装成数据库不可用。

## 17. 测试与发布门禁

### 17.1 真实 PostgreSQL

- 空库升级和 M2 数据库升级；
- downgrade 的明确拒绝或安全策略；
- scope/project CHECK、partial unique index、复合外键；
- 系统和项目同名资产并存；
- 跨项目读写、分页、搜索、发布、审批、binding 和 credential/grant 统一隔离；
- immutable payload 无法 UPDATE，已引用 version 无法 DELETE；
- 并发 version number、发布、审批、binding 升级和 credential replace；
- archived 旧绑定继续解析；suspended/revoke 立即失败；
- 系统新 version 不自动改变项目 binding；
- `system_admin` override 可治理共享资产但不能访问私有 repository；
- credential 加密、错误 key、tamper、轮换、断点续跑和零明文泄漏；
- migration 重跑不重复资产、version、file 或 credential；
- cutover 前后 loader 行为和 fail-closed marker。

所有 PostgreSQL 测试使用随机 `deerflow_test_*` 数据库；缺 `POSTGRES_TEST_URL` 时本地明确 skip，CI 硬失败。

### 17.2 Backend

- capability 与平台 override 矩阵；
- 稳定错误码和 `request_id`；
- Agent dependency closure；
- Skill archive 路径、大小、binary 和 security scan；
- MCP secret 拆分、slot/grant 和 resolver；
- cache generation 与停用/revoke 后失效；
- harness 不导入 app 的边界测试；
- 日志、异常和响应 secret 扫描。

### 17.3 Frontend

- `/admin/assets` 的 system role 门禁；
- 项目系统/项目双区域与同名 badge；
- capability-only 操作显示；
- Editor 发布与 credential MCP 审批流；
- 系统 binding 固定、升级、回退和冲突；
- version diff 与 credential 安全元数据；
- account/project 切换后的 query cache 隔离；
- 所有 M3 项目页无 run CTA。

### 17.4 最终门禁

- Backend lint 和全量测试；
- M1、M2、M3 PostgreSQL isolation gate；
- Frontend check、全量 unit 和 Playwright；
- migration dry-run、重复执行和 cutover 演练；
- credential rotation dry-run 与故障恢复演练；
- 独立整分支安全审查无 Critical 或 Important。

## 18. 完成标准

满足以下条件后才把 M3 标为已完成：

- 系统和项目 Agent、Skill、MCP 的数据库模型、API 和 UI 完成；
- version 内容不可变，发布、审批、binding 和回退状态机成立；
- 系统与项目 credential 加密、grant 和轮换完成；
- `system_admin` 平台治理 override 不泄露私有内容；
- 系统资产迁移和用户自定义资产默认项目迁移完成；
- cutover 后数据库是唯一运行时权威来源；
- legacy 对话只解析系统资产，项目资产不能被无项目上下文执行；
- resolver 返回精确 version snapshot，M4 可以直接消费；
- PostgreSQL、Backend、Frontend 和安全门禁全部通过；
- README、AGENTS、总体设计和运维说明同步。

完成后总体进度更新为 M1、M2、M3 已完成（3/8，37.5%），但必须继续声明：项目私有 Thread、run、file、Memory 和 automation 尚未完成，系统仍不可作为完整多用户 SaaS 发布。
