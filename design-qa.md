# 添加 MCP 与项目凭据向导 Design QA

## Scope and evidence

- Source visual truth: `/Users/jiangfeng/.codex/generated_images/019fc597-932b-7d32-a5a5-a225e82c9ffe/exec-2d61e39c-b73e-4868-a817-6da101a8d019.png`.
- Final browser-rendered implementation: `/Users/jiangfeng/.codex/visualizations/2026/08/03/019fc597-932b-7d32-a5a5-a225e82c9ffe/mcp-create-desktop-final.png`.
- Mobile query-auth evidence: `/Users/jiangfeng/.codex/visualizations/2026/08/03/019fc597-932b-7d32-a5a5-a225e82c9ffe/mcp-create-mobile-query.png`.
- Full-view comparison: `/Users/jiangfeng/.codex/visualizations/2026/08/03/019fc597-932b-7d32-a5a5-a225e82c9ffe/mcp-create-comparison-final.jpg` (left: source, right: implementation).
- Focused preview/flow comparison: `/Users/jiangfeng/.codex/visualizations/2026/08/03/019fc597-932b-7d32-a5a5-a225e82c9ffe/mcp-create-focused-final.jpg` (left: source, right: implementation).
- State: authenticated project Admin, light theme, HTTP transport, request-header authentication, exact `headers.Authorization` Credential match selected. Query-parameter authentication and fixed `query.key` Credential creation were also tested.

## Viewport and normalization

- Source pixels: `1536 x 1024`.
- Desktop CSS viewport and screenshot: `1536 x 1024`, `devicePixelRatio: 1`; no density normalization was needed for the full-view comparison.
- Mobile CSS viewport and screenshot: `390 x 844`, `devicePixelRatio: 1`.
- Focused comparison: the source right panel was cropped to `820 x 836`; the implementation crop was `717 x 756`, proportionally scaled to `820 x 865`, and white-padded without aspect-ratio distortion.

## Fidelity review

- Fonts and typography: retained ActWeave's established serif wordmark and system sans UI stack. Headings, labels, field help, preview values, and compact step explanations remain legible at desktop and mobile sizes.
- Spacing and layout rhythm: the final dialog uses the full useful desktop width, keeps the action footer visible, and places the complete authentication and Project Credential selector above the fold at `1536 x 1024`. Name/slug and transport/URL pairs absorb the required product fields without losing the source's two-column hierarchy.
- Colors and visual tokens: uses ActWeave's existing neutral `background`, `muted`, `border`, foreground, success, and primary tokens. The source mock's purple accent was intentionally not introduced as a page-private palette.
- Image quality and assets: the target contains no raster product imagery. All visible icons use the repository's existing Lucide family; no inline SVG, CSS drawing, emoji, or placeholder asset was introduced.
- Copy and content: the UI distinguishes request headers, query parameters, and no authentication; accepts field names only; exposes exact-match Credential selection and creation; and explains encrypted source, publication status, and the save/select/approve sequence.
- Responsive behavior: at `390 x 844`, the dialog and document both measure `390px` wide, have no horizontal overflow, and keep the primary and cancel actions visible while the form body scrolls independently.

## Primary interactions and accessibility

- Opened the Add MCP dialog and filled name, slug, description, HTTPS URL, and transport.
- Switched among no-auth, request-header, and query-parameter modes. Query mode produced `query.key`, showed only the matching Query Credential, and hid the Header Credential.
- Selected an exact-match Project Credential and verified the submit action changed to “添加、绑定凭据并发布”.
- Opened “新建项目凭据” from query mode and verified `group=query` was disabled, `field=key` was read-only, and the secret input remained empty.
- Switched back from query to request-header mode and verified the default field changed from `key` to `Authorization`, with only the matching Header Credential visible.
- Verified the mobile document has no horizontal overflow and the persistent submit action stays visible.
- Browser console errors: none.

## Comparison history

