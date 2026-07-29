# 11. Security 模块：`main` 最终实现、提交演进与 `dev` 精确落点

## 1. 分析范围与结论

本文把 Security 定义为贯穿模型边界、工具边界、身份/授权边界和秘密边界的横切模块。它不重复 Agent、Skill、MCP、Sandbox 各自的业务流程，只分析这些流程共同依赖的安全原语。对比基线为：

- 公共祖先：`3be3969f8fc3f2d2b6d36ef5c26fa5593d916f2a`
- `main`：`e317f7b8`
- `dev`：`8a91e957`

结论先行：

1. `main` 在公共祖先之后完成了四类可独立移植的纵深防御：完整 framework-tag 输入净化、远程工具结果净化、所有结构化 prompt sink 的上下文转义、Guardrail 空 allowlist 的 fail-closed 修复。
2. `dev` 的平台安全架构远强于 `main`：认证身份生成不可伪造的 `ProjectContext` / `PrivateWorkContext`，所有私有数据绑定 `project_id + owner_user_id`，Run 的每个副作用边界重新校验当前权限，Credential 使用 AEAD envelope 且只在 Worker 命令边界解密。
3. 这两类能力不是替代关系。`dev` 的数据库隔离不能阻止 prompt 结构逃逸；`main` 的字符串净化也不能替代项目授权、版本闭包、租约和 Credential revalidation。
4. 已由最终代码差异确认的 `dev` 回归包括：输入 denylist 缩减且未覆盖当前 authority blocks；上传 middleware 注入的可信 `<current_uploads>` 会被整体净化；`AllowlistProvider(allowed_tools=[])` 被解释为“未配置”而放行全部工具；`web_capture` 结果不再净化；Agent v2 文档与 `describe_skill` metadata 未转义。
5. 不能直接把 `main` 的旧认证/用户模型、文件型秘密、全局运行上下文或 middleware 排序整体覆盖进 `dev`。正确策略是在 `dev` 的 Worker-only、exact snapshot、project/owner revalidation 架构内移植局部防御。

## 2. 信任边界总览

安全数据流应按来源分类，而不是按“是否已经是字符串”分类：

| 数据来源 | 信任级别 | 进入位置 | 应用控制 |
| --- | --- | --- | --- |
| 浏览器/IM 用户文本 | 不可信 | `HumanMessage` | 输入净化、边界包裹、原文旁路字段 |
| 上传文件名/描述 | 不可信 | `<current_uploads>` 内的数据字段 | 在构造可信 wrapper 前转义；不能再净化 wrapper 本身 |
| Web/MCP 远程结果 | 不可信 | `ToolMessage` / `Command.update.messages` | provenance 驱动的结果净化 |
| Agent/Skill/Memory 项目内容 | 项目可配置但对平台不可信 | SystemMessage 结构块 | assembly-site XML/HTML 转义 |
| 平台模板和固定标签 | 可信 | system prompt / hidden context | 不允许用户伪造相同结构 |
| 认证、项目、owner、capability、lease | 仅服务端可信 | runtime context | 丢弃客户端同名字段，使用 issued context |
| Credential 明文 | 高敏感 | Worker 内存和获授权的单次命令 | AEAD、exact closure、短生命周期、禁止序列化 |
| 日志、审计、support bundle | 可外发 | PostgreSQL/ZIP/日志 | 闭集 metadata、HMAC 标识、递归脱敏 |

关键原则是：

```text
authentication
  -> server-issued project/private context
  -> exact Run admission snapshot
  -> Worker lease + current authorization revalidation
  -> model/tool/MCP/sandbox side-effect

untrusted text
  -> source-specific normalization
  -> structure escaping
  -> trusted wrapper/template
  -> model context
```

两个流程必须同时成立。

## 3. `main` 源码地图

| 层次 | `main` 文件 | 责任 |
| --- | --- | --- |
| 用户输入 | `backend/packages/harness/deerflow/agents/middlewares/input_sanitization_middleware.py` | framework/injection tag 转义、用户边界包裹、上传原文分离 |
| 远程结果 | `backend/packages/harness/deerflow/agents/middlewares/tool_result_sanitization_middleware.py` | 对 web 工具 `ToolMessage` / `Command` 递归净化 |
| middleware 组装 | `backend/packages/harness/deerflow/agents/middlewares/tool_error_handling_middleware.py` | 输入、输出预算、结果净化、Guardrail 的顺序 |
| Guardrail 协议 | `backend/packages/harness/deerflow/guardrails/provider.py` | `GuardrailRequest`、`GuardrailDecision`、provider protocol |
| 内置策略 | `backend/packages/harness/deerflow/guardrails/builtin.py` | `AllowlistProvider` |
| 执行门禁 | `backend/packages/harness/deerflow/guardrails/middleware.py` | 工具调用前 evaluate、fail-closed、审计 |
| Agent prompt | `backend/packages/harness/deerflow/agents/lead_agent/prompt.py` | SOUL、Skill metadata、Subagent description 的转义 |
| Skill context | `backend/packages/harness/deerflow/agents/middlewares/skill_activation_middleware.py`、`skill_context.py` | Skill 内容、属性、持久上下文转义 |
| Memory prompt | `backend/packages/harness/deerflow/agents/memory/backends/deermem/deermem/core/prompt.py`、`updater.py` | fact、summary、source error、更新输入转义 |
| summarization | `backend/packages/harness/deerflow/agents/middlewares/summarization_middleware.py` | previous summary 和 message transcript 转义 |
| tool-call 修复 | `backend/packages/harness/deerflow/agents/middlewares/dangling_tool_call_middleware.py` | 不可信 tool name/ID/arguments 规范化 |
| provider 适配 | `backend/packages/harness/deerflow/models/mindie_provider.py` | tool call 参数和 tool response 的 XML 转义 |
| support bundle | `scripts/support_bundle.py` | 配置、URL、CLI、env、Bearer、路径递归脱敏 |
| 本地执行安全 | `backend/packages/harness/deerflow/sandbox/security.py` | host bash 默认禁用 |

