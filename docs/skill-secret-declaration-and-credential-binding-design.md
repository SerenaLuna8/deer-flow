# Skill 凭证环境变量声明与发布绑定改造方案

状态：Implemented
日期：2026-08-19
范围：Project Skill、压缩包创建、AI Skill Builder、版本发布、Credential 绑定
实现状态：本轮已完成并通过文末门禁

## 1. 背景

Project Skill 可以在根目录 `SKILL.md` frontmatter 中通过
`required-secrets` 声明运行时需要的敏感环境变量，并通过项目 Credential
绑定精确 Credential version。当前界面能够显示已发布版本解析出的环境变量，
但缺少以下完整用户流程：

1. 通过表单声明或修改环境变量。
2. 在表单和 `SKILL.md` 源码之间安全双向同步。
3. 发布版本时为声明选择项目 Credential。
4. 保证活跃 Skill 的新版本和 Credential 绑定原子切换。
5. 在压缩包创建和 AI Skill Builder 创建完成后引导配置凭证。

## 2. 已确认事实

1. 已发布 SkillVersion 是不可变版本，不能在发布后修改其 `SKILL.md` 字节。
2. `skill_versions.frontmatter` 和 `skill_versions.secret_requirements` 已保存版本级解析结果。
3. `project_skill_credential_configs` 和
   `project_skill_credential_bindings` 已按精确 `skill_version_id` 建模。
4. 现有数据模型允许在同一未提交事务中为目标 Draft 写入 binding，再将该版本发布。
5. 压缩包创建和 AI Builder create 当前创建 `published v1 + suspended Skill`。
6. 当前运行时只在执行命令时解密并注入绑定的环境变量；Credential 明文不应进入
   prompt、工具参数、浏览器缓存、日志、审计或版本快照。
7. 当前 frontmatter 解析分散在 parser、validation、review analyzer 和
   SkillService 中，错误处理和严格程度不完全一致。

## 3. 关键设计决策

### 3.1 `SKILL.md` 是唯一数据源

环境变量表单只是当前 `SKILL.md` 编辑副本的结构化投影，不建立独立数据库真源。

```text
SKILL.md 编辑副本
  ↕ 服务端 canonical parse/patch
环境变量表单
  ↓ 保存
不可变 Draft SkillVersion
  ↓ 原子发布与绑定
Published SkillVersion + exact Credential bindings
```

表单修改先回写当前编辑副本，保存时创建新 Draft，发布时冻结最终文件字节。
界面文案使用“已同步到当前编辑副本”，不能声称已经写入已发布版本。

### 3.2 仅管理敏感环境变量

本功能管理 `required-secrets` 中的敏感环境变量名及 Credential 绑定，不管理变量值。
普通非敏感运行配置继续通过运行环境配置管理，不写入 Credential。

### 3.3 服务端拥有 YAML 语义

前端不再实现另一套 YAML parser。所有 parse、diagnostic 和 patch 语义由一个纯
harness 模块提供，并被上传、Builder、版本编辑、检查、发布和运行时解析共同复用。

### 3.4 保持当前创建生命周期

第一阶段不把压缩包创建和 AI Builder create 改为 Draft：

- 创建结果仍为已发布 v1。
- Skill 仍默认 suspended。
- 有声明时，创建完成后自动引导配置 Credential。
- 没有声明但需要新增声明时，通过“创建新版本”生成 Draft 再编辑。

这避免同时改变创建 API、权限和生命周期。上传前在线修改归档不属于第一阶段。

### 3.5 发布与绑定必须原子提交

前端不得使用“先发布，再 PUT credential-bindings”模拟发布流程。对现有 Active
Skill 发布新版本时，live pointer 和新版本 Credential bindings 必须在一个数据库
事务中提交。

## 4. 用户流程

### 4.1 编辑已有 Skill

1. 用户打开 Skill 版本工作台。
2. 工作台读取当前工作副本中的根 `SKILL.md`。
3. 服务端解析 `required-secrets` 和 `secrets-autonomous`。
4. 用户在“凭证环境变量”页签增加、修改或删除声明。
5. 服务端 patch 返回新的 `SKILL.md` 内容，写入现有本地 changes buffer。
6. 用户可以切换到源码视图查看差异。
7. 用户保存为新的不可变 Draft。
8. 用户点击发布，选择每个声明对应的 Credential version。
9. 后端原子创建版本级绑定并发布目标 Draft。

