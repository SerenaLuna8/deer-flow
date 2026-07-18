# M7 Task 5 Report: explicit project authority for channels and input polish

## Scope

Task 5 removes the global channel, channel-connection, console, and input-polish
HTTP surfaces. It leaves project connections at
`/api/projects/{project_id}/connections*` and moves input polish to
`/api/projects/{project_id}/private-work/input-polish`.

No Task 6 work was started and `.superpowers/sdd/progress.md` was not changed.

## TDD evidence

Initial backend RED anchors produced the three expected failures: global routes
were still mounted, inbound connection authority did not require the account
coordinate, and the manager still contained implicit identity branches.

Frontend RED evidence was also captured before implementation:

- project input-polish adapter: 2 expected failures for the global URL and
  missing scope guard;
- project provider discovery adapter: 1 expected failure because no scoped
  provider API existed.

During the final PostgreSQL gate, extending the client-authority regression with
forged `account_id` and `connection_id` produced one additional RED failure. The
private runtime sanitizer was then extended to strip both coordinates, and the
focused runtime-context regression passed 23/23.

## Implementation

- Extracted strict project connection schemas into
  `app.gateway.channel_schemas`; the project router has no dependency on a
  deleted global router.
- Added project-only provider discovery with safe availability metadata and
  removed browser credential/runtime-config mutation APIs.
- Added project-only input polish. It requires authenticated project membership,
  `private_work.create`, and `shared_assets.execute`; locks the scoped Thread;
  resolves its Agent snapshot server-side; validates the exact Skill/MCP and
  Credential-grant closure; then performs only the one-shot auxiliary model
  call.
- Rejected request-supplied project, owner, account, capability, snapshot,
  grant, and connection authority. The project connection list explicitly
  projects safe public fields rather than serializing repository authority
  coordinates.
- Removed the legacy channel repository implementation/export and the global
  channel, channel-connections, console, and input-polish routers/tests.
- IM adapters attach only provider coordinates and the server-resolved
  connection ID. Private inbound re-reads the connected PostgreSQL row and
  requires its exact `(account_id, project_id, owner_user_id, connection_id)`
  tuple, then revalidates active project membership before Thread lookup or
  creation. There is no auth-disabled, default-project, recent-project,
  unique-membership, or raw external-user authority fallback.
- System operations performs one bounded provider status read and reduces it to
  `provider`, `status`, `checked_at`, and a closed safe code. Regression coverage
  proves tokens, webhook secrets, external account identifiers, and connection
  payloads are discarded.
- Frontend input polish and Connections derive URLs only from the active project
  client. The legacy workspace channel API/hooks/settings/sidebar UI and their
  stale tests were removed.
- Updated root/backend/frontend architecture documentation and user-facing
  README channel guidance.

The Task 3 assigned residual nodes were closed without expanding into Task 2,
Task 7, or Task 8. The WeChat blocking failure was caused by lazy
`mimetypes.guess_type()` initialization on the event loop; the existing image
fallback is now selected without that blocking system-file lookup.

## Final gates

PostgreSQL Task 5 brief gate:

```text
63 passed in 9.69s
PostgreSQL skips: 0
```

Blocking-I/O gate after deletion of the two global runtime-config handlers:

```text
5 passed in 0.73s
```

Exact Task 3 residual gate:

```text
3 passed in 0.75s
```

Frontend focused gate, including project components, private-work adapters,
settings removal, and strict system-operations parsing:

```text
163 passed, 0 skipped
```

Frontend static checks:

```text
pnpm check
eslint: pass
tsc --noEmit: pass
```

Backend formatting and lint:

```text
ruff check: pass
ruff format --check: pass
```

Complete backend collection:

```text
8178 tests collected in 3.00s
collection errors: 0
```

Current last-failed collection:

```text
119/281 tests collected (162 deselected) in 1.15s
```

Classification is Task 2: 60, Task 7: 3, Task 8: 56, Task 5: 0, other: 0.
The full backend suite was not rerun because the frozen milestone workflow asks
this task to classify known later-task residuals rather than repair them.

Final scoped residue searches returned zero matches for implicit-project terms,
global channel/console/input-polish URLs, deleted router imports, legacy channel
repository symbols, and auth-disabled/external-user identity fallbacks.
