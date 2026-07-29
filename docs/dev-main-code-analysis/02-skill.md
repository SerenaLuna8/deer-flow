# 02. Skill 模块：`main` 最终实现、提交演进与 `dev` 精确落点

## 1. 分析范围与结论

本文独立分析 Skill，不把 Agent 资产、MCP 或通用 Security 当作 Skill 内部实现。对比：

- 公共祖先：`3be3969f8fc3f2d2b6d36ef5c26fa5593d916f2a`
- `main`：`e317f7b8`
- `dev`：`8a91e957`

结论：

1. `main` 的 Skill 是文件系统中的 public/custom/integrations/legacy 包，支持安装、编辑、启停、热重载、slash 激活、请求级秘密和完整 review/SkillScan。
2. `dev` 的 Skill 是 system/project PostgreSQL 资产：文件和扫描结果进入不可变 Skill version，Agent version 精确引用它，Worker 为一个 Run 物化只读目录。
3. `main` 最有价值的增量是运行时授权语义和静态检测能力：`allowed-tools` 只应对“已激活 Skill”生效、slash 每 Run 只注入一次、SkillScan 的网络 client 数据流和不确定 `shell=True` 检测、NTFS ADS 防护。
4. `dev` 当前把所有已准入 Skill 的 `allowed-tools` 在建图时一次性求并集，这会让“可发现”错误等价于“已授权”，是已确认的权限语义回归。
5. `main` 的文件安装/重载/API 不能覆盖 `dev`。Review 系统若要引入，必须增加 PostgreSQL exact-version reader，而不是恢复 `skill://custom/...` 文件权威。

## 2. `main` 源码地图

| 层次 | `main` 路径 | 责任 |
| --- | --- | --- |
| 类型 | `backend/packages/harness/deerflow/skills/types.py` | `SkillCategory`、`SecretRequirement`、`Skill` |
| frontmatter | `backend/packages/harness/deerflow/skills/frontmatter.py` | 公共字段集合、YAML 拆分 |
| 解析 | `backend/packages/harness/deerflow/skills/parser.py` | `allowed-tools`、`required-secrets`、`secrets-autonomous` |
| 存储协议 | `backend/packages/harness/deerflow/skills/storage/skill_storage.py` | Skill 读写抽象 |
| 本地存储 | `backend/packages/harness/deerflow/skills/storage/local_skill_storage.py` | 公共/自定义 Skill、安装、历史 |
| 用户隔离 | `backend/packages/harness/deerflow/skills/storage/user_scoped_skill_storage.py` | per-user custom、integration、legacy、启停状态 |
| 安装 | `backend/packages/harness/deerflow/skills/installer.py` | 安全解压、静态+LLM 扫描、staging |
| API | `backend/app/gateway/routers/skills.py` | 安装、编辑、删除、历史、回滚、toggle、reload |
| slash | `backend/packages/harness/deerflow/skills/slash.py` | slash 语法和 exact Skill 解析 |
| 激活中间件 | `backend/packages/harness/deerflow/agents/middlewares/skill_activation_middleware.py` | 注入 Skill、秘密绑定、运行内去重 |
| durable context | `backend/packages/harness/deerflow/agents/middlewares/skill_context.py` | 从 read_file 结果提取已加载 Skill |
| 工具策略 | `backend/packages/harness/deerflow/agents/middlewares/skill_tool_policy_middleware.py` | 模型 schema、执行、`tool_search` 三重约束 |
| 策略函数 | `backend/packages/harness/deerflow/skills/tool_policy.py` | `allowed_tool_names_for_skills()` |
| SkillScan | `backend/packages/harness/deerflow/skills/skillscan/` | 确定性包/代码扫描 |
| LLM moderation | `backend/packages/harness/deerflow/skills/security_scanner.py` | allow/warn/block 内容审查 |
| Review core | `backend/packages/harness/deerflow/skills/review/` | reader、facts、report、digest、eval/resource graph |
| Review tool | `backend/packages/harness/deerflow/tools/builtins/review_skill_package_tool.py` | 非激活、非安装的模型工具 |
| Review 契约 | `contracts/skill_review/*.schema.json` | snapshot/facts/report v1 |
| Review CI | `.github/workflows/skill-review-ci.yml`、`scripts/review_changed_public_skills.py` | 变更公共 Skill 的确定性门禁 |

