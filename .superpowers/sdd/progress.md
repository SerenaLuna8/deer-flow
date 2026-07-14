# M4 Subagent-Driven Development Progress

- Plan: `docs/superpowers/plans/2026-07-14-project-private-work-m4.md`
- Branch: `codex/m4-private-work`
- Baseline: `d4b3168a0f67b1185393f210c220fe2aebd619d1`

| Task | Title | Commit | Review | Notes |
| --- | --- | --- | --- | --- |
| 1 | final-state ORM 与 expand/finalize schema | `1d42f49c` | APPROVED | 3 commits; live PostgreSQL 17-table catalog equality passed; Task 1 + bootstrap 86 passed |
| 2 | 可信 PrivateWorkContext、稳定错误与 harness scope | `2d007125` | APPROVED | 14 commits from `1d42f49c`; original 3 lifecycle findings plus channel runtime/repository identity and stateless authorization ordering closed; disposable PostgreSQL 159 passed + lock gate 1 passed with 0 skipped; final approval 0 Critical/Important/Minor; latest expanded non-DB regression 547 passed |
| 3 | Thread authority 与 checkpoint 双重作用域 | `f316e480` | APPROVED | 2 commits from `2d007125`; review fix wave closed 2 Critical/5 Important/1 Minor; PostgreSQL gates 246 + 171 + 8 + 2 passed with 0 skips; final review 0 Critical/Important, 1 capacity Minor tracked for Task 14 |
| 4 | Run/Event/Feedback scope 与 exact asset snapshot | `b2c919cf` | APPROVED | 2 commits from `f316e480`; review fix wave closed 3 Important; fresh PostgreSQL gates 16 + 154 + 64 + 11 + 260 passed with 0 skips; final review 0 Critical/Important/Minor |
| 5 | 项目 run admission 与 M3 exact runtime materialization | `6d0e42a` | APPROVED | implementation `90c3c58a` + repair `6d0e42a`; closed 1 Critical/3 Important; fresh gates: Task 5 98, Local risk 243, affected runtime 277 passed; mandatory 102 passed plus 6 declared Task 11 staged 409 failures, 0 skipped; final review 0 Critical/Important and the sole documentation Minor was closed during task handoff |
| 6 | 授权撤销与副作用边界 fail-close | `51a0a8e7` | APPROVED | implementation `068dbe15` + repairs `9f1cafc2`/`51a0a8e7`; two independent repair waves closed the original 3 Important plus 2 follow-up Important; final fresh gates: exact 2, cumulative races 7, Task 6 focused 40, fresh schema/governance 127, Task 4+M3 171, Task 1-3 260, affected runtime 281; final approval 0 Critical/Important/Minor |
| 7 | PostgreSQL chunked file/artifact authority | `1002e431` | APPROVED | schema/repository/service/streaming/shared upload contracts delivered; review waves closed cancellation commit windows, conversion fd/path hardening, inactive Thread reads, Viewer/delete and 100/50 MiB contract drift, MIME/header and body-error boundaries; fresh gates: focused 94, Task 7 + legacy 146, schema 78, Task 2-6 185, Task 4+M3 172; mandatory 123 passed plus 6 declared Task 11 staged 409 failures, 0 skipped; final review 0 Critical/Important/Minor |

## Current checkpoint

- Task 2 re-approved after independent from-scratch review and repair loops at `2d0071253343633b93f8caf47023b6bd7cda1b9b`.
- Task 3 approved at `f316e4804660094f25005cc47b5daebb852ea75d`.
- Task 4 approved at `b2c919cfe98b1d5937fcfcdc8840a7e6a6baf173`.
- Task 5 approved at `6d0e42a74af95ffe2f1271e6ab4e0538df1a1a1e`; final review reported 0 Critical/Important, and its one documentation Minor is closed in the handoff commit.
- Task 6 approved at `51a0a8e760cfb4dfc4ac9a96a220f6946121de8f`; third independent review reported 0 Critical/Important/Minor.
- Task 7 approved at `1002e43144e37a837be378e32707b53793b99d0d`; fixed-commit independent review reported 0 Critical/Important/Minor.
- Task 8 pending — sandbox restore、workspace finalization、branch authority copy，以及 Thread delete/freeze 的 file/artifact 跨资源隐藏集成。
- Known staged gap: 18 legacy channel repository tests await Task 10 project-scoped connection/OAuth cutover; they fail final-schema `project_id NOT NULL` and are not counted green.
- Known staged gap: six legacy runtime lifecycle E2Es await Task 11 project route replacement; they still stop at the intentional legacy Thread `409 PRIVATE_WORK_CUTOVER` and are not counted green.
- Resume branch/worktree: `codex/m4-private-work` at `/Users/jiangfeng/deer-flow/.worktrees/m4-private-work`.
