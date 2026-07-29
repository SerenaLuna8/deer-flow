# 14. Channels 模块：项目入站解析、去重与 Provider 修复

## 1. 分析边界与结论

本文分析 `main@e317f7b8` 在 IM Channels 和 GitHub webhook 上的更新，并对照
`dev@8a91e957` 的项目私有入站链。重点是：

- Dispatcher/Resolver 的真实调用顺序；
- connection、conversation、topic 和 project/owner 的权威来源；
- process-local 与 cross-pod 去重；
- GitHub review fan-out、author/mention、busy follow-up；
- Feishu、WeCom 和公共流文本修复；
- 哪些 `main` patch 已在 `dev` 出现，哪些必须按 Worker-only 重写。

结论先行：

1. 当前 `dev` 的入站 authority 链是：
   `ChannelManager._handle_project_inbound_chat()`
   → `ProjectInboundDispatcher.dispatch()`
   → `ConnectionInboundResolver.resolve()`。
   不能写成 Resolver 在 Dispatcher 之前，也不能跳过 Dispatcher 直接启动 Run。
2. `ConnectionInboundResolver` 从持久连接重新解析 project/owner/membership，再建立或复用
   `connection + external_conversation + external_topic -> Thread` 映射。`InboundMessage`
   上预先附带的 project/private scope 只可作提示，不能作为执行 authority。
3. 当前去重是 `ChannelManager` 内的进程局部四元组，发生在 authority resolve 之前：
   `(channel_name, workspace_id, chat_id, provider_message_id)`。它没有 project、owner、
   connection 或 topic，也不能跨 Pod。这是确认的架构缺口。
4. `main` 的 PostgreSQL dedupe 可以借鉴原子 conditional upsert，但它仍是旧四元组和
   10 分钟 TTL，不能直接复制到项目私有 schema。正确 key 至少要包含：
   `provider + project_id + owner_user_id + connection_id +
   external_conversation_id + normalized external_topic_id + provider_delivery_id`。
5. GitHub case-insensitive `allow_authors`、per-binding redundant review gate、配置空白 normalize，
   Feishu SDK success check、WeCom null quote guard值得移植。
6. bare `connect` 拒绝、stream delta 合并和项目 Provider allowlist 当前 `dev` 已具备，
   不应重复移植。
7. `main` 的 busy follow-up buffer 依赖旧 Gateway StreamBridge 且是进程内队列。`dev`
   若要支持快速跟帖，必须做 project-scoped durable admission/queue，不能复制 watcher。

基线：

- 共同祖先：`3be3969f8fc3f2d2b6d36ef5c26fa5593d916f2a`
- `main`：`e317f7b8d9b2afb4c3925812d4774da602c9f8f3`
- `dev`：`8a91e95799c9b345d9540c7e201b33c603e7870c`

## 2. 源码地图

### 2.1 `main` 最终实现

| 层 | 文件 | 职责 |
| --- | --- | --- |
| 消息协议 | `main:backend/app/channels/message_bus.py` | `InboundMessage`、`OutboundMessage`、async queue |
| 核心调度 | `main:backend/app/channels/manager.py` | 去重、并发、Thread/Run、stream、follow-up |
| 去重存储 | `main:backend/app/channels/dedupe_store.py` | memory/PostgreSQL 原子 dedupe |
| 生命周期 | `main:backend/app/channels/service.py` | Provider start/stop/reload |
| Connection hint | `main:backend/app/channels/connection_identity.py` | server lookup 后附连接提示 |
| Conversation store | `main:backend/packages/harness/deerflow/persistence/channel_connections/` | connection/credential/state/conversation |
| Dedupe model | `main:backend/packages/harness/deerflow/persistence/webhook_delivery/model.py` | `webhook_deliveries` 四元主键 |
| GitHub route | `main:backend/app/gateway/routers/github_webhooks.py` | HMAC、事件解析、fan-out、503 |
| GitHub dispatcher | `main:backend/app/gateway/github/dispatcher.py` | binding fan-out、self/review gate |
| GitHub triggers | `main:backend/app/gateway/github/triggers.py` | actions、mention、allow_authors |
| GitHub registry | `main:backend/app/gateway/github/registry.py` | repo/event binding lookup |
| Provider | `main:backend/app/channels/feishu.py` 等 | 平台收发和附件 |

