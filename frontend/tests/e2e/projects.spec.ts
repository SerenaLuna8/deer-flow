import { expect, test, type Page, type Route } from "@playwright/test";

import type { Project } from "@/core/projects/types";

import { mockLangGraphAPI } from "./utils/mock-api";

const PROJECT_ID = "10000000-0000-4000-8000-000000000001";
const SECOND_PROJECT_ID = "10000000-0000-4000-8000-000000000002";
const TOKEN_SERIES_START = Date.parse("2026-07-26T13:00:00.000Z");

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
  quota_summary: {
    members: { used: 4, reserved: 0, limit: 20 },
    storage_bytes: { used: 0, reserved: 0, limit: 5_368_709_120 },
    concurrent_runs: { used: 0, reserved: 0, limit: 3 },
    mcp_calls_daily: { used: 0, reserved: 0, limit: 10_000 },
  },
  status: "active",
  is_suspended: false,
  membership_version: 1,
  request_id: "request-initial",
};

function makeTokenUsageSeries() {
  const points = Array.from({ length: 24 }, (_, index) => ({
    bucket_start: new Date(
      TOKEN_SERIES_START + index * 60 * 60 * 1000,
    ).toISOString(),
    input_tokens: index === 22 ? 120 : index === 23 ? 80 : 0,
    output_tokens: index === 22 ? 30 : index === 23 ? 20 : 0,
    total_tokens: index === 22 ? 170 : index === 23 ? 130 : 0,
  }));
  return {
    window_start: points[0]!.bucket_start,
    window_end: new Date(
      TOKEN_SERIES_START + (23 * 60 + 30) * 60 * 1000,
    ).toISOString(),
    bucket_minutes: 60,
    totals: {
      input_tokens: 200,
      output_tokens: 50,
      total_tokens: 300,
    },
    points,
  };
}

type ProjectMock = {
  projects: () => Project[];
  listRequests: () => URL[];
  enterPaths: () => string[];
  failNextList: (count?: number) => void;
  holdEnters: () => void;
  releaseEnters: () => void;
  refreshLookup: (requestId: string) => void;
  refreshMembership: (
    membershipVersion: number,
    capabilities: Project["capabilities"],
  ) => void;
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
  let enterGate: Promise<void> | null = null;
  let releaseEnterGate: (() => void) | null = null;

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
      const project = structuredClone(projects.find((item) => item.id === id)!);
      await enterGate;
      const entered = projectResponse(
        { ...project, last_entered_at: "2026-07-12T08:00:00+00:00" },
        "request-enter",
      );
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
    failNextList: (count = 4) => {
      listFailuresRemaining = count;
    },
    holdEnters: () => {
      enterGate = new Promise((resolve) => {
        releaseEnterGate = resolve;
      });
    },
    releaseEnters: () => {
      releaseEnterGate?.();
      enterGate = null;
      releaseEnterGate = null;
    },
    refreshLookup: (requestId) => {
      projects = projects.map((project) => projectResponse(project, requestId));
    },
    refreshMembership: (membershipVersion, capabilities) => {
      projects = projects.map((project) => ({
        ...project,
        membership_version: membershipVersion,
        capabilities,
        request_id: `request-membership-${membershipVersion}`,
      }));
    },
  };
}

async function reconnect(page: Page) {
  await page.evaluate(() => {
    window.dispatchEvent(new Event("offline"));
    window.dispatchEvent(new Event("online"));
  });
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
  await page.getByRole("button", { name: "账户" }).click();
  await expect(
    page.getByRole("menuitem", { name: "平台管理" }),
  ).toHaveAttribute("href", "/admin/operations");
  await page.getByRole("menuitem", { name: "系统设置" }).click();
  const settingsDialog = page.getByRole("dialog", { name: "Settings" });
  await expect(settingsDialog).toBeVisible();
  const systemTheme = settingsDialog.getByRole("button", {
    name: /System.*Match the operating system preference automatically/u,
  });
  await expect(systemTheme).toBeVisible();
  await expect(
    settingsDialog.getByRole("button", { name: /Light.*Bright palette/u }),
  ).toBeVisible();
  const darkTheme = settingsDialog.getByRole("button", {
    name: /Dark.*Dim palette/u,
  });
  await expect(darkTheme).toBeVisible();
  await darkTheme.click();
  await expect(page.locator("html")).toHaveClass(/dark/u);
  await systemTheme.click();
  await page.keyboard.press("Escape");
});

