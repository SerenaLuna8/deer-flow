# 01. Agent 模块：`main` 最终实现、提交演进与 `dev` 精确落点

## 1. 分析范围与结论

本文只分析 Agent，不把 Skill、MCP、授权和 Sandbox 的内部实现混入本章。对比基线为：

- 公共祖先：`3be3969f8fc3f2d2b6d36ef5c26fa5593d916f2a`
- `main`：`e317f7b8`
- `dev`：`8a91e957`

结论先行：

1. `main` 在公共祖先之后把“自定义 Agent”做成了可切换文件/SQL 后端的可变用户资源，并补齐了每 Agent 模型参数、配置式中间件、总委派上限、图片 checkpoint 瘦身等能力。
2. `dev` 已经把 Agent 改造成 PostgreSQL 中的 system/project 共享资产：版本不可变、发布有状态机、Agent 精确引用 Skill/MCP 版本，Run 准入时固化整个闭包，Worker 是唯一执行者。
3. 两条实现的“Agent”只是产品概念同名，持久化模型和一致性语义完全不同。`main` 的 `AgentStore`、`agent_storage.backend`、旧 `/api/agents` CRUD、Alembic `0006` 不能合入 `dev`。
4. 可移植价值最高的不是旧存储，而是局部运行时修复：模型参数合并规则、结构化 prompt 转义、图片 checkpoint 瘦身与临时消息清理、每 Run 总委派上限、标题长度和 `web_fetch` 错误页识别。
5. `dev` 当前存在两个已由代码对比确认的回归：四份 Agent 文档直接插入 XML 风格 system block，且图片仍把 base64 持久化到 graph state。二者都应在 `dev` 的精确版本/远程 Sandbox 语义下重写后落地。

## 2. `main` 源码地图

| 层次 | `main` 文件 | 责任 |
| --- | --- | --- |
| 配置模型 | `backend/packages/harness/deerflow/config/agents_config.py` | `AgentConfig`、`AgentModelSettings`、名称校验、Store 分发 |
| 存储开关 | `backend/packages/harness/deerflow/config/agent_storage_config.py` | `AgentStorageConfig.backend: file | db` |
| 存储协议 | `backend/packages/harness/deerflow/persistence/agents/base.py` | 同步 `AgentStore`、删除结果、配置解析 |
| 文件实现 | `backend/packages/harness/deerflow/persistence/agents/file.py` | per-user `config.yaml` / `SOUL.md`、legacy 只读回退 |
| SQL 实现 | `backend/packages/harness/deerflow/persistence/agents/model.py`、`sql.py` | 旧 `agents` 表、同步 Engine、事务更新 |
| 工厂 | `backend/packages/harness/deerflow/persistence/agents/__init__.py` | `get_agent_store()`、`make_agent_store()` |
| 迁移 | `backend/packages/harness/deerflow/persistence/migrations/versions/0006_agents.py` | 创建旧版 `agents` 表 |
| 导入脚本 | `backend/scripts/migrate_agents_to_db.py` | 文件定义显式导入 SQL |
| Gateway API | `backend/app/gateway/routers/agents.py` | `/api/agents` CRUD、模型配置局部更新 |
| Agent 构造 | `backend/packages/harness/deerflow/agents/lead_agent/agent.py` | 解析 Agent、模型、工具、Skill、中间件并建图 |
| 模型工厂 | `backend/packages/harness/deerflow/models/factory.py` | profile 与 `model_overrides` 合并 |
| prompt | `backend/packages/harness/deerflow/agents/lead_agent/prompt.py` | SOUL、Skill、Subagent 等系统段落 |
| 扩展中间件 | `backend/packages/harness/deerflow/agents/middlewares/configured_extensions.py` | 从类路径加载零参数 `AgentMiddleware` |
| 委派限制 | `backend/packages/harness/deerflow/agents/middlewares/subagent_limit_middleware.py` | 单批并发上限和每 Run 总上限 |
| 图片状态 | `backend/packages/harness/deerflow/agents/thread_state.py`、`view_image_middleware.py`、`tools/builtins/view_image_tool.py` | 仅持久化图片元数据，按需读取，调用后移除临时消息 |
| 结果语义 | `backend/packages/harness/deerflow/agents/middlewares/tool_result_meta.py` | 识别 `web_fetch` HTTP 错误页 |

## 3. `main` 核心类型、签名与调用链

### 3.1 Agent 配置

`AgentModelSettings` 是一个显式 allowlist：

```text
temperature: float | None       # 0.0..2.0
max_tokens: int | None          # 1..200000
model_config.extra = "forbid"
```

`AgentConfig` 的关键字段是：

```text
name: str
description: str
model: str | None
tool_groups: list[str] | None
skills: list[str] | None
model_settings: AgentModelSettings | None
thinking_enabled: bool | None
reasoning_effort: Literal["low", "medium", "high"] | None
github: GitHubAgentConfig | None
```