## 3. 类型与 frontmatter 契约

### 3.1 `SkillCategory`

`main` 定义四类：

```text
PUBLIC       = "public"        # 平台内置，只读
CUSTOM       = "custom"        # 用户可编辑
INTEGRATION  = "integrations"  # 托管第三方，只读
LEGACY       = "legacy"        # 旧全局 custom，只读兼容
```

### 3.2 `Skill`

关键字段：

```text
name: str
description: str
license: str | None
skill_dir / skill_file / relative_path: Path
category: SkillCategory
allowed_tools: tuple[str, ...] | None
enabled: bool
required_secrets: tuple[SecretRequirement, ...]
secrets_autonomous: bool
```

`allowed_tools` 三态非常重要：

- `None`：Skill 没声明该字段，保持 legacy allow-all；
- `()`：显式空数组，不允许任何业务工具；
- 非空 tuple：只允许列出的业务工具。

### 3.3 frontmatter

`ALLOWED_FRONTMATTER_PROPERTIES` 包括：

```text
name, description, license, allowed-tools, required-secrets,
secrets-autonomous, metadata, compatibility, version, author
```

`split_skill_markdown(content)`：

1. 要求根部 YAML frontmatter；
2. `yaml.safe_load`；
3. 要求 mapping；
4. 将 YAML 允许的非字符串 key 规范化为字符串；
5. 返回 metadata、原始 frontmatter text、body。

`parse_allowed_tools(raw, skill_file)` 只接受字符串列表；显式空列表保留为空 tuple。

`parse_required_secrets(raw, skill_file)` 接受：

```yaml
required-secrets:
  - OPENAI_API_KEY
  - name: OPTIONAL_TOKEN
    optional: true
```

要求合法环境变量名并去重。`parse_secrets_autonomous()` 默认 `true`，显式 false 表示只有用户 slash 激活时才可绑定秘密。

## 4. `main` 安装与存储生命周期

### 4.1 安装调用链

```text
POST /api/skills/install
  -> UserScopedSkillStorage.ainstall_skill_from_archive()
  -> LocalSkillStorage._prepare_skill_archive()
     -> scan_archive_preflight_or_raise()
     -> safe_extract_skill_archive()
     -> resolve_skill_dir_from_archive()
     -> _validate_skill_frontmatter()
  -> _scan_skill_archive_contents_or_raise()
     -> enforce_static_scan()
     -> scan_skill_content(SKILL.md)
     -> scan_skill_content(code/support files)
  -> _commit_skill_install()
     -> staging copy
     -> _move_staged_skill_into_reserved_target()
```

静态检查先执行，LLM moderation 后执行。脚本类文件只有 `allow` 才能安装；非执行文本可按配置在 moderation 不可用时 warn/fail-open，但 `security_fail_closed` 默认 true。

### 4.2 安全解压

`safe_extract_skill_archive()`：

- 拒绝绝对路径和 `..`；
- 拒绝任意 `:`，防止 Windows NTFS Alternate Data Stream；
- 跳过 symlink；
- 检查 ELF、PE 和完整 Mach-O magic；
- 上限 4096 entries；
- 总解压上限 512 MiB；
- 写入前再次做 resolved containment。

### 4.3 staging 不是目录级原子提交

`_move_staged_skill_into_reserved_target(staging_target, target)` 的实际实现是：

```text
target.mkdir(mode=0700)
for child in staging_target.iterdir():
    shutil.move(child, target / child.name)
失败 -> shutil.rmtree(target)
```

