# 方案:Skill Builder 修订模式(对话式修改项目 Skill)

> 状态:设计方案 v2(P1/P2 已落地,含 PostgreSQL 行为矩阵、修订成功页 CTA,以及 Skill Builder 中英文 i18n)。
> 未做:P3 可选项。
> v2 修订:采纳评审意见 —— 修正 commit/delete 锁顺序、新增 publish 时机的
> 基线漂移守卫、API 改为单模型交叉校验、seed 时执行完整 Builder dry-run、
> 删除目标时终止在途会话;并采纳复合外键、错误码注册、payload 字段隔离、
> media_type 比较、幂等重放、`draft_ready` 起始状态与唯一索引口径对齐。
> 关联文档:[SKILL_BUILDER.md](SKILL_BUILDER.md)(现有创建模式的运行时架构)。
> 评审范围:backend schema / Gateway API / shared_assets 服务层 / Builder 运行时 / frontend。

## 1. 背景与目标

现状:Skill Builder 只支持「从零对话创建」项目 Skill。会话 commit 后经
`SkillService.create_project_from_preview_in_session` 原子创建新资产
(`suspended` + published v1),发布与启用是独立治理动作。

目标:项目自建 Skill 支持用同一套对话体验创建**新的 draft 版本**:

- 用户从 Skill 详情页发起「对话修改」,Builder 以当前已发布版本为基线播种草稿;
- 多轮对话 + 手工编辑迭代草稿,走既有 validate(SkillScan)门禁;
- commit 只产生 **draft 版本**,不移动 `current_published_version_id` 指针;
- 用户在既有版本管理界面显式 publish 后新版本才生效。

非目标(本期不做):

- 修改 System Skill(打包定义运行时不可变);
- commit 时自动发布或发布快捷通道(授权边界不变);
- 超出 Builder 草稿信封的大型/二进制 Skill 的对话修订(见 §11 P3);
- 以任意历史版本为基线的「对话式 fork」(见 §11 P3);
- 删除目标时跨 owner 主动 cancel 私有 Run(在途 Run 在下一次工具调用边界
  fail closed 收敛,见 §8)。

## 2. 设计原则

把「修订」建模为 Builder 会话的一种 `session_kind`,最大化复用现有机制:

- 会话状态机、operation 幂等、乐观 revision 不变;
- `runtime_kind=skill_builder` 的 Run 准入、Worker 进程与专用图、10 个受限
  工具、CAS 草稿层、密钥扫描、lease 复验的**机制**不变;其中
  `_builder_tool_transaction` 增加一处目标存活检查(见 §8),这是
  Worker 调用路径上唯一的服务层改动;
- validate / SkillScan / commit 门禁不变,仅 commit 落点按 `session_kind` 分支;
- 版本不可变性、draft→published 单向状态机、发布/启用分权 **不动**;
- 全链路遵守既定锁顺序:**Project → Membership → Skill → SkillDesignSession**
  (见 §7.1)。

```text
create 模式: commit → create_project_from_preview_in_session      (新资产 suspended + v1)
revise 模式: commit → create_project_version_from_preview_in_session (既有资产 + draft v(N+1))
                                              └→ 既有 POST /versions/{vid}/publish(带漂移守卫)后生效
```

一个关键的既有事实使方案成立:设计稿草稿校验和
(`SkillDesignService._draft_checksum_from_metadata`)与正式版本校验和
(`skill_service._snapshot_checksum`)是同一公式(排序后
`[{path, sha256, size_bytes}]` 的 canonical JSON 取 sha256;
`finalize_agent_candidate` 已依赖 `preview.checksum == expected_draft_checksum`)。
因此基线 `payload_checksum` 可直接与草稿 checksum 比较。注意该公式**不含
`media_type`**:所有「内容是否变化」的判定(no-op 检查、前端 diff 徽标)
必须在 checksum 之外**额外比较 media_type**(见 §7.3、§10)。

## 3. 数据模型