这里有三层不同语义：

- `model` 选择 `config.yaml` 中的模型 profile；
- `model_settings` 只覆盖允许的采样参数；
- `thinking_enabled`、`reasoning_effort` 是 DeerFlow 运行开关，不混入任意 provider 参数。

`MANAGED_AGENT_CONFIG_FIELDS` 与 `preserve_non_managed_fields(existing_cfg)` 保证 Gateway 或 Agent 工具更新已知字段时，不会静默删除手写的 `github` 等字段。

### 3.2 `AgentStore` 协议

`AgentStore` 是同步接口，精确方法为：

```text
get(name, *, user_id=None) -> AgentConfig
exists(name, *, user_id=None) -> bool
get_soul(name, *, user_id=None) -> str | None
list(*, user_id=None) -> list[AgentConfig]
list_all() -> list[tuple[user_id, AgentConfig]]
create(name, config, soul, *, user_id=None) -> None
update(name, config | None, soul | None, *, user_id=None) -> None
delete(name, *, user_id=None) -> AgentDeleteOutcome
signature() -> Hashable
```

`AgentDeleteOutcome` 区分：

- `deleted`
- `legacy`
- `missing`
- `not-custom-agent`

接口刻意保持同步，因为 graph factory、`setup_agent`、`update_agent` 和 GitHub registry 都有同步调用点；FastAPI 路由通过 `asyncio.to_thread` 隔离文件/数据库阻塞。

### 3.3 Store 选择链

```text
load_agent_config / load_agent_soul / list_custom_agents
  -> get_agent_store()
  -> get_app_config()
  -> make_agent_store(config)
     -> agent_storage.backend == "file" -> FileAgentStore singleton
     -> agent_storage.backend == "db"   -> SqlAgentStore(app_sync_sqlalchemy_url)
```

`db` 只接受 `database.backend` 为 `sqlite` 或 `postgres`；`memory` 被拒绝，因为进程内数据库不能实现多节点共享。

### 3.4 Gateway CRUD 链

`backend/app/gateway/routers/agents.py` 暴露：

```text
GET    /api/agents
GET    /api/agents/check?name=...
GET    /api/agents/{name}
POST   /api/agents
PUT    /api/agents/{name}
DELETE /api/agents/{name}
```

所有路由先经过 `_require_agents_api_enabled()`，即 `agents_api.enabled` 是显式暴露开关。名称经 `^[A-Za-z0-9-]+$` 校验并转小写；模型名经 `_validate_model_exists()` 检查。

更新路径通过 `request.model_fields_set` 区分：

- 字段未出现：保留原值；
- 显式 `null`：清除该字段；
- `model_settings` 中只出现 `temperature`：`_merge_model_settings_update()` 保留原 `max_tokens`。

这比简单 `model_dump(exclude_none=True)` 更准确，因为 `skills=None` 表示继承全部 Skill，而不是“无变化”。

### 3.5 模型解析链

`make_lead_agent(config)` 中的优先级是：

```text
请求 runtime config
  > AgentConfig 中的默认值
  > 应用/模型 profile 默认值
```

关键调用为：

```text
agent_config = load_agent_config(agent_name, user_id=resolved_user_id)
model_name = request model > agent_config.model > global default
thinking_enabled = request > agent_config.thinking_enabled > True
reasoning_effort = request > agent_config.reasoning_effort > None
agent_model_overrides = agent_config.model_settings.model_dump(exclude_none=True)
create_chat_model(
    name=model_name,
    thinking_enabled=thinking_enabled,
    reasoning_effort=reasoning_effort,
    model_overrides=agent_model_overrides,
    app_config=resolved_app_config,
    attach_tracing=False,
)
```

`_resolve_runtime_option()` 用“键是否存在”而不是真值判断，所以显式 `thinking_enabled=false` 不会错误回退到 Agent 默认值。

`create_chat_model(..., model_overrides=None, **kwargs)` 先把非空 override 合并进模型 profile，再执行 provider 特定的 thinking/reasoning 变换。因此 `max_tokens` 不会在后续适配中被意外绕开。

### 3.6 中间件装配

`load_configured_extension_middlewares(app_config)` 对每个
`extensions.middlewares` 条目执行：

```text
resolve_class("module.path:ClassName", AgentMiddleware)
  -> 验证类存在且为 AgentMiddleware 子类
  -> 零参数构造
```

`build_middlewares()` 把这些扩展放在普通运行中间件之后、终止/安全/澄清中间件之前。这个位置使扩展能参与模型和工具调用，但不能越过末端安全处理。

### 3.7 每 Run 委派总量

`SubagentLimitMiddleware(max_concurrent, max_total)` 同时约束：

- 单个 AIMessage 内 `task` 调用数；
- 当前 `run_id` 在 durable delegation ledger 中的历史委派数。

调用链：

