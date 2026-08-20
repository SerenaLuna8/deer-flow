# Host execution approval card design QA

Date: 2026-08-15

Reference: user-provided `1790 × 430` approval-card screenshot. The reference and
the live ActWeave screenshot were reviewed in one vertically stacked comparison
input at the same 1790 px viewport width.

## Live states and viewports

- Desktop `1790 × 900`: pending card measured `784 × 406` inside the product's
  bounded chat column. The amber border, tinted status header, circular status
  mark, large radius, plain full-command preview, and deny/allow hierarchy match
  the reference while retaining ActWeave's required host-risk and execution-domain
  details.
- Mobile `390 × 844`: card measured `328 px` wide without horizontal overflow
  (`scrollWidth` equaled `clientWidth`). Warning text, metadata, command,
  countdown, and actions remained within the card. The card can be scrolled to
  expose its status header and both actions without clipping.
- In the current Lead interaction run, `staged` remained hidden. The first
  visible `pending` frame already contained enabled deny and allow-once buttons,
  with no simultaneous Thinking or RunActivity indicator.
- A denied card unmounted about `293 ms` after the click; an allowed card
  unmounted about `711 ms` after the click. Approved, denied, claimed, and
  terminal projections are recovery state, not read-only cards. The persisted
  approval artifact remains an internal recovery anchor after the decision.
- The `general-purpose` subagent denial path was also exercised with marker
  `approval-subagent-live-20260815-2352`. Its first observed card frame contained
  both enabled actions and no RunActivity indicator; denial unmounted the card
  about `419 ms` later. Worker logs showed no extra model request between staging
  and the decision. The database recorded
  `agent_path=[lead, subagent:general-purpose]`, `denied`, no continuation, and
  zero receipts.
- After the subtask-state projection fix, marker
  `approval-subagent-wait-live-20260816-0010` showed a “等待审批” task capsule in
  the pending frame, zero running subtasks, zero Thinking/“正在运行” indicators,
  and both actions together. Denial unmounted the card in about `440 ms`, changed
  the capsule to “已拒绝”, kept the running count at zero, and re-enabled the
  composer.

## Findings and fixes

| Severity | Finding                                                                             | Resolution                                                                                                                                                                                                                |
| -------- | ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| P1       | The approval surface could appear while the graph still reached another model step. | Local approval now uses a checkpoint-safe pause plus a Worker active-goal/hidden-continuation gate. In the exercised Lead run, Worker logs contained no additional model request between staging and the user's decision. |
| P1       | A completed decision remained on the page as a read-only approval card.             | Only a true `pending` projection renders the decision card. The exercised deny and allow cards unmounted in about `293 ms` and `711 ms`; the recovery anchor remains internal.                                            |
| P1       | A staged request could expose a card before its decision buttons were ready.        | `staged` is not publicly projected as pending. In the exercised Lead run, both enabled actions were present in the first observed card frame.                                                                             |
| P1       | Thinking or RunActivity could imply ongoing model work while approval was pending.  | Pending approval suppresses those loading indicators. Neither indicator was present while the exercised Lead cards were visible.                                                                                          |
| P1       | A paused subagent could still look like a running subtask.                          | The pending live frame now shows “等待审批” with zero running/Thinking indicators; after denial it shows “已拒绝”, keeps the running count at zero, and restores the composer.                                            |
| P2       | Mobile decision buttons rendered at 36 px high, below a reliable touch target.      | Both buttons now use a 44 px mobile minimum height and retain the compact desktop height. Live remeasurement: `286 × 44` for each button.                                                                                 |

No open P0, P1, or P2 finding remains in the exercised Lead and
`general-purpose` subagent-denial browser paths.

The active-goal path was not live-tested because the real model environment did
not expose `create_goal`; it has focused automated coverage, which is not
recorded here as live-browser evidence. The current subagent live evidence covers
denial, not subagent allow-once execution.

final result: passed for visual rendering, Lead deny/allow, and the exercised
general-purpose subagent-denial path

---

# Chat message width and alignment design QA

Date: 2026-08-16

## Visual truth and rendered evidence