`skill_design_sessions` 新增列(同步更新 ORM
`deerflow/persistence/shared_assets/skill_design_model.py`、`full_schema.sql`、
中文列注释、catalog 签名与 parity 测试；已收入当前 `full_schema` 快照):

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `session_kind` | VARCHAR(16) NOT NULL DEFAULT `'create'` | CHECK ∈ `{create, revise}` |
| `target_skill_id` | UUID NULL | 修订目标资产 |
| `base_version_id` | UUID NULL | 钉住的基线版本 |
| `base_version_number` | BIGINT NULL | 基线版本号(展示用) |
| `base_payload_checksum` | CHAR(64) NULL | 基线内容校验和 |
| `target_skill_deleted` | BOOLEAN NOT NULL DEFAULT FALSE | 目标被删后 fail-closed 标记 |

约束与外键:

- **复合外键**(两个前提唯一约束均已存在于 schema):
  - `(project_id, target_skill_id) → skills (project_id, id)`
    (依托 `uq_skills_project_id_id`),数据库层杜绝跨项目引用;
  - `(target_skill_id, base_version_id) → skill_versions (skill_id, id)`
    (依托 `uq_skill_versions_asset_id`),杜绝「版本不属于该 Skill」。
  - 两条 FK 均为普通(非级联)引用;删除流程在删 Skill 前显式解除引用
    (镜像现有 `created_skill_id` 处理,见 §8)。
- CHECK(镜像现有 failed/completed 成对约束风格):
  - `session_kind = 'create'` → 四个 target/base 引用列全空且
    `target_skill_deleted = FALSE`;
  - `session_kind = 'revise'` → (`target_skill_id`、`base_version_id`、
    `base_version_number`、`base_payload_checksum` 全非空且
    `target_skill_deleted = FALSE`)**或**(全空且
    `target_skill_deleted = TRUE`,即删除后遗留态)。
- **部分唯一索引**(命名进 `_CONFLICT_CONSTRAINTS`,见 §5.4):
  `uq_skill_design_sessions_live_revise_target` ON
  `(project_id, owner_user_id, target_skill_id)
  WHERE session_kind = 'revise' AND target_skill_id IS NOT NULL
  AND status NOT IN ('completed', 'cancelled')`
  —— 口径与 `count_incomplete` / `list_incomplete` 的
  `status NOT IN ('completed','cancelled')` 完全一致(`failed` 视为未完成、
  可续跑,因此同目标的 failed 会话存续期间不允许再开新修订会话)。
- 现有 `completed ⟺ created_skill_* 成对` CHECK 不动:revise commit 写
  `created_skill_id = target_skill_id` + `created_skill_version_id = 新版本`,
  语义统一。

## 4. Gateway API

### 4.1 创建会话:单模型 + 交叉校验(不用 discriminated union)

Pydantic v2 的 discriminated union 对缺少 tag 的输入直接报
`union_tag_not_found`,现有前端不发送 `kind`,会导致旧 create 请求 422。
因此采用**单一 strict 模型 + `model_validator`**:

```python
class CreateSkillDesignSessionRequest(_StrictModel):
    kind: Literal["create", "revise"] = "create"
    slug: str | None = None
    display_name: str | None = None
    skill_id: uuid.UUID | None = None
    idempotency_key: str

    @model_validator(mode="after")
    def validate_kind_fields(self) -> CreateSkillDesignSessionRequest:
        if self.kind == "create":
            if self.slug is None or self.display_name is None or self.skill_id is not None:
                raise ValueError("create requires slug and display_name")
        elif self.slug is not None or self.display_name is not None or self.skill_id is None:
            raise ValueError("revise requires only skill_id")
        return self
```

revise 模式不接受 slug/display_name:服务端从目标资产复制,防止漂移。

### 4.2 响应扩展与前端契约协同

