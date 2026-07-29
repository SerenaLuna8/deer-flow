# 03. Memory 模块：main 实现、dev 对照与落地边界

## 1. 分析基线与范围

- `main` 基线：`e317f7b8d9b2afb4c3925812d4774da602c9f8f3`
- `dev` 基线：`8a91e95799c9b345d9540c7e201b33c603e7870c`
- main 演进区间：`3be3969f8fc3f2d2b6d36ef5c26fa5593d916f2a..e317f7b8`
- 范围：Memory 的读取注入、被动更新、显式工具、后端抽象、存储、并发、恢复、API 与测试。
- 不在本篇展开：Agent/Skill 目录本身、Run 调度、Checkpoint 表示和 SSE；它们只在 Memory 调用边界出现时说明。

本文结论来自上述两个提交快照的源码、测试和关键提交 diff，不把提交标题当作实现事实。

## 2. 结论

main 的核心增量不是“增加了一个 memory 文件”，而是把 Memory 做成了可替换的、三层职责清晰的机制：

1. `MemoryManager` 定义后端无关的读写能力；
2. `DynamicContextMiddleware` 只负责把一次读取结果安全地注入对话；
3. `MemoryMiddleware` 或 `memory_*` tools 负责写入，具体取决于 `memory.mode`。

最容易误读的一点是：Memory **不是每轮重新加载**。首次可用 turn 注入的隐藏 Memory 消息会随 checkpoint 冻结；同一天后续 turn 复用它。跨午夜只增加日期更新，不刷新 Memory。

dev 已经拥有更强的 SaaS 数据边界：Memory 的权威范围是
`(project_id, owner_user_id, namespace)`，写入 PostgreSQL，并在 API、提示注入和异步更新前重新验证 membership。main 的可插拔后端、检索算法和模板值得移植，但 main 的用户级文件/远端后端不能直接替换 dev 的项目级 PostgreSQL 权威。

## 3. main 源码地图

| 职责 | main 路径 | 关键符号 |
| --- | --- | --- |
| 共享配置 | `backend/packages/harness/deerflow/config/memory_config.py` | `MemoryConfig`, `should_use_memory_tools()`, `get_memory_config()` |
| 后端契约与工厂 | `backend/packages/harness/deerflow/agents/memory/manager.py` | `MemoryManager`, `get_memory_manager()`, `backend_requires_passive_writes_in_tool_mode()` |
| 被动写入 | `backend/packages/harness/deerflow/agents/middlewares/memory_middleware.py` | `MemoryMiddleware.aafter_agent()` |
| 注入 | `backend/packages/harness/deerflow/agents/middlewares/dynamic_context_middleware.py` | `DynamicContextMiddleware._inject()`, `abefore_agent()` |
| 压缩前抢救 | `backend/packages/harness/deerflow/agents/memory/summarization_hook.py` | `memory_flush_hook()` |
| 显式工具 | `backend/packages/harness/deerflow/agents/memory/tools.py` | `memory_search_tool()`, `memory_add_tool()`, `memory_update_tool()`, `memory_delete_tool()` |
| 中间件装配 | `backend/packages/harness/deerflow/agents/lead_agent/agent.py` | `build_middlewares()` |
| 注入文本入口 | `backend/packages/harness/deerflow/agents/lead_agent/prompt.py` | `_get_memory_context()` |
| DeerMem | `backend/packages/harness/deerflow/agents/memory/backends/deermem/` | `DeerMem`, Markdown storage、更新队列、FTS5 |
| Mem0 | `backend/packages/harness/deerflow/agents/memory/backends/mem0/` | `Mem0Manager`, HTTP client |
| OpenViking | `backend/packages/harness/deerflow/agents/memory/backends/openviking/` | `OpenVikingManager`, watermark/retry |
| 空实现 | `backend/packages/harness/deerflow/agents/memory/backends/noop/` | `NoopMemoryManager` |
| 管理 API | `backend/app/gateway/routers/memory.py` | status/list/import/export/fact CRUD |

## 4. main 的精确接口

### 4.1 配置

`MemoryConfig` 的宿主共享字段是：

```python
class MemoryConfig(BaseModel):
    enabled: bool = True
    mode: Literal["middleware", "tool"] = "middleware"
    injection_enabled: bool = True
    shutdown_flush_timeout_seconds: float = 30.0
    manager_class: str = "deermem"
    backend_config: dict[str, Any] = {}
```

后端私有配置只能放入 `backend_config`。旧 DeerMem 顶层键由
`load_memory_config_from_dict()` 迁入该字典；未知键告警并忽略。`get_memory_config()`
在已有 `AppConfig` 时触发签名检查式热加载，配置损坏则保留 last-good singleton。

