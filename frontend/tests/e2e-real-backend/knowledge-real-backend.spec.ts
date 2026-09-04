import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, test, type BrowserContext, type Page } from "@playwright/test";

import {
  registerReplayProject,
  type ReplayProjectScope,
} from "./project-fixture";

const APP =
  process.env.E2E_APP_URL ??
  `http://localhost:${process.env.E2E_FRONTEND_PORT ?? "3000"}`;
const PARSING_FIXTURES = resolve(
  process.cwd(),
  "tests/fixtures/knowledge-parsing",
);

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

type ProjectObjectInventory = {
  count: number;
  fingerprint: string;
  originals: number;
  manifests: number;
  assets: number;
};

async function projectObjectInventory(
  context: BrowserContext,
  project: ReplayProjectScope,
): Promise<ProjectObjectInventory> {
  const response = await context.request.get(
    `${APP}/api/test-only/replay-knowledge/objects`,
  );
  expect(response.status(), "object inventory probe failed").toBe(200);
  const body = (await response.json()) as { keys: string[] };
  const keys = body.keys.filter((key) => key.includes(project.id)).sort();
  const assets = keys.filter((key) => key.includes("/assets/")).length;
  const manifests = keys.filter((key) => key.endsWith("/manifest.json")).length;
  return {
    count: keys.length,
    fingerprint: createHash("sha256")
      .update(keys.join("\u0000"), "utf8")
      .digest("hex"),
    originals: keys.length - manifests - assets,
    manifests,
    assets,
  };
}

async function expectNoNewObjectsDuring(
  page: Page,
  project: ReplayProjectScope,
  action: () => Promise<void>,
): Promise<void> {
  const before = await projectObjectInventory(page.context(), project);
  await action();
  const after = await projectObjectInventory(page.context(), project);
  expect(after.count, "preview changed the Project object count").toBe(
    before.count,
  );
  expect(
    after.fingerprint === before.fingerprint,
    "preview changed the Project object inventory",
  ).toBe(true);
}

async function storageEvidence(context: BrowserContext): Promise<{
  source_reads: number;
  manifest_reads: number;
  attachment_reads: number;
}> {
  const response = await context.request.get(
    `${APP}/api/test-only/replay-knowledge/storage`,
  );
  expect(response.status(), "unexpected HTTP status").toBe(200);
  return (await response.json()) as {
    source_reads: number;
    manifest_reads: number;
    attachment_reads: number;
  };
}

type ReplayProjectFacts = {
  object_count: number;
  document_rows: number;
  published_documents: number;
  extraction_rows: number;
  ready_attachments: number;
  open_tasks: number;
  running_tasks: number;
  quota_used: number;
  quota_reserved: number;
};

async function projectFacts(
  context: BrowserContext,
  project: ReplayProjectScope,
): Promise<ReplayProjectFacts> {
  const response = await context.request.get(
    `${APP}/api/test-only/replay-knowledge/projects/${project.id}/facts`,
  );
  expect(response.status(), "unexpected HTTP status").toBe(200);
  return (await response.json()) as ReplayProjectFacts;
}

async function setStorageFaults(
  context: BrowserContext,
  project: ReplayProjectScope,
  faults: Partial<{
    source_put_failures: number;
    manifest_put_failures: number;
    attachment_put_failures: number;
    delete_failures: number;
  }>,
): Promise<void> {
  const response = await context.request.post(
    `${APP}/api/test-only/replay-knowledge/storage/faults`,
    {
      headers: { "X-CSRF-Token": project.csrf },
      data: {
        source_put_failures: 0,
        manifest_put_failures: 0,
        attachment_put_failures: 0,
        delete_failures: 0,
        ...faults,
      },
    },
  );
  expect(response.status(), "unexpected HTTP status").toBe(200);
}

async function setProjectRevoked(
  context: BrowserContext,
  project: ReplayProjectScope,
  revoked: boolean,
): Promise<void> {
  const response = await context.request.post(
    `${APP}/api/test-only/replay-knowledge/projects/${project.id}/authority`,
    {
      headers: { "X-CSRF-Token": project.csrf },
      data: { revoked },
    },
  );
  expect(response.status(), "unexpected HTTP status").toBe(200);
}

