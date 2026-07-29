# 12. MCP 模块：`main` 最终实现、提交演进与 `dev` 精确落点

## 1. 分析范围与结论

本文独立分析 MCP 的定义、发现、工具代理、OAuth、session、路由和安全边界；Credential 的通用加密细节只在 MCP closure 需要处说明。对比：

- 公共祖先：`3be3969f8fc3f2d2b6d36ef5c26fa5593d916f2a`
- `main`：`e317f7b8`
- `dev`：`8a91e957`

结论：

1. `main` 是“全局 JSON 配置 + Gateway 内全局工具缓存 + per-user/thread stdio session pool”架构，适合单实例可编辑配置，但配置、工具和秘密不是 Run 的不可变闭包。
2. `dev` 已把 MCP 改成 PostgreSQL system/project 共享资产：definition version 不可变、Credential slot/grant 独立审批、Agent version 精确引用、Run 准入固化 exact version/grant，Worker 每次发现和调用都重新校验并短暂解密。
3. `main` 最值得移植的局部修复有两个：外部 tool name 的 `^[A-Za-z0-9_-]+$` 边界校验，以及 OAuth refresh 在跨 event-loop/thread 场景下的锁与取消安全。前者是 `dev` 已确认缺失；后者应按 `dev` 的 one-shot 调用拓扑验证后选择性移植。
4. `main` 的 config file cache、`/api/mcp/config`、全局 session pool 和 masked secret round-trip 不能恢复到 `dev`；它们会绕过 exact snapshot、项目 policy、Credential grant 和 Worker-only 执行。
5. `dev` 的 one-shot MCP client 是隔离优先的设计：每次发现/调用重建连接，牺牲 stateful server 连续性和延迟，换取无跨 Run session 污染。它是明确取舍，不应误判为实现缺陷。

## 2. `main` 源码地图

| 层次 | `main` 路径 | 责任 |
| --- | --- | --- |
| 配置 | `backend/packages/harness/deerflow/config/extensions_config.py` | `McpServerConfig`、OAuth、routing、JSON/env 解析 |
| client 参数 | `backend/packages/harness/deerflow/mcp/client.py` | `build_server_params()`、`build_servers_config()` |
| 工具加载 | `backend/packages/harness/deerflow/mcp/tools.py` | 多 server 发现、名称校验、metadata、path rewrite、stdio wrapper |
| 全局缓存 | `backend/packages/harness/deerflow/mcp/cache.py` | lazy init、内容签名失效、session reset |
| OAuth | `backend/packages/harness/deerflow/mcp/oauth.py` | token 获取/缓存/刷新、interceptor、initial header |
| session | `backend/packages/harness/deerflow/mcp/session_pool.py` | stdio persistent session、loop ownership、LRU、清理 |
| Gateway API | `backend/app/gateway/routers/mcp.py` | admin GET/PUT config、mask/preserve secrets、cache reset |
| provenance | `backend/packages/harness/deerflow/tools/mcp_metadata.py` | `tag_mcp_tool()`、routing metadata |
| deferred tools | `backend/packages/harness/deerflow/tools/builtins/tool_search.py` | MCP catalog、搜索、promotion、prompt names |
| routing middleware | `backend/packages/harness/deerflow/agents/middlewares/mcp_routing_middleware.py` | 根据关键词自动 promotion |
| runtime metadata | `backend/packages/harness/deerflow/runtime/secret_context.py` | 拒绝 legacy MCP secret 进入 Run metadata |

## 3. `main` 配置模型和解析

### 3.1 类型

`McpRoutingConfig`：

```text
mode: Literal["off", "prefer"] = "off"
priority: int = 0              # 运行时 clamp 0..100
keywords: list[str] = []
extra = "forbid"
```

`McpToolOverride` 目前主要承载 per-tool routing，允许扩展字段。

`McpOAuthConfig`：

```text
enabled
token_url
grant_type: client_credentials | refresh_token
client_id / client_secret / refresh_token
scope / audience
token_field / token_type_field / expires_in_field
default_token_type
refresh_skew_seconds
extra_token_params
```

`McpServerConfig`：

```text
enabled: bool
type: stdio | sse | http
command / args / env
url / headers / oauth
description
routing / tools
tool_call_timeout
```

`_accept_transport_alias()` 接受 MCP spec 的 `transport` 并规范到历史字段 `type`，同时让显式 `type` 优先。

`ExtensionsConfig` 将：

```text
middlewares
mcpServers -> mcp_servers
skills
extra top-level fields
```

放在一个 `extensions_config.json`；向后兼容 `mcp_config.json`。

### 3.2 文件解析顺序

`ExtensionsConfig.resolve_config_path()`：

```text
显式 config_path
  -> DEER_FLOW_EXTENSIONS_CONFIG_PATH
  -> caller project root/extensions_config.json
  -> caller project root/mcp_config.json
  -> legacy backend/repository root
  -> None
```

