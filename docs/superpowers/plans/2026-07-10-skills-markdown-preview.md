# Skills Markdown Preview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a safe, admin-only, read-only `SKILL.md` preview Sheet to `/workspace/skills` for public and current-user custom skills shown by the existing tabs.

**Architecture:** Keep the skills list and existing per-skill metadata contracts unchanged, and add a guarded `/api/skills/content/{name}` endpoint resolved through `UserScopedSkillStorage`. The frontend fetches content lazily per selected skill, removes a backend-parser-compatible leading frontmatter fence, and renders the body through DeerFlow's existing raw-HTML-disabled Markdown component inside a responsive Radix Sheet.

**Tech Stack:** FastAPI, Pydantic, DeerFlow skill storage, pytest, Blockbuster, Next.js 16, React 19, TypeScript 5.8, TanStack Query 5, Tailwind CSS 4, Radix/shadcn Sheet, Streamdown, Rstest, Playwright.

## Global Constraints

- The selected visual target is `docs/pr-evidence/skills-markdown-preview/source-mockup.png`.
- The feature is read-only: no editing, source tab, copy, download, deletion, history, or rollback UI.
- `GET /api/skills` and `GET /api/skills/{skill_name}` remain metadata-only and backward compatible.
- New `GET /api/skills/content/{skill_name}` is admin-only and returns `SkillContentResponse { content: str }` for a skill visible in the current user's storage scope.
- The server must never construct a filesystem path from `skill_name`.
- Discovery, path validation, file checks, and file reading run in one `asyncio.to_thread` call.
- Raw HTML is disabled in Markdown rendering and no new Markdown/YAML dependency is added.
- The enable Switch remains a separate interaction and keeps its existing authorization and mutation behavior.
- Non-admin rows remain passive and never expose or request raw skill content.
- The Sheet is full-width at 390px and capped at 800px on desktop.
- Production code must be written only after its corresponding test has failed for the expected missing behavior.
- Existing user work and the pre-existing `2f9743ae` commit must not be rewritten.

---

### Task 1: Guarded user-scoped skill content API

**Files:**
- Modify: `backend/app/gateway/routers/skills.py`
- Modify: `backend/tests/test_skills_custom_router.py`
- Modify: `backend/tests/test_skills_router_authz.py`
- Create: `backend/tests/blocking_io/test_skills_router.py`

**Interfaces:**
- Produces: `SkillContentResponse` with `content: str`.
- Produces: admin-only `GET /api/skills/content/{skill_name} -> SkillContentResponse`.
- Consumes: `SkillStorage.load_skills(enabled_only=False)`, `Skill.skill_file`, `SkillStorage.validate_skill_file_path(path)`.
- Preserves: existing list/detail/client metadata contracts and every custom edit/history/admin endpoint.

- [ ] **Step 1: Write failing authorization, content, isolation, and blocking-I/O tests**

Add focused tests using the file's existing `_skill_content()` helper and the
same `Paths`/`UserScopedSkillStorage` setup already used by its install and
multi-user tests. The tests must create actual files and call the HTTP route:

```python
def test_get_skill_content_returns_public_markdown_for_admin(
    monkeypatch,
    tmp_path: Path,
) -> None:
    content = _skill_content("paper-review", "Review papers") + "\n# Workflow\n"
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "public" / "paper-review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")

    from deerflow.config.paths import Paths

    monkeypatch.setattr("deerflow.config.paths.get_paths", lambda: Paths(base_dir=tmp_path))
    monkeypatch.setattr("deerflow.config.paths._paths", None)
    storage = UserScopedSkillStorage("default", host_path=str(skills_root))
    config = SimpleNamespace(
        skills=SimpleNamespace(
            get_skills_path=lambda: skills_root,
            container_path="/mnt/skills",
            use="deerflow.skills.storage.local_skill_storage:LocalSkillStorage",
        )
    )
    monkeypatch.setattr(skills_router, "_get_user_skill_storage", lambda _config: storage)

    app = _make_test_app(config)
    with TestClient(app) as client:
        response = client.get("/api/skills/content/paper-review")

    assert response.status_code == 200
    assert response.json() == {"content": content}
```

Add an equivalent assertion for a current-user custom skill. Do not change the
existing public/custom tab taxonomy or add a legacy UI promise. Extend the
existing `test_bob_cannot_read_alice_skill_via_get_api` method while its Bob
client/storage is active:

```python
bob_content_response = client.get(
    "/api/skills/content/alice-secret-skill",
)
assert bob_content_response.status_code == 404
assert "# alice-secret-skill" not in bob_content_response.text
assert str(tmp_path) not in bob_content_response.text
```

Add failure-contract tests with a fake storage that returns a registered `Skill`:

```python
def test_get_skill_content_rejects_non_skill_md_path(monkeypatch, tmp_path):
    readme = tmp_path / "public" / "demo" / "README.md"
    readme.parent.mkdir(parents=True)
    readme.write_text("DO_NOT_LEAK", encoding="utf-8")
    skill = Skill(
        name="demo",
        description="Demo",
        license=None,
        skill_dir=readme.parent,
        skill_file=readme,
        relative_path=Path("demo"),
        category="public",
        enabled=True,
    )
    storage = SimpleNamespace(
        load_skills=lambda *, enabled_only: [skill],
        validate_skill_file_path=lambda path: path.resolve(),
    )
    monkeypatch.setattr(skills_router, "_get_user_skill_storage", lambda _config: storage)

    app = _make_test_app(SimpleNamespace())
    with TestClient(app) as client:
        response = client.get("/api/skills/content/demo")

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid skill content file"}
    assert "DO_NOT_LEAK" not in response.text
```

Cover unknown registry name (404), file removed after discovery (404), path
validation/symlink escape (403), and an `OSError`/decode failure (500). Assert
that no response contains an absolute host path or sentinel file content.

Add `("get", "/api/skills/content/demo", None)` to `_GUARDED_ENDPOINTS` in
`test_skills_router_authz.py`. Its normal-user loop must prove the 403 happens
before any storage lookup. Keep the existing assertion that normal users can
still call list and metadata detail endpoints.

Create `backend/tests/blocking_io/test_skills_router.py` before production code.
Follow `test_skills_load.py`: seed a real storage under `tmp_path` through
`await asyncio.to_thread`, patch `_get_user_skill_storage`, satisfy the admin
dependency, call the new route, and assert `response.content`. This test must
first fail because the content handler is absent. After implementation, the
same test becomes the runtime anchor for the offload.

- [ ] **Step 2: Run all new backend tests and verify RED**

Run:

```bash
cd backend && uv run pytest tests/test_skills_custom_router.py -k "get_skill_content or bob_cannot_read_alice_skill" -q
cd backend && uv run pytest tests/test_skills_router_authz.py -q
cd backend && uv run pytest tests/blocking_io/test_skills_router.py -q
```

Expected: FAIL because the content route/model do not exist. The blocking test
may fail on the missing handler during this first RED, but not from invalid
fixture setup.

- [ ] **Step 3: Add the content model and synchronous safe-read helper**

In `backend/app/gateway/routers/skills.py`, add:

```python
class SkillContentResponse(BaseModel):
    content: str = Field(..., description="Raw SKILL.md content")


class _VisibleSkillNotFoundError(Exception):
    pass


class _SkillContentNotFoundError(Exception):
    pass


class _InvalidSkillContentFileError(Exception):
    pass


class _SkillContentPathNotAllowedError(Exception):
    pass


def _read_visible_skill_content(
    storage: SkillStorage,
    skill_name: str,
) -> str:
    skills = storage.load_skills(enabled_only=False)
    skill = next((candidate for candidate in skills if candidate.name == skill_name), None)
    if skill is None:
        raise _VisibleSkillNotFoundError
    if skill.skill_file.name != SKILL_MD_FILE:
        raise _InvalidSkillContentFileError
    try:
        skill_file = storage.validate_skill_file_path(skill.skill_file)
    except ValueError as exc:
        raise _SkillContentPathNotAllowedError from exc
    if skill_file.name != SKILL_MD_FILE:
        raise _InvalidSkillContentFileError
    if not skill_file.is_file():
        raise _SkillContentNotFoundError
    return skill_file.read_text(encoding="utf-8")
```

Do not include exception text in any client-facing response.

- [ ] **Step 4: Add the guarded route, offload the read path, and map generic failures**

Add this route without changing the existing `get_skill()` metadata route:

```python
@router.get(
    "/skills/content/{skill_name}",
    response_model=SkillContentResponse,
    summary="Get Skill Content",
    description="Retrieve read-only SKILL.md content for a visible skill.",
)
async def get_skill_content(
    skill_name: str,
    request: Request,
    config: AppConfig = Depends(get_config),
) -> SkillContentResponse:
    await require_admin_user(request, detail=_ADMIN_REQUIRED_DETAIL)
    normalized_name = skill_name.replace("\r\n", "").replace("\n", "")
    storage = _get_user_skill_storage(config)
    try:
        content = await asyncio.to_thread(
            _read_visible_skill_content,
            storage,
            normalized_name,
        )
        return SkillContentResponse(content=content)
    except _VisibleSkillNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Skill '{normalized_name}' not found",
        ) from exc
    except _SkillContentNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Skill '{normalized_name}' content not found",
        ) from exc
    except _InvalidSkillContentFileError as exc:
        raise HTTPException(status_code=400, detail="Invalid skill content file") from exc
    except _SkillContentPathNotAllowedError as exc:
        raise HTTPException(status_code=403, detail="Skill content path is not allowed") from exc
    except (OSError, UnicodeError) as exc:
        logger.error("Failed to read skill content for %s", normalized_name, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to read skill content") from exc
    except Exception as exc:
        logger.error("Failed to get skill content for %s", normalized_name, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get skill content") from exc
```

Use `Skill '<name>' not found` only for a registry miss and
`Skill '<name>' content not found` only when a visible skill's main file has
disappeared or is no longer a regular file.

- [ ] **Step 5: Verify GREEN for API behavior**

Run:

```bash
cd backend && uv run pytest tests/test_skills_custom_router.py tests/test_skills_router_authz.py -q
```

Expected: PASS, including admin public/custom reads, normal-user 403, user-scope
isolation, and all existing metadata and mutation authorization tests.

- [ ] **Step 6: Verify the prewritten blocking-I/O regression is GREEN**

Run:

```bash
cd backend && uv run pytest tests/blocking_io/test_skills_router.py -q
```

Expected: PASS. Removing the production `asyncio.to_thread` must make the same
prewritten test fail with `BlockingError`; perform that temporary mutation check
before restoring the offloaded implementation and recording GREEN evidence.

- [ ] **Step 7: Format, lint, and commit the backend slice**

Run:

```bash
cd backend && uv run ruff format app/gateway/routers/skills.py tests/test_skills_custom_router.py tests/test_skills_router_authz.py tests/blocking_io/test_skills_router.py
cd backend && uv run ruff check app/gateway/routers/skills.py tests/test_skills_custom_router.py tests/test_skills_router_authz.py tests/blocking_io/test_skills_router.py
```

Expected: both commands exit 0.

Commit:

```bash
git add backend/app/gateway/routers/skills.py backend/tests/test_skills_custom_router.py backend/tests/test_skills_router_authz.py backend/tests/blocking_io/test_skills_router.py
git commit -m "feat(skills): expose guarded markdown content"
```

---

### Task 2: Frontend content client and frontmatter boundary

**Files:**
- Modify: `frontend/src/core/skills/type.ts`
- Modify: `frontend/src/core/skills/api.ts`
- Modify: `frontend/src/core/skills/index.ts`
- Create: `frontend/src/core/skills/markdown.ts`
- Create: `frontend/tests/unit/core/skills/api.test.ts`
- Create: `frontend/tests/unit/core/skills/markdown.test.ts`

**Interfaces:**
- Produces: `SkillContentResponse { content: string }`.
- Produces: `loadSkillContent(skillName: string): Promise<SkillContentResponse>`.
- Produces: `splitSkillMarkdown(source): { frontmatter: string | null; body: string }`.

- [ ] **Step 1: Write failing API client tests**

Create `frontend/tests/unit/core/skills/api.test.ts` using the existing mocked fetcher pattern:

```ts
import { beforeEach, describe, expect, test, rs } from "@rstest/core";

rs.mock("@/core/api/fetcher", () => ({ fetch: rs.fn() }));
rs.mock("@/core/config", () => ({ getBackendBaseURL: () => "" }));

import { fetch as fetcher } from "@/core/api/fetcher";
import { loadSkillContent, SkillRequestError } from "@/core/skills/api";

const mockedFetch = rs.mocked(fetcher);

beforeEach(() => mockedFetch.mockReset());

test("loads encoded skill content", async () => {
  const content = { content: "---\nname: paper review\n---\n# Workflow" };
  mockedFetch.mockResolvedValueOnce(
    new Response(JSON.stringify(content), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );

  // The value is deliberately outside the validated skill-name grammar so
  // this pure URL-boundary test proves the client encodes path input.
  await expect(loadSkillContent("paper review")).resolves.toEqual(content);
  expect(mockedFetch).toHaveBeenCalledWith("/api/skills/content/paper%20review");
});

test("maps detail failures to SkillRequestError", async () => {
  mockedFetch.mockResolvedValueOnce(
    new Response(JSON.stringify({ detail: "Skill content unavailable" }), {
      status: 404,
      headers: { "Content-Type": "application/json" },
    }),
  );

  await expect(loadSkillContent("missing")).rejects.toMatchObject({
    name: "SkillRequestError",
    status: 404,
    message: "Skill content unavailable",
  });
  await expect(Promise.reject(new SkillRequestError(500, "x"))).rejects.toBeInstanceOf(
    SkillRequestError,
  );
});
```