1. Pass 1 evidence: `/Users/jiangfeng/.codex/visualizations/2026/08/03/019fc597-932b-7d32-a5a5-a225e82c9ffe/mcp-create-desktop-pass1.png` and `/Users/jiangfeng/.codex/visualizations/2026/08/03/019fc597-932b-7d32-a5a5-a225e82c9ffe/mcp-create-comparison-pass1.jpg`.
   - P2 findings: the dialog was materially narrower than the source, the Project Credential selector fell below the initial scroll fold, and the right column was underused.
   - Fixes: expanded the desktop dialog, paired name/slug and transport/URL, reduced unnecessary vertical gaps, and kept the footer fixed.
2. Pass 2 evidence: `/Users/jiangfeng/.codex/visualizations/2026/08/03/019fc597-932b-7d32-a5a5-a225e82c9ffe/mcp-create-desktop-pass2.png` and `/Users/jiangfeng/.codex/visualizations/2026/08/03/019fc597-932b-7d32-a5a5-a225e82c9ffe/mcp-create-comparison-pass2.jpg`.
   - P2 finding: the right preview still omitted the source/status explanation and the publication steps were too terse relative to the selected mock.
   - Fixes: added a read-only preview label, encrypted Credential source, truthful publication status, and one-line detail for every publication step.
3. Final interaction pass.
   - P1 finding: switching `query.key` back to request-header mode initially preserved `key`, so no header Credential could match.
   - Fix: added a deterministic default-field transition (`key` ↔ `Authorization`) that preserves custom names, then reverified both modes in the browser and in unit tests.
   - Post-fix evidence: final desktop/mobile screenshots, exact-match selector state, fixed query Credential field state, no horizontal overflow, and an empty browser error log.

## Findings

- P0: none.
- P1: none.
- P2: none.
- P3: the static source uses purple numbered step markers, while the implementation uses ActWeave's neutral primary token and existing check icons. This is an intentional design-system adaptation; hierarchy and sequence remain equivalent.
- P3: the implementation includes required slug and description fields that the visual exploration omitted. They are compacted into the same left-column hierarchy without hiding the Credential decision.

final result: passed

---

# MCP 连接摘要与 Credential 槽位精简 Design QA

## Scope and evidence

- Source visual truth: `/var/folders/fd/s9_xw3qn0gdfb1ymjmg_md_c0000gn/T/codex-clipboard-c1015b1b-b7f8-49cf-bcb7-f26853f45ee7.png`; the accompanying annotation requires the three connection fields on one row and removal of the Credential-slot section.
- Browser-rendered connection summary: `/Users/jiangfeng/.codex/visualizations/2026/08/03/019fc597-932b-7d32-a5a5-a225e82c9ffe/mcp-connection-three-column-final.jpg`.
- Focused before/after comparison: `/Users/jiangfeng/.codex/visualizations/2026/08/03/019fc597-932b-7d32-a5a5-a225e82c9ffe/mcp-connection-three-column-comparison.jpg` (left: source; right: implementation).
- Full implementation evidence: `/Users/jiangfeng/.codex/visualizations/2026/08/03/019fc597-932b-7d32-a5a5-a225e82c9ffe/mcp-detail-no-credential-slots-final.jpg`.
- Mobile implementation evidence: `/Users/jiangfeng/.codex/visualizations/2026/08/03/019fc597-932b-7d32-a5a5-a225e82c9ffe/mcp-detail-no-credential-slots-mobile.png`.
- State: authenticated project Admin, light theme, active and published project-owned MCP detail with header Credential authentication configured internally.

## Viewport and normalization

- Source pixels: `891 x 196`.
- Browser CSS viewport and full screenshot: `1280 x 720`, `devicePixelRatio: 1`.
- Mobile verification viewport and screenshot: `390 x 844`, with measured document width equal to the viewport width.
- Implementation connection crop: `868 x 110`; it is white-padded without scaling to `891 x 196` for the side-by-side comparison. No density normalization or aspect-ratio distortion was applied.
- The three browser-measured cards share `y=372` and `height=78`; widths are `201`, `201`, and `402` CSS pixels for transport, timeout, and URL respectively.

## Fidelity review

