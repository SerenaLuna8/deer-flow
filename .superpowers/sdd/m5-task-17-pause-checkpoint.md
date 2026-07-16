# M5 Task 17 pause checkpoint

## Status

- Branch: `codex/m5-project-automation`
- Worktree: `/Users/jiangfeng/deer-flow/.worktrees/m5-project-automation`
- Task 16 is complete and independently approved at ledger commit `599e4216`.
- Task 17 initial implementation is committed at `7240d8d1`.
- The first independent Task 17 review reported `0 Critical / 3 Important / 0 Minor`:
  1. account isolation used a mock variable plus reload instead of a real same-SPA AuthProvider transition;
  2. the static-demo check read source strings instead of running a static production build;
  3. the direct URL rendered a client forbidden panel without a Next not-found surface or an explicit API 403/404 proof.

## Review repairs now saved

- Reworked the account scenario to switch `/api/v1/auth/me` through the real AuthProvider in the same SPA and same project, hold the old Automation list, observe abort, release the late response, and prove account-B identity/request/UI contain no account-A task.
- Replaced the source-string static assertion with an independent production build using a separate `.next-static` directory and a dedicated Playwright config/spec.
- Static project routes now return Next not-found instead of redirecting into the normal workspace; the static browser gate proves the legacy chat landing remains, project/Automation entry is absent, direct Automation URL is 404, and no Automation or legacy scheduled-task request is sent.
- A no-capability direct Automation route now uses the Next not-found surface and sends no readiness/list/history request. The mock also rejects an explicit UI-bypass Automation API probe with HTTP 403/404 and no private content.
- Updated the Task 17 report and frontend architecture/test configuration for the new runtime gates.

## Verification completed before pause

- Focused normal account/direct/project E2E: `3 passed`.
- Direct/static routing and entry units: `25 passed`.
- Independent static production build + Chromium: `1 passed`.
- `pnpm check`: passed.
- `pnpm format`: passed.
- Full unit: `126 files / 915 passed / 0 skipped`.
- Full normal E2E: `164 passed`.
- Full static E2E: `1 passed`.
- `git diff --check`: passed.

## Interrupted work

A final fresh verification repeat was intentionally stopped for shutdown after normal E2E reached `64 / 164` with no failures. Do not claim Task 17 complete from this checkpoint alone.

## Resume sequence

1. Confirm the worktree is clean at this checkpoint commit and inspect its base-to-head diff from `599e4216`.
2. Run `pnpm check`, `pnpm format`, and full `pnpm test` from `frontend/`.
3. Run the full normal plus static Playwright command recorded in `frontend/package.json` (`pnpm test:e2e:all`).
4. Regenerate the Task 17 full review package from `599e4216` to the new HEAD.
5. Send the complete package to a fresh independent reviewer and close every Critical/Important finding.
6. Only after approval, add the Task 17 completion ledger entry and continue Task 18.
