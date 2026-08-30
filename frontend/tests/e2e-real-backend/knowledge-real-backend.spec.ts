import { readFileSync } from "node:fs";

import { expect, test, type BrowserContext, type Page } from "@playwright/test";

import {
  registerReplayProject,
  type ReplayProjectScope,
} from "./project-fixture";

const APP =
  process.env.E2E_APP_URL ??
  `http://localhost:${process.env.E2E_FRONTEND_PORT ?? "3000"}`;

const KNOWLEDGE_ENV_READY = Boolean(
  process.env.ACT_WEAVE_KNOWLEDGE_MINIO_ENDPOINT &&
  process.env.ACT_WEAVE_KNOWLEDGE_MINIO_ACCESS_KEY &&
  process.env.ACT_WEAVE_KNOWLEDGE_MINIO_SECRET_KEY,
);

test.skip(
  !KNOWLEDGE_ENV_READY,
  "requires ACT_WEAVE_KNOWLEDGE_MINIO_* so the replay Gateway enables the Knowledge module",
);
test.skip(
  process.env.E2E_REPLAY_WORKER_MODE === "delayed",
  "requires the immediate replay Worker to execute knowledge tasks",
);

/**
 * Deterministic contract with backend/tests/replay_knowledge.py:
 * - documents containing the marker embed far from every query (cosine ranks
 *   them last) but rerank at 0.95 (final order ranks them first);
 * - all other texts rerank below 0.6.
 * Queries must never contain the marker.
 */
const RERANK_MARKER = "深海列车";
const MARKER_PHONE = "400-800-1234";

// The marker paragraph leads the document, so whatever way the splitter packs
// the ~900-char filler paragraphs behind it, the first segment always starts
// with the marker line and its 320-char search snippet shows the phone number.
function buildDocumentText(): string {
  const markerParagraph = `${RERANK_MARKER}项目组的应急联系电话是 ${MARKER_PHONE}，夜间值班室位于三号楼一层。`;
  const fillerA =
    "巡检班组在夜间进入车辆段，逐一记录道岔状态、信号机显示与接触网电压，并将异常条目同步到当班调度台。".repeat(
      19,
    );
  const fillerB =
    "维修车间按周执行转向架探伤计划，探伤结果归档到质量台账，超限项按流程升级到技术组复核并限期整改。".repeat(
      19,
    );
  return `${markerParagraph}\n\n${fillerA}\n\n${fillerB}`;
}

async function listProjectObjects(
  context: BrowserContext,
  project: ReplayProjectScope,
): Promise<string[]> {
  const response = await context.request.get(
    `${APP}/api/test-only/replay-knowledge/objects`,
  );
  expect(response.status(), await response.text()).toBe(200);
  const body = (await response.json()) as { keys: string[] };
  return body.keys.filter((key) => key.includes(project.id));
}

async function setEmbeddingFailures(
  context: BrowserContext,
  project: ReplayProjectScope,
  failures: number,
): Promise<void> {
  const response = await context.request.post(
    `${APP}/api/test-only/replay-knowledge/provider/faults`,
    {
      headers: { "X-CSRF-Token": project.csrf },
      data: { embedding_failures: failures },
    },
  );
  expect(response.status(), await response.text()).toBe(200);
}

async function providerCounters(context: BrowserContext): Promise<{
  embedding_calls: number;
  rerank_calls: number;
  embedding_failures_remaining: number;
}> {
  const response = await context.request.get(
    `${APP}/api/test-only/replay-knowledge/provider`,
  );
  expect(response.status(), await response.text()).toBe(200);
  return (await response.json()) as {
    embedding_calls: number;
    rerank_calls: number;
    embedding_failures_remaining: number;
  };
}