显式路径或环境变量指向不存在文件时抛错；自动搜索找不到则返回 `None`，因为 extension 是可选功能。

`from_file()`：

```text
JSON read
  -> resolve_env_variables() 递归把 "$VAR" 换成 os.getenv(VAR)
  -> 未解析 placeholder 变成空字符串
  -> Pydantic model_validate
```

这里的 JSON 既是配置又包含秘密；它不是 `dev` 可接受的 Credential authority。

### 3.3 routing 合并

`resolve_effective_mcp_routing(server_config, original_tool_name)`：

1. 取 server-level routing；
2. 查 `server_config.tools[original_tool_name]`；
3. 只有 override 的 `routing` 在 `model_fields_set` 中时才覆盖；
4. 返回 JSON mapping。

这样一个空的 per-tool object 不会意外清空 server 默认 routing。

## 4. `main` 工具发现和执行链

### 4.1 client 参数

```text
build_server_params(server_name, config)
  stdio -> transport, command, args, env
  sse/http -> transport, url, headers
  unsupported/missing required field -> ValueError

build_servers_config(extensions_config)
  -> 只遍历 enabled servers
  -> 每 server 调 build_server_params
```

### 4.2 发现

`get_mcp_tools()` 的最终链：

```text
ExtensionsConfig.from_file()               # 每次初始化读取最新文件
  -> build_servers_config()
  -> get_initial_oauth_headers()
  -> build_oauth_tool_interceptor()
  -> resolve_variable() 加载 custom interceptors
  -> MultiServerMCPClient(tool_name_prefix=True)
  -> asyncio.gather(load_server_tools(server_name)...)
  -> 每 server 独立 fail-soft
  -> source server 与返回 tool list 保持 zip 对应
  -> tool name 校验
  -> tag_mcp_tool / tag_mcp_routing
  -> stdio 包 _make_session_pool_tool()
  -> 补 sync wrapper
```

`load_server_tools()` 捕获单 server 异常并返回 `[]`，因此一个坏 server 不会清空健康 server。最外层异常则记录并返回 `[]`。

### 4.3 外部 tool name 边界

`main` 定义：

```text
_VALID_MCP_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
```

校验发生在 tool 被标记、routing、session wrapper、deferred catalog 之前。不合法 tool 单独丢弃，其他 tool 保留。

这是安全边界，不只是 provider 兼容性：

- 绑定给 provider 的函数名通常也有限制；
- deferred MCP tool 暂时不绑定 provider；
- 它的名字先进入 `<available-deferred-tools>` 和 routing hint；
- 换行、反引号、尖括号可伪造 prompt 结构。

因此必须在不可信 MCP discovery 返回进入内部对象图的第一处校验。

### 4.4 stdio persistent wrapper

`_make_session_pool_tool(tool, server_name, connection, tool_interceptors, tool_call_timeout)` 创建 `StructuredTool`，其 coroutine：

```text
call_with_persistent_session(runtime, **arguments)
  -> _extract_thread_id(runtime)
  -> resolve_runtime_user_id(runtime)
  -> scope_key = f"{user_id}:{thread_id}"
  -> stdio:
       asyncio.to_thread(_prepare_stdio_workspace)
       固定 cwd 到 mounted user-data
       TMPDIR/TMP/TEMP 默认放到相同 tree
       记录调用前 workspace snapshot
  -> MCPSessionPool.get_session(server, scope_key, connection)
  -> 逐层执行 OAuth/custom interceptors
  -> session.call_tool(original_name, arguments, timeout/meta)
  -> 有文本结果时 to_thread 计算 changed files
  -> to_thread(_convert_call_tool_result)
```

同时使用 user 和 thread 是必要的；只用 thread ID 会让不同用户的同名 thread 共享 stateful MCP session。

### 4.5 结果和本地路径

`_convert_call_tool_result()` 支持 MCP：

- `TextContent`
- `ImageContent`
- `ResourceLink`
- `EmbeddedResource`
- `structuredContent`
- 已经是 `ToolMessage` / `Command` 的 interceptor short-circuit

对 stdio server，它把 thread user-data 内的本地 URI/绝对路径转换为 `/mnt/user-data/...` 虚拟路径。远程 URI、tree 外文件和无法安全解析的 malformed path 保持原样。

`isError=True` 会把文本拼成 `ToolException`；structured result 进入 artifact，不与 model-visible content 混成字符串。

## 5. `main` cache 与 session 生命周期

### 5.1 全局 tool cache

`mcp/cache.py` 状态：

```text
_mcp_tools_cache
_cache_initialized
_initialization_lock: asyncio.Lock
_config_path
_config_signature: (mtime, size, sha256)
```

`initialize_mcp_tools()` 在 async lock 内只初始化一次。`get_cached_mcp_tools()` 是同步入口：

