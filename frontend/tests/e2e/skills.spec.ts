import { expect, test, type Page, type Route } from "@playwright/test";

import { mockLangGraphAPI, type MockSkill } from "./utils/mock-api";

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

const PAPER_REVIEW: MockSkill = {
  name: "paper-review",
  description: "Review papers",
  category: "public",
  license: "MIT",
  enabled: true,
};

const DATA_ANALYSIS: MockSkill = {
  name: "data-analysis",
  description: "Analyze structured data",
  category: "public",
  enabled: false,
};

function isContentRequest(url: string) {
  return /\/api\/skills\/content\/[^/]+$/.test(new URL(url).pathname);
}

function trackContentGets(page: Page) {
  const requests: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "GET" && isContentRequest(request.url())) {
      requests.push(request.url());
    }
  });
  return requests;
}

test("opens a read-only skill Markdown sheet and restores focus", async ({
  page,
}) => {
  mockLangGraphAPI(page, {
    skills: [PAPER_REVIEW],
    skillContents: { "paper-review": CONTENT },
  });
  await page.goto("/workspace/skills");

  const trigger = page.getByRole("button", {
    name: "View paper-review SKILL.md",
  });
  await trigger.click();

  const sheet = page.getByRole("dialog", { name: "paper-review" });
  await expect(sheet).toBeVisible();
  await expect(sheet.getByRole("heading", { name: "Workflow" })).toBeVisible();
  await expect(
    sheet.getByText("name: paper-review", { exact: true }),
  ).toHaveCount(0);
  await expect(
    page.locator('button[aria-label="View paper-review SKILL.md"]'),
  ).toHaveAttribute("aria-expanded", "true");

  await page.keyboard.press("Escape");
  await expect(sheet).toHaveCount(0);
  await expect(trigger).toBeFocused();
  await expect(trigger).toHaveAttribute("aria-expanded", "false");
});

test("the Switch toggles the skill without requesting or opening content", async ({
  page,
}) => {
  const contentGets = trackContentGets(page);
  let putCount = 0;
  mockLangGraphAPI(page, {
    skills: [PAPER_REVIEW],
    skillContents: { "paper-review": CONTENT },
  });
  page.on("request", (request) => {
    if (
      request.method() === "PUT" &&
      new URL(request.url()).pathname === "/api/skills/paper-review"
    ) {
      putCount += 1;
    }
  });
  await page.goto("/workspace/skills");

  await page
    .getByRole("switch", { name: "Enable or disable paper-review" })
    .click();

  await expect.poll(() => putCount).toBe(1);
  expect(contentGets).toHaveLength(0);
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("does not request content until an administrator opens a skill", async ({
  page,
}) => {
  const contentGets = trackContentGets(page);
  mockLangGraphAPI(page, {
    skills: [PAPER_REVIEW],
    skillContents: { "paper-review": CONTENT },
  });
  await page.goto("/workspace/skills");

  await expect(page.getByText("paper-review", { exact: true })).toBeVisible();
  await page.waitForTimeout(100);
  expect(contentGets).toHaveLength(0);

  await page
    .getByRole("button", { name: "View paper-review SKILL.md" })
    .click();
  await expect.poll(() => contentGets.length).toBe(1);
});

test("shows a loading skeleton while keeping the skills list visible", async ({
  page,
}) => {
  let heldRoute: Route | null = null;
  mockLangGraphAPI(page, {
    skills: [PAPER_REVIEW],
    skillContents: { "paper-review": CONTENT },
  });
  await page.route("**/api/skills/content/paper-review", (route) => {
    heldRoute = route;
  });
  await page.goto("/workspace/skills");

  await page
    .getByRole("button", { name: "View paper-review SKILL.md" })
    .click();

  const sheet = page.getByRole("dialog", { name: "paper-review" });
  await expect(
    sheet.getByRole("status", { name: "Loading skill content" }),
  ).toBeVisible();
  await expect(
    page
      .locator('[data-slot="item"]')
      .getByText("Review papers", { exact: true }),
  ).toBeVisible();
  expect(heldRoute).not.toBeNull();
  await (heldRoute as unknown as Route).fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ content: CONTENT }),
  });
  await expect(sheet.getByRole("heading", { name: "Workflow" })).toBeVisible();
});

for (const { status, message } of [
  {
    status: 403,
    message: "Admin privileges are required to preview skill content.",
  },
  { status: 404, message: "Skill content is unavailable." },
  { status: 500, message: "Unable to load skill content." },
]) {
  test(`${status} content failures stay open and Retry succeeds`, async ({
    page,
  }) => {
    let released = false;
    mockLangGraphAPI(page, {
      skills: [PAPER_REVIEW],
      skillContents: { "paper-review": CONTENT },
    });
    await page.route("**/api/skills/content/paper-review", (route) => {
      if (!released) {
        return route.fulfill({
          status,
          contentType: "application/json",
          body: JSON.stringify({ detail: "controlled failure" }),
        });
      }
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ content: CONTENT }),
      });
    });
    await page.goto("/workspace/skills");

    await page
      .getByRole("button", { name: "View paper-review SKILL.md" })
      .click();
    const sheet = page.getByRole("dialog", { name: "paper-review" });
    await expect(sheet.getByRole("alert")).toContainText(message);
    await expect(sheet).toBeVisible();

    released = true;
    await sheet.getByRole("button", { name: "Retry" }).click();
    await expect(
      sheet.getByRole("heading", { name: "Workflow" }),
    ).toBeVisible();
  });
}

