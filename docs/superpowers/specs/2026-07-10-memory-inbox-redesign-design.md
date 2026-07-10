# Memory Inbox Redesign

## Status

Visual direction approved on 2026-07-10; detailed specification pending review. This document supersedes only the memory-page portion of `2026-07-10-scheduled-tasks-memory-workbench-design.md`.

The selected direction is **Memory Inbox**: facts are the primary daily-management surface, while generated summaries remain available in a compact, expandable area. The generated high-fidelity mockup in the design conversation is the visual reference.

## Goal

Redesign `/workspace/memory` so saved facts are fast to scan, search, verify, edit, and delete without changing memory behavior or backend contracts. The page should remove the current imbalanced `36% / 64%` split and avoid large empty panels when either summaries or facts are sparse.

Success means:

- Facts occupy the main full-width content area.
- Summaries remain discoverable and readable without competing with facts.
- Import, export, add, edit, delete, source navigation, search, filtering, and clear-all retain their current behavior.
- Empty, loading, error, narrow-screen, long-content, light-theme, and dark-theme states remain usable.

## Design Read

This is a calm, fact-first knowledge-management surface for DeerFlow users. It should use the existing warm-neutral theme, shadcn primitives, Lucide icon family, moderate information density, restrained motion, and a single charcoal primary accent. It must not introduce gradients, glass effects, decorative illustration, analytics charts, or oversized typography.

## Information Architecture

The page is a single vertical work surface with five layers:

1. Compact page header.
2. Memory overview strip.
3. Search and filter toolbar.
4. Full-width fact list.
5. Expandable summary area.

This replaces the current permanent side-by-side summary and fact panels.

### Header

The left side contains the existing `Memory` title and a shorter description focused on managing DeerFlow's long-term understanding of the user.

The right side contains two actions:

- `Add fact` remains the single primary button and opens the current fact editor.
- `Manage memory` is an outlined menu trigger containing import, export, and clear-all.

The management menu must preserve the existing hidden file input, import validation and confirmation, exported JSON format and filename, pending states, success/error toasts, and clear-all confirmation. `Clear all memory` remains visually destructive and separated from import/export inside the menu.

On narrow screens the header stacks, with actions wrapping below the title without horizontal overflow.

### Overview Strip

The overview is a compact horizontal status surface, not a dashboard and not a row of independent cards. It derives all values from the existing `UserMemory` object:

- Fact count: `memory.facts.length`.
- Summary count: number of the six summary sections whose trimmed content is non-empty.
- Last updated: existing `memory.lastUpdated` formatted with `formatTimeAgo`.
- Recent focus: existing `memory.user.topOfMind.summary`, falling back to the localized empty label.

The strip includes a `View summaries` action. Activating it expands the summary area and moves focus or scroll position to the summary heading. No new analytics, persistence, or backend data is introduced.

On mobile the values wrap into a compact two-column grid, while recent focus occupies a full row.

### Search and Filters

The existing search field and `All / Facts / Summaries` single-select filter remain the only filtering controls. They continue to use `query`, `useDeferredValue`, `normalizedQuery`, `filter`, `filteredFacts`, and `filteredSectionGroups`.

Display behavior:

- `All`: show the fact list and the summary disclosure.
- `Facts`: show only the fact list.
- `Summaries`: hide the fact list and show the summary area expanded.
- A query that matches summaries while summaries are otherwise collapsed expands the summary area so a matching result is never hidden.
- A query with no visible match renders the existing localized no-results message once.

Filtering remains client-side and keeps its current case-insensitive substring semantics.

### Fact List

Facts render as one full-width bordered list with shared outer chrome and light separators between rows. Repeated card borders and the current large empty fact panel are removed.

Each row contains:

- A small category icon chosen from the existing Lucide dependency. Preference/personal categories use a heart, work uses a briefcase, project uses a folder, context uses a notebook, and unknown categories use a generic memory icon.
- The fact content as the primary text. The UI must not synthesize a title or description that is absent from the stored fact.
- A compact category label.
- Localized confidence level from the existing `confidenceToLevelKey` logic.
- Creation time from `formatTimeAgo`.
- `Manual` for manually entered facts, or the existing thread source link for learned facts.
- Existing edit and delete icon buttons with accessible names.

Long fact content and arbitrary category strings wrap without increasing document width. On mobile, metadata wraps beneath the fact and row actions move to a separate trailing group.

The add/edit dialog, validation rules, default category, default confidence, mutation payloads, pending states, and toasts do not change. Delete keeps its current confirmation and preview.

### Summary Disclosure

Summaries move into a full-width disclosure below the facts in `All` mode and become the main content in `Summaries` mode.

The disclosure header shows the localized summary label, non-empty summary count, read-only status, and expanded/collapsed affordance. It uses a native button with `aria-expanded` and an associated content region.

Expanded content uses the existing `buildMemorySectionGroups` data shape. User context and history remain separate groups. Each of the six sections displays:

