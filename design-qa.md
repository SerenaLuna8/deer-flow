# Platform administration redesign QA

## Result

**Final result: passed.**

The redesign covers every platform-administration route, including the project-scoped
governance pages. No actionable P0, P1, or P2 visual, responsive, interaction, copy, or
accessibility finding remains in the tested states.

## Route inventory

The browser pass covered these rendered pages:

- `/admin/operations`
- `/admin/projects`
- `/admin/jobs`
- `/admin/audit`
- `/admin/assets/agents`
- `/admin/assets/skills`
- `/admin/assets/mcp`
- `/admin/assets/credentials`
- `/admin/settings/models`
- `/admin/projects/{project_id}/assets/agents`
- `/admin/projects/{project_id}/assets/skills`
- `/admin/projects/{project_id}/assets/mcp`
- `/admin/projects/{project_id}/assets/credentials`

The `/admin`, `/admin/assets`, `/admin/settings`, and project-asset index routes remain
intentional redirects into this inventory.

## Visual sources and evidence

Design grounding:

- Selected design direction:
  `docs/dev-main-code-analysis/evidence/admin-redesign-v2/selected-option-2.png`
- User-reported narrow inspector:
  `docs/dev-main-code-analysis/evidence/admin-redesign-v2/fidelity-round-2/02-user-failure-drawer.png`
- Before/after inspector comparison:
  `docs/dev-main-code-analysis/evidence/admin-redesign-v2/final/30-drawer-before-after-comparison.png`

Whole-platform review:

- Operations, projects, jobs, and audit desktop contact sheet:
  `docs/dev-main-code-analysis/evidence/admin-redesign-v2/final/44-operations-contact-sheet.png`
- Four system-asset pages desktop contact sheet:
  `docs/dev-main-code-analysis/evidence/admin-redesign-v2/final/45-system-assets-contact-sheet.png`
- Model settings and four project-asset pages desktop contact sheet:
  `docs/dev-main-code-analysis/evidence/admin-redesign-v2/final/46-model-project-assets-contact-sheet.png`
- Operations pages at 656px:
  `docs/dev-main-code-analysis/evidence/admin-redesign-v2/final/60-operations-656-contact-sheet.png`
- System assets at 656px:
  `docs/dev-main-code-analysis/evidence/admin-redesign-v2/final/61-system-assets-656-contact-sheet.png`
- Model and project assets at 656px:
  `docs/dev-main-code-analysis/evidence/admin-redesign-v2/final/62-model-project-656-contact-sheet.png`
- Representative 390px pages:
  `docs/dev-main-code-analysis/evidence/admin-redesign-v2/final/67-key-pages-390-contact-sheet.png`

Focused interaction evidence:

- 1280px list/detail split:
  `docs/dev-main-code-analysis/evidence/admin-redesign-v2/final/27-system-skill-split-1280.png`
- 1920px list/detail split:
  `docs/dev-main-code-analysis/evidence/admin-redesign-v2/final/28-system-skill-split-1920.png`
- 1963×1248 inspector capture:
  `docs/dev-main-code-analysis/evidence/admin-redesign-v2/final/29-system-skill-inspector-1963x1248.png`
- 390px model editor:
  `docs/dev-main-code-analysis/evidence/admin-redesign-v2/final/68-model-editor-390.png`
- Project system-asset binding dialog:
  `docs/dev-main-code-analysis/evidence/admin-redesign-v2/fidelity-round-2/24-project-agent-binding-dialog.png`
- 390px project binding dialog:
  `docs/dev-main-code-analysis/evidence/admin-redesign-v2/fidelity-round-2/28-project-binding-390.png`
- Expanded navigation:
  `docs/dev-main-code-analysis/evidence/admin-redesign-v2/final/25-sidebar-expanded.png`
- Collapsed navigation tooltip:
  `docs/dev-main-code-analysis/evidence/admin-redesign-v2/final/26-sidebar-collapsed-tooltip.png`
- Collapsed return-action tooltip:
  `docs/dev-main-code-analysis/evidence/admin-redesign-v2/final/71-admin-return-workspace-collapsed-hover.png`
- Expanded return action:
  `docs/dev-main-code-analysis/evidence/admin-redesign-v2/final/73-admin-return-workspace-expanded.png`
- Mobile drawer return action:
  `docs/dev-main-code-analysis/evidence/admin-redesign-v2/final/74-admin-return-workspace-mobile.png`
- Verified `/workspace` destination:
  `docs/dev-main-code-analysis/evidence/admin-redesign-v2/final/72-admin-return-workspace-destination.png`

## Visual review findings and corrections

Screenshots were reviewed for hierarchy, density, spacing, line length, empty states,
alignment, semantic color, and task flow. They were not treated as acceptance by
themselves.

