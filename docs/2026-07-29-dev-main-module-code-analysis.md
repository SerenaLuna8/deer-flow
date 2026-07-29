# dev 建立后 main 更新的代码级模块分析

- 日期：2026-07-29
- 文档性质：分支差异分析与选择性移植依据
- 当前分支：`dev`
- 本地 reflog 记录的 `dev` 建立时间：2026-07-10 09:31:24 +0800
- `dev` 创建基点：`3be3969f8fc3f2d2b6d36ef5c26fa5593d916f2a`
- 当前 `dev`：`8a91e95799c9b345d9540c7e201b33c603e7870c`
- 当前 `main`：`e317f7b8d9b2afb4c3925812d4774da602c9f8f3`
- 分析范围：`3be3969f..main`
- 兼容性对照：`dev..main`

## 1. 文档目的

本文档深入分析 `dev` 分支建立以后进入 `main` 的代码更新，并把这些更新与当前
`dev` 的项目优先、多用户 SaaS M7 架构逐项对照。

本文档不是普通提交日志，也不是把 `main` 合并到 `dev` 的执行方案。每个模块分别回答：

1. `main` 实际新增或修改了哪些代码。
2. 关键入口、类、函数和调用链是什么。
3. 当前 `dev` 中对应的权威实现在哪里。
4. 哪些能力已经被替代。
5. 哪些修复值得按 `dev` 架构手工移植。
6. 哪些代码不能直接 cherry-pick 或 merge。

Agent 和 Skill 在本文中是两个完全独立的模块，不混合描述。

## 2. 分析方法与边界

### 2.1 使用的比较视角

本文同时使用两个比较视角：

- 历史增量：`git diff 3be3969f..main`
  - 回答 `dev` 建立以后 `main` 做了什么。
- 当前兼容性：`git diff dev..main`
  - 回答这些实现与当前 `dev` 的代码和架构是否兼容。

本地 reflog 显示 `dev` 于 2026-07-10 09:31:24 +0800 从 `main` 的
`3be3969f` 建立；第一条 dev-only 提交是 `098d9979`，提交时间为
2026-07-10 09:59:49 +0800。reflog 是本地记录，其他 clone 不一定保留相同的建分支事件，
因此历史增量仍以共同祖先 commit 为可复现边界。

对于关键结论，额外使用了：

- `git show main:<path>` 查看 `main` 的最终实现；
- 当前工作树文件查看 `dev` 的实际实现；
- 相关测试、配置模型和持久化模型交叉验证；
- 关键提交的 patch，而不是只读取提交标题。

### 2.2 规模

从共同祖先到当前 `main`：

- `main` 独有 342 个提交；
- 944 个文件发生变化；
- 新增 157,484 行；
- 删除 12,555 行；
- 367 个新增文件；
- 569 个修改文件；
- 5 个删除文件；
- 3 个重命名文件。

当前 `dev` 自共同祖先起另有 467 个独有提交。直接比较当前两个分支：

- 2,211 个文件不同；
- `main` 相对 `dev` 新增约 264,944 行；
- `main` 相对 `dev` 删除约 290,340 行。

这些数字说明两个分支是长期双向演进，不是简单的“`main` 比 `dev` 多 342 个提交”。

### 2.3 排除项

- 当前工作树中未跟踪的 `docker/` 目录不属于 Git 对比结果，未纳入本次分析。
- 本次工作没有切换分支、修改配置、修改数据库、启动或停止服务。
- 本文没有把历史测试通过记录当作当前 `dev` 的验证结果。

### 2.4 路径标记

- `main:` 表示路径来自 `main@e317f7b8`，当前 `dev` 中可能已经不存在。
- `dev:` 表示当前 `dev@8a91e957` 的权威路径。

## 3. 总体结论

最重要的结论是：`main` 的大量更新仍建立在旧运行模型上，而当前 `dev` 已经完成了
项目优先和 Worker-only 的架构重构。

### 3.1 main 的运行模型

```text
/workspace
  -> 全局 /api/threads、/api/runs
  -> Gateway RunManager
  -> Gateway asyncio.create_task(run_agent)
  -> MemoryStreamBridge 或 RedisStreamBridge
  -> SSE
```

### 3.2 dev 的运行模型

```text
/workspace 项目入口
  -> /projects/{project_slug}
  -> ProjectContext + Capability
  -> PrivateWorkContext(project_id + owner_user_id)
  -> Gateway 事务准入 Run + Snapshot + Job
  -> 独立 Worker claim PostgreSQL Job lease
  -> Worker-only Agent graph
  -> PostgreSQL durable stream
  -> Gateway 只读回放 SSE
```

### 3.3 不能共存的边界

以下两套机制不能同时存在：

| 领域 | main | 当前 dev |
| --- | --- | --- |
| Run 准入 | Gateway `RunManager.create_or_reject()` | `PrivateRunAdmissionService` 事务准入 |
| Agent 执行 | Gateway 后台协程 | 独立 Worker |
| 执行所有权 | Gateway worker lease | PostgreSQL Job lease + Run execution lease |
| 实时流 | Memory/Redis 临时流 | PostgreSQL durable stream |
| Agent 定义 | 用户文件或旧式 SQL JSON 行 | system/project 不可变 AgentVersion |
| Skill 定义 | 文件目录和运行时 reload | system/project 不可变 SkillVersion |
| MCP 定义 | 文件配置和全局 cache | admitted exact MCP snapshot |
| 私有范围 | 用户/Thread 为主 | `project_id + owner_user_id` |
| Schema 生命周期 | 多个增量 migration | 唯一 `full_schema.sql` |
| 前端根路由 | `/workspace` 直接承载聊天 | `/workspace` 仅项目入口，业务位于 `/projects/{slug}` |

因此，正确做法是提取 `main` 的行为、算法、安全不变量和测试，再在 `dev` 的权威边界内重写。

## 4. 移植标签

本文使用以下标签：

- **已具备**：`dev` 已有等价行为。
- **已替代**：`dev` 已有更强或不同的权威实现。
- **可手工移植**：能力有价值，但必须适配项目、owner、Worker 和 PostgreSQL 边界。
- **产品决策**：需要先决定数据模型或产品语义。
- **不适用**：只服务于已经删除的旧架构。
- **禁止直接合并**：会造成数据表、权限、执行所有权或持久化冲突。

## 5. Agent 模块

### 5.1 main 的核心文件

- `main:backend/packages/harness/deerflow/agents/lead_agent/agent.py`
- `main:backend/packages/harness/deerflow/agents/lead_agent/prompt.py`
- `main:backend/packages/harness/deerflow/config/agents_config.py`
- `main:backend/packages/harness/deerflow/config/agent_storage_config.py`
- `main:backend/packages/harness/deerflow/persistence/agents/base.py`
- `main:backend/packages/harness/deerflow/persistence/agents/file.py`
- `main:backend/packages/harness/deerflow/persistence/agents/sql.py`
- `main:backend/app/gateway/routers/agents.py`
- `main:backend/packages/harness/deerflow/agents/middlewares/configured_extensions.py`
- `main:backend/packages/harness/deerflow/agents/middlewares/subagent_limit_middleware.py`

### 5.2 main 的 Agent 管理链

```text
/api/agents
  -> 解析 effective user
  -> 名称、模型、请求字段校验
  -> asyncio.to_thread()
  -> get_agent_store()
  -> FileAgentStore 或 SqlAgentStore
```

`AgentStore` 是同步抽象，负责：

- `get`
- `exists`
- `get_soul`
- `list`
- `list_all`
- `create`
- `update`
- `delete`
- `signature`

File backend 使用每用户目录中的 `config.yaml + SOUL.md`。单文件采用临时文件原子替换，
但两个文件不是同一事务。

SQL backend 使用旧式 `agents` 表：

- 一行代表一个用户可变 Agent；
- `config` 保存 JSON；
- `soul` 独立保存；
- `(user_id, name)` 唯一；
- SQLite 会设置 WAL、`foreign_keys`、`busy_timeout`；
- 提供 file 到 DB 的显式、幂等、非破坏迁移脚本。

### 5.3 main 的 Agent 执行链

```text
make_lead_agent()
  -> resolve_config_user_id()
  -> AgentStore 读取 AgentConfig/SOUL
  -> 解析模型与生成参数
  -> 加载 Skill 和 Tool
  -> apply_tool_authorization()
  -> deferred tool assembly
  -> build_middlewares()
  -> create_agent()
```

模型和参数不是一条完全相同的覆盖链：

- model name：request > Agent > global default；
- thinking/reasoning：request > Agent > runtime default；
- temperature/max tokens：Agent 的显式 override 叠加到 model profile。

`AgentConfig` 增加：

- `model_settings.temperature`
- `model_settings.max_tokens`
- `thinking_enabled`
- `reasoning_effort`

代码使用 Pydantic 的 `model_fields_set` 区分“字段省略”和“显式 null”，因此更新时可以做部分合并。

### 5.4 main 的 Agent 行为变化

1. 每个 Agent 可以覆盖模型、temperature、max tokens、thinking 和 reasoning。
2. 支持 `extensions.middlewares` 中声明 `module:Class` 形式的零参数 middleware。
3. named Agent 才能获得 `update_agent`；GitHub webhook 等不可信渠道显式禁用。
4. SOUL 放入 `<soul>` 标签前进行 HTML 转义。
5. 子 Agent 限制从“单次响应并发数”扩展为“单 Run 总委派数”。
6. `view_image` 不再把 Base64 图片长期保存到 checkpoint。
7. 模型调用结束后移除临时图片 HumanMessage。
8. 标题 fallback 在拼接省略号前预留长度。
9. `web_fetch` 的 404/503 错误页可以按失败证据分类。

关键提交：

- `20debf9c`：每 Agent 模型和生成设置；
- `0d4d0cb1`：数据库 Agent Store；
- `ca16b64b`：配置声明 middleware；
- `4e209827`：单 Run 总委派上限；
- `807c3c52`：SOUL 结构转义；
- `713ee544`、`ce4a6d4`：图片 checkpoint 清理；
- `ca18cf0b`：标题长度；
- `1a1c5def`：`web_fetch` 错误页。

### 5.5 当前 dev 的 Agent 权威模型

当前 `dev` 的 Agent 管理链已经变为：

```text
dev:backend/app/gateway/routers/project_assets.py
  -> dev:backend/app/shared_assets/agent_service.py
  -> dev:backend/app/shared_assets/agent_repository.py
  -> dev:backend/packages/harness/deerflow/persistence/shared_assets/agent_model.py
```

当前物理模型：

- `agents` 是 system/project 级资产；
- 有 `active/suspended/archived` 生命周期；
- 有 `current_published_version_id`；
- `agent_versions` 是不可变版本；
- 版本包含 checksum 和 workflow status；
- 使用 `model_ref`、tool groups 和精确 Skill/MCP 版本引用；
- v2 Agent 有四份逻辑文档：
  - `AGENTS.md`
  - `SOUL.md`
  - `IDENTITY.md`
  - `USER.md`