- 先 `_is_cache_stale()`；
- 未初始化时根据当前 loop 状态选择 `run_until_complete`、`asyncio.run` 或 thread 中的 `asyncio.run`；
- 初始化失败返回 `[]`。

签名同时比较 path 和 `(mtime,size,sha256)`，能发现：

- 同秒修改；
- mtime 回退；
- same size 内容替换；
- 切换到不同配置路径。

配置文件删除/临时不可读时保持 last-known-good，而不是清空已加载工具。这是明确的 fail-soft availability 语义。

### 5.2 `MCPSessionPool`

主要签名：

```text
class MCPSessionPool:
    MAX_SESSIONS = 256
    SESSION_CLOSE_TIMEOUT = 5.0

    _run_session(connection, ready, close_evt)
    get_session(server_name, scope_key, connection) -> ClientSession
    close_scope(scope_key)
    close_server(server_name)
    close_all()
    close_all_sync()
```

registry key 是 `(server_name, scope_key)`；entry 保存：

```text
ClientSession, owning_loop, owner_task, close_event
```

session context manager 的 `__aenter__`、`initialize()` 和 `__aexit__` 全部由同一个 owner task 执行，满足 anyio cancel-scope 的 same-task 要求。

并发创建通过 `_inflight` 共用一个 `ready Future`。registry 只在 `threading.Lock` 内做无 await 的原子检查/登记；慢清理在锁外执行。

跨 loop 条目不会直接复用：

- 当前 loop：复用；
- foreign/closed loop：evict，在 owner loop signal/cancel；
- LRU 达 256：移除最旧；
- cancellation/init failure：shield `ready`，确保 owner task 完成 `__aexit__`。

`reset_mcp_tools_cache()` 同时关闭并替换 session pool，防止配置更新后旧连接继续携带旧 header/env。

## 6. `main` OAuth 并发与秘密生命周期

### 6.1 `OAuthTokenManager`

```text
class OAuthTokenManager:
    from_extensions_config(config)
    has_oauth_servers()
    oauth_server_names()
    get_authorization_header(server_name) -> str | None
    _is_expiring(token, oauth)
    _fetch_token(oauth) -> _OAuthToken
```

token cache 按 server name，过期判断包含 `refresh_skew_seconds`。

### 6.2 跨 loop 锁

`main` 最终使用每 server 一个 `threading.Lock`，原因是同步 tool wrapper 可能在不同 worker thread 里每次 `asyncio.run()`，共享 `OAuthTokenManager` 却不共享 event loop。`asyncio.Lock` 在竞争后具有 loop affinity，会出现 cross-loop error 或静默死锁。

获取 OS lock 不能直接在 event loop 阻塞，因此：

```text
acquire_task = create_task(asyncio.to_thread(lock.acquire))
await shield(acquire_task)
```

如果等待者被取消，底层 thread 的 `lock.acquire()` 无法取消；代码持续 shield 等待它真正取得锁，立即 release，再重新抛 `CancelledError`。否则协程退出后底层 thread 仍可能拿到锁，且永远无人释放。

double-check token 发生在取得锁后，确保并发请求只发一次 token HTTP 请求。

### 6.3 refresh-token rotation

若 refresh grant 的响应包含新的 `refresh_token`，`main` 更新内存中的 `McpOAuthConfig.refresh_token`。不写回 JSON，但本进程下次刷新会使用新 token；否则 rotation provider 的旧 token 已失效，下一次必然 `invalid_grant`。

### 6.4 初始 OAuth fail-soft

`get_initial_oauth_headers()` 逐 server 获取 token；单 server 失败记录 warning 并继续。后续 discovery 仍可让无 OAuth 或健康 OAuth server 提供工具。

## 7. `main` Gateway 配置 API

路由：

```text
GET  /api/mcp/config
PUT  /api/mcp/config
POST /api/mcp/cache/reset
```

全部调用 `require_admin_user()`。

### 7.1 GET 脱敏

`_mask_server_config()`：

- 所有 env value -> `***`；
- 所有 header value -> `***`；
- OAuth `client_secret` / `refresh_token` -> `None`；
- extra 字段按规范化 secret-key taxonomy 递归 mask。

### 7.2 PUT round-trip

`_merge_preserving_secrets()` 区分：

- 已有 env/header key 收到 `***`：保留磁盘真实值；
- 新 key 收到 `***`：400；
- OAuth secret `None`：保留；
- OAuth secret `""`：显式清除；
- 未提交 routing/tools：保留；
- 未提交 top-level/extra keys：保留。

`_validate_mcp_update_request()` 对 API 管理的 stdio command 只接受单 executable name：

- 默认 allowlist：`npx`、`uvx`；
- 可通过 `DEER_FLOW_MCP_STDIO_COMMAND_ALLOWLIST` 扩展；
- 拒绝路径分隔符、空白、shell metachar。

