import { expect, test, type Route } from "@playwright/test";

import { mockLangGraphAPI } from "./utils/mock-api";

const PROJECT_ID = "40000000-0000-4000-8000-000000000010";
const NOW = "2026-07-22T08:00:00+00:00";

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

test("system admin suspends and resumes one project through exact lifecycle APIs", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  let suspended = false;
  let stateVersion = 1;
  let listRequests = 0;
  const lifecycleRequests: string[] = [];

  const project = () => ({
    project_id: PROJECT_ID,
    slug: "research-lab",
    display_name: "Research Lab",
    status: "active" as const,
    is_suspended: suspended,
    state_version: stateVersion,
    created_at: NOW,
    updated_at: NOW,
    deletion_effective_at: null,
  });

  await page.route(
    new RegExp(`/api/admin/projects/${PROJECT_ID}/(suspend|resume)$`, "u"),
    async (route) => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      lifecycleRequests.push(`${request.method()} ${path}`);
      if (request.method() !== "POST") {
        await json(route, { detail: "method not allowed" }, 405);
        return;
      }
      suspended = path.endsWith("/suspend");
      stateVersion += 1;
      await json(route, project());
    },
  );
  await page.route(/\/api\/admin\/projects(?:\?.*)?$/u, async (route) => {
    listRequests += 1;
    await json(route, { items: [project()], next_cursor: null });
  });

  await page.goto("/admin/projects");
  await expect(
    page.getByRole("heading", { name: "Projects", exact: true }),
  ).toBeVisible();
  const projectCard = page.getByRole("listitem").filter({
    has: page.getByRole("heading", { name: "Research Lab" }),
  });
  await expect(projectCard).toContainText("Active");

  await projectCard.getByRole("button", { name: "Platform suspend" }).click();
  const suspendDialog = page.getByRole("dialog", {
    name: "Suspend this project?",
  });
  await expect(suspendDialog).toContainText("freezes member private work");
  await suspendDialog.getByRole("button", { name: "Confirm" }).click();
  await expect(projectCard).toContainText("Suspended");
  await expect(
    projectCard.getByRole("button", { name: "Resume" }),
  ).toBeVisible();

  await projectCard.getByRole("button", { name: "Resume" }).click();
  const resumeDialog = page.getByRole("dialog", {
    name: "Resume this project?",
  });
  await expect(resumeDialog).toContainText("regain private-work access");
  await resumeDialog.getByRole("button", { name: "Confirm" }).click();
  await expect(
    projectCard.getByRole("button", { name: "Platform suspend" }),
  ).toBeVisible();
  await expect(projectCard).not.toContainText("Suspended");

  expect(lifecycleRequests).toEqual([
    `POST /api/admin/projects/${PROJECT_ID}/suspend`,
    `POST /api/admin/projects/${PROJECT_ID}/resume`,
  ]);
  expect(listRequests).toBeGreaterThanOrEqual(3);
});
