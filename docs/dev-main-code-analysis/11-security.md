# 11. Security 模块：`main` 最终实现、提交演进与 `dev` 精确落点

## 1. 分析范围与结论

本文把 Security 定义为贯穿模型边界、工具边界、身份/授权边界和秘密边界的横切模块。它不重复 Agent、Skill、MCP、Sandbox 各自的业务流程，只分析这些流程共同依赖的安全原语。对比基线为：

- 公共祖先：`3be3969f8fc3f2d2b6d36ef5c26fa5593d916f2a`
- `main`：`e317f7b8d9b2afb4c3925812d4774da602c9f8f3`
- `dev` 已提交基线：`785be51341c1c3ddaa073b76aaa4421bee0ac136`
- `dev` 迁移状态：以上述提交加当前工作树为准；本文不把尚未提交的工作树误写成新的 commit

`main` 分支没有本文件。本文中的“`main` 具体实现”均通过
`git show main:<path>` 直接读取对应源码和测试，而不是只看 `dev` 中的同名文件或历史摘要。

结论先行：

1. `main` 在公共祖先之后完成了四类可独立移植的纵深防御：完整 framework-tag 输入净化、远程工具结果净化、所有结构化 prompt sink 的上下文转义、Guardrail 空 allowlist 的 fail-closed 修复。
2. `dev` 的平台安全架构远强于 `main`：认证身份生成不可伪造的 `ProjectContext` / `PrivateWorkContext`，所有私有数据绑定 `project_id + owner_user_id`，Run 的每个副作用边界重新校验当前权限，Credential 使用 AEAD envelope 且只在 Worker 命令边界解密。
3. 这两类能力不是替代关系。`dev` 的数据库隔离不能阻止 prompt 结构逃逸；`main` 的字符串净化也不能替代项目授权、版本闭包、租约和 Credential revalidation。
4. 这些局部防御已按 `dev` 信任边界落地：公网 Run 采用状态通道 allowlist；输入覆盖完整历史和多模态；上传、四个 Web 工具、Worker 注册的 private MCP 以及 MCP schema 均按服务端 provenance 净化；Agent/Skill/Memory/Subagent prompt sink 在组装点转义；Guardrail 使用 Worker-issued carrier，并固定“授权预检 → provider → 按工具类别执行第二次授权检查/副作用 fence → handler”的顺序。
5. `dev` 还收口了迁移外围的外发面：公网 Trace 由 Gateway 生成并贯穿 Run/Job/Worker；unexpected runtime exception 的正文折叠为稳定错误码；SandboxAudit 只记录命令长度和 SHA-256，不记录原始命令；support bundle 只从闭集摘要生成 ZIP 和 sidecar。
6. 文中“净化/中和”均指**结构中和**：转义保留文本，但使其不能闭合或伪造框架标签/边界。它不分析自然语言是否恶意，也不保证模型永远不会服从一段语义上具有诱导性的普通文本。
7. 不能直接把 `main` 的旧认证/用户模型、文件型秘密、全局运行上下文或 middleware 排序整体覆盖进 `dev`。本轮实际做法是在 `dev` 的 Worker-only、exact snapshot、project/owner revalidation 架构内移植局部行为契约。

## 2. 信任边界总览

安全数据流应按来源分类，而不是按“是否已经是字符串”分类：

| 数据来源 | 信任级别 | 进入位置 | 应用控制 |
| --- | --- | --- | --- |
| 公网 Run JSON | 不可信 | `PrivateRunCreateRequest.input/command/config/context/metadata` | 只接受 user/human message 与 `resume`，递归删除 authority 字段 |
| 浏览器/IM 用户文本 | 不可信 | `HumanMessage` | 全历史输入净化、边界包裹、服务端原文旁路字段 |
| 上传文件名/描述 | 不可信 | `<current_uploads>` 内的数据字段 | 在构造可信 wrapper 前转义；不能再净化 wrapper 本身 |
| 上传文件正文、Web/MCP 远程结果 | 不可信 | `ToolMessage` / `Command.update.messages` | 注册工具对象与规范路径驱动的 provenance 净化 |
| MCP description / JSON schema / routing metadata | 远程可控 | `tool_search` 结果和 system prompt | 结果中和、prompt 组装点转义 |
| Agent/Skill/Memory 项目内容 | 项目可配置但对平台不可信 | SystemMessage 结构块 | assembly-site XML/HTML 转义 |
| 平台模板和固定标签 | 可信 | system prompt / hidden context | 不允许用户伪造相同结构 |
| 认证、项目、owner、capability、lease | 仅服务端可信 | runtime context | 丢弃客户端同名字段，使用 issued context |
| Trace ID | 公网输入不可信、服务端生成值可信 | HTTP → Run → Job → Worker | 忽略公网 header/metadata 同名值，持久化并核对 server origin |
| Credential 明文 | 高敏感 | Worker 内存和获授权的单次命令 | AEAD、exact closure、短生命周期、禁止序列化 |
| 异常、日志、审计、support bundle | 可外发 | ToolMessage/stream/PostgreSQL/ZIP/日志 | 异常正文折叠、HMAC 标识、固定字段/限长内容；support 输出需通过闭集 schema |

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
- `Command.update["messages"]` 为 list 时，其中的 `ToolMessage`；
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