- Session item 增加:`session_kind`、`target_skill_id`、`base_version_id`、
  `base_version_number`、`base_payload_checksum`、
  `base_files: [{path, size_bytes, sha256, media_type}]`(仅元数据,含
  media_type 供 diff 徽标使用,不含内容)。
- 列表 summary 增加 `session_kind` 与目标 slug(恢复横幅区分创建/修订)。
- commit 响应 `SkillDesignCommitDataResponse` 增加 `version`
  (新版本视图,含 `version_number` 与 `version_id`);**幂等重放分支必须
  按会话行的 `created_skill_version_id` 重新加载并返回同一精确版本**。
- commit 请求增加 `acknowledge_base_stale: bool`(见 §7.3);该字段纳入
  operation 的 `request_checksum`,保证重放校验一致。
- **兼容性硬约束**:前端 Builder 的 Zod schema 为 `.strict()`,后端先行
  增加响应字段会令现有创建流程解析失败。因此 **P1 必须包含前端契约同批
  变更**(zod schema 与后端响应同批扩展;或先发一版容忍未知字段的前端),
  不允许「P1 只发后端」(见 §11)。

### 4.3 错误码与注册

新错误一律定义为 `SharedAssetError` 子类,并完成三处注册,否则会以
500/503 泄漏:加入 `ASSET_ERRORS` 元组、`raise_asset_domain` 的
状态码映射,以及(涉及唯一约束的)`_CONFLICT_CONSTRAINTS`
(新增 `uq_skill_design_sessions_live_revise_target`)。

| 错误码 | HTTP | 场景 |
| --- | --- | --- |
| —(塌缩 404) | 404 | 目标不存在 / system scope / 外项目 |
| `SKILL_DESIGN_TARGET_UNSUPPORTED` | 422 | 基线未通过 Builder seed dry-run(§6.1) |
| `SKILL_DESIGN_TARGET_SESSION_EXISTS` | 409 | 该目标已有未完成修订会话 |
| `SKILL_DESIGN_TARGET_DELETED` | 409 | 目标已被删除 |
| `SKILL_DESIGN_BASE_STALE` | 409 | commit 时基线漂移且未 ack |
| `SKILL_DESIGN_NO_CHANGES` | 409 | 草稿与基线内容及 media_type 完全一致 |
| `SKILL_PUBLISH_BASE_STALE` | 409 | publish 时 lineage 漂移且未 ack(§5) |

## 5. publish 时机的基线漂移守卫(共享路径行为变更)

commit 时的 `acknowledge_base_stale` 只保护「创建 draft」这一步;真正移动
live pointer 的是之后的 publish,而现有 `_publish_in_transaction` 只校验
`expected_asset_version`(请求瞬间的并发),不识别「基于旧版本的分支」。
典型事故:基于 v3 修订出 draft v4 → 他人发布 v5 → 用户稍后发布 v4,
v5 的修改被静默遮蔽。

**方案**:`_publish_in_transaction` 在既有校验后增加 lineage 守卫:

```text
record.row.supersedes_version_id != asset.current_published_version_id
  且未显式 acknowledge → SKILL_PUBLISH_BASE_STALE (409)
```

- publish 端点请求体增加 `acknowledge_stale_base: bool = False`;
- 该守卫作用于**全部项目 draft 发布路径**(手工归档新版本、fork、Builder
  修订版本、admin override 同路径)。fork 的 `supersedes_version_id` 指向
  历史源版本,发布时**按设计**要求显式 ack —— 把旧版本分支顶成 live
  本就应当是知情动作;
- 首个版本(`supersedes IS NULL` 且指针为 NULL)不触发;
- bootstrap 的 System Skill 发布不经过 `SkillService.publish`,不受影响;
- commit 时的 ack 保留(尽早提示),但**最终保护在 publish**;
- 前端发布确认框相应增加「当前线上已是 v{M},本次将以基于 v{K} 的版本
  覆盖」的显式确认;
- 独立测试覆盖:守卫触发/ack 放行/首版不触发/fork 场景。