### 4.2 `MemoryManager`

Tier 1 是每个后端必须实现的抽象接口：

```python
def add(
    self,
    thread_id: str,
    messages: list[Any],
    *,
    agent_name: str | None = None,
    user_id: str | None = None,
    trace_id: str | None = None,
) -> None

def get_context(
    self,
    user_id: str | None,
    *,
    agent_name: str | None = None,
    thread_id: str | None = None,
) -> str
```

Tier 2/3 提供默认实现或默认拒绝，包括：

- `add_nowait(...)`：默认委托 `add()`，供压缩前立即入队；
- `search(query, top_k=5, *, user_id, agent_name, category)`；
- fact CRUD、import/export、reload/warm；
- `shutdown_flush(timeout)`；
- 同步/异步包装与预压缩、turn hook。

两个类变量形成可执行约束：

- `supports_search` 必须与后端是否真正 override `search()` 一致；
- `mode == "tool"` 时不支持 search 的后端在实例化阶段失败；
- `requires_passive_writes_in_tool_mode` 默认 `False`，仅声明需要会话级被动提取的后端保留 middleware 写入；当前 `Mem0Manager` 为 `True`。

`get_memory_manager()` 解析内置名称或 dotted class path，构造 singleton。解析或类型不正确会 fail fast，不会静默切换到另一个持久化后端。

### 4.3 工具

精确工具签名：

```python
memory_search_tool(runtime, query: str, top_k: int = 5,
                   category: str | None = None) -> str
memory_add_tool(runtime, content: str, category: str = "context",
                confidence: float = 0.8) -> str
memory_update_tool(runtime, fact_id: str, content: str | None = None,
                   category: str | None = None,
                   confidence: float | None = None) -> str
memory_delete_tool(runtime: Runtime, fact_id: str) -> str
get_memory_tools() -> list
```

`_resolve_scope(runtime)` 从服务端 runtime 解析 `user_id` 与 agent bucket；工具不接受调用者自报的用户范围。

## 5. main 完整调用链

### 5.1 首次读取与冻结注入

```text
lead agent graph
  -> DynamicContextMiddleware.abefore_agent(state, runtime)
  -> asyncio.to_thread(_inject), 最长 5 秒
  -> _last_injected_date(messages)
  -> 首次无 reminder：
       选择第一个 genuine HumanMessage
       -> _build_full_reminder(runtime)
       -> prompt._get_memory_context(agent_name, user_id)
       -> get_memory_manager().get_context(...)
       -> _make_reminder_and_user_messages(...)
  -> reducer 持久化三条逻辑消息
       SystemMessage(original_id, date, hidden)
       HumanMessage(original_id + "__memory", untrusted memory, hidden)
       HumanMessage(original_id + "__user", actual user content)
```

Memory 保持 `HumanMessage` 权限级别，日期才是 `SystemMessage`。这避免可被用户影响的持久化内容提升为系统指令。

同一天再次执行时，`_last_injected_date()` 命中，`_inject()` 返回 `None`；因此不会再次调用 `get_context()`。跨午夜时只对最后一个 genuine HumanMessage 做日期 ID-swap，不读取新 Memory。

### 5.2 middleware 模式被动写入

```text
agent turn 完成
  -> MemoryMiddleware.aafter_agent(state, runtime)
  -> 解析 thread_id / agent_name / user_id / trace_id
  -> get_memory_manager().aadd(...)
  -> 后端过滤 Human/AI 消息
  -> 后端队列/HTTP/存储实现
```

同步 `after_agent()` 调 `manager.add()`；异步路径使用 `aadd()`，从而可把阻塞后端移出事件循环。

### 5.3 tool 模式显式读写

```text
build_middlewares()
  -> should_use_memory_tools(config) == True
  -> lead agent 绑定 memory_search/add/update/delete
  -> 模型选择工具
  -> tool._resolve_scope(runtime)
  -> get_memory_manager().search/CRUD(...)
```

tool 模式通常不装 `MemoryMiddleware`。只有
`backend_requires_passive_writes_in_tool_mode(manager_class)` 返回 `True` 才同时保留被动写入。不能把 tool 模式描述为“一定完全没有被动写”。

### 5.4 上下文压缩前写入

```text
SummarizationMiddleware 即将移除旧消息
  -> memory_flush_hook(messages, runtime)
  -> 配置/线程范围检查
  -> get_memory_manager().add_nowait(...)
  -> 再执行 summarization/RemoveMessage
```

这条链保证即将从 checkpoint 状态中移除的消息先进入 Memory 更新队列。