- Localized section title.
- Existing summary content rendered through `SafeStreamdown` so stored Markdown remains supported.
- Existing updated time when present.

The summary content remains read-only. The redesign does not add summary editing, regeneration, deletion, or reordering.

### Empty, Loading, and Error States

- Loading uses layout-matched skeleton blocks for the header body, overview, toolbar, and list rows instead of a lone text label.
- A load error remains contextual inside the work surface and displays the existing error message. No retry contract is added.
- When memory exists but contains no facts or summary content, show one compact empty state with an `Add fact` action. Do not render empty bordered panels below it.
- When facts are empty but summaries exist, keep the summary disclosure available and show a compact fact-empty message in `All` or `Facts` mode.
- When summaries are empty, the disclosure remains available only where filter semantics require it and shows the existing localized empty content.
- Search no-results uses the existing localized message and does not duplicate empty-state messages.

## Component Boundaries

`MemorySettingsPage` remains the state and mutation owner. It continues to own:

- `useMemory` and all memory mutation hooks.
- Search, deferred search, filter, editor, delete, import, export, and clear-all state.
- Import schema validation and export file creation.
- Derived filtered facts and summary groups.

Presentation may be split into focused components under the existing workspace settings area:

- `MemoryHeaderActions`
- `MemoryOverview`
- `MemoryToolbar`
- `MemoryFactList`
- `MemorySummaryDisclosure`
- `MemoryEmptyState`

These components receive values and callbacks through props. They must not call memory APIs, own query-cache updates, duplicate mutation flows, or change payloads. Pure formatting and category-icon helpers may be extracted for unit testing.

No backend, API route, `core/memory` hook, query key, cache behavior, or `UserMemory` type changes are required.

## Data and Interaction Flow

1. `useMemory` loads the current `UserMemory` exactly as today.
2. The page derives overview values, section groups, filtered summaries, and filtered facts without mutating the response.
3. Search and filter state determine which presentation regions are visible.
4. Create, edit, delete, import, and clear actions call the existing mutation hooks.
5. Existing mutation success handlers replace the `['memory']` query cache, and all derived presentation updates from the new value.
6. Export continues to call `exportMemory` directly and download the returned JSON.

The summary expanded state is presentation-only. It resets only when the page remounts, except that the `Summaries` filter and summary search matches force the region open.

## Responsive and Theme Behavior

- Desktop content stays within the existing wide capability-page container.
- The fact list remains full width at every breakpoint.
- Header actions, overview metadata, toolbar filters, fact metadata, and row actions explicitly wrap below `768px`.
- The page must have no horizontal overflow at `390px`, including with 512-character unbroken fact and summary fixtures.
- Existing semantic theme tokens provide light and dark variants; no raw light-only colors are introduced.
- Hover and active feedback remain subtle. No automatic animation is required.
- `prefers-reduced-motion` is naturally respected because the redesign adds no nonessential motion.

## Accessibility

- All icon-only actions retain localized `aria-label` values.
- Decorative icons are hidden from assistive technology.
- The management menu uses the existing Radix/shadcn keyboard and focus behavior.
- The summary disclosure exposes expanded state and controls a labeled content region.
- The segmented filter retains its single-selection semantics.
- Destructive actions remain labeled, visually distinct, and protected by confirmation dialogs.
- Focus rings and text contrast use existing design-system tokens.

## Testing and Acceptance

Implementation follows test-driven development. Update or add tests before changing presentation code.

Required E2E coverage:

- The loaded page shows `Add fact` as primary and `Manage memory` as secondary.
- Opening `Manage memory` exposes import, export, and destructive clear-all entries.
- `All`, `Facts`, and `Summaries` preserve their content visibility semantics.
- `View summaries` and the summary disclosure expand/collapse correctly.
- A summary-only search match becomes visible instead of remaining hidden.
- The fact list uses the full workbench width; the obsolete `36% / 64%` assertion is removed.
- Create, edit, delete, clear, import, export, and learned-fact source navigation remain reachable and use their existing flows.
- The fully empty state has one primary recovery action and no large empty panels.
- A `390 x 844` viewport with long unbroken summary, category, and fact strings has no horizontal overflow.
- Persisted dark mode remains legible and overflow-free.

Verification commands:

- Run the memory E2E spec.
- Run related frontend unit tests.
- Run `pnpm check`.
- Run `pnpm format`.

Manual acceptance checks cover desktop and mobile widths, light and dark themes, populated memory, fully empty memory, facts-only memory, summaries-only memory, pending mutations, and all confirmation dialogs.

## Non-goals

- No backend or API changes.
- No changes to memory generation or summary algorithms.
- No summary editing, regeneration, deletion, or reordering.
- No new sorting, pagination, bulk actions, category management, or analytics.
- No changes to import/export schemas or filenames.
- No changes to search matching semantics.
- No redesign of scheduled tasks, tools, skills, chat, or other workspace pages.
- No new third-party dependency.
