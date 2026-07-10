# Skills Markdown Preview

## Status

The visual direction was approved on 2026-07-10. The selected design is a wide,
read-only right-side sheet opened from the existing `/workspace/skills` list.
The generated high-fidelity mockup in the design conversation is the visual
reference.

## Goal

Let an authenticated DeerFlow user click any visible public, custom, or legacy
skill and read that skill's `SKILL.md` as rendered Markdown without leaving the
skills list.

Success means:

- The list, public/custom filter, scroll position, create action, and enable
  switches retain their current behavior.
- Clicking a skill's content opens a wide details sheet while clicking its
  enable switch never opens the sheet.
- The sheet renders the visible skill's `SKILL.md` with the existing safe
  Markdown stack and never executes raw HTML.
- Public, current-user custom, and visible legacy skills use one read-only
  detail contract that preserves user isolation.
- Loading, missing, forbidden, failed, long-content, keyboard, and narrow-screen
  states remain usable.

## Chosen Direction

The details surface is a right-side Sheet rather than an inline split view, a
centered modal, or a separate route.

- A Sheet preserves the user's list context and makes repeated inspection fast.
- A split view would compress the existing list and create unnecessary layout
  complexity below desktop widths.
- A centered modal is poorly suited to long Markdown and nested scrolling.
- A separate route provides more reading space but makes a quick inspection
  task feel heavier and loses the list's immediate visual context.

The first version is strictly read-only. It does not add editing, source/preview
tabs, copying, download, or additional skill management actions.

## Page and Interaction Design

### Skill rows

The existing card layout remains the list primitive. The name and description
area becomes a semantic button that occupies the row's available content width.
It receives a subtle hover state, visible keyboard focus, and a small disclosure
chevron. The enable switch remains a separate sibling interaction target.

Opening a skill gives its row a restrained selected treatment. Closing the Sheet
restores focus to the row trigger and keeps the active public/custom tab and list
scroll position unchanged.

### Sheet frame

The Sheet is flush with the right edge and uses the existing warm-neutral
background, semantic border tokens, and restrained elevation. It is full width
on narrow screens and `min(92vw, 800px)` on larger screens. It has a fixed header
and independently scrolling body.

The header contains:

- Skill name as the title.
- `SKILL.md` as the file label.
- Localized category and enabled/disabled status badges.
- The existing parsed description and license metadata.
- The standard Sheet close control.

The header does not duplicate the enable switch. Enablement remains owned by the
list.

### Markdown body

The body uses a centered reading column capped at approximately `72ch`. It
supports the capabilities already provided by DeerFlow's safe Streamdown stack,
including headings, paragraphs, lists, tables, code blocks, links, and math.

`SKILL.md` YAML frontmatter is not rendered as a raw code block. A small pure
helper removes only a valid leading frontmatter block; the header displays the
already parsed name, description, category, license, and enable state from the
detail response. If the content has no valid leading frontmatter block, the
complete content is rendered.

Raw HTML is disabled because custom skill content is user-authored and must be
treated as untrusted. Supporting files, relative images, and browsing files
under `references/`, `scripts/`, or `assets/` are outside this version's API and
may not resolve in the preview.

## API and Security Contract

`GET /api/skills` remains a metadata-only list response. The existing
`GET /api/skills/{skill_name}` route returns a new `SkillDetailResponse` that
extends `SkillResponse` with `content: str`.

The detail route is available to authenticated users and resolves only skills
returned by the current user's `UserScopedSkillStorage`:

- Public skills come from the configured global public root.
- Custom skills come only from the current user's custom root.
- Legacy global custom skills are readable only when they are visible through
  the existing legacy fallback rules.
- Guessing another user's skill name returns the same 404 as any unknown name.

The server never builds a filesystem path from `skill_name`. It finds the
visible `Skill` object first, then reads that object's `skill_file` after all of
the following checks:

1. The registered filename is exactly `SKILL.md`.
2. `storage.validate_skill_file_path()` accepts the resolved path.
3. The resolved path is still named `SKILL.md` and is a regular file.
4. The file is read as UTF-8.

Skill discovery, path validation, file checks, and reading run inside one
`asyncio.to_thread` call so synchronous filesystem work does not block the ASGI
event loop.

HTTP behavior is intentionally generic and does not leak host paths or content:

- Unknown or disappeared visible skill file: 404.
- Registered non-`SKILL.md` main file: 400.
- Path validation or symlink escape: 403.
- Permission, decoding, or unexpected read failure: 500.

Existing custom-skill editing/history endpoints and all admin requirements stay
unchanged.

## Component Boundaries

### Backend

- `app/gateway/routers/skills.py` owns the HTTP response model, visible-skill
  lookup, safe file-read helper, status mapping, and async offload.
- Harness storage continues to own user scoping and allowed-root validation; no
  new filesystem resolver is introduced in the Gateway.

### Frontend data layer