### 5.5 Gateway 管理 API

```text
/memory/* router
  -> authenticated user scope
  -> get_memory_manager()
  -> status/list/reload/import/export/fact CRUD
  -> 后端实现
```

main 这里是 user/agent 范围，不具备 dev 的 project + owner 资源范围。

## 6. 数据与状态生命周期

### 6.1 对话内状态

1. 首次可用用户消息触发一次完整注入；
2. date reminder、Memory block、真实用户消息通过稳定 ID 拆分；
3. checkpoint reducer 将隐藏消息持久化；
4. 同日后续 turn 复用 checkpoint 中的旧 Memory 快照；
5. midnight 只补日期；
6. 另一路在 turn 结束后异步提取新事实，新事实只会影响新线程或显式 reload 后重新形成的首次快照，不会隐式改写当前线程的 frozen prompt prefix。

### 6.2 DeerMem 持久化

- 用户摘要：`{root}/users/{user_id}/memory.json`；
- agent facts：按 agent bucket、fact ID 分片的 Markdown 文件；
- Markdown 是事实权威，FTS5/BM25 SQLite 索引是可重建派生数据；
- manifest/fact revision 用于冲突和恢复；
- 写入采用文件锁、临时文件、原子替换和 fsync；
- journal/recovery 目录处理跨多个文件写入中断；
- retrieval adapter 支持 scope 隔离、重建、增量通知和 dirty 后下次 search 修复。

OpenViking 与 Mem0 则将权威交给远端 HTTP 服务。OpenViking 额外持久化处理 watermark，区分 transient/permanent failure；Mem0 直接调用服务且因会话提取模型保留 tool-mode passive writes。

## 7. 并发、恢复与错误语义

- Dynamic injection 的异步入口将同步文件/网络工作放到 thread，5 秒后降级为“不新增注入”；checkpoint 里已有的 frozen Memory 仍有效。
- DeerMem 文件写入由 scope 锁、revision CAS、原子替换和 journal 保证；FTS 索引失败不会改变 Markdown 权威。
- 被动更新可 debounce；Gateway shutdown 调 `shutdown_flush()`，预算由
  `shutdown_flush_timeout_seconds` 限定，超时后仅尾部未完成更新可能丢失。
- `MemoryConflictError` 表示版本冲突，`MemoryCorruptionError` 表示不可安全读取的存储；工厂/配置错误 fail fast。
- search 能力在 manager 构造期验证，不把首个工具调用变成迟到的 `NotImplementedError`。
- 注入超时与检索索引损坏偏向可用性降级；后端选择失败、契约不一致和持久化损坏偏向失败封闭。

## 8. main 的测试与契约

关键测试覆盖：

| 测试 | 主要契约 |
| --- | --- |
| `test_memory_manager_interface.py` | 抽象方法、能力标志与 tool-mode invariant |
| `test_memory_manager_pluggable.py` | 内置名称/dotted class 解析、配置传递 |
| `test_memory_middleware.py` | scope 解析、同步/异步被动写 |
| `test_memory_prompt_injection.py` | 首次 ID-swap、角色隔离、同日冻结、跨日日期更新 |
| `test_memory_tools.py` | 显式工具范围、CRUD、错误映射 |
| `test_summarization_middleware.py` | 压缩前 `add_nowait`，agent/user scope 保留 |
| `test_memory_retrieval_adapter.py` | FTS5/BM25、中文分词、重建、并发、scope 隔离 |
| `test_deermem_self_contained.py` | Markdown 权威、事实生命周期、重启 |
| `test_openviking_memory_backend.py` | retry、watermark、失败分类 |
| `test_mem0_memory_backend.py` | HTTP 后端、过滤与 passive-write 声明 |
| `test_memory_router.py` | Gateway 管理 API |

这里没有跨 project/owner 的 release gate；那是 dev 的独立契约。

## 9. main 关键提交的实现演进

| 提交 | 实际代码变化 |
| --- | --- |
| `ad45f59d` / `01a89f23` | 把 DeerMem 直连调用收敛为 `MemoryManager` 分层契约、backend discovery/factory，并让 router/tools/middleware 依赖接口 |
| `4bf028d0` | 将 agent facts 从共享 JSON 拆到 agent-scoped Markdown 权威文件 |
| `8145d66a` | 收紧消息筛选和会话提取边界 |
| `959bf134` | 在 Gateway lifespan 增加有时间预算的 pending update drain |
| `795af20a` | 新增可重建 FTS5/BM25 adapter，Markdown 保持 source of truth |
| `2aaf74b0` / `9bb82250` | 增加 OpenViking，并补齐重试、错误分类和 durable watermark |
| `b3af8c91` | tool 模式要求查询式 recall 由模型显式触发，避免无条件把所有 facts 塞入 prompt |
| `352f247a` | 增加 Mem0，借 `requires_passive_writes_in_tool_mode=True` 表达“显式搜索 + 被动会话提取” |
| `54f3c43f`, `938391c1`, `158c4f96`, `feb28707`, `8e96a6a2` | 逐步加固 prompt/XML/Markdown 转义和用户内容的低权限注入 |