### 2.2 当前 `dev` 权威实现

| 层 | 文件 | 职责 |
| --- | --- | --- |
| 消息协议 | `dev:backend/app/channels/message_bus.py` | message DTO 和 bus |
| 核心调度 | `dev:backend/app/channels/manager.py` | process-local dedupe、项目分流、outbound |
| 项目 Dispatcher/Resolver | `dev:backend/app/private_work/connection_inbound.py` | persisted connection → issued private scope → Thread/Run |
| Connection Repository | `dev:backend/packages/harness/deerflow/persistence/channel_connections/sql.py` | scoped connection/conversation 操作 |
| Connection model | `dev:backend/packages/harness/deerflow/persistence/channel_connections/model.py` | project/owner FK、conversation unique |
| Schema | `dev:backend/packages/harness/deerflow/persistence/full_schema.sql` | 唯一 schema 来源 |
| Run admission | `dev:backend/app/private_work/run_admission.py` | inbound authority 再锁定、Job 准入 |
| Project connection API | `dev:backend/app/gateway/routers/project_connections.py` | provider list/connect/disconnect |
| GitHub route/logic | `dev:backend/app/gateway/routers/github_webhooks.py`、`gateway/github/` | HMAC、fan-out、trigger |
| Provider adapters | `dev:backend/app/channels/` | `feishu.py`、`wecom.py`、`slack.py`、`telegram.py`、`discord.py`、`dingtalk.py`、`wechat.py`、`github.py` 的平台差异 |

当前 `dev` 没有 `dedupe_store.py` 或 `webhook_deliveries` 表；去重状态只在
`ChannelManager._recent_inbound_events`。

## 3. 当前 `dev` 的真实入站调用链

### 3.1 从 Provider 到 Dispatcher

```text
External provider event
  -> provider adapter 构造 InboundMessage
  -> MessageBus.publish_inbound()
  -> ChannelManager._dispatch_loop()
     -> _is_duplicate_inbound()       # 当前在 authority resolve 之前
     -> create_task(_handle_message())
  -> ChannelManager._handle_message()
     -> _apply_effective_owner()
     -> tombstoned command normalization
     -> _should_dispatch_project_inbound()
     -> semaphore
     -> _handle_project_inbound_chat()
```

`InboundMessage` 主要字段：

```text
channel_name
chat_id
user_id                       # 外部平台用户
text
thread_ts
topic_id
connection_id                 # 可选提示
owner_user_id                 # 可选提示
private_scope                 # 只可用于 scoped lookup 提示
project_id                    # 展示/路由提示
workspace_id
files
metadata
```

字段注释已经明确：可变 message 上的 `private_scope/project_id/owner_user_id` 不是 private-work
执行 authority。

### 3.2 Dispatcher 再调用 Resolver

`ChannelManager._handle_project_inbound_chat(msg)` 只取
`self._private_inbound_dispatcher`，然后：

```py
result = await dispatcher.dispatch(msg)
```

`ProjectInboundDispatcher.dispatch()` 内部才构造：

```py
ProviderIdentity(
    provider=message.channel_name,
    external_account_id=message.user_id,
    workspace_id=message.workspace_id,
    external_conversation_id=message.chat_id,
    external_topic_id=message.topic_id,
)
```

随后顺序是：

```text
ProjectInboundDispatcher.dispatch(message)
  -> ConnectionInboundResolver.resolve(provider_identity)
  -> run_launcher(resolved.context, resolved.thread_id, message, resolved.authority)
  -> ProjectInboundDispatchResult(resolved, state)
```

因此正确关系是 **Dispatcher 包含 Resolver 调用**，不是两个并列入口。

### 3.3 Resolver 的权威解析

`ConnectionInboundResolver.resolve()`：

1. 校验 `ProviderIdentity` 精确类型和非空 provider/account/conversation；
2. `find_connection_by_external_identity(provider, external_account_id, workspace_id)`；
3. `_connection_coordinates()` 要求：
   - status 是 `connected`；
   - connection ID 有效；
   - account/project/owner 都是 UUID；
   - `account_id == owner_user_id`；
4. `_resolve_context(project_id, owner_user_id)`：
   - `resolve_project_context()` 重新查 active project/membership；
   - `PrivateWorkContext.from_project()`；
