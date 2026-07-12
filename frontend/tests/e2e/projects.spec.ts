import { expect, test, type Page, type Route } from "@playwright/test";

import type { Project } from "@/core/projects/types";

import { mockLangGraphAPI } from "./utils/mock-api";

const PROJECT_ID = "10000000-0000-4000-8000-000000000001";
const SECOND_PROJECT_ID = "10000000-0000-4000-8000-000000000002";

const baseProject: Project = {
  id: PROJECT_ID,
  slug: "research-lab",
  display_name: "Research Lab",
  description: "Shared research workspace",
  icon: "folder",
  role: "admin",
  capabilities: [
    "project.read",
    "project.update",
    "project.enter",
    "project.pin",
  ],
  is_pinned: false,
  last_entered_at: null,
  member_count: 4,
  agent_count: 2,
  skill_count: 3,
  mcp_count: 1,
  status: "active",
  is_suspended: false,
  membership_version: 1,
  request_id: "request-initial",
};

type ProjectMock = {
  projects: () => Project[];
  listRequests: () => URL[];
  enterPaths: () => string[];
  failNextList: () => void;
};

function projectResponse(project: Project, requestId: string): Project {
  return { ...project, request_id: requestId };
}

async function fulfillProject(route: Route, project: Project) {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(project),
  });
}

async function mockProjectsAPI(
  page: Page,
  initialProjects: Project[] = [baseProject],
): Promise<ProjectMock> {
  let projects = structuredClone(initialProjects);
  let listFailuresRemaining = 0;
  const listRequests: URL[] = [];
  const enterPaths: string[] = [];

  await page.route(/\/api\/projects(?:\/.*)?(?:\?.*)?$/, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();
    const path = url.pathname;

    if (path.endsWith("/api/projects") && method === "GET") {
      listRequests.push(url);
      if (listFailuresRemaining > 0) {
        listFailuresRemaining -= 1;
        await route.fulfill({
          status: 503,
          contentType: "application/json",
          body: JSON.stringify({
            detail: {
              code: "DATABASE_UNAVAILABLE",
              message: "private backend detail",
            },
          }),
        });
        return;
      }
      const query = url.searchParams.get("query")?.toLocaleLowerCase() ?? "";
      const items = projects.filter(
        (project) =>
          !query ||
          project.slug.toLocaleLowerCase().includes(query) ||
          project.display_name.toLocaleLowerCase().includes(query),
      );
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items, next_cursor: null }),
      });
      return;
    }

    if (path.endsWith("/api/projects") && method === "POST") {
      const input = request.postDataJSON() as {
        slug: string;
        display_name: string;
        description?: string;
        icon?: string;
      };
      if (projects.some((project) => project.slug === input.slug)) {
        await route.fulfill({
          status: 409,
          contentType: "application/json",
          body: JSON.stringify({
            detail: {
              code: "PROJECT_SLUG_CONFLICT",
              message: "private backend detail",
            },
          }),
        });
        return;
      }
      const created: Project = {
        ...baseProject,
        id: SECOND_PROJECT_ID,
        slug: input.slug,
        display_name: input.display_name,
        description: input.description ?? "",
        icon: input.icon ?? "folder",
        member_count: 1,
        agent_count: 0,
        skill_count: 0,
        mcp_count: 0,
        request_id: "request-create",
      };
      projects.push(created);
      await fulfillProject(route, created);
      return;
    }

    const pinMatch = /\/api\/projects\/([^/]+)\/pin$/.exec(path);
    if (pinMatch && method === "PUT") {
      const input = request.postDataJSON() as { pinned: boolean };
      const id = decodeURIComponent(pinMatch[1]!);
      const project = projects.find((item) => item.id === id)!;
      const updated = projectResponse(
        { ...project, is_pinned: input.pinned },
        "request-pin",
      );
      projects = projects.map((item) => (item.id === id ? updated : item));
      await fulfillProject(route, updated);
      return;
    }

    const enterMatch = /\/api\/projects\/([^/]+)\/enter$/.exec(path);
    if (enterMatch && method === "POST") {
      const id = decodeURIComponent(enterMatch[1]!);
      enterPaths.push(path);
      const project = projects.find((item) => item.id === id)!;
      const entered = projectResponse(
        { ...project, last_entered_at: "2026-07-12T08:00:00+00:00" },
        "request-enter",
      );
      projects = projects.map((item) => (item.id === id ? entered : item));
      await fulfillProject(route, entered);
      return;
    }

    const projectMatch = /\/api\/projects\/([^/]+)$/.exec(path);
    if (projectMatch && method === "PATCH") {
      const id = decodeURIComponent(projectMatch[1]!);
      const input = request.postDataJSON() as Partial<Project>;
      const project = projects.find((item) => item.id === id)!;
      const updated = projectResponse(
        { ...project, ...input },
        "request-update",
      );
      projects = projects.map((item) => (item.id === id ? updated : item));
      await fulfillProject(route, updated);
      return;
    }

    await route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({
        detail: { code: "PROJECT_NOT_FOUND", message: "not found" },
      }),
    });
  });

  return {
    projects: () => projects,
    listRequests: () => listRequests,
    enterPaths: () => enterPaths,
    failNextList: () => {
      // TanStack Query retries failed queries three times by default. Fail the
      // initial request plus those retries so the visible retry state is real.
      listFailuresRemaining = 4;
    },
  };
}

