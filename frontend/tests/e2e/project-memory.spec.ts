import { expect, test, type Page, type Route } from "@playwright/test";

import type { Capability, Project } from "@/core/projects/types";

const ACCOUNT_ID = "90000000-0000-4000-8000-000000000001";
const PROJECT_ID = "10000000-0000-4000-8000-000000000001";
const JOB_ID = "20000000-0000-4000-8000-000000000001";
const TIMESTAMP = "2026-08-05T00:00:00Z";
const capabilities: Capability[] = [
  "project.read",
  "project.enter",
  "private_work.read_own",
  "private_work.create",
  "shared_assets.execute",
];
const project: Project = {
  id: PROJECT_ID,
  slug: "alpha",
  display_name: "Alpha Project",
  description: "Project memory route",
  icon: "folder",
  role: "admin",
  capabilities,
  is_pinned: false,
  last_entered_at: null,
  member_count: 1,
  agent_count: 1,
  skill_count: 0,
  mcp_count: 0,
  quota_summary: {
    members: { used: 1, reserved: 0, limit: 20 },
    storage_bytes: { used: 0, reserved: 0, limit: 5_368_709_120 },
    concurrent_runs: { used: 0, reserved: 0, limit: 3 },
    mcp_calls_daily: { used: 0, reserved: 0, limit: 10_000 },
  },
  status: "active",
  is_suspended: false,
  membership_version: 1,
  request_id: "request-alpha",
};

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function mockProjectMemoryRoute(
  page: Page,
  options: { capabilities?: Capability[] } = {},
) {
  const routedProject = {
    ...project,
    capabilities: options.capabilities ?? project.capabilities,
  };
  const memoryRequests: string[] = [];
  let document = {
    content:
      "# Working preferences\n\nPrefers executable implementation plans.",
    version: 3,
    updatedAt: TIMESTAMP,
    pendingCount: 2,
    dreamRunning: false,
    injectionStatus: "ok",
  };
  const details = new Map<
    number,
    {
      version: number;
      trigger: "auto_dream" | "manual_dream" | "restore" | "budget_rewrite";
      historyCount: number | null;
      changed: boolean;
      needsReview: boolean;
      createdAt: string;
      content: string;
      unifiedDiff: string;
      diffTruncated: boolean;
    }
  >([
    [
      3,
      {
        version: 3,
        trigger: "manual_dream",
        historyCount: 2,
        changed: true,
        needsReview: true,
        createdAt: TIMESTAMP,
        content: document.content,
        unifiedDiff:
          "@@ -1,1 +1,3 @@\n+# Working preferences\n+\n+Prefers executable implementation plans.",
        diffTruncated: true,
      },
    ],
    [
      2,
      {
        version: 2,
        trigger: "auto_dream",
        historyCount: 1,
        changed: false,
        needsReview: false,
        createdAt: "2026-08-04T00:00:00Z",
        content: "# Working preferences",
        unifiedDiff: "",
        diffTruncated: false,
      },
    ],
  ]);

  await page.route("**/api/**", (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;

    if (path === "/api/v1/auth/me") {
      return json(route, {
        id: ACCOUNT_ID,
        email: "owner@example.test",
        username: "owner",
        system_role: "user",
        needs_setup: false,
        oauth_provider: null,
      });
    }
    if (path === "/api/v1/auth/setup-status") {
      return json(route, {
        needs_setup: false,
        registration_enabled: true,
      });
    }
    if (path === "/api/projects" && request.method() === "GET") {
      return json(route, { items: [routedProject], next_cursor: null });
    }
    if (
      path === `/api/projects/${PROJECT_ID}/enter` &&
      request.method() === "POST"
    ) {
      return json(route, routedProject);
    }

    const memoryBase = `/api/projects/${PROJECT_ID}/memory`;
    if (path === memoryBase || path.startsWith(`${memoryBase}/`)) {
      memoryRequests.push(path);
    }
    if (path === memoryBase && request.method() === "GET") {
      return json(route, document);
    }
    if (path === `${memoryBase}/pending` && request.method() === "GET") {
      return json(route, {
        items: [
          {
            sequence: 1,
            origin: "tool",
            taggedText: "- [durable] Deployment target is region-eu",
            createdAt: TIMESTAMP,
          },
        ],
      });
    }
    if (path === `${memoryBase}/episodes` && request.method() === "GET") {
      return json(route, { items: [], nextCursor: null });
    }
    if (path === `${memoryBase}/versions` && request.method() === "GET") {
      return json(route, {
        items: [...details.values()]
          .sort((left, right) => right.version - left.version)
          .map(
            ({
              content: _content,
              unifiedDiff: _unifiedDiff,
              diffTruncated: _diffTruncated,
              ...summary
            }) => summary,
          ),
      });
    }
    const detailMatch = new RegExp(
      `^${memoryBase}/versions/([1-9][0-9]*)$`,
      "u",
    ).exec(path);
    if (detailMatch && request.method() === "GET") {
      const detail = details.get(Number(detailMatch[1]));
      return detail
        ? json(route, detail)
        : json(route, { detail: "not found" }, 404);
    }
    const restoreMatch = new RegExp(
      `^${memoryBase}/versions/([1-9][0-9]*)/restore$`,
      "u",
    ).exec(path);
    if (restoreMatch && request.method() === "POST") {
      const body = request.postDataJSON() as {
        expectedCurrentVersion?: number;
      };
      if (body.expectedCurrentVersion !== document.version) {
        return json(route, { detail: "Memory changed" }, 409);
      }
      const source = details.get(Number(restoreMatch[1]));
      if (!source) return json(route, { detail: "not found" }, 404);
      const restored = {
        version: 4,
        trigger: "restore",
        historyCount: null,
        changed: true,
        needsReview: false,
        createdAt: TIMESTAMP,
        content: source.content,
        unifiedDiff:
          "@@ -1,3 +1,1 @@\n # Working preferences\n-\n-Prefers executable implementation plans.",
        diffTruncated: false,
      } as const;
      details.set(4, restored);
      document = {
        ...document,
        content: restored.content,
        version: 4,
      };
      return json(route, restored);
    }
    if (path === `${memoryBase}/dream` && request.method() === "POST") {
      const organized = {
        version: 5,
        trigger: "manual_dream",
        historyCount: 2,
        changed: true,
        needsReview: false,
        createdAt: TIMESTAMP,
        content: `${document.content}\n\nPrefers explicit acceptance checks.`,
        unifiedDiff:
          "@@ -1 +1,3 @@\n # Working preferences\n+\n+Prefers explicit acceptance checks.",
        diffTruncated: false,
      } as const;
      details.set(5, organized);
      document = {
        ...document,
        content: organized.content,
        version: 5,
        pendingCount: 0,
      };
      return json(
        route,
        { disposition: "queued", historyCount: 2, jobId: JOB_ID },
        202,
      );
    }
    return json(route, { detail: "not found" }, 404);
  });
  return { memoryRequests };
}

