import { expect, test, type Page } from "@playwright/test";

import { mockLangGraphAPI } from "./utils/mock-api";

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

function mockMemoryAPI(page: Page) {
  void page.route("**/api/memory", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(memoryFixture),
    }),
  );
}

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
