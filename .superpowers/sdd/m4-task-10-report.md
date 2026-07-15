# M4 Task 10 Implementation Report — Project-private IM Connections

## Status

- Date: 2026-07-15
- Baseline: `e27839ce`
- Branch: `codex/m4-private-work`
- Implementation commit: `e9cb8912`
- Review: APPROVED under the runnable-first checklist
- Task 11: project connection slice delivered early; remaining private-work and Memory routes not started

Task 10 delivers a runnable project-scoped connection, provider callback, inbound text,
and final-response path. One frozen functionality review found two runtime blockers; the
single repair wave added the project connection HTTP entry and exact-scope Slack
credential lookup. No second review loop was opened.

## Delivered boundary

- `ChannelConnectionRepository` product methods require exact
  `PrivateResourceScope`; connection, credential, OAuth state, and conversation rows
  are keyed by project and owner.
- `ProjectConnectionService` begins project connect challenges, re-resolves the stored
  membership on callback, lists the exact owner scope, and disconnects through the same
  service boundary.
- Telegram, Slack, Discord, Feishu/Lark, DingTalk, WeChat, and WeCom binding callbacks
  use the project service in production; provider tests exercise that service-first path.
- `ConnectionInboundResolver` resolves external identity from persistence, derives the
  authoritative project and owner, creates/reuses a private Thread, and never derives
  authority from inbound owner/project fields.
- Gateway lifespan builds and injects the project inbound dispatcher. Ordinary bound
  text calls `start_private_run`, waits on the existing run lifecycle, reads the scoped
  final state, and returns final text.
- Slack outbound credential lookup carries the server-resolved private scope. Missing
  scoped credentials fall back to the deployment operator bot client.
- Project connection list/connect/disconnect is mounted at
  `/api/projects/{project_id}/connections`. Connect stores the selected Agent asset
  reference in the one-time project challenge.

## Verification

The final run used an isolated PostgreSQL cluster on port `55486`; fixtures created and
dropped random `deerflow_test_*` databases. No business database was used.

| Gate | Result |
| --- | --- |
| Repository + OAuth + service + inbound + conversation PostgreSQL gate | 52 passed, 0 skipped |
| Seven provider service-first PostgreSQL bindings | 47 passed, 0 skipped |
| Project connection HTTP integration | 2 passed, 0 skipped |
| Final merged Task 10/provider/Manager/Gateway gate | 469 passed |
| Ruff check | passed |
| Ruff format check | 29 files already formatted |
| Compileall | passed |
| `git diff --check` | passed |

## Known staged compatibility gap

The legacy owner-only `/api/channels` binding tests report 25 failures against the final
project-scoped repository. They are not part of the product write path anymore and were
not repaired with an owner-to-project fallback. The project route above is runnable;
the workspace client migration and the explicit legacy cutover response remain later M4
work.

## Bounded backlog

- Validate the selected Agent asset at connect time instead of first inbound Thread creation.
- Project attachment delivery, incremental streaming, and artifact replies.
- Credential refresh/rotation and real provider-plus-LLM end-to-end tests.
- Finer provider configuration error classification.
- Explicit legacy `/api/channels` cutover guard and workspace UI route migration.
- Extreme callback/rebind races and performance tuning.

These items do not block the runnable project-bound text path and will not reopen Task 10.