5. 用 exact scope 查询 conversation mapping：
   - project；
   - owner；
   - connection；
   - external conversation；
   - normalized topic；
6. 有映射则复用 Thread；
7. 无映射则从 connection metadata 读取 exact Agent ref，创建 project-private Thread，
   再 `set_thread_id()`；
8. 返回 `ResolvedInboundPrivateWork` 和 `PrivateRunInboundAuthority`。

`ChannelConversationRow` 的唯一键是：

```text
connection_id + external_conversation_id + external_topic_id
```

同时通过复合 FK 绑定 project/owner/connection 和 project/owner/thread。topic 的 `None`
在 persistence 层规范为 `""`。

### 3.4 Run launcher 与第二次 authority 校验

`build_gateway_project_run_launcher()` 创建 in-process Gateway adapter，但不执行 graph：

```text
resolved context/thread
  -> start_private_run(
       server_context=PrivateRunAdmissionServerContext(
         inbound_authority=resolved.authority
       )
     )
  -> PrivateRunAdmissionService.admit()
  -> 返回 store-only Run
  -> launcher 通过 PrivateRunService 轮询 durable Run 终态
  -> ProjectScopedCheckpointer 读取最终 state
  -> ChannelManager 生成 OutboundMessage
```

admission 的 `_require_inbound_authority()` 在同一事务中重新锁定：

- connected、未 frozen 的 exact connection；
- exact project/owner/provider/external account/workspace；
- exact conversation/topic/thread mapping。

随后才锁 Thread、创建 Run/Job/snapshot/quota/audit。即使 message 在进入 bus 前附过连接信息，
执行边界仍以 PostgreSQL 当前状态为准。

## 4. 当前去重：发生位置和局限

### 4.1 四元 key

`ChannelManager._inbound_dedupe_key()` 从：

- `metadata.event_id/message_id/msg_id`；
- 或 `metadata.raw_message` 中同名字段

提取稳定 provider message ID。当前 key：

```text
(channel_name, workspace_id, chat_id, message_id)
```

没有 workspace 时直接跳过去重。它不包含：

- project_id；
- owner_user_id；
- connection_id；
- topic_id；
- authoritative external conversation（只是可变 `chat_id`）。

### 4.2 状态和失败语义

`_recent_inbound_events` 是进程内 `OrderedDict`：

- TTL 10 分钟；
- 上限 4096；
- 单进程内先记 key，再启动处理 task；
- 重复直接丢弃；
- transient/unexpected failure 时 `_release_inbound_dedupe_key()`，允许 provider redelivery；
- provider adapter 在 publish 前发出的 reaction/working-card 不受该层去重。

### 4.3 已确认的边界

- 多 Gateway Pod/进程各有独立 map，同一 delivery 可各启动一个 Run；
- restart 后 map 清空；
- 无 workspace 的 provider 消息不去重；
- topic 不在 key 中；
- 去重发生在 Resolver 前，不能使用 persisted project/owner/connection；
- 10 分钟后同一 delivery 会再次执行；
- adapter 的前置副作用可能重复。

这些是源码可确认的限制。它们不等于每个平台都会实际生成冲突 ID；provider ID 的稳定性仍应
用真实 payload fixture 验证。

## 5. `main` 的 cross-pod dedupe

### 5.1 Store 协议

```py
InboundDedupeKey = tuple[str, str, str, str]

class InboundDedupeStore(Protocol):
    async def try_record(key) -> bool
    async def release(key) -> None
```

`MemoryInboundDedupeStore` 保留旧行为。`PostgresInboundDedupeStore` 用单条：

```sql
INSERT ...
ON CONFLICT (...) DO UPDATE SET first_seen = now()
WHERE first_seen < now() - ttl
RETURNING channel
```

语义：

- 新 row：admit；
- live row 冲突：无 returning，判 duplicate；
- expired row：原子刷新并重新 admit；
- proceed path 懒清理旧 row；
- DB 错误 fail-open，宁可重复也不静默丢消息；
- unexpected handler failure 删除 key，允许 redelivery。

`make_inbound_dedupe_store()` 在 PostgreSQL 下默认选 shared store，多 worker 但只能使用
memory 时发 warning。

### 5.2 不能直接复制的原因