### 4.2 上传压缩包

1. 用户上传 Skill archive。
2. 后端使用 canonical parser 分析根 `SKILL.md`。
3. 后端创建 `published v1 + suspended Skill`。
4. 如果存在 `required-secrets`，前端自动打开新 Skill 详情并滚动到凭证配置区域。
5. 用户选择或创建项目 Credential。
6. 必需绑定完整后，具有相应权限的用户启用 Skill。

上传包未声明环境变量时不凭空推断变量；用户需要创建新版本后通过表单声明。

### 4.3 AI Skill Builder

1. AI 生成候选文件包。
2. 候选工作台自动解析根 `SKILL.md`。
3. 用户可在“凭证环境变量”页签调整声明。
4. 表单修改写回 Builder 当前候选 `SKILL.md`。
5. 修改会使旧 validation 失效，用户必须重新执行“检查 Skill”。
6. create 会话提交后继续创建 `published v1 + suspended Skill`。
7. 如果存在声明，成功页提供“配置凭证”主操作并打开精确 Skill。
8. revise 会话继续创建 Draft，发布时进入 Credential 选择流程。

## 5. Frontmatter 合同

### 5.1 支持格式

```yaml
---
name: example-skill
description: Example Skill
required-secrets:
  - name: "OPENAI_API_KEY"
    optional: false
  - name: "SECONDARY_TOKEN"
    optional: true
secrets-autonomous: true
---
```

兼容读取历史 string shorthand；仅当用户实际修改 `required-secrets` 时，才将托管
区段规范化为 mapping 形式。

### 5.2 校验规则

- 环境变量名匹配 `^[A-Za-z_][A-Za-z0-9_]*$`。
- 名称区分大小写且不得重复。
- `optional` 必须是真正的 boolean。
- `secrets-autonomous` 必须是真正的 boolean。
- 环境变量名输出时始终使用双引号，避免 YAML 1.1 隐式类型转换。
- 沿用现有 frontmatter 字节数、节点数、深度和 alias 限制。
- 在确认历史 Project/System Skill 不存在超长名称前，不直接新增更严格的长度限制。

### 5.3 Patch 保真规则

- 只修改顶层 `required-secrets` 和 `secrets-autonomous`。
- 保留其他 frontmatter 字段、Markdown 正文、换行风格和托管区段外注释。
- 不使用 `yaml.safe_dump()` 重写整个文档。
- 使用 YAML node marks 定位顶层字段的源文本 span。
- patch 后重新执行 canonical parse，并断言投影与请求语义一致。
- 托管字段内部包含无法安全归属的复杂注释时，返回 `patchable=false`；表单只读并引导源码编辑。

### 5.4 Diagnostic

```json
{
  "code": "duplicate_env_name",
  "severity": "error",
  "field_path": ["required-secrets", 1, "name"],
  "line": 8,
  "column": 11,
  "public_message": "Environment variable names must be unique"
}
```

Diagnostic 不包含原始源码行、非法字段值、Credential 值、文件系统路径或原始 YAML
异常文本。

## 6. 后端模块设计

### 6.1 新增模块

```text
backend/packages/harness/deerflow/skills/frontmatter.py
backend/packages/harness/deerflow/skills/frontmatter_patch.py
backend/app/shared_assets/skill_frontmatter_service.py
backend/app/shared_assets/skill_credential_policy.py
```

职责：

- `frontmatter.py`：唯一解析、类型、限制和 diagnostics。
- `frontmatter_patch.py`：受管字段的字节保真 patch。
- `skill_frontmatter_service.py`：权限、请求大小、no-store API 和安全错误映射。
- `skill_credential_policy.py`：声明、binding 输入、eligibility 和必需项完整性纯策略。

### 6.2 修改模块

```text
backend/packages/harness/deerflow/skills/parser.py
backend/packages/harness/deerflow/skills/validation.py
backend/packages/harness/deerflow/skills/review/analyzer.py
backend/app/shared_assets/skill_service.py
backend/app/shared_assets/skill_credential_service.py
backend/app/shared_assets/skill_credential_repository.py
backend/app/shared_assets/errors.py
backend/app/gateway/routers/project_assets.py
backend/app/shared_assets/__init__.py
```

