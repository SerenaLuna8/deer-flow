import { expect, test, type Page, type Route } from "@playwright/test";

import type { Project } from "@/core/projects/types";
import type { AssetVersion, ProjectAssetItem } from "@/core/shared-assets";

type SkillAssetVersion = Extract<AssetVersion, { skill_id: string }>;

const ACCOUNT_ID = "90000000-0000-4000-8000-000000000001";
const PROJECT_ID = "10000000-0000-4000-8000-000000000001";
const ASSET_ID = "20000000-0000-4000-8000-000000000001";
const VERSION_ID = "30000000-0000-4000-8000-000000000001";
const CREDENTIAL_ID = "40000000-0000-4000-8000-000000000001";
const CREDENTIAL_VERSION_ID = "50000000-0000-4000-8000-000000000001";
const SYSTEM_TOKEN = "SYSTEM_TOKEN";
const TIMESTAMP = "2026-08-13T00:00:00Z";
const SYSTEM_SKILL_CONTENT = `---\nname: system-skill\ndescription: Published System Skill version.\nrequired-secrets:\n  - name: ${SYSTEM_TOKEN}\n    optional: false\n---\n\n# System Skill\n`;
const SYSTEM_SKILL_SHA = "a".repeat(64);

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
    "shared_assets.edit",
    "shared_assets.manage_bindings",
    "mcp.credentials.approve",
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
  current_version_id: VERSION_ID,
  revision: 1,
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
  relation: "current",
  description: "Published System Skill version.",
  frontmatter: {
    name: "system-skill",
    "required-secrets": [{ name: SYSTEM_TOKEN, optional: false }],
  },
  compatibility: null,
  secret_requirements: [{ name: SYSTEM_TOKEN, optional: false }],
  scan_decision: "allow",
  scan_rule_ids: [],
  scan_summary: { result: "clean" },
  file_views: [
    {
      path: "SKILL.md",
      media_type: "text/markdown",
      size_bytes: Buffer.byteLength(SYSTEM_SKILL_CONTENT, "utf8"),
      sha256: SYSTEM_SKILL_SHA,
    },
  ],
  supersedes_version_id: null,
  payload_checksum: SYSTEM_SKILL_SHA,
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
  const exportRequests: string[] = [];
  const bindingBodies: unknown[] = [];
  let bindingRevision = 0;
  let bindingConfigured = false;

  function bindingResponse() {
    return {
      skill_id: ASSET_ID,
      skill_version_id: VERSION_ID,
      revision: bindingRevision,
      requirements: [
        {
          name: SYSTEM_TOKEN,
          optional: false,
          configured: bindingConfigured,
          mapping_status: bindingConfigured ? "configured" : "missing",
          credential_id: bindingConfigured ? CREDENTIAL_ID : null,
          credential_version_id: bindingConfigured
            ? CREDENTIAL_VERSION_ID
            : null,
          credential_display_name: bindingConfigured
            ? "System Skill token"
            : null,
          credential_version_number: bindingConfigured ? 1 : null,
          source_env_field_name: bindingConfigured ? SYSTEM_TOKEN : null,
          eligible_credentials: [
            {
              credential_id: CREDENTIAL_ID,
              credential_version_id: CREDENTIAL_VERSION_ID,
              display_name: "System Skill token",
              version_number: 1,
              env_fields: [SYSTEM_TOKEN],
            },
          ],
        },
      ],
      request_id: `request-skill-credential-bindings-${bindingRevision}`,
    };
  }

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
    if (
      path === `/api/projects/${PROJECT_ID}/skill-builder/sessions` &&
      method === "GET"
    ) {
      return json(route, { data: [], request_id: "builder-sessions" });
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
        `/api/projects/${PROJECT_ID}/skills/${ASSET_ID}/versions/${VERSION_ID}/files/content` &&
      method === "GET"
    ) {
      return json(route, {
        data: {
          path: "SKILL.md",
          media_type: "text/markdown",
          size_bytes: Buffer.byteLength(SYSTEM_SKILL_CONTENT, "utf8"),
          sha256: SYSTEM_SKILL_SHA,
          preview_status: "ready",
          encoding: "utf-8",
          content: SYSTEM_SKILL_CONTENT,
          source_payload_checksum: SYSTEM_SKILL_SHA,
          asset_version: systemSkill.revision,
        },
        request_id: "request-system-skill-file",
      });
    }
    if (
      path === `/api/projects/${PROJECT_ID}/skills/frontmatter/parse` &&
      method === "POST"
    ) {
      const body = request.postDataJSON() as { source_sha256: string };
      return json(route, {
        source_sha256: body.source_sha256,
        valid: true,
        patchable: true,
        projection: {
          required_secrets: [{ name: SYSTEM_TOKEN, optional: false }],
          secrets_autonomous: false,
          secrets_autonomous_explicit: false,
          shorthand_count: 0,
        },
        diagnostics: [],
        request_id: "request-system-skill-parse",
      });
    }
    if (
      path ===
        `/api/projects/${PROJECT_ID}/skills/${ASSET_ID}/versions/${VERSION_ID}/credential-bindings` &&
      method === "GET"
    ) {
      return json(route, bindingResponse());
    }
    if (
      path ===
        `/api/projects/${PROJECT_ID}/skills/${ASSET_ID}/versions/${VERSION_ID}/export` &&
      method === "GET"
    ) {
      exportRequests.push(path);
      return route.fulfill({
        status: 200,
        headers: {
          "Content-Type": "application/zip",
          "Content-Disposition": 'attachment; filename="system-skill-v1.zip"',
          "Cache-Control": "private, no-store",
          "X-Content-Type-Options": "nosniff",
        },
        body: Buffer.from([80, 75, 3, 4]),
      });
    }
    if (
      path ===
        `/api/projects/${PROJECT_ID}/skills/${ASSET_ID}/versions/${VERSION_ID}/credential-bindings` &&
      method === "PUT"
    ) {
      const body = request.postDataJSON() as {
        expected_revision: number;
        bindings: Array<{
          name: string;
          credential_version_id: string;
          source_env_field_name: string;
        }>;
      };
      bindingBodies.push(body);
      bindingConfigured =
        body.bindings.length === 1 &&
        body.bindings[0]?.name === SYSTEM_TOKEN &&
        body.bindings[0]?.credential_version_id === CREDENTIAL_VERSION_ID &&
        body.bindings[0]?.source_env_field_name === SYSTEM_TOKEN;
      bindingRevision = body.expected_revision + 1;
      return json(route, bindingResponse());
    }

    unexpectedRequests.push(`${method} ${path}${url.search}`);
    return json(route, { detail: "unexpected browser-test request" }, 599);
  });

  return { bindingBodies, exportRequests, unexpectedRequests };
}