`backend/app/reliability/execution.py::PrivateRunExecutionBoundary` 在同一契约上进一步区分：

```text
before_read_only_tool_call()   -> 当前授权检查，不写 ambiguous-side-effect 标记
before_tool_call()             -> 当前授权检查 + unknown-side-effect fence
before_mcp_call()              -> MCP 发现/准备阶段的当前授权检查
before_mcp_tool_dispatch()     -> quota 后、远程 dispatch 前写 unknown-side-effect fence
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

角色变更也已与持续授权闭环：`MembershipService.change_role()` 计算
`capabilities_for(old_role) - capabilities_for(new_role)`；只要集合非空，就在同一治理事务中
`mark_revoked()` 该用户当前 pending/running Run，而不再只处理降为 viewer 的情况。这样
admin → runner 等“仍可运行但能力减少”的变更，也会在下一次只读预检之前终止旧权限下启动的 Run。

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

### 12.7 公网 Run 状态 allowlist

`backend/app/gateway/private_work_schemas.py` 不再把公网请求的任意
`AgentState` 透传给 Worker：

```text
PrivateRunCreateRequest.strip_nested_client_authority()
  -> metadata/config/context: strip_private_client_fields() 递归删除 authority
  -> input: _sanitize_public_run_input()
  -> command: _sanitize_public_run_command()