- [ ] **Step 2: Write failing frontmatter tests**

Create `frontend/tests/unit/core/skills/markdown.test.ts`:

```ts
import { describe, expect, test } from "@rstest/core";

import { splitSkillMarkdown } from "@/core/skills/markdown";

describe("splitSkillMarkdown", () => {
  test.each([
    ["---\nname: demo\n---\n# Body\n", "name: demo", "# Body\n"],
    ["---\r\nname: demo\r\n---\r\n# Body\r\n", "name: demo", "# Body\r\n"],
    ["\uFEFF---\nname: demo\n---\n# Body", "name: demo", "# Body"],
    ["---  \nname: demo\n---\t\n# Body", "name: demo", "# Body"],
  ])("splits a parser-compatible leading frontmatter fence", (source, frontmatter, body) => {
    expect(splitSkillMarkdown(source)).toEqual({ frontmatter, body });
  });

  test.each([
    "# Body\n---\nrest",
    "---\nname: demo\n# no closing fence",
    "plain text",
  ])("leaves non-frontmatter content intact", (source) => {
    expect(splitSkillMarkdown(source)).toEqual({ frontmatter: null, body: source });
  });
});
```

- [ ] **Step 3: Run both unit tests and verify RED**

Run:

```bash
cd frontend && pnpm test tests/unit/core/skills/api.test.ts tests/unit/core/skills/markdown.test.ts
```

Expected: FAIL because the content client and Markdown splitter do not exist.

- [ ] **Step 4: Implement the minimal types, client, and splitter**

Add to `type.ts`:

```ts
export interface Skill {
  name: string;
  description: string;
  category: "public" | "custom" | "legacy";
  license: string | null;
  enabled: boolean;
  editable: boolean;
}

export interface SkillContentResponse {
  content: string;
}
```

Add to `api.ts`:

```ts
export async function loadSkillContent(
  skillName: string,
): Promise<SkillContentResponse> {
  const response = await fetch(
    `${getBackendBaseURL()}/api/skills/content/${encodeURIComponent(skillName)}`,
  );
  if (!response.ok) {
    throw new SkillRequestError(response.status, await readErrorDetail(response));
  }
  return (await response.json()) as SkillContentResponse;
}
```

Implement `markdown.ts` without YAML parsing:

```ts
export interface SkillMarkdownParts {
  frontmatter: string | null;
  body: string;
}

export function splitSkillMarkdown(source: string): SkillMarkdownParts {
  const withoutBom = source.startsWith("\uFEFF") ? source.slice(1) : source;
  const match = /^---[^\S\r\n]*\r?\n([\s\S]*?)\r?\n---[^\S\r\n]*\r?\n/.exec(
    withoutBom,
  );
  if (!match) {
    return { frontmatter: null, body: source };
  }
  return {
    frontmatter: match[1] ?? "",
    body: withoutBom.slice(match[0].length),
  };
}
```

Export the helper and content type through `core/skills/index.ts`.

- [ ] **Step 5: Verify GREEN and commit the frontend data slice**

Run:

```bash
cd frontend && pnpm test tests/unit/core/skills/api.test.ts tests/unit/core/skills/markdown.test.ts
```

Expected: PASS.

Commit:

```bash
git add frontend/src/core/skills frontend/tests/unit/core/skills/api.test.ts frontend/tests/unit/core/skills/markdown.test.ts
git commit -m "feat(frontend): load skill markdown content"
```

---

### Task 3: Responsive, accessible Markdown detail Sheet

