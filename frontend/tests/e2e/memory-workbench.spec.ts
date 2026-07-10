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

const emptyMemoryFixture = {
  ...memoryFixture,
  user: {
    workContext: { ...memoryFixture.user.workContext, summary: "" },
    personalContext: { ...memoryFixture.user.personalContext, summary: "" },
    topOfMind: { ...memoryFixture.user.topOfMind, summary: "" },
  },
  history: {
    recentMonths: { ...memoryFixture.history.recentMonths, summary: "" },
    earlierContext: { ...memoryFixture.history.earlierContext, summary: "" },
    longTermBackground: {
      ...memoryFixture.history.longTermBackground,
      summary: "",
    },
  },
  facts: [],
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

type MemoryFixture = typeof memoryFixture;
type MemoryRequest = {
  method: string;
  path: string;
  body: unknown;
};

async function mockMemoryAPI(
  page: Page,
  initial: MemoryFixture = memoryFixture,
) {
  let current = structuredClone(initial);
  const requests: MemoryRequest[] = [];

  await page.route(/\/api\/memory(?:\/.*)?$/, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();
    const body = request.postDataJSON() as unknown;
    requests.push({ method, path: url.pathname, body });

    if (method === "GET" && url.pathname.endsWith("/api/memory/export")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(current),
      });
      return;
    }
    if (method === "POST" && url.pathname.endsWith("/api/memory/import")) {
      current = structuredClone(body as MemoryFixture);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(current),
      });
      return;
    }
    if (method === "POST" && url.pathname.endsWith("/api/memory/facts")) {
      const input = body as {
        content: string;
        category: string;
        confidence: number;
      };
      current = {
        ...current,
        facts: [
          ...current.facts,
          {
            id: "fact-created",
            ...input,
            createdAt: "2026-07-10T01:00:00Z",
            source: "manual",
          },
        ],
      };
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(current),
      });
      return;
    }
    const factMatch = /\/api\/memory\/facts\/([^/]+)$/.exec(url.pathname);
    if (factMatch && method === "PATCH") {
      current = {
        ...current,
        facts: current.facts.map((fact) =>
          fact.id === decodeURIComponent(factMatch[1]!)
            ? { ...fact, ...(body as object) }
            : fact,
        ),
      };
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(current),
      });
      return;
    }
    if (factMatch && method === "DELETE") {
      current = {
        ...current,
        facts: current.facts.filter(
          (fact) => fact.id !== decodeURIComponent(factMatch[1]!),
        ),
      };
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(current),
      });
      return;
    }
    if (method === "DELETE" && url.pathname.endsWith("/api/memory")) {
      current = structuredClone(emptyMemoryFixture);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(current),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(current),
    });
  });

  return { requests };
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
  await mockMemoryAPI(page);
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

test("fact create, edit, and delete keep their existing request contracts", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  const api = await mockMemoryAPI(page);
  await page.goto("/workspace/memory");

  await page.getByRole("button", { name: "Add fact" }).click();
  await page.getByLabel("Content").fill("New durable context");
  await page.getByLabel("Category").fill("context");
  await page.getByLabel("Confidence").fill("0.75");
  await page.getByRole("button", { name: "Save fact" }).click();
  await expect(
    page.getByText("New durable context", { exact: true }),
  ).toBeVisible();

  const created = api.requests.find(
    (request) =>
      request.method === "POST" && request.path.endsWith("/api/memory/facts"),
  );
  expect(created?.body).toEqual({
    content: "New durable context",
    category: "context",
    confidence: 0.75,
  });

  const createdRow = page.getByTestId("memory-fact-row-fact-created");
  await createdRow.getByRole("button", { name: "Edit" }).click();
  await page.getByLabel("Content").fill("Updated durable context");
  await page.getByRole("button", { name: "Save fact" }).click();
  await expect(
    page.getByText("Updated durable context", { exact: true }),
  ).toBeVisible();
  expect(
    api.requests.some(
      (request) =>
        request.method === "PATCH" &&
        request.path.endsWith("/api/memory/facts/fact-created"),
    ),
  ).toBe(true);

  const updatedRow = page.getByTestId("memory-fact-row-fact-created");
  await updatedRow.getByRole("button", { name: "Delete" }).click();
  await page.getByRole("button", { name: "Delete", exact: true }).click();
  await expect(
    page.getByText("Updated durable context", { exact: true }),
  ).toHaveCount(0);
  expect(
    api.requests.some(
      (request) =>
        request.method === "DELETE" &&
        request.path.endsWith("/api/memory/facts/fact-created"),
    ),
  ).toBe(true);
});

