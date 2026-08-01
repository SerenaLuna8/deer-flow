# 08. Frontend 模块：聊天交互增量与项目私有边界

## 1. 分析边界与结论

本文只分析 `main@e317f7b8` 在聊天前端上的五组增量，并与
`dev@8a91e957` 的项目私有前端逐项对照：

1. 编辑最近一轮用户消息并重新运行；
2. Clarification v2 结构化表单；
3. Clarification 打开时仍允许普通聊天回复；
4. Run duration 的归属、持久化与展示；
5. 浏览器语音听写。

历史增量范围是 `3be3969f..main`。两个分支从共同祖先后分别有大量独立演进，
所以本文提到的“移植”都指在 `dev` 的项目私有接口上重写或小块摘取，不指合并
`main` 的聊天页面、Hook 或全局 API。

结论先行。为避免把历史差距和当前工作树混在一起，下表同时保留移植前
`dev@8a91e957` 基线与本轮完成状态：

| 能力 | `main` 最终状态 | 移植前 `dev@8a91e957` | 当前 `dev` 工作树 | 结论 |
| --- | --- | --- | --- | --- |
| 编辑并重跑 | 前后端完整，含 settled checkpoint、乐观替换、失败回滚 | 只有普通 regenerate | 项目 scoped prepare、父链 settled base、乐观替换和异步失败回滚均已落地 | **已移植并通过真实 E2E** |
| Clarification v2 | 七种字段、严格解析、可访问性和表单序列化完整 | 仅 v1 三种模式 | 七种字段、required/a11y、稳定 JSON 序列化、流式水合稳定身份均已落地 | **已移植并通过真实表单提交** |
| Clarification 中普通回复 | 普通 composer 保持可用，最新未答请求可由可见 HumanMessage 关闭 | composer 被明确禁用 | composer 保持可用；普通可见回复只终结最新未答请求；显式卡片回复仍隐藏 | **已移植并通过真实模型恢复** |
| Run duration | 运行级计算，只在该 Run 最后一个可见分组显示，服务端可重放 | helper 已有，但展示和项目 API 投影不完整 | 成功 Run 墙钟耗时由项目 API 权威投影，group tail 只显示一次，刷新后保持 | **已移植并通过刷新验证** |
| 语音听写 | 活跃 `InputBox` 已接入 Web Speech API | 活跃项目输入框未接入 | 独立协议和活跃 `InputBox` 生命周期已接入；真实 Chrome 麦克风启动/停止及严格自动化均已验证 | **已移植；真实硬件生命周期通过** |

最重要的移植约束是：所有项目私有请求、Query cache、流游标和乐观状态必须绑定
`accountId + projectId`。本文中的“所有缓存”只指项目私有聊天相关缓存，不指应用中
与项目无关的公共缓存。

## 2. 源码地图

### 2.1 `main` 最终实现

| 领域 | 文件 | 关键职责 |
| --- | --- | --- |
| 聊天页面 | `main:frontend/src/app/workspace/chats/[thread_id]/page.tsx` | 组装编辑、重跑、Clarification、composer 状态 |
| Agent 聊天页面 | `main:frontend/src/app/workspace/agents/[agent_name]/chats/[thread_id]/page.tsx` | 与普通聊天页保持行为一致 |
| 线程 Hook | `main:frontend/src/core/threads/hooks.ts` | prepare 请求、乐观遮罩、submit、回滚和缓存失效 |
| 消息列表 | `main:frontend/src/components/workspace/messages/message-list.tsx` | 可编辑轮次判定、Run duration 最终落点 |
| 消息项 | `main:frontend/src/components/workspace/messages/message-list-item.tsx` | 内联编辑器及提交状态 |
| Human input 协议 | `main:frontend/src/core/messages/human-input.ts` | v1/v2 解析、线程状态归约、表单序列化 |
| Human input UI | `main:frontend/src/components/workspace/messages/human-input-card.tsx` | 七种字段、required 校验和辅助技术属性 |
| Duration 计算 | `main:frontend/src/core/messages/run-duration.ts` | 按 `run_id` 聚合并放到最后分组 |
| Duration UI | `main:frontend/src/components/workspace/messages/run-duration.tsx` | 运行中活动状态与最终耗时 |
| 语音协议 | `main:frontend/src/core/voice-input/speech-recognition.ts` | 浏览器差异、文本合并、语言和错误映射 |
| 活跃输入框 | `main:frontend/src/components/workspace/input-box.tsx` | SpeechRecognition 生命周期与按钮 |
| prepare API | `main:backend/app/gateway/routers/thread_runs.py` | regenerate/edit-regenerate、duration 注入 |
| checkpoint 算法 | `main:backend/app/gateway/checkpoint_lineage.py` | 沿父链寻找 settled replay base |

### 2.2 移植前 `dev@8a91e957` 权威路径

