import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type BrowserContext, type Page } from "@playwright/test";

import {
  createReplayThread,
  registerReplayProject,
  type ReplayProjectScope,
} from "./project-fixture";

const here = dirname(fileURLToPath(import.meta.url));
const APP =
  process.env.E2E_APP_URL ??
  `http://localhost:${process.env.E2E_FRONTEND_PORT ?? "3317"}`;
const fixture = JSON.parse(
  readFileSync(
    join(
      here,
      "../../../backend/tests/fixtures/replay/write_read_file.ultra.json",
    ),
    "utf-8",
  ),
) as { prompt: string };

type RunSummary = {
  run_id?: unknown;
  status?: unknown;
  error?: unknown;
};

type ReplayWorkerFaultState = {
  mode: "immediate" | "delayed";
  running: boolean;
  fresh: boolean;
  held_model: boolean;
  held_claim: boolean;
  held_begin_execution: boolean;
};

type TerminalStreamFaultReadback = {
  target_run_id: string;
  matched_streams: number;
  dropped_terminal_frames: number;
  upstream_closed: boolean;
  abort_signalled: boolean;
  downstream_cancelled: boolean;
  reader_cancelled: boolean;
  controller_closed: boolean;
  released: boolean;
};

type NetworkObservation = {
  method: string;
  url: string;
  lastEventId: string | undefined;
};

const TERMINAL_STREAM_FAULT = "__actweaveE2ETerminalStreamFault";

async function installTerminalStreamFault(
  page: Page,
  threadId: string,
  runId: string,
): Promise<void> {
  await page.addInitScript(
    ({ faultName, selectedThreadId, selectedRunId }) => {
      const nativeFetch = window.fetch.bind(window);
      const targetPath = `/threads/${selectedThreadId}/runs/${selectedRunId}/stream`;
      const readback: TerminalStreamFaultReadback = {
        target_run_id: selectedRunId,
        matched_streams: 0,
        dropped_terminal_frames: 0,
        upstream_closed: false,
        abort_signalled: false,
        downstream_cancelled: false,
        reader_cancelled: false,
        controller_closed: false,
        released: false,
      };
      let reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
      let controller: ReadableStreamDefaultController<Uint8Array> | null = null;

      const cancelReader = async (reason: string) => {
        if (reader === null || readback.reader_cancelled) return;
        try {
          await reader.cancel(reason);
        } catch {
          // A concurrent browser abort may have already finalized the reader.
        } finally {
          readback.reader_cancelled = true;
        }
      };
      const closeController = () => {
        if (controller === null || readback.controller_closed) return;
        try {
          controller.close();
        } catch {
          // ReadableStream cancellation closes the controller first.
        } finally {
          readback.controller_closed = true;
        }
      };
      const release = async () => {
        if (readback.released) return;
        readback.released = true;
        await cancelReader("playwright terminal SSE fault released");
        closeController();
      };

      Reflect.set(window, faultName, { readback, release });
      window.fetch = async (input, init) => {
        const requestUrl =
          input instanceof Request
            ? input.url
            : input instanceof URL
              ? input.toString()
              : String(input);
        const method = (
          init?.method ?? (input instanceof Request ? input.method : "GET")
        ).toUpperCase();
        const response = await nativeFetch(input, init);
        const url = new URL(requestUrl, window.location.href);
        if (
          method !== "GET" ||
          !url.pathname.endsWith(targetPath) ||
          response.body === null
        ) {
          return response;
        }

        readback.matched_streams += 1;
        reader = response.body.getReader();
        const decoder = new TextDecoder();
        const encoder = new TextEncoder();
        let pending = "";

        const wrapped = new ReadableStream<Uint8Array>({
          start(selectedController) {
            controller = selectedController;
            const forwardCompleteFrames = () => {
              while (true) {
                const boundary = /\r?\n\r?\n/u.exec(pending);
                if (boundary?.index === undefined) return;
                const frameEnd = boundary.index + boundary[0].length;
                const frame = pending.slice(0, frameEnd);
                pending = pending.slice(frameEnd);
                if (/^event:\s*end\s*$/mu.test(frame)) {
                  readback.dropped_terminal_frames += 1;
                  continue;
                }
                selectedController.enqueue(encoder.encode(frame));
              }
            };
            const pump = async () => {
              try {
                while (!readback.released) {
                  const chunk = await reader!.read();
                  if (chunk.done) {
                    pending += decoder.decode();
                    forwardCompleteFrames();
                    if (pending.length > 0) {
                      selectedController.enqueue(encoder.encode(pending));
                      pending = "";
                    }
                    readback.upstream_closed = true;
                    // Deliberately keep the wrapper open. Canonical REST state,
                    // not the source closing, must converge the browser owner.
                    return;
                  }
                  pending += decoder.decode(chunk.value, { stream: true });
                  forwardCompleteFrames();
                }
              } catch (error) {
                if (!readback.released && !readback.abort_signalled) {
                  selectedController.error(error);
                }
              }
            };
            void pump();
          },
          async cancel(reason) {
            readback.downstream_cancelled = true;
            await cancelReader(String(reason ?? "downstream cancelled"));
          },
        });

        const signal =
          init?.signal ?? (input instanceof Request ? input.signal : null);
        signal?.addEventListener(
          "abort",
          () => {
            readback.abort_signalled = true;
            void cancelReader("reconciliation aborted target SSE").finally(
              closeController,
            );
          },
          { once: true },
        );

        return new Response(wrapped, {
          status: response.status,
          statusText: response.statusText,
          headers: response.headers,
        });
      };
    },
    {
      faultName: TERMINAL_STREAM_FAULT,
      selectedThreadId: threadId,
      selectedRunId: runId,
    },
  );
}