- Fonts and typography: retained the existing system UI and monospace value styles, including the readable full URL.
- Spacing and layout rhythm: changed the desktop connection summary from two rows to one weighted three-column row. The URL receives twice the width of each compact field, while the mobile base layout remains stacked.
- Colors and visual tokens: retained the existing neutral borders, white surface, foreground, and muted-label tokens with no new palette or effects.
- Image quality and assets: this region has no imagery or new icon requirement; no placeholder, CSS art, or substitute asset was added.
- Copy and content: transport, timeout, and complete `/api/mcp` URL remain visible. “Credential 槽位”, slot names, grant state, and schema fields are absent from the detail presentation; configured create/edit still owns Credential selection.

## Primary interactions and accessibility

- Opened the existing project MCP detail from its accessible list button and kept edit, disable, service test, and disclosure controls working.
- The dialog snapshot contains the three connection labels in source order and no “Credential 槽位” heading.
- At `390px`, the three cards stack vertically at the same width without horizontal overflow, and the Credential-slot heading remains absent.
- Browser verification found zero matching Credential-slot headings and an empty console error log.

## Comparison history and findings

1. Initial source evidence showed transport and timeout on the first row with URL spanning a second row.
2. The post-fix focused comparison shows all three cards on one row, with a wider URL card that keeps the complete value readable.
3. The full implementation evidence shows the service-tool section flowing directly into non-sensitive and technical configuration disclosures, with no Credential-slot section between them.

- P0: none.
- P1: none.
- P2: none.
- P3: none.

The focused comparison covers the precise layout change; the full-sheet screenshot separately verifies the requested section removal.

final result: passed

# MCP 服务工具详情 Design QA

## Scope

- Reference: `/var/folders/fd/s9_xw3qn0gdfb1ymjmg_md_c0000gn/T/codex-clipboard-8c7ca807-dcce-4d22-b485-1e6bd29a5894.png`
- Desktop evidence: `/Users/jiangfeng/.codex/visualizations/2026/08/01/019fbd9a-3f5b-7b60-ad70-0c26ac70b238/mcp-tools-detail-tools.png`
- Mobile evidence: `/Users/jiangfeng/.codex/visualizations/2026/08/01/019fbd9a-3f5b-7b60-ad70-0c26ac70b238/mcp-tools-detail-mobile.png`
- Side-by-side comparison: `/Users/jiangfeng/.codex/visualizations/2026/08/01/019fbd9a-3f5b-7b60-ad70-0c26ac70b238/mcp-tools-design-qa-comparison.png`
- Tested state: published project MCP version, successful Worker discovery, 15 bounded tool records.

## Visual comparison

- Compared the reference list and the implementation list together using equal `835 x 488` focused regions.
- Preserved the reference hierarchy: tool name on the leading side, description on the trailing side, one divider per tool, and compact scanning density.
- Reused ActWeave typography, neutral surfaces, borders, radii, and spacing instead of importing the reference application's gray palette verbatim.
- The desktop detail sheet is intentionally scrollable because it also contains version, connection, Credential, and lifecycle information above and below the tool inventory.
- At `390 x 844`, each row changes to a stacked name/description layout; the dialog and document both remain `390px` wide with no horizontal overflow.

## Functional and accessibility checks

- Verified all 15 names and descriptions are present in the rendered accessibility tree.
- Verified the exact-version count, last successful discovery time, loading, empty, degraded, failed, stale, and retry presentation in focused tests.
- Verified the mobile list remains readable without clipped names or descriptions.
- Browser console errors and warnings: none.
- No endpoint secret, Credential identifier, request schema, or raw discovery error is rendered.

## Findings

- P0: none.
- P1: none.
- P2: none.
- P3: the reference fits all 15 rows into a standalone table, while ActWeave shows the same inventory inside a richer detail sheet and therefore scrolls. This is intentional and does not block the task.

final result: passed

---

# 工作空间项目库重设计 Design QA

## Scope