执行链：

```text
Thread 绑定 Agent
  -> ProjectAssetResolver 解析当前发布版本
  -> Run admission 固化 Agent/Skill/MCP/Credential 精确闭包
  -> SnapshotRepository 持久化 exact IDs/checksums
  -> Worker 重新校验闭包
  -> PrivateAssetRuntime.materialize()
  -> run-owned PrivateAgentRuntime
  -> lead factory 使用 exact model_ref/tools/skills/prompt bundle
```

Gateway 不执行 Agent 图，运行中的 Agent 也没有自修改工具。

### 5.6 Agent 可移植性

| main 能力 | 结论 | 原因 |
| --- | --- | --- |
| `AgentStore` 和 `agent_storage` | 禁止直接合并 | main 和 dev 都使用 `agents` 表名，但语义和列完全不同 |
| Alembic Agent migration | 不适用 | dev 只允许 `full_schema.sql` 初始化 |
| 旧 `/api/agents` CRUD | 已替代 | dev 使用项目资产、不可变版本和能力校验 |
| `setup_agent/update_agent` | 已替代 | 会破坏 admitted immutable snapshot |
| 每 Agent 模型设置 | 产品决策 | 应成为 AgentVersion payload、checksum、snapshot 和 Worker manifest 的一部分 |
| 配置 middleware | 产品决策 | 只能作为可信 operator 级、重启生效能力 |
| Run 总委派 cap | 可手工移植 | dev 目前只有每响应并发上限 |
| SOUL/文档结构转义 | 可手工移植，优先 | dev 当前四文档渲染未转义正文 |
| 图片 Base64 checkpoint 清理 | 可手工移植，优先 | dev 当前 `viewed_images` 仍保存 Base64 |
| 标题和 web_fetch 小修 | 可手工移植 | 与项目权威边界耦合较低 |

### 5.7 Agent 代码发现

#### A-1：Agent 四文档结构正文未转义

当前 `dev:backend/packages/harness/deerflow/agents/lead_agent/prompt.py`
的 `render_agent_prompt_bundle()` 直接把项目可写正文放入：

```text
<agent_profile_document>
...
</agent_profile_document>
```

v1 `exact_soul` 也直接插入 `<soul>`。正文可以构造关闭标签，破坏项目指令结构边界。
`main` 的 SOUL 转义修复应扩展成四文档统一转义，而不是只复制旧 `<soul>` 实现。

#### A-2：图片 Base64 进入 checkpoint

当前 `dev`：

- `ViewedImageData` 仍包含 `base64`；
- `ViewImageMiddleware` 从 state 读取 Base64；
- 临时图片消息没有在模型调用后删除。

这会随 Thread checkpoint 持续放大数据库存储和每次 state materialization 成本。
适配时不应直接照搬 main 的 host `actual_path`。dev 应只保存 Worker 可重新授权读取的
opaque file reference/locator，以及 MIME、大小和 hash；调用前按本地/远程 Sandbox
权限边界重新解析、读取并校验，调用后删除临时消息。

#### A-3：旧 Agent Store 与 dev 是物理冲突

main 新增的 SQL Store 不是 dev 资产表的简化版本。它是一套不同的行模型、更新语义和
迁移生命周期。即使解决 Git 冲突，也会产生数据库语义冲突。

## 6. Skill 模块

### 6.1 main 的核心文件

- `main:backend/packages/harness/deerflow/skills/installer.py`
- `main:backend/packages/harness/deerflow/skills/parser.py`
- `main:backend/packages/harness/deerflow/skills/frontmatter.py`
- `main:backend/packages/harness/deerflow/skills/skillscan/orchestrator.py`
- `main:backend/packages/harness/deerflow/skills/storage/user_scoped_skill_storage.py`
- `main:backend/packages/harness/deerflow/agents/middlewares/skill_activation_middleware.py`
- `main:backend/packages/harness/deerflow/agents/middlewares/skill_tool_policy_middleware.py`
- `main:backend/packages/harness/deerflow/skills/review/`
- `main:backend/packages/harness/deerflow/tools/builtins/review_skill_package_tool.py`
- `main:contracts/skill_review/`

### 6.2 main 的 Skill 安装链

```text
Skill archive
  -> static archive preflight
  -> bounded safe extraction
  -> frontmatter parser/validator
 -> directory SkillScan
  -> 每个可扫描文件执行 LLM moderation
  -> staging target
  -> 预留目标目录后分项搬移
  -> 失败时清理目标
```

最后一步不是目录级原子 rename：提交期间目标目录已经可见，只是失败路径会尽力清理。

`UserScopedSkillStorage` 聚合：

- public Skill；
- 每用户 custom Skill；
- integration Skill；
- legacy fallback；
- `_skill_states.json` 启停状态。

旧 `/api/skills` 支持安装、编辑、删除、历史、rollback、toggle 和 Gateway 进程内 reload。

### 6.3 main 的 Skill 激活链

```text
真实用户 Slash 消息
  -> parse_slash_skill_reference()
  -> 校验 installed/enabled/Agent allowlist
  -> 安全读取 SKILL.md
  -> SHA256
  -> XML 转义
  -> 注入隐藏 HumanMessage
  -> Run context 记录激活 identity
```

同一个 Run 的 tool loop 不会重复读盘、重复注入或重复审计 Slash Skill。

Secret 每次模型调用重新计算：

- Slash source 优先；
- `skill_context` 自主 source 次之；
- 重新校验实时 registry、路径、启用状态和 allowlist；
- 值只来自 request secret carrier；
- 审计只记录 secret 名称。

### 6.4 active-only allowed-tools

`main:SkillToolPolicyMiddleware` 把“可发现”和“已激活”分离：

1. 仅启用 Skill 不会立刻改变工具权限。
2. Slash 激活或 `ThreadState.skill_context` 才激活 policy。
3. Slash source 在整个 Run 内优先。
4. 多个 active Skill 的显式 `allowed-tools` 取并集。
5. 未声明表示 legacy allow-all。
6. 显式空列表表示没有业务工具。
7. 始终保留必要框架工具。
8. 同时过滤：
   - 模型可见 tool schemas；
   - 实际 tool call；
   - `tool_search` 返回的 schema；
   - `tool_search` promotion。
9. active 引用无法解析时 fail closed。
10. policy decision 带 owner token、版本、source 和路径，拒绝伪造或 stale 决策。

### 6.5 Skill Review 子系统

main 新增纯分析流水线：

```text
Reader
  -> PackageSnapshot v1
  -> deterministic analyzer
  -> frontmatter/resource graph/evals/SkillScan/digest
  -> ReviewFacts v1
  -> renderer
  -> ReviewReport v1
```

输出结论：

- `blocked`
- `revise`
- `publish_candidate`

Review 保证：

- 不激活；
- 不安装；
- 不执行；
- 不编辑；
- Skill 内容按不可信文本处理；
- 模型看到紧凑 facts/report 和有界语义材料，语义材料上限 80,000 字符；
- 完整 review payload 和中英文 Markdown 放入 artifact；
- 提供 schema contract 和 CI。

### 6.6 SkillScan 增量

main 在原规则集上增加：

- `from os import environ`；
- `requests/httpx` 的 patch、delete、head、options、request、stream；
- `urllib.urlretrieve`；
- `socket.create_connection`；
- requests Session、urllib3 PoolManager、aiohttp ClientSession、`http.client` 等实例 client；
- 构造、别名、分支和 dataflow 追踪；
- `subprocess shell=` 只有字面 `False` 才安全；
- 变量表达式和 `**kwargs` fail closed；
- archive entry 数量限制；
- 任意 archive member 路径中的 `:` 拒绝，阻断 NTFS ADS；
- moderation 服务失败时 executable 永远 block。

关键提交：

- `41658c5f`：Skill Review；
- `65afc9b1`：active-only allowed-tools；
- `2fa05050`：Slash 每 Run 一次；
- `897be7e0`、`cbbd72a1`、`81b3ed01`、`a8bf54cb`：网络 sink；
- `6544d96c`：`shell=` 绕过；
- `1ae02913`：archive entry cap；
- `0cd55067`：NTFS ADS；
- `159b7749`：非字符串 frontmatter key。

### 6.7 当前 dev 的 Skill 权威模型

```text
project_assets.py / project_skill_builder.py
  -> SkillService
  -> skills / skill_versions / skill_version_files
  -> immutable checksum + workflow status
```

执行链：

```text
AgentVersion 固定 SkillVersion IDs
  -> Run admission 固化 Skill + Credential closure
  -> Worker 创建 run-owned 只读 Skill tree
  -> Skill(runtime_read_only=True)
  -> Slash 只从当前 Run root 读取
  -> sandbox 单命令边界重新验证并短时解密 Credential
```

这比 main 的用户目录、运行时 reload 和 flat request secrets 有更强的项目、owner 和版本隔离。

### 6.8 Skill 可移植性

| main 能力 | 结论 | 适配要求 |
| --- | --- | --- |
| 用户目录 storage/reload | 禁止直接合并 | 已由不可变 DB 版本替代 |
| `_skill_states.json` | 不适用 | dev 使用 system/project binding |
| 旧安装器和 `/api/skills` | 已替代 | dev 有 SkillService 和版本 workflow |
| flat request secrets | 禁止直接合并 | dev 必须保留 exact Credential closure |
| active-only allowed-tools | 可手工移植，优先 | 基于 `PrivateAgentRuntime.skills`，不能读全局 storage |
| SkillScan AST 强化 | 可手工移植，优先 | 保留 dev 的强制开启、100 MiB 和有界日志 |
| Slash 每 Run 一次 | 可手工移植 | internal carrier 必须不可伪造且被 redaction |
| archive `:`/ADS | 可手工移植 | 应进入 dev canonical parser，覆盖 ZIP 和 TAR |
| Skill Review | 可手工移植 | Reader 改为 project/asset/version/checksum |
| LLM moderation 阻断安装 | main 行为；dev 中是产品决策 | 决定作用于 preview/version/publish，或仅作为审批证据 |

### 6.9 Skill 代码发现

#### S-1：dev 的 allowed-tools 在 Agent 构造阶段应用

当前 `dev:backend/packages/harness/deerflow/agents/lead_agent/agent.py` 对全部 `runtime_skills` 调用
`filter_tools_by_skill_allowed_tools()`。未激活 Skill 的声明也会参与工具集合计算。

main 的 active-only 设计不能原样复制，因为它读取全局 Skill storage。正确适配应：

1. 先完成 Agent tool groups 和平台授权过滤；
2. 从 immutable `PrivateAgentRuntime.skills` 获取 exact Skill；
3. 在模型 schema、执行和 `tool_search` 三个位置动态过滤；
4. Slash 优先；
5. 引用无法验证时 fail closed。

#### S-2：dev SkillScan 缺少 main 的多类 sink

当前 dev 的直接 sink 覆盖范围比 main 少，且 `shell=flag`、`**kwargs` 没有按 main 的
fail-closed 逻辑处理。应移植 main 的 AST 测试和实现，但不能覆盖 dev 已有的以下强化：