```

`input` 只保留 `messages`；message 必须显式带 `role/type = user|human`，若两个判别字段同时出现，
两者都必须属于该集合。清洗器删除客户端 `name`，并从 `additional_kwargs` 递归删除 private
authority、`ORIGINAL_USER_CONTENT_KEY` 和 `hide_from_ui`。因此公网不能伪造 summary、隐藏审批答复、
上传可信前缀、goal、delegation、promotion 或其他内部状态通道。

`command` 只保留 opaque `resume` 和经过同一 user-message allowlist 清洗的
`update.messages`；其他 command/update 字段被丢弃。`resume` 是 interrupt 答案，不会被解释为
任意 graph state。

响应侧 `backend/app/gateway/routers/private_work.py::_public_run_metadata()` 递归删除
`project_id/owner_user_id/user_id` 以及 secret/token/password/credential/ciphertext/private-key/
key-id/nonce/storage-locator 类字段，避免已持久化内部 metadata 通过 Run API 反向外泄。

### 12.8 当前运行时纵深防御

`dev` 不是“仍有同名文件”，而是已把 `main` 行为重建到自己的边界：

- `InputSanitizationMiddleware`：完整 authority inventory、完整历史、多模态、upload trusted-prefix 分离；
- `ToolResultSanitizationMiddleware`：四个 Web 工具、注册 private MCP、规范 upload read；
- `tool_search`：远程 MCP description/JSON schema 结果净化，名称与 routing hint 转义；
- Agent/Skill/Memory/Subagent prompt assembly：逐 sink 转义；
- `GuardrailMiddleware`：Worker-issued carrier、严格 decision、授权预检与副作用 fence 排序；
- `scripts/support_bundle.py`：只从配置、环境、Git 和 doctor 的闭集摘要生成 ZIP/sidecar；
- Trace：Gateway server-origin 持久化到 Run/Job，并在 Worker 核对、恢复；
- runtime exception：闭集错误码替代异常正文；
- `SandboxAuditMiddleware`：原命令只在内存分类，日志只写长度和摘要；
- `DanglingToolCallMiddleware` 与 host bash 默认禁用继续保留。

## 13. `main` 与 `dev` 逐项差异

| 主题 | `main` final | `dev` 当前 | 判断 |
| --- | --- | --- | --- |
| 公网 Run ingress | `main` 通用 Run 输入 | 只保留 user/human messages、`resume`、`update.messages` | `dev` 新增平台边界 |
| 用户输入 denylist | 完整 framework authority inventory | 完整集合并增加 `agent_profile*` | 已移植并扩展 |
| 历史消息 | 只处理最后一条 genuine user message | 处理每一条 genuine user message | `dev` 更完整 |
| multimodal text | typed text block | typed block + bare string，保留非文本 block | `dev` 更完整 |
| upload 分离 | `ORIGINAL_USER_CONTENT_KEY` 后缀分离 | 保留可信 prefix；marker mismatch 全文净化 | 已移植并 fail-safe |
| malformed original marker | 非字符串修复为真实原文 | 同样修复，同时发出无内容事件 | 已移植 |
| remote tools | 四个 built-in Web 工具 | 同样四个工具 | 已移植 |
| upload read result | 未单独按 upload provenance 覆盖 | 只认 canonical `read_file_tool` + 规范 uploads 路径 | `dev` 新增 |
| MCP result/schema | 任意名 MCP 未覆盖 | Worker 注册 metadata 驱动结果净化；`tool_search` schema 中和 | `dev` 补齐 |
| `Command.update.messages` | 仅 list | 单个、list、tuple，保持原 shape | `dev` 扩展 |
| empty Guardrail allowlist | `[]` deny all | `[]` deny all | 已移植 |
| Guardrail principal | 宽 runtime attribution | private Run 只认 Worker-issued closed carrier | `dev` 收紧 |
| Guardrail decision | provider 对象基本透传 | 严格 dataclass/bool/reason/policy 校验、内容限长 | `dev` 收紧 |
| Guardrail 顺序 | authorization adapter 在 provider 前 | DB 只读预检 → provider → 工具分类后的检查/fence → handler | 适配 private Run |
| 角色降权 | 不含项目持续授权 | 任意 capability loss 撤销 live Run | `dev` 新增 |
| 平台 authorization | 通用 adapter + runtime context | exact private revalidation boundary | `dev` 更强且不可回退 |
| Agent/Skill prompt | SOUL/Skill/Subagent metadata 转义 | v2 Agent bundle、Skill metadata、subagent Skill 内容均转义 | 已移植并覆盖新 sink |
| Memory prompt | DeerMem prompt/updater/summarization 转义 | 当前 Memory prompt/updater/retrieval sink 转义/中和 | 适配新 Memory |
| Credential | 旧配置/请求型秘密 | exact encrypted envelope + command boundary | `dev` 更强 |
| isolation | user/thread 为主 | account/project/owner/composite scope | `dev` 更强 |
| audit | RunJournal + 开放式 support bundle 遮罩 | typed PostgreSQL audit + 固定字段/限长中和的 Guardrail journal + closed support bundle | `dev` 收紧 |
| Trace | 请求相关值可来自运行上下文 | Gateway server-origin，Run/Job 一致性，Worker 恢复 | `dev` 收紧 |
| runtime exception | 多处正文/log 可能带 `str(exc)` | unexpected exception 正文折叠为稳定 code；domain failure 保留固定文案 | `dev` 收紧 |
| SandboxAudit | 非本章 `main` 移植核心 | 命令分类但只外发 length/SHA-256/verdict | `dev` 收紧 |

## 14. 当前迁移实现细节与剩余边界

### 14.1 完整 authority inventory 与公网消息分类

`dev` 当前 `_BLOCKED_TAG_NAMES` 已恢复 `main` 的 framework authority 集合，并增加：

```text
agent_profile
agent_profile_document
```

测试不是只抽查几个常见标签，而是盘点当前 framework wrapper 并固定分类。更外层的公网
Run 清洗先删除非 user/human message 及客户端 `name/hide_from_ui/original_user_content`，
所以 sanitizer 接收的是用户可拥有的数据，而不是用字符串规则补救任意 AgentState 注入。

### 14.2 历史、多模态和 upload 双通道

`InputSanitizationMiddleware._process_request()` 现在正序检查**每一条** genuine
`HumanMessage`，不再只处理末条消息。content list 同时识别裸字符串和
`{"type": "text"}` block；图片等非文本 block 保持位置和内容。

若 `ORIGINAL_USER_CONTENT_KEY` 是字符串：

```text
empty                  -> 保留服务端 trusted upload block
组合文本以 marker 结尾 -> 只包裹/中和用户后缀
marker 与正文不匹配    -> 无内容 warning + 全文净化
```

若 marker 存在但类型非法，代码用真实消息文本修复，并只记录 marker 类型，不记录正文。
公网请求无法直接写该 marker；`UploadsMiddleware` 或可信 channel ingress 才能建立 trusted prefix。

### 14.3 empty allowlist 与 provider decision

`AllowlistProvider` 已恢复三态语义：

```text
None -> 未配置 allowlist
[]   -> deny all
[x]  -> 只允许集合内工具
```

Guardrail provider 返回值还会经过 `_normalize_decision()`：要求 exact
`GuardrailDecision`、`allow` 为真正的 bool、`reasons` 为
`list[GuardrailReason]`、reason code/message 和 policy ID 为字符串。reason message 先做结构中和再截断到
500 字符；标识符限制为安全字符和 128 字符；provider metadata 不进入返回消息或 journal。
畸形 decision 与 provider exception 一样进入配置的 fail-open/fail-closed 分支。

### 14.4 Web、upload、MCP result 与 schema

`ToolResultSanitizationMiddleware._should_sanitize()` 现在覆盖三类来源：

1. `web_capture/web_fetch/web_search/image_search`；
2. 注册工具对象满足 `is_private_mcp_tool(request.tool)`；
3. `request.tool is read_file_tool` 且 `PurePosixPath` 位于规范
   `/mnt/user-data/uploads`、不含 `..`。

模型在 `tool_call` 中伪造名称、path 或 metadata 不能建立 provenance。普通 workspace
`read_file`、bash 等本地结果保持原样。结果净化支持 `ToolMessage` 以及
`Command.update.messages` 的单值、list、tuple，并保持原容器 shape。

`tool_search` 对远程 MCP description/JSON schema 序列化结果调用
`neutralize_untrusted_tags()`；`<available-deferred-tools>` 名称和 MCP routing hints 在
prompt 组装点 `html.escape()`。启用 deferred search 且存在 MCP candidate、却无法恢复 deferred
集合时，assembly fail-closed，不回退为把完整 schema 直接绑定给模型。

### 14.5 Agent v2 与 SOUL sink

`render_agent_prompt_bundle()` 对 `AGENTS.md/SOUL.md/IDENTITY.md/USER.md` body 使用
`html.escape(..., quote=False)`，再放入平台拥有的
`<agent_profile_document>` wrapper；当前 document name 来自固定常量，不由项目输入控制。
v1 SOUL 也经 `_render_soul()` 转义。`AgentPromptBundle.__repr__()` 只暴露 section 名，不打印正文。

这保证项目文档中的 `</agent_profile_document>` 只是可见文本，不能关闭平台 wrapper；它不判断
文档自然语言是否试图说服模型违反规则。

### 14.6 Skill、Subagent 与 Memory sink

- `lead_agent/prompt.py` 对 Skill name/description/location 和 Subagent name/description 转义；
- `skills/describe.py::_render_skill_metadata()` 对 name/description/allowed-tools/location 转义；
- `get_skill_index_prompt_section()` 对每个 name 转义；
- `SubagentExecutor._load_skill_messages()` 对 `<skill name="...">` 属性使用
  `quote=True`，对 Skill 正文使用 `quote=False`；
- `agents/memory/prompt.py` 转义 fact content/category/source error/summary；
- `agents/memory/updater.py` 对进入更新 prompt 的嵌套 dict/list 值递归转义；
- `agents/memory/tools.py` 对检索结果应用同一结构中和原语。

安全属性来自每个 assembly site，而不是“某个上游已经净化过”的隐式假设。

### 14.7 provenance 边界

private MCP provenance 写在 Worker 创建并注册的 `BaseTool.metadata`，读取时也检查
`request.tool` 对象；不会信任 model-authored `tool_call` metadata。upload provenance 进一步要求
canonical tool identity 与 canonical virtual path 同时成立。

相关 docstring/comment 已同步为当前覆盖范围。`test_mcp_routing_metadata.py` 固定 private provenance
必须来自 `Mapping` 且值为 exact `True`，并验证 tagger 保留原 metadata、返回同一工具对象；
`test_tool_error_handling_middleware.py` 还用真实 `StructuredTool` 证明：模型即使在
`tool_call.metadata` 伪造 `deerflow_private_mcp=True`，也只能进入普通 `before_tool_call`，
不能取得 `before_mcp_call` 权限。

### 14.8 明确的非目标：恶意语义检测

以下输入都会被保留为文本，只是失去结构能力：

- “忽略上面的指令”这类普通自然语言；
- 不在 blocked inventory 中的普通 HTML/XML；
- 本地文件或 bash 输出中的 framework-like 示例；
- 已转义的 `&lt;system-reminder&gt;` 可见文本。

因此该层不是 prompt-injection classifier、内容审核器或模型行为证明。真实权限仍由
issued context、repository scope、Guardrail、lease 和副作用 fence 执行。

### 14.9 sanitizer 与授权的不同失败策略

输入 sanitizer 对意外实现异常保留 fail-open，但只写稳定、无正文事件：

```text
security_event=input_guardrail_processing_failed disposition=fail_open
```

marker mismatch 则选择全文净化，优先安全而接受 upload wrapper 可能降级。与之相对，
private attribution 缺失/畸形、授权数据库检查失败、Run/Job Trace 不一致、Credential 解密失败均
fail-closed。不能为了统一风格把两类边界改成同一种异常策略。

## 15. 可移植落点的实际落地

### 15.1 公网 Run allowlist

| 文件/符号 | 实际落点 |
| --- | --- |
| `backend/app/gateway/private_work_schemas.py::_is_public_user_message()` | 只接受显式 user/human 判别 |
| `_sanitize_public_user_message()` | 删除顶层 message name；从 `additional_kwargs` 删除 private authority、上传 marker、UI 隐藏标记 |
| `_sanitize_public_run_input()` | 只保留 messages state channel |
| `_sanitize_public_run_command()` | 只保留 opaque resume 与净化后的 update.messages |
| `PrivateRunCreateRequest.strip_nested_client_authority()` | 在 Pydantic request boundary 统一执行 |
| `backend/app/gateway/routers/private_work.py::_public_run_metadata()` | Run 响应精确删除 `project_id/owner_user_id/user_id` 和 secret-like key |

这个落点早于 LangChain message conversion 和 Run admission，因此内部 state channel 从未进入 snapshot，
不是在 Worker 末端“尽量忽略”。

### 15.2 输入、历史与上传

| 文件/符号 | 实际落点 |
| --- | --- |
| `input_sanitization_middleware.py::_BLOCKED_TAG_NAMES` | `main` inventory + `agent_profile*` |
| `_extract_text_from_content()` | plain string、bare string block、typed text block |
| `_process_request()` | 所有 genuine user history；trusted prefix/用户后缀分离 |
| `_try_process()` | `GraphBubbleUp` 穿透；异常无正文 fail-open 事件 |
| `uploads_middleware.py` | 服务端写入真实 `ORIGINAL_USER_CONTENT_KEY` |

公网清洗、UploadsMiddleware 和 sanitizer 三者必须共同成立：公网不能伪造 marker，服务端建立 marker，
sanitizer 才能把 wrapper 当可信结构。

### 15.3 upload、远程结果与 MCP schema

| 文件/符号 | 实际落点 |
| --- | --- |
| `tool_result_sanitization_middleware.py::_is_registered_upload_read()` | canonical read tool + canonical uploads path |
| `ToolResultSanitizationMiddleware._should_sanitize()` | 四个 Web name、private MCP tool object、upload read |
| `_sanitize_result()` | ToolMessage；Command 的单值/list/tuple |
| `tools/mcp_metadata.py::tag_private_mcp_tool()/is_private_mcp_tool()` | 单一 private provenance key |
| `app/private_work/asset_runtime.py` | Worker 创建 proxy 时写 provenance |
| `tools/builtins/tool_search.py::tool_search()` | MCP description/schema JSON 结构中和 |
| `get_deferred_tools_prompt_section()` / routing hints | 名称与 keyword 转义 |
| `assemble_deferred_tools()` | deferred recovery 失败时拒绝绑定 MCP schema |

### 15.4 prompt sink

| sink | 实际落点 |
| --- | --- |
| v1 SOUL / v2 Agent bundle | `lead_agent/prompt.py::_render_soul()`、`render_agent_prompt_bundle()` |
| Skill index/metadata | `lead_agent/prompt.py`、`skills/describe.py` |
| Subagent registry description | `lead_agent/prompt.py::_build_available_subagents_description()` |
| Subagent Skill 正文 | `subagents/executor.py::_load_skill_messages()` |
| Memory fact/summary/update | `agents/memory/prompt.py`、`updater.py` |
| Memory retrieval result | `agents/memory/tools.py` |

body 使用 `quote=False`，可控 attribute 使用 `quote=True`；固定 framework tag 最后组装，不能对整体
wrapper 再 escape。

### 15.5 Guardrail carrier、顺序与降权撤销

`backend/app/reliability/execution.py::_private_guardrail_attribution()` 从已锁定的 private Run 和
issued `PrivateWorkContext` 构造：

```text
user_id
user_role
thread_id
run_id
is_subagent
authz_attributes = {
  project_id,
  project_role,
  capabilities: sorted tuple
}
```

`runtime/runs/worker.py::_build_runtime_context()` 明确跳过 caller 的
`__guardrail_attribution`，再深拷贝写入 Worker carrier；`_install_runtime_context()` 删除旧 carrier，
禁止复用 config 时保留陈旧身份。`task_tool` 复制 carrier；SubagentExecutor 只把
`is_subagent` 改为 `True`，其他字段继续来自父 Run。

`GuardrailMiddleware._validate_private_attribution()` 要求 carrier 必填字段完整、capabilities 为 tuple、
project role 与 user role 一致，并与 `private_scope.project_id/owner_user_id` 对齐。private sync path
无法执行数据库异步 revalidation，直接 `AuthorizationRevoked`；async path 的顺序固定为：

```text
before_read_only_tool_call()
  -> provider.aevaluate(deep-copied tool args)
  -> deny: ToolMessage，未写 side-effect fence
  -> allow/fail-open:
       ToolErrorHandlingMiddleware
         -> canonical memory_search:
              before_read_only_tool_call()
              -> handler
         -> 普通/其他工具:
              before_tool_call() + unknown-side-effect fence
              -> handler
         -> private MCP:
              before_tool_call() + unknown-side-effect fence
              -> before_mcp_call()
              -> proxy before_mcp_tool_dispatch()
                   -> quota
                   -> dispatch 前再次写 unknown-side-effect fence
              -> remote handler