test("workspace shows project cards without project navigation", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  await mockProjectsAPI(page);

  await page.goto("/workspace");

  await expect(page.getByTestId("project-workbench")).toBeVisible();
  await expect(page.getByText("Research Lab", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Agents" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Skills" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Tools" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Memory" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Scheduled tasks" })).toHaveCount(
    0,
  );
  await expect(page.getByRole("button", { name: "账户" })).toBeVisible();
});

test("legacy project workspace address redirects to workspace", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  await mockProjectsAPI(page);

  await page.goto("/workspace/projects");

  await expect(page).toHaveURL(/\/workspace$/);
  await expect(page.getByTestId("project-workbench")).toBeVisible();
});

test("project workbench supports create, search, pin, edit, enter, and return", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  const api = await mockProjectsAPI(page);

  await page.goto("/workspace");
  await expect(page.getByTestId("project-workbench")).toBeVisible();
  await expect(page.getByText("Research Lab", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "创建项目" }).first().click();
  const createDialog = page.getByRole("dialog", { name: "创建项目" });
  await createDialog.getByLabel("项目名称").fill("Launch Room");
  await createDialog.getByLabel("项目标识").fill("launch-room");
  await createDialog.getByLabel("描述").fill("Launch coordination");
  await createDialog.getByRole("button", { name: "创建项目" }).click();
  await expect(page.getByText("Launch Room", { exact: true })).toBeVisible();

  const launchCard = page
    .getByTestId("project-card")
    .filter({ hasText: "Launch Room" });
  await launchCard.getByRole("button", { name: "置顶项目" }).click();
  await expect(
    launchCard.getByRole("button", { name: "取消置顶" }),
  ).toBeVisible();

  await launchCard.getByRole("button", { name: "编辑项目" }).click();
  const editDialog = page.getByRole("dialog", { name: "编辑项目" });
  await editDialog.getByLabel("项目名称").fill("Launch Center");
  await editDialog.getByRole("button", { name: "保存修改" }).click();
  await expect(page.getByText("Launch Center", { exact: true })).toBeVisible();

  await page.getByRole("textbox", { name: "搜索项目" }).fill("Research");
  await expect(page.getByText("Research Lab", { exact: true })).toBeVisible();
  await expect(page.getByText("Launch Center", { exact: true })).toHaveCount(0);

  await page.getByRole("textbox", { name: "搜索项目" }).fill("no-result");
  await expect(page.getByTestId("project-search-empty")).toBeVisible();
  await page.getByRole("button", { name: "清除搜索" }).click();

  const researchCard = page
    .getByTestId("project-card")
    .filter({ hasText: "Research Lab" });
  await researchCard.getByRole("link", { name: "进入项目" }).click();
  await expect(page).toHaveURL(/\/projects\/research-lab$/);
  await expect(page.getByTestId("project-home")).toBeVisible();
  await expect(
    page.getByText("对话和记忆私有，Agent、Skill 和 MCP 共享。"),
  ).toBeVisible();
  expect(api.enterPaths()).toEqual([`/api/projects/${PROJECT_ID}/enter`]);
  expect(api.listRequests().at(-1)?.searchParams.get("query")).toBe(
    "research-lab",
  );

  await page.getByRole("link", { name: "返回工作空间" }).click();
  await expect(page).toHaveURL(/\/workspace$/);
});