- Source visual truth: `/Users/jiangfeng/.codex/generated_images/019fbd9a-3f5b-7b60-ad70-0c26ac70b238/exec-a8056b3a-3c96-4bcb-aec9-aa72712ffffd.png`
- Latest user refinement: remove the large page-level “工作空间” title and supporting copy, retain the compact “工作空间” context beside the ActWeave wordmark, and keep the single “创建项目” action in the project toolbar.
- Final desktop evidence: `/Users/jiangfeng/.codex/visualizations/2026/08/01/019fbd9a-3f5b-7b60-ad70-0c26ac70b238/workspace-redesign-title-block-removed-1280x720.png`
- Mobile evidence: `/Users/jiangfeng/.codex/visualizations/2026/08/01/019fbd9a-3f5b-7b60-ad70-0c26ac70b238/workspace-redesign-create-in-toolbar-mobile-390x844.png`
- Full-view comparison: `/Users/jiangfeng/.codex/visualizations/2026/08/01/019fbd9a-3f5b-7b60-ad70-0c26ac70b238/workspace-redesign-source-vs-final-no-duplicate.png` (left: source, right: implementation).
- Focused comparison: `/Users/jiangfeng/.codex/visualizations/2026/08/01/019fbd9a-3f5b-7b60-ad70-0c26ac70b238/workspace-redesign-focused-source-vs-final-no-duplicate.png` (left: source, right: implementation).
- State: authenticated system administrator, light theme, one active unpinned default project, no recoverable projects.

## Viewport and normalization

- Source pixels: `1487 x 1058`; normalized to `1440 x 1024` with Lanczos resampling for comparison.
- Desktop implementation: CSS viewport `1440 x 1024`, screenshot `1440 x 1024`, effective density `1`.
- Mobile implementation: CSS viewport and screenshot `390 x 844`, effective density `1`.
- The same expanded recovery state and project data were used for the desktop full-view comparison. The source mock's blue pinned icon is treated as illustrative state; the implementation correctly reflects the live project's unpinned state.

## Fidelity review

- Fonts and typography: retained ActWeave's existing serif wordmark and system sans UI stack. Heading scale, body hierarchy, monospace slug, button weight, and Chinese wrapping match the selected direction without introducing a new font dependency.
- Spacing and layout rhythm: page margins, title baseline, toolbar top edge, card height, icon block, CTA height, and recovery panel align closely after the first calibration pass. The final grid intentionally contains only real project cards after the user's duplicate-action refinement.
- Colors and tokens: uses existing `background`, `card`, `muted`, `border`, `selection`, and `selection-subtle` tokens. No gradients, glass effects, or page-private hard-coded palette was introduced.
- Image quality and assets: the target contains no raster artwork. All visible controls use the existing Lucide icon family; no inline SVG, CSS art, emoji, or placeholder imagery was added.
- Copy and content: project name, slug, description fallback, search/filter copy, count, entry action, and recovery empty state match the real product. The large page intro block and all secondary creation actions were removed; the compact navigation context remains.
- Responsive behavior: at `390px`, the header stays compact, controls stack without overlap, filters remain usable, project/recovery sections stay within the viewport, and measured document width equals viewport width with no horizontally overflowing elements.

## Primary interactions and accessibility

- Verified exactly one visible “创建项目” button, nested inside the project toolbar; it opens the existing create dialog and “取消” closes it.
- Verified the page contains no `main h1` or supporting intro copy, while the ActWeave header retains the compact “工作空间” context.
- Verified “仅看置顶” changes the pressed state and empty result, while “清除筛选” restores the project list.
- Verified the recovery disclosure exposes correct `aria-expanded` state and toggles closed/open from the keyboard-operable button.
- Verified the recovery heading/trigger uses valid `h2 > button` semantics and an inset focus ring that is not clipped.
- Verified project edit, pin, and enter controls remain present with their existing capability and route behavior.
- Browser console error log: empty.

## Comparison history

1. Initial implementation evidence: `/Users/jiangfeng/.codex/visualizations/2026/08/01/019fbd9a-3f5b-7b60-ad70-0c26ac70b238/workspace-redesign-iteration-1.png`.
   - Earlier P1/P2 findings: content was too narrow, toolbar tracks were out of proportion, card/control density was undersized, the recovery heading was nested invalidly inside its trigger, and the focus ring could be clipped.
   - Fixes: widened and recalibrated the page, set explicit toolbar tracks, increased the coordinated density scale, changed recovery semantics to `h2 > CollapsibleTrigger`, and moved the focus ring inset.
2. Refined desktop/mobile browser pass.
   - Post-fix evidence showed matching major-region proportions, valid semantic state, no horizontal overflow, and all primary interactions working.