test("opening skill B never renders skill A content while B loads", async ({
  page,
}) => {
  const contentA = "# Alpha-only body";
  const contentB = "# Bravo-only body";
  let heldRoute: Route | null = null;
  mockLangGraphAPI(page, {
    skills: [PAPER_REVIEW, DATA_ANALYSIS],
    skillContents: {
      "paper-review": contentA,
      "data-analysis": contentB,
    },
  });
  await page.route("**/api/skills/content/data-analysis", (route) => {
    heldRoute = route;
  });
  await page.goto("/workspace/skills");

  await page
    .getByRole("button", { name: "View paper-review SKILL.md" })
    .click();
  await expect(
    page.getByRole("heading", { name: "Alpha-only body" }),
  ).toBeVisible();
  await page.keyboard.press("Escape");

  await page
    .getByRole("button", { name: "View data-analysis SKILL.md" })
    .click();
  const sheet = page.getByRole("dialog", { name: "data-analysis" });
  await expect(
    sheet.getByRole("status", { name: "Loading skill content" }),
  ).toBeVisible();
  await expect(sheet.getByText("Alpha-only body")).toHaveCount(0);

  expect(heldRoute).not.toBeNull();
  await (heldRoute as unknown as Route).fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ content: contentB }),
  });
  await expect(
    sheet.getByRole("heading", { name: "Bravo-only body" }),
  ).toBeVisible();
});

test("raw HTML cannot execute or insert an active script in the sheet", async ({
  page,
}) => {
  const unsafeContent = `# Safe heading

<script>window.__skillPreviewExecuted = true</script>
`;
  mockLangGraphAPI(page, {
    skills: [PAPER_REVIEW],
    skillContents: { "paper-review": unsafeContent },
  });
  await page.goto("/workspace/skills");

  await page
    .getByRole("button", { name: "View paper-review SKILL.md" })
    .click();
  const sheet = page.getByRole("dialog", { name: "paper-review" });
  await expect(
    sheet.getByRole("heading", { name: "Safe heading" }),
  ).toBeVisible();
  await expect(sheet.locator("script")).toHaveCount(0);
  expect(
    await page.evaluate(
      () =>
        (window as typeof window & { __skillPreviewExecuted?: boolean })
          .__skillPreviewExecuted,
    ),
  ).toBeUndefined();
});

test("Escape and close restore focus to each exact row trigger", async ({
  page,
}) => {
  mockLangGraphAPI(page, {
    skills: [PAPER_REVIEW, DATA_ANALYSIS],
    skillContents: {
      "paper-review": CONTENT,
      "data-analysis": "# Data workflow",
    },
  });
  await page.goto("/workspace/skills");

  const paperTrigger = page.getByRole("button", {
    name: "View paper-review SKILL.md",
  });
  await paperTrigger.click();
  await page.keyboard.press("Escape");
  await expect(paperTrigger).toBeFocused();

  const dataTrigger = page.getByRole("button", {
    name: "View data-analysis SKILL.md",
  });
  await dataTrigger.click();
  const sheet = page.getByRole("dialog", { name: "data-analysis" });
  await sheet.getByRole("button", { name: "Close" }).click();
  await expect(dataTrigger).toBeFocused();
});

test("the skill sheet has no horizontal overflow at 390 by 844", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const token = "x".repeat(320);
  const wideCode = Array.from(
    { length: 40 },
    (_, index) => `column-${index}`,
  ).join(" | ");
  mockLangGraphAPI(page, {
    skills: [PAPER_REVIEW],
    skillContents: {
      "paper-review": `# Mobile content\n\n${token}\n\n\`\`\`text\n${wideCode}\n\`\`\``,
    },
  });
  await page.goto("/workspace/skills");

  await page
    .getByRole("button", { name: "View paper-review SKILL.md" })
    .click();
  const sheet = page.getByRole("dialog", { name: "paper-review" });
  await expect(
    sheet.getByRole("heading", { name: "Mobile content" }),
  ).toBeVisible();

  await expect
    .poll(
      async () => (await sheet.boundingBox())?.x ?? Number.POSITIVE_INFINITY,
    )
    .toBeLessThanOrEqual(1);
  const box = await sheet.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.x).toBeLessThanOrEqual(1);
  expect(box!.width).toBeGreaterThanOrEqual(388);
  const overflow = await sheet.evaluate((dialog) => ({
    root:
      document.documentElement.scrollWidth -
      document.documentElement.clientWidth,
    dialog: dialog.scrollWidth - dialog.clientWidth,
  }));
  expect(overflow.root).toBeLessThanOrEqual(0);
  expect(overflow.dialog).toBeLessThanOrEqual(0);
});

test("non-admin skill rows stay passive and never request content", async ({
  page,
}) => {
  const contentGets = trackContentGets(page);
  mockLangGraphAPI(page, {
    skills: [PAPER_REVIEW],
    skillContents: { "paper-review": CONTENT },
  });
  await page.route("**/api/v1/auth/me", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        id: "member",
        email: "member@test.local",
        system_role: "user",
        needs_setup: false,
      }),
    }),
  );
  await page.goto("/workspace/skills");
  await expect(
    page.getByRole("button", { name: "View paper-review SKILL.md" }),
  ).toBeVisible();
  const refreshResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v1/auth/me" &&
      response.request().method() === "GET",
  );
  await page.evaluate(() =>
    document.dispatchEvent(new Event("visibilitychange")),
  );
  await refreshResponse;

  await expect(page.getByText("paper-review", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "View paper-review SKILL.md" }),
  ).toHaveCount(0);
  await page.getByText("paper-review", { exact: true }).click();
  await page.waitForTimeout(100);
  expect(contentGets).toHaveLength(0);
  await expect(page.getByRole("dialog")).toHaveCount(0);
});