这只保护 HTTP API；operator 直接编辑本地 JSON 仍可表达高级配置。

### 7.3 阻塞 I/O 和并发

```text
PUT
  -> _validate_mcp_update_request()
  -> async with _mcp_config_write_lock
  -> asyncio.to_thread(_apply_mcp_config_update)
      resolve/read/merge/write/reload
  -> reset_mcp_tools_cache()
```

async lock 只串行化单进程 PUT；多 Gateway 进程仍可能竞争同一文件。`main` 路由的 generic exception 把 `str(e)` 放进 HTTP 500，也是错误细节泄漏风险。

## 8. `main` deferred MCP 与路由

工具加载后 `tag_mcp_tool(tool)` 写 provenance，`tag_mcp_routing(tool, routing)` 写有效 routing。Agent 构造链：

```text
policy-filtered tools
  -> assemble_deferred_tools()
  -> MCP tools 建 DeferredToolCatalog
  -> tool_search 暴露搜索入口
  -> <available-deferred-tools> 只列 names
  -> McpRoutingMiddleware 根据 operator keywords 自动 promotion
  -> DeferredToolFilterMiddleware 控制本次 model binding
```

`tool_search` 搜索结果返回完整 schema，并通过 per-thread graph state 记录 promoted names。routing 是提示和 promotion，不是授权；授权必须在 deferred catalog 构造前完成，也必须在真正执行时再次检查。

## 9. `main` 测试与行为契约

| 测试文件 | 关键契约 |
| --- | --- |
| `backend/tests/test_mcp_tool_name_validation.py` | tag/newline/markdown tool name 被逐个丢弃，合法 identifier 保留 |
| `backend/tests/test_mcp_cache.py` | same-mtime、backward-mtime、same-size、path switch、文件删除 last-known-good |
| `backend/tests/test_mcp_oauth.py` | token cache、interceptor、per-server fail-soft、rotation、跨线程无死锁、取消不泄漏锁 |
| `backend/tests/test_mcp_session_pool.py` | user/thread scope、LRU、same-task cleanup、cross-loop、inflight dedup、取消、stdio cwd/temp/path |
| `backend/tests/blocking_io/test_mcp_router.py` | PUT 文件操作不阻塞 event loop、并发更新串行 |
| `backend/tests/test_mcp_file_migration.py` | legacy/new config 路径与格式兼容 |
| `backend/tests/test_mcp_routing_config.py` | routing priority/override |
| `backend/tests/test_mcp_routing_metadata.py` | provenance 和 routing metadata |
| `backend/tests/test_mcp_routing_prompt.py` | deferred names/routing hint 的 prompt |
| `backend/tests/test_mcp_routing_auto_promote.py` | keyword promotion 和 catalog hash |
| `backend/tests/test_run_metadata_secret_safety.py` | legacy `auth_token`/config secret 不进入 Run response |

其中 name validation、OAuth cancellation 和 blocking-I/O 测试是局部补丁不可分割的一部分。

## 10. `3be3969f..main` 提交演进

| 日期 | 提交 | 变化 | 可移植价值 |
| --- | --- | --- | --- |
| 2026-07-12 | `b963282f` | OAuth 初始获取 per-server fail-soft，保存 rotated refresh token | 系统 MCP OAuth |
| 2026-07-14 | `79cdd99f` | load boundary 校验 MCP tool name | `dev` P0 |
| 2026-07-14 | `cdefd4a8` | cache 从 mtime 改为 path+content signature | 仅旧全局 cache |
| 2026-07-15 | `289adcbb` | config PUT 阻塞 I/O 移到 thread，RMW 加锁 | 仅旧文件 API |
| 2026-07-19 | `3ed2e1f1` | shared signature helper 与 optional path 契约修正 | 仅旧文件 config |
| 2026-07-22 | `44990ff1` | OAuth 改 `threading.Lock`，处理跨 loop/thread | 条件移植 |
| 2026-07-25 | `07d8b988` | malformed path-like result 不再破坏转换 | 结果转换可参考 |
| 2026-07-28 | `b1984cf4` | 拒绝 legacy MCP Credential 进入 Run metadata | 原则已由 `dev` exact snapshot 覆盖 |

演进顺序表明 `main` 在修补一个进程内全局运行时；`dev` 已经改变权威来源，所以不能按 commit 顺序整串 cherry-pick。

## 11. `dev` 对应实现

### 11.1 PostgreSQL 资产模型

源码：

