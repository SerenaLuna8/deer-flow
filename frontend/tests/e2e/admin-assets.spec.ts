import { expect, test, type Page, type Route } from "@playwright/test";

import type {
  AssetSummary,
  AssetVersion,
  CredentialMetadata,
} from "@/core/shared-assets";

type McpVersion = Extract<AssetVersion, { mcp_server_id: string }>;
type AgentVersion = Extract<AssetVersion, { agent_id: string }>;
type SkillVersion = Extract<AssetVersion, { skill_id: string }>;
type CredentialVersion = Extract<AssetVersion, { credential_id: string }>;
type AdminCredential = CredentialMetadata;

const AGENT_ID = "10000000-0000-4000-8000-000000000001";
const MCP_ID = "10000000-0000-4000-8000-000000000002";
const MCP_VERSION_ID = "20000000-0000-4000-8000-000000000002";
const SKILL_ID = "10000000-0000-4000-8000-000000000004";
const CREDENTIAL_ID = "10000000-0000-4000-8000-000000000005";
const PROJECT_SENTINEL_ID = "10000000-0000-4000-8000-000000000006";
const PROJECT_CREDENTIAL_SENTINEL_ID = "10000000-0000-4000-8000-000000000007";
const PROJECT_SENTINEL_PROJECT_ID = "10000000-0000-4000-8000-000000000008";

const baseAsset = {
  scope: "system" as const,
  project_id: null,
  status: "active" as const,
  current_published_version_id: null,
  version: 1,
  created_by_user_id: "system-admin",
  created_at: "2026-07-13T08:00:00+00:00",
  updated_at: "2026-07-13T08:00:00+00:00",
};

const agent: AssetSummary = {
  ...baseAsset,
  id: AGENT_ID,
  slug: "research-agent",
  display_name: "Research Agent",
};

const mcp: AssetSummary = {
  ...baseAsset,
  id: MCP_ID,
  slug: "github-mcp",
  display_name: "GitHub MCP",
  current_published_version_id: MCP_VERSION_ID,
};

const skill: AssetSummary = {
  ...baseAsset,
  id: SKILL_ID,
  slug: "research-skill",
  display_name: "Research Skill",
};

function projectSentinelAsset(kind: "Agent" | "Skill" | "MCP"): AssetSummary {
  return {
    ...baseAsset,
    id: PROJECT_SENTINEL_ID,
    scope: "project",
    project_id: PROJECT_SENTINEL_PROJECT_ID,
    slug: `project-only-${kind.toLowerCase()}`,
    display_name: `Project-only ${kind} Sentinel`,
  };
}

const projectCredentialSentinel: AdminCredential = {
  id: PROJECT_CREDENTIAL_SENTINEL_ID,
  scope: "project",
  project_id: PROJECT_SENTINEL_PROJECT_ID,
  name: "project-only-credential",
  display_name: "Project-only Credential Sentinel",
  credential_type: "token",
  status: "active",
  current_version_id: null,
  version: 1,
  created_by_user_id: "project-owner",
  created_at: "2026-07-13T08:00:00+00:00",
  updated_at: "2026-07-13T08:00:00+00:00",
};