`main` 表 `webhook_deliveries` 的主键仍只是：

```text
channel + workspace_id + chat_id + message_id
```

它：

- 不理解 Project/Owner；
- 不包含 connection/topic；
- 通过旧 migration `0009_webhook_dedupe.py` 建表；
- 10 分钟后允许相同 delivery 重跑；
- acquisition 不与 project-private Run admission 同事务；
- DB outage 时 fail-open。

当前 `dev` 只允许 `full_schema.sql` 初始化空库，禁止复制 migration。对会产生外部 side effect
的 project-private Run，fail-open 还是 fail-closed 也必须作为明确产品/可靠性策略决定。

## 6. 项目私有去重的正确维度和落点

### 6.1 Key

权威 key 至少是：

```text
provider
+ project_id
+ owner_user_id
+ connection_id
+ external_conversation_id
+ normalize(external_topic_id)  # None -> ""
+ provider_delivery_id
```

说明：

- `account_id` 当前被强制等于 `owner_user_id`，通常无需再重复；若未来允许代理账号，
  schema 需显式加入；
- connection 已隐含 provider/external account/workspace，但 provider 仍建议显式存储，方便
  FK/审计和防错；
- conversation 与 topic 必须同时在 key 中；
- delivery ID 必须来自 provider 稳定字段，不能用客户端生成的临时 ID；
- 不能从 `InboundMessage.project_id/private_scope` 取项目维度。

### 6.2 调用顺序调整

因为当前去重在 `_dispatch_loop()`，它拿不到权威 project scope。移植时应拆成两层：

1. 可保留一个非权威、process-local 的早期抖动过滤，仅用于减压；
2. 真正 cross-pod project dedupe 必须在
   `ProjectInboundDispatcher.dispatch()` 调用 Resolver **之后**，或更强地放进
   `PrivateRunAdmissionService` 的 inbound authority 事务中。

推荐强语义：

```text
ProviderIdentity
  -> Resolver 得到 issued context + connection/conversation/topic/thread
  -> 构造 ProjectInboundDeliveryIdentity
  -> admission 事务重新锁 connection/conversation
  -> 原子插入 scoped delivery key，并绑定 run_id
  -> 同事务创建 Run/Job
```

这样避免“dedupe row 已写但 Run 没建”或“Run 已建但 dedupe row 没写”的 crash window。
重投相同 delivery 时可以返回已绑定 Run/no-op，而不是启动第二个 Run。

精确落点：

- 新建 `app/private_work/inbound_dedupe.py` 的 typed repository/service；
- 扩展 `PrivateRunInboundAuthority` 或 `PrivateRunAdmissionServerContext` 携带
  server-derived delivery identity；
- `full_schema.sql` 新增 project-scoped delivery 表、复合唯一键和 retention 字段；
- ORM 放在 project/channel persistence 包；
- `PrivateRunAdmissionService._require_inbound_authority()` 后、Run 创建前原子 acquire；
- `ProjectInboundDispatchResult` 明确 duplicate/admitted 状态；
- retention 时长按各 provider redelivery窗口和副作用风险决定，不盲用 10 分钟。

## 7. GitHub webhook 与 trigger 修复

### 7.1 Route

`receive_github_webhook()`：

1. route 只有配置 secret 或显式 local unverified flag 时挂载；
2. 先读原始 body 并用 HMAC-SHA256 constant-time verify；
3. 后 parse JSON；
4. unknown event 返回 200 no-op；
5. channel disabled/missing 是永久配置状态，返回 200 并带原因；
6. `fanout_event()` 的 transient failure 返回 503；
7. `fanout_event()` 只发布到 bus，不在 webhook request 内跑 Agent。

当前 `dev` 代码行为已经返回 503，但注释仍错误声称 GitHub 会自动重试 5xx。
`main@474a0fd6`、`04659cc8` 修正为：

- GitHub 不自动重试失败 webhook；
- 503 的价值是让 delivery 在 Recent Deliveries/API 中保持失败、可被人工或恢复脚本发现；
- 误回 200 的 delivery 仍可在窗口内手工/API redeliver，不是“永久不可恢复”。

这是当前 `dev` 的确认文档/运维语义错误，应移植注释和测试说明；不要虚构自动重试。

### 7.2 `allow_authors` 大小写

