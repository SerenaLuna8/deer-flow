# Design QA Reports

## Skills Markdown Preview

## Evidence

- Source visual: `docs/pr-evidence/skills-markdown-preview/source-mockup.png`
- Final desktop implementation: `docs/pr-evidence/skills-markdown-preview/implementation-desktop.png`
- Final mobile implementation: `docs/pr-evidence/skills-markdown-preview/implementation-mobile.png`
- Desktop iteration 1 comparison (source left, implementation right): `docs/pr-evidence/skills-markdown-preview/comparison-desktop-01.png`
- Final desktop comparison (source left, implementation right): `docs/pr-evidence/skills-markdown-preview/comparison-desktop-02.png`
- Final focused header comparison: `docs/pr-evidence/skills-markdown-preview/comparison-header-02.png`
- Final focused body comparison: `docs/pr-evidence/skills-markdown-preview/comparison-body-02.png`

The desktop comparison uses the same `1808 x 870` page viewport and the same open
`academic-paper-review` Sheet state. The mobile capture uses `390 x 844` with the
same skill open.

## Comparison history

### Iteration 1

- P0: none.
- P1: the reused Markdown renderer produced a `30px` H1, `24px` H2, and `16px`
  body/list copy. In the focused body comparison this was visibly too large and
  sparse for the compact reading rhythm established by the approved source.
- Fix: added preview-scoped typography overrides only at the `MarkdownContent`
  call site. The shared Markdown renderer and its raw-HTML-disabled pipeline were
  not changed.
- P2: none requiring a correction. The live skill has a longer real description
  and no license value, while the mock uses abbreviated sample copy and `MIT`;
  the implementation correctly renders API data rather than inventing metadata.

### Iteration 2 — final

- P0: none.
- P1: none.
- P2: none.
- Markdown hierarchy now follows the target density: H1 `20px/28px`, H2
  `18px/28px`, and body/list copy `14px/24px`.
- The Sheet remains the approved wide desktop treatment (`800px` maximum) and
  full viewport width on mobile.

## Final visual checks

- Typography and rhythm: pass. Header hierarchy, compact Markdown headings,
  paragraph spacing, list rhythm, and readable line length match the intended
  right-side document viewer.
- Spacing and density: pass. The header has a clear metadata/content boundary;
  the scroll body keeps consistent horizontal padding and a `72ch` reading
  measure.
- Tokens and shell fidelity: pass. The implementation uses DeerFlow background,
  border, muted text, badge, focus-ring, Sheet shadow, and standard modal overlay
  tokens. The live workspace sidebar and breadcrumb remain intact; the source
  mock intentionally isolates the feature surface.
- Icons and assets: pass. No new decorative assets were invented. Row affordance
  uses the existing Lucide chevron and the Sheet keeps its existing close icon.
- Copy and metadata: pass. `SKILL.md`, category, enabled state, description, and
  conditional license use localized UI copy plus live API data. YAML frontmatter
  is not visible in the rendered document.
- Selected row and background: pass. The selected row receives restrained border
  and muted-background treatment while the existing modal overlay preserves clear
  focus separation.
- Accessibility: pass. The row trigger and Switch remain separate controls;
  close-button and Escape paths both returned focus to the exact
  `academic-paper-review` trigger with `aria-expanded="false"`.

## Responsive and interaction checks

- At `390 x 844`, measured Sheet bounds were `x=0`, `width=390`, `right=390`.
- Document and dialog horizontal overflow were both zero
  (`root scrollWidth=390`, dialog `scrollWidth=389`).
- Metadata and the full live description wrapped without clipping.
- Table and code containers retain the existing internal `overflow-x-auto`
  boundary; the E2E fixture also verifies a 320-character token and wide code
  block without page overflow.
- Row open, close control, Escape, lazy loading, Switch isolation, distinct
  403/404/500 errors with Retry, stale-selection protection, and raw-HTML safety
  are covered by the 12-case browser E2E suite.
- Browser console after desktop and mobile interaction contained no warnings or
  errors.

---

## Memory Inbox