## 4. `main` 输入净化：符号、状态与调用链

### 4.1 核心原语

`input_sanitization_middleware.py` 的核心符号为：

```text
_BLOCKED_TAG_NAMES: frozenset[str]
_BLOCKED_TAG_PATTERN: Pattern
_USER_INPUT_BEGIN = "--- BEGIN USER INPUT ---"
_USER_INPUT_END   = "--- END USER INPUT ---"

neutralize_untrusted_tags(text: str) -> str
_is_genuine_user_message(message: object) -> bool
_check_user_content(text: str) -> str

class InputSanitizationMiddleware(AgentMiddleware[AgentState]):
    _extract_text_from_content(content: str | list) -> tuple[str, list | None]
    _rebuild_content(original_content, processed_text, text_blocks) -> list
    _process_request(request: ModelRequest) -> ModelRequest
    _try_process(request: ModelRequest) -> ModelRequest
    wrap_model_call(request, handler) -> ModelCallResult
    awrap_model_call(request, handler) -> ModelResponse
```

`neutralize_untrusted_tags()` 只做两件确定性的事：

1. 将 blocked tag 的 `<` / `>` HTML 转义；
2. 把用户伪造的 BEGIN/END 边界改成不可执行的视觉近似文本。

它不负责增加用户边界；`_check_user_content()` 才负责：

```text
空白 -> 原样
blocked tags -> 转义
严格已经包裹 -> 保留外层、仍净化内部伪造边界
其他 -> BEGIN + 内容 + END
```

这种拆分使相同净化原语可复用于工具结果，而不会给工具输出错误套上“用户输入”标签。

### 4.2 完整 authority-tag denylist

`main` 最终 denylist 不只是常见的 `system/instruction/override`，还覆盖框架真实生成的 authority blocks：

```text
system-reminder, system_reminder, memory, current_date, think, analysis,
role, soul, self_update, thinking_style, clarification_system,
critical_reminders, response_style, citations, uploaded_files,
current_uploads, subagent_system, skill_system, skill_index,
available_skills, disabled_skills, memory_tool_system, todo_list_system,
durable_context_data, slash_skill_activation, mcp_routing_hints,
available-deferred-tools, goal_continuation, file_editing_workflow,
guidelines, output_format, working_directory, tool_restrictions
```

再加常见注入标签：

```text
system, instruction, important, override, ignore, prompt
```

测试 `test_denylist_covers_framework_authority_blocks()` 固定该集合数量。新增 framework block 时，生产集合和测试 inventory 必须同改；否则“新增一个可信标签”也等于新增一个用户可伪造的权限通道。

### 4.3 genuine user 判定

`_is_genuine_user_message()`：

- 只接受 `HumanMessage`；
- 跳过 `name == "summary"` 的旧摘要；
- 跳过 `hide_from_ui` 的框架隐藏消息；
- 但通过 `read_human_input_response()` 识别真正的隐藏人类审批/回答，不能误删。

因此执行链为：

```text
ModelRequest.messages 逆序扫描
  -> 找最后一条 genuine HumanMessage
  -> 提取 string 或 multimodal text blocks
  -> 仅净化用户拥有的部分
  -> request.override(messages=新列表)
  -> 调用内层 model handler
```

变换只存在于 `ModelRequest`，不写回 graph state。这样 checkpoint 保存原始用户意图，模型看到的是安全视图。

### 4.4 上传内容的双通道

`UploadsMiddleware` 会把服务端构造的 `<current_uploads>` block 放在用户文本之前，并在 `additional_kwargs[ORIGINAL_USER_CONTENT_KEY]` 保存原始用户文本。`main` 的 `_process_request()` 按以下优先级处理：

```text
ORIGINAL_USER_CONTENT_KEY 是非空 str
  -> 只 _check_user_content(original_user_content)
  -> 用 rfind 在组合文本中替换最后一个用户后缀
  -> 服务端 current_uploads block 保持结构

marker 是空 str
  -> 说明仅上传、无用户文本
  -> 不扫描服务端 block

marker 缺失
  -> 普通消息，扫描全部文本
```

multimodal `rfind` 失败时，代码保留 `content[0]` 的服务端 block，只逐项净化后续用户 blocks；无法区分时才降级为全内容净化。这个降级会损失上传 UX，但不牺牲用户注入防护。

### 4.5 错误和并发边界

- `wrap_model_call` / `awrap_model_call` 共用 `_try_process()`，同步/异步语义一致。
- `GraphBubbleUp` 必须继续抛出，不能吃掉 interrupt/pause/resume。
- 其他异常记录 warning 并返回原 request，即这里选择 fail-open，避免净化器 bug 阻断全部模型调用。
- 中间件本身无共享可变状态，多个 Run 可并发复用实例。
- 安全代价是“异常时未净化”。因此异常路径必须有测试/遥测，且不应把任意复杂解析放入该中间件。

