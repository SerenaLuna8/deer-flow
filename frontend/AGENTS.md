# AGENTS.md

This is the source of truth for DeerFlow frontend work. The repository-level
[AGENTS.md](../AGENTS.md) owns monorepo orientation; this guide owns the final M7
project-first routes, authorization, cache isolation, and frontend gates.

## Stack and commands

The frontend uses Next.js 16, React 19, TypeScript 5.8, Tailwind CSS 4, TanStack Query
5, strict Zod contracts, Rstest, Playwright, Node.js 22+, and pnpm 10.26.2+.

Run from `frontend/`:

```bash
pnpm dev
pnpm test
pnpm check
pnpm test:e2e:m7
pnpm test:e2e:m8:deterministic
pnpm test:e2e:m8
pnpm test:e2e:static
pnpm build:production
pnpm build:static
```

`pnpm check` runs lint and type checking. The M7 production Playwright gate writes to
`test-results/m7-production`; the static gate builds into `.next-static` and writes to
`test-results/m7-static`, so normal and static artifacts cannot be reused accidentally.
`test:e2e:m8:deterministic` 是不调用 live model 的完整 CI Chromium 测试清单，包含隔离矩阵
drift contract 和所有现有 Playwright 回归；`test:e2e:m8` 只由完整宿主机验收在
invocation-owned production stack 上运行，不能单独生成 M8 candidate/final。

## Final route model

- `/workspace` is the authenticated account-wide multi-project landing page. It shows
  project cards, invitations, and recoverable projects without a project sidebar.
- `/projects/[project_slug]` is the only live project shell. Nested pages include project
  overview, members/settings, Chats, Memory, Connections, Automation, shared assets,
  Usage, and Audit according to server-issued capabilities.
- `/admin/assets/*` and `/admin/operations/*` are the platform administration shells.
  Their server layouts return not-found for an authenticated non-system-admin and retain
  the requested destination only for an unauthenticated login redirect.
- `BUILD_MODE=static` renders a local no-network demo at `/workspace` and returns
  not-found for all project and admin routes. Static code must not import authenticated
  API clients or send any `/api/` request.

Project slugs are resolved only by paging the member-scoped project list and exact-matching
the returned slug. UUID-only detail, enter, pin, and mutation APIs never receive a slug.
`ProjectContextProvider` is the sole slug-resolution and enter owner; nested pages consume
`useCurrentProject()` and do not repeat those requests.

## Source layout

```text
frontend/src/
├── app/
│   ├── workspace/                    # live account landing or static local demo
│   ├── projects/[project_slug]/      # only live project shell
│   └── admin/                        # platform administration
├── components/
│   ├── projects/                     # project shell and project-private pages
│   ├── workspace/                    # reusable chat/message/artifact presentation
│   └── ui/                           # generated UI primitives
├── core/
│   ├── auth/                         # authenticated account identity
│   ├── projects/                     # strict project contracts and provider
│   ├── private-work/                 # account+project clients and keys
│   ├── project-automations/          # Automation API and pure schedules
│   ├── shared-assets/                # project/system asset contracts
│   ├── admin-operations/             # system-admin safe operations contract
│   ├── threads/                      # project-injected Thread state and streaming
│   └── messages/                     # pure message/human-input rendering model
└── env.js                            # environment validation
```

Generated primitives under `components/ui/` and `components/ai-elements/` should not be
edited manually.

## Project authority and client ownership

Platform role is exactly `system_admin | user`; project membership role is a separate
`admin | editor | runner | viewer` domain. Frontend code never derives capabilities from
either role. It renders only capabilities returned by Gateway.

`ProjectPrivateWorkProvider` owns the only live project client. Its scope contains exact
authenticated account UUID plus entered project UUID. Private-work, Automation, shared
asset, Usage, Audit, and reconnect state all derive their roots from the same pair. There
is no module-level default client, optional unscoped client, or URL fallback.

On account or project transition, always:

1. abort/cancel in-flight queries and mutations;
2. invalidate the old generation so late callbacks cannot commit;
3. remove old scoped queries, mutations, reconnect metadata, and clients;
4. create the new scoped client only after both identities are known.

Every request-capable hook must accept and forward TanStack's `AbortSignal`. A late response
from an old account/project must never update the new scope.

## Project-private data flow

Project clients use `/api/projects/{project_id}/private-work` for Thread, Run, file,
artifact, input-polish, and durable SSE operations. Project Memory uses
`/api/projects/{project_id}/memory`; Connections use
`/api/projects/{project_id}/connections`; Automation uses
`/api/projects/{project_id}/automations`.

