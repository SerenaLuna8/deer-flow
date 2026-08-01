# 15. Tools / Models 模块：`main` 具体实现与 `dev` 对照

## 1. 范围与总判断

本文件以模型适配层和通用工具层为主。Agent 编排见 `01-agent.md`，MCP 见
`12-mcp.md`，Sandbox 文件权限见 `13-sandbox.md`。为使这些能力能安全落入 `dev`，
本文也记录了必须同步收紧的公开流序列化、Worker 公共错误边界、私有文件 finalizer、
聚合审计和前端审计展示；这些是移植落点的配套边界，不是把其他模块混入本轮。

`main@e317f7b8` 在这一模块有四类值得关注的实现：

1. 模型工厂从“类路径白名单”改为“类型能力判断”，修复 OpenAI-compatible 子类的 endpoint、stream timeout 与未知参数问题；
2. vLLM/MindIE 对推理内容、用量、转义和超时做 Provider 级兼容；
3. 工具输出、图片、历史上传和 ACP 调用更完整；
4. 新增进程内 Playwright 浏览器会话，但其 Gateway 亲和设计与 `dev` 的 Worker-only 执行不兼容。

结论：

- 模型工厂、MindIE 转义、vLLM 累计用量转增量、ACP timeout、Browserless timeout 等是低耦合修复，可以逐项移植。
- `main` 的 `view_image` 虽解决 checkpoint 体积问题，却把 Host `actual_path` 放进状态；在
  `dev` 的私有文件模型中仍不合格，本轮改为授权后的 Run-bound sandbox reference。
- `list_uploaded_files` 的产品能力有价值，但旧实现扫描 Host thread 目录；`dev` 必须查询 PostgreSQL 私有文件仓库。
- Browser automation 必须重新设计 session owner/lease，不能原样合入 Worker-only 运行时。

## 2. 源码地图

### 2.1 Models

| `main` 文件 | 作用 |
| --- | --- |
| `backend/packages/harness/deerflow/models/factory.py` | 模型配置解析、thinking 变换、Provider 构造、tracing |
| `config/model_config.py` | 模型声明和 Provider 特性元数据 |
| `models/vllm_provider.py` | vLLM reasoning、tool call、usage 兼容 |
| `models/mindie_provider.py` | MindIE 消息、流式和超时兼容 |
| `models/openai_codex_provider.py` | Codex Responses API 适配 |
| `models/patched_*.py` | DeepSeek、MiMo、MiniMax、StepFun、OpenAI 局部兼容 |
| `models/assistant_payload_replay.py` | assistant reasoning/tool payload 重放 |

### 2.2 Tools

| `main` 文件 | 作用 |
| --- | --- |
| `tools/tools.py` | 配置工具加载与工具集合 |
| `tools/builtins/tool_search.py` | 延迟工具搜索/提升 |
| `tools/builtins/view_image_tool.py` | 图片读取、格式验证与状态登记 |
| `agents/middlewares/view_image_middleware.py` | 模型调用前注入图片块 |
| `agents/middlewares/tool_output_budget_middleware.py` | 超大结果外部化/降级截断 |
| `agents/middlewares/tool_output_synopsis.py` | JSON/CSV/XML/YAML/code/text 确定性摘要 |
| `tools/builtins/list_uploaded_files_tool.py` | 查询历史上传 |
| `tools/builtins/invoke_acp_agent_tool.py` | 启动 ACP 子进程并收集流式响应 |
| `tools/builtins/clarification_tool.py` | 结构化澄清表单协议 |
| `community/browser_automation/` | Playwright 会话与浏览器工具 |
| `community/browserless/` | Browserless capture/screenshot |

## 3. 模型工厂的完整构造流程

`main:create_chat_model()` 的执行顺序很重要：

```text
选择模型名
  -> AppConfig.get_model_config(name)
  -> resolve_class(use, BaseChatModel)
  -> model_dump(exclude presentation fields)
  -> 合并 model_overrides（忽略 None）
  -> thinking enabled/disabled 变换
  -> reasoning_effort 能力过滤
  -> api_base -> base_url 规范化
  -> stream_chunk_timeout 默认值
  -> Codex/MindIE Provider 特化
  -> stream_usage 默认开启
  -> 未知参数告警
  -> model_class(**kwargs, **settings)
  -> 可选 tracing callbacks
```

顺序不能随意调整。例如 `model_overrides` 必须先于 thinking/Codex 变换，否则调用者覆盖的 `max_tokens` 可能绕过 Codex 的删除逻辑。

### 3.1 `model_overrides`

`main` 为 `create_chat_model` 增加 `model_overrides: dict | None`：

- per-agent 的 temperature/max_tokens 可以覆盖 profile；
- 值为 `None` 的项不覆盖已有值；
- Provider-specific 规范化仍处理覆盖后的值；
- presentation-only 的 `pricing`、display fields 不进入客户端构造器。

移植前 `dev` 工厂没有这个参数。本轮没有照搬任意字典合并，而是只接受
`temperature` 和规范化后的 `max_tokens`：拒绝未知字段、`None`、布尔值和越界值；
`max_tokens` 只映射到 Provider 明确声明的 `max_tokens` 或 `max_output_tokens`。覆盖顺序为
request 参数、admitted exact Agent version、global model profile，运行中不重读可变 Agent 定义。