test("project workbench exposes loading-safe API error and retry", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  const api = await mockProjectsAPI(page, []);
  api.failNextList();

  await page.goto("/workspace");
  const error = page.getByTestId("project-load-error");
  await expect(error).toBeVisible({ timeout: 12_000 });
  await expect(error).toContainText("项目服务暂时不可用，请稍后重试。");
  await expect(error).not.toContainText("private backend detail");
  await error.getByRole("button", { name: "重试" }).click();
  await expect(page.getByTestId("project-empty")).toBeVisible();
});

test("project create conflict stays handled and shows a safe message", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  await mockProjectsAPI(page);
  const pageErrors: Error[] = [];
  page.on("pageerror", (error) => pageErrors.push(error));

  await page.goto("/workspace");
  await page.getByRole("button", { name: "创建项目" }).first().click();
  const dialog = page.getByRole("dialog", { name: "创建项目" });
  await dialog.getByLabel("项目名称").fill("Duplicate Research");
  await dialog.getByLabel("项目标识").fill("research-lab");
  await dialog.getByRole("button", { name: "创建项目" }).click();

  await expect(dialog.getByRole("alert")).toHaveText(
    "这个项目标识已存在，请换一个后重试。",
  );
  expect(pageErrors).toEqual([]);
});

test("project home retries a failed slug lookup without stale data", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  const api = await mockProjectsAPI(page);
  api.failNextList();

  await page.goto("/projects/research-lab");
  const alert = page.getByText("项目服务暂时不可用，请稍后重试。", {
    exact: true,
  });
  await expect(alert).toContainText("项目服务暂时不可用，请稍后重试。", {
    timeout: 12_000,
  });
  await page.getByRole("button", { name: "重试" }).click();
  await expect(page.getByTestId("project-home")).toBeVisible();
  expect(api.enterPaths()).toEqual([`/api/projects/${PROJECT_ID}/enter`]);
});

test("project home stays private-work disabled in dark mobile layout", async ({
  page,
}) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.emulateMedia({ colorScheme: "dark" });
  mockLangGraphAPI(page);
  const api = await mockProjectsAPI(page);
  const threadRequests: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.includes("/threads")) {
      threadRequests.push(request.url());
    }
  });

  await page.goto("/projects/research-lab");
  await expect(page.getByTestId("project-home")).toBeVisible();
  await expect(page.getByText("共享 Agent")).toBeVisible();
  await expect(page.getByText("共享 Skill")).toBeVisible();
  await expect(page.getByText("共享 MCP")).toBeVisible();
  const privateWork = page.getByRole("button", { name: "开始私有对话" });
  await expect(privateWork).toBeDisabled();
  const beforeUrl = page.url();
  await privateWork.dispatchEvent("click");
  await expect(page).toHaveURL(beforeUrl);
  expect(threadRequests).toEqual([]);
  expect(api.enterPaths()).toEqual([`/api/projects/${PROJECT_ID}/enter`]);
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          document.documentElement.scrollWidth -
          document.documentElement.clientWidth,
      ),
    )
    .toBeLessThanOrEqual(0);
  await expect
    .poll(() =>
      page.evaluate(() => document.documentElement.classList.contains("dark")),
    )
    .toBe(true);
});