test("desktop project menu collapses to icon navigation and expands again", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  mockLangGraphAPI(page);
  await mockProjectsAPI(page);

  await page.goto("/projects/research-lab");
  await expect(page.getByTestId("project-home")).toBeVisible();

  const menu = page.getByRole("complementary", { name: "项目菜单栏" });
  await expect(menu).toHaveAttribute("data-state", "expanded");
  await expect(menu).toHaveCSS("width", "256px");
  await expect(menu.getByRole("link", { name: "项目概览" })).toBeVisible();

  await menu.getByRole("button", { name: "收起菜单栏" }).click();

  await expect(menu).toHaveAttribute("data-state", "collapsed");
  await expect(menu).toHaveCSS("width", "56px");
  await expect(menu.getByRole("button")).toHaveCount(1);
  const collapsedOverview = menu.getByRole("link", { name: "项目概览" });
  await expect(collapsedOverview).toBeVisible();
  await expect(collapsedOverview).toHaveAttribute("title", "项目概览");
  await expect(collapsedOverview).toHaveAttribute("aria-current", "page");
  await expect(collapsedOverview).not.toContainText("项目概览");
  await expect(menu.getByRole("link", { name: "返回工作空间" })).toBeVisible();

  await menu.getByRole("button", { name: "展开菜单栏" }).click();

  await expect(menu).toHaveAttribute("data-state", "expanded");
  await expect(menu).toHaveCSS("width", "256px");
  await expect(menu.getByRole("link", { name: "项目概览" })).toContainText(
    "项目概览",
  );
});

test("project token trend shows the hourly breakdown on hover and keyboard focus", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  await mockProjectsAPI(page, [
    {
      ...baseProject,
      capabilities: [...baseProject.capabilities, "project.usage.read"],
    },
  ]);
  await page.route(
    `**/api/projects/${PROJECT_ID}/usage/token-series`,
    (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(makeTokenUsageSeries()),
      }),
  );

  await page.goto("/projects/research-lab");
  const chart = page.getByTestId("project-token-usage");
  await expect(chart).toBeVisible();

  const latestPoint = chart.locator('[data-token-usage-index="23"]');
  await latestPoint.hover();
  const tooltip = page.getByRole("tooltip");
  await expect(tooltip).toBeVisible();
  await expect(tooltip).toContainText(/(?:总计|Total)\s*130/u);
  await expect(tooltip).toContainText(/(?:输入|Input)\s*80/u);
  await expect(tooltip).toContainText(/(?:输出|Output)\s*20/u);

  await page.mouse.move(0, 0);
  await expect(tooltip).toBeHidden();

  await latestPoint.focus();
  await expect(tooltip).toBeVisible();
  await expect(tooltip).toContainText(/(?:总计|Total)\s*130/u);
  await expect(tooltip).toContainText(/(?:输入|Input)\s*80/u);
  await expect(tooltip).toContainText(/(?:输出|Output)\s*20/u);
});