- static SkillScan 强制开启；
- 100 MiB 解压总量；
- 完整 Mach-O magic；
- 日志和错误输出有界；
- 单 key `os.environ[...]` 不误判成 bulk dump。

#### S-3：archive canonical parser 未拒绝所有冒号

`dev:backend/app/shared_assets/skill_archive.py` 依赖 Windows path 检查，但
对 `scripts/run.sh:hidden`，`PureWindowsPath(...).drive` 为空，因此现有检查不能阻断这种
NTFS ADS 形状。

#### S-4：main 的 integration storage 自身有缺陷

main `UserScopedSkillStorage` 初始化 `_integrations_root`，但部分方法引用
`_user_integrations_root`；`InstalledSkillReader` 也不接受 integrations URI。
即使参考 main，也不能整块复制其 storage/review reader。

## 7. Memory 模块

### 7.1 main 的核心文件

- `main:backend/packages/harness/deerflow/agents/memory/manager.py`
- `main:backend/packages/harness/deerflow/agents/memory/backends/deermem/`
- `main:backend/packages/harness/deerflow/agents/memory/backends/openviking/`
- `main:backend/packages/harness/deerflow/agents/memory/backends/mem0/`
- `main:backend/packages/harness/deerflow/agents/memory/backends/noop/`
- `main:backend/packages/harness/deerflow/agents/memory/tools.py`
- `main:backend/packages/harness/deerflow/config/memory_config.py`
- `main:backend/packages/harness/deerflow/agents/middlewares/memory_middleware.py`

### 7.2 main 的 Memory 调用链

```text
Agent 构建
  -> get_memory_manager()
  -> manager_class
  -> DeerMem / OpenViking / Mem0 / Noop

首次可用 turn，且 state 中尚无动态 reminder
  -> lead_agent.prompt._get_memory_context()
  -> manager.get_context()
  -> <memory> 隐藏消息写入 state
  -> 同日后续 turn 复用 checkpoint 中的冻结快照

被动写入启用时
  -> MemoryMiddleware
  -> manager.add()/aadd()
  -> 后端队列、文件或 HTTP 服务

上下文压缩前
  -> memory_flush_hook()
  -> manager.add_nowait()

tool 模式
  -> memory_search/add/update/delete
```

`MemoryManager` 的稳定核心是：

- `add()`
- `get_context()`

搜索、CRUD、导入导出、warm、reload 和 flush 是可选能力。
`mode=middleware|tool` 决定被动写入还是由模型显式使用 Memory 工具。tool mode 通常不安装
被动写入 middleware；只有后端声明 `requires_passive_writes_in_tool_mode=True` 时仍保留，
当前符合这一条件的是 Mem0。

### 7.3 main 的 DeerMem 持久化变化

- 用户摘要仍位于 `users/{user}/memory.json`；
- fact 改为 Agent 隔离的分片 Markdown；
- 增加 manifest revision 和 fact revision；
- 使用文件锁、原子写、journal 和崩溃恢复；
- FTS5/BM25 是派生索引，Markdown 是事实源；
- 增加旧 JSON 数据迁移；
- 支持每条 fact 的有效期；
- 支持 staleness extension；
- 支持 consolidation；
- OpenViking 使用 HTTP 和 durable watermark；
- Mem0 使用 HTTP 适配器；
- 进程退出时尝试有界 flush。

重要提交：

- `ad45f59d`：可插拔 Memory；
- `01a89f23`：稳定 `MemoryManager` 接口；
- `4bf028d0`：Agent 隔离 Markdown facts；
- `795af20a`：FTS5/BM25；
- `8145d66a`：消息处理；
- `2aaf74b0`、`9bb82250`：OpenViking；
- `352f247a`：Mem0；
- `b3af8c91`：tool mode 显式 recall。

### 7.4 当前 dev 的 Memory 权威模型

核心文件：

- `dev:backend/app/private_work/memory_service.py`
- `dev:backend/packages/harness/deerflow/persistence/private_work/memory_repository.py`
- `dev:backend/packages/harness/deerflow/agents/memory/storage.py`
- `dev:backend/packages/harness/deerflow/agents/memory/queue.py`
- `dev:backend/packages/harness/deerflow/agents/middlewares/dynamic_context_middleware.py`

调用链：

```text
Worker 注入 PrivateResourceScope
  -> DynamicContextMiddleware.abefore_agent()
  -> state 尚无动态 reminder 时重新校验项目成员
  -> ProjectMemoryStorage.load()
  -> PostgreSQL user_project_memories / facts
  -> format_memory_for_injection()
  -> <memory> 隐藏消息写入 state
  -> 同日后续 turn 复用 checkpoint 中的冻结快照

每轮结束或压缩前
  -> MemoryMiddleware / memory_flush_hook
  -> ProjectMemoryUpdateQueue
  -> 再校验 membership_version
  -> MemoryUpdater
  -> PostgreSQL 乐观版本写入
```

dev 中的 `lead_agent.prompt._get_memory_context()` 明确返回空字符串，禁止无项目作用域的全局
Memory 读取。Memory 注入只能走项目和 owner 绑定的异步 PostgreSQL 路径。这里不是每轮实时重读：
首个可用 turn 固化快照，同日后续 turn 复用 state 中的隐藏 reminder。

### 7.5 Memory 可移植性

| main 能力 | 结论 |
| --- | --- |
| `MemoryManager` 作为运行时权威后端工厂 | 已替代 |
| DeerMem 本地 Markdown 作为事实源 | 禁止直接合并 |
| OpenViking/Mem0 替换 PostgreSQL authority | 禁止直接合并 |
| 消息筛选、外置 prompt/pattern | 可手工移植 |
| consolidation、fact 有效期 | 可手工移植 |
| FTS/BM25 思想 | 可手工移植到 PostgreSQL fact 表 |
| Remote Memory | 产品决策，必须纳入项目 Credential 和 admitted snapshot |

main 的 `memory.mode/manager_class/backend_config` 与 dev 的项目 Memory 数据模型语义冲突。

### 7.6 Memory 安全迁移要求

若移植 main 的提示模板或 consolidation，必须同时移植并扩展以下结构转义：

- fact content；
- Memory state；
- summary；
- conversation block；
- staleness/consolidation 输入。

相关 main 提交：

- `54f3c43f`
- `938391c1`
- `158c4f96`
- `feb28707`
- `8e96a6a2`

这些修复应落在 dev 的 PostgreSQL project-owner Memory 流程中，不能恢复全局 manager 入口。

## 8. Subagent 模块

### 8.1 main 的核心文件

- `main:backend/packages/harness/deerflow/tools/builtins/task_tool.py`
- `main:backend/packages/harness/deerflow/subagents/executor.py`
- `main:backend/packages/harness/deerflow/subagents/status_contract.py`
- `main:backend/packages/harness/deerflow/subagents/step_events.py`
- `main:backend/packages/harness/deerflow/subagents/token_collector.py`
- `main:backend/packages/harness/deerflow/agents/middlewares/subagent_limit_middleware.py`
- `main:backend/packages/harness/deerflow/agents/middlewares/durable_context_middleware.py`

### 8.2 main 的 Subagent 调用链

```text
Lead Agent 生成 task tool call
  -> SubagentLimitMiddleware
  -> task_tool()
  -> 传递父 Run 身份和限制
  -> Subagent 配置可覆盖模型
  -> 工具经过子级 allow/disallow
  -> Skill 取父范围与子配置交集
  -> SubagentExecutor
  -> 单一 SystemMessage + HumanMessage
  -> create_agent(checkpointer=False)
  -> 独立 event loop
  -> astream(values)
  -> 捕获 AI/Tool steps
  -> task_running / terminal event
  -> ToolMessage + delegation ledger
```

main 的增强：

- `checkpointer=False`；
- 保留父图 subgraph namespace；
- ID set + 尾游标避免 O(n²) step 捕获；
- 摘要压缩后重置捕获游标；
- marked LLM fallback 映射为失败；
- recursion 路径优先识别 marked LLM fallback，再进入既有 cap outcome；
- 每 Run 最多 6 次 delegation，配置范围 1–50；
- delegation ledger 带 `run_id`；
- 终态不会被后来的非终态覆盖；
- 总量耗尽时删除超限 task call，并注入模型可见提示；
- task card contract 带模型和 token usage；
- step batch 写失败会重新放回 pending；
- callback 仅剥离 loop-bound 项；
- Skill 延迟激活。

关键提交：

- `266883b3`：总结继承和 step 捕获；
- `bbb3deb2`、`2bd0f56a`：fallback 分类；
- `aafd5077`：模型和 token；
- `4e209827`：总 delegation cap；
- `18c32bea`：失败 re-buffer；
- `de55982c`：父 checkpoint namespace；
- `a5059b82`：callback 隔离和 lazy Skill。

### 8.3 当前 dev 的 Subagent 边界

当前 dev 保留了 Subagent 框架，但增加了项目私有能力：

- 只继承 Worker materialize 的 immutable `runtime_skills`；
- 传递 private scope 和文件 authority；
- 传递授权边界；
- 传递只读 Skill mount；
- 传递 exact Agent prompt bundle；
- Skill Credential 和 MCP 调用代理回 owner Worker loop；
- `checkpointer=False`；
- 尾游标只扫描新增消息，ID set 查找均摊 O(1)，避免全历史重复扫描和 O(n²)；
- 已有 recursion partial result；
- 已有 token collector；
- 已有项目 Run 汇总 token。

### 8.4 Subagent 可移植性

| main 能力 | 结论 |
| --- | --- |
| 本地 user Skill storage 继承 | 已替代 |
| marked LLM fallback 分类 | 可手工移植，优先 |
| 普通 recursion cap outcome | dev 已具备 |
| recursion 路径优先识别 marked fallback | 可手工移植 |
| per-run total delegation cap | 可手工移植，优先 |
| step flush re-buffer | 可手工移植 |
| 压缩后 cursor 重置 | 可手工移植 |
| 父 checkpoint namespace | 可手工移植 |
| callback 选择性隔离 | 可手工移植，但必须保留 owner-loop proxy |
| task card model/token usage | 可选 UI 行为；扩展 shared Subagent payload 和前端解析，不需改事件表 |

general-purpose 不应递归调用 task。dev 已在工具装配层禁用，main 的 prompt 禁令只能作为补强，
不能成为权威控制。

## 9. Run / Worker Runtime

### 9.1 main 实际不是独立 Worker

main 的核心入口：

- `main:backend/app/gateway/services.py::start_run`
- `main:backend/packages/harness/deerflow/runtime/runs/manager.py::RunManager`
- `main:backend/packages/harness/deerflow/runtime/runs/worker.py::run_agent`

调用链：

```text
HTTP 请求进入 Gateway
  -> services.start_run()
  -> 构建 graph input/config
  -> RunManager.create_or_reject()
  -> 持久化 pending Run
  -> record.task = asyncio.create_task(...)
  -> 同一个 Gateway 进程调用 run_agent()
  -> graph.astream()
  -> StreamBridge
  -> SSE
```

