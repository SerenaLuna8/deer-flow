# Frontend AGENTS.md

This is the source of truth for frontend changes. The repository-level
[AGENTS.md](../AGENTS.md) owns monorepo orientation; this guide keeps only route,
authorization, cache-isolation, data-contract, UI-ownership, and test rules.
Exact page copy and layout belong in components and focused tests, not here.

## Stack and commands

The frontend uses Next.js 16 App Router, React 19, TypeScript 5.8, Tailwind CSS
4, TanStack Query 5, strict Zod contracts, Rstest, and Playwright. Use Node.js
22+ and the pnpm version declared in `package.json`.

Run from `frontend/`:

| Command                             | Purpose                                  |
| ----------------------------------- | ---------------------------------------- |
| `pnpm dev`                          | Turbopack development server             |
| `pnpm check`                        | ESLint plus TypeScript                   |
| `pnpm test`                         | Rstest unit suite                        |
| `pnpm test:e2e`                     | Deterministic dynamic-mode Chromium gate |
| `pnpm test:e2e:static`              | Static-build boundary gate               |
| `pnpm build:production`             | Production build                         |
| `pnpm build:static`                 | No-network static demo build             |
| `pnpm format` / `pnpm format:write` | Check or write Prettier formatting       |

The normal full stack starts from the repository root with `make dev` and uses
same-origin `/api/*` through Nginx. Override the backend base URL only for
deliberate split-origin development.

## Route and build model

- `/workspace` is the authenticated account-wide project landing page.
- `/projects/[project_slug]/*` is the only live project shell. It contains
  project chats, assets, Memory, Connections, Automation, Usage, Audit, members,
  and settings according to server-issued capabilities.
- `/admin/*` contains platform assets, operations, projects, and settings. Its
  server layout hides pages from authenticated non-system-admin users and
  preserves safe login destinations for unauthenticated users.
- `BUILD_MODE=static` exposes a local no-network demo at `/workspace` and rejects
  authenticated project/admin routes. Static code must not import production API
  clients or send `/api/` requests.

Project slugs are display/navigation identifiers. `ProjectContextProvider`
pages the member-scoped project list, exact-matches the slug, enters by UUID, and
owns the resulting context. Nested pages consume `useCurrentProject()` and never
repeat slug resolution or send a slug to UUID-only APIs.

## Source ownership

```text
frontend/src/
├── app/                         # App Router pages and layouts
├── build/                       # static/production build boundaries
├── components/
│   ├── projects/                # project shell and project-private features
│   ├── workspace/               # chat, message, file, and artifact UI
│   ├── assets/                  # shared asset presentation
│   ├── admin/                   # platform administration
│   └── ui/                      # shared primitives
├── content/                     # product documentation content
├── core/
│   ├── auth/                    # account identity and account query client
│   ├── projects/                # project contracts and context
│   ├── private-work/            # scoped API client, keys, and teardown
│   ├── threads/                 # chat/run state and streaming integration
│   ├── shared-assets/           # Agent/Skill/MCP/Credential contracts
│   ├── project-automations/     # Automation API and schedule logic
│   ├── admin-operations/        # safe system operation contracts
│   └── admin-settings/          # system model/policy contracts
├── hooks/                       # reusable React hooks
├── lib/                         # cross-cutting helpers
└── styles/                      # Tailwind entry and theme tokens
```

Prefer updating generated/shared primitives through their owning registry or
generator. A necessary local patch needs focused coverage and an explanation.

## Non-negotiable client boundaries

### Authority and identity

- Gateway is the authorization authority. Frontend code renders only
  server-issued capabilities and never derives them from system or project roles.
- System role and project membership role are separate domains. A route/layout
  gate improves UX but does not replace scoped API authorization.
- Project navigation groups follow issued capabilities: owner-private Memory
  remains under Work; Connections is project-admin configuration guarded by
  `project.channels.manage`; the Agent catalog and an owner's existing Builder
  sessions require `shared_assets.read`, while starting or mutating a design
  requires `shared_assets.edit`. Skill/MCP authoring still requires edit or
  binding-management authority, and each Project Management destination requires
  its exact governance capability. Runner-side execution reads remain available
  to chat/Automation and are not authoring access.
- Use the canonical account UUID returned by Gateway for identity and cache keys;
  never key authority by email, route slug, or display name.
- Authentication distinguishes `authenticated`, `unauthenticated`, and
  `unavailable`. Only an authoritative `401` or explicit logout clears identity
  and account-scoped state. Network, 5xx, 403, and malformed-response failures do
  not silently log the user out.