const mcpVersion: McpVersion = {
  id: MCP_VERSION_ID,
  mcp_server_id: MCP_ID,
  version_number: 1,
  workflow_status: "published",
  definition: {
    description: "GitHub tools",
    transport: "http",
    command: null,
    args: [],
    url: "https://mcp.example.test",
    env: {},
    headers: {},
    oauth: {},
    routing: {},
    tool_overrides: {},
    timeout_seconds: 30,
    credential_slots: [
      {
        name: "github-token",
        purpose: "GitHub API",
        payload_schema: { headers: ["Authorization"] },
        required: true,
      },
    ],
  },
  credential_slots: [
    {
      id: "30000000-0000-4000-8000-000000000001",
      name: "github-token",
      purpose: "GitHub API",
      payload_schema: { headers: ["Authorization"] },
      required: true,
    },
  ],
  credential_grants: [],
  supersedes_version_id: null,
  payload_checksum: "mcp-checksum",
  submitted_at: null,
  reviewed_at: null,
  reviewed_by_user_id: null,
  created_by_user_id: "system-admin",
  created_at: "2026-07-13T08:00:00+00:00",
};

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function mockAdminAssets(
  page: Page,
  { seedCredential = false }: { seedCredential?: boolean } = {},
) {
  const mcpWorkflowRequests: {
    submit: { expected_asset_version: number } | null;
    approve: {
      credential_versions: Record<string, string>;
      expected_asset_version: number;
    } | null;
    grants: {
      credential_versions: Record<string, string>;
      expected_active_grant_versions: Record<string, number>;
    } | null;
    credentialDelete: {
      expected_credential_version: number;
    } | null;
  } = {
    submit: null,
    approve: null,
    grants: null,
    credentialDelete: null,
  };
  let agentState = structuredClone(agent);
  let agents = [agentState];
  let agentVersions: AgentVersion[] = [];
  let skillState = structuredClone(skill);
  let skillVersions: SkillVersion[] = [];
  let mcpState = structuredClone(mcp);
  let workflowStatus: McpVersion["workflow_status"] = "published";
  let systemMcpGrants: McpVersion["credential_grants"] = [];
  let directMcpVersion: McpVersion | null = null;
  let credential: AdminCredential | null = seedCredential
    ? {
        id: CREDENTIAL_ID,
        scope: "system",
        project_id: null,
        name: "github-token",
        display_name: "GitHub Token",
        credential_type: "token",
        status: "active",
        current_version_id: "20000000-0000-4000-8000-000000000005",
        version: 1,
        created_by_user_id: "system-admin",
        created_at: "2026-07-13T09:00:00+00:00",
        updated_at: "2026-07-13T09:00:00+00:00",
      }
    : null;
  let credentialVersions: CredentialVersion[] = seedCredential
    ? [
        {
          id: "20000000-0000-4000-8000-000000000005",
          credential_id: CREDENTIAL_ID,
          version_number: 1,
          status: "active",
          payload_schema_version: 1,
          payload_schema: { headers: ["Authorization"] },
          supersedes_version_id: null,
          created_by_user_id: "system-admin",
          created_at: "2026-07-13T09:00:00+00:00",
        },
      ]
    : [];

  await page.route(/\/api\/admin\/assets(?:\/.*)?$/, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (path.endsWith("/api/admin/assets/agents") && method === "GET") {
      await json(route, {
        items: [...agents, projectSentinelAsset("Agent")],
        request_id: "request-agents",
      });
      return;
    }
    if (path.endsWith("/api/admin/assets/agents") && method === "POST") {
      const input = request.postDataJSON() as {
        slug: string;
        display_name: string;
      };
      const created: AssetSummary = {
        ...baseAsset,
        id: "10000000-0000-4000-8000-000000000003",
        slug: input.slug,
        display_name: input.display_name,
      };
      agents = [...agents, created];
      await json(route, { item: created, request_id: "request-create" }, 201);
      return;
    }
    if (
      path.endsWith(`/api/admin/assets/agents/${AGENT_ID}/versions`) &&
      method === "GET"
    ) {
      await json(route, {
        data: agentVersions,
        request_id: "request-agent-history",
      });
      return;
    }
    if (
      path.endsWith(`/api/admin/assets/agents/${AGENT_ID}/versions`) &&
      method === "POST"
    ) {
      const input = request.postDataJSON() as {
        description: string;
        agents_instructions: string;
        soul: string;
        identity: string;
        user_context: string;
        model_ref: string;
        tool_groups: string[];
        skill_version_ids: string[];
        mcp_version_ids: string[];
      };
      const created: AgentVersion = {
        id: "20000000-0000-4000-8000-000000000001",
        agent_id: AGENT_ID,
        version_number: 1,
        workflow_status: "draft",
        description: input.description,
        agents_instructions: input.agents_instructions,
        soul: input.soul,
        identity: input.identity,
        user_context: input.user_context,
        payload_schema_version: 2,
        model_ref: input.model_ref,
        tool_groups: input.tool_groups,
        skill_version_ids: input.skill_version_ids,
        mcp_version_ids: input.mcp_version_ids,
        supersedes_version_id: null,
        payload_checksum: "agent-checksum",
        created_by_user_id: "system-admin",
        created_at: "2026-07-13T09:00:00+00:00",
      };
      agentVersions = [created];
      agentState = { ...agentState, version: 2 };
      agents = [agentState];
      await json(
        route,
        { data: created, request_id: "request-agent-version" },
        201,
      );
      return;
    }
    if (
      path.endsWith(
        `/api/admin/assets/agents/${AGENT_ID}/versions/20000000-0000-4000-8000-000000000001/publish`,
      ) &&
      method === "POST"
    ) {
      agentVersions = agentVersions.map((version) => ({
        ...version,
        workflow_status: "published",
      }));
      agentState = {
        ...agentState,
        version: 3,
        current_published_version_id: agentVersions[0]!.id,
      };
      agents = [agentState];
      await json(route, {
        data: agentVersions[0],
        request_id: "request-agent-publish",
      });
      return;
    }
    if (path.endsWith("/api/admin/assets/skills") && method === "GET") {
      await json(route, {
        items: [skillState, projectSentinelAsset("Skill")],
        request_id: "request-skills",
      });
      return;
    }
    if (
      path.endsWith(`/api/admin/assets/skills/${SKILL_ID}/versions`) &&
      method === "GET"
    ) {
      await json(route, {
        data: skillVersions,
        request_id: "request-skill-history",
      });
      return;
    }
    if (
      path.endsWith(`/api/admin/assets/skills/${SKILL_ID}/versions`) &&
      method === "POST"
    ) {
      const created: SkillVersion = {
        id: "20000000-0000-4000-8000-000000000004",
        skill_id: SKILL_ID,
        version_number: 1,
        workflow_status: "draft",
        description: "Research skill",
        frontmatter: {},
        compatibility: null,
        secret_requirements: [],
        scan_decision: "allow",
        scan_rule_ids: [],
        scan_summary: {},
        file_views: [
          {
            path: "SKILL.md",
            media_type: "text/markdown",
            size_bytes: 24,
            sha256: "skill-file-checksum",
          },
        ],
        supersedes_version_id: null,
        payload_checksum: "skill-checksum",
        created_by_user_id: "system-admin",
        created_at: "2026-07-13T09:00:00+00:00",
      };
      skillVersions = [created];
      skillState = { ...skillState, version: 2 };
      await json(
        route,
        { data: created, request_id: "request-skill-version" },
        201,
      );
      return;
    }
    if (
      path.endsWith(
        `/api/admin/assets/skills/${SKILL_ID}/versions/20000000-0000-4000-8000-000000000004/publish`,
      ) &&
      method === "POST"
    ) {
      skillVersions = skillVersions.map((version) => ({
        ...version,
        workflow_status: "published",
      }));
      skillState = {
        ...skillState,
        version: 3,
        current_published_version_id: skillVersions[0]!.id,
      };
      await json(route, {
        data: skillVersions[0],
        request_id: "request-skill-publish",
      });
      return;
    }
    if (path.endsWith("/api/admin/assets/mcp-servers") && method === "GET") {
      await json(route, {
        items: [mcpState, projectSentinelAsset("MCP")],
        request_id: "request-mcp",
      });
      return;
    }
    if (
      path.endsWith(`/api/admin/assets/mcp-servers/${MCP_ID}/versions`) &&
      method === "GET"
    ) {
      await json(route, {
        data: [
          ...(directMcpVersion ? [directMcpVersion] : []),
          {
            ...mcpVersion,
            workflow_status: workflowStatus,
            credential_grants: systemMcpGrants,
          },
        ],
        request_id: "request-mcp-history",
      });
      return;
    }
    if (
      path.endsWith(
        `/api/admin/assets/mcp-servers/${MCP_ID}/versions/${MCP_VERSION_ID}/credential-grants`,
      ) &&
      method === "POST"
    ) {
      mcpWorkflowRequests.grants = request.postDataJSON() as {
        credential_versions: Record<string, string>;
        expected_active_grant_versions: Record<string, number>;
      };
      systemMcpGrants = [
        {
          id: "40000000-0000-4000-8000-000000000001",
          mcp_server_version_id: MCP_VERSION_ID,
          credential_slot_id: "30000000-0000-4000-8000-000000000001",
          credential_version_id:
            mcpWorkflowRequests.grants.credential_versions["github-token"]!,
          status: "active",
          version: 1,
          created_by_user_id: "system-admin",
          created_at: "2026-07-13T09:30:00+00:00",
        },
      ];
      await json(route, {
        data: {
          ...mcpVersion,
          workflow_status: "published",
          credential_grants: systemMcpGrants,
        },
        request_id: "request-system-mcp-grants",
      });
      return;
    }
    if (
      path.endsWith(`/api/admin/assets/mcp-servers/${MCP_ID}/versions`) &&
      method === "POST"
    ) {
      directMcpVersion = {
        ...mcpVersion,
        id: "20000000-0000-4000-8000-000000000003",
        version_number: 2,
        workflow_status: "draft",
        definition: { ...mcpVersion.definition, credential_slots: [] },
        credential_slots: [],
        payload_checksum: "mcp-direct-checksum",
        created_at: "2026-07-13T10:00:00+00:00",
      };
      mcpState = { ...mcpState, version: mcpState.version + 1 };
      await json(
        route,
        {
          data: directMcpVersion,
          request_id: "request-mcp-version",
        },
        201,
      );
      return;
    }
    if (
      path.endsWith(
        `/api/admin/assets/mcp-servers/${MCP_ID}/versions/20000000-0000-4000-8000-000000000003/publish`,
      ) &&
      method === "POST" &&
      directMcpVersion
    ) {
      directMcpVersion = { ...directMcpVersion, workflow_status: "published" };
      mcpState = {
        ...mcpState,
        version: mcpState.version + 1,
        current_published_version_id: directMcpVersion.id,
      };
      await json(route, {
        data: directMcpVersion,
        request_id: "request-mcp-publish",
      });
      return;
    }
    if (
      path.endsWith(
        `/api/admin/assets/mcp-servers/${MCP_ID}/versions/${MCP_VERSION_ID}/submit-approval`,
      ) &&
      method === "POST"
    ) {
      mcpWorkflowRequests.submit = request.postDataJSON() as {
        expected_asset_version: number;
      };
      if (
        mcpWorkflowRequests.submit.expected_asset_version !== mcpState.version
      ) {
        await json(
          route,
          {
            detail: {
              code: "asset_conflict",
              message: "Asset state conflict",
              request_id: "request-submit-conflict",
            },
          },
          409,
        );
        return;
      }
      workflowStatus = "pending_approval";
      mcpState = { ...mcpState, version: mcpState.version + 1 };
      await json(route, {
        data: {
          ...mcpVersion,
          workflow_status: workflowStatus,
          submitted_at: "2026-07-13T09:00:00+00:00",
        },
        request_id: "request-submit",
      });
      return;
    }
    if (
      path.endsWith(
        `/api/admin/assets/mcp-servers/${MCP_ID}/versions/${MCP_VERSION_ID}/approve`,
      ) &&
      method === "POST"
    ) {
      mcpWorkflowRequests.approve = request.postDataJSON() as {
        credential_versions: Record<string, string>;
        expected_asset_version: number;
      };
      if (
        mcpWorkflowRequests.approve.expected_asset_version !== mcpState.version
      ) {
        await json(
          route,
          {
            detail: {
              code: "asset_conflict",
              message: "Asset state conflict",
              request_id: "request-approve-conflict",
            },
          },
          409,
        );
        return;
      }
      workflowStatus = "published";
      mcpState = {
        ...mcpState,
        version: mcpState.version + 1,
        current_published_version_id: MCP_VERSION_ID,
      };
      await json(route, {
        data: {
          ...mcpVersion,
          workflow_status: workflowStatus,
          submitted_at: "2026-07-13T09:00:00+00:00",
          reviewed_at: "2026-07-13T09:30:00+00:00",
          reviewed_by_user_id: "system-admin",
        },
        request_id: "request-approve",
      });
      return;
    }
    if (path.endsWith("/api/admin/assets/credentials") && method === "GET") {
      await json(route, {
        items: [...(credential ? [credential] : []), projectCredentialSentinel],
        request_id: "request-credentials",
      });
      return;
    }
    if (
      path.endsWith("/api/admin/assets/credentials/rotation-status") &&
      method === "GET"
    ) {
      await json(route, {
        eligible_total: 1,
        current: 1,
        pending: 0,
        status: "current",
      });
      return;
    }
    if (path.endsWith("/api/admin/assets/credentials") && method === "POST") {
      const input = request.postDataJSON() as {
        name: string;
        display_name: string;
        credential_type: string;
      };
      credential = {
        id: CREDENTIAL_ID,
        scope: "system",
        project_id: null,
        name: input.name,
        display_name: input.display_name,
        credential_type: input.credential_type,
        status: "active",
        current_version_id: "20000000-0000-4000-8000-000000000005",
        version: 1,
        created_by_user_id: "system-admin",
        created_at: "2026-07-13T09:00:00+00:00",
        updated_at: "2026-07-13T09:00:00+00:00",
      };
      credentialVersions = [
        {
          id: "20000000-0000-4000-8000-000000000005",
          credential_id: CREDENTIAL_ID,
          version_number: 1,
          status: "active",
          payload_schema_version: 1,
          payload_schema: { env: ["TOKEN"] },
          supersedes_version_id: null,
          created_by_user_id: "system-admin",
          created_at: "2026-07-13T09:00:00+00:00",
        },
      ];
      await json(
        route,
        {
          item: credential,
          request_id: "request-credential-create",
        },
        201,
      );
      return;
    }
    if (
      path.endsWith(
        `/api/admin/assets/credentials/${CREDENTIAL_ID}/versions`,
      ) &&
      method === "GET"
    ) {
      await json(route, {
        data: credentialVersions,
        request_id: "request-credential-history",
      });
      return;
    }
    if (
      path.endsWith(`/api/admin/assets/credentials/${CREDENTIAL_ID}/replace`) &&
      method === "POST" &&
      credential
    ) {
      const previous = credentialVersions[0]!;
      const created: CredentialVersion = {
        ...previous,
        id: "20000000-0000-4000-8000-000000000006",
        version_number: 2,
        supersedes_version_id: previous.id,
        created_at: "2026-07-13T10:00:00+00:00",
      };
      credentialVersions = [created, { ...previous, status: "retired" }];
      credential = {
        ...credential,
        current_version_id: created.id,
        version: 2,
        updated_at: created.created_at,
      };
      await json(route, {
        data: created,
        request_id: "request-credential-replace",
      });
      return;
    }
    if (
      path.endsWith(
        `/api/admin/assets/credentials/${CREDENTIAL_ID}/migrate-grants`,
      ) &&
      method === "POST" &&
      credential
    ) {
      await json(route, {
        credential_id: credential.id,
        credential_version_id: credential.current_version_id,
        migrated_count: 1,
        request_id: "request-credential-migration",
      });
      return;
    }
    if (
      path.endsWith(`/api/admin/assets/credentials/${CREDENTIAL_ID}/revoke`) &&
      method === "POST" &&
      credential
    ) {
      credential = {
        ...credential,
        status: "revoked",
        version: credential.version + 1,
        updated_at: "2026-07-13T11:00:00+00:00",
      };
      await json(route, {
        item: credential,
        request_id: "request-credential-revoke",
      });
      return;
    }
    if (
      path.endsWith(`/api/admin/assets/credentials/${CREDENTIAL_ID}`) &&
      method === "DELETE" &&
      credential
    ) {
      mcpWorkflowRequests.credentialDelete =
        request.postDataJSON() as typeof mcpWorkflowRequests.credentialDelete;
      credential = null;
      credentialVersions = [];
      await route.fulfill({ status: 204 });
      return;
    }

    await json(
      route,
      {
        detail: {
          code: "asset_not_found",
          message: "private backend detail",
          request_id: "request-not-found",
        },
      },
      404,
    );
  });

  return mcpWorkflowRequests;
}