test("keeps the account menu usable after collapsing the project sidebar", async ({
  page,
}) => {
  await mockProjectMemoryRoute(page);
  await page.goto("/projects/alpha/memory");

  const projectMenu = page.getByRole("complementary", {
    name: "Project menu",
  });
  await projectMenu
    .getByRole("button", { name: "Collapse project menu" })
    .click();

  await expect(projectMenu).toHaveAttribute("data-state", "collapsed");
  const accountMenu = projectMenu.getByRole("button", { name: "Account" });
  await expect(accountMenu).toBeVisible();
  await expect(accountMenu).toBeInViewport();
  await accountMenu.click();
  await expect(
    page.getByRole("menuitem", { name: "System settings" }),
  ).toBeVisible();
});

test("the Memory document page organizes, diffs and restores versions", async ({
  page,
}) => {
  const { memoryRequests } = await mockProjectMemoryRoute(page);

  await page.goto("/projects/alpha/memory");

  await expect(page).toHaveURL(/\/projects\/alpha\/memory$/u);
  await expect(page.getByTestId("project-shell")).toBeVisible();
  await expect(
    page.getByRole("tab", { name: "Current memory", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("MEMORY.md", { exact: true })).toBeVisible();
  await expect(
    page.getByText("Prefers executable implementation plans."),
  ).toBeVisible();
  await expect(page.getByText("2 items", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Organize now" }),
  ).toBeEnabled();
  expect(
    memoryRequests.filter(
      (path) => path === `/api/projects/${PROJECT_ID}/memory/versions`,
    ),
  ).toHaveLength(1);

  await expect(page.getByText("Latest version needs review")).toBeVisible();
  await page.getByText("Version history", { exact: true }).click();
  await page.getByRole("button", { name: /Version 3/u }).click();
  await expect(
    page.getByRole("heading", { name: "Document change" }),
  ).toBeVisible();
  await expect(page.getByText("Document change is truncated")).toBeVisible();
  await expect(
    page.getByText(
      "Up to the first 64,000 characters are shown on complete line boundaries",
      {
        exact: false,
      },
    ),
  ).toBeVisible();
  await expect(
    page.getByText("+Prefers executable implementation plans."),
  ).toBeVisible();

  await page.getByRole("button", { name: "Restore this version" }).click();
  await expect(
    page.getByRole("heading", { name: "Restore version 3?" }),
  ).toBeVisible();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Restore" })
    .click();
  await expect(page).toHaveURL(/\/memory\?version=4$/u);

  await page.getByRole("button", { name: "Close" }).click();
  await page.getByRole("button", { name: "Organize now" }).click();
  await expect(page.getByText("0 items", { exact: true })).toBeVisible();
  await expect(
    page.getByText("Prefers explicit acceptance checks."),
  ).toBeVisible();
});

test("a direct Memory URL shows 403 without mounting Memory queries", async ({
  page,
}) => {
  const { memoryRequests } = await mockProjectMemoryRoute(page, {
    capabilities: ["project.read", "project.enter"],
  });

  await page.goto("/projects/alpha/memory");

  const denied = page.locator('[data-error-status="403"]');
  await expect(denied).toHaveAttribute("role", "alert");
  await expect(
    page.getByRole("heading", { name: "You do not have access" }),
  ).toBeVisible();
  await expect(
    page.getByText("cannot access Memory", { exact: false }),
  ).toBeVisible();
  await expect(page.getByText("No historical versions yet.")).toHaveCount(0);
  expect(memoryRequests).toEqual([]);
});