```text
after_model(state, runtime)
  -> _runtime_run_id(runtime)
  -> _count_prior_delegations(state["delegations"], run_id)
  -> remaining_total = max_total - prior
  -> allowed = min(max_concurrent, remaining_total)
  -> clone_ai_message_with_tool_calls(...)
```

达到总上限时，会：

- 删除多余 `task` tool calls；
- 在消息正文追加停止提示；
- 设置 `runtime.context["stop_reason"] = "subagent_limit_capped"`。

若缺少 `run_id`，代码退化为统计整个 thread 的历史委派并记录 warning；这是保守但可能误限流的降级。

## 4. `main` 数据与状态生命周期

### 4.1 文件后端

当前布局：

```text
{base}/users/{user_id}/agents/{name}/
  config.yaml
  SOUL.md
```

兼容读取：

```text
{base}/agents/{name}/
```

`FileAgentStore` 的行为：

1. 读时优先 per-user，并要求 `config.yaml` 存在；
2. legacy 目录只读，更新时返回冲突提示；
3. 单文件写使用同目录临时文件再 `Path.replace()`；
4. config 与 soul 两文件更新不是一个原子事务；
5. 删除 bare memory 目录时返回 `not-custom-agent`，避免误删用户记忆；
6. `signature()` 基于发现到的文件 mtime 形成缓存 token。

### 4.2 SQL 后端

`main` 的旧 `agents` 表是：

```text
id: String(64) primary key
user_id: String(64)
name: String(128), lowercase
config: JSON
soul: Text
created_at / updated_at
UNIQUE(user_id, name)
```

`SqlAgentStore`：

- 每 URL 复用同步 Engine，`_engines_lock` 防止重复构建；
- SQLite 连接启用 WAL、`synchronous=NORMAL`、FK 和 30 秒 busy timeout；
- `create()` 由唯一约束把检查后写入竞态转成 `AgentExistsError`；
- `update()` 是真正 upsert：并发插入 loser 回滚、重读 winner、再应用更新；
- config 与 soul 在同一事务内更新；
- `delete()` 删除行后才清理同目录 memory；没有行时保留 bare memory；
- `signature()` 返回 `(max(updated_at), count)`。

`backend/scripts/migrate_agents_to_db.py` 是显式、幂等、非破坏的文件到 DB 导入；它不是运行时自动迁移。

### 4.3 图片状态

`713ee544` 和 `ce4a6d4c` 把生命周期改为：

```text
view_image_tool
  -> 校验虚拟根、真实路径、文件类型、magic、20 MiB、stat/read 一致性
  -> checkpoint 仅写 {actual_path, mime_type, size}
ViewImageMiddleware.before_model
  -> 再次校验文件仍存在、size 未变化、仍 <= 20 MiB
  -> 按需读文件并临时编码 data URL
  -> 注入带唯一 ID 和内部 marker 的隐藏 HumanMessage
ViewImageMiddleware.after_model
  -> RemoveMessage 删除临时图片上下文
```

因此 base64 既不进入 `viewed_images`，也不会长期留在 `messages` checkpoint。

## 5. 配置、并发、错误与安全边界

### 5.1 配置边界

- `agent_storage.backend` 只决定旧自定义 Agent 定义存储，不决定 thread/run 持久化；
- `agents_api.enabled` 控制是否暴露管理 API；
- `extensions.middlewares` 是运维代码执行入口，只应由可信配置控制；
- Agent 的 `model_settings` 是固定字段 allowlist，不能借此向 provider 注入任意参数。

### 5.2 并发边界

- 文件单项替换原子，但 config+soul 组合不原子；
- SQL create/update 由数据库唯一约束和事务收口；
- HTTP 文件/同步 DB IO 统一 `asyncio.to_thread`；
- 图片读取/编码的 async 路径也放到线程；
- 委派上限依赖 durable ledger 与准确 `run_id`。

### 5.3 错误边界

- 未配置模型在写入 API 被 422 拒绝；无法加载 app config 时该检查 fail-soft；
- Store 未找到保持 `FileNotFoundError`，路由映射 404；
- legacy-only 文件 Agent 更新映射 409；
- `web_fetch` 错误页通过首个非空标题精确分类，不做正文子串误判；
- 502/503/504 归类 transient，500/501 归类 internal。

### 5.4 prompt 安全

`807c3c52` 在 `main` 的 `<soul>` 渲染处对 `SOUL.md` 做
`html.escape(..., quote=False)`，防止内容关闭 `<soul>` 后伪造框架标签。

同类修复还覆盖了 Skill 元数据和 Subagent description。原则是：

```text
可信结构标签由框架产生
不可信/可编辑文本只作为标签内数据，进入前结构转义
```

这不是“禁止 Agent 指令”，而是防止 Agent 可编辑内容改写框架分隔结构。

## 6. `main` 测试与契约

主要测试：