文件名虽然叫 `worker.py`，但它只是 Gateway 内的协程执行器。

### 9.2 main 的多 Gateway 所有权

- `RunRecord.owner_worker_id`；
- `lease_expires_at`；
- Gateway 自己 heartbeat；
- lease 默认 30 秒，grace 默认 10 秒；
- 续租失败后 fence/cancel 本地 task；
- peer Gateway 扫描过期 Run 并 takeover；
- cancel intent 写入 Run store；
- owner heartbeat 接收 cancel；
- Thread operation reservation 串行化 checkpoint 写入。

主要提交：

- `3bc3af25`：Gateway 多 worker 原子性；
- `b53c1ae0`、`8a78c264`：跨 Gateway cancel；
- `80c06414`、`8af760fc`：lease-aware orphan recovery；
- `090e80c1`：lease 不可确认时 fail-stop；
- `bb9f67aa`：replacement admission；
- `1c753124`、`6f53fd5e`：artifact delivery receipt；
- `e47bf801`、`625c07b9`：regenerate。

### 9.3 当前 dev 的 Worker-only 调用链

核心文件：

- `dev:backend/app/private_work/run_admission.py`
- `dev:backend/app/private_work/run_repository.py`
- `dev:backend/app/worker/service.py`
- `dev:backend/app/worker/app.py`
- `dev:backend/app/reliability/execution.py`

```text
Gateway
  -> PrivateRunAdmissionService.admit()
  -> 锁 project / membership / thread
  -> 固化 Agent/Skill/MCP/Credential snapshot
  -> 同一事务创建 Run + Job
  -> 返回，不执行图

Worker
  -> WorkerService.claim_next()
  -> mark_running()
  -> JobLeaseAuthority heartbeat
  -> PrivateRunJobHandler
  -> RunAgentPrivateExecutor.execute()
  -> materialize exact snapshot
  -> ProjectScopedCheckpointer
  -> LeaseAuthorizedStreamBridge
  -> harness run_agent()
  -> 原子 settlement Run + Job
```

dev 的每个模型、工具、MCP、sandbox、checkpoint、stream 和 file 副作用边界都会重新校验：

```text
project
+ owner
+ membership capability
+ Job lease
+ Run execution lease
```

### 9.4 Run/Worker 可移植性

- main 的 Gateway background task：**已完全替代**。
- main 的 Gateway owner lease：**已完全替代**。
- main 的 orphan takeover：**不适用**。
- main 的 cross-Gateway cancel：**不适用**。
- main `create_or_reject()`：**禁止直接合并**。
- `run_agent()` 内与所有权无关的纯执行修复：**逐项手工审计**。

直接引入 main 的运行管理代码会形成：

- 两套准入；
- 两套 lease；
- 两套 cancel；
- 两个可能执行 Agent 图的进程。

这会违反 dev 的 Worker-only 边界。

## 10. Checkpoint 模块

### 10.1 main 的 full/delta 双模式

核心文件：

- `main:backend/packages/harness/deerflow/runtime/checkpoint_mode.py`
- `main:backend/packages/harness/deerflow/runtime/checkpoint_state.py`
- `main:backend/packages/harness/deerflow/checkpoint_patches.py`
- `main:backend/packages/harness/deerflow/agents/thread_state.py`

主要行为：

- `full|delta` 两种 channel 模式；
- `DeltaThreadState.messages` 使用 LangGraph `DeltaChannel`；
- 非 snapshot checkpoint 只保存增量；
- 每 N 次写入生成一次完整 snapshot，默认 10；
- mode 和 cadence 在进程内冻结；
- metadata 标记 delta；
- full 进程读取 delta checkpoint 时 fail closed；
- 只支持 full 到 delta 的透明迁移；
- `CheckpointStateAccessor` 成为 materialized state 入口；
- middleware state schema 自动适配；
- O(n) message reducer 保留替换、删除和 `REMOVE_ALL_MESSAGES`；
- 修复 InMemorySaver 首次 full-to-delta 增量丢失；
- 修复空 aggregate 首次 `Overwrite` wrapper。

关键提交：

- `42baed8c`：双模式；
- `8c19a2eb`：线性 reducer；
- `d1aeea2c`：Overwrite；
- `244ce773`：delta resume；
- `c48de5e7`：snapshot frequency；
- `713ee544`：移除图片 Base64。

### 10.2 当前 dev 的 scoped checkpoint

核心入口是 `dev:backend/app/private_work/checkpointer.py::ProjectScopedCheckpointer`。

它负责：

- 每个 checkpoint metadata 写入 project 和 owner marker；
- get/list/put 校验 marker；
- pending writes 在已有 checkpoint tuple 时校验 marker；
- 首个 checkpoint 前的 pending writes 尚无 marker 可验，依赖 project、membership 和 Thread 权限锁；
- 校验 Thread 存在性；
- 校验项目成员能力；
- 校验 Worker execution boundary；
- 隐藏 raw checkpointer；
- Thread 删除时先隐藏业务对象，再清理原始 checkpoint；
- branch/edit/regenerate 使用 scoped compare-and-swap；
- Worker retry 使用 checkpoint cursor 决定 resume/takeover。

### 10.3 Checkpoint 可移植性

| main 能力 | 结论 |
| --- | --- |
| 权限和隔离层 | 已由 `ProjectScopedCheckpointer` 替代 |
| DeltaChannel | 产品/性能项目，不能直接开启 |
| 图片 Base64 清理 | 可独立手工移植 |
| Overwrite patch | 先对当前 LangGraph 版本运行行为探针 |
| Gateway checkpoint reservation | 不适用 |
| raw fallback accessor | 禁止引入 |

如果实现 DeltaChannel，必须同时满足：

1. Gateway 和所有 Worker 同时切换；
2. project chat controls 全面适配；
3. branch/edit/regenerate 全面适配；
4. retry takeover 全面适配；
5. 真实 PostgreSQL materialization 测试；
6. 不允许不同进程使用不同 mode/cadence。

Run duration 在 dev 中应优先由 Run 数据库/API 成为业务权威，不应重新依赖 checkpoint metadata。

## 11. Streaming 模块

### 11.1 main 的流模型

```text
run_agent()
  -> MemoryStreamBridge 或 RedisStreamBridge
  -> SSE subscriber
  -> Last-Event-ID replay
```

特征：

- Memory bridge 每 Run 默认保留 256 条；
- Redis bridge 使用 Redis Streams、MAXLEN、TTL 和 XREAD；
- cursor 落在已淘汰窗口外时生成 `StreamGap`；
- Gateway 返回 `event: gap`；
- 前端 reload durable state 后重连；
- live stream 与 durable `RunEventStore` 是两份数据；
- subgraph namespace 编入 SSE event name；
- 大型 `write_file/str_replace` 参数 delta 进行批处理；
- subagent batch 写失败会 re-buffer；
- custom events 同时发给 stream writer 和 callback；
- 前端合并同 tick 更新和 80 ms render window。

### 11.2 当前 dev 的 PostgreSQL durable stream

核心文件：

- `dev:backend/packages/harness/deerflow/runtime/events/stream.py`
- `dev:backend/packages/harness/deerflow/runtime/events/store/db.py`
- `dev:backend/app/reliability/execution.py::LeaseAuthorizedStreamBridge`
- `dev:backend/app/gateway/routers/private_work.py`

```text
Worker run_agent()
  -> LeaseAuthorizedStreamBridge.publish()
  -> PostgresStreamBridge.publish_frame()
  -> DbRunEventStore.append_stream_frame()
  -> PostgreSQL commit
  -> notifier 仅降低延迟

Gateway SSE
  -> Last-Event-ID BIGINT cursor
  -> project + owner + thread + run 查询
  -> terminal 后结束
  -> settled Run 缺 terminal 时执行幂等修复
```

PostgreSQL event row 同时是实时流和 durable replay 权威，因此不存在 main 的 live/durable 双写窗口。

### 11.3 Streaming 可移植性

| main 能力 | 结论 |
| --- | --- |
| Memory/Redis StreamBridge | 已替代 |
| `StreamGap` | 不适用 |
| subgraph namespace | dev 已具备 |
| durable reconnect | dev 已具备且边界更强 |
| 大文件 stream batching | 可手工移植 |
| subagent re-buffer | 可手工移植 |
| root-only LLM fallback 判断 | 已确认差异，应手工修复 |
| 前端 `throttle:true`/coalesce | dev 已有等价实现 |

当前 dev 的多 mode/subgraph 路径会扫描所有 namespace 的 chunk，可能把子 Agent 的 fallback
误判为父 Run error。main 已限定只由空 namespace/root frame 决定父 Run fallback，并有回归测试。

大文件批处理仍必须保留：

- 逐帧 lease 校验；
- 数据库单调顺序；
- project/owner 隔离；
- terminal 幂等性。

## 12. Frontend 模块

### 12.1 main 的前端模型

main 仍以 `/workspace` 为业务壳：

- `/workspace/chats/[thread_id]`
- `/workspace/agents/[agent_name]/chats/[thread_id]`
- 全局 Thread hooks；
- 全局 artifact path；
- Gateway live stream；
- 用户或 Agent/Thread 维度的缓存。

主要入口：

- `main:frontend/src/core/threads/hooks.ts`
- `main:frontend/src/core/messages/`
- `main:frontend/src/core/tasks/`
- `main:frontend/src/components/workspace/messages/`
- `main:frontend/src/components/workspace/chats/`

### 12.2 main 的前端功能增量

#### 流式性能

- 同一 macrotask 的流帧合并；
- 80 ms render window；
- `throttle: true`，避免 SDK 默认行为不一致；
- 流式期间减少 Markdown/code highlighting；
- 不再每个 chunk 重新推导完整消息状态；
- MessageGroup 和 artifact path 使用稳定引用。

关键提交：

- `f090f018`
- `adac3e18`
- `e317f7b8`
- `2839a363`

#### 对话行为

- 每 Thread 草稿；
- 固定聊天；
- 编辑最后一个用户消息并重跑；
- regenerate/branch 状态恢复；
- 长 Run 消息顺序修复；
- completed turn 才允许分支；
- structured clarification forms；
- clarification 期间继续处理普通聊天回复；
- Run duration；
- Subtask model/token card。

#### 文件和 Artifact

- 相对图片路径；
- Artifact path 稳定；
- URL path segment 编码；
- binary Range；
- Markdown 文件名冲突；
- 历史上传延迟加载；
- symlink replacement 分类；
- Artifact/Markdown 安全渲染；
- Diff 中以 `--`/`++` 开头的正文。

#### UI

- 语音输入；
- 面板宽度拖动；
- AI 免责声明；
- reasoning Medium 标签；
- 项目版本展示；
- 导出本地化；
- 非 localhost 本地开发访问。

### 12.3 当前 dev 的前端权威模型

核心入口：