它通过“先保留唯一 target”解决并发同名安装，但不是一次原子 directory rename。观察者理论上可能看到尚未移动完成的目录；失败清理也是补偿式，而非事务提交。

### 4.4 用户存储

`UserScopedSkillStorage` 组合：

```text
public:       {global}/public
custom:       {users}/{user_id}/skills/custom
integrations: managed integration root
legacy:       {global}/custom，只读 fallback
state:        {user skills root}/_skill_states.json
```

启停状态通过同目录临时文件 + `replace()` 原子更新。用户首次拥有 custom 后会遮蔽 legacy fallback，这是设计的 shadow semantics。

### 4.5 热重载

Gateway `POST /api/skills/reload` 触发 versioned cache invalidation：

- 文件 IO 在线程执行；
- 一个进程内只保留一个 refresh worker；
- 新 invalidation 到达时 worker 继续下一轮；
- 等待有 5 秒界限；
- 失败时保留 last-known-good cache，而不是清成空集。

这是单进程 cache 协调，不是多 Worker 一致性协议。

## 5. 激活、秘密与运行状态

### 5.1 slash 激活

`SkillActivationMiddleware` 的核心方法：

```text
_resolve_activation(text)
_build_activation_reminder(activation)
_activation_run_key(target)
_already_activated(run_context, run_key)
_find_activation_target(messages, run_context)
_prepare_model_request(request, hook)
_resolve_secret_bindings(request, activation, hook)
```

流程：

```text
最后一个真实用户消息
  -> parse_slash_skill_reference()
  -> exact name 解析
  -> 检查 installed/enabled/Agent allowlist
  -> 安全读取 SKILL.md
  -> 计算 content hash
  -> HTML 转义 user request、Skill 内容、名称、类别、路径、hash
  -> 注入隐藏 HumanMessage
```

`2fa05050` 用消息 ID，或原始用户文本 SHA-256，生成 run key。该 key 写入 runtime context，解决 reminder 只存在于单次 `request.override()`、下一轮 tool loop 又重新注入的问题。因此一次 slash 命令在一个 Run 内只读盘、注入和审计一次。

### 5.2 durable `skill_context`

模型通过 `read_file` 读取 `SKILL.md` 后，`skill_context.py` 只从配对的 AI tool call 和成功 `ToolMessage` 提取：

```text
path
description
loaded_at
```

路径必须在 Skill root 下、basename 必须为 `SKILL.md`，并要求 ToolMessage 中的 metadata path 与调用 path 一致。渲染时对名称、路径、描述做 HTML escape。

### 5.3 秘密绑定

每次模型调用重新计算绑定，不复用上一轮结果：

1. slash Skill 是显式来源，对声明的同名环境变量有优先权；
2. `skill_context` 中的 Skill 是 autonomous 来源；
3. autonomous 来源每次都重新检查 Skill 仍 enabled、仍在 Agent allowlist、`secrets-autonomous` 未关闭；
4. 值只来自 request-scoped carrier，不从 host env 获取；
5. 多 Skill 对同名秘密给出冲突值或部分缺失时 fail closed；
6. context 中的 active set 被替换，不再激活的 Skill 下一轮自动失去秘密；
7. audit 只记录 Skill 名和秘密名，不记录值。

## 6. `allowed-tools` 的正确授权语义

### 6.1 可发现不等于有权限

`65afc9b1` 引入 `SkillToolPolicyMiddleware`：

- passive、只是已安装/可发现：不限制 Agent；
- slash active：只由当前 slash Skill 决定，且在 Run 内优先；
- `skill_context` active：对实际已加载 Skill 求策略；
- slash 存在时，后来被动读取其他 Skill 不能扩大权限。

### 6.2 策略计算

`allowed_tool_names_for_skills(active_skills)`：

- 所有 active Skill 都未声明：返回 `None`，legacy allow-all；
- 至少一个声明：只并集显式声明；
- 显式空声明参与计算但不贡献业务工具。

框架工具始终保留：

```text
describe_skill
read_file
review_skill_package
tool_search
```