test("desktop Skill inspector overlays the catalog without resizing it", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await mockAdminAssets(page);
  await page.goto("/admin/assets/skills");

  const catalog = page.getByTestId("admin-asset-table");
  const desktopRow = page.getByTestId(`admin-asset-row-${SKILL_ID}`);
  const mobileRow = page.getByTestId(`admin-asset-row-mobile-${SKILL_ID}`);
  const catalogRect = () =>
    catalog.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      return {
        left: Math.round(rect.left),
        width: Math.round(rect.width),
      };
    });

  await expect(catalog).toBeVisible();
  await expect(desktopRow).toBeVisible();
  await expect(mobileRow).toBeHidden();
  const beforeOpen = await catalogRect();

  await desktopRow.click();

  const inspector = page.getByTestId("admin-asset-inspector");
  await expect(inspector).toBeVisible();
  await expect(desktopRow).toBeVisible();
  await expect(mobileRow).toBeHidden();
  await expect.poll(catalogRect).toEqual(beforeOpen);

  const [catalogBox, inspectorBox] = await Promise.all([
    catalog.boundingBox(),
    inspector.boundingBox(),
  ]);
  expect(catalogBox).not.toBeNull();
  expect(inspectorBox).not.toBeNull();
  expect(inspectorBox!.x).toBeLessThan(catalogBox!.x + catalogBox!.width);
  expect(inspectorBox!.x + inspectorBox!.width).toBeGreaterThan(catalogBox!.x);
  expect(inspectorBox!.y).toBeLessThan(catalogBox!.y + catalogBox!.height);
  expect(inspectorBox!.y + inspectorBox!.height).toBeGreaterThan(catalogBox!.y);
});