- `dev:frontend/src/app/workspace/page.tsx`
- `dev:frontend/src/app/projects/[project_slug]/layout.tsx`
- `dev:frontend/src/app/projects/[project_slug]/chats/[thread_id]/page.tsx`
- `dev:frontend/src/core/private-work/provider.tsx`
- `dev:frontend/src/core/private-work/api-client.ts`
- `dev:frontend/src/core/threads/hooks.ts`
- `dev:frontend/src/core/threads/api.ts`

调用链：

```text
ProjectContextProvider(project_slug)
  -> 服务端解析 project + capabilities
  -> usePrivateWorkAccess()
  -> createProjectClient(accountId, projectId)
  -> /api/projects/{project_id}/private-work/...
  -> account/project/thread 隔离的 cursor
  -> Last-Event-ID 恢复 PostgreSQL durable stream
```

所有项目私有资源相关的 Query、Mutation、stream cursor 和 client 都必须绑定
account + project；认证、公开资源和系统管理数据不应被错误套入这个约束。

### 12.4 前端可移植性

#### 已具备

- 流帧合并和 coalesce；
- per-thread composer draft；
- 内部 marker 不进入 UI；
- 长 Run 消息顺序；
- workspace change 聚合；
- completed turn 分支 gate；
- 项目私有 artifact/file API；
- project-scoped reconnect。

#### 可手工移植

- edit-and-rerun：
  - main commits `fcbf0609`、`9a43d827`、`919caf7c`；
  - dev 当前只有 regenerate prepare；
  - 应落到 `ProjectChatControlService` 和项目私有 endpoint。
- 语音输入：
  - main commit `be637163`；
  - 项目聊天 composer 尚未接入。
- Run duration：
  - main `e56481d9`；
  - dev 前端已有读取路径，但项目私有后端缺少稳定权威写入。
- structured clarification forms：
  - main `1baa8ad6` 增加 v2 typed form；
  - dev 当前仍是 `free_text`、`single_choice`、`choice_with_other` 三类 v1 结构；
  - 需要同时扩展后端 contract、stream payload、renderer 和提交校验。
- clarification 期间普通回复：
  - main `1bccc8e2` 允许 human-input card 打开时继续普通聊天；
  - dev 当前会禁用普通 composer；
  - 需明确普通回复是进入当前 Run、排队为新 Run，还是继续原 Thread 后再恢复 clarification。
- Artifact/Markdown 的纯渲染测试：
  - 可迁移测试意图；
  - URL 必须继续使用项目文件 UUID API。

#### 已替代或不适用

- `StreamGap` UI；
- 全局最近聊天置顶；
- 旧 `/workspace` 业务路由；
- 旧 Thread/Artifact URL；
- Gateway 内存 browser panel；
- 旧 Lark 全局设置页。

### 12.5 Frontend 风险

不能直接复制 `main:frontend/src/core/threads/hooks.ts`。该文件同时包含：

- 旧 API URL；
- 旧缓存 key；
- 旧 SSE 语义；
- 旧 Thread owner 模型；
- 旧 artifact path。

可迁移单位应是纯函数、独立 UI 组件和针对具体行为的测试。

## 13. Gateway / API 模块

### 13.1 当前 dev 的准入链

核心文件：

- `dev:backend/app/gateway/auth_middleware.py`
- `dev:backend/app/projects/context.py`
- `dev:backend/app/private_work/context.py`
- `dev:backend/app/gateway/routers/private_work.py`
- `dev:backend/app/private_work/http_runtime.py`
- `dev:backend/app/private_work/run_admission.py`
- `dev:backend/app/private_work/run_service.py`
- `dev:backend/app/private_work/chat_controls.py`

```text
AuthMiddleware
  -> request.state.user
  -> resolve_project_context()
  -> ProjectContext.require(capability)
  -> PrivateWorkContext.from_project()
  -> prepare_private_run_config()
  -> 递归丢弃客户端 authority/secret 字段
  -> PrivateRunAdmissionService.admit()
  -> 同事务 Run + Snapshot + Job + Quota + Audit
```

`PrivateWorkContext`：

- 不能由请求字段直接构造；
- 不能复制；
- 不能序列化；
- 记录 issued identity；
- Repository 必须同时过滤 project、owner 和 resource ID。

### 13.2 main Gateway 更新与结论

| 更新 | 结论 | dev 落点 |
| --- | --- | --- |
| create Thread 竞态幂等 `a0acdda1` | 待复现；若存在再移植 | 项目 Thread 事务 |
| 正数 limit/cursor `e89edb39` | 已具备 | Pydantic 范围 |
| `X-Trace-ID` 优先 `0f088033` | 待调用链审计 | TraceMiddleware |
| `configurable=null` `a9a57fb7` | 不适用 | 旧 stateless Run |
| branch seed `70fb9165` | 已替代 | scoped checkpoint clone |
| regenerate state `fbc14638` | 已替代/部分参考 | ProjectChatControlService |
| per-turn seed Run IDs `68797c57` | 已替代 | 项目 Run/Thread journal |
| replay gap `1cd5dea3` | 不适用 | PostgreSQL durable log |
| edit/rerun | 可手工移植 | 项目 private-work API |
| metadata CORS header `c24bf383` | 默认不适用 | 仅 split-origin 决策后考虑 |
| Run duration `e56481d9` | 可手工移植 | Run DB/API |
| concurrent Thread metadata merge `5ce3cecf` | 待并发复现 | 项目 Thread repository |

### 13.3 禁止引入的 Gateway 代码

- 旧 `/api/threads`；
- 旧 `/api/runs`；
- Gateway `RunManager`；
- Gateway graph execution；
- Gateway heartbeat；
- Gateway orphan recovery；
- Memory/Redis StreamBridge；
- 文件型 Agent/MCP 配置写路由。

## 14. Authentication / Authorization

### 14.1 main 的模型

main 增加：

- `AuthorizationProvider` 协议；
- `Principal`；
- built-in RBAC provider；
- 工具装配前过滤；
- 工具调用时再次校验；
- Gateway route permission 派生；
- trusted principal 传播；
- “保持登录”；
- 关闭本地注册；
- 邮箱大小写归一；
- setup-status timeout 恢复。

关键提交：

- `1300c6d3`
- `10890e10`
- `92c8f2f0`
- `7857fa0c`
- `6091ce75`
- `a028dfd5`
- `09e25b8a`
- `b5cc3a81`
- `f881996e`

### 14.2 当前 dev 的权限模型

```text
JWT + PostgreSQL revocable session
  -> system role
  -> Project membership
  -> ProjectRole
  -> server-issued Capability
  -> PrivateWorkContext
  -> 每个 Worker 副作用边界 revalidation
```

平台角色和项目角色相互独立，`system_admin` 不自动获得项目成员权限。

### 14.3 Auth/Authz 可移植性

| main 能力 | 结论 |
| --- | --- |
| `AuthorizationProvider` 替换项目权限 | 禁止 |
| provider 作为 capability 之后的额外 deny hook | 产品决策 |
| trusted Principal | 项目私有权限传递由 issued contexts 覆盖；其他 Principal 用途需另行设计 |
| route permissions | 项目私有路由由 server capabilities 覆盖 |
| 工具双重授权不变量 | admitted private assets、MCP 和 Sandbox 副作用边界已具备 |
| “保持登录”用户选择 | 可手工移植 |
| 关闭本地注册 | 可手工移植，优先 |
| 邮箱大小写归一 | 可手工移植，优先 |
| setup timeout | 基本具备 |

### 14.4 Auth 代码发现

#### AU-1：本地注册缺少部署级关闭开关

main 允许部署关闭 `/api/v1/auth/register`。当前 dev 的本地注册仍是公开入口。
对于只允许邀请或 SSO 的部署，这是值得独立移植的控制。

#### AU-2：邮箱查找和唯一性大小写敏感

当前 dev 的 `SQLUserRepository.get_user_by_email()` 使用精确字符串比较，数据库唯一索引也区分大小写。
应统一 canonical email，并设计数据库唯一性约束，不能只在一个路由中调用 `.lower()`。

## 15. 跨模块安全更新

本节只列安全不变量，不把它们混入 Agent、Skill、Memory 等业务章节。

### 15.1 Prompt 结构转义

main 增加了以下转义：

- Memory fact；
- Memory state；
- Memory summary；
- Memory conversation；
- SOUL；
- Subagent description；
- summarization 输入块；
- MindIE tool response。

相关提交：

- `54f3c43f`
- `938391c1`
- `158c4f96`
- `feb28707`
- `807c3c52`
- `e361122b`
- `c57cf221`
- `8e96a6a2`
- `ae223199`

当前 dev 已有输入和工具结果清理，但不能替代“把不可信正文放进结构标签前做转义”。

### 15.2 Framework tag denylist

main `41b137c4` 扩充了 framework authority tag 的拒绝范围。
当前 dev denylist 较窄，应基于当前所有系统结构标签重新生成闭集，而不是只复制 main 的旧列表。

### 15.3 Remote tool result neutralization

main `5edc7a88` 把 `web_capture` 纳入远程不可信结果处理。
dev 应确保所有远程内容工具使用同一 neutralization boundary。

### 15.4 空 allowlist 语义

main `4fd521e8` 明确：

```text
allowed_tools = []
```

表示 deny all，而不是“未配置”。当前 dev 的确定性缺陷位于
`deerflow/guardrails/builtin.py::AllowlistProvider`：

```python
self._allowed = set(allowed_tools) if allowed_tools else None
```

它会把显式空列表变成未配置并 fail-open。Skill parser/tool policy 的执行语义本身仍能区分
`None` 与空 tuple，不应把这个缺陷泛化为整个 Skill enforcement 都是 fail-open。

另一个独立问题是 `deerflow/skills/describe.py` 会把空 `allowed_tools` 展示成 `(all)`。
应分别修复 Guardrail 执行语义和 Skill 描述文本，并各自增加回归测试。

### 15.5 Secret-looking Run metadata

main `b1984cf4` 拒绝 legacy MCP credential metadata。
旧字段本身在 dev 已不适用，但以下不变量仍成立：

- secret 不能进入 Run metadata；
- secret 不能进入 checkpoint；
- secret 不能进入 stream；
- secret 不能进入日志和 tracing；
- client 提交的 authority/secret-looking key 应递归丢弃。

### 15.6 依赖安全更新

main 的 lockfile 更新包括：

- `soupsieve 2.8.3 -> 2.8.4`；
- `defu 6.1.4 -> 6.1.5`；
- `h3 1.15.5 -> 1.15.6`；
- `cookie-es 1.2.2 -> 1.2.3`；
- `ufo 1.6.3 -> 1.6.4`；
- 后续还有 Next、PostCSS、Pillow、MCP、pyasn1、setuptools 更新。

依赖版本必须对当前 dev lockfile 重新审计，不能把历史 lockfile patch 直接覆盖到 dev。

## 16. MCP 模块

### 16.1 main 的核心文件

- `main:backend/packages/harness/deerflow/mcp/client.py`
- `main:backend/packages/harness/deerflow/mcp/tools.py`
- `main:backend/packages/harness/deerflow/mcp/oauth.py`
- `main:backend/packages/harness/deerflow/mcp/cache.py`