- Source visual truth: `/var/folders/fd/s9_xw3qn0gdfb1ymjmg_md_c0000gn/T/codex-clipboard-2d1fea3d-dab5-4b88-90db-a24c948cfd8e.png`
- Desktop implementation: `/Users/jiangfeng/.codex/visualizations/2026/08/15/01a00628-5027-79d1-9610-9565f5b02ab1/chat-message-layout-desktop.png`
- Mobile implementation: `/Users/jiangfeng/.codex/visualizations/2026/08/15/01a00628-5027-79d1-9610-9565f5b02ab1/chat-message-layout-mobile.png`
- Source pixels: `1816 × 884`; source CSS size and density are unknown, so the
  comparison uses stable width ratios and aligned edges rather than absolute pixels.
- Desktop capture: `1280 × 720` raster at a `1280 × 720` CSS viewport; browser
  `devicePixelRatio=2`, with the browser capture normalized to CSS-pixel dimensions.
- Mobile capture: `390 × 844` raster and CSS viewport at `devicePixelRatio=1`.
- State: authenticated real chat containing a long user prompt, one user image,
  one assistant response, process disclosure, and composer.

## Full-view and focused comparison

The source and desktop implementation were opened together in one comparison
input. The source user bubble measures about `77%` of its content track and shares
the track's right edge. The implementation user container measured `541.5 / 722 =
75%`; its right edge and the assistant content right edge both measured `1249px`.
The assistant content measured the full `722px` inner timeline width and remained
left aligned. The two-percentage-point difference is an intentional reuse of the
product's existing `75%` desktop message proportion.

A separate focused crop was not needed: the requested fidelity surface is the
macro message geometry, which is fully readable in the desktop capture and was
also verified with bounding-box measurements. The mobile capture provides the
responsive focused evidence: the user container measured `288.63px` at the `88%`
limit, the assistant measured the full `328px` inner timeline width, and the
document remained `390px` wide with no horizontal overflow.

## Required fidelity surfaces

- Fonts and typography: unchanged from ActWeave; wrapping remains readable in
  both captures and the long Chinese prompt breaks without clipping.
- Spacing and layout rhythm: user content is right aligned and content-sized with
  `88%` mobile / `75%` desktop limits; assistant content stays full-width and left
  aligned. Existing timeline gaps, padding, radius, and composer alignment remain
  unchanged.
- Colors and tokens: existing `bg-secondary`, foreground, border, and dark-mode
  tokens are preserved; no new hard-coded color was introduced.
- Image quality and assets: the real uploaded image remains sharp, right aligned,
  and contained inside the user message width at desktop and mobile sizes.
- Copy and content: real user and assistant content is unchanged; this patch only
  changes layout.

## Interaction, accessibility, and console checks

- Opened and cancelled the latest-message edit state at `390 × 844`; the textarea
  stayed within the `288.63px` user container and the document did not overflow.
- Existing copy/edit controls remained reachable and aligned with the user message.
- Browser console: zero errors. Four pre-existing uncontrolled/controlled warnings
  appeared on page load; they are unrelated to this width-only change.

## Findings and comparison history

- Pass 1, post-implementation: no actionable P0, P1, or P2 mismatch. No visual
  repair loop was required after the source/implementation comparison.
- Accepted P3 difference: desktop uses the established product proportion `75%`
  rather than the source's measured `~77%`; the visual effect is equivalent and
  keeps Agent Builder and main chat responsive behavior consistent.

final result: passed

---

# Workspace project-list redesign design QA

Date: 2026-08-18

## Visual truth and intended comparison

- Source visual truth: `/Users/jiangfeng/.codex/generated_images/01a012c8-c7a6-7690-a351-f6147cd4fdea/exec-99e8ab8b-dc69-4604-9a68-0d4053ebd3f2.png`
- Source pixels: `1487 × 1058`.
- Intended implementation viewport: `1487 × 1058` CSS px in the Codex in-app browser.
- Intended state: authenticated Chinese workspace with one pinned project and no
  recoverable projects.
- Implementation screenshot: unavailable. The in-app browser currently redirects
  `/workspace` to `/login?next=%2Fworkspace` because it has no authenticated local
  ActWeave session.

## Browser and interaction evidence

- Local Gateway health returned HTTP `200`; the public workspace entry returned
  HTTP `307` to the authenticated flow.
- The in-app browser reached the local ActWeave sign-in page at the intended
  viewport. No project UI or primary workspace interaction can be inspected until
  the user completes local sign-in.
- Console errors for the implementation state were not checked because the
  implementation screen is not yet reachable.

## Required fidelity surfaces

- Fonts and typography: blocked pending the authenticated browser render.
- Spacing and layout rhythm: blocked pending the authenticated browser render.
- Colors and visual tokens: implementation uses existing ActWeave theme tokens,
  but rendered comparison is blocked.
- Image quality and asset fidelity: the target contains no raster image assets;
  implementation reuses the existing Lucide icon dependency. Rendered icon
  comparison is blocked.
- Copy and content: focused component tests cover the new English copy and pinned,
  edit, open, and recovery labels; live Chinese rendering is blocked.

## Findings and comparison history

- No full-view or focused comparison has been performed. A login-page capture is
  not valid implementation evidence and was not compared against the source.
- The source and implementation therefore have not yet been placed into the same
  comparison input.

final result: blocked

---

# Skill Publish Action Placement QA

Date: 2026-08-20

## Visual truth and rendered evidence

- Source truth: `/var/folders/fd/s9_xw3qn0gdfb1ymjmg_md_c0000gn/T/codex-clipboard-010e4a54-c051-4680-8371-04a820dcf09b.png`
  (`600 × 184` pixels).
- Full implementation screenshot:
  `/tmp/actweave-skill-publish-top-full.jpg` (`1280 × 720` pixels).
- Focused implementation screenshot:
  `/tmp/actweave-skill-publish-top-focus.jpg` (`650 × 155` pixels).
- Same-state comparison:
  `/tmp/actweave-skill-publish-top-comparison.jpg` (`650 × 374` pixels).
- State: authenticated `transport-sos-copilot` Project Skill detail for
  `trans-text-route-query`, Draft version 2 selected.

## Comparison and fidelity surfaces

- Moved the existing Publish version action out of the lower version-content
  action row and placed it immediately after Create version in the top action
  row.
- Final order is Create version, Publish version, AI revise, Delete Skill.
- Existing button components, typography, spacing tokens, colors, destructive
  treatment, icons, and surrounding version layout remain unchanged.
- No new image, font, icon, or hard-coded visual token was introduced.

## Interaction, authorization, and console checks

- Confirmed the Publish version button appears only for the selected publishable
  Draft and retains the existing dirty, selection, runtime-block, and validation
  disabled conditions.
- Clicked the relocated button and confirmed it opens the existing exact-version
  Publish Skill version preflight dialog. Closed the dialog without publishing.
- The lower version-content area no longer renders a duplicate Skill publish
  action; Agent and MCP version publish actions retain their existing placement.
- Browser console contained zero errors and zero warnings in the exercised state.
- Frontend unit suite passed `758/758`; focused publish and revision tests passed
  `23/23`; frontend check reported zero errors and five pre-existing warnings.

## Findings and comparison history

- Pass 1: the Skill publish action was visually detached from the other primary
  Skill actions and repeated a separate action row below the Version heading.
- Fix: moved the same governed action into the top row, adjacent to Create
  version, without duplicating mutation or permission logic.
- Post-fix: no P0/P1/P2 issue was observed in layout, interaction, authorization
  state, publish preflight behavior, or browser console.

final result: passed

---

# Skill Credential Mapping Copy Cleanup QA

Date: 2026-08-20

## Visual truth and rendered evidence

- Source truth: `/var/folders/fd/s9_xw3qn0gdfb1ymjmg_md_c0000gn/T/codex-clipboard-efc540b3-56cd-4a47-a958-6fd8d29a33a9.png`
  (`807 × 159` pixels).
- Full implementation screenshot:
  `/tmp/actweave-skill-credential-clean-host-full.jpg` (`1280 × 720` pixels).
- Same-state comparison:
  `/tmp/actweave-skill-credential-clean-comparison.jpg` (`982 × 310` pixels).
- Browser viewport: `1280 × 720` CSS px. The focused implementation row was
  measured at `982 × 96` CSS px and normalized to the source width in the
  comparison image.
- State: authenticated `transport-sos-copilot` Project Skill detail for
  `trans-text-route-query`, Draft version 2, Runtime credentials tab. The
  `TEXT_ROUTE_DB_HOST` row used `文本路由数据库 · version 1` and source
  `env.TEXT_ROUTE_DB_HOST` during inspection; the temporary selection was
  discarded after capture, so no project binding data was changed.

## Comparison and required fidelity surfaces

- Removed the two redundant visible helper labels and the repeated mapping
  summary identified by the source red boxes.
- Preserved the target environment variable heading, required/configured status,
  Credential selector, source-field selector, error surface, and management link.
- Fonts, typography, color tokens, borders, controls, and icon assets are
  unchanged. Only redundant copy and its occupied vertical space were removed.
- No image asset or custom font changed. The full-view and normalized focused
  comparison were both inspected against the supplied source state.

## Interaction, accessibility, and console checks

- Selected both the project Credential and exact source environment field in the
  live browser and confirmed the row remained functional.
- Removed visible labels were retained as unique `aria-label` values, so each
  select remains addressable by its target environment variable name.
- Confirmed the local edit was discarded and the detail sheet reported no dirty
  state afterward.
- Browser console contained zero errors and zero warnings in the exercised state.
- Frontend unit suite passed `758/758`; frontend check reported zero errors and
  five pre-existing unused-variable warnings in admin operation types.

## Findings and comparison history

- Pass 1: the row repeated the target variable in three places around two already
  self-explanatory controls, making a five-row mapping form visually noisy.
- Fix: removed only the three red-boxed visible text surfaces while preserving
  semantic control names for assistive technology.
- Post-fix: no P0/P1/P2 issue was observed in the requested row, its interactions,
  accessibility naming, or browser console.

final result: passed

---

# Skill Credential source-field mapping design QA

Date: 2026-08-20

## Live environment and evidence

- Authenticated local `dev-daemon` through `http://localhost:2026`.
- Project: `transport-sos-copilot`.
- Project Skill: `trans-text-route-query`, exact Draft version 2.
- Browser viewport: `1280 × 720` CSS px.
- Runtime Credential overview screenshot:
  `/tmp/actweave-skill-credential-source-field-mapping.png`.