## 6. 服务层:会话创建与 turn(`app/shared_assets/skill_design_service.py`)

### 6.1 创建会话(revise 分支,单事务)

1. 解析目标(按 §7.1 锁顺序,Skill 行 `for_update`):`scope = 'project'`、
   `project_id` 匹配、`status ∈ {active, suspended}`、
   `current_published_version_id` 非空;不满足 → 404/409。
2. **完整 seed dry-run**(而非仅文件数/大小/UTF-8),任一不过 →
   `SKILL_DESIGN_TARGET_UNSUPPORTED`:
   - 尺寸信封:≤128 文件、单文件 ≤512 KiB、总量 ≤2 MiB、全部 UTF-8
     (对比正式版本上限 100 MiB / 16384 文件);
   - **candidate 路径校验**:逐文件过 `_canonical_candidate_path` 等价规则
     —— 注意其拒绝一切点号开头路径段(`.gitignore`、`.github/...`),
     归档导入的 Skill 可能含此类文件;
   - `_validate_builder_files` 全量约束;
   - **secret 启发式预扫**:`contains_secret_like_material` 命中即拒绝
     —— 否则播种成功后 Agent 连 `read_candidate_file` 都会失败
     (草稿读取载荷同样过该扫描),形成无法推进的死会话;
   - `preview_archive` dry-run:frontmatter 解析 + 以**当前规则**重跑
     SkillScan(历史版本可能在旧规则下发布,新规则 block 的基线直接拒绝)。
3. **播种草稿**:基线文件经 `SkillService._verified_archive_files`
   逐字节校验后复制进 `skill_design_draft_files`;写 `draft_checksum`
   (等于 `base_payload_checksum`);pin base 三元组与复合 FK 引用;
   `slug` / `display_name` 取自目标资产,**跳过**项目内重名检查。
4. **起始状态为 `draft_ready`**(不是 `interviewing`):现有
   `draft_update` 与 `validate` 均要求 `draft_ready|validated`,从
   `interviewing` 起步会禁止播种后的手工编辑与直接校验;`draft_ready`
   起步同时获得「纯手工修订、零模型轮」的合法路径(message turn 本就允许
   从 `draft_ready` 发起)。首条 assistant 消息:「已加载 {slug} v{N} 的
   {count} 个文件,可直接编辑,或描述要修改的内容」。
5. 幂等键、未完成会话总数上限(8)原样复用;§3 部分唯一索引兜底
   单目标单会话,竞态下的 IntegrityError 经 `_CONFLICT_CONSTRAINTS`
   映射为 `SKILL_DESIGN_TARGET_SESSION_EXISTS`。

### 6.2 turn / Run 准入(`app/private_work/skill_builder_run_admission.py`)

`_run_input_payload` 增加**独立的顶层 `authoring` 块**,不复用
`conversation.mode`(该字段既有语义是 `initial|continuation`,不得混用):

```json
{
  "authoring": {
    "kind": "revise",
    "target_slug": "...",
    "base_version_number": 3
  },
  "conversation": {"mode": "continuation", "turn": "..."}
}
```

create 模式 `authoring.kind = "create"`,其余字段不变。草稿文件元数据本就
随 payload 下发,Agent 首次 `list_candidate_files` 自然看到播种的基线文件。
10 个工具、终态强制、CAS、密钥扫描、闭包校验机制不变。

### 6.3 validate

零改动。`_require_preview_name` 使用 `row.slug`(revise 即目标 slug),
与发布时 `_require_archive_name_matches_asset` 天然一致;Agent 改
frontmatter `name` 在 validate 即被拒。

## 7. 服务层:commit(revise 分支)

### 7.1 锁顺序(修正,消除与删除路径的 ABBA 死锁)

现状风险:commit 先锁 session 行再锁 Skill,而删除路径是先锁 Skill
(`_get_asset(for_update)` → 锁版本行)再更新 Builder session 行,
构成 Session→Skill 对 Skill→Session 的死锁环。