async function terminalStreamFaultReadback(
  page: Page,
): Promise<TerminalStreamFaultReadback | null> {
  return await page.evaluate((faultName) => {
    const fault = Reflect.get(window, faultName);
    if (typeof fault !== "object" || fault === null) return null;
    const readback = Reflect.get(fault, "readback");
    return typeof readback === "object" && readback !== null
      ? { ...readback }
      : null;
  }, TERMINAL_STREAM_FAULT);
}

async function releaseTerminalStreamFault(page: Page): Promise<void> {
  if (page.isClosed()) return;
  await page.evaluate(async (faultName) => {
    const fault = Reflect.get(window, faultName);
    const release =
      typeof fault === "object" && fault !== null
        ? Reflect.get(fault, "release")
        : null;
    if (typeof release === "function") await release();
  }, TERMINAL_STREAM_FAULT);
}

function observeRunNetwork(page: Page): NetworkObservation[] {
  const observations: NetworkObservation[] = [];
  page.on("request", (request) => {
    observations.push({
      method: request.method(),
      url: request.url(),
      lastEventId: request.headers()["last-event-id"],
    });
  });
  return observations;
}

async function listRuns(
  context: BrowserContext,
  project: ReplayProjectScope,
  threadId: string,
): Promise<RunSummary[]> {
  const response = await context.request.get(
    `${APP}/api/projects/${project.id}/private-work/threads/${threadId}/runs?limit=10`,
  );
  expect(response.status(), await response.text()).toBe(200);
  return (await response.json()) as RunSummary[];
}

async function setWorkerState(
  context: BrowserContext,
  action: "start" | "stop",
  csrf: string,
) {
  const response = await context.request.post(
    `${APP}/api/test-only/replay-worker/${action}`,
    { headers: { "X-CSRF-Token": csrf } },
  );
  expect(response.status(), await response.text()).toBe(200);
  const payload = (await response.json()) as {
    mode?: unknown;
    running?: unknown;
    fresh?: unknown;
  };
  return {
    mode: payload.mode,
    running: payload.running,
    fresh: payload.fresh,
  };
}

async function setWorkerFault(
  context: BrowserContext,
  barrier: "model" | "claim" | "begin_execution",
  action: "hold" | "release",
  csrf: string,
): Promise<ReplayWorkerFaultState> {
  const response = await context.request.post(
    `${APP}/api/test-only/replay-worker/faults/${barrier}/${action}`,
    { headers: { "X-CSRF-Token": csrf } },
  );
  expect(response.status(), await response.text()).toBe(200);
  return (await response.json()) as ReplayWorkerFaultState;
}