async function createBaseThroughUI(page: Page, name: string): Promise<void> {
  // "New base" opens the document-first wizard; acceptance drives the
  // empty-base dialog and uploads from the documents page instead.
  await page.getByRole("button", { name: "New base" }).click();
  await page.getByRole("button", { name: "Create an empty base" }).click();
  await page.getByLabel("Name").fill(name);
  await page.getByLabel("Embedding model").click();
  await page
    .getByRole("option", { name: "Replay Knowledge Model · replay/embedding" })
    .click();
  await page.getByRole("button", { name: "Create", exact: true }).click();
  await expect(
    page.getByTestId("knowledge-base-list").getByText(name),
  ).toBeVisible();
}

// M9: the wizard binds only the embedding model, so a fresh base retrieves by
// raw cosine similarity — and the marker segment embeds far from every query,
// below the 0.2 default threshold. Rerank-contract scenarios therefore bind
// the replay reranker through the settings panel first (a plain PATCH,
// effective on save, no rebuild). Callers navigate away afterwards.
async function setRerankerThroughUI(
  page: Page,
  optionName: "Replay Knowledge Model · replay/reranker" | "No reranking",
): Promise<void> {
  await page.getByRole("button", { name: "Settings" }).click();
  await page.getByLabel("Reranker model").click();
  await page.getByRole("option", { name: optionName }).click();
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("Saved.")).toBeVisible();
}

async function uploadDocumentThroughUI(
  page: Page,
  fileName: string,
  contents: string,
): Promise<void> {
  await page.getByRole("button", { name: "Upload document" }).click();
  await page.getByLabel("File").setInputFiles({
    name: fileName,
    mimeType: "text/plain",
    buffer: Buffer.from(contents, "utf-8"),
  });
  await page.getByRole("button", { name: "Upload", exact: true }).click();
}

async function openDocumentActions(page: Page, documentName: string) {
  const row = page
    .getByTestId("knowledge-document-rows")
    .getByRole("row")
    .filter({ hasText: documentName });
  await row
    .getByRole("button", { name: `Actions for ${documentName}` })
    .click();
  return page.getByRole("menu");
}