### 6.3 三层 enforcement

仅过滤模型可见 schema 不够，因为模型、恢复状态或恶意 context 仍可能构造 tool call。`SkillToolPolicyMiddleware` 同时控制：

1. `wrap_model_call`：过滤 `request.tools`；
2. `wrap_tool_call`：执行前再次拒绝；
3. `_filter_tool_search_result`：过滤 deferred schema 和 promoted names。

策略决定写入 runtime context 时包含：

```text
version
owner_token
source
active_paths
allowed_names
```

读取时逐项校验 owner token、version、source 和 paths。无法解析真实 active path 时只保留框架安全工具；不接受 caller 自造 allowed list。

## 7. SkillScan

### 7.1 入口与结果

```text
scan_archive_preflight(archive_path) -> ScanResult
scan_skill_dir(skill_dir) -> ScanResult
enforce_static_scan(...) -> findings or StaticScanBlockedError
```

结果包含 findings、scanner errors、blocked 和 stable rule IDs。确定性扫描负责阻止明显危险结构；LLM moderation 是附加语义审查，不替代静态门禁。

### 7.2 `main` 在公共祖先后的检测增强

`main` 最终能够识别：

- `from os import environ` 后的 `environ[...]` / `environ.get(...)`；
- requests/httpx 的 `patch/delete/head/options/request/stream`；
- `urllib.request.urlretrieve`；
- `socket.create_connection`；
- `requests.Session`、`urllib3.PoolManager`、`aiohttp.ClientSession`、`http.client` 实例句柄的数据流调用；
- 分支、局部 scope、赋值重绑定和预算上限；
- `subprocess(..., shell=value)` 中无法证明为 false 的动态值；
- 通过 `**kwargs` 传入的不确定 shell 参数；
- PEP 695 type alias，避免懒求值类型别名被误判为网络调用。

`_call_shell_may_be_true()` 的策略是：只有能确定 `shell=False` 才放行，动态值 fail closed。

### 7.3 `main` 与 `dev` 的扫描差异

`dev` 更强的部分：

- 静态扫描在项目 Skill 发布路径中不可关闭；
- 扫描日志有界；
- 总包上限 100 MiB；
- archive/member 上限 16384；
- 单键 `os.environ["KEY"]` 不误报为“批量环境泄露”；
- 完整 Mach-O magic。

`dev` 回退的部分：

- 丢失 `main` 的额外 requests/httpx/urllib/socket 直接 sink；
- 丢失四类实例 client 数据流；
- 丢失动态/`**kwargs` shell fail-closed；
- archive canonicalizer 未拒绝任意 `:`，`scripts/run.sh:hidden` 在 Windows 上可成为 ADS。

成员数上限不能单独比较优劣：`dev` 是 100 MiB/16384，`main` 是 512 MiB/4096。前者更严总量，后者更严元数据/小文件 DoS。

## 8. Review 子系统

### 8.1 固定契约

三份 JSON Schema：

```text
contracts/skill_review/package_snapshot.v1.schema.json
contracts/skill_review/review_facts.v1.schema.json
contracts/skill_review/review_report.v1.schema.json
```

版本常量：

```text
deerflow.skill-package-snapshot.v1
deerflow.skill-review.facts.v1
deerflow.skill-review.report.v1
```

### 8.2 调用链

```text
inline / local dir / .skill archive / installed skill URI
  -> Reader 构建 PackageSnapshot
  -> analyze_skill_package()
     -> frontmatter
     -> package/resources
     -> eval manifest
     -> SkillScan
     -> digest/completeness
  -> review facts
  -> build_static_report()
  -> readiness: blocked | revise | publish_candidate
  -> EN/ZH markdown
```

`review_skill_package()` 明确不激活、不安装、不执行、不编辑。Local target 只允许当前 workspace、`/tmp` 或配置 Skill root 下的 `.skill`/根含 `SKILL.md` 目录。

模型可见内容并不只是简短结论，而是：