test("management menu preserves export, import, and clear flows", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  const api = await mockMemoryAPI(page);
  await page.goto("/workspace/memory");

  await page.getByRole("button", { name: "Manage memory" }).click();
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("menuitem", { name: "Export memory" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(/^deerflow-memory-.*\.json$/);
  expect(
    api.requests.some(
      (request) =>
        request.method === "GET" && request.path.endsWith("/api/memory/export"),
    ),
  ).toBe(true);

  await page.getByRole("button", { name: "Manage memory" }).click();
  const chooserPromise = page.waitForEvent("filechooser");
  await page.getByRole("menuitem", { name: "Import memory" }).click();
  const chooser = await chooserPromise;
  await chooser.setFiles({
    name: "memory.json",
    mimeType: "application/json",
    buffer: Buffer.from(JSON.stringify(memoryFixture)),
  });
  const importRequestPromise = page.waitForRequest((request) => {
    const url = new URL(request.url());
    return (
      request.method() === "POST" && url.pathname.endsWith("/api/memory/import")
    );
  });
  await page.getByRole("button", { name: "Import", exact: true }).click();
  const importRequest = await importRequestPromise;
  expect(importRequest.method()).toBe("POST");
  expect(new URL(importRequest.url()).pathname).toBe("/api/memory/import");
  expect(importRequest.postDataJSON()).toEqual(memoryFixture);
  expect(
    api.requests.some(
      (request) =>
        request.method === "POST" &&
        request.path.endsWith("/api/memory/import"),
    ),
  ).toBe(true);

  await page.getByRole("button", { name: "Manage memory" }).click();
  await page.getByRole("menuitem", { name: "Clear all memory" }).click();
  await page.getByRole("button", { name: "Clear all memory" }).click();
  await expect(page.getByTestId("memory-empty-state")).toBeVisible();
  expect(
    api.requests.some(
      (request) =>
        request.method === "DELETE" && request.path.endsWith("/api/memory"),
    ),
  ).toBe(true);
});

test("memory overview derives counts and recent focus from loaded memory", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  await mockMemoryAPI(page);
  await page.goto("/workspace/memory");

  const overview = page.getByTestId("memory-overview");
  await expect(overview).toContainText("1 fact");
  await expect(overview).toContainText("4 summaries");
  await expect(overview).toContainText("Redesigning DeerFlow.");
  await expect(
    overview.getByRole("button", { name: "View summaries" }),
  ).toBeVisible();
});

test("filters preserve facts and summaries visibility", async ({ page }) => {
  mockLangGraphAPI(page);
  await mockMemoryAPI(page);
  await page.goto("/workspace/memory");

  await page.getByTestId("memory-filter-facts").click();
  await expect(page.getByTestId("memory-facts-panel")).toBeVisible();
  await expect(page.getByTestId("memory-summary-disclosure")).toHaveCount(0);

  await page.getByTestId("memory-filter-summaries").click();
  await expect(page.getByTestId("memory-facts-panel")).toHaveCount(0);
  await expect(page.getByTestId("memory-summary-panel")).toBeVisible();
});

test("facts stay full width while summaries use a disclosure", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  mockLangGraphAPI(page);
  await mockMemoryAPI(page);
  await page.goto("/workspace/memory");

  const workbench = page.getByTestId("memory-workbench");
  const facts = page.getByTestId("memory-facts-panel");
  const disclosure = page.getByTestId("memory-summary-disclosure");
  await expect(facts).toBeVisible();
  await expect(disclosure).toBeVisible();
  await expect(page.getByTestId("memory-summary-panel")).toHaveCount(0);

  const workbenchBox = await workbench.boundingBox();
  const factsBox = await facts.boundingBox();
  expect(workbenchBox).not.toBeNull();
  expect(factsBox).not.toBeNull();
  expect(factsBox!.width / workbenchBox!.width).toBeGreaterThan(0.95);

  await disclosure.getByRole("button", { name: /Smart summaries/ }).click();
  await expect(page.getByTestId("memory-summary-panel")).toBeVisible();
});

test("a summary-only search match opens the matching summary", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  await mockMemoryAPI(page);
  await page.goto("/workspace/memory");

  await page.getByPlaceholder("Search memory").fill("Forked DeerFlow");
  await expect(page.getByTestId("memory-summary-panel")).toBeVisible();
  await expect(
    page.getByText("Forked DeerFlow.", { exact: true }),
  ).toBeVisible();
  await expect(page.getByTestId("memory-facts-panel")).toHaveCount(0);
});

test("fully empty memory renders one recovery state without empty panels", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  await mockMemoryAPI(page, emptyMemoryFixture);
  await page.goto("/workspace/memory");

  await expect(page.getByTestId("memory-empty-state")).toBeVisible();
  await expect(page.getByTestId("memory-facts-panel")).toHaveCount(0);
  await expect(page.getByTestId("memory-summary-panel")).toHaveCount(0);
  await expect(
    page
      .getByTestId("memory-empty-state")
      .getByRole("button", { name: "Add fact" }),
  ).toBeVisible();
});