| 层次 | `dev` 路径 |
| --- | --- |
| ORM | `backend/packages/harness/deerflow/persistence/shared_assets/mcp_model.py` |
| 定义服务 | `backend/app/shared_assets/mcp_service.py` |
| Credential closure | `backend/app/shared_assets/credential_closure.py` |
| 项目 API | `backend/app/gateway/routers/project_assets.py` |
| 系统治理 API | `backend/app/gateway/routers/admin_assets.py` |
| Run snapshot ORM | `backend/packages/harness/deerflow/persistence/private_work/model.py` |
| Run snapshot service | `backend/app/private_work/snapshot_repository.py` |
| exact resolver | `backend/app/shared_assets/resolver.py` |
| Worker runtime | `backend/app/private_work/asset_runtime.py` |

核心行：

```text
McpServerRow
  scope, project_id, slug, status,
  current_published_version_id, optimistic version

McpServerVersionRow
  immutable definition fields, workflow_status,
  supersedes_version_id, payload_checksum

McpCredentialSlotRow
  version_id, name, purpose, payload_schema, required

RunAssetVersionRow
  admitted exact MCP asset/version/checksum/order

RunMcpGrantSnapshotRow
  admitted slot/grant/credential-version relation
```

Definition、Credential 和 Run snapshot 是三个不同生命周期：

- definition 不保存 Credential 明文；
- slot 只声明需要哪些字段；
- grant 把 slot 绑定到批准的 Credential version；
- Run snapshot 只保存 exact IDs/checksum/grant revision，不保存 envelope/plaintext。

### 11.2 `McpService`

公开 contract：

```text
@dataclass(frozen=True) CreateMcpServer(slug, display_name)
@dataclass(frozen=True) McpCredentialSlot(name, purpose, payload_schema, required)
@dataclass(frozen=True) McpDefinition(...)

McpService.create_asset()
McpService.create_version()
McpService.submit_approval()
McpService.approve()
McpService.configure_system_credential_grants()
McpService.publish()
McpService.archive()
McpService.suspend()
McpService.get()/list_visible()/get_version_history()
```

每次 create/version/transition 都经过 `_validate_definition()`；publish 还调用 `_validate_transition_definition()` 从 locked row 重建并重新计算 checksum，阻止历史数据库行漂移绕过新 policy。

持久化前扫描完整 definition：

- secret-like key；
- compact/camel/snake 变体；
- URL userinfo、敏感 query/fragment/value；
- CLI `--token` / `token=` 等 carrier；
- env/header/oauth/routing/tool override 嵌套值。

项目 MCP 不允许 literal secret。System packaged MCP 保留 stdio/env/header/OAuth 兼容，但其 definitions 只来自 digest-checked bootstrap catalog。

### 11.3 project MCP policy

中性策略放在：

- `backend/packages/harness/deerflow/mcp_endpoint_policy.py`
- `backend/packages/harness/deerflow/mcp_definition_policy.py`
- `backend/packages/harness/deerflow/config/mcp_security_config.py`

它们不 import Agent graph，Gateway/Scheduler 可安全复用。

`McpSecurityConfig`：

```text
project_remote_allowed_endpoints: tuple[str, ...] = ()
require_egress_proxy: bool = True
egress_proxy_url: str | None
discovery_timeout_seconds: int = 15  # 1..300
tool_call_timeout_seconds: int = 60  # 1..300
extra = "forbid"
```

默认没有 endpoint，项目远程 MCP fail-closed。启用 endpoint 且要求 proxy 时，必须配置无 Credential 的 HTTP(S) proxy origin。

`validate_remote_mcp_endpoint_syntax()` 拒绝：

- 非 HTTPS；
- userinfo、fragment、反斜杠、空白/control；
- localhost/localdomain；
- IP literal、IPv6、纯数字/legacy numeric host；
- 非 canonical host。

`ExactMcpEndpointPolicy` 匹配完整 URL，不只匹配 origin。`validate_project_mcp_definition()` 再要求：

```text
transport in {"http", "sse"}
env == {}
headers == {}
oauth == {}
credential slot 只能 {"headers": [...]}
禁止 Host/Content-Length/Connection/Proxy-Authorization 等 hop-by-hop/proxy headers
```

`mcp_security` 是 startup-only 配置；Gateway、Scheduler 和所有 Worker 必须一起重启。

### 11.4 API 边界

项目路由位于：

```text
/api/projects/{project_id}/mcp-servers
/api/projects/{project_id}/mcp-servers/{asset_id}/versions
.../publish
.../submit-approval
.../approve
.../archive
.../suspend
```

系统 `/api/admin/assets/mcp-servers` 是只读治理面；唯一 MCP-specific 写入口是当前 packaged published version 的 Credential grants。

API 响应：

- 不返回 envelope 或明文；
- project 历史 definition 的 env/header 清空；
- project remote URL 最多显示 HTTPS origin，不回放 path/query；
- 系统资产也只暴露治理所需 metadata。

### 11.5 Run 准入

`RunSnapshotRepository.create_run_with_snapshot_in_session()`：