| 领域 | 文件 | 移植前职责 |
| --- | --- | --- |
| 项目路由 | `dev:frontend/src/app/projects/[project_slug]/chats/[thread_id]/page.tsx` | 项目聊天入口 |
| 项目 chat scope | `dev:frontend/src/components/projects/private-work/project-chat-page.tsx` | 生成 capability 驱动的 UI scope |
| 活跃聊天壳 | `dev:frontend/src/components/workspace/chats/scoped-chat-page.tsx` | 项目页和受限页面共用的聊天实现 |
| 项目 Provider | `dev:frontend/src/core/private-work/provider.tsx` | `ProjectPrivateWorkProvider` 生命周期和 scope 切换 |
| 项目 API client | `dev:frontend/src/core/private-work/api-client.ts` | 项目 LangGraph 兼容 client、持久流游标、终态映射 |
| Query keys | `dev:frontend/src/core/private-work/query-keys.ts` | `account/project/private-work` 根键 |
| 线程 Hook | `dev:frontend/src/core/threads/hooks.ts` | scoped client、历史合并、发送与 regenerate |
| Human input | `dev:frontend/src/core/messages/human-input.ts` | v1 卡片协议 |
| Human input UI | `dev:frontend/src/components/workspace/messages/human-input-card.tsx` | v1 选项/文本交互 |
| 消息组件 | `dev:frontend/src/components/workspace/messages/message-list.tsx`、`message-list-item.tsx` | 当前消息和 action 渲染 |
| Duration helper | `dev:frontend/src/core/messages/run-duration.ts` | 已存在运行级归组函数，但未成为活跃展示主路径 |
| 活跃输入框 | `dev:frontend/src/components/workspace/input-box.tsx` | 项目 composer；当前无语音按钮 |
| 项目 prepare API | `dev:backend/app/gateway/routers/private_work.py` | `/projects/{project_id}/private-work/.../regenerate/prepare` |
| 项目控制服务 | `dev:backend/app/private_work/chat_controls.py` | `ProjectChatControlService.prepare_regenerate()` |

移植前的 `dev:frontend/src/components/ai-elements/prompt-input.tsx` 虽然带有一套
`SpeechRecognition` 类型和 `PromptInputSpeechButton`，但活跃项目输入框没有使用它。
该文件属于生成式 UI primitive，不应把它误判为“语音功能已经接入”，也不应把业务逻辑继续
堆进该文件。

## 3. `dev` 项目私有前端的不可破坏边界

### 3.1 Scope、缓存与请求

`ProjectPrivateWorkProvider({ accountId, projectId })` 通过
`createPrivateWorkScopeRegistry()` 取得一个 `ProjectPrivateWorkScope`。它提供：

```ts
type ProjectPrivateWorkScope = {
  scope: { accountId: string; projectId: string };
  client: LangGraphClient;
  apiBaseURL: string;
  queryKeyPrefix: readonly unknown[];
  reconnectOnMount: boolean | (() => RunMetadataStorage);
  runAbortable?<T>(operation: (signal: AbortSignal) => Promise<T>): Promise<T>;
  isActive?(): boolean;
};
```

项目私有 Query 根键固定为：

```text
["account", accountId, "project", projectId, "private-work", ...segments]
```

Provider 离开旧 scope 时调用 `transitionPrivateWorkScope()`，取消旧请求、释放 client、
清理该 scope 的项目私有缓存和流状态。React Strict Mode 的短暂卸载通过 deferred release
消抖。新增的编辑 prepare、失败回滚、历史失效都必须复用：

- `privateWork.apiBaseURL`；
- `scopedThreadQueryKey(privateWork.scope, ...)`；
- `privateWork.runAbortable` / 当前 scope 活性检查；
- 现有 durable SSE cursor。

不能把 `main` 的 `/api/threads/...`、全局 query key 或全局 client 直接带入。

### 3.2 项目流状态

`dev:frontend/src/core/private-work/api-client.ts` 只接受规范正整数 SSE ID：

```text
acceptProjectStreamFrame(state, frame, runId)
  -> 丢弃无 ID、非规范 ID、回退 ID、重复 ID
  -> 提升 lastEventId
  -> end 时记录 terminalRunId
```

失败终态从 durable `event:end + data.status` 转成前端 SDK 可识别的 `event:error`，
但保留原事件 ID。因此新 replay UI 只负责视觉遮罩和提交，不得自建另一套临时流协议。

## 4. 编辑最近用户消息并重新运行

### 4.1 `main` 的交互和调用链

`main` 的前端链是：

```text
MessageList 判定 latest editable turn
  -> MessageListItem 打开内联编辑器
  -> onEditAndRegenerate(messageId, replacementText)
  -> hooks.editAndRegenerateMessage(...)
  -> submitPreparedReplay(...)
  -> POST /api/threads/{thread}/runs/edit-regenerate/prepare
  -> 安装 pending replay mask + optimistic replacement human
  -> thread.submit(prepared.input, checkpoint, metadata)
  -> 成功：刷新 thread/history/search/token usage
  -> 失败：撤销 mask、乐观消息和运行基线
```

公共状态结构是：

```ts
export type PendingPreparedReplayMask = {
  kind: "regenerate" | "edit";
  targetRunId: string;
  supersededMessageIds: string[];
  replacementHumanMessageId?: string;
};
```

`submitPreparedReplay<TPrepared>()` 把 regenerate 和 edit 的共有竞态处理放在同一个入口：