async function createAdditionalProject(
  context: BrowserContext,
  project: ReplayProjectScope,
): Promise<string> {
  const suffix = `${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
  const response = await context.request.post(`${APP}/api/projects`, {
    headers: { "X-CSRF-Token": project.csrf },
    data: {
      slug: `replay-extra-${suffix}`,
      display_name: "Additional replay project",
    },
  });
  expect(response.status(), "unexpected HTTP status").toBe(201);
  const body = (await response.json()) as { id?: unknown };
  expect(typeof body.id).toBe("string");
  if (typeof body.id !== "string") {
    throw new Error("additional replay Project response is invalid");
  }
  return body.id;
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
  expect(response.status(), "unexpected HTTP status").toBe(200);
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
  expect(response.status(), "unexpected HTTP status").toBe(200);
}

async function setSummaryFailures(
  context: BrowserContext,
  project: ReplayProjectScope,
  failures: number,
): Promise<void> {
  const response = await context.request.post(
    `${APP}/api/test-only/replay-knowledge/provider/faults`,
    {
      headers: { "X-CSRF-Token": project.csrf },
      data: { chat_failures: failures },
    },
  );
  expect(response.status(), "unexpected HTTP status").toBe(200);
}

async function providerCounters(context: BrowserContext): Promise<{
  embedding_calls: number;
  rerank_calls: number;
  chat_calls: number;
  embedding_failures_remaining: number;
  embedding_blocked: boolean;
  embedding_waiters: number;
}> {
  const response = await context.request.get(
    `${APP}/api/test-only/replay-knowledge/provider`,
  );
  expect(response.status(), "unexpected HTTP status").toBe(200);
  return (await response.json()) as {
    embedding_calls: number;
    rerank_calls: number;
    chat_calls: number;
    embedding_failures_remaining: number;
    embedding_blocked: boolean;
    embedding_waiters: number;
  };
}

async function setEmbeddingBlocked(
  context: BrowserContext,
  project: ReplayProjectScope,
  blocked: boolean,
): Promise<void> {
  const response = await context.request.post(
    `${APP}/api/test-only/replay-knowledge/provider/faults`,
    {
      headers: { "X-CSRF-Token": project.csrf },
      data: { embedding_blocked: blocked },
    },
  );
  expect(response.status(), "unexpected HTTP status").toBe(200);
}

async function createBaseThroughUI(page: Page, name: string): Promise<void> {
  // "New base" opens the document-first wizard; acceptance drives the
  // empty-base dialog and uploads from the documents page instead.
  await page.getByRole("button", { name: "New base" }).click();
  await page.getByRole("button", { name: "Create an empty base" }).click();
  await page.getByLabel("Name").fill(name);
  await page.getByRole("button", { name: "Create", exact: true }).click();
  await expect(
    page.getByTestId("knowledge-base-list").getByText(name),
  ).toBeVisible();
  // Empty bases no longer bind a model in their create dialog. Configure
  // explicitly through Settings, then return to the list for callers.
  await page.getByRole("button", { name: "View documents" }).click();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByRole("button", { name: "Configure models" }).click();
  await page.getByLabel("Embedding model").click();
  await page
    .getByRole("option", { name: "Replay Knowledge Model · replay/embedding" })
    .click();
  await page.getByRole("button", { name: "Save configuration" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.getByRole("button", { name: "Back", exact: true }).click();
  await expect(page).toHaveURL(/\/knowledge$/u);
  await expect(
    page
      .getByRole("region", { name: "Knowledge bases", exact: true })
      .getByText(name),
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
  await page.getByRole("button", { name: "Next", exact: true }).click();
  await page
    .getByRole("button", { name: "Upload & process", exact: true })
    .click();
  await page.getByRole("button", { name: "Go to documents" }).click();
}

async function selectFixtureForUpload(
  page: Page,
  project: ReplayProjectScope,
  fileName: string,
): Promise<void> {
  await page.getByRole("button", { name: "Upload document" }).click();
  await page
    .getByLabel("File")
    .setInputFiles(resolve(PARSING_FIXTURES, fileName));
  await expectNoNewObjectsDuring(page, project, async () => {
    await page.getByRole("button", { name: "Next", exact: true }).click();
    await expect(
      page
        .getByTestId("chunk-preview-panel")
        .getByText(/Showing \d+ of \d+ chunks/u),
    ).toBeVisible({ timeout: 30_000 });
  });
}

async function submitSelectedFixture(page: Page): Promise<void> {
  await page
    .getByRole("button", { name: "Upload & process", exact: true })
    .click();
  await page.getByRole("button", { name: "Go to documents" }).click();
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
  // Text publication owns the original plus its extraction manifest.
  expect((await projectObjectInventory(context, project)).count).toBe(2);

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
    .poll(async () => (await projectObjectInventory(context, project)).count, {
      timeout: 30_000,
    })
    .toBe(0);

  // Base deletion also purges the objects of documents still inside it.
  await uploadDocumentThroughUI(page, "second.txt", buildDocumentText());
  await expect(rows.getByText("Ready")).toBeVisible({ timeout: 60_000 });
  expect((await projectObjectInventory(context, project)).count).toBe(2);

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
    .poll(async () => (await projectObjectInventory(context, project)).count, {
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

  // Let the automatic preview for the first file release the single replay
  // parser slot before changing parameters and selecting the target file.
  const previewPanel = page.getByTestId("chunk-preview-panel");
  await expect(previewPanel.getByText(/Showing \d+ of \d+ chunks/)).toBeVisible(
    { timeout: 30_000 },
  );

  // Tune every K2 parameter away from its default.
  await page.getByLabel("Chunk size (Knowledge Tokens)").fill("200");
  await page.getByLabel("Chunk overlap (Knowledge Tokens)").fill("0");
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
  await expect(
    previewPanel.getByText(
      "Preview is out of date. Refresh to apply the current settings.",
    ),
  ).toBeVisible();
  const chunkParagraphs = previewPanel.locator("ul li p:last-child");
  let previewChunks: string[] = [];
  const beforePreviewReads = await storageEvidence(context);
  await expectNoNewObjectsDuring(page, project, async () => {
    await previewPanel.getByLabel("Preview file").click();
    await page.getByRole("option", { name: "preview.txt" }).click();
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
  });
  expect(await storageEvidence(context)).toEqual(beforePreviewReads);
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

test("a preview fingerprint cannot authorize different same-name bytes", async ({
  page,
  context,
}) => {
  test.setTimeout(180_000);
  const project = await registerReplayProject(context, APP);
  await page.goto(`/projects/${encodeURIComponent(project.slug)}/knowledge`);
  await createBaseThroughUI(page, "文件身份知识库");
  await page.getByRole("button", { name: "View documents" }).click();
  await expect(page).toHaveURL(/[?&]kb=/u);
  const baseId = new URL(page.url()).searchParams.get("kb");
  expect(baseId).toBeTruthy();
  const profile = {
    unit: "token",
    mode: "general",
    size: 1000,
    overlap: 100,
    separator: "\\n\\n",
    child_size: 500,
    child_separator: "\\n",
    remove_extra_spaces: false,
    remove_urls_emails: false,
    header_rules: [],
  };
  let fingerprint = "";
  await expectNoNewObjectsDuring(page, project, async () => {
    const preview = await context.request.post(
      `${APP}/api/projects/${project.id}/knowledge/chunk-preview`,
      {
        headers: { "X-CSRF-Token": project.csrf },
        multipart: {
          file: {
            name: "same-name.txt",
            mimeType: "text/plain",
            buffer: Buffer.from("文件 A 的确定正文", "utf8"),
          },
          processing_profile: JSON.stringify(profile),
        },
      },
    );
    expect(preview.status(), "unexpected HTTP status").toBe(200);
    fingerprint = ((await preview.json()) as { preview_fingerprint: string })
      .preview_fingerprint;
    expect(fingerprint).toMatch(/^[a-f0-9]{64}$/u);
  });
  const upload = await context.request.post(
    `${APP}/api/projects/${project.id}/knowledge/bases/${baseId}/documents`,
    {
      headers: { "X-CSRF-Token": project.csrf },
      multipart: {
        file: {
          name: "same-name.txt",
          mimeType: "text/plain",
          buffer: Buffer.from("文件 B 的不同确定正文", "utf8"),
        },
        processing_profile: JSON.stringify(profile),
        expected_preview_fingerprint: fingerprint,
      },
    },
  );
  expect(upload.status()).toBe(409);
  expect((await upload.json()) as object).toMatchObject({
    detail: { code: "KNOWLEDGE_CONFLICT" },
  });
  expect(await projectFacts(context, project)).toMatchObject({
    object_count: 0,
    document_rows: 0,
    quota_used: 0,
    quota_reserved: 0,
  });
});

test("the deterministic PDF and CSV fixtures preview and publish with source facts", async ({
  page,
  context,
}) => {
  test.setTimeout(300_000);
  const project = await registerReplayProject(context, APP);
  await page.goto(`/projects/${encodeURIComponent(project.slug)}/knowledge`);
  await createBaseThroughUI(page, "来源样例知识库");
  await page.getByRole("button", { name: "View documents" }).click();

  await selectFixtureForUpload(page, project, "two-pages.pdf");
  const pdfPreview = page.getByTestId("chunk-preview-panel");
  await expect(pdfPreview).toContainText("first page");
  await expect(pdfPreview).toContainText("second page");
  await submitSelectedFixture(page);
  const rows = page.getByTestId("knowledge-document-rows");
  const pdfRow = rows.getByRole("row").filter({ hasText: "two-pages.pdf" });
  await expect(pdfRow.getByText("Ready", { exact: true })).toBeVisible({
    timeout: 90_000,
  });
  await (await openDocumentActions(page, "two-pages.pdf"))
    .getByRole("menuitem", { name: "View segments" })
    .click();
  const pdfBrowser = page.getByTestId("knowledge-segment-browser");
  await expect(pdfBrowser).toContainText("first page");
  await expect(pdfBrowser).toContainText("second page");
  await expect(pdfBrowser).toContainText("Source · Page 1");
  await expect(pdfBrowser).toContainText("Source · Page 2");
  await pdfBrowser
    .getByRole("button", { name: "Documents", exact: true })
    .click();

  await selectFixtureForUpload(page, project, "inventory.csv");
  const csvPreview = page.getByTestId("chunk-preview-panel");
  const headers = csvPreview.getByTestId("knowledge-header-settings");
  await expect(headers).toContainText("Candidate row 2");
  await expect(headers).toContainText("设备");
  await expect(headers).toContainText("端口");
  await headers.getByRole("combobox", { name: "Header mode for CSV" }).click();
  await page.getByRole("option", { name: "Use row" }).click();
  await headers.getByLabel("Header row for CSV").fill("2");
  await expect(csvPreview).toContainText(
    "Preview is out of date. Refresh to apply the current settings.",
  );
  await csvPreview.getByRole("button", { name: "Refresh preview" }).click();
  await expect(headers).toContainText("Selected row 2");
  await expect(csvPreview).toContainText("设备: R1");
  await expect(csvPreview).toContainText("端口: Gi0/0");
  await submitSelectedFixture(page);
  const csvRow = rows.getByRole("row").filter({ hasText: "inventory.csv" });
  await expect(csvRow.getByText("Ready", { exact: true })).toBeVisible({
    timeout: 90_000,
  });
  await (await openDocumentActions(page, "inventory.csv"))
    .getByRole("menuitem", { name: "View segments" })
    .click();
  const csvBrowser = page.getByTestId("knowledge-segment-browser");
  await expect(csvBrowser).toContainText("设备: R1");
  await expect(csvBrowser).toContainText("端口: Gi0/0");
  await expect(csvBrowser).toContainText("Context · Row 2");
  await expect(csvBrowser).toContainText("Source · Row 3");
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
  await page.getByLabel("File").setInputFiles({
    name: "parent-child.txt",
    mimeType: "text/plain",
    buffer: Buffer.from(documentText, "utf-8"),
  });
  await page.getByRole("button", { name: "Next", exact: true }).click();
  await page.getByRole("radio", { name: "Parent-child" }).check();
  await page.getByLabel("Chunk overlap (Knowledge Tokens)").fill("0");
  await page.getByLabel("Child chunk size (Knowledge Tokens)").fill("200");
  await page
    .getByRole("button", { name: "Upload & process", exact: true })
    .click();
  await page.getByRole("button", { name: "Go to documents" }).click();

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

  // At least two marker-derived children truly participated in recall. Token
  // splitting may produce more children than the former character profile,
  // so assert the match evidence instead of the old exact child count.
  const detailChildren = detail.getByTestId("knowledge-detail-children");
  expect(
    await detailChildren.getByRole("listitem").count(),
  ).toBeGreaterThanOrEqual(2);
  expect(
    await detailChildren
      .getByRole("listitem")
      .filter({ hasText: "Matched" })
      .count(),
  ).toBeGreaterThanOrEqual(2);
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
  await page
    .getByRole("region", { name: "Settings", exact: true })
    .getByLabel("Top K")
    .fill("1");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("Saved.")).toBeVisible();

  await page.getByRole("button", { name: "Retrieval test" }).click();
  await expect(
    page.getByRole("region", { name: "Retrieval test" }).getByLabel("Top K"),
  ).toHaveAttribute("placeholder", "1");
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
  expect(documentsResponse.status(), "unexpected HTTP status").toBe(200);
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
  expect(segmentsResponse.status(), "unexpected HTTP status").toBe(200);
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
  expect(patchResponse.status(), "unexpected HTTP status").toBe(200);

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
    expect(response.status(), "unexpected HTTP status").toBe(200);
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
  expect(documentsResponse.status(), "unexpected HTTP status").toBe(200);
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
  expect(editResponse.status(), "unexpected HTTP status").toBe(200);
  const disableResponse = await context.request.patch(
    `${APP}/api/projects/${project.id}/knowledge/segments/${original[0]!.id}`,
    {
      headers: { "X-CSRF-Token": project.csrf },
      data: { enabled: false },
    },
  );
  expect(disableResponse.status(), "unexpected HTTP status").toBe(200);

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

  // Reparse re-splits the stored original file: the chunk settings page
  // previews the split with the edited parameters before anything is
  // committed.
  await nav.getByRole("button", { name: "Documents", exact: true }).click();
  await (await openDocumentActions(page, "rebuild.txt"))
    .getByRole("menuitem", { name: "Chunk settings" })
    .click();
  const chunkSettings = page.getByTestId("knowledge-document-chunk-settings");
  await chunkSettings.getByLabel("Chunk size (Knowledge Tokens)").fill("500");
  await chunkSettings
    .getByRole("button", { name: "Refresh preview", exact: true })
    .click();
  await expect(
    chunkSettings.getByText(/Showing \d+ of \d+ chunks/u),
  ).toBeVisible({ timeout: 30_000 });
  await chunkSettings
    .getByRole("button", { name: "Reparse", exact: true })
    .click();
  await expect(chunkSettings).toBeHidden();
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
  expect(reparsed.some((item) => item.content.includes(MARKER_PHONE))).toBe(
    true,
  );

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

test("summary indexing retries without hiding ready content, caches queries, and survives re-embed before reparse replaces it", async ({
  page,
  context,
}) => {
  test.setTimeout(300_000);
  const project = await registerReplayProject(context, APP);
  // The deterministic provider embeds every source chunk away from the query,
  // and generated summaries toward it. Repeating the marker keeps this true
  // across both the initial split and the later, smaller reparse split.
  const source =
    `${RERANK_MARKER}设备维护手册记录了跨班组应急联络方式、夜间故障升级路径与复核程序，值班人员必须核对原始运行记录后处理。`.repeat(
      30,
    );
  await page.goto(`/projects/${encodeURIComponent(project.slug)}/knowledge`);
  await createBaseThroughUI(page, "摘要索引验收");
  await page.getByRole("button", { name: "View documents" }).click();
  await expect(page).toHaveURL(/[?&]kb=/u);
  const baseId = new URL(page.url()).searchParams.get("kb");
  expect(baseId).toBeTruthy();
  const apiRoot = `${APP}/api/projects/${project.id}/knowledge`;
  const nav = page.getByRole("navigation", { name: "Knowledge base sections" });
  await nav.getByRole("button", { name: "Settings", exact: true }).click();
  const summarySwitch = page.getByRole("switch", {
    name: "Summary index",
    exact: true,
  });
  await expect(summarySwitch).toBeEnabled();
  await summarySwitch.click();
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(
    page.getByTestId("knowledge-summary-backfill-outcome"),
  ).toHaveText("Summary backfill: 0 queued, 0 skipped.");
  await nav.getByRole("button", { name: "Documents", exact: true }).click();

  const row = page
    .getByTestId("knowledge-document-rows")
    .getByRole("row")
    .filter({ hasText: "summary.txt" });
  // A permanent first-attempt failure gives the browser a stable task-progress
  // state to inspect without adding timing delays to the provider or Worker.
  await setSummaryFailures(context, project, 100);
  try {
    await uploadDocumentThroughUI(page, "summary.txt", source);
    await expect(row.getByText("Ready", { exact: true })).toBeVisible({
      timeout: 60_000,
    });
    await expect(row.getByTestId("knowledge-task-progress")).toContainText(
      "Generate summaries · Failed during Generating summaries",
      { timeout: 60_000 },
    );
  } finally {
    await setSummaryFailures(context, project, 0);
  }
  await (await openDocumentActions(page, "summary.txt"))
    .getByRole("menuitem", { name: "Retry", exact: true })
    .click();

  type DocumentState = {
    id: string;
    name: string;
    status: string;
    version: number;
    task_progress: { kind: string; status: string } | null;
  };
  type SegmentState = { id: string; content: string; document_version: number };
  type DetailState = {
    segment: SegmentState;
    summary: { content: string; created_at: string } | null;
    current_document_version: number;
  };
  const readDocument = async (): Promise<DocumentState> => {
    const response = await context.request.get(
      `${apiRoot}/bases/${baseId}/documents?page=1&page_size=50`,
    );
    expect(response.status(), "unexpected HTTP status").toBe(200);
    const body = (await response.json()) as { items: DocumentState[] };
    const document = body.items.find((item) => item.name === "summary.txt");
    expect(document).toBeDefined();
    return document!;
  };
  await expect
    .poll(async () => (await readDocument()).task_progress, { timeout: 60_000 })
    .toBeNull();
  const document = await readDocument();
  expect(document.status).toBe("ready");
  const listSegments = async (): Promise<SegmentState[]> => {
    const response = await context.request.get(
      `${apiRoot}/documents/${document.id}/segments?page=1&page_size=50`,
    );
    expect(response.status(), "unexpected HTTP status").toBe(200);
    return ((await response.json()) as { items: SegmentState[] }).items;
  };
  const detailUrl = (segmentId: string) =>
    `${apiRoot}/bases/${baseId}/documents/${document.id}/segments/${segmentId}`;
  const readDetail = async (segmentId: string): Promise<DetailState> => {
    const response = await context.request.get(detailUrl(segmentId));
    expect(response.status(), "unexpected HTTP status").toBe(200);
    return (await response.json()) as DetailState;
  };
  const originalSegments = await listSegments();
  expect(originalSegments.length).toBeGreaterThan(1);
  const originalDetails = await Promise.all(
    originalSegments.map((segment) => readDetail(segment.id)),
  );
  for (const detail of originalDetails) {
    expect(detail.summary?.content).toMatch(/^摘要索引回放 [a-f0-9]{16}$/u);
    expect(detail.segment.content).toContain(RERANK_MARKER);
    expect(detail.segment.content).not.toContain("摘要索引回放");
  }

  await nav.getByRole("button", { name: "Retrieval test" }).click();
  // Unique across scenarios so the first request is demonstrably cold.
  await page.getByLabel("Query").fill(`夜间维修故障复核 ${project.id}`);
  const search = async () => {
    const [response] = await Promise.all([
      page.waitForResponse(
        (result) =>
          result.url().endsWith(`/projects/${project.id}/knowledge/search`) &&
          result.request().method() === "POST",
      ),
      page.getByRole("button", { name: "Search", exact: true }).click(),
    ]);
    expect(response.status(), "unexpected HTTP status").toBe(200);
    return (await response.json()) as {
      citations: Array<{ segment_id: string; snippet: string; score: number }>;
      diagnostics: {
        counts: {
          summary_candidates: number;
          query_embedding_cache_hits: number;
          query_embedding_cache_misses: number;
        };
        hit_diagnostics: Array<{ segment_id: string; matched_via: string }>;
      };
    };
  };
  const beforeSearch = await providerCounters(context);
  const cold = await search();
  expect(cold.citations.length).toBeGreaterThan(0);
  expect(cold.diagnostics.counts).toMatchObject({
    query_embedding_cache_hits: 0,
    query_embedding_cache_misses: 1,
  });
  expect(cold.diagnostics.counts.summary_candidates).toBeGreaterThan(0);
  for (const hit of cold.diagnostics.hit_diagnostics) {
    expect(hit.matched_via).toBe("summary");
  }
  for (const citation of cold.citations) {
    expect(citation.snippet).toContain(RERANK_MARKER);
    expect(citation.snippet).not.toContain("摘要索引回放");
    expect(citation.score).toBeCloseTo(1);
  }
  const afterCold = await providerCounters(context);
  expect(afterCold.embedding_calls).toBe(beforeSearch.embedding_calls + 1);
  const warm = await search();
  expect(warm.diagnostics.counts).toMatchObject({
    query_embedding_cache_hits: 1,
    query_embedding_cache_misses: 0,
  });
  expect((await providerCounters(context)).embedding_calls).toBe(
    afterCold.embedding_calls,
  );

  const firstHit = page
    .getByTestId("knowledge-search-results")
    .getByRole("listitem")
    .first();
  await expect(firstHit.getByTestId("knowledge-hit-source")).toHaveText(
    "Summary",
  );
  await firstHit
    .getByRole("button", { name: /View segment #\d+ in full/u })
    .click();
  const detailDialog = page.getByRole("dialog");
  const firstDetail = originalDetails.find(
    (item) => item.segment.id === warm.citations[0]!.segment_id,
  )!;
  await expect(detailDialog.getByTestId("knowledge-detail-content")).toHaveText(
    firstDetail.segment.content,
  );
  await expect(
    detailDialog.getByTestId("knowledge-segment-summary"),
  ).toContainText(firstDetail.summary!.content);
  await expect(
    detailDialog.getByTestId("knowledge-segment-summary"),
  ).toContainText("System-generated summary");
  await page.keyboard.press("Escape");

  // Re-embedding changes vector generations only; every summary remains
  // byte-identical, with its timestamp intact and without another chat call.
  const beforeReembed = await providerCounters(context);
  await nav.getByRole("button", { name: "Settings", exact: true }).click();
  await page
    .getByRole("region", { name: "Embedding model" })
    .getByRole("button", { name: "Re-embed documents" })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Re-embed", exact: true })
    .click();
  await expect(page.getByTestId("knowledge-rebuild-outcome")).toHaveText(
    "Re-embedding accepted for 1 documents.",
  );
  await expect
    .poll(
      async () => {
        const current = await readDocument();
        return (
          current.version > document.version &&
          current.status === "ready" &&
          current.task_progress === null
        );
      },
      { timeout: 60_000 },
    )
    .toBe(true);
  const reembedded = await Promise.all(
    originalSegments.map((segment) => readDetail(segment.id)),
  );
  expect(reembedded.map((item) => item.summary)).toEqual(
    originalDetails.map((item) => item.summary),
  );
  expect(reembedded.map((item) => item.segment.content)).toEqual(
    originalSegments.map((item) => item.content),
  );
  const afterReembed = await providerCounters(context);
  expect(afterReembed.chat_calls).toBe(beforeReembed.chat_calls);
  expect(afterReembed.embedding_calls).toBeGreaterThan(
    beforeReembed.embedding_calls,
  );
  await nav.getByRole("button", { name: "Retrieval test" }).click();
  await page.getByLabel("Query").fill(`夜间维修故障复核 ${project.id}`);
  const afterReembedSearch = await search();
  expect(
    afterReembedSearch.diagnostics.hit_diagnostics.every(
      (hit) => hit.matched_via === "summary",
    ),
  ).toBe(true);
  expect(afterReembedSearch.citations.length).toBeGreaterThan(0);

  // Reparse is a new split of the original source. Its old segment details
  // disappear, while replacement segments receive freshly generated summaries.
  const reembeddedDocument = await readDocument();
  await nav.getByRole("button", { name: "Documents", exact: true }).click();
  await (await openDocumentActions(page, "summary.txt"))
    .getByRole("menuitem", { name: "Chunk settings" })
    .click();
  const chunkSettings = page.getByTestId("knowledge-document-chunk-settings");
  await chunkSettings.getByLabel("Chunk size (Knowledge Tokens)").fill("500");
  await chunkSettings
    .getByRole("button", { name: "Refresh preview", exact: true })
    .click();
  await expect(
    chunkSettings.getByText(/Showing \d+ of \d+ chunks/u),
  ).toBeVisible({ timeout: 30_000 });
  await chunkSettings
    .getByRole("button", { name: "Reparse", exact: true })
    .click();
  await expect(chunkSettings).toBeHidden();
  await expect
    .poll(
      async () => {
        const current = await readDocument();
        return (
          current.version > reembeddedDocument.version &&
          current.status === "ready" &&
          current.task_progress === null
        );
      },
      { timeout: 60_000 },
    )
    .toBe(true);
  const reparsedSegments = await listSegments();
  expect(reparsedSegments.length).toBeGreaterThan(originalSegments.length);
  const oldIds = new Set(originalSegments.map((item) => item.id));
  expect(reparsedSegments.every((item) => !oldIds.has(item.id))).toBe(true);
  const reparsedDetails = await Promise.all(
    reparsedSegments.map((item) => readDetail(item.id)),
  );
  expect(reparsedDetails.some((item) => item.summary !== null)).toBe(true);
  expect((await providerCounters(context)).chat_calls).toBeGreaterThan(
    afterReembed.chat_calls,
  );
  for (const previous of originalSegments) {
    expect((await context.request.get(detailUrl(previous.id))).status()).toBe(
      404,
    );
  }
});

test("the P1 image fixture keeps preview ephemeral, pins image reads, reprocesses without source reads, and deletes durably", async ({
  page,
  context,
}) => {
  test.setTimeout(420_000);
  const project = await registerReplayProject(context, APP);
  const apiRoot = `${APP}/api/projects/${project.id}/knowledge`;

  await page.goto(`/projects/${encodeURIComponent(project.slug)}/knowledge`);
  await createBaseThroughUI(page, "图片生命周期知识库");
  await page.getByRole("button", { name: "View documents" }).click();
  const beforeDocxPreviewReads = await storageEvidence(context);
  await selectFixtureForUpload(page, project, "document-with-image.docx");
  expect(await storageEvidence(context)).toEqual(beforeDocxPreviewReads);
  await expect(
    page.getByTestId("chunk-preview-panel").getByTestId("knowledge-image"),
  ).toHaveCount(1);
  expect((await projectObjectInventory(context, project)).count).toBe(0);
  await submitSelectedFixture(page);

  const rows = page.getByTestId("knowledge-document-rows");
  await expect(rows.getByText("Ready")).toBeVisible({ timeout: 90_000 });
  const baseId = new URL(page.url()).searchParams.get("kb");
  expect(baseId).toBeTruthy();

  type DocumentState = {
    id: string;
    name: string;
    status: string;
    version: number;
    delete_error: string | null;
    task_progress: { status: string } | null;
  };
  type SegmentState = {
    id: string;
    content: string;
    document_version: number;
  };
  type SegmentDetail = {
    segment: SegmentState;
    attachments: Array<{
      attachment_id: string;
      ref: string;
      media_type: "image/png" | "image/jpeg" | "image/webp";
    }>;
  };
  const readDocuments = async (): Promise<DocumentState[]> => {
    const response = await context.request.get(
      `${apiRoot}/bases/${baseId}/documents?page=1&page_size=50`,
    );
    expect(response.status(), "unexpected HTTP status").toBe(200);
    return ((await response.json()) as { items: DocumentState[] }).items;
  };
  const readDocument = async (): Promise<DocumentState> => {
    const item = (await readDocuments()).find(
      (candidate) => candidate.name === "document-with-image.docx",
    );
    expect(item).toBeDefined();
    return item!;
  };
  const listSegments = async (): Promise<SegmentState[]> => {
    const item = await readDocument();
    const response = await context.request.get(
      `${apiRoot}/documents/${item.id}/segments?page=1&page_size=50`,
    );
    expect(response.status(), "unexpected HTTP status").toBe(200);
    return ((await response.json()) as { items: SegmentState[] }).items;
  };
  const readDetail = async (segmentId: string): Promise<SegmentDetail> => {
    const item = await readDocument();
    const response = await context.request.get(
      `${apiRoot}/bases/${baseId}/documents/${item.id}/segments/${segmentId}`,
    );
    expect(response.status(), "unexpected HTTP status").toBe(200);
    return (await response.json()) as SegmentDetail;
  };

  const originalInventory = await projectObjectInventory(context, project);
  expect(originalInventory).toMatchObject({
    count: 3,
    originals: 1,
    manifests: 1,
    assets: 1,
  });
  const originalFacts = await projectFacts(context, project);
  expect(originalFacts).toMatchObject({
    object_count: 3,
    document_rows: 1,
    published_documents: 1,
    extraction_rows: 1,
    ready_attachments: 1,
    quota_reserved: 0,
  });
  expect(originalFacts.quota_used).toBeGreaterThan(0);

  const originalDetails = await Promise.all(
    (await listSegments()).map((segment) => readDetail(segment.id)),
  );
  const imageDetail = originalDetails.find(
    (detail) => detail.attachments.length > 0,
  );
  expect(imageDetail).toBeDefined();
  const document = await readDocument();
  const attachment = imageDetail!.attachments[0]!;
  const digest = createHash("sha256")
    .update(imageDetail!.segment.content, "utf8")
    .digest("hex");
  const query = new URLSearchParams({
    expected_document_version: String(document.version),
    expected_content_digest: digest,
  });
  const managementPath = `${apiRoot}/documents/${document.id}/segments/${imageDetail!.segment.id}/attachments/${attachment.attachment_id}?${query}`;
  const citationPath = `${apiRoot}/bases/${baseId}/documents/${document.id}/segments/${imageDetail!.segment.id}/attachments/${attachment.attachment_id}?${query}`;
  const imageResponse = await context.request.get(managementPath);
  expect(imageResponse.status(), "unexpected HTTP status").toBe(200);
  expect(["image/png", "image/jpeg", "image/webp"]).toContain(
    imageResponse.headers()["content-type"]?.split(";", 1)[0],
  );
  expect(imageResponse.headers()["cache-control"]).toContain("no-store");
  const imageByteCount = Number(imageResponse.headers()["content-length"]);
  expect(Number.isSafeInteger(imageByteCount) && imageByteCount > 0).toBe(true);

  const otherProjectId = await createAdditionalProject(context, project);
  const crossProject = await context.request.get(
    managementPath.replace(
      `/api/projects/${project.id}/`,
      `/api/projects/${otherProjectId}/`,
    ),
  );
  expect(crossProject.status()).toBe(404);
  const browser = context.browser();
  expect(browser).not.toBeNull();
  const outsiderContext = await browser!.newContext();
  try {
    await registerReplayProject(outsiderContext, APP);
    expect((await outsiderContext.request.get(managementPath)).status()).toBe(
      404,
    );
  } finally {
    await outsiderContext.close();
  }

  const disable = await context.request.patch(
    `${apiRoot}/segments/${imageDetail!.segment.id}`,
    {
      headers: { "X-CSRF-Token": project.csrf },
      data: { enabled: false },
    },
  );
  expect(disable.status(), "unexpected HTTP status").toBe(200);
  expect((await context.request.get(citationPath)).status()).toBe(409);
  const disabledDocument = await readDocument();
  const disabledDetail = await readDetail(imageDetail!.segment.id);
  const disabledQuery = new URLSearchParams({
    expected_document_version: String(disabledDocument.version),
    expected_content_digest: createHash("sha256")
      .update(disabledDetail.segment.content, "utf8")
      .digest("hex"),
  });
  const disabledManagementPath = `${apiRoot}/documents/${document.id}/segments/${imageDetail!.segment.id}/attachments/${attachment.attachment_id}?${disabledQuery}`;
  const disabledCitationPath = `${apiRoot}/bases/${baseId}/documents/${document.id}/segments/${imageDetail!.segment.id}/attachments/${attachment.attachment_id}?${disabledQuery}`;
  expect((await context.request.get(disabledCitationPath)).status()).toBe(409);
  expect((await context.request.get(disabledManagementPath)).status()).toBe(
    200,
  );
  const enable = await context.request.patch(
    `${apiRoot}/segments/${imageDetail!.segment.id}`,
    {
      headers: { "X-CSRF-Token": project.csrf },
      data: { enabled: true },
    },
  );
  expect(enable.status(), "unexpected HTTP status").toBe(200);

  const enabledDocument = await readDocument();
  const enabledDetail = await readDetail(imageDetail!.segment.id);
  const enabledQuery = new URLSearchParams({
    expected_document_version: String(enabledDocument.version),
    expected_content_digest: createHash("sha256")
      .update(enabledDetail.segment.content, "utf8")
      .digest("hex"),
  });
  const enabledManagementPath = `${apiRoot}/documents/${document.id}/segments/${imageDetail!.segment.id}/attachments/${attachment.attachment_id}?${enabledQuery}`;

  await setProjectRevoked(context, project, true);
  expect((await context.request.get(enabledManagementPath)).status()).toBe(404);
  await setProjectRevoked(context, project, false);

  const beforeReembedObjects = await projectObjectInventory(context, project);
  const beforeReembedReads = await storageEvidence(context);
  const basesResponse = await context.request.get(
    `${apiRoot}/bases?page=1&page_size=50`,
  );
  expect(basesResponse.status(), "unexpected HTTP status").toBe(200);
  const base = (
    (await basesResponse.json()) as {
      items: Array<{ id: string; embedding_model_id: string | null }>;
    }
  ).items.find((item) => item.id === baseId);
  expect(base?.embedding_model_id).toBeTruthy();
  const beforeReembedDocument = await readDocument();
  const rebuild = await context.request.post(
    `${apiRoot}/bases/${baseId}/rebuild`,
    {
      headers: { "X-CSRF-Token": project.csrf },
      data: { embedding_model_id: base!.embedding_model_id },
    },
  );
  expect(rebuild.status(), "unexpected HTTP status").toBe(200);
  await expect
    .poll(
      async () => {
        const current = await readDocument();
        return {
          newer: current.version > beforeReembedDocument.version,
          status: current.status,
          task: current.task_progress,
        };
      },
      { timeout: 90_000 },
    )
    .toEqual({ newer: true, status: "ready", task: null });
  const afterReembedObjects = await projectObjectInventory(context, project);
  expect(afterReembedObjects.count).toBe(beforeReembedObjects.count);
  expect(
    afterReembedObjects.fingerprint === beforeReembedObjects.fingerprint,
    "re-embed changed the Project object inventory",
  ).toBe(true);
  expect((await storageEvidence(context)).source_reads).toBe(
    beforeReembedReads.source_reads,
  );
  await page.reload();
  await expect(rows.getByText("Ready")).toBeVisible({ timeout: 30_000 });

  const afterReembedDetails = await Promise.all(
    (await listSegments()).map((segment) => readDetail(segment.id)),
  );
  const oldCitationDetail = afterReembedDetails.find(
    (detail) => detail.attachments.length > 0,
  );
  expect(oldCitationDetail).toBeDefined();
  const beforeReparseDocument = await readDocument();
  const oldAttachment = oldCitationDetail!.attachments[0]!;
  const oldDigest = createHash("sha256")
    .update(oldCitationDetail!.segment.content, "utf8")
    .digest("hex");
  const oldQuery = new URLSearchParams({
    expected_document_version: String(beforeReparseDocument.version),
    expected_content_digest: oldDigest,
  });
  const oldCitationPath = `${apiRoot}/bases/${baseId}/documents/${document.id}/segments/${oldCitationDetail!.segment.id}/attachments/${oldAttachment.attachment_id}?${oldQuery}`;
  const oldManagementPath = `${apiRoot}/documents/${document.id}/segments/${oldCitationDetail!.segment.id}/attachments/${oldAttachment.attachment_id}?${oldQuery}`;

  await (await openDocumentActions(page, "document-with-image.docx"))
    .getByRole("menuitem", { name: "Chunk settings" })
    .click();
  const chunkSettings = page.getByTestId("knowledge-document-chunk-settings");
  await chunkSettings.getByLabel("Chunk size (Knowledge Tokens)").fill("500");
  await chunkSettings
    .getByRole("button", { name: "Refresh preview", exact: true })
    .click();
  await expect(
    chunkSettings.getByText(/Showing \d+ of \d+ chunks/u),
  ).toBeVisible({ timeout: 45_000 });
  await chunkSettings
    .getByRole("button", { name: "Reparse", exact: true })
    .click();
  await expect(chunkSettings).toBeHidden();
  await expect
    .poll(
      async () => {
        const current = await readDocument();
        return {
          newer: current.version > beforeReparseDocument.version,
          status: current.status,
          task: current.task_progress,
        };
      },
      { timeout: 90_000 },
    )
    .toEqual({ newer: true, status: "ready", task: null });
  const beforeStaleReads = await storageEvidence(context);
  expect((await context.request.get(oldCitationPath)).status()).toBe(404);
  expect((await context.request.get(oldManagementPath)).status()).toBe(404);
  expect((await storageEvidence(context)).attachment_reads).toBe(
    beforeStaleReads.attachment_reads,
  );

  await setStorageFaults(context, project, { delete_failures: 100 });
  await (await openDocumentActions(page, "document-with-image.docx"))
    .getByRole("menuitem", { name: "Delete" })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Delete", exact: true })
    .click();
  await expect
    .poll(
      async () => {
        const current = (await readDocuments()).find(
          (item) => item.id === document.id,
        );
        return current?.delete_error ?? null;
      },
      { timeout: 90_000 },
    )
    .not.toBeNull();
  const failedDeleteFacts = await projectFacts(context, project);
  expect(failedDeleteFacts).toMatchObject({
    document_rows: 1,
    published_documents: 1,
  });
  expect(failedDeleteFacts.object_count).toBeGreaterThan(0);
  expect(failedDeleteFacts.quota_used).toBeGreaterThan(0);

  await setStorageFaults(context, project, {});
  const retry = await context.request.delete(
    `${apiRoot}/documents/${document.id}`,
    { headers: { "X-CSRF-Token": project.csrf } },
  );
  expect(retry.status(), "unexpected HTTP status").toBe(200);
  await expect
    .poll(
      async () => {
        const facts = await projectFacts(context, project);
        return {
          documents: facts.document_rows,
          objects: facts.object_count,
          used: facts.quota_used,
          reserved: facts.quota_reserved,
        };
      },
      { timeout: 90_000 },
    )
    .toEqual({ documents: 0, objects: 0, used: 0, reserved: 0 });
});

test("a derived-image PUT failure never exposes a published attachment", async ({
  page,
  context,
}) => {
  test.setTimeout(240_000);
  const project = await registerReplayProject(context, APP);
  const apiRoot = `${APP}/api/projects/${project.id}/knowledge`;

  await page.goto(`/projects/${encodeURIComponent(project.slug)}/knowledge`);
  await createBaseThroughUI(page, "图片写入故障知识库");
  await page.getByRole("button", { name: "View documents" }).click();
  await selectFixtureForUpload(page, project, "document-with-image.docx");
  await expect(
    page.getByTestId("chunk-preview-panel").getByTestId("knowledge-image"),
  ).toHaveCount(1);
  await setStorageFaults(context, project, {
    attachment_put_failures: 100,
  });
  try {
    await submitSelectedFixture(page);
    const rows = page.getByTestId("knowledge-document-rows");
    await expect(rows.getByText("Failed", { exact: true })).toBeVisible({
      timeout: 90_000,
    });
    const facts = await projectFacts(context, project);
    expect(facts.published_documents).toBe(0);
    expect(facts.ready_attachments).toBe(0);

    const baseId = new URL(page.url()).searchParams.get("kb");
    expect(baseId).toBeTruthy();
    const documentsResponse = await context.request.get(
      `${apiRoot}/bases/${baseId}/documents?page=1&page_size=50`,
    );
    expect(documentsResponse.status(), "unexpected HTTP status").toBe(200);
    const document = (
      (await documentsResponse.json()) as {
        items: Array<{ id: string; name: string }>;
      }
    ).items.find((item) => item.name === "document-with-image.docx");
    expect(document).toBeDefined();
    const attachments = await context.request.get(
      `${apiRoot}/documents/${document!.id}/attachments`,
    );
    expect(attachments.status()).toBe(422);
  } finally {
    await setStorageFaults(context, project, {});
  }
});

test("a blocked model batch cannot publish after Project authority is revoked", async ({
  page,
  context,
}) => {
  test.setTimeout(240_000);
  const project = await registerReplayProject(context, APP);

  await page.goto(`/projects/${encodeURIComponent(project.slug)}/knowledge`);
  await createBaseThroughUI(page, "模型撤权知识库");
  await page.getByRole("button", { name: "View documents" }).click();
  await selectFixtureForUpload(page, project, "document-with-image.docx");
  await setEmbeddingBlocked(context, project, true);
  try {
    await submitSelectedFixture(page);
    await expect
      .poll(async () => (await providerCounters(context)).embedding_waiters, {
        timeout: 90_000,
      })
      .toBeGreaterThan(0);
    const baseId = new URL(page.url()).searchParams.get("kb");
    expect(baseId).toBeTruthy();
    const documentsResponse = await context.request.get(
      `${APP}/api/projects/${project.id}/knowledge/bases/${baseId}/documents?page=1&page_size=50`,
    );
    expect(documentsResponse.status(), "unexpected HTTP status").toBe(200);
    const documentId = (
      (await documentsResponse.json()) as { items: Array<{ id: string }> }
    ).items[0]?.id;
    expect(documentId).toBeTruthy();
    await setProjectRevoked(context, project, true);
    const revokedAttachments = await context.request.get(
      `${APP}/api/projects/${project.id}/knowledge/documents/${documentId}/attachments`,
    );
    expect(revokedAttachments.status()).toBe(404);
  } finally {
    await setEmbeddingBlocked(context, project, false);
  }
  await expect
    .poll(
      async () => {
        const provider = await providerCounters(context);
        const facts = await projectFacts(context, project);
        return {
          waiters: provider.embedding_waiters,
          running: facts.running_tasks,
          published: facts.published_documents,
        };
      },
      { timeout: 90_000 },
    )
    .toEqual({ waiters: 0, running: 0, published: 0 });
});

test("real knowledge settings render safely and a failed storage probe preserves the saved revision", async ({
  page,
  context,
}) => {
  // With no session cookie, the replay Gateway supplies its persisted System
  // Administrator. The UI and route still use the real authorization path.
  const readSettings = async () => {
    const response = await context.request.get(
      `${APP}/api/admin/settings/knowledge`,
    );
    expect(response.status()).toBe(200);
    const body = (await response.json()) as {
      revision: number;
      secret_key_configured: boolean;
      summary_model: { display_name: string } | null;
    };
    expect(body).not.toHaveProperty("minio_secret_key");
    return {
      revision: body.revision,
      secret_key_configured: body.secret_key_configured,
      summary_model: body.summary_model,
    };
  };
  const original = await readSettings();
  expect(original.secret_key_configured).toBe(true);
  expect(original.summary_model?.display_name).toBe("Replay Knowledge Summary");
  await page.goto("/admin/settings/knowledge");
  await expect(
    page.getByRole("heading", { name: "Knowledge settings", exact: true }),
  ).toBeVisible();
  await expect(page.getByTestId("knowledge-settings-revision")).toHaveText(
    `Revision ${original.revision}`,
  );
  await expect(
    page.getByRole("combobox", { name: "Summary model", exact: true }),
  ).toContainText("Replay Knowledge Summary");
  await expect(
    page.getByLabel("Storage secret key", { exact: true }),
  ).toHaveValue("");
  await page
    .getByLabel("Storage endpoint", { exact: true })
    .fill("127.0.0.1:1");
  // The real form requires a fresh secret whenever the endpoint changes. A
  // fictional value reaches the failed probe without exposing the live key.
  const replacementSecret = "fictional-unreachable-storage-key";
  await page
    .getByLabel("Storage secret key", { exact: true })
    .fill(replacementSecret);
  await expect(
    page.getByRole("button", { name: "Save settings", exact: true }),
  ).toBeEnabled();
  const [failed] = await Promise.all([
    page.waitForResponse(
      (response) =>
        response.url().endsWith("/api/admin/settings/knowledge") &&
        response.request().method() === "PUT",
    ),
    page.getByRole("button", { name: "Save settings", exact: true }).click(),
  ]);
  expect(failed.status()).toBe(422);
  const failure = (await failed.json()) as {
    detail: { code: string; message: string };
  };
  expect(failure.detail.code).toBe("KNOWLEDGE_SETTINGS_INVALID");
  expect(JSON.stringify(failure)).not.toContain("127.0.0.1:1");
  expect(JSON.stringify(failure)).not.toContain("minio_secret_key");
  expect(JSON.stringify(failure)).not.toContain(replacementSecret);
  await expect(
    page.getByRole("alert").filter({ hasText: failure.detail.message }),
  ).toBeVisible();
  expect(await readSettings()).toEqual(original);
});