test("system Agent Skill and MCP catalog is read only while Credential governance remains available", async ({
  page,
}) => {
  const mcpWorkflowRequests = await mockAdminAssets(page, {
    seedCredential: true,
  });

  await page.goto("/admin/assets");
  await expect(page).toHaveURL(/\/admin\/assets\/agents$/);
  await expect(
    page.getByRole("navigation", { name: "Platform asset navigation" }),
  ).toHaveAttribute("data-variant", "line");
  await expect(
    page.getByRole("heading", { name: "System Agents" }),
  ).toBeVisible();
  const agentTable = page.getByTestId("admin-asset-table");
  await expect(agentTable).toBeVisible();
  const desktopAgentTable = agentTable.getByRole("table");
  await expect(
    desktopAgentTable.getByText("Research Agent", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Project-only Agent Sentinel", { exact: true }),
  ).toHaveCount(0);
  const agentRow = desktopAgentTable
    .getByRole("row")
    .filter({ hasText: "Research Agent" });
  await expect(agentRow).toContainText("Not published");
  await expect(
    page.getByText(
      "Governance metadata for system Agents initialized from the packaged catalog.",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(
    page.getByText("Runtime read-only", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Create Agent" })).toHaveCount(
    0,
  );
  await expect(
    page.getByRole("button", { name: "Create new version" }),
  ).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Archive" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Suspend" })).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Publish version" }),
  ).toHaveCount(0);

  await page.getByRole("link", { name: "Skill" }).first().click();
  await expect(
    page.getByRole("heading", { name: "System Skills" }),
  ).toBeVisible();
  await expect(
    page.getByText("Project-only Skill Sentinel", { exact: true }),
  ).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Create Skill" })).toHaveCount(
    0,
  );
  await expect(
    page.getByRole("button", { name: "Create new version" }),
  ).toHaveCount(0);

  await page.getByRole("link", { name: "MCP" }).first().click();
  await expect(page.getByRole("heading", { name: "System MCP" })).toBeVisible();
  await expect(
    page.getByText("Project-only MCP Sentinel", { exact: true }),
  ).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Create MCP" })).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Create new version" }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Submit for approval" }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Approve and publish" }),
  ).toHaveCount(0);
  await page.getByTestId(`admin-asset-row-${MCP_ID}`).click();
  const mcpInspector = page.getByTestId("admin-asset-inspector");
  await mcpInspector
    .getByRole("button", { name: "Configure Credential grants" })
    .click();
  const grantDialog = page.getByRole("dialog", {
    name: "Configure MCP Credential grants",
  });
  await grantDialog
    .getByRole("combobox", { name: "github-token required GitHub API" })
    .selectOption("20000000-0000-4000-8000-000000000005");
  await grantDialog.getByRole("button", { name: "Save grants" }).click();
  await expect(grantDialog).toHaveCount(0);
  expect(mcpWorkflowRequests).toEqual({
    submit: null,
    approve: null,
    grants: {
      credential_versions: {
        "github-token": "20000000-0000-4000-8000-000000000005",
      },
      expected_active_grant_versions: {},
    },
    credentialDelete: null,
  });
  await page.keyboard.press("Escape");

  await page.getByRole("link", { name: "Credential" }).first().click();
  await expect(
    page.getByRole("heading", { name: "System Credentials" }),
  ).toBeVisible();
  await expect(
    page.getByText("Project-only Credential Sentinel", { exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Show plaintext" }),
  ).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Copy secret" })).toHaveCount(
    0,
  );
});