**统一顺序:Project → Membership → Skill → SkillDesignSession。**
revise commit 事务内:

1. `get_operation(for_update)`(幂等定位,operations 不在死锁环上);
2. **无锁**读取会话行的 `target_skill_id`;
3. 锁目标 Skill 行(`for_update`);
4. 再锁 session 行(`for_update`),并**重新校验** `target_skill_id`
   未变、`target_skill_deleted = FALSE`、revision/状态/checksum;
5. 其余校验与写入。

create 分支不涉及既有 Skill 行,顺序不变。必须新增并发
delete × commit 的集成测试(见 §12)。

### 7.2 校验与写入

在现有校验(`status = validated`、revision/checksum 三重一致、
`scan_decision = warn` 需 `acknowledge_warnings`)之后:

1. 重验目标:scope / project / `status ∈ {active, suspended}`、
   `target_skill_deleted = FALSE`(否则 `SKILL_DESIGN_TARGET_DELETED`)。
2. **no-op 拒绝**:草稿与基线的 `(path, sha256, size_bytes, media_type)`
   集合完全一致 → `SKILL_DESIGN_NO_CHANGES`(checksum 不含 media_type,
   故需集合比较而非仅比 checksum)。
3. **基线漂移 ack**:`current_published_version_id != base_version_id` 且
   `acknowledge_base_stale != true` → `SKILL_DESIGN_BASE_STALE`。
   (最终保护在 publish 守卫,见 §5;此处仅尽早提示。)
4. 调用 `SkillService` 新增公开方法
   `create_project_version_from_preview_in_session(session, context,
   skill_id, preview, supersedes_version_id=base_version_id)`:
   镜像既有 `create_project_from_preview_in_session` 的封装方式,内部复用
   `_create_version`(版本号 = max+1、配额 `reserve_skill_version`、
   审计 `skill.version.create`)。`supersedes_version_id` 指向**钉住的基线**
   而非 commit 时的 published(真实反映修订来源,并作为 §5 publish 守卫的
   判定输入;`fork_version` 已有同类先例)。资产并发以 Skill 行锁 +
   基线比对保护,不再依赖外部 `expected_asset_version`。
5. 会话收尾:`created_skill_id = target_skill_id`、
   `created_skill_version_id = 新版本`、清空草稿、`status = completed`。

### 7.3 幂等重放

沿用 operation 行 + `request_checksum` / `terminal_request_checksum` 机制:

- `acknowledge_base_stale` 纳入 `request_checksum`;
- 重放分支按会话行 `created_skill_version_id` **重新加载精确版本**并在
  响应中返回(会话 completed 后目标被删的重放,沿用既有
  `created_skill_deleted` fail-closed 路径)。

## 8. 删除联动(`app/shared_assets/skill_repository.py` 等)

删除目标 Skill 的事务内(在既有 `created_skill_*` 处理旁),对引用该
Skill 的 revise 会话:

1. 解除引用:`target_skill_id` / `base_version_id` / `base_version_number` /
   `base_payload_checksum` 置 NULL,`target_skill_deleted = TRUE`
   (满足 §3 CHECK 的删除后遗留态);
2. **非终态会话置为 `failed`**:错误对
   (`error_code = SKILL_DESIGN_TARGET_DELETED`,配套 message),清空
   `active_clarification_json` / `validation_json`,revision +1;
3. 该会话所有 `in_progress` 的 operation 置 `failed`
   (`public_error_code = SKILL_DESIGN_TARGET_DELETED`)。

在途 Run 的收敛方式:**下一次工具调用边界 fail closed**。
`_builder_tool_transaction` 本就按 `run_id` 反查 operation 并要求
`status == "in_progress"`,上述第 3 步使后续草稿/终态工具调用得到
`AuthorizationRevoked`,Run 以失败结算并释放配额;另在该事务内增加
`_require_revise_target_live`(检查 design 行 `target_skill_deleted`)作为
防御纵深。交互端点(turn / validate / commit / draft_update)统一执行同一
检查。**不做**跨 owner 的私有 Run 主动 cancel(删除者与会话 owner 常非同
一人,主动 cancel 需跨 shared_assets → private_work 域边界操作他人私有
Run,收益不抵复杂度;若后续需要即时终止,作为独立增强项评估)。

