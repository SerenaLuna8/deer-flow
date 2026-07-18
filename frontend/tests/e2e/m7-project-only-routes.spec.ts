import { expect, test } from "@playwright/test";

import type { Capability, Project } from "@/core/projects/types";

import { mockLangGraphAPI, mockProjectAutomationAPI } from "./utils/mock-api";

const ACCOUNT_ID = "90000000-0000-4000-8000-000000000001";
const PROJECT_ID = "10000000-0000-4000-8000-000000000001";
const capabilities: Capability[] = [
  "project.read",
  "project.enter",
  "private_work.read_own",
  "private_work.create",
  "shared_assets.read",
  "shared_assets.execute",
  "automation.manage_own",
];
const project: Project = {
  id: PROJECT_ID,
  slug: "alpha",
  display_name: "Alpha Project",
  description: "Project-only routes",
  icon: "folder",
  role: "admin",
  capabilities,
  is_pinned: false,
  last_entered_at: null,
  member_count: 1,
  agent_count: 1,
  skill_count: 0,
  mcp_count: 0,
  status: "active",
  is_suspended: false,
  membership_version: 1,
  request_id: "request-alpha",
};

const LEGACY_WORKSPACE_ROUTES = [
  "/workspace/chats/new",
  "/workspace/agents",
  "/workspace/memory",
  "/workspace/scheduled-tasks",
  "/workspace/skills",
  "/workspace/tools",
  "/workspace/projects",
] as const;

test("legacy workspace URLs render Next not-found without redirect", async ({
  page,
}) => {
  mockLangGraphAPI(page);

  for (const path of LEGACY_WORKSPACE_ROUTES) {
    const response = await page.goto(path);
    expect(response?.status(), path).toBe(404);
    await expect(page, path).toHaveURL(path);
    await expect(page.getByText("This page could not be found.")).toBeVisible();
  }
});

test("private surfaces render only inside the selected project shell", async ({
  page,
}) => {
  await mockProjectAutomationAPI(page, [
    {
      id: ACCOUNT_ID,
      email: "owner@example.test",
      projects: [project],
    },
  ]);

  for (const [path, heading] of [
    ["chats", "私有对话"],
    ["memory", "Memory"],
    ["connections", "Connections"],
  ] as const) {
    await page.goto(`/projects/alpha/${path}`);
    await expect(page).toHaveURL(new RegExp(`/projects/alpha/${path}$`, "u"));
    await expect(page.getByTestId("project-shell")).toBeVisible();
    await expect(
      page.getByRole("heading", { name: heading, exact: true }),
    ).toBeVisible();
  }

  await page.goto("/projects/alpha/automations");
  await expect(page).toHaveURL(/\/projects\/alpha\/automations$/u);
  await expect(page.getByTestId("project-shell")).toBeVisible();
  await expect(page.getByTestId("automation-empty")).toBeVisible();
});
