# Configuration Guide

This guide explains how to configure ActWeave for your environment.

## Config Versioning

`config.example.yaml` contains a `config_version` field that tracks schema changes. When the example version is higher than your local `config.yaml`, the application emits a startup warning:

```
WARNING - Your config.yaml (version 0) is outdated — the latest version is 1.
Run `make config-upgrade` to merge new fields into your config.
```

- **Missing `config_version`** in your config is treated as version 0.
- Run `make config-upgrade` to auto-merge missing fields (your existing values are preserved, a `.bak` backup is created).
- When changing the config schema, bump `config_version` in `config.example.yaml`.
- A config version newer than the current checkout is rejected rather than
  guessed or downgraded; use code that explicitly supports that version.
- The current example schema is **version 1**, the initial public configuration
  contract. All current settings are part of this baseline, including exact
  one-time Local Provider command approval, PostgreSQL-owned Automation and
  runtime policies, bounded stream/MCP/Skill settings, and CIDR-based Project
  MCP policy. There are no earlier public configuration versions.
- `make config-upgrade` can normalize an unversioned pre-release file into v1.
  An empty retired Project MCP endpoint list becomes an empty deny-all network
  list; a nonempty endpoint list requires an explicit operator-selected CIDR
  and stops without writing. Removed YAML policy fields are deleted, while
  deployment-owned siblings such as custom prompt text and storage paths remain.

## Configuration Sections

### Project assets

Agent, Skill and MCP definitions are versioned PostgreSQL assets. System admins
只读治理 packaged bootstrap system catalog；项目成员在经过认证的 UI/API 中管理
project assets。`config.yaml` 只管理进程和运行时设置。

### System model output limits

模型的 `settings.max_tokens` 是单次 provider 调用的生成上限，不是整个 Agent
Run 的累计预算。管理员在系统模型设置中创建新版本并切换 current；
`config.yaml` 不是这个值的权威来源。新空库的 DeepSeek Flash/Pro 初始版本默认
为 `51_200`，但 bootstrap 不会覆盖已有数据库中的不可变模型版本。

Private Run 的 lead 模型如果以 `length` / `max_tokens` /
`max_output_tokens` 或 Responses API 的等价 incomplete reason 结束，且当前响应不含
工具意图或结构化输出，Worker 会在同一 Run 内最多执行一次恢复调用。该调用使用
同一冻结模型配置，关闭 thinking、禁用工具，并要求根据原始任务和可见的部分答案重新
形成简洁完整的最终答案；它不会把被截断的隐藏推理当作可靠的续写上下文。
当前响应含工具/结构化意图、整体 token 预算已 hard-stop，或唯一恢复调用仍被截断/
没有可见正文时，Run 以稳定的 `MODEL_OUTPUT_LIMIT` 非重试终态结束。前端显示专用
提示，并仅在该失败轮没有任何工具活动时提供“关闭深度思考后重试”。

### Project MCP network policy

Project-authored remote MCP uses secret-free HTTP(S) base URLs and accepts only canonical IP
literals inside `mcp_security.project_remote_allowed_networks`. The defaults cover IPv4 loopback,
RFC 1918, IPv6 loopback, and IPv6 ULA. An empty list denies every remote Project MCP; `/0` grants
the corresponding entire address family and should be an explicit operator choice. Any valid path
or port is allowed once the IP is inside a configured network.

Exact `localhost` is normalized to `127.0.0.1` before policy validation and persistence. Use
`[::1]` explicitly for IPv6. Other DNS hostnames are rejected: resolving a name during validation
and resolving it again during the HTTP connection would create a DNS-rebinding/TOCTOU boundary,
not a trustworthy CIDR check. In containers, `localhost` therefore means the Worker container
itself; use the target's private IP literal for another container.

`require_egress_proxy` defaults to `false` and is independent of the CIDR list. When set to `true`,
a valid `egress_proxy_url` is mandatory. HTTP transmits header/query Credentials in plaintext and
is suitable only for a trusted isolated network. Use HTTPS on untrusted links; certificates for
IP-literal URLs need a matching IP SAN. When traffic uses a forward proxy, `127.0.0.1` is resolved
from the proxy's network namespace rather than the Worker, and the proxy must independently enforce
the intended network boundary.

### Project Memory

项目 Memory 的数据与策略都以 PostgreSQL 为唯一权威。system admin 在
`/admin/settings/system` 的 Memory 页面维护两个独立、版本化并使用各自 revision CAS 的
策略分区：`agent_runtime.memory` 负责运行行为，`memory_document` 负责新文档章节模板。
`config.yaml` 不接受这两类数据库策略，也不存在文件回退。

`agent_runtime.memory` 恰好包含六个字段：

- `enabled`：平台总开关。关闭后 Thread 压缩仍可维持短期上下文，但不新增 history、不准入
  Dream，也不向模型注入长期文档。
- `model_name`：Dream 使用的系统模型名；Dream 准入冻结精确模型版本。SNIP 继续使用独立的
  summarization 模型配置，并在归档回执中记录其精确版本。
- `dream_interval_minutes`：Scheduler 自动准入 Dream 的间隔，范围 `15..1440`，默认 `120`。
- `max_injection_tokens`：Run 准入时允许冻结的完整 Memory 文档上限。文档超限时不会静默截断，
  也不会阻断 Run；该 Run 不创建 Memory 快照、不注入这份文档，并在同一准入事务追加只含
  `reason=over_budget` 的 `memory.injection.skipped` 审计事件。摘要、正文、查询或 diff 不会进入
  审计。`GET /api/projects/{project_id}/memory` 默认返回的二态 `injectionStatus` 是滚动兼容
  投影，只根据当前文档与当前预算即时派生；它不检查平台或账号开关。新客户端可显式传
  `injectionContract=advisory_v1`，读取 `injectionAdvisory` 的
  `eligible | skipped_over_budget | inactive` 与原因。该 opt-in 判定器和普通新 Run 准入共用
  平台开关、账号偏好、文档完整性、章节结构和预算口径；完整性损坏继续 fail closed，不会
  包装成普通 200 `invalid`。它仍只是读取事务内的当前证据，不会锁定未来状态，也不是某个
  Run 已经注入 Memory 的权威证明；continuation Run 不读取当前文档或当前开关，而是从源 Run
  继承冻结快照或“不注入”结果，并对继承快照重新执行当前预算和完整性检查，因此不受这个
  current-non-continuation advisory 描述。
  无待整理 history 的超预算文档可由 Scheduler 或“立即整理”准入零 history 的
  `budget_rewrite` Dream；压缩后的版本重新满足预算后，后续 Run 才会恢复注入。文档摘要、结构
  或章节合同损坏仍保持 fail closed，不会被归类为可恢复的超预算跳过。