## 5. `main` 远程工具结果净化

### 5.1 核心符号

```text
_REMOTE_CONTENT_TOOL_NAMES = {
    "web_fetch", "web_search", "image_search", "web_capture"
}

_neutralize_content(content: object) -> object
_sanitize_tool_message(message: ToolMessage) -> ToolMessage
_sanitize_result(result: ToolMessage | Command) -> ToolMessage | Command

class ToolResultSanitizationMiddleware:
    _should_sanitize(request: ToolCallRequest) -> bool
    wrap_tool_call(request, handler) -> ToolMessage | Command
    awrap_tool_call(request, handler) -> ToolMessage | Command
```

支持的数据形态：

- 纯 `str`；
- content list 内的裸 `str`；
- `{"type": "text", "text": ...}`；
- `Command.update["messages"]` 内的一个或多个 `ToolMessage`；
- 非文本 block 保持原对象。

调用链：

```text
ToolCallRequest
  -> handler 执行远程工具
  -> 根据 request.tool_call.name 判断来源
  -> _sanitize_result()
  -> neutralize_untrusted_tags()
  -> 外层 ToolOutputBudgetMiddleware 对已净化内容截断
  -> 模型看到 ToolMessage
```

先净化再截断很重要：如果先截断，可能留下半个标签或截掉转义后的安全后缀。

### 5.2 已知边界

该实现按工具名识别来源。任意 MCP server 可把远程抓取工具命名为 `fetch_url`、`crawl` 或任何其他名字，因此不会命中。`main` 源码已经把它标为 known limitation；正确修复不是模糊匹配名称，而是在注册工具时写不可伪造的 provenance metadata，再按 metadata 净化。

## 6. `main` Guardrail：协议和执行门禁

### 6.1 数据契约

`provider.py` 定义：

```text
@dataclass
class GuardrailRequest:
    tool_name: str
    tool_input: dict[str, Any]
    agent_id: str | None
    thread_id: str | None
    is_subagent: bool
    timestamp: str
    user_id: str | None
    user_role: str | None
    oauth_provider: str | None
    oauth_id: str | None
    run_id: str | None
    tool_call_id: str | None
    channel_user_id: str | None
    is_internal: bool
    authz_attributes: dict[str, Any]

@dataclass
class GuardrailReason:
    code: str
    message: str = ""

@dataclass
class GuardrailDecision:
    allow: bool
    reasons: list[GuardrailReason]
    policy_id: str | None
    metadata: dict[str, Any]

class GuardrailProvider(Protocol):
    evaluate(request) -> GuardrailDecision
    aevaluate(request) -> GuardrailDecision
```

`GuardrailMiddleware._build_request()` 从 server runtime context 取身份与 attribution，`authz_attributes` 经 `normalize_authz_attributes()` 规范化，不能把客户端任意 mapping 原样交给策略。

### 6.2 空 allowlist 的安全语义

`AllowlistProvider.__init__()` 的正确实现是：

```text
self._allowed = set(allowed_tools) if allowed_tools is not None else None
```

语义：

- `None`：operator 未配置 allowlist，默认允许；
- `[]`：operator 明确允许零个工具，拒绝全部；
- 非空列表：只允许集合内工具。

`if allowed_tools` 会把前两种状态折叠，是典型权限 fail-open。

### 6.3 执行链与错误语义

```text
ToolCallRequest
  -> GuardrailMiddleware._resolve_context()
  -> _build_request()
  -> provider.evaluate()/aevaluate()
     -> allow: handler(request)
     -> deny: status="error" ToolMessage，不调用 handler
     -> provider exception:
          fail_closed=True  -> deny + oap.evaluator_error
          fail_closed=False -> audit 后调用 handler
```

`GraphBubbleUp` 始终传播。拒绝结果使用原 `tool_call_id` 和 tool name，让 LangGraph 消息配对保持合法，模型也可选择替代路径。

`_record_guardrail_event()` 最佳努力写 `RunJournal`，包括 tool、agent、subagent、role、allow、policy、reason code、截断后的 reason message、provider_error 和 fail mode。审计失败不能改变已作出的授权决策。

### 6.4 middleware 组装顺序

`_build_runtime_middlewares()` 的关键结构为：

```text
outer_wrappers:
  InputSanitizationMiddleware
  ToolOutputBudgetMiddleware
  ToolResultSanitizationMiddleware

thread_hooks:
  ThreadDataMiddleware
  UploadsMiddleware（lead 可选）
  SandboxMiddleware

tail:
  DanglingToolCallMiddleware（可选）
  LLMErrorHandlingMiddleware
  authorization GuardrailMiddleware
  operator GuardrailMiddleware
```

`main` 在启用 `authorization` 时将 `GuardrailAuthorizationAdapter` 放在显式外部 Guardrail 之前，先拒绝无权限请求，避免对本就应拒绝的调用发送外部 policy 请求。

## 7. `main` 结构化 prompt sink 防护

输入净化只保护用户消息。凡是会进入 SystemMessage 的数据库/文件/远程内容，都必须在其 assembly site 转义：