### 3.2 endpoint 规范化

问题来源：

- `ModelConfig` 允许 extra keys；
- 用户常把其他 Provider 的 `api_base` 写到 `ChatOpenAI` 子类；
- LangChain 可能把未知 key 塞进 `model_kwargs`；
- 到真正 API 请求时才报 unexpected keyword；
- endpoint 覆盖本身还会失效。

`main` 的解决方式：

```text
issubclass(model_class, BaseChatOpenAI)?
  no  -> 不处理
  yes -> 类自己声明 api_base?
           yes -> 保留（例如 ChatDeepSeek）
           no  -> 若已有 base_url/openai_api_base，丢弃冲突 api_base 并告警
                  否则 api_base 重命名为 base_url
```

这优于移植前 `dev` 的 `_OPENAI_COMPAT_USE_PATHS` 白名单。白名单会漏掉后续加入的
`BaseChatOpenAI` 子类，正是 `main` 修复二次扩展的原因。

### 3.3 未知参数告警

`_warn_unknown_model_settings()`：

- 只对 `BaseChatOpenAI` 家族生效，避免用 OpenAI 字段表误判 Anthropic；
- 读取 Pydantic `model_fields` 和每个 field alias；
- 加入工厂合法注入的 `model_kwargs`、`extra_body`、headers/query、stream fields 等；
- 对差集记录明确告警；
- 不阻断构造，保留兼容性。

这是“提前暴露配置拼写错误”，而不是强制 schema 收紧。移植时应保证日志不包含 secret 值，只输出 key 名。

### 3.4 stream chunk timeout

`main` 默认值为 240 秒，适用于长推理模型首 token 或 chunk 间长暂停：

- `BaseChatOpenAI` 子类：显式配置优先，未配置才注入 240；
- 非 OpenAI-compatible：主动删除 `stream_chunk_timeout`，避免传给不认识的构造器；
- 判定同样用 `issubclass`，而不是 class path。

移植前 `dev` 仍按 `_OPENAI_COMPAT_USE_PATHS` 决定，遗漏自定义子类的风险仍在。本轮已改为
类型判断，覆盖测试在 `test_model_factory.py`、`test_agent_version_model_settings.py` 和
`test_lead_agent_model_resolution.py`。

### 3.5 thinking 与 Provider 特化

`main` 同时兼容三类禁用方式：

- OpenAI-compatible gateway：`extra_body.thinking.type = disabled`，并把 reasoning effort 降到 minimal；
- vLLM/Qwen：把 legacy `thinking` 映射/禁用为 `chat_template_kwargs.enable_thinking`；
- Anthropic native：直接构造 `thinking={"type": "disabled"}`。

Codex 模型：

- 删除 endpoint 不接受的 `max_tokens`；
- 非 thinking 时 `reasoning_effort=none`；
- thinking 时接受 low/medium/high/xhigh；
- 没有显式值则 medium。

MindIE：

- 限制默认 retry，避免 timeout 级联；
- timeout 的最终规范化由 `MindIEChatModel` 自己负责。

### 3.6 tracing 附着位置

`attach_tracing=True` 只适合图外直接调用。Lead Agent 或 Title middleware 已在 graph root 附着 tracing 时必须传 `False`，否则同一次模型调用生成两个 span，且 session/user metadata 可能落不到正确 root。

在 `dev` 中还应把 Project、Owner、Run ID 放进根 invocation metadata；不能依赖模型实例的全局 callback 推断权限上下文。

## 4. vLLM Provider

### 4.1 reasoning 保真

`VllmChatModel` 继承 `ChatOpenAI`，处理标准适配器会丢失的非标准 `reasoning`：

- 非流式 response；
- 流式 delta；
- 多轮请求中 assistant 的历史 reasoning；
- reasoning 与 tool call 交错。

它在 payload 发送前还把 DeerFlow 旧配置的 `thinking` 转为 vLLM/Qwen 使用的 `enable_thinking`，使 flash/non-thinking 模式真正关闭推理。

### 4.2 累计 usage 转增量

某些 vLLM 版本在每个 stream chunk 返回“截至当前累计用量”。LangChain 会把 chunk usage 相加，于是重复计费。

`main` 新增：

- `cumulative_stream_usage` 配置开关；
- completion id -> 上次 UsageMetadata 的有序缓存；
- 线程锁；
- 当前累计值减上次累计值，得到本 chunk delta；
- terminal chunk 清理；
- 最大 1024 项；
- 空闲 1 小时清理。

示意：

```text
chunk1 usage=100 -> emit 100, remember 100
chunk2 usage=130 -> emit 30,  remember 130
chunk3 usage=160 -> emit 30,  terminal cleanup
```

缓存必须按 completion id 隔离并加锁，因为一个模型实例可能承载并发流。

移植前 `dev` 尚无这套实现。本轮 delta 继续以 LangChain `UsageMetadata` 输出，复用既有
Run token accounting；当前 quota 模型没有 token 维度，不能把统计误写成已经具备 token quota。

## 5. MindIE Provider

MindIE 的 tool response 会被包装进类似 XML 的边界。若模型/工具内容含 `</tool_response>`，可能提前闭合边界并污染后续提示。