| 测试 | 覆盖 |
| --- | --- |
| `backend/tests/test_agent_model_settings.py` | Pydantic 范围、额外字段拒绝 |
| `backend/tests/test_agents_router_model_settings.py` | omitted/null、嵌套局部更新、模型校验 |
| `backend/tests/test_agent_storage_backend.py` | file/db 选择与配置约束 |
| `backend/tests/test_agent_store_sql.py` | owner 隔离、upsert、删除、signature |
| `backend/tests/test_lead_agent_model_resolution.py` | 请求/Agent/global 优先级与显式 false |
| `backend/tests/test_custom_agent.py` | Agent 解析、工具/Skill选择 |
| `backend/tests/test_create_deerflow_agent.py` | Agent graph 装配 |
| `backend/tests/test_setup_agent_tool.py` | 首次设置、合法性、保存 |
| `backend/tests/test_update_agent_tool.py` | 更新、legacy 防护、空 soul 拒绝 |
| `backend/tests/test_view_image_tool.py`、中间件测试 | 图片格式、大小、状态注入 |
| `backend/tests/test_web_fetch_error_shell_meta.py` | 真实 producer 形状、错误页正反例 |

外部契约主要由 `AgentResponse`、`AgentCreateRequest`、`AgentUpdateRequest` 和
`AgentStore` 抽象构成；`main` 没有 `dev` 那种 Run 级资产快照契约。

## 7. `3be3969f..main` 提交演进

| 日期 | 提交 | 影响 |
| --- | --- | --- |
| 2026-07-11 | `ca18cf0b` | fallback title 为 `...` 预留字符，严格满足 `max_chars` |
| 2026-07-13 | `4e209827` | 增加每 Run 总委派上限 |
| 2026-07-13 | `807c3c52` | SOUL 进入 `<soul>` 前转义 |
| 2026-07-13 | `a94ea932` | 兼容只存在 SOUL、没有 config 的旧目录 |
| 2026-07-14 | `713ee544` | checkpoint 不再持久化图片 base64 |
| 2026-07-14 | `0dd90ccf` | legacy guard 必须检查 `config.yaml`，不把 memory-only 目录当 Agent |
| 2026-07-16 | `7df44f58` | `update_agent` 拒绝空 SOUL |
| 2026-07-20 | `1a1c5def` | `web_fetch` HTTP 错误页不再算成功证据 |
| 2026-07-21 | `ca16b64b` | 支持配置声明的 lead middleware |
| 2026-07-22 | `ce4a6d4c` | 模型调用后移除临时图片 HumanMessage |
| 2026-07-22 | `20debf9c` | per-Agent 模型 profile、采样和 thinking/reasoning 配置 |
| 2026-07-23 | `0d4d0cb1` | Agent 定义增加 SQL Store 和导入脚本 |

这些提交的后半段已经形成一套完整的“可变用户 Agent”产品线；不能只摘 SQL 表而忽略 API、Store 和文件兼容语义。

## 8. `dev` 对应实现

`dev` 的主实现不再是 `AgentStore`，而是共享资产：

| 层次 | `dev` 路径/符号 |
| --- | --- |
| ORM | `backend/packages/harness/deerflow/persistence/shared_assets/agent_model.py`：`AgentRow`、`AgentVersionRow`、`AgentVersionSkillRefRow`、`AgentVersionMcpRefRow` |
| Repository | `backend/app/shared_assets/agent_repository.py`：`AgentRepository` |
| Service | `backend/app/shared_assets/agent_service.py`：`AgentService`、`AgentPayload`、`AgentInstructions` |
| Project API | `backend/app/gateway/routers/project_assets.py`：`register_asset_routes()` |
| Admin API | `backend/app/gateway/routers/admin_assets.py` |
| 准入解析 | `backend/app/shared_assets/resolver.py`：`ProjectAssetResolver` |
| Run 快照 | `backend/app/private_work/snapshot_repository.py`：`RunSnapshotRepository` |
| 运行物化 | `backend/app/private_work/asset_runtime.py`：`PrivateAssetRuntime`、`PrivateAgentRuntime`、`PrivateAgentManifest` |
| prompt | `backend/packages/harness/deerflow/agents/lead_agent/prompt.py`：`AgentPromptBundle`、`render_agent_prompt_bundle()` |
| 兼容系统目录 | `backend/packages/harness/deerflow/config/agents_config.py`：`load_agent_config()`、`load_agent_soul()` 只读 PostgreSQL system catalog |

### 8.1 `dev` 数据模型

`agents` 是资产头：

```text
UUID id
scope: system | project
project_id
slug / display_name
status: active | archived | suspended
current_published_version_id
optimistic version
source_key
created_by_user_id
```

`agent_versions` 是不可变 payload：

```text
UUID id / agent_id / version_number
workflow_status: draft | pending_approval | published | rejected
description
payload_schema_version: 1 | 2 | 3
AGENTS.md / SOUL.md / IDENTITY.md / USER.md
model_ref
model_settings             # v3，严格 allowlist，并进入 checksum
tool_groups
payload_checksum
supersedes_version_id
review metadata
```