```text
facts
bounded semantic artifacts
static_report
```

semantic artifact 总上限 80,000 字符；完整 payload 和中英 Markdown 放在 artifact。所有模型可见 JSON 先经过 `neutralize_untrusted_tags()`。

### 8.3 Review 是质量判定，不是运行时授权

Review 的 `publish_candidate` 不等于允许执行：

- 它不授予 tool；
- 不绑定 secret；
- 不替代 Agent/Run admission；
- 不把被审内容变成 system instruction；
- 静态事实和语义判断必须在报告中区分。

## 9. `main` 测试与契约

| 测试/契约 | 覆盖 |
| --- | --- |
| `backend/tests/test_skill_tool_policy_middleware.py` | active-only、slash precedence、schema/execute/search |
| deferred policy tests | promotion 后仍不能恢复被禁工具 |
| slash activation tests | exact name、HTML escape、每 Run 一次 |
| request-scoped secret tests | 声明、冲突、缺失、names-only audit |
| installer/storage/parser tests | 路径、frontmatter、per-user/legacy、历史 |
| reload tests | versioned refresh、失败保留旧 cache、timeout |
| `backend/tests/test_skillscan_native.py` | 规则、network client flow、shell 动态值 |
| `backend/tests/test_skill_review_core.py` | snapshot/facts/report/digest |
| `backend/tests/test_review_skill_package_tool.py` | target 边界、非激活语义、artifact |
| `backend/tests/test_review_changed_public_skills.py` | CI 变更集，包括完整删除 |
| `contracts/skill_review/*.schema.json` | 跨实现稳定 JSON 契约 |
| `skills/public/skill-reviewer/evals/evals.json` | 正向、负向、blocked/revise/zh 场景 |

## 10. `3be3969f..main` 提交演进

| 日期 | 提交 | 影响 |
| --- | --- | --- |
| 2026-07-11 | `41658c5f` | Skill Review 质量门禁 |
| 2026-07-12 | `897be7e0` | 识别 `from os import environ` |
| 2026-07-13 | `42544755` | Skill 元数据进入 prompt 前转义 |
| 2026-07-13 | `cbbd72a1` | 补全 requests/httpx 方法 |
| 2026-07-13 | `2fa05050` | slash 每 Run 只激活一次 |
| 2026-07-14 | `81b3ed01` | 补充 urllib/socket 等网络 sink |
| 2026-07-14 | `656f6b36` | Review CI 识别被完整删除的 Skill |
| 2026-07-15 | `16919f7c` | no-arg prompt 使用同一个 resolved app config |
| 2026-07-16 | `65afc9b1` | `allowed-tools` 只作用于 active Skill |
| 2026-07-17 | `1ae02913` | 安全解压增加 entry count |
| 2026-07-17 | `c9b6131f` | 挂载 Skill 热重载 |
| 2026-07-19 | `d2f8f61e` | moderation outage 的 fail-closed 配置 |
| 2026-07-19 | `0cd55067` | 拒绝 archive `:`，阻断 NTFS ADS |
| 2026-07-20 | `a8bf54cb` | 网络 client 实例/数据流分析 |
| 2026-07-20 | `cd34a1a5` | graph 内 moderation 不重复 attach tracing |
| 2026-07-20 | `6544d96c` | 动态 `shell=True` 绕过 fail closed |
| 2026-07-21 | `e66f455d` | PEP 695 误报修复 |
| 2026-07-21 | `3c0a45ad` | standalone moderation 注入 Langfuse metadata |
| 2026-07-24 | `159b7749` | 非字符串 YAML key |
| 2026-07-24 | `25d9ac0a` | history 文件 IO 移出事件循环 |

## 11. `dev` 对应实现