test("memory loading state announces localized progress", async ({ page }) => {
  mockLangGraphAPI(page);
  let releaseMemory!: () => void;
  const memoryHeld = new Promise<void>((resolve) => {
    releaseMemory = resolve;
  });
  let memoryStarted!: () => void;
  const memoryStartedPromise = new Promise<void>((resolve) => {
    memoryStarted = resolve;
  });
  await page.route("**/api/memory", async (route) => {
    if (route.request().method() !== "GET") {
      return route.fallback();
    }
    memoryStarted();
    await memoryHeld;
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(memoryFixture),
    });
  });

  await page.goto("/workspace/memory");
  await memoryStartedPromise;

  const loadingState = page.getByRole("status", { name: "Loading..." });
  try {
    await expect(loadingState).toHaveAttribute(
      "data-testid",
      "memory-loading-state",
    );
    await expect(loadingState).toHaveAttribute("aria-live", "polite");
    await expect(loadingState).toHaveAttribute("aria-busy", "true");
  } finally {
    releaseMemory();
  }

  await expect(loadingState).toHaveCount(0);
});

test("learned fact source navigates to its originating thread", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  await mockMemoryAPI(page, narrowMemoryFixture);
  await page.goto("/workspace/memory");

  await page
    .getByTestId("memory-fact-row-fact-long")
    .getByRole("link", { name: "View" })
    .click();
  await expect(page).toHaveURL(`/workspace/chats/${MOCK_THREAD_ID}`);
});

test("narrow workbench stacks long memory content without horizontal overflow", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  mockLangGraphAPI(page);
  await mockMemoryAPI(page, narrowMemoryFixture);
  await page.goto("/workspace/memory");

  const workbench = page.getByTestId("memory-workbench");
  const summaryDisclosure = page.getByTestId("memory-summary-disclosure");
  const summaryPanel = page.getByTestId("memory-summary-panel");
  const factsPanel = page.getByTestId("memory-facts-panel");
  const factContent = page.getByText(longFactContent, { exact: true });
  const summaryTrigger = summaryDisclosure.getByRole("button", {
    name: /Smart summaries/,
  });
  const summaryDescription = summaryTrigger
    .locator("span")
    .filter({ hasText: /Summary sections are read-only for now/ })
    .last();

  await expect(factContent).toBeVisible();
  await expect(summaryDisclosure).toBeVisible();
  await expect(factsPanel).toBeVisible();
  await expect(summaryDescription).toBeVisible();
  const descriptionLayout = await summaryDescription.evaluate((element) => {
    const range = document.createRange();
    range.selectNodeContents(element);
    const textRects = Array.from(range.getClientRects()).filter(
      (rect) => rect.width > 0 && rect.height > 0,
    );
    const button = element.closest("button");
    const icons = button?.querySelectorAll("svg");
    const chevron = icons?.length ? icons[icons.length - 1] : null;

    return {
      lineCount: new Set(textRects.map((rect) => Math.round(rect.top))).size,
      textRight: Math.max(...textRects.map((rect) => rect.right)),
      chevronLeft: chevron?.getBoundingClientRect().left ?? null,
      scrollWidth: element.scrollWidth,
      clientWidth: element.clientWidth,
    };
  });
  expect(descriptionLayout.lineCount).toBeGreaterThan(1);
  expect(descriptionLayout.scrollWidth).toBeLessThanOrEqual(
    descriptionLayout.clientWidth,
  );
  expect(descriptionLayout.chevronLeft).not.toBeNull();
  expect(descriptionLayout.textRight).toBeLessThanOrEqual(
    descriptionLayout.chevronLeft!,
  );
  await summaryTrigger.click();
  await expect(summaryPanel).toBeVisible();
  const summaryBox = await summaryPanel.boundingBox();
  const factsBox = await factsPanel.boundingBox();
  expect(summaryBox).not.toBeNull();
  expect(factsBox).not.toBeNull();
  expect(factsBox!.y + factsBox!.height).toBeLessThanOrEqual(summaryBox!.y);
  await expectNoHorizontalOverflow(workbench);
  await expectNoHorizontalOverflow(summaryDisclosure);
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
  await mockMemoryAPI(page, narrowMemoryFixture);
  await page.goto("/workspace/memory");

  const html = page.locator("html");
  const workbench = page.getByTestId("memory-workbench");
  const summaryDisclosure = page.getByTestId("memory-summary-disclosure");
  const summaryPanel = page.getByTestId("memory-summary-panel");
  const factsPanel = page.getByTestId("memory-facts-panel");
  const factContent = page.getByText(longFactContent, { exact: true });

  await expect(html).toHaveClass(/\bdark\b/);
  await expect(html).not.toHaveClass(/\blight\b/);
  await expect(summaryDisclosure).toBeVisible();
  await expect(factsPanel).toBeVisible();
  await summaryDisclosure
    .getByRole("button", { name: /Smart summaries/ })
    .click();
  await expect(summaryPanel).toBeVisible();
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
  await expectNoHorizontalOverflow(summaryDisclosure);
  await expectNoHorizontalOverflow(summaryPanel);
  await expectNoHorizontalOverflow(factsPanel);
  await expectPageNoHorizontalOverflow(page);
});

test("fact-only search expands the facts panel to the full workbench width", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  await mockMemoryAPI(page);
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
