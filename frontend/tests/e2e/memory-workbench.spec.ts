import { expect, test, type Locator, type Page } from "@playwright/test";

import { MOCK_THREAD_ID, mockLangGraphAPI } from "./utils/mock-api";

const memoryFixture = {
  version: "1",
  lastUpdated: "2026-07-10T00:00:00Z",
  user: {
    workContext: {
      summary: "Prefers source-backed work.",
      updatedAt: "2026-07-10T00:00:00Z",
    },
    personalContext: {
      summary: "Uses Chinese.",
      updatedAt: "2026-07-10T00:00:00Z",
    },
    topOfMind: {
      summary: "Redesigning DeerFlow.",
      updatedAt: "2026-07-10T00:00:00Z",
    },
  },
  history: {
    recentMonths: {
      summary: "Forked DeerFlow.",
      updatedAt: "2026-07-10T00:00:00Z",
    },
    earlierContext: { summary: "", updatedAt: "" },
    longTermBackground: { summary: "", updatedAt: "" },
  },
  facts: [
    {
      id: "fact-1",
      content: "Prefers Chinese responses",
      category: "preference",
      confidence: 0.95,
      createdAt: "2026-07-10T00:00:00Z",
      source: "manual",
    },
  ],
};

const longSummary = `Summary${"S".repeat(512)}`;
const longFactContent = `Fact${"F".repeat(512)}`;
const longFactCategory = `Category${"C".repeat(256)}`;

const narrowMemoryFixture = {
  ...memoryFixture,
  user: {
    ...memoryFixture.user,
    workContext: {
      summary: longSummary,
      updatedAt: "2026-07-10T00:00:00Z",
    },
  },
  facts: [
    {
      id: "fact-long",
      content: longFactContent,
      category: longFactCategory,
      confidence: 0.95,
      createdAt: "2026-07-10T00:00:00Z",
      source: MOCK_THREAD_ID,
    },
  ],
};

function mockMemoryAPI(page: Page, fixture = memoryFixture) {
  void page.route("**/api/memory", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(fixture),
    }),
  );
}

async function expectNoHorizontalOverflow(locator: Locator) {
  await expect(locator).toBeVisible();
  await expect
    .poll(() =>
      locator.evaluate((element) => element.scrollWidth - element.clientWidth),
    )
    .toBeLessThanOrEqual(0);
}

async function expectPageNoHorizontalOverflow(page: Page) {
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          document.documentElement.scrollWidth -
          document.documentElement.clientWidth,
      ),
    )
    .toBeLessThanOrEqual(0);
}

test("memory inbox exposes primary add and secondary management actions", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  mockMemoryAPI(page);
  await page.goto("/workspace/memory");

  await expect(page.getByRole("button", { name: "Add fact" })).toHaveAttribute(
    "data-variant",
    "default",
  );
  const manage = page.getByRole("button", { name: "Manage memory" });
  await expect(manage).toHaveAttribute("data-variant", "outline");

  await manage.click();
  await expect(
    page.getByRole("menuitem", { name: "Import memory" }),
  ).toBeVisible();
  await expect(
    page.getByRole("menuitem", { name: "Export memory" }),
  ).toBeVisible();
  await expect(
    page.getByRole("menuitem", { name: "Clear all memory" }),
  ).toHaveAttribute("data-variant", "destructive");
});

test("memory overview derives counts and recent focus from loaded memory", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  mockMemoryAPI(page);
  await page.goto("/workspace/memory");

  const overview = page.getByTestId("memory-overview");
  await expect(overview).toContainText("1 fact");
  await expect(overview).toContainText("4 summaries");
  await expect(overview).toContainText("Redesigning DeerFlow.");
  await expect(
    overview.getByRole("button", { name: "View summaries" }),
  ).toBeVisible();
});

test("memory workbench switches between summary and facts panels", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  mockMemoryAPI(page);
  await page.goto("/workspace/memory");

  await expect(page.getByTestId("memory-workbench")).toBeVisible();
  await expect(page.getByTestId("memory-summary-panel")).toBeVisible();
  await expect(page.getByTestId("memory-facts-panel")).toBeVisible();

  await page.getByTestId("memory-filter-facts").click();
  await expect(page.getByTestId("memory-summary-panel")).toHaveCount(0);
  await expect(page.getByTestId("memory-facts-panel")).toBeVisible();

  await page.getByTestId("memory-filter-summaries").click();
  await expect(page.getByTestId("memory-summary-panel")).toBeVisible();
  await expect(page.getByTestId("memory-facts-panel")).toHaveCount(0);
});