1. `sendInFlightRef` 阻止重复发送；
2. 在 prepare 前记录 token usage 和可见消息基线；
3. prepare 成功后立即遮罩被替代 Run/消息；
4. edit 额外插入带服务端新 ID 的乐观 HumanMessage；
5. 用 prepare 返回的 checkpoint 和 metadata 提交；
6. 任一步失败都只回滚本次安装的状态；
7. 最后解除 in-flight，不让失败把 composer 永久锁死。

`MessageList` 只把“最新完整用户轮次”交给编辑器。页面还额外要求：

- 没有正在运行的请求；
- 不是新线程、mock 或静态站点；
- 没有 active Goal；
- 没有打开的 HumanInputRequest；
- 分支 mutation 和上传均未进行。

编辑草稿由 `MessageList` 持有并按 `threadId` 隔离，再下传给 `MessageListItem`。空白、
与原文相同或提交中的草稿不能提交；取消恢复原文；回调返回 `false` 或抛错时保留草稿，
成功才关闭编辑器。这样失败回滚引起消息项短暂重排时不会丢失用户输入。

### 4.2 `main` 服务端契约

请求和响应为：

```py
class EditRegeneratePrepareRequest(BaseModel):
    human_message_id: str = Field(..., min_length=1)
    replacement_text: str = Field(..., min_length=1)

class EditRegeneratePrepareResponse(RegeneratePrepareResponse):
    replacement_human_message_id: str
    source_message_ids: list[str]
```

`_prepare_edit_regenerate_payload()` 做的不是简单替换文本：

1. trim 后拒绝空文本和未改变文本；
2. 拒绝 active Goal；
3. `_latest_editable_turn()` 要求目标是最新可编辑 Human turn，且有终态可见 AI 响应；
4. 找到目标 Human 前的 settled checkpoint；
5. `_require_successful_source_run()` 要求来源 Run 成功结束；
6. `_clean_human_message_for_edit()` 只保留允许重放的附件/引用上下文，生成新消息 ID；
7. metadata 写入：
   - `replay_kind=edit`
   - `regenerate_from_message_id`
   - `regenerate_from_run_id`
   - `regenerate_checkpoint_id`
   - `edit_from_message_id`
   - `edit_message_id`
   - `edit_version_group_id`
8. base 已有标题时回放当前标题，保住人工改名；base 尚无标题时不钉死旧提示词生成的标题。

历史隐藏仍以成功 replay 的 `regenerate_from_run_id` 为主。`edit_version_group_id` 在最终实现中
只是未来连续编辑的分组信息，不能把它当成当前可见性算法的唯一依据。

### 4.3 移植前差距与本轮精确落点

移植前 `dev@8a91e957` 的 `useThread()` 只有：

```text
regenerateMessage()
  -> POST {privateWork.apiBaseURL}/threads/{thread}/runs/regenerate/prepare
  -> pendingSupersededRunIds / pendingSupersededMessageIds
  -> thread.submit()
```

当时没有 edit prepare 类型、Hook、页面回调或消息项编辑 UI。这是历史基线中可确认的缺失，
不是当前工作树状态。

移植落点：

1. 在 `dev:backend/app/gateway/routers/private_work.py` 紧邻 regenerate 增加项目 scoped
   `/threads/{thread_id}/runs/edit-regenerate/prepare`。
2. 请求/响应继续使用 strict private-work model，`extra="forbid"`，字段给出长度上限。
3. 在 `dev:backend/app/private_work/chat_controls.py` 增加
   `ProjectChatControlService.prepare_edit_regenerate()`；必须先
   `require_issued_private_work_context()`，并复用 `_lock_thread()` 的 capability、owner、
   active Run 检查。
4. 来源 Run 必须从 `RunEventStore`/`PrivateRunRepository` 按
   `project_id + owner_user_id + thread_id` 查，不能只信 message 上的 `run_id`。
5. replay metadata 可继续放已有 Run metadata JSON，不需要仅为这些字段新增 schema。
6. `dev:frontend/src/core/threads/hooks.ts` 抽出与 `main` 等价的
   `submitPreparedReplay()`，但 URL、Query key、abort 和 scope 活性必须保持 `dev` 实现。
7. `dev:frontend/src/components/workspace/chats/scoped-chat-page.tsx` 接入
   `handleEditAndRegenerate`，capability 仍由 `scope.canRun` 决定。
8. 复用现有 `getSupersededRunIds()`，只在来源 replay 成功后永久隐藏；失败 Run 不得隐藏原答案。

以上落点本轮均已实现。真实浏览器和 SDK 1.9.27 复核还发现，`thread.submit()` 的
hook-level `onError` 同时承载三种来源：Run 创建前失败、带 Run ID 的终态失败，以及成功流结束后
`/state` refetch 失败。当前 Hook 因此使用 `PreparedReplayAttempt` 状态机：

1. 在 `onCreated` 捕获本次服务端实际生成的 Run ID，不使用旧 `currentRunIdRef` 回退；
2. SDK 初始 thread state 尚在 loading 时，编辑、regenerate、branch 均不可操作，
   `submitPreparedReplay()` 也做第二层门禁，避免初始 history 错误进入 replay 归因窗口；
3. 无 metadata 且尚无 created Run ID 时按 admission failure 回滚；
4. callback Run ID 与 created Run ID 相同时按终态失败回滚；
5. 已有 created Run ID 后出现无 metadata 错误时，判定为 post-success history refetch
   failure，保留成功 replay 投影；SDK 随后的同一错误二次回调按对象身份去重；