**Files:**
- Create: `frontend/src/components/workspace/settings/skill-detail-sheet.tsx`
- Modify: `frontend/src/components/workspace/settings/skill-settings-page.tsx`
- Modify: `frontend/src/core/skills/hooks.ts`
- Modify: `frontend/src/core/i18n/locales/en-US.ts`
- Modify: `frontend/src/core/i18n/locales/zh-CN.ts`
- Modify: `frontend/src/core/i18n/locales/types.ts`
- Modify: `frontend/tests/e2e/utils/mock-api.ts`
- Create: `frontend/tests/e2e/skills.spec.ts`

**Interfaces:**
- Produces: `useSkillContent(skillName, enabled)` with query key `['skills', 'content', skillName]`.
- Produces: `SkillDetailSheet({ skill, open, onOpenChange, openerRef })`.
- Consumes: `loadSkillContent`, `splitSkillMarkdown`, `MarkdownContent`, `Sheet`, `Badge`, `Skeleton`.
- Preserves: public/custom Tabs, create navigation, enable mutation, admin/static-site Switch disablement.

- [ ] **Step 1: Add a lazy content mock and all failing Sheet E2E tests**

Extend `MockAPIOptions` without placing content in list items:

```ts
export type MockAPIOptions = {
  skillContents?: Record<string, string>;
};
```

Register the detail route before the exact list route or branch by pathname:

```ts
void page.route("**/api/skills/**", (route) => {
  const segments = new URL(route.request().url()).pathname.split("/").filter(Boolean);
  const isContentRequest = segments.at(-2) === "content";
  const encodedName = segments.at(-1);
  const name = decodeURIComponent(encodedName ?? "");
  const skill = skills.find((candidate) => candidate.name === name);
  if (route.request().method() === "PUT" && skill) {
    const body = route.request().postDataJSON() as { enabled: boolean };
    skill.enabled = body.enabled;
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ...skill, editable: skill.category === "custom" }),
    });
  }
  if (route.request().method() !== "GET" || !isContentRequest) {
    return route.fallback();
  }
  const content = options?.skillContents?.[name];
  if (!skill || content === undefined) {
    return route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Skill content unavailable" }),
    });
  }
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ content }),
  });
});
```

Create `skills.spec.ts`:

```ts
import { expect, test } from "@playwright/test";

import { mockLangGraphAPI } from "./utils/mock-api";

const CONTENT = `---
name: paper-review
description: Review papers
license: MIT
---
# Workflow

## When to use

- Review a paper
- Analyze methodology

\`\`\`text
structured output
\`\`\`
`;