`main` 在 `_fix_messages` 中转义该终止串，再生成 Provider payload。这是典型 prompt-structure injection 修复，低耦合，可直接移植并保留 `backend/tests/test_mindie_provider.py`。

注意：这里只保护 MindIE wire format，不替代通用 ToolResultSanitization，也不等于授权校验。

## 6. 工具输出预算

### 6.1 中间件行为

`ToolOutputBudgetMiddleware` 只处理可安全转成纯文本的 `ToolMessage`：

- string；
- 只含 string/text block 的 list；
- multimodal 或未知结构跳过。

对超过阈值的结果：

1. 解析当前 thread outputs path；
2. 从状态中读取已有 `sandbox_id`，不会为每个工具结果新 acquire；
3. Host mount 可见时写 Host outputs；
4. 远端 Sandbox 时在 `/mnt/user-data/outputs/{storage_subdir}` 创建并写入；
5. 写后用 `test -s` 验证可读；
6. 返回带虚拟路径的结构化 synopsis；
7. 持久化失败则用严格 `fallback_max_chars` 的 head+tail 截断。

文件名由 sanitized tool name + 随机短 ID 构成；storage subdir 拒绝绝对路径和 `..`。

### 6.2 确定性 synopsis

`tool_output_synopsis.py` 不调用 LLM，而是检测并摘要：

- JSON：key、shape、有限深度；
- CSV/TSV：列和采样行；
- XML；
- YAML（结构解析仅限 500,000 字符以内）；
- code：imports、class/function symbols；
- text：标题/段落和摘录；
- binary-like/unknown。

安全预算：

- 输入超过 5,000,000 bytes 时不做完整 parse；
- 改用 raw head/tail sample；
- XML 只在安装 `defusedxml` 时做结构解析；
- synopsis 的结构遍历与渲染结果有深度、条数和字符上限。

这比简单“头尾各截一半”更利于模型理解结果，但不是内容可信化；untrusted tags 仍需单独中和。

### 6.3 与 `dev` 的冲突

`dev` 已有工具输出外部化，但私有资源不能使用 `main` 的裸 Host path 写入：

- 写入应通过 `PrivateSandboxFileProjection` 或 FileRepository；
- 对最终提交生成受 Project/Owner/Thread/Run 约束的 File/Artifact 记录；
- 每次读取重验 capability；
- 计入文件 quota、audit 与 retention；
- snapshot 只保存引用，不保存 storage locator。

因此 synopsis 算法可直接移植，存储通道必须重写。

## 7. `view_image`

### 7.1 `main` 的改进

`main:view_image_tool` 允许 jpg/jpeg/png/webp/gif，并做：

- 只允许 `/mnt/user-data/{workspace,uploads,outputs}`；
- 虚拟路径和本地路径校验；
- 必须存在且是普通文件；
- 最大 20 MiB；
- 扩展名白名单；
- magic bytes 检测；
- magic MIME 必须与扩展一致；
- `stat().st_size` 与实际读取长度一致，否则判定读取期间变化；
- 错误信息遮罩本地路径。

关键变化是 state 不再保存 base64，只保存：

```text
{mime_type, size, actual_path}
```

`ViewImageMiddleware` 在真正调用模型前按需读取。这样避免每个 checkpoint 重复携带几十 MiB base64。

### 7.2 移植前 `dev` 的问题

移植前 `dev` 的 `ViewedImageData` 保存 `{base64, mime_type}`，`view_image_tool` 直接 base64
编码，checkpoint 会放大。

但 `main` 的 `actual_path` 方案也不能原样用于 `dev`：

- Host locator 不应进入 durable state；
- Worker 重启/换机后路径可能失效；
- 路径可能泄漏部署结构；
- 模型前按需读取仍要复核 Project/Owner capability。

本轮可移植落点：

```text
view_image tool
  -> 校验虚拟路径、Run/Sandbox/Project/Owner 和内容签名
  -> state 保存 Run-bound sandbox reference + mime + size + sha256
  -> before_model 重新校验 authority
  -> secure regular-file reader 重新读取
  -> 仅在本次模型请求内构造 data/image block
```

GIF magic 支持和 stat/read 一致性检查可直接吸收。

## 8. 历史上传查询

`main:list_uploaded_files`：

- 从 runtime context、RunnableConfig 或 LangGraph config 解析 thread id；
- 解析 effective user id；
- 扫描用户/线程 uploads 目录；
- 排除当前 Run 的 `uploaded_files`；
- 忽略 symlink、staging 文件；
- 隐藏同 stem 的转换 `.md`；
- 按 mtime 倒序；
- 默认 20、最大 100；
- 可为全部或指定文件提取 outline；
- 对遗漏项按扩展名聚合摘要；
- 文件名、路径、outline 都经过 untrusted tag neutralization。

`main` 还把注入的 runtime 参数从模型可见 schema 隐藏，防止模型伪造运行时上下文。

移植前 `dev` 没有该工具。本轮没有在工具调用时直接分页查询
`FileRepository.list_versions(...)`，而是读取已由 PostgreSQL 恢复到当前私有 Run 的
authority manifest：

```text
scope + thread
  -> PrivateRunFileAuthority.visible_uploads()
  -> 排除本 Run admitted uploads
  -> display_name.casefold() + file_version_id 稳定排序
  -> 返回 file_id/version_id、显示名、大小、MIME
```