main 在 `dev` 建立后对 MCP 做了三类修复：

1. 工具名在加载时校验；
2. OAuth token 刷新并发控制；
3. 配置 cache 和异常文本处理。

关键提交：

- `79cdd99f`：MCP 工具加载时校验工具名；
- `44990ff1`：OAuth token refresh 改用 `threading.Lock`，避免跨线程 event loop 死锁；
- `07d8b988`：忽略格式错误的 path-like 文本；
- `cdefd4a8`：MCP tools cache 同时按配置内容和路径失效；
- `b963282f`：逐 server fail-soft OAuth priming，并保存轮换后的 refresh token。

### 16.2 工具名校验为什么重要

MCP server 返回的工具名最终会进入：

```text
MCP list_tools
  -> LangChain tool schema
  -> deferred tool catalog
  -> tool_search 搜索结果
  -> 模型可见 prompt
  -> MCP call routing
```

如果名称含空白、控制字符、结构分隔符或不受支持字符，风险不只是在调用时失败。它还可能污染
deferred tool prompt，或者让展示名、路由名和实际 server tool name 产生歧义。

当前 dev 的项目 MCP 调用链更严格：

```text
Project MCPVersion
  -> Run admitted exact MCP snapshot
  -> exact Credential closure
  -> Worker PrivateAgentRuntime
  -> discover exact MCP tools
  -> one-shot invocation
  -> secret-free result assertion
```

权威文件：

- `dev:backend/app/shared_assets/mcp_service.py`
- `dev:backend/app/shared_assets/resolver.py`
- `dev:backend/app/private_work/snapshot_repository.py`
- `dev:backend/app/private_work/asset_runtime.py`
- `dev:backend/packages/harness/deerflow/mcp/tools.py`
- `dev:backend/packages/harness/deerflow/mcp/http_security.py`

`79cdd99f` 属于高价值的边界校验，应在 dev 的两个位置同时实现：

1. MCPVersion 创建或批准时，只校验定义中已知的静态 routing selector 和声明字段；
2. Worker discovery 接收到 server 的 `list_tools` 结果时，校验实际动态工具名。

数据库版本创建时无法提前知道远程 server 实际返回的全部工具名，因此第二层是权威边界，
不能用第一层替代。

### 16.3 当前 dev 已有的 MCP 安全边界

当前 dev 已经具备 main 全局 MCP 配置所没有的项目隔离：

- 项目 MCP 只允许 `http`/`sse`；
- 项目 MCP 定义不接受 literal `env`、`headers` 或 `oauth` secret 配置；
- endpoint 必须通过语法和目标策略校验；
- HTTP client `follow_redirects=False`；
- HTTP client `trust_env=False`；
- MCPVersion 不保存明文 secret；
- Credential 以 slot 绑定精确版本；
- Run 准入锁定精确 MCP 和 Credential 闭包；
- Worker 只在调用边界解密；
- MCP result 返回前执行 secret 泄漏检查；
- MCP 调用前重新校验项目、owner、Run 和 lease authority。

因此，main 的全局文件配置、cache 和 router 不是 dev 项目 MCP 的权威来源。

### 16.4 OAuth lock 的适用范围

当前 dev 的 `OAuthTokenManager` 仍使用 `asyncio.Lock`。main 的 `44990ff1` 修复针对同一个
manager 可能被多个线程或 event loop 复用的情况。

移植时应区分：

- 长生命周期 system MCP manager：可能存在跨线程复用，应重点验证；
- `PrivateAgentRuntime` 内为 catalog/system MCP 创建的 run-local OAuth manager：生命周期更短，
  不能仅凭类名断定存在同一问题；
- 当前项目 MCP 定义禁止 OAuth，因此 main 的 refresh-token 持久化路径不适用于项目 MCP；
- 如果未来允许项目 OAuth，refresh token 必须进入项目 Credential 版本和轮换状态，
  不能写回 `config.yaml`。

结论：目前只能列为兼容性和调用点审计，尚不能认定为 dev 生产路径的已复现 P1 缺陷。
若审计证明某个 manager 会跨 event loop 复用，再移植锁策略；refresh token 的存储路径仍不能复制 main。

### 16.5 MCP 可移植性

| main 能力 | dev 结论 |
| --- | --- |
| 加载时工具名校验 | **P0，可手工移植** |
| 跨线程 OAuth refresh lock | 兼容性/调用点审计，复现后再移植 |
| malformed path-like text 防护 | **P1，可手工移植** |
| 配置文件 cache 内容签名 | 生产 system/project MCP 均由 DB snapshot + one-shot materialization 驱动，通常不适用 |
| OAuth priming | 当前 one-shot catalog/system 路径无直接等价关系，按调用点审计 |
| main 全局 MCP router/cache | **禁止直接合并** |
| main 配置中的明文 OAuth 字段 | 项目 MCP **禁止引入** |

## 17. Sandbox 模块

### 17.1 main 的更新分组

main 的 Sandbox 更新可以分成四组。

#### A. 本地文件和命令正确性

- `04a85b30`：清理缩写形式的 `*_PASS` 和 `PGPASSFILE`；
- `446ae986`、`1df9abc9`、`c82fba41`：路径替换正则增加 segment boundary；
- `08fdf615`：`read_file` 支持单边行区间；
- `97e2268d`：`glob`、`grep`、`ls` 不暴露未启用 Skill 的文件；
- `d2ab5bb8`、`ae510cb2`：统一空 `old_str` 的 `str_replace` 语义；
- `0542d3c5`：cwd 初始化失败后的 shell 行为修复；
- `08fd218b`：Windows reverse-resolve containment 使用平台路径分隔符；
- `d455a181`：允许 `grep` 搜索单个文件；
- `8eb3be59`、`6e6c0785`：正确解包 `Overwrite` 包裹的 Sandbox state。

这组修复多数不依赖 main 的 Gateway 运行模型，适合逐函数审计。

#### B. E2B 生命周期和资源边界

- `da3feb38`：bootstrap 失败时安全退出；
- `d2116d86`：同步大小未变但内容变化的输出；
- `05e4f4f6`：限制输出同步资源；
- `4dd7cafe`：串行化 release transition；
- `3b77a740`：副本容量限制；
- `b22f85c6`：安全 reconcile；
- `5eb59cb1`：多 worker reconcile 不杀死 peer sandbox；
- `495e9083`：`grep` glob 只作用于目录前缀。

这些修复涉及 Sandbox 所有权。dev 的正确主键不是 main 的 Gateway worker，而至少是：

```text
project_id
+ owner_user_id
+ thread_id / run_id
+ Worker Job lease
```

所以 E2B 修复只能提取状态机不变量，不能直接复制 owner/reconcile 查询。

#### C. Provisioner 和新 Provider

- `8cc4b3ab`：Provisioner API endpoint 检查 API key；
- `ac18f518`：Tenki cloud Sandbox provider；
- `2e5c8da2`：本地 AIO 流量绕过代理；
- `5d073991`：扩大 BoxLite/AIO tenant hash，并在 reclaim 时校验身份。

Provisioner API key 是部署边界安全修复。如果部署启用了 Provisioner，必须在暴露端口前验证。
但当前工作树的 `docker/` 是未跟踪恢复内容，本文没有把它当作 dev 的受版本控制实现，也没有修改它。

Tenki 是新产品能力，不是缺陷修复；需要先决定是否进入支持矩阵。

#### D. Lark CLI 和 Credential broker

main 新增：

- Lark CLI；
- Sandbox sidecar Credential broker。

当前 dev 已规定：

- Credential 只按 admitted exact Skill/MCP closure 解密；
- 只在当前调用或激活 Skill 的子进程注入；
- Credential locator、ciphertext 和原始错误不能返回给 Sandbox。

因此 main 的 Lark broker 不能直接并入。若确需 Lark CLI，必须重新设计成
project/owner/Run/SkillVersion scoped 的代理，并接受相同 audit、quota 和 lease 校验。

#### E. Thread data mount override

main `9c7cd4ca` 的 Thread data mount override 是通用 Sandbox/upload-sync 改进，不依赖 Lark broker。
可以单独评估，但映射目标必须来自 dev 私有文件 authority，并在每次 mount/sync 时保持
project、owner、Run、thread 和 Worker lease 范围。

### 17.2 当前 dev 的 Sandbox 权威边界

核心文件：

- `dev:backend/app/private_work/sandbox_files.py`
- `dev:backend/packages/harness/deerflow/sandbox/security.py`
- `dev:backend/packages/harness/deerflow/sandbox/env_policy.py`
- `dev:backend/packages/harness/deerflow/sandbox/tools.py`
- `dev:backend/packages/harness/deerflow/sandbox/middleware.py`
- `dev:backend/packages/harness/deerflow/sandbox/local/local_sandbox.py`
- `dev:backend/packages/harness/deerflow/community/e2b_sandbox/e2b_sandbox.py`

私有文件根按如下范围建立：

```text
projects/{project_id}/users/{owner_user_id}/threads/{thread_id}/user-data
```

移植任何 path、mount、sync 或 reconcile 修复时，都必须证明不会跨：

- project；
- owner；
- thread；
- admitted Run；
- 当前有效 Job lease。

### 17.3 Sandbox 移植顺序

| 优先级 | 内容 | 原因 |
| --- | --- | --- |
| P0（条件） | Provisioner endpoint API key | 启用 Provisioner 时是远程执行边界 |
| P1 | env scrub、路径 boundary、Skill 文件隐藏 | 直接安全与隔离修复 |
| P1 | `read_file`、`grep`、`str_replace` 正确性 | 低耦合，可单测验证 |
| P1 | Thread data mount override | 保持 private file authority 后可独立评估 |
| P1 | E2B output sync 资源上限 | 防止 Worker 资源耗尽 |
| P1 | E2B ownership/reconcile | 必须按 dev lease 模型重写 |
| P2 | Tenki provider | 新能力，需产品和运维决策 |
| 不适用 | main 的 Gateway worker ownership | 已被独立 Worker 替代 |
| 禁止直接合并 | Lark Credential broker | 不满足 dev Credential closure |

## 18. Channels 模块

### 18.1 main 的主要更新

main 对渠道层的更新包括：

