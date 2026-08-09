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
  options: { budgetRewrite?: boolean } = {},
) {
  const budgetRewrite = options.budgetRewrite ?? false;
  let document = {
    content:
      "# Working preferences\n\nPrefers executable implementation plans.",
    version: 3,
    updatedAt: TIMESTAMP,
    pendingCount: budgetRewrite ? 0 : 2,
    dreamRunning: false,
    injectionStatus: budgetRewrite ? "skipped_over_budget" : "ok",
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
      return json(route, { items: [project], next_cursor: null });
    }
    if (
      path === `/api/projects/${PROJECT_ID}/enter` &&
      request.method() === "POST"
    ) {
      return json(route, project);
    }

    const memoryBase = `/api/projects/${PROJECT_ID}/memory`;
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
      return json(route, { items: [] });
    }
    if (path === `${memoryBase}/versions` && request.method() === "GET") {
      return json(route, {
        items: [...details.values()]
          .sort((left, right) => right.version - left.version)
          .map(
            ({ content: _content, unifiedDiff: _unifiedDiff, ...summary }) =>
              summary,
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
      if (budgetRewrite) {
        const compressed = {
          version: 5,
          trigger: "budget_rewrite",
          historyCount: 0,
          changed: true,
          needsReview: false,
          createdAt: TIMESTAMP,
          content: "# Working preferences\n\nExecutable plans.",
          unifiedDiff:
            "@@ -1,3 +1,3 @@\n # Working preferences\n \n-Prefers executable implementation plans.\n+Executable plans.",
        } as const;
        details.set(5, compressed);
        document = {
          ...document,
          content: compressed.content,
          version: compressed.version,
          injectionStatus: "ok",
        };
        return json(
          route,
          {
            disposition: "queued",
            historyCount: 0,
            jobId: JOB_ID,
            admissionKind: "budget_rewrite",
          },
          202,
        );
      }
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
}

test("the Memory document page organizes, diffs and restores versions", async ({
  page,
}) => {
  await mockProjectMemoryRoute(page);

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

  await expect(page.getByText("Latest version needs review")).toBeVisible();
  await page.getByText("Version history", { exact: true }).click();
  await page.getByRole("button", { name: /Version 3/u }).click();
  await expect(
    page.getByRole("heading", { name: "Document change" }),
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

test("the remember link target focuses the pending memory section", async ({
  page,
}) => {
  await mockProjectMemoryRoute(page);

  await page.goto("/projects/alpha/memory#memory-pending");

  const pending = page.locator("#memory-pending");
  await expect(pending).toBeVisible();
  await expect(pending).toBeFocused();
  await expect(
    pending.getByText("Deployment target is region-eu"),
  ).toBeVisible();
});

test("an over-budget document can start a zero-history rescue rewrite", async ({
  page,
}) => {
  await mockProjectMemoryRoute(page, { budgetRewrite: true });

  await page.goto("/projects/alpha/memory");

  await expect(
    page.getByText("Memory document exceeds the injection budget"),
  ).toBeVisible();
  await page.getByRole("button", { name: "Compress document now" }).click();
  await expect(
    page.getByText(
      "Started compressing the Memory document into the current injection budget.",
    ),
  ).toBeVisible();
  await expect(
    page.getByText("Memory document exceeds the injection budget"),
  ).toHaveCount(0);
  await expect(page.getByText("Executable plans.")).toBeVisible();
});