Chats, Memory, Connections, and Automation navigation require a non-static build, server
readiness `ready`, and `private_work.read_own`. Create/run/upload/connect controls additionally
require their exact server capability. Viewer can read/list/export and perform allowed
own-delete actions but never sees mutation controls that require create/manage authority.

Durable SSE cursor and deduplication state is keyed by account/project/thread. Event IDs are
thread-monotonic; duplicate IDs and duplicate terminal frames are ignored. Gateway restart
must resume from the stored `Last-Event-ID` without cross-scope replay.

Input polish is project-scoped and never runs without `private_work.create` plus
`shared_assets.execute`. The server revalidates the current Thread Agent snapshot and
Credential-grant closure; the browser never constructs authority fields.

## Shared assets and credentials

Project asset pages group visible system and project Agent, Skill, MCP, and Credential rows.
Queries are keyed by account, project, and kind. UI actions use per-item capabilities and
optimistic revisions; no role-based inference is allowed.

Credential create/replace is an imperative authenticated request, not a TanStack mutation.
Secret-bearing form values must never enter QueryCache or MutationCache, must be cleared after
submit, and must not remain in the DOM. Responses and errors may show safe status metadata but
never plaintext, ciphertext, nonce, key ID, storage locator, secret hash, or raw provider
payload. MCP versions with required Credential slots use submit/approve rather than direct
publish.

## Automation

Automation definitions and occurrences are scoped by exact account, project, and owner.
Admin, Editor, and Runner controls appear only with the matching server capability; Viewer is
read-only. Every key begins with the authenticated account and entered project roots.

Create sends a complete payload. Edit sends a sparse PATCH based on normalized semantic
changes only. Equivalent once timestamps such as `Z` and `+00:00` do not count as a schedule
change. Pure cron/once validation and recipes live under
`core/project-automations/schedule/` and contain no URL, fetch, auth, or query-key behavior.
Manual trigger uses a UUID idempotency key and the same durable admission path as Scheduler.

## Project governance and system administration

Usage and Audit pages mount hooks only after their exact readiness and capability gates pass.
Usage distinguishes configured/effective limit, used/reserved amount, and one 80% warning per
dimension. Audit accepts a closed action enum and action-specific strict metadata, and never
renders private target digests, owner/project internals, or secret content.

Admin operation pages mount no query until the authenticated identity is confirmed as
`system_admin`. Their strict Zod contracts reject unknown owner, Run, Thread, payload,
exception, locator, or secret fields. Closed/degraded readiness displays unavailable state,
not fabricated zero counts. Safe requeue is shown only when the server returns exact
eligibility for a parentless retention-purge predecessor.

Backup, restore, journal, proof, and traffic switching are operator CLI responsibilities.
The browser exposes none of them.

## Component ownership

- `ProjectContextProvider` owns project resolution and enter.
- `ProjectPrivateWorkProvider` owns the scoped client, reconnect state, and teardown.
- `ScopedChatPage` owns project composer busy state, branch actions, and navigation.
- `MessageList` owns human-input answered/latest/pending gating.
- `core/threads/hooks.ts` owns pre-submit upload state and submission.
- Project Memory and Connection pages own their scoped queries and mutations; shared
  presentation components remain pure.
- Static demo fixtures and adapters are separate from the production client registry.

Human-input replies are ordinary human messages with `hide_from_ui: true` and the structured
response in the fourth `sendMessage(..., options)` argument under
`options.additionalKwargs`. While an open request exists, the normal composer remains disabled.

## Code style and tests

- Server Components are the default; use `"use client"` only for interactive components.
- Imports are grouped and alphabetized; use inline type imports.
- Use `@/*` aliases and `cn()` for conditional Tailwind classes.
- Runtime responses use strict Zod schemas and reject unknown authority/private fields.
- Unit tests live under `tests/unit/`; browser tests live under `tests/e2e/` and
  `tests/e2e-static/`.
- Features and fixes follow TDD: add the failing test, observe the expected failure, implement
  the minimal change, and rerun focused plus full affected gates.

Backend base URLs may be set for split-origin development. Leave them unset for the normal
root `make dev` or Docker flow so all browser calls use same-origin `/api/*` through Nginx.

M1–M8 已完成，总体进度为 8/8（100%）。M8 关闭前宿主机验收包含 893 项 frontend unit 和
79 项完整 Playwright deterministic inventory，均为 0 failed、0 skipped、0 flaky；production/static
build 与真实桌面版 Chromium journey 同时通过。Firefox 和 Safari/WebKit 仍未经过 M8 生产认证。
