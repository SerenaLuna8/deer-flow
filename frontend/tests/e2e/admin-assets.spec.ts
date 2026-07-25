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
  } = { submit: null, approve: null, grants: null };
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
      await json(route, { items: agents, request_id: "request-agents" });
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
        soul: string;
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
        soul: input.soul,
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
      await json(route, { items: [skillState], request_id: "request-skills" });
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
      await json(route, { items: [mcpState], request_id: "request-mcp" });
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
        items: credential ? [credential] : [],
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

test("system Agent Skill and MCP catalog is read only while Credential governance remains available", async ({
  page,
}) => {
  const mcpWorkflowRequests = await mockAdminAssets(page, {
    seedCredential: true,
  });

  await page.goto("/admin/assets");
  await expect(page).toHaveURL(/\/admin\/assets\/agents$/);
  await expect(page.getByRole("heading", { name: "系统 Agent" })).toBeVisible();
  await expect(page.getByText("Research Agent", { exact: true })).toBeVisible();
  await expect(page.getByRole("note")).toContainText("packaged catalog");
  await expect(page.getByRole("note")).toContainText("运行期只读");
  await expect(page.getByRole("button", { name: "创建 Agent" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "创建新版本" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "归档" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "暂停" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "发布版本" })).toHaveCount(0);

  await page.getByRole("link", { name: "Skill" }).first().click();
  await expect(page.getByRole("heading", { name: "系统 Skill" })).toBeVisible();
  await expect(page.getByRole("button", { name: "创建 Skill" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "创建新版本" })).toHaveCount(0);

  await page.getByRole("link", { name: "MCP" }).first().click();
  await expect(page.getByRole("heading", { name: "系统 MCP" })).toBeVisible();
  await expect(page.getByRole("button", { name: "创建 MCP" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "创建新版本" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "提交审批" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "批准并发布" })).toHaveCount(0);
  await page.getByRole("button", { name: "配置 Credential 授权" }).click();
  const grantDialog = page.getByRole("dialog", {
    name: "配置 MCP Credential 授权",
  });
  await grantDialog
    .getByLabel("github-token Credential")
    .selectOption("20000000-0000-4000-8000-000000000005");
  await grantDialog.getByRole("button", { name: "保存授权" }).click();
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
  });

  await page.getByRole("link", { name: "Credential" }).first().click();
  await expect(
    page.getByRole("heading", { name: "系统 Credential" }),
  ).toBeVisible();
  await expect(page.getByText("显示明文")).toHaveCount(0);
  await expect(page.getByText("复制密钥")).toHaveCount(0);
});

test("system admin manages the separate system Credential lifecycle", async ({
  page,
}) => {
  await mockAdminAssets(page);

  await page.goto("/admin/assets/credentials");
  await expect(page.getByText("轮换正常", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "创建 Credential" }).click();
  const createCredential = page.getByRole("dialog", {
    name: "创建 Credential",
  });
  await createCredential.getByLabel("名称").fill("GitHub Token");
  await createCredential.getByLabel("Credential 标识").fill("github-token");
  await createCredential.getByLabel("类型").fill("token");
  await createCredential.getByLabel("字段名").fill("TOKEN");
  await createCredential.getByLabel("凭据值").fill("create-secret-sentinel");
  await createCredential.getByRole("button", { name: "加密写入" }).click();
  const credentialCard = page.getByTestId(`credential-card-${CREDENTIAL_ID}`);
  await expect(
    credentialCard.getByText("GitHub Token", { exact: true }),
  ).toBeVisible();
  expect(await page.content()).not.toContain("create-secret-sentinel");

  await credentialCard.getByRole("button", { name: "替换凭据" }).click();
  const replaceCredential = page.getByRole("dialog", { name: "替换凭据" });
  await replaceCredential.getByLabel("字段名").fill("TOKEN");
  await replaceCredential.getByLabel("凭据值").fill("replace-secret-sentinel");
  await replaceCredential.getByRole("button", { name: "替换凭据" }).click();
  await expect(replaceCredential).toHaveCount(0);
  expect(await page.content()).not.toContain("replace-secret-sentinel");
  await expect(credentialCard).toContainText(
    "既有 Grant 仍固定到 retired version",
  );
  await credentialCard.getByRole("button", { name: "迁移兼容 Grant" }).click();
  const migrationDialog = page.getByRole("dialog", {
    name: "迁移 Credential Grant",
  });
  await expect(migrationDialog).toContainText("字段结构完全兼容");
  await migrationDialog.getByRole("button", { name: "确认迁移 Grant" }).click();
  await expect(page.getByRole("status")).toContainText("已完成兼容 Grant 迁移");

  await credentialCard.getByRole("button", { name: "撤销凭据" }).click();
  const revokeDialog = page.getByRole("dialog", {
    name: "确认撤销 Credential",
  });
  await expect(revokeDialog).toContainText("此操作不可恢复");
  await revokeDialog.getByRole("button", { name: "取消" }).click();
  await expect(credentialCard.getByText("已撤销", { exact: true })).toHaveCount(
    0,
  );
  await credentialCard.getByRole("button", { name: "撤销凭据" }).click();
  await page
    .getByRole("dialog", { name: "确认撤销 Credential" })
    .getByRole("button", { name: "确认永久撤销" })
    .click();
  await expect(
    credentialCard.getByText("已撤销", { exact: true }),
  ).toBeVisible();
  await expect(
    credentialCard.getByRole("button", { name: "替换凭据" }),
  ).toHaveCount(0);
});
