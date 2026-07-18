# M7 Task 5 Report: explicit project authority for channels and input polish

## Scope

Task 5 removes the global channel, channel-connection, console, and input-polish
HTTP surfaces. It leaves project connections at
`/api/projects/{project_id}/connections*` and moves input polish to
`/api/projects/{project_id}/private-work/input-polish`.

No Task 6 work was started and `.superpowers/sdd/progress.md` was not changed.

## 2026-07-19 final command-contract and channel-test repair

The frozen follow-up review reported 0 Critical, 2 Important, and 0 Minor
findings. This bounded repair closes exactly those two Important findings.

- Final IM command ownership is now only `/help`, the safe read-only `/models`,
  and enabled slash-skill activation after PostgreSQL binding. `/bootstrap`,
  `/goal`, `/new`, `/status`, and `/memory` were removed from the active command
  set, provider parsing, Telegram registration, help, configuration guidance,
  and user documentation. They remain only as explicit tombstones so delivery
  as either `CHAT` or `COMMAND` returns the stable unknown-command response and
  can never become an ordinary project prompt. The direct `_goal_request()` and
  `_handle_goal_command()` Gateway path plus `parse_goal_command` import were
  deleted; no project command compatibility layer was rebuilt.
- `pytest --last-failed tests/test_channels.py -q` reproduced the frozen 53-node
  failure set. Tests whose subject was the deleted legacy SDK Thread/reuse,
  wait/streaming, artifact/attachment, fire-and-forget, run-parameter, or legacy
  control-command path were removed. Durable inbound dedupe, project dispatcher,
  PostgreSQL binding fail-closed behavior, outbound metadata/clarification, and
  provider adapter/menu contracts were migrated and retained. The complete
  channel file is now green at 167 tests; its last-failed rerun also executes
  167/167 green. None of these nodes were deferred to Task 7.

### Final follow-up verification

```text
Prior 87-node Task 5 PostgreSQL file set, expanded by 11 command regressions:
98 passed in 14.93s, 0 skipped (dedicated temporary cluster on port 55437)
Task 5 authority plus blocking-I/O: 22 passed
Complete backend test_channels.py: 167 passed
Frontend focused unit gate: 31 files, 151 passed, 0 skipped
Frontend pnpm check: eslint pass; tsc --noEmit pass
Changed Chromium cases: 4 passed
Backend Ruff/format and git diff check: pass
Complete backend collection: 8113 tests, 0 collection errors
Current last-failed collection: 119/446 selected; Task 5: 0
```

The remaining last-failed classification is unchanged from the frozen later
milestone inventory: Task 2: 60, Task 7: 3, Task 8: 56, Task 5: 0, other: 0.
Task 7 still owns only true config/extensions/fallback-store removal; it does
not own deleted `ChannelManager` SDK execution behavior.

## 2026-07-19 independent-review repair

The frozen Task 5 review reported three Important findings. This repair closes
exactly those findings without starting Task 6 or changing the milestone
progress ledger.

- Removed both inbound-authority opt-outs:
  `channel_connections.require_bound_identity` and
  `ChannelRunPolicy.requires_bound_identity`. Executable chat plus `/new`,
  `/goal`, `/bootstrap`, and slash-skill input always enters
  `ProjectInboundDispatcher`; missing repository/dispatcher dependencies fail
  closed. The directly callable legacy LangGraph `_handle_legacy_chat` method
  was deleted. GitHub now supplies the repository workspace coordinate and has
  no agent-config owner bypass; without a persisted project connection it is
  rejected.
- Added typed `PrivateRunInboundAuthority` to the server-only admission
  context. After project and membership locks, the same admission transaction
  locks the exact `ChannelConnectionRow FOR UPDATE`, verifies connected/non-
  frozen project/owner/provider/external/workspace coordinates, then locks the
  exact conversation-to-Thread binding before any Run, job, or snapshot is
  written. A lost new-conversation `set_thread_id()` returns
  `PrivateWorkNotFound` instead of continuing. The real PostgreSQL race test
  pauses after initial resolve, revokes in another transaction, resumes
  admission, and proves zero Run/job/snapshot rows.
- Fixed the project Connections Playwright fixture with a later-registered
  exact providers route and strict response shape. Input-polish browser tests
  now run on `/projects/research-lab/chats/{thread_id}`, mock only
  `/api/projects/{project_id}/private-work/input-polish`, and assert zero
  requests to global `/api/input-polish`.

The repair RED evidence was observed before implementation: all three bypass
tests failed, GitHub reached no project dispatcher, and a false conversation
binding result did not raise. All are GREEN in the final focused suites.

### Repair verification

```text
Task 5 PostgreSQL gate: 87 passed in 14.67s, 0 skipped
Real revoke-between-resolve-and-admission race: 1 passed
Blocking-I/O gate: 5 passed in 0.72s
Affected channel authority tests: 19 passed
Frontend focused unit gate: 151 passed, 0 skipped
Frontend pnpm check: eslint pass; tsc --noEmit pass
Requested Chromium pair: 2 passed
All changed Chromium cases: 4 passed
Backend Ruff/format and git diff check: pass
Complete backend collection: 8183 tests, 0 collection errors
Current last-failed collection at that checkpoint: 172/517 selected
```

At that intermediate checkpoint the classification was Task 2: 60, Task 7: 56,
Task 8: 56, Task 5: 0, other: 0. The 53 newly visible `test_channels.py` nodes
were subsequently resolved by the bounded final follow-up above; they were not
deferred to Task 7 and no executable legacy bypass was restored.
Final production/config/document residue searches found zero
`require_bound_identity`, `requires_bound_identity`, or
`_handle_legacy_chat` symbols. The implicit-project/global-route scan has only
the intentional account-bootstrap `default-project` implementation and no
Task 5 global route.

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