改造目标：

- parser、validation、review、archive preview 和 publish 统一调用 canonical parser。
- `SkillService.publish()` 拆分 prepare 和 commit 阶段。
- Credential repository 支持锁定精确 SkillVersion、CredentialVersion 和 active envelope。
- Credential service 支持精确版本读取，并阻止 Active Skill 移除必需绑定。
- Router 增加 parse、patch、publish-plan contract 和结构化错误映射。

### 6.3 无需修改

按当前模型，本方案不需要修改：

```text
backend/packages/harness/deerflow/persistence/shared_assets/skill_model.py
backend/packages/harness/deerflow/persistence/shared_assets/skill_credential_model.py
backend/packages/harness/deerflow/persistence/full_schema.sql
backend/migrations/
backend/app/final_schema.py
```

因此不需要数据库 migration，也不修改 packaged System Asset 内容。

## 7. API 设计

### 7.1 解析编辑副本

```http
POST /api/projects/{project_id}/skills/frontmatter/parse
```

请求：

```json
{
  "content": "...SKILL.md...",
  "source_sha256": "64-lowercase-hex"
}
```

响应：

```json
{
  "source_sha256": "...",
  "valid": true,
  "patchable": true,
  "projection": {
    "required_secrets": [
      {"name": "OPENAI_API_KEY", "optional": false}
    ],
    "secrets_autonomous": true,
    "secrets_autonomous_explicit": true
  },
  "diagnostics": [],
  "request_id": "..."
}
```

YAML 语法或语义错误返回 HTTP 200 和 `valid=false`，使用户能够继续保留和修复当前
源码；权限、请求体和大小错误继续使用相应 HTTP 错误。

### 7.2 Patch 编辑副本

```http
POST /api/projects/{project_id}/skills/frontmatter/patch
```

请求：

```json
{
  "content": "...SKILL.md...",
  "source_sha256": "...",
  "required_secrets": [
    {"name": "OPENAI_API_KEY", "optional": false}
  ],
  "secrets_autonomous": true
}
```

响应返回：

- 原 source SHA。
- result SHA。
- 修改后的完整 `content`。
- 是否发生变化。
- 最新 projection 和 diagnostics。

source SHA 不匹配返回 409。不可安全 patch 或请求声明非法返回 422。

两条接口必须设置：

```text
Cache-Control: private, no-store
X-Content-Type-Options: nosniff
```

服务端不得记录请求中的完整 `SKILL.md`。

### 7.3 获取发布计划

```http
GET /api/projects/{project_id}/skills/{skill_id}/versions/{version_id}/publish-plan
```

响应：

```json
{
  "skill_id": "...",
  "skill_version_id": "...",
  "asset_version": 8,
  "payload_checksum": "...",
  "binding_revision": 0,
  "secrets_autonomous": true,
  "requirements": [
    {
      "name": "OPENAI_API_KEY",
      "optional": false,
      "suggested_credential_version_id": null,
      "eligible_credentials": [
        {
          "credential_id": "...",
          "credential_version_id": "...",
          "display_name": "OpenAI production",
          "version_number": 3
        }
      ]
    }
  ],
  "request_id": "..."
}
```

服务端负责 eligibility，前端不能根据 Credential catalog 自行推断。

### 7.4 发布版本

扩展现有 Skill publish 请求：

```json
{
  "expected_asset_version": 8,
  "expected_payload_checksum": "...",
  "expected_binding_revision": 0,
  "acknowledge_stale_base": false,
  "credential_bindings": [
    {
      "name": "OPENAI_API_KEY",
      "credential_version_id": "..."
    }
  ]
}
```

字段为向后兼容的增量扩展；无 secret Skill 的旧客户端请求仍可发布。响应继续使用现有
`SkillVersionResponse`。

现有 current-published Credential GET/PUT 接口继续保留，用于发布后的安全轮换。

## 8. 原子发布事务

### 8.1 权限

- 始终要求 `shared_assets.manage_bindings`。
- 请求包含 Credential 选择时，额外要求 `mcp.credentials.approve`。
- 所有 Skill、Version 和 Credential 必须属于服务端签发的同一 ProjectContext。

### 8.2 固定锁序

