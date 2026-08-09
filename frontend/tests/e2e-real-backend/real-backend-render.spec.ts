import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test } from "@playwright/test";

import {
  createReplayThread,
  registerReplayProject,
  type ReplayProjectScope,
} from "./project-fixture";

const here = dirname(fileURLToPath(import.meta.url));

/**
 * Drive the real frontend against the real Gateway/Worker Replay boundary and
 * assert the browser renders the backend's data correctly without an API key.
 *
 * The prompt is read from the same fixture the gateway replays, so the input
 * hash matches and the recorded model turns reproduce deterministically. The
 * default auto-title is local fallback state, not a replayed model turn.
 */
// Register through the frontend origin (same-origin proxy) so the auth cookies
// are stored for and sent to the browser origin — the gateway is reached via the
// next.config rewrite, never cross-origin from the browser.
const APP =
  process.env.E2E_APP_URL ??
  `http://localhost:${process.env.E2E_FRONTEND_PORT ?? "3000"}`;
const fixture = JSON.parse(
  readFileSync(
    join(
      here,
      "../../../backend/tests/fixtures/replay/write_read_file.ultra.json",
    ),
    "utf-8",
  ),
) as {
  prompt: string;
  turns: Array<{
    stream?: {
      provenance?: unknown;
      text_chunk_chars?: unknown;
    };
    output: { data: { content?: unknown; id?: unknown } };
  }>;
};

const PROMPT = fixture.prompt;
const FALLBACK_TITLE_MAX_CHARS = 50;

function fallbackTitle(userMsg: string): string {
  if (!userMsg) return "New Conversation";
  if (userMsg.length <= FALLBACK_TITLE_MAX_CHARS) return userMsg;
  return `${userMsg.slice(0, FALLBACK_TITLE_MAX_CHARS).trimEnd()}...`;
}

// Suggestions still come from the recorded model fixture. The default title no
// longer does: TitleMiddleware uses a local fallback when title.model_name is
// unset, so derive that expected title from the prompt.
const textTurns = fixture.turns
  .map((t) => t.output?.data?.content)
  .filter((c): c is string => typeof c === "string" && c.trim().length > 0);
const suggestionsRaw = textTurns.find((c) => c.trim().startsWith("["));
// Guarded parse: a bracket-prefixed turn that isn't a valid JSON string array
// falls back to "" so the `not.toBe("")` assertion below fails with a clear
// message instead of a generic JSON.parse throw.
const EXPECTED_SUGGESTION = ((): string => {
  if (!suggestionsRaw) return "";
  try {
    const arr: unknown = JSON.parse(suggestionsRaw);
    return Array.isArray(arr) && typeof arr[0] === "string" ? arr[0] : "";
  } catch {
    return "";
  }
})();
const EXPECTED_TITLE = fallbackTitle(PROMPT);

const derivedBurstTurn = fixture.turns.find(
  (turn) =>
    turn.stream?.provenance === "derived_from_recorded_output" &&
    typeof turn.stream.text_chunk_chars === "number",
);
const DERIVED_BURST_CONTENT = derivedBurstTurn?.output.data.content;
const DERIVED_BURST_MESSAGE_ID = derivedBurstTurn?.output.data.id;
const DERIVED_BURST_CHUNK_CHARS = derivedBurstTurn?.stream?.text_chunk_chars;

type DurableSseFrame = {
  id?: string;
  event?: string;
  data?: unknown;
};

function parseDurableSse(body: string): DurableSseFrame[] {
  return body
    .split(/\r?\n\r?\n/)
    .map((block) => {
      const frame: DurableSseFrame = {};
      const dataLines: string[] = [];
      for (const line of block.split(/\r?\n/)) {
        if (line.startsWith("id:")) frame.id = line.slice(3).trimStart();
        if (line.startsWith("event:")) frame.event = line.slice(6).trimStart();
        if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
      }
      if (dataLines.length > 0) frame.data = JSON.parse(dataLines.join("\n"));
      return frame;
    })
    .filter((frame) =>
      [frame.id, frame.event, frame.data].some((value) => value !== undefined),
    );
}

