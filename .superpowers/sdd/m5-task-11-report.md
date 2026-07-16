# M5 Task 11 Implementation Report — Scoped Frontend Automation Client

## Status and scope

- Date: 2026-07-16
- Baseline: `5ef7306053f057478b081de638e81de59fffd14f`
- Branch: `codex/m5-project-automation`
- Commit subject: `feat: add scoped automation client`
- Scope: Task 11 frontend Automation contracts, transport, hooks, readiness, and scoped cache lifecycle
- Explicit exclusions: Task 12 workbench UI, backend changes, milestone progress updates, and release claims

Task 11 adds the frontend-only account/project-scoped client for the M5 Automation API. It does not
expose an Automation route or navigation entry and does not claim that M5 is complete.

## Delivered contract

- Added strict Zod contracts for Automation records, occurrences/runs, list envelopes, readiness,
  create/update/version inputs, optimistic deletes, pagination, and UUID idempotency keys. Every
  response schema is strict and rejects owner, lease, resolved-membership, migration-digest, and
  other undeclared internal fields.
- Added exact authenticated REST transport for list, thread-list, detail, create, patch, delete,
  pause, resume, manual trigger, run history, and readiness. Every function accepts a validated
  `ProjectClientScope` plus an optional `AbortSignal`; every URL is derived from
  `/api/projects/{project_id}/automations` and never uses the legacy scheduled-task API.
- Added canonical public error handling. Known backend codes map to fixed public messages; malformed
  error bodies, schema drift, network failures, authentication failures, and aborts remain distinct.
  Backend-provided internal messages are never rendered through the client error.
- Added account/project-owned query and mutation keys rooted at
  `['account', accountId, 'project', projectId, 'automations']`. Inactive hooks are disabled and use
  inert non-project keys, so no request can start without the current `ProjectPrivateWorkProvider`
  scope.
- Added list/detail/thread/run/readiness hooks and scoped create/update/delete/pause/resume/trigger
  mutations. Manual trigger accepts a caller UUID at the transport boundary, while the trigger hook
  generates its UUID inside the mutation operation and sends it only as `Idempotency-Key`; it is not
  stored in query data or mutation variables.
- Added stale-response protection. Query functions forward TanStack abort signals; mutations run
  through the provider-owned abort controller and invalidate only while their originating access is
  still active and matches the same account/project scope.
- Extended private-work scope transition to own the Automation sibling root. It starts cancellation
  for both query roots, synchronously deactivates the registry to abort scoped mutations, waits for
  query cancellation, and only then removes both query and mutation caches. Account transition still
  cancels and clears the entire provider-owned QueryClient.

## Strict TDD evidence

The untouched baseline was verified first:

```text
pnpm test -- private-work
116 test files, 836 tests passed
```

The initial Task 11 RED contained only tests and no production implementation:

```text
pnpm test -- project-automations
121 test files; 6 files failed because project-automations modules did not exist;
833 existing tests passed; 0 assertion failures
```

The first implementation run reached 850/852 assertions. Systematic diagnosis showed two test
fixture/timing defects and one real async contract issue: readiness had over-mocked the config module,
the scope test switched before its mutation entered `runAbortable`, and a missing-scope mutation
threw synchronously. The tests were corrected to mock only the provider and wait on a condition; the
mutation function was made async so all mutation failures have Promise semantics. The next focused
run passed 121 files and 855 tests.

Additional review coverage proves UUID generation/validation without transport and proves account
changes clear the Automation root in addition to project-private work.

## Verification

| Gate | Result |
| --- | ---: |
| Task 11 + identity/project client/cache/private-work focused gate | 15 files, 62 tests passed |
| Fresh full frontend unit suite | 121 files, 856 tests passed, 0 skipped |
| `pnpm check` | ESLint and TypeScript passed |
| Task 11 scoped Prettier check | all matched files passed |
| `git diff --check` before report | passed |

The repository-wide `pnpm format` check also reports two baseline warnings in
`tests/unit/app/workspace/capability-pages.test.ts` and `tests/unit/core/uploads/api.test.ts`.
Neither file is part of Task 11 and neither was modified. All Task 11 files pass the scoped Prettier
gate.

## Self-review conclusion

- **Plan alignment:** all Task 11 contracts, reads, mutations, readiness, keys, provider ownership,
  and scope cleanup are present; no Task 12 UI or backend behavior was added.
- **Security and privacy:** project authority comes only from the current provider scope and server;
  account identity is cache ownership only; strict responses reject internal authority/lease fields;
  raw backend messages and idempotency keys do not enter query cache.
- **Concurrency:** account/project changes cancel requests, deactivate the old access before cache
  removal, drop scoped mutations, and prevent late mutation success from invalidating any new scope.
- **Testing:** real schema/transport/cache behavior is covered, mocks are limited to authenticated
  fetch and provider boundaries, and the relevant identity/client/cache suites plus the full unit
  suite pass.
- **Review findings:** 0 Critical, 0 Important, 0 Minor.
- `.superpowers/sdd/progress.md` is unchanged.

No blocking Task 11 concern remains. The Automation workbench and milestone-level release closure
remain deliberately deferred.