test("system admin manages the separate system Credential lifecycle", async ({
  page,
}) => {
  const state = await mockAdminAssets(page);

  await page.goto("/admin/assets/credentials");
  const rotationStatus = page.getByTestId("credential-rotation-status");
  await expect(rotationStatus).toHaveAttribute("data-density", "compact");
  await expect(rotationStatus).toContainText("Rotation current");
  await page.getByRole("button", { name: "Create Credential" }).click();
  const createCredential = page.getByRole("dialog", {
    name: "Create Credential",
  });
  await createCredential
    .getByLabel("Name", { exact: true })
    .fill("GitHub Token");
  await createCredential.getByLabel("Credential slug").fill("github-token");
  await createCredential.getByLabel("Type").fill("token");
  await createCredential.getByLabel("Field name").fill("TOKEN");
  await createCredential
    .getByLabel("Secret value")
    .fill("create-secret-sentinel");
  await createCredential
    .getByRole("button", { name: "Encrypt and save" })
    .click();
  const credentialRow = page.getByTestId(`admin-asset-row-${CREDENTIAL_ID}`);
  await expect(credentialRow).toBeVisible();
  expect(await page.content()).not.toContain("create-secret-sentinel");

  await credentialRow.click();
  const credentialCard = page.getByTestId("admin-asset-inspector");
  await expect(
    credentialCard.getByText("GitHub Token", { exact: true }),
  ).toBeVisible();
  await credentialCard
    .getByRole("button", { name: "Replace Credential" })
    .click();
  const replaceCredential = page.getByRole("dialog", {
    name: "Replace Credential",
  });
  await replaceCredential.getByLabel("Field name").fill("TOKEN");
  await replaceCredential
    .getByLabel("Secret value")
    .fill("replace-secret-sentinel");
  await replaceCredential
    .getByRole("button", { name: "Replace Credential" })
    .click();
  await expect(replaceCredential).toHaveCount(0);
  expect(await page.content()).not.toContain("replace-secret-sentinel");
  await expect(credentialCard).toContainText(
    "Existing MCP Grants and Skill environment bindings remain pinned until they are migrated explicitly.",
  );
  await credentialCard
    .getByRole("button", { name: "Migrate compatible references" })
    .click();
  const migrationDialog = page.getByRole("dialog", {
    name: "Migrate compatible Credential references",
  });
  await expect(migrationDialog).toContainText(
    "only when every field schema is compatible",
  );
  await migrationDialog
    .getByRole("button", { name: "Migrate references" })
    .click();
  await expect(page.getByRole("status")).toContainText(
    "Compatible reference migration completed",
  );

  await credentialCard
    .getByRole("button", { name: "Revoke Credential" })
    .click();
  const revokeDialog = page.getByRole("dialog", {
    name: "Revoke Credential?",
  });
  await expect(revokeDialog).toContainText("cannot be undone");
  await revokeDialog.getByRole("button", { name: "Cancel" }).click();
  await expect(
    credentialCard.getByText("Revoked", { exact: true }),
  ).toHaveCount(0);
  await credentialCard
    .getByRole("button", { name: "Revoke Credential" })
    .click();
  await page
    .getByRole("dialog", { name: "Revoke Credential?" })
    .getByRole("button", { name: "Permanently revoke" })
    .click();
  await expect(
    credentialCard.getByText("Revoked", { exact: true }),
  ).toBeVisible();
  await expect(
    credentialCard.getByRole("button", { name: "Replace Credential" }),
  ).toHaveCount(0);
  await credentialCard
    .getByRole("button", { name: "Delete", exact: true })
    .click();
  const deleteDialog = page.getByRole("dialog", {
    name: "Delete Credential?",
  });
  const confirmDelete = deleteDialog.getByRole("button", {
    name: /Confirm delete/u,
  });
  await expect(confirmDelete).toBeDisabled();
  await expect(confirmDelete).toBeEnabled({ timeout: 6_000 });
  await confirmDelete.click();
  await expect(credentialCard).toHaveCount(0);
  await expect(credentialRow).toHaveCount(0);
  expect(state.credentialDelete).toEqual({
    expected_credential_version: 3,
  });
});