- Exact alias mapping screenshot:
  `/tmp/actweave-skill-credential-alias-mapping.jpg`.
- Read-only publish preflight screenshot:
  `/tmp/actweave-skill-publish-readonly-preflight.jpg`.

## Verified behavior

- The workbench renders five `required-secrets` declarations read from the exact
  Draft's `SKILL.md`.
- Each target environment variable has two independently labelled controls:
  Project Credential version and source environment field.
- Selecting the existing multi-field Credential exposes only its declared `env`
  fields. For `TEXT_ROUTE_DB_NAME`, the browser displayed four source choices:
  `TEXT_ROUTE_DB_HOST`, `TEXT_ROUTE_DB_PASSWORD`, `TEXT_ROUTE_DB_PORT`, and
  `TEXT_ROUTE_DB_USER`.
- A local unsaved alias mapping
  `TEXT_ROUTE_DB_NAME ← 文本路由数据库 v1 / env.TEXT_ROUTE_DB_HOST` rendered
  with a configured status, proving that the UI no longer assumes source and
  target names are equal.
- The temporary selection was discarded and was never sent to the Gateway.
- The publish dialog contains only the exact-version mapping summary and CAS
  metadata. It has no Credential or source-field selector, reports `0/5`
  required mappings, offers a return-to-workbench action, and disables publish.