| sink | `main` 符号/位置 | 防护 |
| --- | --- | --- |
| SOUL | `backend/packages/harness/deerflow/agents/lead_agent/prompt.py::get_agent_soul()` | `html.escape(soul, quote=False)` |
| Skill metadata | `backend/packages/harness/deerflow/agents/lead_agent/prompt.py::_render_available_skill()`、`backend/packages/harness/deerflow/skills/describe.py::_render_skill_metadata()` | name/description/location/allowed-tools 转义 |
| Subagent 描述 | `backend/packages/harness/deerflow/agents/lead_agent/prompt.py::_build_subagent_section()` | description 首行转义 |
| slash Skill | `SkillActivationMiddleware._build_activation_reminder()` | 内容文本和属性分别 quote-aware 转义 |
| durable Skill context | `skill_context.py::_escape_context_text()` | name/path/description 转义 |
| Memory facts | `deermem/core/prompt.py::_format_fact_line()` | category/content/source_error 转义 |
| Memory summaries | `deermem/core/prompt.py::_escape_summary()` | 任意值转字符串后转义 |
| Memory updater input | `deermem/core/updater.py::_escape_memory_for_prompt()` | dict/list 递归转义 |
| summarization | `SummarizationMiddleware` prompt 组装 | previous summary/new messages 转义 |
| MindIE tool call | `convert_messages_for_mindie()` | function name/参数转义 |
| MindIE tool result | 同一转换函数 | `html.escape(text)` 后包 `<tool_response>` |

“转义后再包 trusted tag”是统一顺序：

```text
raw untrusted data
  -> html.escape(..., quote=False/True)
  -> trusted <tag attr="...">escaped</tag>
```

不能先插值再对整体字符串转义，否则平台标签也会失效；也不能只依赖用户输入 denylist，因为这些内容根本不经过用户消息。

## 8. tool-call 修复、日志和 support bundle

### 8.1 `DanglingToolCallMiddleware`

`main` 对历史/供应商产生的畸形调用做结构修复：

```text
_valid_tool_name(name)
_valid_tool_call_id(tool_call_id)
_relabel_tool_call_ids(tool_calls, msg_index, source)
_normalize_tool_name(name)
_parse_json_object(value)
_normalize_tool_arguments(arguments)
```

安全意义不是“允许坏调用执行”，而是把非法 name、ID、非 object JSON 参数规范化为可预测表示，并为孤立结果建立稳定配对，避免畸形历史跨越 middleware 假设或污染下一次模型调用。

### 8.2 `scripts/support_bundle.py`

主要原语：

```text
redact_text(text: str) -> str
_redact_secret_flag_list(items: list[Any]) -> list[Any]
_redact_env_value(value: Any) -> Any
redact_data(value: Any) -> Any
```

覆盖：

- secret-like mapping key 的递归值替换；
- URL userinfo 和敏感 query 参数；
- CLI `--token value` / `--token=value`；
- `KEY=value` env 行；
- Bearer token、OpenAI 风格 key；
- home directory 的绝对路径；
- 配置/extension/provider 任意嵌套对象。

`collect_thread_summary()` 只生成文件 manifest，不读取文件内容；thread ID 拒绝路径分隔符和 `..`。support bundle 是“可能发给第三方”的产物，因此其安全标准高于普通本地诊断日志。

## 9. `main` 配置、持久化与安全状态

### 9.1 Guardrail 配置

`AppConfig.guardrails` 由 `backend/packages/harness/deerflow/config/guardrails_config.py` 提供，主要状态为：

```text
enabled
provider.use
provider.config
fail_closed
passport
```

provider 通过 `resolve_variable()` 按类路径加载。若构造器接受 `framework` 或 `**kwargs`，组装器才注入 `framework="deerflow"`，避免把未知参数强塞给简单 provider。

配置中“禁用”“未配置 provider”“provider 异常”是三种不同状态：

- 禁用：不安装 middleware；
- 启用但配置非法：启动/解析失败，不应静默跳过；
- provider 运行异常：由 `fail_closed` 决定执行结果。

### 9.2 模型侧状态

输入净化不持久化净化后的消息；它通过 `request.override()` 创建一次模型调用视图。`ORIGINAL_USER_CONTENT_KEY` 则用于保持：

- slash command 仍读取真正用户文本；
- regenerate 不把 BEGIN/END wrapper 当用户原文；
- upload turn 能区分服务端结构和用户文本。

该旁路值本身仍是不可信数据，任何把它送回 system prompt 的调用点都必须重新转义。

### 9.3 Guardrail audit

`main` 的 RunJournal 事件是运行诊断，不是授权状态本身：

- policy 决策在调用前同步发生；
- journal 写入失败不会反转 allow/deny；
- reason message 有长度上限；
- 不记录完整工具输入和 provider metadata，避免秘密或用户内容进入审计。

## 10. `main` 测试与契约

### 10.1 输入净化

`backend/tests/test_input_sanitization_middleware.py` 覆盖：

- 每个 blocked tag 和大小写/属性/裸开标签变体；
- framework authority-block inventory 固定；
- 普通 HTML tag 不误伤；
- BEGIN/END breakout 和重复执行幂等；
- genuine user、summary、hidden framework、HumanInput response；
- string/list/multimodal content；
- 用户伪造 `<current_uploads>` 被转义、服务端 block 保留；
- `rfind` 成功、可区分失败、不可区分降级；
- sync/async 和 `GraphBubbleUp`。

### 10.2 工具结果

`backend/tests/test_tool_result_sanitization_middleware.py` 覆盖：