6. stop 在发出取消前锁定 attempt，并用实际 created Run ID 回滚；
7. 失败/停止 Run 的 live AI/Tool 在绑定 Run ID 后被过滤，replacement Human 被隐藏，
   来源历史和编辑草稿恢复；普通失败 Run 不经过该 replay 专用路径。

## 5. Clarification v2 结构化表单

### 5.1 数据契约

`main` 将 `HumanInputRequest.version` 扩展为 `1 | 2`，但响应仍是 v1 文本/选项结构，
从而避免同时改变恢复执行协议。

```ts
type HumanInputMode =
  | "free_text"
  | "single_choice"
  | "choice_with_other"
  | "form";

type HumanInputFieldType =
  | "text"
  | "textarea"
  | "number"
  | "select"
  | "multi_select"
  | "checkbox"
  | "date";

type HumanInputFormValue = string | number | boolean | string[];
```

版本与模式严格绑定：

- `version: 2` 必须是 `input_mode: "form"`；
- `input_mode: "form"` 必须是 v2；
- v1 不能携带 `fields` 或其他 form 语义。

解析器还拒绝：

- 重复 option ID 或 value；
- 空 option value；这会使 Radix `SelectItem value=""` 崩溃；
- 重复 field name；
- `__proto__`、`constructor`、`toString` 等原型链保留名；
- select/multi_select 缺少非空 options；
- 非法 required、context 或字段类型。

`readHumanInputFormValue()` 使用 own-property 检查，不从 Object prototype 读取值。
`buildInitialHumanInputFormValues()` 把 checkbox 显式初始化为 `false`，保证未点击的“否”
不会在提交时消失。

### 5.2 UI、状态和序列化

`HumanInputCard` 对每个 required 字段维护 `invalidFieldNames`，并设置：

- `aria-required`；
- `aria-invalid`；
- `aria-describedby`；
- label/control 的稳定关联；
- `aria-live` 的提交和错误状态。

其中 input、select、checkbox 使用各自合法的 `aria-required`；`multi_select` 是
`role="group"`，required 通过 `aria-labelledby` 指向含屏幕阅读器专用“必填”文本的 label
表达，不向 group 写无效的 `aria-required`。checkbox 用真实 checkbox 语义，不用伪装的
pressed button。表单提交不是只发人类可读摘要：

```text
<label>: <value>; ... [values: {"stable_field_name": ...}]
```

摘要方便模型阅读，末尾 JSON 保留稳定 field name 到值的无歧义映射。字段 label 或值即使包含
分号也不会破坏机器映射。

### 5.3 移植前差距与本轮落点

移植前 `dev@8a91e957` 的 `HumanInputRequest` 固定 `version: 1`，仅支持三种模式，
`human-input-card.tsx` 也没有 form controls。这是当时确认的缺失。

移植时应成套落入：

1. `dev:frontend/src/core/messages/human-input.ts` 的类型、严格 parser、初始值和序列化 helper；
2. `dev:frontend/src/components/workspace/messages/human-input-card.tsx` 的字段组件与 a11y；
3. 对应 i18n 字段；
4. `dev:backend` Clarification middleware/tool 的 v2 request 产生逻辑；
5. 后端读取响应继续接受 v1 text response，表单值放在 `value` 中。

不能只移 UI：若后端仍只发 v1，表单永远不会出现；也不能只改后端：旧 parser 会把 v2
当普通 tool text。本轮已成套完成前后端协议、严格解析、UI、i18n 和测试。

真实浏览器测试还暴露并修复了一个仅在异步水合阶段出现的问题：澄清卡原先使用可能变化的
ToolMessage ID 作为 React key，流式 ID 被持久化 ID 替换时会重挂载并清空未提交值。当前
`assistant:clarification` group 优先使用
`clarification-request:${request_id}` 作为稳定身份，旧格式才回退消息 ID。

## 6. Clarification 打开时允许普通聊天回复

### 6.1 `main` 的状态归约

显式卡片提交仍产生隐藏 HumanMessage：

```text
additional_kwargs.hide_from_ui = true
additional_kwargs.human_input_response = { ... }
```

`main` 另加了兼容归约：当出现一个可见、没有 `human_input_response` metadata 的普通
HumanMessage 时，只关闭“最新的一个未回答请求”。它不会一次关闭所有待回答请求，避免同一文本
错误吞掉更早的决策。

这个 fallback 同时解决两件事：

1. 用户主动不用卡片、直接在 composer 回复；
2. v1-only 前端把 v2 请求显示成普通文本，用户仍能继续。

页面因此不再把 `hasOpenHumanInputCard` 纳入 `InputBox.disabled`。该状态只继续约束编辑重跑等
会重写历史的操作。

### 6.2 移植前差异与当前实现

移植前 `dev@8a91e957` 的
`frontend/src/components/workspace/chats/scoped-chat-page.tsx` 明确包含：

```text
disabled = ... || hasOpenHumanInputCard || ...
```

且当时的 `deriveHumanInputThreadState()` 只识别带显式 metadata 的
HumanInputResponse。因此“Clarification 时不能普通回复”是历史基线的真实行为，不是遗漏的
测试描述。