不要恢复 Host 目录扫描，也不要返回真实 storage locator。

## 9. ACP Agent 工具

`build_invoke_acp_agent_tool()` 动态把配置的 agent 名和描述写进 tool description。调用链：

```text
校验 agent name
  -> 导入 ACP SDK
  -> 创建 per-user/thread ACP workspace
  -> 转换已启用 MCP server 到 ACP wire format
  -> 解析 agent env
  -> spawn_agent_process
  -> initialize
  -> new_session(cwd, mcp_servers, optional model)
  -> wait_for(conn.prompt, timeout_seconds)
  -> 收集 session_update 文本
  -> 上下文退出时结束子进程
```

permission request 默认 cancel；仅配置 `auto_approve_permissions` 后选择 allow_once，次选 allow_always。命令不存在时返回可操作提示，尤其区分 `codex` CLI 与 `codex-acp` adapter。

`main` 的 timeout 修复确保无响应子进程被上下文结束并返回明确错误。该修复可移植。

但 `dev` 应额外保证：

- ACP workspace 绑定 Project/Owner/Run；
- 子进程环境只含 admitted Credential；
- MCP 列表来自 admitted snapshot，不是运行中全局 extensions 文件；
- timeout/cancel 后回收进程组；
- 文件结果写回私有 FileRepository。

## 10. 结构化澄清工具

`ClarificationFormField` 支持：

- `text`、`textarea`、`number`；
- `select`、`multi_select`；
- `checkbox`、`date`；
- required/options/placeholder。

`ask_clarification_tool` 自身是 placeholder；`ClarificationMiddleware` 在工具真正执行前拦截并 interrupt graph。运行时限制：

- 最多 16 fields；
- 每 field 最多 24 options；
- 名称/标签/选项/placeholder 最多 200 字符；
- 禁止 JavaScript 原型污染类名称；
- 非法表单整体降级为纯文本问题。

这是协议和 UI 能力，不应与 Agent definition 的 input schema 混为一谈。详见前端和 Agent 文档。

## 11. Browserless

`main` 对 Browserless 的改进包括：

- `timeout` 与旧 `timeout_s` 别名；
- `_coerce_timeout` 处理非法数值和边界；
- capture/screenshot 统一 timeout；
- target status 处理；
- Serper 图片 URL 先验证 public URL，丢弃 malformed/private target。

移植前 `dev` 已有部分 target/status 和 URL 安全逻辑，但 timeout 解析仍不完整。本轮以最终
文件逐项对照，保留 `timeout_s` 为主键、`timeout` 为兼容 alias。布尔值直接回退默认值；
非数值、非有限、非正或超过 3600 秒的值会记录不含 secret 的 warning 后回退。

## 12. Playwright Browser Automation

### 12.1 `main` 的具体设计

`BrowserSessionManager` 使用专用 daemon event-loop thread，因为 Playwright 对创建它的 event loop 有亲和性。它维护：

- thread id -> browser session；
- 每个 session 的 page/context/browser；
- idle timestamp；
- 最大 32 个 session；
- idle eviction；
- 单个 live viewer；
- session snapshot 和 screenshot。

工具包括 navigate、snapshot、click、type 等。snapshot 给 DOM 可交互节点写 `data-df-ref`，后续点击/输入通过 ref 定位，避免模型直接拼复杂 selector。

每次 URL 导航都经过 SSRF screening。若配置外部 `cdp_url`，只有显式 `allow_unguarded_cdp: true` 才允许，因为 DeerFlow 无法给已经附着的浏览器强制注入请求拦截。

`browser_capability()` 还检查：

- 是否配置 `browser_navigate`；
- Playwright 包是否存在；
- multi-worker 是否安全；
- CDP 是否显式接受无防护风险。

### 12.2 multi-worker 约束

`browser_multi_worker_error()` 在 Gateway 多 worker 时拒绝启动该能力。原因：

- session 只存在创建它的进程内；
- viewer 和后续工具请求可能落到另一个 Gateway；
- Playwright 对事件循环又有亲和性；
- 没有共享 session registry/lease。

### 12.3 为什么不能合入 `dev`

`dev` 的图只在 Worker 执行。即使 Gateway 单 worker：

- Run A 的浏览器 session 在 Worker 1；
- 下一次 Run/Job 可能由 Worker 2 claim；
- Gateway 的 viewer API 也不拥有 Playwright page；
- Worker 崩溃后无 durable owner；
- session 资源未绑定 Project/Owner/Run lease。

需要的新架构至少包括：

```text
PostgreSQL BrowserSession row
  + project/owner/thread
  + owner_worker_id
  + lease_expires_at / fencing token
  + viewer authorization
  + bounded command queue
  + Worker-side Playwright event loop
```

或者将浏览器能力做成独立有租约的服务。原样合并 `community/browser_automation` 会产生功能偶发失联和跨租户风险。

## 13. `tool_search` 的特殊差异

`main` 曾移除一次选择数量上限，让模型可提升任意多 deferred tools。这个变化不适合直接移植到 `dev`：

- `dev` 的工具集合来自 admitted Agent/Skill/MCP snapshot；
- 工具数量影响 prompt、schema、调用面和审计；
- 应保留 bounded selection 与可解释的提升策略；
- 不能让运行中搜索绕过 admission closure。