```text
锁 project/membership
  -> 锁 exact Agent version
  -> 展开 Agent 引用的 MCP asset/version
  -> _mcps() 读 immutable definitions
  -> _validate_dependency_order()
  -> _credential_closures()
  -> _validate_project_mcp_credential_slots()
  -> 写 Run + RunAssetVersionRow + RunMcpGrantSnapshotRow
  -> 同一事务提交
```

准入阶段重新拒绝 historical project stdio、policy 外 endpoint、无 endpoint policy、非 header slot、inactive/missing envelope、scope/schema mismatch。客户端不能通过“旧版本当时合法”绕过当前 project execution policy。

### 11.6 Worker exact runtime

`PrivateAgentRuntime` 的 MCP 核心符号：

```text
PrivateMcpManifest(asset_id, version_id, definition)
_DiscoveredMcpTool(version_id, name, description, args_schema, routing)

discover_mcp_tools()
_proxy_tool(schema)
_with_one_shot_mcp_tools(...)
_discover_exact_mcp(...)
_invoke_exact_mcp(...)
_validate_project_mcp_snapshot(...)
_validate_project_mcp_material(...)
invoke_with_mcp_material(...)
_materialize_mcp_call(snapshot)
```

发现链：

```text
Run exact snapshot
  -> invoke_with_mcp_material(version_id, discovery operation)
  -> current authorization
  -> _materialize_mcp_call()
       lock current project/membership/run/snapshot/grant/credential/envelope
       compare persisted snapshot with current closure
       decrypt exact material
  -> validate project definition/material
  -> _with_one_shot_mcp_tools()
       optional secure http client
       optional System OAuth header
       MultiServerMCPClient
       get_tools()
  -> _discover_exact_mcp()
       bound count/description/schema
       resolve bounded local `$ref`
       construct safe Pydantic args model
       reject secret echoed in name/description/schema/routing
  -> copy only `_DiscoveredMcpTool`
  -> close client，clear material/derived secrets
  -> `_proxy_tool()` 生成 run-local StructuredTool
```

执行链：

```text
proxy.ainvoke(arguments)
  -> 再次 invoke_with_mcp_material()
  -> 再次 current authorization + exact closure lock/decrypt
  -> one-shot discovery
  -> exact tool-name selection
  -> before_mcp_tool_dispatch/before_mcp_call
  -> selected.ainvoke(arguments)
  -> result 递归检查不得回显 admitted secret/derived OAuth token
  -> close/clear
```

proxy 带：

```text
metadata={"deerflow_private_mcp": True}
tag_mcp_tool(proxy)
可选 tag_mcp_routing(proxy, routing)
```

Subagent 只能从内部 `__runtime_mcp_tools` 取得带该 metadata 的 exact proxies，并 marshal 回 owner Worker loop；它不执行 global MCP discovery。

### 11.7 schema、网络和超时

`_bounded_mcp_schema_copy()` / `_resolve_mcp_schema_refs()` / `_safe_mcp_args_model()`：

- 限制 schema 深度、节点和序列化大小；
- 只允许 bounded local refs；
- 拒绝循环 refs、reserved 字段和不安全 property name；
- 显式构建 Pydantic 类型，不执行 server 提供的 Python。

`make_secure_mcp_http_client_factory()`：

```text
follow_redirects=False
trust_env=False
proxy=operator URL
operator hard timeout（忽略 adapter 传入的更宽 timeout）
connect/pool <= 5 秒
```

discovery 和 tool call 分别由 `asyncio.timeout()` 包围，关闭 client 还有独立 bounded timeout。错误映射为不包含 endpoint/secret 的 `PrivateWorkUnavailable` / stale error。

### 11.8 global cache tombstone

`dev` 保留 `backend/packages/harness/deerflow/mcp/cache.py` 只为兼容 import：

```text
initialize_mcp_tools(...) -> []
get_cached_mcp_tools(...) -> []
```

`_load_admitted_mcp_config()` 固定抛 `AssetCatalogUnavailable("global MCP discovery was removed...")`。这是 fail-closed tombstone，不是待恢复功能。

## 12. `main` 与 `dev` 逐项差异