3. Latest user refinement.
   - Finding: the persistent header action and grid add-card duplicated the same create operation.
   - Fix: removed the add-card, retained the consistent header action, added a regression assertion, and confirmed the accessibility tree contains exactly one “创建项目” button.
4. Toolbar placement refinement.
   - Finding: the title-row action was visually detached from the search/filter controls, and the wordmark repeated the page title.
   - Fix: moved the only creation action into the toolbar's trailing control group, removed the wordmark-side label, removed the empty-state creation duplicate, and reverified desktop/mobile layout, dialog behavior, and zero horizontal overflow.
5. Page-title correction.
   - Finding: the previous interpretation removed the compact header context instead of the large page intro shown by the user.
   - Fix: removed the full page-level title/description block, restored the compact wordmark-side context, and confirmed the toolbar now begins at `120px` in the `1280 x 720` desktop viewport with no overflow.

## Findings

- P0: none.
- P1: none.
- P2: none.
- P3: the expanded recovery chevron points upward in the implementation instead of downward as drawn in the static mock. This is an intentional interactive-state cue and remains consistent with the actual `aria-expanded` state.

final result: passed

---

# MCP 列表精简与项目自建启停 Design QA

## Scope and evidence

- Source visual truth (系统提供): `/var/folders/fd/s9_xw3qn0gdfb1ymjmg_md_c0000gn/T/codex-clipboard-779c1b57-50ac-4309-aabd-71860677d039.png`.
- Source visual truth (项目自建): `/var/folders/fd/s9_xw3qn0gdfb1ymjmg_md_c0000gn/T/codex-clipboard-3b98ac61-bf31-4ee5-b00c-dcb952e7044c.png`.
- Browser-rendered implementation screenshot: unavailable because the authenticated workspace could not reach Gateway.
- Blocker evidence: `/Users/jiangfeng/.codex/visualizations/2026/08/03/019fc597-932b-7d32-a5a5-a225e82c9ffe/mcp-list-gateway-blocked.jpg`.
- State: authenticated light-theme workspace; the project page was replaced by “网关暂时不可用。正在后台重试…”, so neither MCP source tab could render.

## Viewport and normalization

- System reference: `1395 x 367` pixels.
- Project reference: `1315 x 312` pixels.
- Browser viewport and blocker screenshot: `1280 x 720` CSS pixels, `1280 x 720` image pixels, `devicePixelRatio: 1`.
- Density normalization was not applicable because no implementation frame was available for comparison.

## Comparison evidence

- Full-view comparison: blocked; the implementation route did not render past the Gateway availability state.
- Focused row comparison: blocked for the same reason. A focused crop would not contain the implemented MCP row and therefore would not be valid evidence.
- Fonts and typography, spacing and layout rhythm, colors and tokens, icon quality, and copy/content could not be visually compared. Static rendering tests do confirm that the status summary, date, and arrow markup are absent and that both sources expose an accessible switch, but code/test output is not treated as visual evidence.

## Primary interactions and console state

- Attempted to open `/workspace` through the in-app browser and reload after the background retry.
- The page remained in the Gateway-unavailable state.
- The development overlay reported `AbortError: This operation was aborted` from `src/core/auth/server.ts` while fetching Gateway authentication state.
- Project MCP activate/suspend behavior was verified by focused component and hook tests, but the browser interaction itself could not be exercised.

## Findings and comparison history

- No visual mismatch is claimed because the implementation screen was unavailable.
- Blocking condition: Gateway availability prevents capture of the system-provided and project-owned MCP list states.
- Iteration 1: opened and reloaded the authenticated workspace; no MCP content rendered, so no valid source-versus-implementation comparison or post-fix visual pass could be produced.

final result: blocked

---

# MCP 详情头部状态布局 Design QA

## Scope and evidence

