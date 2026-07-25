import { beforeEach, describe, expect, test, rs } from "@rstest/core";

rs.mock("@/core/api/fetcher", () => ({
  fetch: rs.fn(),
  AuthRequiredError: class AuthRequiredError extends Error {},
}));
rs.mock("@/core/config", () => ({ getBackendBaseURL: () => "/backend" }));

import { AuthRequiredError, fetch as fetchWithAuth } from "@/core/api/fetcher";
import {
  SharedAssetApiError,
  approveAdminProjectMcpVersion,
  approveProjectMcpVersion,
  configureAdminMcpCredentialGrants,
  createAdminCredential,
  createAdminProjectAsset,
  createAdminProjectCredential,
  createProjectAsset,
  createProjectAssetVersion,
  createProjectCredential,
  disableProjectSystemBinding,
  enableProjectSystemBinding,
  enableAdminProjectSystemBinding,
  forkProjectSkillVersion,
  getProjectSkillVersionFile,
  listAdminAssets,
  listAdminProjectAssets,
  listProjectAssetVersions,
  listProjectAssets,
  listSystemAssetCatalog,
  migrateProjectCredentialGrants,
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
  test("uses only project-scoped admin override routes for shared assets credentials approvals and bindings", async () => {
    mockedFetch
      .mockResolvedValueOnce(
        jsonResponse(200, {
          system_items: [],
          project_items: [projectAsset],
          request_id: "req-override-list",
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(201, { item: asset, request_id: "req-override-create" }),
      )
      .mockResolvedValueOnce(
        jsonResponse(201, {
          item: { ...credential, status: "active" },
          request_id: "req-override-credential",
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(200, {
          data: mcpVersion,
          request_id: "req-override-approval",
        }),
      )
      .mockResolvedValueOnce(jsonResponse(201, binding));

    await listAdminProjectAssets(PROJECT_ID, "agents");
    await createAdminProjectAsset(PROJECT_ID, "agents", {
      slug: "reviewer",
      display_name: "Reviewer",
    });
    const createdCredential = await createAdminProjectCredential(PROJECT_ID, {
      name: "github",
      display_name: "GitHub",
      credential_type: "token",
      payload: { env: { TOKEN: "write-only" } },
    });
    await approveAdminProjectMcpVersion(PROJECT_ID, asset.id, versionId, {
      credential_versions: { primary: versionId },
      expected_asset_version: 2,
    });
    await enableAdminProjectSystemBinding(PROJECT_ID, "agent", {
      asset_id: asset.id,
      version_id: versionId,
    });

    expect(mockedFetch.mock.calls.map(([url]) => url)).toEqual([
      `/backend/api/admin/projects/${PROJECT_ID}/assets/agents`,
      `/backend/api/admin/projects/${PROJECT_ID}/assets/agents`,
      `/backend/api/admin/projects/${PROJECT_ID}/assets/credentials`,
      `/backend/api/admin/projects/${PROJECT_ID}/assets/mcp-servers/${asset.id}/versions/${versionId}/approve`,
      `/backend/api/admin/projects/${PROJECT_ID}/assets/system-agent-bindings`,
    ]);
    expect(
      mockedFetch.mock.calls.some(([url]) =>
        (typeof url === "string"
          ? url
          : url instanceof URL
            ? url.href
            : url.url
        ).includes(`/api/projects/${PROJECT_ID}/`),
      ),
    ).toBe(false);
    expect(createdCredential.item).not.toHaveProperty("payload");
    expect(createdCredential.item).not.toHaveProperty("plaintext");
  });

  test("configures only Credential grants on a published packaged System MCP version", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(200, {
        data: mcpVersion,
        request_id: "req-system-mcp-grants",
      }),
    );
    const input = {
      credential_versions: { primary: versionId },
      expected_active_grant_versions: { primary: 2 },
    };

    await configureAdminMcpCredentialGrants(asset.id, versionId, input);

    expect(mockedFetch).toHaveBeenCalledWith(
      `/backend/api/admin/assets/mcp-servers/${asset.id}/versions/${versionId}/credential-grants`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(input),
      }),
    );
  });

  test("loads one skill file lazily and forks edits without sending unchanged files", async () => {
    const sourceChecksum = "d".repeat(64);
    const fileResponse = {
      data: {
        path: "references/guide.md",
        media_type: "text/markdown",
        size_bytes: 5,
        sha256: "c".repeat(64),
        preview_status: "ready",
        encoding: "utf-8",
        content: "Guide",
        source_payload_checksum: sourceChecksum,
        asset_version: 3,
      },
      request_id: "req-file",
    };
    const skillVersion = {
      id: versionId,
      skill_id: asset.id,
      version_number: 2,
      workflow_status: "draft",
      description: "Updated",
      frontmatter: {},
      compatibility: null,
      secret_requirements: [],
      scan_decision: "allow",
      scan_rule_ids: [],
      scan_summary: {},
      file_views: [],
      supersedes_version_id: versionId,
      payload_checksum: "e".repeat(64),
      created_by_user_id: "user-1",
      created_at: "2026-07-22T00:00:00Z",
    };
    mockedFetch
      .mockResolvedValueOnce(jsonResponse(200, fileResponse))
      .mockResolvedValueOnce(
        jsonResponse(201, {
          data: skillVersion,
          request_id: "req-fork",
        }),
      );
    const signal = new AbortController().signal;

    await expect(
      getProjectSkillVersionFile(
        PROJECT_ID,
        asset.id,
        versionId,
        "references/guide.md",
        signal,
      ),
    ).resolves.toEqual(fileResponse);
    await forkProjectSkillVersion(PROJECT_ID, asset.id, versionId, {
      expected_asset_version: 3,
      expected_source_payload_checksum: sourceChecksum,
      changes: [
        {
          op: "replace",
          path: "references/guide.md",
          content: "Updated guide",
          media_type: "text/markdown",
        },
      ],
    });

    expect(mockedFetch.mock.calls[0]).toEqual([
      `/backend/api/projects/${PROJECT_ID}/skills/${asset.id}/versions/${versionId}/files/content?path=references%2Fguide.md`,
      { signal },
    ]);
    expect(mockedFetch.mock.calls[1]).toEqual([
      `/backend/api/projects/${PROJECT_ID}/skills/${asset.id}/versions/${versionId}/fork`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          expected_asset_version: 3,
          expected_source_payload_checksum: sourceChecksum,
          changes: [
            {
              op: "replace",
              path: "references/guide.md",
              content: "Updated guide",
              media_type: "text/markdown",
            },
          ],
        }),
      }),
    ]);
  });

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
      {
        credential_id: asset.id,
        credential_version_id: versionId,
        migrated_count: 2,
        request_id: "req-migration",
      },
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
    const migration = await migrateProjectCredentialGrants(
      PROJECT_ID,
      asset.id,
      { expected_credential_version: 2 },
    );
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
      `/backend/api/projects/${PROJECT_ID}/credentials/${asset.id}/migrate-grants`,
      `/backend/api/projects/${PROJECT_ID}/mcp-servers/${asset.id}/versions/${versionId}/submit-approval`,
      `/backend/api/projects/${PROJECT_ID}/mcp-servers/${asset.id}/versions/${versionId}/approve`,
      `/backend/api/projects/${PROJECT_ID}/credentials/${asset.id}/revoke`,
      `/backend/api/projects/${PROJECT_ID}/system-agent-bindings`,
      `/backend/api/projects/${PROJECT_ID}/system-agent-bindings/${asset.id}/upgrade`,
      `/backend/api/projects/${PROJECT_ID}/system-agent-bindings/${asset.id}/disable`,
    ]);
    expect(migration.migrated_count).toBe(2);
    expect(mockedFetch.mock.calls[3]?.[1]?.body).toBe(
      JSON.stringify({ expected_credential_version: 2 }),
    );
  });

  test("creates typed project versions plus write-only project and system credentials", async () => {
    const responses = [
      { data: agentVersion, request_id: "req-agent" },
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
    const credentialInput = {
      name: "github",
      display_name: "GitHub",
      credential_type: "token",
      payload: { env: { TOKEN: "write-only" } },
    };

    await createProjectAssetVersion(PROJECT_ID, "agents", asset.id, agentInput);
    const credentialResult = await createProjectCredential(
      PROJECT_ID,
      credentialInput,
    );
    await createAdminCredential(credentialInput);

    expect(mockedFetch.mock.calls.map(([url]) => url)).toEqual([
      `/backend/api/projects/${PROJECT_ID}/agents/${asset.id}/versions`,
      `/backend/api/projects/${PROJECT_ID}/credentials`,
      "/backend/api/admin/assets/credentials",
    ]);
    expect(mockedFetch.mock.calls[1]?.[1]?.body).toBe(
      JSON.stringify(credentialInput),
    );
    expect(credentialResult).not.toHaveProperty("payload");
  });
});