| 层次 | `dev` 路径/符号 |
| --- | --- |
| ORM | `backend/packages/harness/deerflow/persistence/shared_assets/skill_model.py` |
| Service | `backend/app/shared_assets/skill_service.py:SkillService` |
| Archive | `backend/app/shared_assets/skill_archive.py:load_skill_archive_package()` |
| Project API | `backend/app/gateway/routers/project_assets.py` |
| Builder | `backend/app/gateway/routers/project_skill_builder.py` |
| Resolver | `backend/app/shared_assets/resolver.py:ProjectAssetResolver` |
| Snapshot | `backend/app/private_work/snapshot_repository.py` |
| 物化 | `backend/app/private_work/asset_runtime.py:_write_skill_tree()`、`PrivateAssetRuntime.materialize()` |
| runtime parser | `backend/packages/harness/deerflow/skills/parser.py` |
| slash | `backend/packages/harness/deerflow/agents/middlewares/skill_activation_middleware.py` |
| 当前策略 | `backend/packages/harness/deerflow/skills/tool_policy.py` |

### 11.1 数据模型

`skills` 是带 scope/status/current version/optimistic version 的资产头。

`skill_versions` 保存：

```text
description
frontmatter JSON
compatibility
secret_requirements
scan_decision / scan_summary
payload_checksum
workflow/review metadata
```

`skill_version_files` 按 `(skill_version_id, path)` 保存 bytes、MIME、size、SHA-256。数据库约束限制安全相对路径、单文件 100 MiB、内容长度与 size 一致。

### 11.2 authoring 与运行链

```text
上传 zip/tar
  -> load_skill_archive_package()
  -> normalize_skill_files()
  -> 临时目录中解析 frontmatter
  -> 强制 deterministic SkillScan
  -> 写 SkillVersion + 每个文件 + checksum
  -> publish
AgentVersion 精确引用 SkillVersion UUID
  -> Run admission 固化引用和 Credential closure
  -> Worker 重校验
  -> _write_skill_tree() 写 run-owned 临时树
  -> chmod/read-only runtime contract
  -> slash/read_file 只能访问本 Run root
```

秘密值不在模型调用阶段常驻。`PrivateAgentRuntime.materialize_skill_scoped_secrets()` 在 Sandbox command 边界重新校验 Run、membership、资产和 Credential closure，解密一个命令需要的值，返回后清理 carrier。

## 12. `main` 与 `dev` 逐项差异

| 维度 | `main` | `dev` |
| --- | --- | --- |
| 权威存储 | 文件系统 | PostgreSQL 不可变版本 |
| 所有者 | user/global | system/project |
| 修改 | 原地 edit + history | 新 version/fork + publish |
| 启停 | `_skill_states.json` | asset status / project binding |
| Agent 依赖 | Skill 名称 | Skill version UUID |
| Run 一致性 | 运行时读 live 文件 | admitted checksum + exact version |
| 安装门禁 | deterministic + LLM moderation | mandatory deterministic scan |
| Review | 完整 v1 子系统 | 当前无同等 review core |
| `allowed-tools` | active-only middleware | 建图时对全部 runtime Skills 求并集 |
| slash 去重 | runtime context key | 已删除 run-once key |
| secret | request carrier，每模型调用重算 | command 边界重校验/解密 |
| archive | zip `.skill` | zip/tar、100 MiB、16384 members |

## 13. 已确认缺陷与风险

### S-1：`dev` 把 passive Skill 当成 active 权限

位置：

- `backend/packages/harness/deerflow/agents/lead_agent/agent.py`
- `skills_for_tool_policy = list(runtime_skills)`
- `filter_tools_by_skill_allowed_tools(...)`

Agent version 引用的所有 Skill 在建图时一起参与 `allowed-tools`。只要 Skill 可发现，它就开始限制/扩大工具集合；这违反 `main` 已修复的“discoverability is not authority”。

结果可能同时有两类：

- 一个被动 Skill 显式空 allowlist，导致整个 Agent 只剩 `read_file`；
- 一个被动 Skill 声明高权限工具，使实际激活 Skill 的限制被并集扩大。

### S-2：只过滤 schema，不形成稳定执行边界

