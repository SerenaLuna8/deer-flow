# M5 Task 17 implementation report

## Scope

- Enabled the compile-time `PROJECT_AUTOMATION` candidate only after the new
  focused unit and E2E release gates were in place.
- Replaced the project-only Automation E2E fixture with a shared mock whose
  only state key is the authenticated account UUID plus the project UUID parsed
  from the request URL: `${accountId}:${projectId}`.
- Added owner lifecycle, Viewer, readiness, concurrency/error, direct-route,
  account/project transition, static-demo, and project Chat link coverage.
- Fixed a real scope-transition defect exposed by the new abort assertions:
  both normal and infinite Thread search queries now forward the TanStack
  `AbortSignal` to the project client.
- Updated `frontend/AGENTS.md` for the Automation candidate and Thread-search
  cancellation contract. Task 18 milestone documentation was not changed.

## TDD evidence

The focused unit command actually executes the complete Rstest suite. With the
new release expectations written and `PROJECT_AUTOMATION` still false, the
valid flag RED was:

```text
pnpm test -- project-automations project-automation-entry project-chat
910 passed, 2 failed, 0 skipped
```

Both failures were the intended missing release switch: the compile-time value
was false and the direct route therefore called `notFound()`.

The first corrected three-file Playwright RED (after removing two test-author
construction errors) was:

```text
pnpm test:e2e -- project-automations.spec.ts \
  project-private-work-isolation.spec.ts project-private-chat.spec.ts
10 passed, 10 failed
```

Nine failures demonstrated the closed Automation route/Chat entry. The tenth
demonstrated that a held old-project Thread search did not receive an abort on
scope transition. After opening the flag, four remaining failures were only
strict-locator mistakes; the abort failure remained and exposed product
behavior.

The abort defect received separate focused unit REDs before production code
changed:

```text
thread-search-query.test.ts: 3 passed, 1 failed
infinite.test.ts: 23 passed, 1 failed
```

The minimal fix forwards the query context signal through every paginated
search call. The combined regression GREEN was `28 passed, 0 skipped`.

## Release-gate coverage

- Create, edit, pause, resume, manual trigger, delete, and run history all use
  `/api/projects/{project_id}/automations...`; the fixture records the
  authenticated account for every request and asserts that no legacy
  `/api/scheduled-tasks` request occurs.
- Viewer can list definitions and run history but receives no mutation
  controls. Capabilities come only from the project response.
- A 503 manual retry reuses the exact same valid `Idempotency-Key`; it is not
  merely checked for UUID presence on each request.
- Scheduler-disabled readiness retains manual execution. Migration-required
  readiness renders the blocked state without sending the list request.
- A 409 refresh reloads the server version and the next PATCH carries the new
  `expected_version`. A 429 leaves an explicit safe retry, and 503 manual
  execution can be retried safely.
- A direct URL without `private_work.read_own` renders the stable
  `AUTOMATION_FORBIDDEN` state and sends no readiness/list request.
- A held account-A/project-alpha list is actually aborted during an SPA project
  transition. Releasing it cannot populate project beta. A subsequent account
  transition shows account B's same-project data and no account A cache data.
- Static-demo source and pure navigation gates retain the server `notFound`
  guard, expose no Automation entry, and contain no legacy scheduled-task
  fallback.
- Project Chat exposes the filtered project Automation URL only:
  `/projects/{slug}/automations?thread_id={thread_id}`.
- Existing private-work mutation cleanup remains covered by the registry unit
  gate: scoped abort occurs before query/mutation cache removal and late
  mutation completion cannot recreate old-scope data.

## GREEN verification

```text
# Focused unit command (Rstest executes all files)
912 passed, 0 skipped

# Abort regression unit files
28 passed, 0 skipped

# Automation E2E file
8 passed

# Exact three-file focused E2E gate
20 passed

# Frontend lint + typecheck
pnpm check: passed

# Repository-wide frontend formatting
pnpm format: passed

# Full frontend unit suite
126 files, 914 passed, 0 skipped

# Full frontend E2E suite
164 passed

# Patch structure
git diff --check: passed
```

The full format gate exposed two already-present unformatted test files
(`capability-pages.test.ts` and `uploads/api.test.ts`). They were formatted
mechanically with no semantic change so the required repository-wide gate is
clean.

## Self-review

- The Automation mock has no global task map, account fallback, or legacy API
  fallback. Definitions and runs are both derived from the strict combined
  scope key.
