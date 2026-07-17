# AGENTS.md

This file provides guidance to AI coding agents (Claude Code, Codex, and others) when working with the DeerFlow frontend. It is the source of truth; the sibling `CLAUDE.md` imports it via `@AGENTS.md`.

## Project Overview

DeerFlow Frontend is a Next.js 16 web interface for an AI agent system. It communicates with a LangGraph-based backend to provide thread-based AI conversations with streaming responses, artifacts, and a skills/tools system.

**Stack**: Next.js 16, React 19, TypeScript 5.8, Tailwind CSS 4, pnpm 10.26.2. Requires Node.js 22+ and pnpm 10.26.2+.

### Core dependencies

- **LangGraph SDK** (`@langchain/langgraph-sdk` ^1.5.3) — Agent orchestration and streaming
- **LangChain Core** (`@langchain/core` ^1.1.15) — Fundamental AI building blocks
- **TanStack Query** (`@tanstack/react-query` ^5.90.17) — Server state management
- **UI**: Shadcn UI, MagicUI, React Bits, and Vercel AI SDK elements (generated from registries — see Code Style)

## Commands

| Command                | Purpose                                           |
| ---------------------- | ------------------------------------------------- |
| `pnpm dev`             | Dev server with Turbopack (http://localhost:3000) |
| `pnpm build`           | Production build                                  |
| `pnpm check`           | Lint + type check (run before committing)         |
| `pnpm lint`            | ESLint only                                       |
| `pnpm lint:fix`        | ESLint with auto-fix                              |
| `pnpm format`          | Prettier check (`pnpm format:write` to apply)     |
| `pnpm test`            | Run unit tests with Rstest                        |
| `pnpm test:e2e`        | Run E2E tests with Playwright (Chromium)          |
| `pnpm test:e2e:static` | Build and test the static demo gate               |
| `pnpm test:e2e:all`    | Run normal and static-demo E2E gates              |
| `pnpm typecheck`       | TypeScript type check (`tsc --noEmit`)            |
| `pnpm start`           | Start production server                           |

Unit tests live under `tests/unit/` and mirror the `src/` layout (e.g., `tests/unit/core/api/stream-mode.test.ts` tests `src/core/api/stream-mode.ts`). Powered by Rstest; import source modules via the `@/` path alias.

E2E tests live under `tests/e2e/` and use Playwright with Chromium. They mock all backend APIs via `page.route()` network interception and test real page interactions (navigation, chat input, streaming responses). Config: `playwright.config.ts`; its production WebServer build uses Webpack explicitly so the 120-second startup gate is deterministic across local and CI environments. Static-demo release coverage lives under `tests/e2e-static/` and uses `playwright.static.config.ts`; it builds with `NEXT_PUBLIC_STATIC_WEBSITE_ONLY=true` into the independent `.next-static` dist directory, so normal and static production builds cannot reuse each other's output.

## Architecture

```
Frontend (Next.js) ──▶ LangGraph SDK ──▶ LangGraph Backend (lead_agent)
                                              ├── Sub-Agents
                                              └── Tools & Skills
```

The frontend is a stateful chat application. Users create **threads** (conversations), send messages, set thread-scoped `/goal` completion conditions, and receive streamed AI responses. The backend orchestrates agents that can produce **artifacts** (files/code), **todos**, and goal state updates.

### Source Layout (`src/`)

- **`app/`** — Next.js App Router. Routes include `/` (landing), `/workspace` (multi-project workspace; `/workspace/projects` redirects here), `/projects/[project_slug]` (independent project home), `/projects/[project_slug]/chats` (owner-scoped project conversations), `/admin/assets/{agents,skills,mcp,credentials}` (server-gated system asset administration), `/workspace/chats/[thread_id]` (legacy chat), `/workspace/agents/[agent_name]` and `/workspace/agents/new` (custom agents), `/blog/…`, the `(auth)/{login,setup,auth/callback}` flow, `/[lang]/docs/…`, and `/api/…` route handlers (e.g. `/api/memory`).
- **`components/`** — React components:
  - `ui/` — Shadcn UI primitives (auto-generated, ESLint-ignored)
  - `ai-elements/` — Vercel AI SDK elements (auto-generated, ESLint-ignored)
  - `workspace/` — Chat page components (messages, artifacts, settings)
  - `projects/` — Project workbench cards/dialogs and the independent project-home shell
  - `landing/` — Landing page sections
  - `docs/` — Docs / MDX rendering components
- **`core/`** — Business logic, the heart of the app. Domains include `threads/` (creation, streaming, state), `api/` (LangGraph client singleton), `agents/` (custom agents), `auth/` (authentication), `artifacts/`, `channels/` (IM connections), `i18n/` (en-US, zh-CN), `settings/`, `memory/`, `skills/`, `messages/`, `mcp/`, `models/`, `input-polish/` (pre-send draft rewrite API), `suggestions/`, `tasks/`, `todos/`, `tools/`, `workspace-changes/` (run-scoped changed-file summaries and diff fetching), `config/`, `notification/`, `blog/`, plus rendering helpers (`rehype/`, `streamdown/`) and `utils/`.

Platform authorization uses `system_role: "system_admin" | "user"`. The project-level
role name `admin` is a separate membership concept and must not be accepted as a
platform role in frontend schemas or admin-only UI gates.

`/admin/assets` 的 server layout 是平台管理的真实 gate：未登录保留目标跳转登录，普通用户
返回 404，静态 demo 不暴露入口。Agent、Skill、MCP 的管理动作使用 account-scoped TanStack
keys；含 Credential slot 的 MCP 不显示 direct publish，只能 submit/approve。Credential
create/replace 的 secret-bearing variables 不得进入 QueryCache 或 MutationCache，因此 UI 使用
imperative authenticated API、只保存 pending 与安全公共错误，并在提交后立即清空表单。
Credential response、版本 diff 与轮换状态都不得展示 key ID、nonce、ciphertext、storage locator
或 secret hash；轮换状态只接受 strict eligible/current/pending/status 聚合 contract。

Project server state lives under `src/core/projects/`. Its Zod contracts are strict and
capabilities always come from the Gateway response; UI code must never infer capabilities
from a project role. Every project query key starts with the authenticated account ID.
项目、membership、invitation 及公共 membership `user_id` contract 均保持 UUID。
`findSelfMembership` 只在当前身份的 ID 和 email 同时精确等于 `AUTH_DISABLED_USER` 时允许
按 email 识别本人；普通认证用户必须按 `user_id` 匹配，禁止普遍使用 email 推断成员身份。
Account changes and logout cancel in-flight TanStack queries before clearing the provider-
owned QueryClient, so data or late responses from one account cannot enter another account's
cache. `AuthProvider` therefore must always render inside `QueryClientProvider`; do not add a
module-level QueryClient singleton or mount a new AuthProvider without that outer provider.
Auth refreshes use a generation plus one AbortController per request: newer refreshes,
external identity application, logout, and unmount invalidate older generations, and both
query-cache transition and identity commit must recheck the same generation.
M4 project private work uses a separate LangGraph registry keyed by both authenticated account
UUID and entered project UUID. `ProjectContextProvider` remains the only slug-resolution/enter
owner and mounts `ProjectPrivateWorkProvider` only after both identities are known. Project
clients use the strict `/api/projects/{project_id}/private-work` base, every project private-work
query/mutation key starts with `['account', accountId, 'project', projectId, 'private-work']`, and
SDK reconnect metadata is stored under the same account/project scope. Scope cleanup must call
TanStack cancellation before removing queries, mutations, reconnect metadata, or the client.
Thread and upload hooks consume `usePrivateWorkAccess()` or an explicitly supplied access value;
do not reintroduce direct module-global `getAPIClient()` calls inside those hooks. The default
workspace client and its legacy keys remain compatible only before the server cutover marker;
after cutover those legacy APIs return `PRIVATE_WORK_CUTOVER`. `PROJECT_PRIVATE_WORKSPACE` is a
compile-time constant set to true for the M4 candidate. It must remain combined with server
readiness and `private_work.read_own`; static demo builds must expose no project-private
navigation or data requests.
Project-first mode renders the normal `/workspace` landing directly without the legacy
`WorkspaceContent` sidebar; `/workspace/projects` is a compatibility redirect, while legacy
`/workspace/chats`, agents, memory, skills, tools, and scheduled-task routes keep the existing
shell. Static demo builds preserve their existing chat landing and must not expose a project
entry that can call the project API. Project slug URLs are resolved only by
paging the member-scoped list endpoint and exact-matching the returned slug; UUID-only detail,
enter, pin, and update endpoints must never receive a slug. `/projects/[project_slug]` has its
own server-side auth, QueryClient, and AuthProvider layout and must not be nested in
`WorkspaceContent`. Its nested route layout is the sole owner of slug resolution and the enter
mutation through `ProjectContextProvider`; project pages consume `useCurrentProject()` and must
not repeat either request. The project shell exposes only implemented destinations, exposes
settings from server-returned `project.lifecycle.manage` or `project.update` capabilities, and
never derives visibility from role. Chats, Memory, Connections, and recent private work render
only when the build flag, readiness=`ready`, and `private_work.read_own` agree; a disabled
readiness state never starts a Thread query or navigation.
项目资产页 `/projects/[project_slug]/{agents,skills,mcp,credentials}` 同样只消费
`useCurrentProject()`，不得重复解析 slug、拉取项目列表或调用 enter。资产列表使用严格 read
model，按系统级与项目级分组，并包含逐项 capability、当前已发布版本以及持久化的固定绑定、
启用状态和并发控制修订版本；Query key 必须同时按 account、project 和 kind 隔离。界面只按逐项
capability 展示操作，禁止从角色推断权限。系统资产的项目内版本历史只返回已发布版本，系统
Credential 仅展示安全元数据；暂停或归档资产必须按服务端状态限制创建、发布、审批和绑定移动。
M3 不提供运行或开始对话入口。旧 `/workspace/{agents,skills,tools}` 只读展示 PostgreSQL 系统
资产，并使用 account-scoped `/api/assets/catalog/*`，不得复用受旧 feature gate 限制的配置接口；
普通用户不能写入，`system_admin` 仅可跳转 `/admin/assets` 管理。MCP 审批的 Credential 选择器
只列出同一作用域内已启用 Credential 的 current version：项目审批只能使用项目 Credential，
系统审批只能使用系统 Credential；只强制 required slot，optional slot 允许留空。Credential catalog
的 loading/error 必须与空列表区分，并提供安全错误文案和重试。secret-bearing 字段不得进入 TanStack
cache、响应或错误；用户在 password control 中输入的值提交后必须立即清空，不得继续在 DOM 中
残留或被 UI 回显。
M3 的前端资产交付到此为止：`/admin/assets`、四类项目资产页和旧入口只读 catalog 已接入。
M4 提供 account/project-scoped LangGraph client、cache ownership、Thread/upload 注入，
并让项目 chats 列表和详情复用 `ScopedChatPage`。项目 route 继续关闭 goal、compact、branch、
regenerate、follow-up suggestions 和 scheduled-task；sidecar 与 artifact 只在 project-scoped
Thread/file/artifact loaders 注入后启用，禁止回退到 legacy `/api/threads/*`、legacy artifact URL 或宿主
文件路径。`private_work.read_own` 允许读取、下载并删除自己的 Thread/file，但不隐含
create/run/upload/branch 权限。Thread metadata 只有规范化 404/403 且 history/messages 为空时才显示
公共 not-found，5xx 必须保留可用历史或显示可重试错误。项目 Memory 页面复用可注入的 workspace
Memory view；query key 包含 account/project，Viewer 仅 list/export，不渲染 reload/import/update/delete。
项目 Connections 复用 canonical channel provider metadata 和项目 Agent catalog；connect/disconnect 使用
imperative API，连接临时状态在 `finally` 清除，不进入 TanStack mutation cache。当前 backend 未提供
项目级 provider discovery、secret replace 或 rebind，前端不得借用 global channel endpoints 模拟这些能力。
Chats、Memory 与 Connections 导航入口必须同时满足编译期 feature flag、服务端 readiness=ready 和
`private_work.read_own`；新建或运行仍额外要求 `private_work.create` 和 `shared_assets.execute`。
Viewer 仅 list/export/read/own-delete，不渲染 create/run/upload/connect 或 Memory mutation。项目或账号
切换必须先 cancel，再删除 scoped queries/mutations、reconnect metadata 与 client；隔离 E2E 必须覆盖
迟到响应不能污染新 scope；Thread search 的普通与 infinite TanStack query 都必须把 query signal
传给 project client，保证切换时真实中止旧 scope 请求。已完成的 M5 Automation 前端把
`PROJECT_AUTOMATION` 编译期开启，入口、query/mutation、manual idempotency 与 E2E mock 均按认证
account + project UUID 双重隔离；入口仍同时依赖非 static、服务端 readiness 和
`private_work.read_own`。静态 build 的 `/projects/*` server layout 必须直接 `notFound()`，不得回跳
workspace；能力不足的 Automation 直达页在 `ProjectContextProvider` 完成客户端权限解析后进入 Next
not-found boundary，且不得启动 readiness/list/history。由于 slug/enter 权限只有该 client provider
拥有，初始 HTML status 可能仍为 200，真正的资源权限必须由项目 API 返回 403/404，页面不得复制
server-side slug paging 或 enter。静态独立 build/browser 门禁必须同时证明 workspace 没有项目入口、
项目 Automation 直达为 404、且没有 Automation 或 legacy scheduled-task API 请求。Task 18 的里程碑
总门禁与独立关闭审查已于 2026-07-16 完成；M6–M8 尚未交付，因此仍不能把界面描述为完整多用户 SaaS。
Automation 的所有 query/mutation key 必须同时以认证 account UUID 和 entered project UUID 开头；
无论 account 还是 project 变化，都必须先 cancel in-flight query，再清理 scoped query、mutation、
client 与 reconnect 状态。Viewer 仅能读取自己的 definition/history，不能显示 mutation 或 manual
trigger 控件。Automation edit 必须以当前 `Automation` 为基准构建 sparse PATCH：title、prompt、
schedule_spec 与 timezone 只有发生规范化后的语义变化才发送，等价的 once 时间表示（如 `Z` 与
`+00:00`）不得触发 schedule update；create payload 仍保持完整。M5 cutover 后 legacy
`/workspace/scheduled-tasks` 只根据结构化
`409 AUTOMATION_CUTOVER` 显示本地化迁移完成说明，不显示服务端文本或任何 legacy mutation 控件。
当前前端已完成 M5，并已接入 M6 独立 Worker 对应的 PostgreSQL 持久化 SSE：标准重连游标按
account/project/thread 隔离保存，重复 event ID 与 terminal 会被丢弃。Task 14 还提供项目 Admin
Usage/Audit 页面；两类 query key 都以认证 account UUID 和 entered project UUID 开头，入口必须同时满足
非 static、精确 server capability 和对应 M6 API readiness，直达页仍由 capability 与 API 的 public-404
边界关闭。Usage 只显示 configured/effective limit、used/reserved 和 80% 状态，并以 optimistic version
收紧项目 quota；四个 usage dimension 必须各出现一次，lifetime 与 UTC daily bucket contract 不得混用。
Audit 使用 closed action enum 与逐 action strict metadata schema，不显示 target HMAC、owner/project 私有标识
或 secret。能力/static 父组件必须在挂载 query/mutation hook 前拒绝直达页；导航使用 fixed hook variants，
没有对应 capability 时不得调用该 governance hook。所有 active governance key 都从 exact account+project
`governanceRoot()` 派生，project/account transition 必须与 private-work/Automation 一起先 cancel，再移除旧 scope
的 governance queries 与 mutations。页面、状态、表单和分页文案统一使用 typed en-US/zh-CN i18n。
Task 15 已提供 outer `/admin` system-operations shell 和 Overview/Projects/Jobs/Audit 页面，同时保留
`/admin/assets/*` 的 nested asset shell。Server layout 对普通认证用户返回 404，未认证用户只跳转到
`/login?next=/admin/operations`；client page 在确认当前 `system_admin` identity 前不得挂载任何 operations
query/mutation hook。所有 query/mutation key 必须从 exact account UUID 的
`adminOperationsRoot()` 派生，响应使用 strict Zod 并拒绝 owner/run/thread/payload/exception 等未知或私有字段；
account transition 先 abort safe-requeue controller 和 cancel queries，再 clear 整个 account client。Jobs 只在
服务端明确返回 dead + safe predecessor eligibility 且类型为 parentless `retention_purge` 时显示 requeue；pending
状态按精确 `(project_id, dead_job_id)` 坐标禁用被点击行。Overview 必须渲染后端的 `ready`/`degraded`/`closed`
readiness 与全部组件状态；浏览器不得构造 ProjectContext、owner scope 或读取 raw error。四页
loading/empty/error/data 与 shell/navigation（包括 accessibility label）文案统一使用 typed en-US/zh-CN i18n。
通用备份恢复与最终 M6 关闭仍属于后续 task。
登录后的 `/workspace` 是展示多个项目卡片、待兑换邀请和可恢复项目的全局工作空间，不显示
项目级侧栏；进入 `/projects/[project_slug]` 后才显示项目概览、成员与邀请、项目设置菜单。
邀请页只从 URL fragment 接收一次性 token，立即清除 fragment，通过 HttpOnly claim cookie
跨越登录流程，不写入 storage；产品不发送邀请邮件。M2 的退出/移除和删除恢复 UI 只反映
30 天保留窗口，不代表私有数据或项目数据已被物理清除。M5 已完成，当前进度为 5/8（62.5%）；
M6 通用备份恢复及最终关闭、M7 legacy cleanup 和 M8 完整发布验收仍未交付，因此不能作为完整多用户
SaaS 发布。

- **`hooks/`** — Shared React hooks
- **`lib/`** — Utilities (`cn()` from clsx + tailwind-merge)
- **`content/`** — MDX content (blog posts, docs) rendered by the app
- **`styles/`** — Global CSS with Tailwind v4 `@import` syntax and CSS variables for theming
- **`typings/`** — Ambient TypeScript declarations
- Root files: `env.js` (env validation), `mdx-components.ts` (MDX component map)

### Data Flow

1. Optional composer helpers such as `core/input-polish` can rewrite the local draft before submission; confirmed user input then flows to thread hooks (`core/threads/hooks.ts`) → LangGraph SDK streaming
2. Stream events update thread state (messages, artifacts, todos, goal)
3. Stop actions call the LangGraph SDK stream stop path; `core/threads/hooks.ts` invalidates current-thread, token-usage, and sidebar/search caches immediately and schedules one follow-up refetch because SDK stop may finish via abort + fire-and-forget cancel before backend title finalization commits
4. TanStack Query manages server state; project private-work keys and reconnect metadata include
   both account and project identity, while localStorage stores user settings
5. Components subscribe to thread state and render updates

`/goal` and `/compact` are built-in composer commands, not skill activations. `src/components/workspace/input-box.tsx` intercepts `/goal`, `/goal clear`, and `/goal <condition>` before normal chat submission, calling Gateway `GET/PUT/DELETE /api/threads/{thread_id}/goal`. Setting `/goal <condition>` also submits the condition text as the next user task so the agent starts running immediately; status and clear do not start a run. Goal and compact requests are tied to the current `threadId` with an `AbortController`, so switching threads or unmounting the composer aborts in-flight requests and stale responses cannot update the new thread's composer state. The chat pages render `GoalStatus` above the composer from `AgentThreadState.goal`, with local optimistic state until the next stream `values` update arrives. `/compact` calls `POST /api/threads/{thread_id}/compact` to summarize older active context while leaving the full visible chat history intact; it is skipped on new/empty threads and blocked server-side while a run is in flight.

Human input requests are a structured message protocol layered on normal chat history. The backend writes request payloads to `ToolMessage.artifact.human_input`, `src/core/messages/human-input.ts` owns the runtime validators/types, and `src/components/workspace/messages/human-input-card.tsx` renders the reusable card. `MessageList` owns answered/latest/pending state for visible cards, but derives answered responses from raw `thread.messages` because replies are hidden; pending cards clear when the hidden reply appears, when dispatch is dropped, or when a new `thread.error` reports an async stream failure. Page-level submit callbacks must send a normal human message and put `hide_from_ui: true` plus the response payload in the fourth `sendMessage(..., options)` argument as `options.additionalKwargs`; the third argument remains run context such as `{ agent_name }`. Composer entry points should disable normal bottom input while `hasOpenHumanInputRequest(...)` is true so users answer through the card and preserve response metadata.

### Key Patterns

- **Server Components by default**, `"use client"` only for interactive components
- **Thread hooks** (`useThreadStream`, `useSubmitThread`, `useThreads`) are the primary API interface
- **LangGraph client** — legacy workspace uses the `getAPIClient()` default/mock registry;
  project pages use `ProjectPrivateWorkProvider` and the separate account/project registry in
  `core/private-work/`
- **Environment validation** uses `@t3-oss/env-nextjs` with Zod schemas (`src/env.js`). Skip with `SKIP_ENV_VALIDATION=1`
- **Subtask step history** (`core/tasks/`) — the subtask card shows a subagent's full step timeline (#3779): its assistant reasoning turns interleaved with the tools it ran. `Subtask.steps[]` is accumulated live from `task_running` events (appended via `mergeSteps`, not overwritten) and backfilled on expand for historical runs by `fetchSubtaskSteps`, which pages the events endpoint scoped to one task (GET `/runs/{runId}/events?event_types=subagent.step&task_id=…&after_seq=…`) until a short page, so the run-wide limit can't truncate the timeline. `core/tasks/steps.ts` is the pure model: `messageToStep` (live), `eventsToSteps` (reload), `mergeSteps` (dedup by `message_index`), and `stepsForDisplay` (what the card renders — keeps tool steps + AI steps with text, drops the trailing final-answer AI step when completed since it's shown as `result`). `core/tasks/subtask-update.ts::computeNextSubtask` is the pure per-subtask state transition (merge step deltas, keep terminal status stable); `core/tasks/context.tsx`'s `useUpdateSubtask` applies it against a `tasksRef` mirroring the latest state (not a closure snapshot), so a late-resolving `fetchSubtaskSteps` backfill merges into current state instead of clobbering SSE steps or sibling subtasks that arrived meanwhile. The owning `run_id` is carried onto history content messages in `buildVisibleHistoryMessages` so the card can resolve the events endpoint.

### Interaction Ownership

- `src/components/workspace/chats/scoped-chat-page.tsx` owns shared workspace/project composer busy-state wiring; route adapters supply capability and navigation scope.
- `src/components/workspace/chats/scoped-chat-page.tsx` owns branch-from-turn submission and navigation when the route scope enables it; sidecar `MessageList` instances do not receive the branch action.
- `src/app/workspace/chats/[thread_id]/page.tsx` and `src/app/workspace/agents/[agent_name]/chats/[thread_id]/page.tsx` own active-goal display state for their composer overlays.
- `src/components/workspace/messages/message-list.tsx` owns human-input card answered/latest/pending gating; entry pages only translate a submitted card response into `sendMessage` calls.
- `src/core/threads/hooks.ts` owns pre-submit upload state and thread submission.
- `src/app/workspace/scheduled-tasks/page.tsx` owns scheduled-task filters, selection, mutations, and the controlled create-task Sheet; the Sheet is presentation only and must reuse the page's existing payload and reset flow.
- `src/components/workspace/settings/memory-settings-page.tsx` owns memory queries, filters, file import/export, dialogs, and mutations; components under `settings/memory/` are presentation and pure view-model helpers only, and must not call memory APIs or change mutation payloads.
- `SkillSettingsList` owns skill selection, the opener reference, and the read-only preview Sheet state; `useSkillContent` owns the lazy per-skill query enabled while that Sheet is open.
- `SkillDetailSheet` strips only a parser-compatible leading frontmatter fence and renders the remainder with the existing raw-HTML-disabled Markdown renderer.

## Code Style

- **Imports**: Enforced ordering (builtin → external → internal → parent → sibling), alphabetized, newlines between groups. Use inline type imports: `import { type Foo }`.
- **Unused variables**: Prefix with `_`.
- **Class names**: Use `cn()` from `@/lib/utils` for conditional Tailwind classes.
- **Path alias**: `@/*` maps to `src/*`.
- **Components**: `ui/` and `ai-elements/` are generated from registries (Shadcn, MagicUI, React Bits, Vercel AI SDK) — don't manually edit these.

## Environment

Backend API URLs are optional; an nginx proxy is used by default:

```
NEXT_PUBLIC_BACKEND_BASE_URL=http://localhost:8001
NEXT_PUBLIC_LANGGRAPH_BASE_URL=http://localhost:8001/api
```

Leave these unset for the standard `make dev` / Docker flow, where nginx serves the public `/api/langgraph/*` prefix and rewrites it to Gateway's native `/api/*` routes.

## Resources

- [LangGraph Documentation](https://langchain-ai.github.io/langgraph/)
- [LangChain Core Concepts](https://js.langchain.com/docs/concepts)
- [TanStack Query Documentation](https://tanstack.com/query/latest)
- [Next.js App Router](https://nextjs.org/docs/app)

## Contributing

When adding features:

1. Follow the established `src/` structure
2. Add TypeScript types and proper error handling
3. Write unit tests under `tests/unit/` (`pnpm test`) and E2E tests under `tests/e2e/` (`pnpm test:e2e`)
4. Run `pnpm check` before committing
5. Update this `AGENTS.md` when architecture, commands, or conventions change