test("opens a read-only skill Markdown sheet and restores focus", async ({ page }) => {
  mockLangGraphAPI(page, {
    skills: [{
      name: "paper-review",
      description: "Review papers",
      category: "public",
      license: "MIT",
      enabled: true,
    }],
    skillContents: { "paper-review": CONTENT },
  });
  await page.goto("/workspace/skills");

  const trigger = page.getByRole("button", { name: "View paper-review SKILL.md" });
  await trigger.click();

  const sheet = page.getByRole("dialog", { name: "paper-review" });
  await expect(sheet).toBeVisible();
  await expect(sheet.getByRole("heading", { name: "Workflow" })).toBeVisible();
  await expect(sheet.getByText("name: paper-review", { exact: true })).toHaveCount(0);
  await expect(trigger).toHaveAttribute("aria-expanded", "true");

  await page.keyboard.press("Escape");
  await expect(sheet).toHaveCount(0);
  await expect(trigger).toBeFocused();
  await expect(trigger).toHaveAttribute("aria-expanded", "false");
});
```

Add another failing assertion that clicking the Switch reaches the mocked PUT
handler but never sends a content GET and never creates a dialog.

Before any Sheet, hook, or row-trigger production code, also add route-controlled
tests for every remaining behavior:

- No content GET occurs before an admin opens a skill.
- A held content request displays a skeleton while the list remains visible.
- 403 shows the localized admin-required message, 404 shows unavailable, and
  500 shows a generic error; Retry succeeds after the failure route is released.
- Opening skill B never renders skill A's body while B loads.
- `<script>window.__skillPreviewExecuted = true</script>` never executes and no
  active script node is inserted in the Sheet.
- Closing through Escape and the close control restores focus to the exact row
  trigger.
- At `390 x 844`, the dialog starts at `x <= 1`, is at least 388px wide, and
  neither `document.documentElement` nor the dialog overflows horizontally with
  a 320-character token and wide code block.
- With a non-admin mocked auth user, rows expose no preview button and no content
  request is sent.

- [ ] **Step 2: Run the E2E test and verify RED**

Run:

```bash
cd frontend && pnpm test:e2e tests/e2e/skills.spec.ts
```

Expected: FAIL because the content hook, admin preview trigger, Sheet, and state
handling do not exist. Each test must fail for that missing behavior rather than
for an invalid mock or selector.

- [ ] **Step 3: Add localized preview copy**

Extend `settings.skills` in `types.ts`, `en-US.ts`, and `zh-CN.ts` with the same keys:

```ts
viewSkill: (name: string) => string;
toggleSkill: (name: string) => string;
fileLabel: string;
renderedDescription: string;
enabled: string;
disabled: string;
categories: { public: string; custom: string; legacy: string };
adminRequiredPreview: string;
contentUnavailable: string;
loadError: string;
emptyContent: string;
licenseLabel: string;
loading: string;
retry: string;
```

English values include `View ${name} SKILL.md`, `Enable or disable ${name}`,
`SKILL.md`, `Rendered contents of SKILL.md`, `Enabled`, `Disabled`, `Public`,
`Custom`, `Legacy`, `Admin privileges are required to preview skill content.`,
`Skill content is unavailable.`, `Unable to load skill content.`,
`This SKILL.md is empty.`, `License`, `Loading skill content`, and `Retry`. Add
equivalent natural Chinese copy.

- [ ] **Step 4: Implement the content hook and focused Sheet component**

Add the hook only now, after the E2E suite has failed against its absence:

```ts
export function useSkillContent(skillName: string | null, enabled: boolean) {
  return useQuery({
    queryKey: ["skills", "content", skillName],
    queryFn: () => loadSkillContent(skillName!),
    enabled: enabled && skillName !== null,
    retry: (count, err) => !(err instanceof SkillRequestError) && count < 3,
  });
}
```

Create `skill-detail-sheet.tsx` with this structure:

```tsx
export function SkillDetailSheet({
  skill,
  open,
  onOpenChange,
  openerRef,
}: {
  skill: Skill | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  openerRef: React.RefObject<HTMLButtonElement | null>;
}) {
  const { t } = useI18n();
  const { data, isLoading, error, refetch } = useSkillContent(
    skill?.name ?? null,
    open,
  );
  const body = data ? splitSkillMarkdown(data.content).body : "";
  const errorMessage =
    error instanceof SkillRequestError && error.status === 403
      ? t.settings.skills.adminRequiredPreview
      : error instanceof SkillRequestError && error.status === 404
        ? t.settings.skills.contentUnavailable
        : t.settings.skills.loadError;

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        className="w-full max-w-none gap-0 p-0 sm:w-[min(92vw,800px)] sm:max-w-[800px]"
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          if (openerRef.current?.isConnected) openerRef.current.focus();
        }}
      >
        <SheetHeader className="shrink-0 border-b px-5 py-4 pr-12 sm:px-6">
          <SheetTitle className="break-words text-lg">{skill?.name}</SheetTitle>
          <SheetDescription>
            {t.settings.skills.fileLabel}
            <span className="sr-only"> · {t.settings.skills.renderedDescription}</span>
          </SheetDescription>
          <div className="flex flex-wrap items-center gap-2 pt-2">
            {skill && <Badge variant="secondary">{t.settings.skills.categories[skill.category]}</Badge>}
            {skill && <Badge variant="secondary">{skill.enabled ? t.settings.skills.enabled : t.settings.skills.disabled}</Badge>}
          </div>
          {skill?.description && (
            <p className="text-muted-foreground pt-2 text-sm leading-6">{skill.description}</p>
          )}
          {skill?.license && (
            <p className="text-muted-foreground text-sm">
              {t.settings.skills.licenseLabel}: {skill.license}
            </p>
          )}
        </SheetHeader>
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-5 py-6 sm:px-8">
          <div className="mx-auto w-full min-w-0 max-w-[72ch] [overflow-wrap:anywhere]">
            {isLoading ? (
              <SkillDetailSkeleton />
            ) : error ? (
              <div role="alert" className="space-y-3 text-sm">
                <p>{errorMessage}</p>
                <Button variant="outline" size="sm" onClick={() => void refetch()}>
                  {t.settings.skills.retry}
                </Button>
              </div>
            ) : body.trim() ? (
              <MarkdownContent content={body} isLoading={false} />
            ) : (
              <p className="text-muted-foreground text-sm">{t.settings.skills.emptyContent}</p>
            )}
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}
```

`SkillDetailSkeleton` uses `role="status"`, an `sr-only` loading label, and several `Skeleton` blocks. Reuse `MarkdownContent`; do not enable `rehypeRaw` or duplicate the Markdown pipeline.

- [ ] **Step 5: Separate the row preview button from the Switch**

In `SkillSettingsList`, store only `selectedSkillName`, `detailOpen`, and the
exact opener button ref. Derive `selectedSkill` from the latest `skills` array so
the enabled badge cannot become stale after list invalidation:

```ts
const [selectedSkillName, setSelectedSkillName] = useState<string | null>(null);
const [detailOpen, setDetailOpen] = useState(false);
const openerRef = useRef<HTMLButtonElement | null>(null);
const selectedSkill = useMemo(
  () => skills.find((skill) => skill.name === selectedSkillName) ?? null,
  [selectedSkillName, skills],
);
```

Give the selected `Item` a restrained `bg-muted/20 border-foreground/20`
treatment. For administrators, replace the passive `ItemContent` body with the
following native button; for non-admins, retain the current non-interactive name
and description markup and do not render a chevron or dialog ARIA state:

```tsx
<ItemContent>
  <button
    type="button"
    className="focus-visible:ring-ring group w-full rounded-sm text-left focus-visible:ring-2 focus-visible:outline-none"
    aria-haspopup="dialog"
    aria-expanded={detailOpen && selectedSkill?.name === skill.name}
    aria-label={t.settings.skills.viewSkill(skill.name)}
    onClick={(event) => {
      openerRef.current = event.currentTarget;
      setSelectedSkillName(skill.name);
      setDetailOpen(true);
    }}
  >
    <span className="flex items-center justify-between gap-3">
      <span className="min-w-0">
        <span className="block font-medium">{skill.name}</span>
        <span className="text-muted-foreground mt-1 line-clamp-4 block text-sm">
          {skill.description}
        </span>
      </span>
      <ChevronRightIcon aria-hidden="true" className="text-muted-foreground size-4 shrink-0" />
    </span>
  </button>