```text
Project + Membership
→ Skill
→ target SkillVersion + files
→ target Credential config/bindings
→ Credential rows（UUID 稳定排序）
→ CredentialVersion rows（稳定排序）
→ active envelope rows（稳定排序）
→ insert config/bindings
→ publish pointer
```

### 8.3 事务步骤

1. 校验 expected asset version、payload checksum、Draft workflow 和 lineage。
2. 对目标文件重新执行 canonical parse 和 archive/security checks。
3. 校验 binding 名称全部属于目标版本声明。
4. 校验每个 Credential 属于当前项目、状态 active、current version 未变化。
5. 校验 active envelope 存在，且 Credential schema 的 `env` 包含同名字段。
6. Active Skill 必须覆盖所有 `optional=false` 声明。
7. Suspended Skill 可以暂时缺少必需项，但启用仍必须 fail closed。
8. 为目标版本创建 config revision 1 和 bindings。
9. 将目标 Draft 标记为 published 并移动 live pointer。
10. 在同一事务写 Credential binding 和 publish governance audit。

任一步骤失败时，版本状态、live pointer、binding、config 和 audit 全部回滚。

### 8.4 已准入 Run

- 发布前已准入的 Run 保留旧 SkillVersion 和旧 Credential closure 快照。
- 发布后新准入的 Run使用新版本和新绑定。
- Credential 后续轮换或撤销继续按现有运行时规则 fail closed。

## 9. 前端设计

### 9.1 新增组件

```text
frontend/src/components/projects/assets/skill-secret-declarations-editor.tsx
frontend/src/components/projects/assets/skill-credential-option-select.tsx
frontend/src/components/projects/assets/skill-publish-dialog.tsx
frontend/src/core/shared-assets/skill-secret-declarations.ts
```

职责：

- `SkillSecretDeclarationsEditor`：声明列表、optional、autonomous、错误和源码跳转。
- `SkillCredentialOptionSelect`：只展示后端 publish-plan 返回的 eligible options。
- `SkillPublishDialog`：发布计划、Credential 选择、内联创建、409 合并和最终发布。
- `skill-secret-declarations.ts`：只保存 API projection 类型和前端状态推导，不解析 YAML。

### 9.2 版本工作台

修改：

```text
frontend/src/components/projects/assets/skill-version-workbench.tsx
frontend/src/components/projects/assets/skill-asset-detail.tsx
```

要求：

- 增加 `files | secrets` surface。
- 始终加载工作副本的根 `SKILL.md`。
- 表单 patch 结果通过现有 file changes 机制写入同一个 buffer。
- 切换源码修复时自动选择 `SKILL.md` 和 source mode。
- parse/patch pending 或 projection invalid 时禁止保存和发布。
- 延迟到达的 source GET 不得覆盖本地 changes。
- 保留现有 dirty、beforeunload、discard 和 CAS conflict 行为。

### 9.3 发布对话框

修改：

```text
frontend/src/components/projects/assets/project-asset-detail-sheet.tsx
frontend/src/components/projects/assets/project-asset-view-model.ts
frontend/src/core/shared-assets/types.ts
frontend/src/core/shared-assets/api.ts
frontend/src/core/shared-assets/hooks.ts
frontend/src/core/shared-assets/query-keys.ts
```

状态规则：

- 必需项未选择时阻止发布并聚焦第一项。
- 可选项未选择时显示警告但允许发布。
- 没有 eligible Credential 时显示“创建 Credential”或“管理 Credential”。
- 创建 Credential 后先 refetch publish-plan，确认新版本 eligible 后再自动选择。
- 409 刷新计划并保留仍然 eligible 的选择。
- stale-base 确认在同一对话框处理，不丢失选择。
- 关闭有未提交选择的对话框时提示放弃。
- 选择保存在组件内存中，不进入 localStorage。

### 9.4 发布后绑定编辑器

修改：

```text
frontend/src/components/projects/assets/skill-credential-bindings.tsx
```

- 复用 Credential option select。
- Active Skill 的必需项不显示普通解除绑定动作。
- 服务端仍必须拒绝绕过 UI 的必需项解绑。
- 可选项允许解绑。
- 遗留的不完整 Active Skill 显示阻断状态和修复入口。

### 9.5 AI Skill Builder

修改：