| 主题 | `main` final | `dev` 当前 | 判断 |
| --- | --- | --- | --- |
| authority | JSON file | PostgreSQL exact immutable version | 保留 `dev` |
| secret | file env/header/OAuth | encrypted Credential + approved slot/grant | 保留 `dev` |
| API | global admin file RMW | project capability + workflow API | 禁止恢复旧 API |
| tool cache | process global + signature | fail-closed tombstone | `dev` 有意移除 |
| session | stdio persistent per user/thread | one-shot per discovery/call | isolation/状态取舍 |
| project stdio | API allowlist 可建 | 完全禁止 | 保留 `dev` |
| endpoint | URL 字符串 | exact HTTPS operator allowlist + secure client | `dev` 更强 |
| redirects/env proxy | adapter 默认 | false/false | `dev` 更强 |
| discovery failure | per-server fail-soft | admitted exact asset失败会终止 Run | `dev` 应 fail-closed |
| schema | adapter schema | bounded copy/ref/Pydantic | `dev` 更强 |
| secret echo | 无通用阻断 | schema/result literal guard | `dev` 更强 |
| tool name | regex identifier | 只检查非空和 <=255 | `dev` 回归 |
| deferred prompt | validated + escaped | raw sorted names | 与 name 回归叠加 |
| OAuth lock | cancellation-safe `threading.Lock` | `asyncio.Lock` | 需按调用拓扑审计 |
| rotated refresh | 保存在内存 config | 丢弃 rotated token | System OAuth 风险 |
| initial OAuth | per-server fail-soft | helper fail-hard | exact asset 与全局语义不同 |
| result injection | name-based web sanitizer不含任意 MCP | proxy 有 provenance 但 sanitizer 未用 | `dev` 可补 |

## 13. 已确认缺陷与风险

### 13.1 `dev`：MCP tool name 未做 identifier 校验

`PrivateAgentRuntime._discover_exact_mcp()` 当前只检查：

```text
name 非空
len(name) <= 255
```

没有 `main::_VALID_MCP_TOOL_NAME`。随后 name 被：

1. 放入 `_DiscoveredMcpTool`；
2. 用于 `StructuredTool.from_function(name=...)`；
3. 标为 MCP；
4. 进入 `assemble_deferred_tools()`；
5. `get_deferred_tools_prompt_section()` 用 `"\n".join(sorted(deferred_names))` 裸插值。

恶意 server 可返回带换行、反引号或尖括号的 name，伪造 `<available-deferred-tools>` 内容。该缺失由源码链确认。

### 13.2 `dev`：MCP 结果 prompt injection

private proxy 已有 `deerflow_private_mcp=True`，但 `ToolResultSanitizationMiddleware` 仍只按内置 web tool name 净化。远程 MCP result 可携带 framework tags，且不会命中。

这与“禁止结果回显 Credential literal”不同：secret echo guard 不是 prompt-injection sanitizer。

### 13.3 `dev`：System OAuth rotation

`OAuthTokenManager._fetch_token()` 不保存响应中的 rotated `refresh_token`。Project MCP 禁止 OAuth，所以项目主路径不受影响；packaged System MCP 若使用 rotating refresh grant，第二次 refresh 可能失败。

### 13.4 `dev`：OAuth cross-loop 兼容风险

当前每 server 使用 `asyncio.Lock`。Private one-shot manager 通常在一个 Worker loop 内创建和销毁，尚不能仅凭 diff 断言主路径死锁；但若 System MCP interceptor/同步 wrapper 让同一 manager 跨 loop/thread 复用，则会重现 `main` 已修复问题。

因此这是必须用调用点和跨线程测试验证的风险，不应无条件整段替换。

### 13.5 `dev`：stateful MCP 能力变化

one-shot 每次调用重新 discovery/connection，Playwright 一类依赖 session 状态的 MCP 不能延续页面/登录状态，并产生额外网络成本。它同时消除了跨 Run session 污染。若产品需要 stateful 行为，应设计 run-owned、owner-loop-owned、exact-version-keyed pool，而不是恢复全局 `MCPSessionPool`。

### 13.6 `main`：配置 API 错误细节和跨进程 RMW

`main` 的 PUT 500 返回 `str(e)`，可能泄漏路径或配置细节；`_mcp_config_write_lock` 只保护单进程，多 Gateway writer 仍可丢更新。这两点都说明旧文件 API 不适合作为 `dev` 平台权威。

## 14. 可移植落点

### P0：在 exact discovery 边界校验 tool name

目标：

- `backend/app/private_work/asset_runtime.py::PrivateAgentRuntime._discover_exact_mcp`
- 新增 `backend/tests/test_private_asset_runtime.py` 用例

规则：

```text
^[A-Za-z0-9_-]+$
```

应在复制 description/schema/routing 和构造 proxy 之前执行。与 `main` 的全局“丢弃单个坏 tool”不同，`dev` 的 admitted exact asset 应 fail-closed 整个 MCP version，避免 silently 改变已准入能力集合。

### P0：锁定 deferred prompt 不变量

目标：

- `backend/packages/harness/deerflow/tools/builtins/tool_search.py::get_deferred_tools_prompt_section`
- deferred catalog tests

即使 discovery 已校验，renderer 仍建议 `html.escape(name, quote=False)` 作为纵深防御，并测试 closing tag/newline 不可进入。校验仍是主边界，转义不是允许任意 name 的理由。

### P1：按 provenance 净化 MCP result

目标：

- `backend/packages/harness/deerflow/agents/middlewares/tool_result_sanitization_middleware.py::_should_sanitize`
- `backend/tests/test_tool_result_sanitization_middleware.py`