```

组装器还在启动时断言 `GuardrailMiddleware` 位于 `ToolErrorHandlingMiddleware` 外层，避免将来插入
middleware 时静默颠倒顺序。`_is_trusted_read_only_tool()` 只认代码注册的 canonical
`memory_search_tool`；模型仅靠名称不能取得只读分类。

`MembershipService.change_role()` 对任意 capability loss 调用 `mark_revoked()`；因此角色降权不会让
已入队/运行 Run 继续沿用 admission-time 能力。

对应真实 PostgreSQL 契约先把第二名成员提升为 admin，再通过真实
`MembershipService` 执行原 owner 的 admin → runner。事务将 live Run 写为
`authorization_cancel_reason=authorization_revoked`，但保持 Job `retry_safety=safe`；
`before_read_only_tool_call()` 随即拒绝，provider 与副作用路径均未到达，且不会误写
ambiguous-side-effect 状态。

### 15.6 closed support bundle

`scripts/support_bundle.py` 的主体方向已从“导出开放对象再递归遮罩”改为“闭集摘要出 ZIP”：

- `collect_config_summary() -> _config_summary()`：只保留 present、经验证的 config version、闭集
  error、model/tool/channel 数量；
- `_closed_environment_summary()`：只保留 Darwin/Linux/Windows/Other、已知 machine 和版本 token；
- `_closed_git_summary()`：branch/upstream 值固定为 `None`，只保留合法 40 位 head 与 dirty bool；
- `_closed_doctor_summary()`：只保留 included/ok/returncode/error count/warning count；
- `build_triage_report()`：由上述闭集生成固定 status、signals、next steps 和 evidence manifest。

`_config_summary()` 的计数 fast-path 与普通路径共享同一字段规范化结果：

```text
config_version:
  type(value) is int 且 0 <= value <= 1_000_000 -> value
  其他（包括 bool、字符串和对象）                    -> null