```text
frontend/src/components/projects/skills/skill-builder-candidate-workbench.tsx
frontend/src/components/projects/skills/skill-builder-workspace.tsx
frontend/src/core/skill-builder/types.ts
frontend/src/core/skill-builder/hooks.ts
```

- 候选工作台增加 `files | secrets` surface。
- 表单修改直接更新 `drafts["SKILL.md"]`，不依赖当前选中文件。
- 修改后清除旧 validation checksum。
- 非法候选可保留在 Builder 会话，但禁止 validate 和 commit。
- create 成功记录精确 skill ID/version ID，并提供“配置凭证”操作。
- revise 成功记录精确 Draft，进入发布流程。

### 9.6 Credential 创建

复用现有 `CredentialSecretDialog`，仅允许预填非敏感字段：

- display name。
- credential name。
- fixed type `skill_auth`。
- fixed env field names。

Credential 明文提交后立即从组件 state 清除，不进入 Query cache。

### 9.7 i18n 和可访问性

修改：

```text
frontend/src/core/i18n/locales/types.ts
frontend/src/core/i18n/locales/zh-CN.ts
frontend/src/core/i18n/locales/en-US.ts
```

新增 `skills.secrets` 和 `skills.publishDialog` 文案组。所有行错误关联表单控件，异步
错误使用 aria-live；发布阻断时将焦点移动到第一个未配置的必需项。

## 10. 权限矩阵

| 能力 | 允许操作 |
| --- | --- |
| `shared_assets.read` | 查看声明和不含明文的绑定元数据 |
| `shared_assets.edit` | 修改当前编辑副本并保存 Draft |
| `shared_assets.manage_bindings` | 打开发布检查并发布无凭证版本 |
| `mcp.credentials.approve` | 创建、选择和更换项目 Credential |
| manage_bindings + approve | 原子发布并绑定 Credential |

前端 capability gate 仅改善 UX，所有授权必须由后端重新验证。

## 11. 错误合同

| Code | HTTP | 场景 |
| --- | ---: | --- |
| `SKILL_SECRET_DECLARATION_INVALID` | 422 | frontmatter 声明非法 |
| `SKILL_CREDENTIAL_BINDINGS_INCOMPLETE` | 422 | Active Skill 缺少必需绑定 |
| `SKILL_CREDENTIAL_BINDING_INVALID` | 422 | 未声明名称或 Credential schema 不兼容 |
| `SKILL_CREDENTIAL_SELECTION_STALE` | 409 | Credential 已轮换、停用、撤销或 envelope 消失 |
| `SKILL_PUBLISH_BASE_STALE` | 409 | 目标 Draft 基于过期发布版本 |

前端收到 409 时保留 SKILL.md buffer 和仍合法的非敏感选择，不显示泛化刷新错误。

## 12. 测试计划

### 12.1 Canonical parser 和 patch

新增：

```text
backend/tests/test_skill_frontmatter_document.py
```

覆盖：

- mapping 和历史 shorthand。
- 缺省/显式 `secrets-autonomous`。
- 重复 key、alias、深度、节点数和字节限制。
- 重复或非法 env 名。
- `YES`、`NO`、`ON`、`NULL` 等名称。
- no-op 字节完全一致。
- patch 幂等。
- 正文、未知字段、字段顺序和托管区段外注释保留。
- 不可安全 patch 的托管注释。
- diagnostics 行列和日志安全。

### 12.2 后端服务和 PostgreSQL

新增或扩展：

```text
backend/tests/test_skill_service_lifecycle.py
backend/tests/test_skill_credential_binding_service.py
backend/tests/test_skill_credential_policy.py
backend/tests/test_skill_publish_credentials_atomic.py
backend/tests/test_skill_publish_credentials_postgres.py
backend/tests/test_skill_publish_credential_route_contract.py
backend/tests/test_skill_author_publish_policy.py
backend/tests/test_host_execution_continuation_runner.py
backend/tests/test_host_execution_approval.py
```

覆盖：

- 表单修改创建新不可变 Draft。
- 源 checksum CAS。
- 精确 Draft publish-plan。
- Active Skill 完整 binding + publish 成功。
- 缺失、stale、schema 不匹配、envelope 缺失和 audit failure 全量回滚。
- Suspended Skill 允许不完整配置且 activate fail closed。
- Active binding replace 不能移除必需项。
- Editor/Admin 权限矩阵。
- 并发 publish 和 Credential rotation 不死锁，只产生成功或稳定 409。
- 发布前后 Run 使用精确旧/新 closure。
- 响应、审计和日志无明文。