若产品决定采用 `main` 行为，必须同时：

1. 移除 composer 的 `hasOpenHumanInputCard` 禁用条件；
2. 增加 latest-unanswered fallback；
3. 保留卡片显式提交为隐藏消息；
4. 覆盖多个未答请求、普通回复、隐藏回复、Malformed v2 的测试；
5. 确认 Worker 侧 Clarification 恢复能从普通可见 HumanMessage 继续，而不是只读 metadata。

这是产品语义变化，不能只删一个 `disabled` 条件。本轮五项已一起落地。

水合回归测试又发现，history 与 materialized checkpoint 对同一 ToolMessage 的 `run_id`
可能不对称，且“保留最后重复项的位置”会把旧 HumanMessage 移到新 clarification 后面。当前合并
逻辑按已知 Human Run 边界为同轮 AI/Tool 补齐可信 `run_id`，并采用“最后可见 payload、首次时间
槽位”的去重规则，避免旧消息被误配成新请求的普通回复。

## 7. Run duration

### 7.1 正确的归属

`main` 最终明确：`turn_duration` 是整个 Run 的墙钟生命周期，不是单条 AIMessage 的
“思考时间”。服务端以 `updated_at - created_at` 计算，可能包含排队、工具等待和终态写入时间。

`stamp_turn_duration_on_last_ai(messages, run_durations)` 从后向前扫描：

- 每个 `run_id` 只 stamp 一次；
- 只 stamp 最后一条可见 AI；
- event-store 形态下跳过 `metadata.caller` 为 `middleware:*` 或 `subagent:*` 的内部消息；
- 不修改持久化 event row，只修改 API projection；
- checkpoint history 另有 duration-only checkpoint，保证刷新后仍可读。

前端 `getRunDurationDisplaysByGroupIndex(groups)`：

1. 从 message 顶层或 `additional_kwargs.run_id` 取 Run；
2. 收集该 Run 的权威 duration；
3. 找该 Run 最后出现的 message group；
4. 只在该 group 后显示一次。

`MessageList` 在当前页亲历运行时记录 client timer；服务端 `turn_duration` 到达后覆盖本地值。
如果页面在 Run 中途才挂载，本地计时只能从挂载时开始，因此只是一种临时反馈，不是权威历史。

### 7.2 移植前状态与当前实现

移植前 `dev` 已有 `getRunDurationDisplaysByGroupIndex()` 与对应单测，说明运行级归属已经部分吸收。
但活跃 `MessageList` 没把它作为最终展示路径；`MessageListItem` 仍按 message ID 缓存和渲染
`turn_duration`，容易重复或落在不正确消息。

同时，移植前项目私有消息 API 没有与 `main` 等价的权威 duration projection/stamp。
因此当时 helper 存在不等于端到端功能完成。

精确落点：

1. 后端优先从已有项目私有 `RunRow.created_at/updated_at` 计算终态成功 Run 的 duration；
2. 在 project-scoped history/messages projection 上只给该 Run 最后一条可见 AI 注入；
3. 若要写 checkpoint，必须通过 `ProjectScopedCheckpointer` 并持有线程写锁，不能复制
   `main` 的全局 saver；
4. 前端把 duration 渲染从 `MessageListItem` 提升到 `MessageList` group 尾；
5. Query/history merge 必须保留服务端 duration，不能被较旧流帧覆盖；
6. 明确 UI 文案为“本轮耗时”而非“模型思考”。

仅为 duration 新增数据库列并非必要；本轮按此方案复用现有 Run 时间和 projection，已经完成
成功 Run 权威投影、历史合并保留与 group-tail 唯一展示。

## 8. 语音听写

### 8.1 `main` 的协议层

`main:frontend/src/core/voice-input/speech-recognition.ts` 把浏览器差异隔离为可测试函数：

- `getSpeechRecognitionConstructor()`：标准和 `webkitSpeechRecognition`；
- `getSpeechRecognitionLanguage(locale)`：规范 BCP-47，中文固定 `zh-CN`，未知回退 `en-US`；
- `readSpeechRecognitionTranscript(results)`：区分 final/interim 并规范空白；
- `appendSpeechTranscript(base, transcript)`：不破坏已有草稿；
- `mapSpeechRecognitionError()`：权限、麦克风、网络、语言、no-speech、取消；
- `shouldRestartSpeechRecognition()`：只在无错误或 `no_speech` 时自动重启。

### 8.2 活跃输入框生命周期

`InputBox` 创建 recognition 时设置：

```text
continuous = true
interimResults = true
maxAlternatives = 1
lang = getSpeechRecognitionLanguage(locale)
```

生命周期细节：

- `onresult` 只处理当前 ref 指向的 recognition；
- `onerror` 分类用户提示，不把 permission denied 当可重试；
- 正常 `onend` 可重启，错误结束按策略停止；
- composer 锁定、发送、清空、thread 切换、组件卸载时 abort；
- 用户手工编辑和语音临时文本不会形成两个竞争的真源；
- `VoiceInputButton` 在不支持的浏览器中保持可聚焦，使用 `aria-disabled="true"` 和禁用视觉；
  点击或 Enter 只显示原因，不创建 recognition。composer 真正锁定时仍使用原生 `disabled`。