搜索质量修复可以移植，取消上限不建议。

## 14. 风险与移植矩阵

| `main` 实现 | 价值 | `dev` 处理 |
| --- | --- | --- |
| `issubclass(BaseChatOpenAI)` endpoint/timeout | 高 | 直接移植 |
| unknown model settings warning | 高 | 直接移植，只打印 key |
| `model_overrides` | 中高 | 从 admitted Agent snapshot 传入 |
| vLLM cumulative usage delta | 高 | 移植为 `UsageMetadata`，复用既有 Run token accounting；不宣称 project token quota |
| MindIE terminator escape | 高 | 直接移植 |
| deterministic tool synopsis | 高 | 算法移植 |
| Host/Sandbox output externalize | 中 | 改写为 `PrivateRunFileAuthority.write_internal` → workspace → finalizer/FileRepository |
| `view_image` 不存 base64 | 高 | 改成 Run-bound sandbox ref，不存 Host actual path |
| historical upload tool | 高 | 改写为 PostgreSQL 恢复的 authority manifest 查询 |
| ACP timeout | 高 | 移植通用进程生命周期；私有 bridge 延后 |
| Browserless timeout alias | 中 | 对照当前代码补齐 |
| Playwright session manager | 产品价值高 | 架构重做，不直接合 |
| unlimited `tool_search` select | 风险高 | 不采用 |

## 15. 验证范围

### 15.1 已验证

当前 checkout 的直接测试至少覆盖：

- `backend/tests/test_model_factory.py`
- `backend/tests/test_agent_version_model_settings.py`
- `backend/tests/test_lead_agent_model_resolution.py`
- `backend/tests/test_vllm_provider.py`
- `backend/tests/test_mindie_provider.py`
- `backend/tests/test_tool_output_budget_middleware.py`
- `backend/tests/test_tool_output_truncation.py`
- `backend/tests/test_tool_output_synopsis.py`
- `backend/tests/test_view_image_tool.py`
- `backend/tests/test_view_image_middleware.py`
- `backend/tests/test_view_image_checkpoint_slimming.py`
- `backend/tests/test_list_uploaded_files_tool.py`
- `backend/tests/test_private_sandbox_files.py`
- `backend/tests/test_private_output_authority.py`
- `backend/tests/test_private_file_finalizer.py`
- `backend/tests/test_invoke_acp_agent_tool.py`
- `backend/tests/test_browserless_client.py`
- `backend/tests/test_tool_search.py`
- `backend/tests/test_deferred_tool_crosscontext.py`
- `backend/tests/test_serialization.py`
- `backend/tests/test_asset_audit_sink.py`
- `frontend/tests/unit/components/admin/admin-assets.test.tsx`
- `frontend/tests/e2e/admin-assets.spec.ts`

自动化已验证：自定义 `BaseChatOpenAI` 子类的 endpoint/timeout、unknown-setting 日志脱敏、
vLLM 并发隔离、MindIE 转义、GIF/checkpoint slimming、图片重授权、Browserless、ACP POSIX
生命周期、公开流预算、私有 ToolResult 写入/清理/finalization、聚合审计与前端展示。真实浏览器
另验证 DeepSeek 多轮调用、当前与历史上传、刷新恢复、线程隔离、`grep` 超大结果外置和刷新后
读取。精确数字与 Run 证据见第 20 节及证据目录。

### 15.2 明确延后或不做集成宣称

- process-local Playwright browser automation 本轮不迁入，因此没有把它写成浏览器验收通过；
- vLLM、MindIE、GIF、Browserless、ACP 由自动化测试覆盖，没有冒充真实 Provider/浏览器端到端；
- Windows ACP descendant process tree 仍需 Job Object 或等价机制；
- 私有 ACP asset bridge 未启用，`include_acp=False`；
- quota 已覆盖文件提交，本轮没有新增 token quota。

## 16. 关键 `main` 演进

- `3e7baba3`：把 streaming timeout 覆盖扩到所有 `BaseChatOpenAI` 子类；
- `e2816eaa`：类型判断替代 class-path allowlist；
- `713ee544`：图片不再把 base64 存入 checkpoint；
- `756eac0d`：结构化 tool-output synopsis；
- `ae223199`：MindIE tool response 终止串转义；
- `16a77cb7`：Serper 图片 URL 防御；
- `fa496c0c`：Playwright browser automation；
- `e225ad57`：历史上传工具；
- `09d9cf53`：ACP timeout；
- `7b330101`：隐藏 injected runtime schema；
- `cd9432bc`：GIF 支持；
- `1baa8ad6`：澄清表单；
- `6456c356`：Browserless timeout；
- `94003c1f`：vLLM cumulative usage。

Firecrawl 的一组修改后来被 revert，最终净状态不应按已回退提交重复移植。

## 17. 本轮移植计划与执行顺序

本轮不按文件整包复制，而是按运行时落点分成六个连续阶段。每个阶段先补失败用例，
再改实现，并在进入下一阶段前跑对应聚焦测试。