async function crashWorker(
  context: BrowserContext,
  csrf: string,
): Promise<ReplayWorkerFaultState> {
  const response = await context.request.post(
    `${APP}/api/test-only/replay-worker/crash`,
    { headers: { "X-CSRF-Token": csrf } },
  );
  expect(response.status(), await response.text()).toBe(200);
  return (await response.json()) as ReplayWorkerFaultState;
}

async function markRunRetrySafetyUnknown(
  context: BrowserContext,
  project: ReplayProjectScope,
  threadId: string,
  runId: string,
): Promise<void> {
  const response = await context.request.post(
    `${APP}/api/projects/${project.id}/test-only/threads/${threadId}/runs/${runId}/retry-safety/unknown`,
    { headers: { "X-CSRF-Token": project.csrf } },
  );
  expect(response.status(), await response.text()).toBe(200);
  expect(await response.json()).toEqual({
    run_id: runId,
    retry_safety: "unknown",
  });
}

async function cancelRunForCleanup(
  context: BrowserContext,
  project: ReplayProjectScope,
  threadId: string,
  runId: string,
): Promise<void> {
  const response = await context.request.post(
    `${APP}/api/projects/${project.id}/private-work/threads/${threadId}/runs/${runId}/cancel?wait=true`,
    { headers: { "X-CSRF-Token": project.csrf } },
  );
  expect(response.status(), await response.text()).toBe(204);
}

async function expectExecutionPhase(
  page: Page,
  phase:
    | "executing"
    | "waiting_for_lease_expiry"
    | "waiting_for_recovery"
    | "recovering"
    | "waiting_for_terminalization",
  timeout = 30_000,
): Promise<void> {
  await expect(
    page.locator(
      `[data-testid="run-execution-activity"][data-phase="${phase}"]`,
    ),
  ).toBeVisible({ timeout });
}

test("one Run waits without a Worker, survives reload, and completes after Worker start", async ({
  page,
  context,
}) => {
  test.skip(
    process.env.E2E_REPLAY_WORKER_MODE !== "delayed",
    "requires the delayed replay Worker controller",
  );
  test.setTimeout(150_000);

  const project = await registerReplayProject(context, APP);
  const threadId = await createReplayThread(context, APP, project);
  let workerStarted = false;
  try {
    await page.goto(
      `/projects/${encodeURIComponent(project.slug)}/chats/${threadId}`,
    );
    const textarea = page.getByPlaceholder(/how can i assist you/i);
    await expect(textarea).toBeVisible({ timeout: 30_000 });
    await textarea.fill(fixture.prompt);
    await textarea.press("Enter");

    await expect
      .poll(async () => (await listRuns(context, project, threadId)).length, {
        timeout: 30_000,
      })
      .toBe(1);
    const [admitted] = await listRuns(context, project, threadId);
    expect(typeof admitted?.run_id).toBe("string");
    const admittedRunId = admitted?.run_id;
    if (typeof admittedRunId !== "string") {
      throw new Error("the waiting Run is missing its durable run_id");
    }

    await expect(
      page.locator(
        '[data-testid="run-execution-activity"][data-phase="waiting_for_worker"]',
      ),
    ).toBeVisible({ timeout: 30_000 });
    await expect(
      page.getByText(/Waiting for an execution Worker|等待执行 Worker/),
    ).toBeVisible();

    await page.reload();
    await expect(
      page.locator(
        '[data-testid="run-execution-activity"][data-phase="waiting_for_worker"]',
      ),
    ).toBeVisible({ timeout: 30_000 });
    const afterReload = await listRuns(context, project, threadId);
    expect(afterReload).toHaveLength(1);
    expect(afterReload[0]?.run_id).toBe(admittedRunId);

    const started = await setWorkerState(context, "start", project.csrf);
    expect(started).toEqual({
      mode: "delayed",
      running: true,
      fresh: true,
    });
    workerStarted = true;

    await expect(
      page.locator("#chat").getByText("hi from replay.", { exact: true }),
    ).toBeVisible({ timeout: 90_000 });
    await expect
      .poll(async () => await listRuns(context, project, threadId), {
        timeout: 30_000,
      })
      .toEqual([
        expect.objectContaining({
          run_id: admittedRunId,
          status: "success",
        }),
      ]);
    await expect(page.getByTestId("run-failure-alert")).toHaveCount(0);
  } finally {
    if (workerStarted) {
      const stopped = await setWorkerState(context, "stop", project.csrf);
      expect(stopped).toEqual({
        mode: "delayed",
        running: false,
        fresh: false,
      });
    }
  }
});