- Browser console inspection after the exercised flow returned zero errors and
  zero warnings.

## Safety boundary

- No Credential secret value was read, rendered, typed, logged, cached, or
  transmitted during browser verification.
- Existing project mapping state was not changed. Persistence and runtime alias
  materialization are covered separately by PostgreSQL, Local Provider, and AIO
  Provider regression tests.

final result: pass

---

# Skill detail actions and workbench selected-state design QA

Date: 2026-08-19

## Visual truth and rendered evidence

- Action-order source truth: `/var/folders/fd/s9_xw3qn0gdfb1ymjmg_md_c0000gn/T/codex-clipboard-2a5d3ab0-f161-4c40-bb46-6e580ddf23d7.png`
  (`966 × 166` pixels).
- Tab-state source truth: `/var/folders/fd/s9_xw3qn0gdfb1ymjmg_md_c0000gn/T/codex-clipboard-7256c902-f4a6-4a7e-9725-41107ae42f5b.png`
  (`646 × 108` pixels).
- Full implementation screenshot: `/tmp/actweave-skill-ui-full-after.png`
  (`1065 × 1020` pixels).
- Focused Files-selected screenshot: `/tmp/actweave-skill-ui-focused-after.png`
  (`520 × 235` pixels).
- Focused Credential-selected screenshot:
  `/tmp/actweave-skill-ui-credential-selected.png` (`520 × 235` pixels).