- Browser storage may retain safe preferences, never passwords, access/CSRF
  tokens, raw session IDs, Credential values, or private runtime authority.
- Redirects preserve only validated local destinations. Never reflect an
  arbitrary external URL through login/setup flows.

### Project scope and TanStack ownership

- `ProjectPrivateWorkProvider` owns the only live project client. Its identity is
  the exact authenticated account UUID plus entered project UUID.
- Every project query key starts under that account/project root; never key by
  slug. Add a new domain root to `transitionPrivateWorkScope` so scope changes
  cancel and remove it.
- On account/project transition: abort in-flight work, invalidate the old
  generation, remove old queries/mutations/reconnect state/clients, then create
  the new client only after both UUIDs are known.
- Every request-capable query forwards TanStack's `AbortSignal`. Late callbacks
  from a disposed client or old generation may not update the new scope.
- Before a Skill Builder mutation writes its exact-session cache, cancel the
  in-flight exact-session query so an older GET cannot erase a newly admitted
  Run or newer revision and accidentally stop polling.
- Declare response schemas as strict Zod objects and reject unknown authority,
  owner, trace, secret, or storage fields before data reaches the UI.
- Secret-bearing create/replace operations are imperative authenticated calls,
  not TanStack mutations. Keep values in local form state, clear them after the
  request, and invalidate only safe metadata after success.

### Streams, history, and concurrency

- Durable SSE cursor state is keyed by account/project/thread. Event IDs and
  non-SSE message/event `seq` values remain canonical signed-BIGINT decimal
  strings. Compare by decimal length/value; never convert to JavaScript number.
- Drop duplicate or non-advancing frames. A newly mounted projection joins an
  active Run from cursor `0` because an old hidden consumer's cursor does not
  prove that the new UI rendered those frames.
- Scope disposal aborts the stream and prevents late state writes. Compare-and-
  remove reconnect metadata so an old consumer cannot erase a newer Run.
- A successful Thread DELETE immediately tears down only that exact
  account/project/thread runtime and query subtree. Late stream callbacks may
  not recreate its cursor, reconnect owner, version, or cached projection; a
  failed DELETE must not tear down local state.
- Preserve LangGraph stream-handle property descriptors; do not object-spread a
  handle with lazy getters.
- Thread/Run/file catalogs must page to completion with strict schemas, abort
  support, duplicate/progress checks, and a bounded page/offset limit. Never rely
  on SDK default page sizes or silently return a truncated catalog.
- Agent Builder resume catalogs follow the same rule over the server's opaque
  cursor. A revision `409` refreshes the authoritative session, preserves local
  blueprint/name edits, and releases the stale idempotency entry. Session cache
  merges are revision-monotonic, so late responses cannot overwrite a newer
  cancelled tombstone.
- Mutations that carry `expected_version` treat `409` as an explicit concurrent
  edit. Preserve the local draft and refresh authoritative state; never silently
  overwrite it.
- Conversation rendering is a lead-Agent projection. Subagent/middleware events
  use their dedicated task/progress surfaces and must not be attached to an
  arbitrary assistant message.

### Assets, models, files, and secrets

- Project Agent/Skill/MCP versions are immutable server objects. The UI authors
  through aggregate mutations and optimistic revisions; it never fabricates a
  published pointer, capability, binding, or dependency closure.
- Agent and Skill draft revisions do not move their live pointer; only a user
  with binding-management authority may explicitly publish or activate them. An
  Agent `409` recovery must reload both the Agent catalog and complete version
  history, preserve the local draft, and adopt the backend's live-pointer
  authoring base only after the returned CAS revision is newer.
- Agent `AGENTS.md`, `SOUL.md`, `IDENTITY.md`, and `USER.md` are four logical
  version fields, not a filesystem editor.
- Builder sessions are account/project/owner scoped. Candidate edits remain
  revision/checksum-bound; final confirmation is one server transaction and does
  not imply activation or binding unless the response says so.
- An error-severity Builder conflict blocks commit until a later AI turn
  regenerates the candidate; editing a document alone does not resolve it. Commit
  includes `slug` only when the normalized review name differs from
  `session.slug`, and its idempotency signature must match that request body.
- Project Skill creation exposes exactly two user flows: AI Builder and validated
  archive upload. Do not reintroduce a manual metadata or starter-template form.
- System asset definitions are read-only in global admin views. Project binding
  and Credential-grant operations are separate, narrow mutations.
