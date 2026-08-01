import { expect, test, type Page, type Route } from "@playwright/test";

const PROJECT_ID = "40000000-0000-4000-8000-000000000001";
const SYSTEM_AGENT_ID = "40000000-0000-4000-8000-000000000002";
const SYSTEM_AGENT_VERSION_ID = "40000000-0000-4000-8000-000000000003";
const PROJECT_CREDENTIAL_ID = "40000000-0000-4000-8000-000000000004";
const PROJECT_CREDENTIAL_VERSION_ID = "40000000-0000-4000-8000-000000000005";
const PROJECT_AGENT_ID = "40000000-0000-4000-8000-000000000006";
const SYSTEM_SKILL_ID = "40000000-0000-4000-8000-000000000007";
const PROJECT_SKILL_ID = "40000000-0000-4000-8000-000000000008";
const SYSTEM_MCP_ID = "40000000-0000-4000-8000-000000000009";
const PROJECT_MCP_ID = "40000000-0000-4000-8000-000000000010";
const SYSTEM_CREDENTIAL_ID = "40000000-0000-4000-8000-000000000011";
const NOW = "2026-07-22T08:00:00+00:00";

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function mockAdminProjectAssets(page: Page) {
  const requestedPaths: string[] = [];
  let credentialVisible = true;
  let credentialDeleteRequest: unknown = null;

  page.on("request", (request) => {
    requestedPaths.push(new URL(request.url()).pathname);
  });

  await page.route("**/api/models", (route) =>
    json(route, {
      models: [
        {
          name: "default-model",
          display_name: "Default model",
          model: "default-model",
          description: "",
          supports_thinking: false,
          supports_reasoning_effort: false,
          supports_vision: false,
          is_default: true,
        },
      ],
      token_usage: { enabled: false },
    }),
  );

  await page.route(/\/api\/admin\/assets\/agents\/[^/]+\/versions$/, (route) =>
    json(route, {
      data: [
        {
          id: SYSTEM_AGENT_VERSION_ID,
          agent_id: SYSTEM_AGENT_ID,
          version_number: 1,
          workflow_status: "published",
          description: "Packaged research agent",
          agents_instructions: "",
          soul: "Use evidence.",
          identity: "",
          user_context: "",
          payload_schema_version: 2,
          model_ref: "default-model",
          tool_groups: [],
          skill_version_ids: [],
          mcp_version_ids: [],
          supersedes_version_id: null,
          payload_checksum: "system-agent-v1",
          created_by_user_id: "bootstrap",
          created_at: NOW,
        },
      ],
      request_id: "system-agent-history",
    }),
  );

  await page.route(/\/api\/admin\/projects(?:\?.*)?$/, (route) =>
    json(route, {
      items: [
        {
          project_id: PROJECT_ID,
          slug: "alpha-project",
          display_name: "Alpha Project",
          status: "active",
          is_suspended: false,
          state_version: 1,
          created_at: NOW,
          updated_at: NOW,
          deletion_effective_at: null,
        },
      ],
      next_cursor: null,
    }),
  );

  await page.route("**/api/admin/projects/**", async (route) => {
    const request = route.request();
    const { pathname } = new URL(request.url());
    const method = request.method();
    const base = `/api/admin/projects/${PROJECT_ID}/assets`;

    if (pathname === `${base}/agents` && method === "GET") {
      await json(route, {
        system_items: [
          {
            id: SYSTEM_AGENT_ID,
            scope: "system",
            project_id: null,
            slug: "packaged-research-agent",
            display_name: "Packaged Research Agent",
            status: "active",
            current_published_version_id: SYSTEM_AGENT_VERSION_ID,
            version: 1,
            created_by_user_id: "bootstrap",
            created_at: NOW,
            updated_at: NOW,
            capabilities: [
              "shared_assets.read",
              "shared_assets.execute",
              "shared_assets.manage_bindings",
            ],
            binding: null,
          },
        ],
        project_items: [
          {
            id: PROJECT_AGENT_ID,
            scope: "project",
            project_id: PROJECT_ID,
            slug: "project-research-agent",
            display_name: "Project Research Agent",
            status: "active",
            current_published_version_id: null,
            version: 1,
            created_by_user_id: "project-owner",
            created_at: NOW,
            updated_at: NOW,
            capabilities: ["shared_assets.read", "shared_assets.edit"],
            binding: null,
          },
        ],
        request_id: "admin-project-agents",
      });
      return;
    }

    if (
      (pathname === `${base}/skills` || pathname === `${base}/mcp-servers`) &&
      method === "GET"
    ) {
      const skill = pathname.endsWith("skills");
      await json(route, {
        system_items: [
          {
            id: skill ? SYSTEM_SKILL_ID : SYSTEM_MCP_ID,
            scope: "system",
            project_id: null,
            slug: skill ? "packaged-review-skill" : "packaged-search-mcp",
            display_name: skill
              ? "Packaged Review Skill"
              : "Packaged Search MCP",
            status: "active",
            current_published_version_id: null,
            version: 1,
            created_by_user_id: "bootstrap",
            created_at: NOW,
            updated_at: NOW,
            capabilities: ["shared_assets.read"],
            binding: null,
          },
        ],
        project_items: [
          {
            id: skill ? PROJECT_SKILL_ID : PROJECT_MCP_ID,
            scope: "project",
            project_id: PROJECT_ID,
            slug: skill ? "project-review-skill" : "project-search-mcp",
            display_name: skill ? "Project Review Skill" : "Project Search MCP",
            status: "active",
            current_published_version_id: null,
            version: 1,
            created_by_user_id: "project-owner",
            created_at: NOW,
            updated_at: NOW,
            capabilities: ["shared_assets.read", "shared_assets.edit"],
            binding: null,
          },
        ],
        request_id: `admin-project-${skill ? "skills" : "mcp"}`,
      });
      return;
    }

    if (pathname === `${base}/credentials` && method === "GET") {
      await json(route, {
        system_items: [
          {
            id: SYSTEM_CREDENTIAL_ID,
            scope: "system",
            project_id: null,
            name: "system-token",
            display_name: "System Token",
            credential_type: "token",
            status: "active",
            current_version_id: null,
            version: 1,
            created_by_user_id: "bootstrap",
            created_at: NOW,
            updated_at: NOW,
            capabilities: ["shared_assets.read"],
          },
        ],
        project_items: credentialVisible
          ? [
              {
                id: PROJECT_CREDENTIAL_ID,
                scope: "project",
                project_id: PROJECT_ID,
                name: "github-token",
                display_name: "Project GitHub Token",
                credential_type: "token",
                status: "active",
                current_version_id: PROJECT_CREDENTIAL_VERSION_ID,
                version: 1,
                created_by_user_id: "project-owner",
                created_at: NOW,
                updated_at: NOW,
                capabilities: ["shared_assets.read", "mcp.credentials.approve"],
              },
            ]
          : [],
        request_id: "admin-project-credentials",
      });
      return;
    }

    if (
      pathname === `${base}/credentials/${PROJECT_CREDENTIAL_ID}` &&
      method === "DELETE"
    ) {
      credentialDeleteRequest = request.postDataJSON();
      credentialVisible = false;
      await route.fulfill({ status: 204 });
      return;
    }

    if (
      pathname === `${base}/credentials/${PROJECT_CREDENTIAL_ID}/versions` &&
      method === "GET"
    ) {
      await json(route, {
        data: [
          {
            id: PROJECT_CREDENTIAL_VERSION_ID,
            credential_id: PROJECT_CREDENTIAL_ID,
            version_number: 1,
            status: "active",
            payload_schema_version: 1,
            payload_schema: { headers: ["Authorization"] },
            supersedes_version_id: null,
            created_by_user_id: "project-owner",
            created_at: NOW,
          },
        ],
        request_id: "admin-project-credential-history",
      });
      return;
    }

    await json(
      route,
      {
        detail: {
          code: "ASSET_NOT_FOUND",
          message: "private mock detail",
          request_id: "unexpected-admin-project-request",
        },
      },
      404,
    );
  });

  return {
    requestedPaths,
    credentialDeleteRequest: () => credentialDeleteRequest,
  };
}