两个引用表精确固定 Skill/MCP version UUID 和顺序。数据库 trigger 阻止已发布 payload 和 child refs 被原地修改。

### 8.2 `dev` 生命周期

```text
AgentService.create_version / update_instructions
  -> 校验文档大小、model_ref、tool_groups、Skill/MCP 精确版本闭包
  -> 写不可变 AgentVersion + refs + checksum
AgentService.publish
  -> 状态机与 optimistic asset version
Run admission
  -> ProjectAssetResolver 锁定 exact Agent/Skill/MCP/Credential closure
  -> RunSnapshotRepository 持久化 IDs、scope、checksum、catalog_generation、grant snapshot
Worker claim job
  -> 再校验 project/owner/capability/lease
  -> PrivateAssetRuntime.materialize()
  -> PrivateAgentRuntime + run-owned Skill tree + MCP proxies
  -> lead Agent graph 执行
```

Gateway 不执行 graph，Agent 运行时也不能自我修改资产。

## 9. `main` 与 `dev` 逐项差异

| 维度 | `main` | `dev` |
| --- | --- | --- |
| 资源归属 | `user_id + name` | system 或 `project_id + asset_id` |
| 修改模型 | 原地更新 config/soul | 新建不可变 version，再发布 |
| SQL 表 | 单表 JSON 文档 | 资产头 + version + Skill/MCP refs |
| 主键 | 字符串 UUID + 自然键 | UUID + scope/project 复合约束 |
| API | `/api/agents` | `/api/projects/{project_id}/agents...`、Admin assets |
| 执行位置 | Gateway/graph/TUI 均可能 | Worker-only |
| 执行选择 | 运行时按名称读取最新 | Run 准入固化精确 version/checksum |
| Skill/MCP 依赖 | 名称列表 | version UUID 闭包 |
| 模型微调 | 有 `model_settings`、thinking/reasoning | `model_ref` + v3 严格 `model_settings`，随精确版本快照执行 |
| prompt | 单 SOUL，`main` 已转义 | v2 四文档，当前未做结构转义 |
| 图片 | 元数据 checkpoint + 临时 base64 | 当前仍在 state 保存 base64 |
| 委派限制 | 并发 + 每 Run 总量 | 目前只保留并发限制 |
| schema 生命周期 | Alembic 0006 | 唯一 `full_schema.sql`，拒绝增量迁移 |

## 10. 移植前已确认缺陷与风险（本轮状态见第 14 节）

### A-1：`dev` Agent 文档可关闭框架标签

位置：

- `backend/packages/harness/deerflow/agents/lead_agent/prompt.py`
- `render_agent_prompt_bundle(bundle)`

当前 v1 直接插入 `bundle.soul`，v2 直接插入四个文档：

```text
<agent_profile_document name="SOUL.md">
{content}
</agent_profile_document>
```

内容若包含 `</agent_profile_document><system-reminder>...`，会改变框架构造的结构。文档本来就是 project-configurable system instruction，但它不应能伪造平台自己的 sibling block。应在每个文档正文位置执行 `html.escape(content, quote=False)`；属性名是固定常量，不需来自输入。

### A-2：`dev` 图片 base64 再次进入持久 state

当前 `ViewedImageData` 和 `ViewImageMiddleware` 仍携带 base64，缺少 `main` 的两阶段修复。大型图片会被重复写入 checkpoint，并可能在临时隐藏 HumanMessage 中继续保留。

直接照搬 `actual_path` 也不完全正确：`dev` 支持远程/容器 Sandbox，Worker 进程未必能重新打开宿主路径。应保存“可重新授权读取的 opaque file reference”，在每次模型注入前通过当前 Run/owner/Sandbox authority 重新读取。

### A-3：`dev` 每 Run 委派总量回退

`dev` 的 `SubagentLimitMiddleware` 只限制一个模型响应中的并发 `task` 数。模型可以多轮各发合法批次，无硬总量。应利用 `dev` 已有 durable delegation ledger 和 Run ID，移植 `main` 的总量算法，并把停止原因纳入 Run 终止/审计契约。

### A-4：`dev` fallback title 可超 `max_chars`

`_fallback_title()` 先取 `max_chars` 个字符，再追加 `...`。`max_chars=10` 可得到 13 字符。`main` 的 `ca18cf0b` 已有精确修复。

### A-5：`dev` 丢失 `web_fetch` 错误页分类

`dev` 的 `tool_result_meta.py` 没有 `_classify_error_shell()`。传输成功但页面标题为 404/503 的结果会被记作成功证据。该算法与资产模型无关，可连同真实 producer 回归测试移植。

### A-6：配置式中间件不可直接开放给项目作者

`main` 的 `extensions.middlewares` 会导入任意 Python 类并构造。若在 `dev` 变成普通项目字段，就等价于跨租户代码执行。它只能保留为受信任运维配置，并应有 allowlist、启动时校验和重启生效语义。

