import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type BrowserContext } from "@playwright/test";

import {
  createReplayThread,
  registerReplayProject,
  type ReplayProjectScope,
} from "./project-fixture";

const here = dirname(fileURLToPath(import.meta.url));
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
) as { prompt: string };

type RunSummary = {
  run_id?: unknown;
  status?: unknown;
};

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

async function successfulRunIds(
  context: BrowserContext,
  project: ReplayProjectScope,
  threadId: string,
): Promise<string[]> {
  return (await listRuns(context, project, threadId)).flatMap((run) =>
    run.status === "success" && typeof run.run_id === "string"
      ? [run.run_id]
      : [],
  );
}

async function leadAgentVersion(
  context: BrowserContext,
  project: ReplayProjectScope,
  runId: string,
): Promise<string> {
  const response = await context.request.get(
    `${APP}/api/projects/${project.id}/test-only/runs/${runId}/lead-agent-version`,
  );
  expect(response.status(), await response.text()).toBe(200);
  const body = (await response.json()) as { version_id?: unknown };
  expect(typeof body.version_id).toBe("string");
  if (typeof body.version_id !== "string") {
    throw new Error("lead Agent snapshot is missing its version ID");
  }
  return body.version_id;
}

test("the same Thread resolves the Current Agent for each new Run", async ({
  page,
  context,
}) => {
  test.setTimeout(150_000);
  const project = await registerReplayProject(context, APP);
  const threadId = await createReplayThread(context, APP, project);
  await page.goto(
    `/projects/${encodeURIComponent(project.slug)}/chats/${threadId}`,
  );

  const textarea = page.getByPlaceholder(/how can i assist you/i);
  await expect(textarea).toBeVisible({ timeout: 30_000 });
  await textarea.fill(fixture.prompt);
  await textarea.press("Enter");

  const chat = page.locator("#chat");
  await expect(chat.getByText("hi from replay.", { exact: true })).toBeVisible({
    timeout: 60_000,
  });
  await expect
    .poll(
      async () => (await successfulRunIds(context, project, threadId)).length,
      { timeout: 30_000 },
    )
    .toBe(1);
  const [firstRunId] = await successfulRunIds(context, project, threadId);
  if (!firstRunId) {
    throw new Error("the initial chat did not create a successful Run");
  }
  const firstVersionId = await leadAgentVersion(context, project, firstRunId);
  await page.screenshot({
    path:
      process.env.CURRENT_VERSION_EVIDENCE_PATH ??
      "/tmp/deer-flow-current-version-same-thread.png",
    fullPage: true,
  });

  const activated = await context.request.post(
    `${APP}/api/projects/${project.id}/test-only/agents/${project.agent.id}/activate-next-version`,
    { headers: { "X-CSRF-Token": project.csrf } },
  );
  expect(activated.status(), await activated.text()).toBe(200);
  const activatedBody = (await activated.json()) as {
    version_id?: unknown;
    version_number?: unknown;
  };
  expect(activatedBody.version_number).toBe(2);
  expect(typeof activatedBody.version_id).toBe("string");
  expect(activatedBody.version_id).not.toBe(firstVersionId);

  const assistantTurn = chat.locator("[data-assistant-turn]").last();
  await assistantTurn.hover();
  await assistantTurn
    .getByRole("button", { name: "Regenerate" })
    .click({ force: true });
  await expect
    .poll(async () => (await listRuns(context, project, threadId)).length, {
      timeout: 60_000,
    })
    .toBe(2);
  const runIds = (await listRuns(context, project, threadId)).flatMap((run) =>
    typeof run.run_id === "string" ? [run.run_id] : [],
  );
  const secondRunId = runIds.find((runId) => runId !== firstRunId);
  expect(secondRunId).toBeTruthy();
  if (!secondRunId) {
    throw new Error("regenerate did not create a second Run");
  }
  const secondVersionId = await leadAgentVersion(context, project, secondRunId);
  expect(secondVersionId).toBe(activatedBody.version_id);
});
