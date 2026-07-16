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