## 11. 可移植落点

### P0：Agent prompt 结构转义

修改：

- `backend/packages/harness/deerflow/agents/lead_agent/prompt.py`
  - `render_agent_prompt_bundle()`
  - v1 SOUL wrapper
  - v2 四文档正文
- 新建 `backend/tests/test_agent_prompt_bundle.py`，或扩展现有 exact-agent prompt 测试

测试必须覆盖四个文档各自的闭标签 breakout、正常 Markdown 字节保持、v1 兼容。

### P0：图片 checkpoint 瘦身

修改：

- `backend/packages/harness/deerflow/agents/thread_state.py`
- `backend/packages/harness/deerflow/tools/builtins/view_image_tool.py`
- `backend/packages/harness/deerflow/agents/middlewares/view_image_middleware.py`
- `backend/packages/harness/deerflow/sandbox/sandbox.py` 或私有文件读取适配层

不要持久化宿主绝对路径；新增 Run 绑定的 file reference，并在读取时重新检查授权、size、MIME 和对象版本。

### P0：每 Run 总委派上限

修改：

- `backend/packages/harness/deerflow/config/subagents_config.py`
- `backend/packages/harness/deerflow/agents/middlewares/subagent_limit_middleware.py`
- `backend/packages/harness/deerflow/agents/thread_state.py`
- `backend/app/reliability/execution.py` 的 stop reason/审计映射

以 `project_id + owner_user_id + run_id` 过滤 ledger，不允许 thread 历史串扰。

### P1：模型参数成为版本 payload

若产品需要 per-Agent sampling：

- 在 `backend/app/shared_assets/agent_service.py` 的 `AgentPayload` 增加严格模型；
- 在 `backend/packages/harness/deerflow/persistence/shared_assets/agent_model.py` 与 `full_schema.sql` 增加版本字段；
- 纳入 `_payload_checksum()`；
- 通过 `ProjectAssetResolver`、Run snapshot、`PrivateAgentManifest` 传递；
- 最后在 `backend/packages/harness/deerflow/models/factory.py:create_chat_model()` 合并。

不能只在运行时按 Agent slug 查询“最新配置”，否则破坏精确快照。

### P1：标题和错误页局部修复

- `title_middleware.py:_fallback_title()`
- `tool_result_meta.py:_classify_error_shell()`、`_as_status_line()`

这两项不依赖旧 Agent 存储，可按 `main` 测试原样迁移。

### P2：可信配置中间件（评估后禁止移植）

结论：不移植 `main` 的 `extensions.middlewares` 动态 loader，也不另造可由
`config.yaml` 指定 Python module path 的替代入口。

原因：

- `main` 的 loader 会导入并零参数构造任意类。即使配置文件由运维管理，让
  Gateway 和 Scheduler 为了“三进程一致性”执行同一导入/构造，也会把 Agent
  扩展代码的导入副作用扩散到两个不执行 graph 的进程；Scheduler
  “只做准入、不导入或执行 Agent graph”边界因此变得不可证明。
- 当前跨进程 readiness 只有 Worker 心跳/容量聚合和 Scheduler advisory lock，
  没有插件配置摘要。为了证明三个独立部署实际使用同一类列表，需要扩展
  `full_schema.sql`、Worker registry 和 Scheduler 所有权状态；这不是一个局部
  Agent 能力移植，也不应为了可选扩展扩大 final schema。
- `dev` 已把旧 `extensions` 设为 fail-closed tombstone。重新开放同名入口会与
  M7 的 source-absence/config contract 直接冲突，并给项目字段误接 module path
  留下跨租户代码执行风险。

替代落点：

- 保留 `build_middlewares(custom_middlewares=...)` 作为显式、代码侧注入点；
  当前只有嵌入式 `DeerFlowClient` 将调用方直接提供的对象传入，生产私有 Run
  仍由 Worker-only 组合链执行，配置和项目数据不会解析为 Python 类。
- `extensions` 继续拒绝；`agent_middlewares`、`configured_middlewares` 和
  `trusted_middlewares` 等易误解别名也显式拒绝，而不是被 `AppConfig.extra`
  静默接受。
- Agent payload、`AgentVersionRow`、项目 API 和 readiness 都不增加 middleware
  或 module path 字段；现有内置中间件继续使用各自的严格 typed config。
- 增加 source-absence gate，禁止重新引入
  `configured_extensions.py`、`extensions.middlewares` 或同名动态 loader。

## 12. 禁止合并项

以下代码不能从 `main` 直接合入：

1. `backend/packages/harness/deerflow/persistence/agents/` 整包。
2. `AgentStorageConfig` 和 `agent_storage.backend`。
3. 旧版单表迁移 `backend/packages/harness/deerflow/persistence/migrations/versions/0006_agents.py`。
4. `backend/scripts/migrate_agents_to_db.py`。
5. 旧 `/api/agents` 可变 CRUD。
6. `setup_agent` / `update_agent` 作为运行时自修改权威。
7. `main` 的旧 `AgentRow`。
8. 按 Agent 名称在执行时读取最新定义。