- `idle_seal_minutes`：空闲 Thread 自动封存阈值；`0` 关闭，否则范围 `30..10080`，默认
  `1440`。
- `episode_retention_days`：归档 episodes 的保留天数；`0` 永久保留，否则范围
  `30..3650`，默认 `365`。

`memory_document` 的值严格为 `{sections: string[]}`：2～8 个有序纯标题，每项 trim 后
非空、最多 80 个 Unicode 字符，且不能包含控制字符、Markdown 标题前缀、Dream history
标记或重复标题。某个 project/owner 作用域首次创建 Memory 文档时，当前章节列表及其精确
策略版本会冻结到文档；Run 快照继续复制并冻结该列表。因此管理员后续修改只影响新建
文档，不会迁移已有文档、重排旧版本或取消在途 Dream。

账号级 `memory_enabled` 与平台 `enabled` 共同生效。打包 SNIP 提示词输出两段：续航散文写入
Thread `summary_text`，独立的 tagged 段进入待整理 history；自定义 summary prompt 继续保持
单段兼容语义。Scheduler 或手动入口只准入一个 `memory_dream` Job，每批严格取最老 20 条。
Worker 整理整份 Markdown 文档并在同一事务写版本、server diff、cursor、history tombstone 与
Job 终态。新 Run 在准入事务冻结完整文档及章节合同，之后只通过 Worker 签发的 opaque
authority 读取该快照；没有 Fact/Candidate Pipeline、`memory_search`、向量排序或旧版
回退，`recall_memory` 只检索同作用域的 PostgreSQL episodes。

项目页使用 `/api/projects/{project_id}/memory`、`/pending`、`/episodes`、`/dream` 和
`/versions` 系列接口查看当前文档、按 Dream 顺序查看待整理条目、浏览或检索保留期内的 episodes、
立即整理、查看真实 diff 及 CAS 恢复。账号关闭会从下一模型边界停止使用而不删除数据；reset
清除长期 Memory 数据和 Run Memory 快照，但保留 Thread、消息、文件及 Thread 摘要。

### Checkpoint 表示

LangGraph checkpoint 支持完整值与增量消息两种表示。默认保持兼容模式：

```yaml
database:
  checkpoint_channel_mode: full
  checkpoint_delta:
    snapshot_frequency: 10
```

- `full` 在每次 checkpoint 中保存完整消息状态；`delta` 保存消息增量，并按
  `snapshot_frequency` 周期写入完整 seed。
- 两个字段都在 graph 编译时生效，属于 restart-required 配置。Gateway 与所有 Worker
  必须使用完全相同的 mode 和 frequency；修改后应同时重启，不能热切换。
- 支持从既有 `full` Thread 迁移到 `delta`。写入第一个 delta checkpoint 后，不支持直接
  切回 `full`；full 进程会在读取或写入前 fail closed，避免把局部增量误当成完整状态。
- 私有项目 Thread 的读取、历史、Goal、分支、重新生成、上下文压缩与 Worker 恢复都必须经
  `ProjectScopedCheckpointer` 和 materialized state accessor。不要直接读取 raw
  `checkpoint.channel_values.messages`。
- `snapshot_frequency` 只影响写入放大与读取重放上界，不改变 reducer 的最终语义；数值越大，
  完整 seed 越少，但一次读取最多需要重放更多增量。

旧字段 `database.checkpoint_delta_snapshot_frequency` 会迁移到 nested 配置并记录弃用警告；
新旧字段同时存在时以 `database.checkpoint_delta.snapshot_frequency` 为准。

### 本地账号、注册和保持登录

本地邮箱/密码认证的部署设置仍位于 `auth.local`，但访客自助注册开关已迁到 PostgreSQL：

```yaml
auth:
  local:
    # 默认必须保持 false；仅在运维明确接受公网明文 HTTP 风险时使用。
    allow_insecure_persistent_cookie: false
```

- system admin 在 `/admin/settings/system` 修改 `auth.allow_registration`；`false` 只关闭普通
  local self-registration。Gateway 会在 rate-limit
  和写账号之前返回结构化 `403 registration_disabled`，`/api/v1/auth/setup-status` 同时返回
  `registration_enabled: false`，前端不会显示可用的普通注册入口。
- setup-status 只缓存数据库初始化状态，`registration_enabled` 每次响应都重新读取，因此配置
  收紧不会被旧 setup cache 暂时遮蔽。
- `auth`、`auth.local`、`auth.oidc` 与每个 OIDC provider 都拒绝未知字段。类似
  `allow_registraton`、`auto_create_user` 的拼写错误会让配置加载失败，不会静默使用默认允许值。
- 首次 system-admin 的 `/api/v1/auth/initialize` 不受这个开关影响；OIDC 的受控用户
  provisioning 也不是 local self-registration。
- 邮箱作为一个大小写不敏感的账号标识保存和查询。register 与 `/initialize` 同时要求用户名：
  3–32 位、字母开头、仅 `[a-z0-9_]`，入库小写；login 可用邮箱或用户名。change-email 与 OIDC
  最终经过规范化的 PostgreSQL user repository，完整 schema 以唯一
  `lower(users.email)` index 和 `users.username` 部分唯一索引封闭并发碰撞。
- “保持登录”由每次 local login、register、initialize、change-password 或 OIDC state 的
  `remember_me` 选择控制。浏览器只能保存 preference 和可选邮箱或用户名；密码、access token、CSRF
  token 与 session ID 不得进入 Web Storage。
- `remember_me=false` 时 access、CSRF 和 preference cookie 都没有 `Max-Age`。
  `remember_me=true` 时，HTTPS 与 localhost HTTP 可持久到 `auth` token lifetime；普通公网
  HTTP 默认仍是 session cookie。只有显式设置
  `allow_insecure_persistent_cookie: true` 才放宽最后一条。