当前 `dev`：

```py
if author and author in trigger.allow_authors:
```

`main@259f51ca`：

```py
author.lower() in {a.lower() for a in trigger.allow_authors}
```

GitHub login 匹配应大小写不敏感。这是确认缺失。落点是
`dev:backend/app/gateway/github/triggers.py` 的 `event_should_fire()`，并补 mixed-case 测试。

### 7.3 Redundant review comment fan-out

GitHub 提交一组 PR review 时可能同时发：

- 一个 `pull_request_review`；
- 每个 inline comment 一个 `pull_request_review_comment`。

`main@51bb19fa` 的 `_is_redundant_review_comment()` 只识别：

```text
pull_request_review_id is not None
AND in_reply_to_id is None
```

最终抑制是 **per binding**：

- 该 binding 同 repo 同时订阅 `pull_request_review`；
- paired review trigger 不要求 mention；
- 当前 comment 本身不是 reply；
- 若当前 comment trigger 因其他原因已不 fire，保留更准确 skip reason。

以下情况不能抑制：

- 只订阅 review_comment 的 binding；
- reply (`in_reply_to_id` 有值)；
- paired review trigger 要求 mention，因为 review summary 不一定含 inline mention。

当前 `dev:backend/app/gateway/github/dispatcher.py` 没有该函数和 gate，是确认缺失。不能在 route 层全局丢
event，否则会误伤只订阅 comment 的 Agent。

### 7.4 空白 mention 配置

`main@74392e14` 在 Pydantic model 层把 whitespace-only：

- `GitHubTriggerConfig.mention_login`；
- `GitHubAgentConfig.bot_login`

规范为 `None`，让 precedence 正确回退：

```text
trigger.mention_login
-> github.bot_login
-> channels.github.default_mention_login
-> agent.name
```

当前 `dev` model 无 validator；`"   "` 是 truthy，会要求一个永远不可能匹配的 mention。
这是确认缺失。应在 `dev:backend/packages/harness/deerflow/config/agents_config.py` 统一 normalize，
而不是只在 dispatcher 某个读取点 trim。

### 7.5 Busy follow-up

`main@d2b5f884` 对 `fire_and_forget` GitHub Thread 的 busy conflict：

- process-local per-thread `OrderedDict` buffer；
- provider delivery ID 去重；
- 最多 20 条，按 10 条合并；
- watcher 等旧 `StreamBridge` 出现 END，再提交 `<followups-while-busy>`；
- stop 时取消 watcher。

当前 `dev` admission 固定 `multitask_strategy=reject`。项目 inbound 遇到 active Run 时，
不会自动形成 follow-up Run；这是行为差异。

不能复制 `main` watcher，因为：

- `dev` 没有 Gateway-owned StreamBridge/graph task；
- process-local buffer 在 Gateway restart/跨 Pod 时丢失；
- follow-up 也必须绑定 project/owner/connection/conversation/topic；
- Worker durable终态才是 drain 信号。

若产品需要，落点应是 PostgreSQL durable inbound/follow-up queue，或把消息作为确定性的
pending admission 保存，由 Worker/调度器在前 Run terminal 后 admit。必须有容量、FIFO、
dedupe、membership revalidation 和 cancel/retention 语义。

## 8. Provider 局部修复

### 8.1 Feishu

当前 `dev` 已检查 image/file upload 和 inbound resource download 的 `response.success()`，
但以下调用仍把“SDK 正常返回失败 response”当成功：

- `send_file()` 的 reply/create；
- `_add_reaction()`；
- `_reply_card()`；
- `_create_card()`；
- `_update_card()`。

`main@314f84bc`、`2bb22643`：

- 读取 response；
- `not response.success()` 时记录 code/msg/log_id；
- file/card 需要 retry 时抛 `RuntimeError`；
- reaction 可以 warning 后返回；
- 只有成功 card ID 才写 conversation/clarification mapping。

这是确认缺失，可按函数小块移植。不要改 connection/project authority。

### 8.2 WeCom

当前：

```py
body.get("quote", {}).get("text", {}).get(...)
```

当 payload 显式 `"quote": null` 时，第一段返回 `None`，随后 `.get` 抛 `AttributeError`。

`main@0519c8a5` 使用：