1. The original Skill inspector was too narrow, modal, and underused its height.
   - The desktop inspector is now non-modal and keeps the catalog usable.
   - Its width is `clamp(32rem, 34vw, 48rem)` instead of a small fixed drawer.
   - At 1280px the panel is 512px and the remaining 625px catalog switches to cards.
   - At 1920px the panel grows to 652.8px while the 1124px catalog retains its table.
   - At 1963px the panel is 667.4px.
   - Version metadata and content are shown directly instead of opening into a needless
     empty version state.

2. The list could become narrower than its fixed table after the inspector opened.
   - System and Credential catalogs now use container queries, so the representation is
     chosen from the real list width rather than the browser width.
   - The split view has no internal or document horizontal overflow.

3. The operations overview originally left a large blank column beside a long provider
   list.
   - Aggregate usage and provider health are now full-width sections.
   - Provider health uses a responsive three-column grid.
   - The final provider spans the remaining track when a row is incomplete, removing the
     gray phantom cell discovered during screenshot review.

4. The platform shell and page hierarchy were inconsistent.
   - One 64px collapsed / 240px expanded administration rail now owns global navigation.
   - Collapsed items expose names on hover and keyboard focus.
   - The top bar no longer claims a fake unconditional healthy state.
   - Asset sub-navigation, project context, headings, descriptions, metrics, filters,
     tables, cards, and empty states use one compact visual language.

5. Project asset details appeared after long directories without moving the user's
   viewport.
   - Detail triggers now expose `aria-controls` and `aria-expanded`.
   - Selecting a project asset scrolls and focuses its detail region.
   - Closing restores focus to the triggering control.

6. Important safety and error content was easy to miss.
   - Credential pages visibly state that only metadata is returned and secret values are
     never shown again.
   - Gateway-unavailable state now has a localized title, explanation, alert semantics,
     and reload action.
   - Model controls name the affected model, and form help/error content is connected with
     `aria-describedby`.

7. Chinese and English interface copy was mixed with raw closed-set codes.
   - Credential types, MCP transports, and Credential payload groups are localized while
     preserving their technical codes in parentheses where useful.
   - Unknown extension values still fall back to the raw code instead of being mislabeled.

## Responsive measurements

- Desktop sweep: all 13 rendered pages at 1487px had
  `documentElement.scrollWidth === clientWidth`.
- Tablet sweep: all 13 rendered pages at 656px had
  `documentElement.scrollWidth === clientWidth`; dense tables switched to cards.
- Phone sweep: all 13 rendered pages at 390px had
  `documentElement.scrollWidth === clientWidth`.
- 1280px selected-asset state:
  - document `1265 = 1265`
  - catalog width `625px`
  - inspector width `512px`
  - 20 visible catalog cards and no visible fixed-width table
- 1920px selected-asset state:
  - document `1905 = 1905`
  - catalog width `1124.2px`
  - inspector width `652.8px`
  - dense catalog table remains visible
- Mobile model editor:
  - document `390 = 390`
  - dialog width `390px`
  - fixed action footer remains visible while the form scrolls

## Interaction and accessibility checks

- Sidebar expand/collapse and persisted state
- Collapsed-item hover tooltip
- Active route state and mobile navigation
- Desktop collapsed, desktop expanded, and mobile navigation all expose a localized
  "return to project workspace" action; each was clicked through to `/workspace`
- System asset search, filters, pagination, selection, and version detail
- Inspector Escape close and focus restoration to the exact selected row
- Full-screen mobile asset detail with a valid controlled-region ID
- Project asset source tabs, search, binding dialog, and responsive cards
- Model search, status filter, refresh, edit dialog, and model-specific action names
- Loading, empty, unavailable, and retry states
- Browser logs: no application errors; only development HMR, React DevTools, and Fast
  Refresh messages were present

## Automated verification

- Administration-focused regression: **105 passed**, 0 failed, 0 skipped.
- Focused administration-shell return-path regression: **20 passed**, 0 failed, 0 skipped.
- Full frontend regression: **1,414 passed**, 0 failed, 0 skipped.
- ESLint and TypeScript (`pnpm check`): passed.
- Production Next.js build: passed.
- Static-mode Next.js build: passed.
- `git diff --check`: passed.

## Implementation checklist

- [x] Unified platform-administration shell
- [x] Expandable and collapsible desktop navigation
- [x] Accessible collapsed navigation labels
- [x] Desktop and mobile return path from platform administration to `/workspace`
- [x] Operations, projects, jobs, and audit redesign
- [x] Agent, Skill, MCP, and Credential system catalogs
- [x] Wide responsive system-asset inspector
- [x] Model settings catalog and editor
- [x] Four project-scoped asset governance pages
- [x] Localized Chinese and English UI copy
- [x] Safe Credential and model-editing boundaries
- [x] Desktop, tablet, and phone browser review for every page
- [x] Real interaction, console, test, type, lint, and build verification