- System Skill version history keeps revoked releases visible and labels them as
  ineligible. A project pin is never silently moved or resurrected: the binding
  UI may explicitly upgrade/rollback to an eligible release or disable the pin,
  while optimistic `409` responses refresh authority and require a fresh choice.
- Credential forms never display or cache plaintext after submission. Responses
  may expose safe status/revision metadata only.
- Project MCP authoring exposes only backend-supported remote transports and
  secret-free URLs. Credential fields are encrypted bindings; the browser never
  probes the MCP endpoint, performs discovery, or infers CIDR authorization.
- Model/thinking selections are preferences. Gateway returns the effective
  execution profile; UI history and status use that server result rather than
  claiming the local selection was accepted.
- If a Thread's project Agent becomes suspended, history remains readable but
  send, retry/regenerate, edited rerun, and human-input submission all fail
  closed until the user re-enables the Agent or starts a new Thread with another
  Agent.
- Upload messages carry opaque ready-file IDs and safe metadata, not browser-made
  image data URLs. Vision admission and file integrity remain Worker authority.
- A persisted Thread composer eagerly uploads accepted attachments on selection,
  paste, or drop. Background upload must not lock text editing; Send waits for and
  reuses the exact thread/client pending or ready upload instead of starting a
  duplicate request. Thread and private-work scope transitions detach local
  ownership and clean up late ready results in their exact original scope.
- `present_files` is the explicit published-file boundary. Workspace previews
  and live file-tool state do not by themselves prove durable delivery.

### Admin and Automation

- Admin queries mount only after authenticated `system_admin` state. Strict
  schemas exclude private message, Run, Thread, owner, error, locator, and secret
  fields; unavailable readiness is not a fabricated zero.
- Project governance queries and controls mount only after the exact capability
  gate. The UI does not infer access from role names.
- Automation keys include account, project, and owner. Schedule validation is
  pure; manual triggers use idempotency and the same durable server admission as
  Scheduler.

## Common change paths

### Add a project page

1. Add the route below `src/app/projects/[project_slug]/`.
2. Keep the route thin. Use `requireServerProjectCapability` for an SSR UX gate
   or `useCurrentProject()` in the delegated client component.
3. Put feature UI under `components/projects/` and register navigation with the
   matching capability check.
4. Do not resolve the slug or construct a project client again.

### Add an API call or hook

1. Define a strict response schema under the owning `core/<domain>/` module.
2. Use the authenticated fetcher, validate before return, and forward the abort
   signal.
3. Add an account/project UUID query-key factory.
4. Register a new scope root in `scope-registry.ts`.
5. Invalidate the smallest authoritative root after mutation success.

### Add a component

- Place it under the owning feature directory and compose shared primitives.
- Import cross-module code through `@/*`; merge conditional classes with `cn()`.
- Tailwind classes assembled dynamically need an explicit source/safelist entry.
- Keep data fetching and authorization in owning hooks/providers; presentation
  components receive already-safe data and capabilities.

### Change streaming or message projection

- Start at `core/private-work/api-client.ts`, `core/threads/hooks.ts`, and the
  scoped chat projection rather than adding a second stream client.
- Cover reconnect, duplicate/late frames, scope change, terminal dedup, bigint
  cursor handling, compaction/history merge, and cancellation behavior.

## Tests and release gates

Unit tests mirror source paths under `tests/unit/` and import through `@/*`.
Restore globals and dispose scoped clients after each test.

Playwright modes are intentionally isolated:

| Mode            | Specs                     | Config                              | Command                                                                |
| --------------- | ------------------------- | ----------------------------------- | ---------------------------------------------------------------------- |
| Dynamic mocked  | `tests/e2e/`              | `playwright.config.ts`              | `pnpm test:e2e`                                                        |
| Static boundary | `tests/e2e-static/`       | `playwright.static.config.ts`       | `pnpm test:e2e:static`                                                 |
| Real backend    | `tests/e2e-real-backend/` | `playwright.real-backend.config.ts` | `pnpm exec playwright test --config playwright.real-backend.config.ts` |

Keep mocked E2E deterministic: mock every relevant `/api/` request and use fixed
IDs/timestamps. An unmocked request should fail rather than reach a developer's
live backend. Static and production artifacts use separate output directories.

Features and fixes follow TDD. Before handoff run `pnpm check`, `pnpm test`, and
the affected build/Playwright gates for the current checkout. State explicitly
when dependencies, a live Gateway/PostgreSQL, a browser, or a target deployment
prevented a gate from running.