## 10. dev 对应实现

### 10.1 精确源码与符号

| 职责 | dev 路径 | 精确落点 |
| --- | --- | --- |
| API 应用边界 | `backend/app/private_work/memory_service.py` | `PrivateMemoryService._require/_read/_save/status/list/reload/export/import_memory/create_fact/update/delete` |
| PostgreSQL repository | `backend/packages/harness/deerflow/persistence/private_work/memory_repository.py` | `PrivateMemoryRepository.load/create_if_needed/save/clear` |
| harness 存储适配 | `backend/packages/harness/deerflow/agents/memory/storage.py` | `ProjectMemorySnapshot`, `ProjectMemoryStorage` |
| 异步更新队列 | `backend/packages/harness/deerflow/agents/memory/queue.py` | `MemoryQueueItem`, `ProjectMemoryMembershipRevalidator`, `ProjectMemoryUpdateQueue` |
| 被动写入 | `backend/packages/harness/deerflow/agents/middlewares/memory_middleware.py` | `MemoryMiddleware.aafter_agent()` |
| 私有提示注入 | `backend/packages/harness/deerflow/agents/middlewares/dynamic_context_middleware.py` | `_inject_private()`, `_project_memory_namespace()` |
| 模型 | `backend/packages/harness/deerflow/persistence/private_work/model.py` | `UserProjectMemoryRow`, `UserProjectMemoryFactRow` |

### 10.2 dev 完整调用链

私有读取：

```text
Worker 构造的 Runtime(private_scope)
  -> DynamicContextMiddleware.abefore_agent()
  -> _inject_private(state, scope)
  -> ProjectMemoryMembershipRevalidator.is_active(scope)
  -> ProjectMemoryStorage.load(scope, namespace)
  -> PrivateMemoryRepository.load()
  -> 格式化为低权限 HumanMessage
  -> checkpoint 冻结
```

dev 同样不是每轮 reload。已有 reminder 时 `_inject_private()` 走 date-only `_inject()`。
首次 dateless checkpoint 可能已有多个失败 turn 时，dev 选择**最后一个** genuine user message，防止 ID-swap 把旧失败提示移到当前提示之后；main 仍选第一个。

私有写入：

```text
MemoryMiddleware.aafter_agent()
  -> 要求 PrivateResourceScope + thread_id + run_id
  -> 过滤 Human/AI，识别 correction/reinforcement
  -> ProjectMemoryUpdateQueue.enqueue(MemoryQueueItem)
  -> debounce worker
  -> membership_version/current project/member 再验证
  -> ProjectMemoryStorage.load/save(expected_version)
  -> PostgreSQL CAS
```

管理 API：

```text
PrivateMemoryService method
  -> _require(context, capability)
  -> PrivateWorkRevalidator.require()
  -> ProjectMemoryStorage
  -> PrivateMemoryRepository
  -> version conflict / invalid / unavailable 的公开错误映射
```

数据权威是：

- `user_project_memories(project_id, owner_user_id, namespace, version, summary...)`
- `user_project_memory_facts(project_id, owner_user_id, memory_id, ...)`
- `save()` 以 version CAS 更新摘要，删除并重建同 scope facts；
- namespace 默认 `default`，agent memory 使用 `agent:{agent_name}`。

## 11. main 与 dev 的精确差异

| 维度 | main | dev |
| --- | --- | --- |
| 权威范围 | `(user_id, agent_name)` | `(project_id, owner_user_id, namespace)` |
| 权威存储 | 可插拔：本地 Markdown/远端服务/noop | PostgreSQL 两张受 scope 约束的表 |
| 接口模型 | 通用 `MemoryManager` | `PrivateMemoryService` + `ProjectMemoryStorage/Repository` |
| 模式 | middleware/tool，可按后端混合 passive write | 被动 queue + 管理 API，没有 main 的通用 tool-mode |
| 注入读取 | 同步 manager，经 `to_thread` | async PostgreSQL + membership revalidation |
| 首次消息选择 | 第一个 genuine user message | 最后一个 genuine user message |
| 并发 | backend-specific revision/queue | PostgreSQL version CAS + membership-version revalidation |
| 隔离 | user bucket；不是项目资源模型 | project + owner + namespace，服务端签发 context |
| 管理 API | 用户级 Memory router | capability 受控的 private-work service/router |
| frozen snapshot | 是 | 是 |