语音只把转写结果写入草稿，仍需普通提交才发往 DeerFlow。它不上传原始音频给项目 API。
但浏览器的 SpeechRecognition 实现可能调用浏览器厂商的语音服务，所以隐私文案不能承诺
“音频始终完全本地处理”。

### 8.3 当前 `dev` 实现

1. 已新增 `frontend/src/core/voice-input/speech-recognition.ts`，承载纯函数和浏览器协议类型；
2. 已在活跃 `frontend/src/components/workspace/input-box.tsx` 接入；
3. 生成的 `components/ai-elements/prompt-input.tsx` 不是业务实现真源；
4. disabled、项目切换、发送、清空、polish、unmount 均会 stop/abort；
5. locale、错误映射、interim/final、restart、append 和生命周期已有单测；
6. 根 layout 已挂载 Sonner `Toaster`，权限拒绝和不支持提示真实可见；
7. 自动化测试使用受控 constructor，严格覆盖 interim/final、不自动提交、natural end
   自动重启、权限拒绝不重启、thread SPA 切换 abort，以及旧 recognition 回调隔离；
8. 测试不依赖 CI 主机真实麦克风权限。

用户明确授权后，真实 Chrome 已完成麦克风 recognition 启动和手动停止，按钮 pressed 状态及
生命周期均已截图。用本机 TTS 播放探针时，浏览器的声学路由/回声消除没有产生 transcript，
所以本轮不宣称“真实音频转写成功”；transcript 合并与 lifecycle 由 production/system Chrome
的受控 SpeechRecognition E2E 完整验证。

## 9. 关键提交演化

| 提交 | 日期 | 最终留下的行为 |
| --- | --- | --- |
| `be637163` | 2026-07-10 | 活跃输入框语音听写和独立可测协议 |
| `13fd8e22` | 2026-07-14 | duration 写入 checkpoint，使历史重放可见 |
| `d4fdc275` | 2026-07-23 | duration 从单条消息语义修正为 Run 级展示 |
| `fcbf0609` | 2026-07-27 | 编辑最近用户轮次、prepare API、乐观 UI |
| `1baa8ad6` | 2026-07-27 | Clarification v2 表单协议和控件 |
| `9a43d827` | 2026-07-28 | replay base 必须是当前父链上的 settled checkpoint |
| `1bccc8e2` | 2026-07-28 | Clarification 打开时普通 composer 保持可用 |
| `919caf7c` | 2026-07-28 | replay 保住人工标题，但允许未命名线程按新提示词命名 |
| `e56481d9` | 2026-07-29 | 各消息 API 每个 Run 只 stamp 一次 duration |
| `e317f7b8` | 2026-07-29 | 主线程流更新批处理；不改变上述协议 |

这些提交互相修正。尤其不能只取 `fcbf0609` 而不取 `9a43d827` 的 settled-lineage
安全条件，也不能只取表单 UI 而不取 parser 的 malformed fallback。

## 10. 本轮完成、已消除风险与非问题

### 10.1 本轮已补齐

- 项目 scoped edit-and-rerun API、Hook 和 UI；
- Clarification v2 form、严格 parser、required/a11y 和稳定序列化；
- Clarification 中普通可见回复的归约和 composer 行为；
- 活跃项目输入框语音听写协议与生命周期；
- 项目私有 duration 的完整服务端投影与 group-tail 展示；
- 澄清卡流式/持久化 ID 变化时的稳定 React identity；
- history/checkpoint 混合水合时的 Run identity 和消息时间位置保持。

### 10.2 已消除风险

- regenerate/edit replay 只沿当前 checkpoint 父链寻找 settled base，不再按时间选 sibling；
- duration 已从 `MessageListItem` 提升到 Run 最后可见 group，文案明确为任务墙钟耗时；
- 所有 prepare、Query key、abort 和 cache 失效继续使用 account/project scoped 边界；
- 普通回复 fallback 只关闭最新未答请求，显式隐藏回复仍按 request ID 关联；
- prepared replay 的异步终态失败会恢复原历史，不留下 replacement 文本；
- prepared replay 的 post-success history refetch 错误不再误回滚成功 Run；
- replay stop/terminal failure 会屏蔽实际失败 Run 的 partial AI/Tool，并保留编辑草稿；
- SDK 初始 state 加载期间 replay 操作被前后两层门禁，metadata-less 错误不会串入本次 attempt；
- 去重不再把旧 HumanMessage 搬到后续 clarification 之后。

### 10.3 仍需目标环境验证

- 不同 Chrome/Edge 版本、音频设备和厂商 SpeechRecognition 转写服务；
- Docker、Helm/Kubernetes 目标环境的独立浏览器与网络验证；
- 真实账号/项目跨租户 404/403 仍以完整 M7 PostgreSQL gate 为发布证据。

### 10.4 不应重复移植

- `dev` 已有 account/project scoped query key；
- `dev` 已有旧 scope abort、client disposal 和 durable SSE cursor；
- `dev` 已有 regenerate 的项目 API 与失败时 pending mask 回滚基础；
- `dev` 已有 run-duration 的纯聚合 helper；
- `dev` 已有 HumanInput v1 的显式隐藏响应。