### 12.3 前端单元测试

新增或扩展：

```text
frontend/tests/unit/components/projects/assets/skill-secret-declarations-editor.test.tsx
frontend/tests/unit/components/projects/assets/skill-version-workbench.test.tsx
frontend/tests/unit/components/projects/assets/skill-publisher-governance.test.ts
frontend/tests/unit/core/shared-assets/hooks.test.ts
frontend/tests/unit/components/projects/skills/skill-builder-workspace-actions.test.tsx
frontend/tests/unit/components/projects/skills/skill-builder-secret-candidate.test.tsx
```

覆盖：

- 源码/表单使用同一个 buffer。
- parse/patch out-of-order 响应被丢弃。
- invalid YAML 不丢稿。
- 晚到的 GET 不覆盖本地 changes。
- required/optional 和权限状态。
- Credential 创建后 refetch。
- 409 保留合法选择。
- 请求 payload 不接受或包含 secret value。
- 切换项目后清空 publish plan 和临时选择。

### 12.4 浏览器和真实后端验收

新增：

```text
frontend/tests/e2e/project-skill-secrets.spec.ts
frontend/tests/e2e-real-backend/project-skill-secrets.spec.ts
```

场景：

1. 上传含 `required-secrets` 的 archive，创建后自动进入凭证配置。
2. 创建/选择 Credential，配置完整后启用 Skill。
3. 通过表单修改声明，确认 `SKILL.md` 源码同步并保存新 Draft。
4. 为 Active Skill 原子发布新版本，观察不到未绑定中间状态。
5. AI Builder 候选声明编辑、重新检查、创建和配置凭证。
6. invalid YAML、409、权限不足和项目切换均不丢失或泄漏数据。
7. Local Provider 和 AIO local Provider 运行时均能读取绑定变量。

AIO remote 不在本次验收范围内，除非目标部署已具备对应的私有投影与安全能力。

## 13. 实施顺序

### 阶段 0：兼容性特征测试

- 扫描现有 Project/System Skill 声明。
- 确认是否存在超长 env 名、复杂托管注释或历史宽松格式。
- 为当前接受格式建立 characterization tests。

完成条件：清楚列出必须兼容的历史语法，不直接收紧未知数据。

### 阶段 1：Canonical parser 和 patcher

- 新增纯 harness 实现和 golden tests。
- 逐步替换 archive、Builder、validation、review 和 runtime 解析入口。
- 暂不增加 UI。

完成条件：所有入口对同一 `SKILL.md` 产生完全一致的声明和 diagnostics。

### 阶段 2：发布计划和原子事务

- 新增纯 Credential policy。
- 新增 publish-plan。
- 拆分 publish prepare/commit。
- 加入版本级 binding 原子提交。
- 修复 Active Skill 必需绑定可被移除的问题。

完成条件：PostgreSQL 回滚、并发、权限和 Run snapshot 测试全部通过。

### 阶段 3：版本工作台和发布 UI

- 增加环境变量表单。
- 接入 parse/patch 和现有 changes buffer。
- 增加 Skill 专用发布对话框和内联 Credential 创建。

完成条件：源码和表单双向一致，冲突不丢稿，发布请求不含明文。

### 阶段 4：上传和 AI Builder

- 上传完成后自动进入凭证配置。
- Builder candidate 接入同一表单和验证失效语义。
- create 保持 published+suspended，revise 保持 Draft。

完成条件：两条创建链均能完成声明、配置、启用和实际运行。

### 阶段 5：回归与文档

- 后端先部署，前端后部署。
- 更新 README、backend/AGENTS.md 和 frontend/AGENTS.md。
- 执行 focused tests、backend core gate、frontend check、mocked E2E 和真实后端 E2E。

本方案不改变 System Asset payload，因此不需要运行 `make upgrade-system-assets`。

## 14. 验收标准

功能只有同时满足以下条件才算完成：