修订会话是 owner 私有草稿,**不阻止**目标删除(治理引用检查仍只看
Agent 引用与 Run 快照)。

## 9. Builder 运行时(`app/shared_assets/skill_builder_agent_runtime.py`)

`_SYSTEM_PROMPT` 增加一段模式中立说明(代码内字符串,**不动**打包的
skill-creator Skill,避免牵出 bootstrap 目录再生成与
`make upgrade-system-assets`):

> The run input's `authoring` block declares whether you are creating a new
> Skill or revising an existing one. When revising, the persisted candidate
> draft starts as the exact current version: read before you edit, make
> targeted changes, preserve unrelated files, and never change the
> frontmatter `name`.

工具集、终态、目录、闭包校验不变。若需要给修订模式更丰富的方法论指导,
后续走 skill-creator v2 的治理路径(目录再生成 + 系统资产升级),
作为独立跟进项。

## 10. 前端

- **入口**:Skill 详情/版本页新增「对话修改」按钮 →
  `POST sessions {kind: "revise", skill_id}` → 跳转现有 Builder 工作台路由
  (按 `session_kind` 渲染,不新建路由树)。
- **契约**(P1 同批,见 §4.2):zod schema 扩展 `session_kind` /
  target-base 字段 / `base_files` / commit `version` /
  `acknowledge_base_stale`;发布界面增加 §5 的 stale-base 确认。
- **工作台适配**(`components/projects/skills/skill-builder-workspace.tsx`
  及 `core/skill-builder/*`):
  - 顶部横幅「基于 {slug} v{N} 修订」;目标被删时按 `failed` +
    `SKILL_DESIGN_TARGET_DELETED` 呈现明确终态;
  - 文件树用 `base_files` 对比 sha256 **与 media_type**,标注
    新增/修改/删除 徽标;
  - commit 成功页改为「已创建草稿版本 v{N+1},前往发布」,CTA 链接到
    既有版本管理/发布界面;
  - 恢复横幅区分两种会话;i18n 中英文文案。

## 11. 分阶段实施

| 阶段 | 内容 | 规模 |
| --- | --- | --- |
| P1 | 修订会话 schema + 服务层(seed dry-run、commit 分支与锁顺序、删除联动)+ 准入 payload + prompt 段 + **publish 漂移守卫** + **前端契约同批变更(zod / 发布确认 / 错误码文案)** + 后端测试 | 大(核心) |
| P2 | 前端入口 + 工作台完整适配(diff 徽标、修订横幅、成功页 CTA)+ 前端测试 | 中 |
| P3(可选) | 二进制/超信封文件 carry-over、任意历史版本作基线(对话式 fork)、文件级 diff 视图、Builder SSE 工具流接线、删除时跨 owner 主动 cancel 在途 Run | 独立跟进 |

> P1 不允许「只发后端」:响应字段变更 + 前端 strict zod 意味着契约必须
> 同批落地,否则现有创建流程立即解析失败(§4.2)。

## 12. 安全不变式与评审清单

- 能力要求与手动建版本一致:`SHARED_ASSETS_EDIT`;publish(`EDIT`)与
  activate(`MANAGE_BINDINGS`)分权不变。
- 仅 project scope 目标;System Skill 塌缩 404(打包定义运行时不可变)。
- `skill_id` 只是资源标识,归属与状态在每个事务内以服务端 `ProjectContext`
  重验;复合 FK 在数据库层再兜一道跨项目/跨资产引用。
- 锁顺序全链路遵守 Project → Membership → Skill → SkillDesignSession;
  revise commit 按 §7.1 先锁 Skill 后锁 session。