- Source visual truth: `/var/folders/fd/s9_xw3qn0gdfb1ymjmg_md_c0000gn/T/codex-clipboard-f183c4f0-3f5e-409d-b484-586e8e9d3367.png`.
- Browser-rendered implementation: `/Users/jiangfeng/.codex/visualizations/2026/08/03/019fc597-932b-7d32-a5a5-a225e82c9ffe/mcp-detail-header-status-final.jpg`.
- Side-by-side comparison: `/Users/jiangfeng/.codex/visualizations/2026/08/03/019fc597-932b-7d32-a5a5-a225e82c9ffe/mcp-detail-header-status-comparison.jpg` (left: annotated source; right: implementation).
- State: authenticated project Admin, light theme, active and published project-owned MCP detail.

## Viewport and normalization

- Source pixels: `903 x 334`.
- Browser CSS viewport and full screenshot: `1280 x 720`, `devicePixelRatio: 1`.
- Implementation comparison crop: `900 x 334`, taken from the top of the `900px` MCP detail sheet. The source and implementation use the same height and near-identical width; no density normalization or aspect-ratio distortion was applied.

## Fidelity review

- Fonts and typography: retained the existing ActWeave system sans stack, title/slug hierarchy, summary labels, and compact badge weight.
- Spacing and layout rhythm: removed both pills from the project-owned MCP header, kept the title aligned to the original sheet padding, and placed the lifecycle badge directly below the two summary cards beside the edit and enable/disable actions.
- Colors and visual tokens: retained the existing neutral surface, border, primary action, success badge, and focus-ring tokens; no page-private color was introduced.
- Image quality and assets: this change contains no raster artwork or new icon asset. The implementation reuses the existing status badge and controls without CSS art or substitute imagery.
- Copy and content: “项目自建” no longer repeats inside the project-owned detail header; “启用” remains visible as the truthful lifecycle status in the lower action row. The full configured URL remains visible as `http://127.0.0.1:8771/api/mcp` below the captured region.

## Primary interactions and accessibility

- Opened the MCP list and selected “传输SOS数据查询MCP” through the accessible detail button.
- Confirmed the dialog accessibility tree orders title and slug first, then current configuration and recent update, followed by lifecycle status and the edit/disable actions.
- Confirmed “编辑配置” and “停用” remain keyboard-operable buttons and the lifecycle status is not presented as a fake control.
- Browser console error log: empty.

## Comparison history and findings

1. The annotated source showed the ownership and lifecycle pills in the header and requested removing the former while moving the latter down.
2. The implementation removes “项目自建” from the header, preserves the title/slug hierarchy, and places “启用” immediately before the lower action buttons.
3. The browser-rendered post-fix comparison shows no remaining actionable P0, P1, or P2 difference for the requested region.

- P0: none.
- P1: none.
- P2: none.
- P3: none.

Focused-region comparison is the full requested region, so a second crop was not needed.

final result: passed

---

# MCP 状态摘要三列布局 Design QA

## Scope and evidence

- Source visual truth: `/var/folders/fd/s9_xw3qn0gdfb1ymjmg_md_c0000gn/T/codex-clipboard-55a1470b-146b-434f-b797-f65608de794e.png`; the accompanying instruction places lifecycle status in this summary row.
- Browser-rendered implementation: `/Users/jiangfeng/.codex/visualizations/2026/08/03/019fc597-932b-7d32-a5a5-a225e82c9ffe/mcp-status-summary-three-column-final.jpg`.
- Side-by-side comparison: `/Users/jiangfeng/.codex/visualizations/2026/08/03/019fc597-932b-7d32-a5a5-a225e82c9ffe/mcp-status-summary-three-column-comparison.jpg` (left: source; right: implementation).
- Mobile implementation evidence: `/Users/jiangfeng/.codex/visualizations/2026/08/03/019fc597-932b-7d32-a5a5-a225e82c9ffe/mcp-status-summary-mobile.png`.
- State: authenticated project Admin, light theme, active and published project-owned MCP detail.

## Viewport and normalization

- Source pixels: `892 x 120`.
- Browser CSS viewport and full screenshot: `1280 x 720`, `devicePixelRatio: 1`.
- Implementation crop: `868 x 116`, white-padded without scaling to `892 x 120` for comparison; no aspect-ratio or density distortion was introduced.
- The three desktop cards share `y=115`, `height=84`, and approximately equal `271px` widths.
- Mobile verification uses `390 x 844`; all three cards stack at `326px` width and document width equals viewport width.