- `web_fetch`、`web_search`、`image_search`、`web_capture`；
- plain string、content blocks、`Command.update.messages`；
- 本地工具保持 byte-identical；
- 任意名称的 MCP 工具当前不净化，该行为以负向测试明确记录。

### 10.3 Guardrail

`backend/tests/test_guardrail_middleware.py` 覆盖：

- allow/deny 和 error `ToolMessage` 配对；
- `allowed_tools=None` 与 `allowed_tools=[]`；
- sync/async provider；
- provider exception 的 fail-open/fail-closed；
- `GraphBubbleUp`；
- user/channel/internal/authz attribution；
- RunJournal 记录和审计失败不改变决策。

### 10.4 prompt sink 与外发面

- `backend/tests/test_memory_prompt_injection.py`：fact、category、source error、summary breakout。
- `backend/tests/test_subagent_prompt_security.py`：Subagent 描述和 host bash 能力展示。
- `backend/tests/test_dangling_tool_call_middleware.py`：tool name/ID/args 与 orphan 配对。
- `backend/tests/test_mindie_provider.py`：`</tool_response>` 等结构逃逸。
- `backend/tests/test_support_bundle.py`：递归 secret key、URL、CLI、env、Bearer、home path、thread manifest。

这些是可移植的“行为契约”；不能只复制生产函数而丢掉测试 inventory。

## 11. `3be3969f..main` 提交演进

按时间列出与本章直接相关的关键提交：

| 日期 | 提交 | 变化 | 安全意义 |
| --- | --- | --- | --- |
| 2026-07-11 | `4fd521e8` | 空 allowlist 拒绝所有工具 | 修复显式 deny-all 变 fail-open |
| 2026-07-11 | `5edc7a88` | `web_capture` 结果净化 | 覆盖远端 reason/status 文本 |
| 2026-07-13 | `42544755` | Skill metadata 转义 | 阻断 description/location 结构逃逸 |
| 2026-07-13 | `807c3c52` | SOUL 转义 | 项目/文件 Agent 指令不再闭合 `<soul>` |
| 2026-07-14 | `e361122b` | Subagent description 转义 | 配置描述不能伪造 system block |
| 2026-07-14 | `41b137c4` | 完整 framework tag denylist | 从常见标签升级为 authority-block inventory |
| 2026-07-14 | `c57cf221` | summarization 输入转义 | 历史摘要/消息不能控制摘要 prompt 结构 |
| 2026-07-17 | `ae223199` | MindIE tool response 转义 | 阻断 `</tool_response>` breakout |
| 2026-07-17 | `10890e10` | trusted principal 传播 | Guardrail 获得规范化授权归因 |
| 2026-07-18 | `283cea56` | support bundle denylist 扩展 | 降低 provider 自定义字段秘密泄漏 |

这些提交显示一个共同规律：安全问题集中在“某个新结构化 sink 忘了转义”，不是靠增加一句全局 prompt 就能修复。

## 12. `dev` 对应实现

### 12.1 身份到项目权限

`dev` 的请求链是：

```text
AuthMiddleware.dispatch()
  -> 校验 session / internal auth
  -> request.state.user + user ContextVar
  -> resolve_project_context(session, user_id, project_identifier, request_id)
  -> ProjectContext
  -> context.require(Capability)
  -> PrivateWorkContext.from_project(context)
```

核心文件：

| 文件 | 核心符号 |
| --- | --- |
| `backend/app/gateway/auth_middleware.py` | `AuthMiddleware`、`_is_public()` |
| `backend/app/projects/context.py` | `ProjectContext`、`resolve_project_context()`、`resolve_project_context_in_transaction()` |
| `backend/app/projects/capabilities.py` | `Capability`、`capabilities_for()` |
| `backend/app/private_work/context.py` | `strip_private_client_fields()`、`PrivateWorkContext`、`require_issued_private_work_context()` |
| `backend/app/private_work/runtime_context.py` | `prepare_private_run_config()` |

`ProjectContext` 是 frozen dataclass，携带：

```text
user_id, project_id, membership_id, role, capabilities,
membership_version, request_id
```

`resolve_project_context_in_transaction()` 同时要求：

- Project active 且未 suspended；
- Membership 属于当前认证用户且 active；
- 结果必须唯一；
- outsider、未知/非法 role、歧义结果统一为 `ProjectNotFound`。

`system_admin` 不是项目成员的替代品。

### 12.2 issued `PrivateWorkContext`

`PrivateWorkContext` 不能直接构造、继承、copy、deepcopy、pickle 或 reduce，只能：

```text
PrivateWorkContext.from_project(exact ProjectContext)
```

实例在 `_ISSUED_CONTEXTS` 中用对象 identity、weakref 和字段 snapshot 登记；`require_issued_private_work_context()` 同时校验：

- exact class；
- 同一进程内同一对象；
- 字段未变化；
- 登记仍存活。

这不是密码学 token，而是防止应用内部误把请求 JSON/dataclass-lookalike 当权威对象。

`strip_private_client_fields()` 和 `prepare_private_run_config()` 递归删除：

```text
owner/project/membership/role/capability/asset/model/skill/mcp/private_scope
以及 __*、secret-like、checkpoint-authority 字段
```

最后由服务端无条件写回 exact `thread_id` 和 opaque `private_scope`，客户端同名值永远不能胜出。