</ItemContent>
```

Add `aria-label={t.settings.skills.toggleSkill(skill.name)}` to the Switch.
Render one controlled `SkillDetailSheet` after the mapped list and pass
`openerRef`. Retain `selectedSkillName` after close so the exit animation and
explicit `onCloseAutoFocus` handler keep their context.

- [ ] **Step 6: Verify the main interaction GREEN**

Run:

```bash
cd frontend && pnpm test:e2e tests/e2e/skills.spec.ts
```

Expected: PASS for open, Markdown render, frontmatter removal, Switch isolation, Escape close, and focus restoration.

- [ ] **Step 7: Drive all prewritten state, safety, and mobile tests to GREEN**

Run the tests already written in Step 1 and make only the minimal production
corrections needed for:

- No content request occurs before opening, and non-admin rows remain passive.
- A held request displays the skeleton while the list remains visible.
- 403, 404, and 500 render their distinct localized messages while keeping the
  Sheet open; clicking Retry succeeds.
- Opening skill B never renders skill A's body while B loads.
- `<script>window.__skillPreviewExecuted = true</script>` never executes and no active script element is inserted in the Sheet.
- At `390 x 844`, the dialog starts at `x <= 1`, is at least 388px wide, and neither `document.documentElement` nor the dialog has horizontal overflow with a 320-character unbroken token and a wide code block.

Expected: every Step 1 test now passes; no new production behavior is added
without first extending the E2E test and observing the expected failure.

- [ ] **Step 8: Run frontend checks and commit the Sheet slice**

Run:

```bash
cd frontend && pnpm test tests/unit/core/skills/api.test.ts tests/unit/core/skills/markdown.test.ts
cd frontend && pnpm test:e2e tests/e2e/skills.spec.ts
cd frontend && pnpm check
cd frontend && pnpm format
```

Expected: all commands exit 0.

Commit:

```bash
git add frontend/src/components/workspace/settings/skill-detail-sheet.tsx frontend/src/components/workspace/settings/skill-settings-page.tsx frontend/src/core/skills/hooks.ts frontend/src/core/i18n/locales frontend/tests/e2e/skills.spec.ts frontend/tests/e2e/utils/mock-api.ts
git commit -m "feat(frontend): preview skill markdown in sheet"
```

---

### Task 4: Documentation, integrated verification, and visual QA

**Files:**
- Modify: `README.md`
- Modify: `README_zh.md`
- Modify: `backend/AGENTS.md`
- Modify: `backend/docs/API.md`
- Modify: `frontend/AGENTS.md`
- Create: `design-qa.md`

**Interfaces:**
- Documents: the Web UI preview behavior, guarded Gateway content response, user-scoped safe read path, frontend query ownership, and safe Markdown boundary.
- Verifies: approved desktop open-Sheet target plus mobile responsive state.

- [ ] **Step 1: Update user and architecture documentation**

Add one concise sentence in each README's Skills section explaining that the workspace Skills page can open a read-only rendered `SKILL.md` preview without leaving the list.

Update the Skills API row and Skills System section in `backend/AGENTS.md`, plus
the skills endpoints in `backend/docs/API.md`, to state that admin-only
`GET /api/skills/content/{name}` returns raw Markdown for a skill visible in the
current user's storage scope, validates `Skill.skill_file`, and offloads the
filesystem read. Explicitly state that existing list, metadata detail, and
embedded-client contracts remain unchanged.

Update `frontend/AGENTS.md` Data Flow or Interaction Ownership with:

- `SkillSettingsList` owns selection and Sheet state.
- `useSkillContent` owns the lazy per-skill query.
- `SkillDetailSheet` strips only a parser-compatible leading frontmatter fence
  and uses the existing raw-HTML-disabled Markdown renderer.

- [ ] **Step 2: Run the complete related backend suite**

Run:

```bash
cd backend && uv run pytest tests/test_skills_custom_router.py tests/test_skills_router_authz.py tests/blocking_io/test_skills_router.py -q
cd backend && uv run ruff check app/gateway/routers/skills.py tests/test_skills_custom_router.py tests/test_skills_router_authz.py tests/blocking_io/test_skills_router.py
cd backend && uv run ruff format --check app/gateway/routers/skills.py tests/test_skills_custom_router.py tests/test_skills_router_authz.py tests/blocking_io/test_skills_router.py
```

Expected: all commands exit 0.

- [ ] **Step 3: Run the complete related frontend suite**

Run:

```bash
cd frontend && pnpm test tests/unit/core/skills/api.test.ts tests/unit/core/skills/markdown.test.ts tests/unit/core/skills/slash.test.ts tests/unit/core/skills/slash-contract.test.ts
cd frontend && pnpm test:e2e tests/e2e/skills.spec.ts
cd frontend && pnpm check
cd frontend && pnpm format
```

Expected: all commands exit 0.

- [ ] **Step 4: Capture and compare the implemented desktop state**

Run the normal local DeerFlow frontend and Gateway, open `/workspace/skills`,
and open `academic-paper-review`. Capture the implementation at the same desktop
viewport as
`docs/pr-evidence/skills-markdown-preview/source-mockup.png`. Put that source
mockup and implementation capture into one comparison image/input, then evaluate
typography, spacing, Sheet width, border/shadow, header density, Markdown rhythm,
badges, selected row, and background visibility.

Write `design-qa.md` with:

- Source visual path.
- Implementation screenshot path.
- Viewport and open-Sheet state.
- Full-view and focused header/body comparison evidence.
- Findings and fixes for every P0/P1/P2 mismatch.
- Explicit checks for typography, spacing, tokens, asset/icon fidelity, and copy.
- Comparison history for every corrective iteration.
- Exact final line `final result: passed` only when no actionable P0/P1/P2 issue remains.

- [ ] **Step 5: Verify the mobile state and primary interactions**

At `390 x 844`, verify the Sheet fills the viewport, metadata wraps, code/table regions scroll internally, and the document has no horizontal overflow. Test row click, Switch isolation, close button, Escape, retry, and focus restoration. Check the browser console for errors.

- [ ] **Step 6: Commit documentation and QA evidence**

Run `git diff --check`, then commit:

```bash
git add README.md README_zh.md backend/AGENTS.md backend/docs/API.md frontend/AGENTS.md design-qa.md
git commit -m "docs: document skill markdown preview"
```

- [ ] **Step 7: Request final whole-change code review**

Review the full diff from commit `aab39762` through `HEAD` for specification compliance, security, user isolation, async blocking I/O, Markdown safety, accessibility, responsive behavior, test quality, and unintended changes. Fix every Critical or Important finding, rerun the affected verification commands, and repeat review until approved.