test("desktop project menu stays fixed while long project content scrolls", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  mockLangGraphAPI(page);
  await mockProjectsAPI(page, [
    {
      ...baseProject,
      capabilities: [...baseProject.capabilities, "project.usage.read"],
    },
  ]);
  await page.route(
    `**/api/projects/${PROJECT_ID}/usage/token-series`,
    (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(makeTokenUsageSeries()),
      }),
  );

  await page.goto("/projects/research-lab");
  const projectHome = page.getByTestId("project-home");
  await expect(projectHome).toBeVisible();
  await expect(page.getByTestId("project-token-usage")).toBeVisible();

  const menu = page.getByRole("complementary", { name: "项目菜单栏" });
  const account = menu.getByRole("button", { name: "账户" });
  const menuBefore = await menu.boundingBox();
  const accountBefore = await account.boundingBox();

  expect(menuBefore).not.toBeNull();
  expect(accountBefore).not.toBeNull();
  await page.evaluate(() =>
    window.scrollTo(0, document.documentElement.scrollHeight),
  );
  await expect
    .poll(() => page.evaluate(() => window.scrollY))
    .toBeGreaterThan(100);

  const menuAfter = await menu.boundingBox();
  const accountAfter = await account.boundingBox();
  expect(menuAfter).not.toBeNull();
  expect(accountAfter).not.toBeNull();
  expect(Math.abs(menuAfter!.y - menuBefore!.y)).toBeLessThanOrEqual(1);
  expect(Math.abs(menuAfter!.y + menuAfter!.height - 720)).toBeLessThanOrEqual(
    1,
  );
  expect(Math.abs(accountAfter!.y - accountBefore!.y)).toBeLessThanOrEqual(1);
  expect(accountAfter!.y + accountAfter!.height).toBeLessThanOrEqual(720);

  await account.click();
  await expect(page.getByRole("menuitem", { name: "系统设置" })).toBeVisible();
});

