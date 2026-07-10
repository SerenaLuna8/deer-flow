# Skills Markdown Preview — Design QA

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

final result: passed