尤其不能创建 `main` 的 `agents(id string,user_id,name,config,soul)`：`dev` 已使用同名 `agents` 表，但列、主键、外键和不可变版本模型完全不兼容。

## 13. 建议测试矩阵

| 类别 | 场景 | 预期 |
| --- | --- | --- |
| prompt | 四文档分别含闭标签和伪造框架标签 | 只作为转义文本出现 |
| prompt | 正常 Markdown、中文、代码块 | 语义和可读内容保持 |
| snapshot | 发布后修改新 draft | 旧 Run 仍使用旧 checksum/version |
| model | request / Agent version / global 三层值 | 优先级稳定，显式 false 生效 |
| model | 未支持 provider 参数 | 在版本创建或模型装配时明确拒绝 |
| image | 20 MiB 边界、MIME 伪装、读取后对象变化 | fail closed，不写 base64 checkpoint |
| image | 远程 Sandbox 文件 | 通过 Run 绑定 reference 重读，不依赖宿主路径 |
| image | model 调用成功/异常/取消 | 临时 HumanMessage 都被清理 |
| delegation | 单批超限 | 只保留允许数量 |
| delegation | 多批累计超限 | 达到总量后拒绝新 `task` |
| delegation | 同 thread 不同 Run | ledger 不串计 |
| title | `max_chars` 小于 3、等于 3、大于 3 | 始终不超长 |
| web fetch | 404/403/503 标题、合法“404 Ways...”标题 | 精确分类，无误报 |
| middleware boundary | 旧 `extensions`、动态配置别名、Agent API module path | fail closed；只保留代码侧对象注入 |
| concurrency | 并发创建/发布同 Agent version | 一个成功，一个稳定 conflict |
| isolation | 跨 project/owner 猜测 asset/version ID | 404/403 契约稳定 |
| worker | membership、capability、job lease 中途撤销 | 下一个副作用边界立即停止 |

## 14. `dev` 移植执行计划与落地结果

### 14.1 移植原则

本轮按“能力可移植、权威模型不可移植”执行，边界如下：

1. 不引入 `main` 的 `AgentStore`、`agent_storage.backend`、旧单表
   `agents`、Alembic `0006`、旧 `/api/agents` CRUD 或运行时自修改工具。
2. Agent 定义仍由 PostgreSQL 共享资产管理；发布版本不可变，Run 准入固化
   Agent/Skill/MCP/Credential 精确闭包，Worker 只消费准入快照。
3. Gateway 不建图，Scheduler 不导入或执行 Agent graph；运行期 Agent 能力只在
   Worker/harness 的固定组合链中装配。
4. schema 只修改 `full_schema.sql`，不创建增量 migration，也不在启动期修补旧库。
5. 项目输入只进入严格字段模型；不允许把 Python module path、任意 provider
   kwargs、宿主绝对路径或客户端伪造的 authority 字段带入执行链。

### 14.2 分阶段计划

| 阶段 | 可移植能力 | `dev` 落点 | 状态 |
| --- | --- | --- | --- |
| P0-1 | Agent prompt 结构转义 | `lead_agent/prompt.py` + prompt bundle 回归 | 已完成 |
| P0-2 | 图片 checkpoint 瘦身 | `thread_state.py`、`view_image_tool.py`、`view_image_middleware.py`、serialization/API 防御 | 已完成 |
| P0-3 | 每 Run 委派总量 | `subagents_config.py`、durable ledger、Worker pre-run boundary、RunJournal | 已完成 |
| P1-1 | 每版本模型参数 | AgentVersion v3、checksum、Resolver、Private manifest、模型工厂、前端契约 | 已完成 |
| P1-2 | fallback title 长度 | `title_middleware.py` | 已完成 |
| P1-3 | `web_fetch` 错误页分类 | `tool_result_meta.py` | 已完成 |
| P2 | 配置式动态中间件 | 保留代码侧对象注入；配置入口与易混淆别名 fail closed | 评估后禁止移植 |
| 验收 | 后端/前端回归、格式、静态检查、真实 PostgreSQL gate | 当前 checkout | 本地功能验收完成；真实 PostgreSQL 与历史分析文档分类待处理 |

### 14.3 已落地的精确实现

#### Prompt

- v1 SOUL、named legacy SOUL、`exact_soul` 和 v2
  `AGENTS.md`/`SOUL.md`/`IDENTITY.md`/`USER.md` 的正文统一执行
  `html.escape(..., quote=False)`。
- 框架标签仍由平台生成，Markdown、中文和代码块内容保持可读；闭标签 breakout
  只能作为转义文本出现。

#### 图片