test("System Skill current version owns exact Credential mappings and survives refresh", async ({
  page,
}) => {
  const { bindingBodies, unexpectedRequests } =
    await mockProjectSystemSkills(page);

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
  await detail.getByRole("tab", { name: "Runtime credentials" }).click();
  await expect(
    detail.getByRole("heading", { name: "2. Project Credential mappings" }),
  ).toBeVisible();
  const credentialSelect = detail.getByLabel(
    `Project Credential · ${SYSTEM_TOKEN}`,
  );
  const sourceSelect = detail.getByLabel(
    `Source environment variable · ${SYSTEM_TOKEN}`,
  );
  await expect(credentialSelect).toBeEnabled();
  await credentialSelect.selectOption(CREDENTIAL_VERSION_ID);
  await expect(sourceSelect).toHaveValue(SYSTEM_TOKEN);
  await expect(
    detail.getByRole("button", { name: "Export ZIP" }),
  ).toBeDisabled();
  const [bindingResponse] = await Promise.all([
    page.waitForResponse(
      (response) =>
        response.request().method() === "PUT" &&
        response
          .url()
          .endsWith(
            `/api/projects/${PROJECT_ID}/skills/${ASSET_ID}/versions/${VERSION_ID}/credential-bindings`,
          ),
    ),
    detail.getByRole("button", { name: "Save mappings" }).click(),
  ]);
  expect(bindingResponse.status()).toBe(200);
  expect(bindingBodies).toEqual([
    {
      expected_revision: 0,
      bindings: [
        {
          name: SYSTEM_TOKEN,
          credential_version_id: CREDENTIAL_VERSION_ID,
          source_env_field_name: SYSTEM_TOKEN,
        },
      ],
    },
  ]);
  await expect(
    detail.getByRole("button", { name: "Export ZIP" }),
  ).toBeEnabled();

  await page.reload();

  await expect(page).toHaveURL(`/projects/alpha/skills?skill_id=${ASSET_ID}`);
  await expect(detail).toBeVisible();
  await detail.getByRole("tab", { name: "Runtime credentials" }).click();
  await expect(
    detail.getByRole("heading", { name: "2. Project Credential mappings" }),
  ).toBeVisible();
  await expect(credentialSelect).toHaveValue(CREDENTIAL_VERSION_ID);
  await expect(sourceSelect).toHaveValue(SYSTEM_TOKEN);

  await detail.getByRole("button", { name: "关闭详情" }).click();

  await expect(detail).toHaveCount(0);
  await expect(page).toHaveURL("/projects/alpha/skills");
  expect(unexpectedRequests).toEqual([]);
});

test("System Skill detail exports the exact selected persisted version", async ({
  page,
}) => {
  const { exportRequests, unexpectedRequests } =
    await mockProjectSystemSkills(page);

  await page.goto("/projects/alpha/skills");
  await page.getByRole("button", { name: "查看 System Skill 详情" }).click();

  const detail = page.getByRole("dialog", { name: "System Skill" });
  const exportButton = detail.getByRole("button", { name: "Export ZIP" });
  await expect(exportButton).toBeEnabled();

  const [download] = await Promise.all([
    page.waitForEvent("download"),
    exportButton.click(),
  ]);

  expect(download.suggestedFilename()).toBe("system-skill-v1.zip");
  await expect(page.getByText("Download started")).toBeVisible();
  expect(exportRequests).toEqual([
    `/api/projects/${PROJECT_ID}/skills/${ASSET_ID}/versions/${VERSION_ID}/export`,
  ]);
  expect(unexpectedRequests).toEqual([]);
});