- `core/skills/type.ts` adds `SkillDetail` without changing the list `Skill`
  shape.
- `core/skills/api.ts` adds `loadSkillDetail(name)` and reuses
  `SkillRequestError`.
- `core/skills/hooks.ts` adds a detail query keyed by
  `['skills', 'detail', skillName]` and enables it only while a selected skill
  is open.
- A pure helper under `core/skills/` strips valid leading frontmatter without
  parsing or duplicating YAML metadata.

### Frontend presentation

- `SkillSettingsList` remains the owner of filtering, create navigation,
  enable mutations, selected skill, and Sheet open state.
- A focused `SkillDetailSheet` component owns header presentation, loading,
  errors, retry, responsive Sheet sizing, and safe Markdown rendering.
- Generated `components/ui/*` files are reused and are not edited.

## Data and Interaction Flow

1. The existing list query loads skill metadata.
2. The user activates a row's name/description button.
3. Selection opens the Sheet immediately and enables the detail query.
4. The Sheet shows a layout-matched skeleton while the detail request runs.
5. A successful response supplies both header metadata and raw `SKILL.md`.
6. The frontend removes a valid leading frontmatter block and renders the
   remaining content through the existing raw-HTML-disabled Markdown pipeline.
7. Closing the Sheet disables the detail query for rendering purposes, retains
   cached data under its per-skill query key, and restores focus through Radix
   Sheet behavior.

Switch mutations continue to invalidate the existing `['skills']` query family
and do not depend on the Sheet.

## Loading, Error, and Empty States

- Loading keeps the Sheet header context visible and shows skeleton lines that
  match a Markdown document rather than a lone loading label.
- A request failure keeps the Sheet open, shows a localized compact error, and
  offers a retry button.
- A 404 reports that the skill content is unavailable without exposing a path.
- Empty Markdown shows a localized empty-content message.
- Closing the Sheet during a request prevents stale data from replacing the
  next selected skill because each skill has its own query key.

## Responsive and Theme Behavior

- At desktop widths the Sheet is at most 800px and leaves recognizable list
  context behind it.
- At 390px the Sheet fills the viewport, its header metadata wraps, its reading
  column shrinks to available width, and no document-level horizontal overflow
  is introduced.
- Code blocks and wide tables scroll inside their own content region rather
  than expanding the Sheet or page.
- All colors, borders, typography, radii, and focus rings use existing semantic
  tokens and remain legible in light and dark themes.

## Accessibility

- Each skill preview trigger is a native button with a localized accessible
  name.
- The enable switch is not nested inside the preview button.
- Radix Sheet owns dialog semantics, focus trapping, Escape behavior, close
  control labeling, and focus restoration.
- Loading status and request errors are exposed as text; retry is keyboard
  reachable.
- Decorative disclosure icons are hidden from assistive technology.
- Markdown heading order is preserved from the source content.

## Documentation

The implementation updates:

- `README.md` and `README_zh.md` to mention read-only `SKILL.md` preview in the
  Web UI skills workspace.
- `backend/AGENTS.md` with the detail response and safe user-scoped read path.
- `frontend/AGENTS.md` with the Sheet ownership, detail query, and safe Markdown
  rendering boundary.

## Testing and Acceptance

Implementation follows test-driven development.

Backend coverage includes:

- Public, current-user custom, and visible legacy Markdown responses.
- A normal authenticated user can read their visible skill details.
- One user cannot read another user's custom skill by guessing its name.
- Unknown and disappeared files return 404.
- Non-`SKILL.md` registered files return 400.
- Symlink/root escape returns 403 without leaking paths or file content.
- Filesystem discovery and reading remain off the event loop.

Frontend unit coverage includes:

- Detail request URL, success response, and error mapping.
- Detail query key and enablement contract where practical.
- Frontmatter removal and non-frontmatter fallback.

Frontend end-to-end coverage includes:

- Clicking a row opens the Sheet and renders headings, lists, and code.
- Clicking the enable switch does not open the Sheet.
- Loading, failure, retry, close, and focus restoration.
- Repeated selection does not show stale content.
- A 390px viewport with long prose, code, and an unbroken token has no
  document-level horizontal overflow.
- Raw HTML/script content is not executed or inserted as an active element.

Visual acceptance compares the implemented open-Sheet state to the approved
mockup at the same desktop viewport and verifies the mobile Sheet separately.

## Non-goals

- No editing, deletion, history, rollback, source view, copying, or downloading
  from the Sheet.
- No change to skill enable/disable authorization or mutation behavior.
- No skill search, sorting, pagination, or list redesign.
- No dedicated skill detail route or shareable deep link.
- No browsing of supporting skill files or resolution of relative assets.
- No new Markdown or YAML dependency.
- No changes to agent skill loading, activation, prompt injection, SkillScan,
  or sandbox mounting.
- No change to the embedded `DeerFlowClient.get_skill()` return contract in
  this Web UI-focused increment.