test("the real Worker ingests an upload to ready, rerank owns the final order, and download returns the exact bytes", async ({
  page,
  context,
}) => {
  test.setTimeout(180_000);
  const project = await registerReplayProject(context, APP);
  const documentText = buildDocumentText();

  await page.goto(`/projects/${encodeURIComponent(project.slug)}/knowledge`);
  // The navigation entry exists because the real health probe passed.
  await expect(
    page
      .getByRole("navigation", { name: "Project navigation" })
      .getByRole("link", { name: "Knowledge" })
      .first(),
  ).toBeVisible();

  await createBaseThroughUI(page, "验收知识库");
  await page.getByRole("button", { name: "View documents" }).click();
  await uploadDocumentThroughUI(page, "manual.txt", documentText);

  // queued/processing/ready is driven by the real knowledge task worker.
  const rows = page.getByTestId("knowledge-document-rows");
  await expect(rows.getByText("manual.txt").first()).toBeVisible();
  await expect(rows.getByText("Ready")).toBeVisible({ timeout: 60_000 });

  // Segment browsing returns the real pgvector-stored segment contents.
  await (await openDocumentActions(page, "manual.txt"))
    .getByRole("menuitem", { name: "View segments" })
    .click();
  const browser = page.getByTestId("knowledge-segment-browser");
  await expect(
    browser
      .getByTestId("knowledge-segment-list")
      .getByText(MARKER_PHONE, { exact: false }),
  ).toBeVisible();
  await browser.getByRole("button", { name: "Documents" }).click();
  await expect(rows.getByText("manual.txt").first()).toBeVisible();

  // The stored object round-trips byte-identically through MinIO.
  const downloadMenu = await openDocumentActions(page, "manual.txt");
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    downloadMenu.getByRole("menuitem", { name: "Download original" }).click(),
  ]);
  const downloadPath = await download.path();
  expect(readFileSync(downloadPath, "utf-8")).toBe(documentText);

  // A fresh base has no reranker: search returns cosine-ordered results, and
  // the marker segment (far from every query, under the 0.2 threshold) is
  // absent. The retrieval test lives in the in-base secondary menu.
  await page.getByRole("button", { name: "Retrieval test" }).click();
  await page.getByLabel("Query").fill("如何联系应急值班团队");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  const results = page.getByTestId("knowledge-search-results");
  const items = results.getByRole("listitem");
  await expect(items.first()).toContainText("Retrieval score 1.000", {
    timeout: 30_000,
  });
  await expect(results.getByText(MARKER_PHONE, { exact: false })).toHaveCount(
    0,
  );

  // Binding the reranker is a plain settings PATCH: provider embedding calls
  // must not grow (no re-embed). Sampling right before the change isolates
  // the assertion from ingestion and search query embeddings.
  const beforeBinding = await providerCounters(context);
  await setRerankerThroughUI(page, "Replay Knowledge Model · replay/reranker");
  expect((await providerCounters(context)).embedding_calls).toBe(
    beforeBinding.embedding_calls,
  );

  // Two-stage search: cosine ranks the marker segment last (axis embeddings),
  // the replay reranker puts it first at 0.95 — the page must show rerank
  // order.
  await page.getByRole("button", { name: "Retrieval test" }).click();
  await page.getByLabel("Query").fill("如何联系应急值班团队");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(items.first()).toContainText(MARKER_PHONE, {
    timeout: 30_000,
  });
  await expect(items.first()).toContainText("Retrieval score 0.950");
  expect(await items.count()).toBeGreaterThanOrEqual(2);
  await expect(items.nth(1)).not.toContainText(MARKER_PHONE);

  // Clearing the binding walks retrieval back to cosine order: results stay
  // non-empty and the marker segment drops out again.
  await setRerankerThroughUI(page, "No reranking");
  await page.getByRole("button", { name: "Retrieval test" }).click();
  await page.getByLabel("Query").fill("如何联系应急值班团队");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(items.first()).toContainText("Retrieval score 1.000", {
    timeout: 30_000,
  });
  await expect(results.getByText(MARKER_PHONE, { exact: false })).toHaveCount(
    0,
  );

  // Both provider stages really ran: ingestion + query embeddings, one rerank.
  const counters = await providerCounters(context);
  expect(counters.embedding_calls).toBeGreaterThanOrEqual(2);
  expect(counters.rerank_calls).toBeGreaterThanOrEqual(1);
});