test("terminal REST reconciliation ends the exact Run while its terminal SSE frame is withheld", async ({
  page,
  context,
}) => {
  test.skip(
    process.env.E2E_REPLAY_WORKER_MODE !== "delayed",
    "requires the delayed replay Worker controller",
  );
  test.setTimeout(180_000);

  const project = await registerReplayProject(context, APP);
  const threadId = await createReplayThread(context, APP, project);
  const network = observeRunNetwork(page);
  let workerStarted = false;
  let releaseExecutionStateGate = () => undefined;
  try {
    await page.goto(
      `/projects/${encodeURIComponent(project.slug)}/chats/${threadId}`,
    );
    const textarea = page.getByPlaceholder(/how can i assist you/i);
    await expect(textarea).toBeVisible({ timeout: 30_000 });
    await textarea.fill(fixture.prompt);
    await textarea.press("Enter");

    await expect
      .poll(async () => (await listRuns(context, project, threadId)).length, {
        timeout: 30_000,
      })
      .toBe(1);
    const [admitted] = await listRuns(context, project, threadId);
    if (typeof admitted?.run_id !== "string") {
      throw new Error("the faulted Run is missing its durable run_id");
    }
    const runId = admitted.run_id;
    await expect(
      page.locator(
        '[data-testid="run-execution-activity"][data-phase="waiting_for_worker"]',
      ),
    ).toBeVisible({ timeout: 30_000 });

    await installTerminalStreamFault(page, threadId, runId);
    await page.reload();
    await expect(
      page.locator(
        '[data-testid="run-execution-activity"][data-phase="waiting_for_worker"]',
      ),
    ).toBeVisible({ timeout: 30_000 });

    let executionStateGateOpen = false;
    const executionStateGate = new Promise<void>((resolve) => {
      releaseExecutionStateGate = () => {
        if (executionStateGateOpen) return;
        executionStateGateOpen = true;
        resolve();
      };
    });
    const executionStatePattern = `**/threads/${threadId}/runs/${runId}/execution-state`;
    await page.route(executionStatePattern, async (route) => {
      await executionStateGate;
      await route.continue();
    });

    const started = await setWorkerState(context, "start", project.csrf);
    expect(started).toEqual({
      mode: "delayed",
      running: true,
      fresh: true,
    });
    workerStarted = true;

    await expect
      .poll(async () => await terminalStreamFaultReadback(page), {
        timeout: 90_000,
      })
      .toMatchObject({
        target_run_id: runId,
        matched_streams: 1,
        dropped_terminal_frames: 1,
        upstream_closed: true,
        released: false,
      });

    // The wrapper has now consumed and withheld the real durable end frame,
    // and its upstream has closed without closing the browser-facing stream.
    // Only now expose the real terminal REST projection to the owner.
    releaseExecutionStateGate();

    await expect(
      page.locator("#chat").getByText("hi from replay.", { exact: true }),
    ).toBeVisible({ timeout: 30_000 });
    await expect(page.getByTestId("run-execution-activity")).toHaveCount(0, {
      timeout: 30_000,
    });
    await expect(
      page.getByRole("button", { name: "Submit" }).locator(".lucide-arrow-up"),
    ).toBeVisible({ timeout: 30_000 });

    await expect
      .poll(async () => await terminalStreamFaultReadback(page), {
        timeout: 30_000,
      })
      .toMatchObject({
        abort_signalled: true,
        reader_cancelled: true,
        controller_closed: true,
      });

    const storageBeforeRelease = await page.evaluate((selectedThreadId) => {
      const keys = Array.from({ length: sessionStorage.length }, (_, index) =>
        sessionStorage.key(index),
      ).filter((key): key is string => key !== null);
      const reconnect = keys.find(
        (key) =>
          key.startsWith("lg:stream:account:") &&
          !key.includes(":cursor:") &&
          key.endsWith(`:${selectedThreadId}`),
      );
      const cursor = keys.find(
        (key) =>
          key.startsWith("lg:stream:account:") &&
          key.endsWith(`:cursor:${selectedThreadId}`),
      );
      return {
        reconnect: reconnect ? sessionStorage.getItem(reconnect) : null,
        cursor: cursor ? sessionStorage.getItem(cursor) : null,
      };
    }, threadId);
    expect(storageBeforeRelease.reconnect).toBeNull();
    expect(storageBeforeRelease.cursor).not.toBeNull();

    const streamPath = `/threads/${threadId}/runs/${runId}/stream`;
    const streamRequestsAtConvergence = network.filter(
      (entry) =>
        entry.method === "GET" &&
        new URL(entry.url).pathname.endsWith(streamPath),
    ).length;
    const canonicalText = await page
      .locator("#chat")
      .getByText("hi from replay.", { exact: true })
      .allTextContents();

    await page.waitForTimeout(4_500);
    expect(
      network.filter(
        (entry) =>
          entry.method === "GET" &&
          new URL(entry.url).pathname.endsWith(streamPath),
      ),
    ).toHaveLength(streamRequestsAtConvergence);
    expect(
      network.filter(
        (entry) =>
          entry.method === "POST" &&
          entry.url.includes(runId) &&
          /\/(?:cancel|stop)(?:\?|$)/u.test(entry.url),
      ),
    ).toHaveLength(0);
    expect(await listRuns(context, project, threadId)).toEqual([
      expect.objectContaining({ run_id: runId, status: "success" }),
    ]);
    expect(
      await page
        .locator("#chat")
        .getByText("hi from replay.", { exact: true })
        .allTextContents(),
    ).toEqual(canonicalText);

    await releaseTerminalStreamFault(page);
    const storageAfterRelease = await page.evaluate((selectedThreadId) => {
      const cursorKey = Array.from(
        { length: sessionStorage.length },
        (_, index) => sessionStorage.key(index),
      ).find((key) => key?.endsWith(`:cursor:${selectedThreadId}`));
      return cursorKey ? sessionStorage.getItem(cursorKey) : null;
    }, threadId);
    expect(storageAfterRelease).toBe(storageBeforeRelease.cursor);
  } finally {
    releaseExecutionStateGate();
    await releaseTerminalStreamFault(page).catch(() => undefined);
    if (workerStarted) {
      const stopped = await setWorkerState(context, "stop", project.csrf);
      expect(stopped).toEqual({
        mode: "delayed",
        running: false,
        fresh: false,
      });
    }
  }
});