test.describe("real backend render (replay, no API key)", () => {
  let project: ReplayProjectScope;

  test.beforeEach(async ({ context }) => {
    project = await registerReplayProject(context, APP);
  });

  test("renders the local auto-title + replayed suggestions from a real backend", async ({
    page,
    context,
  }) => {
    const threadId = await createReplayThread(context, APP, project);
    await page.goto(
      `/projects/${encodeURIComponent(project.slug)}/chats/${threadId}`,
    );

    const textarea = page.getByPlaceholder(/how can i assist you/i);
    await expect(textarea).toBeVisible({ timeout: 30_000 });
    await textarea.fill(PROMPT);
    await textarea.press("Enter");

    // The title is project-thread metadata rendered in the conversation list,
    // not chat transcript content. The answer and generated file belong to the
    // chat/artifact surfaces; the suggestion is a Gateway auxiliary model call.
    expect(
      EXPECTED_TITLE,
      "default local fallback title should be derived from the prompt",
    ).not.toBe("");
    expect(
      EXPECTED_SUGGESTION,
      "fixture should contain a suggestions turn",
    ).not.toBe("");
    const chat = page.locator("#chat");
    await expect(
      chat.getByText("hi from replay.", { exact: true }),
    ).toBeVisible({
      timeout: 60_000,
    });
    await expect(
      page.getByRole("link", { name: new RegExp(EXPECTED_TITLE, "i") }),
    ).toBeVisible({ timeout: 30_000 });
    await expect(
      page.getByRole("combobox").filter({ hasText: "note.txt" }),
    ).toBeVisible();
    await expect(chat.getByText(EXPECTED_SUGGESTION)).toBeVisible({
      timeout: 30_000,
    });
    await expect(page.getByTestId("run-failure-alert")).toHaveCount(0);

    // Reconnect from cursor zero after settlement. This response is rebuilt
    // from PostgreSQL run_events by the real Gateway, so the assertion covers
    // the real Worker coalescer + durable write + replay path rather than only
    // the browser's live in-memory projection. The fixture's per-character
    // chunks are explicitly derived from (and must reconstruct) the recorded
    // AIMessage; they do not claim to be historical provider transport frames.
    expect(typeof DERIVED_BURST_CONTENT).toBe("string");
    expect(typeof DERIVED_BURST_MESSAGE_ID).toBe("string");
    expect(typeof DERIVED_BURST_CHUNK_CHARS).toBe("number");
    const runsResponse = await context.request.get(
      `${APP}/api/projects/${project.id}/private-work/threads/${threadId}/runs?limit=10`,
    );
    expect(runsResponse.status(), await runsResponse.text()).toBe(200);
    const runs = (await runsResponse.json()) as Array<{
      run_id?: unknown;
      status?: unknown;
    }>;
    const completedRun = runs.find(
      (run) => typeof run.run_id === "string" && run.status === "success",
    );
    expect(
      completedRun,
      "the replay Run must settle successfully",
    ).toBeTruthy();
    const completedRunId = completedRun?.run_id;
    if (typeof completedRunId !== "string") {
      throw new Error("the replay Run response is missing its durable run_id");
    }

    const durableReplay = await context.request.get(
      `${APP}/api/projects/${project.id}/private-work/threads/${threadId}/runs/${completedRunId}/stream`,
      { headers: { "Last-Event-ID": "0" } },
    );
    expect(durableReplay.status(), await durableReplay.text()).toBe(200);
    const frames = parseDurableSse(await durableReplay.text());
    const durableTextFrames = frames.filter((frame) => {
      if (frame.event !== "messages" || !Array.isArray(frame.data))
        return false;
      const message = frame.data[0];
      return (
        typeof message === "object" &&
        message !== null &&
        Reflect.get(message, "id") === DERIVED_BURST_MESSAGE_ID &&
        typeof Reflect.get(message, "content") === "string"
      );
    });
    const reconstructed = durableTextFrames
      .map((frame) => {
        const message = (frame.data as unknown[])[0];
        if (typeof message !== "object" || message === null) {
          throw new Error("durable text frame is missing its message payload");
        }
        const content = Reflect.get(message, "content");
        if (typeof content !== "string") {
          throw new Error("durable text frame content is not a string");
        }
        return content;
      })
      .join("");
    expect(reconstructed).toBe(DERIVED_BURST_CONTENT);

    const logicalChunkCount = Math.ceil(
      Array.from(DERIVED_BURST_CONTENT as string).length /
        (DERIVED_BURST_CHUNK_CHARS as number),
    );
    expect(logicalChunkCount).toBeGreaterThanOrEqual(10);
    expect(durableTextFrames.length).toBeGreaterThan(0);
    // The leading edge is deliberately immediate. Excluding that required
    // first frame, the burst tail must shrink by at least one order of
    // magnitude while its bytes remain exact above.
    expect(durableTextFrames.length - 1).toBeLessThanOrEqual(
      Math.floor((logicalChunkCount - 1) / 10),
    );

    // Visual regression is OS-sensitive (a macOS baseline won't match CI's
    // Linux render), so it's a local dev gate only; in CI we capture the render
    // as an artifact for human review instead of hard-asserting a cross-OS
    // baseline. The DOM assertions above are the CI gate.
    if (process.env.CI) {
      await page.screenshot({
        path: "test-results/real-backend-render.png",
        fullPage: true,
      });
    } else {
      await expect(page).toHaveScreenshot("real-backend-render.png", {
        maxDiffPixelRatio: 0.02,
        fullPage: true,
      });
    }
  });
});