- checkpoint 只保存 MIME、size、SHA-256 和 Run 绑定的虚拟 file reference；
  不保存 base64，也不保存宿主绝对路径。
- file reference 固定 `sandbox_id + run_id`，私有 Run 还固定
  `project_id + owner_user_id`；每次模型调用前通过当前 authority 重新打开并校验
  路径根、scope、MIME、magic、大小和 digest。
- base64 只存在于本次 `ModelRequest` 的隐藏图片消息中，不写回 state；同步、异步、
  异常和取消路径都会等待读取线程收口。
- text-only 模型也安装 cleanup-only middleware，用来清除旧 checkpoint 的
  `viewed_images.base64` 和旧隐藏 data URL 消息；API/serialization 再做一层裁剪。

#### 委派总量

- 新配置 `subagents.max_total_per_run` 默认 6，合法范围 1..50；单批并发限制仍独立生效。
- durable ledger 以 `project_id + owner_user_id + run_id + tool_call_id +
  occurrence` 计数，重复 provider call ID 不再合并成一次。
- Worker 在执行前读取 checkpoint 并注入稳定的 pre-run message ID boundary。私有
  Run 遇到读取失败、结构异常、历史消息无稳定 ID 或 ID 重复时 fail closed；真正的
  首次空 checkpoint 合法。
- 只归因“总量上限额外丢弃”的调用；纯并发截断不会伪报总量耗尽。达到总量上限时，
  模型消息得到继续执行/总结提示，并记录不含 task 参数的 RunJournal middleware
  事件 `reason=subagent_total_limit`。
- 该事件是中间件审计事实，不伪装成 terminal Run `stop_reason`。客户端或复用配置中
  的 `stop_reason` 在 Gateway 入口和 Worker 二次清理。

#### 每版本模型参数

- `AgentModelSettings` 只允许
  `temperature`、`max_tokens`、`thinking_enabled`、`reasoning_effort`；
  extra 字段、错误类型和越界值 fail closed。
- 非空设置使用 Agent payload schema v3，并进入不可变 payload checksum；
  v1/v2 的 checksum 字节保持兼容。JSONB 同时受数据库 check constraint 与发布版本
  immutable trigger 保护。
- 数据链为：

  ```text
  Builder
    -> AgentVersionRow(model_settings, payload_schema_version=3, checksum)
    -> ProjectAssetResolver exact snapshot
    -> Run snapshot / PrivateAgentManifest
    -> Worker lead Agent
    -> create_chat_model()
  ```

- 合并优先级为 request > exact Agent version > global model profile；显式
  `thinking_enabled=false` 不会被真值判断吞掉。
- canonical `max_tokens` 只映射到 provider 类明确声明的
  `max_tokens`/`max_output_tokens`；不支持时明确报错，Codex endpoint 不再静默丢弃。
- HTTP JSON 省略未设置字段，不输出与前端 Zod 契约冲突的 `null`；空
  `model_settings` 也不会改变升级前 Builder 幂等请求的 checksum。

#### 标题、Web 与中间件边界

- fallback title 在 `max_chars < 3`、`= 3`、`> 3` 时都不超长。
- `web_fetch` 只按首个非空状态标题精确识别 HTTP error shell；合法标题
  “404 Ways ...” 不误判。
- 动态 Python middleware loader 不移植。`extensions` 继续作为已移除配置拒绝，
  `agent_middlewares`、`configured_middlewares`、`trusted_middlewares` 等别名也
  显式拒绝；生产私有 Run 只执行内置固定链。

### 14.4 验收与剩余边界

已覆盖的回归包括 prompt breakout、图片跨 Run/owner/sandbox 隔离、legacy
checkpoint 清理、取消收口、委派多轮累计/重复 ID/恢复边界、v1-v3 checksum、
provider 映射、前后端 JSON、Builder 幂等 checksum、标题与 Web error shell。
后端使用 Ruff format/check，前端使用完整单元测试和 `pnpm check`。

当前仍有三个明确边界：

1. 本机未配置 `POSTGRES_TEST_URL`，因此真实 PostgreSQL integration/release gate
   尚未执行；这不应被本地 mock/SQLite 测试替代。
2. 图片异步取消已能在固定分块之间协作停止并等待 reader close，但单次远程 Sandbox
   provider read 的硬超时仍由各 provider 自身控制。若要统一硬 deadline，应作为
   Sandbox 模块的独立改造，不在 Agent 移植中顺带扩张三个 provider。
3. `test_all_active_docs_describe_only_final_m7_surfaces` 当前报告 66 条 residue：
   `docs/2026-07-29-dev-main-module-code-analysis.md` 与
   `docs/dev-main-code-analysis/` 被现有 gate 当作 active docs 扫描，但这些对比文档
   必须准确记录已删除的旧 API/命令。未擅自放宽 gate 或移动用户文档；后续需明确
   选择把该目录归类为 historical analysis，或调整文档存放契约。