error:
  yaml_parse_failed | json_parse_failed |
  pyyaml_unavailable | invalid_config_shape -> 对应闭集 code
  其他字符串或对象                          -> null
```

因此即使 `models/tools/channels` 已经是非负整数，opaque `config_version/error` 也不能穿过 fast-path。
集成回归把两组字符串/对象 sentinel 写入实际 `config.yaml`，并检查整个 ZIP、
`config-summary.json`、`triage.json` 和两份 Markdown sidecar 均不包含 sentinel。

ZIP 固定包含：

```text
README.md
issue-summary.md
ai-issue-draft.md
triage.json
manifest.json
environment.json
config-summary.json
git.json
doctor.json（仅显式启用）
```

另外写两份 Markdown sidecar。空值、路径型等非法 `thread_id` 在 `_validate_thread_id()` 拒绝；
通过格式校验的非空 ID 随后也因缺少 trusted project + owner scope 固定拒绝。
`collect_thread_summary()` 本身执行相同的 unscoped 拒绝，因此当前 CLI 不再生成
`thread-summary.json`。`.env`、原始消息、文件正文、任意 config/provider 树、原始 command output
按设计都不属于导出 schema。`redact_text()/redact_data()` 继续作为采集边缘的 backstop，但不是
closed-schema 的替代品。

### 15.7 server-origin Trace

`backend/app/gateway/trace_middleware.py::TraceMiddleware` 忽略公网 `X-Trace-Id` 值，每个启用的 HTTP
请求由 `request_trace_context()` 生成 server trace，并只把该值写入响应 header。

`strip_private_client_fields()` 递归删除：

```text
trace_id
deerflow_trace_id
origin_trace_id
```

范围包括顶层和嵌套 metadata/config/context。`http_runtime.py` 从当前 server context 派生
`origin_trace_id`，Run admission 将同一值写入 Run 与 Job；Worker claim 先规范化，executor 要求
Run/Job trace 完全一致，否则以 `RUN_TRACE_MISMATCH` fail-closed。执行期间重新绑定
`request_trace_context(origin_trace_id)`，退出后恢复原 ContextVar。private Langfuse metadata 也只从
该 server chain 派生，不从客户端同名 metadata 恢复。

### 15.8 运行时异常脱敏

`backend/packages/harness/deerflow/error_codes.py` 提供 Tool、LLM 和 Subagent 的闭集错误码以及
LLM reason → code 映射。

```text
Tool:
  TOOL_EXECUTION_FAILED