test("terminal Run A reconnect hint cannot preempt active catalog Run B", async ({
  page,
  context,
}) => {
  test.skip(
    process.env.E2E_REPLAY_WORKER_MODE !== "delayed",
    "requires the delayed replay Worker controller",
  );
  test.setTimeout(180_000);

  const project = await registerReplayProject(context, APP);
  const threadId = await createReplayThread(context, APP, project);
  let workerStarted = false;
  let pendingRunForCleanup: string | null = null;
  try {
    const started = await setWorkerState(context, "start", project.csrf);
    expect(started).toEqual({
      mode: "delayed",
      running: true,
      fresh: true,
    });
    workerStarted = true;

    await page.goto(
      `/projects/${encodeURIComponent(project.slug)}/chats/${threadId}`,
    );
    const textarea = page.getByPlaceholder(/how can i assist you/i);
    await expect(textarea).toBeVisible({ timeout: 30_000 });
    await textarea.fill(fixture.prompt);
    await textarea.press("Enter");
    await expect(
      page.locator("#chat").getByText("hi from replay.", { exact: true }),
    ).toBeVisible({ timeout: 90_000 });
    await expect
      .poll(async () => await listRuns(context, project, threadId), {
        timeout: 30_000,
      })
      .toEqual([expect.objectContaining({ status: "success" })]);
    const [terminalA] = await listRuns(context, project, threadId);
    if (typeof terminalA?.run_id !== "string") {
      throw new Error("terminal Run A is missing its durable run_id");
    }
    const runA = terminalA.run_id;

    const stopped = await setWorkerState(context, "stop", project.csrf);
    expect(stopped).toEqual({
      mode: "delayed",
      running: false,
      fresh: false,
    });
    workerStarted = false;

    await expect(
      page.getByRole("button", { name: "Submit" }).locator(".lucide-arrow-up"),
    ).toBeVisible({ timeout: 30_000 });
    await textarea.fill(fixture.prompt);
    await textarea.press("Enter");
    await expect
      .poll(async () => (await listRuns(context, project, threadId)).length, {
        timeout: 30_000,
      })
      .toBe(2);
    const runsWithB = await listRuns(context, project, threadId);
    const activeB = runsWithB.find(
      (run) => run.run_id !== runA && run.status === "pending",
    );
    if (typeof activeB?.run_id !== "string") {
      throw new Error("active catalog Run B is missing its durable run_id");
    }
    const runB = activeB.run_id;
    pendingRunForCleanup = runB;
    await expect(
      page.locator(
        '[data-testid="run-execution-activity"][data-phase="waiting_for_worker"]',
      ),
    ).toBeVisible({ timeout: 30_000 });

    const reconnect = await page.evaluate(
      ({ selectedThreadId, terminalRunId }) => {
        const keys = Array.from({ length: sessionStorage.length }, (_, index) =>
          sessionStorage.key(index),
        ).filter((key): key is string => key !== null);
        const key = keys.find(
          (candidate) =>
            candidate.startsWith("lg:stream:account:") &&
            !candidate.includes(":cursor:") &&
            candidate.endsWith(`:${selectedThreadId}`),
        );
        if (key === undefined) return null;
        const previous = sessionStorage.getItem(key);
        sessionStorage.setItem(key, terminalRunId);
        return { key, previous };
      },
      { selectedThreadId: threadId, terminalRunId: runA },
    );
    expect(reconnect).not.toBeNull();
    expect(reconnect?.previous).toBe(runB);

    const network = observeRunNetwork(page);
    await page.reload();
    await expect(
      page.locator(
        '[data-testid="run-execution-activity"][data-phase="waiting_for_worker"]',
      ),
    ).toBeVisible({ timeout: 30_000 });
    await expect
      .poll(
        async () =>
          await page.evaluate(
            (key) => sessionStorage.getItem(key),
            reconnect!.key,
          ),
        { timeout: 30_000 },
      )
      .toBe(runB);

    const collectionPath = `/api/projects/${project.id}/private-work/threads/${threadId}/runs`;
    const streamAPath = `${collectionPath}/${runA}/stream`;
    const executionAPath = `${collectionPath}/${runA}/execution-state`;
    const streamBPath = `${collectionPath}/${runB}/stream`;
    const executionBPath = `${collectionPath}/${runB}/execution-state`;
    await expect
      .poll(
        () =>
          network.some(
            (entry) =>
              entry.method === "GET" &&
              new URL(entry.url).pathname === executionBPath,
          ),
        { timeout: 30_000 },
      )
      .toBe(true);

    const catalogIndex = network.findIndex(
      (entry) =>
        entry.method === "GET" &&
        new URL(entry.url).pathname === collectionPath,
    );
    const streamBIndex = network.findIndex(
      (entry) =>
        entry.method === "GET" && new URL(entry.url).pathname === streamBPath,
    );
    expect(catalogIndex).toBeGreaterThanOrEqual(0);
    expect(streamBIndex).toBeGreaterThan(catalogIndex);
    expect(network[streamBIndex]?.lastEventId).toBe("0");
    expect(
      network.filter((entry) => {
        const path = new URL(entry.url).pathname;
        return path === streamAPath || path === executionAPath;
      }),
    ).toHaveLength(0);

    await page.waitForTimeout(2_500);
    expect(
      await page.evaluate((key) => sessionStorage.getItem(key), reconnect!.key),
    ).toBe(runB);
    await expect(
      page.locator(
        '[data-testid="run-execution-activity"][data-phase="waiting_for_worker"]',
      ),
    ).toBeVisible();
    expect(
      network.filter(
        (entry) =>
          entry.method === "POST" &&
          (entry.url.includes(runA) || entry.url.includes(runB)),
      ),
    ).toHaveLength(0);
    expect(await listRuns(context, project, threadId)).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ run_id: runA, status: "success" }),
        expect.objectContaining({ run_id: runB, status: "pending" }),
      ]),
    );
    expect(await listRuns(context, project, threadId)).toHaveLength(2);
  } finally {
    if (pendingRunForCleanup !== null) {
      await cancelRunForCleanup(
        context,
        project,
        threadId,
        pendingRunForCleanup,
      );
    }
    if (workerStarted) {
      const stopped = await setWorkerState(context, "stop", project.csrf);
      expect(stopped).toEqual({
        mode: "delayed",
        running: false,
        fresh: false,
      });
    }
  }
});