- 这个选项不改变认证 authority：JWT 仍包含随机 `sid`，PostgreSQL 只保存其 hash，每次请求仍
  验证 user、token-version、session expiry 和 revoke 状态。logout 删除全部三个浏览器 cookie
  并撤销当前 durable session；密码变更撤销全部旧 session。

ActWeave 当前没有启用 generic `AuthorizationProvider` 配置。旧顶层 `authorization:` 不会被
静默忽略：它是 v1 的 fail-closed tombstone，直接加载会失败；从未版本化的预发布配置运行
`make config-upgrade` 会删除它。
项目权限继续来自 server-issued ProjectContext、当前 membership/capability、owner scope 和
side-effect revalidation。

`lower(email)` 是完整数据库 schema 的一部分。已处于受支持 Alembic 链上、marker 为已知
祖先 revision 的数据库，先备份后只能通过 `make upgrade-db` 显式升级；新装仍只接受空目标并
运行 `make setup-db`，直接记录当前链头 revision `model_catalog_simplify`。未知/legacy marker 或 catalog
drift 保持 fail-closed，必须换空目标；不要
在运行时 `ALTER`、手工 stamp，新增 schema 变更必须同时维护 ORM、`full_schema.sql` 与正式迁移。

打包 System Asset 的发布与 schema 升级是两个显式动作。更新 `skills/public/` 并生成新的
catalog release 后，存量部署应先停止 Gateway、Worker、Scheduler，在维护窗口从仓库根目录
运行：

```bash
make upgrade-system-assets
```

该命令只接受当前 schema head，保留全部历史 Skill 版本和既有项目 pin，并可幂等重跑；它不会
迁移 schema，也不会在应用启动时自动执行。空库先用 `make setup-db`，behind 库先备份并运行
`make upgrade-db`。若命令报告“结果不确定”，先检查数据库，再安全重跑同一命令。

### 认证反向代理

登录、注册和邀请兑换的限流以真实客户端 IP 为输入。Gateway 只接受受信代理提供的
`X-Real-IP`，并会把该值解析为单个规范 IPv4/IPv6 地址；无效值或逗号分隔链会回退到
TCP peer，不能用来绕过限流。

```yaml
auth:
  trusted_proxies:
    - 127.0.0.1/32
    - ::1/128
```

默认 loopback 仅用于仓库自带的本机 Nginx。Docker Compose 会生成独立的
`DEER_FLOW_PROXY_AUTH_TOKEN`，由 Nginx 覆盖内部证明 Header，Gateway 以常量时间比较后
才接受动态容器/Pod 地址转发的 `X-Real-IP`。该 token 不得与
`DEER_FLOW_INTERNAL_AUTH_TOKEN` 或 `AUTH_JWT_SECRET` 复用，也不得写入 YAML、日志或版本库。
自定义反向代理应优先配置精确 CIDR；使用自管部署 Secret 时必须同时提供
`BETTER_AUTH_SECRET`、`DEER_FLOW_INTERNAL_AUTH_TOKEN` 和
`DEER_FLOW_PROXY_AUTH_TOKEN`。

### Models

Model configuration is not part of the v1 `config.yaml` contract. Top-level
`models:` is a rejected tombstone; `make config-upgrade` removes it while
normalizing an unversioned pre-release file. The setup wizard does not write
model definitions to `config.yaml`.

For a new local database, place `DEEPSEEK_API_KEY`, `OPENCODE_API_KEY`,
`DEER_FLOW_CREDENTIAL_ACTIVE_KEY_ID`, and
`DEER_FLOW_CREDENTIAL_KEYRING_JSON` in the root `.env` (an explicitly exported
value has precedence), then run `make setup-db`. Before it creates the target
database, setup validates the keys and keyring and pre-encrypts the keys. It
then writes a system `model_api_key` Credential/envelope shared by active
DeepSeek V4 Flash and DeepSeek V4 Pro configurations, plus a separate OpenCode
Credential/envelope for GPT 5.6 Luna, in one transaction. Flash is selected as
the default. Missing or invalid bootstrap secret material fails without
leaving a newly created half-initialized database.

After application startup, sign in as a system admin and use
`/admin/settings/models` to inspect or change the catalog:

1. create a model catalog entry and immutable provider version;
2. choose an allowlisted provider adapter and model ID;
3. add bounded, secret-free JSON settings such as `base_url`, `temperature`, or
   provider-specific request options;
4. bind the exact encrypted Credential version and its environment key when the
   adapter requires authentication;
5. activate the model and choose the catalog default.

The authorable model adapter allowlist includes OpenAI, Anthropic, DeepSeek,
vLLM, and the patched OpenAI and DeepSeek adapters. Vision Bridge uses these
same adapters and does not add a second adapter family.

Arbitrary Python class paths are not accepted. Secret-bearing keys, headers,
tokens, passwords, and Credential material are rejected from model settings.
Provider secrets are stored only as encrypted Credential envelopes and are
decrypted for the exact admitted model version at the execution boundary.

`vision_openai_compatible_v1` has no production descriptor or runtime class
path. An unknown historical provider string may remain admin-readable for
diagnosis, but cannot be authored, reactivated, made default, bound to a new
Runtime Policy, admitted, or materialized.
`vision_bridge_fake` is not a production adapter and may only be injected by
tests.

The current wire-protocol families are:

| Protocol                           | Model adapters                                                                                        |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------- |
| OpenAI-compatible Chat Completions | `openai` when Responses is not selected, `deepseek`, `vllm`, `patched_openai`, and `patched_deepseek` |
| OpenAI Responses                   | `openai` and `patched_openai` with `use_responses_api=true`                                           |
| Anthropic Messages                 | `anthropic`                                                                                           |

Vision, thinking, reasoning effort, tool calling and structured output are
model capabilities layered on a protocol. `supports_vision=true` makes an
active model selectable for visual work; the administrator connection probe
and target-environment staging must still verify the claimed capability.

#### Text-model Vision Bridge

`inspect_image` is a reserved conditional built-in tool. It is not listed under
`tools:` or `tool_groups:` in `config.yaml`; declaring that name there fails
closed. Vision Bridge model selection is PostgreSQL System Runtime Policy:

1. In `/admin/settings/models`, inspect the seeded model or create an active
   model with `supports_vision=true` using one of the ordinary authorable
   Provider adapters. Bind its exact encrypted Credential when the adapter
   requires one, then run **Test connection** to exercise that adapter's
   multimodal path.