LLM:
  LLM_QUOTA_EXCEEDED
  LLM_AUTHENTICATION_FAILED
  LLM_PROVIDER_BUSY
  LLM_PROVIDER_UNAVAILABLE
  LLM_REQUEST_FAILED
  LLM_CIRCUIT_OPEN

Subagent:
  SUBAGENT_EXECUTION_FAILED
  SUBAGENT_COMMAND_EXECUTION_UNAVAILABLE
  SUBAGENT_TURN_LIMIT_REACHED
  SUBAGENT_CANCELLED
  SUBAGENT_TIMED_OUT
  SUBAGENT_POLLING_TIMED_OUT
  SUBAGENT_RESULT_MISSING
```

- `ToolErrorHandlingMiddleware` 不读取 `str(exc)`；ToolMessage、task metadata、日志和
  `stamp_exception_meta()` 只使用 `TOOL_EXECUTION_FAILED`；
- `LLMErrorHandlingMiddleware` 的 generic 文案固定，fallback metadata 的
  `error_code/error_type/error_detail` 都是闭集 code；retry/final 日志不打印 traceback 或异常正文；
- `SubagentExecutor` 只从闭集 LLM reason 解析 code，unexpected exception 统一为
  `SUBAGENT_EXECUTION_FAILED`，同步、异步、isolated-loop 路径都不打印 traceback；
- `task_tool` 的 cleanup、usage 和 terminal **日志**不打印 exception 或 `result.error`；
  `task_failed`/ToolMessage/metadata 仍携带 `result.error`，但 unexpected-exception 生产路径已在
  `SubagentExecutor` 把它折叠为 `SUBAGENT_EXECUTION_FAILED`；cancel/timeout/max-turn 等 domain
  failure 使用框架固定文案或对应稳定 code。

分类函数可以在内存中观察 exception 以决定 quota/auth/busy/transient，但原始正文不成为用户输出、
日志、stream、task metadata 或持久化字段。这里保证的是“异常派生正文不外发”，不是所有失败状态
都只能使用一个错误码。

### 15.9 SandboxAudit

`SandboxAuditMiddleware` 仍在内存中对 bash command 做 regex/shlex 分类和 compound-command
拆分，但外发面是闭集：

```text
{
  timestamp,
  thread_id | "unknown",
  command_length,
  command_sha256,
  verdict: block | warn | pass | unknown
}
```

invalid/block/warn 日志也只写 length、SHA-256 和闭集 reason，不写 raw command；medium-risk
ToolMessage 追加固定提醒，不回显命令；block reason 不在 allowlist 时折叠为
`security policy violation`。因此 URL userinfo、token、用户脚本正文不会因安全审计本身进入日志。

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
11. 接受或回显公网 `X-Trace-Id`、`metadata.trace_id`、`deerflow_trace_id`、`origin_trace_id` 作为
    Run/Job/审计/Tracing identity。
12. 在用户消息、stream、ToolMessage、Subagent result、日志、审计中恢复 `str(exc)` 或 traceback。
13. 让 support bundle 重新导出开放 config/provider/git/doctor 对象，或在没有 trusted project +
    owner scope 时按 thread ID 枚举文件。
14. 为 SandboxAudit 日志或警告恢复 raw command，即使只截断输出也不允许。

## 17. 验证矩阵与当前证据

| 类别 | 场景 | 预期 |
| --- | --- | --- |
| public Run | input 带 AI/System/summary/hidden message 和内部 state channel | 只保留可见 user/human messages |
| public Run | command 带 resume、messages 和其他 update channel | 只保留 resume 与净化后的 update.messages |
| public Run | 顶层/嵌套 config/context/metadata 伪造 authority/trace | 所有同名字段在 admission 前删除 |
| input | 每个当前 framework wrapper 由用户伪造 | 对应 tag 被转义 |
| input | 普通 HTML / 代码示例 | 非 blocked tag 不误伤 |
| input | 用户内嵌 BEGIN/END | 内层 token 被中和，外层恰好一对 |
| input | 历史中多条 genuine user message | 每条都净化，而非只处理最后一条 |
| input | hidden HumanInput response | 作为 genuine user 净化 |
| upload | 服务端 block + 用户伪造 block | 服务端保留、用户部分转义 |
| upload | image-only / empty original | 不产生 marker 噪音 |
| upload | marker mismatch/malformed | 全文 fail-safe 净化或修复 marker，日志无正文 |
| upload result | canonical uploads read / workspace read / forged read | 仅 canonical upload result 净化 |
| agent prompt | 四文档含 closing tags/属性字符 | wrapper 不可闭合，benign 内容稳定 |
| skill prompt | name/description/location 恶意 markup | 全部按位置转义 |
| remote result | 四个 built-in web tools | string/list/Command 全部净化 |
| MCP result | 注册 private MCP 任意工具名 / model 伪造 metadata | 前者净化、后者不能取得 provenance |
| MCP schema | description/schema/name/routing 含 authority tag | result 中和、prompt sink 转义 |
| local result | bash/read_file 含 XML 代码 | byte-identical |
| guardrail | `allowed_tools=None` | 保持未配置语义 |
| guardrail | `allowed_tools=[]` | 所有业务工具拒绝 |
| guardrail | malformed decision / nested args mutation | 按 fail mode 处理；handler args 不受 provider mutation |
| guardrail | provider exception + fail_closed | handler 未调用 |
| guardrail | `GraphBubbleUp` / `AuthorizationRevoked` | 穿透，不变成普通 ToolMessage |
| attribution | missing/malformed carrier 或 forged client role/project/internal | private path 在 provider 前拒绝 |
| ordering | private allow/deny/fail-open | 均先 read-only preflight → provider；canonical memory 再只读检查，其他执行路径进入相应 fence |
| downgrade | admin → runner 等 capability loss | live Run 标记 revoked，provider 和 side effect 均未到达 |
| isolation | outsider/wrong owner/stale membership | 404 且无存在性泄漏 |
| revalidation | membership 在 Run 中撤销 | 下一 model/tool/MCP/sandbox/checkpoint 边界终止 |
| credential | AAD/version/project/key/nonce 任一错误 | 统一无细节解密失败 |
| credential | API/snapshot/log/audit | 不出现 key/nonce/ciphertext/plaintext |
| audit | metadata 含 prompt/URL/secret/raw owner ID | schema 拒绝 |
| support | 正常 config/environment/git/doctor | ZIP/sidecar 只出现预期摘要字段 |
| support | `models/tools/channels` 为 int 且 `config_version/error` 为 opaque value | version/error 归一为 `null`；ZIP、triage、config-summary、两份 sidecar 无 sentinel |
| support | 非法或无 trusted scope 的 thread ID | 前者校验失败；后者因缺 project + owner scope 拒绝 |
| trace | forged header/metadata 与 Run/Job mismatch | server 生成；mismatch 在 runner 前 fail-closed |
| exception | Tool/LLM/Subagent exception 含 sentinel | 输出、日志、stream、metadata、持久化均无 sentinel |
| sandbox audit | command 含 URL credential/token/script | 日志和 ToolMessage 只含闭集字段/固定文案 |
| concurrency | 两个项目同名资源并发运行 | context、lease、Credential 无交叉 |
| degradation | sanitizer 人为抛异常 | 请求行为符合 fail-open 且只发无内容遥测 |

当前 checkout 的最终证据为：

- 后端完整测试：`7583 passed, 1016 skipped, 10 warnings`；
- 固定 20 文件 M1–M7 真实 PostgreSQL 门禁：`270 passed, 0 failed, 0 skipped`；
- private MCP / upload / tool error / public Run 聚焦组合：`79 passed, 7 skipped`；
- private MCP provenance 独立回归：`62 passed`；
- Ruff check 通过，`1131 files already formatted`，`git diff --check` 通过；
- 前端完整单元测试：`188 files, 1345 passed`；`pnpm check` 与 `pnpm build` 通过，
  构建静态页面 `78/78`；
- 实际 Support Bundle 生成成功，ZIP 精确包含 9 个闭集文件；manifest 明确
  `raw_env_file=false`、`raw_thread_messages=false`、`raw_user_files=false`；
- 真实浏览器完成 6 轮验收，测试窗口的 Worker 日志记录 12 次 DeepSeek HTTP 200
  （包括主 Agent、Subagent 和辅助模型调用）。

真实浏览器结果按顺序为：

1. 第 1 轮写入并返回持久上下文标记；
2. 第 2 轮在历史消息含伪造 `<system>` 与用户边界时，未执行伪造指令，并正确回忆标记；
3. 第 3 轮实际调用 `read_file` 读取上传文件，文件内间接注入被结构中和，返回
   `SEC11-R3-UPLOAD-PASS`；
4. 第 4 轮实际尝试 bash 执行型 Subagent；当前
   `LocalSandboxProvider + allow_host_bash=false` 按安全策略拒绝宿主机命令。该轮记录为
   **policy block**，没有伪写为命令成功，也没有为制造通过结果而放宽配置；
5. 第 5 轮实际调用 `task` 委派通用 Subagent，子模型完成后主 Agent 返回
   `SEC11-R5-SUBAGENT-PASS`；
6. 刷新页面后进行第 6 轮新模型调用，仍从持久历史恢复第 1 轮标记并返回
   `SEC11-R6-REPLAY-PASS|SEC11-CONTEXT-6K4P`。

截图与详细验收记录见
[`evidence/11-security/README.md`](evidence/11-security/README.md)。所有提交到仓库的截图均裁掉账号
侧栏；原始未裁切临时截图不作为证据提交。