- 播种内容来自受治理已发布版本且读回时逐字节校验,并通过完整 Builder
  dry-run(含 secret 启发式与当前规则 SkillScan)才允许开会话;
  模型写入仍全量过密钥扫描 + CAS;版本行/文件行不可变触发器与发布时的
  全量重校验(`_publish_in_transaction` + 新增 lineage 守卫)是最终兜底。
- Worker 进程与专用图不变;Worker 调用的草稿工具事务新增
  `_require_revise_target_live` 一处检查(§8),授权失效在下一个执行边界
  坍缩,与既有失效哲学一致。
- 配额:新版本照常 `reserve_skill_version`;会话取消/失败不产生版本;
  审计复用既有 `skill.version.create` 闭合契约。

## 13. 测试计划(按仓库 TDD 规范)

PostgreSQL 集成(`backend/tests/`):

- 播种正确性(文件逐字节、`draft_checksum == base_payload_checksum`、
  起始状态 `draft_ready`、播种后可直接 draft_update / validate);
- seed dry-run 拒绝矩阵:超文件数/超大小/非 UTF-8/点号路径/
  secret 启发式命中/当前规则 SkillScan block;
- 单目标单会话唯一索引(含 `failed` 会话存续期间拒绝新会话、竞态
  IntegrityError → 409 映射);目标 404 塌缩(system / 外项目 / 不存在);
- turn 准入 payload:`authoring` 块正确、`conversation.mode` 语义不变;
- commit:建版本(`version_number`、`supersedes_version_id = 基线`、
  指针不动、配额 reserve)、no-op 拒绝(含「仅 media_type 变化」不算
  no-op)、漂移 ack 双分支、幂等重放返回精确
  `created_skill_version_id`(ack 字段纳入 request_checksum)、
  锁顺序(**并发 delete × commit 无死锁**,双向都收敛为确定结果);
- publish 漂移守卫:触发/ack 放行/首版不触发/fork 场景/
  `expected_asset_version` 与守卫叠加;
- 删除联动:非终态会话置 failed + 错误对、in_progress operation 置
  failed、在途 Run 下一次工具调用 `AuthorizationRevoked` 并失败结算、
  交互端点统一 `SKILL_DESIGN_TARGET_DELETED`;
- 错误注册:全部新错误码经 `raise_asset_domain` 映射为契约状态码
  (无 500/503 泄漏);
- schema:fresh-install 与 migration parity、
  `generate_schema_comments.py --check`、新 CHECK / 复合 FK / 部分唯一
  索引约束测试。

前端 unit(`frontend/tests/`):新 types / state 分支、hooks 的 revise
会话缓存路径、发布确认的 stale-base 分支。

文档:更新 `backend/docs/SKILL_BUILDER.md`(修订模式章节 + 当前 schema
说明 + publish 守卫)、用户侧 README 功能描述。

## 14. 已定策略与默认值

1. 基线 = 当前 published 版本(v1 不支持选历史版本,留给 P3)。
2. `supersedes_version_id` 指向钉住的基线,而非 commit 时的 published;
   它同时是 publish 漂移守卫的判定输入。
3. 漂移保护双层:commit 时 ack(尽早提示)+ **publish 时 lineage 守卫
   (最终保护,作用于全部项目 draft 发布,fork 触发属预期行为)**。
4. no-op 硬拒绝,判定含 media_type。
5. 单 owner 单目标同时一个未完成修订会话,「未完成」= 非
   `completed/cancelled`(与 `count_incomplete` 口径一致)。
6. 超信封或未过 seed dry-run 的 Skill 明确拒绝并引导归档上传。
7. revise 会话起始状态 `draft_ready`,支持零模型轮的纯手工修订。
8. 删除目标:会话置 failed + 在途 Run 于下一工具边界 fail closed;
   不做跨 owner 主动 cancel。
9. commit 不自动发布;发布沿用既有端点与授权(新增 stale-base ack 字段)。
