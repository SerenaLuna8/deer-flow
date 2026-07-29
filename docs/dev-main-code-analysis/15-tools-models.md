# 15. Tools / Models 模块：`main` 具体实现与 `dev` 对照

## 1. 范围与总判断

本文件只分析模型适配层和通用工具层。Agent 编排见 `01-agent.md`，MCP 见 `12-mcp.md`，Sandbox 文件权限见 `13-sandbox.md`。

`main@e317f7b8` 在这一模块有四类值得关注的实现：

1. 模型工厂从“类路径白名单”改为“类型能力判断”，修复 OpenAI-compatible 子类的 endpoint、stream timeout 与未知参数问题；
2. vLLM/MindIE 对推理内容、用量、转义和超时做 Provider 级兼容；
3. 工具输出、图片、历史上传和 ACP 调用更完整；
4. 新增进程内 Playwright 浏览器会话，但其 Gateway 亲和设计与 `dev` 的 Worker-only 执行不兼容。

结论：

- 模型工厂、MindIE 转义、vLLM 累计用量转增量、ACP timeout、Browserless timeout 等是低耦合修复，可以逐项移植。
- `main` 的 `view_image` 虽解决 checkpoint 体积问题，却把 Host `actual_path` 放进状态；在 `dev` 的私有文件模型中仍不合格，需改为授权后的 opaque file reference。
- `list_uploaded_files` 的产品能力有价值，但旧实现扫描 Host thread 目录；`dev` 必须查询 PostgreSQL 私有文件仓库。
- Browser automation 必须重新设计 session owner/lease，不能原样合入 Worker-only 运行时。

## 2. 源码地图

### 2.1 Models

| `main` 文件 | 作用 |
| --- | --- |
| `backend/packages/harness/deerflow/models/factory.py` | 模型配置解析、thinking 变换、Provider 构造、tracing |
| `models/model_config.py`（配置目录中） | 模型声明和 Provider 特性元数据 |
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

`dev` 当前工厂没有这个参数。若 `dev` 的数据库 Agent 快照已经保存模型采样配置，应在 admitted snapshot 形成时冻结覆盖值，再传入工厂，不能运行中重读可变 Agent 定义。

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

这优于 `dev` 当前 `_OPENAI_COMPAT_USE_PATHS` 白名单。白名单会漏掉后续加入的 `BaseChatOpenAI` 子类，正是 `main` 修复二次扩展的原因。

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

`dev` 仍按 `_OPENAI_COMPAT_USE_PATHS` 决定，遗漏自定义子类的风险仍在。该修复可连同 `backend/tests/test_model_factory.py` 和 `test_agent_model_settings.py` 移植。

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

`dev` 尚无这套实现。移植后，delta 应继续进入 `dev` 的 Run token usage / per-model / project statistics；当前 quota 模型没有 token 维度，不能把统计误写成已经具备 token quota。

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
- YAML；
- code：imports、class/function symbols；
- text：标题/段落和摘录；
- binary-like/unknown。

安全预算：

- 输入超过 5,000,000 bytes 时不做完整 parse；
- 改用 raw head/tail sample；
- XML 优先 `defusedxml`；
- YAML/XML/结构遍历有深度与条数上限。

这比简单“头尾各截一半”更利于模型理解结果，但不是内容可信化；untrusted tags 仍需单独中和。

### 6.3 与 `dev` 的冲突

`dev` 已有工具输出外部化，但私有资源不能使用 `main` 的裸 Host path 写入：

- 写入应通过 `PrivateSandboxFileProjection` 或 FileRepository；
- 生成的是 opaque file/artifact id；
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

### 7.2 `dev` 当前问题

`dev` 的 `ViewedImageData` 仍保存 `{base64, mime_type}`，`view_image_tool` 直接 base64 编码，checkpoint 会放大。

但 `main` 的 `actual_path` 方案也不能原样用于 `dev`：

- Host locator 不应进入 durable state；
- Worker 重启/换机后路径可能失效；
- 路径可能泄漏部署结构；
- 模型前按需读取仍要复核 Project/Owner capability。

正确目标：

```text
view_image tool
  -> 校验私有 file_id 和内容签名
  -> state 保存 opaque file_version_id + mime + size
  -> before_model 重新校验 authority
  -> FileRepository 流式读取
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

`dev` 没有该工具。应实现数据库版本：

```text
scope + thread
  -> FileRepository.list_versions(...)
  -> 排除本 Run admitted uploads
  -> 稳定 created_at/id 分页
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

`dev` 已有部分 target/status 和 URL 安全逻辑，但工具配置仍主要读取 `timeout_s` 并直接 `float(raw)`。应以最终文件逐项 cherry-pick，而不是根据提交标题重复实现已经存在的部分。

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
| vLLM cumulative usage delta | 高 | 移植并接 Run/project 统计 |
| MindIE terminator escape | 高 | 直接移植 |
| deterministic tool synopsis | 高 | 算法移植 |
| Host/Sandbox output externalize | 中 | 改写为私有 FileRepository |
| `view_image` 不存 base64 | 高 | 改成 opaque ref，不存 actual path |
| historical upload tool | 高 | 改写为 DB 查询 |
| ACP timeout | 高 | 移植并补私有 workspace/credential |
| Browserless timeout alias | 中 | 对照当前代码补齐 |
| Playwright session manager | 产品价值高 | 架构重做，不直接合 |
| unlimited `tool_search` select | 风险高 | 不采用 |

## 15. 测试证据

`main` 的直接测试：

- `backend/tests/test_model_factory.py`
- `backend/tests/test_agent_model_settings.py`
- `backend/tests/test_vllm_provider.py`
- `backend/tests/test_mindie_provider.py`
- `backend/tests/test_tool_output_budget_middleware.py`
- `backend/tests/test_tool_output_truncation.py`
- `backend/tests/test_view_image_tool.py`
- `backend/tests/test_view_image_middleware.py`
- `backend/tests/test_list_uploaded_files_tool.py`
- `backend/tests/test_invoke_acp_agent_tool.py`
- `backend/tests/test_browserless_client.py`
- `backend/tests/test_browser_automation.py`
- `backend/tests/test_browser_router.py`
- `backend/tests/test_tool_search.py`
- `backend/tests/test_deferred_tool_crosscontext.py`

面向 `dev` 还需验证：

- 自定义 `BaseChatOpenAI` 子类不在白名单也得到 timeout/base URL 处理；
- unknown setting 日志不含值；
- 同模型并发 completion usage 不串；
- file/image state 不含 base64、Host path 或 storage locator；
- capability 被撤销后不能通过历史图片引用再次读取；
- ACP/Browser session lease 丢失时副作用立即 fail closed；
- deferred tool promotion 不超过 admitted snapshot；
- Tool output 外部化计入 quota/audit/retention。

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