| 阶段 | 状态 | 移植能力 | `dev` 落点 | 阶段验收 |
| --- | --- | --- | --- | --- |
| 15.1 | 完成 | OpenAI-compatible 类型判断、endpoint、未知参数和 stream timeout | `models/factory.py` | `BaseChatOpenAI` 子类构造与脱敏测试通过 |
| 15.2 | 完成 | vLLM cumulative usage、MindIE terminator escape | Provider adapter | 并发、terminal、取消、异常和转义测试通过 |
| 15.3 | 完成 | deterministic synopsis、超大 ToolResult 外置 | Tool middleware + 私有文件 authority | 硬预算、结构摘要、撤权/取消/清理 fail closed 通过 |
| 15.4 | 完成 | GIF image reference、历史上传查询 | Run-bound sandbox ref + PostgreSQL authority manifest | checkpoint slimming、重授权、线程隔离通过 |
| 15.5 | 完成 | Browserless 状态/timeout、ACP 全生命周期 timeout | Browserless client + ACP process boundary | status、非法 timeout、挂死 cleanup、POSIX 进程组回收通过 |
| 15.6 | 完成 | 完整门禁和真实浏览器多轮验收 | 隔离 `deerflow_test_*` 数据库 | 20 文件 PostgreSQL gate 0 skip；真实模型、刷新和截图通过 |

阶段之间的阻塞规则：

1. 私有文件写入若不能绑定当前 Project、Owner、Thread、Run，停止 15.3，不进入浏览器验收；
2. 任一外置 ToolResult 进入用户可见 outputs 却没有被声明为 Artifact，视为失败；
3. 固定 20 文件 PostgreSQL gate 出现 skip，或浏览器没有真实外部模型响应，不写“验收完成”；
4. 截图只在自动化门禁通过后采集，且不得出现密码、Credential、Host locator 或数据库 URL。

## 18. 实际移植落点

### 18.1 模型工厂

模型工厂已把 OpenAI-compatible 判定从 class-path 白名单改为
`issubclass(model_class, BaseChatOpenAI)`。因此后续新增 MiMo、MiniMax 或项目自定义
`BaseChatOpenAI` 子类时，不需要再同步维护一份字符串白名单。

迁入的行为包括：

- `api_base` 在没有冲突 endpoint 时规范化为 `base_url`；
- 已显式设置 `base_url`/`openai_api_base` 时保留显式 endpoint、丢弃重复 `api_base` 并记录
  不含配置值的 warning；
- 仅 OpenAI-compatible 类型得到默认 240 秒 stream chunk timeout；
- 未知参数告警只记录 key，不记录 value；
- Agent override 只允许 `temperature` 和规范化 `max_tokens`，request 参数优先于 admitted
  exact Agent version，后者优先于 global model profile；
- 运行时不回读可变 Agent 定义。

### 18.2 vLLM 与 MindIE

vLLM cumulative usage 已接入，但没有照搬 `main` 的软容量实现。本轮额外收紧为：

- 每个真实 stream invocation 有独立 scope；即使 Provider 并发复用 completion id 也不串账；
- `finish_reason` 帧、额外空 terminal 帧都能正确去重；
- stream 正常结束、Provider 异常和调用取消都会在 `finally` 清理 snapshot；
- active snapshot 与 terminal dedup snapshot 都有不可突破的硬容量；
- 容量压力下允许被淘汰流的下一帧按完整累计值报告，优先保证 Worker 内存有界。

真实 `_stream`/`_astream` invocation 使用 `ContextVar` scope 隔离复用 completion id；直接
调用 chunk converter 的兼容路径仍以 completion id 隔离。MindIE 在组装
`<tool_response>` 前以 `html.escape(..., quote=False)` 转义工具结果中的 `&`、`<`、`>`。
它只修复 Provider wire format 注入，不替代通用 ToolResult sanitization。

### 18.3 ToolResult synopsis 与私有文件写入

确定性 synopsis 算法已移植到预算中间件，但做了比 `main` 更严格的处理：

- 完整 preview 硬上限 12,000 字符；
- metadata 总量、单项、key、JSON path、工具名、虚拟路径和 raw sample 分别设限；
- 结构名先单行化并中和不可信标签；
- JSON/YAML/XML/CSV 只返回结构、类型和数量，不从文件中段抽取 scalar/cell；
- 原始内容只允许出现在配置批准且总量受限的 head/tail 窗口；
- 超过解析上限时跳过完整 parse。

公共/旧式调用仍保留原有 Host/Sandbox 兼容路径。私有 Run 则必须经过
`PrivateRunFileAuthority`：

```text
oversized ToolMessage
  -> private authority.write_internal(...)
  -> /mnt/user-data/workspace/.tool-results/<unique-name>
  -> sandbox atomic publish
  -> finalizer 按 Project + Owner + Thread + Run 提交 File version
  -> ToolMessage 只保存受限 synopsis 与虚拟路径
```

内部结果使用 workspace，而不是 outputs。这样它可被后续 `read_file` 使用，但不会绕过
Artifact presentation gate。写入前、每个不超过 1 MiB 的 append、publish 前后都会重验
authority。publish 前失败会 abort atomic handle；publish 正在阻塞时收到取消，会等待阻塞
调用返回，再删除已经发布的文件后传播 cancellation；publish 后最终授权检查失败也会删除文件。
删除最多重试 3 次，若仍不能确认清理，authority 被 poison，后续写入和 finalize 均 fail
closed，但 release 仍允许执行并清空状态。同步私有调用没有异步 authority 通道，因此在执行
工具 handler 前直接 fail closed，不会退回 Host 写盘。