test("project workbench supports create, search, pin, edit, enter, and return", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  const api = await mockProjectsAPI(page, [
    {
      ...baseProject,
      capabilities: [
        ...baseProject.capabilities,
        "project.members.manage",
        "project.lifecycle.manage",
      ],
    },
  ]);

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
  await page.getByRole("button", { name: "清除筛选" }).click();

  const researchCard = page
    .getByTestId("project-card")
    .filter({ hasText: "Research Lab" });
  await researchCard.getByRole("link", { name: "进入项目" }).click();
  await expect(page).toHaveURL(/\/projects\/research-lab$/);
  await expect(page.getByTestId("project-home")).toBeVisible();
  await expect(page.getByRole("link", { name: "项目概览" })).toBeVisible();
  await expect(page.getByRole("link", { name: "项目成员" })).toBeVisible();
  await expect(page.getByRole("link", { name: "项目设置" })).toBeVisible();
  await page.getByRole("button", { name: "账户" }).click();
  await expect(
    page.getByRole("menuitem", { name: "平台管理" }),
  ).toHaveAttribute("href", "/admin/operations");
  await expect(page.getByRole("menuitem", { name: "系统设置" })).toBeVisible();
  await page.getByRole("menuitem", { name: "系统设置" }).click();
  await expect(page.getByRole("dialog", { name: "Settings" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("link", { name: "Agent" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Skill" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "MCP" })).toHaveCount(0);
  expect(api.enterPaths()).toEqual([`/api/projects/${PROJECT_ID}/enter`]);
  expect(api.listRequests().at(-1)?.searchParams.get("query")).toBe(
    "research-lab",
  );

  await page.getByRole("link", { name: "项目成员" }).click();
  await expect(page).toHaveURL(/\/projects\/research-lab\/members$/);
  await expect(
    page.getByRole("heading", { name: "成员与邀请", level: 1 }),
  ).toBeVisible();
  expect(api.enterPaths()).toEqual([`/api/projects/${PROJECT_ID}/enter`]);

  await page.getByRole("link", { name: "项目设置" }).click();
  await expect(page).toHaveURL(/\/projects\/research-lab\/settings$/);
  await expect(
    page.getByRole("heading", { name: "项目设置", level: 1 }),
  ).toBeVisible();
  expect(api.enterPaths()).toEqual([`/api/projects/${PROJECT_ID}/enter`]);

  await page.getByRole("link", { name: "返回工作空间" }).click();
  await expect(page).toHaveURL(/\/workspace$/);
});

test("project workbench exposes loading-safe API error and retry", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  const api = await mockProjectsAPI(page, []);
  // Active and recoverable workspace lists each retry three times. Fail both
  // initial requests plus their retries so the active grid reaches its error.
  api.failNextList(8);

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

test("project context enters a stable identity once across lookup refreshes", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  const api = await mockProjectsAPI(page);
  api.holdEnters();

  await page.goto("/projects/research-lab");
  await expect
    .poll(() => api.enterPaths())
    .toEqual([`/api/projects/${PROJECT_ID}/enter`]);

  api.refreshLookup("request-pending-refresh");
  const pendingListCount = api.listRequests().length;
  await reconnect(page);
  await expect
    .poll(() => api.listRequests().length)
    .toBeGreaterThan(pendingListCount);
  await page.waitForTimeout(200);
  expect(api.enterPaths()).toEqual([`/api/projects/${PROJECT_ID}/enter`]);

  api.releaseEnters();
  await expect(page.getByTestId("project-home")).toBeVisible();
  expect(api.enterPaths()).toEqual([`/api/projects/${PROJECT_ID}/enter`]);

  api.refreshLookup("request-completed-refresh");
  const completedListCount = api.listRequests().length;
  await reconnect(page);
  await expect
    .poll(() => api.listRequests().length)
    .toBeGreaterThan(completedListCount);
  await page.waitForTimeout(200);
  expect(api.enterPaths()).toEqual([`/api/projects/${PROJECT_ID}/enter`]);
});

test("project context re-enters on membership version changes and rejects the old enter result", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  const api = await mockProjectsAPI(page);
  api.holdEnters();

  await page.goto("/projects/research-lab");
  await expect.poll(() => api.enterPaths()).toHaveLength(1);

  api.refreshMembership(2, ["project.read", "project.enter"]);
  const listCount = api.listRequests().length;
  await reconnect(page);
  await expect.poll(() => api.listRequests().length).toBeGreaterThan(listCount);
  await expect.poll(() => api.enterPaths()).toHaveLength(2);

  api.releaseEnters();
  await expect(page.getByTestId("project-home")).toBeVisible();
  await expect(page.getByRole("link", { name: "项目设置" })).toHaveCount(0);
});

test("project home omits private-work overview sections in dark mobile layout", async ({
  page,
}) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.emulateMedia({ colorScheme: "dark" });
  mockLangGraphAPI(page);
  const readyProject: Project = {
    ...baseProject,
    capabilities: [
      ...baseProject.capabilities,
      "project.members.manage",
      "shared_assets.read",
      "shared_assets.execute",
      "private_work.create",
      "private_work.read_own",
    ],
  };
  const api = await mockProjectsAPI(page, [readyProject]);
  await page.route(
    `**/api/projects/${PROJECT_ID}/private-work/readiness`,
    (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          status: "ready",
          code: "PRIVATE_WORK_READY",
          request_id: "req-ready",
        }),
      }),
  );
  const threadRequests: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.includes("/threads")) {
      threadRequests.push(new URL(request.url()).pathname);
    }
  });

  await page.goto("/projects/research-lab");
  await expect(page.getByTestId("project-home")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "打开项目导航" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "打开项目导航" }).click();
  const mobileNavigation = page.getByRole("navigation", {
    name: "项目导航",
  });
  await expect(
    mobileNavigation.getByRole("link", { name: "项目成员" }),
  ).toBeVisible();
  await expect(
    mobileNavigation.getByRole("region", { name: "能力" }).getByRole("link"),
  ).toHaveText(["Agent", "Skill", "MCP", "Memory"]);
  await expect(
    mobileNavigation
      .getByRole("region")
      .getByRole("link", { name: "项目概览" }),
  ).toHaveCount(0);
  await expect(page.getByRole("link", { name: "项目设置" })).toBeVisible();
  await expect(page.getByRole("link", { name: "返回工作空间" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("heading", { name: "项目隐私边界" })).toHaveCount(
    0,
  );
  await expect(
    page.getByText("对话和记忆私有，Agent、Skill 和 MCP 共享。"),
  ).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "最近私有对话" })).toHaveCount(
    0,
  );
  await expect(page.getByRole("button", { name: "开始私有对话" })).toHaveCount(
    0,
  );
  await expect(page.getByRole("heading", { name: "共享资产" })).toBeVisible();
  await expect(page.getByText("共享 Agent")).toBeVisible();
  await expect(page.getByText("共享 Skill")).toBeVisible();
  await expect(page.getByText("共享 MCP")).toBeVisible();
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