test("wide workbench uses a 36/64 split and single filters stay full width", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  mockLangGraphAPI(page);
  mockMemoryAPI(page);
  await page.goto("/workspace/memory");

  const summaryPanel = page.getByTestId("memory-summary-panel");
  const factsPanel = page.getByTestId("memory-facts-panel");
  await expect(summaryPanel).toBeVisible();
  await expect(factsPanel).toBeVisible();
  const summaryBox = await summaryPanel.boundingBox();
  const factsBox = await factsPanel.boundingBox();
  const allGridBox = await summaryPanel.evaluate((panel) => {
    const grid = panel.parentElement;
    if (!grid) return null;
    const { width, x } = grid.getBoundingClientRect();
    return { width, x };
  });
  expect(summaryBox).not.toBeNull();
  expect(factsBox).not.toBeNull();
  expect(allGridBox).not.toBeNull();

  const combinedPanelWidth = summaryBox!.width + factsBox!.width;
  expect(summaryBox!.width / combinedPanelWidth).toBeGreaterThanOrEqual(0.34);
  expect(summaryBox!.width / combinedPanelWidth).toBeLessThanOrEqual(0.38);
  expect(factsBox!.width / combinedPanelWidth).toBeGreaterThanOrEqual(0.62);
  expect(factsBox!.width / combinedPanelWidth).toBeLessThanOrEqual(0.66);
  expect(Math.abs(summaryBox!.y - factsBox!.y)).toBeLessThanOrEqual(2);

  await page.getByTestId("memory-filter-facts").click();
  await expect(summaryPanel).toHaveCount(0);
  await expect(factsPanel).toBeVisible();
  const factsOnlyPanelBox = await factsPanel.boundingBox();
  expect(factsOnlyPanelBox).not.toBeNull();
  expect(
    Math.abs(factsOnlyPanelBox!.width - allGridBox!.width),
  ).toBeLessThanOrEqual(1);
  expect(Math.abs(factsOnlyPanelBox!.x - allGridBox!.x)).toBeLessThanOrEqual(1);

  await page.getByTestId("memory-filter-summaries").click();
  await expect(summaryPanel).toBeVisible();
  await expect(factsPanel).toHaveCount(0);
  const summariesOnlyPanelBox = await summaryPanel.boundingBox();
  expect(summariesOnlyPanelBox).not.toBeNull();
  expect(
    Math.abs(summariesOnlyPanelBox!.width - allGridBox!.width),
  ).toBeLessThanOrEqual(1);
  expect(
    Math.abs(summariesOnlyPanelBox!.x - allGridBox!.x),
  ).toBeLessThanOrEqual(1);
});

test("narrow workbench stacks long memory content without horizontal overflow", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  mockLangGraphAPI(page);
  mockMemoryAPI(page, narrowMemoryFixture);
  await page.goto("/workspace/memory");

  const workbench = page.getByTestId("memory-workbench");
  const summaryPanel = page.getByTestId("memory-summary-panel");
  const factsPanel = page.getByTestId("memory-facts-panel");
  const factContent = page.getByText(longFactContent, { exact: true });

  await expect(factContent).toBeVisible();
  const summaryBox = await summaryPanel.boundingBox();
  const factsBox = await factsPanel.boundingBox();
  expect(summaryBox).not.toBeNull();
  expect(factsBox).not.toBeNull();
  expect(summaryBox!.y + summaryBox!.height).toBeLessThanOrEqual(factsBox!.y);
  await expectNoHorizontalOverflow(workbench);
  await expectNoHorizontalOverflow(summaryPanel);
  await expectNoHorizontalOverflow(factsPanel);
  await expectPageNoHorizontalOverflow(page);
});

test("persisted dark theme keeps the narrow workbench legible without horizontal overflow", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => {
    window.localStorage.setItem("theme", "dark");
    document.documentElement.classList.remove("light");
  });
  mockLangGraphAPI(page);
  mockMemoryAPI(page, narrowMemoryFixture);
  await page.goto("/workspace/memory");

  const html = page.locator("html");
  const workbench = page.getByTestId("memory-workbench");
  const summaryPanel = page.getByTestId("memory-summary-panel");
  const factsPanel = page.getByTestId("memory-facts-panel");
  const factContent = page.getByText(longFactContent, { exact: true });

  await expect(html).toHaveClass(/\bdark\b/);
  await expect(html).not.toHaveClass(/\blight\b/);
  await expect(summaryPanel).toBeVisible();
  await expect(factsPanel).toBeVisible();
  await expect(factContent).toBeVisible();

  const [textColor, panelBackgroundColor, pageBackgroundColor] =
    await Promise.all([
      factContent.evaluate((element) => getComputedStyle(element).color),
      factsPanel.evaluate(
        (element) => getComputedStyle(element).backgroundColor,
      ),
      page
        .locator("body")
        .evaluate((element) => getComputedStyle(element).backgroundColor),
    ]);
  expect(textColor).not.toBe(panelBackgroundColor);
  expect(textColor).not.toBe(pageBackgroundColor);

  await expectNoHorizontalOverflow(workbench);
  await expectNoHorizontalOverflow(summaryPanel);
  await expectNoHorizontalOverflow(factsPanel);
  await expectPageNoHorizontalOverflow(page);
});

test("fact-only search expands the facts panel to the full workbench width", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  mockMemoryAPI(page);
  await page.goto("/workspace/memory");

  await expect(page.getByTestId("memory-filter-all")).toHaveAttribute(
    "data-state",
    "on",
  );
  await page.getByPlaceholder("Search memory").fill("Chinese responses");

  await expect(page.getByTestId("memory-summary-panel")).toHaveCount(0);
  const factsPanel = page.getByTestId("memory-facts-panel");
  await expect(factsPanel).toBeVisible();
  await expect(factsPanel.locator("..")).not.toHaveClass(/lg:grid-cols-/);
});