```py
(((body.get("quote") or {}).get("text") or {}).get("content") or "").strip()
```

这是确认、独立、低风险修复。

### 8.3 已具备，不重复移植

| `main` 修复 | 当前 `dev` |
| --- | --- |
| `7156e745` bare `connect` 不算绑定命令 | `extract_connect_code()` 已要求至少两个 token |
| `b650456c` delta/cumulative stream merge | `_merge_stream_text()` 已是最终 prefix/suffix/delta 算法 |
| `88b04848` provider name 先 allowlist | `project_connections._ready_provider()` 已先查 `PROJECT_CONNECTION_PROVIDER_META` |
| connection/conversation exact project owner scope | 当前 model、repository、Resolver、admission 已具备 |
| provider failure 后释放进程 dedupe | 当前 generic exception path 已具备 |

## 9. Connection/conversation 并发风险

`ConnectionInboundResolver` 的“创建 Thread”和“插入 conversation mapping”当前是两个 service
事务。`set_thread_id()` 用唯一键 first-writer-wins：

- 相同 Thread retry 幂等 true；
- 不同 Thread 输家返回 false；
- Resolver fail-closed。

数据库不会把同一 conversation 静默重映射到另一 Thread，这是已确认的安全属性。
但两个进程同时看到 mapping missing 时，可能先各自创建 Thread，再由一个 mapping insert
获胜；输家的 Thread 是否需要补偿删除，是当前没有端到端并发测试覆盖的**待复现原子性风险**。

不要把它与 delivery dedupe 混为一谈：

- conversation mapping 决定“这段外部对话映射哪个 Thread”；
- delivery dedupe 决定“这一条 provider event 是否已准入 Run”。

建议分别测试，再决定将 Thread create + mapping bind 做成一个 service transaction，还是对
loser 做安全补偿。补偿必须确认 Thread 没有 Run/文件/映射，不能直接删除任意冲突 Thread。

## 10. 关键提交演化

| 提交 | 日期 | 最终行为 |
| --- | --- | --- |
| `74392e14` | 2026-07-12 | 空白 bot/mention login 规范为 unset |
| `b650456c` | 2026-07-12 | cumulative/delta 文本不再静默丢 delta |
| `0519c8a5` | 2026-07-12 | WeCom null quote guard |
| `2a7469cd` | 2026-07-14 | GitHub redelivery manager dedupe |
| `51bb19fa` | 2026-07-14 | per-binding redundant review fan-out gate |
| `259f51ca` | 2026-07-16 | `allow_authors` 大小写不敏感 |
| `7156e745` | 2026-07-17 | bare connect 拒绝 |
| `474a0fd6` | 2026-07-19 | 修正 GitHub 不会自动 retry 的说明 |
| `5b65d543` | 2026-07-19 | chat-scoped provider 无 workspace 时使用安全 conversation scope |
| `314f84bc` | 2026-07-21 | Feishu card/reaction SDK success |
| `2bb22643` | 2026-07-21 | Feishu send_file reply/create success |
| `04659cc8` | 2026-07-22 | 200 是 discoverability 问题，不是绝对不可恢复 |
| `d2b5f884` | 2026-07-24 | 旧架构 GitHub busy follow-up buffer |
| `83803718` | 2026-07-27 | PostgreSQL cross-pod dedupe |
| `9a5d7013` | 2026-07-27 | dedupe TTL 和 manual redelivery 测试/说明 |

`main` 的最终行为由这些补丁叠加而成；早期注释和 10 分钟 memory 语义不能当最终部署保证。

## 11. 确认可移植清单

### 11.1 高优先级

1. GitHub `allow_authors` casefold；
2. GitHub redundant review per-binding gate；
3. mention/bot login blank-to-none validator；
4. Feishu send/card/reaction success checks；
5. WeCom null quote；
6. 修正 GitHub 503/200 运维说明；
7. 设计 project-scoped cross-pod delivery dedupe。

### 11.2 需要设计后实现

1. durable busy follow-up；
2. dedupe retention/TTL 与 fail-open/closed；
3. delivery dedupe 与 Run idempotency 同事务；
4. concurrent missing conversation 的 loser Thread 补偿。

### 11.3 已具备