- All account/project transitions are exercised through real browser fetch,
  TanStack cancellation, provider cleanup, and late response release rather
  than mocked query clients.
- Manual idempotency evidence inspects the two transport headers across a
  failed request and an explicit user retry.
- The flag is a literal `true as const`; no environment bypass or second
  feature switch was introduced.
- No second AuthProvider, module QueryClient, role-derived capability, legacy
  scheduled-task API, or legacy private-work API was introduced.
- Production changes outside the flag are limited to the two missing
  Thread-search signal paths found by the transition gate.
- No Task 18 status, milestone completion claim, or release document was
  changed.

## Independent review repairs

The review found three release-gate weaknesses and this follow-up repaired
them without adding a production test hook or moving slug resolution out of
`ProjectContextProvider`.

- The account transition scenario no longer reloads the page. It first makes
  account A the real `AuthProvider` identity through the existing throttled
  visibility refresh, holds an account-A Automation list in the same project
  and SPA, changes `/api/v1/auth/me` to account B, advances the Playwright
  clock, and triggers the same refresh path again. The test observes the old
  request's abort before releasing its late response, then proves the account-B
  shell and list are visible and no account-A definition is rendered. Project
  provider teardown is an intentional redundant cancellation path alongside
  `transitionAccountQueries`; the behavioral gate requires the abort and
  isolation outcome rather than coupling itself to one React Query listener.
- Source-string static assertions were replaced with a separate production
  Next build and Chromium scenario. `playwright.static.config.ts` builds with
  `NEXT_PUBLIC_STATIC_WEBSITE_ONLY=true` into `.next-static`, verifies the
  preserved `/workspace/chats/*` landing has no project/Automation entry,
  verifies a direct project Automation request returns 404, and records zero
  Automation or legacy scheduled-task API requests. The normal focused suite
  continues to use `playwright.config.ts`; `pnpm test:e2e:all` is the combined
  repeatable full gate.
- A project lacking `private_work.read_own` now reaches the Next not-found
  boundary after the existing client-owned project context resolves, with no
  readiness, list, or history request. The E2E then bypasses the UI and probes
  the project Automation API explicitly; the capability-aware fixture returns
  403 with no definition content. The initial HTML navigation can still be
  status 200 because `ProjectContextProvider` is deliberately the sole owner
  of slug paging and enter authority. Duplicating those calls in the Server
  Component merely to manufacture an initial 404 would create a second
  authority path, so the API's 403/404 remains the resource security boundary.

Review-repair RED evidence:

```text
# Capability-direct unit: current forbidden panel did not call notFound
12 passed, 1 failed

# Capability-direct browser: default Next not-found surface was absent
1 passed, 1 failed (account path already green; direct path red)

# Independent static production build/browser
expected 404, received 307
```

Focused repair GREEN evidence:

```text
# Direct authority, static routing, and entry units
25 passed

# Direct authority + project transition + real AuthProvider account transition
3 passed

# Independent static production build/browser
1 passed

# Frontend lint + typecheck / repository formatting
pnpm check: passed
pnpm format: passed

# Full frontend unit suite
126 files, 915 passed, 0 skipped

# Combined full production-browser gate
normal E2E: 164 passed
independent static-build E2E: 1 passed

# Patch structure
git diff --check: passed
```

## Resume fresh verification

Recorded at `2026-07-16T19:07:10+08:00`. This verification was run after the
shutdown checkpoint; none of the pre-pause results were substituted for it.

The first fresh combined browser run reached `163 passed` and one deterministic
timeout in the AuthProvider account-transition scenario. Its trace showed that
the test clicked the persistent `Automations` sidebar link before the preceding
project-home navigation had committed, so the Automation query never unmounted
and `holdNextList(...).started` waited for a request that could not begin. The
test now waits for the real `project-home` surface before returning to
Automations. No production code changed for this repair.

Final fresh evidence after that synchronization repair:

```text
pnpm check: passed
pnpm format: passed
pnpm test: 126 files, 915 passed, 0 failed, 0 skipped
pnpm exec playwright test tests/e2e/project-automations.spec.ts: 8 passed
pnpm test:e2e:all:
  normal Chromium: 164 passed
  independent static production build + Chromium: 1 passed
  combined command: exit 0
```

Task 17 remains pending an independent base-to-head review. This section does
not mark the task or the M5 milestone complete.