## 12. 已确认缺陷与风险

### 12.1 main 风险

1. main 的 user bucket 不等价于 dev 的 project/owner 授权，直接替换会造成跨项目可见性语义倒退。
2. tool mode 是否仍被动写取决于 backend class flag；只改配置/提示、不核对 backend 会产生双写或漏写。
3. 当前线程的 frozen snapshot 是刻意的 prefix-cache 设计，但意味着刚更新的事实不会自动进入已存在会话。
4. 远端后端把可用性和数据主权外移，不能自动满足 dev 的 exact admitted snapshot、credential 与项目删除语义。

### 12.2 dev 风险

1. dev 缺少统一 capability contract；若未来新增 search/tool 后各入口直接调 repository，容易绕过 service/revalidation。
2. `ProjectMemoryUpdateQueue` 是进程内 debounce；进程在 flush 前崩溃会丢失尚未持久化的尾部更新。
3. repository 的事实保存采用同 scope 删除后重建，必须持续用事务和 version CAS 包住，不能拆成多个提交。
4. 当前没有 main 的 FTS5/BM25 检索能力；事实规模增大后，全量注入和 CRUD 读取的成本会放大。

## 13. 可移植落点

以下 main 能力可适配进 dev，但落点必须保持 dev 的项目权威：

1. 将通用能力接口放在
   `backend/packages/harness/deerflow/agents/memory/`，实现必须接收
   `PrivateResourceScope`，底层仍走 `ProjectMemoryStorage`。
2. 显式 `memory_search` 工具应在
   `backend/packages/harness/deerflow/agents/memory/tools.py` 增加 private-scope 版本；scope 只从 runtime 取，调用前走 execution boundary/membership revalidation。
3. 检索可把 main 的 ranking/tokenization 思路移入新的 project-scoped adapter；索引键至少包含
   `(project_id, owner_user_id, namespace, fact_id)`，PostgreSQL 表仍是 source of truth。
4. main 的 escaping、模板、staleness review、consolidation 算法可落在 dev 的 queue updater 前后，但保存必须带 expected version。
5. main 的 backend capability/invariant 思路可用于 dev 的策略层，不能让 manager 自行决定项目身份。
6. dev 已有的“最新 genuine user message”修复应保留，不回退为 main 的 first-message 选择。

## 14. 禁止直接合并

- 禁止用 main `DeerMem` 文件、Mem0 或 OpenViking 直接替换 dev PostgreSQL Memory 权威。
- 禁止把 `user_id` 当成 `project_id + owner_user_id` 的等价替代。
- 禁止从工具参数、请求 body 或模型输出接受 scope。
- 禁止绕过 `PrivateMemoryService._require()`、queue membership revalidation 或 Worker execution boundary。
- 禁止把 Memory 文本改成 `SystemMessage`。
- 禁止实现“每轮自动 reload Memory”；这会破坏 frozen prompt prefix 语义。
- 禁止在 tool 模式无条件同时装 passive middleware；必须以明确策略避免意外双写。
- 禁止把派生检索索引当作事实权威。

## 15. 建议测试矩阵

| 场景 | 单元测试 | PostgreSQL/集成测试 |
| --- | --- | --- |
| frozen snapshot | 首次读一次、同日不再读、跨日仅日期 | 重启/续聊后隐藏消息稳定 |
| 注入权限 | date 为 system、Memory 为 hidden human | 客户端伪造 marker/ID 被拒或剥离 |
| project 隔离 | scope key 构造 | 同 user 跨 project、同 project 跨 owner、namespace 隔离 |
| 写入 CAS | version success/conflict | 两 Worker 并发更新只有一个版本推进，失败方可安全重试 |
| membership 撤销 | enqueue 后 revalidator false | 入队后撤销成员，队列不写事实 |
| 压缩前 flush | 原始消息传入且范围不丢 | 压缩与 queue save 并发不覆盖更新 |
| tool mode | backend capability、passive-write flag | search/add/update/delete 全链路仍受 private boundary |
| 检索 | 中文、连字符、category、top-k、重复 fact ID | 索引损坏重建且不跨 scope |
| shutdown | drain 成功、超时、异常 | Worker 终止窗口内已确认项不丢、未确认项可观测 |
| 管理 API | 输入校验和公开错误码 | 404/403、version conflict、导入导出、删除项目后的不可达 |