- Source visual truth: `/Users/jiangfeng/.codex/generated_images/019f4af4-2a5d-7f51-99f5-cf604f2c63ec/exec-5828b811-6528-4af0-9785-772f75976687.png`
- Rendered implementation: `/Users/jiangfeng/deer-flow/.worktrees/memory-inbox-redesign/.superpowers/sdd/memory-qa-desktop-1672x941.png`
- Full-view comparison: `/Users/jiangfeng/deer-flow/.worktrees/memory-inbox-redesign/.superpowers/sdd/memory-qa-comparison-full.png`
- Mobile evidence: `/Users/jiangfeng/deer-flow/.worktrees/memory-inbox-redesign/.superpowers/sdd/memory-qa-mobile-390x844.png`
- Viewport: `1672 × 941` desktop and `390 × 844` mobile, DPR 1
- State: Chinese, light theme, populated memory, All filter, summaries collapsed, management menu closed for the comparison capture

## Full-view comparison evidence

The source and browser-rendered implementation were placed in one native-size comparison board. The implementation preserves the selected composition: compact header with secondary management and primary add actions, one overview strip, search with segmented filters, a full-width divided fact list, and a collapsed full-width smart-summary disclosure.

The app shell and the stored-memory schema explain the visible intentional differences. DeerFlow keeps its workspace rail and breadcrumb; facts expose one stored content field rather than the mockup's invented title/description pair and source-count button. The implementation therefore uses the raw stored content as the only primary text and keeps learned-source navigation in metadata, preserving the product contract.

No separate focused crop was required: both full views were captured at the same native viewport and remain readable at original resolution; there are no raster illustrations, logos, photographs, or dense charts requiring an asset-level crop.

## Required fidelity surfaces

- Fonts and typography: existing DeerFlow font stack and optical weights are retained. Heading, body, metadata, and control hierarchy remain clear; mobile text wraps without document overflow.
- Spacing and layout rhythm: the five-layer vertical hierarchy matches the source. Existing capability-page width and app chrome are retained deliberately. Shared list chrome and dividers remove the previous 36/64 imbalance and large empty panel.
- Colors and visual tokens: warm-neutral background, card, border, muted text, primary charcoal, and destructive tokens come from the existing theme; no raw light-only palette, gradient, or glass treatment was added.
- Image quality and asset fidelity: the target contains no required photographic or illustrative assets. Visible icons use the existing Lucide family; no handcrafted SVG, CSS art, emoji, or placeholder imagery was introduced.
- Copy and content: Chinese page labels, actions, overview labels, filters, empty/loading/error copy, and summary disclosure are coherent. Dynamic category values remain stored strings by design.
- Responsiveness and accessibility: the `390 × 844` capture has `scrollWidth === clientWidth`; controls wrap and remain usable. Loading is a named live status, icon-only actions retain labels, and the management menu uses the existing Radix keyboard/focus behavior.

## Interaction and runtime evidence

- In-app Browser opened the production build through an in-memory QA API proxy, without contacting or mutating the real Gateway memory.
- Management menu opened and exposed Import, Export, and destructive Clear actions.
- The page produced no browser console errors during the QA run.
- Automated acceptance additionally covers add/edit/delete, import/export/clear, learned-source navigation, summary search/filter behavior, 390px overflow, and persisted dark mode.

## Findings

No actionable P0, P1, or P2 visual mismatch remains.

P3 follow-up polish:

- The reference mockup is visually roomier because it contains descriptive fact text that does not exist in `UserMemory`; the implementation correctly avoids synthesizing or persisting absent data.
- On narrow screens, recent focus shares the overview's compact grid and may truncate earlier than the desktop reference. It remains readable and its View summaries action remains available, but a future polish pass could give it a full-width row.
- Standard category keys remain visible as stored English strings in Chinese locale. Localizing known keys would improve polish but must retain arbitrary custom-category support.

## Comparison history

Pass 1: desktop and mobile browser captures were compared against the approved source. No P0/P1/P2 mismatch was identified, so no visual-fix iteration was required. The intentional schema and app-shell differences above were classified as acceptable product constraints; the remaining items are P3 polish.

## Implementation checklist

- [x] Fact-first full-width list
- [x] Summary disclosure collapsed by default
- [x] Header, overview, toolbar, empty/loading/error states
- [x] Core operation contracts preserved
- [x] Desktop and 390px browser evidence
- [x] Console-error check
- [x] Source and implementation in one comparison input
      final result: passed