`dev` 当前静态过滤 `final_tools`，没有 `main` 的：

- 模型调用时基于 active source 刷新决定；
- tool execution 前复核；
- `tool_search` schema/promoted names 过滤；
- owner token/version/source/path 防伪。

恢复/重放、deferred promotion 或未来动态工具装配时会产生旁路。

### S-3：slash 每 tool loop 重复激活

`dev` 删除 `_SLASH_SKILL_ACTIVATION_RUN_KEY`、`_activation_run_key()` 和
`_already_activated()`。reminder 只在单次 request override 中；下一次模型调用从 checkpoint 重建消息后又会读取和注入同一 Skill，并重复 audit。

### S-4：SkillScan 检测能力回退

代码对比确认 `dev` 缺失：

- 多个直接网络 sink；
- requests/urllib3/aiohttp/http.client 实例句柄流；
- 动态 `shell` 和 `**kwargs` fail-closed。

这会让相同恶意包在 `main` 被发现、在 `dev` 只得到更弱或没有 finding。

### S-5：archive 未拒绝 NTFS ADS

`backend/app/shared_assets/skill_archive.py:_canonical_member_path()` 没有拒绝任意 `:`。
`PureWindowsPath("scripts/run.sh:hidden").drive` 为空，不能替代显式 colon 检查。

### S-6：`main` integration 路径有确定 bug

`main` 的 `UserScopedSkillStorage.__init__()` 写入 `_integrations_root`，但
`get_user_integrations_root()` 和 `validate_skill_file_path()` 读取
`_user_integrations_root`，运行时会 `AttributeError`。

同时 `review/readers.py:parse_skill_uri()` 只允许 public/custom/legacy，不接受 integrations。即使修正字段，review URI 仍不完整。该 bug 不应带入 `dev`。

### S-7：`main` staging 目录并非全局原子

安装期间可能观察到已创建但未搬完的 target。`dev` 通过一个数据库事务写版本/文件并以不可变 version 发布，不能退回文件目录提交语义。

### S-8：`dev` `describe_skill` 元数据没有结构转义

`backend/packages/harness/deerflow/skills/describe.py:_render_skill_metadata()` 直接拼接 description 和 location。名称有 slug 约束，但 description 来自项目包。它作为 ToolMessage 数据进入模型，不应允许构造框架标签或伪造 Markdown authority。应复用统一的 untrusted rendering helper。

## 14. 可移植落点

### P0：active-only `allowed-tools`

在 `dev` 新增或恢复中间件，但数据源必须改成 run-exact tuple：

- `backend/packages/harness/deerflow/agents/middlewares/skill_tool_policy_middleware.py`
- `backend/packages/harness/deerflow/agents/lead_agent/agent.py:build_middlewares()`
- `backend/packages/harness/deerflow/skills/tool_policy.py`
- `backend/app/private_work/asset_runtime.py:PrivateAgentRuntime`

要求：

1. 只从 `runtime_skills` 建 registry，不读 live 全局 storage；
2. active source 只接受 slash 或经过配对验证的 `skill_context`；
3. slash 优先；
4. 决定包含 middleware-local owner token、version、source、exact path/version；
5. 同时过滤 schema、执行和 `tool_search`；
6. 无法解析 active reference 时 fail closed；
7. `describe_skill`、`read_file`、`tool_search` 保留；`review_skill_package` 仅在真正引入 review 后保留。

### P0：补回 SkillScan 规则

落点：

- `backend/packages/harness/deerflow/skills/skillscan/orchestrator.py`
- `backend/tests/test_skillscan_native.py`

逐项 cherry-pick 算法和测试，不覆盖 `dev` 的 100 MiB、mandatory scan、日志有界和 Mach-O 改进。

### P1：slash 每 Run 一次

落点：

- `backend/packages/harness/deerflow/runtime/secret_context.py`
- `backend/packages/harness/deerflow/agents/middlewares/skill_activation_middleware.py`