### 12.3 数据隔离

`dev` 不依赖 PostgreSQL RLS；隔离由应用层共同保证：

```text
issued context
  + 每个 repository 的 project_id + owner_user_id 谓词
  + composite FK / unique / check constraints
  + 固定 Project -> Membership -> resource 锁顺序
  + ordinary non-superuser application role
```

项目 outsider、旧 membership、wrong owner 和不存在私有资源折叠为 404；仍为当前成员但缺 capability 才返回 403，以降低资源存在性泄漏。

### 12.4 Run 持续授权

`backend/app/private_work/authorization.py` 定义：

```text
PrivateRunAuthorizationService.mark_revoked(...)
PrivateRunAuthorizationService.is_active(..., lock=False) -> bool

class PrivateRunAuthorizationBoundary:
    bind_abort_event(event)
    before_model_call()
    before_tool_call()
    before_mcp_call()
    before_sandbox_write()
    before_sandbox_exec()
    before_checkpoint_read()
    before_checkpoint_write()
    before_file_finalization()
```

每个方法最终进入 `_check()`：

```text
新 AsyncSession
  -> Run.project_id + owner_user_id + run_id
  -> Run pending/running 且无 authorization cancellation
  -> Project active/not suspended
  -> Membership active 且 role in admin/editor/runner
  -> active?
      yes -> 返回
      no / DB error -> 本地 abort + on_revoke + AuthorizationRevoked
```

数据库异常按 fail-closed 处理且不向外暴露详情。`ToolErrorHandlingMiddleware` 必须让 `AuthorizationRevoked`/`GraphBubbleUp` 穿透，不能转换成普通工具错误后继续 Run。

### 12.5 Credential envelope

`dev` 的核心文件：

| 文件 | 责任 |
| --- | --- |
| `backend/app/shared_assets/keyring.py` | `CredentialKeyring`，32-byte key，active key |
| `backend/app/shared_assets/crypto.py` | canonical JSON、AES-GCM、AAD |
| `backend/app/shared_assets/credential_service.py` | 创建/替换/轮换 workflow |
| `backend/app/shared_assets/credential_closure.py` | MCP exact grant/envelope 闭包 |
| `backend/app/shared_assets/skill_credential_closure.py` | Skill version exact binding 闭包 |
| `backend/packages/harness/deerflow/persistence/shared_assets/credential_model.py` | Credential/version/envelope/grant 行 |

`encrypt_credential_payload()`：

```text
payload
  -> 只允许 env/headers/oauth sections
  -> canonical JSON、<=64 KiB、禁止 NaN/非 JSON 类型
  -> AAD = schema version + credential version ID + scope + project owner
  -> AESGCM(active 32-byte key).encrypt(random 12-byte nonce, plaintext, aad)
  -> EncryptedEnvelope(key_id, nonce, ciphertext)
```

解密会重新 canonicalize 并要求明文字节完全一致；任何 key、nonce、tag、AAD、JSON 或 canonical mismatch 都映射成无细节的 `CredentialDecryptFailed`。

持久化层用唯一 active-envelope 索引和 generation 保证一个 Credential version 只有一个当前 envelope。API、snapshot、repr、日志和 audit 都不能返回 `key_id/nonce/ciphertext`，Worker 只在 exact version/grant/binding 校验后短暂解密。

### 12.6 Audit

`backend/app/audit/models.py` 定义闭集：

```text
AuditAction
AuditTargetKind
AuditOutcome
AuditProcess
AuditScope
AuditActionContract
```

每个 action 绑定允许的 actor、target、outcome 和严格 metadata model；`AuditService` / sinks 写 append-only 行。私有 target 用 domain-separated HMAC，不存 raw project-owner resource ID。prompt、message、Memory、文件、Run output、URL、异常文本、Credential 都不属于允许 metadata。

### 12.7 `dev` 仍保留的 `main` 运行时原语

`dev` 仍有：

- `InputSanitizationMiddleware`；
- `ToolResultSanitizationMiddleware`；
- `GuardrailMiddleware`；
- `DanglingToolCallMiddleware`；
- Memory/summarization 的若干 escaping；
- `scripts/support_bundle.py`；
- host bash 默认禁用。

但这些文件经过平台分支改写，不能假定等价于 `main` final。

## 13. `main` 与 `dev` 逐项差异

| 主题 | `main` final | `dev` 当前 | 判断 |
| --- | --- | --- | --- |
| 用户输入 denylist | 完整 framework authority inventory | 缩减为约十余个旧标签 | `dev` 回归 |
| upload 分离 | 只净化 `ORIGINAL_USER_CONTENT_KEY` | 对组合文本整体 `_check_user_content()` | `dev` 回归 |
| malformed original marker | 非字符串会修复为真实原文 | `setdefault` 保留畸形值 | `dev` 较弱 |
| remote tools | 含 `web_capture` | 只有 fetch/search/image | `dev` 回归 |
| MCP remote result | main 也未覆盖 | private MCP 有 provenance metadata，但净化器未用 | 双方残余风险，`dev` 可修 |
| empty Guardrail allowlist | `[]` deny all | `[]` 被折叠成 `None` | `dev` 确认缺陷 |
| Guardrail principal | channel/internal/normalized authz attrs | 字段和 normalize 被移除 | 普通 operator Guardrail 归因变弱 |
| 平台 authorization | 通用 adapter + runtime context | exact private revalidation boundary | `dev` 更强且不可回退 |
| Agent/Skill prompt | main 对旧 SOUL/metadata 转义 | dev v2 Agent docs、describe metadata 有裸插值 | 新 sink 未继承旧防护 |
| durable Skill context | 转义 | 转义 | 保留 |
| Credential | 旧配置/请求型秘密 | exact encrypted envelope + command boundary | `dev` 更强 |
| isolation | user/thread 为主 | account/project/owner/composite scope | `dev` 更强 |
| audit | RunJournal + support bundle | typed append-only PostgreSQL audit + support bundle | `dev` 更强 |
| exception policy | sanitizer fail-open；Guardrail configurable | private authorization fail-closed | 应保持不同边界的不同策略 |