1. 用户能在版本工作台和 Builder 候选中通过表单声明凭证环境变量。
2. 表单和源码始终映射到同一个 `SKILL.md` buffer。
3. 发布版本不会在提交后再修改文件字节。
4. Active Skill 的新版本和 Credential binding 原子切换。
5. 上传与 AI 创建后能自动进入凭证配置流程。
6. 所有必需绑定完整前，Skill 不能进入可执行 Active 状态。
7. Credential 明文不进入 API 响应、缓存、prompt、日志、审计或快照。
8. Local Provider 与 AIO local Provider 的真实运行均能读取绑定变量。
9. 409、无效 YAML、网络失败和权限错误均保留用户草稿。
10. Canonical parser、PostgreSQL、前端单元和浏览器测试全部通过。

## 15. 实施结果

### 15.1 已完成

1. 新增 canonical frontmatter parser/patcher；上传、Builder、检查、review、发布和
   runtime description 入口统一采用同一严格语义。重复 key、alias、非法声明和资源限制
   均 fail closed，diagnostic 不回显源码行或非法值。
2. 新增项目级 parse/patch 与 publish-plan API。响应设置 `private, no-store` 和
   `nosniff`，前端只消费结构化 projection，不自行解析 YAML。
3. Skill 版本工作台和 Builder 候选工作台均提供 `files | secrets` 表单；patch 写回同一
   `SKILL.md` buffer，乱序响应不会覆盖新编辑，非法或待解析状态会阻止保存、检查和发布。
4. 发布 Draft 与精确 Credential-version bindings 在同一 PostgreSQL 事务完成；Active
   Skill 必需项不完整时稳定返回 `SKILL_CREDENTIAL_BINDINGS_INCOMPLETE`，Suspended
   Skill 可以先创建后配置。
5. 压缩包创建会在存在声明时自动打开精确版本的环境变量区域；AI Builder create/revise
   会持久化并返回 `created_skill_version_id`，刷新或幂等重放后仍导航到真实创建版本。
6. Local Provider 与 AIO local Provider 均在每条授权命令执行前按冻结 Skill closure
   重新解析 Credential，按命令注入环境变量并清理引用；输出、异常和结果均经过掩码，
   provider/closure 不匹配时不启动命令。
7. 本实现没有新增数据库表、字段或 migration，也没有修改 packaged System Asset payload。
8. 管理员项目覆盖发布同样校验 Active Skill 的精确 Credential closure，不能绕过必需
   binding；管理员仍不能替项目成员提交新的 Credential 选择。
9. 运行时兼容已持久化的 v2 审批，同时在 executor 真正出队后重新授权并解密；撤销、
   轮换、取消和 AIO provider 异常均 fail closed，且不记录 provider 原始响应。
10. Credential binding 后台刷新采用逐字段三方合并，版本/资产切换纳入未保存修改保护；
    两个 files/secrets tab 组支持完整键盘导航和 ARIA 关联。

### 15.2 已验证

- Backend core gate：3083 passed，0 failed，0 skipped；其中新增管理员覆盖真实 PostgreSQL
  发布回归与原子发布聚焦测试为 15 passed。
- Runtime Credential/approval 扩展回归：310 passed。
- Frontend unit：139 files、723 passed、0 skipped；`pnpm check` 0 errors；production build
  成功。
- Mocked Chromium：上传、工作台/发布、Builder 与激活修复四个场景全部通过。
- 真实 Gateway + production frontend Chromium：archive 上传、Credential 创建与绑定、
  canonical patch、Draft fork、publish-plan 和原子发布完整通过；发布请求经断言不含明文。
- 真实 LocalSandbox 子进程和 Apple Container/AIO private container 均读取到绑定变量；
  返回值仅为 `[redacted]`，下一条命令无残留，AIO 容器释放后不存在。

### 15.3 保留边界

1. 环境变量声明不设产品层长度上限以保持历史 frontmatter 兼容；当前绑定持久化列为
   255 字符，因此超长声明可解析、但不会被列为可绑定 Credential，写入绑定时稳定拒绝。
2. 托管字段自身包含注释时允许读取但 `patchable=false`，用户必须转源码模式，避免静默
   丢失注释。
3. AIO remote/provisioner 不在本轮范围内；该模式仍按现有私有投影能力 fail closed。
4. 真实外部模型生成的 Skill Builder 会话仍属于 Provider 集成 gate；本轮以 durable
   Builder 单元/PostgreSQL 合同和 mocked browser 场景验证候选编辑、重新检查、提交及
   精确版本导航。