## 11. 禁止直接合并的内容

以下内容禁止从 `main` 整文件覆盖：

- `/workspace/chats/[thread_id]` 页面；
- `main:frontend/src/core/threads/hooks.ts`；
- `main` 的全局 `/api/threads/{thread}/...` URL；
- `main` 的全局 Query cache key；
- legacy RunManager/StreamBridge 客户端假设；
- `main` checkpoint saver 和非项目消息读取；
- 生成式 `ai-elements/prompt-input.tsx` 中的旧语音实现。

允许复用的是纯协议、纯 UI 算法、错误映射和测试用例；涉及请求、缓存、历史、执行或权限的代码
必须落在 `dev` 的 project-private 入口。

## 12. 测试与契约

### 12.1 `main` 证据

- `frontend/tests/e2e/agent-chat.spec.ts`
  - 编辑 latest turn、取消、失败回滚、可见历史；
  - Clarification 中普通回复；
- `frontend/tests/unit/core/messages/human-input.test.ts`
  - v1/v2、malformed payload、普通回复只关闭 latest request；
- `frontend/tests/unit/components/workspace/messages/human-input-card.test.ts`
  - 提交规则与 required；
- `frontend/tests/unit/components/workspace/messages/human-input-card.dom.test.tsx`
  - DOM/a11y；
- `frontend/tests/unit/core/messages/run-duration.test.ts`
  - 每 Run 一次、最后 group、格式化；
- `frontend/tests/unit/core/voice-input/speech-recognition.test.ts`
  - constructor、locale、transcript、error、restart；
- `backend/tests/test_thread_regenerate_prepare.py`
  - edit/regenerate prepare 和标题；
- `backend/tests/test_checkpoint_lineage.py`
  - parent、cycle、dangling、duration-only、pending tasks。

### 12.2 当前 `dev` 回归与本轮新增覆盖

- `frontend/tests/e2e/project-private-chat.spec.ts`
- `frontend/tests/unit/core/threads/message-merge.test.ts`
- `frontend/tests/unit/core/threads/send-message.test.ts`
- `frontend/tests/unit/core/messages/human-input.test.ts`
- `frontend/tests/unit/components/workspace/messages/human-input-card.test.ts`
- `frontend/tests/unit/core/messages/run-duration.test.ts`
- `frontend/tests/unit/components/workspace/input-box-voice-lifecycle.test.ts`
- `frontend/tests/unit/core/voice-input/interaction.test.ts`
- `frontend/tests/unit/core/voice-input/speech-recognition.test.ts`
- `backend/tests/test_private_work_chat_controls.py`
- `backend/tests/test_private_work_run_router.py`
- `backend/tests/test_private_work_context.py`

本轮新增或扩展了 edit 成功/创建前失败/终态失败/post-success refetch/stop 回滚、partial
AI/Tool 清理、SDK initial state 门禁、scope transition、stable clarification key、
history/checkpoint 水合顺序、七字段 parser/序列化、duration、voice interaction/lifecycle 等
覆盖。测试继续沿用项目 ID、owner、capability 和 scope transition，没有用 legacy
`/workspace` fixture 替代项目 fixture。

## 13. 移植验证矩阵

| 场景 | 预期结果 | 层级 | 本轮结果 |
| --- | --- | --- | --- |
| 编辑最新成功轮次 | 原 Run 被遮罩，新 Human ID 和文本出现，新 Run 成功 | E2E + API | 通过 |
| 编辑旧轮次 | 409/稳定 private-work conflict，不改变 UI | API + E2E | 通过自动化 |
| 编辑空白/相同文本 | 客户端禁用；伪造请求仍被服务端拒绝 | Unit + API | 通过 |
| 编辑时 active Goal/open clarification/active Run | 被拒绝，不回滚无关状态 | API | 通过 |
| prepare 成功、submit 失败 | 原历史恢复，乐观 Human 和 mask 撤销 | Unit + E2E | 通过确定性 E2E |
| SDK initial state 尚未结束 | history action 不可用，不打开 replay 错误归因窗口 | Unit + E2E | 通过 production Chrome |
| 成功流后的 state refetch 失败 | 保留 replacement，显示 refetch 错误，不伪装成 Run 失败 | Unit + E2E | 通过 production Chrome |
| replay 终态失败/停止且已有 partial 输出 | 原历史和草稿恢复，失败 Run 的 AI/Tool 全部隐藏 | Unit + E2E | 通过 |
| 切换账号或项目时 prepare 未完成 | 请求 abort，旧响应不能写入新 scope cache | Integration | 通过 |
| sibling checkpoint 分支 | 只沿当前 head 父链找到 settled base | Backend | 通过 |
| v2 七类字段 | 正确渲染、required、序列化和恢复 | Unit + DOM + 真实浏览器 | 通过 |
| 恶意字段名/重复 option | parser 返回 null，退化为普通文本 | Unit | 通过 |
| 普通回复打开 clarification | 只关闭 latest unanswered request | Unit + 真实浏览器 | 通过 |
| 显式卡片回复 | 隐藏 HumanMessage，metadata 完整 | Unit + 真实浏览器 | 通过 |
| 异步 history/checkpoint 水合 | 未提交表单值不清空，旧 Human 不串轮 | Unit + 真实浏览器 | 通过 |
| 一个 Run 多条 AI | duration 只显示在最后可见 group | Unit | 通过 |
| 刷新历史 | 服务端 duration 保持一致，本地 timer 被替换 | 真实浏览器 | 通过 |
| 不支持语音浏览器 | 按钮回退，文本输入不受影响 | Unit + 真实浏览器 | 通过 |
| 拒绝麦克风权限 | 不自动重启，给可理解且真实可见的错误 | Unit + Chromium | 通过 |
| natural end | 当前实例结束后自动重启，旧实例 callback 无效 | Unit + Chromium | 通过 |
| thread/project 切换时录音 | recognition abort，旧 transcript 不进入新草稿 | Component + Chromium | 通过 |
| 真实 Chrome 麦克风启动/停止 | pressed 状态切换，停止后恢复 idle | 真实浏览器 | 通过 |
| 项目 A 用户读取项目 B replay | 404，不泄露消息或 Run 是否存在 | Backend integration | 通过 |