- bare connect guard；
- stream delta merge；
- project provider allowlist；
- HMAC verify-then-parse；
- exact connection/conversation authority revalidation；
- project/owner scoped outbound routing；
- admission strategy reject；
- durable Worker Run。

## 12. 禁止直接合并

- `main:backend/app/channels/manager.py` 整文件；
- legacy LangGraph SDK `/api/threads` 调用；
- Gateway StreamBridge watcher；
- process-local GitHub follow-up buffer；
- `main` 的 `webhook_deliveries` 四元 schema；
- migration `0009_webhook_dedupe.py`；
- 在 Resolver 前信任 `InboundMessage.private_scope/project_id/owner_user_id`；
- 不带 external conversation/topic 的 dedupe key；
- 只按 `chat_id` 做 project-private conversation mapping；
- 在 webhook request 内直接执行 Agent；
- 声称 GitHub 自动 retry 5xx。

## 13. 测试与契约

### 13.1 `main` 证据

- `backend/tests/test_inbound_dedupe.py`
- `backend/tests/test_multi_pod_inbound_dedupe.py`
- `backend/tests/test_migration_0009_webhook_dedupe.py`
- `backend/tests/test_github_webhooks.py`
- `backend/tests/test_github_dispatcher.py`
- `backend/tests/test_github_triggers.py`
- `backend/tests/test_github_agents_config.py`
- `backend/tests/test_channels.py`
- `backend/tests/test_feishu_parser.py`
- `backend/tests/test_wecom_ws_text.py`

Migration 测试只提供 main 行为证据，不能进入 `dev` 的 single-full-schema gate。

### 13.2 当前 `dev` 基础

- `backend/tests/test_private_connection_inbound.py`
- `backend/tests/test_private_connection_repository.py`
- `backend/tests/test_channel_connections_repository.py`
- `backend/tests/test_channel_runtime_identity.py`
- `backend/tests/test_channel_runtime_worker_scope.py`
- `backend/tests/test_m6_private_run_admission_postgres.py`
- `backend/tests/test_m7_project_channel_authority.py`
- `backend/tests/test_channels.py`
- `backend/tests/test_github_webhooks.py`
- `backend/tests/test_github_dispatcher.py`
- `backend/tests/test_github_triggers.py`
- `backend/tests/test_feishu_parser.py`

## 14. 验证矩阵

| 场景 | 预期结果 |
| --- | --- |
| forged message 携带别的 project/owner | Resolver 忽略，按 persisted connection 重建 authority |
| connection frozen/revoked 后消息到达 | admission 二次锁校验失败，不建 Run |
| membership 在 resolve 后、admit 前撤销 | admission revalidation 失败 |
| 同 connection/conversation/topic | 始终复用一个 Thread |
| 同 conversation 不同 topic | 映射和 dedupe 都分离 |
| 同 provider message ID、不同 project/connection | 不能互相抑制 |
| 同 delivery 落到两个 Gateway Pod | 只准入一个 scoped Run |
| Gateway 在 dedupe 与 Run create 间崩溃 | retry 返回已绑定 Run或可安全重新准入，不形成黑洞 |
| dedupe DB unavailable | 按明确 policy 失败/降级，并有审计，不静默 |
| handler transient failure | redelivery 可恢复；已提交外部 side effect 不重复 |
| 过 retention 后 manual redelivery | 行为明确记录：重新执行或要求人工确认 |
| concurrent first message in new conversation | 一个 mapping；loser 不留不可达 Thread |
| GitHub mixed-case allow author | fire |
| review + 20 companion inline comments | 有无 paired binding/mention 分别得到正确 fan-out |
| review-thread reply | 不作为 redundant comment 丢弃 |
| whitespace mention_login | 回退下一优先级 |
| GitHub fan-out exception | 503 并标为 failed；文档不宣称自动重试 |
| Feishu SDK `success() == False` | 不记成功 mapping，按类型 retry/warn |
| WeCom `"quote": null` | 正常处理 text，不抛异常 |
| delta 和 cumulative 混合 stream | 文本不重复、不丢失 |
| bare `connect` | 普通文本/无绑定 code，不消耗 challenge |
| active project Run 收到 follow-up | reject 或 durable queue 行为与产品合同一致 |

完成标准是“provider delivery → authoritative project scope → durable Run → scoped outbound”
整条链可证明，而不是只看到某个平台返回了 200。