读取 server-created `request.tool.metadata["deerflow_private_mcp"] is True`；不要接受客户端 tool call args 中的 metadata，也不要用名称子串猜测。

### P1：System OAuth rotation

目标：

- `backend/packages/harness/deerflow/mcp/oauth.py::OAuthTokenManager._fetch_token`
- `backend/tests/test_mcp_oauth.py`

仅更新 run-local/in-memory `McpOAuthConfig`，不写数据库、snapshot 或日志。测试 two-refresh 使用新 refresh token。

### P1：跨 loop OAuth 审计

先为以下路径增加测试：

- owner Worker loop 的 one-shot system OAuth；
- Subagent marshal；
- sync wrapper 若仍可达；
- cancellation while waiting。

只有确认 manager 跨 loop 共享时，才移植 `threading.Lock + shielded to_thread acquire`。不能把 OS lock 同步阻塞在 event loop。

### P2：若需要 stateful MCP，设计 run-owned pool

新 pool 必须至少以：

```text
project_id + owner_user_id + run_id + exact mcp_version_id
```

为 key，owner Worker loop 执行 enter/exit，membership/lease/grant 撤销时立即 close，且永不跨 Run 复用。`main` 的 `user_id:thread_id` key 不满足 `dev` 权限模型。

## 15. 禁止合并项

1. `backend/packages/harness/deerflow/config/extensions_config.py` 及 `extensions_config.json` MCP authority。
2. `backend/app/gateway/routers/mcp.py` 的 `/api/mcp/config` 和 `/api/mcp/cache/reset`。
3. `mcp/cache.py` 的全局 discovery/cache 恢复。
4. 以 file mtime/content signature 代替 exact DB version/checksum snapshot。
5. 把 `main` 的 masked `***` round-trip 当成 Credential storage。
6. project stdio、literal env/header、project OAuth、非 header Credential slot。
7. 跳过 exact endpoint allowlist、redirect 禁止、`trust_env=false` 或 controlled proxy。
8. 以 per-server fail-soft 跳过一个已准入 MCP；exact Run closure 漂移必须 fail-closed。
9. 直接恢复 `(server_name, user_id:thread_id)` 全局 session pool。
10. 将秘密放入 tool metadata、description、schema、routing、Run snapshot、API 或 error detail。
11. 让 Gateway 执行 discovery/tool call；Worker 仍是唯一 graph/MCP executor。
12. 用旧 migration chain 更新 `dev`；当前只接受空库的 `full_schema.sql`。

## 16. 建议测试矩阵

| 类别 | 场景 | 预期 |
| --- | --- | --- |
| definition | project stdio/streamable_http | authoring、准入、Worker 均拒绝 |
| definition | literal env/header/oauth | 持久化前拒绝 |
| endpoint | localhost/IP/numeric/userinfo/query/fragment | config/policy 均拒绝 |
| endpoint | origin 相同但 path 不同 | exact allowlist 不匹配 |
| endpoint | policy 未注入 | fail-closed |
| HTTP | 302 redirect / env proxy | 不跟随、不读取环境代理 |
| HTTP | adapter 请求更宽 timeout | operator hard timeout 胜出 |
| slots | Host/Content-Length/Proxy-Authorization | 拒绝 |
| snapshot | historical project stdio/unsafe slot | 准入拒绝 |
| snapshot | inactive grant/envelope/version drift | 准入或调用拒绝 |
| closure | grant 在 Run 中撤销 | 下一 discovery/call 拒绝 |
| tool name | tag/newline/markdown/Unicode control | exact version fail-closed |
| tool name | `[A-Za-z0-9_-]+` | 保留 |
| deferred | 恶意 name 尝试进入 prompt | renderer 不可被闭合 |
| schema | huge/deep/cyclic/ref escape/reserved field | 构造 model 前拒绝 |
| schema | bounded local refs | 生成稳定 Pydantic schema |
| discovery | server 超时 | bounded generic unavailable，无 endpoint |
| call | tool 超时 | bounded generic unavailable，无 args/secret |
| secret | metadata/schema/result 回显明文 | 拒绝并清空 material |
| secret | OAuth result 回显 access token | 拒绝 |
| result | MCP 返回 framework tags | provenance sanitizer 转义 |
| authz | membership/lease 在 materialize 后撤销 | dispatch 前再次拒绝 |
| isolation | 同 tool/version 在不同 project/owner | Credential、proxy、结果不交叉 |
| OAuth | rotating refresh 两次刷新 | 第二次使用新 refresh token |
| OAuth | 同 loop 并发 | 只获取一次 token |
| OAuth | cross-loop/cancel（若路径可达） | 无 deadlock、无泄漏锁 |
| cleanup | discovery/call/cancel/close error | client bounded close，material/derived secrets clear |
| stateful | one-shot server 连续两调用 | 行为明确记录；若不支持应给出产品可理解限制 |
