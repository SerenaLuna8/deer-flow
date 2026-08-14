import { expect, test, type Page, type Route } from "@playwright/test";

import type { Project } from "@/core/projects/types";
import type { AssetVersion, ProjectAssetItem } from "@/core/shared-assets";

type SkillAssetVersion = Extract<AssetVersion, { skill_id: string }>;

const ACCOUNT_ID = "90000000-0000-4000-8000-000000000001";
const PROJECT_ID = "10000000-0000-4000-8000-000000000001";
const ASSET_ID = "20000000-0000-4000-8000-000000000001";
const VERSION_ID = "30000000-0000-4000-8000-000000000001";
const TIMESTAMP = "2026-08-13T00:00:00Z";

const project: Project = {
  id: PROJECT_ID,
  slug: "alpha",
  display_name: "Alpha Project",
  description: "System Skill detail browser coverage",
  icon: "folder",
  role: "admin",
  capabilities: [
    "project.read",
    "project.enter",
    "shared_assets.read",
    "shared_assets.manage_bindings",
  ],
  is_pinned: false,
  last_entered_at: null,
  member_count: 1,
  agent_count: 0,
  skill_count: 1,
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

const systemSkill: ProjectAssetItem = {
  id: ASSET_ID,
  scope: "system",
  project_id: null,
  slug: "system-skill",
  display_name: "System Skill",
  description: "A packaged System Skill used by this project.",
  status: "active",
  current_published_version_id: VERSION_ID,
  version: 1,
  capabilities: ["shared_assets.read", "shared_assets.manage_bindings"],
  binding: null,
  created_by_user_id: "system",
  created_at: TIMESTAMP,
  updated_at: TIMESTAMP,
};

const systemSkillVersion: SkillAssetVersion = {
  id: VERSION_ID,
  skill_id: ASSET_ID,
  version_number: 1,
  workflow_status: "published",
  description: "Published System Skill version.",
  frontmatter: { name: "system-skill" },
  compatibility: null,
  secret_requirements: [],
  scan_decision: "allow",
  scan_rule_ids: [],
  scan_summary: { result: "clean" },
  file_views: [],
  supersedes_version_id: null,
  payload_checksum: "sha256:system-skill-v1",
  revoked_at: null,
  revoked_by_user_id: null,
  revocation_reason_code: null,
  governance_status: "active",
  binding_eligible: true,
  created_by_user_id: "system",
  created_at: TIMESTAMP,
};

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function mockProjectSystemSkills(page: Page) {
  const unexpectedRequests: string[] = [];

  await page.route("**/api/**", (route) => {
    const request = route.request();
    const method = request.method();
    const url = new URL(request.url());
    const path = url.pathname;

    if (path === "/api/v1/auth/me" && method === "GET") {
      return json(route, {
        id: ACCOUNT_ID,
        email: "owner@example.test",
        username: "owner",
        system_role: "user",
        needs_setup: false,
        oauth_provider: null,
      });
    }
    if (path === "/api/v1/auth/setup-status" && method === "GET") {
      return json(route, {
        needs_setup: false,
        registration_enabled: true,
      });
    }
    if (path === "/api/projects" && method === "GET") {
      return json(route, { items: [project], next_cursor: null });
    }
    if (path === `/api/projects/${PROJECT_ID}/enter` && method === "POST") {
      return json(route, project);
    }
    if (path === `/api/projects/${PROJECT_ID}/skills` && method === "GET") {
      return json(route, {
        system_items: [systemSkill],
        project_items: [],
        request_id: "request-skills",
      });
    }
    if (
      path === `/api/projects/${PROJECT_ID}/skills/${ASSET_ID}/versions` &&
      method === "GET"
    ) {
      return json(route, {
        data: [systemSkillVersion],
        request_id: "request-skill-versions",
      });
    }
    if (
      path ===
        `/api/projects/${PROJECT_ID}/skills/${ASSET_ID}/credential-bindings` &&
      method === "GET"
    ) {
      return json(route, {
        skill_id: ASSET_ID,
        skill_version_id: VERSION_ID,
        revision: 0,
        requirements: [],
        request_id: "request-skill-credential-bindings",
      });
    }

    unexpectedRequests.push(`${method} ${path}${url.search}`);
    return json(route, { detail: "unexpected browser-test request" }, 599);
  });

  return { unexpectedRequests };
}

test("System Skill details own version management and survive refresh", async ({
  page,
}) => {
  const { unexpectedRequests } = await mockProjectSystemSkills(page);

  await page.goto("/projects/alpha/skills");

  const detailTrigger = page.getByRole("button", {
    name: "查看 System Skill 详情",
  });
  await expect(detailTrigger).toBeVisible();

  const systemSkillList = page.getByRole("list").filter({ has: detailTrigger });
  await expect(
    systemSkillList.getByRole("button", { name: /管理.*版本/u }),
  ).toHaveCount(0);

  await detailTrigger.click();

  await expect(page).toHaveURL(`/projects/alpha/skills?skill_id=${ASSET_ID}`);
  const detail = page.getByRole("dialog", { name: "System Skill" });
  await expect(detail).toBeVisible();
  await expect(
    detail.getByRole("button", { name: /管理.*版本/u }),
  ).toBeVisible();

  await page.reload();

  await expect(page).toHaveURL(`/projects/alpha/skills?skill_id=${ASSET_ID}`);
  await expect(detail).toBeVisible();
  await expect(
    detail.getByRole("button", { name: /管理.*版本/u }),
  ).toBeVisible();

  await detail.getByRole("button", { name: "关闭详情" }).click();

  await expect(detail).toHaveCount(0);
  await expect(page).toHaveURL("/projects/alpha/skills");
  expect(unexpectedRequests).toEqual([]);
});