- `83803718`：PostgreSQL 跨 Pod inbound webhook 去重；
- `5b65d543`：去重 key 使用 chat-scoped workspace；
- `9a5d7013`：明确 GitHub redelivery TTL；
- `2a7469cd`：GitHub webhook redelivery 去重；
- `51bb19fa`：移除 GitHub review-comment 冗余 fan-out；
- `259f51ca`：GitHub `allow_authors` 大小写不敏感；
- `d2b5f884`：忙碌 Run 时缓冲 GitHub follow-up；
- `b06372b8`：快速同 Thread 消息排队；
- `37580862`：slash Skill whitelist 校验绑定 Run owner；
- `74392e14`：空白 mention-login 不应通过 require-mention gate；
- `7156e745`：bare `connect` 不应触发绑定；
- `b650456c`：不再静默丢弃 stream text delta；
- `0519c8a5`：WeCom quote 空值保护；
- `314f84bc`、`2bb22643`：Feishu SDK 响应成功检查；
- `bc6f1adc`、`b3a0dac8`：群聊 @mention 命令识别；
- `8153e68e`：Slack mrkdwn 保留字符转义；
- `a65eb531`、`62b73fd2`：Telegram、DingTalk inbound 附件；
- `a9a5fc9c`：Telegram final reply 使用 Rich Message；
- `f8bef42a`：WeChat timing 配置校验；
- `9a4c72db`：WeChat poll loop 隔离单条消息失败；
- `b02308bb`：DingTalk 丢弃缺少 conversation identity 的消息。

### 18.2 当前 dev 的渠道调用链

当前 dev 的权威调用链是：

```text
Provider adapter
  -> InboundMessage
  -> ChannelManager
  -> ProjectInboundDispatcher.dispatch()
  -> ConnectionInboundResolver.resolve()
  -> 数据库 ChannelConnection
  -> issued ProjectContext
  -> PrivateWorkContext(project + owner)
  -> PrivateRunAdmissionService
  -> PostgreSQL Run + Snapshot + Job
```

核心文件：

- `dev:backend/app/channels/manager.py`
- `dev:backend/app/channels/message_bus.py`
- `dev:backend/app/private_work/connection_inbound.py`
- `dev:backend/app/private_work/run_admission.py`

当前 `ChannelManager` 已按 provider metadata 构造 inbound dedupe key，但
`_recent_inbound_events` 是进程内 `OrderedDict`。它只能阻止同一个进程内的短期重复，
不能阻止：

- provider 重试命中另一个 Pod；
- Pod 重启后的重复投递；
- 两个 Pod 同时处理同一 event；
- 同一 event 重复创建两个 PostgreSQL Job。

### 18.3 跨 Pod 去重的 dev 适配方案

main 的 `83803718` 行为值得移植，但其 migration 不能使用。dev 不支持增量 migration，
只能修改唯一的 `full_schema.sql`，并在空库初始化测试中验证。

建议去重 key 至少包括：

```text
provider
+ account_id
+ project_id
+ owner_user_id
+ connection_id
+ external_conversation_id
+ external_topic_id（平台存在 topic 且 ID 仅在 topic 内唯一时）
+ provider event/message id
```

数据库操作应与 Run admission 的事务关系明确：

1. 先用唯一键 claim inbound event；
2. claim 成功后才允许创建 Thread/Run/Job；
3. 已完成或已接收的重复 event 返回稳定的幂等结果；
4. 临时失败是否释放 claim，必须按 provider redelivery 语义设计；
5. TTL 清理不能删除仍对应 pending/running Run 的记录；
6. 去重记录不得保存原始消息正文、附件内容或 secret。

不能把 main 的“内存 busy queue”直接搬入 Gateway，因为 dev 的 Gateway 不拥有执行任务。
需要排队时，应表现为可恢复的 PostgreSQL admission/job 状态。

### 18.4 Provider 修复的可移植性

| 更新 | 结论 |
| --- | --- |
| WeCom 空 quote、WeChat poll isolation | 低耦合，可直接比对移植 |
| Feishu success 检查 | 低耦合，可直接比对移植 |
| Slack 字符转义 | 低耦合，但需快照测试 |
| Telegram/DingTalk 附件 | 需走 dev 私有 File + quota + audit |
| mention/command 识别 | 需保留 connection/project/owner 解析 |
| GitHub `allow_authors` 大小写 | 当前 dev 仍是大小写敏感比较，应移植 |
| GitHub redelivery/review fan-out | 与跨 Pod 去重一起验证，不重复创建 Run |
| silent stream delta discard | 审计当前 outbound 聚合；不复制旧 StreamBridge |
| slash owner 校验 | dev 已由 admitted Agent/Skill snapshot 替代旧 whitelist |
| 跨 Pod 去重 | **P1，按 full schema 和事务准入重写** |
| busy follow-up | **P1/P2，必须持久化，不用 Gateway task queue** |

## 19. Tools / Models 模块

### 19.1 模型 Provider 修复

| 提交 | 行为 | dev 结论 |
| --- | --- | --- |
| `e2816eaa` | OpenAI-compatible 规则按 `BaseChatOpenAI` 子类判断 | P1，避免 class-path allowlist 漏掉自定义子类 |
| `3e7baba3` | 所有 `BaseChatOpenAI` 子类应用默认 stream chunk timeout | P1，防止流永久挂起 |
| `94003c1f` | 支持 vLLM cumulative stream usage | P1，关系到 Run token 持久化和项目用量统计 |
| `09d9cf53` | ACP Agent 调用超时 | P1，防止 Worker Job 无限占用 |

当前 dev 的 token usage 不只是 UI 展示，还会进入 `RunRow.total_*_tokens`、
`token_usage_by_model`，项目 24 小时统计再聚合 Run/Job。因此 `94003c1f` 不能只验证消息显示，
还必须验证：

```text
provider chunk
  -> normalized usage
  -> Run settlement
  -> RunRow total tokens + token_usage_by_model
  -> project usage dashboard aggregation
```

当前 quota ledger 没有 token 维度；如果未来增加 token quota，那是新的 schema 和产品设计，
不能写成现有链路。

### 19.2 工具修复

| 提交 | 行为 | dev 适配要求 |
| --- | --- | --- |
| `6456c356` | Browserless 接受 `timeout` key 并严格转换 | 配置解析单测 |
| `d075be02` | `web_fetch_tool` 暴露目标页错误状态 | 仍需 remote-content neutralization |
| `16a77cb7` | Serper 忽略 malformed image URL | 可直接移植 |
| `cd9432bc` | `view_image` 支持 GIF | 与 checkpoint 去 base64 一起验证 |
| `7b330101` | schema 隐藏注入的 runtime 参数 | 防止模型伪造 authority/runtime |
| `756eac0d` | oversized tool output 生成结构化 synopsis | 适配 dev 私有文件、quota、audit 和流预算 |

`list_uploaded_files` 等工具中注入的 runtime 对象不应出现在模型参数 schema 中。这不是 UI 整洁问题，
而是 authority 对象不能被模型构造或覆盖的安全边界。

### 19.3 不建议移植的行为

- main `1ebf59fe` 取消 `tool_search select` 的结果数上限：不建议直接引入。dev 需要稳定的 prompt、
  token 和执行预算。
- Firecrawl SDK 对齐提交随后被 revert，main 最终净行为不应按中间提交移植。
- main Browser Agent 依赖旧 Gateway/内存运行模型，不能作为普通工具直接合并。
- main 历史上传懒加载依赖旧 workspace 文件模型；dev 必须通过私有 FileRepository。

## 20. Config / Build / Deploy 模块

### 20.1 配置版本不能按数字覆盖

当前版本：

- main：`config_version: 31`；
- dev：`config_version: 28`。

这个数字不能用来判断 main 配置一定更新。两个分支的 schema 已发生语义分叉：

main 新增或强化的配置包括：

- `agent_storage`；
- Gateway `run_ownership`；
- channel dedupe；
- checkpoint full/delta；
- browser；
- memory backend；
- authorization provider；
- Sandbox ownership/capacity。

dev 的配置则围绕：

- 单一 `DATABASE_URL`；
- Gateway/Worker/Scheduler 独立进程；
- project capability；
- PostgreSQL Job/stream/checkpoint；
- immutable Agent/Skill/MCP catalog；
- private quota/audit/retention。

不能把 main `config.example.yaml` 覆盖到 dev，也不能把 `config_version` 单独升到 31。
正确做法是对每个候选字段回答：

1. 权威 owner 是 Gateway、Worker 还是 Scheduler；
2. 是否属于项目定义、系统配置或 Run snapshot；
3. 是否允许运行时 reload；
4. 是否影响 checksum/admission；
5. 是否含 secret；
6. 未配置时是 fail-open 还是 fail-closed。

### 20.2 数据库生命周期

main 部分功能通过 Alembic migration 或增量表变更实现。当前 dev 明确禁止这种迁移方式：

```text
空 PostgreSQL
  -> make setup-db
  -> full_schema.sql
  -> full_schema_v1 marker
  -> builtin catalog
  -> LangGraph schema
  -> default project
```

如果候选方案最终需要新增应用表、列或约束，则必须同时更新唯一 `full_schema.sql` 和 ORM，
不能复制 main migration 或新增 Alembic 增量链。例如跨 Pod channel dedupe 大概率需要新表；
但其他功能未必需要应用 schema：

- Agent 设置可以进入现有不可变 asset payload；
- Skill Review 可以先作为有界 artifact；
- Run duration 可以由现有时间戳推导；
- edit-and-rerun 可以继续使用 metadata/checkpoint；
- checkpoint delta 更可能属于 LangGraph/checkpointer 存储层。

任何表变更都必须重跑空库初始化和固定 PostgreSQL gate。

### 20.3 Build 与 Nginx

main 中相对独立、值得核对的更新：

- `4e449385`：pnpm consumer 与 Corepack fallback 对齐；
- `5994fdf3`：避免强制 frontend Nginx upgrade；
- `7757e38b`：允许较大的长 prompt 通过 LangGraph API；
- `d57f6957`：Helm Sandbox Service 默认 `ClusterIP`；
- lockfile 中 Next、PostCSS、Pillow、MCP、pyasn1、setuptools 等更新。

Nginx 调整需要与 dev 实际路由重新对照：

- 浏览器公共入口仍是 `2026`；
- `/api/*` 权威目标是 Gateway；
- WebSocket/HMR 只服务本地开发；
- body size、read timeout 和 buffering 要分别配置；
- 不应恢复旧 `/api/langgraph` 路由后再把请求送入 Gateway 内执行图。

当前 `docker/` 是用户恢复的未跟踪目录，本次分析没有修改，也没有把其中内容当成 dev 已提交事实。

### 20.4 CI 与发布

dev 已规定 `.github/workflows/project-saas-release-gates.yml` 是完整确定性 CI 的唯一编排。
main 新增的单项测试工作流不能直接复制为重复 workflow。

移植行为应进入现有门禁：

- 后端完整 pytest；
- 固定 20 文件 M1-M7 PostgreSQL 0-skip gate；
- 前端 unit；
- deterministic Chromium E2E；
- build 和 security checks。

## 21. 建议优先级

### 21.1 P0：先解决安全边界