2. In `/admin/settings/system`, open **Image inspection** and confirm or select
   that model.
   A non-null `agent_runtime.vision_bridge.model_name` enables later text-only
   project Runs; clearing it disables the bridge. There is no second `enabled`
   flag and no project egress grant. Fresh installations select the seeded
   `gpt-5.6-luna` model by default. This bootstrap value is not evidence of an
   organization's provider-policy approval; clear it before accepting project
   Runs in a production deployment whose data-egress review is incomplete.
3. `timeout_seconds` is the single image-read-through-result deadline; fresh
   installations default it to 60 seconds.
   `contract_version` remains fixed to `vision.bridge.v1` for existing Runtime
   Policy and Run snapshot compatibility. It is a platform control-contract
   version, not a Provider wire-protocol selector.

`analysis_goal` is required by the current v1 tool input. Before deploying this
change, drain pending, running, or paused Runs that may already contain an
unexecuted legacy `inspect_image` call without that field. Completed historical
tool results remain readable and keep their existing result schema.

The policy accepts any active current model whose adapter remains eligible for
new binding and whose model version declares `supports_vision=true`. It does not
hard-code Luna, OpenAI, Chat Completions, Responses, or another Provider. Luna
is only the fresh-install default.

At Run admission, a text-only lead freezes the selected model as an exact,
secret-free `purpose="vision"` snapshot. Worker materialization resolves that
version and Credential into the Run-isolated `AppConfig`. Retry and resume use
the same snapshot; neither the tool nor current catalog defaults may change the
model, Endpoint, adapter, or Credential reference.

`inspect_image` uses the same `deerflow.models.ModelRuntime` as all other model
calls. It sends one fixed System instruction and one Human message containing
the fixed mode task, the required bounded `analysis_goal` supplied by the lead,
and the normalized image block. The selected
model's existing adapter owns Provider SDK construction, authentication,
Endpoint handling, request serialization, and response parsing. Bridge code
does not construct Chat Completions or Responses HTTP requests and does not
require the Provider to generate an ActWeave-specific JSON Schema.

The request contains only the normalized image, fixed mode instructions, and
the lead's bounded analysis goal; paths, full chat history, Memory, other
attachments, and authority metadata are not sent. The Provider returns a
normal `AIMessage`. The server rejects empty, refusal, tool-call, ambiguous, or
otherwise invalid responses and wraps valid text in a bounded
`inspect_image.result.v2` ToolMessage whose content type is
`untrusted_image_analysis`. Historical `vision.evidence.v1` ToolMessages remain
readable during the compatibility period, but new calls write v2.

The administrator **Test connection** action for a model declaring visual
support sends only a platform-generated 64x64 blue-square PNG through the same
`ModelRuntime` and Provider adapter. The synthetic probe contains no project,
user, or uploaded-file data. Probe success is a current connectivity check, not
proof of production image quality or provider-policy approval.

The one runtime-policy timeout covers file read, normalization, dispatch, the
single Provider invocation, response validation, and ToolMessage generation.
The sensitive multimodal Runtime profile suppresses inherited/model content
tracing and sets Provider internal retries to zero. Run-level calls, bytes, and
pixels remain bounded. Every possible external call obtains durable dispatch
authority before invocation and submits a usage receipt afterward; dispatched
work without trusted usage keeps `usage_unknown=true` and is not automatically
replayed. A suspended System Model or revoked Credential blocks a later call,
but cannot recall a request already in flight.

There is no second `enabled` flag, project grant, Bridge adapter, or Bridge
Provider client. Complete provider policy, staging quality, latency, and
rate-limit acceptance before enabling project Runs. Current architecture,
implementation status, legacy cleanup gates, and the complete acceptance matrix
are documented in
[VISION_BRIDGE_REFACTOR_PLAN.md](./VISION_BRIDGE_REFACTOR_PLAN.md).

Gateway, Worker, Scheduler, and Docker Compose do not receive the local
bootstrap provider key as a process-wide environment block. Each Run records a secret-free,
immutable model-version snapshot so later catalog changes do not silently alter
already admitted work. `make doctor` reports whether the PostgreSQL catalog has
an active current model; it never inspects YAML model entries or tests a
provider key.

### Tool Groups

Organize tools into logical groups:

```yaml
tool_groups:
  - name: web # Web browsing and search
  - name: file:read # Read-only file operations
  - name: file:write # Write file operations
  - name: bash # Shell command execution
```

### Automations

Automation polling, the global concurrent occurrence cap, and the one-time
schedule minimum delay are PostgreSQL system policy (`automations` on
`/admin/settings/system`), not a `config.yaml` section. Scheduler process
presence is still an operator choice (`make start` always starts it locally;
Compose keeps the `scheduler` profile). Edits take effect on later Gateway
requests and the next Scheduler poll; they do not interrupt already admitted
work. `enabled: false` stops scheduled admission only. Manual project triggers
and Memory Dream/Seal polling continue.

Notes:

- `max_concurrent_runs` is a global cap on active Automation occurrences
  (queued/running occurrence rows) shared by scheduled and manual triggers.
- Scheduler state, occurrence admission and Run jobs always use the configured PostgreSQL database.
- Thread reuse and fresh-thread-per-run execution modes remain available.
- Supported schedules are `once` and `cron`.

### Tools

Configure specific tools available to the agent:

```yaml
tools:
  - name: web_search
    group: web
    use: deerflow.community.tavily.tools:web_search_tool
    max_results: 5
    # api_key: $TAVILY_API_KEY  # Optional
```

**Built-in Tools**:

- `web_search` - Search the web (DuckDuckGo, Tavily, Brave, Exa, InfoQuest, Firecrawl, fastCRW, GroundRoute)
- `web_fetch` - Fetch web pages (Jina AI, Crawl4AI, Exa, InfoQuest, Firecrawl, fastCRW, GroundRoute, Browserless)
- `web_capture` - Capture rendered webpage screenshots as artifacts (Browserless)
- `image_search` - Search for reference images (DuckDuckGo, InfoQuest, Serper, Brave)
- `ls` - List directory contents
- `read_file` - Read file contents
- `write_file` - Write file contents
- `str_replace` - String replacement in files
- `bash` - Execute bash commands

Browserless can be configured as an opt-in visual capture tool:

```yaml
tools:
  - name: web_capture
    group: web
    use: deerflow.community.browserless.tools:web_capture_tool
    base_url: http://localhost:3032
    # token: $BROWSERLESS_TOKEN
    output_format: png
    full_page: true
    viewport_width: 1280
    viewport_height: 720
    # allow_private_addresses: false  # SSRF guard; keep false in production
```

`web_capture` writes screenshots to the current thread's `/mnt/user-data/outputs`
directory and presents the image path through the standard artifact mechanism. By
default it refuses URLs that resolve to private, loopback, link-local, or
cloud-metadata addresses; set `allow_private_addresses: true` only when you
intentionally point the tool at an internal target.

Both `web_fetch` (Browserless provider) and `web_capture` need a running
Browserless instance. You can point `base_url` at [Browserless Cloud](https://www.browserless.io/)
(set `BROWSERLESS_TOKEN`) or run one locally with Docker:

```bash
# Browserless listens on port 3000 inside the container; map it to 3032 to
# match the default base_url (http://localhost:3032). Recent Browserless
# images always require a token — if you don't pass one, a random token is
# generated and requests without it are rejected — so set it explicitly.
docker run -d --name browserless -p 3032:3000 -e "TOKEN=local-dev-token" ghcr.io/browserless/chromium
```

Then set the same token so the tool sends it (uncomment `token: $BROWSERLESS_TOKEN`
in the config above):

```bash
export BROWSERLESS_TOKEN=local-dev-token
```

Verify the instance is reachable before enabling the tool:

```bash
curl -sS "http://localhost:3032/screenshot?token=local-dev-token" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "options": {"type": "png"}}' \
  -o /tmp/browserless-check.png  # writes a PNG on success
```

For Docker Compose deployments, run Browserless as a service and point `base_url`
at the service name (e.g. `http://browserless:3000`) instead of `localhost`. See
the [Browserless project](https://github.com/browserless/browserless) for full
deployment and configuration options.

### Sandbox

ActWeave supports multiple sandbox execution modes. Configure your preferred mode in `config.yaml`:

**Local Execution** (runs sandbox code directly on the host machine):

```yaml
sandbox:
  use: deerflow.sandbox.local:LocalSandboxProvider # Local execution
  allow_host_bash: false
  host_execution_approval:
    mode: disabled # default: host Bash is unavailable
    request_ttl_seconds: 300
    max_timeout_seconds: 600
    execution_domain_id: null
    execution_domain_label: Worker host environment
```

`LocalSandboxProvider` is a host-side path mapper and process runner, not a
security sandbox. If an interactive private conversation needs Local Bash,
prefer per-command approval over the legacy direct switch:

```yaml
sandbox:
  use: deerflow.sandbox.local:LocalSandboxProvider
  allow_host_bash: false
  host_execution_approval:
    mode: approval_required
    request_ttl_seconds: 300
    max_timeout_seconds: 600
    execution_domain_id: mac-primary-worker
    execution_domain_label: My local Worker
```

In `approval_required` mode, every Lead or delegated Agent Bash call freezes its
exact logical command and requires a separate `allow_once` or `deny` decision.
This includes Python launched by Bash, pipelines, redirects, and here-documents.
The standalone `write_file` tool does not require host-execution approval, but a
Bash command that writes a file does. One approval authorizes one top-level Shell
launch; the Shell can still start multiple child processes and produce
irreversible side effects. There is no session/thread/permanent grant.

Local `approval_required` requires an operator-provisioned
`execution_domain_id`. The ID is private and must be unique per intended Worker
OS namespace; the Worker additionally binds a secret-free device/namespace,
uid/gid, runtime-base and sanitized-environment fingerprint, so copying the ID
to another machine does not make it eligible. `execution_domain_label` is the
presentation-only approval-card label and must not contain hostnames, usernames,
paths, secrets, control characters, or Unicode bidirectional controls. A
continuation can only be claimed by a Worker with the exact frozen affinity;
an unclaimable queued continuation is cancelled with its Run and quota when the
approval claim TTL expires.

`allow_host_bash: true` remains a dangerous compatibility switch for immediate
host execution. It is mutually exclusive with `mode: approval_required`.
Non-interactive Runs fail closed when approval would be required. Unknown
providers and custom subclasses of trusted isolated providers also fail closed;
only the exact built-in AIO/BoxLite/E2B classes (including re-exports of those
same classes) receive isolated-direct execution authority. See
[HOST_EXECUTION_APPROVAL.md](./HOST_EXECUTION_APPROVAL.md) for lifecycle,
recovery, limits, and current validation status.

**AIO Container Execution** (runs sandbox code in an isolated Docker or Apple Container runtime):

```yaml
sandbox:
  use: deerflow.community.aio_sandbox:AioSandboxProvider
  image: enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:1.11.0
  allow_host_bash: false
```

On a native macOS Worker, the local backend prefers Apple Container when its
CLI is installed. See [APPLE_CONTAINER.md](./APPLE_CONTAINER.md) for the
supported macOS version, startup commands, live verification, and network
boundary. Linux and Compose Workers use Docker. Bash in an explicitly trusted
built-in AIO/BoxLite/E2B provider executes inside that provider without Local approval. Provider startup
or command failure is reported as a failure and never falls back to Local host
Bash.

**BoxLite micro-VM Sandbox** (runs sandbox code in daemonless OCI micro-VMs):

```yaml
sandbox:
  use: deerflow.community.boxlite:BoxliteProvider
  image: python:3.12-slim
  memory_mib: 1024 # optional per-box memory cap
  cpus: 2 # optional per-box vCPUs
  replicas: 3 # max active + warm VMs per gateway process
  idle_timeout: 600 # warm VM idle seconds before stop; 0 disables idle reaping
  environment:
    PYTHONUNBUFFERED: "1"
```

Install the optional runtime before selecting this provider:

```bash
pip install "deerflow-harness[boxlite]"
```

BoxLite boxes are named from the effective `(user_id, thread_id)` scope and are
released into an in-process warm pool after each turn. The same user/thread can
reclaim its warm VM on the next acquire; different threads cannot share a VM.
`replicas` caps active plus warm VMs. When the cap is reached only warm VMs are
evicted; active VMs continue and the provider may temporarily exceed the cap if
all boxes are active.

**Docker Execution with Kubernetes** (runs sandbox code in Kubernetes pods via provisioner service):

This mode runs each sandbox in an isolated Kubernetes Pod on your **host machine's cluster**. Requires Docker Desktop K8s, OrbStack, or similar local K8s setup.

```yaml
sandbox:
  use: deerflow.community.aio_sandbox:AioSandboxProvider
  provisioner_url: http://provisioner:8002
  provisioner_api_key: $PROVISIONER_API_KEY
```

`PROVISIONER_API_KEY` is required in this mode and must have the same value in
the Worker and Provisioner processes. The Provisioner leaves `/health` public,
but fails closed with `401` for every `/api/*` request when the configured key
is empty, missing, or does not match the `X-API-Key` header. The public Nginx
entry does not route Provisioner endpoints directly; lifecycle calls originate
from the backend Worker.

When using Docker development (`make docker-start`), ActWeave starts the `provisioner` service only if this provisioner mode is configured. In local or plain Docker sandbox modes, `provisioner` is skipped.

See [Provisioner Setup Guide](../../docker/provisioner/README.md) for detailed configuration, prerequisites, and troubleshooting.

**E2B Cloud Sandbox** (runs sandbox code in [E2B](https://e2b.dev) cloud micro-VMs):

```yaml
sandbox:
  use: deerflow.community.e2b_sandbox:E2BSandboxProvider
  api_key: $E2B_API_KEY # required; or set the E2B_API_KEY env var
  template: code-interpreter-v1 # e2b sandbox template id
  # domain: e2b.dev                # optional; for self-hosted e2b deployments
  home_dir: /home/user # /mnt/user-data is remapped under this directory
  idle_timeout: 600 # forwarded to e2b's server-side set_timeout()
  replicas: 3 # max concurrent sandboxes per gateway process
  mounts: # one-shot upload of host files at sandbox start
    - host_path: /path/on/host
      container_path: /home/user/shared
      read_only: false
  environment: # forwarded to the sandbox at create time
    WORKLOAD_PROFILE: batch
```

`e2b-code-interpreter` is bundled as a core dependency of `deerflow-harness`,
so no extra install step is needed; just supply your API key and switch the
provider in `config.yaml`.

Notes specific to `E2BSandboxProvider`:

- Each ActWeave thread is bound to its e2b sandbox via metadata
  (`deer_flow_user`, `deer_flow_thread`), so the same thread reuses the same
  sandbox across gateway restarts and across processes — no cross-process
  file lock is needed because the e2b control plane is the source of truth.
- Idle expiry is enforced server-side by e2b's `set_timeout()`. The provider
  refreshes the timeout on every release so warm sandboxes stay alive long
  enough for the next acquire.
- `mounts` are uploaded once when the sandbox starts; e2b cannot host bind-mount
  the gateway filesystem, so changes inside the sandbox are not reflected back
  on disk automatically. Use the `download_file` tool or write outputs under
  `/mnt/user-data/outputs/` (which is mapped to `home_dir/outputs/` inside the
  sandbox and surfaced through the standard artifact pipeline) to ship files
  back to the gateway.

Choose between host-side local execution and container isolation:

**Option 1: Local Sandbox** (default, simpler setup):

```yaml
sandbox:
  use: deerflow.sandbox.local:LocalSandboxProvider
  allow_host_bash: false
  host_execution_approval:
    mode: disabled
    execution_domain_id: null
    execution_domain_label: Worker host environment
```

`allow_host_bash` and host-execution approval are intentionally disabled by
default. ActWeave's local sandbox is a host-side convenience mode, not a secure
shell isolation boundary. If you need Bash, prefer `AioSandboxProvider`. For a
fully trusted, interactive, single-user Local workflow, set
`host_execution_approval.mode: approval_required` to require one decision per
command. Only set `allow_host_bash: true` when immediate, unapproved host
execution is explicitly intended.

When `LocalSandboxProvider` runs under `make up`, Agent commands run inside the
`deer-flow-worker` container. In that mode, `sandbox.mounts[].host_path` is
resolved from the Worker container's filesystem, not directly from your Docker
host. If you need a Local-provider custom mount in production Docker, bind the
host directory into the Worker service first, then use the in-container path in
`config.yaml`:

In Local `approval_required` mode, every configured mount must already have an
absolute, existing `host_path` when configuration loads. Its `container_path`
must be absolute and cannot overlap `/mnt/acp-workspace`, `/mnt/user-data`, or
the configured `skills.container_path`. These stricter startup checks also
apply to Local Provider re-exports and subclasses.

```yaml
# docker/docker-compose.yaml or an override file
services:
  worker:
    volumes:
      - ${DEER_FLOW_REPO_ROOT}/.deer-flow/knowledge:/app/.deer-flow/knowledge:ro
```

```yaml
sandbox:
  use: deerflow.sandbox.local:LocalSandboxProvider
  mounts:
    - host_path: /app/.deer-flow/knowledge
      container_path: /mnt/knowledge
      read_only: true
```

If the configured `host_path` is not visible to the Worker process, ActWeave
logs an error and ignores that mount.

**Option 2: AIO Container Sandbox** (isolated; Docker or Apple Container):

```yaml
sandbox:
  use: deerflow.community.aio_sandbox:AioSandboxProvider
  image: enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:1.11.0
  allow_host_bash: false
  container_prefix: deer-flow-sandbox

  # Optional: Additional mounts
  mounts:
    - host_path: /path/on/host
      container_path: /path/in/container
      read_only: false
```

When you configure `sandbox.mounts`, ActWeave exposes those `container_path` values in the agent prompt so the agent can discover and operate on mounted directories directly instead of assuming everything must live under `/mnt/user-data`.

For bare-metal Docker sandbox runs that use localhost, ActWeave binds the
sandbox HTTP port to `127.0.0.1` by default so it is not exposed on every host
interface. Docker-outside-of-Docker deployments that connect through
`host.docker.internal` keep the broad legacy bind for compatibility. Set
`DEER_FLOW_SANDBOX_BIND_HOST` explicitly if your Docker deployment needs a
different bind address.

Apple Container does not publish a host port. ActWeave reads the managed
container's `status.networks[].ipv4Address` and connects to port `8080` on its
private default-network address. Therefore `sandbox.port` and
`DEER_FLOW_SANDBOX_BIND_HOST` are ignored on the Apple path. Private/loopback
sandbox control traffic bypasses ambient HTTP proxy variables.

### Building a Custom AIO Sandbox Image

`AioSandboxProvider` talks to the sandbox container through the `agent-sandbox`
SDK. The Dockerfile for the pinned
`enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:1.11.0`
image is not part of this repository; ActWeave treats that image as an upstream
AIO sandbox runtime.

For persistent system or language dependencies, extend the published image and keep its startup command intact:

```dockerfile
FROM enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:1.11.0

USER root
# Example user dependency; not required by ActWeave itself.
RUN apt-get update \
    && apt-get install -y --no-install-recommends graphviz \
    && rm -rf /var/lib/apt/lists/*

# Example Python dependency for work done inside the sandbox.
RUN python -m pip install --no-cache-dir pandas

# Do not override ENTRYPOINT or CMD; keep the upstream sandbox server startup.
```

Use the custom image in local Docker or Apple Container mode with `sandbox.image`:

```yaml
sandbox:
  use: deerflow.community.aio_sandbox:AioSandboxProvider
  image: your-registry/your-aio-sandbox:tag
```

In provisioner mode, sandbox Pods are created by the provisioner service, so configure the provisioner `SANDBOX_IMAGE` environment variable instead of `sandbox.image`. See the [Provisioner Setup Guide](../../docker/provisioner/README.md#custom-sandbox-image).

If you rebuild the runtime from scratch instead of extending the published image, it must expose the same HTTP API used by `agent-sandbox`. ActWeave currently depends on:

- `sandbox.get_context()`, including `home_dir`
- `shell.exec_command(...)`
- `bash.exec(...)` — only exercised for per-command environment injection (skills that declare `required-secrets`). The `/v1/bash/*` routes exist since upstream all-in-one-sandbox `1.9.3`; on older images (including a `latest` tag still frozen on the `1.0.0.x` line) ActWeave fails fast with an actionable error instead of surfacing the raw 404. Pin `sandbox.image` to `1.9.3` or newer (e.g. `1.11.0`) and recreate the sandbox container to use `required-secrets` with the AIO sandbox.
- `file.read_file(...)`
- `file.write_file(...)`, including base64 writes for binary content
- streamed `file.download_file(...)`
- `file.find_files(...)`
- `file.list_path(...)`
- `file.search_in_file(...)`

Custom images must also keep these compatibility constraints:

- The container should listen on the configured sandbox port, `8080` by default.
- `/mnt/user-data` must remain writable because ActWeave mounts thread workspace, uploads, and outputs there.
- `home_dir` comes from the sandbox context endpoint; do not assume ActWeave hardcodes it.
- Shell command handling must remain compatible with serialized `exec_command` calls. ActWeave serializes shell access on the host side to avoid corrupting the sandbox's persistent shell session.

### Skills

Configure the skills directory for specialized workflows:

```yaml
skills:
  # Host path (optional, default: ../skills)
  path: /custom/path/to/skills

  # Container mount path (default: /mnt/skills)
  container_path: /mnt/skills
```

**How Skills Work**:

- Skills are stored in `deer-flow/skills/{public,custom}/`
- Each skill has a `SKILL.md` file with metadata
- Skills are automatically discovered and loaded
- Available in both local and Docker sandbox via path mapping

Skill installs and agent-managed skill writes always run through native deterministic SkillScan before the LLM scanner. This mandatory security boundary is not configurable.

**Per-Agent Skill Filtering**:
Custom agents can restrict which skills they load by defining a `skills` field in their `config.yaml` (located at `workspace/agents/<agent_name>/config.yaml`):

- **Omitted or `null`**: Loads all globally enabled skills (default fallback).
- **`[]` (empty list)**: Disables all skills for this specific agent.
- **`["skill-name"]`**: Loads only the explicitly specified skills.

### Title Generation

Automatic conversation title generation is a PostgreSQL system setting
(`agent_runtime.title` on `/admin/settings/system`), not a `config.yaml` leaf.

- `enabled`：whether to generate a title after the first successful exchange.
- `max_words` / `max_chars`：bounds for the generated title.
- `model_name`：logical catalog model; `null` uses the current system default
  model. The exact default is frozen into the Run snapshot at admission.
  Model failure still falls back to a local title derived from the first user
  message.

### GitHub API Token (Optional for GitHub Deep Research Skill)

The default GitHub API rate limits are quite restrictive. For frequent project research, we recommend configuring a personal access token (PAT) with read-only permissions.

**Configuration Steps**:

1. Uncomment the `GITHUB_TOKEN` line in the `.env` file and add your personal access token
2. Restart the ActWeave service to apply changes

## Environment Variables

Process configuration supports environment-variable substitution using the `$`
prefix for fields that remain in `config.yaml`:

```yaml
sandbox:
  provisioner_api_key: $PROVISIONER_API_KEY
```

Model-provider keys are intentionally excluded from runtime configuration
substitution. The local `make setup-db` command consumes `DEEPSEEK_API_KEY` and
`OPENCODE_API_KEY` once to create the encrypted Credentials for the seeded
DeepSeek V4 Flash/Pro models and GPT 5.6 Luna; later keys and rotations are
managed in `/admin/settings/models`. Do not broadcast `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, or similar provider keys to backend runtime processes.

**Common process and tool environment variables**:

- `TAVILY_API_KEY` - Tavily search API key
- `BRAVE_SEARCH_API_KEY` - Brave Search API key for `web_search` and `image_search`
- `SERPER_API_KEY` - Serper (Google Search/Images API) key for `web_search` and `image_search`
- `GROUNDROUTE_API_KEY` - GroundRoute meta-search API key for `web_search` and `web_fetch` (routes across Serper, Brave, Exa, Tavily, Firecrawl, and Perplexity)
- `BROWSERLESS_TOKEN` - Browserless Cloud token for `web_capture` (optional for self-hosted Browserless)
- `DEER_FLOW_PROJECT_ROOT` - Project root for relative runtime paths
- `DEER_FLOW_CONFIG_PATH` - Custom config file path
- `DEER_FLOW_HOME` - Runtime state directory (defaults to `.deer-flow` under the project root)
- `DEER_FLOW_SKILLS_PATH` - Local harness Skill source directory when `skills.path` is omitted; project Run authority still comes from the admitted PostgreSQL snapshot
- `GATEWAY_ENABLE_DOCS` - Set to `false` to disable Swagger UI (`/docs`), ReDoc (`/redoc`), and OpenAPI schema (`/openapi.json`) endpoints (default: `true`)

## Configuration Location

The configuration file should be placed in the **project root directory** (`deer-flow/config.yaml`). Set `DEER_FLOW_PROJECT_ROOT` when the process may start from another working directory, or set `DEER_FLOW_CONFIG_PATH` to point at a specific file.

## Configuration Priority

ActWeave searches for configuration in this order:

1. Path specified in code via `config_path` argument
2. Path from `DEER_FLOW_CONFIG_PATH` environment variable
3. `config.yaml` under `DEER_FLOW_PROJECT_ROOT`, or under the current working directory when `DEER_FLOW_PROJECT_ROOT` is unset
4. Legacy backend/repository-root locations for monorepo compatibility

## Security Notes

### Sandbox Isolation and the Docker Socket (DooD)

ActWeave executes agent-generated shell/code through a configurable sandbox
(`sandbox.use` in `config.yaml`). The isolation guarantees differ by mode, and
one mode requires mounting the host Docker socket. Understand the trade-offs
before exposing an instance to untrusted input.

| Mode                       | `config.yaml`                                                              | Host Docker socket           | Isolation                                                                                                                                                                             |
| -------------------------- | -------------------------------------------------------------------------- | ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `local` (default)          | `deerflow.sandbox.local:LocalSandboxProvider`                              | Not mounted                  | Commands run in the Worker OS namespace. Not a strong boundary. Keep Bash disabled for untrusted workloads; per-command approval adds consent and one-shot accounting, not isolation. |
| `aio` (native macOS)       | `deerflow.community.aio_sandbox:AioSandboxProvider` (no `provisioner_url`) | Not mounted                  | Apple Container runs each sandbox in a lightweight Linux VM. Only explicitly configured/run-scoped mounts expose host paths.                                                          |
| `aio` (Docker / DooD)      | `deerflow.community.aio_sandbox:AioSandboxProvider` (no `provisioner_url`) | **Mounted** (opt-in overlay) | Sandbox containers are started via the host Docker daemon.                                                                                                                            |
| `provisioner` (Kubernetes) | `AioSandboxProvider` + `provisioner_url`                                   | Not mounted                  | Sandbox pods are created through the provisioner's K8s API over HTTP. Strongest isolation.                                                                                            |

#### The Docker socket is host root

Mounting `/var/run/docker.sock` into a container grants that container
**root-equivalent control of the host**: anything able to reach the socket can
start a new container that bind-mounts the host filesystem and escape. This
matters for ActWeave because the gateway executes model-generated commands, so a
prompt injection or any in-container code-execution primitive could pivot to the
host through the socket.

To keep this off the default attack surface:

- The host Docker socket is **not** mounted by the default Compose stack. It is
  added only for `aio` mode through the opt-in `docker/docker-compose.dood.yaml`
  overlay, which `scripts/deploy.sh` and `scripts/docker.sh` append
  automatically when `detect_sandbox_mode()` returns `aio`.
- Prefer **provisioner/Kubernetes mode** for multi-tenant or internet-exposed
  deployments — it isolates sandboxes without handing the gateway the host
  daemon.
- If you must use `aio`/DooD, treat the host as part of the gateway's trust
  boundary: run it on a dedicated host, and consider a scoped Docker API proxy
  instead of the raw socket.

> Note: the base Compose stacks do not bind-mount `$HOME/.claude` or
> `$HOME/.codex`. The opt-in CLI overlay mounts them read-only into Worker only.
> These directories hold long-lived CLI credentials; do not enable the overlay
> for an untrusted Worker.

### CLI Credential Mounts (Claude Code / Codex)

ActWeave can reuse your Claude Code / Codex CLI subscription login for ACP agents
that run the CLI in-container. The Compose stack used to bind-mount the **entire** `~/.claude`
and `~/.codex` directories (read-only) into the gateway container in **every**
configuration — exposing not just credentials but full conversation history,
per-project session data, and global CLI config. A gateway compromise (prompt
injection, tool/MCP misuse, RCE) would leak all of it.

These directories are **no longer mounted by default**. Supply CLI credentials
with the least exposure that fits your setup:

| Need      | How                                                                                                                | Exposure                          |
| --------- | ------------------------------------------------------------------------------------------------------------------ | --------------------------------- |
| ACP agent | Follow the adapter's isolated auth contract; use the CLI overlay only if it genuinely reads the full CLI directory | Adapter-specific / full directory |

The base Compose stack never forwards root `.env` wholesale to Gateway, Worker,
Scheduler, or Provisioner. API-key model adapters must use PostgreSQL Credential
bindings; the opt-in CLI mount is reserved for ACP processes that require the
CLI's local configuration.

## Best Practices

1. **Place `config.yaml` in project root** - Set `DEER_FLOW_PROJECT_ROOT` if the runtime starts elsewhere
2. **Never commit `config.yaml`** - It's already in `.gitignore`
3. **Keep secret domains explicit** - Use environment variables only for documented process/tool secrets; use encrypted Credentials for model-provider keys
4. **Keep `config.example.yaml` updated** - Document all new options
5. **Test configuration changes locally** - Before deploying
6. **Use Docker sandbox for production** - Better isolation and security

## Troubleshooting

### "Config file not found"

- Ensure `config.yaml` exists in the **project root** directory (`deer-flow/config.yaml`)
- If the runtime starts outside the project root, set `DEER_FLOW_PROJECT_ROOT`
- Alternatively, set `DEER_FLOW_CONFIG_PATH` environment variable to custom location

### "Invalid API key"

- Verify environment variables are set correctly
- Check that `$` prefix is used for env var references

### "Skills not loading"

- Project Run：检查 Skill 是否已经发布、绑定到当前项目并被本次 Run snapshot 固定；业务运行不会把仓库目录当作授权来源。
- 独立 harness/TUI：检查本地 Skill 目录、`SKILL.md`，以及 `skills.path` 或 `DEER_FLOW_SKILLS_PATH`。

### "Docker sandbox fails to start"

- Ensure Docker is running
- Check port 8080 (or configured port) is available
- Verify Docker image is accessible

## Examples

See `config.example.yaml` for complete examples of all configuration options.
