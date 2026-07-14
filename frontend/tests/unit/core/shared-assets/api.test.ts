import { beforeEach, describe, expect, test, rs } from "@rstest/core";

rs.mock("@/core/api/fetcher", () => ({
  fetch: rs.fn(),
  AuthRequiredError: class AuthRequiredError extends Error {},
}));
rs.mock("@/core/config", () => ({ getBackendBaseURL: () => "/backend" }));

import { AuthRequiredError, fetch as fetchWithAuth } from "@/core/api/fetcher";
import {
  SharedAssetApiError,
  approveProjectMcpVersion,
  createAdminAssetVersion,
  createAdminCredential,
  createProjectAsset,
  createProjectAssetVersion,
  createProjectCredential,
  disableProjectSystemBinding,
  enableProjectSystemBinding,
  listAdminAssets,
  listProjectAssetVersions,
  listProjectAssets,
  listSystemAssetCatalog,
  publishProjectAssetVersion,
  replaceProjectCredential,
  revokeProjectCredential,
  submitProjectMcpVersion,
  upgradeProjectSystemBinding,
} from "@/core/shared-assets/api";

const mockedFetch = rs.mocked(fetchWithAuth);
const PROJECT_ID = "33333333-3333-4333-8333-333333333333";
const asset = {
  id: "11111111-1111-4111-8111-111111111111",
  scope: "project",
  project_id: PROJECT_ID,
  slug: "writer",
  display_name: "Writer",
  status: "active",
  current_published_version_id: null,
  version: 1,
  created_by_user_id: "user-1",
  created_at: "2026-07-14T00:00:00Z",
  updated_at: "2026-07-14T00:00:00Z",
};
const projectAsset = {
  ...asset,
  capabilities: ["shared_assets.read"],
  binding: null,
};
const versionId = "22222222-2222-4222-8222-222222222222";
const agentVersion = {
  id: versionId,
  agent_id: asset.id,
  version_number: 1,
  workflow_status: "published",
  description: "Review changes",
  soul: "Be precise",
  model_ref: "default",
  tool_groups: ["web"],
  skill_version_ids: [],
  mcp_version_ids: [],
  supersedes_version_id: null,
  payload_checksum: "a".repeat(64),
  created_by_user_id: "user-1",
  created_at: "2026-07-14T00:00:00Z",
};
const credentialVersion = {
  id: versionId,
  credential_id: asset.id,
  version_number: 1,
  status: "active",
  payload_schema_version: 1,
  payload_schema: { env: ["TOKEN"] },
  supersedes_version_id: null,
  created_by_user_id: "user-1",
  created_at: "2026-07-14T00:00:00Z",
};
const mcpVersion = {
  id: versionId,
  mcp_server_id: asset.id,
  version_number: 1,
  workflow_status: "published",
  definition: {
    description: "GitHub MCP",
    transport: "stdio",
    command: "github-mcp",
    args: [],
    url: null,
    env: {},
    headers: {},
    oauth: {},
    routing: {},
    tool_overrides: {},
    timeout_seconds: 30,
    credential_slots: [],
  },
  credential_slots: [],
  credential_grants: [],
  supersedes_version_id: null,
  payload_checksum: "b".repeat(64),
  submitted_at: "2026-07-14T00:00:00Z",
  reviewed_at: "2026-07-14T00:01:00Z",
  reviewed_by_user_id: "reviewer-1",
  created_by_user_id: "user-1",
  created_at: "2026-07-14T00:00:00Z",
};
const credential = {
  id: asset.id,
  scope: "project",
  project_id: PROJECT_ID,
  name: "github",
  display_name: "GitHub",
  credential_type: "token",
  status: "revoked",
  current_version_id: versionId,
  version: 2,
  created_by_user_id: "user-1",
  created_at: "2026-07-14T00:00:00Z",
  updated_at: "2026-07-14T00:01:00Z",
};
const binding = {
  project_id: PROJECT_ID,
  kind: "agent",
  asset_id: asset.id,
  version_id: versionId,
  enabled: true,
  version: 1,
  created_by_user_id: "user-1",
  updated_by_user_id: "user-1",
  created_at: "2026-07-14T00:00:00Z",
  updated_at: "2026-07-14T00:00:00Z",
  request_id: "req-binding",
};

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  mockedFetch.mockReset();
});