## Fidelity review

- Fonts and typography: retained the existing summary label, value, timestamp, and compact lifecycle-badge typography.
- Spacing and layout rhythm: current configuration, recent update, and lifecycle status now occupy one equal-width desktop row. The lower action row contains only edit and disable actions.
- Colors and visual tokens: retained the existing muted card surface and success badge tokens without adding a new status treatment.
- Image quality and assets: the target has no raster imagery or new icon requirement; no placeholder or substitute visual was added.
- Copy and content: “状态” and the truthful “启用” value are added beside the two source fields, while configuration and timestamp text remain unchanged.

## Primary interactions and accessibility

- Opened the existing MCP detail from its accessible list button and confirmed the dialog order is current configuration, recent update, status, then actions.
- The status badge remains informational rather than a fake switch; “编辑配置” and “停用” remain the only controls in the action row.
- At `390px`, the summary stacks without horizontal overflow and preserves the same semantic order.
- Browser console error log: empty.

## Comparison history and findings

1. The source showed current configuration and recent update in the target summary region, while the status badge was previously rendered below it beside actions.
2. The implementation adds a third equal-width “状态” card to that row and removes the duplicate badge from the action row.
3. Desktop and mobile browser evidence show no remaining actionable P0, P1, or P2 mismatch for the requested region.

- P0: none.
- P1: none.
- P2: none.
- P3: none.

The focused comparison is sufficient because the requested change is entirely contained in the summary region; the mobile screenshot provides the responsive evidence.

final result: passed

---

# 模型目录压缩与连通性探测 Design QA

## Scope and evidence

- Source visual truth: `/var/folders/fd/s9_xw3qn0gdfb1ymjmg_md_c0000gn/T/codex-clipboard-868a1be5-f2f9-4795-b398-fcaa4cac1ad8.png`.
- Browser-rendered list: `/private/tmp/model-settings-list-final.png`.
- Browser-rendered editor: `/private/tmp/model-settings-editor-final.png`.
- Side-by-side comparison: `/private/tmp/model-settings-list-comparison.png` (left: annotated source; right: implementation).
- State: authenticated system admin, light theme, two configured models.

## Viewport and normalization

- List review used the desktop `1440 x 900` viewport.
- The editor review temporarily used `1440 x 1200` only to expose the Credential-binding action and the following capability section in one frame; the override was reset before handoff.
- The comparison crops the source table and the rendered table to their shared content region, then uses a common width without aspect-ratio distortion.

## Fidelity review

- The desktop catalog now presents one primary line for configured model, Provider adapter, and Credential. Logical identifier, Provider model identifier, and injected environment-key secondary lines are removed from this desktop density.
- `更新时间` is an independent table column. Version retains only `vN` and its compact revision/order metadata.
- The wider action column keeps edit, state, and default controls horizontal at the reviewed desktop width.
- The connection action follows the Credential binding and precedes capabilities. Its description states that the current form settings and selected Credential are used for a minimal request, while neither a model nor any browser-visible secret is persisted.
- Existing design tokens, Lucide icons, typography, borders, and responsive mobile detail cards are retained; no custom visual asset was introduced.

## Primary interactions and safety

- The connection action is a real server-backed, non-graph probe. The server re-authorizes the system admin, locks only the selected current system Credential version, decrypts it only at the execution boundary, runs an untraced bounded request, and returns only `succeeded` or `failed`.
- The browser test form shows the control disabled before a required Credential is selected. A live provider request was intentionally not made during visual QA because it can consume external provider quota.
- Focused unit coverage verifies both successful and failed opaque probe outcomes, and the form placement assertion confirms that the action remains inside the Credential-binding section.

## Comparison history and findings

1. The source shows redundant lower metadata under the first three desktop columns and the timestamp nested below the version.
2. The rendered comparison confirms those lower lines are removed, the timestamp occupies its own column, and action controls remain aligned in one row.
3. The editor evidence confirms the added connection action is visible in the intended configuration flow without shifting Base URL or Credential placement.

- P0: none.
- P1: none.
- P2: none.
- P3: the desktop table intentionally retains compact version/revision metadata because it remains useful operational information and was not highlighted for removal.

final result: passed