`read_file` 和兼容名 `read_file_tool` 默认列在 `tool_output.exempt_tools`，避免读取已外置文件
后再次触发外置循环；它们自身受 `sandbox.read_file_output_max_chars` 约束，默认最多返回
50,000 字符。真实验收用 `grep` 产生 23,794 字符结果触发外置，写入
`/mnt/user-data/workspace/.tool-results/grep-06ac1acd5bff-5f8143e3b089.txt`；外置 Run 为
`eb9868db-2acb-4f93-b2ac-6dd6507eec39`，刷新后读取 Run 为
`7c6c381f-66be-42ab-b15a-e5b711ece31b`。

私有 `write_output` 仍用于 Browserless screenshot 等真正需要展示的结果，并显式登记
presented path。relative path 必须 canonical，拒绝绝对路径、`..`、反斜杠、空段和
`.deerflow*` 保留名。

### 18.4 图片与历史上传

`view_image` 已支持 GIF87a/GIF89a，并改为当前 Run 绑定的 sandbox reference：

- checkpoint 不保存 base64；
- durable state 不保存 Host `actual_path` 或 storage locator；
- 保存 virtual path、Sandbox ID、Run ID、私有 Project/Owner、MIME、size 和 SHA256；
- 模型调用前核对 Run、Sandbox、Project、Owner、size、SHA256 和 magic MIME，并通过 secure
  regular-file reader 重新授权、重新读取；
- 旧 Run、换 Sandbox、内容变化、撤权或读取取消均 fail closed，base64 只进入临时
  `ModelRequest`。

这个引用不是数据库 file-version id，也不承诺跨 Run、跨 Sandbox 或跨 Worker 恢复。

`list_uploaded_files` 没有扫描 Host thread 目录，而是读取当前私有 Run authority 已恢复的
PostgreSQL manifest：

- 结果只含 file version id、version、display name、size、media type；
- 不返回 logical/physical path 或 locator；
- 排除最新可见 HumanMessage 以及 authority 累积的当前 Run upload id；
- 普通 hidden framework HumanMessage 不会错误切断用户边界，但 human-input response 仍是
  真实用户边界；
- filename filter 精确匹配，limit 为 1–100；
- 排序为 `display_name.casefold()` 加 file version id；
- 输出先中和不可信标签；
- Project、Owner、Thread 任一不匹配都不可见；
- runtime 注入参数不出现在模型可见 tool schema。

前端上传 POST response 先经过 strict Zod schema，`id` 必须是 UUID；转换为最终和 optimistic
消息附件时再次拒绝缺失或非法 id，并始终携带 `file_id`。当前 upload id 同时写入
`RunFileAuthority`，所以 Subagent 即使没有父 Agent HumanMessage，也不会把本轮上传误报成历史
上传。

### 18.4.1 公开流与异常边界

所有公开 stream mode（`messages`、`values`、`updates`、`debug`、`tasks`、
`checkpoints`、`custom`）递归投影 `viewed_images`，API/SSE 只保留 MIME、size、SHA256，
移除 file reference、Sandbox/Run/Project/Owner coordinate、legacy base64 和 Host path。
hidden message 中大小写变体的 `data:` image URL 同样移除。

Serializer 支持 tuple、Pydantic `model_dump`、循环和共享 DAG，并对公开 payload 设置：

- 最大深度 128；
- 最多 10,000 个节点；
- 单个 collection 最多 10,000 项；
- 单字符串最多 1,000,000 字符；
- 所有字符串合计最多 4,000,000 字符；
- 单个 key 最多 1,024 字符；
- 非字符串 key 丢弃，key 截断碰撞保留首值；
- `NaN`/`Infinity` 投影为 `null`；
- `messages` 始终保持 `[chunk, metadata]` 双元素 envelope。

预算耗尽时停止容器遍历，不生成海量 `None` 尾；循环引用降级为 `None`。未分类 Worker 异常
统一公开为 `RUN_EXECUTION_FAILED`，不回显异常文本或 traceback。只有三个闭集
`PublicRunErrorCode` 能返回预审的稳定公开消息，调用者不能用任意字符串构造公开错误。

两个已知 P2 不影响当前公开生产路径的最终输出上界：独立导出的
`strip_data_url_image_blocks()` 本身没有遍历预算，但生产公共路径统一走 `serialize()`；
第三方对象的 `model_dump()`、`dict()` 或 `__str__()` 在裁剪前执行，最终输出仍有界，但这些
受信任进程内 hook 自身的执行成本尚未被限制。

### 18.5 Browserless 与 ACP

Browserless client 已统一 `timeout_s` 与兼容 alias `timeout`。布尔值直接回退默认 30 秒；
非数值、非有限、非正值或超过 3600 秒不会让配置加载失败，而是 warning 后回退。Browserless
transport 非 200 是错误；目标页面 4xx/5xx 仍可返回内容或截图，但工具结果附加 warning，
不能把截图当作成功页面证据。私有 capture 通过 `write_output` 进入文件 authority 并登记
presented path，而不是直接写 Host。

ACP 只迁移通用 subprocess 安全修复，不在私有 Run 中启用 ambient ACP：

- timeout 是整个 `__aenter__`、initialize、new session、prompt 的总预算，request 默认
  1800 秒；