test("a safe Run crosses stale lease recovery into a new real Worker Attempt", async ({
  page,
  context,
}) => {
  test.skip(
    process.env.E2E_REPLAY_WORKER_MODE !== "delayed",
    "requires the delayed replay Worker controller",
  );
  test.setTimeout(210_000);

  const project = await registerReplayProject(context, APP);
  const threadId = await createReplayThread(context, APP, project);
  const observedPhases: string[] = [];
  let workerStarted = false;
  try {
    expect(
      await setWorkerFault(context, "model", "hold", project.csrf),
    ).toMatchObject({ held_model: true });
    const started = await setWorkerState(context, "start", project.csrf);
    expect(started).toEqual({
      mode: "delayed",
      running: true,
      fresh: true,
    });
    workerStarted = true;

    await page.goto(
      `/projects/${encodeURIComponent(project.slug)}/chats/${threadId}`,
    );
    const textarea = page.getByPlaceholder(/how can i assist you/i);
    await expect(textarea).toBeVisible({ timeout: 30_000 });
    await textarea.fill(fixture.prompt);
    await textarea.press("Enter");
    await expectExecutionPhase(page, "executing", 60_000);
    observedPhases.push("executing");

    await expect
      .poll(async () => (await listRuns(context, project, threadId)).length, {
        timeout: 30_000,
      })
      .toBe(1);
    const [admitted] = await listRuns(context, project, threadId);
    if (typeof admitted?.run_id !== "string") {
      throw new Error("safe recovery Run is missing its durable run_id");
    }
    const runId = admitted.run_id;

    expect(
      await setWorkerFault(context, "claim", "hold", project.csrf),
    ).toMatchObject({ held_claim: true });
    expect(
      await setWorkerFault(context, "begin_execution", "hold", project.csrf),
    ).toMatchObject({ held_begin_execution: true });
    const crashed = await crashWorker(context, project.csrf);
    expect(crashed).toMatchObject({
      mode: "delayed",
      running: false,
      held_model: true,
      held_claim: true,
      held_begin_execution: true,
    });
    workerStarted = false;

    await expectExecutionPhase(page, "waiting_for_lease_expiry", 30_000);
    observedPhases.push("waiting_for_lease_expiry");

    const replacement = await setWorkerState(context, "start", project.csrf);
    expect(replacement).toEqual({
      mode: "delayed",
      running: true,
      fresh: true,
    });
    workerStarted = true;
    await expectExecutionPhase(page, "waiting_for_recovery", 30_000);
    observedPhases.push("waiting_for_recovery");

    expect(
      await setWorkerFault(context, "claim", "release", project.csrf),
    ).toMatchObject({ held_claim: false });
    await expectExecutionPhase(page, "recovering", 30_000);
    observedPhases.push("recovering");

    expect(
      await setWorkerFault(context, "begin_execution", "release", project.csrf),
    ).toMatchObject({ held_begin_execution: false });
    await expectExecutionPhase(page, "executing", 30_000);
    observedPhases.push("executing");

    expect(
      await setWorkerFault(context, "model", "release", project.csrf),
    ).toMatchObject({ held_model: false });
    await expect(
      page.locator("#chat").getByText("hi from replay.", { exact: true }),
    ).toBeVisible({ timeout: 90_000 });
    await expect(page.getByTestId("run-execution-activity")).toHaveCount(0, {
      timeout: 30_000,
    });
    observedPhases.push("terminal");

    expect(observedPhases).toEqual([
      "executing",
      "waiting_for_lease_expiry",
      "waiting_for_recovery",
      "recovering",
      "executing",
      "terminal",
    ]);
    expect(await listRuns(context, project, threadId)).toEqual([
      expect.objectContaining({ run_id: runId, status: "success" }),
    ]);
    await expect(page.getByTestId("run-failure-alert")).toHaveCount(0);
  } finally {
    await setWorkerFault(context, "claim", "release", project.csrf).catch(
      () => undefined,
    );
    await setWorkerFault(
      context,
      "begin_execution",
      "release",
      project.csrf,
    ).catch(() => undefined);
    await setWorkerFault(context, "model", "release", project.csrf).catch(
      () => undefined,
    );
    if (workerStarted) {
      const stopped = await setWorkerState(context, "stop", project.csrf);
      expect(stopped).toEqual({
        mode: "delayed",
        running: false,
        fresh: false,
      });
    }
  }
});

