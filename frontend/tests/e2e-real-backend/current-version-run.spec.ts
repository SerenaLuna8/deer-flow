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

async function leadAgentSnapshotDefinition(
  context: BrowserContext,
  project: ReplayProjectScope,
  runId: string,
): Promise<string> {
  const response = await context.request.get(
    `${APP}/api/projects/${project.id}/test-only/runs/${runId}/lead-agent-definition`,
  );
  expect(response.status(), await response.text()).toBe(200);
  const body = (await response.json()) as { definition_id?: unknown };
  expect(typeof body.definition_id).toBe("string");
  if (typeof body.definition_id !== "string") {
    throw new Error("lead Agent snapshot is missing its Definition ID");
  }
  return body.definition_id;
}

test("the same Thread resolves the current Agent Definition for each new Run", async ({
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
  const firstDefinitionId = await leadAgentSnapshotDefinition(
    context,
    project,
    firstRunId,
  );
  await page.screenshot({
    path:
      process.env.CURRENT_VERSION_EVIDENCE_PATH ??
      "/tmp/deer-flow-current-definition-same-thread.png",
    fullPage: true,
  });

  const saved = await context.request.post(
    `${APP}/api/projects/${project.id}/test-only/agents/${project.agent.id}/save-next-definition`,
    { headers: { "X-CSRF-Token": project.csrf } },
  );
  expect(saved.status(), await saved.text()).toBe(200);
  const savedBody = (await saved.json()) as {
    definition_id?: unknown;
    revision?: unknown;
  };
  expect(savedBody.revision).toBe(2);
  expect(typeof savedBody.definition_id).toBe("string");
  expect(savedBody.definition_id).not.toBe(firstDefinitionId);

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
  const secondDefinitionId = await leadAgentSnapshotDefinition(
    context,
    project,
    secondRunId,
  );
  expect(secondDefinitionId).toBe(savedBody.definition_id);
});