## 14. 已确认缺陷与剩余风险

### 14.1 `dev`：输入 authority-tag inventory 回归

当前 `_BLOCKED_TAG_NAMES` 删除了 `system_reminder`、`current_uploads`、`durable_context_data`、`skill_index`、`available-deferred-tools`、`goal_continuation`、`file_editing_workflow` 等 `main` 已覆盖标签。

更重要的是，`dev` 又新增：

```text
agent_profile
agent_profile_document
```

以及新的项目资产/运行时结构。单纯复制 `main` 常量仍不完整。应扫描 `dev` 所有 SystemMessage/hidden HumanMessage 的 framework wrapper，生成当前 inventory 并用 exact-set 测试锁定。

### 14.2 `dev`：上传可信 wrapper 被误净化

`dev` 的 `_process_request()` 不再根据 `ORIGINAL_USER_CONTENT_KEY` 分离用户后缀，而是对拼接后的全文执行 `_check_user_content()`。因为 `<current_uploads>` 也应加入 denylist，直接恢复完整 denylist 会把服务端 wrapper 一并转义，上传上下文失去结构。

因此 upload 分离修复与 denylist 修复必须同一提交落地，不能只做其中一个。

### 14.3 `dev`：空 allowlist fail-open

当前：

```text
self._allowed = set(allowed_tools) if allowed_tools else None
```

`AllowlistProvider(allowed_tools=[])` 允许所有工具。这是确定性权限缺陷，不是产品取舍。

### 14.4 `dev`：`web_capture` 结果未净化

`web_capture` 会暴露目标站点可控的响应状态文本。当前 `_REMOTE_CONTENT_TOOL_NAMES` 删除它，使远程站点可把伪造 framework tag 送入模型上下文。

### 14.5 `dev`：Agent v2 文档结构逃逸

`backend/packages/harness/deerflow/agents/lead_agent/prompt.py::render_agent_prompt_bundle()` 将 `AGENTS.md`、`SOUL.md`、`IDENTITY.md`、`USER.md` 内容直接放入：

```text
<agent_profile_document name="...">...</agent_profile_document>
```

项目作者可用 `</agent_profile_document>` / `</agent_profile>` 闭合结构。平台权限仍由代码执行边界控制，但模型层级和保密提醒可被扰乱。

### 14.6 `dev`：Skill metadata 结构逃逸

`backend/packages/harness/deerflow/skills/describe.py::_render_skill_metadata()` 直接插入 description/location。虽然 Skill package 已静态扫描，metadata 仍是项目可配置内容，且“通过扫描”不等于“可作为 system markup”。

### 14.7 双方：按名称净化远程工具不完整

`dev` 的 private MCP proxy 已带 `metadata={"deerflow_private_mcp": True}`。这是比名称更可靠的来源标记，但 `ToolResultSanitizationMiddleware._should_sanitize()` 尚未读取它。MCP server 返回的 prompt-injection 文本因此仍可原样进入模型。

### 14.8 sanitizer fail-open 的可观测性

输入净化器对意外异常 fail-open 是可用性选择，但 warning 若在生产聚合中不可见，会形成无声降级。应增加稳定计数器/安全事件，禁止记录原始用户内容。

### 14.9 通用 Guardrail 与 private authorization 的语义漂移

`dev` 通过 `ToolErrorHandlingMiddleware` 直接调用 `check_authorization_boundary()`，这比 `main` adapter 更贴近 exact private runtime；但 `GuardrailRequest` 删除 channel/internal/authz attribution 后，外部 operator policy 无法获得同等归因。应恢复字段或明确废弃外部 provider 的这些能力，不能保持半兼容。

## 15. 可移植落点

### P0：恢复输入边界完整性

精确目标：

- `backend/packages/harness/deerflow/agents/middlewares/input_sanitization_middleware.py`
- `backend/packages/harness/deerflow/agents/middlewares/uploads_middleware.py`
- `backend/tests/test_input_sanitization_middleware.py`

实施：

1. 盘点 `dev` 当前所有 framework wrapper；
2. 恢复 `ORIGINAL_USER_CONTENT_KEY` 分离逻辑；
3. 加入 `agent_profile` / `agent_profile_document` 等 `dev` 新标签；
4. 用 exact inventory 测试防漂移；
5. 保留 multimodal、empty upload 和 malformed marker 的安全降级。

### P0：修复权限 fail-open

精确目标：

- `backend/packages/harness/deerflow/guardrails/builtin.py::AllowlistProvider.__init__`
- `backend/tests/test_guardrail_middleware.py`

只移植 `is not None` 三态语义，不覆盖 `dev` 的 private authorization boundary。