test("deleting the document and the base removes their MinIO objects", async ({
  page,
  context,
}) => {
  test.setTimeout(180_000);
  const project = await registerReplayProject(context, APP);

  await page.goto(`/projects/${encodeURIComponent(project.slug)}/knowledge`);
  await createBaseThroughUI(page, "清理知识库");
  await page.getByRole("button", { name: "View documents" }).click();
  await uploadDocumentThroughUI(page, "first.txt", buildDocumentText());

  const rows = page.getByTestId("knowledge-document-rows");
  await expect(rows.getByText("Ready")).toBeVisible({ timeout: 60_000 });
  expect(await listProjectObjects(context, project)).toHaveLength(1);

  // Document deletion is a real worker task; the object must be gone after.
  await (await openDocumentActions(page, "first.txt"))
    .getByRole("menuitem", { name: "Delete" })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Delete", exact: true })
    .click();
  await expect(
    page.getByText("No documents yet", { exact: false }),
  ).toBeVisible({ timeout: 60_000 });
  await expect
    .poll(async () => (await listProjectObjects(context, project)).length, {
      timeout: 30_000,
    })
    .toBe(0);

  // Base deletion also purges the objects of documents still inside it.
  await uploadDocumentThroughUI(page, "second.txt", buildDocumentText());
  await expect(rows.getByText("Ready")).toBeVisible({ timeout: 60_000 });
  expect(await listProjectObjects(context, project)).toHaveLength(1);

  // The documents view replaces the base list; return before deleting.
  await page.getByRole("button", { name: "Back" }).click();
  const baseList = page.getByTestId("knowledge-base-list");
  await expect(baseList).toBeVisible();
  const baseRow = baseList
    .getByRole("listitem")
    .filter({ hasText: "清理知识库" });
  await baseRow.getByRole("button", { name: "Delete", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Delete", exact: true })
    .click();
  await expect(baseList.getByText("清理知识库")).toHaveCount(0, {
    timeout: 60_000,
  });
  await expect
    .poll(async () => (await listProjectObjects(context, project)).length, {
      timeout: 30_000,
    })
    .toBe(0);
});

test("segment edits re-embed for retrieval and disables exclude content at both levels", async ({
  page,
  context,
}) => {
  test.setTimeout(240_000);
  const project = await registerReplayProject(context, APP);

  await page.goto(`/projects/${encodeURIComponent(project.slug)}/knowledge`);
  await createBaseThroughUI(page, "治理知识库");
  await page.getByRole("button", { name: "View documents" }).click();
  await setRerankerThroughUI(page, "Replay Knowledge Model · replay/reranker");
  await page.getByRole("button", { name: "Documents" }).click();
  await uploadDocumentThroughUI(page, "govern.txt", buildDocumentText());

  const rows = page.getByTestId("knowledge-document-rows");
  await expect(rows.getByText("Ready")).toBeVisible({ timeout: 60_000 });

  // Baseline: the marker segment is retrievable before any governance ops.
  const results = page.getByTestId("knowledge-search-results");
  await page.getByRole("button", { name: "Retrieval test" }).click();
  await page.getByLabel("Query").fill("如何联系应急值班团队");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(
    results.getByText(MARKER_PHONE, { exact: false }).first(),
  ).toBeVisible({ timeout: 30_000 });

  // Edit a filler segment to carry the marker plus a fresh token: the
  // synchronous re-embed must make the new content retrievable immediately.
  await page.getByRole("button", { name: "Documents" }).click();
  await (await openDocumentActions(page, "govern.txt"))
    .getByRole("menuitem", { name: "View segments" })
    .click();
  const browser = page.getByTestId("knowledge-segment-browser");
  const list = browser.getByTestId("knowledge-segment-list");
  await list
    .getByRole("listitem")
    .nth(1)
    .getByRole("button", { name: "Edit" })
    .click();
  await page
    .getByLabel("Content")
    .fill(`${RERANK_MARKER}最新的应急预案编号是 EMG-2077。`);
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Save", exact: true })
    .click();
  await expect(list.getByText("EMG-2077", { exact: false })).toBeVisible();

  // Disable the segment that carries the phone number (always segment #1
  // because the marker paragraph leads the document).
  await list.getByRole("switch", { name: "Disable segment #1" }).click();
  await expect(
    list.getByRole("switch", { name: "Enable segment #1" }),
  ).toBeVisible();

  // The edited segment hits; the disabled one is gone from retrieval.
  await page.getByRole("button", { name: "Retrieval test" }).click();
  await page.getByLabel("Query").fill("最新的应急预案编号");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(
    results.getByText("EMG-2077", { exact: false }).first(),
  ).toBeVisible({ timeout: 30_000 });
  await expect(results.getByText(MARKER_PHONE, { exact: false })).toHaveCount(
    0,
  );

  // Disabling the whole document empties retrieval entirely.
  await page.getByRole("button", { name: "Documents" }).click();
  await rows.getByRole("switch", { name: "Disable govern.txt" }).click();
  await expect(
    rows.getByRole("switch", { name: "Enable govern.txt" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Retrieval test" }).click();
  await page.getByLabel("Query").fill("最新的应急预案编号");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(page.getByTestId("knowledge-search-empty")).toBeVisible({
    timeout: 30_000,
  });

  // Re-enabling the document restores the enabled segments only.
  await page.getByRole("button", { name: "Documents" }).click();
  await rows.getByRole("switch", { name: "Enable govern.txt" }).click();
  await expect(
    rows.getByRole("switch", { name: "Disable govern.txt" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Retrieval test" }).click();
  await page.getByLabel("Query").fill("最新的应急预案编号");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(
    results.getByText("EMG-2077", { exact: false }).first(),
  ).toBeVisible({ timeout: 30_000 });
});

test("wizard chunk preview matches the segments the real pipeline ingests", async ({
  page,
  context,
}) => {
  test.setTimeout(180_000);
  const project = await registerReplayProject(context, APP);

  // Three sentences ending in 。, each long enough (>100 chars) that two never
  // pack into one 200-char chunk. Noisy spacing and a URL make both cleaning
  // rules observable in the preview.
  const sentences = [
    `值班规程要求值班员每小时记录设备状态，多  余   空格与制表符\t必须被清洗规则压缩，${"记录内容包括道岔状态与信号机显示，".repeat(4)}结果同步调度台。`,
    `夜间联系方式发布在内网 https://example.com/duty 页面，邮箱 duty@example.com 会被链接清洗规则删除，${"值班电话装订在一号楼值班室的台账首页，".repeat(4)}以备离线查阅。`,
    `巡检班组交接班时核对工具清单并在台账签字确认，${"缺失工具需要在交接记录中注明数量与去向，".repeat(4)}由接班组长复核。`,
  ];
  const documentText = sentences.join("\n");

  await page.goto(`/projects/${encodeURIComponent(project.slug)}/knowledge`);
  // "New base" opens the document-first wizard.
  await page.getByRole("button", { name: "New base" }).click();
  await page.getByLabel("File").setInputFiles({
    name: "preview.txt",
    mimeType: "text/plain",
    buffer: Buffer.from(documentText, "utf-8"),
  });
  await page.getByRole("button", { name: "Next" }).click();

  // Tune every K2 parameter away from its default.
  await page.getByLabel("Chunk size (characters)").fill("200");
  await page.getByLabel("Chunk overlap (characters)").fill("0");
  await page.getByLabel("Delimiter").fill("。");
  await page
    .getByLabel("Replace consecutive spaces, newlines and tabs")
    .check();
  await page.getByLabel("Delete all URLs and email addresses").check();
  await page.getByLabel("Name").fill("预览一致性");
  await page.getByLabel("Embedding model").click();
  await page
    .getByRole("option", { name: "Replay Knowledge Model · replay/embedding" })
    .click();

  // Parameter edits do not re-upload the full file. One explicit refresh
  // generates the final-parameter preview used for parity below.
  const previewPanel = page.getByTestId("chunk-preview-panel");
  await expect(
    previewPanel.getByText(
      "Preview is out of date. Refresh to apply the current settings.",
    ),
  ).toBeVisible();
  await previewPanel.getByRole("button", { name: "Refresh preview" }).click();
  const chunkParagraphs = previewPanel.locator("ul li p:last-child");
  let previewChunks: string[] = [];
  await expect
    .poll(
      async () => {
        previewChunks = await chunkParagraphs.allTextContents();
        return (
          previewChunks.length >= 3 &&
          previewChunks.every((text) => !text.includes("https://")) &&
          previewChunks.every((text) => !/ {2}/.test(text))
        );
      },
      { timeout: 30_000 },
    )
    .toBe(true);
  await expect(previewPanel.getByText(/chunks in total/)).toBeVisible();

  // The real Worker ingests with the same frozen parameters.
  await page.getByRole("button", { name: "Save & process" }).click();
  await expect(page.getByText("Knowledge base created")).toBeVisible();
  const statusList = page.getByTestId("wizard-document-status");
  await expect(statusList.getByText("Ready")).toBeVisible({
    timeout: 60_000,
  });
  await page.getByRole("button", { name: "Go to documents" }).click();

  await (await openDocumentActions(page, "preview.txt"))
    .getByRole("menuitem", { name: "View segments" })
    .click();
  const list = page
    .getByTestId("knowledge-segment-browser")
    .getByTestId("knowledge-segment-list");
  await expect(list.getByRole("listitem").first()).toBeVisible({
    timeout: 30_000,
  });

  // Byte-identical parity between the preview and the stored segments.
  const segmentTexts = await list.locator("li > p").allTextContents();
  expect(segmentTexts).toEqual(previewChunks);
});

test("parent-child upload rolls child hits up to one parent citation and base defaults drive the retrieval test", async ({
  page,
  context,
}) => {
  test.setTimeout(240_000);
  const project = await registerReplayProject(context, APP);

  // Parent 1 (two marker lines, each >100 chars so a 200-char child limit
  // keeps them apart): both children embed on the marker axis, so the parent
  // is only reachable through child recall and must be deduplicated from two
  // child hits into one citation. Parent 2 is a single 900-char line, which
  // the child splitter hard-cuts into plain-axis children that dominate
  // cosine recall.
  const lineA = `${RERANK_MARKER}项目组的应急联系电话是 ${MARKER_PHONE}，${"电话台账在值班簿首页登记，".repeat(8)}保持畅通。`;
  const lineB = `${RERANK_MARKER}项目组的夜间值班室位于三号楼一层，${"值班室钥匙由当班组长保管，".repeat(8)}凭工牌进入。`;
  const filler =
    "巡检班组在夜间进入车辆段，逐一记录道岔状态、信号机显示与接触网电压，并将异常条目同步到当班调度台。".repeat(
      19,
    );
  const documentText = `${lineA}\n${lineB}\n\n${filler}`;

  await page.goto(`/projects/${encodeURIComponent(project.slug)}/knowledge`);
  await createBaseThroughUI(page, "父子知识库");
  await page.getByRole("button", { name: "View documents" }).click();
  await setRerankerThroughUI(page, "Replay Knowledge Model · replay/reranker");
  await page.getByRole("button", { name: "Documents" }).click();

  // Upload in parent-child mode with an explicit child size and no overlap.
  await page.getByRole("button", { name: "Upload document" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("File").setInputFiles({
    name: "parent-child.txt",
    mimeType: "text/plain",
    buffer: Buffer.from(documentText, "utf-8"),
  });
  await dialog.getByRole("radio", { name: "Parent-child" }).check();
  await dialog.getByLabel("Chunk overlap (characters)").fill("0");
  await dialog.getByLabel("Child chunk size (characters)").fill("200");
  await dialog.getByRole("button", { name: "Upload", exact: true }).click();

  const rows = page.getByTestId("knowledge-document-rows");
  await expect(rows.getByText("Ready")).toBeVisible({ timeout: 60_000 });

  // The segment browser lists parents: both lines live in segment #1.
  await (await openDocumentActions(page, "parent-child.txt"))
    .getByRole("menuitem", { name: "View segments" })
    .click();
  const browser = page.getByTestId("knowledge-segment-browser");
  const list = browser.getByTestId("knowledge-segment-list");
  await expect(browser.getByText(/2 segments/u)).toBeVisible();
  const firstSegment = list.getByRole("listitem").first();
  await expect(firstSegment).toContainText(MARKER_PHONE);
  await expect(firstSegment).toContainText("夜间值班室位于三号楼一层");
  await browser.getByRole("button", { name: "Documents" }).click();

  // Search without explicit parameters: the base defaults (top_k 4) apply.
  // Cosine favors the filler children, rerank favors the marker parent; the
  // final order therefore proves the child→parent rollup fed the reranker.
  await page.getByRole("button", { name: "Retrieval test" }).click();
  await page.getByLabel("Query").fill("如何联系应急值班团队");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  const results = page.getByTestId("knowledge-search-results");
  const items = results.getByRole("listitem");
  await expect(items.first()).toContainText(MARKER_PHONE, {
    timeout: 30_000,
  });
  await expect(items.first()).toContainText("Retrieval score 0.950");
  // The citation returns the parent content: both child lines are present.
  await expect(items.first()).toContainText("夜间值班室位于三号楼一层");
  // Two marker children recalled, but their shared parent cites only once.
  await expect(results.getByText(MARKER_PHONE, { exact: false })).toHaveCount(
    1,
  );
  expect(await items.count()).toBe(2);

  // The finished search lands in the recent-queries log with its hit count.
  const recent = page.getByTestId("knowledge-recent-queries");
  await expect(recent.getByText("如何联系应急值班团队")).toBeVisible();
  await expect(recent.getByText("Retrieval test").first()).toBeVisible();

  // Saving a stricter base default caps the next defaults-driven search.
  await page.getByRole("button", { name: "Settings" }).click();
  await page.getByLabel("Default results (top_k)").fill("1");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("Saved.")).toBeVisible();

  await page.getByRole("button", { name: "Retrieval test" }).click();
  await expect(page.getByLabel("Results (top_k)")).toHaveAttribute(
    "placeholder",
    "1",
  );
  await page.getByLabel("Query").fill("如何联系应急值班团队");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(
    page.getByTestId("knowledge-search-results").getByRole("listitem").first(),
  ).toContainText(MARKER_PHONE, { timeout: 30_000 });
  await expect(
    page.getByTestId("knowledge-search-results").getByRole("listitem"),
  ).toHaveCount(1);
});

test("exhausted embedding retries fail the document and a retry after recovery reaches ready", async ({
  page,
  context,
}) => {
  test.setTimeout(180_000);
  const project = await registerReplayProject(context, APP);

  await page.goto(`/projects/${encodeURIComponent(project.slug)}/knowledge`);
  await createBaseThroughUI(page, "故障知识库");
  await page.getByRole("button", { name: "View documents" }).click();

  // Every embedding call fails until the fault is cleared, so all three task
  // attempts exhaust and the document parks as failed with the provider error.
  await setEmbeddingFailures(context, project, 1000);
  await uploadDocumentThroughUI(page, "flaky.txt", buildDocumentText());

  const rows = page.getByTestId("knowledge-document-rows");
  await expect(rows.getByText("Failed")).toBeVisible({ timeout: 90_000 });
  await expect(rows.getByText(/HTTP 500/)).toBeVisible();

  // Recovery: clear the fault, retry through the UI, reach ready for real.
  await setEmbeddingFailures(context, project, 0);
  await (await openDocumentActions(page, "flaky.txt"))
    .getByRole("menuitem", { name: "Retry" })
    .click();
  await expect(rows.getByText("Ready")).toBeVisible({ timeout: 60_000 });
});

test("metadata field values gate retrieval to the matching document", async ({
  page,
  context,
}) => {
  test.setTimeout(240_000);
  const project = await registerReplayProject(context, APP);

  await page.goto(`/projects/${encodeURIComponent(project.slug)}/knowledge`);
  await createBaseThroughUI(page, "元数据知识库");
  await page.getByRole("button", { name: "View documents" }).click();
  await setRerankerThroughUI(page, "Replay Knowledge Model · replay/reranker");
  await page.getByRole("button", { name: "Documents" }).click();

  // Two byte-identical documents: only metadata can tell them apart, so a
  // narrowed result proves the JSONB predicate ran inside real recall SQL.
  const rows = page.getByTestId("knowledge-document-rows");
  await uploadDocumentThroughUI(page, "safety.txt", buildDocumentText());
  await expect(rows.getByText("Ready")).toBeVisible({ timeout: 60_000 });
  await uploadDocumentThroughUI(page, "ops.txt", buildDocumentText());
  await expect(rows.getByText("Ready")).toHaveCount(2, { timeout: 60_000 });

  // Define a string field in the metadata panel.
  const nav = page.getByRole("navigation", { name: "Knowledge base sections" });
  await nav.getByRole("button", { name: "Metadata" }).click();
  await page.getByRole("button", { name: "Add field" }).click();
  await page.getByRole("dialog").getByLabel("Field name").fill("dept");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Create", exact: true })
    .click();
  await expect(
    page.getByTestId("knowledge-metadata-field-rows").getByText("dept"),
  ).toBeVisible();

  // Assign a different value to each document.
  await nav.getByRole("button", { name: "Documents" }).click();
  for (const [fileName, value] of [
    ["safety.txt", "安全部"],
    ["ops.txt", "运营部"],
  ] as const) {
    await (await openDocumentActions(page, fileName))
      .getByRole("menuitem", { name: "Metadata" })
      .click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel(/dept/u).fill(value);
    await dialog.getByRole("button", { name: "Save", exact: true }).click();
    await expect(dialog).toHaveCount(0);
  }

  // Without filters both documents surface at the marker rerank score.
  await nav.getByRole("button", { name: "Retrieval test" }).click();
  await page.getByLabel("Query").fill("如何联系应急值班团队");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  const results = page.getByTestId("knowledge-search-results");
  await expect(
    results.getByText(MARKER_PHONE, { exact: false }).first(),
  ).toBeVisible({ timeout: 30_000 });
  await expect(results.getByText("safety.txt").first()).toBeVisible();
  await expect(results.getByText("ops.txt").first()).toBeVisible();

  // The equals condition drops the other document from recall entirely.
  await page.getByRole("button", { name: "Add condition" }).click();
  await page.getByLabel("Condition 1 value").fill("安全部");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(results.getByText("ops.txt")).toHaveCount(0, {
    timeout: 30_000,
  });
  await expect(results.getByText("safety.txt").first()).toBeVisible();
  await expect(
    results.getByText(MARKER_PHONE, { exact: false }).first(),
  ).toBeVisible();
});

test("rebuild re-embeds the documents and retrieval serves the new version", async ({
  page,
  context,
}) => {
  test.setTimeout(240_000);
  const project = await registerReplayProject(context, APP);

  await page.goto(`/projects/${encodeURIComponent(project.slug)}/knowledge`);
  await createBaseThroughUI(page, "重建知识库");
  await page.getByRole("button", { name: "View documents" }).click();
  await setRerankerThroughUI(page, "Replay Knowledge Model · replay/reranker");
  await page.getByRole("button", { name: "Documents" }).click();
  await uploadDocumentThroughUI(page, "rebuild.txt", buildDocumentText());
  const rows = page.getByTestId("knowledge-document-rows");
  await expect(rows.getByText("Ready")).toBeVisible({ timeout: 60_000 });

  const before = await providerCounters(context);

  // The settings rebuild block confirms and requeues every document. The
  // selector keeps the current configuration: a plain re-embed.
  await page.getByRole("button", { name: "Settings" }).click();
  const rebuildSection = page.getByRole("region", { name: "Embedding model" });
  await rebuildSection
    .getByRole("button", { name: "Rebuild embeddings" })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Rebuild", exact: true })
    .click();
  await expect(
    page.getByText("Rebuild started", { exact: false }),
  ).toBeVisible();

  // The real Worker re-embeds: provider embedding calls must grow.
  await expect
    .poll(async () => (await providerCounters(context)).embedding_calls, {
      timeout: 60_000,
    })
    .toBeGreaterThan(before.embedding_calls);

  // The requeued document walks back to ready.
  const nav = page.getByRole("navigation", { name: "Knowledge base sections" });
  await nav.getByRole("button", { name: "Documents" }).click();
  await expect(rows.getByText("Ready")).toBeVisible({ timeout: 60_000 });

  // Old-version segments are excluded from recall, so a hit here proves the
  // re-ingested segments serve retrieval end to end.
  await nav.getByRole("button", { name: "Retrieval test" }).click();
  await page.getByLabel("Query").fill("如何联系应急值班团队");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(
    page
      .getByTestId("knowledge-search-results")
      .getByText(MARKER_PHONE, { exact: false })
      .first(),
  ).toBeVisible({ timeout: 30_000 });
});