run key 应绑定 `project_id + owner_user_id + run_id + message id/hash`，并列入 context redaction key。不要把秘密值写入 key 或 checkpoint。

### P1：ADS 防护

在两个入口同时拒绝：

- `backend/app/shared_assets/skill_archive.py:_canonical_member_path()`
- `backend/packages/harness/deerflow/skills/skillscan/orchestrator.py:_normalize_archive_name()` / preflight

zip 和 tar 都应覆盖，不能只改 ZIP helper。

### P1：Skill 元数据安全渲染

修改：

- `backend/packages/harness/deerflow/skills/describe.py:_render_skill_metadata()`
- `get_skill_index_prompt_section()` 的名字和路径防御性转义

使用统一 data rendering，不应靠输入 sanitizer，因为这些内容不是用户消息。

### P2：Review v1 适配数据库资产

保留：

- `review/analyzer.py`
- `review/models.py`
- `review/renderer.py`
- 三份 JSON Schema
- CI 的确定性报告

重写 reader：

```text
PostgresSkillVersionReader(
    project_id,
    skill_id,
    version_id,
    expected_checksum,
) -> PackageSnapshot v1
```

读取必须走项目 scope/capability 和 immutable version，报告 subject 使用无秘密的 asset/version/checksum。不要恢复旧 `skill://custom/...` 权威。

LLM semantic review 建议作为治理证据，不默认成为 deterministic publish 的唯一阻断条件。

## 15. 禁止合并项

不能从 `main` 直接合入：

1. `LocalSkillStorage` / `UserScopedSkillStorage` 作为权威。
2. `_skill_states.json`。
3. 旧 `/api/skills` install/edit/delete/history/rollback/toggle/reload 路由。
4. 运行时 `skill_manage` 对已发布 Skill 的原地修改。
5. 文件目录“最新内容”覆盖 Run exact version。
6. `skill://custom/...` reader 作为项目资产读取方式。
7. global mounted integrations 作为跨项目共享授权。
8. LLM moderation 结果直接替代静态门禁、审批或 capability。
9. `main` 的 512 MiB/4096 限制整体覆盖 `dev` 的 100 MiB/16384 策略。

## 16. 建议测试矩阵

| 类别 | 场景 | 预期 |
| --- | --- | --- |
| frontmatter | absent/null/empty/non-string `allowed-tools` | 三态精确 |
| active policy | passive Skill 有 allowlist | 不改变工具 |
| active policy | slash Skill + 后读另一 Skill | slash 不被扩大 |
| active policy | 多 in-context Skill | 只并集 active |
| active policy | 显式空 allowlist | 只剩框架安全工具 |
| active policy | forged context/owner token/version | fail closed |
| deferred | 被禁工具被 `tool_search` 搜到 | schema 和 promotion 都被过滤 |
| execution | 直接构造被禁 ToolCall | 执行前拒绝 |
| slash | 同 Run 多次 model loop | 只激活/审计一次 |
| slash | 新 Run 同一消息文本 | 可重新激活 |
| secret | 多 Skill 同名同值/异值/缺失 | 同值可用，异值/部分缺失 fail closed |
| secret | membership/Run lease 调用前撤销 | 不解密，不启动命令 |
| archive | zip/tar 绝对路径、`..`、symlink、ADS | 全部拒绝 |
| archive | 100 MiB、16384 member 边界 | 确定性接受/拒绝 |
| SkillScan | Session/PoolManager/ClientSession 数据流 | 命中稳定 rule ID |
| SkillScan | `shell=flag`、`**kwargs` | 动态不确定值阻断 |
| SkillScan | PEP 695 type alias | 不误报 |
| version | 发布后创建新版本 | 已准入 Run 仍读旧 bytes/checksum |
| isolation | 跨 project/version UUID 猜测 | 404/403 且无 metadata 泄漏 |
| review | 同一版本重复分析 | digest/facts/report 字节稳定 |
| review | 模型阅读恶意 SKILL.md | 内容仍标为 untrusted，不激活 |