| 模块 | 问题 | dev 中的具体风险 | 建议 |
| --- | --- | --- | --- |
| Agent | 四份 Agent 文档未做结构标签转义 | 项目作者正文可闭合系统 prompt 标签 | 对 AGENTS/SOUL/IDENTITY/USER 统一转义 |
| Skill | allowed-tools 不是 active-only | 未激活 Skill 可能缩小或污染整次 Run 的工具集合 | 在 runtime middleware 按实际激活 Skill 决策 |
| Guardrail/Security | `AllowlistProvider` 把空 allowlist 转成 `None` | 显式 deny-all 变成 fail-open | 修复 provider，并单测 `None` 与 `[]` |
| SkillScan | 网络 sink、instance flow、动态 shell kwargs 缺口 | 恶意包可能绕过静态准入 | 合并 main 检测并保留 dev 现有强化 |
| MCP | 动态工具名未在 discovery 边界严格校验 | deferred prompt 和 routing 可被污染 | 创建时和 Worker discovery 双重校验 |
| Sandbox | Provisioner endpoint 未验证 API key（若启用） | 暴露远程执行控制面 | 部署前验证并补集成测试 |

### 21.2 P1：可靠性和一致性

| 模块 | 候选 |
| --- | --- |
| Agent/Subagent | 图片 checkpoint 数据最小化、总 cap、rebuffer、cursor reset、root-only fallback |
| Skill | slash Skill run-once、archive colon/ADS 拒绝 |
| Auth | 可关闭本地注册、canonical email + 大小写不敏感唯一性 |
| Gateway | create-thread race 幂等、`X-Trace-Id` 优先级、稳定 Run duration |
| MCP | OAuth refresh lock 调用点兼容性审计、malformed path-like text 防护 |
| Sandbox | env scrub、路径边界、文件工具、private-scoped mount、E2B 资源和 reconcile |
| Channels | PostgreSQL 跨 Pod inbound 去重、provider 错误处理和附件 |
| Models | subclass timeout、vLLM usage、ACP timeout |
| Tools | runtime 参数隐藏、Browserless/Serper 修复、oversized synopsis |
| Frontend | edit-and-rerun、clarification v2 form、clarification 期间普通回复语义 |

### 21.3 P2：产品能力或大型性能项目

- Agent per-version model/generation settings；
- Skill Review 治理子系统；
- Memory FTS5/BM25、consolidation、remote backend；
- checkpoint DeltaChannel；
- voice dictation；
- Tenki Sandbox；
- Browser Agent；
- task card model/token 展示；
- durable busy-run follow-up queue。

这些项目都需要独立设计，不应和 P0 修复放在一次变更中。

## 22. 明确禁止直接合并的代码

以下不是“当前先不做”，而是由于 authority、数据模型或分支状态不同，不能原样进入 dev：

1. main `AgentStore`、`SqlAgentStore`、旧 `agents` JSON 行表和对应 Alembic migration；
2. main `/api/agents` mutable CRUD，以及 `setup_agent`/`update_agent` 作为运行时可变权威；
3. main 用户目录式 Skill store/reloader，以及 `/api/skills`/`skill_manage` 作为运行时可变权威；
4. main flat request-secret carrier 和直接注入路径；dev 必须保留 exact Skill Credential closure
   与 Sandbox 命令边界解密；
5. main Gateway `RunManager`、`asyncio.create_task(run_agent)` 和 Gateway owner lease；
6. main Memory/Redis `StreamBridge` 替换 PostgreSQL durable stream；
7. main MemoryManager/DeerMem 直接替换 project-owner PostgreSQL Memory；
8. main 全局 MCP 文件配置、cache 和明文 OAuth 写回路径；
9. main `/workspace` 全局 Thread/Run API 和不带 project scope 的前端 hooks；
10. main AuthorizationProvider 直接替换 project membership/capability；
11. main 增量 migration 链；
12. main 整份 `config.example.yaml` 覆盖；lockfile 也不能整份复制，依赖需在 dev 逐项解析并重新锁定；
13. main Lark Sandbox Credential broker；
14. main 基于 Gateway worker 的 Sandbox reconcile/ownership；
15. main in-process channel busy queue；
16. main Browser Agent 的 Gateway 内存 session 模型。

## 23. 移植验证矩阵

### 23.1 Agent

最低验证：

- `backend/tests/test_lead_agent_prompt.py`
- `backend/tests/test_create_deerflow_agent.py`
- `backend/tests/test_private_asset_runtime.py`
- `backend/tests/test_view_image_tool.py`
- 新增四类 Agent 文档闭合标签攻击用例；
- 新增 checkpoint 中不存在 image base64 的断言。

### 23.2 Skill

最低验证：

- `backend/tests/test_skill_permissions.py`
- `backend/tests/test_lead_agent_skills.py`
- `backend/tests/test_skillscan_native.py`
- `backend/tests/test_skill_archive_package.py`
- `backend/tests/test_private_skill_runtime_layout.py`
- 新增 passive/active Skill 工具可见性矩阵；
- 新增 `AllowlistProvider` 空 allowlist deny-all；
- 单独验证 Skill 空 tuple policy 与 describe 展示；
- 新增网络 instance/dataflow 和动态 shell kwargs 检测。

### 23.3 MCP

最低验证：

- `backend/tests/test_mcp_definition_policy.py`
- `backend/tests/test_mcp_runtime_composition.py`
- `backend/tests/test_private_asset_runtime.py`
- `backend/tests/test_mcp_secure_http_client.py`
- `backend/tests/test_mcp_oauth.py`
- `backend/tests/integration/test_m3_mcp_credentials_postgres.py`
- 新增非法动态工具名；
- 仅当调用点审计复现跨 event-loop OAuth manager 时，新增对应并发 refresh 回归；
- 断言错误、stream、audit 中不存在 Credential 明文。

### 23.4 Subagent / Worker / Streaming / Checkpoint

最低验证：

- `backend/tests/test_subagent_executor.py`
- `backend/tests/test_subagent_limit_middleware.py`
- `backend/tests/test_subagent_checkpointer_isolation.py`
- `backend/tests/test_worker_subagent_persistence.py`
- `backend/tests/test_m6_private_run_admission_postgres.py`
- `backend/tests/test_m6_private_run_worker_postgres.py`
- `backend/tests/test_m6_durable_stream_postgres.py`
- `backend/tests/test_m6_worker_crash_recovery_postgres.py`
- `backend/tests/test_project_scoped_checkpointer.py`
- `backend/tests/test_worker_subgraph_streaming.py`
- marked fallback 与 recursion 路径分类；
- compaction 后 cursor reset；
- 父 checkpoint namespace 保留；
- callback 隔离不破坏 owner-loop MCP/Auth/Credential proxy；
- flush 失败 event re-buffer；
- 子 Agent fallback 不得污染父 Run root fallback；
- 验证 Gateway 永远不执行 Agent graph；
- 验证失去 lease 后任何 stream/checkpoint/file 副作用都被 fence。

### 23.5 Channels

最低验证：

- `backend/tests/test_m7_project_channel_authority.py`
- `backend/tests/test_channel_runtime_identity.py`
- `backend/tests/test_channel_file_attachments.py`
- 各 provider adapter 单测；
- 新增两个 Gateway/Pod 并发接收同一 event 只准入一个 Job 的 PostgreSQL 测试；
- 新增相同 message id 在不同 external conversation/topic 中不互相去重的测试；
- 新增跨 account/project/owner 相同 provider event id 不互相去重的测试。

### 23.6 Frontend

最低验证：

- project-scoped query key 和 cache isolation；
- `frontend/tests/unit/core/threads/run-messages.test.ts`
- `frontend/tests/unit/core/messages/run-duration.test.ts`
- `frontend/tests/unit/core/private-work/m6-private-stream-reconnect.test.ts`
- `frontend/tests/e2e-real-backend/multi-run-order.spec.ts`
- edit-and-rerun 必须覆盖 manual rename、settled checkpoint、project route 和 owner 隔离。

### 23.7 Schema 与发布

数据库变更后至少需要：

```bash
make setup-db
make check-db
POSTGRES_TEST_URL=... make test-project-foundation-postgres
```

`test-project-foundation-postgres` 是固定的 20 文件 M1-M7 0-skip 门禁。数据库命令需要专用
空库或测试数据库，不能对业务库运行。

## 24. 推荐拆分方式

不要创建一个“同步 main”大分支。建议按独立变更集拆分：

1. `security/prompt-boundaries`
   - Agent 文档转义；
   - framework tags；
   - empty allowlist；
   - remote tool neutralization。
2. `security/skill-runtime`
   - active-only policy；
   - SkillScan；
   - archive parser；
   - slash run-once。
3. `security/mcp-runtime`
   - tool name；
   - OAuth 跨 event-loop 调用点审计，复现后再决定 lock；
   - error/path sanitization。
4. `reliability/subagent-stream`
   - total cap；
   - rebuffer；
   - cursor reset；
   - image checkpoint cleanup。
5. `reliability/channel-dedupe`
   - full schema；
   - repository；
   - admission transaction；
   - multi-Pod PostgreSQL tests。
6. `correctness/sandbox-tools`
   - 低耦合文件工具修复。
7. `correctness/model-providers`
   - timeout；
   - cumulative usage；
   - Run token 持久化和项目用量统计测试。
8. `feature/edit-rerun`
   - Gateway contract；
   - project-scoped frontend；
   - E2E。

每个变更集都应只触碰一个权威边界，避免把 schema、运行所有权和前端功能混在一起。

## 25. 关键结论

1. `main` 的 342 个独有提交包含大量有价值的安全、可靠性和 UX 修复，但它们不是一个可合并的
   release train。
2. 当前 dev 的 project/owner、transactional admission、Worker-only、PostgreSQL durable stream
   和 immutable asset snapshot 是更高优先级的不变量。
3. Agent 与 Skill 必须分别迁移：
   - Agent 重点是 prompt 文档边界、图像 checkpoint、model setting 数据模型和 delegation cap；
   - Skill 重点是 active-only tool policy、SkillScan、slash run-once 和 archive；
   - Skill Review 应作为独立治理能力评估。
4. 最先处理的不是新功能，而是 prompt 结构转义、deny-all 语义、Skill active-only policy、
   SkillScan、MCP tool name 和条件性的 Provisioner 鉴权。
5. Memory、Delta checkpoint、Browser Agent、Tenki、voice 等应作为独立产品或性能项目评估。
6. 任何移植都必须重新进入 dev 当前测试矩阵；main 的历史通过记录不能证明 dev 兼容。

## 附录 A：复现本次分支统计

```bash
git merge-base dev main
git rev-list --count 3be3969f..main
git rev-list --count 3be3969f..dev
git diff --shortstat 3be3969f..main
git diff --name-status 3be3969f..main
git diff --shortstat dev..main
git log --date=iso --reverse --format='%h %ad %s' 3be3969f..dev
```

查看某模块的 main 历史：

```bash
git log --oneline 3be3969f..main -- backend/packages/harness/deerflow/agents
git log --oneline 3be3969f..main -- backend/packages/harness/deerflow/skills
git log --oneline 3be3969f..main -- backend/packages/harness/deerflow/mcp
git log --oneline 3be3969f..main -- backend/packages/harness/deerflow/sandbox
git log --oneline 3be3969f..main -- backend/app/channels
```

查看 main 文件而不切换分支：

```bash
git show main:path/to/file
git diff 3be3969f..main -- path/to/module
git diff dev..main -- path/to/module
```