### P0：转义 Agent v2 bundle

精确目标：

- `backend/packages/harness/deerflow/agents/lead_agent/prompt.py::render_agent_prompt_bundle`
- 新建或扩展 exact Agent prompt security 测试

文档 body 用 `html.escape(..., quote=False)`；document `name` 若进入属性必须 `quote=True`。四个 body 都应测试 closing-tag breakout 和 benign byte stability。

### P1：恢复所有远程结果净化

精确目标：

- `backend/packages/harness/deerflow/agents/middlewares/tool_result_sanitization_middleware.py`
- `backend/tests/test_tool_result_sanitization_middleware.py`

实施：

1. 恢复 `web_capture`；
2. `_should_sanitize()` 同时识别 immutable tool metadata 的 `deerflow_private_mcp=True`；
3. 不用 `fetch/search` 子串启发式；
4. 保持本地文件/命令输出 byte-identical。

### P1：转义 Skill metadata

精确目标：

- `backend/packages/harness/deerflow/skills/describe.py::_render_skill_metadata`
- `backend/packages/harness/deerflow/agents/lead_agent/prompt.py` 中仍存在的 metadata renderer

属性和值采用不同 quote 策略；禁止把整个 wrapper 一次性 escape。

### P1：恢复 Guardrail attribution

精确目标：

- `backend/packages/harness/deerflow/guardrails/provider.py::GuardrailRequest`
- `backend/packages/harness/deerflow/guardrails/middleware.py::_build_request`

字段必须来自 Worker/server-issued runtime context；不能从请求 metadata 恢复客户端值。`dev` 已有 issued `PrivateWorkContext`，应从该载体派生而不是复制 `main` 的宽松 dict 信任。

### P2：安全降级遥测

为 sanitizer exception、Guardrail provider error、support redaction failure增加无内容、低基数计数；若写审计失败，保持原始授权结果。

## 16. 禁止合并项

以下内容不应从 `main` 整体合并到 `dev`：

1. 任何用 `user_id/thread_id` 代替 `project_id + owner_user_id` 的持久化过滤。
2. 任何让客户端 `configurable/context/metadata` 提供 owner、project、role、capability、snapshot 或 lease 的路径。
3. 用通用 `GuardrailMiddleware` 代替 `PrivateRunAuthorizationBoundary` 的做法。
4. 在 Gateway 解密 Credential，或把明文放入 prompt、snapshot、checkpoint、event、repr、log、trace。
5. `main` 的旧文件/环境型 Skill secret lifecycle。
6. 用名称猜测 MCP 远程性；`dev` 已有 private MCP provenance。
7. 把 sanitizer 异常策略改成全局 fail-closed，从而让格式 bug 终止所有模型调用。
8. 把 authorization/credential/lease 异常改成 fail-open。
9. 恢复已被 `dev` 移除的全局 Agent/Skill/MCP CRUD 权威。
10. 以历史 Alembic patch 修复 `dev` final full-schema；`dev` 只支持空库执行 `full_schema.sql`。

## 17. 建议测试矩阵

| 类别 | 场景 | 预期 |
| --- | --- | --- |
| input | 每个当前 framework wrapper 由用户伪造 | 对应 tag 被转义 |
| input | 普通 HTML / 代码示例 | 非 blocked tag 不误伤 |
| input | 用户内嵌 BEGIN/END | 内层 token 被中和，外层恰好一对 |
| input | hidden HumanInput response | 作为 genuine user 净化 |
| upload | 服务端 block + 用户伪造 block | 服务端保留、用户部分转义 |
| upload | image-only / empty original | 不产生 marker 噪音 |
| upload | multimodal rfind 失败 | 服务端 block 保留，用户 text blocks 净化 |
| agent prompt | 四文档含 closing tags/属性字符 | wrapper 不可闭合，benign 内容稳定 |
| skill prompt | name/description/location 恶意 markup | 全部按位置转义 |
| remote result | 四个 built-in web tools | string/list/Command 全部净化 |
| MCP result | `deerflow_private_mcp=True` 任意工具名 | 结果净化 |
| local result | bash/read_file 含 XML 代码 | byte-identical |
| guardrail | `allowed_tools=None` | 保持未配置语义 |
| guardrail | `allowed_tools=[]` | 所有业务工具拒绝 |
| guardrail | provider exception + fail_closed | handler 未调用 |
| guardrail | `GraphBubbleUp` / `AuthorizationRevoked` | 穿透，不变成普通 ToolMessage |
| attribution | forged client role/project/internal | provider 只见 server-issued 值 |
| isolation | outsider/wrong owner/stale membership | 404 且无存在性泄漏 |
| revalidation | membership 在 Run 中撤销 | 下一 model/tool/MCP/sandbox/checkpoint 边界终止 |
| credential | AAD/version/project/key/nonce 任一错误 | 统一无细节解密失败 |
| credential | API/snapshot/log/audit | 不出现 key/nonce/ciphertext/plaintext |
| audit | metadata 含 prompt/URL/secret/raw owner ID | schema 拒绝 |
| support | 嵌套 provider 自定义 secret key | 递归遮罩 |
| support | thread directory 有敏感文件 | 只列 manifest，不读正文 |
| concurrency | 两个项目同名资源并发运行 | context、lease、Credential 无交叉 |
| degradation | sanitizer 人为抛异常 | 请求行为符合 fail-open 且只发无内容遥测 |