## Reasoning duration disclosure QA

Reference:
`codex-clipboard-f2beafc5-3c12-4e6a-a284-e44fa8d1a916.png`.

- Matched the compact, borderless one-line disclosure: blue 14px Atom icon,
  14px muted label, 8px gaps, and adjacent rotating chevron.
- Completed Subagent/execution groups no longer interrupt the terminal answer;
  their active and failed-before-answer states remain visible while work is in progress.
- Four real DeepSeek calls were exercised in the signed-in project chat. Persisted observed
  reasoning intervals included 1.548s, 2.099s, 2.820s, and 3.509s.
- The fourth call rendered `已思考（用时 2 秒）` while its independent Run total was
  `本次任务耗时 4 分 13 秒`, demonstrating that the two clocks are not conflated.
- Expanding and collapsing the disclosure exposed the reasoning content, and a full browser
  reload retained both the 2-second reasoning label and the 4-minute-13-second Run total.
- Evidence:
  `docs/dev-main-code-analysis/evidence/reasoning-duration/02-final-compact-reasoning-row-crop.png`,
  `docs/dev-main-code-analysis/evidence/reasoning-duration/05-real-reasoning-2s-task-total-4m13s.png`,
  and
  `docs/dev-main-code-analysis/evidence/reasoning-duration/06-after-refresh-persisted-duration.png`.
- Automated verification: backend focused regression 98 passed (5 environment-gated skips);
  frontend focused regression 136 passed; full frontend regression 1,443 passed; frontend
  lint/type check and backend Ruff checks passed.

Final result: passed.

## Platform asset tab and content alignment follow-up

### Comparison target

- Source visual truth: `docs/dev-main-code-analysis/evidence/admin-assets-alignment/reference-before.png`
- Browser-rendered implementation: `docs/dev-main-code-analysis/evidence/admin-assets-alignment/admin-assets-aligned-1280x720.png`
- Full-view comparison: `docs/dev-main-code-analysis/evidence/admin-assets-alignment/before-after-comparison.png`
- Focused alignment comparison: `docs/dev-main-code-analysis/evidence/admin-assets-alignment/alignment-focused-comparison.png`
- Source pixels: 3616 x 1804; its originating CSS viewport and density are unknown because it is a user-provided desktop capture.
- Implementation pixels and CSS viewport: 1280 x 720. The browser reported devicePixelRatio 2; the saved Browser capture was normalized to CSS-pixel dimensions.
- Comparison normalization: each full-view image was scaled proportionally to 1000 px wide and padded, without cropping; the focused comparison used source crop 2000 x 700 and implementation crop 1060 x 420, then scaled each to 1000 px wide.
- State: authenticated Chinese system-admin Agent catalog, expanded administration navigation, no asset detail open.

### Findings and comparison history

1. Initial P2 spacing/layout mismatch: the source capture placed the Tab row on a centered 96rem frame while the catalog content used a right-aligned 120rem frame. Their left edges visibly diverged on a wide viewport; the Tab and page also used different padding breakpoints.
2. Fix: the platform asset page now uses the same centered 96rem maximum width as its Tab row, and both use 16px / 20px / 24px responsive horizontal padding at the same breakpoints.
3. Post-fix evidence: at the 1280 x 720 browser viewport, the Tab frame and `main` both measured x=240 and width=1040; the first Tab and content heading both measured x=264. The Skill route repeated the same x=264 content alignment.
4. Full-view and focused comparison show the intended correction. No actionable P0, P1, or P2 mismatch remains.

### Required fidelity surfaces

- Fonts and typography: unchanged; the existing product type scale, weights, wrapping, and antialiasing remain intact.
- Spacing and layout rhythm: passed; Tab and content now share exact horizontal anchors, with no observed overflow.
- Colors and visual tokens: unchanged and still use the existing administration design-system tokens.
- Image quality and asset fidelity: no application imagery was added or replaced; the existing Lucide icon family remains consistent.
- Copy and content: unchanged and coherent in the tested Chinese state.
- Responsiveness and accessibility: shared responsive padding removes the prior 4px breakpoint drift; Tab navigation remained keyboard-semantic and Agent → Skill → Agent navigation worked.

### Runtime checks

- Browser console: no application errors; only React DevTools, HMR, and Fast Refresh development messages.
- Primary interaction: Agent → Skill → Agent Tab navigation passed.
- Focused unit regression: 34 passed, 0 failed.
- Frontend lint and TypeScript check: passed.

final result: passed