test("an unknown-safety expired lease terminalizes without replaying the Run", async ({
  page,
  context,
}) => {
  test.skip(
    process.env.E2E_REPLAY_WORKER_MODE !== "delayed",
    "requires the delayed replay Worker controller",
  );
  test.setTimeout(210_000);

  const project = await registerReplayProject(context, APP);
  const threadId = await createReplayThread(context, APP, project);
  const observedPhases: string[] = [];
  let workerStarted = false;
  try {
    expect(
      await setWorkerFault(context, "model", "hold", project.csrf),
    ).toMatchObject({ held_model: true });
    const started = await setWorkerState(context, "start", project.csrf);
    expect(started).toEqual({
      mode: "delayed",
      running: true,
      fresh: true,
    });
    workerStarted = true;

    await page.goto(
      `/projects/${encodeURIComponent(project.slug)}/chats/${threadId}`,
    );
    const textarea = page.getByPlaceholder(/how can i assist you/i);
    await expect(textarea).toBeVisible({ timeout: 30_000 });
    await textarea.fill(fixture.prompt);
    await textarea.press("Enter");
    await expectExecutionPhase(page, "executing", 60_000);
    observedPhases.push("executing");

    await expect
      .poll(async () => (await listRuns(context, project, threadId)).length, {
        timeout: 30_000,
      })
      .toBe(1);
    const [admitted] = await listRuns(context, project, threadId);
    if (typeof admitted?.run_id !== "string") {
      throw new Error("unknown-safety Run is missing its durable run_id");
    }
    const runId = admitted.run_id;
    await markRunRetrySafetyUnknown(context, project, threadId, runId);
    expect(
      await setWorkerFault(context, "claim", "hold", project.csrf),
    ).toMatchObject({ held_claim: true });

    const crashed = await crashWorker(context, project.csrf);
    expect(crashed).toMatchObject({
      mode: "delayed",
      running: false,
      held_model: true,
      held_claim: true,
    });
    workerStarted = false;
    await expectExecutionPhase(page, "waiting_for_lease_expiry", 30_000);
    observedPhases.push("waiting_for_lease_expiry");

    const replacement = await setWorkerState(context, "start", project.csrf);
    expect(replacement).toEqual({
      mode: "delayed",
      running: true,
      fresh: true,
    });
    workerStarted = true;
    await expectExecutionPhase(page, "waiting_for_terminalization", 30_000);
    observedPhases.push("waiting_for_terminalization");

    expect(
      await setWorkerFault(context, "claim", "release", project.csrf),
    ).toMatchObject({ held_claim: false });
    await expect(page.getByTestId("run-execution-activity")).toHaveCount(0, {
      timeout: 30_000,
    });
    observedPhases.push("terminal");

    expect(observedPhases).toEqual([
      "executing",
      "waiting_for_lease_expiry",
      "waiting_for_terminalization",
      "terminal",
    ]);
    await expect
      .poll(async () => await listRuns(context, project, threadId), {
        timeout: 30_000,
      })
      .toEqual([
        expect.objectContaining({
          run_id: runId,
          status: "error",
          error: "SIDE_EFFECT_STATE_UNKNOWN",
        }),
      ]);
    await expect(page.getByTestId("run-failure-alert")).toBeVisible();
  } finally {
    await setWorkerFault(context, "claim", "release", project.csrf).catch(
      () => undefined,
    );
    await setWorkerFault(context, "model", "release", project.csrf).catch(
      () => undefined,
    );
    if (workerStarted) {
      const stopped = await setWorkerState(context, "stop", project.csrf);
      expect(stopped).toEqual({
        mode: "delayed",
        running: false,
        fresh: false,
      });
    }
  }
});
