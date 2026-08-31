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

async function setRerankFailures(
  context: BrowserContext,
  project: ReplayProjectScope,
  failures: number,
): Promise<void> {
  const response = await context.request.post(
    `${APP}/api/test-only/replay-knowledge/provider/faults`,
    {
      headers: { "X-CSRF-Token": project.csrf },
      data: { rerank_failures: failures },
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
  await browser.getByRole("button", { name: "Documents", exact: true }).click();
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
  // Without a reranker the score provenance is the cosine domain.
  await expect(items.first()).toContainText("Cosine");
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
  // The provenance badge flips with the pipeline: rerank now owns the score.
  await expect(items.first()).toContainText("Rerank");
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
  await page.getByRole("button", { name: "Documents", exact: true }).click();
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
  await page.getByRole("button", { name: "Documents", exact: true }).click();
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
  await page.getByRole("button", { name: "Documents", exact: true }).click();
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
  await page.getByRole("button", { name: "Documents", exact: true }).click();
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
  // A decoy first file proves the picker previews the *chosen* file, not
  // just whichever file happens to be first in the selection.
  const decoyText = "封面页只有一句与预览无关的话。";

  await page.goto(`/projects/${encodeURIComponent(project.slug)}/knowledge`);
  // "New base" opens the document-first wizard.
  await page.getByRole("button", { name: "New base" }).click();
  await page.getByLabel("File").setInputFiles([
    {
      name: "cover.txt",
      mimeType: "text/plain",
      buffer: Buffer.from(decoyText, "utf-8"),
    },
    {
      name: "preview.txt",
      mimeType: "text/plain",
      buffer: Buffer.from(documentText, "utf-8"),
    },
  ]);
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

  // Parameter edits do not re-upload the full file. Picking the second file
  // in the preview picker switches the preview target and auto-previews it
  // once with the current parameters — the decoy content must not leak in.
  const previewPanel = page.getByTestId("chunk-preview-panel");
  await expect(
    previewPanel.getByText(
      "Preview is out of date. Refresh to apply the current settings.",
    ),
  ).toBeVisible();
  await previewPanel.getByLabel("Preview file").click();
  await page.getByRole("option", { name: "preview.txt" }).click();
  const chunkParagraphs = previewPanel.locator("ul li p:last-child");
  let previewChunks: string[] = [];
  await expect
    .poll(
      async () => {
        previewChunks = await chunkParagraphs.allTextContents();
        return (
          previewChunks.length >= 3 &&
          previewChunks.every((text) => !text.includes("https://")) &&
          previewChunks.every((text) => !/ {2}/.test(text)) &&
          previewChunks.every((text) => !text.includes(decoyText))
        );
      },
      { timeout: 30_000 },
    )
    .toBe(true);
  await expect(
    previewPanel.getByText(/Showing \d+ of \d+ chunks/),
  ).toBeVisible();

  // The real Worker ingests both files with the same frozen parameters.
  await page.getByRole("button", { name: "Save & process" }).click();
  await expect(page.getByText("Knowledge base created")).toBeVisible();
  const statusList = page.getByTestId("wizard-document-status");
  await expect(statusList.getByText("Ready")).toHaveCount(2, {
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
  await page.getByRole("button", { name: "Documents", exact: true }).click();

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
  await browser.getByRole("button", { name: "Documents", exact: true }).click();

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

  // The reranked hit carries its provenance badge on the result row.
  await expect(items.first()).toContainText("Rerank");

  // Hit detail pins the full parent: both complete marker lines are present,
  // including the tail of line B that a snippet would have cut off.
  await items
    .first()
    .getByRole("button", { name: /View segment #\d+ in full/u })
    .click();
  const detail = page.getByTestId("knowledge-hit-detail");
  const detailContent = detail.getByTestId("knowledge-detail-content");
  await expect(detailContent).toContainText(MARKER_PHONE);
  await expect(detailContent).toContainText("凭工牌进入");

  // Both children truly participated in recall (marker-axis embeddings), so
  // the matched flag must cover the second child too — it cannot be inferred
  // from list order, only from the per-hit matched_children evidence.
  const detailChildren = detail.getByTestId("knowledge-detail-children");
  await expect(detailChildren.getByRole("listitem")).toHaveCount(2);
  await expect(detailChildren.getByRole("listitem").nth(0)).toContainText(
    "Matched",
  );
  await expect(detailChildren.getByRole("listitem").nth(1)).toContainText(
    "Matched",
  );
  await expect(
    detail.getByLabel("Child chunks matched in this search"),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(detail).toBeHidden();

  // Debug diagnostics expose counts and models but never segment text.
  const diagnostics = page.getByTestId("knowledge-search-diagnostics");
  await diagnostics.locator("summary").click();
  await expect(diagnostics).toContainText("Semantic candidates");
  await expect(diagnostics).not.toContainText(MARKER_PHONE);
  await expect(diagnostics).not.toContainText("夜间值班室");
  await diagnostics.locator("summary").click();

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

  // Editing the cited segment behind the page's back moves its content
  // digest; reopening the pinned detail must surface a conflict, not silently
  // show different content than the one that was scored.
  const baseId = new URL(page.url()).searchParams.get("kb");
  expect(baseId).toBeTruthy();
  const documentsResponse = await context.request.get(
    `${APP}/api/projects/${project.id}/knowledge/bases/${baseId}/documents?page=1&page_size=50`,
  );
  expect(documentsResponse.status(), await documentsResponse.text()).toBe(200);
  const documentsBody = (await documentsResponse.json()) as {
    items: Array<{ id: string; name: string }>;
  };
  const documentId = documentsBody.items.find(
    (item) => item.name === "parent-child.txt",
  )?.id;
  expect(documentId).toBeTruthy();
  const segmentsResponse = await context.request.get(
    `${APP}/api/projects/${project.id}/knowledge/documents/${documentId}/segments?page=1&page_size=10`,
  );
  expect(segmentsResponse.status(), await segmentsResponse.text()).toBe(200);
  const segmentsBody = (await segmentsResponse.json()) as {
    items: Array<{ id: string; position: number; content: string }>;
  };
  const markerSegment = segmentsBody.items.find((item) =>
    item.content.includes(MARKER_PHONE),
  );
  expect(markerSegment).toBeTruthy();
  const patchResponse = await context.request.patch(
    `${APP}/api/projects/${project.id}/knowledge/segments/${markerSegment!.id}`,
    {
      headers: { "X-CSRF-Token": project.csrf },
      data: { content: `${markerSegment!.content}\n补充：值班表每周一更新。` },
    },
  );
  expect(patchResponse.status(), await patchResponse.text()).toBe(200);

  await page
    .getByTestId("knowledge-search-results")
    .getByRole("listitem")
    .first()
    .getByRole("button", { name: /View segment #\d+ in full/u })
    .click();
  const staleDetail = page.getByTestId("knowledge-hit-detail");
  await expect(
    staleDetail.getByTestId("knowledge-detail-conflict"),
  ).toBeVisible({ timeout: 15_000 });
  await expect(
    staleDetail.getByTestId("knowledge-detail-conflict"),
  ).toContainText("Run the search again");
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
  await expect(rows.getByText("Failed", { exact: true })).toBeVisible({
    timeout: 90_000,
  });
  await expect(rows.getByText(/HTTP 500/)).toBeVisible();

  // The status cell carries the real task evidence projected from the worker:
  // the failing stage and the exhausted attempt counter, not a bare badge.
  const progress = rows.getByTestId("knowledge-task-progress");
  await expect(progress).toContainText("Failed during Embedding");
  await expect(progress).toContainText("Attempt 3/3");

  // Recovery: clear the fault, retry through the UI, reach ready for real.
  await setEmbeddingFailures(context, project, 0);
  await (await openDocumentActions(page, "flaky.txt"))
    .getByRole("menuitem", { name: "Retry" })
    .click();
  await expect(rows.getByText("Ready")).toBeVisible({ timeout: 60_000 });
});

test("a reranker outage keeps the search error visible until a retry succeeds", async ({
  page,
  context,
}) => {
  test.setTimeout(180_000);
  const project = await registerReplayProject(context, APP);

  await page.goto(`/projects/${encodeURIComponent(project.slug)}/knowledge`);
  await createBaseThroughUI(page, "故障检索知识库");
  await page.getByRole("button", { name: "View documents" }).click();
  await setRerankerThroughUI(page, "Replay Knowledge Model · replay/reranker");
  await page.getByRole("button", { name: "Documents", exact: true }).click();
  await uploadDocumentThroughUI(page, "outage.txt", buildDocumentText());
  const rows = page.getByTestId("knowledge-document-rows");
  await expect(rows.getByText("Ready")).toBeVisible({ timeout: 60_000 });

  // Every rerank call fails: the search must land in a persistent, visible
  // error — never an empty state that could pass for "no hits".
  await setRerankFailures(context, project, 1000);
  await page.getByRole("button", { name: "Retrieval test" }).click();
  await page.getByLabel("Query").fill("如何联系应急值班团队");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  const searchError = page.getByTestId("knowledge-search-error");
  await expect(searchError).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId("knowledge-search-results")).toHaveCount(0);
  await expect(page.getByTestId("knowledge-search-empty")).toHaveCount(0);

  // Clearing the fault and retrying re-runs the same query to success.
  await setRerankFailures(context, project, 0);
  await searchError.getByRole("button", { name: "Retry" }).click();
  await expect(
    page
      .getByTestId("knowledge-search-results")
      .getByText(MARKER_PHONE, { exact: false })
      .first(),
  ).toBeVisible({ timeout: 30_000 });
  await expect(searchError).toHaveCount(0);
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
  await page.getByRole("button", { name: "Documents", exact: true }).click();

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
  await nav.getByRole("button", { name: "Documents", exact: true }).click();
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

  // Batch metadata: the mixed dept values surface as distinct, one shared
  // set patches both rows, and the filter proves the write reached recall.
  await nav.getByRole("button", { name: "Documents", exact: true }).click();
  await page.getByLabel("Select all documents").check();
  await page.getByRole("button", { name: "Edit metadata" }).click();
  const batchDialog = page.getByRole("dialog");
  await expect(
    batchDialog.getByText("Batch metadata (2 documents)"),
  ).toBeVisible();
  const deptField = batchDialog
    .getByTestId("knowledge-batch-field")
    .filter({ hasText: "dept" });
  await expect(deptField).toContainText("2 distinct values");
  await deptField.getByLabel("dept mode").click();
  await page.getByRole("option", { name: "Set" }).click();
  await deptField.getByLabel("dept value").fill("联合部");
  await batchDialog.getByRole("button", { name: "Save" }).click();
  await expect(batchDialog).toBeHidden();

  // The same equals filter now matches both documents on the shared value.
  await nav.getByRole("button", { name: "Retrieval test" }).click();
  await page.getByLabel("Query").fill("如何联系应急值班团队");
  await page.getByRole("button", { name: "Add condition" }).click();
  await page.getByLabel("Condition 1 value").fill("联合部");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(results.getByText("safety.txt").first()).toBeVisible({
    timeout: 30_000,
  });
  await expect(results.getByText("ops.txt").first()).toBeVisible();
});

test("re-embedding keeps segment identity, edits, and disables; reparse replaces them", async ({
  page,
  context,
}) => {
  test.setTimeout(300_000);
  const project = await registerReplayProject(context, APP);

  await page.goto(`/projects/${encodeURIComponent(project.slug)}/knowledge`);
  await createBaseThroughUI(page, "重建知识库");
  await page.getByRole("button", { name: "View documents" }).click();
  await setRerankerThroughUI(page, "Replay Knowledge Model · replay/reranker");
  await page.getByRole("button", { name: "Documents", exact: true }).click();
  await uploadDocumentThroughUI(page, "rebuild.txt", buildDocumentText());
  const rows = page.getByTestId("knowledge-document-rows");
  await expect(rows.getByText("Ready")).toBeVisible({ timeout: 60_000 });

  // Snapshot the published rows straight from the API: UUIDs, contents, and
  // enabled states are the identity that re-embedding must preserve.
  const baseId = new URL(page.url()).searchParams.get("kb");
  expect(baseId).toBeTruthy();
  const listSegments = async (documentId: string) => {
    const response = await context.request.get(
      `${APP}/api/projects/${project.id}/knowledge/documents/${documentId}/segments?page=1&page_size=50`,
    );
    expect(response.status(), await response.text()).toBe(200);
    return (await response.json()) as {
      items: Array<{
        id: string;
        position: number;
        content: string;
        enabled: boolean;
      }>;
    };
  };
  const documentsResponse = await context.request.get(
    `${APP}/api/projects/${project.id}/knowledge/bases/${baseId}/documents?page=1&page_size=50`,
  );
  expect(documentsResponse.status(), await documentsResponse.text()).toBe(200);
  const documentId = (
    (await documentsResponse.json()) as {
      items: Array<{ id: string; name: string }>;
    }
  ).items.find((item) => item.name === "rebuild.txt")?.id;
  expect(documentId).toBeTruthy();

  const original = (await listSegments(documentId!)).items;
  expect(original.length).toBeGreaterThanOrEqual(2);

  // Manual governance before re-embedding: edit one segment (the marker makes
  // it rerank-visible) and disable the marker-bearing first segment. Both
  // must survive the re-embed; only a reparse may undo them.
  const editedContent = `${RERANK_MARKER}重嵌入保持验证的专用编号是 EMG-3088。`;
  const editResponse = await context.request.patch(
    `${APP}/api/projects/${project.id}/knowledge/segments/${original[1]!.id}`,
    {
      headers: { "X-CSRF-Token": project.csrf },
      data: { content: editedContent },
    },
  );
  expect(editResponse.status(), await editResponse.text()).toBe(200);
  const disableResponse = await context.request.patch(
    `${APP}/api/projects/${project.id}/knowledge/segments/${original[0]!.id}`,
    {
      headers: { "X-CSRF-Token": project.csrf },
      data: { enabled: false },
    },
  );
  expect(disableResponse.status(), await disableResponse.text()).toBe(200);

  const before = await providerCounters(context);

  // The settings re-embed block confirms and requeues the published document.
  // The selector keeps the current configuration: a plain re-embed.
  await page.getByRole("button", { name: "Settings" }).click();
  const rebuildSection = page.getByRole("region", { name: "Embedding model" });
  await rebuildSection
    .getByRole("button", { name: "Re-embed documents" })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Re-embed", exact: true })
    .click();
  // The admission outcome reports the real accepted count (nothing skipped).
  await expect(page.getByTestId("knowledge-rebuild-outcome")).toHaveText(
    "Re-embedding accepted for 1 documents.",
  );

  // The real Worker re-embeds: provider embedding calls must grow.
  await expect
    .poll(async () => (await providerCounters(context)).embedding_calls, {
      timeout: 60_000,
    })
    .toBeGreaterThan(before.embedding_calls);

  // The requeued document walks back to ready.
  const nav = page.getByRole("navigation", { name: "Knowledge base sections" });
  await nav.getByRole("button", { name: "Documents", exact: true }).click();
  await expect(rows.getByText("Ready")).toBeVisible({ timeout: 60_000 });

  // Identity check: same UUIDs at the same positions, contents preserved
  // (manual edit included), and the disable is still in force.
  const reembedded = (await listSegments(documentId!)).items;
  expect(reembedded.map((item) => item.id)).toEqual(
    original.map((item) => item.id),
  );
  expect(reembedded[1]!.content).toBe(editedContent);
  expect(reembedded[0]!.content).toBe(original[0]!.content);
  expect(reembedded.map((item) => item.enabled)).toEqual(
    original.map((item, index) => (index === 0 ? false : item.enabled)),
  );

  // The re-embedded vectors serve retrieval: the edited segment still hits.
  await nav.getByRole("button", { name: "Retrieval test" }).click();
  await page.getByLabel("Query").fill("重嵌入保持验证的专用编号");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(
    page
      .getByTestId("knowledge-search-results")
      .getByText("EMG-3088", { exact: false })
      .first(),
  ).toBeVisible({ timeout: 30_000 });

  // Reparse re-splits the stored original file: the dialog previews the split
  // with the edited parameters before anything is committed.
  await nav.getByRole("button", { name: "Documents", exact: true }).click();
  await (await openDocumentActions(page, "rebuild.txt"))
    .getByRole("menuitem", { name: "Reparse from original" })
    .click();
  const reparseDialog = page.getByRole("dialog");
  await reparseDialog.getByLabel("Chunk size (characters)").fill("500");
  await reparseDialog
    .getByRole("button", { name: "Preview split", exact: true })
    .click();
  await expect(
    reparseDialog.getByText(/Showing \d+ of \d+ chunks/u),
  ).toBeVisible({ timeout: 30_000 });
  await reparseDialog
    .getByRole("button", { name: "Reparse", exact: true })
    .click();
  await expect(reparseDialog).toBeHidden();
  await expect(rows.getByText("Ready")).toBeVisible({ timeout: 60_000 });

  // Replacement check: brand-new rows from the original file. The manual edit
  // is gone, the disable is reset, and the marker paragraph is restored.
  const reparsed = (await listSegments(documentId!)).items;
  const originalIds = new Set(original.map((item) => item.id));
  expect(reparsed.length).toBeGreaterThan(0);
  for (const item of reparsed) {
    expect(originalIds.has(item.id)).toBe(false);
    expect(item.enabled).toBe(true);
    expect(item.content).not.toContain("EMG-3088");
  }
  expect(
    reparsed.some((item) => item.content.includes(MARKER_PHONE)),
  ).toBe(true);

  // Retrieval serves only the reparsed generation: the marker paragraph hits
  // again (proving the reparse also reset the manual disable) while the
  // overwritten manual edit is gone for good.
  await nav.getByRole("button", { name: "Retrieval test" }).click();
  await page.getByLabel("Query").fill("如何联系应急值班团队");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  const finalResults = page.getByTestId("knowledge-search-results");
  await expect(
    finalResults.getByText(MARKER_PHONE, { exact: false }).first(),
  ).toBeVisible({ timeout: 30_000 });
  await expect(finalResults.getByText("EMG-3088")).toHaveCount(0);

  // The hit detail locates into the maintenance view: the URL carries the
  // document and segment, and the locate panel pins the same real content.
  await finalResults
    .getByRole("listitem")
    .first()
    .getByRole("button", { name: /View segment #\d+ in full/u })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Open in documents" })
    .click();
  await expect(page).toHaveURL(/doc=/u);
  await expect(page).toHaveURL(/segment=/u);
  await expect(page.getByTestId("knowledge-segment-locate")).toContainText(
    MARKER_PHONE,
    { timeout: 15_000 },
  );
});