describe("shared asset api", () => {
  test("uses only the authenticated fetcher for project, admin, and system catalog lists", async () => {
    mockedFetch
      .mockResolvedValueOnce(
        jsonResponse(200, {
          system_items: [],
          project_items: [projectAsset],
          request_id: "req-1",
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(200, {
          items: [{ ...asset, scope: "system", project_id: null }],
          request_id: "req-2",
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(200, {
          items: [{ ...asset, scope: "system", project_id: null }],
          request_id: "req-3",
        }),
      );
    const signal = new AbortController().signal;

    await expect(
      listProjectAssets(PROJECT_ID, "agents", signal),
    ).resolves.toMatchObject({ project_items: [projectAsset] });
    await expect(listAdminAssets("agents", signal)).resolves.toMatchObject({
      items: [{ id: asset.id }],
    });
    await expect(
      listSystemAssetCatalog("agents", signal),
    ).resolves.toMatchObject({ items: [{ id: asset.id }] });
    expect(mockedFetch.mock.calls).toEqual([
      [`/backend/api/projects/${PROJECT_ID}/agents`, { signal }],
      ["/backend/api/admin/assets/agents", { signal }],
      ["/backend/api/assets/catalog/agents", { signal }],
    ]);
  });

  test("validates mutation input before sending it through the authenticated fetcher", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(201, { item: asset, request_id: "req-3" }),
    );
    await createProjectAsset(PROJECT_ID, "agents", {
      slug: "writer",
      display_name: "Writer",
    });
    expect(mockedFetch).toHaveBeenCalledWith(
      `/backend/api/projects/${PROJECT_ID}/agents`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ slug: "writer", display_name: "Writer" }),
      }),
    );
    await expect(
      createProjectAsset("not-a-uuid", "agents", {
        slug: "writer",
        display_name: "Writer",
      }),
    ).rejects.toMatchObject({ code: "ASSET_VALIDATION_FAILED" });
    expect(mockedFetch).toHaveBeenCalledTimes(1);
  });

  test("uses canonical public errors and rejects unsafe responses", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(403, {
        detail: {
          code: "asset_forbidden",
          message: "SQL secret should not escape",
          request_id: "req-4",
        },
      }),
    );
    const error = await listAdminAssets("skills").catch(
      (caught: unknown) => caught,
    );
    expect(error).toBeInstanceOf(SharedAssetApiError);
    expect(error).toMatchObject({
      status: 403,
      code: "ASSET_FORBIDDEN",
      message: "Asset capability required",
    });

    mockedFetch.mockResolvedValueOnce(
      jsonResponse(200, {
        items: [
          {
            ...asset,
            scope: "system",
            project_id: null,
            plaintext: "forbidden",
          },
        ],
        request_id: "req-5",
      }),
    );
    await expect(listAdminAssets("agents")).rejects.toMatchObject({
      code: "ASSET_RESPONSE_INVALID",
      message: "Shared asset response was invalid",
    });
  });

  test("maps explicit authentication failures and preserves aborts", async () => {
    mockedFetch.mockRejectedValueOnce(new AuthRequiredError());
    await expect(listAdminAssets("agents")).rejects.toMatchObject({
      status: 401,
      code: "AUTH_REQUIRED",
      message: "Authentication required",
    });

    const aborted = new DOMException("Aborted", "AbortError");
    mockedFetch.mockRejectedValueOnce(aborted);
    await expect(listAdminAssets("agents")).rejects.toBe(aborted);
  });

  test("covers the project version credential approval and binding mutation routes", async () => {
    const responses = [
      { data: [], request_id: "req-history" },
      { data: agentVersion, request_id: "req-version" },
      { data: credentialVersion, request_id: "req-version" },
      { data: mcpVersion, request_id: "req-version" },
      { data: mcpVersion, request_id: "req-version" },
      { item: credential, request_id: "req-credential" },
      binding,
      { ...binding, version: 2 },
      { ...binding, enabled: false, version: 3 },
    ];
    for (const body of responses) {
      mockedFetch.mockResolvedValueOnce(jsonResponse(200, body));
    }

    await listProjectAssetVersions(PROJECT_ID, "agents", asset.id);
    await publishProjectAssetVersion(
      PROJECT_ID,
      "agents",
      asset.id,
      versionId,
      {
        expected_asset_version: 1,
      },
    );
    await replaceProjectCredential(PROJECT_ID, asset.id, {
      payload: { env: { TOKEN: "write-only" } },
      expected_credential_version: 1,
    });
    await submitProjectMcpVersion(PROJECT_ID, asset.id, versionId, {
      expected_asset_version: 1,
    });
    await approveProjectMcpVersion(PROJECT_ID, asset.id, versionId, {
      credential_versions: { github: versionId },
      expected_asset_version: 1,
    });
    await revokeProjectCredential(PROJECT_ID, asset.id, {
      expected_credential_version: 1,
    });
    await enableProjectSystemBinding(PROJECT_ID, "agent", {
      asset_id: asset.id,
      version_id: versionId,
    });
    await upgradeProjectSystemBinding(PROJECT_ID, "agent", asset.id, {
      version_id: versionId,
      expected_binding_version: 1,
    });
    await disableProjectSystemBinding(PROJECT_ID, "agent", asset.id, {
      expected_binding_version: 2,
    });

    expect(mockedFetch.mock.calls.map(([url]) => url)).toEqual([
      `/backend/api/projects/${PROJECT_ID}/agents/${asset.id}/versions`,
      `/backend/api/projects/${PROJECT_ID}/agents/${asset.id}/versions/${versionId}/publish`,
      `/backend/api/projects/${PROJECT_ID}/credentials/${asset.id}/replace`,
      `/backend/api/projects/${PROJECT_ID}/mcp-servers/${asset.id}/versions/${versionId}/submit-approval`,
      `/backend/api/projects/${PROJECT_ID}/mcp-servers/${asset.id}/versions/${versionId}/approve`,
      `/backend/api/projects/${PROJECT_ID}/credentials/${asset.id}/revoke`,
      `/backend/api/projects/${PROJECT_ID}/system-agent-bindings`,
      `/backend/api/projects/${PROJECT_ID}/system-agent-bindings/${asset.id}/upgrade`,
      `/backend/api/projects/${PROJECT_ID}/system-agent-bindings/${asset.id}/disable`,
    ]);
  });

  test("creates typed project and admin versions plus write-only credentials", async () => {
    const responses = [
      { data: agentVersion, request_id: "req-agent" },
      { data: mcpVersion, request_id: "req-mcp" },
      { item: { ...credential, status: "active" }, request_id: "req-cred" },
      { item: { ...credential, status: "active" }, request_id: "req-cred" },
    ];
    for (const body of responses) {
      mockedFetch.mockResolvedValueOnce(jsonResponse(201, body));
    }

    const agentInput = {
      description: "Writer",
      soul: "Be precise",
      model_ref: "default",
      tool_groups: [],
      skill_version_ids: [],
      mcp_version_ids: [],
      expected_asset_version: 1,
    };
    const mcpInput = {
      description: "GitHub",
      transport: "stdio" as const,
      command: "github-mcp",
      args: [],
      url: null,
      env: {},
      headers: {},
      oauth: {},
      routing: {},
      tool_overrides: {},
      timeout_seconds: 30,
      credential_slots: [],
      expected_asset_version: 1,
    };
    const credentialInput = {
      name: "github",
      display_name: "GitHub",
      credential_type: "token",
      payload: { env: { TOKEN: "write-only" } },
    };

    await createProjectAssetVersion(PROJECT_ID, "agents", asset.id, agentInput);
    await createAdminAssetVersion("mcp-servers", asset.id, mcpInput);
    const credentialResult = await createProjectCredential(
      PROJECT_ID,
      credentialInput,
    );
    await createAdminCredential(credentialInput);

    expect(mockedFetch.mock.calls.map(([url]) => url)).toEqual([
      `/backend/api/projects/${PROJECT_ID}/agents/${asset.id}/versions`,
      `/backend/api/admin/assets/mcp-servers/${asset.id}/versions`,
      `/backend/api/projects/${PROJECT_ID}/credentials`,
      "/backend/api/admin/assets/credentials",
    ]);
    expect(mockedFetch.mock.calls[2]?.[1]?.body).toBe(
      JSON.stringify(credentialInput),
    );
    expect(credentialResult).not.toHaveProperty("payload");
  });
});