test("system admin selects one project and governs only its shared assets", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const state = await mockAdminProjectAssets(page);

  await page.goto("/admin/projects");
  const projectCard = page.getByRole("listitem").filter({
    has: page.getByRole("heading", { name: "Alpha Project" }),
  });
  await projectCard.getByRole("link", { name: "Govern shared assets" }).click();

  await expect(page).toHaveURL(`/admin/projects/${PROJECT_ID}/assets/agents`);
  await expect(page.getByTestId("admin-project-assets-context")).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Back to projects" }),
  ).toBeVisible();
  await expect(page.getByText(PROJECT_ID, { exact: true })).toBeVisible();
  await expect(
    page.getByText(/never reads members, chats, runs, Memory, files/u),
  ).toBeVisible();
  await expect(
    page.getByRole("navigation", {
      name: "Project asset governance navigation",
    }),
  ).toHaveAttribute("data-variant", "line");
  await expect(
    page.getByTestId("admin-project-asset-directory"),
  ).toHaveAttribute("data-density", "dense-directory");
  for (const name of ["Agent", "Skill", "MCP", "Credential"]) {
    await expect(
      page
        .getByRole("navigation", {
          name: "Project asset governance navigation",
        })
        .getByRole("link", { name }),
    ).toBeVisible();
  }

  const projectAgent = page.getByTestId(
    `admin-project-asset-row-${PROJECT_AGENT_ID}`,
  );
  await expect(projectAgent.getByText("Project Research Agent")).toBeVisible();
  await expect(
    page.getByTestId(`admin-project-asset-row-${SYSTEM_AGENT_ID}`),
  ).toHaveCount(0);
  await expect(page.getByText("System provided", { exact: true })).toHaveCount(
    0,
  );
  await expect(
    page.getByRole("button", { name: "Manage binding" }),
  ).toHaveCount(0);

  await page
    .getByRole("navigation", {
      name: "Project asset governance navigation",
    })
    .getByRole("link", { name: "Skill" })
    .click();
  await expect(
    page.getByRole("heading", { name: "Project Skill governance" }),
  ).toBeVisible();
  await expect(
    page.getByText("Project Review Skill", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Packaged Review Skill", { exact: true }),
  ).toHaveCount(0);
  await page
    .getByRole("navigation", {
      name: "Project asset governance navigation",
    })
    .getByRole("link", { name: "MCP" })
    .click();
  await expect(
    page.getByRole("heading", { name: "Project MCP governance" }),
  ).toBeVisible();
  await expect(
    page.getByText("Project Search MCP", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Packaged Search MCP", { exact: true }),
  ).toHaveCount(0);
  await page
    .getByRole("navigation", {
      name: "Project asset governance navigation",
    })
    .getByRole("link", { name: "Credential" })
    .click();
  await expect(
    page.getByRole("heading", { name: "Project Credential governance" }),
  ).toBeVisible();
  await expect(
    page.getByText("Project GitHub Token", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("System Token", { exact: true })).toHaveCount(0);
  await expect(
    page.getByTestId("admin-project-credential-directory"),
  ).toHaveAttribute("data-density", "dense-directory");
  await expect(
    page.getByRole("button", { name: "Show plaintext" }),
  ).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Copy secret" })).toHaveCount(
    0,
  );
  const credentialRow = page.getByTestId(
    `admin-project-credential-row-${PROJECT_CREDENTIAL_ID}`,
  );
  await credentialRow.getByRole("button", { name: "View details" }).click();
  const credentialDetail = page.getByTestId("admin-project-credential-detail");
  await credentialDetail
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
  await expect(credentialDetail).toHaveCount(0);
  await expect(credentialRow).toHaveCount(0);
  expect(state.credentialDeleteRequest()).toEqual({
    expected_credential_version: 1,
  });

  expect(
    await page.evaluate(
      () =>
        document.documentElement.scrollWidth <=
        document.documentElement.clientWidth,
    ),
  ).toBe(true);

  const projectApiPrefix = `/api/admin/projects/${PROJECT_ID}/assets/`;
  expect(
    state.requestedPaths.some((path) => path.startsWith(projectApiPrefix)),
  ).toBe(true);
  expect(
    state.requestedPaths.some((path) =>
      path.startsWith(`/api/projects/${PROJECT_ID}/`),
    ),
  ).toBe(false);
  expect(
    state.requestedPaths.some((path) =>
      /\/(members|threads|runs|memory|files)(?:\/|$)/u.test(path),
    ),
  ).toBe(false);
});