- Browser viewport: `1065 × 1020` CSS px at `devicePixelRatio=1`, matching
  the desktop scale of the supplied screenshots as closely as their crops allow.
  The source CSS size and density are unknown, so no absolute-pixel typography or
  spacing claim is made.
- State: authenticated `transport-sos-copilot` Project Skill detail for
  `trans-e2e-query`, published version 1. Both Files-selected and
  Credential-selected states were exercised. The in-app browser rendered English
  locale copy while the supplied source crops use Chinese; the comparison target
  is action order and selected-state treatment, not translated copy.

## Full-view and focused evidence

- The full implementation capture shows the complete Skill detail sheet with the
  action row in the order Create version, AI revise, Delete Skill.
- Focused browser inspection measured the action buttons left-to-right at
  `x=25`, `x=135`, and `x=249.625` CSS px.
- In the Files-selected state, the selected tab computed to
  `rgb(23, 24, 28)` with white text; the unselected tab was transparent with dark
  text. After clicking Credential environment variables, those visual states and
  `aria-selected` values swapped.
- The source captures and implementation captures were both opened and inspected.
  A strict same-input contact sheet could not be produced: the in-app browser
  security policy rejected the local data-URL comparison page and prohibited an
  alternate-browser workaround. This prevents a formal side-by-side pass under
  the Product Design QA contract even though the targeted live states were
  measured and captured.

## Required fidelity surfaces

- Fonts and typography: existing ActWeave typography, sizes, weights, wrapping,
  and antialiasing are unchanged by this patch.
- Spacing and layout rhythm: the existing button and tab spacing, padding,
  radii, and section rhythm are preserved; only action order and active variant
  changed.
- Colors and visual tokens: the selected tab now uses the existing primary
  button tokens for a high-contrast dark background and white foreground;
  unselected tabs retain the existing ghost treatment. No hard-coded color was
  introduced.
- Image quality and asset fidelity: no raster product assets or custom icons are
  added or changed; existing icon components remain sharp at the inspected DPR.
- Copy and content: no product copy changed. The browser/source locale mismatch is
  expected and not part of the requested change.

## Interaction, accessibility, and console checks

- Clicked both workbench tabs and confirmed the panel state changed correctly.
- `aria-selected` changed from Files `true` / Credential `false` to Files `false`
  / Credential `true`; the existing roving-tabindex keyboard contract remains
  covered by unit tests.
- Browser console contained zero errors in the exercised state.

## Findings and comparison history

- Pass 1 source findings: destructive Delete appeared before AI revise, and the
  Files/Credential tabs lacked a clearly differentiated selected state.
- Fixes: moved AI revise before Delete and changed the selected tab from the
  low-contrast secondary variant to the existing high-contrast primary variant
  in both the version workbench and the Skill Builder candidate workbench.
- Post-fix live evidence: correct measured action order, visible dark/white active
  tab treatment in both tab states, working interaction, and zero console errors.
- No additional P0/P1/P2 issue was observed in the individually inspected live
  evidence. Formal comparison remains blocked solely because the required
  same-input source/implementation composition was disallowed by browser policy.

final result: blocked