完成标准不是“按钮出现”，而是上述项目 scope、checkpoint、失败回滚、持久历史和辅助技术契约
一起通过。

## 14. 本轮实际验收记录

### 14.1 自动化与数据库

- `pnpm check`：通过；
- `pnpm build:production`：通过；
- 全量 `pnpm test`：181 个文件、1311 项通过；
- 项目编辑/重跑 production/system Chrome E2E：5 项通过，覆盖初始 state 门禁、创建前
  失败、终态失败、post-success 503 和 partial output；
- 语音 production/system Chrome E2E：4 项通过，编辑/重放与语音组合回归 9/9；
- 后端 08 相关 Ruff：通过；
- 后端非 PostgreSQL focused：51 项通过，16 项按环境预期跳过；
- 真实 PostgreSQL focused：26 项通过；
- `make setup-db` 后 `make check-db`：`full_schema_v1`、`ready`；
- 真实验收线程在最终 replay 前完成 9 个成功 Run、9 次真实 LLM 调用、原始合计
  91,638 tokens；最终又完成 2 次真实模型 Run（正常提交 + 编辑重跑）。页面最终累计显示
  114.6K Tokens，包含被编辑后 UI 隐藏的来源 Run，无失败提示。

### 14.2 真实浏览器与模型证据

证据目录：`docs/dev-main-code-analysis/evidence/08-frontend/`。

| 文件 | 证明内容 |
| --- | --- |
| `01-real-model-two-rounds.png` | 连续两轮真实模型调用 |
| `02-real-edit-rerun.png` | 编辑最新用户轮次后真实模型重跑 |
| `03-refresh-authoritative-durations.png` | 刷新后有效 Run 各显示一次权威耗时 |
| `04-clarification-v2-open-composer-enabled.png` | v2 七字段卡打开时普通 composer 仍可用 |
| `05-clarification-required-errors.png` | required 错误和 `aria-invalid` |
| `06-clarification-normal-reply-terminal.png` | 普通可见回复只终结最新请求并恢复模型 |
| `07-clarification-seven-fields-filled.png` | Chrome 中七种字段全部填写，含原生日期 |
| `08-voice-browser-after-start.png` | 真实浏览器语音入口回退后 composer 仍可用 |
| `09-clarification-seven-fields-submitted.png` | 稳定 JSON 序列化、隐藏回复和真实模型恢复 |
| `10-clarification-refresh-persisted.png` | 刷新后序列化回答和模型结果仍存在，无旧消息串轮 |
| `11-clarification-hydration-state-retained.png` | 异步建议/历史水合后未提交字段值保持 |
| `12-real-microphone-active.png` | 用户授权后真实 Chrome recognition 已启动，按钮进入停止状态 |
| `13-replay-final-real-model.png` | 最终真实编辑重跑完成，replacement 和新模型回复可见 |
| `14-replay-final-refresh.png` | 刷新后 replacement、权威 Tokens/耗时仍存在，来源轮次保持隐藏 |

### 14.3 真实测试中发现并修复的问题

1. failed edit 的 SDK 终态错误晚于 submit Promise，replacement 文本残留；
2. SDK 初始 state、Run admission 和 post-success refetch 共用 `onError`，无 metadata
   错误会被错误归因；
3. replay stop/失败后，已流出的 replacement AI/Tool 可能继续留在 live projection；
4. post-success 503 E2E 只观察路由命中会提前假绿，必须等待 UI toast；
5. clarification ToolMessage 流式/持久化 ID 变化导致卡片重挂载、表单清空；
6. history/checkpoint Tool `run_id` 不对称与 last-position dedupe 叠加，旧 HumanMessage
   被错误关联到新请求；
7. v1 请求携带 `fields` 未被严格拒绝，multi-select group 使用了无效的
   `aria-required`；
8. 语音不支持按钮不可聚焦，且根 layout 未挂载 Toaster，权限错误对用户不可见；
9. duration projection 只排除了 middleware，可能把 subagent 内部 AI 当作主线程尾消息。

以上问题均已修复并有回归覆盖。真实麦克风启动/停止已在用户授权后完成；受本机声学路由和
回声消除影响，TTS 探针没有生成 transcript，因此没有把“真实音频转写”伪装成已通过。