- request deadline 与 cleanup deadline 分离，cleanup 默认 5 秒、最大 30 秒；
- POSIX 生产启动路径使用无 shell 的 `setsid + exec` wrapper 创建独立进程组；
- wrapper 使用 `python -I -S`，阻断 `PYTHONPATH/sitecustomize` 在 `setsid()` 前注入；
- relative command/PATH 以 ACP workspace 为基准解析为绝对路径；
- timeout/cancel 按 TERM → KILL 回收进程组，取消完成有界清理后继续抛出；
- 成功结果只有在 `__aexit__`、`wait()` 和最终 `returncode` 都可验证后才返回，否则丢弃；
- 不合作的 lifecycle task 会被强引用 quarantine，未结束前拒绝后续 ACP 调用；
- 日志只记录 agent、command 和长度，不记录 prompt/result 正文；
- ambient extensions MCP 列表固定为空。

### 18.6 同事务文件审计

私有文件 finalization 新增 `run.files_finalized` 聚合审计。文件内容先通过独立事务分批写入
staging rows/chunks；最终 promotion transaction 原子完成旧 File quota release、staging File
promotion、删除标记、新 quota reserve、Artifact 创建、Run `finalization_status=complete` 和
audit append。审计失败时最终事务整组回滚，随后精确补偿删除本 Run staging rows 并把
finalization 标为 failed，不会出现“文件已提交但没有审计”。

审计 metadata 只允许：

- `created_count`
- `modified_count`
- `deleted_count`
- `artifact_count`
- `committed_bytes`

不允许 path、filename、display name、file/artifact/thread id、正文、Run output、locator、
SHA256 或异常文本。该 action 仅允许受信任 Worker process 写入；`AuditHmacKeyring` 根据
RUN target 的 `authority_id` 生成 target-reference HMAC，审计存储不暴露原始 target ID。
该 action 复用现有 `audit_logs` schema，本模块不新增 schema object 或 marker。
前端使用 strict schema，并提供中英文聚合展示。

## 19. 明确不移植

### 19.1 process-local Playwright session

`main` 的 BrowserSessionManager 仍不合入。它把 page/context/browser 放在单进程 daemon
event loop，无法满足 `dev` 的 Worker claim、lease/fencing、跨 Worker 恢复和 Gateway
viewer authorization。Browserless 无状态 capture 可用，不代表有状态 Playwright 已移植。

### 19.2 unlimited `tool_search`

不迁入取消 deferred tool 选择上限的改动。私有 Run 的工具面由 admitted
Agent/Skill/MCP snapshot 冻结；搜索只能在该闭包内有界提升，不能运行中扩大能力集合。

### 19.3 ACP 私有资产桥接

本轮没有把 ACP workspace、MCP 或 Credential 接到私有 Run。当前私有图仍
`include_acp=False`。只有在 ACP workspace 能绑定 Project/Owner/Run、Credential 来自精确
snapshot 且结果能通过 FileRepository 后，才可单独开启。

### 19.4 Windows ACP descendant process tree

POSIX 目标已经通过独立进程组完成 TERM → KILL 回收；非 POSIX 路径目前只能保证直接 child
的 terminate/kill。Windows 上 Node、cmd 等 launcher 的后代进程树仍需 Job Object 或等价
机制，并需在真实 Windows 目标环境验证，因此本轮明确延后。

## 20. 验收记录

验收基线为 `dev@785be51341c1`，工作树包含本轮未提交修改：

- 模块聚焦测试：`633 passed, 17 skipped`；17 项均为无数据库环境下的
  `test_private_file_finalizer.py`，随后连接隔离真实 PostgreSQL 单独运行该文件得到
  `25 passed, 0 skipped`；
- 后端全量：`7841 passed, 1021 skipped, 0 failed`；全量中的既有环境/可选集成 skip 不替代
  强制数据库门禁；
- 固定 20 文件 M1–M7 真实 PostgreSQL gate：`275 passed, 0 skipped, 0 failed`；
- Ruff：1151 个 Python 文件格式与 lint 通过；
- 前端单元测试：188 个文件、`1349 passed, 0 skipped`；
- `pnpm check`、production build、static build 均通过，两个 build 都完成 78/78 页面；
- 正确使用测试专用 webServer 启动的完整 Chromium E2E：`109 passed`。

真实浏览器验收使用隔离数据库 `deerflow_test_m15_acceptance_20260730`、真实
`deepseek-v4 / DeepSeek V4 Pro`，共完成 9 个 Run、20 次外部 LLM 调用。6 个验收步骤覆盖当前
上传读取、历史上传列表、刷新恢复、新线程隔离、`grep` 超大 ToolResult 外置以及再次刷新后
读取；全部 Run 最终 `finalization_status=complete`。3 个探索性 probe 如实排除在验收通过数
之外：`read_file` 默认豁免外置、LocalSandbox host bash 未开放、`web_fetch` 在中间件前已内部
截断，均未被误报成外置能力成功。

允许存在一般测试套件中有明确原因的 skip；只有固定 20 文件 PostgreSQL gate 与本轮必需验收
用例要求 0 skip。完整命令、Run ID、截图哈希、脱敏检查、隔离数据库清理和服务恢复记录见
`evidence/15-tools-models/README.md`。
